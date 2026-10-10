import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class RecoveryHandoffTests(unittest.TestCase):
    """turn_budget block on a top-level user turn -> transparent handoff to executor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')
        self.app.probe('codex-local') # make a local executor routable

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _budget_blocked_user_turn(self, text='那个 51GB 文件在哪个目录'):
        m = self.app.send('bot-main', {'text': text}, 'k' + uid())
        turn = self.app.store.get('turn', m['turn_id'])
        turn.update(status='blocked', error='turn_budget')
        self.app.store.put('turn', turn)
        return turn

    def test_toplevel_budget_block_hands_off_to_executor(self):
        turn = self._budget_blocked_user_turn()
        task_id = self.app.try_recovery_handoff(turn)
        self.assertIsNotNone(task_id)
        task = self.app.store.get('task', task_id)
        # handoff carries the original question and lands as a real executor task
        self.assertIn('51GB', task['user_prompt'])
        self.assertEqual(task['work_id'], turn['work_id'])
        # turn stamped so it is not handed off twice
        self.assertEqual(self.app.store.get('turn', turn['id'])['recovery_dispatched'], task_id)

    def test_handoff_is_idempotent(self):
        turn = self._budget_blocked_user_turn()
        first = self.app.try_recovery_handoff(turn)
        turn = self.app.store.get('turn', turn['id'])
        second = self.app.try_recovery_handoff(turn)
        self.assertIsNotNone(first)
        self.assertIsNone(second) # already dispatched -> no second task

    def test_only_turn_budget_triggers(self):
        m = self.app.send('bot-main', {'text': 'x'}, 'k' + uid())
        turn = self.app.store.get('turn', m['turn_id'])
        turn.update(status='blocked', error='model_request_failed')
        self.app.store.put('turn', turn)
        self.assertIsNone(self.app.try_recovery_handoff(turn))

    def test_child_work_turn_not_handed_off(self):
        # a delegated child work turn settles via fail_turn_dependency, not a fresh task
        m = self.app.send('bot-main', {'text': 'x'}, 'k' + uid())
        turn = self.app.store.get('turn', m['turn_id'])
        work = self.app.store.get('work', turn['work_id'])
        work['parent_work_id'] = 'work-fake-parent'
        self.app.store.put('work', work)
        turn.update(status='blocked', error='turn_budget')
        self.app.store.put('turn', turn)
        self.assertIsNone(self.app.try_recovery_handoff(turn))

    def test_handoff_failure_does_not_raise(self):
        # no eligible executor -> handoff returns None, never raises (must not break
        # the block settlement that runs after it in the scheduler)
        turn = self._budget_blocked_user_turn()
        # disable every installation so route finds nothing
        for inst in self.app.execution.installations.values():
            inst['enabled'] = False
        result = self.app.try_recovery_handoff(turn)
        self.assertIsNone(result)
        # stamped as attempted (False) so it will not retry forever
        self.assertEqual(self.app.store.get('turn', turn['id'])['recovery_dispatched'], False)

    def test_attempted_failure_not_retried(self):
        # once a handoff attempt failed (recovery_dispatched present=False), a later
        # call must NOT dispatch even after executors come back — present-not-truthy.
        turn = self._budget_blocked_user_turn()
        for inst in self.app.execution.installations.values():
            inst['enabled'] = False
        self.assertIsNone(self.app.try_recovery_handoff(turn))
        for inst in self.app.execution.installations.values():
            inst['enabled'] = True
        turn = self.app.store.get('turn', turn['id'])
        self.assertIsNone(self.app.try_recovery_handoff(turn)) # attempted -> no retry

    def test_semantic_barrier_turn_not_handed_off(self):
        # a turn frozen by a semantic barrier must not be laundered into a dispatch
        # by exhausting its budget: the barrier outranks the budget.
        turn = self._budget_blocked_user_turn()
        turn['semantic_gate_barrier'] = True
        self.app.store.put('turn', turn)
        self.assertIsNone(self.app.try_recovery_handoff(turn))

    def test_legacy_history_barrier_not_handed_off(self):
        # a barrier that only lives in a history tool-result (older turns,
        # no top-level bit) must also block handoff — same test Runtime uses.
        import json
        turn = self._budget_blocked_user_turn()
        turn['history'] = [{'role': 'tool', 'content': json.dumps({'semantic_gate_barrier': True})}]
        self.app.store.put('turn', turn)
        self.assertIsNone(self.app.try_recovery_handoff(turn))

    def test_ordinary_tool_history_allows_handoff(self):
        # a normal tool-result history (no barrier) must NOT block handoff —
        # guards against the json/NameError regression that failed closed on all tools.
        import json
        turn = self._budget_blocked_user_turn()
        turn['history'] = [{'role': 'tool', 'content': json.dumps({'result': 'ok', 'data': 123})}]
        self.app.store.put('turn', turn)
        task_id = self.app.try_recovery_handoff(turn)
        self.assertIsNotNone(task_id) # ordinary tool history is not a barrier

    def test_coalesced_messages_all_carried(self):
        # a coalesced turn consuming several user messages must carry them all (and
        # their constraints), not just the trigger message.
        m1 = self.app.send('bot-main', {'text': '找到 report.csv，不要修改任何文件'}, 'k' + uid())
        turn = self.app.store.get('turn', m1['turn_id'])
        # simulate a second user message coalesced into the same turn
        m2 = self.app.store.put('message', {'id': 'msg-second-' + uid(), 'work_id': turn['work_id'],
            'from': 'user', 'kind': 'user', 'content': '再告诉我它的大小', 'created': 1.0})
        turn['message_ids'] = [turn['message_id'], m2['id']]
        turn.update(status='blocked', error='turn_budget')
        self.app.store.put('turn', turn)
        task_id = self.app.try_recovery_handoff(turn)
        task = self.app.store.get('task', task_id)
        self.assertIn('report.csv', task['user_prompt'])
        self.assertIn('不要修改', task['user_prompt']) # constraint kept
        self.assertIn('大小', task['user_prompt']) # second message kept

    def test_handoff_inherits_authorization_host_constraint(self):
        # a fresh budget must not relax authority: an origin turn bound to host A must
        # not let its handoff run on host B.
        turn = self._budget_blocked_user_turn()
        turn['authorization'] = {'grant_write': True, 'required_installation_id': 'inst-does-not-exist'}
        self.app.store.put('turn', turn)
        # route can find codex-local, but the inherited constraint pins a missing
        # installation -> no eligible executor -> no silent dispatch on the wrong one.
        result = self.app.try_recovery_handoff(turn)
        self.assertIsNone(result)

    def test_failed_submit_leaves_no_orphan_task(self):
        # submit_task writes task then dependency in one atomic; if the
        # dependency cap raises, the whole submit_task rolls back. Because we call it
        # OUTSIDE our claim txn, no orphan queued task without a dependency survives.
        turn = self._budget_blocked_user_turn()
        before = len(self.app.store.find('task', limit=1000))
        # force submit_task to fail after it would write the task: cap dependencies at 0
        self.app.scheduler_config['max_outstanding_dependencies_per_work'] = 0
        result = self.app.try_recovery_handoff(turn)
        self.assertIsNone(result)
        after = self.app.store.find('task', limit=1000)
        self.assertEqual(len(after), before) # no orphan task left behind
        self.assertEqual(self.app.store.get('turn', turn['id'])['recovery_dispatched'], False)

    def test_recovery_root_persists_inherited_authorization(self):
        # the fresh root must carry the inherited authorization, so the turn
        # woken by the handoff's result re-reads the SAME limits (not a widened one).
        turn = self._budget_blocked_user_turn()
        turn['authorization'] = {'grant_write': False}
        self.app.store.put('turn', turn)
        task_id = self.app.try_recovery_handoff(turn)
        self.assertIsNotNone(task_id)
        root_id = self.app.store.get('turn', turn['id'])['recovery_root_id']
        root = self.app.store.get('root_request', root_id)
        self.assertFalse(root['authorization']['grant_write']) # not widened to True


_c = [0]
def uid():
    _c[0] += 1
    return str(_c[0])


if __name__ == '__main__':
    unittest.main()
