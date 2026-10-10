"""Adversarial acceptance at approval, commit and evidence boundaries."""
import base64
import copy
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.execution import Execution, artifact_bytes
from pangpang.service import Service
from pangpang.store import Fault, uid, set_strict_txn_io


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def routine(self):
        return self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'UTC',
            'schedule': {'type': 'interval', 'minutes': 10}})

    def turn(self):
        sent = self.app.send('bot-main', {'text': '更新日报'}, uid('test'))
        return self.app.store.get('turn', sent['turn_id'])

    def test_missing_and_stale_review_cannot_approve(self):
        r = self.routine()
        old = self.app.routine_review(r)['approval_digest']
        self.app.update_routine('bot-main', r['id'], {'instruction': '新指令', 'revision': r['revision']}, turn=self.turn())
        for supplied in (None, '', old):
            with self.assertRaisesRegex(Fault, 'routine_changed_since_review'):
                self.app.approve_routine('bot-main', r['id'], expected_digest=supplied)
        current = self.app.routine_review(r)
        self.app.approve_routine('bot-main', r['id'], expected_digest=current['approval_digest'])
        self.assertTrue(self.app.routine_step() is False)
        self.assertIsNotNone(self.app.run_routine_now('bot-main', r['id']))

    def test_legacy_and_mismatched_approvals_fail_at_all_fire_entries(self):
        for state in ('legacy', 'mismatch'):
            with self.subTest(state=state):
                r = self.app.store.get('routine', self.routine()['id'])
                if state == 'legacy':
                    r.pop('approval_state'); r.pop('approved_digest')
                else:
                    r['approved_digest'] = 'wrong'
                r.update(active=True, next_run_at=1)
                self.app.store.put('routine', r)
                for action in (lambda: self.app.run_routine_now('bot-main', r['id']),
                               lambda: self.app.pause_routine('bot-main', r['id'], True),
                               lambda: self.app._materialize_routine(r, 1, 'scheduled')):
                    with self.assertRaisesRegex(Fault, 'routine_not_approved'):
                        action()
                self.assertFalse(self.app.routine_step())
                self.assertEqual(self.app.routine_review(r)['approval_state'], 'pending')
        self.assertEqual(self.app.store.count('routine_run'), 0)

    def test_pending_create_and_update_still_validate_schedule(self):
        turn = self.turn()
        r = self.routine()
        for change in ({'schedule': 'not-a-dict'}, {'timezone': 'No/SuchZone'},
                       {'schedule': {'type': 'interval', 'minutes': -1}}):
            with self.subTest(change=change):
                before = self.app.store.get('routine', r['id'])
                with self.assertRaises(Fault):
                    self.app.create_routine('bot-main', dict(before, **change), turn=turn)
                with self.assertRaises(Fault):
                    self.app.update_routine('bot-main', r['id'], change, turn=turn)
                self.assertEqual(self.app.store.get('routine', r['id']), before)

    def test_restart_migrates_legacy_approval_without_rearming(self):
        r = self.routine()
        r.pop('approval_state'); r.pop('approved_digest')
        r.update(active=True, next_run_at=1)
        self.app.store.put('routine', r)
        self.app.store.close()
        self.app = Service(config(self.root), self.root / 'data')
        saved = self.app.store.get('routine', r['id'])
        self.assertEqual(saved['approval_state'], 'pending')
        self.assertFalse(saved['active'])
        self.assertIsNone(saved['next_run_at'])
        self.assertEqual(saved['instruction'], r['instruction'])
        self.assertFalse(self.app.routine_step())

    def test_owner_instruction_change_invalidates_approval(self):
        r = self.routine()
        old = self.app.routine_review(r)['approval_digest']
        self.app.update_bot('bot-main', {'instructions': '新的长期指令', 'revision': self.app.bot('bot-main')['revision']})
        current = self.app.routine_review(r)
        self.assertEqual(current['approval_state'], 'pending')
        self.assertEqual(current['owner_policy']['instructions'], '新的长期指令')
        self.assertNotEqual(current['approval_digest'], old)
        with self.assertRaises(Fault):
            self.app.run_routine_now('bot-main', r['id'])

    def test_queued_fire_keeps_the_approved_owner_instructions(self):
        from pangpang.context import projection
        r = self.routine()
        approved = self.app.bot('bot-main')['instructions']
        run = self.app.run_routine_now('bot-main', r['id'])
        self.app.update_bot('bot-main', {'instructions': 'unreviewed new instructions',
                                       'revision': self.app.bot('bot-main')['revision']})
        messages = projection(self.app, self.app.store.get('turn', run['turn_id']))
        self.assertIn(approved, messages[0]['content'])
        self.assertNotIn('unreviewed new instructions', messages[0]['content'])

    def test_scheduler_rereads_after_concurrent_edit_or_pause(self):
        for action in ('edit', 'pause'):
            with self.subTest(action=action):
                r = self.routine()
                r['next_run_at'] = 1
                self.app.store.put('routine', r)
                scanned, resume = threading.Event(), threading.Event()
                original = self.app.store.find
                errors = []
                def scan(kind, **kwargs):
                    rows = original(kind, **kwargs)
                    if kind == 'routine':
                        scanned.set()
                        if not resume.wait(5):
                            raise RuntimeError('test interleave timed out')
                    return rows
                def sweep():
                    try: self.app.routine_step()
                    except BaseException as exc: errors.append(exc)
                turn = self.turn()
                with patch.object(self.app.store, 'find', scan):
                    thread = threading.Thread(target=sweep)
                    thread.start()
                    try:
                        self.assertTrue(scanned.wait(5))
                        if action == 'edit':
                            self.app.update_routine('bot-main', r['id'], {'instruction': '新指令',
                                'revision': self.app.store.get('routine', r['id'])['revision']}, turn=turn)
                        else:
                            self.app.pause_routine('bot-main', r['id'])
                    finally:
                        resume.set(); thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                saved = self.app.store.get('routine', r['id'])
                self.assertFalse(saved['active'])
                if action == 'edit':
                    self.assertEqual(saved['instruction'], '新指令')
                    self.assertEqual(saved['approval_state'], 'pending')
                self.assertEqual(self.app.store.count('routine_run'), 0)

    def test_fire_and_schedule_rollback_together(self):
        r = self.routine(); r['next_run_at'] = 1
        self.app.store.put('routine', r)
        original = self.app.store.put
        def fail_schedule(kind, item, **kw):
            if kind == 'routine': raise Fault('injected_write_failure')
            return original(kind, item, **kw)
        set_strict_txn_io(True)
        try:
            with patch.object(self.app.store, 'put', fail_schedule), self.assertRaises(Fault):
                self.app.routine_step()
        finally:
            set_strict_txn_io(False)
        self.assertEqual(self.app.store.count('routine_run'), 0)
        self.assertEqual(self.app.store.count('work'), 0)
        self.assertEqual(self.app.store.get('routine', r['id'])['next_run_at'], 1)

    def device(self):
        # G: the single managed-config writer; never mutate the live dict.
        candidate = copy.deepcopy(self.app.config)
        candidate['hosts'].append({'id': 'host-B', 'transport': 'connector'})
        self.app.commit_managed_config(candidate)
        return uid('device')

    def report(self, adapter='codex'):
        cmd = self.app.broker.enqueue('host-B', 'configure', {'adapter': adapter})
        item = {'id': adapter + '-host-B', 'host_id': 'host-B', 'adapter': adapter,
                'cwd': str(self.root), 'resource_keys': [], 'max_concurrency': 1}
        return cmd, {'installation': item, 'probe': {'available': True, 'policy_digest': 'p'}}

    def test_report_is_bound_to_target_and_policy(self):
        self.device()
        cmd, report = self.report()
        mutations = [{'id': 'codex-host-A'}, {'adapter': 'claude-code'}, {'cwd': {'invalid': 'type'}},
                     {'cwd': '../elsewhere'}, {'max_concurrency': -5}, {'max_concurrency': True},
                     {'max_concurrency': 2}, {'resource_keys': ['other-host-resource']}]
        before = copy.deepcopy(self.app.config)
        for change in mutations:
            with self.subTest(change=change):
                bad = copy.deepcopy(report); bad['installation'].update(change)
                with self.assertRaises(Fault): self.app.onboarding.report(cmd, bad)
                self.assertEqual(self.app.config, before)
                self.assertEqual(self.app.store.get('settings', 'managed')['config'], before)
        self.app.onboarding.report(cmd, report)
        credential = dict(cmd, action='credential_install', payload={'installation_id': 'different'})
        with self.assertRaisesRegex(Fault, 'installation_approval_mismatch'):
            self.app.onboarding.report(credential, report)

    def test_batch_rollback_never_publishes_ghost_and_retry_commits(self):
        identity = self.device()
        cmd, report = self.report()
        original_config, original_execution = self.app.config, self.app.execution
        body = {'instance_id': identity, 'reports': [{'id': cmd['id'], 'result': report},
                                                    {'id': 'missing-command', 'result': {}}]}
        with self.assertRaises(Fault): self.app.broker.sync('host-B', body)
        self.assertIs(self.app.config, original_config)
        self.assertIs(self.app.execution, original_execution)
        self.assertEqual(self.app.store.get('settings', 'managed')['config'], original_config)
        self.assertEqual(self.app.store.get('command', cmd['id'])['status'], 'pending')
        cmd2, report2 = self.report('claude-code')
        body['reports'][-1] = {'id': cmd2['id'], 'result': report2}
        self.app.broker.sync('host-B', body)
        self.assertIn('codex-host-B', self.app.execution.installations)
        self.assertIn('claude-code-host-B', self.app.execution.installations)
        self.assertEqual(self.app.config, self.app.store.get('settings', 'managed')['config'])
        self.assertIs(self.app.execution.config, self.app.config)
        self.assertIs(self.app.execution.broker, self.app.broker)

    def test_outer_rollback_and_write_failure_discard_staged_publication(self):
        self.device(); cmd, report = self.report()
        original_config = self.app.config
        with self.assertRaisesRegex(Fault, 'outer'):
            with self.app.store.atomic():
                self.app.onboarding.report(cmd, report)
                self.assertIs(self.app.config, original_config)
                raise Fault('outer')
        self.assertIs(self.app.config, original_config)
        original_put = self.app.store.put
        def fail(kind, item, **kw):
            if kind == 'probe': raise Fault('probe_failure')
            return original_put(kind, item, **kw)
        with patch.object(self.app.store, 'put', fail), self.assertRaises(Fault):
            self.app.onboarding.report(cmd, report)
        self.assertIs(self.app.config, original_config)
        self.app.store.put('inventory', {'id': 'unrelated'})
        self.assertIs(self.app.config, original_config)

    def test_database_commit_failure_does_not_publish(self):
        import sqlite3
        self.device(); cmd, report = self.report()
        original_config, original_execution = self.app.config, self.app.execution
        db = self.app.store.db
        class FailCommit:
            def execute(self, sql, *args):
                if sql == 'COMMIT': raise sqlite3.OperationalError('injected commit failure')
                return db.execute(sql, *args)
            def __getattr__(self, name): return getattr(db, name)
        with patch.object(self.app.store, 'db', FailCommit()):
            with self.assertRaises(sqlite3.OperationalError): self.app.onboarding.report(cmd, report)
        self.assertIs(self.app.config, original_config)
        self.assertIs(self.app.execution, original_execution)
        self.assertEqual(self.app.store.get('settings', 'managed')['config'], original_config)
        self.app.store.put('inventory', {'id': 'after-failed-commit'})
        self.assertIs(self.app.config, original_config)

    def test_evidence_rejects_tampering_invalid_encoding_and_wrong_size(self):
        raw = b'original'
        a = {'id': 'artifact-test', 'owner_bot_id': 'bot-main', 'host_id': 'local',
             'path': str(self.root / 'report.txt'), 'digest': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
        self.app.store.put('artifact', a)
        for encoded in (base64.b64encode(b'TAMPERED').decode(), 'bad base64!'):
            with patch.object(self.app.execution, 'read_artifact', return_value={'data': encoded, 'digest': a['digest']}):
                with self.assertRaises(Fault): self.app.read_evidence('bot-main', a['id'])
        good = {'data': base64.b64encode(raw).decode(), 'digest': 'untrusted-self-report'}
        with patch.object(self.app.execution, 'read_artifact', return_value=good):
            result = self.app.read_evidence('bot-main', a['id'])
            self.assertEqual(result['content'], 'original')
            self.assertEqual(result['digest'], a['digest'])
        with self.assertRaises(Fault): artifact_bytes(dict(a, size=999), good)
