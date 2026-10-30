"""Linker engine (stage 2): lines_snapshot.db -> ref-based artifacts.

Deterministic function: for each source book in the snapshot, run the Sefaria linker
over its cleaned lines and emit one artifact file of unambiguous citation links.

Key contracts:
- Runs on the *snapshot* (post-cleaning line.content) — never raw merged.json/.txt —
  so `start`/`end` index the exact bytes the DB stores. See LINKER_IMPLEMENTATION_STAGES.md.
- Ambiguity policy: DROP by default (a clickable link cannot be ambiguous). `--bavli-convention`
  optionally keeps the one case the corpus shows is decidable (Bavli/Yerushalmi, Mishnah/Gemara).
- Output format is owned by linker_artifact.py — this module never re-implements it.

Run (from the Sefaria-Project dir, its venv, with the NER server up):
  cd <Sefaria-Project> && PYTHONPATH=. .venv/bin/python <repo>/src/link_books.py \
      --snapshot /path/to/lines_snapshot.db --repo <repo> --run-dir /path/to/workdir [--label w1]
"""
from __future__ import annotations

import argparse
import collections
import functools
import hashlib
import itertools
import os
import re
import resource
import sqlite3
import sys
import threading
import time
import uuid
import warnings
from contextlib import contextmanager

# django/requests are imported lazily (main/ner_alive): the incremental driver imports
# this module for claim_id alone and must not drag the whole Sefaria stack with it.

# linker_artifact lives next to this file — import it as the single format authority.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linker_artifact import (  # noqa: E402
    BookKey, LinkRecord, book_key_to_relpath, content_hash, remove_artifact, write_artifact,
)
from line_baseline import indices_from_ranges, placed_by_daf_headings, requires_book_context  # noqa: E402

# Lines per bulk NER call. This is transport granularity: the explicit ibid context
# below keeps citation history continuous across these boundaries. Tunable per host:
# GPU serving OOMs on 100-line batches of monster books (16GB VRAM), so Kaggle runs
# use a smaller batch via env.
BATCH_LINES = int(os.environ.get("LINKER_BATCH_LINES", "100"))
# A line-count ceiling alone lets one pathological HTML/data row dominate a batch.
# Keep ordinary batches large, but split before the aggregate source text becomes
# unreasonably expensive for normalization, JSON transport, or resolution.  A single
# over-budget line is still emitted alone so no source row can be silently skipped.
BATCH_CHARS = int(os.environ.get("LINKER_BATCH_CHARS", "120000"))
# Give up on a dead NER after this long (crash-looped GPU service, not a blip).
NER_MAX_WAIT_SEC = int(os.environ.get("LINKER_NER_MAX_WAIT_SEC", "1800"))
# Self-recycle above this many bytes. Overridable per machine: recycling costs a full
# library reload (~8s on M-series, ~22s on Neoverse-N1), so give workers headroom when
# RAM allows (e.g. 3e9 on a 22GB box with 2 workers) and keep the tight default for CI.
RSS_CAP = float(os.environ.get("LINKER_RSS_CAP_BYTES", 1.8e9))
GC_EVERY_BATCHES = max(1, int(os.environ.get("LINKER_GC_EVERY_BATCHES", "20")))
HEARTBEAT_SECONDS = max(5, int(os.environ.get("LINKER_HEARTBEAT_SECONDS", "30")))
CLAIM_STALE_SEC = 900    # compatibility argument; the kernel lock is liveness authority
RESCAN_WAIT_SEC = max(1, int(os.environ.get("LINKER_RESCAN_SECONDS", "60")))
# Heavy books.  A handful of texts grow a resolver by many GB inside one book (one
# reached 12.5 GB on 2026-09-03 and took the whole VM down).  Instead of dropping such
# a book, a worker that grows by more than this many bytes over the book (judged on the
# same figure as the recycle cap) DEFERS it: its finished shards stay, a marker sends
# it to the heavy phase, and the worker recycles.  0 disables deferral.
HEAVY_BOOK_GROWTH_BYTES = float(os.environ.get("LINKER_HEAVY_BOOK_GROWTH_BYTES", 2.5e9))
# The heavy phase starts only once every other book is done; then at most this many
# deferred books resolve concurrently (kernel-locked slots).  Budget: slots x
# (HEAVY_RSS_CAP + one batch of growth, judged only between batches) + the pool
# master + idle children + mongod's cache must stay below the VM.  That VM is the
# WSL2 VM of the RUNNER's own Windows account, sized by that account's .wslconfig -
# outside this repo and not readable from any other account - so the only honest
# figure for a given run is its OWN perf/host.jsonl: relink 34016397157 measured
# MemTotal 41,072,256 KiB = 42.1 GB with 12 GiB of swap.  This comment used to assert
# a flat "32 GB VM", which it was not.  The interactive WSL VM on the same machine
# belongs to a DIFFERENT Windows account (26 GB with 32 GiB of swap) and is NOT this
# one: the swap figures alone tell the two apart.  Never size the linker from it.
HEAVY_BOOK_SLOTS = max(1, int(os.environ.get("LINKER_HEAVY_BOOK_SLOTS", "2")))
# Recycle cap while resolving a deferred book under a slot: the ordinary cap would
# recycle such a worker after nearly every batch.  Judged on the same figure as RSS_CAP.
HEAVY_RSS_CAP = float(os.environ.get("LINKER_HEAVY_RSS_CAP_BYTES", 5e9))
# A worker idling in the heavy phase (no free slot) still holds the private memory it
# accumulated; above this many bytes it recycles so the heavy books get that RAM.
HEAVY_IDLE_RECYCLE_BYTES = float(os.environ.get("LINKER_HEAVY_IDLE_RECYCLE_BYTES", 3e8))
# A deferred (heavy) book is taken ONLY by a worker whose current life is fresh.  In
# relink 34016397157 the same book that a fresh worker linked in 2.3 s killed the run
# on a worker that had already processed 75 books this life: one line asked for
# 11.9 GB on top of that accumulated Ref cache and hit the 26 GB address-space ceiling
# (`w06 ERROR ... line 43 failed to link: MemoryError`).  Heavy books are the ones
# that need the whole budget, so they get a worker that owns none of it yet.  A worker
# that is not fresh recycles BEFORE claiming the book (nothing is held yet, so nothing
# leaks); the same book is then picked up by the fresh image.  0/false disables.
HEAVY_FRESH_WORKER = os.environ.get("LINKER_HEAVY_FRESH_WORKER", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")
# "Fresh" is `no book completed in this life` OR `private growth below this` - a
# disjunction on purpose: a fresh image has processed 0, so it can never be sent
# round again by this rule, whatever the memory reading says (no recycle loop).
# Same figure as the idle-recycle threshold: below it a life is not worth shedding.
HEAVY_FRESH_GROWTH_BYTES = float(os.environ.get("LINKER_HEAVY_FRESH_GROWTH_BYTES", 3e8))
# Last-resort address-space ceilings (RLIMIT_AS soft limit, bytes; 0 = unlimited).  A
# single line that balloons past the deferral check gets a MemoryError inside THIS
# worker - which defers the book in the normal phase, and in the heavy phase requeues
# it once onto a fresh worker before failing the run - instead of the kernel OOM
# killer taking down the runner.  RLIMIT_AS bounds VIRTUAL size per process and is a
# ceiling, never a reservation: two heavy slots may map more than the VM holds, which
# is precisely why the ceiling exists (a MemoryError inside one worker instead of an
# OOM kill of the runner) and why the fresh-worker rule above removes the accumulated
# growth that let one ordinary book reach it.
WORKER_ADDRESS_SPACE_BYTES = int(float(os.environ.get("LINKER_WORKER_ADDRESS_SPACE_BYTES", "0")))
HEAVY_WORKER_ADDRESS_SPACE_BYTES = int(float(os.environ.get("LINKER_HEAVY_WORKER_ADDRESS_SPACE_BYTES", "0")))
# Pool memory governor: the ceilings above are per process, so together they can
# exhaust the host (36112302819 took the whole machine down). Below the soft floor
# (MemAvailable) the largest worker is asked to drop its book; below the hard floor,
# or after the grace, it is killed. 0 = a fraction of MemTotal.
POOL_MEMORY_GOVERNOR = os.environ.get("LINKER_POOL_MEMORY_GOVERNOR", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")
POOL_MEMORY_SOFT_FLOOR_BYTES = float(os.environ.get("LINKER_POOL_MEMORY_SOFT_FLOOR_BYTES", "0"))
POOL_MEMORY_HARD_FLOOR_BYTES = float(os.environ.get("LINKER_POOL_MEMORY_HARD_FLOOR_BYTES", "0"))
POOL_MEMORY_SOFT_FLOOR_FRACTION = 0.18
POOL_MEMORY_HARD_FLOOR_FRACTION = 0.10
POOL_MEMORY_MIN_VICTIM_BYTES = float(os.environ.get("LINKER_POOL_MEMORY_MIN_VICTIM_BYTES", 2e9))
POOL_MEMORY_GRACE_SECONDS = float(os.environ.get("LINKER_POOL_MEMORY_GRACE_SECONDS", "20"))
POOL_MEMORY_KILL_INTERVAL_SECONDS = 2.0
# Heavy-phase memory retries continue while the book commits new batches (36072909683:
# 1108 -> 1971 -> 6193); a second standstill in a row fails it. Hard cap below.
MEMORY_RETRY_LIMIT = max(1, int(os.environ.get("LINKER_MEMORY_RETRY_LIMIT", "40")))
NER_URL = "http://127.0.0.1:5051/recognize-entities"
# Exit status a forked pool child uses to ask the warm master for a fresh image (see
# run_pool): the equivalent of the standalone worker's self-exec, without reloading
# the library.  Distinct from every status the engine or the kernel otherwise produces.
RECYCLE_EXIT_CODE = 75
# True inside a run_pool child: switches the recycle cap to private memory and the
# recycle action from execv to a pool exit.
_POOL_CHILD = False
HEARTBEAT_NAMESPACE_ENV = "LINKER_HEARTBEAT_NAMESPACE"
_HEARTBEAT_NAMESPACE_RE = re.compile(r"[0-9a-f]{32}")

_HEADING_RE = re.compile(r"^[\s\ufeff]*<h[1-6](?:\s|>)", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_HEBREW_RE = re.compile(r"[\u0590-\u05ff]")


def is_heading_line(content: str) -> bool:
    """Structural headings are navigation labels, never clickable prose."""
    return bool(_HEADING_RE.match(content or ""))


def is_ner_eligible_line(content: str) -> bool:
    """Return whether a stored row contains visible Hebrew prose for the NER.

    Besides headings, this excludes image-only/data rows whose hidden payload can
    be megabytes long. The pinned linker is Hebrew-only, so sending such rows adds
    no links and can make a batch needlessly slow or run out of memory.
    """
    if not content or len(content.strip()) <= 1 or is_heading_line(content):
        return False
    visible = _HTML_TAG_RE.sub(" ", content)
    return len(_HEBREW_RE.findall(visible)) >= 2


def transport_batches(lines, selected_indices=None):
    """Yield deterministic, content-bounded NER/resolver batches.

    ``batch_start`` is the first row's ordinal position in the immutable per-book
    snapshot, not its user-visible line index.  Producer and resolver share this
    helper, making the raw-NER handoff fail closed if either boundary changes.
    """
    selected = None if selected_indices is None else set(selected_indices)
    batch = []
    batch_chars = 0
    batch_start = None
    for position, row in enumerate(lines):
        line_index, content, _context_ref = row
        if selected is not None and line_index not in selected:
            continue
        if not is_ner_eligible_line(content):
            continue
        size = len(content)
        if batch and (len(batch) >= BATCH_LINES or batch_chars + size > BATCH_CHARS):
            yield batch_start, batch
            batch = []
            batch_chars = 0
            batch_start = None
        if batch_start is None:
            batch_start = position
        batch.append(row)
        batch_chars += size
    if batch:
        yield batch_start, batch


def transport_seams(lines) -> list[int]:
    """The line opening each transport batch after the first, over the whole book."""
    return [batch[0][0] for _, batch in transport_batches(lines)][1:]


@contextmanager
def periodic_heartbeat(callback, interval=HEARTBEAT_SECONDS):
    """Refresh a live worker/book claim while one resolver batch is blocked."""
    stop = threading.Event()
    errors = []

    def run():
        while not stop.wait(interval):
            try:
                callback()
            except BaseException as error:  # surfaced on the owning worker thread
                errors.append(error)
                return

    callback()
    thread = threading.Thread(target=run, name="linker-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
    if errors:
        raise RuntimeError("periodic linker heartbeat failed") from errors[0]


# Sefaria's linker package logs at INFO through the root handler Django installs, and
# nothing in this repo ever set a level on it: one line per DH cache miss plus two per
# bulk_link call made 441k lines / 121 MB of the 123 MB relink log of 2026-09-06 —
# 98% of it — burying every real message and every worker's progress.  None of it is
# progress (perf-<label>.jsonl already records per-batch timings, and the incremental
# driver prints a `progress:` line per minute), so the whole package drops to WARNING.
# The two emitters are named explicitly as well: a level configured on a CHILD logger
# by Django's LOGGING dict would out-vote the package level and bring the flood back.
QUIET_SEFARIA_LOGGERS = (
    "sefaria.model.linker",
    "sefaria.model.linker.linker",
    "sefaria.model.linker.referenceable_book_node",
)


def quiet_sefaria_linker_logs() -> dict:
    """Drop Sefaria's linker INFO firehose to WARNING.  WARNING+ is untouched.

    Call AFTER django.setup(): that is what applies Django's LOGGING dict, which would
    otherwise reset the levels set here.  Pool children inherit the levels across
    fork (logging state is ordinary process memory), so the master setting them once
    before forking covers every resolver.

    Idempotent, and called a second time immediately before the workers start: the
    whole Sefaria/linker/NER import chain runs in between, and anything in it that
    re-applied a logging dictConfig would silently restore the 121 MB firehose.
    Returns the resulting EFFECTIVE level per logger so the caller can state, in one
    line, that the silencing actually took.
    """
    import logging
    levels = {}
    for name in QUIET_SEFARIA_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        levels[name] = logging.getLevelName(logger.getEffectiveLevel())
    return levels


def validate_snapshot_contract(con) -> None:
    """Require the context-aware snapshot produced by the current DB build."""
    try:
        meta = dict(con.execute("SELECT key, value FROM lines_snapshot_meta"))
        columns = {
            row[1] for row in con.execute("PRAGMA table_info(lines_snapshot)")
        }
    except sqlite3.DatabaseError as error:
        raise RuntimeError(f"invalid lines snapshot: {error}") from error
    if meta.get("schema_version") != "2" or meta.get("context_policy") != "explicit-relative-v1":
        raise RuntimeError(
            "snapshot must be schema 2 with context_policy=explicit-relative-v1; "
            "rebuild Phase-1 with the current SeforimLibrary generator"
        )
    required = {"source_name", "canonical_he_title", "line_index", "content", "context_ref"}
    if columns != required:
        raise RuntimeError(f"unexpected lines_snapshot columns: {sorted(columns)!r}")


def rss_bytes() -> int:
    """Return the process's *current* resident set, not its historical maximum.

    Linux preserves ``ru_maxrss`` across ``execve``.  This worker deliberately
    self-recycles with ``os.execv``; using the high-water mark therefore made every
    fresh image inherit the old over-cap value and immediately exec again forever.
    ``/proc/self/statm`` is the kernel's current-RSS source on Linux.  ``ps`` is a
    portable current-RSS fallback; ``getrusage`` remains only the last resort.
    """
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/statm", encoding="ascii") as fh:
                fields = fh.read().split()
            resident_pages = int(fields[1])
            if resident_pages < 0:
                raise ValueError("negative resident page count")
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, IndexError):
            pass
    try:
        import subprocess
        rss_kib = int(subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True,
        ).strip())
        if rss_kib < 0:
            raise ValueError("negative RSS")
        return rss_kib * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        # Last-resort estimate.  ru_maxrss is bytes on macOS and KiB elsewhere.
        multiplier = 1 if sys.platform == "darwin" else 1024
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * multiplier


def private_bytes() -> int:
    """Private (non-shared) resident bytes of this process, from ``/proc/self/smaps_rollup``.

    A forked pool child shares the master's loaded library copy-on-write, so its plain
    RSS starts at the master's size and says nothing about the child's own growth.  In
    pool mode the recycle cap therefore judges ``Private_Clean + Private_Dirty``: the
    pages this child actually owns (its Ref cache, batch documents, dirtied shared pages).
    """
    total = 0
    with open("/proc/self/smaps_rollup", encoding="ascii") as fh:
        for line in fh:
            if line.startswith(("Private_Clean:", "Private_Dirty:")):
                total += int(line.split()[1]) * 1024
    return total


def worker_memory_bytes() -> int:
    """The figure the recycle cap is judged against for this worker image."""
    if _POOL_CHILD:
        return private_bytes()
    return rss_bytes()


def heartbeat_directory(run_dir: str) -> str:
    """The current driver's private heartbeat directory, or the legacy default.

    The incremental driver creates a 128-bit namespace per invocation.  Pool children
    inherit it from their master, so an old durable-run heartbeat can never inflate the
    next run's ``workers_alive`` progress number.  Standalone use keeps the historical
    flat directory for compatibility.
    """
    namespace = os.environ.get(HEARTBEAT_NAMESPACE_ENV)
    if namespace is None:
        return os.path.join(run_dir, "worker-heartbeats")
    if not _HEARTBEAT_NAMESPACE_RE.fullmatch(namespace):
        raise RuntimeError(f"invalid {HEARTBEAT_NAMESPACE_ENV} value")
    return os.path.join(run_dir, "worker-heartbeats", namespace)


def prepare_pool_child(snapshot: str, run_dir: str, label: str):
    """Per-child state after fork: Django handles closed, own SQLite handle, own
    heartbeat path, and proof that the private-memory cap can be judged here.

    Returns ``(connection, heartbeat_path)``.  Kept outside main() so the one piece of
    the pool that touches inherited resources is unit-testable without Sefaria.
    """
    try:
        from django.db import connections as _django_connections
        _django_connections.close_all()
    except Exception:
        pass
    # The recycle cap judges PRIVATE memory in a pool child (see private_bytes); fail
    # loudly here rather than letting every child trip the zero-progress guard if the
    # kernel lacks smaps_rollup.
    try:
        private_bytes()
    except OSError as error:
        raise RuntimeError(
            "--pool-workers requires /proc/self/smaps_rollup for the private "
            f"memory cap: {error}"
        ) from error
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    return connection, os.path.join(heartbeat_directory(run_dir), label)


def _recycle_process() -> None:
    """Replace this worker image.

    Standalone workers exec themselves afresh (a full library reload).  A pool child
    instead exits with RECYCLE_EXIT_CODE so the master forks a fresh child from its
    already-loaded image; the caller has already released every claim it held.
    """
    if _POOL_CHILD:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(RECYCLE_EXIT_CODE)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ── pool memory governor ──────────────────────────────────────────────────────────
class HostMemoryPressure(BaseException):
    """The governor asked this worker to drop its book.

    BaseException so Sefaria's ``except Exception`` cannot swallow it; the book loop
    handles it exactly like a MemoryError.
    """


MEMORY_CURRENT_DIR = "current"
GOVERNOR_KILL_ERROR = "killed by the pool memory governor"
SINGLE_HEAVY_MARKER = "heavy-one-at-a-time"
_MEMORY_WINDOW = False
_MEMORY_RECYCLE_REQUESTED = False
# Set by ANY MemoryError or abort in this image, even one Sefaria swallows (a bare
# ``except:`` there drops a citation silently): such an image commits nothing more.
_IMAGE_TAINTED = False
_TAINT_TOOL_ID = 3


def _taint_on_raise(_code, _offset, exception):
    global _IMAGE_TAINTED
    if isinstance(exception, (MemoryError, HostMemoryPressure)):
        _IMAGE_TAINTED = True


def install_memory_taint_monitor() -> bool:
    """Watch every raise in this process (sys.monitoring, Python 3.12+)."""
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None:
        return False
    try:
        monitoring.use_tool_id(_TAINT_TOOL_ID, "linker-memory-taint")
    except ValueError:
        return monitoring.get_tool(_TAINT_TOOL_ID) == "linker-memory-taint"
    monitoring.register_callback(_TAINT_TOOL_ID, monitoring.events.RAISE, _taint_on_raise)
    monitoring.set_events(_TAINT_TOOL_ID, monitoring.events.RAISE)
    return True


class TaintedImage(HostMemoryPressure):
    """The commit gate's refusal (a swallowed MemoryError or abort in this image)."""


def refuse_to_commit_after_memory_trouble() -> None:
    """The commit gate: nothing computed after a MemoryError or abort is written."""
    if _IMAGE_TAINTED:
        raise TaintedImage(
            "this worker image hit a MemoryError or a governor abort; it commits nothing more"
        )


def single_heavy_marker_path(run_dir: str) -> str:
    return os.path.join(run_dir, MEMORY_DIR, SINGLE_HEAVY_MARKER)


def single_heavy_mode(run_dir: str) -> bool:
    """Heavy books run one at a time until the book the governor stopped is done
    (else two giants would stop each other in turn)."""
    try:
        with open(single_heavy_marker_path(run_dir), encoding="ascii") as fh:
            cid = fh.read().strip()
    except OSError:
        return False
    return bool(cid) and not os.path.exists(os.path.join(run_dir, "done", cid))


def enter_single_heavy_mode(run_dir: str, cid: str, log) -> None:
    if single_heavy_mode(run_dir):
        return
    path = single_heavy_marker_path(run_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.tmp-{os.getpid()}"
        with open(temporary, "w", encoding="ascii") as fh:
            fh.write(cid)
        os.replace(temporary, path)
    except OSError:
        return
    log("heavy phase: the memory governor had to stop a book; heavy books run one at a "
        "time until it is done")


def _on_memory_pressure(_signum, _frame):
    """SIGUSR1: abort the book in progress, or recycle at the next safe point."""
    global _MEMORY_RECYCLE_REQUESTED, _IMAGE_TAINTED
    _MEMORY_RECYCLE_REQUESTED = True
    if _MEMORY_WINDOW:
        _IMAGE_TAINTED = True
        raise HostMemoryPressure(
            "host MemAvailable fell below the pool memory floor; the pool memory "
            "governor stopped this book"
        )


def install_memory_pressure_handler() -> None:
    """Pool children only: the master is the one process that never resolves."""
    import signal
    signal.signal(signal.SIGUSR1, _on_memory_pressure)
    signal.siginterrupt(signal.SIGUSR1, False)


def current_book_path(run_dir: str, label: str) -> str:
    return os.path.join(run_dir, MEMORY_DIR, MEMORY_CURRENT_DIR, label)


@contextmanager
def memory_pressure_window(run_dir: str, label: str, record: dict, log=None):
    """While resolving a book: abortable, and the book is published for the master."""
    import json as _json
    global _MEMORY_WINDOW
    path = current_book_path(run_dir, label)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.tmp-{os.getpid()}"
        with open(temporary, "w", encoding="utf-8") as fh:
            _json.dump(record, fh, ensure_ascii=False, sort_keys=True)
        os.replace(temporary, path)
    except Exception as error:
        if log is not None:
            log(f"pool memory governor: could not publish the current book ({error}); "
                "a kill of this worker could not be accounted")
    _MEMORY_WINDOW = True
    try:
        if _MEMORY_RECYCLE_REQUESTED:
            raise HostMemoryPressure("the pool memory governor asked before the book began")
        yield
    finally:
        # The published book stays until the book loop is done with it (its own
        # failure accounting included), so a kill in between is still charged.
        _MEMORY_WINDOW = False


def close_memory_window() -> None:
    """From here the book only assembles its shards: no abort may land."""
    global _MEMORY_WINDOW
    _MEMORY_WINDOW = False


def read_current_book(run_dir: str, label: str):
    """The book a (dead) worker had published on entering its window, or None."""
    import json as _json
    try:
        with open(current_book_path(run_dir, label), encoding="utf-8") as fh:
            value = _json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    if not all(isinstance(value.get(key), str)
               for key in ("claim_id", "source_name", "canonical_he_title")):
        return None
    return value


def clear_current_book(run_dir: str, label: str) -> None:
    try:
        os.remove(current_book_path(run_dir, label))
    except OSError:
        pass


def mem_available_bytes():
    """MemAvailable of this (guest) kernel, or None where /proc/meminfo is absent."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def mem_total_bytes():
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def owned_bytes_from_smaps(text: str) -> int:
    """Private resident plus swapped bytes from an smaps_rollup text."""
    total = 0
    for line in text.splitlines():
        if line.startswith(("Private_Clean:", "Private_Dirty:", "SwapPss:")):
            total += int(line.split()[1]) * 1024
    return total


def process_private_bytes(pid: int):
    """What another process (a pool child) owns, swap included, or None if gone."""
    try:
        with open(f"/proc/{int(pid)}/smaps_rollup", encoding="ascii") as fh:
            return owned_bytes_from_smaps(fh.read())
    except (OSError, ValueError, IndexError):
        return None


def pool_memory_floors(total=None) -> tuple:
    """(soft, hard) MemAvailable floors in bytes; explicit env wins over fractions."""
    if total is None:
        total = mem_total_bytes() or 0
    soft = POOL_MEMORY_SOFT_FLOOR_BYTES or total * POOL_MEMORY_SOFT_FLOOR_FRACTION
    hard = POOL_MEMORY_HARD_FLOOR_BYTES or total * POOL_MEMORY_HARD_FLOOR_FRACTION
    return float(soft), float(min(hard, soft))


class PoolMemoryGovernor:
    """Once per pool poll: ask the largest worker to drop its book (one request at a
    time), kill it after the grace or below the hard floor. Readers are injectable."""

    def __init__(self, *, log, soft_floor, hard_floor, min_victim=POOL_MEMORY_MIN_VICTIM_BYTES,
                 grace=POOL_MEMORY_GRACE_SECONDS, kill_interval=POOL_MEMORY_KILL_INTERVAL_SECONDS,
                 read_available=mem_available_bytes, read_private=process_private_bytes,
                 send_signal=os.kill):
        self.log = log
        self.soft_floor = float(soft_floor)
        self.hard_floor = float(hard_floor)
        self.min_victim = float(min_victim)
        self.grace = float(grace)
        self.kill_interval = float(kill_interval)
        self.read_available = read_available
        self.read_private = read_private
        self.send_signal = send_signal
        self.asked = {}      # pid -> unix time the child was asked
        self.reasked = set()
        self.killed = {}     # pid -> private bytes at the kill
        self.last_kill = 0.0
        self.last_idle_note = 0.0

    def forget(self, pid: int) -> None:
        self.asked.pop(pid, None)
        self.reasked.discard(pid)

    def was_killed(self, pid: int):
        """Private bytes the governor killed ``pid`` at, or None if it did not."""
        return self.killed.pop(pid, None)

    def tick(self, active: dict, now: float) -> None:
        import signal
        available = self.read_available()
        if available is None or available >= self.soft_floor:
            if available is not None and available >= self.soft_floor * 1.25:
                self.asked.clear()  # recovered: a later dip starts with a fresh grace
                self.reasked.clear()
            return
        if now - self.last_kill < self.kill_interval:
            return  # let the last kill free its memory first
        sizes = []
        for label, (pid, _spawned) in active.items():
            if pid in self.killed:
                continue
            size = self.read_private(pid)
            if size is not None and size >= self.min_victim:
                sizes.append((size, label, pid))
        if not sizes:
            if now - self.last_idle_note >= 60:
                self.last_idle_note = now
                self.log(
                    f"pool memory governor: MemAvailable {format_bytes_gb(available)} is "
                    f"below the {format_bytes_gb(self.soft_floor)} floor but no worker owns "
                    f"{format_bytes_gb(self.min_victim)}; not intervening"
                )
            return
        sizes.sort(reverse=True)
        size, label, pid = sizes[0]
        overdue = pid in self.asked and now - self.asked[pid] >= self.grace
        if available < self.hard_floor or overdue:
            reason = ("below the hard floor " + format_bytes_gb(self.hard_floor)
                      if available < self.hard_floor else
                      f"still alive {now - self.asked[pid]:.0f}s after being asked")
            self.log(
                f"pool memory governor: MemAvailable {format_bytes_gb(available)} {reason}; "
                f"killing {label} pid={pid} ({format_bytes_gb(size)} private)"
            )
            try:
                self.send_signal(pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            self.killed[pid] = int(size)
            self.last_kill = now
            return
        live = {child_pid for child_pid, _spawned in active.values()}
        pending = [asked_pid for asked_pid, asked_at in self.asked.items()
                   if asked_pid in live and now - asked_at < self.grace]
        if pending:
            # One request at a time; repeat it once at half the grace in case the
            # abort landed where Python swallows it (a finalizer).
            for asked_pid in pending:
                if (asked_pid not in self.reasked
                        and now - self.asked[asked_pid] >= self.grace / 2):
                    self.reasked.add(asked_pid)
                    try:
                        self.send_signal(asked_pid, signal.SIGUSR1)
                    except ProcessLookupError:
                        pass
            return
        if pid in self.asked:
            return
        self.log(
            f"pool memory governor: MemAvailable {format_bytes_gb(available)} below the "
            f"{format_bytes_gb(self.soft_floor)} floor; asking {label} pid={pid} "
            f"({format_bytes_gb(size)} private) to give up its book"
        )
        try:
            self.send_signal(pid, signal.SIGUSR1)
        except ProcessLookupError:
            return
        self.asked[pid] = now


def _os_thread_count():
    """Threads the KERNEL sees — the number CPython itself judges at fork(), which
    counts threads started by native libraries that ``threading`` never hears about."""
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def fork_thread_inventory() -> str:
    """One line naming everything alive at the moment the pool forks.

    Every relink log carries two warnings about this instant — CPython's "This process
    is multi-threaded, use of fork()" and pymongo's "MongoClient opened before fork" —
    and neither names a single thread, so the 2026-09-06 post-mortem could not tell
    whether the threads were ours to stop.  This states them: the Python threads by
    name, and the kernel's own count next to it (a native runtime's threads exist for
    fork() even though ``threading`` cannot see them).
    """
    names = sorted(thread.name for thread in threading.enumerate())
    os_threads = _os_thread_count()
    return (
        f"fork-time threads: {len(names)} python "
        f"({', '.join(names) or 'none'}), "
        f"{'unknown' if os_threads is None else os_threads} OS"
    )


def run_pool(worker_count, run_dir, master_label, child_setup, child_body, *,
             restart_limit=2, stall_seconds=1800.0, log=print, poll_seconds=1.0,
             governor=None) -> int:
    """Fork ``worker_count`` resolver children from this already-loaded process.

    The master keeps the loaded Sefaria library, resolver tries and verified NER
    bundle; every child shares those pages copy-on-write instead of loading its own
    ~2 GB copy.  ``gc.freeze()`` moves the shared objects out of every child's
    collector so their GC traffic does not needlessly dirty shared pages.

    Per child the master reproduces the standalone supervisor contract: a heartbeat
    file per label that a stall watchdog judges, bounded replacement of a child that
    dies with a nonzero status, and an immediate re-fork of a child that exits with
    RECYCLE_EXIT_CODE (the pool's cheap equivalent of self-exec).  The master refreshes
    its own heartbeat (``master_label``) so the outer driver's watchdog sees it alive
    while children work.  Children stay in the master's process group, so the outer
    driver's group TERM/KILL reaches them; the master also forwards SIGTERM itself.

    ``child_setup(label)`` runs in the child before ``child_body()``; the body's int
    return (None = 0) becomes the child's exit status.  Returns 0 when the last life
    of every label ended cleanly, else 1 — the driver still judges the ledger.
    """
    import gc
    import json as _json
    import signal
    import traceback

    heartbeat_dir = heartbeat_directory(run_dir)
    os.makedirs(heartbeat_dir, exist_ok=True)
    master_hb = os.path.join(heartbeat_dir, master_label)
    labels = [f"w{n:02d}" for n in range(1, int(worker_count) + 1)]
    active = {}          # label -> (pid, spawned_at)
    codes = {label: [] for label in labels}
    restarts = {label: 0 for label in labels}
    terminating = False

    def touch_master():
        open(master_hb, "a").close()
        os.utime(master_hb, None)

    def fork_child(label):
        try:
            os.remove(os.path.join(heartbeat_dir, label))
        except FileNotFoundError:
            pass
        clear_current_book(run_dir, label)  # never charge a new life for an old book
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            with warnings.catch_warnings():
                # CPython 3.12 deprecates fork() in a process that has more than one OS
                # thread.  The threads here are not ours: they belong to the loaded
                # library (pymongo's topology monitors) and to whatever native runtime
                # the import chain started — the master itself runs nothing in parallel
                # (its heartbeats are inline, and the driver's progress line lives in
                # another process).  Forking before them is not available: sharing the
                # ~2 GB loaded library copy-on-write is the entire point of the pool, so
                # the fork must come after the load.  What IS available is naming them,
                # which the inventory line above does on every run — so this filter
                # hides one already-answered message and nothing else.  It is bound to
                # that exact message text and category, and only around the fork call.
                warnings.filterwarnings(
                    "ignore",
                    message=r"This process \(pid=\d+\) is multi-threaded, use of fork\(\)",
                    category=DeprecationWarning,
                )
                pid = os.fork()
        except OSError as error:
            # EAGAIN/ENOMEM is exactly the host state this pool exists to survive: give
            # up on THIS label (the driver judges the ledger; peers keep working) rather
            # than unwinding into finally and terminating every healthy sibling.
            log(f"pool: fork for {label} failed ({error}); leaving it to its peers")
            codes[label].append(1)
            return None
        if pid == 0:
            code = 1
            try:
                global _POOL_CHILD
                _POOL_CHILD = True
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                child_setup(label)
                result = child_body()
                code = 0 if result is None else int(result)
            except SystemExit as error:
                if error.code is None:
                    code = 0
                else:
                    code = error.code if isinstance(error.code, int) else 1
            except BaseException:
                traceback.print_exc()
                code = 1
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(code)
        active[label] = (pid, time.time())
        return pid

    def signal_children(sig):
        for _label, (pid, _spawned) in list(active.items()):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def reap_nonblocking():
        # Poll only the children this pool owns: waitpid(-1) would also swallow the
        # status of any subprocess a future master might run.
        for label, (child_pid, _spawned) in list(active.items()):
            try:
                pid, status = os.waitpid(child_pid, os.WNOHANG)
            except ChildProcessError:
                del active[label]
                return label, 1, child_pid
            if pid == 0:
                continue
            del active[label]
            if os.WIFSIGNALED(status):
                return label, -os.WTERMSIG(status), child_pid
            return label, os.WEXITSTATUS(status), child_pid
        return None

    def on_term(_signum, _frame):
        nonlocal terminating
        terminating = True
        signal_children(signal.SIGTERM)

    previous = signal.signal(signal.SIGTERM, on_term)
    # Children inherit SIG_IGN until they install the governor's handler.
    previous_usr1 = signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    # State, once, what else is alive at the moment of the fork.  Two warnings in every
    # relink log are about exactly this and neither says which threads it means; the
    # inventory is what makes the next post-mortem able to answer it (see
    # fork_thread_inventory).
    log(f"pool master: {fork_thread_inventory()}")
    # Drop the garbage the library load left behind before freezing: frozen objects
    # are inherited by every child and never collected again.
    gc.collect()
    gc.freeze()
    try:
        for label in labels:
            fork_child(label)
        while active:
            touch_master()
            reaped = reap_nonblocking()
            if reaped is not None:
                label, code, pid = reaped
                killed_at = None
                if governor is not None:
                    governor.forget(pid)
                    killed_at = governor.was_killed(pid)
                if terminating:
                    codes[label].append(code)
                    continue
                if killed_at is not None:
                    # A governor kill is a recycle, not a crash.
                    try:
                        account_governor_kill(run_dir, label, killed_at, log)
                    except Exception as error:
                        log(f"pool memory governor: accounting for {label} failed: {error}")
                    fork_child(label)
                    continue
                if code == RECYCLE_EXIT_CODE:
                    log(f"pool: {label} recycled; forking a fresh child from the warm master")
                    fork_child(label)
                    continue
                codes[label].append(code)
                if code == 0:
                    continue
                if restarts[label] >= restart_limit:
                    log(
                        f"pool: {label} exited {code}; bounded replacement limit "
                        f"{restart_limit} exhausted — peers may still complete its books"
                    )
                    continue
                restarts[label] += 1
                log(
                    f"pool: {label} exited {code} before finishing; starting bounded "
                    f"replacement {restarts[label]}/{restart_limit}"
                )
                fork_child(label)
                continue
            now = time.time()
            for label, (pid, spawned) in list(active.items()):
                try:
                    last = max(spawned, os.path.getmtime(os.path.join(heartbeat_dir, label)))
                except OSError:
                    last = spawned
                if now - last > stall_seconds:
                    log(f"pool: {label} pid={pid} has no heartbeat for {now - last:.0f}s; killing it")
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if governor is not None:
                try:
                    governor.tick(active, now)
                except Exception as error:  # a governor bug must never stop the pool
                    log(f"pool memory governor: tick failed: {error}")
            time.sleep(poll_seconds)
    finally:
        signal.signal(signal.SIGTERM, previous)
        if previous_usr1 is not None:
            signal.signal(signal.SIGUSR1, previous_usr1)
        signal_children(signal.SIGTERM)
        deadline = time.time() + float(os.environ.get("LINKER_PROCESS_TERM_GRACE", "15"))
        while active and time.time() < deadline:
            if reap_nonblocking() is None:
                time.sleep(0.2)
        signal_children(signal.SIGKILL)
        while active:
            if reap_nonblocking() is None:
                time.sleep(0.2)
        try:
            os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
            with open(os.path.join(run_dir, "logs", "pool-exit-codes.json"), "w", encoding="utf-8") as fh:
                _json.dump(codes, fh, sort_keys=True)
        except OSError:
            pass
    unclean = sorted(label for label, seen in codes.items() if seen and seen[-1] != 0)
    log(f"pool: exit codes {codes}" + (f"; unclean labels {unclean}" if unclean else ""))
    return 1 if unclean else 0


def recycle_needed(current_rss: int, cap: float, processed: int) -> bool:
    """Decide whether a worker life may safely self-recycle.

    An over-cap worker that has completed no book cannot make progress by execing
    itself again.  Treat that as a configuration/infrastructure failure instead of
    creating a hot zero-progress restart loop until the outer job timeout.
    """
    if current_rss <= cap:
        return False
    if processed <= 0:
        raise RuntimeError(
            f"worker RSS {current_rss} exceeds cap {int(cap)} before completing any book; "
            "refusing a zero-progress recycle loop"
        )
    return True


RECYCLE_PROGRESS_FILE = "recycle-progress.json"


def committed_shard_count(checkpoint_dir: str) -> int:
    """How many batches of one book are committed on disk right now."""
    try:
        return sum(1 for name in os.listdir(checkpoint_dir) if name.endswith(".jsonl")
                   and name[0].isdigit())
    except FileNotFoundError:
        return 0


def refuse_a_book_that_recycles_without_progress(checkpoint_dir: str) -> None:
    """Fail a book that would recycle a second time having committed nothing new.

    A recycle is only worth its cost if the next life starts further along.  A book
    that replays the same batches every life (2026-09: a stateful-ibid book deleted
    its own shards, so relinks 35458432836 and 35477368314 span three workers for
    hours at 100% CPU and never finished) must therefore end as a loud failure, not
    as a job that burns its whole 48-hour ceiling.  recycle_needed() cannot see this:
    a life that commits 27 batches and loses them looks identical to one that keeps
    them, so the comparison has to survive the exec - hence a file, not a counter.
    """
    import json as _json

    marker = os.path.join(checkpoint_dir, RECYCLE_PROGRESS_FILE)
    committed = committed_shard_count(checkpoint_dir)
    previous = None
    try:
        with open(marker, encoding="utf-8") as fh:
            previous = _json.load(fh).get("committed_shards")
    except Exception:
        previous = None
    if isinstance(previous, int) and committed <= previous:
        raise RuntimeError(
            f"book is recycling without progress: {committed} committed batch(es) now, "
            f"{previous} before the previous recycle; refusing an endless replay"
        )
    tmp = f"{marker}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump({"committed_shards": committed}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, marker)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


def claim_id(bk: BookKey) -> str:
    """Filesystem-safe, collision-free handle for a book (claim/done markers)."""
    h = hashlib.sha1(f"{bk.source_name}\0{bk.canonical_he_title}".encode("utf-8"))
    return h.hexdigest()


CLAIM_EVENT_DIR = "claim-events"


def record_claim_event(run_dir: str, kind: str, cid: str) -> None:
    """Note one EXCEPTIONAL claim transition so the driver can summarise the run.

    Only the rare paths write here: a claim taken over from an owner that died, and a
    second completion of a book (which the exclusive claim makes impossible — the
    marker exists so a regression shows up as a number in the summary line instead of
    as a silently absent invariant).  Best effort: failing to journal must never fail
    a book, and these markers are diagnostics, never a gate.
    """
    try:
        directory = os.path.join(run_dir, CLAIM_EVENT_DIR)
        os.makedirs(directory, exist_ok=True)
        open(os.path.join(directory, f"{kind}-{cid}"), "w").close()
    except OSError:
        pass


def count_claim_events(run_dir: str, kind: str) -> int:
    """How many distinct books recorded ``kind`` (see record_claim_event)."""
    try:
        names = os.listdir(os.path.join(run_dir, CLAIM_EVENT_DIR))
    except OSError:
        return 0
    return sum(1 for name in names if name.startswith(f"{kind}-"))


def mark_done(run_dir: str, cid: str) -> bool:
    """Publish the book's `done` marker; False if one already existed.

    Exactly the file the checkpoint saver, the driver's completeness assertion and
    BookClaim.acquire read — `<run>/done/<claim id>`, an empty regular file — with
    only the CREATION made exclusive, so a second completion of the same book is
    counted rather than silently overwriting the first.
    """
    path = os.path.join(run_dir, "done", cid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        record_claim_event(run_dir, "duplicate", cid)
        return False
    return True


class BookClaim:
    """Exclusive, crash-safe ownership of one book in an engine run.

    The old directory/heartbeat protocol used the heartbeat as both liveness and
    ownership.  A slow but healthy resolver could therefore be declared stale;
    two workers then wrote the same checkpoint directory and one could delete a
    shard while the other was still reading it.  The marker remains useful for
    recovering after a *dead* process, but an advisory lock is now the authority
    for a live owner.  ``flock`` is released by the kernel if that owner dies,
    making a stale takeover safe without allowing concurrent resolvers.
    """

    def __init__(self, claim_path: str, lock_fd: int):
        self.claim_path = claim_path
        self.lock_fd = lock_fd
        self.released = False

    @staticmethod
    def acquire(run_dir: str, cid: str, stale_seconds: int = CLAIM_STALE_SEC):
        """Return the sole live claim for ``cid``, or ``None`` if unavailable."""
        import fcntl
        import shutil

        claims_root = os.path.join(run_dir, "claim")
        os.makedirs(claims_root, exist_ok=True)
        claim_path = os.path.join(claims_root, cid)
        # Keep the lock file outside the mutable claim directory: reclaiming the
        # directory must never drop the kernel lock that serializes reclaimers.
        lock_path = os.path.join(claims_root, f".{cid}.lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            return None

        def abandon_lock():
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        if os.path.exists(os.path.join(run_dir, "done", cid)):
            abandon_lock()
            return None

        heartbeat_path = os.path.join(claim_path, "hb")
        try:
            os.mkdir(claim_path)
        except FileExistsError:
            # Reaching this branch while holding the advisory lock proves that no
            # live worker owns the claim, and that the previous owner never reached
            # release() — i.e. it died holding the book.  Waiting for the old
            # heartbeat to age out used to strand a crashed worker's book for up to
            # 15 minutes even though the kernel had already released its ownership
            # lock.  Retire only the mutable claim directory immediately; immutable
            # batch checkpoints live elsewhere and are reused by this worker.
            retired = f"{claim_path}.abandoned-{uuid.uuid4().hex}"
            try:
                os.replace(claim_path, retired)
            except FileNotFoundError:
                # EAFP: nothing left to retire.  Only the lock holder may remove the
                # claim directory, so this is a torn release rather than a peer — but
                # an exists()/replace() pair would be exactly the two-syscall window
                # this task exists to close.
                pass
            else:
                shutil.rmtree(retired, ignore_errors=True)
            os.mkdir(claim_path)
            # Journalled only once the takeover actually succeeded, so the closing
            # summary counts books recovered rather than takeovers attempted.
            record_claim_event(run_dir, "recovered", cid)

        open(heartbeat_path, "w").close()
        return BookClaim(claim_path, lock_fd)

    @staticmethod
    def is_claimed(run_dir: str, cid: str) -> bool:
        """True only while a LIVE worker owns ``cid``.

        Probes the ownership lock with a SHARED, non-blocking flock.  Shared probes do
        not exclude each other, so twelve rescanning workers cannot hide an available
        book from one another, while a live owner's exclusive lock does refuse them.
        The kernel drops a dead owner's lock, so a crashed worker's book reports
        unclaimed and the next acquire() takes it over — the book is retried, never
        stranded, which is the whole point of never dropping a heavy book.

        Advisory only: acquire() remains the sole authority on ownership.  A false
        "claimed" costs one rescan round; a false "free" costs one refused acquire.
        """
        import fcntl

        lock_path = os.path.join(run_dir, "claim", f".{cid}.lock")
        try:
            # No O_CREAT: probing must not litter the claim root for books this run
            # never reaches, and a missing lock file simply means "never claimed".
            fd = os.open(lock_path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def heartbeat(self) -> None:
        """Refresh liveness without ever recreating a claim we no longer own."""
        if self.released:
            raise RuntimeError("attempted to heartbeat a released book claim")
        try:
            os.utime(os.path.join(self.claim_path, "hb"), None)
        except FileNotFoundError as error:
            raise RuntimeError("book claim disappeared while its owner was live") from error

    def release(self) -> None:
        """Drop ownership and make the book immediately available to a peer."""
        if self.released:
            return
        import fcntl
        import shutil
        try:
            shutil.rmtree(self.claim_path, ignore_errors=True)
        finally:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.released = True


class BatchClaim:
    """Kernel-owned non-blocking claim for one immutable resolver batch."""

    def __init__(self, fd):
        self.fd = fd

    @classmethod
    def acquire(cls, checkpoint_dir: str, batch_start: int):
        import fcntl

        lock_dir = os.path.join(checkpoint_dir, ".batch-locks")
        os.makedirs(lock_dir, exist_ok=True)
        path = os.path.join(lock_dir, f"{batch_start:012d}.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return cls(fd)

    def release(self):
        import fcntl

        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


class BookDeferred(Exception):
    """Raised between batches when the current book grew this worker past
    HEAVY_BOOK_GROWTH_BYTES; the caller marks it heavy and recycles."""

    def __init__(self, bk, growth_bytes, batches):
        super().__init__(f"{bk.source_name}/{bk.canonical_he_title} grew this worker by "
                         f"{growth_bytes} bytes over {batches} batch(es)")
        self.book_key = bk
        self.growth_bytes = growth_bytes
        self.batches = batches


def _memory_exception(error: BaseException):
    """The MemoryError (or governor abort) in ``error``'s cause chain, or None."""
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        if isinstance(current, (MemoryError, HostMemoryPressure)):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def is_memory_error(error: BaseException) -> bool:
    """True if ``error`` is, or was caused by, a MemoryError or a governor abort."""
    return _memory_exception(error) is not None


def memory_error_site(error: BaseException, depth: int = 5) -> list:
    """Innermost ``dir/file.py:function:line`` frames of the memory exception."""
    import traceback
    try:  # runs right after a MemoryError: never let it raise a second one
        memory = _memory_exception(error)
        if memory is None or memory.__traceback__ is None:
            return []
        # No source lookup; the site is the interrupted frame, not the signal handler.
        frames = [(frame.f_code, lineno) for frame, lineno in traceback.walk_tb(memory.__traceback__)
                  if frame.f_code.co_name != "_on_memory_pressure"][-max(1, int(depth)):]
        site = []
        for code, lineno in frames:
            parts = code.co_filename.replace("\\", "/").split("/")
            site.append(f"{'/'.join(parts[-2:])}:{code.co_name}:{lineno}")
        return site
    except BaseException:
        return []


def virtual_bytes() -> int:
    """This process's virtual size (what RLIMIT_AS is judged against); 0 if unknown."""
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            return int(fh.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        return 0


def apply_address_space_limit(limit_bytes: int, log=None) -> int:
    """Set this process's RLIMIT_AS soft limit (never above the hard limit).

    RLIMIT_AS bounds VIRTUAL size, and a loaded Sefaria image already maps several GB
    before the first book.  A ceiling the baseline crowds (less than half of it left)
    would turn every book into a MemoryError - deferred, then failed - and waste the
    whole relink; refuse it loudly instead, leaving the current limit in place.
    Returns the soft limit actually applied, or 0 when disabled/unsupported/refused."""
    if not limit_bytes or limit_bytes <= 0 or not hasattr(resource, "RLIMIT_AS"):
        return 0
    baseline = virtual_bytes()
    if baseline * 2 > limit_bytes:
        if log is not None:
            log(
                f"address-space limit {int(limit_bytes)} refused: baseline virtual size "
                f"{baseline} leaves less than half of it for book growth"
            )
        return 0
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    wanted = int(limit_bytes)
    if hard != resource.RLIM_INFINITY and wanted > hard:
        wanted = hard
    try:
        resource.setrlimit(resource.RLIMIT_AS, (wanted, hard))
    except (ValueError, OSError):
        return 0
    return wanted


def heavy_marker_path(run_dir: str, cid: str) -> str:
    return os.path.join(run_dir, "heavy", cid)


def mark_heavy(run_dir: str, cid: str, bk: BookKey, growth_bytes: int, reason: str) -> None:
    """Persist the deferral decision for every worker (and every later life) to see."""
    import json as _json
    os.makedirs(os.path.join(run_dir, "heavy"), exist_ok=True)
    path = heavy_marker_path(run_dir, cid)
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as fh:
        _json.dump({**bk.to_dict(), "growth_bytes": int(growth_bytes), "reason": reason,
                    "unix": time.time()}, fh, ensure_ascii=False)
    os.replace(temporary, path)


def gate_heavy_book(run_dir: str, cid: str, heavy_phase: bool) -> str:
    """Decide how a worker may touch a book right now.

    Returns "normal" (no marker), "skip" (marked heavy but ordinary books are still
    pending somewhere - a peer may have marked it a moment ago; it must wait for the
    heavy phase, or two slot-holders at 8 GB each would run beside ten ordinary
    workers), or "heavy" (marked and the heavy phase has begun: take a slot).
    """
    if not os.path.exists(heavy_marker_path(run_dir, cid)):
        return "normal"
    return "heavy" if heavy_phase else "skip"


# ── memory events: fresh workers for heavy books, one requeue after MemoryError ──
# Everything under <run>/memory is THIS invocation's bookkeeping: the driver wipes it
# with the other ledgers, and the checkpoint saver's allow-list has no member that
# could match it, so a requeue can never be carried into another run - let alone
# adopted there as a completed book.
MEMORY_DIR = "memory"
MEMORY_JOURNAL = "journal.jsonl"
REQUEUE_MARKER_DIR = "requeued-after-memoryerror"


def worker_is_fresh_for_heavy(processed: int, growth_bytes: int) -> bool:
    """May this worker life take a deferred (heavy) book?

    Fresh = it has completed no book yet, OR it owns less than
    HEAVY_FRESH_GROWTH_BYTES of private memory above its life baseline.  The first
    disjunct is what makes the rule terminate: a freshly forked/exec'd image has
    ``processed == 0``, so it is fresh by definition and cannot be sent round again,
    whatever a memory reading says.  The second spares a fork for a life that
    happens to have linked a few tiny books without growing.
    """
    if processed <= 0:
        return True
    return growth_bytes <= HEAVY_FRESH_GROWTH_BYTES


def format_bytes_gb(value) -> str:
    """11888857088 -> '11.9 GB'.  Growth figures are read at a glance, never summed."""
    return f"{float(value) / 1e9:.1f} GB"


_MEMORY_LINE_RE = re.compile(r"\bline (\d+) failed to link\b")


def memory_error_line_number(message: str):
    """The line a wrapped MemoryError names, or None.

    On the raw-NER path this is the batch's FIRST line, not the offending row.
    """
    found = _MEMORY_LINE_RE.search(message or "")
    return int(found.group(1)) if found else None


def memory_journal_path(run_dir: str) -> str:
    return os.path.join(run_dir, MEMORY_DIR, MEMORY_JOURNAL)


def journal_memory_event(run_dir: str, event: dict) -> None:
    """Append one memory event (best effort; a journal must never fail a book).

    One short O_APPEND write per event is atomic against the other eleven workers,
    so the file stays parseable without a lock.  Events, not state: the requeue
    marker below is the authority on whether a book's single retry was spent.
    """
    import json as _json
    try:
        directory = os.path.join(run_dir, MEMORY_DIR)
        os.makedirs(directory, exist_ok=True)
        payload = _json.dumps(
            {"unix": time.time(), **event}, ensure_ascii=False, sort_keys=True
        ) + "\n"
        fd = os.open(
            os.path.join(directory, MEMORY_JOURNAL),
            os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644,
        )
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        pass


def read_memory_journal(run_dir: str) -> list:
    """Every journalled memory event, oldest first; a torn final line is ignored."""
    import json as _json
    events = []
    try:
        with open(memory_journal_path(run_dir), encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = _json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
    except OSError:
        return []
    return events


def count_memory_events(run_dir: str, event: str) -> int:
    """How many events of one kind this run journalled (recycles are per event)."""
    return sum(1 for item in read_memory_journal(run_dir) if item.get("event") == event)


def requeue_marker_path(run_dir: str, cid: str, attempt: int = 1) -> str:
    """Marker of a book's ``attempt``-th memory retry (the first keeps its old name)."""
    name = cid if attempt <= 1 else f"{cid}.{int(attempt)}"
    return os.path.join(run_dir, MEMORY_DIR, REQUEUE_MARKER_DIR, name)


def read_memory_retries(run_dir: str, cid: str) -> list:
    """Every retry this book was granted, oldest first; ``{}`` for a torn marker."""
    import json as _json
    records = []
    attempt = 1
    while True:
        path = requeue_marker_path(run_dir, cid, attempt)
        if not os.path.exists(path):
            return records
        try:
            with open(path, encoding="utf-8") as stream:
                stored = _json.load(stream)
        except (OSError, ValueError):
            stored = {}
        records.append(stored if isinstance(stored, dict) else {})
        attempt += 1


def _made_progress(before: dict, after: dict) -> bool:
    earlier, later = before.get("committed"), after.get("committed")
    return (type(earlier) is int and type(later) is int and later > earlier)


def _standstill(before: dict, after: dict) -> bool:
    """No new batch, by the book's own doing (a governor stop is outside pressure)."""
    return not after.get("governor") and not _made_progress(before, after)


def claim_memory_requeue(run_dir: str, cid: str, record: dict):
    """(granted, earlier retries) for a heavy-phase memory failure.

    Granted while ``record["committed"]`` rises; one standstill is forgiven, a second
    in a row is not. O_EXCL numbered markers; a torn marker fails the book.
    """
    import json as _json
    earlier = read_memory_retries(run_dir, cid)
    if earlier:
        if len(earlier) >= MEMORY_RETRY_LIMIT:
            return False, earlier
        if not earlier[-1]:
            return False, earlier  # torn: never grant on a record we cannot read
        if _standstill(earlier[-1], record):
            standstill_before = len(earlier) >= 2 and _standstill(earlier[-2], earlier[-1])
            if standstill_before or "committed" not in earlier[-1]:
                return False, earlier
    path = requeue_marker_path(run_dir, cid, len(earlier) + 1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Written aside, then linked: exclusive like O_EXCL, and never torn by a kill.
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as stream:
        _json.dump(record, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
    try:
        os.link(temporary, path)
    except FileExistsError:
        return False, read_memory_retries(run_dir, cid)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass
    return True, earlier


def current_memory_limits(heavy: bool) -> dict:
    """The ceilings actually in force in THIS worker, for the failure message."""
    address_space = 0
    if hasattr(resource, "RLIMIT_AS"):
        soft = resource.getrlimit(resource.RLIMIT_AS)[0]
        address_space = 0 if soft == resource.RLIM_INFINITY else int(soft)
    return {
        "address_space_bytes": address_space,
        "recycle_cap_bytes": int(HEAVY_RSS_CAP if heavy else RSS_CAP),
        "heavy_growth_bytes": int(HEAVY_BOOK_GROWTH_BYTES),
    }


def _memory_attempt_phrase(record: dict) -> str:
    """One attempt for the log: growth this life, batch, worker, progress, site."""
    worker = record.get("worker") or "an unknown worker"
    if "growth_bytes" not in record:
        return f"figures unavailable (journal record incomplete) on {worker}"
    line_number = record.get("line")
    where = "" if line_number is None else f" in the batch from line {line_number}"
    processed = record.get("processed")
    after = "" if processed is None else \
        f" after {processed} book{'' if processed == 1 else 's'}"
    committed = record.get("committed")
    shards = "" if type(committed) is not int else f", {committed} batch(es) committed"
    site = record.get("site") or []
    at = "" if not site else f", at {site[-1]}"
    size = format_bytes_gb(record["growth_bytes"])
    what = (f"killed holding {size}" if record.get("error") == GOVERNOR_KILL_ERROR
            else f"grew {size} this life")
    return f"{what}{where} on {worker}{after}{shards}{at}"


def format_memory_requeue_line(*, bk, growth_bytes, worker, processed, line_number=None,
                               retry=1, committed=None, site=None) -> str:
    """The one line an operator needs when a book is given another attempt."""
    return "memory: requeued {}/{!r} after MemoryError, retry {} ({}; retrying on a fresh worker)".format(
        bk.source_name, bk.canonical_he_title, int(retry),
        _memory_attempt_phrase({
            "growth_bytes": growth_bytes, "worker": worker, "processed": processed,
            "line": line_number, "committed": committed, "site": site or [],
        }),
    )


def format_memory_failure(*, bk, earlier: list, last: dict, limits: dict) -> str:
    """One line: the book, every attempt and the live ceilings."""
    attempts = list(earlier) + [last]
    listed = "; ".join(
        f"attempt {index} {_memory_attempt_phrase(item)}"
        for index, item in enumerate(attempts, start=1)
    )
    return (
        "{}/{!r}: MemoryError on {} attempt(s), stopped because it no longer made "
        "progress or reached the retry limit {} ({}); limits: address space {}, "
        "recycle cap {}, heavy deferral threshold {}".format(
            bk.source_name, bk.canonical_he_title, len(attempts), MEMORY_RETRY_LIMIT,
            listed,
            limits.get("address_space_bytes", 0) or "unlimited",
            limits.get("recycle_cap_bytes", 0),
            limits.get("heavy_growth_bytes", 0),
        )
    )


def write_failed_book(run_dir: str, cid: str, bk: BookKey, note=None, mark_done_too=True) -> None:
    """`failed` (the driver will not advance) plus `done` (no retry in this run)."""
    import json as _json
    os.makedirs(os.path.join(run_dir, "failed"), exist_ok=True)
    with open(os.path.join(run_dir, "failed", cid), "w", encoding="utf-8") as ff:
        _json.dump({**bk.to_dict(), **({"note": note} if note else {})}, ff, ensure_ascii=False)
    if mark_done_too:
        mark_done(run_dir, cid)


def account_governor_kill(run_dir: str, label: str, private_bytes_at_kill: int, log) -> None:
    """Account a killed worker's book as the worker would have (defer/retry/fail),
    so a kill can never become an endless retry."""
    current = read_current_book(run_dir, label)
    clear_current_book(run_dir, label)
    base = {"event": "killed-by-memory-governor", "worker": label,
            "growth_bytes": int(private_bytes_at_kill)}
    if current is None:
        journal_memory_event(run_dir, base)
        return
    cid = current["claim_id"]
    bk = BookKey(current["source_name"], current["canonical_he_title"])
    journal_memory_event(run_dir, {**base, "claim_id": cid,
                                   "source_name": bk.source_name,
                                   "canonical_he_title": bk.canonical_he_title})
    import fcntl
    lock_path = os.path.join(run_dir, "claim", f".{cid}.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            peer_holds = False
        except BlockingIOError:
            peer_holds = True  # already resuming: charge it all the same, never mark done
        _account_killed_book(run_dir, label, cid, bk, current, private_bytes_at_kill, log,
                             peer_holds)
    finally:
        os.close(lock_fd)


def _account_killed_book(run_dir, label, cid, bk, current, private_bytes_at_kill, log,
                         peer_holds=False):
    if os.path.exists(os.path.join(run_dir, "done", cid)):
        return
    heavy = bool(current.get("heavy"))
    attempt = {
        "claim_id": cid, "source_name": bk.source_name,
        "canonical_he_title": bk.canonical_he_title, "worker": label,
        "processed": current.get("processed"), "growth_bytes": int(private_bytes_at_kill),
        "line": None, "phase": "heavy" if heavy else "normal",
        "error": GOVERNOR_KILL_ERROR, "governor": not current.get("exclusive"),
        "committed": committed_shard_count(os.path.join(run_dir, "checkpoints", cid)),
        "limits": current.get("limits") or {},
    }
    if not heavy:
        journal_memory_event(run_dir, {"event": "deferred-after-memoryerror", **attempt})
        mark_heavy(run_dir, cid, bk, attempt["growth_bytes"], "killed by the pool memory governor")
        log(f"pool memory governor: deferred {bk.source_name}/{bk.canonical_he_title!r} "
            "to the heavy phase")
        return
    enter_single_heavy_mode(run_dir, cid, log)
    won, earlier = claim_memory_requeue(run_dir, cid, attempt)
    if won:
        journal_memory_event(run_dir, {"event": "requeued-after-memoryerror", **attempt})
        log(format_memory_requeue_line(
            bk=bk, growth_bytes=attempt["growth_bytes"], worker=label,
            processed=attempt["processed"], retry=len(earlier) + 1,
            committed=attempt["committed"],
        ))
        return
    journal_memory_event(run_dir, {"event": "failed-after-requeue", **attempt})
    note = format_memory_failure(bk=bk, earlier=earlier, last=attempt, limits=attempt["limits"])
    write_failed_book(run_dir, cid, bk, note, mark_done_too=not peer_holds)
    log(f"ERROR {bk.canonical_he_title!r}: {note}")


def partition_pending(books, run_dir: str):
    """Split undone books into (normal, heavy) preserving order."""
    normal, heavy = [], []
    for bk in books:
        cid = claim_id(bk)
        if os.path.exists(os.path.join(run_dir, "done", cid)):
            continue
        (heavy if os.path.exists(heavy_marker_path(run_dir, cid)) else normal).append(bk)
    return normal, heavy


def unclaimed(books, run_dir: str):
    """Of ``books``, those no live worker owns right now — the rescan's hand-out set.

    Every worker used to walk the same ordered pending list and start anything that
    merely lacked a `done` marker, so a book already being resolved was resolved again
    by each worker that arrived.  In relink 34016397157 that produced 2,148 redundant
    completions out of 7,274 (29.5%), 1,107 books completed 2-10 times each, and
    268,047 book-lines re-read from the snapshot for nothing; the resulting pile-up on
    the same artifact is also what raced `os.remove(out_path)`.

    The filter is advisory and deliberately snapshot-in-time: BookClaim.acquire still
    decides ownership, and a book whose owner dies reappears here on the next pass
    because the kernel released that owner's lock.  Books are therefore skipped for a
    round, never removed from the run — the caller's loop keeps going until every book
    carries a `done` marker.
    """
    return [bk for bk in books if not BookClaim.is_claimed(run_dir, claim_id(bk))]


class HeavySlot:
    """One of HEAVY_BOOK_SLOTS kernel-locked slots; held while a heavy book resolves.

    flock is released by the kernel on any death, so a slot can never leak."""

    def __init__(self, fd: int, path: str, extra_fds=()):
        self.fd = fd
        self.path = path
        self.extra_fds = list(extra_fds)

    @classmethod
    def acquire(cls, run_dir: str, slots: int):
        import fcntl
        root = os.path.join(run_dir, "heavy-slots")
        os.makedirs(root, exist_ok=True)
        for index in range(slots):
            path = os.path.join(root, f"{index:02d}.lock")
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            return cls(fd, path)
        return None

    @classmethod
    def acquire_all(cls, run_dir: str, slots: int):
        """Every slot or none: this book resolves with no other heavy book beside it."""
        import fcntl
        root = os.path.join(run_dir, "heavy-slots")
        os.makedirs(root, exist_ok=True)
        held = []
        for index in range(slots):
            path = os.path.join(root, f"{index:02d}.lock")
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                for other in held:
                    os.close(other)
                return None
            held.append(fd)
        return cls(held[0], os.path.join(root, "00.lock"), held[1:])

    def release(self) -> None:
        for fd in self.extra_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.extra_fds = []
        if self.fd is not None:
            try:
                os.close(self.fd)   # closing drops the flock
            finally:
                self.fd = None


class BookWorkInProgress(Exception):
    """Another worker owns at least one missing batch; rescan without failing."""


def capture_prior_artifact(checkpoint_dir, out_path, lock_path):
    """Snapshot the old artifact once before cooperative assemblers can replace it."""
    import fcntl
    import shutil

    os.makedirs(checkpoint_dir, exist_ok=True)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    prior = os.path.join(checkpoint_dir, "prior.jsonl")
    absent = os.path.join(checkpoint_dir, "prior.absent")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if not os.path.exists(prior) and not os.path.exists(absent):
            if os.path.isfile(out_path):
                # Keep an interrupted copy beside the run-scoped lock rather than
                # inside the durable checkpoint allow-list. A killed worker may
                # leave this file behind, but checkpoint save deliberately excludes
                # claim state and another worker can safely replace it.
                temporary = f"{lock_path}.{os.getpid()}.tmp"
                shutil.copy2(out_path, temporary)
                os.replace(temporary, prior)
            else:
                with open(absent, "xb") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
        return prior if os.path.isfile(prior) else None
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def ner_alive() -> bool:
    import requests
    try:
        # raise_for_status: an HTTP 500 means the service is NOT healthy — without it
        # a broken NER reads as "alive" and the failure gets misattributed to the book.
        requests.post(NER_URL, json={"text": "בדיקה", "lang": "he"}, timeout=60).raise_for_status()
        return True
    except Exception:
        return False


def wait_for_ner(log) -> None:
    # Bounded: an NER that stays dead is broken infrastructure (e.g. a CUDA OOM
    # crash-loop on a GPU runner), not a blip. Waiting forever burned a whole Kaggle
    # session in silence once — fail loudly instead and let the driver report it.
    waited = 0
    while not ner_alive():
        if waited >= NER_MAX_WAIT_SEC:
            raise RuntimeError(f"NER still dead after {waited}s — giving up (broken NER service)")
        log("NER unreachable; waiting 15s")
        time.sleep(15)
        waited += 15


def all_book_keys(con) -> list[BookKey]:
    rows = con.execute(
        "SELECT DISTINCT source_name, canonical_he_title FROM lines_snapshot "
        "ORDER BY source_name, canonical_he_title"
    ).fetchall()
    return [BookKey(s, t) for s, t in rows]


def book_lines(con, bk: BookKey) -> list[tuple[int, str, str]]:
    return con.execute(
        "SELECT line_index, content, context_ref FROM lines_snapshot "
        "WHERE source_name=? AND canonical_he_title=? ORDER BY line_index",
        (bk.source_name, bk.canonical_he_title),
    ).fetchall()


def largest_books_first(books, eligible_line_counts):
    """Deterministically start expensive CPU books early without rescanning SQLite."""
    wanted = {(book.source_name, book.canonical_he_title) for book in books}
    if set(eligible_line_counts) != wanted or any(
        type(value) is not int or value < 0 for value in eligible_line_counts.values()
    ):
        raise RuntimeError("could not size every requested resolver book")
    return sorted(
        books,
        key=lambda book: (
            -eligible_line_counts[(book.source_name, book.canonical_he_title)],
            book.source_name,
            book.canonical_he_title,
        ),
    )


def disambiguate_bavli(cands):
    """Opt-in disambiguation for the single corpus-documented decidable case.

    Returns a single Ref if the candidates differ ONLY on the Bavli/Yerushalmi or
    Mishnah/Gemara axis (prefer Bavli / Mishnah); otherwise None (drop). Conservative
    by design: when unsure it drops, so a bad link is never emitted — only a missing one.
    """
    if not cands:
        return None
    cats = {c.index.get_primary_category() for c in cands}
    # Bavli vs Yerushalmi: both are "Talmud"; distinguish by title prefix.
    titles = {c.index.title for c in cands}
    bavli = [c for c in cands if not c.index.title.startswith("Jerusalem Talmud")]
    yeru = [c for c in cands if c.index.title.startswith("Jerusalem Talmud")]
    if bavli and yeru and len(bavli) == 1 and _same_location(bavli[0], yeru):
        return bavli[0]
    # Mishnah vs Gemara: prefer the Mishnah reading.
    mishnah = [c for c in cands if c.index.get_primary_category() == "Mishnah"]
    talmud = [c for c in cands if c.index.get_primary_category() == "Talmud"]
    if mishnah and talmud and len(mishnah) == 1:
        return mishnah[0]
    return None


def _same_location(one, others) -> bool:
    # crude: the same masechta name after stripping the Jerusalem prefix
    base = one.index.title
    return any(o.index.title.replace("Jerusalem Talmud ", "") == base for o in others)


class IbidContext:
    """Preserve Sefaria's ibid history across transport batches of one book.

    ``Linker.bulk_link`` resets its resolver at every call, whereas the inputs in a
    book are consecutive lines.  We skip only that *first* reset on later calls;
    resets caused by a failed citation inside Sefaria still run normally.  This is
    deliberately pinned-private API use: fail loudly if the pinned linker no longer
    exposes the contract rather than silently making output batch-size dependent.
    """

    IBID_STATE_SCHEMA = 4

    def __init__(self):
        self.initialized = False
        # These are Sefaria Ref objects, not serialized data: the state lives only
        # within this worker and has at most the resolver's three remembered refs.
        self.resolved_refs: tuple[object, ...] = ()
        self.last_emitted_ref = None
        # The immediately preceding citation, including a text-rebuilt target.
        # It is separate from Sefaria's history: recovery never rewrites that history.
        self.previous_citation = None

    def state(self, book_key, batch_start: int, batch, shard_sha256: str) -> dict:
        """The whole cross-batch state, as normalized ref strings.

        A batch's output depends on this and nothing else, so writing it beside the
        shard makes a recycled worker resume with byte-identical history instead of
        replaying the book from its first batch.  It carries the identity of the
        exact shard it followed, because history adopted for the wrong batch would
        change the output silently - the one failure this checkpoint must not have.
        """
        return {
            "schema": self.IBID_STATE_SCHEMA,
            "book": [book_key.source_name, book_key.canonical_he_title],
            "batch_start": int(batch_start),
            # The start alone does not say which lines the batch held: a batch whose
            # last lines yield no link writes the same shard bytes as a shorter one
            # starting there, and adopting its state would carry lines into the next
            # batch's history that it never saw.
            "batch_extent": [len(batch), int(batch[-1][0])],
            "shard_sha256": shard_sha256,
            "resolved_refs": [ref.normal() for ref in self.resolved_refs],
            "last_emitted_ref": (
                None if self.last_emitted_ref is None else self.last_emitted_ref.normal()
            ),
            "previous_citation": None if self.previous_citation is None else {
                "line": self.previous_citation[0], "end": self.previous_citation[1],
                "failed": self.previous_citation[2],
                "refs": [ref.normal() for ref in self.previous_citation[3]],
                "trusted_ref": (None if self.previous_citation[4] is None
                                else self.previous_citation[4].normal()),
            },
        }

    def restore_state(self, state, ref_factory, book_key, batch_start, batch, shard_sha256) -> None:
        """Adopt a state() written for this exact shard; raise if it is unusable.

        Refusing loudly is the point: the caller then recomputes that batch (and
        every later one) rather than continuing with a silently different history.
        Nothing is assigned until every field has been rebuilt, so a rejected state
        can never leave half of one batch's history next to half of another's.
        """
        if ref_factory is None:
            raise RuntimeError("ibid history requires a Ref factory")
        if not isinstance(state, dict) or state.get("schema") != self.IBID_STATE_SCHEMA:
            raise RuntimeError("unrecognized ibid checkpoint state")
        if "previous_citation" not in state:
            raise RuntimeError("missing preceding citation state")
        if state.get("book") != [book_key.source_name, book_key.canonical_he_title]:
            raise RuntimeError("ibid checkpoint state belongs to a different book")
        if state.get("batch_start") != int(batch_start):
            raise RuntimeError("ibid checkpoint state belongs to a different batch")
        if state.get("batch_extent") != [len(batch), int(batch[-1][0])]:
            raise RuntimeError("ibid checkpoint state covers a different span of lines")
        if state.get("shard_sha256") != shard_sha256:
            raise RuntimeError("ibid checkpoint state does not match its shard")
        refs = state.get("resolved_refs")
        last = state.get("last_emitted_ref")
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise RuntimeError("malformed ibid checkpoint refs")
        if last is not None and not isinstance(last, str):
            raise RuntimeError("malformed ibid checkpoint antecedent")
        resolved = tuple(ref_factory(normal) for normal in refs)
        emitted = None if last is None else ref_factory(last)
        previous = state.get("previous_citation")
        if previous is not None:
            if (not isinstance(previous, dict)
                    or set(previous) != {"line", "end", "failed", "refs", "trusted_ref"}
                    or type(previous["line"]) is not int or previous["line"] < 0
                    or type(previous["end"]) is not int or previous["end"] < 0
                    or type(previous["failed"]) is not bool
                    or not isinstance(previous["refs"], list)
                    or not all(isinstance(r, str) for r in previous["refs"])
                    or (previous["trusted_ref"] is not None
                        and not isinstance(previous["trusted_ref"], str))):
                raise RuntimeError("malformed preceding citation state")
            previous = (previous["line"], previous["end"], previous["failed"],
                        tuple(ref_factory(r) for r in previous["refs"]),
                        None if previous["trusted_ref"] is None else ref_factory(previous["trusted_ref"]))
        self.resolved_refs = resolved
        self.last_emitted_ref = emitted
        self.previous_citation = previous
        self.initialized = True

    @staticmethod
    def _resolver(linker):
        resolver = getattr(linker, "_ref_resolver", None)
        history = getattr(resolver, "_ibid_history", None)
        if resolver is None or history is None or not callable(
            getattr(resolver, "reset_ibid_history", None)
        ):
            raise RuntimeError(
                "pinned Sefaria linker does not expose the ibid-history contract"
            )
        return resolver

    def _restore(self, resolver):
        resolver.reset_ibid_history()
        for ref in self.resolved_refs:
            resolver._ibid_history.last_refs = ref

    def resolve(self, linker, invoke):
        """Run one bulk call while retaining only its initial ibid history reset."""
        resolver = self._resolver(linker)
        original_reset = resolver.reset_ibid_history
        if self.initialized:
            self._restore(resolver)
            skipped_initial_reset = False

            def reset_ibid_history():
                nonlocal skipped_initial_reset
                if not skipped_initial_reset:
                    skipped_initial_reset = True
                    return
                original_reset()

            resolver.reset_ibid_history = reset_ibid_history
        try:
            docs = invoke()
        finally:
            resolver.reset_ibid_history = original_reset
        self.initialized = True
        self.resolved_refs = tuple(resolver._ibid_history.last_refs)
        return docs


def process_book(
    linker, bk, lines, skipped_log, heartbeat, precomputed=None,
    context_ref_factory=None,
):
    """Link one book's lines into a list of LinkRecord (unambiguous only).

    Calls heartbeat() once per batch so a long book keeps its claim fresh and is
    never falsely stolen by another worker mid-processing.
    """
    records: list[LinkRecord] = []
    words = 0
    ibid_context = IbidContext() if requires_book_context(lines) else None
    positions = talmud_positions(bk, lines, context_ref_factory)
    for batch_start, batch in transport_batches(lines):
        heartbeat()
        batch_records, batch_words = process_batch(
            linker, bk, batch, skipped_log, batch_start=batch_start, precomputed=precomputed,
            context_ref_factory=context_ref_factory, ibid_context=ibid_context,
            talmud_positions=positions,
        )
        records.extend(batch_records)
        words += batch_words
    return records, words


def process_batch(
    linker, bk, batch, skipped_log, *, batch_start=0, precomputed=None,
    context_ref_factory=None, metrics=None, ibid_context=None, talmud_positions=None,
):
    """Link one transport batch, optionally continuing a book's ibid history.

    ``talmud_positions`` (from :func:`talmud_positions`) places lines whose own
    context Sefaria cannot parse, for relative-citation recovery only.
    """
    started = time.perf_counter()
    words = sum(len(content.split()) for _, content, _ in batch)
    context_objects = []
    for _line_index, _content, context_ref in batch:
        try:
            context_objects.append(
                context_ref_factory(context_ref) if context_ref_factory and context_ref else None
            )
        except Exception:
            # Non-Sefaria books may have titles that Ref cannot parse. They still
            # link context-free; relative citations fail closed below.
            context_objects.append(None)
    context_done = time.perf_counter()
    def resolve(invoke):
        if ibid_context is None:
            return invoke()
        return ibid_context.resolve(linker, invoke)

    try:
        if precomputed is not None:
            docs = resolve(lambda: precomputed.resolve_batch(
                linker, bk, batch, batch_start, book_context_refs=context_objects,
            ))
        else:
            docs = resolve(lambda: linker.bulk_link(
                [content for _, content, _ in batch],
                book_context_refs=context_objects,
                type_filter="citation", with_failures=True,
            ))
        if len(docs) != len(batch):  # a short reply would silently drop tail lines
            raise RuntimeError(f"bulk_link returned {len(docs)} docs for {len(batch)} lines")
    except Exception:
        if precomputed is None and not ner_alive():
            raise
        # Batch failed but NER is alive: replay line-by-line to pinpoint the broken
        # line, then FAIL the book on it (logged first, for diagnosis). A swallowed
        # line would silently drop all its citations while the book counts as linked
        # and the baseline advances past it — the bootstrap lost 216 lines this way.
        docs = []
        for batch_offset, (li, content, _context_ref) in enumerate(batch):
            try:
                if precomputed is not None:
                    # A replay failure is a corrupt/mismatched handoff or resolver bug.
                    # Re-batching would destroy the exact context boundary in the
                    # signed contract, so fail the book rather than guessing.
                    raise
                docs.append(resolve(lambda: linker.bulk_link(
                    [content],
                    book_context_refs=[context_objects[batch_offset]],
                    type_filter="citation", with_failures=True,
                )[0]))
            except Exception as le:
                skipped_log(f"{bk.source_name}\t{bk.canonical_he_title}\t{li}\t{type(le).__name__}: {le}")
                raise RuntimeError(
                    f"line {li} failed to link: {type(le).__name__}: {le}") from le

    resolve_done = time.perf_counter()
    records: list[LinkRecord] = []
    # Prefer the preceding emitted target for a remaining ambiguous "שם".  Unlike
    # Sefaria's resolver history this deliberately excludes records we reject.
    last_ref = ibid_context.last_emitted_ref if ibid_context is not None else None
    # (line, end, failed, candidates, trusted target) across transport boundaries.
    previous = ibid_context.previous_citation if ibid_context is not None else None
    for (line_index, content, context_ref), context_object, doc in zip(batch, context_objects, docs):
        # Digest the exact content the offsets index, so the build can drop this line's
        # links if the source book changed before Phase-2 applies them (cross-cycle drift).
        src_hash = content_hash(content)
        # spaCy spans are Python code-point offsets; the Kotlin consumer indexes the
        # SAME content string in UTF-16 units. They diverge only past a non-BMP char
        # (each adds one extra UTF-16 unit) — convert exactly, on the rare lines only.
        has_non_bmp = any(ord(c) > 0xFFFF for c in content)
        marker_end = leading_hebrew_numeral_marker_end(content)
        for rr in recoverable_citations(linker, doc.resolved_refs, content):
            try:
                # Failures are fetched only for recovery from the text; they must not
                # reset the "שם" antecedent.
                failed = getattr(rr, "resolution_failed", False)
                text_rebuilt = getattr(rr, "text_rebuilt", False)
                relative = has_raw_part_type(rr, "RELATIVE")
                ref = None if failed else _pick_ref(rr, last_ref)
                if not failed and not text_rebuilt:
                    # A citation this book drops is not an antecedent for the next "שם".
                    last_ref = None
                start, end = rr.raw_entity.span.range
                # Sefaria's reading of the citation before this one, whatever we emit for it.
                preceding = None if previous is None else (
                    content[previous[1]:start] if previous[0] == line_index and previous[1] <= start
                    else "" if previous[0] == line_index else None,
                    previous[2], previous[3])
                antecedent = None if previous is None else previous[4]
                previous = (line_index, end, failed, [] if failed else candidate_refs(rr), None)
                spelled = None
                regrouped = None
                chapters = ()
                chapter_led = False
                own_daf = None
                contextual = None
                quoted_ibid = None
                code_commentary = None
                bounded_chapter = None
                source_relative = explicit_own_relative_ref(
                    linker, rr, content, start, end, context_object, context_ref_factory)
                if source_relative is not None:
                    ref, relative = source_relative[0], False
                else:
                    scoped = explicit_relative_address(content[start:end], context_object, context_ref_factory)
                    if scoped is not None and (ref is None or ref.index.title == scoped[0].index.title):
                        # A successful upstream base-text guess must pass the same
                        # work-identity proof as a failed/ambiguous recovery.
                        continue
                if ref is None and not failed:
                    own_daf = own_tractate_daf_ref(
                        rr, content, start, end, context_object, context_ref_factory,
                        preceding=preceding,
                        names_elsewhere=functools.partial(clause_names_elsewhere, linker))
                    if own_daf is not None:
                        ref, relative = own_daf, False
                if failed and source_relative is None:
                    regrouped = parenthesized_address(linker, rr, content, context_object)
                    if regrouped is not None:
                        ref, (start, end), chapters = regrouped
                    else:
                        regrouped = chapter_led_citation(linker, rr, context_object)
                        if regrouped is not None:
                            ref, chapters = regrouped
                            chapter_led = True
                    relative = relative and regrouped is None
                    if ref is None and not relative:
                        spelled = geresh_numeral_ref(content[start:end], context_ref_factory)
                        ref = spelled
                if ref is None and not relative and antecedent is not None:
                    contextual = contextual_ibid_ref(
                        linker, rr, content, start, end, antecedent, context_ref_factory,
                        gap=None if preceding is None else preceding[0])
                    ref = contextual
                if ref is None and not relative:
                    quoted_ibid = quoted_biblical_ibid_ref(
                        linker, rr, content, start, end, context_ref_factory)
                    if quoted_ibid is None:
                        quoted_ibid = abbreviated_biblical_ibid_ref(
                            linker, rr, content, start, end, context_ref_factory)
                    ref = quoted_ibid
                if ref is None and not relative:
                    continue
                # A parenthesized Hebrew number opening a line is its structural
                # marker, not a citation (for example "(נח)" as Parashat Noach).
                if end <= marker_end:
                    continue
                anchor_text = content[start:end]
                relative_direction = None
                if ref is not None:
                    marked = amud_marked_daf(ref, content, start, end, context_ref_factory)
                    # The citation is where its daf and its named chapter meet. Only a
                    # chapter-led citation needs that; a parenthesized address stands alone.
                    if chapters and marked is None:
                        narrowed = chapter_amud(ref, chapters, content, end, context_ref_factory)
                        if narrowed is not None:
                            ref = narrowed
                        elif chapter_led:
                            continue
                    elif chapters and chapter_led and not any(c.overlaps(marked[0]) for c in chapters):
                        continue
                    if marked is not None:
                        ref, end = marked
                        anchor_text = content[start:end]
                    target_ref = ref.normal()
                    if is_implausible_citation_target(target_ref, anchor_text):
                        continue
                    if relative and talmud_address_contradicts(target_ref, anchor_text):
                        corrected = spelled_talmud_ref(target_ref, anchor_text, context_ref_factory)
                        if corrected is None:
                            continue
                        ref, target_ref = context_ref_factory(corrected), corrected
                    relative_direction = (source_relative[1] if source_relative is not None else
                                          relative_ref_direction(rr, ref, context_object, anchor_text))
                recovered = None
                if relative and relative_direction is None:
                    position = context_object
                    if bk.source_name != "Sefaria":
                        # Its context ref only counts lines; Phase 2 also needs the record's own context.
                        position = talmud_positions.get(line_index) if talmud_positions and context_ref else None
                    recovered = own_tractate_relative_ref(
                        anchor_text, position, context_ref_factory
                    ) or untitled_relative_ref(
                        anchor_text, position, context_ref_factory
                    ) or chapter_located_relative_ref(anchor_text, position, context_ref_factory)
                    if recovered is None:
                        continue
                    ref, relative_direction = recovered
                    # The newly accepted inner parenthesis must not hide the
                    # actual cited work outside the NER span: "כתב הרא\"ש ז\"ל
                    # לעיל (ח' ע\"א)" cites Rosh, not the underlying Gemara.
                    # Older whole-anchor forms keep their published behavior.
                    if "(" in _bare_anchor(anchor_text) and (
                            clause_names_elsewhere(
                                linker, clause_before(content, start), ref, ref.index.title)
                            or commentary_qualifier_after(content, end)):
                        continue
                    target_ref = ref.normal()
                code_commentary = explicit_code_commentary_ref(
                    ref, content, start, end, context_ref_factory)
                if code_commentary is not None:
                    ref, target_ref = code_commentary, code_commentary.normal()
                    relative_direction = None
                if not target_has_text(ref):
                    bounded_chapter = named_chapter_content_ref(
                        ref, rr, anchor_text, context_ref_factory, content=content, end=end, source_ref=context_object)
                    if bounded_chapter is None:
                        continue
                    ref, target_ref = bounded_chapter, bounded_chapter.normal()
                if (base_code_has_commentary_address(ref, anchor_text)
                        or has_unproven_code_number(rr, ref)):
                    continue
                if (has_raw_part_type(rr, "IBID") and not has_raw_part_type(rr, "NAMED")
                        and (antecedent is None or ref.index.title != antecedent.index.title)
                        and own_daf is None and recovered is None
                        and quoted_ibid is None):
                    # A rejected citation is not a book/address antecedent, even
                    # when Sefaria kept it in its private resolver history.
                    continue
                # Text-rebuilt targets stay out of the "שם" history, so later ibid
                # picks match a run without them.
                if (recovered is None and regrouped is None and spelled is None
                        and own_daf is None and contextual is None and quoted_ibid is None
                        and not text_rebuilt and code_commentary is None and bounded_chapter is None
                        and source_relative is None):
                    last_ref = ref
                record_context = context_ref if relative_direction is not None else None
                if has_non_bmp:
                    start += sum(1 for c in content[:start] if ord(c) > 0xFFFF)
                    end += sum(1 for c in content[:end] if ord(c) > 0xFFFF)
                records.append(LinkRecord(
                    book_key=bk, line_index=line_index,
                    start=start, end=end, target_ref=target_ref,
                    source_hash=src_hash,
                    context_ref=record_context,
                    relative_direction=relative_direction,
                ))
                previous = (*previous[:4], ref)
            except Exception as ce:
                # A broken citation is OUR bug (resolver/normal()), not corpus noise —
                # fail the book loudly (logged first) instead of silently losing a link.
                skipped_log(f"{bk.source_name}\t{bk.canonical_he_title}\t{line_index}\tcit\t{type(ce).__name__}: {ce}")
                raise RuntimeError(
                    f"citation on line {line_index} failed: {type(ce).__name__}: {ce}") from ce
    if ibid_context is not None:
        ibid_context.last_emitted_ref = last_ref
        ibid_context.previous_citation = previous
    if metrics is not None:
        finished = time.perf_counter()
        metrics.update({
            "batch_start": batch_start,
            "lines": len(batch),
            "chars": sum(len(content) for _, content, _ in batch),
            "words": words,
            "context_seconds": context_done - started,
            "resolve_seconds": resolve_done - context_done,
            "mapping_seconds": finished - resolve_done,
            "links": len(records),
        })
    return records, words


def ibid_state_path(shard_path: str) -> str:
    """The ibid-state sidecar of one batch shard."""
    return f"{shard_path[:-len('.jsonl')]}.ibid.json" if shard_path.endswith(".jsonl") \
        else f"{shard_path}.ibid.json"


def shard_digest(shard_path: str) -> str:
    """The sha256 of one committed shard, binding an ibid state to its records."""
    digest = hashlib.sha256()
    with open(shard_path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_ibid_state(shard_path: str, state: dict) -> None:
    """Commit a shard's ibid state atomically, like the shard itself.

    A torn sidecar must never be read as a valid restart point, and a crash between
    the two renames is safe in one direction only: the shard is written first, so a
    sidecar can never describe a batch whose records are missing.
    """
    import json as _json
    path = ibid_state_path(shard_path)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(state, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # The rename itself must reach the disk too, or a machine crash could leave
        # a shard whose state is gone - recoverable, but only by replaying the book.
        directory = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


def read_ibid_state(shard_path: str) -> dict:
    """The sidecar of `shard_path`; raises when it is absent or unreadable."""
    import json as _json
    with open(ibid_state_path(shard_path), encoding="utf-8") as fh:
        return _json.load(fh)


def process_book_checkpointed(
    linker, bk, lines, skipped_log, heartbeat, checkpoint_dir, out_path, on_recycle,
    precomputed=None, ner_indices=None, reuse=(), context_ref_factory=None,
    metric_log=None, cooperative=False, prior_lock_path=None, defer_when_heavy=True,
    recycle_cap=None, ibid_context=None,
):
    """Link a book with an atomic checkpoint after every transport batch.

    Large books can grow Sefaria's in-process Ref caches until the kernel OOM-kills
    the worker.  Each completed batch is therefore written as an immutable JSONL
    shard.  Once RSS crosses the cap, ``on_recycle`` execs a fresh worker image;
    that image skips verified-complete shards and continues at the next batch.
    The public per-book artifact is replaced atomically only after every shard is
    present, so a crash can expose neither a partial new artifact nor a false done
    marker.
    """
    from itertools import chain
    from linker_artifact import read_artifact

    if ner_indices is None:
        ner_indices = {line_index for line_index, _, _ in lines}
    else:
        ner_indices = set(ner_indices)
    current_by_index = {
        line_index: (content, context_ref)
        for line_index, content, context_ref in lines
    }
    stateful_ibid = ibid_context is not None
    if stateful_ibid:
        if reuse:
            raise RuntimeError("stateful ibid linking cannot reuse per-line artifacts")
        if set(ner_indices) != set(current_by_index):
            raise RuntimeError("stateful ibid linking must resolve the whole book")
        if cooperative:
            raise RuntimeError("stateful ibid linking cannot claim batches cooperatively")
    reuse_by_old = {}
    reused_destinations = set()
    for old_index, new_index in reuse:
        if (
            type(old_index) is not int
            or type(new_index) is not int
            or old_index < 0
            or new_index < 0
            or old_index in reuse_by_old
            or new_index in reused_destinations
        ):
            raise RuntimeError(f"invalid/duplicate line reuse mapping for {bk!r}")
        reuse_by_old[old_index] = new_index
        reused_destinations.add(new_index)
    current_indices = set(current_by_index)
    if (
        ner_indices & reused_destinations
        or ner_indices | reused_destinations != current_indices
    ):
        raise RuntimeError(f"line plan does not exactly partition {bk!r}")
    reused_records = []
    prior_artifact = out_path
    if cooperative and reuse_by_old:
        if not prior_lock_path:
            raise RuntimeError("cooperative line reuse requires a prior-artifact lock")
        prior_artifact = capture_prior_artifact(
            checkpoint_dir, out_path, prior_lock_path
        )
    if reuse_by_old and prior_artifact and os.path.exists(prior_artifact):
        for record in read_artifact(prior_artifact):
            if record.book_key != bk:
                raise RuntimeError(
                    f"prior artifact contains a different book identity for {bk!r}"
                )
            new_index = reuse_by_old.get(record.line_index)
            if new_index is None:
                continue
            current_content, current_context = current_by_index[new_index]
            expected_source_hash = content_hash(current_content)
            if record.source_hash != expected_source_hash:
                raise RuntimeError(
                    f"reused artifact source hash mismatch for "
                    f"{bk.source_name}/{bk.canonical_he_title}/{record.line_index}"
                )
            if record.context_ref is not None and record.context_ref != current_context:
                raise RuntimeError(
                    f"reused artifact context mismatch for "
                    f"{bk.source_name}/{bk.canonical_he_title}/{record.line_index}"
                )
            reused_records.append(LinkRecord(
                book_key=bk,
                line_index=new_index,
                start=record.start,
                end=record.end,
                target_ref=record.target_ref,
                line_index_base=record.line_index_base,
                source_path=record.source_path,
                source_hash=record.source_hash,
                context_ref=record.context_ref,
                relative_direction=record.relative_direction,
            ))
    os.makedirs(checkpoint_dir, exist_ok=True)
    shard_paths = []
    total_words = 0
    batches_this_life = 0
    cap = RSS_CAP if recycle_cap is None else float(recycle_cap)
    memory_at_start = worker_memory_bytes()
    # A shard alone is not a valid restart point for a stateful-ibid book: it holds
    # records but not Sefaria's last-three-reference state, so batch N+1 would resume
    # with a different history and a different output.  Each shard therefore carries
    # that state beside it, and a resumed batch adopts it.  Only where the state is
    # missing or unreadable does the replay-from-here rule apply, and then every later
    # shard goes too, because it was produced under history this life cannot reproduce.
    # (Before 2026-09-20 every shard was deleted unconditionally, so a book whose peak
    # exceeded the recycle cap replayed the same batches forever: relinks 35458432836
    # and 35477368314 both livelocked there and never finished.)
    ibid_resumable = stateful_ibid and context_ref_factory is not None
    # From the whole book: a batch may start mid-daf, below its heading.
    positions = talmud_positions(bk, lines, context_ref_factory)
    for i, batch in transport_batches(lines, ner_indices):
        total_words += sum(len(content.split()) for _, content, _ in batch)
        heartbeat()
        shard = os.path.join(checkpoint_dir, f"{i:012d}.jsonl")
        shard_paths.append(shard)
        if os.path.exists(shard) and stateful_ibid and not ibid_resumable:
            # This life cannot reproduce the history the shard was written under.
            remove_artifact(shard)
            remove_artifact(ibid_state_path(shard))
        if os.path.exists(shard):
            # A resumed shard is useful only if it is bound to this exact book, batch,
            # source content and contextual line identity. The cache manifest protects
            # bytes in transit; these semantic checks protect against restoring a valid
            # shard under the wrong plan/snapshot. Non-relative records intentionally
            # omit context_ref, while a relative record must retain the exact context.
            batch_by_index = {
                line_index: (content, context_ref)
                for line_index, content, context_ref in batch
            }
            for record in read_artifact(shard):
                if record.book_key != bk:
                    raise RuntimeError(f"checkpoint shard contains a different book for {bk!r}")
                current = batch_by_index.get(record.line_index)
                if current is None:
                    raise RuntimeError(
                        f"checkpoint shard line {record.line_index} is outside its exact batch for {bk!r}"
                    )
                current_content, current_context = current
                if record.source_hash != content_hash(current_content):
                    raise RuntimeError(
                        f"checkpoint shard source hash mismatch for "
                        f"{bk.source_name}/{bk.canonical_he_title}/{record.line_index}"
                    )
                if record.context_ref is not None and record.context_ref != current_context:
                    raise RuntimeError(
                        f"checkpoint shard context mismatch for "
                        f"{bk.source_name}/{bk.canonical_he_title}/{record.line_index}"
                    )
                utf16_length = len(current_content.encode("utf-16-le")) // 2
                if record.end > utf16_length:
                    raise RuntimeError(
                        f"checkpoint shard offset exceeds source line for "
                        f"{bk.source_name}/{bk.canonical_he_title}/{record.line_index}"
                    )
            if not stateful_ibid:
                continue
            try:
                ibid_context.restore_state(
                    read_ibid_state(shard), context_ref_factory, bk, i, batch,
                    shard_digest(shard),
                )
            except Exception as exc:  # any unusable state is recoverable by replay
                # Without this batch's history the next one cannot be reproduced, so
                # the shard is recomputed from the history in hand (the previous
                # batch's sidecar, or the book's start).  Every later shard is dropped
                # by the guard above, for the same reason.
                skipped_log(
                    f"{bk.source_name}\t{bk.canonical_he_title}\t"
                    f"batch {i}\tibid-state\t{type(exc).__name__}: {exc}"
                )
                ibid_resumable = False
                remove_artifact(shard)
                remove_artifact(ibid_state_path(shard))
            else:
                continue
        batch_claim = BatchClaim.acquire(checkpoint_dir, i) if cooperative else None
        if cooperative and batch_claim is None:
            continue
        try:
            # A peer may have committed the shard between our first existence check
            # and this lock acquisition. Its atomic rename is the completion marker.
            if os.path.exists(shard):
                continue
            metrics = {}
            records, _ = process_batch(
                linker, bk, batch, skipped_log, batch_start=i, precomputed=precomputed,
                context_ref_factory=context_ref_factory, metrics=metrics,
                ibid_context=ibid_context, talmud_positions=positions,
            )
            write_started = time.perf_counter()
            refuse_to_commit_after_memory_trouble()
            write_artifact(shard, records)
            if stateful_ibid:
                # Written after the shard, so a sidecar never describes absent records.
                state = ibid_context.state(bk, i, batch, shard_digest(shard))
                if _IMAGE_TAINTED:
                    remove_artifact(shard)
                    refuse_to_commit_after_memory_trouble()
                write_ibid_state(shard, state)
            write_done = time.perf_counter()
        finally:
            if batch_claim is not None:
                batch_claim.release()
        batches_this_life += 1
        # Drop batch-local resolved documents before measuring current RSS.  The
        # long-lived Sefaria caches remain; those are precisely what exec recycles.
        del records
        import gc
        current_rss = worker_memory_bytes()
        gc_started = time.perf_counter()
        collected = 0
        gc_ran = False
        if batches_this_life % GC_EVERY_BATCHES == 0 or current_rss >= cap * 0.8:
            collected = gc.collect()
            gc_ran = True
            current_rss = worker_memory_bytes()
        gc_done = time.perf_counter()
        if metric_log is not None:
            metric_log({
                **metrics,
                "write_fsync_seconds": write_done - write_started,
                "gc_seconds": gc_done - gc_started,
                "gc_collected": collected,
                "rss_bytes": current_rss,
                "memory_kind": "private" if _POOL_CHILD else "rss",
            })
        growth = current_rss - memory_at_start
        if (defer_when_heavy and HEAVY_BOOK_GROWTH_BYTES > 0 and growth > HEAVY_BOOK_GROWTH_BYTES
                and not gc_ran):
            # Cyclic batch garbage is not book growth: collect once and judge again
            # before sending the book to the serialized heavy phase.
            gc.collect()
            current_rss = worker_memory_bytes()
            growth = current_rss - memory_at_start
        if defer_when_heavy and HEAVY_BOOK_GROWTH_BYTES > 0 and growth > HEAVY_BOOK_GROWTH_BYTES:
            # The shard just written stays; the book resumes in the heavy phase under
            # a slot, and the caller recycles this bloated worker.  Deferral is judged
            # BEFORE the recycle cap and its threshold must sit below that cap, or a
            # ballooning book would only ever be recycled batch by batch, never deferred.
            raise BookDeferred(bk, growth, batches_this_life)
        if recycle_needed(current_rss, cap, batches_this_life):
            refuse_a_book_that_recycles_without_progress(checkpoint_dir)
            on_recycle(current_rss, batches_this_life)
            raise RuntimeError("worker recycle callback returned unexpectedly")

    refuse_to_commit_after_memory_trouble()
    close_memory_window()
    if cooperative and any(not os.path.isfile(path) for path in shard_paths):
        raise BookWorkInProgress
    records = list(reused_records)
    records.extend(chain.from_iterable(read_artifact(path) for path in shard_paths))
    records.sort(key=lambda record: (record.line_index, record.start, record.end, record.target_ref))
    count = write_artifact(out_path, records)
    return count, total_words


# Bavli-convention flag is read once into a module global by main().
_BAVLI_CONVENTION = False

_LEADING_PAREN_TOKEN = re.compile(r'^\s*\((?P<token>[א-ת\'"׳״]+)\)')
_HEBREW_NUMERAL_PUNCTUATION = str.maketrans("", "", "\'\"׳״")
_HEBREW_NUMERAL_VALUES = {
    letter: value
    for letter, value in zip(
        "אבגדהוזחטיכלמנסעפצקרשת",
        (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 30, 40, 50, 60, 70,
         80, 90, 100, 200, 300, 400),
    )
}


def leading_hebrew_numeral_marker_end(content: str) -> int:
    """End of a parenthesized Hebrew-number line marker, or zero.

    Short parenthesized citations also occur at the start of real source text.
    Accept only canonical Hebrew numerals: at most three letters in descending
    value order, plus the traditional טו/טז exceptions.  In particular, ``שם``
    is an ibid citation rather than the otherwise-valid numeral 340.
    """
    match = _LEADING_PAREN_TOKEN.match(content)
    if match is None:
        return 0
    letters = match.group("token").translate(_HEBREW_NUMERAL_PUNCTUATION)
    if not 1 <= len(letters) <= 3 or letters == "שם":
        return 0
    if any(letter not in _HEBREW_NUMERAL_VALUES for letter in letters):
        return 0
    values = [_HEBREW_NUMERAL_VALUES[letter] for letter in letters]
    if letters not in {"טו", "טז"} and any(
        left < right for left, right in zip(values, values[1:])
    ):
        return 0
    return match.end()


def _inherits_sections(candidate, last_ref) -> bool:
    candidate_sections = list(getattr(candidate, "sections", []))
    last_sections = list(getattr(last_ref, "sections", []))
    # The only permitted ambiguity is the final section (chapter/verse/daf
    # component).  A different depth is a different reference shape, not ibid
    # continuation evidence.
    if len(candidate_sections) != len(last_sections):
        return False
    prefix = candidate_sections[:-1]
    return bool(prefix) and prefix == last_sections[:-1]


def _ibid_candidate(rr, last_ref):
    """The candidate an ambiguous "שם" names, when only its section is in doubt.

    Sefaria offers the last three refs as ibid context without preferring the most
    recent, so "שם שם יד" after two chapters of one book is ambiguous and dropped
    (issue Otzaria/otzaria#1193). Deciding it by the preceding citation is only safe
    while every candidate is that same book — a candidate from another book would be
    a guess about which citation "שם" continues.
    """
    if last_ref is None or not has_raw_part_type(rr, "IBID"):
        return None
    candidates = [r.ref for r in rr.resolved_raw_refs if r.ref]
    if len(candidates) < 2:
        return None
    if {c.index.title for c in candidates} != {last_ref.index.title}:
        return None
    matches = [c for c in candidates if _inherits_sections(c, last_ref)]
    return matches[0] if len(matches) == 1 else None


def _pick_ref(rr, last_ref=None):
    """Return the single target Ref for a resolved citation, or None to drop it."""
    if (
        not has_raw_part_type(rr, "RELATIVE")
        and getattr(getattr(rr, "context_type", None), "name", None) == "CURRENT_BOOK"
    ):
        return None
    if rr.is_ambiguous:
        ibid_ref = _ibid_candidate(rr, last_ref)
        if ibid_ref is not None:
            return ibid_ref
        if not _BAVLI_CONVENTION:
            return None
        cands = [r.ref for r in rr.resolved_raw_refs if r.ref]
        return disambiguate_bavli(cands)
    return rr.ref if rr.ref else None


def has_raw_part_type(rr, type_name: str) -> bool:
    parts = getattr(getattr(rr, "raw_entity", None), "raw_ref_parts", ())
    return any(getattr(getattr(part, "type", None), "name", None) == type_name for part in parts)


_PAREN_ADDRESS_TYPES = {"NAMED", "NUMBERED", "DH"}
# A chapter locator: "בפרק X", "בפ' X", "בריש X", "בסוף X", "בס"פ X", "ר"פ X".
_CHAPTER_LEAD_RE = re.compile(r"^ו?[במ]?(?:פרק|פ['׳]|ריש|סוף|ס[\"״]פ|ר[\"״]פ|רפ[\"״]|ספ[\"״])(?![א-ת])")


def _locates_only(part) -> bool:
    # A commentator or book named before "(" is the work cited, so it must not be dropped.
    return part.type.name == "RELATIVE" or (
        part.type.name == "NAMED" and bool(_CHAPTER_LEAD_RE.match(_HTML_TAG_RE.sub("", part.text).strip())))


def parenthesized_address_parts(raw_entity, content: str, *, any_lead: bool = False) -> int | None:
    """Index of the first part inside the citation's one "(", or None.

    The parts before it must only locate the address ("בריש מרובה", "בפרק דלקמן"),
    unless ``any_lead``, and those inside must spell a title and an address on their
    own ("ב״ק דף סג:").
    """
    raw_start, raw_end = raw_entity.span.range
    text = content[raw_start:raw_end]
    if text.count("(") != 1 or ")" in text:
        return None
    paren = text.index("(")
    parts = list(raw_entity.raw_ref_parts)
    first = next((i for i, part in enumerate(parts) if part.span.range[0] > paren), None)
    if not first or any(part.span.range[1] > paren for part in parts[:first]):
        return None
    address = [part.type.name for part in parts[first:]]
    if not set(address) <= _PAREN_ADDRESS_TYPES or not (
            any_lead or all(_locates_only(part) for part in parts[:first])):
        return None
    if "NAMED" not in address or "NUMBERED" not in address:
        return None
    return first


def _related_works(one, other) -> bool:
    """Whether two indexes are one work, one comments on the other, or one names the other."""
    titles = (one.title, other.title)
    bases = [set(getattr(index, "base_text_titles", None) or ()) for index in (one, other)]
    return (titles[0] == titles[1] or titles[0] in bases[1] or titles[1] in bases[0]
            or bool(bases[0] & bases[1])
            or any(re.search(rf"(?:^|\s){re.escape(a)}(?:\s|$)", b) for a, b in (titles, titles[::-1])))


_NIQQUD_RE = re.compile(r"[\u0591-\u05c7]")
_QUOTE_INTRO_RE = re.compile(r"(?<![א-ת])(?:ו?שנאמר|דכתיב|כדכתיב|שהרי\s+הוא\s+אומר)(?![א-ת])")
# A '>' inside a quoted HTML attribute does not end its tag. Attribute text
# must never become positive quotation evidence after stripping half a tag.
_QUOTE_HTML_TAG = r'''<(?:"[^"]*"|'[^']*'|[^'">])*>'''
_QUOTE_HTML_TAG_RE = re.compile(_QUOTE_HTML_TAG)
_QUOTE_CLOSE_RE = re.compile(rf"(?:\s|{_QUOTE_HTML_TAG})*\)")


def hebrew_quote_words(text: str) -> tuple[str, ...]:
    """Words for exact quotation evidence; never used to change stored offsets."""
    import html

    plain = html.unescape(_QUOTE_HTML_TAG_RE.sub(" ", text)).replace("־", " ")
    def object_pronoun(match):
        word = match.group()
        bare = _NIQQUD_RE.sub("", word)
        # Quoters spell אֹתָם as אותם. Retain the vowel distinction from אַתֶּם
        # ("you"); deleting arbitrary ו/י would conflate unrelated words.
        if bare in {"אתם", "אתן", "אתו", "אתה"} and re.match(r"א[\u0591-\u05c7]*\u05b9", word):
            return "או" + bare[1:]
        return word
    plain = re.sub(r"[א-ת\u0591-\u05c7]+", object_pronoun, plain)
    plain = _NIQQUD_RE.sub("", plain)
    return tuple(re.findall(r"[א-ת]+", plain))


@functools.lru_cache(maxsize=1024)
def _hebrew_ref_words(normal: str) -> tuple[str, ...]:
    from sefaria.model import Ref

    return hebrew_quote_words(Ref(normal).text("he").as_string())


@functools.lru_cache(maxsize=64)
def _hebrew_book_quote_text(title: str) -> str:
    words = _hebrew_ref_words(title)
    return " " + " ".join(words) + " " if len(words) <= 100_000 else ""


def _unique_quote(title, words):
    corpus = _hebrew_book_quote_text(title)
    quote = " " + " ".join(words) + " "
    at = corpus.find(quote)
    # Count overlapping occurrences too; adjacent quotations share a boundary space.
    return at >= 0 and corpus.find(quote, at + 1) < 0


@functools.lru_cache(maxsize=1)
def _tanakh_quote_books() -> tuple:
    from sefaria.model import library

    # Include every base book, so a repeated phrase in another biblical book
    # cannot manufacture evidence for one of the resolver's remembered books.
    indexes = library.get_indexes_in_category("Tanakh", include_dependant=False, full_records=True)
    titles = sorted(index.title for index in indexes if list(index.categories) in (
        ["Tanakh", "Torah"], ["Tanakh", "Prophets"], ["Tanakh", "Writings"]))
    # The category also contains complex ancillary works that cannot be read at
    # book level. An incomplete base corpus cannot establish global uniqueness.
    if len(titles) != 39:
        return ()
    books = tuple((title, _hebrew_book_quote_text(title)) for title in titles)
    return books if all(corpus.strip() for _title, corpus in books) else ()


def adjoining_quote_words(content, start, end):
    # Keep a complete short quotation (sometimes two verses), but never borrow
    # evidence across another parenthesized citation or from HTML attributes.
    prefix = _QUOTE_HTML_TAG_RE.sub(" ", content[:start]).rstrip(" ([")
    cut = max(prefix.rfind(mark) for mark in "()[]")
    before = hebrew_quote_words(prefix[cut + 1:])[-24:]
    tail = content[end:]
    close = _QUOTE_CLOSE_RE.match(tail)
    after = hebrew_quote_words(tail[close.end():])[:8] if close else ()
    return before, after


def unquoted_clause_words(words, phrases):
    """Keep potential work names outside the positively identified quoted words.

    Biblical words such as 'בית' also begin book titles. Only words covered by
    an exact, globally unique four-word quotation lose their title reading.
    Original tokens (including author acronyms) are retained everywhere else.
    """
    normalized = [(word, i) for i, token in enumerate(words) for word in hebrew_quote_words(token)]
    quoted = set()
    for phrase in phrases:
        size = len(phrase.split())
        for j in range(len(normalized) - size + 1):
            if " " + " ".join(word for word, _i in normalized[j:j + size]) + " " == phrase:
                quoted.update(i for _word, i in normalized[j:j + size])
    return [word for i, word in enumerate(words) if i not in quoted]


class _TextRebuiltCitation:
    is_ambiguous = False
    resolution_failed = False
    context_type = None
    text_rebuilt = True

    def __init__(self, raw, ref):
        self.raw_entity, self.ref = raw, ref


def recoverable_citations(linker, citations, content):
    """Split a failed, comma-separated list of independently explicit Bible refs.

    Every group must resolve without source or ibid context. No commentator,
    relative address, range, or omitted title is stripped to manufacture a group.
    Rebuilt groups pass through the ordinary record/offset validation below and
    never enter the legacy ibid history.
    """
    for rr in citations:
        rebuilt = split_explicit_verses(linker, rr, content) if getattr(rr, "resolution_failed", False) else ()
        yield from rebuilt or (rr,)


def split_explicit_verses(linker, rr, content):
    raw = rr.raw_entity
    parts = list(raw.raw_ref_parts)
    groups = []
    for i, part in enumerate(parts):
        if part.type.name == "NAMED":
            if not groups or parts[groups[-1][-1]].type.name == "NUMBERED":
                groups.append([])
        if not groups or part.type.name not in ("NAMED", "NUMBERED"):
            return ()
        groups[-1].append(i)
    if not 2 <= len(groups) <= 4:
        return ()
    for group in groups:
        kinds = [parts[i].type.name for i in group]
        if kinds != ["NAMED", "NUMBERED", "NUMBERED"]:
            return ()
    raw_start = raw.span.range[0]
    for left, right in zip(groups, groups[1:]):
        gap = content[raw_start + parts[left[-1]].span.range[1]:raw_start + parts[right[0]].span.range[0]]
        if not re.fullmatch(r"\s*[,;]\s*", gap):
            return ()
    from sefaria.model.linker.ref_part import RawRef

    rebuilt = []
    for group in groups:
        try:
            span = raw.subspan(slice(group[0], group[-1] + 1))
        except AssertionError:
            return ()
        aligned = [parts[i].realign_to_new_raw_ref(raw.span, span) for i in group]
        smaller = RawRef(span, raw.lang, aligned)
        resolved = linker._ref_resolver.resolve_raw_ref(None, smaller)
        if (resolved is None or resolved.is_ambiguous or resolved.ref is None
                or getattr(resolved, "context_type", None) is not None):
            return ()
        ref = resolved.ref
        numbers = [_NIQQUD_RE.sub("", _plain_quotes(parts[i].text)) for i in group[1:]]
        if not all(re.fullmatch(_GERESH_NUMERAL, token) for token in numbers):
            return ()
        letters = [re.sub(r"[^א-ת]", "", token) for token in numbers]
        if (_top_category(ref.index) != "Tanakh" or getattr(ref.index, "dependence", None)
                or len(ref.sections) != 2 or list(ref.sections) != list(ref.toSections)
                or not all(_is_canonical_numeral(token) for token in letters)
                or list(ref.sections) != [_numeral(token) for token in letters]
                or ref.is_empty()):
            return ()
        rebuilt.append(_TextRebuiltCitation(smaller, ref))
    return tuple(rebuilt)


def quoted_tanakh_address(ref, content: str, start: int, end: int, *, absorbed_lead=False,
                         allow_introduced=True) -> bool:
    """Positive evidence for a dropped explicit verse or an inherited verse address.

    Four consecutive words must occur in the addressed text and uniquely in its
    book, excluding shared formulas such as "וידבר ה' אל משה". An absorbed
    lead additionally needs an explicit quotation introducer, or must itself be
    part of the quotation ("ויקרא (בראשית א ה) אלהים לאור יום"). A nearby author's
    name is not a quotation introducer. No fuzzy matching or alternate address is
    used: a printed wrong address is never silently repaired.
    """
    if (_top_category(ref.index) != "Tanakh" or getattr(ref.index, "dependence", None)
            or not 1 <= len(ref.sections) <= 2 or list(ref.sections) != list(ref.toSections)):
        return False
    expected = _hebrew_ref_words(ref.normal())
    if len(expected) < 4:
        return False
    before = hebrew_quote_words(content[:start])[-16:]
    tail = content[end:]
    close = _QUOTE_CLOSE_RE.match(tail)
    after = hebrew_quote_words(tail[close.end():])[:16] if close else ()

    def matches(words):
        return any(tuple(words[i:i + 4]) == expected[j:j + 4]
                   and _unique_quote(ref.index.title, words[i:i + 4])
                   for i in range(len(words) - 3) for j in range(len(expected) - 3))

    if not absorbed_lead:
        # At an ibid anchor, only the immediately adjoining quoted words count.
        return matches(before[-8:]) or matches(after[:8])
    raw = content[start:end]
    if "(" not in raw:
        return False
    opening = start + raw.index("(")
    lead = hebrew_quote_words(content[start:opening])
    prefix = _NIQQUD_RE.sub("", _plain_quotes(content[:opening]))
    intros = list(_QUOTE_INTRO_RE.finditer(prefix))
    if allow_introduced and intros:
        following = prefix[intros[-1].end():].strip()
        raw_lead = _NIQQUD_RE.sub("", _plain_quotes(content[start:opening]))
        raw_intros = list(_QUOTE_INTRO_RE.finditer(raw_lead))
        raw_lead = raw_lead[raw_intros[-1].end():].strip() if raw_intros else raw_lead
        if following == raw_lead and matches(after[:8]):
            return True
    # The whole absorbed lead is quoted text, not a preceding work's name.
    return bool(lead and len(lead) <= 3 and len(after) >= 3 and any(
        lead + after[:4 - len(lead)] == expected[i:i + 4]
        and _unique_quote(ref.index.title, lead + after[:4 - len(lead)])
        for i in range(len(expected) - 3)))


def lead_names_other_works(linker, raw, first: int, ref, content: str | None = None) -> bool:
    """Whether the parts before ``first`` (the parenthesis) stay outside the citation of ref.

    The NER may join words before the parenthesis to the citation in it ("בנדה (ויקרא
    טו יט)", "בספר הישר בפרק זה בורר (סנהדרין דף כד.)", Otzaria/otzaria#3453, #3450).
    A Bible address needs an adjoining, distinctive quotation. Its lead must itself
    be quoted words, or complete unrelated titles after an explicit quotation
    introducer. A Bavli address needs complete unrelated titles plus a chapter that
    contains it. Unknown words and unfinished titles ("ירושלמי", "רש\"י") do not
    prove that a lead can be discarded. Existing chapter-only locators are handled
    separately by parenthesized_address.
    """
    if _top_category(ref.index) != "Tanakh" and not _bavli_tractate_itself(ref.index):
        return False
    from sefaria.model.linker.ref_part import LEAF_TRIE_ENTRY

    trie = linker._ref_resolver.get_ref_part_title_trie()
    tanakh = _top_category(ref.index) == "Tanakh"
    if tanakh:
        if content is None or not quoted_tanakh_address(
                ref, content, *raw.span.range, absorbed_lead=True):
            return False
        if quoted_tanakh_address(ref, content, *raw.span.range,
                                absorbed_lead=True, allow_introduced=False):
            return True  # the entire lead is part of the actual quoted verse
    placed = False
    works = {}
    for part in raw.raw_ref_parts[:first]:
        text = _NIQQUD_RE.sub("", _plain_quotes(part.text))
        if part.type.name == "RELATIVE":
            continue
        if part.type.name in ("NAMED", "NUMBERED") and _CHAPTER_LEAD_RE.match(text):
            if any(chapter.overlaps(ref) for chapter in named_chapters(text, ref)):
                placed = True
            elif part.type.name == "NUMBERED":
                return False
            # A named chapter phrase only narrows the address (see parenthesized_address).
            continue
        if part.type.name != "NAMED":
            return False
        if not tanakh:
            chapters = named_chapters(text, ref)
            if chapters:
                if not any(chapter.overlaps(ref) for chapter in chapters):
                    return False
                placed = True
                continue
        if tanakh:
            text = _QUOTE_INTRO_RE.sub("", text).strip()
            if not text:
                continue
        titles, _ = trie.get_continuations(text, allow_partial=False)
        if titles is None:
            return False  # the trie is incomplete, especially for commentary aliases
        named = {}
        for node in (titles[LEAF_TRIE_ENTRY] if LEAF_TRIE_ENTRY in titles else ()):
            try:
                index = node.ref().index
            except Exception:
                continue
            named[index.title] = index
        if not named:
            return False
        works.update(named)
    category = _top_category(ref.index)
    return (tanakh or placed) and not any(
        _related_works(work, ref.index) or (tanakh and getattr(work, "dependence", None))
        or (not placed and _top_category(work) == category)
        for work in works.values())


def _top_category(index):
    return (list(getattr(index, "categories", None) or ()) or [None])[0]


def parenthesized_address(linker, rr, content: str, context_ref):
    """(Ref, (start, end), chapters) of a failed citation's parenthesized address, resolved alone.

    The NER joins a chapter phrase the resolver cannot read to an explicit address in
    parentheses; the address alone is unambiguous (Otzaria/otzaria#2490). Any other lead
    must pass :func:`lead_names_other_works`. The chapters the
    phrase names that overlap it may narrow it, but never drop it: authors use some
    chapter names for other chapters than Sefaria ("דיני ממונות בתרא" for "זה בורר").
    """
    raw = rr.raw_entity
    first = parenthesized_address_parts(raw, content, any_lead=True)
    if first is None:
        return None
    from sefaria.model.linker.ref_part import RawRef

    try:
        span = raw.subspan(slice(first, None))
    except AssertionError:  # Sefaria's own guard for part/token misalignment
        return None
    parts = [part.realign_to_new_raw_ref(raw.span, span) for part in raw.raw_ref_parts[first:]]
    resolved = linker._ref_resolver.resolve_raw_ref(context_ref, RawRef(span, raw.lang, parts))
    if resolved is None or resolved.is_ambiguous or resolved.ref is None:
        return None
    if getattr(getattr(resolved, "context_type", None), "name", None) == "CURRENT_BOOK":
        return None
    if not all(_locates_only(part) for part in raw.raw_ref_parts[:first]) and not lead_names_other_works(
            linker, raw, first, resolved.ref, content):
        return None
    raw_start, raw_end = raw.span.range
    text = content[raw_start:raw_end]
    lead = text[raw.raw_ref_parts[0].span.range[0]:text.index("(")]
    chapters = tuple(c for c in named_chapters(lead, resolved.ref) if c.overlaps(resolved.ref))
    return resolved.ref, span.range, chapters


def chapter_led_splits(raw_entity) -> list[tuple[str, int, int]]:
    """(lead text, title part index, title start) for each chapter-then-title reading.

    The title is a NAMED part, or its tail from a word opening with "ד" ("בפרק שני
    דכתובות"); an address of NUMBERED/DH parts must follow it.
    """
    parts = list(raw_entity.raw_ref_parts)
    if any(part.type.name not in _PAREN_ADDRESS_TYPES for part in parts):
        return []
    splits = []
    for i, part in enumerate(parts):
        address = [p.type.name for p in parts[i + 1:]]
        if part.type.name != "NAMED" or "NUMBERED" not in address or not set(address) <= {"NUMBERED", "DH"}:
            continue
        part_start = part.span.range[0]
        starts = [part_start] if i else []
        starts += [part_start + m.end() - 1 for m in re.finditer(r"\sד", part.text)]
        for title_start in starts:
            lead = raw_entity.span.text[parts[0].span.range[0]:title_start].strip()
            if lead:
                splits.append((lead, i, title_start))
    return splits


# Words that place a citation within its chapter: "בריש", "סוף", "שלהי".
_CHAPTER_EDGE_RE = re.compile(r"^ו?[במ]?(?:ריש|סוף|שלהי) ")
_LEAD_PREFIXES = ("", "ו", "ב", "מ", "ד", "ל", "וב", "ומ", "דב", "דמ")


def _plain_quotes(text: str) -> str:
    return " ".join(_HTML_TAG_RE.sub(" ", text).replace("״", '"').replace("׳", "'").split())


def chapter_lead_names(lead: str) -> set[str]:
    """The chapter designations a citation's lead text can spell exactly."""
    lead = _plain_quotes(lead)
    leads = {lead, _CHAPTER_EDGE_RE.sub("", lead)}
    return {l[len(p):] for l in leads for p in _LEAD_PREFIXES if l.startswith(p) and len(l) > len(p)}


def chapter_names(match_templates, term_titles) -> frozenset[str]:
    """Every spelling of one chapter node from Sefaria's own term titles.

    "פרק"+"שני" also yields "פ"ב", "רפ"ב" and "ספ"ב", which the ordinal term
    spells with the chapter word already in it.
    """
    names = set()
    for template in match_templates:
        slugs = template["term_slugs"]
        titles = [[_plain_quotes(t) for t in term_titles(slug)] for slug in slugs]
        names.update(" ".join(combo) for combo in itertools.product(*titles))
        if len(slugs) == 2 and slugs[0] == "perek":
            names.update(t for t in titles[1] if re.match(r'^[רס]?פ"', t))
    return frozenset(names)


@functools.lru_cache(maxsize=None)
def _book_chapters(index_title: str) -> tuple:
    from sefaria.model import Ref, library
    from sefaria.model.schema import NonUniqueTerm

    def term_titles(slug):
        term = NonUniqueTerm.init(slug)
        return term.get_titles("he") if term else []

    structs = getattr(library.get_index(index_title), "alt_structs", None) or {}
    # A grouping node (Sippurei Maasiyot's "נוספים") has no range of its own.
    return tuple(
        (Ref(node["wholeRef"]), chapter_names(node.get("match_templates", []), term_titles))
        for node in structs.get("Chapters", {}).get("nodes", []) if "wholeRef" in node
    )


def named_chapters(lead: str, ref) -> tuple:
    """The chapters of ref's book that a citation's lead text names."""
    names = chapter_lead_names(lead)
    return tuple(whole for whole, spelled in _book_chapters(ref.index.title) if names & spelled)


def chapter_led_citation(linker, rr, context_ref):
    """(Ref, named chapters) of a failed "פרק X דמסכת דף N" citation, or None.

    Sefaria reads a chapter only after the title ("כתובות פרק שני דף יח"), so a chapter
    before it fails the whole citation. The title and address resolve alone, and the
    chapter named must contain the result (Otzaria/otzaria#2490).
    """
    raw = rr.raw_entity
    splits = chapter_led_splits(raw)
    if not splits:
        return None
    from sefaria.model.linker.ref_part import RawRef

    parts = raw.raw_ref_parts
    raw_start = raw.span.range[0]
    end = parts[-1].span.range[1]
    found = {}
    for lead, i, title_start in splits:
        span = raw.span.doc.subspan(slice(raw_start + title_start, raw_start + end))
        title = parts[i].clone(span=raw.span.subspan(slice(title_start, parts[i].span.range[1])))
        address = [p.realign_to_new_raw_ref(raw.span, span) for p in [title, *parts[i + 1:]]]
        resolved = linker._ref_resolver.resolve_raw_ref(context_ref, RawRef(span, raw.lang, address))
        if resolved is None:
            continue
        for candidate in resolved.resolved_raw_refs if resolved.is_ambiguous else [resolved]:
            ref = candidate.ref
            if ref is None or getattr(getattr(candidate, "context_type", None), "name", None):
                continue
            chapters = named_chapters(lead, ref)
            if any(chapter.overlaps(ref) for chapter in chapters):
                known = found.setdefault(ref.normal(), (ref, []))[1]
                known.extend(c for c in chapters if all(c.normal() != k.normal() for k in known))
    if len(found) != 1:
        return None
    ref, chapters = next(iter(found.values()))
    return ref, tuple(chapters)


# A mark after a daf, past spaces and tags ("מד</small>:"): "." for amud a, ":" or "ע"ב" for amud b.
_AMUD_AFTER_RE = re.compile(r'(?:\s|<[^>]*>)*(?:(?P<mark>[.:])|ע["״](?P<side>[אב]))')


def chapter_amud(ref, chapters, content: str, end: int, ref_factory):
    """The one amud of a whole daf inside its named chapters, or None when the text marks the other.

    "רפ"ג דערכין (דף י"ג)" is 13b, where chapter 3 opens; "דף יז." before a chapter that
    opens on 17b contradicts it.
    """
    match = _WHOLE_DAF_RE.match(ref.normal())
    if match is None or ref_factory is None or not ref.is_talmud():
        return ref
    amudim = {side: _amud_ref(match.group(1), int(match.group(2)), side, ref_factory) for side in "אב"}
    inside = [side for side, amud in amudim.items()
              if amud is not None and any(chapter.overlaps(amud) for chapter in chapters)]
    if len(inside) != 1:
        return ref
    written = _AMUD_AFTER_RE.match(content, end)
    if written is not None:
        side = written["side"] or ("א" if written["mark"] == "." else "ב")
        if side != inside[0]:
            return None
    return amudim[inside[0]]


_WHOLE_DAF_RE = re.compile(r"^(.+) (\d+)$")


def amud_marked_daf(ref, content: str, start: int, end: int, ref_factory):
    """(Ref, end) of amud b after a whole-daf anchor, past spaces and HTML tags.

    The NER cuts "נא:" before the mark when "ושם" follows (Otzaria/otzaria#2490). Only ":"
    mid-line is an amud mark: "." also separates list items, and a line-final ":" ends it.
    The returned end includes the mark in the original content, preserving its offsets.
    """
    if ref_factory is None or end >= len(content):
        return None
    written = _AMUD_AFTER_RE.match(content, end)
    if written is None or written["mark"] != ":":
        return None
    marked_end = written.end()
    if not _HTML_TAG_RE.sub("", content[marked_end:]).strip():
        return None
    match = _WHOLE_DAF_RE.match(ref.normal())
    if match is None or not ref.is_talmud():
        return None
    tokens = _HTML_TAG_RE.sub("", content[start:end]).split()
    if not tokens or int(match.group(2)) not in _hebrew_number(tokens[-1]):
        return None
    marked = _amud_ref(match.group(1), int(match.group(2)), "ב", ref_factory)
    return None if marked is None else (marked, marked_end)


def relative_ref_direction(rr, target_ref, source_ref, anchor_text: str) -> str | None:
    """Validate an explicit relative citation and return its direction.

    Context is deliberately confined to NER parts marked RELATIVE. A bare
    "לעיל"/"לקמן" without a numbered or named destination is too vague and is
    dropped. An unnamed relative target must remain in the exact source book;
    cross-book resolution is accepted only when the citation explicitly names a
    book. Same-book targets must also be on the requested side of the source.
    """
    if not has_raw_part_type(rr, "RELATIVE"):
        return None
    if getattr(rr, "is_ambiguous", False) or source_ref is None or target_ref is None:
        return None
    context_type = getattr(getattr(rr, "context_type", None), "name", None)
    if context_type != "CURRENT_BOOK":
        return None
    side = _relative_side(anchor_text)
    if side is None:
        return None
    above, below = side == "above", side == "below"
    has_named = has_raw_part_type(rr, "NAMED")
    has_numbered = has_raw_part_type(rr, "NUMBERED") or has_raw_part_type(rr, "RANGE")
    if not (has_named or has_numbered):
        return None
    source_title = getattr(getattr(source_ref, "index", None), "title", None)
    target_title = getattr(getattr(target_ref, "index", None), "title", None)
    if not has_named and (not source_title or target_title != source_title):
        return None
    if target_title == source_title:
        source_order = source_ref.order_id()
        target_order = target_ref.order_id()
        if above and not target_order < source_order:
            return None
        if below and not target_order > source_order:
            return None
    return side


def _relative_side(anchor_text: str) -> str | None:
    above = "לעיל" in anchor_text
    below = "לקמן" in anchor_text or "להלן" in anchor_text
    if above == below:
        return None
    return "above" if above else "below"


_GEMATRIA = {
    "א": 1, "ב": 2, "ג": 3, "ד": 4, "ה": 5, "ו": 6, "ז": 7, "ח": 8, "ט": 9,
    "י": 10, "כ": 20, "ך": 20, "ל": 30, "מ": 40, "ם": 40, "נ": 50, "ן": 50, "ס": 60,
    "ע": 70, "פ": 80, "ף": 80, "צ": 90, "ץ": 90, "ק": 100, "ר": 200, "ש": 300, "ת": 400,
}
# "טו, ב" / "טו,ב" / "ט״ו ע״ב" — the daf token and its amud marker.
_DAF_COMMA_RE = re.compile(r"(\S+?)\s*,\s*([אב])(?![א-ת])")
# A bare ``עא`` is a very common daf numeral (e.g. ``עבודה זרה עא, א``),
# not an amud marker.  Require its customary geresh/gershayim so the comma
# form remains the single address in that phrase.
_DAF_AMUD_RE = re.compile(r"(\S+)\s+ע[\"'״׳]([אב])(?![א-ת])")
_TALMUD_TARGET_RE = re.compile(r"^(.+?) (\d+)([ab])(?::[\d:]+)?(?:-(\d+)([ab]))?(?::|$)")


def _numeral(letters: str) -> int | None:
    values = [_GEMATRIA[c] for c in letters]
    if not values or len(values) > 3:
        return None
    # A numeral's letters never ascend ("קנג"); טו/טז are the one spelled exception.
    if letters not in ("טו", "טז") and any(a < b for a, b in zip(values, values[1:])):
        return None
    return sum(values)


def _hebrew_number(token: str) -> set[int]:
    letters = re.sub(r"[^א-ת]", "", token.split("דף")[-1])
    # A one-letter proclitic ("דל״ג", "בטו") may stick to the number.
    candidates = (letters, letters[1:]) if len(letters) > 1 else (letters,)
    return {value for value in map(_numeral, candidates) if value is not None}


def _spelled_addresses(anchor_text: str) -> list[tuple[str, str]]:
    return [
        (token, side)
        for regex in (_DAF_COMMA_RE, _DAF_AMUD_RE)
        for token, side in (m.groups() for m in regex.finditer(anchor_text))
        if _hebrew_number(token)
    ]


def talmud_address_contradicts(target_ref: str, anchor_text: str) -> bool:
    """True when the anchor spells a daf/amud and the resolved target is a different one.

    The Sefaria linker reads an in-book relative daf citation in comma form
    ("להלן עירובין טו, ב") as an offset from the citing amud, not as an address
    (issue Otzaria/otzaria#1348).
    """
    target = _TALMUD_TARGET_RE.match(target_ref)
    if not target:
        return False
    spelled = _spelled_addresses(anchor_text)
    if not spelled:
        return False
    addresses = {
        (int(target.group(i)), "א" if target.group(i + 1) == "a" else "ב")
        for i in (2, 4) if target.group(i)
    }
    return not any(
        (daf, side) in addresses for token, side in spelled for daf in _hebrew_number(token)
    )


def spelled_talmud_ref(target_ref: str, anchor_text: str, ref_factory) -> str | None:
    """The ref the anchor itself spells, in the resolved target's tractate.

    Recovers the true address of a contradicted citation; the tractate comes from
    the resolved target because the citation named it. Returns None when the anchor
    spells more than one address, when the daf is unreadable, or when the rebuilt
    ref does not exist (a daf past the end of the tractate).
    """
    target = _TALMUD_TARGET_RE.match(target_ref)
    spelled = _spelled_addresses(anchor_text)
    if not target or len(spelled) != 1 or ref_factory is None:
        return None
    ref = _daf_ref(target.group(1), *spelled[0], ref_factory)
    return ref.normal() if ref is not None else None


def _daf_ref(tractate: str, token: str, side: str, ref_factory):
    letters = re.sub(r"[^א-ת]", "", token.split("דף")[-1])
    daf = _numeral(letters) or (_numeral(letters[1:]) if len(letters) > 1 else None)
    if daf is None:
        return None
    return _amud_ref(tractate, daf, side, ref_factory)


def _amud_ref(tractate: str, daf: int, side: str, ref_factory):
    try:
        return ref_factory(f"{tractate} {daf}{'a' if side == 'א' else 'b'}")
    except Exception:
        return None


# "פ"ב." / "פ"ק." abbreviate פרק ב / פרק קמא, not a daf.
_CHAPTER_ABBREVIATION_RE = re.compile(r"פ[\"'״׳]")
# The address may open a parenthesis after the word: "לעיל (דף לו:)" (Otzaria/otzaria#3450).
_RELATIVE_WORD = r"[וד]?(?:לקמן|לעיל|להלן)(?:\s*\(\s*|\s+)"
_UNTITLED_DAF = r"[א-ת]{1,3}|[א-ת]{1,2}[\"״][א-ת]|[א-ת]['׳]"
# One daf address: "יא:", "דף כ"ג.", "ב,א" or "צה ע"ב".
_DAF_ADDRESS = (
    rf"(?P<prefix>דף\s+|ד['׳]\s*)?(?P<daf>{_UNTITLED_DAF})"
    r"(?:(?P<mark>[.:])|\s*,\s*(?P<comma>[אב])|\s+ע[\"״](?P<amud>[אב]))")
# The whole anchor: "לקמן יא:", "לעיל דף כ"ג.", "לעיל ב,א", "לקמן צה ע"ב", or "לקמן ע"ב".
_UNTITLED_RELATIVE_RE = re.compile(
    rf"{_RELATIVE_WORD}(?:{_DAF_ADDRESS}|ע[\"״](?P<only>[אב]))")
_PUNCT_SIDE = {".": "א", ":": "ב"}


def _bare_anchor(anchor_text: str) -> str:
    return anchor_text.strip().strip("()").strip()


def own_tractate_relative_ref(anchor_text: str, source_ref, ref_factory):
    """The (Ref, direction) of "לקמן גיטין דף כג:" cited from within Gittin or its commentary.

    Sefaria leaves a relative citation that names a tractate unresolved, since its
    לקמן/לעיל part matches nothing once the title supplies the book. The whole anchor
    must be the relative word, the source's own tractate and one daf address.
    """
    if not _is_talmud_source(source_ref, ref_factory):
        return None
    tractates = _own_tractates(source_ref, ref_factory)
    if len(tractates) != 1:
        return None
    he = ref_factory(tractates[0]).index.get_title("he")
    if not he:
        return None
    match = re.fullmatch(rf"{_RELATIVE_WORD}{re.escape(he)}\s+{_DAF_ADDRESS}",
                         _bare_anchor(anchor_text))
    if match is None:
        return None
    return _on_stated_side(_address_ref(match, tractates[0], ref_factory), source_ref,
                           _relative_side(match.group()))


def untitled_relative_ref(anchor_text: str, source_ref, ref_factory):
    """The (Ref, direction) of "לקמן יא:" or "לעיל ע"א" cited from within a tractate or its commentary.

    The tractate is the source's own: the tractate itself, or the one a commentary
    "X on <tractate>" is on. A lone amud is the source's own daf. Anything beyond one
    such address (a chapter, a section, "שם", a typo) is dropped.
    """
    match = _UNTITLED_RELATIVE_RE.fullmatch(_bare_anchor(anchor_text))
    if match is None or not _is_talmud_source(source_ref, ref_factory):
        return None
    side_word = _relative_side(match.group())
    tractates = _own_tractates(source_ref, ref_factory)
    if len(tractates) != 1:
        return None
    if match["only"]:
        amud = source_ref.sections[0]
        # A source spanning amudim has no single own daf to take the other side of.
        if source_ref.toSections[0] != amud:
            return None
        target = _amud_ref(tractates[0], (amud + 1) // 2, match["only"], ref_factory)
        return _on_stated_side(target, source_ref, side_word)
    return _on_stated_side(_address_ref(match, tractates[0], ref_factory), source_ref, side_word)


# A daf address ending the anchor: "(דף כא.)", "התם (דף כז.)", "שם לט:".
_TRAILING_DAF_RE = re.compile(rf"(?:^|(?<=[\s(])){_DAF_ADDRESS}$")


def _in_tractate(ref, tractate: str) -> bool:
    title = getattr(getattr(ref, "index", None), "title", None) or ""
    return title == tractate or title.endswith(f" on {tractate}")


def candidate_refs(rr) -> list:
    found = [r.ref for r in rr.resolved_raw_refs] if getattr(rr, "is_ambiguous", False) else [rr.ref]
    return [ref for ref in found if ref is not None]


def clause_before(content: str, start: int) -> list[str]:
    """The last words before an anchor, back to its clause's start; its own "(" is skipped."""
    text = _plain_quotes(content[:start]).rstrip(" ([")
    cut = max(text.rfind(mark) for mark in ".:;()[]")
    return text[cut + 1:].split()


def commentary_qualifier_after(content: str, end: int) -> bool:
    """A DH immediately after the citation still qualifies its cited work.

    E.g. Tosafot Shevuot604: 'לעיל (דף מו.) [בד"ה בדרבנן וכו׳]'. The
    qualifier may sit outside the NER entity but rules out a bare Gemara target.
    """
    tail = content[end:]
    close = _QUOTE_CLOSE_RE.match(tail)
    if close:
        tail = tail[close.end():]
    tail = " ".join(_QUOTE_HTML_TAG_RE.sub(" ", tail).replace(
        "״", '"').replace("׳", "'").split()).lstrip(" ([")
    return bool(re.match(r"(?:ב?ד[\"']ה)(?![א-ת])", tail))


def untitled_daf_anchor(anchor_text, source_ref, ref_factory):
    """One explicit amud, with only an ibid, Gemara or verified chapter locator.

    A trailing address alone is insufficient: "רש\"י (דף כא.)" names Rashi,
    not the base Talmud. Every word before the address must be accounted for.
    """
    if not _is_talmud_source(source_ref, ref_factory) or _relative_side(anchor_text) is not None:
        return None
    tractates = _own_tractates(source_ref, ref_factory)
    anchor = _NIQQUD_RE.sub("", _plain_quotes(_bare_anchor(anchor_text)))
    match = _TRAILING_DAF_RE.search(anchor)
    if len(tractates) != 1 or match is None:
        return None
    target = _address_ref(match, tractates[0], ref_factory)
    if target is None:
        return None
    prefix = anchor[:match.start()].strip(" ()")
    gemara = r"ו?[במ]?(?:גמרא|גמ['\"]?)"
    if not prefix or re.fullmatch(rf"(?:(?:שם|התם)\s*)?(?:{gemara})?|{gemara}\s+(?:שם|התם)", prefix):
        return target
    if _OWN_CHAPTER_RE.fullmatch(prefix):
        source_amud = _amud_ref(tractates[0], (source_ref.sections[0] + 1) // 2,
                                "א" if source_ref.sections[0] % 2 else "ב", ref_factory)
        return target if source_amud is not None and any(
            whole.overlaps(source_amud) and whole.overlaps(target)
            for whole, _names in _book_chapters(tractates[0])) else None
    if not (_CHAPTER_LEAD_RE.match(prefix) or re.match(r"ו?[במ]?[רס]?פ[\"']", prefix)):
        return None
    return target if any(chapter.overlaps(target) for chapter in named_chapters(prefix, target)) else None


def own_tractate_daf_ref(rr, content: str, start: int, end: int, source_ref, ref_factory, *,
                         preceding=None, names_elsewhere=None):
    """The own tractate's amud for an untitled daf citation Sefaria reads only in that tractate.

    In a Bavli tractate or its "X on <tractate>" commentary, "(דף כא.)" or "התם (דף כז.)"
    names the tractate's own daf. Sefaria offers it together with the same address in the
    commentary, or only in the commentary through the current book, so the citation was
    dropped (Otzaria/otzaria#3450). Every candidate must be that one amud, the anchor must
    end with it and name no side; a relative citation stays with its own recovery.

    Sefaria's reading may only lack what the text says elsewhere: the clause before the
    anchor must name no other tractate or chapter (``names_elsewhere``), and the preceding
    citation (``preceding``: the text between, or None on another line; whether it failed;
    its candidates) must lie in this tractate when "שם" refers to it or when nothing but
    a few words separate them ("(ב"מ דף מד: ודף מה.)").
    """
    anchor_text = content[start:end]
    target = untitled_daf_anchor(anchor_text, source_ref, ref_factory)
    if target is None:
        return None
    tractate = target.index.title
    candidates = candidate_refs(rr)
    if target is None or not candidates:
        return None
    for candidate in candidates:
        if not _in_tractate(candidate, tractate):
            return None
        if list(candidate.sections) != list(target.sections) or (
                list(candidate.toSections) != list(target.toSections)):
            return None
    gap, failed, refs = preceding if preceding is not None else (None, True, [])
    adjacent = gap is not None and len(_plain_quotes(gap).strip(" ()[].,:;-–").split()) <= 3
    if has_raw_part_type(rr, "IBID") or adjacent:
        if failed or not refs or not all(_in_tractate(ref, tractate) for ref in refs):
            return None
    if names_elsewhere is not None and names_elsewhere(clause_before(content, start), target, tractate):
        return None
    return target


_RELATIVE_DISCOURSE_ABBREVIATIONS = frozenset({
    'אע"ג', 'וא"ת', 'א"ת', 'וי"ל', 'י"ל', 'צ"ל', 'א"כ', 'א"נ', 'ה"ל', 'דה"ל',
    'וה"ל', 'והל"ל', 'הל"ל', 'דהל"ל', 'ע"י', 'ע"כ', 'א"ל', 'כ"ע', 'לכ"ע', 'דכ"ע',
    'י"ס', 'ס"ד', 'א"נ', 'מ"מ', 'מ"ל', 'נ"מ', 'ה"נ', 'כ"ש', 'א"א', 'כדפ"ל',
    'ת"ל', 'כר"ע', 'ר"ע', 'דל"ל', 'דה"נ', 'ה"מ', 'בד"א', 'אע"פ', 'כה"ג', 'ב"ד',
})
_NATIVE_CITATION_VERB_RE = re.compile(
    r'ו?[דכבש]?(?:אמרינן|אמר|קאמר|פריך|תנא|תנן|תניא|משמע|מוכח|מייתי|כדאמר|כדמוכח)')
_CITED_WRITING_RE = re.compile(
    r'ו?[דכבשמל]?(?:כתבתי|כתבנו|פירשתי|פירשנו|ביארתי|ביארנו|כתב|כתבו|פירש|פירשו|פירוש|לפרש|מפרש|מפרשים|פי[\"\']|כמ[\"]ש|כמש[\"]כ)')


def relative_clause_is_clear(linker, rr, content, start, end, source_ref, target):
    """A scoped relative address may quote a discussion, not the earlier writer.

    Keep unknown writer acronyms and governing commentary/self attributions out.
    Ordinary Talmud abbreviations and unfinished regular-word title prefixes do
    not name a book. A native discussion predicate can separate Tosafot's Rashi/
    Rabbenu Tam attribution from the Gemara address that follows it.
    """
    if commentary_qualifier_after(content, end) or has_raw_part_type(rr, "DH"):
        return False
    words = clause_before(content, start)
    if not clause_names_elsewhere(linker, words, target, target.index.title):
        return True
    if len(words) > 512:
        return False
    words = [_NIQQUD_RE.sub('', _plain_quotes(word)).strip('(),;:.') for word in words]
    raw_anchor = _NIQQUD_RE.sub('', _plain_quotes(content[start:end]))
    native = [i for i, word in enumerate(words) if _NATIVE_CITATION_VERB_RE.fullmatch(word)]
    for i, word in enumerate(words):
        if word in ('ההיא', 'מפרש') and (words[i + 1:] == ['ד'] or
                i == len(words) - 1 and raw_anchor.startswith('ד')):
            native.append(i)
    last_native = max(native, default=-1)
    writers = [i for i, word in enumerate(words) if _CITED_WRITING_RE.fullmatch(word)]
    # A governing "פירש לעיל" stays a commentary even if its later text quotes Gemara.
    if any(i > last_native for i in writers):
        return False
    tosafot = getattr(source_ref.index, 'title', '').startswith('Tosafot on ')
    from sefaria.model.linker.ref_part import LEAF_TRIE_ENTRY
    trie = linker._ref_resolver.get_ref_part_title_trie()
    own = {target.index.title, 'Mishnah ' + target.index.title}
    def attribution(i):
        # "רש״י אמר לעיל" could still refer to Rashi's own discussion.
        # Require an intervening explanation before its Gemara predicate,
        # or the explicitly subordinate "ר״ת מפרש דלקמן ..." form.
        explanation = (last_native - i >= 3 and any(j < last_native for j in writers))
        subordinate = (i + 1 == last_native and words[last_native] == 'מפרש'
                       and raw_anchor.startswith('ד')
                       and bool(re.fullmatch(r'ו?[בכלמד]?ר"ת', words[i])))
        return tosafot and (explanation or subordinate)
    for i, word in enumerate(words):
        # Do not turn a named but otherwise unknown writer into a discourse abbreviation.
        governed = i > 0 and re.fullmatch(
            r'ו?(?:עיין|ראה|כתב|פירש|ספר|הרב|הגאון|רבנו|רבינו)', words[i - 1])
        variants = {word[n:] for n in range(3)
                    if n == 0 or re.fullmatch(r'[ובכלמדשה]{1,2}', word[:n])}
        common = bool(variants & _RELATIVE_DISCOURSE_ABBREVIATIONS) and not governed
        rt = bool(re.fullmatch(r'ו?[בכלמד]?ר"ת', word))
        rashi = bool(re.fullmatch(r'ו?[בכלמד]?רש"י', word))
        kuntres = bool(re.fullmatch(r'ו?[בכלמה]?(?:קונטרס|קונט\')', word))
        if rt or rashi or kuntres:
            if not attribution(i):
                return False
            continue
        if re.search(r'[א-ת]["\'][א-ת]', word) and not common:
            letters = re.sub(r'[^א-ת]', '', word)
            if governed or not _is_canonical_numeral(letters):
                return False
        if word in ('ספר', 'בספר', 'חידושי', 'בחידושי'):
            return False
        if _chapter_marker(word):
            if not any(chapter.overlaps(target) for j in range(i + 1, len(words) + 1)
                       for chapter in named_chapters(' '.join(words[i:j]), target)):
                return False
        # A discourse abbreviation is not enough to dismiss a complete known
        # commentary alias (e.g. מ״מ / ע״י). Those ambiguous readings stay out.
        for length in (1, 2, 3):
            if i + length > len(words):
                break
            node, _ = trie.get_continuations(' '.join(words[i:i + length]), allow_partial=False)
            if node is None or LEAF_TRIE_ENTRY not in node:
                continue
            for named_node in node[LEAF_TRIE_ENTRY]:
                try:
                    named = named_node.ref()
                except Exception:
                    continue
                if getattr(named.index, 'dependence', None) or getattr(named.index, 'base_text_titles', None):
                    title = named.index.title
                    if attribution(i) and (title.startswith('Rashi on ') or title == 'Sefer HaYashar'):
                        continue
                    return False
                if _top_category(named.index) not in ('Talmud', 'Mishnah', 'Tosefta') or named.sections:
                    continue  # a bare sage/chapter alias is not a work locator
                if named.index.title not in own:
                    # כר׳ + a sage's name is "like Rabbi ...", not the
                    # title-trie's coincidentally identical Keritot abbreviation.
                    if (word in ("כר'", "וכר'") and i + 1 < len(words)
                            and words[i + 1] in {
                                'ישמעאל', 'אבא', 'אחא', 'עקיבא', 'מאיר', 'יהודה', 'יוסי',
                                'שמעון', 'אליעזר', 'אלעזר', 'יהושע', 'יוחנן', 'חנינא'}):
                        continue
                    # The resolver's permissive title trie also reads כפרה as
                    # Mishnah Parah, הכלים as Kelim and כר׳ as Keritot. Require
                    # an actual tractate name, with only a locator prefix.
                    title = named.index.get_title('he')
                    title = re.sub(r'^(?:משנה|תוספתא)\s+', '', title)
                    phrase = ' '.join(words[i:i + length])
                    base = re.sub(r'^(?:Mishnah|Tosefta) ', '', named.index.title)
                    aliases = {title}
                    for canonical, book_aliases, _ in _bavli_chapter_books():
                        if canonical == base:
                            aliases.update(book_aliases)
                    if not any(re.fullmatch(rf'ו?[במדל]?{re.escape(_plain_quotes(alias))}', phrase)
                               for alias in aliases):
                        continue
                    # "משבת קפריש" describes the Shabbat prohibition, not a tractate.
                    if named.index.title in ('Shabbat', 'Mishnah Shabbat') and any(
                            w in ('קפריש', 'העלם', 'אמלאכות', 'מלאכה', 'אשבת')
                            for w in words[max(0, i - 2):i + 3]):
                        continue
                    return False
    return True


def explicit_relative_address(anchor, source_ref, ref_factory):
    """The printed own-tractate address; this alone proves no cited work."""
    if not getattr(getattr(source_ref, 'index', None), 'title', '').startswith('Tosafot on '):
        return None
    anchor = _NIQQUD_RE.sub('', _plain_quotes(anchor))
    recovered = (own_tractate_relative_ref(anchor, source_ref, ref_factory)
                 or untitled_relative_ref(anchor, source_ref, ref_factory)
                 or chapter_located_relative_ref(anchor, source_ref, ref_factory))
    if recovered is None:
        # "בפ\"ב לקמן (דף יז:)": the chapter precedes the relative word,
        # rather than sitting inside its parenthesis.
        at = re.search(r'(?<![א-ת])[וד]?(?:לקמן|לעיל|להלן)(?![א-ת])', anchor)
        if at is None or at.start() == 0:
            return None
        lead = anchor[:at.start()].strip()
        recovered = untitled_relative_ref(anchor[at.start():], source_ref, ref_factory)
        if recovered is None or not _chapter_marker(lead) or not any(
                chapter.overlaps(recovered[0]) for chapter in named_chapters(lead, recovered[0])):
            return None
    return recovered


def explicit_own_relative_ref(linker, rr, content, start, end, source_ref, ref_factory):
    """One printed relative amud with independently checked source and ownership."""
    recovered = explicit_relative_address(content[start:end], source_ref, ref_factory)
    if recovered is None:
        return None
    target, direction = recovered
    if any(not _in_tractate(candidate, target.index.title)
           or list(candidate.sections[:1]) != list(target.sections)
           or list(candidate.toSections[:1]) != list(target.toSections)
           for candidate in candidate_refs(rr)):
        return None
    if commentary_qualifier_after(content, end):
        return own_relative_dh_ref(linker, rr, content, start, end, source_ref,
                                   target, direction, ref_factory)
    if not relative_clause_is_clear(linker, rr, content, start, end, source_ref, target):
        return None
    return target, direction


def own_relative_dh_ref(linker, rr, content, start, end, source_ref, target, direction, ref_factory):
    """A DH outside a relative anchor identifies Tosafot, never bare Gemara.

    Require a candidate in the citing commentary at that exact printed amud,
    no other work in the preceding clause, and one unique matching DH.
    """
    if not any(candidate.index.title == source_ref.index.title for candidate in candidate_refs(rr)):
        return None
    if clause_names_elsewhere(linker, clause_before(content, start), target, target.index.title):
        return None
    tail = _plain_quotes(_QUOTE_HTML_TAG_RE.sub(' ', content[end:])).lstrip(' )]([')
    match = re.match(r'ב?ד["\']ה\s+([א-ת ]+?)\s+(?:וכו[\'"׳״]|וגו[\'"׳״])', tail)
    if match is None:
        return None
    dh = hebrew_quote_words(match[1])
    if not 1 <= len(dh) <= 5:
        return None
    address = source_ref.index.title + target.normal()[len(target.index.title):]
    container = ref_factory(address)
    found = []
    for segment in container.all_segment_refs():
        text = segment.text('he').as_string()
        headings = []
        for bold in re.finditer(r'<b>(.*?)</b>', text, re.S):
            if hebrew_quote_words(text[:bold.start()]):
                break
            heading = hebrew_quote_words(bold[1])
            if not heading:
                continue
            headings.append(heading)
            break
        # Sefaria's Tosafot text often removes bold tags but retains the short
        # DH as its first period-terminated sentence. Never search the body.
        plain = _QUOTE_HTML_TAG_RE.sub(' ', text)
        if not headings and '.' in plain and '<' not in plain and '>' not in plain:
            heading = hebrew_quote_words(plain.split('.', 1)[0])
            if 1 <= len(heading) <= 12:
                headings.append(heading)
        if headings and headings[0][:len(dh)] == dh:
            found.append(segment)
    return (found[0], direction) if len(found) == 1 else None


def clause_names_elsewhere(linker, words: list[str], target, tractate: str) -> bool:
    """Whether these words, ending just before target's address, may place it elsewhere.

    They do when they name another tractate, or a chapter that does not contain target:
    a chapter phrase ("בפ' הוציאו לו את הכף") must open with the name of one of the
    tractate's own chapters that contains it, and a chapter's bare name counts only right
    before the address, since many are also a sage's name ("ר' ישמעאל").
    """
    from sefaria.model.linker.ref_part import LEAF_TRIE_ENTRY

    trie = linker._ref_resolver.get_ref_part_title_trie()
    if len(words) > 64:
        return True  # do not silently forget a work named earlier in a long clause
    own = {tractate, f"Mishnah {tractate}"}
    words = [_NIQQUD_RE.sub("", word) for word in words]
    if any(re.search(r"(?:כתבתי|כתבנו|פירשתי|פירשנו|ביארתי|ביארנו)(?![א-ת])", word)
           for word in words):
        return True  # an author's self-reference is not the underlying Gemara
    if any(re.match(r"ו?[במ]?(?:פירש|פירוש|כתב|כתבו|חידושי|ספר)(?![א-ת])", word) for word in words):
        return True
    for i, word in enumerate(words):
        if _CHAPTER_LEAD_RE.match(word) and not any(
                chapter.overlaps(target)
                for j in range(i + 1, len(words) + 1)
                for chapter in named_chapters(" ".join(words[i:j]), target)):
            return True
        for n in (1, 2, 3):
            if i + n > len(words):
                break
            titles, _ = trie.get_continuations(" ".join(words[i:i + n]), allow_partial=False)
            if titles is None:
                if n == 1 and re.search(r"[א-ת][\"'][א-ת]", word):
                    letters = re.sub(r"[^א-ת]", "", word)
                    if not _is_canonical_numeral(letters):
                        return True
                continue
            if LEAF_TRIE_ENTRY not in titles:
                if n == 1 and not re.fullmatch(
                        r"ו?[במ]?(?:גמרא|גמ['\"]?|מתני['\"]?|מתניתין|פירקין|פרקין)", word):
                    return True  # e.g. רש"י: an unfinished commentary title
                continue
            for node in titles[LEAF_TRIE_ENTRY]:
                try:
                    named = node.ref()
                except Exception:
                    continue
                if getattr(named.index, "dependence", None) or getattr(named.index, "base_text_titles", None):
                    return True
                if _top_category(named.index) not in ("Talmud", "Mishnah", "Tosefta"):
                    continue
                if not named.sections:
                    if named.index.title not in own:
                        return True
                elif i + n == len(words) and not (
                        named.index.title == tractate and named.overlaps(target)):
                    return True
    return False


_IBID_VERSE_RE = re.compile(
    rf"(?P<ibids>שם\s+(?:שם\s+)?)(?P<first>{_UNTITLED_DAF})"
    rf"(?:\s+(?P<second>{_UNTITLED_DAF}))?")


def quoted_biblical_ibid_ref(linker, rr, content, start, end, ref_factory):
    """Identify a fully addressed biblical quote without inheriting lost history.

    A four-word phrase must occur exactly once in the entire Tanakh, in the
    printed verse. Two printed numbers supply both chapter and verse. With just
    a verse number, the resolver must independently offer that exact address;
    no chapter is invented. Different adjoining quotations, short quotations,
    commentary candidates, ranges and contradictory numbers all fail closed.
    This evidence does not revive or modify Sefaria's ibid history.
    """
    if ref_factory is None or not has_raw_part_type(rr, "IBID"):
        return None
    anchor = _NIQQUD_RE.sub("", _plain_quotes(_bare_anchor(content[start:end])))
    match = _IBID_VERSE_RE.fullmatch(anchor)
    if match is None:
        return None
    tokens = [re.sub(r"[^א-ת]", "", match[n]) for n in ("first", "second") if match[n]]
    if (not all(_is_canonical_numeral(t) for t in tokens)
            or (match["ibids"].split().count("שם") == 2 and len(tokens) != 1)):
        return None
    numbers = [_numeral(t) for t in tokens]
    refs = [] if getattr(rr, "resolution_failed", False) else candidate_refs(rr)
    if any(_top_category(r.index) != "Tanakh" or getattr(r.index, "dependence", None)
           for r in refs):
        return None
    if len(numbers) == 1 and not refs:
        return None  # the omitted chapter cannot be inferred from the words alone
    windows = adjoining_quote_words(content, start, end)
    quotes = {" " + " ".join(words[i:i + 4]) + " "
              for words in windows for i in range(len(words) - 3)}
    if not quotes:
        return None
    distinctive = {}
    for quote in quotes:
        found = []
        for title, corpus in _tanakh_quote_books():
            at = corpus.find(quote)
            if at >= 0:
                found.append(title)
                if corpus.find(quote, at + 1) >= 0:
                    found.append(title)
        if len(found) == 1:
            distinctive.setdefault(found[0], set()).add(quote)
    if len(distinctive) != 1:
        return None
    title, phrases = next(iter(distinctive.items()))
    if len(numbers) == 2:
        try:
            refs = [ref_factory(f"{title} {numbers[0]}:{numbers[1]}")]
        except Exception:
            return None
    found = {}
    for ref in refs:
        if (ref.index.title != title or len(ref.sections) != 2
                or list(ref.sections) != list(ref.toSections)
                or list(ref.sections[-len(numbers):]) != numbers):
            continue
        words = unquoted_clause_words(clause_before(content, start), phrases)
        if clause_names_elsewhere(linker, words, ref, title):
            continue
        verse = " " + " ".join(_hebrew_ref_words(ref.normal())) + " "
        if any(phrase in verse for phrase in phrases):
            found[ref.normal()] = ref
    return next(iter(found.values())) if len(found) == 1 else None


def abbreviated_biblical_ibid_ref(linker, rr, content, start, end, ref_factory):
    """Resolve a short, explicitly abbreviated double-ibid quotation.

    Unlike the general four-word evidence, the ENTIRE phrase must be globally
    unique, immediately followed by וגו׳/וגומר, and contained in one existing
    base-Bible candidate with the printed verse number. No chapter is invented.
    """
    if (ref_factory is None or not has_raw_part_type(rr, 'IBID')
            or has_raw_part_type(rr, 'NAMED') or has_raw_part_type(rr, 'DH')):
        return None
    anchor = _NIQQUD_RE.sub('', _plain_quotes(_bare_anchor(content[start:end])))
    match = _IBID_VERSE_RE.fullmatch(anchor)
    if (match is None or match['ibids'].split() != ['שם', 'שם']
            or match['second'] is not None):
        return None
    numeral = re.sub(r'[^א-ת]', '', match['first'])
    if not _is_canonical_numeral(numeral):
        return None
    refs = candidate_refs(rr)
    if not refs or any(_top_category(r.index) != 'Tanakh'
                       or getattr(r.index, 'dependence', None) for r in refs):
        return None
    prefix = _QUOTE_HTML_TAG_RE.sub(' ', content[:start]).rstrip(' ([')
    if '<' in prefix or '>' in prefix:
        return None
    ellipsis = re.search(r'\s+וג(?:ו[\'"׳״]|ומר)\s*[,;]?\s*$', prefix)
    if ellipsis is None:
        return None
    before = prefix[:ellipsis.start()]
    cut = max(before.rfind(mark) for mark in ',.;:!?()[]')
    intros = list(_QUOTE_INTRO_RE.finditer(before))
    if intros:
        cut = max(cut, intros[-1].end() - 1)
    quoted = before[cut + 1:]
    words = hebrew_quote_words(quoted)
    if not 2 <= len(words) <= 8:
        return None
    # A conventional expansion in Aggadat Bereshit: "את יד ימינו" for
    # Genesis48:14's "את ימינו". This exact expansion is the sole variant;
    # arbitrary word deletion could erase a negation or change the subject.
    variants = {words}
    if words == ('וישלח', 'ישראל', 'את', 'יד', 'ימינו'):
        variants.add(('וישלח', 'ישראל', 'את', 'ימינו'))
    found = {}
    for variant in variants:
        phrase = ' ' + ' '.join(variant) + ' '
        hits = []
        for title, corpus in _tanakh_quote_books():
            at = corpus.find(phrase)
            if at >= 0:
                hits.append(title)
                if corpus.find(phrase, at + 1) >= 0:
                    hits.append(title)
        if len(hits) != 1:
            continue
        for ref in refs:
            if (ref.index.title != hits[0] or len(ref.sections) != 2
                    or list(ref.sections) != list(ref.toSections)
                    or ref.sections[-1] != _numeral(numeral)):
                continue
            if phrase not in ' ' + ' '.join(_hebrew_ref_words(ref.normal())) + ' ':
                continue
            # Remove only the exact positively identified quote, retaining all
            # author/work attributions that precede it in the same clause.
            original = ' ' + ' '.join(words) + ' '
            unquoted = unquoted_clause_words(clause_before(content, start), {original})
            if not clause_names_elsewhere(linker, unquoted, ref, ref.index.title):
                found[ref.normal()] = ref
    return next(iter(found.values())) if len(found) == 1 else None


def contextual_ibid_ref(linker, rr, content, start, end, antecedent, ref_factory, *, gap=None):
    """Recover only an explicit address using the immediately preceding validated target.

    No failed intervening citation is skipped. This isolated context does not enter
    Sefaria's resolver history. Biblical inheritance additionally needs matching
    quoted words, so a printed wrong verse or prose misclassified as ibid stays out.
    """
    if ref_factory is None or not has_raw_part_type(rr, "IBID"):
        return None
    anchor = _NIQQUD_RE.sub("", _plain_quotes(_bare_anchor(content[start:end])))
    if _bavli_tractate_itself(antecedent.index):
        if not re.match(r"^(?:שם|התם)\s", anchor):
            return None
        target = untitled_daf_anchor(anchor, antecedent, ref_factory)
        if target is None:
            return None
        words = _plain_quotes(gap).split() if gap is not None else clause_before(content, start)
        return None if clause_names_elsewhere(linker, words, target, target.index.title) else target
    if (_top_category(antecedent.index) != "Tanakh"
            or getattr(antecedent.index, "dependence", None)
            or len(antecedent.sections) != 2
            or list(antecedent.sections) != list(antecedent.toSections)):
        return None
    match = _IBID_VERSE_RE.fullmatch(anchor)
    if match is None:
        return None
    tokens = [re.sub(r"[^א-ת]", "", match[name]) for name in ("first", "second") if match[name]]
    if not all(_is_canonical_numeral(token) for token in tokens):
        return None
    if match["ibids"].split().count("שם") == 2 and len(tokens) != 1:
        return None
    sections = ([_numeral(t) for t in tokens] if len(tokens) == 2
                else [antecedent.sections[0], _numeral(tokens[0])])
    try:
        target = ref_factory(f"{antecedent.index.title} {sections[0]}:{sections[1]}")
    except Exception:
        return None
    if (list(target.sections) != sections or list(target.toSections) != sections
            or target.index.title != antecedent.index.title):
        return None
    return target if quoted_tanakh_address(target, content, start, end) else None


# A chapter phrase with the relative word before it or inside the parenthesis:
# "בספ"ב (לקמן דף יט:)", "לקמן פ"ב (דף טז:)", "לקמן בפירקין (דף ה.)".
_CHAPTER_RELATIVE_RE = re.compile(
    rf"(?P<before>{_RELATIVE_WORD})?(?P<chapter>[^()]+?)\s*\(\s*(?P<inside>{_RELATIVE_WORD})?{_DAF_ADDRESS}")
# "בפירקין": the chapter the citing line is in.
_OWN_CHAPTER_RE = re.compile(r"ו?[בד]?(?:פירקין|פרקין)")


def chapter_located_relative_ref(anchor_text: str, source_ref, ref_factory):
    """The (Ref, direction) of "בספ"ב (לקמן דף יט:)" cited from within a tractate or its commentary.

    The relative word, the chapter and the one address must all agree: the chapter must be
    the source's own one ("בפירקין") or one the phrase names in Sefaria's own chapter terms,
    and it must contain the address (Otzaria/otzaria#3450).
    """
    match = _CHAPTER_RELATIVE_RE.fullmatch(_bare_anchor(anchor_text))
    if match is None or bool(match["before"]) == bool(match["inside"]):
        return None
    if not _is_talmud_source(source_ref, ref_factory):
        return None
    tractates = _own_tractates(source_ref, ref_factory)
    if len(tractates) != 1:
        return None
    side_word = _relative_side(match["before"] or match["inside"])
    found = _on_stated_side(_address_ref(match, tractates[0], ref_factory), source_ref, side_word)
    if found is None:
        return None
    target = found[0]
    chapter = _plain_quotes(match["chapter"])
    if _OWN_CHAPTER_RE.fullmatch(chapter):
        amud = source_ref.sections[0]
        if source_ref.toSections[0] != amud:
            return None
        source = _amud_ref(tractates[0], (amud + 1) // 2, "א" if amud % 2 else "ב", ref_factory)
        chapters = [whole for whole, _names in _book_chapters(tractates[0])
                    if source is not None and whole.overlaps(source)]
    else:
        chapters = named_chapters(chapter, target)
    return found if any(whole.overlaps(target) for whole in chapters) else None


def _address_ref(match, tractate: str, ref_factory):
    daf = match["daf"]
    letters = re.sub(r"[^א-ת]", "", daf)
    # "ע"ב." may be amud b, not daf 72; "פ"ב." is a chapter; "פנ:" is a typo, not daf 130.
    if (re.fullmatch(r"ע[\"״'׳][אב]", daf) or _CHAPTER_ABBREVIATION_RE.match(daf)
            or not _is_canonical_numeral(letters)):
        return None
    # "ד' ע"ב" may be "דף ע"ב" as well as daf 4b.
    if letters == "ד" and not (match["prefix"] or "").startswith("דף") and (
            daf != "ד" or match["amud"]):
        return None
    side = _PUNCT_SIDE[match["mark"]] if match["mark"] else match["comma"] or match["amud"]
    return _amud_ref(tractate, _numeral(letters), side, ref_factory)


# "(תהלים יח' לא')": a title, then a chapter and an optional verse.
_GERESH = "'׳‘’"
# The other number may use Sefaria's standard gershayim spelling, including two
# apostrophes: "תהלים י\"ח לא'", "תהלים יח' ל״א", or "תהלים י''ח לא'".
_GERESH_NUMERAL = rf"(?:[א-ת]{{1,3}}[{_GERESH}]?|[א-ת]{{1,2}}(?:[\"״“”]|'')[א-ת])"
_GERESH_CITATION = (r"(?P<title>[א-ת][א-ת\"״{g} ]*{lazy})\s+(?P<chapter>{number})"
                    r"(?:\s*,?\s+(?P<verse>{number}))?")
# The greedy reading keeps "מלכים א'" whole when "א'" is not a chapter.
_GERESH_CITATION_RES = [re.compile(_GERESH_CITATION.format(g=_GERESH, number=_GERESH_NUMERAL, lazy=lazy))
                        for lazy in ("?", "")]
_MULTI_LETTER_GERESH_RE = re.compile(rf"[א-ת]{{2,3}}[{_GERESH}]")


def geresh_numeral_ref(anchor_text: str, ref_factory):
    """The Ref of "תהלים יח' לא'", whose geresh after a multi-letter number fails Sefaria.

    Sefaria reads a geresh only after a single letter. Only a whole anchor of a Tanakh
    title and canonical numbers is rebuilt, into a Ref with exactly those sections.
    """
    if ref_factory is None or not _MULTI_LETTER_GERESH_RE.search(anchor_text):
        return None
    anchor = _bare_anchor(anchor_text)
    for regex in _GERESH_CITATION_RES:
        match = regex.fullmatch(anchor)
        tokens = [token for token in (match["chapter"], match["verse"]) if token] if match else []
        if not any(_MULTI_LETTER_GERESH_RE.fullmatch(token) for token in tokens):
            continue
        letters = [re.sub(r"[^א-ת]", "", token) for token in tokens]
        if not all(_is_canonical_numeral(token) for token in letters):
            continue
        try:
            title = re.sub(rf"(?<= [א-ת])[{_GERESH}]$", "", match["title"].strip())
            ref = ref_factory(" ".join([title, *letters]))
        except Exception:
            continue
        # A Talmud title would read "יב' ב'" as a Mishnah; Ref never checks the verse exists.
        if (ref.primary_category == "Tanakh"
                and list(ref.sections) == [_numeral(token) for token in letters]
                and not ref.is_empty()):
            return ref
    return None


def _is_canonical_numeral(letters: str) -> bool:
    value = _numeral(letters)
    if value is None or value >= 400:
        return False
    hundreds, rest = divmod(value, 100)
    tens, units = divmod(rest, 10)
    spelled = {15: "טו", 16: "טז"}.get(rest, " יכלמנסעפצ"[tens] + " אבגדהוזחט"[units])
    return letters == (" קרש"[hundreds] + spelled).replace(" ", "")


def _is_talmud_source(source_ref, ref_factory) -> bool:
    return (source_ref is not None and ref_factory is not None
            and bool(source_ref.is_talmud()) and bool(source_ref.sections))


def _own_tractates(source_ref, ref_factory) -> list[str]:
    return (_commented_tractates(source_ref, ref_factory)
            or _bavli_tractate_itself(source_ref.index))


def _commented_tractates(source_ref, ref_factory) -> list[str]:
    # A commentary "X on <tractate>" is laid out by the tractate's own amudim; one on
    # Rif or Zohar is not.
    own = source_ref.index.title
    return [t for t in getattr(source_ref.index, "base_text_titles", None) or ()
            if own.endswith(f" on {t}") and _is_bavli_tractate_title(t, ref_factory)]


def _is_bavli_tractate_title(title: str, ref_factory) -> bool:
    try:
        return bool(_bavli_tractate_itself(ref_factory(title).index))
    except Exception:
        return False


def _bavli_tractate_itself(index) -> list[str]:
    # Only a tractate (["Talmud", "Bavli", "Seder ..."]) qualifies; a work such as Rif
    # is paged by its own dapim.
    categories = list(getattr(index, "categories", None) or ())
    if (categories[:2] == ["Talmud", "Bavli"] and len(categories) == 3
            and categories[2].startswith("Seder ")
            and getattr(index, "dependence", None) is None
            and not getattr(index, "base_text_titles", None)):
        return [index.title]
    return []


_HEADING_LINE_RE = re.compile(r"\s*<h([1-6])[^>]*>(.*?)</h\1>\s*", re.S)
# A heading that spells an address: "דף כג:", "דף כג ע"ב", "דף כג עמוד ב", "צ' ע"א".
_HEADING_ADDRESS_RE = re.compile(
    rf"(?P<prefix>דף\s+)?(?P<daf>{_UNTITLED_DAF})"
    r"(?:\s*(?P<mark>[.:])|\s+ע[\"״'׳](?P<amud>[אב])|\s+עמוד\s+(?P<amud_word>[אב]))?")
_HEADING_AMUD_RE = re.compile(r"(?:ע[\"״'׳]|עמוד\s+)(?P<amud>[אב])")


def talmud_positions(bk, lines, ref_factory) -> dict:
    """The Bavli amud of each line of a non-Sefaria "X על <tractate>", from its daf headings.

    Sefaria cannot parse such a book's own context ref, so a relative daf citation in
    it has no position. A line is placed by the nearest "דף ..." heading above it only
    when that heading spells one amud and no other heading in its stack disagrees.
    """
    # KSK quotes other rishonim, whose "לקמן" often means their own tractate.
    if not placed_by_daf_headings(bk) or ref_factory is None:
        return {}
    tractate = _commented_bavli_tractate(bk.canonical_he_title, ref_factory)
    if tractate is None:
        return {}
    refs = {}

    def amud_ref(address):
        if address not in refs:
            refs[address] = _amud_ref(tractate, address[0], address[1], ref_factory)
        return refs[address]

    positions = {}
    stack = {}
    ref = None
    for line_index, content, _context_ref in lines:
        heading = _HEADING_LINE_RE.fullmatch(content or "")
        if heading:
            level = int(heading.group(1))
            stack = {lvl: text for lvl, text in stack.items() if lvl < level}
            stack[level] = re.sub(r"\s+", " ", _HTML_TAG_RE.sub("", heading.group(2))).strip()
            address = _stack_address(stack)
            ref = amud_ref(address) if address is not None else None
        if ref is not None:
            positions[line_index] = ref
    return positions


def _commented_bavli_tractate(title: str, ref_factory):
    parts = (title or "").split(" על ")
    found = set()
    for i in range(1, len(parts)):
        he = " על ".join(parts[i:])
        try:
            ref = ref_factory(he)
        except Exception:
            continue
        if not ref.sections and ref.index.get_title("he") == he:
            found.update(_bavli_tractate_itself(ref.index))
    return found.pop() if len(found) == 1 else None


def _stack_address(stack):
    """The (daf, amud) of the innermost "דף" heading, or None on any doubt."""
    daf_levels = [lvl for lvl, text in stack.items() if text.startswith("דף")]
    if not daf_levels:
        return None
    nearest = max(daf_levels)
    address = _heading_address(stack[nearest], canonical=True)
    if address is None or address[1] is None:
        return None
    for lvl, text in stack.items():
        if lvl == nearest:
            continue
        other = _heading_address(text, canonical=False)
        if other is None:
            amud_only = _HEADING_AMUD_RE.fullmatch(text)
            other = (address[0], amud_only["amud"]) if amud_only else None
        # Any other "דף" heading must spell this same amud; others only when they spell one.
        if (other is None and text.startswith("דף")) or (other is not None and other != address):
            return None
    return address


def _heading_address(text: str, *, canonical: bool):
    match = _HEADING_ADDRESS_RE.fullmatch(text)
    if match is None:
        return None
    letters = re.sub(r"[^א-ת]", "", match["daf"])
    valid = _is_canonical_numeral(letters) if canonical else _numeral(letters) is not None
    if not valid:
        return None
    amud = (_PUNCT_SIDE[match["mark"]] if match["mark"]
            else match["amud"] or match["amud_word"])
    # A bare "א" is a section number; only "דף א" or "א." spells a daf.
    if amud is None and not match["prefix"]:
        return None
    return _numeral(letters), amud


def _on_stated_side(target, source_ref, side_word: str):
    if target is None:
        return None
    amud = target.sections[0]
    if side_word == "above" and not amud < source_ref.sections[0]:
        return None
    if side_word == "below" and not amud > source_ref.toSections[0]:
        return None
    return target, side_word


# Abbreviations the linker reads as a book name: מ״א (Magen Avraham) as I Kings and
# עמ׳ (a page number) as Amos.
_NON_CITATION_ABBREVIATIONS = (
    (re.compile(r"^\W*מ[\"'״׳]א(?:\W*$|\s+(?:סי|ס[\"'״׳]ק|סק))"), "I Kings"),
    (re.compile(r"^\W*עמ[\"'׳]"), "Amos"),
)
_MISHNAH_CHAPTER_MISREAD_RE = re.compile(r"פ[\"'״׳]ק\s+ד?עירובין")


def is_abbreviation_misread(target_ref: str, anchor_text: str) -> bool:
    return any(
        (target_ref == book or target_ref.startswith(book + " ")) and pattern.search(anchor_text)
        for pattern, book in _NON_CITATION_ABBREVIATIONS
    )


def is_mishnah_chapter_misread(target_ref: str, anchor_text: str) -> bool:
    """Reject ``פ\"ק דעירובין`` when the linker chose Mishnah Eruvin.

    The phrase ordinarily names the first Bavli chapter; without an explicit daf
    we cannot safely manufacture its full range, but retaining a link into the
    Mishnah is demonstrably wrong (#1348).  An anchor that itself says ``משנה``
    is left alone because that reading is explicit.
    """
    return (
        target_ref.startswith("Mishnah Eruvin")
        and "משנה" not in anchor_text
        and bool(_MISHNAH_CHAPTER_MISREAD_RE.search(anchor_text))
    )


# Availability belongs to the final target, including legacy and relative paths.
# Cache by type and normal form so transport batches do not repeat Mongo queries.
_TARGET_TEXT_CACHE = collections.OrderedDict()
_EXPLICIT_TARGET_RANGE_RE = re.compile(r" \d+[ab]?(?::\d+[ab]?)*-\d+[ab]?(?::\d+[ab]?)*$")


def target_has_text(ref) -> bool:
    key = type(ref), ref.normal()
    if key in _TARGET_TEXT_CACHE:
        _TARGET_TEXT_CACHE.move_to_end(key)
        return _TARGET_TEXT_CACHE[key]
    # is_empty() on an out-of-bounds range can report False: validate endpoints
    # independently instead of treating any text in its parent as range evidence.
    try:
        # A resolver-created virtual Ref can print a normal form that its schema
        # cannot parse back (four Nefesh David addresses in the published payload).
        # Schema-backed refs must round-trip before exporting that canonical key.
        canonical = True
        if getattr(ref, "index_node", None) is not None:
            parsed = type(ref)(key[1])
            canonical = parsed.normal() == key[1]
            ref = parsed
        available = canonical and not ref.is_empty()
        if available and ref.is_range():
            start_exists = not ref.starting_ref().is_empty()
            end_exists = not ref.ending_ref().is_empty()
            # A canonical whole daf ("Ramban on Shabbat 122") implicitly spans
            # both amudim; one may have no commentary. It is still a valid scope
            # when the other has text. Explicit numeric ranges require both ends.
            available = ((start_exists and end_exists) if _EXPLICIT_TARGET_RANGE_RE.search(key[1])
                         else (start_exists or end_exists))
    except Exception as error:
        # Some upstream alt-structure nodes have an invalid reversed boundary.
        # That is an unusable citation; database/transport failures still fail the book.
        if not any(cls.__name__ == "InputError" and cls.__module__ == "sefaria.system.exceptions"
                   for cls in type(error).__mro__):
            raise
        available = False
    _TARGET_TEXT_CACHE[key] = available
    if len(_TARGET_TEXT_CACHE) > 65_536:
        _TARGET_TEXT_CACHE.popitem(last=False)
    return available


@functools.lru_cache(maxsize=4096)
def _chapter_content_bounds(normal: str) -> str | None:
    from sefaria.model import Ref

    scope = Ref(normal)
    leaves = scope.all_segment_refs()
    if not leaves or len(leaves) > 10_000:
        return None
    available = [leaf for leaf in leaves if scope.contains(leaf) and target_has_text(leaf)]
    if not available:
        return None
    result = available[0].to(available[-1])
    # This is strictly an intersection with the declared chapter, never a move
    # to the first line of its parent or to text after the requested range.
    if not scope.contains(result) or not target_has_text(result):
        return None
    return result.normal()


def _chapter_marker(text):
    return (_CHAPTER_LEAD_RE.match(text) or re.match(
        r'ו?[במ]?(?:[רס]?פ["״][א-ת]+|ריש|סוף|שלהי)(?![א-ת])', text))


@functools.lru_cache(maxsize=1)
def _bavli_chapter_books():
    from sefaria.model import library
    from sefaria.model.schema import NonUniqueTerm

    books = []
    for index in library.get_indexes_in_category("Bavli", include_dependant=False, full_records=True):
        if not _bavli_tractate_itself(index):
            continue
        aliases = set(index.nodes.all_node_titles("he"))
        # Linker terms also carry the printed abbreviations (ב"מ, ר"ה, גטין).
        # Use only templates consisting of one term that identifies this book.
        for template in index.nodes.serialize().get("match_templates", []):
            slugs = template.get("term_slugs", [])
            if len(slugs) == 1:
                term = NonUniqueTerm.init(slugs[0])
                if term is not None:
                    aliases.update(term.get_titles("he"))
        books.append((index.title, tuple(sorted(aliases)), _book_chapters(index.title)))
    return tuple(books)


@functools.lru_cache(maxsize=1)
def _chapter_alias_works():
    aliases = collections.defaultdict(set)
    for title, _, chapters in _bavli_chapter_books():
        for _, names in chapters:
            for name in names:
                words = name.split()
                # Printed chapter names can abbreviate a longer canonical title:
                # Gittin's "הזורק גט" also competes with Shabbat's "הזורק".
                for end in range(1, len(words) + 1):
                    aliases[" ".join(words[:end])].add(title)
    return aliases


def chapter_book_is_proven(ref, anchor, tail, source_ref=None) -> bool:
    """A chapter ordinal/shared name does not independently identify its tractate.

    A tractate just outside the NER span still belongs to the address ("פרק שני
    דגיטין"). Otherwise require a chapter name unique across canonical Bavli
    tractates, or a relative chapter in the citing work's own tractate.
    """
    books = _bavli_chapter_books()
    bases = set(getattr(ref.index, "base_text_titles", None) or ())
    target = {title for title, _, _ in books if title in bases}
    if len(target) != 1:
        return False
    def plain(text):
        return _NIQQUD_RE.sub("", _plain_quotes(_QUOTE_HTML_TAG_RE.sub(" ", text)))
    text = plain(anchor)
    after = plain(tail).lstrip(" )] ")
    # Only the immediately following ד<title> (or במסכת title) can extend
    # the address. Do not mistake a later comparison to another work for it.
    explicit = set()
    for title, aliases, _ in books:
        for alias in aliases:
            escaped = re.escape(plain(alias))
            if (re.search(rf'(?<![א-ת])(?:ו?[במדל])?{escaped}(?![א-ת])', text)
                    or re.match(rf'(?:ד|במסכת\s+){escaped}(?![א-ת])', after)):
                explicit.add(title)
    if explicit:
        return explicit == target
    # A source's printed relative locator can refer to its own base tractate;
    # an arbitrary resolver ibid cannot manufacture this evidence.
    if (source_ref is not None and bases & set(getattr(source_ref.index, "base_text_titles", None) or ())
            and source_ref.is_talmud() and source_ref.sections and ref.sections):
        if (re.search(r'(?<![א-ת])ו?לעיל(?![א-ת])', text)
                and ref.toSections[0] < source_ref.sections[0]):
            return True
        if (re.search(r'(?<![א-ת])ו?(?:לקמן|להלן)(?![א-ת])', text)
                and ref.sections[0] > source_ref.toSections[0]):
            return True
    words = [word.strip("()[],:;.") for word in text.split()]
    anchor_words = len(words)
    words += [word.strip("()[],:;.") for word in after.split()[:3]]
    names = set().union(*(chapter_lead_names(" ".join(words[i:j]))
                          for i in range(min(anchor_words, 16))
                          for j in range(i + 1, min(len(words), i + 8) + 1)))
    aliases = _chapter_alias_works()
    matches = [(len(name.split()), title) for name in names for title in aliases.get(name, ())]
    if not matches:
        return False
    longest = max(length for length, _ in matches)
    return {title for length, title in matches if length == longest} == target


def named_chapter_content_ref(ref, rr, anchor, ref_factory, *, content=None, end=None, source_ref=None):
    """Repair empty boundaries only for an independently named canonical chapter.

    Sefaria copies Bavli chapter bounds to its commentaries, whose first/last
    comment may be absent. Do not treat that as a malformed printed segment:
    keep the actual text INSIDE the chapter. An explicit numeric range has no
    such evidence and must retain both endpoints or stay omitted.
    """
    if (ref_factory is None or _top_category(ref.index) != "Talmud"
            or not ref.is_range() or ref.is_segment_level()
            or not getattr(ref.index, "base_text_titles", None)):
        return None
    parts = getattr(rr.raw_entity, "raw_ref_parts", ())
    if any(getattr(getattr(part, "type", None), "name", None) == "NUMBERED"
           and not _chapter_marker(_plain_quotes(getattr(part, "text", ""))) for part in parts):
        return None
    words = [word.strip("()[],:;.") for word in
             _NIQQUD_RE.sub("", _plain_quotes(_QUOTE_HTML_TAG_RE.sub(" ", anchor))).split()]
    if len(words) > 16 or not any(_chapter_marker(word) for word in words):
        return None
    spelled = set().union(*(chapter_lead_names(" ".join(words[i:j]))
                            for i in range(len(words)) for j in range(i + 1, min(len(words), i + 8) + 1)))
    if not any(whole.normal() == ref.normal() and spelled & names
               for whole, names in _book_chapters(ref.index.title)):
        return None
    tail = "" if content is None or end is None else content[end:]
    if not chapter_book_is_proven(ref, anchor, tail, source_ref):
        return None
    normal = _chapter_content_bounds(ref.normal())
    return None if normal is None else ref_factory(normal)


def base_code_has_commentary_address(ref, anchor: str) -> bool:
    # A se'if katan belongs to a commentator, not to the Shulchan Arukh itself.
    # Do not guess the commentator or remove the prefix and invent a base se'if.
    return getattr(getattr(ref, "index", None), "title", "").startswith("Shulchan Arukh,") and bool(re.search(
        r'(?<![א-ת])ו?[בלמכ]?(?:ס[\"\']ק|סק[\"\']|סעיף\s+קטן)', _plain_quotes(anchor)))


_CODE_NOTE_ADDRESS_RE = re.compile(
    r"ו?ב?(?P<part>יו[\"]ד|חו[\"]מ)\s+"
    r"(?:(?:סימן|סי['\"]?)\s+)?(?P<chapter>[א-ת'\"]{1,7})\s+"
    r"(?:ס[\"]ק\s*|סק[\"]?\s*|סעיף\s+קטן\s+)(?P<note>[א-ת'\"]{1,7})")
_CODE_NOTE_AUTHOR_RE = re.compile(
    r"(?<![א-ת])ו?[כשה]?(?:ה)?(?P<author>ש[\"]ך|ט[\"]ז)"
    r"(?:\s+ז[\"]ל)?(?:\s+שם)?(?:\s+בסי'\s+הנ[\"]ל)?$")


def explicit_code_commentary_ref(ref, content, start, end, ref_factory):
    """Recover a named Shakh/Taz with its own printed siman and se'if katan.

    The full anchor must spell the code, both canonical numbers and the note
    marker. A directly attributed, unambiguous author must precede it. Never
    subtract100 from the resolver's result or infer an author from a source book.
    """
    if ref_factory is None or not getattr(getattr(ref, "index", None), "title", "").startswith("Shulchan Arukh,"):
        return None
    def plain(text):
        return _NIQQUD_RE.sub("", _plain_quotes(_QUOTE_HTML_TAG_RE.sub(" ", text)))
    anchor = plain(content[start:end]).strip(" ()[]")
    address = _CODE_NOTE_ADDRESS_RE.fullmatch(anchor)
    if address is None:
        return None
    before = plain(content[:start]).rstrip(" ([")
    cut = max(before.rfind(mark) for mark in ".:;()[]")
    before = before[cut + 1:].strip()
    author = _CODE_NOTE_AUTHOR_RE.search(before)
    if author is None:
        return None
    lead = before[:author.start()].strip()
    if lead and not re.search(r"(?:כתב|ועיין|ועי'|ולא|קושית|קושיית)$", lead):
        return None  # e.g. "הבית יוסף והש\"ך": more than one attributed work
    letters = [re.sub(r"[^א-ת]", "", address[k]) for k in ("chapter", "note")]
    # These are the existing strict canonical numerals (below400). Other spellings
    # stay omitted rather than expanding the accepted address language implicitly.
    if not all(_is_canonical_numeral(n) for n in letters):
        return None
    chapter, note = map(_numeral, letters)
    part = {"יו\"ד": "Yoreh De'ah", 'חו\"מ': "Choshen Mishpat"}[address["part"]]
    if ref.index.title != f"Shulchan Arukh, {part}":
        return None
    title = {"ש\"ך": "Siftei Kohen", 'ט\"ז': "Turei Zahav"}[author["author"]]
    title = f"{title} on Shulchan Arukh, {part}"
    try:
        candidate = ref_factory(f"{title} {chapter}:{note}")
    except Exception:
        return None
    if (candidate.index.title != title or candidate.sections != [chapter, note]
            or candidate.toSections != candidate.sections or not target_has_text(candidate)):
        return None
    return candidate


def has_unproven_code_number(rr, ref) -> bool:
    """Do not turn common prose/halakhot abbreviations into Rambam segments.

    Bare לא / כ׳ / מה״נ may be prose, unlike explicitly labelled הלכה כ׳.
    Omit the ambiguous reading; a coincidentally existing segment proves no number.
    """
    if not getattr(getattr(ref, "index", None), "title", "").startswith("Mishneh Torah,"):
        return False
    return any(getattr(getattr(part, "type", None), "name", None) == "NUMBERED"
               and _NIQQUD_RE.sub("", _plain_quotes(getattr(part, "text", ""))).strip()
               in {"לא", "כ'", 'מה"נ'}
               for part in getattr(rr.raw_entity, "raw_ref_parts", ()))


def is_implausible_citation_target(target_ref: str, anchor_text: str) -> bool:
    return (
        is_abbreviation_misread(target_ref, anchor_text)
        or is_mishnah_chapter_misread(target_ref, anchor_text)
    )


def book_line_plan(bk, lines, item, precomputed):
    """(NER line indices, line reuse pairs, stateful ibid) for one requested book.

    An unescalated context probe stays narrow and stateless: its probed lines have no
    NER entity, so no ibid history moves.  Otherwise it covers the whole book.
    """
    stateful_ibid = requires_book_context(lines)
    all_indices = {line_index for line_index, _, _ in lines}
    if "ner_ranges" in item:
        ner_indices = indices_from_ranges(item["ner_ranges"])
        reuse_value = item.get("reuse")
        if type(reuse_value) is not list:
            raise RuntimeError(f"line reuse plan is not an array for {bk!r}")
        reuse = []
        for index, pair in enumerate(reuse_value):
            if (
                type(pair) is not list
                or len(pair) != 2
                or any(type(value) is not int for value in pair)
            ):
                raise RuntimeError(
                    f"invalid line reuse pair {index} for {bk!r}"
                )
            reuse.append(tuple(pair))
    else:
        ner_indices = all_indices
        reuse = []
    if "context_ranges" in item:
        if indices_from_ranges(item["context_ranges"]) != all_indices:
            raise RuntimeError(f"context probe does not cover the book {bk!r}")
        key = (bk.source_name, bk.canonical_he_title)
        if precomputed is not None and key not in precomputed.escalated:
            if not stateful_ibid or any(precomputed.entity_lines(key).values()):
                raise RuntimeError(f"unescalated context probe is not inert for {bk!r}")
            stateful_ibid = False
        else:
            ner_indices = all_indices
            reuse = []
    return ner_indices, reuse, stateful_ibid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True, help="lines_snapshot.db from dumpLines")
    ap.add_argument("--repo", required=True, help="LinkerToOtzaria repo root (artifacts/ written here)")
    ap.add_argument("--run-dir", required=True, help="transient workdir for claim/done/logs")
    ap.add_argument("--label", default="w1")
    ap.add_argument("--only-books", default=None,
                    help="restrict to book_keys listed in this JSON file "
                         "([{source_name, canonical_he_title}, …]); used by the incremental driver")
    ap.add_argument("--bavli-convention", action="store_true",
                    help="keep Bavli/Yerushalmi & Mishnah/Gemara ambiguities (prefer Bavli/Mishnah); default drops all ambiguous")
    ap.add_argument("--ner-bundle-dir", default=None,
                    help="raw-NER handoff root; disables all live GPU calls")
    ap.add_argument("--expected-engine-fingerprint", default=None)
    ap.add_argument("--expected-relink-request-id", default=None)
    ap.add_argument("--pool-workers", type=int, default=0,
                    help="load the library once, then fork this many resolver children "
                         "from it (copy-on-write); this process becomes their master")
    args = ap.parse_args()

    global _BAVLI_CONVENTION
    _BAVLI_CONVENTION = args.bavli_convention

    run = args.run_dir
    for d in ("done", "claim", "logs", "failed", "worker-heartbeats", "checkpoints",
              CLAIM_EVENT_DIR, os.path.join(MEMORY_DIR, REQUEUE_MARKER_DIR)):
        os.makedirs(os.path.join(run, d), exist_ok=True)
    heartbeat_path = os.path.join(heartbeat_directory(run), args.label)
    os.makedirs(os.path.dirname(heartbeat_path), exist_ok=True)

    def worker_heartbeat():
        # A watchdog monitors this worker-specific heartbeat, not book completion.
        # Giant healthy books may run for hours; process_book refreshes once per
        # batch, so only a genuinely stalled worker crosses the watchdog threshold.
        try:
            os.utime(heartbeat_path, None)
        except FileNotFoundError:
            open(heartbeat_path, "a").close()

    worker_heartbeat()

    def log(msg):
        line = f"{time.strftime('%H:%M:%S')} {args.label} {msg}"
        with open(os.path.join(run, "logs", "progress.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # Also to stdout, flushed: on remote runners (Kaggle) the run dir is unreachable —
        # the CI job log is the ONLY live window into worker progress.
        print(line, flush=True)

    def skipped_log(line):
        with open(os.path.join(run, "logs", "skipped_lines.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def metric_log(value):
        import json as _json
        payload = {
            "unix": time.time(),
            "worker": args.label,
            **value,
        }
        with open(
            os.path.join(run, "logs", f"perf-{args.label}.jsonl"),
            "a",
            encoding="utf-8",
        ) as stream:
            stream.write(_json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")
    # Before the library load: pool children inherit the monitor and any taint.
    install_memory_taint_monitor()
    import django
    django.setup()
    quiet_sefaria_linker_logs()
    from sefaria.model import Ref, library
    linker = library.get_linker("he")

    con = sqlite3.connect(f"file:{args.snapshot}?mode=ro", uri=True)
    validate_snapshot_contract(con)
    books = all_book_keys(con)
    requested_items = None
    requested_plans = {}
    if args.only_books:
        import json as _json
        with open(args.only_books, encoding="utf-8") as fh:
            requested_items = _json.load(fh)
        if not isinstance(requested_items, list):
            raise RuntimeError("--only-books must contain a JSON array")
        wanted = set()
        for index, item in enumerate(requested_items):
            allowed_shapes = (
                {"source_name", "canonical_he_title"},
                {"source_name", "canonical_he_title", "hash"},
                {
                    "source_name", "canonical_he_title", "hash",
                    "ner_ranges", "reuse",
                },
                {
                    "source_name", "canonical_he_title", "hash",
                    "ner_ranges", "reuse", "context_ranges",
                },
            )
            if (not isinstance(item, dict)
                    or set(item) not in allowed_shapes
                    or not isinstance(item.get("source_name"), str)
                    or not isinstance(item.get("canonical_he_title"), str)):
                raise RuntimeError(f"--only-books entry {index} has an invalid shape")
            key = (item["source_name"], item["canonical_he_title"])
            if key in wanted:
                raise RuntimeError(f"--only-books contains duplicate book {key!r}")
            wanted.add(key)
            requested_plans[key] = item
        books = [bk for bk in books if (bk.source_name, bk.canonical_he_title) in wanted]
        if len(books) != len(wanted):
            missing = wanted - {(book.source_name, book.canonical_he_title) for book in books}
            raise RuntimeError(f"--only-books includes book(s) absent from snapshot: {sorted(missing)!r}")
        log(f"restricted to {len(books)}/{len(wanted)} requested books")
    precomputed = None
    if args.ner_bundle_dir:
        if not args.only_books:
            raise RuntimeError("--ner-bundle-dir requires --only-books")
        if not args.expected_engine_fingerprint or not args.expected_relink_request_id:
            raise RuntimeError("precomputed NER requires exact engine fingerprint and request identity")
        from incremental import sha256_of_file
        from ner_handoff import NerBundle
        expected_hashes = {}
        for index, item in enumerate(requested_items):
            digest = item.get("hash")
            if not isinstance(digest, str):
                raise RuntimeError(f"precomputed NER requires source hash on --only-books entry {index}")
            if "ner_ranges" not in item or "reuse" not in item:
                raise RuntimeError(
                    f"precomputed NER requires the exact line plan on --only-books entry {index}"
                )
            expected_hashes[(item["source_name"], item["canonical_he_title"])] = digest
        precomputed = NerBundle(
            args.ner_bundle_dir,
            request_id=args.expected_relink_request_id,
            snapshot_sha256=sha256_of_file(args.snapshot),
            engine_fingerprint=args.expected_engine_fingerprint,
            changed_books=[
                {
                    **book.to_dict(),
                    **{
                        name: requested_plans[(book.source_name, book.canonical_he_title)][name]
                        for name in ("ner_ranges", "context_ranges")
                        if name in requested_plans[(book.source_name, book.canonical_he_title)]
                    },
                }
                for book in books
            ],
            expected_book_hashes=expected_hashes,
            expected_batch_lines=BATCH_LINES,
            expected_batch_chars=BATCH_CHARS,
        )
        books = largest_books_first(
            books,
            {
                key: manifest["eligible_line_count"]
                for key, manifest in precomputed.books.items()
            },
        )
        log(f"verified raw-NER handoff for {len(books)} changed book(s); live GPU disabled")
    log(f"worker up: {len(books)} books in snapshot, bavli_convention={_BAVLI_CONVENTION}")

    def _worker_loop():
        """One worker life: claim, link and mark books until none remain."""
        heavy_phase = False
        # Idle-recycle decisions are relative to this life's baseline: an absolute
        # threshold below a fresh image's own footprint would recycle forever.
        life_baseline = worker_memory_bytes()
        if not install_memory_taint_monitor():
            log("memory: sys.monitoring unavailable; a swallowed MemoryError cannot be gated")
        applied = apply_address_space_limit(WORKER_ADDRESS_SPACE_BYTES, log)
        log(
            f"worker life: virtual size {virtual_bytes()}, address-space limit "
            f"{applied or 'none'} (requested {WORKER_ADDRESS_SPACE_BYTES})"
        )

        def pending_books():
            # A single pass is not enough: a worker walks PAST a book whose claim a peer
            # holds — if that peer then dies mid-book (e.g. kernel OOM), nobody in a
            # one-pass world ever comes back, and the whole run fails hours later on the
            # completeness assertion. Rescan until every book is done: a worker may recover
            # a stale claim only after the dead owner has also released its kernel lock, so
            # each round either shrinks the set or politely waits a live peer out.
            #
            # Deferred (heavy) books come last: the heavy phase begins only once every
            # ordinary book is DONE (not merely claimed), so the memory they may need is
            # actually free, and HeavySlot bounds how many resolve at once.
            nonlocal heavy_phase
            remaining = books
            while remaining:
                normal, heavy = partition_pending(remaining, run)
                # Hand out only books no live peer owns.  The pending list itself is
                # unchanged (partition_pending still answers "which books lack a done
                # marker"), so the heavy phase still begins only once every ordinary
                # book is DONE — not merely claimed.
                if normal:
                    for bk in unclaimed(normal, run):
                        yield bk
                elif heavy:
                    if not heavy_phase:
                        heavy_phase = True
                        applied = apply_address_space_limit(HEAVY_WORKER_ADDRESS_SPACE_BYTES, log)
                        log(
                            f"heavy phase: {len(heavy)} deferred book(s) remain; at most "
                            f"{HEAVY_BOOK_SLOTS} in flight, address space "
                            + (str(applied) if applied else
                               f"kept at {resource.getrlimit(resource.RLIMIT_AS)[0] if hasattr(resource, 'RLIMIT_AS') else 'none'}")
                        )
                    for bk in unclaimed(heavy, run):
                        yield bk
                remaining = [bk for bk in remaining
                             if not os.path.exists(os.path.join(run, "done", claim_id(bk)))]
                if remaining:
                    worker_heartbeat()
                    # The loop condition stays the `done` ledger, never the claim
                    # state: a worker must outlive every claimed-but-unfinished book,
                    # or a peer dying at the tail would leave nobody to retry it.
                    owned = sum(1 for bk in remaining
                                if BookClaim.is_claimed(run, claim_id(bk)))
                    log(
                        f"rescan: {len(remaining)} book(s) still lack a done marker "
                        f"({owned} owned by a live peer); sleeping {RESCAN_WAIT_SEC}s"
                    )
                    time.sleep(RESCAN_WAIT_SEC)

        def recycle_now(message, claim=None, slot=None):
            """Shed this worker image, releasing everything it holds first.

            Every recycle states the two numbers a memory post-mortem needs -- books
            completed in this life and private bytes owned above the life baseline --
            so `pool: wNN recycled` is never the only record of why an image went.
            release() and HeavySlot.release() are idempotent, so the caller's
            `finally` is free to release again on the paths that reach it (these do
            not: exec/exit never returns).
            """
            close_memory_window()  # no abort may interrupt the release below
            clear_current_book(run, args.label)
            log(
                f"{message} [life: {processed} book(s), "
                f"{worker_memory_bytes() - life_baseline} private bytes above baseline]"
            )
            if claim is not None:
                claim.release()
            if slot is not None:
                slot.release()
            worker_heartbeat()
            con.close()
            _recycle_process()

        def defer_book(bk, cid, growth_bytes, reason, claim=None):
            # Persist the decision BEFORE the claim goes (a peer walking a stale list
            # must meet the marker, not a free claim), then shed this bloated image: the
            # book resumes from its shards in the heavy phase, under a slot, in a fresh
            # worker.
            mark_heavy(run, cid, bk, growth_bytes, reason)
            recycle_now(
                f"deferring {bk.source_name}/{bk.canonical_he_title!r} to the heavy phase "
                f"({reason}; grew {growth_bytes} bytes); recycling",
                claim,
            )

        processed = 0
        for bk in pending_books():
            if _MEMORY_RECYCLE_REQUESTED or _IMAGE_TAINTED:
                recycle_now("memory: recycling between books")
            cid = claim_id(bk)
            gate = gate_heavy_book(run, cid, heavy_phase)
            if gate == "skip":
                continue
            heavy = gate == "heavy"
            if heavy and HEAVY_FRESH_WORKER:
                # A heavy book gets a worker that owns none of the budget yet.  This
                # is decided BEFORE the slot and BEFORE the claim, so a recycle here
                # holds nothing: the book keeps its heavy marker, stays unclaimed and
                # undone, and the fresh image (or a fresh peer) takes it on the next
                # pass.  A fresh image has processed == 0, so it never lands here
                # twice in a row -- the rule cannot become a recycle loop.
                growth = worker_memory_bytes() - life_baseline
                if not worker_is_fresh_for_heavy(processed, growth):
                    journal_memory_event(run, {
                        "event": "recycled-for-heavy", "claim_id": cid,
                        "source_name": bk.source_name,
                        "canonical_he_title": bk.canonical_he_title,
                        "worker": args.label, "processed": processed,
                        "growth_bytes": int(growth),
                    })
                    recycle_now(
                        f"heavy phase: {bk.source_name}/{bk.canonical_he_title!r} needs a "
                        "fresh worker; recycling before claiming it"
                    )
            slot = None
            claim = None
            exclusive = heavy and single_heavy_mode(run)
            if heavy:
                slot = (HeavySlot.acquire_all(run, HEAVY_BOOK_SLOTS) if exclusive
                        else HeavySlot.acquire(run, HEAVY_BOOK_SLOTS))
                if slot is None:
                    # Every slot is busy; another heavy book (or a rescan) comes next.
                    # A pool child that idles here gives its accumulated PRIVATE memory
                    # back to the heavy pair - judged against this life's baseline, so a
                    # fresh image (pool child or exec'd standalone) never loops on itself.
                    worker_heartbeat()
                    time.sleep(15)
                    if worker_memory_bytes() - life_baseline > HEAVY_IDLE_RECYCLE_BYTES:
                        recycle_now("heavy phase: no free slot; recycling to release memory")
                    continue
            # Retry loop: a NER outage mid-book must retry THE SAME book, not skip to the
            # next one — every other worker may already be past it, and a book with neither
            # `done` nor `failed` would silently advance the baseline (the driver also
            # asserts completeness, but the engine must not create the gap to begin with).
            try:
                while True:
                    if os.path.exists(os.path.join(run, "done", cid)):
                        break
                    # Exclusive, unconditionally.  The raw-NER (cooperative) path used
                    # to skip the book claim entirely and rely on per-batch locks, so
                    # every worker that reached a book re-read its lines, re-validated
                    # its shards, re-assembled its artifact and wrote a second `done`
                    # marker: 29.5% of the completions in relink 34016397157.  Claiming
                    # BEFORE book_lines() is what makes a losing worker free.
                    claim = BookClaim.acquire(run, cid)
                    if claim is None:
                        break  # done, or another live worker owns it (it marks done/failed)
                    lines = book_lines(con, bk)
                    ner_indices, reuse, stateful_ibid = book_line_plan(
                        bk, lines,
                        requested_plans.get((bk.source_name, bk.canonical_he_title), {}),
                        precomputed,
                    )
                    cooperative = precomputed is not None and not stateful_ibid
                    if stateful_ibid and (reuse or set(ner_indices) != {
                        line_index for line_index, _, _ in lines
                    }):
                        raise RuntimeError(
                            "incremental plan omitted ibid context; resolve this whole book"
                        )
                    out_path = os.path.join(args.repo, book_key_to_relpath(bk))
                    checkpoint_dir = os.path.join(run, "checkpoints", cid)
                    t0 = time.time()
                    memory_before_book = worker_memory_bytes()
                    try:
                        worker_heartbeat()
                        if precomputed is None and ner_indices:
                            wait_for_ner(log)

                        def recycle_worker(current_rss, batches):
                            # The next exec (or a peer) must be able to claim the same book
                            # immediately and resume its immutable batch shards.  The slot,
                            # if this is a heavy book, goes with it: the fresh image
                            # re-acquires one rather than holding it across a fork.
                            recycle_now(
                                f"recycling mid-book (current RSS {current_rss} over cap "
                                f"{int(HEAVY_RSS_CAP if heavy else RSS_CAP)}) after {batches} "
                                f"checkpointed batch(es) this life",
                                claim, slot,
                            )

                        heartbeat = lambda: (
                            worker_heartbeat(),
                            claim.heartbeat() if claim is not None else None,
                        )
                        if _MEMORY_RECYCLE_REQUESTED:
                            recycle_now("pool memory governor: recycling before the book",
                                        claim, slot)
                        window = {"claim_id": cid, **bk.to_dict(), "heavy": heavy,
                                  "exclusive": exclusive,
                                  "processed": processed,
                                  "limits": current_memory_limits(heavy)}
                        with periodic_heartbeat(heartbeat), \
                                memory_pressure_window(run, args.label, window, log):
                            record_count, words = process_book_checkpointed(
                                linker, bk, lines, skipped_log, heartbeat,
                                checkpoint_dir, out_path, recycle_worker,
                                precomputed=precomputed,
                                ner_indices=ner_indices,
                                reuse=reuse,
                                context_ref_factory=Ref,
                                metric_log=metric_log,
                                cooperative=cooperative,
                                prior_lock_path=os.path.join(run, "claim", f"prior-{cid}.lock"),
                                defer_when_heavy=not heavy,
                                recycle_cap=HEAVY_RSS_CAP if heavy else None,
                                ibid_context=IbidContext() if stateful_ibid else None,
                            )
                    except BookWorkInProgress:
                        # A batch lock is still held by a process this worker took the book
                        # over from (a stale takeover can overlap a not-yet-reaped owner).
                        # Assemble on a later rescan, once every atomic shard exists —
                        # never write a book whose shards are incomplete.
                        break
                    except BookDeferred as deferred:
                        defer_book(bk, cid, deferred.growth_bytes,
                                   f"grew past the heavy threshold after {deferred.batches} batch(es)",
                                   claim)
                    except (Exception, HostMemoryPressure) as e:
                        detail = f"{type(e).__name__}: {e}"
                        fatal_memory = False
                        if is_memory_error(e) or _IMAGE_TAINTED:
                            # The address-space ceiling caught a runaway line. Nothing of
                            # this image is trustworthy after MemoryError, so the worker
                            # goes either way; the question is where the book goes.
                            site = memory_error_site(e)
                            if site:
                                log(f"memory: {bk.source_name}/{bk.canonical_he_title!r} "
                                    f"ran out in {' > '.join(site)}")
                            # Stopped by the governor while another heavy book ran beside it:
                            # outside pressure, not this book's own standstill.
                            governor_caused = _MEMORY_RECYCLE_REQUESTED and not exclusive
                            if heavy and _MEMORY_RECYCLE_REQUESTED:
                                enter_single_heavy_mode(run, cid, log)
                            attempt = {
                                "committed": committed_shard_count(checkpoint_dir),
                                "governor": governor_caused,
                                "asked_by_governor": _MEMORY_RECYCLE_REQUESTED,
                                "site": site,
                                "claim_id": cid,
                                "source_name": bk.source_name,
                                "canonical_he_title": bk.canonical_he_title,
                                "worker": args.label,
                                "processed": processed,
                                "growth_bytes": int(worker_memory_bytes() - memory_before_book),
                                "line": memory_error_line_number(str(e)),
                                "phase": "heavy" if heavy else "normal",
                                "error": detail,
                                "limits": current_memory_limits(heavy),
                            }
                            if not heavy:
                                # Normal phase: the designed lane is the heavy phase, which
                                # retries the book under a slot, a raised ceiling and (since
                                # the fresh-worker rule) an empty worker.  Deferral is not
                                # the book's one requeue - that budget is for the lane where
                                # a MemoryError is otherwise fatal.
                                journal_memory_event(run, {"event": "deferred-after-memoryerror",
                                                           **attempt})
                                defer_book(bk, cid, attempt["growth_bytes"], detail, claim)
                            won, earlier = claim_memory_requeue(run, cid, attempt)
                            if won:
                                # Undone and unclaimed: resumes from its shards on a fresh worker.
                                journal_memory_event(run, {"event": "requeued-after-memoryerror",
                                                           **attempt})
                                recycle_now(
                                    format_memory_requeue_line(
                                        bk=bk, growth_bytes=attempt["growth_bytes"],
                                        worker=args.label, processed=processed,
                                        line_number=attempt["line"], retry=len(earlier) + 1,
                                        committed=attempt["committed"], site=site,
                                    ),
                                    claim, slot,
                                )
                            # No more progress to buy: fail the run.
                            journal_memory_event(run, {"event": "failed-after-requeue", **attempt})
                            detail = format_memory_failure(
                                bk=bk, earlier=earlier, last=attempt, limits=attempt["limits"],
                            )
                            fatal_memory = True
                        if not fatal_memory and precomputed is None and ner_indices and not ner_alive():
                            # Infrastructure outage, not a book problem: release the claim, wait
                            # for NER, and retry this book (any worker may pick it up meanwhile).
                            # The heavy slot, if any, stays held across the retry.
                            log(f"NER outage during {bk.canonical_he_title!r}; releasing claim and waiting")
                            if claim is not None:
                                claim.release()
                            wait_for_ner(log)
                            continue
                        log(f"ERROR {bk.canonical_he_title!r}: {detail}")
                        with open(os.path.join(run, "logs", "errors.log"), "a", encoding="utf-8") as ef:
                            ef.write(f"{bk.source_name}\t{bk.canonical_he_title}\t{detail}\n")
                        # Record the failure so the incremental driver FAILS the run loudly:
                        # a book-level crash must never be silently absorbed into a done+exit-0.
                        # The `done` marker still stops THIS run from retrying it (poison-book
                        # loop guard). Content = the book_key (cid is not reversible) plus, for
                        # a memory failure, the note the driver quotes in its own error: the
                        # ##[error] a human reads is the driver's, not this line.
                        write_failed_book(run, cid, bk, detail if fatal_memory else None)
                        if fatal_memory:
                            # Exit before Python frees the exploded object graph.
                            recycle_now("memory: recycling after a failed book", claim, slot)
                        if claim is not None:
                            claim.release()
                        break

                    # Atomic per-book output: write the artifact only when the whole book is linked.
                    # A book with zero links writes no file (kept clean); a previously-linked book
                    # that now yields nothing has its stale artifact removed.  EAFP: the
                    # exists()/remove() pair here raced peers into 36 worker deaths across the
                    # two 2026-09-06 relinks (see linker_artifact.remove_artifact).
                    if record_count == 0:
                        remove_artifact(out_path)
                    if not mark_done(run, cid):
                        log(
                            f"WARNING: {bk.source_name}/{bk.canonical_he_title!r} was already "
                            "marked done — the exclusive book claim did not hold"
                        )
                    import shutil
                    # Shards stay for the run in the raw-NER path: they are the local
                    # checkpoint's resumable state, and the next invocation resets them.
                    if not cooperative:
                        shutil.rmtree(checkpoint_dir, ignore_errors=True)
                    if claim is not None:
                        claim.release()
                    processed += 1
                    log(f"done {bk.source_name}/{bk.canonical_he_title!r} lines={len(lines)} words={words} links={record_count} {time.time()-t0:.1f}s")
                    worker_heartbeat()
                    break
            finally:
                # Every exit of the retry loop - done, a lost claim, contention, failure,
                # success - gives the claim and the slot back here.  flock is per open
                # file description, so a leaked fd would block even THIS worker's next
                # acquire and, once every slot leaked, leave the heavy phase spinning
                # until the job timeout.  (Recycling execs: the fd is O_CLOEXEC, the
                # kernel drops it.)
                #
                # The claim matters most on the BookWorkInProgress path, which leaves the
                # book UNFINISHED: holding its claim would hide the book from every peer's
                # rescan while this worker never comes back to it, and the run would spin
                # to the job timeout on a book nobody may touch.  release() is idempotent,
                # so the paths that already released (success, failure, NER outage) are
                # unaffected, and the ones that exec/exit never reach here.
                if claim is not None:
                    claim.release()
                if slot is not None:
                    slot.release()
                clear_current_book(run, args.label)

            if _MEMORY_RECYCLE_REQUESTED or _IMAGE_TAINTED:
                recycle_now("memory: recycling between books")

            # Self-recycle BETWEEN books (never mid-book): the Ref cache grows across books,
            # so releasing it here reclaims memory without abandoning a claimed, half-linked book.
            current_rss = worker_memory_bytes()
            if precomputed is None and recycle_needed(current_rss, RSS_CAP, processed):
                log(
                    f"recycling (current RSS {current_rss} over cap {int(RSS_CAP)}) "
                    f"after {processed} books this life"
                )
                _recycle_process()

        log(f"no more books (processed {processed} this life); exiting")
        # Leave the final heartbeat in place until the supervisor observes process exit.
        # Deleting it created a race where a still-live cleanly-exiting worker was treated
        # as stale from its original spawn time and killed with SIGTERM.
        worker_heartbeat()

    # Last moment before any book is linked and before the pool forks: re-assert the
    # quieting set right after django.setup(), because the whole Sefaria/linker/NER
    # import chain has run since then.  One line states the effective levels, so a
    # future post-mortem can tell "quieted" from "silently reconfigured back to INFO"
    # without diffing a 123 MB log.
    log("sefaria linker logs quieted: " + ", ".join(
        f"{name}={level}" for name, level in quiet_sefaria_linker_logs().items()
    ))

    if args.pool_workers:
        # Everything loaded above — Django, the library, the resolver tries, the
        # verified NER bundle — is shared copy-on-write by every forked child.  A child
        # only reopens its own SQLite handle and heartbeat; pymongo (4.x) resets its
        # own connections after fork (MongoClient._after_fork), and Django's DB
        # handles are closed so nothing inherited is used across the fork.
        #
        # That reset is what each child's first query reports as `UserWarning:
        # MongoClient opened before fork` (13 of them in run 34016397157, one per child
        # life).  The client is Sefaria's own module-level handle, created when the
        # library is loaded — i.e. necessarily before this fork, because loading once
        # and sharing the result IS the pool.  Creating a client per child would mean
        # rebinding Sefaria's global `db` inside the child, which this repo does not
        # own; closing the parent's client first is not obviously reversible in the
        # pinned pymongo, and a client the children cannot reopen would break every
        # relink.  So the warning stays visible and unfiltered: it names a real
        # constraint of this design, and the fork-time inventory above says exactly
        # which threads it is about.
        con.close()

        def _child_setup(label):
            nonlocal con, heartbeat_path
            args.label = label
            con, heartbeat_path = prepare_pool_child(args.snapshot, run, label)
            install_memory_pressure_handler()
            worker_heartbeat()

        governor = None
        if POOL_MEMORY_GOVERNOR and mem_available_bytes() is not None:
            soft, hard = pool_memory_floors()
            governor = PoolMemoryGovernor(log=log, soft_floor=soft, hard_floor=hard)
            log(f"pool memory governor: soft floor {format_bytes_gb(soft)}, hard floor "
                f"{format_bytes_gb(hard)} MemAvailable, victims from "
                f"{format_bytes_gb(POOL_MEMORY_MIN_VICTIM_BYTES)} private, grace "
                f"{POOL_MEMORY_GRACE_SECONDS:.0f}s")
        else:
            log("pool memory governor: off")
        log(f"pool master: forking {args.pool_workers} resolver child(ren) from the loaded library")
        code = run_pool(
            args.pool_workers, run, args.label, _child_setup, _worker_loop,
            restart_limit=max(0, int(os.environ.get("LINKER_POOL_RESTART_LIMIT", "2"))),
            stall_seconds=float(os.environ.get("LINKER_POOL_STALL_SECONDS", "1800")),
            log=log, governor=governor,
        )
        worker_heartbeat()
        sys.exit(code)

    _worker_loop()


if __name__ == "__main__":
    main()
