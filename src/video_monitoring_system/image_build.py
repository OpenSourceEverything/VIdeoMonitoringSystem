"""Versioned Linux build orchestration for the pilot camera image.

The customer-facing provisioner writes a released image; it never runs this
module.  This developer tool pins pi-gen, assembles a public root-filesystem
payload, and emits an unsigned development release manifest next to the built
artifact.  Product promotion signs a separately verified manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Mapping, Sequence

from .device.pilot import (
    PILOT_DIAGNOSTICS_SCRIPT,
    PILOT_JOURNALD_CONFIG,
    PILOT_SSH_CONFIG,
    PILOT_USB_RECOVERY_SCRIPT,
    PILOT_USB_RECOVERY_SERVICE,
)
from .device.service import (
    CAMERA_SERVICE,
    NETWORK_RECOVERY_SERVICE,
    NETWORK_RECOVERY_TIMER,
)
from .doctor import run_doctor
from .provisioning import PREINSTALLED_CAMERA_IMAGE_REFERENCE


_REQUIRED_PACKAGES = frozenset(
    {
        "avahi-daemon",
        "cloud-init",
        "iputils-ping",
        "network-manager",
        "openssh-server",
        "python3",
        "python3-picamera2",
        "raspi-config",
        "sudo",
    }
)
_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_PI_CAMERA_IMAGE = Path("platforms/raspberry-pi/camera-image/pilot")
_PI_GEN_GPGV_PATCH = _PI_CAMERA_IMAGE / "pi-gen-gpgv.patch"
_PI_GEN_BINFMT_PATCH = _PI_CAMERA_IMAGE / "pi-gen-binfmt.patch"
_PI_GEN_ARM64_BINFMT_PATCH = Path(
    "platforms/raspberry-pi/recorder-image/pilot/pi-gen-binfmt.patch"
)
_PI_GEN_FIRST_BOOT_PATCH = _PI_CAMERA_IMAGE / "pi-gen-first-boot-state.patch"
_PATCHED_PI_GEN_TRACKED_FILES = frozenset(
    {"Dockerfile", "build-docker.sh", "export-image/05-finalise/01-run.sh"}
)
_PATCHED_PI_GEN_DOCKERFILE_SHA256 = (
    "a6da36dd9b81c89384a025f1afc778fe3ae40fb851274b062b1c857c7c367be6"
)
_PINNED_PI_GEN_BUILD_DOCKER_SHA256 = (
    "58b8826763b2daafa8461b11b2ddbca33c897475ce662c94f7da9dc64cb32514"
)
_PATCHED_PI_GEN_BUILD_DOCKER_SHA256 = (
    "7a20c84ab5b65ee2e958f16c850b896e619dc560965764c20a31b2e84f921c9f"
)
_PINNED_PI_GEN_ARM64_BUILD_DOCKER_SHA256 = (
    "1cefd268d0f88c45c6e04e9d202adbf3bcbe6de7e0bcd550cd5c0238603eef49"
)
_PATCHED_PI_GEN_ARM64_BUILD_DOCKER_SHA256 = (
    "25a5b5fba368dff90d1b78894401bb60f62241a2f7c56d7d05b28ee09eafa0b2"
)
_PINNED_PI_GEN_FINALISE_SHA256 = (
    "f55a9fb128a10c07617ff0bf7d4d95eafd227e1b762274d3eada71573823ad65"
)
_PATCHED_PI_GEN_FINALISE_SHA256 = (
    "8a3c11c7e2b144ce0c661a47b8a43d7e0422d1986120b335c87a2563e138fc7d"
)
_CLOUD_INIT_STAGE_UNITS = (
    "cloud-init-main.service",
    "cloud-init-local.service",
    "cloud-init-network.service",
    "cloud-config.service",
    "cloud-final.service",
)
_CLOUD_INIT_JOURNAL_ONLY_DROP_IN_NAME = "99-vms-journal-only.conf"
_CLOUD_INIT_JOURNAL_ONLY_DROP_IN = """[Service]
StandardOutput=journal
StandardError=journal
"""
_VMS_PROVISION_TRANSACTION_SECONDS = 600
_VMS_PROVISION_TIMEOUT_SECONDS = 620
_VMS_PROVISION_SCRIPT = """#!/bin/sh
set -eu
umask 077

ready=/var/lib/vms/first-boot.json
first_boot=/usr/local/libexec/vms-first-boot
first_boot_unit=/etc/systemd/system/vms-first-boot.service

is_ready() {
    test -f "$ready" &&
        test ! -L "$ready" &&
        test "$(cat "$ready")" = '{"schema_version":1,"state":"ready"}'
}

if is_ready; then
    exit 0
fi

test "$(systemctl show --property=ActiveState --value cloud-init-network.service)" = active
test "$(systemctl show --property=Result --value cloud-init-network.service)" = success
test ! -L "$first_boot"
test ! -L "$first_boot_unit"
test -x "$first_boot"
test -s "$first_boot_unit"
test "$(stat -c '%U:%G:%a' "$first_boot")" = root:root:700
test "$(stat -c '%U:%G:%a' "$first_boot_unit")" = root:root:644

systemctl daemon-reload
systemctl start vms-first-boot.service
if ! is_ready; then
    echo "First-boot service completed without the exact ready marker" >&2
    exit 1
fi
"""
_VMS_PROVISION_SERVICE = f"""[Unit]
Description=Dispatch VMS per-device provisioning
Requires=cloud-init-network.service
After=cloud-init-network.service NetworkManager.service
Wants=NetworkManager.service
JobTimeoutSec=15min
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/vms-provision
TimeoutStartSec={_VMS_PROVISION_TIMEOUT_SECONDS}s
Restart=on-failure
RestartSec=15s
RestartSteps=4
RestartMaxDelaySec=5min
RemainAfterExit=true
StandardOutput=journal
StandardError=journal
SyslogIdentifier=vms-provision

[Install]
WantedBy=multi-user.target
"""
_VMS_PROVISION_CLEANUP_SCRIPT = r"""#!/bin/sh
set -eu
umask 077

state_dir=/var/lib/vms
ready="$state_dir/first-boot.json"
marker="$state_dir/cloud-init-cleanup.json"

if test -e "$marker" || test -L "$marker"; then
    if test -f "$marker" && test ! -L "$marker" && \
        test "$(cat "$marker")" = '{"schema_version":1,"state":"complete"}'
    then
        systemctl disable vms-provision-cleanup.service >/dev/null 2>&1 || true
        exit 0
    fi
    echo "Cloud-init cleanup marker is invalid" >&2
    exit 1
fi

test "$(systemctl show --property=ActiveState --value cloud-final.service)" = active
test "$(systemctl show --property=Result --value cloud-final.service)" = success
test -f "$ready"
test ! -L "$ready"
test "$(cat "$ready")" = '{"schema_version":1,"state":"ready"}'

for path in \
    /boot/firmware/user-data \
    /boot/firmware/network-config \
    /boot/firmware/meta-data \
    /boot/firmware/vendor-data \
    /boot/user-data \
    /boot/network-config \
    /boot/meta-data \
    /boot/vendor-data \
    /var/lib/cloud/data/instance-data-sensitive.json \
    /run/cloud-init/instance-data-sensitive.json
do
    rm -f -- "$path"
done

for directory in /var/lib/cloud/instances /var/lib/cloud/seed; do
    if test -d "$directory"; then
        find "$directory" -xdev \
            \( -type f -o -type l \) \
            \( -name 'user-data*' -o -name 'vendor-data*' \
               -o -name 'network-config*' -o -name 'cloud-config.txt' \
               -o -name 'instance-data-sensitive.json' -o -name 'obj.pkl' \) \
            -delete
    fi
done

if test -d /var/log; then
    find /var/log -xdev -maxdepth 1 \
        \( -type f -o -type l \) -name 'cloud-init*.log*' -delete
fi

for path in \
    /boot/firmware/user-data \
    /boot/firmware/network-config \
    /boot/firmware/meta-data \
    /boot/firmware/vendor-data \
    /boot/user-data \
    /boot/network-config \
    /boot/meta-data \
    /boot/vendor-data \
    /var/lib/cloud/data/instance-data-sensitive.json \
    /run/cloud-init/instance-data-sensitive.json
do
    if test -e "$path" || test -L "$path"; then
        echo "Cloud-init secret remains at $path" >&2
        exit 1
    fi
done

for directory in /var/lib/cloud/instances /var/lib/cloud/seed; do
    if test -d "$directory" && find "$directory" -xdev \
        \( -type f -o -type l \) \
        \( -name 'user-data*' -o -name 'vendor-data*' \
           -o -name 'network-config*' -o -name 'cloud-config.txt' \
           -o -name 'instance-data-sensitive.json' -o -name 'obj.pkl' \) \
        -print -quit | grep -q .
    then
        echo "Cloud-init secret-bearing instance data remains" >&2
        exit 1
    fi
done
if test -d /var/log && find /var/log -xdev -maxdepth 1 \
    \( -type f -o -type l \) -name 'cloud-init*.log*' -print -quit | grep -q .
then
    echo "Cloud-init file logs remain" >&2
    exit 1
fi

install -d -m 0700 "$state_dir"
temporary="$marker.tmp"
rm -f "$temporary"
printf '%s\n' '{"schema_version":1,"state":"complete"}' > "$temporary"
chmod 0600 "$temporary"
sync
mv -f "$temporary" "$marker"
sync
systemctl disable vms-provision-cleanup.service >/dev/null 2>&1 || true
"""
_VMS_PROVISION_CLEANUP_SERVICE = """[Unit]
Description=Remove VMS cloud-init provisioning secrets
Requires=cloud-final.service
Wants=vms-provision.service
After=cloud-final.service vms-provision.service
StartLimitIntervalSec=0
JobTimeoutSec=20min

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/vms-provision-cleanup
TimeoutStartSec=60s
Restart=on-failure
RestartSec=15s
RestartSteps=4
RestartMaxDelaySec=5min
RemainAfterExit=true
StandardOutput=journal
StandardError=journal
SyslogIdentifier=vms-provision-cleanup

[Install]
WantedBy=cloud-init.target
"""


class CameraImageBuildError(RuntimeError):
    """The camera image build cannot safely continue."""


@dataclass(frozen=True, slots=True)
class RawImagePartition:
    index: int
    type: str
    start_sector: int
    sector_count: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "index": self.index,
            "type": self.type,
            "start_sector": self.start_sector,
            "sector_count": self.sector_count,
            "byte_offset": self.start_sector * 512,
            "byte_count": self.sector_count * 512,
        }


@dataclass(frozen=True, slots=True)
class RawImageInspection:
    bytes: int
    sha256: str
    partitions: tuple[RawImagePartition, ...]


@dataclass(frozen=True, slots=True)
class CameraImageBuildManifest:
    schema_version: int
    profile: str
    image_reference: str
    release_version: str
    distribution_release: str
    architecture: str
    bits: int
    supported_boards: tuple[str, ...]
    builder_repository: str
    builder_commit: str
    packages: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CameraImageBuildManifest:
        expected = {
            "schema_version",
            "profile",
            "image_reference",
            "release_version",
            "distribution_release",
            "architecture",
            "bits",
            "supported_boards",
            "builder",
            "packages",
        }
        if set(value) != expected:
            raise ValueError("camera image build manifest fields are invalid")
        builder = value.get("builder")
        if not isinstance(builder, Mapping) or set(builder) != {"repository", "commit"}:
            raise ValueError("builder must contain exactly repository and commit")
        boards = value.get("supported_boards")
        packages = value.get("packages")
        if not isinstance(boards, list) or not all(
            isinstance(item, str) for item in boards
        ):
            raise ValueError("supported_boards must be an array of strings")
        if not isinstance(packages, list) or not all(
            isinstance(item, str) for item in packages
        ):
            raise ValueError("packages must be an array of strings")
        manifest = cls(
            schema_version=value.get("schema_version"),
            profile=value.get("profile"),
            image_reference=value.get("image_reference"),
            release_version=value.get("release_version"),
            distribution_release=value.get("distribution_release"),
            architecture=value.get("architecture"),
            bits=value.get("bits"),
            supported_boards=tuple(boards),
            builder_repository=builder.get("repository"),
            builder_commit=builder.get("commit"),
            packages=tuple(packages),
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported camera image build schema_version")
        if self.profile != "pilot-debug":
            raise ValueError("this builder accepts only the pilot-debug profile")
        if self.image_reference != PREINSTALLED_CAMERA_IMAGE_REFERENCE:
            raise ValueError("preinstalled camera image reference is inconsistent")
        for label, value in (
            ("release_version", self.release_version),
            ("distribution_release", self.distribution_release),
            ("architecture", self.architecture),
            ("builder repository", self.builder_repository),
        ):
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise ValueError(f"{label} must be non-empty text")
        if self.distribution_release != "trixie":
            raise ValueError("pilot camera image must use the tested trixie release")
        if self.architecture != "armhf" or self.bits != 32:
            raise ValueError("Zero W compatibility requires the 32-bit armhf image")
        if self.supported_boards != ("zero-w", "zero-2-w"):
            raise ValueError("pilot image must declare both tested Zero boards")
        if _COMMIT.fullmatch(self.builder_commit) is None:
            raise ValueError("pi-gen must be pinned to a full commit hash")
        if len(set(self.packages)) != len(self.packages):
            raise ValueError("packages must not contain duplicates")
        if any(_PACKAGE_NAME.fullmatch(package) is None for package in self.packages):
            raise ValueError("packages contain an invalid Debian package name")
        missing = _REQUIRED_PACKAGES - set(self.packages)
        if missing:
            raise ValueError(
                "camera image is missing required packages: "
                + ", ".join(sorted(missing))
            )


def load_build_manifest(path: str | os.PathLike[str]) -> CameraImageBuildManifest:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CameraImageBuildError(
            "camera image build manifest is unreadable"
        ) from error
    if not isinstance(value, Mapping):
        raise CameraImageBuildError("camera image build manifest must be an object")
    try:
        return CameraImageBuildManifest.from_dict(value)
    except (TypeError, ValueError) as error:
        raise CameraImageBuildError(str(error)) from error


def verify_rootfs_first_boot_state(rootfs: Path) -> None:
    """Verify that an offline generic root filesystem has no prior boot state."""

    rootfs = rootfs.resolve()
    cloud_state = rootfs / "var" / "lib" / "cloud"
    if cloud_state.is_symlink() or not cloud_state.is_dir():
        raise CameraImageBuildError(
            "camera image cloud-init state directory is missing or unsafe"
        )
    try:
        next(cloud_state.iterdir())
    except StopIteration:
        pass
    except OSError as error:
        raise CameraImageBuildError(
            "camera image cloud-init state cannot be inspected"
        ) from error
    else:
        raise CameraImageBuildError("camera image contains build-time cloud-init state")

    machine_id = rootfs / "etc" / "machine-id"
    try:
        machine_id_value = machine_id.read_bytes()
    except OSError as error:
        raise CameraImageBuildError("camera image machine-id is unreadable") from error
    if machine_id.is_symlink() or machine_id_value != b"uninitialized\n":
        raise CameraImageBuildError("camera image machine-id was not reset")

    dbus_machine_id = rootfs / "var" / "lib" / "dbus" / "machine-id"
    if os.path.lexists(dbus_machine_id):
        raise CameraImageBuildError("camera image D-Bus machine-id was not reset")


def _confined_rootfs_file(
    rootfs: Path,
    relative: str,
    *,
    label: str,
    allow_symlink: bool = False,
) -> Path:
    candidate = rootfs.joinpath(*relative.split("/"))
    if candidate.is_symlink() and not allow_symlink:
        raise CameraImageBuildError(f"{label} must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(rootfs)
    except (OSError, RuntimeError, ValueError) as error:
        raise CameraImageBuildError(f"{label} is missing or unsafe") from error
    if not resolved.is_file():
        raise CameraImageBuildError(f"{label} is not a regular file")
    return resolved


def _unit_drop_in_names(unit: str) -> tuple[str, ...]:
    stem, separator, suffix = unit.rpartition(".")
    if not separator:
        return (f"{unit}.d",)
    names = [f"{suffix}.d"]
    dash_positions = [index for index, character in enumerate(stem) if character == "-"]
    names.extend(f"{stem[: index + 1]}.{suffix}.d" for index in dash_positions)
    names.append(f"{unit}.d")
    return tuple(names)


def _effective_unit_assignments(
    rootfs: Path, unit: str
) -> dict[tuple[str, str], list[str]]:
    """Read the effective offline unit fragments needed by the image contract."""

    unit_roots = (
        "lib/systemd/system",
        "usr/lib/systemd/system",
        "usr/local/lib/systemd/system",
        "run/systemd/system",
        "etc/systemd/system",
    )
    main_fragment: Path | None = None
    for unit_root in reversed(unit_roots):
        relative = f"{unit_root}/{unit}"
        candidate = rootfs.joinpath(*relative.split("/"))
        if candidate.exists() or candidate.is_symlink():
            main_fragment = _confined_rootfs_file(
                rootfs,
                relative,
                label=f"effective {unit} fragment",
                allow_symlink=True,
            )
            break
    if main_fragment is None:
        raise CameraImageBuildError(f"effective systemd unit {unit} is missing")

    # Systemd reads drop-ins lexically. Equally named files in a more specific
    # unit directory or a higher-priority unit root replace the lower-priority
    # file before that lexical merge.
    selected_drop_ins: dict[str, Path | None] = {}
    for drop_in_name in _unit_drop_in_names(unit):
        for unit_root in unit_roots:
            directory = rootfs.joinpath(*f"{unit_root}/{drop_in_name}".split("/"))
            if not directory.is_dir():
                continue
            for candidate in sorted(directory.glob("*.conf")):
                if candidate.is_symlink() and os.readlink(candidate) == "/dev/null":
                    selected_drop_ins[candidate.name] = None
                    continue
                relative = candidate.relative_to(rootfs).as_posix()
                selected_drop_ins[candidate.name] = _confined_rootfs_file(
                    rootfs,
                    relative,
                    label=f"effective {unit} drop-in",
                    allow_symlink=True,
                )

    fragments = [main_fragment]
    fragments.extend(
        path for _name, path in sorted(selected_drop_ins.items()) if path is not None
    )
    assignments: dict[tuple[str, str], list[str]] = {}
    for fragment in fragments:
        try:
            content = fragment.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise CameraImageBuildError(
                f"effective systemd unit {unit} is unreadable"
            ) from error
        section = ""
        logical_line = ""
        for raw_line in content.splitlines():
            logical_line += raw_line
            if logical_line.endswith("\\"):
                logical_line = logical_line[:-1]
                continue
            line = logical_line.strip()
            logical_line = ""
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            key, separator, value = line.partition("=")
            if separator:
                assignments.setdefault((section, key.strip()), []).append(value.strip())
    return assignments


def _effective_scalar(
    assignments: Mapping[tuple[str, str], Sequence[str]],
    section: str,
    key: str,
) -> str | None:
    values = assignments.get((section, key), ())
    return values[-1] if values else None


def _effective_unit_list(
    assignments: Mapping[tuple[str, str], Sequence[str]],
    section: str,
    key: str,
) -> tuple[str, ...]:
    effective: list[str] = []
    for value in assignments.get((section, key), ()):
        if not value:
            effective.clear()
        else:
            effective.extend(value.split())
    return tuple(effective)


def _verify_enabled_unit(rootfs: Path, target: str, unit: str) -> None:
    link = rootfs / "etc" / "systemd" / "system" / f"{target}.wants" / unit
    if not link.is_symlink():
        raise CameraImageBuildError(f"{unit} is not enabled under {target}")
    raw_target = os.readlink(link)
    if os.path.isabs(raw_target):
        destination = rootfs.joinpath(
            *raw_target.lstrip("/\\").replace("\\", "/").split("/")
        )
    else:
        destination = link.parent / raw_target
    try:
        resolved = destination.resolve(strict=True)
        resolved.relative_to(rootfs)
    except (OSError, RuntimeError, ValueError) as error:
        raise CameraImageBuildError(
            f"{unit} enablement target is missing or unsafe"
        ) from error
    expected = (rootfs / "etc" / "systemd" / "system" / unit).resolve()
    if resolved != expected:
        raise CameraImageBuildError(f"{unit} enablement target is inconsistent")


def verify_rootfs_runtime_contract(rootfs: Path) -> None:
    """Verify effective systemd and dispatcher state without booting the image."""

    rootfs = rootfs.resolve()
    if not rootfs.is_dir() or rootfs.is_symlink():
        raise CameraImageBuildError("camera image root filesystem is missing or unsafe")

    owned_files: dict[str, tuple[str, int]] = {
        "opt/vms/runtime_metrics.py": (
            files("video_monitoring_system.device")
            .joinpath("runtime_metrics.py").read_text(encoding="utf-8"), 0o755,
        ),
        "opt/vms/journal_reader.py": (
            files("video_monitoring_system.device")
            .joinpath("journal_reader.py").read_text(encoding="utf-8"), 0o755,
        ),
        "opt/vms/network_recovery.py": (
            files("video_monitoring_system.device")
            .joinpath("network_recovery.py")
            .read_text(encoding="utf-8"),
            0o755,
        ),
        "etc/systemd/system/vms-network-recovery.service": (
            NETWORK_RECOVERY_SERVICE,
            0o644,
        ),
        "etc/systemd/system/vms-network-recovery.timer": (
            NETWORK_RECOVERY_TIMER,
            0o644,
        ),
        "usr/local/libexec/vms-provision": (_VMS_PROVISION_SCRIPT, 0o755),
        "etc/systemd/system/vms-provision.service": (
            _VMS_PROVISION_SERVICE,
            0o644,
        ),
        "usr/local/libexec/vms-provision-cleanup": (
            _VMS_PROVISION_CLEANUP_SCRIPT,
            0o700,
        ),
        "etc/systemd/system/vms-provision-cleanup.service": (
            _VMS_PROVISION_CLEANUP_SERVICE,
            0o644,
        ),
    }
    for unit in _CLOUD_INIT_STAGE_UNITS:
        owned_files[
            f"etc/systemd/system/{unit}.d/{_CLOUD_INIT_JOURNAL_ONLY_DROP_IN_NAME}"
        ] = (_CLOUD_INIT_JOURNAL_ONLY_DROP_IN, 0o644)

    for relative, (expected_content, expected_mode) in owned_files.items():
        path = _confined_rootfs_file(
            rootfs,
            relative,
            label=f"image-owned /{relative}",
        )
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise CameraImageBuildError(
                f"image-owned /{relative} is unreadable"
            ) from error
        if content != expected_content:
            raise CameraImageBuildError(
                f"image-owned /{relative} does not match its build contract"
            )
        # Windows-hosted source tests cannot represent Unix execute modes on
        # NTFS. Production image verification always runs on the Linux builder.
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != expected_mode:
            raise CameraImageBuildError(f"image-owned /{relative} has an unsafe mode")

    for relative in (
        "usr/local/libexec/vms-first-boot",
        "etc/systemd/system/vms-first-boot.service",
    ):
        path = rootfs.joinpath(*relative.split("/"))
        if os.path.lexists(path):
            raise CameraImageBuildError(
                "generic camera image contains a per-device first-boot placeholder"
            )

    for unit in _CLOUD_INIT_STAGE_UNITS:
        assignments = _effective_unit_assignments(rootfs, unit)
        standard_output = _effective_scalar(assignments, "Service", "StandardOutput")
        standard_error = _effective_scalar(assignments, "Service", "StandardError")
        if standard_output != "journal" or standard_error != "journal":
            raise CameraImageBuildError(f"effective {unit} output is not journal-only")
        if "console" in standard_output or "console" in standard_error:
            raise CameraImageBuildError(f"effective {unit} still writes to a console")

    provision = _effective_unit_assignments(rootfs, "vms-provision.service")
    provision_after = set(_effective_unit_list(provision, "Unit", "After"))
    provision_requires = set(_effective_unit_list(provision, "Unit", "Requires"))
    if (
        not {"cloud-init-network.service", "NetworkManager.service"}.issubset(
            provision_after
        )
        or "cloud-init-network.service" not in provision_requires
    ):
        raise CameraImageBuildError("vms-provision.service ordering is incomplete")
    if provision_after.intersection(
        {"cloud-config.service", "cloud-final.service", "network-online.target"}
    ):
        raise CameraImageBuildError(
            "vms-provision.service has an unsafe boot dependency"
        )
    if _effective_scalar(provision, "Service", "TimeoutStartSec") != (
        f"{_VMS_PROVISION_TIMEOUT_SECONDS}s"
    ):
        raise CameraImageBuildError("vms-provision.service is not bounded")
    retry_contract = {
        ("Unit", "StartLimitIntervalSec"): "0",
        ("Service", "Restart"): "on-failure",
        ("Service", "RestartSec"): "15s",
        ("Service", "RestartSteps"): "4",
        ("Service", "RestartMaxDelaySec"): "5min",
    }
    if any(
        _effective_scalar(provision, section, key) != expected
        for (section, key), expected in retry_contract.items()
    ):
        raise CameraImageBuildError("vms-provision.service retry policy is not bounded")

    cleanup = _effective_unit_assignments(rootfs, "vms-provision-cleanup.service")
    cleanup_after = set(_effective_unit_list(cleanup, "Unit", "After"))
    cleanup_requires = set(_effective_unit_list(cleanup, "Unit", "Requires"))
    if (
        not {"cloud-final.service", "vms-provision.service"}.issubset(cleanup_after)
        or "cloud-final.service" not in cleanup_requires
    ):
        raise CameraImageBuildError("post-cloud-final cleanup ordering is incomplete")
    if any(
        _effective_scalar(cleanup, section, key) != expected
        for (section, key), expected in retry_contract.items()
    ):
        raise CameraImageBuildError(
            "vms-provision-cleanup.service retry policy is not durable"
        )
    for unit, assignments in (
        ("vms-provision.service", provision),
        ("vms-provision-cleanup.service", cleanup),
    ):
        if (
            _effective_scalar(assignments, "Service", "StandardOutput") != "journal"
            or _effective_scalar(assignments, "Service", "StandardError") != "journal"
        ):
            raise CameraImageBuildError(f"effective {unit} output is not journal-only")

    recovery = _effective_unit_assignments(rootfs, "vms-network-recovery.service")
    if (
        _effective_scalar(recovery, "Service", "ExecStart")
        != "/usr/bin/python3 /opt/vms/network_recovery.py"
        or _effective_scalar(recovery, "Service", "TimeoutStartSec") != "90"
        or _effective_scalar(recovery, "Service", "ReadWritePaths") != "/var/lib/vms"
    ):
        raise CameraImageBuildError("network recovery service is not bounded")
    timer = _effective_unit_assignments(rootfs, "vms-network-recovery.timer")
    if any(
        _effective_scalar(timer, "Timer", key) != expected
        for key, expected in (
            ("OnBootSec", "30s"),
            ("OnUnitInactiveSec", "30s"),
            ("Unit", "vms-network-recovery.service"),
        )
    ):
        raise CameraImageBuildError("network recovery timer is not bounded")

    _verify_enabled_unit(rootfs, "multi-user.target", "vms-provision.service")
    _verify_enabled_unit(rootfs, "cloud-init.target", "vms-provision-cleanup.service")


def assemble_pi_gen_stage(
    repository: Path,
    stage_directory: Path,
    manifest: CameraImageBuildManifest,
    *,
    source_revision: str,
) -> None:
    """Assemble one pi-gen stage entirely from versioned public source."""

    if _COMMIT.fullmatch(source_revision) is None:
        raise CameraImageBuildError(
            "VMS source must be identified by a full commit hash"
        )
    stage_directory = stage_directory.resolve()
    if stage_directory.name != "stage-vms-camera":
        raise CameraImageBuildError("refusing to replace a non-camera pi-gen stage")
    if stage_directory.is_symlink():
        raise CameraImageBuildError("camera pi-gen stage must not be a symbolic link")
    if stage_directory.exists():
        shutil.rmtree(stage_directory)
    substage = stage_directory / "00-vms-camera"
    files_directory = substage / "files"
    files_directory.mkdir(parents=True)
    previous_stage = stage_directory.parent / "stage2"
    if not previous_stage.is_dir():
        raise CameraImageBuildError("pi-gen stage2 is unavailable")
    skip_image = previous_stage / "SKIP_IMAGES"
    if skip_image.is_symlink():
        raise CameraImageBuildError(
            "pi-gen stage2 SKIP_IMAGES must not be a symbolic link"
        )
    skip_image.write_text("", encoding="utf-8")
    (stage_directory / "EXPORT_IMAGE").write_text("", encoding="utf-8")
    prerun = stage_directory / "prerun.sh"
    prerun.write_text(
        '#!/bin/bash -e\n\nif [ ! -d "${ROOTFS_DIR}" ]; then\n    copy_previous\nfi\n',
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(prerun, 0o755)
    (substage / "00-packages-nr").write_text(
        "\n".join(manifest.packages) + "\n", encoding="utf-8", newline="\n"
    )
    install_script = repository / _PI_CAMERA_IMAGE / "install-rootfs.sh"
    shutil.copyfile(install_script, substage / "00-run.sh")
    os.chmod(substage / "00-run.sh", 0o755)

    camera_source = (
        files("video_monitoring_system.device")
        .joinpath("camera_agent.py")
        .read_text(encoding="utf-8")
    )
    network_recovery_source = (
        files("video_monitoring_system.device")
        .joinpath("network_recovery.py")
        .read_text(encoding="utf-8")
    )
    payload: dict[str, tuple[str, int]] = {
        "opt/vms/camera_agent.py": (camera_source, 0o755),
        "opt/vms/journal_reader.py": (
            files("video_monitoring_system.device")
            .joinpath("journal_reader.py").read_text(encoding="utf-8"), 0o755,
        ),
        "opt/vms/runtime_metrics.py": (
            files("video_monitoring_system.device")
            .joinpath("runtime_metrics.py").read_text(encoding="utf-8"), 0o755,
        ),
        "opt/vms/network_recovery.py": (network_recovery_source, 0o755),
        "etc/systemd/system/vms-camera.service": (CAMERA_SERVICE, 0o644),
        "etc/systemd/system/vms-network-recovery.service": (
            NETWORK_RECOVERY_SERVICE,
            0o644,
        ),
        "etc/systemd/system/vms-network-recovery.timer": (
            NETWORK_RECOVERY_TIMER,
            0o644,
        ),
        "usr/local/libexec/vms-provision": (_VMS_PROVISION_SCRIPT, 0o755),
        "etc/systemd/system/vms-provision.service": (
            _VMS_PROVISION_SERVICE,
            0o644,
        ),
        "usr/local/libexec/vms-provision-cleanup": (
            _VMS_PROVISION_CLEANUP_SCRIPT,
            0o700,
        ),
        "etc/systemd/system/vms-provision-cleanup.service": (
            _VMS_PROVISION_CLEANUP_SERVICE,
            0o644,
        ),
        "usr/local/libexec/vms-usb-recovery": (PILOT_USB_RECOVERY_SCRIPT, 0o700),
        "etc/systemd/system/vms-usb-recovery.service": (
            PILOT_USB_RECOVERY_SERVICE,
            0o644,
        ),
        "usr/local/bin/vms-pilot-diagnostics": (PILOT_DIAGNOSTICS_SCRIPT, 0o755),
        "etc/systemd/journald.conf.d/90-vms-pilot-diagnostics.conf": (
            PILOT_JOURNALD_CONFIG,
            0o644,
        ),
        "etc/ssh/sshd_config.d/00-vms-pilot-recovery.conf": (
            PILOT_SSH_CONFIG,
            0o600,
        ),
    }
    for unit in _CLOUD_INIT_STAGE_UNITS:
        payload[
            f"etc/systemd/system/{unit}.d/{_CLOUD_INIT_JOURNAL_ONLY_DROP_IN_NAME}"
        ] = (_CLOUD_INIT_JOURNAL_ONLY_DROP_IN, 0o644)
    payload_hashes = {
        f"/{relative}": hashlib.sha256(content.encode("utf-8")).hexdigest()
        for relative, (content, _mode) in payload.items()
    }
    contract = {
        "schema_version": 1,
        "profile": manifest.profile,
        "image_reference": manifest.image_reference,
        "release_version": manifest.release_version,
        "first_boot": "configuration-only",
        "network_requirements": {
            "private_lan_required_for_readiness": True,
            "internet_required_for_first_boot": False,
            "internet_required_for_readiness": False,
        },
        "provisioning_dispatch": {
            "service": "vms-provision.service",
            "generated_script": "/usr/local/libexec/vms-first-boot",
            "generated_service": "/etc/systemd/system/vms-first-boot.service",
            "transaction_timeout_seconds": _VMS_PROVISION_TRANSACTION_SECONDS,
            "service_timeout_seconds": _VMS_PROVISION_TIMEOUT_SECONDS,
            "cloud_init_payload_stage": "cloud-init-network.service",
        },
        "post_cloud_init_cleanup": {
            "service": "vms-provision-cleanup.service",
            "after": "cloud-final.service",
            "marker": "/var/lib/vms/cloud-init-cleanup.json",
        },
        "network_recovery": {
            "service": "vms-network-recovery.service",
            "timer": "vms-network-recovery.timer",
            "interval_seconds": 30,
            "state": "/var/lib/vms/network-health.json",
        },
        "cloud_init_stage_output": {
            unit: "journal" for unit in _CLOUD_INIT_STAGE_UNITS
        },
        "vms_source_commit": source_revision,
        "pi_gen_commit": manifest.builder_commit,
        "pi_gen_patch_sha256": hashlib.sha256(
            (repository / _PI_GEN_GPGV_PATCH).read_bytes()
        ).hexdigest(),
        "pi_gen_binfmt_patch_sha256": hashlib.sha256(
            (repository / _PI_GEN_BINFMT_PATCH).read_bytes()
        ).hexdigest(),
        "pi_gen_first_boot_patch_sha256": hashlib.sha256(
            (repository / _PI_GEN_FIRST_BOOT_PATCH).read_bytes()
        ).hexdigest(),
        "payload_sha256": payload_hashes,
    }
    payload["usr/share/vms-camera-image/build-contract.json"] = (
        json.dumps(contract, indent=2, sort_keys=True) + "\n",
        0o644,
    )
    payload["usr/share/vms-camera-image/required-packages.txt"] = (
        "\n".join(manifest.packages) + "\n",
        0o644,
    )
    for relative, (content, mode) in payload.items():
        destination = files_directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")
        os.chmod(destination, mode)


def check_build_host(
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> None:
    if platform != "linux":
        raise CameraImageBuildError(
            "camera images must be built on Linux; flash clients remain platform-neutral"
        )
    missing = [
        command
        for command in ("docker", "git", "debugfs", "e2fsck")
        if which(command) is None
    ]
    if missing:
        raise CameraImageBuildError(
            "camera image build is missing: " + ", ".join(missing)
        )


def _checked_output(arguments: Sequence[str], *, cwd: Path) -> str:
    try:
        completed = subprocess.run(
            list(arguments),
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CameraImageBuildError(f"command failed: {arguments[0]}") from error
    return completed.stdout.strip()


def _require_clean_versioned_source(repository: Path) -> str:
    report = run_doctor(repository)
    if not report.passed:
        raise CameraImageBuildError("vms dev doctor failed; image build did not start")
    revision = _checked_output(["git", "rev-parse", "HEAD"], cwd=repository)
    if _COMMIT.fullmatch(revision) is None:
        raise CameraImageBuildError("VMS source revision is unavailable")
    status = _checked_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repository
    )
    if status:
        raise CameraImageBuildError(
            "image builds require committed, versioned inputs; the repository is dirty"
        )
    return revision


def _run_checked(arguments: Sequence[str], *, cwd: Path) -> None:
    try:
        subprocess.run(list(arguments), cwd=cwd, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise CameraImageBuildError(f"command failed: {arguments[0]}") from error


def _git_blob_sha256(repository: Path, revision_path: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "cat-file", "blob", revision_path],
            cwd=repository,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CameraImageBuildError(
            "pinned pi-gen source blob is unavailable"
        ) from error
    return hashlib.sha256(completed.stdout).hexdigest()


def _apply_pi_gen_patches(pi_gen: Path, patches: Sequence[Path]) -> None:
    """Reconcile exact known patches without leaving a new partial state."""

    pending: list[Path] = []
    for patch in patches:
        reverse_check = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(patch)],
            cwd=pi_gen,
            check=False,
            capture_output=True,
        )
        if reverse_check.returncode == 0:
            continue
        forward_check = subprocess.run(
            ["git", "apply", "--check", str(patch)],
            cwd=pi_gen,
            check=False,
            capture_output=True,
        )
        if forward_check.returncode != 0:
            raise CameraImageBuildError(
                f"pi-gen compatibility patch cannot be reconciled: {patch.name}"
            )
        pending.append(patch)

    if pending:
        patch_arguments = [str(patch) for patch in pending]
        _run_checked(["git", "apply", "--check", *patch_arguments], cwd=pi_gen)
        _run_checked(["git", "apply", *patch_arguments], cwd=pi_gen)


def _pi_gen_compatibility_inputs(
    architecture: str,
) -> tuple[tuple[Path, ...], str, str]:
    if architecture == "armhf":
        binfmt_patch = _PI_GEN_BINFMT_PATCH
        pinned_build_docker = _PINNED_PI_GEN_BUILD_DOCKER_SHA256
        patched_build_docker = _PATCHED_PI_GEN_BUILD_DOCKER_SHA256
    elif architecture == "arm64":
        binfmt_patch = _PI_GEN_ARM64_BINFMT_PATCH
        pinned_build_docker = _PINNED_PI_GEN_ARM64_BUILD_DOCKER_SHA256
        patched_build_docker = _PATCHED_PI_GEN_ARM64_BUILD_DOCKER_SHA256
    else:
        raise CameraImageBuildError("unsupported pi-gen architecture")
    return (
        (_PI_GEN_GPGV_PATCH, binfmt_patch, _PI_GEN_FIRST_BOOT_PATCH),
        pinned_build_docker,
        patched_build_docker,
    )


def _verify_pi_gen_architecture(pi_gen: Path, architecture: str) -> None:
    try:
        lines = (pi_gen / "build.sh").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise CameraImageBuildError("pi-gen architecture is unreadable") from error
    if f"export ARCH={architecture}" not in lines:
        raise CameraImageBuildError(
            f"pinned pi-gen checkout does not build {architecture}"
        )


def _prepare_pi_gen(
    repository: Path,
    work_directory: Path,
    manifest: CameraImageBuildManifest,
) -> Path:
    pi_gen = work_directory / "pi-gen"
    work_directory.mkdir(parents=True, exist_ok=True)
    if not pi_gen.exists():
        try:
            subprocess.run(
                ["git", "clone", manifest.builder_repository, str(pi_gen)],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise CameraImageBuildError("pi-gen could not be cloned") from error
    if not (pi_gen / ".git").is_dir():
        raise CameraImageBuildError("existing pi-gen build directory is invalid")
    origin = _checked_output(["git", "remote", "get-url", "origin"], cwd=pi_gen)
    if origin.rstrip("/") != manifest.builder_repository.rstrip("/"):
        raise CameraImageBuildError("existing pi-gen checkout has the wrong origin")
    relative_patches, pinned_build_docker, patched_build_docker = (
        _pi_gen_compatibility_inputs(manifest.architecture)
    )
    patches = tuple(repository / relative for relative in relative_patches)
    if any(not patch.is_file() for patch in patches):
        raise CameraImageBuildError("a versioned pi-gen compatibility patch is missing")
    tracked_files = frozenset(
        _checked_output(
            ["git", "diff", "--name-only", "--no-ext-diff"], cwd=pi_gen
        ).splitlines()
    )
    if not tracked_files:
        try:
            subprocess.run(
                ["git", "checkout", "--detach", manifest.builder_commit],
                cwd=pi_gen,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise CameraImageBuildError(
                "pinned pi-gen commit is unavailable"
            ) from error
    elif not tracked_files.issubset(_PATCHED_PI_GEN_TRACKED_FILES):
        raise CameraImageBuildError(
            "existing pi-gen checkout has modified tracked files"
        )
    revision = _checked_output(["git", "rev-parse", "HEAD"], cwd=pi_gen)
    if revision != manifest.builder_commit:
        raise CameraImageBuildError("pi-gen checkout does not match the pinned commit")
    _verify_pi_gen_architecture(pi_gen, manifest.architecture)
    if (
        "build-docker.sh" not in tracked_files
        and _git_blob_sha256(pi_gen, f"{manifest.builder_commit}:build-docker.sh")
        != pinned_build_docker
    ):
        raise CameraImageBuildError(
            "pinned pi-gen Docker wrapper is not the verified input"
        )
    if (
        "export-image/05-finalise/01-run.sh" not in tracked_files
        and _git_blob_sha256(
            pi_gen,
            f"{manifest.builder_commit}:export-image/05-finalise/01-run.sh",
        )
        != _PINNED_PI_GEN_FINALISE_SHA256
    ):
        raise CameraImageBuildError(
            "pinned pi-gen finalise script is not the verified input"
        )
    _apply_pi_gen_patches(pi_gen, patches)
    tracked_files = frozenset(
        _checked_output(
            ["git", "diff", "--name-only", "--no-ext-diff"], cwd=pi_gen
        ).splitlines()
    )
    if tracked_files != _PATCHED_PI_GEN_TRACKED_FILES:
        raise CameraImageBuildError("pi-gen compatibility patches are incomplete")
    for patch in patches:
        reverse_check = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(patch)],
            cwd=pi_gen,
            check=False,
            capture_output=True,
        )
        if reverse_check.returncode != 0:
            raise CameraImageBuildError("pi-gen compatibility patch does not match")
    if _sha256(pi_gen / "Dockerfile") != _PATCHED_PI_GEN_DOCKERFILE_SHA256:
        raise CameraImageBuildError(
            "patched pi-gen Dockerfile is not the verified input"
        )
    if _sha256(pi_gen / "build-docker.sh") != patched_build_docker:
        raise CameraImageBuildError(
            "patched pi-gen Docker wrapper is not the verified input"
        )
    if (
        _sha256(pi_gen / "export-image/05-finalise/01-run.sh")
        != _PATCHED_PI_GEN_FINALISE_SHA256
    ):
        raise CameraImageBuildError(
            "patched pi-gen finalise script is not the verified input"
        )
    return pi_gen


def _pi_gen_config(manifest: CameraImageBuildManifest) -> str:
    image_name = re.sub(
        r"[^A-Za-z0-9_.-]", "-", f"vms-camera-{manifest.release_version}"
    )
    return "\n".join(
        (
            f"IMG_NAME='{image_name}'",
            "PI_GEN_RELEASE='Video Monitoring System pilot camera'",
            f"RELEASE='{manifest.distribution_release}'",
            f"ARCH='{manifest.architecture}'",
            "DEPLOY_COMPRESSION='xz'",
            "COMPRESSION_LEVEL='6'",
            "ENABLE_CLOUD_INIT='1'",
            "ENABLE_SSH='1'",
            "FIRST_USER_NAME='vms-build'",
            "STAGE_LIST='stage0 stage1 stage2 stage-vms-camera'",
            "",
        )
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_xz_image(path: Path) -> RawImageInspection:
    """Fully decompress, hash, and validate the image's DOS partition table."""

    digest = hashlib.sha256()
    first_sector = bytearray()
    raw_bytes = 0
    try:
        with lzma.open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                if len(first_sector) < 512:
                    first_sector.extend(chunk[: 512 - len(first_sector)])
                digest.update(chunk)
                raw_bytes += len(chunk)
    except (EOFError, OSError, lzma.LZMAError) as error:
        raise CameraImageBuildError(
            "built camera image failed xz verification"
        ) from error
    if raw_bytes < 512 or raw_bytes % 512:
        raise CameraImageBuildError("built camera image has an invalid raw byte count")
    if first_sector[510:512] != b"\x55\xaa":
        raise CameraImageBuildError(
            "built camera image has no valid DOS partition table"
        )

    partitions: list[RawImagePartition] = []
    for index in range(4):
        entry = first_sector[446 + index * 16 : 462 + index * 16]
        boot_status = entry[0]
        partition_type = entry[4]
        start_sector = int.from_bytes(entry[8:12], "little")
        sector_count = int.from_bytes(entry[12:16], "little")
        if partition_type == 0 and start_sector == 0 and sector_count == 0:
            continue
        if boot_status not in (0, 0x80) or not partition_type:
            raise CameraImageBuildError(
                "built camera image has an invalid partition entry"
            )
        if not start_sector or not sector_count:
            raise CameraImageBuildError("built camera image has an empty partition")
        if (start_sector + sector_count) * 512 > raw_bytes:
            raise CameraImageBuildError(
                "built camera image partition exceeds the image"
            )
        partitions.append(
            RawImagePartition(
                index=index + 1,
                type=f"0x{partition_type:02x}",
                start_sector=start_sector,
                sector_count=sector_count,
            )
        )
    if len(partitions) != 2:
        raise CameraImageBuildError(
            "camera image must contain boot and root partitions"
        )
    if partitions[0].type not in {"0x06", "0x0b", "0x0c", "0x0e"}:
        raise CameraImageBuildError("camera image boot partition is not FAT")
    if partitions[1].type != "0x83":
        raise CameraImageBuildError("camera image root partition is not Linux")
    if (
        partitions[0].start_sector + partitions[0].sector_count
        > partitions[1].start_sector
    ):
        raise CameraImageBuildError("camera image partitions overlap")
    return RawImageInspection(
        bytes=raw_bytes,
        sha256=digest.hexdigest(),
        partitions=tuple(partitions),
    )


def _extract_xz_partition(
    image: Path, partition: RawImagePartition, destination: Path
) -> None:
    byte_offset = partition.start_sector * 512
    remaining = partition.sector_count * 512
    skipped = 0
    try:
        with lzma.open(image, "rb") as source, destination.open("xb") as output:
            while skipped < byte_offset:
                chunk = source.read(min(1024 * 1024, byte_offset - skipped))
                if not chunk:
                    raise CameraImageBuildError(
                        "built camera root partition starts beyond the image"
                    )
                skipped += len(chunk)
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise CameraImageBuildError(
                        "built camera root partition is truncated"
                    )
                output.write(chunk)
                remaining -= len(chunk)
    except CameraImageBuildError:
        raise
    except (EOFError, OSError, lzma.LZMAError) as error:
        raise CameraImageBuildError(
            "built camera root partition could not be extracted"
        ) from error


def _debugfs_quote(path: Path) -> str:
    value = os.fspath(path)
    if any(character in value for character in ("\0", "\n", "\r")):
        raise CameraImageBuildError("temporary rootfs path is unsafe")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def verify_exported_image_rootfs(image: Path, inspection: RawImageInspection) -> None:
    """Extract and verify the exported ext4 root partition without mounting it."""

    _verify_exported_image_rootfs(
        image,
        inspection,
        runtime_verifier=verify_rootfs_runtime_contract,
        role="camera",
    )


def _verify_exported_image_rootfs(
    image: Path,
    inspection: RawImageInspection,
    *,
    runtime_verifier: Callable[[Path], None],
    role: str,
) -> None:
    """Verify a built image and dispatch its role-specific rootfs checks."""

    debugfs = shutil.which("debugfs")
    e2fsck = shutil.which("e2fsck")
    if debugfs is None or e2fsck is None:
        raise CameraImageBuildError(
            f"{role} image rootfs verification requires debugfs and e2fsck "
            "from e2fsprogs"
        )
    image = Path(image)
    if image.is_symlink() or not image.is_file():
        raise CameraImageBuildError(
            f"built {role} image artifact is missing or unsafe"
        )
    root_partitions = tuple(
        partition for partition in inspection.partitions if partition.type == "0x83"
    )
    if len(root_partitions) != 1:
        raise CameraImageBuildError(
            f"{role} image must contain exactly one Linux root partition"
        )

    try:
        with tempfile.TemporaryDirectory(prefix="vms-camera-rootfs-") as directory:
            temporary = Path(directory)
            partition_image = temporary / "root.ext4"
            extracted_rootfs = temporary / "rootfs"
            extracted_rootfs.mkdir(mode=0o700)
            partition_bytes = root_partitions[0].sector_count * 512
            required_free = partition_bytes * 2 + 256 * 1024 * 1024
            if shutil.disk_usage(temporary).free < required_free:
                raise CameraImageBuildError(
                    f"{role} image rootfs verification has insufficient temporary space"
                )
            _extract_xz_partition(image, root_partitions[0], partition_image)
            verification_environment = dict(os.environ, LC_ALL="C")
            try:
                subprocess.run(
                    [e2fsck, "-f", "-n", os.fspath(partition_image)],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=verification_environment,
                )
            except (OSError, subprocess.CalledProcessError) as error:
                raise CameraImageBuildError(
                    f"built {role} ext4 root filesystem failed e2fsck"
                ) from error
            command = f"rdump / {_debugfs_quote(extracted_rootfs)}"
            try:
                completed = subprocess.run(
                    [debugfs, "-R", command, os.fspath(partition_image)],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=verification_environment,
                )
            except (OSError, subprocess.CalledProcessError) as error:
                raise CameraImageBuildError(
                    f"built {role} ext4 root filesystem could not be read"
                ) from error
            debugfs_banner = re.compile(
                r"^debugfs [0-9]+(?:\.[0-9]+)+(?: \([^()\r\n]+\))?$"
            )
            ownership_warning = re.compile(
                r"^(?:rdump|dump_file): Operation not permitted while changing "
                r"ownership of (?P<path>.+)$"
            )

            def is_expected_diagnostic(line: str) -> bool:
                if debugfs_banner.fullmatch(line) is not None:
                    return True
                warning = ownership_warning.fullmatch(line)
                if warning is None:
                    return False
                try:
                    relative_path = Path(warning.group("path")).relative_to(
                        extracted_rootfs
                    )
                except ValueError:
                    return False
                return ".." not in relative_path.parts

            unexpected_diagnostics = tuple(
                line
                for line in completed.stderr.splitlines()
                if line and not is_expected_diagnostic(line)
            )
            if unexpected_diagnostics:
                raise CameraImageBuildError(
                    f"debugfs reported an error while reading the {role} rootfs: "
                    + "; ".join(unexpected_diagnostics[:3])
                )
            verify_rootfs_first_boot_state(extracted_rootfs)
            runtime_verifier(extracted_rootfs)
    except CameraImageBuildError:
        raise
    except OSError as error:
        raise CameraImageBuildError(
            f"temporary {role} rootfs verification failed"
        ) from error


def build_camera_image(
    repository: Path,
    manifest_path: Path,
    work_directory: Path,
    output_directory: Path,
) -> tuple[Path, Path, Path]:
    source_revision = _require_clean_versioned_source(repository)
    check_build_host()
    manifest = load_build_manifest(manifest_path)
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise CameraImageBuildError(
            "Docker is installed but its daemon is unavailable"
        ) from error

    pi_gen = _prepare_pi_gen(repository, work_directory, manifest)
    assemble_pi_gen_stage(
        repository,
        pi_gen / "stage-vms-camera",
        manifest,
        source_revision=source_revision,
    )
    config = pi_gen / "config-vms-camera"
    config.write_text(_pi_gen_config(manifest), encoding="utf-8", newline="\n")
    # build-docker.sh always copies /pi-gen/deploy out of the build container.
    # A custom DEPLOY_DIR would leave the verified artifact inside the discarded
    # container, so the Docker path intentionally uses pi-gen's default.
    deploy = pi_gen / "deploy"
    if deploy.is_symlink():
        raise CameraImageBuildError(
            "pi-gen deploy directory must not be a symbolic link"
        )
    if deploy.exists():
        shutil.rmtree(deploy)
    try:
        subprocess.run(["./build-docker.sh", "-c", config.name], cwd=pi_gen, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise CameraImageBuildError("pi-gen camera image build failed") from error
    artifacts = tuple(deploy.glob("*.img.xz"))
    if len(artifacts) != 1:
        raise CameraImageBuildError(
            "pi-gen did not produce exactly one compressed image"
        )

    output_directory.mkdir(parents=True, exist_ok=True)
    artifact = output_directory / f"vms-camera-{manifest.release_version}.img.xz"
    release_path = output_directory / f"vms-camera-{manifest.release_version}.json"
    verification_path = (
        output_directory / f"vms-camera-{manifest.release_version}.verification.json"
    )
    if artifact.exists() or release_path.exists() or verification_path.exists():
        raise CameraImageBuildError(
            "output already exists; select an empty output directory"
        )
    try:
        with tempfile.TemporaryDirectory(
            prefix=".vms-camera-verify-", dir=output_directory
        ) as staging_directory:
            staged_artifact = Path(staging_directory) / artifact.name
            shutil.copy2(artifacts[0], staged_artifact)
            inspection = inspect_xz_image(staged_artifact)
            verify_exported_image_rootfs(staged_artifact, inspection)
            compressed_sha256 = _sha256(staged_artifact)
            try:
                os.link(staged_artifact, artifact)
            except FileExistsError as error:
                raise CameraImageBuildError(
                    "output was created while the camera image was being verified"
                ) from error
            staged_artifact.unlink()
    except CameraImageBuildError:
        raise
    except OSError as error:
        raise CameraImageBuildError(
            "verified camera image could not be published"
        ) from error
    release = {
        "schema_version": 1,
        "release_version": manifest.release_version,
        "role": "camera",
        "supported_boards": list(manifest.supported_boards),
        "image": str(artifact.resolve()),
        "sha256": compressed_sha256,
        "extract_sha256": inspection.sha256,
        "signature": None,
        "channel": "development",
        "architecture": manifest.architecture,
        "bits": manifest.bits,
        "released": date.today().isoformat(),
        "image_reference": manifest.image_reference,
    }
    release_path.write_text(
        json.dumps(release, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    verification = {
        "schema_version": 1,
        "release_version": manifest.release_version,
        "compressed_image": {
            "bytes": artifact.stat().st_size,
            "sha256": compressed_sha256,
        },
        "raw_image": {
            "bytes": inspection.bytes,
            "sha256": inspection.sha256,
            "sector_size": 512,
            "partitions": [partition.to_dict() for partition in inspection.partitions],
        },
        "rootfs_contract": {
            "verified": True,
            "method": "debugfs-rdump-read-only",
            "filesystem_check": "e2fsck -f -n",
            "scope": "linux-root-partition-only",
        },
    }
    verification_path.write_text(
        json.dumps(verification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return artifact, release_path, verification_path


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    repository = _repository_root()
    parser = argparse.ArgumentParser(
        prog="build-camera-image",
        description="Build the preinstalled Zero W pilot camera image on Linux.",
    )
    parser.add_argument(
        "--check", action="store_true", help="validate without building"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository / _PI_CAMERA_IMAGE / "build.json",
    )
    parser.add_argument(
        "--work-dir", type=Path, default=repository / "build" / "camera-image"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=repository / "dist" / "releases"
    )
    arguments = parser.parse_args(argv)
    try:
        if arguments.check:
            manifest = load_build_manifest(arguments.manifest)
            revision = _require_clean_versioned_source(repository)
            check_build_host()
            print(
                f"camera image inputs ready: {manifest.release_version} from {revision}"
            )
            return 0
        artifact, release, verification = build_camera_image(
            repository,
            arguments.manifest,
            arguments.work_dir,
            arguments.output_dir,
        )
    except CameraImageBuildError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    print(f"Image: {artifact}")
    print(f"Development release manifest: {release}")
    print(f"Image verification: {verification}")
    print("The image is unsigned and must not be promoted as a product release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
