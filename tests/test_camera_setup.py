import json
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from video_monitoring_system.camera_setup import (
    CameraSetupCoordinator,
    CameraSetupError,
    CameraSetupTransport,
    VerifiedCameraEnrollment,
)


CAMERA_ID = "CAM-0123456789ABCDEF"
RECORDER_ID = "REC-FEDCBA9876543210"
BOOTSTRAP = "sha256:" + "a" * 64
PERMANENT = "identity:" + "b" * 43
TRANSACTION = "transaction_0123456789abcdef"
NOW = datetime(2026, 9, 29, tzinfo=UTC)


class CameraSetupCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.coordinator = CameraSetupCoordinator(
            self.root,
            token_factory=lambda: TRANSACTION,
        )

    def begin(self):
        return self.coordinator.begin(
            camera_id=CAMERA_ID,
            recorder_id=RECORDER_ID,
            bootstrap_identity=BOOTSTRAP,
            permanent_identity=PERMANENT,
            transport="dpp",
            now=NOW,
        )

    @staticmethod
    def result(transaction_id=TRANSACTION, bootstrap_identity=BOOTSTRAP):
        return VerifiedCameraEnrollment(
            transaction_id=transaction_id,
            camera_id=CAMERA_ID,
            recorder_id=RECORDER_ID,
            bootstrap_identity=bootstrap_identity,
            permanent_identity=PERMANENT,
            transport="dpp",
        )

    def test_transaction_is_resumable_and_persists_only_public_binding(self):
        binding = self.begin()

        self.assertNotIn(TRANSACTION, repr(binding))
        self.assertEqual(
            self.coordinator.status(CAMERA_ID)["state"],
            "waiting-network",
        )
        path = self.root / "camera-setup.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        content = path.read_text(encoding="utf-8")
        for forbidden in (
            "wifi_passphrase",
            "stream_secret",
            "claim_secret",
            "password",
            "psk",
        ):
            self.assertNotIn(forbidden, content.lower())

        resumed = CameraSetupCoordinator(self.root)
        self.assertEqual(resumed.binding(TRANSACTION), binding)
        self.assertNotIn("bootstrap", resumed.status(CAMERA_ID))
        self.assertNotIn("permanent", resumed.status(CAMERA_ID))

    def test_verified_enrollment_heartbeat_and_media_advance_in_order(self):
        self.begin()

        waiting = self.coordinator.accept_enrollment(
            self.result(), now=NOW + timedelta(seconds=1)
        )
        verifying = self.coordinator.observe_heartbeat(
            CAMERA_ID, now=NOW + timedelta(seconds=2)
        )
        ready = self.coordinator.mark_ready(
            CAMERA_ID, now=NOW + timedelta(seconds=3)
        )

        self.assertEqual(waiting["state"], "waiting-heartbeat")
        self.assertEqual(verifying["state"], "verifying-video")
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(self.coordinator.mark_ready(CAMERA_ID), ready)

    def test_mismatched_bootstrap_identity_fails_without_advancing(self):
        self.begin()

        with self.assertRaisesRegex(CameraSetupError, "identity did not match"):
            self.coordinator.accept_enrollment(
                self.result(bootstrap_identity="sha256:" + "c" * 64)
            )

        self.assertEqual(
            self.coordinator.status(CAMERA_ID)["state"],
            "waiting-network",
        )

    def test_heartbeat_before_verified_network_enrollment_fails_closed(self):
        self.begin()

        with self.assertRaisesRegex(CameraSetupError, "out of order"):
            self.coordinator.observe_heartbeat(CAMERA_ID)

        self.assertEqual(
            self.coordinator.status(CAMERA_ID)["state"],
            "waiting-network",
        )

    def test_failure_is_publicly_bounded_and_retryable(self):
        self.begin()

        attention = self.coordinator.mark_attention(
            CAMERA_ID,
            issue="network-timeout",
            now=NOW + timedelta(seconds=1),
        )
        retry = self.coordinator.retry(
            CAMERA_ID, now=NOW + timedelta(seconds=2)
        )

        self.assertEqual(attention["state"], "needs-attention")
        self.assertEqual(attention["issue"], "network-timeout")
        self.assertEqual(retry["state"], "waiting-network")
        self.assertIsNone(retry["issue"])
        with self.assertRaisesRegex(ValueError, "issue is invalid"):
            self.coordinator.mark_attention(CAMERA_ID, issue="private details")

    def test_malformed_or_secret_bearing_saved_state_fails_closed(self):
        (self.root / "camera-setup.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "setups": {
                        CAMERA_ID: {
                            "camera_id": CAMERA_ID,
                            "wifi_passphrase": "must-not-be-accepted",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(CameraSetupError, "could not be read"):
            self.coordinator.status(CAMERA_ID)

    def test_transport_protocol_exposes_only_verified_result_boundary(self):
        class DppAdapter:
            name = "dpp"

            def verified_enrollment(self, binding):
                return self_result(binding.transaction_id)

        self_result = self.result
        adapter = DppAdapter()

        self.assertIsInstance(adapter, CameraSetupTransport)
        self.assertEqual(
            adapter.verified_enrollment(self.begin()).camera_id,
            CAMERA_ID,
        )


if __name__ == "__main__":
    unittest.main()
