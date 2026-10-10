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
from pangpang.context import projection, _evidence_manifest, model_artifact_view


class AtomicFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _system(self, turn):
        return projection(self.app, turn)[0]['content']

    # ---- #4 trigger ----
    def test_trigger_present_with_received_at_and_tz(self):
        m = self.app.send('bot-main', {'text': 'x'}, 't4')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        trig = json.loads(re.search(r'"trigger":\s*(\{.*?\})', self._system(turn)).group(1))
        self.assertEqual(trig['kind'], 'user')
        self.assertEqual(trig['effective_timezone'], 'Asia/Shanghai')
        self.assertIn('received_at', trig) # from the trigger message created ts

    def test_routine_trigger_uses_its_own_timezone(self):
        # A routine-kind work must expose the routine's timezone, not the global default.
        r = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'America/New_York',
                                                 'schedule': {'type': 'daily', 'time': '09:00'}})
        run = self.app.run_routine_now('bot-main', r['id'])
        # find the routine work + its turn
        rwork = next(w for w in self.app.store.find('work', limit=100)
                     if w.get('kind') == 'routine' and w.get('routine_id') == r['id'])
        turn = self.app.store.find('turn', work_id=rwork['id'])[0]
        trig = json.loads(re.search(r'"trigger":\s*(\{.*?\})', self._system(turn)).group(1))
        self.assertEqual(trig['effective_timezone'], 'America/New_York')
        self.assertEqual(trig['kind'], 'routine')

    # ---- #2 evidence_manifest ----
    def test_no_manifest_when_no_attachments(self):
        m = self.app.send('bot-main', {'text': '没有附件'}, 'e0')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        # the packet field (not the prompt text) must be absent
        system = self._system(turn)
        self.assertNotRegex(system, r'"evidence_manifest":\s*\{')

    def test_manifest_lists_attached_artifact(self):
        # attach a real artifact to the trigger message -> manifest surfaces it with
        # basename/size/host, never the absolute path or full digest.
        art = self.app.store.put('artifact', {'id': 'artifact-x', 'path': 'C:/secret/dir/报告.docx',
            'digest': 'a' * 64, 'size': 1234, 'host_id': 'local', 'owner_bot_id': 'bot-main',
            'work_id': None, 'source': 'user_import'})
        m = self.app.send('bot-main', {'text': '看这个文件', 'refs': ['artifact-x']}, 'e1')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        system = self._system(turn)
        self.assertIn('"evidence_manifest":', system)
        self.assertIn('artifact-x', system)
        self.assertIn('报告.docx', system) # basename shown
        self.assertNotIn('C:/secret/dir', system) # absolute path NOT leaked
        self.assertNotIn('a' * 64, system) # full digest NOT leaked

    def test_artifact_view_allowlist(self):
        self.app.store.put('artifact', {'id': 'artifact-y', 'path': '/x/y/z.txt', 'digest': 'b' * 64,
            'size': 9, 'host_id': 'h1', 'owner_bot_id': 'bot-main', 'work_id': None, 'source': 'task-1'})
        v = model_artifact_view(self.app, 'artifact-y')
        self.assertEqual(set(v) - {'ref'}, {'resolved', 'name', 'size', 'host_id', 'kind', 'mime', 'source', 'verification_status'})
        self.assertEqual(v['name'], 'z.txt')
        self.assertNotIn('digest', v)
        self.assertEqual(v['verification_status'], 'recorded_metadata_only')

    def test_unresolvable_ref_returns_stub(self):
        v = model_artifact_view(self.app, 'not-a-real-ref')
        self.assertEqual(v, {'ref': 'not-a-real-ref', 'resolved': False})

    # ---- prompt guidance present ----
    def test_prompt_mentions_manifest_and_trigger(self):
        m = self.app.send('bot-main', {'text': 'x'}, 'pg')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        system = self._system(turn)
        self.assertIn('evidence_manifest', system)
        self.assertIn('effective_timezone', system)


if __name__ == '__main__':
    unittest.main()
