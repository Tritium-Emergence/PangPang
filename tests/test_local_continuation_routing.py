# -*- coding: utf-8 -*-
"""Keyless continuation routing (1.0): without TYPESAFE_API_KEY the user's own
configured model may route a follow-up message to an existing Work
(route_source="local_model"). Every failure degrades to the deterministic
unconfigured_new — NEVER to the weak `fallback` route (whose "attribution
untrusted" semantics would gate delegate/submit_task worse than before).
The local model may choose a Work but may never apply a correction."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class LocalRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev = os.environ.pop("TYPESAFE_API_KEY", None)
        self.app = Service(config(self.root), self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()
        if self._prev is None:
            os.environ.pop("TYPESAFE_API_KEY", None)
        else:
            os.environ["TYPESAFE_API_KEY"] = self._prev

    def _seed(self, text, key):
        return self.app.send("bot-main", {"text": text}, key)["work_id"]

    def _resolve(self, text, hint=None):
        body = {"text": text, "stream_id": "tab-1"}
        if hint:
            body["work_hint_id"] = hint
        return self.app.resolve_continuation("bot-main", body)

    def _fake_pi(self, reply=None, raises=None, calls=None):
        calls = [] if calls is None else calls

        class FakePiModel:
            def __init__(self, service):
                pass

            def complete(self, profile, messages, tools, on_proc=None, cancelled=None):
                calls.append(profile)
                if raises is not None:
                    raise raises
                return {"role": "assistant", "content": reply}, {}

        return patch("pangpang.pi_runtime.PiModel", FakePiModel), calls

    def test_current_choice_continues_hint(self):
        work = self._seed("整理季度财报", "lr-a")
        fake, _ = self._fake_pi('{"choice": "CURRENT"}')
        with fake:
            routing = self._resolve("把毛利率那页再核对一遍", hint=work)
        self.assertEqual(routing["route_source"], "local_model")
        self.assertEqual(routing["work_id"], work)
        self.assertEqual(routing["route_quality"], "confident")

    def test_candidate_choice_maps_to_recent_work(self):
        first = self._seed("写周报", "lr-b1")
        second = self._seed("调研供应商", "lr-b2")
        fake, _ = self._fake_pi('{"choice": "CANDIDATE_2"}')
        with fake:
            routing = self._resolve("供应商那件事继续查")
        self.assertEqual(routing["route_source"], "local_model")
        self.assertEqual(routing["work_id"], first)  # CANDIDATE_2 = second-newest
        self.assertNotEqual(routing["work_id"], second)

    def test_new_choice_starts_fresh_work_confident(self):
        self._seed("写周报", "lr-c")
        fake, _ = self._fake_pi('{"choice": "NEW"}')
        with fake:
            routing = self._resolve("冰箱怎么修")
        self.assertEqual(routing["route_source"], "local_model")
        self.assertIsNone(routing["work_id"])
        self.assertEqual(routing["route_quality"], "confident")

    def test_bad_json_degrades_to_unconfigured_new(self):
        work = self._seed("写周报", "lr-d")
        fake, _ = self._fake_pi("I think CURRENT?")
        with fake:
            routing = self._resolve("继续", hint=work)
        self.assertEqual(routing["route_source"], "unconfigured_new")
        self.assertIsNone(routing["work_id"])

    def test_model_error_degrades_to_unconfigured_new(self):
        work = self._seed("写周报", "lr-e")
        fake, _ = self._fake_pi(raises=Fault("model_request_failed"))
        with fake:
            routing = self._resolve("继续", hint=work)
        self.assertEqual(routing["route_source"], "unconfigured_new")
        self.assertIsNone(routing["work_id"])

    def test_out_of_range_candidate_degrades(self):
        work = self._seed("写周报", "lr-f")
        fake, _ = self._fake_pi('{"choice": "CANDIDATE_9"}')
        with fake:
            routing = self._resolve("继续", hint=work)
        self.assertEqual(routing["route_source"], "unconfigured_new")

    def test_nothing_to_route_never_calls_the_model(self):
        fake, calls = self._fake_pi('{"choice": "NEW"}')
        with fake:
            routing = self._resolve("第一条消息")
        self.assertEqual(routing["route_source"], "unconfigured_new")
        self.assertEqual(calls, [])

    def test_breaker_opens_after_three_failures(self):
        work = self._seed("写周报", "lr-g")
        fake, calls = self._fake_pi("garbage")
        with fake:
            for _ in range(3):
                self.assertEqual(self._resolve("继续", hint=work)["route_source"], "unconfigured_new")
            self.assertEqual(len(calls), 3)
            routing = self._resolve("继续", hint=work)
        self.assertEqual(routing["route_source"], "unconfigured_new")
        self.assertEqual(len(calls), 3)  # 4th attempt skipped by the breaker

    def test_local_model_never_applies_correction(self):
        work = self._seed("写周报", "lr-h")
        fake, _ = self._fake_pi('{"choice": "CURRENT", "correction": true}')
        with fake:
            routing = self._resolve("不对，改成月报", hint=work)
        self.assertEqual(routing["route_source"], "local_model")
        self.assertIsNone(routing["correction_target_message_id"])
        self.assertFalse(routing["proposal"]["correction"])

    def test_configured_jev_takes_precedence_over_local(self):
        work = self._seed("写周报", "lr-i")
        os.environ["TYPESAFE_API_KEY"] = "k"
        try:
            fake, calls = self._fake_pi('{"choice": "CURRENT"}')
            with fake, patch.object(self.app.semantic_gate, "classify_continuation",
                                    return_value={"choice": "NEW_TOPIC", "confidence": 0.9}):
                routing = self._resolve("换个话题", hint=work)
            self.assertEqual(routing["route_source"], "jev")
            self.assertEqual(calls, [])
        finally:
            os.environ.pop("TYPESAFE_API_KEY", None)

    def test_keyless_continuation_survives_without_a_semantic_gate_object(self):
        # Deleting the Jev object must not turn "继续" into a fresh Work.
        self.app.semantic_gate = None
        work = self._seed("整理季度财报", "lr-gate")
        fake, calls = self._fake_pi('{"choice": "CURRENT"}')
        with fake:
            routing = self._resolve("把毛利率那页再核对一遍", hint=work)
        self.assertEqual(len(calls), 1)
        self.assertEqual(routing["route_source"], "local_model")
        self.assertEqual(routing["work_id"], work)
        self.assertIsNone(routing["correction_target_message_id"])

    def test_routing_off_in_config_stays_off_without_a_gate_object(self):
        self.app.config["semantic_gate"] = {"continuation_routing": False}
        self.app.semantic_gate = None
        work = self._seed("写周报", "lr-off")
        fake, calls = self._fake_pi('{"choice": "CURRENT"}')
        with fake:
            routing = self._resolve("继续", hint=work)
        self.assertEqual(calls, [])
        self.assertEqual(routing["route_source"], "unconfigured_new")
        self.assertIsNone(routing["work_id"])

    def test_low_confidence_new_with_focus_stays_current(self):
        work = self._seed("整理季度财报", "lr-hys")
        fake, _ = self._fake_pi('{"choice": "NEW", "confidence": 0.4}')
        with fake:
            routing = self._resolve("这个呢", hint=work)
        self.assertEqual(routing["work_id"], work)
        self.assertEqual(routing["route_quality"], "default_continue")
        self.assertEqual(routing["route_source"], "local_model")

    def test_high_confidence_new_with_focus_starts_a_fresh_work(self):
        work = self._seed("整理季度财报", "lr-new")
        fake, _ = self._fake_pi('{"choice": "NEW", "confidence": 0.96}')
        with fake:
            routing = self._resolve("冰箱怎么修", hint=work)
        self.assertIsNone(routing["work_id"])
        self.assertNotEqual(routing["work_id"], work)
        self.assertEqual(routing["route_quality"], "confident")

    def test_unsure_with_focus_keeps_current(self):
        work = self._seed("整理季度财报", "lr-unsure")
        fake, _ = self._fake_pi('{"choice": "UNSURE"}')
        with fake:
            routing = self._resolve("再看一下", hint=work)
        self.assertEqual(routing["work_id"], work)
        self.assertEqual(routing["route_quality"], "default_continue")
        self.assertFalse(routing["proposal"]["correction"])

    def test_unsure_without_focus_does_not_guess_the_newest_work(self):
        newest = self._seed("最新的工作", "lr-nofocus-new")
        older = self._seed("更早的工作", "lr-nofocus-old")
        fake, _ = self._fake_pi('{"choice": "UNSURE"}')
        with fake:
            routing = self._resolve("帮我看看")
        self.assertIsNone(routing["work_id"])
        self.assertNotIn(routing["work_id"], (newest, older))
        self.assertEqual(routing["route_quality"], "deterministic")
        self.assertNotEqual(routing["route_quality"], "confident")

    def test_low_confidence_candidate_without_focus_does_not_attach(self):
        newest = self._seed("最新的工作", "lr-ask")
        fake, _ = self._fake_pi('{"choice": "CANDIDATE_1", "confidence": 0.2}')
        with fake:
            routing = self._resolve("不太确定是哪件")
        self.assertIsNone(routing["work_id"])
        self.assertNotEqual(routing["work_id"], newest)
        self.assertEqual(routing["route_quality"], "fallback")

    def test_candidate_window_stops_at_seven(self):
        works = [self._seed("工作-%02d" % i, "lr-win-%02d" % i) for i in range(9)]
        fake, _ = self._fake_pi('{"choice": "CURRENT"}')
        with fake:
            routing = self._resolve("继续当前这个", hint=works[-1])
        slots = [row["slot"] for row in routing["candidate_snapshot"]]
        candidates = [row["work_id"] for row in routing["candidate_snapshot"]
                      if str(row["slot"]).startswith("CANDIDATE_")]
        self.assertEqual(slots[0], "CURRENT")
        self.assertEqual(slots[-1], "NEW")
        self.assertEqual(len(candidates), 7)
        self.assertNotIn(works[0], candidates)
        self.assertNotIn(works[-1], candidates)

    def test_busy_model_slot_does_not_start_a_second_call_or_open_the_breaker(self):
        from pangpang.pi_runtime import PiModel
        from pangpang.runtime import Runtime
        self.app.scheduler_config["model_max_concurrency"] = 1
        self.app.LOCAL_ROUTING_TIMEOUT_S = 0.15
        Runtime(self.app)
        started = []

        def impl(model, *args, **kwargs):
            started.append(1)
            raise AssertionError("worker started while the slot was held")

        self.assertTrue(self.app.scheduler.acquire_model_call(timeout_s=0))
        work = self._seed("写周报", "lr-busy")
        try:
            with patch.object(PiModel, "_exchange_impl", impl):
                routing = self._resolve("继续", hint=work)
        finally:
            self.app.scheduler.release_model_call()
        self.assertEqual(started, [])
        self.assertIsNone(routing["work_id"])
        self.assertEqual(routing["route_source"], "unconfigured_new")
        self.assertEqual(getattr(self.app, "_local_routing_failures", 0), 0)

    def test_same_thread_model_call_reuses_the_slot_it_holds(self):
        from pangpang.runtime import Runtime
        self.app.scheduler_config["model_max_concurrency"] = 1
        Runtime(self.app)
        sched = self.app.scheduler
        self.assertTrue(sched.acquire_model_call(timeout_s=0))
        try:
            self.assertTrue(sched.acquire_model_call(timeout_s=0))
            self.assertEqual(sched._model_call_inflight, 1)
            sched.release_model_call()
            self.assertEqual(sched._model_call_inflight, 1)
        finally:
            sched.release_model_call()
        self.assertEqual(sched._model_call_inflight, 0)

    def test_nested_hold_keeps_its_count_beside_another_caller(self):
        import threading
        from pangpang.runtime import Runtime
        self.app.scheduler_config["model_max_concurrency"] = 2
        Runtime(self.app)
        sched = self.app.scheduler
        held = threading.Event()
        drop_inner = threading.Event()
        inner_dropped = threading.Event()
        drop_outer = threading.Event()

        def owner():
            self.assertTrue(sched.acquire_model_call(timeout_s=1))
            self.assertTrue(sched.acquire_model_call(timeout_s=0))
            held.set()
            self.assertTrue(drop_inner.wait(2))
            sched.release_model_call()
            inner_dropped.set()
            self.assertTrue(drop_outer.wait(2))
            sched.release_model_call()

        thread = threading.Thread(target=owner)
        thread.start()
        self.assertTrue(held.wait(2))
        self.assertEqual(sched._model_call_inflight, 1)
        self.assertTrue(sched.acquire_model_call(timeout_s=1))
        self.assertEqual(sched._model_call_inflight, 2)

        def try_once(box, timeout_s):
            box.append(sched.acquire_model_call(timeout_s=timeout_s))

        blocked = []
        extra = threading.Thread(target=try_once, args=(blocked, 0.05))
        extra.start()
        extra.join(2)
        self.assertEqual(blocked, [False])
        self.assertEqual(sched._model_call_inflight, 2)
        drop_inner.set()
        self.assertTrue(inner_dropped.wait(2))
        # The nested release must not free the outer hold.
        self.assertEqual(sched._model_call_inflight, 2)
        still_blocked = []
        extra = threading.Thread(target=try_once, args=(still_blocked, 0.05))
        extra.start()
        extra.join(2)
        self.assertEqual(still_blocked, [False])
        drop_outer.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sched._model_call_inflight, 1)
        sched.release_model_call()
        self.assertEqual(sched._model_call_inflight, 0)
        self.assertEqual(sched._model_call_depth, {})

    def test_pi_model_run_and_routing_share_one_slot(self):
        import threading
        import time
        from pangpang.pi_runtime import PiModel
        from pangpang.runtime import Runtime
        from pangpang.store import Fault
        self.app.scheduler_config["model_max_concurrency"] = 1
        runtime = Runtime(self.app)
        state = {"current": 0, "peak": 0, "slot": 0}
        lock = threading.Lock()
        samples = []

        def impl(model, profile, history, tools, handler, *args, **kwargs):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
                state["slot"] = max(state["slot"], self.app.scheduler._model_call_inflight)
            time.sleep(0.03)
            with lock:
                state["current"] -= 1
            handler({"type": "assistant", "stop": "stop",
                     "message": {"role": "assistant", "content": '{"choice": "CURRENT"}'},
                     "usage": {}})
            return {"type": "finished", "answer": "supervisor"}

        sent = self._seed("整理季度财报", "lr-slot")
        turn = self.app.store.get("turn", self.app.store.find(
            "turn", work_id=sent, newest=True, limit=1)[0]["id"])
        profile = next(iter(self.app.profiles.values()))

        def supervisor():
            try:
                PiModel(self.app).run(runtime, turn, profile)
            except Fault:
                pass

        with patch.object(PiModel, "_exchange_impl", impl):
            for _ in range(5):
                thread = threading.Thread(target=supervisor)
                started = time.perf_counter()
                thread.start()
                time.sleep(0.005)
                routing = self._resolve("继续核对", hint=sent)
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                samples.append((time.perf_counter() - started) * 1000)
                self.assertEqual(routing["work_id"], sent)
                self.assertEqual(routing["route_source"], "local_model")
        self.assertEqual(state["peak"], 1)
        self.assertEqual(state["slot"], 1)
        samples.sort()
        p50 = samples[len(samples) // 2]
        p95 = samples[max(0, int(round(0.95 * len(samples))) - 1)]
        print("model-slot wait ms p50=%.1f p95=%.1f samples=%s" % (
            p50, p95, [round(v, 1) for v in samples]))
        self.assertGreaterEqual(p50, 20)
        self.assertLess(p95, 500)

    def test_continuation_router_matches_the_classifier_that_runs(self):
        # No key, and the test profile has an endpoint and model name.
        self.assertEqual(self.app.continuation_router(), "local_model")
        caps = self.app.diagnostics()["capabilities"]
        self.assertEqual(caps["continuation_router"], "local_model")
        self.assertTrue(caps["continuation_routing"])
        detail = next(row for row in self.app.diagnostics()["effective_capabilities"]
                      if row["capability"] == "semantic_gate")["detail"]
        self.assertEqual(detail["continuation_router"], "local_model")
        self.assertIn("configured local model", "\n".join(self.app.capability_warnings()))
        work = self._seed("写周报", "router-local")
        fake, calls = self._fake_pi('{"choice": "CURRENT"}')
        with fake:
            routing = self._resolve("继续", hint=work)
        self.assertEqual(calls, [next(iter(self.app.profiles.values()))])
        self.assertEqual(routing["route_source"], "local_model")
        self.assertEqual(self.app.continuation_router(), "local_model")

        # A key selects Jev even when a local model is also configured.
        os.environ["TYPESAFE_API_KEY"] = "k"
        try:
            self.assertEqual(self.app.continuation_router(), "jev")
            self.assertEqual(self.app.diagnostics()["capabilities"]["continuation_router"], "jev")
            self.assertTrue(self.app.capabilities()["continuation_routing"])
            self.assertNotIn("configured local model", "\n".join(self.app.capability_warnings()))
            with fake, patch.object(
                    self.app.semantic_gate, "classify_continuation",
                    return_value={"choice": "CONTINUE_CURRENT", "confidence": 0.9}):
                routed = self._resolve("继续核对", hint=work)
            self.assertEqual(routed["route_source"], "jev")
            self.assertEqual(calls, [next(iter(self.app.profiles.values()))])
        finally:
            os.environ.pop("TYPESAFE_API_KEY", None)

        # The switch off stays off with a key and a local model.
        self.app.semantic_gate.continuation_routing = False
        os.environ["TYPESAFE_API_KEY"] = "k"
        try:
            self.assertEqual(self.app.continuation_router(), "off")
            self.assertFalse(self.app.capabilities()["continuation_routing"])
            with fake, patch.object(
                    self.app.semantic_gate, "classify_continuation",
                    return_value={"choice": "CONTINUE_CURRENT", "confidence": 0.9}) as jev:
                parked = self._resolve("继续", hint=work)
            self.assertEqual(jev.call_count, 0)
            self.assertEqual(parked["route_source"], "unconfigured_new")
        finally:
            os.environ.pop("TYPESAFE_API_KEY", None)
            self.app.semantic_gate.continuation_routing = True
        self.assertEqual(len(calls), 1)

        # A key with no Jev object stays on the local model. The key alone does
        # not select Jev; resolve_continuation only calls Jev when the gate object
        # is present and continuation_configured.
        saved_gate = self.app.semantic_gate
        self.app.semantic_gate = None
        os.environ["TYPESAFE_API_KEY"] = "k"
        try:
            self.assertEqual(self.app.continuation_router(), "local_model")
        finally:
            os.environ.pop("TYPESAFE_API_KEY", None)
            self.app.semantic_gate = saved_gate

        # No configured local model: the field is off, and the real call does not
        # report route_source=local_model.
        for profile in self.app.profiles.values():
            profile["endpoint"] = ""
            profile["model"] = ""
        self.assertEqual(self.app.continuation_router(), "off")
        self.assertFalse(self.app.capabilities()["continuation_routing"])
        self.assertEqual(self.app.diagnostics()["capabilities"]["continuation_router"], "off")
        warnings = "\n".join(self.app.capability_warnings())
        self.assertNotIn("configured local model", warnings)
        self.assertIn("continuation routing is off", warnings)
        bare = self._resolve("继续", hint=work)
        self.assertEqual(bare["route_source"], "unconfigured_new")
        self.assertEqual(self.app.continuation_router(), "off")

        # No profile at all is off, even with a key and no Jev object.
        self.app.profiles = {}
        self.app.semantic_gate = None
        os.environ["TYPESAFE_API_KEY"] = "k"
        try:
            self.assertEqual(self.app.continuation_router(), "off")
        finally:
            os.environ.pop("TYPESAFE_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
