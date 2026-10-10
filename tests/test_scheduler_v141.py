import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.runtime import Runtime
from pangpang.store import Fault


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')
        self.rt = Runtime(self.app) # PiModel; we drive claim_one directly
        self.sched = self.rt.scheduler

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_config_validation(self):
        # default concurrency is 4 with 1 reserved P0 slot.
        self.assertEqual(self.app.scheduler_config['model_max_concurrency'], 4)
        self.assertEqual(self.app.scheduler_config['reserved_user_slots'], 1)
        # Validate the rule directly (avoid leaking half-built Services on Windows,
        # where an exception mid-__init__ leaves the vault db handle open).
        self.app.config['scheduler'] = {'model_max_concurrency': 1, 'reserved_user_slots': 1}
        with self.assertRaises(Fault):
            self.app._scheduler_config()
        self.app.config['scheduler'] = {'model_max_concurrency': 4, 'reserved_user_slots': 4}
        with self.assertRaises(Fault):
            self.app._scheduler_config()
        self.app.config['scheduler'] = {'model_max_concurrency': 0}
        with self.assertRaises(Fault):
            self.app._scheduler_config()
        # valid: max>1 with reserved<max
        self.app.config['scheduler'] = {'model_max_concurrency': 4, 'reserved_user_slots': 1}
        self.assertEqual(self.app._scheduler_config()['reserved_user_slots'], 1)

    def test_boot_id_written_on_claim(self):
        m = self.app.send('bot-main', {'text': '活'}, 'boot')
        turn = self.sched.claim_one()
        self.assertIsNotNone(turn)
        self.assertEqual(turn['status'], 'running')
        self.assertEqual(turn['run_owner_boot_id'], self.app.boot_id)
        self.assertIn('started_at', turn)

    def test_claim_cas_only_one_wins(self):
        # Once claimed (running), a second claim finds nothing runnable (same-work
        # running guard + partial index).
        self.app.send('bot-main', {'text': '活'}, 'cas')
        first = self.sched.claim_one()
        self.assertIsNotNone(first)
        second = self.sched.claim_one()
        self.assertIsNone(second)

    def test_priority_order_p0_before_p3(self):
        # A routine (P3) and a direct user message (P0): user claims first.
        routine = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'Asia/Shanghai',
                                                        'schedule': {'type': 'daily', 'time': '05:00'}})
        self.app.run_routine_now('bot-main', routine['id'])
        direct = self.app.send('bot-main', {'text': '先回答我'}, 'p0')
        turn = self.sched.claim_one()
        self.assertEqual(turn['work_id'], direct['work_id'])
        self.assertEqual(int(turn['priority']), 0)

    def test_not_before_respected(self):
        import time
        m = self.app.send('bot-main', {'text': '活'}, 'nb')
        turn = self.app.store.get('turn', m['turn_id'])
        turn['not_before'] = time.time() + 3600
        self.app.store.put('turn', turn)
        self.assertIsNone(self.sched.claim_one()) # not yet runnable

    def test_runnable_excludes_waiting_work_normal_turn(self):
        # A normal turn on a work with pending deps is not runnable.
        e1 = self.app.create_bot({'name': 'A', 'role': 'r'})
        m = self.app.send('bot-main', {'text': '活'}, 'wt')
        # consume the initial user turn
        for t in self.app.store.find('turn', work_id=m['work_id']):
            if t['status'] == 'queued':
                t['status'] = 'done'
                self.app.store.put('turn', t)
        self.app.delegate('bot-main', m['work_id'],
                          {'target_bot_id': e1['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        # parent is waiting; its only would-be turn is the (done) user turn.
        # There should be no runnable turn for the parent work now.
        cand = [t for t in self.sched._candidates(__import__('time').time())
                if t['work_id'] == m['work_id']]
        self.assertEqual(cand, [])

    def test_next_deadline_ignores_overdue(self):
        import time
        # an overdue not_before must not appear as a future wake deadline
        m = self.app.send('bot-main', {'text': '活'}, 'od')
        turn = self.app.store.get('turn', m['turn_id'])
        turn['not_before'] = time.time() - 100
        self.app.store.put('turn', turn)
        dl = self.sched._next_deadline(time.time())
        self.assertTrue(dl is None or dl > time.time())

    def test_success_turn_gets_metrics(self):
        # A completed turn must carry queue_wait_ms/model_duration_ms/finished_at
        # persisted to the store (scheduler observability).
        m = self.app.send('bot-main', {'text': 'x'}, 'mtr')

        class Trivial:
            def complete(self, profile, messages, tools):
                return {'content': '完成'}, {}
        Runtime(self.app, Trivial()).scheduler.run_one_sync()
        turn = self.app.store.get('turn', m['turn_id'])
        self.assertEqual(turn['status'], 'done')
        self.assertIn('finished_at', turn)
        self.assertIn('queue_wait_ms', turn)
        self.assertIn('model_duration_ms', turn)

    def test_claim_does_not_busy_loop_on_stale_candidates(self):
        # many queued turns whose work is already running (or terminal)
        # are non-runnable; claim_one filters them via _is_runnable and returns in a
        # bounded pass without spinning on any single stale candidate.
        import time
        # one runnable turn on its own work, plus 50 turns whose work is 'delivered'
        good = self.app.send('bot-main', {'text': '真活'}, 'good')
        for i in range(50):
            dead = self.app.store.put('work', {'id': 'w-dead-%02d' % i, 'owner_bot_id': 'bot-main',
                'kind': 'chat', 'status': 'delivered', 'goal': 'g', 'created': time.time()})
            self.app.store.put('turn', {'id': 't-dead-%02d' % i, 'work_id': dead['id'],
                'bot_id': 'bot-main', 'status': 'queued', 'priority': 2, 'created': time.time(),
                'history': [], 'message_id': None})
        # exactly the runnable one is claimed; the 50 stale candidates are skipped.
        cands = self.sched._candidates(time.time())
        self.assertTrue(all(c['work_id'] == good['work_id'] for c in cands))
        turn = self.sched.claim_one()
        self.assertEqual(turn['work_id'], good['work_id'])

    def test_safety_wake_finds_runnable_after_lost_notify(self):
        # even if a notify is lost, a runnable turn/deadline is still
        # discoverable — the dispatcher's 1s safety tick re-scans. We assert the
        # discovery predicate directly (no dependence on a lost wake event): a fresh
        # queued turn is a claim candidate without any notify() call.
        import time
        m = self.app.send('bot-main', {'text': '活'}, 'safety')
        # do NOT call self.sched.notify(); the safety tick path re-scans regardless.
        cands = self.sched._candidates(time.time())
        self.assertIn(m['work_id'], [c['work_id'] for c in cands])
        self.assertIsNotNone(self.sched.claim_one())

    def test_idle_deadline_uses_soonest_future_and_skips_turn_history(self):
        import json
        import time
        from unittest.mock import patch
        marker = "IDLE-HISTORY-MARKER-NOT-DESERIALIZED"
        now = time.time()
        sent = self.app.send('bot-main', {'text': '活'}, 'idle-dl')
        turn = self.app.store.get('turn', sent['turn_id'])
        turn['history'] = [{"role": "user", "content": marker}]
        self.app.store.put('turn', turn)
        seen = []
        real_loads = json.loads
        def spy_loads(text, *args, **kwargs):
            if marker in text:
                seen.append(text)
            return real_loads(text, *args, **kwargs)
        finds = []
        real_find = self.app.store.find
        def spy_find(kind, *args, **kwargs):
            finds.append((kind, kwargs.get("omit")))
            return real_find(kind, *args, **kwargs)
        soon = []
        real_soon = self.app.store.soonest_future
        def spy_soon(*args, **kwargs):
            soon.append(args)
            return real_soon(*args, **kwargs)
        self.app.store.find = spy_find
        self.app.store.soonest_future = spy_soon
        try:
            with patch('pangpang.store.json.loads', spy_loads):
                cands = self.sched._candidates(now)
                self.assertTrue(any(c['id'] == turn['id'] for c in cands))
                self.assertTrue(all('history' not in c for c in cands))
                turn['not_before'] = now + 100
                self.app.store.put('turn', turn)
                self.app.store.put('dependency', {
                    'id': 'dep-idle', 'source_work_id': sent['work_id'], 'status': 'pending',
                    'deadline': now + 40})
                self.app.store.put('dependency', {
                    'id': 'dep-overdue', 'source_work_id': sent['work_id'], 'status': 'pending',
                    'deadline': now - 10})
                dl = self.sched._next_deadline(now)
                turn['not_before'] = now + 10
                self.app.store.put('turn', turn)
                self.app.store.put('dependency', {
                    'id': 'dep-idle', 'source_work_id': sent['work_id'], 'status': 'pending',
                    'deadline': now + 80})
                nearer = self.sched._next_deadline(now)
        finally:
            self.app.store.find = real_find
            self.app.store.soonest_future = real_soon
        self.assertAlmostEqual(dl, now + 40, delta=1e-3)
        self.assertAlmostEqual(nearer, now + 10, delta=1e-3)
        self.assertEqual(seen, [])
        self.assertTrue(soon)
        self.assertTrue(all(call[:3] == ('dependency', 'pending', 'deadline') for call in soon))
        self.assertNotIn('dependency', [kind for kind, _ in finds])
        self.assertTrue(any(kind == 'turn' and omit == ('history',) for kind, omit in finds))

    def test_order_key_does_not_read_turn_blobs(self):
        import json
        from unittest.mock import patch
        marker = "ORDER-KEY-BLOB-MARKER"
        bot = 'bot-fair'
        with self.app.store.atomic():
            self.app.store.put('turn', {
                'id': 't-fair-old', 'bot_id': bot, 'status': 'done',
                'started_at': 9000.0, 'created': 1.0,
                'history': ['hist'], 'blob_marker': marker})
            for i in range(200):
                self.app.store.put('turn', {
                    'id': 't-fair-%03d' % i, 'bot_id': bot, 'status': 'done',
                    'started_at': 1.0, 'created': 2.0 + i,
                    'history': ['hist']})
        seen = []
        real_loads = json.loads
        def spy_loads(text, *args, **kwargs):
            if marker in text:
                seen.append(text)
            return real_loads(text, *args, **kwargs)
        sqls = []
        self.app.store.db.set_trace_callback(sqls.append)
        try:
            with patch('pangpang.store.json.loads', spy_loads):
                key = self.sched._order_key({'priority': 2, 'bot_id': bot, 'created': 3})
        finally:
            self.app.store.db.set_trace_callback(None)
        self.assertEqual(key[0], 2)
        self.assertEqual(key[1], 9000.0)
        self.assertEqual(seen, [])
        self.assertTrue(any('$.started_at' in sql and 'LIMIT 1' in sql for sql in sqls), sqls)
        self.assertFalse(any('SELECT data' in sql or 'json_remove' in sql for sql in sqls), sqls)


if __name__ == '__main__':
    unittest.main()
