from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROBE = (
    ROOT
    / "platforms"
    / "raspberry-pi"
    / "camera-pilot"
    / "vms-dpp-reference-probe"
)
UNIT = PROBE.with_suffix(".service")


class DppReferenceProbeTests(unittest.TestCase):
    def test_probe_is_disposable_and_refuses_personalized_camera(self) -> None:
        script = PROBE.read_text(encoding="utf-8")

        self.assertIn("state=/run/vms-dpp-reference-probe", script)
        self.assertIn("[ ! -e /etc/vms/device.json ]", script)
        self.assertIn("fail refuses_personalized_camera", script)
        self.assertIn('rm -f "$config" "$pidfile"', script)
        self.assertNotIn("/etc/NetworkManager/system-connections", script)

    def test_probe_uses_dpp_without_accepting_network_secrets(self) -> None:
        script = PROBE.read_text(encoding="utf-8")

        self.assertIn("dpp_bootstrap_gen", script)
        self.assertIn("dpp_bootstrap_get_uri", script)
        self.assertIn("dpp_listen", script)
        for forbidden in ("passphrase", " wifi-password", " psk=", "--password"):
            self.assertNotIn(forbidden, script.lower())

    def test_unit_is_bounded_and_console_visible(self) -> None:
        unit = UNIT.read_text(encoding="utf-8")

        self.assertIn("ConditionPathExists=!/etc/vms/device.json", unit)
        self.assertIn("TimeoutStartSec=360", unit)
        self.assertIn("StandardOutput=journal+console", unit)


if __name__ == "__main__":
    unittest.main()
