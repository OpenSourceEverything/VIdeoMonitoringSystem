import json
import os
import tempfile
import unittest
from pathlib import Path

from video_monitoring_system.video_engine import (
    FrigateEngine,
    VideoEngineError,
    load_configured_video_engine,
    save_frigate_engine_configuration,
)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def __call__(self, path, accept, maximum_bytes):
        self.calls.append((path, accept, maximum_bytes))
        response = self.responses.get(path)
        if response is None:
            raise AssertionError(f"unexpected Frigate request: {path}")
        return response


class FakeSignalTransport(FakeTransport):
    def __call__(self, path, body, maximum_bytes):
        self.calls.append((path, body, maximum_bytes))
        response = self.responses.get(path)
        if response is None:
            raise AssertionError(f"unexpected Frigate signaling request: {path}")
        return response


class FakeMutationTransport(FakeSignalTransport):
    pass


class FakeRouteTransport:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def __call__(self, method, path, maximum_bytes):
        self.calls.append((method, path, maximum_bytes))
        response = self.responses.get((method, path))
        if response is None:
            raise AssertionError(f"unexpected private route request: {method} {path}")
        return response


class FrigateEngineTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.signal_transport = FakeSignalTransport()
        self.mutation_transport = FakeMutationTransport()
        self.route_transport = FakeRouteTransport()
        self.engine = FrigateEngine(
            {"CAM-FRONT": "front_door", "CAM-BACK": "back_porch"},
            port=15000,
            transport=self.transport,
            go2rtc_transport=self.transport,
            signal_transport=self.signal_transport,
            mutation_transport=self.mutation_transport,
            route_transport=self.route_transport,
        )

    def test_status_normalizes_version_without_exposing_frigate_ui(self):
        self.transport.responses["/api/version"] = (b'"0.18.0"', "application/json")

        self.assertEqual(
            self.engine.status(),
            {"state": "ready", "engine": "frigate", "version": "0.18.0"},
        )
        self.assertEqual(self.transport.calls[0][0], "/api/version")

        self.transport.responses["/api/version"] = (b"0.18.0", "text/plain")
        self.assertEqual(self.engine.status()["version"], "0.18.0")

    def test_live_metrics_identify_detect_input_not_recording_rate(self):
        self.transport.responses["/api/stats"] = (
            json.dumps(
                {
                    "cameras": {
                        "front_door": {
                            "camera_fps": 5.16,
                            "skipped_fps": 0.08,
                            "connection_quality": "fair",
                            "stalls_last_hour": 12,
                            "reconnects_last_hour": 2,
                            "expected_fps": 5,
                            "ffmpeg_cpu": "22.7",
                        }
                    },
                    "cpu_usages": {
                        "frigate.full_system": {"cpu": "78.9", "mem": "51.2"}
                    },
                }
            ).encode(),
            "application/json",
        )

        self.assertEqual(
            self.engine.live_metrics(camera_id="CAM-FRONT"),
            {
                "detect_input_fps": 5.2,
                "skipped_fps": 0.1,
                "connection_quality": "fair",
                "source_stalls_last_hour": 12,
                "source_reconnects_last_hour": 2,
                "expected_fps": 5.0,
                "camera_ffmpeg_cpu_percent": 22.7,
                "recorder_cpu_percent": 78.9,
            },
        )
        self.assertEqual(self.transport.calls[-1], ("/api/stats", "application/json", 2_000_000))

    def test_recording_coverage_merges_overlaps_and_reports_real_gaps(self):
        path = "/api/front_door/recordings?after=100&before=160"
        self.transport.responses[path] = (
            json.dumps(
                [
                    {"start_time": 100, "end_time": 120, "segment_size": 1000},
                    {"start_time": 115, "end_time": 130, "segment_size": 1000},
                    {"start_time": 140, "end_time": 155, "segment_size": 1000},
                ]
            ).encode(),
            "application/json",
        )

        self.assertEqual(
            self.engine.recording_coverage(camera_id="CAM-FRONT", after=100, before=160),
            {
                "state": "indexed",
                "window_seconds": 60,
                "indexed_seconds": 45.0,
                "gap_seconds": 15.0,
                "largest_gap_seconds": 10.0,
                "segment_count": 3,
                "latest_segment_end": 155.0,
                "basis": "retained main-stream index; file playability unverified",
            },
        )
        self.assertEqual(self.transport.calls[-1], (path, "application/json", 1_000_000))

    def test_recording_coverage_reports_no_retained_segments_without_claiming_failure(self):
        self.transport.responses["/api/front_door/recordings?after=100&before=160"] = (
            b"[]", "application/json"
        )
        result = self.engine.recording_coverage(
            camera_id="CAM-FRONT", after=100, before=160
        )
        self.assertEqual(result["state"], "none")
        self.assertEqual(result["gap_seconds"], 60.0)
        self.assertEqual(result["segment_count"], 0)

    def test_recording_coverage_rejects_invalid_rows_and_unbounded_windows(self):
        path = "/api/front_door/recordings?after=100&before=160"
        self.transport.responses[path] = (
            b'[{"start_time":100,"end_time":120,"segment_size":0}]',
            "application/json",
        )
        with self.assertRaises(VideoEngineError):
            self.engine.recording_coverage(camera_id="CAM-FRONT", after=100, before=160)
        with self.assertRaises(ValueError):
            self.engine.recording_coverage(camera_id="CAM-FRONT", after=100, before=3701)

    def test_reviews_map_engine_camera_and_event_ids_to_ose_objects(self):
        path = "/api/review?cameras=front_door%2Cback_porch&limit=2&after=1"
        self.transport.responses[path] = (
            json.dumps(
                [
                    {
                        "id": "1720000000.25-abcd12",
                        "camera": "front_door",
                        "start_time": 1720000000.25,
                        "end_time": 1720000010.5,
                        "severity": "alert",
                        "data": {"objects": ["person"], "zones": ["porch"]},
                    },
                    {
                        "id": "1720000020.0-other",
                        "camera": "not_owned_by_ose",
                        "start_time": 1720000020.0,
                        "end_time": None,
                        "severity": "detection",
                        "data": {},
                    },
                ]
            ).encode(),
            "application/json; charset=utf-8",
        )

        events = self.engine.list_events(limit=2)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, "frigate:1720000000.25-abcd12")
        self.assertEqual(events[0].camera_id, "CAM-FRONT")
        self.assertEqual(events[0].severity, "alert")
        self.assertEqual(events[0].labels, ("person",))
        self.assertEqual(events[0].zones, ("porch",))
        self.assertEqual(events[0].started_at.isoformat(), "2024-07-03T09:46:40.250000+00:00")

    def test_reviews_accept_documented_iso_timestamps(self):
        path = "/api/review?cameras=front_door&limit=1&after=1"
        self.transport.responses[path] = (
            json.dumps(
                [
                    {
                        "id": "event-iso",
                        "camera": "front_door",
                        "start_time": "2026-09-22T03:00:00Z",
                        "end_time": "2026-09-22T03:00:12+00:00",
                        "severity": "alert",
                        "data": {"objects": ["person"], "zones": []},
                    }
                ]
            ).encode(),
            "application/json",
        )

        event = self.engine.list_events(camera_ids=("CAM-FRONT",), limit=1)[0]

        self.assertEqual(event.started_at.isoformat(), "2026-09-22T03:00:00+00:00")
        self.assertEqual(event.ended_at.isoformat(), "2026-09-22T03:00:12+00:00")

    def test_snapshot_and_preview_are_fetched_through_bounded_private_paths(self):
        self.transport.responses["/api/front_door/latest.jpg"] = (
            b"jpeg",
            "image/jpeg",
        )
        preview_path = "/api/front_door/start/1719999995.25/end/1720000015.5/clip.mp4"
        self.transport.responses[preview_path] = (b"mp4", "video/mp4")
        review_path = "/api/review/1720000000.25-abcd12"
        self.transport.responses[review_path] = (
            json.dumps({
                "id": "1720000000.25-abcd12",
                "camera": "front_door",
                "start_time": 1720000000.25,
                "end_time": 1720000010.5,
                "severity": "alert",
                "data": {"detections": ["1720000000.0-detect"]},
            }).encode(),
            "application/json",
        )
        thumbnail_path = "/api/events/1720000000.0-detect/thumbnail.jpg"
        self.transport.responses[thumbnail_path] = (b"thumbnail", "image/jpeg")

        self.assertEqual(self.engine.read_snapshot(camera_id="CAM-FRONT"), b"jpeg")
        self.assertEqual(
            self.engine.read_event_preview(
                event_id="frigate:1720000000.25-abcd12"
            ),
            b"mp4",
        )
        self.assertEqual(
            self.engine.read_event_thumbnail(
                event_id="frigate:1720000000.25-abcd12"
            ),
            b"thumbnail",
        )
        self.assertEqual(self.transport.calls[0][2], 8_000_000)
        self.assertEqual(self.transport.calls[1][2], 2_000_000)
        self.assertEqual(self.transport.calls[2][2], 64_000_000)
        self.assertEqual(self.transport.calls[3][2], 2_000_000)
        self.assertEqual(self.transport.calls[4][2], 8_000_000)

    def test_completed_review_can_be_saved_outside_rolling_retention(self):
        review_path = "/api/review/1720000000.25-abcd12"
        self.transport.responses[review_path] = (
            json.dumps(
                {
                    "id": "1720000000.25-abcd12",
                    "camera": "front_door",
                    "start_time": 1720000000.25,
                    "end_time": 1720000010.5,
                    "severity": "alert",
                    "data": {"objects": ["person"], "zones": []},
                }
            ).encode(),
            "application/json",
        )
        self.transport.responses["/api/exports"] = (b"[]", "application/json")
        export_path = "/api/export/front_door/start/1719999995.25/end/1720000015.5"
        self.mutation_transport.responses[export_path] = (
            b'{"success":true,"export_id":"export-123","status":"in_progress"}',
            "application/json",
        )

        result = self.engine.save_event(event_id="frigate:1720000000.25-abcd12")

        self.assertEqual(result, {"state": "saving", "clip_id": "export-123"})
        path, body, maximum = self.mutation_transport.calls[-1]
        self.assertEqual(path, export_path)
        self.assertEqual(maximum, 64_000)
        payload = json.loads(body)
        self.assertEqual(payload["source"], "recordings")
        self.assertRegex(payload["name"], r"^ose-event-[0-9a-f]{24}$")

    def test_saved_clips_are_authorized_listed_downloaded_and_deleted(self):
        exported = {
            "id": "export-123",
            "camera": "back_porch",
            "name": "ose-event-1234",
            "date": 1720000030.0,
            "video_path": "/media/frigate/exports/back_porch-clip.mp4",
            "in_progress": False,
        }
        self.transport.responses["/api/exports"] = (
            json.dumps([exported]).encode(),
            "application/json",
        )
        self.transport.responses["/api/exports/export-123"] = (
            json.dumps(exported).encode(),
            "application/json",
        )
        self.transport.responses["/exports/back_porch-clip.mp4"] = (
            b"saved-mp4",
            "video/mp4",
        )
        self.mutation_transport.responses["/api/exports/delete"] = (
            b'{"success":true}',
            "application/json",
        )

        clips = self.engine.list_saved_clips(camera_ids=("CAM-BACK",))
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].camera_id, "CAM-BACK")
        self.assertEqual(
            self.engine.read_saved_clip(clip_id="export-123"), b"saved-mp4"
        )
        self.engine.delete_saved_clip(clip_id="export-123")

        path, body, maximum = self.mutation_transport.calls[-1]
        self.assertEqual((path, maximum), ("/api/exports/delete", 64_000))
        self.assertEqual(json.loads(body), {"ids": ["export-123"]})

    def test_deleting_review_authorizes_it_before_mutation(self):
        review_path = "/api/review/1720000000.25-abcd12"
        self.transport.responses[review_path] = (
            json.dumps(
                {
                    "id": "1720000000.25-abcd12",
                    "camera": "front_door",
                    "start_time": 1720000000.25,
                    "end_time": 1720000010.5,
                    "severity": "alert",
                    "data": {},
                }
            ).encode(),
            "application/json",
        )
        self.mutation_transport.responses["/api/reviews/delete"] = (
            b'{"success":true}',
            "application/json",
        )

        self.engine.delete_event(event_id="frigate:1720000000.25-abcd12")

        self.assertEqual(
            json.loads(self.mutation_transport.calls[-1][1]),
            {"ids": ["1720000000.25-abcd12"]},
        )

    def test_event_vod_uses_authorized_review_camera_and_time_range(self):
        review_path = "/api/review/1720000000.25-abcd12"
        review = {
            "id": "1720000000.25-abcd12",
            "camera": "front_door",
            "start_time": 1720000000.25,
            "end_time": 1720000010.5,
            "severity": "alert",
            "data": {"objects": ["person"], "zones": []},
        }
        self.transport.responses[review_path] = (
            json.dumps(review).encode(),
            "application/json",
        )
        master_path = (
            "/vod/front_door/start/1719999995.25/end/1720000015.5/master.m3u8"
        )
        self.transport.responses[master_path] = (
            b"#EXTM3U\nindex-v1.m3u8\n",
            "application/vnd.apple.mpegurl",
        )

        body, content_type = self.engine.read_event_vod(
            event_id="frigate:1720000000.25-abcd12",
            resource="master.m3u8",
        )

        self.assertEqual(body, b"#EXTM3U\nindex-v1.m3u8\n")
        self.assertEqual(content_type, "application/vnd.apple.mpegurl")
        self.assertEqual(
            self.transport.calls[-1], (master_path, content_type, 256_000)
        )

        self.transport.responses[review_path] = (
            json.dumps(review).encode(),
            "application/json",
        )
        segment_path = (
            "/vod/front_door/start/1719999995.25/end/1720000015.5/seg-1-v1.m4s"
        )
        self.transport.responses[segment_path] = (b"segment", "video/mp4")

        segment, segment_type = self.engine.read_event_vod(
            event_id="frigate:1720000000.25-abcd12",
            resource="seg-1-v1.m4s",
        )

        self.assertEqual(segment, b"segment")
        self.assertEqual(segment_type, "video/mp4")
        self.assertEqual(
            self.transport.calls[-1], (segment_path, "video/mp4", 16_000_000)
        )

    def test_event_vod_rejects_arbitrary_paths_and_active_reviews(self):
        for resource in (
            "../config.yml",
            "https://external.example/segment.ts",
            "segment.exe",
        ):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                self.engine.read_event_vod(
                    event_id="frigate:1720000000.25-abcd12",
                    resource=resource,
                )

        review_path = "/api/review/1720000000.25-abcd12"
        self.transport.responses[review_path] = (
            json.dumps(
                {
                    "id": "1720000000.25-abcd12",
                    "camera": "front_door",
                    "start_time": 1720000000.25,
                    "end_time": None,
                    "severity": "alert",
                    "data": {},
                }
            ).encode(),
            "application/json",
        )
        with self.assertRaises(VideoEngineError):
            self.engine.read_event_vod(
                event_id="frigate:1720000000.25-abcd12",
                resource="master.m3u8",
            )

    def test_review_media_rejects_mismatched_review_identity(self):
        self.transport.responses["/api/review/1720000000.25-abcd12"] = (
            json.dumps({
                "id": "another-review",
                "camera": "front_door",
                "start_time": 1720000000.25,
                "end_time": 1720000010.5,
                "severity": "alert",
                "data": {},
            }).encode(),
            "application/json",
        )
        with self.assertRaises(VideoEngineError):
            self.engine.read_event_preview(event_id="frigate:1720000000.25-abcd12")
        self.assertEqual(len(self.transport.calls), 1)

    def test_review_thumbnail_rejects_unbound_camera(self):
        self.transport.responses["/api/review/1720000000.25-abcd12"] = (
            json.dumps({
                "id": "1720000000.25-abcd12",
                "camera": "not_owned_by_ose",
                "start_time": 1720000000.25,
                "end_time": 1720000010.5,
                "severity": "alert",
                "data": {"detections": ["1720000000.0-detect"]},
            }).encode(),
            "application/json",
        )
        with self.assertRaises(VideoEngineError):
            self.engine.read_event_thumbnail(event_id="frigate:1720000000.25-abcd12")
        self.assertEqual(len(self.transport.calls), 1)

    def test_rejects_ambiguous_bindings_and_cross_engine_identifiers(self):
        with self.assertRaises(ValueError):
            FrigateEngine({"CAM-1": "same", "CAM-2": "same"})
        with self.assertRaises(ValueError):
            FrigateEngine({"CAM-1": "http://external.example/camera"})
        with self.assertRaises(ValueError):
            self.engine.read_snapshot(camera_id="CAM-UNKNOWN")
        with self.assertRaises(ValueError):
            self.engine.read_event_preview(event_id="legacy:event")
        with self.assertRaises(ValueError):
            self.engine.read_event_vod(
                event_id="legacy:event", resource="master.m3u8"
            )

    def test_webrtc_offer_is_bounded_and_camera_mapped_before_signaling(self):
        path = "/api/go2rtc/webrtc?src=back_porch"
        self.signal_transport.responses[path] = (
            b"v=0\r\no=engine-answer\r\n",
            "application/sdp",
        )

        answer = self.engine.negotiate_live_webrtc(
            camera_id="CAM-BACK",
            offer="v=0\r\no=browser-offer\r\n",
        )

        self.assertTrue(answer.startswith("v=0"))
        self.assertEqual(self.signal_transport.calls[0][0], path)
        self.assertEqual(
            self.signal_transport.calls[0][1], b"v=0\r\no=browser-offer\r\n"
        )
        self.assertEqual(self.signal_transport.calls[0][2], 64 * 1024)
        with self.assertRaises(ValueError):
            self.engine.negotiate_live_webrtc(
                camera_id="CAM-UNKNOWN",
                offer="v=0\r\n",
            )
        with self.assertRaises(ValueError):
            self.engine.negotiate_live_webrtc(
                camera_id="CAM-BACK",
                offer="not-sdp",
            )

    def test_private_route_update_accepts_only_credential_free_relay_sources(self):
        source = FrigateEngine.private_route_source(
            camera_id="CAM-0123456789ABCDEF",
            selection="candidate",
        )
        path = (
            "/api/go2rtc/streams/ose_stage_123?src="
            "http%3A%2F%2Fose-camera-route-relay.internal%3A9081%2Fv1%2F"
            "cameras%2FCAM-0123456789ABCDEF%2Fcandidate%2Fmain"
        )
        self.route_transport.responses[("PUT", path)] = (
            b'{"success":true}',
            "application/json",
        )

        self.engine.replace_private_route_source(
            stream_name="ose_stage_123", source=source
        )

        self.assertEqual(
            self.route_transport.calls,
            [("PUT", path, 64_000)],
        )
        for unsafe in (
            "http://user:secret@ose-camera-route-relay.internal:9081/"
            "v1/cameras/CAM-0123456789ABCDEF/candidate/main",
            "http://192.168.1.82:9080/stream.h264",
            "exec:camera-command",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                self.engine.replace_private_route_source(
                    stream_name="ose_stage_123", source=unsafe
                )

    def test_private_staging_stream_can_be_verified_and_removed(self):
        frame_path = "/api/frame.jpeg?src=ose_stage_123"
        self.transport.responses[frame_path] = (
            b"\xff\xd8frame\xff\xd9",
            "image/jpeg",
        )
        delete_path = "/api/go2rtc/streams/ose_stage_123"
        self.route_transport.responses[("DELETE", delete_path)] = (
            b'{"success":true}',
            "application/json",
        )

        frame = self.engine.read_private_route_frame(stream_name="ose_stage_123")
        self.engine.delete_private_route_source(stream_name="ose_stage_123")

        self.assertEqual(frame, b"\xff\xd8frame\xff\xd9")
        self.assertEqual(
            self.route_transport.calls[-1], ("DELETE", delete_path, 64_000)
        )

    def test_rejects_malformed_or_overscoped_event_data(self):
        path = "/api/review?cameras=front_door&limit=1&after=1"
        self.transport.responses[path] = (
            json.dumps(
                [
                    {
                        "id": "event-1",
                        "camera": "front_door",
                        "start_time": "not-a-timestamp",
                        "end_time": None,
                        "severity": "alert",
                        "data": {},
                    }
                ]
            ).encode(),
            "application/json",
        )
        with self.assertRaises(VideoEngineError):
            self.engine.list_events(camera_ids=("CAM-FRONT",), limit=1)
        with self.assertRaises(ValueError):
            self.engine.list_events(camera_ids=("CAM-NOT-BOUND",), limit=1)

    def test_private_configuration_round_trip_selects_only_a_loopback_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            saved = save_frigate_engine_configuration(
                directory,
                {"CAM-FRONT": "front_door"},
                port=15000,
            )
            loaded = load_configured_video_engine(directory)

            self.assertEqual(saved.port, 15000)
            self.assertIsInstance(loaded, FrigateEngine)
            self.assertEqual(loaded.camera_bindings, {"CAM-FRONT": "front_door"})
            self.assertFalse(loaded.route_relay_enabled)
            self.assertEqual(
                (directory / "video-engine.json").stat().st_mode & 0o777,
                0o600,
            )

    def test_private_configuration_can_enable_transactional_route_relay(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            save_frigate_engine_configuration(
                directory,
                {"CAM-FRONT": "front_door"},
                port=15000,
                route_relay_enabled=True,
            )

            loaded = load_configured_video_engine(directory)

            self.assertTrue(loaded.route_relay_enabled)
            document = json.loads(
                (directory / "video-engine.json").read_text(encoding="utf-8")
            )
            self.assertEqual(document["schema_version"], 2)
            self.assertTrue(document["route_relay_enabled"])

    @unittest.skipUnless(os.name == "posix", "POSIX permissions are required")
    def test_configuration_rejects_group_readable_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            save_frigate_engine_configuration(
                directory,
                {"CAM-FRONT": "front_door"},
                port=15000,
            )
            os.chmod(directory / "video-engine.json", 0o640)

            with self.assertRaises(VideoEngineError):
                load_configured_video_engine(directory)


if __name__ == "__main__":
    unittest.main()
