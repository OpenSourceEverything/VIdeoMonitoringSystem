import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "platforms" / "linux" / "recorder"
PI_RECORDER_DIR = ROOT / "platforms" / "raspberry-pi" / "recorder-pilot"


class LinuxRecorderServiceTests(unittest.TestCase):
    def test_actual_reconcile_script_recovers_stopped_engine_without_configuration_change(self):
        asset = ROOT / "adapters/frigate/pilot/vms-frigate-reconcile"
        with tempfile.TemporaryDirectory() as directory:
            script = asset.read_text().replace(
                "/run/lock/vms-frigate-reconcile.lock", str(Path(directory) / "lock")
            ).replace(
                "/usr/local/libexec/vms-recording-recover", "storage_check"
            ).replace("/usr/local/libexec/vms-frigate-render", "render")
            for storage, rendered, status, calls in (
                (0, 0, 0, ["start vms-frigate.service"]),
                (0, 10, 0, ["restart vms-frigate.service", "restart vms-recorder.service"]),
                (1, 0, 1, ["stop vms-frigate.service"]),
                (0, 2, 2, []),
            ):
                with self.subTest(storage=storage, rendered=rendered):
                    helpers = (
                        'id() { printf "0\\n"; }\n'
                        'flock() { :; }\n'
                        f'storage_check() {{ return {storage}; }}\n'
                        f'render() {{ return {rendered}; }}\n'
                        'systemctl() { printf "%s\\n" "$*"; }\n'
                    )
                    result = subprocess.run(
                        ["/bin/sh"], input=helpers + script,
                        text=True, capture_output=True, timeout=5,
                    )
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), calls)

    def test_pi_recorder_pilot_pins_and_verifies_the_base_image(self):
        manifest = json.loads(
            (PI_RECORDER_DIR / "base-image.json").read_text(encoding="utf-8")
        )
        helper = (PI_RECORDER_DIR / "prepare-card").read_text(encoding="utf-8")

        self.assertEqual(manifest["role"], "recorder")
        self.assertEqual(manifest["supported_boards"], ["pi-4-b"])
        self.assertEqual(len(manifest["sha256"]), 64)
        self.assertEqual(len(manifest["raw_sha256"]), 64)
        self.assertIn("sha256sum --check", helper)
        self.assertIn('rpi-imager --cli --sha256 "$raw_sha256"', helper)
        self.assertIn('"ssh_pwauth": False', helper)
        self.assertIn('"ssh_authorized_keys": [key]', helper)
        self.assertIn("ssh-add -L", helper)
        self.assertNotIn("/dev/mmcblk0", helper)

    def test_service_runs_the_product_portal_as_an_unprivileged_user(self):
        unit = (SERVICE_DIR / "vms-recorder.service").read_text(encoding="utf-8")

        self.assertIn("User=vms", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WorkingDirectory=/opt/video-monitoring-system", unit)
        self.assertNotIn("WorkingDirectory=/var/lib/video-monitoring-system", unit)
        self.assertIn("recorder serve --port 80 --local-network", unit)
        self.assertIn(
            "Environment=VMS_RECORDER_DEVICE_CONFIG=/etc/vms/device.json", unit
        )
        self.assertIn("Environment=VMS_TAILSCALE_SUDO=1", unit)
        self.assertIn("NoNewPrivileges=false", unit)
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", unit)
        self.assertNotIn("motioneye", unit.lower())
        self.assertNotIn("Requires=mnt-VMS_RECORDING.mount", unit)

        worker = (SERVICE_DIR / "vms-recorder-capture.service").read_text(
            encoding="utf-8"
        )
        timer = (SERVICE_DIR / "vms-recorder-capture.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("User=vms", worker)
        self.assertIn("WorkingDirectory=/opt/video-monitoring-system", worker)
        self.assertNotIn("WorkingDirectory=/var/lib/video-monitoring-system", worker)
        self.assertIn("recorder record --frames 100", worker)
        self.assertIn("Type=oneshot", worker)
        self.assertIn("TimeoutStartSec=45", worker)
        self.assertNotIn("Restart=", worker)
        self.assertNotIn("motioneye", worker.lower())
        self.assertIn("OnUnitInactiveSec=2", timer)
        self.assertIn("Unit=vms-recorder-capture.service", timer)

        alerts = (SERVICE_DIR / "vms-recorder-alerts.service").read_text()
        self.assertIn("User=vms", alerts)
        self.assertIn("recorder alerts", alerts)
        self.assertIn("Type=simple", alerts)
        self.assertIn("recorder alerts --watch", alerts)
        self.assertIn("Restart=on-failure", alerts)
        self.assertIn("NoNewPrivileges=true", alerts)
        self.assertNotIn("vms-recorder-capture", alerts)
        self.assertNotIn("mnt-VMS_RECORDING", alerts)
        self.assertIn("WantedBy=multi-user.target", alerts)

        maintenance = (SERVICE_DIR / "vms-recorder-maintenance.service").read_text(
            encoding="utf-8"
        )
        maintenance_timer = (
            SERVICE_DIR / "vms-recorder-maintenance.timer"
        ).read_text(encoding="utf-8")
        self.assertIn("User=vms", maintenance)
        self.assertIn("recording maintain", maintenance)
        self.assertIn("Type=oneshot", maintenance)
        self.assertIn("OnCalendar=daily", maintenance_timer)
        self.assertIn("Persistent=true", maintenance_timer)
        self.assertIn("RandomizedDelaySec=30m", maintenance_timer)

    def test_installer_uses_the_tracked_service_contract(self):
        installer = (SERVICE_DIR / "install-recorder").read_text(encoding="utf-8")

        self.assertIn("python3 -m venv", installer)
        self.assertIn("VMS_INSTALL_MODE", installer)
        self.assertIn("--no-index --find-links=", installer)
        self.assertIn("Recorder runtime installed for image provisioning.", installer)
        self.assertIn("vms-recording-ready.path vms-recorder.service", installer)
        self.assertIn("vms-recorder-capture.timer", installer)
        self.assertIn("vms-recorder-alerts.service", installer)
        self.assertIn("vms-recorder-maintenance.timer", installer)
        self.assertIn("vms-remote-gateway.path", installer)
        self.assertIn("/usr/local/bin/vms", installer)
        self.assertIn("hostnamectl set-hostname osecameras", installer)
        self.assertIn("is-active --quiet mnt-VMS_RECORDING.mount", installer)
        self.assertIn("restart vms-recording-ready.service", installer)
        self.assertNotIn("curl ", installer)
        self.assertNotIn("wget ", installer)

        appliance_command = (SERVICE_DIR / "vms-appliance-command").read_text(
            encoding="utf-8"
        )
        self.assertIn("VMS_TAILSCALE_SUDO=1", appliance_command)
        self.assertIn("runuser -u vms", appliance_command)
        self.assertNotIn("chown -R", appliance_command)

        sudoers = (SERVICE_DIR / "vms-tailscale.sudoers").read_text(
            encoding="utf-8"
        )
        self.assertIn("/usr/bin/tailscale status --json", sudoers)
        self.assertIn("/usr/bin/tailscale funnel reset", sudoers)
        self.assertIn("/usr/bin/tailscale serve reset", sudoers)
        self.assertNotIn("/usr/bin/tailscale *", sudoers)

    def test_owner_gateway_tunnel_reconnects_without_a_human_shell(self):
        watcher = (SERVICE_DIR / "vms-remote-gateway.path").read_text(
            encoding="utf-8"
        )
        service = (SERVICE_DIR / "vms-remote-gateway.service").read_text(
            encoding="utf-8"
        )

        self.assertIn("PathExists=/var/lib/video-monitoring-system/gateway-tunnel.json", watcher)
        self.assertIn("remote tunnel", service)
        self.assertIn("User=vms", service)
        self.assertIn("Restart=always", service)

    def test_private_frigate_and_go2rtc_apis_stay_loopback(self):
        unit = (
            ROOT / "adapters" / "frigate" / "pilot" / "vms-frigate-pilot.service"
        ).read_text(encoding="utf-8")
        config = (
            ROOT / "adapters" / "frigate" / "pilot" / "config.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("-p 127.0.0.1:15000:5000", unit)
        self.assertIn("-p 127.0.0.1:15001:1984", unit)
        self.assertIn("-p 8555:8555/tcp", unit)
        self.assertIn("-p 8555:8555/udp", unit)
        self.assertIn("--log-opt max-size=10m --log-opt max-file=3", unit)
        self.assertIn("FRIGATE_WEBRTC_LAN_CANDIDATE", config)
        self.assertIn("FRIGATE_WEBRTC_TAILSCALE_CANDIDATE", config)

        managed = (
            ROOT / "adapters" / "frigate" / "pilot" / "vms-frigate.service"
        ).read_text(encoding="utf-8")
        renderer = (
            ROOT / "adapters" / "frigate" / "pilot" / "vms-frigate-render"
        ).read_text(encoding="utf-8")
        watcher = (
            ROOT
            / "adapters"
            / "frigate"
            / "pilot"
            / "vms-frigate-reconcile.path"
        ).read_text(encoding="utf-8")
        reconcile = (
            ROOT / "adapters" / "frigate" / "pilot" / "vms-frigate-reconcile"
        ).read_text(encoding="utf-8")
        timer = (
            ROOT
            / "adapters"
            / "frigate"
            / "pilot"
            / "vms-frigate-reconcile.timer"
        ).read_text(encoding="utf-8")
        installer = (
            ROOT
            / "adapters"
            / "frigate"
            / "pilot"
            / "install-managed-adapter"
        ).read_text(encoding="utf-8")
        relay = (
            ROOT
            / "adapters"
            / "frigate"
            / "pilot"
            / "vms-camera-route-relay.service"
        ).read_text(encoding="utf-8")
        relay_launcher = (
            ROOT
            / "adapters"
            / "frigate"
            / "pilot"
            / "vms-camera-route-relay"
        ).read_text(encoding="utf-8")
        self.assertIn("-p 127.0.0.1:15000:5000", managed)
        self.assertIn("-p 127.0.0.1:15001:1984", managed)
        self.assertIn("-p 8555:8555/tcp", managed)
        self.assertIn("-p 8555:8555/udp", managed)
        self.assertIn("--log-opt max-size=10m --log-opt max-file=3", managed)
        self.assertIn("--network vms-frigate-private", managed)
        self.assertIn("--add-host=ose-camera-route-relay.internal:${VMS_CAMERA_RELAY_BIND}", managed)
        self.assertIn("EnvironmentFile=/etc/vms/frigate-hardware", managed)
        self.assertIn("$VMS_FRIGATE_HARDWARE_ARGUMENT", managed)
        self.assertIn("Requires=docker.service", relay)
        self.assertIn("Before=vms-frigate.service", relay)
        self.assertIn("ProtectSystem=strict", relay)
        self.assertIn("User=vms", relay)
        self.assertIn("/etc/vms/frigate-route-relay", relay_launcher)
        self.assertIn("recorder route-relay", relay_launcher)
        self.assertIn("--allowed-client-network", relay_launcher)
        self.assertIn("ExecStartPre=/usr/local/libexec/vms-frigate-render", managed)
        self.assertIn("ExecStartPre=/usr/local/libexec/vms-recording-recover --check-only", managed)
        self.assertIn("recorder reconcile-engine", renderer)
        self.assertIn("--mjpeg-hardware v4l2m2m", renderer)
        self.assertIn("runuser -u vms", renderer)
        self.assertIn('stat -c %u "$adapter_environment"', renderer)
        self.assertIn('stat -c %a "$adapter_environment"', renderer)
        self.assertIn("PathChanged=/var/lib/video-monitoring-system/video-engine.json", watcher)
        self.assertIn("PathChanged=/var/lib/video-monitoring-system/imported-camera-sources.json", watcher)
        self.assertIn("PathChanged=/var/lib/video-monitoring-system/camera-endpoints.json", watcher)
        self.assertIn("PathChanged=/var/lib/video-monitoring-system/video-engine-policy.json", watcher)
        self.assertIn("PathChanged=/var/lib/video-monitoring-system/recorder-state.json", watcher)
        self.assertIn("flock 9", reconcile)
        self.assertIn("vms-frigate-render --signal-change", reconcile)
        self.assertIn("/usr/local/libexec/vms-recording-recover", reconcile)
        self.assertIn("systemctl start vms-frigate.service", reconcile)
        self.assertIn("10)", reconcile)
        self.assertIn("systemctl restart vms-frigate.service", reconcile)
        self.assertIn("OnUnitInactiveSec=2min", timer)
        self.assertIn("vms-frigate-reconcile.timer", installer)
        self.assertIn("vms-camera-route-relay.service", installer)
        self.assertIn("docker network create", installer)
        self.assertIn("VMS_CAMERA_RELAY_CLIENT_NETWORK", installer)
        self.assertNotIn("bcm2835-codec-encode", installer)
        self.assertNotIn("VMS_FRIGATE_MJPEG_HARDWARE=v4l2m2m", installer)
        self.assertIn("VMS_FRIGATE_MJPEG_HARDWARE=", installer)
        self.assertIn('stat -c %a /etc/vms/frigate-hardware', installer)
        self.assertIn("VMS_FRIGATE_HARDWARE_ARGUMENT", installer)
        self.assertIn("SuccessExitStatus=143", managed)
        self.assertIn("docker image inspect", installer)
        self.assertNotIn("docker pull", installer)
        self.assertNotIn("curl ", installer)
        self.assertNotIn("wget ", installer)

    def test_labeled_ext4_drive_is_mounted_with_restricted_options(self):
        mount = (SERVICE_DIR / "mnt-VMS_RECORDING.mount").read_text(
            encoding="utf-8"
        )

        self.assertIn("What=/dev/disk/by-label/VMS_RECORDING", mount)
        self.assertIn("Where=/mnt/VMS_RECORDING", mount)
        self.assertIn("Type=ext4", mount)
        self.assertIn("Options=rw,nosuid,nodev,noexec,noatime", mount)
        self.assertIn(r"BindsTo=dev-disk-by\x2dlabel-VMS_RECORDING.device", mount)

    def test_drive_insertion_prepares_only_the_mounted_root(self):
        watcher = (SERVICE_DIR / "vms-recording-ready.path").read_text(
            encoding="utf-8"
        )
        ready = (SERVICE_DIR / "vms-recording-ready.service").read_text(
            encoding="utf-8"
        )

        self.assertIn("PathExists=/dev/disk/by-label/VMS_RECORDING", watcher)
        self.assertIn("Unit=vms-recording-ready.service", watcher)
        self.assertIn("BindsTo=mnt-VMS_RECORDING.mount", ready)
        self.assertIn("chown vms:vms /mnt/VMS_RECORDING", ready)
        self.assertNotIn("chown -R", ready)
        self.assertNotIn("mkfs", ready)


if __name__ == "__main__":
    unittest.main()
