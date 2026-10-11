"""S5 regressions: a 32K window can take a real output cap, concurrency persists,
and a private-network HTTP endpoint stays refused.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pangpang.model_config import WINDOW_FIT_OUTPUT_TOKENS, setup_profile
from pangpang.onboarding import endpoint
from pangpang.service import Service, normalize_profile
from pangpang.store import Fault
from pangpang.onboarding import empty_config
from test_bot_runtime import call


class ContextCapTests(unittest.TestCase):
    def test_operator_can_set_a_32k_cap_and_an_oversize_cap_is_refused(self):
        old = {"endpoint": "https://local.example/v1", "model": "qwen", "provider": "custom",
               "context_window_tokens": 32000, "output_tokens": 32768}
        saved = setup_profile(old, {"endpoint": old["endpoint"], "model": "qwen",
                                    "context_window_tokens": 32000, "output_tokens": 8192})
        self.assertEqual(saved["output_tokens"], 8192)
        self.assertEqual(saved["context_window_tokens"], 32000)
        with self.assertRaises(Fault) as caught:
            setup_profile(old, {"endpoint": old["endpoint"], "model": "qwen",
                                "context_window_tokens": 32000, "output_tokens": 32768})
        self.assertEqual(caught.exception.code, "output_exceeds_context_window")
        with self.assertRaises(Fault) as caught:
            setup_profile(old, {"endpoint": old["endpoint"], "model": "qwen",
                                "context_window_tokens": 4000, "output_tokens": 8192})
        self.assertEqual(caught.exception.code, "invalid_context_window")

    def test_loading_a_32k_profile_keeps_a_legal_cap_and_fits_an_illegal_one(self):
        kept = normalize_profile({"id": "default", "model": "qwen", "endpoint": "https://local.example/v1",
                                  "context_window_tokens": 32000, "output_tokens": 8192})
        self.assertEqual(kept["output_tokens"], 8192)
        fitted = normalize_profile({"id": "default", "model": "qwen", "endpoint": "https://local.example/v1",
                                    "context_budget": 32000, "output_tokens": 32768})
        self.assertEqual(fitted["context_window_tokens"], 32000)
        self.assertEqual(fitted["output_tokens"], WINDOW_FIT_OUTPUT_TOKENS)

    def test_private_http_endpoints_stay_refused(self):
        for url in ("http://192.168.1.5/v1", "http://10.1.2.3/v1", "http://172.16.0.4/v1", "http://169.254.1.1/v1"):
            with self.assertRaises(Fault) as caught:
                endpoint(url)
            self.assertEqual(caught.exception.code, "endpoint_requires_https_or_loopback")
        self.assertEqual(endpoint("http://127.0.0.1:11434/v1"), "http://127.0.0.1:11434/v1")
        self.assertEqual(endpoint("https://llm.example/v1"), "https://llm.example/v1")


class ConcurrencyPersistTests(unittest.TestCase):
    def _save(self, app, **extra):
        from pangpang.pi_runtime import PiModel
        caps = []

        def complete(model, profile, messages, tools):
            caps.append(profile["output_tokens"])
            return (call("setup_probe", value="pangpang-ready") if len(caps) == 1 else {"content": "ready"}), {}

        body = {"provider": "custom", "api": "openai-completions", "endpoint": "https://proxy.example/v1",
                "model": "qwen-32k", "secret": "setup-key", "context_window_tokens": 32000,
                "output_tokens": 8192}
        body.update(extra)
        with patch.object(PiModel, "complete", complete):
            app.onboarding.model(body)
        return caps

    def test_concurrency_and_32k_cap_survive_restart_and_the_probe_sees_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            app = Service(empty_config(), data)
            try:
                caps = self._save(app, model_max_concurrency=1)
                self.assertEqual(caps, [8192, 8192])
                self.assertTrue(app.onboarding.status()["model_concurrency_restart_required"])
                self.assertEqual(app.onboarding.status()["model_max_concurrency"], 1)
                stored = app.store.get("settings", "managed")["config"]
                self.assertEqual(stored["models"][0]["output_tokens"], 8192)
                self.assertEqual(stored["scheduler"]["model_max_concurrency"], 1)
                self.assertEqual(stored["scheduler"]["reserved_user_slots"], 0)
            finally:
                app.store.close()
            app = Service(empty_config(), data)
            try:
                self.assertEqual(app.scheduler_config["model_max_concurrency"], 1)
                self.assertEqual(app.scheduler_config["reserved_user_slots"], 0)
                profile = next(iter(app.profiles.values()))
                self.assertEqual(profile["output_tokens"], 8192)
                self.assertEqual(profile["context_window_tokens"], 32000)
                self.assertFalse(app.onboarding.status()["model_concurrency_restart_required"])
                caps = self._save(app, model_max_concurrency=2)
                self.assertEqual(caps, [8192, 8192])
                self.assertEqual(app.store.get("settings", "managed")["config"]["scheduler"]["model_max_concurrency"], 2)
            finally:
                app.store.close()
            app = Service(empty_config(), data)
            try:
                self.assertEqual(app.scheduler_config["model_max_concurrency"], 2)
                # 0 stayed legal when concurrency rose from 1; do not invent a reserved slot.
                self.assertEqual(app.scheduler_config["reserved_user_slots"], 0)
            finally:
                app.store.close()

    def test_illegal_concurrency_and_oversize_cap_do_not_probe_or_persist(self):
        from pangpang.pi_runtime import PiModel
        with tempfile.TemporaryDirectory() as tmp:
            app = Service(empty_config(), Path(tmp) / "data")
            try:
                def boom(*args, **kwargs):
                    raise AssertionError("probe must not run")
                body = {"provider": "custom", "api": "openai-completions", "endpoint": "https://proxy.example/v1",
                        "model": "qwen-32k", "secret": "setup-key", "context_window_tokens": 32000,
                        "output_tokens": 8192}
                with patch.object(PiModel, "complete", boom):
                    for bad in (0, 17, True, "2", 1.5):
                        with self.assertRaises(Fault) as caught:
                            app.onboarding.model(dict(body, model_max_concurrency=bad))
                        self.assertEqual(caught.exception.code, "invalid_max_concurrency")
                    with self.assertRaises(Fault) as caught:
                        app.onboarding.model(dict(body, output_tokens=32768))
                    self.assertEqual(caught.exception.code, "output_exceeds_context_window")
                self.assertNotIn("scheduler", app.config)
                self.assertEqual(next(iter(app.profiles.values()))["output_tokens"], 32768)
            finally:
                app.store.close()


if __name__ == "__main__":
    unittest.main()
