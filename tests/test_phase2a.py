import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service, message_inbox_meta


class Phase2aTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_message_inbox_classifier(self):
        self.assertEqual(message_inbox_meta('assistant'), (False, 'none'))
        self.assertEqual(message_inbox_meta('peer_out'), (False, 'none'))
        self.assertEqual(message_inbox_meta('delivery'), (False, 'none'))
        self.assertEqual(message_inbox_meta('user'), (True, 'user'))
        self.assertEqual(message_inbox_meta('handoff'), (True, 'user'))
        self.assertEqual(message_inbox_meta('routine'), (True, 'user'))
        self.assertEqual(message_inbox_meta('execution_result'), (True, 'dependency_result'))
        self.assertEqual(message_inbox_meta('peer_result'), (True, 'dependency_result'))
        self.assertEqual(message_inbox_meta('peer', fyi=True), (True, 'fyi'))
        self.assertEqual(message_inbox_meta('peer'), (True, 'dependency_result'))

    def test_user_message_creates_root_and_stamps(self):
        m = self.app.send('bot-main', {'text': '干活'}, 'r1')
        msg = self.app.store.get('message', m['message_id'])
        self.assertIn('root_request_id', msg)
        self.assertTrue(msg['root_request_id'].startswith('root-'))
        self.assertEqual(msg['inbox_eligible'], True)
        self.assertEqual(msg['wake_intent'], 'user')
        self.assertIn('sequence', msg)
        root = self.app.store.get('root_request', msg['root_request_id'])
        self.assertEqual(root['source'], 'user')
        turn = self.app.store.get('turn', m['turn_id'])
        self.assertEqual(turn['message_ids'], [m['message_id']])
        self.assertEqual(turn['trigger_root_request_id'], msg['root_request_id'])
        self.assertEqual(turn['mode'], 'normal')

    def test_distinct_user_messages_get_distinct_roots(self):
        a = self.app.send('bot-main', {'text': '一'}, 'ra')
        b = self.app.send('bot-main', {'text': '二'}, 'rb')
        ra = self.app.store.get('message', a['message_id'])['root_request_id']
        rb = self.app.store.get('message', b['message_id'])['root_request_id']
        self.assertNotEqual(ra, rb)

    def test_sequence_is_monotonic(self):
        a = self.app.send('bot-main', {'text': '一'}, 'sa')
        b = self.app.send('bot-main', {'text': '二'}, 'sb')
        sa = self.app.store.get('message', a['message_id'])['sequence']
        sb = self.app.store.get('message', b['message_id'])['sequence']
        self.assertLess(sa, sb)

    def test_delegate_dependency_inherits_turn_root(self):
        from pangpang.runtime import Runtime
        editor = self.app.create_bot({'name': '编辑', 'role': '编辑'})
        src = self.app.send('bot-main', {'text': '活'}, 'dg')
        turn = self.app.store.get('turn', src['turn_id'])
        root = turn['trigger_root_request_id']
        # call delegate the way invoke does, threading the turn root
        self.app.delegate('bot-main', src['work_id'],
                          {'target_bot_id': editor['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']},
                          root_request_id=root)
        dep = self.app.store.find('dependency', source_work_id=src['work_id'])[0]
        self.assertEqual(dep['root_request_id'], root)

    def test_backfill_inbox_metadata_idempotent(self):
        m = self.app.send('bot-main', {'text': '活'}, 'bf')
        # simulate a pre-Phase-2 message/turn by stripping new fields
        msg = self.app.store.get('message', m['message_id'])
        for f in ('inbox_eligible', 'wake_intent', 'sequence'):
            msg.pop(f, None)
        self.app.store.put('message', msg)
        turn = self.app.store.get('turn', m['turn_id'])
        turn.pop('message_ids', None)
        self.app.store.put('turn', turn)
        self.app._backfill_inbox_metadata()
        msg2 = self.app.store.get('message', m['message_id'])
        self.assertIn('inbox_eligible', msg2)
        self.assertIn('sequence', msg2)
        turn2 = self.app.store.get('turn', m['turn_id'])
        self.assertEqual(turn2['message_ids'], [m['message_id']])
        # idempotent second run
        self.app._backfill_inbox_metadata()
        self.assertEqual(self.app.store.get('turn', m['turn_id'])['message_ids'], [m['message_id']])


if __name__ == '__main__':
    unittest.main()
