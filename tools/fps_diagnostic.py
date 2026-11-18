#!/usr/bin/env python3
"""Bounded, read-only Frigate and saved-video FPS diagnostic."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Callable, Mapping
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_CAMERA_METRICS = {
    "camera_fps": "input_fps",
    "process_fps": "processed_fps",
    "detection_fps": "ai_checks_fps",
    "skipped_fps": "skipped_fps",
    "expected_fps": "expected_fps",
    "stalls_last_hour": "stalls_last_hour",
    "reconnects_last_hour": "reconnects_last_hour",
    "ffmpeg_cpu": "ffmpeg_cpu_percent",
}
_THERMAL_TYPES = {"cpu-thermal", "cpu_thermal", "bcm2835_thermal", "x86_pkg_temp", "soc_thermal"}


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("use an HTTP(S) Frigate stats URL without credentials or query data")
    return value


def validate_rtsp_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"rtsp", "rtsps"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("use an RTSP(S) source URL without credentials or query data")
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value.strip().removesuffix("%") if isinstance(value, str) else value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def normalize_stats(payload: Mapping[str, object]) -> dict[str, object]:
    """Keep only stable rate/latency fields; discard URLs and arbitrary payload text."""
    raw_cameras = payload.get("cameras")
    if not isinstance(raw_cameras, Mapping):
        raise ValueError("Frigate stats has no camera metrics")
    cameras: dict[str, dict[str, float]] = {}
    for index, (raw_name, raw_metrics) in enumerate(sorted(raw_cameras.items(), key=lambda item: str(item[0])), 1):
        name = str(raw_name)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
            name = f"camera-{index}"
        if not isinstance(raw_metrics, Mapping):
            continue
        metrics = {}
        for source, target in _CAMERA_METRICS.items():
            value = _number(raw_metrics.get(source))
            if value is not None:
                metrics[target] = value
        cameras[name] = metrics

    raw_detectors = payload.get("detectors")
    detectors: dict[str, float] = {}
    if isinstance(raw_detectors, Mapping):
        for index, (raw_name, raw_value) in enumerate(sorted(raw_detectors.items(), key=lambda item: str(item[0])), 1):
            name = str(raw_name)
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
                name = f"detector-{index}"
            if isinstance(raw_value, Mapping):
                value = _number(raw_value.get("inference_speed"))
                if value is not None:
                    detectors[name] = value

    cpu_usages = payload.get("cpu_usages")
    full_system = cpu_usages.get("frigate.full_system") if isinstance(cpu_usages, Mapping) else None
    cpu = _number(full_system.get("cpu")) if isinstance(full_system, Mapping) else None
    return {"cameras": cameras, "detectors_inference_ms": detectors, "frigate_cpu_percent": cpu}


def _read_cpu_ticks(root: Path) -> tuple[int, int] | None:
    try:
        first = (root / "proc/stat").read_text(encoding="ascii").splitlines()[0].split()
        if first[0] != "cpu":
            return None
        values = [int(item) for item in first[1:]]
        if len(values) < 4:
            return None
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
    except (OSError, ValueError, IndexError):
        return None


def cpu_busy_percent(previous: tuple[int, int] | None, current: tuple[int, int] | None) -> float | None:
    if previous is None or current is None:
        return None
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    if total <= 0 or idle < 0 or idle > total:
        return None
    return round(100 * (total - idle) / total, 2)


def _host_metrics(root: Path) -> dict[str, float | None]:
    load = temperature = None
    try:
        load = _number((root / "proc/loadavg").read_text(encoding="ascii").split()[0])
    except (OSError, IndexError):
        pass
    zones = root / "sys/class/thermal"
    try:
        for zone in sorted(zones.glob("thermal_zone*")):
            if (zone / "type").read_text(encoding="ascii").strip() in _THERMAL_TYPES:
                raw = _number((zone / "temp").read_text(encoding="ascii").strip())
                if raw is not None and raw <= 150000:
                    temperature = round(raw / 1000, 2)
                break
    except OSError:
        pass
    return {"load_1m": load, "temperature_c": temperature}


def _fetch_stats(url: str, timeout: float = 3.0) -> Mapping[str, object]:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, Mapping):
        raise ValueError("Frigate returned an invalid stats object")
    return payload


def _summary(values: list[object]) -> dict[str, float | int] | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    if not numbers:
        return None
    return {
        "min": round(min(numbers), 2),
        "mean": round(sum(numbers) / len(numbers), 2),
        "max": round(max(numbers), 2),
        "n": len(numbers),
    }


def _summarize(samples: list[dict[str, object]]) -> dict[str, object]:
    camera_names = sorted({name for sample in samples for name in sample.get("cameras", {})})
    metric_names = sorted({metric for sample in samples for camera in sample.get("cameras", {}).values() for metric in camera})
    cameras = {
        name: {
            metric: _summary([
                sample.get("cameras", {}).get(name, {}).get(metric)
                for sample in samples
            ])
            for metric in metric_names
        }
        for name in camera_names
    }
    detector_names = sorted({name for sample in samples for name in sample.get("detectors_inference_ms", {})})
    detectors = {
        name: _summary([sample.get("detectors_inference_ms", {}).get(name) for sample in samples])
        for name in detector_names
    }
    return {
        "camera_metrics": cameras,
        "detector_inference_ms": detectors,
        "frigate_cpu_percent": _summary([sample.get("frigate_cpu_percent") for sample in samples]),
    }


def probe_recording(path: Path, ffprobe: str | None) -> dict[str, object]:
    """Count decoded frames in one saved video; never return the local path."""
    file_name = path.name if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", path.name) else "recording"
    camera_name = path.parent.name
    camera_name = camera_name if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", camera_name) else None
    result: dict[str, object] = {"camera": camera_name, "file": file_name, "status": "unavailable"}
    if not ffprobe or not path.is_file():
        return result
    args = [
        ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,nb_read_frames",
        "-show_entries", "format=duration", "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode != 0:
            return result
        payload = json.loads(completed.stdout)
        stream = payload.get("streams", [])[0]
        frames = int(stream["nb_read_frames"])
        duration = float(payload["format"]["duration"])
        if frames < 1 or not math.isfinite(duration) or duration <= 0:
            return result
        result.update({
            "status": "measured",
            "codec": stream.get("codec_name"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "frames": frames,
            "duration_s": round(duration, 3),
            "fps": round(frames / duration, 2),
        })
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError, TypeError):
        pass
    return result


def probe_source_stream(camera: str, url: str, ffprobe: str | None) -> dict[str, object]:
    """Measure packet timestamps on one recorder-ingress RTSP stream for 8 seconds."""
    camera = camera if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", camera) else "camera"
    result: dict[str, object] = {"camera": camera, "status": "unavailable"}
    try:
        validate_rtsp_url(url)
    except ValueError:
        return result
    if not ffprobe:
        return result
    args = [
        ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-read_intervals", "%+8",
        "-select_streams", "v:0", "-show_packets", "-show_entries", "packet=pts_time",
        "-of", "json", url,
    ]
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
        if completed.returncode != 0:
            return result
        payload = json.loads(completed.stdout)
        timestamps = sorted({
            float(packet["pts_time"])
            for packet in payload.get("packets", [])
            if isinstance(packet, Mapping) and _number(packet.get("pts_time")) is not None
        })
        span = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0
        if span <= 0 or not math.isfinite(span):
            return result
        result.update({
            "status": "measured",
            "frames": len(timestamps),
            "stream_span_s": round(span, 3),
            "fps": round((len(timestamps) - 1) / span, 2),
        })
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, IndexError):
        pass
    return result


def capture(
    url: str,
    *,
    duration_s: int = 60,
    interval_s: int = 5,
    source_streams: tuple[tuple[str, str], ...] = (),
    recordings: tuple[Path, ...] = (),
    ffprobe: str | None = None,
    stats_reader: Callable[[str], Mapping[str, object]] = _fetch_stats,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    root: Path = Path("/"),
) -> dict[str, object]:
    validate_url(url)
    if type(duration_s) is not int or not 1 <= duration_s <= 3600:
        raise ValueError("duration must be 1-3600 seconds")
    if type(interval_s) is not int or not 1 <= interval_s <= 60:
        raise ValueError("interval must be 1-60 seconds")

    offsets = [index * interval_s for index in range(duration_s // interval_s + 1)]
    if offsets[-1] < duration_s:
        offsets.append(duration_s)
    if len(offsets) > 721:
        raise ValueError("duration and interval would exceed 721 samples")
    if len(recordings) > 16:
        raise ValueError("at most 16 saved clips may be probed per run")
    if len(source_streams) > 16:
        raise ValueError("at most 16 source streams may be probed per run")
    started_at = datetime.now(UTC)
    started = monotonic()
    previous_cpu = _read_cpu_ticks(root)
    normalized_samples: list[dict[str, object]] = []
    host_samples: list[dict[str, object]] = []
    failures = 0
    for index, offset in enumerate(offsets):
        delay = started + offset - monotonic()
        if delay > 0:
            sleeper(delay)
        host = _host_metrics(root)
        current_cpu = _read_cpu_ticks(root)
        busy = cpu_busy_percent(previous_cpu, current_cpu) if index else None
        previous_cpu = current_cpu
        host["cpu_busy_percent"] = busy
        host_samples.append(host)
        try:
            normalized_samples.append(normalize_stats(stats_reader(url)))
        except Exception:
            failures += 1

    ended_at = datetime.now(UTC)
    actual_duration = max(0.0, monotonic() - started)
    source_result = [probe_source_stream(name, source_url, ffprobe) for name, source_url in source_streams]
    recordings_result = [probe_recording(path, ffprobe) for path in recordings]
    successful = len(normalized_samples)
    return {
        "schema_version": 1,
        "capture": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_s": round(actual_duration, 1),
            "requested_duration_s": duration_s,
            "interval_s": interval_s,
            "frigate_samples_ok": successful,
            "frigate_samples_failed": failures,
        },
        "host": {
            "cpu_busy_percent": _summary([sample.get("cpu_busy_percent") for sample in host_samples]),
            "cpu_headroom_percent": _summary([
                100 - value for sample in host_samples
                if (value := _number(sample.get("cpu_busy_percent"))) is not None
            ]),
            "load_1m": _summary([sample.get("load_1m") for sample in host_samples]),
            "temperature_c": _summary([sample.get("temperature_c") for sample in host_samples]),
        },
        "frigate": _summarize(normalized_samples),
        "source_video": source_result,
        "saved_video": recordings_result,
        "not_measured": {
            "camera_sensor_fps": "The camera sensor's output before the network handoff is not measured by recorder-side RTSP probes.",
            "browser_view_fps": "Measured in the viewing browser, not available from recorder-side sampling.",
        },
        "source_measurement_scope": (
            "RTSP packet timestamps at the recorder ingress."
            if any(item["status"] == "measured" for item in source_result)
            else "No recorder-ingress RTSP streams were supplied."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frigate-url", default="http://127.0.0.1:5000/api/stats")
    parser.add_argument("--duration", type=int, default=60, help="sample duration in seconds (1-3600)")
    parser.add_argument("--interval", type=int, default=5, help="sample interval in seconds (1-60)")
    parser.add_argument("--source-stream", action="append", default=[], metavar="CAMERA=RTSP_URL", help="recorder-ingress RTSP stream to measure; may be repeated")
    parser.add_argument("--recording", type=Path, action="append", default=[], help="saved video to measure; may be repeated")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe"), help="ffprobe executable for saved-video measurements")
    args = parser.parse_args(argv)
    try:
        sources = []
        for value in args.source_stream:
            name, separator, source_url = value.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
                raise ValueError("source stream must be CAMERA=RTSP_URL")
            sources.append((name, validate_rtsp_url(source_url)))
        report = capture(
            args.frigate_url,
            duration_s=args.duration,
            interval_s=args.interval,
            source_streams=tuple(sources),
            recordings=tuple(args.recording),
            ffprobe=args.ffprobe,
        )
    except (OSError, ValueError, URLError):
        parser.error("could not collect the bounded Frigate diagnostic")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
