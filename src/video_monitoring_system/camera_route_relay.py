"""Private camera-route relay state and upstream resolution.

go2rtc's runtime stream API accepts source addresses in a query parameter.  A
camera URL containing Basic credentials therefore cannot be sent to that API
without putting a long-term secret in request URLs and potentially in logs.

The relay gives the private engine a stable, credential-free recorder-local
URL.  It resolves that URL to either the committed endpoint or the exact
staged candidate, then injects the camera Authorization header while opening
the upstream request.  Selection state contains addresses only; camera
credentials remain in the existing owner-private device configuration.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .camera_endpoints import CameraEndpointRegistry, ObservedCameraEndpoint
from .provisioning import secure_write_text
from .verification import HttpCameraTransport, camera_media_routes


class CameraRouteRelayError(RuntimeError):
    """A private relay route could not be resolved safely."""


_CAMERA_ID = re.compile(r"CAM-[0-9A-F]{16,64}")
_STATE_FILE = "camera-route-relay.json"
_MAXIMUM_STATE_BYTES = 64 * 1024
_MAXIMUM_REQUESTS = 16
_REQUEST_PATH = re.compile(
    r"/v1/cameras/(?P<camera_id>CAM-[0-9A-F]{16,64})/"
    r"(?P<selection>active|candidate|committed)/"
    r"(?P<role>main|detection)"
)


@dataclass(frozen=True, slots=True)
class CameraRouteUpstream:
    """One resolved upstream request without credentials in its URL."""

    camera_id: str
    endpoint: ObservedCameraEndpoint
    path: str
    codec: str
    frame_rate: int
    authorization: str = field(repr=False)

    @property
    def url(self) -> str:
        """Return the secret-free upstream URL used only for network routing."""

        return f"http://{self.endpoint.ip_address}:{self.endpoint.port}{self.path}"


SecretProvider = Callable[[str], str | None]
HealthProvider = Callable[
    [ObservedCameraEndpoint, str, str], Mapping[str, object]
]


class CameraRouteRelayState:
    """Atomically select a candidate route without committing the registry."""

    def __init__(
        self,
        data_directory: str | os.PathLike[str],
        *,
        secret_provider: SecretProvider,
        health_provider: HealthProvider | None = None,
    ) -> None:
        self.data_directory = Path(data_directory)
        self.state_path = self.data_directory / _STATE_FILE
        self.endpoint_path = self.data_directory / "camera-endpoints.json"
        self._secret_provider = secret_provider
        self._health_provider = health_provider or self._read_health
        self._lock = threading.RLock()

    def activate_candidate(
        self, *, camera_id: str, endpoint: ObservedCameraEndpoint
    ) -> None:
        """Point the stable relay route at exactly one staged candidate."""

        selected_id = self._camera_id(camera_id)
        if not isinstance(endpoint, ObservedCameraEndpoint):
            raise CameraRouteRelayError("Camera route candidate is invalid.")
        with self._lock:
            registry = CameraEndpointRegistry(self.endpoint_path)
            if registry.candidate_for(selected_id) != endpoint:
                raise CameraRouteRelayError("Camera route candidate changed.")
            active = self._load()
            active[selected_id] = endpoint
            self._save(active)

    def restore_committed(self, *, camera_id: str) -> None:
        """Return the stable relay route to the committed endpoint."""

        selected_id = self._camera_id(camera_id)
        with self._lock:
            active = self._load()
            if active.pop(selected_id, None) is not None:
                self._save(active)

    def active_candidate_for(
        self, camera_id: str
    ) -> ObservedCameraEndpoint | None:
        selected_id = self._camera_id(camera_id)
        with self._lock:
            return self._load().get(selected_id)

    def resolve(
        self, *, camera_id: str, role: str, selection: str = "active"
    ) -> CameraRouteUpstream:
        """Resolve a stable or staging relay request to an authenticated source."""

        selected_id = self._camera_id(camera_id)
        if role not in {"main", "detection"}:
            raise CameraRouteRelayError("Camera media role is invalid.")
        if selection not in {"active", "candidate", "committed"}:
            raise CameraRouteRelayError("Camera route selection is invalid.")
        with self._lock:
            registry = CameraEndpointRegistry(self.endpoint_path)
            committed = registry.endpoint_for(selected_id)
            candidate = registry.candidate_for(selected_id)
            if selection == "candidate":
                endpoint = candidate
            elif selection == "committed":
                endpoint = committed
            else:
                activated = self._load().get(selected_id)
                if activated is not None and activated != candidate:
                    raise CameraRouteRelayError("Camera route candidate changed.")
                endpoint = activated or committed
            if endpoint is None:
                raise CameraRouteRelayError("Camera route is unavailable.")
            try:
                secret = self._secret_provider(selected_id)
            except Exception as error:
                raise CameraRouteRelayError(
                    "Camera authorization is unavailable."
                ) from error
            if not isinstance(secret, str) or not secret:
                raise CameraRouteRelayError("Camera authorization is unavailable.")
            health = self._health_provider(endpoint, selected_id, secret)
            try:
                routes = camera_media_routes(health)
                path, codec, frame_rate = routes[role]
            except (KeyError, TypeError, ValueError) as error:
                raise CameraRouteRelayError("Camera media route is unavailable.") from error
            token = base64.b64encode(
                f"{selected_id}:{secret}".encode("utf-8")
            ).decode("ascii")
            return CameraRouteUpstream(
                camera_id=selected_id,
                endpoint=endpoint,
                path=path,
                codec=codec,
                frame_rate=frame_rate,
                authorization=f"Basic {token}",
            )

    @staticmethod
    def _read_health(
        endpoint: ObservedCameraEndpoint, camera_id: str, secret: str
    ) -> Mapping[str, object]:
        transport = HttpCameraTransport(
            endpoint.ip_address,
            camera_id,
            secret,
            port=endpoint.port,
            timeout=3,
        )
        return transport.get_json("/health")

    def _load(self) -> dict[str, ObservedCameraEndpoint]:
        if not self.state_path.exists():
            return {}
        try:
            details = self.state_path.stat()
            if (
                self.state_path.is_symlink()
                or not self.state_path.is_file()
                or not 0 < details.st_size <= _MAXIMUM_STATE_BYTES
                or (
                    os.name == "posix"
                    and (details.st_mode & 0o077 or details.st_uid != os.geteuid())
                )
            ):
                raise ValueError
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or set(document) != {"schema_version", "active_candidates"}
                or document["schema_version"] != 1
                or not isinstance(document["active_candidates"], dict)
                or len(document["active_candidates"]) > 16
            ):
                raise ValueError
            active: dict[str, ObservedCameraEndpoint] = {}
            for camera_id, value in document["active_candidates"].items():
                selected_id = self._camera_id(camera_id)
                if not isinstance(value, dict) or set(value) != {
                    "ip_address",
                    "port",
                }:
                    raise ValueError
                active[selected_id] = ObservedCameraEndpoint(
                    ip_address=value["ip_address"],
                    port=value["port"],
                )
            return active
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            raise CameraRouteRelayError(
                "Camera route relay state could not be read."
            ) from error

    def _save(self, active: Mapping[str, ObservedCameraEndpoint]) -> None:
        document = {
            "schema_version": 1,
            "active_candidates": {
                camera_id: {
                    "ip_address": endpoint.ip_address,
                    "port": endpoint.port,
                }
                for camera_id, endpoint in sorted(active.items())
            },
        }
        try:
            secure_write_text(
                self.state_path,
                json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n",
            )
        except OSError as error:
            raise CameraRouteRelayError(
                "Camera route relay state could not be saved."
            ) from error

    @staticmethod
    def _camera_id(value: object) -> str:
        if not isinstance(value, str) or _CAMERA_ID.fullmatch(value) is None:
            raise CameraRouteRelayError("Camera identity is invalid.")
        return value


UpstreamConnector = Callable[[CameraRouteUpstream, float], http.client.HTTPConnection]


def _connect_upstream(
    upstream: CameraRouteUpstream, timeout: float
) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(
        upstream.endpoint.ip_address,
        upstream.endpoint.port,
        timeout=timeout,
    )


class CameraRouteRelayServer(ThreadingHTTPServer):
    """Bounded private proxy for credential-free go2rtc source URLs."""

    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(
        self,
        server_address: tuple[str, int],
        relay: CameraRouteRelayState,
        *,
        allowed_client_networks: tuple[str, ...],
        maximum_requests: int = _MAXIMUM_REQUESTS,
        upstream_timeout_seconds: float = 10.0,
        upstream_connector: UpstreamConnector | None = None,
    ) -> None:
        if not isinstance(relay, CameraRouteRelayState):
            raise ValueError("Camera route relay state is invalid.")
        if (
            not isinstance(maximum_requests, int)
            or isinstance(maximum_requests, bool)
            or not 1 <= maximum_requests <= 64
        ):
            raise ValueError("Camera route request bound is invalid.")
        if (
            not isinstance(upstream_timeout_seconds, (int, float))
            or isinstance(upstream_timeout_seconds, bool)
            or not 0 < float(upstream_timeout_seconds) <= 60
        ):
            raise ValueError("Camera route timeout is invalid.")
        try:
            networks = tuple(
                ipaddress.ip_network(value, strict=False)
                for value in allowed_client_networks
            )
        except (TypeError, ValueError) as error:
            raise ValueError("Camera route client network is invalid.") from error
        if not networks or any(
            network.is_multicast
            or network.is_unspecified
            or network.is_global
            or network.is_link_local
            for network in networks
        ):
            raise ValueError("Camera route client network is invalid.")
        try:
            bind_address = ipaddress.ip_address(server_address[0])
        except (IndexError, TypeError, ValueError) as error:
            raise ValueError("Camera route bind address is invalid.") from error
        if (
            bind_address.is_global
            or bind_address.is_multicast
            or bind_address.is_unspecified
            or bind_address.is_link_local
            or not any(bind_address in network for network in networks)
        ):
            raise ValueError("Camera route bind address is invalid.")
        self.relay = relay
        self.allowed_client_networks = networks
        self.upstream_timeout_seconds = float(upstream_timeout_seconds)
        self.upstream_connector = upstream_connector or _connect_upstream
        self._request_slots = threading.BoundedSemaphore(maximum_requests)
        super().__init__(server_address, camera_route_relay_handler(relay))

    def client_allowed(self, client_address: object) -> bool:
        try:
            address = ipaddress.ip_address(client_address[0])  # type: ignore[index]
        except (IndexError, TypeError, ValueError):
            return False
        return any(address in network for network in self.allowed_client_networks)

    def process_request(self, request: object, client_address: object) -> None:
        if not self.client_allowed(client_address):
            self.shutdown_request(request)
            return
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


def camera_route_relay_handler(
    relay: CameraRouteRelayState,
) -> type[BaseHTTPRequestHandler]:
    """Create the strict relay handler while keeping state out of globals."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "VMSCameraRouteRelay/0.1"

        def log_message(self, message: str, *arguments: object) -> None:
            # Route identity is private operational data. Service health belongs
            # in systemd status, not a per-request access log.
            return None

        def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
            match = _REQUEST_PATH.fullmatch(self.path)
            if match is None:
                self._error(HTTPStatus.NOT_FOUND)
                return
            try:
                upstream = relay.resolve(
                    camera_id=match.group("camera_id"),
                    selection=match.group("selection"),
                    role=match.group("role"),
                )
                self._stream(upstream)
            except CameraRouteRelayError:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE)

        def _stream(self, upstream: CameraRouteUpstream) -> None:
            server = self.server
            if not isinstance(server, CameraRouteRelayServer):
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            connection: http.client.HTTPConnection | None = None
            response: http.client.HTTPResponse | None = None
            response_started = False
            try:
                connection = server.upstream_connector(
                    upstream, server.upstream_timeout_seconds
                )
                connection.request(
                    "GET",
                    upstream.path,
                    headers={
                        "Accept": (
                            "video/h264"
                            if upstream.codec == "h264"
                            else "multipart/x-mixed-replace"
                        ),
                        "Authorization": upstream.authorization,
                        "Cache-Control": "no-store",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                content_type = response.getheader("Content-Type", "")
                media_type = content_type.partition(";")[0].strip().lower()
                expected = (
                    "video/h264"
                    if upstream.codec == "h264"
                    else "multipart/x-mixed-replace"
                )
                if response.status != HTTPStatus.OK or media_type != expected:
                    self._error(HTTPStatus.BAD_GATEWAY)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                response_started = True
                while True:
                    # read(n) waits to fill n bytes on an endless HTTP stream.
                    # Low-bitrate H.264 can otherwise arrive in multi-second
                    # bursts, losing live timing before go2rtc timestamps it.
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        return
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (
                BrokenPipeError,
                ConnectionResetError,
                TimeoutError,
                socket.timeout,
                OSError,
                http.client.HTTPException,
            ):
                if not response_started:
                    self._error(HTTPStatus.BAD_GATEWAY)
            finally:
                if response is not None:
                    try:
                        response.close()
                    except OSError:
                        pass
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass

        def _error(self, status: HTTPStatus) -> None:
            body = b"Camera route unavailable.\n"
            try:
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


__all__ = [
    "CameraRouteRelayError",
    "CameraRouteRelayServer",
    "CameraRouteRelayState",
    "CameraRouteUpstream",
    "camera_route_relay_handler",
]
