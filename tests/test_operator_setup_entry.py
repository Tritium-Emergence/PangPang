import http.cookiejar
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pangpang.http import serve
from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Fault, Store, uid
from pangpang.vault import Vault


class OperatorSetupEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.app = Service(empty_config(), self.root.parent / "cloud")
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.tmp.cleanup)

    def paired_host(self, discover_adapters=("codex",)):
        data = self.root.parent / "device"
        data.mkdir(exist_ok=True)
        store = Store(data / "connector.db")
        identity = uid("device")
        store.put("identity", {"id": "device", "instance_id": identity})
        store.close()
        vault = Vault(data / "vault")
        pub = vault.public_key()
        vault.close()
        code = self.app.onboarding.pairing_code()["code"]
        pair = self.app.onboarding.pair({"code": code, "instance_id": identity, "public_key": pub,
                                         "name": "PC", "os": "Linux"})
        if discover_adapters:
            self.discover(pair["host_id"], *discover_adapters)
        return pair["host_id"]

    def discover(self, host_id, *adapters):
        self.app.store.put("inventory", {"id": host_id, "items": [
            {"adapter": a, "binary": a, "version": "1.0"} for a in adapters]})

    def command_for(self, request_id):
        return [c for c in self.app.store.find("command", action="configure")
                if c["payload"].get("request_id") == request_id][0]

    def report_landing(self, request_id, host_id, adapter, cwd=None):
        if self.app.store.get("setup_request", request_id)["status"] == "needs_approval":
            self.app.onboarding.approve(request_id, {})
        command = self.command_for(request_id)
        installation = {"id": adapter + "-" + host_id, "host_id": host_id, "adapter": adapter,
                        "cwd": cwd or str(self.root)}
        self.app.onboarding.report(command, {"installation": installation})
        return installation["id"]

    def test_operator_creates_configure_and_it_lands(self):
        host_id = self.paired_host()
        r = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                  "adapter": "codex"})
        self.assertEqual(r["status"], "configuring")
        self.assertTrue(r["accepted"])
        req = self.app.store.get("setup_request", r["request_id"])
        self.assertIsNone(req["bot_id"])
        self.assertIsNone(req["work_id"])
        self.assertEqual(req["origin"], "operator")
        self.assertEqual(req["status"], "configuring")
        self.assertEqual(req["proposal"]["adapter"], "codex")
        self.assertEqual(req["proposal"]["auth"], "existing")
        self.assertEqual(req["proposal"]["endpoint"], "")
        self.assertEqual(req["proposal"]["endpoint_source"], "operator")
        self.assertEqual(req["proposal"]["installation_id"], "codex-" + host_id)
        self.assertNotIn("cwd", req["proposal"])
        self.assertEqual(len(self.app.store.find("command", action="configure")), 1)
        again = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                      "adapter": "codex"})
        self.assertEqual(again["request_id"], r["request_id"])
        self.assertTrue(again["reused"])
        self.assertEqual(len(self.app.store.find("command", action="configure")), 1)
        landed = self.report_landing(r["request_id"], host_id, "codex")
        self.assertEqual(self.app.execution.installations[landed]["adapter"], "codex")
        grants = self.app.store.get("settings", "executor_grants")
        self.assertNotIn(landed, grants.get("grants") or {})
        self.assertFalse(self.app._grant_matches(self.app.execution.installations[landed]))
        parked = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                       "adapter": "codex"})
        self.assertEqual(parked["status"], "already_registered")
        self.assertEqual(len(self.app.store.find("command", action="configure")), 1)

    def test_cwd_passes_through_and_mismatch_still_refused(self):
        host_id = self.paired_host()
        cwd = str(self.root)
        r = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                  "adapter": "codex", "cwd": cwd})
        self.assertEqual(self.app.store.get("setup_request", r["request_id"])["proposal"]["cwd"], cwd)
        self.app.onboarding.approve(r["request_id"], {})
        command = self.command_for(r["request_id"])
        self.assertEqual(command["payload"]["cwd"], cwd)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.report(command, {"installation": {
                "id": "codex-" + host_id, "host_id": host_id, "adapter": "codex",
                "cwd": "/somewhere/else"}})
        self.assertEqual(ctx.exception.code, "installation_approval_mismatch")

    def test_gates(self):
        host_id = self.paired_host()
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": "local", "action": "configure", "adapter": "codex"})
        self.assertEqual(ctx.exception.code, "connector_host_required")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": host_id, "action": "configure", "adapter": "nope"})
        self.assertEqual(ctx.exception.code, "adapter_required")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": host_id, "action": "bogus"})
        self.assertEqual(ctx.exception.code, "invalid_setup_action")
        self.assertEqual(self.app.store.find("setup_request"), [])

    def test_field_set_and_inventory_gates(self):
        host_id = self.paired_host()
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                  "adapter": "codex", "argv": ["--evil"]})
        self.assertEqual(ctx.exception.code, "invalid_setup_request_field")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                  "adapter": "codex", "extra_argv": ["--x"]})
        self.assertEqual(ctx.exception.code, "invalid_setup_request_field")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": host_id, "action": "credential",
                                                  "installation_id": "codex-" + host_id, "env": {}})
        self.assertEqual(ctx.exception.code, "invalid_setup_request_field")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                  "adapter": "codex", "auth": "oauth"})
        self.assertEqual(ctx.exception.code, "invalid_setup_auth")
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                  "adapter": "claude-code"})
        self.assertEqual(ctx.exception.code, "adapter_not_in_inventory")
        other_host = self.paired_host(())
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": other_host, "action": "configure",
                                                  "adapter": "codex"})
        self.assertEqual(ctx.exception.code, "adapter_not_in_inventory")
        self.discover(other_host, "claude-code")
        r = self.app.onboarding.operator_request({"host_id": other_host, "action": "configure",
                                                  "adapter": "claude-code"})
        self.assertEqual(r["status"], "configuring")
        self.assertEqual(self.command_for(r["request_id"])["host_id"], other_host)

    def test_credential_request_for_landed_installation_opens_secret_window(self):
        host_id = self.paired_host()
        r = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                  "adapter": "codex", "auth": "api_key"})
        landed = self.report_landing(r["request_id"], host_id, "codex")
        other_host = self.paired_host()
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": other_host, "action": "credential",
                                                  "installation_id": landed})
        self.assertEqual(ctx.exception.code, "installation_host_mismatch")
        cred = self.app.onboarding.operator_request({"host_id": host_id, "action": "credential",
                                                     "installation_id": landed})
        self.assertEqual(cred["status"], "needs_secret")
        row = self.app.store.get("setup_request", cred["request_id"])
        self.assertIsNone(row["bot_id"])
        self.assertEqual(row["proposal"]["installation_id"], landed)

    def test_api_key_configure_reports_without_bot_and_stays_disabled(self):
        host_id = self.paired_host()
        r = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                  "adapter": "codex", "auth": "api_key"})
        landed = self.report_landing(r["request_id"], host_id, "codex")
        self.assertFalse(self.app.execution.installations[landed]["enabled"])
        self.assertEqual([q for q in self.app.store.find("setup_request") if q["status"] == "needs_secret"], [])
        self.assertEqual(self.app.store.get("setup_request", r["request_id"])["status"], "done")

    def test_bot_path_still_works_unchanged(self):
        host_id = self.paired_host()
        work = self.app.send("bot-main", {"text": "connect"}, "op-entry")["work_id"]
        req = self.app.onboarding.request("bot-main", work, {"action": "configure", "adapter": "codex",
                                                             "host_id": host_id})
        self.assertEqual(req["status"], "needs_approval")
        row = self.app.store.get("setup_request", req["request_id"])
        self.assertEqual(row["bot_id"], "bot-main")
        self.assertNotIn("origin", row)
        self.assertEqual(self.app.store.find("command", action="configure"), [])

    def test_chat_configure_does_not_reuse_or_approve(self):
        host_id = self.paired_host()
        work = self.app.send("bot-main", {"text": "add"}, "reuse")["work_id"]
        first = self.app.onboarding.request("bot-main", work, {"action": "configure", "adapter": "codex",
                                                               "host_id": host_id})
        second = self.app.onboarding.request("bot-main", work, {"action": "configure", "adapter": "codex",
                                                                "host_id": host_id, "auth": "existing"})
        self.assertNotEqual(second["request_id"], first["request_id"])
        self.assertEqual(self.app.store.get("setup_request", first["request_id"])["status"], "needs_approval")
        self.assertEqual(self.app.store.get("setup_request", second["request_id"])["status"], "needs_approval")
        self.assertEqual(self.app.store.find("command", action="configure"), [])

    def test_denied_and_expired_cards_are_not_treated_as_consent(self):
        host_id = self.paired_host()
        work = self.app.send("bot-main", {"text": "add"}, "deny")["work_id"]
        denied = self.app.onboarding.request("bot-main", work, {"action": "configure", "adapter": "codex",
                                                                "host_id": host_id})
        self.app.onboarding.deny(denied["request_id"])
        self.assertEqual(self.app.store.find("command", action="configure"), [])
        started = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                        "adapter": "codex"})
        self.assertNotEqual(started["request_id"], denied["request_id"])
        self.assertEqual(self.app.store.get("setup_request", denied["request_id"])["status"], "denied")
        self.assertEqual(self.command_for(started["request_id"])["payload"]["request_id"], started["request_id"])
        self.assertEqual(len(self.app.store.find("command", action="configure")), 1)

        other = self.paired_host()
        expired = self.app.onboarding.request("bot-main", work, {"action": "configure", "adapter": "codex",
                                                                 "host_id": other})
        row = self.app.store.get("setup_request", expired["request_id"])
        row["expires"] = time.time() - 5
        self.app.store.put("setup_request", row)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.approve(expired["request_id"], {})
        self.assertEqual(ctx.exception.code, "setup_request_expired_or_used")
        fresh = self.app.onboarding.operator_request({"host_id": other, "action": "configure", "adapter": "codex"})
        self.assertNotEqual(fresh["request_id"], expired["request_id"])
        self.assertEqual(self.app.store.get("setup_request", expired["request_id"])["status"], "needs_approval")
        self.assertEqual(self.command_for(fresh["request_id"])["host_id"], other)

    def test_a_different_scope_or_target_cannot_borrow_the_default_click(self):
        host_id = self.paired_host()
        work = self.app.send("bot-main", {"text": "add"}, "scope")["work_id"]
        cwd = self.app.onboarding.request("bot-main", work, {"action": "configure", "adapter": "codex",
                                                             "host_id": host_id, "cwd": str(self.root)})
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": host_id, "action": "configure", "adapter": "codex"})
        self.assertEqual(ctx.exception.code, "setup_scope_pending")
        self.assertEqual(self.app.store.get("setup_request", cwd["request_id"])["status"], "needs_approval")
        self.assertEqual(self.app.store.find("command", action="configure"), [])
        self.app.onboarding.deny(cwd["request_id"])
        self.assertEqual(self.app.store.find("command", action="configure"), [])

        self.discover(host_id, "codex", "claude-code")
        waiting = self.app.onboarding.request("bot-main", work, {"action": "configure", "adapter": "claude-code",
                                                                 "host_id": host_id, "auth": "api_key"})
        started = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure", "adapter": "codex"})
        self.assertEqual(started["status"], "configuring")
        self.assertEqual(self.app.store.get("setup_request", waiting["request_id"])["status"], "needs_approval")
        self.assertEqual(self.command_for(started["request_id"])["payload"]["adapter"], "codex")
        self.assertEqual(len(self.app.store.find("command", action="configure")), 1)

        other = self.paired_host()
        other_card = self.app.onboarding.request("bot-main", work, {"action": "configure", "adapter": "codex",
                                                                    "host_id": other})
        keyed = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                      "adapter": "codex", "auth": "api_key"})
        self.assertEqual(keyed["status"], "needs_approval")
        self.assertNotEqual(keyed["request_id"], other_card["request_id"])
        self.assertEqual(self.app.store.get("setup_request", other_card["request_id"])["status"], "needs_approval")
        self.assertEqual(len(self.app.store.find("command", action="configure")), 1)

    def test_default_click_does_not_approve_a_non_string_cwd_draft(self):
        host_id = self.paired_host()
        work = self.app.send("bot-main", {"text": "add"}, "r1")["work_id"]
        foreign = self.app.onboarding.request("bot-main", work, {
            "host_id": host_id, "action": "configure", "adapter": "codex",
            "cwd": True, "draft": {"cwd": "/work/other"}})
        stored = self.app.store.get("setup_request", foreign["request_id"])
        self.assertEqual(stored["proposal"]["draft"]["cwd"], "/work/other")
        self.assertIs(stored["proposal"]["cwd"], True)
        with self.assertRaises(Fault) as ctx:
            self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                  "adapter": "codex"})
        self.assertEqual(ctx.exception.code, "setup_scope_pending")
        self.assertEqual(self.app.store.get("setup_request", foreign["request_id"])["status"], "needs_approval")
        self.assertEqual(self.app.store.get("setup_request", foreign["request_id"])["proposal"]["draft"]["cwd"],
                         "/work/other")
        self.assertEqual(self.app.store.find("command", action="configure"), [])

    def test_cwd_key_of_any_shape_stays_a_card(self):
        host_id = self.paired_host()
        for value in (None, "", True, 1, "/work/other"):
            opened = self.app.onboarding.operator_request({"host_id": host_id, "action": "configure",
                                                           "adapter": "codex", "cwd": value})
            self.assertEqual(opened["status"], "needs_approval", value)
            self.assertEqual(self.app.store.find("command", action="configure"), [])
            self.app.onboarding.deny(opened["request_id"])

    def test_discover_still_runs_without_an_approval_card(self):
        host_id = self.paired_host(())
        work = self.app.send("bot-main", {"text": "look"}, "discover")["work_id"]
        found = self.app.onboarding.request("bot-main", work, {"action": "discover", "host_id": host_id})
        self.assertIn("command_id", found)
        self.assertEqual(self.app.store.find("setup_request"), [])
        command = self.app.store.get("command", found["command_id"])
        self.assertEqual(command["action"], "discover")
        self.assertEqual(command["payload"], {"bot_id": "bot-main", "work_id": work})


class OperatorSetupHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.app = Service(empty_config(), self.root.parent / "cloud")
        self.token = "x" * 32
        self.server = serve(self.app, self.token, port=0)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, body=None, client=None, key="op-http"):
        headers = {"Origin": self.origin}
        if body is not None:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
        req = urllib.request.Request(self.origin + "/api/v1/" + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers=headers)
        return (client or self.client).open(req)

    def login(self):
        with self.request("login", {"token": self.token}, key="login") as r:
            self.assertEqual(r.status, 200)

    def paired_host(self):
        data = self.root.parent / "device"
        data.mkdir(exist_ok=True)
        store = Store(data / "connector.db")
        identity = uid("device")
        store.put("identity", {"id": "device", "instance_id": identity})
        store.close()
        vault = Vault(data / "vault")
        pub = vault.public_key()
        vault.close()
        code = self.app.onboarding.pairing_code()["code"]
        pair = self.app.onboarding.pair({"code": code, "instance_id": identity, "public_key": pub,
                                         "name": "PC", "os": "Linux"})
        self.app.store.put("inventory", {"id": pair["host_id"], "items": [
            {"adapter": "codex", "binary": "codex", "version": "1.0"}]})
        return pair["host_id"]

    def test_unauthenticated_rejected_and_operator_flow_over_http(self):
        host_id = self.paired_host()
        anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("setup/request", {"host_id": host_id, "action": "configure", "adapter": "codex"},
                         client=anon)
        self.assertIn(denied.exception.code, (401, 403))
        self.assertEqual(self.app.store.find("setup_request"), [])
        self.login()
        with self.assertRaises(urllib.error.HTTPError) as bad_host:
            self.request("setup/request", {"host_id": "local", "action": "configure", "adapter": "codex"})
        self.assertEqual(bad_host.exception.code, 409)
        with self.request("setup/request", {"host_id": host_id, "action": "configure", "adapter": "codex"}) as r:
            created = json.load(r)
        self.assertEqual(created["status"], "configuring")
        self.assertTrue(created["accepted"])
        with self.request("snapshot") as r:
            snap = json.load(r)
        self.assertIn(created["request_id"], [q["id"] for q in snap["setup"]["requests"]])
        with self.assertRaises(urllib.error.HTTPError) as already:
            self.request("setup/" + created["request_id"] + "/approve", {"endpoint": ""}, key="approve-again")
        self.assertEqual(already.exception.code, 409)

    def test_idempotency_key_required_replay_and_conflict(self):
        host_id = self.paired_host()
        self.login()
        body = {"host_id": host_id, "action": "configure", "adapter": "codex"}
        anonymous = urllib.request.Request(self.origin + "/api/v1/setup/request",
                                           data=json.dumps(body).encode(),
                                           headers={"Origin": self.origin, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as nokey:
            self.client.open(anonymous)
        self.assertEqual(nokey.exception.code, 400)
        self.assertEqual(self.app.store.find("setup_request"), [])
        with self.request("setup/request", body, key="idem-1") as r:
            created = json.load(r)
        with self.request("setup/request", body, key="idem-1") as r:
            replay = json.load(r)
        self.assertEqual(replay["request_id"], created["request_id"])
        self.assertEqual(len(self.app.store.find("setup_request")), 1)
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.request("setup/request", dict(body, adapter="codex", cwd="/x"), key="idem-1")
        self.assertEqual(conflict.exception.code, 409)
        self.assertEqual(len(self.app.store.find("setup_request")), 1)


if __name__ == "__main__":
    unittest.main()
