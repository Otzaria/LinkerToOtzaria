#!/usr/bin/env python3
import hashlib
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
sidecar = pathlib.Path(sys.argv[2])
destination = pathlib.Path(sys.argv[3])
expected = sidecar.read_text(encoding="ascii").strip()
if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
    raise SystemExit("invalid publisher handoff digest sidecar")
if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
    raise SystemExit("publisher handoff digest mismatch")
allowed_roots = {"linker_links.zst", "linker_links.zst.sha256", "relink_manifest.json", "baseline", "meta.json"}
with tarfile.open(archive, "r:") as bundle:
    members = bundle.getmembers()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit(f"unsafe publisher handoff path: {member.name}")
        if path.parts[0] not in allowed_roots or member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"unexpected publisher handoff member: {member.name}")
    destination.mkdir(parents=True, exist_ok=True)
    bundle.extractall(destination, members=members, filter="data")
required = ["linker_links.zst", "linker_links.zst.sha256", "relink_manifest.json", "baseline", "meta.json"]
for name in required:
    if not (destination / name).exists():
        raise SystemExit(f"publisher handoff is missing {name}")
