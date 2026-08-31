#!/usr/bin/env python3
"""Low-overhead host/GPU telemetry for long self-hosted linker runs."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path


STOP = False


def _stop(_signum, _frame):
    global STOP
    STOP = True


def _proc_cpu():
    fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _memory():
    result = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            result[key.lower() + "_kib"] = int(value.strip().split()[0])
    return result


def _disk_bytes():
    devices = {path.name for path in Path("/sys/block").iterdir()}
    read_sectors = 0
    written_sectors = 0
    for line in Path("/proc/diskstats").read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) < 14 or fields[2] not in devices:
            continue
        read_sectors += int(fields[5])
        written_sectors += int(fields[9])
    return read_sectors * 512, written_sectors * 512


def _process_io(pid):
    try:
        values = {}
        for line in Path(f"/proc/{pid}/io").read_text(encoding="ascii").splitlines():
            key, raw = line.split(":", 1)
            if key in {"read_bytes", "write_bytes"}:
                values[key] = int(raw)
        return values
    except (OSError, ValueError):
        return {}


def _linker_processes():
    output = subprocess.check_output(
        ["ps", "-eo", "pid=,ppid=,pcpu=,rss=,comm=,args="],
        text=True,
        timeout=5,
    )
    names = ("link_books.py", "precompute_ner.py", "gunicorn", "mongod")
    rows = []
    for line in output.splitlines():
        if not any(name in line for name in names):
            continue
        parts = line.strip().split(None, 5)
        if len(parts) != 6:
            continue
        rows.append({
            "pid": int(parts[0]),
            "ppid": int(parts[1]),
            "cpu_percent": float(parts[2]),
            "rss_kib": int(parts[3]),
            "command": parts[4],
            "args": parts[5][:300],
            **_process_io(int(parts[0])),
        })
    return rows


def _gpu():
    try:
        result = subprocess.run(
            [
                "rocm-smi", "--showuse", "--showmemuse", "--showpower",
                "--showclocks", "--json",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return {"error": result.stderr[-500:]}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout[-2000:]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=float, default=10.0)
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least one second")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    previous = _proc_cpu()
    previous_disk = _disk_bytes()
    previous_sample = time.monotonic()
    samples = 0
    cpu_sum = 0.0
    with output.open("a", encoding="utf-8") as stream:
        while not STOP:
            started = time.monotonic()
            current = _proc_cpu()
            current_disk = _disk_bytes()
            now = time.monotonic()
            elapsed = max(now - previous_sample, 0.001)
            total_delta = current[0] - previous[0]
            idle_delta = current[1] - previous[1]
            cpu_percent = (
                100.0 * (total_delta - idle_delta) / total_delta
                if total_delta > 0 else 0.0
            )
            previous = current
            disk_read_bps = (current_disk[0] - previous_disk[0]) / elapsed
            disk_write_bps = (current_disk[1] - previous_disk[1]) / elapsed
            previous_disk = current_disk
            previous_sample = now
            try:
                processes = _linker_processes()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                processes = [{"error": f"{type(error).__name__}: {error}"}]
            record = {
                "type": "sample",
                "unix": time.time(),
                "loadavg": os.getloadavg(),
                "host_cpu_percent": cpu_percent,
                "disk_read_bytes_per_second": disk_read_bps,
                "disk_write_bytes_per_second": disk_write_bps,
                **_memory(),
                "processes": processes,
                "gpu": _gpu(),
            }
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            samples += 1
            cpu_sum += cpu_percent
            STOP_WAIT = args.interval - (time.monotonic() - started)
            if STOP_WAIT > 0:
                time.sleep(STOP_WAIT)
        stream.write(json.dumps({
            "type": "summary",
            "unix": time.time(),
            "samples": samples,
            "average_host_cpu_percent": cpu_sum / samples if samples else 0.0,
        }, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
