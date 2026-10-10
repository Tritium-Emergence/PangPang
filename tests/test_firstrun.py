# -*- coding: utf-8 -*-
""" Conversational First-run & Effective Capability Setup.

Backend contract tests for Service.effective_capabilities() (B). The projection
is the thing the first-run conversation reads to tell the user what is missing and
the correct operator action, so these tests pin:

  * PARITY — the executor state/reason is taken verbatim from the real admission
              gate route(); it can never claim an executor the control plane would
              reject. This is the RCB acceptance test (not a mock of machinery).
  * CREDENTIAL effective reality — "configured once" is not "usable now"; only a
              credential the RUNNING coordinator process can resolve counts.
  * HERO PATH — empty -> model ready -> focused Bot -> bounded Routine -> first run,
              with authority bounded by the control plane, not the model.
  * ZERO NETWORK — the projection is deterministic; it never probes an endpoint.

Preconditions for the hero path (explicit so the 5-minute metric is not faked by
pre-provisioning): a reachable model endpoint (here a local mock), and — for a real
executor run — a paired device with a discovered CLI. The smoke test proves the
control-plane loop reaches "run"; a real executor receipt is covered by the
scheduler/executor suites and is not re-tested here.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from pangpang.service import Service
from pangpang.onboarding import empty_config
from pangpang.store import Fault, encode
from test_bot_runtime import config


def caps(app, bot_id=None):
    return {c["capability"]: c for c in app.effective_capabilities(bot_id)}


def commit_model(app, *, model="test", credential_ref=None, credential_env=None, vault_value="secret"):
    """Configure the live model through the control plane's real commit path (CAS +
    publish) — the same commit_managed_config onboarding.model() uses after its probe,
    minus the Node probe (that path is covered by test_onboarding.py). This keeps the
    projection tests deterministic and free of a Node worker dependency."""
    if credential_ref and vault_value is not None:
        app.vault.set(credential_ref, vault_value)
    candidate, base = app.managed_snapshot()
    p = candidate["models"][0]
    p.update(endpoint="https://api.example/v1", model=model)
    p.pop("credential_ref", None)
    p.pop("credential_env", None)
    if credential_ref:
        p["credential_ref"] = credential_ref
    elif credential_env:
        p["credential_env"] = credential_env
    app.commit_managed_config(candidate, base_revision=base)


# Process-global env vars these tests read/mutate. setUp snapshots them and tearDown
# restores the exact pre-test values, so a test that pops/sets one cannot leak into
# later tests in the same unittest process (would otherwise be an order-dependent flake
# source). patch.dict(..., clear=False) blocks already self-restore; the snapshot also
# covers the bare os.environ.pop() cases.
_ENV_VARS = ("TAVILY_API_KEY", "TYPESAFE_API_KEY", "XP74_TEST_MODEL_KEY")


def _restore_env(snapshot):
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class CapabilityStateTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in _ENV_VARS}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(empty_config(), self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()
        _restore_env(self._env) # exact pre-test values, no leak to later tests

    def test_fresh_install_is_unconfigured_with_actionable_reasons(self):
        for var in ("TAVILY_API_KEY", "TYPESAFE_API_KEY"):
            os.environ.pop(var, None)
        c = caps(self.app)
        self.assertEqual(c["model"]["state"], "unconfigured")
        self.assertEqual(c["executor"]["state"], "unconfigured")
        hint = c["executor"]["operator_action"]
        self.assertIn("内置执行器", hint)
        self.assertIn("重启协调器", hint)
        self.assertIn("claude", hint)
        self.assertIn("codex", hint)
        for banned in ("配对", "探测安装", "PATH", "按钮"):
            self.assertNotIn(banned, hint)
        self.assertEqual(c["device"]["state"], "unconfigured")
        self.assertEqual(c["web_search"]["state"], "ready") # keyless since 1.0 (Bing/DuckDuckGo)
        # Every non-ready capability must give the operator something to act on.
        for entry in self.app.effective_capabilities():
            if entry["state"] != "ready":
                self.assertTrue(entry["reason"], entry)
                self.assertTrue(entry["operator_action_code"], entry)
            self.assertIn(entry["state"], {"ready", "unconfigured", "blocked", "unknown"})

    def test_model_credential_effective_reality(self):
        # Configured with a vault-backed credential -> ready. Then make the credential
        # unresolvable (the vault entry is gone): configured, but THIS process cannot
        # resolve it -> blocked, not ready.
        commit_model(self.app, credential_ref="mk1")
        self.assertEqual(caps(self.app)["model"]["state"], "ready")
        self.app.vault.delete("mk1")
        model = caps(self.app)["model"]
        self.assertEqual(model["state"], "blocked")
        self.assertEqual(model["reason"], "model_credential_missing")

    def test_vault_row_present_but_undecryptable_is_not_ready(self):
        # The projection must not be stronger than the real model path: a vault row that
        # exists but whose ciphertext cannot be decrypted (corruption / key mismatch)
        # passes vault.has() yet faults in PiModel.credentials() -> must be blocked.
        commit_model(self.app, credential_ref="mk2")
        self.assertEqual(caps(self.app)["model"]["state"], "ready")
        self.assertTrue(self.app.vault.has("mk2"))
        with self.app.vault.lock: # overwrite the stored token with a non-decryptable value
            self.app.vault.db.execute("INSERT OR REPLACE INTO secrets VALUES (?,?)", ("mk2", b"not-a-valid-fernet-token"))
            self.app.vault.db.commit()
        self.assertTrue(self.app.vault.has("mk2")) # has() still true...
        self.assertEqual(caps(self.app)["model"]["state"], "blocked") #...but projection agrees with runtime

    def test_runtime_rejected_endpoint_is_not_ready(self):
        # An endpoint the real model path would refuse (non-loopback http) must read
        # blocked, even though a credential is present — the projection defers to the
        # canonical validator, not just credential presence.
        commit_model(self.app) # valid https, no-auth -> ready
        self.assertEqual(caps(self.app)["model"]["state"], "ready")
        next(iter(self.app.profiles.values()))["endpoint"] = "http://evil.example/v1"
        model = caps(self.app)["model"]
        self.assertEqual(model["state"], "blocked")
        self.assertEqual(model["reason"], "model_endpoint_requires_https_or_loopback")

    def test_no_secret_in_serialized_diagnostics_or_status(self):
        secret = "SUPER-SECRET-NEVER-IN-PAYLOAD-XYZ"
        self.app.vault.set("mk3", secret)
        commit_model(self.app, credential_ref="mk3", vault_value=None) # ref already seeded above
        self.assertEqual(caps(self.app)["model"]["state"], "ready") # resolves (and discards) the secret
        self.assertNotIn(secret, encode(self.app.diagnostics()))
        self.assertNotIn(secret, encode(self.app.onboarding.status()))

    def test_env_credential_only_counts_if_this_process_inherited_it(self):
        # An env-var credential is "the shell export after start does not count" case:
        # readiness tracks THIS process's os.environ, nothing else.
        commit_model(self.app, credential_env="XP74_TEST_MODEL_KEY")
        os.environ.pop("XP74_TEST_MODEL_KEY", None)
        self.assertEqual(caps(self.app)["model"]["state"], "blocked")
        with patch.dict(os.environ, {"XP74_TEST_MODEL_KEY": "value-this-process-sees"}):
            self.assertEqual(caps(self.app)["model"]["state"], "ready")

    def test_config_stale_makes_model_unknown(self):
        commit_model(self.app) # no-auth, configured and coherent -> ready
        self.assertEqual(caps(self.app)["model"]["state"], "ready")
        # Simulate an edit behind the running service: the DB revision moves past what
        # this process published, so we cannot assert the live profile is usable.
        self.app.store.put("settings", {"id": "managed", "config": self.app.config})
        self.assertTrue(self.app.config_stale())
        self.assertEqual(caps(self.app)["model"]["state"], "unknown")

    def test_device_revoked_is_distinct_from_offline(self):
        # A paired-but-never-seen device is offline (restart the connector); a revoked
        # device needs re-pairing. The two must not collapse to one action.
        self.app.store.put("device", {"id": "host-x", "instance_id": "i"})
        d = caps(self.app)["device"]
        self.assertEqual((d["state"], d["reason"]), ("blocked", "all_devices_offline"))
        self.assertEqual(d["operator_action_code"], "bring_device_online")
        self.app.broker.revoke("host-x")
        d = caps(self.app)["device"]
        self.assertEqual((d["state"], d["reason"]), ("blocked", "all_devices_revoked"))
        self.assertEqual(d["operator_action_code"], "pair_device")

    def test_semantic_gate_enforce_without_key_is_blocked(self):
        self.app.semantic_gate.mode = "enforce"
        os.environ.pop("TYPESAFE_API_KEY", None)
        sg = caps(self.app)["semantic_gate"]
        self.assertEqual(sg["state"], "blocked")
        self.assertEqual(sg["reason"], "gate_key_missing_enforce")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}):
            self.assertEqual(caps(self.app)["semantic_gate"]["state"], "ready")

    def test_semantic_gate_key_present_but_all_off_is_not_ready(self):
        # P2: "ready" must mean actually enabled under the current config, not merely
        # "a key exists". With every usage path off, a present key is not readiness.
        self.app.semantic_gate.mode = "off"
        self.app.task_result_gate.mode = "off"
        self.app.semantic_gate.continuation_routing = False
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}):
            sg = caps(self.app)["semantic_gate"]
        self.assertEqual(sg["state"], "unconfigured")
        self.assertEqual(sg["reason"], "disabled")

    def test_shadow_only_missing_key_does_not_misreport_continuation(self):
        # P2 (reason accuracy): when the only enabled path is a shadow gate and
        # continuation routing is off, a missing key must NOT be attributed to
        # continuation. The reason must name the actual enabled path.
        self.app.semantic_gate.mode = "shadow"
        self.app.task_result_gate.mode = "off"
        self.app.semantic_gate.continuation_routing = False
        os.environ.pop("TYPESAFE_API_KEY", None)
        sg = caps(self.app)["semantic_gate"]
        self.assertEqual(sg["state"], "unconfigured")
        self.assertNotEqual(sg["reason"], "continuation_key_missing")
        self.assertEqual(sg["reason"], "gate_key_missing_optional")

    def test_projection_does_no_network(self):
        # The projection must be a pure read of durable/process state. If it ever
        # reached out to the model endpoint, patching complete() to raise would surface.
        from pangpang.pi_runtime import PiModel
        with patch.object(PiModel, "complete", side_effect=AssertionError("projection must not call the model")):
            result = self.app.effective_capabilities()
        self.assertTrue(result)

    def test_env_is_restored_after_mutation(self):
        # Isolation guard: a test that pops a managed env var must not leak the removal
        # to later tests. Prove the setUp/tearDown snapshot restores the exact value by
        # seeding a sentinel, mutating it, and checking tearDown-style restore brings it
        # back verbatim.
        sentinel = "XP74-ISOLATION-SENTINEL"
        snapshot = {k: os.environ.get(k) for k in _ENV_VARS}
        try:
            os.environ["TAVILY_API_KEY"] = sentinel
            os.environ.pop("TYPESAFE_API_KEY", None)
            _restore_env(snapshot)
            self.assertEqual(os.environ.get("TAVILY_API_KEY"), snapshot["TAVILY_API_KEY"])
            self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), snapshot["TYPESAFE_API_KEY"])
        finally:
            _restore_env(snapshot)


class ExecutorParityTests(unittest.TestCase):
    """The RCB teeth: the executor projection equals the real admission verdict."""

    def _assert_parity(self, app, bot_id="bot-main"):
        routing = app.route(bot_id, {"id": "capability-probe"}, {})
        entry = caps(app, bot_id)["executor"]
        if not app.execution.installations:
            self.assertEqual(entry["state"], "unconfigured")
            return
        self.assertEqual(entry["state"], "ready" if routing["selected"] else "blocked")
        route_rejected = {(r["installation_id"], r["reason"]) for r in routing["rejected"]}
        proj_rejected = {(i["installation_id"], i["reason"]) for i in entry["detail"]["installations"] if i["reason"]}
        self.assertEqual(proj_rejected, route_rejected)

    def test_parity_no_installations(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Service(empty_config(), Path(tmp) / "data")
            try:
                self._assert_parity(app)
            finally:
                app.store.close()

    def test_parity_not_discovered_available_and_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Service(config(root), root / "data")
            try:
                # (a) no probe row yet -> route rejects not_discovered
                self._assert_parity(app)
                self.assertEqual(caps(app)["executor"]["state"], "blocked")
                # (b) probe says available -> route may select -> ready (parity holds
                # regardless of whether supervision policy also gates it)
                app.store.put("probe", {"id": "codex-local", "available": True,
                                        "supervision": "windows-job-object"})
                self._assert_parity(app)
                # (c) disabled installation -> installation_disabled
                app.execution.installations["codex-local"]["enabled"] = False
                self._assert_parity(app)
                self.assertEqual(caps(app)["executor"]["state"], "blocked")
            finally:
                app.store.close()

    def test_parity_control_only_rejects_local_executor(self):
        # The exact RCB hole: a control-only coordinator must never report a local
        # executor as ready, because route() hard-gates it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Service(config(root), root / "data", control_only=True)
            try:
                app.store.put("probe", {"id": "codex-local", "available": True,
                                        "supervision": "windows-job-object"})
                self._assert_parity(app)
                entry = caps(app)["executor"]
                self.assertEqual(entry["state"], "blocked")
                reasons = {i["reason"] for i in entry["detail"]["installations"] if i["reason"]}
                self.assertIn("control_only_no_local_executor", reasons)
            finally:
                app.store.close()

    def test_empty_executor_hint_matches_the_coordinator_mode(self):
        #: a control-only coordinator never downloads pi or registers a
        # local executor, so "restart or install claude" cannot help it.
        for control_only, code in ((True, "pair_remote_executor"), (False, "connect_executor")):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app = Service(empty_config(), root / "data", control_only=control_only)
                try:
                    entry = caps(app)["executor"]
                    self.assertEqual((entry["state"], entry["reason"]), ("unconfigured", "no_executor_registered"))
                    self.assertEqual(entry["operator_action_code"], code, control_only)
                    self.assertEqual("重启" in entry["operator_action"], not control_only, control_only)
                finally:
                    app.store.close()


    def test_parity_bot_policy_is_evaluated_per_bot(self):
        # The model-facing projection must be scoped to the asking bot: a child not in
        # an installation's allowed_bots must see it blocked (bot_policy), matching what
        # its own submit would hit — not bot-main's view.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Service(config(root), root / "data")
            try:
                app.store.put("probe", {"id": "codex-local", "available": True,
                                        "supervision": "windows-job-object"})
                app.execution.installations["codex-local"]["allowed_bots"] = ["bot-main"]
                self._assert_parity(app, "bot-main")
                self._assert_parity(app, "bot-other")
                other = caps(app, "bot-other")["executor"]
                self.assertEqual(other["state"], "blocked")
                reasons = {i["reason"] for i in other["detail"]["installations"] if i["reason"]}
                self.assertIn("bot_policy", reasons)
                self.assertEqual(other["operator_action_code"], "adjust_executor_policy")
            finally:
                app.store.close()


class HeroPathTests(unittest.TestCase):
    """Lean start -> first-value smoke: capability transitions + bounded authority."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(empty_config(), self.root / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _user_turn(self):
        m = self.app.send("bot-main", {"text": "我要一个负责 AI 公众号的 Bot"}, "hero-" + str(time.time()))
        return self.app.store.find("turn", work_id=m["work_id"])[0]

    def test_start_to_first_value(self):
        # 1. Start: model unconfigured.
        self.assertEqual(caps(self.app)["model"]["state"], "unconfigured")
        # 2. Configured: the control plane (not the model) commits the model config via
        # the real CAS commit path; the projection flips to ready.
        commit_model(self.app, credential_ref="hero-key")
        self.assertEqual(caps(self.app)["model"]["state"], "ready")
        # 3. A focused Bot for the stated intent.
        pid = next(iter(self.app.profiles))
        bot = self.app.create_bot({"name": "公众号管家", "role": "AI 公众号编辑",
                                   "instructions": "每天产出一篇公众号稿件", "model_profile_id": pid})
        self.assertEqual(self.app.bot(bot["id"])["role"], "AI 公众号编辑")
        # 4. A model-proposed Routine starts PENDING with bounded authority — the model
        # cannot grant itself standing write; approval is the control plane's act.
        r = self.app.create_routine("bot-main", {"instruction": "每天五点出公众号选题",
            "timezone": "Asia/Shanghai", "schedule": {"type": "daily", "time": "05:00"},
            "authority": {"grant_write": True}}, turn=self._user_turn())
        routine = self.app.store.get("routine", r["id"])
        self.assertEqual(routine["approval_state"], "pending")
        self.assertEqual(routine["authority"], {"grant_write": False}) # proposal cannot widen
        approved = self.app.approve_routine("bot-main", r["id"],
            expected_digest=self.app.routine_review(r)["approval_digest"], authority={"grant_write": True})
        self.assertTrue(approved["active"])
        self.assertEqual(approved["authority"], {"grant_write": True})
        # 5. First value: the approved routine, run once, materializes real Work.
        self.app.run_routine_now("bot-main", r["id"])
        self.assertTrue(any(w.get("routine_id") == r["id"] for w in self.app.store.find("work", limit=100)))


if __name__ == "__main__":
    unittest.main()
