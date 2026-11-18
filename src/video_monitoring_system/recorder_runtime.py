"""Concurrent recording cycles over injected storage and camera adapters."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .recorder import RecorderService
from .recording import (
    MjpegFrameSource,
    RecorderCaptureService,
    RecordingCaptureError,
    RecordingStorage,
)


@dataclass(frozen=True, slots=True)
class CameraCaptureResult:
    camera_id: str
    state: str
    recording_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "camera_id": self.camera_id,
            "state": self.state,
        }
        if self.recording_id is not None:
            result["recording_id"] = self.recording_id
        return result


class RecorderRuntime:
    """Run one bounded segment for every authorized camera concurrently."""

    def __init__(self, recorder: RecorderService, storage: RecordingStorage) -> None:
        if not isinstance(recorder, RecorderService):
            raise TypeError("Recorder state is required.")
        if not isinstance(storage, RecordingStorage):
            raise TypeError("Recording storage is required.")
        self.recorder = recorder
        self.capture = RecorderCaptureService(recorder, storage)

    def capture_cycle(
        self,
        sources: tuple[MjpegFrameSource, ...],
        *,
        managed_camera_ids: frozenset[str] = frozenset(),
        frame_count: int = RecorderCaptureService.DEFAULT_FRAME_COUNT,
        maximum_bytes: int = RecorderCaptureService.DEFAULT_MAXIMUM_BYTES,
        timeout_seconds: float = RecorderCaptureService.DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        if self.recorder.state["recording"]["mode"] != "continuous":
            return {"state": "idle", "cameras": []}

        authorized = self.recorder.recording_camera_ids
        managed = authorized & managed_camera_ids
        legacy_authorized = authorized - managed
        selected: dict[str, MjpegFrameSource] = {}
        for source in sources:
            try:
                camera_id = source.camera_id
            except Exception as error:
                raise RecordingCaptureError(
                    "Authenticated camera stream identity is unavailable."
                ) from error
            if not isinstance(camera_id, str) or not camera_id:
                raise RecordingCaptureError(
                    "Authenticated camera stream identity is unavailable."
                )
            if camera_id in selected:
                raise RecordingCaptureError(
                    "Camera stream adapter returned a duplicate identity."
                )
            selected[camera_id] = source

        results = [CameraCaptureResult(camera_id, "managed") for camera_id in managed]
        results.extend(
            CameraCaptureResult(camera_id, "unavailable")
            for camera_id in legacy_authorized - set(selected)
        )
        eligible = tuple(
            (camera_id, source)
            for camera_id, source in selected.items()
            if camera_id in legacy_authorized
        )
        if eligible:
            with ThreadPoolExecutor(max_workers=min(4, len(eligible))) as pool:
                pending = {
                    pool.submit(
                        self.capture.capture_segment,
                        source=source,
                        frame_count=frame_count,
                        maximum_bytes=maximum_bytes,
                        timeout_seconds=timeout_seconds,
                    ): camera_id
                    for camera_id, source in eligible
                }
                for future in as_completed(pending):
                    camera_id = pending[future]
                    try:
                        recording = future.result()
                    except Exception:
                        # Camera and transport details are intentionally redacted.
                        results.append(CameraCaptureResult(camera_id, "needs_attention"))
                    else:
                        results.append(
                            CameraCaptureResult(
                                camera_id, "recorded", recording.id
                            )
                        )

        ordered = sorted(results, key=lambda item: item.camera_id)
        states = {item.state for item in ordered}
        state = (
            "recording"
            if states & {"recorded", "managed"}
            else "needs_attention"
            if states
            else "waiting"
        )
        return {
            "state": state,
            "cameras": [item.to_dict() for item in ordered],
        }


__all__ = ["CameraCaptureResult", "RecorderRuntime"]
