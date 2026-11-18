"""Minimal authenticated camera service for Raspberry Pi camera nodes.

This module is copied into the camera image during provisioning. It delays its
Pi-specific imports until startup so host-side packaging and tests remain
platform neutral.
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
import hashlib
import hmac
import io
import json
import logging
from pathlib import Path
import re
import subprocess
import threading
from time import monotonic
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit

try:
    from .network_recovery import read_health_summary
    from .runtime_metrics import read_runtime_metrics
    from .journal_reader import ALLOWED_LOG_LINES, CAMERA_LOG_SOURCES, read_journal
except ImportError:  # The image executes this file directly from /opt/vms.
    from network_recovery import read_health_summary
    from runtime_metrics import read_runtime_metrics
    from journal_reader import ALLOWED_LOG_LINES, CAMERA_LOG_SOURCES, read_journal


LOGGER = logging.getLogger("vms.camera")
_VIDEO_SIZE = (1920, 1080)
_SNAPSHOT_SIZE = (640, 360)
_VIDEO_FPS = 15
_VIDEO_BITRATE = 3_000_000
_VIDEO_IPERIOD = 15
_CAMERA_STARTUP_GRACE_SECONDS = 20
_MAXIMUM_HTTP_REQUESTS = 8
_REGISTRATION_PATH = "/api/v1/camera-registration"
_DEFAULT_RECORDER_HOSTS = ("osecameras.local",)


class CameraRegistrationClient:
    """Publish the camera's authenticated current route to its recorder."""

    def __init__(
        self,
        *,
        camera_id: str,
        stream_secret: str,
        recorder_hosts: tuple[str, ...],
        camera_port: int = 9080,
    ) -> None:
        if not isinstance(camera_id, str) or not camera_id:
            raise ValueError("camera ID is invalid")
        if not isinstance(stream_secret, str) or not stream_secret:
            raise ValueError("camera registration secret is invalid")
        if not recorder_hosts or any(
            not isinstance(host, str)
            or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host)
            is None
            for host in recorder_hosts
        ):
            raise ValueError("recorder registration hosts are invalid")
        if isinstance(camera_port, bool) or not 1 <= camera_port <= 65535:
            raise ValueError("camera registration port is invalid")
        self.camera_id = camera_id
        self.stream_secret = stream_secret
        self.recorder_hosts = recorder_hosts
        self.camera_port = camera_port

    def register(self) -> bool:
        payload = json.dumps(
            {
                "schema_version": 1,
                "camera_id": self.camera_id,
                "port": self.camera_port,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(
            self.stream_secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        for host in self.recorder_hosts:
            request = Request(
                f"http://{host}{_REGISTRATION_PATH}",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-VMS-Signature": signature,
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=15) as response:
                    if response.status == HTTPStatus.OK:
                        response.read(4097)
                        return True
            except Exception:
                continue
        return False


class H264StreamBuffer(io.BufferedIOBase):
    """Bounded hardware-encoder output shared by a small number of clients."""

    def __init__(self, *, maximum_chunks: int = 120, maximum_clients: int = 2) -> None:
        if maximum_chunks < 2 or maximum_clients < 1:
            raise ValueError("H.264 stream bounds are invalid")
        self._condition = threading.Condition()
        self._chunks: deque[tuple[int, bytes, bool]] = deque(maxlen=maximum_chunks)
        self._sequence = 0
        self._clients = 0
        self._maximum_clients = maximum_clients

    def write(self, data: bytes) -> int:
        chunk = bytes(data)
        if not chunk:
            return 0
        with self._condition:
            self._sequence += 1
            self._chunks.append(
                (self._sequence, chunk, _contains_h264_nal_type(chunk, 7))
            )
            self._condition.notify_all()
        return len(chunk)

    @property
    def ready(self) -> bool:
        with self._condition:
            return bool(self._chunks)

    def acquire_client(self) -> bool:
        with self._condition:
            if self._clients >= self._maximum_clients:
                return False
            self._clients += 1
            return True

    def release_client(self) -> None:
        with self._condition:
            self._clients = max(0, self._clients - 1)

    def wait_for_chunk(
        self, previous_sequence: int | None, *, timeout: float = 10
    ) -> tuple[int, bytes]:
        """Return contiguous output, starting or resuming at sequence headers."""

        deadline = monotonic() + timeout
        with self._condition:
            while True:
                selected = self._select_chunk(previous_sequence)
                if selected is not None:
                    return selected[0], selected[1]
                remaining = deadline - monotonic()
                if remaining <= 0 or not self._condition.wait(remaining):
                    raise TimeoutError("No H.264 camera data is available.")

    def _select_chunk(
        self, previous_sequence: int | None
    ) -> tuple[int, bytes, bool] | None:
        if not self._chunks:
            return None
        if previous_sequence is None or previous_sequence < self._chunks[0][0] - 1:
            return next(
                (chunk for chunk in reversed(self._chunks) if chunk[2]),
                None,
            )
        return next(
            (chunk for chunk in self._chunks if chunk[0] > previous_sequence),
            None,
        )


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server with a hard cap on live request handlers."""

    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        maximum_requests: int = _MAXIMUM_HTTP_REQUESTS,
    ) -> None:
        if maximum_requests < 1:
            raise ValueError("HTTP request bound is invalid")
        self._request_slots = threading.BoundedSemaphore(maximum_requests)
        super().__init__(server_address, request_handler_class)

    def process_request(self, request: object, client_address: object) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def _contains_h264_nal_type(data: bytes, expected: int) -> bool:
    """Recognize Annex-B NAL units without parsing untrusted stream payloads."""

    index = 0
    while index < len(data) - 3:
        if data[index : index + 3] == b"\x00\x00\x01":
            header = index + 3
        elif data[index : index + 4] == b"\x00\x00\x00\x01":
            header = index + 4
        else:
            index += 1
            continue
        if header < len(data) and data[header] & 0x1F == expected:
            return True
        index = header + 1
    return False


class CameraApplication:
    def __init__(
        self,
        config_path: Path,
        *,
        provisioning_marker: Path = Path("/var/lib/vms/first-boot.json"),
        network_health_path: Path = Path("/var/lib/vms/network-health.json"),
    ) -> None:
        self.config_path = config_path
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.provisioning_marker = provisioning_marker
        self.network_health_path = network_health_path
        self.stream = H264StreamBuffer()
        self.camera = None
        self.error: str | None = None
        self._capture_started_at: float | None = None
        self._snapshot_lock = threading.Lock()
        self._registration_stop = threading.Event()
        self._registration_thread: threading.Thread | None = None
        assigned_recorder = self.config.get("assigned_recorder")
        configured_hosts = (
            assigned_recorder.get("network_hosts")
            if isinstance(assigned_recorder, dict)
            else None
        )
        recorder_hosts = (
            tuple(configured_hosts)
            if isinstance(configured_hosts, list)
            and configured_hosts
            and all(isinstance(host, str) for host in configured_hosts)
            else _DEFAULT_RECORDER_HOSTS
        )
        self.registration = CameraRegistrationClient(
            camera_id=str(self.config["device_id"]),
            stream_secret=str(self.config["stream_secret"]),
            recorder_hosts=recorder_hosts,
        )

    def start(self) -> None:
        self._capture_started_at = monotonic()
        try:
            from picamera2 import Picamera2
            from picamera2.encoders import H264Encoder
            from picamera2.outputs import FileOutput

            self.camera = Picamera2()
            camera_config = self.camera.create_video_configuration(
                main={"size": _VIDEO_SIZE, "format": "YUV420"},
                lores={"size": _SNAPSHOT_SIZE, "format": "YUV420"},
                controls={"FrameRate": _VIDEO_FPS},
                encode="main",
            )
            self.camera.configure(camera_config)
            encoder = H264Encoder(
                bitrate=_VIDEO_BITRATE,
                repeat=True,
                iperiod=_VIDEO_IPERIOD,
            )
            self.camera.start_recording(encoder, FileOutput(self.stream))
            LOGGER.info("Native H.264 camera capture started")
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            LOGGER.exception("Camera capture initialization failed")
        self._registration_thread = threading.Thread(
            target=self._registration_loop,
            name="vms-camera-registration",
            daemon=True,
        )
        self._registration_thread.start()

    def stop(self) -> None:
        self._registration_stop.set()
        if self._registration_thread is not None:
            self._registration_thread.join(timeout=6)
        if self.camera is not None:
            try:
                self.camera.stop_recording()
            finally:
                self.camera.close()

    def _registration_loop(self) -> None:
        while not self._registration_stop.is_set():
            if not self.registration.register():
                LOGGER.debug("Recorder registration is temporarily unavailable")
            self._registration_stop.wait(30)

    def snapshot(self) -> bytes:
        if self.camera is None or self.error is not None:
            raise RuntimeError("Camera is unavailable.")
        if not self._snapshot_lock.acquire(blocking=False):
            raise RuntimeError("Camera snapshot is busy.")
        try:
            # capture_file can wait indefinitely behind an active encoder on
            # some Picamera2/libcamera combinations. Capture the dedicated YUV
            # lores stream and encode its planes directly instead, while
            # allowing at most one snapshot operation to touch the camera.
            import simplejpeg

            pixels = self.camera.capture_array("lores")
            width, height = _SNAPSHOT_SIZE
            y_plane = pixels[:height, :width]
            reshaped = pixels.reshape(
                (pixels.shape[0] * 2, pixels.strides[0] // 2)
            )
            u_plane = reshaped[
                2 * height : 2 * height + height // 2,
                : width // 2,
            ]
            v_plane = reshaped[
                2 * height + height // 2 :,
                : width // 2,
            ]
            frame = simplejpeg.encode_jpeg_yuv_planes(
                y_plane,
                u_plane,
                v_plane,
                80,
            )
        finally:
            self._snapshot_lock.release()
        if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
            raise RuntimeError("Camera snapshot is invalid.")
        return frame

    def authorized(self, header: str | None) -> bool:
        expected_user = str(self.config["device_id"])
        expected_password = str(self.config["stream_secret"])
        if not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            user, password = decoded.split(":", 1)
        except (ValueError, UnicodeError):
            return False
        return hmac.compare_digest(user, expected_user) and hmac.compare_digest(
            password, expected_password
        )

    def health(self) -> dict[str, object]:
        model_path = Path("/proc/device-tree/model")
        model = (
            model_path.read_text(encoding="utf-8", errors="replace").rstrip("\0")
            if model_path.exists()
            else "unknown"
        )
        power = _command_value(["vcgencmd", "get_throttled"])
        system_state = _command_value(["systemctl", "is-system-running"])
        network = read_health_summary(self.network_health_path)
        network.update(_wireless_link_metrics())
        capture_state = self._capture_state()
        capture_error = self.error
        if capture_state == "stalled":
            capture_error = "Camera capture started but produced no video."
        return {
            "schema_version": 4,
            "device_id": self.config["device_id"],
            "camera_number": self.config["camera_number"],
            "model": model,
            "camera_ready": capture_state == "ready",
            "capture_state": capture_state,
            # Camera capture may start before the provisioning transaction and
            # the rest of userspace have settled.  Keep those facts separate
            # so an apparently healthy stream cannot hide a stuck boot.
            "system_ready": (
                _provisioning_ready(self.provisioning_marker)
                and system_state == "running"
            ),
            "power": power,
            "runtime": read_runtime_metrics(source=Path(__file__), power=power),
            "claimed": bool(self.config.get("assigned_recorder")),
            "provisioning_mode": not bool(self.config.get("assigned_recorder")),
            "network": network,
            "media": {
                "live": {
                    "codec": "h264",
                    "path": "/stream.h264",
                    "width": _VIDEO_SIZE[0],
                    "height": _VIDEO_SIZE[1],
                    "fps": _VIDEO_FPS,
                    "bitrate": _VIDEO_BITRATE,
                },
                "snapshot": {
                    "codec": "jpeg",
                    "path": "/snapshot.jpg",
                    "width": _SNAPSHOT_SIZE[0],
                    "height": _SNAPSHOT_SIZE[1],
                },
            },
            "error": capture_error,
        }

    def _capture_state(self) -> str:
        if self.error is not None:
            return "failed"
        if self.stream.ready:
            return "ready"
        if self._capture_started_at is None:
            return "not-started"
        if monotonic() - self._capture_started_at < _CAMERA_STARTUP_GRACE_SECONDS:
            return "starting"
        return "stalled"


def _wireless_link_metrics(
    path: Path = Path("/proc/net/wireless"),
) -> dict[str, int | None]:
    """Read owner-safe Wi-Fi signal values without exposing network identity."""

    try:
        interface_line = next(
            line for line in path.read_text(encoding="utf-8").splitlines() if ":" in line
        )
        fields = interface_line.split(":", 1)[1].split()
        quality = float(fields[1].rstrip("."))
        level = float(fields[2].rstrip("."))
        if not 0 <= quality <= 70 or not -150 <= level <= 0:
            raise ValueError
    except (OSError, StopIteration, UnicodeError, IndexError, ValueError):
        return {"rssi_dbm": None, "signal_percent": None}
    return {
        "rssi_dbm": round(level),
        "signal_percent": round(quality * 100 / 70),
    }


def _provisioning_ready(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("schema_version") == 1
        and value.get("state") == "ready"
    )


def _command_value(arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def handler_for(application: CameraApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "VMSCamera/0.2"

        def log_message(self, message: str, *arguments: object) -> None:
            # Keep device identifiers and future one-time values out of logs.
            return None

        def _authorize(self) -> bool:
            if application.authorized(self.headers.get("Authorization")):
                return True
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Camera"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
            if not self._authorize():
                return
            request_path = urlsplit(self.path)
            if request_path.path == "/diagnostics/logs":
                try:
                    values = parse_qs(
                        request_path.query,
                        keep_blank_values=False,
                        strict_parsing=True,
                    )
                    if (
                        set(values) != {"component", "lines"}
                        or any(len(items) != 1 for items in values.values())
                    ):
                        raise ValueError
                    component = values["component"][0]
                    line_text = values["lines"][0]
                    if not line_text.isascii() or not line_text.isdigit():
                        raise ValueError
                    lines = int(line_text)
                    if component not in CAMERA_LOG_SOURCES or lines not in ALLOWED_LOG_LINES:
                        raise ValueError
                    body = json.dumps(
                        read_journal("camera", component, lines),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                except (UnicodeError, ValueError):
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if request_path.path == "/health":
                body = json.dumps(application.health(), sort_keys=True).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/snapshot.jpg":
                try:
                    frame = application.snapshot()
                except (OSError, RuntimeError):
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                return
            if self.path == "/stream.h264":
                if not application.stream.acquire_client():
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "video/h264")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                sequence = None
                try:
                    while True:
                        sequence, chunk = application.stream.wait_for_chunk(
                            sequence
                        )
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    return
                finally:
                    application.stream.release_client()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/etc/vms/device.json"))
    parser.add_argument("--port", type=int, default=9080)
    arguments = parser.parse_args(argv)

    application = CameraApplication(arguments.config)
    LOGGER.info("Camera service starting")
    application.start()
    server = BoundedThreadingHTTPServer(
        ("0.0.0.0", arguments.port), handler_for(application)
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        application.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
