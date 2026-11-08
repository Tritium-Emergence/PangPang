"""Device upgrade over the real HTTP surface (Repair #86).

The upgrade route must not run inside Store.once: broker.request polls while the
device is supposed to answer through a concurrent /connector/sync. The key test
starts the HTTP upgrade, then completes its command through a REAL sync request
and expects the original fault code back — if the route were still transactional
the sync could not commit until the upgrade timed out.

Preflight is deliberately narrow: only a device that reported source build/unknown
is rejected before a command exists. A device row without a `source` field still
goes to the connector, which owns the install checks.
"""
import http.cookiejar
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config

from pangpang import store as store_module
from pangpang.http import serve
from pangpang.service import Service
from pangpang.store import set_strict_txn_io


HOST = "pc"
DEVICE_KEY = "d" * 40
INSTANCE = "device-" + "c" * 32
TARGET = "a" * 40


class DeviceUpgradeHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.app.execution.hosts[HOST] = {
            "id": HOST, "transport": "connector", "os": "Linux",
            "allowed_roots": [str(self.root)], "credential_env": "TEST_DEVICE_KEY",
        }
        self.app.store.put("device", {"id": HOST, "revoked": False,
                                      "instance_id": INSTANCE, "source": "git"})
        # Volatile presence, same fact a sync publishes. The tests that need a
        # sync still exercise the real route.
        self.app.broker._last_seen[HOST] = time.time()
        self.token = "x" * 32
        self.server = serve(self.app, self.token, port=0)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._strict = store_module._strict_txn_io[0]
        # The production invariant: any network wait under a store transaction
        # raises io_inside_transaction instead of silently blocking a device.
        set_strict_txn_io(True)
        self.login()

    def tearDown(self):
        set_strict_txn_io(self._strict)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.store.close()
        self.tmp.cleanup()

    # --- HTTP helpers -----------------------------------------------------

    def post(self, path, body, key="upgrade-test"):
        request = urllib.request.Request(
            self.origin + "/api/v1/" + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Origin": self.origin,
                     "Idempotency-Key": key},
        )
        try:
            with self.client.open(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def login(self):
        status, payload = self.post("login", {"token": self.token},
                                    key="login-device-upgrade")
        self.assertEqual(status, 200, payload)

    def sync(self, **extra):
        body = {"instance_id": INSTANCE, "reports": [], "receipts": [],
                "source": "git"}
        body.update(extra)
        request = urllib.request.Request(
            self.origin + "/api/v1/connector/sync",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-PangPang-Host": HOST,
                     "Authorization": "Bearer " + DEVICE_KEY},
        )
        with patch.dict(os.environ, {"TEST_DEVICE_KEY": DEVICE_KEY}):
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
                return json.load(response)

    def wait_for_command(self):
        """Poll through the real sync until the upgrade command is delivered."""
        deadline = time.time() + 10
        while time.time() < deadline:
            result = self.sync()
            if result["commands"]:
                return result["commands"][0]
            time.sleep(0.05)
        self.fail("upgrade command was never delivered to the connector")

    def run_upgrade_async(self):
        outcome = {}

        def run():
            try:
                outcome["response"] = self.post(
                    "devices/" + HOST + "/upgrade", {"target_commit": TARGET},
                    key="upgrade-async")
            except BaseException as error:  # noqa: BLE001 - surfaced below
                outcome["error"] = error

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, outcome

    def commands(self):
        return self.app.store.find("command", host_id=HOST)

    def assert_not_500(self, status, payload):
        self.assertNotEqual(status, 500, payload)
        self.assertNotEqual(payload.get("code"), "internal_error", payload)
        self.assertNotIn("io_inside_transaction", json.dumps(payload))

    # --- 1. preflight -----------------------------------------------------

    def test_non_git_source_is_rejected_before_any_command(self):
        for source in ("build", "unknown"):
            with self.subTest(source=source):
                device = self.app.store.get("device", HOST)
                device["source"] = source
                self.app.store.put("device", device)
                status, payload = self.post("devices/" + HOST + "/upgrade",
                                            {"target_commit": TARGET})
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload.get("code"), "install_not_git_checkout")
                self.assertEqual(self.commands(), [])

    def test_missing_source_still_reaches_the_connector(self):
        # An older device that never reported `source` is unknown, not "not git".
        self.app.store.put("device", {"id": HOST, "revoked": False,
                                      "instance_id": INSTANCE})
        self.assertNotIn("source", self.app.store.get("device", HOST))
        # No preflight rejection: a command is created and then waits for the
        # connector (which owns the real install check). Short-circuit the wait.
        with patch("pangpang.broker.time.monotonic", side_effect=[0.0, 100.0]):
            status, payload = self.post("devices/" + HOST + "/upgrade",
                                        {"target_commit": TARGET})
        self.assert_not_500(status, payload)
        self.assertEqual(payload.get("code"), "connector_operation_pending")
        created = self.commands()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["payload"], {"target_commit": TARGET})

    # --- 2. critical path: real sync completes the waiting request --------

    def test_request_wait_is_not_blocked_by_a_transaction(self):
        thread, outcome = self.run_upgrade_async()
        command = self.wait_for_command()
        self.assertEqual(command["action"], "upgrade")
        self.assertEqual(command["payload"], {"target_commit": TARGET})
        # A second REAL sync writes the device's own late error while the
        # upgrade HTTP request is still waiting.
        self.sync(reports=[{"id": command["id"],
                            "result": {"error": "upgrade_blocked_active_runs"}}])
        stored = self.app.store.get("command", command["id"])
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["result"], {"error": "upgrade_blocked_active_runs"})
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "upgrade request never returned")
        self.assertNotIn("error", outcome)
        status, payload = outcome["response"]
        # The original device fault survives the round trip.
        self.assertEqual(payload.get("code"), "upgrade_blocked_active_runs", payload)
        self.assert_not_500(status, payload)
        self.assertTrue(store_module._strict_txn_io[0])

    # --- 4. success shapes ------------------------------------------------

    def test_success_shapes_are_returned_at_top_level(self):
        cases = [
            ({"status": "already_current", "commit": TARGET},
             lambda status, payload: (
                 self.assertEqual(status, 200),
                 self.assertEqual(payload.get("status"), "already_current"),
                 self.assertFalse(payload.get("restarting")))),
            ({"status": "updated", "commit": TARGET, "restarting": True},
             lambda status, payload: (
                 self.assertEqual(status, 200),
                 self.assertEqual(payload.get("status"), "updated"),
                 self.assertIs(payload.get("restarting"), True))),
        ]
        for result, check in cases:
            with self.subTest(status=result["status"]):
                thread, outcome = self.run_upgrade_async()
                command = self.wait_for_command()
                self.sync(reports=[{"id": command["id"], "result": result}])
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
                self.assertNotIn("error", outcome)
                status, payload = outcome["response"]
                check(status, payload)

    # --- 5. pending timeout ----------------------------------------------

    def test_wait_timeout_reports_pending_not_failure_or_success(self):
        with patch("pangpang.broker.time.monotonic", side_effect=[0.0, 100.0]):
            status, payload = self.post("devices/" + HOST + "/upgrade",
                                        {"target_commit": TARGET})
        self.assertEqual(payload.get("code"), "connector_operation_pending", payload)
        self.assert_not_500(status, payload)
        # The durable command is not cancelled by the wait timing out.
        created = self.commands()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["status"], "pending")

    # --- 7. offline / revoked / non-connector do not regress --------------

    def test_offline_and_revoked_still_refuse_without_a_command(self):
        self.app.broker._last_seen.pop(HOST, None)
        status, payload = self.post("devices/" + HOST + "/upgrade",
                                    {"target_commit": TARGET})
        self.assertEqual(payload.get("code"), "connector_offline")
        self.assertEqual(self.commands(), [])

        device = self.app.store.get("device", HOST)
        device["revoked"] = True
        self.app.store.put("device", device)
        self.app.broker._last_seen[HOST] = time.time()
        status, payload = self.post("devices/" + HOST + "/upgrade",
                                    {"target_commit": TARGET})
        self.assertEqual(payload.get("code"), "connector_offline")
        self.assertEqual(self.commands(), [])

    def test_non_connector_and_bad_target_are_rejected(self):
        status, payload = self.post("devices/local/upgrade", {"target_commit": TARGET})
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("code"), "upgrade_requires_connector")

        status, payload = self.post("devices/" + HOST + "/upgrade",
                                    {"target_commit": "not-a-sha"})
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("code"), "invalid_target_commit")
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    unittest.main()
