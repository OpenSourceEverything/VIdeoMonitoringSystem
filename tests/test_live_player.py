import json
import shutil
import subprocess
import unittest
from pathlib import Path

from video_monitoring_system.gui import _LIVE_REFRESH_SCRIPT


@unittest.skipUnless(shutil.which("node"), "Node is required for player execution tests")
class LivePlayerTests(unittest.TestCase):
    def test_real_player_recovers_and_keeps_metrics_honest(self):
        harness = Path(__file__).with_name("live_player_harness.cjs")
        for scenario in (
            "signaling_timeout",
            "stalled_video",
            "independent_rates",
            "brief_disconnect",
            "page_closed",
            "restored_page",
            "expired_session",
            "background_tab",
            "empty_recording",
        ):
            with self.subTest(scenario=scenario):
                result = subprocess.run(
                    ["node", str(harness)],
                    input=json.dumps({
                        "script": _LIVE_REFRESH_SCRIPT.decode(),
                        "scenario": scenario,
                    }),
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
