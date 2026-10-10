import json
import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.runtime import Runtime
from pangpang.planner_prompt import PLANNER_SYSTEM, detect_language, response_language_block


class PlannerPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close(); self.tmp.cleanup()

    # The English canonical prompt must still carry every load-bearing safety clause
    # (these are the invariants the former zh prompt enforced; a translation that
    # dropped one would silently weaken the agent).
    def test_english_canonical_has_safety_clauses(self):
        for probe in ("never fake completion",
                      "authority=delegator",
                      "never expands permissions",
                      "untrusted_reference_state",
                      "Real permissions are guaranteed by server-side deterministic checks",
                      "packet.now"):
            self.assertIn(probe, PLANNER_SYSTEM, probe)
        self.assertTrue(PLANNER_SYSTEM.startswith("You are a long-running staff Bot of PangPang"))
        # Canonical prompt is English: no CJK should remain in it.
        self.assertFalse(any('一' <= c <= '鿿' for c in PLANNER_SYSTEM))

    def test_detect_language(self):
        self.assertEqual(detect_language('帮我查一下阿里巴巴的股价'), 'zh')
        self.assertEqual(detect_language('please fetch the latest quote'), 'en')
        self.assertEqual(detect_language(''), 'en') # empty -> default en
        self.assertEqual(detect_language(None), 'en') # None tolerated
        # A CJK-dominant line with a few latin tokens still reads as zh.
        self.assertEqual(detect_language('用 SAP BTP 查一下'), 'zh')

    def test_response_language_block_marks_detected_language(self):
        zh = response_language_block('zh')
        self.assertIn('detected: zh', zh)
        self.assertIn('Never translate code', zh)
        self.assertIn('presentation only', zh)
        self.assertIn('detected: en', response_language_block('en'))

    # Integration: the system message is a stable English prefix + a language block
    # keyed off the latest user message. The cacheable prefix must not change with
    # the user's language (prompt-cache guard).
    def test_projection_injects_language_block_by_message(self):
        r = Runtime(self.app, _Stub())
        for text, want in (('帮我做一个表格', 'detected: zh'), ('build me a table', 'detected: en')):
            m = self.app.send('bot-main', {'text': text}, 'k-' + want)
            turn = self.app.store.get('turn', m['turn_id'])
            system = r.context(turn)[0]
            self.assertEqual(system['role'], 'system')
            self.assertTrue(system['content'].startswith(PLANNER_SYSTEM[:60]))
            self.assertIn(want, system['content'])

    def test_projection_prefix_is_language_independent(self):
        r = Runtime(self.app, _Stub())
        prefixes = []
        for text in ('继续处理这件事', 'carry on with this'):
            m = self.app.send('bot-main', {'text': text}, 'p-' + text[:4])
            turn = self.app.store.get('turn', m['turn_id'])
            system = r.context(turn)[0]['content']
            # Everything up to the response-language block is the cacheable prefix.
            prefixes.append(system.split('## Response language', 1)[0])
        self.assertEqual(prefixes[0], prefixes[1])


class _Stub:
    def complete(self, profile, messages, tools):
        return {'content': 'ok'}, {}


if __name__ == '__main__':
    unittest.main()
