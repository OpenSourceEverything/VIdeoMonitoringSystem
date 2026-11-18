"""Transactional downstream camera-route switching.

Candidate identity and direct media checks happen before this boundary.  This
module coordinates the remaining engine-facing transaction and deliberately
knows nothing about go2rtc URLs or camera credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from collections.abc import Callable
from typing import Protocol

from .camera_endpoints import ObservedCameraEndpoint
from .camera_route_relay import CameraRouteRelayState
from .video_engine import FrigateEngine


class CameraRouteHandoffError(RuntimeError):
    """The downstream route could not be switched or safely restored."""


class CameraRouteSwitchAdapter(Protocol):
    """Engine-specific operations used by one route transaction."""

    def stage(
        self, *, camera_id: str, endpoint: ObservedCameraEndpoint
    ) -> object: ...

    def verify_staged(self, *, camera_id: str, handle: object) -> None: ...

    def switch(
        self, *, camera_id: str, handle: object
    ) -> None: ...

    def verify_active(self, *, camera_id: str) -> None: ...

    def restore(
        self,
        *,
        camera_id: str,
        previous: ObservedCameraEndpoint | None,
        handle: object,
    ) -> None: ...

    def commit(self, *, camera_id: str, handle: object) -> None: ...

    def discard(self, *, camera_id: str, handle: object) -> None: ...


@dataclass(slots=True)
class CameraRouteTransaction:
    """A switched route awaiting durable registry promotion."""

    adapter: CameraRouteSwitchAdapter
    camera_id: str
    previous: ObservedCameraEndpoint | None
    candidate: ObservedCameraEndpoint
    handle: object
    _closed: bool = False

    def commit(self) -> None:
        """Finish temporary staging after the caller commits the registry."""

        if self._closed:
            raise CameraRouteHandoffError("Camera route transaction is closed.")
        try:
            self.adapter.commit(camera_id=self.camera_id, handle=self.handle)
            self.adapter.discard(camera_id=self.camera_id, handle=self.handle)
        except Exception as error:
            raise CameraRouteHandoffError(
                "Camera route could not be finalized."
            ) from error
        self._closed = True

    def rollback(self) -> None:
        """Restore and prove the previous downstream route."""

        if self._closed:
            return
        try:
            self.adapter.restore(
                camera_id=self.camera_id,
                previous=self.previous,
                handle=self.handle,
            )
            if self.previous is not None:
                self.adapter.verify_active(camera_id=self.camera_id)
        except Exception as error:
            raise CameraRouteHandoffError(
                "The previous camera route could not be restored."
            ) from error
        finally:
            try:
                self.adapter.discard(camera_id=self.camera_id, handle=self.handle)
            except Exception:
                pass
            self._closed = True


class TransactionalCameraRouteHandoff:
    """Stage, switch, and prove one camera while preserving its old route."""

    def __init__(self, adapter: CameraRouteSwitchAdapter) -> None:
        self.adapter = adapter

    def prepare(
        self,
        *,
        camera_id: str,
        previous: ObservedCameraEndpoint | None,
        candidate: ObservedCameraEndpoint,
    ) -> CameraRouteTransaction:
        if not isinstance(candidate, ObservedCameraEndpoint):
            raise CameraRouteHandoffError("Camera route candidate is invalid.")
        if previous is not None and not isinstance(previous, ObservedCameraEndpoint):
            raise CameraRouteHandoffError("Previous camera route is invalid.")
        handle: object | None = None
        switched = False
        try:
            handle = self.adapter.stage(camera_id=camera_id, endpoint=candidate)
            self.adapter.verify_staged(camera_id=camera_id, handle=handle)
            self.adapter.switch(camera_id=camera_id, handle=handle)
            switched = True
            self.adapter.verify_active(camera_id=camera_id)
        except Exception as error:
            restoration_error: Exception | None = None
            if handle is not None and switched:
                try:
                    self.adapter.restore(
                        camera_id=camera_id,
                        previous=previous,
                        handle=handle,
                    )
                    if previous is not None:
                        self.adapter.verify_active(camera_id=camera_id)
                except Exception as restore_error:
                    restoration_error = restore_error
            if handle is not None:
                try:
                    self.adapter.discard(camera_id=camera_id, handle=handle)
                except Exception:
                    pass
            if restoration_error is not None:
                raise CameraRouteHandoffError(
                    "The previous camera route could not be restored."
                ) from restoration_error
            raise CameraRouteHandoffError(
                "Camera route could not be verified downstream."
            ) from error
        return CameraRouteTransaction(
            adapter=self.adapter,
            camera_id=camera_id,
            previous=previous,
            candidate=candidate,
            handle=handle,
        )


@dataclass(frozen=True, slots=True)
class FrigateRouteStage:
    camera_id: str
    endpoint: ObservedCameraEndpoint
    stream_name: str


class FrigateCameraRouteAdapter:
    """Switch one Frigate/go2rtc binding through the private route relay."""

    def __init__(
        self,
        engine: FrigateEngine,
        relay: CameraRouteRelayState,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        frame_interval_seconds: float = 0.25,
    ) -> None:
        if (
            not isinstance(frame_interval_seconds, (int, float))
            or isinstance(frame_interval_seconds, bool)
            or not 0 <= float(frame_interval_seconds) <= 5
        ):
            raise ValueError("Camera route frame interval is invalid.")
        self.engine = engine
        self.relay = relay
        self._sleeper = sleeper
        self._frame_interval_seconds = float(frame_interval_seconds)

    def stage(
        self, *, camera_id: str, endpoint: ObservedCameraEndpoint
    ) -> FrigateRouteStage:
        self._working_stream(camera_id)
        digest = hashlib.sha256(
            f"{camera_id}\0{endpoint.ip_address}\0{endpoint.port}".encode("utf-8")
        ).hexdigest()[:16]
        handle = FrigateRouteStage(
            camera_id=camera_id,
            endpoint=endpoint,
            stream_name=f"ose_stage_{digest}",
        )
        self.engine.replace_private_route_source(
            stream_name=handle.stream_name,
            source=self.engine.private_route_source(
                camera_id=camera_id,
                selection="candidate",
            ),
        )
        return handle

    def verify_staged(self, *, camera_id: str, handle: object) -> None:
        stage = self._stage(camera_id, handle)
        self._verify_decodable_route(stage.stream_name)

    def switch(self, *, camera_id: str, handle: object) -> None:
        stage = self._stage(camera_id, handle)
        self.relay.activate_candidate(camera_id=camera_id, endpoint=stage.endpoint)
        try:
            self.engine.replace_private_route_source(
                stream_name=self._working_stream(camera_id),
                source=self.engine.private_route_source(
                    camera_id=camera_id,
                    selection="active",
                ),
            )
        except Exception:
            self.relay.restore_committed(camera_id=camera_id)
            raise

    def verify_active(self, *, camera_id: str) -> None:
        self._verify_decodable_route(self._working_stream(camera_id))
        self.engine.read_snapshot(camera_id=camera_id)
        self._sleeper(self._frame_interval_seconds)
        self.engine.read_snapshot(camera_id=camera_id)

    def restore(
        self,
        *,
        camera_id: str,
        previous: ObservedCameraEndpoint | None,
        handle: object,
    ) -> None:
        self._stage(camera_id, handle)
        self.relay.restore_committed(camera_id=camera_id)
        working = self._working_stream(camera_id)
        if previous is None:
            self.engine.delete_private_route_source(stream_name=working)
            return
        self.engine.replace_private_route_source(
            stream_name=working,
            source=self.engine.private_route_source(
                camera_id=camera_id,
                selection="active",
            ),
        )

    def commit(self, *, camera_id: str, handle: object) -> None:
        self._stage(camera_id, handle)
        # Registry promotion has made the candidate the committed endpoint.
        # Clearing the temporary selection leaves the stable relay URL on the
        # same physical route and makes reboot recovery deterministic.
        self.relay.restore_committed(camera_id=camera_id)

    def discard(self, *, camera_id: str, handle: object) -> None:
        stage = self._stage(camera_id, handle)
        self.engine.delete_private_route_source(stream_name=stage.stream_name)

    def _verify_decodable_route(self, stream_name: str) -> None:
        """Require decoded snapshots without mistaking a still scene for a fault."""

        self.engine.read_private_route_frame(stream_name=stream_name)
        self._sleeper(self._frame_interval_seconds)
        self.engine.read_private_route_frame(stream_name=stream_name)

    def _working_stream(self, camera_id: str) -> str:
        try:
            return self.engine.camera_bindings[camera_id]
        except (KeyError, TypeError):
            raise CameraRouteHandoffError(
                "Camera is not bound to the video engine."
            ) from None

    @staticmethod
    def _stage(camera_id: str, handle: object) -> FrigateRouteStage:
        if not isinstance(handle, FrigateRouteStage) or handle.camera_id != camera_id:
            raise CameraRouteHandoffError("Camera route stage is invalid.")
        return handle
