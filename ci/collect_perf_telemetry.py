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


# ── GPU telemetry ────────────────────────────────────────────────────────────
# Every sample of every audited cycle carried {"gpu":{"raw":""}} — 20/20 in run
# 34021656701 and 606/606 in 34016397157 (re-parsed from the stored artifacts; every
# other retained linker-perf run is 100% too) — so "was the card busy during NER?"
# could not be answered from telemetry at all.  Cause: `rocm-smi --json` exits 0 with an EMPTY
# stdout when the amdgpu kernel driver is absent (it prints `ERROR:root:Driver not
# initialized (amdgpu not found in modules)` on stderr), and the old probe fed that
# empty string to json.loads and stored the decode fallback, once per sample, forever.
# Reproduced verbatim on a ROCm-7.2 WSL2 userspace: rocm-smi exits 0 with an EMPTY
# stdout (the reason only on stderr), amd-smi exits 255 printing just its version
# banner, and /sys/class/drm holds no card* at all, because the GPU reaches a WSL2 VM
# through /dev/dxg instead of the amdgpu DRM driver.
#
# So: probe the known sources ONCE, in order, keep the first that returns a real
# number, and record that number in every sample.  If none answers, say so exactly
# ONCE — with the tool's own first stderr line — and omit the key from every later
# sample.  A source that answered at start-up and later stops is reported the same
# way: one line, then silence.  Never a hundred empty strings again.
GPU_PROBE_TIMEOUT_SECONDS = 8
SYSFS_DRM_ROOT = Path("/sys/class/drm")


def _number(value):
    """Integers stay integers; everything else keeps one decimal."""
    number = float(value)
    return int(number) if number.is_integer() else round(number, 1)


def _as_number(value):
    """Numeric value of a JSON/sysfs field, or None.  Tools quote their numbers and
    sometimes append the unit ("37", "37 %", "1234 MB"), so strip a trailing unit."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().rstrip("%").strip()
        text = text.split()[0] if text.split() else ""
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _pick(fields, keys):
    for key in keys:
        if key in fields:
            number = _as_number(fields[key])
            if number is not None:
                return number
    return None


def _walk_number(payload, names):
    """First numeric value stored under any of ``names``, at any depth.  amd-smi wraps
    each metric in {"value": …, "unit": …} and nests it a few levels deep, so this is
    fed ONE device's entry at a time — over a whole multi-GPU payload it would answer
    for whichever device came first."""
    queue = [payload]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                if key in names:
                    number = _as_number(
                        value.get("value") if isinstance(value, dict) else value
                    )
                    if number is not None:
                        return number
                if isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(node, list):
            queue.extend(node)
    return None


def _first_line(text):
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def _run_tool(command, timeout=GPU_PROBE_TIMEOUT_SECONDS):
    """Run one probe.  A missing binary is an ordinary outcome here, not an error:
    it comes back as a returncode and a reason like any other failed probe."""
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", f"{command[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"{command[0]} timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as error:
        return 127, "", f"{command[0]}: {type(error).__name__}: {error}"
    return result.returncode, result.stdout, result.stderr


def parse_rocm_smi(stdout):
    """rocm-smi --json → {"card0": {"GPU use (%)": "37", "VRAM Total Memory (B)": …}}."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    best = None
    for device, fields in sorted(payload.items()):
        if not isinstance(fields, dict):
            continue
        util = _pick(fields, ("GPU use (%)", "GPU Utilization (%)", "GPU use (%) "))
        if util is None:
            continue
        reading = {"source": "rocm-smi", "device": device, "util_pct": _number(util)}
        used = _pick(fields, ("VRAM Total Used Memory (B)",))
        total = _pick(fields, ("VRAM Total Memory (B)",))
        if used is not None:
            reading["mem_used_mb"] = _number(used / (1024 * 1024))
        if total is not None:
            reading["mem_total_mb"] = _number(total / (1024 * 1024))
        if best is None or reading["util_pct"] > best["util_pct"]:
            best = reading
    return best


def _amd_smi_device(entry, index, indexed):
    """The device name amd-smi states, else its position — never a guess."""
    if isinstance(entry, dict):
        gpu = entry.get("gpu")
        if type(gpu) is int:
            return f"gpu{gpu}"
        if isinstance(gpu, str) and gpu.strip():
            return gpu.strip()[:64]
    return f"gpu{index}" if indexed else None


def _amd_smi_entry(entry, index, indexed):
    util = _walk_number(entry, {"gfx_activity", "gfx_usage", "gpu_activity"})
    if util is None:
        return None
    reading = {"source": "amd-smi", "util_pct": _number(util)}
    device = _amd_smi_device(entry, index, indexed)
    if device is not None:
        reading["device"] = device
    used = _walk_number(entry, {"used_vram", "vram_used"})
    total = _walk_number(entry, {"total_vram", "vram_total"})
    if used is not None:
        reading["mem_used_mb"] = _number(used)
    if total is not None:
        reading["mem_total_mb"] = _number(total)
    return reading


def parse_amd_smi(stdout):
    """amd-smi metric --json → a per-GPU list; VRAM figures are already in MB.

    Per device, busiest wins — the same rule parse_rocm_smi, read_sysfs_gpu and
    parse_nvidia_smi apply.  Reading the whole payload at once answered for whichever
    device the walk reached first, so a run saturating gpu1 beside an idle gpu0 was
    recorded as 0% for its whole NER stage.  A payload that is not a list is treated
    as one device, which is what the walk already did for it.
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    indexed = isinstance(payload, list)
    best = None
    for index, entry in enumerate(payload if indexed else [payload]):
        if not isinstance(entry, (dict, list)):
            continue
        reading = _amd_smi_entry(entry, index, indexed)
        if reading is None:
            continue
        if best is None or reading["util_pct"] > best["util_pct"]:
            best = reading
    return best


def _sysfs_number(path):
    try:
        return _as_number(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def read_sysfs_gpu(root=SYSFS_DRM_ROOT):
    """The driver's own counter: /sys/class/drm/card*/device/gpu_busy_percent."""
    best = None
    try:
        busy_files = sorted(Path(root).glob("card*/device/gpu_busy_percent"))
    except OSError:
        return None
    for busy in busy_files:
        util = _sysfs_number(busy)
        if util is None:
            continue
        reading = {
            "source": "sysfs",
            "device": busy.parent.parent.name,
            "util_pct": _number(util),
        }
        used = _sysfs_number(busy.parent / "mem_info_vram_used")
        total = _sysfs_number(busy.parent / "mem_info_vram_total")
        if used is not None:
            reading["mem_used_mb"] = _number(used / (1024 * 1024))
        if total is not None:
            reading["mem_total_mb"] = _number(total / (1024 * 1024))
        if best is None or reading["util_pct"] > best["util_pct"]:
            best = reading
    return best


def parse_nvidia_smi(stdout):
    """--query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits."""
    best = None
    for index, line in enumerate((stdout or "").splitlines()):
        parts = [part.strip() for part in line.split(",")]
        if not parts or not parts[0]:
            continue
        util = _as_number(parts[0])
        if util is None:
            continue
        reading = {
            "source": "nvidia-smi",
            "device": f"gpu{index}",
            "util_pct": _number(util),
        }
        used = _as_number(parts[1]) if len(parts) > 1 else None
        total = _as_number(parts[2]) if len(parts) > 2 else None
        if used is not None:
            reading["mem_used_mb"] = _number(used)
        if total is not None:
            reading["mem_total_mb"] = _number(total)
        if best is None or reading["util_pct"] > best["util_pct"]:
            best = reading
    return best


def gpu_sources(run=_run_tool, sysfs_root=SYSFS_DRM_ROOT):
    """The probe order: the AMD tools this host is built around, then the driver's own
    sysfs counter, then nvidia-smi for a GPU runner that is not AMD at all."""
    def command_source(name, argv, parse):
        def read():
            code, out, err = run(argv)
            reading = parse(out)
            if reading is not None:
                return reading, None
            reason = _first_line(err)
            if not reason:
                reason = (
                    f"exited {code} with no output" if code
                    else "exited 0 but reported no GPU reading"
                )
            return None, f"{name}: {reason}"
        return name, read

    def sysfs_read():
        reading = read_sysfs_gpu(sysfs_root)
        if reading is not None:
            return reading, None
        return None, f"sysfs: no {sysfs_root}/card*/device/gpu_busy_percent"

    return [
        command_source(
            "rocm-smi",
            ["rocm-smi", "--showuse", "--showmeminfo", "vram", "--json"],
            parse_rocm_smi,
        ),
        command_source("amd-smi", ["amd-smi", "metric", "--json"], parse_amd_smi),
        ("sysfs", sysfs_read),
        command_source(
            "nvidia-smi",
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            parse_nvidia_smi,
        ),
    ]


class GpuTelemetry:
    """One resolved GPU source, or one honest statement that there is none."""

    def __init__(self, sources):
        self._sources = list(sources)
        self._name = None
        self._read = None
        self._pending = None

    @property
    def source(self):
        return self._name

    def start(self):
        """Probe every source in order.  Returns the single line for the step log."""
        reasons = []
        for name, read in self._sources:
            reading, reason = read()
            if reading is not None:
                self._name, self._read = name, read
                device = reading.get("device")
                return (
                    f"gpu telemetry: source={name}"
                    + (f" device={device}" if device else "")
                    + f" util={reading['util_pct']}%"
                )
            reasons.append((name, reason))
        reason = next(
            (text for _, text in reasons if text), "no source reported a GPU"
        )
        self._pending = {
            "available": False,
            "reason": reason,
            "probed": [name for name, _ in reasons],
        }
        return f"gpu telemetry: unavailable ({reason})"

    def sample(self):
        """The sample's ``gpu`` value, or None to omit the key entirely."""
        if self._pending is not None:
            record, self._pending = self._pending, None
            return record
        if self._read is None:
            return None
        reading, reason = self._read()
        if reading is not None:
            return reading
        name, self._name, self._read = self._name, None, None
        return {
            "available": False,
            "source": name,
            "reason": reason or f"{name}: stopped reporting",
        }


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
    gpu = GpuTelemetry(gpu_sources())
    # One line in the step log, so the GPU question is answerable from the log alone —
    # either which source is being recorded, or why no reading exists this run.
    print(gpu.start(), flush=True)
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
            }
            gpu_record = gpu.sample()
            if gpu_record is not None:
                record["gpu"] = gpu_record
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
            "gpu_source": gpu.source,
        }, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
