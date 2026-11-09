import threading
import time
import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.runtime import Runtime
from pangpang.store import Fault


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')
        self.rt = Runtime(self.app)
        self.sched = self.rt.scheduler

    def tearDown(self):
        try:
            self.sched.stop()
        except Exception:
            pass
        self.app.store.close()
        self.tmp.cleanup()

    def _run(self, work_id, tid, status='running', priority=1, bot='bot-main', attempt=1):
        t = self.app.store.get('turn', tid)
        t.update(status=status, priority=priority, bot_id=bot, run_owner_boot_id=self.app.boot_id,
                 run_attempt_id=attempt, started_at=time.time())
        return self.app.store.put('turn', t)

    def test_p0_reservation_blocks_fourth_nonp0(self):
        # 3 non-P0 turns running (of 4 slots, 1 reserved for P0) -> a 4th non-P0
        # cannot claim, but a P0 can.
        self.assertEqual(self.app.scheduler_config['model_max_concurrency'], 4)
        bots = [self.app.create_bot({'name': f'B{i}', 'role': 'r'}) for i in range(4)]
        works = [self.app.send(b['id'], {'text': 'x'}, f'p0-{i}') for i, b in enumerate(bots)]
        # mark first 3 as running non-P0 (each its own bot to avoid per-bot cap)
        for i in range(3):
            self._run(works[i]['work_id'], works[i]['turn_id'], priority=1, bot=bots[i]['id'])
        # the 4th queued non-P0 candidate must NOT be admittable
        running = self.sched._running_turns()
        cand4 = self.app.store.get('turn', works[3]['turn_id'])
        cand4['priority'] = 1
        self.assertFalse(self.sched._admission_ok(cand4, running))
        # a P0 candidate CAN use the reserved slot
        cand4p0 = dict(cand4, priority=0)
        self.assertTrue(self.sched._admission_ok(cand4p0, running))

    def test_service_bot_default_cap_is_four(self):
        # The default service Bot cap is 4, not "one turn per Bot".
        self.assertEqual(self.app.scheduler_config['service_bot_max_active_turns'], 4)
        b = self.app.create_bot({'name': 'svc', 'role': 'r'})
        works = [self.app.send(b['id'], {'text': f'x{i}'}, f'd{i}') for i in range(3)]
        self._run(works[0]['work_id'], works[0]['turn_id'], priority=1, bot=b['id'])
        self._run(works[1]['work_id'], works[1]['turn_id'], priority=1, bot=b['id'])
        running = self.sched._running_turns()
        cand = self.app.store.get('turn', works[2]['turn_id'])
        cand['priority'] = 1
        self.assertEqual(self.sched._bot_cap(b), 4)
        # Two different Works already run; a third on the same Bot is admitted
        # (with the old cap 1 it could never be).
        self.assertTrue(self.sched._admission_ok(cand, running))

    def test_service_bot_cap_one_explicit_still_serializes(self):
        # An explicitly stored cap=1 keeps the old behaviour.
        self.sched.config['service_bot_max_active_turns'] = 1
        b = self.app.create_bot({'name': 'svc', 'role': 'r'})
        w1 = self.app.send(b['id'], {'text': 'a'}, 'c1')
        w2 = self.app.send(b['id'], {'text': 'b'}, 'c2')
        self._run(w1['work_id'], w1['turn_id'], priority=1, bot=b['id'])
        running = self.sched._running_turns()
        cand = self.app.store.get('turn', w2['turn_id'])
        cand['priority'] = 1
        self.assertFalse(self.sched._admission_ok(cand, running))

    def test_same_work_still_rejects_a_second_running_turn(self):
        # The per-Bot cap widened, but the Work write boundary did not.
        b = self.app.create_bot({'name': 'svc', 'role': 'r'})
        w = self.app.send(b['id'], {'text': 'a'}, 'same-work')
        self._run(w['work_id'], w['turn_id'], priority=1, bot=b['id'])
        with self.assertRaises(Fault):
            self.app.store.put('turn', {'id': 'turn-other', 'work_id': w['work_id'],
                                        'bot_id': b['id'], 'status': 'running'})

    def test_main_bot_cap_two_with_p0_reserve(self):
        # main bot cap=2, reserved=1: one non-P0 main turn running -> a 2nd non-P0
        # main turn is blocked (2nd slot reserved for P0), but a P0 main turn is ok.
        w1 = self.app.send('bot-main', {'text': 'a'}, 'm1')
        w2 = self.app.send('bot-main', {'text': 'b'}, 'm2')
        self._run(w1['work_id'], w1['turn_id'], priority=1, bot='bot-main')
        running = self.sched._running_turns()
        cand = self.app.store.get('turn', w2['turn_id']); cand['priority'] = 1
        self.assertFalse(self.sched._admission_ok(cand, running))
        candp0 = dict(cand, priority=0)
        self.assertTrue(self.sched._admission_ok(candp0, running))

    def test_real_parallel_model_calls_reach_barrier(self):
        # Acceptance B shape (without a real Node worker): 3 different Works must be
        # in a model call SIMULTANEOUSLY. A barrier model proves the scheduler runs
        # them in parallel rather than serializing.
        n = 3
        barrier = threading.Barrier(n, timeout=5)
        reached = []
        lock = threading.Lock()

        class BarrierModel:
            def complete(self, profile, messages, tools):
                with lock:
                    reached.append(1)
                barrier.wait() # all n must arrive together or this times out
                return {'content': '完成'}, {}

        bots = [self.app.create_bot({'name': f'W{i}', 'role': 'r'}) for i in range(n)]
        for i, b in enumerate(bots):
            self.app.send(b['id'], {'text': 'x'}, f'bar-{i}')
        rt = Runtime(self.app, BarrierModel())
        sched = rt.scheduler
        # drive the pool directly: claim+submit n turns, then let the pool run them
        import concurrent.futures
        sched._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            for _ in range(n):
                t = sched.claim_one()
                self.assertIsNotNone(t)
                sched._submit(t)
            # wait for all to finish
            deadline = time.time() + 8
            while time.time() < deadline and len(reached) < n:
                time.sleep(0.05)
            self.assertEqual(len(reached), n) # all reached the barrier concurrently
        finally:
            sched._pool.shutdown(wait=True)

    def test_same_service_bot_different_works_reach_barrier(self):
        # The point of the B1 default: one service Bot, three different Works,
        # three model calls in flight at the same time.
        n = 3
        barrier = threading.Barrier(n, timeout=5)
        reached = []
        lock = threading.Lock()

        class BarrierModel:
            def complete(self, profile, messages, tools):
                with lock:
                    reached.append(1)
                barrier.wait() # all n must arrive together or this times out
                return {'content': '完成'}, {}

        bot = self.app.create_bot({'name': 'svc', 'role': 'r'})
        for i in range(n):
            self.app.send(bot['id'], {'text': f'x{i}'}, f'same-bot-{i}')
        rt = Runtime(self.app, BarrierModel())
        sched = rt.scheduler
        import concurrent.futures
        sched._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            claimed = []
            for _ in range(n):
                t = sched.claim_one()
                self.assertIsNotNone(t)
                self.assertEqual(t['bot_id'], bot['id'])
                claimed.append(t)
            self.assertEqual(len({t['work_id'] for t in claimed}), n)
            for t in claimed:
                sched._submit(t)
            deadline = time.time() + 8
            while time.time() < deadline and len(reached) < n:
                time.sleep(0.05)
            self.assertEqual(len(reached), n)
        finally:
            sched._pool.shutdown(wait=True)

    def test_shared_bot_state_patch_rejects_a_stale_revision(self):
        # Parallel Works share one Bot state; the CAS, not a lock, is the guard.
        bot = self.app.create_bot({'name': 'svc', 'role': 'r'})
        state = self.app.store.get('state', bot['id'])
        self.app.state_patch(bot['id'], {'revision': state['revision'], 'open_items': ['from work a']})
        with self.assertRaises(Fault) as caught:
            self.app.state_patch(bot['id'], {'revision': state['revision'], 'open_items': ['from work b']})
        self.assertEqual(caught.exception.code, 'state_conflict')


    def test_hard_timeout_flag_routes_to_recover_not_block(self):
        # A hard-timed-out turn whose run raises (killed proc) must route to
        # recover_turn (requeue/continue), NOT a plain block.
        m = self.app.send('bot-main', {'text': 'x'}, 'ht')
        t = self.sched.claim_one()
        self.assertIsNotNone(t)
        self.sched._timed_out.add((t['id'], t.get('run_attempt_id', 0)))

        class Killed:
            def complete(self, *a):
                raise Fault('pi_worker_failed')
        rt = Runtime(self.app, Killed())
        self.app.config['turn_retry_delays_s'] = []
        # run via the SAME scheduler that holds the timeout mark
        self.sched.runtime = rt
        self.sched._run_claimed(self.app.store.get('turn', t['id']))
        turn = self.app.store.get('turn', t['id'])
        self.assertEqual(turn['status'], 'queued')
        self.assertFalse(self.sched.hard_timed_out(turn)) # cleared after recover

    def test_sweep_marks_and_kills_overdue_turn(self):
        import time as _t
        m = self.app.send('bot-main', {'text': 'x'}, 'sw')
        t = self.sched.claim_one()
        t['started_at'] = _t.time() - 999999
        self.app.store.put('turn', t)
        self.app.config['pi_turn_hard_timeout_s'] = 1
        self.sched.sweep_hard_timeouts()
        self.assertTrue(self.sched.hard_timed_out(self.app.store.get('turn', t['id'])))


if __name__ == '__main__':
    unittest.main()
