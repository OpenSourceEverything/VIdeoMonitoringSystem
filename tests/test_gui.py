import io
import json
import re
import tempfile
import unittest
from html import unescape
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

from video_monitoring_system.application import VMSApplication
from video_monitoring_system.backup import BackupSummary
from video_monitoring_system.gui import create_application, create_server
from video_monitoring_system.imaging import DiskTarget
from video_monitoring_system.storage_adapter import RecordingStorageTarget
from video_monitoring_system.verification import CheckResult, VerificationReport


class FakeFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.provisioning_requests = []
        self.owner_ready = False
        self.owner_password = "example-owner-password"
        self.camera_name = "Driveway"
        self.camera_state = "pending"
        self.camera_connected = False
        self.camera_recording_enabled = True
        self.camera_alerts_enabled = True
        self.alerts_enabled = True
        self.camera_metrics = {
            "rssi_dbm": -67,
            "signal_percent": 61,
            "detect_input_fps": 5.2,
            "skipped_fps": 0.1,
            "connection_quality": "fair",
            "source_stalls_last_hour": 12,
            "source_reconnects_last_hour": 2,
            "recorder_cpu_percent": 78.9,
            "source_codec": "mjpeg",
            "source_fps": 10,
            "recorder_transcoding": True,
            "diagnoses": ["source-interruptions", "legacy-transcoding"],
            "diagnosis": "source-interruptions",
        }
        self.camera_extra = {}
        self.camera_setup = None
        self.diagnostics = []
        self.recordings = []
        self.video_events = []
        self.saved_video_clips = []
        self.video_engine = {"state": "legacy", "engine": "legacy"}
        self.setup_next = None
        self.target = DiskTarget(
            "card",
            "CARD-123",
            "32 GB system card",
            32_000_000_000,
            True,
            False,
        )
        self.storage_target = RecordingStorageTarget(
            stable_id="STORAGE-123",
            display_name="Recording Drive (1.0 TB)",
            confirmation="INITIALIZE STORAGE-123",
            root=Path("/recording"),
            volume_identity="linux-uuid:volume-1",
        )
        self.storage_status = {"state": "planned"}
        self.access = {
            "mode": "off",
            "state": "local-only",
            "local_url": "http://osecameras.local",
            "remote_url": None,
            "public_access": False,
        }
        self.remote_approval_url = None
        self.push_subscription = None

    def status(self):
        status = {
            "owner_ready": self.owner_ready,
            "cameras": {"connected": 1, "expected": 2},
            "recording": {"mode": "continuous", "days": 7},
            "alerts": {"enabled": self.alerts_enabled},
            "access": self.access,
        }
        if self.setup_next is not None:
            status["setup"] = {
                "ready": self.setup_next == "ready",
                "next": self.setup_next,
            }
        return status

    def list_cameras(self):
        return [
            {
                "id": "CAM-0001",
                "name": self.camera_name,
                "state": self.camera_state,
                "connected": self.camera_connected,
                "recording_enabled": self.camera_recording_enabled,
                "alerts_enabled": self.camera_alerts_enabled,
                **self.camera_extra,
            }
        ]

    def register_camera_endpoint(self, **values):
        self.calls.append(("register_camera_endpoint", values))
        return {"result": "registered", "camera_id": "CAM-0001"}

    def create_owner(self, **values):
        self.calls.append(("create_owner", values))
        self.owner_ready = True

    def authenticate_owner(self, **values):
        self.calls.append(("authenticate_owner", values))
        return values.get("password") == self.owner_password

    def change_owner_password(self, **values):
        self.calls.append(("change_owner_password", values))
        if values.get("current_password") != self.owner_password:
            raise ValueError("incorrect current password")
        self.owner_password = str(values["new_password"])

    def create_backup_download(self, **values):
        self.calls.append(("create_backup_download", values))
        return (
            b"encrypted-backup",
            "vms-recorder-backup-20260830T000000Z.vms-backup",
            BackupSummary("2026-08-30T00:00:00+00:00", 3, 12),
        )

    def initialize_storage(self):
        self.calls.append(("initialize_storage", {}))

    def list_recording_storage_targets(self):
        return (self.storage_target,)

    def initialize_selected_recording_storage(self, **values):
        self.calls.append(("initialize_selected_recording_storage", values))

    def recording_storage_status(self):
        return dict(self.storage_status)

    def claim_camera(self, **values):
        self.calls.append(("claim_camera", values))

    def claim_camera_payload(self, **values):
        self.calls.append(("claim_camera_payload", values))

    def claim_camera_code(self, **values):
        self.calls.append(("claim_camera_code", values))

    def camera_setup_status(self, **values):
        return None if self.camera_setup is None else dict(self.camera_setup)

    def retry_camera_network_setup(self, **values):
        self.calls.append(("retry_camera_network_setup", values))
        if self.camera_setup is None:
            raise ValueError("Camera setup is unavailable.")
        self.camera_setup.update(state="waiting-network", issue=None)
        return dict(self.camera_setup)

    def resolve_camera_identity_conflict(self, **values):
        self.calls.append(("resolve_camera_identity_conflict", values))
        self.camera_extra["network_issue"] = None
        return {"result": "resolved", "camera_id": values["camera_id"]}

    def name_camera(self, **values):
        self.calls.append(("name_camera", values))

    def position_camera(self, **values):
        self.calls.append(("position_camera", values))

    def set_camera_recording(self, **values):
        self.calls.append(("set_camera_recording", values))
        self.camera_recording_enabled = bool(values["enabled"])

    def set_alerts_enabled(self, **values):
        self.calls.append(("set_alerts_enabled", values))
        self.alerts_enabled = bool(values["enabled"])

    def set_camera_alerts(self, **values):
        self.calls.append(("set_camera_alerts", values))
        self.camera_alerts_enabled = bool(values["enabled"])

    def verify_recorder_camera(self, **values):
        self.calls.append(("verify_recorder_camera", values))
        report = VerificationReport(
            schema_version=1,
            device_id=str(values["camera_id"]),
            checked_at="2026-08-29T20:30:00+00:00",
            checks=(
                CheckResult("connection", True, "Camera responded."),
                CheckResult("power", True, "Power passed."),
                CheckResult("capture", True, "Sensor passed."),
                CheckResult("image", True, "Image passed."),
                CheckResult("stream", True, "Stream passed."),
            ),
        )
        self.camera_connected = True
        self.camera_extra["verification"] = report
        return report

    def set_recording(self, **values):
        self.calls.append(("set_recording", values))

    def begin_private_remote_access(self):
        self.calls.append(("begin_private_remote_access", {}))
        if self.remote_approval_url is not None:
            return {
                "mode": "private-vpn",
                "state": "approval-required",
                "local_url": "http://osecameras.local",
                "remote_url": None,
                "public_access": False,
                "approval_url": self.remote_approval_url,
            }
        self.access.update(
            mode="private-vpn",
            state="ready",
            remote_url="https://osecameras.owner.ts.net",
            public_access=False,
        )
        return dict(self.access)

    def push_public_key(self):
        return "test-public-key"

    def subscribe_push(self, subscription):
        self.calls.append(("subscribe_push", {"subscription": subscription}))
        self.push_subscription = subscription
        return "test-subscription-id"

    def unsubscribe_push(self, endpoint):
        self.calls.append(("unsubscribe_push", {"endpoint": endpoint}))
        self.push_subscription = None

    def send_test_push(self, endpoint):
        self.calls.append(("send_test_push", {"endpoint": endpoint}))

    def disable_private_remote_access(self):
        self.calls.append(("disable_private_remote_access", {}))
        self.access.update(
            mode="off", state="local-only", remote_url=None, public_access=False
        )
        return dict(self.access)

    def setup_owner_gateway_access(self, **values):
        self.calls.append(("setup_owner_gateway_access", values))
        self.access.update(
            mode="owner-gateway",
            state="ready",
            remote_url=f"https://{values['domain']}",
            public_access=True,
            gateway_owned_by_customer=True,
        )
        return dict(self.access)

    def list_recordings(self, **values):
        self.calls.append(("list_recordings", values))
        return tuple(self.recordings)

    def list_video_events(self, **values):
        self.calls.append(("list_video_events", values))
        events = tuple(self.video_events)
        camera_id = values.get("camera_id")
        if camera_id is not None:
            events = tuple(
                event for event in events if event.get("camera_id") == camera_id
            )
        return events

    def video_engine_status(self):
        return dict(self.video_engine)

    def read_video_event_preview(self, **values):
        self.calls.append(("read_video_event_preview", values))
        return b"video-event-preview"

    def read_video_event_vod(self, **values):
        self.calls.append(("read_video_event_vod", values))
        resource = values.get("resource")
        if resource == "master.m3u8":
            return (
                b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nindex-v1.m3u8\n',
                "application/vnd.apple.mpegurl",
            )
        if resource == "index-v1.m3u8":
            return (
                b'#EXTM3U\n#EXT-X-MAP:URI="init-v1.mp4"\n#EXTINF:4.0,\nseg-1-v1.m4s\n',
                "application/vnd.apple.mpegurl",
            )
        if resource in {"init-v1.mp4", "seg-1-v1.m4s"}:
            return b"video-segment", "video/mp4"
        raise ValueError("invalid resource")

    def read_video_event_thumbnail(self, **values):
        self.calls.append(("read_video_event_thumbnail", values))
        return b"event-thumbnail"

    def save_video_event(self, **values):
        self.calls.append(("save_video_event", values))
        return {"state": "saving", "clip_id": "export-1"}

    def delete_video_event(self, **values):
        self.calls.append(("delete_video_event", values))

    def list_saved_video_clips(self, **values):
        self.calls.append(("list_saved_video_clips", values))
        clips = tuple(self.saved_video_clips)
        camera_id = values.get("camera_id")
        if camera_id is not None:
            clips = tuple(
                clip for clip in clips if clip.get("camera_id") == camera_id
            )
        return clips

    def read_saved_video_clip(self, **values):
        self.calls.append(("read_saved_video_clip", values))
        return b"saved-video-clip"

    def delete_saved_video_clip(self, **values):
        self.calls.append(("delete_saved_video_clip", values))

    def read_recording_media(self, **values):
        self.calls.append(("read_recording_media", values))
        return (
            b"--vms-frame\r\nframe\r\n--vms-frame--\r\n",
            "multipart/x-mixed-replace; boundary=vms-frame",
        )

    def read_recording_preview(self, **values):
        self.calls.append(("read_recording_preview", values))
        return b"\xff\xd8preview-frame\xff\xd9"

    def read_live_snapshot(self, **values):
        self.calls.append(("read_live_snapshot", values))
        return b"\xff\xd8live-frame\xff\xd9"

    def camera_live_metrics(self, **values):
        self.calls.append(("camera_live_metrics", values))
        return dict(self.camera_metrics)

    def camera_recording_coverage(self, **values):
        self.calls.append(("camera_recording_coverage", values))
        return {
            "state": "indexed",
            "window_seconds": 300,
            "indexed_seconds": 250.0,
            "gap_seconds": 50.0,
            "largest_gap_seconds": 30.0,
            "segment_count": 25,
            "basis": "retained main-stream index; file playability unverified",
        }

    def negotiate_live_webrtc(self, **values):
        self.calls.append(("negotiate_live_webrtc", values))
        return "v=0\r\no=answer\r\n"

    def create_kit(self, **values):
        self.calls.append(("create_kit", values))

    def prepare_provisioning(self, request, **values):
        self.provisioning_requests.append(request)
        return self.create_kit(
            camera_count=request.camera_count,
            camera_model=request.camera_model,
            wifi_country=request.network.country,
        )

    def provision_selected_camera(self, request, **values):
        self.provisioning_requests.append(request)
        self.calls.append(
            ("provision_selected_camera", {"request": request, **values})
        )

    def list_media_targets(self):
        return (self.target,)

    def flash_selected(self, **values):
        self.calls.append(("flash_selected", values))

    def verify_kit(self):
        self.calls.append(("verify_kit", {}))

    def pilot_diagnostics(self):
        return tuple(self.diagnostics)


def request(
    application,
    path="/",
    *,
    method="GET",
    fields=None,
    cookie=None,
    scheme="http",
    headers=None,
    raw_body=None,
    content_type=None,
    remote_addr="127.0.0.1",
):
    environ = {}
    setup_testing_defaults(environ)
    environ["wsgi.url_scheme"] = scheme
    target = urlsplit(path)
    environ["REQUEST_METHOD"] = method
    environ["PATH_INFO"] = target.path
    environ["QUERY_STRING"] = target.query
    environ["REMOTE_ADDR"] = remote_addr
    body = (
        urlencode(fields or {}).encode("utf-8")
        if raw_body is None
        else raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    )
    environ["wsgi.input"] = io.BytesIO(body)
    environ["CONTENT_LENGTH"] = str(len(body))
    if method == "POST":
        environ["CONTENT_TYPE"] = content_type or "application/x-www-form-urlencoded"
    if cookie is not None:
        environ["HTTP_COOKIE"] = cookie
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    captured["raw_body"] = b"".join(application(environ, start_response))
    captured["body"] = captured["raw_body"].decode("utf-8", errors="replace")
    return captured


def session_cookie(response):
    return response["headers"]["Set-Cookie"].split(";", 1)[0]


class LocalWebApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.facade = FakeFacade()
        self.application = create_application(self.facade, csrf_token="test-token")

    def post(self, path: str, *, cookie=None, **fields):
        return request(
            self.application,
            path,
            method="POST",
            fields={"csrf_token": "test-token", **fields},
            cookie=cookie,
        )

    def test_camera_registration_uses_device_auth_without_owner_session(self):
        self.facade.owner_ready = True
        payload = b'{"schema_version":1,"camera_id":"CAM-0001","port":9080}'

        response = request(
            self.application,
            "/api/v1/camera-registration",
            method="POST",
            raw_body=payload,
            content_type="application/json",
            headers={"X-VMS-Signature": "a" * 64},
            remote_addr="192.168.0.184",
        )

        self.assertTrue(response["status"].startswith("200"))
        self.assertEqual(
            self.facade.calls,
            [
                (
                    "register_camera_endpoint",
                    {
                        "payload": payload,
                        "signature": "a" * 64,
                        "source_address": "192.168.0.184",
                    },
                )
            ],
        )

        rejected = request(
            self.application,
            "/api/v1/camera-registration",
            method="POST",
            raw_body=payload,
            content_type="application/json",
            remote_addr="192.168.0.184",
        )
        self.assertTrue(rejected["status"].startswith("401"))

    def test_retail_navigation_is_exact_and_pages_avoid_infrastructure_terms(self):
        expected = ["Feeds", "Settings"]
        for path in ("/settings", "/live", "/events", "/alerts", "/update"):
            with self.subTest(path=path):
                response = request(self.application, path)
                self.assertTrue(response["status"].startswith("200"))
                nav = re.search(r'<nav aria-label="Main">(.*?)</nav>', response["body"], re.DOTALL).group(1)
                self.assertEqual(
                    re.findall(r">([^<]+)</a>", nav),
                    expected,
                )
                lowered = response["body"].lower()
                for forbidden in ("ssh", "ip address", "sd card", "linux", "wi-fi", "ethernet"):
                    self.assertNotIn(forbidden, lowered)

        for path, location in (
            ("/recorder", "/settings"),
            ("/cameras", "/settings#cameras"),
            ("/storage", "/settings#storage"),
        ):
            with self.subTest(legacy_path=path):
                response = request(self.application, path)
                self.assertTrue(response["status"].startswith("303"))
                self.assertEqual(response["headers"]["Location"], location)

    def test_untrusted_camera_values_are_html_escaped(self):
        self.facade.camera_name = '<script>alert("secret")</script>'
        response = request(self.application, "/settings")
        self.assertNotIn("<script>", response["body"])
        self.assertIn("&lt;script&gt;alert(&quot;secret&quot;)&lt;/script&gt;", response["body"])

    def test_root_follows_the_three_step_consumer_setup(self):
        self.facade.setup_next = "owner"
        owner = request(self.application, "/")["body"]
        self.assertIn("Step 1 of 3", owner)
        self.assertIn("Confirm password", owner)
        self.assertEqual(owner.count('minlength="12"'), 2)

        too_short = self.post(
            "/actions/owner",
            name="Owner",
            password="example",
            password_confirm="example",
        )
        self.assertTrue(too_short["status"].startswith("400"))
        self.assertIn("at least 12 characters", too_short["body"])
        self.assertEqual(self.facade.calls, [])

        mismatch = self.post(
            "/actions/owner",
            name="Owner",
            password="example-owner-password",
            password_confirm="different-owner-password",
        )
        self.assertTrue(mismatch["status"].startswith("400"))
        self.assertEqual(self.facade.calls, [])

        self.facade.setup_next = "storage"
        storage = request(self.application, "/")["body"]
        self.assertIn("Step 2 of 3", storage)
        self.assertIn("Use Recording Drive", storage)
        self.assertNotIn("Initialization confirmation", storage)

        self.facade.setup_next = "cameras"
        self.facade.camera_state = "ready"
        cameras = request(self.application, "/")["body"]
        self.assertIn("Step 3 of 3", cameras)
        self.assertIn("Save Name", cameras)

        self.facade.setup_next = "ready"
        live = request(self.application, "/")["body"]
        self.assertIn("<h1>Feeds</h1>", live)

    def test_ready_storage_page_shows_capacity_instead_of_setup(self):
        self.facade.storage_status = {
            "state": "ready",
            "total_bytes": 1_000_000_000_000,
            "used_bytes": 44_000_000_000,
            "free_bytes": 956_000_000_000,
            "available_bytes": 955_750_000_000,
            "reserved_bytes": 250_000_000,
        }

        page = request(self.application, "/settings")["body"]

        self.assertIn("Recordings are being kept on the attached drive", page)
        self.assertIn("44.0 GB", page)
        self.assertIn("955.8 GB", page)
        self.assertIn("1.0 TB", page)
        self.assertNotIn("Initialize Storage", page)
        for private_detail in ("volume-1", "STORAGE-123"):
            self.assertNotIn(private_detail, page)

    def test_settings_offer_only_simple_retention_choices(self):
        page = request(self.application, "/settings")["body"]

        self.assertIn('<select id="days" name="days">', page)
        for days in (1, 3, 7, 14, 30):
            self.assertIn(f'<option value="{days}"', page)
        self.assertIn('<option value="7" selected>7 days</option>', page)
        self.assertNotIn('name="days" type="number"', page)

        rejected = self.post(
            "/actions/recording", mode="continuous", days="21"
        )
        self.assertTrue(rejected["status"].startswith("400"))

    def test_settings_are_compact_expandable_rows_without_internal_owner_state(self):
        self.facade.owner_ready = True
        signed_in = self.post(
            "/actions/login", password=self.facade.owner_password
        )

        page = request(
            self.application, "/settings", cookie=session_cookie(signed_in)
        )["body"]

        self.assertIn('class="settings-heading"', page)
        self.assertIn('<details class="settings-row" id="cameras">', page)
        self.assertIn('<span class="settings-row-value">1 of 2 online</span>', page)
        self.assertIn('<span class="settings-row-value">Keep 7 days</span>', page)
        self.assertEqual(page.count('class="settings-row"'), 7)
        self.assertNotIn('<section class="summary">', page)
        self.assertNotIn('<span>Owner</span>', page)
        self.assertIn("#50abe7", page.lower())
        self.assertIn("#edf2fb", page.lower())
        self.assertIn('<div class="brand">OSE Cameras</div>', page)
        self.assertIn('<div class="header-actions">', page)

    def test_real_application_reaches_ready_from_empty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "source"
            repository.mkdir()
            volume = root / "recording-volume"
            volume.mkdir()
            storage_target = RecordingStorageTarget(
                stable_id="STORAGE-ACCEPTANCE",
                display_name="Recording Drive (1.0 TB)",
                confirmation="INITIALIZE STORAGE-ACCEPTANCE",
                root=volume,
                volume_identity="linux-uuid:acceptance-volume",
            )

            class StorageDiscovery:
                def list_targets(self):
                    return (storage_target,)

            application_service = VMSApplication(
                root / "private-data",
                repository=repository,
                recording_storage_discovery=StorageDiscovery(),
            )
            application = create_application(
                application_service, csrf_token="acceptance-token"
            )

            self.assertIn("Step 1 of 3", request(application, "/")["body"])
            owner = request(
                application,
                "/actions/owner",
                method="POST",
                fields={
                    "csrf_token": "acceptance-token",
                    "name": "Owner",
                    "password": "example-owner-password",
                    "password_confirm": "example-owner-password",
                },
            )
            self.assertTrue(owner["status"].startswith("303"))
            cookie = session_cookie(owner)
            self.assertIn(
                "Step 2 of 3", request(application, "/", cookie=cookie)["body"]
            )

            storage = request(
                application,
                "/actions/storage",
                method="POST",
                fields={
                    "csrf_token": "acceptance-token",
                    "storage_id": storage_target.stable_id,
                },
                cookie=cookie,
            )
            self.assertTrue(storage["status"].startswith("303"))
            application_service.recorder.register_camera(
                camera_id="CAM-INCLUDED",
                claim_secret_hash="0" * 64,
                identity_reference="identity:CAM-INCLUDED",
                pre_enrolled=True,
            )
            self.assertTrue(application_service.status()["setup"]["ready"])
            self.assertIn(
                "<h1>Feeds</h1>", request(application, "/", cookie=cookie)["body"]
            )
            self.assertIn("Camera 1", request(application, "/", cookie=cookie)["body"])

            named = request(
                application,
                "/actions/camera-name",
                method="POST",
                fields={
                    "csrf_token": "acceptance-token",
                    "camera_id": "CAM-INCLUDED",
                    "name": "Driveway",
                },
                cookie=cookie,
            )
            self.assertTrue(named["status"].startswith("303"))
            self.assertTrue(application_service.status()["setup"]["ready"])
            self.assertIn("Driveway", request(application, "/", cookie=cookie)["body"])

    def test_live_wall_selects_camera_in_place_and_exposes_actions(self):
        self.facade.setup_next = "ready"
        self.facade.camera_state = "ready"
        self.facade.camera_connected = True

        page = request(self.application, "/")
        image = request(self.application, "/live.jpg?slot=1")
        detail = request(self.application, "/camera?slot=1")

        self.assertIn("<h1>Feeds</h1>", page["body"])
        self.assertIn('<body class="wall-page">', page["body"])
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", page["body"])
        self.assertIn("data-camera-card", page["body"])
        self.assertNotIn('data-camera-name="Driveway" hidden', page["body"])
        self.assertIn("data-select-camera", page["body"])
        self.assertNotIn('<a class="live-card"', page["body"])
        self.assertIn('href="/events?camera=1"', page["body"])
        self.assertNotIn('href="/camera?', page["body"])
        self.assertIn("Recording: Active", page["body"])
        self.assertIn('id="camera-1-diagnostics"', page["body"])
        self.assertIn("data-live-path", page["body"])
        self.assertIn("data-live-connection", page["body"])
        self.assertIn("data-live-rate", page["body"])
        self.assertIn("data-live-signal", page["body"])
        self.assertIn("data-live-status", page["body"])
        self.assertIn(
            'data-live-metrics="/api/v1/cameras/1/metrics"', page["body"]
        )
        self.assertIn("Check camera stream now", page["body"])
        self.assertIn("Check saved video now", page["body"])
        self.assertIn("data-fullscreen", page["body"])
        self.assertIn("data-rename-camera", page["body"])
        self.assertIn('action="/actions/camera-name-slot"', page["body"])
        self.assertIn('value="Driveway"', page["body"])
        self.assertIn('action="/actions/camera-position"', page["body"])
        self.assertIn("data-drag-camera", page["body"])
        self.assertIn('aria-label="Move earlier" disabled', page["body"])
        self.assertIn('aria-label="Move later" disabled', page["body"])
        self.assertIn('src="/live.jpg?slot=1"', page["body"])
        self.assertIn(
            'data-live-webrtc="/api/v1/cameras/1/webrtc"', page["body"]
        )
        self.assertIn('<script src="/live.js" defer></script>', page["body"])
        self.assertIn("grid-template-columns:1fr;grid-auto-rows:auto", page["body"])
        self.assertNotIn("calc(100vh - 92px)", page["body"])
        self.assertNotIn("CAM-0001", page["body"])
        self.assertTrue(image["status"].startswith("200"))
        self.assertEqual(image["headers"]["Content-Type"], "image/jpeg")
        self.assertEqual(image["raw_body"], b"\xff\xd8live-frame\xff\xd9")
        self.assertTrue(detail["status"].startswith("303"))
        self.assertEqual(detail["headers"]["Location"], "/#camera-1")
        self.assertEqual(
            self.facade.calls,
            [("read_live_snapshot", {"camera_id": "CAM-0001"})],
        )

    def test_offline_camera_collapses_into_named_status_strip(self):
        self.facade.setup_next = "ready"
        self.facade.camera_state = "ready"
        self.facade.camera_connected = False

        page = request(self.application, "/")
        script = request(self.application, "/live.js")

        self.assertIn('data-camera-name="Driveway" hidden', page["body"])
        self.assertIn("<span data-offline-count>1</span> offline", page["body"])
        self.assertIn("<span data-offline-names>Driveway</span>", page["body"])
        self.assertNotIn("Manage cameras", page["body"])
        self.assertIn("consecutiveFailures >= 3", script["body"])
        self.assertIn("RTCPeerConnection", script["body"])
        self.assertIn("requestVideoFrameCallback", script["body"])
        self.assertIn("Snapshots (fallback)", script["body"])
        self.assertIn("refreshSignal", script["body"])
        self.assertIn("5000", script["body"])
        self.assertIn("skipped ${skipped}/s", script["body"])
        self.assertIn("Saved-video check unavailable", script["body"])
        self.assertIn("Checking saved video", script["body"])
        self.assertIn("View ${fps.toFixed(1)} FPS", script["body"])

    def test_camera_wall_position_action_resolves_public_slot_to_camera(self):
        self.facade.setup_next = "ready"
        self.facade.camera_state = "ready"

        response = self.post(
            "/actions/camera-position",
            slot="1",
            position="1",
        )

        self.assertTrue(response["status"].startswith("303"))
        self.assertEqual(response["headers"]["Location"], "/?notice=camera-moved")
        self.assertEqual(
            self.facade.calls,
            [("position_camera", {"camera_id": "CAM-0001", "position": 1})],
        )

    def test_camera_wall_rename_action_resolves_public_slot_to_camera(self):
        self.facade.setup_next = "ready"
        self.facade.camera_state = "ready"

        response = self.post(
            "/actions/camera-name-slot",
            slot="1",
            name="Side Gate",
        )

        self.assertTrue(response["status"].startswith("303"))
        self.assertEqual(response["headers"]["Location"], "/?notice=camera-named")
        self.assertEqual(
            self.facade.calls,
            [("name_camera", {"camera_id": "CAM-0001", "name": "Side Gate"})],
        )

    def test_camera_recording_selector_saves_without_disabling_live_view(self):
        self.facade.camera_state = "ready"
        feeds = request(self.application, "/")

        response = self.post(
            "/actions/camera-recording-slot", slot="1", enabled="false"
        )
        paused = request(self.application, "/")
        image = request(self.application, "/live.jpg?slot=1")

        self.assertIn("Recording: Active", feeds["body"])
        self.assertIn("Pause recording", feeds["body"])
        self.assertNotIn('id="camera-1-recording-state"', feeds["body"])
        self.assertTrue(response["status"].startswith("303"))
        self.assertEqual(
            response["headers"]["Location"],
            "/?notice=camera-recording-saved#camera-1",
        )
        self.assertIn("Recording: Paused", paused["body"])
        self.assertIn("Resume recording", paused["body"])
        self.assertTrue(image["status"].startswith("200"))
        self.assertIn(
            (
                "set_camera_recording",
                {"camera_id": "CAM-0001", "enabled": False},
            ),
            self.facade.calls,
        )

    def test_global_and_camera_alert_selectors_save_independent_preferences(self):
        self.facade.camera_state = "ready"
        feeds = request(self.application, "/")

        camera_response = self.post(
            "/actions/camera-alerts-slot", slot="1", enabled="false"
        )
        global_response = self.post("/actions/alerts", enabled="false")
        paused = request(self.application, "/")

        self.assertIn("Alerts: Active", feeds["body"])
        self.assertIn("Pause alerts", feeds["body"])
        self.assertNotIn('id="camera-1-alerts-state"', feeds["body"])
        self.assertEqual(
            camera_response["headers"]["Location"],
            "/?notice=camera-alerts-saved#camera-1",
        )
        self.assertEqual(
            global_response["headers"]["Location"],
            "/?notice=alerts-saved#alerts",
        )
        self.assertIn("Alerts: Paused", paused["body"])
        self.assertIn("Resume alerts", paused["body"])
        self.assertIn(
            ("set_camera_alerts", {"camera_id": "CAM-0001", "enabled": False}),
            self.facade.calls,
        )
        self.assertIn(
            ("set_alerts_enabled", {"enabled": False}),
            self.facade.calls,
        )

    def test_camera_dropdown_runs_real_checks_and_returns_to_diagnostics(self):
        self.facade.camera_state = "ready"
        detail = request(self.application, "/")

        response = self.post("/actions/camera-check-slot", slot="1")
        checked = request(self.application, "/")

        self.assertIn("Check camera stream now", detail["body"])
        self.assertIn("data-live-camera-stream", detail["body"])
        self.assertTrue(response["status"].startswith("303"))
        self.assertEqual(
            response["headers"]["Location"],
            "/?notice=camera-checked#camera-1-diagnostics",
        )
        self.assertEqual(
            self.facade.calls,
            [("verify_recorder_camera", {"camera_id": "CAM-0001"})],
        )
        self.assertIn("Last checked: 2026-08-29T20:30:00+00:00", checked["body"])
        self.assertIn('<dd class="health-ok">OK</dd>', checked["body"])

    def test_owner_session_protects_pages_api_and_media(self):
        self.facade.owner_ready = True
        self.facade.setup_next = "ready"
        self.facade.camera_state = "ready"

        page = request(self.application, "/")
        login = request(self.application, "/login")
        api = request(self.application, "/api/v1/cameras")
        snapshot = request(self.application, "/live.jpg?slot=1")
        preview = request(
            self.application,
            "/recording-preview.jpg?reference=private-recording",
        )

        self.assertTrue(page["status"].startswith("303"))
        self.assertEqual(page["headers"]["Location"], "/login")
        self.assertTrue(login["status"].startswith("200"))
        self.assertIn("Use the recorder owner password.", login["body"])
        self.assertNotIn("Driveway", login["body"])
        self.assertTrue(api["status"].startswith("401"))
        self.assertEqual(api["headers"]["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(api["body"])["error"]["code"], "unauthorized")
        self.assertTrue(snapshot["status"].startswith("401"))
        webrtc = request(
            self.application,
            "/api/v1/cameras/1/webrtc",
            method="POST",
            raw_body="v=0\r\no=offer\r\n",
            content_type="application/sdp",
        )
        self.assertTrue(webrtc["status"].startswith("401"))
        self.assertTrue(preview["status"].startswith("401"))
        self.assertNotIn("read_live_snapshot", [call[0] for call in self.facade.calls])
        self.assertNotIn("read_recording_preview", [call[0] for call in self.facade.calls])

    def test_owner_can_sign_in_use_the_wall_and_log_out(self):
        self.facade.owner_ready = True
        self.facade.setup_next = "ready"
        self.facade.camera_state = "ready"

        incorrect = self.post(
            "/actions/login", password="test-placeholder-incorrect-password"
        )
        signed_in = self.post(
            "/actions/login", password="example-owner-password"
        )
        cookie = session_cookie(signed_in)
        wall = request(self.application, "/", cookie=cookie)
        snapshot = request(self.application, "/live.jpg?slot=1", cookie=cookie)
        signed_out = request(
            self.application,
            "/actions/logout",
            method="POST",
            fields={"csrf_token": "test-token"},
            cookie=cookie,
        )
        expired = request(self.application, "/", cookie=cookie)

        self.assertTrue(incorrect["status"].startswith("401"))
        self.assertIn("Password is incorrect.", incorrect["body"])
        self.assertNotIn("Set-Cookie", incorrect["headers"])
        self.assertTrue(signed_in["status"].startswith("303"))
        self.assertEqual(signed_in["headers"]["Location"], "/")
        set_cookie = signed_in["headers"]["Set-Cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Max-Age=2592000", set_cookie)
        self.assertTrue(wall["status"].startswith("200"))
        self.assertIn("<h1>Feeds</h1>", wall["body"])
        self.assertIn('action="/actions/logout"', wall["body"])
        self.assertTrue(snapshot["status"].startswith("200"))
        self.assertTrue(signed_out["status"].startswith("303"))
        self.assertEqual(signed_out["headers"]["Location"], "/login")
        self.assertIn("Max-Age=0", signed_out["headers"]["Set-Cookie"])
        self.assertTrue(expired["status"].startswith("303"))
        self.assertEqual(expired["headers"]["Location"], "/login")

    def test_owner_login_is_rate_limited_after_repeated_failures(self):
        self.facade.owner_ready = True

        for attempt in range(5):
            with self.subTest(attempt=attempt):
                response = self.post(
                    "/actions/login", password=f"incorrect-password-{attempt}"
                )
                self.assertTrue(response["status"].startswith("401"))

        limited = self.post("/actions/login", password="example-owner-password")

        self.assertTrue(limited["status"].startswith("429"))
        self.assertEqual(limited["headers"]["Retry-After"], "60")
        self.assertIn("Wait one minute", limited["body"])

    def test_owner_session_expires_and_https_cookie_is_secure(self):
        clock = [100.0]
        self.facade.owner_ready = True
        self.facade.setup_next = "ready"
        application = create_application(
            self.facade,
            csrf_token="test-token",
            time_source=lambda: clock[0],
        )
        signed_in = request(
            application,
            "/actions/login",
            method="POST",
            fields={
                "csrf_token": "test-token",
                "password": "example-owner-password",
            },
            scheme="https",
        )
        cookie = session_cookie(signed_in)

        self.assertIn("; Secure", signed_in["headers"]["Set-Cookie"])
        self.assertTrue(request(application, "/", cookie=cookie)["status"].startswith("200"))
        clock[0] += 30 * 24 * 60 * 60 + 1
        expired = request(application, "/", cookie=cookie)
        self.assertTrue(expired["status"].startswith("303"))
        self.assertEqual(expired["headers"]["Location"], "/login")

    def test_versioned_read_api_hides_camera_identity_and_serves_live_media(self):
        self.facade.camera_state = "ready"
        self.facade.camera_connected = True
        self.facade.recordings = [
            {
                "id": "segment-1",
                "camera_id": "CAM-0001",
                "started_at": "2026-08-29T12:00:00+00:00",
                "reference": "private-reference",
            }
        ]

        discovery = request(self.application, "/api/v1")
        status = request(self.application, "/api/v1/status")
        cameras = request(self.application, "/api/v1/cameras")
        storage = request(self.application, "/api/v1/storage")
        camera = request(self.application, "/api/v1/cameras/1")
        metrics = request(self.application, "/api/v1/cameras/1/metrics")
        coverage = request(self.application, "/api/v1/cameras/1/recording-coverage")
        snapshot = request(self.application, "/api/v1/cameras/1/snapshot")
        recordings = request(self.application, "/api/v1/recordings?limit=10")

        for response in (
            discovery,
            status,
            cameras,
            storage,
            camera,
            metrics,
            coverage,
            recordings,
        ):
            self.assertTrue(response["status"].startswith("200"))
            self.assertEqual(
                response["headers"]["Content-Type"],
                "application/json; charset=utf-8",
            )
            self.assertEqual(json.loads(response["body"])["api_version"], "v1")
        self.assertEqual(
            json.loads(discovery["body"])["links"]["storage"],
            "/api/v1/storage",
        )
        self.assertEqual(json.loads(storage["body"])["storage"], {"state": "planned"})
        camera_document = json.loads(camera["body"])["camera"]
        self.assertEqual(camera_document["slot"], 1)
        self.assertEqual(camera_document["name"], "Driveway")
        self.assertEqual(
            camera_document["links"]["metrics"],
            "/api/v1/cameras/1/metrics",
        )
        self.assertEqual(
            json.loads(metrics["body"])["metrics"],
            {
                "rssi_dbm": -67,
                "signal_percent": 61,
                "detect_input_fps": 5.2,
                "skipped_fps": 0.1,
                "connection_quality": "fair",
                "source_stalls_last_hour": 12,
                "source_reconnects_last_hour": 2,
                "recorder_cpu_percent": 78.9,
                "source_codec": "mjpeg",
                "source_fps": 10,
                "recorder_transcoding": True,
                "diagnoses": ["source-interruptions", "legacy-transcoding"],
                "diagnosis": "source-interruptions",
            },
        )
        self.assertEqual(
            json.loads(coverage["body"])["coverage"]["indexed_seconds"], 250.0
        )
        self.assertEqual(
            camera_document["links"]["recording_coverage"],
            "/api/v1/cameras/1/recording-coverage",
        )
        self.assertNotIn("CAM-0001", cameras["body"])
        self.assertNotIn("private-reference", recordings["body"])
        self.assertIn(("camera_live_metrics", {"camera_id": "CAM-0001"}), self.facade.calls)
        self.assertIn(("camera_recording_coverage", {"camera_id": "CAM-0001"}), self.facade.calls)
        self.assertTrue(snapshot["status"].startswith("200"))
        self.assertEqual(snapshot["headers"]["Content-Type"], "image/jpeg")

        offer = "v=0\r\no=browser-offer\r\n"
        webrtc = request(
            self.application,
            "/api/v1/cameras/1/webrtc",
            method="POST",
            raw_body=offer,
            content_type="application/sdp",
        )
        self.assertTrue(webrtc["status"].startswith("200"))
        self.assertEqual(webrtc["headers"]["Content-Type"], "application/sdp")
        self.assertTrue(webrtc["body"].startswith("v=0"))
        self.assertIn(
            (
                "negotiate_live_webrtc",
                {"camera_id": "CAM-0001", "offer": offer},
            ),
            self.facade.calls,
        )

        wrong_type = request(
            self.application,
            "/api/v1/cameras/1/webrtc",
            method="POST",
            raw_body=offer,
            content_type="text/plain",
        )
        self.assertTrue(wrong_type["status"].startswith("415"))

        invalid = request(self.application, "/api/v1/cameras/999")
        self.assertTrue(invalid["status"].startswith("404"))
        self.assertEqual(json.loads(invalid["body"])["error"]["code"], "not_found")

    def test_camera_setup_status_is_bounded_and_hides_internal_identities(self):
        self.facade.camera_setup = {
            "camera_id": "CAM-0001",
            "transaction_id": "private-transaction",
            "bootstrap_identity": "sha256:private-bootstrap",
            "permanent_identity": "identity:private-permanent",
            "state": "needs-attention",
            "transport": "dpp",
            "issue": "video-verification-failed",
            "updated_at": 1_790_642_400.0,
        }

        cameras = request(self.application, "/api/v1/cameras")
        setup = request(self.application, "/api/v1/cameras/1/setup")
        settings = request(self.application, "/settings")

        self.assertTrue(setup["status"].startswith("200"))
        document = json.loads(setup["body"])["setup"]
        self.assertEqual(
            document,
            {
                "state": "needs-attention",
                "transport": "dpp",
                "issue": "video-verification-failed",
                "updated_at": 1_790_642_400.0,
            },
        )
        self.assertEqual(
            json.loads(cameras["body"])["cameras"][0]["links"]["setup"],
            "/api/v1/cameras/1/setup",
        )
        for private_value in (
            "CAM-0001",
            "private-transaction",
            "private-bootstrap",
            "private-permanent",
        ):
            self.assertNotIn(private_value, cameras["body"])
            self.assertNotIn(private_value, setup["body"])
        self.assertIn("Camera connected, but video failed", settings["body"])
        self.assertIn('action="/actions/camera-setup-retry-slot"', settings["body"])

        retried = self.post(
            "/actions/camera-setup-retry-slot",
            slot="1",
        )

        self.assertTrue(retried["status"].startswith("303"))
        self.assertEqual(
            retried["headers"]["Location"],
            "/settings?notice=camera-setup-retried#cameras",
        )
        self.assertIn(
            ("retry_camera_network_setup", {"camera_id": "CAM-0001"}),
            self.facade.calls,
        )

    def test_camera_setup_status_is_absent_when_no_transaction_exists(self):
        camera = request(self.application, "/api/v1/cameras/1")
        setup = request(self.application, "/api/v1/cameras/1/setup")

        self.assertNotIn("setup", json.loads(camera["body"])["camera"])
        self.assertTrue(setup["status"].startswith("404"))

    def test_real_capture_is_listed_and_playable_through_the_portal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "source"
            repository.mkdir()
            volume = root / "recording-volume"
            volume.mkdir()
            target = RecordingStorageTarget(
                stable_id="STORAGE-ACCEPTANCE",
                display_name="Recording Drive (1.0 TB)",
                confirmation="INITIALIZE STORAGE-ACCEPTANCE",
                root=volume,
                volume_identity="linux-uuid:acceptance-volume",
            )

            class StorageDiscovery:
                def list_targets(self):
                    return (target,)

            class Source:
                camera_id = "CAM-INCLUDED"

                def get_bytes(self, path, *, maximum):
                    self.assertions = (path, maximum)
                    return b"\xff\xd8current-live-frame\xff\xd9"

                def get_mjpeg_frames(
                    self, path, *, maximum, count, timeout_seconds
                ):
                    return (
                        b"\xff\xd8first-frame\xff\xd9",
                        b"\xff\xd8second-frame\xff\xd9",
                    )

            service = VMSApplication(
                root / "private-data",
                repository=repository,
                recording_storage_discovery=StorageDiscovery(),
                recording_source_provider=lambda: (Source(),),
            )
            service.recorder.register_camera(
                camera_id="CAM-INCLUDED",
                claim_secret_hash="0" * 64,
                identity_reference="identity:CAM-INCLUDED",
                pre_enrolled=True,
            )
            service.name_camera(camera_id="CAM-INCLUDED", name="Driveway")
            service.initialize_selected_recording_storage(
                stable_id=target.stable_id,
                confirmation=target.confirmation,
            )
            service.set_recording(mode="continuous", days=21)
            service.capture_recording_cycle(frame_count=2, maximum_bytes=4096)
            portal = create_application(service, csrf_token="acceptance-token")

            events = request(portal, "/events")
            preview_source = unescape(
                re.search(r'<img src="([^"]+)"', events["body"]).group(1)
            )
            media_source = unescape(
                re.search(r'data-event-source="([^"]+)"', events["body"]).group(1)
            )
            preview = request(portal, preview_source)
            media = request(portal, media_source)
            live = request(portal, "/live.jpg?slot=1")

            self.assertIn("Driveway recording", events["body"])
            self.assertEqual(preview["raw_body"], b"\xff\xd8first-frame\xff\xd9")
            self.assertTrue(media["status"].startswith("200"))
            self.assertIn(b"first-frame", media["raw_body"])
            self.assertIn(b"second-frame", media["raw_body"])
            self.assertTrue(live["status"].startswith("200"))
            self.assertEqual(live["raw_body"], b"\xff\xd8current-live-frame\xff\xd9")
            self.assertTrue(service.list_cameras()[0]["connected"])

    def test_camera_health_uses_plain_statuses_and_hides_internal_details(self):
        self.facade.camera_extra = {
            "ip_address": "192.0.2.44",
            "ssh_key": "/private/CAM-0001.key",
            "operating_system": "Linux",
            "stream_secret": "private-stream-secret",
        }

        page = request(self.application, "/")["body"]

        self.assertIn("Diagnostics", page)
        for name in ("Power", "Network", "Sensor", "Live Video", "Recorder"):
            self.assertIn(f"<dt>{name}</dt>", page)
        self.assertIn("Needs attention", page)
        self.assertIn("Not checked", page)
        for private_detail in (
            "192.0.2.44",
            "/private/CAM-0001.key",
            "Linux",
            "private-stream-secret",
        ):
            self.assertNotIn(private_detail, page)

    def test_support_report_is_downloadable_json_and_redacted(self):
        self.facade.diagnostic_history = lambda: {"schema_version": 1, "samples": [], "events": []}
        self.facade.camera_extra = {
            "private_network_ssh": "camera-001.local",
            "credentials_file": "/private/camera-001.json",
            "stream_secret": "private-stream-secret",
        }

        response = request(self.application, "/support-report.json")
        report = json.loads(response["body"])

        self.assertTrue(response["status"].startswith("200"))
        self.assertEqual(
            response["headers"]["Content-Type"], "application/json; charset=utf-8"
        )
        self.assertEqual(
            response["headers"]["Content-Disposition"],
            'attachment; filename="camera-health-support-report.json"',
        )
        self.assertEqual(report["report"], "Camera Health")
        self.assertEqual(report["diagnostics"]["schema_version"], 1)
        self.assertEqual(
            tuple(report["cameras"][0]["statuses"]),
            ("Power", "Network", "Sensor", "Live Video", "Recorder"),
        )
        output = response["body"].lower()
        for private_detail in (
            "ssh",
            "camera-001.local",
            "/private/",
            "private-stream-secret",
        ):
            self.assertNotIn(private_detail, output)

    def test_system_log_is_linked_from_settings_and_requires_owner_login(self):
        self.assertIn('href="/diagnostics"', request(self.application,"/settings")["body"])
        page=request(self.application,"/diagnostics?sample=9999")
        self.assertTrue(page["status"].startswith("200"))
        self.assertIn('No measurements saved yet',page["body"])
        self.facade.owner_ready=True
        private=request(self.application,"/diagnostics")
        self.assertEqual(private["headers"]["Location"],"/login")

    def test_owner_downloads_an_encrypted_backup_from_recorder_settings(self):
        page = request(self.application, "/settings")["body"]
        self.assertIn("Download Encrypted Backup", page)
        self.assertIn("Video recordings stay on the recording drive", page)

        response = self.post(
            "/actions/backup", password=self.facade.owner_password
        )

        self.assertTrue(response["status"].startswith("200"))
        self.assertEqual(response["raw_body"], b"encrypted-backup")
        self.assertEqual(
            response["headers"]["Content-Type"], "application/octet-stream"
        )
        self.assertEqual(
            response["headers"]["Content-Disposition"],
            'attachment; filename="vms-recorder-backup-20260830T000000Z.vms-backup"',
        )
        self.assertEqual(
            self.facade.calls[-1],
            (
                "create_backup_download",
                {"password": self.facade.owner_password},
            ),
        )

    def test_owner_changes_password_and_other_sessions_are_revoked(self):
        self.facade.owner_ready = True
        self.facade.setup_next = "ready"
        first_login = self.post(
            "/actions/login", password=self.facade.owner_password
        )
        second_login = self.post(
            "/actions/login", password=self.facade.owner_password
        )
        first_cookie = session_cookie(first_login)
        second_cookie = session_cookie(second_login)
        page = request(self.application, "/settings", cookie=first_cookie)

        self.assertIn("Change Password", page["body"])
        self.assertNotIn("Create Owner", page["body"])
        mismatch = self.post(
            "/actions/owner-password",
            cookie=first_cookie,
            current_password=self.facade.owner_password,
            new_password="test-placeholder-new-owner-password",
            new_password_confirm="test-placeholder-different-password",
        )
        self.assertTrue(mismatch["status"].startswith("400"))

        new_password = "test-placeholder-new-owner-password"
        changed = self.post(
            "/actions/owner-password",
            cookie=first_cookie,
            current_password=self.facade.owner_password,
            new_password=new_password,
            new_password_confirm=new_password,
        )
        replacement_cookie = session_cookie(changed)

        self.assertTrue(changed["status"].startswith("303"))
        self.assertEqual(
            changed["headers"]["Location"],
            "/settings?notice=owner-password-changed",
        )
        self.assertTrue(
            request(self.application, "/", cookie=second_cookie)["status"].startswith(
                "303"
            )
        )
        self.assertTrue(
            request(self.application, "/settings", cookie=replacement_cookie)[
                "status"
            ].startswith("200")
        )
        self.assertTrue(
            self.post("/actions/login", password=new_password)["status"].startswith(
                "303"
            )
        )

    def test_recorder_settings_answer_where_to_open_the_cameras(self):
        local = request(self.application, "/settings")["body"]
        self.assertIn("http://osecameras.local", local)
        self.assertIn("Away from home", local)
        self.assertIn("Enable Remote Access", local)

        self.facade.access.update(
            mode="private-vpn",
            state="ready",
            remote_url="https://osecameras.owner.ts.net",
        )
        remote = request(self.application, "/settings")["body"]
        self.assertIn("https://osecameras.owner.ts.net", remote)
        for action in ("Open", "Share Link", "Copy Link", "Add OSE Cameras"):
            self.assertIn(action, remote)

    def test_remote_access_toggle_handles_approval_ready_and_off(self):
        self.facade.remote_approval_url = "https://login.tailscale.com/a/example-code"
        approval = self.post("/actions/remote-access", enabled="1")

        self.assertTrue(approval["status"].startswith("303"))
        self.assertIn("remote-approval", approval["headers"]["Location"])
        page = request(self.application, "/settings")["body"]
        self.assertIn("Approve once", page)
        self.assertIn("/remote-access-qr.svg?kind=approval", page)
        qr = request(self.application, "/remote-access-qr.svg?kind=approval")
        self.assertTrue(qr["status"].startswith("200"))
        self.assertEqual(qr["headers"]["Content-Type"], "image/svg+xml")
        self.assertIn("<svg", qr["body"])

        self.facade.remote_approval_url = None
        ready = self.post("/actions/remote-access", enabled="1")
        self.assertIn("remote-ready", ready["headers"]["Location"])
        open_qr = request(self.application, "/remote-access-qr.svg?kind=open")
        self.assertTrue(open_qr["status"].startswith("200"))

        disabled = self.post("/actions/remote-access", enabled="0")
        self.assertIn("remote-disabled", disabled["headers"]["Location"])
        self.assertEqual(self.facade.access["state"], "local-only")

    def test_owner_gateway_address_is_verified_before_ui_uses_it(self):
        page = request(self.application, "/settings")["body"]
        self.assertIn("Use my own HTTPS address", page)

        response = self.post(
            "/actions/owner-gateway",
            domain="cameras.example.com",
            enrollment_code="secret-code-123456",
        )

        self.assertTrue(response["status"].startswith("303"))
        self.assertIn("remote-gateway-ready", response["headers"]["Location"])
        self.assertIn(
            (
                "setup_owner_gateway_access",
                {
                    "domain": "cameras.example.com",
                    "enrollment_code": "secret-code-123456",
                },
            ),
            self.facade.calls,
        )
        self.assertIn(
            "https://cameras.example.com",
            request(self.application, "/settings")["body"],
        )

    def test_install_manifest_and_icon_are_public_but_remote_qr_is_private(self):
        self.facade.owner_ready = True

        manifest = request(self.application, "/manifest.webmanifest")
        icon = request(self.application, "/app-icon.svg")
        private_qr = request(self.application, "/remote-access-qr.svg?kind=open")

        self.assertTrue(manifest["status"].startswith("200"))
        manifest_body = json.loads(manifest["body"])
        self.assertEqual(manifest_body["name"], "OSE Cameras")
        self.assertEqual(manifest_body["theme_color"], "#50abe7")
        self.assertEqual(manifest_body["background_color"], "#edf2fb")
        self.assertIn("#50abe7", icon["body"])
        self.assertIn("#edf2fb", icon["body"])
        self.assertTrue(icon["status"].startswith("200"))
        self.assertTrue(private_qr["status"].startswith("401"))

    def test_push_setup_requires_owner_session_and_csrf(self):
        self.facade.owner_ready = True
        anonymous = request(self.application, "/api/v1/push/config")
        worker = request(self.application, "/sw.js")
        signed_in = self.post("/actions/login", password=self.facade.owner_password)
        cookie = session_cookie(signed_in)
        config = request(self.application, "/api/v1/push/config", cookie=cookie)
        page = request(self.application, "/settings", cookie=cookie)
        script = request(self.application, "/push.js", cookie=cookie)
        subscription = {"endpoint": "https://fcm.googleapis.com/fcm/send/test", "keys": {}}
        invalid_csrf = request(
            self.application,
            "/actions/push-subscribe",
            method="POST",
            cookie=cookie,
            fields={"csrf_token": "wrong", "subscription": json.dumps(subscription)},
        )
        enrolled = self.post(
            "/actions/push-subscribe", cookie=cookie, subscription=json.dumps(subscription)
        )
        tested = self.post(
            "/actions/push-test", cookie=cookie, endpoint=subscription["endpoint"]
        )
        removed = self.post(
            "/actions/push-unsubscribe", cookie=cookie, endpoint=subscription["endpoint"]
        )

        self.assertTrue(anonymous["status"].startswith("401"))
        self.assertTrue(worker["status"].startswith("200"))
        self.assertIn("notificationclick", worker["body"])
        self.assertEqual(json.loads(config["body"])["public_key"], "test-public-key")
        self.assertIn("Notifications", page["body"])
        self.assertIn('data-state="working"', page["body"])
        self.assertIn("connect-src 'self'; worker-src 'self'", page["headers"]["Content-Security-Policy"])
        self.assertIn("media-src 'self'", page["headers"]["Content-Security-Policy"])
        self.assertIn("Notification.requestPermission", script["body"])
        self.assertIn("Waiting for Chrome notification permission", script["body"])
        self.assertIn("Finishing notification setup", script["body"])
        self.assertTrue(invalid_csrf["status"].startswith("403"))
        self.assertIn("push-subscribed", enrolled["headers"]["Location"])
        self.assertIn("push-test-sent", tested["headers"]["Location"])
        self.assertIn("push-unsubscribed", removed["headers"]["Location"])
        self.assertEqual(
            [name for name, _ in self.facade.calls if name.endswith("_push")],
            ["subscribe_push", "send_test_push", "unsubscribe_push"],
        )

    def test_push_validation_exposes_only_safe_error_header(self):
        self.facade.owner_ready = True
        signed_in = self.post("/actions/login", password=self.facade.owner_password)
        response = self.post(
            "/actions/push-subscribe",
            cookie=session_cookie(signed_in),
            subscription="[]",
        )

        self.assertTrue(response["status"].startswith("400"))
        self.assertEqual(
            response["headers"]["X-VMS-Error"],
            "The phone's notification details are invalid.",
        )

    def test_backup_download_rejects_wrong_password_and_csrf(self):
        wrong_password = self.post(
            "/actions/backup", password="test-placeholder-incorrect-owner-password"
        )
        wrong_csrf = request(
            self.application,
            "/actions/backup",
            method="POST",
            fields={"csrf_token": "wrong", "password": self.facade.owner_password},
        )

        self.assertTrue(wrong_password["status"].startswith("401"))
        self.assertTrue(wrong_csrf["status"].startswith("403"))
        self.assertNotIn(
            "create_backup_download", [name for name, _ in self.facade.calls]
        )

    def test_events_list_recordings_and_serve_the_selected_media(self):
        reference = "recordings/CAM-0001/2026/08/28/120000000000Z-segment-1"
        self.facade.recordings = [
            {
                "id": "segment-1",
                "camera_id": "CAM-0001",
                "started_at": "2026-08-28T12:00:00+00:00",
                "reference": reference,
            }
        ]

        page = request(self.application, "/events")
        media = request(
            self.application,
            "/recording.mjpeg?" + urlencode({"reference": reference}),
        )
        download = request(
            self.application,
            "/recording.mjpeg?"
            + urlencode({"reference": reference, "download": "1"}),
        )
        preview = request(
            self.application,
            "/recording-preview.jpg?" + urlencode({"reference": reference}),
        )

        self.assertIn("Driveway recording", page["body"])
        self.assertIn("/recording.mjpeg?reference=", page["body"])
        self.assertIn("/recording-preview.jpg?reference=", page["body"])
        self.assertIn("data-event-toggle", page["body"])
        self.assertIn("Download</a>", page["body"])
        self.assertIn('<script src="/events.js" defer></script>', page["body"])
        self.assertTrue(media["status"].startswith("200"))
        self.assertTrue(media["raw_body"].startswith(b"--vms-frame"))
        self.assertEqual(download["raw_body"], media["raw_body"])
        self.assertEqual(
            download["headers"]["Content-Disposition"],
            'attachment; filename="vms-recording-120000000000Z-segment-1.mjpeg"',
        )
        self.assertTrue(preview["status"].startswith("200"))
        self.assertEqual(preview["headers"]["Content-Type"], "image/jpeg")
        self.assertEqual(preview["raw_body"], b"\xff\xd8preview-frame\xff\xd9")
        self.assertEqual(
            self.facade.calls[-1],
            ("read_recording_preview", {"reference": reference}),
        )

    def test_events_show_engine_alert_and_serve_protected_preview(self):
        event_id = "frigate:review-123"
        self.facade.video_events = [
            {
                "id": event_id,
                "camera_id": "CAM-0001",
                "started_at": "2026-09-22T12:00:00+00:00",
                "ended_at": "2026-09-22T12:00:10+00:00",
                "severity": "alert",
                "labels": ("person",),
                "zones": (),
            }
        ]

        page = request(self.application, "/events")
        preview = request(
            self.application,
            "/event-preview.mp4?" + urlencode({"event": event_id}),
        )
        thumbnail = request(
            self.application,
            "/event-thumbnail.jpg?" + urlencode({"event": event_id}),
        )

        self.assertIn("Driveway", page["body"])
        self.assertIn("Person alert", page["body"])
        self.assertIn('<span class="event-duration">10 sec</span>', page["body"])
        self.assertIn("/event-thumbnail.jpg?event=frigate%3Areview-123", page["body"])
        self.assertIn(
            "/event-vod?event=frigate%3Areview-123&amp;resource=master.m3u8",
            page["body"],
        )
        self.assertIn("/event-preview.mp4?event=frigate%3Areview-123", page["body"])
        self.assertIn("data-event-video", page["body"])
        self.assertIn('loading="lazy"', page["body"])
        self.assertNotIn('<video controls preload="metadata"', page["body"])
        self.assertIn("Save Clip", page["body"])
        self.assertIn("Download", page["body"])
        self.assertIn("Delete", page["body"])
        self.assertTrue(thumbnail["status"].startswith("200"))
        self.assertEqual(thumbnail["headers"]["Content-Type"], "image/jpeg")
        self.assertEqual(thumbnail["raw_body"], b"event-thumbnail")
        self.assertTrue(preview["status"].startswith("200"))
        self.assertEqual(preview["headers"]["Content-Type"], "video/mp4")
        self.assertEqual(preview["raw_body"], b"video-event-preview")
        download = request(
            self.application,
            "/event-preview.mp4?"
            + urlencode({"event": event_id, "download": "1"}),
        )
        self.assertIn("attachment; filename=", download["headers"]["Content-Disposition"])
        self.assertIn(
            ("read_video_event_preview", {"event_id": event_id}),
            self.facade.calls,
        )
        self.assertIn(
            ("read_video_event_thumbnail", {"event_id": event_id}),
            self.facade.calls,
        )

        master = request(
            self.application,
            "/event-vod?" + urlencode(
                {"event": event_id, "resource": "master.m3u8"}
            ),
        )
        self.assertTrue(master["status"].startswith("200"))
        self.assertEqual(
            master["headers"]["Content-Type"], "application/vnd.apple.mpegurl"
        )
        self.assertIn(
            "/event-vod?event=frigate%3Areview-123&resource=index-v1.m3u8",
            master["body"],
        )

        media = request(
            self.application,
            "/event-vod?" + urlencode(
                {"event": event_id, "resource": "index-v1.m3u8"}
            ),
        )
        self.assertIn(
            'URI="/event-vod?event=frigate%3Areview-123&resource=init-v1.mp4"',
            media["body"],
        )
        self.assertIn(
            "/event-vod?event=frigate%3Areview-123&resource=seg-1-v1.m4s",
            media["body"],
        )
        segment = request(
            self.application,
            "/event-vod?" + urlencode(
                {"event": event_id, "resource": "seg-1-v1.m4s"}
            ),
        )
        self.assertEqual(segment["headers"]["Content-Type"], "video/mp4")
        self.assertEqual(segment["raw_body"], b"video-segment")

        unsafe = request(
            self.application,
            "/event-vod?" + urlencode(
                {"event": event_id, "resource": "../config.yml"}
            ),
        )
        self.assertTrue(unsafe["status"].startswith("400"))

        ranged = request(
            self.application,
            "/event-preview.mp4?" + urlencode({"event": event_id}),
            headers={"Range": "bytes=6-10"},
        )
        self.assertTrue(ranged["status"].startswith("206"))
        self.assertEqual(ranged["raw_body"], b"event")
        self.assertEqual(ranged["headers"]["Accept-Ranges"], "bytes")
        self.assertEqual(ranged["headers"]["Content-Range"], "bytes 6-10/19")

        unsatisfied = request(
            self.application,
            "/event-preview.mp4?" + urlencode({"event": event_id}),
            headers={"Range": "bytes=100-"},
        )
        self.assertTrue(unsatisfied["status"].startswith("416"))
        self.assertEqual(unsatisfied["headers"]["Content-Range"], "bytes */19")

        self.facade.owner_ready = True
        denied = request(
            self.application,
            "/event-preview.mp4?" + urlencode({"event": event_id}),
        )
        self.assertTrue(denied["status"].startswith("401"))
        denied_vod = request(
            self.application,
            "/event-vod?" + urlencode(
                {"event": event_id, "resource": "master.m3u8"}
            ),
        )
        self.assertTrue(denied_vod["status"].startswith("401"))

    def test_event_actions_and_saved_clip_media_are_owner_routes(self):
        event_id = "frigate:review-123"
        self.facade.video_events = [
            {
                "id": event_id,
                "camera_id": "CAM-0001",
                "started_at": "2026-09-22T12:00:00+00:00",
                "ended_at": "2026-09-22T12:00:10+00:00",
                "severity": "alert",
                "labels": ("person",),
            }
        ]
        self.facade.saved_video_clips = [
            {
                "id": "export-123",
                "camera_id": "CAM-0001",
                "created_at": "2026-09-22T12:01:00+00:00",
                "in_progress": False,
            }
        ]

        page = request(self.application, "/events")["body"]
        saved = request(
            self.application,
            "/saved-clip.mp4?" + urlencode({"clip": "export-123"}),
        )
        saved_download = request(
            self.application,
            "/saved-clip.mp4?"
            + urlencode({"clip": "export-123", "download": "1"}),
        )
        saving = self.post("/actions/event-save", event=event_id)
        event_deleted = self.post("/actions/event-delete", event=event_id)
        clip_deleted = self.post(
            "/actions/saved-clip-delete", clip="export-123"
        )

        self.assertIn("Saved Clips", page)
        self.assertIn("kept until you delete them", page)
        self.assertIn("/saved-clip.mp4?clip=export-123", page)
        self.assertIn("Play saved clip", page)
        self.assertNotIn('<video controls preload="metadata"', page)
        self.assertEqual(saved["raw_body"], b"saved-video-clip")
        self.assertIn(
            "attachment; filename=", saved_download["headers"]["Content-Disposition"]
        )
        self.assertIn("event-saving", saving["headers"]["Location"])
        self.assertIn("event-deleted", event_deleted["headers"]["Location"])
        self.assertIn("saved-clip-deleted", clip_deleted["headers"]["Location"])
        self.assertIn(
            ("save_video_event", {"event_id": event_id}), self.facade.calls
        )
        self.assertIn(
            ("delete_video_event", {"event_id": event_id}), self.facade.calls
        )
        self.assertIn(
            ("delete_saved_video_clip", {"clip_id": "export-123"}),
            self.facade.calls,
        )

        self.facade.owner_ready = True
        denied = request(
            self.application,
            "/saved-clip.mp4?" + urlencode({"clip": "export-123"}),
        )
        self.assertTrue(denied["status"].startswith("401"))

    def test_event_deep_link_selects_only_the_requested_engine_event(self):
        self.facade.video_events = [
            {
                "id": "frigate:selected",
                "camera_id": "CAM-0001",
                "started_at": "2026-09-22T12:00:00+00:00",
                "ended_at": "2026-09-22T12:00:10+00:00",
                "severity": "alert",
                "labels": ("person",),
            },
            {
                "id": "frigate:other",
                "camera_id": "CAM-0001",
                "started_at": "2026-09-22T12:01:00+00:00",
                "ended_at": "2026-09-22T12:01:10+00:00",
                "severity": "detection",
                "labels": ("cat",),
            },
        ]
        self.facade.recordings = [
            {
                "id": "legacy-segment",
                "camera_id": "CAM-0001",
                "started_at": "2026-09-22T11:59:00+00:00",
                "reference": "recordings/CAM-0001/legacy-segment",
            }
        ]

        page = request(
            self.application,
            "/events?" + urlencode({"event": "frigate:selected"}),
        )["body"]

        self.assertIn("Person alert", page)
        self.assertNotIn("Cat detection", page)
        self.assertNotIn("legacy-segment", page)

    def test_active_event_opens_live_camera_and_refreshes_until_clip_is_ready(self):
        self.facade.video_events = [
            {
                "id": "frigate:active",
                "camera_id": "CAM-0001",
                "started_at": "2026-09-22T12:00:00+00:00",
                "ended_at": None,
                "severity": "alert",
                "labels": ("person",),
            }
        ]

        page = request(
            self.application,
            "/events?" + urlencode({"event": "frigate:active"}),
        )["body"]

        self.assertIn("data-active-event", page)
        self.assertIn("Live · Recording now", page)
        self.assertIn('href="/#camera-1"', page)
        self.assertNotIn("/event-preview.mp4", page)
        script = request(self.application, "/events.js")["body"]
        self.assertIn("window.location.reload()", script)

    def test_event_engine_does_not_label_background_segments_as_events(self):
        self.facade.video_engine = {
            "state": "ready",
            "engine": "frigate",
            "version": "0.18.0",
        }
        self.facade.recordings = [
            {
                "id": "background-segment",
                "camera_id": "CAM-0001",
                "started_at": "2026-09-22T12:00:00+00:00",
                "reference": "recordings/CAM-0001/background-segment",
            }
        ]

        page = request(self.application, "/events")["body"]

        self.assertIn("No events yet.", page)
        self.assertNotIn("background-segment", page)
        self.assertNotIn(
            "list_recordings", [name for name, _ in self.facade.calls]
        )

    def test_event_history_failure_is_not_reported_as_an_empty_history(self):
        with patch.object(
            self.facade, "list_video_events",
            side_effect=ValueError("private-disk-path-and-secret"),
        ):
            for path in ("/events", "/events?event=frigate:selected"):
                with self.subTest(path=path):
                    page = request(self.application, path)["body"]
                    self.assertIn("data-event-history-unavailable", page)
                    self.assertIn('role="alert"', page)
                    self.assertNotIn("No events yet.", page)
                    self.assertNotIn("That event is no longer available.", page)
                    self.assertNotIn("private-disk-path-and-secret", page)

    def test_legacy_recording_failure_is_not_reported_as_empty_history(self):
        with patch.object(self.facade, "list_recordings", side_effect=OSError()):
            page = request(self.application, "/events")["body"]
        self.assertIn("data-event-history-unavailable", page)
        self.assertNotIn("No events yet.", page)

    def test_engine_status_failure_is_not_reported_as_empty_history(self):
        with patch.object(self.facade, "video_engine_status", side_effect=OSError()):
            page = request(self.application, "/events")["body"]
        self.assertIn("data-event-history-unavailable", page)
        self.assertNotIn("No events yet.", page)

    def test_saved_clip_failure_is_visible_without_leaking_engine_details(self):
        with patch.object(
            self.facade, "list_saved_video_clips",
            side_effect=ValueError("private-export-secret"),
        ):
            page = request(self.application, "/events")["body"]
        self.assertIn("data-saved-clips-unavailable", page)
        self.assertNotIn("private-export-secret", page)

    def test_camera_action_filters_events_without_exposing_identity(self):
        self.facade.recordings = [
            {
                "id": "segment-1",
                "camera_id": "CAM-0001",
                "started_at": "2026-08-28T12:00:00+00:00",
                "reference": "recordings/CAM-0001/segment-1",
            },
            {
                "id": "segment-other",
                "camera_id": "CAM-OTHER",
                "started_at": "2026-08-28T12:01:00+00:00",
                "reference": "recordings/CAM-OTHER/segment-other",
            },
        ]

        page = request(self.application, "/events?camera=1")["body"]

        self.assertIn("Driveway Events", page)
        self.assertIn("All camera events", page)
        self.assertIn("segment-1", page)
        self.assertNotIn("segment-other", page)

    def test_every_form_has_csrf_token_and_post_rejects_missing_or_wrong_token(self):
        factory_application = create_application(
            self.facade,
            csrf_token="test-token",
            factory_enabled=True,
            wifi_country_default="US",
        )
        for application, path in (
            (self.application, "/settings"),
            (factory_application, "/provisioning"),
        ):
            body = request(application, path)["body"]
            self.assertEqual(body.count('<form method="post"'), body.count('name="csrf_token"'))

        missing = request(
            self.application,
            "/actions/owner",
            method="POST",
            fields={"name": "Owner", "password": "example-owner-password"},
        )
        wrong = request(
            self.application,
            "/actions/owner",
            method="POST",
            fields={
                "csrf_token": "wrong",
                "name": "Owner",
                "password": "example-owner-password",
            },
        )
        self.assertTrue(missing["status"].startswith("403"))
        self.assertTrue(wrong["status"].startswith("403"))
        self.assertEqual(self.facade.calls, [])

    def test_retail_forms_call_facade_with_validated_values(self):
        owner = self.post(
            "/actions/owner",
            name="Owner",
            password="example-owner-password",
            password_confirm="example-owner-password",
        )
        cookie = session_cookie(owner)
        responses = [
            owner,
            self.post(
                "/actions/storage",
                cookie=cookie,
                storage_id=self.facade.storage_target.stable_id,
            ),
            self.post(
                "/actions/camera",
                cookie=cookie,
                camera_id="CAM-0001",
                claim_code="claim-private",
                name="Driveway",
            ),
            self.post(
                "/actions/recording",
                cookie=cookie,
                mode="continuous",
                days="7",
            ),
        ]
        self.assertTrue(all(item["status"].startswith("303") for item in responses))
        self.assertEqual(
            self.facade.calls,
            [
                (
                    "create_owner",
                    {"name": "Owner", "password": "example-owner-password"},
                ),
                (
                    "initialize_selected_recording_storage",
                    {
                        "stable_id": self.facade.storage_target.stable_id,
                        "confirmation": self.facade.storage_target.confirmation,
                    },
                ),
                (
                    "claim_camera",
                    {
                        "camera_id": "CAM-0001",
                        "claim_code": "claim-private",
                        "name": "Driveway",
                    },
                ),
                ("set_recording", {"mode": "continuous", "days": 7}),
            ],
        )
        rendered = "".join(item["body"] for item in responses)
        self.assertNotIn("owner-private", rendered)
        self.assertNotIn("claim-private", rendered)

    def test_scanned_camera_label_survives_login_and_uses_one_add_form(self):
        self.facade.owner_ready = True
        login = request(self.application, "/login")
        self.assertIn('src="/camera-setup.js"', login["body"])
        script = request(self.application, "/camera-setup.js")
        self.assertTrue(script["status"].startswith("200"))
        self.assertIn("history.replaceState", script["body"])
        self.assertNotIn("fetch(", script["body"])

        signed_in = self.post(
            "/actions/login", password=self.facade.owner_password
        )
        cookie = session_cookie(signed_in)
        settings = request(
            self.application,
            "/settings",
            cookie=cookie,
        )
        self.assertIn('name="claim_payload"', settings["body"])
        self.assertIn('name="camera_code"', settings["body"])
        self.assertIn("<h3>Add camera</h3>", settings["body"])
        self.assertNotIn("<summary>Add or replace a camera</summary>", settings["body"])
        payload = (
            "vms://claim?kit=KIT-0123456789AB&device=CAM-0123456789ABCDEF"
            "&number=4&code=abcdefghijklmnopqrstuvwxyz012345"
        )
        response = self.post(
            "/actions/camera",
            cookie=cookie,
            claim_payload=payload,
            name="Driveway",
        )
        self.assertTrue(response["status"].startswith("303"))
        self.assertIn(
            (
                "claim_camera_payload",
                {"claim_payload": payload, "name": "Driveway"},
            ),
            self.facade.calls,
        )

        manual = self.post(
            "/actions/camera",
            cookie=cookie,
            claim_payload="",
            camera_code="ABCD EFGH IJKL MNOP QRST UVWX YZ23 4567",
            name="Back Porch",
        )
        self.assertTrue(manual["status"].startswith("303"))
        self.assertIn(
            (
                "claim_camera_code",
                {
                    "camera_code": "ABCD EFGH IJKL MNOP QRST UVWX YZ23 4567",
                    "name": "Back Porch",
                },
            ),
            self.facade.calls,
        )

    def test_factory_forms_call_facade_without_raw_device_details(self):
        factory_application = create_application(
            self.facade,
            csrf_token="test-token",
            factory_enabled=True,
            wifi_country_default="US",
        )
        page = request(factory_application, "/provisioning")["body"]
        self.assertIn("Create Kit", page)
        self.assertIn("Raspberry Pi Zero W", page)
        self.assertIn("Raspberry Pi Zero 2 W", page)
        self.assertIn('name="wifi_country"', page)
        self.assertIn('value="US"', page)
        for forbidden in ("/dev/", "disk", "partition"):
            self.assertNotIn(forbidden, page.lower())

        def factory_post(path, **fields):
            return request(
                factory_application,
                path,
                method="POST",
                fields={"csrf_token": "test-token", **fields},
            )

        factory_post(
            "/actions/kit",
            camera_count="4",
            camera_model="zero-2-w",
            wifi_country="US",
        )
        factory_post(
            "/actions/flash",
            stable_id=self.facade.target.stable_id,
            confirmation=self.facade.target.confirmation,
        )
        factory_post("/actions/verify")
        self.assertEqual(
            self.facade.calls,
            [
                (
                    "create_kit",
                    {
                        "camera_count": 4,
                        "camera_model": "zero-2-w",
                        "wifi_country": "US",
                    },
                ),
                (
                    "flash_selected",
                    {
                        "stable_id": self.facade.target.stable_id,
                        "confirmation": self.facade.target.confirmation,
                    },
                ),
                ("verify_kit", {}),
            ],
        )
        self.assertEqual(len(self.facade.provisioning_requests), 1)
        self.assertEqual(self.facade.provisioning_requests[0].mode, "kit")

    def test_factory_can_provision_one_camera_from_one_form(self):
        factory_application = create_application(
            self.facade,
            csrf_token="test-token",
            factory_enabled=True,
            wifi_country_default="US",
        )
        page = request(factory_application, "/provisioning")["body"]
        self.assertIn("Provision Camera", page)

        passphrase = "private camera passphrase"
        response = request(
            factory_application,
            "/actions/camera-provision",
            method="POST",
            fields={
                "csrf_token": "test-token",
                "single_camera_number": "2",
                "single_camera_model": "zero-2-w",
                "single_wifi_ssid": "Camera Network",
                "single_wifi_passphrase": passphrase,
                "single_wifi_passphrase_confirm": passphrase,
                "single_wifi_country": "US",
                "single_stable_id": self.facade.target.stable_id,
                "single_confirmation": self.facade.target.confirmation,
            },
        )

        self.assertTrue(response["status"].startswith("303"))
        self.assertNotIn(passphrase, response["body"])
        self.assertEqual(len(self.facade.provisioning_requests), 1)
        provisioning = self.facade.provisioning_requests[0]
        self.assertEqual(provisioning.mode, "camera")
        self.assertEqual(provisioning.camera_number, 2)
        self.assertEqual(provisioning.network.ssid, "Camera Network")
        operation, values = self.facade.calls[0]
        self.assertEqual(operation, "provision_selected_camera")
        self.assertIs(values["request"], provisioning)
        self.assertEqual(values["wifi_passphrase"], passphrase)
        self.assertEqual(values["stable_id"], self.facade.target.stable_id)
        self.assertEqual(values["confirmation"], self.facade.target.confirmation)

    def test_factory_page_shows_pilot_endpoints_but_never_secrets(self):
        self.facade.diagnostics = [
            {
                "camera_number": 3,
                "username": "vms-recovery",
                "private_network_ssh": "camera-003.local",
                "usb_serial": "USB data port; serial console",
                "gpio_uart": "GPIO pin 8 TX and pin 6 GND; 115200 8-N-1",
                "ssh_key_file": "/private/CAM-3.key",
                "credentials_file": "/private/camera-003.json",
            }
        ]
        application = create_application(
            self.facade,
            csrf_token="test-token",
            factory_enabled=True,
            wifi_country_default="US",
        )

        page = request(application, "/provisioning")["body"]

        self.assertIn("Pilot Diagnostics", page)
        self.assertIn("vms-recovery@camera-003.local", page)
        self.assertIn("/private/CAM-3.key", page)
        self.assertNotIn("private-password", page)

    def test_settings_does_not_duplicate_the_feed_rename_control(self):
        self.facade.camera_state = "ready"
        page = request(self.application, "/settings")["body"]
        self.assertIn("Driveway", page)
        self.assertNotIn('action="/actions/camera-name"', page)

        response = self.post(
            "/actions/camera-name-slot",
            slot="1",
            name="Driveway",
        )

        self.assertTrue(response["status"].startswith("303"))
        self.assertEqual(
            self.facade.calls,
            [("name_camera", {"camera_id": "CAM-0001", "name": "Driveway"})],
        )

    def test_factory_rejects_an_invalid_wifi_country_before_creating_kit(self):
        factory_application = create_application(
            self.facade,
            csrf_token="test-token",
            factory_enabled=True,
        )

        response = request(
            factory_application,
            "/actions/kit",
            method="POST",
            fields={
                "csrf_token": "test-token",
                "camera_count": "1",
                "camera_model": "zero-2-w",
                "wifi_country": "us",
            },
        )

        self.assertTrue(response["status"].startswith("400"))
        self.assertEqual(self.facade.calls, [])

    def test_facade_exception_is_not_disclosed(self):
        def fail(**values):
            raise RuntimeError("owner-private")

        self.facade.create_owner = fail
        response = self.post(
            "/actions/owner",
            name="Owner",
            password="example-owner-password",
            password_confirm="example-owner-password",
        )
        self.assertTrue(response["status"].startswith("500"))
        self.assertNotIn("owner-private", response["body"])

    def test_server_binds_loopback_by_default(self):
        server = create_server(self.facade, port=0, csrf_token="test-token")
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")

    def test_retail_interface_does_not_expose_factory_provisioning(self):
        settings = request(self.application, "/settings")
        provisioning = request(self.application, "/provisioning")
        factory_action = self.post(
            "/actions/kit",
            camera_count="1",
            camera_model="zero-2-w",
            wifi_country="US",
        )
        self.assertNotIn("Factory Provisioning", settings["body"])
        self.assertTrue(provisioning["status"].startswith("404"))
        self.assertTrue(factory_action["status"].startswith("404"))


if __name__ == "__main__":
    unittest.main()
