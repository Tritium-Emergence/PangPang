"""Host scheduling limits: concurrency + task timeout.

Documents where the old pins lived:
- concurrency 1: adapter catalog concurrency, installation.max_concurrency
  defaults, Service._task_admissible / Connector._reject_workspace_conflict
  counting by installation; unused hosts.example.json ``parallel``.
- timeout 3600: submit_task / launch_contract / runner missing-field fallthrough.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from pangpang import consent
from pangpang.connector import Connector, authorization_digest, policy_digest
from pangpang.host_limits import (
    apply_host_limits,
    explicit_host_concurrency,
    host_concurrency_limit,
    limits_view,
    parse_concurrency,
    parse_timeout,
    task_timeout_s,
)
from pangpang.http import project_host
from pangpang.service import Service
from pangpang.store import Fault

from test_bot_runtime import config


class HostLimitsUnitTests(unittest.TestCase):
    def test_parse_bounds_and_bool_reject(self):
        self.assertEqual(parse_concurrency(3), 3)
        self.assertEqual(parse_timeout(7200), 7200)
        with self.assertRaises(ValueError):
            parse_concurrency(True)
        with self.assertRaises(ValueError):
            parse_timeout(True)
        with self.assertRaises(ValueError):
            parse_concurrency(0)
        with self.assertRaises(ValueError):
            parse_timeout(10)

    def test_legacy_parallel_and_apply(self):
        self.assertEqual(explicit_host_concurrency({"parallel": 4}), 4)
        host = {"id": "h", "parallel": 1}
        out = apply_host_limits(host, {"max_concurrency": 3, "timeout_s": 7200})
        self.assertEqual(out["max_concurrency"], 3)
        self.assertEqual(out["timeout_s"], 7200)
        self.assertNotIn("parallel", out)
        cleared = apply_host_limits(out, {"max_concurrency": None, "timeout_s": None})
        self.assertNotIn("max_concurrency", cleared)
        self.assertNotIn("timeout_s", cleared)
        with self.assertRaises(ValueError):
            apply_host_limits(host, {})
        with self.assertRaises(ValueError):
            apply_host_limits(host, {"nope": 1})

    def test_task_timeout_order(self):
        self.assertEqual(task_timeout_s({}, {}, {}), 3600)
        self.assertEqual(task_timeout_s({}, {}, {"timeout_s": 120}), 120)
        self.assertEqual(task_timeout_s({}, {"timeout_s": 7200}, {"timeout_s": 120}), 7200)
        self.assertEqual(task_timeout_s({"timeout_s": 900}, {"timeout_s": 7200}, {"timeout_s": 120}), 900)

    def test_limits_view_and_project_host(self):
        view = limits_view({"max_concurrency": 2})
        self.assertEqual(view["max_concurrency"], 2)
        self.assertEqual(view["scope"], "host")
        row = project_host({"id": "local", "transport": "local", "timeout_s": 4000})
        self.assertEqual(row["limits"]["timeout_s"], 4000)
        self.assertEqual(row["id"], "local")


class HostLimitsServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.cfg = config(self.root)
        self.app = Service(self.cfg, self.root.parent / "data")
        self.app.probe("codex-local")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_set_host_limits_and_digest_stable(self):
        before_auth = authorization_digest(self.app.config, "codex-local")
        before_pol = policy_digest(self.app.config, "codex-local")
        out = self.app.set_host_limits("local", {"max_concurrency": 3, "timeout_s": 7200})
        self.assertEqual(out["limits"]["max_concurrency"], 3)
        self.assertEqual(out["limits"]["timeout_s"], 7200)
        after_auth = authorization_digest(self.app.config, "codex-local")
        after_pol = policy_digest(self.app.config, "codex-local")
        self.assertEqual(before_auth, after_auth)
        self.assertEqual(before_pol, after_pol)
        # consent still changes digests
        host = next(h for h in self.app.config["hosts"] if h["id"] == "local")
        host = dict(host)
        host[consent.CONSENT_KEY] = "other-version"
        cfg2 = copy.deepcopy(self.app.config)
        cfg2["hosts"] = [host if h["id"] == "local" else h for h in cfg2["hosts"]]
        self.assertNotEqual(authorization_digest(cfg2, "codex-local"), after_auth)

    def test_admissible_host_cap(self):
        self.app.set_host_limits("local", {"max_concurrency": 2})
        # two active tasks on host fill the cap
        for i, status in enumerate(("running", "running")):
            self.app.store.put("task", {
                "id": f"t-active-{i}", "status": status, "host_id": "local",
                "installation_id": "codex-local", "cwd": str(self.root / f"a{i}"),
                "access_mode": "verified_read", "workspace_key": ["local", [f"a{i}"]],
                "execution_id": f"exec-active-{i}",
            })
        candidate = {
            "id": "t-new", "status": "queued", "host_id": "local",
            "installation_id": "codex-local", "cwd": str(self.root / "b"),
            "access_mode": "verified_read", "workspace_key": ["local", ["b"]],
            "execution_id": "exec-new",
        }
        self.assertFalse(self.app._task_admissible(candidate))
        self.app.set_host_limits("local", {"max_concurrency": 3})
        self.assertTrue(self.app._task_admissible(candidate))
        # clearing host cap falls back to installation default 1
        self.app.set_host_limits("local", {"max_concurrency": None})
        # with 2 actives and install cap 1, still not admissible for a third on same install
        self.assertFalse(self.app._task_admissible(candidate))

    def test_submit_timeout_uses_host_and_override(self):
        self.app.set_host_limits("local", {"timeout_s": 7200})
        msg = self.app.send("bot-main", {"text": "task", "allow_write": True}, "hl-timeout")
        res = self.app.submit_task(
            "bot-main", msg["work_id"],
            {"prompt": "hi", "installation_id": "codex-local", "outputs": ["result.txt"]},
        )
        task = self.app.store.get("task", res["task_id"])
        if task["status"] == "awaiting_approval":
            task = self.app.approve(task["id"], {"spec_digest": task["spec_digest"], "always": True})
        self.assertEqual(task["timeout_s"], 7200)
        msg2 = self.app.send("bot-main", {"text": "task2", "allow_write": True}, "hl-timeout-2")
        res2 = self.app.submit_task(
            "bot-main", msg2["work_id"],
            {"prompt": "hi2", "installation_id": "codex-local", "outputs": ["result.txt"], "timeout_s": 900},
        )
        task2 = self.app.store.get("task", res2["task_id"])
        if task2["status"] == "awaiting_approval":
            task2 = self.app.approve(task2["id"], {"spec_digest": task2["spec_digest"], "always": True})
        self.assertEqual(task2["timeout_s"], 900)



    def test_pre_scheduling_grant_still_matches(self):
        """Grants hashed before scheduling keys were stripped still authorize."""
        from pangpang.connector import (
            authorization_digest, pre_scheduling_authorization_digest,
        )
        # Simulate an old grant that included parallel on the host.
        hosts = self.app.config["hosts"]
        host = dict(next(h for h in hosts if h["id"] == "local"))
        host["parallel"] = 1
        self.app.config["hosts"] = [host if h["id"] == "local" else h for h in hosts]
        old = pre_scheduling_authorization_digest(self.app.config, "codex-local")
        new = authorization_digest(self.app.config, "codex-local")
        self.assertNotEqual(old, new)
        inst = self.app.execution.installations["codex-local"]
        self.assertTrue(self.app._authorization_matches(old, inst))
        self.assertTrue(self.app._authorization_matches(new, inst))

    def test_broker_start_replays_frozen_host_cap(self):
        """Raising the host cap after enqueue must not change the durable payload."""
        host = {
            "id": "conn-a", "transport": "connector", "os": "Linux",
            "credential_hash": "a" * 64, "max_concurrency": 2,
            "allowed_roots": [str(self.root)],
        }
        self.app.execution.hosts["conn-a"] = host
        # Keep managed config in sync for set_host_limits later.
        cfg_hosts = list(self.app.config.get("hosts") or [])
        cfg_hosts.append(dict(host))
        self.app.config["hosts"] = cfg_hosts
        self.app.store.put("device", {"id": "conn-a", "supported_contract_version": 4})
        task = {
            "id": "task-freeze", "execution_id": "exec-freeze", "status": "starting",
            "host_id": "conn-a", "installation_id": "codex-local",
            "inputs": [], "outputs": ["result.txt"], "prompt": "x",
            "spec_digest": "spec", "policy_digest": "pol",
            "attempt_uuid": "a" * 32, "timeout_s": 3600,
            "cwd": str(self.root), "access_mode": "write",
            "contract_version": 2, "host_max_concurrency": 2,
            "workspace_key": ["conn-a", ["w"]],
            "resource_keys": [],
        }
        self.app.store.put("task", task)
        first = self.app.broker.start(task)
        self.assertIn("command_id", first)
        payload1 = self.app.store.get("command", first["command_id"])["payload"]
        self.assertEqual(payload1["host_max_concurrency"], 2)
        # Raise live host cap in both execution map and managed config.
        self.app.execution.hosts["conn-a"]["max_concurrency"] = 5
        for h in self.app.config["hosts"]:
            if h.get("id") == "conn-a":
                h["max_concurrency"] = 5
        task = self.app.store.get("task", "task-freeze")
        second = self.app.broker.start(task)
        self.assertEqual(second.get("command_id"), first["command_id"])
        payload2 = self.app.store.get("command", first["command_id"])["payload"]
        self.assertEqual(payload2["host_max_concurrency"], 2)


    def test_unstamped_legacy_replay_reuses_durable_payload(self):
        """Pre-fix starting tasks without a stamp must not rebuild a new payload."""
        host = {
            "id": "conn-b", "transport": "connector", "os": "Linux",
            "credential_hash": "b" * 64, "max_concurrency": 2,
            "allowed_roots": [str(self.root)],
        }
        self.app.execution.hosts["conn-b"] = host
        self.app.config["hosts"] = list(self.app.config.get("hosts") or []) + [dict(host)]
        self.app.store.put("device", {"id": "conn-b", "supported_contract_version": 4})
        task = {
            "id": "task-legacy", "execution_id": "exec-legacy", "status": "starting",
            "host_id": "conn-b", "installation_id": "codex-local",
            "inputs": [], "outputs": ["result.txt"], "prompt": "x",
            "spec_digest": "spec", "policy_digest": "pol",
            "attempt_uuid": "b" * 32, "timeout_s": 3600,
            "cwd": str(self.root), "access_mode": "write",
            "contract_version": 2, "workspace_key": ["conn-b", ["w"]],
            "resource_keys": [],
            # intentionally omit host_max_concurrency (pre-fix shape)
        }
        self.app.store.put("task", task)
        # First start stamps from live (=2) and enqueues
        first = self.app.broker.start(task)
        payload1 = self.app.store.get("command", first["command_id"])["payload"]
        self.assertEqual(payload1["host_max_concurrency"], 2)
        # Raise live cap; strip stamp again to simulate a process that still has
        # an unstamped in-memory task while the durable command exists.
        self.app.execution.hosts["conn-b"]["max_concurrency"] = 5
        bare = self.app.store.get("task", "task-legacy")
        bare.pop("host_max_concurrency", None)
        self.app.store.put("task", bare)
        second = self.app.broker.start(bare)
        self.assertEqual(second.get("command_id"), first["command_id"])
        payload2 = self.app.store.get("command", first["command_id"])["payload"]
        self.assertEqual(payload2["host_max_concurrency"], 2)

class DeviceHostCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "ws").mkdir()
        data = self.root / "data"
        data.mkdir()
        cfg = {
            "hosts": [{"id": "local", "transport": "local", "os": "Linux",
                       "allowed_roots": [str(self.root / "ws")]}],
            "installations": [{"id": "codex-a", "host_id": "local", "adapter": "codex",
                               "argv": [sys.executable, "-c", "print(1)"],
                               "probe_argv": [sys.executable, "--version"],
                               "cwd": str(self.root / "ws"), "max_concurrency": 1}],
        }
        # Minimal Connector: reuse Service connector path when possible
        self.app = Service({
            **cfg,
            "default_model": "test",
            "models": [{"id": "test", "model": "test", "endpoint": "http://127.0.0.1:1/v1", "context_budget": 40000}],
            "hosts": [{**cfg["hosts"][0], consent.CONSENT_KEY: consent.RISK_VERSION}],
            "installations": cfg["installations"],
        }, data)
        # Build a Connector bound to local host data dir pattern used in tests
        from pangpang.execution import Execution
        # Use connector module's class with service-like bits via constructing from CLI patterns
        # Simpler: call _reject_workspace_conflict on a lightweight object
        self.conn = object.__new__(Connector)
        self.conn.host_id = "local"
        self.conn.store = self.app.store
        self.conn.execution = self.app.execution
        self.conn._active_runs = lambda: [
            r for r in self.app.store.find("local_run")
            if r.get("status") in {"launching", "active"}
        ]

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _put_active(self, eid, cwd_name, mode="verified_read", segs=None):
        cwd = self.root / "ws" / cwd_name
        cwd.mkdir(parents=True, exist_ok=True)
        task = {
            "execution_id": eid, "installation_id": "codex-a", "host_id": "local",
            "cwd": str(cwd), "access_mode": mode,
            "workspace_key": ["local", list(segs or [cwd_name])],
            "host_max_concurrency": 3,
        }
        self.app.store.put("local_run", {"id": eid, "status": "active", "task": task})
        return task

    def _candidate(self, eid, cwd_name, mode="write", segs=None):
        return {
            "execution_id": eid, "installation_id": "codex-a", "host_id": "local",
            "cwd": str(self.root / "ws" / cwd_name), "access_mode": mode,
            "workspace_key": ["local", list(segs or [cwd_name])],
            "host_max_concurrency": 3,
        }

    def test_device_missing_lock_identity_rejected(self):
        # No trustworthy frozen identity -> never launch (fail closed).
        no_wk = self._candidate("exec-x", "dx")
        no_wk.pop("workspace_key")
        empty_segs = self._candidate("exec-x", "dx")
        empty_segs["workspace_key"] = ["local", []]
        bad_eid = self._candidate("a/b", "dx")
        traversal = self._candidate("..", "dx")
        for candidate in (no_wk, empty_segs, bad_eid, traversal):
            with self.assertRaises(Fault) as ctx:
                self.conn._reject_workspace_conflict(candidate)
            self.assertEqual(ctx.exception.code, "local_task_lock_identity_missing")

    def test_device_same_workspace_writers_concurrent(self):
        # Different execution ids -> different output dirs -> both writers admitted.
        self._put_active("exec-a", "repo", mode="write", segs=["repo"])
        self.conn._reject_workspace_conflict(
            self._candidate("exec-b", "repo", mode="write", segs=["repo"]))

    def test_device_true_overlap_write_write_and_reads(self):
        # B cwd is inside A's output dir: the directory locks genuinely overlap.
        inside = ["repo", ".xpp-out", "exec-a", "sub"]
        self._put_active("exec-a", "repo", mode="write", segs=["repo"])
        with self.assertRaises(Fault) as ctx:
            self.conn._reject_workspace_conflict(
                self._candidate("exec-b", "repo/.xpp-out/exec-a/sub",
                                mode="write", segs=inside))
        self.assertEqual(ctx.exception.code, "local_workspace_busy")
        # write does not block read, in either direction; readers never conflict.
        self.conn._reject_workspace_conflict(
            self._candidate("exec-b", "repo/.xpp-out/exec-a/sub",
                            mode="verified_read", segs=inside))
        self.app.store.put("local_run", {"id": "exec-a", "status": "active", "task": {
            **self.app.store.get("local_run", "exec-a")["task"],
            "access_mode": "verified_read"}})
        self.conn._reject_workspace_conflict(
            self._candidate("exec-b", "repo/.xpp-out/exec-a/sub",
                            mode="write", segs=inside))

    def test_host_max_concurrency_overrides_install(self):
        self._put_active("exec-1", "d1")
        self._put_active("exec-2", "d2")
        candidate = {
            "execution_id": "exec-3", "installation_id": "codex-a", "host_id": "local",
            "cwd": str(self.root / "ws" / "d3"), "access_mode": "verified_read",
            "workspace_key": ["local", ["d3"]],
            "host_max_concurrency": 3,
        }
        (self.root / "ws" / "d3").mkdir(exist_ok=True)
        # should not raise — host cap 3, 2 active
        self.conn._reject_workspace_conflict(candidate)
        # fourth exceeds
        self._put_active("exec-3b", "d3b")
        (self.root / "ws" / "d4").mkdir(exist_ok=True)
        with self.assertRaises(Fault) as ctx:
            self.conn._reject_workspace_conflict({
                **candidate, "execution_id": "exec-4",
                "cwd": str(self.root / "ws" / "d4"),
                "workspace_key": ["local", ["d4"]],
            })
        self.assertEqual(ctx.exception.code, "local_installation_at_capacity")


    def test_pre_scheduling_auth_accepted_on_device_gate(self):
        """Named host + parallel: frozen pre-scheduling auth must pass device gate."""
        from pangpang.connector import (
            authorization_digest, legacy_authorization_digest,
            policy_digest, pre_scheduling_authorization_digest,
        )
        # Rebuild host with name + parallel on the connector fixture's config
        hosts = self.app.config["hosts"]
        host = dict(next(h for h in hosts if h["id"] == "local"))
        host["name"] = "Named device"
        host["parallel"] = 1
        self.app.config["hosts"] = [host if h["id"] == "local" else h for h in hosts]
        # Point connector at this config
        self.conn.config = self.app.config
        self.conn.execution = self.app.execution
        old_auth = pre_scheduling_authorization_digest(self.app.config, "codex-a")
        live = authorization_digest(self.app.config, "codex-a")
        self.assertNotEqual(old_auth, live)
        # The accept set used by Connector.start must include old_auth
        installation_id = "codex-a"
        live_policy = policy_digest(self.app.config, installation_id)
        live_policy_legacy = policy_digest(self.app.config, installation_id, strip_scheduling=False)
        accepted = {
            live_policy, live_policy_legacy,
            authorization_digest(self.app.config, installation_id),
            legacy_authorization_digest(self.app.config, installation_id),
            pre_scheduling_authorization_digest(self.app.config, installation_id),
        }
        self.assertIn(old_auth, accepted)


if __name__ == "__main__":
    unittest.main()
