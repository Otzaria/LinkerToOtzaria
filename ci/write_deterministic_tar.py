#!/usr/bin/env python3
"""Write a deterministic regular-file/directory tar stream to stdout."""
from __future__ import annotations

import argparse
import pathlib
import sys
import tarfile


def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 1577836800  # 2020-01-01 UTC
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("members", nargs="+")
    args = parser.parse_args()
    root = pathlib.Path(args.root).resolve()
    selected = []
    for name in args.members:
        relative = pathlib.PurePosixPath(name)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise SystemExit(f"unsafe member path: {name!r}")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise SystemExit(f"member escapes root: {name!r}") from error
        if not path.exists():
            raise SystemExit(f"missing member: {name!r}")
        selected.append((path, str(relative)))

    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for path, arcname in selected:
            candidates = [path]
            if path.is_dir():
                candidates += sorted(path.rglob("*"))
            for candidate in candidates:
                if candidate.is_symlink() or not (candidate.is_dir() or candidate.is_file()):
                    raise SystemExit(f"unsupported member type: {candidate}")
                name = arcname if candidate == path else str(pathlib.PurePosixPath(arcname) / candidate.relative_to(path))
                archive.add(candidate, arcname=name, recursive=False, filter=normalized)


if __name__ == "__main__":
    main()
