"""Positive regressions for the 2026-09-25 architecture review."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_bot_runtime import config
from pangpang import runner
from pangpang.connector import Connector
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.app = Service(config(self.workspace), self.root / "control")
        # Probe the real host so a launched attempt's frozen supervision floor is one
        # this OS can meet (Job Object, cgroup scope or the L0 process-group tier).
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def message(self, label, read_only=False, **body):
        sent = self.app.send("bot-main", {"text": label, **body}, label)
        if read_only: # a request without write authority (e.g. a restricted handoff)
            work = self.app.store.get("work", sent["work_id"])
            work["grant_write"] = False
            self.app.store.put("work", work)
            turn = self.app.store.get("turn", sent["turn_id"])
            turn["authorization"] = dict(turn.get("authorization") or {}, grant_write=False)
            self.app.store.put("turn", turn)
        return sent

    def turn(self, work_id):
        return self.app.store.find("turn", work_id=work_id, newest=True)[0]

    def end_turn(self, work_id):
        turn = self.turn(work_id)
        turn["status"] = "done"
        self.app.store.put("turn", turn)

    def task(self, work_id):
        result = self.app.submit_task("bot-main", work_id, {"prompt": "isolated probe"})
        return self.app.store.get("task", result["task_id"])

    def delegate(self, bot_id, work_id, target, mode):
        return self.app.delegate(bot_id, work_id, {"target_bot_id": target, "goal": "child", "request": "answer",
                                 "acceptance": ["answer delivered"], "mode": mode})

    def test_start_replay_spawns_once_even_after_ack_loss(self):
        directory = self.root / "run"
        directory.mkdir()
        with patch.object(sys, "argv", ["runner.py", "start", str(directory)]), \
             patch.object(runner.subprocess, "Popen", return_value=Mock(pid=101)) as spawn, \
             contextlib.redirect_stdout(io.StringIO()):
            runner.main()
            runner.main()
            (directory / "start.json").unlink()
            runner.main()
        self.assertEqual(spawn.call_count, 1)

    def test_claim_without_spawn_never_retries_side_effect(self):
        directory = self.root / "run"
        directory.mkdir()
        with patch.object(sys, "argv", ["runner.py", "start", str(directory)]), \
             patch.object(runner.subprocess, "Popen", side_effect=OSError("lost")) as spawn, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                runner.main()
            runner.main()
        self.assertEqual(spawn.call_count, 1)

    def test_concurrent_real_starts_execute_side_effect_once(self):
        task = self.task(self.message("concurrent start")["work_id"])
        self.app.execution.installations["codex-local"]["argv"] = [sys.executable, "-c",
            "from pathlib import Path; p=Path('count.txt'); p.open('a').write('one\\n')"]
        task["attempt_uuid"] = "concurrent-attempt"
        directory = Path(self.app.execution.directory(self.app.execution.hosts["local"], task["execution_id"]))
        self.app.execution.prepare(self.app.execution.hosts["local"], str(directory), self.app.execution.spec(task))
        commands = [subprocess.Popen([sys.executable, runner.__file__, "start", str(directory)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for proc in commands:
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIsInstance(json.loads(out), dict)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (directory / "receipt.json").exists():
            time.sleep(.05)
        receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "succeeded", receipt)
        # A race that wins resolve before the CLI exits also reports succeeded.
        # On a host whose detector actually has a cgroup scope, the receipt must
        # name that scope and the populated-zero proof. Elsewhere the detector
        # never selects this tier, so those fields are not this host's contract.
        if runner._detect_supervision() == "linux-cgroup-cooperative":
            self.assertEqual(receipt.get("supervision"), "linux-cgroup-cooperative", receipt)
            self.assertEqual(receipt.get("termination_evidence"), "cgroup-v2-populated-zero", receipt)
        self.assertEqual((self.workspace / "count.txt").read_text(), "one\n")

    def test_unresolved_cgroup_scope_never_starts_the_cli(self):
        # resolve() fails only after a released CLI would have written the
        # canary, so moving the job-assigned touch before resolve and launching
        # the CLI directly both fail the canary assertion. The stand-in
        # systemd-run execs whatever payload the runner passed; this host may
        # have no user bus.
        if not sys.platform.startswith("linux"):
            self.skipTest("linux cgroup ready-gate")
        canary = self.root / "unresolved-cgroup-canary.txt"
        task = self.task(self.message("unresolved cgroup")["work_id"])
        self.app.execution.installations["codex-local"]["argv"] = [sys.executable, "-c",
            "import pathlib; pathlib.Path(%r).write_text('ran')" % str(canary)]
        task["attempt_uuid"] = "unresolved-cgroup"
        host = self.app.execution.hosts["local"]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        spec = self.app.execution.spec(task)
        spec["supervision_floor"] = "linux-cgroup-cooperative"
        self.app.execution.prepare(host, str(directory), spec)
        bindir = self.root / "bin"
        bindir.mkdir()
        fake = bindir / "systemd-run"
        fake.write_text("#!" + sys.executable + "\n"
                        "import os, sys\n"
                        "args = sys.argv[1:]\n"
                        "while args and args[0].startswith('-'):\n"
                        " args = args[1:]\n"
                        "os.execvp(args[0], args)\n", encoding="utf-8")
        fake.chmod(0o755)
        pangpang_dir = str(Path(runner.__file__).resolve().parent)
        if pangpang_dir not in sys.path:
            sys.path.insert(0, pangpang_dir)
        import linux_job
        procs = []
        real_popen = subprocess.Popen
        def spy(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            procs.append(proc)
            return proc
        def resolve_unresolved(self, timeout=5.0):
            # An immediate False kills the payload before it can write. Hold the
            # failure until the canary appears or 2s pass: a released CLI (early
            # job-assigned touch, or no trampoline) writes it within ~0.2s even
            # on a loaded host, so the canary assertion catches both on its own.
            # The gated child never writes it, so the real code waits the full 2s.
            deadline = time.monotonic() + 2.0
            while not canary.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            return False
        try:
            with patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}), \
                 patch.object(linux_job.LinuxJob, "resolve", resolve_unresolved), \
                 patch.object(runner, "_detect_supervision", return_value="linux-cgroup-cooperative"), \
                 patch.object(runner.subprocess, "Popen", spy):
                runner.supervise(directory)
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
        self.assertFalse(canary.exists(), "CLI must not run when the cgroup scope is unresolved")
        self.assertFalse((directory / "job-assigned").exists())
        receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "unknown", receipt)
        self.assertIn("cgroup_scope_unresolved", receipt.get("error", ""))
        self.assertNotIn("not_launched", receipt)

    def test_child_runs_the_cli_in_the_supervisors_checked_cwd(self):
        # supervise() starts the child with cwd=checked(spec["cwd"]). The raw
        # spec value is never expanded in the child: a "~" path would make the
        # CLI fail to start while the receipt reports a run that exited 1.
        run = self.root / "child-run"
        run.mkdir()
        (run / "spec.json").write_text(json.dumps({
            "prompt": "p", "cwd": "~/xpp-unexpanded-" + os.urandom(4).hex(),
            "argv": [sys.executable, "-c",
                     "import os, pathlib; pathlib.Path('canary.txt').write_text(os.getcwd())"]}),
            encoding="utf-8")
        (run / "job-assigned").touch()
        result = subprocess.run([sys.executable, runner.__file__, "child", str(run)],
                                cwd=self.workspace, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.samefile((self.workspace / "canary.txt").read_text(), self.workspace))

    def test_legacy_start_marker_prevents_upgrade_relaunch(self):
        directory = self.root / "run"
        directory.mkdir()
        (directory / "started.json").write_text("{}")
        with patch.object(sys, "argv", ["runner.py", "start", str(directory)]), \
             patch.object(runner.subprocess, "Popen") as spawn, contextlib.redirect_stdout(io.StringIO()):
            runner.main()
        spawn.assert_not_called()

    def test_ambiguous_remote_start_retains_claim_and_pending_join(self):
        work = self.message("lost ACK")
        task = self.task(work["work_id"])
        task.update(status="starting", started=time.time(), attempt_uuid="attempt")
        self.app.store.put("task", task)
        # Remote stand-in is Connector. SSH launches are refused before this
        # lost-ACK path; Connector still has no local process view. The attempt
        # was already admitted, so the device digest matches the frozen one.
        probe = self.app.store.get("probe", task["installation_id"])
        probe["authorization_digest"] = task["authorization_digest"]
        self.app.store.put("probe", probe)
        self.app.execution.hosts["local"]["transport"] = "connector"
        self.app.execution.hosts["local"]["os"] = "linux"
        with patch.object(self.app.execution, "start", side_effect=Fault("transport_unavailable")), \
             patch.object(self.app.execution, "poll", return_value={"status": "unknown"}) as poll:
            for _ in range(self.app.START_ATTEMPT_BUDGET):
                self.app._try_start(self.app.store.get("task", task["id"]))
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "unknown")
        self.assertGreater(poll.call_count, 0)
        self.assertFalse(self.app._task_admissible(dict(task, id="other", status="queued")))
        self.assertEqual(self.app.store.get("dependency", task["dependency_id"])["status"], "pending")

    def test_readonly_link_and_delegation_cannot_upgrade(self):
        old = self.message("writable")
        self.end_turn(old["work_id"])
        new = self.message("readonly", read_only=True, host_id="local")
        self.app.link_work(self.turn(new["work_id"])["id"], old["work_id"])
        self.assertFalse(self.app.store.get("work", old["work_id"])["grant_write"])
        task = self.task(old["work_id"])
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertFalse(task["authorization"]["grant_write"])
        child_bot = self.app.create_bot({"name": "child", "role": "test"})
        child = self.delegate("bot-main", old["work_id"], child_bot["id"], "return_to_sender")
        child_work = self.app.store.get("work", child["work_id"])
        self.assertFalse(child_work["grant_write"])
        self.assertEqual(child_work["required_host_id"], "local")

    def test_request_authority_survives_work_mutation(self):
        msg = self.message("readonly", read_only=True)
        turn = self.turn(msg["work_id"])
        work = self.app.store.get("work", msg["work_id"])
        work["grant_write"] = True
        self.app.store.put("work", work)
        result = Runtime(self.app, model=Mock()).invoke(turn, "submit_task", {"prompt": "write"})
        self.assertEqual(result["status"], "awaiting_approval")

    def test_nested_delivery_settles_ancestor(self):
        middle = self.app.create_bot({"name": "middle", "role": "test"})
        leaf = self.app.create_bot({"name": "leaf", "role": "test"})
        root = self.message("three levels")
        child = self.delegate("bot-main", root["work_id"], middle["id"], "return_to_sender")
        self.end_turn(root["work_id"])
        grandchild = self.delegate(middle["id"], child["work_id"], leaf["id"], "deliver_to_user")
        self.end_turn(child["work_id"])
        self.app.finish_turn(self.turn(grandchild["work_id"]), "answer")
        self.assertEqual(self.app.store.get("dependency", child["dependency_id"])["outcome"], "success")
        self.assertNotEqual(self.app.store.get("work", root["work_id"])["status"], "waiting")

    def test_last_success_does_not_mask_unread_failure(self):
        a = self.app.create_bot({"name": "a", "role": "test"})
        b = self.app.create_bot({"name": "b", "role": "test"})
        root = self.message("two deliverables")
        first = self.delegate("bot-main", root["work_id"], a["id"], "return_to_sender")
        last = self.delegate("bot-main", root["work_id"], b["id"], "deliver_to_user")
        self.end_turn(root["work_id"])
        self.app.settle_dependency(first["dependency_id"], "failure", summary="first missing")
        self.app.finish_turn(self.turn(last["work_id"]), "second only")
        self.assertNotEqual(self.app.store.get("work", root["work_id"])["status"], "delivered")
        continuation = self.turn(root["work_id"])
        messages = [self.app.store.get("message", mid) for mid in continuation["message_ids"]]
        self.assertTrue(any("first missing" in m["content"] for m in messages))

    def test_finish_replay_does_not_duplicate_delivery(self):
        work_id = self.message("finish once")["work_id"]
        turn = self.turn(work_id)
        self.app.finish_turn(turn, "answer")
        self.app.finish_turn(turn, "answer")
        messages = [m for m in self.app.store.find("message", work_id=work_id) if m["kind"] == "assistant"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["turn_id"], turn["id"])

    def test_fast_accept_cannot_mask_an_unread_failure(self):
        work_id = self.message("mixed tasks")["work_id"]
        first, last = self.task(work_id), self.task(work_id)
        self.end_turn(work_id)
        self.app.settle_dependency(first["dependency_id"], "failed", summary="missing")
        self.app.settle_dependency(last["dependency_id"], "success", summary="answer", fast_accept={"fast_accept": True})
        self.assertNotEqual(self.app.store.get("work", work_id)["status"], "delivered")

    def test_complete_task_itself_requires_terminal_proof(self):
        task = self.task(self.message("forged receipt")["work_id"])
        with self.assertRaises(Fault):
            self.app.complete_task(task, {"status": "succeeded", "process_tree_confirmed": True,
                                         "execution_id": task["execution_id"], "spec_digest": "forged"})
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "queued")

    def test_prepare_refuses_changed_spec_for_same_execution(self):
        task = self.task(self.message("fenced spec")["work_id"])
        host = self.app.execution.hosts["local"]
        directory = self.app.execution.directory(host, task["execution_id"])
        spec = self.app.execution.spec(task)
        self.app.execution.prepare(host, directory, spec)
        with self.assertRaises(Fault):
            self.app.execution.prepare(host, directory, dict(spec, prompt="different side effect"))

    def test_unknown_automatically_reads_matching_late_receipt(self):
        root = self.message("late receipt")
        task = self.task(root["work_id"])
        task.update(status="running", started=time.time(), attempt_uuid="attempt")
        self.app.store.put("task", task)
        self.app._mark_task_unknown(task, "receipt_missing")
        current = self.app.store.get("task", task["id"])
        current["observation_retry_not_before"] = 0
        self.app.store.put("task", current)
        directory = Path(self.app.execution.directory(self.app.execution.hosts["local"], task["execution_id"]))
        directory.mkdir(parents=True)
        receipt = {"status": "succeeded", "process_tree_confirmed": True, "supervision": "windows-job-object",
                   "execution_id": task["execution_id"],
                   "spec_digest": task["spec_digest"], "attempt_uuid": "attempt", "artifacts": []}
        (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        self.app.task_step()
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "succeeded")
        self.assertEqual(self.app.store.get("dependency", task["dependency_id"])["outcome"], "success")

    def test_connector_restart_does_not_invent_terminal_outcome(self):
        task = self.task(self.message("restart")["work_id"])
        task.update(status="running", started=time.time())
        self.app.store.put("task", task)
        self.app.execution.hosts["local"]["transport"] = "connector"
        self.app._recover_task(task["id"])
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "running")
        self.assertEqual(self.app.store.get("dependency", task["dependency_id"])["status"], "pending")

    def test_evidence_checks_real_bytes_and_detects_deleted_file(self):
        root = self.message("verify")
        path = self.workspace / "document.txt"
        path.write_text("original", encoding="utf-8")
        artifact = {"id": "artifact-probe", "owner_bot_id": "bot-main", "work_id": root["work_id"],
                    "host_id": "local", "path": str(path), "digest": runner.file_hash(path), "size": path.stat().st_size}
        self.app.store.put("artifact", artifact)
        runtime = Runtime(self.app, model=Mock())
        result = runtime.invoke(self.turn(root["work_id"]), "read_evidence", {"reference": artifact["id"]})
        self.assertEqual(result["content"], "original")
        self.assertEqual(result["verification_status"], "bytes_verified_not_business_acceptance")
        path.unlink()
        with self.assertRaises(Fault):
            runtime.invoke(self.turn(root["work_id"]), "read_evidence", {"reference": artifact["id"]})

    def test_archived_delivery_survives_source_change(self):
        path = self.workspace / "document.txt"
        path.write_text("original", encoding="utf-8")
        execution_id = "exec-" + "a" * 32
        directory = Path(self.app.execution.directory(self.app.execution.hosts["local"], execution_id))
        directory.mkdir(parents=True)
        artifact = {"path": str(path), "digest": runner.file_hash(path), "host_id": "local"}
        runner.archive_artifacts(directory, [artifact], [str(self.workspace)], execution_id)
        path.write_text("changed", encoding="utf-8")
        data = self.app.execution.read_artifact(artifact)
        self.assertEqual(data["digest"], artifact["digest"])

    def test_archived_connector_read_uses_device_command(self):
        self.app.execution.hosts["local"]["transport"] = "connector"
        artifact = {"host_id": "local", "snapshot_execution_id": "exec-" + "a" * 32, "digest": "b" * 64}
        with patch.object(self.app.broker, "request", return_value={"digest": artifact["digest"]}) as request:
            self.assertEqual(self.app.execution.read_artifact(artifact)["digest"], artifact["digest"])
        request.assert_called_once_with("local", "artifact_read", {"execution_id": artifact["snapshot_execution_id"],
                                                                  "digest": artifact["digest"]})

    def test_registration_rejects_control_overlap(self):
        for workspace in (self.app.data, self.root, self.app.data / "child"):
            with self.subTest(workspace=workspace), self.assertRaises(Fault):
                self.app.ensure_local_host(workspace, discover=False)

    def test_start_rechecks_legacy_unsafe_root(self):
        task = self.task(self.message("unsafe")["work_id"])
        self.app.execution.hosts["local"]["allowed_roots"] = [str(self.root)]
        with patch.object(self.app.execution, "call") as call, self.assertRaises(Fault):
            self.app.execution.start(task)
        call.assert_not_called()

    def test_connector_history_cannot_hide_active_runs(self):
        with self.app.store.atomic():
            for index in range(1001):
                self.app.store.put("local_run", {"id": "old-" + str(index), "status": "acknowledged"})
            self.app.store.put("local_run", {"id": "new-active", "status": "active", "task": {
                "execution_id": "exec-" + "a" * 32, "host_id": "local", "installation_id": "codex-local"}})
        connector = object.__new__(Connector)
        connector.store, connector.execution, connector.host_id = self.app.store, self.app.execution, "local"
        with patch.object(self.app.execution, "poll", return_value={"execution_id": "exec-" + "a" * 32,
                "status": "succeeded", "process_tree_confirmed": True, "supervision": "windows-job-object"}) as poll:
            connector.collect()
        # The active run is polled: once for the terminal receipt, once more for the
        # one-shot tail attach on the non-report->report transition. The point
        # of this test is that a large acknowledged backlog does NOT hide the active run.
        self.assertEqual(poll.call_count, 2)
        self.assertEqual(self.app.store.get("local_run", "new-active")["status"], "report")
