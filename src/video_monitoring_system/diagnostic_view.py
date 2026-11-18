"""Human view of the same bounded JSON history offered to support tools."""

from datetime import UTC, datetime
from html import escape
import re


_LABELS = {
    "uptime_s": "Uptime (seconds)", "temperature_c": "CPU temperature (°C)",
    "load_per_core": "CPU work per core", "agent_revision": "Software revision",
    "boot": "Boot reference", "response_ms": "Health + image time (ms)",
    "camera_ref": "Camera reference", "software_revision": "Installed software reference",
    "rssi_dbm": "Wi-Fi signal (dBm)", "signal_percent": "Wi-Fi signal (%)",
    "source_fps": "Camera video target (frames/second)", "source_width": "Video width (pixels)",
    "source_height": "Video height (pixels)", "source_bitrate": "Video bitrate limit (bits/second)",
    "detect_input_fps": "Frames entering detection (per second)", "processed_fps": "Frames processed by Frigate (per second)",
    "object_detection_fps": "AI checks run (per second)", "skipped_fps": "Frames Frigate skipped (per second)",
    "detector_inference_ms": "Object check time (ms)", "expected_fps": "Detection target frames/second",
    "recording_age_s": "Age of latest indexed video segment (seconds)",
    "snapshot_ok": "Image received", "identity_ok": "Correct camera verified",
    "sensor_ready": "Image sensor ready", "engine_managed": "Recording engine connected",
    "low_voltage_now": "Low voltage now", "low_voltage_seen": "Low voltage since boot",
    "throttled_now": "Slowed down now", "throttled_seen": "Slowed down since boot",
    "frequency_capped_now": "CPU speed limited now", "frequency_capped_seen": "CPU speed limited since boot",
    "temperature_limit_now": "Heat limit now", "temperature_limit_seen": "Heat limit since boot",
    "camera_ffmpeg_cpu_percent": "Camera video CPU (%)", "recorder_cpu_percent": "Recorder CPU (%)",
}


def _value(value):
    if value is None:
        return "Unknown"
    if type(value) is bool:
        return "Yes" if value else "No"
    return str(value)


def _table(values):
    rows = []
    for key, value in values.items():
        if isinstance(value, dict):
            rows.append(f'<tr><th colspan="2">{escape(key.replace("_", " ").title())}</th></tr>')
            rows.append('<tr><td colspan="2">' + _table(value) + '</td></tr>')
        elif not isinstance(value, list):
            label = _LABELS.get(key, key.replace("_", " ").capitalize())
            if key.endswith("_bytes") and type(value) in (int, float):
                label = key.removesuffix("_bytes").replace("_", " ").capitalize() + " (GiB)"
                value = round(value / 1024 ** 3, 2)
            rows.append(f'<tr><th scope="row">{escape(label)}</th><td>{escape(_value(value))}</td></tr>')
    return '<table class="system-log-table"><tbody>' + ''.join(rows) + '</tbody></table>'


def _warning(code, names):
    match = re.fullmatch(r"camera-([0-9]+)-(.+)", code)
    prefix = "Recorder: "
    suffix = code.removeprefix("recorder-")
    if match:
        slot = int(match[1])
        prefix, suffix = names.get(slot, f"Camera {slot}") + ": ", match[2]
    meanings = {
        "hot": "CPU temperature is high", "busy": "CPU has queued work",
        "low-voltage-recorded": "low voltage happened since boot",
        "throttling-recorded": "CPU slowdown happened since boot",
        "unavailable": "camera did not answer", "not-engine-managed": "recording engine is not connected",
        "detector-skipping": "Frigate skipped frames for processing; check saved video separately",
        "detect-input-stopped": "no frames are reaching detection",
        "no-recent-retained-index": "no recent video segment is indexed; playback is not checked",
    }
    return prefix + meanings.get(suffix, suffix.replace("-", " "))


def render_system_log(history, camera_names, *, selected=None, now=None):
    samples = history.get("samples", [])
    controls = ('<p><a href="/settings">Settings</a> · <a href="/diagnostics">Refresh</a> · '
                '<a href="/diagnostics/logs">Raw service logs</a> · '
                '<a href="/support-report.json" download>Download system log (JSON)</a></p>')
    entry_points = (
        '<section class="panel"><h2>Access and tools</h2><ul>'
        '<li><strong>Recorder portal:</strong> '
        '<a href="http://osecameras.local">http://osecameras.local</a></li>'
        '<li><strong>Portal login:</strong> use the owner password.</li>'
        '<li><strong>Recorder shell (Pi pilot):</strong> '
        '<code>ssh vms-recovery@osecameras.local</code> — SSH key only; '
        'the portal password does not work here.</li>'
        '<li><strong>Remote access:</strong> '
        '<a href="/settings#remote-access">open remote access settings</a>.</li>'
        '<li><strong>Camera logs:</strong> open Raw service logs and choose a camera.</li>'
        '</ul><p>Use osecameras.local on the home network; DHCP addresses can change. '
        'Never paste or upload a private key.</p></section>'
    )
    if not samples:
        return controls + '<h1>System log</h1>' + entry_points + '<p>No measurements saved yet. Check again in one minute.</p>'
    index = selected if type(selected) is int and 0 <= selected < len(samples) else len(samples) - 1
    sample = samples[index]
    camera_names = {**camera_names, **{c["slot"]: c["camera_name"] for c in sample.get("cameras", []) if c.get("camera_name")}}
    stamp = sample.get("checked_at", "Unknown")
    try:
        stale = index == len(samples) - 1 and ((now or datetime.now(UTC)) - datetime.fromisoformat(stamp)).total_seconds() > 180
    except (ValueError, TypeError):
        stale = True
    notice = '<p class="notice" role="alert">Measurements are old. The log collector may be stopped.</p>' if stale else ''
    issues = sample.get("warnings", [])
    warnings = '<ul>' + ''.join('<li>' + escape(_warning(code, camera_names)) + '</li>' for code in issues) + '</ul>' if issues else '<p>No warnings in this sample.</p>'
    options = ''.join(f'<option value="{i}"{" selected" if i == index else ""}>{escape(s.get("checked_at", "Unknown"))}</option>' for i, s in reversed(list(enumerate(samples))))
    content = (controls + '<h1>System log</h1>' + entry_points
               + '<p>Recorder and camera measurements from one saved snapshot. '
               'Times show UTC. Unknown means the device did not supply a reading.</p>'
               f'<form action="/diagnostics" method="get"><label for="sample">Snapshot time</label><select id="sample" name="sample">{options}</select><button type="submit">Show snapshot</button></form>'
               f'{notice}<p>Captured: <time>{escape(stamp)}</time></p><section class="panel"><h2>Needs attention</h2>{warnings}</section>'
               '<section class="panel"><h2>Recorder</h2>' + _table(sample.get("recorder", {})) + '</section>'
               '<section class="panel"><h2>Recording drive</h2>' + _table(sample.get("storage", {})) + '</section>'
               '<section class="panel"><h2>Cameras</h2>')
    for camera in sample.get("cameras", []):
        slot = camera.get("slot")
        name = camera_names.get(slot, f"Camera {slot}")
        content += (f'<details class="system-log-device"><summary>Camera {slot}: {escape(name)} — {escape(camera.get("state", "unknown"))}</summary>'
                    + _table({k:v for k,v in camera.items() if k != "slot"}) + '</details>')
    content += '</section><section class="panel"><h2>Recent changes</h2><ol>'
    for event in reversed(history.get("events", [])[-30:]):
        event_names = dict(enumerate(event["camera_names"], 1)) if event.get("camera_names") else camera_names
        message = '; '.join(_warning(code, event_names) for code in event.get("warnings", [])) or 'Warnings cleared or device restarted'
        content += f'<li><time>{escape(event.get("checked_at", "Unknown"))}</time>: {escape(message)}</li>'
    return content + '</ol></section>'
