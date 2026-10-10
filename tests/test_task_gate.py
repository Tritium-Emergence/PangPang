# -*- coding: utf-8 -*-
"""Phase C smoke: TaskResultGate eligibility is deterministic + shadow is inert by
default (no kernel change, no network unless enabled)."""
import sys, os, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.task_gate import TaskResultGate
from pangpang.semantic_gate import GateUnavailable, SemanticGate
from pangpang.store import Fault


class TaskGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.store.close()
        self.tmp.cleanup()

    def _gate(self, cfg_extra):
        cfg = config(self.root)
        cfg.update(cfg_extra)
        self.app = Service(cfg, self.root / "data")
        return TaskResultGate(self.app, SemanticGate(self.app))

    def test_default_off_is_disabled(self):
        g = self._gate({})
        self.assertEqual(g.mode, "off")
        self.assertFalse(g.enabled())

    def test_fail_open_true_is_rejected(self):
        cfg = config(self.root)
        cfg["task_result_gate"] = {"mode": "enforce", "fail_open": True}
        with self.assertRaises(Fault) as raised:
            self.app = Service(cfg, self.root / "data")
        self.app = None
        self.assertEqual(raised.exception.code, "task_result_gate_fail_open_unsupported")

    def test_enforce_unavailable_always_escalates(self):
        g = self._gate({"task_result_gate": {"mode": "enforce"}})
        g.gate._call = lambda *a, **k: (_ for _ in ()).throw(GateUnavailable("down"))
        decision = g.decide(
            {"id": "t1", "work_id": "w1", "access_mode": "verified_read"},
            {"status": "succeeded", "process_tree_confirmed": True,
             "tail": "the answer is 42", "artifacts": []})
        self.assertFalse(decision["fast_accept"])
        self.assertEqual(decision["reason"], "gate_unavailable_escalate")
        self.assertEqual(decision["gate_error"], "down")

    def test_eligibility_is_deterministic(self):
        g = self._gate({"task_result_gate": {"mode": "shadow"}})
        self.assertTrue(g.enabled())
        base_task = {"id": "t1", "access_mode": "verified_read"}
        ok_receipt = {"status": "succeeded", "process_tree_confirmed": True,
                      "tail": "the answer is 42", "artifacts": []}
        self.assertTrue(g.eligible(base_task, ok_receipt))
        # a write task is never fast-accepted
        self.assertFalse(g.eligible({"id": "t", "access_mode": "write"}, ok_receipt))
        # unconfirmed process tree
        self.assertFalse(g.eligible(base_task, dict(ok_receipt, process_tree_confirmed=False)))
        # non-succeeded
        self.assertFalse(g.eligible(base_task, dict(ok_receipt, status="failed")))
        # has artifacts -> needs real read_evidence, not a text judgement
        self.assertFalse(g.eligible(base_task, dict(ok_receipt, artifacts=[{"path": "x"}])))
        # truncated / empty tail
        self.assertFalse(g.eligible(base_task, dict(ok_receipt, truncated=True)))
        self.assertFalse(g.eligible(base_task, dict(ok_receipt, tail=" ")))

    def test_observe_shadow_never_calls_when_ineligible(self):
        g = self._gate({"task_result_gate": {"mode": "shadow"}})
        called = []
        g.gate._call = lambda *a, **k: called.append(1) or {"disposition": "ACCEPT", "confidence": 1.0}
        # write task is ineligible -> no Jev call
        g.observe({"id": "t", "access_mode": "write", "work_id": "w"},
                  {"status": "succeeded", "process_tree_confirmed": True, "tail": "x", "artifacts": []})
        import time as _t; _t.sleep(0.2)
        self.assertEqual(called, [])

    def test_fast_accept_delivery_settles_parent_child_work_dep(self):
        # Regression: a delegated child that fast-accepts (delivered without a planner
        # turn) MUST still settle its parent's child_work dependency, else the
        # delegator waits forever. Exercises _deliver_fast_accept's parent settle.
        self._gate({"task_result_gate": {"mode": "enforce"}})
        app = self.app
        parent = app.send("bot-main", {"text": "p"}, "parent-ws")
        parent_work = app.store.get("work", parent["work_id"])
        child = app.store.put("work", {"id": "w-child-fa", "owner_bot_id": "bot-main",
            "kind": "delegated", "status": "active", "goal": "g",
            "parent_work_id": parent_work["id"], "created": __import__("time").time()})
        pdep = app.store.put("dependency", {"id": "dep-child-fa", "kind": "child_work",
            "source_work_id": parent_work["id"], "target_id": child["id"], "status": "pending",
            "deadline": None, "created": __import__("time").time(), "root_request_id": None})
        parent_work["pending_dep_ids"] = [pdep["id"]]
        parent_work["status"] = "waiting"
        app.store.put("work", parent_work)
        # direct-deliver the child (as fast-accept would), then assert the parent dep
        # settled success and the child is delivered.
        with app.store.atomic():
            app._deliver_fast_accept(app.store.get("work", child["id"]),
                                     {"id": "d", "target_id": child["id"],
                                      "result_payload": {"result_text": "answer 42"},
                                      "root_request_id": None}, "answer 42", [])
        self.assertEqual(app.store.get("work", child["id"])["status"], "delivered")
        self.assertEqual(app.store.get("dependency", pdep["id"])["status"], "settled")
        self.assertEqual(app.store.get("dependency", pdep["id"])["outcome"], "success")


if __name__ == "__main__":
    unittest.main()
