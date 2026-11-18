import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from video_monitoring_system.gateway import (
    GatewayEnrollmentApplication,
    initialize_gateway,
)
from video_monitoring_system.remote_access import (
    CommandResult,
    GATEWAY_KEY_FILE,
    GATEWAY_KNOWN_HOSTS_FILE,
    GATEWAY_TUNNEL_FILE,
    OwnerGatewayRemoteAccess,
    RemoteAccessError,
    RemoteAccessStore,
    TailscaleRemoteAccess,
    gateway_tunnel_command,
)


def ssh_public_key(comment="test"):
    value = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return f"{value} {comment}"


class RemoteAccessTests(unittest.TestCase):
    def test_status_always_tells_owner_the_stable_local_name(self):
        with tempfile.TemporaryDirectory() as directory:
            status = RemoteAccessStore(Path(directory)).status()

        self.assertEqual(status["local_url"], "http://osecameras.local")
        self.assertEqual(status["state"], "local-only")
        self.assertIsNone(status["remote_url"])

    def test_tailscale_setup_is_private_persistent_and_clears_funnel(self):
        calls = []
        status = json.dumps(
            {
                "BackendState": "Running",
                "Self": {"DNSName": "osecameras.owner.ts.net."},
            }
        )

        def runner(arguments, interactive):
            calls.append((tuple(arguments), interactive))
            if tuple(arguments)[1:] == ("status", "--json"):
                return CommandResult(0, status)
            return CommandResult(0)

        with tempfile.TemporaryDirectory() as directory:
            store = RemoteAccessStore(Path(directory))
            result = TailscaleRemoteAccess(
                store, runner=runner, executable="tailscale", portal_port=80
            ).configure()

        self.assertEqual(result["remote_url"], "https://osecameras.owner.ts.net")
        self.assertFalse(result["public_access"])
        self.assertIn((("tailscale", "funnel", "reset"), False), calls)
        self.assertIn(
            (
                (
                    "tailscale",
                    "serve",
                    "--bg",
                    "--yes",
                    "http://127.0.0.1:80",
                ),
                True,
            ),
            calls,
        )

    def test_tailscale_setup_shows_login_only_when_needed(self):
        calls = []
        states = iter(
            [
                {"BackendState": "NeedsLogin"},
                {
                    "BackendState": "Running",
                    "Self": {"DNSName": "osecameras.owner.ts.net."},
                },
                {
                    "BackendState": "Running",
                    "Self": {"DNSName": "osecameras.owner.ts.net."},
                },
            ]
        )

        def runner(arguments, interactive):
            calls.append((tuple(arguments), interactive))
            if tuple(arguments)[1:] == ("status", "--json"):
                return CommandResult(0, json.dumps(next(states)))
            return CommandResult(0)

        with tempfile.TemporaryDirectory() as directory:
            TailscaleRemoteAccess(
                RemoteAccessStore(Path(directory)),
                runner=runner,
                executable="tailscale",
            ).configure()

        self.assertIn(
            (
                (
                    "tailscale",
                    "login",
                    "--hostname=osecameras",
                    "--qr",
                    "--timeout=30m",
                ),
                True,
            ),
            calls,
        )

    def test_tailscale_accepts_running_state_at_login_timeout_boundary(self):
        states = iter(
            [
                {"BackendState": "NeedsLogin"},
                {
                    "BackendState": "Running",
                    "Self": {"DNSName": "osecameras.owner.ts.net."},
                },
                {
                    "BackendState": "Running",
                    "Self": {"DNSName": "osecameras.owner.ts.net."},
                },
            ]
        )

        def runner(arguments, interactive):
            if tuple(arguments)[1:] == ("status", "--json"):
                return CommandResult(0, json.dumps(next(states)))
            if tuple(arguments)[1] == "login":
                return CommandResult(1)
            return CommandResult(0)

        with tempfile.TemporaryDirectory() as directory:
            result = TailscaleRemoteAccess(
                RemoteAccessStore(Path(directory)),
                runner=runner,
                executable="tailscale",
            ).configure()

        self.assertEqual(result["state"], "ready")

    def test_browser_setup_returns_one_time_approval_without_persisting_it(self):
        calls = []

        def runner(arguments, interactive):
            calls.append((tuple(arguments), interactive))
            if tuple(arguments)[1:] == ("status", "--json"):
                return CommandResult(0, json.dumps({"BackendState": "NeedsLogin"}))
            return CommandResult(0)

        def start_login(arguments, callback):
            calls.append((tuple(arguments), "background"))
            return CommandResult(
                0,
                "Approve at https://login.tailscale.com/a/example-code\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            store = RemoteAccessStore(Path(directory))
            result = TailscaleRemoteAccess(
                store,
                runner=runner,
                login_starter=start_login,
                executable="tailscale",
            ).begin()
            persisted = store.status()

        self.assertEqual(result["state"], "approval-required")
        self.assertEqual(
            result["approval_url"],
            "https://login.tailscale.com/a/example-code",
        )
        self.assertEqual(persisted["state"], "local-only")
        self.assertNotIn("approval_url", persisted)
        self.assertIn(
            (
                (
                    "tailscale",
                    "login",
                    "--hostname=osecameras",
                    "--qr",
                    "--timeout=30m",
                ),
                "background",
            ),
            calls,
        )

    def test_browser_setup_rejects_untrusted_approval_urls(self):
        def runner(arguments, interactive):
            return CommandResult(0, json.dumps({"BackendState": "NeedsLogin"}))

        with tempfile.TemporaryDirectory() as directory:
            access = TailscaleRemoteAccess(
                RemoteAccessStore(Path(directory)),
                runner=runner,
                login_starter=lambda arguments, callback: CommandResult(
                    0, "Approve at https://example.com/not-tailscale"
                ),
                executable="tailscale",
            )
            with self.assertRaisesRegex(RemoteAccessError, "could not be started"):
                access.begin()

    def test_disabling_private_access_resets_serve_but_keeps_tailscale_login(self):
        calls = []

        def runner(arguments, interactive):
            calls.append((tuple(arguments), interactive))
            return CommandResult(0)

        with tempfile.TemporaryDirectory() as directory:
            store = RemoteAccessStore(Path(directory))
            result = TailscaleRemoteAccess(
                store, runner=runner, executable="tailscale"
            ).disable()

        self.assertEqual(result["state"], "local-only")
        self.assertIn((("tailscale", "serve", "reset"), False), calls)
        self.assertNotIn("logout", [argument for call, _ in calls for argument in call])

    def test_owner_gateway_enrollment_generates_and_pins_keys(self):
        seen = {}
        host_key = ssh_public_key("gateway")

        def enroll(domain, code, public_key):
            seen.update(domain=domain, code=code, public_key=public_key)
            return {
                "ssh_user": "vms-gateway",
                "ssh_port": 22,
                "reverse_port": 19081,
                "ssh_host_keys": [host_key],
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            access = OwnerGatewayRemoteAccess(
                RemoteAccessStore(root),
                enrollment_client=enroll,
                verifier=lambda domain: True,
                portal_port=80,
            )
            result = access.configure(
                domain="Cameras.Example.com.", enrollment_code="secret-code-123456"
            )
            command = gateway_tunnel_command(root)
            private_text = (root / GATEWAY_KEY_FILE).read_text(encoding="utf-8")
            known_hosts = (root / GATEWAY_KNOWN_HOSTS_FILE).read_text(encoding="utf-8")
            tunnel_state = (root / GATEWAY_TUNNEL_FILE).read_text(encoding="utf-8")

            self.assertEqual(oct(os.stat(root / GATEWAY_KEY_FILE).st_mode & 0o777), "0o600")

        self.assertEqual(seen["domain"], "cameras.example.com")
        self.assertNotIn(seen["code"], tunnel_state)
        self.assertIn("OPENSSH PRIVATE KEY", private_text)
        self.assertIn("[cameras.example.com]:22 ssh-ed25519", known_hosts)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn("127.0.0.1:19081:127.0.0.1:80", command)
        self.assertEqual(result["remote_url"], "https://cameras.example.com")

    def test_gateway_requires_a_hostname_not_a_url_or_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            access = OwnerGatewayRemoteAccess(RemoteAccessStore(Path(directory)))
            for invalid in ("https://example.com", "192.0.2.4", "localhost"):
                with self.subTest(invalid=invalid), self.assertRaises(RemoteAccessError):
                    access.configure(
                        domain=invalid, enrollment_code="secret-code-123456"
                    )

    def test_gateway_does_not_claim_ready_until_https_reaches_the_recorder(self):
        attempts = []

        def enroll(domain, code, public_key):
            return {
                "ssh_user": "vms-gateway",
                "ssh_port": 22,
                "reverse_port": 19081,
                "ssh_host_keys": [ssh_public_key("gateway")],
            }

        def verify(domain):
            attempts.append(domain)
            return len(attempts) == 3

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            access = OwnerGatewayRemoteAccess(
                RemoteAccessStore(root),
                enrollment_client=enroll,
                verifier=verify,
                sleep=lambda seconds: None,
            )
            result = access.configure(
                domain="cameras.example.com",
                enrollment_code="secret-code-123456",
            )

        self.assertEqual(result["state"], "ready")
        self.assertEqual(len(attempts), 3)

    def test_gateway_connectivity_failure_is_persisted_as_attention(self):
        def enroll(domain, code, public_key):
            return {
                "ssh_user": "vms-gateway",
                "ssh_port": 22,
                "reverse_port": 19081,
                "ssh_host_keys": [ssh_public_key("gateway")],
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            access = OwnerGatewayRemoteAccess(
                RemoteAccessStore(root),
                enrollment_client=enroll,
                verifier=lambda domain: False,
                sleep=lambda seconds: None,
            )
            with self.assertRaisesRegex(RemoteAccessError, "not ready"):
                access.configure(
                    domain="cameras.example.com",
                    enrollment_code="secret-code-123456",
                )
            status = RemoteAccessStore(root).status()

        self.assertEqual(status["state"], "attention")
        self.assertEqual(status["remote_url"], "https://cameras.example.com")

    def test_disabling_owner_gateway_removes_recorder_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RemoteAccessStore(root)
            store.save_status(
                {
                    "mode": "owner-gateway",
                    "state": "ready",
                    "remote_url": "https://cameras.example.com",
                }
            )
            for name in (
                GATEWAY_TUNNEL_FILE,
                GATEWAY_KEY_FILE,
                GATEWAY_KNOWN_HOSTS_FILE,
            ):
                (root / name).write_text("private\n", encoding="utf-8")
            access = OwnerGatewayRemoteAccess(store)

            result = access.disable()

            self.assertEqual(result["state"], "local-only")
            self.assertIsNone(result["remote_url"])
            self.assertFalse((root / GATEWAY_TUNNEL_FILE).exists())
            self.assertFalse((root / GATEWAY_KEY_FILE).exists())
            self.assertFalse((root / GATEWAY_KNOWN_HOSTS_FILE).exists())


class GatewayEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.authorized_keys = self.root / "authorized_keys"
        self.host_key = self.root / "ssh_host_ed25519_key.pub"
        self.host_key.write_text(ssh_public_key("host") + "\n", encoding="ascii")
        self.code = initialize_gateway(self.state)
        self.application = GatewayEnrollmentApplication(
            self.state,
            authorized_keys=self.authorized_keys,
            host_key=self.host_key,
        )

    def request(self, code, public_key):
        body = json.dumps(
            {"enrollment_code": code, "public_key": public_key}
        ).encode("utf-8")
        environ = {}
        setup_testing_defaults(environ)
        environ.update(
            {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/v1/enroll",
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "REMOTE_ADDR": "192.0.2.20",
                "wsgi.input": io.BytesIO(body),
            }
        )
        response = {}

        def start(status, headers):
            response["status"] = status
            response["headers"] = headers

        response["body"] = json.loads(
            b"".join(self.application(environ, start)).decode("utf-8")
        )
        return response

    def test_code_enrolls_exactly_one_recorder_key(self):
        key = ssh_public_key("recorder")
        accepted = self.request(self.code, key)
        replay = self.request(self.code, key)
        denied = self.request(self.code, ssh_public_key("other"))

        self.assertTrue(accepted["status"].startswith("200"))
        self.assertTrue(replay["status"].startswith("200"))
        self.assertTrue(denied["status"].startswith("403"))
        authorized = self.authorized_keys.read_text(encoding="utf-8")
        self.assertIn('permitlisten="127.0.0.1:19081"', authorized)
        self.assertIn("restrict,port-forwarding", authorized)
        self.assertEqual(oct(os.stat(self.authorized_keys).st_mode & 0o777), "0o600")

    def test_wrong_code_does_not_write_authorized_keys(self):
        response = self.request("wrong-code-123456789", ssh_public_key())

        self.assertTrue(response["status"].startswith("403"))
        self.assertFalse(self.authorized_keys.exists())
        self.assertEqual(response["body"], {"error": "enrollment_denied"})


if __name__ == "__main__":
    unittest.main()
