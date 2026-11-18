import copy
from datetime import UTC, datetime
import unittest

from video_monitoring_system.diagnostic_view import render_system_log


class DiagnosticViewTests(unittest.TestCase):
    def test_device_sections_historical_selection_and_escaping(self):
        sample={"checked_at":"2026-10-29T14:00:00+00:00", "recorder":{"temperature_c":68,"power":{"low_voltage_now":False,"low_voltage_seen":True}},"storage":{"free_bytes":1024**3},"cameras":[{"slot":2,"state":"online","runtime":{"temperature_c":51},"source_fps":15,"engine_metrics":{"detect_input_fps":3}}],"warnings":["camera-2-detector-skipping"]}
        past=copy.deepcopy(sample)
        past["recorder"]["temperature_c"]=44
        history={"samples":[past,sample],"events":[]}
        page=render_system_log(history,{2:'Porch <script>'},now=datetime(2026,10,29,14,1,tzinfo=UTC))
        for text in ('<h2>Recorder</h2>','<h2>Recording drive</h2>','Camera 2: Porch &lt;script&gt;','Low voltage now','Low voltage since boot','Camera video target (frames/second)','Frames entering detection (per second)','/support-report.json'):
            self.assertIn(text,page)
        self.assertNotIn('<script>',page)
        past_page=render_system_log(history,{2:'Porch'},selected=0)
        self.assertIn('>44<',past_page)
        self.assertNotIn('Measurements are old',past_page)

    def test_stale_or_missing_readings_never_look_current(self):
        self.assertIn('No measurements saved yet',render_system_log({},{}))
        page=render_system_log({"samples":[{"checked_at":"2020-01-01T00:00:00+00:00","recorder":{"temperature_c":None}}]}, {})
        self.assertIn('Measurements are old',page)
        self.assertIn('Unknown',page)
