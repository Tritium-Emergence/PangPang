"""Same-routine coalesce: do not stack an empty supervisor Work.

A due routine records outcome=coalesced and advances next_run_at when that
routine already has an unfinished supervisor turn, or a real task that is
awaiting approval, unknown, or starting/running on a connector that is offline
now. It does not stamp last_run_at. A manual run_now in that state is an
error, not a success. An online in-progress task can still be reviewed, and
another routine is not frozen.
"""
import tempfile
import time
import unittest
from pathlib import Path

from test_bot_runtime import config

from pangpang.service import Service
from pangpang.store import Fault


class RoutineCoalesceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.app.execution.hosts["pc"] = {
            "id": "pc", "transport": "connector", "os": "Linux",
            "allowed_roots": [str(self.root)],
        }
        self.app.store.put("device", {"id": "pc", "revoked": False})

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _routine(self, instruction):
        return self.app.create_routine("bot-main", {
            "instruction": instruction,
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "interval", "minutes": 60},
        })

    def _due(self, routine):
        row = self.app.store.get("routine", routine["id"])
        row["next_run_at"] = time.time() - 5
        return self.app.store.put("routine", row)

    def _works(self, routine_id):
        return self.app.store.find("work", routine_id=routine_id, limit=50)

    def _runs(self, routine_id):
        return self.app.store.find("routine_run", routine_id=routine_id, limit=20)

    def _finish_turns(self, routine_id):
        for work in self._works(routine_id):
            for turn in self.app.store.find("turn", work_id=work["id"], limit=20):
                if turn.get("status") in {"queued", "running", "starting"}:
                    turn["status"] = "done"
                    self.app.store.put("turn", turn)

    def test_blocked_routine_advances_without_a_new_work_or_a_fake_run(self):
        routine = self._routine("日报")
        self._due(routine)
        self.assertTrue(self.app.routine_step())
        works = self._works(routine["id"])
        self.assertEqual(len(works), 1)
        self.assertIsNone(works[0].get("parent_work_id"))
        first = self.app.store.get("routine", routine["id"])
        last = first["last_run_at"]
        self.assertIsNotNone(last)

        self._due(routine)
        self.assertTrue(self.app.routine_step())
        self.assertEqual(len(self._works(routine["id"])), 1)
        held = self.app.store.get("routine", routine["id"])
        self.assertEqual(held["last_run_at"], last)
        self.assertGreater(held["next_run_at"], time.time())
        self.assertTrue(any(
            run.get("outcome") == "coalesced" and run.get("coalesce_reason") == "supervisor_open"
            for run in self._runs(routine["id"])))
        self.assertIsNone(held.get("last_manual_run_at"))
        with self.assertRaises(Fault) as caught:
            self.app.run_routine_now("bot-main", routine["id"])
        self.assertEqual(caught.exception.code, "routine_coalesced")
        self.assertEqual(caught.exception.reason, "supervisor_open")
        after_manual = self.app.store.get("routine", routine["id"])
        self.assertIsNone(after_manual.get("last_manual_run_at"))
        self.assertEqual(after_manual["last_run_at"], last)
        self.assertEqual(len(self._works(routine["id"])), 1)

        self._finish_turns(routine["id"])
        self.app.store.put("task", {
            "id": "task-approve", "work_id": works[0]["id"], "owner_bot_id": "bot-main",
            "status": "awaiting_approval", "host_id": "local", "created": time.time(),
        })
        self._due(routine)
        self.app.routine_step()
        self.assertEqual(len(self._works(routine["id"])), 1)
        self.assertEqual(self.app.store.get("routine", routine["id"])["last_run_at"], last)
        self.assertTrue(any(run.get("coalesce_reason") == "awaiting_approval" for run in self._runs(routine["id"])))

        waiting = self.app.store.get("task", "task-approve")
        waiting["status"] = "unknown"
        self.app.store.put("task", waiting)
        self._due(routine)
        self.app.routine_step()
        self.assertEqual(len(self._works(routine["id"])), 1)
        self.assertTrue(any(run.get("coalesce_reason") == "unknown" for run in self._runs(routine["id"])))
        self.assertEqual(self.app.store.get("routine", routine["id"])["last_run_at"], last)

        waiting = self.app.store.get("task", "task-approve")
        waiting["status"] = "succeeded"
        self.app.store.put("task", waiting)
        self.app.store.put("task", {
            "id": "task-off", "work_id": works[0]["id"], "owner_bot_id": "bot-main",
            "status": "running", "host_id": "pc", "created": time.time(),
            "last_observation_error": None,
        })
        self.app.broker._last_seen.pop("pc", None)
        self.assertFalse(self.app.broker.online("pc"))
        self._due(routine)
        self.app.routine_step()
        self.assertEqual(len(self._works(routine["id"])), 1)
        self.assertTrue(any(run.get("coalesce_reason") == "unreachable" for run in self._runs(routine["id"])))
        self.assertEqual(self.app.store.get("routine", routine["id"])["last_run_at"], last)

        # Stale connector_offline must not block stage review once the device is online.
        offline = self.app.store.get("task", "task-off")
        offline["last_observation_error"] = "connector_offline"
        self.app.store.put("task", offline)
        self.app.broker._last_seen["pc"] = time.time()
        self.assertTrue(self.app.broker.online("pc"))
        self._due(routine)
        self.assertTrue(self.app.routine_step())
        reviewed = self._works(routine["id"])
        self.assertEqual(len(reviewed), 2)
        self.assertTrue(all(work.get("parent_work_id") is None for work in reviewed))
        self.assertGreater(self.app.store.get("routine", routine["id"])["last_run_at"], last)

    def test_other_routines_and_online_stage_review_are_not_frozen(self):
        blocked = self._routine("甲")
        other = self._routine("乙")
        self._due(blocked)
        self.app.routine_step()
        self.assertEqual(len(self._works(blocked["id"])), 1)
        self._due(other)
        self.assertTrue(self.app.routine_step())
        self.assertEqual(len(self._works(other["id"])), 1)
        self.assertEqual(len(self._works(blocked["id"])), 1)

        self._finish_turns(other["id"])
        work = self._works(other["id"])[0]
        self.app.store.put("task", {
            "id": "task-local-run", "work_id": work["id"], "status": "running",
            "host_id": "local", "owner_bot_id": "bot-main", "created": time.time(),
        })
        self._due(other)
        self.app.routine_step()
        self.assertEqual(len(self._works(other["id"])), 2)
        self.assertEqual(len(self._works(blocked["id"])), 1)
        self.assertTrue(all(item.get("parent_work_id") is None for item in self._works(other["id"])))

    def test_blocked_task_past_the_old_page_cap_still_coalesces(self):
        routine = self._routine("扫全量")
        self._due(routine)
        self.app.routine_step()
        work = self._works(routine["id"])[0]
        self._finish_turns(routine["id"])
        # Oldest 20 running rows are online. The offline connector is later.
        # A limit=20 scan would miss it and open another supervisor Work.
        now = time.time()
        for i in range(20):
            self.app.store.put("task", {
                "id": "task-online-%02d" % i, "work_id": work["id"], "owner_bot_id": "bot-main",
                "status": "running", "host_id": "local", "created": now + i,
            })
        self.app.store.put("task", {
            "id": "task-offline-late", "work_id": work["id"], "owner_bot_id": "bot-main",
            "status": "running", "host_id": "pc", "created": now + 50,
            "last_observation_error": "connector_offline",
        })
        before = self.app.store.get("routine", routine["id"])
        self.assertFalse(self.app.broker.online("pc"))
        self._due(routine)
        self.assertTrue(self.app.routine_step())
        self.assertEqual(self.app.store.count("work", routine_id=routine["id"]), 1)
        after = self.app.store.get("routine", routine["id"])
        self.assertEqual(after["last_run_at"], before["last_run_at"])
        self.assertTrue(any(
            run.get("outcome") == "coalesced" and run.get("coalesce_reason") == "unreachable"
            for run in self._runs(routine["id"])))
        for task_id in ["task-offline-late"] + ["task-online-%02d" % i for i in range(20)]:
            task = self.app.store.get("task", task_id)
            task["status"] = "succeeded"
            self.app.store.put("task", task)

        # The blocked work is past the old 200-work page. It must still be seen.
        for i in range(200):
            self.app.store.put("work", {
                "id": "work-fill-%03d" % i, "owner_bot_id": "bot-main", "routine_id": routine["id"],
                "goal": "filler", "status": "active", "parent_work_id": None, "created": now + 100 + i,
            })
        blocked = self.app.store.put("work", {
            "id": "work-blocked-late", "owner_bot_id": "bot-main", "routine_id": routine["id"],
            "goal": "late approval", "status": "active", "parent_work_id": None, "created": now + 1000,
        })
        self.app.store.put("task", {
            "id": "task-approve-late", "work_id": blocked["id"], "owner_bot_id": "bot-main",
            "status": "awaiting_approval", "host_id": "local", "created": now + 1001,
        })
        held = self.app.store.get("routine", routine["id"])
        count = self.app.store.count("work", routine_id=routine["id"])
        self.assertGreater(count, 200)
        self._due(routine)
        self.app.routine_step()
        self.assertEqual(self.app.store.count("work", routine_id=routine["id"]), count)
        self.assertEqual(self.app.store.get("routine", routine["id"])["last_run_at"], held["last_run_at"])
        self.assertTrue(any(run.get("coalesce_reason") == "awaiting_approval" for run in self._runs(routine["id"])))
        self.assertIsNone(self.app.store.get("work", blocked["id"]).get("parent_work_id"))
