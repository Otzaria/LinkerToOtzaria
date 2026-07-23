#!/usr/bin/env python3
"""Safely extract a checksum-pinned raw-NER tar.zst handoff."""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("checksum")
    parser.add_argument("destination")
    args = parser.parse_args()
    archive = pathlib.Path(args.archive)
    checksum = pathlib.Path(args.checksum).read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", checksum) or sha256(archive) != checksum:
        raise SystemExit("raw-NER archive checksum mismatch")
    destination = pathlib.Path(args.destination)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary_name:
        temporary = pathlib.Path(temporary_name)
        process = subprocess.Popen(["zstd", "-q", "-dc", archive], stdout=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as stream:
                names = set()
                count = 0
                for member in stream:
                    path = pathlib.PurePosixPath(member.name)
                    if (member.name in names or path.is_absolute() or not path.parts
                            or ".." in path.parts or path.parts[0] not in {"ner_manifest.json", "ner-data"}
                            or not (member.isdir() or member.isfile())):
                        raise SystemExit(f"unsafe/duplicate raw-NER member: {member.name!r}")
                    if path.parts[0] == "ner_manifest.json" and (
                            len(path.parts) != 1 or not member.isfile()):
                        raise SystemExit("ner_manifest.json must be one regular root file")
                    names.add(member.name)
                    count += 1
                    stream.extract(member, path=temporary, filter="data")
        finally:
            if process.stdout:
                process.stdout.close()
            if process.wait() != 0:
                raise SystemExit("raw-NER decompression failed")
        if count < 2 or "ner_manifest.json" not in names or not (temporary / "ner-data").is_dir():
            raise SystemExit("raw-NER archive is incomplete")
        shutil.rmtree(destination, ignore_errors=True)
        temporary.rename(destination)


if __name__ == "__main__":
    main()
