from __future__ import annotations

import json
import plistlib
import subprocess
import unittest
from dataclasses import dataclass
from typing import Any

from video_monitoring_system.disks import (
    AutoDiskDiscovery,
    DiskDiscoveryError,
    discover_disks,
    is_supported_system_card,
)
from video_monitoring_system.imaging import DiskTarget


@dataclass
class Completed:
    stdout: bytes
    returncode: int = 0


class MappingRunner:
    def __init__(self, responses: dict[tuple[str, ...], bytes]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(self, argv: tuple[str, ...], **kwargs: Any) -> Completed:
        self.calls.append((argv, kwargs))
        if argv not in self.responses:
            raise subprocess.CalledProcessError(1, argv)
        return Completed(self.responses[argv])


def encoded_json(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def encoded_plist(value: object) -> bytes:
    return plistlib.dumps(value)


class WindowsDiscoveryTests(unittest.TestCase):
    def test_uses_structured_inventory_and_returns_only_removable_whole_disks(self) -> None:
        records = [
            {
                "Number": 0,
                "FriendlyName": "Internal Storage",
                "SerialNumber": "INT-1",
                "UniqueId": "INTERNAL-1",
                "Size": 1_000_000_000_000,
                "BusType": "NVMe",
                "IsBoot": True,
                "IsSystem": True,
                "IsOffline": False,
            },
            {
                "Number": 3,
                "FriendlyName": "Card Reader",
                "SerialNumber": "CARD-123",
                "UniqueId": "CARD-UNIQUE-123",
                "Size": 32_000_000_000,
                "BusType": "USB",
                "IsBoot": False,
                "IsSystem": False,
                "IsOffline": False,
            },
        ]
        # The response is supplied by operation name because the PowerShell
        # program is deliberately an internal implementation detail.
        class WindowsRunner:
            def __init__(self) -> None:
                self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

            def __call__(self, argv: tuple[str, ...], **kwargs: Any) -> Completed:
                self.calls.append((argv, kwargs))
                return Completed(encoded_json(records))

        windows_runner = WindowsRunner()
        disks = discover_disks(platform="win32", runner=windows_runner)

        self.assertEqual(len(disks), 1)
        disk = disks[0]
        self.assertEqual(disk.device, r"\\.\PhysicalDrive3")
        self.assertEqual(disk.display_name, "Card Reader (32.0 GB)")
        self.assertEqual(disk.size_bytes, 32_000_000_000)
        self.assertTrue(disk.removable)
        self.assertFalse(disk.system_disk)
        self.assertRegex(disk.stable_id, r"^MEDIA-[0-9A-F]{20}$")

        argv, kwargs = windows_runner.calls[0]
        self.assertEqual(argv[0], "powershell.exe")
        self.assertIn("Get-Disk", argv[-1])
        self.assertIn("ConvertTo-Json", argv[-1])
        self.assertEqual(
            kwargs,
            {
                "check": True,
                "shell": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
            },
        )

    def test_missing_system_flags_fail_closed(self) -> None:
        record = {
            "Number": 4,
            "FriendlyName": "Media",
            "SerialNumber": "CARD-124",
            "UniqueId": "CARD-UNIQUE-124",
            "Size": 16_000_000_000,
            "BusType": "SD",
            "IsBoot": False,
            "IsOffline": False,
        }

        def runner(argv: tuple[str, ...], **kwargs: Any) -> Completed:
            return Completed(encoded_json(record))

        disks = discover_disks(platform="windows", runner=runner)
        self.assertEqual(len(disks), 1)
        self.assertTrue(disks[0].system_disk)


class LinuxDiscoveryTests(unittest.TestCase):
    def test_maps_disk_nodes_and_finds_root_through_partition_tree(self) -> None:
        inventory = {
            "blockdevices": [
                {
                    "name": "/dev/nvme0n1",
                    "path": "/dev/nvme0n1",
                    "type": "disk",
                    "size": 512_000_000_000,
                    "rm": False,
                    "hotplug": False,
                    "tran": "nvme",
                    "model": "Internal",
                    "serial": "SYSTEM-1",
                    "wwn": "SYS-WWN",
                    "mountpoints": [None],
                    "children": [
                        {
                            "name": "/dev/nvme0n1p2",
                            "path": "/dev/nvme0n1p2",
                            "type": "part",
                            "size": 500_000_000_000,
                            "mountpoints": ["/"],
                        }
                    ],
                },
                {
                    "name": "/dev/sdb",
                    "path": "/dev/sdb",
                    "type": "disk",
                    "size": 32_000_000_000,
                    "rm": True,
                    "hotplug": True,
                    "tran": "usb",
                    "model": "Card Reader",
                    "serial": "CARD-200",
                    "wwn": None,
                    "mountpoints": [None],
                    "children": [
                        {
                            "name": "/dev/sdb1",
                            "path": "/dev/sdb1",
                            "type": "part",
                            "size": 31_000_000_000,
                            "mountpoints": ["/media/card"],
                        }
                    ],
                },
                {
                    "name": "/dev/loop0",
                    "path": "/dev/loop0",
                    "type": "loop",
                    "size": 1_000_000,
                    "rm": True,
                    "mountpoints": [None],
                },
            ]
        }

        class LinuxRunner:
            def __init__(self) -> None:
                self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

            def __call__(self, argv: tuple[str, ...], **kwargs: Any) -> Completed:
                self.calls.append((argv, kwargs))
                return Completed(encoded_json(inventory))

        runner = LinuxRunner()
        disks = discover_disks(platform="linux", runner=runner)

        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].device, "/dev/sdb")
        self.assertEqual(disks[0].display_name, "Card Reader (32.0 GB)")
        self.assertFalse(disks[0].system_disk)
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv[:3], ("lsblk", "--json", "--bytes"))
        self.assertIn("MOUNTPOINTS", argv[-1])
        self.assertFalse(kwargs["shell"])

    def test_external_root_is_marked_as_system_disk(self) -> None:
        inventory = {
            "blockdevices": [
                {
                    "name": "/dev/sda",
                    "path": "/dev/sda",
                    "type": "disk",
                    "size": 64_000_000_000,
                    "rm": False,
                    "hotplug": True,
                    "tran": "usb",
                    "model": "External",
                    "serial": "BOOT-USB",
                    "wwn": None,
                    "mountpoints": [None],
                    "children": [
                        {
                            "name": "/dev/sda2",
                            "path": "/dev/sda2",
                            "type": "part",
                            "size": 63_000_000_000,
                            "mountpoints": ["/"],
                        }
                    ],
                }
            ]
        }

        def runner(argv: tuple[str, ...], **kwargs: Any) -> Completed:
            return Completed(encoded_json(inventory))

        disks = discover_disks(platform="linux", runner=runner)
        self.assertEqual(len(disks), 1)
        self.assertTrue(disks[0].system_disk)

    def test_unresolved_root_marks_every_candidate_as_system(self) -> None:
        inventory = {
            "blockdevices": [
                {
                    "name": "/dev/sdc",
                    "path": "/dev/sdc",
                    "type": "disk",
                    "size": 32_000_000_000,
                    "rm": True,
                    "hotplug": True,
                    "tran": "usb",
                    "model": "Media",
                    "serial": None,
                    "wwn": None,
                    "mountpoints": [None],
                }
            ]
        }

        def runner(argv: tuple[str, ...], **kwargs: Any) -> Completed:
            return Completed(encoded_json(inventory))

        first = discover_disks(platform="linux-test", runner=runner)
        second = discover_disks(platform="linux-test", runner=runner)
        self.assertEqual(first, second)
        self.assertTrue(first[0].system_disk)

    def test_fixed_reader_requires_udev_proof_of_physical_sd_card(self) -> None:
        inventory = {
            "blockdevices": [
                {
                    "name": "/dev/nvme0n1",
                    "path": "/dev/nvme0n1",
                    "type": "disk",
                    "size": 512_000_000_000,
                    "rm": False,
                    "hotplug": False,
                    "tran": "nvme",
                    "serial": "SYSTEM",
                    "mountpoints": ["/"],
                },
                {
                    "name": "/dev/mmcblk0",
                    "path": "/dev/mmcblk0",
                    "type": "disk",
                    "size": 16_000_000_000,
                    "rm": False,
                    "hotplug": False,
                    "tran": None,
                    "model": "SD Card",
                    "serial": "CARD-SD-1",
                    "mountpoints": [None],
                },
            ]
        }
        udev = b"MMC_TYPE=SD\nID_DRIVE_FLASH_SD=1\n"
        runner = MappingRunner(
            {
                (
                    "lsblk",
                    "--json",
                    "--bytes",
                    "--paths",
                    "--output",
                    "NAME,KNAME,PATH,TYPE,SIZE,RM,HOTPLUG,TRAN,MODEL,SERIAL,WWN,"
                    "MOUNTPOINTS,PKNAME",
                ): encoded_json(inventory),
                (
                    "udevadm",
                    "info",
                    "--query=property",
                    "--name=/dev/mmcblk0",
                ): udev,
            }
        )

        disks = discover_disks(platform="linux", runner=runner)

        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].device, "/dev/mmcblk0")
        self.assertTrue(disks[0].removable)
        self.assertFalse(disks[0].system_disk)
        self.assertEqual(runner.calls[-1][0][0], "udevadm")
        self.assertFalse(runner.calls[-1][1]["shell"])

    def test_fixed_emmc_is_not_offered_as_removable(self) -> None:
        inventory = {
            "blockdevices": [
                {
                    "name": "/dev/mmcblk1",
                    "path": "/dev/mmcblk1",
                    "type": "disk",
                    "size": 64_000_000_000,
                    "rm": False,
                    "hotplug": False,
                    "tran": None,
                    "serial": "EMMC-1",
                    "mountpoints": ["/"],
                }
            ]
        }
        runner = MappingRunner(
            {
                (
                    "lsblk",
                    "--json",
                    "--bytes",
                    "--paths",
                    "--output",
                    "NAME,KNAME,PATH,TYPE,SIZE,RM,HOTPLUG,TRAN,MODEL,SERIAL,WWN,"
                    "MOUNTPOINTS,PKNAME",
                ): encoded_json(inventory),
                (
                    "udevadm",
                    "info",
                    "--query=property",
                    "--name=/dev/mmcblk1",
                ): b"MMC_TYPE=MMC\nID_DRIVE_FLASH_SD=0\n",
            }
        )

        self.assertEqual(discover_disks(platform="linux", runner=runner), ())


class MacOSDiscoveryTests(unittest.TestCase):
    def test_uses_plists_and_maps_only_physical_whole_removable_disks(self) -> None:
        responses = {
            ("diskutil", "list", "-plist"): encoded_plist(
                {"WholeDisks": ["disk0", "disk2", "disk4"]}
            ),
            ("diskutil", "info", "-plist", "/dev/disk0"): encoded_plist(
                {
                    "Whole": True,
                    "DeviceIdentifier": "disk0",
                    "VirtualOrPhysical": "Physical",
                    "TotalSize": 512_000_000_000,
                    "Internal": True,
                    "Removable": False,
                    "Ejectable": False,
                    "BusProtocol": "PCI",
                    "MediaName": "Internal",
                    "SerialNumber": "SYSTEM",
                }
            ),
            ("diskutil", "info", "-plist", "/dev/disk2"): encoded_plist(
                {
                    "Whole": True,
                    "DeviceIdentifier": "disk2",
                    "VirtualOrPhysical": "Physical",
                    "TotalSize": 32_000_000_000,
                    "Internal": False,
                    "Removable": True,
                    "Ejectable": True,
                    "BusProtocol": "USB",
                    "MediaName": "Card Reader",
                    "SerialNumber": "CARD-MAC-1",
                }
            ),
            ("diskutil", "info", "-plist", "/dev/disk4"): encoded_plist(
                {
                    "Whole": True,
                    "DeviceIdentifier": "disk4",
                    "VirtualOrPhysical": "Virtual",
                    "TotalSize": 10_000_000_000,
                    "Internal": False,
                    "Removable": True,
                    "MediaName": "Virtual",
                    "SerialNumber": "VIRTUAL",
                }
            ),
            ("diskutil", "info", "-plist", "/"): encoded_plist(
                {
                    "DeviceIdentifier": "disk3s1s1",
                    "ParentWholeDisk": "disk3",
                    "Internal": True,
                    "APFSPhysicalStores": [
                        {"DeviceIdentifier": "disk0s2"}
                    ],
                }
            ),
            ("diskutil", "apfs", "list", "-plist"): encoded_plist(
                {"Containers": []}
            ),
        }
        runner = MappingRunner(responses)

        disks = discover_disks(platform="darwin", runner=runner)

        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].device, "/dev/disk2")
        self.assertFalse(disks[0].system_disk)
        self.assertEqual(disks[0].display_name, "Card Reader (32.0 GB)")
        for _, kwargs in runner.calls:
            self.assertFalse(kwargs["shell"])

    def test_external_root_is_marked_as_system_disk(self) -> None:
        responses = {
            ("diskutil", "list", "-plist"): encoded_plist(
                {"WholeDisks": ["disk2"]}
            ),
            ("diskutil", "info", "-plist", "/dev/disk2"): encoded_plist(
                {
                    "Whole": True,
                    "DeviceIdentifier": "disk2",
                    "VirtualOrPhysical": "Physical",
                    "TotalSize": 64_000_000_000,
                    "Internal": False,
                    "Removable": True,
                    "BusProtocol": "USB",
                    "MediaName": "External",
                    "DiskUUID": "EXTERNAL-BOOT",
                }
            ),
            ("diskutil", "info", "-plist", "/"): encoded_plist(
                {
                    "DeviceIdentifier": "disk2s2",
                    "ParentWholeDisk": "disk2",
                    "Internal": False,
                }
            ),
            ("diskutil", "apfs", "list", "-plist"): encoded_plist(
                {"Containers": []}
            ),
        }

        disks = discover_disks(
            platform="macos", runner=MappingRunner(responses)
        )
        self.assertEqual(len(disks), 1)
        self.assertTrue(disks[0].system_disk)

    def test_unavailable_root_inventory_fails_closed(self) -> None:
        responses = {
            ("diskutil", "list", "-plist"): encoded_plist(
                {"WholeDisks": ["disk5"]}
            ),
            ("diskutil", "info", "-plist", "/dev/disk5"): encoded_plist(
                {
                    "Whole": True,
                    "DeviceIdentifier": "disk5",
                    "VirtualOrPhysical": "Physical",
                    "TotalSize": 32_000_000_000,
                    "Internal": False,
                    "Removable": True,
                    "MediaName": "Media",
                    "IORegistryEntryPath": "IOService:/reader/card",
                }
            ),
        }
        disks = discover_disks(
            platform="darwin", runner=MappingRunner(responses)
        )
        self.assertEqual(len(disks), 1)
        self.assertTrue(disks[0].system_disk)


class DiscoveryBoundaryTests(unittest.TestCase):
    def test_only_plausible_system_card_sizes_are_supported(self):
        def media(size):
            return DiskTarget(
                device="device",
                stable_id=f"MEDIA-{size}",
                display_name="Media",
                size_bytes=size,
                removable=True,
                system_disk=False,
            )

        self.assertFalse(is_supported_system_card(media(36_864)))
        self.assertTrue(is_supported_system_card(media(32_000_000_000)))
        self.assertFalse(is_supported_system_card(media(2_000_000_000_000)))

    def test_auto_discovery_implements_imaging_protocol(self) -> None:
        payload = encoded_json({"blockdevices": []})

        def runner(argv: tuple[str, ...], **kwargs: Any) -> Completed:
            return Completed(payload)

        discovery = AutoDiskDiscovery(platform="linux", runner=runner)
        self.assertEqual(discovery.list_disks(), ())

    def test_unsupported_platform_and_bad_inventory_have_generic_errors(self) -> None:
        with self.assertRaisesRegex(DiskDiscoveryError, "could not be inspected"):
            discover_disks(platform="unsupported", runner=lambda *a, **k: None)

        def bad_runner(argv: tuple[str, ...], **kwargs: Any) -> Completed:
            return Completed(b"not structured data")

        with self.assertRaisesRegex(DiskDiscoveryError, "could not be inspected"):
            discover_disks(platform="linux", runner=bad_runner)

    def test_duplicate_device_identities_are_not_offered(self) -> None:
        inventory = {
            "blockdevices": [
                {
                    "name": f"/dev/sd{letter}",
                    "path": f"/dev/sd{letter}",
                    "type": "disk",
                    "size": 32_000_000_000,
                    "rm": True,
                    "hotplug": True,
                    "tran": "usb",
                    "model": "Reader",
                    "serial": "DUPLICATE",
                    "wwn": None,
                    "mountpoints": [None],
                    "children": [
                        {
                            "name": f"/dev/sd{letter}1",
                            "type": "part",
                            "mountpoints": ["/" if letter == "a" else None],
                        }
                    ],
                }
                for letter in ("a", "b")
            ]
        }

        def runner(argv: tuple[str, ...], **kwargs: Any) -> Completed:
            return Completed(encoded_json(inventory))

        self.assertEqual(discover_disks(platform="linux", runner=runner), ())


if __name__ == "__main__":
    unittest.main()
