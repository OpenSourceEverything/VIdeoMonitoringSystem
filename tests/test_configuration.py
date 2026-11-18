import tempfile
import unittest
from pathlib import Path

from video_monitoring_system.configuration import (
    ConfigurationError,
    NetworkConfig,
    ProvisioningConfig,
    build_provisioning_config,
    load_provisioning_config,
    parse_provisioning_config,
)
from video_monitoring_system.model import SOFTWARE_CAMERA_LIMIT


VALID_JSON = """{
  "schema_version": 1,
  "mode": "kit",
  "camera_count": 4,
  "camera_model": "zero-2-w",
  "network": {"country": "US", "credential_source": "prompt"}
}"""

VALID_YAML = """schema_version: 1
mode: camera
camera_model: zero-w
camera_number: 1
network:
  ssid: Camera Setup
  country: US
  credential_source: prompt
"""


class ProvisioningConfigurationTests(unittest.TestCase):
    def test_json_returns_an_immutable_typed_configuration(self) -> None:
        result = parse_provisioning_config(VALID_JSON, format="json")

        self.assertEqual(
            result,
            ProvisioningConfig(
                schema_version=1,
                mode="kit",
                camera_model="zero-2-w",
                network=NetworkConfig(
                    credential_source="prompt",
                    country="US",
                ),
                camera_count=4,
                camera_number=None,
            ),
        )
        with self.assertRaises(AttributeError):
            result.camera_count = 2  # type: ignore[misc]

    def test_yaml_returns_optional_camera_number_and_ssid(self) -> None:
        result = parse_provisioning_config(VALID_YAML, format="yaml")

        self.assertEqual(result.camera_model, "zero-w")
        self.assertEqual(result.camera_number, 1)
        self.assertEqual(result.network.ssid, "Camera Setup")
        self.assertEqual(result.network.country, "US")
        self.assertEqual(result.network.credential_source, "prompt")

    def test_load_uses_a_platform_neutral_file_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "kit.yaml")
            path.write_text(VALID_YAML, encoding="utf-8")
            result = load_provisioning_config(path)

        self.assertEqual(result.camera_number, 1)

    def test_interface_builder_produces_the_same_request_as_yaml(self) -> None:
        parsed = parse_provisioning_config(VALID_YAML, format="yaml")

        built = build_provisioning_config(
            mode="camera",
            camera_model="zero-w",
            camera_number=1,
            ssid="Camera Setup",
            country="US",
        )

        self.assertEqual(built, parsed)

    def test_json_duplicate_fields_are_rejected(self) -> None:
        duplicate = VALID_JSON.replace('"camera_count": 4', '"camera_count": 1, "camera_count": 4')
        with self.assertRaisesRegex(ConfigurationError, "duplicate"):
            parse_provisioning_config(duplicate, format="json")

    def test_yaml_duplicate_fields_are_rejected(self) -> None:
        duplicate = VALID_YAML.replace("mode: camera", "mode: camera\nmode: kit")
        with self.assertRaisesRegex(ConfigurationError, "invalid YAML"):
            parse_provisioning_config(duplicate, format="yaml")

    def test_yaml_aliases_and_anchors_are_rejected(self) -> None:
        payload = """camera_count: 1
camera_model: zero-w
network: &network
  credential_source: prompt
copy: *network
"""
        with self.assertRaisesRegex(ConfigurationError, "aliases or anchors"):
            parse_provisioning_config(payload, format="yaml")

    def test_yaml_custom_tags_are_rejected(self) -> None:
        payload = VALID_YAML.replace("zero-w", "!device zero-w")
        with self.assertRaisesRegex(ConfigurationError, "invalid YAML"):
            parse_provisioning_config(payload, format="yaml")

    def test_unknown_fields_are_rejected_at_each_level(self) -> None:
        for payload in (
            VALID_JSON.replace('"camera_count": 4,', '"extra": true, "camera_count": 4,'),
            VALID_JSON.replace(
                '"credential_source": "prompt"',
                '"credential_source": "prompt", "mode": "home"',
            ),
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ConfigurationError, "unknown"):
                    parse_provisioning_config(payload, format="json")

    def test_secret_bearing_fields_are_rejected_recursively_without_echoing_value(self) -> None:
        private_value = "do-not-repeat-this-value"
        forbidden_names = (
            "password",
            "wifi_passphrase",
            "claimSecret",
            "access-token",
            "device_private_key",
        )
        for name in forbidden_names:
            payload = VALID_JSON.replace(
                '"credential_source": "prompt"',
                f'"credential_source": "prompt", "nested": {{"{name}": "{private_value}"}}',
            )
            with self.subTest(name=name):
                with self.assertRaises(ConfigurationError) as raised:
                    parse_provisioning_config(payload, format="json")
                self.assertIn("secret fields", str(raised.exception))
                self.assertNotIn(private_value, str(raised.exception))

    def test_range_model_and_boolean_values_are_strict(self) -> None:
        invalid_payloads = (
            VALID_JSON.replace('"camera_count": 4', '"camera_count": 0'),
            VALID_JSON.replace(
                '"camera_count": 4', f'"camera_count": {SOFTWARE_CAMERA_LIMIT + 1}'
            ),
            VALID_JSON.replace('"camera_count": 4', '"camera_count": true'),
            VALID_JSON.replace("zero-2-w", "zero-3-w"),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ConfigurationError):
                    parse_provisioning_config(payload, format="json")

    def test_camera_number_has_the_software_envelope_range(self) -> None:
        valid = VALID_JSON.replace('"mode": "kit",', '"mode": "camera",').replace(
            '  "camera_count": 4,\n',
            f'  "camera_number": {SOFTWARE_CAMERA_LIMIT},\n',
        )
        self.assertEqual(
            parse_provisioning_config(valid, format="json").camera_number,
            SOFTWARE_CAMERA_LIMIT,
        )

        for number in (0, SOFTWARE_CAMERA_LIMIT + 1, True):
            encoded = "true" if number is True else str(number)
            payload = VALID_JSON.replace(
                '"mode": "kit",', '"mode": "camera",'
            ).replace('  "camera_count": 4,\n', f'  "camera_number": {encoded},\n')
            with self.subTest(number=number):
                with self.assertRaises(ConfigurationError):
                    parse_provisioning_config(payload, format="json")

    def test_mode_requires_only_its_relevant_camera_selector(self) -> None:
        camera_with_count = VALID_JSON.replace('"mode": "kit"', '"mode": "camera"')
        kit_with_number = VALID_JSON.replace(
            '"camera_count": 4,', '"camera_count": 4, "camera_number": 1,'
        )
        for payload in (camera_with_count, kit_with_number):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ConfigurationError, "requires"):
                    parse_provisioning_config(payload, format="json")

    def test_schema_version_and_mode_are_explicit(self) -> None:
        for payload in (
            VALID_JSON.replace('"schema_version": 1,', ''),
            VALID_JSON.replace('"schema_version": 1', '"schema_version": 2'),
            VALID_JSON.replace('"mode": "kit"', '"mode": "device"'),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ConfigurationError):
                    parse_provisioning_config(payload, format="json")

    def test_network_requires_prompt_and_accepts_no_plaintext_credentials(self) -> None:
        missing = VALID_JSON.replace('"credential_source": "prompt"', "")
        wrong = VALID_JSON.replace('"credential_source": "prompt"', '"credential_source": "file"')
        plaintext = VALID_JSON.replace(
            '"credential_source": "prompt"',
            '"credential_source": "prompt", "password": "private"',
        )
        for payload in (missing, wrong, plaintext):
            with self.subTest(payload=payload):
                with self.assertRaises(ConfigurationError):
                    parse_provisioning_config(payload, format="json")

    def test_country_must_be_an_assigned_exact_uppercase_iso_code(self) -> None:
        for country in ("us", "USA", "ZZ", "00", 1, True):
            encoded = f'"{country}"' if isinstance(country, str) else str(country).lower()
            payload = VALID_JSON.replace('"country": "US"', f'"country": {encoded}')
            with self.subTest(country=country):
                with self.assertRaisesRegex(ConfigurationError, "country code"):
                    parse_provisioning_config(payload, format="json")

        british = VALID_JSON.replace('"country": "US"', '"country": "GB"')
        self.assertEqual(
            parse_provisioning_config(british, format="json").network.country,
            "GB",
        )

    def test_network_section_is_optional_and_still_defaults_to_private_prompt(self):
        payload = (
            '{"schema_version": 1, "mode": "kit", '
            '"camera_count": 4, "camera_model": "zero-2-w"}'
        )

        result = parse_provisioning_config(payload, format="json")

        self.assertEqual(result.network, NetworkConfig(credential_source="prompt"))

    def test_non_object_unsupported_suffix_and_oversize_input_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must be an object"):
            parse_provisioning_config("[]", format="json")
        with self.assertRaisesRegex(ConfigurationError, "too large"):
            parse_provisioning_config(" " * (64 * 1024 + 1), format="json")
        with self.assertRaisesRegex(ConfigurationError, "unsupported"):
            load_provisioning_config("settings.txt")


if __name__ == "__main__":
    unittest.main()
