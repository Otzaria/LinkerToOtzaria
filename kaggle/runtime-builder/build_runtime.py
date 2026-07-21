"""Build the immutable Python runtime consumed by ephemeral linker kernels.

This is deliberately a Kaggle *kernel output*, rather than a Kaggle dataset:
large private datasets can remain stuck in Kaggle's dataset processing step,
while a completed private kernel output is directly attachable as a
``kernel_source``.  Consumers still pin and verify the exact archive SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path


SEFARIA_COMMIT = "59291d9f8754de0062711bc1cd49214b0e618fc5"
GPU_SERVER_COMMIT = "d9da16119e9c0de64224d7b187cd999b2ead4bad"
SCHEMA_VERSION = 1
ARCHIVE_NAME = "linker-python-runtime-v1.tar.zst"

WORKING = Path("/kaggle/working")
BUILD = Path("/kaggle/temp/linker-python-runtime-build")
PYTHON = Path("/usr/bin/python3.12")


def run(*argv: str | os.PathLike[str], cwd: Path | None = None, capture: bool = False) -> str:
    printable = " ".join(map(str, argv))
    print(f"+ {printable}", flush=True)
    result = subprocess.run(
        [str(value) for value in argv],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        env={key: value for key, value in os.environ.items() if key not in {
            "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"
        }},
    )
    return result.stdout.strip() if capture else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clone_exact(url: str, commit: str, destination: Path) -> None:
    run("git", "init", "-q", destination)
    run("git", "-C", destination, "remote", "add", "origin", url)
    run("git", "-C", destination, "fetch", "-q", "--depth=1", "origin", commit)
    run("git", "-C", destination, "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")
    actual = run("git", "-C", destination, "rev-parse", "HEAD", capture=True)
    if actual != commit:
        raise SystemExit(f"checkout mismatch for {url}: {actual} != {commit}")


def create_venv(destination: Path) -> None:
    run(PYTHON, "-m", "venv", destination)
    run(destination / "bin/pip", "install", "--no-cache-dir", "--upgrade", "pip")


def freeze(venv: Path, output: Path) -> str:
    resolved = run(venv / "bin/pip", "freeze", "--all", capture=True) + "\n"
    output.write_text(resolved, encoding="utf-8")
    return hashlib.sha256(resolved.encode()).hexdigest()


def main() -> None:
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    WORKING.mkdir(parents=True, exist_ok=True)

    run("apt-get", "update", "-qq")
    run(
        "apt-get", "install", "-y", "-qq",
        "ca-certificates", "git", "zstd", "python3.12", "python3.12-venv",
    )
    if not PYTHON.exists():
        raise SystemExit(f"expected interpreter is missing: {PYTHON}")
    # Kaggle's sitecustomize imports kaggle_gcp for google.cloud; the production
    # bootstrap removes it for the same reason before either environment starts.
    for customizer in Path("/usr/lib").glob("python3.*/sitecustomize.py"):
        customizer.unlink()

    sources = BUILD / "sources"
    sources.mkdir()
    sefaria = sources / "Sefaria-Project"
    gpu = sources / "gpu-server"
    clone_exact("https://github.com/Sefaria/Sefaria-Project", SEFARIA_COMMIT, sefaria)
    clone_exact("https://github.com/Sefaria/gpu-server", GPU_SERVER_COMMIT, gpu)

    python_version = run(PYTHON, "--version", capture=True)
    sef_requirements = sefaria / "requirements.txt"
    gpu_requirements = gpu / "app/requirements.txt"
    sef_requirements_sha = sha256(sef_requirements)
    gpu_requirements_sha = sha256(gpu_requirements)

    sef_venv = BUILD / "sefaria-venv"
    gpu_venv = BUILD / "gpu-venv"
    create_venv(sef_venv)
    run(sef_venv / "bin/pip", "install", "--no-cache-dir", "-r", sef_requirements)
    run(sef_venv / "bin/pip", "install", "--no-cache-dir", "numpy<2")
    (sef_venv / ".identity").write_text(
        f"{python_version}:{sef_requirements_sha[:12]}:{SEFARIA_COMMIT}", encoding="ascii"
    )

    create_venv(gpu_venv)
    run(
        gpu_venv / "bin/pip", "install", "--no-cache-dir",
        "-r", gpu_requirements, "gunicorn",
    )
    (gpu_venv / ".identity").write_text(
        f"{python_version}:{gpu_requirements_sha[:12]}:{GPU_SERVER_COMMIT}", encoding="ascii"
    )

    run(sef_venv / "bin/pip", "check")
    run(gpu_venv / "bin/pip", "check")
    run(sef_venv / "bin/python", "-c", "import django,numpy,spacy; print(django.get_version(),numpy.__version__,spacy.__version__)")
    run(gpu_venv / "bin/python", "-c", "import flask,gunicorn,spacy,torch,transformers; print(torch.__version__,torch.cuda.is_available())")

    freeze_dir = BUILD / "freeze"
    freeze_dir.mkdir()
    sef_freeze_sha = freeze(sef_venv, freeze_dir / "sefaria.txt")
    gpu_freeze_sha = freeze(gpu_venv, freeze_dir / "gpu-server.txt")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "python_version": python_version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "sefaria_commit": SEFARIA_COMMIT,
        "sefaria_requirements_sha256": sef_requirements_sha,
        "sefaria_freeze_sha256": sef_freeze_sha,
        "sefaria_identity": (sef_venv / ".identity").read_text(encoding="ascii"),
        "gpu_server_commit": GPU_SERVER_COMMIT,
        "gpu_server_requirements_sha256": gpu_requirements_sha,
        "gpu_server_freeze_sha256": gpu_freeze_sha,
        "gpu_server_identity": (gpu_venv / ".identity").read_text(encoding="ascii"),
        "builder_sha256": sha256(Path(__file__)),
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (BUILD / "runtime-manifest.json").write_bytes(manifest_bytes)

    # Cached bytecode bakes builder paths/timestamps into the payload and is safe
    # to regenerate after extraction. Excluding it also cuts upload and extract time.
    for cache in BUILD.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache)
    for bytecode in BUILD.rglob("*.py[co]"):
        bytecode.unlink()

    tar_path = BUILD / "linker-python-runtime-v1.tar"
    run(
        "tar", "--sort=name", "--mtime=@0", "--owner=0", "--group=0", "--numeric-owner",
        "-C", BUILD, "-cf", tar_path,
        "sefaria-venv", "gpu-venv", "freeze", "runtime-manifest.json",
    )
    archive = WORKING / ARCHIVE_NAME
    archive.unlink(missing_ok=True)
    run("zstd", "-T0", "-10", "-f", tar_path, "-o", archive)
    run("zstd", "-q", "-t", archive)
    archive_sha = sha256(archive)

    external = dict(manifest, archive_name=ARCHIVE_NAME, archive_sha256=archive_sha,
                    archive_size=archive.stat().st_size)
    (WORKING / "linker-python-runtime-v1.manifest.json").write_text(
        json.dumps(external, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    (WORKING / f"{ARCHIVE_NAME}.sha256").write_text(f"{archive_sha}  {ARCHIVE_NAME}\n", encoding="ascii")
    print(f"runtime ready: {archive} bytes={archive.stat().st_size} sha256={archive_sha}", flush=True)


if __name__ == "__main__":
    main()
