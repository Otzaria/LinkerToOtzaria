#!/usr/bin/env python3
"""Benchmark the shared-model NER service with resolver-shaped concurrency.

This is a canary/profiling probe, not a linker stage.  It compares the same request
set at 1/2/4/8-way concurrency, requires exact decoded output identity, and reports
throughput plus server-side batch occupancy.  That distinguishes an under-fed model
from saturated inference without loading a second model into VRAM.
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
    metric_keys = (
        "batches", "requests", "texts", "model_seconds", "queue_seconds",
        "failures", "timeouts",
    )

    def run(concurrency: int) -> tuple[list[list[dict]], dict]:
        before = get_metrics()
        started = time.monotonic()
        output: list[list[dict] | None] = [None] * REQUESTS
        for group_start in range(0, REQUESTS, concurrency):
            numbers = list(range(group_start, min(group_start + concurrency, REQUESTS)))
            gate = threading.Barrier(len(numbers)) if len(numbers) > 1 else None
            with ThreadPoolExecutor(max_workers=len(numbers)) as pool:
                futures = {number: pool.submit(post, number, gate) for number in numbers}
                for number, future in futures.items():
                    output[number] = future.result()
        elapsed = time.monotonic() - started
        after = get_metrics()
        delta = {key: after[key] - before[key] for key in metric_keys}
        if delta["requests"] != REQUESTS or delta["texts"] != REQUESTS * LINES_PER_REQUEST:
            raise RuntimeError(f"micro-batcher accounting differs at c={concurrency}: {delta!r}")
        if delta["failures"] or delta["timeouts"]:
            raise RuntimeError(f"micro-batcher failed at c={concurrency}: {delta!r}")
        return [value for value in output if value is not None], {
            "concurrency": concurrency,
            "elapsed_seconds": round(elapsed, 6),
            "texts_per_second": round(REQUESTS * LINES_PER_REQUEST / elapsed, 3),
            "delta": delta,
        }

    serial, serial_case = run(1)
    cases = [serial_case]
    for concurrency in (2, 4, 8):
        output, case = run(concurrency)
        if output != serial:
            raise RuntimeError(f"c={concurrency} micro-batching changed NER output")
        cases.append(case)
    if not 1 <= cases[-1]["delta"]["batches"] < REQUESTS:
        raise RuntimeError(f"eight requests were not consolidated: {cases[-1]!r}")
    print(json.dumps({"cases": cases, "after": get_metrics()}, sort_keys=True))


if __name__ == "__main__":
    main()
