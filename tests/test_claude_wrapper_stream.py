"""Claude-compatible custom adapters speak the existing stream-json launch.

A row may name stdin_protocol claude-stream-json. The runner appends the same
flags the built-in Claude Code row already gets, after any stored
``--settings <value>``, and adds ``-p`` only when print mode is absent.
The final text is still the last ``type=result`` event. A row that omits the
protocol stays on closed stdin and the banner tail. Online inject stays off.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_adapters_file import catalog_teardown
from test_bot_runtime import config
from pangpang import runner
from pangpang.activity import activity_projection
from pangpang.execution import (
    ADAPTER_CATALOG, CLAUDE_STREAM_JSON, interjection_tier_of, stdin_protocol_for,
    task_interjection_tier,
)
from pangpang.service import Service


SENTINEL = "SENTINEL_PROMPT_DO_NOT_LEAK"
SETTINGS = "/tmp/host-claude-settings.json"
STREAM_FLAGS = (
    "-p",
    "--input-format=stream-json",
    "--output-format=stream-json",
    "--verbose",
    "--replay-user-messages",
)

# Forwards every argument, and prefixes ``--settings`` the way a Claude
# wrapper does. Without stream-json it prints the startup banner and exits.
WRAPPER = r"""
import json, sys, pathlib
args = sys.argv[1:]
inner = ["claude", "--settings", %r, *args]
name = "inner-argv.json" if "--output-format=stream-json" in args else "plain-argv.json"
pathlib.Path(name).write_text(json.dumps(inner), encoding="utf-8")
if "--output-format=stream-json" not in args:
    sys.stdout.write("Claude Code banner\n")
    sys.stdout.flush()
    raise SystemExit(0)
sys.stdin.readline()
sys.stdout.write("Claude Code banner\n")
sys.stdout.write(json.dumps({
    "type": "assistant",
    "message": {"content": [{
        "type": "tool_use", "name": "Read",
        "input": {"file_path": "src/a.py"}}]}}, ensure_ascii=False) + "\n")
sys.stdout.write(json.dumps({
    "type": "result", "result": "FINAL_ANSWER", "user_message_uuids": []}) + "\n")
sys.stdout.flush()
""" % SETTINGS


def _execution_id(byte):
    return "exec-" + (byte * 16)


class StreamArgvTests(unittest.TestCase):
    def test_flags_follow_a_settings_value_and_print_is_added_once(self):
        execution_id = _execution_id("ab")
        spec = {
            "argv": ["host-claude", "--settings", SETTINGS],
            "execution_id": execution_id,
            "stdin_protocol": CLAUDE_STREAM_JSON,
        }
        argv = runner.executor_argv(spec, SENTINEL)
        self.assertEqual(argv[:3], ["host-claude", "--settings", SETTINGS])
        self.assertEqual(argv[3:], [
            "-p",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--verbose",
            "--replay-user-messages",
            "--session-id=" + runner.session_id_from_execution(execution_id),
        ])
        self.assertNotIn(SENTINEL, argv)
        self.assertEqual(argv.count("-p"), 1)
        already = runner.executor_argv({
            "argv": ["host-claude", "-p", "--settings", SETTINGS],
            "execution_id": execution_id,
            "stdin_protocol": CLAUDE_STREAM_JSON,
        }, SENTINEL)
        self.assertEqual(already[:4], ["host-claude", "-p", "--settings", SETTINGS])
        self.assertEqual(already.count("-p"), 1)
        printed = runner.executor_argv({
            "argv": ["host-claude", "--print", "--settings=" + SETTINGS],
            "execution_id": execution_id,
            "stdin_protocol": CLAUDE_STREAM_JSON,
        }, SENTINEL)
        self.assertEqual(printed[:3], ["host-claude", "--print", "--settings=" + SETTINGS])
        self.assertNotIn("-p", printed)
        plain = runner.executor_argv({"argv": ["host-claude", "{prompt}"]}, SENTINEL)
        self.assertEqual(plain, ["host-claude", SENTINEL])

    def test_built_in_claude_argv_is_unchanged(self):
        execution_id = _execution_id("cd")
        spec = {
            "argv": ["-p", "--permission-mode", "acceptEdits", "--allowed-tools", "Read", "Write"],
            "execution_id": execution_id,
            "stdin_protocol": CLAUDE_STREAM_JSON,
        }
        argv = runner.executor_argv(spec, SENTINEL)
        self.assertEqual(argv[:6], spec["argv"])
        self.assertEqual(argv.count("-p"), 1)
        self.assertNotIn("--print", argv)


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class WrapperSuperviseTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.wrapper = self.root / "host-claude.py"
        self.wrapper.write_text(WRAPPER, encoding="utf-8")
        row = {"binary": "host-claude", "argv": [], "concurrency": 1, "priority": 20,
               "use_when": "Claude-compatible wrapper."}
        (self.data / "adapters.json").write_text(json.dumps({
            "host-claude": dict(row, stdin_protocol=CLAUDE_STREAM_JSON),
            "host-plain": dict(row, binary="host-plain"),
        }), encoding="utf-8")
        (self.data / "adapters.json").chmod(0o644)
        self.app = Service(config(self.workspace), self.data)
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.tmp.cleanup)
        self.threads = []
        base = self.app.execution.installations["codex-local"]
        command = [sys.executable, str(self.wrapper)]
        for name in ("host-claude", "host-plain"):
            row = dict(base)
            row.update(id=name + "-local", adapter=name, argv=list(command), prompt_mode="stdin")
            self.app.execution.installations[row["id"]] = row

    def tearDown(self):
        for thread, directory in self.threads:
            if thread.is_alive():
                started = runner.read_json_bounded(directory / "started.json") or {}
                pid = started.get("pid")
                if isinstance(pid, int) and pid > 0:
                    try:
                        os.killpg(pid, signal_kill())
                    except OSError:
                        pass
                thread.join(5)

    def _run(self, adapter, execution_id):
        host = self.app.execution.hosts["local"]
        task = {
            "installation_id": adapter + "-local",
            "execution_id": execution_id,
            "spec_digest": "digest-" + adapter,
            "prompt": SENTINEL,
            "inputs": [],
            "cwd": str(self.workspace),
        }
        spec = self.app.execution.spec(task)
        directory = Path(self.app.execution.directory(host, execution_id))
        self.app.execution.prepare(host, str(directory), spec)

        def work():
            with patch.object(runner, "_detect_supervision", return_value=runner.PGROUP_COOPERATIVE):
                runner.supervise(directory)

        thread = threading.Thread(target=work)
        self.threads.append((thread, directory))
        thread.start()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        return spec, directory

    def test_wrapper_forwards_stream_flags_and_plain_stays_banner_only(self):
        self.assertEqual(stdin_protocol_for("host-claude"), CLAUDE_STREAM_JSON)
        self.assertEqual(interjection_tier_of(ADAPTER_CATALOG["host-claude"]), 3)
        self.assertIsNone(stdin_protocol_for("host-plain"))
        host = self.app.execution.hosts["local"]
        self.assertEqual(task_interjection_tier(
            self.app.execution.installations["host-claude-local"], host), 3)

        spec, directory = self._run("host-claude", _execution_id("ab"))
        self.assertEqual(spec["stdin_protocol"], CLAUDE_STREAM_JSON)
        self.assertNotIn("resume_protocol", spec)
        inner = json.loads((self.workspace / "inner-argv.json").read_text(encoding="utf-8"))
        self.assertEqual(inner[:3], ["claude", "--settings", SETTINGS])
        for flag in STREAM_FLAGS:
            self.assertIn(flag, inner[3:])
        session = runner.session_id_from_execution(_execution_id("ab"))
        self.assertIn("--session-id=" + session, inner)
        self.assertNotIn(SENTINEL, inner)
        self.assertEqual(session, str(uuid.UUID(hex="ab" * 16)))
        log = (directory / "output.log").read_text(encoding="utf-8")
        self.assertIn("Claude Code banner", log)
        self.assertIn('"type": "tool_use"', log)
        self.assertIn("src/a.py", log)
        receipt = runner.inspect(directory, include_tail=True)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["result_text"], "FINAL_ANSWER")
        self.assertEqual(receipt["tail"], "FINAL_ANSWER")
        self.assertNotIn("banner", receipt["tail"])
        self.assertNotIn("interjection_tier", receipt)
        self.assertEqual(receipt["injects"], [])
        activity = activity_projection(
            {"status": "running", "created": time.time() - 5, "execution_id": _execution_id("ab")},
            directory=directory, cwd=str(self.workspace), roots=[str(self.workspace)])
        self.assertEqual(activity["recent"], [{"tool": "read", "path": "src/a.py"}])

        plain_spec, plain_dir = self._run("host-plain", _execution_id("cd"))
        self.assertNotIn("stdin_protocol", plain_spec)
        plain_inner = json.loads((self.workspace / "plain-argv.json").read_text(encoding="utf-8"))
        self.assertEqual(plain_inner, ["claude", "--settings", SETTINGS])
        plain_log = (plain_dir / "output.log").read_text(encoding="utf-8")
        self.assertEqual(plain_log, "Claude Code banner\n")
        plain = runner.inspect(plain_dir, include_tail=True)
        self.assertEqual(plain["status"], "succeeded")
        self.assertNotIn("result_text", plain)
        self.assertEqual(plain["tail"], "Claude Code banner\n")
        plain_activity = activity_projection(
            {"status": "running", "created": time.time() - 5, "execution_id": _execution_id("cd")},
            directory=plain_dir, cwd=str(self.workspace), roots=[str(self.workspace)])
        self.assertEqual(plain_activity["recent"], [])


def signal_kill():
    import signal
    return signal.SIGKILL


if __name__ == "__main__":
    unittest.main()
