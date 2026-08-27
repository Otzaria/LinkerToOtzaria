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
import hashlib
import os
import re
import resource
import sqlite3
import sys
import time
import uuid

# django/requests are imported lazily (main/ner_alive): the incremental driver imports
# this module for claim_id alone and must not drag the whole Sefaria stack with it.

# linker_artifact lives next to this file — import it as the single format authority.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linker_artifact import BookKey, LinkRecord, book_key_to_relpath, content_hash, write_artifact  # noqa: E402
from line_baseline import indices_from_ranges  # noqa: E402

# Lines per bulk NER call. Transport granularity only — per-line output is independent
# of batching. Tunable per host: GPU serving OOMs on 100-line batches of monster books
# (16GB VRAM), so Kaggle runs use a smaller batch via env.
BATCH_LINES = int(os.environ.get("LINKER_BATCH_LINES", "100"))
# Give up on a dead NER after this long (crash-looped GPU service, not a blip).
NER_MAX_WAIT_SEC = int(os.environ.get("LINKER_NER_MAX_WAIT_SEC", "1800"))
# Self-recycle above this many bytes. Overridable per machine: recycling costs a full
# library reload (~8s on M-series, ~22s on Neoverse-N1), so give workers headroom when
# RAM allows (e.g. 3e9 on a 22GB box with 2 workers) and keep the tight default for CI.
RSS_CAP = float(os.environ.get("LINKER_RSS_CAP_BYTES", 1.8e9))
CLAIM_STALE_SEC = 900    # compatibility argument; the kernel lock is liveness authority
RESCAN_WAIT_SEC = max(1, int(os.environ.get("LINKER_RESCAN_SECONDS", "60")))
NER_URL = "http://127.0.0.1:5051/recognize-entities"

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


def claim_id(bk: BookKey) -> str:
    """Filesystem-safe, collision-free handle for a book (claim/done markers)."""
    h = hashlib.sha1(f"{bk.source_name}\0{bk.canonical_he_title}".encode("utf-8"))
    return h.hexdigest()


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
            # live worker owns the claim.  Waiting for the old heartbeat to age out
            # used to strand a crashed worker's book for up to 15 minutes even though
            # the kernel had already released its ownership lock.  Retire only the
            # mutable claim directory immediately; immutable batch checkpoints live
            # elsewhere and are reused by the replacement worker.
            retired = f"{claim_path}.abandoned-{uuid.uuid4().hex}"
            os.replace(claim_path, retired)
            shutil.rmtree(retired, ignore_errors=True)
            os.mkdir(claim_path)

        open(heartbeat_path, "w").close()
        return BookClaim(claim_path, lock_fd)

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
    for i in range(0, len(lines), BATCH_LINES):
        batch = [
            (li, content, context_ref)
            for li, content, context_ref in lines[i:i + BATCH_LINES]
            if is_ner_eligible_line(content)
        ]
        heartbeat()
        if not batch:
            continue
        batch_records, batch_words = process_batch(
            linker, bk, batch, skipped_log, batch_start=i, precomputed=precomputed,
            context_ref_factory=context_ref_factory,
        )
        records.extend(batch_records)
        words += batch_words
    return records, words


def process_batch(
    linker, bk, batch, skipped_log, *, batch_start=0, precomputed=None,
    context_ref_factory=None,
):
    """Link one transport batch. Output is independent of neighbouring batches."""
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
    try:
        if precomputed is not None:
            docs = precomputed.resolve_batch(
                linker, bk, batch, batch_start, book_context_refs=context_objects,
            )
        else:
            docs = linker.bulk_link(
                [content for _, content, _ in batch],
                book_context_refs=context_objects,
                type_filter="citation",
            )
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
                docs.append(linker.bulk_link(
                    [content],
                    book_context_refs=[context_objects[batch_offset]],
                    type_filter="citation",
                )[0])
            except Exception as le:
                skipped_log(f"{bk.source_name}\t{bk.canonical_he_title}\t{li}\t{type(le).__name__}: {le}")
                raise RuntimeError(
                    f"line {li} failed to link: {type(le).__name__}: {le}") from le

    records: list[LinkRecord] = []
    for (line_index, content, context_ref), context_object, doc in zip(batch, context_objects, docs):
        # Digest the exact content the offsets index, so the build can drop this line's
        # links if the source book changed before Phase-2 applies them (cross-cycle drift).
        src_hash = content_hash(content)
        # spaCy spans are Python code-point offsets; the Kotlin consumer indexes the
        # SAME content string in UTF-16 units. They diverge only past a non-BMP char
        # (each adds one extra UTF-16 unit) — convert exactly, on the rare lines only.
        has_non_bmp = any(ord(c) > 0xFFFF for c in content)
        for rr in doc.resolved_refs:
            try:
                ref = _pick_ref(rr)
                if ref is None:
                    continue
                start, end = rr.raw_entity.span.range
                relative_direction = relative_ref_direction(
                    rr, ref, context_object, content[start:end]
                )
                if has_raw_part_type(rr, "RELATIVE") and relative_direction is None:
                    continue
                record_context = context_ref if relative_direction is not None else None
                if has_non_bmp:
                    start += sum(1 for c in content[:start] if ord(c) > 0xFFFF)
                    end += sum(1 for c in content[:end] if ord(c) > 0xFFFF)
                records.append(LinkRecord(
                    book_key=bk, line_index=line_index,
                    start=start, end=end, target_ref=ref.normal(),
                    source_hash=src_hash,
                    context_ref=record_context,
                    relative_direction=relative_direction,
                ))
            except Exception as ce:
                # A broken citation is OUR bug (resolver/normal()), not corpus noise —
                # fail the book loudly (logged first) instead of silently losing a link.
                skipped_log(f"{bk.source_name}\t{bk.canonical_he_title}\t{line_index}\tcit\t{type(ce).__name__}: {ce}")
                raise RuntimeError(
                    f"citation on line {line_index} failed: {type(ce).__name__}: {ce}") from ce
    return records, words


def process_book_checkpointed(
    linker, bk, lines, skipped_log, heartbeat, checkpoint_dir, out_path, on_recycle,
    precomputed=None, ner_indices=None, reuse=(), context_ref_factory=None,
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
    if reuse_by_old and os.path.exists(out_path):
        for record in read_artifact(out_path):
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
    for i in range(0, len(lines), BATCH_LINES):
        batch = [
            (li, content, context_ref)
            for li, content, context_ref in lines[i:i + BATCH_LINES]
            if li in ner_indices and is_ner_eligible_line(content)
        ]
        total_words += sum(len(content.split()) for _, content, _ in batch)
        heartbeat()
        # A large changed book may reuse almost every line.  Do not create thousands
        # of empty checkpoint shards for batches that contain no requested NER work;
        # the exact line partition above already proves those lines are accounted for.
        if not batch:
            continue
        shard = os.path.join(checkpoint_dir, f"{i:012d}.jsonl")
        shard_paths.append(shard)
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
            continue
        records, _ = process_batch(
            linker, bk, batch, skipped_log, batch_start=i, precomputed=precomputed,
            context_ref_factory=context_ref_factory,
        )
        write_artifact(shard, records)
        batches_this_life += 1
        # Drop batch-local resolved documents before measuring current RSS.  The
        # long-lived Sefaria caches remain; those are precisely what exec recycles.
        del records
        import gc
        gc.collect()
        current_rss = rss_bytes()
        if recycle_needed(current_rss, RSS_CAP, batches_this_life):
            on_recycle(current_rss, batches_this_life)
            raise RuntimeError("worker recycle callback returned unexpectedly")

    records = list(reused_records)
    records.extend(chain.from_iterable(read_artifact(path) for path in shard_paths))
    records.sort(key=lambda record: (record.line_index, record.start, record.end, record.target_ref))
    count = write_artifact(out_path, records)
    return count, total_words


# Bavli-convention flag is read once into a module global by main().
_BAVLI_CONVENTION = False


def _pick_ref(rr):
    """Return the single target Ref for a resolved citation, or None to drop it."""
    if (
        not has_raw_part_type(rr, "RELATIVE")
        and getattr(getattr(rr, "context_type", None), "name", None) == "CURRENT_BOOK"
    ):
        return None
    if rr.is_ambiguous:
        if not _BAVLI_CONVENTION:
            return None
        cands = [r.ref for r in rr.resolved_raw_refs if r.ref]
        return disambiguate_bavli(cands)
    return rr.ref if rr.ref else None


def has_raw_part_type(rr, type_name: str) -> bool:
    parts = getattr(getattr(rr, "raw_entity", None), "raw_ref_parts", ())
    return any(getattr(getattr(part, "type", None), "name", None) == type_name for part in parts)


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
    above = "לעיל" in anchor_text
    below = "לקמן" in anchor_text or "להלן" in anchor_text
    if above == below:
        return None
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
    return "above" if above else "below"


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
    args = ap.parse_args()

    global _BAVLI_CONVENTION
    _BAVLI_CONVENTION = args.bavli_convention

    run = args.run_dir
    for d in ("done", "claim", "logs", "failed", "worker-heartbeats", "checkpoints"):
        os.makedirs(os.path.join(run, d), exist_ok=True)
    heartbeat_path = os.path.join(run, "worker-heartbeats", args.label)

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

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")
    import django
    django.setup()
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
                    "ner_ranges": requested_plans[
                        (book.source_name, book.canonical_he_title)
                    ]["ner_ranges"],
                }
                for book in books
            ],
            expected_book_hashes=expected_hashes,
            expected_batch_lines=BATCH_LINES,
        )
        log(f"verified raw-NER handoff for {len(books)} changed book(s); live GPU disabled")
    log(f"worker up: {len(books)} books in snapshot, bavli_convention={_BAVLI_CONVENTION}")

    def pending_books():
        # A single pass is not enough: a worker walks PAST a book whose claim a peer
        # holds — if that peer then dies mid-book (e.g. kernel OOM), nobody in a
        # one-pass world ever comes back, and the whole run fails hours later on the
        # completeness assertion. Rescan until every book is done: a worker may recover
        # a stale claim only after the dead owner has also released its kernel lock, so
        # each round either shrinks the set or politely waits a live peer out.
        remaining = books
        while remaining:
            for bk in remaining:
                yield bk
            remaining = [bk for bk in remaining
                         if not os.path.exists(os.path.join(run, "done", claim_id(bk)))]
            if remaining:
                worker_heartbeat()
                log(
                    f"rescan: {len(remaining)} book(s) still lack a done marker; "
                    f"sleeping {RESCAN_WAIT_SEC}s"
                )
                time.sleep(RESCAN_WAIT_SEC)

    processed = 0
    for bk in pending_books():
        cid = claim_id(bk)
        # Retry loop: a NER outage mid-book must retry THE SAME book, not skip to the
        # next one — every other worker may already be past it, and a book with neither
        # `done` nor `failed` would silently advance the baseline (the driver also
        # asserts completeness, but the engine must not create the gap to begin with).
        while True:
            if os.path.exists(os.path.join(run, "done", cid)):
                break
            claim = BookClaim.acquire(run, cid)
            if claim is None:
                break  # another live worker owns it — it will mark done/failed
            lines = book_lines(con, bk)
            item = requested_plans.get((bk.source_name, bk.canonical_he_title), {})
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
                ner_indices = {line_index for line_index, _, _ in lines}
                reuse = []
            out_path = os.path.join(args.repo, book_key_to_relpath(bk))
            checkpoint_dir = os.path.join(run, "checkpoints", cid)
            t0 = time.time()
            try:
                worker_heartbeat()
                if precomputed is None and ner_indices:
                    wait_for_ner(log)

                def recycle_worker(current_rss, batches):
                    log(
                        f"recycling mid-book (current RSS {current_rss} over cap "
                        f"{int(RSS_CAP)}) after {batches} checkpointed batch(es) this life"
                    )
                    # The next exec (or a peer) must be able to claim the same book
                    # immediately and resume its immutable batch shards.
                    claim.release()
                    con.close()
                    os.execv(sys.executable, [sys.executable] + sys.argv)

                record_count, words = process_book_checkpointed(
                    linker, bk, lines, skipped_log,
                    lambda: (worker_heartbeat(), claim.heartbeat()),
                    checkpoint_dir, out_path, recycle_worker,
                    precomputed=precomputed,
                    ner_indices=ner_indices,
                    reuse=reuse,
                    context_ref_factory=Ref,
                )
            except Exception as e:
                if precomputed is None and ner_indices and not ner_alive():
                    # Infrastructure outage, not a book problem: release the claim, wait
                    # for NER, and retry this book (any worker may pick it up meanwhile).
                    log(f"NER outage during {bk.canonical_he_title!r}; releasing claim and waiting")
                    claim.release()
                    wait_for_ner(log)
                    continue
                log(f"ERROR {bk.canonical_he_title!r}: {type(e).__name__}: {e}")
                with open(os.path.join(run, "logs", "errors.log"), "a", encoding="utf-8") as ef:
                    ef.write(f"{bk.source_name}\t{bk.canonical_he_title}\t{type(e).__name__}: {e}\n")
                # Record the failure so the incremental driver FAILS the run loudly:
                # a book-level crash must never be silently absorbed into a done+exit-0.
                # The `done` marker still stops THIS run from retrying it (poison-book
                # loop guard). Content = the book_key (cid is not reversible).
                import json as _json
                with open(os.path.join(run, "failed", cid), "w", encoding="utf-8") as ff:
                    _json.dump(bk.to_dict(), ff, ensure_ascii=False)
                open(os.path.join(run, "done", cid), "w").close()
                claim.release()
                break

            # Atomic per-book output: write the artifact only when the whole book is linked.
            # A book with zero links writes no file (kept clean); a previously-linked book
            # that now yields nothing has its stale artifact removed.
            if record_count == 0 and os.path.exists(out_path):
                os.remove(out_path)
            open(os.path.join(run, "done", cid), "w").close()
            import shutil
            shutil.rmtree(checkpoint_dir, ignore_errors=True)
            claim.release()
            processed += 1
            log(f"done {bk.source_name}/{bk.canonical_he_title!r} lines={len(lines)} words={words} links={record_count} {time.time()-t0:.1f}s")
            worker_heartbeat()
            break

        # Self-recycle BETWEEN books (never mid-book): the Ref cache grows across books,
        # so releasing it here reclaims memory without abandoning a claimed, half-linked book.
        current_rss = rss_bytes()
        if recycle_needed(current_rss, RSS_CAP, processed):
            log(
                f"recycling (current RSS {current_rss} over cap {int(RSS_CAP)}) "
                f"after {processed} books this life"
            )
            os.execv(sys.executable, [sys.executable] + sys.argv)

    log(f"no more books (processed {processed} this life); exiting")
    try:
        os.remove(heartbeat_path)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()
