"""Warm linker daemon (runs on a compute-only server, e.g. the 8-core x86 box).

Keeps `get_linker('he')` loaded once (the ~30s startup is paid a single time) and links
one book per request. The Mac orchestrator drives it over an SSH pipe: it feeds a book's
lines on stdin and reads the finished artifact back on stdout. The daemon NEVER talks to
Oracle — all queue/claim/commit coordination stays on the Mac (which already has Oracle
access), so this untrusted third-party server is fully isolated.

Protocol (one JSON object per line):
  in : {"source_name":..,"canonical_he_title":..,"lines":[[line_index,content],..]}
  out: {"ok":true,"content":<artifact jsonl text|null>,"words":N,"n":<#records>}
       {"ok":false,"error":".."}
The first stdout line is literally `READY` once the library is loaded.
"""
import json
import os
import sys
import tempfile

# The Sefaria linker logs verbose INFO ("Querying DH…") to stdout, which would corrupt our
# one-JSON-per-line protocol. So reserve the ORIGINAL stdout (the pipe to the Mac) as a private
# protocol channel and redirect fd1 -> stderr, sending every library print/log to stderr instead.
_prot = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

# link_books.py + linker_artifact.py live in the sibling src/ dir on the server
# (daemon at ~/linker/link_daemon.py, engine at ~/linker/src/).
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_here, "src"), _here):
    if os.path.isfile(os.path.join(_p, "link_books.py")):
        sys.path.insert(0, _p)
        break
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")

import django  # noqa: E402
django.setup()
from sefaria.model import library  # noqa: E402
import link_books as lb  # noqa: E402  (reuse process_book + NER_URL=127.0.0.1:5051)
from linker_artifact import BookKey, write_artifact  # noqa: E402

linker = library.get_linker("he")
_prot.write("READY\n")
_prot.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
        bk = BookKey(req["source_name"], req["canonical_he_title"])
        blines = [(int(li), c) for li, c in req["lines"]]
        records, words = lb.process_book(linker, bk, blines, lambda _x: None, lambda: None)
        if records:
            tmp = tempfile.mktemp(suffix=".jsonl")
            write_artifact(tmp, records)
            with open(tmp, encoding="utf-8") as fh:
                content = fh.read()
            os.remove(tmp)
        else:
            content = None
        _prot.write(json.dumps({"ok": True, "content": content, "words": words, "n": len(records)}) + "\n")
    except Exception as e:  # noqa: BLE001
        _prot.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}) + "\n")
    _prot.flush()
