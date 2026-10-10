import tempfile
import time
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service


class LiveDeltaTests(unittest.TestCase):
    """Streaming buffer (UI-only, ephemeral). Verifies the invariants codex flagged:
    rev-monotonic (not text-length), (turn_id, attempt) isolation, done-tombstone +
    TTL cleanup, per-bot filtering."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_rev_monotonic_not_length(self):
        # A post-tool-round assistant message restarts from empty text, so a shorter
        # text with a HIGHER rev must win; a stale lower rev must be ignored.
        self.app.record_delta('turn-1', 0, '你好，我来帮你', 3)
        # a stale rev (<= current) is dropped
        self.app.record_delta('turn-1', 0, '你好，我来帮你啊啊啊', 2)
        with self.app.live_lock:
            self.assertEqual(self.app.live_deltas[('turn-1', 0)]['text'], '你好，我来帮你')
            self.assertEqual(self.app.live_deltas[('turn-1', 0)]['rev'], 3)
        # a higher rev with SHORTER text wins (new message after a tool round)
        self.app.record_delta('turn-1', 0, '好', 4)
        with self.app.live_lock:
            self.assertEqual(self.app.live_deltas[('turn-1', 0)]['text'], '好')
            self.assertEqual(self.app.live_deltas[('turn-1', 0)]['rev'], 4)

    def test_attempt_isolation(self):
        # A retry (attempt=1) must not clobber the old attempt=0 entry.
        self.app.record_delta('turn-2', 0, 'old attempt', 1)
        self.app.record_delta('turn-2', 1, 'new attempt', 1)
        with self.app.live_lock:
            self.assertEqual(self.app.live_deltas[('turn-2', 0)]['text'], 'old attempt')
            self.assertEqual(self.app.live_deltas[('turn-2', 1)]['text'], 'new attempt')

    def test_done_tombstone_then_ttl_sweep(self):
        # finish_delta marks done (kept as a tombstone so the SSE can emit one 'done');
        # snapshot sweeps it only after the TTL.
        m = self.app.send('bot-main', {'text': 'hi'}, 'ld')
        turn = self.app.store.find('turn', work_id=m['work_id'])[0]
        bot_id = turn['bot_id']
        self.app.record_delta(turn['id'], 0, 'partial', 1)
        # visible to its bot
        self.assertIn((turn['id'], 0), self.app.snapshot_deltas(bot_id))
        self.app.finish_delta(turn['id'], 0)
        # still present immediately (tombstone), marked done
        snap = self.app.snapshot_deltas(bot_id)
        self.assertTrue(snap[(turn['id'], 0)]['done'])
        # force the tombstone past TTL -> swept on next snapshot
        with self.app.live_lock:
            self.app.live_deltas[(turn['id'], 0)]['ts'] = time.time() - 10
        self.assertNotIn((turn['id'], 0), self.app.snapshot_deltas(bot_id))
        with self.app.live_lock:
            self.assertNotIn((turn['id'], 0), self.app.live_deltas)

    def test_per_bot_filtering(self):
        # snapshot only returns the queried bot's entries.
        a = self.app.send('bot-main', {'text': 'a'}, 'lda')
        turn_a = self.app.store.find('turn', work_id=a['work_id'])[0]
        self.app.record_delta(turn_a['id'], 0, 'text-a', 1)
        # a fabricated entry for another bot must not leak
        self.app.record_delta('turn-other', 0, 'text-other', 1)
        with self.app.live_lock:
            self.app.live_deltas[('turn-other', 0)]['bot_id'] = 'bot-someone-else'
        snap = self.app.snapshot_deltas(turn_a['bot_id'])
        self.assertIn((turn_a['id'], 0), snap)
        self.assertNotIn(('turn-other', 0), snap)

    def test_empty_turn_id_ignored(self):
        self.app.record_delta('', 0, 'x', 1)
        self.app.record_delta(None, 0, 'x', 1)
        with self.app.live_lock:
            self.assertEqual(len(self.app.live_deltas), 0)

    def test_finish_without_prior_delta_blocks_straggler(self):
        # A turn that finished before any delta was recorded (fast/empty answer) must
        # leave a done tombstone, so a straggler delta arriving after finish cannot
        # start a phantom stream for an already-finished turn.
        self.app.finish_delta('turn-fast', 0)
        with self.app.live_lock:
            self.assertTrue(self.app.live_deltas[('turn-fast', 0)]['done'])
        # a late delta must NOT resurrect it
        self.app.record_delta('turn-fast', 0, 'late token', 1)
        with self.app.live_lock:
            self.assertTrue(self.app.live_deltas[('turn-fast', 0)]['done'])
            self.assertEqual(self.app.live_deltas[('turn-fast', 0)]['text'], '')

    def test_invalid_rev_ignored(self):
        self.app.record_delta('turn-x', 0, 'x', None)
        self.app.record_delta('turn-x', 0, 'x', 'nope')
        with self.app.live_lock:
            self.assertEqual(len(self.app.live_deltas), 0)


if __name__ == '__main__':
    unittest.main()
