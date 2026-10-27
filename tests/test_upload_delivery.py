"""A chat upload on the coordinator can be read by an executor on another device."""
import base64
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_bot_runtime import config, call
from pangpang import runner
from pangpang.planner_prompt import PLANNER_SYSTEM
from pangpang.runtime import Runtime
from pangpang.service import Service, portable_upload
from pangpang.store import Fault, assert_no_txn_io


def b64(raw):
    return base64.b64encode(raw).decode()


class UploadDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.local_root = base / "local-ws"
        self.device_root = base / "device-ws"
        self.local_root.mkdir()
        self.device_root.mkdir()
        cfg = config(self.local_root)
        cfg["hosts"].append({
            "id": "device", "name": "lab-device", "transport": "connector", "os": "Linux",
            "allowed_roots": [str(self.device_root)], "execution_level": "L0",
            "credential_env": "TEST_DEVICE_KEY",
        })
        # Lower priority number is preferred, so auto-pick would choose this
        # device when the upload's own host has no eligible executor.
        cfg["installations"].append({
            "id": "worker-device", "host_id": "device", "adapter": "codex", "priority": 1,
            "argv": [sys.executable, "-c", "import sys; sys.stdin.read()"],
            "probe_argv": [sys.executable, "--version"],
            "cwd": str(self.device_root), "risk": "write",
        })
        self.app = Service(cfg, base / "data")
        self.app.store.put("device", {"id": "device"})
        self.app.broker._last_seen["device"] = time.time()
        self.app.store.put("probe", {
            "id": "worker-device", "available": True,
            "supervision": "linux-cgroup-cooperative", "at": time.time(),
        })
        self.receives = []
        self.app.broker.request = self._receive
        self.raw = b"notes-for-the-device"
        self.upload = self.app.upload_file("bot-main", {
            "host_id": "local", "name": "notes.txt", "data": b64(self.raw)})

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _receive(self, host_id, action, payload):
        assert_no_txn_io("broker.request")
        self.assertEqual((host_id, action, payload.get("action")), ("device", "file", "receive"))
        self.receives.append(payload)
        return runner.receive_file({
            "path": payload["path"], "data": payload["data"],
            "allowed_roots": [str(self.device_root)],
        })

    def _work(self, key):
        sent = self.app.send("bot-main", {"text": "use the file", "allow_write": True}, key)
        return sent

    def _submit(self, sent, **extra):
        body = {"prompt": "read the attached notes", "artifact_refs": [self.upload["id"]]}
        body.update(extra)
        return self.app.submit_task("bot-main", sent["work_id"], body)

    def test_named_remote_executor_receives_the_upload(self):
        sent = self._work("remote")
        result = self._submit(sent, installation_id="worker-device")
        task = self.app.store.get("task", result["task_id"])
        self.assertEqual(task["host_id"], "device")
        self.assertEqual(len(task["inputs"]), 1)
        placed_id = task["inputs"][0]["artifact_id"]
        self.assertNotEqual(placed_id, self.upload["id"])
        self.assertEqual(task["inputs"][0]["digest"], self.upload["digest"])
        path = Path(task["inputs"][0]["path"])
        self.assertEqual(path.parent.parent, self.device_root.resolve() / ".pangpang-inbox")
        self.assertEqual(path.read_bytes(), self.raw)
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), task["inputs"][0]["digest"])
        placed = self.app.store.get("artifact", placed_id)
        self.assertEqual(placed["host_id"], "device")
        self.assertEqual(placed["source"], self.upload["id"])
        self.assertFalse(portable_upload(placed))
        self.assertTrue(portable_upload(self.app.store.get("artifact", self.upload["id"])))
        self.assertEqual(self.app.store.get("artifact", self.upload["id"])["host_id"], "local")
        self.assertEqual(len(self.receives), 1)
        # A second submission reuses the placed bytes.
        again = self._submit(sent, installation_id="worker-device")
        other = self.app.store.get("task", again["task_id"])
        self.assertEqual(other["inputs"][0]["artifact_id"], placed_id)
        self.assertEqual(len(self.receives), 1)

    def test_auto_route_delivers_when_the_upload_host_has_no_executor(self):
        sent = self._work("auto")
        result = self._submit(sent)
        task = self.app.store.get("task", result["task_id"])
        self.assertEqual(task["installation_id"], "worker-device")
        self.assertEqual(Path(task["inputs"][0]["path"]).read_bytes(), self.raw)
        self.assertEqual(len(self.receives), 1)

    def test_auto_route_keeps_an_executor_that_already_has_the_upload(self):
        self.app.probe("codex-local")
        sent = self._work("stay")
        result = self._submit(sent)
        task = self.app.store.get("task", result["task_id"])
        self.assertEqual(task["installation_id"], "codex-local")
        self.assertEqual(task["inputs"][0]["path"], self.upload["path"])
        self.assertEqual(task["inputs"][0]["artifact_id"], self.upload["id"])
        self.assertEqual(self.receives, [])

    def test_refused_receive_is_an_actionable_error_and_creates_no_task(self):
        def refuse(host_id, action, payload):
            assert_no_txn_io("broker.request")
            raise Fault("local_file_receive_disabled")
        self.app.broker.request = refuse
        sent = self._work("refused")
        with self.assertRaises(Fault) as caught:
            self._submit(sent, installation_id="worker-device")
        self.assertEqual(caught.exception.code, "artifact_delivery_failed")
        self.assertIn("allow_file_receive", caught.exception.advice)
        self.assertIn("lab-device", caught.exception.advice)
        self.assertEqual(self.app.store.find("task"), [])

    def test_offline_device_explains_that_the_upload_cannot_be_delivered(self):
        self.app.broker._last_seen.pop("device", None)
        sent = self._work("offline")
        with self.assertRaises(Fault) as caught:
            self._submit(sent, installation_id="worker-device")
        self.assertEqual(caught.exception.code, "executor_host_offline")
        self.assertIn("offline", caught.exception.advice)
        self.assertIn("lab-device", caught.exception.advice)
        self.assertEqual(self.receives, [])
        self.assertEqual(self.app.store.find("task"), [])

    def test_workspace_reference_is_not_copied(self):
        source = self.local_root / "ref.txt"
        source.write_text("keep", encoding="utf-8")
        imported = self.app.import_file("bot-main", {"host_id": "local", "path": str(source)})
        self.assertEqual(imported["source"], "user_import")
        sent = self._work("import")
        with self.assertRaises(Fault) as caught:
            self.app.submit_task("bot-main", sent["work_id"], {
                "prompt": "read the reference", "installation_id": "worker-device",
                "artifact_refs": [imported["id"]],
            })
        self.assertEqual(caught.exception.code, "artifact_location_conflict")
        self.assertIn("workspace reference", caught.exception.advice)
        self.assertEqual(self.receives, [])
        self.assertEqual(self.app.store.find("task"), [])

    def test_a_workspace_reference_blocks_delivery_of_an_upload(self):
        source = self.local_root / "ref.txt"
        source.write_text("keep", encoding="utf-8")
        imported = self.app.import_file("bot-main", {"host_id": "local", "path": str(source)})
        sent = self._work("mixed")
        with self.assertRaises(Fault) as caught:
            self.app.submit_task("bot-main", sent["work_id"], {
                "prompt": "read both", "installation_id": "worker-device",
                "artifact_refs": [self.upload["id"], imported["id"]],
            })
        self.assertEqual(caught.exception.code, "artifact_location_conflict")
        self.assertEqual(self.receives, [])
        self.assertEqual(self.app.store.find("task"), [])

    def test_changed_upload_bytes_are_not_sent(self):
        Path(self.upload["path"]).write_bytes(b"tampered")
        sent = self._work("tamper")
        with self.assertRaises(Fault) as caught:
            self._submit(sent, installation_id="worker-device")
        self.assertEqual(caught.exception.code, "source_changed")
        self.assertEqual(self.receives, [])
        self.assertEqual(self.app.store.find("task"), [])

    def test_device_digest_must_match_the_upload(self):
        def lie(host_id, action, payload):
            assert_no_txn_io("broker.request")
            got = runner.receive_file({
                "path": payload["path"], "data": payload["data"],
                "allowed_roots": [str(self.device_root)],
            })
            return dict(got, digest="0" * 64)
        self.app.broker.request = lie
        sent = self._work("digest")
        with self.assertRaises(Fault) as caught:
            self._submit(sent, installation_id="worker-device")
        self.assertEqual(caught.exception.code, "transfer_digest_mismatch")
        self.assertEqual(self.app.store.find("task"), [])
        stored = [a for a in self.app.store.find("artifact") if a.get("host_id") == "device"]
        self.assertEqual(stored, [])

    def test_tool_submit_delivers_outside_the_transaction(self):
        sent = self._work("tool")
        runtime = Runtime(self.app)
        tool_call = call(
            "submit_task", prompt="read the attached notes",
            installation_id="worker-device", artifact_refs=[self.upload["id"]],
        )["tool_calls"][0]
        result = runtime.durable_tool({"id": sent["turn_id"]}, tool_call)
        self.assertNotIn("error", result)
        task = self.app.store.get("task", result["task_id"])
        self.assertEqual(Path(task["inputs"][0]["path"]).read_bytes(), self.raw)
        self.assertEqual(len(self.receives), 1)
        replay = runtime.durable_tool({"id": sent["turn_id"]}, tool_call)
        self.assertEqual(replay["task_id"], result["task_id"])
        self.assertEqual(len(self.receives), 1)

    def test_tool_submit_reports_a_refused_receive(self):
        def refuse(host_id, action, payload):
            assert_no_txn_io("broker.request")
            raise Fault("local_file_receive_disabled")
        self.app.broker.request = refuse
        sent = self._work("tool-refuse")
        runtime = Runtime(self.app)
        tool_call = call(
            "submit_task", prompt="read the attached notes",
            installation_id="worker-device", artifact_refs=[self.upload["id"]],
        )["tool_calls"][0]
        result = runtime.durable_tool({"id": sent["turn_id"]}, tool_call)
        self.assertEqual(result["error"], "artifact_delivery_failed")
        self.assertIn("allow_file_receive", result["advice"])
        self.assertEqual(self.app.store.find("task"), [])

    def test_prompt_says_a_chat_upload_can_be_copied(self):
        self.assertIn("source user_upload", PLANNER_SYSTEM)
        self.assertIn("device-local path", PLANNER_SYSTEM)
        self.assertNotIn("machine-A", PLANNER_SYSTEM)
        description = next(t["function"]["description"] for t in Runtime(self.app).tools_for(
            {"bot_id": "bot-main"}) if t["function"]["name"] == "submit_task")
        self.assertIn("copied onto the chosen executor", description)


if __name__ == "__main__":
    unittest.main()
