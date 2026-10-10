import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class RoutineAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_model_created_routine_is_pending_and_never_fires(self):
        # R6: a routine created on a model turn (even one carrying a user message,
        # as a prompt-injection would) starts pending+inactive and does NOT fire.
        turn = self._user_turn()
        r = self.app.create_routine('bot-main', {'instruction': '定时写文件', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}}, turn=turn)
        routine = self.app.store.get('routine', r['id'])
        self.assertEqual(routine['approval_state'], 'pending')
        self.assertFalse(routine['active'])
        self.assertIsNone(routine['next_run_at'])
        # manual run is refused while pending
        with self.assertRaises(Fault) as cm:
            self.app.run_routine_now('bot-main', r['id']) # operator path, but still pending
        self.assertEqual(cm.exception.code, 'routine_not_approved')
        # a due-but-pending routine never fires via routine_step
        routine['active'] = True; routine['next_run_at'] = 1.0
        self.app.store.put('routine', routine)
        self.app.routine_step()
        self.assertFalse(any(w.get('routine_id') == r['id'] for w in self.app.store.find('work', limit=100)))

    def test_approve_arms_the_exact_version(self):
        # R6: operator approval arms the routine; a digest mismatch (edited since
        # review) is refused.
        turn = self._user_turn()
        r = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}}, turn=turn)
        with self.assertRaises(Fault) as cm:
            self.app.approve_routine('bot-main', r['id'], expected_digest='stale')
        self.assertEqual(cm.exception.code, 'routine_changed_since_review')
        approved = self.app.approve_routine('bot-main', r['id'], expected_digest=self.app.routine_review(r)['approval_digest'])
        self.assertEqual(approved['approval_state'], 'approved')
        self.assertTrue(approved['active'])
        self.assertIsNotNone(approved['next_run_at'])

    def test_model_edit_of_approved_routine_re_gates(self):
        # R6: after approval, a model turn editing the instruction re-gates to pending
        # and disarms until re-approved.
        turn = self._user_turn()
        r = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}}, turn=turn)
        self.app.approve_routine('bot-main', r['id'], expected_digest=self.app.routine_review(r)['approval_digest'])
        rev = self.app.store.get('routine', r['id'])['revision']
        self.app.update_routine('bot-main', r['id'], {'instruction': '偷偷写文件', 'revision': rev}, turn=self._user_turn())
        routine = self.app.store.get('routine', r['id'])
        self.assertEqual(routine['approval_state'], 'pending')
        self.assertFalse(routine['active'])

    def _user_turn(self, bot_id='bot-main'):
        # a real user-message turn (kind=user, from=user)
        m = self.app.send(bot_id, {'text': '每天研究新闻'}, 'k' + uid_suffix())
        return self.app.store.find('turn', work_id=m['work_id'])[0]

    def test_create_on_user_turn_ok(self):
        turn = self._user_turn()
        r = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}}, turn=turn)
        self.assertIn('id', r)

    def test_create_without_turn_ok_operator(self):
        # turn=None = operator/launch path, authorized by its own session
        r = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}})
        self.assertIn('id', r)

    def test_create_on_non_user_turn_rejected(self):
        # a turn whose triggering message is NOT a direct user message (simulate a
        # handoff/autonomous turn) must be refused.
        m = self.app.send('bot-main', {'text': 'x'}, 'base')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        # mutate the trigger message to a non-user kind (as a delegate/handoff would be)
        msg = self.app.store.get('message', turn['message_id'])
        msg['kind'] = 'handoff'; msg['from'] = 'bot-main'
        self.app.store.put('message', msg)
        with self.assertRaises(Fault) as cm:
            self.app.create_routine('bot-main', {'instruction': 'x', 'timezone': 'Asia/Shanghai',
                'schedule': {'type': 'daily', 'time': '09:00'}}, turn=turn)
        self.assertEqual(cm.exception.code, 'routine_requires_user_authorization')

    def test_routine_fire_grants_write(self):
        #: write authority is the operator's explicit envelope, never implied; with
        # it, a fired routine's write task runs (queued) instead of awaiting approval.
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'windows-job-object'})
        r = self.app.create_routine('bot-main', {'instruction': '写份简报', 'authority': {'grant_write': True},
            'timezone': 'Asia/Shanghai', 'schedule': {'type': 'daily', 'time': '09:00'}})
        run = self.app.run_routine_now('bot-main', r['id'])
        rwork = next(w for w in self.app.store.find('work', limit=100)
                     if w.get('kind') == 'routine' and w.get('routine_id') == r['id'])
        self.assertTrue(rwork['grant_write'])
        # a write task submitted for this work is queued, not awaiting_approval
        res = self.app.submit_task(rwork['owner_bot_id'], rwork['id'], {'prompt': 'x'})
        self.assertEqual(self.app.store.get('task', res['task_id'])['status'], 'queued')

    def _non_user_turn(self):
        # a turn whose trigger message is NOT a direct user message (like a handoff)
        m = self.app.send('bot-main', {'text': 'x'}, 'nu' + uid_suffix())
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        msg = self.app.store.get('message', turn['message_id'])
        msg['kind'] = 'handoff'; msg['from'] = 'bot-main'
        self.app.store.put('message', msg)
        return turn

    def test_run_now_on_non_user_turn_rejected(self):
        # A bot autonomously calling run_routine_now would materialize a grant_write
        # Work = authority expansion. Must require a direct user turn.
        r = self.app.create_routine('bot-main', {'instruction': 'x', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}})
        with self.assertRaises(Fault) as cm:
            self.app.run_routine_now('bot-main', r['id'], turn=self._non_user_turn())
        self.assertEqual(cm.exception.code, 'routine_requires_user_authorization')

    def test_resume_on_non_user_turn_rejected_but_pause_ok(self):
        r = self.app.create_routine('bot-main', {'instruction': 'x', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}})
        # pause (shrinks authority) is fine even from a non-user turn
        self.app.pause_routine('bot-main', r['id'], False, turn=self._non_user_turn())
        # resume (re-arms standing write authority) from a non-user turn is refused
        with self.assertRaises(Fault) as cm:
            self.app.pause_routine('bot-main', r['id'], True, turn=self._non_user_turn())
        self.assertEqual(cm.exception.code, 'routine_requires_user_authorization')

    def test_run_now_operator_path_allowed(self):
        # turn=None (operator HTTP / direct service call) is allowed.
        r = self.app.create_routine('bot-main', {'instruction': 'x', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}})
        run = self.app.run_routine_now('bot-main', r['id']) # no turn = operator
        self.assertIsNotNone(run)

    def test_fired_work_freezes_scheduled_for(self):
        # THIS fire's scheduled_for is frozen on the Work, so context reads the
        # correct trigger time even after routine.next_run_at advances to the next fire.
        r = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}})
        fire_at = 1700000000.0
        run = self.app._materialize_routine(self.app.store.get('routine', r['id']), fire_at, 'scheduled')
        work = self.app.store.get('work', run['work_id'])
        self.assertEqual(work.get('scheduled_for'), fire_at)
        # advancing next_run_at (as routine_step does) must NOT change the Work's frozen fact
        routine = self.app.store.get('routine', r['id'])
        routine['next_run_at'] = fire_at + 86400
        self.app.store.put('routine', routine)
        self.assertEqual(self.app.store.get('work', run['work_id']).get('scheduled_for'), fire_at)

    def test_manual_run_reports_backpressure_not_phantom(self):
        # a manual run that hits backpressure must raise (no automatic retry),
        # and must NOT stamp last_manual_run_at (a phantom "ran" record).
        r = self.app.create_routine('bot-main', {'instruction': 'x', 'timezone': 'Asia/Shanghai',
            'schedule': {'type': 'daily', 'time': '09:00'}})
        # force backpressure
        orig = self.app._check_backpressure
        def boom(_bot):
            raise Fault('backpressure', 429)
        self.app._check_backpressure = boom
        try:
            with self.assertRaises(Fault) as cm:
                self.app.run_routine_now('bot-main', r['id'])
            self.assertEqual(cm.exception.code, 'backpressure')
        finally:
            self.app._check_backpressure = orig
        self.assertIsNone(self.app.store.get('routine', r['id']).get('last_manual_run_at'))


_c = [0]
def uid_suffix():
    _c[0] += 1
    return str(_c[0])


if __name__ == '__main__':
    unittest.main()
