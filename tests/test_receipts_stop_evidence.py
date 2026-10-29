"""Lost receipts, cancellation intent, and fail-closed stop evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang import runner
from pangpang.service import Service
from pangpang.store import Fault


class LostReceiptTests(unittest.TestCase):
    GRACE_S = 5
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        workspace = self.root / 'workspace'
        workspace.mkdir()
        self.app = Service(config(workspace), self.root / 'data')
        self.app.store.put('probe', {'id': 'codex-local', 'available': True,
                                   'supervision': 'linux-cgroup-cooperative'})
        msg = self.app.send('bot-main', {'text': 'research', 'allow_write': True}, 'i-stop-evidence')
        result = self.app.submit_task('bot-main', msg['work_id'], {'prompt': 'research'})
        self.task = self.app.store.get('task', result['task_id'])
        self.task.update(status='unknown', error='execution_state_unknown', started=1,
                         attempt_uuid='attempt-stop-evidence', supervision_floor='linux-cgroup-cooperative')
        self.app.store.put('task', self.task)
        self.directory = Path(self.app.execution.directory(self.app.execution.hosts['local'], self.task['execution_id']))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cgroup = self.root / 'cgroup'
        self.cgroup.mkdir()
        (self.cgroup / 'cgroup.controllers').write_text('cpu pids')
        unit = 'xpp-' + self.task['execution_id'] + '.scope'
        self.group = self.cgroup / 'user.slice' / unit
        self.group.mkdir(parents=True)
        (self.group / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        self.started = dict(execution_id=self.task['execution_id'], spec_digest=self.task['spec_digest'],
                            attempt_uuid=self.task['attempt_uuid'], launcher_pid=222222,
                            supervision='linux-cgroup-cooperative', unit=unit, control_group=str(self.group))
        self.supervisor = dict(pid=111111, execution_id=self.task['execution_id'], attempt_uuid=self.task['attempt_uuid'])
        self.write_witnesses()
        (self.directory / 'output.log').write_text('partial side effect may have happened')
        self.clock = patch('pangpang.service.time.time', return_value=10000)
        self.time = self.clock.start()
        self.cg = patch('pangpang.linux_job.CGROUP_ROOT', str(self.cgroup)); self.cg.start()
        self.os = patch('pangpang.service.platform.system', return_value='Linux'); self.os.start()
        self.alive = patch('pangpang.runner.process_alive', return_value=False); self.live = self.alive.start()

    def tearDown(self):
        self.alive.stop(); self.os.stop(); self.cg.stop(); self.clock.stop()
        self.app.store.close(); self.tmp.cleanup()

    def write_witnesses(self):
        (self.directory / 'started.json').write_text(json.dumps(self.started))
        (self.directory / 'supervisor.json').write_text(json.dumps(self.supervisor))

    def observe(self):
        return self.app._reconcile_active_task(self.app.store.get('task', self.task['id']))

    def age(self):
        self.time.return_value = 10000 + self.GRACE_S

    def assert_held(self):
        self.assertEqual(self.app.store.get('task', self.task['id'])['status'], 'unknown')
        self.assertEqual(self.app.store.get('dependency', self.task['dependency_id'])['status'], 'pending')

    def test_grace_then_failed_single_settlement_retains_scene_releases_workspace(self):
        candidate = dict(self.task, id='candidate', status='queued', execution_id='exec-' + 'f' * 32)
        self.assertFalse(self.app._task_admissible(candidate))
        self.observe(); self.assert_held()
        self.age()
        with patch.object(self.app.execution, 'start', side_effect=AssertionError('must not relaunch')):
            self.observe()
        current = self.app.store.get('task', self.task['id'])
        self.assertEqual(current['status'], 'failed')
        self.assertEqual(current['receipt']['error'], 'execution_vanished')
        self.assertTrue(current['receipt']['side_effects_unknown'])
        dep = self.app.store.get('dependency', self.task['dependency_id'])
        self.assertEqual(dep['status'], 'settled')
        self.assertEqual(dep['outcome'], 'failed')
        self.assertTrue(dep['result_payload']['side_effects_unknown'])
        self.assertIn('不得自动重试', dep['result_payload']['notice'])
        self.assertTrue(self.app._task_admissible(candidate))
        self.assertTrue((self.directory / 'output.log').exists())
        messages = self.app.store.count('message')
        self.app.reconcile(self.task['id'])
        self.assertEqual(self.app.store.count('message'), messages)

    def test_work_cancel_proven_dead_finishes_cancelled_without_grace(self):
        # The fixture already proves the tree dead (empty scope, both pids gone)
        # and there is no receipt. User cancel finishes immediately as cancelled.
        # The clock is NOT advanced: this is not a timeout.
        self.app.cancel_work(self.task['work_id'])
        current = self.app.store.get('task', self.task['id'])
        self.assertEqual(current['status'], 'cancelled')
        self.assertEqual(current['receipt']['error'], 'execution_vanished')
        self.assertTrue(current['receipt']['side_effects_unknown'])
        self.assertEqual(self.app.store.get('work', self.task['work_id'])['status'], 'cancelled')
        self.assertEqual(self.app.store.count('turn', work_id=self.task['work_id'], status='queued'), 0)
        dep = self.app.store.get('dependency', self.task['dependency_id'])
        self.assertEqual(dep['status'], 'settled')
        self.assertEqual(dep['outcome'], 'cancelled')

    def _plant_unknown_on(self, bot_id, work_id, *, populated, tag):
        res = self.app.submit_task(bot_id, work_id, {'prompt': 'child ' + tag})
        task = self.app.store.get('task', res['task_id'])
        task.update(status='unknown', error='execution_state_unknown', started=1,
                    attempt_uuid='attempt-' + tag, supervision_floor='linux-cgroup-cooperative')
        self.app.store.put('task', task)
        directory = Path(self.app.execution.directory(
            self.app.execution.hosts['local'], task['execution_id']))
        directory.mkdir(parents=True, exist_ok=True)
        unit = 'xpp-' + task['execution_id'] + '.scope'
        group = self.cgroup / 'user.slice' / unit
        group.mkdir(parents=True)
        (group / 'cgroup.events').write_text('populated %d\nfrozen 0\n' % populated)
        started = dict(execution_id=task['execution_id'], spec_digest=task['spec_digest'],
                       attempt_uuid=task['attempt_uuid'], launcher_pid=400000 + populated,
                       supervision='linux-cgroup-cooperative', unit=unit, control_group=str(group))
        supervisor = dict(pid=300000 + populated, execution_id=task['execution_id'],
                          attempt_uuid=task['attempt_uuid'])
        (directory / 'started.json').write_text(json.dumps(started))
        (directory / 'supervisor.json').write_text(json.dumps(supervisor))
        return task

    def test_parent_cancel_settles_proven_child_and_holds_unproven_child(self):
        # Recursive cancel marks a delegated child's unknown task cancel_requested.
        # The same immediate death proof must finish a proven-dead child and must
        # leave an unproven child unknown. The clock is not advanced.
        editor = self.app.create_bot({'name': '编辑', 'role': '编辑'})
        parent_id = self.task['work_id']
        dead = self.app.delegate('bot-main', parent_id, {
            'target_bot_id': editor['id'], 'goal': 'dead', 'request': 'r', 'acceptance': ['a']})
        live = self.app.delegate('bot-main', parent_id, {
            'target_bot_id': editor['id'], 'goal': 'live', 'request': 'r', 'acceptance': ['a']})
        dead_work = self.app.store.get('turn', dead['turn_id'])['work_id']
        live_work = self.app.store.get('turn', live['turn_id'])['work_id']
        proven = self._plant_unknown_on(editor['id'], dead_work, populated=0, tag='dead')
        unproven = self._plant_unknown_on(editor['id'], live_work, populated=1, tag='live')
        self.app.cancel_work(parent_id)
        settled = self.app.store.get('task', proven['id'])
        held = self.app.store.get('task', unproven['id'])
        self.assertEqual(settled['status'], 'cancelled')
        self.assertEqual(settled['receipt']['error'], 'execution_vanished')
        self.assertTrue(settled['receipt']['side_effects_unknown'])
        self.assertEqual(held['status'], 'unknown')
        self.assertTrue(held.get('cancel_requested'))
        self.assertEqual(self.time.return_value, 10000)

    def test_work_cancel_not_proven_dead_leaves_task_unknown(self):
        (self.group / 'cgroup.events').write_text('populated 1\n')
        self.app.cancel_work(self.task['work_id'])
        current = self.app.store.get('task', self.task['id'])
        self.assertEqual(current['status'], 'unknown')
        self.assertTrue(current['cancel_requested'])
        self.assertEqual(self.app.store.get('work', self.task['work_id'])['status'], 'cancelled')
        with patch.object(self.app.execution, 'cancel') as cancel:
            self.observe(); self.observe()
            cancel.assert_called_once()
        self.age(); self.observe()
        self.assertEqual(self.app.store.get('task', self.task['id'])['status'], 'unknown')

    def test_direct_cancel_proven_dead_finishes_cancelled_without_grace(self):
        self.app.cancel(self.task['id'])
        current = self.app.store.get('task', self.task['id'])
        self.assertEqual(current['status'], 'cancelled')
        self.assertEqual(current['receipt']['error'], 'execution_vanished')
        self.assertTrue(current['receipt']['side_effects_unknown'])
        dep = self.app.store.get('dependency', self.task['dependency_id'])
        self.assertEqual(dep['status'], 'settled')
        self.assertEqual(dep['outcome'], 'cancelled')
        # Idempotent: a second cancel does not invent another settlement.
        messages = self.app.store.count('message')
        self.app.cancel(self.task['id'])
        self.assertEqual(self.app.store.get('task', self.task['id'])['status'], 'cancelled')
        self.assertEqual(self.app.store.count('message'), messages)

    def test_direct_cancel_valid_receipt_wins(self):
        receipt = dict(self.started, status='succeeded', process_tree_confirmed=True,
                       termination_evidence='cgroup-v2-populated-zero', artifacts=[])
        (self.directory / 'receipt.json').write_text(json.dumps(receipt))
        self.app.cancel(self.task['id'])
        current = self.app.store.get('task', self.task['id'])
        self.assertEqual(current['status'], 'succeeded')
        self.assertNotEqual(current['receipt'].get('error'), 'execution_vanished')

    def test_direct_task_cancel_unknown_keeps_quarantine_until_proof(self):
        # Process cannot be proven dead: cancel does not finish the task.
        self.live.return_value = True
        with patch.object(self.app.execution, 'cancel') as cancel:
            self.app.cancel(self.task['id'])
            cancel.assert_called_once()
        self.assert_held()
        self.assertTrue(self.app.store.get('task', self.task['id'])['device_cancel_sent'])
        self.age()
        self.app.cancel(self.task['id'])
        self.assert_held()

    def test_cancel_transport_failure_retains_intent_retries(self):
        self.app._cancel_task_local(self.task['id'])
        with patch.object(self.app.execution, 'cancel', side_effect=Fault('transport_unavailable')):
            self.observe()
        self.assert_held()
        self.assertFalse(self.app.store.get('task', self.task['id']).get('device_cancel_sent', False))
        with patch.object(self.app.execution, 'cancel') as cancel:
            self.observe()
            cancel.assert_called_once()

    def test_live_launcher_or_supervisor_cannot_release_empty_scope(self):
        for pid in (111111, 222222):
            with self.subTest(pid=pid):
                self.live.side_effect = lambda p: p == pid
                with patch('pangpang.runner.process_cmdline', return_value=None), patch('pangpang.runner.process_started_at', return_value=None):
                    self.observe(); self.age(); self.observe()
                self.assert_held()
        self.live.side_effect = None

    def test_pid_reuse_not_signalled_but_scope_and_launcher_still_required(self):
        self.live.side_effect = lambda p: p == 111111
        with patch('pangpang.runner.process_cmdline', return_value=['unrelated-process']), patch.object(self.app.execution, 'cancel') as cancel:
            self.observe(); self.age(); self.observe()
            cancel.assert_not_called()
        self.assertEqual(self.app.store.get('task', self.task['id'])['status'], 'failed')

    def test_foreign_missing_weak_witnesses_and_remote_hold(self):
        original = dict(self.started)
        for change in ({'attempt_uuid': 'foreign'}, {'spec_digest': 'foreign'}, {'execution_id': 'foreign'},
                       {'supervision': runner.PGROUP_COOPERATIVE}, {'control_group': '/tmp/other.scope'},
                       {'unit': 'foreign.scope'}, {'launcher_pid': None}):
            with self.subTest(change=change):
                self.started = dict(original, **change); self.write_witnesses()
                self.observe(); self.age(); self.observe(); self.assert_held()
        self.started = original; self.write_witnesses()
        for name in ('started.json', 'supervisor.json'):
            p = self.directory / name; raw = p.read_bytes(); p.unlink()
            self.observe(); self.assert_held(); p.write_bytes(raw)
        self.app.execution.hosts['local']['transport'] = 'ssh'
        with patch.object(self.app.execution, 'poll', return_value={'status': 'unknown'}):
            self.observe(); self.assert_held()

    def test_nonzero_malformed_missing_events_and_observation_error_hold(self):
        for text in ('populated 1', 'populated bogus', 'frozen 0', 'populated 0\npopulated 1'):
            (self.group / 'cgroup.events').write_text(text)
            self.observe(); self.age(); self.observe(); self.assert_held()
        (self.group / 'cgroup.events').unlink()
        self.observe(); self.assert_held()
        with patch('pathlib.Path.read_text', side_effect=PermissionError('no observation')):
            self.observe(); self.assert_held()
        self.live.return_value = None
        self.observe(); self.assert_held()

    def test_absent_scope_only_recovers_while_hierarchy_available(self):
        (self.group / 'cgroup.events').unlink(); self.group.rmdir()
        (self.cgroup / 'cgroup.controllers').unlink()
        self.observe(); self.age(); self.observe(); self.assert_held()
        (self.cgroup / 'cgroup.controllers').write_text('cpu')
        self.observe()
        self.time.return_value += self.GRACE_S
        self.observe()
        self.assertEqual(self.app.store.get('task', self.task['id'])['status'], 'failed')

    def test_external_resources_and_stronger_floor_hold(self):
        task = self.app.store.get('task', self.task['id'])
        task['resource_keys'] = ['external-lock']; self.app.store.put('task', task)
        self.observe(); self.age(); self.observe(); self.assert_held()
        task.update(resource_keys=[], supervision_floor='windows-job-object'); self.app.store.put('task', task)
        self.observe(); self.time.return_value += self.GRACE_S; self.observe(); self.assert_held()

    def test_grace_resets_when_scope_is_populated_again(self):
        self.observe()
        (self.group / 'cgroup.events').write_text('populated 1')
        self.age(); self.observe(); self.assert_held()
        (self.group / 'cgroup.events').write_text('populated 0')
        self.observe(); self.assert_held()
        self.time.return_value += self.GRACE_S; self.observe()
        self.assertEqual(self.app.store.get('task', self.task['id'])['status'], 'failed')

    def test_valid_late_receipt_wins_second_poll(self):
        self.observe(); self.age()
        receipt = dict(self.started, status='succeeded', process_tree_confirmed=True,
                       termination_evidence='cgroup-v2-populated-zero', artifacts=[])
        with patch.object(self.app.execution, 'poll', side_effect=[{'status': 'unknown'}, receipt]):
            self.observe()
        self.assertEqual(self.app.store.get('task', self.task['id'])['status'], 'succeeded')

    def test_explicit_reconcile_uses_same_grace_and_settlement(self):
        with self.assertRaises(Fault):
            self.app.reconcile(self.task['id'])
        self.assert_held(); self.age()
        self.assertEqual(self.app.reconcile(self.task['id'])['status'], 'failed')

    def test_restart_keeps_grace_and_pending_join(self):
        self.observe(); self.app.store.close()
        self.app = Service(config(self.root / 'workspace'), self.root / 'data')
        self.assert_held(); self.age(); self.observe()
        self.assertEqual(self.app.store.get('task', self.task['id'])['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
