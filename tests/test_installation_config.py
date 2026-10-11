import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class InstallationConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()
        self.app = Service(config(self.root), self.root.parent / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_edit_max_concurrency_persists_and_takes_effect(self):
        # 2 (arbitrary) -> live installation + persisted config both updated.
        res = self.app.configure_installation('codex-local', {'max_concurrency': 3})
        self.assertEqual(res['max_concurrency'], 3)
        self.assertEqual(self.app.execution.installations['codex-local']['max_concurrency'], 3)
        # persisted to settings config (survives an Execution rebuild)
        saved = self.app.store.get('settings', 'managed')['config']
        inst = next(i for i in saved['installations'] if i['id'] == 'codex-local')
        self.assertEqual(inst['max_concurrency'], 3)

    def test_prepared_config_rejects_same_installation_change(self):
        prepared = self.app.prepare_installation_config('codex-local', {'max_concurrency': 3})
        self.app.configure_installation('codex-local', {'timeout_s': 120})
        with self.assertRaisesRegex(Fault, 'installation_config_changed'):
            self.app.commit_installation_config('codex-local', prepared)
        self.assertEqual(self.app.execution.installations['codex-local']['timeout_s'], 120)

    def test_prepared_cwd_rechecks_busy_at_commit(self):
        sub = self.root / 'sub'; sub.mkdir()
        prepared = self.app.prepare_installation_config('codex-local', {'cwd': str(sub)})
        self.app.store.put('task', {'id': 'new-running', 'status': 'running', 'installation_id': 'codex-local'})
        with self.assertRaisesRegex(Fault, 'installation_busy'):
            self.app.commit_installation_config('codex-local', prepared)

    def test_http_outer_rollback_does_not_publish_configuration(self):
        original = self.app.config
        prepared = self.app.prepare_installation_config('codex-local', {'max_concurrency': 3})
        with self.assertRaisesRegex(Fault, 'journal_failed'):
            with self.app.store.atomic():
                self.app.commit_installation_config('codex-local', prepared)
                self.assertIs(self.app.config, original)
                raise Fault('journal_failed')
        self.assertIs(self.app.config, original)
        self.assertEqual(self.app.store.get('settings', 'managed')['config'], original)

    def test_max_concurrency_gates_admission(self):
        # With cap=2, a third overlapping-disjoint task is not admissible.
        self.app.configure_installation('codex-local', {'max_concurrency': 2})
        def mk(tid, seg):
            t = {'id': tid, 'status': 'running', 'host_id': 'local', 'installation_id': 'codex-local',
                 'workspace_key': ['local', [seg]], 'access_mode': 'write', 'resource_keys': [],
                 'execution_id': tid + '-x', 'spec_digest': 'd', 'created': 1.0}
            return self.app.store.put('task', t)
        mk('t1', 'a'); mk('t2', 'b') # 2 running == cap
        cand = mk('t3', 'c'); cand['status'] = 'queued'; self.app.store.put('task', cand)
        self.assertFalse(self.app._task_admissible(self.app.store.get('task', 't3')))
        # bump cap to 3 -> now admissible
        self.app.configure_installation('codex-local', {'max_concurrency': 3})
        self.assertTrue(self.app._task_admissible(self.app.store.get('task', 't3')))

    def test_edit_enabled_and_timeout(self):
        res = self.app.configure_installation('codex-local', {'enabled': False, 'timeout_s': 120})
        self.assertEqual(res['enabled'], False)
        self.assertEqual(res['timeout_s'], 120)
        self.assertEqual(self.app.execution.installations['codex-local']['enabled'], False)

    def test_edit_cwd_within_allowed_roots(self):
        sub = self.root / 'sub'
        sub.mkdir()
        res = self.app.configure_installation('codex-local', {'cwd': str(sub)})
        self.assertEqual(Path(res['cwd']).resolve(), sub.resolve())

    def test_cwd_outside_allowed_roots_rejected(self):
        with self.assertRaises(Fault):
            self.app.configure_installation('codex-local', {'cwd': 'C:/Windows' if __import__('os').name == 'nt' else '/etc'})

    def test_invalid_max_concurrency_rejected(self):
        for bad in (0, 17, 'x', -1):
            with self.assertRaises(Fault) as cm:
                self.app.configure_installation('codex-local', {'max_concurrency': bad})
            self.assertEqual(cm.exception.code, 'invalid_max_concurrency')

    def test_invalid_timeout_rejected(self):
        for bad in (10, 999999, 'nope'):
            with self.assertRaises(Fault) as cm:
                self.app.configure_installation('codex-local', {'timeout_s': bad})
            self.assertEqual(cm.exception.code, 'invalid_timeout_s')

    def test_unknown_field_rejected(self):
        # argv / adapter / host_id / resource_keys must NOT be editable (safety law).
        for field in ('argv', 'adapter', 'host_id', 'resource_keys', 'endpoint'):
            with self.assertRaises(Fault) as cm:
                self.app.configure_installation('codex-local', {field: 'x'})
            self.assertEqual(cm.exception.code, 'unsupported_installation_field')

    def test_missing_installation_rejected(self):
        with self.assertRaises(Fault) as cm:
            self.app.configure_installation('does-not-exist', {'max_concurrency': 2})
        self.assertEqual(cm.exception.code, 'installation_not_found')

    def test_disabled_installation_not_admissible(self):
        # an executor disabled AFTER a task was queued must not start (admission
        # is the queued->starting linearization point; route ran at submit time).
        t = self.app.store.put('task', {'id': 'td', 'status': 'queued', 'host_id': 'local',
            'installation_id': 'codex-local', 'workspace_key': ['local', ['a']], 'access_mode': 'write',
            'resource_keys': [], 'execution_id': 'td-x', 'spec_digest': 'd', 'created': 1.0})
        self.assertTrue(self.app._task_admissible(self.app.store.get('task', 'td')))
        self.app.configure_installation('codex-local', {'enabled': False})
        self.assertFalse(self.app._task_admissible(self.app.store.get('task', 'td')))

    def test_timeout_snapshot_frozen_on_task(self):
        # submit_task snapshots timeout_s so a later installation edit cannot
        # change what execution.spec launches (spec_digest would otherwise lie).
        self.app.configure_installation('codex-local', {'timeout_s': 120})
        self.app.probe('codex-local')
        m = self.app.send('bot-main', {'text': 'x', 'allow_write': True}, 'tsnap')
        res = self.app.submit_task('bot-main', m['work_id'], {'prompt': 'y', 'installation_id': 'codex-local'})
        task = self.app.store.get('task', res['task_id'])
        self.assertEqual(task['timeout_s'], 120)
        # editing the installation later does NOT change the frozen task snapshot
        self.app.configure_installation('codex-local', {'timeout_s': 3600})
        self.assertEqual(self.app.execution.spec(task)['timeout_s'], 120)

    def test_cwd_edit_refused_while_installation_busy(self):
        # a launching/active run means UNKNOWN != idle; moving cwd under it is
        # unsafe. enabled/timeout edits stay allowed; only cwd is gated.
        sub = self.root / 'sub'; sub.mkdir()
        self.app.store.put('task', {'id': 'busy1', 'status': 'running', 'host_id': 'local',
            'installation_id': 'codex-local', 'workspace_key': ['local', ['a']], 'access_mode': 'write',
            'resource_keys': [], 'execution_id': 'busy1-x', 'spec_digest': 'd', 'created': 1.0})
        with self.assertRaises(Fault) as cm:
            self.app.configure_installation('codex-local', {'cwd': str(sub)})
        self.assertEqual(cm.exception.code, 'installation_busy')
        # a non-cwd edit is still fine while busy
        self.assertEqual(self.app.configure_installation('codex-local', {'enabled': False})['enabled'], False)

    def test_priority_override_and_fallback(self):
        # No override -> catalog default (codex=30). Operator sets it -> that value
        # wins in _route_priority. Cleared (None) -> falls back to the catalog again.
        e = self.app.execution.installations['codex-local']
        self.assertEqual(self.app._route_priority(e), 30)
        res = self.app.configure_installation('codex-local', {'priority': 5})
        self.assertEqual(res['priority'], 5)
        self.assertEqual(self.app._route_priority(self.app.execution.installations['codex-local']), 5)
        self.app.configure_installation('codex-local', {'priority': None})
        self.assertEqual(self.app._route_priority(self.app.execution.installations['codex-local']), 30)

    def test_use_when_override_and_fallback(self):
        # No override -> catalog default; operator override wins; cleared -> fallback.
        default = self.app._use_when(self.app.execution.installations['codex-local'])
        self.assertTrue(default) # codex has a catalog use_when
        self.app.configure_installation('codex-local', {'use_when': '只在需要出图时用'})
        self.assertEqual(self.app._use_when(self.app.execution.installations['codex-local']), '只在需要出图时用')
        self.app.configure_installation('codex-local', {'use_when': ' '}) # blank clears
        self.assertEqual(self.app._use_when(self.app.execution.installations['codex-local']), default)

    def test_invalid_priority_rejected(self):
        # bool (int subclass, True->1) and non-integer floats/strings must NOT
        # silently truncate into a valid priority and reorder scheduling.
        for bad in (0, 1000, -1, 'x', True, False, 1.9, 999.9, '1.9', [5]):
            with self.assertRaises(Fault) as cm:
                self.app.configure_installation('codex-local', {'priority': bad})
            self.assertEqual(cm.exception.code, 'invalid_priority', f'accepted bad priority {bad!r}')

    def test_priority_accepts_int_and_integer_forms(self):
        # Real int, integer-valued float, and integer string are all fine.
        for good, want in [(5, 5), (5.0, 5), ('7', 7)]:
            res = self.app.configure_installation('codex-local', {'priority': good})
            self.assertEqual(res['priority'], want)

    def test_invalid_use_when_rejected(self):
        for bad in ('x' * 501, 123, ['list']):
            with self.assertRaises(Fault) as cm:
                self.app.configure_installation('codex-local', {'use_when': bad})
            self.assertEqual(cm.exception.code, 'invalid_use_when')

    def test_priority_use_when_persist(self):
        self.app.configure_installation('codex-local', {'priority': 7, 'use_when': '自定义说明'})
        saved = self.app.store.get('settings', 'managed')['config']
        inst = next(i for i in saved['installations'] if i['id'] == 'codex-local')
        self.assertEqual(inst['priority'], 7)
        self.assertEqual(inst['use_when'], '自定义说明')

    def test_argv_still_immutable_alongside_new_fields(self):
        # Even in the same body as valid new fields, argv must be rejected wholesale.
        with self.assertRaises(Fault) as cm:
            self.app.configure_installation('codex-local', {'priority': 5, 'argv': ['evil']})
        self.assertEqual(cm.exception.code, 'unsupported_installation_field')

    def test_connector_installation_not_editable(self):
        # A connector (device-owned) installation is read-only here — its config is
        # overwritten by the device report, so a cloud edit would silently revert.
        cfg = dict(self.app.config)
        cfg = __import__('copy').deepcopy(self.app.config)
        cfg['hosts'].append({'id': 'dev1', 'transport': 'connector', 'os': 'Linux',
                             'credential_hash': 'x' * 64, 'allowed_roots': []})
        cfg['installations'].append({'id': 'codex-dev1', 'host_id': 'dev1', 'adapter': 'codex',
                                     'argv': ['device-owned-command'], 'cwd': '/tmp', 'risk': 'write',
                                     'max_concurrency': 2, 'enabled': True, 'prompt_mode': 'stdin'})
        app2 = Service(cfg, self.root.parent / 'data2')
        try:
            with self.assertRaises(Fault) as cm:
                app2.configure_installation('codex-dev1', {'max_concurrency': 4})
            self.assertEqual(cm.exception.code, 'only_local_installations_editable')
            self.assertEqual(app2.execution.installations['codex-dev1']['max_concurrency'], 2)
        finally:
            app2.store.close()

    def _connector(self):
        cfg = __import__('copy').deepcopy(self.app.config)
        cfg['hosts'].append({'id': 'dev1', 'transport': 'connector', 'os': 'Linux',
                             'credential_hash': 'x' * 64, 'allowed_roots': []})
        cfg['installations'].append({'id': 'codex-dev1', 'host_id': 'dev1', 'adapter': 'codex',
                                     'argv': ['device-owned-command'], 'cwd': '/home/dev/proj', 'risk': 'write',
                                     'max_concurrency': 2, 'enabled': True, 'prompt_mode': 'stdin'})
        app = Service(cfg, self.root.parent / 'data-remote')
        app.store.put('probe', {'id': 'codex-dev1', 'available': True, 'policy_digest': 'device-digest',
                                'supervision': 'windows-job-object'})
        return app

    def test_connector_routing_metadata_keeps_execution_fields_and_digest(self):
        app = self._connector()
        try:
            saved = app.configure_installation('codex-dev1', {'use_when': '家里的开发机：改这个项目', 'priority': 4})
            self.assertEqual(saved['use_when'], '家里的开发机：改这个项目')
            self.assertEqual(saved['priority'], 4)
            inst = app.execution.installations['codex-dev1']
            self.assertEqual(inst['argv'], ['device-owned-command'])
            self.assertEqual(inst['cwd'], '/home/dev/proj')
            self.assertEqual(inst['max_concurrency'], 2)
            self.assertEqual(app.store.get('probe', 'codex-dev1')['policy_digest'], 'device-digest')
            self.assertFalse((self.root.parent / 'data-remote' / 'knowledge.db').exists())
        finally:
            app.store.close()

    def test_connector_execution_fields_are_refused_without_partial_apply(self):
        app = self._connector()
        try:
            for field, value in (('cwd', '/elsewhere'), ('enabled', False), ('timeout_s', 60),
                                 ('extra_argv', ['--flag']), ('max_concurrency', 3)):
                with self.subTest(field=field):
                    with self.assertRaises(Fault) as cm:
                        app.configure_installation('codex-dev1', {'priority': 4, field: value})
                    self.assertEqual(cm.exception.code, 'only_local_installations_editable')
            inst = app.execution.installations['codex-dev1']
            self.assertNotIn('priority', inst)
            self.assertEqual(inst['cwd'], '/home/dev/proj')
            self.assertEqual(inst['max_concurrency'], 2)
            for field, value in (('argv', ['evil']), ('env', {'CODEX_HOME': '/x'}),
                                 ('policy_digest', 'forged'), ('secret_refs', {'K': 'v'})):
                with self.subTest(field=field):
                    with self.assertRaises(Fault) as cm:
                        app.configure_installation('codex-dev1', {field: value})
                    self.assertEqual(cm.exception.code, 'unsupported_installation_field')
            self.assertEqual(app.store.get('probe', 'codex-dev1')['policy_digest'], 'device-digest')
        finally:
            app.store.close()

    def test_connector_routing_edit_conflicts_on_a_stale_prepare(self):
        app = self._connector()
        try:
            prepared = app.prepare_installation_config('codex-dev1', {'use_when': '先写的说明'})
            app.configure_installation('codex-dev1', {'priority': 8})
            with self.assertRaises(Fault) as cm:
                app.commit_installation_config('codex-dev1', prepared)
            self.assertEqual(cm.exception.code, 'installation_config_changed')
            inst = app.execution.installations['codex-dev1']
            self.assertEqual(inst['priority'], 8)
            self.assertNotIn('use_when', inst)
        finally:
            app.store.close()


if __name__ == '__main__':
    unittest.main()
