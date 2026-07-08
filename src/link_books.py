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
import resource
import sqlite3
import sys
import time

import django
import requests

# linker_artifact lives next to this file — import it as the single format authority.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linker_artifact import BookKey, LinkRecord, book_key_to_relpath, write_artifact  # noqa: E402

BATCH_LINES = 100
RSS_CAP = 1.8e9          # self-recycle above this (bytes)
CLAIM_STALE_SEC = 900    # steal a claim whose heartbeat is older than this
NER_URL = "http://127.0.0.1:5051/recognize-entities"


def claim_id(bk: BookKey) -> str:
    """Filesystem-safe, collision-free handle for a book (claim/done markers)."""
    h = hashlib.sha1(f"{bk.source_name}\0{bk.canonical_he_title}".encode("utf-8"))
    return h.hexdigest()


def ner_alive() -> bool:
    try:
        requests.post(NER_URL, json={"text": "בדיקה", "lang": "he"}, timeout=60)
        return True
    except Exception:
        return False


def wait_for_ner(log) -> None:
    while not ner_alive():
        log("NER unreachable; waiting 15s")
        time.sleep(15)


def all_book_keys(con) -> list[BookKey]:
    rows = con.execute(
        "SELECT DISTINCT source_name, canonical_he_title FROM lines_snapshot "
        "ORDER BY source_name, canonical_he_title"
    ).fetchall()
    return [BookKey(s, t) for s, t in rows]


def book_lines(con, bk: BookKey) -> list[tuple[int, str]]:
    return con.execute(
        "SELECT line_index, content FROM lines_snapshot "
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


def process_book(linker, bk, lines, out_path, log, skipped_log):
    """Link one book's lines into a list of LinkRecord (unambiguous only)."""
    records: list[LinkRecord] = []
    words = 0
    for i in range(0, len(lines), BATCH_LINES):
        batch = [(li, c) for li, c in lines[i:i + BATCH_LINES] if c and len(c.strip()) > 1]
        if not batch:
            _maybe_recycle(bk, i, log)
            continue
        words += sum(len(c.split()) for _, c in batch)
        try:
            docs = linker.bulk_link([c for _, c in batch], type_filter="citation")
        except Exception:
            if not ner_alive():
                raise
            docs = []
            for li, c in batch:
                try:
                    docs.append(linker.bulk_link([c], type_filter="citation")[0])
                except Exception as le:
                    docs.append(None)
                    skipped_log(f"{bk.source_name}\t{bk.canonical_he_title}\t{li}\t{type(le).__name__}: {le}")
        for (line_index, content), doc in zip(batch, docs):
            if doc is None:
                continue
            for rr in doc.resolved_refs:
                try:  # a broken citation must cost one link, never the book
                    ref = _pick_ref(rr)
                    if ref is None:
                        continue
                    start, end = rr.raw_entity.span.range
                    records.append(LinkRecord(
                        book_key=bk, line_index=line_index,
                        start=start, end=end, target_ref=ref.normal(),
                    ))
                except Exception as ce:
                    skipped_log(f"{bk.source_name}\t{bk.canonical_he_title}\t{line_index}\tcit\t{type(ce).__name__}: {ce}")
        _maybe_recycle(bk, i, log)
    return records, words


# Bavli-convention flag is read once into a module global by main().
_BAVLI_CONVENTION = False


def _pick_ref(rr):
    """Return the single target Ref for a resolved citation, or None to drop it."""
    if rr.is_ambiguous:
        if not _BAVLI_CONVENTION:
            return None
        cands = [r.ref for r in rr.resolved_raw_refs if r.ref]
        return disambiguate_bavli(cands)
    return rr.ref if rr.ref else None


def _maybe_recycle(bk, i, log):
    if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > RSS_CAP:
        log(f"recycling mid-book={bk.canonical_he_title!r} at batch {i} (book will be redone)")
        os.execv(sys.executable, [sys.executable] + sys.argv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True, help="lines_snapshot.db from dumpLines")
    ap.add_argument("--repo", required=True, help="LinkerToOtzaria repo root (artifacts/ written here)")
    ap.add_argument("--run-dir", required=True, help="transient workdir for claim/done/logs")
    ap.add_argument("--label", default="w1")
    ap.add_argument("--bavli-convention", action="store_true",
                    help="keep Bavli/Yerushalmi & Mishnah/Gemara ambiguities (prefer Bavli/Mishnah); default drops all ambiguous")
    args = ap.parse_args()

    global _BAVLI_CONVENTION
    _BAVLI_CONVENTION = args.bavli_convention

    run = args.run_dir
    for d in ("done", "claim", "logs"):
        os.makedirs(os.path.join(run, d), exist_ok=True)

    def log(msg):
        with open(os.path.join(run, "logs", "progress.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {args.label} {msg}\n")

    def skipped_log(line):
        with open(os.path.join(run, "logs", "skipped_lines.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def try_claim(cid):
        claim = os.path.join(run, "claim", cid)
        hb = os.path.join(claim, "hb")
        try:
            os.mkdir(claim)
        except FileExistsError:
            if os.path.exists(os.path.join(run, "done", cid)):
                return False
            try:
                if time.time() - os.path.getmtime(hb) < CLAIM_STALE_SEC:
                    return False
            except OSError:
                pass
            log(f"stealing stale claim {cid}")
        open(hb, "w").close()
        return True

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")
    django.setup()
    from sefaria.model import library
    linker = library.get_linker("he")

    con = sqlite3.connect(f"file:{args.snapshot}?mode=ro", uri=True)
    books = all_book_keys(con)
    log(f"worker up: {len(books)} books in snapshot, bavli_convention={_BAVLI_CONVENTION}")

    processed = 0
    for bk in books:
        cid = claim_id(bk)
        if os.path.exists(os.path.join(run, "done", cid)):
            continue
        if not try_claim(cid):
            continue
        lines = book_lines(con, bk)
        out_path = os.path.join(args.repo, book_key_to_relpath(bk))
        t0 = time.time()
        try:
            wait_for_ner(log)
            records, words = process_book(linker, bk, lines, out_path, log, skipped_log)
        except Exception as e:
            if not ner_alive():
                log(f"NER outage during {bk.canonical_he_title!r}; waiting then retrying")
                wait_for_ner(log)
                continue  # claim retained; retry this book
            log(f"ERROR {bk.canonical_he_title!r}: {type(e).__name__}: {e}")
            with open(os.path.join(run, "logs", "errors.log"), "a", encoding="utf-8") as ef:
                ef.write(f"{bk.source_name}\t{bk.canonical_he_title}\t{type(e).__name__}: {e}\n")
            open(os.path.join(run, "done", cid), "w").close()  # don't loop forever on a poison book
            continue

        # Atomic per-book output: write the artifact only when the whole book is linked.
        # A book with zero links still gets an (empty) marker via the done file — but we
        # only write an artifact file when there is at least one record, to keep the tree clean.
        if records:
            write_artifact(out_path, records)
        elif os.path.exists(out_path):
            os.remove(out_path)  # a previously-linked book that now yields nothing
        open(os.path.join(run, "done", cid), "w").close()
        processed += 1
        log(f"done {bk.source_name}/{bk.canonical_he_title!r} lines={len(lines)} words={words} links={len(records)} {time.time()-t0:.1f}s")

    log(f"no more books (processed {processed} this life); exiting")


if __name__ == "__main__":
    main()
