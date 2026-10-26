# -*- coding: utf-8 -*-
"""Sealed Web path for allowlisted coordinator env (TYPESAFE_API_KEY, TAVILY_API_KEY).

The browser seals to the coordinator vault. Plaintext is not a request field,
not a chat message, and not written to the workspace. The vault holds ciphertext;
this process environment receives the value, and the next start loads it again.
"""
import base64
import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pangpang.coordinator_env import COORDINATOR_ENV_ALLOWLIST
from pangpang.http import serve
from pangpang.onboarding import empty_config
from pangpang.semantic_gate import redact_known_secrets, secret_env_names
from pangpang.service import Service
from pangpang.store import Fault, encode


def _seal(public_key, context, secret):
    key = serialization.load_der_public_key(base64.b64decode(public_key))
    aes = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    wrapped = key.encrypt(aes, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    ciphertext = AESGCM(aes).encrypt(nonce, secret.encode(), context.encode())
    return {
        "key": base64.b64encode(wrapped).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


def _restore(snapshot):
    for name, value in snapshot.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class CoordinatorEnvTests(unittest.TestCase):
    def setUp(self):
        self._env = {name: os.environ.get(name) for name in ("TYPESAFE_API_KEY", "TAVILY_API_KEY", "PANGPANG_TOKEN")}
        for name in ("TYPESAFE_API_KEY", "TAVILY_API_KEY"):
            os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.app = Service(empty_config(), self.data)

    def tearDown(self):
        try:
            self.app.store.close()
        except Exception:
            pass
        self.tmp.cleanup()
        _restore(self._env)

    def _target(self, name="TYPESAFE_API_KEY"):
        return self.app.onboarding.coordinator_env_target(name)

    def _install(self, name, secret):
        target = self._target(name)
        sealed = _seal(target["public_key"], target["context"], secret)
        result = self.app.onboarding.set_coordinator_env({"name": name, "sealed": sealed})
        return result, sealed

    def _assert_secret_absent(self, secret):
        raw = secret.encode()
        self.assertNotIn(secret, encode(self.app.onboarding.status()))
        self.assertNotIn(secret, encode(self.app.effective_capabilities()))
        self.assertNotIn(secret, encode(self.app.diagnostics()))
        self.assertNotIn(secret, encode(self.app.store.find("message")))
        self.assertNotIn(raw, (self.data / "vault" / "secrets.db").read_bytes())
        self.assertNotIn(raw, (self.data / "bots.db").read_bytes())

    def test_allowlist_is_the_coordinator_optional_keys_only(self):
        self.assertEqual(COORDINATOR_ENV_ALLOWLIST, ("TYPESAFE_API_KEY", "TAVILY_API_KEY"))
        self.assertNotIn("PANGPANG_TOKEN", COORDINATOR_ENV_ALLOWLIST)

    def test_sealed_value_is_stored_encrypted_and_applied_to_this_process(self):
        secret = "typesafe-sealed-value-not-in-chat"
        missing = next(row for row in self.app.effective_capabilities() if row["capability"] == "semantic_gate")
        self.assertEqual(missing["operator_action_code"], "set_coordinator_env")
        self.assertEqual(missing["detail"]["env_var"], "TYPESAFE_API_KEY")
        self.assertFalse(missing["restart_required"])
        self.assertIn("凭据库", missing["operator_action"])
        result, _sealed = self._install("TYPESAFE_API_KEY", secret)
        self.assertEqual(result, {"name": "TYPESAFE_API_KEY", "configured": True, "sealed": True})
        self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), secret)
        self.assertEqual(self.app.vault.get("coordinator-env:TYPESAFE_API_KEY"), secret)
        row = next(item for item in self.app.onboarding.status()["coordinator_env"] if item["name"] == "TYPESAFE_API_KEY")
        self.assertEqual(row, {"name": "TYPESAFE_API_KEY", "configured": True, "sealed": True})
        gate = next(item for item in self.app.effective_capabilities() if item["capability"] == "semantic_gate")
        self.assertEqual(gate["state"], "ready")
        self.assertIsNone(gate["operator_action_code"])
        self.assertTrue(self.app.capabilities()["semantic_gate_key"])
        redacted = redact_known_secrets(secret_env_names(self.app.config), "prefix " + secret + " suffix")
        self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED_KNOWN_SECRET]", redacted)
        self._assert_secret_absent(secret)

    def test_tavily_is_allowlisted_and_other_names_are_rejected_before_unseal(self):
        secret = "tavily-sealed-value-not-in-chat"
        self._install("TAVILY_API_KEY", secret)
        redacted = redact_known_secrets(secret_env_names(self.app.config), "see " + secret)
        self.assertNotIn(secret, redacted)
        search = next(row for row in self.app.effective_capabilities() if row["capability"] == "web_search")
        self.assertEqual(search["provider"], "tavily")
        self.assertNotIn(secret, encode(search))
        typesafe = next(row for row in self.app.onboarding.status()["coordinator_env"] if row["name"] == "TYPESAFE_API_KEY")
        self.assertFalse(typesafe["sealed"])
        self.assertIsNone(os.environ.get("TYPESAFE_API_KEY"))
        os.environ["PANGPANG_TOKEN"] = "login-token-stays-put"
        for name in ("PANGPANG_TOKEN", "typesafe_api_key", "TYPESAFE_API_KEY_EXTRA", "PATH", "", None, 1):
            with self.assertRaises(Fault) as caught:
                self.app.onboarding.set_coordinator_env({
                    "name": name, "sealed": {"key": "aa", "nonce": "bb", "ciphertext": "cc"}})
            self.assertEqual(caught.exception.code, "coordinator_env_not_allowlisted")
        self.assertEqual(os.environ.get("PANGPANG_TOKEN"), "login-token-stays-put")
        self.assertFalse(self.app.vault.has("coordinator-env:PANGPANG_TOKEN"))
        self._assert_secret_absent(secret)

    def test_plaintext_body_is_rejected(self):
        secret = "plaintext-must-not-be-accepted"
        target = self._target()
        sealed = _seal(target["public_key"], target["context"], secret)
        for body in (
            {"name": "TYPESAFE_API_KEY", "secret": secret},
            {"name": "TYPESAFE_API_KEY", "value": secret},
            {"name": "TYPESAFE_API_KEY", "sealed": sealed, "secret": secret},
            {"sealed": sealed},
            {"name": "TYPESAFE_API_KEY"},
        ):
            with self.assertRaises(Fault) as caught:
                self.app.onboarding.set_coordinator_env(body)
            self.assertEqual(caught.exception.code, "sealed_credential_required")
        self.assertIsNone(os.environ.get("TYPESAFE_API_KEY"))
        self.assertFalse(self.app.vault.has("coordinator-env:TYPESAFE_API_KEY"))
        self._assert_secret_absent(secret)

    def test_seal_is_bound_to_the_allowlisted_name(self):
        secret = "context-bound-typesafe-value"
        target = self._target("TYPESAFE_API_KEY")
        foreign = _seal(target["public_key"], "setup-request-id", secret)
        swapped = _seal(target["public_key"], target["context"], secret)
        with self.assertRaises(Fault) as foreign_error:
            self.app.onboarding.set_coordinator_env({"name": "TYPESAFE_API_KEY", "sealed": foreign})
        self.assertEqual(foreign_error.exception.code, "sealed_credential_rejected")
        with self.assertRaises(Fault) as swapped_error:
            self.app.onboarding.set_coordinator_env({"name": "TAVILY_API_KEY", "sealed": swapped})
        self.assertEqual(swapped_error.exception.code, "sealed_credential_rejected")
        self.assertIsNone(os.environ.get("TYPESAFE_API_KEY"))
        self.assertIsNone(os.environ.get("TAVILY_API_KEY"))
        self.assertFalse(self.app.vault.has("coordinator-env:TYPESAFE_API_KEY"))
        self.assertFalse(self.app.vault.has("coordinator-env:TAVILY_API_KEY"))
        self._assert_secret_absent(secret)

    def test_newline_and_blank_values_are_rejected(self):
        target = self._target()
        for secret in ("line\nbreak", "   ", "x" * 16385):
            sealed = _seal(target["public_key"], target["context"], secret)
            with self.assertRaises(Fault) as caught:
                self.app.onboarding.set_coordinator_env({"name": "TYPESAFE_API_KEY", "sealed": sealed})
            self.assertEqual(caught.exception.code, "invalid_coordinator_env")
        self.assertIsNone(os.environ.get("TYPESAFE_API_KEY"))
        self.assertFalse(self.app.vault.has("coordinator-env:TYPESAFE_API_KEY"))

    def test_replace_and_clear(self):
        first = "first-sealed-typesafe-value"
        second = "second-sealed-typesafe-value"
        self._install("TYPESAFE_API_KEY", first)
        self._install("TYPESAFE_API_KEY", second)
        self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), second)
        self.assertEqual(self.app.vault.get("coordinator-env:TYPESAFE_API_KEY"), second)
        self.assertNotIn(first.encode(), (self.data / "vault" / "secrets.db").read_bytes())
        cleared = self.app.onboarding.clear_coordinator_env({"name": "TYPESAFE_API_KEY"})
        self.assertEqual(cleared, {"name": "TYPESAFE_API_KEY", "configured": False, "sealed": False})
        self.assertIsNone(os.environ.get("TYPESAFE_API_KEY"))
        self.assertFalse(self.app.vault.has("coordinator-env:TYPESAFE_API_KEY"))
        self._install("TAVILY_API_KEY", "sealed-tavily-clear-me")
        os.environ["TAVILY_API_KEY"] = "inherited-tavily-stays"
        left = self.app.onboarding.clear_coordinator_env({"name": "TAVILY_API_KEY"})
        self.assertEqual(os.environ.get("TAVILY_API_KEY"), "inherited-tavily-stays")
        self.assertEqual(left["configured"], True)
        self.assertEqual(left["sealed"], False)
        self.assertFalse(self.app.vault.has("coordinator-env:TAVILY_API_KEY"))
        with self.assertRaises(Fault) as caught:
            self.app.onboarding.clear_coordinator_env({"name": "TYPESAFE_API_KEY", "secret": second})
        self.assertEqual(caught.exception.code, "coordinator_env_not_allowlisted")

    def test_next_start_reapplies_sealed_value_over_an_inherited_one(self):
        secret = "sealed-typesafe-survives-restart"
        self._install("TYPESAFE_API_KEY", secret)
        self.app.store.close()
        os.environ["TYPESAFE_API_KEY"] = "inherited-typesafe-loses"
        self.app = Service(empty_config(), self.data)
        self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), secret)
        self.assertTrue(self.app.capabilities()["semantic_gate_key"])
        self._assert_secret_absent(secret)

    def test_inherited_value_stays_when_nothing_is_sealed(self):
        os.environ["TAVILY_API_KEY"] = "inherited-tavily-only"
        other = Path(self.tmp.name) / "other"
        app = Service(empty_config(), other)
        try:
            self.assertEqual(os.environ.get("TAVILY_API_KEY"), "inherited-tavily-only")
            row = next(item for item in app.onboarding.status()["coordinator_env"] if item["name"] == "TAVILY_API_KEY")
            self.assertEqual(row, {"name": "TAVILY_API_KEY", "configured": True, "sealed": False})
        finally:
            app.store.close()

    def test_web_source_posts_the_sealed_envelope(self):
        root = Path(__file__).resolve().parents[1]
        setup = (root / "web-src/components/Setup.tsx").read_text(encoding="utf-8")
        self.assertIn("sealCredential(secret, target)", setup)
        self.assertIn("api('setup/coordinator-env', {name, sealed})", setup)
        self.assertNotIn("setup/coordinator-env', {name, secret", setup)
        self.assertNotIn("setup/coordinator-env', {secret", setup)
        self.assertIn("operator_action_code === 'set_coordinator_env'", setup)
        self.assertIn("data-env-var={envVar}", setup)
        for name in ("en-US.ts", "zh-CN.ts"):
            text = (root / "web-src/i18n" / name).read_text(encoding="utf-8")
            for key in ("saveCoordinatorEnv", "coordinatorEnvHint", "coordinator_env_not_allowlisted",
                        "sealed_credential_required", "sealed_credential_rejected", "invalid_coordinator_env"):
                self.assertIn(key, text, name)
        runbook = (root / "docs/RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("TYPESAFE_API_KEY", runbook)
        self.assertIn("TAVILY_API_KEY", runbook)
        self.assertIn("凭据库", runbook)


class CoordinatorEnvHttpTests(unittest.TestCase):
    def setUp(self):
        self._env = {name: os.environ.get(name) for name in ("TYPESAFE_API_KEY", "TAVILY_API_KEY")}
        for name in self._env:
            os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(empty_config(), self.root / "data")
        self.token = "h" * 32
        self.server = serve(self.app, self.token, port=0)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.store.close()
        self.tmp.cleanup()
        _restore(self._env)

    def _request(self, path, body=None, origin=True, key="env-http"):
        headers = {}
        if origin:
            headers["Origin"] = self.origin if origin is True else origin
        if body is not None:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
        request = urllib.request.Request(
            self.origin + "/api/v1/" + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers)
        return self.client.open(request)

    def _login(self):
        with self._request("login", {"token": self.token}, key="login") as response:
            self.assertEqual(response.status, 200)

    def _error(self, path, body):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._request(path, body, key="reject-" + path.replace("/", "-"))
        payload = json.loads(caught.exception.read().decode())
        return caught.exception.code, payload

    def test_http_sealed_roundtrip_hides_the_secret(self):
        secret = "http-sealed-typesafe-value"
        self._login()
        with self._request("setup/coordinator-env/target?name=TYPESAFE_API_KEY") as response:
            target = json.load(response)
        self.assertEqual(target["name"], "TYPESAFE_API_KEY")
        self.assertEqual(target["context"], "coordinator-env:TYPESAFE_API_KEY")
        self.assertNotIn("secret", target)
        sealed = _seal(target["public_key"], target["context"], secret)
        with self._request("setup/coordinator-env", {"name": "TYPESAFE_API_KEY", "sealed": sealed}, key="save-1") as response:
            saved = json.load(response)
        self.assertEqual(saved, {"name": "TYPESAFE_API_KEY", "configured": True, "sealed": True})
        self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), secret)
        rotated = "http-sealed-typesafe-value-rotated"
        again = _seal(target["public_key"], target["context"], rotated)
        with self._request("setup/coordinator-env", {"name": "TYPESAFE_API_KEY", "sealed": again}, key="save-1") as response:
            self.assertEqual(json.load(response)["configured"], True)
        self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), rotated)
        with self._request("snapshot") as response:
            snap = json.load(response)
        self.assertNotIn(rotated, json.dumps(snap))
        row = next(item for item in snap["setup"]["coordinator_env"] if item["name"] == "TYPESAFE_API_KEY")
        self.assertEqual(row["sealed"], True)
        status, payload = self._error("setup/coordinator-env", {"name": "TYPESAFE_API_KEY", "secret": "plaintext-http-secret"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "sealed_credential_required")
        self.assertNotIn("plaintext-http-secret", json.dumps(payload))
        self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), rotated)
        status, payload = self._error("setup/coordinator-env", {"name": "PANGPANG_TOKEN", "sealed": sealed})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "coordinator_env_not_allowlisted")
        self.assertNotIn(rotated.encode(), (self.root / "data" / "vault" / "secrets.db").read_bytes())
        self.assertNotIn(rotated.encode(), (self.root / "data" / "bots.db").read_bytes())

    def test_target_rejects_unknown_names_and_requires_login(self):
        with self.assertRaises(urllib.error.HTTPError) as anon:
            self._request("setup/coordinator-env/target?name=TYPESAFE_API_KEY")
        self.assertEqual(anon.exception.code, 401)
        self._login()
        status, payload = self._error("setup/coordinator-env/target?name=PANGPANG_TOKEN", None)
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "coordinator_env_not_allowlisted")
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self._request("setup/coordinator-env", {"name": "TYPESAFE_API_KEY", "sealed": {}}, origin=False, key="no-origin")
        self.assertIn(denied.exception.code, (401, 403))


if __name__ == "__main__":
    unittest.main()
