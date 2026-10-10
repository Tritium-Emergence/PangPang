import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from test_bot_runtime import config
from pangpang.broker import global_profile
from pangpang.connector import Connector, policy_digest
from pangpang.http import serve
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault, digest


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Real path, as local_setup reports it. The cloud side keys a connector
        # host's cwd lexically and the device keys it by realpath, so an aliased
        # temp dir (macOS /var -> /private/var) would fail the workspace identity
        # check before any test logic runs.
        self.root = Path(self.tmp.name).resolve() / 'workspace'; self.root.mkdir()
        local = config(self.root)
        local["installations"][0]["enabled"] = True
        local.update(host_id="local", cloud_url="http://127.0.0.1:1",
                     credential_env="TEST_DEVICE_KEY")
        self.connector = Connector(local, self.root.parent / "device")
        cloud = config(self.root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        self.app = Service(cloud, self.root.parent / "cloud")
        self.connector.request = lambda body: self.app.broker.sync("local", body)
        self.connector.tick()
        probe = self.connector.execution.probe("codex-local")
        probe.update(id="codex-local", at=time.time(), policy_digest=policy_digest(self.connector.config, "codex-local"))
        self.app.store.put("probe", probe)

    def tearDown(self):
        # Always stop child trees on assertion failure, then retain confirmed receipts.
        for run in self.connector.store.find("local_run", status="active"):
            try:
                self.connector.execution.cancel(run["task"])
            except Exception:
                pass
        self.connector.close()
        self.app.store.close()
        self.tmp.cleanup()

    def task(self):
        m = self.app.send("bot-main", {"text": "制作结果", "allow_write": True}, "device-work")
        r = self.app.submit_task("bot-main", m["work_id"],
                                {"prompt": "中文内容", "outputs": ["result.txt"]})
        self.app.task_step()
        return self.app.store.get("task", r["task_id"])

    def finish(self, task):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.connector.tick()
            result = self.app.store.get("task", task["id"])
            if result["status"] in {"succeeded", "failed", "cancelled"}:
                return result
            time.sleep(.05)
        self.fail("no terminal receipt: " + str(self.app.store.get("task", task["id"])))

    def test_start_error_keeps_dependency_pending(self):
        # An unconfirmed device action quarantines but cannot settle the join.
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "err-work")
        r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        self.app.task_step() # enqueue the start command
        task = self.app.store.get("task", r["task_id"])
        cmd = self.app.store.find("command", action="start")[0]
        self.app.broker.sync("local", {"instance_id": self.connector.instance_id,
            "reports": [{"id": cmd["id"], "result": {"error": "device_boom",
                        "execution_id": task["execution_id"]}}], "receipts": []})
        self.assertEqual(self.app.store.get("task", r["task_id"])["status"], "unknown")
        dep = self.app.store.get("dependency", r["dependency_id"])
        self.assertEqual(dep["status"], "pending")
        self.assertIsNone(dep.get("outcome"))

    def test_unverified_receipt_keeps_dependency_pending(self):
        # Unverified execution retains its pending join until terminal proof.
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "unv-work")
        r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        self.app.task_step()
        task = self.app.store.get("task", r["task_id"])
        # A real v2 device echoes attempt_uuid; a uuid-less receipt for a v2 task is now
        # a fencing mismatch. Carry it so the receipt is fenced-IN and the
        # unconfirmed status still drives unknown (the property under test).
        forged = {"execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
                  "attempt_uuid": task.get("attempt_uuid"),
                  "status": "running", "process_tree_confirmed": False}
        self.app.broker.sync("local", {"instance_id": self.connector.instance_id,
            "reports": [], "receipts": [forged]})
        self.assertEqual(self.app.store.get("task", r["task_id"])["status"], "unknown")
        dep = self.app.store.get("dependency", r["dependency_id"])
        self.assertEqual(dep["status"], "pending")
        self.assertIsNone(dep.get("outcome"))

    def test_outbound_execute_and_duplicate_delivery(self):
        task = self.task()
        command = self.app.store.find("command", action="start")[0]
        payload = {k: command[k] for k in ("id", "action", "payload", "expires")}
        self.connector.execute(payload)
        self.connector.execute(payload)
        result = self.finish(task)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertTrue((self.root / "result.txt").exists())
        self.assertEqual(len(self.connector.store.find("local_run")), 1)
        self.assertEqual(len(self.app.store.find("artifact")), 1)

    def test_resolve_run_on_reported_run_does_not_rewrite_receipt(self):
        # once a run is reported (its receipt may be held by the cloud),
        # a late resolve_run must NOT rewrite the receipt — that would change the
        # payload and trip receipt_conflict on the next upload.
        task = self.task()
        result = self.finish(task)
        self.assertEqual(result["status"], "succeeded")
        run_before = self.connector.store.get("local_run", task["execution_id"])
        self.assertIn(run_before["status"], ("report", "acknowledged"))
        receipt_before = copy.deepcopy(run_before["receipt"])
        # A late operator resolve_run arrives for the same execution.
        self.connector.execute({"id": "cmd-late-resolve", "action": "resolve_run", "expires": None,
            "payload": {"execution_id": task["execution_id"], "resolution": "confirmed_stopped",
                        "operator_evidence": "late operator check after report"}})
        run_after = self.connector.store.get("local_run", task["execution_id"])
        self.assertEqual(run_after["receipt"], receipt_before, "receipt must be immutable after report")

    def test_acknowledged_unconfirmed_run_still_operator_resolvable(self):
        # a run whose receipt is UNCONFIRMED but was acknowledged by the cloud (the
        # #2d path — cloud ACKs an unconfirmed receipt without storing it) must STILL be
        # resolvable by the operator to a CONFIRMED terminal. The report/ack early-return
        # only applies to a genuinely confirmed (immutable) receipt.
        task = self.task()
        # Synthesize an acknowledged-but-unconfirmed local_run directly (no real spawn):
        # this is the #2d state where the cloud ACKed an unconfirmed receipt.
        full_task = self.app.store.get("task", [t["id"] for t in self.app.store.find("task")
                                               if t["execution_id"] == task["execution_id"]][0])
        self.connector.store.put("local_run", {"id": task["execution_id"], "task": full_task,
            "status": "acknowledged", "receipt": {"execution_id": task["execution_id"],
                "spec_digest": task["spec_digest"], "attempt_uuid": full_task.get("attempt_uuid"),
                "status": "running", "process_tree_confirmed": False}})
        self.connector.execute({"id": "cmd-op-resolve", "action": "resolve_run", "expires": None,
            "payload": {"execution_id": task["execution_id"], "resolution": "confirmed_stopped",
                        "operator_evidence": "operator confirms process stopped"}})
        after = self.connector.store.get("local_run", task["execution_id"])
        self.assertEqual(after["receipt"].get("process_tree_confirmed"), True, "operator must produce a confirmed terminal")
        self.assertIn(after["receipt"]["status"], ("cancelled", "succeeded", "failed"))

    def test_v2_task_held_until_device_proves_capability(self):
        # handshake: a v2-stamped task is NOT sent to a device that has not
        # proven it understands v2 (fail-closed). Once the device syncs its
        # supported_contract_version, the launch proceeds.
        # Erase the proven capability recorded during setUp's tick().
        dev = self.app.store.get("device", "local")
        dev["supported_contract_version"] = 0
        self.app.store.put("device", dev)
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "hs-work")
        r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "y"})
        self.app.task_step()
        # No start command enqueued yet — the launch is held in 'starting'.
        cmds = [c for c in self.app.store.find("command", action="start", limit=1000)
                if c.get("payload", {}).get("execution_id") == self.app.store.get("task", r["task_id"])["execution_id"]]
        self.assertEqual(cmds, [])
        self.assertEqual(self.app.store.get("task", r["task_id"])["status"], "starting")
        # a capability HOLD must NOT burn the start-attempt budget. Drive many
        # hold cycles; the task stays 'starting' with start_attempts unchanged (would
        # have flipped to unknown after START_ATTEMPT_BUDGET=5 under the old path).
        for _ in range(8):
            t = self.app.store.get("task", r["task_id"]); t["start_retry_not_before"] = 0
            self.app.store.put("task", t)
            self.app.task_step()
        held = self.app.store.get("task", r["task_id"])
        self.assertEqual(held["status"], "starting")
        self.assertEqual(int(held.get("start_attempts", 0)), 0, "capability hold must not consume start budget")
        # Device proves v3 by syncing; now the held task can launch.
        self.connector.tick()
        self.app.store.get("task", r["task_id"]).update(start_retry_not_before=0)
        t = self.app.store.get("task", r["task_id"]); t["start_retry_not_before"] = 0
        self.app.store.put("task", t)
        self.app.task_step()
        cmds = [c for c in self.app.store.find("command", action="start", limit=1000)
                if c.get("payload", {}).get("execution_id") == t["execution_id"]]
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["payload"]["contract"]["contract_version"], 3)

    def test_start_command_carries_launch_contract(self):
        # the cloud sends an explicit contract + digest with the frozen
        # cwd/timeout_s/access_mode/attempt_uuid/workspace_key, not just the old 9 fields.
        self.task()
        payload = self.app.store.find("command", action="start")[0]["payload"]
        c = payload["contract"]
        self.assertEqual(c["contract_version"], 3)
        for k in ("cwd", "timeout_s", "access_mode", "attempt_uuid", "workspace_key", "supervision_floor"):
            self.assertIn(k, c)
        from pangpang.store import contract_digest
        task = self.app.store.get("task", self.app.store.find("task")[0]["id"])
        self.assertEqual(payload["contract_digest"], contract_digest(task))

    def test_device_rejects_tampered_contract_digest(self):
        # A start whose contract fields were altered in transit (digest no longer
        # matches) is refused with a stable error — the device never falls back to its
        # own installation defaults.
        self.task()
        command = self.app.store.find("command", action="start")[0]
        payload = copy.deepcopy({k: command[k] for k in ("id", "action", "payload", "expires")})
        payload["payload"]["contract"]["timeout_s"] = 999999 # tamper, digest stays old
        self.connector.execute(payload)
        run = self.connector.store.find("local_run")[0]
        self.assertEqual(run["status"], "report")
        self.assertEqual(run["receipt"]["error"], "local_contract_digest_mismatch")

    def test_start_command_carries_min_contract_version_marker(self):
        # the start command carries a top-level min_contract_version sibling to
        # `contract`, so stripping the contract alone cannot silently downgrade.
        base = self.task()
        payload = [c for c in self.app.store.find("command", action="start", limit=1000)
                   if c.get("payload", {}).get("execution_id") == base["execution_id"]][0]["payload"]
        self.assertEqual(payload["min_contract_version"], 3)

    def test_device_rejects_stripped_contract_downgrade(self):
        # delete `contract`/`contract_digest` but keep the marker -> rejected
        # (not fall through to legacy), even with an altered top-level prompt.
        base = self.task()
        base_cmd = [c for c in self.app.store.find("command", action="start", limit=1000)
                    if c.get("payload", {}).get("execution_id") == base["execution_id"]][0]["payload"]
        p = copy.deepcopy(base_cmd)
        del p["contract"]; del p["contract_digest"]
        p["prompt"] = "EVIL-DOWNGRADE"
        result = self.connector.start(p)
        self.assertEqual(result.get("rejected"), True)
        run = self.connector.store.get("local_run", p["execution_id"])
        self.assertEqual(run["receipt"]["error"], "local_contract_required")

    def test_device_rejects_full_downgrade_and_never_starts(self):
        # full downgrade: delete contract + digest + marker ALL, alter prompt. A
        # v2 device (its own capability) still refuses and execution.start is NEVER
        # called — the RCB boundary holds BEFORE any side effect.
        base = self.task()
        base_cmd = [c for c in self.app.store.find("command", action="start", limit=1000)
                    if c.get("payload", {}).get("execution_id") == base["execution_id"]][0]["payload"]
        p = copy.deepcopy(base_cmd)
        del p["contract"]; del p["contract_digest"]; p.pop("min_contract_version", None)
        p["prompt"] = "EVIL-FULL-DOWNGRADE"
        started = []
        orig_start = self.connector.execution.start
        self.connector.execution.start = lambda task: started.append(task["execution_id"]) or orig_start(task)
        try:
            result = self.connector.start(p)
        finally:
            self.connector.execution.start = orig_start
        self.assertEqual(result.get("rejected"), True)
        self.assertEqual(started, [], "execution.start must NOT be called for a full downgrade")
        run = self.connector.store.get("local_run", p["execution_id"])
        self.assertEqual(run["receipt"]["error"], "local_contract_required")

    def test_device_rejects_any_tampered_contract_field(self):
        # the whole contract is signed, so ANY field (not just the six the
        # device adopts) is tamper-detected. Drive connector.start directly with a fresh
        # execution_id per field so admission/workspace coupling does not interfere.
        from pangpang.store import digest
        base = self.task()
        base_cmd = [c for c in self.app.store.find("command", action="start", limit=1000)
                    if c.get("payload", {}).get("execution_id") == base["execution_id"]][0]["payload"]
        for i, (field, value, expect) in enumerate((
                ("contract_version", 999, "local_contract_version_unsupported"),
                ("prompt", "EVIL", "local_contract_digest_mismatch"),
                ("policy_digest", "forged", "local_contract_digest_mismatch"),
                ("outputs", ["/x"], "local_contract_digest_mismatch"))):
            p = copy.deepcopy(base_cmd)
            p["execution_id"] = "exec-tamper-" + str(i) + "-" + "a" * 20
            p["contract"]["execution_id"] = p["execution_id"]
            p["contract_digest"] = digest(p["contract"]) # re-sign the untampered contract
            p["contract"][field] = value # THEN tamper: digest now stale
            result = self.connector.start(p)
            self.assertEqual(result.get("rejected"), True, field)
            run = self.connector.store.get("local_run", p["execution_id"])
            self.assertEqual(run["receipt"]["error"], expect, field)

    def test_device_rejects_split_payload_top_level_altered(self):
        # A valid signed contract with an ALTERED top-level prompt (the device also reads
        # prompt from the top level) must not execute with the unsigned value.
        from pangpang.store import digest
        base = self.task()
        base_cmd = [c for c in self.app.store.find("command", action="start", limit=1000)
                    if c.get("payload", {}).get("execution_id") == base["execution_id"]][0]["payload"]
        p = copy.deepcopy(base_cmd)
        p["execution_id"] = "exec-split-" + "a" * 24
        p["contract"]["execution_id"] = p["execution_id"]
        p["contract_digest"] = digest(p["contract"])
        p["prompt"] = "EVIL-TOP-LEVEL" # top-level altered; contract.prompt still original
        result = self.connector.start(p)
        self.assertEqual(result.get("rejected"), True)
        run = self.connector.store.get("local_run", p["execution_id"])
        self.assertEqual(run["receipt"]["error"], "local_contract_payload_mismatch")

    def test_device_rejects_workspace_identity_mismatch(self):
        # The cloud's workspace_key must equal what the device computes for the launch
        # cwd; a lie about the workspace identity is refused even if the digest matches.
        from pangpang.store import digest
        base = self.task()
        base_cmd = [c for c in self.app.store.find("command", action="start", limit=1000)
                    if c.get("payload", {}).get("execution_id") == base["execution_id"]][0]["payload"]
        p = copy.deepcopy(base_cmd)
        c = p["contract"]
        c["workspace_key"] = [c["workspace_key"][0], ["some", "other", "dir"]]
        p["contract_digest"] = digest(c) # re-sign so the digest guard passes
        result = self.connector.start(p)
        self.assertEqual(result.get("rejected"), True)
        run = self.connector.store.get("local_run", p["execution_id"])
        self.assertEqual(run["receipt"]["error"], "local_workspace_identity_mismatch")

    def test_required_outputs_bound_cloud_to_device_full_chain(self):
        # a connector task's required_outputs must survive the WHOLE chain:
        # cloud task -> broker payload (signed v4 contract + top-level mirror) ->
        # verify_launch_contract -> device-adopted local task -> runner spec. Without
        # this the device never knows to verify them and the cloud fails every
        # connector hard-deliverable task closed.
        from pangpang.store import digest, verify_launch_contract
        m = self.app.send("bot-main", {"text": "出报告", "allow_write": True}, "ro-chain")
        r = self.app.submit_task("bot-main", m["work_id"],
                                 {"prompt": "x", "required_outputs": ["out/report.docx"]})
        task = self.app.store.get("task", r["task_id"])
        self.assertEqual(task["required_outputs"], ["out/report.docx"])
        self.assertEqual(task["contract_version"], 4) # bumped only because of the hard contract
        self.app.task_step()
        payload = [c for c in self.app.store.find("command", action="start", limit=1000)
                   if c.get("payload", {}).get("execution_id") == task["execution_id"]][0]["payload"]
        self.assertEqual(payload["contract"]["contract_version"], 4)
        self.assertEqual(payload["contract"]["required_outputs"], ["out/report.docx"]) # SIGNED
        self.assertEqual(payload["required_outputs"], ["out/report.docx"]) # top-level mirror
        self.assertEqual(payload["contract_digest"], digest(payload["contract"])) # self-consistently signed
        self.assertIsNone(verify_launch_contract(payload)) # device gate accepts the honest contract
        captured = {}
        orig = self.connector.execution.start
        self.connector.execution.start = lambda t: (captured.update(task=t) or {"supervisor": "mock"})
        try:
            result = self.connector.start(payload)
        finally:
            self.connector.execution.start = orig
        self.assertEqual(result.get("accepted"), True)
        adopted = captured["task"] # the task handed to the device runner
        self.assertEqual(adopted["required_outputs"], ["out/report.docx"])
        spec = self.connector.execution.spec(adopted) # what the runner actually receives
        self.assertEqual(spec["required_outputs"], ["out/report.docx"])

    def test_device_rejects_tampered_required_outputs(self):
        # tamper: deleting / altering required_outputs (in the signed contract
        # or its top-level mirror) must fail-closed as a contract mismatch — a device can
        # never be driven to run a hard-deliverable task with a stripped/forged contract.
        from pangpang.store import digest, verify_launch_contract
        m = self.app.send("bot-main", {"text": "x", "allow_write": True}, "ro-tamper")
        r = self.app.submit_task("bot-main", m["work_id"],
                                 {"prompt": "x", "required_outputs": ["out/a.docx"]})
        task = self.app.store.get("task", r["task_id"])
        self.app.task_step()
        base_cmd = [c for c in self.app.store.find("command", action="start", limit=1000)
                    if c.get("payload", {}).get("execution_id") == task["execution_id"]][0]["payload"]

        # (a) DELETE required_outputs from the signed v4 contract, re-sign the stripped
        # contract -> exact-key schema catches the dropped field (same class).
        p = copy.deepcopy(base_cmd)
        p["execution_id"] = p["contract"]["execution_id"] = "exec-" + "a" * 32
        del p["contract"]["required_outputs"]
        p["contract_digest"] = digest(p["contract"])
        self.assertEqual(verify_launch_contract(p), "local_contract_schema_invalid")

        # (b) ALTER required_outputs INSIDE the contract after signing -> digest mismatch.
        p = copy.deepcopy(base_cmd)
        p["execution_id"] = p["contract"]["execution_id"] = "exec-" + "b" * 32
        p["contract_digest"] = digest(p["contract"])
        p["contract"]["required_outputs"] = ["EVIL.docx"]
        self.assertEqual(verify_launch_contract(p), "local_contract_digest_mismatch")

        # (c) ALTER only the top-level mirror (contract honestly signed) -> payload bind
        # mismatch; and the full device gate refuses to launch.
        p = copy.deepcopy(base_cmd)
        p["execution_id"] = p["contract"]["execution_id"] = "exec-" + "c" * 32
        p["contract_digest"] = digest(p["contract"])
        p["required_outputs"] = ["EVIL.docx"]
        self.assertEqual(verify_launch_contract(p), "local_contract_payload_mismatch")
        result = self.connector.start(p)
        self.assertEqual(result.get("rejected"), True)
        run = self.connector.store.get("local_run", p["execution_id"])
        self.assertEqual(run["receipt"]["error"], "local_contract_payload_mismatch")

    def test_connector_text_answer_reaches_dependency_result_text(self):
        # a connector task whose deliverable is TEXT (the executor's stdout,
        # e.g. a research answer) must land in the dependency's untrusted result_text.
        # Before the fix the device never attached a tail for connector transport, so
        # result_text was always None even on success.
        m = self.app.send("bot-main", {"text": "查一下", "allow_write": True}, "text-work")
        r = self.app.submit_task("bot-main", m["work_id"], {"prompt": "RESEARCH_ANSWER_42"})
        self.app.task_step()
        task = self.app.store.get("task", r["task_id"])
        result = self.finish(task)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertIn("tail", result["receipt"])
        self.assertIn("Executed RESEARCH_ANSWER_42", result["receipt"]["tail"])
        # The tail flows to the dependency as bounded, untrusted result_text.
        dep = self.app.store.get("dependency", r["dependency_id"])
        payload = dep.get("result_payload") or {}
        self.assertIn("Executed RESEARCH_ANSWER_42", payload.get("result_text", ""))

    def test_cloud_cancel_waits_for_device_confirmation(self):
        self.connector.config["installations"][0]["argv"] = [sys.executable, "-c", "import time; time.sleep(60)"]
        probe = self.app.store.get("probe", "codex-local")
        probe["policy_digest"] = policy_digest(self.connector.config, "codex-local")
        self.app.store.put("probe", probe)
        task = self.task()
        self.connector.tick()
        self.app.cancel(task["id"])
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "cancel_requested")
        result = self.finish(task)
        self.assertEqual(result["status"], "cancelled", result)
        self.assertTrue(result["receipt"]["process_tree_confirmed"])

    def test_probe_and_file_mailbox(self):
        from concurrent.futures import ThreadPoolExecutor
        def await_operation(pool, action):
            future = pool.submit(action)
            deadline = time.monotonic() + 25
            while not future.done() and time.monotonic() < deadline:
                self.connector.tick()
                time.sleep(.02)
            return future.result(timeout=2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            probe = await_operation(pool, lambda: self.app.probe("codex-local"))
            self.assertTrue(probe["available"], probe)
            source = self.root / "source.txt"
            source.write_text("material", encoding="utf-8")
            artifact = await_operation(pool, lambda: self.app.import_file("bot-main", {"host_id": "local", "path": str(source)}))
            self.assertEqual(artifact["size"], 8)
            with self.assertRaises(Fault):
                await_operation(pool, lambda: self.app.broker.request("local", "file",
                    {"action": "write", "path": str(self.root / "copy.txt"), "data": "bWF0ZXJpYWw="}))

    def test_browse_and_upload_through_the_device(self):
        from concurrent.futures import ThreadPoolExecutor
        import base64
        def await_operation(pool, action):
            future = pool.submit(action)
            deadline = time.monotonic() + 25
            while not future.done() and time.monotonic() < deadline:
                self.connector.tick()
                time.sleep(.02)
            return future.result(timeout=2)
        (self.root / "notes.txt").write_text("hi", encoding="utf-8")
        body = {"host_id": "local", "name": "a.txt", "data": base64.b64encode(b"upload").decode()}
        with ThreadPoolExecutor(max_workers=1) as pool:
            # The device picks the place: the cloud does not know a connector's roots.
            listing = await_operation(pool, lambda: self.app.browse("local", {"path": str(self.root)}))
            self.assertIn("notes.txt", [e["name"] for e in listing["entries"]])
            with self.assertRaises(Fault) as cm: # receiving files stays a device-side opt-in
                await_operation(pool, lambda: self.app.upload_file("bot-main", body))
            self.assertEqual(cm.exception.code, "local_file_receive_disabled")
            self.connector.config["allow_file_receive"] = True
            art = await_operation(pool, lambda: self.app.upload_file("bot-main", body))
        self.assertEqual(Path(art["path"]).parent.parent, self.root.resolve() / ".pangpang-inbox")
        self.assertEqual(Path(art["path"]).read_bytes(), b"upload")

    def test_disconnect_restart_receipt_recovery(self):
        task = self.task()
        self.connector.tick() # persist launch before simulated network outage
        self.app.recover()
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "running")
        self.connector.close()
        self.connector = Connector(self.connector.config, self.root.parent / "device")
        self.connector.request = lambda body: self.app.broker.sync("local", body)
        result = self.finish(task)
        self.assertEqual(result["status"], "succeeded", result)
        count = len(self.app.store.find("message"))
        self.connector.tick()
        self.assertEqual(count, len(self.app.store.find("message")))

    def test_policy_changes_cannot_launch(self):
        task = self.task()
        self.connector.config["installations"][0]["argv"] = [sys.executable, "-c", "raise Exception('must not run')"]
        result = self.finish(task)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["receipt"]["error"], "local_policy_changed_probe_and_resubmit")
        self.assertFalse((self.root / "result.txt").exists())

    def test_cloud_argv_does_not_replace_local_command(self):
        self.app.execution.installations["codex-local"]["argv"] = [sys.executable, "-c", "raise Exception('cloud argv')"]
        task = self.task()
        result = self.finish(task)
        self.assertEqual(result["status"], "succeeded", result)

    def test_crash_during_command_never_replays_spawn(self):
        task = self.task()
        cmd = self.app.store.find("command", action="start")[0]
        cmd = {k: cmd[k] for k in ("id", "action", "payload", "expires")}
        self.connector.store.put("local_command", {"id": cmd["id"], "fingerprint": digest(cmd), "status": "executing"})
        self.connector.execute(cmd)
        self.assertFalse(self.connector.store.find("local_run"))
        self.assertEqual(self.connector.store.get("local_command", cmd["id"])["result"]["error"], "local_operation_unconfirmed")
        self.app.recover()
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "running")
        self.connector.tick() # only the device's unconfirmed action establishes unknown
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "unknown")

    def test_second_data_directory_cannot_take_over_device(self):
        second = Connector(self.connector.config, self.root / "second-device")
        second.request = lambda body: self.app.broker.sync("local", body)
        try:
            with self.assertRaises(Fault) as caught:
                second.tick()
            self.assertEqual(caught.exception.code, "device_instance_mismatch")
        finally:
            second.close()

    def test_device_credentials_are_scoped_and_revocable(self):
        with patch.dict(os.environ, {"TEST_DEVICE_KEY": "d" * 40}):
            self.assertEqual(self.app.broker.authenticate("local", "Bearer " + "d" * 40), "local")
            with self.assertRaises(Fault):
                self.app.broker.authenticate("other", "Bearer " + "d" * 40)
            self.app.broker.revoke("local")
            with self.assertRaises(Fault):
                self.app.broker.authenticate("local", "Bearer " + "d" * 40)

    def test_model_outage_does_not_block_receipt_collection(self):
        message = self.app.send("bot-main", {"text": "对话"}, "outage")
        Runtime(self.app).step()
        self.assertEqual(self.app.store.get("turn", message["turn_id"])["status"], "blocked")
        task = self.task()
        self.assertEqual(self.finish(task)["status"], "succeeded")
        self.assertEqual(self.app.store.get("turn", message["turn_id"])["status"], "blocked")

    def test_single_global_endpoint_and_no_per_bot_override(self):
        cfg = copy.deepcopy(self.app.config)
        cfg["models"].append(dict(cfg["models"][0], id="fallback"))
        with self.assertRaises(Fault):
            global_profile(cfg)
        with self.assertRaises(Fault):
            self.app.create_bot({"name": "独立模型", "role": "x", "model_profile_id": "fallback"})
        bot = self.app.create_bot({"name": "编辑", "role": "编辑"})
        self.assertEqual(bot["model_profile_id"], self.app.bot("bot-main")["model_profile_id"])

    def test_http_device_cannot_access_admin_or_other_host_reports(self):
        server = serve(self.app, "a" * 40, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.connector.endpoint = "http://127.0.0.1:" + str(server.server_port) + "/api/v1/connector/sync"
        try:
            with patch.dict(os.environ, {"TEST_DEVICE_KEY": "d" * 40}):
                result = Connector.request(self.connector, {"reports": [], "receipts": [], "version": "test", "instance_id": self.connector.instance_id})
                self.assertIn("commands", result)
                request = urllib.request.Request(self.connector.endpoint.replace("connector/sync", "snapshot"),
                    headers={"Authorization": "Bearer " + "d" * 40})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 401)
                task = self.task()
                forged = {"execution_id": task["execution_id"], "spec_digest": "wrong",
                          "status": "succeeded", "process_tree_confirmed": True}
                with self.assertRaises(Fault):
                    self.app.broker.sync("local", {"receipts": [forged], "instance_id": self.connector.instance_id})
                self.assertNotEqual(self.app.store.get("task", task["id"])["status"], "succeeded")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
