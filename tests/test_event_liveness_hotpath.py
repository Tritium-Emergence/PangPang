"""Regression guards for the event/liveness/Store hot path (control plane)."""
import http.cookiejar
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
import pangpang.store as store_module
from pangpang.http import serve
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault, uid


def device_events(app):
    return [e for e in app.store.events(0) if e["type"] == "device.changed"]


def all_events(app):
    out, after = [], 0
    while True:
        rows = app.store.events(after)
        if not rows:
            return out
        out += rows
        after = rows[-1]["seq"]


class HeartbeatLivenessTests(unittest.TestCase):
    """N1: liveness is a volatile observation, not a durable heartbeat write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cloud = config(root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        self.app = Service(cloud, root / "cloud")
        self.instance = "device-" + uuid.uuid4().hex
        self.body = {"instance_id": self.instance, "version": "1.0", "supported_contract_version": 2}

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_idle_heartbeats_write_no_device_rows_or_events(self):
        self.app.broker.sync("local", self.body)
        self.assertTrue(self.app.broker.online("local"))
        before = len(device_events(self.app))
        revision = self.app.store.get("device", "local")["revision"]
        for _ in range(30):
            self.app.broker.sync("local", self.body)
        self.assertEqual(len(device_events(self.app)), before)
        self.assertEqual(self.app.store.get("device", "local")["revision"], revision)
        self.assertNotIn("last_seen", self.app.store.get("device", "local"))
        self.assertTrue(self.app.broker.online("local"))

    def test_durable_facts_still_written_when_they_change(self):
        self.app.broker.sync("local", self.body)
        self.app.broker.sync("local", dict(self.body, supported_contract_version=1))
        self.assertEqual(self.app.store.get("device", "local")["supported_contract_version"], 1)
        self.app.broker.sync("local", dict(self.body, version="1.1"))
        self.assertEqual(self.app.store.get("device", "local")["version"], "1.1")
        with self.assertRaises(Fault): # instance binding is still durable truth
            self.app.broker.sync("local", dict(self.body, instance_id="device-" + uuid.uuid4().hex))

    def test_restart_reads_offline_until_first_sync_which_writes_nothing(self):
        self.app.broker.sync("local", self.body)
        self.app.broker._last_seen.clear() # a fresh process has observed nothing
        self.assertFalse(self.app.broker.online("local"))
        before = len(device_events(self.app))
        self.app.broker.sync("local", self.body)
        self.assertTrue(self.app.broker.online("local"))
        self.assertEqual(len(device_events(self.app)), before)

    def test_liveness_expires_after_90s_and_is_published_only_on_commit(self):
        self.app.broker.sync("local", self.body)
        self.app.broker._last_seen["local"] = time.time() - 91
        self.assertFalse(self.app.broker.online("local"))
        self.app.broker._last_seen.clear()
        with self.assertRaises(Fault): # rolled-back sync must not mark the device live
            self.app.broker.sync("local", dict(self.body, instance_id="device-" + uuid.uuid4().hex))
        self.assertFalse(self.app.broker.online("local"))

    def test_revoked_device_is_offline_despite_recent_heartbeat(self):
        self.app.broker.sync("local", self.body)
        self.app.broker.revoke("local")
        self.assertFalse(self.app.broker.online("local"))


class EventStreamContinuityTests(unittest.TestCase):
    """A/B: one server-side continuity rule; events are invalidations, never replay."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = Service(config(root), root / "data")
        self.token = "x" * 32
        self.server = serve(self.app, self.token, port=0)
        self.origin = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        req = urllib.request.Request(self.origin + "/api/v1/login", data=json.dumps({"token": self.token}).encode(),
                                     headers={"Origin": self.origin, "Content-Type": "application/json",
                                              "Idempotency-Key": "login"})
        self.client.open(req).close()

    def tearDown(self):
        # SSE handlers notice the closed client only on a write after it is gone
        # (1s ticks); let them exit before the store closes underneath them.
        time.sleep(2.5)
        self.server.shutdown(); self.server.server_close()
        self.app.store.close(); self.tmp.cleanup()

    def bump(self, n):
        for i in range(n):
            self.app.store.put("probe_x", {"id": "p%d" % i})

    def first_events(self, n=1, headers=None, query="", during=None):
        req = urllib.request.Request(self.origin + "/api/v1/events" + query, headers=headers or {})
        out = []
        with self.client.open(req, timeout=10) as resp:
            if during:
                during()
            current = {}
            while len(out) < n:
                line = resp.readline().decode().rstrip("\n")
                if line.startswith("id: "):
                    current["id"] = int(line[4:])
                elif line.startswith("data: "):
                    current["data"] = json.loads(line[6:])
                elif line == "" and current:
                    out.append(current); current = {}
        return out

    def head(self):
        return self.app.store.event_bounds()[1]

    def test_fresh_connection_resets_at_head_without_replaying_history(self):
        self.bump(500)
        first = self.first_events()[0]
        self.assertEqual(first["data"], {"type": "reset"})
        self.assertEqual(first["id"], self.head())

    def test_missing_cursor_is_not_zero_and_zero_is_a_legal_cursor(self):
        self.bump(3)
        first = self.first_events(headers={"Last-Event-ID": "0"})[0]
        self.assertEqual(first["id"], self.app.store.event_bounds()[0])
        self.assertNotEqual(first["data"].get("type"), "reset")
        self.assertEqual(self.first_events(query="?after=0")[0]["id"], self.app.store.event_bounds()[0])

    def test_write_racing_the_reset_is_streamed_not_lost(self):
        self.bump(5)
        head = self.head()
        events = self.first_events(2, during=lambda: self.app.store.put("probe_x", {"id": "late"}))
        self.assertEqual(events[0], {"id": head, "data": {"type": "reset"}})
        self.assertGreater(events[1]["id"], head)
        self.assertEqual(events[1]["data"]["entity_id"], "late")

    def test_cursor_ahead_of_head_after_restore_resets(self):
        self.bump(3)
        first = self.first_events(headers={"Last-Event-ID": str(self.head() + 1000)})[0]
        self.assertEqual(first, {"id": self.head(), "data": {"type": "reset"}})

    def test_retention_prunes_events_and_stale_cursor_resets_but_dedup_is_kept(self):
        self.app.store.once("scope", "key-1", {"a": 1}, lambda: {"ok": True})
        with patch.object(store_module, "EVENT_RETENTION", 50), patch.object(store_module, "EVENT_PRUNE_EVERY", 16):
            self.bump(300)
        floor, head = self.app.store.event_bounds()
        self.assertLessEqual(len(all_events(self.app)), 50 + 16)
        self.assertGreater(floor, 2)
        self.assertEqual(self.app.store.once_lookup("scope", "key-1", {"a": 1}), {"ok": True})
        first = self.first_events(headers={"Last-Event-ID": "1"})[0]
        self.assertEqual(first, {"id": head, "data": {"type": "reset"}})
        # floor-1 is still continuous: the next retained event is delivered as-is.
        cont = self.first_events(headers={"Last-Event-ID": str(floor - 1)})[0]
        self.assertEqual(cont["id"], floor)

    def test_seq_stays_monotonic_after_prune(self):
        with patch.object(store_module, "EVENT_RETENTION", 4), patch.object(store_module, "EVENT_PRUNE_EVERY", 4):
            self.bump(40)
        head = self.head()
        self.app.store.put("probe_x", {"id": "next"})
        self.assertEqual(self.head(), head + 1)


class RunningTurnInvariantTests(unittest.TestCase):
    """C: running-turn checks use the one_running_turn_per_work invariant index."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = Service(config(root), root / "data")

    def tearDown(self):
        self.app.store.close(); self.tmp.cleanup()

    def test_running_turn_found_beyond_1000_historical_turns(self):
        with self.app.store.atomic():
            for i in range(1001):
                self.app.store.put("turn", {"id": "t%04d" % i, "work_id": "w1", "status": "delivered",
                                            "history": [{"role": "assistant", "content": "x"}], "created": i})
            self.app.store.put("turn", {"id": "t-run", "work_id": "w1", "status": "running", "created": 5000})
        sched = Runtime(self.app).scheduler
        self.assertEqual(self.app.store.running_turn_id("w1"), "t-run")
        self.assertTrue(sched._work_running("w1"))
        self.assertFalse(sched._work_running("w1", exclude="t-run"))
        self.assertIsNone(self.app.store.running_turn_id("w2"))
        self.assertFalse(sched._work_running("w2"))

    def test_single_running_invariant_unchanged(self):
        self.app.store.put("turn", {"id": "a", "work_id": "w1", "status": "running", "created": 1})
        with self.assertRaises(Fault) as ctx:
            self.app.store.put("turn", {"id": "b", "work_id": "w1", "status": "running", "created": 2})
        self.assertEqual(ctx.exception.code, "work_already_running")


class StoreDecodeOutsideLockTests(unittest.TestCase):
    """N4: multi-row read primitives release Store.lock before JSON decode."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = Service(config(root), root / "data")
        self.store = self.app.store

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def assert_decodes_unlocked(self, call):
        real, owned = json.loads, []
        def spy(text, *a, **kw):
            owned.append(self.store.lock._is_owned())
            return real(text, *a, **kw)
        with patch.object(store_module.json, "loads", spy):
            result = call()
        self.assertTrue(owned, "expected at least one decoded row")
        self.assertFalse(any(owned), "JSON decoded while holding Store.lock")
        return result

    def test_find_find_due_history_and_corrections_decode_outside_lock(self):
        now = time.time()
        self.store.put("dependency", {"id": "d1", "status": "pending", "deadline": now - 5})
        self.store.put("message", {"id": "m1", "bot_id": "bot-main", "work_id": "w1", "kind": "user",
                                   "from": "user", "content": "独特词汇 unicornword", "created": now})
        self.store.put("routing_correction", {"id": "c1", "target_message_id": "m1", "new_work_id": "w2",
                                              "created": now})
        self.assertEqual(len(self.assert_decodes_unlocked(lambda: self.store.find("dependency"))), 1)
        self.assertEqual(len(self.assert_decodes_unlocked(
            lambda: self.store.find_due("dependency", "pending", "deadline", now))), 1)
        self.assertEqual(len(self.assert_decodes_unlocked(
            lambda: self.store.routing_corrections_for_target("m1", limit=4))), 1)
        if self.store.history_index_mode() != "scan":
            self.store.history_index_catch_up()
            rows = self.assert_decodes_unlocked(
                lambda: self.store.history_match('"unicornword"', bot_id="bot-main"))
            self.assertEqual([r["id"] for r in rows], ["m1"])


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")

    def tearDown(self):
        try:
            self.app.store.close()
        except Exception:
            pass
        self.tmp.cleanup()


class RoutingConstraintTests(ServiceCase):
    """F: chat text never becomes a durable host/installation pin."""

    def work(self, text, key, **body):
        m = self.app.send("bot-main", dict({"text": text}, **body), key)
        return self.app.store.get("work", m["work_id"])

    def test_mentions_in_free_text_do_not_pin(self):
        for i, text in enumerate(["codex-local 刚才失败了，换个执行器", "不要再用 codex-local",
                                  "local 上跑不动，换台设备", "请用 codex-local 在 local 跑"]):
            work = self.work(text, "t%d" % i)
            self.assertNotIn("required_installation_id", work, text)
            self.assertNotIn("required_host_id", work, text)

    def test_structured_body_still_pins_and_is_validated(self):
        work = self.work("跑一下", "explicit", host_id="local", installation_id="codex-local")
        self.assertEqual((work["required_host_id"], work["required_installation_id"]), ("local", "codex-local"))
        with self.assertRaises(Fault) as ctx:
            self.work("跑一下", "unknown", installation_id="no-such")
        self.assertEqual(ctx.exception.code, "unknown_routing_constraint")

    def test_restriction_is_named_visible_and_listed_not_auto_cleared(self):
        from pangpang.context import model_work_view
        from pangpang.service import ROUTE_ERROR
        work = self.work("跑一下", "legacy")
        work["required_installation_id"] = "inst-elsewhere" # a legacy pin of unknown origin
        work = self.app.store.put("work", work)
        from pangpang.connector import policy_digest
        self.app.store.put("probe", {"id": "codex-local", "available": True, "at": time.time(),
                                     "supervision": "windows-job-object",
                                     "policy_digest": policy_digest(self.app.config, "codex-local")})
        route = self.app.route("bot-main", work, {})
        reasons = {r["reason"] for r in route["rejected"]}
        self.assertIn("user_executor_constraint", reasons)
        self.assertEqual(ROUTE_ERROR["user_executor_constraint"], "work_routing_constraint")
        self.assertEqual(ROUTE_ERROR["user_host_constraint"], "work_routing_constraint")
        self.assertEqual(model_work_view(work)["required_installation_id"], "inst-elsewhere")
        listed = self.app.diagnostics()["work_routing_constraints"]
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["works"][0]["id"], work["id"])
        # Reopening the service never widens it.
        self.app.store.close()
        self.app = Service(config(self.root), self.root / "data")
        self.assertEqual(self.app.store.get("work", work["id"])["required_installation_id"], "inst-elsewhere")

    def test_offline_tool_clears_exactly_one_work(self):
        from pangpang import routing_constraint
        a = self.work("一", "a", installation_id="codex-local")
        b = self.work("二", "b", installation_id="codex-local")
        self.app.store.close()
        with patch("sys.stdout"):
            self.assertEqual(routing_constraint.main(["clear", "--data", str(self.root / "data"), "--work", a["id"]]), 0)
        self.app = Service(config(self.root), self.root / "data")
        self.assertNotIn("required_installation_id", self.app.store.get("work", a["id"]))
        self.assertEqual(self.app.store.get("work", b["id"])["required_installation_id"], "codex-local")


class RoutineEnvelopeClearTests(ServiceCase):
    """F ×: the offline tool must not widen an approved Routine envelope."""

    def test_routine_lineage_restriction_cannot_be_cleared_offline(self):
        from pangpang import routing_constraint
        self.app.store.put("probe", {"id": "codex-local", "available": True, "supervision": "windows-job-object"})
        sent = self.app.send("bot-main", {"text": "每天出日报"}, "routine-proposal")
        r = self.app.create_routine("bot-main", {"instruction": "日报", "timezone": "UTC",
                                                 "schedule": {"type": "daily", "time": "09:00"}},
                                    turn=self.app.store.get("turn", sent["turn_id"]))
        review = self.app.routine_review(self.app.store.get("routine", r["id"]))
        envelope = {"grant_write": True, "required_host_id": "local", "required_installation_id": "codex-local"}
        self.app.approve_routine("bot-main", r["id"], expected_digest=review["approval_digest"], authority=envelope)
        digest_before = self.app.routine_review(self.app.store.get("routine", r["id"]))["approval_digest"]
        work = self.app.store.get("work", self.app.run_routine_now("bot-main", r["id"])["work_id"])
        child_bot = self.app.create_bot({"name": "child", "role": "test"})
        child = self.app.store.get("work", self.app.delegate("bot-main", work["id"], {
            "target_bot_id": child_bot["id"], "goal": "c", "request": "r", "acceptance": ["d"],
            "mode": "return_to_sender"})["work_id"])
        legacy = self.app.store.get("work", self.app.send("bot-main", {"text": "x", "installation_id": "codex-local"},
                                                          "legacy")["work_id"])
        self.app.store.close()
        data = str(self.root / "data")
        with patch("sys.stdout"):
            self.assertEqual(routing_constraint.main(["clear", "--data", data, "--work", work["id"]]), 2)
            self.assertEqual(routing_constraint.main(["clear", "--data", data, "--work", child["id"]]), 2)
            self.assertEqual(routing_constraint.main(["clear", "--data", data, "--work", legacy["id"]]), 0)
        self.app = Service(config(self.root), self.root / "data")
        for w in (work, child):
            stored = self.app.store.get("work", w["id"])
            self.assertEqual((stored["required_host_id"], stored["required_installation_id"]), ("local", "codex-local"))
        self.assertNotIn("required_installation_id", self.app.store.get("work", legacy["id"]))
        self.assertEqual(self.app.routine_review(self.app.store.get("routine", r["id"]))["approval_digest"],
                         digest_before)
        lineages = {w["id"]: w["lineage"] for w in self.app.diagnostics()["work_routing_constraints"]["works"]}
        self.assertEqual((lineages[work["id"]], lineages[child["id"]]), ("routine", "routine"))


class RoutineEnvelopeLineageInvariantTests(ServiceCase):
    """O6: lineage_kind is sound only if a Routine envelope reaches a Work solely
    along parent_work_id edges from a kind=routine Work. Pin it over every path
    that writes required_* (materialize, delegate, explicit body, continuation,
    link_work's provisional guard): a new path that breaks it fails here."""

    def test_routine_envelope_only_travels_along_routine_lineage(self):
        from pangpang import routing_constraint
        self.app.store.put("probe", {"id": "codex-local", "available": True, "supervision": "windows-job-object"})
        r = self.app.create_routine("bot-main", {"instruction": "日报", "timezone": "UTC",
                                                 "schedule": {"type": "daily", "time": "09:00"},
                                                 "authority": {"grant_write": True, "required_host_id": "local",
                                                               "required_installation_id": "inst-envelope"}})
        fire = self.app.store.get("work", self.app.run_routine_now("bot-main", r["id"])["work_id"])
        self.assertNotEqual(fire.get("entry_mode"), "provisional") # link_work cannot relink it away
        a = self.app.create_bot({"name": "a", "role": "t"}); b = self.app.create_bot({"name": "b", "role": "t"})
        task = {"goal": "g", "request": "r", "acceptance": ["d"], "mode": "return_to_sender"}
        child = self.app.delegate("bot-main", fire["id"], dict(task, target_bot_id=a["id"]))["work_id"]
        self.app.delegate(a["id"], child, dict(task, target_bot_id=b["id"]))
        with self.assertRaises(Fault) as ctx: # continuation never joins/forks a Routine Work
            self.app.resolve_continuation("bot-main", {"text": "继续", "continue_work_id": fire["id"],
                                                       "stream_id": "t"})
        self.assertEqual(ctx.exception.code, "continue_work_not_continuable")
        conv = self.app.send("bot-main", {"text": "x", "installation_id": "codex-local"}, "conv")["work_id"]
        self.app.delegate("bot-main", conv, dict(task, target_bot_id=a["id"]))
        rows = [w for w in self.app.store.find("work", limit=1000)
                if w.get("required_host_id") or w.get("required_installation_id")]
        lineages = {w["id"]: routing_constraint.lineage_kind(self.app.store, w) for w in rows}
        self.assertEqual(sum(1 for w in rows if w.get("required_installation_id") == "inst-envelope"), 3)
        for w in rows:
            self.assertIn(lineages[w["id"]], {"routine", "conversation"}, w["id"])
            if w.get("required_installation_id") == "inst-envelope":
                self.assertEqual(lineages[w["id"]], "routine", w["id"])


class ConfigPublicationTests(ServiceCase):
    """G: memory publication only after durable commit; CAS; Vault after commit."""

    def live(self):
        return (self.app.config, self.app.profiles, self.app.execution, self.app.config_revision)

    def fail_settings_put(self):
        real = self.app.store.put
        def put(kind, obj, expected=None):
            if kind == "settings":
                raise Fault("disk_full_injected")
            return real(kind, obj, expected)
        return patch.object(self.app.store, "put", side_effect=put)

    def test_failed_commit_publishes_nothing(self):
        before = self.live()
        with self.fail_settings_put(), self.assertRaises(Fault):
            self.app.configure_installation("codex-local", {"priority": 7})
        self.assertEqual([id(x) for x in self.live()[:3]] + [self.live()[3]],
                         [id(x) for x in before[:3]] + [before[3]])
        self.assertNotEqual(self.app.execution.installations["codex-local"].get("priority"), 7)

    def test_rollback_after_staging_publishes_nothing(self):
        before = self.live()
        with self.assertRaises(RuntimeError):
            with self.app.store.atomic():
                candidate = json.loads(json.dumps(self.app.config))
                candidate["hosts"].append({"id": "ghost", "transport": "local", "os": "Linux",
                                           "allowed_roots": [str(self.root)]})
                self.app.commit_managed_config(candidate)
                raise RuntimeError("later step fails")
        self.assertIs(self.app.config, before[0])
        self.assertNotIn("ghost", self.app.execution.hosts)
        self.assertEqual(self.app.config_revision, before[3])

    def test_change_behind_running_process_conflicts_and_is_reported_stale(self):
        self.app.configure_installation("codex-local", {"priority": 5})
        self.assertFalse(self.app.config_stale())
        row = self.app.store.get("settings", "managed")
        row["config"]["installations"][0]["priority"] = 42 # edited behind the process
        self.app.store.put("settings", {"id": "managed", "config": row["config"]})
        self.assertTrue(self.app.config_stale())
        self.assertTrue(self.app.diagnostics()["config_stale"])
        with self.assertRaises(Fault) as ctx:
            self.app.configure_installation("codex-local", {"priority": 6})
        self.assertEqual(ctx.exception.code, "state_conflict")
        self.assertEqual(self.app.store.get("settings", "managed")["config"]["installations"][0]["priority"], 42)

    def test_revoke_keeps_old_secret_until_config_commits(self):
        self.app.vault.set("model-key-old", "s3cret")
        candidate = json.loads(json.dumps(self.app.config))
        candidate["models"][0]["credential_ref"] = "model-key-old"
        self.app.commit_managed_config(candidate)
        with self.fail_settings_put(), self.assertRaises(Fault):
            self.app.onboarding.revoke_model_key()
        self.assertTrue(self.app.vault.has("model-key-old"))
        self.assertEqual(self.app.profiles["test"]["credential_ref"], "model-key-old")
        self.app.onboarding.revoke_model_key()
        self.assertFalse(self.app.vault.has("model-key-old"))
        self.assertNotEqual(self.app.store.get("settings", "managed")["config"]["models"][0]["credential_ref"],
                            "model-key-old")

    def test_write_published_between_snapshot_and_commit_is_a_conflict_not_lost(self): # O5
        self.app.vault.set("model-key-old", "s3cret")
        candidate = json.loads(json.dumps(self.app.config))
        candidate["models"][0]["credential_ref"] = "model-key-old"
        self.app.commit_managed_config(candidate)
        real, fired = self.app.commit_managed_config, []

        def interleaved(*args, **kwargs):
            if not fired: # another request publishes after revoke built its candidate
                fired.append(1)
                self.app.configure_installation("codex-local", {"priority": 9})
            return real(*args, **kwargs)
        with patch.object(self.app, "commit_managed_config", side_effect=interleaved):
            with self.assertRaises(Fault) as ctx:
                self.app.onboarding.revoke_model_key()
        self.assertEqual(ctx.exception.code, "state_conflict")
        saved = self.app.store.get("settings", "managed")["config"]
        self.assertEqual(saved["installations"][0]["priority"], 9)
        self.assertEqual(saved["models"][0]["credential_ref"], "model-key-old")
        self.assertTrue(self.app.vault.has("model-key-old"))
        self.assertFalse(self.app.config_stale())

    def test_model_change_conflicts_with_a_write_published_during_its_probe(self): # O5 / TTT R5
        from pangpang.pi_runtime import PiModel
        call = {"function": {"name": "setup_probe", "arguments": '{"value":"pangpang-ready"}'}, "id": "c1"}
        probes = []

        def probe(model_self, profile, messages, tools):
            probes.append(1)
            if len(probes) == 1: # a device/operator config write lands while the model is probed
                self.app.configure_installation("codex-local", {"priority": 9})
            return {"role": "assistant", "content": "", "tool_calls": [call]}, {}
        before = self.app.store.get("settings", "managed")["config"]["models"][0]
        with patch.object(PiModel, "complete", probe), self.assertRaises(Fault) as ctx:
            self.app.onboarding.model({"endpoint": "http://127.0.0.1:9/v1", "model": "m2", "secret": "k2"})
        self.assertEqual(ctx.exception.code, "state_conflict")
        saved = self.app.store.get("settings", "managed")["config"]
        self.assertEqual(saved["installations"][0]["priority"], 9)
        self.assertEqual(saved["models"][0]["model"], before["model"])
        self.assertFalse(any(r.startswith("model-key") for r in self._vault_refs())) # new secret cleaned

    def _vault_refs(self):
        return [r[0] for r in self.app.vault.db.execute("SELECT id FROM secrets")]

    def test_failed_pairing_leaves_no_ghost_host_and_no_orphan_token(self):
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = base64.b64encode(key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        code = self.app.onboarding.pairing_code()["code"]
        body = {"code": code, "instance_id": "device-" + uuid.uuid4().hex, "public_key": pub, "name": "pc"}
        hosts_before = set(self.app.execution.hosts)
        with patch.object(self.app.broker, "enqueue", side_effect=Fault("injected")), self.assertRaises(Fault):
            self.app.onboarding.pair(body)
        self.assertEqual(set(self.app.execution.hosts), hosts_before)
        self.assertEqual([h["id"] for h in self.app.config["hosts"]], sorted(hosts_before))
        from pangpang.store import digest
        self.assertFalse(self.app.vault.has("pair:" + digest(code)))
        result = self.app.onboarding.pair(body) # the code is still usable
        self.assertIn(result["host_id"], self.app.execution.hosts)
        self.assertFalse(self.app.config_stale())


class LocalSetupSaveTests(unittest.TestCase):
    def test_connector_save_publishes_after_write_and_cas(self):
        from types import SimpleNamespace
        from pangpang.local_setup import LocalSetup
        from pangpang.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "connector.db")
            try:
                live = config(root)
                c = SimpleNamespace(store=store, data=root, config=live, config_revision=0, execution=None)
                setup = LocalSetup(c)
                candidate = json.loads(json.dumps(live)); candidate["installations"][0]["priority"] = 3
                setup.save(candidate)
                self.assertIs(c.config, candidate)
                self.assertEqual(c.config_revision, 1)
                store.put("settings", {"id": "managed", "config": live}) # edited behind the process
                again = json.loads(json.dumps(candidate)); again["installations"][0]["priority"] = 4
                with self.assertRaises(Fault):
                    setup.save(again)
                self.assertIs(c.config, candidate)
            finally:
                store.close()


class ConnectorCredentialRotationTests(unittest.TestCase):
    """G (connector): the executor's secret never changes unless the config commits."""

    def setUp(self):
        from types import SimpleNamespace
        from pangpang.execution import Execution
        from pangpang.local_setup import LocalSetup
        from pangpang.store import Store, digest
        from pangpang.vault import Vault
        from pangpang.connector import policy_digest
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store, self.vault = Store(root / "connector.db"), Vault(root / "vault")
        live = config(root)
        self.c = SimpleNamespace(store=self.store, vault=self.vault, data=root, config=live, config_revision=0,
                                 execution=Execution(live, root), host_id="local")
        self.setup = LocalSetup(self.c)
        self.ref = "cli-" + digest(["codex-local", "CODEX_API_KEY"])
        self.digest = lambda: policy_digest(self.c.config, "codex-local")
        self.vault.unseal = lambda sealed, request_id: sealed # sealing is out of scope here

    def tearDown(self):
        self.store.close(); self.vault.close(); self.tmp.cleanup()

    def install(self, value):
        return self.setup.credential({"installation_id": "codex-local", "expected_policy_digest": self.digest(),
                                      "sealed": value, "request_id": "r"})

    def edit_behind(self):
        row = self.store.get("settings", "managed")
        self.store.put("settings", {"id": "managed", "config": row["config"]})

    def test_failed_commit_restores_previous_secret_and_config(self):
        self.install("old")
        self.assertEqual(self.vault.get(self.ref), "old")
        config_before, revision = self.c.config, self.c.config_revision
        self.edit_behind()
        with self.assertRaises(Fault) as ctx:
            self.install("new")
        self.assertEqual(ctx.exception.code, "state_conflict")
        self.assertEqual(self.vault.get(self.ref), "old")
        self.assertIs(self.c.config, config_before)
        self.assertEqual(self.c.config_revision, revision)

    def test_failed_first_install_leaves_no_secret(self):
        self.setup.save(json.loads(json.dumps(self.c.config)))
        self.edit_behind()
        with self.assertRaises(Fault):
            self.install("first")
        self.assertFalse(self.vault.has(self.ref))

    def test_successful_rotation_keeps_stable_ref_and_policy_digest(self):
        self.install("old")
        digest_after_install = self.digest()
        self.install("new")
        self.assertEqual(self.vault.get(self.ref), "new")
        self.assertEqual(self.digest(), digest_after_install) # queued tasks stay valid


class CapabilityTests(ServiceCase):
    """H: startup self-check and diagnostics; the tool surface is unchanged."""

    def test_missing_keys_are_reported_not_silent(self):
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("TAVILY_API_KEY", None); env.pop("TYPESAFE_API_KEY", None)
            self.app.semantic_gate.mode = "enforce"
            caps = self.app.diagnostics()["capabilities"]
            # web_search is keyless since 1.0 (Bing/DuckDuckGo chain): always on.
            # A configured local model still routes continuation when the Jev key is absent.
            self.assertEqual((caps["web_search"], caps["semantic_gate_key"], caps["continuation_routing"],
                              caps["continuation_router"]),
                             (True, False, True, "local_model"))
            text = "\n".join(self.app.capability_warnings())
            self.assertIn("mode=enforce without TYPESAFE_API_KEY", text)
            self.assertIn("configured local model", text)
            self.assertNotIn("every message starts a fresh Work", text)
            self.app.semantic_gate.continuation_routing = False
            quiet = "\n".join(self.app.capability_warnings())
            self.assertNotIn("configured local model", quiet)
            self.assertEqual(self.app.capabilities()["continuation_router"], "off")
            self.assertFalse(self.app.capabilities()["continuation_routing"])
            self.assertIn("mode=enforce without TYPESAFE_API_KEY", quiet)
            env["TYPESAFE_API_KEY"] = "k"
            self.assertNotIn("mode=enforce without TYPESAFE_API_KEY",
                             "\n".join(self.app.capability_warnings()))
        from pangpang.runtime import TOOLS
        self.assertIn("web_search", {t["function"]["name"] for t in TOOLS})


@unittest.skipUnless((Path(__file__).resolve().parents[1] / "scripts" / "tls-bridge.py").exists(),
                     "tls-bridge.py is not part of this distribution")
class TlsBridgeTests(unittest.TestCase):
    """N2: an idle pre-handshake connection cannot stall the bridge."""

    def test_idle_connections_do_not_block_and_handshakes_are_bounded(self):
        import importlib.util, ssl, sys, datetime
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("tls_bridge", root / "scripts" / "tls-bridge.py")
        bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
        bridge.HANDSHAKE_TIMEOUT_S, bridge.HANDSHAKE_LIMIT = 0.5, 4
        with tempfile.TemporaryDirectory() as tmp:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
.serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
.sign(key, hashes.SHA256()))
            cert_path, key_path = Path(tmp) / "c.pem", Path(tmp) / "k.pem"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            backend = socket.socket(); backend.bind(("127.0.0.1", 0)); backend.listen(8)
            def serve_backend():
                while True:
                    try:
                        conn, _ = backend.accept()
                    except OSError:
                        return
                    conn.recv(16); conn.sendall(b"pong"); conn.close()
            threading.Thread(target=serve_backend, daemon=True).start()
            probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
            argv = ["tls-bridge", "--listen-host", "127.0.0.1", "--listen-port", str(port),
                    "--backend-port", str(backend.getsockname()[1]), "--cert", str(cert_path), "--key", str(key_path)]
            with patch.object(sys, "argv", argv), patch("builtins.print"):
                threading.Thread(target=bridge.main, daemon=True).start()
                deadline = time.time() + 5
                while time.time() < deadline:
                    try:
                        socket.create_connection(("127.0.0.1", port), timeout=1).close(); break
                    except OSError:
                        time.sleep(0.05)
                time.sleep(0.7) # let the readiness probe's handshake slot time out
                ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

                def ping():
                    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
                        with ctx.wrap_socket(raw, server_hostname="localhost") as tls:
                            tls.sendall(b"ping"); return tls.recv(4)

                idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
                self.assertEqual(ping(), b"pong") # silent sockets do not stall accept
                idle += [socket.create_connection(("127.0.0.1", port)) for _ in range(6)]
                time.sleep(1.0) # every stalled handshake times out and frees its slot
                self.assertEqual(ping(), b"pong")
                for s in idle:
                    s.close()
            backend.close()

    def test_bridge_is_not_in_public_release(self):
        import sys
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "tools"))
        try:
            import build_release as br
        finally:
            sys.path.pop(0)
        self.assertNotIn("scripts/tls-bridge.py", br.release_candidates())


class CompactionCostTests(ServiceCase):
    """N5: in-turn compaction is up to 8 serial model calls; its cost is recorded."""

    def runtime(self, calls):
        class Summarizer:
            def complete(self, profile, messages, tools):
                calls.append(len(messages[-1]["content"]))
                return {"content": "摘要"}, {"input": 1000, "output": 50, "totalTokens": 1050}
        return Runtime(self.app, Summarizer())

    def turn_with(self, runtime, size):
        m = self.app.send("bot-main", {"text": "大输出"}, "c%d" % size)
        turn = self.app.store.get("turn", m["turn_id"])
        turn["history"] = runtime.context(turn) + [{"role": "tool", "tool_call_id": "x", "content": "y" * size}]
        return turn

    def test_cost_is_recorded_and_bounded(self):
        calls = []
        rt = self.runtime(calls)
        profile = self.app.profiles["test"] # safety 256 KiB -> 128 KiB chunks
        turn = rt.compact(self.turn_with(rt, 1000 * 1024), profile)
        self.assertEqual(len(calls), 8)
        stats = self.app.store.get("turn", turn["id"])["compaction_stats"]
        self.assertEqual((stats["count"], stats["chunks"], stats["model_calls"]), (1, 8, 8))
        self.assertEqual((stats["input_tokens"], stats["output_tokens"]), (8000, 400))
        turn["history"].append({"role": "tool", "tool_call_id": "z", "content": "y" * 300 * 1024})
        turn = rt.compact(turn, profile)
        stats = self.app.store.get("turn", turn["id"])["compaction_stats"]
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["model_calls"], len(calls))
        with self.assertRaises(Fault) as ctx:
            rt.compact(turn, profile)
        self.assertEqual(ctx.exception.code, "context_budget")
        recent = self.app.diagnostics()["model"]["compaction_recent"]
        self.assertEqual((recent["turns"], recent["successful_compactions"], recent["model_calls_completed"],
                          recent["model_calls_attempted"], recent["failures"]),
                         (1, 2, len(calls), len(calls), 0))

    def test_failed_compaction_cost_is_recorded_and_fault_propagates(self):
        calls = []
        class Flaky:
            def complete(self, profile, messages, tools):
                calls.append(1)
                if len(calls) == 5:
                    raise Fault("model_timeout")
                return {"content": "摘要"}, {"input": 1000, "output": 50}
        rt = Runtime(self.app, Flaky())
        turn = self.turn_with(rt, 1000 * 1024)
        self.app.store.put("turn", turn)
        history_before = self.app.store.get("turn", turn["id"])["history"]
        with self.assertRaises(Fault) as ctx:
            rt.compact(turn, self.app.profiles["test"])
        self.assertEqual(ctx.exception.code, "model_timeout")
        stored = self.app.store.get("turn", turn["id"])
        stats = stored["compaction_stats"]
        self.assertEqual((stats["model_calls_attempted"], stats["model_calls"], stats["failures"]), (5, 4, 1))
        self.assertEqual((stats["count"], stats["input_tokens"], stats["last_error"]), (0, 4000, "model_timeout"))
        self.assertEqual(stored["history"], history_before)
        self.assertEqual(stored.get("compactions", 0), 0)
        recent = self.app.diagnostics()["model"]["compaction_recent"] # what reads
        self.assertEqual((recent["successful_compactions"], recent["model_calls_attempted"],
                          recent["model_calls_completed"], recent["failures"]), (0, 5, 4, 1))

    def test_more_than_eight_chunks_fails_closed(self):
        calls = []
        rt = self.runtime(calls)
        turn = self.turn_with(rt, 1200 * 1024)
        with self.assertRaises(Fault) as ctx:
            rt.compact(turn, self.app.profiles["test"])
        self.assertEqual(ctx.exception.code, "compaction_budget")
        self.assertEqual(calls, [])
        stats = self.app.store.get("turn", turn["id"])["compaction_stats"] # the cliff's worst end is observed
        self.assertEqual((stats["budget_rejections"], stats["model_calls_attempted"], stats["count"],
                          stats["last_error"]), (1, 0, 0, "compaction_budget"))
        self.assertEqual(self.app.diagnostics()["model"]["compaction_recent"]["compaction_budget_rejections"], 1)


class SmallInvariantTests(unittest.TestCase):
    def test_dead_resource_blocked_is_gone(self): # D
        self.assertFalse(hasattr(Runtime, "resource_blocked"))

    def test_strict_txn_io_is_not_operator_configurable(self): # N3
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp)); cfg["strict_txn_io_asserts"] = False
            store_module.set_strict_txn_io(False)
            app = Service(cfg, Path(tmp) / "data")
            try:
                self.assertTrue(store_module._strict_txn_io[0])
            finally:
                store_module.set_strict_txn_io(False)
                app.store.close()


if __name__ == "__main__":
    unittest.main()
