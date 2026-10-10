import http.cookiejar
import json
import tempfile
import threading
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
        self.assertEqual(r["status"], "needs_approval")
        req = self.app.store.get("setup_request", r["request_id"])
        self.assertIsNone(req["bot_id"])
        self.assertIsNone(req["work_id"])
        self.assertEqual(req["origin"], "operator")
        self.assertEqual(req["proposal"], {"action": "configure", "adapter": "codex",
                                           "auth": "existing", "endpoint": "", "endpoint_source": "operator"})
        landed = self.report_landing(r["request_id"], host_id, "codex")
        self.assertEqual(self.app.execution.installations[landed]["adapter"], "codex")

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
        self.assertEqual(r["status"], "needs_approval")
        self.assertEqual(self.app.store.find("setup_request", status="needs_approval")[0]["host_id"], other_host)

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
        self.assertEqual(created["status"], "needs_approval")
        with self.request("snapshot") as r:
            snap = json.load(r)
        self.assertIn(created["request_id"], [q["id"] for q in snap["setup"]["requests"]])
        with self.request("setup/" + created["request_id"] + "/approve", {"endpoint": ""}) as r:
            self.assertTrue(json.load(r)["accepted"])

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
