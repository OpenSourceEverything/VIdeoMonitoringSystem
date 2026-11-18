import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from video_monitoring_system.application import ApplicationError, VMSApplication
from video_monitoring_system.backup import (
    BackupError,
    create_encrypted_backup,
    read_encrypted_backup,
    restored_files,
)


class BackupTests(unittest.TestCase):
    password = "test-placeholder-owner-password"

    def test_backup_is_encrypted_authenticated_and_hash_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "recorder-state.json"
            state.write_text('{"private":"camera secret"}\n', encoding="utf-8")
            output = root / "recorder-2026-08-30.vms-backup"

            summary = create_encrypted_backup(
                output,
                password=self.password,
                state_files={"recorder-state.json": state},
                recordings=({"id": "recording-1"},),
                clock=lambda: datetime(2026, 8, 30, tzinfo=UTC),
            )

            encoded = output.read_bytes()
            self.assertNotIn(b"camera secret", encoded)
            self.assertNotIn(self.password.encode(), encoded)
            self.assertEqual(summary.recordings, 1)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            document, loaded = read_encrypted_backup(output, password=self.password)
            self.assertEqual(loaded, summary)
            self.assertEqual(
                restored_files(document)["recorder-state.json"], state.read_bytes()
            )

            changed = bytearray(encoded)
            changed[-1] ^= 1
            output.write_bytes(changed)
            with self.assertRaisesRegex(BackupError, "incorrect|changed|invalid"):
                read_encrypted_backup(output, password=self.password)

    def test_wrong_password_and_existing_destination_are_safe_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "recorder-state.json"
            state.write_text("{}", encoding="utf-8")
            output = root / "backup.vms-backup"
            create_encrypted_backup(
                output,
                password=self.password,
                state_files={"recorder-state.json": state},
                recordings=(),
            )

            with self.assertRaisesRegex(BackupError, "incorrect|changed"):
                read_encrypted_backup(
                    output, password="test-placeholder-wrong-password"
                )
            with self.assertRaisesRegex(BackupError, "already exists"):
                create_encrypted_backup(
                    output,
                    password=self.password,
                    state_files={"recorder-state.json": state},
                    recordings=(),
                )

    def test_application_clean_restore_preserves_owner_camera_and_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            source = VMSApplication(root / "source", repository=repository)
            source.create_owner(name="Owner", password=self.password)
            source.recorder.register_camera(
                camera_id="CAM-0000000000000001",
                claim_secret_hash="a" * 64,
                identity_reference="identity:camera-one",
                pre_enrolled=True,
            )
            source.name_camera(
                camera_id="CAM-0000000000000001", name="Front Door"
            )
            source.set_recording(mode="continuous", days=45)
            remote_state = source.data_directory / "remote-access.json"
            remote_state.write_text(
                '{"mode":"owner-gateway","state":"ready"}\n',
                encoding="utf-8",
            )
            os.chmod(remote_state, 0o600)
            backup = root / "portable.vms-backup"

            created = source.create_backup(
                destination=backup, password=self.password
            )
            target = VMSApplication(root / "restored", repository=repository)
            restored = target.restore_backup(source=backup, password=self.password)

            self.assertEqual(restored, created)
            self.assertTrue(target.authenticate_owner(password=self.password))
            self.assertEqual(target.list_cameras()[0]["name"], "Front Door")
            self.assertEqual(target.status()["recording"]["days"], 45)
            self.assertTrue((target.data_directory / "remote-access.json").is_file())
            catalog = root / "restored" / "restored-recording-catalog.json"
            self.assertTrue(catalog.is_file())
            self.assertEqual(os.stat(catalog).st_mode & 0o777, 0o600)

    def test_restore_refuses_to_overwrite_any_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            source = VMSApplication(root / "source", repository=repository)
            source.create_owner(name="Owner", password=self.password)
            backup = root / "portable.vms-backup"
            source.create_backup(destination=backup, password=self.password)

            target = VMSApplication(root / "occupied", repository=repository)
            marker = target.data_directory / "keep-me"
            marker.write_text("present", encoding="utf-8")
            with self.assertRaisesRegex(ApplicationError, "new, empty"):
                target.restore_backup(source=backup, password=self.password)
            self.assertEqual(marker.read_text(encoding="utf-8"), "present")


if __name__ == "__main__":
    unittest.main()
