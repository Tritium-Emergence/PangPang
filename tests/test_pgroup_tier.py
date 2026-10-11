"""Trusted-L0 process-group cooperative tier.

Unit checks run everywhere. Real-process checks need a POSIX host WITHOUT a usable
systemd --user cgroup scope (otherwise the runner correctly prefers the cgroup
tier) and are skipped elsewhere.
"""
import contextlib
import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang import consent, runner
from pangpang.connector import Connector, policy_digest
from pangpang.execution import Execution
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault, encode
from pangpang.task_gate import TaskResultGate


def consenting(root):
    cfg = config(root)
    cfg["hosts"][0][consent.CONSENT_KEY] = consent.RISK_VERSION
    return cfg


def not_consenting(root):
    """A host whose operator has NOT accepted the risk (the shared test host has)."""
    cfg = config(root)
    cfg["hosts"][0].pop(consent.CONSENT_KEY, None)
    return cfg


class ReceiptProofTests(unittest.TestCase):
    PG, CG, WJ = "posix-pgroup-cooperative", "linux-cgroup-cooperative", "windows-job-object"

    def ok(self, floor=None, **receipt):
        return runner.receipt_proof_ok(dict({"process_tree_confirmed": True}, **receipt), floor)

    def test_executed_runs_need_their_evidence(self):
        self.assertTrue(self.ok(status="succeeded", supervision=self.PG, termination_evidence="pgroup-empty"))
        self.assertTrue(self.ok(status="succeeded", supervision=self.CG, termination_evidence="cgroup-v2-populated-zero"))
        self.assertTrue(self.ok(status="succeeded", supervision=self.WJ, exit_code=0))
        self.assertTrue(self.ok(status="failed", supervision=self.PG, termination_evidence="pgroup-empty", exit_code=2))
        # Adversarial: a success without its tree evidence never commits.
        for label in (self.PG, "posix-process-group", "invented-backend", None):
            self.assertFalse(self.ok(status="succeeded", supervision=label, exit_code=0), label)
        self.assertFalse(self.ok(status="failed", supervision=self.CG, exit_code=1))
        self.assertFalse(self.ok(status="cancelled", supervision="posix-process-group", exit_code=-9))

    def test_floor_attempt_terminal_matrix(self):
        # with a frozen floor, EVERY terminal outcome of a launched attempt
        # needs cleanup proof; only explicit, source-bound paths are evidence-free.
        PG = self.PG
        proof = {"supervision": PG, "termination_evidence": "pgroup-empty"}
        for status in ("succeeded", "failed", "cancelled"):
            for with_exit in (True, False):
                extra = {"exit_code": 0} if with_exit else {}
                # launched, with proof: accepted
                self.assertTrue(self.ok(PG, status=status, **proof, **extra), (status, with_exit))
                # launched, incomplete (the reviewer's repro shape): rejected
                self.assertFalse(self.ok(PG, status=status, supervision="posix-process-group",
                                         error="report incomplete", **extra), (status, with_exit))
                self.assertFalse(self.ok(PG, status=status, supervision=PG, **extra), (status, with_exit))
                # below the floor: rejected for all three outcomes
                self.assertFalse(self.ok(self.CG, status=status, **proof, **extra), (status, with_exit))
        # Explicitly not launched: only the path's own outcome, never with exit_code/evidence.
        for source, outcome in runner.NOT_LAUNCHED_OUTCOME.items():
            for status in ("succeeded", "failed", "cancelled"):
                self.assertEqual(self.ok(PG, status=status, not_launched=source), status == outcome, (source, status))
            self.assertFalse(self.ok(PG, status=outcome, not_launched=source, exit_code=1))
            self.assertFalse(self.ok(PG, status=outcome, not_launched=source, **proof))
        self.assertFalse(self.ok(PG, status="failed", not_launched="invented"))
        self.assertFalse(self.ok(PG, status="failed", not_launched=True))
        # Operator resolution may only cancel.
        self.assertTrue(self.ok(PG, status="cancelled", operator_resolution="confirmed_stopped"))
        for status in ("succeeded", "failed"):
            self.assertFalse(self.ok(PG, status=status, operator_resolution="confirmed_stopped"))

    def test_unknown_floor_is_not_no_floor(self):
        proof = {"status": "succeeded", "supervision": self.WJ}
        self.assertTrue(self.ok(None, **proof))
        self.assertFalse(self.ok("invented-floor", **proof))
        #...but the runner's own refusal and operator cancellation still close it.
        self.assertTrue(self.ok("invented-floor", status="failed", not_launched="runner_prelaunch"))
        self.assertTrue(self.ok("invented-floor", status="cancelled", operator_resolution="confirmed_stopped"))

    def test_legacy_floorless_attempt_keeps_previous_rule(self):
        # Floor-less = submitted before this contract existed (its device may never write
        # not_launched): unchanged compatibility.
        self.assertTrue(self.ok(status="failed", supervision="posix-process-group", error="supervision_unavailable:x"))
        self.assertTrue(self.ok(status="cancelled", operator_resolution="confirmed_stopped"))
        self.assertTrue(self.ok(status="failed", error="local_policy_rejected"))
        self.assertFalse(self.ok(status="failed", supervision="invented-backend"))

    def test_contradictions_and_unconfirmed(self):
        self.assertFalse(self.ok(status="succeeded", supervision=self.PG, termination_evidence="cgroup-v2-populated-zero"))
        self.assertFalse(self.ok(status="failed", supervision=self.WJ, termination_evidence="pgroup-empty"))
        self.assertFalse(self.ok(status="failed", supervision="posix-process-group", termination_evidence="pgroup-empty"))
        self.assertFalse(runner.receipt_proof_ok({"status": "succeeded", "supervision": self.PG,
                                                  "termination_evidence": "pgroup-empty"}))

    def test_floor_blocks_downgrade_not_policy(self):
        pg = dict(status="succeeded", supervision=self.PG, termination_evidence="pgroup-empty")
        self.assertTrue(self.ok(self.PG, **pg))
        self.assertFalse(self.ok(self.CG, **pg))
        self.assertTrue(self.ok(self.PG, status="succeeded", supervision=self.CG,
                                termination_evidence="cgroup-v2-populated-zero"))


class ServiceSideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / "workspace"; self.root.mkdir()
        self.app = Service(consenting(self.root), self.root.parent / "data", deployment_level="L0")
        self.app.store.put("probe", {"id": "codex-local", "available": True, "supervision": "posix-pgroup-cooperative"})

    def tearDown(self):
        self.app.store.close(); self.tmp.cleanup()

    def _queued_task(self):
        with patch.object(self.app.execution, "start", side_effect=Fault("transport_unavailable")):
            m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "pg")
            r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        return self.app.store.get("task", r["task_id"])

    def test_contradictory_receipt_never_commits(self):
        task = self._queued_task()
        receipt = self._receipt(task, supervision="posix-pgroup-cooperative",
                                termination_evidence="cgroup-v2-populated-zero")
        with self.assertRaises(Fault) as caught:
            self.app.complete_task(task, receipt)
        self.assertEqual(caught.exception.code, "receipt_identity_or_terminal_proof_required")
        self.app.complete_task(task, dict(receipt, termination_evidence="pgroup-empty"))
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "succeeded")

    def _receipt(self, task, **extra):
        return dict({"execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
                     "attempt_uuid": task.get("attempt_uuid"), "status": "succeeded", "artifacts": [],
                     "exit_code": 0, "process_tree_confirmed": True}, **extra)

    def test_success_without_evidence_keeps_dependency_pending(self):
        # Adversarial table, through the real commit entry.
        task = self._queued_task()
        self.assertEqual(task["supervision_floor"], "posix-pgroup-cooperative")
        for extra in ({"supervision": "posix-pgroup-cooperative"}, {"supervision": "posix-process-group"},
                      {"supervision": "invented-backend"}, {}):
            with self.assertRaises(Fault, msg=extra):
                self.app.complete_task(task, self._receipt(task, **extra))
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "queued")
        self.assertEqual(self.app.store.get("dependency", task["dependency_id"])["status"], "pending")

    def test_launched_attempt_closes_by_frozen_floor_not_current_level(self):
        # Adversarial: after a restart without the L0 declaration, an attempt
        # launched under L0 still commits its valid evidence exactly once; the new
        # policy only blocks NEW launches.
        task = self._queued_task()
        self.app.store.close()
        self.app = Service(consenting(self.root), self.root.parent / "data", deployment_level=None)
        self.assertIsNone(self.app.deployment_level)
        receipt = self._receipt(task, supervision="posix-pgroup-cooperative", termination_evidence="pgroup-empty")
        self.app.complete_task(task, receipt)
        self.app.complete_task(task, receipt)
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "succeeded")
        dep = self.app.store.get("dependency", task["dependency_id"])
        self.assertNotEqual(dep["status"], "pending")
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "pg2")
        with self.assertRaises(Fault) as caught:
            self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        self.assertEqual(caught.exception.code, "executor_cleanup_unsupported")

    def test_launched_failed_without_evidence_keeps_dependency_pending(self):
        # through the real commit entry: failed/cancelled, no exit_code, no
        # evidence, correct attempt identity -> not a terminal proof.
        task = self._queued_task()
        for status in ("failed", "cancelled"):
            receipt = self._receipt(task, status=status, supervision="posix-process-group",
                                    error="report incomplete")
            receipt.pop("exit_code")
            with self.assertRaises(Fault):
                self.app.complete_task(task, receipt)
        self.assertEqual(self.app.store.get("dependency", task["dependency_id"])["status"], "pending")

    def test_queued_cancel_is_an_explicit_not_launched_path(self):
        task = self._queued_task()
        self.app.cancel(task["id"])
        current = self.app.store.get("task", task["id"])
        self.assertEqual(current["status"], "cancelled")
        self.assertEqual(current["receipt"]["not_launched"], "queued_cancel")

    def test_revoked_consent_blocks_new_launch_despite_stale_probe(self):
        # The consent field is not a second gate once --deployment-level L0 is set.
        # Dropping the field leaves the cooperative probe admissible. Clearing the
        # declaration does not: an undeclared level still rejects that probe.
        self.app.execution.hosts["local"].pop(consent.CONSENT_KEY)
        work = self.app.store.get("work", self.app.send("bot-main", {"text": "x", "allow_write": True}, "rv")["work_id"])
        route = self.app.route("bot-main", work, {})
        self.assertEqual(route["selected"]["installation_id"], "codex-local")
        self.assertIsNone(self.app._start_denied({"host_id": "local", "installation_id": "codex-local"}))
        self.app.deployment_level = None
        route = self.app.route("bot-main", work, {})
        self.assertIsNone(route["selected"])
        self.assertEqual(route["rejected"][0]["reason"], "platform_supervision_unverified")

    def test_model_projection_omits_supervision_evidence(self):
        task = self._queued_task()
        self.app.complete_task(task, {"execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
            "attempt_uuid": task.get("attempt_uuid"), "status": "succeeded", "artifacts": [], "exit_code": 0,
            "process_tree_confirmed": True, "supervision": "posix-pgroup-cooperative",
            "termination_evidence": "pgroup-empty"})
        turn = next(t for t in self.app.store.find("turn", work_id=task["work_id"]))
        view = Runtime(self.app).invoke(turn, "inspect_task", {"task_id": task["id"]})
        self.assertEqual(view["receipt"]["status"], "succeeded")
        for key in ("process_tree_confirmed", "supervision", "termination_evidence"):
            self.assertNotIn(key, view["receipt"])
        # Operator record keeps the evidence.
        self.assertEqual(self.app.store.get("task", task["id"])["receipt"]["termination_evidence"], "pgroup-empty")

    def test_fast_accept_not_widened(self):
        gate = TaskResultGate.__new__(TaskResultGate)
        gate.max_result_bytes = 4096
        task = {"access_mode": "verified_read"}
        receipt = {"status": "succeeded", "process_tree_confirmed": True, "tail": "answer",
                   "supervision": "linux-cgroup-cooperative", "termination_evidence": "cgroup-v2-populated-zero"}
        self.assertTrue(gate.eligible(task, receipt))
        self.assertFalse(gate.eligible(task, dict(receipt, supervision="posix-pgroup-cooperative",
                                                  termination_evidence="pgroup-empty")))

    def test_spec_and_probe_carry_only_current_consent(self):
        ex = self.app.execution
        task = {"installation_id": "codex-local", "execution_id": "exec-" + "0" * 32, "spec_digest": "d",
                "prompt": "p", "inputs": []}
        self.assertIs(ex.spec(task)["allow_pgroup_cooperative"], True)
        # Explicit L0 carries the flag without writing the consent field.
        ex.hosts["local"].pop(consent.CONSENT_KEY, None)
        self.assertNotIn(consent.CONSENT_KEY, ex.hosts["local"])
        self.assertIs(ex.spec(task)["allow_pgroup_cooperative"], True)
        seen = {}
        with patch.object(ex, "call", lambda host, action, directory, payload=None: seen.setdefault("p", payload)):
            ex.probe("codex-local")
            self.assertIs(seen.pop("p")["allow_pgroup_cooperative"], True)
            self.assertNotIn(consent.CONSENT_KEY, ex.hosts["local"])
            # Flag absent: only the current consent text turns the tier on.
            ex.deployment_level = None
            self.assertNotIn("allow_pgroup_cooperative", ex.spec(task))
            ex.probe("codex-local")
            self.assertNotIn("allow_pgroup_cooperative", seen.pop("p"))
            for value in ("l0-pgroup-risk-v0", True):
                ex.hosts["local"][consent.CONSENT_KEY] = value
                self.assertNotIn("allow_pgroup_cooperative", ex.spec(task), value)
            ex.hosts["local"][consent.CONSENT_KEY] = consent.RISK_VERSION
            ex.probe("codex-local")
            self.assertIs(seen.pop("p")["allow_pgroup_cooperative"], True)


class ConnectorResolveTests(unittest.TestCase):
    """Adversarial: a receipt the cloud refuses (and ACKs) must stay resolvable on
    the device; device and cloud use the same predicate, so no dead end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / "workspace"; self.root.mkdir()
        local = consenting(self.root)
        local["installations"][0]["enabled"] = True
        local.update(host_id="local", cloud_url="http://127.0.0.1:1", credential_env="TEST_DEVICE_KEY")
        self.connector = Connector(local, self.root.parent / "device")
        cloud = config(self.root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        self.app = Service(cloud, self.root.parent / "cloud", deployment_level="L0")
        self.connector.request = lambda body: self.app.broker.sync("local", body)
        self.connector.tick()
        self.app.store.put("probe", {"id": "codex-local", "available": True, "supervision": "posix-pgroup-cooperative",
                                     "policy_digest": policy_digest(self.connector.config, "codex-local")})

    def tearDown(self):
        self.connector.close(); self.app.store.close(); self.tmp.cleanup()

    def test_refused_receipt_then_operator_resolve_closes(self):
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "cr")
        r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        self.app.task_step()
        for cmd in self.app.store.find("command", action="start"): # never deliver a real launch
            cmd.update(status="done", result={}); self.app.store.put("command", cmd)
        task = self.app.store.get("task", r["task_id"])
        bogus = {"execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
                 "attempt_uuid": task.get("attempt_uuid"), "status": "succeeded", "exit_code": 0,
                 "process_tree_confirmed": True, "supervision": "posix-pgroup-cooperative", "artifacts": []}
        device_task = {k: task.get(k) for k in ("execution_id", "spec_digest", "attempt_uuid", "host_id",
                                                 "installation_id", "supervision_floor")}
        self.connector.store.put("local_run", {"id": task["execution_id"], "task": device_task,
                                               "status": "active", "receipt": bogus})
        self.connector.tick()
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "unknown")
        self.assertEqual(self.connector.store.get("local_run", task["execution_id"])["status"], "acknowledged")
        self.app.resolve_task(task["id"], {"resolution": "confirmed_stopped", "evidence": "operator checked host"})
        for _ in range(4):
            self.connector.tick()
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "cancelled")
        self.assertNotEqual(self.app.store.get("dependency", r["dependency_id"])["status"], "pending")


class FloorContractTests(unittest.TestCase):
    """The floor is signed in the v3 contract and only reaches a device that
    proved v3."""

    def _task(self, **over):
        from pangpang.store import launch_contract, digest
        task = {"execution_id": "exec-" + "b" * 32, "installation_id": "codex-local", "cwd": "/repo",
                "timeout_s": 900, "access_mode": "write", "workspace_key": ["h", ["repo"]], "inputs": [],
                "outputs": [], "resource_keys": [], "prompt": "hi", "attempt_uuid": "att",
                "policy_digest": "pd", "supervision_floor": "linux-cgroup-cooperative"}
        task.update(over)
        task["contract"] = launch_contract(task)
        task["contract_digest"] = digest(task["contract"])
        task["min_contract_version"] = task["contract"]["contract_version"]
        return task

    def test_floor_is_bound(self):
        from pangpang.store import verify_launch_contract, digest
        task = self._task()
        self.assertEqual(task["contract"]["contract_version"], 3)
        self.assertEqual(task["contract"]["supervision_floor"], "linux-cgroup-cooperative")
        self.assertIsNone(verify_launch_contract(task))
        # top-level deleted / weakened, contract untouched
        for mutate in (lambda t: t.pop("supervision_floor"),
                       lambda t: t.update(supervision_floor="posix-pgroup-cooperative")):
            t = self._task(); mutate(t)
            self.assertEqual(verify_launch_contract(t), "local_contract_payload_mismatch")
        # weakened inside the contract without re-signing
        t = self._task(); t["contract"]["supervision_floor"] = "posix-pgroup-cooperative"
        self.assertEqual(verify_launch_contract(t), "local_contract_digest_mismatch")
        # unknown floor, consistently signed
        t = self._task(supervision_floor="invented")
        self.assertEqual(verify_launch_contract(t), "local_contract_floor_invalid")
        # v3 re-shaped as a floor-less v2 contract: the marker still demands 3
        t = self._task(); t.pop("supervision_floor"); del t["contract"]["supervision_floor"]
        t["contract"]["contract_version"] = 2; t["contract_digest"] = digest(t["contract"])
        self.assertEqual(verify_launch_contract(t), "local_contract_version_unsupported")

    def test_device_that_speaks_only_v2_rejects_v3(self):
        from pangpang.store import verify_launch_contract
        self.assertEqual(verify_launch_contract(self._task(), device_contract_version=2),
                         "local_contract_version_unsupported")

    def test_legacy_floorless_task_still_launches_on_new_device(self):
        from pangpang.store import verify_launch_contract
        task = self._task(supervision_floor=None)
        task.pop("supervision_floor")
        from pangpang.store import launch_contract, digest
        task["contract"] = launch_contract(task); task["contract_digest"] = digest(task["contract"])
        task["min_contract_version"] = 2
        self.assertEqual(task["contract"]["contract_version"], 2)
        self.assertIsNone(verify_launch_contract(task))


class MixedVersionTests(unittest.TestCase):
    """End to end: new cloud x device that only proves contract v2."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / "workspace"; self.root.mkdir()
        cloud = config(self.root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        self.app = Service(cloud, self.root.parent / "cloud", deployment_level="L0")
        self.app.store.put("probe", {"id": "codex-local", "available": True,
                                     "supervision": "linux-cgroup-cooperative", "policy_digest": "pd"})

    def tearDown(self):
        self.app.store.close(); self.tmp.cleanup()

    def sync_as_v2_device(self):
        return self.app.broker.sync("local", {"instance_id": "device-" + "0" * 32, "supported_contract_version": 2,
                                              "reports": [], "receipts": []})

    def test_v3_task_is_held_never_delivered_then_resolvable(self):
        self.sync_as_v2_device()
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "mv")
        r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        task = self.app.store.get("task", r["task_id"])
        self.assertEqual(task["contract_version"], 3)
        self.app.task_step()
        for _ in range(3):
            self.assertEqual(self.sync_as_v2_device()["commands"], []) # nothing reaches the old device
        self.assertEqual(self.app.store.find("command", action="start"), [])
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "starting")
        # Held past its deadline -> unknown; the operator can close it cloud-side
        # because the start provably never left the cloud (no dead end).
        self.app._mark_task_unknown(self.app.store.get("task", task["id"]), "capability_hold_deadline")
        self.app.resolve_task(task["id"], {"resolution": "confirmed_never_started",
                                           "evidence": "held: device never proved v3"})
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "cancelled")
        self.assertEqual(self.app.store.find("command", action="resolve_run"), [])
        dep = self.app.store.get("dependency", r["dependency_id"])
        self.assertNotEqual(dep["status"], "pending")
        with self.assertRaises(Fault): # settles once
            self.app.resolve_task(task["id"], {"resolution": "confirmed_never_started", "evidence": "again, twice"})

    def test_delivered_start_still_needs_the_device(self):
        # A start that did reach a device keeps the device-side resolution.
        self.app.broker.sync("local", {"instance_id": "device-" + "0" * 32, "supported_contract_version": 3,
                                       "reports": [], "receipts": []})
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "mv2")
        r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        self.app.task_step()
        commands = self.app.broker.sync("local", {"instance_id": "device-" + "0" * 32, "supported_contract_version": 3,
                                                  "reports": [], "receipts": []})["commands"]
        self.assertEqual([c["action"] for c in commands], ["start"])
        self.assertEqual(commands[0]["payload"]["contract"]["supervision_floor"], "linux-cgroup-cooperative")
        task = self.app.store.get("task", r["task_id"])
        self.app._mark_task_unknown(task, "test")
        out = self.app.resolve_task(task["id"], {"resolution": "confirmed_stopped", "evidence": "operator checked"})
        self.assertEqual(out["resolution_status"], "awaiting_device_receipt")
        self.assertEqual(len(self.app.store.find("command", action="resolve_run")), 1)


class ResolveBacklogTests(unittest.TestCase):
    """Resolve must read THIS attempt's start command exactly; a host
    history of >1000 start commands must never turn "not seen" into "never sent"."""
    DEVICE = "device-" + "0" * 32

    def app_with_history(self, n):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "workspace"; root.mkdir()
        cloud = config(root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        app = Service(cloud, root.parent / "cloud", deployment_level="L0")
        self.addCleanup(app.store.close)
        app.store.put("probe", {"id": "codex-local", "available": True,
                                "supervision": "linux-cgroup-cooperative", "policy_digest": "pd"})
        with app.store.atomic():
            for i in range(n):
                app.store.put("command", {"id": "cmd-hist-%d" % i, "host_id": "local", "action": "start",
                                          "payload": {"execution_id": "exec-%032x" % i}, "status": "done",
                                          "result": {"accepted": True}, "expires": None})
        return app

    def sync(self, app, reports=()):
        return app.broker.sync("local", {"instance_id": self.DEVICE, "supported_contract_version": 3,
                                         "reports": list(reports), "receipts": []})

    def unknown_task(self, app, state):
        self.sync(app)
        m = app.send("bot-main", {"text": "x", "allow_write": True}, "bl")
        r = app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        app.task_step()
        task = app.store.get("task", r["task_id"])
        start_id = app.broker.command_id("local", "start", task["execution_id"])
        self.assertEqual(app.store.get("command", start_id)["status"], "pending")
        if state in ("delivered", "done"):
            self.assertEqual([c["id"] for c in self.sync(app)["commands"]], [start_id])
        if state == "done":
            self.sync(app, [{"id": start_id, "result": {"accepted": True}}])
        app._mark_task_unknown(app.store.get("task", task["id"]), "test")
        return task, r["dependency_id"], start_id

    def test_history_matrix(self):
        for n in (0, 999, 1000, 1001):
            for state in ("pending", "delivered", "done"):
                with self.subTest(history=n, start=state):
                    app = self.app_with_history(n)
                    task, dep_id, start_id = self.unknown_task(app, state)
                    before = app.store.get("command", start_id)["status"]
                    out = app.resolve_task(task["id"], {"resolution": "confirmed_stopped", "evidence": "operator check"})
                    start = app.store.get("command", start_id)
                    if state == "pending":
                        # Provably never sent: superseded, settled once, never delivered later.
                        self.assertEqual(out["status"], "cancelled")
                        self.assertEqual(start["status"], "done")
                        self.assertTrue(start["result"]["superseded_by_operator_resolution"])
                        self.assertNotEqual(app.store.get("dependency", dep_id)["status"], "pending")
                        self.assertNotIn(start_id, [c["id"] for c in self.sync(app)["commands"]])
                        with self.assertRaises(Fault):
                            app.resolve_task(task["id"], {"resolution": "confirmed_stopped", "evidence": "again"})
                    else:
                        # Reached the device: only the device can prove it stopped.
                        self.assertEqual(out["resolution_status"], "awaiting_device_receipt")
                        self.assertEqual(start["status"], before)
                        self.assertEqual(app.store.get("task", task["id"])["status"], "unknown")
                        self.assertEqual(app.store.get("dependency", dep_id)["status"], "pending")
                        self.assertEqual(len(app.store.find("command", action="resolve_run")), 1)


class LateStartTests(unittest.TestCase):
    """A start thread holding a stale 'starting' snapshot must not enqueue
    after cancel -> device not_found -> unknown -> operator resolve. Real Service,
    Broker, Connector and runner; the interleaving is fixed by capturing the
    snapshot instead of racing threads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / "workspace"; self.root.mkdir()
        self.canary = self.root / "late-start-canary.txt"
        local = consenting(self.root)
        local["installations"][0].update(enabled=True, argv=[sys.executable, "-c",
            "import pathlib; pathlib.Path(%r).write_text('ran')" % str(self.canary)])
        local.update(host_id="local", cloud_url="http://127.0.0.1:1", credential_env="TEST_DEVICE_KEY")
        self.connector = Connector(local, self.root.parent / "device")
        cloud = config(self.root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        self.app = Service(cloud, self.root.parent / "cloud", deployment_level="L0")
        self.connector.request = lambda body: self.app.broker.sync("local", body)
        self.connector.tick() # proves contract v3
        self.app.store.put("probe", {"id": "codex-local", "available": True, "supervision": "posix-pgroup-cooperative",
                                     "policy_digest": policy_digest(self.connector.config, "codex-local")})

    def tearDown(self):
        for run in self.connector.store.find("local_run", limit=100):
            try:
                self.connector.execution.cancel(run["task"])
            except Exception:
                pass
        self.connector.close(); self.app.store.close(); self.tmp.cleanup()

    def starting_snapshot(self):
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "late")
        r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        snaps = []
        with patch.object(self.app, "_try_start", side_effect=lambda t: snaps.append(dict(t)) or True):
            self.app.task_step()
        self.assertEqual(snaps[0]["status"], "starting")
        return snaps[0], r["dependency_id"]

    def start_id(self, task):
        return self.app.broker.command_id("local", "start", task["execution_id"])

    def test_cancel_notfound_resolve_then_late_start_is_withdrawn(self):
        snap, dep_id = self.starting_snapshot()
        self.app.cancel(snap["id"])
        self.assertEqual(self.app.store.get("task", snap["id"])["status"], "cancel_requested")
        for _ in range(3):
            self.connector.tick() # device: cancel -> not_found (no run yet) -> unknown
        self.assertEqual(self.app.store.get("task", snap["id"])["status"], "unknown")
        self.app.resolve_task(snap["id"], {"resolution": "confirmed_never_started", "evidence": "no run, no start"})
        self.assertEqual(self.app.store.get("task", snap["id"])["status"], "cancelled")
        self.assertNotEqual(self.app.store.get("dependency", dep_id)["status"], "pending")
        # The paused start thread resumes with its stale snapshot.
        self.assertTrue(self.app._try_start(snap))
        with self.assertRaises(Fault):
            self.app.store.get("command", self.start_id(snap))
        for _ in range(3):
            self.connector.tick()
        time.sleep(.3)
        self.assertFalse(self.canary.exists(), "a resolved task must never start afterwards")
        self.assertEqual(self.connector.store.find("local_run", limit=10), [])
        self.assertEqual(self.app.store.get("task", snap["id"])["status"], "cancelled")
        # Repeated stale calls and repeated resolve stay inert.
        self.app._try_start(snap)
        with self.assertRaises(Fault):
            self.app.resolve_task(snap["id"], {"resolution": "confirmed_never_started", "evidence": "second try"})

    def test_cancel_requested_withdraws_a_not_yet_enqueued_start(self):
        # Check-then-enqueue has no gap: once the task left 'starting', the start
        # is refused inside the enqueue transaction.
        snap, _ = self.starting_snapshot()
        self.app.cancel(snap["id"])
        self.assertEqual(self.app.broker.start(snap), {"status": "start_withdrawn"})
        with self.assertRaises(Fault):
            self.app.store.get("command", self.start_id(snap))

    def test_stale_attempt_snapshot_cannot_launch_or_touch_newer_attempt(self):
        snap, _ = self.starting_snapshot()
        current = self.app.store.get("task", snap["id"])
        current["attempt_uuid"] = "newer-attempt"
        self.app.store.put("task", current)
        self.assertTrue(self.app._try_start(snap))
        with self.assertRaises(Fault):
            self.app.store.get("command", self.start_id(snap))
        after = self.app.store.get("task", snap["id"])
        self.assertEqual((after["status"], after["attempt_uuid"]), ("starting", "newer-attempt"))
        self.assertNotIn("supervisor", after)

    def test_current_attempt_still_starts_and_resolve_supersedes_pending(self):
        snap, dep_id = self.starting_snapshot()
        self.assertTrue(self.app._try_start(snap))
        self.assertEqual(self.app.store.get("command", self.start_id(snap))["status"], "pending")
        self.app._mark_task_unknown(self.app.store.get("task", snap["id"]), "test")
        self.app.resolve_task(snap["id"], {"resolution": "confirmed_never_started", "evidence": "pending only"})
        self.assertEqual(self.app.store.get("command", self.start_id(snap))["status"], "done")
        for _ in range(3):
            self.connector.tick()
        time.sleep(.3)
        self.assertFalse(self.canary.exists())


class DetectionTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "linux detection")
    def test_no_silent_fallback_without_consent(self):
        with patch.object(runner, "_probe_linux_cgroup_scope", return_value=False):
            self.assertEqual(runner._detect_supervision(), "posix-process-group")
            self.assertEqual(runner._detect_supervision(True), "posix-pgroup-cooperative")
        with patch.object(runner, "_probe_linux_cgroup_scope", return_value=True):
            # A stronger backend always wins, with or without consent.
            self.assertEqual(runner._detect_supervision(True), "linux-cgroup-cooperative")
        with patch.object(runner, "_probe_linux_cgroup_scope", return_value=False), \
                patch.object(runner, "_unreaped_exit_supported", return_value=False):
            self.assertEqual(runner._detect_supervision(True), "posix-process-group")

    @unittest.skipUnless(sys.platform == "darwin", "macOS detection")
    def test_macos_labels(self):
        self.assertEqual(runner._detect_supervision(), "macos-pgroup-postverify")
        self.assertEqual(runner._detect_supervision(True), "posix-pgroup-cooperative")


class SshBootstrapTests(unittest.TestCase):
    def test_prepare_does_not_ship_a_remote_runner(self):
        # The SSH bootstrap that wrote runner.py on the far side is gone.
        # prepare must fail before any subprocess, and must not create the dir.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "run"
            ex = Execution({"hosts": [{"id": "s", "transport": "ssh", "os": "Linux", "target": "u@h",
                                       "runner_root": "/srv/xpp", "allowed_roots": ["/srv/w"]}],
                            "installations": []}, Path(tmp) / "data")
            with patch.object(Execution, "_run") as run, \
                    patch("pangpang.execution.subprocess.run") as spawned:
                with self.assertRaises(Fault) as caught:
                    ex.prepare(ex.hosts["s"], str(target), {"execution_id": "x"})
                self.assertEqual(caught.exception.code, "ssh_transport_retired")
                run.assert_not_called()
                spawned.assert_not_called()
            self.assertFalse(target.exists())


class ConsentToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / "workspace"; self.root.mkdir()
        self.data = self.root.parent / "data"
        Service(not_consenting(self.root), self.data).store.close()
        from pangpang.store import Store
        store = Store(self.data / "bots.db")
        store.put("settings", {"id": "managed", "config": not_consenting(self.root)})
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def host(self):
        from pangpang.store import Store
        store = Store(self.data / "bots.db")
        try:
            return store.get("settings", "managed")["config"]["hosts"][0]
        finally:
            store.close()

    def run_tool(self, action, typed=None, tty=True):
        with patch("sys.stdin.isatty", return_value=tty), patch("builtins.input", return_value=typed or ""), \
                patch("builtins.print"):
            return consent.main([action, "--data", str(self.data)])

    def test_accept_requires_interactive_exact_version(self):
        with self.assertRaises(SystemExit):
            self.run_tool("accept", consent.RISK_VERSION, tty=False)
        with self.assertRaises(SystemExit):
            self.run_tool("accept", "yes")
        self.assertNotIn(consent.CONSENT_KEY, self.host())
        self.assertEqual(self.run_tool("accept", consent.RISK_VERSION), 0)
        self.assertTrue(consent.host_consented(self.host()))
        self.assertEqual(self.run_tool("revoke"), 0)
        self.assertNotIn(consent.CONSENT_KEY, self.host())

    def test_refuses_while_service_holds_lock(self):
        from pangpang.__main__ import ProcessLock
        lock = ProcessLock(self.data / "coordinator.lock")
        try:
            with self.assertRaises(SystemExit):
                self.run_tool("accept", consent.RISK_VERSION)
        finally:
            lock.file.close()
        self.assertNotIn(consent.CONSENT_KEY, self.host())

    def test_superseded_risk_text_consent_does_not_count(self):
        self.assertEqual(consent.RISK_VERSION, "l0-pgroup-risk-v2")
        self.assertFalse(consent.host_consented({consent.CONSENT_KEY: "l0-pgroup-risk-v1"}))
        self.assertIn("detached", consent.RISK_TEXT)
        self.assertIn("密钥", consent.RISK_TEXT)

    def test_l0_risk_text_is_not_an_os_sandbox(self):
        # The supported macOS path is: stop the service, then on the device
        # `python -m pangpang.consent accept --data <data>`, then an explicit
        # L0 start or a reprobe. The text itself says this tier is not isolation.
        self.assertIn("不是隔离", consent.RISK_TEXT)
        self.assertIn("同一账号", consent.RISK_TEXT)

    def test_consent_is_part_of_device_policy_digest(self):
        cfg = not_consenting(self.root)
        before = policy_digest(cfg, "codex-local")
        cfg["hosts"][0][consent.CONSENT_KEY] = consent.RISK_VERSION
        self.assertNotEqual(before, policy_digest(cfg, "codex-local"))


def _pgroup_runtime_supported():
    if os.name != "posix" or not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return False
    if not runner._unreaped_exit_supported():
        return False
    # On a host with a usable systemd --user scope the runner rightly prefers cgroup.
    return not (sys.platform.startswith("linux") and runner._probe_linux_cgroup_scope())


@unittest.skipUnless(_pgroup_runtime_supported(), "needs POSIX without a cgroup scope")
class ProcessGroupRuntimeTests(unittest.TestCase):
    """Real runner processes; mirrors ExecutionTests.start_task."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / "workspace"; self.root.mkdir()
        self.escaped = []

    def tearDown(self):
        for pid in self.escaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if hasattr(self, "app"):
            self.app.store.close()
        self.tmp.cleanup()

    def run_task(self, code, *, consented=True, level="L0", cancel_after_start=False, timeout_s=None):
        cfg = consenting(self.root) if consented else config(self.root)
        cfg["installations"][0]["argv"] = [sys.executable, "-c", code]
        if timeout_s:
            cfg["installations"][0]["timeout_s"] = timeout_s
        self.app = Service(cfg, self.root.parent / "data", deployment_level=level)
        self.app.probe("codex-local")
        msg = self.app.send("bot-main", {"text": "task", "allow_write": True}, "exec")
        result = self.app.submit_task("bot-main", msg["work_id"], {"prompt": "task"})
        self.app.task_step()
        task = self.app.store.get("task", result["task_id"])
        directory = Path(self.app.execution.directory(self.app.execution.hosts["local"], task["execution_id"]))
        if cancel_after_start:
            for _ in range(200):
                if (directory / "started.json").exists():
                    break
                time.sleep(.03)
            self.app.cancel(task["id"])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.app.task_step()
            task = self.app.store.get("task", task["id"])
            if task["status"] in {"succeeded", "failed", "cancelled", "unknown"} and (directory / "receipt.json").exists():
                break
            time.sleep(.05)
        return task, runner.inspect(directory)

    def test_probe_advertises_tier_only_with_consent(self):
        # Explicit L0 is enough. The consent field is not written, and is not required.
        self.app = Service(not_consenting(self.root), self.root.parent / "data", deployment_level="L0")
        self.assertEqual(self.app.probe("codex-local")["supervision"], "posix-pgroup-cooperative")
        self.assertNotIn(consent.CONSENT_KEY, self.app.execution.hosts["local"])
        self.app.store.close()
        self.app = Service(not_consenting(self.root), self.root.parent / "data-undeclared", deployment_level=None)
        self.assertNotEqual(self.app.probe("codex-local")["supervision"], "posix-pgroup-cooperative")
        self.assertNotIn(consent.CONSENT_KEY, self.app.execution.hosts["local"])

    def test_normal_run_commits_once_with_group_scope_evidence(self):
        task, receipt = self.run_task("print('pg-ok')")
        self.assertEqual(task["status"], "succeeded", task)
        for r in (task["receipt"], receipt):
            self.assertIs(r["process_tree_confirmed"], True)
            self.assertEqual(r["supervision"], "posix-pgroup-cooperative")
            self.assertEqual(r["termination_evidence"], "pgroup-empty")
        self.assertIn("pg-ok", task["receipt"]["tail"])

    def test_group_residue_is_terminated(self):
        pidfile = self.root / "child.pid"
        code = ("import subprocess,sys,pathlib;"
                "q=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
                "pathlib.Path(%r).write_text(str(q.pid))" % str(pidfile))
        task, _ = self.run_task(code)
        self.assertEqual(task["status"], "succeeded", task)
        child = int(pidfile.read_text())
        self.escaped.append(child)
        self.assertIsNot(runner.process_alive(child), True, "in-group residue must be killed before release")

    def test_setsid_descendant_escapes_and_is_not_claimed(self):
        # the group tier can leak a setsid/double-fork descendant. The
        # receipt claims only the group scope; the escapee keeps running (and could
        # keep writing) — the documented, operator-accepted limitation.
        pidfile, canary = self.root / "escaped.pid", self.root / "canary.txt"
        writer = self.root / "writer.py"
        writer.write_text("import pathlib,sys,time\n"
                          "while True:\n"
                          " pathlib.Path(sys.argv[1]).write_text(str(time.time()))\n"
                          " time.sleep(.1)\n", encoding="utf-8")
        code = ("import subprocess,sys,pathlib;"
                "q=subprocess.Popen([sys.executable,%r,%r],start_new_session=True);"
                "pathlib.Path(%r).write_text(str(q.pid))" % (str(writer), str(canary), str(pidfile)))
        task, receipt = self.run_task(code)
        escaped = int(pidfile.read_text())
        self.escaped.append(escaped)
        self.assertEqual(task["status"], "succeeded", task)
        self.assertEqual(receipt["termination_evidence"], "pgroup-empty")
        self.assertIs(runner.process_alive(escaped), True)
        first = canary.read_text()
        time.sleep(.5)
        self.assertNotEqual(first, canary.read_text(), "escaped descendant still writes the workspace")

    def _launch_bypassing_route(self, cfg, canary, **task_extra):
        cfg["installations"][0]["argv"] = [sys.executable, "-c",
                                           "import pathlib; pathlib.Path(%r).write_text('ran')" % str(canary)]
        self.app = Service(cfg, self.root.parent / "data", deployment_level="L0")
        self.app.store.put("probe", {"id": "codex-local", "available": True, "supervision": "posix-pgroup-cooperative"})
        msg = self.app.send("bot-main", {"text": "task", "allow_write": True}, "exec")
        result = self.app.submit_task("bot-main", msg["work_id"], {"prompt": "task"})
        task = self.app.store.get("task", result["task_id"])
        task.update(task_extra)
        self.app.store.put("task", task)
        return task

    def _settle(self, task):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.app.task_step()
            task = self.app.store.get("task", task["id"])
            if task["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                return task
            time.sleep(.05)
        self.fail("no terminal state: %s" % task)

    def test_revoked_consent_never_executes(self):
        # The group tier still does not run when the L0 declaration is gone.
        # Admission is bypassed here so the runner's own pre-launch check is what refuses.
        canary = self.root / "ran-after-revoke.txt"
        task = self._launch_bypassing_route(consenting(self.root), canary)
        self.app.execution.hosts["local"].pop(consent.CONSENT_KEY)
        self.app.deployment_level = None
        self.app.execution.deployment_level = None
        self.app._start_denied = lambda task: None # simulate an admission race
        task = self._settle(task)
        self.assertFalse(canary.exists(), "CLI must not run without current consent")
        self.assertEqual(task["status"], "failed", task)
        # No-consent label is whatever this platform's detector returns when
        # process-group supervision is not allowed. Darwin keeps
        # macos-pgroup-postverify; posix-process-group is the Linux probe.
        self.assertIn("supervision_unavailable:" + runner._detect_supervision(False), task["receipt"]["error"])
        self.assertNotIn("exit_code", task["receipt"])
        self.assertEqual(task["receipt"]["not_launched"], "runner_prelaunch")

    def test_floor_refuses_silent_downgrade(self):
        # admitted as cgroup, only a process group available at launch.
        canary = self.root / "ran-downgraded.txt"
        task = self._launch_bypassing_route(consenting(self.root), canary,
                                            supervision_floor="linux-cgroup-cooperative")
        task = self._settle(task)
        self.assertFalse(canary.exists())
        self.assertEqual(task["status"], "failed", task)
        self.assertIn("supervision_unavailable:posix-pgroup-cooperative", task["receipt"]["error"])

    def test_floorless_spec_never_falls_back_to_group(self):
        # CCC-review P0 residual: a spec without a frozen floor (older control side /
        # previously task) must not use the group tier even when the host consented.
        canary = self.root / "ran-floorless.txt"
        task = self._launch_bypassing_route(consenting(self.root), canary, supervision_floor=None)
        spec = self.app.execution.spec(task)
        self.assertIs(spec["allow_pgroup_cooperative"], True)
        self.assertNotIn("supervision_floor", spec)
        task = self._settle(task)
        self.assertFalse(canary.exists())
        self.assertEqual(task["status"], "failed", task)
        # Floorless specs also refuse before launch. The label is the detector's
        # no-consent result on this platform, not a hardcoded Linux probe string.
        self.assertIn("supervision_unavailable:" + runner._detect_supervision(False), task["receipt"]["error"])
        self.assertEqual(task["receipt"]["not_launched"], "runner_prelaunch")

    def test_eperm_group_member_is_not_confirmed(self):
        # CCC-review P2: a group member we may not signal (setuid) reads as alive, so
        # the attempt stays unconfirmed (unknown) instead of releasing.
        with patch.object(runner.os, "killpg", side_effect=PermissionError()):
            self.assertIs(runner.group_alive(12345), True)
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        exited = runner._exit_watcher(proc.pid)
        while not exited():
            time.sleep(.02)
        real_killpg = runner.os.killpg
        def killpg(pgid, sig):
            if sig == 0:
                raise PermissionError()
            return real_killpg(pgid, sig)
        with patch.object(runner.os, "killpg", side_effect=killpg), patch.object(runner.time, "sleep"):
            self.assertIs(runner._settle_process_group(proc), False)

    def test_settle_names_the_step_that_left_the_group_unconfirmed(self):
        # Historical case D. The pre-reap SIGKILL is stubbed in every case: what a real
        # kill to a zombie-only group returns is platform-specific (macOS: EPERM).
        def exited_leader():
            proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
            exited = runner._exit_watcher(proc.pid)
            while not exited():
                time.sleep(.02)
            return proc
        real_killpg = runner.os.killpg
        def kill_denied(probe):
            def killpg(pgid, sig):
                if sig != 0:
                    raise PermissionError(errno.EPERM, "denied")
                return probe(pgid, sig)
            return killpg
        def probe_denied(pgid, sig):
            raise PermissionError(errno.EPERM, "denied")
        # macOS: SIGKILL to a group holding only our unreaped zombie leader is
        # EPERM. After the reap, signal 0 finds the group gone: confirmed.
        proc = exited_leader()
        why = []
        with patch.object(runner.os, "killpg", side_effect=kill_denied(real_killpg)):
            self.assertIs(runner._settle_process_group(proc, why), True)
        self.assertEqual(why, [])
        # A member we may not signal: EPERM on the kill and on the probe after the
        # reap. Never confirmed; both steps are named.
        proc = exited_leader()
        why = []
        with patch.object(runner.os, "killpg", side_effect=kill_denied(probe_denied)), \
                patch.object(runner.time, "sleep"):
            self.assertIs(runner._settle_process_group(proc, why), False)
        self.assertEqual(why, ["killpg_EPERM", "group_probe_EPERM"])
        # Any other kill error stops before the reap.
        proc = exited_leader()
        why = []
        with patch.object(runner.os, "killpg", side_effect=OSError(errno.EINVAL, "invalid")):
            self.assertIs(runner._settle_process_group(proc, why), False)
        proc.wait()
        self.assertEqual(why, ["killpg_EINVAL"])

    def test_unknown_floor_refused_before_launch(self):
        canary = self.root / "ran-unknown-floor.txt"
        task = self._launch_bypassing_route(consenting(self.root), canary, supervision_floor="invented-floor")
        task = self._settle(task)
        self.assertFalse(canary.exists())
        self.assertEqual(task["status"], "failed", task)
        self.assertIn("supervision_floor_invalid", task["receipt"]["error"])

    def test_cancel_is_bounded_and_confirmed(self):
        task, receipt = self.run_task("import time;time.sleep(60)", cancel_after_start=True)
        self.assertEqual(task["status"], "cancelled", task)
        self.assertEqual(receipt["termination_evidence"], "pgroup-empty")

    def test_timeout_ignoring_sigterm_is_force_killed(self):
        code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
        task, receipt = self.run_task(code, timeout_s=1)
        self.assertEqual(task["status"], "failed", task)
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(receipt["termination_evidence"], "pgroup-empty")

    def test_level_not_declared_refuses_route(self):
        cfg = consenting(self.root)
        self.app = Service(cfg, self.root.parent / "data", deployment_level=None) # undeclared
        self.app.probe("codex-local")
        msg = self.app.send("bot-main", {"text": "task", "allow_write": True}, "exec")
        with self.assertRaises(Fault) as caught:
            self.app.submit_task("bot-main", msg["work_id"], {"prompt": "task"})
        self.assertEqual(caught.exception.code, "executor_cleanup_unsupported")


class ExplicitL0MacProbeTests(unittest.TestCase):
    """--deployment-level L0 is the macOS cooperative-tier declaration.

    The startup log says that flag enables the L0 tiers. It must move a macOS
    probe off macos-pgroup-postverify without a second consent field, and a
    note task must be admissible. An undeclared level must not.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.store.close()
        self.tmp.cleanup()

    def _service(self, level):
        from pangpang.onboarding import empty_config
        app = Service(empty_config(), self.root / ("data-" + (level or "undeclared")), deployment_level=level)
        self.apps.append(app)
        return app

    def _macos_probe_call(self):
        # The local probe runs in a subprocess (Execution.call -> runner.py), so
        # in-process platform patches never reach it. Synthesize the probe the
        # way a macOS host would answer, with the platform stood in here.
        def call(execution, host, action, directory, payload=None):
            del execution, host, directory
            if action != "probe":
                raise AssertionError(action)
            with patch("pangpang.runner.os.name", "posix"), \
                    patch("pangpang.runner.sys.platform", "darwin"), \
                    patch("pangpang.runner._unreaped_exit_supported", return_value=True):
                label = runner._detect_supervision(
                    (payload or {}).get("allow_pgroup_cooperative") is True)
            return {"available": True, "binary": payload["argv"][0], "version": "test",
                    "os": "test", "supervision": label}
        return patch("pangpang.execution.Execution.call", call)

    def _discover(self, app):
        def which(binary):
            return sys.executable if binary == "claude" else None

        with patch("pangpang.service.shutil.which", side_effect=which), \
                self._macos_probe_call():
            app.ensure_local_host(str(self.workspace), discover=True)
            app.refresh_l0_local_probes()
        return app.store.get("probe", "inst-local-claude-code")

    def test_explicit_l0_macos_probe_is_not_the_rejected_stub(self):
        app = self._service("L0")
        probe = self._discover(app)
        self.assertNotIn(probe["supervision"], {"macos-pgroup-postverify", "posix-process-group"})
        self.assertEqual(probe["supervision"], "posix-pgroup-cooperative")
        self.assertTrue(probe.get("available"))
        host = app.execution.hosts["host-local"]
        self.assertNotIn(consent.CONSENT_KEY, host)
        # A later restart still has the installation. A stored macOS stub must not stick.
        app.store.put("probe", dict(probe, supervision="macos-pgroup-postverify"))
        with self._macos_probe_call():
            app.refresh_l0_local_probes()
        refreshed = app.store.get("probe", "inst-local-claude-code")
        self.assertNotEqual(refreshed["supervision"], "macos-pgroup-postverify")
        self.assertEqual(refreshed["supervision"], probe["supervision"])
        msg = app.send("bot-main", {"text": "记一条笔记", "allow_write": True}, "note")
        result = app.submit_task("bot-main", msg["work_id"], {
            "prompt": "写一条笔记", "installation_id": "inst-local-claude-code"})
        task = app.store.get("task", result["task_id"])
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["supervision_floor"], refreshed["supervision"])
        self.assertIsNone(app._start_denied(task))
        self.assertIs(app.execution.spec(task)["allow_pgroup_cooperative"], True)

        # Same flag the probe spec carries: on macOS it leaves the rejected stub.
        with patch("pangpang.runner.os.name", "posix"), \
                patch("pangpang.runner.sys.platform", "darwin"), \
                patch("pangpang.runner._unreaped_exit_supported", return_value=True):
            self.assertEqual(runner._detect_supervision(False), "macos-pgroup-postverify")
            self.assertEqual(runner._detect_supervision(True), "posix-pgroup-cooperative")

        undeclared = self._service(None)
        stub = self._discover(undeclared)
        self.assertIn(stub["supervision"], {"macos-pgroup-postverify", "posix-process-group"})
        self.assertNotIn(consent.CONSENT_KEY, undeclared.execution.hosts["host-local"])
        work = undeclared.send("bot-main", {"text": "记一条笔记", "allow_write": True}, "note")
        with self.assertRaises(Fault) as caught:
            undeclared.submit_task("bot-main", work["work_id"], {
                "prompt": "写一条笔记", "installation_id": "inst-local-claude-code"})
        self.assertEqual(caught.exception.code, "executor_cleanup_unsupported")


class StartHarness(unittest.TestCase):
    """In-process `python -m pangpang` start with serve() captured, for tests
    about what the entry point registers and declares at startup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            try:
                app.store.close()
            except Exception:
                pass
        self.tmp.cleanup()

    def _start(self, extra_argv, platform_name, contexts, *, omitted=True, allow_local_exec=True):
        data = self.root / ("data-" + str(len(self.apps)))
        workspace = self.root / ("workspace-" + str(len(self.apps)))
        workspace.mkdir()
        held = {}

        def serve(service, token, host, port, origin):
            held["service"] = service

            class Server:
                def serve_forever(self):
                    pass

                def shutdown(self):
                    pass

                def server_close(self):
                    pass

            return Server()

        argv = ["pangpang", "--data", str(data), "--workspace", str(workspace), *extra_argv]
        if allow_local_exec:
            # First-run local-executor consent (the operator's stand-in); these
            # tests are about deployment-level inference, not the consent gate.
            argv.append("--allow-local-exec")
        if omitted:
            self.assertNotIn("--deployment-level", argv)
        # Replace only the entry module's sys binding. Pathlib and argparse keep
        # the real sys module, so a Mac/Windows start can be reproduced here.
        fake_sys = types.SimpleNamespace(platform=platform_name)
        with patch.dict(os.environ, {"PANGPANG_TOKEN": "t" * 24}), \
                patch("sys.argv", argv), \
                patch("pangpang.__main__.serve", serve), \
                patch("pangpang.__main__.threading.Thread") as threads, \
                patch("pangpang.__main__.sys", fake_sys, create=True), \
                contextlib.ExitStack() as stack:
            threads.return_value.is_alive.return_value = False
            for ctx in contexts:
                stack.enter_context(ctx)
            from pangpang.__main__ import main
            main()
        self.assertIn("service", held, "coordinator did not reach serve")
        self.apps.append(held["service"])
        return held["service"]

    def _which_claude(self):
        def which(binary):
            return sys.executable if binary == "claude" else None
        return which

    def _which_executors(self, names):
        def which(binary):
            return sys.executable if binary in names else None
        return which

    def _probe_returning(self, label_for):
        def call(execution, host, action, directory, payload=None):
            del execution, host, directory
            if action != "probe":
                raise AssertionError(action)
            return {"available": True, "binary": payload["argv"][0], "version": "test",
                    "os": "test", "supervision": label_for(payload or {})}
        return patch("pangpang.execution.Execution.call", call)

    def _macos_label(self, payload):
        with patch("pangpang.runner.os.name", "posix"), \
                patch("sys.platform", "darwin"), \
                patch("pangpang.runner._unreaped_exit_supported", return_value=True):
            return runner._detect_supervision(payload.get("allow_pgroup_cooperative") is True)

    def _windows_label(self, payload):
        del payload
        with patch("pangpang.runner.os.name", "nt"):
            return runner._detect_supervision(False)

    def _linux_cgroup_label(self, payload):
        with patch("pangpang.runner.os.name", "posix"), \
                patch("sys.platform", "linux"), \
                patch("pangpang.runner._probe_linux_cgroup_scope", return_value=True):
            return runner._detect_supervision(payload.get("allow_pgroup_cooperative") is True)

    def _linux_weak_label(self, payload):
        with patch("pangpang.runner.os.name", "posix"), \
                patch("sys.platform", "linux"), \
                patch("pangpang.runner._probe_linux_cgroup_scope", return_value=False), \
                patch("pangpang.runner._unreaped_exit_supported", return_value=False):
            return runner._detect_supervision(payload.get("allow_pgroup_cooperative") is True)

    def _submit(self, app, adapter, key):
        inst = "inst-local-" + adapter
        msg = app.send("bot-main", {"text": "记一条笔记", "allow_write": True}, key)
        result = app.submit_task("bot-main", msg["work_id"], {
            "prompt": "写一条笔记", "installation_id": inst})
        return app.store.get("task", result["task_id"])


class OmittedDeploymentLevelStartTests(StartHarness):
    """The normal start is `python -m pangpang` with no --deployment-level flag
    (launch.py cloud execs that). On POSIX (macOS, Linux) that process infers L0.
    macOS admits a note to a discovered local claude on the process-group tier.
    Linux admits claude-code, codex, and gemini on the cgroup tier when the
    scope probe succeeds. The rejected stub stays rejected. Windows Job Object
    admission at an undeclared level stays as it is."""

    def test_omitted_flag_on_macos_admits_a_note_to_local_claude(self):
        app = self._start([], "darwin", [
            patch("pangpang.service.shutil.which", side_effect=self._which_claude()),
            self._probe_returning(self._macos_label),
        ])
        with patch("pangpang.runner.os.name", "posix"), \
                patch("sys.platform", "darwin"), \
                patch("pangpang.runner._unreaped_exit_supported", return_value=True):
            # The stub stays the rejected label. Admission uses the cooperative tier.
            self.assertEqual(runner._detect_supervision(False), "macos-pgroup-postverify")
            self.assertEqual(runner._detect_supervision(True), "posix-pgroup-cooperative")
        self.assertEqual(app.deployment_level, "L0")
        host = app.execution.hosts["host-local"]
        self.assertNotIn(consent.CONSENT_KEY, host)
        probe = app.store.get("probe", "inst-local-claude-code")
        self.assertTrue(probe.get("available"))
        self.assertEqual(probe["supervision"], "posix-pgroup-cooperative")
        self.assertNotEqual(probe["supervision"], "macos-pgroup-postverify")
        caps = {c["capability"]: c for c in app.effective_capabilities()}
        self.assertEqual(caps["executor"]["state"], "ready")
        self.assertNotEqual(caps["executor"]["reason"], "platform_supervision_unverified")
        msg = app.send("bot-main", {"text": "记一条笔记", "allow_write": True}, "note")
        result = app.submit_task("bot-main", msg["work_id"], {
            "prompt": "写一条笔记", "installation_id": "inst-local-claude-code"})
        task = app.store.get("task", result["task_id"])
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["supervision_floor"], "posix-pgroup-cooperative")
        self.assertIsNone(app._start_denied(task))
        self.assertIs(app.execution.spec(task)["allow_pgroup_cooperative"], True)
        # The stub itself is still not supervision, even after this start.
        app.store.put("probe", {"id": "inst-local-claude-code", "available": True,
                                 "supervision": "macos-pgroup-postverify"})
        blocked = {c["capability"]: c for c in app.effective_capabilities()}
        self.assertEqual(blocked["executor"]["state"], "blocked")
        self.assertEqual(blocked["executor"]["reason"], "platform_supervision_unverified")
        work = app.send("bot-main", {"text": "再记一条", "allow_write": True}, "note-stub")
        with self.assertRaises(Fault) as caught:
            app.submit_task("bot-main", work["work_id"], {
                "prompt": "写一条笔记", "installation_id": "inst-local-claude-code"})
        self.assertEqual(caught.exception.code, "executor_cleanup_unsupported")

    def test_omitted_flag_on_windows_still_admits_a_job_object(self):
        app = self._start([], "win32", [
            patch("pangpang.service.shutil.which", side_effect=self._which_claude()),
            self._probe_returning(self._windows_label),
        ])
        self.assertIsNone(app.deployment_level)
        self.assertNotIn(consent.CONSENT_KEY, app.execution.hosts["host-local"])
        probe = app.store.get("probe", "inst-local-claude-code")
        self.assertEqual(probe["supervision"], "windows-job-object")
        caps = {c["capability"]: c for c in app.effective_capabilities()}
        self.assertEqual(caps["executor"]["state"], "ready")
        msg = app.send("bot-main", {"text": "记一条笔记", "allow_write": True}, "note-win")
        result = app.submit_task("bot-main", msg["work_id"], {
            "prompt": "写一条笔记", "installation_id": "inst-local-claude-code"})
        task = app.store.get("task", result["task_id"])
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["supervision_floor"], "windows-job-object")
        self.assertIsNone(app._start_denied(task))

    def test_omitted_flag_on_linux_infers_l0_and_admits_cgroup(self):
        app = self._start([], "linux", [
            patch("pangpang.service.shutil.which",
                  side_effect=self._which_executors({"claude", "codex", "gemini"})),
            self._probe_returning(self._linux_cgroup_label),
        ])
        with patch("pangpang.runner.os.name", "posix"), \
                patch("sys.platform", "linux"), \
                patch("pangpang.runner._probe_linux_cgroup_scope", return_value=True):
            self.assertEqual(runner._detect_supervision(False), "linux-cgroup-cooperative")
        self.assertEqual(app.deployment_level, "L0")
        self.assertNotIn(consent.CONSENT_KEY, app.execution.hosts["host-local"])
        caps = {c["capability"]: c for c in app.effective_capabilities()}
        self.assertEqual(caps["executor"]["state"], "ready")
        self.assertNotEqual(caps["executor"]["reason"], "platform_supervision_unverified")
        for adapter in ("claude-code", "codex", "gemini"):
            inst = "inst-local-" + adapter
            probe = app.store.get("probe", inst)
            self.assertTrue(probe.get("available"), adapter)
            self.assertEqual(probe["supervision"], "linux-cgroup-cooperative", adapter)
            task = self._submit(app, adapter, "note-" + adapter)
            self.assertEqual(task["status"], "queued", adapter)
            self.assertEqual(task["supervision_floor"], "linux-cgroup-cooperative", adapter)
            self.assertIsNone(app._start_denied(task), adapter)

    def test_origin_on_linux_does_not_infer_l0(self):
        app = self._start(["--origin", "https://pangpang.example"], "linux", [
            patch("pangpang.service.shutil.which", side_effect=self._which_executors({"claude"})),
            self._probe_returning(self._linux_cgroup_label),
        ])
        self.assertIsNone(app.deployment_level)
        probe = app.store.get("probe", "inst-local-claude-code")
        self.assertEqual(probe["supervision"], "linux-cgroup-cooperative")
        caps = {c["capability"]: c for c in app.effective_capabilities()}
        self.assertEqual(caps["executor"]["state"], "blocked")
        self.assertEqual(caps["executor"]["reason"], "platform_supervision_unverified")
        with self.assertRaises(Fault) as caught:
            self._submit(app, "claude-code", "note-origin")
        self.assertEqual(caught.exception.code, "executor_cleanup_unsupported")

    def test_control_only_linux_omitted_flag_stays_undeclared(self):
        app = self._start(["--control-only"], "linux", [
            patch("pangpang.service.shutil.which", side_effect=self._which_executors({"claude"})),
        ])
        self.assertIsNone(app.deployment_level)
        self.assertTrue(app.control_only)
        self.assertEqual(app.execution.installations, {})

    def test_explicit_l1_on_linux_rejects_cgroup_probe(self):
        app = self._start(["--deployment-level", "L1"], "linux", [
            patch("pangpang.service.shutil.which", side_effect=self._which_executors({"claude"})),
            self._probe_returning(self._linux_cgroup_label),
        ], omitted=False)
        self.assertEqual(app.deployment_level, "L1")
        probe = app.store.get("probe", "inst-local-claude-code")
        self.assertEqual(probe["supervision"], "linux-cgroup-cooperative")
        caps = {c["capability"]: c for c in app.effective_capabilities()}
        self.assertEqual(caps["executor"]["state"], "blocked")
        self.assertEqual(caps["executor"]["reason"], "platform_supervision_unverified")
        with self.assertRaises(Fault) as caught:
            self._submit(app, "claude-code", "note-l1")
        self.assertEqual(caught.exception.code, "executor_cleanup_unsupported")

    def test_inferred_l0_rejects_weaker_probe_labels(self):
        app = self._start([], "linux", [
            patch("pangpang.service.shutil.which", side_effect=self._which_executors({"claude"})),
            self._probe_returning(self._linux_weak_label),
        ])
        self.assertEqual(app.deployment_level, "L0")
        probe = app.store.get("probe", "inst-local-claude-code")
        self.assertEqual(probe["supervision"], "posix-process-group")
        caps = {c["capability"]: c for c in app.effective_capabilities()}
        self.assertEqual(caps["executor"]["state"], "blocked")
        self.assertEqual(caps["executor"]["reason"], "platform_supervision_unverified")
        with self.assertRaises(Fault) as caught:
            self._submit(app, "claude-code", "note-weak")
        self.assertEqual(caught.exception.code, "executor_cleanup_unsupported")
        with patch("pangpang.runner.os.name", "posix"), \
                patch("sys.platform", "darwin"), \
                patch("pangpang.runner._probe_linux_cgroup_scope", return_value=False), \
                patch("pangpang.runner._unreaped_exit_supported", return_value=False):
            self.assertEqual(runner._detect_supervision(True), "macos-pgroup-postverify")
        app.store.put("probe", {"id": "inst-local-claude-code", "available": True,
                                 "supervision": "macos-pgroup-postverify"})
        with self.assertRaises(Fault) as caught:
            self._submit(app, "claude-code", "note-stub")
        self.assertEqual(caught.exception.code, "executor_cleanup_unsupported")

    def test_origin_on_macos_does_not_infer_l0(self):
        # --origin is evidence the coordinator is reached from another machine.
        # That is not a personal L0 start.
        app = self._start(["--origin", "https://pangpang.example"], "darwin", [
            patch("pangpang.service.shutil.which", side_effect=self._which_claude()),
            self._probe_returning(self._macos_label),
        ])
        self.assertIsNone(app.deployment_level)
        self.assertNotIn(consent.CONSENT_KEY, app.execution.hosts["host-local"])
        probe = app.store.get("probe", "inst-local-claude-code")
        self.assertNotEqual(probe["supervision"], "posix-pgroup-cooperative")

    def test_control_only_macos_omitted_flag_stays_undeclared(self):
        app = self._start(["--control-only"], "darwin", [
            patch("pangpang.service.shutil.which", side_effect=self._which_claude()),
        ])
        self.assertIsNone(app.deployment_level)
        self.assertTrue(app.control_only)
        self.assertEqual(app.execution.installations, {})


class FirstRunLocalExecConsentTests(StartHarness):
    """First-run registration of the local execution host requires explicit
    operator consent: interactive confirmation on a tty, or --allow-local-exec.
    Without either the registration is skipped (fail-closed) and serving
    continues; a granted consent persists in the managed config."""

    def test_no_consent_no_tty_skips_registration(self):
        app = self._start([], "linux", [
            patch("pangpang.service.shutil.which", side_effect=self._which_executors({"claude"})),
            patch("sys.stdin.isatty", return_value=False),
        ], allow_local_exec=False)
        self.assertFalse([h for h in app.execution.hosts.values() if h.get("transport") == "local"])
        self.assertEqual(app.execution.installations, {})

    def test_flag_grants_consent_and_persists(self):
        from pangpang import __main__ as entry
        app = self._start([], "linux", [
            patch("pangpang.service.shutil.which", side_effect=self._which_executors({"claude"})),
            self._probe_returning(self._linux_cgroup_label),
        ])
        self.assertIn("host-local", app.execution.hosts)
        self.assertEqual(app.store.get("settings", "managed")["config"].get(entry.LOCAL_EXEC_CONSENT_KEY),
                         entry.LOCAL_EXEC_CONSENT_VERSION)

    def test_tty_confirmation_grants_consent(self):
        fake_sys = types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda: True),
                                         stdout=sys.stdout, stderr=sys.stderr)
        app = self._start([], "linux", [
            patch("pangpang.service.shutil.which", side_effect=self._which_executors({"claude"})),
            self._probe_returning(self._linux_cgroup_label),
            patch("pangpang.__main__._SYS", fake_sys),
            patch("builtins.input", return_value="y"),
        ], allow_local_exec=False)
        self.assertIn("host-local", app.execution.hosts)


if __name__ == "__main__":
    unittest.main()
