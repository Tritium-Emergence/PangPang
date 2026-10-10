import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service


class CheckpointNoRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_checkpoint_without_revision_succeeds(self):
        # Regression: checkpoint_work used to require a model-supplied `revision` and
        # CAS on it, but the model is never shown the work revision (model_work_view
        # omits it) -> unsatisfiable state_conflict loop (the "3-minute create-a-bot"
        # hang). It must now succeed with no revision at all.
        m = self.app.send('bot-main', {'text': '建个bot'}, 'ck1')
        res = self.app.checkpoint_work('bot-main', m['work_id'],
            {'phase': '完成', 'completed': ['建好了 Bot']})
        self.assertEqual(res['checkpoint']['phase'], '完成')
        self.assertEqual(res['checkpoint']['completed'], ['建好了 Bot'])

    def test_repeated_checkpoint_never_conflicts(self):
        # Three back-to-back checkpoints (the exact death-loop shape) all succeed.
        m = self.app.send('bot-main', {'text': 'x'}, 'ck2')
        wid = m['work_id']
        for phase in ('一', '二', '三'):
            r = self.app.checkpoint_work('bot-main', wid, {'phase': phase})
            self.assertEqual(r['checkpoint']['phase'], phase)

    def test_stale_revision_ignored_not_rejected(self):
        # A model that still sends a (stale) revision is not punished — it is ignored.
        m = self.app.send('bot-main', {'text': 'y'}, 'ck3')
        wid = m['work_id']
        self.app.checkpoint_work('bot-main', wid, {'phase': 'first'})
        r = self.app.checkpoint_work('bot-main', wid, {'revision': 0, 'phase': 'second'})
        self.assertEqual(r['checkpoint']['phase'], 'second')

    def test_checkpoint_does_not_clobber_concurrent_field(self):
        # The atomic re-read-inside-txn (rule A) means checkpoint only touches the
        # checkpoint field: a change made to another field between a stale read and
        # the write is preserved. Simulate by mutating status on the stored work,
        # then checkpointing from a stale in-memory copy's work_id.
        m = self.app.send('bot-main', {'text': 'z'}, 'ck4')
        wid = m['work_id']
        # external field change lands first
        w = self.app.store.get('work', wid)
        w['status'] = 'waiting'
        w['pending_dep_ids'] = ['dep-external']
        self.app.store.put('work', w)
        # checkpoint (re-reads inside txn) must keep status/pending_dep_ids intact
        self.app.checkpoint_work('bot-main', wid, {'phase': 'p'})
        after = self.app.store.get('work', wid)
        self.assertEqual(after['status'], 'waiting')
        self.assertEqual(after['pending_dep_ids'], ['dep-external'])
        self.assertEqual(after['checkpoint']['phase'], 'p')

    def test_wrong_owner_still_rejected(self):
        from pangpang.store import Fault
        other = self.app.create_bot({'name': '别人', 'role': 'r'})
        m = self.app.send('bot-main', {'text': 'w'}, 'ck5')
        with self.assertRaises(Fault) as cm:
            self.app.checkpoint_work(other['id'], m['work_id'], {'phase': 'x'})
        self.assertEqual(cm.exception.code, 'work_forbidden')


if __name__ == '__main__':
    unittest.main()
