import base64
import hashlib
import hmac
import io
import json
import sys
import tempfile
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from video_monitoring_system.device.camera_agent import (
    BoundedThreadingHTTPServer,
    CameraApplication,
    CameraRegistrationClient,
    H264StreamBuffer,
    _wireless_link_metrics,
)
from video_monitoring_system.device.network_recovery import (
    NetworkObservation,
    evaluate_recovery,
    load_state,
    save_state,
)


class H264StreamBufferTests(unittest.TestCase):
    def test_new_client_starts_at_repeated_sequence_header(self):
        stream = H264StreamBuffer()
        stream.write(b"\x00\x00\x00\x01\x41delta")
        header = b"\x00\x00\x00\x01\x67sps\x00\x00\x01\x68pps"
        stream.write(header)
        stream.write(b"\x00\x00\x01\x65keyframe")

        sequence, chunk = stream.wait_for_chunk(None, timeout=0.01)
        next_sequence, next_chunk = stream.wait_for_chunk(sequence, timeout=0.01)

        self.assertEqual(chunk, header)
        self.assertEqual(next_sequence, sequence + 1)
        self.assertIn(b"keyframe", next_chunk)

    def test_lagging_client_resynchronizes_at_a_new_header(self):
        stream = H264StreamBuffer(maximum_chunks=2)
        stream.write(b"\x00\x00\x01\x67old-header")
        sequence, _ = stream.wait_for_chunk(None, timeout=0.01)
        stream.write(b"\x00\x00\x01\x41delta-one")
        stream.write(b"\x00\x00\x01\x41delta-two")
        replacement = b"\x00\x00\x01\x67new-header"
        stream.write(replacement)

        resumed_sequence, resumed = stream.wait_for_chunk(sequence, timeout=0.01)

        self.assertGreater(resumed_sequence, sequence + 1)
        self.assertEqual(resumed, replacement)

    def test_stream_client_count_is_bounded(self):
        stream = H264StreamBuffer(maximum_clients=1)

        self.assertTrue(stream.acquire_client())
        self.assertFalse(stream.acquire_client())
        stream.release_client()
        self.assertTrue(stream.acquire_client())


class CameraApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_path = Path(self.temporary.name) / "device.json"
        self.config = {
            "device_id": "CAM-TEST",
            "camera_number": 2,
            "stream_secret": "example-stream-credential",
            "assigned_recorder": {"device_id": "REC-TEST"},
        }
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.marker = Path(self.temporary.name) / "first-boot.json"
        self.marker.write_text(
            '{"schema_version":1,"state":"ready"}\n', encoding="utf-8"
        )
        self.network_health = Path(self.temporary.name) / "network-health.json"
        state = load_state(self.network_health, "a" * 32)
        evaluate_recovery(state, NetworkObservation(True, True, True, True))
        save_state(self.network_health, state)
        self.application = CameraApplication(
            self.config_path,
            provisioning_marker=self.marker,
            network_health_path=self.network_health,
        )

    @staticmethod
    def authorization(user="CAM-TEST", password="example-stream-credential"):
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return f"Basic {token}"

    def test_authentication_fails_closed(self):
        self.assertTrue(self.application.authorized(self.authorization()))
        self.assertFalse(self.application.authorized(None))
        self.assertFalse(self.application.authorized("Bearer anything"))
        self.assertFalse(self.application.authorized("Basic broken"))
        self.assertFalse(
            self.application.authorized(self.authorization(password="wrong"))
        )

    @patch("video_monitoring_system.device.camera_agent._command_value")
    @patch("pathlib.Path.exists", return_value=False)
    def test_health_is_enrolled_and_never_contains_secrets(self, _exists, command):
        command.side_effect = lambda arguments: (
            "running"
            if arguments == ["systemctl", "is-system-running"]
            else "throttled=0x0"
        )
        self.application.stream.write(b"\x00\x00\x01\x67sps")

        health = self.application.health()

        self.assertTrue(health["camera_ready"])
        self.assertEqual(health["capture_state"], "ready")
        self.assertEqual(health["schema_version"], 4)
        self.assertTrue(health["system_ready"])
        self.assertTrue(health["claimed"])
        self.assertFalse(health["provisioning_mode"])
        self.assertEqual(health["network"]["state"], "healthy")
        self.assertTrue(health["network"]["recorder_reachable"])
        self.assertEqual(
            health["media"]["live"],
            {
                "codec": "h264",
                "path": "/stream.h264",
                "width": 1920,
                "height": 1080,
                "fps": 15,
                "bitrate": 3_000_000,
            },
        )
        self.assertNotIn("example-stream-credential", json.dumps(health))

    def test_wireless_metrics_are_bounded_and_hide_interface_identity(self):
        wireless = Path(self.temporary.name) / "wireless"
        wireless.write_text(
            "Inter-| sta-| Quality        | Discarded packets\n"
            " wlan0: 0000   25.  -85.  -256        0      0      0\n",
            encoding="utf-8",
        )

        metrics = _wireless_link_metrics(wireless)

        self.assertEqual(metrics, {"rssi_dbm": -85, "signal_percent": 36})
        self.assertNotIn("wlan0", json.dumps(metrics))
        self.assertEqual(
            _wireless_link_metrics(wireless.with_name("missing")),
            {"rssi_dbm": None, "signal_percent": None},
        )

    @patch("video_monitoring_system.device.camera_agent._command_value")
    @patch("video_monitoring_system.device.camera_agent.monotonic", return_value=121)
    @patch("pathlib.Path.exists", return_value=False)
    def test_health_reports_capture_that_started_without_video(
        self, _exists, _monotonic, command
    ):
        command.side_effect = lambda arguments: (
            "running"
            if arguments == ["systemctl", "is-system-running"]
            else "throttled=0x0"
        )
        self.application._capture_started_at = 100

        health = self.application.health()

        self.assertFalse(health["camera_ready"])
        self.assertEqual(health["capture_state"], "stalled")
        self.assertEqual(
            health["error"],
            "Camera capture started but produced no video.",
        )

    @patch("video_monitoring_system.device.camera_agent._command_value")
    @patch("video_monitoring_system.device.camera_agent.monotonic", return_value=110)
    @patch("pathlib.Path.exists", return_value=False)
    def test_health_allows_camera_startup_grace(
        self, _exists, _monotonic, command
    ):
        command.side_effect = lambda arguments: (
            "running"
            if arguments == ["systemctl", "is-system-running"]
            else "throttled=0x0"
        )
        self.application._capture_started_at = 100

        health = self.application.health()

        self.assertFalse(health["camera_ready"])
        self.assertEqual(health["capture_state"], "starting")
        self.assertIsNone(health["error"])

    def test_missing_recovery_state_fails_closed_without_exposing_a_path(self):
        self.network_health.unlink()

        health = self.application.health()

        self.assertEqual(health["network"]["state"], "unknown")
        self.assertFalse(health["network"]["recorder_reachable"])
        self.assertNotIn(str(self.network_health), json.dumps(health))

    @patch("video_monitoring_system.device.camera_agent._command_value")
    @patch("pathlib.Path.exists", return_value=False)
    def test_live_capture_cannot_hide_an_unfinished_boot(self, _exists, command):
        command.side_effect = lambda arguments: (
            "starting"
            if arguments == ["systemctl", "is-system-running"]
            else "throttled=0x0"
        )
        self.application.stream.write(b"\x00\x00\x01\x67sps")

        health = self.application.health()

        self.assertTrue(health["camera_ready"])
        self.assertFalse(health["system_ready"])

        self.marker.write_text("not-json\n", encoding="utf-8")
        command.side_effect = lambda arguments: (
            "running"
            if arguments == ["systemctl", "is-system-running"]
            else "throttled=0x0"
        )
        self.assertFalse(self.application.health()["system_ready"])

    def test_snapshot_encodes_lores_yuv_planes_and_produces_jpeg(self):
        camera = Mock()
        pixels = MagicMock()
        pixels.shape = (540, 640)
        pixels.strides = (640, 1)
        camera.capture_array.return_value = pixels
        self.application.camera = camera
        encoder = Mock(
            return_value=b"\xff\xd8camera\xff\xd9",
        )
        simplejpeg_module = types.SimpleNamespace(
            encode_jpeg_yuv_planes=encoder,
        )

        with patch.dict(sys.modules, {"simplejpeg": simplejpeg_module}):
            frame = self.application.snapshot()

        self.assertEqual(frame, b"\xff\xd8camera\xff\xd9")
        camera.capture_array.assert_called_once_with("lores")
        pixels.reshape.assert_called_once_with((1080, 320))
        self.assertEqual(encoder.call_args.args[-1], 80)

    def test_concurrent_snapshot_fails_instead_of_waiting(self):
        self.application.camera = Mock()
        self.assertTrue(self.application._snapshot_lock.acquire(blocking=False))
        self.addCleanup(self.application._snapshot_lock.release)

        with self.assertRaisesRegex(RuntimeError, "snapshot is busy"):
            self.application.snapshot()


class BoundedThreadingHTTPServerTests(unittest.TestCase):
    def setUp(self):
        self.server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0),
            Mock,
            maximum_requests=1,
        )
        self.addCleanup(self.server.server_close)

    @patch.object(BoundedThreadingHTTPServer, "shutdown_request")
    @patch.object(ThreadingHTTPServer, "process_request")
    def test_request_count_is_bounded(self, parent_process, shutdown):
        first = Mock()
        second = Mock()

        self.server.process_request(first, ("127.0.0.1", 1))
        self.server.process_request(second, ("127.0.0.1", 2))

        parent_process.assert_called_once_with(first, ("127.0.0.1", 1))
        shutdown.assert_called_once_with(second)

    @patch.object(ThreadingHTTPServer, "process_request")
    def test_thread_start_failure_releases_request_slot(self, parent_process):
        parent_process.side_effect = [RuntimeError("thread unavailable"), None]

        with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
            self.server.process_request(Mock(), ("127.0.0.1", 1))
        self.server.process_request(Mock(), ("127.0.0.1", 2))

        self.assertEqual(parent_process.call_count, 2)


class CameraRegistrationClientTests(unittest.TestCase):
    @patch("video_monitoring_system.device.camera_agent.urlopen")
    def test_registration_is_canonical_and_authenticated(self, open_url):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                return False

            @staticmethod
            def read(_maximum):
                return b"{}"

        open_url.return_value = Response()
        client = CameraRegistrationClient(
            camera_id="CAM-0123456789ABCDEF",
            stream_secret="example-stream-credential",
            recorder_hosts=("osecameras.local",),
        )

        self.assertTrue(client.register())

        request = open_url.call_args.args[0]
        expected = b'{"camera_id":"CAM-0123456789ABCDEF","port":9080,"schema_version":1}'
        self.assertEqual(request.full_url, "http://osecameras.local/api/v1/camera-registration")
        self.assertEqual(request.data, expected)
        self.assertEqual(
            request.get_header("X-vms-signature"),
            hmac.new(
                b"example-stream-credential", expected, hashlib.sha256
            ).hexdigest(),
        )

    @patch("video_monitoring_system.device.camera_agent.urlopen")
    def test_registration_does_not_fall_back_to_recorder_hotspot(self, open_url):
        open_url.side_effect = OSError("home LAN unavailable")
        client = CameraRegistrationClient(
            camera_id="CAM-0123456789ABCDEF",
            stream_secret="example-stream-credential",
            recorder_hosts=("osecameras.local",),
        )

        self.assertFalse(client.register())
        self.assertEqual(open_url.call_count, 1)
        self.assertEqual(
            open_url.call_args.args[0].full_url,
            "http://osecameras.local/api/v1/camera-registration",
        )


if __name__ == "__main__":
    unittest.main()
