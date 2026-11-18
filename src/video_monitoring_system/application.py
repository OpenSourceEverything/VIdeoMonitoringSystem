"""Application service shared by the local web interface and the CLI.

This module joins the product-domain services to provisioning, imaging, label
generation, and verification.  It deliberately keeps platform-specific disk
enumeration behind :class:`~video_monitoring_system.imaging.DiskDiscovery` and
stores generated credentials only in an owner-private data directory outside
the source repository.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from .backup import (
    BackupError,
    BackupSummary,
    create_encrypted_backup,
    read_encrypted_backup,
    restored_files,
)
from .camera_endpoints import (
    AuthenticatedCameraObservation,
    CameraEndpointError,
    CameraEndpointRegistry,
    ObservedCameraEndpoint,
)
from .camera_route_handoff import (
    CameraRouteHandoffError,
    FrigateCameraRouteAdapter,
    TransactionalCameraRouteHandoff,
)
from .camera_route_relay import CameraRouteRelayServer, CameraRouteRelayState
from .camera_setup import (
    CameraSetupBinding,
    CameraSetupCoordinator,
    CameraSetupError,
    VerifiedCameraEnrollment,
)
from .configuration import ProvisioningConfig
from .device.journal_reader import (
    CAMERA_LOG_SOURCES,
    RECORDER_LOG_SOURCES,
    redact_log_text,
    unavailable_journal,
)
from .disks import AutoDiskDiscovery, DiskDiscoveryError, is_supported_system_card
from .doctor import (
    POLICY_ENVIRONMENT_VARIABLE,
    DoctorReport,
    run_doctor,
)
from .engine_alerts import EngineAlertLedger, EngineAlertLedgerError
from .frigate_configuration import (
    FrigateCameraSource,
    FrigateConfigurationError,
    load_camera_policies,
    write_managed_frigate_configuration,
)
from .gui import create_server
from .imaging import (
    BootCustomizer,
    DiskDiscovery,
    DiskTarget,
    ImageReleaseManifest,
    ImagingPlan,
    ImagingResult,
    image_media,
)
from .labels import (
    ClaimPayloadError,
    parse_claim_payload,
    parse_camera_code,
    write_claim_label,
    write_label_index,
)
from .model import SOFTWARE_CAMERA_LIMIT
from .notifications import PushNotificationError, PushNotificationService
from .provisioning import (
    PILOT_RECOVERY_USERNAME,
    PREINSTALLED_CAMERA_IMAGE_REFERENCE,
    DeviceConfiguration,
    DeviceIdentity,
    ProvisioningJob,
    ProvisioningSpec,
    camera_dhcp_client_id,
    get_board_profile,
    pilot_recovery_ssh_private_key,
    secure_write_text,
)
from .recorder import RecorderError, RecorderService
from .recorder_authorization import (
    RecorderAuthorization,
    RecorderAuthorizationError,
    load_recorder_authorization,
)
from .recording import (
    MjpegFrameSource,
    RecorderCaptureService,
    RecordingCaptureError,
    RecordingStorage,
    RecordingStorageError,
    RecordingVolumeAttestation,
)
from .recorder_runtime import RecorderRuntime
from .remote_access import (
    GATEWAY_KEY_FILE,
    GATEWAY_KNOWN_HOSTS_FILE,
    GATEWAY_TUNNEL_FILE,
    REMOTE_STATE_FILE,
    RemoteAccessManager,
    gateway_tunnel_command,
)
from .storage_adapter import (
    LinuxRecordingStorageDiscovery,
    RecordingStorageDiscovery,
    RecordingStorageDiscoveryError,
    RecordingStorageTarget,
)
from .regulatory import (
    WIFI_COUNTRY_ENVIRONMENT_VARIABLE,
    validate_wifi_country,
)
from .verification import (
    CheckResult,
    HttpCameraTransport,
    VerificationReport,
    camera_media_profile,
    camera_media_routes,
    verify_camera,
    verify_camera_source,
)
from .video_engine import (
    VIDEO_ENGINE_CONFIG_FILE,
    FrigateEngine,
    VideoEngine,
    VideoEngineError,
    load_configured_video_engine,
)


class ApplicationError(RuntimeError):
    """A safe, user-facing application operation could not be completed."""


class SafetyCheckFailed(ApplicationError):
    """The external OSE safety policy did not pass."""


DoctorRunner = Callable[..., DoctorReport]
ImageWriter = Callable[..., ImagingPlan | ImagingResult]
CameraVerifier = Callable[..., VerificationReport]
LabelWriter = Callable[[DeviceIdentity, Path], Path]
LabelIndexWriter = Callable[[tuple[DeviceIdentity, ...], Path], Path]
RecordingSourceProvider = Callable[[], Sequence[MjpegFrameSource]]
_IMPORTED_CAMERA_SOURCES_FILE = "imported-camera-sources.json"
_CAMERA_VERIFICATIONS_FILE = "camera-verifications.json"
RECORDER_AUTHORIZATION_ENVIRONMENT_VARIABLE = "VMS_RECORDER_DEVICE_CONFIG"


def default_data_directory() -> Path:
    """Return the platform-neutral private state location.

    ``VMS_DATA_DIR`` is useful for an appliance or factory workstation.  The
    home-directory fallback keeps generated state away from a source checkout.
    """

    configured = os.environ.get("VMS_DATA_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".vms"


def load_release_catalog(directory: str | os.PathLike[str]) -> dict[str, ImageReleaseManifest]:
    """Load public image manifests from a directory.

    A manifest may optionally provide ``image_reference``.  When it does not,
    callers can still select it by role and supported board.
    """

    catalog: dict[str, ImageReleaseManifest] = {}
    release_directory = Path(directory)
    if not release_directory.is_dir():
        return catalog
    for path in sorted(release_directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                continue
            manifest_value = dict(value)
            reference = manifest_value.pop("image_reference", None)
            manifest = ImageReleaseManifest.from_dict(manifest_value)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
        key = reference if isinstance(reference, str) and reference.strip() else path.stem
        catalog[key] = manifest
    return catalog


class VMSApplication:
    """Concrete application facade used by both supported user interfaces."""

    def __init__(
        self,
        data_directory: str | os.PathLike[str] | None = None,
        *,
        repository: str | os.PathLike[str] = ".",
        policy_path: str | os.PathLike[str] | None = None,
        release_catalog: Mapping[str, ImageReleaseManifest] | None = None,
        release_directory: str | os.PathLike[str] | None = None,
        trusted_release_keys: Mapping[str, str | bytes] | None = None,
        disk_discovery: DiskDiscovery | None = None,
        doctor_runner: DoctorRunner = run_doctor,
        image_writer: ImageWriter = image_media,
        camera_verifier: CameraVerifier = verify_camera,
        label_writer: LabelWriter = write_claim_label,
        label_index_writer: LabelIndexWriter = write_label_index,
        imager_which: Callable[[str], str | None] | None = None,
        imager_runner: Callable[..., object] | None = None,
        boot_customizer: BootCustomizer | None = None,
        recording_storage_discovery: RecordingStorageDiscovery | None = None,
        recording_source_provider: RecordingSourceProvider | None = None,
        wifi_country_default: str | None = None,
        remote_access_manager: RemoteAccessManager | None = None,
        recorder_authorization_path: str | os.PathLike[str] | None = None,
        video_engine: VideoEngine | None = None,
        camera_route_handoff: TransactionalCameraRouteHandoff | None = None,
        camera_setup_coordinator: CameraSetupCoordinator | None = None,
    ) -> None:
        self.data_directory = Path(data_directory or default_data_directory()).expanduser().resolve()
        self.repository = Path(repository).expanduser().resolve()
        self.policy_path = Path(policy_path).expanduser().resolve() if policy_path else None
        selected_release_directory = (
            Path(release_directory).expanduser()
            if release_directory is not None
            else self.repository / "releases"
        )
        self.release_catalog = dict(
            release_catalog
            if release_catalog is not None
            else load_release_catalog(selected_release_directory)
        )
        self.trusted_release_keys = dict(trusted_release_keys or {})
        self.disk_discovery = disk_discovery or AutoDiskDiscovery()
        self._doctor_runner = doctor_runner
        self._image_writer = image_writer
        self._camera_verifier = camera_verifier
        self._label_writer = label_writer
        self._label_index_writer = label_index_writer
        self._imager_which = imager_which
        self._imager_runner = imager_runner
        self._boot_customizer = boot_customizer
        self.recording_storage_discovery = (
            recording_storage_discovery
            if recording_storage_discovery is not None
            else (
                LinuxRecordingStorageDiscovery()
                if sys.platform.startswith("linux")
                else None
            )
        )
        self._recording_source_provider = (
            recording_source_provider or self._private_recording_sources
        )
        configured_wifi_country = (
            wifi_country_default
            if wifi_country_default is not None
            else os.environ.get(WIFI_COUNTRY_ENVIRONMENT_VARIABLE)
        )
        try:
            self.wifi_country_default = (
                None
                if configured_wifi_country is None
                else validate_wifi_country(configured_wifi_country)
            )
        except ValueError as error:
            raise ApplicationError(
                f"{WIFI_COUNTRY_ENVIRONMENT_VARIABLE} must contain an assigned "
                "uppercase two-letter country code."
            ) from error

        if _is_within(self.data_directory, self.repository):
            raise ApplicationError(
                "Private product data must be stored outside the source repository."
            )
        self.data_directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_directory, 0o700)
        except OSError:
            pass
        self.camera_endpoints = CameraEndpointRegistry(
            self.data_directory / "camera-endpoints.json"
        )
        self.camera_setup = camera_setup_coordinator or CameraSetupCoordinator(
            self.data_directory
        )
        self.recorder = RecorderService(self.data_directory / "recorder-state.json")
        selected_authorization = recorder_authorization_path or os.environ.get(
            RECORDER_AUTHORIZATION_ENVIRONMENT_VARIABLE
        )
        self.recorder_authorization: RecorderAuthorization | None = None
        if selected_authorization is not None:
            try:
                self.recorder_authorization = load_recorder_authorization(
                    selected_authorization
                )
                if self.recorder_authorization is not None:
                    for camera in self.recorder_authorization.cameras:
                        existing = next(
                            (
                                item
                                for item in self.recorder.state["cameras"]
                                if item["id"] == camera.device_id
                            ),
                            None,
                        )
                        expected_identity = camera.identity_reference
                        if existing is not None:
                            if existing.get("identity_reference") != expected_identity:
                                raise RecorderError(
                                    "That camera identity already exists."
                                )
                            # Factory authorization proves identity; durable
                            # owner state remains authoritative for names,
                            # claims, recording choices, and revocation.
                            continue
                        self.recorder.register_camera(
                            camera_id=camera.device_id,
                            claim_secret_hash=camera.claim_secret_hash,
                            identity_reference=expected_identity,
                            pre_enrolled=True,
                        )
            except RecorderAuthorizationError as error:
                raise ApplicationError(
                    "Recorder camera authorization could not be loaded."
                ) from error
            except RecorderError as error:
                raise ApplicationError(
                    "Recorder camera authorization conflicts with saved state."
                ) from error
        self.remote_access = remote_access_manager or RemoteAccessManager(
            self.data_directory
        )
        self.push_notifications = PushNotificationService(self.data_directory)
        try:
            self.video_engine = (
                video_engine
                if video_engine is not None
                else load_configured_video_engine(self.data_directory)
            )
        except VideoEngineError as error:
            raise ApplicationError(str(error)) from error
        self.engine_alerts = EngineAlertLedger(self.data_directory)
        if self.video_engine is not None:
            try:
                self.engine_alerts.initialize()
            except EngineAlertLedgerError as error:
                raise ApplicationError(str(error)) from error
        self.camera_route_relay: CameraRouteRelayState | None = None
        if isinstance(self.video_engine, FrigateEngine) and self.video_engine.route_relay_enabled:
            self.camera_route_relay = CameraRouteRelayState(
                self.data_directory,
                secret_provider=self._camera_stream_secret,
            )
        self.camera_route_handoff = camera_route_handoff
        if (
            self.camera_route_handoff is None
            and isinstance(self.video_engine, FrigateEngine)
            and self.camera_route_relay is not None
        ):
            self.camera_route_handoff = TransactionalCameraRouteHandoff(
                FrigateCameraRouteAdapter(
                    self.video_engine,
                    self.camera_route_relay,
                )
            )
        self._camera_verifications = self._load_camera_verifications()

    # Retail facade -----------------------------------------------------

    def status(self) -> Mapping[str, object]:
        status = dict(self.recorder.status())
        status["access"] = self.remote_access.status()
        return status

    def remote_access_status(self) -> Mapping[str, object]:
        return self.remote_access.status()

    def push_public_key(self) -> str:
        return self.push_notifications.public_key()

    def subscribe_push(self, subscription: Mapping[str, object]) -> str:
        return self.push_notifications.subscribe(subscription)

    def unsubscribe_push(self, endpoint: str) -> None:
        self.push_notifications.unsubscribe(endpoint)

    def send_test_push(self, endpoint: str) -> None:
        access = self.remote_access.status()
        remote_url = access.get("remote_url")
        self.push_notifications.send_test(
            endpoint,
            contact_uri=str(remote_url) if isinstance(remote_url, str) else "",
        )

    def video_engine_status(self) -> Mapping[str, object]:
        if self.video_engine is None:
            return {"state": "legacy", "engine": "legacy"}
        try:
            return self.video_engine.status()
        except VideoEngineError as error:
            raise ApplicationError(str(error)) from error

    def create_camera_route_relay_server(
        self,
        *,
        bind_address: str,
        port: int,
        allowed_client_networks: Sequence[str],
    ) -> CameraRouteRelayServer:
        """Create the isolated recorder-to-engine stream relay."""

        try:
            relay = self.camera_route_relay or CameraRouteRelayState(
                self.data_directory,
                secret_provider=self._camera_stream_secret,
            )
            return CameraRouteRelayServer(
                (bind_address, port),
                relay,
                allowed_client_networks=tuple(allowed_client_networks),
            )
        except (OSError, TypeError, ValueError) as error:
            raise ApplicationError("Camera route relay could not be started.") from error

    def reconcile_video_engine_configuration(
        self,
        *,
        state_directory: str | os.PathLike[str],
        candidates: Sequence[str],
        mjpeg_hardware: str | None = None,
        mjpeg_software_preset: str = "superfast",
        validation_continuous_days: int = 0,
    ) -> Mapping[str, object]:
        """Derive private Frigate runtime files from OSE-owned camera state."""

        if not isinstance(self.video_engine, FrigateEngine):
            return {"state": "disabled", "changed": False, "cameras": 0}
        try:
            selected_state_directory = Path(state_directory).expanduser().resolve()
            if (
                selected_state_directory == self.data_directory
                or not _is_within(selected_state_directory, self.data_directory)
            ):
                raise ApplicationError(
                    "Private video-engine state must remain inside product state."
                )
            available = {
                source.camera_id: source for source in self._recording_source_provider()
            }
            configured: list[FrigateCameraSource] = []
            recording_choices = {
                camera["id"]: camera.get("recording_enabled", True)
                for camera in self.recorder.state["cameras"]
            }
            for camera_id, engine_name in self.video_engine.camera_bindings.items():
                source = available.get(camera_id)
                authenticated_url = getattr(source, "authenticated_url", None)
                health_reader = getattr(source, "get_json", None)
                if (
                    source is None
                    or not callable(authenticated_url)
                    or not callable(health_reader)
                ):
                    raise ApplicationError(
                        "A video-engine camera source is unavailable."
                    )
                try:
                    health = health_reader("/health")
                except Exception:
                    # Leave the last known-good engine files untouched while a
                    # camera is offline. Guessing MJPEG here would roll a
                    # native-H.264 camera back to a nonexistent endpoint.
                    raise ApplicationError(
                        "A video-engine camera source is unavailable."
                    ) from None
                if health.get("device_id") != camera_id:
                    raise ApplicationError(
                        "A video-engine camera source is unavailable."
                    )
                stream_path, codec, frame_rate = camera_media_profile(health)
                configured.append(
                    FrigateCameraSource(
                        camera_id=camera_id,
                        engine_name=engine_name,
                        stream_url=(
                            self.video_engine.private_route_source(
                                camera_id=camera_id,
                                selection="active",
                            )
                            if self.video_engine.route_relay_enabled
                            else authenticated_url(stream_path, resolve=True)
                        ),
                        codec=codec,
                        frame_rate=frame_rate,
                        recording_enabled=recording_choices.get(camera_id, True),
                    )
                )
            policies = load_camera_policies(self.data_directory)
            selected_policies = {
                camera_id: policies[camera_id]
                for camera_id in self.video_engine.camera_bindings
                if camera_id in policies
            }
            result = write_managed_frigate_configuration(
                selected_state_directory,
                configured,
                candidates=candidates,
                policies=selected_policies,
                retention_days=int(self.recorder.state["recording"]["days"]),
                validation_continuous_days=validation_continuous_days,
                mjpeg_hardware=mjpeg_hardware,
                mjpeg_software_preset=mjpeg_software_preset,
            )
        except ApplicationError:
            raise
        except (FrigateConfigurationError, OSError, TypeError, ValueError) as error:
            raise ApplicationError(str(error)) from error
        return {
            "state": "ready",
            "changed": result.changed,
            "cameras": result.cameras,
        }

    def list_video_events(
        self, *, limit: int = 100, camera_id: str | None = None
    ) -> Sequence[Mapping[str, object]]:
        if self.video_engine is None:
            return ()
        if camera_id is not None and camera_id not in self.recorder.active_camera_ids:
            raise ApplicationError("Camera is unavailable.")
        try:
            events = self.video_engine.list_events(
                camera_ids=() if camera_id is None else (camera_id,),
                limit=limit,
            )
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error
        return tuple(event.to_dict() for event in events)

    def read_video_event_preview(self, *, event_id: str) -> bytes:
        if self.video_engine is None:
            raise ApplicationError("Video event is unavailable.")
        try:
            return self.video_engine.read_event_preview(event_id=event_id)
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error

    def read_video_event_vod(
        self, *, event_id: str, resource: str
    ) -> tuple[bytes, str]:
        if self.video_engine is None:
            raise ApplicationError("Video event is unavailable.")
        try:
            return self.video_engine.read_event_vod(
                event_id=event_id, resource=resource
            )
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error

    def read_video_event_thumbnail(self, *, event_id: str) -> bytes:
        if self.video_engine is None:
            raise ApplicationError("Video event is unavailable.")
        try:
            return self.video_engine.read_event_thumbnail(event_id=event_id)
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error

    def save_video_event(self, *, event_id: str) -> Mapping[str, object]:
        if self.video_engine is None:
            raise ApplicationError("Video event is unavailable.")
        try:
            return self.video_engine.save_event(event_id=event_id)
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error

    def delete_video_event(self, *, event_id: str) -> None:
        if self.video_engine is None:
            raise ApplicationError("Video event is unavailable.")
        try:
            self.video_engine.delete_event(event_id=event_id)
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error

    def list_saved_video_clips(
        self, *, limit: int = 100, camera_id: str | None = None
    ) -> Sequence[Mapping[str, object]]:
        if self.video_engine is None:
            return ()
        if camera_id is not None and camera_id not in self.recorder.active_camera_ids:
            raise ApplicationError("Camera is unavailable.")
        try:
            clips = self.video_engine.list_saved_clips(
                camera_ids=() if camera_id is None else (camera_id,),
                limit=limit,
            )
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error
        return tuple(clip.to_dict() for clip in clips)

    def read_saved_video_clip(self, *, clip_id: str) -> bytes:
        if self.video_engine is None:
            raise ApplicationError("Saved clip is unavailable.")
        try:
            return self.video_engine.read_saved_clip(clip_id=clip_id)
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error

    def delete_saved_video_clip(self, *, clip_id: str) -> None:
        if self.video_engine is None:
            raise ApplicationError("Saved clip is unavailable.")
        try:
            self.video_engine.delete_saved_clip(clip_id=clip_id)
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error

    def negotiate_live_webrtc(self, *, camera_id: str, offer: str) -> str:
        """Authorize a camera and exchange WebRTC session descriptions."""

        if camera_id not in self.recorder.active_camera_ids:
            raise ApplicationError("Camera is unavailable.")
        if self.video_engine is None:
            raise ApplicationError("Live WebRTC video is unavailable.")
        try:
            return self.video_engine.negotiate_live_webrtc(
                camera_id=camera_id,
                offer=offer,
            )
        except (TypeError, ValueError, VideoEngineError) as error:
            raise ApplicationError(str(error)) from error

    def process_video_engine_alerts(
        self, *, now: datetime | None = None
    ) -> Mapping[str, object]:
        """Poll and send independently of recording, with one active worker."""

        try:
            with self.engine_alerts.delivery_cycle() as acquired:
                if not acquired:
                    return {"state": "busy", "delivered": 0}
                return self._process_video_engine_alerts(now=now)
        except OSError:
            raise ApplicationError("Alert worker state is unavailable.") from None

    def _process_video_engine_alerts(
        self, *, now: datetime | None = None
    ) -> Mapping[str, object]:
        """A successful send means provider acceptance, not phone display."""

        if self.video_engine is None:
            return {"state": "disabled", "delivered": 0}
        selected_now = datetime.now(UTC) if now is None else now
        if not isinstance(selected_now, datetime) or selected_now.tzinfo is None:
            raise ApplicationError("Alert delivery time is invalid.")
        try:
            events = self.video_engine.list_events(limit=50)
            camera_alerts = {
                str(camera.get("id", "")): camera.get("alerts_enabled") is not False
                for camera in self.list_cameras()
            }
            alerts_enabled = self.recorder.alerts_enabled
            for event in events:
                if event.severity != "alert":
                    continue
                if alerts_enabled and camera_alerts.get(event.camera_id, False):
                    continue
                self.engine_alerts.suppress(event.id, now=selected_now)
            pending = self.engine_alerts.due(events, now=selected_now, limit=5)
        except (TypeError, ValueError, VideoEngineError, EngineAlertLedgerError) as error:
            raise ApplicationError(str(error)) from error
        if not alerts_enabled:
            return {"state": "paused", "delivered": 0}
        access = self.remote_access.status()
        remote_url = access.get("remote_url")
        if not isinstance(remote_url, str) or not remote_url.startswith("https://"):
            return {"state": "waiting-for-remote-access", "delivered": 0}
        camera_names = {
            str(camera.get("id", "")): str(camera.get("name") or "Camera")
            for camera in self.list_cameras()
        }
        delivered = 0
        failed = 0
        for event in pending:
            camera_name = camera_names.get(event.camera_id, "Camera")
            subject = event.labels[0].replace("_", " ").title() if event.labels else "Activity"
            tag = "ose-event-" + hashlib.sha256(event.id.encode("utf-8")).hexdigest()[:24]
            try:
                self.push_notifications.send_event(
                    title=f"{subject} at {camera_name}"[:80],
                    body=(
                        "Tap to view activity in progress."
                        if event.ended_at is None
                        else "Tap to view the recorded event."
                    ),
                    url="/events?" + urlencode({"event": event.id}),
                    tag=tag,
                    contact_uri=remote_url,
                )
            except PushNotificationError:
                failed += 1
                notification_delivered = False
            else:
                delivered += 1
                notification_delivered = True
            try:
                self.engine_alerts.record(
                    event.id,
                    delivered=notification_delivered,
                    now=datetime.now(UTC) if now is None else selected_now,
                )
            except (TypeError, ValueError, EngineAlertLedgerError) as error:
                raise ApplicationError(str(error)) from error
        state = "ready" if failed == 0 else "needs-attention"
        return {"state": state, "delivered": delivered, "failed": failed}

    def setup_private_remote_access(self) -> Mapping[str, object]:
        return self.remote_access.setup_private()

    def begin_private_remote_access(self) -> Mapping[str, object]:
        return self.remote_access.begin_private()

    def disable_private_remote_access(self) -> Mapping[str, object]:
        return self.remote_access.disable_private()

    def setup_owner_gateway_access(
        self, *, domain: str, enrollment_code: str
    ) -> Mapping[str, object]:
        return self.remote_access.setup_gateway(
            domain=domain, enrollment_code=enrollment_code
        )

    def run_owner_gateway_tunnel(self) -> int:
        command = gateway_tunnel_command(self.data_directory)
        try:
            completed = subprocess.run(command, check=False)
        except OSError as error:
            raise ApplicationError("The owner gateway tunnel could not start.") from error
        return completed.returncode

    def list_cameras(self, *, unclaimed: bool = False) -> Sequence[Mapping[str, object]]:
        if unclaimed:
            return self.recorder.list_unclaimed_cameras()
        cameras = self.recorder.state["cameras"]
        return tuple(
            {
                "id": camera["id"],
                "name": camera["name"],
                "state": camera["state"],
                "connected": camera["connected"],
                "recording_enabled": camera.get("recording_enabled", True),
                "alerts_enabled": camera.get("alerts_enabled", True),
                "video_engine_bound": (
                    isinstance(self.video_engine, FrigateEngine)
                    and camera["id"] in self.video_engine.camera_bindings
                ),
                "verification": self._camera_verifications.get(camera["id"]),
                "network_issue": (
                    "duplicate-identity"
                    if self.camera_endpoints.conflict_for(camera["id"]) is not None
                    else None
                ),
            }
            for camera in cameras
            if not camera["revoked"]
        )

    def register_camera_endpoint(
        self,
        *,
        payload: bytes,
        signature: str,
        source_address: str,
    ) -> Mapping[str, object]:
        """Authenticate one camera heartbeat and publish its current LAN route."""

        if not isinstance(payload, bytes) or not 1 <= len(payload) <= 4096:
            raise ApplicationError("Camera registration is invalid.")
        if (
            not isinstance(signature, str)
            or re.fullmatch(r"[0-9a-f]{64}", signature) is None
        ):
            raise ApplicationError("Camera registration is unauthorized.")
        try:
            address = ipaddress.ip_address(source_address)
        except ValueError as error:
            raise ApplicationError("Camera registration is invalid.") from error
        if (
            address.version != 4
            or not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ApplicationError("Camera registration is outside the local network.")
        try:
            document = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ApplicationError("Camera registration is invalid.") from error
        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "camera_id",
            "port",
        }:
            raise ApplicationError("Camera registration is invalid.")
        camera_id = document.get("camera_id")
        port = document.get("port")
        if (
            document.get("schema_version") != 1
            or not isinstance(camera_id, str)
            or re.fullmatch(r"CAM-[0-9A-F]{16,64}", camera_id) is None
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or camera_id not in self.recorder.active_camera_ids
        ):
            raise ApplicationError("Camera registration is unauthorized.")

        stream_secret = self._camera_stream_secret(camera_id)
        if stream_secret is None:
            raise ApplicationError("Camera registration is unauthorized.")
        expected = hmac.new(
            stream_secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ApplicationError("Camera registration is unauthorized.")

        self._camera_setup_observe_heartbeat(camera_id)

        endpoint = ObservedCameraEndpoint(ip_address=str(address), port=port)
        transport = HttpCameraTransport(
            endpoint.ip_address,
            camera_id,
            stream_secret,
            port=endpoint.port,
            timeout=3,
        )
        try:
            health = transport.get_json("/health")
        except Exception as error:
            self._camera_setup_attention(
                camera_id, issue="video-verification-failed"
            )
            raise ApplicationError("Camera registration could not be verified.") from error
        if health.get("device_id") != camera_id or health.get("claimed") is not True:
            self._camera_setup_attention(camera_id, issue="identity-mismatch")
            raise ApplicationError("Camera registration could not be verified.")
        if health.get("camera_ready") is not True:
            self._camera_setup_attention(
                camera_id, issue="video-verification-failed"
            )
            raise ApplicationError("Camera registration could not be verified.")
        previous_endpoint = self.camera_endpoints.endpoint_for(camera_id)
        if (
            previous_endpoint is not None
            and previous_endpoint != endpoint
            and _camera_route_claims_identity(
                previous_endpoint,
                camera_id=camera_id,
                stream_secret=stream_secret,
            )
        ):
            try:
                self.camera_endpoints.quarantine(
                    camera_id=camera_id,
                    endpoint=endpoint,
                )
            except (CameraEndpointError, OSError) as error:
                raise ApplicationError(
                    "Duplicate camera identity could not be quarantined."
                ) from error
            self.recorder.set_camera_connected(camera_id=camera_id, connected=True)
            raise ApplicationError(
                "Duplicate camera identity detected. The working camera was kept; "
                "reset the extra camera, then resolve the conflict."
            )
        try:
            staged = self.camera_endpoints.observe(
                authorized_camera_ids=self.recorder.active_camera_ids,
                observation=AuthenticatedCameraObservation(
                    camera_id=camera_id,
                    endpoint=endpoint,
                ),
            )
        except (CameraEndpointError, OSError) as error:
            raise ApplicationError("Camera location could not be saved.") from error
        if previous_endpoint != endpoint:
            try:
                _verify_camera_route_media(transport, health)
            except Exception as error:
                self._camera_setup_attention(
                    camera_id, issue="video-verification-failed"
                )
                try:
                    self.camera_endpoints.reject(
                        camera_id=camera_id,
                        endpoint=endpoint,
                    )
                except (CameraEndpointError, OSError):
                    pass
                raise ApplicationError(
                    "Camera route media could not be verified."
                ) from error
            transaction = None
            if (
                isinstance(self.video_engine, FrigateEngine)
                and camera_id in self.video_engine.camera_bindings
            ):
                if self.camera_route_handoff is None:
                    self._camera_setup_attention(
                        camera_id, issue="route-commit-failed"
                    )
                    try:
                        self.camera_endpoints.reject(
                            camera_id=camera_id,
                            endpoint=endpoint,
                        )
                    except (CameraEndpointError, OSError):
                        pass
                    raise ApplicationError(
                        "Camera route downstream handoff is unavailable."
                    )
                try:
                    transaction = self.camera_route_handoff.prepare(
                        camera_id=camera_id,
                        previous=previous_endpoint,
                        candidate=endpoint,
                    )
                except CameraRouteHandoffError as error:
                    self._camera_setup_attention(
                        camera_id, issue="route-commit-failed"
                    )
                    try:
                        self.camera_endpoints.reject(
                            camera_id=camera_id,
                            endpoint=endpoint,
                        )
                    except (CameraEndpointError, OSError):
                        pass
                    raise ApplicationError(str(error)) from error
            try:
                result = self.camera_endpoints.promote(
                    camera_id=camera_id,
                    endpoint=endpoint,
                )
            except (CameraEndpointError, OSError) as error:
                self._camera_setup_attention(
                    camera_id, issue="route-commit-failed"
                )
                if transaction is not None:
                    try:
                        transaction.rollback()
                    except CameraRouteHandoffError as rollback_error:
                        raise ApplicationError(str(rollback_error)) from rollback_error
                try:
                    self.camera_endpoints.reject(
                        camera_id=camera_id,
                        endpoint=endpoint,
                    )
                except (CameraEndpointError, OSError):
                    pass
                raise ApplicationError("Camera location could not be saved.") from error
            if transaction is not None:
                try:
                    transaction.commit()
                except CameraRouteHandoffError as error:
                    self._camera_setup_attention(
                        camera_id, issue="route-commit-failed"
                    )
                    raise ApplicationError(str(error)) from error
        else:
            result = staged
        self.recorder.set_camera_connected(camera_id=camera_id, connected=True)
        self._camera_setup_mark_ready(camera_id)
        return {
            "result": (
                "updated"
                if result.updated
                else "registered" if result.added else "unchanged"
            ),
            "camera_id": camera_id,
        }

    def resolve_camera_identity_conflict(
        self, *, camera_id: str
    ) -> Mapping[str, object]:
        """Clear a quarantine after the owner resets or removes the duplicate."""

        if camera_id not in self.recorder.active_camera_ids:
            raise ApplicationError("Camera is unavailable.")
        duplicate = self.camera_endpoints.conflict_for(camera_id)
        if duplicate is None:
            raise ApplicationError("Camera has no network conflict.")
        connection = self._private_camera_connections().get(camera_id)
        if connection is None:
            raise ApplicationError("Camera network conflict could not be checked.")
        _, stream_secret = connection
        if _camera_route_claims_identity(
            duplicate,
            camera_id=camera_id,
            stream_secret=stream_secret,
        ):
            raise ApplicationError(
                "The extra camera is still online. Reset or unplug it, then recheck."
            )
        try:
            cleared = self.camera_endpoints.clear_conflict(camera_id)
        except (CameraEndpointError, OSError) as error:
            raise ApplicationError("Camera network conflict could not be cleared.") from error
        if not cleared:  # Defensive against an external state change during the probe.
            raise ApplicationError("Camera network conflict changed; recheck it.")
        return {"result": "resolved", "camera_id": camera_id}

    def position_camera(
        self, *, camera_id: str, position: int
    ) -> Mapping[str, object]:
        return self.recorder.position_camera(camera_id=camera_id, position=position)

    def set_camera_recording(
        self, *, camera_id: str, enabled: bool
    ) -> Mapping[str, object]:
        return self.recorder.set_camera_recording(
            camera_id=camera_id, enabled=enabled
        )

    def set_alerts_enabled(self, *, enabled: bool) -> Mapping[str, object]:
        if enabled and not self.recorder.alerts_enabled:
            self._suppress_current_video_engine_alerts()
        return self.recorder.set_alerts_enabled(enabled=enabled)

    def set_camera_alerts(
        self, *, camera_id: str, enabled: bool
    ) -> Mapping[str, object]:
        if enabled and camera_id not in self.recorder.alert_camera_ids:
            self._suppress_current_video_engine_alerts(camera_ids={camera_id})
        return self.recorder.set_camera_alerts(camera_id=camera_id, enabled=enabled)

    def _suppress_current_video_engine_alerts(
        self, *, camera_ids: set[str] | None = None
    ) -> None:
        """Prevent alerts accumulated during a pause from arriving on resume."""

        if self.video_engine is None:
            return
        selected_now = datetime.now(UTC)
        try:
            events = self.video_engine.list_events(limit=50)
            for event in events:
                if event.severity != "alert":
                    continue
                if camera_ids is not None and event.camera_id not in camera_ids:
                    continue
                self.engine_alerts.suppress(event.id, now=selected_now)
        except (TypeError, ValueError, VideoEngineError, EngineAlertLedgerError) as error:
            raise ApplicationError(str(error)) from error

    def verify_recorder_camera(self, *, camera_id: str) -> VerificationReport:
        if camera_id not in self.recorder.active_camera_ids:
            raise ApplicationError("Camera is unavailable.")
        try:
            sources = tuple(
                source
                for source in self._recording_source_provider()
                if source.camera_id == camera_id
            )
            if len(sources) != 1:
                raise ApplicationError("Camera is unavailable.")
            report = verify_camera_source(camera_id, transport=sources[0])
        except ApplicationError:
            raise
        except Exception as error:
            raise ApplicationError("Camera checks could not be completed.") from error
        connection = next(
            (check.passed for check in report.checks if check.name == "connection"),
            False,
        )
        self.recorder.set_camera_connected(camera_id=camera_id, connected=connection)
        previous = dict(self._camera_verifications)
        self._camera_verifications = {
            device_id: saved_report
            for device_id, saved_report in self._camera_verifications.items()
            if device_id in self.recorder.active_camera_ids
        }
        self._camera_verifications[camera_id] = report
        try:
            self._save_camera_verifications()
        except Exception as error:
            self._camera_verifications = previous
            raise ApplicationError("Camera checks could not be saved.") from error
        return report

    def create_owner(self, *, name: str, password: str) -> Mapping[str, object]:
        _require_password(password)
        owner = self.recorder.state["owner"]
        if owner is not None:
            reference = str(owner["credential_reference"])
            if owner["name"] == name.strip() and _password_matches(password, reference):
                return {"result": "already_configured", "message": "Owner is ready."}
        reference = _hash_password(password)
        return self.recorder.setup_owner(name=name, credential_reference=reference)

    def authenticate_owner(self, *, password: str) -> bool:
        """Verify the single recorder owner without exposing credential state."""

        if not isinstance(password, str) or not password or len(password) > 1024:
            return False
        owner = self.recorder.state["owner"]
        if not isinstance(owner, dict):
            return False
        reference = owner.get("credential_reference")
        return isinstance(reference, str) and _password_matches(password, reference)

    def change_owner_password(
        self, *, current_password: str, new_password: str
    ) -> Mapping[str, object]:
        """Rotate the owner's local password after verifying the current one."""

        _require_password(new_password)
        if not self.authenticate_owner(password=current_password):
            raise ApplicationError("The current owner password is incorrect.")
        owner = self.recorder.state["owner"]
        if not isinstance(owner, dict):
            raise ApplicationError("An owner must be created first.")
        reference = owner.get("credential_reference")
        if isinstance(reference, str) and _password_matches(new_password, reference):
            return {"result": "unchanged", "message": "Owner password is unchanged."}
        return self.recorder.change_owner_credential(
            credential_reference=_hash_password(new_password)
        )

    def create_backup(
        self, *, destination: str | os.PathLike[str], password: str
    ) -> BackupSummary:
        """Create an encrypted portable state and recording-metadata backup."""

        if not self.authenticate_owner(password=password):
            raise ApplicationError("The owner password is incorrect.")
        try:
            recordings = ()
            if self.recorder.initialized_storage_identity is not None:
                recordings = tuple(
                    {
                        "id": recording.id,
                        "camera_id": recording.camera_id,
                        "started_at": recording.started_at.isoformat(),
                        "ended_at": recording.ended_at.isoformat(),
                        "frame_count": recording.frame_count,
                        "encoded_bytes": recording.encoded_bytes,
                        "container_bytes": recording.container_bytes,
                        "sha256": recording.sha256,
                        "reference": recording.reference,
                    }
                    for recording in self._active_recording_storage().backup_catalog()
                )
            return create_encrypted_backup(
                destination,
                password=password,
                state_files=self._backup_state_files(),
                recordings=recordings,
            )
        except (BackupError, RecordingStorageError) as error:
            raise ApplicationError(str(error)) from error

    def create_backup_download(
        self, *, password: str
    ) -> tuple[bytes, str, BackupSummary]:
        """Return a freshly generated encrypted backup for the owner portal."""

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        filename = f"vms-recorder-backup-{timestamp}.vms-backup"
        with tempfile.TemporaryDirectory(prefix="vms-backup-") as directory:
            destination = Path(directory) / filename
            summary = self.create_backup(
                destination=destination,
                password=password,
            )
            try:
                content = destination.read_bytes()
            except OSError as error:
                raise ApplicationError("The encrypted backup could not be read.") from error
        return content, filename, summary

    def restore_backup(
        self, *, source: str | os.PathLike[str], password: str
    ) -> BackupSummary:
        """Restore a backup only into a new, otherwise empty data directory."""

        try:
            if any(self.data_directory.iterdir()):
                raise BackupError(
                    "Restore requires a new, empty application data directory."
                )
            document, summary = read_encrypted_backup(source, password=password)
            files = restored_files(document)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{self.data_directory.name}.restore.",
                    dir=self.data_directory.parent,
                )
            )
            os.chmod(staging, 0o700)
            try:
                for relative, content in files.items():
                    destination = staging.joinpath(*relative.split("/"))
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(destination.parent, 0o700)
                    descriptor = os.open(
                        destination,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                catalog = staging / "restored-recording-catalog.json"
                secure_write_text(
                    catalog,
                    json.dumps(
                        {
                            "schema_version": 1,
                            "created_at": summary.created_at,
                            "includes_video": False,
                            "recordings": document["recordings"],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
                restored = RecorderService(staging / "recorder-state.json")
                owner = restored.state.get("owner")
                reference = (
                    owner.get("credential_reference")
                    if isinstance(owner, dict)
                    else None
                )
                if not isinstance(reference, str) or not _password_matches(
                    password, reference
                ):
                    raise BackupError("The backup owner state did not verify.")
                validation = VMSApplication(
                    staging,
                    repository=self.repository,
                    release_catalog=self.release_catalog,
                    trusted_release_keys=self.trusted_release_keys,
                    disk_discovery=self.disk_discovery,
                    recording_storage_discovery=self.recording_storage_discovery,
                    recording_source_provider=self._recording_source_provider,
                    wifi_country_default=self.wifi_country_default,
                )
                validation._load_imported_camera_sources()
                load_camera_policies(staging)
                self.data_directory.rmdir()
                os.replace(staging, self.data_directory)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                self.data_directory.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(self.data_directory, 0o700)
                except OSError:
                    pass
                raise
            self.recorder = RecorderService(self.data_directory / "recorder-state.json")
            self._camera_verifications = self._load_camera_verifications()
            self._load_imported_camera_sources()
            return summary
        except BackupError as error:
            raise ApplicationError(str(error)) from error

    def initialize_storage(self, storage_reference: str = "recordings") -> Mapping[str, object]:
        return self.recorder.plan_storage_initialization(
            storage_reference=storage_reference
        )

    def list_recording_storage_targets(
        self,
    ) -> Sequence[RecordingStorageTarget]:
        if self.recording_storage_discovery is None:
            return ()
        try:
            return tuple(self.recording_storage_discovery.list_targets())
        except RecordingStorageDiscoveryError as error:
            raise ApplicationError(str(error)) from error

    def initialize_selected_recording_storage(
        self, *, stable_id: str, confirmation: str
    ) -> Mapping[str, object]:
        matches = [
            target
            for target in self.list_recording_storage_targets()
            if target.stable_id == stable_id
        ]
        if len(matches) != 1:
            raise ApplicationError("The selected recording drive is unavailable.")
        target = matches[0]
        if not hmac.compare_digest(
            confirmation.encode("utf-8"), target.confirmation.encode("utf-8")
        ):
            raise ApplicationError("Recording drive initialization was not confirmed.")
        return self.activate_recording_storage(target.attestation())

    def adopt_selected_recording_storage(
        self, *, stable_id: str, confirmation: str
    ) -> Mapping[str, object]:
        matches = [
            target
            for target in self.list_recording_storage_targets()
            if target.stable_id == stable_id
        ]
        if len(matches) != 1:
            raise ApplicationError("The selected recording drive is unavailable.")
        target = matches[0]
        if not hmac.compare_digest(
            confirmation.encode("utf-8"),
            target.adoption_confirmation.encode("utf-8"),
        ):
            raise ApplicationError("Recording drive adoption was not confirmed.")
        return self.adopt_recording_storage(target.attestation())

    def activate_recording_storage(
        self,
        attestation: RecordingVolumeAttestation,
    ) -> Mapping[str, object]:
        """Initialize an adapter-attested mounted volume and mark it ready."""

        return self._activate_recording_storage(attestation, adopt=False)

    def adopt_recording_storage(
        self,
        attestation: RecordingVolumeAttestation,
    ) -> Mapping[str, object]:
        """Adopt an adapter-attested volume while preserving unrelated data."""

        return self._activate_recording_storage(attestation, adopt=True)

    def _activate_recording_storage(
        self,
        attestation: RecordingVolumeAttestation,
        *,
        adopt: bool,
    ) -> Mapping[str, object]:

        if not isinstance(attestation, RecordingVolumeAttestation):
            raise ApplicationError(
                "A trusted recording-volume attestation is required."
            )
        expected_identity = (
            attestation.storage_reference,
            attestation.volume_identity,
        )
        initialized_identity = self.recorder.initialized_storage_identity
        if (
            initialized_identity is not None
            and initialized_identity != expected_identity
        ):
            raise ApplicationError(
                "Recording storage is already initialized to another volume."
            )
        if self.recorder.status()["storage"] == "invalid":
            raise ApplicationError("Recording storage state is invalid.")
        self.recorder.plan_storage_initialization(
            storage_reference=attestation.storage_reference
        )
        try:
            if initialized_identity is None:
                if adopt:
                    RecordingStorage.adopt_layout(attestation)
                else:
                    RecordingStorage.initialize_layout(attestation)
            else:
                RecordingStorage(attestation)
            result = self.recorder.confirm_storage_initialized(
                storage_reference=attestation.storage_reference,
                volume_identity=attestation.volume_identity,
            )
        except RecordingStorageError as error:
            raise ApplicationError(str(error)) from error
        return {
            "result": result["result"],
            "storage": {
                "state": "ready",
                "reference": attestation.storage_reference,
            },
        }

    def capture_camera_segment(
        self,
        *,
        storage_attestation: RecordingVolumeAttestation,
        source: MjpegFrameSource,
        frame_count: int = RecorderCaptureService.DEFAULT_FRAME_COUNT,
        maximum_bytes: int = RecorderCaptureService.DEFAULT_MAXIMUM_BYTES,
        timeout_seconds: float = RecorderCaptureService.DEFAULT_TIMEOUT_SECONDS,
    ) -> Mapping[str, object]:
        """Capture one bounded segment through an authenticated stream adapter."""

        try:
            storage = RecordingStorage(
                storage_attestation,
            )
            recording = RecorderCaptureService(
                self.recorder,
                storage,
            ).capture_segment(
                source=source,
                frame_count=frame_count,
                maximum_bytes=maximum_bytes,
                timeout_seconds=timeout_seconds,
            )
        except (RecordingCaptureError, RecordingStorageError) as error:
            raise ApplicationError(str(error)) from error
        return {
            "result": "recorded",
            "recording": {
                "id": recording.id,
                "camera_id": recording.camera_id,
                "started_at": recording.started_at.isoformat(),
                "ended_at": (
                    None
                    if recording.ended_at is None
                    else recording.ended_at.isoformat()
                ),
                "storage_reference": recording.storage_reference,
                "frame_count": recording.metadata["frame_count"],
                "encoded_bytes": recording.metadata["encoded_bytes"],
            },
        }

    def capture_recording_cycle(
        self,
        *,
        frame_count: int = RecorderCaptureService.DEFAULT_FRAME_COUNT,
        maximum_bytes: int = RecorderCaptureService.DEFAULT_MAXIMUM_BYTES,
        timeout_seconds: float = RecorderCaptureService.DEFAULT_TIMEOUT_SECONDS,
    ) -> Mapping[str, object]:
        """Capture one redacted concurrent cycle from every known camera."""

        if self.recorder.state["recording"]["mode"] != "continuous":
            return {"state": "idle", "cameras": []}
        try:
            storage = self._active_recording_storage()
            sources = tuple(self._recording_source_provider())
            managed_camera_ids = (
                frozenset(self.video_engine.camera_bindings)
                if isinstance(self.video_engine, FrigateEngine)
                else frozenset()
            )
            result = RecorderRuntime(self.recorder, storage).capture_cycle(
                sources,
                managed_camera_ids=managed_camera_ids,
                frame_count=frame_count,
                maximum_bytes=maximum_bytes,
                timeout_seconds=timeout_seconds,
            )
        except (RecordingCaptureError, RecordingStorageError) as error:
            raise ApplicationError(str(error)) from error
        return result

    def list_recordings(self, *, limit: int = 100) -> Sequence[Mapping[str, object]]:
        try:
            return tuple(
                recording.to_dict()
                for recording in self._active_recording_storage().list_recordings(
                    limit=limit
                )
            )
        except RecordingStorageError as error:
            raise ApplicationError(str(error)) from error

    def recording_storage_status(self) -> Mapping[str, object]:
        """Return owner-safe state and capacity for the configured drive."""

        state = str(self.recorder.status().get("storage", "planned"))
        if state != "ready":
            return {"state": state}
        try:
            capacity = self._active_recording_storage().capacity()
        except RecordingStorageError as error:
            raise ApplicationError(str(error)) from error
        return {"state": "ready", **capacity}

    def maintain_recordings(
        self, *, now: datetime | None = None
    ) -> Mapping[str, object]:
        """Apply the saved retention period to validated product recordings."""

        if self.recorder.initialized_storage_identity is None:
            return {"state": "waiting", "deleted": 0, "reclaimed_bytes": 0}
        selected_now = datetime.now(UTC) if now is None else now
        if not isinstance(selected_now, datetime) or selected_now.tzinfo is None:
            raise ApplicationError("Recorder maintenance time is invalid.")
        days = self.recorder.state["recording"]["days"]
        try:
            result = self._active_recording_storage().prune_before(
                selected_now.astimezone(UTC) - timedelta(days=days)
            )
        except RecordingStorageError as error:
            raise ApplicationError(str(error)) from error
        return {"state": "ready", **result}

    def read_recording_media(self, *, reference: str) -> tuple[bytes, str]:
        try:
            return self._active_recording_storage().read_recording_media(reference)
        except RecordingStorageError as error:
            raise ApplicationError(str(error)) from error

    def read_recording_preview(self, *, reference: str) -> bytes:
        try:
            return self._active_recording_storage().read_recording_preview(reference)
        except RecordingStorageError as error:
            raise ApplicationError(str(error)) from error

    def read_live_snapshot(self, *, camera_id: str) -> bytes:
        """Return one bounded authenticated frame without exposing camera access."""

        if camera_id not in self.recorder.active_camera_ids:
            raise ApplicationError("Camera is unavailable.")
        try:
            matches = tuple(
                source
                for source in self._recording_source_provider()
                if source.camera_id == camera_id
            )
            if len(matches) != 1:
                raise ApplicationError("Camera is unavailable.")
            reader = getattr(matches[0], "get_bytes", None)
            if not callable(reader):
                raise ApplicationError("Live image is unavailable.")
            frame = reader("/snapshot.jpg", maximum=8 * 1024 * 1024)
            if (
                not isinstance(frame, bytes)
                or len(frame) < 4
                or not frame.startswith(b"\xff\xd8")
                or not frame.endswith(b"\xff\xd9")
            ):
                raise ApplicationError("Live image is unavailable.")
        except ApplicationError:
            self.recorder.set_camera_connected(camera_id=camera_id, connected=False)
            raise
        except Exception as error:
            self.recorder.set_camera_connected(camera_id=camera_id, connected=False)
            raise ApplicationError("Live image is unavailable.") from error
        self.recorder.set_camera_connected(camera_id=camera_id, connected=True)
        return frame

    def diagnostic_history(self) -> Mapping[str, object]:
        from .monitoring import read_history
        try:
            return read_history(self.data_directory / "monitoring" / "health-history.json")
        except (OSError, ValueError, TypeError) as error:
            raise ApplicationError("Diagnostic history is unavailable.") from error

    def read_diagnostic_log(
        self,
        *,
        target: str,
        component: str,
        lines: int,
        camera_id: str | None = None,
    ) -> Mapping[str, object]:
        """Read a bounded, allowlisted journal tail for the owner diagnostics UI."""

        if target == "recorder":
            if camera_id is not None or component not in RECORDER_LOG_SOURCES:
                raise ApplicationError("Diagnostic log request is invalid.")
            try:
                completed = subprocess.run(
                    ["/usr/bin/sudo", "-n", "/usr/local/libexec/vms-journal-read"],
                    input=json.dumps(
                        {"source": component, "lines": lines},
                        separators=(",", ":"),
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                )
                if completed.returncode == 0:
                    record = json.loads(completed.stdout)
                    return _validated_diagnostic_log(record, "recorder", component, lines)
            except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError, ValueError):
                pass
            return unavailable_journal("recorder", component, lines)

        if target != "camera" or not isinstance(camera_id, str):
            raise ApplicationError("Diagnostic log request is invalid.")
        if camera_id not in self.recorder.active_camera_ids or component not in CAMERA_LOG_SOURCES:
            raise ApplicationError("Diagnostic log request is invalid.")
        try:
            sources = tuple(
                source
                for source in self._recording_source_provider()
                if source.camera_id == camera_id
            )
            if len(sources) != 1:
                return unavailable_journal("camera", component, lines)
            response = sources[0].get_json(
                "/diagnostics/logs?"
                + urlencode({"component": component, "lines": str(lines)})
            )
            return _validated_diagnostic_log(response, "camera", component, lines)
        except Exception:
            return unavailable_journal("camera", component, lines)

    def camera_recording_coverage(self, *, camera_id: str) -> Mapping[str, object]:
        """Check recent retained segments without claiming they are playable.

        Leave fifteen seconds for Frigate to finish and index its latest
        segment. This on-demand check is intentionally absent from the
        five-second live-telemetry poll.
        """

        if camera_id not in self.recorder.active_camera_ids:
            raise ApplicationError("Camera is unavailable.")
        engine = self.video_engine
        if not isinstance(engine, FrigateEngine) or camera_id not in engine.camera_bindings:
            return {"state": "unverified", "reason": "No supported recording engine."}
        before = int(datetime.now(UTC).timestamp()) - 15
        try:
            return engine.recording_coverage(
                camera_id=camera_id, after=before - 300, before=before
            )
        except (TypeError, ValueError, VideoEngineError):
            return {"state": "unverified", "reason": "Retained video could not be checked."}

    def camera_live_metrics(self, *, camera_id: str) -> Mapping[str, object]:
        """Return safe camera and recorder telemetry without network identities."""

        if camera_id not in self.recorder.active_camera_ids:
            raise ApplicationError("Camera is unavailable.")
        result: dict[str, object] = {}
        try:
            matches = tuple(
                source
                for source in self._recording_source_provider()
                if source.camera_id == camera_id
            )
            if len(matches) == 1:
                reader = getattr(matches[0], "get_json", None)
                if callable(reader):
                    try:
                        health = reader("/health")
                    except Exception:
                        result["camera_control_reachable"] = False
                        health = None
                    network = (
                        health.get("network") if isinstance(health, Mapping) else None
                    )
                    if (
                        isinstance(health, Mapping)
                        and health.get("device_id") == camera_id
                    ):
                        result["camera_control_reachable"] = True
                        if isinstance(network, Mapping):
                            rssi = network.get("rssi_dbm")
                            if (
                                isinstance(rssi, (int, float))
                                and not isinstance(rssi, bool)
                                and -150 <= rssi <= 0
                            ):
                                result["rssi_dbm"] = round(rssi)
                            signal = network.get("signal_percent")
                            if (
                                isinstance(signal, (int, float))
                                and not isinstance(signal, bool)
                                and 0 <= signal <= 100
                            ):
                                result["signal_percent"] = round(signal)
                        _, codec, source_fps = camera_media_profile(health)
                        result["source_codec"] = codec
                        result["source_fps"] = source_fps
                        result["recorder_transcoding"] = codec == "mjpeg"
        except Exception:
            pass
        if (
            isinstance(self.video_engine, FrigateEngine)
            and camera_id in self.video_engine.camera_bindings
        ):
            try:
                result.update(self.video_engine.live_metrics(camera_id=camera_id))
            except (TypeError, ValueError, VideoEngineError):
                pass
        # The on-demand camera check proves whether media reaches this recorder.
        # Never run that potentially expensive stream check on the 5-second
        # telemetry poll, and never treat an old successful check as current.
        report = self._camera_verifications.get(camera_id)
        if report is not None:
            try:
                checked_at = datetime.fromisoformat(report.checked_at)
                age = datetime.now(UTC) - checked_at
            except (TypeError, ValueError):
                age = None
            if (
                age is not None
                and checked_at.tzinfo is not None
                and -timedelta(seconds=5) <= age <= timedelta(minutes=2)
            ):
                stream_check = next(
                    (check for check in report.checks if check.name == "stream"),
                    None,
                )
                if stream_check is not None:
                    result["camera_media_state"] = (
                        "advancing" if stream_check.passed else "unavailable"
                    )
                    result["camera_media_checked_at"] = report.checked_at
        if result:
            detect_input_fps = result.get("detect_input_fps")
            if isinstance(detect_input_fps, (int, float)):
                result["detect_input_state"] = (
                    "stopped" if detect_input_fps == 0 else "input-present"
                )
            # Neither detect input nor a live snapshot proves that Frigate
            # retained a valid segment on the recording volume.
            result["recording_state"] = "unverified"
            stalls = result.get("source_stalls_last_hour")
            skipped = result.get("skipped_fps")
            rssi = result.get("rssi_dbm")
            recorder_cpu = result.get("recorder_cpu_percent")
            quality = result.get("connection_quality")
            findings: list[str] = []
            if not isinstance(detect_input_fps, (int, float)):
                findings.append("detect-input-unverified")
            if result.get("detect_input_state") == "stopped":
                findings.append("detect-input-stopped")
                if result.get("camera_media_state") == "advancing":
                    findings.append("recorder-detect-ingest-failed")
                elif result.get("camera_media_state") == "unavailable":
                    findings.append("camera-media-unavailable")
                elif result.get("camera_control_reachable") is False:
                    findings.append("camera-control-unreachable")
                elif result.get("camera_control_reachable") is True:
                    findings.append("camera-control-only-reachable")
            if isinstance(rssi, (int, float)) and rssi <= -75:
                findings.append("weak-camera-signal")
            if (
                (isinstance(stalls, (int, float)) and stalls > 0)
                or quality in {"poor", "unusable"}
            ):
                findings.append("source-interruptions")
            if isinstance(skipped, (int, float)) and skipped >= 0.5:
                findings.append("detection-not-keeping-up")
            if (
                isinstance(recorder_cpu, (int, float))
                and recorder_cpu >= 85
            ):
                findings.append("recorder-overloaded")
            if result.get("recorder_transcoding") is True:
                findings.append("legacy-transcoding")
            if not findings:
                findings.append("healthy")
            result["diagnoses"] = findings
            result["diagnosis"] = findings[0]
        return result

    def _active_recording_storage(self) -> RecordingStorage:
        storage_identity = self.recorder.initialized_storage_identity
        if storage_identity is None:
            raise ApplicationError("Recording storage is not ready.")
        matches = []
        for target in self.list_recording_storage_targets():
            attestation = target.attestation()
            if (
                attestation.storage_reference,
                attestation.volume_identity,
            ) == storage_identity:
                matches.append(attestation)
        if len(matches) != 1:
            raise ApplicationError("The initialized recording drive is unavailable.")
        try:
            return RecordingStorage(matches[0])
        except RecordingStorageError as error:
            raise ApplicationError(str(error)) from error

    def claim_camera(
        self, *, camera_id: str, claim_code: str, name: str
    ) -> Mapping[str, object]:
        return self.recorder.claim_camera(
            camera_id=camera_id,
            claim_secret_hash=_claim_hash(claim_code),
            name=name,
        )

    def claim_camera_payload(
        self, *, claim_payload: str, name: str
    ) -> Mapping[str, object]:
        """Claim a known camera from its scanned, one-time label."""

        try:
            claim = parse_claim_payload(claim_payload)
        except ClaimPayloadError as error:
            raise ApplicationError(str(error)) from error
        return self.claim_camera(
            camera_id=claim.device_id,
            claim_code=claim.claim_code,
            name=name,
        )

    def claim_camera_code(
        self, *, camera_code: str, name: str
    ) -> Mapping[str, object]:
        """Claim a known camera using the text printed below its QR code."""

        try:
            normalized = parse_camera_code(camera_code)
        except ClaimPayloadError as error:
            raise ApplicationError(str(error)) from error
        return self.recorder.claim_camera_code(
            claim_secret_hash=_claim_hash(normalized),
            name=name,
        )

    def begin_camera_network_setup(
        self,
        *,
        camera_id: str,
        bootstrap_identity: str,
        transport: str,
    ) -> CameraSetupBinding:
        """Bind one claimed camera to a reviewed network-enrollment adapter."""

        camera = next(
            (
                item
                for item in self.recorder.state["cameras"]
                if item["id"] == camera_id
                and not item["revoked"]
                and item["state"] in {"ready", "claimed"}
            ),
            None,
        )
        if camera is None:
            raise ApplicationError("Claim this camera before network setup.")
        if self.recorder_authorization is not None:
            recorder_id = self.recorder_authorization.recorder_id
        elif self._kit_state_path.is_file():
            recorder_id = self._load_active_kit().identity.recorder.device_id
        else:
            raise ApplicationError("Recorder identity is unavailable.")
        try:
            return self.camera_setup.begin(
                camera_id=camera_id,
                recorder_id=recorder_id,
                bootstrap_identity=bootstrap_identity,
                permanent_identity=str(camera["identity_reference"]),
                transport=transport,
            )
        except (TypeError, ValueError, CameraSetupError) as error:
            raise ApplicationError(str(error)) from error

    def accept_camera_network_enrollment(
        self, result: VerifiedCameraEnrollment
    ) -> Mapping[str, object]:
        """Accept only the secret-free verified result of a setup adapter."""

        try:
            return self.camera_setup.accept_enrollment(result)
        except (TypeError, ValueError, CameraSetupError) as error:
            raise ApplicationError(str(error)) from error

    def camera_setup_status(self, *, camera_id: str) -> Mapping[str, object] | None:
        try:
            return self.camera_setup.status(camera_id)
        except CameraSetupError as error:
            raise ApplicationError(str(error)) from error

    def retry_camera_network_setup(
        self, *, camera_id: str
    ) -> Mapping[str, object]:
        """Restart a failed setup without changing its bound identities."""

        try:
            return self.camera_setup.retry(camera_id)
        except CameraSetupError as error:
            raise ApplicationError(str(error)) from error

    def name_camera(self, *, camera_id: str, name: str) -> Mapping[str, object]:
        return self.recorder.name_camera(camera_id=camera_id, name=name)

    def replace_camera(
        self, *, camera_name: str, replacement_id: str, claim_code: str
    ) -> Mapping[str, object]:
        return self.recorder.replace_camera(
            camera_name=camera_name,
            replacement_id=replacement_id,
            claim_secret_hash=_claim_hash(claim_code),
        )

    def remove_camera(self, *, camera_name: str) -> Mapping[str, object]:
        return self.recorder.remove_camera(camera_name=camera_name)

    def set_recording(self, *, mode: str, days: int) -> Mapping[str, object]:
        return self.recorder.set_recording(mode=mode, days=days)

    def update(self) -> Mapping[str, object]:
        """Plan the newest catalogued, signed recorder update."""

        self.require_ose_doctor()
        candidates = [
            manifest
            for manifest in self.release_catalog.values()
            if manifest.role == "recorder"
            and manifest.channel == "product"
            and manifest.signature is not None
        ]
        if not candidates:
            raise ApplicationError("No verified recorder update is available.")
        selected = max(
            candidates, key=lambda item: (item.released, item.release_version)
        )
        selected.validate_for_use(
            role="recorder",
            board="pi4",
            trusted_release_keys=self.trusted_release_keys,
        )
        return self.recorder.plan_update(
            version=selected.release_version,
            release_reference=selected.image,
            signature_verified=True,
        )

    # Safety ------------------------------------------------------------

    def doctor(self, *, ose: bool = False) -> DoctorReport:
        policy_path = self.policy_path
        if policy_path is None:
            configured = os.environ.get(POLICY_ENVIRONMENT_VARIABLE)
            if configured:
                policy_path = Path(configured).expanduser().resolve()
            else:
                conventional = Path.home() / ".vms" / "doctor-policy.json"
                policy_path = conventional.resolve() if conventional.is_file() else None
        if ose and policy_path is None:
            raise SafetyCheckFailed(
                f"OSE policy is not configured; set {POLICY_ENVIRONMENT_VARIABLE}."
            )
        return self._doctor_runner(
            self.repository,
            policy_path=policy_path,
            provisioning_tool_paths=(
                "src/video_monitoring_system/application.py",
                "src/video_monitoring_system/cli.py",
            ),
        )

    def require_ose_doctor(self) -> DoctorReport:
        report = self.doctor(ose=True)
        if not report.passed:
            raise SafetyCheckFailed(
                "Safety checks failed. Run 'vms dev doctor --ose' for details."
            )
        return report

    # Factory provisioning ---------------------------------------------

    def prepare_provisioning(
        self,
        request: ProvisioningConfig,
        *,
        wifi_passphrase: str | None = None,
        flash: bool = False,
        target: DiskTarget | None = None,
        confirmation: str | None = None,
        allow_development: bool = False,
        rotate_network: bool = False,
        pilot_recovery: bool | None = None,
    ) -> Mapping[str, object]:
        """Dispatch one validated setup request to the appropriate workflow."""

        if not isinstance(request, ProvisioningConfig):
            raise ApplicationError("A validated provisioning request is required.")
        country = request.network.country
        if country is None:
            raise ApplicationError("Select the Wi-Fi country before provisioning.")

        if request.mode == "kit":
            if (
                request.camera_count is None
                or wifi_passphrase is not None
                or flash
                or target is not None
                or confirmation is not None
                or allow_development
                or rotate_network
                or pilot_recovery is not None
            ):
                raise ApplicationError("Kit provisioning options are invalid.")
            return self.create_kit(
                camera_count=request.camera_count,
                camera_model=request.camera_model,
                wifi_country=country,
            )

        if request.camera_number is None or request.network.ssid is None:
            raise ApplicationError("Camera network setup is incomplete.")
        if not isinstance(wifi_passphrase, str) or not wifi_passphrase:
            raise ApplicationError("Camera network credentials are required.")
        return self.provision_camera(
            model=request.camera_model,
            number=request.camera_number,
            wifi_ssid=request.network.ssid,
            wifi_passphrase=wifi_passphrase,
            wifi_country=country,
            flash=flash,
            target=target,
            confirmation=confirmation,
            allow_development=allow_development,
            rotate_network=rotate_network,
            pilot_recovery=pilot_recovery,
        )

    def provision_selected_camera(
        self,
        request: ProvisioningConfig,
        *,
        wifi_passphrase: str,
        stable_id: str,
        confirmation: str,
        allow_development: bool = False,
        rotate_network: bool = False,
        pilot_recovery: bool | None = None,
    ) -> Mapping[str, object]:
        """Re-resolve one selected card, then provision one camera onto it."""

        if not isinstance(request, ProvisioningConfig) or request.mode != "camera":
            raise ApplicationError("Select one validated camera setup request.")
        matches = [
            target
            for target in self.list_media_targets()
            if target.stable_id == stable_id
        ]
        if len(matches) != 1:
            raise ApplicationError("The selected card is no longer available.")
        return self.prepare_provisioning(
            request,
            wifi_passphrase=wifi_passphrase,
            flash=True,
            target=matches[0],
            confirmation=confirmation,
            allow_development=allow_development,
            rotate_network=rotate_network,
            pilot_recovery=pilot_recovery,
        )

    @property
    def _kit_state_path(self) -> Path:
        return self.data_directory / "provisioning" / "active-kit.json"

    @property
    def _kit_progress_path(self) -> Path:
        return self.data_directory / "provisioning" / "active-kit-progress.json"

    def create_kit(
        self, *, camera_count: int, camera_model: str, wifi_country: str
    ) -> Mapping[str, object]:
        self.require_ose_doctor()
        spec = ProvisioningSpec(
            camera_count=camera_count,
            camera_board=camera_model,
            camera_image=self._camera_image_for_state(self._kit_state_path),
        )
        job = ProvisioningJob.open_or_create(
            self._kit_state_path,
            spec,
            wifi_country=wifi_country,
        )
        output_directory = self.data_directory / "kits" / job.identity.kit_id
        secure_write_text(
            output_directory / "manifest.json", job.public_manifest().to_json()
        )
        labels = output_directory / "labels"
        for camera in job.identity.cameras:
            self._label_writer(camera, labels)
            self._ensure_pilot_recovery_key(camera)
        self._label_index_writer(job.identity.cameras, labels)
        if not self._kit_progress_path.exists():
            self._write_progress(0)
        self._register_job_cameras(job)
        return {
            "result": "ready",
            "kit_id": job.identity.kit_id,
            "camera_count": len(job.identity.cameras),
            "camera_model": camera_model,
            "labels": str(labels),
            "pilot_diagnostics": list(self.pilot_diagnostics()),
        }

    def flash_next(
        self,
        *,
        target: DiskTarget | None = None,
        confirmation: str | None = None,
        dry_run: bool = False,
        allow_development: bool = False,
    ) -> Mapping[str, object]:
        self.require_ose_doctor()
        job = self._load_active_kit()
        index = self._read_progress()
        configurations = job.device_configurations()
        if index >= len(configurations):
            return {"result": "complete", "remaining": 0}
        configuration = configurations[index]
        device = self._job_device(job, configuration.device_id)
        plan_or_result = self._write_configuration(
            configuration=configuration,
            device=device,
            target=target,
            confirmation=confirmation,
            dry_run=dry_run,
            allow_development=allow_development,
        )
        if not dry_run:
            self._write_progress(index + 1)
        return {
            "result": "preview" if dry_run else "flashed",
            "device_id": device.device_id,
            "role": device.role,
            "camera_number": device.camera_number,
            "remaining": len(configurations) - index - (0 if dry_run else 1),
            "image": _public_imaging_result(plan_or_result),
        }

    def verify_kit(self) -> Mapping[str, object]:
        self.require_ose_doctor()
        job = self._load_active_kit()
        reports = tuple(self._camera_verifier(camera) for camera in job.identity.cameras)
        return {
            "passed": all(report.passed for report in reports),
            "cameras": [report.to_dict() for report in reports],
        }

    def kit_flash_status(self) -> Mapping[str, object]:
        job = self._load_active_kit()
        total = 1 + len(job.identity.cameras)
        completed = min(self._read_progress(), total)
        return {"completed": completed, "total": total, "remaining": total - completed}

    # Single-camera pilot ----------------------------------------------

    def provision_camera(
        self,
        *,
        model: str,
        number: int,
        wifi_ssid: str,
        wifi_passphrase: str,
        wifi_country: str,
        flash: bool = False,
        target: DiskTarget | None = None,
        confirmation: str | None = None,
        allow_development: bool = False,
        rotate_network: bool = False,
        pilot_recovery: bool | None = None,
    ) -> Mapping[str, object]:
        """Prepare or flash one camera for a pilot on an existing network.

        Provisioning state may contain earlier camera numbers so that the
        selected camera keeps the requested stable one-based number.  Only the
        selected camera is exposed or written by this operation.
        """

        self.require_ose_doctor()
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 1 <= number <= SOFTWARE_CAMERA_LIMIT
        ):
            raise ApplicationError(
                f"Camera number must be between 1 and {SOFTWARE_CAMERA_LIMIT}."
            )
        get_board_profile(model, "camera")
        state_path = self._pilot_state_path(model, number)
        spec = ProvisioningSpec(
            camera_count=number,
            camera_board=model,
            camera_image=self._camera_image_for_state(state_path),
        )
        if state_path.is_file():
            saved = self._load_pilot_job(state_path, model=model, number=number)
            # Reflashing one camera must not migrate the unrelated recorder
            # image when a newer application changes its default release.
            spec = replace(spec, recorder_image=saved.spec.recorder_image)
        job = ProvisioningJob.open_or_create(
            state_path,
            spec,
            wifi_ssid=wifi_ssid,
            wifi_passphrase=wifi_passphrase,
            wifi_country=wifi_country,
            rotate_network=rotate_network,
            pilot_recovery=pilot_recovery,
        )
        camera = job.identity.cameras[number - 1]
        diagnostics = self._pilot_recovery_metadata(camera, state_path)
        if diagnostics is not None:
            self._ensure_pilot_recovery_key(camera)
        configuration = next(
            item
            for item in job.device_configurations()
            if item.device_id == camera.device_id
        )
        labels = self.data_directory / "pilots" / "labels"
        label = self._label_writer(camera, labels)
        self._register_camera(camera)
        plan_or_result = self._write_configuration(
            configuration=configuration,
            device=camera,
            target=target,
            confirmation=confirmation,
            dry_run=not flash,
            allow_development=(
                allow_development
                or get_board_profile(model).release_tier == "pilot"
            ),
        )
        return {
            "result": "flashed" if flash else "preview",
            "device_id": camera.device_id,
            "camera_number": camera.camera_number,
            "model": model,
            "pilot_recovery": camera.pilot_recovery_secret is not None,
            "diagnostics": diagnostics,
            "label": str(label),
            "image": _public_imaging_result(plan_or_result),
        }

    def pilot_camera_diagnostics(
        self, *, model: str, number: int
    ) -> Mapping[str, object]:
        """Return recovery endpoints and private file locations, never secrets."""

        self.require_ose_doctor()
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 1 <= number <= SOFTWARE_CAMERA_LIMIT
        ):
            raise ApplicationError(
                f"Camera number must be between 1 and {SOFTWARE_CAMERA_LIMIT}."
            )
        get_board_profile(model, "camera")
        state_path = self._pilot_state_path(model, number)
        job = self._load_pilot_job(state_path, model=model, number=number)
        camera = job.identity.cameras[number - 1]
        metadata = self._pilot_recovery_metadata(camera, state_path)
        if metadata is None:
            raise ApplicationError("Pilot recovery is not enabled for this camera.")
        self._ensure_pilot_recovery_key(camera)
        return metadata

    def stage_pilot_camera(
        self,
        *,
        model: str,
        number: int,
        output_directory: str | os.PathLike[str],
    ) -> Mapping[str, object]:
        """Export one owner-private payload for an external imaging adapter.

        The payload is written as a new directory and never replaces an
        existing path.  This keeps a failed or repeated operator action from
        silently changing the identity assigned to media already in flight.
        """

        self.require_ose_doctor()
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 1 <= number <= SOFTWARE_CAMERA_LIMIT
        ):
            raise ApplicationError(
                f"Camera number must be between 1 and {SOFTWARE_CAMERA_LIMIT}."
            )
        get_board_profile(model, "camera")
        state_path = self._pilot_state_path(model, number)
        job = self._load_pilot_job(state_path, model=model, number=number)
        camera = job.identity.cameras[number - 1]
        configuration = next(
            item
            for item in job.device_configurations()
            if item.device_id == camera.device_id
        )
        release = self._release_for(
            configuration.image_reference,
            role="camera",
            board=model,
        )

        destination = Path(output_directory).expanduser().resolve()
        if _is_within(destination, self.repository):
            raise ApplicationError(
                "Private camera configuration must be staged outside the source repository."
            )
        if destination.exists():
            raise ApplicationError("The staging directory already exists.")

        instance_id = f"rpi-imager-{int(time.time() * 1000):013d}"
        meta_data = f"instance-id: {instance_id}\n"
        boot_customization = json.dumps(
            asdict(configuration.boot_customization)
            if configuration.boot_customization is not None
            else None,
            indent=2,
            sort_keys=True,
        ) + "\n"
        state_content = state_path.read_text(encoding="utf-8")
        private_documents = {
            "user-data": configuration.user_data,
            "network-config": configuration.network_config,
            "meta-data": meta_data,
            "boot-customization.json": boot_customization,
        }
        metadata = {
            "schema_version": 2,
            "device_id": camera.device_id,
            "camera_number": number,
            "model": model,
            "dhcp_client_id": camera_dhcp_client_id(camera),
            "image_reference": configuration.image_reference,
            "release_version": release.release_version,
            "release_image_sha256": release.sha256,
            "instance_id": instance_id,
            "state_sha256": _sha256_text(state_content),
            "user_data_sha256": _sha256_text(configuration.user_data),
            "network_config_sha256": _sha256_text(configuration.network_config),
            "meta_data_sha256": _sha256_text(meta_data),
            "boot_customization_sha256": _sha256_text(boot_customization),
        }
        private_documents["metadata.json"] = (
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
        )
        os.chmod(temporary, 0o700)
        try:
            for name, content in private_documents.items():
                secure_write_text(temporary / name, content)
            for name, content in private_documents.items():
                if _sha256_text((temporary / name).read_text(encoding="utf-8")) != _sha256_text(content):
                    raise ApplicationError("The staged camera configuration did not verify.")
            destination.mkdir(mode=0o700)
            os.chmod(destination, 0o700)
            for name in private_documents:
                os.replace(temporary / name, destination / name)
            temporary.rmdir()
        except FileExistsError as error:
            shutil.rmtree(temporary, ignore_errors=True)
            raise ApplicationError("The staging directory already exists.") from error
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return {
            "device_id": camera.device_id,
            "camera_number": number,
            "model": model,
            "directory": str(destination),
            "verified": True,
        }

    def migrate_pilot_camera_release(
        self,
        *,
        model: str,
        number: int,
        image_reference: str = PREINSTALLED_CAMERA_IMAGE_REFERENCE,
    ) -> Mapping[str, object]:
        """Move persisted pilot identities to an explicitly selected release.

        Migration changes only the shared camera image reference. Device and
        kit identities, keys, claims, recovery access, and network credentials
        remain byte-for-byte identical values.
        """

        self.require_ose_doctor()
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 1 <= number <= SOFTWARE_CAMERA_LIMIT
        ):
            raise ApplicationError(
                f"Camera number must be between 1 and {SOFTWARE_CAMERA_LIMIT}."
            )
        get_board_profile(model, "camera")
        manifest = self.release_catalog.get(image_reference)
        if manifest is None:
            raise ApplicationError("The selected camera release does not exist.")
        try:
            manifest.validate_for_use(
                role="camera",
                board=model,
                allow_development=True,
                trusted_release_keys=self.trusted_release_keys,
            )
        except (TypeError, ValueError) as error:
            raise ApplicationError(
                "The selected camera release does not support this camera."
            ) from error

        state_path = self._pilot_state_path(model, number)
        job = self._load_pilot_job(state_path, model=model, number=number)
        before = _pilot_identity_invariants(job)
        if (
            job.spec.camera_image == image_reference
            and all(
                camera.image_reference == image_reference
                for camera in job.identity.cameras
            )
        ):
            return {
                "result": "already_current",
                "camera_number": number,
                "model": model,
                "image_reference": image_reference,
            }

        migrated = ProvisioningJob(
            spec=replace(job.spec, camera_image=image_reference),
            identity=replace(
                job.identity,
                cameras=tuple(
                    replace(camera, image_reference=image_reference)
                    for camera in job.identity.cameras
                ),
            ),
        )
        if _pilot_identity_invariants(migrated) != before:
            raise ApplicationError("Camera identity changed during release migration.")
        content = migrated.to_private_json()
        try:
            verified = ProvisioningJob.from_json(content)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ApplicationError("The migrated camera state did not verify.") from error
        if (
            verified.spec.camera_image != image_reference
            or any(
                camera.image_reference != image_reference
                for camera in verified.identity.cameras
            )
            or _pilot_identity_invariants(verified) != before
        ):
            raise ApplicationError("The migrated camera state did not verify.")
        secure_write_text(state_path, content)
        return {
            "result": "migrated",
            "camera_number": number,
            "model": model,
            "image_reference": image_reference,
        }

    def pilot_diagnostics(self) -> Sequence[Mapping[str, object]]:
        """Return factory-GUI diagnostics for the active pilot kit."""

        if not self._kit_state_path.is_file():
            return ()
        try:
            job = self._load_active_kit()
        except ApplicationError:
            return ()
        items = []
        for camera in job.identity.cameras:
            metadata = self._pilot_recovery_metadata(camera, self._kit_state_path)
            if metadata is not None:
                items.append(metadata)
        return tuple(items)

    def verify_pilot_camera(
        self,
        *,
        model: str,
        number: int,
        hostname: str | None = None,
        port: int = 9080,
    ) -> Mapping[str, object]:
        """Verify a previously provisioned camera without exposing its secrets."""

        self.require_ose_doctor()
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 1 <= number <= SOFTWARE_CAMERA_LIMIT
        ):
            raise ApplicationError(
                f"Camera number must be between 1 and {SOFTWARE_CAMERA_LIMIT}."
            )
        get_board_profile(model, "camera")
        state_path = self._pilot_state_path(model, number)
        job = self._load_pilot_job(state_path, model=model, number=number)
        report = self._camera_verifier(
            job.identity.cameras[number - 1],
            hostname=hostname,
            port=port,
        )
        return report.to_dict()

    # Media and GUI adapters -------------------------------------------

    def list_media_targets(self) -> tuple[DiskTarget, ...]:
        try:
            targets = self.disk_discovery.list_disks()
        except DiskDiscoveryError as error:
            raise ApplicationError(
                "Removable cards could not be inspected safely."
            ) from error
        return tuple(target for target in targets if is_supported_system_card(target))

    def flash_selected(
        self,
        *,
        stable_id: str,
        confirmation: str,
        allow_development: bool = False,
    ) -> Mapping[str, object]:
        """Resolve a GUI card choice again, then perform the normal safe write."""

        matches = [
            target
            for target in self.list_media_targets()
            if target.stable_id == stable_id
        ]
        if len(matches) != 1:
            raise ApplicationError("The selected card is no longer available.")
        target = matches[0]
        if confirmation != target.confirmation:
            raise ApplicationError("Card erase was not confirmed.")
        return self.flash_next(
            target=target,
            confirmation=confirmation,
            dry_run=False,
            allow_development=allow_development,
        )

    def create_gui_server(
        self, *, port: int = 0, factory: bool = False, local_network: bool = False
    ):
        return create_server(
            self,
            host="0.0.0.0" if local_network else "127.0.0.1",
            port=port,
            factory_enabled=factory,
            wifi_country_default=self.wifi_country_default,
        )

    # Internal helpers --------------------------------------------------

    def _pilot_state_path(self, model: str, number: int) -> Path:
        return self.data_directory / "pilots" / f"camera-{number:03d}-{model}.json"

    def _camera_image_for_state(self, state_path: Path) -> str:
        """Keep existing pilot identities stable, preferring a built image for new state."""

        if state_path.is_file():
            try:
                return ProvisioningJob.from_json(
                    state_path.read_text(encoding="utf-8")
                ).spec.camera_image
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ):
                # The normal open path reports the complete persisted-state error.
                pass
        if PREINSTALLED_CAMERA_IMAGE_REFERENCE in self.release_catalog:
            return PREINSTALLED_CAMERA_IMAGE_REFERENCE
        return "raspios-32-vms-camera"

    @staticmethod
    def _load_pilot_job(
        state_path: Path, *, model: str, number: int
    ) -> ProvisioningJob:
        if not state_path.is_file():
            raise ApplicationError("Provision this camera before continuing.")
        try:
            job = ProvisioningJob.from_json(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ApplicationError("The camera provisioning state could not be read.") from error
        if job.spec.camera_board != model or len(job.identity.cameras) < number:
            raise ApplicationError("The camera provisioning state does not match.")
        return job

    def _pilot_recovery_key_path(self, camera: DeviceIdentity) -> Path:
        return self.data_directory / "pilot-recovery" / f"{camera.device_id}.key"

    def _ensure_pilot_recovery_key(self, camera: DeviceIdentity) -> Path | None:
        if camera.pilot_recovery_secret is None:
            return None
        path = self._pilot_recovery_key_path(camera)
        try:
            secure_write_text(path, pilot_recovery_ssh_private_key(camera))
        except (OSError, RuntimeError, ValueError) as error:
            raise ApplicationError("The pilot recovery key could not be prepared.") from error
        return path

    def _pilot_recovery_metadata(
        self, camera: DeviceIdentity, credentials_path: Path
    ) -> Mapping[str, object] | None:
        if camera.pilot_recovery_secret is None or camera.camera_number is None:
            return None
        hostname = f"camera-{camera.camera_number:03d}.local"
        return {
            "device_id": camera.device_id,
            "camera_number": camera.camera_number,
            "username": PILOT_RECOVERY_USERNAME,
            "credentials_file": str(credentials_path),
            "ssh_key_file": str(self._pilot_recovery_key_path(camera)),
            "private_network_ssh": hostname,
            "usb_serial": "USB data port; serial console",
            "gpio_uart": "GPIO pin 8 TX and pin 6 GND; 115200 8-N-1",
            "early_boot": (
                "active from first power-on"
                if self._boot_customizer is not None
                else "activates during first boot, followed by one recovery restart"
            ),
            "diagnostic_command": "sudo vms-pilot-diagnostics",
            "persistent_log": "/var/log/journal",
        }

    def _load_active_kit(self) -> ProvisioningJob:
        if not self._kit_state_path.is_file():
            raise ApplicationError("Create a kit before continuing.")
        try:
            return ProvisioningJob.from_json(
                self._kit_state_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ApplicationError("The active kit state could not be read.") from error

    def _write_progress(self, completed: int) -> None:
        secure_write_text(
            self._kit_progress_path,
            json.dumps({"schema_version": 1, "completed": completed}) + "\n",
        )

    def _read_progress(self) -> int:
        if not self._kit_progress_path.exists():
            return 0
        try:
            value = json.loads(self._kit_progress_path.read_text(encoding="utf-8"))
            completed = value["completed"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise ApplicationError("Kit progress could not be read.") from error
        if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
            raise ApplicationError("Kit progress is invalid.")
        return completed

    @staticmethod
    def _job_device(job: ProvisioningJob, device_id: str) -> DeviceIdentity:
        devices = (job.identity.recorder, *job.identity.cameras)
        return next(device for device in devices if device.device_id == device_id)

    def _release_for(
        self, image_reference: str, *, role: str, board: str
    ) -> ImageReleaseManifest:
        exact = self.release_catalog.get(image_reference)
        if exact is not None:
            return exact
        candidates = [
            manifest
            for manifest in self.release_catalog.values()
            if manifest.role == role and board in manifest.supported_boards
        ]
        if not candidates:
            raise ApplicationError(
                f"No {role} image release supports the selected board."
            )
        return max(candidates, key=lambda item: item.release_version)

    def _write_configuration(
        self,
        *,
        configuration: DeviceConfiguration,
        device: DeviceIdentity,
        target: DiskTarget | None,
        confirmation: str | None,
        dry_run: bool,
        allow_development: bool,
    ) -> ImagingPlan | ImagingResult:
        if target is None:
            if not dry_run:
                raise ApplicationError("Select a removable card before flashing.")
            target = DiskTarget(
                device="selected-card",
                stable_id="INSERTED-CARD",
                display_name="Selected removable card",
                size_bytes=1,
                removable=True,
                system_disk=False,
            )
        manifest = self._release_for(
            configuration.image_reference,
            role=device.role,
            board=device.board,
        )
        return self._image_writer(
            manifest=manifest,
            role=device.role,
            board=device.board,
            target=target,
            user_data=configuration.user_data,
            network_config=configuration.network_config,
            boot_customization=configuration.boot_customization,
            dry_run=dry_run,
            confirmation=confirmation,
            allow_development=allow_development,
            trusted_release_keys=self.trusted_release_keys,
            disk_discovery=self.disk_discovery,
            which=self._imager_which,
            runner=self._imager_runner,
            boot_customizer=self._boot_customizer,
        )

    def _register_job_cameras(self, job: ProvisioningJob) -> None:
        for camera in job.identity.cameras:
            self._register_camera(camera, pre_enrolled=True)

    def _register_camera(
        self, camera: DeviceIdentity, *, pre_enrolled: bool = False
    ) -> None:
        if not camera.claim_secret:
            raise ApplicationError("Camera claim material is unavailable.")
        existing = next(
            (
                item
                for item in self.recorder.state["cameras"]
                if item["id"] == camera.device_id
            ),
            None,
        )
        expected_identity = f"identity:{camera.key.public_identity}"
        if (
            existing is not None
            and not existing["revoked"]
            and existing["identity_reference"] == expected_identity
        ):
            return
        self.recorder.register_camera(
            camera_id=camera.device_id,
            claim_secret_hash=_claim_hash(camera.claim_secret),
            identity_reference=expected_identity,
            pre_enrolled=pre_enrolled,
        )

    def _camera_stream_secret(self, camera_id: str) -> str | None:
        """Return one active camera secret only to an in-process private adapter."""

        if camera_id not in self.recorder.active_camera_ids:
            return None
        connections = self._private_camera_connections()
        imported = self._load_imported_camera_source_details()
        if set(imported) - self.recorder.active_camera_ids:
            raise ApplicationError(
                "Imported camera connection state is not authorized."
            )
        if set(imported) & set(connections):
            raise ApplicationError("Private camera connection state conflicts.")
        connection = connections.get(camera_id)
        if connection is not None:
            return connection[1]
        imported_connection = imported.get(camera_id)
        return None if imported_connection is None else imported_connection[2]

    def _camera_setup_observe_heartbeat(self, camera_id: str) -> None:
        try:
            self.camera_setup.observe_heartbeat(camera_id)
        except CameraSetupError as error:
            raise ApplicationError(str(error)) from error

    def _camera_setup_mark_ready(self, camera_id: str) -> None:
        try:
            self.camera_setup.mark_ready(camera_id)
        except CameraSetupError as error:
            raise ApplicationError(str(error)) from error

    def _camera_setup_attention(self, camera_id: str, *, issue: str) -> None:
        try:
            self.camera_setup.mark_attention(camera_id, issue=issue)
        except (ValueError, CameraSetupError) as error:
            raise ApplicationError(str(error)) from error

    def _private_camera_connections(self) -> dict[str, tuple[int, str]]:
        """Load camera number and stream secret from owner-private state."""

        cameras: dict[str, DeviceIdentity] = {}
        if self._kit_state_path.is_file():
            job = self._load_active_kit()
            cameras.update(
                (camera.device_id, camera) for camera in job.identity.cameras
            )
        pilot_name = re.compile(r"camera-(?P<number>[0-9]{3})-[a-z0-9-]+\.json")
        for path in sorted((self.data_directory / "pilots").glob("camera-*.json")):
            match = pilot_name.fullmatch(path.name)
            if match is None or path.is_symlink():
                continue
            number = int(match.group("number"))
            if not 1 <= number <= SOFTWARE_CAMERA_LIMIT:
                continue
            try:
                job = ProvisioningJob.from_json(path.read_text(encoding="utf-8"))
                camera = job.identity.cameras[number - 1]
            except (OSError, UnicodeError, json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError) as error:
                raise ApplicationError(
                    "Private camera connection state could not be read."
                ) from error
            existing = cameras.get(camera.device_id)
            if existing is not None and existing != camera:
                raise ApplicationError("Private camera connection state conflicts.")
            cameras[camera.device_id] = camera

        connections: dict[str, tuple[int, str]] = {}
        assigned_numbers: dict[int, str] = {}
        for camera_id, camera in cameras.items():
            if camera.camera_number is None or camera.stream_secret is None:
                continue
            owner = assigned_numbers.get(camera.camera_number)
            if owner is not None and owner != camera.device_id:
                raise ApplicationError("Private camera connection state conflicts.")
            assigned_numbers[camera.camera_number] = camera.device_id
            connections[camera_id] = (camera.camera_number, camera.stream_secret)
        if self.recorder_authorization is not None:
            for camera in self.recorder_authorization.cameras:
                value = (camera.camera_number, camera.stream_secret)
                existing = connections.get(camera.device_id)
                if existing is not None and existing != value:
                    raise ApplicationError("Private camera connection state conflicts.")
                owner = assigned_numbers.get(camera.camera_number)
                if owner is not None and owner != camera.device_id:
                    raise ApplicationError("Private camera connection state conflicts.")
                assigned_numbers[camera.camera_number] = camera.device_id
                connections[camera.device_id] = value
        return connections

    def _private_recording_sources(self) -> tuple[MjpegFrameSource, ...]:
        """Load authenticated routes, preferring camera-announced LAN addresses."""

        sources: dict[str, MjpegFrameSource] = {}
        connections = self._private_camera_connections()
        for camera_id in sorted(self.recorder.active_camera_ids):
            connection = connections.get(camera_id)
            if connection is None:
                continue
            camera_number, stream_secret = connection
            endpoint = self.camera_endpoints.endpoint_for(camera_id)
            host = (
                endpoint.ip_address
                if endpoint is not None
                else f"camera-{camera_number:03d}.local"
            )
            port = endpoint.port if endpoint is not None else 9080
            sources[camera_id] = HttpCameraTransport(
                host,
                camera_id,
                stream_secret,
                port=port,
                timeout=3,
            )
        imported = self._load_imported_camera_sources()
        authorized = self.recorder.active_camera_ids
        if set(imported) - authorized:
            raise ApplicationError(
                "Imported camera connection state is not authorized."
            )
        if set(imported) & set(sources):
            raise ApplicationError("Private camera connection state conflicts.")
        sources.update(imported)
        return tuple(sources[camera_id] for camera_id in sorted(sources))

    def _load_imported_camera_sources(self) -> dict[str, MjpegFrameSource]:
        """Load an owner-private authenticated migration adapter."""

        return {
            camera_id: HttpCameraTransport(
                endpoint.ip_address if endpoint is not None else hostname,
                camera_id,
                stream_secret,
                port=endpoint.port if endpoint is not None else port,
                timeout=3,
            )
            for camera_id, (hostname, port, stream_secret) in (
                self._load_imported_camera_source_details().items()
            )
            for endpoint in (self.camera_endpoints.endpoint_for(camera_id),)
        }

    def _load_imported_camera_source_details(
        self,
    ) -> dict[str, tuple[str, int, str]]:
        """Validate private legacy routes without exposing their credentials."""

        path = self.data_directory / _IMPORTED_CAMERA_SOURCES_FILE
        if not path.exists():
            return {}
        try:
            details = path.stat()
            if path.is_symlink() or not path.is_file():
                raise ValueError
            if os.name == "posix" and (
                details.st_mode & 0o077
                or details.st_uid != os.geteuid()
            ):
                raise ValueError
            document = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or set(document) != {"schema_version", "cameras"}
                or document["schema_version"] != 1
                or not isinstance(document["cameras"], list)
                or not 1 <= len(document["cameras"]) <= SOFTWARE_CAMERA_LIMIT
            ):
                raise ValueError
            sources: dict[str, tuple[str, int, str]] = {}
            for camera in document["cameras"]:
                if (
                    not isinstance(camera, dict)
                    or set(camera)
                    != {"camera_id", "hostname", "port", "stream_secret"}
                ):
                    raise ValueError
                camera_id = camera["camera_id"]
                hostname = camera["hostname"]
                port = camera["port"]
                stream_secret = camera["stream_secret"]
                if (
                    not isinstance(camera_id, str)
                    or re.fullmatch(r"CAM-[0-9A-F]{16,64}", camera_id) is None
                    or camera_id in sources
                    or not isinstance(hostname, str)
                    or not hostname
                    or not isinstance(stream_secret, str)
                    or not 16 <= len(stream_secret) <= 1024
                ):
                    raise ValueError
                # Construction performs the same strict hostname, port, and
                # timeout validation used by the live transport.  Do not keep
                # this temporary object: the returned tuple is consumed only
                # by in-process adapters and must never be logged or rendered.
                HttpCameraTransport(
                    hostname, camera_id, stream_secret, port=port, timeout=3
                )
                sources[camera_id] = (hostname, port, stream_secret)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ApplicationError(
                "Imported camera connection state could not be read."
            ) from None
        return sources

    def _backup_state_files(self) -> dict[str, Path]:
        """Select durable identity/configuration state, excluding derived media."""

        candidates = [
            self.data_directory / "recorder-state.json",
            self.data_directory / "camera-endpoints.json",
            self.data_directory / _IMPORTED_CAMERA_SOURCES_FILE,
            self.data_directory / _CAMERA_VERIFICATIONS_FILE,
            self.data_directory / REMOTE_STATE_FILE,
            self.data_directory / GATEWAY_TUNNEL_FILE,
            self.data_directory / GATEWAY_KEY_FILE,
            self.data_directory / GATEWAY_KNOWN_HOSTS_FILE,
            self.data_directory / "push-notifications.json",
            self.data_directory / VIDEO_ENGINE_CONFIG_FILE,
            self.data_directory / "video-engine-policy.json",
            self.data_directory / "video-engine-alerts.json",
            self.data_directory / "monitoring" / "health-history.json",
        ]
        candidates.extend(sorted((self.data_directory / "provisioning").glob("*.json")))
        candidates.extend(sorted((self.data_directory / "pilots").glob("*.json")))
        candidates.extend(sorted((self.data_directory / "pilot-recovery").glob("*.key")))
        candidates.extend(sorted((self.data_directory / "kits").glob("*/manifest.json")))
        selected = {}
        for path in candidates:
            if not path.exists():
                continue
            try:
                relative = path.relative_to(self.data_directory).as_posix()
            except ValueError as error:
                raise ApplicationError(
                    "Recorder state could not be selected for backup."
                ) from error
            selected[relative] = path
        return selected

    def _load_camera_verifications(self) -> dict[str, VerificationReport]:
        path = self.data_directory / _CAMERA_VERIFICATIONS_FILE
        if not path.exists():
            return {}
        try:
            details = path.stat()
            if path.is_symlink() or not path.is_file():
                raise ValueError
            if os.name == "posix" and (
                details.st_mode & 0o077 or details.st_uid != os.geteuid()
            ):
                raise ValueError
            document = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or set(document) != {"schema_version", "cameras"}
                or document["schema_version"] != 1
                or not isinstance(document["cameras"], list)
                or len(document["cameras"]) > SOFTWARE_CAMERA_LIMIT
            ):
                raise ValueError
            reports: dict[str, VerificationReport] = {}
            for value in document["cameras"]:
                if (
                    not isinstance(value, dict)
                    or set(value)
                    != {
                        "schema_version",
                        "device_id",
                        "checked_at",
                        "passed",
                        "checks",
                    }
                    or value["schema_version"] != 1
                    or not isinstance(value["device_id"], str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value["device_id"])
                    is None
                    or not isinstance(value["checked_at"], str)
                    or not 1 <= len(value["checked_at"]) <= 64
                    or not isinstance(value["passed"], bool)
                    or not isinstance(value["checks"], list)
                    or not 1 <= len(value["checks"]) <= 20
                ):
                    raise ValueError
                checks = []
                for check in value["checks"]:
                    if (
                        not isinstance(check, dict)
                        or set(check) != {"name", "passed", "message"}
                        or not isinstance(check["name"], str)
                        or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", check["name"])
                        is None
                        or not isinstance(check["passed"], bool)
                        or not isinstance(check["message"], str)
                        or not 1 <= len(check["message"]) <= 200
                    ):
                        raise ValueError
                    checks.append(
                        CheckResult(
                            check["name"], check["passed"], check["message"]
                        )
                    )
                report = VerificationReport(
                    schema_version=1,
                    device_id=value["device_id"],
                    checked_at=value["checked_at"],
                    checks=tuple(checks),
                )
                if report.passed != value["passed"] or report.device_id in reports:
                    raise ValueError
                reports[report.device_id] = report
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ApplicationError("Saved camera checks could not be read.") from None
        return reports

    def _save_camera_verifications(self) -> None:
        secure_write_text(
            self.data_directory / _CAMERA_VERIFICATIONS_FILE,
            json.dumps(
                {
                    "schema_version": 1,
                    "cameras": [
                        self._camera_verifications[camera_id].to_dict()
                        for camera_id in sorted(self._camera_verifications)
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return "hash:scrypt$16384$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def _password_matches(password: str, reference: str) -> bool:
    try:
        prefix, n_value, r_value, p_value, salt_value, digest_value = reference.split("$")
        if prefix != "hash:scrypt":
            return False
        salt = _urlsafe_decode(salt_value)
        expected = _urlsafe_decode(digest_value)
        n = int(n_value)
        r = int(r_value)
        p = int(p_value)
        if (n, r, p, len(salt), len(expected)) != (16384, 8, 1, 16, 32):
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def _urlsafe_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _require_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < 12:
        raise ApplicationError("Owner password must contain at least 12 characters.")
    if len(password) > 1024:
        raise ApplicationError("Owner password is too long.")


def _claim_hash(claim_code: str) -> str:
    if not isinstance(claim_code, str) or not claim_code:
        raise ApplicationError("Claim code is required.")
    if len(claim_code) > 1024:
        raise ApplicationError("Claim code is too long.")
    return hashlib.sha256(claim_code.encode("utf-8")).hexdigest()


def _verify_camera_route_media(
    transport: HttpCameraTransport, health: Mapping[str, object]
) -> None:
    """Prove every declared media role before a candidate route is committed."""

    profiles = camera_media_routes(health)
    if set(profiles) != {"main", "detection"}:
        raise ValueError("camera media roles are incomplete")
    verified: set[tuple[str, str, int]] = set()
    for profile in profiles.values():
        if profile in verified:
            continue
        path, codec, _ = profile
        if codec == "h264":
            transport.get_stream_prefix(
                path,
                minimum=1024,
                maximum=1024 * 1024,
                timeout_seconds=4.0,
            )
        elif codec == "mjpeg":
            frames = transport.get_mjpeg_frames(
                path,
                maximum=4 * 1024 * 1024,
                count=2,
                timeout_seconds=4.0,
            )
            if len(frames) != 2 or frames[0] == frames[1]:
                raise ValueError("camera media stream is not advancing")
        else:
            raise ValueError("camera media codec is unsupported")
        verified.add(profile)


def _validated_diagnostic_log(
    value: object, role: str, component: str, lines: int
) -> Mapping[str, object]:
    """Reject malformed or oversized device log responses before rendering."""

    catalog = RECORDER_LOG_SOURCES if role == "recorder" else CAMERA_LOG_SOURCES
    required = {
        "schema_version",
        "state",
        "source",
        "label",
        "captured_at",
        "lines_requested",
        "lines_returned",
        "truncated",
        "text",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("diagnostic log response is invalid")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("state") not in {"available", "unavailable"}
        or value.get("source") != component
        or value.get("label") != catalog[component][0]
        or value.get("lines_requested") != lines
        or type(value.get("lines_returned")) is not int
        or not 0 <= value["lines_returned"] <= lines
        or type(value.get("truncated")) is not bool
        or not isinstance(value.get("captured_at"), str)
        or len(value["captured_at"]) > 80
        or not isinstance(value.get("text"), str)
    ):
        raise ValueError("diagnostic log response is invalid")
    record = dict(value)
    record["text"] = redact_log_text(value["text"])
    if len(record["text"].encode("utf-8")) > 64 * 1024:
        raise ValueError("diagnostic log response is oversized")
    return record


def _camera_route_claims_identity(
    endpoint: ObservedCameraEndpoint,
    *,
    camera_id: str,
    stream_secret: str,
) -> bool:
    """Return true only when the committed route is live with the same identity."""

    current = HttpCameraTransport(
        endpoint.ip_address,
        camera_id,
        stream_secret,
        port=endpoint.port,
        timeout=1.5,
    )
    try:
        health = current.get_json("/health")
    except Exception:
        return False
    return (
        health.get("device_id") == camera_id
        and health.get("claimed") is True
        and health.get("camera_ready") is True
    )


def _public_imaging_result(result: ImagingPlan | ImagingResult) -> dict[str, object]:
    plan = result.plan if isinstance(result, ImagingResult) else result
    return {
        "release_version": plan.release_version,
        "role": plan.role,
        "board": plan.board,
        "channel": plan.channel,
        "target": plan.target_display_name,
        "size_bytes": plan.target_size_bytes,
        "required_confirmation": plan.required_confirmation,
        "verified": True,
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pilot_identity_invariants(job: ProvisioningJob) -> tuple[object, ...]:
    kit = job.identity
    spec = job.spec
    cameras = tuple(
        (
            camera.device_id,
            camera.kit_id,
            camera.role,
            camera.board,
            camera.key,
            camera.claim_secret,
            camera.stream_secret,
            camera.camera_number,
            camera.pilot_recovery_secret,
        )
        for camera in kit.cameras
    )
    return (
        spec.camera_count,
        spec.camera_board,
        spec.recorder_board,
        spec.recorder_image,
        spec.camera_wifi_band,
        spec.camera_wifi_security,
        kit.kit_id,
        kit.wifi_ssid,
        kit.wifi_passphrase,
        kit.wifi_country,
        kit.recorder,
        cameras,
    )


__all__ = [
    "ApplicationError",
    "SafetyCheckFailed",
    "VMSApplication",
    "default_data_directory",
    "load_release_catalog",
]
