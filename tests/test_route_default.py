import copy
import sys
import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from test_unknown_adapter_startup import _removed_adapter_name
from pangpang.service import Service
from pangpang.store import Fault


def _cfg_three(root):
    """Three local executors that share a directory. Ids sort opposite to priority."""
    c = config(root)
    argv = [sys.executable, '-c', "import sys; sys.stdin.read(); print('ok')"]
    probe = [sys.executable, '--version']
    c['installations'] = [
        {'id': 'a-pi', 'host_id': 'local', 'adapter': 'pi', 'argv': argv, 'probe_argv': probe, 'cwd': str(root), 'risk': 'write'},
        {'id': 'm-codex', 'host_id': 'local', 'adapter': 'codex', 'argv': argv, 'probe_argv': probe, 'cwd': str(root), 'risk': 'write'},
        {'id': 'z-claude', 'host_id': 'local', 'adapter': 'claude-code', 'argv': argv, 'probe_argv': probe, 'cwd': str(root), 'risk': 'write'},
    ]
    return c


class RouteDefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(_cfg_three(self.root), self.root / 'data')
        for i in ('a-pi', 'm-codex', 'z-claude'):
            self.app.probe(i)
        m = self.app.send('bot-main', {'text': 'x'}, 'rd')
        self.work = self.app.store.get('work', m['work_id'])

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_default_route_follows_priority_not_name(self):
        # a-pi sorts first by id and last by catalog priority. Shared directory
        # and permissions, so priority may choose.
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNotNone(r['selected'])
        self.assertEqual(r['selected']['installation_id'], 'z-claude')

    def test_explicit_pin_still_honored(self):
        r = self.app.route('bot-main', self.work, {'installation_id': 'codex'})
        self.assertEqual(r['selected']['installation_id'], 'm-codex')

    def test_adapter_name_in_host_field_resolves_to_installation(self):
        # The model may put the adapter name in host_id. It resolves to the
        # unique installation; an unknown name does not.
        r = self.app.route('bot-main', self.work, {'host_id': 'claude-code'})
        self.assertIsNotNone(r['selected'], r)
        self.assertEqual(r['selected']['installation_id'], 'z-claude')

    def test_unknown_host_in_host_field_still_rejects(self):
        # A host_id that is neither a host id/name nor an adapter must be left as-is so
        # route rejects every executor with host_constraint (diagnosable), not silently
        # resolve to a guess.
        r = self.app.route('bot-main', self.work, {'host_id': 'no-such-host-or-adapter'})
        self.assertIsNone(r['selected'])
        self.assertTrue(all(x['reason'] == 'host_constraint' for x in r['rejected']))

    def test_operator_priority_overrides_default_and_is_not_clobbered(self):
        # An operator-set installation.priority wins over the product default, and
        # nothing persists/overwrites it (no backfill double-source-of-truth).
        pi = self.app.execution.installations['a-pi']
        pi['priority'] = 1 # operator makes pi the preferred one
        r = self.app.route('bot-main', self.work, {})
        self.assertEqual(r['selected']['installation_id'], 'a-pi')

    def test_prompt_tells_model_to_use_param(self):
        from pangpang.context import projection
        m = self.app.send('bot-main', {'text': 'y'}, 'pp')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        system = projection(self.app, turn)[0]['content']
        self.assertIn('installation_id', system)
        self.assertIn('is not read by the system', system)
        self.assertNotIn(_removed_adapter_name(), system)
        self.assertNotIn('SAP', system)
        self.assertIn('does not authorize rewriting', system)


def _probe(app, installation_id, supervision='windows-job-object'):
    app.store.put('probe', {'id': installation_id, 'available': True, 'supervision': supervision,
                            'policy_digest': 'digest-' + installation_id})


class WorkAffinityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = config(self.root)
        argv = [sys.executable, '-c', 'print(1)']
        probe = [sys.executable, '--version']
        cfg['hosts'] = [
            {'id': 'home', 'name': '家里的开发机', 'transport': 'connector', 'os': 'Linux',
             'credential_hash': 'ab' * 32, 'allowed_roots': ['/home/dev/proj']},
            {'id': 'desk', 'name': '写作机', 'transport': 'local', 'os': 'Linux',
             'allowed_roots': [str(self.root)]},
        ]
        cfg['installations'] = [
            {'id': 'codex-home', 'host_id': 'home', 'adapter': 'codex', 'argv': argv, 'probe_argv': probe,
             'cwd': '/home/dev/proj', 'risk': 'write', 'priority': 30, 'use_when': '改家里的代码'},
            {'id': 'codex-desk', 'host_id': 'desk', 'adapter': 'codex', 'argv': argv, 'probe_argv': probe,
             'cwd': '/home/dev/proj', 'risk': 'write', 'priority': 1, 'use_when': '写作'},
        ]
        self.app = Service(cfg, self.root / 'data')
        _probe(self.app, 'codex-home')
        _probe(self.app, 'codex-desk')
        self.app.store.put('device', {'id': 'home', 'instance_id': 'device-home'})
        self.app.broker._last_seen['home'] = __import__('time').time()
        m = self.app.send('bot-main', {'text': '继续'}, 'aff')
        self.work = self.app.store.get('work', m['work_id'])

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _ran(self, installation_id, host_id, cwd, status='succeeded'):
        self.app.store.put('task', {
            'id': 'task-' + installation_id + '-' + status, 'work_id': self.work['id'], 'status': status,
            'host_id': host_id, 'installation_id': installation_id, 'cwd': cwd,
            'receipt': {'status': status}, 'created': 1.0})

    def test_prior_execution_beats_a_higher_priority_other_host(self):
        self._ran('codex-home', 'home', '/home/dev/proj')
        r = self.app.route('bot-main', self.work, {})
        self.assertEqual(r['selected']['installation_id'], 'codex-home')
        self.assertEqual(r['selected']['host_id'], 'home')

    def test_offline_prior_host_is_not_replaced_by_the_same_path(self):
        self._ran('codex-home', 'home', '/home/dev/proj')
        self.app.broker._last_seen.pop('home', None)
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNone(r['selected'])
        self.assertEqual(r['routing_block']['code'], 'executor_host_offline')
        with self.assertRaises(Fault) as cm:
            self.app.submit_task('bot-main', self.work['id'], {'prompt': '继续'})
        self.assertEqual(cm.exception.code, 'executor_host_offline')
        self.assertNotIn('codex-desk', cm.exception.advice)

    def test_explicit_migration_may_leave_the_prior_host(self):
        self._ran('codex-home', 'home', '/home/dev/proj')
        r = self.app.route('bot-main', self.work, {'installation_id': 'codex-desk'})
        self.assertEqual(r['selected']['installation_id'], 'codex-desk')

    def test_several_directories_are_not_collapsed_onto_one_candidate(self):
        self._ran('codex-home', 'home', '/home/dev/proj')
        self.app.store.put('task', {
            'id': 'task-other-dir', 'work_id': self.work['id'], 'status': 'succeeded',
            'host_id': 'home', 'installation_id': 'codex-home', 'cwd': '/home/dev/notes',
            'receipt': {'status': 'succeeded'}, 'created': 2.0})
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNone(r['selected'])
        self.assertEqual(r['routing_block']['code'], 'ambiguous_executor')

    def test_one_directory_matches_every_installation_that_used_it(self):
        notes = {'id': 'claude-home', 'host_id': 'home', 'adapter': 'claude-code',
                 'argv': [sys.executable, '-c', 'print(1)'], 'cwd': '/home/dev/proj',
                 'risk': 'write', 'priority': 1, 'use_when': '同一目录'}
        self.app.execution.installations['claude-home'] = notes
        _probe(self.app, 'claude-home')
        self._ran('codex-home', 'home', '/home/dev/proj')
        self.app.store.put('task', {
            'id': 'task-claude-same-dir', 'work_id': self.work['id'], 'status': 'succeeded',
            'host_id': 'home', 'installation_id': 'claude-home', 'cwd': '/home/dev/proj',
            'receipt': {'status': 'succeeded'}, 'created': 2.0})
        r = self.app.route('bot-main', self.work, {})
        self.assertEqual(r['selected']['installation_id'], 'claude-home')
        self.assertNotIn('routing_block', r)

    def test_different_credential_ids_are_not_chosen_by_priority(self):
        self.app.execution.installations['codex-home']['secret_refs'] = {'CODEX_API_KEY': 'ref-home'}
        self.app.execution.installations['codex-desk']['secret_refs'] = {'CODEX_API_KEY': 'ref-desk'}
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNone(r['selected'])
        self.assertEqual(r['routing_block']['code'], 'ambiguous_executor')
        self.assertNotIn('ref-home', str(r))
        self.assertNotIn('ref-desk', str(r))

    def test_the_same_credential_id_still_follows_priority(self):
        self.app.execution.installations['codex-home']['secret_refs'] = {'CODEX_API_KEY': 'ref-shared'}
        self.app.execution.installations['codex-desk']['secret_refs'] = {'CODEX_API_KEY': 'ref-shared'}
        r = self.app.route('bot-main', self.work, {})
        self.assertEqual(r['selected']['installation_id'], 'codex-desk')

    def test_preflight_keeps_the_routing_block(self):
        from pangpang.runtime import Runtime
        self._ran('codex-home', 'home', '/home/dev/proj')
        self.app.broker._last_seen.pop('home', None)
        runtime = Runtime(self.app)
        current = {'bot_id': 'bot-main', 'work_id': self.work['id']}
        with self.assertRaises(Fault) as blocked:
            runtime._semantic_preflight(current, 'submit_task', {'prompt': '继续'})
        self.assertEqual(blocked.exception.code, 'executor_host_offline')
        self.assertIn('不会改派', blocked.exception.advice)
        with self.assertRaises(Fault) as missing:
            runtime._semantic_preflight(current, 'submit_task',
                                        {'prompt': '继续', 'installation_id': 'missing'})
        self.assertEqual(missing.exception.code, 'no_eligible_executor')
        self.assertIsNone(missing.exception.advice)

    def test_unknown_directory_does_not_guess_another_machine(self):
        notes = {'id': 'claude-home', 'host_id': 'home', 'adapter': 'claude-code',
                 'argv': [sys.executable, '-c', 'print(1)'], 'cwd': '/home/dev/notes',
                 'risk': 'write', 'priority': 1, 'use_when': '笔记'}
        self.app.execution.installations['claude-home'] = notes
        _probe(self.app, 'claude-home')
        self._ran('codex-home', 'home', '/home/dev/proj', status='succeeded')
        self.app.store.put('task', {
            'id': 'task-other-cwd', 'work_id': self.work['id'], 'status': 'succeeded',
            'host_id': 'home', 'installation_id': 'claude-home', 'cwd': '/home/dev/notes',
            'receipt': {'status': 'succeeded'}, 'created': 2.0})
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNone(r['selected'])
        self.assertEqual(r['routing_block']['code'], 'ambiguous_executor')
        self.assertNotEqual((r.get('selected') or {}).get('host_id'), 'desk')

    def test_use_when_text_is_not_reachability(self):
        self.app.execution.installations['codex-home']['use_when'] = '直连不通，不要选这台'
        r = self.app.route('bot-main', self.work, {'installation_id': 'codex-home'})
        self.assertEqual(r['selected']['installation_id'], 'codex-home')
        self.app.broker._last_seen.pop('home', None)
        self.app.execution.installations['codex-home']['use_when'] = '一直在线'
        r = self.app.route('bot-main', self.work, {'installation_id': 'codex-home'})
        self.assertIsNone(r['selected'])
        self.assertTrue(any(x['reason'] == 'connector_offline' for x in r['rejected'] if x['installation_id'] == 'codex-home'))

    def test_artifact_on_the_prior_host_is_not_sent_elsewhere(self):
        self.app.store.put('artifact', {
            'id': 'artifact-home', 'work_id': self.work['id'], 'host_id': 'home',
            'path': '/home/dev/proj/out.txt', 'created': 1.0})
        r = self.app.route('bot-main', self.work, {})
        self.assertEqual(r['selected']['installation_id'], 'codex-home')

    def test_artifacts_on_two_hosts_are_not_guessed(self):
        for host_id, created in (('home', 1.0), ('desk', 2.0)):
            self.app.store.put('artifact', {
                'id': 'artifact-' + host_id, 'work_id': self.work['id'], 'host_id': host_id,
                'path': '/home/dev/proj/' + host_id + '.txt', 'created': created})
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNone(r['selected'])
        self.assertEqual(r['routing_block']['code'], 'ambiguous_executor')

    def test_missing_directory_is_not_replaced_by_another_machine(self):
        self._ran('codex-home', 'home', '/home/dev/proj')
        self.app.execution.installations['codex-home']['cwd'] = '/home/dev/other'
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNone(r['selected'])
        self.assertEqual(r['routing_block']['code'], 'work_routing_constraint')

    def test_different_rights_across_hosts_are_not_chosen_by_priority(self):
        self.app.execution.installations['codex-desk']['risk'] = 'read'
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNone(r['selected'])
        self.assertEqual(r['routing_block']['code'], 'ambiguous_executor')

    def test_unlaunched_history_does_not_pin_the_work(self):
        self.app.store.put('task', {
            'id': 'queued-only', 'work_id': self.work['id'], 'status': 'queued',
            'host_id': 'home', 'installation_id': 'codex-home', 'cwd': '/home/dev/proj',
            'created': 1.0})
        self.app.store.put('task', {
            'id': 'not-launched', 'work_id': self.work['id'], 'status': 'failed',
            'host_id': 'home', 'installation_id': 'codex-home', 'cwd': '/home/dev/proj',
            'receipt': {'not_launched': True}, 'created': 2.0})
        r = self.app.route('bot-main', self.work, {})
        self.assertEqual(r['selected']['installation_id'], 'codex-desk')

    def test_retired_ssh_history_is_not_sent_to_local(self):
        cfg = copy.deepcopy(self.app.config)
        cfg['hosts'].append({'id': 'ssh-h', 'transport': 'ssh', 'os': 'linux', 'target': 'user@host',
                             'runner_root': '/opt/pangpang-runner', 'python': 'python3',
                             'allowed_roots': ['/opt/work']})
        cfg['installations'].append({'id': 'codex-ssh-h', 'host_id': 'ssh-h', 'adapter': 'codex',
                                    'argv': [sys.executable, '-c', 'print(1)'], 'cwd': '/opt/work',
                                    'risk': 'write', 'priority': 1})
        app = Service(cfg, self.root / 'data-ssh')
        try:
            _probe(app, 'codex-desk')
            _probe(app, 'codex-ssh-h')
            m = app.send('bot-main', {'text': 'old'}, 'ssh-aff')
            work = app.store.get('work', m['work_id'])
            app.store.put('task', {'id': 'old-ssh', 'work_id': work['id'], 'status': 'succeeded',
                                  'host_id': 'ssh-h', 'installation_id': 'codex-ssh-h', 'cwd': '/opt/work',
                                  'receipt': {'status': 'succeeded'}, 'created': 1.0})
            r = app.route('bot-main', work, {})
            self.assertIsNone(r['selected'])
            self.assertNotEqual((r.get('selected') or {}).get('host_id'), 'desk')
        finally:
            app.store.close()


class SameHostProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = config(self.root)
        base = cfg['installations'][0]
        dev = self.root / 'dev'
        notes = self.root / 'notes'
        dev.mkdir()
        notes.mkdir()
        cfg['installations'] = [
            dict(base, id='codex-dev', cwd=str(dev), priority=30, use_when='开发'),
            dict(base, id='codex-notes', cwd=str(notes), priority=1, use_when='写作'),
        ]
        self.app = Service(cfg, self.root / 'data')
        self.app.probe('codex-dev')
        self.app.probe('codex-notes')
        m = self.app.send('bot-main', {'text': 'x'}, 'prof')
        self.work = self.app.store.get('work', m['work_id'])

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_different_directories_are_not_chosen_by_priority(self):
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNone(r['selected'])
        self.assertEqual(r['routing_block']['code'], 'ambiguous_executor')
        with self.assertRaises(Fault) as cm:
            self.app.submit_task('bot-main', self.work['id'], {'prompt': '写一点'})
        self.assertEqual(cm.exception.code, 'ambiguous_executor')
        pinned = self.app.route('bot-main', self.work, {'installation_id': 'codex-notes'})
        self.assertEqual(pinned['selected']['installation_id'], 'codex-notes')

    def test_readiness_stays_ready_when_directories_differ(self):
        entry = next(row for row in self.app.effective_capabilities() if row['capability'] == 'executor')
        self.assertEqual(entry['state'], 'ready')
        reasons = {row['reason'] for row in entry['detail']['installations'] if row['reason']}
        self.assertNotIn('work_directory_held', reasons)
        submitted = self.app.route('bot-main', self.work, {})
        self.assertIsNone(submitted['selected'])
        self.assertEqual(submitted['routing_block']['code'], 'ambiguous_executor')
        for installation in self.app.execution.installations.values():
            installation['enabled'] = False
        blocked = next(row for row in self.app.effective_capabilities() if row['capability'] == 'executor')
        self.assertEqual(blocked['state'], 'blocked')

    def test_same_directory_and_rights_follow_priority(self):
        self.app.execution.installations['codex-notes']['cwd'] = self.app.execution.installations['codex-dev']['cwd']
        r = self.app.route('bot-main', self.work, {})
        self.assertEqual(r['selected']['installation_id'], 'codex-notes')


class WorkspaceCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_install_workspace_cached_at_init(self):
        # _backfill_installation_workspace stamps workspace_key on the pre-existing
        # codex-local install so admission never realpaths in a hot txn.
        e = self.app.execution.installations['codex-local']
        self.assertIn('workspace_key', e)
        self.assertEqual(e.get('workspace_cwd'), e.get('cwd'))

    def test_submit_task_no_realpath_in_txn(self):
        # submit_task runs inside the durable-tool store.once transaction; with the
        # cache it must NOT call canonical_workspace (which now asserts no txn I/O).
        # Simulate: hold a store transaction and submit — must not raise.
        self.app.probe('codex-local')
        m = self.app.send('bot-main', {'text': 'x', 'allow_write': True}, 'wc')
        # ensure cache present
        self.assertIn('workspace_key', self.app.execution.installations['codex-local'])
        res = self.app.submit_task('bot-main', m['work_id'], {'prompt': 'y'})
        task = self.app.store.get('task', res['task_id'])
        # workspace_key on the task matches the cached install key
        self.assertEqual(task['workspace_key'], self.app.execution.installations['codex-local']['workspace_key'])


class StrictTxnMigrationTests(unittest.TestCase):
    """Guard: startup migrations must not realpath inside a store transaction.
    Run with strict_txn_io ON + a legacy local task lacking
    workspace_key -> Service init must not raise io_inside_transaction."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backfill_workspace_no_realpath_in_txn(self):
        from pangpang.store import set_strict_txn_io
        # first boot: create a legacy-style local task WITHOUT workspace_key
        app = Service(config(self.root), self.root / 'data')
        app.probe('codex-local')
        m = app.send('bot-main', {'text': 'x', 'allow_write': True}, 'legacy')
        res = app.submit_task('bot-main', m['work_id'], {'prompt': 'y'})
        # strip workspace_key to simulate a pre-Phase-6 legacy record
        t = app.store.get('task', res['task_id']); t.pop('workspace_key', None); app.store.put('task', t)
        # also strip the installation cache to force the migration path
        app.store.close()
        # second boot with strict mode: migrations (incl _backfill_workspace) must not
        # realpath in a txn.
        set_strict_txn_io(True)
        try:
            app2 = Service(config(self.root), self.root / 'data') # must not raise
            app2.store.close()
        finally:
            set_strict_txn_io(False)
