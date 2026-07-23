#!/usr/bin/env python3
"""Safely extract a checksum-pinned resumable NER checkpoint."""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile

from unpack_ner_handoff import sha256


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: unpack_ner_checkpoint.py ARCHIVE CHECKSUM DESTINATION")
    archive = pathlib.Path(sys.argv[1])
    expected = pathlib.Path(sys.argv[2]).read_text(encoding="ascii").strip()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise SystemExit("invalid NER checkpoint checksum")
    if sha256(archive) != expected:
        raise SystemExit("NER checkpoint checksum mismatch")
    destination = pathlib.Path(sys.argv[3])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary_name:
        temporary = pathlib.Path(temporary_name)
        process = subprocess.Popen(["zstd", "-q", "-dc", archive], stdout=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as stream:
                names = set()
                for member in stream:
                    path = pathlib.PurePosixPath(member.name)
                    if (
                        member.name in names
                        or path.is_absolute()
                        or not path.parts
                        or ".." in path.parts
                        or path.parts[0] not in {
                            "checkpoint.json", "ner-data", "done", "failed", "partial"
                        }
                        or not (member.isdir() or member.isfile())
                    ):
                        raise SystemExit(
                            f"unsafe/duplicate NER checkpoint member: {member.name!r}"
                        )
                    if path.parts[0] == "checkpoint.json" and (
                        len(path.parts) != 1 or not member.isfile()
                    ):
                        raise SystemExit("checkpoint.json must be one regular root file")
                    names.add(member.name)
                    stream.extract(member, path=temporary, filter="data")
        finally:
            if process.stdout:
                process.stdout.close()
            if process.wait() != 0:
                raise SystemExit("NER checkpoint decompression failed")
        if (
            "checkpoint.json" not in names
            or not (temporary / "ner-data").is_dir()
            or not (temporary / "done").is_dir()
            or not (temporary / "failed").is_dir()
        ):
            raise SystemExit("NER checkpoint archive is incomplete")
        # Checkpoints written before batch-level resume have no partial/ root.
        # They still contain valid completed-book progress; upgrade them in place.
        (temporary / "partial").mkdir(exist_ok=True)
        shutil.rmtree(destination, ignore_errors=True)
        temporary.rename(destination)


if __name__ == "__main__":
    main()
