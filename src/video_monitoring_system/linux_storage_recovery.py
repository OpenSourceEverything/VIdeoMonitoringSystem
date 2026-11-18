"""Fail-closed Linux mount recovery; never format or run filesystem repair."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
from uuid import UUID

from .recorder import RecorderService
from .recording import RecordingStorage
from .storage_adapter import LinuxRecordingStorageDiscovery


class StorageRecoveryError(RuntimeError):
    pass


def _command(arguments, *, missing_ok=False):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=30)
    if result.returncode and not (missing_ok and result.returncode == 1):
        raise StorageRecoveryError("Recording storage operation failed.")
    return result


def _candidate_device(expected_uuid):
    result = _command([
        # JSON is otherwise flat when NAME is not requested; ancestry is
        # required to reject every partition on the system disk.
        "lsblk", "--json", "--tree", "--paths", "--output",
        "PATH,TYPE,FSTYPE,UUID,LABEL,MOUNTPOINTS,RO",
    ])
    nodes = []
    system_disks = set()

    def visit(node, disk=None):
        if node.get("type") == "disk":
            disk = node.get("path")
        mounts = tuple(value for value in node.get("mountpoints", ()) if value)
        if "/" in mounts:
            system_disks.add(disk)
        nodes.append((node, disk, mounts))
        for child in node.get("children", ()):
            visit(child, disk)

    for node in json.loads(result.stdout)["blockdevices"]:
        visit(node)
    matches = [entry for entry in nodes if entry[0].get("uuid") == expected_uuid]
    if len(matches) != 1:
        raise StorageRecoveryError("The paired recording drive is missing or ambiguous.")
    node, disk, mounts = matches[0]
    if (
        not system_disks or disk in system_disks or disk is None
        or node.get("fstype") != "ext4" or node.get("label") != "VMS_RECORDING"
        or node.get("ro") not in (False, 0)
        or any(mount != "/mnt/VMS_RECORDING" for mount in mounts)
    ):
        raise StorageRecoveryError("The paired recording drive is unsafe or in use elsewhere.")
    return Path(node["path"])


def _device_number(source):
    info = source.stat()
    if not stat.S_ISBLK(info.st_mode):
        raise StorageRecoveryError("Recording storage is not a block device.")
    return info.st_rdev


def _mount_state(root):
    result = _command([
        "findmnt", "--json", "--mountpoint", str(root),
        "--output", "TARGET,FSTYPE,OPTIONS,MAJ:MIN",
    ], missing_ok=True)
    if result.returncode == 1:
        return None
    rows = json.loads(result.stdout)["filesystems"]
    if len(rows) != 1 or rows[0]["target"] != str(root) or rows[0]["fstype"] != "ext4":
        raise StorageRecoveryError("The recording mount is unsafe.")
    row = rows[0]
    major, minor = row["maj:min"].split(":")
    return os.makedev(int(major), int(minor)), set(row["options"].split(","))


def _device_present(number):
    return Path(f"/sys/dev/block/{os.major(number)}:{os.minor(number)}").exists()


def _validate_mounted_storage(identity, root, device):
    targets = [
        target.attestation()
        for target in LinuxRecordingStorageDiscovery().list_targets()
        if target.root == root
    ]
    targets = [
        target for target in targets
        if (target.storage_reference, target.volume_identity) == identity
    ]
    if len(targets) != 1:
        raise StorageRecoveryError("The paired recording drive is not mounted safely.")
    RecordingStorage(targets[0]).capacity()
    # Real I/O, not mountpoint presence or a cached capacity value. Never probe
    # until the mounted volume has matched the saved identity and marker.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = ".vms-storage-check-" + secrets.token_hex(12)
    created = False
    try:
        if os.fstat(directory).st_dev != device:
            raise StorageRecoveryError("Recording storage changed during verification.")
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"ok\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        try:
            if created:
                os.unlink(name, dir_fd=directory)
        finally:
            os.close(directory)


def ensure_recording_storage(*, check_only=False):
    root = Path("/mnt/VMS_RECORDING")
    identity = RecorderService(
        "/var/lib/video-monitoring-system/recorder-state.json"
    ).initialized_storage_identity
    if identity is None or not identity[1].startswith("linux-uuid:"):
        raise StorageRecoveryError("Recording storage has not been paired.")
    expected_uuid = identity[1].removeprefix("linux-uuid:")
    if str(UUID(expected_uuid)) != expected_uuid:
        raise StorageRecoveryError("The paired recording identity is invalid.")
    device = _device_number(_candidate_device(expected_uuid))
    mounted = _mount_state(root)
    if mounted is not None and mounted[0] != device:
        # Detach only a dead device's stale mount, never an unrelated live disk.
        if check_only or _device_present(mounted[0]):
            raise StorageRecoveryError("Recording storage needs mount recovery.")
        _command(["systemctl", "stop", "vms-frigate.service"])
        _command(["systemctl", "stop", "mnt-VMS_RECORDING.mount"])
        mounted = _mount_state(root)
        if mounted is not None:
            raise StorageRecoveryError("The stale recording mount could not be detached.")
    if mounted is None:
        if check_only:
            raise StorageRecoveryError("The paired recording drive is not mounted.")
        _command(["systemctl", "start", "mnt-VMS_RECORDING.mount"])
        mounted = _mount_state(root)
    if mounted is None or mounted[0] != device:
        raise StorageRecoveryError("Recording storage did not mount on the paired device.")
    if "rw" not in mounted[1] or "shutdown" in mounted[1]:
        raise StorageRecoveryError("Recording filesystem needs inspection before recording.")
    _validate_mounted_storage(identity, root, device)


def main():
    if os.geteuid() != 0 or sys.argv[1:] not in ([], ["--check-only"]):
        print("Storage recovery requires the appliance service.", file=sys.stderr)
        return 2
    try:
        ensure_recording_storage(check_only=bool(sys.argv[1:]))
    except StorageRecoveryError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # Raw OS/JSON errors may contain private paths or state; keep them local.
        print("Recording storage could not be verified safely.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
