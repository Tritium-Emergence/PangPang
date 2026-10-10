# -*- coding: utf-8 -*-
""" first-start self-check: format and exit code.

The report is Service.effective_capabilities, in order, with that projection's
own reason and operator_action. It does not add a second checklist.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pangpang.__main__ import main, render_self_check
from pangpang.logs import configure_logging
from pangpang.onboarding import empty_config
from pangpang.service import Service
from test_firstrun import _ENV_VARS, _restore_env, caps, commit_model


def parse_self_check(text):
    lines = text.splitlines()
    if not lines or lines[0] != "self-check:":
        raise AssertionError(text)
    rows = {}
    for line in lines[1:]:
        capability, state, reason, action = line.split("\t", 3)
        rows[capability] = {"state": state, "reason": reason, "operator_action": action}
    return rows


def rendered_field(value):
    return value if value else "-"


class SelfCheckFormatTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in _ENV_VARS}
        for key in ("TAVILY_API_KEY", "TYPESAFE_API_KEY"):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Service(empty_config(), Path(self.tmp.name) / "data")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()
        _restore_env(self._env)

    def test_report_matches_effective_capabilities_and_exits_ok(self):
        text, code = render_self_check(self.app)
        rows = parse_self_check(text)
        projection = self.app.effective_capabilities()
        self.assertEqual(code, 0)
        self.assertEqual(list(rows), [entry["capability"] for entry in projection])
        self.assertTrue(all(entry["state"] != "blocked" for entry in projection))
        for entry in projection:
            row = rows[entry["capability"]]
            self.assertEqual(row["state"], entry["state"])
            self.assertEqual(row["reason"], rendered_field(entry["reason"]))
            self.assertEqual(row["operator_action"], rendered_field(entry["operator_action"]))
            if entry["state"] != "ready":
                self.assertEqual(row["operator_action"], entry["operator_action"])

    def test_blocked_exit_uses_the_projection_operator_action(self):
        self.app.semantic_gate.mode = "enforce"
        text, code = render_self_check(self.app)
        entry = caps(self.app)["semantic_gate"]
        self.assertEqual(entry["state"], "blocked")
        self.assertEqual(code, 1)
        row = parse_self_check(text)["semantic_gate"]
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["reason"], entry["reason"])
        self.assertEqual(row["operator_action"], entry["operator_action"])
        self.assertIn("TYPESAFE_API_KEY", row["operator_action"])

    def test_unknown_is_reported_and_fails_the_check(self):
        commit_model(self.app)
        self.app.store.put("settings", {"id": "managed", "config": self.app.config})
        self.assertEqual(caps(self.app)["model"]["state"], "unknown")
        text, code = render_self_check(self.app)
        self.assertEqual(code, 1)
        row = parse_self_check(text)["model"]
        entry = caps(self.app)["model"]
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["reason"], entry["reason"])
        self.assertEqual(row["operator_action"], entry["operator_action"])
        self.assertTrue(all(item["state"] != "blocked" for item in self.app.effective_capabilities()))

    def test_unverified_supervision_names_the_deployment_level_fix(self):
        from test_bot_runtime import config
        root = Path(self.tmp.name) / "workspace"
        root.mkdir()
        app = Service(config(root), Path(self.tmp.name) / "exec-data", deployment_level=None)
        try:
            app.store.put("probe", {"id": "codex-local", "available": True,
                                    "supervision": "posix-pgroup-cooperative"})
            entry = caps(app)["executor"]
            self.assertEqual(entry["state"], "blocked")
            self.assertEqual(entry["reason"], "platform_supervision_unverified")
            self.assertEqual(entry["operator_action_code"], "set_deployment_level")
            self.assertIn("--deployment-level L0", entry["operator_action"])
            self.assertIn("SECURITY.md", entry["operator_action"])
            text, code = render_self_check(app)
            self.assertEqual(code, 1)
            row = parse_self_check(text)["executor"]
            self.assertEqual(row["state"], "blocked")
            self.assertEqual(row["reason"], "platform_supervision_unverified")
            self.assertEqual(row["operator_action"], entry["operator_action"])
        finally:
            app.store.close()

    def test_l1_unverified_supervision_does_not_suggest_l0(self):
        from test_bot_runtime import config
        root = Path(self.tmp.name) / "workspace-l1"
        root.mkdir()
        app = Service(config(root), Path(self.tmp.name) / "l1-data", deployment_level="L1")
        try:
            app.store.put("probe", {"id": "codex-local", "available": True,
                                    "supervision": "posix-pgroup-cooperative"})
            entry = caps(app)["executor"]
            self.assertEqual(entry["state"], "blocked")
            self.assertEqual(entry["reason"], "platform_supervision_unverified")
            self.assertNotEqual(entry["operator_action_code"], "set_deployment_level")
            self.assertNotIn("--deployment-level L0", entry["operator_action"])
            self.assertNotIn("PANGPANG_DEPLOYMENT_LEVEL", entry["operator_action"])
            self.assertIn("强监管", entry["operator_action"])
        finally:
            app.store.close()

    def test_connector_unverified_supervision_points_to_execution_level(self):
        #: a blocked connector is steered to open THAT device with its own
        # execution_level (the per-device opt-in), not to drop the whole cloud's
        # deployment level. The process-group consent step is named as the second
        # half of the double confirmation for the pgroup tier.
        import time
        root = Path(self.tmp.name) / "workspace-connector"
        root.mkdir()
        cfg = {
            "default_model": "test", "turn_retry_delays_s": [],
            "models": [{"id": "test", "model": "test", "endpoint": "http://127.0.0.1:1/v1",
                        "context_budget": 40000}],
            "hosts": [{"id": "dev1", "transport": "connector", "os": "Linux",
                       "credential_hash": "ab" * 32, "allowed_roots": [str(root)]}],
            "installations": [{"id": "codex-dev1", "host_id": "dev1", "adapter": "codex",
                               "argv": ["codex"], "cwd": str(root), "risk": "write", "enabled": True}],
        }
        app = Service(cfg, Path(self.tmp.name) / "connector-data", deployment_level="L0")
        try:
            app.store.put("device", {"id": "dev1", "instance_id": "device-1"})
            app.broker._last_seen["dev1"] = time.time()
            app.store.put("probe", {"id": "codex-dev1", "available": True,
                                    "supervision": "posix-process-group"})
            entry = caps(app)["executor"]
            self.assertEqual(entry["state"], "blocked")
            self.assertEqual(entry["reason"], "platform_supervision_unverified")
            self.assertEqual(entry["operator_action_code"], "set_host_execution_level")
            self.assertIn("execution_level", entry["operator_action"])
            self.assertIn("pangpang.consent", entry["operator_action"])
            self.assertIn("连接器", entry["operator_action"])
        finally:
            app.store.close()


class SelfCheckCliTests(unittest.TestCase):
    def setUp(self):
        configure_logging()
        self._env = {k: os.environ.get(k) for k in ("TAVILY_API_KEY", "TYPESAFE_API_KEY", "PANGPANG_TOKEN")}
        # The gate must read as unconfigured on every machine: tests that assert
        # blocked/ready rows render outside _run's with-block, so the keys have
        # to be gone for the whole test, not just during main().
        os.environ.pop("TAVILY_API_KEY", None)
        os.environ.pop("TYPESAFE_API_KEY", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            try:
                app.store.close()
            except Exception:
                pass
        self.tmp.cleanup()
        _restore_env(self._env)

    def _config(self, enforce):
        cfg = empty_config()
        if enforce:
            cfg["semantic_gate"] = {"mode": "enforce"}
        path = self.root / ("enforce.json" if enforce else "empty.json")
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def _run(self, enforce, check):
        data = self.root / ("data-check" if check else "data-serve")
        argv = ["pangpang", "--data", str(data), "--config", str(self._config(enforce)),
                "--allow-local-exec"]
        self.assertNotIn("--deployment-level", argv)
        self.assertNotIn("--port", argv)
        if check:
            argv.append("--check")
        held = {}

        def serve(service, token, host, port, origin):
            held["service"] = service
            held["bind"] = (host, port)

            class Server:
                def serve_forever(self):
                    pass

                def shutdown(self):
                    pass

                def server_close(self):
                    pass

            return Server()

        buf = io.StringIO()
        env = {"PANGPANG_TOKEN": "t" * 24}
        with patch.dict(os.environ, env, clear=False), \
                patch("sys.argv", argv), \
                patch("pangpang.__main__.serve", serve), \
                patch("pangpang.__main__.threading.Thread") as threads, \
                patch("pangpang.service.fetch_pinned_pi", return_value=None), \
                patch("pangpang.service.shutil.which", return_value=None), \
                contextlib.redirect_stdout(buf):
            os.environ.pop("TAVILY_API_KEY", None)
            os.environ.pop("TYPESAFE_API_KEY", None)
            threads.return_value.is_alive.return_value = False
            code = main()
        if "service" in held:
            self.apps.append(held["service"])
        return code, buf.getvalue(), held

    def test_check_exits_nonzero_when_blocked_and_does_not_serve(self):
        code, out, held = self._run(enforce=True, check=True)
        self.assertEqual(code, 1)
        self.assertNotIn("service", held)
        row = parse_self_check(out)["semantic_gate"]
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["reason"], "gate_key_missing_enforce")
        twin = Service(json.loads(self._config(True).read_text(encoding="utf-8")), self.root / "twin-blocked")
        self.apps.append(twin)
        twin_row = parse_self_check(render_self_check(twin)[0])["semantic_gate"]
        self.assertEqual(row["operator_action"], twin_row["operator_action"])

    def test_check_exits_ok_when_nothing_is_blocked(self):
        code, out, held = self._run(enforce=False, check=True)
        self.assertEqual(code, 0)
        self.assertNotIn("service", held)
        rows = parse_self_check(out)
        self.assertTrue(all(row["state"] != "blocked" for row in rows.values()))
        self.assertEqual(rows["model"]["state"], "unconfigured")
        self.assertEqual(rows["model"]["reason"], "model_not_configured")

    def test_bind_failure_names_host_port_and_exits(self):
        data = self.root / "data-bind"
        argv = ["pangpang", "--data", str(data), "--config", str(self._config(False)), "--port", "8791"]

        def serve(service, token, host, port, origin):
            raise OSError(98, "Address already in use")

        err = io.StringIO()
        with patch.dict(os.environ, {"PANGPANG_TOKEN": "t" * 24}, clear=False), \
                patch("sys.argv", argv), \
                patch("pangpang.__main__.serve", serve), \
                patch("pangpang.__main__.threading.Thread") as threads, \
                patch("pangpang.service.fetch_pinned_pi", return_value=None), \
                patch("pangpang.service.shutil.which", return_value=None), \
                patch("sys.stdin.isatty", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            os.environ.pop("TAVILY_API_KEY", None)
            os.environ.pop("TYPESAFE_API_KEY", None)
            threads.return_value.is_alive.return_value = False
            code = main()
        self.assertEqual(code, 1)
        text = err.getvalue()
        self.assertIn("127.0.0.1:8791", text)
        self.assertIn("Address already in use", text)
        self.assertIn("--port", text)
        self.assertNotIn("Traceback", text)

    def test_check_while_the_service_holds_the_lock_is_one_line(self):
        from pangpang.__main__ import ProcessLock
        data = self.root / "data-locked"
        data.mkdir()
        lock = ProcessLock(data / "coordinator.lock")
        argv = ["pangpang", "--data", str(data), "--config", str(self._config(False)), "--check"]
        err = io.StringIO()
        try:
            with patch.dict(os.environ, {"PANGPANG_TOKEN": "t" * 24}, clear=False), \
                    patch("sys.argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = main()
        finally:
            lock.file.close()
        self.assertEqual(code, 1)
        text = err.getvalue()
        self.assertEqual(text, "协调器正在运行并持有进程锁。请先停止服务，再执行 --check。\n")
        self.assertNotIn("Traceback", text)
        self.assertNotIn("BlockingIOError", text)

    def test_startup_prints_the_projection_and_still_serves_when_blocked(self):
        code, out, held = self._run(enforce=True, check=False)
        self.assertIsNone(code)
        self.assertEqual(held["bind"], ("127.0.0.1", 8790))
        text, status = render_self_check(held["service"])
        self.assertEqual(status, 1)
        self.assertEqual(out, text)
        # The install command does not pass the flag. A POSIX personal start
        # (macOS, Linux) infers L0; Windows stays unset.
        expected = "L0" if os.name == "posix" else None
        self.assertEqual(held["service"].deployment_level, expected)
