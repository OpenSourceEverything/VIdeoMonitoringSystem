import json
import http.client
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from video_monitoring_system.camera_endpoints import (
    AuthenticatedCameraObservation,
    CameraEndpointRegistry,
    ObservedCameraEndpoint,
)
from video_monitoring_system.camera_route_relay import (
    CameraRouteRelayError,
    CameraRouteRelayServer,
    CameraRouteRelayState,
)


CAMERA_ID = "CAM-0123456789ABCDEF"


def health(_endpoint, camera_id, _secret):
    return {
        "schema_version": 4,
        "device_id": camera_id,
        "media": {
            "live": {
                "codec": "h264",
                "path": "/stream.h264",
                "width": 1920,
                "height": 1080,
                "fps": 15,
                "bitrate": 3_000_000,
            },
            "detection": {
                "codec": "h264",
                "path": "/detect.h264",
                "width": 640,
                "height": 360,
                "fps": 5,
                "bitrate": 500_000,
            },
        },
    }


class CameraRouteRelayStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.registry = CameraEndpointRegistry(self.data / "camera-endpoints.json")
        self.committed = ObservedCameraEndpoint("10.42.0.82", 9080)
        self.candidate = ObservedCameraEndpoint("192.168.1.82", 9080)
        self.registry.reconcile(
            authorized_camera_ids={CAMERA_ID},
            observations=[
                AuthenticatedCameraObservation(CAMERA_ID, self.committed)
            ],
        )
        self.registry.observe(
            authorized_camera_ids={CAMERA_ID},
            observation=AuthenticatedCameraObservation(CAMERA_ID, self.candidate),
        )
        self.relay = CameraRouteRelayState(
            self.data,
            secret_provider=lambda camera_id: (
                "private-stream-secret" if camera_id == CAMERA_ID else None
            ),
            health_provider=health,
        )

    def test_active_route_stays_committed_until_exact_candidate_is_activated(self):
        before = self.relay.resolve(camera_id=CAMERA_ID, role="main")

        self.relay.activate_candidate(camera_id=CAMERA_ID, endpoint=self.candidate)
        after = self.relay.resolve(camera_id=CAMERA_ID, role="main")

        self.assertEqual(before.endpoint, self.committed)
        self.assertEqual(after.endpoint, self.candidate)
        self.assertEqual(after.path, "/stream.h264")
        self.assertEqual(after.codec, "h264")
        self.assertNotIn("private-stream-secret", after.url)
        self.assertNotIn("private-stream-secret", repr(after))
        self.assertTrue(after.authorization.startswith("Basic "))

    def test_candidate_can_be_probed_without_switching_the_active_route(self):
        staged = self.relay.resolve(
            camera_id=CAMERA_ID, role="detection", selection="candidate"
        )
        active = self.relay.resolve(camera_id=CAMERA_ID, role="detection")

        self.assertEqual(staged.endpoint, self.candidate)
        self.assertEqual(active.endpoint, self.committed)
        self.assertEqual(staged.path, "/detect.h264")

    def test_restore_removes_only_the_selected_camera_override(self):
        self.relay.activate_candidate(camera_id=CAMERA_ID, endpoint=self.candidate)

        self.relay.restore_committed(camera_id=CAMERA_ID)

        self.assertIsNone(self.relay.active_candidate_for(CAMERA_ID))
        self.assertEqual(
            self.relay.resolve(camera_id=CAMERA_ID, role="main").endpoint,
            self.committed,
        )

    def test_activation_rejects_a_route_that_is_not_the_saved_candidate(self):
        with self.assertRaisesRegex(CameraRouteRelayError, "candidate changed"):
            self.relay.activate_candidate(
                camera_id=CAMERA_ID,
                endpoint=ObservedCameraEndpoint("192.168.1.99", 9080),
            )

    def test_stale_active_selection_fails_closed_after_candidate_changes(self):
        self.relay.activate_candidate(camera_id=CAMERA_ID, endpoint=self.candidate)
        self.registry.reject(camera_id=CAMERA_ID, endpoint=self.candidate)

        with self.assertRaisesRegex(CameraRouteRelayError, "candidate changed"):
            self.relay.resolve(camera_id=CAMERA_ID, role="main")

    def test_state_is_private_and_contains_no_camera_secret(self):
        self.relay.activate_candidate(camera_id=CAMERA_ID, endpoint=self.candidate)

        content = (self.data / "camera-route-relay.json").read_text(encoding="utf-8")

        self.assertNotIn("private-stream-secret", content)
        self.assertEqual(
            os.stat(self.data / "camera-route-relay.json").st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            json.loads(content)["active_candidates"][CAMERA_ID]["ip_address"],
            "192.168.1.82",
        )

    def test_invalid_or_group_readable_state_is_rejected(self):
        path = self.data / "camera-route-relay.json"
        path.write_text('{"schema_version":1,"active_candidates":{}}\n')
        path.chmod(0o640)

        with self.assertRaisesRegex(CameraRouteRelayError, "could not be read"):
            self.relay.active_candidate_for(CAMERA_ID)


class FakeUpstreamResponse:
    def __init__(self, *, status=200, content_type="video/h264", body=b"frames"):
        self.status = status
        self.content_type = content_type
        self.body = body
        self.offset = 0
        self.closed = False

    def getheader(self, name, default=None):
        return self.content_type if name.lower() == "content-type" else default

    def read(self, size=-1):
        if self.offset >= len(self.body):
            return b""
        if size < 0:
            size = len(self.body) - self.offset
        result = self.body[self.offset : self.offset + size]
        self.offset += len(result)
        return result

    def close(self):
        self.closed = True

    def read1(self, size=-1):
        return self.read(size)


class FakeUpstreamConnection:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def request(self, method, path, *, headers):
        self.calls.append((method, path, dict(headers)))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class CameraRouteRelayServerTests(CameraRouteRelayStateTests):
    def setUp(self):
        super().setUp()
        self.upstream_response = FakeUpstreamResponse()
        self.upstream_connections = []

        def connect(_upstream, _timeout):
            connection = FakeUpstreamConnection(self.upstream_response)
            self.upstream_connections.append(connection)
            return connection

        self.server = CameraRouteRelayServer(
            ("127.0.0.1", 0),
            self.relay,
            allowed_client_networks=("127.0.0.0/8",),
            upstream_connector=connect,
        )
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01),
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=2
        )
        connection.request("GET", path)
        response = connection.getresponse()
        result = (response.status, response.getheader("Content-Type"), response.read())
        connection.close()
        return result

    def test_relays_stream_and_injects_authorization_only_as_a_header(self):
        status, content_type, body = self.request(
            f"/v1/cameras/{CAMERA_ID}/committed/main"
        )

        self.assertEqual((status, content_type, body), (200, "video/h264", b"frames"))
        method, path, headers = self.upstream_connections[0].calls[0]
        self.assertEqual((method, path), ("GET", "/stream.h264"))
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        self.assertNotIn("private-stream-secret", path)
        self.assertTrue(self.upstream_connections[0].closed)

    def test_small_live_chunk_arrives_before_upstream_fills_or_closes(self):
        release = threading.Event()
        finished = threading.Event()
        class SlowCamera(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/h264")
                self.end_headers()
                self.wfile.write(b"small")
                self.wfile.flush()
                release.wait(4)
                finished.set()
            def log_message(self, *args): pass
        upstream = ThreadingHTTPServer(("127.0.0.1",0),SlowCamera)
        thread = threading.Thread(target=upstream.serve_forever,daemon=True)
        thread.start()
        self.server.upstream_connector = lambda *_: http.client.HTTPConnection("127.0.0.1",upstream.server_address[1],timeout=5)
        client = http.client.HTTPConnection("127.0.0.1",self.server.server_address[1],timeout=2)
        try:
            client.request("GET",f"/v1/cameras/{CAMERA_ID}/active/main")
            response=client.getresponse()
            self.assertEqual(response.read(5),b"small")
            self.assertFalse(finished.is_set(), "relay waited for the upstream to finish")
        finally:
            release.set()
            client.close()
            upstream.shutdown()
            upstream.server_close()
            thread.join(2)

    def test_rejects_query_strings_and_unrecognized_paths_before_upstream(self):
        status, _, _ = self.request(
            f"/v1/cameras/{CAMERA_ID}/active/main?secret=bad"
        )

        self.assertEqual(status, 404)
        self.assertEqual(self.upstream_connections, [])

    def test_rejects_an_unexpected_upstream_media_type(self):
        self.upstream_response = FakeUpstreamResponse(content_type="text/plain")

        status, _, body = self.request(
            f"/v1/cameras/{CAMERA_ID}/active/main"
        )

        self.assertEqual(status, 502)
        self.assertEqual(body, b"Camera route unavailable.\n")

    def test_server_rejects_clients_outside_the_private_network(self):
        self.assertTrue(self.server.client_allowed(("127.0.0.1", 1234)))
        self.assertFalse(self.server.client_allowed(("192.0.2.20", 1234)))

    def test_server_refuses_a_bind_outside_its_private_client_network(self):
        with self.assertRaisesRegex(ValueError, "bind address"):
            CameraRouteRelayServer(
                ("127.0.0.1", 0),
                self.relay,
                allowed_client_networks=("10.42.0.0/24",),
            )

    def test_server_refuses_a_public_client_network(self):
        with self.assertRaisesRegex(ValueError, "client network"):
            CameraRouteRelayServer(
                ("8.8.8.8", 0),
                self.relay,
                allowed_client_networks=("8.8.8.0/24",),
            )


if __name__ == "__main__":
    unittest.main()
