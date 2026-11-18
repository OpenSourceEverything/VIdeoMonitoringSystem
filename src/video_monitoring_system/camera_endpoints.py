"""Durable identity-to-endpoint reconciliation for recorder camera access.

Discovery adapters authenticate a camera first, then submit the resulting
observation here.  Camera identity is the durable key; IP addresses, network
adapter MAC addresses, and DHCP client identifiers are observations that may
change without replacing the camera.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
from typing import Final


class CameraEndpointError(ValueError):
    """An endpoint observation or reconciliation request is invalid."""


class CameraEndpointConflictError(CameraEndpointError):
    """Authenticated observations do not describe one unambiguous snapshot."""


class CameraEndpointStateError(RuntimeError):
    """Saved camera endpoint state is unreadable or unsupported."""


def _camera_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CameraEndpointError("Camera ID is required.")
    return value.strip()


def _ip_address(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CameraEndpointError("Camera IP address is required.")
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise CameraEndpointError("Camera IP address is invalid.") from exc


def _mac_address(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CameraEndpointError("Camera MAC address is invalid.")
    compact = re.sub(r"[:-]", "", value.strip())
    if len(compact) != 12 or re.fullmatch(r"[0-9a-fA-F]{12}", compact) is None:
        raise CameraEndpointError("Camera MAC address is invalid.")
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2)).lower()


def _optional_metadata(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CameraEndpointError(f"{label} is invalid.")
    return value.strip()


@dataclass(frozen=True)
class ObservedCameraEndpoint:
    """Mutable network facts observed for one camera card.

    ``dhcp_client_id`` can help discovery or DHCP keep a card's address stable
    across a board swap.  It is deliberately metadata, never an authentication
    credential or registry key.
    """

    ip_address: str
    port: int = 9080
    mac_address: str | None = None
    dhcp_client_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ip_address", _ip_address(self.ip_address))
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise CameraEndpointError("Camera endpoint port is invalid.")
        if not 1 <= self.port <= 65535:
            raise CameraEndpointError("Camera endpoint port is invalid.")
        object.__setattr__(self, "mac_address", _mac_address(self.mac_address))
        object.__setattr__(
            self,
            "dhcp_client_id",
            _optional_metadata(self.dhcp_client_id, "DHCP client ID"),
        )


@dataclass(frozen=True)
class AuthenticatedCameraObservation:
    """An endpoint whose camera application identity has been authenticated.

    A discovery adapter must verify the camera-specific credential or device
    identity before constructing this value.  A matching MAC address, DHCP
    client ID, or previous IP address is not sufficient authentication.
    """

    camera_id: str
    endpoint: ObservedCameraEndpoint

    def __post_init__(self) -> None:
        object.__setattr__(self, "camera_id", _camera_id(self.camera_id))
        if not isinstance(self.endpoint, ObservedCameraEndpoint):
            raise CameraEndpointError("Authenticated camera endpoint is invalid.")


@dataclass(frozen=True)
class EndpointReconciliation:
    """Identity-level changes made from one complete discovery snapshot."""

    added: tuple[str, ...]
    updated: tuple[str, ...]
    removed: tuple[str, ...]
    unchanged: tuple[str, ...]
    ignored_unauthorized: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


class CameraEndpointRegistry:
    """Persist candidate and committed routes by cryptographic camera identity."""

    STATE_VERSION: Final[int] = 3

    def __init__(self, state_path: str | os.PathLike[str]) -> None:
        self.state_path = Path(state_path)
        self._lock = RLock()
        self._endpoints, self._candidates, self._conflicts = self._load()

    @property
    def endpoints(self) -> dict[str, ObservedCameraEndpoint]:
        """Return a snapshot keyed only by authenticated camera identity."""

        with self._lock:
            return dict(self._endpoints)

    @property
    def candidates(self) -> dict[str, ObservedCameraEndpoint]:
        """Return uncommitted routes awaiting media-path verification."""

        with self._lock:
            return dict(self._candidates)

    @property
    def conflicts(self) -> dict[str, ObservedCameraEndpoint]:
        """Return quarantined routes claiming an already-live identity."""

        with self._lock:
            return dict(self._conflicts)

    def endpoint_for(self, camera_id: str) -> ObservedCameraEndpoint | None:
        """Resolve the current endpoint for an application camera identity."""

        with self._lock:
            return self._endpoints.get(_camera_id(camera_id))

    def candidate_for(self, camera_id: str) -> ObservedCameraEndpoint | None:
        """Return a proposed route without exposing it as the working route."""

        with self._lock:
            return self._candidates.get(_camera_id(camera_id))

    def conflict_for(self, camera_id: str) -> ObservedCameraEndpoint | None:
        """Return the quarantined duplicate route for an identity, if any."""

        with self._lock:
            return self._conflicts.get(_camera_id(camera_id))

    def observe(
        self,
        *,
        authorized_camera_ids: Iterable[str],
        observation: AuthenticatedCameraObservation,
    ) -> EndpointReconciliation:
        """Atomically stage one authenticated camera-initiated observation.

        Unlike :meth:`reconcile`, this is not a complete network scan. Cameras
        absent from this heartbeat remain unchanged, which lets independently
        reconnecting home-LAN cameras propose their DHCP location without
        making unrelated cameras disappear or replacing a working route.
        """

        with self._lock:
            return self._observe(
                authorized_camera_ids=authorized_camera_ids,
                observation=observation,
            )

    def _observe(
        self,
        *,
        authorized_camera_ids: Iterable[str],
        observation: AuthenticatedCameraObservation,
    ) -> EndpointReconciliation:
        if isinstance(authorized_camera_ids, (str, bytes)):
            raise CameraEndpointError("Authorized camera IDs must be a collection.")
        try:
            authorized = {_camera_id(value) for value in authorized_camera_ids}
        except TypeError as exc:
            raise CameraEndpointError(
                "Authorized camera IDs must be a collection."
            ) from exc
        if not isinstance(observation, AuthenticatedCameraObservation):
            raise CameraEndpointError("Camera observation is not authenticated.")
        camera_id = observation.camera_id
        if camera_id not in authorized:
            return EndpointReconciliation(
                added=(),
                updated=(),
                removed=(),
                unchanged=(),
                ignored_unauthorized=(camera_id,),
            )

        route = (observation.endpoint.ip_address, observation.endpoint.port)
        for owner, endpoint in {**self._endpoints, **self._candidates}.items():
            if owner != camera_id and (endpoint.ip_address, endpoint.port) == route:
                raise CameraEndpointConflictError(
                    "One endpoint was authenticated as multiple camera identities."
                )

        previous = self._endpoints.get(camera_id)
        if previous == observation.endpoint:
            if camera_id in self._candidates:
                candidates = dict(self._candidates)
                candidates.pop(camera_id, None)
                self._save(self._endpoints, candidates, self._conflicts)
                self._candidates = candidates
            return EndpointReconciliation(
                added=(),
                updated=(),
                removed=(),
                unchanged=(camera_id,),
                ignored_unauthorized=(),
            )

        if self._candidates.get(camera_id) == observation.endpoint:
            return EndpointReconciliation(
                added=(),
                updated=(),
                removed=(),
                unchanged=(camera_id,),
                ignored_unauthorized=(),
            )

        candidates = dict(self._candidates)
        candidates[camera_id] = observation.endpoint
        self._save(self._endpoints, candidates, self._conflicts)
        self._candidates = candidates
        return EndpointReconciliation(
            added=(camera_id,) if previous is None else (),
            updated=(camera_id,) if previous is not None else (),
            removed=(),
            unchanged=(),
            ignored_unauthorized=(),
        )

    def promote(
        self, *, camera_id: str, endpoint: ObservedCameraEndpoint
    ) -> EndpointReconciliation:
        """Commit exactly the candidate that completed route verification."""

        with self._lock:
            return self._promote(camera_id=camera_id, endpoint=endpoint)

    def _promote(
        self, *, camera_id: str, endpoint: ObservedCameraEndpoint
    ) -> EndpointReconciliation:
        selected_id = _camera_id(camera_id)
        if not isinstance(endpoint, ObservedCameraEndpoint):
            raise CameraEndpointError("Camera endpoint candidate is invalid.")
        if self._candidates.get(selected_id) != endpoint:
            raise CameraEndpointConflictError(
                "Camera endpoint candidate changed during verification."
            )
        route = (endpoint.ip_address, endpoint.port)
        for owner, current in self._endpoints.items():
            if owner != selected_id and (current.ip_address, current.port) == route:
                raise CameraEndpointConflictError(
                    "One endpoint was authenticated as multiple camera identities."
                )
        previous = self._endpoints.get(selected_id)
        endpoints = dict(self._endpoints)
        endpoints[selected_id] = endpoint
        candidates = dict(self._candidates)
        candidates.pop(selected_id, None)
        conflicts = dict(self._conflicts)
        conflicts.pop(selected_id, None)
        self._save(endpoints, candidates, conflicts)
        self._endpoints = endpoints
        self._candidates = candidates
        self._conflicts = conflicts
        return EndpointReconciliation(
            added=(selected_id,) if previous is None else (),
            updated=(selected_id,) if previous is not None and previous != endpoint else (),
            removed=(),
            unchanged=(selected_id,) if previous == endpoint else (),
            ignored_unauthorized=(),
        )

    def reject(
        self, *, camera_id: str, endpoint: ObservedCameraEndpoint
    ) -> bool:
        """Discard a failed candidate while preserving the committed route."""

        with self._lock:
            return self._reject(camera_id=camera_id, endpoint=endpoint)

    def _reject(
        self, *, camera_id: str, endpoint: ObservedCameraEndpoint
    ) -> bool:
        selected_id = _camera_id(camera_id)
        if not isinstance(endpoint, ObservedCameraEndpoint):
            raise CameraEndpointError("Camera endpoint candidate is invalid.")
        if self._candidates.get(selected_id) != endpoint:
            return False
        candidates = dict(self._candidates)
        candidates.pop(selected_id, None)
        self._save(self._endpoints, candidates, self._conflicts)
        self._candidates = candidates
        return True

    def quarantine(
        self, *, camera_id: str, endpoint: ObservedCameraEndpoint
    ) -> None:
        """Keep a second live route out of service without replacing the first."""

        selected_id = _camera_id(camera_id)
        if not isinstance(endpoint, ObservedCameraEndpoint):
            raise CameraEndpointError("Camera conflict endpoint is invalid.")
        with self._lock:
            current = self._endpoints.get(selected_id)
            if current is None or current == endpoint:
                raise CameraEndpointError("Camera conflict endpoint is invalid.")
            route = (endpoint.ip_address, endpoint.port)
            for owner, active in self._endpoints.items():
                if owner != selected_id and (active.ip_address, active.port) == route:
                    raise CameraEndpointConflictError(
                        "One endpoint was authenticated as multiple camera identities."
                    )
            conflicts = dict(self._conflicts)
            conflicts[selected_id] = endpoint
            candidates = dict(self._candidates)
            candidates.pop(selected_id, None)
            self._save(self._endpoints, candidates, conflicts)
            self._candidates = candidates
            self._conflicts = conflicts

    def clear_conflict(self, camera_id: str) -> bool:
        """Clear a resolved quarantine; a still-live duplicate will be re-added."""

        selected_id = _camera_id(camera_id)
        with self._lock:
            if selected_id not in self._conflicts:
                return False
            conflicts = dict(self._conflicts)
            conflicts.pop(selected_id)
            self._save(self._endpoints, self._candidates, conflicts)
            self._conflicts = conflicts
            return True

    def reconcile(
        self,
        *,
        authorized_camera_ids: Iterable[str],
        observations: Iterable[AuthenticatedCameraObservation],
    ) -> EndpointReconciliation:
        """Atomically replace routes from one complete authenticated scan.

        Cameras absent from the snapshot become unavailable.  Observations for
        identities outside the recorder's active manifest are reported and
        ignored, so network metadata alone can never enroll a camera.
        """

        with self._lock:
            return self._reconcile(
                authorized_camera_ids=authorized_camera_ids,
                observations=observations,
            )

    def _reconcile(
        self,
        *,
        authorized_camera_ids: Iterable[str],
        observations: Iterable[AuthenticatedCameraObservation],
    ) -> EndpointReconciliation:
        if isinstance(authorized_camera_ids, (str, bytes)):
            raise CameraEndpointError("Authorized camera IDs must be a collection.")
        try:
            authorized = {_camera_id(value) for value in authorized_camera_ids}
        except TypeError as exc:
            raise CameraEndpointError(
                "Authorized camera IDs must be a collection."
            ) from exc

        proposed: dict[str, ObservedCameraEndpoint] = {}
        endpoint_owners: dict[tuple[str, int], str] = {}
        ignored: set[str] = set()
        try:
            candidates = iter(observations)
        except TypeError as exc:
            raise CameraEndpointError(
                "Camera observations must be a collection."
            ) from exc
        for observation in candidates:
            if not isinstance(observation, AuthenticatedCameraObservation):
                raise CameraEndpointError("Camera observation is not authenticated.")
            camera_id = observation.camera_id
            if camera_id not in authorized:
                ignored.add(camera_id)
                continue
            existing = proposed.get(camera_id)
            if existing is not None and existing != observation.endpoint:
                raise CameraEndpointConflictError(
                    f"Camera {camera_id} was authenticated at multiple endpoints."
                )
            route = (observation.endpoint.ip_address, observation.endpoint.port)
            owner = endpoint_owners.get(route)
            if owner is not None and owner != camera_id:
                raise CameraEndpointConflictError(
                    "One endpoint was authenticated as multiple camera identities."
                )
            proposed[camera_id] = observation.endpoint
            endpoint_owners[route] = camera_id

        previous_ids = set(self._endpoints)
        proposed_ids = set(proposed)
        added = tuple(sorted(proposed_ids - previous_ids))
        removed = tuple(sorted(previous_ids - proposed_ids))
        updated = tuple(
            sorted(
                camera_id
                for camera_id in previous_ids & proposed_ids
                if self._endpoints[camera_id] != proposed[camera_id]
            )
        )
        unchanged = tuple(sorted((previous_ids & proposed_ids) - set(updated)))
        result = EndpointReconciliation(
            added=added,
            updated=updated,
            removed=removed,
            unchanged=unchanged,
            ignored_unauthorized=tuple(sorted(ignored)),
        )
        if result.changed:
            self._save(proposed, self._candidates, self._conflicts)
            self._endpoints = proposed
        return result

    def _load(
        self,
    ) -> tuple[
        dict[str, ObservedCameraEndpoint],
        dict[str, ObservedCameraEndpoint],
        dict[str, ObservedCameraEndpoint],
    ]:
        if not self.state_path.exists():
            return {}, {}, {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (
                not isinstance(raw, dict)
                or raw.get("schema_version") not in {1, 2, self.STATE_VERSION}
            ):
                raise CameraEndpointStateError(
                    "Camera endpoint state version is not supported."
                )
            saved_endpoints = raw.get("endpoints")
            if not isinstance(saved_endpoints, dict):
                raise CameraEndpointStateError("Camera endpoint state is incomplete.")
            endpoints = self._parse_endpoints(saved_endpoints)
            candidates = (
                {}
                if raw.get("schema_version") == 1
                else self._parse_endpoints(raw.get("candidates"))
            )
            conflicts = (
                {}
                if raw.get("schema_version") in {1, 2}
                else self._parse_endpoints(raw.get("conflicts"))
            )
            active_routes = {
                (endpoint.ip_address, endpoint.port): camera_id
                for camera_id, endpoint in endpoints.items()
            }
            for camera_id, endpoint in candidates.items():
                owner = active_routes.get((endpoint.ip_address, endpoint.port))
                if owner is not None and owner != camera_id:
                    raise CameraEndpointError("Saved camera endpoints are ambiguous.")
            for camera_id, endpoint in conflicts.items():
                current = endpoints.get(camera_id)
                if current is None or current == endpoint:
                    raise CameraEndpointError("Saved camera conflict is invalid.")
                owner = active_routes.get((endpoint.ip_address, endpoint.port))
                if owner is not None:
                    raise CameraEndpointError("Saved camera endpoints are ambiguous.")
            return endpoints, candidates, conflicts
        except CameraEndpointStateError:
            raise
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            CameraEndpointError,
        ) as exc:
            raise CameraEndpointStateError(
                "Camera endpoint state could not be read."
            ) from exc

    @staticmethod
    def _parse_endpoints(
        saved_endpoints: object,
    ) -> dict[str, ObservedCameraEndpoint]:
        if not isinstance(saved_endpoints, dict):
            raise CameraEndpointError("Saved camera endpoint state is incomplete.")
        endpoints: dict[str, ObservedCameraEndpoint] = {}
        endpoint_owners: dict[tuple[str, int], str] = {}
        for saved_id, saved_endpoint in saved_endpoints.items():
            camera_id = _camera_id(saved_id)
            if camera_id != saved_id or not isinstance(saved_endpoint, dict):
                raise CameraEndpointError("Saved camera endpoint is invalid.")
            endpoint = ObservedCameraEndpoint(
                ip_address=saved_endpoint["ip_address"],
                port=saved_endpoint["port"],
                mac_address=saved_endpoint.get("mac_address"),
                dhcp_client_id=saved_endpoint.get("dhcp_client_id"),
            )
            route = (endpoint.ip_address, endpoint.port)
            if route in endpoint_owners:
                raise CameraEndpointError("Saved camera endpoints are ambiguous.")
            endpoints[camera_id] = endpoint
            endpoint_owners[route] = camera_id
        return endpoints

    def _save(
        self,
        endpoints: dict[str, ObservedCameraEndpoint],
        candidates: dict[str, ObservedCameraEndpoint],
        conflicts: dict[str, ObservedCameraEndpoint],
    ) -> None:
        def serialized(
            values: dict[str, ObservedCameraEndpoint],
        ) -> dict[str, dict[str, object]]:
            return {
                camera_id: {
                    "ip_address": endpoint.ip_address,
                    "port": endpoint.port,
                    "mac_address": endpoint.mac_address,
                    "dhcp_client_id": endpoint.dhcp_client_id,
                }
                for camera_id, endpoint in sorted(values.items())
            }

        document = {
            "schema_version": self.STATE_VERSION,
            "endpoints": serialized(endpoints),
            "candidates": serialized(candidates),
            "conflicts": serialized(conflicts),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            dir=self.state_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.state_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


__all__ = [
    "AuthenticatedCameraObservation",
    "CameraEndpointConflictError",
    "CameraEndpointError",
    "CameraEndpointRegistry",
    "CameraEndpointStateError",
    "EndpointReconciliation",
    "ObservedCameraEndpoint",
]
