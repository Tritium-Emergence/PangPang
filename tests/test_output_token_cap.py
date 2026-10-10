""": a Work stalled because the planner ran with output_tokens=1200.

The 1200 came from config/runtime.example.json and survived setup/model. The output
cap now defaults to and may not go below 32768 (or a catalog model's lower maximum):
a stored smaller cap is raised on load, and the settings form shows and sets it.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pangpang.model_config import DEFAULT_OUTPUT_TOKENS, catalog, output_floor, setup_profile
from pangpang.onboarding import empty_config
from pangpang.service import Service, normalize_profile
from pangpang.store import Fault
from test_bot_runtime import call

ROOT = Path(__file__).resolve().parent.parent


class OutputCapTests(unittest.TestCase):
    def test_example_config_uses_the_first_run_output_cap(self):
        example = json.loads((ROOT / "config" / "runtime.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["models"][0]["output_tokens"], DEFAULT_OUTPUT_TOKENS)
        self.assertEqual(empty_config()["models"][0]["output_tokens"], DEFAULT_OUTPUT_TOKENS)

    def test_setup_model_onto_a_new_model_drops_the_old_cap(self):
        # The tester's path: an example-derived profile (placeholders, cap 1200), then
        # setup/model with a custom Anthropic-protocol endpoint.
        from pangpang.pi_runtime import PiModel
        with tempfile.TemporaryDirectory() as tmp:
            app = Service(empty_config(), Path(tmp) / "data")
            try:
                next(iter(app.profiles.values())).update(
                    endpoint="https://REPLACE_WITH_LLM_ENDPOINT/v1", model="REPLACE_WITH_YOUR_MODEL_ID",
                    credential_env="PANGPANG_LLM_KEY", output_tokens=1200)
                caps = []
                def complete(model, profile, messages, tools):
                    caps.append(profile["output_tokens"])
                    return (call("setup_probe", value="pangpang-ready") if len(caps) == 1 else {"content": "ready"}), {}
                with patch.object(PiModel, "complete", complete):
                    app.onboarding.model({"provider": "custom", "api": "anthropic-messages",
                                          "endpoint": "https://proxy.example", "model": "anthropic--claude-4.8-opus",
                                          "secret": "setup-key"})
                self.assertEqual(caps, [DEFAULT_OUTPUT_TOKENS, DEFAULT_OUTPUT_TOKENS])
                self.assertEqual(next(iter(app.profiles.values()))["output_tokens"], DEFAULT_OUTPUT_TOKENS)
            finally:
                app.store.close()

    def test_catalog_switch_clamps_the_default_not_the_old_cap(self):
        key, known = next(iter(catalog()["providers"]["anthropic"].items()))
        old = {"endpoint": "https://old.example/v1", "model": "qwen", "output_tokens": 1200}
        p = setup_profile(old, {"provider": "anthropic", "model": key})
        self.assertEqual(p["output_tokens"], min(DEFAULT_OUTPUT_TOKENS, known["maxTokens"]))

    def test_resaving_the_same_model_keeps_an_explicit_cap(self):
        old = {"endpoint": "https://local.example/v1", "model": "qwen", "output_tokens": 65536}
        self.assertEqual(setup_profile(old, {"endpoint": old["endpoint"], "model": "qwen"})["output_tokens"], 65536)

    def test_form_cap_is_validated_against_the_floor(self):
        old = {"endpoint": "https://local.example/v1", "model": "qwen", "output_tokens": DEFAULT_OUTPUT_TOKENS}
        body = {"endpoint": old["endpoint"], "model": "qwen"}
        self.assertEqual(setup_profile(old, dict(body, output_tokens=65536))["output_tokens"], 65536)
        for bad in (1200, DEFAULT_OUTPUT_TOKENS - 1, 2 ** 21, "65536", True, 65536.0):
            with self.assertRaises(Fault) as caught:
                setup_profile(old, dict(body, output_tokens=bad))
            self.assertEqual(caught.exception.code, "invalid_output_tokens")
        # A catalog model with a lower hard maximum: its maximum is the floor, and a larger
        # request is clamped to it (the provider rejects more).
        provider, key, known = self.low_max_model()
        p = setup_profile(old, {"provider": provider, "model": key, "output_tokens": known["maxTokens"]})
        self.assertEqual(p["output_tokens"], known["maxTokens"])
        with self.assertRaises(Fault):
            setup_profile(old, {"provider": provider, "model": key, "output_tokens": known["maxTokens"] - 1})
        key, known = next((k, m) for k, m in catalog()["providers"]["anthropic"].items() if m["maxTokens"] > DEFAULT_OUTPUT_TOKENS)
        p = setup_profile(old, {"provider": "anthropic", "model": key, "output_tokens": known["maxTokens"] + 1})
        self.assertEqual(p["output_tokens"], known["maxTokens"])

    def test_loading_raises_a_stored_cap_below_the_floor(self):
        for stored, expected in ((1200, DEFAULT_OUTPUT_TOKENS), ("absent", DEFAULT_OUTPUT_TOKENS), (None, DEFAULT_OUTPUT_TOKENS), (65536, 65536)):
            profile = {"id": "default", "model": "qwen", "endpoint": "https://local.example/v1"}
            if stored != "absent":
                profile["output_tokens"] = stored
            self.assertEqual(normalize_profile(profile)["output_tokens"], expected)
        provider, key, known = self.low_max_model()
        profile = {"id": "default", "provider": provider, "model": key, "api": known["api"], "output_tokens": 1200}
        self.assertEqual(normalize_profile(profile)["output_tokens"], known["maxTokens"])
        self.assertEqual(output_floor(profile), known["maxTokens"])

    def test_a_deployment_saved_with_1200_runs_and_shows_32768_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            app = Service(empty_config(), data)
            try:
                candidate, base = app.managed_snapshot()
                candidate["models"][0].update(endpoint="https://proxy.example", model="opus", output_tokens=1200)
                app.commit_managed_config(candidate, base_revision=base) # the old example's value, persisted
                self.assertEqual(app.store.get("settings", "managed")["config"]["models"][0]["output_tokens"], 1200)
            finally:
                app.store.close()
            app = Service(empty_config(), data)
            try:
                self.assertEqual(next(iter(app.profiles.values()))["output_tokens"], DEFAULT_OUTPUT_TOKENS)
                self.assertEqual(app.onboarding.status()["model"]["output_tokens"], DEFAULT_OUTPUT_TOKENS)
                # The next config commit writes the raised value back.
                candidate, base = app.managed_snapshot()
                app.commit_managed_config(candidate, base_revision=base)
                self.assertEqual(app.store.get("settings", "managed")["config"]["models"][0]["output_tokens"], DEFAULT_OUTPUT_TOKENS)
            finally:
                app.store.close()

    @staticmethod
    def low_max_model():
        return next((provider, key, m) for provider, models in catalog()["providers"].items()
                    for key, m in models.items() if m["maxTokens"] < DEFAULT_OUTPUT_TOKENS)


if __name__ == "__main__":
    unittest.main()
