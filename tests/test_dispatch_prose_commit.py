""" /: persist dispatch prose only after the batch commits, and refuse
send_bot_message aimed at an executor task. Scripted models emit fixed tool
calls; they do not follow the prompt."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.pi_runtime import PiModel
from pangpang.runtime import Runtime, TOOLS
from pangpang.semantic_gate import GateUnavailable
from pangpang.service import Service
from pangpang.store import Fault


PROSE = "我让执行器在工作目录里整理已有笔记。"


def tool(call_id, name, body):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(body)}}


def assistants(app, content=None):
    rows = [m for m in app.store.find("message") if m.get("kind") == "assistant"]
    if content is None:
        return rows
    return [m for m in rows if m.get("content") == content]


def tool_results(turn):
    out = []
    for message in turn.get("history") or []:
        if message.get("role") != "tool":
            continue
        out.append(json.loads(message["content"]))
    return out


class _Once:
    def __init__(self, first, rest=None):
        self.n = 0
        self.first = first
        self.rest = rest or {"content": "收到工具结果。"}
        self.judge_calls = 0

    def complete(self, profile, messages, tools):
        self.n += 1
        return (self.first if self.n == 1 else self.rest), {}


class ReplyCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _send(self, text="整理工作区里的笔记", key="note"):
        return self.app.send("bot-main", {"text": text}, key)

    def test_submit_text_is_persisted_after_commit_and_replay_is_once(self):
        self.app.probe("codex-local")
        sent = self._send()
        call_id = "call-submit-1"
        calls = [tool(call_id, "submit_task", {"prompt": "整理工作目录里的笔记"})]
        model = _Once({"role": "assistant", "content": PROSE, "tool_calls": calls})
        runtime = Runtime(self.app, model)
        runtime.semantic_gate.judge = lambda state: self.fail("mode=off must not call Jev")
        runtime.run(self.app.store.get("turn", sent["turn_id"]))
        self.assertFalse(runtime.interim_deliverable(calls))
        self.assertEqual(model.n, 1)
        rows = assistants(self.app, PROSE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "msg-%s-%s" % (sent["turn_id"], call_id))
        self.assertTrue(rows[0].get("interim"))
        work = self.app.store.get("work", sent["work_id"])
        self.assertEqual(work["status"], "waiting")
        self.assertEqual(len(self.app.store.find("task")), 1)
        turn = self.app.store.get("turn", sent["turn_id"])
        runtime.run(turn)
        self.assertEqual(model.n, 1)
        self.assertEqual(len(assistants(self.app, PROSE)), 1)

    def test_pi_submit_text_is_persisted_and_replay_is_once(self):
        self.app.probe("codex-local")
        sent = self._send(key="pi-note")
        call_id = "call-submit-pi"
        args = {"prompt": "整理工作目录里的笔记"}
        calls = [tool(call_id, "submit_task", args)]
        seen = {"n": 0}

        def exchange(profile, history, tools, handler, **kwargs):
            seen["n"] += 1
            if seen["n"] > 1:
                raise AssertionError("replay must not call the model")
            handler({"type": "context"})
            handler({"type": "model_begin"})
            handler({"type": "assistant", "stop": "toolUse", "usage": {}, "message": {
                "role": "assistant", "content": PROSE, "tool_calls": calls}})
            handler({"type": "tool", "call_id": call_id, "name": "submit_task",
                     "args": json.loads(calls[0]["function"]["arguments"])})
            return {"waiting": True}

        runtime = Runtime(self.app)
        with patch.object(PiModel, "exchange", side_effect=exchange):
            runtime.run(self.app.store.get("turn", sent["turn_id"]))
            rows = assistants(self.app, PROSE)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "msg-%s-%s" % (sent["turn_id"], call_id))
            self.assertFalse(runtime.interim_deliverable(calls))
            self.assertEqual(self.app.store.get("work", sent["work_id"])["status"], "waiting")
            self.assertEqual(len(self.app.store.find("task")), 1)
            turn = self.app.store.get("turn", sent["turn_id"])
            runtime.run(turn)
        self.assertEqual(seen["n"], 1)
        self.assertEqual(len(assistants(self.app, PROSE)), 1)

    def test_checkpoint_text_uses_committed_id(self):
        sent = self._send(text="记下阶段", key="ck")
        call_id = "call-ck"
        calls = [tool(call_id, "checkpoint_work",
                      {"work_id": sent["work_id"], "summary": "阶段已记下"})]
        model = _Once({"role": "assistant", "content": PROSE, "tool_calls": calls},
                      {"content": "阶段已经写上。"})
        runtime = Runtime(self.app, model)
        self.assertTrue(runtime.interim_deliverable(calls))
        runtime.run(self.app.store.get("turn", sent["turn_id"]))
        rows = assistants(self.app, PROSE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "msg-%s-%s" % (sent["turn_id"], call_id))
        self.assertEqual(self.app.store.get("work", sent["work_id"])["checkpoint"]["summary"],
                         "阶段已记下")

    def test_failed_checkpoint_withholds_text(self):
        failed = self._send(text="再记一次", key="ck-bad")
        bad = [tool("call-ck-bad", "checkpoint_work",
                    {"work_id": "work-does-not-exist", "summary": "不该写上"})]
        held = "笔记已经整理好了。"
        model = _Once({"role": "assistant", "content": held, "tool_calls": bad},
                      {"content": "checkpoint 被拒绝了，还没写上。"})
        Runtime(self.app, model).run(self.app.store.get("turn", failed["turn_id"]))
        contents = [m["content"] for m in assistants(self.app)]
        self.assertNotIn(held, contents)
        self.assertIn("checkpoint 被拒绝了，还没写上。", contents)
        turn = self.app.store.get("turn", failed["turn_id"])
        self.assertTrue(any(r.get("error") for r in tool_results(turn)))

    def test_mixed_readonly_and_checkpoint_uses_committed_id(self):
        sent = self._send(text="先看再记", key="mix-ok")
        prose = "目录看过了，阶段也记下了。"
        calls = [
            tool("call-cat", "catalog", {}),
            tool("call-mix-ck", "checkpoint_work",
                 {"work_id": sent["work_id"], "summary": "阶段已记下"}),
        ]
        runtime = Runtime(self.app, _Once({"content": prose, "tool_calls": calls},
                                          {"content": "这两步都写上了。"}))
        self.assertTrue(runtime.interim_deliverable(calls))
        runtime.run(self.app.store.get("turn", sent["turn_id"]))
        rows = assistants(self.app, prose)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "msg-%s-call-cat" % sent["turn_id"])
        self.assertEqual(self.app.store.get("work", sent["work_id"])["checkpoint"]["summary"],
                         "阶段已记下")

    def test_mixed_readonly_and_failing_checkpoint_withholds_text(self):
        failed = self._send(text="再记坏的", key="mix-bad")
        held = "目录看过了，阶段也写好了。"
        bad = [
            tool("call-cat-bad", "catalog", {}),
            tool("call-mix-bad", "checkpoint_work", {
                "work_id": failed["work_id"], "summary": "不该写上",
                "next_actions": ["x"] * 41}),
        ]
        runtime = Runtime(self.app, _Once({"content": held, "tool_calls": bad},
                                          {"content": "checkpoint 被拒绝了，正文不该先落。"}))
        self.assertTrue(runtime.interim_deliverable(bad))
        runtime.run(self.app.store.get("turn", failed["turn_id"]))
        contents = [m["content"] for m in assistants(self.app)]
        self.assertNotIn(held, contents)
        self.assertIn("checkpoint 被拒绝了，正文不该先落。", contents)
        turn = self.app.store.get("turn", failed["turn_id"])
        self.assertTrue(any(r.get("error") == "invalid_checkpoint" for r in tool_results(turn)))

    def test_text_withheld_when_batch_does_not_commit(self):
        self.app.probe("codex-local")
        sent = self._send(key="err")
        prose = "两件事都办好了。"
        calls = [
            tool("call-ok", "submit_task", {"prompt": "先跑着"}),
            tool("call-bad", "checkpoint_work",
                 {"work_id": "work-does-not-exist", "summary": "不该写"}),
        ]
        Runtime(self.app, _Once({"content": prose, "tool_calls": calls})).run(
            self.app.store.get("turn", sent["turn_id"]))
        self.assertEqual(assistants(self.app, prose), [])
        self.assertEqual(len(self.app.store.find("task", work_id=sent["work_id"])), 1)
        turn = self.app.store.get("turn", sent["turn_id"])
        self.assertTrue(any(r.get("error") for r in tool_results(turn)))
        self.assertTrue(any(r.get("yielded") for r in tool_results(turn)))

        settled = self._send(text="改方向", key="settled")
        first = self.app.submit_task("bot-main", settled["work_id"], {"prompt": "旧任务"})
        dep = self.app.store.get("dependency", first["dependency_id"])
        dep["status"] = "settled"
        self.app.store.put("dependency", dep)
        prose = "已经按新方向重派了。"
        calls = [
            tool("call-cancel", "cancel_dependency", {"dep_id": dep["id"]}),
            tool("call-again", "submit_task", {"prompt": "新方向"}),
        ]
        Runtime(self.app, _Once({"content": prose, "tool_calls": calls})).run(
            self.app.store.get("turn", settled["turn_id"]))
        self.assertEqual(assistants(self.app, prose), [])
        turn = self.app.store.get("turn", settled["turn_id"])
        results = tool_results(turn)
        self.assertTrue(any(r.get("status") == "already_settled" for r in results))
        self.assertTrue(any(r.get("task_id") for r in results))

        old = self._send(text="旧话题", key="old")
        new = self._send(text="还是旧话题", key="link")
        prose = "我把它接回上一件工作了。"
        calls = [tool("call-link", "link_work", {"work_id": old["work_id"]})]
        Runtime(self.app, _Once({"content": prose, "tool_calls": calls})).run(
            self.app.store.get("turn", new["turn_id"]))
        self.assertEqual(assistants(self.app, prose), [])
        self.assertEqual(self.app.store.get("work", new["work_id"])["status"], "linked")

    def test_semantic_gate_outcomes_withhold_text_after_a_yielded_submit(self):
        self.app.probe("codex-local")
        peer = self.app.create_bot({"name": "编辑", "role": "编辑"})
        peer_sent = self.app.send(peer["id"], {"text": "待命"}, "peer")
        cases = {
            "ask": {"disposition": "ASK_USER"},
            "revise": {"disposition": "REVISE"},
            "unavailable": None,
        }
        for name, decision in cases.items():
            with self.subTest(name=name):
                sent = self._send(text="派活 " + name, key="gate-" + name)
                prose = "已经派出去了：" + name
                calls = [
                    tool("call-submit-" + name, "submit_task", {"prompt": "跑 " + name}),
                    tool("call-send-" + name, "send_bot_message", {
                        "target_bot_id": peer["id"], "target_work_id": peer_sent["work_id"],
                        "text": "问一句", "requires_response": False}),
                ]
                runtime = Runtime(self.app, _Once({"content": prose, "tool_calls": calls}))
                runtime.semantic_gate.mode = "enforce"

                def judge(state, decision=decision):
                    tool_name = state["proposed_action"]["tool"]
                    if tool_name == "submit_task":
                        return {"disposition": "EXECUTE", "probabilities": {},
                                "confidence": 1, "model": "fake", "latency_ms": 1}
                    if decision is None:
                        raise GateUnavailable("down")
                    return dict(decision, probabilities={}, confidence=1,
                                model="fake", latency_ms=1)

                runtime.semantic_gate.judge = judge
                runtime.run(self.app.store.get("turn", sent["turn_id"]))
                self.assertEqual(assistants(self.app, prose), [])
                turn = self.app.store.get("turn", sent["turn_id"])
                results = tool_results(turn)
                self.assertTrue(any(r.get("yielded") and r.get("task_id") for r in results), results)
                if name == "ask":
                    self.assertTrue(any(r.get("semantic_gate") == "ASK_USER" for r in results), results)
                elif name == "revise":
                    self.assertTrue(any(r.get("semantic_gate") == "REVISE" for r in results), results)
                else:
                    self.assertTrue(any(r.get("gate_error") == "unavailable" for r in results), results)
                self.assertFalse(any(r.get("error") and r.get("gate_error") == "unavailable" for r in results))

    def test_text_only_turn_keeps_a_waiting_work(self):
        self.app.probe("codex-local")
        sent = self._send(key="wait")
        self.app.submit_task("bot-main", sent["work_id"], {"prompt": "先跑着"})
        self.assertEqual(self.app.store.get("work", sent["work_id"])["status"], "waiting")
        Runtime(self.app, _Once({"content": "笔记还在跑，等结果回来再补。"})).run(
            self.app.store.get("turn", sent["turn_id"]))
        work = self.app.store.get("work", sent["work_id"])
        self.assertEqual(work["status"], "waiting")
        self.assertTrue(work.get("pending_dep_ids"))

    def test_new_work_projection_shows_other_waiting_in_flight(self):
        self.app.probe("codex-local")
        first = self._send(text="先整理笔记", key="idx-a")
        submitted = self.app.submit_task(
            "bot-main", first["work_id"], {"prompt": "SECRET-TASK-BODY-112"})
        task = self.app.store.get("task", submitted["task_id"])
        task["started"] = time.time() - 120
        task["status"] = "running"
        self.app.store.put("task", task)
        second = self._send(text="另一件事", key="idx-b")
        self.assertNotEqual(first["work_id"], second["work_id"])
        system = Runtime(self.app).context(
            self.app.store.get("turn", second["turn_id"]))[0]["content"]
        packet = json.loads(system[system.index("{"):])
        entry = next(row for row in packet["recent_work_index"] if row["id"] == first["work_id"])
        self.assertEqual(entry["status"], "waiting")
        self.assertEqual(len(entry["in_flight"]), 1)
        item = entry["in_flight"][0]
        self.assertEqual(set(item), {"kind", "status", "age_s"})
        self.assertEqual(item["kind"], "task")
        self.assertEqual(item["status"], "running")
        self.assertGreaterEqual(item["age_s"], 100)
        blob = json.dumps(entry["in_flight"], ensure_ascii=False)
        self.assertNotIn("SECRET-TASK-BODY-112", blob)
        self.assertNotIn(submitted["dependency_id"], blob)
        self.assertNotIn(submitted["task_id"], blob)
        self.assertIn("status=waiting means that work has unsettled dependencies", system)

    def test_send_bot_message_task_id_returns_not_found_advice(self):
        self.app.probe("codex-local")
        sent = self._send(key="msg")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "在跑"})
        body = {"target_bot_id": "bot-main", "target_work_id": submitted["task_id"],
                "text": "再加一节", "requires_response": True}
        with self.assertRaises(Fault) as caught:
            self.app.send_bot_message("bot-main", sent["work_id"], body)
        self.assertEqual(caught.exception.code, "not_found")
        advice = caught.exception.advice
        for phrase in ("Work id", "task id", "execution_result", "submit_task", "cancel_dependency"):
            self.assertIn(phrase, advice)
        with self.assertRaises(Fault) as missing:
            self.app.send_bot_message("bot-main", sent["work_id"],
                                      dict(body, target_work_id="work-missing"))
        self.assertEqual(missing.exception.code, "not_found")
        self.assertIsNone(missing.exception.advice)

        turn = self.app.store.get("turn", sent["turn_id"])
        runtime = Runtime(self.app, model=object())
        runtime.semantic_gate.judge = lambda state: self.fail("mode=off must not call Jev")
        result = runtime.durable_tool(turn, tool("call-send-off", "send_bot_message", body))
        self.assertEqual(result.get("error"), "not_found")
        self.assertIn("submit_task", result.get("advice", ""))

        runtime.semantic_gate.mode = "enforce"
        runtime.semantic_gate.judge = lambda state: self.fail("preflight must not call Jev")
        enforced = runtime.durable_tool(turn, tool("call-send-enforce", "send_bot_message", body))
        self.assertEqual(enforced.get("error"), "not_found")
        self.assertIn("cancel_dependency", enforced.get("advice", ""))
        described = {t["function"]["name"]: t["function"]["description"] for t in TOOLS}
        self.assertNotIn("已有工作的追加要求不要重新 delegate，使用 send_bot_message",
                         described["delegate"])
        self.assertIn("another long-running Bot's Work", described["send_bot_message"])
        self.assertIn("task id", described["send_bot_message"])
        system = runtime.context(turn)[0]["content"]
        self.assertNotIn("已有工作的追加要求用 send_bot_message", system)


if __name__ == "__main__":
    unittest.main()
