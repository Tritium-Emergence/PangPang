""" finding 1: stock claude-code can do the file edit its catalog advertises.

No live Claude binary and no Anthropic key. LocalSetup.configure copies argv
from ADAPTER_CATALOG; a stub executor writes a required output only when that
argv carries acceptEdits plus Read and Write.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pangpang.execution import ADAPTER_CATALOG, Execution
from pangpang.local_setup import LocalSetup
from pangpang.service import Service
from pangpang.store import Store
from test_bot_runtime import config
from test_unknown_adapter_startup import _removed_adapter_name


# The narrow grant. Print mode stays; permission is not skipped.
CLAUDE_CODE_ARGV = [
    "-p", "--permission-mode", "acceptEdits", "--allowed-tools", "Read", "Write",
]
CLAUDE_CODE_USE_WHEN = "Edit code and local files. Not for financial data or image generation."

STUB = r"""
import json, pathlib, re, sys
GRANT = ["-p", "--permission-mode", "acceptEdits", "--allowed-tools", "Read", "Write"]
args = sys.argv[1:]
bypass = "--dangerously-skip-permissions" in args or "bypassPermissions" in args
def has_grant(argv):
    n = len(GRANT)
    return any(argv[i:i + n] == GRANT for i in range(len(argv) - n + 1))
if bypass or not has_grant(args):
    raise SystemExit(0)
# One line is enough: stream-json keeps the pipe open, so a full read would wait
# for EOF that only arrives after this process exits.
line = sys.stdin.readline()
prompt = line
if line.lstrip().startswith("{"):
    try:
        content = (json.loads(line).get("message") or {}).get("content")
        if isinstance(content, str):
            prompt = content
    except Exception:
        prompt = line
match = re.search(r"[^\s（）]*\.xpp-out[^\s（）]*", prompt)
if not match:
    raise SystemExit(0)
pathlib.Path(match.group(0), "final.docx").write_bytes(b"ok")
"""


class ClaudeCodeGrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub-claude.py"
        self.stub.write_text("#!" + sys.executable + "\n" + STUB, encoding="utf-8")
        self.stub.chmod(0o755)
        self._closers = []

    def tearDown(self):
        for close in reversed(self._closers):
            close()
        self.tmp.cleanup()

    def _track(self, obj):
        self._closers.append(obj.close)
        return obj

    def test_catalog_grant_is_claude_code_only(self):
        self.assertEqual(ADAPTER_CATALOG["claude-code"]["argv"], CLAUDE_CODE_ARGV)
        self.assertEqual(ADAPTER_CATALOG["claude-code"]["use_when"], CLAUDE_CODE_USE_WHEN)
        self.assertNotIn("跑构建", CLAUDE_CODE_USE_WHEN)
        self.assertNotIn("--allowedTools", CLAUDE_CODE_ARGV)
        self.assertNotIn("--dangerously-skip-permissions", CLAUDE_CODE_ARGV)
        self.assertNotIn("bypassPermissions", CLAUDE_CODE_ARGV)
        self.assertNotIn("Bash", CLAUDE_CODE_ARGV)
        self.assertNotIn(_removed_adapter_name(), ADAPTER_CATALOG)
        self.assertEqual(ADAPTER_CATALOG["codex"]["argv"],
                         ["exec", "--sandbox", "workspace-write", "-"])

    def _connector(self, installations):
        data = self.root / "device"
        data.mkdir(exist_ok=True)
        store = self._track(Store(data / "connector.db"))
        live = {"host_id": "local",
                "hosts": [{"id": "local", "transport": "local", "os": "Linux",
                           "allowed_roots": [str(self.workspace)]}],
                "installations": installations, "resources": []}
        holder = SimpleNamespace(store=store, data=data, config=live, config_revision=0,
                                 execution=Execution(live, data), host_id="local")
        return LocalSetup(holder), holder

    def test_configure_installs_the_grant_and_leaves_other_argv_stored(self):
        old = ["/opt/old/claude", "-p"]
        codex = {"id": "codex-local", "host_id": "local", "adapter": "codex", "enabled": True,
                 "argv": ["/opt/old/codex", "exec", "-"], "cwd": str(self.workspace),
                 "prompt_mode": "stdin", "risk": "write", "resource_keys": [],
                 "max_concurrency": 1, "env_allowlist": []}
        claude = {"id": "claude-code-local", "host_id": "local", "adapter": "claude-code",
                  "enabled": True, "argv": list(old), "cwd": str(self.workspace),
                  "prompt_mode": "stdin", "risk": "write", "resource_keys": [],
                  "max_concurrency": 1, "env_allowlist": []}
        setup, holder = self._connector([claude, codex])
        # Loading the stored installation does not rewrite its argv.
        self.assertEqual(holder.execution.installations["claude-code-local"]["argv"], old)
        inventory = [{"adapter": "claude-code", "binary": str(self.stub), "version": "stub"},
                     {"adapter": "gemini", "binary": "/opt/gemini", "version": "stub"}]
        with patch.object(LocalSetup, "discover", return_value={"inventory": inventory}):
            setup.configure({"adapter": "claude-code"})
            setup.configure({"adapter": "gemini"})
        granted = holder.execution.installations["claude-code-local"]["argv"]
        self.assertEqual(granted, [str(self.stub), *CLAUDE_CODE_ARGV])
        self.assertEqual(holder.execution.installations["gemini-local"]["argv"],
                         ["/opt/gemini", "--skip-trust"])
        # The codex installation was not reconfigured, so its stored argv stays.
        self.assertEqual(holder.execution.installations["codex-local"]["argv"],
                         ["/opt/old/codex", "exec", "-"])

    def test_saved_claude_argv_survives_local_host_registration(self):
        # ensure_local_host migrates only a bare binary name. An installation that
        # already has flags keeps them until configure runs again.
        cfg = config(self.workspace)
        saved = ["/usr/bin/claude", "-p"]
        cfg["installations"].append({
            "id": "claude-code-local", "host_id": "local", "adapter": "claude-code",
            "argv": list(saved), "cwd": str(self.workspace), "risk": "write",
            "probe_argv": [sys.executable, "--version"]})
        app = Service(cfg, self.root / "cloud-migrate")
        self._closers.append(app.store.close)
        app.ensure_local_host(str(self.workspace), discover=False)
        self.assertEqual(app.execution.installations["claude-code-local"]["argv"], saved)

    def _drive(self, argv):
        cfg = config(self.workspace)
        cfg["installations"][0].update(
            id="claude-code-local", adapter="claude-code", argv=list(argv),
            probe_argv=[sys.executable, "--version"], cwd=str(self.workspace))
        app = Service(cfg, self.root / ("cloud-" + str(len(argv))))
        self._closers.append(app.store.close)
        app.probe("claude-code-local")
        msg = app.send("bot-main", {"text": "task", "allow_write": True}, "exec-" + str(len(argv)))
        res = app.submit_task("bot-main", msg["work_id"], {
            "prompt": "写文件", "required_outputs": ["final.docx"],
            "installation_id": "claude-code-local"})
        task = app.store.get("task", res["task_id"])
        for _ in range(200):
            app.task_step()
            task = app.store.get("task", task["id"])
            if task["status"] in ("succeeded", "failed", "cancelled", "unknown"):
                break
            time.sleep(0.03)
        return task

    def _launch(self, configured):
        # The runner spawns installation argv directly. POSIX can exec the shebang
        # stub, which is the configured argv. Windows CreateProcess cannot start a
        #.py script, so the same stub is started with this interpreter and the
        # same grant tail.
        if os.name == "nt":
            return [sys.executable, configured[0], *configured[1:]]
        return list(configured)

    def test_configured_grant_satisfies_required_output(self):
        configured = [str(self.stub), *CLAUDE_CODE_ARGV]
        task = self._drive(self._launch(configured))
        self.assertEqual(task["status"], "succeeded", task.get("receipt"))
        receipt = task["receipt"]
        self.assertEqual([item["present"] for item in receipt["required_output_results"]], [True])
        self.assertEqual([item["source"] for item in receipt.get("artifacts", [])], ["output_dir"])

    def test_stub_does_not_write_without_the_grant(self):
        # The pre-fix argv: print mode only. The process can still exit 0 (a version
        # probe would look available) but the required file is never written.
        task = self._drive(self._launch([str(self.stub), "-p"]))
        self.assertEqual(task["status"], "failed", task.get("receipt"))
        self.assertEqual(task.get("error"), "required_output_missing")
