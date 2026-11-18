import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_monitoring_system.model import SOFTWARE_CAMERA_LIMIT
from video_monitoring_system.recorder import (
    RecorderError,
    RecorderService,
    RecorderStateError,
)


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class RecorderServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.state_path = Path(self.temporary_directory.name) / "state" / "recorder.json"
        self.service = RecorderService(self.state_path)

    def register(self, camera_id: str = "CAM-0001", secret: str = "claim-one") -> str:
        digest = secret_hash(secret)
        self.service.register_camera(
            camera_id=camera_id,
            claim_secret_hash=digest,
            identity_reference=f"identity:{camera_id}",
        )
        return digest

    def test_owner_requires_opaque_credential_and_is_idempotent(self) -> None:
        with self.assertRaisesRegex(RecorderError, "pre-hashed"):
            self.service.setup_owner(name="Owner", credential_reference="plain-password")

        result = self.service.setup_owner(name="Owner", credential_reference="hash:argon2id:encoded")
        self.assertEqual(result["result"], "configured")
        self.assertEqual(
            self.service.setup_owner(name="Owner", credential_reference="hash:argon2id:encoded")["result"],
            "already_configured",
        )
        saved = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn("plain-password", saved)
        with self.assertRaisesRegex(RecorderError, "already exists"):
            self.service.setup_owner(name="Other", credential_reference="ref:vault:owner")

    def test_owner_credential_can_be_changed_without_changing_owner_name(self) -> None:
        with self.assertRaisesRegex(RecorderError, "created first"):
            self.service.change_owner_credential(
                credential_reference="hash:scrypt:new-encoded-value"
            )
        self.service.setup_owner(
            name="Owner", credential_reference="hash:scrypt:old-encoded-value"
        )

        result = self.service.change_owner_credential(
            credential_reference="hash:scrypt:new-encoded-value"
        )

        self.assertEqual(result["result"], "updated")
        self.assertEqual(self.service.state["owner"]["name"], "Owner")
        self.assertEqual(
            self.service.state["owner"]["credential_reference"],
            "hash:scrypt:new-encoded-value",
        )
        self.assertEqual(
            self.service.change_owner_credential(
                credential_reference="hash:scrypt:new-encoded-value"
            )["result"],
            "unchanged",
        )

    def test_state_persists_and_snapshot_cannot_mutate_it(self) -> None:
        self.service.setup_owner(name="Owner", credential_reference="ref:secure-store:owner")
        snapshot = self.service.state
        snapshot["owner"]["name"] = "Changed"
        reloaded = RecorderService(self.state_path)
        self.assertEqual(reloaded.state["owner"]["name"], "Owner")

    def test_saved_json_is_atomic_and_leaves_no_temporary_file(self) -> None:
        with patch("video_monitoring_system.recorder.os.replace", wraps=__import__("os").replace) as replace:
            self.service.set_recording(mode="continuous", days=14)
        replace.assert_called_once()
        self.assertEqual(json.loads(self.state_path.read_text())["recording"]["days"], 14)
        self.assertEqual(list(self.state_path.parent.glob("*.tmp")), [])

    def test_failed_atomic_write_restores_in_memory_and_saved_state(self) -> None:
        original = self.service.state
        with (
            patch(
                "video_monitoring_system.recorder.os.replace",
                side_effect=OSError("write failed"),
            ),
            self.assertRaises(OSError),
        ):
            self.service.set_recording(mode="continuous", days=14)
        self.assertEqual(self.service.state, original)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(list(self.state_path.parent.glob("*.tmp")), [])

    def test_stale_camera_writer_does_not_restore_an_old_owner_password(self) -> None:
        self.service.setup_owner(
            name="Owner", credential_reference="hash:argon2id:old"
        )
        self.service.register_camera(
            camera_id="camera-1",
            claim_secret_hash=secret_hash("claim-one"),
            identity_reference="identity:camera-1",
            pre_enrolled=True,
        )
        stale_camera_writer = RecorderService(self.state_path)

        self.service.change_owner_credential(
            credential_reference="hash:argon2id:new"
        )
        stale_camera_writer.set_camera_connected(
            camera_id="camera-1", connected=True
        )

        saved = RecorderService(self.state_path).state
        self.assertEqual(
            saved["owner"]["credential_reference"], "hash:argon2id:new"
        )
        self.assertTrue(saved["cameras"][0]["connected"])

    def test_storage_initialization_is_only_a_plan(self) -> None:
        result = self.service.plan_storage_initialization(storage_reference="recording-store")
        self.assertEqual(result["plan"]["action"], "initialize_storage")
        self.assertTrue(result["plan"]["confirmation_required"])
        self.assertEqual(self.service.status()["storage"], "planned")
        self.assertEqual(
            self.service.plan_storage_initialization(storage_reference="recording-store")["result"],
            "already_planned",
        )

    def test_storage_becomes_ready_only_after_exact_adapter_confirmation(self) -> None:
        with self.assertRaisesRegex(RecorderError, "planned first"):
            self.service.confirm_storage_initialized(
                storage_reference="recording-store",
                volume_identity="volume-uuid-1",
            )
        self.service.plan_storage_initialization(storage_reference="recording-store")
        with self.assertRaisesRegex(RecorderError, "did not match"):
            self.service.confirm_storage_initialized(
                storage_reference="other",
                volume_identity="volume-uuid-1",
            )

        result = self.service.confirm_storage_initialized(
            storage_reference="recording-store",
            volume_identity="volume-uuid-1",
        )

        self.assertEqual(result["result"], "initialized")
        self.assertEqual(self.service.status()["storage"], "ready")
        self.assertEqual(
            self.service.initialized_storage_identity,
            ("recording-store", "volume-uuid-1"),
        )
        self.assertEqual(
            self.service.confirm_storage_initialized(
                storage_reference="recording-store",
                volume_identity="volume-uuid-1",
            )["result"],
            "already_initialized",
        )
        with self.assertRaisesRegex(RecorderError, "identity did not match"):
            self.service.confirm_storage_initialized(
                storage_reference="recording-store",
                volume_identity="volume-uuid-2",
            )
        with self.assertRaisesRegex(RecorderError, "already initialized"):
            self.service.plan_storage_initialization(storage_reference="other")

    def test_register_list_and_claim_consumes_secret(self) -> None:
        digest = self.register()
        listed = self.service.list_unclaimed_cameras()
        self.assertEqual(
            listed,
            [
                {
                    "id": "CAM-0001",
                    "name": None,
                    "state": "unclaimed",
                    "connected": False,
                    "recording_enabled": True,
                    "alerts_enabled": True,
                }
            ],
        )
        self.assertNotIn("secret", json.dumps(listed))

        result = self.service.claim_camera(camera_id="CAM-0001", claim_secret_hash=digest, name="Driveway")
        self.assertEqual(result["result"], "claimed")
        self.assertEqual(self.service.state["cameras"][0]["claim_secret_hash"], None)
        self.assertEqual(self.service.list_unclaimed_cameras(), [])
        retry = self.service.claim_camera(camera_id="CAM-0001", claim_secret_hash=digest, name="Driveway")
        self.assertEqual(retry["result"], "already_claimed")

    def test_factory_enrolled_camera_is_ready_and_can_be_renamed(self) -> None:
        digest = secret_hash("included-camera")
        self.service.register_camera(
            camera_id="CAM-INCLUDED",
            claim_secret_hash=digest,
            identity_reference="identity:CAM-INCLUDED",
            pre_enrolled=True,
        )

        stored = self.service.state["cameras"][0]
        self.assertEqual(stored["state"], "ready")
        self.assertIsNone(stored["claim_secret_hash"])
        self.assertEqual(self.service.list_unclaimed_cameras(), [])
        named = self.service.name_camera(
            camera_id="CAM-INCLUDED", name="Driveway"
        )
        self.assertEqual(named["result"], "named")
        self.assertEqual(
            self.service.name_camera(
                camera_id="CAM-INCLUDED", name="Driveway"
            )["result"],
            "already_named",
        )
        renamed = self.service.name_camera(
            camera_id="CAM-INCLUDED", name="Front Door"
        )
        self.assertEqual(renamed["result"], "renamed")
        self.assertEqual(renamed["camera"]["name"], "Front Door")
        self.assertEqual(self.service.active_camera_ids, frozenset({"CAM-INCLUDED"}))
        self.assertEqual(self.service.recording_camera_ids, frozenset({"CAM-INCLUDED"}))

    def test_manual_code_finds_one_unclaimed_camera_and_is_consumed(self) -> None:
        self.register(camera_id="CAM-0001", secret="example-first-code")
        second = self.register(camera_id="CAM-0002", secret="example-second-code")

        result = self.service.claim_camera_code(
            claim_secret_hash=second,
            name="Back Porch",
        )

        self.assertEqual(result["result"], "claimed")
        self.assertEqual(result["camera"]["id"], "CAM-0002")
        with self.assertRaisesRegex(RecorderError, "did not match"):
            self.service.claim_camera_code(
                claim_secret_hash=second,
                name="Back Porch",
            )
        with self.assertRaisesRegex(RecorderError, "did not match"):
            self.service.claim_camera_code(
                claim_secret_hash=secret_hash("wrong-code"),
                name="Unknown",
            )
        self.assertEqual(
            [camera["id"] for camera in self.service.list_unclaimed_cameras()],
            ["CAM-0001"],
        )

    def test_camera_recording_can_pause_without_revoking_live_access(self) -> None:
        self.service.register_camera(
            camera_id="CAM-INCLUDED",
            claim_secret_hash=secret_hash("included-camera"),
            identity_reference="identity:CAM-INCLUDED",
            pre_enrolled=True,
        )

        paused = self.service.set_camera_recording(
            camera_id="CAM-INCLUDED", enabled=False
        )

        self.assertFalse(paused["camera"]["recording_enabled"])
        self.assertEqual(self.service.active_camera_ids, frozenset({"CAM-INCLUDED"}))
        self.assertEqual(self.service.recording_camera_ids, frozenset())
        reloaded = RecorderService(self.state_path)
        self.assertFalse(reloaded.state["cameras"][0]["recording_enabled"])
        self.assertEqual(
            reloaded.set_camera_recording(
                camera_id="CAM-INCLUDED", enabled=False
            )["result"],
            "unchanged",
        )
        with self.assertRaisesRegex(RecorderError, "setting"):
            reloaded.set_camera_recording(
                camera_id="CAM-INCLUDED", enabled="false"  # type: ignore[arg-type]
            )

    def test_global_alert_pause_preserves_each_camera_preference(self) -> None:
        for camera_id in ("CAM-0001", "CAM-0002"):
            self.service.register_camera(
                camera_id=camera_id,
                claim_secret_hash=secret_hash(camera_id),
                identity_reference=f"identity:{camera_id}",
                pre_enrolled=True,
            )
        self.service.set_camera_alerts(camera_id="CAM-0002", enabled=False)

        paused = self.service.set_alerts_enabled(enabled=False)
        reloaded = RecorderService(self.state_path)

        self.assertFalse(paused["alerts"]["enabled"])
        self.assertFalse(reloaded.alerts_enabled)
        self.assertEqual(reloaded.alert_camera_ids, frozenset({"CAM-0001"}))
        reloaded.set_alerts_enabled(enabled=True)
        self.assertTrue(reloaded.alerts_enabled)
        self.assertEqual(reloaded.alert_camera_ids, frozenset({"CAM-0001"}))
        cameras = {camera["id"]: camera for camera in reloaded.state["cameras"]}
        self.assertTrue(cameras["CAM-0001"]["alerts_enabled"])
        self.assertFalse(cameras["CAM-0002"]["alerts_enabled"])

    def test_old_state_defaults_alert_preferences_to_active(self) -> None:
        self.register()
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        saved.pop("alerts", None)
        saved["cameras"][0].pop("alerts_enabled", None)
        self.state_path.write_text(json.dumps(saved), encoding="utf-8")

        reloaded = RecorderService(self.state_path)

        self.assertTrue(reloaded.alerts_enabled)
        self.assertTrue(reloaded.state["cameras"][0]["alerts_enabled"])

    def test_unclaimed_add_on_cannot_skip_claim_code(self) -> None:
        self.register()
        with self.assertRaisesRegex(RecorderError, "claimed first"):
            self.service.name_camera(camera_id="CAM-0001", name="Driveway")

    def test_camera_position_is_persistent_validated_and_idempotent(self) -> None:
        for number in range(1, 5):
            self.register(f"CAM-{number:04}", f"claim-{number}")

        moved = self.service.position_camera(camera_id="CAM-0004", position=2)

        self.assertEqual(moved["result"], "moved")
        self.assertEqual(moved["position"], 2)
        self.assertEqual(
            [camera["id"] for camera in self.service.state["cameras"]],
            ["CAM-0001", "CAM-0004", "CAM-0002", "CAM-0003"],
        )
        reloaded = RecorderService(self.state_path)
        self.assertEqual(
            [camera["id"] for camera in reloaded.state["cameras"]],
            ["CAM-0001", "CAM-0004", "CAM-0002", "CAM-0003"],
        )
        self.assertEqual(
            reloaded.position_camera(camera_id="CAM-0004", position=2)["result"],
            "unchanged",
        )
        for position in (0, 5, True, "2"):
            with self.subTest(position=position), self.assertRaisesRegex(
                RecorderError, "position is invalid"
            ):
                reloaded.position_camera(camera_id="CAM-0004", position=position)

    def test_camera_position_keeps_revoked_entries_out_of_active_order(self) -> None:
        first = self.register("CAM-0001", "first")
        second = self.register("CAM-0002", "second")
        third = self.register("CAM-0003", "third")
        self.service.claim_camera(
            camera_id="CAM-0001", claim_secret_hash=first, name="First"
        )
        self.service.claim_camera(
            camera_id="CAM-0002", claim_secret_hash=second, name="Second"
        )
        self.service.claim_camera(
            camera_id="CAM-0003", claim_secret_hash=third, name="Third"
        )
        self.service.remove_camera(camera_name="First")

        self.service.position_camera(camera_id="CAM-0003", position=1)

        cameras = self.service.state["cameras"]
        self.assertEqual(
            [camera["id"] for camera in cameras],
            ["CAM-0003", "CAM-0002", "CAM-0001"],
        )
        self.assertTrue(cameras[-1]["revoked"])

    def test_claim_rejects_wrong_or_malformed_hash(self) -> None:
        self.register()
        with self.assertRaisesRegex(RecorderError, "invalid"):
            self.service.claim_camera(camera_id="CAM-0001", claim_secret_hash="not-a-hash", name="Driveway")
        with self.assertRaisesRegex(RecorderError, "did not match"):
            self.service.claim_camera(camera_id="CAM-0001", claim_secret_hash=secret_hash("wrong"), name="Driveway")

    def test_duplicate_identity_is_idempotent_but_conflict_fails(self) -> None:
        digest = self.register()
        same = self.service.register_camera(
            camera_id="CAM-0001", claim_secret_hash=digest, identity_reference="identity:CAM-0001"
        )
        self.assertEqual(same["result"], "already_registered")
        with self.assertRaisesRegex(RecorderError, "already exists"):
            self.service.register_camera(
                camera_id="CAM-0001", claim_secret_hash=secret_hash("different"), identity_reference="identity:other"
            )
        with self.assertRaisesRegex(RecorderError, "already exists"):
            self.service.register_camera(
                camera_id="CAM-0002", claim_secret_hash=secret_hash("second"), identity_reference="identity:CAM-0001"
            )

    def test_identity_must_be_an_opaque_reference(self) -> None:
        with self.assertRaisesRegex(RecorderError, "opaque identity reference"):
            self.service.register_camera(
                camera_id="CAM-0001", claim_secret_hash=secret_hash("first"), identity_reference="private-key-material"
            )

    def test_default_capacity_matches_software_envelope(self) -> None:
        for number in range(1, SOFTWARE_CAMERA_LIMIT + 1):
            self.register(f"CAM-{number:04}", f"claim-{number}")
        with self.assertRaisesRegex(
            RecorderError, f"up to {SOFTWARE_CAMERA_LIMIT}"
        ):
            self.register("CAM-OVER", "claim-over")

    def test_missing_cameras_are_pending(self) -> None:
        first = self.register("CAM-0001", "first")
        self.register("CAM-0002", "second")
        self.service.claim_camera(camera_id="CAM-0001", claim_secret_hash=first, name="Driveway")
        self.service.set_camera_connected(camera_id="CAM-0001", connected=True)
        self.assertEqual(self.service.status()["cameras"], {"expected": 2, "connected": 1, "pending": 1})

    def test_setup_state_follows_the_short_consumer_path(self) -> None:
        self.assertEqual(
            self.service.status()["setup"],
            {"ready": False, "next": "owner"},
        )
        self.service.setup_owner(
            name="Owner", credential_reference="ref:secure-store:owner"
        )
        self.assertEqual(self.service.status()["setup"]["next"], "storage")
        self.service.plan_storage_initialization(storage_reference="recordings")
        self.service.confirm_storage_initialized(
            storage_reference="recordings", volume_identity="volume-1"
        )
        self.assertEqual(self.service.status()["setup"]["next"], "cameras")
        self.service.register_camera(
            camera_id="CAM-INCLUDED",
            claim_secret_hash=secret_hash("included-camera"),
            identity_reference="identity:CAM-INCLUDED",
            pre_enrolled=True,
        )
        self.assertEqual(
            self.service.status()["setup"],
            {"ready": True, "next": "ready"},
        )

    def test_replace_inherits_name_and_revokes_old_identity(self) -> None:
        old_hash = self.register("CAM-OLD", "old")
        new_hash = self.register("CAM-NEW", "new")
        self.service.claim_camera(camera_id="CAM-OLD", claim_secret_hash=old_hash, name="Driveway")
        self.service.set_camera_recording(camera_id="CAM-OLD", enabled=False)
        self.service.set_camera_alerts(camera_id="CAM-OLD", enabled=False)
        result = self.service.replace_camera(
            camera_name="Driveway", replacement_id="CAM-NEW", claim_secret_hash=new_hash
        )
        self.assertEqual(result["camera"]["name"], "Driveway")
        old, new = self.service.state["cameras"]
        self.assertTrue(old["revoked"])
        self.assertEqual(new["state"], "claimed")
        self.assertIsNone(new["claim_secret_hash"])
        self.assertEqual(self.service.active_camera_ids, frozenset({"CAM-NEW"}))
        self.assertEqual(self.service.recording_camera_ids, frozenset())
        self.assertFalse(new["recording_enabled"])
        self.assertFalse(new["alerts_enabled"])

    def test_replacement_can_be_reserved_when_recorder_is_full(self) -> None:
        hashes: dict[str, str] = {}
        for number in range(1, 5):
            camera_id = f"CAM-{number:04}"
            hashes[camera_id] = self.register(camera_id, f"claim-{number}")
            self.service.claim_camera(camera_id=camera_id, claim_secret_hash=hashes[camera_id], name=f"Camera {number}")
        replacement_hash = secret_hash("replacement")
        self.service.register_camera(
            camera_id="CAM-NEW",
            claim_secret_hash=replacement_hash,
            identity_reference="identity:CAM-NEW",
            replaces="Camera 1",
        )
        self.assertEqual(self.service.status()["cameras"]["expected"], 4)
        self.service.replace_camera(
            camera_name="Camera 1", replacement_id="CAM-NEW", claim_secret_hash=replacement_hash
        )
        status = self.service.status()
        self.assertEqual(status["cameras"]["expected"], 4)

    def test_removing_after_replacement_revokes_the_current_camera(self) -> None:
        old_hash = self.register("CAM-OLD", "old")
        new_hash = self.register("CAM-NEW", "new")
        self.service.claim_camera(camera_id="CAM-OLD", claim_secret_hash=old_hash, name="Driveway")
        self.service.replace_camera(
            camera_name="Driveway", replacement_id="CAM-NEW", claim_secret_hash=new_hash
        )
        self.service.remove_camera(camera_name="Driveway")
        cameras = {camera["id"]: camera for camera in self.service.state["cameras"]}
        self.assertTrue(cameras["CAM-NEW"]["revoked"])

    def test_remove_revokes_and_is_idempotent(self) -> None:
        digest = self.register()
        self.service.claim_camera(camera_id="CAM-0001", claim_secret_hash=digest, name="Driveway")
        self.assertEqual(self.service.remove_camera(camera_name="Driveway")["result"], "removed")
        self.assertEqual(self.service.remove_camera(camera_name="Driveway")["result"], "already_removed")
        self.assertEqual(self.service.status()["cameras"]["expected"], 0)
        with self.assertRaisesRegex(RecorderError, "not found"):
            self.service.set_camera_connected(camera_id="CAM-0001", connected=True)

    def test_recording_settings_validate_and_are_idempotent(self) -> None:
        self.assertEqual(
            self.service.state["recording"], {"mode": "continuous", "days": 7}
        )
        configured = self.service.set_recording(mode="continuous", days=21)
        self.assertEqual(configured["recording"], {"mode": "continuous", "days": 21})
        self.assertEqual(self.service.set_recording(mode="continuous", days=21)["result"], "unchanged")
        for mode, days in (("sometimes", 1), ("events", 0), ("events", True)):
            with self.subTest(mode=mode, days=days), self.assertRaises(RecorderError):
                self.service.set_recording(mode=mode, days=days)

    def test_update_requires_verification_and_only_returns_a_plan(self) -> None:
        with self.assertRaisesRegex(RecorderError, "could not be verified"):
            self.service.plan_update(version="2.0", release_reference="release:2.0", signature_verified=False)
        result = self.service.plan_update(version="2.0", release_reference="release:2.0", signature_verified=True)
        self.assertEqual(result["plan"]["action"], "install_update")
        self.assertEqual(result["plan"]["state"], "planned")
        self.assertEqual(
            self.service.plan_update(version="2.0", release_reference="release:2.0", signature_verified=True)["result"],
            "already_planned",
        )

    def test_retail_messages_do_not_expose_infrastructure_terms(self) -> None:
        results = [
            self.service.setup_owner(name="Owner", credential_reference="ref:secure-store:owner"),
            self.service.plan_storage_initialization(storage_reference="recording-store"),
            self.service.set_recording(mode="events", days=7),
            self.service.plan_update(version="2.0", release_reference="release:2.0", signature_verified=True),
        ]
        rendered = json.dumps(results).lower()
        for forbidden in ("ssh", "linux", "ip address"):
            self.assertNotIn(forbidden, rendered)

    def test_corrupt_or_unsupported_state_fails_closed(self) -> None:
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(RecorderStateError):
            RecorderService(self.state_path)
        self.state_path.write_text('{"schema_version": 999}', encoding="utf-8")
        with self.assertRaises(RecorderStateError):
            RecorderService(self.state_path)

    def test_malformed_storage_state_fails_closed(self) -> None:
        self.service.set_recording(mode="continuous", days=3)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["storage"] = "not-a-storage-object"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(RecorderStateError, "incomplete"):
            RecorderService(self.state_path)


if __name__ == "__main__":
    unittest.main()
