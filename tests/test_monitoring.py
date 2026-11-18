import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from video_monitoring_system.monitoring import (
    HealthHistory, MonitorLimits, camera_observation, collect_sample, warnings, read_history,
)


class MonitoringTests(unittest.TestCase):
    def source(self, identity="synthetic-camera"):
        source = Mock(camera_id=identity)
        source.get_json.return_value = {
            "device_id": identity, "camera_ready": True, "schema_version": 4,
            "power": "throttled=0x10000", "stream_secret": "never-persist-this",
            "private_url": "http://private-camera/",
            "runtime": {"boot": "a" * 16, "uptime_s": 10, "temperature_c": 52},
            "network": {"state": "recovering", "rssi_dbm": -75, "current_boot": {"reconnects": 2}},
            "media": {"live": {"codec": "h264"}},
        }
        source.get_bytes.return_value = b"\xff\xd8synthetic-jpeg\xff\xd9"
        return source

    def test_identity_and_secrets_are_checked_before_images(self):
        source = self.source()
        observed = camera_observation(source, "other-camera", 1)
        self.assertEqual(observed["state"], "identity-mismatch")
        source.get_bytes.assert_not_called()
        observed = camera_observation(source, "synthetic-camera", 1)
        self.assertEqual(observed["state"], "online")
        self.assertTrue(observed["power"]["low_voltage_seen"])
        self.assertEqual(observed["network"]["reconnects"], 2)
        self.assertEqual(observed["network"]["rssi_dbm"], -75)
        for private in ("synthetic-camera", "never-persist-this", "private-camera"):
            self.assertNotIn(private, json.dumps(observed))
        source.get_bytes.side_effect = OSError("private-camera never-persist-this")
        self.assertEqual(camera_observation(source, "synthetic-camera", 1)["state"], "unavailable")

    def test_one_failed_camera_does_not_hide_other_cameras_or_recording(self):
        good, bad = self.source("good"), self.source("bad")
        bad.get_json.side_effect = TimeoutError("private detail")
        app = SimpleNamespace(
            recorder=SimpleNamespace(state={"cameras": [{"id": "good"}, {"id": "bad"}, {"id": "revoked", "revoked": True}]}),
            video_engine=SimpleNamespace(camera_bindings={"good": "front"}),
            _recording_source_provider=lambda: (good, bad),
            camera_recording_coverage=lambda **kw: {"state": "indexed", "latest_segment_end": 970},
        )
        with patch("video_monitoring_system.monitoring.subprocess.run", side_effect=OSError):
            result = collect_sample(app, clock=lambda: 1000)
        self.assertEqual([c["state"] for c in result["cameras"]], ["online", "unavailable"])
        self.assertEqual(result["cameras"][0]["recording_age_s"], 30)
        self.assertIn("camera-2-not-engine-managed", warnings(result, MonitorLimits()))

    def test_history_survives_restart_and_bounds_events_and_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            limits = MonitorLimits(samples=2, events=2)
            sample = {"checked_at": "synthetic-time", "recorder": {"boot": "first"}, "cameras": []}
            self.assertTrue(HealthHistory(path, limits).append(copy.deepcopy(sample)))
            self.assertFalse(HealthHistory(path, limits).append(copy.deepcopy(sample)))
            sample["recorder"]["power"] = {"low_voltage_seen": True}
            self.assertTrue(HealthHistory(path, limits).append(copy.deepcopy(sample)))
            sample["recorder"]["boot"] = "second"
            HealthHistory(path, limits).append(sample)
            saved = json.loads(path.read_text())
            self.assertEqual(len(saved["samples"]), 2)
            self.assertEqual(len(saved["events"]), 2)
            self.assertIn("recorder-low-voltage-recorded", saved["events"][0]["warnings"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text('{"schema_version":1,"samples":[{}],"events":[]}')
            self.assertTrue(HealthHistory(path, limits).append(copy.deepcopy(sample)))

    def test_invalid_bounds_and_symlink_are_rejected(self):
        for kwargs in ({"interval_s": 1}, {"samples": 2.5}, {"temperature_c": float("nan")}, {"events": True}):
            with self.assertRaises(ValueError):
                MonitorLimits(**kwargs)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("untouched")
            link = Path(directory) / "link"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                HealthHistory(link).append({})
            self.assertEqual(target.read_text(), "untouched")

    def test_paused_recording_does_not_report_an_outage(self):
        camera = camera_observation(self.source(), "synthetic-camera", 1)
        camera.update(engine_managed=True, recording_enabled=False, recording_state="none", recording_age_s=900)
        result = warnings({"recorder": {}, "cameras": [camera]}, MonitorLimits())
        self.assertNotIn("camera-1-no-recent-retained-index", result)

    def test_support_history_rejects_extra_private_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            sample = {"checked_at": "2026-10-29T14:00:00+00:00", "recorder": {"boot": "a" * 16}, "cameras": []}
            HealthHistory(path).append(sample)
            self.assertEqual(len(read_history(path)["samples"]), 1)
            saved = json.loads(path.read_text())
            saved["samples"][0]["private_url"] = "never-export-this"
            path.write_text(json.dumps(saved))
            with self.assertRaises(ValueError):
                read_history(path)
