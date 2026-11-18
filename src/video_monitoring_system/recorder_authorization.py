"""Strict reader for the recorder's private per-kit camera authorization."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .model import SOFTWARE_CAMERA_LIMIT


class RecorderAuthorizationError(RuntimeError):
    """Recorder authorization was unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class AuthorizedCamera:
    device_id: str
    camera_number: int
    identity_reference: str
    claim_secret_hash: str = field(repr=False)
    stream_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RecorderAuthorization:
    recorder_id: str
    cameras: tuple[AuthorizedCamera, ...]


_DEVICE_ID = re.compile(r"(?:REC|CAM)-[A-F0-9]{16}")
_DIGEST = re.compile(r"[0-9a-fA-F]{64}")
_ALGORITHM = re.compile(r"[a-z0-9-]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
_MAXIMUM_BYTES = 64 * 1024


def load_recorder_authorization(
    path: str | os.PathLike[str],
) -> RecorderAuthorization | None:
    """Load one root- or process-owned private authorization document."""

    source = Path(path)
    try:
        details = source.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RecorderAuthorizationError(
            "Recorder authorization could not be read safely."
        ) from error
    try:
        mode = stat.S_IMODE(details.st_mode)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid not in {0, os.geteuid()}
            or mode not in {0o600, 0o640}
            or not 0 < details.st_size <= _MAXIMUM_BYTES
        ):
            raise ValueError
        document = json.loads(
            source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
        return _parse(document)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RecorderAuthorizationError(
            "Recorder authorization could not be read safely."
        ) from error


def _parse(document: object) -> RecorderAuthorization:
    if not isinstance(document, dict):
        raise ValueError
    if document.get("schema_version") != 1 or document.get("role") != "recorder":
        raise ValueError
    recorder_id = _device_id(document.get("device_id"), role="REC")
    raw_cameras = document.get("authorized_cameras")
    if (
        not isinstance(raw_cameras, list)
        or not 1 <= len(raw_cameras) <= SOFTWARE_CAMERA_LIMIT
    ):
        raise ValueError

    cameras = tuple(_camera(value) for value in raw_cameras)
    if len({camera.device_id for camera in cameras}) != len(cameras):
        raise ValueError
    if len({camera.camera_number for camera in cameras}) != len(cameras):
        raise ValueError
    return RecorderAuthorization(recorder_id=recorder_id, cameras=cameras)


def _camera(value: object) -> AuthorizedCamera:
    if not isinstance(value, dict) or set(value) != {
        "device_id",
        "camera_number",
        "identity",
        "claim_secret_hash",
        "stream_secret",
    }:
        raise ValueError
    identity = value["identity"]
    if not isinstance(identity, dict) or set(identity) != {"algorithm", "public"}:
        raise ValueError
    algorithm = _text(identity["algorithm"], maximum=64)
    public_identity = _text(identity["public"], maximum=1024)
    if (
        _ALGORITHM.fullmatch(algorithm) is None
        or _TOKEN.fullmatch(public_identity) is None
    ):
        raise ValueError
    number = value["camera_number"]
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or not 1 <= number <= SOFTWARE_CAMERA_LIMIT
    ):
        raise ValueError
    claim_hash = value["claim_secret_hash"]
    if not isinstance(claim_hash, str) or _DIGEST.fullmatch(claim_hash) is None:
        raise ValueError
    stream_secret = _text(value["stream_secret"], minimum=16, maximum=1024)
    if _TOKEN.fullmatch(stream_secret) is None:
        raise ValueError
    return AuthorizedCamera(
        device_id=_device_id(value["device_id"], role="CAM"),
        camera_number=number,
        identity_reference=f"identity:{public_identity}",
        claim_secret_hash=claim_hash.lower(),
        stream_secret=stream_secret,
    )


def _device_id(value: object, *, role: str) -> str:
    result = _text(value, maximum=20)
    if _DEVICE_ID.fullmatch(result) is None or not result.startswith(f"{role}-"):
        raise ValueError
    return result


def _text(value: object, *, minimum: int = 1, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or "\x00" in value
    ):
        raise ValueError
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


__all__ = [
    "AuthorizedCamera",
    "RecorderAuthorization",
    "RecorderAuthorizationError",
    "load_recorder_authorization",
]
