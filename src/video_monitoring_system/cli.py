"""Platform-neutral command-line interface for Video Monitoring System.

Secret values are accepted only through private interactive prompts.  No
password, claim code, network name, or network passphrase is represented by a
command-line argument or written to normal output.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import getpass
import json
import sys
import time
from typing import TextIO

from .application import ApplicationError, VMSApplication
from .configuration import (
    ConfigurationError,
    build_provisioning_config,
    load_provisioning_config,
)
from .doctor import DoctorReport
from .imaging import DiskTarget, ImagingError
from .model import SOFTWARE_CAMERA_LIMIT
from .provisioning import PREINSTALLED_CAMERA_IMAGE_REFERENCE
from .recorder import RecorderError, RecorderStateError
from .regulatory import validate_wifi_country
from .remote_access import LOCAL_PORTAL_URL, RemoteAccessError
from .storage_adapter import RecordingStorageTarget


class CLIInputError(ApplicationError):
    """Interactive input was missing or did not pass confirmation."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vms", description="Set up and manage a local camera system."
    )
    parser.add_argument(
        "--data-dir",
        metavar="DIRECTORY",
        help="private application data directory",
    )
    parser.add_argument(
        "--policy",
        metavar="FILE",
        help="external OSE safety policy",
    )
    parser.add_argument(
        "--release-dir",
        metavar="DIRECTORY",
        help="directory containing verified image release manifests",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    provision = commands.add_parser(
        "provision", help="prepare a complete kit or one camera"
    )
    provision.add_argument(
        "--config",
        metavar="FILE",
        help="read non-secret setup choices from YAML or JSON",
    )
    provision.add_argument(
        "--mode", choices=("kit", "camera"), help="prepare a complete kit or one camera"
    )
    provision.add_argument(
        "--model", choices=("zero-w", "zero-2-w"), help="camera board model"
    )
    provision.add_argument("--cameras", type=int, help="number of cameras in a kit")
    provision.add_argument("--number", type=int, help="one camera's kit number")
    provision.add_argument(
        "--country", metavar="CODE", help="uppercase two-letter Wi-Fi country code"
    )
    provision.add_argument(
        "--flash", action="store_true", help="erase and write selected removable cards"
    )
    provision.add_argument(
        "--allow-development",
        action="store_true",
        help="allow an explicitly selected development image",
    )
    provision.add_argument(
        "--rotate-network",
        action="store_true",
        help="replace saved pilot network credentials",
    )
    provision.add_argument(
        "--pilot-recovery",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override the temporary pilot recovery-access default",
    )

    setup = commands.add_parser("setup", help="create the recorder owner")
    setup.add_argument("--name", help="owner display name")

    storage = commands.add_parser("storage", help="manage recording storage")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_commands.add_parser("init", help="prepare empty recording storage")
    storage_commands.add_parser(
        "adopt", help="use prepared storage without deleting existing files"
    )

    camera = commands.add_parser("camera", help="manage cameras")
    camera_commands = camera.add_subparsers(dest="camera_command", required=True)
    camera_list = camera_commands.add_parser("list", help="list cameras")
    camera_list.add_argument("--unclaimed", action="store_true")
    camera_claim = camera_commands.add_parser("claim", help="claim a camera")
    camera_claim.add_argument("camera_id")
    camera_claim.add_argument("--name", required=True)
    camera_name = camera_commands.add_parser("name", help="name an included camera")
    camera_name.add_argument("camera_id")
    camera_name.add_argument("--name", required=True)
    camera_recording = camera_commands.add_parser(
        "recording", help="enable or pause recording for one camera"
    )
    camera_recording.add_argument("camera_id")
    camera_recording.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    camera_replace = camera_commands.add_parser("replace", help="replace a camera")
    camera_replace.add_argument("camera_name")
    camera_replace.add_argument("replacement_id")
    camera_remove = camera_commands.add_parser("remove", help="remove a camera")
    camera_remove.add_argument("camera_name")
    camera_verify = camera_commands.add_parser(
        "verify", help="check one provisioned pilot camera"
    )
    camera_verify.add_argument("--model", choices=("zero-w", "zero-2-w"))
    camera_verify.add_argument("--number", type=int)
    camera_verify.add_argument("--config", metavar="CONFIG_FILE")
    camera_verify.add_argument(
        "--host", help="camera host or a local endpoint forwarded to the camera"
    )
    camera_verify.add_argument("--port", type=int, default=9080)
    camera_stage = camera_commands.add_parser(
        "stage", help="prepare private configuration for an external imager"
    )
    camera_stage.add_argument("--model", choices=("zero-w", "zero-2-w"))
    camera_stage.add_argument("--number", type=int)
    camera_stage.add_argument("--config", metavar="CONFIG_FILE")
    camera_stage.add_argument("--output", required=True, metavar="DIRECTORY")
    camera_migrate = camera_commands.add_parser(
        "migrate-release", help="move saved pilot identity to a selected image release"
    )
    camera_migrate.add_argument("--model", choices=("zero-w", "zero-2-w"))
    camera_migrate.add_argument("--number", type=int)
    camera_migrate.add_argument("--config", metavar="CONFIG_FILE")
    camera_migrate.add_argument(
        "--release", default=PREINSTALLED_CAMERA_IMAGE_REFERENCE
    )
    camera_diagnostics = camera_commands.add_parser(
        "diagnostics", help="show pilot recovery connections and private file locations"
    )
    camera_diagnostics.add_argument("--model", choices=("zero-w", "zero-2-w"))
    camera_diagnostics.add_argument("--number", type=int)
    camera_provision = camera_commands.add_parser(
        "provision", help="prepare one camera for a pilot"
    )
    camera_provision.add_argument(
        "--model", choices=("zero-w", "zero-2-w")
    )
    camera_provision.add_argument("--number", type=int)
    camera_provision.add_argument("--config", metavar="CONFIG_FILE")
    camera_provision.add_argument(
        "--country",
        metavar="CODE",
        help="uppercase two-letter Wi-Fi regulatory country code",
    )
    camera_provision.add_argument(
        "--flash",
        action="store_true",
        help="erase and write the selected removable card",
    )
    camera_provision.add_argument(
        "--allow-development",
        action="store_true",
        help="allow an explicitly selected development image",
    )
    camera_provision.add_argument(
        "--rotate-network",
        action="store_true",
        help="explicitly replace saved pilot network credentials",
    )
    camera_provision.add_argument(
        "--pilot-recovery",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override the temporary per-device recovery default for pilot hardware",
    )

    recording = commands.add_parser("recording", help="manage recording")
    recording_commands = recording.add_subparsers(
        dest="recording_command", required=True
    )
    recording_set = recording_commands.add_parser("set", help="set recording policy")
    recording_set.add_argument("--mode", choices=("continuous",), required=True)
    recording_set.add_argument("--days", type=int, required=True)
    recording_list = recording_commands.add_parser(
        "list", help="list recent recordings"
    )
    recording_list.add_argument("--limit", type=int, default=100)
    recording_commands.add_parser(
        "maintain", help="apply the saved recording-retention period"
    )

    recorder = commands.add_parser("recorder", help="run the recorder service")
    recorder_commands = recorder.add_subparsers(
        dest="recorder_command", required=True
    )
    recorder_serve = recorder_commands.add_parser(
        "serve", help="serve the local recorder portal"
    )
    recorder_serve.add_argument("--port", type=int, default=9081)
    recorder_serve.add_argument(
        "--local-network", action="store_true", help=argparse.SUPPRESS
    )
    recorder_record = recorder_commands.add_parser(
        "record", help="run one continuous recording cycle"
    )
    recorder_record.add_argument("--frames", type=int, default=100)
    recorder_alerts = recorder_commands.add_parser("alerts", help="poll and send pending event alerts")
    recorder_alerts.add_argument("--watch", action="store_true", help="keep polling without restarting Python")
    recorder_engine = recorder_commands.add_parser(
        "reconcile-engine", help=argparse.SUPPRESS
    )
    recorder_engine.add_argument("--state-dir", required=True, help=argparse.SUPPRESS)
    recorder_engine.add_argument(
        "--candidate", action="append", required=True, help=argparse.SUPPRESS
    )
    recorder_engine.add_argument(
        "--changed-exit-code", action="store_true", help=argparse.SUPPRESS
    )
    recorder_engine.add_argument(
        "--mjpeg-hardware", choices=("v4l2m2m",), help=argparse.SUPPRESS
    )
    recorder_engine.add_argument(
        "--mjpeg-software-preset", choices=("superfast", "ultrafast"),
        default="superfast", help=argparse.SUPPRESS
    )
    recorder_engine.add_argument(
        "--validation-continuous-days", type=int, choices=(0, 1),
        default=0, help=argparse.SUPPRESS
    )
    recorder_relay = recorder_commands.add_parser(
        "route-relay", help=argparse.SUPPRESS
    )
    recorder_relay.add_argument("--bind", required=True, help=argparse.SUPPRESS)
    recorder_relay.add_argument("--port", type=int, default=9081, help=argparse.SUPPRESS)
    recorder_relay.add_argument(
        "--allowed-client-network",
        action="append",
        required=True,
        help=argparse.SUPPRESS,
    )

    backup = commands.add_parser("backup", help="protect or restore recorder state")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_commands.add_parser(
        "create", help="create an encrypted state and event-catalog backup"
    )
    backup_create.add_argument("destination", metavar="FILE")
    backup_restore = backup_commands.add_parser(
        "restore", help="restore into a new empty application data directory"
    )
    backup_restore.add_argument("source", metavar="FILE")

    remote = commands.add_parser("remote", help="set up simple remote access")
    remote_commands = remote.add_subparsers(dest="remote_command", required=True)
    remote_commands.add_parser(
        "setup", help="set up private or owner-gateway access"
    )
    remote_commands.add_parser("status", help="show where to open the cameras")
    remote_commands.add_parser("tunnel", help=argparse.SUPPRESS)

    commands.add_parser("status", help="show system status")
    commands.add_parser("update", help="prepare the newest verified update")

    dev = commands.add_parser("dev", help="developer checks")
    dev_commands = dev.add_subparsers(dest="dev_command", required=True)
    doctor = dev_commands.add_parser("doctor", help="run safety checks")
    doctor.add_argument("--ose", action="store_true")
    doctor.add_argument(
        "--policy", dest="doctor_policy", metavar="FILE", help="external OSE safety policy"
    )

    kit = commands.add_parser("kit", help="factory kit operations")
    kit_commands = kit.add_subparsers(dest="kit_command", required=True)
    kit_provision = kit_commands.add_parser("provision", help="create a recorder kit")
    kit_provision.add_argument("--cameras", type=int)
    kit_provision.add_argument(
        "--model", choices=("zero-w", "zero-2-w")
    )
    kit_provision.add_argument("--config", metavar="CONFIG_FILE")
    kit_provision.add_argument(
        "--country",
        metavar="CODE",
        help="uppercase two-letter Wi-Fi regulatory country code",
    )
    kit_provision.add_argument("--flash", action="store_true")
    kit_provision.add_argument("--allow-development", action="store_true")
    kit_commands.add_parser("verify", help="verify every camera in the active kit")

    gui = commands.add_parser("gui", help="open the local setup interface")
    gui.add_argument("--port", type=int, default=0)
    gui.add_argument(
        "--factory", action="store_true", help="enable factory provisioning pages"
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    application: VMSApplication | None = None,
    input_fn: Callable[[str], str] | None = None,
    secret_fn: Callable[[str], str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one CLI command and return its process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    read = input if input_fn is None else input_fn
    read_secret = getpass.getpass if secret_fn is None else secret_fn
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    selected_policy = getattr(args, "doctor_policy", None) or args.policy
    if application is not None:
        app = application
        reload_application = lambda: application
    else:
        def reload_application():
            return VMSApplication(
                args.data_dir,
                policy_path=selected_policy,
                release_directory=args.release_dir,
            )

        try:
            app = reload_application()
        except ApplicationError as error:
            _line(errors, f"Error: {error}")
            return 2
        except Exception:
            _line(errors, "Error: The application could not be started.")
            return 2

    try:
        if args.command == "provision":
            return _provision_command(args, app, read, read_secret, output)

        if args.command == "setup":
            name = args.name or read("Owner name: ").strip()
            password = _confirmed_secret(
                read_secret, "Owner password: ", "Confirm owner password: "
            )
            app.create_owner(name=name, password=password)
            _line(output, "Owner is ready.")
            return 0

        if args.command == "storage":
            target = _select_recording_storage_target(app, read, output)
            confirmation = (
                target.adoption_confirmation
                if args.storage_command == "adopt"
                else target.confirmation
            )
            _line(output, f"Type exactly: {confirmation}")
            confirmation = read("Confirmation: ")
            if args.storage_command == "adopt":
                app.adopt_selected_recording_storage(
                    stable_id=target.stable_id,
                    confirmation=confirmation,
                )
            else:
                app.initialize_selected_recording_storage(
                    stable_id=target.stable_id,
                    confirmation=confirmation,
                )
            _line(output, "Recording storage is ready.")
            return 0

        if args.command == "camera":
            return _camera_command(args, app, read, read_secret, output)

        if args.command == "recording":
            if args.recording_command == "set":
                app.set_recording(mode=args.mode, days=args.days)
                _line(output, "Recording settings saved.")
            elif args.recording_command == "list":
                _json(output, app.list_recordings(limit=args.limit))
            else:
                _json(output, app.maintain_recordings())
            return 0

        if args.command == "recorder":
            if args.recorder_command == "serve":
                return _serve_portal(
                    app,
                    port=args.port,
                    factory=False,
                    local_network=args.local_network,
                    output=output,
                )
            if args.recorder_command == "alerts":
                return _alert_worker(
                    app, watch=args.watch, reload_application=reload_application,
                    output=output,
                )
            if args.recorder_command == "record":
                return _recording_worker(
                    app,
                    frame_count=args.frames,
                    output=output,
                )
            if args.recorder_command == "route-relay":
                return _serve_camera_route_relay(
                    app,
                    bind_address=args.bind,
                    port=args.port,
                    allowed_client_networks=tuple(args.allowed_client_network),
                    output=output,
                )
            result = app.reconcile_video_engine_configuration(
                state_directory=args.state_dir,
                candidates=tuple(args.candidate),
                mjpeg_hardware=args.mjpeg_hardware,
                mjpeg_software_preset=args.mjpeg_software_preset,
                validation_continuous_days=args.validation_continuous_days,
            )
            _json(output, result)
            return 10 if args.changed_exit_code and result.get("changed") is True else 0

        if args.command == "backup":
            password = read_secret("Owner password: ")
            if args.backup_command == "create":
                summary = app.create_backup(
                    destination=args.destination, password=password
                )
                _line(
                    output,
                    "Encrypted backup created "
                    f"({summary.state_files} state files, "
                    f"{summary.recordings} recording records; video not included).",
                )
            else:
                summary = app.restore_backup(source=args.source, password=password)
                _line(
                    output,
                    "Backup restored "
                    f"({summary.state_files} state files, "
                    f"{summary.recordings} recording records; video not included).",
                )
            return 0

        if args.command == "remote":
            if args.remote_command == "setup":
                return _remote_setup(app, read, read_secret, output)
            if args.remote_command == "status":
                _print_access(output, app.remote_access_status())
                return 0
            return app.run_owner_gateway_tunnel()

        if args.command == "status":
            _json(output, app.status())
            return 0

        if args.command == "update":
            app.require_ose_doctor()
            app.update()
            _line(output, "Verified update is ready for confirmation.")
            return 0

        if args.command == "dev":
            report = app.doctor(ose=args.ose)
            _doctor_report(output, report)
            return 0 if report.passed else 2

        if args.command == "kit":
            return _kit_command(args, app, read, output)

        if args.command == "gui":
            return _serve_portal(
                app,
                port=args.port,
                factory=args.factory,
                local_network=False,
                output=output,
            )
    except (
        ApplicationError,
        ConfigurationError,
        ImagingError,
        RecorderError,
        RecorderStateError,
        RemoteAccessError,
    ) as error:
        _line(errors, f"Error: {error}")
        return 2
    except (OSError, ValueError):
        # Values at this boundary may originate in a private prompt.  Keep an
        # unexpected adapter error from reflecting them into terminal logs.
        _line(errors, "Error: The operation could not be completed.")
        return 2
    except Exception:
        _line(errors, "Error: The operation could not be completed.")
        return 2
    return 2


def _serve_portal(
    app: VMSApplication,
    *,
    port: int,
    factory: bool,
    local_network: bool,
    output: TextIO,
) -> int:
    server = app.create_gui_server(
        port=port, factory=factory, local_network=local_network
    )
    address, selected_port = server.server_address[:2]
    if local_network and selected_port == 80:
        _line(output, f"Open {LOCAL_PORTAL_URL}")
    else:
        _line(output, f"Open http://{address}:{selected_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _serve_camera_route_relay(
    app: VMSApplication,
    *,
    bind_address: str,
    port: int,
    allowed_client_networks: tuple[str, ...],
    output: TextIO,
) -> int:
    server = app.create_camera_route_relay_server(
        bind_address=bind_address,
        port=port,
        allowed_client_networks=allowed_client_networks,
    )
    _line(output, "Private camera route relay is ready.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _remote_setup(
    app: VMSApplication,
    read: Callable[[str], str],
    read_secret: Callable[[str], str],
    output: TextIO,
) -> int:
    _line(output, f"At home: {LOCAL_PORTAL_URL}")
    _line(output, "1. Private access (Tailscale app required)")
    _line(output, "2. Browser access (your DigitalOcean droplet)")
    selection = read("Choose 1 or 2: ").strip().lower()
    if selection in {"1", "private", "tailscale"}:
        _line(output, "Sign in or scan the code shown by Tailscale.")
        result = app.setup_private_remote_access()
    elif selection in {"2", "browser", "droplet", "gateway"}:
        domain = read("Remote address (example: cameras.example.com): ").strip()
        enrollment_code = _one_secret(read_secret, "One-time droplet code: ")
        result = app.setup_owner_gateway_access(
            domain=domain, enrollment_code=enrollment_code
        )
    else:
        raise CLIInputError("Choose 1 for private access or 2 for browser access.")
    _line(output, "Remote access is ready.")
    _print_access(output, result)
    return 0


def _print_access(output: TextIO, status: Mapping[str, object]) -> None:
    local_url = status.get("local_url") or LOCAL_PORTAL_URL
    _line(output, f"At home: {local_url}")
    remote_url = status.get("remote_url")
    if remote_url:
        _line(output, f"Away: {remote_url}")
    else:
        _line(output, "Away: not set up")


def _alert_worker(
    app: VMSApplication,
    *,
    watch: bool,
    reload_application: Callable[[], VMSApplication],
    output: TextIO,
) -> int:
    """Keep imports warm, but reload owner settings before each new poll."""

    previous = None
    try:
        while True:
            try:
                result = dict(app.process_video_engine_alerts())
            except ApplicationError:
                if not watch:
                    raise
                result = {"state": "needs-attention", "delivered": 0}
            result["provider_accepted"] = result.pop("delivered", 0)
            result["phone_display"] = "not-measured"
            if result != previous:
                _json(output, result)
                output.flush()
                previous = result
            if not watch:
                return 0
            time.sleep(2)
            app = reload_application()
    except KeyboardInterrupt:
        return 0


def _recording_worker(
    app: VMSApplication,
    *,
    frame_count: int,
    output: TextIO,
) -> int:
    if not 2 <= frame_count <= 10_000:
        raise ApplicationError("Recorder worker settings are invalid.")
    try:
        result = app.capture_recording_cycle(frame_count=frame_count)
    except ApplicationError:
        result = {"state": "waiting", "cameras": []}
    _json(output, result)
    return 0


def _camera_command(
    args: argparse.Namespace,
    app: VMSApplication,
    read: Callable[[str], str],
    read_secret: Callable[[str], str],
    output: TextIO,
) -> int:
    if args.camera_command == "list":
        cameras = app.list_cameras(unclaimed=args.unclaimed)
        if not cameras:
            _line(output, "No cameras found.")
        for camera in cameras:
            name = camera.get("name") or "Pending"
            state = camera.get("state", "unknown")
            _line(output, f"{camera.get('id', 'Camera')} | {name} | {state}")
        return 0

    if args.camera_command == "claim":
        claim_code = _one_secret(read_secret, "One-time claim code: ")
        app.claim_camera(
            camera_id=args.camera_id, claim_code=claim_code, name=args.name
        )
        _line(output, "Camera added.")
        return 0

    if args.camera_command == "name":
        app.name_camera(camera_id=args.camera_id, name=args.name)
        _line(output, "Camera named.")
        return 0

    if args.camera_command == "recording":
        app.set_camera_recording(
            camera_id=args.camera_id,
            enabled=args.enabled,
        )
        _line(output, "Camera recording setting saved.")
        return 0

    if args.camera_command == "replace":
        claim_code = _one_secret(read_secret, "New camera claim code: ")
        app.replace_camera(
            camera_name=args.camera_name,
            replacement_id=args.replacement_id,
            claim_code=claim_code,
        )
        _line(output, "Camera replaced.")
        return 0

    if args.camera_command == "remove":
        confirmation = read(f"Type REMOVE {args.camera_name} to confirm: ")
        if confirmation != f"REMOVE {args.camera_name}":
            raise CLIInputError("Camera removal was not confirmed.")
        app.remove_camera(camera_name=args.camera_name)
        _line(output, "Camera removed.")
        return 0

    if args.camera_command == "verify":
        app.require_ose_doctor()
        config = load_provisioning_config(args.config) if args.config else None
        model = _selected_value(
            args.model,
            config.camera_model if config else None,
            label="camera model",
        )
        number = _selected_value(
            args.number,
            config.camera_number if config else None,
            label="camera number",
        )
        if model not in ("zero-w", "zero-2-w"):
            raise CLIInputError("Select a supported camera model.")
        if isinstance(number, bool) or not isinstance(number, int):
            raise CLIInputError("Camera number is required.")
        result = app.verify_pilot_camera(
            model=model,
            number=number,
            hostname=args.host,
            port=args.port,
        )
        passed = bool(result.get("passed"))
        _line(
            output,
            "Camera verification passed." if passed else "Camera verification failed.",
        )
        return 0 if passed else 2

    if args.camera_command == "stage":
        app.require_ose_doctor()
        config = load_provisioning_config(args.config) if args.config else None
        model = _selected_value(
            args.model,
            config.camera_model if config else None,
            label="camera model",
        )
        number = _selected_value(
            args.number,
            config.camera_number if config else None,
            label="camera number",
        )
        if model not in ("zero-w", "zero-2-w"):
            raise CLIInputError("Select a supported camera model.")
        if isinstance(number, bool) or not isinstance(number, int):
            raise CLIInputError("Camera number is required.")
        result = app.stage_pilot_camera(
            model=model,
            number=number,
            output_directory=args.output,
        )
        _line(output, f"Private configuration staged at {result['directory']}")
        return 0

    if args.camera_command == "migrate-release":
        app.require_ose_doctor()
        config = load_provisioning_config(args.config) if args.config else None
        model = _selected_value(
            args.model,
            config.camera_model if config else None,
            label="camera model",
        )
        number = _selected_value(
            args.number,
            config.camera_number if config else None,
            label="camera number",
        )
        if model not in ("zero-w", "zero-2-w"):
            raise CLIInputError("Select a supported camera model.")
        if isinstance(number, bool) or not isinstance(number, int):
            raise CLIInputError("Camera number is required.")
        result = app.migrate_pilot_camera_release(
            model=model,
            number=number,
            image_reference=args.release,
        )
        if result["result"] == "already_current":
            _line(output, "Camera release is already current; stage a new payload.")
        else:
            _line(output, "Camera release migrated; stage a new payload.")
        return 0

    if args.camera_command == "diagnostics":
        if args.model not in ("zero-w", "zero-2-w"):
            raise CLIInputError("Select a supported camera model.")
        if isinstance(args.number, bool) or not isinstance(args.number, int):
            raise CLIInputError("Camera number is required.")
        result = app.pilot_camera_diagnostics(
            model=args.model, number=args.number
        )
        _print_pilot_diagnostics(output, result)
        return 0

    if args.camera_command == "provision":
        # The safety gate intentionally happens before private network prompts.
        app.require_ose_doctor()
        config = load_provisioning_config(args.config) if args.config else None
        if config is not None and config.mode != "camera":
            raise CLIInputError("The configuration is not for one camera.")
        model = _selected_value(
            args.model,
            config.camera_model if config else None,
            label="camera model",
        )
        number = _selected_value(
            args.number,
            config.camera_number if config else None,
            label="camera number",
        )
        if model not in ("zero-w", "zero-2-w"):
            raise CLIInputError("Select a supported camera model.")
        if isinstance(number, bool) or not isinstance(number, int):
            raise CLIInputError("Camera number is required.")
        wifi_country = _select_wifi_country(
            command_value=args.country,
            file_value=config.network.country if config else None,
            application=app,
            read=read,
        )
        wifi_ssid = (
            config.network.ssid
            if config is not None and config.network.ssid is not None
            else _one_secret(read_secret, "Camera Wi-Fi name: ")
        )
        wifi_passphrase = _confirmed_secret(
            read_secret,
            "Camera Wi-Fi password: ",
            "Confirm Camera Wi-Fi password: ",
        )
        target = None
        confirmation = None
        if args.flash:
            target = _select_target(app, read, output)
            confirmation = _exact_erase_confirmation(target, read, output)
        request = build_provisioning_config(
            mode="camera",
            camera_model=model,
            camera_number=number,
            ssid=wifi_ssid,
            country=wifi_country,
        )
        result = app.prepare_provisioning(
            request,
            wifi_passphrase=wifi_passphrase,
            flash=args.flash,
            target=target,
            confirmation=confirmation,
            allow_development=args.allow_development,
            rotate_network=args.rotate_network,
            pilot_recovery=args.pilot_recovery,
        )
        device_id = result.get("device_id", "Camera")
        if args.flash:
            _line(output, f"{device_id} was flashed and verified.")
        else:
            _line(output, f"{device_id} is ready to flash. No card was changed.")
        if result.get("pilot_recovery"):
            _line(output, "Pilot recovery is enabled; diagnostics are persistent.")
            diagnostics = result.get("diagnostics")
            if isinstance(diagnostics, Mapping):
                _print_pilot_diagnostics(output, diagnostics)
        return 0
    raise CLIInputError("Camera command was not recognized.")


def _kit_command(
    args: argparse.Namespace,
    app: VMSApplication,
    read: Callable[[str], str],
    output: TextIO,
) -> int:
    if args.kit_command == "provision":
        app.require_ose_doctor()
        config = load_provisioning_config(args.config) if args.config else None
        if config is not None and config.mode != "kit":
            raise CLIInputError("The configuration is not for a complete kit.")
        camera_count = _selected_value(
            args.cameras,
            config.camera_count if config else None,
            label="camera count",
        )
        camera_model = _selected_value(
            args.model,
            config.camera_model if config else None,
            label="camera model",
        )
        if camera_model is None:
            camera_model = "zero-2-w"
        if isinstance(camera_count, bool) or not isinstance(camera_count, int):
            raise CLIInputError("Camera count is required.")
        if camera_model not in ("zero-w", "zero-2-w"):
            raise CLIInputError("Select a supported camera model.")
        wifi_country = _select_wifi_country(
            command_value=args.country,
            file_value=config.network.country if config else None,
            application=app,
            read=read,
        )
        request = build_provisioning_config(
            mode="kit",
            camera_model=camera_model,
            camera_count=camera_count,
            country=wifi_country,
        )
        result = app.prepare_provisioning(request)
        _line(
            output,
            f"Kit {result.get('kit_id', '')} is ready with {result.get('camera_count', camera_count)} cameras.",
        )
        if not args.flash:
            return 0
        while app.kit_flash_status()["remaining"]:
            read("Insert the next card, then press Enter: ")
            target = _select_target(app, read, output)
            confirmation = _exact_erase_confirmation(target, read, output)
            flashed = app.flash_next(
                target=target,
                confirmation=confirmation,
                dry_run=False,
                allow_development=args.allow_development,
            )
            _line(output, f"{flashed.get('device_id', 'Card')} completed.")
        _line(output, "All kit cards completed.")
        return 0

    if args.kit_command == "verify":
        app.require_ose_doctor()
        result = app.verify_kit()
        passed = bool(result.get("passed"))
        _line(output, "Kit verification passed." if passed else "Kit verification failed.")
        return 0 if passed else 2
    raise CLIInputError("Kit command was not recognized.")


def _provision_command(
    args: argparse.Namespace,
    app: VMSApplication,
    read: Callable[[str], str],
    read_secret: Callable[[str], str],
    output: TextIO,
) -> int:
    config = load_provisioning_config(args.config) if args.config else None
    mode = _selected_value(
        args.mode,
        config.mode if config else None,
        label="provisioning mode",
    )
    if mode is None:
        mode = read("Provision a kit or one camera [kit/camera]: ").strip().lower()
    if mode not in {"kit", "camera"}:
        raise CLIInputError("Select kit or camera provisioning.")

    if config is None:
        if args.model is None:
            selected_model = read("Camera model [zero-2-w]: ").strip()
            args.model = selected_model or "zero-2-w"
        if mode == "kit" and args.cameras is None:
            selected_count = read("Number of cameras [4]: ").strip()
            try:
                args.cameras = int(selected_count or "4")
            except ValueError:
                raise CLIInputError(
                    f"Camera count must be between 1 and {SOFTWARE_CAMERA_LIMIT}."
                ) from None
        if mode == "camera" and args.number is None:
            selected_number = read("Camera number: ").strip()
            try:
                args.number = int(selected_number)
            except ValueError:
                raise CLIInputError(
                    f"Camera number must be between 1 and {SOFTWARE_CAMERA_LIMIT}."
                ) from None

    if mode == "kit":
        if (
            args.number is not None
            or args.rotate_network
            or args.pilot_recovery is not None
        ):
            raise CLIInputError("Camera-only options cannot be used for a kit.")
        args.kit_command = "provision"
        return _kit_command(args, app, read, output)

    if args.cameras is not None:
        raise CLIInputError("Kit camera count cannot be used for one camera.")
    args.camera_command = "provision"
    return _camera_command(args, app, read, read_secret, output)


def _select_target(
    app: VMSApplication, read: Callable[[str], str], output: TextIO
) -> DiskTarget:
    targets = app.list_media_targets()
    if not targets:
        raise CLIInputError("No safe removable card was found.")
    _line(output, "Available removable cards:")
    for index, target in enumerate(targets, start=1):
        _line(output, f"  {index}. {target.display_name}")
    selected = read("Card number: ").strip()
    try:
        target = targets[int(selected) - 1]
    except (ValueError, IndexError):
        raise CLIInputError("Select one of the listed cards.") from None
    return target


def _select_recording_storage_target(
    app: VMSApplication, read: Callable[[str], str], output: TextIO
) -> RecordingStorageTarget:
    targets = app.list_recording_storage_targets()
    if not targets:
        raise CLIInputError("No prepared recording drive was found.")
    if len(targets) == 1:
        target = targets[0]
        _line(output, f"Recording drive: {target.display_name}")
        return target
    _line(output, "Available recording drives:")
    for index, target in enumerate(targets, start=1):
        _line(output, f"  {index}. {target.display_name}")
    selected = read("Drive number: ").strip()
    try:
        return targets[int(selected) - 1]
    except (ValueError, IndexError):
        raise CLIInputError("Select one of the listed recording drives.") from None


def _exact_erase_confirmation(
    target: DiskTarget, read: Callable[[str], str], output: TextIO
) -> str:
    _line(output, f"This will erase {target.display_name}.")
    _line(output, f"Type exactly: {target.confirmation}")
    confirmation = read("Confirmation: ")
    if confirmation != target.confirmation:
        raise CLIInputError("Card erase was not confirmed.")
    return confirmation


def _one_secret(read_secret: Callable[[str], str], prompt: str) -> str:
    value = read_secret(prompt)
    if not value:
        raise CLIInputError("A required private value was not entered.")
    return value


def _selected_value(command_value: object, file_value: object, *, label: str) -> object:
    if command_value is not None and file_value is not None and command_value != file_value:
        raise CLIInputError(f"The command and configuration {label} do not match.")
    return command_value if command_value is not None else file_value


def _select_wifi_country(
    *,
    command_value: object,
    file_value: object,
    application: VMSApplication,
    read: Callable[[str], str],
) -> str:
    selected = _selected_value(
        command_value,
        file_value,
        label="Wi-Fi country",
    )
    if selected is None:
        configured_default = getattr(application, "wifi_country_default", None)
        prompt = (
            f"Wi-Fi country code [{configured_default}]: "
            if configured_default is not None
            else "Wi-Fi country code (for example, US): "
        )
        entered = read(prompt).strip()
        selected = entered or configured_default
    try:
        return validate_wifi_country(selected)
    except ValueError as error:
        raise CLIInputError(
            "Enter an assigned uppercase two-letter Wi-Fi country code."
        ) from error


def _confirmed_secret(
    read_secret: Callable[[str], str], first_prompt: str, second_prompt: str
) -> str:
    first = _one_secret(read_secret, first_prompt)
    second = _one_secret(read_secret, second_prompt)
    if not _constant_time_text_equal(first, second):
        raise CLIInputError("The private values did not match.")
    return first


def _constant_time_text_equal(first: str, second: str) -> bool:
    # Encoding before compare keeps the operation valid for non-ASCII owner
    # passwords without exposing either input.
    import hmac

    return hmac.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


def _doctor_report(output: TextIO, report: DoctorReport) -> None:
    _line(output, "Safety checks passed." if report.passed else "Safety checks failed.")
    for finding in report.findings:
        subject = f" ({finding.subject})" if finding.subject else ""
        _line(output, f"{finding.severity.upper()}: {finding.message}{subject}")


def _line(stream: TextIO, value: str) -> None:
    print(value, file=stream, flush=True)


def _print_pilot_diagnostics(
    output: TextIO, diagnostics: Mapping[str, object]
) -> None:
    _line(
        output,
        "Private-network SSH: "
        f"{diagnostics.get('username')}@{diagnostics.get('private_network_ssh')}",
    )
    _line(output, f"SSH key: {diagnostics.get('ssh_key_file')}")
    _line(output, f"Recovery credentials: {diagnostics.get('credentials_file')}")
    _line(output, f"USB recovery: {diagnostics.get('usb_serial')}")
    _line(output, f"GPIO UART: {diagnostics.get('gpio_uart')}")
    _line(output, f"Earliest availability: {diagnostics.get('early_boot')}")
    _line(output, f"On-device report: {diagnostics.get('diagnostic_command')}")


def _json(stream: TextIO, value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True), file=stream, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
