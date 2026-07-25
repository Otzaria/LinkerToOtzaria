#!/usr/bin/env python3
"""Validate the cross-platform resolver runtime lock.

Kaggle ships a content-addressed x86_64 runtime.  The ARM resolver cannot use
those wheels, but it must resolve the same Python package versions.  This helper
binds the committed ARM-installable Sefaria freeze to the exact runtime manifest
that produced Kaggle's NER handoff and emits the canonical combined runtime id.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


HEX64 = re.compile(r"[0-9a-f]{64}")
MANIFEST_KEYS = {
    "builder_sha256",
    "gpu_server_commit",
    "gpu_server_freeze_sha256",
    "gpu_server_identity",
    "gpu_server_requirements_sha256",
    "machine",
    "platform",
    "python_version",
    "schema_version",
    "sefaria_commit",
    "sefaria_freeze_sha256",
    "sefaria_identity",
    "sefaria_requirements_sha256",
}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def validate(
    manifest_path: Path,
    sefaria_freeze: Path,
    sefaria_repo: Path,
    gpu_repo: Path,
    python_version: str,
) -> str:
    with manifest_path.open(encoding="utf-8") as stream:
        manifest = json.load(stream, object_pairs_hook=_unique_object)
    if type(manifest) is not dict or set(manifest) != MANIFEST_KEYS:
        raise RuntimeError("runtime lock manifest has an invalid key set")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise RuntimeError("runtime lock manifest has an unsupported schema_version")
    for key in (
        "builder_sha256",
        "gpu_server_freeze_sha256",
        "gpu_server_requirements_sha256",
        "sefaria_freeze_sha256",
        "sefaria_requirements_sha256",
    ):
        if type(manifest[key]) is not str or not HEX64.fullmatch(manifest[key]):
            raise RuntimeError(f"runtime lock {key} is not a sha256")
    for key in ("gpu_server_commit", "sefaria_commit"):
        if type(manifest[key]) is not str or not re.fullmatch(r"[0-9a-f]{40}", manifest[key]):
            raise RuntimeError(f"runtime lock {key} is not a commit")
    if manifest["python_version"] != python_version:
        raise RuntimeError(
            f"resolver Python differs from the Kaggle runtime lock: "
            f"{python_version!r} != {manifest['python_version']!r}"
        )
    checks = (
        ("sefaria_commit", git_head(sefaria_repo)),
        ("gpu_server_commit", git_head(gpu_repo)),
        ("sefaria_requirements_sha256", sha256_file(sefaria_repo / "requirements.txt")),
        (
            "gpu_server_requirements_sha256",
            sha256_file(gpu_repo / "app" / "requirements.txt"),
        ),
        ("sefaria_freeze_sha256", sha256_file(sefaria_freeze)),
    )
    for key, actual in checks:
        if manifest[key] != actual:
            raise RuntimeError(
                f"runtime lock {key} differs from the pinned source: "
                f"{actual} != {manifest[key]}"
            )
    combined = hashlib.sha256(
        (
            manifest["sefaria_freeze_sha256"]
            + "\n"
            + manifest["gpu_server_freeze_sha256"]
            + "\n"
        ).encode()
    ).hexdigest()
    return combined[:16]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sefaria-freeze", type=Path, required=True)
    parser.add_argument("--sefaria-repo", type=Path, required=True)
    parser.add_argument("--gpu-repo", type=Path, required=True)
    parser.add_argument("--python-version", required=True)
    args = parser.parse_args()
    print(
        validate(
            args.manifest,
            args.sefaria_freeze,
            args.sefaria_repo,
            args.gpu_repo,
            args.python_version,
        )
    )


if __name__ == "__main__":
    main()
