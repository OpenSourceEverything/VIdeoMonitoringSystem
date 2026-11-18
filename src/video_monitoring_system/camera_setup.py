"""Resumable, hardware-neutral camera network-enrollment transactions.

The browser and recorder must never collect a home Wi-Fi credential.  A DPP or
other reviewed platform adapter performs that exchange and reports only a
verified, public identity binding through :class:`CameraSetupTransport`.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from .model import SOFTWARE_CAMERA_LIMIT
from .provisioning import secure_write_text


class CameraSetupError(RuntimeError):
    """Camera setup state or a transport result failed closed."""


_STATE_FILE = "camera-setup.json"
_MAXIMUM_BYTES = 128 * 1024
_DEVICE_ID = re.compile(r"(?:CAM|REC)-[A-F0-9]{16,64}")
_TRANSACTION_ID = re.compile(r"[A-Za-z0-9_-]{24,128}")
_TRANSPORT = re.compile(r"[a-z][a-z0-9-]{0,31}")
_BOOTSTRAP_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}")
_PERMANENT_IDENTITY = re.compile(r"identity:[A-Za-z0-9_-]{16,1024}")
_STATES = {
    "waiting-network",
    "waiting-heartbeat",
    "verifying-video",
    "ready",
    "needs-attention",
}
_ISSUES = {
    "unsupported-device",
    "network-timeout",
    "identity-mismatch",
    "heartbeat-timeout",
    "video-verification-failed",
    "route-commit-failed",
}


@dataclass(frozen=True, slots=True)
class CameraSetupBinding:
    """Public identities a setup adapter must bind in one transaction."""

    transaction_id: str = field(repr=False)
    camera_id: str
    recorder_id: str
    bootstrap_identity: str
    permanent_identity: str
    transport: str

    def __post_init__(self) -> None:
        _validate_binding(self)


@dataclass(frozen=True, slots=True)
class VerifiedCameraEnrollment:
    """Secret-free result emitted only after a transport verifies enrollment."""

    transaction_id: str = field(repr=False)
    camera_id: str
    recorder_id: str
    bootstrap_identity: str
    permanent_identity: str
    transport: str

    def __post_init__(self) -> None:
        _validate_binding(self)


@runtime_checkable
class CameraSetupTransport(Protocol):
    """Adapter boundary for DPP or an equivalently reviewed transport.

    Network names and credentials are deliberately absent.  The adapter may
    return ``None`` while its OS/native enrollment surface is still pending.
    """

    name: str

    def verified_enrollment(
        self, binding: CameraSetupBinding
    ) -> VerifiedCameraEnrollment | None: ...


class CameraSetupCoordinator:
    """Persist one resumable setup transaction per physical camera identity."""

    def __init__(
        self,
        data_directory: Path,
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(data_directory) / _STATE_FILE
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._lock = threading.RLock()

    def begin(
        self,
        *,
        camera_id: str,
        recorder_id: str,
        bootstrap_identity: str,
        permanent_identity: str,
        transport: str,
        now: datetime | None = None,
    ) -> CameraSetupBinding:
        selected_now = _timestamp(now)
        with self._lock:
            state = self._load()
            setups = state["setups"]
            assert isinstance(setups, dict)
            existing = setups.get(camera_id)
            if existing is not None:
                binding = _entry_binding(existing)
                expected = (
                    camera_id,
                    recorder_id,
                    bootstrap_identity,
                    permanent_identity,
                    transport,
                )
                actual = (
                    binding.camera_id,
                    binding.recorder_id,
                    binding.bootstrap_identity,
                    binding.permanent_identity,
                    binding.transport,
                )
                if actual != expected:
                    raise CameraSetupError(
                        "That camera already has a different setup binding."
                    )
                return binding
            if len(setups) >= SOFTWARE_CAMERA_LIMIT:
                raise CameraSetupError("The camera setup limit has been reached.")
            binding = CameraSetupBinding(
                transaction_id=self._token_factory(),
                camera_id=camera_id,
                recorder_id=recorder_id,
                bootstrap_identity=bootstrap_identity,
                permanent_identity=permanent_identity,
                transport=transport,
            )
            if any(
                isinstance(entry, dict)
                and entry.get("transaction_id") == binding.transaction_id
                for entry in setups.values()
            ):
                raise CameraSetupError("The camera setup transaction is invalid.")
            setups[camera_id] = {
                "transaction_id": binding.transaction_id,
                "camera_id": binding.camera_id,
                "recorder_id": binding.recorder_id,
                "bootstrap_identity": binding.bootstrap_identity,
                "permanent_identity": binding.permanent_identity,
                "transport": binding.transport,
                "state": "waiting-network",
                "issue": None,
                "created_at": selected_now,
                "updated_at": selected_now,
            }
            self._save(state)
            return binding

    def accept_enrollment(
        self,
        result: VerifiedCameraEnrollment,
        *,
        now: datetime | None = None,
    ) -> Mapping[str, object]:
        if not isinstance(result, VerifiedCameraEnrollment):
            raise TypeError("A verified camera enrollment result is required.")
        selected_now = _timestamp(now)
        with self._lock:
            state = self._load()
            entry = self._entry_for_transaction(state, result.transaction_id)
            binding = _entry_binding(entry)
            if (
                result.camera_id,
                result.recorder_id,
                result.bootstrap_identity,
                result.permanent_identity,
                result.transport,
            ) != (
                binding.camera_id,
                binding.recorder_id,
                binding.bootstrap_identity,
                binding.permanent_identity,
                binding.transport,
            ):
                raise CameraSetupError("Camera enrollment identity did not match.")
            if entry["state"] == "needs-attention":
                raise CameraSetupError("Camera setup must be retried first.")
            if entry["state"] == "waiting-network":
                entry["state"] = "waiting-heartbeat"
                entry["updated_at"] = selected_now
                self._save(state)
            return _public_status(entry)

    def observe_heartbeat(
        self, camera_id: str, *, now: datetime | None = None
    ) -> Mapping[str, object] | None:
        return self._advance(
            camera_id,
            expected="waiting-heartbeat",
            selected="verifying-video",
            now=now,
        )

    def mark_ready(
        self, camera_id: str, *, now: datetime | None = None
    ) -> Mapping[str, object] | None:
        return self._advance(
            camera_id,
            expected="verifying-video",
            selected="ready",
            now=now,
        )

    def mark_attention(
        self,
        camera_id: str,
        *,
        issue: str,
        now: datetime | None = None,
    ) -> Mapping[str, object] | None:
        if issue not in _ISSUES:
            raise ValueError("Camera setup issue is invalid.")
        selected_now = _timestamp(now)
        with self._lock:
            state = self._load()
            entry = self._entry_for_camera(state, camera_id)
            if entry is None:
                return None
            if entry["state"] == "ready":
                raise CameraSetupError("A completed camera setup cannot be changed.")
            entry["state"] = "needs-attention"
            entry["issue"] = issue
            entry["updated_at"] = selected_now
            self._save(state)
            return _public_status(entry)

    def retry(
        self, camera_id: str, *, now: datetime | None = None
    ) -> Mapping[str, object]:
        selected_now = _timestamp(now)
        with self._lock:
            state = self._load()
            entry = self._entry_for_camera(state, camera_id)
            if entry is None or entry["state"] != "needs-attention":
                raise CameraSetupError("Camera setup is not ready to retry.")
            entry["state"] = "waiting-network"
            entry["issue"] = None
            entry["updated_at"] = selected_now
            self._save(state)
            return _public_status(entry)

    def status(self, camera_id: str) -> Mapping[str, object] | None:
        with self._lock:
            entry = self._entry_for_camera(self._load(), camera_id)
            return None if entry is None else _public_status(entry)

    def binding(self, transaction_id: str) -> CameraSetupBinding:
        if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(
            transaction_id
        ) is None:
            raise CameraSetupError("Camera setup transaction is invalid.")
        with self._lock:
            return _entry_binding(
                self._entry_for_transaction(self._load(), transaction_id)
            )

    def _advance(
        self,
        camera_id: str,
        *,
        expected: str,
        selected: str,
        now: datetime | None,
    ) -> Mapping[str, object] | None:
        selected_now = _timestamp(now)
        with self._lock:
            state = self._load()
            entry = self._entry_for_camera(state, camera_id)
            if entry is None:
                return None
            if entry["state"] == selected or entry["state"] == "ready":
                return _public_status(entry)
            if entry["state"] != expected:
                raise CameraSetupError("Camera setup arrived out of order.")
            entry["state"] = selected
            entry["issue"] = None
            entry["updated_at"] = selected_now
            self._save(state)
            return _public_status(entry)

    @staticmethod
    def _entry_for_camera(
        state: Mapping[str, object], camera_id: str
    ) -> dict[str, object] | None:
        if not isinstance(camera_id, str) or _DEVICE_ID.fullmatch(camera_id) is None:
            raise CameraSetupError("Camera identity is invalid.")
        setups = state["setups"]
        assert isinstance(setups, dict)
        entry = setups.get(camera_id)
        assert entry is None or isinstance(entry, dict)
        return entry

    @staticmethod
    def _entry_for_transaction(
        state: Mapping[str, object], transaction_id: str
    ) -> dict[str, object]:
        if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(
            transaction_id
        ) is None:
            raise CameraSetupError("Camera setup transaction is invalid.")
        setups = state["setups"]
        assert isinstance(setups, dict)
        matches = [
            entry
            for entry in setups.values()
            if isinstance(entry, dict) and entry.get("transaction_id") == transaction_id
        ]
        if len(matches) != 1:
            raise CameraSetupError("Camera setup transaction is unavailable.")
        return matches[0]

    def _load(self) -> dict[str, object]:
        try:
            if self.path.stat().st_size > _MAXIMUM_BYTES:
                raise ValueError
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "setups": {}}
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise CameraSetupError("Saved camera setup state could not be read.") from error
        try:
            return _validate_state(document)
        except (TypeError, ValueError) as error:
            raise CameraSetupError("Saved camera setup state could not be read.") from error

    def _save(self, state: Mapping[str, object]) -> None:
        try:
            validated = _validate_state(dict(state))
            secure_write_text(
                self.path,
                json.dumps(validated, separators=(",", ":"), sort_keys=True) + "\n",
            )
        except (OSError, TypeError, ValueError) as error:
            raise CameraSetupError("Camera setup state could not be saved.") from error


def _validate_binding(value: object) -> None:
    for name, pattern in (
        ("transaction_id", _TRANSACTION_ID),
        ("camera_id", _DEVICE_ID),
        ("recorder_id", _DEVICE_ID),
        ("bootstrap_identity", _BOOTSTRAP_IDENTITY),
        ("permanent_identity", _PERMANENT_IDENTITY),
        ("transport", _TRANSPORT),
    ):
        selected = getattr(value, name, None)
        if not isinstance(selected, str) or pattern.fullmatch(selected) is None:
            raise ValueError(f"Camera setup {name.replace('_', ' ')} is invalid.")
    if not str(getattr(value, "camera_id")).startswith("CAM-") or not str(
        getattr(value, "recorder_id")
    ).startswith("REC-"):
        raise ValueError("Camera setup device roles are invalid.")


def _entry_binding(entry: Mapping[str, object]) -> CameraSetupBinding:
    return CameraSetupBinding(
        transaction_id=str(entry["transaction_id"]),
        camera_id=str(entry["camera_id"]),
        recorder_id=str(entry["recorder_id"]),
        bootstrap_identity=str(entry["bootstrap_identity"]),
        permanent_identity=str(entry["permanent_identity"]),
        transport=str(entry["transport"]),
    )


def _public_status(entry: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "camera_id": entry["camera_id"],
        "state": entry["state"],
        "transport": entry["transport"],
        "issue": entry["issue"],
        "updated_at": entry["updated_at"],
    }


def _validate_state(document: object) -> dict[str, object]:
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "setups"}
        or document.get("schema_version") != 1
        or not isinstance(document.get("setups"), dict)
    ):
        raise ValueError
    setups = document["setups"]
    assert isinstance(setups, dict)
    if len(setups) > SOFTWARE_CAMERA_LIMIT:
        raise ValueError
    transactions: set[str] = set()
    expected_keys = {
        "transaction_id",
        "camera_id",
        "recorder_id",
        "bootstrap_identity",
        "permanent_identity",
        "transport",
        "state",
        "issue",
        "created_at",
        "updated_at",
    }
    for camera_id, entry in setups.items():
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            raise ValueError
        binding = _entry_binding(entry)
        if camera_id != binding.camera_id or binding.transaction_id in transactions:
            raise ValueError
        transactions.add(binding.transaction_id)
        state = entry["state"]
        issue = entry["issue"]
        if state not in _STATES or (issue is not None and issue not in _ISSUES):
            raise ValueError
        if (state == "needs-attention") != (issue is not None):
            raise ValueError
        created = entry["created_at"]
        updated = entry["updated_at"]
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in (created, updated)
        ) or not 0 <= float(created) <= float(updated):
            raise ValueError
    return document


def _timestamp(now: datetime | None) -> float:
    selected = datetime.now(UTC) if now is None else now
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("Camera setup time must include a timezone.")
    timestamp = selected.timestamp()
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("Camera setup time is invalid.")
    return timestamp


__all__ = [
    "CameraSetupBinding",
    "CameraSetupCoordinator",
    "CameraSetupError",
    "CameraSetupTransport",
    "VerifiedCameraEnrollment",
]
