import unittest
from datetime import UTC, datetime

from video_monitoring_system import Alert, Camera, DetectionEvent, Recorder, Recording
from video_monitoring_system.model import SOFTWARE_CAMERA_LIMIT


class ModelTests(unittest.TestCase):
    def test_recorder_uses_the_software_camera_envelope(self) -> None:
        recorder = Recorder(id="recorder-1", name="Recorder")
        camera = Camera(id="camera-1", number=1)
        self.assertEqual(recorder.camera_limit, SOFTWARE_CAMERA_LIMIT)
        self.assertEqual(camera.state, "pending")

    def test_information_flow(self) -> None:
        now = datetime.now(UTC)
        recording = Recording(
            id="recording-1",
            camera_id="camera-1",
            started_at=now,
        )
        event = DetectionEvent(
            id="event-1",
            camera_id=recording.camera_id,
            recording_id=recording.id,
            occurred_at=now,
            kind="motion",
        )
        alert = Alert(
            id="alert-1",
            detection_event_id=event.id,
            created_at=now,
            channel="local",
        )
        self.assertEqual(event.recording_id, recording.id)
        self.assertEqual(alert.detection_event_id, event.id)


if __name__ == "__main__":
    unittest.main()
