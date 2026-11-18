import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from video_monitoring_system.engine_alerts import (
    EngineAlertLedger,
    EngineAlertLedgerError,
)
from video_monitoring_system.video_engine import EngineEvent


def event(
    identifier: str,
    started_at: datetime,
    *,
    ended: bool = True,
    severity: str = "alert",
) -> EngineEvent:
    return EngineEvent(
        id=identifier,
        camera_id="CAM-BACK",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=10) if ended else None,
        severity=severity,
        labels=("person",),
    )


class EngineAlertLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.ledger = EngineAlertLedger(self.directory)
        self.started = datetime(2026, 9, 22, 3, 0, tzinfo=UTC)
        self.ledger.initialize(now=self.started)

    def test_new_confirmed_alerts_are_due_before_or_after_completion(self):
        events = (
            event("frigate:old", self.started - timedelta(seconds=1)),
            event("frigate:active", self.started + timedelta(seconds=1), ended=False),
            event(
                "frigate:detection",
                self.started + timedelta(seconds=2),
                severity="detection",
            ),
            event("frigate:new", self.started + timedelta(seconds=3)),
        )

        self.assertEqual(
            self.ledger.due(events, now=self.started + timedelta(seconds=20)),
            (events[1], events[-1]),
        )

    def test_success_deduplicates_and_failure_uses_bounded_backoff(self):
        selected = event("frigate:new", self.started + timedelta(seconds=1))
        now = self.started + timedelta(seconds=20)

        self.ledger.record(selected.id, delivered=False, now=now)
        self.assertEqual(self.ledger.due((selected,), now=now), ())
        self.assertEqual(
            self.ledger.due((selected,), now=now + timedelta(seconds=15)),
            (selected,),
        )
        self.ledger.record(selected.id, delivered=True, now=now + timedelta(seconds=15))
        self.assertEqual(
            self.ledger.due((selected,), now=now + timedelta(days=1)),
            (),
        )

    def test_suppressed_alert_is_not_delivered_after_alerts_resume(self):
        selected = event("frigate:muted", self.started + timedelta(seconds=1))
        now = self.started + timedelta(seconds=20)

        self.ledger.suppress(selected.id, now=now)

        self.assertEqual(
            self.ledger.due((selected,), now=now + timedelta(days=1)),
            (),
        )
        saved = json.loads(self.ledger.path.read_text(encoding="utf-8"))
        self.assertIsNotNone(saved["events"][selected.id]["suppressed_at"])

    def test_private_state_survives_restart_and_rejects_corruption(self):
        selected = event("frigate:new", self.started + timedelta(seconds=1))
        self.ledger.record(selected.id, delivered=True, now=self.started + timedelta(seconds=20))
        reloaded = EngineAlertLedger(self.directory)

        self.assertEqual(
            reloaded.due((selected,), now=self.started + timedelta(days=1)),
            (),
        )
        self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)
        self.ledger.path.write_text(json.dumps({"version": 99}))
        with self.assertRaises(EngineAlertLedgerError):
            reloaded.due((selected,), now=self.started + timedelta(days=1))

    def test_separate_workers_cannot_send_the_same_pending_batch(self):
        other = EngineAlertLedger(self.directory)
        with self.ledger.delivery_cycle() as acquired:
            self.assertTrue(acquired)
            with other.delivery_cycle() as competing:
                self.assertFalse(competing)
        with other.delivery_cycle() as acquired:
            self.assertTrue(acquired)
        self.assertEqual(
            self.ledger.path.with_suffix(".delivery.lock").stat().st_mode & 0o777,
            0o600,
        )

    def test_delivery_lock_releases_after_failure_without_consuming_events(self):
        selected = event("frigate:pending", self.started + timedelta(seconds=1))
        with self.assertRaises(RuntimeError):
            with self.ledger.delivery_cycle():
                raise RuntimeError("worker stopped")
        other = EngineAlertLedger(self.directory)
        with other.delivery_cycle() as acquired:
            self.assertTrue(acquired)
            self.assertEqual(
                other.due((selected,), now=self.started + timedelta(seconds=10)),
                (selected,),
            )

    def test_worker_refuses_a_symlink_lock(self):
        target = self.directory / "unrelated"
        target.write_text("preserve")
        self.ledger.path.with_suffix(".delivery.lock").symlink_to(target)
        with self.assertRaises(OSError):
            with self.ledger.delivery_cycle():
                self.fail("A symlink cannot be used as a worker lock.")
        self.assertEqual(target.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
