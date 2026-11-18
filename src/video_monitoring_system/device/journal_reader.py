"""Bounded, allowlisted systemd journal access for the VMS diagnostic view."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import re
import selectors
import subprocess
import sys
import time


RECORDER_LOG_SOURCES = {
    "portal": ("Recorder portal", ("vms-recorder.service",)),
    "capture": ("Recording capture", ("vms-recorder-capture.service",)),
    "monitor": ("Health monitor", ("vms-recorder-monitor.service",)),
    "alerts": ("Alert worker", ("vms-recorder-alerts.service",)),
    "maintenance": ("Recording maintenance", ("vms-recorder-maintenance.service",)),
    "frigate": ("Frigate video engine", ("vms-frigate.service",)),
    "camera-routes": (
        "Camera route services",
        ("vms-camera-route-relay.service", "vms-frigate-reconcile.service"),
    ),
    "system": ("All system services", "all"),
    "network": ("Network services", ("NetworkManager.service", "systemd-networkd.service")),
    "kernel": ("Kernel", "kernel"),
    "boot-warnings": ("Boot and system warnings", "warnings"),
}

CAMERA_LOG_SOURCES = {
    "camera": ("Camera capture service", ("vms-camera.service",)),
    "network-recovery": ("Camera network recovery", ("vms-network-recovery.service",)),
    "system": ("All system services", "all"),
    "network": ("NetworkManager", ("NetworkManager.service",)),
    "kernel": ("Kernel and camera sensor", "kernel"),
    "boot-warnings": ("Boot and system warnings", "warnings"),
}

ALLOWED_LOG_LINES = frozenset({50, 100, 200})
MAX_LOG_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 4096
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|passphrase|psk|secret|stream_secret|claim_secret|"
    r"token|access_token|refresh_token|authorization|cookie|credential|credentials|"
    r"client_secret|api_key|private_key|private-key|wifi_key|wifi_password|"
    r"wireless_password|wpa_key)\b"
    r"(\s*[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;\]}]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


def log_source_catalog(role: str) -> dict[str, str]:
    """Return the public labels for fixed recorder or Pi-camera log sources."""

    catalog = RECORDER_LOG_SOURCES if role == "recorder" else CAMERA_LOG_SOURCES if role == "camera" else None
    if catalog is None:
        raise ValueError("diagnostic log role is invalid")
    return {key: item[0] for key, item in catalog.items()}


def unavailable_journal(role: str, source: str, lines: int) -> dict[str, object]:
    """Return the stable unavailable record when a remote source cannot be read."""

    catalog = RECORDER_LOG_SOURCES if role == "recorder" else CAMERA_LOG_SOURCES if role == "camera" else None
    if catalog is None or source not in catalog or type(lines) is not int or lines not in ALLOWED_LOG_LINES:
        raise ValueError("diagnostic log request is invalid")
    return _unavailable(source, catalog[source][0], lines, datetime.now(UTC).isoformat(timespec="seconds"))


def read_journal(role: str, source: str, lines: int = 100) -> dict[str, object]:
    """Read a recent journal tail without accepting arbitrary commands or units."""

    catalog = RECORDER_LOG_SOURCES if role == "recorder" else CAMERA_LOG_SOURCES if role == "camera" else None
    if catalog is None or source not in catalog or type(lines) is not int or lines not in ALLOWED_LOG_LINES:
        raise ValueError("diagnostic log request is invalid")

    label, units = catalog[source]
    command = [
        "/usr/bin/journalctl",
        "--no-pager",
        "--quiet",
        f"--lines={lines}",
        "--output=short-iso-precise",
    ]
    if units == "kernel":
        command.append("--kernel")
    elif units == "warnings":
        command.extend(("--boot", "--priority=warning"))
    elif units == "all":
        command.append("--boot")
    else:
        command.extend(f"--unit={unit}" for unit in units)

    captured_at = datetime.now(UTC).isoformat(timespec="seconds")
    output = bytearray()
    truncated = False
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        if process.stdout is None:
            process.kill()
            return _unavailable(source, label, lines, captured_at)
        deadline = time.monotonic() + 5
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(command, 5)
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                output.extend(chunk)
                if len(output) > MAX_LOG_BYTES + 8192:
                    del output[: len(output) - (MAX_LOG_BYTES + 8192)]
                    truncated = True
        if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            return _unavailable(source, label, lines, captured_at)
    except (OSError, subprocess.TimeoutExpired):
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.stdout is not None:
                process.stdout.close()
        return _unavailable(source, label, lines, captured_at)
    if process.stdout is not None:
        process.stdout.close()

    if len(output) > MAX_LOG_BYTES:
        truncated = True
    if truncated:
        start = max(0, len(output) - MAX_LOG_BYTES)
        boundary = output.find(b"\n", start)
        output = output[boundary + 1 :] if boundary >= 0 else bytearray()
    text = redact_log_text(output.decode("utf-8", errors="replace"))
    rendered_lines = text.splitlines()
    if len(rendered_lines) > lines:
        text = "\n".join(rendered_lines[-lines:])
        truncated = True
    return {
        "schema_version": 1,
        "state": "available",
        "source": source,
        "label": label,
        "captured_at": captured_at,
        "lines_requested": lines,
        "lines_returned": len(text.splitlines()),
        "truncated": truncated,
        "text": text,
    }


def redact_log_text(value: str) -> str:
    """Keep useful journal text while removing common credential values."""

    def redact_assignment(match: re.Match[str]) -> str:
        value = match.group(3)
        quote = (
            value[0]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
            else ""
        )
        return f"{match.group(1)}{match.group(2)}{quote}[REDACTED]{quote}"

    text = _SENSITIVE_ASSIGNMENT.sub(redact_assignment, value)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    return _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", text)


def recorder_helper_main() -> int:
    """Root-helper entry point: accept one small JSON request on stdin."""

    payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(payload) > MAX_REQUEST_BYTES:
        return 2
    try:
        request = json.loads(payload)
        if (
            not isinstance(request, dict)
            or set(request) != {"source", "lines"}
            or not isinstance(request.get("source"), str)
        ):
            return 2
        result = read_journal("recorder", request["source"], request["lines"])
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def _unavailable(source: str, label: str, lines: int, captured_at: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "state": "unavailable",
        "source": source,
        "label": label,
        "captured_at": captured_at,
        "lines_requested": lines,
        "lines_returned": 0,
        "truncated": False,
        "text": "Journal source is unavailable on this device.",
    }
