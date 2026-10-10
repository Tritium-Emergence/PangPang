import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _tune(self, **kw):
        self.app.config['scheduler'] = kw
        self.app.scheduler_config = self.app._scheduler_config()

    def test_config_positive_validation(self):
        self._make = lambda: None
        for bad in ({'max_delegations_per_root': 0}, {'max_model_turns_per_root': -1},
                    {'max_outstanding_dependencies_per_work': 0}, {'max_delegation_depth': 0}):
            self.app.config['scheduler'] = bad
            with self.assertRaises(Fault):
                self.app._scheduler_config()

    def test_delegation_depth_cap(self):
        self._tune(max_delegation_depth=2)
        b = self.app.create_bot({'name': 'A', 'role': 'r'})
        m = self.app.send('bot-main', {'text': 'x'}, 'd')
        root = self.app.store.get('turn', m['turn_id'])['trigger_root_request_id']
        body = {'target_bot_id': b['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']}
        # depth 1 (work.depth 0 -> child 1) ok
        h1 = self.app.delegate('bot-main', m['work_id'], body, root_request_id=root)
        # depth 2 from the child ok
        b2 = self.app.create_bot({'name': 'B', 'role': 'r'})
        h2 = self.app.delegate(b['id'], h1['work_id'],
                               {'target_bot_id': b2['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']},
                               root_request_id=root)
        # depth 3 exceeds cap=2
        b3 = self.app.create_bot({'name': 'C', 'role': 'r'})
        with self.assertRaises(Fault) as cm:
            self.app.delegate(b2['id'], h2['work_id'],
                              {'target_bot_id': b3['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']},
                              root_request_id=root)
        self.assertEqual(cm.exception.code, 'delegation_depth')
        self.assertIs(cm.exception.retryable_in_same_turn, False)

    def test_delegations_per_root_cap(self):
        self._tune(max_delegations_per_root=2)
        b = self.app.create_bot({'name': 'A', 'role': 'r'})
        m = self.app.send('bot-main', {'text': 'x'}, 'dr')
        root = self.app.store.get('turn', m['turn_id'])['trigger_root_request_id']
        body = {'target_bot_id': b['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']}
        self.app.delegate('bot-main', m['work_id'], body, root_request_id=root)
        self.app.delegate('bot-main', m['work_id'], body, root_request_id=root)
        with self.assertRaises(Fault) as cm:
            self.app.delegate('bot-main', m['work_id'], body, root_request_id=root)
        self.assertEqual(cm.exception.code, 'root_delegation_budget')

    def test_outstanding_dependencies_cap(self):
        self._tune(max_outstanding_dependencies_per_work=2, max_delegations_per_root=100)
        b = self.app.create_bot({'name': 'A', 'role': 'r'})
        m = self.app.send('bot-main', {'text': 'x'}, 'od')
        root = self.app.store.get('turn', m['turn_id'])['trigger_root_request_id']
        body = {'target_bot_id': b['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']}
        self.app.delegate('bot-main', m['work_id'], body, root_request_id=root)
        self.app.delegate('bot-main', m['work_id'], body, root_request_id=root)
        with self.assertRaises(Fault) as cm:
            self.app.delegate('bot-main', m['work_id'], body, root_request_id=root)
        self.assertEqual(cm.exception.code, 'outstanding_dependencies')

    def test_model_turn_budget_blocks_toplevel(self):
        # A top-level work whose root model-turn budget is exhausted blocks (does
        # not fail); the settlement/result stays durable.
        self._tune(max_model_turns_per_root=1)
        m = self.app.send('bot-main', {'text': 'x'}, 'mt')
        # the initial user turn already consumed the only model-turn budget slot.
        root = self.app.store.get('turn', m['turn_id'])['trigger_root_request_id']
        self.assertEqual(self.app.store.get('root_request', root)['model_turn_count'], 1)
        # a second wakeable message on the same root/work cannot get a new turn
        wid = m['work_id']
        # consume the first turn
        for t in self.app.store.find('turn', work_id=wid):
            if t['status'] == 'queued':
                t['status'] = 'done'
                self.app.store.put('turn', t)
        # append another user message on the same root and try to schedule
        msg = self.app._stamp_message({'id': 'msg-x', 'bot_id': 'bot-main', 'work_id': wid,
            'kind': 'user', 'from': 'user', 'content': '再来', 'created': 1.0}, root_request_id=root)
        self.app.store.put('message', msg)
        self.app.ensure_work_turn(wid)
        self.assertEqual(self.app.store.get('work', wid)['status'], 'blocked')
        self.assertEqual(self.app.store.get('work', wid)['error'], 'root_turn_budget')

    def test_coalescing_counts_model_turn_once(self):
        # Two messages coalescing into one turn charge the root once.
        self._tune(max_model_turns_per_root=32)
        m = self.app.send('bot-main', {'text': 'x'}, 'cc')
        root = self.app.store.get('turn', m['turn_id'])['trigger_root_request_id']
        self.assertEqual(self.app.store.get('root_request', root)['model_turn_count'], 1)

    def test_settlement_exempt_from_backpressure(self):
        # With a tiny global cap, settlement wakeups must still schedule (exempt),
        # while a fresh delegate is rejected.
        self._tune(max_global_runnable_turns=1)
        b = self.app.create_bot({'name': 'A', 'role': 'r'})
        m = self.app.send('bot-main', {'text': 'x'}, 'bp') # 1 queued turn -> at cap
        root = self.app.store.get('turn', m['turn_id'])['trigger_root_request_id']
        with self.assertRaises(Fault) as cm:
            self.app.delegate('bot-main', m['work_id'],
                              {'target_bot_id': b['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']},
                              root_request_id=root)
        self.assertEqual(cm.exception.code, 'backpressure')


    def test_rootless_turn_gets_stable_root(self):
        # A message with no root must mint a real, persisted root on the trigger so
        # repeated scheduling charges the SAME root (not a fresh count-1 each time),
        # otherwise the model-turn cap is trivially evadable.
        self._tune(max_model_turns_per_root=32)
        m = self.app.send('bot-main', {'text': 'x'}, 'rl')
        wid = m['work_id']
        # strip the root off the trigger message to simulate a legacy/backfilled msg
        for t in self.app.store.find('turn', work_id=wid):
            t['status'] = 'done'
            self.app.store.put('turn', t)
        msg = self.app.store.get('message', m['message_id'])
        msg['root_request_id'] = None
        msg['consumed_by_turn_id'] = None
        self.app.store.put('message', msg)
        self.app.ensure_work_turn(wid)
        msg2 = self.app.store.get('message', m['message_id'])
        self.assertIsNotNone(msg2.get('root_request_id')) # a real root was minted + persisted
        turn = [t for t in self.app.store.find('turn', work_id=wid) if t['status'] == 'queued'][0]
        self.assertEqual(turn['accounted_root_request_id'], msg2['root_request_id'])
        self.assertEqual(self.app.store.get('root_request', msg2['root_request_id'])['model_turn_count'], 1)


if __name__ == '__main__':
    unittest.main()
