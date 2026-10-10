import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service


class RecoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _running_turn(self, work_id, history, boot=None):
        turns = self.app.store.find('turn', work_id=work_id)
        t = turns[0]
        t['status'] = 'running'
        t['history'] = history
        t['run_owner_boot_id'] = boot if boot is not None else 'prev-boot'
        return self.app.store.put('turn', t)

    def test_recover_ignores_current_boot_running(self):
        # A running turn owned by the CURRENT boot is held by a live worker and must
        # not be recovered on startup.
        m = self.app.send('bot-main', {'text': 'x'}, 'cb')
        self._running_turn(m['work_id'], [], boot=self.app.boot_id)
        self.app.recover()
        self.assertEqual(self.app.store.get('turn', m['turn_id'])['status'], 'running')

    def test_recover_prev_boot_no_assistant_requeues(self):
        # Case (a): no assistant message yet -> requeue to re-ask the model.
        m = self.app.send('bot-main', {'text': 'x'}, 'na')
        self._running_turn(m['work_id'], [{'role': 'system', 'content': '{}'}], boot='prev')
        self.app.recover()
        turn = self.app.store.get('turn', m['turn_id'])
        self.assertEqual(turn['status'], 'queued')
        self.assertEqual(turn['recovery_count'], 1)

    def test_recover_persisted_final_answer_finishes(self):
        # Case (c): last assistant has no tool_calls -> persisted final answer whose
        # finish_turn was interrupted -> complete WITHOUT re-asking the model.
        m = self.app.send('bot-main', {'text': 'x'}, 'fa')
        self._running_turn(m['work_id'], [
            {'role': 'system', 'content': '{}'},
            {'role': 'assistant', 'content': '最终答案'}], boot='prev')
        self.app.recover()
        turn = self.app.store.get('turn', m['turn_id'])
        self.assertEqual(turn['status'], 'done')
        # a delivery/assistant message with the answer was persisted by finish_turn
        msgs = [x for x in self.app.store.find('message', work_id=m['work_id']) if x.get('content') == '最终答案']
        self.assertTrue(msgs)

    def test_recover_tool_batch_requeues_for_replay(self):
        # Case (b): last assistant has tool_calls (some results missing) -> requeue;
        # run() replays unfinished durable tools by call_id (not regenerated here).
        m = self.app.send('bot-main', {'text': 'x'}, 'tb')
        self._running_turn(m['work_id'], [
            {'role': 'system', 'content': '{}'},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'c1', 'type': 'function', 'function': {'name': 'read_state', 'arguments': '{}'}}]}],
            boot='prev')
        self.app.recover()
        turn = self.app.store.get('turn', m['turn_id'])
        self.assertEqual(turn['status'], 'queued')

    def test_poison_cap_blocks_after_two_recoveries(self):
        m = self.app.send('bot-main', {'text': 'x'}, 'pc')
        self.app.config['recovery_poison_cap'] = 2
        # first two recoveries requeue, third blocks
        for expected in ('queued', 'queued'):
            self._running_turn(m['work_id'], [{'role': 'system', 'content': '{}'}], boot='prev')
            self.app.recover()
            self.assertEqual(self.app.store.get('turn', m['turn_id'])['status'], expected)
        self._running_turn(m['work_id'], [{'role': 'system', 'content': '{}'}], boot='prev')
        self.app.recover()
        turn = self.app.store.get('turn', m['turn_id'])
        self.assertEqual(turn['status'], 'blocked')
        self.assertEqual(turn['error'], 'recovery_poison_cap')

    def test_prior_turn_assistant_not_mistaken_for_final_answer(self):
        # A crash BEFORE this turn produced its own reply must NOT be "finished"
        # with a stale prior-turn assistant reply carried in the continuity context.
        m = self.app.send('bot-main', {'text': '新请求'}, 'stale')
        # history = prior assistant (continuity) THEN this turn's user message, and
        # NO current-turn assistant yet -> must requeue to re-ask, not finish.
        self._running_turn(m['work_id'], [
            {'role': 'system', 'content': '{}'},
            {'role': 'assistant', 'content': '旧答案'},
            {'role': 'user', 'content': '新请求'}], boot='prev')
        self.app.recover()
        turn = self.app.store.get('turn', m['turn_id'])
        self.assertEqual(turn['status'], 'queued')
        # the stale answer was not delivered
        self.assertFalse([x for x in self.app.store.find('message', work_id=m['work_id']) if x.get('content') == '旧答案'])


if __name__ == '__main__':
    unittest.main()
