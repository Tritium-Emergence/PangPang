import tempfile
import time
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service


class WorkspaceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_segment_overlap_not_string_prefix(self):
        # /repo overlaps /repo/sub (parent/child) but NOT /repo2 (string-prefix trap).
        cw = self.app._canonical_workspace
        ov = self.app._workspace_overlaps
        host = {"id": "h", "transport": "remote", "os": "linux"}
        repo = cw(host, "/repo")
        sub = cw(host, "/repo/sub")
        repo2 = cw(host, "/repo2")
        self.assertTrue(ov(repo, sub))
        self.assertTrue(ov(sub, repo))
        self.assertFalse(ov(repo, repo2))
        self.assertFalse(ov(repo2, repo))
        self.assertTrue(ov(repo, repo)) # identical overlaps

    def test_different_hosts_never_overlap(self):
        cw = self.app._canonical_workspace
        ov = self.app._workspace_overlaps
        a = cw({"id": "h1", "transport": "remote"}, "/repo")
        b = cw({"id": "h2", "transport": "remote"}, "/repo")
        self.assertFalse(ov(a, b))

    def test_trailing_slash_and_dot_normalized(self):
        cw = self.app._canonical_workspace
        ov = self.app._workspace_overlaps
        host = {"id": "h", "transport": "remote", "os": "linux"}
        self.assertTrue(ov(cw(host, "/repo/"), cw(host, "/repo")))
        self.assertTrue(ov(cw(host, "/repo/./sub"), cw(host, "/repo/sub")))

    def test_dotdot_folded_remote(self):
        # /repo/x/../shared and /repo/shared are the SAME directory; folding..
        # lexically stops two writers aliasing the same dir past admission (P1).
        cw = self.app._canonical_workspace
        ov = self.app._workspace_overlaps
        host = {"id": "h", "transport": "remote", "os": "linux"}
        self.assertEqual(cw(host, "/repo/x/../shared"), cw(host, "/repo/shared"))
        self.assertTrue(ov(cw(host, "/repo/x/../shared"), cw(host, "/repo/shared")))
        # leading.. cannot escape above root
        self.assertEqual(cw(host, "/../etc"), ("h", ("etc",)))

    def test_windows_case_insensitive(self):
        cw = self.app._canonical_workspace
        ov = self.app._workspace_overlaps
        host = {"id": "h", "transport": "remote", "os": "Windows"}
        self.assertTrue(ov(cw(host, "C:/Repo/Sub"), cw(host, "c:/repo/sub")))
        self.assertTrue(ov(cw(host, "C:\\Repo"), cw(host, "c:/repo/sub")))

    def test_case_folding_follows_target_not_controller(self):
        # a Linux TARGET's /Repo and /repo are DISTINCT even though this
        # controller is Windows (os.name=='nt'). Folding must follow the target host's
        # OS, never the controller's — else a Windows coordinator wrongly serializes a
        # Linux device's disjoint dirs.
        cw = self.app._canonical_workspace
        ov = self.app._workspace_overlaps
        linux = {"id": "h", "transport": "remote", "os": "linux"}
        self.assertFalse(ov(cw(linux, "/Repo"), cw(linux, "/repo")))
        # darwin folds (default volume is case-insensitive)
        mac = {"id": "h", "transport": "remote", "os": "darwin"}
        self.assertTrue(ov(cw(mac, "/Repo"), cw(mac, "/repo")))

    def test_unknown_remote_os_fails_closed_host_wide(self):
        # an unknown/blank remote OS returns a host-wide quarantine (empty
        # segments) that overlaps EVERYTHING on that host — conservative serialization
        # rather than a wrong case guess that could alias two distinct dirs (fail open).
        cw = self.app._canonical_workspace
        ov = self.app._workspace_overlaps
        host = {"id": "h", "transport": "remote"} # no os
        a = cw(host, "/repo/a")
        b = cw(host, "/totally/different")
        self.assertEqual(a[1], ())
        self.assertTrue(ov(a, b)) # host-wide: overlaps anything on the host

    def test_task_snapshots_cwd_and_workspace_key(self):
        # A submitted task carries its own cwd + workspace_key + access_mode, immune
        # to a later installation.cwd change (claim-A/execute-B guard).
        self.app.probe('codex-local')
        m = self.app.send('bot-main', {'text': 'x', 'allow_write': True}, 'ws')
        res = self.app.submit_task('bot-main', m['work_id'], {'prompt': 'x'})
        task = self.app.store.get('task', res['task_id'])
        self.assertIn('cwd', task)
        self.assertIn('workspace_key', task)
        self.assertEqual(task['access_mode'], 'write')
        # spec() launches the task's own cwd, not the live installation cwd
        inst = self.app.execution.installations['codex-local']
        inst['cwd'] = '/somewhere/else'
        self.app.execution.installations['codex-local'] = inst
        spec = self.app.execution.spec(task)
        self.assertEqual(spec['cwd'], task['cwd'])
        self.assertNotEqual(spec['cwd'], '/somewhere/else')

    def _mk_task(self, tid, host='local', inst='codex-local', segs=('repo',), mode='write', status='running'):
        t = {'id': tid, 'status': status, 'host_id': host, 'installation_id': inst,
             'workspace_key': [host, list(segs)], 'access_mode': mode, 'resource_keys': [],
             'execution_id': tid + '-x', 'spec_digest': 'd', 'created': 1.0}
        return self.app.store.put('task', t)

    def test_overlapping_writers_not_admissible(self):
        # /repo (running writer) blocks /repo/sub writer admission.
        self.app.execution.installations['codex-local']['max_concurrency'] = 5 # isolate workspace rule
        self._mk_task('t-run', segs=('repo',), status='running')
        cand = self._mk_task('t-cand', segs=('repo', 'sub'), status='queued')
        self.assertFalse(self.app._task_admissible(cand))

    def test_disjoint_writers_admissible(self):
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        self._mk_task('t-run', segs=('repo',), status='running')
        cand = self._mk_task('t-cand', segs=('repo2',), status='queued')
        self.assertTrue(self.app._task_admissible(cand))

    def test_verified_readers_concurrent_but_writer_conflicts(self):
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        self._mk_task('t-r1', segs=('repo',), mode='verified_read', status='running')
        r2 = self._mk_task('t-r2', segs=('repo',), mode='verified_read', status='queued')
        self.assertTrue(self.app._task_admissible(r2)) # read/read concurrent
        w = self._mk_task('t-w', segs=('repo',), mode='write', status='queued')
        self.assertFalse(self.app._task_admissible(w)) # write vs read conflicts

    def test_max_concurrency_caps_installation(self):
        self.app.execution.installations['codex-local']['max_concurrency'] = 1
        self._mk_task('t-run', segs=('a',), status='running')
        cand = self._mk_task('t-cand', segs=('b',), status='queued') # disjoint ws
        self.assertFalse(self.app._task_admissible(cand)) # capacity 1 already used

    def test_executor_needs_no_hardcoded_resource(self):
        # Capacity is max_concurrency. No adapter requires a product resource key.
        from pangpang.execution import Execution
        import copy
        cfg = copy.deepcopy(self.app.config)
        cfg.setdefault('hosts', []).append({'id': 'h2', 'transport': 'local', 'os': 'Linux', 'allowed_roots': ['/tmp']})
        cfg.setdefault('installations', []).append({'id': 'q', 'host_id': 'h2', 'adapter': 'codex',
                                                    'argv': ['codex', 'exec', '-'], 'cwd': '/tmp', 'max_concurrency': 2})
        ex = Execution(cfg, self.root / 'exec-q')
        self.assertEqual(ex.installations['q']['max_concurrency'], 2)
        self.assertEqual(ex.installations['q'].get('resource_keys', []), [])

    def test_task_prompt_does_not_authorize_pitfall_rewrites(self):
        self.app.probe('codex-local')
        m = self.app.send('bot-main', {'text': '注意以前的坑', 'allow_write': True}, 'pitfall')
        res = self.app.submit_task('bot-main', m['work_id'], {'prompt': '注意以前的坑'})
        prompt = self.app.store.get('task', res['task_id'])['prompt']
        self.assertIn('PITFALLS.md', prompt)
        self.assertIn('unless this task explicitly asks to record them', prompt)

    def test_unknown_task_quarantines_workspace(self):
        # Acceptance P: an unknown writer keeps its workspace quarantined — an
        # overlapping new task cannot be admitted until it is resolved.
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        self._mk_task('t-unk', segs=('repo',), status='unknown')
        cand = self._mk_task('t-cand', segs=('repo', 'sub'), status='queued')
        self.assertFalse(self.app._task_admissible(cand))
        # once the unknown task reaches a terminal state (operator resolve), the
        # workspace is released and the candidate is admissible.
        unk = self.app.store.get('task', 't-unk')
        unk['status'] = 'cancelled'
        self.app.store.put('task', unk)
        self.assertTrue(self.app._task_admissible(self.app.store.get('task', 't-cand')))

    def test_expired_queue_deadline_not_admissible(self):
        # A task already past its queue_deadline must never be launched, even if the
        # sweep hasn't run yet (P2: admission is the last gate before start()).
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        cand = self._mk_task('t-exp', segs=('repo',), status='queued')
        cand['queue_deadline'] = 1.0 # far past
        self.app.store.put('task', cand)
        self.assertFalse(self.app._task_admissible(self.app.store.get('task', 't-exp')))
        # a future deadline is fine
        cand2 = self._mk_task('t-ok', segs=('repo2',), status='queued')
        cand2['queue_deadline'] = time.time() + 3600
        self.app.store.put('task', cand2)
        self.assertTrue(self.app._task_admissible(self.app.store.get('task', 't-ok')))

    def test_active_task_seen_behind_terminal_backlog(self):
        # Admission queries by active status (not oldest-1000 scan), so a running
        # writer stays visible even behind a large terminal backlog (P1 over-admit).
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        for i in range(60):
            t = self._mk_task('done-%d' % i, segs=('x', str(i)), status='succeeded')
        self._mk_task('t-run', segs=('repo',), status='running')
        cand = self._mk_task('t-cand', segs=('repo', 'sub'), status='queued')
        self.assertFalse(self.app._task_admissible(self.app.store.get('task', 't-cand')))
        # A task waiting past its queue_deadline without starting fails (queue_timeout)
        # and its dependency settles failed.
        self.app.probe('codex-local')
        m = self.app.send('bot-main', {'text': 'x', 'allow_write': True}, 'qd')
        res = self.app.submit_task('bot-main', m['work_id'], {'prompt': 'x'})
        task = self.app.store.get('task', res['task_id'])
        task['queue_deadline'] = 1.0 # far in the past
        self.app.store.put('task', task)
        self.app.sweep_deadlines()
        done = self.app.store.get('task', res['task_id'])
        self.assertEqual(done['status'], 'failed')
        self.assertEqual(done['error'], 'queue_timeout')
        dep = self.app.store.get('dependency', res['dependency_id'])
        self.assertEqual(dep['outcome'], 'failed')


if __name__ == '__main__':
    unittest.main()
