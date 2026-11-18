"""One-owner gateway enrollment service for a customer-owned droplet."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus
from pathlib import Path
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .remote_access import _OPENSSH_ED25519, _secure_write


DEFAULT_STATE_DIRECTORY = Path("/var/lib/vms-gateway")
DEFAULT_AUTHORIZED_KEYS = DEFAULT_STATE_DIRECTORY / ".ssh" / "authorized_keys"
DEFAULT_HOST_KEY = Path("/etc/ssh/ssh_host_ed25519_key.pub")
STATE_FILE = "gateway-enrollment.json"
MAX_BODY_BYTES = 16 * 1024
FAILURE_LIMIT = 8
FAILURE_WINDOW_SECONDS = 60


class GatewayError(RuntimeError):
    """Gateway state or enrollment is invalid."""


def initialize_gateway(state_directory: Path) -> str:
    """Create a fresh one-time code and return it for one display."""

    enrollment_code = secrets.token_urlsafe(24)
    state = {
        "schema_version": 1,
        "code_hash": hashlib.sha256(enrollment_code.encode("ascii")).hexdigest(),
        "used": False,
        "public_key": None,
    }
    _secure_write(
        state_directory / STATE_FILE,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
    )
    return enrollment_code


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GatewayError("Gateway enrollment is not initialized.") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("code_hash"), str)
        or len(value["code_hash"]) != 64
        or not isinstance(value.get("used"), bool)
    ):
        raise GatewayError("Gateway enrollment state is invalid.")
    return value


def _validated_public_key(value: object) -> str:
    if not isinstance(value, str) or len(value) > 256:
        raise GatewayError("Recorder public key is invalid.")
    match = _OPENSSH_ED25519.fullmatch(value.strip())
    if match is None:
        raise GatewayError("Recorder public key is invalid.")
    try:
        raw = base64.b64decode(match.group(1), validate=True)
    except ValueError as error:
        raise GatewayError("Recorder public key is invalid.") from error
    # OpenSSH's public-key blob contains the algorithm and 32-byte key; a
    # bounded decode check rejects arbitrary base64 accepted by the regex.
    if not 45 <= len(raw) <= 64:
        raise GatewayError("Recorder public key is invalid.")
    return value.strip()


def _host_public_key(path: Path) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise GatewayError("The gateway SSH host key is unavailable.") from error
    match = _OPENSSH_ED25519.fullmatch(value)
    if match is None:
        raise GatewayError("The gateway SSH host key is invalid.")
    return f"ssh-ed25519 {match.group(1)}"


class GatewayEnrollmentApplication:
    """Tiny, rate-limited, one-code WSGI enrollment API."""

    def __init__(
        self,
        state_directory: Path = DEFAULT_STATE_DIRECTORY,
        *,
        authorized_keys: Path = DEFAULT_AUTHORIZED_KEYS,
        host_key: Path = DEFAULT_HOST_KEY,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state_path = state_directory / STATE_FILE
        self.authorized_keys = authorized_keys
        self.host_key = host_key
        self._time_source = time_source
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def __call__(self, environ: Mapping[str, object], start_response):
        if (
            str(environ.get("REQUEST_METHOD", "")).upper() != "POST"
            or environ.get("PATH_INFO") != "/v1/enroll"
        ):
            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        peer = str(environ.get("REMOTE_ADDR", "unknown"))[:128]
        if self._rate_limited(peer):
            return self._response(
                start_response,
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "try_later"},
            )
        try:
            length = int(str(environ.get("CONTENT_LENGTH", "0")))
        except ValueError:
            length = -1
        if not 1 <= length <= MAX_BODY_BYTES:
            self._record_failure(peer)
            return self._response(
                start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_request"}
            )
        body_stream = environ.get("wsgi.input")
        body = body_stream.read(length) if hasattr(body_stream, "read") else b""
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            request = None
        if not isinstance(request, dict):
            self._record_failure(peer)
            return self._response(
                start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_request"}
            )
        try:
            response = self._enroll(
                enrollment_code=request.get("enrollment_code"),
                public_key=request.get("public_key"),
            )
        except GatewayError:
            self._record_failure(peer)
            return self._response(
                start_response, HTTPStatus.FORBIDDEN, {"error": "enrollment_denied"}
            )
        self._failures.pop(peer, None)
        return self._response(start_response, HTTPStatus.OK, response)

    def _enroll(
        self, *, enrollment_code: object, public_key: object
    ) -> Mapping[str, object]:
        if not isinstance(enrollment_code, str) or not 16 <= len(enrollment_code) <= 128:
            raise GatewayError("Enrollment code is invalid.")
        key = _validated_public_key(public_key)
        supplied_hash = hashlib.sha256(enrollment_code.encode("utf-8")).hexdigest()
        with self._lock:
            state = _load_state(self.state_path)
            if not hmac.compare_digest(supplied_hash, state["code_hash"]):
                raise GatewayError("Enrollment code is invalid.")
            if state["used"] and state.get("public_key") != key:
                raise GatewayError("Enrollment code has already been used.")
            if not state["used"]:
                state["used"] = True
                state["public_key"] = key
                _secure_write(
                    self.state_path,
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                )
            authorized = (
                'restrict,port-forwarding,permitlisten="127.0.0.1:19081",'
                'command="/usr/local/libexec/vms-tunnel-hold" '
                f"{key}\n"
            )
            _secure_write(self.authorized_keys, authorized)
        return {
            "ssh_user": "vms-gateway",
            "ssh_port": 22,
            "reverse_port": 19081,
            "ssh_host_keys": [_host_public_key(self.host_key)],
        }

    def _rate_limited(self, peer: str) -> bool:
        now = self._time_source()
        cutoff = now - FAILURE_WINDOW_SECONDS
        failures = [value for value in self._failures.get(peer, ()) if value > cutoff]
        self._failures[peer] = failures
        return len(failures) >= FAILURE_LIMIT

    def _record_failure(self, peer: str) -> None:
        self._failures.setdefault(peer, []).append(self._time_source())

    @staticmethod
    def _response(start_response, status: HTTPStatus, value: Mapping[str, object]):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        start_response(
            f"{status.value} {status.phrase}",
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [body]


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vms-gateway")
    parser.add_argument(
        "--state-dir", default=str(DEFAULT_STATE_DIRECTORY), help=argparse.SUPPRESS
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help=argparse.SUPPRESS)
    serve = commands.add_parser("serve", help=argparse.SUPPRESS)
    serve.add_argument("--port", type=int, default=19080, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    state_directory = Path(arguments.state_dir).expanduser().resolve()
    if arguments.command == "init":
        print(initialize_gateway(state_directory))
        return 0
    application = GatewayEnrollmentApplication(
        state_directory,
        authorized_keys=state_directory / ".ssh" / "authorized_keys",
    )
    server: WSGIServer = make_server(
        "127.0.0.1", arguments.port, application, handler_class=_QuietHandler
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
