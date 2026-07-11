"""Mac helper worker: a single local linker worker that contributes to the Oracle run.

It links books on this (fast, M-series) Mac and pushes every finished book straight to
Oracle's ledger via mac_coord.py, so the overall bootstrap finishes sooner. Two sources,
in priority order each loop:
  1. FAILED books on Oracle (books the main run OOM'd on) — re-linked here with more RAM.
  2. QUEUE books — claimed from the FAR END of Oracle's ordered list (Oracle's own workers
     consume the near end), so the two machines never collide.

Safety: an RSS watchdog kills any book that exceeds CAP_GB (this Mac is 16GB and in daily
use — a runaway book must never freeze it). A queue book killed here is released back to
Oracle; a failed book that also dies here is marked exhausted locally and reported, never
retried in a loop. A claim heartbeat keeps Oracle from stealing a slow book mid-link.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from linker_artifact import BookKey, book_key_to_relpath  # noqa: E402

SSH = ["ssh", "-o", "BatchMode=yes", "otzaria"]
COORD = ["sudo", "-iu", "LinkerToOtzaria", "python3", "/home/LinkerToOtzaria/mac_coord.py"]

# Instance id: run several instances (mac_worker.py 1, mac_worker.py 2, ...) to use more of the
# Mac's cores. Each gets its OWN local run-dir/only.json/label/log; cross-instance (and cross-machine)
# book coordination is handled by Oracle's claim ledger via mac_coord pick, so no collisions.
INST = sys.argv[1] if len(sys.argv) > 1 else "1"
LABEL = "mac" + INST

REPO = "/Users/david/Documents/otzaria-books/LinkerToOtzaria"
SEF = "/Users/david/Documents/otzaria-books/sefaria-linker-poc/Sefaria-Project"
PY = SEF + "/.venv/bin/python"
SNAP = "/Users/david/Documents/linker-bootstrap/lines_snapshot_current.db"
MACRUN = "/Users/david/Documents/linker-bootstrap/mac-run-" + INST
STATE = "/Users/david/Documents/linker-bootstrap/mac-state"
CAP_KB = 4 * 1024 * 1024           # 4GB RSS watchdog per instance. Tightened after a 4-instance run
                                   # overran this 16GB Mac (several big books loading at once between
                                   # watchdog polls spiked past 16GB → kernel reboot). With 2 instances
                                   # at 4GB the aggregate ceiling stays well under RAM; big books that
                                   # exceed this are released back to Oracle, the safer place for them.
HEARTBEAT_EVERY = 60               # refresh Oracle claim every 60s while linking (< 900s stale)

os.makedirs(MACRUN, exist_ok=True)
os.makedirs(os.path.join(STATE, "exhausted"), exist_ok=True)
os.makedirs(os.path.join(STATE, "logs"), exist_ok=True)
LOG = os.path.join(STATE, "logs", "mac_worker_" + INST + ".log")


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def coord(args, stdin=None):
    r = subprocess.run(SSH + COORD + args, input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"coord {args} rc={r.returncode} err={r.stderr.strip()[:200]}")
    return r.stdout.strip()


def rss_kb(pid):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return int(out) if out else 0
    except Exception:
        return 0


def link_one(bk_dict):
    """Link a single book locally under the watchdog. Returns relpath on success, None on failure."""
    cid = bk_dict["cid"]
    bk = BookKey(bk_dict["source_name"], bk_dict["canonical_he_title"])
    for d in ("done", "claim", "failed"):
        p = os.path.join(MACRUN, d, cid)
        if os.path.isdir(p):
            import shutil
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)
    only = os.path.join(MACRUN, "only.json")
    with open(only, "w", encoding="utf-8") as f:
        json.dump([{"source_name": bk.source_name, "canonical_he_title": bk.canonical_he_title}], f, ensure_ascii=False)
    env = dict(os.environ, PYTHONPATH=SEF, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               MKL_NUM_THREADS="1", VECLIB_MAXIMUM_THREADS="1", DJANGO_SETTINGS_MODULE="sefaria.settings")
    proc = subprocess.Popen(
        [PY, os.path.join(REPO, "src", "link_books.py"), "--snapshot", SNAP, "--repo", REPO,
         "--run-dir", MACRUN, "--only-books", only, "--label", LABEL],
        cwd=SEF, env=env, stdout=open(os.path.join(STATE, "logs", "link_" + INST + ".log"), "a"), stderr=subprocess.STDOUT)
    killed = False
    last_hb = 0.0
    while proc.poll() is None:
        time.sleep(3)
        if rss_kb(proc.pid) > CAP_KB:
            proc.kill()
            killed = True
            log(f"WATCHDOG killed {bk.canonical_he_title!r} (RSS>{CAP_KB//1024//1024}GB)")
            break
        now = time.time()
        if now - last_hb > HEARTBEAT_EVERY:
            coord(["hb", cid])
            last_hb = now
    proc.wait()
    if killed or not os.path.exists(os.path.join(MACRUN, "done", cid)):
        return None
    relpath = book_key_to_relpath(bk)
    art = os.path.join(REPO, relpath)
    content = open(art, encoding="utf-8").read() if os.path.exists(art) else None
    coord(["commit"], stdin=json.dumps({"cid": cid, "relpath": relpath, "content": content}, ensure_ascii=False))
    return relpath


def main():
    log(f"mac_worker[{INST}] up: label={LABEL} snapshot={SNAP} cap={CAP_KB//1024//1024}GB")
    idle = 0
    while True:
        # 1. Oracle failed books first (main run couldn't do them) — link here with more RAM.
        failed = json.loads(coord(["failed"]) or "[]")
        did = False
        for bk in failed:
            cid = __import__("hashlib").sha1(f"{bk['source_name']}\0{bk['canonical_he_title']}".encode()).hexdigest()
            if os.path.exists(os.path.join(STATE, "exhausted", cid)):
                continue
            bk["cid"] = cid
            t0 = time.time()
            rel = link_one(bk)
            did = True
            if rel is None:
                open(os.path.join(STATE, "exhausted", cid), "w").close()
                log(f"EXHAUSTED (failed on Mac too) {bk['source_name']}/{bk['canonical_he_title']!r} — reported")
            else:
                log(f"RECOVERED failed {bk['source_name']}/{bk['canonical_he_title']!r} {time.time()-t0:.0f}s -> {rel}")
        if did:
            continue
        # 2. Queue book from the far end.
        picked = coord(["pick"])
        if not picked or picked == "NONE":
            idle += 1
            if idle % 20 == 1:
                log("queue empty / all claimed — idling")
            time.sleep(30)
            continue
        idle = 0
        bk = json.loads(picked)
        t0 = time.time()
        rel = link_one(bk)
        if rel is None:
            coord(["release", bk["cid"]])
            log(f"mac-fail queue {bk['source_name']}/{bk['canonical_he_title']!r} — released to Oracle")
        else:
            log(f"done queue {bk['source_name']}/{bk['canonical_he_title']!r} {time.time()-t0:.0f}s -> {rel}")


if __name__ == "__main__":
    main()
