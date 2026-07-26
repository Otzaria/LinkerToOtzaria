"""One ephemeral Kaggle shard: verified inputs -> raw NER handoff only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path


RUNTIME_SHA256 = "bacad1b486c2bb392ee786bcc35b27dcc2beb17ea90b05f47352a06e44c8ff43"
TOTAL_SESSION_BUDGET_SECONDS = 9_700
EXTRACTED_MODEL_SHA256 = {
    "he_ref_ner-any-py3-none-any.whl": "37ff5191562e86559e0600b2bb83b7ed634454ad283d0da5a4527c0877855e2d",
    "he_subref_ner-any-py3-none-any.whl": "4480a8acec08bae08b8a8be4cbd13d04ed986390b6625630b5da371d4c24c2f0",
    "EXPECTED_SHA256.txt": "391f558eccb421f06b6d70427f65210ccd23f5d0915117eb6dd6a12c6f2a4979",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def exactly_one(root: Path, name: str) -> Path:
    matches = [path for path in root.rglob(name) if path.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {name!r} under {root}; found {len(matches)}")
    return matches[0]


def run(*argv: str | Path, cwd: Path | None = None, env: dict | None = None) -> None:
    print("+", " ".join(map(str, argv)), flush=True)
    subprocess.run(
        [str(value) for value in argv],
        cwd=cwd,
        env=env,
        check=True,
    )


def zstandard_module():
    try:
        import zstandard
    except ImportError:
        run(
            sys.executable, "-m", "pip", "install",
            "--quiet", "zstandard==0.23.0",
        )
        import zstandard
    return zstandard


def extract_zstd_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    zstandard = zstandard_module()

    def safe_member(member: tarfile.TarInfo, _destination: str) -> tarfile.TarInfo:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe archive member: {member.name!r}")
        if member.issym() or member.islnk():
            target = Path(member.linkname)
            safe_relative = (
                bool(target.parts)
                and not target.is_absolute()
                and ".." not in target.parts
            )
            if not safe_relative and member.linkname != "/usr/bin/python3.12":
                raise RuntimeError(
                    f"unsafe archive link: {member.name!r} -> {member.linkname!r}"
                )
        return member

    with archive.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                tar.extractall(destination, filter=safe_member)


def decompress_zstd(archive: Path, destination: Path) -> None:
    zstandard = zstandard_module()
    with archive.open("rb") as compressed, destination.open("wb") as output:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
            shutil.copyfileobj(stream, output, length=1024 * 1024)


def wait_for_ner() -> None:
    payload = json.dumps({"text": "בדיקה", "lang": "he"}).encode()
    for _ in range(120):
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:5051/recognize-entities",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(5)
    raise RuntimeError("NER service did not become healthy")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--input-root", type=Path, default=Path("/kaggle/input"))
    parser.add_argument("--work-root", type=Path, default=Path("/kaggle/temp/parallel-ner"))
    parser.add_argument("--output-root", type=Path, default=Path("/kaggle/working"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--batch-lines", type=int, default=75)
    parser.add_argument("--session-budget-seconds", type=int, default=TOTAL_SESSION_BUDGET_SECONDS)
    args = parser.parse_args()
    started = time.monotonic()
    if not 0 <= args.shard_index <= 99:
        parser.error("--shard-index must be between 0 and 99")

    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("PYTHONHOME", None)
    for customizer in Path("/usr/lib").glob("python3.*/sitecustomize.py"):
        customizer.unlink(missing_ok=True)

    runtime_archive = exactly_one(args.input_root, "linker-python-runtime-v1.tar.zst")
    if sha256(runtime_archive) != RUNTIME_SHA256:
        raise RuntimeError("prebuilt Python runtime digest mismatch")
    data_manifest_path = exactly_one(args.input_root, "parallel_ner_input_manifest.json")
    data_manifest = json.loads(data_manifest_path.read_text(encoding="utf-8"))
    if data_manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported parallel NER input manifest")
    for name, expected in data_manifest["files"].items():
        if name == "linker_models.tar.gz" and not list(args.input_root.rglob(name)):
            for extracted_name, extracted_digest in EXTRACTED_MODEL_SHA256.items():
                extracted = exactly_one(args.input_root, extracted_name)
                if sha256(extracted) != extracted_digest:
                    raise RuntimeError(f"extracted model digest mismatch: {extracted_name}")
            continue
        path = exactly_one(args.input_root, name)
        if sha256(path) != expected:
            raise RuntimeError(f"input digest mismatch: {name}")

    args.work_root.mkdir(parents=True, exist_ok=True)
    runtime = args.work_root / "runtime"
    sources = args.work_root / "sources"
    extract_zstd_tar(runtime_archive, runtime)
    extract_zstd_tar(exactly_one(args.input_root, "sefaria-source.tar.zst"), sources)
    extract_zstd_tar(exactly_one(args.input_root, "gpu-source.tar.zst"), sources)
    extract_zstd_tar(exactly_one(args.input_root, "linker-source.tar.zst"), sources)
    sefaria = sources / "Sefaria-Project"
    gpu = sources / "gpu-server"
    linker = sources / "LinkerToOtzaria"
    shutil.move(runtime / "sefaria-venv", sefaria / ".venv")
    shutil.move(runtime / "gpu-venv", gpu / ".venv")

    models = args.work_root / "models"
    models.mkdir()
    model_archives = list(args.input_root.rglob("linker_models.tar.gz"))
    if model_archives:
        if len(model_archives) != 1:
            raise RuntimeError("expected at most one linker_models.tar.gz")
        run("tar", "-xzf", model_archives[0], "-C", models)
    else:
        for name in EXTRACTED_MODEL_SHA256:
            shutil.copy2(exactly_one(args.input_root, name), models / name)
    for name in ("he_ref_ner", "he_subref_ner"):
        wheel = next(models.glob(f"{name}-*.whl"))
        package = models / f"{name}_pkg"
        package.mkdir()
        run("unzip", "-q", wheel, "-d", package)
    ref_model = next((models / "he_ref_ner_pkg" / "he_ref_ner").glob("he_ref_ner-*"))
    subref_model = next((models / "he_subref_ner_pkg" / "he_subref_ner").glob("he_subref_ner-*"))
    (gpu / "app" / "local_config.py").write_text(
        "MODEL_PATHS = [\n"
        f"    {{'arch': 'spacy', 'lang': 'he', 'path': {str(ref_model)!r}, 'type': 'named_entity'}},\n"
        f"    {{'arch': 'spacy', 'lang': 'he', 'path': {str(subref_model)!r}, 'type': 'ref_part'}},\n"
        "]\n",
        encoding="utf-8",
    )

    ner_log = (args.output_root / f"ner-service-{args.shard_index:02d}.log").open("wb")
    ner = subprocess.Popen(
        [
            str(gpu / ".venv" / "bin" / "python"),
            "-m", "gunicorn.app.wsgiapp",
            "-w", str(args.workers),
            "--timeout", "600",
            "-b", "127.0.0.1:5051",
            "app:create_app()",
        ],
        cwd=gpu / "app",
        env={**os.environ, "APP_CONFIG": "local_config.py"},
        stdout=ner_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        wait_for_ner()
        snapshot_zst = exactly_one(args.input_root, "lines_snapshot_context_v2.db.zst")
        snapshot = args.work_root / "lines_snapshot.db"
        decompress_zstd(snapshot_zst, snapshot)
        plan = exactly_one(args.input_root, f"shard-{args.shard_index:02d}.json")
        request_id = data_manifest["relink_request_id"]
        engine_fingerprint = data_manifest["engine_fingerprint"]
        handoff = args.output_root / f"raw-ner-shard-{args.shard_index:02d}"
        remaining = args.session_budget_seconds - int(time.monotonic() - started)
        if remaining < 300:
            raise TimeoutError("setup exhausted the bounded session budget")
        environment = {
            **os.environ,
            "PYTHONPATH": str(sefaria),
            "LINKER_BATCH_LINES": str(args.batch_lines),
            "LINKER_NER_MAX_WAIT_SEC": "300",
        }
        run(
            sefaria / ".venv" / "bin" / "python",
            linker / "src" / "precompute_ner.py",
            "--snapshot", snapshot,
            "--plan", plan,
            "--output", handoff,
            "--relink-request-id", request_id,
            "--engine-fingerprint", engine_fingerprint,
            "--workers", str(args.workers),
            "--deadline-seconds", str(remaining),
            env=environment,
        )
        (args.output_root / f"shard-{args.shard_index:02d}-complete.json").write_text(
            json.dumps({
                "schema_version": 1,
                "shard_index": args.shard_index,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "request_id": request_id,
                "engine_fingerprint": engine_fingerprint,
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    finally:
        try:
            os.killpg(ner.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            ner.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(ner.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            ner.wait()
        ner_log.close()


if __name__ == "__main__":
    main()
