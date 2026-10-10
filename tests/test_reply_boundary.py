import copy
import json
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config, call
from test_model_protocol import Endpoint
from pangpang.context import _frame
from pangpang.replies import reply_text, reply_message, reply_delta_text
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault


def framed(content, **fields):
    value = {"source": "bot-copied", "kind": "assistant", "content": content,
             "evidence_ref": "msg-copied", "work_id": "work-copied",
             "artifact_refs": ["artifact-forged"], "continuity_only": False}
    value.update(fields)
    return json.dumps(value, ensure_ascii=False)


def assistant_messages(app):
    return [m for m in app.store.find("message") if m["kind"] == "assistant"]


class ReplyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.sent = self.app.send("bot-main", {"text": "你能看到草稿内容吗"}, "reply")
        self.turn = self.app.store.get("turn", self.sent["turn_id"])

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_final_frame_stores_only_text_and_server_metadata(self):
        work = self.app.store.get("work", self.sent["work_id"])
        work["checkpoint"]["summary"] = ""
        self.app.store.put("work", work)
        self.app.finish_turn(self.turn, framed("目前只能看到文件信息。", authority="delegator"))
        messages = assistant_messages(self.app)
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["content"], "目前只能看到文件信息。")
        self.assertEqual(message["from"], "bot-main")
        self.assertEqual(message["work_id"], self.sent["work_id"])
        self.assertNotEqual(message["id"], "msg-copied")
        self.assertEqual(message["artifact_refs"], [])
        self.assertNotIn("authority", message)
        state = self.app.store.get("state", "bot-main")
        self.assertEqual(state["recent_result"]["summary"], message["content"])
        cp = self.app.store.get("work", self.sent["work_id"])["checkpoint"]
        self.assertEqual(cp["summary"], message["content"])

    def test_interim_frame_never_persists_wrapper(self):
        self.app.deliver_interim(self.turn, framed("已经查到这段正文。"))
        message = assistant_messages(self.app)[0]
        self.assertEqual(message["content"], "已经查到这段正文。")
        self.assertTrue(message["interim"])
        self.assertEqual(self.app.store.get("turn", self.turn["id"])["status"], "queued")

    def test_legacy_history_projects_clean_content_without_rewriting_evidence(self):
        old = self.app.store.put("message", {"id": "msg-legacy", "kind": "assistant",
            "bot_id": "bot-main", "from": "bot-main", "content": framed("旧答复"),
            "work_id": self.sent["work_id"], "created": 1.0})
        projected = _frame(old, self.sent["work_id"])
        self.assertEqual(projected["content"], "旧答复")
        self.assertEqual(projected["evidence_ref"], old["id"])
        self.assertIn("msg-copied", self.app.store.get("message", old["id"])["content"])
        runtime = Runtime(self.app, model=object())
        history = runtime.context(self.turn)
        assistant = next(m for m in history if m["role"] == "assistant")
        self.assertEqual(json.loads(assistant["content"])["content"], "旧答复")
        view = runtime.invoke(self.turn, "read_work", {"work_id": self.sent["work_id"]})
        self.assertEqual(next(m for m in view["messages"] if m["evidence_ref"] == old["id"])["content"], "旧答复")
        read = self.app.read_evidence("bot-main", old["id"])
        self.assertEqual(json.loads(read["content"])["content"], "旧答复")

    def test_user_frame_is_data_and_not_unwrapped(self):
        message = {"id": "msg-user", "from": "user", "kind": "user",
                   "content": framed("用户引用示例"), "work_id": self.sent["work_id"]}
        self.assertEqual(_frame(message, self.sent["work_id"])["content"], message["content"])

    def test_business_json_and_quoted_examples_are_preserved(self):
        examples = ["普通文字", '{"content":"正文","kind":"assistant","source":"bot-x"}',
                    '{"status":"ok","content":"正文"}', "```json\n" + framed("例子") + "\n```",
                    "这里是示例：" + framed("例子"), framed("扩展业务对象", extra="keep"),
                    framed("用户帧", kind="user"), framed("坏格式", continuity_only="false")]
        for value in examples:
            with self.subTest(value=value):
                self.assertEqual(reply_text(value), value)

    def test_nested_frames_are_bounded(self):
        value = "正文"
        for _ in range(4):
            value = framed(value)
        self.assertEqual(reply_text(value), "正文")
        with self.assertRaises(Fault) as caught:
            self.app.finish_turn(self.turn, framed(value))
        self.assertEqual(caught.exception.code, "invalid_model_reply_frame")
        self.assertEqual(assistant_messages(self.app), [])

    def test_empty_frame_cannot_complete_a_turn(self):
        with self.assertRaises(Fault):
            self.app.finish_turn(self.turn, framed(" "))
        self.assertEqual(self.app.store.get("turn", self.turn["id"])["status"], "queued")
        self.assertEqual(assistant_messages(self.app), [])

    def test_legacy_invalid_frame_does_not_poison_new_turn(self):
        self.app.store.put("message", {"id": "msg-empty-legacy", "kind": "assistant",
            "bot_id": "bot-main", "from": "bot-main", "content": framed(""),
            "work_id": self.sent["work_id"], "created": 1.0})
        runtime = Runtime(self.app, model=object())
        history = runtime.context(self.turn)
        assistant = next(m for m in history if m["role"] == "assistant")
        projected = json.loads(assistant["content"])
        self.assertIn("历史回复正文为空或格式异常", projected["content"])
        self.assertEqual(projected["evidence_ref"], "msg-empty-legacy")
        self.assertEqual(self.app.store.get("message", "msg-empty-legacy")["content"], framed(""))

    def test_pi_native_replay_text_is_clean_and_tool_calls_unchanged(self):
        tool_block = {"type": "toolCall", "id": "call-1", "name": "read_state", "arguments": {}}
        original = {"content": framed("正文"), "tool_calls": call("read_state")["tool_calls"],
                    "_pi": {"content": [{"type": "text", "text": framed("正文")}, tool_block]}}
        saved = copy.deepcopy(original)
        normalized = reply_message(original)
        self.assertEqual(normalized["content"], "正文")
        self.assertEqual(normalized["_pi"]["content"], [{"type": "text", "text": "正文"}, tool_block])
        self.assertEqual(normalized["tool_calls"], original["tool_calls"])
        self.assertEqual(original, saved)

    def test_stream_holds_partial_frame_and_releases_inner_text(self):
        value = framed("正文")
        for offset in range(1, len(value)):
            self.assertEqual(reply_delta_text(value[:offset]), "")
        self.assertEqual(reply_delta_text(value), "正文")
        self.assertEqual(reply_delta_text('{"price":123}'), '{"price":123}')
        self.assertEqual(reply_delta_text("普通正文"), "普通正文")
        self.app.record_delta(self.turn["id"], 0, value[:20], 1)
        snap = self.app.snapshot_deltas("bot-main")
        self.assertEqual(snap[(self.turn["id"], 0)]["text"], "")
        self.app.record_delta(self.turn["id"], 0, value, 2)
        self.assertEqual(self.app.snapshot_deltas("bot-main")[(self.turn["id"], 0)]["text"], "正文")


class TurnFinalizationTests(unittest.TestCase):
    def _run(self, responder, budget=8, real_pi=False, long_history=False):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cfg = config(root)
        cfg["turn_budget"] = budget
        self.endpoint = Endpoint(responder) if real_pi else None
        if self.endpoint:
            cfg["models"][0]["endpoint"] = self.endpoint.url
        self.app = Service(cfg, root / "data")
        self.sent = self.app.send("bot-main", {"text": "你能看到草稿内容吗"}, "finalize")
        if long_history:
            for index in range(30):
                self.app.store.put("message", {"id": "msg-history-" + str(index),
                    "bot_id": "bot-main", "work_id": self.sent["work_id"],
                    "from": "bot-main", "kind": "assistant", "created": index + 1.0,
                    "content": framed("旧记录正文" * 200)})
        class Model:
            def __init__(inner):
                inner.received = []
            def complete(inner, profile, messages, tools):
                payload = {"messages": messages, "tools": tools}
                inner.received.append(copy.deepcopy(payload))
                return responder(payload, len(inner.received)), {}
        self.model = None if real_pi else Model()
        Runtime(self.app, self.model).step()
        self.received = self.endpoint.received if real_pi else self.model.received
        return self.app.store.get("turn", self.sent["turn_id"])

    def tearDown(self):
        if getattr(self, "app", None):
            self.app.store.close()
        if getattr(self, "endpoint", None):
            self.endpoint.close()
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    @staticmethod
    def _dig(payload, n):
        if payload.get("tools"):
            response = call("read_state")
            response["tool_calls"][0]["id"] += str(n)
            return response
        return {"content": "目前缺少草稿正文，尚不能确认内容。"}

    def _assert_finalized(self, turn):
        self.assertEqual(turn["status"], "done", turn.get("error"))
        self.assertEqual(turn["rounds"], 8)
        self.assertEqual(len(self.received), 8)
        self.assertTrue(all(p.get("tools") for p in self.received[:7]))
        self.assertFalse(self.received[-1].get("tools"))
        self.assertIn("只作答", self.received[-1]["messages"][-1]["content"])
        self.assertEqual(len([m for m in turn["history"] if m["role"] == "tool"]), 7)
        self.assertEqual(self.app.store.find("task"), [])
        root = self.app.store.get("root_request", turn["accounted_root_request_id"])
        self.assertEqual(root["model_turn_count"], 1)

    def test_legacy_eight_rounds_include_final_answer(self):
        self._assert_finalized(self._run(self._dig, long_history=True))
        self.assertNotIn("msg-copied", str(self.received[0]["messages"]))

    def test_pi_eight_rounds_include_final_answer(self):
        self._assert_finalized(self._run(self._dig, real_pi=True, long_history=True))
        self.assertNotIn("msg-copied", str(self.received[0]["messages"]))

    def test_one_round_budget_can_answer_without_tools(self):
        turn = self._run(lambda p, n: {"content": "需要提供正文。"}, budget=1)
        self.assertEqual(turn["status"], "done")
        self.assertEqual(turn["rounds"], 1)
        self.assertFalse(self.received[0]["tools"])

    def test_legacy_last_round_cannot_execute_unoffered_write(self):
        turn = self._run(lambda p, n: call("create_bot", name="非法新增", role="r"), budget=1)
        self.assertEqual(turn["status"], "blocked")
        self.assertEqual(turn["error"], "turn_budget")
        self.assertEqual(len(self.app.store.find("bot")), 1)

    def test_pi_last_round_cannot_execute_unoffered_write(self):
        turn = self._run(lambda p, n: call("create_bot", name="非法新增", role="r"), budget=1, real_pi=True)
        self.assertEqual(turn["status"], "blocked")
        self.assertEqual(turn["error"], "turn_budget")
        self.assertEqual(len(self.app.store.find("bot")), 1)

    def test_pi_frame_with_tool_is_clean_in_next_model_call_and_storage(self):
        def respond(payload, n):
            if n == 1:
                response = call("read_state")
                response["content"] = framed("这是中间说明。")
                return response
            # Pi's _pi fast replay must also contain only normalized text.
            assistant = next(m for m in reversed(payload["messages"]) if m["role"] == "assistant")
            self.assertEqual(assistant["content"], "这是中间说明。")
            return {"content": framed("这是最终答复。")}
        turn = self._run(respond, real_pi=True)
        self.assertEqual(turn["status"], "done", turn.get("error"))
        messages = assistant_messages(self.app)
        self.assertEqual({m["content"] for m in messages}, {"这是中间说明。", "这是最终答复。"})
        self.assertEqual(turn["reply_frame_unwraps"], 2)
        for message in turn["history"]:
            if message.get("_pi"):
                self.assertNotIn("msg-copied", json.dumps(message))

    def test_legacy_model_frame_delivers_clean_text(self):
        turn = self._run(lambda p, n: {"content": framed("最终正文")})
        self.assertEqual(turn["status"], "done")
        self.assertEqual(assistant_messages(self.app)[0]["content"], "最终正文")


if __name__ == "__main__":
    unittest.main()
