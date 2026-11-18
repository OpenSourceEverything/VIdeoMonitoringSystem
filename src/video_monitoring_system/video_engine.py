"""Narrow media-engine boundary owned by the OSE application.

The product UI must not depend on an engine database, filesystem layout, or
customer-facing web interface. This module translates the small, explicit set
of OSE operations from a loopback-only Frigate API into stable product objects.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit

from .provisioning import secure_write_text


class VideoEngineError(RuntimeError):
    """The private video engine was unavailable or returned unsafe data."""


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """Engine-independent event metadata safe for the OSE application."""

    id: str
    camera_id: str
    started_at: datetime
    ended_at: datetime | None
    severity: str
    labels: tuple[str, ...] = ()
    zones: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": (
                None if self.ended_at is None else self.ended_at.isoformat()
            ),
            "severity": self.severity,
            "labels": self.labels,
            "zones": self.zones,
        }


@dataclass(frozen=True, slots=True)
class EngineSavedClip:
    """A permanent Frigate export owned and presented by OSE."""

    id: str
    camera_id: str
    created_at: datetime
    in_progress: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "created_at": self.created_at.isoformat(),
            "in_progress": self.in_progress,
        }


class VideoEngine(Protocol):
    """Narrow contract consumed by the OSE UI and notification service."""

    def status(self) -> Mapping[str, object]: ...

    def live_metrics(self, *, camera_id: str) -> Mapping[str, object]: ...

    def list_events(
        self, *, camera_ids: Sequence[str] = (), limit: int = 100
    ) -> Sequence[EngineEvent]: ...

    def read_snapshot(self, *, camera_id: str) -> bytes: ...

    def read_event_thumbnail(self, *, event_id: str) -> bytes: ...

    def read_event_preview(self, *, event_id: str) -> bytes: ...

    def read_event_vod(
        self, *, event_id: str, resource: str
    ) -> tuple[bytes, str]: ...

    def save_event(self, *, event_id: str) -> Mapping[str, object]: ...

    def delete_event(self, *, event_id: str) -> None: ...

    def list_saved_clips(
        self, *, camera_ids: Sequence[str] = (), limit: int = 100
    ) -> Sequence[EngineSavedClip]: ...

    def read_saved_clip(self, *, clip_id: str) -> bytes: ...

    def delete_saved_clip(self, *, clip_id: str) -> None: ...

    def negotiate_live_webrtc(self, *, camera_id: str, offer: str) -> str: ...


_Transport = Callable[[str, str, int], tuple[bytes, str]]
_SignalTransport = Callable[[str, bytes, int], tuple[bytes, str]]
_MutationTransport = Callable[[str, bytes, int], tuple[bytes, str]]
_RouteTransport = Callable[[str, str, int], tuple[bytes, str]]
_FRIGATE_CAMERA = re.compile(r"[A-Za-z0-9_-]{1,80}")
_FRIGATE_EVENT = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
_FRIGATE_VOD_RESOURCE = re.compile(
    r"(?:master|index(?:-v[0-9]+)?)\.m3u8|[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.(?:m4s|mp4|ts)"
)
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_FRIGATE_EXPORT_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.mp4")
_PRIVATE_ROUTE_STREAM = re.compile(r"[A-Za-z0-9_-]{1,80}")
_PRIVATE_RELAY_PATH = re.compile(
    r"/v1/cameras/CAM-[0-9A-F]{16,64}/(?:active|candidate)/(?:main|detection)"
)
_PRIVATE_RELAY_HOST = "ose-camera-route-relay.internal"
_PRIVATE_RELAY_PORT = 9081
_FRIGATE_GO2RTC_LOOPBACK_PORT = 15001
VIDEO_ENGINE_CONFIG_FILE = "video-engine.json"


class FrigateEngine:
    """Translate Frigate's private HTTP API into the OSE engine contract.

    Frigate's unauthenticated internal API is intentionally fixed to loopback.
    The constructor accepts only a port, never a hostname or URL.  OSE remains
    the authentication, remote-access, identity, and customer-facing boundary.
    """

    def __init__(
        self,
        camera_bindings: Mapping[str, str],
        *,
        port: int = 5000,
        go2rtc_port: int = _FRIGATE_GO2RTC_LOOPBACK_PORT,
        timeout_seconds: float = 5.0,
        transport: _Transport | None = None,
        go2rtc_transport: _Transport | None = None,
        signal_transport: _SignalTransport | None = None,
        mutation_transport: _MutationTransport | None = None,
        route_transport: _RouteTransport | None = None,
        route_relay_enabled: bool = False,
    ) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("Frigate port is invalid.")
        if (
            not isinstance(go2rtc_port, int)
            or isinstance(go2rtc_port, bool)
            or not 1 <= go2rtc_port <= 65535
        ):
            raise ValueError("go2rtc port is invalid.")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= 30
        ):
            raise ValueError("Frigate timeout is invalid.")
        if not isinstance(route_relay_enabled, bool):
            raise ValueError("Frigate route relay setting is invalid.")
        bindings: dict[str, str] = {}
        reverse: dict[str, str] = {}
        for camera_id, engine_name in camera_bindings.items():
            if (
                not isinstance(camera_id, str)
                or not camera_id
                or len(camera_id) > 160
                or not isinstance(engine_name, str)
                or not _FRIGATE_CAMERA.fullmatch(engine_name)
            ):
                raise ValueError("Frigate camera binding is invalid.")
            if engine_name in reverse:
                raise ValueError("Frigate camera bindings must be unique.")
            bindings[camera_id] = engine_name
            reverse[engine_name] = camera_id
        self.camera_bindings = bindings
        self._ose_camera_ids = reverse
        self.port = port
        self.go2rtc_port = go2rtc_port
        self.route_relay_enabled = route_relay_enabled
        self.timeout_seconds = float(timeout_seconds)
        self._transport = transport or self._request
        self._go2rtc_transport = go2rtc_transport or self._go2rtc_request
        self._signal_transport = signal_transport or self._signal_request
        self._mutation_transport = mutation_transport or self._mutation_request
        self._route_transport = route_transport or self._route_request

    def status(self) -> Mapping[str, object]:
        body, _ = self._transport("/api/version", "application/json", 1024)
        try:
            text = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise VideoEngineError("Frigate returned an invalid version.") from error
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = text.strip()
        version = decoded if isinstance(decoded, str) else None
        if version is None and isinstance(decoded, Mapping):
            candidate = decoded.get("version")
            version = candidate if isinstance(candidate, str) else None
        if version is None or not _VERSION.fullmatch(version):
            raise VideoEngineError("Frigate returned an invalid version.")
        return {"state": "ready", "engine": "frigate", "version": version}

    def live_metrics(self, *, camera_id: str) -> Mapping[str, object]:
        """Return bounded recorder-observed health for one OSE camera."""

        engine_name = self._camera_name(camera_id)
        payload = self._json("/api/stats", maximum_bytes=2_000_000)
        cameras = payload.get("cameras") if isinstance(payload, Mapping) else None
        metrics = cameras.get(engine_name) if isinstance(cameras, Mapping) else None
        if not isinstance(metrics, Mapping):
            raise VideoEngineError("Frigate returned invalid camera metrics.")
        result: dict[str, object] = {}
        for source, target in (
            # Frigate camera_fps is frames entering its detect pipeline. The
            # separate record stream and disk persistence are not proven by it.
            ("camera_fps", "detect_input_fps"),
            ("skipped_fps", "skipped_fps"),
        ):
            value = metrics.get(source)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not 0 <= value <= 1_000
            ):
                raise VideoEngineError("Frigate returned invalid camera metrics.")
            result[target] = round(float(value), 1)
        quality = metrics.get("connection_quality")
        if quality in {"excellent", "good", "fair", "poor", "unusable"}:
            result["connection_quality"] = quality
        for source, target in (
            ("stalls_last_hour", "source_stalls_last_hour"),
            ("process_fps", "processed_fps"),
            ("detection_fps", "object_detection_fps"),
            ("reconnects_last_hour", "source_reconnects_last_hour"),
            ("expected_fps", "expected_fps"),
            ("ffmpeg_cpu", "camera_ffmpeg_cpu_percent"),
        ):
            value = metrics.get(source)
            if isinstance(value, str):
                try:
                    value = float(value.rstrip("%"))
                except ValueError:
                    value = None
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and 0 <= value <= 1_000_000
            ):
                result[target] = round(float(value), 1)
        detectors = payload.get("detectors", {})
        if isinstance(detectors, Mapping):
            speeds = [v.get("inference_speed") for v in detectors.values() if isinstance(v, Mapping)]
            speeds = [v for v in speeds if type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 60000]
            if speeds:
                result["detector_inference_ms"] = round(max(speeds), 1)
        cpu_usages = payload.get("cpu_usages") if isinstance(payload, Mapping) else None
        full_system = (
            cpu_usages.get("frigate.full_system")
            if isinstance(cpu_usages, Mapping)
            else None
        )
        system_cpu = full_system.get("cpu") if isinstance(full_system, Mapping) else None
        if isinstance(system_cpu, str):
            try:
                system_cpu = float(system_cpu.rstrip("%"))
            except ValueError:
                system_cpu = None
        if (
            isinstance(system_cpu, (int, float))
            and not isinstance(system_cpu, bool)
            and math.isfinite(float(system_cpu))
            and 0 <= system_cpu <= 100
        ):
            result["recorder_cpu_percent"] = round(float(system_cpu), 1)
        return result

    def recording_coverage(
        self, *, camera_id: str, after: int, before: int
    ) -> Mapping[str, object]:
        """Measure retained main-stream index coverage, not playback or live FPS.

        The bounded Frigate query is deliberately separate from the frequent
        live-metrics poll. Motion/event retention can leave expected gaps, so
        callers must not turn this result into an unconditional health verdict.
        """

        if (
            not isinstance(after, int)
            or isinstance(after, bool)
            or not isinstance(before, int)
            or isinstance(before, bool)
            or after < 0
            or not 0 < before - after <= 3600
        ):
            raise ValueError("Recording coverage window is invalid.")
        engine_name = self._camera_name(camera_id)
        rows = self._json(
            f"/api/{engine_name}/recordings?after={after}&before={before}",
            maximum_bytes=1_000_000,
        )
        if not isinstance(rows, list) or len(rows) > 1000:
            raise VideoEngineError("Frigate returned invalid recording coverage.")
        spans: list[tuple[float, float]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise VideoEngineError("Frigate returned invalid recording coverage.")
            start = row.get("start_time")
            end = row.get("end_time")
            size = row.get("segment_size")
            if any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                for value in (start, end, size)
            ) or start < 0 or end <= start or size <= 0:
                raise VideoEngineError("Frigate returned invalid recording coverage.")
            clipped = (max(float(start), after), min(float(end), before))
            if clipped[1] > clipped[0]:
                spans.append(clipped)
        spans.sort()
        cursor = float(after)
        indexed = 0.0
        largest_gap = 0.0
        for start, end in spans:
            largest_gap = max(largest_gap, max(0.0, start - cursor))
            if end > cursor:
                indexed += end - max(start, cursor)
                cursor = end
        largest_gap = max(largest_gap, float(before) - cursor)
        window = before - after
        return {
            "state": "indexed" if indexed > 0 else "none",
            "window_seconds": window,
            "indexed_seconds": round(indexed, 1),
            "gap_seconds": round(max(0.0, window - indexed), 1),
            "largest_gap_seconds": round(largest_gap, 1),
            "segment_count": len(spans),
            "latest_segment_end": max((end for _, end in spans), default=None),
            "basis": "retained main-stream index; file playability unverified",
        }

    def list_events(
        self, *, camera_ids: Sequence[str] = (), limit: int = 100
    ) -> Sequence[EngineEvent]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
        ):
            raise ValueError("Event limit is invalid.")
        selected = tuple(camera_ids) if camera_ids else tuple(self.camera_bindings)
        if any(camera_id not in self.camera_bindings for camera_id in selected):
            raise ValueError("Camera is not bound to the video engine.")
        if not selected:
            return ()
        query = urlencode(
            {
                "cameras": ",".join(self.camera_bindings[camera_id] for camera_id in selected),
                "limit": limit,
                # Frigate 0.18 returns an empty review list when `after` is
                # omitted.  A positive epoch bound includes retained events
                # without making the product guess the engine's retention.
                "after": 1,
            }
        )
        payload = self._json(f"/api/review?{query}", maximum_bytes=2_000_000)
        if not isinstance(payload, list):
            raise VideoEngineError("Frigate returned invalid event data.")
        events: list[EngineEvent] = []
        for item in payload:
            event = self._event(item)
            if event is not None and event.camera_id in selected:
                events.append(event)
        return tuple(events)

    def read_snapshot(self, *, camera_id: str) -> bytes:
        engine_name = self._camera_name(camera_id)
        body, content_type = self._transport(
            f"/api/{quote(engine_name, safe='')}/latest.jpg",
            "image/jpeg",
            8_000_000,
        )
        if content_type.split(";", 1)[0].strip().lower() != "image/jpeg":
            raise VideoEngineError("Frigate returned an invalid camera snapshot.")
        return body

    def read_event_preview(self, *, event_id: str) -> bytes:
        """Request the full recording interval around one review.

        Frigate's review clip endpoint can be a short preview even when the
        review lasts longer.  Resolve the review first so the caller cannot
        choose a camera or arbitrary recording time range.
        """

        raw_id = self._raw_event_id(event_id)
        event = self._review_event(raw_id)
        start, end = self._event_window(event)
        engine_name = self._camera_name(event.camera_id)
        body, content_type = self._transport(
            f"/api/{quote(engine_name, safe='')}/start/{start}/end/{end}/clip.mp4",
            "video/mp4",
            64_000_000,
        )
        if content_type.split(";", 1)[0].strip().lower() not in {
            "video/mp4",
            "application/octet-stream",
        }:
            raise VideoEngineError("Frigate returned an invalid event preview.")
        return body

    def read_event_vod(
        self, *, event_id: str, resource: str
    ) -> tuple[bytes, str]:
        """Read one bounded HLS resource for an authorized OSE review.

        Frigate review IDs and object-event IDs are different namespaces.  The
        VOD endpoint accepts an object event or a camera/time range, so resolve
        the review through the private API and construct the range internally.
        The browser can select only a validated basename, never an upstream URL
        or Frigate path.
        """

        raw_id = self._raw_event_id(event_id)
        selected_resource = self._vod_resource(resource)
        event = self._review_event(raw_id)
        engine_name = self._camera_name(event.camera_id)
        start, end = self._event_window(event)
        path = (
            f"/vod/{quote(engine_name, safe='')}/start/{start}/end/{end}/"
            f"{quote(selected_resource, safe='')}"
        )
        playlist = selected_resource.endswith(".m3u8")
        body, content_type = self._transport(
            path,
            "application/vnd.apple.mpegurl" if playlist else "video/mp4",
            256_000 if playlist else 16_000_000,
        )
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        allowed_types = (
            {"application/vnd.apple.mpegurl", "application/x-mpegurl"}
            if playlist
            else (
                {"video/mp2t", "application/octet-stream"}
                if selected_resource.endswith(".ts")
                else {"video/mp4", "application/octet-stream"}
            )
        )
        if normalized_type not in allowed_types:
            raise VideoEngineError("Frigate returned an invalid event VOD resource.")
        response_type = (
            "application/vnd.apple.mpegurl"
            if playlist
            else "video/mp2t" if selected_resource.endswith(".ts") else "video/mp4"
        )
        return body, response_type

    def read_event_thumbnail(self, *, event_id: str) -> bytes:
        raw_id = self._raw_event_id(event_id)
        review = self._json(
            f"/api/review/{quote(raw_id, safe='')}", maximum_bytes=2_000_000
        )
        if not isinstance(review, Mapping):
            raise VideoEngineError("Frigate returned invalid event metadata.")
        event = self._event(review)
        if event is None or event.id != f"frigate:{raw_id}":
            raise VideoEngineError("Video event is unavailable.")
        data = review.get("data")
        detections = data.get("detections") if isinstance(data, Mapping) else None
        if (
            not isinstance(detections, list)
            or not detections
            or not isinstance(detections[0], str)
            or not _FRIGATE_EVENT.fullmatch(detections[0])
        ):
            raise VideoEngineError("Frigate event thumbnail is unavailable.")
        body, content_type = self._transport(
            f"/api/events/{quote(detections[0], safe='')}/thumbnail.jpg",
            "image/jpeg",
            8_000_000,
        )
        if content_type.split(";", 1)[0].strip().lower() != "image/jpeg":
            raise VideoEngineError("Frigate returned an invalid event thumbnail.")
        return body

    def save_event(self, *, event_id: str) -> Mapping[str, object]:
        """Export one completed review outside normal recording retention."""

        raw_id = self._raw_event_id(event_id)
        event = self._review_event(raw_id)
        export_name = self._export_name(raw_id)
        for saved in self._export_documents(limit=200):
            if saved.get("name") == export_name:
                clip = self._saved_clip(saved)
                if clip is not None:
                    return {"state": "saved", "clip": clip.to_dict()}
        engine_name = self._camera_name(event.camera_id)
        start, end = self._event_window(event)
        result = self._json_mutation(
            f"/api/export/{quote(engine_name, safe='')}/start/{start}/end/{end}",
            {"source": "recordings", "name": export_name},
            maximum_bytes=64_000,
        )
        if not isinstance(result, Mapping):
            raise VideoEngineError("Frigate returned an invalid export result.")
        export_id = result.get("export_id")
        if not isinstance(export_id, str) or not _FRIGATE_EVENT.fullmatch(export_id):
            raise VideoEngineError("Frigate returned an invalid export ID.")
        return {"state": "saving", "clip_id": export_id}

    def delete_event(self, *, event_id: str) -> None:
        """Delete one authorized review and its rolling recording media."""

        raw_id = self._raw_event_id(event_id)
        review = self._json(
            f"/api/review/{quote(raw_id, safe='')}", maximum_bytes=2_000_000
        )
        event = self._event(review)
        if event is None or event.id != f"frigate:{raw_id}":
            raise VideoEngineError("Video event is unavailable.")
        self._json_mutation(
            "/api/reviews/delete", {"ids": [raw_id]}, maximum_bytes=64_000
        )

    def list_saved_clips(
        self, *, camera_ids: Sequence[str] = (), limit: int = 100
    ) -> Sequence[EngineSavedClip]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("Saved clip limit is invalid.")
        selected = set(camera_ids) if camera_ids else set(self.camera_bindings)
        if any(camera_id not in self.camera_bindings for camera_id in selected):
            raise ValueError("Camera is not bound to the video engine.")
        clips = []
        for document in self._export_documents(limit=200):
            clip = self._saved_clip(document)
            if clip is not None and clip.camera_id in selected:
                clips.append(clip)
        clips.sort(key=lambda item: item.created_at, reverse=True)
        return tuple(clips[:limit])

    def read_saved_clip(self, *, clip_id: str) -> bytes:
        document = self._export_document(clip_id)
        clip = self._saved_clip(document)
        if clip is None or clip.in_progress:
            raise VideoEngineError("Saved clip is not ready.")
        video_path = document.get("video_path")
        if not isinstance(video_path, str):
            raise VideoEngineError("Saved clip path is invalid.")
        filename = video_path.rsplit("/", 1)[-1]
        if (
            not video_path.startswith(("/media/frigate/exports/", "/exports/"))
            or not _FRIGATE_EXPORT_FILE.fullmatch(filename)
        ):
            raise VideoEngineError("Saved clip path is invalid.")
        body, content_type = self._transport(
            f"/exports/{quote(filename, safe='')}", "video/mp4", 512_000_000
        )
        if content_type.split(";", 1)[0].strip().lower() not in {
            "video/mp4",
            "application/octet-stream",
        }:
            raise VideoEngineError("Frigate returned an invalid saved clip.")
        return body

    def delete_saved_clip(self, *, clip_id: str) -> None:
        document = self._export_document(clip_id)
        if self._saved_clip(document) is None:
            raise VideoEngineError("Saved clip is unavailable.")
        self._json_mutation(
            "/api/exports/delete", {"ids": [clip_id]}, maximum_bytes=64_000
        )

    def negotiate_live_webrtc(self, *, camera_id: str, offer: str) -> str:
        """Exchange one bounded SDP offer without exposing the engine API."""

        engine_name = self._camera_name(camera_id)
        if (
            not isinstance(offer, str)
            or not 1 <= len(offer.encode("utf-8")) <= 64 * 1024
            or not offer.startswith("v=0")
        ):
            raise ValueError("WebRTC offer is invalid.")
        body, content_type = self._signal_transport(
            f"/api/go2rtc/webrtc?{urlencode({'src': engine_name})}",
            offer.encode("utf-8"),
            64 * 1024,
        )
        if content_type.split(";", 1)[0].strip().lower() != "application/sdp":
            raise VideoEngineError("Frigate returned an invalid WebRTC answer.")
        try:
            answer = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise VideoEngineError("Frigate returned an invalid WebRTC answer.") from error
        if not answer.startswith("v=0"):
            raise VideoEngineError("Frigate returned an invalid WebRTC answer.")
        return answer

    def replace_private_route_source(
        self, *, stream_name: str, source: str
    ) -> None:
        """Replace one go2rtc source using a credential-free relay URL only."""

        selected_name = self._private_stream_name(stream_name)
        selected_source = self._private_relay_source(source)
        path = "/api/go2rtc/streams/" + quote(selected_name, safe="") + "?" + urlencode(
            {"src": selected_source}
        )
        body, content_type = self._route_transport("PUT", path, 64_000)
        self._validate_route_mutation(body, content_type)

    def delete_private_route_source(self, *, stream_name: str) -> None:
        """Remove one temporary go2rtc source without changing another camera."""

        selected_name = self._private_stream_name(stream_name)
        path = "/api/go2rtc/streams/" + quote(selected_name, safe="")
        body, content_type = self._route_transport("DELETE", path, 64_000)
        self._validate_route_mutation(body, content_type)

    def read_private_route_frame(self, *, stream_name: str) -> bytes:
        """Read one decoded frame from a private staged or active go2rtc source."""

        selected_name = self._private_stream_name(stream_name)
        path = "/api/frame.jpeg?" + urlencode({"src": selected_name})
        body, content_type = self._go2rtc_transport(
            path, "image/jpeg", 8_000_000
        )
        if (
            content_type.split(";", 1)[0].strip().lower() != "image/jpeg"
            or not body.startswith(b"\xff\xd8")
            or not body.endswith(b"\xff\xd9")
        ):
            raise VideoEngineError("Private camera route returned an invalid frame.")
        return body

    def _go2rtc_request(
        self, path: str, accept: str, maximum_bytes: int
    ) -> tuple[bytes, str]:
        if (
            not path.startswith("/api/frame.jpeg?")
            or any(character in path for character in "\r\n")
        ):
            raise VideoEngineError("go2rtc request path is invalid.")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.go2rtc_port, timeout=self.timeout_seconds
        )
        try:
            connection.request("GET", path, headers={"Accept": accept})
            response = connection.getresponse()
            if response.status != 200:
                raise VideoEngineError("go2rtc could not complete the request.")
            body = response.read(maximum_bytes + 1)
            if len(body) > maximum_bytes:
                raise VideoEngineError("go2rtc response exceeded its safe limit.")
            return body, response.getheader("Content-Type", "")
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise VideoEngineError("go2rtc is unavailable.") from error
        finally:
            connection.close()

    @staticmethod
    def private_route_source(
        *, camera_id: str, selection: str, role: str = "main"
    ) -> str:
        if (
            not isinstance(camera_id, str)
            or re.fullmatch(r"CAM-[0-9A-F]{16,64}", camera_id) is None
            or selection not in {"active", "candidate"}
            or role not in {"main", "detection"}
        ):
            raise ValueError("Private camera relay source is invalid.")
        return (
            f"http://{_PRIVATE_RELAY_HOST}:{_PRIVATE_RELAY_PORT}"
            f"/v1/cameras/{camera_id}/{selection}/{role}"
        )

    def _camera_name(self, camera_id: str) -> str:
        try:
            return self.camera_bindings[camera_id]
        except (KeyError, TypeError):
            raise ValueError("Camera is not bound to the video engine.") from None

    @staticmethod
    def _private_stream_name(value: object) -> str:
        if not isinstance(value, str) or _PRIVATE_ROUTE_STREAM.fullmatch(value) is None:
            raise ValueError("Private camera route name is invalid.")
        return value

    @staticmethod
    def _private_relay_source(value: object) -> str:
        if not isinstance(value, str) or len(value) > 300:
            raise ValueError("Private camera relay source is invalid.")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname != _PRIVATE_RELAY_HOST
            or parsed.port != _PRIVATE_RELAY_PORT
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or _PRIVATE_RELAY_PATH.fullmatch(parsed.path) is None
        ):
            raise ValueError("Private camera relay source is invalid.")
        return value

    @staticmethod
    def _validate_route_mutation(body: bytes, content_type: str) -> None:
        if not body:
            return
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise VideoEngineError("Private camera route returned an invalid response.")
        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VideoEngineError(
                "Private camera route returned an invalid response."
            ) from error
        if not isinstance(document, Mapping) or document.get("success") is not True:
            raise VideoEngineError("Private camera route update failed.")

    def _raw_event_id(self, event_id: str) -> str:
        prefix = "frigate:"
        if not isinstance(event_id, str) or not event_id.startswith(prefix):
            raise ValueError("Event does not belong to Frigate.")
        raw_id = event_id[len(prefix) :]
        if not _FRIGATE_EVENT.fullmatch(raw_id):
            raise ValueError("Frigate event ID is invalid.")
        return raw_id

    @staticmethod
    def _export_name(raw_event_id: str) -> str:
        digest = hashlib.sha256(raw_event_id.encode("utf-8")).hexdigest()[:24]
        return f"ose-event-{digest}"

    def _export_documents(self, *, limit: int) -> tuple[Mapping[str, object], ...]:
        payload = self._json("/api/exports", maximum_bytes=2_000_000)
        if not isinstance(payload, list):
            raise VideoEngineError("Frigate returned invalid saved clip data.")
        if any(not isinstance(item, Mapping) for item in payload):
            raise VideoEngineError("Frigate returned invalid saved clip data.")
        documents = tuple(payload)
        if len(documents) > limit:
            return documents[:limit]
        return documents

    def _export_document(self, clip_id: str) -> Mapping[str, object]:
        if not isinstance(clip_id, str) or not _FRIGATE_EVENT.fullmatch(clip_id):
            raise ValueError("Saved clip ID is invalid.")
        payload = self._json(
            f"/api/exports/{quote(clip_id, safe='')}", maximum_bytes=64_000
        )
        if not isinstance(payload, Mapping) or payload.get("id") != clip_id:
            raise VideoEngineError("Frigate returned invalid saved clip data.")
        return payload

    def _saved_clip(self, value: Mapping[str, object]) -> EngineSavedClip | None:
        export_id = value.get("id")
        engine_camera = value.get("camera")
        if not isinstance(export_id, str) or not _FRIGATE_EVENT.fullmatch(export_id):
            raise VideoEngineError("Frigate returned an invalid saved clip ID.")
        if not isinstance(engine_camera, str):
            raise VideoEngineError("Frigate returned an invalid saved clip camera.")
        camera_id = self._ose_camera_ids.get(engine_camera)
        if camera_id is None:
            return None
        in_progress = value.get("in_progress", False)
        if not isinstance(in_progress, bool):
            raise VideoEngineError("Frigate returned invalid saved clip data.")
        created_at = self._timestamp(value.get("date"), required=True)
        if created_at is None:
            raise VideoEngineError("Frigate returned invalid saved clip data.")
        return EngineSavedClip(
            id=export_id,
            camera_id=camera_id,
            created_at=created_at,
            in_progress=in_progress,
        )

    @staticmethod
    def _vod_resource(resource: str) -> str:
        if (
            not isinstance(resource, str)
            or ".." in resource
            or not _FRIGATE_VOD_RESOURCE.fullmatch(resource)
        ):
            raise ValueError("Frigate VOD resource is invalid.")
        return resource

    @staticmethod
    def _vod_timestamp(value: datetime) -> str:
        return f"{value.timestamp():.6f}".rstrip("0").rstrip(".")

    def _review_event(self, raw_id: str) -> EngineEvent:
        review = self._json(
            f"/api/review/{quote(raw_id, safe='')}", maximum_bytes=2_000_000
        )
        event = self._event(review)
        if (
            event is None
            or event.id != f"frigate:{raw_id}"
            or event.ended_at is None
        ):
            raise VideoEngineError("Completed video event is unavailable.")
        return event

    @classmethod
    def _event_window(cls, event: EngineEvent) -> tuple[str, str]:
        if event.ended_at is None or event.ended_at <= event.started_at:
            raise VideoEngineError("Completed video event is unavailable.")
        # Include context on both sides. This can only return footage that was
        # actually recorded; a gap in the source cannot be reconstructed.
        start = datetime.fromtimestamp(max(0, event.started_at.timestamp() - 5), UTC)
        end = datetime.fromtimestamp(event.ended_at.timestamp() + 5, UTC)
        return cls._vod_timestamp(start), cls._vod_timestamp(end)

    def _json(self, path: str, *, maximum_bytes: int) -> object:
        body, content_type = self._transport(path, "application/json", maximum_bytes)
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise VideoEngineError("Frigate returned an invalid response type.")
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VideoEngineError("Frigate returned invalid JSON.") from error

    def _json_mutation(
        self, path: str, payload: Mapping[str, object], *, maximum_bytes: int
    ) -> object:
        body, content_type = self._mutation_transport(
            path,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            maximum_bytes,
        )
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise VideoEngineError("Frigate returned an invalid response type.")
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VideoEngineError("Frigate returned invalid JSON.") from error

    def _event(self, value: object) -> EngineEvent | None:
        if not isinstance(value, Mapping):
            raise VideoEngineError("Frigate returned invalid event data.")
        raw_id = value.get("id")
        engine_camera = value.get("camera")
        if not isinstance(raw_id, str) or not _FRIGATE_EVENT.fullmatch(raw_id):
            raise VideoEngineError("Frigate returned an invalid event ID.")
        if not isinstance(engine_camera, str):
            raise VideoEngineError("Frigate returned an invalid event camera.")
        camera_id = self._ose_camera_ids.get(engine_camera)
        if camera_id is None:
            return None
        started_at = self._timestamp(value.get("start_time"), required=True)
        ended_at = self._timestamp(value.get("end_time"), required=False)
        severity = value.get("severity")
        if severity not in {"alert", "detection"}:
            raise VideoEngineError("Frigate returned an invalid event severity.")
        data = value.get("data")
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise VideoEngineError("Frigate returned invalid event metadata.")
        return EngineEvent(
            id=f"frigate:{raw_id}",
            camera_id=camera_id,
            started_at=started_at,
            ended_at=ended_at,
            severity=severity,
            labels=self._strings(data.get("objects")),
            zones=self._strings(data.get("zones")),
        )

    @staticmethod
    def _timestamp(value: object, *, required: bool) -> datetime | None:
        if value is None and not required:
            return None
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise VideoEngineError(
                    "Frigate returned an invalid event timestamp."
                ) from error
            if parsed.tzinfo is None:
                raise VideoEngineError("Frigate returned an invalid event timestamp.")
            return parsed.astimezone(UTC)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise VideoEngineError("Frigate returned an invalid event timestamp.")
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError) as error:
            raise VideoEngineError("Frigate returned an invalid event timestamp.") from error

    @staticmethod
    def _strings(value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise VideoEngineError("Frigate returned invalid event metadata.")
        if any(not isinstance(item, str) or len(item) > 80 for item in value):
            raise VideoEngineError("Frigate returned invalid event metadata.")
        return tuple(value)

    def _request(
        self, path: str, accept: str, maximum_bytes: int
    ) -> tuple[bytes, str]:
        if (
            not path.startswith(("/api/", "/vod/", "/exports/"))
            or any(character in path for character in "\r\n")
        ):
            raise VideoEngineError("Frigate request path is invalid.")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=self.timeout_seconds
        )
        try:
            connection.request("GET", path, headers={"Accept": accept})
            response = connection.getresponse()
            if response.status != 200:
                raise VideoEngineError("Frigate could not complete the request.")
            body = response.read(maximum_bytes + 1)
            if len(body) > maximum_bytes:
                raise VideoEngineError("Frigate response exceeded its safe limit.")
            return body, response.getheader("Content-Type", "")
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise VideoEngineError("Frigate is unavailable.") from error
        finally:
            connection.close()

    def _mutation_request(
        self, path: str, payload: bytes, maximum_bytes: int
    ) -> tuple[bytes, str]:
        allowed = (
            path.startswith("/api/export/")
            or path == "/api/reviews/delete"
            or path == "/api/exports/delete"
        )
        if not allowed or any(character in path for character in "\r\n"):
            raise VideoEngineError("Frigate mutation path is invalid.")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=self.timeout_seconds
        )
        try:
            connection.request(
                "POST",
                path,
                body=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            if response.status not in {200, 201, 202}:
                raise VideoEngineError("Frigate could not complete the request.")
            body = response.read(maximum_bytes + 1)
            if len(body) > maximum_bytes:
                raise VideoEngineError("Frigate response exceeded its safe limit.")
            return body, response.getheader("Content-Type", "")
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise VideoEngineError("Frigate is unavailable.") from error
        finally:
            connection.close()

    def _route_request(
        self, method: str, path: str, maximum_bytes: int
    ) -> tuple[bytes, str]:
        if (
            method not in {"PUT", "DELETE"}
            or not path.startswith("/api/go2rtc/streams/")
            or any(character in path for character in "\r\n")
        ):
            raise VideoEngineError("Private camera route request is invalid.")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=self.timeout_seconds
        )
        try:
            connection.request(
                method,
                path,
                body=b"",
                headers={"Accept": "application/json"},
            )
            response = connection.getresponse()
            if response.status not in {200, 201, 204}:
                raise VideoEngineError("Private camera route update failed.")
            body = response.read(maximum_bytes + 1)
            if len(body) > maximum_bytes:
                raise VideoEngineError("Private camera route response was too large.")
            return body, response.getheader("Content-Type", "")
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise VideoEngineError("Frigate is unavailable.") from error
        finally:
            connection.close()

    def _signal_request(
        self, path: str, offer: bytes, maximum_bytes: int
    ) -> tuple[bytes, str]:
        if (
            not path.startswith("/api/go2rtc/webrtc?")
            or any(character in path for character in "\r\n")
        ):
            raise VideoEngineError("Frigate signaling path is invalid.")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=self.timeout_seconds
        )
        try:
            connection.request(
                "POST",
                path,
                body=offer,
                headers={
                    "Accept": "application/sdp",
                    "Content-Type": "application/sdp",
                },
            )
            response = connection.getresponse()
            if response.status not in {200, 201}:
                raise VideoEngineError("Frigate could not negotiate live video.")
            body = response.read(maximum_bytes + 1)
            if len(body) > maximum_bytes:
                raise VideoEngineError("Frigate response exceeded its safe limit.")
            return body, response.getheader("Content-Type", "")
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise VideoEngineError("Frigate is unavailable.") from error
        finally:
            connection.close()


def load_configured_video_engine(data_directory: Path) -> FrigateEngine | None:
    """Load the appliance-selected engine without accepting an external host."""

    path = Path(data_directory) / VIDEO_ENGINE_CONFIG_FILE
    if not path.exists():
        return None
    try:
        details = path.stat()
        if path.is_symlink() or not path.is_file():
            raise ValueError
        if os.name == "posix" and (
            details.st_mode & 0o077 or details.st_uid != os.geteuid()
        ):
            raise ValueError
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError
        schema_version = document.get("schema_version")
        expected_keys = {"schema_version", "engine", "port", "camera_bindings"}
        if schema_version == 2:
            expected_keys.add("route_relay_enabled")
        if (
            set(document) != expected_keys
            or schema_version not in {1, 2}
            or document["engine"] != "frigate"
            or not isinstance(document["camera_bindings"], dict)
            or not 1 <= len(document["camera_bindings"]) <= 16
            or (
                schema_version == 2
                and not isinstance(document["route_relay_enabled"], bool)
            )
        ):
            raise ValueError
        return FrigateEngine(
            document["camera_bindings"],
            port=document["port"],
            route_relay_enabled=(
                bool(document["route_relay_enabled"])
                if schema_version == 2
                else False
            ),
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise VideoEngineError(
            "Saved video engine settings could not be read."
        ) from error


def save_frigate_engine_configuration(
    data_directory: Path,
    camera_bindings: Mapping[str, str],
    *,
    port: int = 5000,
    route_relay_enabled: bool = False,
) -> FrigateEngine:
    """Atomically select a loopback Frigate instance for the OSE application."""

    engine = FrigateEngine(
        camera_bindings,
        port=port,
        route_relay_enabled=route_relay_enabled,
    )
    document = {
        "schema_version": 2,
        "engine": "frigate",
        "port": port,
        "camera_bindings": dict(engine.camera_bindings),
        "route_relay_enabled": route_relay_enabled,
    }
    secure_write_text(
        Path(data_directory) / VIDEO_ENGINE_CONFIG_FILE,
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n",
    )
    return engine
