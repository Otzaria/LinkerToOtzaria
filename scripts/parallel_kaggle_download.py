#!/usr/bin/env python3
"""Download Kaggle kernel output concurrently and atomically.

The stock Kaggle CLI downloads every output file serially. NER handoffs contain
thousands of small batch files, so parallel HTTP transfers are substantially
faster while the final manifest still provides cryptographic validation.
"""

from __future__ import annotations

import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from threading import local
from time import monotonic, sleep

import requests
from kaggle.api.kaggle_api_extended import (
    ApiListKernelSessionOutputRequest,
    KaggleApi,
)


_thread_state = local()


def session() -> requests.Session:
    value = getattr(_thread_state, "session", None)
    if value is None:
        value = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
        value.mount("https://", adapter)
        _thread_state.session = value
    return value


def output_files(api: KaggleApi, reference: str) -> list[tuple[str, str]]:
    owner, slug, _version = api.parse_kernel_string(reference)
    files: list[tuple[str, str]] = []
    token = None
    with api.build_kaggle_client() as client:
        while True:
            request = ApiListKernelSessionOutputRequest()
            request.user_name = owner
            request.kernel_slug = slug
            request.page_size = 200
            if token:
                request.page_token = token
            response = client.kernels.kernels_api_client.list_kernel_session_output(
                request
            )
            files.extend((item.file_name, item.url) for item in response.files or [])
            token = response.next_page_token
            if not token:
                return files


def target_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe Kaggle output path: {name!r}")
    return root.joinpath(*relative.parts)


def download_one(root: Path, name: str, url: str) -> tuple[str, int]:
    target = target_path(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.part")
    for attempt in range(4):
        try:
            with session().get(url, stream=True, timeout=(20, 180)) as response:
                response.raise_for_status()
                size = 0
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            size += len(chunk)
                expected = response.headers.get("Content-Length")
                if expected is not None and size != int(expected):
                    raise IOError(
                        f"short response for {name}: {size} != {expected}"
                    )
            temporary.replace(target)
            return name, size
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                raise
            sleep(2**attempt)
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()

    api = KaggleApi()
    api.authenticate()
    files = output_files(api, args.reference)
    args.output.mkdir(parents=True, exist_ok=True)
    print(f"{args.reference}: {len(files)} files", flush=True)
    started = monotonic()
    bytes_downloaded = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(download_one, args.output, name, url)
            for name, url in files
        ]
        for count, future in enumerate(as_completed(futures), 1):
            _name, size = future.result()
            bytes_downloaded += size
            if count % 500 == 0 or count == len(files):
                print(
                    f"{count}/{len(files)} files, "
                    f"{bytes_downloaded / 1024 / 1024:.1f} MiB, "
                    f"{monotonic() - started:.1f}s",
                    flush=True,
                )


if __name__ == "__main__":
    main()
