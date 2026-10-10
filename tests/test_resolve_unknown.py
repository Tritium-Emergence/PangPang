import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class ResolveUnknownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _mk_task(self, tid, status, segs=('repo',), mode='write'):
        exec_id = 'exec-%032x' % (abs(hash(tid)) % (16 ** 32))
        t = {'id': tid, 'status': status, 'host_id': 'local', 'installation_id': 'codex-local',
             'workspace_key': ['host-local', list(segs)], 'access_mode': mode, 'resource_keys': [],
             'execution_id': exec_id, 'spec_digest': 'd', 'created': 1.0, 'owner_bot_id': 'bot-main',
             'work_id': 'w-' + tid, 'prompt': 'p', 'inputs': [], 'outputs': []}
        return self.app.store.put('task', t)

    def test_unknown_quarantines_then_resolve_releases_workspace(self):
        # An unknown writer quarantines its workspace: an overlapping queued task is
        # NOT admissible. After operator resolve (evidence), the task leaves unknown
        # and the overlapping task becomes admissible again.
        self.app.execution.installations['codex-local']['max_concurrency'] = 5
        self._mk_task('t-unknown', 'unknown', segs=('repo',))
        cand = self._mk_task('t-cand', 'queued', segs=('repo', 'sub'))
        self.assertFalse(self.app._task_admissible(self.app.store.get('task', 't-cand')))
        # operator resolves the unknown task (local transport -> immediate complete)
        self.app.resolve_task('t-unknown', {'resolution': 'confirmed_stopped',
                                            'evidence': '已确认执行器进程不存在，目录无锁文件。'})
        done = self.app.store.get('task', 't-unknown')
        self.assertIn(done['status'], ('cancelled', 'failed', 'succeeded')) # left unknown -> terminal
        self.assertNotEqual(done['status'], 'unknown')
        # workspace released: the overlapping candidate is now admissible
        self.assertTrue(self.app._task_admissible(self.app.store.get('task', 't-cand')))

    def test_resolve_requires_valid_resolution_and_evidence(self):
        self._mk_task('t-u2', 'unknown')
        with self.assertRaises(Fault) as cm:
            self.app.resolve_task('t-u2', {'resolution': 'confirmed_stopped', 'evidence': '太短'})
        self.assertEqual(cm.exception.code, 'operator_evidence_required')
        with self.assertRaises(Fault) as cm:
            self.app.resolve_task('t-u2', {'resolution': 'bogus', 'evidence': '足够长的判断依据说明文字'})
        self.assertEqual(cm.exception.code, 'operator_evidence_required')

    def test_resolve_only_on_unknown(self):
        self._mk_task('t-run', 'running')
        with self.assertRaises(Fault) as cm:
            self.app.resolve_task('t-run', {'resolution': 'confirmed_stopped', 'evidence': '足够长的判断依据说明'})
        self.assertEqual(cm.exception.code, 'task_not_unknown')


if __name__ == '__main__':
    unittest.main()
