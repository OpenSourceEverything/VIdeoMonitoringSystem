import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
import yaml

from video_monitoring_system.application import (
    ApplicationError,
    VMSApplication,
    load_release_catalog,
)
from video_monitoring_system.configuration import build_provisioning_config
from video_monitoring_system.camera_endpoints import (
    AuthenticatedCameraObservation,
    ObservedCameraEndpoint,
)
from video_monitoring_system.camera_route_handoff import (
    TransactionalCameraRouteHandoff,
)
from video_monitoring_system.camera_setup import VerifiedCameraEnrollment
from video_monitoring_system.doctor import DoctorReport
from video_monitoring_system.imaging import (
    DiskTarget,
    ImageReleaseManifest,
    ImagingPlan,
)
from video_monitoring_system.provisioning import (
    PREINSTALLED_CAMERA_IMAGE_REFERENCE,
    ProvisioningJob,
    ProvisioningSpec,
)
from video_monitoring_system.recording import RecordingVolumeAttestation
from video_monitoring_system.storage_adapter import RecordingStorageTarget
from video_monitoring_system.verification import CheckResult, VerificationReport
from video_monitoring_system.video_engine import (
    EngineEvent,
    FrigateEngine,
    VideoEngineError,
    save_frigate_engine_configuration,
)


def development_camera_release() -> ImageReleaseManifest:
    return ImageReleaseManifest(
        schema_version=1,
        release_version="camera-pilot-1",
        role="camera",
        supported_boards=("zero-w", "zero-2-w"),
        image="https://example.invalid/camera.img.xz",
        sha256="a" * 64,
        signature=None,
        channel="development",
        architecture="armhf",
        bits=32,
        released="2026-08-14",
        extract_sha256="b" * 64,
    )


def recorder_authorization(*, stream_secret: str = "test-camera-stream-secret") -> dict:
    return {
        "schema_version": 1,
        "device_id": "REC-0123456789ABCDEF",
        "role": "recorder",
        "authorized_cameras": [
            {
                "device_id": "CAM-0123456789ABCDEF",
                "camera_number": 2,
                "identity": {
                    "algorithm": "ed25519",
                    "public": "test-public-camera-identity",
                },
                "claim_secret_hash": "a" * 64,
                "stream_secret": stream_secret,
            }
        ],
    }


class Harness:
    def __init__(self, root: Path) -> None:
        self.events = []
        self.image_arguments = []
        self.root = root

    def doctor(self, repository, **kwargs):
        self.events.append(("doctor", kwargs))
        return DoctorReport(str(repository), ())

    def image(self, **kwargs):
        self.events.append(("image", kwargs["dry_run"]))
        self.image_arguments.append(kwargs)
        target = kwargs["target"]
        manifest = kwargs["manifest"]
        return ImagingPlan(
            release_version=manifest.release_version,
            role=kwargs["role"],
            board=kwargs["board"],
            channel=manifest.channel,
            image=manifest.image,
            sha256=manifest.sha256,
            target_device=target.device,
            target_display_name=target.display_name,
            target_size_bytes=target.size_bytes,
            required_confirmation=target.confirmation,
            argv=("rpi-imager", "<owner-private-user-data>"),
        )

    def label(self, camera, output_directory):
        self.events.append(("label", camera.device_id))
        output_directory.mkdir(parents=True, exist_ok=True)
        path = output_directory / f"{camera.device_id}.svg"
        path.write_text("label", encoding="utf-8")
        return path

    def label_index(self, cameras, output_directory):
        self.events.append(("label-index", len(cameras)))
        output_directory.mkdir(parents=True, exist_ok=True)
        path = output_directory / "claim-labels.json"
        path.write_text("{}", encoding="utf-8")
        return path

    def verifier(self, camera, **kwargs):
        self.events.append(("verify-camera", kwargs))
        return VerificationReport(
            schema_version=1,
            device_id=camera.device_id,
            checked_at="2026-08-14T00:00:00+00:00",
            checks=(CheckResult("connection", True, "Camera responded."),),
        )

    def application(self, **options) -> VMSApplication:
        repository = self.root / "source"
        data = self.root / "private"
        repository.mkdir()
        return VMSApplication(
            data,
            repository=repository,
            policy_path=self.root / "doctor-policy.json",
            release_catalog={"camera": development_camera_release()},
            doctor_runner=self.doctor,
            image_writer=self.image,
            camera_verifier=self.verifier,
            label_writer=self.label,
            label_index_writer=self.label_index,
            **options,
        )


class ApplicationTests(unittest.TestCase):
    def test_scanned_claim_payload_claims_a_known_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = Harness(root).application()
            claim_code = "abcdefghijklmnopqrstuvwxyz012345"
            camera_id = "CAM-0123456789ABCDEF"
            application.recorder.register_camera(
                camera_id=camera_id,
                claim_secret_hash=hashlib.sha256(claim_code.encode()).hexdigest(),
                identity_reference="identity:test-camera-public-key",
            )

            result = application.claim_camera_payload(
                claim_payload=(
                    "vms://claim?kit=KIT-0123456789AB&"
                    f"device={camera_id}&number=1&code={claim_code}"
                ),
                name="Driveway",
            )

            self.assertEqual(result["result"], "claimed")
            self.assertEqual(application.list_cameras()[0]["name"], "Driveway")

    def test_printed_camera_code_claims_a_known_camera_without_device_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = Harness(root).application()
            camera_code = "ABCDEF0123456789ABCDEF0123456789"
            application.recorder.register_camera(
                camera_id="CAM-0123456789ABCDEF",
                claim_secret_hash=hashlib.sha256(camera_code.encode()).hexdigest(),
                identity_reference="identity:test-camera-public-key",
            )

            result = application.claim_camera_code(
                camera_code="ABCD EF01 2345 6789 ABCD EF01 2345 6789",
                name="Driveway",
            )

            self.assertEqual(result["result"], "claimed")
            self.assertEqual(application.list_cameras()[0]["name"], "Driveway")

    def test_missing_recorder_authorization_keeps_generic_recorder_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = Harness(root).application(
                recorder_authorization_path=root / "missing-device.json"
            )

            self.assertEqual(
                application.status()["cameras"],
                {"expected": 0, "connected": 0, "pending": 0},
            )

    def test_recorder_authorization_pre_enrolls_camera_and_supplies_its_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)

            application = Harness(root).application(
                recorder_authorization_path=authorization
            )
            sources = tuple(application._recording_source_provider())

            self.assertEqual(
                application.status()["cameras"],
                {"expected": 1, "connected": 0, "pending": 1},
            )
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].camera_id, "CAM-0123456789ABCDEF")
            self.assertEqual(sources[0].base_url, "http://camera-002.local:9080")
            self.assertNotIn("test-camera-stream-secret", repr(sources))

    def test_authenticated_heartbeat_becomes_the_current_camera_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            application = Harness(root).application(
                recorder_authorization_path=authorization
            )
            payload = (
                b'{"camera_id":"CAM-0123456789ABCDEF","port":9080,'
                b'"schema_version":1}'
            )
            signature = hmac.new(
                b"test-camera-stream-secret", payload, hashlib.sha256
            ).hexdigest()

            with (
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_json",
                    return_value={
                        "schema_version": 3,
                        "device_id": "CAM-0123456789ABCDEF",
                        "claimed": True,
                        "camera_ready": True,
                    },
                ),
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_mjpeg_frames",
                    return_value=(b"frame-one", b"frame-two"),
                ),
            ):
                result = application.register_camera_endpoint(
                    payload=payload,
                    signature=signature,
                    source_address="192.168.0.184",
                )

            sources = tuple(application._recording_source_provider())
            self.assertEqual(result["result"], "registered")
            self.assertEqual(sources[0].base_url, "http://192.168.0.184:9080")
            self.assertTrue(application.list_cameras()[0]["connected"])

            with self.assertRaisesRegex(ApplicationError, "unauthorized"):
                application.register_camera_endpoint(
                    payload=payload,
                    signature="0" * 64,
                    source_address="192.168.0.185",
                )

    def test_verified_network_setup_advances_through_heartbeat_and_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            application = Harness(root).application(
                recorder_authorization_path=authorization
            )
            binding = application.begin_camera_network_setup(
                camera_id="CAM-0123456789ABCDEF",
                bootstrap_identity="sha256:" + "b" * 64,
                transport="dpp",
            )
            waiting = application.accept_camera_network_enrollment(
                VerifiedCameraEnrollment(
                    transaction_id=binding.transaction_id,
                    camera_id=binding.camera_id,
                    recorder_id=binding.recorder_id,
                    bootstrap_identity=binding.bootstrap_identity,
                    permanent_identity=binding.permanent_identity,
                    transport=binding.transport,
                )
            )
            payload = (
                b'{"camera_id":"CAM-0123456789ABCDEF","port":9080,'
                b'"schema_version":1}'
            )
            signature = hmac.new(
                b"test-camera-stream-secret", payload, hashlib.sha256
            ).hexdigest()

            with (
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_json",
                    return_value={
                        "schema_version": 3,
                        "device_id": binding.camera_id,
                        "claimed": True,
                        "camera_ready": True,
                    },
                ),
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_mjpeg_frames",
                    return_value=(b"frame-one", b"frame-two"),
                ),
            ):
                application.register_camera_endpoint(
                    payload=payload,
                    signature=signature,
                    source_address="192.168.0.184",
                )

            self.assertEqual(waiting["state"], "waiting-heartbeat")
            self.assertEqual(
                application.camera_setup_status(camera_id=binding.camera_id)["state"],
                "ready",
            )

    def test_camera_setup_rejects_heartbeat_before_verified_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            application = Harness(root).application(
                recorder_authorization_path=authorization
            )
            application.begin_camera_network_setup(
                camera_id="CAM-0123456789ABCDEF",
                bootstrap_identity="sha256:" + "b" * 64,
                transport="dpp",
            )
            payload = (
                b'{"camera_id":"CAM-0123456789ABCDEF","port":9080,'
                b'"schema_version":1}'
            )
            signature = hmac.new(
                b"test-camera-stream-secret", payload, hashlib.sha256
            ).hexdigest()

            with self.assertRaisesRegex(ApplicationError, "out of order"):
                application.register_camera_endpoint(
                    payload=payload,
                    signature=signature,
                    source_address="192.168.0.184",
                )

            self.assertIsNone(
                application.camera_endpoints.endpoint_for(
                    "CAM-0123456789ABCDEF"
                )
            )

    def test_failed_camera_setup_can_be_retried_without_rebinding_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            application = Harness(root).application(
                recorder_authorization_path=authorization
            )
            binding = application.begin_camera_network_setup(
                camera_id="CAM-0123456789ABCDEF",
                bootstrap_identity="sha256:" + "b" * 64,
                transport="dpp",
            )
            application.camera_setup.mark_attention(
                binding.camera_id,
                issue="network-timeout",
            )

            retried = application.retry_camera_network_setup(
                camera_id=binding.camera_id
            )

            self.assertEqual(retried["state"], "waiting-network")
            self.assertIsNone(retried["issue"])
            self.assertEqual(
                application.camera_setup.binding(binding.transaction_id),
                binding,
            )

    def test_failed_route_media_preserves_the_last_working_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            application = Harness(root).application(
                recorder_authorization_path=authorization
            )
            from video_monitoring_system.camera_endpoints import (
                AuthenticatedCameraObservation,
                ObservedCameraEndpoint,
            )

            previous = ObservedCameraEndpoint("10.42.0.82", 9080)
            application.camera_endpoints.reconcile(
                authorized_camera_ids={"CAM-0123456789ABCDEF"},
                observations=[
                    AuthenticatedCameraObservation(
                        "CAM-0123456789ABCDEF", previous
                    )
                ],
            )
            payload = (
                b'{"camera_id":"CAM-0123456789ABCDEF","port":9080,'
                b'"schema_version":1}'
            )
            signature = hmac.new(
                b"test-camera-stream-secret", payload, hashlib.sha256
            ).hexdigest()

            with (
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_json",
                    return_value={
                        "schema_version": 3,
                        "device_id": "CAM-0123456789ABCDEF",
                        "claimed": True,
                        "camera_ready": True,
                    },
                ),
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_mjpeg_frames",
                    side_effect=TimeoutError("no advancing frames"),
                ),
                patch(
                    "video_monitoring_system.application._camera_route_claims_identity",
                    return_value=False,
                ),
                self.assertRaisesRegex(ApplicationError, "media could not be verified"),
            ):
                application.register_camera_endpoint(
                    payload=payload,
                    signature=signature,
                    source_address="192.168.0.184",
                )

            self.assertEqual(
                application.camera_endpoints.endpoint_for(
                    "CAM-0123456789ABCDEF"
                ),
                previous,
            )
            self.assertIsNone(
                application.camera_endpoints.candidate_for(
                    "CAM-0123456789ABCDEF"
                )
            )

    def test_second_live_route_is_quarantined_as_duplicate_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            application = Harness(root).application(
                recorder_authorization_path=authorization
            )
            previous = ObservedCameraEndpoint("192.168.0.82", 9080)
            duplicate = ObservedCameraEndpoint("192.168.0.184", 9080)
            application.camera_endpoints.reconcile(
                authorized_camera_ids={"CAM-0123456789ABCDEF"},
                observations=[
                    AuthenticatedCameraObservation(
                        "CAM-0123456789ABCDEF", previous
                    )
                ],
            )
            payload = (
                b'{"camera_id":"CAM-0123456789ABCDEF","port":9080,'
                b'"schema_version":1}'
            )
            signature = hmac.new(
                b"test-camera-stream-secret", payload, hashlib.sha256
            ).hexdigest()

            with (
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_json",
                    return_value={
                        "schema_version": 3,
                        "device_id": "CAM-0123456789ABCDEF",
                        "claimed": True,
                        "camera_ready": True,
                    },
                ),
                patch(
                    "video_monitoring_system.application._camera_route_claims_identity",
                    return_value=True,
                ),
                self.assertRaisesRegex(ApplicationError, "Duplicate camera identity"),
            ):
                application.register_camera_endpoint(
                    payload=payload,
                    signature=signature,
                    source_address=duplicate.ip_address,
                )

            self.assertEqual(
                application.camera_endpoints.endpoint_for(
                    "CAM-0123456789ABCDEF"
                ),
                previous,
            )
            self.assertEqual(
                application.camera_endpoints.conflict_for(
                    "CAM-0123456789ABCDEF"
                ),
                duplicate,
            )
            self.assertEqual(
                application.list_cameras()[0]["network_issue"],
                "duplicate-identity",
            )
            with patch(
                "video_monitoring_system.application._camera_route_claims_identity",
                return_value=False,
            ):
                self.assertEqual(
                    application.resolve_camera_identity_conflict(
                        camera_id="CAM-0123456789ABCDEF"
                    )["result"],
                    "resolved",
                )
            self.assertIsNone(
                application.camera_endpoints.conflict_for(
                    "CAM-0123456789ABCDEF"
                )
            )

    def test_duplicate_identity_cannot_be_cleared_while_extra_camera_is_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            application = Harness(root).application(
                recorder_authorization_path=authorization
            )
            camera_id = "CAM-0123456789ABCDEF"
            application.camera_endpoints.reconcile(
                authorized_camera_ids={camera_id},
                observations=[
                    AuthenticatedCameraObservation(
                        camera_id, ObservedCameraEndpoint("10.42.0.82", 9080)
                    )
                ],
            )
            duplicate = ObservedCameraEndpoint("10.42.0.83", 9080)
            application.camera_endpoints.quarantine(
                camera_id=camera_id,
                endpoint=duplicate,
            )

            with (
                patch(
                    "video_monitoring_system.application._camera_route_claims_identity",
                    return_value=True,
                ),
                self.assertRaisesRegex(ApplicationError, "still online"),
            ):
                application.resolve_camera_identity_conflict(camera_id=camera_id)

            self.assertEqual(
                application.camera_endpoints.conflict_for(camera_id), duplicate
            )

    def test_bound_engine_refuses_route_promotion_without_downstream_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            application = Harness(root).application(
                recorder_authorization_path=authorization,
                video_engine=FrigateEngine(
                    {"CAM-0123456789ABCDEF": "driveway"}
                ),
            )
            previous = ObservedCameraEndpoint("10.42.0.82", 9080)
            application.camera_endpoints.reconcile(
                authorized_camera_ids={"CAM-0123456789ABCDEF"},
                observations=[
                    AuthenticatedCameraObservation(
                        "CAM-0123456789ABCDEF", previous
                    )
                ],
            )
            payload = (
                b'{"camera_id":"CAM-0123456789ABCDEF","port":9080,'
                b'"schema_version":1}'
            )
            signature = hmac.new(
                b"test-camera-stream-secret", payload, hashlib.sha256
            ).hexdigest()

            with (
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_json",
                    return_value={
                        "schema_version": 3,
                        "device_id": "CAM-0123456789ABCDEF",
                        "claimed": True,
                        "camera_ready": True,
                    },
                ),
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_mjpeg_frames",
                    return_value=(b"frame-one", b"frame-two"),
                ),
                patch(
                    "video_monitoring_system.application._camera_route_claims_identity",
                    return_value=False,
                ),
                self.assertRaisesRegex(ApplicationError, "handoff is unavailable"),
            ):
                application.register_camera_endpoint(
                    payload=payload,
                    signature=signature,
                    source_address="192.168.0.184",
                )

            self.assertEqual(
                application.camera_endpoints.endpoint_for(
                    "CAM-0123456789ABCDEF"
                ),
                previous,
            )
            self.assertIsNone(
                application.camera_endpoints.candidate_for(
                    "CAM-0123456789ABCDEF"
                )
            )

    def test_bound_engine_commits_only_after_transactional_downstream_proof(self):
        class RouteAdapter:
            def __init__(self):
                self.calls = []

            def _call(self, name, **values):
                self.calls.append((name, values))

            def stage(self, **values):
                self._call("stage", **values)
                return "stage-handle"

            def verify_staged(self, **values):
                self._call("verify_staged", **values)

            def switch(self, **values):
                self._call("switch", **values)

            def verify_active(self, **values):
                self._call("verify_active", **values)

            def restore(self, **values):
                self._call("restore", **values)

            def commit(self, **values):
                self._call("commit", **values)

            def discard(self, **values):
                self._call("discard", **values)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            adapter = RouteAdapter()
            application = Harness(root).application(
                recorder_authorization_path=authorization,
                video_engine=FrigateEngine(
                    {"CAM-0123456789ABCDEF": "driveway"}
                ),
                camera_route_handoff=TransactionalCameraRouteHandoff(adapter),
            )
            previous = ObservedCameraEndpoint("10.42.0.82", 9080)
            candidate = ObservedCameraEndpoint("192.168.0.184", 9080)
            application.camera_endpoints.reconcile(
                authorized_camera_ids={"CAM-0123456789ABCDEF"},
                observations=[
                    AuthenticatedCameraObservation(
                        "CAM-0123456789ABCDEF", previous
                    )
                ],
            )
            payload = (
                b'{"camera_id":"CAM-0123456789ABCDEF","port":9080,'
                b'"schema_version":1}'
            )
            signature = hmac.new(
                b"test-camera-stream-secret", payload, hashlib.sha256
            ).hexdigest()

            with (
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_json",
                    return_value={
                        "schema_version": 3,
                        "device_id": "CAM-0123456789ABCDEF",
                        "claimed": True,
                        "camera_ready": True,
                    },
                ),
                patch(
                    "video_monitoring_system.application.HttpCameraTransport.get_mjpeg_frames",
                    return_value=(b"frame-one", b"frame-two"),
                ),
                patch(
                    "video_monitoring_system.application._camera_route_claims_identity",
                    return_value=False,
                ),
            ):
                result = application.register_camera_endpoint(
                    payload=payload,
                    signature=signature,
                    source_address=candidate.ip_address,
                )

            self.assertEqual(result["result"], "updated")
            self.assertEqual(
                application.camera_endpoints.endpoint_for(
                    "CAM-0123456789ABCDEF"
                ),
                candidate,
            )
            self.assertEqual(
                [name for name, _ in adapter.calls],
                [
                    "stage",
                    "verify_staged",
                    "switch",
                    "verify_active",
                    "commit",
                    "discard",
                ],
            )

    def test_reconciles_private_frigate_files_from_authorized_camera_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "private"
            data.mkdir()
            save_frigate_engine_configuration(
                data,
                {"CAM-0123456789ABCDEF": "back_porch"},
                port=15000,
            )
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            application = Harness(root).application(
                recorder_authorization_path=authorization
            )

            with (
                patch(
                    "video_monitoring_system.verification.socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("10.42.0.200", 9080))],
                ),
                patch(
                    "video_monitoring_system.verification.HttpCameraTransport.get_json",
                    return_value={
                        "schema_version": 3,
                        "device_id": "CAM-0123456789ABCDEF",
                    },
                ),
            ):
                result = application.reconcile_video_engine_configuration(
                    state_directory=data / "frigate",
                    candidates=("192.168.0.108:8555",),
                )
                config_path = data / "frigate/config/config.yml"
                self.assertTrue(
                    yaml.safe_load(config_path.read_text())["cameras"]["back_porch"]
                    ["record"]["enabled"]
                )
                application.set_camera_recording(
                    camera_id="CAM-0123456789ABCDEF", enabled=False
                )
                application.reconcile_video_engine_configuration(
                    state_directory=data / "frigate",
                    candidates=("192.168.0.108:8555",),
                )
                self.assertFalse(
                    yaml.safe_load(config_path.read_text())["cameras"]["back_porch"]
                    ["record"]["enabled"]
                )
                application.set_camera_recording(
                    camera_id="CAM-0123456789ABCDEF", enabled=True
                )
                application.reconcile_video_engine_configuration(
                    state_directory=data / "frigate",
                    candidates=("192.168.0.108:8555",),
                )
                self.assertTrue(
                    yaml.safe_load(config_path.read_text())["cameras"]["back_porch"]
                    ["record"]["enabled"]
                )

            config = (data / "frigate/config/config.yml").read_text(encoding="utf-8")
            environment = (data / "frigate/environment").read_text(encoding="utf-8")
            self.assertEqual(result, {"state": "ready", "changed": True, "cameras": 1})
            self.assertIn("back_porch", config)
            self.assertNotIn("test-camera-stream-secret", config)
            self.assertIn("test-camera-stream-secret", environment)
            self.assertIn("@10.42.0.200:9080/stream.mjpg", environment)

    def test_reconciles_native_h264_capability_without_transcoding(self):
        class Source:
            camera_id = "CAM-0123456789ABCDEF"

            def get_json(self, path):
                self.health_path = path
                return {
                    "schema_version": 4,
                    "device_id": self.camera_id,
                    "media": {
                        "live": {
                            "codec": "h264",
                            "path": "/stream.h264",
                            "width": 1920,
                            "height": 1080,
                            "fps": 15,
                            "bitrate": 3_000_000,
                        }
                    },
                }

            def authenticated_url(self, path, *, resolve):
                self.stream_call = (path, resolve)
                return f"http://CAM-0123456789ABCDEF:secret@10.42.0.9:9080{path}"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "private"
            data.mkdir()
            save_frigate_engine_configuration(
                data,
                {"CAM-0123456789ABCDEF": "bench"},
                port=15000,
            )
            source = Source()
            application = Harness(root).application(
                recording_source_provider=lambda: (source,)
            )

            result = application.reconcile_video_engine_configuration(
                state_directory=data / "frigate",
                candidates=("192.168.0.108:8555",),
            )

            config = (data / "frigate/config/config.yml").read_text(encoding="utf-8")
            environment = (data / "frigate/environment").read_text(encoding="utf-8")
            self.assertEqual(result["cameras"], 1)
            self.assertEqual(source.health_path, "/health")
            self.assertEqual(source.stream_call, ("/stream.h264", True))
            self.assertNotIn("libx264", config)
            self.assertIn(
                "bench: '{FRIGATE_OSE_CAMERA_F1C54BE68592E567_URL}'",
                config,
            )
            self.assertIn("/stream.h264", environment)

    def test_route_relay_configuration_removes_camera_credentials_from_engine(self):
        class Source:
            camera_id = "CAM-0123456789ABCDEF"

            def get_json(self, path):
                self.health_path = path
                return {
                    "schema_version": 4,
                    "device_id": self.camera_id,
                    "media": {
                        "live": {
                            "codec": "h264",
                            "path": "/stream.h264",
                            "width": 1920,
                            "height": 1080,
                            "fps": 15,
                            "bitrate": 3_000_000,
                        }
                    },
                }

            def authenticated_url(self, path, *, resolve):
                raise AssertionError("relay configuration must not request a secret URL")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "private"
            data.mkdir()
            save_frigate_engine_configuration(
                data,
                {"CAM-0123456789ABCDEF": "bench"},
                port=15000,
                route_relay_enabled=True,
            )
            application = Harness(root).application(
                recording_source_provider=lambda: (Source(),)
            )

            result = application.reconcile_video_engine_configuration(
                state_directory=data / "frigate",
                candidates=("192.168.0.108:8555",),
            )

            environment = (data / "frigate/environment").read_text(encoding="utf-8")
            self.assertEqual(result["cameras"], 1)
            self.assertIn("ose-camera-route-relay.internal:9081", environment)
            self.assertNotIn("test-camera-stream-secret", environment)
            self.assertIsNotNone(application.camera_route_relay)
            self.assertIsNotNone(application.camera_route_handoff)

    def test_engine_reconciliation_refuses_missing_bound_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "private"
            data.mkdir()
            save_frigate_engine_configuration(
                data,
                {"CAM-FFFFFFFFFFFFFFFF": "missing_camera"},
                port=15000,
            )
            application = Harness(root).application()

            with self.assertRaisesRegex(ApplicationError, "source is unavailable"):
                application.reconcile_video_engine_configuration(
                    state_directory=data / "frigate",
                    candidates=("192.168.0.108:8555",),
                )

    def test_recorder_authorization_preserves_claimed_owner_state_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            harness = Harness(root)
            app = harness.application(recorder_authorization_path=authorization)
            app.recorder.name_camera(
                camera_id="CAM-0123456789ABCDEF", name="Driveway"
            )

            restarted = VMSApplication(
                app.data_directory,
                repository=app.repository,
                recorder_authorization_path=authorization,
            )

            camera = restarted.recorder.state["cameras"][0]
            self.assertEqual(camera["name"], "Driveway")
            self.assertEqual(camera["state"], "claimed")

    def test_recorder_authorization_does_not_revive_owner_revoked_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization()), encoding="utf-8"
            )
            authorization.chmod(0o600)
            harness = Harness(root)
            app = harness.application(recorder_authorization_path=authorization)
            app.recorder.name_camera(
                camera_id="CAM-0123456789ABCDEF", name="Driveway"
            )
            app.recorder.remove_camera(camera_name="Driveway")

            restarted = VMSApplication(
                app.data_directory,
                repository=app.repository,
                recorder_authorization_path=authorization,
            )

            camera = restarted.recorder.state["cameras"][0]
            self.assertTrue(camera["revoked"])
            self.assertEqual(camera["state"], "revoked")

    def test_recorder_authorization_rejects_public_permissions_without_leaking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "test-placeholder-camera-stream-secret"
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(recorder_authorization(stream_secret=secret)),
                encoding="utf-8",
            )
            authorization.chmod(0o644)

            with self.assertRaises(ApplicationError) as raised:
                Harness(root).application(
                    recorder_authorization_path=authorization
                )

            self.assertNotIn(secret, str(raised.exception))

    def test_runtime_camera_checks_use_authenticated_source_and_update_health(self):
        class Source:
            camera_id = "CAM-RUNTIME"

            def get_json(self, path):
                self.health_path = path
                return {
                    "schema_version": 3,
                    "system_ready": True,
                    "device_id": self.camera_id,
                    "camera_ready": True,
                    "power": "throttled=0x0",
                    "claimed": True,
                    "provisioning_mode": False,
                    "network": {
                        "state": "healthy",
                        "failed_layer": "none",
                        "recorder_reachable": True,
                    },
                }

            def get_bytes(self, path, *, maximum):
                self.snapshot_call = (path, maximum)
                return b"\xff\xd8snapshot\xff\xd9"

            def get_mjpeg_frames(
                self, path, *, maximum, count, timeout_seconds
            ):
                self.stream_call = (path, maximum, count, timeout_seconds)
                return (
                    b"\xff\xd8first\xff\xd9",
                    b"\xff\xd8second\xff\xd9",
                )

        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            application = Harness(Path(directory)).application(
                recording_source_provider=lambda: (source,)
            )
            application.recorder.register_camera(
                camera_id=source.camera_id,
                claim_secret_hash="0" * 64,
                identity_reference=f"identity:{source.camera_id}",
                pre_enrolled=True,
            )

            report = application.verify_recorder_camera(camera_id=source.camera_id)

            self.assertTrue(report.passed)
            self.assertEqual(source.health_path, "/health")
            self.assertEqual(source.snapshot_call[0], "/snapshot.jpg")
            self.assertEqual(source.stream_call[0], "/stream.mjpg")
            camera = application.list_cameras()[0]
            self.assertTrue(camera["connected"])
            self.assertIs(camera["verification"], report)
            checks_path = application.data_directory / "camera-verifications.json"
            self.assertEqual(checks_path.stat().st_mode & 0o777, 0o600)

            reloaded = VMSApplication(
                application.data_directory,
                repository=application.repository,
                recording_source_provider=lambda: (source,),
            )
            saved_report = reloaded.list_cameras()[0]["verification"]
            self.assertEqual(saved_report.to_dict(), report.to_dict())

    def test_live_metrics_return_only_bounded_signal_values(self):
        class Source:
            camera_id = "CAM-METRICS"

            def __init__(self):
                self.network = {"rssi_dbm": -85.2, "signal_percent": 36.4}

            def get_json(self, path):
                self.health_path = path
                return {
                    "device_id": self.camera_id,
                    "network": dict(self.network),
                }

        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            application = Harness(Path(directory)).application(
                recording_source_provider=lambda: (source,)
            )
            application.recorder.register_camera(
                camera_id=source.camera_id,
                claim_secret_hash="0" * 64,
                identity_reference=f"identity:{source.camera_id}",
                pre_enrolled=True,
            )

            self.assertEqual(
                application.camera_live_metrics(camera_id=source.camera_id),
                {
                    "rssi_dbm": -85,
                    "signal_percent": 36,
                    "camera_control_reachable": True,
                    "recording_state": "unverified",
                    "diagnoses": ["detect-input-unverified", "weak-camera-signal"],
                    "diagnosis": "detect-input-unverified",
                },
            )
            self.assertEqual(source.health_path, "/health")

            source.network = {
                "rssi_dbm": -999,
                "signal_percent": 200,
                "ssid": "owner-private-network",
            }
            self.assertEqual(
                application.camera_live_metrics(camera_id=source.camera_id),
                {
                    "camera_control_reachable": True,
                    "recording_state": "unverified",
                    "diagnoses": ["detect-input-unverified"],
                    "diagnosis": "detect-input-unverified",
                },
            )

    def test_zero_detect_input_fps_does_not_claim_recording_or_weak_wifi(self):
        class Source:
            camera_id = "CAM-METRICS"

            def get_json(self, path):
                return {"device_id": self.camera_id}

        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application(
                recording_source_provider=lambda: (Source(),)
            )
            application.recorder.register_camera(
                camera_id=Source.camera_id,
                claim_secret_hash="0" * 64,
                identity_reference="identity:CAM-METRICS",
                pre_enrolled=True,
            )
            engine = FrigateEngine({Source.camera_id: "front_door"})
            engine.live_metrics = lambda **kwargs: {
                "detect_input_fps": 0.0,
                "source_reconnects_last_hour": 9,
            }
            application.video_engine = engine

            metrics = application.camera_live_metrics(camera_id=Source.camera_id)

            self.assertEqual(metrics["recording_state"], "unverified")
            self.assertEqual(metrics["detect_input_state"], "stopped")
            self.assertEqual(metrics["diagnosis"], "detect-input-stopped")
            self.assertEqual(metrics["camera_control_reachable"], True)
            self.assertNotIn("weak-camera-signal", metrics["diagnoses"])

    def test_retained_recording_coverage_is_an_explicit_on_demand_check(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application()
            application.recorder.register_camera(
                camera_id="CAM-METRICS",
                claim_secret_hash="0" * 64,
                identity_reference="identity:CAM-METRICS",
                pre_enrolled=True,
            )
            self.assertEqual(
                application.camera_recording_coverage(camera_id="CAM-METRICS"),
                {"state": "unverified", "reason": "No supported recording engine."},
            )
            engine = FrigateEngine({"CAM-METRICS": "front_door"})
            calls = []
            engine.recording_coverage = lambda **values: (
                calls.append(values) or {"state": "indexed", "indexed_seconds": 120.0}
            )
            application.video_engine = engine

            self.assertEqual(
                application.camera_recording_coverage(camera_id="CAM-METRICS"),
                {"state": "indexed", "indexed_seconds": 120.0},
            )
            self.assertEqual(calls[0]["camera_id"], "CAM-METRICS")
            self.assertEqual(calls[0]["before"] - calls[0]["after"], 300)
            self.assertLessEqual(calls[0]["before"], int(datetime.now(UTC).timestamp()) - 14)

            def unavailable(**_values):
                raise VideoEngineError("private engine detail")

            engine.recording_coverage = unavailable
            self.assertEqual(
                application.camera_recording_coverage(camera_id="CAM-METRICS"),
                {"state": "unverified", "reason": "Retained video could not be checked."},
            )

    def test_recent_stream_check_separates_camera_delivery_from_recorder_ingest(self):
        class Source:
            camera_id = "CAM-METRICS"

            def get_json(self, path):
                return {"device_id": self.camera_id}

        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application(
                recording_source_provider=lambda: (Source(),)
            )
            application.recorder.register_camera(
                camera_id=Source.camera_id,
                claim_secret_hash="0" * 64,
                identity_reference="identity:CAM-METRICS",
                pre_enrolled=True,
            )
            engine = FrigateEngine({Source.camera_id: "front_door"})
            engine.live_metrics = lambda **kwargs: {"detect_input_fps": 0.0}
            application.video_engine = engine

            application._camera_verifications[Source.camera_id] = VerificationReport(
                schema_version=1,
                device_id=Source.camera_id,
                checked_at=datetime.now(UTC).isoformat(),
                checks=(CheckResult("stream", True, "Continuous stream passed."),),
            )
            metrics = application.camera_live_metrics(camera_id=Source.camera_id)
            self.assertEqual(metrics["camera_media_state"], "advancing")
            self.assertIn("recorder-detect-ingest-failed", metrics["diagnoses"])
            self.assertNotIn("weak-camera-signal", metrics["diagnoses"])

            application._camera_verifications[Source.camera_id] = VerificationReport(
                schema_version=1,
                device_id=Source.camera_id,
                checked_at=datetime.now(UTC).isoformat(),
                checks=(CheckResult("stream", False, "Continuous stream failed."),),
            )
            metrics = application.camera_live_metrics(camera_id=Source.camera_id)
            self.assertEqual(metrics["camera_media_state"], "unavailable")
            self.assertIn("camera-media-unavailable", metrics["diagnoses"])
            self.assertNotIn("recorder-detect-ingest-failed", metrics["diagnoses"])

            application._camera_verifications[Source.camera_id] = VerificationReport(
                schema_version=1,
                device_id=Source.camera_id,
                checked_at=(datetime.now(UTC) - timedelta(minutes=3)).isoformat(),
                checks=(CheckResult("stream", True, "Continuous stream passed."),),
            )
            metrics = application.camera_live_metrics(camera_id=Source.camera_id)
            self.assertNotIn("camera_media_state", metrics)
            self.assertNotIn("recorder-detect-ingest-failed", metrics["diagnoses"])

    def test_validated_setup_request_dispatches_both_provisioning_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application()
            kit = build_provisioning_config(
                mode="kit",
                camera_model="zero-2-w",
                camera_count=4,
                country="US",
            )
            camera = build_provisioning_config(
                mode="camera",
                camera_model="zero-w",
                camera_number=2,
                ssid="Camera Network",
                country="US",
            )

            with patch.object(
                application, "create_kit", return_value={"result": "kit"}
            ) as create_kit:
                self.assertEqual(
                    application.prepare_provisioning(kit), {"result": "kit"}
                )
            create_kit.assert_called_once_with(
                camera_count=4,
                camera_model="zero-2-w",
                wifi_country="US",
            )

            with patch.object(
                application, "provision_camera", return_value={"result": "camera"}
            ) as provision_camera:
                self.assertEqual(
                    application.prepare_provisioning(
                        camera,
                        wifi_passphrase="private passphrase",
                    ),
                    {"result": "camera"},
                )
            provision_camera.assert_called_once_with(
                model="zero-w",
                number=2,
                wifi_ssid="Camera Network",
                wifi_passphrase="private passphrase",
                wifi_country="US",
                flash=False,
                target=None,
                confirmation=None,
                allow_development=False,
                rotate_network=False,
                pilot_recovery=None,
            )

    def test_single_camera_gui_re_resolves_media_before_provisioning(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application()
            request = build_provisioning_config(
                mode="camera",
                camera_model="zero-w",
                camera_number=1,
                ssid="Camera Network",
                country="US",
            )
            target = DiskTarget(
                device="card-device",
                stable_id="CARD-123",
                display_name="32 GB removable card",
                size_bytes=32_000_000_000,
                removable=True,
                system_disk=False,
            )

            with patch.object(
                application, "list_media_targets", return_value=(target,)
            ), patch.object(
                application,
                "prepare_provisioning",
                return_value={"result": "flashed"},
            ) as prepare:
                result = application.provision_selected_camera(
                    request,
                    wifi_passphrase="private passphrase",
                    stable_id=target.stable_id,
                    confirmation=target.confirmation,
                )

            self.assertEqual(result, {"result": "flashed"})
            prepare.assert_called_once_with(
                request,
                wifi_passphrase="private passphrase",
                flash=True,
                target=target,
                confirmation=target.confirmation,
                allow_development=False,
                rotate_network=False,
                pilot_recovery=None,
            )

    def test_recording_volume_activation_and_capture_are_adapter_driven(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            volume = harness.root / "mounted-recording-drive"
            volume.mkdir()
            application.recorder.register_camera(
                camera_id="CAM-0001",
                claim_secret_hash=hashlib.sha256(b"claim").hexdigest(),
                identity_reference="identity:CAM-0001",
                pre_enrolled=True,
            )

            attestation = RecordingVolumeAttestation(
                root=volume,
                storage_reference="hdd-main",
                volume_identity="volume-uuid-1",
                mounted=True,
                system_device=False,
            )
            activated = application.activate_recording_storage(attestation)

            class Source:
                camera_id = "CAM-0001"

                def get_mjpeg_frames(
                    self, path, *, maximum, count, timeout_seconds
                ):
                    self.arguments = (path, maximum, count, timeout_seconds)
                    return (
                        b"\xff\xd8first\xff\xd9",
                        b"\xff\xd8second\xff\xd9",
                    )

            source = Source()
            captured = application.capture_camera_segment(
                storage_attestation=attestation,
                source=source,
                frame_count=2,
                maximum_bytes=4096,
            )

            self.assertEqual(activated["storage"], {"state": "ready", "reference": "hdd-main"})
            self.assertEqual(captured["result"], "recorded")
            self.assertEqual(captured["recording"]["camera_id"], "CAM-0001")
            self.assertEqual(captured["recording"]["frame_count"], 2)
            self.assertNotIn(str(volume), json.dumps(captured))
            self.assertEqual(source.arguments, ("/stream.mjpg", 4096, 2, 30.0))
            self.assertEqual(len(tuple((volume / "recordings").rglob("video.mjpeg"))), 1)

    def test_recording_cycle_resolves_storage_and_uses_injected_camera_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            volume = root / "recording-volume"
            volume.mkdir()
            target = RecordingStorageTarget(
                stable_id="STORAGE-123",
                display_name="Recording Drive (1.0 TB)",
                confirmation="INITIALIZE STORAGE-123",
                root=volume,
                volume_identity="linux-uuid:volume-1",
            )

            class Discovery:
                def list_targets(self):
                    return (target,)

            class Source:
                camera_id = "CAM-0001"

                def get_mjpeg_frames(
                    self, path, *, maximum, count, timeout_seconds
                ):
                    return (b"\xff\xd8first\xff\xd9", b"\xff\xd8second\xff\xd9")

            application = Harness(root).application(
                recording_storage_discovery=Discovery(),
                recording_source_provider=lambda: (Source(),),
            )
            application.recorder.register_camera(
                camera_id="CAM-0001",
                claim_secret_hash=hashlib.sha256(b"claim").hexdigest(),
                identity_reference="identity:CAM-0001",
                pre_enrolled=True,
            )
            application.initialize_selected_recording_storage(
                stable_id=target.stable_id,
                confirmation=target.confirmation,
            )
            application.set_recording(mode="continuous", days=21)

            result = application.capture_recording_cycle(
                frame_count=2, maximum_bytes=4096
            )

            self.assertEqual(result["state"], "recording")
            self.assertEqual(result["cameras"][0]["camera_id"], "CAM-0001")
            self.assertNotIn(str(volume), json.dumps(result))
            recordings = application.list_recordings()
            media, media_type = application.read_recording_media(
                reference=recordings[0]["reference"]
            )
            self.assertEqual(recordings[0]["camera_id"], "CAM-0001")
            self.assertTrue(media.startswith(b"--vms-frame"))
            self.assertEqual(media_type, "multipart/x-mixed-replace; boundary=vms-frame")

            application.set_recording(mode="continuous", days=1)
            maintenance = application.maintain_recordings(
                now=datetime(2030, 1, 1, tzinfo=UTC)
            )
            self.assertEqual(maintenance["state"], "ready")
            self.assertEqual(maintenance["deleted"], 1)
            self.assertEqual(application.list_recordings(), ())

    def test_recording_cycle_delegates_bound_cameras_only_to_video_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Source:
                camera_id = "CAM-0001"

            engine = FrigateEngine(
                {"CAM-0001": "back_porch"},
                transport=lambda *_: (b"[]", "application/json"),
            )
            application = Harness(root).application(
                recording_source_provider=lambda: (Source(),),
                video_engine=engine,
            )
            application.set_recording(mode="continuous", days=7)

            with (
                patch.object(application, "_active_recording_storage", return_value=object()),
                patch("video_monitoring_system.application.RecorderRuntime") as runtime,
            ):
                runtime.return_value.capture_cycle.return_value = {
                    "state": "recording",
                    "cameras": [],
                }
                application.capture_recording_cycle(frame_count=2)

            self.assertEqual(
                runtime.return_value.capture_cycle.call_args.kwargs[
                    "managed_camera_ids"
                ],
                frozenset({"CAM-0001"}),
            )

    def test_video_engine_events_drive_deduplicated_owner_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started_at = datetime.now(UTC) + timedelta(seconds=2)
            event = EngineEvent(
                id="frigate:review-123",
                camera_id="CAM-0001",
                started_at=started_at,
                ended_at=None,
                severity="alert",
                labels=("person",),
            )

            class Engine:
                def status(self):
                    return {"state": "ready", "engine": "frigate", "version": "0.18.0"}

                def list_events(self, **values):
                    self.list_arguments = values
                    return (event,)

                def read_snapshot(self, **values):
                    return b"snapshot"

                def read_event_preview(self, **values):
                    self.preview_arguments = values
                    return b"preview"

                def read_event_vod(self, **values):
                    self.vod_arguments = values
                    return b"#EXTM3U\n", "application/vnd.apple.mpegurl"

                def negotiate_live_webrtc(self, **values):
                    self.webrtc_arguments = values
                    return "v=0\r\no=answer\r\n"

            class RemoteAccess:
                @staticmethod
                def status():
                    return {
                        "state": "ready",
                        "remote_url": "https://osecameras.owner.ts.net",
                    }

            engine = Engine()
            application = Harness(root).application(
                remote_access_manager=RemoteAccess(),
                video_engine=engine,
            )
            application.recorder.register_camera(
                camera_id="CAM-0001",
                claim_secret_hash=hashlib.sha256(b"claim").hexdigest(),
                identity_reference="identity:CAM-0001",
                pre_enrolled=True,
            )
            application.recorder.name_camera(
                camera_id="CAM-0001", name="Back Porch"
            )
            sent = []
            application.push_notifications.send_event = lambda **values: sent.append(values) or 1

            events = application.list_video_events(camera_id="CAM-0001")
            preview = application.read_video_event_preview(event_id=event.id)
            vod = application.read_video_event_vod(
                event_id=event.id, resource="master.m3u8"
            )
            answer = application.negotiate_live_webrtc(
                camera_id="CAM-0001",
                offer="v=0\r\no=offer\r\n",
            )
            first = application.process_video_engine_alerts(
                now=started_at + timedelta(seconds=10)
            )
            second = application.process_video_engine_alerts(
                now=started_at + timedelta(seconds=20)
            )

            self.assertEqual(events[0]["id"], event.id)
            self.assertEqual(preview, b"preview")
            self.assertEqual(vod, (b"#EXTM3U\n", "application/vnd.apple.mpegurl"))
            self.assertTrue(answer.startswith("v=0"))
            self.assertEqual(first, {"state": "ready", "delivered": 1, "failed": 0})
            self.assertEqual(second, {"state": "ready", "delivered": 0, "failed": 0})
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["title"], "Person at Back Porch")
            self.assertEqual(sent[0]["body"], "Tap to view activity in progress.")
            self.assertEqual(sent[0]["url"], "/events?event=frigate%3Areview-123")
            self.assertEqual(engine.preview_arguments, {"event_id": event.id})
            self.assertEqual(
                engine.vod_arguments,
                {"event_id": event.id, "resource": "master.m3u8"},
            )
            self.assertEqual(
                engine.webrtc_arguments,
                {"camera_id": "CAM-0001", "offer": "v=0\r\no=offer\r\n"},
            )

    def test_paused_alerts_are_suppressed_without_changing_camera_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started_at = datetime.now(UTC) + timedelta(seconds=2)

            class Engine:
                def __init__(self):
                    self.events = ()

                def status(self):
                    return {"state": "ready", "engine": "frigate"}

                def list_events(self, **values):
                    return self.events

            class RemoteAccess:
                @staticmethod
                def status():
                    return {
                        "state": "ready",
                        "remote_url": "https://osecameras.owner.ts.net",
                    }

            engine = Engine()
            application = Harness(root).application(
                remote_access_manager=RemoteAccess(), video_engine=engine
            )
            application.recorder.register_camera(
                camera_id="CAM-0001",
                claim_secret_hash=hashlib.sha256(b"claim").hexdigest(),
                identity_reference="identity:CAM-0001",
                pre_enrolled=True,
            )
            sent = []
            application.push_notifications.send_event = lambda **values: sent.append(values) or 1
            globally_muted = EngineEvent(
                id="frigate:global-muted",
                camera_id="CAM-0001",
                started_at=started_at,
                ended_at=None,
                severity="alert",
                labels=("person",),
            )
            engine.events = (globally_muted,)

            application.set_alerts_enabled(enabled=False)
            paused = application.process_video_engine_alerts(
                now=started_at + timedelta(seconds=10)
            )
            application.set_alerts_enabled(enabled=True)
            resumed = application.process_video_engine_alerts(
                now=started_at + timedelta(seconds=20)
            )

            self.assertEqual(paused, {"state": "paused", "delivered": 0})
            self.assertEqual(resumed["delivered"], 0)
            self.assertEqual(sent, [])
            self.assertTrue(application.list_cameras()[0]["alerts_enabled"])

            camera_muted = EngineEvent(
                id="frigate:camera-muted",
                camera_id="CAM-0001",
                started_at=started_at + timedelta(seconds=30),
                ended_at=None,
                severity="alert",
                labels=("person",),
            )
            engine.events = (globally_muted, camera_muted)
            application.set_camera_alerts(camera_id="CAM-0001", enabled=False)
            application.process_video_engine_alerts(
                now=started_at + timedelta(seconds=40)
            )
            application.set_camera_alerts(camera_id="CAM-0001", enabled=True)
            application.process_video_engine_alerts(
                now=started_at + timedelta(seconds=50)
            )
            self.assertEqual(sent, [])

    def test_default_recording_adapter_loads_only_authorized_private_kit_cameras(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application()
            job = ProvisioningJob.create(
                ProvisioningSpec(camera_count=1, camera_board="zero-w"),
                wifi_country="US",
            )
            application._kit_state_path.parent.mkdir(parents=True)
            application._kit_state_path.write_text(
                job.to_private_json(), encoding="utf-8"
            )
            camera = job.identity.cameras[0]
            application.recorder.register_camera(
                camera_id=camera.device_id,
                claim_secret_hash=hashlib.sha256(camera.claim_secret.encode()).hexdigest(),
                identity_reference=f"identity:{camera.key.public_identity}",
                pre_enrolled=True,
            )
            application.set_camera_recording(
                camera_id=camera.device_id, enabled=False
            )

            sources = tuple(application._recording_source_provider())

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].camera_id, camera.device_id)
            self.assertFalse(application.list_cameras()[0]["recording_enabled"])
            self.assertEqual(sources[0].base_url, "http://camera-001.local:9080")
            self.assertNotIn(camera.stream_secret, repr(sources))

    def test_matching_kit_and_device_authorization_share_one_camera_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = Harness(root).application()
            job = ProvisioningJob.create(
                ProvisioningSpec(camera_count=1, camera_board="zero-w"),
                wifi_country="US",
            )
            application._kit_state_path.parent.mkdir(parents=True)
            application._kit_state_path.write_text(
                job.to_private_json(), encoding="utf-8"
            )
            camera = job.identity.cameras[0]
            application.recorder.register_camera(
                camera_id=camera.device_id,
                claim_secret_hash=hashlib.sha256(camera.claim_secret.encode()).hexdigest(),
                identity_reference=f"identity:{camera.key.public_identity}",
                pre_enrolled=True,
            )
            authorization = root / "recorder-device.json"
            authorization.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "device_id": job.identity.recorder.device_id,
                        "role": "recorder",
                        "authorized_cameras": [
                            {
                                "device_id": camera.device_id,
                                "camera_number": camera.camera_number,
                                "identity": {
                                    "algorithm": camera.key.algorithm,
                                    "public": camera.key.public_identity,
                                },
                                "claim_secret_hash": hashlib.sha256(
                                    camera.claim_secret.encode()
                                ).hexdigest(),
                                "stream_secret": camera.stream_secret,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            authorization.chmod(0o600)

            restarted = VMSApplication(
                application.data_directory,
                repository=application.repository,
                recorder_authorization_path=authorization,
            )
            sources = tuple(restarted._recording_source_provider())

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].camera_id, camera.device_id)

    def test_default_recording_adapter_loads_zero_padded_pilot_state(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application()
            job = ProvisioningJob.create(
                ProvisioningSpec(camera_count=1, camera_board="zero-w"),
                wifi_country="US",
            )
            state_path = application._pilot_state_path("zero-w", 1)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(job.to_private_json(), encoding="utf-8")
            camera = job.identity.cameras[0]
            application.recorder.register_camera(
                camera_id=camera.device_id,
                claim_secret_hash=hashlib.sha256(camera.claim_secret.encode()).hexdigest(),
                identity_reference=f"identity:{camera.key.public_identity}",
                pre_enrolled=True,
            )

            sources = tuple(application._recording_source_provider())

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].camera_id, camera.device_id)

    def test_default_recording_adapter_loads_authorized_private_import(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application()
            camera_id = "CAM-0123456789ABCDEF"
            stream_secret = "test-placeholder-imported-stream-secret"
            application.recorder.register_camera(
                camera_id=camera_id,
                claim_secret_hash=hashlib.sha256(b"claim").hexdigest(),
                identity_reference=f"identity:legacy-migration:{camera_id}",
                pre_enrolled=True,
            )
            source_path = application.data_directory / "imported-camera-sources.json"
            source_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cameras": [
                            {
                                "camera_id": camera_id,
                                "hostname": "camera-004.local",
                                "port": 9080,
                                "stream_secret": stream_secret,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            source_path.chmod(0o600)

            sources = tuple(application._recording_source_provider())

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].camera_id, camera_id)
            self.assertEqual(sources[0].base_url, "http://camera-004.local:9080")
            self.assertNotIn(stream_secret, repr(sources))

            payload = json.dumps({"schema_version": 1, "camera_id": camera_id, "port": 9080}).encode()
            signature = hmac.new(stream_secret.encode(), payload, hashlib.sha256).hexdigest()
            health = {"device_id": camera_id, "claimed": True, "camera_ready": True}
            with patch("video_monitoring_system.application.HttpCameraTransport") as transport, patch("video_monitoring_system.application._verify_camera_route_media"):
                transport.return_value.get_json.return_value = health
                application.register_camera_endpoint(payload=payload, signature=signature, source_address="192.168.20.21")
            sources = tuple(application._recording_source_provider())
            self.assertEqual(sources[0].base_url, "http://192.168.20.21:9080")

    def test_route_relay_can_authorize_an_active_private_import(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application()
            camera_id = "CAM-0123456789ABCDEF"
            stream_secret = "test-placeholder-imported-stream-secret"
            application.recorder.register_camera(
                camera_id=camera_id,
                claim_secret_hash=hashlib.sha256(b"claim").hexdigest(),
                identity_reference=f"identity:legacy-migration:{camera_id}",
                pre_enrolled=True,
            )
            source_path = application.data_directory / "imported-camera-sources.json"
            source_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cameras": [
                            {
                                "camera_id": camera_id,
                                "hostname": "camera-004.local",
                                "port": 9080,
                                "stream_secret": stream_secret,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            source_path.chmod(0o600)

            self.assertEqual(
                application._camera_stream_secret(camera_id), stream_secret
            )
            self.assertIsNone(
                application._camera_stream_secret("CAM-FFFFFFFFFFFFFFFF")
            )

    def test_recording_volume_activation_refuses_existing_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            volume = harness.root / "not-an-empty-drive"
            volume.mkdir()
            protected = volume / "keep.txt"
            protected.write_text("keep", encoding="utf-8")
            attestation = RecordingVolumeAttestation(
                root=volume,
                storage_reference="recordings",
                volume_identity="volume-uuid-2",
                mounted=True,
                system_device=False,
            )

            with self.assertRaisesRegex(ApplicationError, "must be empty"):
                application.activate_recording_storage(attestation)

            self.assertEqual(protected.read_text(encoding="utf-8"), "keep")
            self.assertEqual(application.status()["storage"], "planned")

    def test_selected_recording_storage_is_re_resolved_and_exactly_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application()
            volume = Path(directory) / "recording-volume"
            volume.mkdir()
            target = RecordingStorageTarget(
                stable_id="STORAGE-123",
                display_name="Recording Drive (1.0 TB)",
                confirmation="INITIALIZE STORAGE-123",
                root=volume,
                volume_identity="linux-uuid:volume-1",
            )

            class Discovery:
                calls = 0

                def list_targets(self):
                    self.calls += 1
                    return (target,)

            discovery = Discovery()
            application.recording_storage_discovery = discovery
            with self.assertRaisesRegex(ApplicationError, "not confirmed"):
                application.initialize_selected_recording_storage(
                    stable_id=target.stable_id,
                    confirmation="wrong",
                )
            self.assertFalse((volume / ".vms-recording-storage.json").exists())

            result = application.initialize_selected_recording_storage(
                stable_id=target.stable_id,
                confirmation=target.confirmation,
            )

            self.assertGreaterEqual(discovery.calls, 2)
            self.assertEqual(result["storage"], {"state": "ready", "reference": "recordings"})
            self.assertEqual(application.status()["storage"], "ready")

    def test_selected_recording_storage_adoption_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Harness(Path(directory)).application()
            volume = Path(directory) / "recording-volume"
            volume.mkdir()
            legacy = volume / "legacy-recordings"
            legacy.mkdir()
            protected = legacy / "keep.mjpeg"
            protected.write_bytes(b"legacy recording")
            target = RecordingStorageTarget(
                stable_id="STORAGE-123",
                display_name="Recording Drive (1.0 TB)",
                confirmation="INITIALIZE STORAGE-123",
                root=volume,
                volume_identity="linux-uuid:volume-1",
            )

            class Discovery:
                def list_targets(self):
                    return (target,)

            application.recording_storage_discovery = Discovery()
            with self.assertRaisesRegex(ApplicationError, "not confirmed"):
                application.adopt_selected_recording_storage(
                    stable_id=target.stable_id,
                    confirmation=target.confirmation,
                )

            result = application.adopt_selected_recording_storage(
                stable_id=target.stable_id,
                confirmation=target.adoption_confirmation,
            )

            self.assertEqual(protected.read_bytes(), b"legacy recording")
            self.assertEqual(result["storage"], {"state": "ready", "reference": "recordings"})
            self.assertEqual(application.status()["storage"], "ready")

    def test_ready_storage_rejects_another_volume_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            original = harness.root / "original-volume"
            candidate = harness.root / "candidate-volume"
            original.mkdir()
            candidate.mkdir()
            application.activate_recording_storage(
                RecordingVolumeAttestation(
                    root=original,
                    storage_reference="hdd-main",
                    volume_identity="volume-uuid-1",
                    mounted=True,
                    system_device=False,
                )
            )
            candidate_attestation = RecordingVolumeAttestation(
                root=candidate,
                storage_reference="hdd-main",
                volume_identity="volume-uuid-2",
                mounted=True,
                system_device=False,
            )

            with self.assertRaisesRegex(ApplicationError, "another volume"):
                application.activate_recording_storage(candidate_attestation)

            self.assertEqual(tuple(candidate.iterdir()), ())

    def test_ready_storage_does_not_recreate_a_missing_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            volume = harness.root / "recording-volume"
            volume.mkdir()
            attestation = RecordingVolumeAttestation(
                root=volume,
                storage_reference="hdd-main",
                volume_identity="volume-uuid-1",
                mounted=True,
                system_device=False,
            )
            application.activate_recording_storage(attestation)
            marker = volume / ".vms-recording-storage.json"
            marker.unlink()

            with self.assertRaisesRegex(ApplicationError, "not initialized"):
                application.activate_recording_storage(attestation)

            self.assertFalse(marker.exists())

    def test_wifi_country_default_requires_explicit_valid_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"VMS_WIFI_COUNTRY": "US"}):
                application = Harness(root).application()
            self.assertEqual(application.wifi_country_default, "US")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.dict(os.environ, {"VMS_WIFI_COUNTRY": "us"}),
                self.assertRaisesRegex(Exception, "VMS_WIFI_COUNTRY"),
            ):
                Harness(root).application()

    def test_owner_password_is_hashed_and_same_request_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            password = "test-placeholder-owner-password"

            first = application.create_owner(name="Owner", password=password)
            second = application.create_owner(name="Owner", password=password)

            self.assertEqual(first["result"], "configured")
            self.assertEqual(second["result"], "already_configured")
            saved = (harness.root / "private" / "recorder-state.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(password, saved)
            self.assertIn("hash:scrypt$", saved)
            self.assertTrue(application.authenticate_owner(password=password))
            self.assertFalse(
                application.authenticate_owner(password="incorrect-owner-password")
            )

    def test_owner_can_change_password_without_storing_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            old_password = "test-placeholder-old-owner-password"
            new_password = "test-placeholder-new-owner-password"
            application.create_owner(name="Owner", password=old_password)

            with self.assertRaisesRegex(ApplicationError, "current owner password"):
                application.change_owner_password(
                    current_password="test-placeholder-incorrect-password",
                    new_password=new_password,
                )
            result = application.change_owner_password(
                current_password=old_password, new_password=new_password
            )

            self.assertEqual(result["result"], "updated")
            self.assertFalse(application.authenticate_owner(password=old_password))
            self.assertTrue(application.authenticate_owner(password=new_password))
            saved = (harness.root / "private" / "recorder-state.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(old_password, saved)
            self.assertNotIn(new_password, saved)

    def test_kit_creation_runs_doctor_first_and_returns_only_public_values(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()

            first = application.create_kit(
                camera_count=2,
                camera_model="zero-2-w",
                wifi_country="US",
            )
            second = application.create_kit(
                camera_count=2,
                camera_model="zero-2-w",
                wifi_country="US",
            )

            self.assertEqual(harness.events[0][0], "doctor")
            self.assertEqual(first["kit_id"], second["kit_id"])
            self.assertEqual(len(application.list_cameras(unclaimed=True)), 0)
            included = application.list_cameras()
            self.assertEqual([camera["state"] for camera in included], ["ready", "ready"])
            application.name_camera(camera_id=included[0]["id"], name="Driveway")
            self.assertEqual(application.list_cameras()[0]["name"], "Driveway")
            private_job = application._kit_state_path.read_text(encoding="utf-8")
            public_result = json.dumps(first)
            private_value = json.loads(private_job)["identity"]["wifi_passphrase"]
            self.assertEqual(
                json.loads(private_job)["identity"]["wifi_country"],
                "US",
            )
            self.assertNotIn(private_value, public_result)
            public_manifest = (
                harness.root
                / "private"
                / "kits"
                / first["kit_id"]
                / "manifest.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn(private_value, public_manifest)

    def test_zero_w_pilot_preserves_number_and_defaults_to_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            ssid = "private network name"
            passphrase = "test-placeholder-network-password"

            result = application.provision_camera(
                model="zero-w",
                number=3,
                wifi_ssid=ssid,
                wifi_passphrase=passphrase,
                wifi_country="US",
            )

            self.assertEqual(result["result"], "preview")
            self.assertEqual(result["camera_number"], 3)
            self.assertTrue(result["pilot_recovery"])
            diagnostics = result["diagnostics"]
            self.assertEqual(diagnostics["private_network_ssh"], "camera-003.local")
            self.assertEqual(diagnostics["username"], "vms-recovery")
            ssh_key = Path(diagnostics["ssh_key_file"])
            self.assertTrue(ssh_key.is_file())
            self.assertNotIn(
                ssh_key.read_text(encoding="utf-8"), json.dumps(result)
            )
            self.assertEqual(harness.events[0][0], "doctor")
            arguments = harness.image_arguments[0]
            self.assertTrue(arguments["dry_run"])
            self.assertTrue(arguments["allow_development"])
            self.assertIn(
                "enable_uart=1",
                arguments["boot_customization"].config_txt_append,
            )
            self.assertEqual(
                arguments["network_config"],
                "version: 2\nrenderer: NetworkManager\nwifis: {}\n",
            )
            self.assertNotIn(ssid, arguments["network_config"])
            self.assertNotIn(passphrase, arguments["network_config"])
            self.assertNotIn(ssid, json.dumps(result))
            self.assertNotIn(passphrase, json.dumps(result))

            shown = application.pilot_camera_diagnostics(
                model="zero-w", number=3
            )
            self.assertEqual(shown, diagnostics)

    def test_camera_reflash_preserves_legacy_recorder_release_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            state_path = application.data_directory / "pilots" / "camera-002-zero-w.json"
            saved = ProvisioningJob.open_or_create(
                state_path,
                ProvisioningSpec(
                    camera_count=2,
                    camera_board="zero-w",
                    recorder_image="vms-recorder",
                ),
                wifi_ssid="Development Network",
                wifi_passphrase="test-placeholder-network-password",
                wifi_country="US",
            )
            before = state_path.read_bytes()
            for _ in range(2):
                result = application.provision_camera(
                    model="zero-w",
                    number=2,
                    wifi_ssid=saved.identity.wifi_ssid,
                    wifi_passphrase=saved.identity.wifi_passphrase,
                    wifi_country="US",
                )
                self.assertEqual(result["device_id"], saved.identity.cameras[1].device_id)
                self.assertEqual(state_path.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, "different wifi_passphrase"):
                application.provision_camera(
                    model="zero-w",
                    number=2,
                    wifi_ssid=saved.identity.wifi_ssid,
                    wifi_passphrase="a-different-network-password",
                    wifi_country="US",
                )
            self.assertEqual(state_path.read_bytes(), before)

    def test_pilot_diagnostics_migrates_legacy_pkcs8_key_without_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            result = application.provision_camera(
                model="zero-w",
                number=4,
                wifi_ssid="private network name",
                wifi_passphrase="test-placeholder-network-password",
                wifi_country="US",
            )
            ssh_key = Path(result["diagnostics"]["ssh_key_file"])
            current_key = serialization.load_ssh_private_key(
                ssh_key.read_bytes(), password=None
            )
            legacy_key = current_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            ssh_key.write_bytes(legacy_key)

            application.pilot_camera_diagnostics(model="zero-w", number=4)

            recovered_key = serialization.load_ssh_private_key(
                ssh_key.read_bytes(), password=None
            )
            self.assertEqual(
                recovered_key.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                ),
                current_key.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                ),
            )

    def test_new_pilot_state_selects_available_preinstalled_release(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            application.release_catalog = {
                PREINSTALLED_CAMERA_IMAGE_REFERENCE: development_camera_release()
            }

            application.provision_camera(
                model="zero-w",
                number=1,
                wifi_ssid="private network name",
                wifi_passphrase="test-placeholder-network-password",
                wifi_country="US",
            )

            state = json.loads(
                (
                    harness.root
                    / "private"
                    / "pilots"
                    / "camera-001-zero-w.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                state["spec"]["camera_image"],
                PREINSTALLED_CAMERA_IMAGE_REFERENCE,
            )

    def test_pilot_stage_is_complete_private_and_content_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            provisioned = application.provision_camera(
                model="zero-w",
                number=2,
                wifi_ssid="Development Network",
                wifi_passphrase="test-placeholder-network-password",
                wifi_country="US",
            )
            destination = harness.root / "operator-private" / "camera-002"

            result = application.stage_pilot_camera(
                model="zero-w",
                number=2,
                output_directory=destination,
            )

            self.assertTrue(result["verified"])
            self.assertEqual(result["device_id"], provisioned["device_id"])
            self.assertEqual(
                {path.name for path in destination.iterdir()},
                {
                    "user-data",
                    "network-config",
                    "meta-data",
                    "metadata.json",
                    "boot-customization.json",
                },
            )
            metadata = json.loads(
                (destination / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["image_reference"], "raspios-32-vms-camera")
            self.assertEqual(metadata["release_version"], "camera-pilot-1")
            self.assertEqual(metadata["release_image_sha256"], "a" * 64)
            self.assertRegex(metadata["dhcp_client_id"], r"^00(?::[0-9a-f]{2})+$")
            for filename, hash_name in (
                ("user-data", "user_data_sha256"),
                ("network-config", "network_config_sha256"),
                ("meta-data", "meta_data_sha256"),
                ("boot-customization.json", "boot_customization_sha256"),
            ):
                content = (destination / filename).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), metadata[hash_name])
            self.assertNotIn(
                "test-placeholder-network-password", json.dumps(result)
            )

    def test_pilot_stage_refuses_source_and_existing_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            application.provision_camera(
                model="zero-w",
                number=1,
                wifi_ssid="Development Network",
                wifi_passphrase="test-placeholder-network-password",
                wifi_country="US",
            )

            with self.assertRaisesRegex(Exception, "outside the source"):
                application.stage_pilot_camera(
                    model="zero-w",
                    number=1,
                    output_directory=harness.root / "source" / "private-stage",
                )
            existing = harness.root / "existing-stage"
            existing.mkdir()
            with self.assertRaisesRegex(Exception, "already exists"):
                application.stage_pilot_camera(
                    model="zero-w", number=1, output_directory=existing
                )

    def test_pilot_release_migration_preserves_identity_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            application.provision_camera(
                model="zero-w",
                number=3,
                wifi_ssid="Development Network",
                wifi_passphrase="test-placeholder-network-password",
                wifi_country="US",
            )
            state_path = (
                harness.root / "private" / "pilots" / "camera-003-zero-w.json"
            )
            before = json.loads(state_path.read_text(encoding="utf-8"))
            application.release_catalog[PREINSTALLED_CAMERA_IMAGE_REFERENCE] = (
                development_camera_release()
            )

            first = application.migrate_pilot_camera_release(
                model="zero-w", number=3
            )
            after_first_bytes = state_path.read_bytes()
            second = application.migrate_pilot_camera_release(
                model="zero-w", number=3
            )

            self.assertEqual(first["result"], "migrated")
            self.assertEqual(second["result"], "already_current")
            self.assertEqual(state_path.read_bytes(), after_first_bytes)
            after = json.loads(after_first_bytes)
            self.assertEqual(
                after["spec"]["camera_image"], PREINSTALLED_CAMERA_IMAGE_REFERENCE
            )
            self.assertTrue(
                all(
                    camera["image_reference"]
                    == PREINSTALLED_CAMERA_IMAGE_REFERENCE
                    for camera in after["identity"]["cameras"]
                )
            )
            before["spec"].pop("camera_image")
            after["spec"].pop("camera_image")
            for camera in before["identity"]["cameras"]:
                camera.pop("image_reference")
            for camera in after["identity"]["cameras"]:
                camera.pop("image_reference")
            self.assertEqual(after, before)

            stage = harness.root / "operator-private" / "camera-003-preinstalled"
            application.stage_pilot_camera(
                model="zero-w", number=3, output_directory=stage
            )
            self.assertNotIn(
                "apt-get", (stage / "user-data").read_text(encoding="utf-8")
            )

    def test_pilot_release_migration_requires_an_exact_compatible_release(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            application.provision_camera(
                model="zero-w",
                number=1,
                wifi_ssid="Development Network",
                wifi_passphrase="test-placeholder-network-password",
                wifi_country="US",
            )
            with self.assertRaisesRegex(Exception, "does not exist"):
                application.migrate_pilot_camera_release(
                    model="zero-w", number=1, image_reference="missing-release"
                )

            incompatible = development_camera_release().to_dict()
            incompatible["supported_boards"] = ["zero-2-w"]
            application.release_catalog["zero-2-only"] = ImageReleaseManifest.from_dict(
                incompatible
            )
            with self.assertRaisesRegex(Exception, "does not support"):
                application.migrate_pilot_camera_release(
                    model="zero-w", number=1, image_reference="zero-2-only"
                )

    def test_verify_kit_returns_redacted_camera_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            application.create_kit(
                camera_count=2,
                camera_model="zero-2-w",
                wifi_country="US",
            )

            result = application.verify_kit()

            self.assertTrue(result["passed"])
            self.assertEqual(len(result["cameras"]), 2)
            private_job = json.loads(application._kit_state_path.read_text(encoding="utf-8"))
            private_values = [
                private_job["identity"]["wifi_passphrase"],
                private_job["identity"]["cameras"][0]["claim_secret"],
            ]
            encoded = json.dumps(result)
            for value in private_values:
                self.assertNotIn(value, encoded)

    def test_pilot_verification_loads_the_selected_persisted_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            provisioned = application.provision_camera(
                model="zero-w",
                number=2,
                wifi_ssid="Development Network",
                wifi_passphrase="test-placeholder-network-password",
                wifi_country="US",
            )

            result = application.verify_pilot_camera(
                model="zero-w",
                number=2,
                hostname="127.0.0.1",
                port=19080,
            )

            self.assertTrue(result["passed"])
            self.assertEqual(result["device_id"], provisioned["device_id"])
            encoded = json.dumps(result)
            private_job = (
                harness.root / "private" / "pilots" / "camera-002-zero-w.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn(json.loads(private_job)["identity"]["wifi_passphrase"], encoded)
            verification = next(
                event for event in harness.events if event[0] == "verify-camera"
            )
            self.assertEqual(
                verification[1], {"hostname": "127.0.0.1", "port": 19080}
            )

    def test_data_directory_inside_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "source"
            repository.mkdir()
            with self.assertRaisesRegex(Exception, "outside the source repository"):
                VMSApplication(repository / "private", repository=repository)

    def test_media_list_never_offers_tiny_peripheral_storage(self):
        class Discovery:
            def list_disks(self):
                return (
                    DiskTarget(
                        "tiny",
                        "TINY",
                        "Peripheral storage",
                        36_000,
                        True,
                        False,
                    ),
                    DiskTarget(
                        "card",
                        "CARD",
                        "32 GB system card",
                        32_000_000_000,
                        True,
                        False,
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            application.disk_discovery = Discovery()

            targets = application.list_media_targets()

            self.assertEqual([target.stable_id for target in targets], ["CARD"])

    def test_gui_is_retail_only_unless_factory_mode_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            retail = application.create_gui_server(port=0)
            factory = application.create_gui_server(port=0, factory=True)
            self.addCleanup(retail.server_close)
            self.addCleanup(factory.server_close)
            self.assertFalse(retail.get_app().factory_enabled)
            self.assertTrue(factory.get_app().factory_enabled)

    def test_gui_flash_re_resolves_card_and_requires_exact_phrase(self):
        class Discovery:
            def __init__(self, target):
                self.target = target

            def list_disks(self):
                return (self.target,)

        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            application = harness.application()
            target = DiskTarget(
                "card",
                "CARD-123",
                "32 GB system card",
                32_000_000_000,
                True,
                False,
            )
            application.disk_discovery = Discovery(target)
            captured = {}

            def flash_next(**values):
                captured.update(values)
                return {"result": "flashed"}

            application.flash_next = flash_next
            with self.assertRaisesRegex(Exception, "not confirmed"):
                application.flash_selected(
                    stable_id=target.stable_id,
                    confirmation="wrong",
                )

            result = application.flash_selected(
                stable_id=target.stable_id,
                confirmation=target.confirmation,
            )

            self.assertEqual(result["result"], "flashed")
            self.assertEqual(captured["target"], target)
            self.assertEqual(captured["confirmation"], target.confirmation)


class ReleaseCatalogTests(unittest.TestCase):
    def test_strict_manifest_is_loaded_by_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera-release.json"
            path.write_text(
                json.dumps(development_camera_release().to_dict()), encoding="utf-8"
            )
            catalog = load_release_catalog(path.parent)
            self.assertEqual(catalog["camera-release"].role, "camera")

    def test_manifest_can_declare_a_stable_image_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera-release.json"
            value = development_camera_release().to_dict()
            value["image_reference"] = "raspios-32-vms-camera-preinstalled"
            path.write_text(json.dumps(value), encoding="utf-8")

            catalog = load_release_catalog(path.parent)

            self.assertIn("raspios-32-vms-camera-preinstalled", catalog)
            self.assertEqual(
                catalog["raspios-32-vms-camera-preinstalled"].role, "camera"
            )

    def test_invalid_manifest_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text('{"password": "test-placeholder-value"}', encoding="utf-8")
            self.assertEqual(load_release_catalog(path.parent), {})


if __name__ == "__main__":
    unittest.main()
