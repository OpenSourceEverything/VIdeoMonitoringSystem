import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from video_monitoring_system import linux_storage_recovery as recovery
from video_monitoring_system.storage_adapter import RecordingStorageTarget


UUID = "8af1d774-e571-4668-8a6e-d99837aba13b"
IDENTITY = ("recordings", "linux-uuid:" + UUID)
ROOT = Path("/mnt/VMS_RECORDING")
CURRENT = os.makedev(8, 17)
OLD = os.makedev(8, 1)


class LinuxStorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.state = patch.object(recovery, "RecorderService").start()
        self.state.return_value.initialized_storage_identity = IDENTITY
        self.candidate = patch.object(
            recovery, "_candidate_device", return_value=Path("/dev/sdb1")
        ).start()
        self.device = patch.object(
            recovery, "_device_number", return_value=CURRENT
        ).start()
        self.mount = patch.object(
            recovery, "_mount_state", return_value=(CURRENT, {"rw"})
        ).start()
        self.present = patch.object(
            recovery, "_device_present", return_value=False
        ).start()
        self.command = patch.object(recovery, "_command").start()
        self.validate = patch.object(recovery, "_validate_mounted_storage").start()
        self.addCleanup(patch.stopall)

    def test_healthy_mount_does_not_restart_or_remount(self):
        recovery.ensure_recording_storage()
        self.command.assert_not_called()
        self.validate.assert_called_once_with(IDENTITY, ROOT, CURRENT)

    def test_replugged_same_volume_detaches_only_the_dead_mount(self):
        self.mount.side_effect = [(OLD, {"rw", "shutdown"}), None, (CURRENT, {"rw"})]
        recovery.ensure_recording_storage()
        self.assertEqual(self.command.call_args_list, [
            unittest.mock.call(["systemctl", "stop", "vms-frigate.service"]),
            unittest.mock.call(["systemctl", "stop", "mnt-VMS_RECORDING.mount"]),
            unittest.mock.call(["systemctl", "start", "mnt-VMS_RECORDING.mount"]),
        ])
        self.validate.assert_called_once()

    def test_starting_an_unmounted_paired_drive_checks_the_result(self):
        self.mount.side_effect = [None, (CURRENT, {"rw"})]
        recovery.ensure_recording_storage()
        self.command.assert_called_once_with(["systemctl", "start", "mnt-VMS_RECORDING.mount"])
        self.validate.assert_called_once()

    def test_check_only_never_mutates_mount_or_services(self):
        for mounted in (None, (OLD, {"rw"}), (CURRENT, {"ro"}), (CURRENT, {"rw", "shutdown"})):
            with self.subTest(mounted=mounted):
                self.mount.return_value = mounted
                with self.assertRaises(recovery.StorageRecoveryError):
                    recovery.ensure_recording_storage(check_only=True)
        self.command.assert_not_called()
        self.validate.assert_not_called()

    def test_unrelated_live_mount_is_never_detached(self):
        self.mount.return_value = (OLD, {"rw"})
        self.present.return_value = True
        with self.assertRaises(recovery.StorageRecoveryError):
            recovery.ensure_recording_storage()
        self.command.assert_not_called()

    def test_mount_failure_does_not_probe_or_allow_boot_disk_fallback(self):
        self.mount.side_effect = [None, None]
        with self.assertRaises(recovery.StorageRecoveryError):
            recovery.ensure_recording_storage()
        self.validate.assert_not_called()

    def test_io_failure_is_propagated_instead_of_claiming_storage_ready(self):
        self.validate.side_effect = OSError("I/O error")
        with self.assertRaises(OSError):
            recovery.ensure_recording_storage()

    def test_unpaired_storage_does_not_mount_anything(self):
        self.state.return_value.initialized_storage_identity = None
        with self.assertRaises(recovery.StorageRecoveryError):
            recovery.ensure_recording_storage()
        self.candidate.assert_not_called()
        self.command.assert_not_called()


class RecordingCandidateTests(unittest.TestCase):
    def inventory(self):
        return {"blockdevices": [
            {"path": "/dev/mmcblk0", "type": "disk", "mountpoints": [None], "children": [
                {"path": "/dev/mmcblk0p2", "type": "part", "uuid": "system", "mountpoints": ["/"]},
            ]},
            {"path": "/dev/sdb", "type": "disk", "mountpoints": [None], "children": [
                {"path": "/dev/sdb1", "type": "part", "uuid": UUID, "fstype": "ext4", "label": "VMS_RECORDING", "ro": False, "mountpoints": [None]},
            ]},
        ]}

    def select(self, inventory):
        with patch.object(recovery, "_command", return_value=SimpleNamespace(stdout=json.dumps(inventory))) as command:
            selected = recovery._candidate_device(UUID)
            self.assertIn("--tree", command.call_args.args[0])
            return selected

    def test_finds_the_paired_volume_even_when_its_device_name_changed(self):
        self.assertEqual(self.select(self.inventory()), Path("/dev/sdb1"))

    def test_rejects_missing_duplicate_wrong_label_and_read_only_volumes(self):
        for change in ("missing", "duplicate", "label", "readonly", "mounted-elsewhere", "system-disk"):
            with self.subTest(change=change):
                inventory = self.inventory()
                candidate = inventory["blockdevices"][1]["children"][0]
                if change == "missing":
                    candidate["uuid"] = "other"
                elif change == "duplicate":
                    inventory["blockdevices"][1]["children"].append(dict(candidate))
                elif change == "label":
                    candidate["label"] = "OTHER"
                elif change == "readonly":
                    candidate["ro"] = True
                elif change == "mounted-elsewhere":
                    candidate["mountpoints"] = ["/somewhere-else"]
                else:
                    inventory["blockdevices"][0]["children"].append(candidate)
                    inventory["blockdevices"].pop()
                with self.assertRaises(recovery.StorageRecoveryError):
                    self.select(inventory)


class StorageProbeTests(unittest.TestCase):
    def test_real_io_probe_leaves_no_file_after_success_or_write_failure(self):
        import tempfile

        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = RecordingStorageTarget(
                    "STORAGE-picker-id", "Drive", "INITIALIZE", root, IDENTITY[1]
                )
                with (
                    patch.object(recovery, "LinuxRecordingStorageDiscovery") as discovery,
                    patch.object(recovery, "RecordingStorage"),
                    patch.object(recovery.os, "fsync", side_effect=OSError() if failed else None) as sync,
                ):
                    discovery.return_value.list_targets.return_value = [target]
                    if failed:
                        with self.assertRaises(OSError):
                            recovery._validate_mounted_storage(IDENTITY, root, root.stat().st_dev)
                    else:
                        recovery._validate_mounted_storage(IDENTITY, root, root.stat().st_dev)
                    sync.assert_called_once()
                self.assertEqual(list(root.iterdir()), [])

    def test_mount_disappearing_before_probe_never_creates_a_boot_disk_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = RecordingStorageTarget(
                "STORAGE-picker-id", "Drive", "INITIALIZE", root, IDENTITY[1]
            )
            with (
                patch.object(recovery, "LinuxRecordingStorageDiscovery") as discovery,
                patch.object(recovery, "RecordingStorage"),
            ):
                discovery.return_value.list_targets.return_value = [target]
                with self.assertRaises(recovery.StorageRecoveryError):
                    recovery._validate_mounted_storage(IDENTITY, root, root.stat().st_dev + 1)
            self.assertEqual(list(root.iterdir()), [])
