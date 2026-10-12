"""Stored installations whose adapter is no longer in the catalog still load.

A startup file, a direct Execution(), and a write that adds or edits an unknown
adapter still reject it. A write that leaves that row identical does not.
Pairing distinguishes an answered 4xx from a transport or TLS failure.
Service templates can name a port and document local execution only as a comment.
"""
import contextlib
import datetime
import http.server
import io
import json
import logging
import os
import plistlib
import re
import socket
import sqlite3
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pangpang.backup import create_consistent_backup
from pangpang.connector import Connector, paired_config
from pangpang.execution import ADAPTER_CATALOG, Execution
from pangpang.http import serve
from pangpang.local_setup import LocalSetup
from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Fault, Store
from pangpang.vault import Vault


CLAUDE_ARGV = ["-p", "--permission-mode", "acceptEdits", "--allowed-tools", "Read", "Write"]
LEGACY_ROW = {"binary": "legacy-private-cli", "argv": ["-p"], "concurrency": 2, "priority": 10,
            "use_when": "自定义执行器。"}


def _installation(adapter, argv, workspace, host="host-local"):
    return {
        "id": "inst-local-" + adapter,
        "host_id": host,
        "adapter": adapter,
        "argv": argv,
        "cwd": str(workspace),
        "enabled": True,
        "prompt_mode": "stdin",
        "risk": "write",
        "timeout_s": 3600,
        "max_concurrency": 2 if adapter == "legacy-private-cli" else 1,
        "workspace_key": [host, ["ws"]],
        "workspace_cwd": str(workspace),
    }


def _managed(workspace, installations):
    config = empty_config()
    config["hosts"] = [{
        "id": "host-local", "name": "本机", "transport": "local", "os": "Linux",
        "allowed_roots": [str(workspace)],
    }]
    config["installations"] = installations
    return config


def _settings_blob(db_path):
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT data FROM objects WHERE kind='settings' AND id='managed'").fetchone()
    finally:
        connection.close()
    return row[0]


def _seed(data, config, probes=()):
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "bots.db")
    store.put("settings", {"id": "managed", "config": config})
    for probe in probes:
        store.put("probe", probe)
    store.close()
    return _settings_blob(data / "bots.db")


def _probe(installation_id):
    return {"id": installation_id, "available": True, "supervision": "windows-job-object"}


def _qwen_and_codex(workspace):
    return [
        _installation("legacy-private-cli", ["/usr/bin/legacy-private-cli", "-p"], workspace),
        _installation("codex", ["/usr/bin/codex", "exec", "-"], workspace),
    ]


def _release_rows(workspace):
    """Four rows a 0.9.0 first start writes when those four CLIs are on PATH."""
    return [
        _installation("legacy-private-cli", ["/usr/bin/legacy-private-cli", "-p"], workspace),
        _installation("claude-code", ["/usr/bin/claude", *CLAUDE_ARGV], workspace),
        _installation("codex", ["/usr/bin/codex", "exec", "-"], workspace),
        _installation("gemini", ["/usr/bin/gemini", "--skip-trust"], workspace),
    ]


class StoredUnknownAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.apps = []
        self.addCleanup(self._close_apps)
        self.addCleanup(self._drop_custom_adapter)

    def _close_apps(self):
        for app in self.apps:
            try:
                app.store.close()
            except Exception:
                pass
            vault = getattr(app, "vault", None)
            if vault is not None:
                try:
                    vault.close()
                except Exception:
                    pass

    def _drop_custom_adapter(self):
        from pangpang import adapters_file
        ADAPTER_CATALOG.pop("legacy-private-cli", None)
        adapters_file._merged_names.discard("legacy-private-cli")

    def _start(self, installations, probes=()):
        config = _managed(self.workspace, installations)
        before = _seed(self.data, config, probes)
        messages = []
        handler = logging.Handler()
        handler.emit = lambda record: messages.append(record.getMessage())
        logger = logging.getLogger("pangpang.execution")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.WARNING)
        try:
            app = Service(empty_config(), self.data, deployment_level="L0")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.apps.append(app)
        return app, before, messages

    def test_stored_unknown_adapter_is_unavailable_and_row_is_unchanged(self):
        self.assertNotIn("legacy-private-cli", ADAPTER_CATALOG)
        app, before, logs = self._start(
            _qwen_and_codex(self.workspace),
            probes=[_probe("inst-local-codex"), _probe("inst-local-legacy-private-cli")])
        self.assertEqual(_settings_blob(self.data / "bots.db"), before)
        marked = app.execution.unknown_adapters["inst-local-legacy-private-cli"]
        self.assertEqual(marked["reason"], "unknown_adapter")
        self.assertEqual(marked["adapter"], "legacy-private-cli")
        self.assertNotIn("Setup", marked["advice"])
        self.assertIn("declare the adapter as a custom row", marked["advice"])
        self.assertIn(str(self.data / "adapters.json"), marked["advice"])
        self.assertIn("re-enable", marked["advice"])
        matching = [line for line in logs if "inst-local-legacy-private-cli" in line and "legacy-private-cli" in line]
        self.assertEqual(len(matching), 1)
        self.assertNotIn("Setup", matching[0])
        self.assertIn("declare the adapter as a custom row", matching[0])
        self.assertIn("adapters.json", matching[0])
        route = app.route("bot-main", {"id": "work-1"}, {})
        self.assertEqual(route["selected"]["installation_id"], "inst-local-codex")
        self.assertNotIn("inst-local-legacy-private-cli",
                         [row["installation_id"] for row in route["candidates"]])
        rejected = {row["installation_id"]: row["reason"] for row in route["rejected"]}
        self.assertEqual(rejected["inst-local-legacy-private-cli"], "unknown_adapter")
        self.assertEqual(_settings_blob(self.data / "bots.db"), before)
        probed = app.probe("inst-local-legacy-private-cli")
        self.assertFalse(probed["available"])
        self.assertEqual(probed["reason"], "unknown_adapter")
        stored = json.loads(_settings_blob(self.data / "bots.db"))
        legacy = next(row for row in stored["config"]["installations"] if row["adapter"] == "legacy-private-cli")
        self.assertEqual(legacy["argv"], ["/usr/bin/legacy-private-cli", "-p"])
        self.assertNotIn("unknown_adapter", legacy)

    def test_custom_adapters_file_makes_the_same_row_available(self):
        self.data.mkdir()
        path = self.data / "adapters.json"
        path.write_text(json.dumps({"legacy-private-cli": LEGACY_ROW}), encoding="utf-8")
        path.chmod(0o600)
        app, before, logs = self._start(
            _qwen_and_codex(self.workspace),
            probes=[_probe("inst-local-codex"), _probe("inst-local-legacy-private-cli")])
        self.assertEqual(_settings_blob(self.data / "bots.db"), before)
        self.assertNotIn("inst-local-legacy-private-cli", app.execution.unknown_adapters)
        self.assertEqual(app.execution.installations["inst-local-legacy-private-cli"]["adapter"], "legacy-private-cli")
        self.assertFalse(any("inst-local-legacy-private-cli" in line for line in logs))
        route = app.route("bot-main", {"id": "work-1"}, {})
        self.assertEqual(route["selected"]["installation_id"], "inst-local-legacy-private-cli")
        self.assertNotIn("unknown_adapter", {row["reason"] for row in route["rejected"]})

    def test_startup_file_rejects_unknown_adapter_and_unchanged_rows_do_not_block_writes(self):
        config = _managed(self.workspace, _qwen_and_codex(self.workspace))
        with self.assertRaises(Fault) as raised:
            Service(config, self.data, deployment_level="L0")
        self.assertEqual(raised.exception.code, "invalid_adapter")
        self.assertIn("adapters.json", raised.exception.advice or "")

        before = _seed(self.data, config, probes=[_probe("inst-local-codex")])
        app = Service(empty_config(), self.data, deployment_level="L0")
        self.apps.append(app)
        candidate = json.loads(json.dumps(app.config))
        candidate["installations"][1]["enabled"] = False
        app.commit_managed_config(candidate)
        stored = json.loads(_settings_blob(self.data / "bots.db"))
        legacy = next(row for row in stored["config"]["installations"] if row["adapter"] == "legacy-private-cli")
        original = next(row for row in json.loads(before)["config"]["installations"] if row["adapter"] == "legacy-private-cli")
        self.assertEqual(legacy, original)
        self.assertFalse(app.execution.installations["inst-local-codex"]["enabled"])
        self.assertIn("inst-local-legacy-private-cli", app.execution.unknown_adapters)
        with self.assertRaises(Fault) as direct:
            Execution(candidate, self.data)
        self.assertEqual(direct.exception.code, "invalid_adapter")
        self.assertTrue(direct.exception.advice)

        mutated = json.loads(json.dumps(app.config))
        for row in mutated["installations"]:
            if row["adapter"] == "legacy-private-cli":
                row["enabled"] = False
        before_mutation = _settings_blob(self.data / "bots.db")
        with self.assertRaises(Fault) as rejected:
            app.commit_managed_config(mutated)
        self.assertEqual(rejected.exception.code, "invalid_adapter")
        self.assertIn("inst-local-legacy-private-cli", rejected.exception.advice or "")
        self.assertIn("adapters.json", rejected.exception.advice or "")
        self.assertEqual(_settings_blob(self.data / "bots.db"), before_mutation)

        removed = json.loads(json.dumps(app.config))
        removed["installations"] = [row for row in removed["installations"] if row["adapter"] != "legacy-private-cli"]
        removed["installations"][0]["enabled"] = False
        app.commit_managed_config(removed)
        self.assertNotIn("legacy-private-cli", [row["adapter"] for row in app.config["installations"]])
        self.assertEqual(app.execution.installations["inst-local-codex"]["enabled"], False)
        self.assertNotIn("legacy-private-cli", app.execution.unknown_adapters)

    def test_connector_load_tolerates_and_device_save_rejects(self):
        config = _managed(self.workspace, _qwen_and_codex(self.workspace))
        config["host_id"] = "host-local"
        config["cloud_url"] = "http://127.0.0.1:9"
        self.data.mkdir()
        store = Store(self.data / "connector.db")
        store.put("settings", {"id": "managed", "config": config})
        store.close()
        before = _settings_blob(self.data / "connector.db")
        connector = Connector(config, self.data)
        self.addCleanup(connector.close)
        self.assertEqual(connector.execution.unknown_adapters["inst-local-legacy-private-cli"]["reason"],
                         "unknown_adapter")
        self.assertIn("inst-local-codex", connector.execution.installations)
        self.assertEqual(_settings_blob(self.data / "connector.db"), before)
        with self.assertRaises(Fault) as raised:
            LocalSetup(connector).save(json.loads(json.dumps(connector.config)))
        self.assertEqual(raised.exception.code, "invalid_adapter")
        self.assertEqual(_settings_blob(self.data / "connector.db"), before)
        fresh = self.root / "fresh-connector"
        with self.assertRaises(Fault) as startup:
            Connector(config, fresh)
        self.assertEqual(startup.exception.code, "invalid_adapter")

    def test_connector_probe_does_not_run_an_unknown_adapter(self):
        config = _managed(self.workspace, _qwen_and_codex(self.workspace))
        config["host_id"] = "host-local"
        config["cloud_url"] = "http://127.0.0.1:9"
        data = self.root / "probe-connector"
        data.mkdir()
        store = Store(data / "connector.db")
        store.put("settings", {"id": "managed", "config": config})
        store.close()
        before = _settings_blob(data / "connector.db")
        connector = Connector(config, data)
        self.addCleanup(connector.close)

        def probe(installation_id, command=False):
            spawned = []

            def fake_run(argv, input=None, stdout=None, stderr=None, timeout=None):
                spawned.append(list(argv))
                stdout.write(b'{"available": true, "supervision": "windows-job-object"}')
                return SimpleNamespace(returncode=0)

            with patch("pangpang.execution.subprocess.run", side_effect=fake_run):
                if command:
                    connector.execute({
                        "id": "cmd-" + installation_id,
                        "action": "probe",
                        "payload": {"installation_id": installation_id},
                    })
                    result = connector.store.get("local_command", "cmd-" + installation_id)["result"]
                else:
                    result = connector.execution.probe(installation_id)
            return spawned, result

        spawned, direct = probe("inst-local-legacy-private-cli")
        self.assertEqual(spawned, [])
        self.assertEqual(direct, {"available": False, "reason": "unknown_adapter"})
        spawned, reported = probe("inst-local-legacy-private-cli", command=True)
        self.assertEqual(spawned, [])
        self.assertFalse(reported["available"])
        self.assertEqual(reported["reason"], "unknown_adapter")
        self.assertIn("policy_digest", reported)
        spawned, known = probe("inst-local-codex")
        self.assertTrue(spawned)
        self.assertIn("probe", spawned[0])
        self.assertTrue(known["available"])
        self.assertEqual(_settings_blob(data / "connector.db"), before)

    def test_startup_fault_after_construct_prints_code_without_traceback(self):
        from pangpang.__main__ import main
        argv = ["pangpang", "--data", str(self.root / "consent-cloud"),
                "--config", str(self.root / "missing.json"), "--allow-local-exec", "--check"]
        err = io.StringIO()

        def reject(self, candidate, execution=None, base_revision=None):
            raise Fault("state_conflict", advice="managed config changed")

        with patch.dict(os.environ, {"PANGPANG_TOKEN": "t" * 24}, clear=False), \
                patch("sys.argv", argv), \
                patch.object(Service, "commit_managed_config", reject), \
                patch("pangpang.service.shutil.which", return_value=None), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main()
        text = err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("state_conflict: managed config changed", text)
        self.assertNotIn("Traceback", text)

        err = io.StringIO()
        out = io.StringIO()
        argv = ["pangpang", "--data", str(self.root / "consent-cloud-ok"),
                "--config", str(self.root / "missing.json"), "--allow-local-exec", "--check"]
        with patch.dict(os.environ, {"PANGPANG_TOKEN": "t" * 24}, clear=False), \
                patch("sys.argv", argv), \
                patch("pangpang.service.shutil.which", return_value=None), \
                patch("pangpang.service.fetch_pinned_pi", return_value=None), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(out):
            os.environ.pop("TYPESAFE_API_KEY", None)
            code = main()
        self.assertEqual(code, 0, err.getvalue() + out.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("state_conflict", err.getvalue())

    def test_release_rows_pass_cloud_check_without_rewriting_settings(self):
        config = _managed(self.workspace, _release_rows(self.workspace))
        probes = [_probe("inst-local-" + name) for name in ("claude-code", "codex", "gemini", "legacy-private-cli")]
        before = _seed(self.data, config, probes=probes)
        from pangpang.__main__ import main
        argv = ["pangpang", "--data", str(self.data), "--config", str(self.root / "missing.json"), "--check"]
        out, err = io.StringIO(), io.StringIO()
        env = {"PANGPANG_TOKEN": "t" * 24}
        with patch.dict(os.environ, env, clear=False), \
                patch("sys.argv", argv), \
                patch("pangpang.service.shutil.which", return_value=None), \
                patch("pangpang.service.fetch_pinned_pi", return_value=None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            os.environ.pop("TYPESAFE_API_KEY", None)
            code = main()
        self.assertEqual(code, 0, err.getvalue() + out.getvalue())
        self.assertIn("self-check:", out.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("Traceback", out.getvalue())
        self.assertEqual(_settings_blob(self.data / "bots.db"), before)
        stored = json.loads(before)
        adapters = [row["adapter"] for row in stored["config"]["installations"]]
        self.assertEqual(adapters, ["legacy-private-cli", "claude-code", "codex", "gemini"])

    def test_startup_fault_prints_code_and_advice_without_traceback(self):
        from pangpang.__main__ import main
        config = _managed(self.workspace, _qwen_and_codex(self.workspace))
        path = self.root / "runtime.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        argv = ["pangpang", "--data", str(self.root / "empty-cloud"), "--config", str(path), "--check"]
        err = io.StringIO()
        with patch.dict(os.environ, {"PANGPANG_TOKEN": "t" * 24}, clear=False), \
                patch("sys.argv", argv), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main()
        text = err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("invalid_adapter:", text)
        self.assertIn("adapters.json", text)
        self.assertNotIn("Traceback", text)

        from pangpang.connector import main as connector_main
        connector_config = dict(config, host_id="host-local", cloud_url="http://127.0.0.1:9")
        connector_path = self.root / "connector.json"
        connector_path.write_text(json.dumps(connector_config), encoding="utf-8")
        argv = ["pangpang.connector", "--data", str(self.root / "empty-connector"), "--config", str(connector_path)]
        out = io.StringIO()
        with patch("sys.argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = connector_main()
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("invalid_adapter:", text)
        self.assertIn("adapters.json", text)
        self.assertNotIn("Traceback", text)


class UnknownAdapterBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.data = self.root / "cloud"
        self.data.mkdir()
        self.addCleanup(self._drop_custom_adapter)

    def _drop_custom_adapter(self):
        from pangpang import adapters_file
        ADAPTER_CATALOG.pop("legacy-private-cli", None)
        adapters_file._merged_names.discard("legacy-private-cli")

    def _vault(self):
        vault = Vault(self.data / "vault")
        vault.set("web-access-token", "t" * 32)
        vault.close()

    def test_backup_lists_unknown_adapter_without_blocking_upgrade(self):
        _seed(self.data, _managed(self.workspace, _qwen_and_codex(self.workspace)))
        self._vault()
        dest = self.root / "backup"
        result = create_consistent_backup(self.data, dest, confirm=True)
        self.assertTrue(result["upgrade_allowed"])
        self.assertNotIn("unknown_adapter", result["upgrade_blocked"])
        listed = result["manifest"]["inventory"]["unknown_adapter_installations"]
        self.assertEqual([row["id"] for row in listed], ["inst-local-legacy-private-cli"])
        self.assertEqual(listed[0]["adapter"], "legacy-private-cli")
        self.assertNotIn("Setup", listed[0]["advice"])
        self.assertIn("declare the adapter as a custom row", listed[0]["advice"])
        self.assertIn(str(self.data / "adapters.json"), listed[0]["advice"])
        self.assertIn("re-enable", listed[0]["advice"])
        live = _settings_blob(self.data / "bots.db")
        again = create_consistent_backup(self.data, self.root / "backup-2", confirm=True)
        self.assertEqual(_settings_blob(self.data / "bots.db"), live)
        self.assertEqual(again["manifest"]["inventory"]["unknown_adapter_installations"], listed)

    def test_declared_custom_adapter_is_absent_from_the_inventory_list(self):
        _seed(self.data, _managed(self.workspace, _qwen_and_codex(self.workspace)))
        self._vault()
        path = self.data / "adapters.json"
        path.write_text(json.dumps({"legacy-private-cli": LEGACY_ROW}), encoding="utf-8")
        path.chmod(0o600)
        result = create_consistent_backup(self.data, self.root / "backup", confirm=True)
        self.assertTrue(result["upgrade_allowed"])
        self.assertEqual(result["manifest"]["inventory"]["unknown_adapter_installations"], [])

    def test_known_installations_only_list_nothing(self):
        rows = _release_rows(self.workspace)[1:]
        _seed(self.data, _managed(self.workspace, rows))
        self._vault()
        result = create_consistent_backup(self.data, self.root / "backup", confirm=True)
        self.assertTrue(result["upgrade_allowed"])
        self.assertEqual(result["manifest"]["inventory"]["unknown_adapter_installations"], [])


class PairingErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()

    def _pair(self, cloud, code="wrong-code"):
        self.pair_index = getattr(self, "pair_index", 0) + 1
        args = SimpleNamespace(data=str(self.root / ("pair-%s" % self.pair_index)),
                               cloud=cloud, workspace=str(self.workspace), name="box")
        with patch("getpass.getpass", return_value=code):
            with self.assertRaises(Fault) as raised:
                paired_config(args)
        return raised.exception

    def test_wrong_code_closed_port_and_untrusted_certificate_differ(self):
        cloud_data = self.root / "cloud"
        app = Service(empty_config(), cloud_data, deployment_level="L0")
        self.addCleanup(app.store.close)
        server = serve(app, "t" * 24, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        rejected = self._pair("http://127.0.0.1:%s" % server.server_port)
        self.assertEqual(rejected.code, "pairing_code_rejected")
        self.assertIn("reachable", rejected.advice)
        self.assertIn("wrong or expired", rejected.advice)
        self.assertIn("web UI", rejected.advice)
        self.assertNotIn("无法连接", rejected.advice)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
        sock.close()
        unreachable = self._pair("http://127.0.0.1:%s" % closed)
        self.assertEqual(unreachable.code, "pairing_failed_check_code_and_cloud")
        self.assertIn("无法连接", unreachable.advice)

        cert, key = self._certificate()
        https = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        https.socket = context.wrap_socket(https.socket, server_side=True)
        https_thread = threading.Thread(target=https.serve_forever, daemon=True)
        https_thread.start()
        self.addCleanup(https.shutdown)
        self.addCleanup(https.server_close)
        untrusted = self._pair("https://127.0.0.1:%s" % https.server_port)
        self.assertEqual(untrusted.code, "connector_tls_untrusted")
        self.assertNotEqual(rejected.code, unreachable.code)
        self.assertNotEqual(rejected.code, untrusted.code)
        self.assertNotEqual(unreachable.code, untrusted.code)

    def test_http_500_keeps_the_generic_pairing_fault(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(500)
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        fault = self._pair("http://127.0.0.1:%s" % server.server_port)
        self.assertEqual(fault.code, "pairing_failed_check_code_and_cloud")
        self.assertNotEqual(fault.code, "pairing_code_rejected")

    def _certificate(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(1)
                .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
                .sign(key, hashes.SHA256()))
        key_path = self.root / "key.pem"
        cert_path = self.root / "cert.pem"
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cert_path, key_path


class ServiceTemplateTests(unittest.TestCase):
    def test_port_is_optional_and_local_exec_stays_commented_out(self):
        root = Path(__file__).resolve().parents[1] / "deploy"
        script = (root / "start-cloud.ps1").read_text(encoding="utf-8")
        port_line = next(line for line in script.splitlines() if "[int]$Port" in line)
        self.assertNotIn("Mandatory", port_line)
        self.assertIn('ContainsKey("Port")', script)
        self.assertIn('"--port"', script)
        self.assertLess(script.index('ContainsKey("Port")'), script.index('"--port"'))
        self.assertNotIn("--allow-local-exec", script)
        self.assertNotIn("& $Python -m pangpang.cli cloud --data $Data", script)

        service = (root / "pangpang-cloud.service").read_text(encoding="utf-8")
        self.assertIn("--allow-local-exec", service)
        self.assertIn("--port", service)
        self.assertIn("local execution host", service)
        active = _without_comments("pangpang-cloud.service", service)
        self.assertNotIn("--allow-local-exec", active)
        self.assertNotIn("--port", active)

        plist_text = (root / "com.pangpang.cloud.plist").read_text(encoding="utf-8")
        parsed = plistlib.loads(plist_text.encode("utf-8"))
        arguments = " ".join(parsed["ProgramArguments"])
        self.assertIn("pangpang.cli", arguments)
        self.assertNotIn("allow-local-exec", arguments)
        self.assertNotIn("--port", parsed["ProgramArguments"])
        comments = re.findall(r"<!--(.*?)-->", plist_text, flags=re.S)
        self.assertGreaterEqual(len(comments), 3)
        for comment in comments:
            self.assertNotIn("--", comment)
        self.assertTrue(any("allow-local-exec" in comment and "local execution host" in comment
                            for comment in comments))
        self.assertTrue(any("port" in comment and "8790" in comment for comment in comments))

    def test_removed_private_adapter_name_is_absent_from_shipped_files(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(_shipped_hits(root), [])
        planted = self._planted_tree()
        self.assertEqual(_shipped_hits(planted), ["pangpang/note.txt"])

    def _planted_tree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        note = root / "pangpang"
        note.mkdir()
        (note / "note.txt").write_text("name=qwen-claude\n", encoding="utf-8")
        (note / "clean.txt").write_text("codex\n", encoding="utf-8")
        (root / "README.md").write_text("coordinator\n", encoding="utf-8")
        return root


def _shipped_hits(root):
    """Files under the shipped trees that still contain the removed adapter name."""
    needle = b"qwen-claude"
    hits = []
    trees = [root / "pangpang", root / "deploy", root / "config", root / "docs"]
    files = []
    for tree in trees:
        if tree.is_dir():
            files.extend(path for path in tree.rglob("*") if path.is_file())
    files.extend(path for path in root.glob("README*") if path.is_file())
    security = root / "SECURITY.md"
    if security.is_file():
        files.append(security)
    for path in files:
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if needle in path.read_bytes():
            hits.append(str(path.relative_to(root)))
    return sorted(hits)


def _without_comments(name, text):
    if name.endswith(".plist"):
        return re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


if __name__ == "__main__":
    unittest.main()
