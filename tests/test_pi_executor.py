""": bundled pi 1.0.3 is an ordinary fallback executor.

The binary is the official standalone build. A stub OpenAI-compatible server
drives tool calls. No real model and no network beyond that stub (or, once,
the pinned release archive when the binary is not already on disk).
"""
import hashlib
import io
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

# runner.py is executed as a script (its directory is sys.path[0]) and imports
# `vault` as a sibling. An in-process supervise() needs that same path.
_PANGPANG_DIR = str(Path(__file__).resolve().parents[1] / "pangpang")
if _PANGPANG_DIR not in sys.path:
    sys.path.insert(0, _PANGPANG_DIR)

from pangpang.execution import (ADAPTER_CATALOG, PI_GUARD_PATH, PI_GUARD_SHA256, PI_LICENSE,
                                PI_LICENSE_SHA256, PI_VERSION, _PI_BASE_URL_ENV,
                                _pi_binary_sha, binary_matches_pin, bundled_pi_path,
                                fetch_pinned_pi, materialize_executor_env, pi_models_api,
                                pinned_digests, write_pi_provider_config)
from pangpang.local_setup import LocalSetup
from pangpang.onboarding import empty_config
from pangpang.service import Service
from pangpang.store import Fault
from pangpang import consent, runner
from test_bot_runtime import config


PI_ARGV = ["--print", "--no-session", "--tools", "read,write,edit,grep,find,ls",
           "--offline", "--no-extensions", "--no-skills", "-e", str(PI_GUARD_PATH)]
_CREATED_BINARY = False


def ensure_pinned_pi():
    """Put the pinned 1.0.3 binary at the catalog path. Same installer as product code."""
    global _CREATED_BINARY
    if not _pi_binary_sha():
        raise unittest.SkipTest("pi 1.0.3 has no pin for %s/%s" % (platform.system(), platform.machine()))
    dest = bundled_pi_path()
    existed = dest.is_file()
    path = fetch_pinned_pi()
    if path is None:
        raise AssertionError("pinned pi 1.0.3 was not installed")
    if not existed:
        _CREATED_BINARY = True
    return path


def setUpModule():
    ensure_pinned_pi()


def tearDownModule():
    if _CREATED_BINARY:
        path = bundled_pi_path()
        if path.exists() or path.is_symlink():
            path.unlink()


def _sse(obj):
    return ("data: " + json.dumps(obj) + "\n\n").encode()


class _Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "done"
    outside = ""
    tool = "read"
    wire = "openai"
    seen = []
    hits = 0

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size)
        type(self).hits += 1
        type(self).seen.append({
            "auth": self.headers.get("Authorization", ""),
            "x-api-key": self.headers.get("x-api-key", ""),
            "path": self.path,
            "body": raw.decode("utf-8", "replace")})
        body = json.loads(raw or b"{}")
        if self.wire == "anthropic":
            if _anthropic_tool_result(body):
                payload = _anthropic_final("done")
            else:
                payload = _anthropic_tool("write", {"path": _output_file(raw), "content": "ok\n"})
            self._send(payload)
            return
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        if last.get("role") == "tool" and self.mode != "grep-then-write":
            payload = _final("done")
        elif last.get("role") == "tool" and self.mode == "grep-then-write":
            # The grep result arrived. Write the required file, then finish on the next turn.
            if "result.txt" in json.dumps(last):
                payload = _final("done")
            else:
                payload = _tool("write", {"path": _output_file(raw), "content": "ok\n"})
        elif self.mode == "abs":
            payload = _tool("write", {"path": self.outside, "content": "pwn\n"})
        elif self.mode == "dotdot":
            payload = _tool("read", {"path": "../secret.txt"})
        elif self.mode == "dirlink":
            payload = _tool("write", {"path": "escape/pwn.txt", "content": "pwn\n"})
        elif self.mode == "dangling":
            payload = _tool("write", {"path": "dangling", "content": "pwn\n"})
        elif self.mode == "link-dotdot":
            payload = _tool("write", {"path": "escape/../escaped.txt", "content": "pwn\n"})
        elif self.mode == "deliver":
            payload = _tool("write", {"path": _output_file(raw), "content": "ok\n"})
        elif self.mode == "given":
            args = {"path": self.outside}
            if self.tool == "write":
                args["content"] = "pwn\n"
            payload = _tool(self.tool, args)
        elif self.mode == "grep-then-write":
            payload = _tool("grep", {"pattern": "token", "path": "."})
        else:
            payload = _final("ok")
        self._send(payload)

    def _send(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _tool(name, args):
    call = {"index": 0, "id": "call_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}
    head = {"choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]}
    tail = {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
    return _sse(head) + _sse(tail) + b"data: [DONE]\n\n"


def _final(text):
    head = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
    tail = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return _sse(head) + _sse(tail) + b"data: [DONE]\n\n"


def _anthropic_tool_result(body):
    for message in body.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
    return False


def _anthropic_sse(events):
    return "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()


def _anthropic_tool(name, args):
    events = [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
         "model": "stub", "content": [], "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return _anthropic_sse(events)


def _anthropic_final(text):
    events = [
        {"type": "message_start", "message": {"id": "msg_2", "type": "message", "role": "assistant",
         "model": "stub", "content": [], "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return _anthropic_sse(events)


def _output_file(raw):
    """Absolute `<cwd>/.xpp-out/<execution_id>/result.txt` from a request body.

    The prompt text around the substituted directory is not part of the path.
    Walk back only through the path characters, then stop at the execution id.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    # bind_output_dir substitutes a native path: on Windows the JSON body carries
    # doubled backslashes. Normalize to slashes so one parser handles both —
    # a real model reads a Windows path fine; this stub must too.
    text = text.replace("\\\\", "/").replace("\\", "/")
    marker = ".xpp-out/"
    start = text.find(marker)
    if start < 0:
        return "result.txt"
    begin = start
    while begin > 0 and text[begin - 1] not in " \"'\n\t":
        begin -= 1
    end = start + len(marker)
    while end < len(text) and text[end] not in "/ \"'\\\n,]}":
        end += 1
    directory = text[begin:end]
    absolute = directory.startswith("/") or (directory[1:3] == ":/" and directory[:1].isalpha())
    if not absolute:
        return "result.txt"
    return directory.rstrip("/") + "/result.txt"


class _Server:
    def __init__(self, mode, outside="", tool="read", wire="openai"):
        _Stub.mode = mode
        _Stub.outside = outside
        _Stub.tool = tool
        _Stub.wire = wire
        _Stub.seen = []
        _Stub.hits = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def url_for(self, api):
        # Anthropic's client appends /v1/messages to a root base URL.
        if api == "anthropic-messages":
            return "http://127.0.0.1:%d" % self.port
        return self.url

    def close(self):
        self.httpd.shutdown()


class PiExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Registered first so it runs LAST: tearDown runs before doCleanups, and
        # the vault/secrets.db handle closed via addCleanup must finish before
        # Windows may delete its open database file.
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.servers = []
        self._env = {key: os.environ.get(key) for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
            "NODE_TLS_REJECT_UNAUTHORIZED", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")}
        for key in self._env:
            os.environ.pop(key, None)

    def tearDown(self):
        for server in self.servers:
            server.close()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _server(self, mode, outside="", tool="read", wire="openai"):
        server = _Server(mode, outside, tool=tool, wire=wire)
        self.servers.append(server)
        return server

    def _pi_env(self, url, model="stub-model", api="openai-completions"):
        extra = materialize_executor_env(ADAPTER_CATALOG["pi"], self.data)
        extra[_PI_BASE_URL_ENV] = url
        write_pi_provider_config(extra["PI_CODING_AGENT_DIR"], url, "OPENAI_API_KEY", model, api)
        return extra

    def _supervise(self, prompt, extra, *, mode_cwd=None, required=None, path=None, argv=None, pins=None):
        cwd = mode_cwd or self.workspace
        binary = str(bundled_pi_path())
        use_argv = argv or [binary, *ADAPTER_CATALOG["pi"]["argv"]]
        installation = {"adapter": "pi", "argv": use_argv}
        spec = {"execution_id": "exec-" + "ab" * 16, "spec_digest": "d", "attempt_uuid": "u",
                "argv": use_argv, "prompt": prompt, "prompt_mode": "stdin", "cwd": str(cwd),
                "allowed_roots": [str(cwd)], "inputs": [], "control_roots": [str(self.data)],
                "outputs": [], "required_outputs": required or [], "timeout_s": 90,
                "env_allowlist": [], "extra_env": extra, "secret_refs": {"OPENAI_API_KEY": "pi-key"},
                "vault_path": str(self.data / "vault"),
                "pinned_digests": pinned_digests(installation) if pins is None else pins,
                "allow_pgroup_cooperative": True, "supervision_floor": "posix-pgroup-cooperative"}
        from pangpang.vault import Vault
        vault = Vault(self.data / "vault")
        vault.set("pi-key", "test-key")
        vault.close()
        directory = self.data / ("run-" + mode_label(prompt))
        directory.mkdir()
        (directory / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        saved = os.environ.get("PATH")
        if path is not None:
            os.environ["PATH"] = path
        try:
            runner.supervise(directory)
        finally:
            if path is not None:
                if saved is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = saved
        return json.loads((directory / "receipt.json").read_text(encoding="utf-8"))

    def test_pin_is_exactly_1_0_3_and_guard_and_license_match(self):
        self.assertEqual(PI_VERSION, "1.0.3")
        self.assertEqual(hashlib.sha256(PI_GUARD_PATH.read_bytes()).hexdigest(), PI_GUARD_SHA256)
        self.assertEqual(hashlib.sha256(PI_LICENSE.read_bytes()).hexdigest(), PI_LICENSE_SHA256)
        self.assertEqual(ADAPTER_CATALOG["pi"]["argv"], PI_ARGV)
        self.assertNotIn("{prompt}", ADAPTER_CATALOG["pi"]["argv"])
        self.assertEqual(ADAPTER_CATALOG["pi"]["priority"], 40)
        self.assertLess(ADAPTER_CATALOG["codex"]["priority"], 40)
        self.assertEqual(ADAPTER_CATALOG["pi"]["sha256"], _pi_binary_sha())
        self.assertEqual(ADAPTER_CATALOG["pi"]["secret_env"], "OPENAI_API_KEY")

    def test_pinned_pi_registers_below_user_clis(self):
        cfg = empty_config()
        cfg["hosts"] = [{"id": "host-local", "name": "本机", "transport": "local", "os": "Linux",
                         "allowed_roots": [str(self.workspace)]}]
        app = Service(cfg, self.data / "svc")
        self.addCleanup(app.store.close)
        result = app.ensure_local_host(str(self.workspace), discover=True)
        self.assertIn("inst-local-pi", result["installations"])
        inst = app.execution.installations["inst-local-pi"]
        self.assertEqual(inst["argv"], [str(bundled_pi_path()), *PI_ARGV])
        self.assertEqual(app._route_priority(inst), 40)
        self.assertEqual(inst["extra_env"]["PI_TELEMETRY"], "0")
        self.assertEqual(inst["extra_env"]["PI_SKIP_VERSION_CHECK"], "1")
        # macOS tempfile lives under /var, which is a symlink to /private/var.
        # Service resolves the data dir; compare both sides after resolve.
        agent_dir = str(Path(inst["extra_env"]["PI_CODING_AGENT_DIR"]).resolve())
        self.assertTrue(agent_dir.startswith(str(self.data.resolve())))
        self.assertNotIn(str(self.workspace.resolve()), agent_dir)
        self.assertFalse(binary_matches_pin(ADAPTER_CATALOG["pi"], str(PI_GUARD_PATH)))

    def test_digest_mismatch_refuses_install_and_launch(self):
        bad = self.root / "not-pi"
        bad.write_bytes(b"not the official binary")
        bad.chmod(0o755)
        cfg = empty_config()
        cfg["hosts"] = [{"id": "host-local", "name": "本机", "transport": "local", "os": "Linux",
                         "allowed_roots": [str(self.workspace)]}]
        app = Service(cfg, self.data / "svc-bad")
        self.addCleanup(app.store.close)

        def which(binary):
            return str(bad) if binary == ADAPTER_CATALOG["pi"]["binary"] else None

        with patch("pangpang.service.shutil.which", side_effect=which):
            result = app.ensure_local_host(str(self.workspace), discover=True)
        self.assertNotIn("inst-local-pi", result.get("installations") or [])
        self.assertNotIn("inst-local-pi", app.execution.installations)
        receipt = self._supervise("noop", {}, argv=[str(bad), "--version"],
                                  pins=[{"path": str(bad), "sha256": _pi_binary_sha()}])
        self.assertEqual(receipt.get("not_launched"), "runner_prelaunch")
        self.assertIn("binary_digest_mismatch", receipt.get("error", ""))
        self.assertEqual(receipt["status"], "failed")

    def test_model_cannot_alter_argv(self):
        cfg = config(self.workspace)
        cfg["installations"].append({
            "id": "inst-local-pi", "host_id": "local", "adapter": "pi",
            "argv": [str(bundled_pi_path()), *PI_ARGV], "cwd": str(self.workspace),
            "risk": "write", "probe_argv": [str(bundled_pi_path()), "--version"]})
        app = Service(cfg, self.data / "svc-argv")
        self.addCleanup(app.store.close)
        with self.assertRaises(Fault) as raised:
            app.configure_installation("inst-local-pi", {"argv": ["rm", "-rf", "/"]})
        self.assertEqual(raised.exception.code, "unsupported_installation_field")
        self.assertEqual(app.execution.installations["inst-local-pi"]["argv"],
                         [str(bundled_pi_path()), *PI_ARGV])

    def test_configure_writes_models_json_and_presets(self):
        from types import SimpleNamespace
        from pangpang.store import Store
        holder_data = self.data / "device"
        holder_data.mkdir()
        store = Store(holder_data / "connector.db")
        self.addCleanup(store.close)
        live = {"host_id": "local",
                "hosts": [{"id": "local", "transport": "local", "os": "Linux",
                           "allowed_roots": [str(self.workspace)]}],
                "installations": [], "resources": []}
        from pangpang.execution import Execution
        holder = SimpleNamespace(store=store, data=holder_data, config=live, config_revision=0,
                                 execution=Execution(live, holder_data), host_id="local")
        setup = LocalSetup(holder)
        server = self._server("done")
        info = setup.configure({"adapter": "pi", "endpoint": server.url, "auth": "api_key",
                                "model": "configured-model", "api": "openai-completions"})
        inst = holder.execution.installations["pi-local"]
        self.assertEqual(inst["argv"], [str(bundled_pi_path()), *PI_ARGV])
        self.assertEqual(set(inst["secret_refs"]), {"OPENAI_API_KEY"})
        self.assertEqual(inst["extra_env"]["HTTP_PROXY"], "")
        self.assertEqual(inst["extra_env"]["http_proxy"], "")
        self.assertEqual(inst["extra_env"]["https_proxy"], "")
        self.assertEqual(inst["extra_env"]["all_proxy"], "")
        self.assertEqual(inst["extra_env"]["no_proxy"], "")
        self.assertEqual(inst["extra_env"][_PI_BASE_URL_ENV], server.url)
        models = json.loads((Path(inst["extra_env"]["PI_CODING_AGENT_DIR"]) / "models.json").read_text())
        provider = models["providers"]["pangpang"]
        self.assertEqual(provider["baseUrl"], server.url)
        self.assertEqual(provider["api"], "openai-completions")
        self.assertEqual(provider["apiKey"], "$OPENAI_API_KEY")
        self.assertEqual(provider["models"][0]["id"], "configured-model")
        settings = json.loads((Path(inst["extra_env"]["PI_CODING_AGENT_DIR"]) / "settings.json").read_text())
        self.assertEqual(settings["defaultModel"], "configured-model")
        self.assertNotIn(str(self.workspace), inst["extra_env"]["PI_PACKAGE_DIR"])
        self.assertTrue(info["installation"]["adapter"] == "pi" or inst["adapter"] == "pi")

    def _check_outside_cwd_cases(self, work, cases, secret=None):
        for mode, outside_path, forbidden in cases:
            server = self._server(mode, outside_path)
            extra = self._pi_env(server.url)
            receipt = self._supervise(mode, extra, mode_cwd=work)
            self.assertEqual(receipt["status"], "succeeded", (mode, receipt.get("error"), receipt.get("exit_code")))
            for path in forbidden:
                if path == secret:
                    # The file is the pre-existing secret. A blocked read leaves it untouched.
                    self.assertEqual(secret.read_text(), "SECRET-DATA\\n")
                else:
                    self.assertFalse(path.exists(), mode)
            blob = json.dumps(_Stub.seen)
            self.assertIn("path_outside_cwd", blob, mode)
            self.assertNotIn("SECRET-DATA", blob)

    def test_blocks_paths_outside_cwd(self):
        # Always run the absolute and parent-traversal cases, including on Windows
        # hosts without SeCreateSymbolicLinkPrivilege.
        parent = self.root / "parent"
        parent.mkdir()
        work = parent / "work"
        work.mkdir()
        secret = parent / "secret.txt"
        secret.write_text("SECRET-DATA\\n")
        outside = self.root / "outside"
        outside.mkdir()
        self._check_outside_cwd_cases(work, (
            ("abs", str(outside / "abs.txt"), [outside / "abs.txt"]),
            ("dotdot", "", [secret]),
        ), secret=secret)

    def test_symlink_paths_outside_cwd(self):
        # A separate test so that unsupported Windows symlink creation is an
        # EXPLICIT unittest SKIP, never a silent omission in a passing test.
        outside = self.root / "outside-links"
        outside.mkdir()
        nested = outside / "nested"
        nested.mkdir()
        try:
            (self.workspace / "escape").symlink_to(nested, target_is_directory=True)
            (self.workspace / "dangling").symlink_to(outside / "missing", target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable (WinError 1314)")
            raise
        self._check_outside_cwd_cases(self.workspace, (
            ("dirlink", "", [nested / "pwn.txt"]),
            ("dangling", "", [outside / "missing"]),
            ("link-dotdot", "", [outside / "escaped.txt", nested / "escaped.txt"]),
        ))

    def test_inside_write_satisfies_required_output_and_task_succeeds(self):
        server = self._server("deliver")
        extra = self._pi_env(server.url)
        cfg = config(self.workspace)
        # inst-local-pi is the profile-derived installation: launch reads this profile.
        cfg["models"][0]["endpoint"] = server.url
        cfg["models"][0]["credential_ref"] = "pi-key"
        cfg["installations"] = [{
            "id": "inst-local-pi", "host_id": "local", "adapter": "pi",
            "argv": [str(bundled_pi_path()), *PI_ARGV], "cwd": str(self.workspace),
            "risk": "write", "prompt_mode": "stdin", "enabled": True,
            "probe_argv": [str(bundled_pi_path()), "--version"],
            "extra_env": extra, "secret_refs": {"OPENAI_API_KEY": "pi-key"},
            "max_concurrency": 1}]
        app = Service(cfg, self.data / "svc-task")
        self.addCleanup(app.store.close)
        app.vault.set("pi-key", "test-key")
        app.probe("inst-local-pi")
        message = app.send("bot-main", {"text": "write the file", "allow_write": True}, "pi-task")
        submitted = app.submit_task("bot-main", message["work_id"], {
            "prompt": "把 ok 写入 $XPP_OUTPUT_DIR/result.txt",
            "required_outputs": ["result.txt"], "installation_id": "inst-local-pi"})
        task = app.store.get("task", submitted["task_id"])
        deadline = time.time() + 90
        while time.time() < deadline:
            app.task_step()
            task = app.store.get("task", task["id"])
            if task["status"] in ("succeeded", "failed", "cancelled", "unknown"):
                break
            time.sleep(0.05)
        self.assertEqual(task["status"], "succeeded", task.get("receipt") or task.get("error"))
        receipt = task["receipt"]
        self.assertEqual([item["present"] for item in receipt["required_output_results"]], [True])

    def test_workspace_dotenv_cannot_redirect_proxy_or_config_dir(self):
        server = self._server("done")
        extra = self._pi_env(server.url)
        isolated = Path(extra["PI_CODING_AGENT_DIR"])
        before = (isolated / "models.json").read_bytes()
        evil = self.root / "evil"
        (self.workspace / ".env").write_text(
            "\n".join(["HTTP_PROXY=http://127.0.0.1:1", "HTTPS_PROXY=http://127.0.0.1:1",
                       "ALL_PROXY=http://127.0.0.1:1", "NO_PROXY=",
                       "http_proxy=http://127.0.0.1:1", "https_proxy=http://127.0.0.1:1",
                       "NODE_TLS_REJECT_UNAUTHORIZED=0",
                       "PI_CODING_AGENT_DIR=" + str(evil / "config"),
                       "PI_PACKAGE_DIR=" + str(evil / "package"),
                       "PI_TELEMETRY=1", "OPENAI_API_KEY=wrong-key"]) + "\n",
            encoding="utf-8")
        receipt = self._supervise("say ok", extra)
        self.assertEqual(receipt["status"], "succeeded", receipt.get("error"))
        self.assertGreaterEqual(_Stub.hits, 1)
        self.assertIn("test-key", _Stub.seen[0]["auth"])
        self.assertNotIn("wrong-key", _Stub.seen[0]["auth"])
        self.assertEqual((isolated / "models.json").read_bytes(), before)
        self.assertFalse(evil.exists())

    def test_no_rg_or_fd_still_completes_without_download(self):
        server = self._server("grep-then-write")
        extra = self._pi_env(server.url)
        empty = self.root / "empty-path"
        empty.mkdir()
        receipt = self._supervise("search then write $XPP_OUTPUT_DIR/result.txt", extra,
                                  required=["result.txt"], path=str(empty))
        self.assertEqual(receipt["status"], "succeeded", receipt.get("error"))
        self.assertEqual([item["present"] for item in receipt["required_output_results"]], [True])
        isolated = Path(extra["PI_CODING_AGENT_DIR"])
        names = {path.name for path in isolated.rglob("*") if path.is_file()}
        self.assertFalse(names & {"rg", "fd", "rg.exe", "fd.exe"})
        self.assertGreaterEqual(_Stub.hits, 1)

    def test_guard_blocks_normalize_path_escapes(self):
        """Paths pi 1.0.3 rewrites before access must be judged after that rewrite."""
        home = self.root / "home"
        home.mkdir()
        work = self.root / "work"
        work.mkdir()
        (home / "secret.txt").write_text("SECRET-DATA\n")
        (self.root / "secret.txt").write_text("SECRET-DATA\n")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("SECRET-DATA\n")
        abs_write = outside / "x"
        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", previous) if previous is not None else os.environ.pop("HOME", None))
        cases = (
            ("read", "~/secret.txt", home / "secret.txt", False),
            ("read", "@../secret.txt", self.root / "secret.txt", False),
            ("write", "@" + str(abs_write), abs_write, True),
            ("read", (outside / "secret.txt").as_uri(), outside / "secret.txt", False),
            ("read", "@~/secret.txt", home / "secret.txt", False),
)
        for tool, raw_path, target, is_write in cases:
            server = self._server("given", raw_path, tool=tool)
            extra = self._pi_env(server.url)
            receipt = self._supervise(raw_path, extra, mode_cwd=work)
            self.assertEqual(receipt["status"], "succeeded", (raw_path, receipt.get("error"), receipt.get("exit_code")))
            blob = json.dumps(_Stub.seen)
            self.assertIn("path_outside_cwd", blob, raw_path)
            self.assertNotIn("SECRET-DATA", blob, raw_path)
            if is_write:
                self.assertFalse(target.exists(), raw_path)
            else:
                self.assertEqual(target.read_text(), "SECRET-DATA\n", raw_path)

    def test_tilde_inside_cwd_is_allowed(self):
        home = self.root / "home"
        work = home / "work"
        work.mkdir(parents=True)
        # The guard resolves `~` with Node's os.homedir(): HOME on POSIX,
        # USERPROFILE on Windows. Point both at the fake home or the write
        # lands outside cwd on a Windows runner.
        previous = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        def restore():
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        server = self._server("given", "~/work/ok.txt", tool="write")
        extra = self._pi_env(server.url)
        receipt = self._supervise("write inside", extra, mode_cwd=work)
        self.assertEqual(receipt["status"], "succeeded", receipt.get("error"))
        self.assertEqual((work / "ok.txt").read_text(), "pwn\n")
        self.assertNotIn("path_outside_cwd", json.dumps(_Stub.seen))

    def _profile(self, endpoint, model, api, ref="bot-key", credential_env=None):
        cfg = empty_config()
        cfg["models"][0].update(model=model, endpoint=endpoint, api=api, provider="custom")
        if credential_env:
            cfg["models"][0]["credential_env"] = credential_env
        elif ref:
            cfg["models"][0]["credential_ref"] = ref
        cfg["hosts"] = [{"id": "host-local", "name": "本机", "transport": "local",
                         "os": platform.system(), "allowed_roots": [str(self.workspace)]}]
        return cfg

    def _discover_pi(self, app):
        """Pinned binary hidden until fetch_pinned_pi runs, same condition as discover()."""
        binary = str(bundled_pi_path())
        state = {"fetched": False}

        def which(name):
            if state["fetched"] and name == binary:
                return binary
            return None

        def fetch():
            state["fetched"] = True
            return bundled_pi_path()

        with patch("pangpang.service.shutil.which", side_effect=which), \
             patch("pangpang.service.fetch_pinned_pi", side_effect=fetch):
            result = app.ensure_local_host(str(self.workspace), discover=True)
        self.assertTrue(state["fetched"])
        return result

    def test_models_json_api_and_model_id_follow_profile(self):
        for api, model in (
            ("openai-completions", "chat-model"),
            ("openai-responses", "responses-model"),
            ("anthropic-messages", "claude-model"),
            ("google-generative-ai", "gemini-model"),
):
            with self.subTest(api=api):
                data = self.root / ("svc-" + api)
                app = Service(self._profile("http://127.0.0.1:9/v1", model, api), data)
                self.addCleanup(app.store.close)
                app.vault.set("bot-key", "vault-secret-value")
                result = self._discover_pi(app)
                self.assertIn("inst-local-pi", result["installations"])
                inst = app.execution.installations["inst-local-pi"]
                # Registration does not snapshot the profile. Launch does.
                self.assertNotIn("secret_refs", inst)
                self.assertTrue(app._bind_profile_pi(inst))
                self.assertEqual(inst["secret_refs"], {"OPENAI_API_KEY": "bot-key"})
                directory = Path(inst["extra_env"]["PI_CODING_AGENT_DIR"])
                text = (directory / "models.json").read_text(encoding="utf-8")
                self.assertNotIn("vault-secret-value", text)
                provider = json.loads(text)["providers"]["pangpang"]
                self.assertEqual(provider["api"], api)
                self.assertEqual(provider["apiKey"], "$OPENAI_API_KEY")
                self.assertEqual(provider["models"][0]["id"], model)
                settings = json.loads((directory / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["defaultModel"], model)

    def test_pi_models_api_is_none_for_an_unknown_api(self):
        # validate_profile (model_config.py) rejects any api outside the four
        # _PI_API keys, so this is not a route() case. The mapper still fails closed.
        self.assertIsNone(pi_models_api({"api": "not-a-pi-1.0.3-api"}))
        self.assertIsNone(pi_models_api(None))
        self.assertEqual(pi_models_api({"model": "x"}), "openai-completions")

    def _task_against_stub(self, api, model):
        wire = "anthropic" if api == "anthropic-messages" else "openai"
        server = self._server("deliver", wire=wire)
        endpoint = server.url_for(api)
        app = Service(self._profile(endpoint, model, api), self.root / ("task-" + api))
        self.addCleanup(app.store.close)
        app.vault.set("bot-key", "vault-secret-value")
        result = self._discover_pi(app)
        self.assertIn("inst-local-pi", result["installations"])
        app.probe("inst-local-pi")
        message = app.send("bot-main", {"text": "write the file", "allow_write": True}, "pi-" + api)
        submitted = app.submit_task("bot-main", message["work_id"], {
            "prompt": "把 ok 写入 $XPP_OUTPUT_DIR/result.txt",
            "required_outputs": ["result.txt"], "installation_id": "inst-local-pi"})
        task = app.store.get("task", submitted["task_id"])
        if task.get("installation_authorization") == "required":
            app.approve(task["id"], {"spec_digest": task["spec_digest"]})
            task = app.store.get("task", task["id"])
        deadline = time.time() + 90
        while time.time() < deadline:
            app.task_step()
            task = app.store.get("task", task["id"])
            if task["status"] in ("succeeded", "failed", "cancelled", "unknown"):
                break
            time.sleep(0.05)
        self.assertEqual(task["status"], "succeeded", task.get("receipt") or task.get("error"))
        self.assertEqual([item["present"] for item in task["receipt"]["required_output_results"]], [True])
        self.assertGreaterEqual(_Stub.hits, 1)
        first = json.loads(_Stub.seen[0]["body"])
        self.assertEqual(first["model"], model)
        path = _Stub.seen[0]["path"].split("?", 1)[0]
        if api == "anthropic-messages":
            self.assertEqual(path, "/v1/messages")
            self.assertEqual(_Stub.seen[0]["x-api-key"], "vault-secret-value")
        else:
            self.assertEqual(path, "/v1/chat/completions")
            self.assertIn("vault-secret-value", _Stub.seen[0]["auth"])
        text = (Path(app.execution.installations["inst-local-pi"]["extra_env"]["PI_CODING_AGENT_DIR"])
                / "models.json").read_text(encoding="utf-8")
        self.assertNotIn("vault-secret-value", text)

    def test_default_host_chat_completions_task_uses_profile_model(self):
        self._task_against_stub("openai-completions", "chat-model")

    def test_default_host_anthropic_task_uses_profile_model(self):
        self._task_against_stub("anthropic-messages", "claude-model")

    def _pi_config_blobs(self, app):
        inst = app.execution.installations["inst-local-pi"]
        directory = Path(inst["extra_env"]["PI_CODING_AGENT_DIR"])
        managed = app.store.get("settings", "managed")["config"]
        return {
            "models.json": (directory / "models.json").read_text(encoding="utf-8"),
            "settings.json": (directory / "settings.json").read_text(encoding="utf-8"),
            "installation": json.dumps(inst),
            "config": json.dumps(app.config),
            "managed": json.dumps(managed),
        }

    def _assert_absent(self, blobs, *secrets):
        for label, blob in blobs.items():
            for secret in secrets:
                self.assertNotIn(secret, blob, label)

    def test_no_auth_profile_uses_bot_placeholder_not_workspace_dotenv(self):
        """A Web 'no key' profile must reach pi. The placeholder is pre-set so a
        workspace.env cannot supply the key, and a stale secret_refs is dropped."""
        planted = "dotenv-planted-key"
        stale = "stale-plaintext-key"
        server = self._server("deliver")
        app = Service(self._profile(server.url, "no-auth-model", "openai-completions", ref=None),
                      self.root / "task-no-auth")
        self.addCleanup(app.store.close)
        result = self._discover_pi(app)
        self.assertIn("inst-local-pi", result["installations"])
        app.vault.set("stale-ref", stale)
        app.execution.installations["inst-local-pi"]["secret_refs"] = {"OPENAI_API_KEY": "stale-ref"}
        (self.workspace / ".env").write_text("OPENAI_API_KEY=" + planted + "\n", encoding="utf-8")
        app.probe("inst-local-pi")
        task = self._run_task(app, "pi-no-auth")
        self.assertEqual(task["status"], "succeeded", task.get("receipt") or task.get("error"))
        self.assertGreaterEqual(_Stub.hits, 1)
        self.assertEqual(json.loads(_Stub.seen[0]["body"])["model"], "no-auth-model")
        self.assertIn("pangpang-no-auth", _Stub.seen[0]["auth"])
        self.assertNotIn(planted, _Stub.seen[0]["auth"])
        self.assertNotIn(stale, _Stub.seen[0]["auth"])
        candidate, base = app.managed_snapshot()
        app.commit_managed_config(candidate, base_revision=base)
        inst = app.execution.installations["inst-local-pi"]
        self.assertNotIn("secret_refs", inst)
        self.assertEqual(inst["extra_env"]["OPENAI_API_KEY"], "pangpang-no-auth")
        blobs = self._pi_config_blobs(app)
        provider = json.loads(blobs["models.json"])["providers"]["pangpang"]
        self.assertEqual(provider["apiKey"], "$OPENAI_API_KEY")
        self.assertNotIn("pangpang-no-auth", blobs["models.json"])
        self.assertNotIn("pangpang-no-auth", blobs["settings.json"])
        self._assert_absent(blobs, planted, stale)

    def test_credential_env_profile_passes_the_name_not_the_value(self):
        """credential_env is the coordinator variable name. The value is never
        copied into extra_env, models.json, or the installation that commit persists."""
        name = "PANGPANG_PI_TEST_KEY"
        value = "pi-credential-env-fixture"
        planted = "dotenv-planted-key"
        previous = os.environ.pop(name, None)

        def restore(previous=previous):
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

        self.addCleanup(restore)
        server = self._server("deliver")
        app = Service(self._profile(server.url, "env-model", "openai-completions",
                                    ref=None, credential_env=name),
                      self.root / "task-credential-env")
        self.addCleanup(app.store.close)
        result = self._discover_pi(app)
        self.assertIn("inst-local-pi", result["installations"])
        app.vault.set("stale-ref", "stale-plaintext-key")
        app.execution.installations["inst-local-pi"]["secret_refs"] = {"OPENAI_API_KEY": "stale-ref"}
        (self.workspace / ".env").write_text(
            "OPENAI_API_KEY=" + planted + "\n" + name + "=" + planted + "\n", encoding="utf-8")
        app.probe("inst-local-pi")
        _Stub.seen, _Stub.hits = [], 0
        denied = self._run_task(app, "pi-env-missing")
        self.assertEqual(denied["status"], "failed", denied.get("error"))
        self.assertEqual(denied.get("error"), "model_not_configured")
        self.assertEqual(_Stub.hits, 0)

        os.environ[name] = value
        _Stub.seen, _Stub.hits = [], 0
        task = self._run_task(app, "pi-env-set")
        self.assertEqual(task["status"], "succeeded", task.get("receipt") or task.get("error"))
        self.assertGreaterEqual(_Stub.hits, 1)
        self.assertEqual(json.loads(_Stub.seen[0]["body"])["model"], "env-model")
        self.assertIn(value, _Stub.seen[0]["auth"])
        self.assertNotIn(planted, _Stub.seen[0]["auth"])
        candidate, base = app.managed_snapshot()
        app.commit_managed_config(candidate, base_revision=base)
        inst = app.execution.installations["inst-local-pi"]
        self.assertNotIn("secret_refs", inst)
        self.assertIn(name, inst.get("env_allowlist") or [])
        self.assertNotIn(value, json.dumps(inst.get("extra_env") or {}))
        blobs = self._pi_config_blobs(app)
        provider = json.loads(blobs["models.json"])["providers"]["pangpang"]
        self.assertEqual(provider["apiKey"], "$" + name)
        self._assert_absent(blobs, value, planted, "stale-plaintext-key")

    def test_bind_replaces_stale_credential_env_on_the_allowlist(self):
        name = "PANGPANG_PI_STALE_ENV"
        os.environ[name] = "present-for-bind"
        self.addCleanup(lambda: os.environ.pop(name, None))
        app = Service(self._profile("http://127.0.0.1:9/v1", "m", "openai-completions",
                                    ref=None, credential_env=name),
                      self.root / "bind-allow")
        self.addCleanup(app.store.close)
        inst = {"id": "inst-local-pi", "adapter": "pi",
                "extra_env": {"PI_CODING_AGENT_DIR": str(self.root / "pi-allow")},
                "env_allowlist": ["UNRELATED_SHOULD_BE_REPLACED", name]}
        self.assertTrue(app._bind_profile_pi(inst))
        self.assertEqual(inst["env_allowlist"], [name])

        profile = app.config["models"][0]
        profile.pop("credential_env", None)
        profile["credential_ref"] = "bot-key"
        self.assertTrue(app._bind_profile_pi(inst))
        self.assertEqual(inst["env_allowlist"], [])
        self.assertNotIn(name, inst["env_allowlist"])

        profile.pop("credential_ref", None)
        profile["credential_env"] = name
        self.assertTrue(app._bind_profile_pi(inst))
        self.assertEqual(inst["env_allowlist"], [name])
        profile.pop("credential_env", None)
        self.assertTrue(app._bind_profile_pi(inst))
        self.assertEqual(inst["env_allowlist"], [])
        self.assertNotIn(name, inst["env_allowlist"])

    def _save_model(self, app, endpoint, model, secret=None):
        """Onboarding.model is the Web UI save. The probe is the Node worker; stub it."""
        from pangpang.pi_runtime import PiModel
        body = {"endpoint": endpoint, "model": model, "api": "openai-completions"}
        if secret:
            body["secret"] = secret
        calls = {"n": 0}

        def complete(_model, _profile, _messages, _tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return ({"role": "assistant", "tool_calls": [{
                    "id": "c", "type": "function",
                    "function": {"name": "setup_probe",
                                 "arguments": json.dumps({"value": "pangpang-ready"})}}]}, {})
            return ({"role": "assistant", "content": "ready"}, {})

        with patch.object(PiModel, "complete", complete):
            app.onboarding.model(body)

    def _run_task(self, app, key):
        message = app.send("bot-main", {"text": "write the file", "allow_write": True}, key)
        submitted = app.submit_task("bot-main", message["work_id"], {
            "prompt": "把 ok 写入 $XPP_OUTPUT_DIR/result.txt",
            "required_outputs": ["result.txt"], "installation_id": "inst-local-pi"})
        task = app.store.get("task", submitted["task_id"])
        if task.get("installation_authorization") == "required":
            app.approve(task["id"], {"spec_digest": task["spec_digest"]})
            task = app.store.get("task", task["id"])
        deadline = time.time() + 90
        while time.time() < deadline:
            app.task_step()
            task = app.store.get("task", task["id"])
            if task["status"] in ("succeeded", "failed", "cancelled", "unknown"):
                break
            time.sleep(0.05)
        return task

    def test_model_saved_after_start_and_later_change_reach_pi(self):
        """Fresh data dir registers pi with an empty profile. The Web save, and a
        later model rename, are what the next launch sends."""
        server = self._server("deliver")
        cfg = empty_config()
        cfg["hosts"] = [{"id": "host-local", "name": "本机", "transport": "local",
                         "os": platform.system(), "allowed_roots": [str(self.workspace)]}]
        app = Service(cfg, self.root / "svc-late-model")
        self.addCleanup(app.store.close)
        result = self._discover_pi(app)
        self.assertIn("inst-local-pi", result["installations"])
        inst = app.execution.installations["inst-local-pi"]
        self.assertFalse((Path(inst["extra_env"]["PI_CODING_AGENT_DIR"]) / "models.json").exists())
        _Stub.seen, _Stub.hits = [], 0
        early = self._run_task(app, "pi-before-model")
        self.assertEqual(early["status"], "failed", early.get("error"))
        self.assertEqual(early.get("error"), "model_not_configured")
        self.assertEqual(_Stub.hits, 0)

        self._save_model(app, server.url, "first-model", secret="vault-secret-value")
        _Stub.seen, _Stub.hits = [], 0
        first = self._run_task(app, "pi-first-model")
        self.assertEqual(first["status"], "succeeded", first.get("receipt") or first.get("error"))
        self.assertEqual(json.loads(_Stub.seen[0]["body"])["model"], "first-model")
        self.assertEqual(_Stub.seen[0]["path"].split("?", 1)[0], "/v1/chat/completions")
        self.assertIn("vault-secret-value", _Stub.seen[0]["auth"])

        self._save_model(app, server.url, "second-model")
        _Stub.seen, _Stub.hits = [], 0
        second = self._run_task(app, "pi-second-model")
        self.assertEqual(second["status"], "succeeded", second.get("receipt") or second.get("error"))
        self.assertEqual(json.loads(_Stub.seen[0]["body"])["model"], "second-model")
        self.assertIn("vault-secret-value", _Stub.seen[0]["auth"])
        live = app.execution.installations["inst-local-pi"]
        text = (Path(live["extra_env"]["PI_CODING_AGENT_DIR"]) / "models.json").read_text(encoding="utf-8")
        self.assertNotIn("vault-secret-value", text)
        self.assertEqual(json.loads(text)["providers"]["pangpang"]["models"][0]["id"], "second-model")


class _BytesResponse:
    def __init__(self, data):
        self._data = data

    def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._data = self._data, b""
            return chunk
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _member(bundle, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    bundle.addfile(info, io.BytesIO(data))


def _tar_bytes(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as bundle:
        for name, data in members:
            _member(bundle, name, data)
    return buf.getvalue()


def _zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as bundle:
        for name, data in members:
            bundle.writestr(name, data)
    return buf.getvalue()


class PiFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Registered first so it runs LAST: tearDown runs before doCleanups, and
        # the store closed via addCleanup must finish before Windows may delete
        # its open connector.db.
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dest = self.root / "pi" / "pi"

    def _fetch(self, blob, archive_sha, binary_sha, archive_name="pi-linux-x64.tar.gz"):
        from pangpang import execution
        release = {(platform.system(), platform.machine()): (archive_name, archive_sha, binary_sha)}
        with patch.object(execution, "bundled_pi_path", return_value=self.dest), \
             patch.dict(execution.PI_RELEASES, release), \
             patch.object(execution.urllib.request, "urlopen", return_value=_BytesResponse(blob)):
            return execution.fetch_pinned_pi()

    def test_archive_sha_mismatch_leaves_no_file(self):
        blob = _tar_bytes((("pi/pi", b"binary-bytes"), ("pi/photon_rs_bg.wasm", b"wasm-bytes")))
        found = self._fetch(blob, "0" * 64, hashlib.sha256(b"binary-bytes").hexdigest())
        self.assertIsNone(found)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name(self.dest.name + ".partial").exists())
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_binary_sha_mismatch_leaves_no_file(self):
        blob = _tar_bytes((("pi/pi", b"binary-bytes"), ("pi/photon_rs_bg.wasm", b"wasm-bytes")))
        found = self._fetch(blob, hashlib.sha256(blob).hexdigest(), "0" * 64)
        self.assertIsNone(found)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name(self.dest.name + ".partial").exists())
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_fetch_writes_only_the_binary(self):
        raw = b"binary-bytes"
        blob = _tar_bytes((("pi/pi", raw), ("pi/photon_rs_bg.wasm", b"wasm-bytes")))
        found = self._fetch(blob, hashlib.sha256(blob).hexdigest(), hashlib.sha256(raw).hexdigest())
        self.assertEqual(found, self.dest)
        self.assertEqual(self.dest.read_bytes(), raw)
        if os.name != "nt":  # Windows has no exec bit; ACLs govern
            self.assertTrue(self.dest.stat().st_mode & 0o111)
        names = {path.name for path in self.root.rglob("*")}
        self.assertNotIn("photon_rs_bg.wasm", names)
        zip_blob = _zip_bytes((("pi.exe", raw), ("photon_rs_bg.wasm", b"wasm-bytes")))
        self.dest.unlink()
        found = self._fetch(zip_blob, hashlib.sha256(zip_blob).hexdigest(),
                            hashlib.sha256(raw).hexdigest(), archive_name="pi-windows-x64.zip")
        self.assertEqual(self.dest.read_bytes(), raw)

    def _setup(self):
        from types import SimpleNamespace
        from pangpang.execution import Execution
        from pangpang.store import Store
        data = self.root / "device"
        data.mkdir()
        store = Store(data / "connector.db")
        self.addCleanup(store.close)
        workspace = self.root / "workspace"
        workspace.mkdir()
        live = {"host_id": "local",
                "hosts": [{"id": "local", "transport": "local", "os": "Linux",
                           "allowed_roots": [str(workspace)]}],
                "installations": [], "resources": []}
        holder = SimpleNamespace(store=store, data=data, config=live, config_revision=0,
                                 execution=Execution(live, data), host_id="local")
        return LocalSetup(holder)

    def test_other_executor_present_means_no_fetch(self):
        fake = self.root / "claude"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        setup = self._setup()

        def which(binary):
            return str(fake) if binary == "claude" else None

        with patch("pangpang.local_setup.shutil.which", side_effect=which), \
             patch("pangpang.local_setup.fetch_pinned_pi") as fetch:
            inventory = setup.discover()["inventory"]
        fetch.assert_not_called()
        self.assertEqual([item["adapter"] for item in inventory], ["claude-code"])

    def test_no_other_executor_fetches_pinned_pi(self):
        fake = self.root / "fetched-pi"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        setup = self._setup()
        pi_binary = ADAPTER_CATALOG["pi"]["binary"]
        fetched = {"done": False}

        def fetch():
            fetched["done"] = True
            return fake

        def which(binary):
            if fetched["done"] and binary == pi_binary:
                return str(fake)
            return None

        with patch("pangpang.local_setup.shutil.which", side_effect=which), \
             patch("pangpang.local_setup.fetch_pinned_pi", side_effect=fetch), \
             patch("pangpang.local_setup.binary_matches_pin", return_value=True):
            inventory = setup.discover()["inventory"]
        self.assertTrue(fetched["done"])
        self.assertEqual([item["adapter"] for item in inventory], ["pi"])


def mode_label(prompt):
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in prompt)[:40]
    return cleaned or "run"
