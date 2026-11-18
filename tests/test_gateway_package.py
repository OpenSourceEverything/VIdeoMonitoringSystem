import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "platforms" / "linux" / "gateway"


class GatewayPackageTests(unittest.TestCase):
    def test_one_installer_prompts_only_for_the_owner_hostname(self):
        installer = (GATEWAY / "install-gateway").read_text(encoding="utf-8")

        self.assertTrue(os.access(GATEWAY / "install-gateway", os.X_OK))
        self.assertTrue(os.access(GATEWAY / "setup", os.X_OK))
        self.assertIn("/root/vms-domain", installer)
        self.assertIn("Camera address", installer)
        self.assertIn("One-time droplet code", installer)
        self.assertIn("Use a fresh dedicated droplet", installer)
        self.assertIn("ufw --force enable", installer)
        self.assertIn("ufw allow 22/tcp", installer)
        self.assertIn("ufw allow 80/tcp", installer)
        self.assertIn("ufw allow 443/tcp", installer)
        self.assertNotIn("ssh-keygen", installer)
        self.assertNotIn("--password", installer)

    def test_public_https_routes_only_to_loopback_services(self):
        caddy = (GATEWAY / "Caddyfile.template").read_text(encoding="utf-8")

        self.assertIn("handle_path /vms-enroll/*", caddy)
        self.assertIn("reverse_proxy 127.0.0.1:19080", caddy)
        self.assertIn("reverse_proxy 127.0.0.1:19081", caddy)
        self.assertIn("Strict-Transport-Security", caddy)
        self.assertNotIn("0.0.0.0:1908", caddy)

    def test_ssh_account_can_only_make_the_one_reverse_forward(self):
        sshd = (GATEWAY / "90-vms-gateway.conf").read_text(encoding="utf-8")

        self.assertIn("AuthenticationMethods publickey", sshd)
        self.assertIn("PasswordAuthentication no", sshd)
        self.assertIn("AllowTcpForwarding remote", sshd)
        self.assertIn("PermitListen 127.0.0.1:19081", sshd)
        self.assertIn("GatewayPorts no", sshd)
        self.assertIn("PermitTTY no", sshd)
        self.assertIn("MaxSessions 0", sshd)


if __name__ == "__main__":
    unittest.main()
