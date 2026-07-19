"""Acceptance smoke for the relocatable, content-addressed runtime output."""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

EXPECTED = "bacad1b486c2bb392ee786bcc35b27dcc2beb17ea90b05f47352a06e44c8ff43"
INPUT_ROOT = Path("/kaggle/input")
ARCHIVE_NAME = "linker-python-runtime-v1.tar.zst"
DESTINATION = Path("/kaggle/temp/linker-runtime-smoke")


def run(*argv: str | os.PathLike[str], capture: bool = False) -> str:
    print("+", " ".join(map(str, argv)), flush=True)
    result = subprocess.run(
        [str(item) for item in argv], check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
        env={key: value for key, value in os.environ.items() if key not in {
            "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"
        }},
    )
    return result.stdout if capture else ""


print("input tree:", [str(path) for path in list(INPUT_ROOT.rglob("*"))[:100]], flush=True)
archives = list(INPUT_ROOT.rglob(ARCHIVE_NAME))
if len(archives) != 1:
    raise SystemExit(f"expected one attached {ARCHIVE_NAME}, found {archives!r}")
ARCHIVE = archives[0]
SOURCE = ARCHIVE.parent

run("apt-get", "update", "-qq")
run("apt-get", "install", "-y", "-qq", "zstd", "python3.12")
digest = hashlib.sha256()
with ARCHIVE.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != EXPECTED:
    raise SystemExit(f"runtime digest mismatch: {digest.hexdigest()} != {EXPECTED}")

shutil.rmtree(DESTINATION, ignore_errors=True)
DESTINATION.mkdir(parents=True)
tar_path = DESTINATION / "runtime.tar"
with tar_path.open("wb") as output:
    subprocess.run(["zstd", "-q", "-dc", ARCHIVE], stdout=output, check=True)
with tarfile.open(tar_path, "r:") as stream:
    for member in stream.getmembers():
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe runtime member: {member.name!r}")
        if member.issym():
            target = Path(member.linkname)
            safe_relative = bool(target.parts) and not target.is_absolute() and ".." not in target.parts
            if not safe_relative and member.linkname != "/usr/bin/python3.12":
                raise SystemExit(f"unsafe runtime symlink: {member.name!r} -> {member.linkname!r}")
run("tar", "-xf", tar_path, "-C", DESTINATION)
tar_path.unlink()
manifest = json.loads((DESTINATION / "runtime-manifest.json").read_text())
external = json.loads((SOURCE / "linker-python-runtime-v1.manifest.json").read_text())
for key, value in manifest.items():
    if external.get(key) != value:
        raise SystemExit(f"external/internal manifest mismatch: {key}")

for name, freeze_name in (("sefaria-venv", "sefaria.txt"), ("gpu-venv", "gpu-server.txt")):
    python = DESTINATION / name / "bin/python"
    run(python, "-m", "pip", "check")
    actual = run(python, "-m", "pip", "freeze", "--all", capture=True)
    expected = (DESTINATION / "freeze" / freeze_name).read_text()
    if actual != expected:
        raise SystemExit(f"relocated package inventory mismatch: {name}")

run(DESTINATION / "sefaria-venv/bin/python", "-c",
    "import django,numpy,spacy; print(django.get_version(),numpy.__version__,spacy.__version__)")
run(DESTINATION / "gpu-venv/bin/python", "-c",
    "import flask,gunicorn,spacy,torch,transformers; print(torch.__version__,torch.cuda.is_available())")
run(DESTINATION / "gpu-venv/bin/python", "-m", "gunicorn.app.wsgiapp", "--version")
print("CONTENT_ADDRESSED_RUNTIME_SMOKE_OK", flush=True)
