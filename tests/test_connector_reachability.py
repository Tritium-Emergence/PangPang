"""Connector progress copy follows the live online bit.

last_observation_error is not cleared on a successful poll, so it must not
keep a reconnected device looking offline. Internal status stays running.
Terminal and cancel_requested copy win. A connector with no report is
dispatched, not in-progress execution.
"""
import http.cookiejar
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from test_bot_runtime import config

from pangpang.activity import (
    annotate_dependency_reachability,
    annotate_snapshot_tasks,
    connector_reachability,
    task_reachability,
)
from pangpang.attention import work_progress_phase
from pangpang.http import serve
from pangpang.runtime import Runtime
from pangpang.service import Service


_EXEC = "exec-" + "a" * 32


def _task(**extra):
    row = {"id": "task-reach", "status": "running", "host_id": "pc", "work_id": "work-reach",
           "owner_bot_id": "bot-main", "execution_id": _EXEC, "created": time.time()}
    row.update(extra)
    return row


class PureReachabilityTests(unittest.TestCase):
    def test_online_bit_wins_over_a_leftover_observation_error(self):
        stale = {"status": "running", "last_observation_error": "connector_offline"}
        offline = connector_reachability(stale, transport="connector", online=False)
        online = connector_reachability(stale, transport="connector", online=True)
        self.assertEqual(offline["phase"], "unreachable")
        self.assertIn("cannot be reached", offline["note"])
        self.assertNotIn("connector_offline", offline["note"])
        self.assertEqual(online["phase"], "dispatched")
        self.assertIn("not in-progress", online["note"])
        self.assertEqual(stale["status"], "running")
        cleared = {"status": "running", "last_observation_error": None}
        self.assertEqual(
            connector_reachability(cleared, transport="connector", online=False)["phase"],
            "unreachable")

    def test_terminal_and_cancel_copy_win_and_silence_is_not_execution(self):
        for status in ("succeeded", "failed", "cancelled", "cancel_requested"):
            self.assertIsNone(connector_reachability(
                {"status": status, "last_observation_error": "connector_offline"},
                transport="connector", online=False))
        queued = connector_reachability({"status": "queued"}, transport="connector", online=True)
        self.assertEqual(queued["phase"], "dispatched")
        self.assertIsNone(queued["last_log_at"])
        quiet = connector_reachability({"status": "running"}, transport="connector", online=True)
        self.assertEqual(quiet["phase"], "dispatched")
        self.assertNotIn("in-progress execution.", quiet["note"].split("not ", 1)[0])
        active = connector_reachability(
            {"status": "running"}, transport="connector", online=True,
            activity={"last_log_at": 1_700_000_100, "recent": [{"tool": "read", "path": "a.py"}]})
        self.assertEqual(active["phase"], "active")
        self.assertEqual(active["last_log_at"], 1_700_000_100)
        # Offline wins over a leftover report: the machine cannot be reached now.
        self.assertEqual(connector_reachability(
            {"status": "running"}, transport="connector", online=False,
            activity={"last_log_at": 1_700_000_100, "recent": [{"tool": "read"}]})["phase"],
            "unreachable")
        self.assertIsNone(connector_reachability(
            {"status": "running"}, transport="local", online=False))

    def test_attention_phase_does_not_invent_a_state(self):
        work = {"id": "w", "status": "active"}
        tasks = [{"id": "t", "work_id": "w", "status": "running",
                  "reachability": {"phase": "unreachable"}}]
        self.assertEqual(work_progress_phase(work, tasks), "unreachable")
        tasks[0]["reachability"] = {"phase": "active", "last_log_at": 10}
        self.assertIsNone(work_progress_phase(work, tasks))
        work["status"] = "delivered"
        tasks[0]["reachability"] = {"phase": "unreachable"}
        self.assertIsNone(work_progress_phase(work, tasks))
        tasks[0]["status"] = "cancel_requested"
        self.assertIsNone(connector_reachability(tasks[0], transport="connector", online=False))


class ServiceReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.app.execution.hosts["pc"] = {
            "id": "pc", "transport": "connector", "os": "Linux",
            "allowed_roots": [str(self.root)],
        }
        self.app.store.put("device", {"id": "pc", "revoked": False})
        self.app.store.put("work", {
            "id": "work-reach", "owner_bot_id": "bot-main", "goal": "看设备",
            "status": "active", "created": time.time(),
        })

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _put(self, **extra):
        return self.app.store.put("task", _task(**extra))

    def test_projection_follows_devices_online_and_keeps_status_running(self):
        task = self._put(last_observation_error="connector_offline")
        self.app._record_observation(task, "poll", "connector_offline")
        stored = self.app.store.get("task", task["id"])
        self.assertEqual(stored["status"], "running")
        self.assertGreaterEqual(stored.get("observation_failures", 0), 1)
        self.assertEqual(stored["last_observation_error"], "connector_offline")
        self.assertFalse(self.app.broker.online("pc"))
        self.assertFalse(any(item["id"] == "pc" and item["online"] for item in self.app.broker.devices()))

        offline = task_reachability(self.app, stored)
        self.assertEqual(offline["phase"], "unreachable")
        self.assertEqual(stored["status"], "running")

        self.app.broker._last_seen["pc"] = time.time()
        self.assertTrue(any(item["id"] == "pc" and item["online"] for item in self.app.broker.devices()))
        # The error field is still the old code. The phase follows online.
        stored = self.app.store.get("task", task["id"])
        self.assertEqual(stored["last_observation_error"], "connector_offline")
        dispatched = task_reachability(self.app, stored)
        self.assertEqual(dispatched["phase"], "dispatched")
        self.assertNotIn("connector_offline", json.dumps(dispatched))

        sent = self.app.send("bot-main", {"text": "看看"}, "reach-turn")
        turn = self.app.store.get("turn", sent["turn_id"])
        view = Runtime(self.app).invoke(turn, "inspect_task", {"task_id": task["id"]})
        self.assertEqual(view["status"], "running")
        self.assertEqual(view["reachability"]["phase"], "dispatched")
        self.assertNotIn("last_observation_error", view)

        dep = {"kind": "task", "status": "pending", "target": {"task_id": task["id"], "status": "running"}}
        annotate_dependency_reachability(self.app, [dep])
        self.assertEqual(dep["target"]["reachability"]["phase"], "dispatched")

        self.app.broker._activity[_EXEC] = {
            "last_log_at": 1_700_000_100, "observation_capped": "no",
            "recent": [{"tool": "read", "path": "src/a.py"}],
        }
        active = task_reachability(self.app, stored)
        self.assertEqual(active["phase"], "active")
        self.assertEqual(active["last_log_at"], 1_700_000_100)
        row = dict(stored)
        annotate_snapshot_tasks(self.app, [row])
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["reachability"]["phase"], "active")
        self.assertEqual(row["activity"]["last_log_at"], 1_700_000_100)

        self.app.broker._last_seen.pop("pc", None)
        again = dict(self.app.store.get("task", task["id"]))
        annotate_snapshot_tasks(self.app, [again])
        self.assertEqual(again["reachability"]["phase"], "unreachable")
        self.assertEqual(again["status"], "running")
        self.assertEqual(self.app.store.get("task", task["id"])["status"], "running")

    def test_snapshot_phase_matches_devices_online(self):
        self._put(last_observation_error="connector_offline")
        token = "y" * 32
        server = serve(self.app, token, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = "http://127.0.0.1:" + str(server.server_port)
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            login = urllib.request.Request(
                origin + "/api/v1/login",
                data=json.dumps({"token": token}).encode(),
                headers={"Content-Type": "application/json", "Origin": origin, "Idempotency-Key": "login-reach"})
            client.open(login).close()

            def snapshot():
                request = urllib.request.Request(origin + "/api/v1/snapshot", headers={"Origin": origin})
                with client.open(request) as response:
                    return json.load(response)

            offline = snapshot()
            task = next(item for item in offline["tasks"] if item["id"] == "task-reach")
            work = next(item for item in offline["work"] if item["id"] == "work-reach")
            device = next(item for item in offline["devices"] if item["id"] == "pc")
            self.assertFalse(device["online"])
            self.assertEqual(task["status"], "running")
            self.assertEqual(task["reachability"]["phase"], "unreachable")
            self.assertEqual(work["attention"], "working")
            self.assertEqual(work["progress_phase"], "unreachable")
            self.assertEqual(task["last_observation_error"], "connector_offline")

            self.app.broker._last_seen["pc"] = time.time()
            online = snapshot()
            task = next(item for item in online["tasks"] if item["id"] == "task-reach")
            work = next(item for item in online["work"] if item["id"] == "work-reach")
            device = next(item for item in online["devices"] if item["id"] == "pc")
            self.assertTrue(device["online"])
            self.assertEqual(task["last_observation_error"], "connector_offline")
            self.assertEqual(task["status"], "running")
            self.assertEqual(task["reachability"]["phase"], "dispatched")
            self.assertEqual(work["progress_phase"], "dispatched")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
