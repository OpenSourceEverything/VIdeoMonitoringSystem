"""Read-only, platform-neutral discovery of removable whole disks.

The adapters in this module consume structured operating-system inventory and
never interpolate device data into a command.  They intentionally fail closed:
when the operating-system disk cannot be identified, discovered media is
reported as a system disk so :mod:`video_monitoring_system.imaging` refuses to
write it.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from .imaging import DiskTarget


MIN_SYSTEM_CARD_BYTES = 8_000_000_000
MAX_SYSTEM_CARD_BYTES = 256_000_000_000


class DiskDiscoveryError(RuntimeError):
    """Storage media could not be inspected with sufficient confidence."""


class _CompletedProcess(Protocol):
    returncode: int
    stdout: bytes | str


_Runner = Callable[..., _CompletedProcess]

_WINDOWS_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
    "$disks=@(Get-Disk | Select-Object Number,FriendlyName,SerialNumber,"
    "UniqueId,Size,@{Name='BusType';Expression={$_.BusType.ToString()}},"
    "IsBoot,IsSystem,IsOffline);"
    "ConvertTo-Json -Compress -Depth 3 -InputObject $disks"
)
_WINDOWS_ARGV = (
    "powershell.exe",
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    _WINDOWS_SCRIPT,
)
_LINUX_ARGV = (
    "lsblk",
    "--json",
    "--bytes",
    "--paths",
    "--output",
    "NAME,KNAME,PATH,TYPE,SIZE,RM,HOTPLUG,TRAN,MODEL,SERIAL,WWN,"
    "MOUNTPOINTS,PKNAME",
)
_LINUX_MMC_DEVICE = re.compile(r"/dev/mmcblk[0-9]+")
_MACOS_LIST_ARGV = ("diskutil", "list", "-plist")
_MACOS_ROOT_ARGV = ("diskutil", "info", "-plist", "/")
_MACOS_APFS_ARGV = ("diskutil", "apfs", "list", "-plist")
_MACOS_DISK_ID = re.compile(r"disk[0-9]+")
_MACOS_DEVICE_ID = re.compile(r"disk[0-9]+(?:s[0-9]+)*(?:s[0-9]+)?")


@dataclass(frozen=True, slots=True)
class AutoDiskDiscovery:
    """An imaging-compatible discovery object for the current host."""

    platform: str | None = None
    runner: _Runner | None = None

    def list_disks(self) -> Sequence[DiskTarget]:
        return discover_disks(platform=self.platform, runner=self.runner)


def discover_disks(
    *, platform: str | None = None, runner: _Runner | None = None
) -> tuple[DiskTarget, ...]:
    """Return removable whole-disk targets using the current host adapter.

    The function is read-only.  Returned system disks remain visible to the
    safety layer but cannot pass the imaging module's write checks.
    """

    host = (sys.platform if platform is None else platform).casefold()
    command_runner = subprocess.run if runner is None else runner
    try:
        if host in {"win32", "windows", "cygwin", "msys"}:
            targets = _discover_windows(command_runner)
        elif host in {"darwin", "macos"}:
            targets = _discover_macos(command_runner)
        elif host.startswith("linux"):
            targets = _discover_linux(command_runner)
        else:
            raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR)
    except DiskDiscoveryError:
        raise
    except (KeyError, TypeError, ValueError, plistlib.InvalidFileException) as error:
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR) from error
    return _unique_targets(targets)


def is_supported_system_card(target: DiskTarget) -> bool:
    """Return whether a safe removable target has a plausible card capacity."""

    return (
        target.removable
        and not target.system_disk
        and MIN_SYSTEM_CARD_BYTES <= target.size_bytes <= MAX_SYSTEM_CARD_BYTES
    )


_GENERIC_DISCOVERY_ERROR = "Storage devices could not be inspected safely."


def _invoke(runner: _Runner, argv: tuple[str, ...]) -> bytes:
    try:
        completed = runner(
            argv,
            check=True,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR) from error
    if getattr(completed, "returncode", 0) != 0:
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR)
    output = getattr(completed, "stdout", None)
    if isinstance(output, str):
        return output.encode("utf-8")
    if not isinstance(output, bytes):
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR)
    return output


def _try_invoke(runner: _Runner, argv: tuple[str, ...]) -> bytes | None:
    try:
        return _invoke(runner, argv)
    except DiskDiscoveryError:
        return None


def _json_object(payload: bytes) -> Mapping[str, Any] | list[Any]:
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR) from error
    if not isinstance(value, (Mapping, list)):
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR)
    return value


def _plist_object(payload: bytes) -> Mapping[str, Any]:
    try:
        value = plistlib.loads(payload)
    except (ValueError, plistlib.InvalidFileException) as error:
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR) from error
    if not isinstance(value, Mapping):
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR)
    return value


def _discover_windows(runner: _Runner) -> list[DiskTarget]:
    decoded = _json_object(_invoke(runner, _WINDOWS_ARGV))
    records: list[Any]
    if isinstance(decoded, list):
        records = decoded
    else:
        records = [decoded]

    targets: list[DiskTarget] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        number = _nonnegative_int(record.get("Number"))
        size = _positive_int(record.get("Size"))
        bus = _plain_text(record.get("BusType"))
        if number is None or size is None or bus is None:
            continue
        removable = bus.casefold() in {"usb", "sd", "mmc"}
        if not removable or _exact_bool(record.get("IsOffline")) is not False:
            continue
        boot = _exact_bool(record.get("IsBoot"))
        system = _exact_bool(record.get("IsSystem"))
        system_disk = not (boot is False and system is False)
        source = _first_identity(record, "UniqueId", "SerialNumber")
        if source is None:
            source = f"PhysicalDrive{number}|{size}|{bus}"
        name = _plain_text(record.get("FriendlyName"))
        targets.append(
            DiskTarget(
                device=rf"\\.\PhysicalDrive{number}",
                stable_id=_stable_id("MEDIA", "windows", source),
                display_name=_display_name(name, size),
                size_bytes=size,
                removable=True,
                system_disk=system_disk,
            )
        )
    return targets


def _discover_linux(runner: _Runner) -> list[DiskTarget]:
    decoded = _json_object(_invoke(runner, _LINUX_ARGV))
    if not isinstance(decoded, Mapping):
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR)
    roots = decoded.get("blockdevices")
    if not isinstance(roots, list):
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR)

    disks: list[Mapping[str, Any]] = []
    root_seen = False
    root_disks: set[str] = set()

    def visit(node: Any, ancestors: tuple[str, ...]) -> None:
        nonlocal root_seen
        if not isinstance(node, Mapping):
            return
        node_type = _plain_text(node.get("type"))
        path = _linux_device(node)
        disk_ancestors = ancestors
        if node_type == "disk" and path is not None:
            disks.append(node)
            disk_ancestors = ancestors + (path,)
        if _has_root_mount(node.get("mountpoints")):
            root_seen = True
            root_disks.update(disk_ancestors)
        children = node.get("children", ())
        if isinstance(children, list):
            for child in children:
                visit(child, disk_ancestors)

    for root in roots:
        visit(root, ())
    root_resolved = root_seen and bool(root_disks)

    targets: list[DiskTarget] = []
    for record in disks:
        path = _linux_device(record)
        size = _positive_int(record.get("size"))
        removable_flag = _exact_bool(record.get("rm")) is True
        hotplug = _exact_bool(record.get("hotplug")) is True
        transport = _plain_text(record.get("tran"))
        removable = removable_flag or (
            hotplug
            and transport is not None
            and transport.casefold() in {"usb", "mmc", "sd"}
        )
        if not removable and path is not None:
            removable = _linux_udev_proves_sd_card(runner, path)
        if path is None or size is None or not removable:
            continue
        source = _first_identity(record, "wwn", "serial")
        if source is None:
            source = "|".join(
                (
                    path,
                    str(size),
                    _plain_text(record.get("model")) or "",
                    transport or "",
                )
            )
        targets.append(
            DiskTarget(
                device=path,
                stable_id=_stable_id("MEDIA", "linux", source),
                display_name=_display_name(_plain_text(record.get("model")), size),
                size_bytes=size,
                removable=True,
                system_disk=not root_resolved or path in root_disks,
            )
        )
    return targets


def _linux_udev_proves_sd_card(runner: _Runner, path: str) -> bool:
    """Recognize a card in a fixed laptop reader without accepting eMMC."""

    if _LINUX_MMC_DEVICE.fullmatch(path) is None:
        return False
    payload = _try_invoke(
        runner,
        ("udevadm", "info", "--query=property", f"--name={path}"),
    )
    if payload is None:
        return False
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError:
        return False
    properties: dict[str, str] = {}
    for line in lines:
        name, separator, value = line.partition("=")
        if not separator or not name or name in properties:
            return False
        properties[name] = value
    return (
        properties.get("MMC_TYPE") == "SD"
        and properties.get("ID_DRIVE_FLASH_SD") == "1"
    )


def _discover_macos(runner: _Runner) -> list[DiskTarget]:
    inventory = _plist_object(_invoke(runner, _MACOS_LIST_ARGV))
    identifiers = inventory.get("WholeDisks")
    if not isinstance(identifiers, list):
        raise DiskDiscoveryError(_GENERIC_DISCOVERY_ERROR)

    candidates: list[tuple[str, Mapping[str, Any], int, str]] = []
    for raw_identifier in identifiers:
        identifier = _plain_text(raw_identifier)
        if identifier is None or _MACOS_DISK_ID.fullmatch(identifier) is None:
            continue
        payload = _try_invoke(
            runner, ("diskutil", "info", "-plist", f"/dev/{identifier}")
        )
        if payload is None:
            continue
        try:
            info = _plist_object(payload)
        except DiskDiscoveryError:
            continue
        if _exact_bool(info.get("Whole")) is not True:
            continue
        if _plain_text(info.get("DeviceIdentifier")) != identifier:
            continue
        kind = _plain_text(info.get("VirtualOrPhysical"))
        if kind is not None and kind.casefold() != "physical":
            continue
        size = _first_positive_int(info, "TotalSize", "Size", "IOKitSize")
        if size is None or not _macos_removable(info):
            continue
        source = _first_identity(
            info,
            "DiskUUID",
            "MediaUUID",
            "SerialNumber",
            "IORegistryEntryPath",
            "DeviceTreePath",
        )
        if source is None:
            source = "|".join(
                (
                    identifier,
                    str(size),
                    _plain_text(info.get("MediaName")) or "",
                    _plain_text(info.get("BusProtocol")) or "",
                )
            )
        candidates.append((identifier, info, size, source))

    root_payload = _try_invoke(runner, _MACOS_ROOT_ARGV)
    root_info: Mapping[str, Any] | None = None
    if root_payload is not None:
        try:
            root_info = _plist_object(root_payload)
        except DiskDiscoveryError:
            root_info = None
    apfs_info: Mapping[str, Any] | None = None
    if root_info is not None:
        apfs_payload = _try_invoke(runner, _MACOS_APFS_ARGV)
        if apfs_payload is not None:
            try:
                apfs_info = _plist_object(apfs_payload)
            except DiskDiscoveryError:
                apfs_info = None

    candidate_ids = {identifier for identifier, _, _, _ in candidates}
    system_ids = _macos_system_ids(root_info, apfs_info, candidate_ids)
    root_internal = (
        root_info is not None
        and _exact_bool(root_info.get("Internal")) is True
    )
    root_resolved = bool(system_ids)

    targets: list[DiskTarget] = []
    for identifier, info, size, source in candidates:
        explicitly_external = _exact_bool(info.get("Internal")) is False
        if identifier in system_ids:
            system_disk = True
        elif root_resolved:
            system_disk = False
        elif root_internal and explicitly_external:
            system_disk = False
        else:
            system_disk = True
        name = _first_text(info, "MediaName", "IORegistryEntryName")
        targets.append(
            DiskTarget(
                device=f"/dev/{identifier}",
                stable_id=_stable_id("MEDIA", "macos", source),
                display_name=_display_name(name, size),
                size_bytes=size,
                removable=True,
                system_disk=system_disk,
            )
        )
    return targets


def _macos_system_ids(
    root_info: Mapping[str, Any] | None,
    apfs_info: Mapping[str, Any] | None,
    candidate_ids: set[str],
) -> set[str]:
    if root_info is None:
        return set()
    root_references = _disk_references(root_info)
    system_ids = {
        whole
        for reference in root_references
        if (whole := _macos_whole_id(reference)) in candidate_ids
    }

    if apfs_info is not None:
        for container in _container_records(apfs_info):
            container_reference = _first_text(
                container, "ContainerReference", "APFSContainerReference"
            )
            if container_reference not in root_references:
                continue
            physical = container.get("PhysicalStores")
            for reference in _disk_references(physical):
                whole = _macos_whole_id(reference)
                if whole in candidate_ids:
                    system_ids.add(whole)

    # Some diskutil versions include the APFS physical-store list directly in
    # the root-volume record.  The recursive reference extraction above covers
    # that representation without relying on a release-specific plist schema.
    return system_ids


def _container_records(value: Any) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if "ContainerReference" in value or "APFSContainerReference" in value:
            records.append(value)
        for child in value.values():
            records.extend(_container_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(_container_records(child))
    return records


def _disk_references(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, Mapping):
        for child in value.values():
            references.update(_disk_references(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_disk_references(child))
    elif isinstance(value, str):
        candidate = value.strip()
        if _MACOS_DEVICE_ID.fullmatch(candidate):
            references.add(candidate)
    return references


def _macos_whole_id(identifier: str) -> str | None:
    match = _MACOS_DISK_ID.match(identifier)
    return match.group(0) if match is not None else None


def _macos_removable(info: Mapping[str, Any]) -> bool:
    if any(
        _exact_bool(info.get(key)) is True
        for key in ("Removable", "RemovableMedia", "Ejectable")
    ):
        return True
    protocol = _plain_text(info.get("BusProtocol"))
    return (
        _exact_bool(info.get("Internal")) is False
        and protocol is not None
        and protocol.casefold() in {"usb", "sd", "mmc", "secure digital"}
    )


def _linux_device(record: Mapping[str, Any]) -> str | None:
    path = _first_text(record, "path", "name")
    if path is None or not path.startswith("/dev/"):
        return None
    return path


def _has_root_mount(value: Any) -> bool:
    if isinstance(value, str):
        return value == "/"
    if isinstance(value, list):
        return any(item == "/" for item in value)
    return False


def _unique_targets(targets: list[DiskTarget]) -> tuple[DiskTarget, ...]:
    stable_counts: dict[str, int] = {}
    device_counts: dict[str, int] = {}
    for target in targets:
        stable_counts[target.stable_id] = stable_counts.get(target.stable_id, 0) + 1
        device_counts[target.device] = device_counts.get(target.device, 0) + 1
    safe = [
        target
        for target in targets
        if stable_counts[target.stable_id] == 1
        and device_counts[target.device] == 1
    ]
    return tuple(sorted(safe, key=lambda target: target.stable_id))


def _stable_id(label: str, namespace: str, source: str) -> str:
    digest = hashlib.sha256(f"{namespace}\0{source}".encode("utf-8")).hexdigest()
    return f"{label}-{digest[:20].upper()}"


def _display_name(name: str | None, size: int) -> str:
    label = name or "Removable media"
    return f"{label} ({_format_size(size)})"


def _format_size(size: int) -> str:
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    return f"{size / 1_000_000:.1f} MB"


def _plain_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or any(ord(character) == 127 for character in cleaned):
        return None
    return cleaned[:120]


def _first_text(record: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _plain_text(record.get(key))
        if value is not None:
            return value
    return None


def _first_identity(record: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _plain_text(record.get(key))
        if value is None:
            continue
        compact = re.sub(r"[^A-Za-z0-9]", "", value).casefold()
        if compact and compact not in {"0", "unknown", "none", "na", "null"}:
            if set(compact) != {"0"}:
                return value
    return None


def _exact_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _positive_int(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    return None


def _nonnegative_int(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _first_positive_int(record: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _positive_int(record.get(key))
        if value is not None:
            return value
    return None


__all__ = [
    "AutoDiskDiscovery",
    "DiskDiscoveryError",
    "MAX_SYSTEM_CARD_BYTES",
    "MIN_SYSTEM_CARD_BYTES",
    "discover_disks",
    "is_supported_system_card",
]
