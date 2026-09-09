"""GPU telemetry: a real number, or one honest statement that there is none.

Runs 34021656701 and 34016397157 wrote {"gpu":{"raw":""}} in 20/20 and 606/606 samples,
because `rocm-smi --json` exits 0 with empty stdout when the amdgpu driver is missing
and the old probe stored the JSON-decode fallback every ten seconds.  These tests pin
the parse of each source the runner can plausibly have, and the discipline for the
case the audited cycles actually hit: say it once, then omit the key.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci import collect_perf_telemetry as telemetry  # noqa: E402


ROCM_SMI_JSON = json.dumps({
    "card0": {
        "GPU use (%)": "12",
        "VRAM Total Memory (B)": "17163091968",
        "VRAM Total Used Memory (B)": "1073741824",
    },
    "card1": {
        "GPU use (%)": "83",
        "VRAM Total Memory (B)": "17163091968",
        "VRAM Total Used Memory (B)": "8589934592",
    },
})

AMD_SMI_JSON = json.dumps([{
    "gpu": 0,
    "usage": {"gfx_activity": {"value": 37, "unit": "%"}},
    "mem_usage": {
        "total_vram": {"value": 16368, "unit": "MB"},
        "used_vram": {"value": 2048, "unit": "MB"},
    },
}])

# Two cards, and the busy one is not the first: the shape the old whole-payload walk
# got backwards.  amd-smi metric --json emits one object per GPU, in device order.
AMD_SMI_TWO_GPU_JSON = json.dumps([
    {
        "gpu": 0,
        "usage": {"gfx_activity": {"value": 0, "unit": "%"}},
        "mem_usage": {
            "total_vram": {"value": 16368, "unit": "MB"},
            "used_vram": {"value": 16, "unit": "MB"},
        },
    },
    {
        "gpu": 1,
        "usage": {"gfx_activity": {"value": 97, "unit": "%"}},
        "mem_usage": {
            "total_vram": {"value": 16368, "unit": "MB"},
            "used_vram": {"value": 9001, "unit": "MB"},
        },
    },
])

NVIDIA_SMI_CSV = "3, 512, 16376\n71, 8192, 16376\n"

# What rocm-smi really does on a ROCm-for-WSL userspace: exit 0, print nothing on
# stdout, and put the reason on stderr.  (amd-smi fails differently there — exit 255
# with only its version banner on stdout — which parse_amd_smi also reads as no
# reading; the fixtures below cover both shapes.)
DRIVER_ERROR = "ERROR:root:Driver not initialized (amdgpu not found in modules)"


class GpuParsingTest(unittest.TestCase):
    def test_rocm_smi_json_reports_the_busiest_card_in_megabytes(self):
        reading = telemetry.parse_rocm_smi(ROCM_SMI_JSON)
        self.assertEqual(reading, {
            "source": "rocm-smi", "device": "card1", "util_pct": 83,
            "mem_used_mb": 8192, "mem_total_mb": 16368,
        })

    def test_empty_rocm_smi_output_is_not_a_reading(self):
        for stdout in ("", "   ", "not json", "null"):
            self.assertIsNone(telemetry.parse_rocm_smi(stdout), stdout)

    def test_amd_smi_json_unwraps_value_unit_pairs(self):
        self.assertEqual(telemetry.parse_amd_smi(AMD_SMI_JSON), {
            "source": "amd-smi", "device": "gpu0", "util_pct": 37,
            "mem_used_mb": 2048, "mem_total_mb": 16368,
        })

    def test_amd_smi_reports_the_busiest_gpu_not_the_first(self):
        # The regression: the probe walked the whole payload and answered for whichever
        # device it reached first, so a relink saturating gpu1 next to an idle gpu0 was
        # recorded as 0% util for its entire NER stage.  Its three sibling probes have
        # always taken the max; this one now does too, and names the device it means.
        reading = telemetry.parse_amd_smi(AMD_SMI_TWO_GPU_JSON)
        self.assertEqual(reading, {
            "source": "amd-smi", "device": "gpu1", "util_pct": 97,
            "mem_used_mb": 9001, "mem_total_mb": 16368,
        })
        # The VRAM figures must come from the SAME card as the utilisation, not from
        # whichever entry the walk happened to reach first (gpu0 holds 16 MB).
        self.assertNotEqual(reading["mem_used_mb"], 16)

    def test_amd_smi_reads_a_single_device_payload_that_is_not_a_list(self):
        # An unrecognised (non-list) shape keeps working exactly as before, as one
        # device — but claims no device name, because none was stated.
        payload = json.loads(AMD_SMI_JSON)[0]
        self.assertEqual(telemetry.parse_amd_smi(json.dumps(payload)), {
            "source": "amd-smi", "device": "gpu0", "util_pct": 37,
            "mem_used_mb": 2048, "mem_total_mb": 16368,
        })
        del payload["gpu"]
        self.assertEqual(telemetry.parse_amd_smi(json.dumps(payload)), {
            "source": "amd-smi", "util_pct": 37,
            "mem_used_mb": 2048, "mem_total_mb": 16368,
        })

    def test_amd_smi_without_a_usage_metric_is_not_a_reading(self):
        self.assertIsNone(telemetry.parse_amd_smi(json.dumps([{"gpu": 0}])))
        # One idle-but-silent card must not hide the card that did answer.
        self.assertEqual(
            telemetry.parse_amd_smi(json.dumps(
                [{"gpu": 0}, json.loads(AMD_SMI_TWO_GPU_JSON)[1]]
            ))["util_pct"],
            97,
        )

    def test_nvidia_smi_csv_reports_the_busiest_gpu(self):
        self.assertEqual(telemetry.parse_nvidia_smi(NVIDIA_SMI_CSV), {
            "source": "nvidia-smi", "device": "gpu1", "util_pct": 71,
            "mem_used_mb": 8192, "mem_total_mb": 16376,
        })

    def test_nvidia_smi_no_devices_line_is_not_a_reading(self):
        self.assertIsNone(telemetry.parse_nvidia_smi("\n"))

    def test_sysfs_counter_is_read_per_card(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            device = root / "card0" / "device"
            device.mkdir(parents=True)
            (device / "gpu_busy_percent").write_text("64\n", encoding="ascii")
            (device / "mem_info_vram_used").write_text("2147483648\n", encoding="ascii")
            (device / "mem_info_vram_total").write_text("17163091968\n", encoding="ascii")
            (root / "renderD128").mkdir()
            self.assertEqual(telemetry.read_sysfs_gpu(root), {
                "source": "sysfs", "device": "card0", "util_pct": 64,
                "mem_used_mb": 2048, "mem_total_mb": 16368,
            })

    def test_sysfs_without_a_card_is_not_a_reading(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(telemetry.read_sysfs_gpu(Path(temp)))


class GpuSourceSelectionTest(unittest.TestCase):
    def _sources(self, answers, sysfs_root):
        def run(command, timeout=None):
            return answers.get(command[0], (0, "", DRIVER_ERROR))
        return telemetry.gpu_sources(run=run, sysfs_root=sysfs_root)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.empty = Path(self.temp.name)

    def test_the_first_working_source_wins_and_is_named(self):
        gpu = telemetry.GpuTelemetry(self._sources(
            {"amd-smi": (0, AMD_SMI_JSON, "")}, self.empty))
        self.assertEqual(gpu.start(), "gpu telemetry: source=amd-smi device=gpu0 util=37%")
        self.assertEqual(gpu.source, "amd-smi")
        for _ in range(3):
            self.assertEqual(gpu.sample()["util_pct"], 37)

    def test_rocm_smi_is_preferred_when_it_answers(self):
        gpu = telemetry.GpuTelemetry(self._sources(
            {"rocm-smi": (0, ROCM_SMI_JSON, ""), "amd-smi": (0, AMD_SMI_JSON, "")},
            self.empty))
        self.assertEqual(gpu.start(), "gpu telemetry: source=rocm-smi device=card1 util=83%")

    def test_nvidia_smi_is_the_last_resort(self):
        gpu = telemetry.GpuTelemetry(self._sources(
            {"nvidia-smi": (0, NVIDIA_SMI_CSV, "")}, self.empty))
        self.assertEqual(gpu.source or gpu.start() and gpu.source, "nvidia-smi")

    def test_no_source_is_reported_once_with_the_tools_own_stderr(self):
        gpu = telemetry.GpuTelemetry(self._sources({}, self.empty))
        line = gpu.start()
        self.assertEqual(line, f"gpu telemetry: unavailable (rocm-smi: {DRIVER_ERROR})")
        self.assertIsNone(gpu.source)
        first = gpu.sample()
        self.assertEqual(first, {
            "available": False,
            "reason": f"rocm-smi: {DRIVER_ERROR}",
            "probed": ["rocm-smi", "amd-smi", "sysfs", "nvidia-smi"],
        })
        # …and never again: the audited failure was 101 samples of the same non-answer.
        self.assertEqual([gpu.sample() for _ in range(100)], [None] * 100)

    def test_a_missing_binary_reads_as_a_reason_not_a_crash(self):
        def run(command, timeout=None):
            return telemetry._run_tool(["definitely-not-a-real-binary-x7"], timeout=5)
        gpu = telemetry.GpuTelemetry(telemetry.gpu_sources(run=run, sysfs_root=self.empty))
        self.assertIn("is not installed", gpu.start())

    def test_a_source_that_stops_answering_is_reported_once_then_omitted(self):
        state = {"calls": 0}

        def run(command, timeout=None):
            if command[0] != "rocm-smi":
                return 0, "", DRIVER_ERROR
            state["calls"] += 1
            if state["calls"] <= 2:
                return 0, ROCM_SMI_JSON, ""
            return 0, "", "rocm-smi: GPU went away"

        gpu = telemetry.GpuTelemetry(telemetry.gpu_sources(run=run, sysfs_root=self.empty))
        gpu.start()
        self.assertEqual(gpu.sample()["util_pct"], 83)
        died = gpu.sample()
        self.assertEqual(died["available"], False)
        self.assertEqual(died["source"], "rocm-smi")
        self.assertIn("GPU went away", died["reason"])
        self.assertEqual([gpu.sample() for _ in range(5)], [None] * 5)


@unittest.skipUnless(sys.platform.startswith("linux"), "the sampler reads /proc and /sys")
class SamplerRecordTest(unittest.TestCase):
    """End to end: the key is present with a number, or absent — never empty."""

    def test_samples_never_repeat_a_non_answer(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "host.jsonl"
            empty_path = Path(temp) / "bin"
            empty_path.mkdir()
            child = subprocess.Popen(
                [sys.executable, str(ROOT / "ci" / "collect_perf_telemetry.py"),
                 "--output", str(output), "--interval", "1"],
                env=dict(os.environ, PATH=str(empty_path)),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.time() + 30
                while time.time() < deadline:
                    if output.exists() and len(output.read_text().splitlines()) >= 3:
                        break
                    time.sleep(0.2)
                child.send_signal(signal.SIGTERM)
                stdout, stderr = child.communicate(timeout=30)
            finally:
                if child.poll() is None:
                    child.kill()
            self.assertEqual(child.returncode, 0, stderr)
            self.assertTrue(stdout.startswith("gpu telemetry: "), stdout)
            records = [json.loads(line) for line in output.read_text().splitlines()]
            samples = [r for r in records if r["type"] == "sample"]
            self.assertGreaterEqual(len(samples), 3)
            unavailable = [r for r in samples if r.get("gpu", {}).get("available") is False]
            self.assertLessEqual(len(unavailable), 1)
            if unavailable:
                self.assertIs(unavailable[0], samples[0])
                self.assertTrue(all("gpu" not in r for r in samples[1:]))
            else:  # a host that really has a GPU keeps reporting it
                for sample in samples:
                    self.assertIn("util_pct", sample["gpu"])
            self.assertFalse(any(r.get("gpu") == {"raw": ""} for r in samples))
            summary = [r for r in records if r["type"] == "summary"]
            self.assertEqual(len(summary), 1)
            self.assertIn("gpu_source", summary[0])


if __name__ == "__main__":
    unittest.main()
