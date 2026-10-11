""": a tier 1/2 correction is an inject, not a cancel.

The planner sees the running task's catalog tier on the pending dependency.
The instruction and the tool descriptions send a correction into that same
session when the tier is 1 or 2, and still cancel-and-redispatch for tier 3.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.context import model_dependency_view, projection
from pangpang.execution import ADAPTER_CATALOG, CLI_SESSION_RESUME
from pangpang.runtime import Runtime, TOOLS
from pangpang.service import EXECUTOR_TASK_MESSAGE_ADVICE, Service
from pangpang.store import Fault


def packet_of(messages):
    system = messages[0]["content"]
    return json.loads(system[system.index("{"):])


def tool_description(name):
    return next(item["function"]["description"] for item in TOOLS if item["function"]["name"] == name)


class Issue147TierRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _running_task(self):
        sent = self.app.send("bot-main", {"text": "写一章"}, "go")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "写完这一章"})
        follow = self.app.send("bot-main", {"text": "后半段改成短一些", "work_id": sent["work_id"]}, "fix")
        return sent, submitted, follow

    def _retarget(self, submitted, installation_id, adapter):
        self.app.execution.installations[installation_id] = {
            "id": installation_id, "adapter": adapter, "host_id": "local"}
        task = self.app.store.get("task", submitted["task_id"])
        task["installation_id"] = installation_id
        self.app.store.put("task", task)

    def _pending_target(self, follow):
        messages = projection(self.app, self.app.store.get("turn", follow["turn_id"]))
        packet = packet_of(messages)
        return next(item for item in packet["dependencies"] if item.get("status") == "pending"), messages

    def test_projection_shows_catalog_tier_for_the_running_task(self):
        _, submitted, follow = self._running_task()
        # The test installation is codex, which declares tier 1.
        target, _ = self._pending_target(follow)
        self.assertEqual(target["target"]["interjection_tier"], 1)
        self.assertEqual(target["target"]["installation_id"], "codex-local")

        cases = (
            ("inst-claude", "claude-code", 1),
            ("inst-codex", "codex", 1),
            ("inst-gemini", "gemini", 2),
            ("inst-qwen", "qwen-claude", 3),
            ("inst-pi", "pi", 3),
            ("inst-acp", "acp", 3),
)
        for installation_id, adapter, tier in cases:
            self._retarget(submitted, installation_id, adapter)
            target, _ = self._pending_target(follow)
            self.assertEqual(target["target"]["interjection_tier"], tier, adapter)
            self.assertNotIn("stdin_protocol", target["target"])
            self.assertNotIn("resume_protocol", target["target"])

        task = self.app.store.get("task", submitted["task_id"])
        task["installation_id"] = "inst-missing"
        self.app.store.put("task", task)
        target, _ = self._pending_target(follow)
        self.assertEqual(target["target"]["interjection_tier"], 3)

    def test_read_work_shows_the_same_tier(self):
        sent, submitted, follow = self._running_task()
        self._retarget(submitted, "inst-gemini", "gemini")
        turn = self.app.store.get("turn", follow["turn_id"])
        view = Runtime(self.app).invoke(turn, "read_work", {"work_id": sent["work_id"]})
        dep = next(item for item in view["dependencies"] if item.get("status") == "pending")
        self.assertEqual(dep["target"]["interjection_tier"], 2)
        self.assertEqual(dep["id"], submitted["dependency_id"])

    def test_tier_follows_the_catalog_row_not_the_adapter_name(self):
        _, submitted, follow = self._running_task()
        dep = self.app.store.get("dependency", submitted["dependency_id"])
        rows = {
            "future-exec": {"interjection_tier": 1, "stdin_protocol": "claude-stream-json"},
            "future-resume": {"interjection_tier": 2, "resume_protocol": CLI_SESSION_RESUME},
            "future-bare": {"interjection_tier": 1},
        }
        with patch.dict(ADAPTER_CATALOG, rows):
            for adapter, tier in (("future-exec", 1), ("future-resume", 2), ("future-bare", 3)):
                view = model_dependency_view(
                    dep, own=True, store=self.app.store,
                    installations={"codex-local": {"adapter": adapter}},
                    hosts=self.app.execution.hosts)
                self.assertEqual(view["target"]["interjection_tier"], tier, adapter)

    def test_shown_tier_is_the_tier_inject_accepts(self):
        # review: the projected tier and inject_task_message share one
        # predicate. A connector host has no mailbox the runner reads; a
        # Windows host gets no SIGINT, so tier 2 there is 3.
        _, submitted, follow = self._running_task()
        task = self.app.store.get("task", submitted["task_id"])
        task["status"] = "running"
        self.app.store.put("task", task)
        host = self.app.execution.hosts["local"]
        cases = (
            ("connector", "Linux", "claude-code", 3),
            ("connector", "Linux", "gemini", 3),
            ("ssh", "Linux", "gemini", 3),
            ("local", "Windows", "gemini", 3),
            ("local", "Windows", "claude-code", 1),
)
        for transport, os_label, adapter, tier in cases:
            host["transport"], host["os"] = transport, os_label
            self._retarget(submitted, "inst-x", adapter)
            target, _ = self._pending_target(follow)
            self.assertEqual(target["target"]["interjection_tier"], tier, (transport, os_label, adapter))
            turn = self.app.store.get("turn", follow["turn_id"])
            with patch.object(self.app.execution, "inject") as inject:
                try:
                    result = self.app.inject_task_message(
                        "bot-main", turn["work_id"],
                        {"dep_id": submitted["dependency_id"], "text": "后半段改短"},
                        turn_id=turn["id"], message_uuid="u-" + adapter)
                except Fault as caught:
                    self.assertEqual(tier, 3, (transport, os_label, adapter))
                    self.assertEqual(caught.code, "interjection_unsupported")
                    inject.assert_not_called()
                else:
                    self.assertEqual(result["tier"], tier, (transport, os_label, adapter))

    def test_child_work_has_no_executor_tier(self):
        editor = self.app.create_bot({"name": "编辑", "role": "编辑"})
        sent = self.app.send("bot-main", {"text": "交给编辑"}, "child")
        self.app.delegate("bot-main", sent["work_id"], {
            "target_bot_id": editor["id"], "goal": "改正文", "request": "改", "acceptance": ["ok"]})
        follow = self.app.send("bot-main", {"text": "后半段改短", "work_id": sent["work_id"]}, "child-fix")
        target, _ = self._pending_target(follow)
        self.assertEqual(target["kind"], "child_work")
        self.assertNotIn("interjection_tier", target.get("target") or {})

    def test_instruction_injects_tier_1_and_2_and_cancels_tier_3(self):
        _, _, follow = self._running_task()
        system = projection(self.app, self.app.store.get("turn", follow["turn_id"]))[0]["content"]
        self.assertIn("target.interjection_tier", system)
        self.assertIn(
            "send the user's exact line into the same running session with "
            "send_bot_message (only dep_id and text); do not cancel_dependency "
            "and do not open another task.",
            system)
        self.assertIn(
            "tier 1 or 2: send the user's exact line into the same running session "
            "via send_bot_message, with only dep_id and text.",
            system)
        self.assertIn("If the tool returns an error saying it cannot be accepted online, cancel-and-resubmit per the next clause.", system)
        self.assertIn("tier 3: first cancel_dependency to withdraw the running task", system)
        self.assertIn("When the user explicitly wants to stop, cancel or drop it, regardless of tier", system)
        self.assertIn("If the dependency is a child_work you delegated, cancel that dependency directly", system)
        self.assertNotIn("这就是纠正：先 cancel_dependency 撤回正在跑的任务", system)
        self.assertNotIn(
            "或用户要停、换目标、否定正在跑的任务）= 同一批里 cancel_dependency", system)

        inject = tool_description("send_bot_message")
        cancel = tool_description("cancel_dependency")
        self.assertIn("target.interjection_tier is 1 or 2", inject)
        self.assertIn("do not cancel_dependency and do not open another task.", inject)
        self.assertIn("When tier is 3 or absent, do not interject with this tool", inject)
        self.assertIn("target.interjection_tier is 3 or absent", cancel)
        self.assertIn("For a tier 1 or 2 correction or redirection, first send into the same session via send_bot_message", cancel)
        self.assertIn("only cancel-and-resubmit when that call returns that it cannot be accepted online", cancel)
        self.assertIn("target.interjection_tier is 1 or 2", tool_description("delegate"))
        self.assertIn("dep_id", EXECUTOR_TASK_MESSAGE_ADVICE)
        self.assertNotIn("cancel_dependency 只用于纠正或推翻", EXECUTOR_TASK_MESSAGE_ADVICE)
        self.assertNotIn("任务不能接收执行中途的消息", EXECUTOR_TASK_MESSAGE_ADVICE)


if __name__ == "__main__":
    unittest.main()
