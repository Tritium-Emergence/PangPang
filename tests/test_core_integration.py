"""1.0-core five-line integration: invariants that live on
the seam between two lines and are therefore owned by neither line's own suite."""
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config
from test_routine_standing_authority import DAILY
from pangpang.service import Service
from pangpang.store import Fault

ROOT = Path(__file__).resolve().parents[1]


class RoutineContinuationSeamTests(unittest.TestCase):
    """ x: a Routine Work carries the operator-approved standing authority
    envelope; the explicit Web continuation must never become a path by which a
    user message joins (or forks) that Work and runs under the Routine's envelope."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')
        routine = self.app.create_routine('bot-main', {
            'instruction': '日报', 'timezone': 'UTC', 'schedule': DAILY,
            'authority': {'grant_write': True, 'required_host_id': 'local'}})
        self.app.run_routine_now('bot-main', routine['id'])
        self.work = next(w for w in self.app.store.find('work', owner_bot_id='bot-main')
                         if w.get('kind') == 'routine')
        # Precondition: the Work really carries standing write authority.
        self.assertTrue(self.work['grant_write'])

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _assert_not_continuable(self):
        before = self.app.store.count('work', owner_bot_id='bot-main')
        body = {'text': '继续', 'continue_work_id': self.work['id'], 'stream_id': 'tab-x'}
        with self.assertRaises(Fault) as caught:
            self.app.resolve_continuation('bot-main', body)
        self.assertEqual(caught.exception.code, 'continue_work_not_continuable')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.app.store.count('work', owner_bot_id='bot-main'), before)

    def test_active_routine_work_is_not_explicitly_continuable(self):
        self._assert_not_continuable()

    def test_delivered_routine_work_is_not_forked_by_explicit_continuation(self):
        work = self.app.store.get('work', self.work['id'])
        work['status'] = 'delivered'
        self.app.store.put('work', work)
        self._assert_not_continuable()


if __name__ == '__main__':
    unittest.main()
