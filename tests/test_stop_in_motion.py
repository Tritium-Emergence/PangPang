""": a stop must reach work that is already in motion.

A — the stop turn's packet names each pending dependency's target (tool calls never
    enter the cross-turn history, so this is the only place the dispatch is visible);
C — a `fallback` route does not block cancel_dependency.
"""
import json
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang import consent
from pangpang.context import projection
from pangpang.runtime import Runtime
from pangpang.service import Service


def packet_of(messages):
    system = messages[0]["content"]
    return json.loads(system[system.index("{"):])


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c-" + name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class StopInMotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = config(self.root)
        # Admit an executor on any Linux host (cgroup scope or, with consent, the
        # process-group tier); both tiers are L0-only. No task is launched here.
        cfg["hosts"][0][consent.CONSENT_KEY] = consent.RISK_VERSION
        self.app = Service(cfg, self.root / "data", deployment_level="L0")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _dispatch(self, text="把草稿箱里的文章都删除", prompt="删除草稿箱里的所有文章"):
        sent = self.app.send("bot-main", {"text": text}, "go-" + text)
        task = self.app.submit_task("bot-main", sent["work_id"], {"prompt": prompt})
        return sent, task

    # --- A ----------------------------------------------------------------
    def test_pending_task_dependency_names_the_running_task(self):
        sent, task = self._dispatch()
        stop = self.app.send("bot-main", {"text": "停停！", "work_id": sent["work_id"]}, "stop")
        messages = projection(self.app, self.app.store.get("turn", stop["turn_id"]))
        dep = next(d for d in packet_of(messages)["dependencies"] if d["status"] == "pending")
        self.assertEqual(dep["id"], task["dependency_id"])
        activity = dep["target"].pop("activity")
        self.assertEqual(activity["status"], "queued")
        self.assertIn("age_s", activity)
        self.assertEqual(dep["target"], {"task_id": task["task_id"], "status": "queued",
                                         "installation_id": "codex-local", "request": "删除草稿箱里的所有文章",
                                         "interjection_tier": 1})
        self.assertIn("A dependency with status=pending in packet.dependencies", messages[0]["content"])

        self.app.cancel(task["task_id"])
        messages = projection(self.app, self.app.store.get("turn", stop["turn_id"]))
        settled = next(d for d in packet_of(messages)["dependencies"] if d["id"] == task["dependency_id"])
        self.assertEqual(settled["status"], "settled")
        self.assertNotIn("target", settled)

    def test_pending_child_work_dependency_names_its_goal(self):
        editor = self.app.create_bot({"name": "编辑", "role": "编辑"})
        src = self.app.send("bot-main", {"text": "编辑正文"}, "child")
        handoff = self.app.delegate("bot-main", src["work_id"], {"target_bot_id": editor["id"], "goal": "改正文",
                                                                "request": "改", "acceptance": ["ok"]})
        stop = self.app.send("bot-main", {"text": "别改了", "work_id": src["work_id"]}, "child-stop")
        dep = next(d for d in packet_of(projection(self.app, self.app.store.get("turn", stop["turn_id"])))["dependencies"]
                   if d["status"] == "pending")
        self.assertEqual(dep["target"]["goal"], "改正文")
        self.assertEqual(dep["target"]["bot_id"], editor["id"])
        self.assertEqual(dep["target"]["work_id"], self.app.store.get("turn", handoff["turn_id"])["work_id"])

    def test_stop_after_narrated_dispatch_sees_the_task(self):
        """Round 1 narrates + reads the catalog while the Work is still provisional,
        round 2 dispatches in place. The narration is held only while link_work
        could still rebind; the dispatch settles ownership here, so that prose is
        stored and the stop turn sees it. The dispatch tool call is still not
        cross-turn history. The packet names the task."""
        class Model:
            def __init__(self):
                self.stop_inputs = []

            def complete(self, profile, messages, tools):
                last = messages[-1]
                if last["role"] == "user" and "停停" in last["content"]:
                    self.stop_inputs.append(messages)
                    return {"role": "assistant", "content": "收到。"}, {}
                if last["role"] == "user":
                    reply = call("catalog")
                    reply["content"] = "好，我先看一眼现在执行器和主机状态，再派活。"
                    return reply, {}
                if '"c-catalog"' in json.dumps(last) or last.get("tool_call_id") == "c-catalog":
                    return call("submit_task", prompt="删除草稿箱里的所有文章"), {}
                return {"role": "assistant", "content": "ok"}, {}

        model = Model()
        runtime = Runtime(self.app, model)
        first = self.app.send("bot-main", {"text": "把草稿箱里的文章都删除", "allow_write": True}, "n1")
        for _ in range(6):
            runtime.scheduler.run_one_sync() # turns only; no task launch
        task = self.app.store.find("task", work_id=first["work_id"])[0]
        self.app.send("bot-main", {"text": "停停！", "work_id": first["work_id"]}, "n2")
        for _ in range(4):
            runtime.scheduler.run_one_sync() # turns only; no task launch
        stop_input = model.stop_inputs[0]
        chat = json.dumps(stop_input[1:], ensure_ascii=False)
        self.assertIn("先看一眼", chat) # in-place dispatch persisted the narration
        self.assertNotIn("submit_task", chat) # dispatch tool call is not cross-turn history
        dep = next(d for d in packet_of(stop_input)["dependencies"] if d["status"] == "pending")
        self.assertEqual(dep["target"]["task_id"], task["id"]) # but the packet names it.
        self.assertEqual(dep["target"]["request"], "删除草稿箱里的所有文章")

    # --- C ----------------------------------------------------------------
    def test_fallback_route_still_allows_cancel_dependency(self):
        sent, task = self._dispatch()
        routing = {"work_id": sent["work_id"], "work_hint_id": sent["work_id"], "route_source": "fallback",
                   "route_quality": "fallback", "proposal": None, "stream_id": "tab-fb", "last_seen_event_id": None,
                   "correction_target_message_id": None, "correction_superseded_message_ids": []}
        seen = []

        class Model:
            def complete(self, profile, messages, tools):
                if messages[-1]["role"] == "tool":
                    seen.append(json.loads(messages[-1]["content"]))
                    return {"role": "assistant", "content": "已停。"}, {}
                dep = next(d for d in packet_of(messages)["dependencies"] if d["status"] == "pending")
                return call("cancel_dependency", dep_id=dep["id"]), {}

        stop = self.app.send("bot-main", {"text": "停掉！", "stream_id": "tab-fb"}, "fb-stop", routing=routing)
        self.assertEqual(stop["work_id"], sent["work_id"])
        self.assertEqual(self.app.store.get("turn", stop["turn_id"])["route_quality"], "fallback")
        runtime = Runtime(self.app, Model())
        for _ in range(4):
            runtime.scheduler.run_one_sync() # turns only; no task launch
        self.assertEqual(seen[0], {"dependency_id": task["dependency_id"], "status": "cancelled",
                                   "task_id": task["task_id"]})
        self.assertNotIn("output_dir", seen[0])
        self.assertEqual(self.app.store.get("task", task["task_id"])["status"], "cancelled")

    def test_fallback_route_still_blocks_other_consequential_tools(self):
        sent, _ = self._dispatch()
        routing = {"work_id": sent["work_id"], "work_hint_id": sent["work_id"], "route_source": "fallback",
                   "route_quality": "fallback", "proposal": None, "stream_id": "tab-fb2", "last_seen_event_id": None,
                   "correction_target_message_id": None, "correction_superseded_message_ids": []}
        seen = []

        class Model:
            def complete(self, profile, messages, tools):
                if messages[-1]["role"] == "tool":
                    seen.append(json.loads(messages[-1]["content"]))
                    return {"role": "assistant", "content": "未执行。"}, {}
                return call("create_routine", instruction="x", schedule={"kind": "once", "at": "2030-01-01T00:00:00"}), {}

        self.app.send("bot-main", {"text": "定个闹钟", "stream_id": "tab-fb2"}, "fb-other", routing=routing)
        runtime = Runtime(self.app, Model())
        for _ in range(4):
            runtime.scheduler.run_one_sync() # turns only; no task launch
        self.assertEqual(seen[0]["error"], "route_confirmation_required")


if __name__ == "__main__":
    unittest.main()
