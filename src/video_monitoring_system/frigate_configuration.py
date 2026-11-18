"""Generate private Frigate runtime files from OSE-owned camera state.

Frigate remains an appliance implementation detail.  Camera credentials live
only in an owner-readable environment file, while the generated YAML contains
opaque environment references and stable OSE-to-engine bindings.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .provisioning import secure_write_text


class FrigateConfigurationError(RuntimeError):
    """OSE-owned engine configuration is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class FrigateCameraSource:
    """One authenticated camera stream and its measured media profile."""

    camera_id: str
    engine_name: str
    stream_url: str = field(repr=False)
    codec: str = "mjpeg"
    frame_rate: int = 10
    recording_enabled: bool = True


@dataclass(frozen=True, slots=True)
class FrigateCameraPolicy:
    """Small engine-neutral detection policy currently projected to Frigate."""

    contour_area: int = 30
    ignore_zones: tuple[tuple[str, str], ...] = ()
    detect_fps: int = 5


@dataclass(frozen=True, slots=True)
class FrigateConfigurationResult:
    changed: bool
    cameras: int
    configuration_path: Path
    environment_path: Path


_CAMERA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
_ENGINE_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}")
_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,95}")
_ZONE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,79}")
_POLICY_FILE = "video-engine-policy.json"
_MAXIMUM_POLICY_BYTES = 128 * 1024
_PRIVATE_RELAY_HOST = "ose-camera-route-relay.internal"
_PRIVATE_RELAY_PORT = 9081


def load_camera_policies(data_directory: Path) -> dict[str, FrigateCameraPolicy]:
    """Load optional OSE detection choices without exposing Frigate YAML."""

    path = Path(data_directory) / _POLICY_FILE
    if not path.exists():
        return {}
    try:
        details = path.stat()
        if (
            path.is_symlink()
            or not path.is_file()
            or not 0 < details.st_size <= _MAXIMUM_POLICY_BYTES
            or (
                os.name == "posix"
                and (details.st_mode & 0o077 or details.st_uid != os.geteuid())
            )
        ):
            raise ValueError
        document = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "cameras"}
            or document["schema_version"] != 1
            or not isinstance(document["cameras"], dict)
            or len(document["cameras"]) > 16
        ):
            raise ValueError
        policies: dict[str, FrigateCameraPolicy] = {}
        for camera_id, value in document["cameras"].items():
            if not isinstance(camera_id, str) or _CAMERA_ID.fullmatch(camera_id) is None:
                raise ValueError
            if not isinstance(value, dict) or not {"contour_area", "ignore_zones"} <= set(value) or set(value) - {"contour_area", "ignore_zones", "detect_fps"}:
                raise ValueError
            detect_fps = value.get("detect_fps", 5)
            if type(detect_fps) is not int or not 1 <= detect_fps <= 10:
                raise ValueError
            contour_area = value["contour_area"]
            zones = value["ignore_zones"]
            if (
                isinstance(contour_area, bool)
                or not isinstance(contour_area, int)
                or not 1 <= contour_area <= 10_000
                or not isinstance(zones, list)
                or len(zones) > 16
            ):
                raise ValueError
            parsed_zones: list[tuple[str, str]] = []
            zone_names: set[str] = set()
            for zone in zones:
                if not isinstance(zone, dict) or set(zone) != {"name", "coordinates"}:
                    raise ValueError
                name = zone["name"]
                coordinates = zone["coordinates"]
                if (
                    not isinstance(name, str)
                    or _ZONE_NAME.fullmatch(name) is None
                    or not isinstance(coordinates, str)
                ):
                    raise ValueError
                if name in zone_names:
                    raise ValueError
                zone_names.add(name)
                _validate_coordinates(coordinates)
                parsed_zones.append((name, coordinates))
            policies[camera_id] = FrigateCameraPolicy(
                contour_area=contour_area,
                ignore_zones=tuple(parsed_zones),
                detect_fps=detect_fps,
            )
        return policies
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise FrigateConfigurationError(
            "Saved camera detection policy could not be read."
        ) from error


def save_camera_policies(
    data_directory: Path,
    policies: Mapping[str, FrigateCameraPolicy],
) -> Path:
    """Persist OSE detection policy separately from generated engine YAML."""

    if not isinstance(policies, Mapping) or len(policies) > 16:
        raise FrigateConfigurationError("Camera detection policy is invalid.")
    cameras: dict[str, object] = {}
    for camera_id, policy in policies.items():
        if (
            not isinstance(camera_id, str)
            or _CAMERA_ID.fullmatch(camera_id) is None
            or not isinstance(policy, FrigateCameraPolicy)
            or isinstance(policy.contour_area, bool)
            or not isinstance(policy.contour_area, int)
            or not 1 <= policy.contour_area <= 10_000
            or len(policy.ignore_zones) > 16
            or type(policy.detect_fps) is not int or not 1 <= policy.detect_fps <= 10
        ):
            raise FrigateConfigurationError("Camera detection policy is invalid.")
        zones: list[dict[str, str]] = []
        names: set[str] = set()
        for name, coordinates in policy.ignore_zones:
            try:
                if _ZONE_NAME.fullmatch(name) is None or name in names:
                    raise ValueError
                names.add(name)
                _validate_coordinates(coordinates)
            except (TypeError, ValueError) as error:
                raise FrigateConfigurationError(
                    "Camera detection policy is invalid."
                ) from error
            zones.append({"name": name, "coordinates": coordinates})
        cameras[camera_id] = {
            "contour_area": policy.contour_area,
            "ignore_zones": zones,
        }
        if policy.detect_fps != 5:
            cameras[camera_id]["detect_fps"] = policy.detect_fps
    path = Path(data_directory) / _POLICY_FILE
    try:
        secure_write_text(
            path,
            json.dumps(
                {"schema_version": 1, "cameras": cameras},
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    except OSError as error:
        raise FrigateConfigurationError(
            "Camera detection policy could not be saved."
        ) from error
    return path


def write_managed_frigate_configuration(
    state_directory: Path,
    sources: Sequence[FrigateCameraSource],
    *,
    candidates: Sequence[str],
    policies: Mapping[str, FrigateCameraPolicy] | None = None,
    retention_days: int = 7,
    validation_continuous_days: int = 0,
    mjpeg_hardware: str | None = None,
    mjpeg_software_preset: str = "superfast",
) -> FrigateConfigurationResult:
    """Convergently write Frigate YAML and its separate secret environment."""

    if (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or not 1 <= retention_days <= 3650
    ):
        raise FrigateConfigurationError("Recording retention is invalid.")
    if mjpeg_hardware not in {None, "v4l2m2m"}:
        raise FrigateConfigurationError("MJPEG hardware encoder is invalid.")
    if mjpeg_software_preset not in {"superfast", "ultrafast"}:
        raise FrigateConfigurationError("MJPEG software encoder preset is invalid.")
    if (
        type(validation_continuous_days) is not int
        or validation_continuous_days not in {0, 1}
    ):
        raise FrigateConfigurationError("Validation recording duration is invalid.")
    selected_sources = tuple(sources)
    if not 1 <= len(selected_sources) <= 16:
        raise FrigateConfigurationError("At least one video-engine camera is required.")
    state_directory = Path(state_directory)
    configuration_directory = state_directory / "config"
    try:
        for directory in (state_directory, configuration_directory):
            if directory.is_symlink():
                raise ValueError
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not directory.is_dir():
                raise ValueError
    except (OSError, ValueError) as error:
        raise FrigateConfigurationError(
            "Private video-engine directory is unsafe."
        ) from error
    selected_policies = dict(policies or {})
    camera_ids: set[str] = set()
    engine_names: set[str] = set()
    streams: dict[str, str] = {}
    ffmpeg_inputs: dict[str, str] = {}
    cameras: dict[str, object] = {}
    environment: dict[str, str] = {}
    for source in selected_sources:
        _validate_source(source)
        if source.camera_id in camera_ids or source.engine_name in engine_names:
            raise FrigateConfigurationError("Video-engine camera bindings are ambiguous.")
        camera_ids.add(source.camera_id)
        engine_names.add(source.engine_name)
        variable = _camera_environment_name(source.camera_id)
        environment[variable] = source.stream_url
        if source.codec == "h264":
            # go2rtc accepts native H.264 over authenticated HTTP or RTSP.
            # Preserve the camera-side encoding without a recorder transcode.
            streams[source.engine_name] = f"{{{variable}}}"
        else:
            input_profile = f"ose_mjpeg_{source.frame_rate}fps"
            ffmpeg_inputs[input_profile] = (
                f"-r {source.frame_rate} -fflags nobuffer -flags low_delay "
                "-i {input}"
            )
            output_profile = f"ose_h264_software_{source.frame_rate}fps"
            ffmpeg_inputs[output_profile] = (
                "-c:v libx264 -pix_fmt:v yuv420p -profile:v high -level:v 4.1 "
                f"-preset:v {mjpeg_software_preset} -tune:v zerolatency -crf:v 23 "
                f"-threads:v 2 -g {source.frame_rate * 2} -bf 0"
            )
            if mjpeg_hardware == "v4l2m2m":
                # Pi 3/4 expose H.264 encoding through V4L2 M2M. MJPEG
                # decodes to yuvj420p, while this encoder requires yuv420p.
                output_profile = "ose_h264_v4l2m2m"
                ffmpeg_inputs[output_profile] = (
                    "-pix_fmt:v yuv420p -c:v h264_v4l2m2m -g 50 -bf 0"
                )
            streams[source.engine_name] = (
                f"ffmpeg:{{{variable}}}#input={input_profile}"
                f"#video={output_profile}"
            )
        policy = selected_policies.get(source.camera_id, FrigateCameraPolicy())
        if not isinstance(policy, FrigateCameraPolicy) or type(policy.detect_fps) is not int or not 1 <= policy.detect_fps <= 10:
            raise FrigateConfigurationError("Camera detection frame rate is invalid.")
        motion: dict[str, object] = {"contour_area": policy.contour_area}
        if policy.ignore_zones:
            motion["mask"] = {
                _mask_key(name, index): {
                    "friendly_name": name,
                    "coordinates": coordinates,
                }
                for index, (name, coordinates) in enumerate(
                    policy.ignore_zones, start=1
                )
            }
        cameras[source.engine_name] = {
            "ffmpeg": {
                "inputs": [
                    {
                        "path": f"rtsp://127.0.0.1:8554/{source.engine_name}",
                        "input_args": "preset-rtsp-generic",
                        "roles": ["detect", "record"],
                    }
                ]
            },
            "detect": {"enabled": True, "width": 640, "height": 360, "fps": policy.detect_fps},
            "record": {"enabled": source.recording_enabled},
            "motion": motion,
            "objects": {"track": ["person"]},
        }
    unknown_policies = set(selected_policies) - camera_ids
    if unknown_policies:
        raise FrigateConfigurationError(
            "Camera detection policy does not match the video-engine cameras."
        )

    selected_candidates = tuple(_candidate(value) for value in candidates)
    if not selected_candidates:
        raise FrigateConfigurationError("A private WebRTC candidate is required.")
    if len(set(selected_candidates)) != len(selected_candidates):
        raise FrigateConfigurationError("Private WebRTC candidates must be unique.")
    document = {
        "mqtt": {"enabled": False},
        "detectors": {"ose_cpu": {"type": "cpu", "num_threads": 2}},
        "go2rtc": {
            "log": {"ffmpeg": "quiet"},
            "ffmpeg": ffmpeg_inputs,
            # Frigate expands ``{VARIABLE}`` placeholders in go2rtc stream
            # URLs, but passes the rest of the go2rtc document through
            # unchanged.  Candidates are routing data rather than secrets, so
            # write the validated addresses directly; otherwise go2rtc ignores
            # the literal placeholders and advertises an unrelated STUN-derived
            # public address.
            "webrtc": {"candidates": list(selected_candidates)},
            "streams": streams,
        },
        "record": {
            "enabled": True,
            "continuous": {"days": validation_continuous_days},
            "motion": {"days": retention_days},
            "alerts": {
                "pre_capture": 5,
                "post_capture": 5,
                "retain": {"days": retention_days, "mode": "all"},
            },
        },
        "review": {"alerts": {"labels": ["person"], "cutoff_time": 10}},
        "cameras": cameras,
        "version": "0.18-0",
    }
    configuration = yaml.safe_dump(
        document,
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
    )
    environment_text = "".join(
        f"{name}={_environment_value(value)}\n"
        for name, value in sorted(environment.items())
    )
    configuration_path = configuration_directory / "config.yml"
    environment_path = state_directory / "environment"
    changed = _write_if_changed(
        configuration_path, configuration, semantic_yaml=True
    )
    changed = _write_if_changed(environment_path, environment_text) or changed
    try:
        os.chmod(state_directory, 0o700)
        os.chmod(configuration_path.parent, 0o700)
    except OSError as error:
        raise FrigateConfigurationError(
            "Private video-engine files could not be protected."
        ) from error
    return FrigateConfigurationResult(
        changed=changed,
        cameras=len(selected_sources),
        configuration_path=configuration_path,
        environment_path=environment_path,
    )


def _validate_source(source: FrigateCameraSource) -> None:
    if not isinstance(source, FrigateCameraSource):
        raise FrigateConfigurationError("Video-engine camera source is invalid.")
    if not isinstance(source.recording_enabled, bool):
        raise FrigateConfigurationError("Video-engine recording policy is invalid.")
    if _CAMERA_ID.fullmatch(source.camera_id) is None:
        raise FrigateConfigurationError("Video-engine camera identity is invalid.")
    if _ENGINE_NAME.fullmatch(source.engine_name) is None:
        raise FrigateConfigurationError("Video-engine camera binding is invalid.")
    if (
        not isinstance(source.frame_rate, int)
        or isinstance(source.frame_rate, bool)
        or not 1 <= source.frame_rate <= 60
    ):
        raise FrigateConfigurationError("Video-engine camera frame rate is invalid.")
    if source.codec not in {"h264", "mjpeg"}:
        raise FrigateConfigurationError("Video-engine camera codec is invalid.")
    if not isinstance(source.stream_url, str) or any(
        character in source.stream_url for character in "\x00\r\n"
    ):
        raise FrigateConfigurationError("Private camera stream is invalid.")
    try:
        parsed = urlsplit(source.stream_url)
        parsed_port = parsed.port
    except ValueError as error:
        raise FrigateConfigurationError("Private camera stream is invalid.") from error
    private_relay = (
        parsed.scheme == "http"
        and parsed.hostname == _PRIVATE_RELAY_HOST
        and parsed_port == _PRIVATE_RELAY_PORT
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(r"CAM-[0-9A-F]{16,64}", source.camera_id) is not None
        and parsed.path
        == f"/v1/cameras/{source.camera_id}/active/main"
    )
    authenticated_camera = (
        parsed.scheme == "http"
        and bool(parsed.hostname)
        and parsed.username is not None
        and parsed.password is not None
        and parsed.path
        == ("/stream.h264" if source.codec == "h264" else "/stream.mjpg")
        and not parsed.query
        and not parsed.fragment
    )
    authenticated_rtsp = (
        parsed.scheme == "rtsp"
        and source.codec == "h264"
        and bool(parsed.hostname)
        and bool(parsed.username)
        and bool(parsed.password)
        and parsed.path.startswith("/")
        and len(parsed.path) > 1
        and not parsed.query
        and not parsed.fragment
    )
    if (
        len(source.stream_url) > 4096
        or not (private_relay or authenticated_camera or authenticated_rtsp)
    ):
        raise FrigateConfigurationError("Private camera stream is invalid.")
    if private_relay:
        return
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise FrigateConfigurationError(
            "Private camera stream must use a resolved address."
        ) from error
    if (
        address.is_global
        or address.is_loopback
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise FrigateConfigurationError("Private camera stream is invalid.")


def _camera_environment_name(camera_id: str) -> str:
    suffix = hashlib.sha256(camera_id.encode("utf-8")).hexdigest()[:16].upper()
    return f"FRIGATE_OSE_CAMERA_{suffix}_URL"


def _candidate(value: str) -> str:
    if not isinstance(value, str) or any(character in value for character in "\x00\r\n"):
        raise FrigateConfigurationError("Private WebRTC candidate is invalid.")
    host, separator, raw_port = value.rpartition(":")
    if not separator or raw_port != "8555":
        raise FrigateConfigurationError("Private WebRTC candidate is invalid.")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError as error:
        raise FrigateConfigurationError("Private WebRTC candidate is invalid.") from error
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise FrigateConfigurationError("Private WebRTC candidate is invalid.")
    formatted = f"[{address}]" if address.version == 6 else str(address)
    return f"{formatted}:8555"


def _validate_coordinates(value: str) -> None:
    parts = value.split(",")
    if len(parts) < 6 or len(parts) % 2:
        raise ValueError
    for part in parts:
        coordinate = float(part)
        if not math.isfinite(coordinate) or not 0 <= coordinate <= 1:
            raise ValueError


def _mask_key(name: str, index: int) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return (key or f"ignore_zone_{index}")[:80]


def _environment_value(value: str) -> str:
    if not isinstance(value, str) or any(character in value for character in "\x00\r\n"):
        raise FrigateConfigurationError("Private video-engine environment is invalid.")
    return value


def _write_if_changed(
    path: Path, content: str, *, semantic_yaml: bool = False
) -> bool:
    try:
        if path.is_file() and not path.is_symlink():
            existing = path.read_text(encoding="utf-8")
            equivalent = existing == content
            if semantic_yaml and not equivalent:
                try:
                    equivalent = yaml.safe_load(existing) == yaml.safe_load(content)
                except yaml.YAMLError:
                    equivalent = False
            if equivalent:
                if os.name == "posix" and path.stat().st_mode & 0o077:
                    os.chmod(path, 0o600)
                return False
        secure_write_text(path, content)
        return True
    except (OSError, UnicodeError) as error:
        raise FrigateConfigurationError(
            "Private video-engine files could not be written."
        ) from error


__all__ = [
    "FrigateCameraPolicy",
    "FrigateCameraSource",
    "FrigateConfigurationError",
    "FrigateConfigurationResult",
    "load_camera_policies",
    "save_camera_policies",
    "write_managed_frigate_configuration",
]
