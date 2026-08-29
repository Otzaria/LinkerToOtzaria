#!/usr/bin/env python3
"""Exercise the one-model NER service with concurrent resolver-shaped requests.

This is a canary/profiling probe, not a linker stage.  It verifies that eight HTTP
waiters are consolidated by the ordered micro-batcher while every result retains its
request-local order and shape.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading
import time
from urllib.request import Request, urlopen


BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:5051"
REQUESTS = 8
LINES_PER_REQUEST = 100


def get_metrics() -> dict:
    with urlopen(f"{BASE}/otzaria-microbatch-metrics", timeout=20) as response:
        return json.load(response)


def payload_for(request_number: int) -> bytes:
    texts = [
        f"עיין מסכת ברכות דף ב עמוד א בדיקת עומס {request_number}-{line_number}"
        for line_number in range(LINES_PER_REQUEST)
    ]
    return json.dumps({"lang": "he", "texts": texts}, ensure_ascii=False).encode("utf-8")


def post(request_number: int, gate: threading.Barrier | None = None) -> list[dict]:
    texts = [
        f"עיין מסכת ברכות דף ב עמוד א בדיקת עומס {request_number}-{line_number}"
        for line_number in range(LINES_PER_REQUEST)
    ]
    body = payload_for(request_number)
    request = Request(
        f"{BASE}/bulk-recognize-entities",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if gate is not None:
        gate.wait(timeout=20)
    with urlopen(request, timeout=600) as response:
        value = json.load(response)
    results = value.get("results") if isinstance(value, dict) else None
    if not isinstance(results, list) or len(results) != len(texts):
        raise RuntimeError(f"invalid NER response for request {request_number}: {value!r}")
    return results


def main() -> None:
    # First establish exact serial results.  The concurrent phase below must return
    # byte-equivalent decoded JSON for every request before it may claim batching.
    serial = [post(number) for number in range(REQUESTS)]
    before = get_metrics()
    gate = threading.Barrier(REQUESTS)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=REQUESTS) as pool:
        concurrent = list(pool.map(lambda number: post(number, gate), range(REQUESTS)))
    elapsed = time.monotonic() - started
    after = get_metrics()
    delta = {key: after[key] - before[key] for key in ("batches", "requests", "texts", "model_seconds", "queue_seconds")}
    if concurrent != serial:
        raise RuntimeError("concurrent micro-batching changed NER output")
    if delta["requests"] != REQUESTS or delta["texts"] != REQUESTS * LINES_PER_REQUEST:
        raise RuntimeError(f"micro-batcher accounting differs: {delta!r}")
    if not 1 <= delta["batches"] < REQUESTS:
        raise RuntimeError(f"requests were not consolidated by one-model batcher: {delta!r}")
    print(json.dumps({"elapsed_seconds": round(elapsed, 6), "delta": delta, "after": after}, sort_keys=True))


if __name__ == "__main__":
    main()
