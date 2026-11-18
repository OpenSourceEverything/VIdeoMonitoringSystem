"""Persistent, platform-neutral recorder application service.

The service owns product state and returns data suitable for either a GUI or a
CLI.  Operations that require privileged platform integration are represented
as plans; an adapter may execute them after presenting the plan to the user.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Final

from .model import SOFTWARE_CAMERA_LIMIT


class RecorderError(ValueError):
    """A requested recorder operation cannot be completed."""


class RecorderStateError(RuntimeError):
    """The saved recorder state is unreadable or unsupported."""


class RecorderService:
    """Manage one recorder's durable product state.

    ``state_path`` is injectable so a GUI, CLI, appliance adapter, or test can
    decide where state belongs.  This class never performs storage formatting,
    device updates, or credential hashing.
    """

    STATE_VERSION: Final[int] = 1
    RECORDING_MODES: Final[frozenset[str]] = frozenset({"continuous", "events"})
    CREDENTIAL_PREFIXES: Final[tuple[str, ...]] = ("hash:", "ref:")

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_cameras: int = SOFTWARE_CAMERA_LIMIT,
    ):
        if max_cameras < 1:
            raise ValueError("Camera capacity must be at least one.")
        self.state_path = Path(state_path)
        self.max_cameras = max_cameras
        self._state = self._load()

    @property
    def state(self) -> dict[str, Any]:
        """Return a defensive snapshot of current state."""

        return copy.deepcopy(self._state)

    def setup_owner(self, *, name: str, credential_reference: str) -> dict[str, Any]:
        """Create the owner using a pre-hashed or externally stored credential."""

        name = self._required(name, "Owner name")
        credential_reference = self._credential_reference(credential_reference)
        existing = self._state["owner"]
        proposed = {"name": name, "credential_reference": credential_reference}
        if existing is not None:
            if existing == proposed:
                return {"result": "already_configured", "message": "Owner is ready."}
            raise RecorderError("An owner already exists.")
        self._state["owner"] = proposed
        self._save("owner")
        return {"result": "configured", "message": "Owner created."}

    def change_owner_credential(
        self, *, credential_reference: str
    ) -> dict[str, Any]:
        """Replace the existing owner's pre-hashed credential."""

        credential_reference = self._credential_reference(credential_reference)
        owner = self._state["owner"]
        if not isinstance(owner, dict):
            raise RecorderError("An owner must be created first.")
        if owner.get("credential_reference") == credential_reference:
            return {"result": "unchanged", "message": "Owner password is unchanged."}
        owner["credential_reference"] = credential_reference
        self._save("owner")
        return {"result": "updated", "message": "Owner password changed."}

    def plan_storage_initialization(self, *, storage_reference: str) -> dict[str, Any]:
        """Record and return a storage plan without changing real storage."""

        storage_reference = self._required(storage_reference, "Storage reference")
        existing = self._state["storage"]
        if existing is not None and existing.get("state") == "ready":
            if existing.get("storage_reference") == storage_reference:
                return {
                    "result": "already_initialized",
                    "message": "Storage is ready.",
                    "storage": copy.deepcopy(existing),
                }
            raise RecorderError("Recording storage is already initialized.")
        plan = {
            "action": "initialize_storage",
            "storage_reference": storage_reference,
            "confirmation_required": True,
            "state": "planned",
        }
        if self._state["storage"] == plan:
            return {"result": "already_planned", "message": "Storage is ready for confirmation.", "plan": copy.deepcopy(plan)}
        self._state["storage"] = plan
        self._save("storage")
        return {"result": "planned", "message": "Storage is ready for confirmation.", "plan": copy.deepcopy(plan)}

    def confirm_storage_initialized(
        self, *, storage_reference: str, volume_identity: str
    ) -> dict[str, Any]:
        """Mark an exact planned volume ready after its storage adapter attests it."""

        storage_reference = self._required(storage_reference, "Storage reference")
        volume_identity = self._required(volume_identity, "Storage volume identity")
        existing = self._state["storage"]
        if existing is None:
            raise RecorderError("Recording storage must be planned first.")
        if existing.get("storage_reference") != storage_reference:
            raise RecorderError("Initialized storage did not match the planned storage.")
        ready = {
            "storage_reference": storage_reference,
            "volume_identity": volume_identity,
            "state": "ready",
        }
        if existing == ready:
            return {
                "result": "already_initialized",
                "message": "Storage is ready.",
                "storage": copy.deepcopy(ready),
            }
        if existing.get("state") != "planned":
            raise RecorderError("Initialized storage volume identity did not match.")
        self._state["storage"] = ready
        self._save("storage")
        return {
            "result": "initialized",
            "message": "Storage is ready.",
            "storage": copy.deepcopy(ready),
        }

    @property
    def initialized_storage_identity(self) -> tuple[str, str] | None:
        storage = self._state["storage"]
        if not isinstance(storage, dict) or storage.get("state") != "ready":
            return None
        reference = storage.get("storage_reference")
        volume_identity = storage.get("volume_identity")
        if (
            not isinstance(reference, str)
            or not reference
            or not isinstance(volume_identity, str)
            or not volume_identity
        ):
            return None
        return reference, volume_identity

    @property
    def active_camera_ids(self) -> frozenset[str]:
        """Return active identities authorized for recorder access."""

        return frozenset(
            camera["id"]
            for camera in self._state["cameras"]
            if not camera["revoked"]
            and camera.get("replaces") is None
            and camera["state"] in {"ready", "claimed"}
        )

    @property
    def recording_camera_ids(self) -> frozenset[str]:
        """Return active identities whose owner has recording enabled."""

        return frozenset(
            camera["id"]
            for camera in self._state["cameras"]
            if camera["id"] in self.active_camera_ids
            and camera.get("recording_enabled", True)
        )

    @property
    def alerts_enabled(self) -> bool:
        """Return whether notification delivery is enabled recorder-wide."""

        return bool(self._state["alerts"]["enabled"])

    @property
    def alert_camera_ids(self) -> frozenset[str]:
        """Return active cameras whose saved alert preference is enabled."""

        return frozenset(
            camera["id"]
            for camera in self._state["cameras"]
            if camera["id"] in self.active_camera_ids
            and camera.get("alerts_enabled", True)
        )

    def register_camera(
        self,
        *,
        camera_id: str,
        claim_secret_hash: str,
        identity_reference: str,
        replaces: str | None = None,
        pre_enrolled: bool = False,
    ) -> dict[str, Any]:
        """Import one factory-provisioned camera into the recorder manifest."""

        camera_id = self._required(camera_id, "Camera ID")
        claim_secret_hash = self._secret_hash(claim_secret_hash)
        identity_reference = self._identity_reference(identity_reference)
        if not isinstance(pre_enrolled, bool):
            raise RecorderError("Camera enrollment state is invalid.")
        replaced_camera = None
        if replaces is not None:
            replaced_camera = self._claimed_camera_by_name(replaces)
        existing = self._camera(camera_id)
        proposed = {
            "id": camera_id,
            "identity_reference": identity_reference,
            "claim_secret_hash": None if pre_enrolled else claim_secret_hash,
            "state": "ready" if pre_enrolled else "unclaimed",
            "name": None,
            "connected": False,
            "recording_enabled": True,
            "alerts_enabled": True,
            "revoked": False,
            "replaces": replaced_camera["id"] if replaced_camera else None,
        }
        if existing is not None:
            if existing == proposed:
                return {"result": "already_registered", "camera_id": camera_id}
            raise RecorderError("That camera identity already exists.")
        if any(camera["identity_reference"] == identity_reference for camera in self._state["cameras"]):
            raise RecorderError("That camera identity already exists.")
        if replaced_camera is None and len([camera for camera in self._state["cameras"] if not camera["revoked"]]) >= self.max_cameras:
            raise RecorderError(f"This recorder supports up to {self.max_cameras} cameras.")
        self._state["cameras"].append(proposed)
        self._save("cameras")
        return {"result": "registered", "camera_id": camera_id}

    def name_camera(self, *, camera_id: str, name: str) -> dict[str, Any]:
        """Set the display name of an enrolled camera."""

        camera = self._active_camera(camera_id)
        name = self._required(name, "Camera name")
        if camera["state"] == "claimed":
            if camera["name"] == name:
                return {
                    "result": "already_named",
                    "message": f"{name} is ready.",
                    "camera": self._public_camera(camera),
                }
            camera["name"] = name
            self._save("cameras")
            return {
                "result": "renamed",
                "message": f"{name} is ready.",
                "camera": self._public_camera(camera),
            }
        if camera["state"] != "ready":
            raise RecorderError("That camera must be claimed first.")
        camera["state"] = "claimed"
        camera["name"] = name
        self._save("cameras")
        return {
            "result": "named",
            "message": f"{name} is ready.",
            "camera": self._public_camera(camera),
        }

    def position_camera(self, *, camera_id: str, position: int) -> dict[str, Any]:
        """Move an active camera to a one-based position in the feed wall."""

        camera = self._active_camera(camera_id)
        active = [item for item in self._state["cameras"] if not item["revoked"]]
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 1 <= position <= len(active)
        ):
            raise RecorderError("Camera position is invalid.")
        current = active.index(camera)
        target = position - 1
        if current == target:
            return {
                "result": "unchanged",
                "position": position,
                "camera": self._public_camera(camera),
            }
        active.pop(current)
        active.insert(target, camera)
        revoked = [item for item in self._state["cameras"] if item["revoked"]]
        self._state["cameras"] = active + revoked
        self._save("cameras")
        return {
            "result": "moved",
            "position": position,
            "camera": self._public_camera(camera),
        }

    def list_unclaimed_cameras(self) -> list[dict[str, Any]]:
        """List cameras available to claim without exposing secrets."""

        return [self._public_camera(camera) for camera in self._state["cameras"] if camera["state"] == "unclaimed" and not camera["revoked"]]

    def claim_camera(self, *, camera_id: str, claim_secret_hash: str, name: str) -> dict[str, Any]:
        """Claim a camera and consume its one-time claim secret."""

        camera = self._active_camera(camera_id)
        name = self._required(name, "Camera name")
        if camera["state"] == "claimed":
            if camera["name"] == name:
                return {"result": "already_claimed", "message": f"{name} is ready.", "camera": self._public_camera(camera)}
            raise RecorderError("That camera is already claimed.")
        supplied_hash = self._secret_hash(claim_secret_hash)
        expected_hash = camera["claim_secret_hash"]
        if expected_hash is None or not hmac.compare_digest(expected_hash, supplied_hash):
            raise RecorderError("Claim code did not match.")
        camera["state"] = "claimed"
        camera["name"] = name
        camera["claim_secret_hash"] = None
        self._save("cameras")
        return {"result": "claimed", "message": f"{name} added.", "camera": self._public_camera(camera)}

    def claim_camera_code(self, *, claim_secret_hash: str, name: str) -> dict[str, Any]:
        """Claim the one unclaimed camera matching a manual one-time code."""

        supplied_hash = self._secret_hash(claim_secret_hash)
        matches = []
        for camera in self._state["cameras"]:
            expected_hash = camera.get("claim_secret_hash")
            matches.append(
                camera
                if camera["state"] == "unclaimed"
                and not camera["revoked"]
                and isinstance(expected_hash, str)
                and hmac.compare_digest(expected_hash, supplied_hash)
                else None
            )
        selected = [camera for camera in matches if camera is not None]
        if len(selected) != 1:
            raise RecorderError("Camera code did not match.")
        return self.claim_camera(
            camera_id=selected[0]["id"],
            claim_secret_hash=supplied_hash,
            name=name,
        )

    def replace_camera(
        self,
        *,
        camera_name: str,
        replacement_id: str,
        claim_secret_hash: str,
    ) -> dict[str, Any]:
        """Replace a claimed camera, preserving its user-facing settings."""

        old = self._claimed_camera_by_name(camera_name)
        replacement = self._active_camera(replacement_id)
        if replacement is old:
            return {"result": "already_replaced", "message": f"{camera_name} is ready.", "camera": self._public_camera(old)}
        if replacement["state"] != "unclaimed":
            if replacement["state"] == "claimed" and replacement["name"] == old["name"]:
                return {"result": "already_replaced", "message": f"{camera_name} is ready.", "camera": self._public_camera(replacement)}
            raise RecorderError("The replacement camera is not available.")
        reserved_target = replacement.get("replaces")
        if reserved_target is not None and reserved_target != old["id"]:
            raise RecorderError("The replacement camera is reserved for another camera.")
        supplied_hash = self._secret_hash(claim_secret_hash)
        if not hmac.compare_digest(replacement["claim_secret_hash"], supplied_hash):
            raise RecorderError("Claim code did not match.")

        old["revoked"] = True
        old["state"] = "revoked"
        old["connected"] = False
        replacement["state"] = "claimed"
        replacement["name"] = old["name"]
        replacement["recording_enabled"] = old.get("recording_enabled", True)
        replacement["alerts_enabled"] = old.get("alerts_enabled", True)
        replacement["claim_secret_hash"] = None
        replacement["replaces"] = None
        self._save("cameras")
        return {"result": "replaced", "message": f"{camera_name} replaced.", "camera": self._public_camera(replacement)}

    def remove_camera(self, *, camera_name: str) -> dict[str, Any]:
        """Remove a camera and revoke its identity."""

        camera = self._claimed_camera_by_name(camera_name, include_revoked=True)
        if camera["revoked"]:
            return {"result": "already_removed", "message": f"{camera_name} removed."}
        camera["revoked"] = True
        camera["state"] = "revoked"
        camera["connected"] = False
        camera["claim_secret_hash"] = None
        self._save("cameras")
        return {"result": "removed", "message": f"{camera_name} removed."}

    def set_camera_connected(self, *, camera_id: str, connected: bool) -> dict[str, Any]:
        """Update observed camera presence for status reporting."""

        camera = self._active_camera(camera_id)
        if camera["connected"] == bool(connected):
            return {"result": "unchanged", "camera": self._public_camera(camera)}
        camera["connected"] = bool(connected)
        self._save("cameras")
        return {"result": "updated", "camera": self._public_camera(camera)}

    def set_camera_recording(
        self, *, camera_id: str, enabled: bool
    ) -> dict[str, Any]:
        """Enable or pause recording without disabling the live camera feed."""

        camera = self._active_camera(camera_id)
        if not isinstance(enabled, bool):
            raise RecorderError("Camera recording setting is invalid.")
        if camera.get("recording_enabled", True) is enabled:
            return {"result": "unchanged", "camera": self._public_camera(camera)}
        camera["recording_enabled"] = enabled
        self._save("cameras")
        return {"result": "updated", "camera": self._public_camera(camera)}

    def set_alerts_enabled(self, *, enabled: bool) -> dict[str, Any]:
        """Pause or resume all alerts without changing camera preferences."""

        if not isinstance(enabled, bool):
            raise RecorderError("Recorder alert setting is invalid.")
        if self._state["alerts"]["enabled"] is enabled:
            return {"result": "unchanged", "alerts": copy.deepcopy(self._state["alerts"])}
        self._state["alerts"]["enabled"] = enabled
        self._save("alerts")
        return {"result": "updated", "alerts": copy.deepcopy(self._state["alerts"])}

    def set_camera_alerts(
        self, *, camera_id: str, enabled: bool
    ) -> dict[str, Any]:
        """Save whether one camera may generate owner notifications."""

        camera = self._active_camera(camera_id)
        if not isinstance(enabled, bool):
            raise RecorderError("Camera alert setting is invalid.")
        if camera.get("alerts_enabled", True) is enabled:
            return {"result": "unchanged", "camera": self._public_camera(camera)}
        camera["alerts_enabled"] = enabled
        self._save("cameras")
        return {"result": "updated", "camera": self._public_camera(camera)}

    def set_recording(self, *, mode: str, days: int) -> dict[str, Any]:
        """Set the recording mode and retention period."""

        if mode not in self.RECORDING_MODES:
            raise RecorderError("Recording mode must be continuous or events.")
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise RecorderError("Retention must be at least one day.")
        proposed = {"mode": mode, "days": days}
        if self._state["recording"] == proposed:
            return {"result": "unchanged", "message": "Recording settings are ready.", "recording": copy.deepcopy(proposed)}
        self._state["recording"] = proposed
        self._save("recording")
        return {"result": "configured", "message": "Recording settings saved.", "recording": copy.deepcopy(proposed)}

    def status(self) -> dict[str, Any]:
        """Return a retail-safe summary; missing cameras remain pending."""

        active = [
            camera
            for camera in self._state["cameras"]
            if not camera["revoked"] and camera.get("replaces") is None
        ]
        connected = sum(camera["connected"] for camera in active)
        pending = len(active) - connected
        storage = self._storage_status()
        if self._state["owner"] is None:
            next_setup_step = "owner"
        elif storage != "ready":
            next_setup_step = "storage"
        elif not active or any(
            camera["state"] not in {"ready", "claimed"} for camera in active
        ):
            next_setup_step = "cameras"
        else:
            next_setup_step = "ready"
        return {
            "owner_ready": self._state["owner"] is not None,
            "storage": storage,
            "cameras": {"expected": len(active), "connected": connected, "pending": pending},
            "recording": copy.deepcopy(self._state["recording"]),
            "alerts": copy.deepcopy(self._state["alerts"]),
            "update": copy.deepcopy(self._state["update"]),
            "setup": {
                "ready": next_setup_step == "ready",
                "next": next_setup_step,
            },
        }

    def plan_update(self, *, version: str, release_reference: str, signature_verified: bool) -> dict[str, Any]:
        """Create an update plan; executing the update belongs to an adapter."""

        version = self._required(version, "Version")
        release_reference = self._required(release_reference, "Release reference")
        if not signature_verified:
            raise RecorderError("The update could not be verified.")
        plan = {
            "action": "install_update",
            "version": version,
            "release_reference": release_reference,
            "state": "planned",
        }
        if self._state["update"] == plan:
            return {"result": "already_planned", "message": "Update is ready for confirmation.", "plan": copy.deepcopy(plan)}
        self._state["update"] = plan
        self._save("update")
        return {"result": "planned", "message": "Update is ready for confirmation.", "plan": copy.deepcopy(plan)}

    def _default_state(self) -> dict[str, Any]:
        return {
            "schema_version": self.STATE_VERSION,
            "owner": None,
            "storage": None,
            "cameras": [],
            "recording": {"mode": "continuous", "days": 7},
            "alerts": {"enabled": True},
            "update": None,
        }

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RecorderStateError("Recorder state could not be read.") from exc
        if not isinstance(state, dict) or state.get("schema_version") != self.STATE_VERSION:
            raise RecorderStateError("Recorder state version is not supported.")
        required = {"owner", "storage", "cameras", "recording", "update"}
        if (
            not required.issubset(state)
            or not isinstance(state["cameras"], list)
            or (state["storage"] is not None and not isinstance(state["storage"], dict))
        ):
            raise RecorderStateError("Recorder state is incomplete.")
        alerts = state.setdefault("alerts", {"enabled": True})
        if (
            not isinstance(alerts, dict)
            or not isinstance(alerts.get("enabled"), bool)
        ):
            raise RecorderStateError("Recorder alert state is incomplete.")
        for camera in state["cameras"]:
            if not isinstance(camera, dict):
                raise RecorderStateError("Recorder camera state is incomplete.")
            recording_enabled = camera.setdefault("recording_enabled", True)
            if not isinstance(recording_enabled, bool):
                raise RecorderStateError("Recorder camera state is incomplete.")
            alerts_enabled = camera.setdefault("alerts_enabled", True)
            if not isinstance(alerts_enabled, bool):
                raise RecorderStateError("Recorder camera state is incomplete.")
        return state

    def _storage_status(self) -> str:
        storage = self._state["storage"]
        if storage is None:
            return "not_configured"
        state = storage.get("state")
        if state == "planned" and isinstance(
            storage.get("storage_reference"), str
        ):
            return "planned"
        if state == "ready" and self.initialized_storage_identity is not None:
            return "ready"
        return "invalid"

    def _save(self, *changed_sections: str) -> None:
        """Atomically merge this operation into the latest saved state.

        Recorder adapters run as separate processes on the appliance. Each one
        keeps an in-memory snapshot, so writing the whole snapshot can otherwise
        restore stale credentials or settings when (for example) a camera status
        update lands after an owner password change.
        """

        allowed_sections = {
            "owner",
            "storage",
            "cameras",
            "recording",
            "alerts",
            "update",
        }
        if not changed_sections or not set(changed_sections).issubset(allowed_sections):
            raise ValueError("Saved recorder sections must be declared.")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_name(f".{self.state_path.name}.lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        persisted_state: dict[str, Any] | None = None
        temporary_path: Path | None = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            persisted_state = self._load()
            merged_state = copy.deepcopy(persisted_state)
            for section in changed_sections:
                merged_state[section] = copy.deepcopy(self._state[section])
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.state_path.name}.",
                suffix=".tmp",
                dir=self.state_path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(merged_state, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.state_path)
            self._state = merged_state
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if persisted_state is not None:
                self._state = persisted_state
            raise
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _camera(self, camera_id: str) -> dict[str, Any] | None:
        camera_id = self._required(camera_id, "Camera ID")
        return next((camera for camera in self._state["cameras"] if camera["id"] == camera_id), None)

    def _active_camera(self, camera_id: str) -> dict[str, Any]:
        camera = self._camera(camera_id)
        if camera is None or camera["revoked"]:
            raise RecorderError("Camera was not found.")
        return camera

    def _claimed_camera_by_name(self, name: str, *, include_revoked: bool = False) -> dict[str, Any]:
        name = self._required(name, "Camera name")
        matching = [camera for camera in self._state["cameras"] if camera["name"] == name]
        camera = next((camera for camera in matching if camera["state"] == "claimed" and not camera["revoked"]), None)
        if camera is None and include_revoked:
            camera = next((camera for camera in reversed(matching) if camera["state"] == "revoked"), None)
        if camera is None:
            raise RecorderError("Camera was not found.")
        return camera

    @staticmethod
    def _required(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise RecorderError(f"{label} is required.")
        return value.strip()

    @classmethod
    def _credential_reference(cls, value: str) -> str:
        value = cls._required(value, "Owner credential")
        if not value.startswith(cls.CREDENTIAL_PREFIXES):
            raise RecorderError("Owner credential must be pre-hashed or stored by a credential provider.")
        return value

    @staticmethod
    def _identity_reference(value: str) -> str:
        value = RecorderService._required(value, "Camera identity")
        if not value.startswith(("identity:", "ref:")) or "PRIVATE KEY" in value.upper():
            raise RecorderError("Camera identity must be an opaque identity reference.")
        return value

    @staticmethod
    def _secret_hash(value: str) -> str:
        value = RecorderService._required(value, "Claim code hash")
        try:
            digest = bytes.fromhex(value)
        except ValueError as exc:
            raise RecorderError("Claim code hash is invalid.") from exc
        if len(digest) != hashlib.sha256().digest_size:
            raise RecorderError("Claim code hash is invalid.")
        return value.lower()

    @staticmethod
    def _public_camera(camera: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": camera["id"],
            "name": camera["name"],
            "state": camera["state"],
            "connected": camera["connected"],
            "recording_enabled": camera.get("recording_enabled", True),
            "alerts_enabled": camera.get("alerts_enabled", True),
        }
