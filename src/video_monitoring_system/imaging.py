"""Safe, platform-neutral Raspberry Pi media imaging primitives.

Operating-system-specific disk enumeration intentionally lives outside this
module.  A caller supplies a :class:`DiskDiscovery` implementation, and this
module rechecks its result immediately before invoking Raspberry Pi Imager.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Protocol, Sequence

if TYPE_CHECKING:
    from .provisioning import BootCustomization


_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ROLES = frozenset({"camera", "recorder"})
_CHANNELS = frozenset({"development", "product"})
_REDACTED_USER_DATA = "<owner-private-user-data>"
_REDACTED_NETWORK_CONFIG = "<owner-private-network-config>"


class ImagingError(RuntimeError):
    """Base class for errors raised by this module."""


class ImagerNotFoundError(ImagingError):
    """Raspberry Pi Imager is not available on the command search path."""


class UnsafeDiskError(ImagingError):
    """A requested write target has not been proven safe to erase."""


class ConfirmationError(ImagingError):
    """The destructive-write confirmation did not match exactly."""


class DevelopmentImageNotAllowed(ImagingError):
    """A development image was selected without an explicit opt-in."""


class ReleaseSignatureError(ImagingError):
    """A product manifest could not be authenticated."""


class UntrustedReleaseKey(ReleaseSignatureError):
    """The manifest's signing key is absent from the caller's trust store."""


class InvalidReleaseSignature(ReleaseSignatureError):
    """The manifest signature is malformed or does not authenticate its data."""


class SignatureVerificationUnavailable(ReleaseSignatureError):
    """The required Ed25519 verification implementation is unavailable."""


@dataclass(frozen=True, slots=True)
class SignatureMetadata:
    """Public metadata for an Ed25519 signature over canonical release data."""

    algorithm: str
    key_id: str
    signature: str

    def __post_init__(self) -> None:
        _require_clean_text("algorithm", self.algorithm)
        _require_clean_text("key_id", self.key_id)
        _require_clean_text("signature", self.signature)
        if self.algorithm != "ed25519":
            raise ValueError("signature algorithm must be ed25519")
        if _KEY_ID.fullmatch(self.key_id) is None:
            raise ValueError("key_id contains unsupported characters")
        if len(_decode_base64("signature", self.signature)) != 64:
            raise ValueError("Ed25519 signature must decode to exactly 64 bytes")


@dataclass(frozen=True, slots=True)
class ImageReleaseManifest:
    """A versioned, content-addressed image release record."""

    schema_version: int
    release_version: str
    role: str
    supported_boards: tuple[str, ...]
    image: str
    sha256: str
    signature: SignatureMetadata | None
    channel: str
    architecture: str
    bits: int
    released: str
    extract_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported image manifest schema_version")
        _require_clean_text("release_version", self.release_version)
        if self.role not in _ROLES:
            raise ValueError("role must be camera or recorder")
        if not isinstance(self.supported_boards, tuple) or not self.supported_boards:
            raise ValueError("supported_boards must be a non-empty tuple")
        if len(set(self.supported_boards)) != len(self.supported_boards):
            raise ValueError("supported_boards must not contain duplicates")
        for board in self.supported_boards:
            _require_clean_text("supported board", board)
        _require_clean_text("image", self.image)
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be exactly 64 lowercase hexadecimal characters")
        if (
            self.extract_sha256 is not None
            and _SHA256.fullmatch(self.extract_sha256) is None
        ):
            raise ValueError(
                "extract_sha256 must be exactly 64 lowercase hexadecimal characters"
            )
        if self.channel not in _CHANNELS:
            raise ValueError("channel must be development or product")
        _require_clean_text("architecture", self.architecture)
        if type(self.bits) is not int or self.bits not in (32, 64):
            raise ValueError("bits must be 32 or 64")
        _require_iso_date("released", self.released)
        if self.signature is not None and not isinstance(
            self.signature, SignatureMetadata
        ):
            raise TypeError("signature must be SignatureMetadata or None")
        if self.channel == "product" and self.signature is None:
            raise ValueError("product image manifests require signature metadata")

    def validate_for_use(
        self,
        *,
        role: str,
        board: str,
        allow_development: bool = False,
        trusted_release_keys: Mapping[str, str | bytes] | None = None,
    ) -> None:
        """Authenticate and validate this release before any media write.

        Product releases always require an Ed25519 signature which verifies
        against a caller-supplied trusted public key.  Development releases
        are kept on a separate channel and require a deliberate opt-in even
        when they happen to carry a signature.
        """

        if role != self.role:
            raise ValueError(
                f"image role {self.role!r} does not match requested role {role!r}"
            )
        if board not in self.supported_boards:
            raise ValueError(f"image does not support board {board!r}")
        if self.channel == "development" and not allow_development:
            raise DevelopmentImageNotAllowed(
                "development images require allow_development=True"
            )
        if self.channel == "product":
            self.verify_signature(trusted_release_keys)

    def canonical_metadata(self) -> bytes:
        """Return the deterministic UTF-8 bytes authenticated by a signature."""

        value = {
            "architecture": self.architecture,
            "bits": self.bits,
            "channel": self.channel,
            "image": self.image,
            "release_version": self.release_version,
            "released": self.released,
            "role": self.role,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "supported_boards": list(self.supported_boards),
        }
        if self.extract_sha256 is not None:
            value["extract_sha256"] = self.extract_sha256
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def verify_signature(
        self, trusted_release_keys: Mapping[str, str | bytes] | None
    ) -> None:
        """Verify the manifest against an external Ed25519 trust store."""

        if self.signature is None:
            raise InvalidReleaseSignature("release manifest is unsigned")
        if trusted_release_keys is None:
            raise UntrustedReleaseKey(
                "product releases require a trusted release key"
            )
        encoded_key = trusted_release_keys.get(self.signature.key_id)
        if encoded_key is None:
            raise UntrustedReleaseKey(
                f"release key {self.signature.key_id!r} is not trusted"
            )
        public_key_bytes = _decode_public_key(encoded_key)
        signature_bytes = _decode_base64("signature", self.signature.signature)
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except ImportError as error:  # pragma: no cover - dependency contract
            raise SignatureVerificationUnavailable(
                "Ed25519 verification requires the cryptography package"
            ) from error
        try:
            key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            key.verify(signature_bytes, self.canonical_metadata())
        except (InvalidSignature, ValueError) as error:
            raise InvalidReleaseSignature(
                "release manifest signature verification failed"
            ) from error

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ImageReleaseManifest:
        """Parse a JSON-compatible mapping and apply all manifest checks."""

        if not isinstance(value, Mapping):
            raise TypeError("release manifest must be an object")
        expected_keys = {
            "schema_version",
            "release_version",
            "role",
            "supported_boards",
            "image",
            "sha256",
            "signature",
            "channel",
            "architecture",
            "bits",
            "released",
        }
        optional_keys = {"extract_sha256"}
        unknown_keys = set(value) - expected_keys - optional_keys
        missing_keys = expected_keys - set(value)
        if unknown_keys:
            raise ValueError(
                "unsupported image manifest fields: "
                + ", ".join(sorted(unknown_keys))
            )
        if missing_keys:
            raise ValueError(
                "missing image manifest fields: " + ", ".join(sorted(missing_keys))
            )

        signature_value = value.get("signature")
        if signature_value is None:
            signature = None
        elif isinstance(signature_value, Mapping):
            signature_keys = {"algorithm", "key_id", "signature"}
            if set(signature_value) != signature_keys:
                raise ValueError(
                    "signature must contain exactly algorithm, key_id, and signature"
                )
            signature = SignatureMetadata(
                algorithm=signature_value.get("algorithm"),
                key_id=signature_value.get("key_id"),
                signature=signature_value.get("signature"),
            )
        else:
            raise TypeError("signature must be an object or null")
        boards = value.get("supported_boards")
        if not isinstance(boards, (list, tuple)):
            raise TypeError("supported_boards must be an array")
        return cls(
            schema_version=value.get("schema_version"),
            release_version=value.get("release_version"),
            role=value.get("role"),
            supported_boards=tuple(boards),
            image=value.get("image"),
            sha256=value.get("sha256"),
            signature=signature,
            channel=value.get("channel"),
            architecture=value.get("architecture"),
            bits=value.get("bits"),
            released=value.get("released"),
            extract_sha256=value.get("extract_sha256"),
        )


def load_release_manifest(path: str | os.PathLike[str]) -> ImageReleaseManifest:
    """Load and strictly validate one JSON release manifest."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("release manifest is not readable JSON") from error
    if not isinstance(value, Mapping):
        raise TypeError("release manifest root must be an object")
    return ImageReleaseManifest.from_dict(value)


@dataclass(frozen=True, slots=True)
class DiskTarget:
    """A whole-disk target returned by a trusted discovery implementation."""

    device: str
    stable_id: str
    display_name: str
    size_bytes: int
    removable: bool
    system_disk: bool

    def __post_init__(self) -> None:
        _require_clean_text("device", self.device)
        _require_clean_text("stable_id", self.stable_id)
        _require_clean_text("display_name", self.display_name)
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool):
            raise TypeError("size_bytes must be an integer")
        if self.size_bytes <= 0:
            raise ValueError("size_bytes must be positive")
        if not isinstance(self.removable, bool) or not isinstance(
            self.system_disk, bool
        ):
            raise TypeError("disk safety properties must be booleans")

    @property
    def confirmation(self) -> str:
        """The exact phrase required to authorize erasing this device."""

        return f"ERASE {self.stable_id}"


class DiskDiscovery(Protocol):
    """Injectable boundary for platform-specific whole-disk discovery."""

    def list_disks(self) -> Sequence[DiskTarget]:
        """Return current whole-disk targets and their safety properties."""


class BootCustomizer(Protocol):
    """Platform adapter that edits the readable boot partition after imaging."""

    def __call__(
        self, target: DiskTarget, customization: BootCustomization
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ImagingPlan:
    """A redacted preview safe to display or serialize in ordinary logs."""

    release_version: str
    role: str
    board: str
    channel: str
    image: str
    sha256: str
    target_device: str
    target_display_name: str
    target_size_bytes: int
    required_confirmation: str
    argv: tuple[str, ...]
    redacted: bool = True
    boot_customization: str = "none"


@dataclass(frozen=True, slots=True)
class ImagingResult:
    """Result of a completed, verified media-write command."""

    plan: ImagingPlan
    returncode: int


class _RunResult(Protocol):
    returncode: int


_Which = Callable[[str], str | None]
_Runner = Callable[..., _RunResult]


def discover_rpi_imager(
    *,
    which: _Which | None = None,
    candidates: Sequence[str | os.PathLike[str]] | None = None,
) -> str:
    """Find the official Raspberry Pi Imager executable.

    Package-manager shims are preferred. Standard application locations are
    checked as a fallback because desktop installers do not always update the
    command search path.
    """

    finder = shutil.which if which is None else which
    executable = finder("rpi-imager")
    if executable is None:
        selected_candidates = (
            _standard_imager_candidates() if candidates is None else candidates
        )
        executable = next(
            (
                os.fspath(Path(candidate))
                for candidate in selected_candidates
                if Path(candidate).is_file()
            ),
            None,
        )
    if executable is None:
        raise ImagerNotFoundError("Raspberry Pi Imager is not installed.")
    _require_clean_text("rpi-imager executable", executable)
    return executable


def _standard_imager_candidates() -> tuple[Path, ...]:
    if sys.platform == "win32":
        roots = tuple(
            Path(value)
            for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
            if (value := os.environ.get(name))
        )
        return tuple(
            path
            for root in roots
            for path in (
                root / "Raspberry Pi Ltd" / "Imager" / "rpi-imager.exe",
                root / "Raspberry Pi Imager" / "rpi-imager.exe",
            )
        )
    if sys.platform == "darwin":
        return (
            Path("/Applications/Raspberry Pi Imager.app/Contents/MacOS/rpi-imager"),
        )
    return (Path("/usr/local/bin/rpi-imager"), Path("/usr/bin/rpi-imager"))


def build_imager_argv(
    *,
    executable: str,
    manifest: ImageReleaseManifest,
    target: DiskTarget,
    user_data_path: str | os.PathLike[str],
    network_config_path: str | os.PathLike[str],
) -> tuple[str, ...]:
    """Build the verified official Imager CLI invocation as an argv tuple."""

    _require_clean_text("rpi-imager executable", executable)
    if manifest.extract_sha256 is None:
        raise ImagingError("release manifest has no extracted-image checksum")
    argv = (
        executable,
        "--cli",
        "--sha256",
        manifest.extract_sha256,
        "--cloudinit-userdata",
        os.fspath(user_data_path),
        "--cloudinit-networkconfig",
        os.fspath(network_config_path),
        manifest.image,
        target.device,
    )
    if "--disable-verify" in argv:
        raise AssertionError("image verification must never be disabled")
    return argv


def image_media(
    *,
    manifest: ImageReleaseManifest,
    role: str,
    board: str,
    target: DiskTarget,
    user_data: str,
    network_config: str,
    boot_customization: BootCustomization | None = None,
    dry_run: bool = True,
    confirmation: str | None = None,
    allow_development: bool = False,
    trusted_release_keys: Mapping[str, str | bytes] | None = None,
    disk_discovery: DiskDiscovery | None = None,
    which: _Which | None = None,
    runner: _Runner | None = None,
    boot_customizer: BootCustomizer | None = None,
) -> ImagingPlan | ImagingResult:
    """Preview or execute one verified, cloud-init-customized media write.

    Dry runs never write configuration files or inspect disks.  Real writes
    require a current discovery match, removable/non-system proof, and the
    exact confirmation phrase exposed by the returned preview.
    """

    manifest.validate_for_use(
        role=role,
        board=board,
        allow_development=allow_development,
        trusted_release_keys=trusted_release_keys,
    )
    _require_secret_document("user_data", user_data)
    _require_secret_document("network_config", network_config)
    _validate_boot_customization(boot_customization)
    executable = discover_rpi_imager(which=which)
    redacted_argv = build_imager_argv(
        executable=executable,
        manifest=manifest,
        target=target,
        user_data_path=_REDACTED_USER_DATA,
        network_config_path=_REDACTED_NETWORK_CONFIG,
    )
    plan = ImagingPlan(
        release_version=manifest.release_version,
        role=role,
        board=board,
        channel=manifest.channel,
        image=manifest.image,
        sha256=manifest.sha256,
        target_device=target.device,
        target_display_name=target.display_name,
        target_size_bytes=target.size_bytes,
        required_confirmation=target.confirmation,
        argv=redacted_argv,
        boot_customization=(
            "adapter"
            if boot_customization is not None and boot_customizer is not None
            else "first-boot-fallback"
            if boot_customization is not None
            else "none"
        ),
    )
    if dry_run:
        return plan

    if disk_discovery is None:
        raise UnsafeDiskError("actual writes require disk discovery")
    safe_target = _require_current_safe_target(target, disk_discovery)
    if confirmation != safe_target.confirmation:
        raise ConfirmationError(
            "confirmation must exactly match the phrase in the imaging plan"
        )

    command_runner = subprocess.run if runner is None else runner
    with _private_cloud_init_files(user_data, network_config) as paths:
        argv = build_imager_argv(
            executable=executable,
            manifest=manifest,
            target=safe_target,
            user_data_path=paths[0],
            network_config_path=paths[1],
        )
        completed = command_runner(argv, check=True, shell=False)
    if boot_customization is not None and boot_customizer is not None:
        boot_customizer(safe_target, boot_customization)
    return ImagingResult(plan=plan, returncode=completed.returncode)


def _validate_boot_customization(customization: BootCustomization | None) -> None:
    if customization is None:
        return
    fields = (
        customization.config_txt_append,
        customization.cmdline_append,
        customization.cmdline_remove,
    )
    for values in fields:
        if not isinstance(values, tuple):
            raise TypeError("boot customization fields must be tuples")
        for value in values:
            _require_clean_text("boot customization value", value)
            if any(character in value for character in ("\r", "\n", "\0")):
                raise ValueError("boot customization values must be single-line text")


def _require_current_safe_target(
    requested: DiskTarget, discovery: DiskDiscovery
) -> DiskTarget:
    matches = [
        candidate
        for candidate in discovery.list_disks()
        if candidate.stable_id == requested.stable_id
    ]
    if len(matches) != 1:
        raise UnsafeDiskError(
            "target must match exactly one currently discovered removable disk"
        )
    current = matches[0]
    if current != requested:
        raise UnsafeDiskError("target properties changed after selection")
    if not current.removable:
        raise UnsafeDiskError("target is not proven removable")
    if current.system_disk:
        raise UnsafeDiskError("target is a system disk")
    return current


@contextmanager
def _private_cloud_init_files(
    user_data: str, network_config: str
) -> Iterator[tuple[Path, Path]]:
    """Materialize secret configuration with owner-only filesystem modes."""

    with tempfile.TemporaryDirectory(prefix="vms-imaging-") as directory_name:
        directory = Path(directory_name)
        os.chmod(directory, 0o700)
        user_data_path = directory / "user-data"
        network_config_path = directory / "network-config"
        _write_owner_private(user_data_path, user_data)
        _write_owner_private(network_config_path, network_config)
        yield user_data_path, network_config_path


def _write_owner_private(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(content)
        os.chmod(path, 0o600)
        # Permission bits are authoritative on POSIX filesystems.  Other
        # platforms protect these files through the securely-created private
        # temporary directory and its inherited access controls.
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ImagingError("temporary configuration is not owner-private")
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _require_clean_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty, trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")


def _require_secret_document(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_iso_date(name: str, value: object) -> None:
    _require_clean_text(name, value)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO 8601 calendar date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use YYYY-MM-DD format")


def _decode_base64(name: str, value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError(f"{name} must be canonical base64") from error
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{name} must be canonical base64")
    return decoded


def _decode_public_key(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        decoded = value
    elif isinstance(value, str):
        _require_clean_text("trusted release public key", value)
        try:
            decoded = _decode_base64("trusted release public key", value)
        except ValueError as error:
            raise InvalidReleaseSignature(
                "trusted release public key is not canonical base64"
            ) from error
    else:
        raise InvalidReleaseSignature(
            "trusted release public key must be raw bytes or base64 text"
        )
    if len(decoded) != 32:
        raise InvalidReleaseSignature(
            "Ed25519 public key must contain exactly 32 bytes"
        )
    return decoded
