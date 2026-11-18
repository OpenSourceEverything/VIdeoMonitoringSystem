"""Read-only discovery of an appliance-prepared Linux recording volume."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .recording import RecordingVolumeAttestation


class RecordingStorageDiscoveryError(RuntimeError):
    """Recording storage could not be identified with sufficient confidence."""


class _CompletedProcess(Protocol):
    returncode: int
    stdout: bytes | str


class RecordingStorageDiscovery(Protocol):
    def list_targets(self) -> Sequence["RecordingStorageTarget"]: ...


_Runner = Callable[..., _CompletedProcess]
_LSBLK = (
    "lsblk",
    "--json",
    "--bytes",
    "--paths",
    "--output",
    "NAME,PATH,PKNAME,TYPE,SIZE,FSTYPE,UUID,LABEL,MOUNTPOINTS,RO",
)
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_GENERIC_ERROR = "Recording storage could not be inspected safely."


@dataclass(frozen=True, slots=True)
class RecordingStorageTarget:
    stable_id: str
    display_name: str
    confirmation: str
    root: Path
    volume_identity: str

    @property
    def adoption_confirmation(self) -> str:
        return f"ADOPT {self.stable_id}"

    def attestation(self) -> RecordingVolumeAttestation:
        return RecordingVolumeAttestation(
            root=self.root,
            storage_reference="recordings",
            volume_identity=self.volume_identity,
            mounted=True,
            system_device=False,
        )


@dataclass(frozen=True, slots=True)
class LinuxRecordingStorageDiscovery:
    """Find one mounted ext4 volume prepared with the product label."""

    runner: _Runner = subprocess.run

    def list_targets(self) -> tuple[RecordingStorageTarget, ...]:
        inventory = _inventory(self.runner)
        roots = inventory.get("blockdevices")
        if not isinstance(roots, list):
            raise RecordingStorageDiscoveryError(_GENERIC_ERROR)

        candidates: list[tuple[RecordingStorageTarget, str]] = []
        root_disks: set[str] = set()

        def visit(node: object, disk: str | None) -> None:
            if not isinstance(node, Mapping):
                return
            path = _text(node.get("path"))
            node_type = _text(node.get("type"))
            current_disk = path if node_type == "disk" else disk
            mounts = _mounts(node.get("mountpoints"))
            if "/" in mounts and current_disk is not None:
                root_disks.add(current_disk)
            target = _target(node, current_disk, mounts)
            if target is not None:
                candidates.append(target)
            children = node.get("children")
            if isinstance(children, list):
                for child in children:
                    visit(child, current_disk)

        for root in roots:
            visit(root, None)
        if not root_disks:
            raise RecordingStorageDiscoveryError(_GENERIC_ERROR)

        safe = [
            target for target, disk in candidates if disk not in root_disks
        ]
        if len({item.stable_id for item in safe}) != len(safe):
            raise RecordingStorageDiscoveryError(_GENERIC_ERROR)
        return tuple(sorted(safe, key=lambda item: item.stable_id))


def _target(
    node: Mapping[str, object], disk: str | None, mounts: tuple[str, ...]
) -> tuple[RecordingStorageTarget, str] | None:
    if disk is None or _text(node.get("label")) != "VMS_RECORDING":
        return None
    if _text(node.get("fstype")) != "ext4" or _exact_bool(node.get("ro")) is not False:
        return None
    uuid = _text(node.get("uuid"))
    size = node.get("size")
    if (
        uuid is None
        or _IDENTITY.fullmatch(uuid) is None
        or type(size) is not int
        or size <= 0
        or len(mounts) != 1
    ):
        return None
    root = Path(mounts[0])
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        return None
    digest = hashlib.sha256(uuid.encode("utf-8")).hexdigest()[:20].upper()
    stable_id = f"STORAGE-{digest}"
    target = RecordingStorageTarget(
        stable_id=stable_id,
        display_name=f"Recording Drive ({_size(size)})",
        confirmation=f"INITIALIZE {stable_id}",
        root=root,
        volume_identity=f"linux-uuid:{uuid}",
    )
    return target, disk


def _inventory(runner: _Runner) -> Mapping[str, object]:
    try:
        result = runner(
            _LSBLK,
            check=True,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RecordingStorageDiscoveryError(_GENERIC_ERROR) from error
    output = getattr(result, "stdout", None)
    if isinstance(output, bytes):
        try:
            output = output.decode("utf-8")
        except UnicodeError as error:
            raise RecordingStorageDiscoveryError(_GENERIC_ERROR) from error
    if getattr(result, "returncode", 0) != 0 or not isinstance(output, str):
        raise RecordingStorageDiscoveryError(_GENERIC_ERROR)
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise RecordingStorageDiscoveryError(_GENERIC_ERROR) from error
    if not isinstance(value, Mapping):
        raise RecordingStorageDiscoveryError(_GENERIC_ERROR)
    return value


def _mounts(value: object) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    return value


def _exact_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _size(value: int) -> str:
    return f"{value / 1_000_000_000:.1f} GB"


__all__ = [
    "LinuxRecordingStorageDiscovery",
    "RecordingStorageDiscovery",
    "RecordingStorageDiscoveryError",
    "RecordingStorageTarget",
]
