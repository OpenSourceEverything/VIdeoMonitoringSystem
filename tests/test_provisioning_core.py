import base64
from dataclasses import replace
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from video_monitoring_system.model import SOFTWARE_CAMERA_LIMIT
from video_monitoring_system.provisioning import (
    BOARD_PROFILES,
    CryptographyEd25519KeyGenerator,
    KeyMaterial,
    OpaqueTokenKeyGenerator,
    PILOT_RECOVERY_USERNAME,
    PREINSTALLED_CAMERA_IMAGE_REFERENCE,
    PREINSTALLED_RECORDER_IMAGE_REFERENCE,
    ProvisioningJob,
    ProvisioningSpec,
    camera_dhcp_client_id,
    get_board_profile,
    pilot_recovery_ssh_authorized_key,
    pilot_recovery_ssh_private_key,
    render_network_config,
    render_networkmanager_connection,
    secure_write_text,
    temporary_secret_file,
    validate_camera_network_compatibility,
)


class DeterministicKeyGenerator:
    def __init__(self) -> None:
        self.index = 0

    def generate(self) -> KeyMaterial:
        self.index += 1
        return KeyMaterial(
            algorithm="test-only",
            public_identity=f"public-{self.index}",
            private_identity=f"private-{self.index}",
        )


def decoded_cloud_file(user_data: str, path: str) -> str:
    """Return one base64 write_files payload from rendered cloud-init data."""

    lines = user_data.splitlines()
    marker = f"- path: {path}"
    start = next(
        index for index, line in enumerate(lines) if line.strip() == marker
    )
    encoded = next(
        line.split("content:", 1)[1].strip()
        for line in lines[start + 1 :]
        if line.strip().startswith("content:")
    )
    return base64.b64decode(encoded).decode("utf-8")


class BoardProfileTests(unittest.TestCase):
    def test_expected_profiles_and_roles_exist(self) -> None:
        self.assertEqual(set(BOARD_PROFILES), {"zero-w", "zero-2-w", "pi4"})
        self.assertEqual(get_board_profile("zero-w").architecture, "armv6")
        self.assertEqual(get_board_profile("zero-w").release_tier, "pilot")
        self.assertIn("camera", get_board_profile("zero-2-w").roles)
        self.assertEqual(get_board_profile("zero-2-w").release_tier, "product")
        self.assertIn("recorder", get_board_profile("pi4").roles)

    def test_unsupported_board_or_role_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            get_board_profile("unknown")
        with self.assertRaises(ValueError):
            get_board_profile("zero-w", "recorder")


class IdentityTests(unittest.TestCase):
    def test_fallback_generator_creates_distinct_opaque_identities(self) -> None:
        generator = OpaqueTokenKeyGenerator()
        first = generator.generate()
        second = generator.generate()
        self.assertEqual(first.algorithm, "opaque-token-sha256-v1")
        self.assertNotEqual(first.private_identity, second.private_identity)
        self.assertNotEqual(first.public_identity, second.public_identity)
        self.assertGreaterEqual(len(first.private_identity), 43)
        self.assertNotIn(first.private_identity, first.public_identity)

    def test_too_little_identity_entropy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OpaqueTokenKeyGenerator(16).generate()


class ProvisioningJobTests(unittest.TestCase):
    def test_camera_limit_and_role_validation(self) -> None:
        for count in (0, SOFTWARE_CAMERA_LIMIT + 1):
            with self.assertRaises(ValueError):
                ProvisioningSpec(camera_count=count)
        with self.assertRaises(ValueError):
            ProvisioningSpec(camera_count=1, recorder_board="zero-w")

    def test_pluggable_generator_and_unique_per_device_secrets(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=4),
            DeterministicKeyGenerator(),
            wifi_country="US",
        )
        devices = (job.identity.recorder, *job.identity.cameras)
        self.assertEqual(
            [device.key.public_identity for device in devices],
            [f"public-{index}" for index in range(1, 6)],
        )
        claims = [camera.claim_secret for camera in job.identity.cameras]
        stream_secrets = [camera.stream_secret for camera in job.identity.cameras]
        self.assertEqual(len(set(claims)), 4)
        self.assertEqual(len(set(stream_secrets)), 4)
        self.assertNotIn(None, claims)
        self.assertNotIn(None, stream_secrets)
        for camera in job.identity.cameras:
            self.assertNotEqual(camera.stream_secret, camera.claim_secret)
            self.assertNotEqual(camera.stream_secret, camera.key.private_identity)
            self.assertRegex(camera.claim_secret or "", r"^[0-9A-F]{32}$")
        self.assertGreaterEqual(len(job.identity.wifi_passphrase), 32)

    def test_cameras_are_one_based_and_have_stable_product_hostnames(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=3),
            DeterministicKeyGenerator(),
            wifi_country="US",
        )
        manifest = job.public_manifest()
        self.assertIsNone(job.identity.recorder.camera_number)
        self.assertIsNone(manifest.recorder.camera_number)
        self.assertEqual(
            [camera.camera_number for camera in job.identity.cameras], [1, 2, 3]
        )
        self.assertEqual(
            [camera.camera_number for camera in manifest.cameras], [1, 2, 3]
        )
        configs = job.device_configurations()
        self.assertIn('hostname: "osecameras"', configs[0].user_data)
        self.assertIn('hostname: "camera-001"', configs[1].user_data)
        self.assertIn('hostname: "camera-003"', configs[3].user_data)

    def test_public_manifest_contains_no_private_material(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=2), wifi_country="US"
        )
        public_json = job.public_manifest().to_json()
        private_values = [
            job.identity.wifi_passphrase,
            job.identity.recorder.key.private_identity,
            *[camera.key.private_identity for camera in job.identity.cameras],
            *[camera.claim_secret or "" for camera in job.identity.cameras],
            *[camera.stream_secret or "" for camera in job.identity.cameras],
        ]
        for value in private_values:
            self.assertNotIn(value, public_json)
        parsed = json.loads(public_json)
        self.assertEqual(len(parsed["cameras"]), 2)
        self.assertNotIn("wifi_passphrase", parsed)
        self.assertNotIn("claim_secret", public_json)
        self.assertNotIn("private_identity", public_json)
        self.assertNotIn("stream_secret", public_json)

    def test_shared_images_have_distinct_device_configuration(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=3), wifi_country="US"
        )
        assignments = {item.image_reference: item for item in job.image_assignments()}
        self.assertEqual(len(assignments["raspios-32-vms-camera"].device_ids), 3)
        self.assertEqual(
            len(assignments[PREINSTALLED_RECORDER_IMAGE_REFERENCE].device_ids), 1
        )

        configurations = job.device_configurations()
        camera_configs = [
            item
            for item in configurations
            if item.image_reference == "raspios-32-vms-camera"
        ]
        self.assertEqual(len({item.user_data for item in camera_configs}), 3)
        for config in camera_configs:
            self.assertEqual(
                config.network_config,
                "version: 2\nrenderer: NetworkManager\nwifis: {}\n",
            )
            self.assertNotIn(job.identity.wifi_ssid, config.network_config)
            self.assertNotIn(job.identity.wifi_passphrase, config.network_config)
            self.assertTrue(config.user_data.startswith("#cloud-config\n"))
            self.assertIn("permissions: '0600'", config.user_data)
            self.assertIn("ssh_pwauth: false", config.user_data)
            self.assertIn(
                "systemctl mask --runtime --now userconfig.service",
                config.user_data,
            )
            self.assertIn("systemctl disable --now ssh.service", config.user_data)
            self.assertNotIn("packages:", config.user_data)
            first_boot = decoded_cloud_file(
                config.user_data, "/usr/local/libexec/vms-first-boot"
            )
            connection_id = next(
                line.split("=", 1)[1]
                for line in config.network_manager.splitlines()
                if line.startswith("id=")
            )
            keyfile = decoded_cloud_file(
                config.user_data,
                f"/etc/NetworkManager/system-connections/{connection_id}.nmconnection",
            )
            first_boot_service = decoded_cloud_file(
                config.user_data,
                "/etc/systemd/system/vms-first-boot.service",
            )
            self.assertIn("autoconnect=false", keyfile)
            self.assertNotIn("autoconnect=true", keyfile)
            self.assertIn("powersave=2", keyfile)
            self.assertIn(
                "After=NetworkManager.service cloud-config.service",
                first_boot_service,
            )
            self.assertIn("Wants=NetworkManager.service", first_boot_service)
            self.assertNotIn("network-online.target", first_boot_service)
            self.assertIn("StandardOutput=journal\n", first_boot_service)
            self.assertIn("StandardError=journal\n", first_boot_service)
            self.assertIn("TimeoutStartSec=2400\n", first_boot_service)
            self.assertNotIn("journal+console", first_boot_service)
            self.assertLess(
                first_boot.index("raspi-config nonint do_wifi_country US"),
                first_boot.index('nmcli connection load "$keyfile"'),
            )
            self.assertLess(
                first_boot.index('nmcli connection load "$keyfile"'),
                first_boot.index(
                    f"nmcli connection modify {connection_id} "
                    "connection.autoconnect yes"
                ),
            )
            self.assertLess(
                first_boot.index(
                    f"nmcli connection modify {connection_id} "
                    "connection.autoconnect yes"
                ),
                first_boot.index("nmcli connection reload"),
            )
            self.assertLess(
                first_boot.index("nmcli connection reload"),
                first_boot.index(
                    f"nmcli --wait 60 connection up {connection_id}"
                ),
            )
            self.assertLess(
                first_boot.index(
                    f"nmcli --wait 60 connection up {connection_id}"
                ),
                first_boot.index("nm-online -q --timeout=60"),
            )
            self.assertLess(
                first_boot.index("nm-online -q --timeout=60"),
                first_boot.index("apt-get update"),
            )
            self.assertLess(
                first_boot.index('mv -f "$temporary" "$marker"'),
                first_boot.index("systemctl disable vms-first-boot.service"),
            )
            self.assertIn(
                'write_status "{\\"schema_version\\":1,\\"state\\":'
                '\\"failed\\",\\"exit_code\\":$code}"',
                first_boot,
            )
            self.assertIn(': > "$cloud_log"', first_boot)
        recorder = next(
            item
            for item in configurations
            if item.image_reference == PREINSTALLED_RECORDER_IMAGE_REFERENCE
        )
        self.assertIn("  - name: vms\n", recorder.user_data)
        self.assertIn("    owner: root:vms\n", recorder.user_data)
        self.assertIn("    permissions: '0640'\n", recorder.user_data)
        self.assertIn("eth0", recorder.network_config)
        self.assertNotIn(job.identity.wifi_passphrase, recorder.network_config)
        encoded_line = next(
            line for line in recorder.user_data.splitlines() if "content:" in line
        )
        private_document = json.loads(
            base64.b64decode(encoded_line.split("content:", 1)[1].strip())
        )
        self.assertNotIn("private_network", private_document)
        recorder_first_boot = decoded_cloud_file(
            recorder.user_data, "/usr/local/libexec/vms-first-boot"
        )
        self.assertIn("raspi-config nonint do_wifi_country US", recorder_first_boot)
        self.assertIn(
            "systemctl enable --now",
            recorder_first_boot,
        )
        self.assertIn("vms-recording-ready.path", recorder_first_boot)
        self.assertIn("vms-recorder-maintenance.timer", recorder_first_boot)
        recorder_first_boot_service = decoded_cloud_file(
            recorder.user_data, "/etc/systemd/system/vms-first-boot.service"
        )
        self.assertIn("cloud-init-network.service", recorder_first_boot_service)
        self.assertIn("TimeoutStartSec=600", recorder_first_boot_service)
        self.assertNotIn("runcmd:", recorder.user_data)

    def test_preinstalled_camera_uses_baked_dispatcher_and_bounded_checks(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(
                camera_count=1,
                camera_board="zero-w",
                camera_image=PREINSTALLED_CAMERA_IMAGE_REFERENCE,
            ),
            DeterministicKeyGenerator(),
            wifi_ssid="Development Network",
            wifi_passphrase="correct horse battery staple",
            wifi_country="US",
        )
        camera = job.device_configurations()[1]
        first_boot = decoded_cloud_file(
            camera.user_data, "/usr/local/libexec/vms-first-boot"
        )
        first_boot_service = decoded_cloud_file(
            camera.user_data, "/etc/systemd/system/vms-first-boot.service"
        )
        ready_marker = 'mv -f "$temporary" "$marker"'

        self.assertIn("python3 -c 'import picamera2'", first_boot)
        self.assertIn("command -v avahi-daemon", first_boot)
        self.assertIn("connection.autoconnect yes", first_boot)
        self.assertIn(
            "timeout 65 nmcli --wait 60 connection up vms-home-001",
            first_boot,
        )
        self.assertIn("nm-online -q --timeout=60", first_boot)
        self.assertIn("GENERAL.CONNECTION:vms-home-001", first_boot)
        self.assertIn(
            "timeout 90 systemctl enable --now avahi-daemon.service",
            first_boot,
        )
        self.assertIn("vms-network-recovery.timer", first_boot)
        self.assertIn("systemctl start vms-network-recovery.service", first_boot)
        self.assertIn("/var/lib/vms/network-health.json", first_boot)
        self.assertIn('health["network"].get("state") == "healthy"', first_boot)
        self.assertIn(
            'health["network"].get("recorder_reachable") is True', first_boot
        )
        self.assertIn('health.get("camera_ready") is True', first_boot)
        self.assertIn('"http://127.0.0.1:9080/stream.h264"', first_boot)
        self.assertIn('content_type != "video/h264"', first_boot)
        self.assertIn('stream_prefix.startswith(b"\\x00\\x00\\x00\\x01")', first_boot)
        self.assertNotIn("system_ready", first_boot)
        self.assertNotIn("apt-get", first_boot)
        self.assertNotIn("path: /opt/vms/camera_agent.py", camera.user_data)
        self.assertNotIn("path: /opt/vms/network_recovery.py", camera.user_data)
        self.assertNotIn("path: /usr/local/bin/vms-pilot-diagnostics", camera.user_data)
        self.assertIn("systemctl restart ssh.service", first_boot)
        self.assertNotIn("bootcmd:", camera.user_data)
        self.assertNotIn("runcmd:", camera.user_data)
        self.assertIn(
            "After=NetworkManager.service cloud-init-network.service",
            first_boot_service,
        )
        self.assertIn(
            "Requires=cloud-init-network.service",
            first_boot_service,
        )
        self.assertIn("TimeoutStartSec=600", first_boot_service)
        self.assertNotIn("cloud-init-local.service", first_boot_service)
        self.assertNotIn("cloud-config.service", first_boot_service)
        self.assertNotIn("cloud-final.service", first_boot_service)
        self.assertNotIn("network-online.target", first_boot_service)
        self.assertIn(
            "systemctl is-active --quiet cloud-init-network.service",
            first_boot,
        )
        self.assertIn(
            "systemctl show --property=Result --value cloud-init-network.service",
            first_boot,
        )
        for premature_cleanup in (
            "/boot/firmware/user-data",
            "/var/lib/cloud/instances",
            "/var/log/cloud-init.log",
        ):
            with self.subTest(premature_cleanup=premature_cleanup):
                self.assertNotIn(premature_cleanup, first_boot)
        self.assertNotIn(job.identity.cameras[0].stream_secret, first_boot)
        for prerequisite in (
            "nm-online -q --timeout=60",
            "systemctl is-active --quiet vms-camera.service",
            'health.get("camera_ready") is True',
            'content_type != "video/h264"',
            "sshd -t",
            "systemctl is-active --quiet ssh.service",
            "systemctl is-active --quiet vms-usb-recovery.service",
            "test -c /dev/ttyGS0",
            "systemctl is-active --quiet serial-getty@ttyGS0.service",
        ):
            with self.subTest(prerequisite=prerequisite):
                self.assertLess(
                    first_boot.index(prerequisite),
                    first_boot.index(ready_marker),
                )

    def test_legacy_runtime_install_retains_cloud_final_bootstrap(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1, camera_board="zero-w"),
            DeterministicKeyGenerator(),
            wifi_country="US",
        )
        camera = job.device_configurations()[1]
        first_boot = decoded_cloud_file(
            camera.user_data, "/usr/local/libexec/vms-first-boot"
        )

        self.assertIn("runcmd:", camera.user_data)
        self.assertIn("bootcmd:", camera.user_data)
        self.assertIn(
            "  - [systemctl, start, vms-first-boot.service]",
            camera.user_data,
        )
        self.assertIn("apt-get install", first_boot)
        self.assertIn("iputils-ping", first_boot)
        network_recovery = decoded_cloud_file(
            camera.user_data, "/opt/vms/network_recovery.py"
        )
        recovery_service = decoded_cloud_file(
            camera.user_data,
            "/etc/systemd/system/vms-network-recovery.service",
        )
        recovery_timer = decoded_cloud_file(
            camera.user_data,
            "/etc/systemd/system/vms-network-recovery.timer",
        )
        self.assertIn("class NetworkManagerAdapter", network_recovery)
        self.assertIn("ReadWritePaths=/var/lib/vms", recovery_service)
        self.assertIn("OnUnitInactiveSec=30s", recovery_timer)
        self.assertIn("/boot/firmware/user-data", first_boot)
        self.assertIn("/var/lib/cloud/instances", first_boot)
        self.assertIn("/var/log/cloud-init.log", first_boot)

    def test_yaml_scalars_cannot_inject_lines(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1), wifi_country="US"
        )
        identity = job.identity
        altered = type(identity)(
            kit_id=identity.kit_id,
            wifi_ssid='name\npassword: "injected"',
            wifi_passphrase='secret\nnext: "injected"',
            wifi_country=identity.wifi_country,
            recorder=identity.recorder,
            cameras=identity.cameras,
        )
        camera_config = render_network_config(altered.cameras[0], altered)
        self.assertEqual(
            camera_config,
            "version: 2\nrenderer: NetworkManager\nwifis: {}\n",
        )
        self.assertNotIn("name\npassword", camera_config)
        self.assertNotIn("secret\nnext", camera_config)

    def test_existing_network_credentials_can_be_injected(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1),
            DeterministicKeyGenerator(),
            wifi_ssid="Development Network",
            wifi_passphrase="correct horse battery staple",
            wifi_country="US",
        )
        self.assertEqual(job.identity.wifi_ssid, "Development Network")
        self.assertEqual(
            job.identity.wifi_passphrase, "correct horse battery staple"
        )
        keyfile = render_networkmanager_connection(
            job.identity.cameras[0], job.identity
        )
        self.assertIn("mode=infrastructure", keyfile)
        self.assertIn("ssid=Development Network", keyfile)
        self.assertIn("psk=correct horse battery staple", keyfile)
        self.assertIn("id=vms-home-001", keyfile)
        self.assertIn(
            f"dhcp-client-id={camera_dhcp_client_id(job.identity.cameras[0])}",
            keyfile,
        )

    def test_dhcp_client_identity_follows_the_card_not_the_board(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=2),
            DeterministicKeyGenerator(),
            wifi_ssid="Development Network",
            wifi_passphrase="correct horse battery staple",
            wifi_country="US",
        )
        camera = job.identity.cameras[0]
        client_id = camera_dhcp_client_id(camera)

        self.assertEqual(
            client_id,
            camera_dhcp_client_id(replace(camera, board="zero-2-w")),
        )
        self.assertNotEqual(client_id, camera_dhcp_client_id(job.identity.cameras[1]))
        self.assertRegex(client_id, r"^00(?::[0-9a-f]{2})+$")
        with self.assertRaisesRegex(ValueError, "valid camera identity"):
            camera_dhcp_client_id(job.identity.recorder)
        with self.assertRaisesRegex(ValueError, "valid camera identity"):
            camera_dhcp_client_id(replace(camera, device_id="camera-001"))

    def test_injected_network_credentials_are_validated(self) -> None:
        spec = ProvisioningSpec(camera_count=1)
        invalid = (
            ("ssid", None),
            (None, "long-enough"),
            ("bad\nssid", "long-enough"),
            ("ssid", "short"),
        )
        for ssid, passphrase in invalid:
            with self.subTest(ssid=ssid, passphrase=passphrase):
                with self.assertRaises(ValueError):
                    ProvisioningJob.create(
                        spec,
                        DeterministicKeyGenerator(),
                        wifi_ssid=ssid,
                        wifi_passphrase=passphrase,
                        wifi_country="US",
                    )

    def test_injected_network_retry_must_match_persisted_job(self) -> None:
        spec = ProvisioningSpec(camera_count=1)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "job.json"
            first = ProvisioningJob.open_or_create(
                state,
                spec,
                DeterministicKeyGenerator(),
                wifi_ssid="Development Network",
                wifi_passphrase="first-secure-passphrase",
                wifi_country="US",
            )
            second = ProvisioningJob.open_or_create(
                state,
                spec,
                wifi_ssid="Development Network",
                wifi_passphrase="first-secure-passphrase",
                wifi_country="US",
            )
            self.assertEqual(first, second)
            with self.assertRaises(ValueError):
                ProvisioningJob.open_or_create(
                    state,
                    spec,
                    wifi_ssid="Development Network",
                    wifi_passphrase="different-secure-passphrase",
                    wifi_country="US",
                )

    def test_network_rotation_is_explicit_and_preserves_device_identities(self) -> None:
        spec = ProvisioningSpec(camera_count=2, camera_board="zero-w")
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "job.json"
            original = ProvisioningJob.open_or_create(
                state,
                spec,
                DeterministicKeyGenerator(),
                wifi_ssid="Pilot-Network-One",
                wifi_passphrase="first-development-passphrase",
                wifi_country="US",
            )
            with self.assertRaisesRegex(ValueError, "different wifi_ssid"):
                ProvisioningJob.open_or_create(
                    state,
                    spec,
                    wifi_ssid="Pilot-Network-Two",
                    wifi_passphrase="second-development-passphrase",
                    wifi_country="US",
                )

            rotated = ProvisioningJob.open_or_create(
                state,
                spec,
                wifi_ssid="Pilot-Network-Two",
                wifi_passphrase="second-development-passphrase",
                wifi_country="US",
                rotate_network=True,
            )

            self.assertEqual(rotated.identity.recorder, original.identity.recorder)
            self.assertEqual(rotated.identity.cameras, original.identity.cameras)
            self.assertEqual(rotated.identity.wifi_ssid, "Pilot-Network-Two")
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["schema_version"],
                3,
            )

    def test_pilot_recovery_is_default_unique_and_not_public(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=2, camera_board="zero-w"),
            DeterministicKeyGenerator(),
            wifi_country="US",
        )
        recovery_values = [
            job.identity.recorder.pilot_recovery_secret,
            *(camera.pilot_recovery_secret for camera in job.identity.cameras),
        ]
        self.assertNotIn(None, recovery_values)
        self.assertEqual(len(set(recovery_values)), 3)
        public = job.public_manifest().to_json()
        camera_config = job.device_configurations()[1]
        for value in recovery_values:
            self.assertNotIn(value, public)
            self.assertNotIn(value, camera_config.user_data)
        self.assertIn(f"name: {PILOT_RECOVERY_USERNAME}", camera_config.user_data)
        self.assertIn("ssh_pwauth: false", camera_config.user_data)
        self.assertIn("lock_passwd: false", camera_config.user_data)
        self.assertIn("interfaces:\n    serial: true", camera_config.user_data)
        self.assertIn(
            pilot_recovery_ssh_authorized_key(job.identity.cameras[0]),
            camera_config.user_data,
        )
        private_ssh_key = pilot_recovery_ssh_private_key(job.identity.cameras[0])
        private_key_header = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
        self.assertTrue(private_ssh_key.startswith(private_key_header))
        loaded_private_key = serialization.load_ssh_private_key(
            private_ssh_key.encode("ascii"), password=None
        )
        retried_private_key = serialization.load_ssh_private_key(
            pilot_recovery_ssh_private_key(job.identity.cameras[0]).encode("ascii"),
            password=None,
        )
        self.assertEqual(
            loaded_private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            retried_private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )
        authorized_key = pilot_recovery_ssh_authorized_key(
            job.identity.cameras[0]
        ).split()
        loaded_public_key = serialization.load_ssh_public_key(
            " ".join(authorized_key[:2]).encode("ascii")
        )
        self.assertEqual(
            loaded_private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
            loaded_public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
        )
        self.assertNotIn(private_ssh_key, camera_config.user_data)
        self.assertIsNotNone(camera_config.boot_customization)
        self.assertIn(
            "enable_uart=1", camera_config.boot_customization.config_txt_append
        )
        self.assertIn(
            "uart_2ndstage=1", camera_config.boot_customization.config_txt_append
        )
        self.assertIn(
            "console=serial0,115200",
            camera_config.boot_customization.cmdline_append,
        )
        self.assertIn("loglevel=7", camera_config.boot_customization.cmdline_append)
        self.assertIn(
            "systemd.show_status=1",
            camera_config.boot_customization.cmdline_append,
        )
        self.assertNotIn(
            "systemd.log_level=debug",
            camera_config.boot_customization.cmdline_append,
        )
        self.assertIn(
            "systemd.log_level=debug",
            camera_config.boot_customization.cmdline_remove,
        )
        self.assertIn("quiet", camera_config.boot_customization.cmdline_remove)
        first_boot = decoded_cloud_file(
            camera_config.user_data, "/usr/local/libexec/vms-first-boot"
        )
        usb_recovery = decoded_cloud_file(
            camera_config.user_data, "/usr/local/libexec/vms-usb-recovery"
        )
        self.assertIn("dtoverlay=dwc2,dr_mode=peripheral", first_boot)
        self.assertIn("serial-getty@ttyGS0.service", usb_recovery)
        self.assertIn(
            'recovery_reboot="$state_dir/pilot-recovery-reboot-requested"',
            first_boot,
        )
        self.assertIn("systemctl --no-block reboot", first_boot)
        self.assertIn("systemctl start vms-usb-recovery.service", first_boot)
        self.assertIn("systemctl restart ssh.service", first_boot)
        self.assertNotIn("systemd.log_level=debug", first_boot)
        self.assertIn("systemd.log_level=*", first_boot)
        self.assertIn("loglevel=7", first_boot)
        self.assertIn("systemd.show_status=1", first_boot)
        self.assertIn("${filtered_cmdline:+$filtered_cmdline }", first_boot)
        self.assertNotIn("${{", first_boot)
        self.assertIn("MaxLevelStore=debug", camera_config.user_data)
        self.assertIn("AllowUsers vms-recovery", camera_config.user_data)
        self.assertIn(
            "path: /etc/ssh/sshd_config.d/00-vms-pilot-recovery.conf",
            camera_config.user_data,
        )
        diagnostic_script = decoded_cloud_file(
            camera_config.user_data, "/usr/local/bin/vms-pilot-diagnostics"
        )
        self.assertIn("journalctl --no-pager", diagnostic_script)
        self.assertNotIn(job.identity.wifi_passphrase, diagnostic_script)
        self.assertEqual(first_boot.count("systemctl --no-block reboot"), 1)
        self.assertLess(
            first_boot.index("boot_config=/boot/firmware/config.txt"),
            first_boot.index("keyfile=/etc/NetworkManager/system-connections/"),
        )
        self.assertLess(
            first_boot.index("systemctl enable vms-usb-recovery.service"),
            first_boot.index("nmcli connection reload"),
        )
        self.assertLess(
            first_boot.index("systemctl enable vms-usb-recovery.service"),
            first_boot.index("apt-get update"),
        )
        self.assertLess(
            first_boot.index("systemctl --no-block reboot"),
            first_boot.index("nmcli connection reload"),
        )

        self.assertNotIn(
            "systemctl enable serial-getty@serial0.service", first_boot
        )
        self.assertIn(
            "systemctl disable --now serial-getty@serial0.service", first_boot
        )

    def test_first_boot_owns_and_validates_recovery_access_before_ready(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1, camera_board="zero-w"),
            DeterministicKeyGenerator(),
            wifi_country="US",
        )
        identity = job.identity.cameras[0]
        user_data = job.device_configurations()[1].user_data
        first_boot = decoded_cloud_file(
            user_data, "/usr/local/libexec/vms-first-boot"
        )

        password = re.search(r'^    passwd: "([^\"]+)"$', user_data, re.MULTILINE)
        password_record = re.search(
            r"password_record=\$\(printf '%s' '([^']+)' \| base64 -d\)",
            first_boot,
        )
        self.assertIsNotNone(password)
        self.assertIsNotNone(password_record)
        decoded_record = base64.b64decode(password_record.group(1)).decode("ascii")
        self.assertEqual(
            decoded_record,
            f"{PILOT_RECOVERY_USERNAME}:{password.group(1)}",
        )
        self.assertNotIn(identity.pilot_recovery_secret, first_boot)

        encoded_key = base64.b64encode(
            (pilot_recovery_ssh_authorized_key(identity) + "\n").encode("ascii")
        ).decode("ascii")
        self.assertEqual(first_boot.count(encoded_key), 2)
        for invariant in (
            'useradd --create-home --home-dir "$recovery_home"',
            'usermod --home "$recovery_home" --shell /bin/bash',
            'printf \'%s\\n\' "$password_record" | chpasswd -e',
            'install -m 0600 -o "$recovery_user"',
            'getent passwd "$recovery_user" | cut -d: -f6',
            'getent shadow "$recovery_user" | cut -d: -f2',
            "stat -c '%u:%g:%a' \"$authorized_keys\"",
            'cmp -s - "$authorized_keys"',
            "sshd -t",
            'sshd -T -C "user=$recovery_user,host=localhost,addr=127.0.0.1"',
            "grep -Fqx 'pubkeyauthentication yes'",
            "grep -Fqx 'passwordauthentication no'",
            "grep -Fqx 'authenticationmethods publickey'",
            "grep -Fqx 'authorizedkeysfile .ssh/authorized_keys'",
            "grep -Fqx 'usepam yes'",
            "grep -Fqx 'allowusers vms-recovery'",
            "systemctl is-active --quiet ssh.service",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, first_boot)

        ready_marker = 'mv -f "$temporary" "$marker"'
        self.assertLess(first_boot.index("sshd -t"), first_boot.index(ready_marker))
        self.assertIn(
            "if ! systemctl is-enabled --quiet ssh.service; then", first_boot
        )
        self.assertIn("timeout 90 systemctl enable ssh.service", first_boot)
        for recovery_check in (
            "timeout 90 systemctl restart ssh.service",
            "systemctl is-active --quiet ssh.service",
            "timeout 30 systemctl start vms-usb-recovery.service",
            "systemctl is-active --quiet vms-usb-recovery.service",
            "test -c /dev/ttyGS0",
            "timeout 30 systemctl start serial-getty@ttyGS0.service",
            "systemctl is-active --quiet serial-getty@ttyGS0.service",
        ):
            with self.subTest(recovery_check=recovery_check):
                self.assertLess(
                    first_boot.index(recovery_check),
                    first_boot.index(ready_marker),
                )

        opted_out = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1, camera_board="zero-w"),
            DeterministicKeyGenerator(),
            wifi_country="US",
            pilot_recovery=False,
        )
        self.assertIsNone(opted_out.identity.recorder.pilot_recovery_secret)
        self.assertIsNone(opted_out.identity.cameras[0].pilot_recovery_secret)
        self.assertIsNone(opted_out.device_configurations()[1].boot_customization)
        self.assertNotIn(
            "vms-usb-recovery",
            opted_out.device_configurations()[1].user_data,
        )
        opted_out_first_boot = decoded_cloud_file(
            opted_out.device_configurations()[1].user_data,
            "/usr/local/libexec/vms-first-boot",
        )
        self.assertNotIn("recovery_user=", opted_out_first_boot)
        self.assertNotIn("sshd -T", opted_out_first_boot)
        self.assertIn(
            "timeout 30 systemctl disable --now ssh.service ssh.socket",
            opted_out_first_boot,
        )
        self.assertLess(
            opted_out_first_boot.index(
                "systemctl is-active --quiet ssh.service"
            ),
            opted_out_first_boot.index('mv -f "$temporary" "$marker"'),
        )

        with self.assertRaisesRegex(ValueError, "pilot-tier"):
            ProvisioningJob.create(
                ProvisioningSpec(camera_count=1, camera_board="zero-2-w"),
                wifi_country="US",
                pilot_recovery=True,
            )

    def test_camera_network_compatibility_is_explicit(self) -> None:
        validate_camera_network_compatibility(
            "zero-w", band="2.4-ghz", security="wpa2-psk"
        )
        with self.assertRaisesRegex(ValueError, "2.4 GHz"):
            validate_camera_network_compatibility(
                "zero-w", band="5-ghz", security="wpa2-psk"
            )
        with self.assertRaisesRegex(ValueError, "WPA2"):
            validate_camera_network_compatibility(
                "zero-w", band="2.4-ghz", security="wpa3-sae"
            )

    def test_wifi_country_is_required_validated_and_retry_stable(self) -> None:
        spec = ProvisioningSpec(camera_count=1)
        for country in (None, "us", "USA", "ZZ", "00"):
            with self.subTest(country=country):
                with self.assertRaises(ValueError):
                    ProvisioningJob.create(spec, wifi_country=country)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "job.json"
            first = ProvisioningJob.open_or_create(
                state,
                spec,
                wifi_country="US",
            )
            self.assertEqual(first.identity.wifi_country, "US")
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["identity"][
                    "wifi_country"
                ],
                "US",
            )
            with self.assertRaisesRegex(ValueError, "different wifi_country"):
                ProvisioningJob.open_or_create(
                    state,
                    spec,
                    wifi_country="CA",
                )

    def test_schema_one_state_upgrades_without_rotating_private_material(self) -> None:
        spec = ProvisioningSpec(camera_count=2)
        original = ProvisioningJob.create(spec, wifi_country="US")
        legacy = json.loads(original.to_private_json())
        legacy["schema_version"] = 1
        del legacy["identity"]["wifi_country"]
        legacy_text = json.dumps(legacy, indent=2, sort_keys=True) + "\n"

        parsed_legacy = ProvisioningJob.from_json(legacy_text)
        self.assertIsNone(parsed_legacy.identity.wifi_country)
        with self.assertRaisesRegex(ValueError, "explicit Wi-Fi country"):
            parsed_legacy.device_configurations()

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "job.json"
            state.write_text(legacy_text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different wifi_ssid"):
                ProvisioningJob.open_or_create(
                    state,
                    spec,
                    wifi_ssid="Different Network",
                    wifi_passphrase="different-secure-passphrase",
                    wifi_country="US",
                )
            self.assertEqual(state.read_text(encoding="utf-8"), legacy_text)

            upgraded = ProvisioningJob.open_or_create(
                state,
                spec,
                wifi_country="US",
            )

            self.assertEqual(upgraded.identity.wifi_country, "US")
            self.assertEqual(upgraded.identity.wifi_passphrase, original.identity.wifi_passphrase)
            self.assertEqual(upgraded.identity.recorder, original.identity.recorder)
            self.assertEqual(upgraded.identity.cameras, original.identity.cameras)
            upgraded_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(upgraded_state["schema_version"], 3)
            self.assertEqual(upgraded_state["identity"]["wifi_country"], "US")

    def test_current_state_without_country_fails_closed(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1), wifi_country="US"
        )
        state = json.loads(job.to_private_json())
        del state["identity"]["wifi_country"]

        with self.assertRaisesRegex(ValueError, "country is missing"):
            ProvisioningJob.from_json(json.dumps(state))

    def test_networkmanager_recorder_uses_ethernet(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1),
            DeterministicKeyGenerator(),
            wifi_country="US",
        )
        keyfile = render_networkmanager_connection(
            job.identity.recorder, job.identity
        )
        self.assertIn("type=ethernet", keyfile)
        self.assertIn("interface-name=eth0", keyfile)
        self.assertNotIn(job.identity.wifi_passphrase, keyfile)

    def test_recorder_uses_wired_lan_without_hosting_camera_wifi(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=2),
            DeterministicKeyGenerator(),
            wifi_country="US",
        )

        recorder = job.device_configurations()[0]
        self.assertIn("hostname: \"osecameras\"", recorder.user_data)
        self.assertEqual(
            recorder.network_config,
            "version: 2\n"
            "renderer: NetworkManager\n"
            "ethernets:\n"
            "  eth0:\n"
            "    dhcp4: true\n"
            "    optional: true\n",
        )
        self.assertNotIn(job.identity.wifi_passphrase, recorder.user_data)
        recorder_first_boot = decoded_cloud_file(
            recorder.user_data, "/usr/local/libexec/vms-first-boot"
        )
        self.assertNotIn("dnsmasq.service", recorder_first_boot)
        for retired_path in (
            "/etc/vms/recorder-ap.conf",
            "/etc/vms/recorder-ap.nft",
            "/etc/systemd/system/vms-recorder-ap.service",
            "/etc/systemd/system/vms-recorder-dhcp.service",
            "/etc/systemd/system/vms-recorder-routing.service",
        ):
            self.assertNotIn(retired_path, recorder.user_data)
        self.assertNotIn("10.42.0.1", recorder.user_data)
        for camera in job.device_configurations()[1:]:
            self.assertIn("vms-home-", camera.user_data)

    def test_open_or_create_reuses_all_persisted_secrets(self) -> None:
        spec = ProvisioningSpec(camera_count=2, camera_board="zero-w")
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "job.json"
            first = ProvisioningJob.open_or_create(state, spec, wifi_country="US")
            second = ProvisioningJob.open_or_create(state, spec, wifi_country="US")
            self.assertEqual(first, second)
            self.assertEqual(first.to_private_json(), state.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_retry_with_different_request_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "job.json"
            ProvisioningJob.open_or_create(
                state,
                ProvisioningSpec(camera_count=1),
                wifi_country="US",
            )
            with self.assertRaises(ValueError):
                ProvisioningJob.open_or_create(
                    state,
                    ProvisioningSpec(camera_count=2),
                    wifi_country="US",
                )

    def test_corrupt_cross_kit_state_is_rejected(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1), wifi_country="US"
        )
        state = json.loads(job.to_private_json())
        state["identity"]["cameras"][0]["kit_id"] = "KIT-OTHER"
        with self.assertRaises(ValueError):
            ProvisioningJob.from_json(json.dumps(state))


class SecretFileTests(unittest.TestCase):
    def test_secret_files_preserve_lf_bytes_on_every_platform(self) -> None:
        content = "first\nsecond\n"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "private-state"
            secure_write_text(destination, content)
            self.assertEqual(destination.read_bytes(), b"first\nsecond\n")

            with temporary_secret_file(content, directory) as path:
                self.assertEqual(path.read_bytes(), b"first\nsecond\n")

    def test_temporary_secret_file_is_private_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with temporary_secret_file("sensitive", directory) as path:
                self.assertEqual(path.read_text(encoding="utf-8"), "sensitive")
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                saved_path = path
            self.assertFalse(saved_path.exists())


class Ed25519Tests(unittest.TestCase):
    @unittest.skipUnless(
        CryptographyEd25519KeyGenerator.is_available(),
        "cryptography package is not installed",
    )
    def test_ed25519_generator_returns_raw_key_material(self) -> None:
        first = CryptographyEd25519KeyGenerator().generate()
        second = CryptographyEd25519KeyGenerator().generate()
        self.assertEqual(first.algorithm, "ed25519")
        self.assertEqual(len(first.public_identity), 43)
        self.assertEqual(len(first.private_identity), 43)
        self.assertNotEqual(first.private_identity, second.private_identity)

    def test_default_prefers_ed25519_when_available(self) -> None:
        job = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1), wifi_country="US"
        )
        expected = (
            "ed25519"
            if CryptographyEd25519KeyGenerator.is_available()
            else "opaque-token-sha256-v1"
        )
        self.assertEqual(job.identity.recorder.key.algorithm, expected)


if __name__ == "__main__":
    unittest.main()
