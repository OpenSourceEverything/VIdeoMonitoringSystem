import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from video_monitoring_system.cli import _alert_worker, build_parser, main
from video_monitoring_system.application import ApplicationError
from video_monitoring_system.backup import BackupSummary
from video_monitoring_system.doctor import DoctorReport
from video_monitoring_system.imaging import DiskTarget
from video_monitoring_system.storage_adapter import RecordingStorageTarget


class FakeApplication:
    def __init__(self):
        self.calls = []
        self.provisioning_requests = []
        self.target = DiskTarget(
            device="card-device",
            stable_id="CARD-123",
            display_name="32 GB removable card",
            size_bytes=32_000_000_000,
            removable=True,
            system_disk=False,
        )
        self.storage_target = RecordingStorageTarget(
            stable_id="STORAGE-123",
            display_name="Recording Drive (1.0 TB)",
            confirmation="INITIALIZE STORAGE-123",
            root=Path("/recording"),
            volume_identity="linux-uuid:volume-1",
        )
        self.remaining = 0

    def require_ose_doctor(self):
        self.calls.append(("doctor", {}))
        return DoctorReport("source", ())

    def doctor(self, *, ose=False):
        self.calls.append(("doctor-report", {"ose": ose}))
        return DoctorReport("source", ())

    def create_owner(self, **values):
        self.calls.append(("create_owner", values))
        return {"result": "configured"}

    def create_gui_server(self, **values):
        self.calls.append(("portal", values))
        return FakeServer()

    def remote_access_status(self):
        self.calls.append(("remote-status", {}))
        return {
            "state": "local-only",
            "local_url": "http://osecameras.local",
            "remote_url": None,
        }

    def setup_private_remote_access(self):
        self.calls.append(("remote-private", {}))
        return {
            "state": "ready",
            "local_url": "http://osecameras.local",
            "remote_url": "https://osecameras.example.ts.net",
        }

    def setup_owner_gateway_access(self, **values):
        self.calls.append(("remote-gateway", values))
        return {
            "state": "ready",
            "local_url": "http://osecameras.local",
            "remote_url": f"https://{values['domain']}",
        }

    def run_owner_gateway_tunnel(self):
        self.calls.append(("remote-tunnel", {}))
        return 0

    def capture_recording_cycle(self, **values):
        self.calls.append(("capture-cycle", values))
        return {"state": "recording", "cameras": []}

    def process_video_engine_alerts(self):
        self.calls.append(("process-alerts", {}))
        return {"state": "ready", "delivered": 0, "failed": 0}

    def reconcile_video_engine_configuration(self, **values):
        self.calls.append(("reconcile-engine", values))
        return {"state": "ready", "changed": True, "cameras": 1}

    def initialize_storage(self, storage):
        self.calls.append(("storage", {"storage": storage}))

    def list_recording_storage_targets(self):
        return (self.storage_target,)

    def initialize_selected_recording_storage(self, **values):
        self.calls.append(("storage", values))

    def adopt_selected_recording_storage(self, **values):
        self.calls.append(("adopt_storage", values))

    def list_cameras(self, *, unclaimed=False):
        self.calls.append(("list", {"unclaimed": unclaimed}))
        return ({"id": "CAM-1", "name": None, "state": "unclaimed"},)

    def claim_camera(self, **values):
        self.calls.append(("claim", values))

    def name_camera(self, **values):
        self.calls.append(("name", values))

    def set_camera_recording(self, **values):
        self.calls.append(("camera-recording", values))

    def replace_camera(self, **values):
        self.calls.append(("replace", values))

    def remove_camera(self, **values):
        self.calls.append(("remove", values))

    def provision_camera(self, **values):
        self.calls.append(("provision", values))
        recovery = values["pilot_recovery"]
        if recovery is None:
            recovery = values["model"] == "zero-w"
        return {
            "result": "flashed" if values["flash"] else "preview",
            "device_id": "CAM-1",
            "pilot_recovery": recovery,
        }

    def prepare_provisioning(self, request, **values):
        self.provisioning_requests.append(request)
        if request.mode == "kit":
            return self.create_kit(
                camera_count=request.camera_count,
                camera_model=request.camera_model,
                wifi_country=request.network.country,
            )
        return self.provision_camera(
            model=request.camera_model,
            number=request.camera_number,
            wifi_ssid=request.network.ssid,
            wifi_passphrase=values["wifi_passphrase"],
            wifi_country=request.network.country,
            flash=values["flash"],
            target=values["target"],
            confirmation=values["confirmation"],
            allow_development=values["allow_development"],
            rotate_network=values["rotate_network"],
            pilot_recovery=values["pilot_recovery"],
        )

    def verify_pilot_camera(self, **values):
        self.calls.append(("verify_pilot", values))
        return {"passed": True, "device_id": "CAM-1", "checks": []}

    def stage_pilot_camera(self, **values):
        self.calls.append(("stage_pilot", values))
        return {"directory": values["output_directory"], "verified": True}

    def migrate_pilot_camera_release(self, **values):
        self.calls.append(("migrate_pilot", values))
        return {"result": "migrated"}

    def pilot_camera_diagnostics(self, **values):
        self.calls.append(("pilot_diagnostics", values))
        return {
            "username": "vms-recovery",
            "private_network_ssh": "camera-001.local",
            "ssh_key_file": "/private/CAM-1.key",
            "credentials_file": "/private/camera-001.json",
            "usb_serial": "USB data port; serial console",
            "gpio_uart": "GPIO pin 8 TX and pin 6 GND; 115200 8-N-1",
            "diagnostic_command": "sudo vms-pilot-diagnostics",
        }

    def set_recording(self, **values):
        self.calls.append(("recording", values))

    def list_recordings(self, **values):
        self.calls.append(("recording-list", values))
        return ({"id": "segment-1", "camera_id": "CAM-1"},)

    def maintain_recordings(self):
        self.calls.append(("recording-maintain", {}))
        return {"state": "ready", "deleted": 2, "reclaimed_bytes": 4096}

    def create_backup(self, **values):
        self.calls.append(("backup-create", values))
        return BackupSummary("2026-08-30T00:00:00+00:00", 3, 12)

    def restore_backup(self, **values):
        self.calls.append(("backup-restore", values))
        return BackupSummary("2026-08-30T00:00:00+00:00", 3, 12)

    def status(self):
        return {"owner_ready": True, "cameras": {"connected": 1}}

    def update(self):
        self.calls.append(("update", {}))

    def create_kit(self, **values):
        self.calls.append(("create_kit", values))
        self.remaining = 0
        return {"kit_id": "KIT-1", "camera_count": values["camera_count"]}

    def kit_flash_status(self):
        return {"remaining": self.remaining}

    def flash_next(self, **values):
        self.calls.append(("flash_next", values))
        self.remaining -= 1
        return {"device_id": "CAM-1"}

    def verify_kit(self):
        self.calls.append(("verify", {}))
        return {"passed": True, "cameras": []}

    def list_media_targets(self):
        self.calls.append(("targets", {}))
        return (self.target,)


class FakeServer:
    server_address = ("127.0.0.1", 9081)

    def __init__(self):
        self.served = False
        self.closed = False

    def serve_forever(self):
        self.served = True

    def server_close(self):
        self.closed = True


class CLITests(unittest.TestCase):
    def test_release_directory_is_a_global_platform_neutral_option(self):
        parsed = build_parser().parse_args(
            ["--release-dir", "/private/releases", "status"]
        )

        self.assertEqual(parsed.release_dir, "/private/releases")

    def run_cli(self, arguments, *, application=None, inputs=(), secrets=()):
        application = application or FakeApplication()
        input_values = iter(inputs)
        secret_values = iter(secrets)
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            arguments,
            application=application,
            input_fn=lambda prompt: next(input_values),
            secret_fn=lambda prompt: next(secret_values),
            stdout=stdout,
            stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue(), application

    def test_no_command_accepts_password_claim_code_or_network_credentials(self):
        help_text = build_parser().format_help().lower()
        for forbidden in ("--password", "--claim-code", "--ssid", "--passphrase"):
            self.assertNotIn(forbidden, help_text)

    def test_factory_gui_requires_explicit_flag(self):
        retail = build_parser().parse_args(["gui"])
        factory = build_parser().parse_args(["gui", "--factory"])
        self.assertFalse(retail.factory)
        self.assertTrue(factory.factory)

    def test_recorder_service_is_a_loopback_portal_without_factory_pages(self):
        parsed = build_parser().parse_args(["recorder", "serve"])
        self.assertEqual(parsed.port, 9081)

        code, stdout, stderr, app = self.run_cli(["recorder", "serve"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            app.calls[0],
            (
                "portal",
                {"port": 9081, "factory": False, "local_network": False},
            ),
        )
        self.assertEqual(stdout, "Open http://127.0.0.1:9081\n")

    def test_remote_setup_is_one_short_interactive_command(self):
        parsed = build_parser().parse_args(["remote", "setup"])
        self.assertEqual(parsed.remote_command, "setup")

        code, stdout, stderr, app = self.run_cli(
            ["remote", "setup"], inputs=("1",)
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(app.calls, [("remote-private", {})])
        self.assertIn("At home: http://osecameras.local", stdout)
        self.assertIn("Away: https://osecameras.example.ts.net", stdout)

    def test_owner_gateway_code_is_private_prompt_input(self):
        code, stdout, stderr, app = self.run_cli(
            ["remote", "setup"],
            inputs=("2", "cameras.example.com"),
            secrets=("one-time-code-123456",),
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            app.calls,
            [
                (
                    "remote-gateway",
                    {
                        "domain": "cameras.example.com",
                        "enrollment_code": "one-time-code-123456",
                    },
                )
            ],
        )
        self.assertNotIn("one-time-code", stdout)

    def test_remote_status_answers_where_to_open_cameras(self):
        code, stdout, stderr, app = self.run_cli(["remote", "status"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(app.calls, [("remote-status", {})])
        self.assertEqual(
            stdout,
            "At home: http://osecameras.local\nAway: not set up\n",
        )

    def test_recorder_worker_runs_one_shared_application_cycle(self):
        code, stdout, stderr, app = self.run_cli(
            ["recorder", "record", "--frames", "2"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(app.calls[0], ("capture-cycle", {"frame_count": 2}))
        self.assertEqual(
            json.loads(stdout), {"state": "recording", "cameras": []}
        )

    def test_alert_worker_does_not_run_capture(self):
        code, stdout, stderr, app = self.run_cli(["recorder", "alerts"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(app.calls, [("process-alerts", {})])
        self.assertEqual(json.loads(stdout)["state"], "ready")
        self.assertEqual(json.loads(stdout)["phone_display"], "not-measured")
        self.assertEqual(json.loads(stdout)["provider_accepted"], 0)
        self.assertNotIn("delivered", json.loads(stdout))

    def test_warm_alert_worker_reloads_owner_settings_between_polls(self):
        first = FakeApplication()
        second = FakeApplication()
        second.process_video_engine_alerts = Mock(return_value={"state": "paused", "delivered": 0})
        reload_application = Mock(return_value=second)
        output = io.StringIO()
        with patch("video_monitoring_system.cli.time.sleep", side_effect=[None, KeyboardInterrupt]):
            result = _alert_worker(
                first, watch=True, reload_application=reload_application, output=output,
            )
        self.assertEqual(result, 0)
        self.assertEqual(first.calls, [("process-alerts", {})])
        reload_application.assert_called_once_with()
        second.process_video_engine_alerts.assert_called_once_with()
        self.assertIn('"state": "paused"', output.getvalue())

    def test_warm_alert_worker_recovers_without_logging_private_failure(self):
        first = FakeApplication()
        first.process_video_engine_alerts = Mock(side_effect=ApplicationError("private failure detail"))
        recovered = FakeApplication()
        output = io.StringIO()
        with patch("video_monitoring_system.cli.time.sleep", side_effect=[None, KeyboardInterrupt]):
            _alert_worker(
                first, watch=True, reload_application=lambda: recovered, output=output,
            )
        self.assertIn('"state": "needs-attention"', output.getvalue())
        self.assertIn('"state": "ready"', output.getvalue())
        self.assertNotIn("private failure detail", output.getvalue())

    def test_warm_alert_worker_does_not_log_every_unchanged_poll(self):
        app = FakeApplication()
        output = io.StringIO()
        with patch("video_monitoring_system.cli.time.sleep", side_effect=[None, KeyboardInterrupt]):
            _alert_worker(app, watch=True, reload_application=lambda: app, output=output)
        self.assertEqual(output.getvalue().count('"state"'), 1)

    def test_recording_list_uses_the_shared_application_catalog(self):
        code, stdout, stderr, app = self.run_cli(
            ["recording", "list", "--limit", "12"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(app.calls[0], ("recording-list", {"limit": 12}))
        self.assertEqual(json.loads(stdout)[0]["id"], "segment-1")

    def test_recording_maintenance_uses_the_saved_retention_policy(self):
        code, stdout, stderr, app = self.run_cli(["recording", "maintain"])

        self.assertEqual(code, 0, stderr)
        self.assertEqual(app.calls[0], ("recording-maintain", {}))
        self.assertEqual(json.loads(stdout)["deleted"], 2)

    def test_backup_password_is_prompted_and_never_printed(self):
        password = "test-placeholder-owner-password"
        code, stdout, stderr, app = self.run_cli(
            ["backup", "create", "/safe/recorder.vms-backup"],
            secrets=(password,),
        )

        self.assertEqual(code, 0, stderr)
        self.assertNotIn(password, stdout + stderr)
        self.assertEqual(
            app.calls[0],
            (
                "backup-create",
                {
                    "destination": "/safe/recorder.vms-backup",
                    "password": password,
                },
            ),
        )
        self.assertIn("video not included", stdout)

    def test_backup_restore_dispatches_to_the_selected_empty_data_directory(self):
        password = "test-placeholder-owner-password"
        code, stdout, stderr, app = self.run_cli(
            ["backup", "restore", "/safe/recorder.vms-backup"],
            secrets=(password,),
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            app.calls[0],
            (
                "backup-restore",
                {"source": "/safe/recorder.vms-backup", "password": password},
            ),
        )
        self.assertIn("Backup restored", stdout)

    def test_setup_collects_password_only_from_private_prompt(self):
        password = "test-placeholder-owner-password"
        code, stdout, stderr, app = self.run_cli(
            ["setup", "--name", "Owner"], secrets=(password, password)
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn(password, stdout)
        self.assertEqual(app.calls[0][0], "create_owner")
        self.assertEqual(app.calls[0][1]["password"], password)

    def test_claim_code_is_prompted_and_never_printed(self):
        claim_code = "private-claim-code"
        code, stdout, _, app = self.run_cli(
            ["camera", "claim", "CAM-1", "--name", "Driveway"],
            secrets=(claim_code,),
        )
        self.assertEqual(code, 0)
        self.assertNotIn(claim_code, stdout)
        self.assertEqual(app.calls[0][1]["claim_code"], claim_code)

    def test_zero_w_provision_runs_doctor_before_network_prompts_and_dry_runs(self):
        app = FakeApplication()
        secrets = iter(("private ssid", "private passphrase", "private passphrase"))

        def private_prompt(prompt):
            self.assertEqual(app.calls[0][0], "doctor")
            return next(secrets)

        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            [
                "camera",
                "provision",
                "--model",
                "zero-w",
                "--number",
                "2",
                "--country",
                "US",
            ],
            application=app,
            input_fn=lambda prompt: "",
            secret_fn=private_prompt,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(code, 0, stderr.getvalue())
        provision = next(item for item in app.calls if item[0] == "provision")
        self.assertFalse(provision[1]["flash"])
        self.assertEqual(provision[1]["number"], 2)
        self.assertEqual(provision[1]["wifi_country"], "US")
        self.assertIsNone(provision[1]["pilot_recovery"])
        self.assertIn("Pilot recovery is enabled", stdout.getvalue())
        self.assertNotIn("private ssid", stdout.getvalue())
        self.assertNotIn("private passphrase", stdout.getvalue())

    def test_country_can_confirm_an_explicit_nonsecret_policy_default(self):
        app = FakeApplication()
        app.wifi_country_default = "US"
        code, _, stderr, app = self.run_cli(
            ["camera", "provision", "--model", "zero-w", "--number", "1"],
            application=app,
            inputs=("",),
            secrets=("private ssid", "private passphrase", "private passphrase"),
        )

        self.assertEqual(code, 0, stderr)
        provision = next(item for item in app.calls if item[0] == "provision")
        self.assertEqual(provision[1]["wifi_country"], "US")

    def test_invalid_country_fails_before_network_secret_prompts(self):
        app = FakeApplication()
        code, _, stderr, app = self.run_cli(
            [
                "camera",
                "provision",
                "--model",
                "zero-w",
                "--number",
                "1",
                "--country",
                "us",
            ],
            application=app,
        )

        self.assertEqual(code, 2)
        self.assertIn("uppercase", stderr)
        self.assertNotIn("provision", [item[0] for item in app.calls])

    def test_flash_requires_selection_and_exact_confirmation(self):
        app = FakeApplication()
        code, stdout, stderr, app = self.run_cli(
            [
                "camera",
                "provision",
                "--model",
                "zero-w",
                "--number",
                "1",
                "--country",
                "US",
                "--flash",
            ],
            application=app,
            inputs=("1", app.target.confirmation),
            secrets=("private ssid", "private passphrase", "private passphrase"),
        )
        self.assertEqual(code, 0, stderr)
        provision = next(item for item in app.calls if item[0] == "provision")
        self.assertTrue(provision[1]["flash"])
        self.assertEqual(provision[1]["target"], app.target)
        self.assertEqual(provision[1]["confirmation"], app.target.confirmation)
        self.assertIn("was flashed and verified", stdout)

    def test_wrong_erase_confirmation_never_calls_provision(self):
        app = FakeApplication()
        code, _, stderr, app = self.run_cli(
            [
                "camera",
                "provision",
                "--model",
                "zero-w",
                "--number",
                "1",
                "--country",
                "US",
                "--flash",
            ],
            application=app,
            inputs=("1", "wrong"),
            secrets=("private ssid", "private passphrase", "private passphrase"),
        )
        self.assertEqual(code, 2)
        self.assertIn("not confirmed", stderr)
        self.assertNotIn("provision", [item[0] for item in app.calls])

    def test_json_camera_config_contains_only_non_secret_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "camera.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "camera",
                        "camera_model": "zero-w",
                        "camera_number": 4,
                        "network": {
                            "country": "US",
                            "credential_source": "prompt",
                        },
                    }
                ),
                encoding="utf-8",
            )
            code, _, stderr, app = self.run_cli(
                ["camera", "provision", "--config", str(config)],
                secrets=("private ssid", "private passphrase", "private passphrase"),
            )
            self.assertEqual(code, 0, stderr)
            provision = next(item for item in app.calls if item[0] == "provision")
            self.assertEqual(provision[1]["model"], "zero-w")
            self.assertEqual(provision[1]["number"], 4)

    def test_unified_and_legacy_commands_build_the_same_setup_request(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "camera.yaml"
            config.write_text(
                """schema_version: 1
mode: camera
camera_model: zero-w
camera_number: 2
network:
  country: US
  credential_source: prompt
""",
                encoding="utf-8",
            )
            secrets = (
                "private ssid",
                "private passphrase",
                "private passphrase",
            )

            unified = self.run_cli(
                ["provision", "--config", str(config)], secrets=secrets
            )
            legacy = self.run_cli(
                ["camera", "provision", "--config", str(config)],
                secrets=secrets,
            )

        self.assertEqual(unified[0], 0, unified[2])
        self.assertEqual(legacy[0], 0, legacy[2])
        self.assertEqual(
            unified[3].provisioning_requests,
            legacy[3].provisioning_requests,
        )

    def test_json_config_rejects_embedded_network_password(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = "test-placeholder-private-value"
            config = Path(directory) / "camera.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "camera",
                        "camera_model": "zero-w",
                        "camera_number": 1,
                        "network": {
                            "country": "US",
                            "credential_source": "prompt",
                        },
                        "password": secret,
                    }
                ),
                encoding="utf-8",
            )
            code, stdout, stderr, app = self.run_cli(
                ["camera", "provision", "--config", str(config)]
            )
            self.assertEqual(code, 2)
            self.assertNotIn(secret, stdout + stderr)
            self.assertNotIn("provision", [item[0] for item in app.calls])

    def test_requested_command_shapes_dispatch(self):
        cases = (
            (["storage", "init"], "storage"),
            (["storage", "adopt"], "adopt_storage"),
            (["camera", "list", "--unclaimed"], "list"),
            (["camera", "name", "CAM-1", "--name", "Driveway"], "name"),
            (
                ["camera", "recording", "CAM-1", "--no-enabled"],
                "camera-recording",
            ),
            (
                [
                    "recorder",
                    "reconcile-engine",
                    "--state-dir",
                    "/private/frigate",
                    "--candidate",
                    "192.168.0.108:8555",
                ],
                "reconcile-engine",
            ),
            (
                [
                    "camera",
                    "verify",
                    "--model",
                    "zero-w",
                    "--number",
                    "1",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "19080",
                ],
                "verify_pilot",
            ),
            (
                [
                    "camera",
                    "stage",
                    "--model",
                    "zero-w",
                    "--number",
                    "1",
                    "--output",
                    "/private/camera-001",
                ],
                "stage_pilot",
            ),
            (
                [
                    "camera",
                    "migrate-release",
                    "--model",
                    "zero-w",
                    "--number",
                    "1",
                ],
                "migrate_pilot",
            ),
            (["camera", "diagnostics", "--model", "zero-w", "--number", "1"], "pilot_diagnostics"),
            (["recording", "set", "--mode", "continuous", "--days", "21"], "recording"),
            (["update"], "update"),
            (
                ["kit", "provision", "--cameras", "4", "--country", "US"],
                "create_kit",
            ),
            (["kit", "verify"], "verify"),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                application = FakeApplication()
                inputs = (
                    (
                        application.storage_target.adoption_confirmation
                        if expected == "adopt_storage"
                        else application.storage_target.confirmation,
                    )
                    if expected in {"storage", "adopt_storage"}
                    else ()
                )
                code, _, stderr, app = self.run_cli(
                    arguments, application=application, inputs=inputs
                )
                self.assertEqual(code, 0, stderr)
                self.assertIn(expected, [item[0] for item in app.calls])
                if expected == "verify_pilot":
                    values = next(
                        item[1] for item in app.calls if item[0] == "verify_pilot"
                    )
                    self.assertEqual(values["hostname"], "127.0.0.1")
                    self.assertEqual(values["port"], 19080)
                if expected == "migrate_pilot":
                    values = next(
                        item[1] for item in app.calls if item[0] == "migrate_pilot"
                    )
                    self.assertEqual(
                        values["image_reference"],
                        "raspios-32-vms-camera-preinstalled",
                    )

    def test_engine_reconcile_can_signal_a_changed_runtime(self):
        code, stdout, stderr, app = self.run_cli(
            [
                "recorder",
                "reconcile-engine",
                "--state-dir",
                "/private/frigate",
                "--candidate",
                "192.168.0.108:8555",
                "--changed-exit-code",
            ]
        )

        self.assertEqual(code, 10)
        self.assertEqual(stderr, "")
        self.assertIn('"changed": true', stdout)
        self.assertEqual(app.calls[-1][0], "reconcile-engine")


if __name__ == "__main__":
    unittest.main()
