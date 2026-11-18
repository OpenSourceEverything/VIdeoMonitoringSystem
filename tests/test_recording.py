import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_monitoring_system.recorder import RecorderService
from video_monitoring_system.recording import (
    RecorderCaptureService,
    RecordingCaptureError,
    RecordingStorage,
    RecordingStorageError,
    RecordingVolumeAttestation,
)

JPEG_ONE = b"\xff\xd8frame-one\xff\xd9"
JPEG_TWO = b"\xff\xd8frame-two\xff\xd9"


class FrameSource:
    def __init__(self, frames, *, camera_id: str = "CAM-0001"):
        self.frames = frames
        self.camera_id = camera_id
        self.calls: list[tuple[str, int, int, float]] = []

    def get_mjpeg_frames(
        self,
        path: str,
        *,
        maximum: int,
        count: int,
        timeout_seconds: float,
    ):
        self.calls.append((path, maximum, count, timeout_seconds))
        return self.frames


def volume_attestation(
    root: Path,
    *,
    storage_reference: str = "hdd-main",
    volume_identity: str = "volume-uuid-1",
) -> RecordingVolumeAttestation:
    return RecordingVolumeAttestation(
        root=root,
        storage_reference=storage_reference,
        volume_identity=volume_identity,
        mounted=True,
        system_device=False,
    )


class RecordingCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        temporary = Path(self.temporary_directory.name)
        self.volume = temporary / "recording-volume"
        self.volume.mkdir()
        self.recorder = RecorderService(temporary / "recorder.json")
        self.recorder.plan_storage_initialization(storage_reference="hdd-main")
        self.recorder.confirm_storage_initialized(
            storage_reference="hdd-main",
            volume_identity="volume-uuid-1",
        )
        self.recorder.register_camera(
            camera_id="CAM-0001",
            claim_secret_hash=hashlib.sha256(b"claim").hexdigest(),
            identity_reference="identity:CAM-0001",
            pre_enrolled=True,
        )
        self.storage = RecordingStorage.initialize_layout(
            volume_attestation(self.volume),
            reserve_bytes=0,
        )
        started = datetime(2026, 8, 23, 12, 30, tzinfo=UTC)
        self.capture = RecorderCaptureService(
            self.recorder,
            self.storage,
            clock=lambda: started,
            recording_id_factory=lambda: "segment-0001",
        )

    def test_capacity_reports_only_bounded_space_facts(self) -> None:
        self.storage.reserve_bytes = 250
        with patch(
            "video_monitoring_system.recording.shutil.disk_usage",
            return_value=SimpleNamespace(total=10_000, used=4_000, free=6_000),
        ):
            capacity = self.storage.capacity()

        self.assertEqual(
            capacity,
            {
                "total_bytes": 10_000,
                "used_bytes": 4_000,
                "free_bytes": 6_000,
                "available_bytes": 5_750,
                "reserved_bytes": 250,
            },
        )
        self.assertNotIn(str(self.volume), repr(capacity))

    def test_capture_publishes_verified_segment_on_recording_volume(self) -> None:
        source = FrameSource((JPEG_ONE, JPEG_TWO))

        recording = self.capture.capture_segment(
            source=source,
            frame_count=2,
            maximum_bytes=4096,
        )

        self.assertEqual(
            source.calls,
            [(RecorderCaptureService.STREAM_PATH, 4096, 2, 30.0)],
        )
        self.assertEqual(recording.id, "segment-0001")
        self.assertEqual(recording.camera_id, "CAM-0001")
        self.assertEqual(recording.storage_reference, "hdd-main")
        self.assertEqual(recording.metadata["frame_count"], 2)
        segment = self.volume / str(recording.metadata["relative_directory"])
        manifest = json.loads((segment / "recording.json").read_text())
        media = (segment / "video.mjpeg").read_bytes()
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(len(manifest["sha256"]), 64)
        self.assertEqual(manifest["frame_count"], 2)
        self.assertLess(media.index(JPEG_ONE), media.index(JPEG_TWO))
        self.assertEqual(tuple(self.storage.incomplete_directory.iterdir()), ())

        listed = self.storage.list_recordings()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].id, "segment-0001")
        self.assertNotIn(str(self.volume), json.dumps(listed[0].to_dict()))
        played, media_type = self.storage.read_recording_media(listed[0].reference)
        self.assertEqual(played, media)
        self.assertEqual(media_type, RecordingStorage.MEDIA_TYPE)
        self.assertEqual(
            self.storage.read_recording_preview(listed[0].reference), JPEG_ONE
        )

    def test_retention_atomically_removes_only_expired_product_segments(self):
        old = self.capture.capture_segment(
            source=FrameSource((JPEG_ONE, JPEG_TWO)),
            frame_count=2,
            maximum_bytes=4096,
        )
        recent_time = datetime(2026, 8, 29, 12, 30, tzinfo=UTC)
        recent_capture = RecorderCaptureService(
            self.recorder,
            self.storage,
            clock=lambda: recent_time,
            recording_id_factory=lambda: "segment-recent",
        )
        recent = recent_capture.capture_segment(
            source=FrameSource((JPEG_ONE, JPEG_TWO)),
            frame_count=2,
            maximum_bytes=4096,
        )
        unrelated = self.volume / "legacy-camera-recordings"
        unrelated.mkdir()
        protected = unrelated / "keep.mjpeg"
        protected.write_bytes(b"legacy recording")

        # Backup export is deliberately bounded. Retention must remain able to
        # reclaim old recordings after a recorder grows beyond that bound.
        with patch.object(
            self.storage,
            "backup_catalog",
            side_effect=AssertionError("retention used the backup catalog"),
        ):
            result = self.storage.prune_before(
                datetime(2026, 8, 28, tzinfo=UTC)
            )

        self.assertEqual(result["deleted"], 1)
        self.assertGreater(result["reclaimed_bytes"], 0)
        self.assertFalse((self.volume / str(old.metadata["relative_directory"])).exists())
        self.assertTrue((self.volume / str(recent.metadata["relative_directory"])).is_dir())
        self.assertEqual([item.id for item in self.storage.list_recordings()], ["segment-recent"])
        self.assertEqual(protected.read_bytes(), b"legacy recording")
        self.assertEqual(tuple(self.storage.incomplete_directory.iterdir()), ())

    def test_retention_finishes_a_previously_retired_segment(self):
        retired = self.storage.incomplete_directory / "prune-interrupted.deleted"
        retired.mkdir()
        (retired / "recording.json").write_text("partial", encoding="utf-8")

        result = self.storage.prune_before(datetime(2026, 8, 28, tzinfo=UTC))

        self.assertEqual(result, {"deleted": 0, "reclaimed_bytes": 0})
        self.assertFalse(retired.exists())

    def test_retention_requires_an_aware_cutoff(self):
        with self.assertRaisesRegex(RecordingStorageError, "cutoff"):
            self.storage.prune_before(datetime(2026, 8, 28))

    def test_playback_rejects_untrusted_or_mismatched_references(self) -> None:
        self.capture.capture_segment(
            source=FrameSource((JPEG_ONE, JPEG_TWO)),
            frame_count=2,
            maximum_bytes=4096,
        )
        recording = self.storage.list_recordings()[0]

        for reference in (
            "../recording.json",
            "/recordings/CAM-0001/2026/08/23/segment",
            recording.reference + "/video.mjpeg",
        ):
            with self.subTest(reference=reference), self.assertRaisesRegex(
                RecordingStorageError, "reference"
            ):
                self.storage.read_recording_media(reference)
            with self.subTest(preview_reference=reference), self.assertRaisesRegex(
                RecordingStorageError, "reference"
            ):
                self.storage.read_recording_preview(reference)

        media_path = self.volume / recording.reference / "video.mjpeg"
        original_media = media_path.read_bytes()
        tampered_media = bytearray(original_media)
        tampered_media[len(tampered_media) // 2] ^= 1
        media_path.write_bytes(tampered_media)
        with self.assertRaisesRegex(RecordingStorageError, "incomplete"):
            self.storage.read_recording_media(recording.reference)
        media_path.write_bytes(original_media)

        manifest_path = self.volume / recording.reference / "recording.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["camera_id"] = "CAM-OTHER"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.storage.list_recordings(), ())
        with self.assertRaisesRegex(RecordingStorageError, "did not match"):
            self.storage.read_recording_media(recording.reference)

    def test_end_time_is_read_after_lazy_stream_consumption(self) -> None:
        started = datetime(2026, 8, 23, 12, 30, tzinfo=UTC)
        ended = datetime(2026, 8, 23, 12, 31, tzinfo=UTC)
        clock_calls = 0
        stream_consumed = False

        def clock() -> datetime:
            nonlocal clock_calls
            clock_calls += 1
            return ended if stream_consumed else started

        def lazy_frames():
            nonlocal stream_consumed
            self.assertEqual(clock_calls, 1)
            yield JPEG_ONE
            yield JPEG_TWO
            stream_consumed = True

        capture = RecorderCaptureService(
            self.recorder,
            self.storage,
            clock=clock,
            recording_id_factory=lambda: "segment-lazy",
        )

        recording = capture.capture_segment(
            source=FrameSource(lazy_frames()),
            frame_count=2,
            maximum_bytes=4096,
        )

        self.assertTrue(stream_consumed)
        self.assertEqual(clock_calls, 2)
        self.assertEqual(recording.ended_at, ended)
        segment = self.volume / str(recording.metadata["relative_directory"])
        manifest = json.loads((segment / "recording.json").read_text())
        self.assertEqual(manifest["ended_at"], ended.isoformat())

    def test_capture_syncs_publication_source_and_destination_directories(
        self,
    ) -> None:
        with patch("video_monitoring_system.recording._sync_directory") as sync:
            recording = self.capture.capture_segment(
                source=FrameSource((JPEG_ONE, JPEG_TWO)),
                frame_count=2,
                maximum_bytes=4096,
            )

        segment = self.volume / str(recording.metadata["relative_directory"])
        synced = [call.args[0] for call in sync.call_args_list]
        self.assertIn(self.storage.incomplete_directory, synced)
        self.assertIn(segment.parent, synced)
        self.assertIn(segment, synced)

    def test_layout_syncs_marker_file_and_parent_directory(self) -> None:
        other_volume = Path(self.temporary_directory.name) / "fresh-volume"
        other_volume.mkdir()
        with (
            patch("video_monitoring_system.recording.os.fsync") as fsync,
            patch("video_monitoring_system.recording._sync_directory") as sync,
        ):
            RecordingStorage.initialize_layout(
                volume_attestation(
                    other_volume,
                    volume_identity="fresh-volume-uuid",
                ),
                reserve_bytes=0,
            )

        self.assertGreaterEqual(fsync.call_count, 1)
        self.assertEqual(sync.call_args_list[-1].args[0], other_volume)

    def test_stream_adapter_receives_caller_bounded_timeout(self) -> None:
        source = FrameSource((JPEG_ONE, JPEG_TWO))

        self.capture.capture_segment(
            source=source,
            frame_count=2,
            maximum_bytes=4096,
            timeout_seconds=12.5,
        )

        self.assertEqual(source.calls[0][-1], 12.5)

    def test_invalid_stream_timeout_fails_before_camera_access(self) -> None:
        for timeout in (0, -1, float("inf"), 301, 10**10000, True):
            source = FrameSource((JPEG_ONE, JPEG_TWO))
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(RecordingCaptureError, "timeout"),
            ):
                self.capture.capture_segment(
                    source=source,
                    frame_count=2,
                    timeout_seconds=timeout,
                )
            self.assertEqual(source.calls, [])

    def test_missing_volume_marker_fails_before_camera_access(self) -> None:
        (self.volume / RecordingStorage.MARKER_NAME).unlink()
        source = FrameSource((JPEG_ONE, JPEG_TWO))

        with self.assertRaisesRegex(RecordingCaptureError, "not ready"):
            self.capture.capture_segment(source=source, frame_count=2)

        self.assertEqual(source.calls, [])

    def test_wrong_volume_identity_fails_before_camera_access(self) -> None:
        other_volume = Path(self.temporary_directory.name) / "other-volume"
        other_volume.mkdir()
        other_storage = RecordingStorage.initialize_layout(
            volume_attestation(
                other_volume,
                storage_reference="other-hdd",
                volume_identity="volume-uuid-2",
            ),
            reserve_bytes=0,
        )
        source = FrameSource((JPEG_ONE, JPEG_TWO))
        capture = RecorderCaptureService(self.recorder, other_storage)

        with self.assertRaisesRegex(RecordingCaptureError, "not ready"):
            capture.capture_segment(source=source, frame_count=2)

        self.assertEqual(source.calls, [])

    def test_marker_is_bound_to_attested_stable_volume_identity(self) -> None:
        wrong_attestation = volume_attestation(
            self.volume,
            volume_identity="different-volume-uuid",
        )

        with self.assertRaisesRegex(RecordingStorageError, "identity did not match"):
            RecordingStorage(wrong_attestation, reserve_bytes=0)

    def test_storage_api_requires_safe_platform_attestation(self) -> None:
        with self.assertRaisesRegex(RecordingStorageError, "attestation is required"):
            RecordingStorage.initialize_layout(self.volume)  # type: ignore[arg-type]
        with self.assertRaisesRegex(RecordingStorageError, "not attested as mounted"):
            RecordingVolumeAttestation(
                root=self.volume,
                storage_reference="hdd-main",
                volume_identity="volume-uuid-1",
                mounted=False,
                system_device=False,
            )
        with self.assertRaisesRegex(RecordingStorageError, "system device"):
            RecordingVolumeAttestation(
                root=self.volume,
                storage_reference="hdd-main",
                volume_identity="volume-uuid-1",
                mounted=True,
                system_device=True,
            )

    def test_camera_must_be_authorized_before_stream_access(self) -> None:
        source = FrameSource((JPEG_ONE, JPEG_TWO), camera_id="CAM-UNKNOWN")

        with self.assertRaisesRegex(RecordingCaptureError, "not authorized"):
            self.capture.capture_segment(source=source, frame_count=2)

        self.assertEqual(source.calls, [])

    def test_invalid_incomplete_and_oversized_streams_leave_no_partial_data(
        self,
    ) -> None:
        large_jpeg = b"\xff\xd8" + b"x" * 1024 + b"\xff\xd9"
        cases = (
            ((JPEG_ONE,), 4096, "incomplete"),
            ((b"not-a-jpeg", JPEG_TWO), 4096, "invalid frame"),
            ((large_jpeg, JPEG_TWO), 1024, "byte limit"),
            ((JPEG_ONE, JPEG_TWO, JPEG_ONE), 4096, "too many"),
        )
        for frames, maximum, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(RecordingCaptureError, message),
            ):
                self.capture.capture_segment(
                    source=FrameSource(frames),
                    frame_count=2,
                    maximum_bytes=maximum,
                )
            self.assertEqual(tuple(self.storage.incomplete_directory.iterdir()), ())
            self.assertEqual(
                tuple(self.storage.recordings_directory.rglob("*.mjpeg")), ()
            )

    def test_stream_failure_is_redacted_and_cleans_staging(self) -> None:
        def failed_stream():
            yield JPEG_ONE
            raise RuntimeError("private endpoint details")

        with self.assertRaisesRegex(
            RecordingCaptureError, "could not be captured"
        ) as captured:
            self.capture.capture_segment(
                source=FrameSource(failed_stream()),
                frame_count=2,
            )

        self.assertNotIn("private endpoint", str(captured.exception))
        self.assertEqual(tuple(self.storage.incomplete_directory.iterdir()), ())

    def test_capacity_reserve_is_checked_before_stream_access(self) -> None:
        source = FrameSource((JPEG_ONE, JPEG_TWO))
        with (
            patch(
                "video_monitoring_system.recording.shutil.disk_usage",
                return_value=SimpleNamespace(free=1),
            ),
            self.assertRaisesRegex(RecordingCaptureError, "safely published"),
        ):
            self.capture.capture_segment(
                source=source,
                frame_count=2,
                maximum_bytes=4096,
            )

        self.assertEqual(len(source.calls), 1)
        self.assertEqual(tuple(self.storage.incomplete_directory.iterdir()), ())

    def test_unmarked_nonempty_directory_is_never_claimed_as_storage(self) -> None:
        other_volume = Path(self.temporary_directory.name) / "personal-files"
        other_volume.mkdir()
        (other_volume / "keep.txt").write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(RecordingStorageError, "must be empty"):
            RecordingStorage.initialize_layout(
                volume_attestation(
                    other_volume,
                    storage_reference="other-hdd",
                    volume_identity="volume-uuid-2",
                ),
            )

        self.assertEqual((other_volume / "keep.txt").read_text(), "keep")

    def test_explicit_adoption_preserves_unrelated_existing_data(self) -> None:
        other_volume = Path(self.temporary_directory.name) / "legacy-recordings"
        other_volume.mkdir()
        legacy = other_volume / "vms-recordings"
        legacy.mkdir()
        protected = legacy / "keep.mjpeg"
        protected.write_bytes(b"legacy recording")
        attestation = volume_attestation(
            other_volume,
            storage_reference="other-hdd",
            volume_identity="volume-uuid-2",
        )

        storage = RecordingStorage.adopt_layout(attestation)

        self.assertEqual(protected.read_bytes(), b"legacy recording")
        self.assertTrue(storage.recordings_directory.is_dir())
        self.assertTrue(storage.incomplete_directory.is_dir())
        self.assertTrue((other_volume / RecordingStorage.MARKER_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
