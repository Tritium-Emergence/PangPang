"""A later routine fire gets a read-only handoff of the same routine's open Work.

Pointers and recorded checkpoint fields only. This Work's checkpoint and
dependencies stay its own. File bodies stay behind read_evidence.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from test_bot_runtime import config

from pangpang.context import projection
from pangpang.planner_prompt import PLANNER_SYSTEM
from pangpang.service import Service


class RoutineHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _routine(self, instruction):
        return self.app.create_routine("bot-main", {
            "instruction": instruction,
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "interval", "minutes": 60},
        })

    def _fire(self, routine):
        run = self.app.run_routine_now("bot-main", routine["id"])
        self.assertTrue(run.get("work_id"))
        return run["work_id"]

    def _finish_turns(self, routine_id):
        for work in self.app.store.find("work", routine_id=routine_id, limit=50):
            for turn in self.app.store.find("turn", work_id=work["id"], limit=20):
                if turn.get("status") in {"queued", "running", "starting"}:
                    turn["status"] = "done"
                    self.app.store.put("turn", turn)

    def _packet(self, work_id):
        turn = self.app.store.find("turn", work_id=work_id, newest=True, limit=1)[0]
        system = projection(self.app, turn)[0]["content"]
        return json.loads(system[system.index("{"):]), system

    def test_later_fire_projects_prior_open_work_without_file_bodies(self):
        self.app.store.put("artifact", {
            "id": "artifact-prior", "path": "/secret/handoff/brief.txt",
            "digest": "c" * 64, "size": 42, "host_id": "local",
            "owner_bot_id": "bot-main", "work_id": None, "source": "recorded-task",
            "content": "FILE-BODY-SHOULD-NOT-INLINE",
        })
        self.app.store.put("artifact", {
            "id": "artifact-ref-only", "path": "/secret/handoff/extra.txt",
            "digest": "d" * 64, "size": 7, "host_id": "local",
            "owner_bot_id": "bot-main", "work_id": None, "source": "recorded-task",
        })
        routine = self._routine("STANDING-CHARTER")
        prior = self._fire(routine)
        self.app.checkpoint_work("bot-main", prior, {
            "phase": "review",
            "summary": "PRIOR-CHECKPOINT-SUMMARY",
            "completed": ["draft written"],
            "next_actions": ["check the brief"],
            "blocked_on": ["waiting on brief.txt"],
            "evidence_refs": ["artifact-prior"],
        })
        row = self.app.store.get("work", prior)
        row["refs"] = ["artifact-ref-only", "artifact-prior"]
        row["status"] = "waiting"
        row["pending_dep_ids"] = ["dep-prior"]
        self.app.store.put("work", row)
        self.app.store.put("dependency", {
            "id": "dep-prior", "source_work_id": prior, "kind": "task",
            "target_id": "task-secret-row", "status": "pending", "outcome": None,
            "summary": None, "refs": ["artifact-prior"], "created": time.time(),
        })
        self.app.store.put("task", {
            "id": "task-secret-row", "work_id": prior, "owner_bot_id": "bot-main",
            "status": "running", "host_id": "local",
            "user_prompt": "TASK-BODY-SECRET", "created": time.time(),
        })

        own, _ = self._packet(prior)
        self.assertNotIn("same_routine_open_work", own)
        self.assertEqual(own["work"]["checkpoint"]["summary"], "PRIOR-CHECKPOINT-SUMMARY")
        self.assertEqual(own["work"]["checkpoint"]["evidence_refs"], ["artifact-prior"])

        self._finish_turns(routine["id"])
        second = self._fire(routine)
        self.assertNotEqual(second, prior)
        stored = self.app.store.get("work", second)
        prior_stored = self.app.store.get("work", prior)
        self.assertIsNone(stored.get("parent_work_id"))
        self.assertIsNone(prior_stored.get("parent_work_id"))
        self.assertEqual(stored["checkpoint"]["summary"], "STANDING-CHARTER")
        self.assertEqual(stored["checkpoint"]["completed"], [])
        self.assertFalse(stored.get("pending_dep_ids"))
        self.assertEqual(stored["goal"], "STANDING-CHARTER")

        packet, system = self._packet(second)
        self.assertEqual(packet["work"]["checkpoint"]["summary"], "STANDING-CHARTER")
        self.assertEqual(packet["work"]["checkpoint"]["completed"], [])
        self.assertEqual(packet["dependencies"], [])
        self.assertEqual(packet["work"]["goal"], "STANDING-CHARTER")

        handoff = packet["same_routine_open_work"]
        self.assertFalse(handoff["truncated"])
        self.assertEqual([item["id"] for item in handoff["works"]], [prior])
        entry = handoff["works"][0]
        self.assertEqual(entry["status"], "waiting")
        self.assertNotIn("goal", entry)
        self.assertNotIn("parent_work_id", entry)
        self.assertNotIn("grant_write", entry)
        self.assertNotIn("pending_dep_ids", entry)
        self.assertEqual(entry["refs"], ["artifact-ref-only", "artifact-prior"])
        self.assertEqual(entry["checkpoint"]["summary"], "PRIOR-CHECKPOINT-SUMMARY")
        self.assertEqual(entry["checkpoint"]["completed"], ["draft written"])
        self.assertEqual(entry["checkpoint"]["next_actions"], ["check the brief"])
        self.assertEqual(entry["checkpoint"]["blocked_on"], ["waiting on brief.txt"])
        self.assertEqual(entry["checkpoint"]["evidence_refs"], ["artifact-prior"])
        self.assertNotIn("checkpoint_truncated", entry)
        self.assertEqual(len(entry["dependencies"]), 1)
        dep = entry["dependencies"][0]
        self.assertEqual(set(dep), {"kind", "status", "outcome", "refs"})
        self.assertEqual(dep["kind"], "task")
        self.assertEqual(dep["status"], "pending")
        self.assertIsNone(dep["outcome"])
        self.assertEqual(dep["refs"], ["artifact-prior"])
        names = {item["ref"]: item for item in entry["artifacts"]}
        self.assertEqual(set(names), {"artifact-ref-only", "artifact-prior"})
        self.assertEqual(names["artifact-prior"]["name"], "brief.txt")
        self.assertEqual(names["artifact-prior"]["source"], "recorded-task")
        self.assertTrue(names["artifact-prior"]["resolved"])
        self.assertNotIn("path", names["artifact-prior"])
        self.assertNotIn("digest", names["artifact-prior"])
        self.assertNotIn("content", names["artifact-prior"])

        indexed = next(item for item in packet["recent_work_index"] if item["id"] == prior)
        self.assertNotIn("evidence_refs", indexed)
        self.assertEqual(indexed["in_flight"][0]["kind"], "task")
        self.assertNotIn("dep-prior", json.dumps(indexed))

        blob = json.dumps(handoff)
        for secret in ("FILE-BODY-SHOULD-NOT-INLINE", "/secret/handoff", "c" * 64,
                       "d" * 64, "dep-prior", "task-secret-row", "TASK-BODY-SECRET",
                       "parent_work_id", "pending_dep_ids", "grant_write"):
            self.assertNotIn(secret, blob, secret)
        self.assertNotIn("TASK-BODY-SECRET", system)
        self.assertNotIn("FILE-BODY-SHOULD-NOT-INLINE", system)

        self.app.probe("codex-local")
        submitted = self.app.submit_task("bot-main", second, {"prompt": "continue from the brief"})
        prompt = self.app.store.get("task", submitted["task_id"])["prompt"]
        self.assertIn("Work progress", prompt)
        self.assertIn("STANDING-CHARTER", prompt)
        self.assertNotIn("PRIOR-CHECKPOINT-SUMMARY", prompt)
        self.assertNotIn("FILE-BODY-SHOULD-NOT-INLINE", prompt)
        self.assertEqual(self.app.store.get("work", second)["checkpoint"]["summary"], "STANDING-CHARTER")

    def test_same_work_checkpoint_and_submit_task_still_inject(self):
        note = self.root / "note.txt"
        self.app.store.put("artifact", {
            "id": "artifact-same", "path": str(note), "digest": "e" * 64, "size": 4,
            "host_id": "local", "owner_bot_id": "bot-main", "work_id": None,
            "source": "user_import",
        })
        first = self.app.send("bot-main", {"text": "SAME-WORK-ASK"}, "sw")
        self.app.checkpoint_work("bot-main", first["work_id"], {
            "phase": "doing",
            "summary": "THIS-WORK-SUMMARY",
            "completed": ["done-step"],
            "next_actions": ["next-step"],
        })
        done = self.app.store.get("turn", first["turn_id"])
        done["status"] = "done"
        self.app.store.put("turn", done)
        again = self.app.send("bot-main", {
            "text": "SAME-WORK-FOLLOWUP", "work_id": first["work_id"],
        }, "sw2")
        self.assertEqual(again["work_id"], first["work_id"])
        self.assertNotEqual(again["turn_id"], first["turn_id"])
        turn = self.app.store.get("turn", again["turn_id"])
        context = projection(self.app, turn)
        system = context[0]["content"]
        packet = json.loads(system[system.index("{"):])
        blob = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("same_routine_open_work", packet)
        self.assertEqual(packet["work"]["checkpoint"]["summary"], "THIS-WORK-SUMMARY")
        self.assertEqual(packet["work"]["checkpoint"]["completed"], ["done-step"])
        self.assertEqual(packet["work"]["goal"], "SAME-WORK-ASK")
        self.assertIn("SAME-WORK-ASK", blob)
        self.assertIn("SAME-WORK-FOLLOWUP", blob)
        self.assertNotIn("SAME-WORK-FOLLOWUP", packet["work"]["goal"])

        self.app.probe("codex-local")
        submitted = self.app.submit_task("bot-main", first["work_id"], {
            "prompt": "continue the notes",
            "artifact_refs": ["artifact-same"],
        })
        prompt = self.app.store.get("task", submitted["task_id"])["prompt"]
        self.assertIn("Work progress", prompt)
        self.assertIn("THIS-WORK-SUMMARY", prompt)
        self.assertIn("done-step", prompt)
        self.assertIn("next-step", prompt)
        self.assertIn("artifact-same", prompt)
        self.assertIn(str(note), prompt)

    def test_unrelated_routine_and_chat_are_not_mixed_in(self):
        alpha = self._routine("CHARTER-ALPHA")
        beta = self._routine("CHARTER-BETA")
        alpha_work = self._fire(alpha)
        self.app.checkpoint_work("bot-main", alpha_work, {"summary": "PROGRESS-ALPHA"})
        self._finish_turns(alpha["id"])
        beta_work = self._fire(beta)
        packet, _ = self._packet(beta_work)
        self.assertNotIn("same_routine_open_work", packet)
        chat = self.app.send("bot-main", {"text": "unrelated chat"}, "chat")
        chat_packet, _ = self._packet(chat["work_id"])
        self.assertNotIn("same_routine_open_work", chat_packet)

        self._finish_turns(beta["id"])
        self.app.checkpoint_work("bot-main", beta_work, {"summary": "PROGRESS-BETA"})
        beta_next = self._fire(beta)
        packet, _ = self._packet(beta_next)
        handoff = packet["same_routine_open_work"]
        self.assertEqual([item["id"] for item in handoff["works"]], [beta_work])
        self.assertEqual(handoff["works"][0]["checkpoint"]["summary"], "PROGRESS-BETA")
        self.assertNotIn("PROGRESS-ALPHA", json.dumps(handoff))
        self.assertNotIn(alpha_work, [item["id"] for item in handoff["works"]])
        self.assertEqual(packet["work"]["goal"], "CHARTER-BETA")
        self.assertEqual(packet["work"]["checkpoint"]["summary"], "CHARTER-BETA")

    def test_delivered_work_is_not_a_live_handoff(self):
        routine = self._routine("DONE-CHARTER")
        first = self._fire(routine)
        self.app.checkpoint_work("bot-main", first, {"summary": "FINISHED-PROGRESS"})
        self._finish_turns(routine["id"])
        row = self.app.store.get("work", first)
        row["status"] = "delivered"
        self.app.store.put("work", row)
        second = self._fire(routine)
        packet, _ = self._packet(second)
        self.assertNotIn("same_routine_open_work", packet)
        indexed = next(item for item in packet["recent_work_index"] if item["id"] == first)
        self.assertEqual(indexed["status"], "delivered")
        self.assertEqual(indexed["summary"], "FINISHED-PROGRESS")

    def test_open_work_handoff_is_bounded(self):
        routine = self._routine("CAP")
        ids = []
        for i in range(5):
            work_id = self._fire(routine)
            ids.append(work_id)
            summary = ("S" * 400) + "TAIL-MARKER" if i == 4 else "OPEN-%d" % i
            completed = ["ITEM-%d" % n for n in range(9)] if i == 4 else ["kept"]
            self.app.checkpoint_work("bot-main", work_id, {
                "summary": summary, "completed": completed,
            })
            self._finish_turns(routine["id"])
            time.sleep(0.02)
        current = self._fire(routine)
        packet, system = self._packet(current)
        handoff = packet["same_routine_open_work"]
        self.assertTrue(handoff["truncated"])
        shown = [item["id"] for item in handoff["works"]]
        self.assertEqual(shown, list(reversed(ids[1:])))
        self.assertNotIn(ids[0], shown)
        self.assertNotIn(current, shown)
        newest = handoff["works"][0]
        self.assertEqual(newest["id"], ids[4])
        self.assertEqual(newest["checkpoint"]["summary"], "S" * 400)
        self.assertNotIn("TAIL-MARKER", newest["checkpoint"]["summary"])
        self.assertEqual(len(newest["checkpoint"]["completed"]), 8)
        self.assertNotIn("ITEM-8", newest["checkpoint"]["completed"])
        self.assertTrue(newest["checkpoint_truncated"])
        self.assertNotIn("TAIL-MARKER", system)
        self.assertEqual(packet["work"]["checkpoint"]["summary"], "CAP")

    def test_prompt_says_verify_then_pass_artifact_refs(self):
        self.assertNotIn("{", PLANNER_SYSTEM)
        self.assertIn(
            "packet.same_routine_open_work, when present, is a read-only handoff",
            PLANNER_SYSTEM)
        self.assertIn("verify the pointers with read_work and read_evidence", PLANNER_SYSTEM)
        self.assertIn("pass the artifact refs you verified in artifact_refs", PLANNER_SYSTEM)
        self.assertIn("Standing routine instructions stay in identity and work.goal", PLANNER_SYSTEM)
        self.assertIn("not this Work's checkpoint and not its dependencies", PLANNER_SYSTEM)
        self.assertIn("do not treat the handoff as a review or as file contents", PLANNER_SYSTEM)
        self.assertFalse(any("一" <= ch <= "鿿" for ch in PLANNER_SYSTEM))


if __name__ == "__main__":
    unittest.main()
