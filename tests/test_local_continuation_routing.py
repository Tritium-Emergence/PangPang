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
        if self._prev is not None:
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


if __name__ == "__main__":
    unittest.main()
