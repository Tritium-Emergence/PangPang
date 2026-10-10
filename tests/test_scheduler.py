import threading
import time
import unittest

from pangpang.__main__ import install_routine_scheduler_gate


class RoutineSchedulerGateTests(unittest.TestCase):
    def test_overlapping_scheduler_tick_is_skipped(self):
        class Service:
            def __init__(self):
                self.calls = 0
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def routine_step(self):
                with self.lock:
                    self.calls += 1
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.05)
                with self.lock:
                    self.active -= 1
                return True

        service = Service()
        install_routine_scheduler_gate(service)
        barrier = threading.Barrier(3)
        results = []

        def tick():
            barrier.wait()
            results.append(service.routine_step())

        first = threading.Thread(target=tick)
        second = threading.Thread(target=tick)
        first.start()
        second.start()
        barrier.wait()
        first.join()
        second.join()

        self.assertEqual(service.max_active, 1)
        self.assertEqual(service.calls, 1)
        self.assertEqual(sorted(results), [False, True])
        self.assertTrue(service.routine_step())
        self.assertEqual(service.calls, 2)


if __name__ == "__main__":
    unittest.main()
