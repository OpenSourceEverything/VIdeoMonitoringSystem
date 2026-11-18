"""Bounded delivery ledger for OSE alerts sourced from a video engine.

The engine remains authoritative for event lifecycle and deduplication.  This
ledger answers only whether OSE already delivered a notification for an engine
event and when a failed delivery may be retried.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from contextlib import contextmanager
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .provisioning import secure_write_text
from .video_engine import EngineEvent


class EngineAlertLedgerError(RuntimeError):
    """The private alert delivery ledger is invalid."""


_STATE_FILE = "video-engine-alerts.json"
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_MAXIMUM_ENTRIES = 1000


class EngineAlertLedger:
    """Persist notification delivery state without owning engine events."""

    def __init__(self, data_directory: Path) -> None:
        self.path = Path(data_directory) / _STATE_FILE
        self._lock = threading.RLock()

    @contextmanager
    def _file_lock(self, purpose: str, *, blocking: bool = True):
        """Serialize separate recorder processes without logging private state."""

        import fcntl

        descriptor = os.open(
            self.path.with_suffix(f".{purpose}.lock"),
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @contextmanager
    def delivery_cycle(self):
        """Only one poll-and-send cycle may run across all workers."""

        with self._file_lock("delivery", blocking=False) as acquired:
            yield acquired

    def initialize(self, *, now: datetime | None = None) -> None:
        selected_now = self._now(now)
        with self._lock, self._file_lock("state"):
            self._state(selected_now)

    def due(
        self,
        events: Sequence[EngineEvent],
        *,
        now: datetime | None = None,
        limit: int = 5,
    ) -> tuple[EngineEvent, ...]:
        selected_now = self._now(now)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("Alert delivery limit is invalid.")
        with self._lock, self._file_lock("state"):
            state = self._state(selected_now)
            enabled_at = float(state["enabled_at"])
            entries = state["events"]
            assert isinstance(entries, dict)
            selected: list[EngineEvent] = []
            seen: set[str] = set()
            for event in sorted(events, key=lambda item: item.started_at):
                if not isinstance(event, EngineEvent):
                    raise TypeError("Engine events are required.")
                if event.id in seen:
                    continue
                seen.add(event.id)
                if (
                    event.severity != "alert"
                    or event.started_at.timestamp() < enabled_at
                ):
                    continue
                entry = entries.get(event.id)
                if entry is not None and not isinstance(entry, dict):
                    raise EngineAlertLedgerError(
                        "Saved video alert delivery state could not be read."
                    )
                if entry is not None and (
                    entry.get("delivered_at") is not None
                    or entry.get("suppressed_at") is not None
                ):
                    continue
                next_attempt_at = 0.0 if entry is None else entry.get("next_attempt_at", 0.0)
                if (
                    not isinstance(next_attempt_at, (int, float))
                    or isinstance(next_attempt_at, bool)
                    or not math.isfinite(float(next_attempt_at))
                ):
                    raise EngineAlertLedgerError(
                        "Saved video alert delivery state could not be read."
                    )
                if float(next_attempt_at) <= selected_now.timestamp():
                    selected.append(event)
                if len(selected) >= limit:
                    break
            return tuple(selected)

    def record(
        self,
        event_id: str,
        *,
        delivered: bool,
        now: datetime | None = None,
    ) -> None:
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            raise ValueError("Engine event ID is invalid.")
        if not isinstance(delivered, bool):
            raise TypeError("Alert delivery outcome must be boolean.")
        selected_now = self._now(now)
        timestamp = selected_now.timestamp()
        with self._lock, self._file_lock("state"):
            state = self._state(selected_now)
            entries = state["events"]
            assert isinstance(entries, dict)
            existing = entries.get(event_id, {})
            if not isinstance(existing, dict):
                raise EngineAlertLedgerError(
                    "Saved video alert delivery state could not be read."
                )
            attempts = existing.get("attempts", 0)
            if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
                raise EngineAlertLedgerError(
                    "Saved video alert delivery state could not be read."
                )
            attempts = min(attempts + 1, 1000)
            delay = min(3600, 15 * (2 ** min(attempts - 1, 8)))
            entries[event_id] = {
                "attempts": attempts,
                "last_attempt_at": timestamp,
                "next_attempt_at": timestamp if delivered else timestamp + delay,
                "delivered_at": timestamp if delivered else None,
            }
            self._prune(entries)
            self._save(state)

    def suppress(
        self,
        event_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Permanently consume an event while owner alert delivery is paused."""

        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            raise ValueError("Engine event ID is invalid.")
        selected_now = self._now(now)
        timestamp = selected_now.timestamp()
        with self._lock, self._file_lock("state"):
            state = self._state(selected_now)
            entries = state["events"]
            assert isinstance(entries, dict)
            existing = entries.get(event_id, {})
            if not isinstance(existing, dict):
                raise EngineAlertLedgerError(
                    "Saved video alert delivery state could not be read."
                )
            if (
                existing.get("delivered_at") is not None
                or existing.get("suppressed_at") is not None
            ):
                return
            attempts = existing.get("attempts", 0)
            if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
                raise EngineAlertLedgerError(
                    "Saved video alert delivery state could not be read."
                )
            entries[event_id] = {
                "attempts": attempts,
                "last_attempt_at": timestamp,
                "next_attempt_at": timestamp,
                "delivered_at": None,
                "suppressed_at": timestamp,
            }
            self._prune(entries)
            self._save(state)

    def _state(self, now: datetime) -> dict[str, object]:
        if not self.path.exists():
            state: dict[str, object] = {
                "version": 1,
                "enabled_at": now.timestamp(),
                "events": {},
            }
            self._save(state)
            return state
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(state, dict)
                or set(state) != {"version", "enabled_at", "events"}
                or state["version"] != 1
                or not isinstance(state["enabled_at"], (int, float))
                or isinstance(state["enabled_at"], bool)
                or not math.isfinite(float(state["enabled_at"]))
                or not isinstance(state["events"], dict)
                or len(state["events"]) > _MAXIMUM_ENTRIES
            ):
                raise ValueError
            return state
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise EngineAlertLedgerError(
                "Saved video alert delivery state could not be read."
            ) from None

    def _save(self, state: dict[str, object]) -> None:
        secure_write_text(
            self.path,
            json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n",
        )

    @staticmethod
    def _prune(entries: dict[str, object]) -> None:
        if len(entries) <= _MAXIMUM_ENTRIES:
            return
        ordered = sorted(
            entries,
            key=lambda event_id: float(
                entries[event_id].get("last_attempt_at", 0.0)  # type: ignore[union-attr]
            ),
        )
        for event_id in ordered[: len(entries) - _MAXIMUM_ENTRIES]:
            del entries[event_id]

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        selected = datetime.now(UTC) if value is None else value
        if not isinstance(selected, datetime) or selected.tzinfo is None:
            raise ValueError("Alert delivery time is invalid.")
        return selected.astimezone(UTC)
