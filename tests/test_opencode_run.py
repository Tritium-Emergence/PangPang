"""OpenCode-family launches use `run` and the closed stdin prompt.

The default command is the interactive TUI. A wrapper that only does
``exec opencode -m <model> "$@"`` forwards whatever this runner appends.
`opencode run` with no message arguments reads a non-TTY stdin until EOF,
which is the prompt the runner already writes and closes. The prompt is not
also placed on argv: `run` joins message arguments with that stdin.

`--auto` is not added. It approves permission prompts. Claude stream-json
and Codex `exec` / app-server launches are not rewritten by this path.
"""
import json
import os
import signal
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_adapters_file import catalog_teardown
from test_bot_runtime import config
from pangpang import runner
from pangpang.execution import CLAUDE_STREAM_JSON
from pangpang.service import Service


PROMPT = "SENTINEL_PROMPT_DO_NOT_LEAK\n你好"
MODEL = "aicore-gemini/gemini-4-argon"

# The Mac wrapper from the report: model flag, then "$@". Records the argv
# OpenCode would see and the stdin `run` would read, then exits.
WRAPPER = r"""#!/usr/bin/env python3
import json, sys, pathlib
args = sys.argv[1:]
root = pathlib.Path(sys.argv[0]).resolve().parent
inner = ["opencode", "-m", %r, *args]
prompt = sys.stdin.read()
(root / "launch-record.json").write_text(
    json.dumps({"inner": inner, "stdin": prompt}, ensure_ascii=False),
    encoding="utf-8")
if "run" not in args:
    sys.stdout.write("TUI\n")
    raise SystemExit(7)
sys.stdout.write(prompt)
raise SystemExit(0)
""" % MODEL


def _execution_id(byte):
    return "exec-" + (byte * 16)


class OpenCodeArgvTests(unittest.TestCase):
    def test_family_command_gains_one_run_and_not_the_prompt(self):
        bare = runner.executor_argv({"argv": ["opencode"]}, PROMPT)
        self.assertEqual(bare, ["opencode", "run"])
        wrapped = runner.executor_argv(
            {"argv": ["/Users/me/.local/bin/gemini-opencode"]}, PROMPT)
        self.assertEqual(wrapped, ["/Users/me/.local/bin/gemini-opencode", "run"])
        windows = runner.executor_argv(
            {"argv": [r"C:\Tools\Gemini-OpenCode.CMD"]}, PROMPT)
        self.assertEqual(windows, [r"C:\Tools\Gemini-OpenCode.CMD", "run"])
        exe = runner.executor_argv({"argv": ["opencode.exe"]}, PROMPT)
        self.assertEqual(exe, ["opencode.exe", "run"])
        flagged = runner.executor_argv(
            {"argv": ["opencode", "-m", MODEL, "--pure"]}, PROMPT)
        self.assertEqual(flagged, ["opencode", "-m", MODEL, "--pure", "run"])
        inline = runner.executor_argv(
            {"argv": ["opencode", "--model=" + MODEL]}, PROMPT)
        self.assertEqual(inline, ["opencode", "--model=" + MODEL, "run"])
        # A flag value of `run` is not the subcommand.
        titled = runner.executor_argv(
            {"argv": ["opencode", "--title", "run"]}, PROMPT)
        self.assertEqual(titled, ["opencode", "--title", "run", "run"])
        for argv in (bare, wrapped, windows, exe, flagged, inline, titled):
            self.assertNotIn(PROMPT, argv)
            self.assertNotIn("--auto", argv)
            self.assertNotIn("--output-format=stream-json", argv)
            self.assertNotIn("--json", argv)

    def test_existing_run_is_not_doubled_and_other_commands_stay(self):
        already = ["gemini-opencode", "run"]
        self.assertEqual(runner.executor_argv({"argv": already}, PROMPT), already)
        formatted = ["opencode", "run", "--format", "json"]
        self.assertEqual(runner.executor_argv({"argv": formatted}, PROMPT), formatted)
        serve = ["opencode", "serve", "--port", "4096"]
        self.assertEqual(runner.executor_argv({"argv": serve}, PROMPT), serve)
        unknown = ["opencode", "--new-flag", "value"]
        self.assertEqual(runner.executor_argv({"argv": unknown}, PROMPT), unknown)
        ended = ["opencode", "--", "run"]
        self.assertEqual(runner.executor_argv({"argv": ended}, PROMPT), ended)
        # A stored positional is the operator's argument. `run` is not
        # inserted in front of it: the prompt would then be on argv and stdin.
        placed = runner.executor_argv({"argv": ["opencode", "{prompt}"]}, "hello")
        self.assertEqual(placed, ["opencode", "hello"])

    def test_claude_codex_and_other_bins_are_unchanged(self):
        for name in ("myopencode", "opencode-cli", "claude", "gemini", "codex"):
            self.assertEqual(runner.executor_argv({"argv": [name]}, PROMPT), [name])
        self.assertEqual(
            runner.executor_argv(
                {"argv": ["gemini", "--skip-trust"]}, PROMPT),
            ["gemini", "--skip-trust"])
        self.assertEqual(
            runner.executor_argv(
                {"argv": ["codex", "exec", "--sandbox", "workspace-write", "-"]}, PROMPT),
            ["codex", "exec", "--json", "--sandbox", "workspace-write", "-"])
        self.assertEqual(
            runner.executor_argv({"argv": ["codex", "exec", "{prompt}"]}, "hello"),
            ["codex", "exec", "--json", "hello"])
        claude = runner.executor_argv({
            "argv": ["claude", "-p", "--permission-mode", "acceptEdits"],
            "execution_id": _execution_id("ab"),
            "stdin_protocol": CLAUDE_STREAM_JSON,
        }, PROMPT)
        self.assertEqual(claude[:4], ["claude", "-p", "--permission-mode", "acceptEdits"])
        self.assertIn("--output-format=stream-json", claude)
        self.assertNotIn("run", claude)
        self.assertNotIn(PROMPT, claude)
        self.assertNotIn("--json", claude)


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class OpenCodeLaunchTests(unittest.TestCase):
    def setUp(self):
        catalog_teardown(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.script = self.root / "gemini-opencode"
        self.script.write_text(WRAPPER, encoding="utf-8")
        self.script.chmod(0o755)
        row = {"binary": "gemini-opencode", "argv": [], "concurrency": 1, "priority": 25,
               "use_when": "OpenCode wrapper that only adds a model flag."}
        path = self.data / "adapters.json"
        path.write_text(json.dumps({"gemini-opencode": row}), encoding="utf-8")
        path.chmod(0o644)
        self.app = Service(config(self.workspace), self.data)
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.tmp.cleanup)
        self.threads = []
        base = self.app.execution.installations["codex-local"]
        for installation_id, argv in (
                ("gemini-opencode-local", [str(self.script)]),
                ("gemini-opencode-run-local", [str(self.script), "run"])):
            installed = dict(base)
            installed.update(id=installation_id, adapter="gemini-opencode",
                             argv=list(argv), prompt_mode="stdin")
            self.app.execution.installations[installation_id] = installed

    def tearDown(self):
        for thread, directory in self.threads:
            if thread.is_alive():
                started = runner.read_json_bounded(directory / "started.json") or {}
                pid = started.get("pid")
                if isinstance(pid, int) and pid > 0:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                thread.join(5)

    def _run(self, installation_id, execution_id):
        record = self.root / "launch-record.json"
        if record.exists():
            record.unlink()
        host = self.app.execution.hosts["local"]
        task = {
            "installation_id": installation_id,
            "execution_id": execution_id,
            "spec_digest": "digest-" + installation_id,
            "prompt": PROMPT,
            "inputs": [],
            "cwd": str(self.workspace),
        }
        spec = self.app.execution.spec(task)
        self.assertEqual(spec["prompt_mode"], "stdin")
        self.assertNotIn("stdin_protocol", spec)
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
        receipt = runner.inspect(directory, include_tail=True)
        recorded = json.loads(record.read_text(encoding="utf-8"))
        return spec, receipt, recorded

    def test_wrapper_with_empty_argv_runs_and_receives_stdin_prompt(self):
        spec, receipt, recorded = self._run("gemini-opencode-local", _execution_id("ab"))
        self.assertEqual(spec["argv"], [str(self.script)])
        self.assertEqual(recorded["inner"], ["opencode", "-m", MODEL, "run"])
        self.assertEqual(recorded["inner"].count("run"), 1)
        self.assertNotIn(PROMPT, recorded["inner"])
        self.assertEqual(recorded["stdin"], PROMPT)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertFalse(receipt["timed_out"])
        self.assertNotIn("result_text", receipt)
        self.assertIn(PROMPT, receipt["tail"])
        self.assertNotIn("TUI", receipt["tail"])

    def test_wrapper_that_already_has_run_is_not_doubled(self):
        spec, receipt, recorded = self._run("gemini-opencode-run-local", _execution_id("cd"))
        self.assertEqual(spec["argv"], [str(self.script), "run"])
        self.assertEqual(recorded["inner"], ["opencode", "-m", MODEL, "run"])
        self.assertEqual(recorded["inner"].count("run"), 1)
        self.assertEqual(recorded["stdin"], PROMPT)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertFalse(receipt["timed_out"])
        self.assertIn(PROMPT, receipt["tail"])


if __name__ == "__main__":
    unittest.main()
