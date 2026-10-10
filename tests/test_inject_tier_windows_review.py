""" review: Windows cannot carry tier 2, and a queue is not a delivery.

Tier 2 resumes by SIGINT in the runner, which does not signal on Windows.
A Windows host is therefore tier 3 at inject time. A successful queue only
means the mailbox accepted the sentence; undelivered uuids come back on the
execution_result the Bot reads.
"""
import json
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang.context import projection
from pangpang.execution import ADAPTER_CATALOG, CLI_SESSION_RESUME
from pangpang.runtime import TOOLS
from pangpang.service import INJECT_UNSUPPORTED_ADVICE, Service, undelivered_inject_uuids
from pangpang.store import Fault


class WindowsTier2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _running(self, key):
        sent = self.app.send("bot-main", {"text": "执行", "allow_write": True}, key)
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "写一节"})
        task = self.app.store.get("task", submitted["task_id"])
        task["status"] = "running"
        self.app.store.put("task", task)
        turn = self.app.store.get("turn", sent["turn_id"])
        turn["status"] = "running"
        self.app.store.put("turn", turn)
        dep = self.app.store.get("dependency", task["dependency_id"])
        directory = Path(self.app.execution.directory(
            self.app.execution.hosts[task["host_id"]], task["execution_id"]))
        directory.mkdir(parents=True, exist_ok=True)
        return sent, dep, directory

    def test_windows_host_rejects_every_tier_2_executor(self):
        # platform.system() on a Windows local host is "Windows".
        self.app.execution.hosts["local"]["os"] = "Windows"
        ADAPTER_CATALOG["future-resume"] = {
            "binary": "future-resume", "argv": ["--skip-trust"], "concurrency": 1,
            "interjection_tier": 2, "resume_protocol": CLI_SESSION_RESUME}
        self.addCleanup(ADAPTER_CATALOG.pop, "future-resume", None)
        for key, adapter in (("gemini-win", "gemini"), ("future-win", "future-resume")):
            sent, dep, directory = self._running(key)
            self.app.execution.installations["codex-local"]["adapter"] = adapter
            with self.assertRaises(Fault) as caught:
                self.app.send_bot_message("bot-main", sent["work_id"], {
                    "dep_id": dep["id"], "text": "后半段改短"})
            self.assertEqual(caught.exception.code, "interjection_unsupported", adapter)
            self.assertEqual(caught.exception.advice, INJECT_UNSUPPORTED_ADVICE)
            self.assertIn("cancel_dependency", caught.exception.advice)
            self.assertFalse((directory / "injects.jsonl").exists(), adapter)

    def test_windows_host_still_queues_tier_1(self):
        self.app.execution.hosts["local"]["os"] = "Windows"
        self.app.execution.installations["codex-local"]["adapter"] = "claude-code"
        sent, dep, directory = self._running("claude-win")
        result = self.app.send_bot_message("bot-main", sent["work_id"], {
            "dep_id": dep["id"], "text": "加一句"})
        self.assertEqual(result["tier"], 1)
        self.assertTrue(result["queued"])
        self.assertNotIn("injected", result)
        self.assertTrue((directory / "injects.jsonl").is_file())


class UndeliveredInjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_receipt_flag_is_not_delivery_until_it_is_true(self):
        missed = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        landed = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        resumed_miss = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        resumed_ok = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        receipt = {"injects": [
            {"uuid": missed, "delivered": False},
            {"uuid": landed, "delivered": True},
            {"uuid": "no-flag"},
            "not-a-row",
        ], "resumed": [
            {"uuid": resumed_miss, "resumed": False},
            {"uuid": resumed_ok, "resumed": True},
        ]}
        self.assertEqual(undelivered_inject_uuids(receipt), [missed, "no-flag", resumed_miss])
        self.assertEqual(undelivered_inject_uuids({"injects": [{"uuid": landed, "delivered": True}]}), [])
        self.assertEqual(undelivered_inject_uuids(None), [])

    def test_execution_result_names_the_line_that_did_not_land(self):
        sent = self.app.send("bot-main", {"text": "写一章", "allow_write": True}, "land")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "写完"})
        task = self.app.store.get("task", submitted["task_id"])
        missed = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        landed = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        self.app.complete_task(task, {
            "execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
            "status": "succeeded", "process_tree_confirmed": True,
            "supervision": "windows-job-object", "exit_code": 0, "artifacts": [],
            "output_warnings": [], "tail": "FINAL",
            "injects": [
                {"uuid": missed, "delivered": False},
                {"uuid": landed, "delivered": True},
            ]})
        dep = self.app.store.get("dependency", task["dependency_id"])
        self.assertEqual(dep["result_payload"]["undelivered_injects"], [missed])
        self.assertNotIn(landed, dep["result_payload"]["undelivered_injects"])
        self.assertIn("没有进入执行器", dep["result_payload"]["notice"])
        message = next(item for item in self.app.store.find("message", work_id=sent["work_id"])
                       if item.get("kind") == "execution_result")
        body = json.loads(message["content"])
        self.assertEqual(body["undelivered_injects"], [missed])
        system = projection(self.app, self.app.store.get("turn", sent["turn_id"]))[0]["content"]
        self.assertIn("undelivered_injects", system)
        described = next(item["function"]["description"] for item in TOOLS
                         if item["function"]["name"] == "send_bot_message")
        self.assertIn("queued=true", described)
        self.assertIn("undelivered_injects", described)
        self.assertNotIn("已经打断", described)
        self.assertNotIn("已打断", system)


if __name__ == "__main__":
    unittest.main()
