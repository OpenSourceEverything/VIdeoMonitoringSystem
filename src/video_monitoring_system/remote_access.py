"""Owner-controlled remote access adapters.

The product never accepts camera credentials here and never exposes camera
ports.  Both adapters terminate at the password-protected recorder portal.
Secrets are supplied through an interactive prompt or HTTPS request body, not
command-line arguments, and are stored only in the private application data
directory.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LOCAL_PORTAL_PORT = 80
LOCAL_PORTAL_URL = "http://osecameras.local"
REMOTE_STATE_FILE = "remote-access.json"
GATEWAY_TUNNEL_FILE = "gateway-tunnel.json"
GATEWAY_KEY_FILE = "gateway-tunnel.key"
GATEWAY_KNOWN_HOSTS_FILE = "gateway-known-hosts"
TAILSCALE_HOSTNAME = "osecameras"
_DOMAIN = re.compile(
    r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_OPENSSH_ED25519 = re.compile(r"ssh-ed25519 ([A-Za-z0-9+/]{40,100}={0,2})(?: .*)?")
_TAILSCALE_APPROVAL_URL = re.compile(
    r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]+"
)


class RemoteAccessError(RuntimeError):
    """A safe, owner-facing remote-access operation failed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], bool], CommandResult]
LoginStarter = Callable[[Sequence[str], Callable[[], None]], CommandResult]
EnrollmentClient = Callable[[str, str, str], Mapping[str, object]]
GatewayVerifier = Callable[[str], bool]


def _default_runner(arguments: Sequence[str], interactive: bool) -> CommandResult:
    try:
        completed = subprocess.run(
            list(arguments),
            check=False,
            text=True,
            stdout=None if interactive else subprocess.PIPE,
            stderr=None if interactive else subprocess.PIPE,
        )
    except OSError as error:
        raise RemoteAccessError("The remote-access service is not installed.") from error
    return CommandResult(
        completed.returncode,
        "" if completed.stdout is None else completed.stdout,
        "" if completed.stderr is None else completed.stderr,
    )


def _default_login_starter(
    arguments: Sequence[str], completed_callback: Callable[[], None]
) -> CommandResult:
    """Start owner approval without tying up the recorder web request."""

    try:
        process = subprocess.Popen(
            list(arguments),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
    except OSError as error:
        raise RemoteAccessError("The remote-access service is not installed.") from error
    if process.stdout is None:
        process.terminate()
        raise RemoteAccessError("Tailscale sign-in could not be started.")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = ""
    deadline = time.monotonic() + 15
    approval_url: str | None = None
    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if not selector.select(timeout=remaining):
                break
            line = process.stdout.readline()
            if line:
                output = (output + line)[-65536:]
                match = _TAILSCALE_APPROVAL_URL.search(output)
                if match is not None:
                    approval_url = match.group(0)
                    break
            elif process.poll() is not None:
                break
    finally:
        selector.close()

    if approval_url is None:
        if process.poll() is None:
            process.terminate()
        remainder, _ = process.communicate()
        output = (output + (remainder or ""))[-65536:]
        return CommandResult(process.returncode or 1, output)

    def finish() -> None:
        try:
            process.communicate()
            completed_callback()
        except Exception:
            # A later page load retries the authoritative status check.
            pass

    threading.Thread(target=finish, daemon=True).start()
    return CommandResult(0, output)


def _secure_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _json_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise RemoteAccessError(f"{label} returned an unreadable status.") from error
    if not isinstance(value, dict):
        raise RemoteAccessError(f"{label} returned an unreadable status.")
    return value


def _validate_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not _DOMAIN.fullmatch(domain):
        raise RemoteAccessError(
            "Enter only the public hostname you pointed at your droplet."
        )
    return domain


class RemoteAccessStore:
    """Persist only owner-displayable remote state and tunnel material."""

    def __init__(self, data_directory: Path) -> None:
        self.data_directory = data_directory

    def save_status(self, status: Mapping[str, object]) -> None:
        _secure_write(
            self.data_directory / REMOTE_STATE_FILE,
            json.dumps(dict(status), indent=2, sort_keys=True) + "\n",
        )

    def status(self) -> dict[str, object]:
        default: dict[str, object] = {
            "mode": "off",
            "state": "local-only",
            "local_url": LOCAL_PORTAL_URL,
            "remote_url": None,
        }
        path = self.data_directory / REMOTE_STATE_FILE
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {**default, "state": "attention"}
        if not isinstance(value, dict):
            return {**default, "state": "attention"}
        return {**default, **value, "local_url": LOCAL_PORTAL_URL}


class TailscaleRemoteAccess:
    """Configure private HTTPS access inside the owner's own tailnet."""

    def __init__(
        self,
        store: RemoteAccessStore,
        *,
        runner: CommandRunner = _default_runner,
        executable: str | None = None,
        portal_port: int = LOCAL_PORTAL_PORT,
        command_prefix: Sequence[str] = (),
        login_starter: LoginStarter = _default_login_starter,
    ) -> None:
        self.store = store
        self._runner = runner
        self.executable = executable or shutil.which("tailscale") or "tailscale"
        self.portal_port = portal_port
        self.command_prefix = tuple(command_prefix)
        self._login_starter = login_starter
        self._approval_url: str | None = None
        self._lock = threading.Lock()

    def _command(self, *arguments: str) -> tuple[str, ...]:
        return (*self.command_prefix, self.executable, *arguments)

    def configure(self) -> Mapping[str, object]:
        status = self._status()
        if status.get("BackendState") != "Running":
            login = self._runner(
                self._command(
                    "login",
                    f"--hostname={TAILSCALE_HOSTNAME}",
                    "--qr",
                    "--timeout=30m",
                ),
                True,
            )
            # Authorization may land at the edge of Tailscale's timeout.  The
            # resulting appliance state is authoritative, not the helper's
            # stale exit code.
            status = self._status()
            if login.returncode != 0 and status.get("BackendState") != "Running":
                raise RemoteAccessError("Tailscale sign-in did not finish.")
        if status.get("BackendState") != "Running":
            raise RemoteAccessError("Tailscale is waiting for owner approval.")

        return self._enable(status)

    def begin(self) -> Mapping[str, object]:
        """Begin browser-friendly private access and return owner approval state."""

        with self._lock:
            status = self._status()
            if status.get("BackendState") == "Running":
                self._approval_url = None
                return self._enable(status)
            if self._approval_url is None:
                started = self._login_starter(
                    self._command(
                        "login",
                        f"--hostname={TAILSCALE_HOSTNAME}",
                        "--qr",
                        "--timeout=30m",
                    ),
                    self._finish_after_login,
                )
                combined = f"{started.stdout}\n{started.stderr}"
                match = _TAILSCALE_APPROVAL_URL.search(combined)
                if started.returncode != 0 or match is None:
                    raise RemoteAccessError("Tailscale sign-in could not be started.")
                self._approval_url = match.group(0)
            return {
                "mode": "private-vpn",
                "state": "approval-required",
                "local_url": LOCAL_PORTAL_URL,
                "remote_url": None,
                "public_access": False,
                "approval_url": self._approval_url,
            }

    def disable(self) -> Mapping[str, object]:
        """Disable remote serving without logging the owner out of Tailscale."""

        with self._lock:
            funnel = self._runner(self._command("funnel", "reset"), False)
            serve = self._runner(self._command("serve", "reset"), False)
            if funnel.returncode != 0 or serve.returncode != 0:
                raise RemoteAccessError("Private Tailscale access could not be disabled.")
            self._approval_url = None
            result: dict[str, object] = {
                "mode": "off",
                "state": "local-only",
                "local_url": LOCAL_PORTAL_URL,
                "remote_url": None,
                "public_access": False,
            }
            self.store.save_status(result)
            return result

    def _finish_after_login(self) -> None:
        with self._lock:
            status = self._status()
            if status.get("BackendState") == "Running":
                self._approval_url = None
                self._enable(status)

    def _enable(self, status: Mapping[str, object]) -> Mapping[str, object]:
        # This is a dedicated appliance. Resetting Funnel is intentional: the
        # public Tailscale feature must never survive a previous experiment.
        funnel = self._runner(self._command("funnel", "reset"), False)
        if funnel.returncode != 0:
            raise RemoteAccessError("Public Tailscale access could not be disabled.")

        serve = self._runner(
            self._command(
                "serve",
                "--bg",
                "--yes",
                f"http://127.0.0.1:{self.portal_port}",
            ),
            True,
        )
        if serve.returncode != 0:
            raise RemoteAccessError("Private Tailscale access could not be enabled.")

        status = self._status()
        own = status.get("Self")
        dns_name = own.get("DNSName") if isinstance(own, dict) else None
        if not isinstance(dns_name, str) or not dns_name.strip("."):
            raise RemoteAccessError("Tailscale did not provide a private address.")
        remote_url = f"https://{dns_name.rstrip('.')}"
        result: dict[str, object] = {
            "mode": "private-vpn",
            "state": "ready",
            "local_url": LOCAL_PORTAL_URL,
            "remote_url": remote_url,
            "public_access": False,
        }
        self.store.save_status(result)
        return result

    def _status(self) -> dict[str, Any]:
        result = self._runner(self._command("status", "--json"), False)
        if result.returncode != 0:
            raise RemoteAccessError("Tailscale is not running on the recorder.")
        return _json_object(result.stdout, label="Tailscale")


def _default_enrollment_client(
    domain: str, enrollment_code: str, public_key: str
) -> Mapping[str, object]:
    body = json.dumps(
        {"enrollment_code": enrollment_code, "public_key": public_key},
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        f"https://{domain}/vms-enroll/v1/enroll",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(32 * 1024 + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RemoteAccessError(
            "The owner gateway could not be reached securely."
        ) from error
    if len(raw) > 32 * 1024:
        raise RemoteAccessError("The owner gateway returned an invalid response.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RemoteAccessError("The owner gateway returned an invalid response.") from error
    if not isinstance(value, dict):
        raise RemoteAccessError("The owner gateway returned an invalid response.")
    return value


def _default_gateway_verifier(domain: str) -> bool:
    request = Request(
        f"https://{domain}/login",
        headers={"Accept": "text/html"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            response.read(1)
            return response.status == 200
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _new_ssh_identity() -> tuple[str, str]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as error:
        raise RemoteAccessError("Secure gateway key support is not installed.") from error
    private = Ed25519PrivateKey.generate()
    private_text = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_text = private.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return private_text, f"{public_text} osecameras"


class OwnerGatewayRemoteAccess:
    """Enroll the recorder with a gateway owned by the same customer."""

    def __init__(
        self,
        store: RemoteAccessStore,
        *,
        enrollment_client: EnrollmentClient = _default_enrollment_client,
        verifier: GatewayVerifier = _default_gateway_verifier,
        sleep: Callable[[float], None] = time.sleep,
        portal_port: int = LOCAL_PORTAL_PORT,
    ) -> None:
        self.store = store
        self._enrollment_client = enrollment_client
        self._verifier = verifier
        self._sleep = sleep
        self.portal_port = portal_port

    def configure(
        self, *, domain: str, enrollment_code: str
    ) -> Mapping[str, object]:
        selected_domain = _validate_domain(domain)
        code = enrollment_code.strip()
        if not 16 <= len(code) <= 128 or any(character.isspace() for character in code):
            raise RemoteAccessError("The one-time gateway code is invalid.")
        private_key, public_key = _new_ssh_identity()
        response = self._enrollment_client(selected_domain, code, public_key)
        tunnel = self._validated_gateway_response(selected_domain, response)

        key_path = self.store.data_directory / GATEWAY_KEY_FILE
        known_hosts_path = self.store.data_directory / GATEWAY_KNOWN_HOSTS_FILE
        tunnel_path = self.store.data_directory / GATEWAY_TUNNEL_FILE
        known_hosts = "".join(
            f"[{selected_domain}]:{tunnel['ssh_port']} {key}\n"
            for key in tunnel.pop("ssh_host_keys")
        )
        _secure_write(key_path, private_key)
        _secure_write(known_hosts_path, known_hosts)
        tunnel.update(
            {
                "schema_version": 1,
                "key_file": str(key_path),
                "known_hosts_file": str(known_hosts_path),
                "local_port": self.portal_port,
            }
        )
        # This file is written last because the recorder's systemd path unit
        # treats its existence as the signal to establish the tunnel.
        _secure_write(tunnel_path, json.dumps(tunnel, indent=2, sort_keys=True) + "\n")
        result: dict[str, object] = {
            "mode": "owner-gateway",
            "state": "ready",
            "local_url": LOCAL_PORTAL_URL,
            "remote_url": f"https://{selected_domain}",
            "public_access": True,
            "gateway_owned_by_customer": True,
        }
        for attempt in range(15):
            if self._verifier(selected_domain):
                break
            if attempt < 14:
                self._sleep(1)
        else:
            self.store.save_status({**result, "state": "attention"})
            raise RemoteAccessError(
                "The gateway enrolled, but its HTTPS connection is not ready."
            )
        self.store.save_status(result)
        return result

    def disable(self) -> Mapping[str, object]:
        """Remove the recorder-side owner gateway identity and tunnel state."""

        for name in (GATEWAY_TUNNEL_FILE, GATEWAY_KEY_FILE, GATEWAY_KNOWN_HOSTS_FILE):
            try:
                (self.store.data_directory / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise RemoteAccessError("Owner gateway access could not be disabled.") from error
        result: dict[str, object] = {
            "mode": "off",
            "state": "local-only",
            "local_url": LOCAL_PORTAL_URL,
            "remote_url": None,
            "public_access": False,
        }
        self.store.save_status(result)
        return result

    @staticmethod
    def _validated_gateway_response(
        domain: str, response: Mapping[str, object]
    ) -> dict[str, Any]:
        user = response.get("ssh_user")
        ssh_port = response.get("ssh_port")
        reverse_port = response.get("reverse_port")
        host_keys = response.get("ssh_host_keys")
        if user != "vms-gateway":
            raise RemoteAccessError("The owner gateway returned an invalid identity.")
        if isinstance(ssh_port, bool) or not isinstance(ssh_port, int) or not 1 <= ssh_port <= 65535:
            raise RemoteAccessError("The owner gateway returned an invalid SSH port.")
        if reverse_port != 19081:
            raise RemoteAccessError("The owner gateway returned an invalid tunnel port.")
        if not isinstance(host_keys, list) or not 1 <= len(host_keys) <= 4:
            raise RemoteAccessError("The owner gateway returned no trusted host key.")
        validated_keys: list[str] = []
        for key in host_keys:
            if not isinstance(key, str) or _OPENSSH_ED25519.fullmatch(key.strip()) is None:
                raise RemoteAccessError("The owner gateway returned an invalid host key.")
            validated_keys.append(key.strip())
        return {
            "domain": domain,
            "ssh_user": user,
            "ssh_port": ssh_port,
            "reverse_port": reverse_port,
            "ssh_host_keys": validated_keys,
        }


def gateway_tunnel_command(data_directory: Path) -> tuple[str, ...]:
    """Return the fixed, host-key-pinned outbound tunnel command."""

    path = data_directory / GATEWAY_TUNNEL_FILE
    try:
        state = _json_object(path.read_text(encoding="utf-8"), label="Gateway tunnel")
    except (FileNotFoundError, OSError, UnicodeError) as error:
        raise RemoteAccessError("Owner gateway access is not configured.") from error
    domain = _validate_domain(str(state.get("domain", "")))
    if state.get("ssh_user") != "vms-gateway":
        raise RemoteAccessError("Owner gateway tunnel configuration is invalid.")
    integers: dict[str, int] = {}
    for name in ("ssh_port", "reverse_port", "local_port"):
        value = state.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise RemoteAccessError("Owner gateway tunnel configuration is invalid.")
        integers[name] = value
    key_file = Path(str(state.get("key_file", ""))).resolve()
    known_hosts_file = Path(str(state.get("known_hosts_file", ""))).resolve()
    root = data_directory.resolve()
    if key_file.parent != root or known_hosts_file.parent != root:
        raise RemoteAccessError("Owner gateway tunnel configuration is invalid.")
    if not key_file.is_file() or not known_hosts_file.is_file():
        raise RemoteAccessError("Owner gateway tunnel keys are missing.")
    return (
        shutil.which("ssh") or "ssh",
        "-NT",
        "-p",
        str(integers["ssh_port"]),
        "-i",
        str(key_file),
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-R",
        f"127.0.0.1:{integers['reverse_port']}:127.0.0.1:{integers['local_port']}",
        f"vms-gateway@{domain}",
    )


class RemoteAccessManager:
    """Small application facade shared by CLI and the future setup screen."""

    def __init__(
        self,
        data_directory: Path,
        *,
        tailscale: TailscaleRemoteAccess | None = None,
        gateway: OwnerGatewayRemoteAccess | None = None,
    ) -> None:
        self.store = RemoteAccessStore(data_directory)
        tailscale_prefix = (
            ("sudo", "-n")
            if os.environ.get("VMS_TAILSCALE_SUDO") == "1"
            else ()
        )
        self.tailscale = tailscale or TailscaleRemoteAccess(
            self.store, command_prefix=tailscale_prefix
        )
        self.gateway = gateway or OwnerGatewayRemoteAccess(self.store)

    def status(self) -> Mapping[str, object]:
        return self.store.status()

    def setup_private(self) -> Mapping[str, object]:
        return self.tailscale.configure()

    def begin_private(self) -> Mapping[str, object]:
        return self.tailscale.begin()

    def disable_private(self) -> Mapping[str, object]:
        if self.store.status().get("mode") == "owner-gateway":
            return self.gateway.disable()
        return self.tailscale.disable()

    def setup_gateway(self, *, domain: str, enrollment_code: str) -> Mapping[str, object]:
        return self.gateway.configure(domain=domain, enrollment_code=enrollment_code)
