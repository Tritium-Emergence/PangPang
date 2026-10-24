import json
import os
import tempfile
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang.context import projection
from pangpang.service import Service
from pangpang.store import Fault, uid


class WorkContinuityV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = config(self.root)
        # Plan A: continuation routing is decoupled from the pre-commit gate.
        # mode stays OFF (the shipped default) yet routing is active because a key
        # is configured. classify_continuation is stubbed, so no network happens;
        # the key only flips continuation_configured true.
        cfg["semantic_gate"] = {
            "mode": "off",
            "continuation_routing": True,
            "model": "jev-latest",
            "timeout_s": 0.2,
            "continuation_switch_threshold": 0.75,
            "correction_threshold": 0.85,
        }
        self._prev_key = os.environ.get("TYPESAFE_API_KEY")
        os.environ["TYPESAFE_API_KEY"] = "test-key"
        self.app = Service(cfg, self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()
        if self._prev_key is None:
            os.environ.pop("TYPESAFE_API_KEY", None)
        else:
            os.environ["TYPESAFE_API_KEY"] = self._prev_key

    def _route(self, verdict):
        self.app.semantic_gate.classify_continuation = lambda *args, **kwargs: verdict

    def test_high_confidence_new_topic_breaks_sticky_work(self):
        first = self.app.send("bot-main", {"text": "阿里股价"}, "sticky-a")
        self._route({"choice": "NEW_TOPIC", "confidence":.96, "probabilities": {}})
        body = {"text": "冰箱怎么修", "work_hint_id": first["work_id"],
                "stream_id": "stream-a"}
        routing = self.app.resolve_continuation("bot-main", body)
        self.assertIsNone(routing["work_id"])
        self.assertEqual(routing["route_quality"], "confident")
        second = self.app.send("bot-main", body, "sticky-b", routing=routing)
        self.assertNotEqual(second["work_id"], first["work_id"])

    def test_low_confidence_switch_uses_hysteresis(self):
        first = self.app.send("bot-main", {"text": "阿里股价"}, "hys-a")
        self._route({"choice": "NEW_TOPIC", "confidence":.55, "probabilities": {}})
        body = {"text": "这个呢", "work_hint_id": first["work_id"],
                "stream_id": "stream-hys"}
        routing = self.app.resolve_continuation("bot-main", body)
        self.assertEqual(routing["work_id"], first["work_id"])
        self.assertEqual(routing["route_quality"], "default_continue")

    def test_low_confidence_new_topic_without_hint_is_confident_not_fallback(self):
        # A brand-new conversation (no work_hint_id) that Jev calls NEW_TOPIC with
        # only middling confidence must still start a fresh Work AND be `confident`,
        # not `fallback`. picked=None means there is no existing Work to
        # mis-attribute to, so it is attribution-safe; marking it fallback wrongly
        # gated the FIRST consequential action (delegate / submit_task) of a new
        # conversation behind route_confirmation_required.
        self._route({"choice": "NEW_TOPIC", "confidence":.40, "probabilities": {}})
        routing = self.app.resolve_continuation(
            "bot-main", {"text": "让秦仲海做一份分析", "stream_id": "stream-new"})
        self.assertIsNone(routing["work_id"]) # fresh Work
        self.assertEqual(routing["route_quality"], "confident") # not fallback
        # And a consequential tool is NOT gated: the turn carries a trusted route.
        self.assertNotEqual(routing["route_quality"], "fallback")

    def test_gate_failure_degrades_to_fresh_work_and_invalid_hint_is_not_authority(self):
        first = self.app.send("bot-main", {"text": "阿里股价"}, "fb-a")
        self.app.semantic_gate.classify_continuation = lambda *a, **k: None
        valid = self.app.resolve_continuation("bot-main", {
            "text": "继续", "work_hint_id": first["work_id"], "stream_id": "stream-fb"})
        # A failed classifier is an outage, not an attribution dispute:
        # deterministic fresh Work, not a mutation freeze on the hinted Work.
        self.assertIsNone(valid["work_id"])
        self.assertEqual(valid["route_source"], "degraded_new")
        self.assertEqual(valid["route_quality"], "deterministic")

        invalid = self.app.resolve_continuation("bot-main", {
            "text": "继续", "work_hint_id": "work-does-not-exist", "stream_id": "stream-fb"})
        self.assertIsNone(invalid["work_id"])
        self.assertIsNone(invalid["work_hint_id"])

    def test_stream_boundary_prevents_other_tab_from_becoming_correction_lineage(self):
        a = self.app.send("bot-main", {"text": "A", "stream_id": "tab-a"}, "stream-a")
        b = self.app.send("bot-main", {"text": "B", "stream_id": "tab-b"}, "stream-b")
        # tab-a says its last rendered event is from tab-b: strict adjacency is absent.
        stream_id, last_seen, previous, _ = self.app._stream_context("bot-main", {
            "stream_id": "tab-a", "last_seen_event_id": b["message_id"]})
        self.assertEqual(stream_id, "tab-a")
        self.assertIsNone(last_seen)
        # We may still know tab-a's older user turn for ordinary context, but it
        # cannot be an adjacency-backed correction target without last_seen.
        self.assertEqual(previous["id"], a["message_id"])

    def test_explicit_correction_is_append_only_and_repairs_effective_context(self):
        # Work A exists.
        a = self.app.send("bot-main", {"text": "阿里估值", "stream_id": "tab-old"}, "corr-a")
        ta = self.app.store.get("turn", a["turn_id"])
        self.app.finish_turn(ta, "阿里上下文")

        # U1 is wrongly routed into Work B and gets a wrong answer.
        b = self.app.send("bot-main", {"text": "冰箱维修", "stream_id": "tab-corr"}, "corr-b")
        tb = self.app.store.get("turn", b["turn_id"])
        self.app.finish_turn(tb, "冰箱上下文")
        u1 = self.app.send("bot-main", {
            "text": "这个涨了多少", "work_id": b["work_id"], "stream_id": "tab-corr"
        }, "corr-u1")
        t1 = self.app.store.get("turn", u1["turn_id"])
        self.app.finish_turn(t1, "压缩机价格上涨了")
        wrong_answer = self.app.store.get("state", "bot-main")["last_delivery_ref"]

        self._route({"choice": "CONTINUE_OTHER", "candidate_work_id": a["work_id"],
                     "confidence":.96, "probabilities": {},
                     "correction": True, "correction_confidence":.96,
                     "correction_probabilities": {}})
        body = {"text": "不是，我说的是刚才阿里那个", "work_hint_id": b["work_id"],
                "stream_id": "tab-corr", "last_seen_event_id": wrong_answer}
        routing = self.app.resolve_continuation("bot-main", body)
        self.assertEqual(routing["work_id"], a["work_id"])
        # A classifier may name a Work. It does not write a correction.
        self.assertIsNone(routing["correction_target_message_id"])
        corrected = self.app.send("bot-main", body, "corr-u2", routing=routing)
        self.assertIsNone(corrected["routing_correction_id"])
        self.assertEqual(self.app.store.find("routing_correction", limit=10), [])
        self.assertEqual(self.app.store.get("message", u1["message_id"])["work_id"], b["work_id"])

    def test_weak_route_write_task_is_upgraded_to_approval(self):
        self.app.probe("codex-local")
        body = {"text": "执行修改", "stream_id": "tab-weak"}
        routing = {"work_id": None, "work_hint_id": None, "route_source": "fallback",
                   "route_quality": "fallback", "proposal": None,
                   "stream_id": "tab-weak", "last_seen_event_id": None,
                   "correction_target_message_id": None,
                   "correction_superseded_message_ids": []}
        sent = self.app.send("bot-main", body, "weak-route", routing=routing)
        turn = self.app.store.get("turn", sent["turn_id"])
        self.assertEqual(turn["route_quality"], "fallback")
        submitted = self.app.submit_task(
            "bot-main", sent["work_id"], {"prompt": "修改文件"},
            root_request_id=turn.get("accounted_root_request_id"),
            route_quality=turn.get("route_quality"))
        task = self.app.store.get("task", submitted["task_id"])
        self.assertEqual(task["status"], "awaiting_approval")

    def test_default_continue_route_does_not_gate_task_behind_approval(self):
        # A trusted CURRENT continuation (Jev ran, kept CURRENT, only switch
        # confidence was mid) must NOT force approval — a user's own follow-up
        # task ("用 legacy-executor 去查一下" mid-thread) should run directly. Only a
        # real `fallback` (untrusted attribution) gates. grant_write stays True.
        self.app.probe("codex-local")
        body = {"text": "执行修改", "stream_id": "tab-dc"}
        routing = {"work_id": None, "work_hint_id": None, "route_source": "jev",
                   "route_quality": "default_continue", "proposal": None,
                   "stream_id": "tab-dc", "last_seen_event_id": None,
                   "correction_target_message_id": None,
                   "correction_superseded_message_ids": []}
        sent = self.app.send("bot-main", body, "dc-route", routing=routing)
        turn = self.app.store.get("turn", sent["turn_id"])
        self.assertEqual(turn["route_quality"], "default_continue")
        submitted = self.app.submit_task(
            "bot-main", sent["work_id"], {"prompt": "修改文件"},
            root_request_id=turn.get("accounted_root_request_id"),
            route_quality=turn.get("route_quality"))
        task = self.app.store.get("task", submitted["task_id"])
        self.assertEqual(task["status"], "queued")

    def test_checkpoint_work_prose_is_interim_deliverable(self):
        # A turn that emits formatted result prose alongside checkpoint_work must be
        # deliverable: checkpoint_work only records the bot's own Work state, no
        # external side effect. Regression for "胖胖 formatted the SPCX price table
        # but it was dropped because it shipped in the same message as checkpoint_work".
        from pangpang.runtime import Runtime
        rt = Runtime(self.app)
        call = lambda name: [{"function": {"name": name}}]
        self.assertTrue(rt.interim_deliverable(call("checkpoint_work")))
        self.assertTrue(rt.interim_deliverable(call("web_search")))
        # Genuinely consequential tools still block interim delivery.
        self.assertFalse(rt.interim_deliverable(call("submit_task")))
        self.assertFalse(rt.interim_deliverable(call("send_bot_message")))
        # Mixed batch: any real side-effect tool present blocks it.
        self.assertFalse(rt.interim_deliverable(
            [{"function": {"name": "checkpoint_work"}},
             {"function": {"name": "submit_task"}}]))

    def test_bounded_projection_marks_history_gap(self):
        sent = self.app.send("bot-main", {"text": "current"}, "gap-current")
        incoming = self.app.store.get("message", sent["message_id"])
        for i in range(35):
            self.app.store.put("message", {
                "id": uid("msg"), "bot_id": "bot-main", "work_id": sent["work_id"],
                "kind": "user", "from": "user", "content": "old-%02d" % i,
                "created": incoming["created"] - 100 + i,
            })
        turn = self.app.store.get("turn", sent["turn_id"])
        system = projection(self.app, turn)[0]["content"]
        packet = json.loads(system[system.index("{"):])
        self.assertTrue(packet["history_gap"]["active_projection_incomplete"])

    def test_routing_active_with_gate_mode_off_and_reverts_when_flag_disabled(self):
        # Core Plan A contract. Gate mode is OFF (default), yet a high-confidence
        # NEW_TOPIC still breaks the sticky Work: routing no longer rides on the
        # pre-commit gate's mode.
        self.assertEqual(self.app.semantic_gate.mode, "off")
        self.assertTrue(self.app.semantic_gate.continuation_configured)
        first = self.app.send("bot-main", {"text": "阿里股价"}, "decouple-a")
        self._route({"choice": "NEW_TOPIC", "confidence":.96, "probabilities": {}})
        body = {"text": "冰箱怎么修", "work_hint_id": first["work_id"],
                "stream_id": "stream-decouple"}
        routing = self.app.resolve_continuation("bot-main", body)
        self.assertIsNone(routing["work_id"])
        self.assertEqual(routing["route_quality"], "confident")

        # Without routing, a tab hint cannot establish continuation. Create NEW
        # deterministically, with normal authority for that new request.
        self.app.semantic_gate.continuation_routing = False
        det = self.app.resolve_continuation("bot-main", body)
        self.assertIsNone(det["work_id"])
        self.assertEqual(det["route_quality"], "deterministic")

    def test_missing_key_creates_new_work_instead_of_trusting_hint(self):
        # No key = unconfigured, not an outage. Must NOT weaken the route to
        # fallback (which would force writes into approval and starve execution).
        os.environ.pop("TYPESAFE_API_KEY", None)
        self.assertFalse(self.app.semantic_gate.continuation_configured)
        first = self.app.send("bot-main", {"text": "阿里股价"}, "nokey-a")
        routing = self.app.resolve_continuation("bot-main", {
            "text": "继续", "work_hint_id": first["work_id"], "stream_id": "stream-nokey"})
        self.assertIsNone(routing["work_id"])
        self.assertEqual(routing["route_quality"], "deterministic")

    def _user_move(self, target_work_id, stream_id, last_seen, text="这条属于那件工作"):
        return {
            "text": text,
            "correct_to_work_id": target_work_id,
            "stream_id": stream_id,
            "last_seen_event_id": last_seen,
        }

    def test_user_correction_is_append_only_and_does_not_call_the_model(self):
        calls = []
        self.app.semantic_gate.classify_continuation = lambda *args, **kwargs: calls.append(1)
        target = self.app.send("bot-main", {"text": "阿里估值", "stream_id": "tab-user"}, "user-a")
        self.app.finish_turn(self.app.store.get("turn", target["turn_id"]), "阿里上下文")
        wrong = self.app.send("bot-main", {"text": "这个涨了多少", "stream_id": "tab-user"}, "user-b")
        self.app.finish_turn(self.app.store.get("turn", wrong["turn_id"]), "压缩机价格上涨了")
        answer = self.app.store.get("state", "bot-main")["last_delivery_ref"]
        kept = self.app.store.get("work", target["work_id"])
        kept["grant_write"] = False
        self.app.store.put("work", kept)
        body = self._user_move(target["work_id"], "tab-user", answer)
        routing = self.app.resolve_continuation("bot-main", body)
        self.assertEqual(calls, [])
        self.assertEqual(routing["route_source"], "user_correction")
        self.assertEqual(routing["route_quality"], "deterministic")
        self.assertEqual(routing["work_id"], target["work_id"])
        self.assertEqual(routing["correction_target_message_id"], wrong["message_id"])
        corrected = self.app.send("bot-main", body, "user-c", routing=routing)
        self.assertIsNotNone(corrected["routing_correction_id"])
        self.assertEqual(self.app.store.get("message", wrong["message_id"])["work_id"], wrong["work_id"])
        self.assertFalse(self.app.store.get("work", target["work_id"])["grant_write"])
        payload = json.dumps(projection(self.app, self.app.store.get("turn", corrected["turn_id"])),
                             ensure_ascii=False)
        self.assertIn("这个涨了多少", payload)
        self.assertNotIn("压缩机价格上涨了", payload)

    def test_user_correction_refuses_a_turn_that_is_still_queued_or_running(self):
        target = self.app.send("bot-main", {"text": "阿里估值", "stream_id": "tab-live"}, "live-a")
        self.app.finish_turn(self.app.store.get("turn", target["turn_id"]), "阿里上下文")
        for status in ("queued", "running"):
            wrong = self.app.send("bot-main", {"text": "这个涨了多少", "stream_id": "tab-live"}, "live-" + status)
            turn = self.app.store.get("turn", wrong["turn_id"])
            turn["status"] = status
            self.app.store.put("turn", turn)
            before = len(self.app.store.find("routing_correction", limit=20))
            messages = len(self.app.store.find("message", bot_id="bot-main", limit=1000))
            body = self._user_move(target["work_id"], "tab-live", wrong["message_id"])
            with self.assertRaises(Fault) as caught:
                self.app.resolve_continuation("bot-main", body)
            self.assertEqual(caught.exception.code, "routing_correction_rejected")
            self.assertEqual(len(self.app.store.find("routing_correction", limit=20)), before)
            self.assertEqual(len(self.app.store.find("message", bot_id="bot-main", limit=1000)), messages)
            self.assertEqual(self.app.store.get("turn", wrong["turn_id"])["status"], status)
            self.app.finish_turn(self.app.store.get("turn", wrong["turn_id"]), "先结束")

    def test_send_refuses_a_correction_once_the_old_turn_is_running(self):
        target = self.app.send("bot-main", {"text": "阿里估值", "stream_id": "tab-race"}, "race-a")
        self.app.finish_turn(self.app.store.get("turn", target["turn_id"]), "阿里上下文")
        wrong = self.app.send("bot-main", {"text": "这个涨了多少", "stream_id": "tab-race"}, "race-b")
        self.app.finish_turn(self.app.store.get("turn", wrong["turn_id"]), "压缩机")
        answer = self.app.store.get("state", "bot-main")["last_delivery_ref"]
        body = self._user_move(target["work_id"], "tab-race", answer)
        routing = self.app.resolve_continuation("bot-main", body)
        self.assertEqual(routing["correction_target_message_id"], wrong["message_id"])
        turn = self.app.store.get("turn", wrong["turn_id"])
        turn["status"] = "running"
        self.app.store.put("turn", turn)
        before = len(self.app.store.find("message", bot_id="bot-main", limit=1000))
        with self.assertRaises(Fault) as caught:
            self.app.send("bot-main", body, "race-c", routing=routing)
        self.assertEqual(caught.exception.code, "routing_correction_rejected")
        self.assertEqual(len(self.app.store.find("message", bot_id="bot-main", limit=1000)), before)
        self.assertEqual(self.app.store.find("routing_correction", limit=10), [])
        self.assertEqual(self.app.store.get("turn", wrong["turn_id"])["status"], "running")

    def test_user_correction_refuses_a_turn_that_already_dispatched_a_tool(self):
        calls = []
        self.app.semantic_gate.classify_continuation = lambda *args, **kwargs: calls.append(1)
        target = self.app.send("bot-main", {"text": "阿里估值", "stream_id": "tab-side"}, "side-a")
        wrong = self.app.send("bot-main", {"text": "这个涨了多少", "stream_id": "tab-side"}, "side-b")
        turn = self.app.store.get("turn", wrong["turn_id"])
        turn["history"] = [{"role": "assistant", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "submit_task", "arguments": "{}"}}]}]
        self.app.store.put("turn", turn)
        before = len(self.app.store.find("routing_correction", limit=20))
        body = self._user_move(target["work_id"], "tab-side", wrong["message_id"])
        with self.assertRaises(Fault) as caught:
            self.app.resolve_continuation("bot-main", body)
        self.assertEqual(caught.exception.code, "routing_correction_rejected")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(calls, [])
        self.assertEqual(len(self.app.store.find("routing_correction", limit=20)), before)
        self.assertEqual(self.app.store.get("message", wrong["message_id"])["work_id"], wrong["work_id"])

    def test_user_correction_refuses_a_different_stream(self):
        target = self.app.send("bot-main", {"text": "阿里估值", "stream_id": "tab-a"}, "stream-move-a")
        wrong = self.app.send("bot-main", {"text": "冰箱", "stream_id": "tab-b"}, "stream-move-b")
        body = self._user_move(target["work_id"], "tab-a", wrong["message_id"])
        with self.assertRaises(Fault) as caught:
            self.app.resolve_continuation("bot-main", body)
        self.assertEqual(caught.exception.code, "routing_correction_rejected")

    def test_user_correction_of_a_delivered_work_forks_instead_of_reviving(self):
        delivered = self.app.send("bot-main", {"text": "阿里估值", "stream_id": "tab-fork"}, "fork-a")
        work = self.app.store.get("work", delivered["work_id"])
        work["grant_write"] = False
        work["status"] = "delivered"
        work["constraints"] = ["必须保留的硬约束"]
        self.app.store.put("work", work)
        wrong = self.app.send("bot-main", {"text": "冰箱", "stream_id": "tab-fork"}, "fork-b")
        self.app.finish_turn(self.app.store.get("turn", wrong["turn_id"]), "冰箱上下文")
        body = self._user_move(delivered["work_id"], "tab-fork", wrong["message_id"])
        routing = self.app.resolve_continuation("bot-main", body)
        sent = self.app.send("bot-main", body, "fork-c", routing=routing)
        self.assertNotEqual(sent["work_id"], delivered["work_id"])
        fork = self.app.store.get("work", sent["work_id"])
        self.assertEqual(fork["continued_from"], delivered["work_id"])
        self.assertEqual(fork["constraints"], ["必须保留的硬约束"])
        self.assertEqual(self.app.store.get("work", delivered["work_id"])["status"], "delivered")
        self.assertFalse(self.app.store.get("work", delivered["work_id"])["grant_write"])

    def test_explicit_continue_is_not_rewritten_by_a_local_or_jev_choice(self):
        calls = []
        self.app.semantic_gate.classify_continuation = (
            lambda *args, **kwargs: calls.append("jev") or {"choice": "NEW_TOPIC", "confidence": 0.99})
        first = self.app.send("bot-main", {"text": "阿里估值"}, "ex-model")
        body = {"text": "冰箱怎么修", "continue_work_id": first["work_id"], "stream_id": "tab-ex"}
        routing = self.app.resolve_continuation("bot-main", body)
        self.assertEqual(calls, [])
        self.assertEqual(routing["route_source"], "explicit")
        self.assertEqual(routing["work_id"], first["work_id"])


if __name__ == "__main__":
    unittest.main()
