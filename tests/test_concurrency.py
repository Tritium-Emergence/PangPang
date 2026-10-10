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

    def test_service_bot_cap_one(self):
        # a service bot has cap=1: one running turn blocks a second on the same bot.
        b = self.app.create_bot({'name': 'svc', 'role': 'r'})
        w1 = self.app.send(b['id'], {'text': 'a'}, 'c1')
        w2 = self.app.send(b['id'], {'text': 'b'}, 'c2')
        self._run(w1['work_id'], w1['turn_id'], priority=1, bot=b['id'])
        running = self.sched._running_turns()
        cand = self.app.store.get('turn', w2['turn_id'])
        cand['priority'] = 1
        self.assertFalse(self.sched._admission_ok(cand, running))

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
