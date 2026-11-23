"""Confirmation cards wait for a click. They do not die on a clock.

An already-expired field card cannot be applied. Deny retires it, the
pending panel drops it, and the same setup can be requested again.
Switching Work does not keep that global card on the chat header.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Fault, Store, uid
from pangpang.vault import Vault

PENDING = ("needs_approval", "needs_secret", "configuring")


class SetupClickWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.data = self.root.parent / "cloud"
        self.app = Service(empty_config(), self.data)
        self.addCleanup(self.app.store.close)

    def paired_host(self):
        device = self.root.parent / "device"
        device.mkdir(exist_ok=True)
        store = Store(device / "connector.db")
        identity = uid("device")
        store.put("identity", {"id": "device", "instance_id": identity})
        store.close()
        vault = Vault(device / "vault")
        pub = vault.public_key()
        vault.close()
        code = self.app.onboarding.pairing_code()["code"]
        pair = self.app.onboarding.pair({
            "code": code, "instance_id": identity, "public_key": pub, "name": "PC", "os": "Linux"})
        self.app.store.put("inventory", {"id": pair["host_id"], "items": [
            {"adapter": "codex", "binary": "codex", "version": "1.0"}]})
        return pair["host_id"]

    def panel(self):
        """The chat header reads this global list, not the current Work."""
        return [row["id"] for row in self.app.onboarding.status()["requests"]
                if row.get("status") in PENDING]

    def configure_commands(self, request_id):
        return [cmd for cmd in self.app.store.find("command", action="configure")
                if (cmd.get("payload") or {}).get("request_id") == request_id]

    def expire(self, request_id):
        row = self.app.store.get("setup_request", request_id)
        row["expires"] = time.time() - 5
        self.app.store.put("setup_request", row)
        return row

    def test_expired_apply_does_not_land_and_deny_clears_the_panel(self):
        host_id = self.paired_host()
        work = self.app.send("bot-main", {"text": "add executor"}, "click-wait")["work_id"]
        opened = self.app.onboarding.request("bot-main", work, {
            "action": "configure", "adapter": "codex", "host_id": host_id, "cwd": str(self.root)})
        request_id = opened["request_id"]
        self.assertNotIn("expires", self.app.store.get("setup_request", request_id))
        self.expire(request_id)
        before = set(self.app.execution.installations)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(request_id, {})
        self.assertEqual(ctx.exception.code, "setup_request_expired_or_used")
        self.assertEqual(set(self.app.execution.installations), before)
        self.assertEqual(self.configure_commands(request_id), [])
        self.assertEqual(self.app.store.get("setup_request", request_id)["status"], "needs_approval")
        other = self.app.send("bot-main", {"text": "leave this work"}, "other-work")["work_id"]
        self.assertNotEqual(other, work)
        self.assertIn(request_id, self.panel())

        self.assertEqual(self.app.onboarding.deny(request_id), {"denied": True})
        self.assertEqual(self.app.store.get("setup_request", request_id)["status"], "denied")
        self.assertNotIn(request_id, self.panel())
        self.assertEqual(self.configure_commands(request_id), [])

        again = self.app.onboarding.request("bot-main", other, {
            "action": "configure", "adapter": "codex", "host_id": host_id})
        self.assertNotEqual(again["request_id"], request_id)
        self.assertEqual(again["status"], "needs_approval")
        self.assertIn(again["request_id"], self.panel())
        self.assertNotIn(request_id, self.panel())
        self.app.onboarding.approve(again["request_id"], {})
        self.assertEqual(self.configure_commands(again["request_id"])[0]["host_id"], host_id)
        self.assertEqual(self.configure_commands(request_id), [])

    def test_expired_secret_card_can_be_dismissed_without_installing(self):
        host_id = self.paired_host()
        work = self.app.send("bot-main", {"text": "key"}, "secret-wait")["work_id"]
        opened = self.app.onboarding.request("bot-main", work, {
            "action": "configure", "adapter": "codex", "host_id": host_id, "auth": "api_key"})
        self.app.onboarding.approve(opened["request_id"], {})
        command = self.configure_commands(opened["request_id"])[0]
        installation = {"id": "codex-" + host_id, "host_id": host_id, "adapter": "codex",
                        "cwd": str(self.root)}
        self.app.onboarding.report(command, {"installation": installation})
        secret = self.app.onboarding.request("bot-main", work, {
            "action": "credential", "host_id": host_id, "installation_id": installation["id"]})
        self.assertEqual(secret["status"], "needs_secret")
        self.expire(secret["request_id"])
        with self.assertRaises(Fault) as target:
            self.app.onboarding.secret_target(secret["request_id"])
        self.assertEqual(target.exception.code, "setup_request_expired_or_used")
        sealed = {"sealed": {"key": "aa", "nonce": "bb", "ciphertext": "cc"}}
        with self.assertRaises(Fault) as saved:
            self.app.onboarding.secret(secret["request_id"], sealed)
        self.assertEqual(saved.exception.code, "setup_request_expired_or_used")
        self.assertEqual(self.app.store.find("command", action="credential_install"), [])
        self.assertIn(secret["request_id"], self.panel())

        self.app.onboarding.deny(secret["request_id"])
        self.assertEqual(self.app.store.get("setup_request", secret["request_id"])["status"], "denied")
        self.assertNotIn(secret["request_id"], self.panel())
        self.assertEqual(self.app.store.find("command", action="credential_install"), [])

        fresh = self.app.onboarding.request("bot-main", work, {
            "action": "credential", "host_id": host_id, "installation_id": installation["id"]})
        self.assertNotEqual(fresh["request_id"], secret["request_id"])
        self.assertEqual(fresh["status"], "needs_secret")
        self.assertIn(fresh["request_id"], self.panel())
        target = self.app.onboarding.secret_target(fresh["request_id"])
        self.assertEqual(target["installation_id"], installation["id"])

    def test_waiting_card_does_not_die_because_time_passed(self):
        host_id = self.paired_host()
        work = self.app.send("bot-main", {"text": "wait"}, "still-valid")["work_id"]
        opened = self.app.onboarding.request("bot-main", work, {
            "action": "configure", "adapter": "codex", "host_id": host_id})
        row = self.app.store.get("setup_request", opened["request_id"])
        self.assertNotIn("expires", row)
        later = time.time() + 7200
        with patch("pangpang.onboarding.time.time", return_value=later):
            self.app.onboarding.approve(opened["request_id"], {})
        self.assertEqual(self.app.store.get("setup_request", opened["request_id"])["status"], "configuring")
        self.assertEqual(len(self.configure_commands(opened["request_id"])), 1)

        drifted = self.app.onboarding.request("bot-main", work, {
            "action": "configure", "adapter": "codex", "host_id": host_id, "cwd": str(self.root)})
        stored = self.app.store.get("setup_request", drifted["request_id"])
        stored["proposal"] = dict(stored["proposal"], base_revision="not-the-live-revision")
        self.app.store.put("setup_request", stored)
        with patch("pangpang.onboarding.time.time", return_value=later):
            with self.assertRaises(Fault) as ctx:
                self.app.onboarding.approve(drifted["request_id"], {})
        self.assertEqual(ctx.exception.code, "installation_config_changed")
        self.assertEqual(self.configure_commands(drifted["request_id"]), [])
        self.assertEqual(self.app.store.get("setup_request", drifted["request_id"])["status"], "needs_approval")
        self.assertIn(drifted["request_id"], self.panel())

    def test_startup_drops_a_deadline_that_has_not_passed(self):
        host_id = self.paired_host()
        work = self.app.send("bot-main", {"text": "legacy"}, "legacy-wait")["work_id"]
        opened = self.app.onboarding.request("bot-main", work, {
            "action": "configure", "adapter": "codex", "host_id": host_id})
        row = self.app.store.get("setup_request", opened["request_id"])
        row["expires"] = time.time() + 30
        self.app.store.put("setup_request", row)
        self.app.store.close()
        restarted = Service(empty_config(), self.data)
        self.addCleanup(restarted.store.close)
        stored = restarted.store.get("setup_request", opened["request_id"])
        self.assertNotIn("expires", stored)
        self.assertEqual(stored["status"], "needs_approval")
        later = time.time() + 7200
        with patch("pangpang.onboarding.time.time", return_value=later):
            restarted.onboarding.approve(opened["request_id"], {})
        self.assertEqual(restarted.store.get("setup_request", opened["request_id"])["status"], "configuring")
