# -*- coding: utf-8 -*-
"""Capability projection is the single source of truth for what the model may use:
web_search is keyless since 1.0 and must always be present in both the toolset and
the planner prompt, and an auth failure (401/403) must be classified distinctly
while staying retryable (behavior-preserving)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.pi_runtime import _model_error_code
from pangpang.planner_prompt import PLANNER_SYSTEM, WEB_SEARCH_GUIDANCE
from pangpang.runtime import RETRYABLE_TURN_ERRORS as RUNTIME_RETRYABLE
from pangpang.runtime import Runtime
from pangpang.scheduler import RETRYABLE_TURN_ERRORS
from pangpang.service import Service


class CapabilityToolProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.runtime = Runtime(self.app)

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _turn(self, tag):
        sent = self.app.send("bot-main", {"text": "hello"}, tag)
        return self.app.store.get("turn", sent["turn_id"])

    def _visible(self, turn):
        return {t["function"]["name"] for t in self.runtime.tools_for(turn)}

    def test_web_search_always_visible_without_any_key(self):
        # 1.0 keyless fallback: web_search is always available (Bing/DuckDuckGo
        # chain), so it must never vanish from the toolset or the planner prompt.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAVILY_API_KEY", None)
            self.assertTrue(self.app.capabilities()["web_search"])
            visible = self._visible(self._turn("cap-off-1"))
            self.assertIn("web_search", visible)
            self.assertIn("checkpoint_work", visible) # unrelated tools unaffected
            self.assertIn("create_bot", visible) # main-bot kind filter unchanged
            system = self.runtime.context(self._turn("cap-prompt-off"))[0]["content"]
            self.assertIn("You have a web_search tool", system)

    def test_web_search_visible_with_tavily_key(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}):
            self.assertTrue(self.app.capabilities()["web_search"])
            self.assertIn("web_search", self._visible(self._turn("cap-on-1")))
            system = self.runtime.context(self._turn("cap-prompt-on"))[0]["content"]
            self.assertIn("You have a web_search tool", system)

    def test_planner_system_never_claims_a_search_tool(self):
        # The unconditional persona must not promise web_search; only the
        # capability-gated WEB_SEARCH_GUIDANCE block may.
        self.assertNotIn("web_search", PLANNER_SYSTEM)
        self.assertIn("web_search", WEB_SEARCH_GUIDANCE)


class ModelErrorCodeTests(unittest.TestCase):
    def test_auth_failure_classified_distinctly(self):
        self.assertEqual(_model_error_code("HTTP 429 Too Many Requests"), "model_rate_limited")
        self.assertEqual(_model_error_code("Error: 401 Unauthorized"), "model_auth_failed")
        self.assertEqual(_model_error_code("403 Forbidden"), "model_auth_failed")
        self.assertEqual(_model_error_code("invalid api key"), "model_auth_failed")
        self.assertEqual(_model_error_code("connection reset by peer"), "model_request_failed")

    def test_auth_status_codes_need_token_boundaries(self):
        # 401/403 must match as standalone tokens: ports, request ids and model
        # names containing those digits are NOT auth failures (review P1).
        self.assertEqual(_model_error_code("connect ECONNREFUSED 127.0.0.1:4010"), "model_request_failed")
        self.assertEqual(_model_error_code("request id req_403abc failed"), "model_request_failed")
        self.assertEqual(_model_error_code("model x401-large is unavailable"), "model_request_failed")

    def test_common_auth_wordings_not_missed(self):
        # Google-style and generic auth phrasings must not fall through to generic.
        self.assertEqual(_model_error_code("UNAUTHENTICATED"), "model_auth_failed")
        self.assertEqual(_model_error_code("API key not valid. Please pass a valid API key."), "model_auth_failed")

    def test_auth_failure_stays_retryable_and_sets_stay_in_sync(self):
        # Classification must not change retry behavior (a flaky gateway can emit
        # a transient 401); runtime.py keeps a mirror of the authoritative set.
        self.assertIn("model_auth_failed", RETRYABLE_TURN_ERRORS)
        self.assertEqual(RUNTIME_RETRYABLE, RETRYABLE_TURN_ERRORS)


if __name__ == "__main__":
    unittest.main()
