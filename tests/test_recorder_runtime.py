import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from video_monitoring_system.recorder import RecorderService
from video_monitoring_system.recorder_runtime import RecorderRuntime
from video_monitoring_system.recording import (
    RecordingStorage,
    RecordingVolumeAttestation,
)


JPEG_ONE = b"\xff\xd8frame-one\xff\xd9"
JPEG_TWO = b"\xff\xd8frame-two\xff\xd9"


class Source:
    def __init__(self, camera_id: str, barrier: threading.Barrier | None = None):
        self.camera_id = camera_id
        self.barrier = barrier

    def get_mjpeg_frames(
        self, path, *, maximum, count, timeout_seconds
    ):
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        return (JPEG_ONE, JPEG_TWO)


class FailedSource(Source):
    def get_mjpeg_frames(
        self, path, *, maximum, count, timeout_seconds
    ):
        raise RuntimeError("private camera address and credential")


class RecorderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        volume = root / "recording-volume"
        volume.mkdir()
        self.recorder = RecorderService(root / "recorder.json")
        self.recorder.plan_storage_initialization(storage_reference="recordings")
        self.recorder.confirm_storage_initialized(
            storage_reference="recordings", volume_identity="volume-1"
        )
        self.attestation = RecordingVolumeAttestation(
            root=volume,
            storage_reference="recordings",
            volume_identity="volume-1",
            mounted=True,
            system_device=False,
        )
        storage = RecordingStorage.initialize_layout(
            self.attestation, reserve_bytes=0
        )
        self.runtime = RecorderRuntime(self.recorder, storage)

    def register(self, camera_id: str):
        self.recorder.register_camera(
            camera_id=camera_id,
            claim_secret_hash=hashlib.sha256(camera_id.encode()).hexdigest(),
            identity_reference=f"identity:{camera_id}",
            pre_enrolled=True,
        )

    def test_continuous_cycle_captures_cameras_concurrently(self):
        self.register("CAM-0001")
        self.register("CAM-0002")
        self.recorder.set_recording(mode="continuous", days=21)
        barrier = threading.Barrier(2)

        result = self.runtime.capture_cycle(
            (Source("CAM-0001", barrier), Source("CAM-0002", barrier)),
            frame_count=2,
            maximum_bytes=4096,
        )

        self.assertEqual(result["state"], "recording")
        self.assertEqual(
            [item["camera_id"] for item in result["cameras"]],
            ["CAM-0001", "CAM-0002"],
        )
        self.assertTrue(
            all(item["state"] == "recorded" for item in result["cameras"])
        )

    def test_one_failed_camera_is_redacted_and_does_not_stop_another(self):
        self.register("CAM-0001")
        self.register("CAM-0002")
        self.recorder.set_recording(mode="continuous", days=21)

        result = self.runtime.capture_cycle(
            (FailedSource("CAM-0001"), Source("CAM-0002")),
            frame_count=2,
            maximum_bytes=4096,
        )

        rendered = str(result)
        self.assertIn("needs_attention", rendered)
        self.assertIn("recorded", rendered)
        self.assertNotIn("private camera", rendered)

    def test_event_mode_does_not_access_camera_sources(self):
        self.register("CAM-0001")
        self.recorder.set_recording(mode="events", days=21)

        result = self.runtime.capture_cycle((FailedSource("CAM-0001"),))

        self.assertEqual(result, {"state": "idle", "cameras": []})

    def test_paused_camera_is_not_recorded_or_reported_unavailable(self):
        self.register("CAM-0001")
        self.recorder.set_recording(mode="continuous", days=21)
        self.recorder.set_camera_recording(camera_id="CAM-0001", enabled=False)

        result = self.runtime.capture_cycle((FailedSource("CAM-0001"),))

        self.assertEqual(result, {"state": "waiting", "cameras": []})
        self.assertEqual(self.recorder.active_camera_ids, frozenset({"CAM-0001"}))

    def test_managed_camera_is_not_recorded_by_legacy_capture(self):
        self.register("CAM-0001")
        self.register("CAM-0002")
        self.recorder.set_recording(mode="continuous", days=21)

        result = self.runtime.capture_cycle(
            (FailedSource("CAM-0001"), Source("CAM-0002")),
            managed_camera_ids=frozenset({"CAM-0001"}),
            frame_count=2,
            maximum_bytes=4096,
        )

        self.assertEqual(result["state"], "recording")
        self.assertEqual(
            result["cameras"],
            [
                {"camera_id": "CAM-0001", "state": "managed"},
                {
                    "camera_id": "CAM-0002",
                    "state": "recorded",
                    "recording_id": result["cameras"][1]["recording_id"],
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
