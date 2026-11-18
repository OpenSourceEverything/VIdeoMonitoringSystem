import tempfile
import unittest
from pathlib import Path

from video_monitoring_system.device.runtime_metrics import power_flags, read_runtime_metrics


class RuntimeMetricsTests(unittest.TestCase):
    def test_historical_voltage_is_distinct_from_current_voltage(self):
        historical = power_flags("throttled=0xf0000")
        self.assertTrue(historical["low_voltage_seen"])
        self.assertFalse(historical["low_voltage_now"])
        self.assertTrue(power_flags("throttled=0x1")["low_voltage_now"])
        self.assertTrue(all(v is None for v in power_flags("unknown").values()))

    def test_linux_measurements_and_unknown_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {
                "proc/sys/kernel/random/boot_id": "a-test-boot-id",
                "proc/uptime": "123.5 52.7",
                "proc/loadavg": "2.0 1.0 0.5 1/9 123",
                "sys/class/thermal/thermal_zone0/type": "cpu-thermal",
                "sys/class/thermal/thermal_zone0/temp": "76500",
            }
            for name, value in values.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            result = read_runtime_metrics(root=root, cpu_count=4)
            self.assertEqual(result["uptime_s"], 123.5)
            self.assertEqual(result["temperature_c"], 76.5)
            self.assertEqual(result["load_per_core"], .5)
            self.assertNotIn("a-test-boot-id", str(result))
            (root / "proc/uptime").write_text("nan")
            (root / "sys/class/thermal/thermal_zone0/type").write_text("unrelated-sensor")
            result = read_runtime_metrics(root=root, cpu_count=4)
            self.assertIsNone(result["uptime_s"])
            self.assertIsNone(result["temperature_c"])
