"""Bounded, passive health history independent of camera/recorder hardware.

Camera adapters expose the existing get_json/get_bytes interface. This worker
does not enroll devices, restart services or alter recording policy.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import hashlib
from functools import lru_cache
import math
import os
from pathlib import Path
import re
import subprocess
import time

from .device.runtime_metrics import power_flags, read_runtime_metrics
from .provisioning import secure_write_text


@dataclass(frozen=True)
class MonitorLimits:
    interval_s: int = 60
    temperature_c: float = 75
    load_per_core: float = 1
    samples: int = 120
    events: int = 256

    def __post_init__(self):
        for name in ("interval_s", "samples", "events"):
            if type(getattr(self, name)) is not int:
                raise ValueError("monitor bounds must be integers")
        for name in ("temperature_c", "load_per_core"):
            if _number(getattr(self, name)) is None:
                raise ValueError("monitor thresholds must be finite numbers")
        if not 15 <= self.interval_s <= 3600:
            raise ValueError("monitor interval must be 15-3600 seconds")
        if not 30 <= self.temperature_c <= 100 or not .1 <= self.load_per_core <= 8:
            raise ValueError("monitor thresholds are invalid")
        if not 2 <= self.samples <= 1440 or not 2 <= self.events <= 1024:
            raise ValueError("monitor history bounds are invalid")


def _number(value, minimum=0, maximum=1e12):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return round(value, 2) if minimum <= value <= maximum else None


def camera_observation(source, expected_id: str, slot: int, *, clock=time.monotonic):
    result = {
        "slot": slot, "state": "unavailable", "identity_ok": None,
        "camera_ref": hashlib.sha256(expected_id.encode()).hexdigest()[:16],
        "snapshot_ok": False, "response_ms": None, "health_schema": None,
        "sensor_ready": None, "network_state": "unknown",
        "capture_state": "unknown",
        "network": {},
        "source_fps": None, "source_width": None, "source_height": None, "source_bitrate": None,
        "power": power_flags(None), "runtime": {}, "codec": "unknown",
    }
    if source is None:
        return result
    started = clock()
    try:
        health = source.get_json("/health")
        if health.get("device_id") != expected_id:
            result.update(state="identity-mismatch", identity_ok=False)
            return result
        result["identity_ok"] = True
        result["health_schema"] = _number(health.get("schema_version"), 1, 100)
        result["sensor_ready"] = health.get("camera_ready") if type(health.get("camera_ready")) is bool else None
        if health.get("capture_state") in {"starting", "ready", "stalled", "failed", "unavailable"}:
            result["capture_state"] = health["capture_state"]
        result["power"] = power_flags(health.get("power"))
        network = health.get("network", {})
        state = network.get("state") if isinstance(network, dict) else None
        if state in {"healthy", "degraded", "recovering"}:
            result["network_state"] = state
        if isinstance(network, dict):
            for key, minimum, maximum in (("rssi_dbm", -150, 0), ("signal_percent", 0, 100)):
                result["network"][key] = _number(network.get(key), minimum, maximum)
            current = network.get("current_boot", {})
            if isinstance(current, dict):
                for key in ("consecutive_failures", "reconnects", "observations"):
                    result["network"][key] = _number(current.get(key), maximum=1e9)
        runtime = health.get("runtime", {})
        if isinstance(runtime, dict):
            for key, maximum in (("uptime_s", 1e12), ("temperature_c", 150), ("load_per_core", 1000)):
                result["runtime"][key] = _number(runtime.get(key), maximum=maximum)
            for key in ("boot", "agent_revision"):
                value = runtime.get(key)
                result["runtime"][key] = value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value) else None
        media = health.get("media", {})
        if isinstance(media, dict) and isinstance(media.get("live"), dict):
            for source_key, target_key, maximum in (("fps", "source_fps", 240), ("width", "source_width", 16384), ("height", "source_height", 16384), ("bitrate", "source_bitrate", 1e9)):
                result[target_key] = _number(media["live"].get(source_key), maximum=maximum)
            codec = media["live"].get("codec")
            if codec in {"h264", "h265", "mjpeg"}:
                result["codec"] = codec
        elif health.get("schema_version") in {2, 3}:
            result["codec"] = "mjpeg"  # Defined legacy VMS camera contract.
        frame = source.get_bytes("/snapshot.jpg", maximum=2 * 1024 * 1024)
        valid = isinstance(frame, bytes) and 4 <= len(frame) <= 2 * 1024 * 1024 and frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")
        result.update(snapshot_ok=valid, state="online" if valid else "bad-image")
        if valid and result["sensor_ready"] is False:
            result["state"] = "capture-unavailable"
    except Exception:
        # Exception strings can contain addresses, headers or credentials.
        result["state"] = "unavailable"
    finally:
        result["response_ms"] = round((clock() - started) * 1000, 1)
    return result


@lru_cache(maxsize=1)
def _software_revision():
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def collect_sample(application, *, clock=time.time):
    try:
        power = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        power = None
    runtime = read_runtime_metrics(power=power)
    runtime["software_revision"] = _software_revision()
    roster = [c for c in application.recorder.state["cameras"] if not c.get("revoked")]
    try:
        sources = {s.camera_id: s for s in application._recording_source_provider()}
    except Exception:
        sources = {}
    bindings = getattr(application.video_engine, "camera_bindings", {})
    def probe(item):
        slot, camera = item
        result = camera_observation(sources.get(camera["id"]), camera["id"], slot)
        name = camera.get("name")
        result["camera_name"] = name if isinstance(name, str) and len(name) <= 120 and all(ord(c) >= 32 for c in name) else f"Camera {slot}"
        result["engine_managed"] = camera["id"] in bindings
        result["recording_enabled"] = camera.get("recording_enabled") is not False
        result["recording_age_s"] = None
        result["recording_state"] = "not-checked"
        result["engine_metrics"] = {}
        if result["engine_managed"]:
            try:
                metrics = application.video_engine.live_metrics(camera_id=camera["id"])
                for key in ("detect_input_fps", "processed_fps", "object_detection_fps", "skipped_fps", "detector_inference_ms", "expected_fps", "source_stalls_last_hour", "source_reconnects_last_hour", "recorder_cpu_percent", "camera_ffmpeg_cpu_percent"):
                    result["engine_metrics"][key] = _number(metrics.get(key), maximum=1e6)
            except Exception:
                pass
            try:
                coverage = application.camera_recording_coverage(camera_id=camera["id"])
                if coverage.get("state") in {"indexed", "none", "unverified", "unavailable"}:
                    result["recording_state"] = coverage["state"]
                latest = _number(coverage.get("latest_segment_end"))
                if latest is not None:
                    result["recording_age_s"] = round(max(0, clock() - latest), 1)
            except Exception:
                result["recording_state"] = "unavailable"
        return result
    with ThreadPoolExecutor(max_workers=4) as pool:
        cameras = list(pool.map(probe, enumerate(roster[:16], 1)))
    storage = {"state": "unknown"}
    try:
        status = application.recording_storage_status()
        storage["state"] = status.get("state") if status.get("state") in {"ready", "planned", "waiting", "unavailable"} else "unknown"
        for key in ("total_bytes", "used_bytes", "free_bytes", "available_bytes", "reserved_bytes"):
            storage[key] = _number(status.get(key), maximum=1e18)
    except Exception:
        storage["state"] = "unavailable"
    return {
        "checked_at": datetime.fromtimestamp(clock(), UTC).isoformat(),
        "recorder": runtime,
        "storage": storage,
        "cameras": cameras,
    }


def warnings(sample, limits):
    result = []
    runtime = sample["recorder"]
    if sample.get("storage", {}).get("state") == "unavailable":
        result.append("recording-storage-unavailable")
    if sample.get("storage", {}).get("available_bytes") == 0:
        result.append("recording-storage-reserve-reached")
    if runtime.get("temperature_c") is not None and runtime["temperature_c"] >= limits.temperature_c:
        result.append("recorder-hot")
    if runtime.get("load_per_core") is not None and runtime["load_per_core"] >= limits.load_per_core:
        result.append("recorder-busy")
    power = runtime.get("power", {})
    if power.get("low_voltage_now") or power.get("low_voltage_seen"):
        result.append("recorder-low-voltage-recorded")
    if power.get("throttled_now") or power.get("throttled_seen"):
        result.append("recorder-throttling-recorded")
    for camera in sample["cameras"]:
        prefix = f"camera-{camera['slot']}-"
        if camera["state"] != "online":
            result.append(prefix + camera["state"])
        if camera.get("network_state") in {"degraded", "recovering"}:
            result.append(prefix + "network-" + camera["network_state"])
        if camera.get("engine_metrics", {}).get("skipped_fps", 0):
            result.append(prefix + "detector-skipping")
        if camera.get("engine_managed") and camera.get("engine_metrics", {}).get("detect_input_fps") == 0:
            result.append(prefix + "detect-input-stopped")
        if not camera.get("engine_managed"):
            result.append(prefix + "not-engine-managed")
        if camera.get("recording_enabled") is not False and camera.get("recording_state") in {"unavailable", "unverified"}:
            result.append(prefix + "recording-unavailable")
        age = camera.get("recording_age_s")
        if camera.get("recording_enabled") is not False and (camera.get("recording_state") == "none" or age is not None and age > max(120, limits.interval_s * 2)):
            result.append(prefix + "no-recent-retained-index")
        if camera["power"].get("low_voltage_now") or camera["power"].get("low_voltage_seen"):
            result.append(prefix + "low-voltage-recorded")
    return result


class HealthHistory:
    """Keep a fixed window and state changes across process/recorder restarts."""

    def __init__(self, path: Path, limits=MonitorLimits()):
        self.path, self.limits = path, limits

    @staticmethod
    def signature(sample):
        if not isinstance(sample, dict) or not isinstance(sample.get("recorder"), dict) or not isinstance(sample.get("cameras"), list):
            raise ValueError("invalid health sample")
        camera_boots = []
        for camera in sample["cameras"]:
            if not isinstance(camera, dict) or not isinstance(camera.get("runtime"), dict):
                raise ValueError("invalid camera health")
            camera_boots.append(camera["runtime"].get("boot"))
        if not isinstance(sample.get("warnings"), list):
            raise ValueError("invalid health warnings")
        return [sample["recorder"].get("boot"), sample["warnings"], camera_boots, [c.get("camera_ref") for c in sample["cameras"]], sample["recorder"].get("software_revision")]

    def append(self, sample):
        if self.path.is_symlink():
            raise ValueError("health history is unsafe")
        state = {"schema_version": 1, "samples": [], "events": []}
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("health history is unsafe")
            loaded = json.loads(self.path.read_text())
            if loaded.get("schema_version") == 1 and isinstance(loaded.get("samples"), list) and isinstance(loaded.get("events"), list):
                for prior in loaded["samples"]:
                    self.signature(prior)
                state = loaded
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        previous = state["samples"][-1] if state["samples"] else None
        sample["warnings"] = warnings(sample, self.limits)
        signature = self.signature(sample)
        old_signature = self.signature(previous) if previous else None
        if signature != old_signature:
            state["events"].append({"checked_at": sample["checked_at"], "boot": sample["recorder"].get("boot"), "camera_boots": signature[2], "camera_refs": signature[3], "camera_names": [c.get("camera_name", f"Camera {c['slot']}") for c in sample["cameras"]], "warnings": sample["warnings"]})
        state["samples"] = (state["samples"] + [sample])[-self.limits.samples:]
        state["events"] = state["events"][-self.limits.events:]
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("health history is unsafe")
        # One central file: valid JSON, with readable keys and indentation.
        encoded = json.dumps(state, indent=2) + "\n"
        while len(encoded.encode()) > 8 * 1024 * 1024:
            if len(state["samples"]) > 1:
                state["samples"] = state["samples"][max(1, len(state["samples"]) // 4):]
            elif len(state["events"]) > 1:
                state["events"] = state["events"][max(1, len(state["events"]) // 4):]
            else:
                raise ValueError("health sample is too large")
            encoded = json.dumps(state, indent=2) + "\n"
        secure_write_text(self.path, encoded)
        return signature != old_signature


def read_history(path: Path):
    """Read the shared support snapshot without exporting unrecognized fields."""
    if not path.exists():
        return {"schema_version": 1, "samples": [], "events": []}
    details = path.stat()
    if path.is_symlink() or details.st_size > 8 * 1024 * 1024:
        raise ValueError("health history is unsafe")
    if os.name == "posix" and (details.st_uid != os.geteuid() or details.st_mode & 0o077):
        raise ValueError("health history is private")
    document = json.loads(path.read_text())
    keys = {
        "schema_version", "samples", "events", "checked_at", "recorder", "storage", "cameras", "warnings", "camera_boots",
        "camera_ref", "camera_refs", "camera_name", "camera_names", "software_revision",
        "boot", "uptime_s", "temperature_c", "load_per_core", "agent_revision", "power",
        "slot", "state", "identity_ok", "snapshot_ok", "response_ms", "health_schema", "sensor_ready",
        "network_state", "capture_state", "network", "runtime", "codec", "engine_managed", "recording_enabled",
        "recording_age_s", "recording_state", "engine_metrics", "rssi_dbm", "signal_percent",
        "consecutive_failures", "reconnects", "observations", "detect_input_fps", "skipped_fps",
        "source_stalls_last_hour", "source_reconnects_last_hour", "recorder_cpu_percent", "camera_ffmpeg_cpu_percent",
        "processed_fps", "object_detection_fps", "detector_inference_ms", "expected_fps",
        "source_fps", "source_width", "source_height", "source_bitrate",
        "total_bytes", "used_bytes", "free_bytes", "available_bytes", "reserved_bytes",
        *power_flags(None),
    }
    states = {
        "unknown", "unavailable", "identity-mismatch", "online", "bad-image", "capture-unavailable",
        "healthy", "degraded", "recovering", "starting", "ready", "stalled", "failed", "not-started",
        "h264", "h265", "mjpeg", "not-checked", "indexed", "none", "unverified", "planned", "waiting",
    }
    warning_pattern = re.compile(
        r"(?:recorder-(?:hot|busy|low-voltage-recorded|throttling-recorded)|recording-storage-(?:unavailable|reserve-reached)|"
        r"camera-(?:[1-9]|1[0-6])-(?:unavailable|identity-mismatch|bad-image|capture-unavailable|not-engine-managed|"
        r"network-(?:degraded|recovering)|detector-skipping|detect-input-stopped|recording-unavailable|no-recent-retained-index|low-voltage-recorded))"
    )
    def validate(value, key="", depth=0):
        if depth > 10:
            raise ValueError("health history nesting is invalid")
        if isinstance(value, dict):
            if set(value) - keys:
                raise ValueError("health history contains unrecognized fields")
            for name, item in value.items():
                validate(item, name, depth + 1)
        elif isinstance(value, list):
            if len(value) > 1440:
                raise ValueError("health history is too long")
            for item in value:
                validate(item, key, depth + 1)
        elif isinstance(value, str):
            if key == "checked_at":
                if len(value) > 64 or datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError("health timestamp is invalid")
            elif key in {"camera_name", "camera_names"}:
                if len(value) > 120 or any(ord(c) < 32 for c in value):
                    raise ValueError("camera name is invalid")
            elif key in {"boot", "agent_revision", "camera_boots", "camera_ref", "camera_refs", "software_revision"}:
                if not re.fullmatch(r"[0-9a-f]{16}", value):
                    raise ValueError("health revision is invalid")
            elif key == "warnings":
                if not warning_pattern.fullmatch(value):
                    raise ValueError("health warning is invalid")
            elif value not in states:
                raise ValueError("health history contains unrecognized text")
        elif value is not None and type(value) is not bool and _number(value, -150, 1e18) is None:
            raise ValueError("health measurement is invalid")
    validate(document)
    if not isinstance(document, dict) or document.get("schema_version") != 1 or not isinstance(document.get("samples"), list) or not isinstance(document.get("events"), list):
        raise ValueError("health history is invalid")
    return document


def main(argv=None):
    import fcntl  # Linux worker only; report readers remain portable.
    parser = argparse.ArgumentParser(description="Keep bounded camera/recorder health evidence")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text()) if args.config else {}
        limits = MonitorLimits(**config)
        directory = args.data_dir / "monitoring"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            from .application import VMSApplication
            history = HealthHistory(directory / "health-history.json", limits)
            while True:
                # Re-read owner policy/routes without changing recorder state.
                sample = collect_sample(VMSApplication(args.data_dir))
                if history.append(sample):
                    print(json.dumps({"checked_at": sample["checked_at"], "warnings": sample["warnings"]}), flush=True)
                if args.once:
                    return 0
                time.sleep(limits.interval_s)
    except (OSError, ValueError, TypeError):
        print("Health monitor could not read or save its private state.", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
