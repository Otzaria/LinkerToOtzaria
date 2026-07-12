"""Mac-side orchestrator that drives the compute-only server as extra linker capacity.

The Mac is the hub: it already has the snapshot AND Oracle access, so the third-party
server never touches Oracle. Each of N threads keeps one warm `link_daemon.py` open on the
server over an SSH pipe and, in a loop:
  1. claims a book from Oracle's queue (mac_coord pick),
  2. reads that book's lines from the Mac's LOCAL snapshot,
  3. ships them to its server daemon, gets the finished artifact back,
  4. commits the artifact to Oracle (mac_coord commit).
A daemon that dies (e.g. OOM on a giant book) breaks its pipe → the thread releases the
book back to Oracle and restarts the daemon. So the server is pure isolated compute.
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linker_artifact import BookKey, book_key_to_relpath  # noqa: E402

SSH_ORACLE = ["ssh", "-o", "BatchMode=yes", "otzaria"]
COORD = ["sudo", "-iu", "LinkerToOtzaria", "python3", "/home/LinkerToOtzaria/mac_coord.py"]
SSH_SERVER = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "otzaria@109.199.98.195"]
# Each daemon is tagged `wid=<N>` in its argv (link_daemon ignores it) so a thread can reap its
# OWN orphan before spawning a fresh one — a dropped SSH (network blip) leaves the remote python
# alive, so without this daemons accumulate past N. 6GB virtual cap so a runaway book kills only
# its own daemon (pipe breaks → book released). OMP=1 keeps each daemon single-threaded.
def daemon_cmd(wid):
    return ("ulimit -v 7340032; cd /home/otzaria/linker/Sefaria-Project && "
            "PYTHONPATH=. OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 "
            f"exec /home/otzaria/linker/Sefaria-Project/.venv/bin/python "
            f"/home/otzaria/linker/link_daemon.py wid={wid}")
SNAP = "/Users/david/Documents/linker-bootstrap/lines_snapshot_current.db"
REPO = "/Users/david/Documents/otzaria-books/LinkerToOtzaria"  # local Mac copy of every server artifact
STATE_LOG = "/Users/david/Documents/linker-bootstrap/mac-state/logs/server_orchestrator.log"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

_loglock = threading.Lock()


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    with _loglock:
        with open(STATE_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)


def coord(args, stdin=None):
    r = subprocess.run(SSH_ORACLE + COORD + args, input=stdin, capture_output=True, text=True)
    return r.stdout.strip()


def save_local(relpath, content):
    """Write every server-produced artifact to the Mac's local repo immediately (atomically),
    so a copy always lands on a machine we control the moment it's linked — independent of the
    Oracle commit and safe against the server dying."""
    if content is None:
        return
    dest = os.path.join(REPO, relpath)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = f"{dest}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, dest)


def book_lines(con, bk):
    rows = con.execute(
        "SELECT line_index, content FROM lines_snapshot WHERE source_name=? AND canonical_he_title=? "
        "ORDER BY line_index", (bk.source_name, bk.canonical_he_title)).fetchall()
    return [[li, c] for li, c in rows]


def worker(wid):
    con = sqlite3.connect(f"file:{SNAP}?mode=ro", uri=True)
    while True:
        # reap this thread's own orphaned daemon (survives a dropped SSH) before starting a fresh one
        subprocess.run(SSH_SERVER + [f"pkill -9 -f 'link_daemon.py wid={wid}$'"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = subprocess.Popen(SSH_SERVER + [daemon_cmd(wid)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        ready = False
        for _ in range(200):
            l = proc.stdout.readline()
            if not l:
                break
            if l.strip() == "READY":
                ready = True
                break
        if not ready:
            log(f"[{wid}] daemon failed to start; retry in 10s")
            try:
                proc.kill()
            except Exception:
                pass
            time.sleep(10)
            continue
        log(f"[{wid}] daemon ready")
        restart = False
        while not restart:
            picked = coord(["pick"])
            if not picked or picked == "NONE":
                if proc.poll() is not None:
                    break
                time.sleep(20)
                continue
            bkj = json.loads(picked)
            bk = BookKey(bkj["source_name"], bkj["canonical_he_title"])
            cid = bkj["cid"]
            lines = book_lines(con, bk)
            t0 = time.time()
            try:
                proc.stdin.write(json.dumps({"source_name": bk.source_name,
                                             "canonical_he_title": bk.canonical_he_title,
                                             "lines": lines}, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                resp = proc.stdout.readline()
                if not resp:
                    raise RuntimeError("daemon closed pipe (likely OOM)")
                r = json.loads(resp)
            except Exception as e:  # noqa: BLE001
                coord(["release", cid])
                log(f"[{wid}] daemon died on {bk.canonical_he_title!r} ({e}); released, restarting")
                try:
                    proc.kill()
                except Exception:
                    pass
                restart = True
                break
            if not r.get("ok"):
                coord(["release", cid])
                log(f"[{wid}] link error {bk.canonical_he_title!r}: {r.get('error')}")
                continue
            relpath = book_key_to_relpath(bk)
            save_local(relpath, r["content"])  # local Mac copy first — always land it on a machine we control
            coord(["commit"], stdin=json.dumps({"cid": cid, "relpath": relpath, "content": r["content"]}, ensure_ascii=False))
            log(f"[{wid}] done {bk.source_name}/{bk.canonical_he_title!r} n={r['n']} {time.time()-t0:.0f}s")
        time.sleep(3)


def main():
    os.makedirs(os.path.dirname(STATE_LOG), exist_ok=True)
    log(f"server_orchestrator up: {N} daemons -> otzaria@109.199.98.195")
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(1, N + 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
