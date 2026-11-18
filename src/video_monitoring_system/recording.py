"""Bounded recorder capture into an initialized recording volume.

The capture layer consumes an authenticated camera transport and never owns
network discovery or credentials. Storage arrives as a trusted platform
attestation, is marker-verified, and is published as immutable segment
directories. Physical mount discovery remains the platform adapter's job.
"""

from __future__ import annotations

import hashlib
import heapq
import hmac
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from .model import Recording
from .recorder import RecorderService


class RecordingStorageError(RuntimeError):
    """The configured recording volume is absent, unsafe, or unreadable."""


class RecordingCaptureError(RuntimeError):
    """A camera segment could not be captured and safely published."""


class MjpegFrameSource(Protocol):
    """Authenticated camera transport with an adapter-enforced time bound."""

    @property
    def camera_id(self) -> str:
        """Return the identity authenticated by the transport adapter."""
        ...

    def get_mjpeg_frames(
        self,
        path: str,
        *,
        maximum: int,
        count: int,
        timeout_seconds: float,
    ) -> Iterable[bytes]:
        """Return frames while enforcing the bound through iterator exhaustion."""
        ...


_STORAGE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_VOLUME_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_CAMERA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_RECORDING_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_YEAR = re.compile(r"[0-9]{4}")
_MONTH_DAY = re.compile(r"[0-9]{2}")
_SEGMENT_DIRECTORY = re.compile(r"[0-9]{12}Z-[a-z0-9][a-z0-9-]{0,63}")
_MAXIMUM_MANIFEST_BYTES = 64 * 1024
_MAXIMUM_PLAYBACK_BYTES = 80 * 1024 * 1024
_MAXIMUM_PREVIEW_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class RecordingVolumeAttestation:
    """Trusted platform-adapter proof for one mounted recording volume.

    The platform adapter, not the recording core, establishes mount and device
    facts.  Requiring this capability keeps a caller from passing an arbitrary
    directory to the storage API.
    """

    root: Path
    storage_reference: str
    volume_identity: str
    mounted: bool
    system_device: bool

    def __post_init__(self) -> None:
        try:
            root = Path(self.root)
        except TypeError as error:
            raise RecordingStorageError(
                "Recording volume attestation root is invalid."
            ) from error
        object.__setattr__(self, "root", root)
        object.__setattr__(
            self,
            "storage_reference",
            _storage_reference(self.storage_reference),
        )
        object.__setattr__(
            self,
            "volume_identity",
            _volume_identity(self.volume_identity),
        )
        if self.mounted is not True:
            raise RecordingStorageError("Recording volume is not attested as mounted.")
        if self.system_device is not False:
            raise RecordingStorageError(
                "The recorder system device cannot store recordings."
            )
        _validate_absolute_directory(root)


@dataclass(frozen=True, slots=True)
class StoredRecording:
    id: str
    camera_id: str
    started_at: datetime
    ended_at: datetime
    frame_count: int
    encoded_bytes: int
    container_bytes: int
    sha256: str
    reference: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "frame_count": self.frame_count,
            "encoded_bytes": self.encoded_bytes,
            "reference": self.reference,
        }


class RecordingStorage:
    """One configured filesystem root reserved for recorder media."""

    SCHEMA_VERSION: Final[int] = 1
    RECORDING_SCHEMA_VERSION: Final[int] = 2
    MARKER_NAME: Final[str] = ".vms-recording-storage.json"
    RECORDINGS_DIRECTORY: Final[str] = "recordings"
    INCOMPLETE_DIRECTORY: Final[str] = ".incomplete"
    MEDIA_TYPE: Final[str] = "multipart/x-mixed-replace; boundary=vms-frame"
    _OWNED_ENTRIES: Final[frozenset[str]] = frozenset(
        {MARKER_NAME, RECORDINGS_DIRECTORY, INCOMPLETE_DIRECTORY, "lost+found"}
    )

    def __init__(
        self,
        attestation: RecordingVolumeAttestation,
        *,
        reserve_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        if not isinstance(attestation, RecordingVolumeAttestation):
            raise RecordingStorageError(
                "A trusted recording-volume attestation is required."
            )
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise RecordingStorageError("Recording storage reserve is invalid.")
        self.attestation = attestation
        self.root = attestation.root
        self.storage_reference = attestation.storage_reference
        self.volume_identity = attestation.volume_identity
        self.reserve_bytes = reserve_bytes
        self._runtime_device_id = _filesystem_device(self.root)
        self.recordings_directory = self.root / self.RECORDINGS_DIRECTORY
        self.incomplete_directory = self.root / self.INCOMPLETE_DIRECTORY
        self._validate_root()
        self._validate_marker()
        for directory in (self.recordings_directory, self.incomplete_directory):
            if directory.is_symlink() or not directory.is_dir():
                raise RecordingStorageError(
                    "Recording storage layout is incomplete or unsafe."
                )

    @classmethod
    def initialize_layout(
        cls,
        attestation: RecordingVolumeAttestation,
        *,
        reserve_bytes: int = 256 * 1024 * 1024,
    ) -> RecordingStorage:
        """Create only the VMS layout on a volume initialized by its adapter.

        This method never partitions, formats, mounts, or discovers a device.
        It refuses a nonempty unmarked directory so the caller cannot stamp an
        arbitrary filesystem as recording storage.
        """

        return cls._create_layout(
            attestation,
            reserve_bytes=reserve_bytes,
            allow_unowned_entries=False,
        )

    @classmethod
    def adopt_layout(
        cls,
        attestation: RecordingVolumeAttestation,
        *,
        reserve_bytes: int = 256 * 1024 * 1024,
    ) -> RecordingStorage:
        """Add the VMS layout without altering unrelated existing files.

        Adoption is an explicit migration operation. It still refuses unsafe
        product-owned paths and relies on the same mounted-volume attestation
        as fresh initialization.
        """

        return cls._create_layout(
            attestation,
            reserve_bytes=reserve_bytes,
            allow_unowned_entries=True,
        )

    @classmethod
    def _create_layout(
        cls,
        attestation: RecordingVolumeAttestation,
        *,
        reserve_bytes: int,
        allow_unowned_entries: bool,
    ) -> RecordingStorage:
        if not isinstance(attestation, RecordingVolumeAttestation):
            raise RecordingStorageError(
                "A trusted recording-volume attestation is required."
            )
        volume = attestation.root
        reference = attestation.storage_reference
        volume_identity = attestation.volume_identity
        _validate_absolute_directory(volume)
        initial_device_id = _filesystem_device(volume)
        marker = volume / cls.MARKER_NAME
        if marker.exists() or marker.is_symlink():
            return cls(
                attestation,
                reserve_bytes=reserve_bytes,
            )

        try:
            entries = tuple(volume.iterdir())
        except OSError as error:
            raise RecordingStorageError(
                "Recording storage could not be read."
            ) from error
        unexpected = sorted(
            entry.name for entry in entries if entry.name not in cls._OWNED_ENTRIES
        )
        if unexpected and not allow_unowned_entries:
            raise RecordingStorageError(
                "Recording storage must be empty before its layout is initialized."
            )
        for name in (cls.RECORDINGS_DIRECTORY, cls.INCOMPLETE_DIRECTORY):
            directory = volume / name
            if directory.is_symlink() or (
                directory.exists() and not directory.is_dir()
            ):
                raise RecordingStorageError(
                    "Recording storage layout is incomplete or unsafe."
                )
            created = not directory.exists()
            directory.mkdir(mode=0o700, exist_ok=True)
            _private_mode(directory, 0o700)
            if created:
                _sync_directory(directory)
                _sync_directory(volume)
            try:
                if any(directory.iterdir()):
                    raise RecordingStorageError(
                        "Unmarked recording storage contains application data."
                    )
            except OSError as error:
                raise RecordingStorageError(
                    "Recording storage layout could not be read."
                ) from error

        if _filesystem_device(volume) != initial_device_id:
            raise RecordingStorageError(
                "Recording volume changed while its layout was initialized."
            )
        _atomic_json(
            marker,
            {
                "schema_version": cls.SCHEMA_VERSION,
                "state": "ready",
                "storage_reference": reference,
                "volume_identity": volume_identity,
            },
        )
        return cls(
            attestation,
            reserve_bytes=reserve_bytes,
        )

    def validate(self) -> None:
        """Re-attest the configured volume before and after stream access."""

        self._validate_root()
        self._validate_marker()
        for directory in (self.recordings_directory, self.incomplete_directory):
            if directory.is_symlink() or not directory.is_dir():
                raise RecordingStorageError(
                    "Recording storage layout is incomplete or unsafe."
                )

    def capacity(self) -> dict[str, int]:
        """Return bounded, non-identifying capacity facts for this volume."""

        self.validate()
        try:
            usage = shutil.disk_usage(self.root)
        except OSError as error:
            raise RecordingStorageError(
                "Recording storage capacity could not be read."
            ) from error
        if min(usage.total, usage.used, usage.free) < 0:
            raise RecordingStorageError("Recording storage capacity is invalid.")
        self.validate()
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "available_bytes": max(0, usage.free - self.reserve_bytes),
            "reserved_bytes": min(self.reserve_bytes, usage.free),
        }

    def list_recordings(self, *, limit: int = 100) -> tuple[StoredRecording, ...]:
        """Return recent recordings without traversing older date partitions."""

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RecordingStorageError("Recording list limit is invalid.")
        selected = []
        for recording in self._recordings_newest_first():
            selected.append(recording)
            if len(selected) == limit:
                break
        self.validate()
        return tuple(selected)

    def backup_catalog(
        self, *, maximum: int = 100_000
    ) -> tuple[StoredRecording, ...]:
        """Return all recording metadata for an owner-requested backup.

        Unlike the interactive event list, this intentionally traverses every
        date partition.  The generous hard limit prevents an unexpectedly
        corrupt or hostile directory tree from exhausting recorder memory.
        """

        if type(maximum) is not int or not 1 <= maximum <= 1_000_000:
            raise RecordingStorageError("Recording catalog limit is invalid.")
        selected = []
        for recording in self._recordings_newest_first():
            if len(selected) == maximum:
                raise RecordingStorageError("Recording catalog is too large to back up.")
            selected.append(recording)
        self.validate()
        return tuple(selected)

    def prune_before(self, cutoff: datetime) -> dict[str, int]:
        """Remove validated recordings older than an owner-selected cutoff.

        Each completed segment is first atomically moved out of the published
        catalog.  This makes an interrupted maintenance run look like a missing
        old segment, never a partially deleted playable recording.
        """

        if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
            raise RecordingStorageError("Recording retention cutoff is invalid.")
        cutoff = cutoff.astimezone(UTC)
        self._remove_retired_recordings()
        deleted = 0
        reclaimed = 0
        # Retention is an oldest-first storage operation, not a backup.  Walk
        # the on-disk partitions incrementally so cleanup continues to work
        # after the catalog exceeds the intentionally bounded backup export.
        for recording in self._recordings_oldest_first():
            if recording.started_at >= cutoff:
                break
            if recording.ended_at >= cutoff:
                continue
            directory = self.root.joinpath(*_recording_reference(recording.reference))
            verified = self._load_recording(directory)
            if verified != recording:
                raise RecordingStorageError("Recording changed during maintenance.")
            try:
                entries = tuple(directory.iterdir())
            except OSError as error:
                raise RecordingStorageError(
                    "Recording could not be checked for retention."
                ) from error
            if (
                len(entries) != 2
                or {entry.name for entry in entries}
                != {"recording.json", "video.mjpeg"}
                or any(entry.is_symlink() or not entry.is_file() for entry in entries)
            ):
                raise RecordingStorageError(
                    "Recording layout is unsafe for retention."
                )
            retired = self.incomplete_directory / f"prune-{uuid.uuid4().hex}.deleted"
            try:
                os.replace(directory, retired)
                _sync_directory(directory.parent)
                _sync_directory(self.incomplete_directory)
                shutil.rmtree(retired)
                _sync_directory(self.incomplete_directory)
            except OSError as error:
                raise RecordingStorageError(
                    "Recording retention could not be completed."
                ) from error
            deleted += 1
            reclaimed += recording.container_bytes
            self._remove_empty_recording_parents(directory.parent)
        self.validate()
        return {"deleted": deleted, "reclaimed_bytes": reclaimed}

    def _recordings_newest_first(self) -> Iterator[StoredRecording]:
        yield from self._recordings_by_age(newest_first=True)

    def _recordings_oldest_first(self) -> Iterator[StoredRecording]:
        yield from self._recordings_by_age(newest_first=False)

    def _recordings_by_age(
        self, *, newest_first: bool
    ) -> Iterator[StoredRecording]:
        self.validate()
        iterators = []
        for camera_directory in _matching_directories(
            self.recordings_directory, _CAMERA_ID
        ):
            iterator = self._camera_recordings(
                camera_directory, newest_first=newest_first
            )
            try:
                recording = next(iterator)
            except StopIteration:
                continue
            heapq.heappush(
                iterators,
                (
                    recording.started_at.timestamp()
                    * (-1 if newest_first else 1),
                    recording.reference,
                    recording,
                    iterator,
                ),
            )

        while iterators:
            _, _, recording, iterator = heapq.heappop(iterators)
            yield recording
            try:
                following = next(iterator)
            except StopIteration:
                continue
            heapq.heappush(
                iterators,
                (
                    following.started_at.timestamp()
                    * (-1 if newest_first else 1),
                    following.reference,
                    following,
                    iterator,
                ),
            )

    def read_recording_media(self, reference: str) -> tuple[bytes, str]:
        """Read one validated bounded recording selected by its public reference."""

        self.validate()
        parts = _recording_reference(reference)
        directory = self.root.joinpath(*parts)
        recording = self._load_recording(directory)
        if recording.reference != reference:
            raise RecordingStorageError("Recording reference did not match.")
        media = directory / "video.mjpeg"
        if recording.container_bytes > _MAXIMUM_PLAYBACK_BYTES:
            raise RecordingStorageError("Recording is too large for playback.")
        try:
            content = media.read_bytes()
        except OSError as error:
            raise RecordingStorageError("Recording media could not be read.") from error
        if (
            len(content) != recording.container_bytes
            or not hmac.compare_digest(
                hashlib.sha256(content).hexdigest(), recording.sha256
            )
            or not content.startswith(b"--vms-frame\r\n")
            or not content.endswith(b"--vms-frame--\r\n")
        ):
            raise RecordingStorageError("Recording media is incomplete.")
        self.validate()
        return content, self.MEDIA_TYPE

    def read_recording_preview(self, reference: str) -> bytes:
        """Read only the first bounded JPEG frame for a lightweight event tile."""

        self.validate()
        parts = _recording_reference(reference)
        directory = self.root.joinpath(*parts)
        recording = self._load_recording(directory)
        if recording.reference != reference:
            raise RecordingStorageError("Recording reference did not match.")
        media = directory / "video.mjpeg"
        try:
            with media.open("rb") as stream:
                boundary = stream.readline(64)
                content_type = stream.readline(64)
                content_length = stream.readline(64)
                separator = stream.readline(4)
                if (
                    boundary != b"--vms-frame\r\n"
                    or content_type != b"Content-Type: image/jpeg\r\n"
                    or not content_length.startswith(b"Content-Length: ")
                    or not content_length.endswith(b"\r\n")
                    or separator != b"\r\n"
                ):
                    raise RecordingStorageError("Recording preview is invalid.")
                length_text = content_length[len(b"Content-Length: ") : -2]
                if not length_text.isdigit():
                    raise RecordingStorageError("Recording preview is invalid.")
                length = int(length_text)
                if not 4 <= length <= _MAXIMUM_PREVIEW_BYTES:
                    raise RecordingStorageError("Recording preview is invalid.")
                frame = stream.read(length)
                if (
                    len(frame) != length
                    or not frame.startswith(b"\xff\xd8")
                    or not frame.endswith(b"\xff\xd9")
                    or stream.read(2) != b"\r\n"
                ):
                    raise RecordingStorageError("Recording preview is invalid.")
        except RecordingStorageError:
            raise
        except OSError as error:
            raise RecordingStorageError("Recording preview could not be read.") from error
        self.validate()
        return frame

    def _camera_recordings(
        self, camera_directory: Path, *, newest_first: bool = True
    ) -> Iterator[StoredRecording]:
        for year in _matching_directories(
            camera_directory, _YEAR, reverse=newest_first
        ):
            for month in _matching_directories(
                year, _MONTH_DAY, reverse=newest_first
            ):
                for day in _matching_directories(
                    month, _MONTH_DAY, reverse=newest_first
                ):
                    for segment in _matching_directories(
                        day, _SEGMENT_DIRECTORY, reverse=newest_first
                    ):
                        try:
                            yield self._load_recording(segment)
                        except RecordingStorageError:
                            continue

    def _load_recording(self, directory: Path) -> StoredRecording:
        try:
            relative = directory.relative_to(self.root).as_posix()
        except ValueError as error:
            raise RecordingStorageError(
                "Recording path escaped configured storage."
            ) from error
        parts = _recording_reference(relative)
        manifest_path = directory / "recording.json"
        media_path = directory / "video.mjpeg"
        if (
            directory.is_symlink()
            or manifest_path.is_symlink()
            or media_path.is_symlink()
            or not manifest_path.is_file()
            or not media_path.is_file()
        ):
            raise RecordingStorageError("Recording layout is incomplete or unsafe.")
        try:
            if manifest_path.stat().st_size > _MAXIMUM_MANIFEST_BYTES:
                raise RecordingStorageError("Recording manifest is invalid.")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            media_size = media_path.stat().st_size
        except RecordingStorageError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RecordingStorageError("Recording manifest could not be read.") from error
        if not isinstance(manifest, dict):
            raise RecordingStorageError("Recording manifest is invalid.")
        try:
            recording_id = _recording_id(manifest["recording_id"])
            camera_id = _camera_id(manifest["camera_id"])
            started = _stored_datetime(manifest["started_at"])
            ended = _stored_datetime(manifest["ended_at"])
            frame_count = _bounded_integer(manifest["frame_count"], 2, 10_000)
            encoded_bytes = _bounded_integer(
                manifest["encoded_bytes"],
                1,
                RecorderCaptureService.DEFAULT_MAXIMUM_BYTES,
            )
            container_bytes = _bounded_integer(
                manifest["container_bytes"], encoded_bytes, _MAXIMUM_PLAYBACK_BYTES
            )
            sha256 = manifest["sha256"]
        except (KeyError, RecordingCaptureError, RecordingStorageError) as error:
            raise RecordingStorageError("Recording manifest is invalid.") from error
        expected_segment = f"{started:%H%M%S}{started.microsecond:06d}Z-{recording_id}"
        if (
            manifest.get("schema_version") != self.RECORDING_SCHEMA_VERSION
            or manifest.get("state") != "complete"
            or manifest.get("media_type") != self.MEDIA_TYPE
            or manifest.get("media_file") != "video.mjpeg"
            or manifest.get("storage_reference") != self.storage_reference
            or manifest.get("relative_directory") != relative
            or camera_id != parts[1]
            or parts[2:5] != (
                f"{started.year:04d}",
                f"{started.month:02d}",
                f"{started.day:02d}",
            )
            or parts[5] != expected_segment
            or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None
            or ended < started
            or media_size != container_bytes
        ):
            raise RecordingStorageError("Recording manifest did not match its media.")
        return StoredRecording(
            id=recording_id,
            camera_id=camera_id,
            started_at=started,
            ended_at=ended,
            frame_count=frame_count,
            encoded_bytes=encoded_bytes,
            container_bytes=container_bytes,
            sha256=sha256,
            reference=relative,
        )

    def _remove_empty_recording_parents(self, start: Path) -> None:
        current = start
        while current != self.recordings_directory:
            try:
                current.relative_to(self.recordings_directory)
                current.rmdir()
                _sync_directory(current.parent)
            except ValueError as error:
                raise RecordingStorageError(
                    "Recording retention path escaped storage."
                ) from error
            except OSError:
                return
            current = current.parent

    def _remove_retired_recordings(self) -> None:
        try:
            entries = tuple(self.incomplete_directory.iterdir())
        except OSError as error:
            raise RecordingStorageError(
                "Recording retention staging could not be read."
            ) from error
        for entry in entries:
            if not (
                entry.name.startswith("prune-")
                and entry.name.endswith(".deleted")
            ):
                continue
            if entry.is_symlink() or not entry.is_dir():
                raise RecordingStorageError(
                    "Recording retention staging is unsafe."
                )
            try:
                shutil.rmtree(entry)
                _sync_directory(self.incomplete_directory)
            except OSError as error:
                raise RecordingStorageError(
                    "Recording retention cleanup could not be completed."
                ) from error

    def publish_segment(
        self,
        *,
        recording_id: str,
        camera_id: str,
        started_at: datetime,
        completion_clock: Callable[[], datetime],
        frames: Iterable[bytes],
        expected_frame_count: int,
        maximum_bytes: int,
    ) -> Recording:
        recording_id = _recording_id(recording_id)
        camera_id = _camera_id(camera_id)
        started_at = _utc_datetime(started_at, "Recording start")
        if not callable(completion_clock):
            raise RecordingCaptureError("Recording completion clock is invalid.")
        if (
            type(expected_frame_count) is not int
            or not 2 <= expected_frame_count <= 10_000
        ):
            raise RecordingCaptureError(
                "Recording segments require between 2 and 10000 frames."
            )
        if type(maximum_bytes) is not int or maximum_bytes < 1024:
            raise RecordingCaptureError("Recording byte limit is invalid.")
        if isinstance(frames, (bytes, bytearray, str)):
            raise RecordingCaptureError("Camera stream returned invalid frames.")
        try:
            frame_iterator = iter(frames)
        except TypeError as error:
            raise RecordingCaptureError(
                "Camera stream returned invalid frames."
            ) from error
        self.validate()
        # Reserve for the full bounded segment before consuming a remote stream.
        # Container overhead is deliberately overestimated per frame.
        self._require_capacity(maximum_bytes + expected_frame_count * 128)

        relative_parent = Path(
            self.RECORDINGS_DIRECTORY,
            camera_id,
            f"{started_at.year:04d}",
            f"{started_at.month:02d}",
            f"{started_at.day:02d}",
        )
        destination_parent = self.root / relative_parent
        _create_private_directories(self.root, destination_parent)
        directory_name = (
            f"{started_at:%H%M%S}{started_at.microsecond:06d}Z-{recording_id}"
        )
        destination = destination_parent / directory_name
        if destination.exists() or destination.is_symlink():
            raise RecordingCaptureError("Recording identity already exists.")

        try:
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f"{recording_id}.",
                    suffix=".partial",
                    dir=self.incomplete_directory,
                )
            )
            _private_mode(staging, 0o700)
            _sync_directory(staging)
            _sync_directory(self.incomplete_directory)
        except OSError as error:
            raise RecordingCaptureError(
                "Recording could not reserve temporary storage."
            ) from error

        try:
            media = staging / "video.mjpeg"
            encoded_bytes = 0
            frame_count = 0
            media_digest = hashlib.sha256()
            with media.open("xb") as stream:
                _private_mode(media, 0o600)

                def write(data: bytes) -> None:
                    stream.write(data)
                    media_digest.update(data)

                for frame in frame_iterator:
                    if frame_count >= expected_frame_count:
                        raise RecordingCaptureError(
                            "Camera stream returned too many recording frames."
                        )
                    if (
                        not isinstance(frame, bytes)
                        or len(frame) < 4
                        or not frame.startswith(b"\xff\xd8")
                        or not frame.endswith(b"\xff\xd9")
                    ):
                        raise RecordingCaptureError(
                            "Camera stream returned an invalid frame."
                        )
                    if encoded_bytes + len(frame) > maximum_bytes:
                        raise RecordingCaptureError(
                            "Camera stream exceeded its byte limit."
                        )
                    encoded_bytes += len(frame)
                    frame_count += 1
                    write(b"--vms-frame\r\nContent-Type: image/jpeg\r\n")
                    write(
                        f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    )
                    write(frame)
                    write(b"\r\n")
                if frame_count != expected_frame_count:
                    raise RecordingCaptureError(
                        "Camera stream returned an incomplete recording segment."
                    )
                write(b"--vms-frame--\r\n")
                stream.flush()
                os.fsync(stream.fileno())
            ended_at = _utc_datetime(completion_clock(), "Recording end")
            ended_at = max(ended_at, started_at)

            relative_directory = (relative_parent / directory_name).as_posix()
            manifest = {
                "schema_version": self.RECORDING_SCHEMA_VERSION,
                "state": "complete",
                "recording_id": recording_id,
                "camera_id": camera_id,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "frame_count": frame_count,
                "encoded_bytes": encoded_bytes,
                "container_bytes": media.stat().st_size,
                "sha256": media_digest.hexdigest(),
                "media_type": self.MEDIA_TYPE,
                "media_file": media.name,
                "storage_reference": self.storage_reference,
                "relative_directory": relative_directory,
            }
            _atomic_json(staging / "recording.json", manifest)
            self.validate()
            os.replace(staging, destination)
            _sync_directory(destination)
            _sync_directory(self.incomplete_directory)
            _sync_directory(destination_parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        return Recording(
            id=recording_id,
            camera_id=camera_id,
            started_at=started_at,
            ended_at=ended_at,
            storage_reference=self.storage_reference,
            metadata={
                "frame_count": frame_count,
                "encoded_bytes": encoded_bytes,
                "relative_directory": relative_directory,
                "media_file": "video.mjpeg",
            },
        )

    def _validate_root(self) -> None:
        _validate_absolute_directory(self.root)
        if _filesystem_device(self.root) != self._runtime_device_id:
            raise RecordingStorageError("Configured recording volume mount changed.")

    def _validate_marker(self) -> None:
        marker = self.root / self.MARKER_NAME
        if marker.is_symlink() or not marker.is_file():
            raise RecordingStorageError(
                "Configured recording storage is not initialized."
            )
        try:
            document = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RecordingStorageError(
                "Recording storage marker could not be read."
            ) from error
        expected = {
            "schema_version": self.SCHEMA_VERSION,
            "state": "ready",
            "storage_reference": self.storage_reference,
            "volume_identity": self.volume_identity,
        }
        if document != expected:
            raise RecordingStorageError(
                "Configured recording storage identity did not match."
            )

    def _require_capacity(self, required_bytes: int) -> None:
        try:
            free = shutil.disk_usage(self.root).free
        except OSError as error:
            raise RecordingStorageError(
                "Recording storage capacity could not be read."
            ) from error
        if free < self.reserve_bytes + required_bytes:
            raise RecordingStorageError(
                "Recording storage does not have enough reserved free space."
            )


class RecorderCaptureService:
    """Capture bounded segments for the recorder's authorized cameras."""

    STREAM_PATH: Final[str] = "/stream.mjpg"
    DEFAULT_FRAME_COUNT: Final[int] = 20
    DEFAULT_MAXIMUM_BYTES: Final[int] = 64 * 1024 * 1024
    DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
    MAXIMUM_TIMEOUT_SECONDS: Final[float] = 300.0

    def __init__(
        self,
        recorder: RecorderService,
        storage: RecordingStorage,
        *,
        clock: Callable[[], datetime] | None = None,
        recording_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(recorder, RecorderService):
            raise TypeError("Recorder state is required.")
        if not isinstance(storage, RecordingStorage):
            raise TypeError("Configured recording storage is required.")
        self.recorder = recorder
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(UTC))
        self.recording_id_factory = recording_id_factory or (lambda: uuid.uuid4().hex)

    def capture_segment(
        self,
        *,
        source: MjpegFrameSource,
        frame_count: int = DEFAULT_FRAME_COUNT,
        maximum_bytes: int = DEFAULT_MAXIMUM_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> Recording:
        try:
            camera_id = _camera_id(source.camera_id)
        except RecordingCaptureError:
            raise
        except Exception as error:
            raise RecordingCaptureError(
                "Authenticated camera stream identity is unavailable."
            ) from error
        self._validate_capture_target(camera_id)
        if type(frame_count) is not int or not 2 <= frame_count <= 10_000:
            raise RecordingCaptureError(
                "Recording segments require between 2 and 10000 frames."
            )
        if type(maximum_bytes) is not int or maximum_bytes < 1024:
            raise RecordingCaptureError("Recording byte limit is invalid.")
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise RecordingCaptureError("Recording stream timeout is invalid.")
        try:
            timeout_seconds = float(timeout_seconds)
        except (OverflowError, ValueError) as error:
            raise RecordingCaptureError(
                "Recording stream timeout is invalid."
            ) from error
        if (
            not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= self.MAXIMUM_TIMEOUT_SECONDS
        ):
            raise RecordingCaptureError("Recording stream timeout is invalid.")

        started_at = _utc_datetime(self.clock(), "Recording start")
        try:
            frames = source.get_mjpeg_frames(
                self.STREAM_PATH,
                maximum=maximum_bytes,
                count=frame_count,
                timeout_seconds=timeout_seconds,
            )
            self._validate_capture_target(camera_id)

            def authorized_frames() -> Iterable[bytes]:
                yield from frames
                self._validate_capture_target(camera_id)

            return self.storage.publish_segment(
                recording_id=self.recording_id_factory(),
                camera_id=camera_id,
                started_at=started_at,
                completion_clock=self.clock,
                frames=authorized_frames(),
                expected_frame_count=frame_count,
                maximum_bytes=maximum_bytes,
            )
        except RecordingCaptureError:
            raise
        except (OSError, RecordingStorageError) as error:
            raise RecordingCaptureError(
                "Recording segment could not be safely published."
            ) from error
        except Exception as error:
            raise RecordingCaptureError(
                "Camera stream could not be captured."
            ) from error

    def _validate_capture_target(self, camera_id: str) -> None:
        if self.recorder.initialized_storage_identity != (
            self.storage.storage_reference,
            self.storage.volume_identity,
        ):
            raise RecordingCaptureError(
                "Configured recording storage is not ready for capture."
            )
        if camera_id not in self.recorder.recording_camera_ids:
            raise RecordingCaptureError("Camera is not authorized for this recorder.")
        try:
            self.storage.validate()
        except RecordingStorageError as error:
            raise RecordingCaptureError(
                "Configured recording storage is not ready for capture."
            ) from error


def _validate_absolute_directory(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise RecordingStorageError(
            "Recording storage must be an existing absolute directory."
        )


def _storage_reference(value: str) -> str:
    if not isinstance(value, str) or _STORAGE_REFERENCE.fullmatch(value) is None:
        raise RecordingStorageError("Recording storage reference is invalid.")
    return value


def _volume_identity(value: str) -> str:
    if not isinstance(value, str) or _VOLUME_IDENTITY.fullmatch(value) is None:
        raise RecordingStorageError("Recording volume identity is invalid.")
    return value


def _camera_id(value: str) -> str:
    if not isinstance(value, str) or _CAMERA_ID.fullmatch(value) is None:
        raise RecordingCaptureError("Camera identity is invalid.")
    return value


def _recording_id(value: str) -> str:
    if not isinstance(value, str) or _RECORDING_ID.fullmatch(value) is None:
        raise RecordingCaptureError("Recording identity is invalid.")
    return value


def _recording_reference(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise RecordingStorageError("Recording reference is invalid.")
    path = PurePosixPath(value)
    parts = path.parts
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(parts) != 6
        or parts[0] != RecordingStorage.RECORDINGS_DIRECTORY
        or _CAMERA_ID.fullmatch(parts[1]) is None
        or _YEAR.fullmatch(parts[2]) is None
        or _MONTH_DAY.fullmatch(parts[3]) is None
        or _MONTH_DAY.fullmatch(parts[4]) is None
        or _SEGMENT_DIRECTORY.fullmatch(parts[5]) is None
    ):
        raise RecordingStorageError("Recording reference is invalid.")
    return parts


def _matching_directories(
    parent: Path, pattern: re.Pattern[str], *, reverse: bool = True
) -> tuple[Path, ...]:
    try:
        entries = tuple(parent.iterdir())
    except OSError as error:
        raise RecordingStorageError("Recording storage could not be read.") from error
    selected = []
    for entry in entries:
        if pattern.fullmatch(entry.name) is None:
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise RecordingStorageError("Recording path is unsafe.")
        selected.append(entry)
    return tuple(sorted(selected, key=lambda item: item.name, reverse=reverse))


def _stored_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise RecordingStorageError("Recording timestamp is invalid.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RecordingStorageError("Recording timestamp is invalid.") from error
    if parsed.tzinfo is None:
        raise RecordingStorageError("Recording timestamp is invalid.")
    return parsed.astimezone(UTC)


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RecordingStorageError("Recording manifest value is invalid.")
    return value


def _utc_datetime(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RecordingCaptureError(f"{label} must include a timezone.")
    return value.astimezone(UTC)


def _create_private_directories(root: Path, destination: Path) -> None:
    try:
        relative = destination.relative_to(root)
    except ValueError as error:
        raise RecordingStorageError(
            "Recording path escaped configured storage."
        ) from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise RecordingStorageError("Recording storage path is unsafe.")
        created = not current.exists()
        current.mkdir(mode=0o700, exist_ok=True)
        _private_mode(current, 0o700)
        if created:
            _sync_directory(current)
            _sync_directory(current.parent)


def _atomic_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        _private_mode(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _private_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # Some platform-neutral development filesystems do not expose POSIX modes.
        pass


def _filesystem_device(path: Path) -> int:
    try:
        return os.stat(path, follow_symlinks=False).st_dev
    except OSError as error:
        raise RecordingStorageError(
            "Recording volume identity could not be checked."
        ) from error


def _sync_directory(path: Path) -> None:
    """Make directory-entry changes durable where POSIX supports it."""

    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise RecordingStorageError(
            "Recording storage metadata could not be synchronized."
        ) from error


__all__ = [
    "MjpegFrameSource",
    "RecorderCaptureService",
    "RecordingCaptureError",
    "RecordingStorage",
    "RecordingStorageError",
    "RecordingVolumeAttestation",
    "StoredRecording",
]
