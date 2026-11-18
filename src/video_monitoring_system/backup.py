"""Authenticated, encrypted, portable recorder backups.

Backups deliberately contain product state and a recording catalog, not video
payloads.  This keeps the default backup small enough to copy off the recorder
while preserving the secrets needed to reconnect cameras after recovery.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class BackupError(RuntimeError):
    """A backup could not be created, authenticated, or safely restored."""


@dataclass(frozen=True, slots=True)
class BackupSummary:
    created_at: str
    state_files: int
    recordings: int
    includes_video: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "created_at": self.created_at,
            "state_files": self.state_files,
            "recordings": self.recordings,
            "includes_video": self.includes_video,
        }


_MAGIC: Final[bytes] = b"VMS-BACKUP\x00\x01"
_SALT_BYTES: Final[int] = 16
_NONCE_BYTES: Final[int] = 12
_MAXIMUM_STATE_FILE_BYTES: Final[int] = 8 * 1024 * 1024
_MAXIMUM_PLAINTEXT_BYTES: Final[int] = 64 * 1024 * 1024
_MAXIMUM_STATE_FILES: Final[int] = 256
_MAXIMUM_RECORDINGS: Final[int] = 100_000
_RELATIVE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def create_encrypted_backup(
    destination: str | os.PathLike[str],
    *,
    password: str,
    state_files: Mapping[str, Path],
    recordings: Sequence[Mapping[str, object]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BackupSummary:
    """Write one new mode-0600 backup without exposing plaintext on disk."""

    _require_password(password)
    destination_path = Path(destination).expanduser().resolve()
    if destination_path.exists():
        raise BackupError("The backup destination already exists.")
    if not 1 <= len(state_files) <= _MAXIMUM_STATE_FILES:
        raise BackupError("Recorder state is incomplete or too large to back up.")
    if len(recordings) > _MAXIMUM_RECORDINGS:
        raise BackupError("Recording catalog is too large to back up.")

    files = []
    total = 0
    for relative, source in sorted(state_files.items()):
        relative = _relative_path(relative)
        source = Path(source)
        try:
            details = source.stat()
            if source.is_symlink() or not source.is_file():
                raise OSError
            if details.st_size > _MAXIMUM_STATE_FILE_BYTES:
                raise BackupError("A recorder state file is too large to back up.")
            content = source.read_bytes()
        except BackupError:
            raise
        except OSError as error:
            raise BackupError("Recorder state could not be read for backup.") from error
        total += len(content)
        if total > _MAXIMUM_PLAINTEXT_BYTES:
            raise BackupError("Recorder state is too large to back up.")
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "content": base64.b64encode(content).decode("ascii"),
            }
        )

    created_at = clock().astimezone(UTC).isoformat()
    document = {
        "schema_version": 1,
        "created_at": created_at,
        "scope": "state-and-recording-metadata",
        "includes_video": False,
        "state_files": files,
        "recordings": list(recordings),
    }
    plaintext = json.dumps(
        document, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(plaintext) > _MAXIMUM_PLAINTEXT_BYTES:
        raise BackupError("Backup metadata is too large.")

    salt = secrets.token_bytes(_SALT_BYTES)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    key = _derive_key(password, salt)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _MAGIC)
    envelope = {
        "schema_version": 1,
        "kdf": "scrypt-16384-8-1",
        "salt": _encode(salt),
        "cipher": "aes-256-gcm",
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
    }
    encoded = _MAGIC + json.dumps(
        envelope, sort_keys=True, separators=(",", ":")
    ).encode("ascii")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", dir=destination_path.parent
    )
    try:
        os.fchmod(temporary_fd, 0o600)
        with os.fdopen(temporary_fd, "wb") as stream:
            temporary_fd = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_name, destination_path)
        os.chmod(destination_path, 0o600)
    except FileExistsError as error:
        raise BackupError("The backup destination already exists.") from error
    except OSError as error:
        raise BackupError("The encrypted backup could not be saved.") from error
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass

    return BackupSummary(created_at, len(files), len(recordings))


def read_encrypted_backup(
    source: str | os.PathLike[str], *, password: str
) -> tuple[dict[str, object], BackupSummary]:
    """Authenticate, decrypt, and strictly validate one backup document."""

    _require_password(password)
    source_path = Path(source).expanduser().resolve()
    try:
        if source_path.is_symlink() or not source_path.is_file():
            raise OSError
        encoded = source_path.read_bytes()
    except OSError as error:
        raise BackupError("The backup file could not be read.") from error
    if len(encoded) > _MAXIMUM_PLAINTEXT_BYTES * 2 or not encoded.startswith(_MAGIC):
        raise BackupError("This is not a supported VMS backup.")
    try:
        envelope = json.loads(encoded[len(_MAGIC) :].decode("ascii"))
        if (
            not isinstance(envelope, dict)
            or set(envelope)
            != {"schema_version", "kdf", "salt", "cipher", "nonce", "ciphertext"}
            or envelope["schema_version"] != 1
            or envelope["kdf"] != "scrypt-16384-8-1"
            or envelope["cipher"] != "aes-256-gcm"
        ):
            raise ValueError
        salt = _decode(envelope["salt"], _SALT_BYTES)
        nonce = _decode(envelope["nonce"], _NONCE_BYTES)
        ciphertext = _decode(envelope["ciphertext"], None)
        plaintext = AESGCM(_derive_key(password, salt)).decrypt(
            nonce, ciphertext, _MAGIC
        )
        document = json.loads(plaintext.decode("utf-8"))
        summary = _validate_document(document)
    except InvalidTag as error:
        raise BackupError("The backup password is incorrect or the file was changed.") from error
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupError("The backup file is invalid or unsupported.") from error
    return document, summary


def restored_files(document: Mapping[str, object]) -> dict[str, bytes]:
    """Return hash-verified state payloads from a validated document."""

    _validate_document(document)
    restored = {}
    for value in document["state_files"]:
        path = _relative_path(value["path"])
        content = _decode(value["content"], None)
        if not secrets.compare_digest(
            hashlib.sha256(content).hexdigest(), value["sha256"]
        ):
            raise BackupError("A backup state file failed verification.")
        restored[path] = content
    return restored


def _validate_document(document: object) -> BackupSummary:
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema_version",
            "created_at",
            "scope",
            "includes_video",
            "state_files",
            "recordings",
        }
        or document["schema_version"] != 1
        or document["scope"] != "state-and-recording-metadata"
        or document["includes_video"] is not False
        or not isinstance(document["created_at"], str)
        or not isinstance(document["state_files"], list)
        or not 1 <= len(document["state_files"]) <= _MAXIMUM_STATE_FILES
        or not isinstance(document["recordings"], list)
        or len(document["recordings"]) > _MAXIMUM_RECORDINGS
    ):
        raise ValueError("invalid backup document")
    try:
        created_at = datetime.fromisoformat(document["created_at"])
    except ValueError as error:
        raise ValueError("invalid backup time") from error
    if created_at.tzinfo is None:
        raise ValueError("invalid backup time")

    seen = set()
    for value in document["state_files"]:
        if (
            not isinstance(value, dict)
            or set(value) != {"path", "sha256", "content"}
            or not isinstance(value["path"], str)
            or not isinstance(value["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
            or not isinstance(value["content"], str)
        ):
            raise ValueError("invalid state file")
        path = _relative_path(value["path"])
        if path in seen:
            raise ValueError("duplicate state file")
        seen.add(path)
    if "recorder-state.json" not in seen:
        raise ValueError("recorder state is missing")
    for recording in document["recordings"]:
        if not isinstance(recording, dict):
            raise ValueError("invalid recording catalog")
    return BackupSummary(
        document["created_at"],
        len(document["state_files"]),
        len(document["recordings"]),
    )


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid backup path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        component in {"", ".", ".."} or _RELATIVE_COMPONENT.fullmatch(component) is None
        for component in path.parts
    ):
        raise ValueError("invalid backup path")
    return path.as_posix()


def _derive_key(password: str, salt: bytes) -> bytes:
    try:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
    except (UnicodeError, ValueError) as error:
        raise BackupError("The backup password is invalid.") from error


def _require_password(password: str) -> None:
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise BackupError("The owner password is required.")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: object, expected_bytes: int | None) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid encoded value")
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise ValueError("invalid encoded length")
    return decoded


__all__ = [
    "BackupError",
    "BackupSummary",
    "create_encrypted_backup",
    "read_encrypted_backup",
    "restored_files",
]
