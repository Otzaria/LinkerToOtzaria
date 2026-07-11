"""Oracle-side coordinator for the Mac helper worker. Runs as user LinkerToOtzaria.
The Mac claims books from the FAR END of the same ordered list Oracle's workers consume
from the near end, links them locally, and commits results back here. All state lives in
Oracle's ~/run ledger, so the Mac is just a remote worker — no disruption to the main run.

Subcommands:
  failed            -> JSON list of currently-failed book_keys (for the Mac to re-link with more RAM)
  pick              -> claim + print the next queue book (reverse order, not done/claimed), or NONE
  hb <cid>          -> refresh a claim heartbeat while the Mac links a slow book
  release <cid>     -> drop a claim (Mac gave up / watchdog killed) so Oracle can take it
  commit            -> stdin JSON {cid, relpath, content|null}: write artifact + done, clear failed/claim
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

RUN = os.path.expanduser("~/run")
REPO = os.path.expanduser("~/LinkerToOtzaria")
SNAP = os.path.expanduser("~/inputs/lines_snapshot.db")
STALE = 900


def cid_of(s, t):
    return hashlib.sha1(f"{s}\0{t}".encode("utf-8")).hexdigest()


def cmd_failed():
    out = []
    fd = os.path.join(RUN, "failed")
    if os.path.isdir(fd):
        for n in os.listdir(fd):
            p = os.path.join(fd, n)
            if os.path.isfile(p):
                try:
                    out.append(json.load(open(p, encoding="utf-8")))
                except Exception:
                    pass
    print(json.dumps(out, ensure_ascii=False))


def cmd_pick():
    con = sqlite3.connect(f"file:{SNAP}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT DISTINCT source_name, canonical_he_title FROM lines_snapshot "
        "ORDER BY source_name, canonical_he_title DESC"
    ).fetchall()
    for s, t in rows:
        c = cid_of(s, t)
        if os.path.exists(os.path.join(RUN, "done", c)):
            continue
        claim = os.path.join(RUN, "claim", c)
        if os.path.exists(claim):
            hb = os.path.join(claim, "hb")
            try:
                if time.time() - os.path.getmtime(hb) < STALE:
                    continue
            except OSError:
                continue
        try:
            os.makedirs(claim, exist_ok=False)
        except FileExistsError:
            continue
        open(os.path.join(claim, "hb"), "w").close()
        print(json.dumps({"source_name": s, "canonical_he_title": t, "cid": c}, ensure_ascii=False))
        return
    print("NONE")


def cmd_hb(c):
    hb = os.path.join(RUN, "claim", c, "hb")
    try:
        os.utime(hb, None)
    except OSError:
        try:
            open(hb, "w").close()
        except OSError:
            pass


def cmd_release(c):
    shutil.rmtree(os.path.join(RUN, "claim", c), ignore_errors=True)


def cmd_commit():
    p = json.load(sys.stdin)
    c = p["cid"]
    dest = os.path.join(REPO, p["relpath"])
    content = p.get("content")
    if content is not None:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, dest)
    elif os.path.exists(dest):
        os.remove(dest)
    open(os.path.join(RUN, "done", c), "w").close()
    try:
        os.remove(os.path.join(RUN, "failed", c))
    except OSError:
        pass
    shutil.rmtree(os.path.join(RUN, "claim", c), ignore_errors=True)
    print("OK")


cmd = sys.argv[1] if len(sys.argv) > 1 else ""
{"failed": cmd_failed, "pick": cmd_pick,
 "hb": lambda: cmd_hb(sys.argv[2]), "release": lambda: cmd_release(sys.argv[2]),
 "commit": cmd_commit}.get(cmd, lambda: sys.exit(f"unknown cmd {cmd!r}"))()
