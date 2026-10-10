import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class ZombieAutoResolveTests(unittest.TestCase):
    DEVICE = "device-" + "0" * 32

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        cloud = config(self.root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        self.app = Service(cloud, self.root.parent / "cloud", deployment_level="L0")
        self.app.store.put("probe", {"id": "codex-local", "available": True,
                                     "supervision": "linux-cgroup-cooperative", "policy_digest": "pd"})

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def sync(self, contract_version):
        return self.app.broker.sync("local", {"instance_id": self.DEVICE,
                                              "supported_contract_version": contract_version,
                                              "reports": [], "receipts": []})

    def submit(self, tag):
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, tag)
        r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        return self.app.store.get("task", r["task_id"]), r

    def mutate(self, task_id, **fields):
        current = self.app.store.get("task", task_id)
        current.update(fields)
        self.app.store.put("task", current)
        return current

    def finish_start(self, task, result):
        cmd = self.app.store.get("command",
                                 self.app.broker.command_id(task["host_id"], "start", task["execution_id"]))
        cmd.update(status="done", result=result)
        self.app.store.put("command", cmd)

    def held_task(self, tag):
        self.sync(2)
        task, r = self.submit(tag)
        self.app.task_step()
        self.assertEqual(self.app.store.find("command", action="start"), [])
        return task, r

    def observe(self, task):
        self.app._apply_observation(self.app.store.get("task", task["id"]), {"status": "awaiting_receipt"})
        return self.app.store.get("task", task["id"])

    def test_capability_hold_deadline_auto_cancels_end_to_end(self):
        task, r = self.held_task("z1")
        self.app._mark_task_unknown(task, "capability_hold_deadline")
        done = self.app.reconcile(task["id"])
        self.assertEqual(done["status"], "cancelled")
        receipt = done["receipt"]
        self.assertTrue(receipt["auto_resolved"])
        self.assertEqual(receipt["resolution_source"], "system")
        self.assertNotIn("operator_resolution", receipt)
        self.assertEqual(receipt["not_launched"], "never_started")
        self.assertEqual(receipt["unknown_reason"], "capability_hold_deadline")
        self.assertEqual(receipt["start_command_state"], "never_enqueued")
        self.assertEqual(receipt["start_command_id"],
                         self.app.broker.command_id("local", "start", task["execution_id"]))
        self.assertEqual(self.app.store.find("command", action="resolve_run"), [])
        self.assertNotEqual(self.app.store.get("dependency", r["dependency_id"])["status"], "pending")

    def test_unconfirmed_with_cancel_and_expired_start_auto_cancels(self):
        self.sync(3)
        task, _ = self.submit("z2")
        self.app.task_step()
        self.finish_start(task, {"error": "command_expired"})
        self.mutate(task["id"], cancel_requested=True)
        self.app._mark_task_unknown(task, "device_action_unconfirmed")
        done = self.observe(task)
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(done["receipt"]["start_command_state"], "expired_before_delivery")

    def test_superseded_start_auto_cancels(self):
        self.sync(3)
        task, _ = self.submit("z3")
        self.app.task_step()
        self.finish_start(task, {"superseded_by_operator_resolution": True})
        self.mutate(task["id"], device_cancel_sent=True)
        self.app._mark_task_unknown(task, "device_action_unconfirmed")
        done = self.observe(task)
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(done["receipt"]["start_command_state"], "superseded_before_delivery")

    def test_pending_start_is_never_presumed_dead(self):
        self.sync(3)
        task, _ = self.submit("z4")
        self.app.task_step()
        self.mutate(task["id"], cancel_requested=True)
        self.app._mark_task_unknown(task, "device_action_unconfirmed")
        done = self.observe(task)
        self.assertEqual(done["status"], "unknown")
        cmd = self.app.store.get("command",
                                 self.app.broker.command_id("local", "start", task["execution_id"]))
        self.assertEqual(cmd["status"], "pending")

    def test_delivered_start_is_never_presumed_dead(self):
        self.sync(3)
        task, _ = self.submit("z5")
        self.app.task_step()
        self.sync(3)
        cmd = self.app.store.get("command",
                                 self.app.broker.command_id("local", "start", task["execution_id"]))
        self.assertEqual(cmd["status"], "delivered")
        self.mutate(task["id"], cancel_requested=True)
        self.app._mark_task_unknown(task, "device_action_unconfirmed")
        done = self.observe(task)
        self.assertEqual(done["status"], "unknown")

    def test_unconfirmed_without_cancel_never_auto_cancels(self):
        task, _ = self.held_task("z6")
        self.app._mark_task_unknown(task, "device_action_unconfirmed")
        done = self.observe(task)
        self.assertEqual(done["status"], "unknown")

    def test_other_unknown_reasons_never_auto_cancel(self):
        task, _ = self.held_task("z7")
        self.app._mark_task_unknown(task, "receipt_missing")
        done = self.observe(task)
        self.assertEqual(done["status"], "unknown")

    def test_local_task_without_broker_command_is_never_auto_cancelled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        local_root = Path(tmp.name) / "workspace"
        local_root.mkdir()
        app = Service(config(local_root), local_root.parent / "cloud", deployment_level="L0")
        self.addCleanup(app.store.close)
        app.store.put("probe", {"id": "codex-local", "available": True,
                                "supervision": "linux-cgroup-cooperative", "policy_digest": "pd"})
        m = app.send("bot-main", {"text": "x", "allow_write": True}, "z8")
        task = app.store.get("task", app.submit_task("bot-main", m["work_id"], {"prompt": "y"})["task_id"])
        app._mark_task_unknown(task, "capability_hold_deadline")
        app._apply_observation(app.store.get("task", task["id"]), {"status": "awaiting_receipt"})
        self.assertEqual(app.store.get("task", task["id"])["status"], "unknown")

    def test_operator_evidence_fault_names_requirements(self):
        task, _ = self.held_task("z9")
        self.app._mark_task_unknown(task, "capability_hold_deadline")
        with self.assertRaises(Fault) as ctx:
            self.app.resolve_task(task["id"], {"resolution": "maybe", "evidence": "short"})
        self.assertEqual(ctx.exception.code, "operator_evidence_required")
        self.assertIn("10 characters", ctx.exception.advice)
        self.assertIn("confirmed_stopped", ctx.exception.advice)


if __name__ == "__main__":
    unittest.main()
