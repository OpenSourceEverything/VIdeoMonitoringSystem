"""Redacted runtime verification shared by the GUI and CLI."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import ipaddress
import json
import math
import re
import socket
from time import monotonic
from typing import Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen

from .provisioning import DeviceIdentity, get_board_profile


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    message: str


@dataclass(frozen=True, slots=True)
class VerificationReport:
    schema_version: int
    device_id: str
    checked_at: str
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "device_id": self.device_id,
            "checked_at": self.checked_at,
            "passed": self.passed,
            "checks": [asdict(check) for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


CAMERA_HEALTH_NAMES = ("Power", "Network", "Sensor", "Live Video", "Recorder")
HEALTH_OK = "OK"
HEALTH_ATTENTION = "Needs attention"
HEALTH_NOT_CHECKED = "Not checked"
_HEALTH_VALUES = frozenset((HEALTH_OK, HEALTH_ATTENTION, HEALTH_NOT_CHECKED))


@dataclass(frozen=True, slots=True)
class CameraHealthStatus:
    """One intentionally plain, retail-safe camera status."""

    name: str
    status: str


@dataclass(frozen=True, slots=True)
class CameraHealth:
    """Whitelisted projection of a detailed camera verification report."""

    camera_code: str
    camera_name: str
    checked_at: str | None
    statuses: tuple[CameraHealthStatus, ...]

    def to_dict(self) -> dict[str, object]:
        supplied = {
            item.name: item.status
            for item in self.statuses
            if item.name in CAMERA_HEALTH_NAMES and item.status in _HEALTH_VALUES
        }
        return {
            "camera": self.camera_name,
            "camera_code": self.camera_code,
            "checked_at": self.checked_at,
            "statuses": {
                name: supplied.get(name, HEALTH_NOT_CHECKED)
                for name in CAMERA_HEALTH_NAMES
            },
        }


def camera_health_summary(
    camera_code: str,
    *,
    camera_name: str | None = None,
    connected: bool | None = None,
    verification: VerificationReport | None = None,
) -> CameraHealth:
    """Reduce internal checks to the five statuses safe for retail display.

    Detailed check messages are deliberately ignored. A current disconnected
    observation takes precedence over an older successful verification.
    """

    checks = (
        {}
        if verification is None
        else {check.name: check.passed for check in verification.checks}
    )
    network_checks = (
        ("connection", "network") if "network" in checks else ("connection",)
    )
    recorder_checks = ("identity", "number", "enrollment", "startup")
    if "recorder" in checks:
        recorder_checks += ("recorder",)
    values = {
        "Power": _health_status(checks, ("power",)),
        "Network": _health_status(checks, network_checks),
        "Sensor": _health_status(checks, ("capture",)),
        "Live Video": _health_status(checks, ("image", "stream")),
        "Recorder": _health_status(checks, recorder_checks),
    }

    if verification is not None and verification.device_id != camera_code:
        values["Recorder"] = HEALTH_ATTENTION
    if connected is False:
        values["Power"] = HEALTH_NOT_CHECKED
        values["Network"] = HEALTH_ATTENTION
        values["Sensor"] = HEALTH_NOT_CHECKED
        values["Live Video"] = HEALTH_ATTENTION
        values["Recorder"] = HEALTH_ATTENTION
    elif connected is True:
        if values["Network"] == HEALTH_NOT_CHECKED:
            values["Network"] = HEALTH_OK
        if values["Recorder"] == HEALTH_NOT_CHECKED:
            values["Recorder"] = HEALTH_OK

    return CameraHealth(
        camera_code=str(camera_code),
        camera_name=str(camera_name or "Unnamed camera"),
        checked_at=None if verification is None else verification.checked_at,
        statuses=tuple(
            CameraHealthStatus(name, values[name]) for name in CAMERA_HEALTH_NAMES
        ),
    )


def camera_support_report(
    cameras: Sequence[CameraHealth], *, generated_at: str | None = None
) -> str:
    """Serialize the single redacted support-report format offered by the GUI."""

    payload = {
        "schema_version": 1,
        "report": "Camera Health",
        "created_at": generated_at or datetime.now(UTC).isoformat(),
        "cameras": [
            camera.to_dict()
            for camera in cameras
            if isinstance(camera, CameraHealth)
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


def camera_media_profile(health: Mapping[str, object]) -> tuple[str, str, int]:
    """Return the validated live endpoint, codec, and source frame rate."""

    schema_version = health.get("schema_version")
    if schema_version in {2, 3}:
        return "/stream.mjpg", "mjpeg", 10
    media = health.get("media")
    live = media.get("live") if isinstance(media, dict) else None
    if schema_version != 4 or not isinstance(live, dict):
        raise ValueError("camera media capabilities are invalid")
    codec = live.get("codec")
    path = live.get("path")
    frame_rate = live.get("fps")
    width = live.get("width")
    height = live.get("height")
    bitrate = live.get("bitrate")
    if (
        codec != "h264"
        or path != "/stream.h264"
        or isinstance(frame_rate, bool)
        or not isinstance(frame_rate, int)
        or not 1 <= frame_rate <= 60
        or isinstance(width, bool)
        or not isinstance(width, int)
        or not 320 <= width <= 7680
        or isinstance(height, bool)
        or not isinstance(height, int)
        or not 240 <= height <= 4320
        or isinstance(bitrate, bool)
        or not isinstance(bitrate, int)
        or not 100_000 <= bitrate <= 100_000_000
    ):
        raise ValueError("camera media capabilities are invalid")
    return path, codec, frame_rate


def camera_media_routes(
    health: Mapping[str, object],
) -> dict[str, tuple[str, str, int]]:
    """Return the main and detection routes declared by one camera.

    Schema 2/3 cameras and the initial schema 4 Pi reference expose one stream
    for both roles.  That alias keeps the development fixtures usable, but it
    does not satisfy the separate product hardware qualification requirement.
    New schema 4 endpoints may declare a distinct ``detection`` profile.
    """

    main = camera_media_profile(health)
    media = health.get("media")
    detection = media.get("detection") if isinstance(media, dict) else None
    if detection is None:
        return {"main": main, "detection": main}
    if not isinstance(detection, dict):
        raise ValueError("camera detection media capability is invalid")
    path = detection.get("path")
    codec = detection.get("codec")
    frame_rate = detection.get("fps")
    width = detection.get("width")
    height = detection.get("height")
    bitrate = detection.get("bitrate")
    if (
        codec != "h264"
        or not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or ".." in path.split("/")
        or any(character in path for character in "\x00\r\n?#")
        or len(path) > 160
        or isinstance(frame_rate, bool)
        or not isinstance(frame_rate, int)
        or not 1 <= frame_rate <= 60
        or isinstance(width, bool)
        or not isinstance(width, int)
        or not 320 <= width <= 3840
        or isinstance(height, bool)
        or not isinstance(height, int)
        or not 240 <= height <= 2160
        or isinstance(bitrate, bool)
        or not isinstance(bitrate, int)
        or not 100_000 <= bitrate <= 20_000_000
    ):
        raise ValueError("camera detection media capability is invalid")
    return {
        "main": main,
        "detection": (path, codec, frame_rate),
    }


def _health_status(
    checks: dict[str, bool], required: tuple[str, ...]
) -> str:
    if any(checks.get(name) is False for name in required):
        return HEALTH_ATTENTION
    if any(name not in checks for name in required):
        return HEALTH_NOT_CHECKED
    return HEALTH_OK


class CameraTransport(Protocol):
    def get_json(self, path: str) -> dict[str, object]: ...

    def get_bytes(self, path: str, *, maximum: int) -> bytes: ...

    def get_mjpeg_frames(
        self,
        path: str,
        *,
        maximum: int,
        count: int,
        timeout_seconds: float | None = None,
    ) -> tuple[bytes, ...]: ...

    def get_stream_prefix(
        self,
        path: str,
        *,
        minimum: int,
        maximum: int,
        timeout_seconds: float,
    ) -> bytes: ...


class HttpCameraTransport:
    def __init__(
        self,
        hostname: str,
        device_id: str,
        stream_secret: str,
        *,
        port: int = 9080,
        timeout: float = 10,
    ) -> None:
        formatted_hostname = _validated_hostname(hostname)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("camera port must be between 1 and 65535")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("camera timeout must be a positive number")
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("camera timeout must be a positive number")
        credentials = base64.b64encode(
            f"{device_id}:{stream_secret}".encode("utf-8")
        ).decode("ascii")
        self._resolver_hostname = hostname
        self._hostname = formatted_hostname
        self._port = port
        self._device_id = device_id
        self._stream_secret = stream_secret
        self.base_url = f"http://{formatted_hostname}:{port}"
        self.camera_id = device_id
        self.authorization = f"Basic {credentials}"
        self.timeout = timeout

    def authenticated_url(self, path: str, *, resolve: bool = False) -> str:
        """Return a private URL for a trusted local media-engine adapter."""

        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or any(character in path for character in "\x00\r\n?#")
        ):
            raise ValueError("camera path is invalid")
        hostname = self._hostname
        if resolve:
            hostname = self._resolved_private_hostname()
        username = quote(self._device_id, safe="")
        password = quote(self._stream_secret, safe="")
        return (
            f"http://{username}:{password}@{hostname}:{self._port}{path}"
        )

    def _resolved_private_hostname(self) -> str:
        """Resolve once on the recorder for runtimes without mDNS support."""

        try:
            literal = ipaddress.ip_address(self._resolver_hostname)
            addresses = {literal}
        except ValueError:
            try:
                answers = socket.getaddrinfo(
                    self._resolver_hostname,
                    self._port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                )
                addresses = {
                    ipaddress.ip_address(answer[4][0]) for answer in answers
                }
            except (OSError, TypeError, ValueError) as error:
                raise ValueError(
                    "camera hostname did not resolve to a private address"
                ) from error
        private = {
            address
            for address in addresses
            if not (
                address.is_global
                or address.is_loopback
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            )
        }
        if len(private) != 1 or private != addresses:
            raise ValueError(
                "camera hostname did not resolve to one private address"
            )
        address = private.pop()
        return f"[{address}]" if address.version == 6 else str(address)

    def _request(self, path: str, *, timeout: float | None = None):
        request = Request(
            self.base_url + path,
            headers={"Authorization": self.authorization},
        )
        return urlopen(request, timeout=self.timeout if timeout is None else timeout)

    def get_json(self, path: str) -> dict[str, object]:
        with self._request(path) as response:
            data = response.read(128 * 1024)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("camera returned an invalid response")
        return value

    def get_bytes(self, path: str, *, maximum: int) -> bytes:
        with self._request(path) as response:
            data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("camera response exceeded the allowed size")
        return data

    def get_mjpeg_frames(
        self,
        path: str,
        *,
        maximum: int,
        count: int,
        timeout_seconds: float | None = None,
    ) -> tuple[bytes, ...]:
        if type(count) is not int or count < 2:
            raise ValueError("at least two camera frames are required")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
        ):
            raise ValueError("camera timeout must be a positive number")
        timeout = self.timeout if timeout_seconds is None else float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("camera timeout must be a positive number")
        deadline = monotonic() + timeout
        data = bytearray()
        frames: list[bytes] = []
        frame_start: int | None = None
        search_position = 0
        with self._request(path, timeout=timeout) as response:
            if response.headers.get_content_type() != "multipart/x-mixed-replace":
                raise ValueError("camera response is not an MJPEG stream")
            while len(data) <= maximum:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("camera stream timed out")
                _set_response_timeout(response, remaining)
                chunk = response.read1(min(64 * 1024, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > maximum:
                    raise ValueError("camera stream exceeded the allowed size")
                while len(frames) < count:
                    if frame_start is None:
                        frame_start = data.find(b"\xff\xd8", search_position)
                        if frame_start < 0:
                            frame_start = None
                            search_position = max(0, len(data) - 1)
                            break
                        search_position = frame_start + 2
                    frame_end = data.find(b"\xff\xd9", search_position)
                    if frame_end < 0:
                        search_position = max(frame_start + 2, len(data) - 1)
                        break
                    frames.append(bytes(data[frame_start : frame_end + 2]))
                    frame_start = None
                    search_position = frame_end + 2
                if len(frames) >= count:
                    return tuple(frames[:count])
        raise ValueError("camera stream did not contain enough complete frames")

    def get_stream_prefix(
        self,
        path: str,
        *,
        minimum: int,
        maximum: int,
        timeout_seconds: float,
    ) -> bytes:
        if (
            type(minimum) is not int
            or type(maximum) is not int
            or not 1 <= minimum <= maximum <= 8 * 1024 * 1024
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
        ):
            raise ValueError("camera stream bound is invalid")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("camera timeout must be a positive number")
        deadline = monotonic() + timeout
        data = bytearray()
        with self._request(path, timeout=timeout) as response:
            if response.headers.get_content_type() not in {
                "application/octet-stream",
                "video/h264",
                "video/x-h264",
            }:
                raise ValueError("camera response is not an H.264 stream")
            while len(data) <= maximum:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("camera stream timed out")
                _set_response_timeout(response, remaining)
                chunk = response.read1(min(64 * 1024, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > maximum:
                    raise ValueError("camera stream exceeded the allowed size")
                if len(data) >= minimum and _valid_h264(bytes(data)):
                    return bytes(data)
        raise ValueError("camera stream did not contain decodable H.264")


def verify_camera(
    camera: DeviceIdentity,
    *,
    transport: CameraTransport | None = None,
    hostname: str | None = None,
    port: int = 9080,
) -> VerificationReport:
    if camera.role != "camera" or camera.camera_number is None or not camera.stream_secret:
        raise ValueError("a provisioned camera identity is required")
    selected_transport = transport or HttpCameraTransport(
        hostname or f"camera-{camera.camera_number:03d}.local",
        camera.device_id,
        camera.stream_secret,
        port=port,
    )
    checks: list[CheckResult] = []
    try:
        health = selected_transport.get_json("/health")
    except Exception:
        checks.append(CheckResult("connection", False, "Camera did not respond."))
        return _report(camera.device_id, checks)

    checks.append(CheckResult("connection", True, "Camera responded."))
    health_schema = health.get("schema_version")
    startup_ok = health_schema in {2, 3, 4} and health.get("system_ready") is True
    checks.append(
        CheckResult(
            "startup",
            startup_ok,
            (
                "Camera startup completed."
                if startup_ok
                else "Camera startup did not complete."
            ),
        )
    )
    checks.append(
        CheckResult(
            "identity",
            health.get("device_id") == camera.device_id,
            "Camera identity matches." if health.get("device_id") == camera.device_id else "Camera identity did not match.",
        )
    )
    checks.append(
        CheckResult(
            "number",
            health.get("camera_number") == camera.camera_number,
            "Camera number matches." if health.get("camera_number") == camera.camera_number else "Camera number did not match.",
        )
    )
    profile = get_board_profile(camera.board, "camera")
    model = str(health.get("model", ""))
    expected_model = profile.display_name.removeprefix("Raspberry Pi ")
    model_matches = expected_model.lower() in model.lower()
    checks.append(
        CheckResult(
            "model",
            model_matches,
            "Camera model matches." if model_matches else "Camera model did not match.",
        )
    )
    ready = health.get("camera_ready") is True
    checks.append(
        CheckResult(
            "capture",
            ready,
            "Image sensor is ready." if ready else "Image sensor is not ready.",
        )
    )
    power_ok = health.get("power") == "throttled=0x0"
    checks.append(
        CheckResult(
            "power",
            power_ok,
            "Power check passed." if power_ok else "Power check failed.",
        )
    )
    enrollment_ok = health.get("claimed") is True and health.get("provisioning_mode") is False
    checks.append(
        CheckResult(
            "enrollment",
            enrollment_ok,
            "Enrollment is complete." if enrollment_ok else "Enrollment is incomplete.",
        )
    )
    if health_schema in {3, 4}:
        network = health.get("network")
        network_ok = (
            isinstance(network, dict)
            and network.get("state") == "healthy"
            and network.get("failed_layer") == "none"
        )
        recorder_ok = (
            isinstance(network, dict) and network.get("recorder_reachable") is True
        )
        summaries_ok = (
            isinstance(network, dict)
            and _valid_boot_summary(network.get("current_boot"))
            and (
                network.get("previous_boot") is None
                or _valid_boot_summary(network.get("previous_boot"))
            )
        )
        checks.extend(
            (
                CheckResult(
                    "network",
                    network_ok,
                    (
                        "Camera network is healthy."
                        if network_ok
                        else "Camera network needs attention."
                    ),
                ),
                CheckResult(
                    "recorder",
                    recorder_ok,
                    (
                        "Recorder path is healthy."
                        if recorder_ok
                        else "Recorder path needs attention."
                    ),
                ),
                CheckResult(
                    "resilience",
                    summaries_ok,
                    (
                        "Recovery state is available."
                        if summaries_ok
                        else "Recovery state is unavailable."
                    ),
                ),
            )
        )
    try:
        snapshot = selected_transport.get_bytes("/snapshot.jpg", maximum=8 * 1024 * 1024)
        snapshot_ok = snapshot.startswith(b"\xff\xd8") and snapshot.endswith(b"\xff\xd9")
    except Exception:
        snapshot_ok = False
    checks.append(
        CheckResult(
            "image",
            snapshot_ok,
            "Live image passed." if snapshot_ok else "Live image failed.",
        )
    )
    try:
        stream_path, stream_codec, _ = camera_media_profile(health)
        if stream_codec == "h264":
            stream_ok = _valid_h264(
                selected_transport.get_stream_prefix(
                    stream_path,
                    minimum=1024,
                    maximum=1024 * 1024,
                    timeout_seconds=10,
                )
            )
        else:
            stream_frames = selected_transport.get_mjpeg_frames(
                stream_path, maximum=4 * 1024 * 1024, count=2
            )
            stream_ok = (
                len(stream_frames) == 2
                and all(_valid_jpeg(frame) for frame in stream_frames)
                and stream_frames[0] != stream_frames[1]
            )
    except Exception:
        stream_ok = False
    checks.append(
        CheckResult(
            "stream",
            stream_ok,
            "Continuous stream passed." if stream_ok else "Continuous stream failed.",
        )
    )
    return _report(camera.device_id, checks)


def verify_camera_source(
    device_id: str,
    *,
    transport: CameraTransport,
) -> VerificationReport:
    """Verify an enrolled runtime source without assuming a board or slot identity."""

    if not isinstance(device_id, str) or not device_id:
        raise ValueError("an enrolled camera identity is required")
    checks: list[CheckResult] = []
    health: dict[str, object] | None = None
    health_reader = getattr(transport, "get_json", None)
    if callable(health_reader):
        try:
            observed = health_reader("/health")
            if isinstance(observed, dict):
                health = observed
        except Exception:
            health = None

    snapshot_ok = False
    snapshot_reader = getattr(transport, "get_bytes", None)
    if callable(snapshot_reader):
        try:
            snapshot = snapshot_reader("/snapshot.jpg", maximum=8 * 1024 * 1024)
            snapshot_ok = _valid_jpeg(snapshot)
        except Exception:
            snapshot_ok = False

    stream_ok = False
    try:
        stream_path, stream_codec, _ = camera_media_profile(
            health or {"schema_version": 3}
        )
        if stream_codec == "h264":
            stream_reader = getattr(transport, "get_stream_prefix", None)
            if callable(stream_reader):
                stream_ok = _valid_h264(
                    stream_reader(
                        stream_path,
                        minimum=1024,
                        maximum=1024 * 1024,
                        timeout_seconds=10.0,
                    )
                )
        else:
            stream_reader = getattr(transport, "get_mjpeg_frames", None)
            if not callable(stream_reader):
                raise ValueError
            frames = tuple(
                stream_reader(
                    stream_path,
                    maximum=4 * 1024 * 1024,
                    count=2,
                    timeout_seconds=10.0,
                )
            )
            stream_ok = (
                len(frames) == 2
                and all(_valid_jpeg(frame) for frame in frames)
                and frames[0] != frames[1]
            )
    except Exception:
        stream_ok = False

    connection_ok = health is not None or snapshot_ok or stream_ok
    checks.append(
        CheckResult(
            "connection",
            connection_ok,
            "Camera responded." if connection_ok else "Camera did not respond.",
        )
    )
    if health is not None:
        health_schema = health.get("schema_version")
        checks.extend(
            (
                CheckResult(
                    "startup",
                    health_schema in {2, 3, 4}
                    and health.get("system_ready") is True,
                    "Camera startup state checked.",
                ),
                CheckResult(
                    "identity",
                    health.get("device_id") == device_id,
                    "Camera identity checked.",
                ),
                CheckResult(
                    "capture",
                    health.get("camera_ready") is True,
                    "Image sensor state checked.",
                ),
                CheckResult(
                    "power",
                    health.get("power") == "throttled=0x0",
                    "Camera power state checked.",
                ),
                CheckResult(
                    "enrollment",
                    health.get("claimed") is True
                    and health.get("provisioning_mode") is False,
                    "Camera enrollment checked.",
                ),
            )
        )
        network = health.get("network")
        if health_schema in {3, 4} and isinstance(network, dict):
            checks.extend(
                (
                    CheckResult(
                        "network",
                        network.get("state") == "healthy"
                        and network.get("failed_layer") == "none",
                        "Camera network state checked.",
                    ),
                    CheckResult(
                        "recorder",
                        network.get("recorder_reachable") is True,
                        "Recorder path checked.",
                    ),
                )
            )
    checks.extend(
        (
            CheckResult(
                "image",
                snapshot_ok,
                "Live image passed." if snapshot_ok else "Live image failed.",
            ),
            CheckResult(
                "stream",
                stream_ok,
                "Continuous stream passed."
                if stream_ok
                else "Continuous stream failed.",
            ),
        )
    )
    return _report(device_id, checks)


_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _validated_hostname(hostname: str) -> str:
    if not isinstance(hostname, str) or not hostname or hostname != hostname.strip():
        raise ValueError("camera hostname is invalid")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if len(hostname) > 253 or any(
            _HOST_LABEL.fullmatch(label) is None for label in hostname.rstrip(".").split(".")
        ):
            raise ValueError("camera hostname is invalid") from None
        return hostname
    return f"[{address}]" if address.version == 6 else str(address)


def _set_response_timeout(response: object, timeout: float) -> None:
    """Apply the remaining total budget to the active urllib socket."""

    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    connection = getattr(raw, "_sock", None)
    if connection is None:
        raise OSError("camera response socket is unavailable")
    connection.settimeout(timeout)


def _valid_jpeg(content: bytes) -> bool:
    return content.startswith(b"\xff\xd8") and content.endswith(b"\xff\xd9")


def _valid_h264(content: bytes) -> bool:
    nal_types: set[int] = set()
    index = 0
    while index < len(content) - 3:
        if content[index : index + 3] == b"\x00\x00\x01":
            header = index + 3
        elif content[index : index + 4] == b"\x00\x00\x00\x01":
            header = index + 4
        else:
            index += 1
            continue
        if header < len(content):
            nal_types.add(content[header] & 0x1F)
        index = header + 1
    return 7 in nal_types and 8 in nal_types and bool(nal_types & {1, 5})


def _valid_boot_summary(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("status") in {"unknown", "healthy", "degraded", "recovering"}
        and value.get("failed_layer")
        in {"unknown", "none", "radio", "address", "gateway", "recorder"}
        and isinstance(value.get("recorder_reachable"), bool)
        and type(value.get("observations")) is int
        and type(value.get("reconnects")) is int
        and isinstance(value.get("reboot_requested"), bool)
    )
def _report(device_id: str, checks: list[CheckResult]) -> VerificationReport:
    return VerificationReport(
        schema_version=1,
        device_id=device_id,
        checked_at=datetime.now(UTC).isoformat(),
        checks=tuple(checks),
    )
