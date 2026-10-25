"""Conversational rename of a registered computer's display name.

The inventory label is hosts[].name. Task routing cards copy that label into
run_preview.host_name. Both update together. Host ids, the OS field, pairing,
and executor bindings stay put.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from pangpang.connector import authorization_digest, legacy_authorization_digest
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault
from test_bot_runtime import call, config


LINUX = "Ubuntu-2404-noble-amd64-base"
WINDOWS = "W-PW0EEPY5"


def inventory_config(root):
    cfg = config(root)
    cfg["semantic_gate"] = {"mode": "off", "continuation_routing": False}
    local = cfg["hosts"][0]
    local["name"] = "Desk-Local"
    cfg["hosts"] = [
        local,
        {"id": "host-linux", "name": LINUX, "os": "Linux", "transport": "connector",
         "execution_level": "L0", "allowed_roots": [str(root)]},
        {"id": "host-win", "name": WINDOWS, "os": "Windows", "transport": "connector",
         "execution_level": "L0", "allowed_roots": [str(root)]},
    ]
    cfg["installations"].append({
        "id": "codex-linux", "host_id": "host-linux", "adapter": "codex",
        "argv": [sys.executable, "-c", "print(1)"],
        "probe_argv": [sys.executable, "--version"],
        "cwd": str(root), "risk": "write", "enabled": True,
    })
    return cfg


class _RenameModel:
    """Planner stand-in: the user names the computer, the tool performs the write."""

    def complete(self, profile, messages, tools):
        names = [item["function"]["name"] for item in tools or []]
        if names:
            assert "rename_host" in names
        last = messages[-1]
        if last["role"] == "tool":
            body = json.loads(last["content"])
            assert "error" not in body, body
            return {"content": "已改为 %s（%s）" % (body["name"], body["host_id"])}, {}
        text = last["content"]
        if LINUX in text and "Finland-Linux" in text:
            return call("rename_host", host_id=LINUX, name="Finland-Linux"), {}
        if WINDOWS in text and "Work-Windows" in text:
            return call("rename_host", host_id=WINDOWS, name="Work-Windows"), {}
        raise AssertionError(text[:400])


class RenameHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.app = Service(inventory_config(self.workspace), self.data)
        self._plant()

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _plant(self):
        self.app.store.put("device", {
            "id": "host-linux", "instance_id": "device-" + "ab" * 16, "public_key": "pk"})
        self.app.store.put("device", {
            "id": "host-win", "instance_id": "device-" + "cd" * 16, "public_key": "pk2"})
        self.app.store.put("inventory", {
            "id": "host-linux", "items": [{"adapter": "codex", "version": "1"}]})
        self.app.store.put("declared_adapters", {
            "id": "host-linux", "rows": {"codex": {"concurrency": 1, "priority": 10}}})
        self.app.store.put("task", {
            "id": "task-linux", "host_id": "host-linux", "execution_id": "exec-linux",
            "status": "succeeded", "spec_digest": "frozen-linux",
            "prompt": "keep %s in the instruction" % LINUX,
            "run_preview": {"host_id": "host-linux", "host_name": LINUX, "command": ["codex"]},
        })
        self.app.store.put("task", {
            "id": "task-win", "host_id": "host-win", "execution_id": "exec-win",
            "status": "succeeded", "spec_digest": "frozen-win",
            "run_preview": {"host_id": "host-win", "host_name": WINDOWS},
        })
        self.snapshot = {
            host_id: {k: v for k, v in host.items() if k != "name"}
            for host_id, host in self.app.execution.hosts.items()}

    def _rename(self, query, new_name):
        return self.app.rename_host_display("bot-main", query, new_name)

    def _stored_host(self, host_id):
        config = self.app.store.get("settings", "managed")["config"]
        return next(host for host in config["hosts"] if host["id"] == host_id)

    def _assert_host_untouched(self, host_id):
        live = self.app.execution.hosts[host_id]
        stored = self._stored_host(host_id)
        for source in (live, stored):
            self.assertEqual(source["id"], host_id)
            for key, value in self.snapshot[host_id].items():
                self.assertEqual(source.get(key), value, key)
        self.assertEqual(live["os"], stored["os"])

    def _walk_labels(self, obj, host_id, old, new, path):
        if isinstance(obj, dict):
            bound = (obj.get("host_id") == host_id or obj.get("required_host_id") == host_id
                     or (obj.get("id") == host_id and ("transport" in obj or "instance_id" in obj
                                                        or "items" in obj or "rows" in obj)))
            if obj.get("id") == host_id and obj.get("transport"):
                self.assertEqual(obj.get("name"), new, path)
                self.assertEqual(obj.get("id"), host_id, path)
            if bound:
                self.assertNotEqual(obj.get("name"), old, path)
                self.assertNotEqual(obj.get("host_name"), old, path)
                if "host_name" in obj:
                    self.assertEqual(obj["host_name"], new, path)
                if obj.get("host_id") is not None:
                    self.assertEqual(obj["host_id"], host_id, path)
                if obj.get("required_host_id") is not None:
                    self.assertEqual(obj["required_host_id"], host_id, path)
            for key, value in obj.items():
                self._walk_labels(value, host_id, old, new, path + "." + key)
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                self._walk_labels(value, host_id, old, new, "%s[%s]" % (path, index))

    def _assert_no_stale_label(self, host_id, old, new):
        rows = self.app.store.db.execute("SELECT kind, id, data FROM objects").fetchall()
        for kind, oid, raw in rows:
            self._walk_labels(json.loads(raw), host_id, old, new, "%s/%s" % (kind, oid))

    def test_conversation_renames_display_and_persists(self):
        runtime = Runtime(self.app, _RenameModel())
        self.assertEqual(self.app.store.find("command"), [])
        first = self.app.send("bot-main", {"text": "把 %s 改成 Finland-Linux" % LINUX}, "rename-linux")
        runtime.step()
        linux = self.app.execution.hosts["host-linux"]
        self.assertEqual(linux["id"], "host-linux")
        self.assertEqual(linux["name"], "Finland-Linux")
        self.assertEqual(linux["os"], "Linux")
        self._assert_host_untouched("host-linux")
        self.assertEqual(self.app.store.get("task", "task-linux")["run_preview"]["host_name"], "Finland-Linux")
        self.assertEqual(self.app.store.get("task", "task-linux")["run_preview"]["host_id"], "host-linux")
        self.assertEqual(self.app.store.get("task", "task-linux")["spec_digest"], "frozen-linux")
        self.assertIn(LINUX, self.app.store.get("task", "task-linux")["prompt"])
        self.assertEqual(self.app.store.get("task", "task-win")["run_preview"]["host_name"], WINDOWS)
        self.assertEqual(self.app.store.get("device", "host-linux")["instance_id"], "device-" + "ab" * 16)
        self.assertNotIn("name", self.app.store.get("device", "host-linux"))
        self.assertEqual(self.app.store.get("inventory", "host-linux")["id"], "host-linux")
        self.assertEqual(self.app.store.find("command"), [])
        turn = self.app.store.get("turn", first["turn_id"])
        self.assertEqual(turn["status"], "done", turn.get("error"))
        self.assertIn("rename_host", turn["history"][0]["content"])
        self.assertTrue(any(m.get("content") == "已改为 Finland-Linux（host-linux）"
                            for m in self.app.store.find("message")))

        second = self.app.send("bot-main", {"text": "把 %s 改成 Work-Windows" % WINDOWS}, "rename-win")
        runtime.step()
        self.assertEqual(self.app.store.get("turn", second["turn_id"])["status"], "done",
                         self.app.store.get("turn", second["turn_id"]).get("error"))
        self.assertEqual(self.app.execution.hosts["host-win"]["name"], "Work-Windows")
        self.assertEqual(self.app.execution.hosts["host-win"]["id"], "host-win")
        self.assertEqual(self.app.execution.hosts["host-win"]["os"], "Windows")
        self.assertEqual(self.app.execution.hosts["host-linux"]["name"], "Finland-Linux")
        self.assertEqual(self.app.store.get("task", "task-win")["run_preview"]["host_name"], "Work-Windows")
        self.assertEqual(self.app.store.get("task", "task-win")["spec_digest"], "frozen-win")
        self._assert_host_untouched("host-win")
        self._assert_no_stale_label("host-linux", LINUX, "Finland-Linux")
        self._assert_no_stale_label("host-win", WINDOWS, "Work-Windows")

        listed = {row["installation_id"]: row for row in self.app.list_executors()["installations"]}
        self.assertEqual(listed["codex-linux"]["host_id"], "host-linux")
        self.assertEqual(listed["codex-linux"]["host_name"], "Finland-Linux")
        self.assertEqual(listed["codex-linux"]["adapter"], "codex")
        self.assertEqual(listed["codex-local"]["host_id"], "local")
        self.assertEqual(listed["codex-local"]["host_name"], "Desk-Local")
        catalog = runtime.invoke(
            {"bot_id": "bot-main", "work_id": first["work_id"]}, "catalog", {})
        names = {host["id"]: host["name"] for host in catalog["hosts"]}
        self.assertEqual(names["host-linux"], "Finland-Linux")
        self.assertEqual(names["host-win"], "Work-Windows")
        self.assertEqual(names["local"], "Desk-Local")
        installed = {row["id"]: row["host_id"] for row in catalog["installations"]}
        self.assertEqual(installed["codex-linux"], "host-linux")
        self.assertEqual(installed["codex-local"], "local")

        again = self._rename("Finland-Linux", "Finland-Linux")
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["labels_updated"], 0)
        revision = self.app.config_revision
        stale = self.app.store.get("task", "task-linux")
        stale["run_preview"] = dict(stale["run_preview"], host_name=LINUX)
        self.app.store.put("task", stale)
        repaired = self._rename("host-linux", "Finland-Linux")
        self.assertEqual(repaired["labels_updated"], 1)
        self.assertFalse(repaired["unchanged"])
        self.assertEqual(self.app.config_revision, revision)
        self.assertEqual(self.app.store.get("task", "task-linux")["run_preview"]["host_name"], "Finland-Linux")
        self.assertEqual(self.app.store.get("task", "task-linux")["spec_digest"], "frozen-linux")

        self.app.store.close()
        self.app = Service(inventory_config(self.workspace), self.data)
        self.assertEqual(self.app.execution.hosts["host-linux"]["name"], "Finland-Linux")
        self.assertEqual(self.app.execution.hosts["host-linux"]["id"], "host-linux")
        self.assertEqual(self.app.execution.hosts["host-linux"]["os"], "Linux")
        self.assertEqual(self.app.execution.hosts["host-win"]["name"], "Work-Windows")
        self.assertEqual(self.app.store.get("task", "task-linux")["run_preview"]["host_name"], "Finland-Linux")
        self.assertEqual(self.app.store.get("task", "task-win")["run_preview"]["host_name"], "Work-Windows")
        self.assertEqual(self.app.store.find("command"), [])

    def test_routing_uses_new_label_and_keeps_host_id(self):
        self.app.probe("codex-local")
        sent = self.app.send("bot-main", {"text": "route"}, "route-work")
        work = self.app.store.get("work", sent["work_id"])
        work["required_host_id"] = "local"
        self.app.store.put("work", work)
        renamed = self._rename("Desk-Local", "Desk-Renamed")
        self.assertEqual(renamed["host_id"], "local")
        self.assertEqual(renamed["previous_name"], "Desk-Local")
        self.assertEqual(self.app.execution.hosts["local"]["id"], "local")
        routed = self.app.route("bot-main", self.app.store.get("work", work["id"]), {"host_id": "Desk-Renamed"})
        self.assertEqual(routed["selected"]["host_id"], "local")
        self.assertEqual(routed["selected"]["installation_id"], "codex-local")
        unresolved = self.app._resolve_executor_intent({"host_id": "Desk-Local"})
        self.assertEqual(unresolved["host_id"], "Desk-Local")
        preview = self.app._run_preview(self.app.execution.installations["codex-local"])
        self.assertEqual(preview["host_name"], "Desk-Renamed")
        self.assertEqual(preview["host_id"], "local")
        self.assertEqual(self.app.store.get("work", work["id"])["required_host_id"], "local")
        pinned = self.app.route("bot-main", self.app.store.get("work", work["id"]), {})
        self.assertEqual(pinned["selected"]["host_id"], "local")

    def test_invalid_ambiguous_and_collision_fail_without_writes(self):
        before_card = self.app.store.get("task", "task-linux")["run_preview"]["host_name"]
        revision = self.app.config_revision
        for bad in ("", "   ", "x" * 81, "line\nbreak", "tab\there", "del\x7f",
                    "next\u0085line", "rtl\u202eoverride", "zw\u200bspace", "\u202efinland", 12):
            with self.assertRaises(Fault) as ctx:
                self._rename(LINUX, bad)
            self.assertEqual(ctx.exception.code, "invalid_display_name")
        with self.assertRaises(Fault) as ctx:
            self._rename("no-such-computer", "Finland-Linux")
        self.assertEqual(ctx.exception.code, "host_not_found")
        with self.assertRaises(Fault) as ctx:
            self._rename("  ", "Finland-Linux")
        self.assertEqual(ctx.exception.code, "host_not_found")
        with self.assertRaises(Fault) as ctx:
            self._rename(LINUX, WINDOWS)
        self.assertEqual(ctx.exception.code, "display_name_conflict")
        self.assertIn("host-win", ctx.exception.advice)
        with self.assertRaises(Fault) as ctx:
            self._rename(LINUX, "host-win")
        self.assertEqual(ctx.exception.code, "display_name_conflict")
        self.assertEqual(self.app.execution.hosts["host-linux"]["name"], LINUX)
        self.assertEqual(self.app.store.get("task", "task-linux")["run_preview"]["host_name"], before_card)
        self.assertEqual(self.app.config_revision, revision)

        candidate, revision = self.app.managed_snapshot()
        for host in candidate["hosts"]:
            if host["id"] in {"host-linux", "host-win"}:
                host["name"] = "Shared-Box"
        self.app.commit_managed_config(candidate, base_revision=revision)
        with self.assertRaises(Fault) as ctx:
            self._rename("Shared-Box", "Finland-Linux")
        self.assertEqual(ctx.exception.code, "ambiguous_host_name")
        self.assertIn("host-linux", ctx.exception.advice)
        self.assertIn("host-win", ctx.exception.advice)
        self.assertEqual(self.app.execution.hosts["host-linux"]["name"], "Shared-Box")
        self.assertEqual(self.app.execution.hosts["host-win"]["name"], "Shared-Box")
        self.assertEqual(self.app.store.get("task", "task-linux")["run_preview"]["host_name"], before_card)
        chosen = self._rename("host-linux", "Finland-Linux")
        self.assertEqual(chosen["host_id"], "host-linux")
        self.assertEqual(self.app.execution.hosts["host-linux"]["name"], "Finland-Linux")
        self.assertEqual(self.app.execution.hosts["host-win"]["name"], "Shared-Box")
        self.assertEqual(self.app.store.get("task", "task-linux")["run_preview"]["host_name"], "Finland-Linux")
        self.assertEqual(self.app.store.get("task", "task-win")["run_preview"]["host_name"], WINDOWS)
        stripped = self._rename("  host-win  ", "  Work-Windows  ")
        self.assertEqual(stripped["name"], "Work-Windows")
        self.assertEqual(stripped["host_id"], "host-win")

    def test_rename_keeps_the_authorization_grant(self):
        inst = self.app.execution.installations["codex-local"]
        frozen = self.app._authorization_digest("codex-local")
        self.assertEqual(self.app._executor_grant("codex-local")["digest"], frozen)
        self.assertTrue(self.app._grant_matches(inst))
        renamed_only = json.loads(json.dumps(self.app.config))
        for host in renamed_only["hosts"]:
            if host["id"] == "local":
                host["name"] = "Something-Else"
        self.assertEqual(authorization_digest(renamed_only, "codex-local"), frozen)
        rooted = json.loads(json.dumps(self.app.config))
        for host in rooted["hosts"]:
            if host["id"] == "local":
                host["allowed_roots"] = [str(self.root / "other-root")]
        self.assertNotEqual(authorization_digest(rooted, "codex-local"), frozen)
        self.app.store.put("probe", {
            "id": "codex-local", "available": True, "supervision": "windows-job-object"})
        task = {"id": "task-auth", "host_id": "local", "installation_id": "codex-local",
                "authorization_digest": frozen, "status": "queued"}
        self.app.store.put("task", task)
        self.assertIsNone(self.app._start_denied(task))
        self._rename("Desk-Local", "Desk-Renamed")
        live = self.app.execution.installations["codex-local"]
        self.assertEqual(self.app.execution.hosts["local"]["id"], "local")
        self.assertEqual(self.app.execution.hosts["local"]["name"], "Desk-Renamed")
        self.assertEqual(self.app._authorization_digest("codex-local"), frozen)
        self.assertEqual(self.app._executor_grant("codex-local")["digest"], frozen)
        self.assertTrue(self.app._grant_matches(live))
        stored = self.app.store.get("task", "task-auth")
        self.assertIsNone(self.app._authorization_denied(stored, live))
        self.assertIsNone(self.app._start_denied(stored))

    def test_legacy_name_inclusive_digest_still_authorizes(self):
        inst = self.app.execution.installations["codex-local"]
        live = self.app._authorization_digest("codex-local")
        legacy = self.app._legacy_authorization_digest("codex-local")
        self.assertEqual(legacy, legacy_authorization_digest(self.app.config, "codex-local"))
        self.assertNotEqual(legacy, live)
        row = self.app.store.get("settings", "executor_grants")
        grants = dict(row["grants"])
        grants["codex-local"] = dict(grants["codex-local"], digest=legacy)
        self.app.store.put("settings", dict(row, grants=grants))
        self.app.store.put("probe", {
            "id": "codex-local", "available": True, "supervision": "windows-job-object"})
        task = {"id": "task-legacy", "host_id": "local", "installation_id": "codex-local",
                "authorization_digest": legacy, "status": "queued"}
        self.app.store.put("task", task)
        self.assertTrue(self.app._grant_matches(inst))
        self.assertIsNone(self.app._authorization_denied(task, inst))
        self.assertIsNone(self.app._start_denied(task))
        listed = {item["installation_id"]: item for item in self.app.list_executors()["installations"]}
        self.assertEqual(listed["codex-local"]["authorization"], "always")
        forged = dict(task, authorization_digest="forged")
        self.assertEqual(self.app._authorization_denied(forged, inst), "installation_not_authorized")
        self._rename("Desk-Local", "Desk-Renamed")
        renamed = self.app.execution.installations["codex-local"]
        self.assertFalse(self.app._grant_matches(renamed))
        self.assertEqual(self.app._start_denied(task), "installation_not_authorized")
        candidate, revision = self.app.managed_snapshot()
        for host in candidate["hosts"]:
            if host["id"] == "local":
                host["allowed_roots"] = [str(self.root / "other-root")]
        self.app.commit_managed_config(candidate, base_revision=revision)
        moved = self.app.execution.installations["codex-local"]
        self.assertFalse(self.app._grant_matches(moved))
        self.assertEqual(self.app._authorization_denied(task, moved), "installation_not_authorized")
        self.assertEqual(self.app._start_denied(task), "installation_not_authorized")

    def test_connector_legacy_digest_matches_until_rename(self):
        config = self.app._config_for_digest()
        legacy = legacy_authorization_digest(config, "codex-linux")
        reported = authorization_digest(config, "codex-linux")
        self.assertNotEqual(legacy, reported)
        row = self.app.store.get("settings", "executor_grants")
        grants = dict(row.get("grants") or {})
        grants["codex-linux"] = {"digest": legacy, "granted_at": 1, "origin": "migration"}
        pending = [item for item in (row.get("pending_device") or []) if item != "codex-linux"]
        self.app.store.put("settings", dict(row, grants=grants, pending_device=pending))
        self.app.store.put("probe", {
            "id": "codex-linux", "available": True, "supervision": "windows-job-object",
            "authorization_digest": reported})
        task = {"id": "task-connector-legacy", "host_id": "host-linux",
                "installation_id": "codex-linux", "authorization_digest": legacy,
                "status": "queued"}
        self.app.store.put("task", task)
        inst = self.app.execution.installations["codex-linux"]
        self.assertEqual(self.app._device_reported_digest("codex-linux"), reported)
        self.assertTrue(self.app._grant_matches(inst))
        self.assertIsNone(self.app._authorization_denied(task, inst))
        self.assertIsNone(self.app._start_denied(task))
        self._rename(LINUX, "Finland-Linux")
        renamed = self.app.execution.installations["codex-linux"]
        self.assertEqual(self.app.execution.hosts["host-linux"]["name"], "Finland-Linux")
        self.assertEqual(self.app._device_reported_digest("codex-linux"), reported)
        self.assertFalse(self.app._grant_matches(renamed))
        self.assertEqual(self.app._authorization_denied(task, renamed), "installation_not_authorized")
        self.assertEqual(self.app._start_denied(task), "installation_not_authorized")

    def test_child_bot_cannot_rename(self):
        child = self.app.create_bot({"name": "helper", "role": "notes"})
        sent = self.app.send("bot-main", {"text": "child"}, "child-work")
        runtime = Runtime(self.app, _RenameModel())
        visible = {tool["function"]["name"] for tool in runtime.tools_for({"bot_id": child["id"]})}
        self.assertNotIn("rename_host", visible)
        self.assertIn("rename_host", {tool["function"]["name"] for tool in runtime.tools_for({"bot_id": "bot-main"})})
        with self.assertRaises(Fault) as ctx:
            runtime.invoke({"bot_id": child["id"], "work_id": sent["work_id"]},
                           "rename_host", {"host_id": LINUX, "name": "Nope"})
        self.assertEqual(ctx.exception.code, "main_bot_required")
        self.assertEqual(self.app.execution.hosts["host-linux"]["name"], LINUX)


if __name__ == "__main__":
    unittest.main()
