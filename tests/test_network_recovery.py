from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from video_monitoring_system.device.network_recovery import (
    EVIDENCE_LIMIT,
    NetworkManagerAdapter,
    NetworkObservation,
    evaluate_recovery,
    load_state,
    read_health_summary,
    record_action_result,
    run_cycle,
    save_state,
)


def completed(arguments, returncode=0, stdout=""):
    return subprocess.CompletedProcess(arguments, returncode, stdout, "")


class NetworkRecoveryPolicyTests(unittest.TestCase):
    def test_failure_layers_are_specific_and_ordered(self) -> None:
        observations = (
            (NetworkObservation(False, False, False, False), "radio"),
            (NetworkObservation(True, False, False, False), "address"),
            (NetworkObservation(True, True, False, False), "gateway"),
            (NetworkObservation(True, True, True, False), "recorder"),
            (NetworkObservation(True, True, True, True), "none"),
        )

        for observation, expected in observations:
            with self.subTest(expected=expected):
                self.assertEqual(observation.failed_layer, expected)

    def test_sustained_failure_reconnects_twice_then_reboots_once(self) -> None:
        state = load_state(Path("/missing"), "a" * 32)
        failed = NetworkObservation(True, True, False, False)

        actions = [evaluate_recovery(state, failed).action for _ in range(9)]

        self.assertEqual(
            actions,
            [
                "none",
                "none",
                "reconnect",
                "none",
                "none",
                "reconnect",
                "none",
                "none",
                "reboot",
            ],
        )
        self.assertTrue(state["current_boot"]["reboot_requested"])
        self.assertTrue(state["reboot_guard"])

    def test_reboot_guard_survives_boot_until_health_recovers(self) -> None:
        state = load_state(Path("/missing"), "a" * 32)
        failed = NetworkObservation(False, False, False, False)
        decision = None
        for _ in range(9):
            decision = evaluate_recovery(state, failed)
        self.assertEqual(decision.action, "reboot")
        record_action_result(state, decision, True)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            save_state(path, state)
            next_boot = load_state(path, "b" * 32)

        self.assertTrue(next_boot["reboot_guard"])
        self.assertTrue(next_boot["previous_boot"]["reboot_requested"])
        self.assertNotIn(
            "reboot",
            [evaluate_recovery(next_boot, failed).action for _ in range(12)],
        )

        healthy = NetworkObservation(True, True, True, True)
        evaluate_recovery(next_boot, healthy)
        self.assertFalse(next_boot["reboot_guard"])
        self.assertEqual(next_boot["current_boot"]["reconnects"], 0)

    def test_failed_reboot_command_does_not_permanently_arm_guard(self) -> None:
        state = load_state(Path("/missing"), "a" * 32)
        failed = NetworkObservation(False, False, False, False)
        decision = None
        for _ in range(9):
            decision = evaluate_recovery(state, failed)

        record_action_result(state, decision, False)

        self.assertFalse(state["reboot_guard"])
        self.assertFalse(state["current_boot"]["reboot_requested"])

    def test_evidence_is_bounded_and_contains_no_network_details(self) -> None:
        state = load_state(Path("/missing"), "a" * 32)
        healthy = NetworkObservation(True, True, True, True)
        failed = NetworkObservation(False, False, False, False)
        for index in range(EVIDENCE_LIMIT + 10):
            evaluate_recovery(state, healthy if index % 2 else failed)

        encoded = json.dumps(state)
        self.assertEqual(len(state["evidence"]), EVIDENCE_LIMIT)
        self.assertNotIn("192.0.2", encoded)
        self.assertNotIn("password", encoded.lower())

    def test_corrupt_state_fails_closed_to_unknown_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            path.write_text('{"schema_version":1}', encoding="utf-8")

            state = load_state(path, "a" * 32)
            health = read_health_summary(path)

        self.assertEqual(state["current_boot"]["status"], "unknown")
        self.assertEqual(health["state"], "unknown")
        self.assertFalse(health["recorder_reachable"])

    def test_load_recovers_reboot_summary_from_last_good_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            state = load_state(path, "a" * 32)
            evaluate_recovery(state, NetworkObservation(True, True, True, True))
            save_state(path, state)

            for replacement in (None, "not-json\n"):
                with self.subTest(replacement=replacement):
                    if replacement is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(replacement, encoding="utf-8")
                    recovered = load_state(path, "b" * 32)
                    self.assertEqual(recovered["previous_boot"]["status"], "healthy")
                    self.assertEqual(recovered["boot_id"], "b" * 32)
                    save_state(path, recovered)
                    reloaded = load_state(path, "b" * 32)
                    self.assertEqual(reloaded["previous_boot"]["status"], "healthy")

            backup = path.with_name("health.json.last-good")
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)


class NetworkManagerAdapterTests(unittest.TestCase):
    def test_observation_checks_radio_address_gateway_and_recorder(self) -> None:
        calls = []

        def runner(arguments):
            arguments = tuple(arguments)
            calls.append(arguments)
            if arguments[:4] == ("nmcli", "-t", "-f", "GENERAL.STATE"):
                return completed(arguments, stdout="GENERAL.STATE:100 (connected)\n")
            if arguments[:3] == ("ip", "-4", "-o"):
                return completed(
                    arguments, stdout="2: wlan0 inet 192.0.2.5/24 scope global\n"
                )
            if arguments[:4] == ("ip", "-4", "route", "show"):
                return completed(arguments, stdout="default via 192.0.2.1 dev wlan0\n")
            if arguments[0] == "ping":
                return completed(arguments)
            raise AssertionError(arguments)

        adapter = NetworkManagerAdapter(
            "vms-home-001", recorder_host="recorder.local", runner=runner
        )

        self.assertEqual(adapter.observe(), NetworkObservation(True, True, True, True))
        ping_targets = [arguments[-1] for arguments in calls if arguments[0] == "ping"]
        self.assertEqual(ping_targets, ["192.0.2.1", "recorder.local"])

    def test_associated_radio_without_address_stops_at_address_layer(self) -> None:
        def runner(arguments):
            if arguments[0] == "nmcli":
                return completed(arguments, stdout="GENERAL.STATE:100 (connected)\n")
            return completed(arguments, stdout="")

        adapter = NetworkManagerAdapter("vms-home-002", runner=runner)

        self.assertEqual(adapter.observe().failed_layer, "address")

    def test_reconnect_is_scoped_to_the_provisioned_connection(self) -> None:
        calls = []

        def runner(arguments):
            calls.append(tuple(arguments))
            return completed(arguments)

        adapter = NetworkManagerAdapter("vms-home-003", runner=runner)

        self.assertTrue(adapter.reconnect())
        self.assertEqual(
            calls,
            [
                ("nmcli", "--wait", "15", "connection", "down", "id", "vms-home-003"),
                ("nmcli", "--wait", "60", "connection", "up", "id", "vms-home-003"),
            ],
        )

    def test_run_cycle_persists_safe_health(self) -> None:
        class Adapter:
            def observe(self):
                return NetworkObservation(True, True, True, True)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network-health.json"

            decision = run_cycle(path, "a" * 32, Adapter())
            health = read_health_summary(path)

        self.assertEqual(decision.action, "none")
        self.assertEqual(health["state"], "healthy")
        self.assertTrue(health["recorder_reachable"])
        self.assertEqual(health["failed_layer"], "none")


if __name__ == "__main__":
    unittest.main()
