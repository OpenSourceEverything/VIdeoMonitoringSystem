import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from video_monitoring_system.storage_adapter import (
    LinuxRecordingStorageDiscovery,
    RecordingStorageDiscoveryError,
)


class LinuxRecordingStorageDiscoveryTests(unittest.TestCase):
    def inventory(self, mount: Path, *, recording_disk: str = "/dev/sdb"):
        return {
            "blockdevices": [
                {
                    "path": "/dev/sda",
                    "type": "disk",
                    "size": 500_000_000_000,
                    "children": [
                        {
                            "path": "/dev/sda1",
                            "type": "part",
                            "size": 500_000_000_000,
                            "fstype": "ext4",
                            "uuid": "system-uuid",
                            "label": None,
                            "mountpoints": ["/"],
                            "ro": False,
                        }
                    ],
                },
                {
                    "path": recording_disk,
                    "type": "disk",
                    "size": 1_000_000_000_000,
                    "children": [
                        {
                            "path": f"{recording_disk}1",
                            "type": "part",
                            "size": 1_000_000_000_000,
                            "fstype": "ext4",
                            "uuid": "recording-uuid",
                            "label": "VMS_RECORDING",
                            "mountpoints": [str(mount)],
                            "ro": False,
                        }
                    ],
                },
            ]
        }

    @staticmethod
    def runner(value):
        def run(argv, **options):
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(value).encode("utf-8")
            )

        return run

    def test_finds_only_the_mounted_labeled_non_system_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            discovery = LinuxRecordingStorageDiscovery(
                runner=self.runner(self.inventory(mount))
            )

            targets = discovery.list_targets()

            self.assertEqual(len(targets), 1)
            target = targets[0]
            self.assertEqual(target.display_name, "Recording Drive (1000.0 GB)")
            self.assertTrue(target.confirmation.startswith("INITIALIZE STORAGE-"))
            attestation = target.attestation()
            self.assertEqual(attestation.root, mount)
            self.assertEqual(attestation.volume_identity, "linux-uuid:recording-uuid")
            self.assertFalse(attestation.system_device)

    def test_same_system_disk_and_unresolved_root_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            same_disk = self.inventory(mount, recording_disk="/dev/sda")
            same_disk["blockdevices"] = [same_disk["blockdevices"][1]]
            same_disk["blockdevices"][0]["children"].insert(
                0,
                {
                    "path": "/dev/sda1",
                    "type": "part",
                    "size": 500_000_000_000,
                    "fstype": "ext4",
                    "uuid": "system-uuid",
                    "label": None,
                    "mountpoints": ["/"],
                    "ro": False,
                },
            )
            self.assertEqual(
                LinuxRecordingStorageDiscovery(
                    runner=self.runner(same_disk)
                ).list_targets(),
                (),
            )

            unresolved = self.inventory(mount)
            unresolved["blockdevices"][0]["children"][0]["mountpoints"] = [None]
            with self.assertRaises(RecordingStorageDiscoveryError):
                LinuxRecordingStorageDiscovery(
                    runner=self.runner(unresolved)
                ).list_targets()

    def test_wrong_label_filesystem_or_read_only_volume_is_not_offered(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            for field, value in (
                ("label", "OTHER"),
                ("fstype", "exfat"),
                ("ro", True),
                ("mountpoints", []),
            ):
                with self.subTest(field=field):
                    inventory = self.inventory(mount)
                    inventory["blockdevices"][1]["children"][0][field] = value
                    targets = LinuxRecordingStorageDiscovery(
                        runner=self.runner(inventory)
                    ).list_targets()
                    self.assertEqual(targets, ())


if __name__ == "__main__":
    unittest.main()
