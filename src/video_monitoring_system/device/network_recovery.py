"""Bounded, recorder-independent camera network recovery.

The recovery policy is pure Python and the NetworkManager calls are confined to
one adapter.  Recorder engines and legacy migration targets do not participate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence


STATE_VERSION = 1
FAILURES_BEFORE_ACTION = 3
MAX_RECONNECTS_PER_BOOT = 2
EVIDENCE_LIMIT = 24
HEALTHY_LAYER = "none"
UNKNOWN_LAYER = "unknown"
FAILED_LAYERS = frozenset(("radio", "address", "gateway", "recorder"))
ACTIONS = frozenset(("none", "reconnect", "reboot"))


@dataclass(frozen=True, slots=True)
class NetworkObservation:
    radio_associated: bool
    address_present: bool
    gateway_reachable: bool
    recorder_reachable: bool

    @property
    def failed_layer(self) -> str:
        if not self.radio_associated:
            return "radio"
        if not self.address_present:
            return "address"
        if not self.gateway_reachable:
            return "gateway"
        if not self.recorder_reachable:
            return "recorder"
        return HEALTHY_LAYER


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: str
    failed_layer: str


def _new_boot() -> dict[str, object]:
    return {
        "status": "unknown",
        "failed_layer": UNKNOWN_LAYER,
        "recorder_reachable": False,
        "observations": 0,
        "consecutive_failures": 0,
        "reconnects": 0,
        "reboot_requested": False,
    }


def _new_state(boot_id: str) -> dict[str, object]:
    return {
        "schema_version": STATE_VERSION,
        "boot_id": boot_id,
        "sequence": 0,
        "reboot_guard": False,
        "current_boot": _new_boot(),
        "previous_boot": None,
        "evidence": [],
    }


def _boot_summary(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": value.get("status", "unknown"),
        "failed_layer": value.get("failed_layer", UNKNOWN_LAYER),
        "recorder_reachable": value.get("recorder_reachable") is True,
        "observations": int(value.get("observations", 0)),
        "consecutive_failures": int(value.get("consecutive_failures", 0)),
        "reconnects": int(value.get("reconnects", 0)),
        "reboot_requested": value.get("reboot_requested") is True,
    }


def _valid_boot(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    counters_are_valid = all(
        type(value.get(name)) is int and int(value[name]) >= 0
        for name in ("observations", "consecutive_failures", "reconnects")
    )
    return (
        value.get("status") in {"unknown", "healthy", "degraded", "recovering"}
        and value.get("failed_layer") in FAILED_LAYERS | {HEALTHY_LAYER, UNKNOWN_LAYER}
        and isinstance(value.get("recorder_reachable"), bool)
        and isinstance(value.get("reboot_requested"), bool)
        and counters_are_valid
    )


def _valid_state(value: object) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != STATE_VERSION:
        return False
    current = value.get("current_boot")
    evidence = value.get("evidence")
    if not _valid_boot(current) or not isinstance(evidence, list):
        return False
    previous = value.get("previous_boot")
    return (
        isinstance(value.get("boot_id"), str)
        and bool(value["boot_id"])
        and type(value.get("sequence")) is int
        and int(value["sequence"]) >= 0
        and isinstance(value.get("reboot_guard"), bool)
        and (previous is None or _valid_boot(previous))
    )


def _read_valid_state(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if _valid_state(value) else None


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.last-good")


def load_state(path: Path, boot_id: str) -> dict[str, object]:
    """Load state, retaining one previous-boot summary when boot ID changes."""

    value = _read_valid_state(path) or _read_valid_state(_backup_path(path))
    if value is None:
        return _new_state(boot_id)
    if value["boot_id"] == boot_id:
        return value

    previous = _boot_summary(value["current_boot"])
    value["boot_id"] = boot_id
    value["previous_boot"] = previous
    value["current_boot"] = _new_boot()
    value["reboot_guard"] = previous["reboot_requested"] is True
    _append_evidence(value, "boot", UNKNOWN_LAYER, "none", "started")
    return value


def _write_state(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def save_state(path: Path, state: Mapping[str, object]) -> None:
    """Atomically persist owner-private recovery state and one fallback copy."""

    _write_state(path, state)
    _write_state(_backup_path(path), state)


def evaluate_recovery(
    state: dict[str, object], observation: NetworkObservation
) -> RecoveryDecision:
    """Apply one observation and choose at most one bounded recovery action."""

    current = state["current_boot"]
    assert isinstance(current, dict)
    layer = observation.failed_layer
    previous_layer = current.get("failed_layer")
    current["observations"] = int(current.get("observations", 0)) + 1
    current["failed_layer"] = layer
    current["recorder_reachable"] = observation.recorder_reachable

    if layer == HEALTHY_LAYER:
        current["status"] = "healthy"
        current["consecutive_failures"] = 0
        current["reconnects"] = 0
        state["reboot_guard"] = False
        if previous_layer != layer:
            _append_evidence(state, "observation", layer, "none", "healthy")
        return RecoveryDecision("none", layer)

    current["status"] = "degraded"
    current["consecutive_failures"] = int(current.get("consecutive_failures", 0)) + 1
    action = "none"
    if int(current["consecutive_failures"]) >= FAILURES_BEFORE_ACTION:
        current["consecutive_failures"] = 0
        if int(current.get("reconnects", 0)) < MAX_RECONNECTS_PER_BOOT:
            current["reconnects"] = int(current.get("reconnects", 0)) + 1
            action = "reconnect"
        elif not state.get("reboot_guard") and not current.get("reboot_requested"):
            current["reboot_requested"] = True
            state["reboot_guard"] = True
            action = "reboot"

    if previous_layer != layer or action != "none":
        _append_evidence(state, "observation", layer, action, "observed")
    return RecoveryDecision(action, layer)


def record_action_result(
    state: dict[str, object], decision: RecoveryDecision, succeeded: bool
) -> None:
    if decision.action == "none":
        return
    current = state["current_boot"]
    assert isinstance(current, dict)
    current["status"] = "recovering" if succeeded else "degraded"
    if decision.action == "reboot" and not succeeded:
        current["reboot_requested"] = False
        state["reboot_guard"] = False
    _append_evidence(
        state,
        "action",
        decision.failed_layer,
        decision.action,
        "succeeded" if succeeded else "failed",
    )


def _append_evidence(
    state: dict[str, object], event: str, layer: str, action: str, result: str
) -> None:
    sequence = int(state.get("sequence", 0)) + 1
    state["sequence"] = sequence
    evidence = state.setdefault("evidence", [])
    assert isinstance(evidence, list)
    evidence.append(
        {
            "sequence": sequence,
            "event": event,
            "layer": (
                layer if layer in FAILED_LAYERS | {HEALTHY_LAYER} else UNKNOWN_LAYER
            ),
            "action": action if action in ACTIONS else "none",
            "result": result,
        }
    )
    del evidence[:-EVIDENCE_LIMIT]


def read_health_summary(path: Path) -> dict[str, object]:
    """Return only the stable, address-free fields exposed by camera health."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        value = None
    if not _valid_state(value):
        return {
            "state": "unknown",
            "failed_layer": UNKNOWN_LAYER,
            "recorder_reachable": False,
            "current_boot": _boot_summary(_new_boot()),
            "previous_boot": None,
        }
    current = _boot_summary(value["current_boot"])
    previous_value = value.get("previous_boot")
    previous = (
        _boot_summary(previous_value) if isinstance(previous_value, dict) else None
    )
    return {
        "state": current["status"],
        "failed_layer": current["failed_layer"],
        "recorder_reachable": current["recorder_reachable"],
        "current_boot": current,
        "previous_boot": previous,
    }


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class NetworkManagerAdapter:
    """Observe and repair one NetworkManager camera connection."""

    def __init__(
        self,
        connection_name: str,
        *,
        interface: str = "wlan0",
        recorder_host: str | None = None,
        runner: Runner | None = None,
    ) -> None:
        if re.fullmatch(r"vms-home-[0-9]{3}", connection_name) is None:
            raise ValueError("camera connection name is invalid")
        if re.fullmatch(r"[A-Za-z0-9_.-]+", interface) is None:
            raise ValueError("network interface is invalid")
        self.connection_name = connection_name
        self.interface = interface
        self.recorder_host = _validated_host(recorder_host) if recorder_host else None
        self._runner = runner or self._run

    @staticmethod
    def _run(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=70,
            env={**os.environ, "LC_ALL": "C"},
        )

    def observe(self) -> NetworkObservation:
        radio = self._runner(
            ("nmcli", "-t", "-f", "GENERAL.STATE", "device", "show", self.interface)
        )
        associated = radio.returncode == 0 and any(
            line.startswith("GENERAL.STATE:100") for line in radio.stdout.splitlines()
        )
        if not associated:
            return NetworkObservation(False, False, False, False)

        address = self._runner(
            (
                "ip",
                "-4",
                "-o",
                "address",
                "show",
                "dev",
                self.interface,
                "scope",
                "global",
            )
        )
        address_present = address.returncode == 0 and bool(address.stdout.strip())
        if not address_present:
            return NetworkObservation(True, False, False, False)

        route = self._runner(
            ("ip", "-4", "route", "show", "default", "dev", self.interface)
        )
        gateway = _gateway_from_route(route.stdout) if route.returncode == 0 else None
        if gateway is None or not self._ping(gateway):
            return NetworkObservation(True, True, False, False)

        recorder_reachable = (
            True if self.recorder_host is None else self._ping(self.recorder_host)
        )
        return NetworkObservation(True, True, True, recorder_reachable)

    def _ping(self, host: str) -> bool:
        result = self._runner(("ping", "-n", "-c", "1", "-W", "2", "--", host))
        return result.returncode == 0

    def reconnect(self) -> bool:
        self._runner(
            ("nmcli", "--wait", "15", "connection", "down", "id", self.connection_name)
        )
        result = self._runner(
            ("nmcli", "--wait", "60", "connection", "up", "id", self.connection_name)
        )
        return result.returncode == 0

    def reboot(self) -> bool:
        return self._runner(("systemctl", "reboot")).returncode == 0


def _gateway_from_route(output: str) -> str | None:
    fields = output.split()
    try:
        candidate = fields[fields.index("via") + 1]
        return str(ipaddress.ip_address(candidate))
    except (ValueError, IndexError):
        return None


def _validated_host(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("-")
    ):
        raise ValueError("recorder host is invalid")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        labels = value.rstrip(".").split(".")
        if len(value) > 253 or any(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            is None
            for label in labels
        ):
            raise ValueError("recorder host is invalid") from None
        return value


def _load_config(path: Path) -> tuple[str, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("camera configuration is unreadable") from error
    if not isinstance(value, dict):
        raise ValueError("camera configuration is invalid")
    number = value.get("camera_number")
    if (
        value.get("role") != "camera"
        or isinstance(number, bool)
        or not isinstance(number, int)
        or not 1 <= number <= 999
    ):
        raise ValueError("camera configuration is invalid")
    assigned = value.get("assigned_recorder")
    host = assigned.get("network_host") if isinstance(assigned, dict) else None
    if host is not None and not isinstance(host, str):
        raise ValueError("recorder host is invalid")
    expected_connection = f"vms-home-{number:03d}"
    configured_connection = (
        assigned.get("home_connection") if isinstance(assigned, dict) else None
    )
    if configured_connection is not None and configured_connection != expected_connection:
        raise ValueError("home network connection is invalid")
    return expected_connection, host


def _read_boot_id(path: Path) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise ValueError("boot identity is unavailable") from error
    if re.fullmatch(r"[A-Fa-f0-9-]{16,64}", value) is None:
        raise ValueError("boot identity is invalid")
    return value


def run_cycle(
    state_path: Path, boot_id: str, adapter: NetworkManagerAdapter
) -> RecoveryDecision:
    state = load_state(state_path, boot_id)
    decision = evaluate_recovery(state, adapter.observe())
    save_state(state_path, state)
    if decision.action == "reconnect":
        succeeded = adapter.reconnect()
    elif decision.action == "reboot":
        succeeded = adapter.reboot()
    else:
        return decision
    record_action_result(state, decision, succeeded)
    save_state(state_path, state)
    return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded VMS network check.")
    parser.add_argument("--config", type=Path, default=Path("/etc/vms/device.json"))
    parser.add_argument(
        "--state", type=Path, default=Path("/var/lib/vms/network-health.json")
    )
    parser.add_argument(
        "--boot-id", type=Path, default=Path("/proc/sys/kernel/random/boot_id")
    )
    arguments = parser.parse_args(argv)
    try:
        connection_name, recorder_host = _load_config(arguments.config)
        adapter = NetworkManagerAdapter(connection_name, recorder_host=recorder_host)
        run_cycle(arguments.state, _read_boot_id(arguments.boot_id), adapter)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
