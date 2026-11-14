"""Operator toggle for a connector's allow_file_receive.

The connector enforces the flag on its own settings/managed document. A missing
key is off. The devices page sends set_file_receive; it does not invent a cloud
host field the connector never reads. The running process applies the new value
without a restart.
"""
import base64
import http.cookiejar
import json
import os
import subprocess
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.connector import Connector, policy_digest
from pangpang.http import serve
from pangpang.service import Service
from pangpang.store import Fault


def _b64(raw):
    return base64.b64encode(raw).decode()


class FileReceiveSettingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.device_root = self.root / "device-ws"
        self.cloud_root = self.root / "cloud-ws"
        self.device_root.mkdir()
        self.cloud_root.mkdir()
        local = config(self.device_root)
        local["installations"][0]["enabled"] = True
        local.update(host_id="local", cloud_url="http://127.0.0.1:1",
                     credential_env="TEST_DEVICE_KEY")
        self.seed = json.loads(json.dumps(local))
        self.assertNotIn("allow_file_receive", self.seed)
        self.connector = Connector(local, self.root / "device")
        cloud = config(self.device_root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY",
                                 name="Work-Windows")
        cloud["hosts"].append({
            "id": "coordinator", "name": "cloud", "transport": "local", "os": "Linux",
            "allowed_roots": [str(self.cloud_root)],
        })
        self.app = Service(cloud, self.root / "cloud")
        self.connector.request = lambda body: self.app.broker.sync("local", body)
        self.connector.tick()

    def tearDown(self):
        self.connector.close()
        self.app.store.close()
        self.tmp.cleanup()

    def await_operation(self, action):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(action)
            deadline = time.monotonic() + 25
            while not future.done() and time.monotonic() < deadline:
                self.connector.tick()
                time.sleep(0.02)
            return future.result(timeout=2)

    def _receive_error(self, marker):
        command = {"id": "cmd-" + marker, "action": "file", "expires": time.time() + 30,
                   "payload": {"action": "receive", "path": "pic.png", "data": _b64(b"png")}}
        self.connector.execute(command)
        return self.connector.store.get("local_command", command["id"])["result"]

    def test_missing_and_non_true_values_are_off(self):
        self.assertNotIn("allow_file_receive", self.connector.config)
        self.assertIs(self.connector.config.get("allow_file_receive") is True, False)
        self.assertEqual(self._receive_error("missing")["error"], "local_file_receive_disabled")
        for marker, value in (("false", False), ("text", "true"), ("one", 1), ("none", None)):
            self.connector.config["allow_file_receive"] = value
            self.assertEqual(self._receive_error(marker)["error"], "local_file_receive_disabled", value)
        self.connector.config.pop("allow_file_receive", None)
        device = self.app.store.get("device", "local")
        self.assertIs(device.get("allow_file_receive"), False)
        # A cloud observation cannot open the gate. The connector has not stored the key.
        device["allow_file_receive"] = True
        self.app.store.put("device", device)
        self.assertEqual(self._receive_error("cloud-lie")["error"], "local_file_receive_disabled")
        with self.assertRaises(Fault) as missing_row:
            self.connector.store.get("settings", "managed")
        self.assertEqual(missing_row.exception.status, 404)

    def test_api_enables_persists_and_disables_without_restart(self):
        digest_before = policy_digest(self.connector.config, "codex-local")
        raw = b"chat-image"
        staged = self.app.upload_file("bot-main", {
            "host_id": "coordinator", "name": "pic.png", "data": _b64(raw)})
        with self.assertRaises(Fault) as refused:
            self.await_operation(lambda: self.app._copy_upload_to_host(staged, "local"))
        self.assertEqual(refused.exception.code, "artifact_delivery_failed")
        self.assertEqual(refused.exception.__cause__.code, "local_file_receive_disabled")
        self.assertIn("allow_file_receive", refused.exception.advice)
        self.assertIn("允许接收文件", refused.exception.advice)
        self.assertIn("Work-Windows", refused.exception.advice)
        self.assertEqual(list(self.device_root.rglob("*pic.png")), [])

        with self.assertRaises(Fault) as direct:
            self.await_operation(lambda: self.app.execution.inspect_file(
                self.app.execution.hosts["local"], "pic.png", "receive", _b64(raw)))
        self.assertEqual(direct.exception.code, "local_file_receive_disabled")

        enabled = self.await_operation(lambda: self.app.set_host_file_receive("local", {"allow": True}))
        self.assertEqual(enabled, {"host_id": "local", "allow_file_receive": True, "restart_required": False})
        self.assertIs(self.connector.config.get("allow_file_receive"), True)
        stored = self.connector.store.get("settings", "managed")["config"]
        self.assertIs(stored.get("allow_file_receive"), True)
        self.assertIs(self.app.store.get("device", "local").get("allow_file_receive"), True)
        self.assertNotIn("allow_file_receive", self.app.execution.hosts["local"])
        self.assertEqual(policy_digest(self.connector.config, "codex-local"), digest_before)

        placed = self.await_operation(lambda: self.app._copy_upload_to_host(staged, "local"))
        self.assertEqual(Path(placed["path"]).read_bytes(), raw)
        self.assertEqual(Path(placed["path"]).parent.parent, self.device_root.resolve() / ".pangpang-inbox")

        # A new process loads the connector database, not a cloud host field.
        data = self.connector.data
        self.connector.close()
        restarted = Connector(self.seed, data)
        restarted.request = lambda body: self.app.broker.sync("local", body)
        self.connector = restarted
        self.assertIs(self.connector.config.get("allow_file_receive"), True)
        self.assertNotIn("allow_file_receive", self.seed)
        again = self.await_operation(lambda: self.app.upload_file("bot-main", {
            "host_id": "local", "name": "again.png", "data": _b64(b"again")}))
        self.assertEqual(Path(again["path"]).read_bytes(), b"again")

        disabled = self.await_operation(lambda: self.app.set_host_file_receive("local", {"allow": False}))
        self.assertIs(disabled["allow_file_receive"], False)
        self.assertIs(disabled["restart_required"], False)
        self.assertIs(self.connector.config.get("allow_file_receive"), False)
        self.assertIs(self.connector.store.get("settings", "managed")["config"].get("allow_file_receive"), False)
        with self.assertRaises(Fault) as off:
            self.await_operation(lambda: self.app.execution.inspect_file(
                self.app.execution.hosts["local"], "nope.png", "receive", _b64(b"no")))
        self.assertEqual(off.exception.code, "local_file_receive_disabled")
        self.assertEqual(list(self.device_root.rglob("*nope.png")), [])

    def test_stale_switch_result_does_not_clobber_newer_observation_or_revoke(self):
        """An older switch must not write the device row after it returns.

        Interleaving, with the first caller held after its command is done:
        a later switch's sync publishes true, then revoke commits. Releasing
        the first caller must leave both facts in place. Broker.authenticate
        still rejects the device credential.
        """
        paused = threading.Event()
        release = threading.Event()
        get_ready = threading.Event()
        get_resume = threading.Event()
        switch_thread = {}
        device_puts = []
        real_request = self.app.broker.request
        real_get = self.app.store.get
        real_put = self.app.store.put
        hold_get = {"on": False, "held": False}

        def request(host_id, action, payload):
            result = real_request(host_id, action, payload)
            if action == "set_file_receive" and payload.get("allow") is False:
                paused.set()
                self.assertTrue(release.wait(5), "older switch was not released")
            return result

        def get(kind, key):
            row = real_get(kind, key)
            if (kind == "device" and key == "local" and hold_get["on"] and not hold_get["held"]
                    and threading.get_ident() == switch_thread.get("id")):
                hold_get["held"] = True
                get_ready.set()
                self.assertTrue(get_resume.wait(5), "stale device read was not released")
            return row

        def put(kind, obj, expected=None):
            if kind == "device" and threading.get_ident() == switch_thread.get("id"):
                device_puts.append(dict(obj))
            return real_put(kind, obj, expected=expected)

        def turn_off():
            switch_thread["id"] = threading.get_ident()
            return self.app.set_host_file_receive("local", {"allow": False})

        self.app.broker.request = request
        self.app.store.get = get
        self.app.store.put = put
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(turn_off)
                deadline = time.monotonic() + 10
                while not paused.is_set() and time.monotonic() < deadline:
                    self.connector.tick()
                    time.sleep(0.02)
                self.assertTrue(paused.is_set(), "older switch never finished its command")
                self.assertIs(real_get("device", "local").get("allow_file_receive"), False)
                self.app.broker.enqueue("local", "set_file_receive", {"allow": True})
                deadline = time.monotonic() + 10
                while (real_get("device", "local").get("allow_file_receive") is not True
                       and time.monotonic() < deadline):
                    self.connector.tick()
                    time.sleep(0.02)
                self.assertIs(real_get("device", "local").get("allow_file_receive"), True)
                self.assertIs(self.connector.config.get("allow_file_receive"), True)
                # If the older caller re-reads the device row, freeze that snapshot
                # so a revoke can land before the caller writes it back.
                hold_get["on"] = True
                release.set()
                try:
                    result = future.result(timeout=1)
                except FutureTimeout:
                    self.assertTrue(get_ready.wait(1), "switch blocked without reading the device row")
                    self.app.broker.revoke("local")
                    get_resume.set()
                    result = future.result(timeout=5)
                else:
                    self.app.broker.revoke("local")
        finally:
            get_resume.set()
            release.set()
            self.app.broker.request = real_request
            self.app.store.get = real_get
            self.app.store.put = real_put

        self.assertEqual(result, {"host_id": "local", "allow_file_receive": False, "restart_required": False})
        self.assertEqual(device_puts, [])
        device = real_get("device", "local")
        self.assertIs(device.get("allow_file_receive"), True)
        self.assertIs(device.get("revoked"), True)
        self.assertIs(self.connector.config.get("allow_file_receive"), True)
        self.assertFalse(self.app.broker.online("local"))
        with patch.dict(os.environ, {"TEST_DEVICE_KEY": "d" * 40}):
            with self.assertRaises(Fault) as auth:
                self.app.broker.authenticate("local", "Bearer " + "d" * 40)
        self.assertEqual(auth.exception.code, "device_revoked")

    def test_rejects_local_host_bad_payload_and_offline(self):
        with self.assertRaises(Fault) as missing:
            self.app.set_host_file_receive("no-such", {"allow": True})
        self.assertEqual(missing.exception.code, "host_not_found")
        with self.assertRaises(Fault) as empty:
            self.app.set_host_file_receive("local", {})
        self.assertEqual(empty.exception.code, "allow_file_receive_required")
        with self.assertRaises(Fault) as bad:
            self.app.set_host_file_receive("local", {"allow": "true"})
        self.assertEqual(bad.exception.code, "invalid_allow_file_receive")
        self.assertNotIn("allow_file_receive", self.connector.config)

        other = self.root / "only-local"
        other.mkdir()
        local_app = Service(config(other), self.root / "only-local-data")
        try:
            with self.assertRaises(Fault) as local:
                local_app.set_host_file_receive("local", {"allow": True})
            self.assertEqual(local.exception.code, "file_receive_connector_only")
        finally:
            local_app.store.close()

        self.app.broker._last_seen["local"] = 0
        with self.assertRaises(Fault) as offline:
            self.app.set_host_file_receive("local", {"allow": True})
        self.assertEqual(offline.exception.code, "connector_offline")
        self.assertNotIn("allow_file_receive", self.connector.config)
        self.assertEqual(self.app.store.find("command", action="set_file_receive"), [])

    def test_http_toggle_is_outside_a_transaction(self):
        token = "h" * 32
        server = serve(self.app, token, port=0)
        origin = "http://127.0.0.1:" + str(server.server_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            login = urllib.request.Request(
                origin + "/api/v1/login", data=json.dumps({"token": token}).encode(),
                headers={"Content-Type": "application/json", "Origin": origin,
                         "Idempotency-Key": "login-file-receive"})
            with client.open(login) as response:
                self.assertEqual(response.status, 200)

            def post(allow, key):
                request = urllib.request.Request(
                    origin + "/api/v1/hosts/local/file-receive",
                    data=json.dumps({"allow": allow}).encode(),
                    headers={"Content-Type": "application/json", "Origin": origin,
                             "Idempotency-Key": key})
                with client.open(request) as response:
                    return response.status, json.load(response)

            status, payload = self.await_operation(lambda: post(True, "file-receive-on"))
            self.assertEqual(status, 200, payload)
            self.assertIs(payload["allow_file_receive"], True)
            self.assertIs(payload["restart_required"], False)
            self.assertIs(self.connector.config.get("allow_file_receive"), True)
            self.assertNotIn("io_inside_transaction", json.dumps(payload))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class FileReceiveUiCopyTests(unittest.TestCase):
    def test_switch_copy_points_at_my_computers(self):
        env = os.environ.copy()
        env.pop("TYPESAFE_API_KEY", None)
        proc = subprocess.run(
            ["node", "--experimental-strip-types", "--test", "tests/test_file_receive_ui.mjs"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)


if __name__ == "__main__":
    unittest.main()
