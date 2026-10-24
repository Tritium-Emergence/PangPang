import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.service import Service


def http_error(code=451):
    return urllib.error.HTTPError("https://api.typesafe.ai/v1/systemone", code,
                                  "Unavailable", {}, io.BytesIO(b""))


def ok_response(choice="NEW"):
    body = json.dumps({"answers": {"continuation": {"choice": choice, "confidence": 0.9}}}).encode()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *_args):
            return body
    return _Response()


def ok_commit():
    body = json.dumps({"answers": {"commit_disposition": {"choice": "EXECUTE", "confidence": 0.9}}}).encode()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *_args):
            return body
    return _Response()


class _Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class JevBreakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = config(self.root)
        cfg["semantic_gate"] = {"mode": "off", "continuation_routing": True,
                                "continuation_breaker_failures": 3,
                                "continuation_breaker_cooldown_s": 30}
        self.app = Service(cfg, self.root / "data")
        self.gate = self.app.semantic_gate
        self.key = patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        self.key.start()

    def tearDown(self):
        self.key.stop()
        self.app.store.close()
        self.tmp.cleanup()

    def _drive(self, opener):
        with patch("urllib.request.build_opener", return_value=opener):
            return self.gate.classify_continuation("继续旧项目", None, [], None)

    def test_endpoint_failures_open_breaker(self):
        opener = _Opener([http_error()] * 3)
        for _ in range(3):
            self.assertIsNone(self._drive(opener))
        self.assertTrue(self.gate.continuation_breaker_open)
        self.assertIsNone(self._drive(opener))
        self.assertEqual(opener.calls, 3)

    def test_content_errors_never_open_breaker(self):
        opener = _Opener([ValueError("schema drift")] * 10)
        for _ in range(10):
            self.assertIsNone(self._drive(opener))
        self.assertFalse(self.gate.continuation_breaker_open)

    def test_success_after_recovery_closes_breaker(self):
        opener = _Opener([http_error()] * 3)
        for _ in range(3):
            self._drive(opener)
        self.assertTrue(self.gate.continuation_breaker_open)
        self.gate._continuation_open_until = time.monotonic() - 1
        self.assertIsNotNone(self._drive(_Opener([ok_response()])))
        self.assertFalse(self.gate.continuation_breaker_open)

    def test_steady_451_reopens_breaker_on_first_post_cooldown_failure(self):
        opener = _Opener([http_error()] * 10)
        for _ in range(3):
            self._drive(opener)
        self.assertTrue(self.gate.continuation_breaker_open)
        self.gate._continuation_open_until = time.monotonic() - 1
        self.assertIsNone(self._drive(opener))
        self.assertTrue(self.gate.continuation_breaker_open)
        self.assertEqual(opener.calls, 4)

    def test_sparse_451_traffic_degrades_deterministically_from_the_first_failure(self):
        opener = _Opener([http_error()] * 30)
        qualities = []
        with patch("urllib.request.build_opener", return_value=opener):
            for _ in range(6):
                self.gate._continuation_open_until = time.monotonic() - 1
                routing = self.app.resolve_continuation("bot-main", {"text": "继续旧项目"})
                qualities.append(routing["route_quality"])
        # A failed classification is an outage, not an attribution dispute:
        # every message routes as deterministic fresh Work — never gated.
        self.assertEqual(qualities, ["deterministic"] * 6)

    def test_breaker_open_routes_deterministic_fresh_work(self):
        self.gate._continuation_open_until = time.monotonic() + 30
        with patch("urllib.request.build_opener") as build:
            routing = self.app.resolve_continuation("bot-main", {"text": "继续旧项目"})
        build.assert_not_called()
        self.assertIsNone(routing["work_id"])
        self.assertEqual(routing["route_source"], "degraded_new")
        self.assertEqual(routing["route_quality"], "deterministic")

    def test_single_failure_before_threshold_degrades_to_deterministic_new_work(self):
        opener = _Opener([http_error()])
        self.assertIsNone(self._drive(opener))
        self.assertFalse(self.gate.continuation_breaker_open)
        with patch("urllib.request.build_opener", return_value=_Opener([http_error()])):
            routing = self.app.resolve_continuation("bot-main", {"text": "继续旧项目"})
        # A failure is an outage: degrade to the same deterministic fresh Work
        # the open breaker uses instead of freezing consequential tools.
        self.assertEqual(routing["route_source"], "degraded_new")
        self.assertEqual(routing["route_quality"], "deterministic")
        self.assertIsNone(routing["work_id"])

    def test_content_error_degrades_to_deterministic_new_work(self):
        self.assertIsNone(self._drive(_Opener([ValueError("schema drift")])))
        self.assertFalse(self.gate.continuation_breaker_open)
        with patch("urllib.request.build_opener", return_value=_Opener([ValueError("schema drift")])):
            routing = self.app.resolve_continuation("bot-main", {"text": "继续旧项目"})
        # Schema drift is a broken classifier, not an attribution dispute —
        # the weak `fallback` it used to produce was a permanent freeze.
        self.assertEqual(routing["route_source"], "degraded_new")
        self.assertEqual(routing["route_quality"], "deterministic")

    def test_breaker_open_does_not_bypass_precommit_judge(self):
        from pangpang.semantic_gate import GateUnavailable
        self.gate._continuation_open_until = time.monotonic() + 30
        with patch("urllib.request.build_opener", return_value=_Opener([ok_commit()])):
            verdict = self.gate.judge({"turn": "state"})
        self.assertEqual(verdict["disposition"], "EXECUTE")
        with patch("urllib.request.build_opener", return_value=_Opener([http_error()])):
            with self.assertRaises(GateUnavailable):
                self.gate.judge({"turn": "state"})

    def _semantic_gate_row(self):
        return {row["capability"]: row
                for row in self.app.effective_capabilities()}["semantic_gate"]

    def test_self_check_distinguishes_healthy_and_degraded(self):
        self.assertEqual(self._semantic_gate_row()["state"], "ready")
        self.gate._continuation_open_until = time.monotonic() + 30
        row = self._semantic_gate_row()
        self.assertEqual(row["state"], "degraded")
        self.assertEqual(row["reason"], "jev_endpoint_breaker_open")
        self.assertNotIn("test-key", json.dumps(row))

    def test_enforce_and_shadow_are_not_rewritten_to_off(self):
        # Removing continuation from the Jev object must not fail-open the
        # pre-commit gate or the task-result gate. Those modes still mean
        # what they meant: enforce stays enforce.
        for mode in ("enforce", "shadow"):
            cfg = config(self.root)
            cfg["semantic_gate"] = {"mode": mode, "continuation_routing": True}
            cfg["task_result_gate"] = {"mode": mode}
            app = Service(cfg, self.root / ("gate-" + mode))
            try:
                self.assertEqual(app.semantic_gate.mode, mode)
                self.assertTrue(app.semantic_gate.is_guarded("submit_task"))
                self.assertTrue(app.semantic_gate.is_guarded("delegate"))
                self.assertTrue(app.semantic_gate.is_guarded("link_work"))
                self.assertEqual(app.task_result_gate.mode, mode)
                self.assertEqual(app.task_result_gate.enforcing(), mode == "enforce")
            finally:
                app.store.close()

    def test_keyless_local_model_is_ready_not_a_missing_key(self):
        from pangpang.__main__ import render_self_check
        # self.key.stop() restores a pre-existing TYPESAFE_API_KEY, so the router
        # would be jev. Drop the key for this assertion only; the patch restores it.
        env = os.environ.copy()
        env.pop("TYPESAFE_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(os.environ.get("TYPESAFE_API_KEY"))
            self.assertEqual(self.app.continuation_router(), "local_model")
            row = self._semantic_gate_row()
            self.assertEqual((row["state"], row["reason"]), ("ready", "continuation_local_model"))
            self.assertIsNone(row["operator_action_code"])
            self.assertIsNone(row["operator_action"])
            self.assertNotIn("TYPESAFE_API_KEY", json.dumps(row))
            diag = self.app.diagnostics()
            listed = next(item for item in diag["effective_capabilities"]
                          if item["capability"] == "semantic_gate")
            self.assertEqual((listed["state"], listed["reason"]), ("ready", "continuation_local_model"))
            self.assertEqual(listed["detail"]["continuation_router"], "local_model")
            self.assertEqual(diag["capabilities"]["continuation_router"], "local_model")
            text, _code = render_self_check(self.app)
            line = next(item for item in text.splitlines() if item.startswith("semantic_gate\t"))
            _name, state, reason, action = line.split("\t", 3)
            self.assertEqual((state, reason, action), ("ready", "continuation_local_model", "-"))

            self.app.semantic_gate.mode = "enforce"
            blocked = self._semantic_gate_row()
            self.assertEqual((blocked["state"], blocked["reason"]), ("blocked", "gate_key_missing_enforce"))
            self.app.semantic_gate.mode = "off"

            for profile in self.app.profiles.values():
                profile["endpoint"] = ""
                profile["model"] = ""
            bare = self._semantic_gate_row()
            self.assertEqual(self.app.continuation_router(), "off")
            self.assertEqual(bare["state"], "unconfigured")
            self.assertNotEqual(bare["reason"], "continuation_local_model")


if __name__ == "__main__":
    unittest.main()
