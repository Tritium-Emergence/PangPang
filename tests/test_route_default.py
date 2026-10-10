import copy
import sys
import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service


def _cfg_three(root):
    """Base test config + 3 local executors (qwen-claude / claude-code / codex),
    all priority-less, to test default routing."""
    c = config(root)
    argv = [sys.executable, '-c', "import sys; sys.stdin.read(); print('ok')"]
    probe = [sys.executable, '--version']
    c['installations'] = [
        {'id': 'inst-local-codex', 'host_id': 'local', 'adapter': 'codex', 'argv': argv, 'probe_argv': probe, 'cwd': str(root), 'risk': 'write'},
        {'id': 'inst-local-claude-code', 'host_id': 'local', 'adapter': 'claude-code', 'argv': argv, 'probe_argv': probe, 'cwd': str(root), 'risk': 'write'},
        {'id': 'inst-local-qwen-claude', 'host_id': 'local', 'adapter': 'qwen-claude', 'argv': argv, 'probe_argv': probe, 'cwd': str(root), 'risk': 'write', 'max_concurrency': 2},
    ]
    return c


class RouteDefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(_cfg_three(self.root), self.root / 'data')
        for i in ('inst-local-codex', 'inst-local-claude-code', 'inst-local-qwen-claude'):
            self.app.probe(i)
        m = self.app.send('bot-main', {'text': 'x'}, 'rd')
        self.work = self.app.store.get('work', m['work_id'])

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_default_route_picks_qwen_not_alphabetical(self):
        # Priority is a PURE FALLBACK now (no persisted priority). An unpinned task
        # must route to qwen-claude, not the alphabetical claude-code.
        r = self.app.route('bot-main', self.work, {})
        self.assertIsNotNone(r['selected'])
        self.assertEqual(r['selected']['installation_id'], 'inst-local-qwen-claude')

    def test_explicit_pin_still_honored(self):
        # Pinning claude-code explicitly must still route there.
        r = self.app.route('bot-main', self.work, {'installation_id': 'claude-code'})
        self.assertEqual(r['selected']['installation_id'], 'inst-local-claude-code')

    def test_adapter_name_in_host_field_resolves_to_installation(self):
        # The model may put the adapter name in host_id (the system prompt tells it the
        # adapter name may go in host_id OR installation_id). It must resolve to the
        # unique installation and route there — NOT reject every executor with
        # host_constraint (the bug that wedged the SAP SAC task).
        r = self.app.route('bot-main', self.work, {'host_id': 'qwen-claude'})
        self.assertIsNotNone(r['selected'], r)
        self.assertEqual(r['selected']['installation_id'], 'inst-local-qwen-claude')

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
        codex = self.app.execution.installations['inst-local-codex']
        codex['priority'] = 1 # operator makes codex the preferred one
        r = self.app.route('bot-main', self.work, {})
        self.assertEqual(r['selected']['installation_id'], 'inst-local-codex')

    def test_prompt_tells_model_to_use_param(self):
        from pangpang.context import projection
        m = self.app.send('bot-main', {'text': 'y'}, 'pp')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        system = projection(self.app, turn)[0]['content']
        self.assertIn('installation_id', system)
        self.assertIn('is not read by the system', system)


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
