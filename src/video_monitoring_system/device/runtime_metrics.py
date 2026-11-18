"""Small Linux measurements shared by camera and recorder adapters.

Absent hardware interfaces produce unknown values. No addresses, credentials,
kernel logs or permanent hardware identifiers are included.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re


def power_flags(value: object) -> dict[str, bool | None]:
    match = re.fullmatch(r"throttled=0x([0-9a-fA-F]{1,8})", str(value))
    mask = int(match[1], 16) if match else None
    return {
        name: bool(mask & (1 << bit)) if mask is not None else None
        for name, bit in (
            ("low_voltage_now", 0), ("frequency_capped_now", 1),
            ("throttled_now", 2), ("temperature_limit_now", 3),
            ("low_voltage_seen", 16), ("frequency_capped_seen", 17),
            ("throttled_seen", 18), ("temperature_limit_seen", 19),
        )
    }


def _read(path: Path) -> str:
    try:
        with path.open(encoding="ascii") as stream:
            return stream.read(16384).strip()
    except (OSError, UnicodeError):
        return ""


def _number(value: str, minimum: float = 0, maximum: float = 1e12) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if math.isfinite(number) and minimum <= number <= maximum else None


def read_runtime_metrics(
    *, root: Path = Path("/"), source: Path | None = None,
    power: object = None, cpu_count: int | None = None,
) -> dict[str, object]:
    proc = root / "proc"
    boot = _read(proc / "sys/kernel/random/boot_id")
    revision = None
    if source is not None:
        try:
            revision = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        except OSError:
            pass
    temperature = None
    zones = root / "sys/class/thermal"
    try:
        for zone in sorted(zones.glob("thermal_zone*")):
            if _read(zone / "type") in {
                "cpu-thermal", "cpu_thermal", "bcm2835_thermal",
                "x86_pkg_temp", "soc_thermal",
            }:
                raw = _number(_read(zone / "temp"), 0, 150000)
                temperature = round(raw / 1000, 2) if raw is not None else None
                break
    except OSError:
        pass
    uptime = _number((_read(proc / "uptime").split() or [""])[0])
    load = _number((_read(proc / "loadavg").split() or [""])[0])
    cores = cpu_count or os.cpu_count() or 1
    return {
        "boot": hashlib.sha256(boot.encode()).hexdigest()[:16] if boot else None,
        "uptime_s": uptime,
        "temperature_c": temperature,
        "load_per_core": round(load / cores, 2) if load is not None else None,
        "agent_revision": revision,
        "power": power_flags(power),
    }
