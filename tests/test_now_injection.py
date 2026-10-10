import json
import re
import tempfile
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.context import projection


class NowInjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _now_field(self, turn):
        msgs = projection(self.app, turn)
        system = msgs[0]['content']
        m = re.search(r'"now":\s*(\{.*?\})', system)
        self.assertIsNotNone(m, "packet has no now field")
        return json.loads(m.group(1))

    def test_now_injected_with_real_date(self):
        m = self.app.send('bot-main', {'text': 'x'}, 'nowt')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        now = self._now_field(turn)
        for k in ('iso', 'date', 'time', 'weekday', 'timezone'):
            self.assertIn(k, now)
        # date matches what the same tz says for the real current time (not a guess)
        expected = datetime.fromtimestamp(time.time(), ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
        self.assertEqual(now['date'], expected)
        self.assertEqual(now['timezone'], 'Asia/Shanghai')

    def test_now_respects_configured_timezone(self):
        cfg = dict(self.app.config)
        cfg = __import__('copy').deepcopy(self.app.config)
        cfg['default_timezone'] = 'America/New_York'
        app2 = Service(cfg, self.root / 'data2')
        try:
            m = app2.send('bot-main', {'text': 'x'}, 'tz')
            turn = app2.store.find('turn', work_id=m['work_id'])[0]
            msgs = projection(app2, turn)
            now = json.loads(re.search(r'"now":\s*(\{.*?\})', msgs[0]['content']).group(1))
            self.assertEqual(now['timezone'], 'America/New_York')
            expected = datetime.fromtimestamp(time.time(), ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
            self.assertEqual(now['date'], expected)
        finally:
            app2.store.close()

    def test_prompt_tells_model_to_use_now(self):
        m = self.app.send('bot-main', {'text': 'x'}, 'p')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        system = projection(self.app, turn)[0]['content']
        self.assertIn('packet.now', system)
        self.assertIn('create_routine', system)


if __name__ == '__main__':
    unittest.main()
