import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.fps_diagnostic import (
    capture,
    cpu_busy_percent,
    normalize_stats,
    probe_recording,
    probe_source_stream,
    validate_url,
    validate_rtsp_url,
)


class FpsDiagnosticTests(unittest.TestCase):
    def test_stats_keep_only_fps_fields_and_detector_latency(self):
        result = normalize_stats({
            "cameras": {
                "front_door": {
                    "camera_fps": 5,
                    "process_fps": 4.5,
                    "detection_fps": 1,
                    "skipped_fps": 0.5,
                    "ffmpeg_cpu": "20.1%",
                    "url": "rtsp://secret.invalid/private",
                }
            },
            "detectors": {"cpu": {"inference_speed": 212.3, "token": "private"}},
            "cpu_usages": {"frigate.full_system": {"cpu": "67.0%"}},
            "private": "discard-me",
        })
        encoded = json.dumps(result)
        self.assertEqual(result["cameras"]["front_door"]["processed_fps"], 4.5)
        self.assertEqual(result["cameras"]["front_door"]["ffmpeg_cpu_percent"], 20.1)
        self.assertEqual(result["detectors_inference_ms"]["cpu"], 212.3)
        self.assertEqual(result["frigate_cpu_percent"], 67.0)
        self.assertNotIn("secret.invalid", encoded)
        self.assertNotIn("discard-me", encoded)
        self.assertNotIn("private", encoded)

    def test_cpu_busy_percent_uses_total_and_idle_ticks(self):
        self.assertEqual(cpu_busy_percent((100, 40), (200, 80)), 60.0)
        self.assertIsNone(cpu_busy_percent(None, (200, 80)))
        self.assertIsNone(cpu_busy_percent((100, 40), (100, 40)))

    def test_url_rejects_embedded_credentials_and_query_data(self):
        for value in (
            "http://user:password@localhost:5000/api/stats",
            "http://localhost/api/stats?token=private",
            "ftp://localhost/stats",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_url(value)
        with self.assertRaises(ValueError):
            validate_rtsp_url("rtsp://user:password@camera.invalid/main")

    def test_source_probe_counts_pts_and_does_not_return_source_url(self):
        payload = {"packets": [{"pts_time": "0.000000"}, {"pts_time": "0.066667"}, {"pts_time": "0.133333"}]}
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        source_url = "rtsp://127.0.0.1:8554/front_door"
        with patch("tools.fps_diagnostic.subprocess.run", return_value=completed) as run:
            result = probe_source_stream("front_door", source_url, "/usr/bin/ffprobe")
        self.assertEqual(result["status"], "measured")
        self.assertEqual(result["fps"], 15.0)
        self.assertNotIn(source_url, json.dumps(result))
        self.assertEqual(run.call_args.args[0][-1], source_url)

    def test_capture_is_bounded_and_marks_client_and_source_gaps(self):
        class FakeClock:
            value = 0.0

            def monotonic(self):
                return self.value

            def sleep(self, seconds):
                self.value += seconds

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "proc").mkdir()
            (root / "sys/class/thermal/thermal_zone0").mkdir(parents=True)
            (root / "proc/stat").write_text("cpu 100 0 0 100 0 0 0 0 0 0\n")
            (root / "proc/loadavg").write_text("0.50 0 0 1/1 1\n")
            (root / "sys/class/thermal/thermal_zone0/type").write_text("cpu-thermal\n")
            (root / "sys/class/thermal/thermal_zone0/temp").write_text("64000\n")
            clock = FakeClock()
            calls = []

            def reader(url):
                calls.append(url)
                return {"cameras": {"porch": {"camera_fps": 5, "process_fps": 4.8, "skipped_fps": 0.2}}}

            report = capture(
                "http://localhost:5000/api/stats",
                duration_s=2,
                interval_s=1,
                stats_reader=reader,
                monotonic=clock.monotonic,
                sleeper=clock.sleep,
                root=root,
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(report["capture"]["duration_s"], 2.0)
        self.assertEqual(report["capture"]["frigate_samples_ok"], 3)
        self.assertEqual(report["frigate"]["camera_metrics"]["porch"]["processed_fps"]["mean"], 4.8)
        self.assertEqual(report["host"]["temperature_c"]["mean"], 64.0)
        self.assertIn("browser_view_fps", report["not_measured"])
        self.assertIn("camera_sensor_fps", report["not_measured"])

    def test_recording_probe_counts_decoded_frames_without_returning_path(self):
        payload = {
            "streams": [{"codec_name": "h264", "width": 1920, "height": 1080, "nb_read_frames": "150"}],
            "format": {"duration": "10.0"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "segment.mp4"
            video.write_bytes(b"test")
            completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
            with patch("tools.fps_diagnostic.subprocess.run", return_value=completed) as run:
                result = probe_recording(video, "/usr/bin/ffprobe")
        self.assertEqual(result["fps"], 15.0)
        self.assertEqual(result["frames"], 150)
        self.assertEqual(result["camera"], Path(temporary).name)
        self.assertEqual(result["file"], "segment.mp4")
        self.assertNotIn(temporary, json.dumps(result))
        self.assertIn("-count_frames", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
