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

    def _mk_task(self, tid, host='local', inst='codex-local', segs=('repo',), mode='write',
                 status='running', execution_id=None):
        # cwd, workspace_key and execution_id are consistent snapshots of the same
        # task: the lock identity is (<workspace>, ".xpp-out", <execution_id>).
        eid = execution_id or tid + '-x'
        cwd = str(self.root / 'ws' / Path(*segs))
        t = {'id': tid, 'status': status, 'host_id': host, 'installation_id': inst,
             'cwd': cwd, 'workspace_key': [host, list(segs)], 'access_mode': mode,
             'resource_keys': [], 'execution_id': eid, 'spec_digest': 'd', 'created': 1.0}
        return self.app.store.put('task', t)

    def test_same_workspace_writers_concurrent(self):
        # Task locking is on the TASK OUTPUT directory, not the whole workspace:
        # two writers in the same repo with different execution ids are concurrent.
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        self._mk_task('t-run', segs=('repo',), execution_id='exec-a', status='running')
        cand = self._mk_task('t-cand', segs=('repo',), execution_id='exec-b', status='queued')
        self.assertTrue(self.app._task_admissible(cand))

    def test_disjoint_workspaces_admissible(self):
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        self._mk_task('t-run', segs=('repo',), status='running')
        cand = self._mk_task('t-cand', segs=('repo2',), status='queued')
        self.assertTrue(self.app._task_admissible(cand))

    def test_true_overlap_conflicts_only_for_write_write(self):
        # A cwd=/repo eid=E1; B cwd=/repo/.xpp-out/E1/sub eid=E2. B works inside
        # A's output directory, so the two directory locks genuinely overlap.
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        inside = ('repo', '.xpp-out', 'exec-a', 'sub')

        def pair(mode_a, mode_b, suffix):
            # Fixed ids: each pair replaces the previous active/candidate snapshot
            # so earlier cases cannot leak a contradictory writer into the scan.
            self._mk_task('t-run', segs=('repo',), mode=mode_a,
                          execution_id='exec-a', status='running')
            cand = self._mk_task('t-cand', segs=inside, mode=mode_b,
                                 execution_id='exec-b-' + suffix, status='queued')
            return cand

        self.assertFalse(self.app._task_admissible(
            pair('write', 'write', 'ww')))
        # write does not block read, in either direction; readers never conflict.
        self.assertTrue(self.app._task_admissible(
            pair('write', 'verified_read', 'wr')))
        self.assertTrue(self.app._task_admissible(
            pair('verified_read', 'write', 'rw')))
        self.assertTrue(self.app._task_admissible(
            pair('verified_read', 'verified_read', 'rr')))

    def test_untrustworthy_identity_not_admissible(self):
        # Missing/invalid frozen identity: no directory lock can be derived, so the
        # task must never be admitted (fail closed).
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        base = self._mk_task('t-base', segs=('repo',), status='queued')
        candidates = []
        missing = dict(base, id='t-no-wk'); missing.pop('workspace_key')
        candidates.append(missing)
        candidates.append(dict(base, id='t-empty-segs', workspace_key=['local', []]))
        candidates.append(dict(base, id='t-bad-host', workspace_key=['', ['repo']]))
        candidates.append(dict(base, id='t-bad-segs', workspace_key=['local', ['repo', 7]]))
        candidates.append(dict(base, id='t-eid-missing', execution_id=None))
        candidates.append(dict(base, id='t-eid-slash', execution_id='a/b'))
        candidates.append(dict(base, id='t-eid-backslash', execution_id='a\\b'))
        candidates.append(dict(base, id='t-eid-dotdot', execution_id='..'))
        for cand in candidates:
            self.assertFalse(self.app._task_admissible(cand), cand['id'])

    def test_unidentified_active_task_occupies_cap_but_not_directory(self):
        # An active task whose lock key is None cannot name a directory, so it must
        # not block an overlapping-looking candidate — but it still occupies capacity.
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        broken = self._mk_task('t-broken', segs=('repo',), status='running')
        broken.pop('workspace_key')
        self.app.store.put('task', broken)
        cand = self._mk_task('t-cand', segs=('repo', 'sub'), status='queued')
        self.assertTrue(self.app._task_admissible(cand))
        # Same active task with cap 1 still counts against max_concurrency.
        self.app.execution.installations['codex-local']['max_concurrency'] = 1
        self.assertFalse(self.app._task_admissible(cand))

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
        # Acceptance P: an unknown writer keeps its TASK OUTPUT directory
        # quarantined — a candidate working inside it cannot be admitted until the
        # unknown task is resolved.
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        self._mk_task('t-unk', segs=('repo',), execution_id='exec-unk', status='unknown')
        cand = self._mk_task('t-cand', segs=('repo', '.xpp-out', 'exec-unk', 'sub'),
                             status='queued')
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
        self._mk_task('t-run', segs=('repo',), execution_id='exec-run', status='running')
        cand = self._mk_task('t-cand', segs=('repo', '.xpp-out', 'exec-run', 'sub'),
                             status='queued')
        self.assertFalse(self.app._task_admissible(self.app.store.get('task', 't-cand')))
        # A task waiting past its queue_deadline without starting fails (queue_timeout)
        # and its dependency settles failed.
        self.app.probe('codex-local')
        m = self.app.send('bot-main', {'text': 'x', 'allow_write': True}, 'qd')
        res = self.app.submit_task('bot-main', m['work_id'], {'prompt': 'x'})
        task = self.app.store.get('task', res['task_id'])
        if task.get('installation_authorization') == 'required':
            self.app.approve(task['id'], {'spec_digest': task['spec_digest']})
            task = self.app.store.get('task', task['id'])
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
