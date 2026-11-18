"""Platform-neutral primitives for creating recorder and camera kits.

This module deliberately separates public manifests from the private identity
material needed while provisioning. It performs no flashing and emits no logs.
"""

from __future__ import annotations

import base64
import hashlib
from importlib.resources import files
import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping, Protocol

from ..device.pilot import (
    PILOT_DIAGNOSTICS_SCRIPT,
    PILOT_SSH_CONFIG,
    PILOT_USB_RECOVERY_SCRIPT,
    PILOT_USB_RECOVERY_SERVICE,
)
from ..device.service import (
    CAMERA_SERVICE,
    NETWORK_RECOVERY_SERVICE,
    NETWORK_RECOVERY_TIMER,
)
from ..regulatory import validate_wifi_country
from ..model import SOFTWARE_CAMERA_LIMIT


@dataclass(frozen=True, slots=True)
class BoardProfile:
    """A supported board and the product roles it may perform."""

    name: str
    display_name: str
    architecture: str
    roles: tuple[str, ...]
    release_tier: str


BOARD_PROFILES: Mapping[str, BoardProfile] = MappingProxyType(
    {
        "zero-w": BoardProfile(
            name="zero-w",
            display_name="Raspberry Pi Zero W",
            architecture="armv6",
            roles=("camera",),
            release_tier="pilot",
        ),
        "zero-2-w": BoardProfile(
            name="zero-2-w",
            display_name="Raspberry Pi Zero 2 W",
            architecture="arm64",
            roles=("camera",),
            release_tier="product",
        ),
        "pi4": BoardProfile(
            name="pi4",
            display_name="Raspberry Pi 4 Model B",
            architecture="arm64",
            roles=("recorder", "camera"),
            release_tier="product",
        ),
    }
)


def get_board_profile(name: str, role: str | None = None) -> BoardProfile:
    """Return a profile, optionally verifying that it supports ``role``."""

    try:
        profile = BOARD_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unsupported board profile: {name}") from exc
    if role is not None and role not in profile.roles:
        raise ValueError(f"board profile {name} does not support role {role}")
    return profile


@dataclass(frozen=True, slots=True)
class KeyMaterial:
    """Private authentication material and its publishable counterpart."""

    algorithm: str
    public_identity: str
    private_identity: str


class KeyGenerator(Protocol):
    """Extension point for a signing-key implementation such as Ed25519."""

    def generate(self) -> KeyMaterial:
        """Generate one independent device identity."""


@dataclass(frozen=True, slots=True)
class OpaqueTokenKeyGenerator:
    """Standard-library fallback using a high-entropy opaque bearer token.

    It is not a signing-key implementation.  The public value is a fingerprint
    that can safely identify the corresponding private token in a manifest.
    """

    entropy_bytes: int = 32

    def generate(self) -> KeyMaterial:
        if self.entropy_bytes < 32:
            raise ValueError("device identities require at least 32 bytes of entropy")
        private_identity = secrets.token_urlsafe(self.entropy_bytes)
        digest = hashlib.sha256(
            b"vms-opaque-identity\0" + private_identity.encode("ascii")
        ).hexdigest()
        return KeyMaterial(
            algorithm="opaque-token-sha256-v1",
            public_identity=digest,
            private_identity=private_identity,
        )


@dataclass(frozen=True, slots=True)
class DeviceManifest:
    """Public device data suitable for labels and inventory."""

    device_id: str
    kit_id: str
    role: str
    board: str
    image_reference: str
    identity_algorithm: str
    public_identity: str
    camera_number: int | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """Private per-device material.  Never serialize this into public output."""

    device_id: str
    kit_id: str
    role: str
    board: str
    image_reference: str
    key: KeyMaterial
    claim_secret: str | None
    stream_secret: str | None
    camera_number: int | None
    pilot_recovery_secret: str | None = None

    def public_manifest(self) -> DeviceManifest:
        return DeviceManifest(
            device_id=self.device_id,
            kit_id=self.kit_id,
            role=self.role,
            board=self.board,
            image_reference=self.image_reference,
            identity_algorithm=self.key.algorithm,
            public_identity=self.key.public_identity,
            camera_number=self.camera_number,
        )


@dataclass(frozen=True, slots=True)
class KitManifest:
    """Public recorder inventory and its authorized cameras."""

    schema_version: int
    kit_id: str
    recorder: DeviceManifest
    cameras: tuple[DeviceManifest, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kit_id": self.kit_id,
            "recorder": self.recorder.to_dict(),
            "cameras": [camera.to_dict() for camera in self.cameras],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class KitIdentity:
    """Complete private state for a kit provisioning job."""

    kit_id: str
    wifi_ssid: str
    wifi_passphrase: str
    wifi_country: str | None
    recorder: DeviceIdentity
    cameras: tuple[DeviceIdentity, ...]

    def public_manifest(self) -> KitManifest:
        return KitManifest(
            schema_version=1,
            kit_id=self.kit_id,
            recorder=self.recorder.public_manifest(),
            cameras=tuple(camera.public_manifest() for camera in self.cameras),
        )


@dataclass(frozen=True, slots=True)
class ProvisioningSpec:
    """Non-secret inputs that define one kit."""

    camera_count: int
    camera_board: str = "zero-2-w"
    recorder_board: str = "pi4"
    # The 32-bit build is compatible with both supported camera generations.
    camera_image: str = "raspios-32-vms-camera"
    recorder_image: str = "raspios-64-vms-recorder-preinstalled"
    camera_wifi_band: str = "2.4-ghz"
    camera_wifi_security: str = "wpa2-psk"

    def __post_init__(self) -> None:
        if not 1 <= self.camera_count <= SOFTWARE_CAMERA_LIMIT:
            raise ValueError(
                f"camera_count must be between 1 and {SOFTWARE_CAMERA_LIMIT}"
            )
        get_board_profile(self.camera_board, "camera")
        get_board_profile(self.recorder_board, "recorder")
        validate_camera_network_compatibility(
            self.camera_board,
            band=self.camera_wifi_band,
            security=self.camera_wifi_security,
        )
        if not self.camera_image.strip() or not self.recorder_image.strip():
            raise ValueError("image references must not be empty")


@dataclass(frozen=True, slots=True)
class ImageAssignment:
    """One shared image and every device that receives it."""

    image_reference: str
    device_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeviceConfiguration:
    """Private per-device configuration layered onto a shared image."""

    device_id: str
    image_reference: str
    user_data: str
    network_config: str
    network_manager: str
    boot_customization: BootCustomization | None


@dataclass(frozen=True, slots=True)
class BootCustomization:
    """Non-secret boot-partition changes requested by an imaging adapter.

    Cloud-init repeats these changes on the device as a compatibility fallback.
    An adapter that can edit the boot partition after writing the image applies
    them before the first power-on, which makes the earliest kernel output
    available during a pilot recovery.
    """

    config_txt_append: tuple[str, ...]
    cmdline_append: tuple[str, ...]
    cmdline_remove: tuple[str, ...]


def _random_id(prefix: str, entropy_bytes: int = 8) -> str:
    return f"{prefix}-{secrets.token_hex(entropy_bytes).upper()}"


CAMERA_WIFI_BAND = "2.4-ghz"
CAMERA_WIFI_SECURITY = "wpa2-psk"
PILOT_RECOVERY_USERNAME = "vms-recovery"
PREINSTALLED_CAMERA_IMAGE_REFERENCE = "raspios-32-vms-camera-preinstalled"
PREINSTALLED_RECORDER_IMAGE_REFERENCE = "raspios-64-vms-recorder-preinstalled"
_CRYPT_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_CAMERA_DEVICE_ID = re.compile(r"CAM-[0-9A-F]{16,64}")


def pilot_recovery_boot_customization(
    identity: DeviceIdentity,
) -> BootCustomization | None:
    """Return the early-boot diagnostic profile for a pilot device."""

    if identity.pilot_recovery_secret is None:
        return None
    return BootCustomization(
        config_txt_append=(
            "enable_uart=1",
            "uart_2ndstage=1",
            "dtoverlay=dwc2,dr_mode=peripheral",
        ),
        cmdline_append=(
            "console=serial0,115200",
            "loglevel=7",
            "systemd.show_status=1",
        ),
        cmdline_remove=(
            "quiet",
            "splash",
            "plymouth.ignore-serial-consoles",
            "systemd.log_level=debug",
        ),
    )


def validate_camera_network_compatibility(
    board: str, *, band: str, security: str
) -> None:
    """Fail closed when a camera network is outside the tested profile.

    Both supported Zero generations are provisioned only for 2.4 GHz WPA2
    Personal.  Keeping this as an explicit hook lets a future discovery UI
    reject an incompatible router before a card is written.
    """

    get_board_profile(board, "camera")
    if band != CAMERA_WIFI_BAND:
        raise ValueError("camera Wi-Fi must be a 2.4 GHz network")
    if security != CAMERA_WIFI_SECURITY:
        raise ValueError("camera Wi-Fi must use WPA2 Personal (PSK)")


def _crypt_base64(value: int, length: int) -> str:
    encoded = []
    for _ in range(length):
        encoded.append(_CRYPT_ALPHABET[value & 0x3F])
        value >>= 6
    return "".join(encoded)


def _sha512_crypt(password: str, salt: str, rounds: int = 5000) -> str:
    """Return a portable SHA-512 crypt hash without a plaintext password.

    Python removed the platform ``crypt`` module, and invoking an external
    utility would make provisioning host-dependent.  Recovery secrets have
    256 bits of generated entropy; the standard 5000 rounds are therefore not
    compensating for a human-selected password.
    """

    password_bytes = password.encode("utf-8")
    salt_bytes = salt.encode("ascii")
    if not password_bytes or not 1 <= len(salt_bytes) <= 16:
        raise ValueError("invalid recovery password or salt")
    if not 1000 <= rounds <= 999_999_999:
        raise ValueError("invalid SHA-512 crypt round count")

    alternate = hashlib.sha512(password_bytes + salt_bytes + password_bytes).digest()
    digest = hashlib.sha512()
    digest.update(password_bytes)
    digest.update(salt_bytes)
    digest.update((alternate * ((len(password_bytes) + 63) // 64))[: len(password_bytes)])
    count = len(password_bytes)
    while count:
        digest.update(alternate if count & 1 else password_bytes)
        count >>= 1
    current = digest.digest()

    password_digest = hashlib.sha512(password_bytes * len(password_bytes)).digest()
    repeated_password = (
        password_digest * ((len(password_bytes) + 63) // 64)
    )[: len(password_bytes)]
    salt_digest = hashlib.sha512(salt_bytes * (16 + current[0])).digest()
    repeated_salt = (salt_digest * ((len(salt_bytes) + 63) // 64))[: len(salt_bytes)]

    for index in range(rounds):
        iteration = hashlib.sha512()
        iteration.update(repeated_password if index & 1 else current)
        if index % 3:
            iteration.update(repeated_salt)
        if index % 7:
            iteration.update(repeated_password)
        iteration.update(current if index & 1 else repeated_password)
        current = iteration.digest()

    groups = (
        (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45),
        (25, 46, 4), (47, 5, 26), (6, 27, 48), (28, 49, 7),
        (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32),
        (12, 33, 54), (34, 55, 13), (56, 14, 35), (15, 36, 57),
        (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19),
        (62, 20, 41),
    )
    encoded = "".join(
        _crypt_base64(
            current[first] << 16 | current[second] << 8 | current[third], 4
        )
        for first, second, third in groups
    )
    encoded += _crypt_base64(current[63], 2)
    rounds_field = "" if rounds == 5000 else f"rounds={rounds}$"
    return f"$6${rounds_field}{salt}${encoded}"


def _pilot_recovery_password_hash(identity: DeviceIdentity) -> str:
    secret = identity.pilot_recovery_secret
    if secret is None:
        raise ValueError("pilot recovery is not enabled for this device")
    salt = hashlib.sha256(
        b"vms-pilot-recovery\0" + identity.device_id.encode("ascii")
    ).hexdigest()[:16]
    return _sha512_crypt(secret, salt)


def _pilot_recovery_ssh_key(identity: DeviceIdentity):
    """Derive the pilot-only SSH key without adding another persisted secret."""

    secret = identity.pilot_recovery_secret
    if secret is None:
        raise ValueError("pilot recovery is not enabled for this device")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError as error:  # pragma: no cover - required project dependency
        raise RuntimeError("pilot SSH recovery requires cryptography") from error
    seed = hashlib.sha256(
        b"vms-pilot-recovery-ssh-v1\0"
        + identity.device_id.encode("ascii")
        + b"\0"
        + secret.encode("ascii")
    ).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def pilot_recovery_ssh_authorized_key(identity: DeviceIdentity) -> str:
    """Return only the public key installed on a pilot camera."""

    from cryptography.hazmat.primitives import serialization

    key = _pilot_recovery_ssh_key(identity).public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return f"{key.decode('ascii')} {PILOT_RECOVERY_USERNAME}@{identity.device_id}"


def pilot_recovery_ssh_private_key(identity: DeviceIdentity) -> str:
    """Return a deterministic OpenSSH key for owner-private storage."""

    from cryptography.hazmat.primitives import serialization

    value = _pilot_recovery_ssh_key(identity).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return value.decode("ascii")


def _required_wifi_country(kit: KitIdentity) -> str:
    if kit.wifi_country is None:
        raise ValueError(
            "legacy provisioning state requires an explicit Wi-Fi country before use"
        )
    return validate_wifi_country(kit.wifi_country)


def _private_device_document(
    identity: DeviceIdentity, kit: KitIdentity | None = None
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "device_id": identity.device_id,
        "kit_id": identity.kit_id,
        "role": identity.role,
        "camera_number": identity.camera_number,
        "identity": {
            "algorithm": identity.key.algorithm,
            "public": identity.key.public_identity,
            "private": identity.key.private_identity,
        },
        "claim_secret": identity.claim_secret,
        "stream_secret": identity.stream_secret,
    }
    if identity.role == "recorder" and kit is not None:
        if kit.kit_id != identity.kit_id:
            raise ValueError("recorder configuration requires its kit identity")
        document["authorized_cameras"] = [
            {
                "device_id": camera.device_id,
                "camera_number": camera.camera_number,
                "identity": {
                    "algorithm": camera.key.algorithm,
                    "public": camera.key.public_identity,
                },
                "claim_secret_hash": hashlib.sha256(
                    (camera.claim_secret or "").encode("utf-8")
                ).hexdigest(),
                "stream_secret": camera.stream_secret,
            }
            for camera in kit.cameras
        ]
    elif identity.role == "camera" and kit is not None:
        if kit.kit_id != identity.kit_id:
            raise ValueError("camera configuration requires its kit identity")
        document["assigned_recorder"] = {
            "device_id": kit.recorder.device_id,
            "network_host": "osecameras.local",
            "network_hosts": ["osecameras.local"],
            "home_connection": f"vms-home-{identity.camera_number:03d}",
            "identity": {
                "algorithm": kit.recorder.key.algorithm,
                "public": kit.recorder.key.public_identity,
            },
        }
    return document


def _render_first_boot_script(
    identity: DeviceIdentity,
    kit: KitIdentity,
    *,
    runtime_preinstalled: bool = False,
) -> str:
    """Render the retryable, fail-fast on-device provisioning transaction."""

    wifi_country = _required_wifi_country(kit)
    if identity.role == "camera":
        connection_id = f"vms-home-{identity.camera_number:03d}"
        network_configuration = f"""keyfile=/etc/NetworkManager/system-connections/{connection_id}.nmconnection
test -s "$keyfile"
chown root:root "$keyfile"
chmod 600 "$keyfile"
test "$(stat -c '%u:%g:%a' "$keyfile")" = 0:0:600
timeout 45 nmcli connection load "$keyfile" >/dev/null
timeout 45 nmcli connection modify {connection_id} connection.autoconnect yes
timeout 45 nmcli connection reload
timeout 65 nmcli --wait 60 connection up {connection_id} >/dev/null
nm-online -q --timeout=60
LC_ALL=C nmcli -t -f GENERAL.STATE device show wlan0 | grep -Eq '^GENERAL.STATE:100'
LC_ALL=C nmcli -t -f GENERAL.CONNECTION device show wlan0 | grep -Fqx 'GENERAL.CONNECTION:{connection_id}'
"""
        camera_service_steps = """systemctl daemon-reload
timeout 90 systemctl enable --now avahi-daemon.service vms-camera.service vms-network-recovery.timer
timeout 90 systemctl start vms-network-recovery.service
systemctl is-active --quiet avahi-daemon.service
systemctl is-active --quiet vms-camera.service
systemctl is-active --quiet vms-network-recovery.timer
test -s /var/lib/vms/network-health.json
test "$(stat -c '%u:%g:%a' /etc/vms/device.json)" = 0:0:600
python3 - <<'PY'
import base64
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

config = json.loads(Path("/etc/vms/device.json").read_text(encoding="utf-8"))
valid = (
    config.get("schema_version") == 1
    and config.get("role") == "camera"
    and isinstance(config.get("device_id"), str)
    and bool(config["device_id"])
    and isinstance(config.get("camera_number"), int)
    and config["camera_number"] > 0
    and isinstance(config.get("identity"), dict)
    and isinstance(config.get("assigned_recorder"), dict)
    and isinstance(config.get("stream_secret"), str)
    and bool(config["stream_secret"])
)
if not valid:
    raise SystemExit("camera provisioning document is invalid")

credential = f'{config["device_id"]}:{config["stream_secret"]}'.encode("utf-8")
authorization = "Basic " + base64.b64encode(credential).decode("ascii")
deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    try:
        health_request = Request(
            "http://127.0.0.1:9080/health",
            headers={"Authorization": authorization},
        )
        with urlopen(health_request, timeout=4) as response:
            health = json.loads(response.read(65537))
        health_valid = (
            health.get("device_id") == config["device_id"]
            and health.get("camera_number") == config["camera_number"]
            and health.get("camera_ready") is True
            and isinstance(health.get("network"), dict)
            and health["network"].get("state") == "healthy"
            and health["network"].get("recorder_reachable") is True
        )
        if not health_valid:
            raise RuntimeError("camera health is not ready")
        stream_request = Request(
            "http://127.0.0.1:9080/stream.h264",
            headers={"Authorization": authorization},
        )
        with urlopen(stream_request, timeout=4) as response:
            content_type = response.headers.get_content_type()
            stream_prefix = response.read(5)
        if content_type != "video/h264" or not (
            stream_prefix.startswith(b"\\x00\\x00\\x01")
            or stream_prefix.startswith(b"\\x00\\x00\\x00\\x01")
        ):
            raise RuntimeError("camera H.264 stream is invalid")
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("camera readiness probe failed")
PY
"""
        if runtime_preinstalled:
            role_steps = f"""test -s /opt/vms/camera_agent.py
test -s /opt/vms/journal_reader.py
test -s /opt/vms/runtime_metrics.py
test -s /opt/vms/network_recovery.py
test -s /etc/systemd/system/vms-camera.service
test -s /etc/systemd/system/vms-network-recovery.service
test -s /etc/systemd/system/vms-network-recovery.timer
python3 -c 'import picamera2'
command -v avahi-daemon >/dev/null
command -v ping >/dev/null
{network_configuration}{camera_service_steps}
"""
        else:
            role_steps = f"""{network_configuration}
export DEBIAN_FRONTEND=noninteractive
timeout --kill-after=30 600 apt-get update
timeout --kill-after=30 900 apt-get install -y --no-install-recommends python3-picamera2 avahi-daemon iputils-ping
{camera_service_steps}
"""
        final_role_checks = f"""systemctl is-active --quiet avahi-daemon.service
systemctl is-active --quiet vms-camera.service
systemctl is-active --quiet vms-network-recovery.timer
test -s /var/lib/vms/network-health.json
LC_ALL=C nmcli -t -f GENERAL.STATE device show wlan0 | grep -Eq '^GENERAL.STATE:100'
LC_ALL=C nmcli -t -f GENERAL.CONNECTION device show wlan0 | grep -Fqx 'GENERAL.CONNECTION:{connection_id}'
"""
    else:
        recorder_service_steps = ""
        if runtime_preinstalled:
            recorder_service_steps = """test -x /opt/video-monitoring-system/bin/vms
test -s /etc/systemd/system/vms-recorder.service
test -s /etc/systemd/system/vms-recorder-capture.timer
test -s /etc/systemd/system/vms-recorder-alerts.service
test -s /etc/systemd/system/vms-recorder-monitor.service
test -s /etc/systemd/system/vms-recorder-maintenance.timer
test -s /etc/systemd/system/vms-recording-ready.path
test -s /etc/systemd/system/vms-remote-gateway.path
systemctl daemon-reload
timeout 90 systemctl enable --now \\
    vms-recording-ready.path \\
    vms-recorder.service \\
    vms-recorder-capture.timer \\
    vms-recorder-alerts.service \\
    vms-recorder-maintenance.timer \\
    vms-remote-gateway.path
systemctl is-active --quiet vms-recorder.service
systemctl is-active --quiet vms-recorder-capture.timer
systemctl is-active --quiet vms-recorder-alerts.service
systemctl enable --now vms-recorder-monitor.service
systemctl is-active --quiet vms-recorder-monitor.service
systemctl is-active --quiet vms-recorder-maintenance.timer
systemctl is-active --quiet vms-recording-ready.path
systemctl is-active --quiet vms-remote-gateway.path
"""
        role_steps = """systemctl enable --now NetworkManager.service
timeout 60 nmcli --wait 55 connection up id vms-recorder ifname eth0 >/dev/null
nm-online -q --timeout=60
LC_ALL=C nmcli -t -f GENERAL.STATE device show eth0 | grep -Eq '^GENERAL.STATE:100'
""" + recorder_service_steps
        final_role_checks = """systemctl is-active --quiet NetworkManager.service
LC_ALL=C nmcli -t -f GENERAL.STATE device show eth0 | grep -Eq '^GENERAL.STATE:100'
ip -4 -o address show dev eth0 scope global | grep -q .
"""
        if runtime_preinstalled:
            final_role_checks += """systemctl is-active --quiet vms-recorder.service
systemctl is-active --quiet vms-recorder-capture.timer
systemctl is-active --quiet vms-recorder-alerts.service
systemctl is-active --quiet vms-recorder-monitor.service
systemctl is-active --quiet vms-recorder-maintenance.timer
systemctl is-active --quiet vms-recording-ready.path
systemctl is-active --quiet vms-remote-gateway.path
"""

    recovery_steps = ""
    if identity.pilot_recovery_secret is not None:
        password_record = (
            f"{PILOT_RECOVERY_USERNAME}:{_pilot_recovery_password_hash(identity)}"
        )
        password_record_b64 = base64.b64encode(
            password_record.encode("ascii")
        ).decode("ascii")
        authorized_key_b64 = base64.b64encode(
            (pilot_recovery_ssh_authorized_key(identity) + "\n").encode("ascii")
        ).decode("ascii")
        ssh_config_b64 = base64.b64encode(PILOT_SSH_CONFIG.encode("ascii")).decode(
            "ascii"
        )
        recovery_steps = """recovery_user=vms-recovery
recovery_home=/home/vms-recovery
command -v useradd >/dev/null
command -v usermod >/dev/null
command -v chpasswd >/dev/null
command -v sshd >/dev/null
if ! id "$recovery_user" >/dev/null 2>&1; then
    useradd --create-home --home-dir "$recovery_home" --shell /bin/bash "$recovery_user"
fi
usermod --home "$recovery_home" --shell /bin/bash --append --groups adm,sudo "$recovery_user"
recovery_group=$(id -gn "$recovery_user")
recovery_uid=$(id -u "$recovery_user")
recovery_gid=$(id -g "$recovery_user")
install -d -m 0750 -o "$recovery_user" -g "$recovery_group" "$recovery_home"
install -d -m 0700 -o "$recovery_user" -g "$recovery_group" "$recovery_home/.ssh"
password_record=$(printf '%s' '__PASSWORD_RECORD_B64__' | base64 -d)
expected_password_hash=${password_record#*:}
printf '%s\\n' "$password_record" | chpasswd -e
unset password_record
authorized_key_tmp="$state_dir/authorized_keys.tmp"
authorized_keys="$recovery_home/.ssh/authorized_keys"
printf '%s' '__AUTHORIZED_KEY_B64__' | base64 -d > "$authorized_key_tmp"
install -m 0600 -o "$recovery_user" -g "$recovery_group" "$authorized_key_tmp" "$authorized_keys"
rm -f "$authorized_key_tmp"
test "$(getent passwd "$recovery_user" | cut -d: -f6)" = "$recovery_home"
test "$(getent passwd "$recovery_user" | cut -d: -f7)" = /bin/bash
test "$(getent shadow "$recovery_user" | cut -d: -f2)" = "$expected_password_hash"
unset expected_password_hash
test "$(stat -c '%u:%g:%a' "$recovery_home/.ssh")" = "$recovery_uid:$recovery_gid:700"
test "$(stat -c '%u:%g:%a' "$authorized_keys")" = "$recovery_uid:$recovery_gid:600"
printf '%s' '__AUTHORIZED_KEY_B64__' | base64 -d | cmp -s - "$authorized_keys"
ssh_config=/etc/ssh/sshd_config.d/00-vms-pilot-recovery.conf
legacy_ssh_config=/etc/ssh/sshd_config.d/90-vms-pilot-recovery.conf
ssh_config_tmp="$state_dir/sshd_config.tmp"
printf '%s' '__SSH_CONFIG_B64__' | base64 -d > "$ssh_config_tmp"
install -m 0600 -o root -g root "$ssh_config_tmp" "$ssh_config"
rm -f "$ssh_config_tmp"
rm -f "$legacy_ssh_config"
sshd -t
effective_ssh=$(sshd -T -C "user=$recovery_user,host=localhost,addr=127.0.0.1")
printf '%s\\n' "$effective_ssh" | grep -Fqx 'permitrootlogin no'
printf '%s\\n' "$effective_ssh" | grep -Fqx 'pubkeyauthentication yes'
printf '%s\\n' "$effective_ssh" | grep -Fqx 'passwordauthentication no'
printf '%s\\n' "$effective_ssh" | grep -Fqx 'kbdinteractiveauthentication no'
printf '%s\\n' "$effective_ssh" | grep -Fqx 'authenticationmethods publickey'
printf '%s\\n' "$effective_ssh" | grep -Fqx 'authorizedkeysfile .ssh/authorized_keys'
printf '%s\\n' "$effective_ssh" | grep -Fqx 'usepam yes'
printf '%s\\n' "$effective_ssh" | grep -Fqx 'allowusers vms-recovery'
unset effective_ssh
if ! systemctl is-enabled --quiet ssh.service; then
    timeout 90 systemctl enable ssh.service
fi
timeout 90 systemctl restart ssh.service
systemctl is-active --quiet ssh.service
boot_config=/boot/firmware/config.txt
if [ ! -f "$boot_config" ]; then boot_config=/boot/config.txt; fi
test -f "$boot_config"
grep -Eq '^[[:space:]]*enable_uart=1([[:space:]]|$)' "$boot_config" || printf '\\nenable_uart=1\\n' >> "$boot_config"
grep -Eq '^[[:space:]]*dtoverlay=dwc2([,[:space:]]|$)' "$boot_config" || printf 'dtoverlay=dwc2,dr_mode=peripheral\\n' >> "$boot_config"
boot_cmdline=/boot/firmware/cmdline.txt
if [ ! -f "$boot_cmdline" ]; then boot_cmdline=/boot/cmdline.txt; fi
test -f "$boot_cmdline"
current_cmdline=$(cat "$boot_cmdline")
filtered_cmdline=
for argument in $current_cmdline; do
    case "$argument" in
        quiet|splash|plymouth.ignore-serial-consoles|console=serial0,*|systemd.log_level=*) continue ;;
    esac
    filtered_cmdline="${filtered_cmdline:+$filtered_cmdline }$argument"
done
for argument in console=serial0,115200 loglevel=7 systemd.show_status=1; do
    case " $filtered_cmdline " in
        *" $argument "*) ;;
        *) filtered_cmdline="$filtered_cmdline $argument" ;;
    esac
done
printf '%s\\n' "$filtered_cmdline" > "$boot_cmdline"
# The kernel console causes systemd's generator to own the concrete UART getty.
# An explicit serial0 instance creates a second getty for the same hardware.
systemctl disable --now serial-getty@serial0.service 2>/dev/null || true
test -x /usr/local/libexec/vms-usb-recovery
test -s /etc/systemd/system/vms-usb-recovery.service
timeout 30 systemctl enable vms-usb-recovery.service
udc=
remaining=30
while [ "$remaining" -gt 0 ]; do
    udc=$(find /sys/class/udc -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)
    if [ -n "$udc" ]; then break; fi
    remaining=$((remaining - 1))
    sleep 1
done
if [ -z "$udc" ]; then
    recovery_reboot="$state_dir/pilot-recovery-reboot-requested"
    if [ ! -e "$recovery_reboot" ]; then
        : > "$recovery_reboot"
        write_status '{"schema_version":1,"state":"restarting_for_usb_recovery"}'
        sync
        systemctl --no-block reboot
        exit 0
    fi
    exit 1
fi
timeout 30 systemctl start vms-usb-recovery.service
systemctl is-active --quiet vms-usb-recovery.service
remaining=30
while [ "$remaining" -gt 0 ] && [ ! -c /dev/ttyGS0 ]; do
    remaining=$((remaining - 1))
    sleep 1
done
test -c /dev/ttyGS0
timeout 30 systemctl start serial-getty@ttyGS0.service
systemctl is-active --quiet serial-getty@ttyGS0.service
rm -f "$state_dir/pilot-recovery-reboot-requested"
"""
        recovery_steps = (
            recovery_steps.replace("__PASSWORD_RECORD_B64__", password_record_b64)
            .replace("__AUTHORIZED_KEY_B64__", authorized_key_b64)
            .replace("__SSH_CONFIG_B64__", ssh_config_b64)
        )
        final_recovery_checks = """systemctl is-active --quiet ssh.service
systemctl is-active --quiet vms-usb-recovery.service
test -c /dev/ttyGS0
systemctl is-active --quiet serial-getty@ttyGS0.service
"""
    else:
        recovery_steps = """timeout 30 systemctl disable --now ssh.service ssh.socket >/dev/null 2>&1 || true
if systemctl is-active --quiet ssh.service || systemctl is-active --quiet ssh.socket; then
    exit 1
fi
"""
        final_recovery_checks = """if systemctl is-active --quiet ssh.service || systemctl is-active --quiet ssh.socket; then
    exit 1
fi
"""

    cloud_cleanup_steps = ""
    provisioning_stage_check = ""
    if not runtime_preinstalled:
        cloud_cleanup_steps = """rm -f /boot/firmware/user-data /boot/firmware/network-config /boot/user-data /boot/network-config
find /var/lib/cloud/instances -mindepth 2 -maxdepth 2 -type f \\
    \\( -name user-data.txt -o -name user-data.txt.i -o -name network-config.json \\) \\
    -delete 2>/dev/null || true
rm -f /var/lib/cloud/instance/user-data.txt /var/lib/cloud/instance/user-data.txt.i /var/lib/cloud/instance/network-config.json
for cloud_log in /var/log/cloud-init.log /var/log/cloud-init-output.log; do
    if [ -f "$cloud_log" ]; then : > "$cloud_log"; fi
done
"""
    else:
        provisioning_stage_check = """systemctl is-active --quiet cloud-init-network.service
test "$(systemctl show --property=Result --value cloud-init-network.service)" = success
"""

    return f"""#!/bin/sh
set -eu
umask 077
state_dir=/var/lib/vms
marker="$state_dir/first-boot.json"
status="$state_dir/first-boot-status.json"
install -d -m 700 "$state_dir"
is_ready() {{
    test -f "$marker" &&
        test ! -L "$marker" &&
        test "$(cat "$marker")" = '{{"schema_version":1,"state":"ready"}}'
}}
if is_ready; then exit 0; fi
if [ -e "$marker" ] || [ -L "$marker" ]; then exit 1; fi
write_status() {{
    temporary="$status.tmp"
    printf '%s\\n' "$1" > "$temporary"
    mv -f "$temporary" "$status"
}}
on_exit() {{
    code=$?
    trap - EXIT
    if [ "$code" -ne 0 ]; then
        write_status "{{\\\"schema_version\\\":1,\\\"state\\\":\\\"failed\\\",\\\"exit_code\\\":$code}}"
    fi
    exit "$code"
}}
trap on_exit EXIT
write_status '{{"schema_version":1,"state":"provisioning"}}'
{provisioning_stage_check}systemctl mask userconfig.service
install -d -m 700 /var/log/vms
systemctl restart systemd-journald.service
raspi-config nonint do_wifi_country {wifi_country}
{recovery_steps}{role_steps}
{final_recovery_checks}{final_role_checks}
{cloud_cleanup_steps}
rm -f "$status"
temporary="$marker.tmp"
rm -f "$temporary"
printf '%s\\n' '{{"schema_version":1,"state":"ready"}}' > "$temporary"
chmod 600 "$temporary"
sync
mv -f "$temporary" "$marker"
sync
trap - EXIT
systemctl disable vms-first-boot.service >/dev/null 2>&1 || true
"""


def _render_usb_recovery_script() -> str:
    return """#!/bin/sh
set -eu
modprobe libcomposite
mountpoint -q /sys/kernel/config || mount -t configfs configfs /sys/kernel/config
gadget=/sys/kernel/config/usb_gadget/vms-pilot-recovery
mkdir -p "$gadget"
printf '0x1d6b\\n' > "$gadget/idVendor"
printf '0x0104\\n' > "$gadget/idProduct"
mkdir -p "$gadget/strings/0x409"
tr -d '\\n' < /etc/machine-id > "$gadget/strings/0x409/serialnumber"
printf 'Video Monitoring System\\n' > "$gadget/strings/0x409/manufacturer"
printf 'Pilot Recovery Console\\n' > "$gadget/strings/0x409/product"
mkdir -p "$gadget/configs/c.1/strings/0x409"
printf 'USB serial recovery\\n' > "$gadget/configs/c.1/strings/0x409/configuration"
mkdir -p "$gadget/functions/acm.usb0"
if [ ! -e "$gadget/configs/c.1/acm.usb0" ]; then
    ln -s "$gadget/functions/acm.usb0" "$gadget/configs/c.1/acm.usb0"
fi
if [ ! -s "$gadget/UDC" ]; then
    udc=$(find /sys/class/udc -mindepth 1 -maxdepth 1 -printf '%f\\n' | head -n 1)
    test -n "$udc"
    printf '%s\\n' "$udc" > "$gadget/UDC"
fi
systemctl --no-block start serial-getty@ttyGS0.service
"""


def _render_pilot_diagnostic_script() -> str:
    """Render a redacted local report usable through any recovery console."""

    return """#!/bin/sh
set -u
printf '%s\n' 'VMS pilot diagnostics'
printf '%s\n' '--- provisioning state ---'
for status in /var/lib/vms/first-boot-status.json /var/lib/vms/first-boot.json; do
    if [ -f "$status" ]; then cat "$status"; fi
done
printf '%s\n' '--- devices ---'
ip -brief address 2>/dev/null || true
nmcli -t -f DEVICE,TYPE,STATE device status 2>/dev/null || true
printf '%s\n' '--- power ---'
vcgencmd get_throttled 2>/dev/null || true
printf '%s\n' '--- failed services ---'
systemctl --no-pager --failed 2>/dev/null || true
printf '%s\n' '--- VMS journal ---'
journalctl --no-pager -b -u vms-first-boot.service -u vms-usb-recovery.service -u vms-camera.service -u NetworkManager.service 2>/dev/null || true
printf '%s\n' '--- recent kernel journal ---'
journalctl --no-pager -b -k -n 200 2>/dev/null || true
"""


def render_user_data(
    identity: DeviceIdentity, kit: KitIdentity | None = None
) -> str:
    """Render secret-bearing cloud-init data without plaintext login passwords.

    Wi-Fi and device credentials necessarily transit the removable boot media
    and cloud-init cache until the first-boot transaction succeeds.  Callers
    must treat this whole value as secret and must not log it.
    """

    if kit is None:
        raise ValueError("device provisioning requires its kit identity")
    _required_wifi_country(kit)
    document = json.dumps(
        _private_device_document(identity, kit),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.b64encode(document).decode("ascii")
    hostname = (
        f"camera-{identity.camera_number:03d}"
        if identity.camera_number is not None
        else "osecameras"
    )
    pilot_recovery = identity.pilot_recovery_secret is not None
    runtime_preinstalled = (
        (
            identity.role == "camera"
            and identity.image_reference == PREINSTALLED_CAMERA_IMAGE_REFERENCE
        )
        or (
            identity.role == "recorder"
            and identity.image_reference == PREINSTALLED_RECORDER_IMAGE_REFERENCE
        )
    )
    service_user = "vms" if identity.role == "recorder" else "vms-device"
    device_owner = "root:vms" if identity.role == "recorder" else "root:root"
    device_permissions = "0640" if identity.role == "recorder" else "0600"
    lines = [
        "#cloud-config",
        f"hostname: {json.dumps(hostname)}",
        "manage_etc_hosts: true",
        "ssh_pwauth: false",
        "disable_root: true",
    ]
    if pilot_recovery:
        lines.extend(
            [
                "rpi:",
                "  interfaces:",
                "    serial: true",
            ]
        )
    lines.extend(
        [
            "users:",
            f"  - name: {service_user}",
            "    system: true",
            "    no_create_home: true",
            "    shell: /usr/sbin/nologin",
            "    lock_passwd: true",
        ]
    )
    if pilot_recovery:
        password_hash = _pilot_recovery_password_hash(identity)
        authorized_key = pilot_recovery_ssh_authorized_key(identity)
        lines.extend(
            [
                f"  - name: {PILOT_RECOVERY_USERNAME}",
                "    gecos: VMS pilot recovery",
                "    groups: [adm, sudo]",
                "    shell: /bin/bash",
                "    lock_passwd: false",
                f"    passwd: {json.dumps(password_hash)}",
                "    ssh_authorized_keys:",
                f"      - {json.dumps(authorized_key)}",
            ]
        )
    if not runtime_preinstalled:
        lines.extend(["bootcmd:"])
        lines.append(
            "  - [sh, -c, 'systemctl mask --runtime --now userconfig.service 2>/dev/null || true']"
        )
        if pilot_recovery:
            lines.append("  - [install, -d, -m, '2750', /var/log/journal]")
        else:
            lines.append(
                "  - [sh, -c, 'systemctl disable --now ssh.service ssh.socket 2>/dev/null || true']"
            )
    lines.extend(
        [
            "write_files:",
            "  - path: /etc/vms/device.json",
            f"    owner: {device_owner}",
            f"    permissions: '{device_permissions}'",
            "    encoding: b64",
            f"    content: {encoded}",
        ]
    )
    if identity.role == "camera":
        network_manager = render_networkmanager_connection(identity, kit)
        network_manager_name = f"vms-home-{identity.camera_number:03d}.nmconnection"
        lines.extend(
            [
                f"  - path: /etc/NetworkManager/system-connections/{network_manager_name}",
                "    owner: root:root",
                "    permissions: '0600'",
                "    encoding: b64",
                f"    content: {base64.b64encode(network_manager.encode('utf-8')).decode('ascii')}",
            ]
        )
    if identity.role == "camera" and not runtime_preinstalled:
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
        runtime_source = files("video_monitoring_system.device").joinpath(
            "runtime_metrics.py"
        ).read_bytes()
        journal_reader_source = files("video_monitoring_system.device").joinpath(
            "journal_reader.py"
        ).read_bytes()

        lines.extend(
            [
                "  - path: /opt/vms/camera_agent.py",
                "    owner: root:root",
                "    permissions: '0755'",
                "    encoding: b64",
                f"    content: {base64.b64encode(camera_source.encode('utf-8')).decode('ascii')}",
                "  - path: /opt/vms/journal_reader.py",
                "    owner: root:root",
                "    permissions: '0755'",
                "    encoding: b64",
                f"    content: {base64.b64encode(journal_reader_source).decode('ascii')}",
                "  - path: /opt/vms/runtime_metrics.py",
                "    owner: root:root",
                "    permissions: '0755'",
                "    encoding: b64",
                f"    content: {base64.b64encode(runtime_source).decode('ascii')}",
                "  - path: /opt/vms/network_recovery.py",
                "    owner: root:root",
                "    permissions: '0755'",
                "    encoding: b64",
                f"    content: {base64.b64encode(network_recovery_source.encode('utf-8')).decode('ascii')}",
                "  - path: /etc/systemd/system/vms-camera.service",
                "    owner: root:root",
                "    permissions: '0644'",
                "    encoding: b64",
                f"    content: {base64.b64encode(CAMERA_SERVICE.encode('utf-8')).decode('ascii')}",
                "  - path: /etc/systemd/system/vms-network-recovery.service",
                "    owner: root:root",
                "    permissions: '0644'",
                "    encoding: b64",
                f"    content: {base64.b64encode(NETWORK_RECOVERY_SERVICE.encode('utf-8')).decode('ascii')}",
                "  - path: /etc/systemd/system/vms-network-recovery.timer",
                "    owner: root:root",
                "    permissions: '0644'",
                "    encoding: b64",
                f"    content: {base64.b64encode(NETWORK_RECOVERY_TIMER.encode('utf-8')).decode('ascii')}",
            ]
        )
    if pilot_recovery and not runtime_preinstalled:
        usb_script = PILOT_USB_RECOVERY_SCRIPT
        diagnostic_script = PILOT_DIAGNOSTICS_SCRIPT
        lines.extend(
            [
                "  - path: /usr/local/libexec/vms-usb-recovery",
                "    owner: root:root",
                "    permissions: '0700'",
                "    encoding: b64",
                f"    content: {base64.b64encode(usb_script.encode('utf-8')).decode('ascii')}",
                "  - path: /etc/systemd/system/vms-usb-recovery.service",
                "    owner: root:root",
                "    permissions: '0644'",
                "    encoding: b64",
                f"    content: {base64.b64encode(PILOT_USB_RECOVERY_SERVICE.encode('utf-8')).decode('ascii')}",
                "  - path: /usr/local/bin/vms-pilot-diagnostics",
                "    owner: root:root",
                "    permissions: '0755'",
                "    encoding: b64",
                f"    content: {base64.b64encode(diagnostic_script.encode('utf-8')).decode('ascii')}",
                "  - path: /etc/systemd/journald.conf.d/90-vms-pilot-diagnostics.conf",
                "    owner: root:root",
                "    permissions: '0644'",
                "    content: |",
                "      [Journal]",
                "      Storage=persistent",
                "      Compress=yes",
                "      SystemMaxUse=128M",
                "      MaxRetentionSec=30day",
                "      MaxLevelStore=debug",
                "  - path: /etc/ssh/sshd_config.d/00-vms-pilot-recovery.conf",
                "    owner: root:root",
                "    permissions: '0600'",
                "    content: |",
                *(f"      {line}" for line in PILOT_SSH_CONFIG.splitlines()),
            ]
        )
    first_boot_script = _render_first_boot_script(
        identity,
        kit,
        runtime_preinstalled=runtime_preinstalled,
    )
    # The baked dispatcher discovers these files after cloud-init-local, while
    # Raspberry Pi OS executes inline write_files in cloud-init-network.  With
    # no bootcmd or remote write source, that stage does not await connectivity.
    first_boot_after = (
        "NetworkManager.service cloud-init-network.service"
        if runtime_preinstalled
        else "NetworkManager.service cloud-config.service"
    )
    first_boot_requires = (
        "Requires=cloud-init-network.service\n"
        if runtime_preinstalled
        else ""
    )
    first_boot_timeout = 600 if runtime_preinstalled else 2400
    first_boot_service = f"""[Unit]
Description=VMS first-boot provisioning transaction
After={first_boot_after}
Wants=NetworkManager.service
{first_boot_requires}ConditionPathExists=!/var/lib/vms/first-boot.json

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/vms-first-boot
TimeoutStartSec={first_boot_timeout}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=vms-first-boot

[Install]
WantedBy=multi-user.target
"""
    lines.extend(
        [
            "  - path: /usr/local/libexec/vms-first-boot",
            "    owner: root:root",
            "    permissions: '0700'",
            "    encoding: b64",
            f"    content: {base64.b64encode(first_boot_script.encode('utf-8')).decode('ascii')}",
            "  - path: /etc/systemd/system/vms-first-boot.service",
            "    owner: root:root",
            "    permissions: '0644'",
            "    encoding: b64",
            f"    content: {base64.b64encode(first_boot_service.encode('utf-8')).decode('ascii')}",
        ]
    )
    if not runtime_preinstalled:
        lines.extend(
            [
                "runcmd:",
                "  - [systemctl, daemon-reload]",
                *(
                    [
                        "  - [systemctl, restart, systemd-journald.service]",
                        "  - [systemctl, enable, --now, ssh.service]",
                    ]
                    if pilot_recovery
                    else []
                ),
                "  - [systemctl, enable, vms-first-boot.service]",
                "  - [systemctl, start, vms-first-boot.service]",
            ]
        )
    return "\n".join(lines) + "\n"


def render_network_config(identity: DeviceIdentity, kit: KitIdentity) -> str:
    """Render cloud-init network-config for the device's product role."""

    wifi_country = _required_wifi_country(kit)
    if identity.role == "recorder":
        return (
            "version: 2\n"
            "renderer: NetworkManager\n"
            "ethernets:\n"
            "  eth0:\n"
            "    dhcp4: true\n"
            "    optional: true\n"
        )
    # Raspberry Pi OS logs cloud-init's network mapping at debug level. Keep
    # the password out of that channel: user-data installs an owner-private
    # NetworkManager keyfile, and the first-boot transaction sets the country
    # before activating that connection.
    return "version: 2\nrenderer: NetworkManager\nwifis: {}\n"


def camera_dhcp_client_id(identity: DeviceIdentity) -> str:
    """Return routing metadata that follows a camera card between boards.

    DHCP uses this non-secret client identifier instead of the board MAC.  It
    is only a stable address-allocation hint; application authentication still
    uses the camera's private identity.
    """

    if (
        identity.role != "camera"
        or _CAMERA_DEVICE_ID.fullmatch(identity.device_id) is None
    ):
        raise ValueError("a valid camera identity is required")
    value = f"vms-{identity.device_id}".encode("ascii")
    return "00:" + ":".join(f"{byte:02x}" for byte in value)


def render_networkmanager_connection(
    identity: DeviceIdentity, kit: KitIdentity
) -> str:
    """Render a NetworkManager keyfile for one device.

    Injected network values are validated when the job is created, so values
    cannot introduce additional keyfile fields.
    """

    _required_wifi_country(kit)
    if identity.role == "recorder":
        return (
            "[connection]\n"
            "id=vms-recorder\n"
            "type=ethernet\n"
            "interface-name=eth0\n"
            "autoconnect=true\n"
            "\n"
            "[ipv4]\n"
            "method=auto\n"
            "\n"
            "[ipv6]\n"
            "method=auto\n"
        )
    return (
        "[connection]\n"
        f"id=vms-home-{identity.camera_number:03d}\n"
        "type=wifi\n"
        "interface-name=wlan0\n"
        "autoconnect=false\n"
        "\n"
        "[wifi]\n"
        "mode=infrastructure\n"
        "band=bg\n"
        # Zero W power saving can make an infrastructure connection disappear
        # while the camera is still running.  Cameras are mains-powered, so
        # favor a stable control/video link over negligible radio savings.
        "powersave=2\n"
        f"ssid={kit.wifi_ssid}\n"
        "\n"
        "[wifi-security]\n"
        "key-mgmt=wpa-psk\n"
        "proto=rsn;\n"
        "pairwise=ccmp;\n"
        "group=ccmp;\n"
        f"psk={kit.wifi_passphrase}\n"
        "\n"
        "[ipv4]\n"
        f"dhcp-client-id={camera_dhcp_client_id(identity)}\n"
        "method=auto\n"
        "\n"
        "[ipv6]\n"
        "method=auto\n"
    )


def _validate_wifi_credentials(ssid: str, passphrase: str) -> None:
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise ValueError("wifi_ssid must contain between 1 and 32 UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in ssid):
        raise ValueError("wifi_ssid must not contain control characters")
    if not 8 <= len(passphrase) <= 63:
        raise ValueError("wifi_passphrase must contain between 8 and 63 characters")
    if any(ord(character) < 32 or ord(character) > 126 for character in passphrase):
        raise ValueError("wifi_passphrase must contain printable ASCII characters")


@dataclass(frozen=True, slots=True)
class ProvisioningJob:
    """Persistable private job state used to make retries idempotent."""

    spec: ProvisioningSpec
    identity: KitIdentity

    @classmethod
    def create(
        cls,
        spec: ProvisioningSpec,
        key_generator: KeyGenerator | None = None,
        wifi_ssid: str | None = None,
        wifi_passphrase: str | None = None,
        wifi_country: str | None = None,
        pilot_recovery: bool | None = None,
    ) -> ProvisioningJob:
        if wifi_country is None:
            raise ValueError("wifi_country is required")
        wifi_country = validate_wifi_country(wifi_country)
        if (wifi_ssid is None) != (wifi_passphrase is None):
            raise ValueError("wifi_ssid and wifi_passphrase must be supplied together")
        if wifi_ssid is not None and wifi_passphrase is not None:
            _validate_wifi_credentials(wifi_ssid, wifi_passphrase)
        camera_profile = get_board_profile(spec.camera_board)
        if pilot_recovery is None:
            pilot_recovery = camera_profile.release_tier == "pilot"
        if pilot_recovery and camera_profile.release_tier != "pilot":
            raise ValueError("pilot recovery is available only for pilot-tier hardware")
        if key_generator is None:
            from .keys import preferred_key_generator

            generator = preferred_key_generator()
        else:
            generator = key_generator
        kit_id = _random_id("KIT", 6)

        def device(
            role: str,
            board: str,
            image: str,
            prefix: str,
            camera_number: int | None = None,
        ) -> DeviceIdentity:
            return DeviceIdentity(
                device_id=_random_id(prefix),
                kit_id=kit_id,
                role=role,
                board=board,
                image_reference=image,
                key=generator.generate(),
                # 128 bits, rendered as eight four-character groups on the
                # physical label so owners can type it when scanning fails.
                claim_secret=secrets.token_hex(16).upper() if role == "camera" else None,
                stream_secret=secrets.token_urlsafe(32) if role == "camera" else None,
                camera_number=camera_number,
                pilot_recovery_secret=(
                    secrets.token_urlsafe(32) if pilot_recovery else None
                ),
            )

        recorder = device("recorder", spec.recorder_board, spec.recorder_image, "REC")
        cameras = tuple(
            device("camera", spec.camera_board, spec.camera_image, "CAM", number)
            for number in range(1, spec.camera_count + 1)
        )
        generated_ssid = f"VMS-{kit_id.removeprefix('KIT-')}"
        return cls(
            spec=spec,
            identity=KitIdentity(
                kit_id=kit_id,
                wifi_ssid=wifi_ssid or generated_ssid,
                wifi_passphrase=wifi_passphrase or secrets.token_urlsafe(24),
                wifi_country=wifi_country,
                recorder=recorder,
                cameras=cameras,
            ),
        )

    @classmethod
    def open_or_create(
        cls,
        path: str | os.PathLike[str],
        spec: ProvisioningSpec,
        key_generator: KeyGenerator | None = None,
        wifi_ssid: str | None = None,
        wifi_passphrase: str | None = None,
        wifi_country: str | None = None,
        rotate_network: bool = False,
        pilot_recovery: bool | None = None,
    ) -> ProvisioningJob:
        """Load an existing job or atomically persist a newly created one."""

        if wifi_country is None:
            raise ValueError("wifi_country is required")
        wifi_country = validate_wifi_country(wifi_country)
        if (wifi_ssid is None) != (wifi_passphrase is None):
            raise ValueError("wifi_ssid and wifi_passphrase must be supplied together")
        if wifi_ssid is not None and wifi_passphrase is not None:
            _validate_wifi_credentials(wifi_ssid, wifi_passphrase)
        if rotate_network and (wifi_ssid is None or wifi_passphrase is None):
            raise ValueError("network rotation requires an SSID and passphrase")
        camera_profile = get_board_profile(spec.camera_board)
        if pilot_recovery is None:
            pilot_recovery = camera_profile.release_tier == "pilot"
        if pilot_recovery and camera_profile.release_tier != "pilot":
            raise ValueError("pilot recovery is available only for pilot-tier hardware")
        state_path = Path(path)
        if state_path.exists():
            job = cls.from_json(state_path.read_text(encoding="utf-8"))
            if job.spec != spec:
                raise ValueError("persisted provisioning job does not match the request")
            network_changed = (
                (wifi_ssid is not None and job.identity.wifi_ssid != wifi_ssid)
                or (
                    wifi_passphrase is not None
                    and job.identity.wifi_passphrase != wifi_passphrase
                )
                or (
                    job.identity.wifi_country is not None
                    and job.identity.wifi_country != wifi_country
                )
            )
            if network_changed and not rotate_network:
                if (
                    job.identity.wifi_country is not None
                    and job.identity.wifi_country != wifi_country
                ):
                    raise ValueError(
                        "persisted provisioning job uses a different wifi_country"
                    )
                if wifi_ssid is not None and job.identity.wifi_ssid != wifi_ssid:
                    raise ValueError(
                        "persisted provisioning job uses a different wifi_ssid"
                    )
                raise ValueError(
                    "persisted provisioning job uses a different wifi_passphrase"
                )
            changed = False
            if network_changed:
                job = replace(
                    job,
                    identity=replace(
                        job.identity,
                        wifi_ssid=wifi_ssid,
                        wifi_passphrase=wifi_passphrase,
                        wifi_country=wifi_country,
                    ),
                )
                changed = True
            if job.identity.wifi_country is None:
                # Schema 1 did not record a regulatory domain.  Upgrade only
                # after every retry input matches, retaining all identities,
                # claim codes, stream credentials, and network credentials.
                job = replace(
                    job,
                    identity=replace(job.identity, wifi_country=wifi_country),
                )
                changed = True
            if pilot_recovery:
                recovered_recorder = (
                    job.identity.recorder
                    if job.identity.recorder.pilot_recovery_secret is not None
                    else replace(
                        job.identity.recorder,
                        pilot_recovery_secret=secrets.token_urlsafe(32),
                    )
                )
                recovered_cameras = tuple(
                    camera
                    if camera.pilot_recovery_secret is not None
                    else replace(
                        camera,
                        pilot_recovery_secret=secrets.token_urlsafe(32),
                    )
                    for camera in job.identity.cameras
                )
                if (
                    recovered_recorder != job.identity.recorder
                    or recovered_cameras != job.identity.cameras
                ):
                    job = replace(
                        job,
                        identity=replace(
                            job.identity,
                            recorder=recovered_recorder,
                            cameras=recovered_cameras,
                        ),
                    )
                    changed = True
            if changed:
                # Identity, recovery, and network updates become visible as one
                # owner-private atomic replacement.  A failed write leaves the
                # previous job intact and usable.
                secure_write_text(state_path, job.to_private_json())
            return job
        job = cls.create(
            spec,
            key_generator,
            wifi_ssid,
            wifi_passphrase,
            wifi_country,
            pilot_recovery,
        )
        secure_write_text(state_path, job.to_private_json())
        return job

    def public_manifest(self) -> KitManifest:
        return self.identity.public_manifest()

    def image_assignments(self) -> tuple[ImageAssignment, ...]:
        assignments: dict[str, list[str]] = {}
        devices = (self.identity.recorder, *self.identity.cameras)
        for device in devices:
            assignments.setdefault(device.image_reference, []).append(device.device_id)
        return tuple(
            ImageAssignment(image, tuple(device_ids))
            for image, device_ids in assignments.items()
        )

    def device_configurations(self) -> tuple[DeviceConfiguration, ...]:
        devices = (self.identity.recorder, *self.identity.cameras)
        return tuple(
            DeviceConfiguration(
                device_id=device.device_id,
                image_reference=device.image_reference,
                user_data=render_user_data(device, self.identity),
                network_config=render_network_config(device, self.identity),
                network_manager=render_networkmanager_connection(
                    device, self.identity
                ),
                boot_customization=pilot_recovery_boot_customization(device),
            )
            for device in devices
        )

    def to_private_json(self) -> str:
        wifi_country = _required_wifi_country(self.identity)

        def identity_dict(device: DeviceIdentity) -> dict[str, object]:
            return {
                "device_id": device.device_id,
                "kit_id": device.kit_id,
                "role": device.role,
                "board": device.board,
                "image_reference": device.image_reference,
                "key": asdict(device.key),
                "claim_secret": device.claim_secret,
                "stream_secret": device.stream_secret,
                "camera_number": device.camera_number,
                "pilot_recovery_secret": device.pilot_recovery_secret,
            }

        state = {
            "schema_version": 3,
            "spec": asdict(self.spec),
            "identity": {
                "kit_id": self.identity.kit_id,
                "wifi_ssid": self.identity.wifi_ssid,
                "wifi_passphrase": self.identity.wifi_passphrase,
                "wifi_country": wifi_country,
                "recorder": identity_dict(self.identity.recorder),
                "cameras": [identity_dict(camera) for camera in self.identity.cameras],
            },
        }
        return json.dumps(state, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, content: str) -> ProvisioningJob:
        raw = json.loads(content)
        schema_version = raw.get("schema_version")
        if schema_version not in (1, 2, 3):
            raise ValueError("unsupported provisioning state schema")
        spec = ProvisioningSpec(**raw["spec"])

        def device(
            raw_device: Mapping[str, object], inferred_camera_number: int | None = None
        ) -> DeviceIdentity:
            raw_key = raw_device["key"]
            if not isinstance(raw_key, dict):
                raise ValueError("invalid device key state")
            return DeviceIdentity(
                device_id=str(raw_device["device_id"]),
                kit_id=str(raw_device["kit_id"]),
                role=str(raw_device["role"]),
                board=str(raw_device["board"]),
                image_reference=str(raw_device["image_reference"]),
                key=KeyMaterial(
                    algorithm=str(raw_key["algorithm"]),
                    public_identity=str(raw_key["public_identity"]),
                    private_identity=str(raw_key["private_identity"]),
                ),
                claim_secret=(
                    None
                    if raw_device.get("claim_secret") is None
                    else str(raw_device["claim_secret"])
                ),
                stream_secret=(
                    None
                    if raw_device.get("stream_secret") is None
                    else str(raw_device["stream_secret"])
                ),
                camera_number=(
                    inferred_camera_number
                    if raw_device.get("camera_number") is None
                    else int(raw_device["camera_number"])
                ),
                pilot_recovery_secret=(
                    None
                    if raw_device.get("pilot_recovery_secret") is None
                    else str(raw_device["pilot_recovery_secret"])
                ),
            )

        raw_identity = raw["identity"]
        if not isinstance(raw_identity, dict):
            raise ValueError("invalid kit identity state")
        wifi_ssid = raw_identity["wifi_ssid"]
        wifi_passphrase = raw_identity["wifi_passphrase"]
        if not isinstance(wifi_ssid, str) or not isinstance(wifi_passphrase, str):
            raise ValueError("invalid persisted Wi-Fi credentials")
        _validate_wifi_credentials(wifi_ssid, wifi_passphrase)
        raw_wifi_country = raw_identity.get("wifi_country")
        if schema_version in (2, 3) and raw_wifi_country is None:
            raise ValueError("persisted Wi-Fi country is missing")
        wifi_country = (
            None
            if raw_wifi_country is None
            else validate_wifi_country(raw_wifi_country)
        )
        identity = KitIdentity(
            kit_id=str(raw_identity["kit_id"]),
            wifi_ssid=wifi_ssid,
            wifi_passphrase=wifi_passphrase,
            wifi_country=wifi_country,
            recorder=device(raw_identity["recorder"]),
            cameras=tuple(
                device(item, index)
                for index, item in enumerate(raw_identity["cameras"], start=1)
            ),
        )
        if len(identity.cameras) != spec.camera_count:
            raise ValueError("camera count does not match persisted specification")
        for camera in identity.cameras:
            recovery = camera.pilot_recovery_secret
            if recovery is not None and (
                get_board_profile(spec.camera_board).release_tier != "pilot"
                or len(recovery) < 32
                or any(ord(character) < 33 or ord(character) > 126 for character in recovery)
            ):
                raise ValueError("invalid persisted pilot recovery credential")
        all_devices = (identity.recorder, *identity.cameras)
        if any(item.kit_id != identity.kit_id for item in all_devices):
            raise ValueError("device belongs to a different kit")
        if len({item.device_id for item in all_devices}) != len(all_devices):
            raise ValueError("persisted device identifiers are not unique")
        if (
            identity.recorder.role != "recorder"
            or identity.recorder.board != spec.recorder_board
            or identity.recorder.image_reference != spec.recorder_image
            or identity.recorder.camera_number is not None
            or any(
                camera.role != "camera"
                or camera.board != spec.camera_board
                or camera.image_reference != spec.camera_image
                or not camera.claim_secret
                or not camera.stream_secret
                or camera.stream_secret == camera.claim_secret
                or camera.stream_secret == camera.key.private_identity
                or camera.camera_number != number
                for number, camera in enumerate(identity.cameras, start=1)
            )
        ):
            raise ValueError("persisted devices do not match the specification")
        return cls(spec=spec, identity=identity)


def secure_write_text(path: Path, content: str) -> None:
    """Atomically write sensitive text with owner-only file permissions."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            os.chmod(temporary_path, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def temporary_secret_file(
    content: str, directory: str | os.PathLike[str] | None = None
) -> Iterator[Path]:
    """Create an owner-only temporary file and remove it after use."""

    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=directory,
            prefix="vms-provision-",
            delete=False,
        ) as stream:
            path = Path(stream.name)
            os.chmod(path, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        yield path
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
