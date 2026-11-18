import unittest
from types import SimpleNamespace
from unittest.mock import patch

from video_monitoring_system.provisioning import ProvisioningJob, ProvisioningSpec
from video_monitoring_system.verification import (
    CAMERA_HEALTH_NAMES,
    HEALTH_ATTENTION,
    HEALTH_NOT_CHECKED,
    HEALTH_OK,
    CheckResult,
    VerificationReport,
    camera_media_routes,
    camera_health_summary,
    camera_support_report,
    HttpCameraTransport,
    verify_camera,
)


class FakeTransport:
    def __init__(
        self,
        health,
        snapshot=b"\xff\xd8frame\xff\xd9",
        stream=(b"\xff\xd8frame-one\xff\xd9", b"\xff\xd8frame-two\xff\xd9"),
        h264=(
            b"\x00\x00\x00\x01\x67sps"
            b"\x00\x00\x00\x01\x68pps"
            b"\x00\x00\x00\x01\x65keyframe"
        ),
    ):
        self.health = health
        self.snapshot = snapshot
        self.stream = stream
        self.h264 = h264

    def get_json(self, path):
        return self.health

    def get_bytes(self, path, *, maximum):
        return self.snapshot

    def get_mjpeg_frames(self, path, *, maximum, count):
        return self.stream[:count]

    def get_stream_prefix(
        self, path, *, minimum, maximum, timeout_seconds
    ):
        self.h264_call = (path, minimum, maximum, timeout_seconds)
        return self.h264


class VerificationTests(unittest.TestCase):
    def test_media_routes_allow_legacy_alias_and_distinct_detection_stream(self):
        legacy = camera_media_routes({"schema_version": 3})
        self.assertEqual(legacy["main"], legacy["detection"])

        routes = camera_media_routes(
            {
                "schema_version": 4,
                "media": {
                    "live": {
                        "codec": "h264",
                        "path": "/stream.h264",
                        "width": 1920,
                        "height": 1080,
                        "fps": 15,
                        "bitrate": 3_000_000,
                    },
                    "detection": {
                        "codec": "h264",
                        "path": "/detect.h264",
                        "width": 640,
                        "height": 360,
                        "fps": 5,
                        "bitrate": 500_000,
                    },
                },
            }
        )

        self.assertEqual(routes["main"][0], "/stream.h264")
        self.assertEqual(routes["detection"][0], "/detect.h264")

    def test_camera_health_is_a_whitelisted_projection(self) -> None:
        report = VerificationReport(
            schema_version=1,
            device_id="CAM-0001",
            checked_at="2026-08-16T12:00:00+00:00",
            checks=(
                CheckResult("power", True, "linux detail: secret-value"),
                CheckResult("connection", True, "192.0.2.10"),
                CheckResult("capture", False, "sensor driver path"),
                CheckResult("image", True, "private stream address"),
                CheckResult("stream", True, "private stream address"),
                CheckResult("identity", True, "secret identity detail"),
                CheckResult("number", True, "internal number"),
                CheckResult("enrollment", True, "ssh key path"),
                CheckResult("startup", True, "systemd internal detail"),
                CheckResult("model", False, "ignored internal check"),
            ),
        )

        health = camera_health_summary(
            "CAM-0001", camera_name="Driveway", verification=report
        )
        statuses = {item.name: item.status for item in health.statuses}
        output = camera_support_report(
            (health,), generated_at="2026-08-16T12:01:00+00:00"
        )

        self.assertEqual(tuple(statuses), CAMERA_HEALTH_NAMES)
        self.assertEqual(
            statuses,
            {
                "Power": HEALTH_OK,
                "Network": HEALTH_OK,
                "Sensor": HEALTH_ATTENTION,
                "Live Video": HEALTH_OK,
                "Recorder": HEALTH_OK,
            },
        )
        for private_detail in (
            "linux detail",
            "secret-value",
            "192.0.2.10",
            "driver path",
            "ssh",
        ):
            self.assertNotIn(private_detail, output.lower())

    def test_current_disconnect_overrides_an_older_successful_check(self) -> None:
        report = VerificationReport(
            schema_version=1,
            device_id="CAM-0001",
            checked_at="2026-08-16T12:00:00+00:00",
            checks=tuple(
                CheckResult(name, True, "Passed.")
                for name in (
                    "power",
                    "connection",
                    "capture",
                    "image",
                    "stream",
                    "identity",
                    "number",
                    "enrollment",
                )
            ),
        )

        health = camera_health_summary(
            "CAM-0001", connected=False, verification=report
        )
        statuses = {item.name: item.status for item in health.statuses}

        self.assertEqual(statuses["Power"], HEALTH_NOT_CHECKED)
        self.assertEqual(statuses["Sensor"], HEALTH_NOT_CHECKED)
        for name in ("Network", "Live Video", "Recorder"):
            self.assertEqual(statuses[name], HEALTH_ATTENTION)

    def test_camera_passes_without_exposing_secrets(self) -> None:
        camera = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1, camera_board="zero-w"),
            wifi_country="US",
        ).identity.cameras[0]
        report = verify_camera(
            camera,
            transport=FakeTransport(
                {
                    "schema_version": 3,
                    "device_id": camera.device_id,
                    "camera_number": 1,
                    "model": "Raspberry Pi Zero W Rev 1.1",
                    "camera_ready": True,
                    "power": "throttled=0x0",
                    "claimed": True,
                    "provisioning_mode": False,
                    "system_ready": True,
                    "network": {
                        "state": "healthy",
                        "failed_layer": "none",
                        "recorder_reachable": True,
                        "current_boot": {
                            "status": "healthy",
                            "failed_layer": "none",
                            "recorder_reachable": True,
                            "observations": 3,
                            "reconnects": 0,
                            "reboot_requested": False,
                        },
                        "previous_boot": None,
                    },
                }
            ),
        )
        self.assertTrue(report.passed)
        output = report.to_json()
        self.assertNotIn(camera.stream_secret, output)
        self.assertNotIn(camera.claim_secret, output)
        self.assertNotIn(camera.key.private_identity, output)
        self.assertTrue(
            next(check for check in report.checks if check.name == "network").passed
        )
        self.assertTrue(
            next(check for check in report.checks if check.name == "recorder").passed
        )
        self.assertTrue(
            next(check for check in report.checks if check.name == "resilience").passed
        )
        self.assertTrue(
            next(check for check in report.checks if check.name == "stream").passed
        )

    def test_camera_network_failure_uses_the_shared_health_contract(self) -> None:
        report = VerificationReport(
            schema_version=1,
            device_id="CAM-0001",
            checked_at="2026-08-27T12:00:00+00:00",
            checks=(
                CheckResult("power", True, "Passed."),
                CheckResult("connection", True, "Passed."),
                CheckResult("network", False, "Private detail."),
                CheckResult("capture", True, "Passed."),
                CheckResult("image", True, "Passed."),
                CheckResult("stream", True, "Passed."),
                CheckResult("identity", True, "Passed."),
                CheckResult("number", True, "Passed."),
                CheckResult("enrollment", True, "Passed."),
                CheckResult("startup", True, "Passed."),
                CheckResult("recorder", False, "Private detail."),
            ),
        )

        health = camera_health_summary("CAM-0001", verification=report)
        statuses = {item.name: item.status for item in health.statuses}

        self.assertEqual(statuses["Network"], HEALTH_ATTENTION)
        self.assertEqual(statuses["Recorder"], HEALTH_ATTENTION)

    def test_native_h264_camera_passes_the_live_gate(self) -> None:
        camera = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1, camera_board="zero-w"),
            wifi_country="US",
        ).identity.cameras[0]
        transport = FakeTransport(
            {
                "schema_version": 4,
                "device_id": camera.device_id,
                "camera_number": 1,
                "model": "Raspberry Pi Zero W Rev 1.1",
                "camera_ready": True,
                "power": "throttled=0x0",
                "claimed": True,
                "provisioning_mode": False,
                "system_ready": True,
                "network": {
                    "state": "healthy",
                    "failed_layer": "none",
                    "recorder_reachable": True,
                    "current_boot": {
                        "status": "healthy",
                        "failed_layer": "none",
                        "recorder_reachable": True,
                        "observations": 3,
                        "reconnects": 0,
                        "reboot_requested": False,
                    },
                    "previous_boot": None,
                },
                "media": {
                    "live": {
                        "codec": "h264",
                        "path": "/stream.h264",
                        "width": 1920,
                        "height": 1080,
                        "fps": 15,
                        "bitrate": 3_000_000,
                    }
                },
            }
        )

        report = verify_camera(camera, transport=transport)

        self.assertTrue(report.passed)
        self.assertEqual(transport.h264_call[0], "/stream.h264")
        self.assertTrue(
            next(check for check in report.checks if check.name == "stream").passed
        )

    def test_incomplete_mjpeg_stream_fails_the_acceptance_gate(self) -> None:
        camera = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1, camera_board="zero-w"),
            wifi_country="US",
        ).identity.cameras[0]
        report = verify_camera(
            camera,
            transport=FakeTransport(
                {
                    "schema_version": 2,
                    "device_id": camera.device_id,
                    "camera_number": 1,
                    "model": "Raspberry Pi Zero W Rev 1.1",
                    "camera_ready": True,
                    "power": "throttled=0x0",
                    "claimed": True,
                    "provisioning_mode": False,
                    "system_ready": True,
                },
                stream=(b"not an MJPEG stream",),
            ),
        )

        self.assertFalse(report.passed)
        self.assertFalse(next(check for check in report.checks if check.name == "stream").passed)

    def test_repeated_mjpeg_placeholder_frames_fail_the_live_gate(self) -> None:
        camera = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1, camera_board="zero-w"),
            wifi_country="US",
        ).identity.cameras[0]
        placeholder = b"\xff\xd8changing-recorder-timestamp-is-not-camera-video\xff\xd9"

        report = verify_camera(
            camera,
            transport=FakeTransport(
                {
                    "schema_version": 2,
                    "device_id": camera.device_id,
                    "camera_number": 1,
                    "model": "Raspberry Pi Zero W Rev 1.1",
                    "camera_ready": True,
                    "power": "throttled=0x0",
                    "claimed": True,
                    "provisioning_mode": False,
                    "system_ready": True,
                },
                stream=(placeholder, placeholder),
            ),
        )

        self.assertFalse(report.passed)
        self.assertFalse(
            next(check for check in report.checks if check.name == "stream").passed
        )

    def test_http_transport_rejects_url_injection_and_invalid_ports(self) -> None:
        with self.assertRaisesRegex(ValueError, "hostname"):
            HttpCameraTransport("trusted.invalid/path", "CAM-1", "secret")
        with self.assertRaisesRegex(ValueError, "port"):
            HttpCameraTransport("camera.local", "CAM-1", "secret", port=0)

    def test_http_transport_builds_private_authenticated_engine_url(self) -> None:
        transport = HttpCameraTransport(
            "camera.local", "CAM-1", "secret/value", port=9080
        )

        self.assertEqual(
            transport.authenticated_url("/stream.mjpg"),
            "http://CAM-1:secret%2Fvalue@camera.local:9080/stream.mjpg",
        )
        self.assertNotIn("secret/value", repr(transport))
        with self.assertRaises(ValueError):
            transport.authenticated_url("//external.invalid/stream.mjpg")

    def test_http_transport_resolves_private_engine_url_on_the_recorder(self) -> None:
        transport = HttpCameraTransport(
            "camera.local", "CAM-1", "secret/value", port=9080
        )
        answer = (2, 1, 6, "", ("10.42.0.200", 9080))

        with patch(
            "video_monitoring_system.verification.socket.getaddrinfo",
            return_value=[answer, answer],
        ):
            self.assertEqual(
                transport.authenticated_url("/stream.mjpg", resolve=True),
                "http://CAM-1:secret%2Fvalue@10.42.0.200:9080/stream.mjpg",
            )

    def test_http_transport_refuses_public_or_ambiguous_engine_resolution(self) -> None:
        transport = HttpCameraTransport("camera.local", "CAM-1", "secret")
        with patch(
            "video_monitoring_system.verification.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("8.8.8.8", 9080))],
        ):
            with self.assertRaisesRegex(ValueError, "private address"):
                transport.authenticated_url("/stream.mjpg", resolve=True)
        with patch(
            "video_monitoring_system.verification.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("10.42.0.200", 9080)),
                (2, 1, 6, "", ("10.42.0.201", 9080)),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "one private address"):
                transport.authenticated_url("/stream.mjpg", resolve=True)

    def test_http_stream_uses_one_decreasing_total_timeout_budget(self) -> None:
        connection = SimpleNamespace(timeouts=[])
        connection.settimeout = connection.timeouts.append

        class Response:
            headers = SimpleNamespace(
                get_content_type=lambda: "multipart/x-mixed-replace"
            )
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=connection))

            def __init__(self):
                self.chunks = iter(
                    (
                        b"\xff",
                        b"\xd8frame-one\xff\xd9\xff",
                        b"\xd8frame-two\xff",
                        b"\xd9",
                    )
                )

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read1(self, maximum):
                return next(self.chunks)

        with (
            patch(
                "video_monitoring_system.verification.urlopen",
                return_value=Response(),
            ) as open_url,
            patch(
                "video_monitoring_system.verification.monotonic",
                side_effect=(10.0, 10.1, 10.2, 10.3, 10.4),
            ),
        ):
            frames = HttpCameraTransport(
                "camera.local", "CAM-1", "secret"
            ).get_mjpeg_frames(
                "/stream.mjpg", maximum=4096, count=2, timeout_seconds=2
            )

        self.assertEqual(len(frames), 2)
        self.assertEqual(len(connection.timeouts), 4)
        self.assertTrue(
            all(
                earlier > later
                for earlier, later in zip(
                    connection.timeouts, connection.timeouts[1:]
                )
            )
        )
        self.assertEqual(open_url.call_args.kwargs["timeout"], 2.0)

    def test_http_stream_stops_when_total_timeout_is_spent(self) -> None:
        class Response:
            headers = SimpleNamespace(
                get_content_type=lambda: "multipart/x-mixed-replace"
            )

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with (
            patch(
                "video_monitoring_system.verification.urlopen",
                return_value=Response(),
            ),
            patch(
                "video_monitoring_system.verification.monotonic",
                side_effect=(10.0, 12.1),
            ),
            self.assertRaisesRegex(TimeoutError, "timed out"),
        ):
            HttpCameraTransport("camera.local", "CAM-1", "secret").get_mjpeg_frames(
                "/stream.mjpg", maximum=4096, count=2, timeout_seconds=2
            )

    def test_http_h264_stream_reads_a_bounded_decodable_prefix(self) -> None:
        connection = SimpleNamespace(timeouts=[])
        connection.settimeout = connection.timeouts.append
        payload = (
            b"\x00\x00\x00\x01\x67sps"
            b"\x00\x00\x00\x01\x68pps"
            b"\x00\x00\x00\x01\x65keyframe"
        )

        class Response:
            headers = SimpleNamespace(get_content_type=lambda: "video/h264")
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=connection))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read1(self, maximum):
                return payload

        with (
            patch(
                "video_monitoring_system.verification.urlopen",
                return_value=Response(),
            ),
            patch(
                "video_monitoring_system.verification.monotonic",
                side_effect=(10.0, 10.1),
            ),
        ):
            observed = HttpCameraTransport(
                "camera.local", "CAM-1", "secret"
            ).get_stream_prefix(
                "/stream.h264",
                minimum=1,
                maximum=4096,
                timeout_seconds=2,
            )

        self.assertEqual(observed, payload)
        self.assertEqual(len(connection.timeouts), 1)
        self.assertAlmostEqual(connection.timeouts[0], 1.9)

    def test_live_video_does_not_hide_an_unfinished_boot(self) -> None:
        camera = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1, camera_board="zero-w"),
            wifi_country="US",
        ).identity.cameras[0]

        report = verify_camera(
            camera,
            transport=FakeTransport(
                {
                    "schema_version": 2,
                    "device_id": camera.device_id,
                    "camera_number": 1,
                    "model": "Raspberry Pi Zero W Rev 1.1",
                    "camera_ready": True,
                    "system_ready": False,
                    "power": "throttled=0x0",
                    "claimed": True,
                    "provisioning_mode": False,
                }
            ),
        )

        self.assertFalse(report.passed)
        self.assertFalse(
            next(check for check in report.checks if check.name == "startup").passed
        )

    def test_legacy_health_without_boot_readiness_fails_closed(self) -> None:
        camera = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1, camera_board="zero-w"),
            wifi_country="US",
        ).identity.cameras[0]

        report = verify_camera(
            camera,
            transport=FakeTransport(
                {
                    "schema_version": 1,
                    "device_id": camera.device_id,
                    "camera_number": 1,
                    "model": "Raspberry Pi Zero W Rev 1.1",
                    "camera_ready": True,
                    "power": "throttled=0x0",
                    "claimed": True,
                    "provisioning_mode": False,
                }
            ),
        )

        self.assertFalse(report.passed)
        self.assertFalse(
            next(check for check in report.checks if check.name == "startup").passed
        )

    def test_failed_health_is_clear_and_redacted(self) -> None:
        camera = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1), wifi_country="US"
        ).identity.cameras[0]

        class Failed:
            def get_json(self, path):
                raise RuntimeError("private detail")

        report = verify_camera(camera, transport=Failed())
        self.assertFalse(report.passed)
        self.assertEqual(report.checks[0].message, "Camera did not respond.")
        self.assertNotIn("private detail", report.to_json())


if __name__ == "__main__":
    unittest.main()
