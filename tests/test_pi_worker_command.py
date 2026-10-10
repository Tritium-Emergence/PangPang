"""Worker selection is local-only and must never replay work on another runtime."""
import argparse
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launch import ensure_model_runtime
from pangpang.pi_runtime import PiModel
import pangpang.pi_worker_command as pwc
from pangpang.pi_worker_command import (BUN_BUNDLE_ENV, NODE_BUNDLE_ENV, WORKER_ENV,
                                         worker_command, worker_invocation)
from pangpang.store import Fault
from test_model_protocol import Endpoint


class WorkerSelectionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ)
        self.env.start()
        os.environ.pop(WORKER_ENV, None)
        os.environ.pop(NODE_BUNDLE_ENV, None)
        os.environ.pop(BUN_BUNDLE_ENV, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Isolate release auto-discovery: point it at a non-existent path so these
        # selector/default tests never pick up a locally built runtime/pi-worker tree.
        self.release = patch("pangpang.pi_worker_command.release_bundle_dir",
                             return_value=self.root / "no-release")
        self.release.start()
        self.binary = self.root / "worker with spaces.exe"
        self.binary.write_bytes(b"test fixture")
        self.binary.chmod(0o700)

    def tearDown(self):
        self.release.stop()
        self.tmp.cleanup()
        self.env.stop()

    def test_default_is_existing_node_worker(self):
        argv = worker_command()
        self.assertEqual(argv[0], "node")
        self.assertEqual(Path(argv[1]).name, "pi_worker.mjs")

    def test_binary_with_spaces_is_one_argument_and_needs_no_npm_at_launch(self):
        os.environ[WORKER_ENV] = str(self.binary)
        self.assertEqual(worker_command(), [str(self.binary.resolve())])
        with patch("pangpang.cli.subprocess.run") as run, patch("pangpang.cli.subprocess.check_output") as check:
            ensure_model_runtime(self.root, self.root / ".venv", argparse.ArgumentParser())
        run.assert_not_called()
        check.assert_not_called()

    def test_invalid_explicit_selection_does_not_fall_back(self):
        for value in ("", "relative-worker", str(self.root), str(self.root / "missing"),
                      str(self.binary) + " --extra-argument"):
            with self.subTest(value=value):
                os.environ[WORKER_ENV] = value
                with patch("pangpang.pi_runtime.subprocess.Popen") as spawn:
                    with self.assertRaises(Fault) as raised:
                        PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
                self.assertEqual(raised.exception.code, "pi_worker_invalid_binary_path")
                spawn.assert_not_called()

    def test_node_bundle_with_spaces_needs_no_system_node_or_npm(self):
        root = self.root / "node bundle with spaces"
        root.mkdir()
        node = root / ("node.exe" if os.name == "nt" else "node")
        node.write_bytes(b"test fixture")
        node.chmod(0o700)
        script = root / "pi_worker.bundle.mjs"
        script.write_text("// fixture")
        os.environ[NODE_BUNDLE_ENV] = str(root)
        self.assertEqual(worker_command(), [str(node.resolve()), str(script.resolve())])
        with patch("pangpang.cli.subprocess.run") as run, patch("pangpang.cli.subprocess.check_output") as check:
            ensure_model_runtime(self.root, self.root / ".venv", argparse.ArgumentParser())
        run.assert_not_called()
        check.assert_not_called()
        with patch("pangpang.pi_runtime.subprocess.Popen", side_effect=OSError("bad executable")) as spawn:
            with self.assertRaises(Fault) as raised:
                PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
        self.assertEqual(raised.exception.code, "pi_worker_binary_unavailable")
        self.assertEqual(spawn.call_count, 1)
        self.assertNotIn(NODE_BUNDLE_ENV, spawn.call_args.kwargs["env"])

    def test_bun_bundle_pins_cwd_and_hardens_runtime_no_system_bun_or_npm(self):
        # formal shape: pinned bun(.exe) + bundled worker JS in one directory.
        root = self.root / "bun bundle with spaces"
        root.mkdir()
        bun = root / ("bun.exe" if os.name == "nt" else "bun")
        bun.write_bytes(b"test fixture")
        bun.chmod(0o700)
        script = root / "pi_worker.bundle.mjs"
        script.write_text("// fixture")
        os.environ[BUN_BUNDLE_ENV] = str(root)
        argv, cwd = worker_invocation()
        # Runs the PACKAGED bun on the bundle; hardening flags + cwd pinned to the
        # artifact dir so a hostile working directory's bunfig.toml/.env cannot inject.
        self.assertEqual(argv, [str(bun.resolve()), "run", "--no-install",
                                "--no-env-file", "--no-orphans", str(script.resolve())])
        self.assertEqual(cwd, str(root.resolve()))
        with patch("pangpang.cli.subprocess.run") as run, patch("pangpang.cli.subprocess.check_output") as check:
            ensure_model_runtime(self.root, self.root / ".venv", argparse.ArgumentParser())
        run.assert_not_called()
        check.assert_not_called()
        with patch("pangpang.pi_runtime.subprocess.Popen", side_effect=OSError("bad executable")) as spawn:
            with self.assertRaises(Fault) as raised:
                PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
        self.assertEqual(raised.exception.code, "pi_worker_binary_unavailable")
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(spawn.call_args.kwargs["cwd"], str(root.resolve()))
        for name in (BUN_BUNDLE_ENV, "BUN_OPTIONS", "NODE_OPTIONS"):
            self.assertNotIn(name, spawn.call_args.kwargs["env"])

    def test_invalid_bun_bundle_never_spawns_and_does_not_fall_back(self):
        for value, expected in (("", "pi_worker_invalid_bun_bundle"),
                                ("relative", "pi_worker_invalid_bun_bundle"),
                                (str(self.root / "missing"), "pi_worker_invalid_bun_bundle"),
                                (str(self.root), "pi_worker_incomplete_bun_bundle")):
            with self.subTest(value=value):
                os.environ[BUN_BUNDLE_ENV] = value
                with self.assertRaisesRegex(ValueError, expected):
                    worker_command()
                with patch("pangpang.pi_runtime.subprocess.Popen") as spawn:
                    with self.assertRaises(Fault) as raised:
                        PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
                self.assertEqual(raised.exception.code, "pi_worker_invalid_binary_path")
                spawn.assert_not_called()

    def test_two_packaged_selectors_conflict_and_never_spawn(self):
        # Any two of the three selectors set at once is a hard conflict (never guess).
        root = self.root / "bundle"
        root.mkdir()
        (root / ("bun.exe" if os.name == "nt" else "bun")).write_bytes(b"x")
        (root / "pi_worker.bundle.mjs").write_text("// fixture")
        for a, b in ((WORKER_ENV, BUN_BUNDLE_ENV), (NODE_BUNDLE_ENV, BUN_BUNDLE_ENV)):
            with self.subTest(pair=(a, b)):
                os.environ[a] = str(self.binary)
                os.environ[b] = str(root)
                with self.assertRaisesRegex(ValueError, "pi_worker_conflicting_selection"):
                    worker_command()
                with patch("pangpang.pi_runtime.subprocess.Popen") as spawn:
                    with self.assertRaises(Fault):
                        PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
                spawn.assert_not_called()
                os.environ.pop(a, None)
                os.environ.pop(b, None)

    def test_invalid_node_bundle_and_conflicting_selection_never_spawn(self):
        for value, expected in (("", "pi_worker_invalid_node_bundle"),
                                ("relative", "pi_worker_invalid_node_bundle"),
                                (str(self.root / "missing"), "pi_worker_invalid_node_bundle"),
                                (str(self.root), "pi_worker_incomplete_node_bundle")):
            with self.subTest(value=value):
                os.environ[NODE_BUNDLE_ENV] = value
                with self.assertRaisesRegex(ValueError, expected):
                    worker_command()
                with patch("pangpang.pi_runtime.subprocess.Popen") as spawn:
                    with self.assertRaises(Fault) as raised:
                        PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
                self.assertEqual(raised.exception.code, "pi_worker_invalid_binary_path")
                spawn.assert_not_called()
        os.environ[WORKER_ENV] = str(self.binary)
        with self.assertRaisesRegex(ValueError, "pi_worker_conflicting_selection"):
            worker_command()

    def test_spawn_failure_is_explicit_and_not_retried(self):
        os.environ[WORKER_ENV] = str(self.binary)
        with patch("pangpang.pi_runtime.subprocess.Popen", side_effect=OSError("bad executable")) as spawn:
            with self.assertRaises(Fault) as raised:
                PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
        self.assertEqual(raised.exception.code, "pi_worker_binary_unavailable")
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(spawn.call_args.args[0], [str(self.binary.resolve())])

    def test_failure_after_tool_does_not_replay_and_keeps_env_allowlist(self):
        os.environ[WORKER_ENV] = str(self.binary)
        os.environ["OPENAI_API_KEY"] = "fixture-never-forward"
        os.environ["BUN_OPTIONS"] = "--preload untrusted-fixture.js"
        os.environ["NODE_OPTIONS"] = "--require untrusted-fixture.js"
        events = [{"version": 1, "id": 1, "type": "tool", "name": "fixture_action"},
                  {"version": 1, "type": "failed"}]
        proc = Mock(stdin=io.StringIO(), stdout=io.StringIO("".join(json.dumps(e) + "\n" for e in events)))
        proc.poll.return_value = 0
        effects = []
        def handle(event):
            effects.append(event["name"])
            return {"committed": True}
        with patch("pangpang.pi_runtime.subprocess.Popen", return_value=proc) as spawn:
            with self.assertRaises(Fault) as raised:
                PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], handle)
        self.assertEqual(raised.exception.code, "pi_worker_failed")
        self.assertEqual(effects, ["fixture_action"])
        self.assertEqual(spawn.call_count, 1)
        for name in (WORKER_ENV, "OPENAI_API_KEY", "BUN_OPTIONS", "NODE_OPTIONS"):
            self.assertNotIn(name, spawn.call_args.kwargs["env"])
        proc.wait.assert_called_once()

    def test_init_stdin_broken_pipe_is_pi_worker_failed(self):
        # A worker that dies before reading stdin raises BrokenPipeError on the
        # init write and again when close() flushes. Both are OSError. The fault
        # that surfaces is pi_worker_failed, not an escaping OSError (which the
        # scheduler would record as runtime_error).
        os.environ[WORKER_ENV] = str(self.binary)
        class DeadStdin:
            def write(self, data):
                raise BrokenPipeError(32, "Broken pipe")
            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")
            def close(self):
                raise BrokenPipeError(32, "Broken pipe")
        proc = Mock(stdin=DeadStdin(), stdout=io.StringIO(""))
        proc.poll.return_value = 0
        with patch("pangpang.pi_runtime.subprocess.Popen", return_value=proc):
            with self.assertRaises(Fault) as raised:
                PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
        self.assertEqual(raised.exception.code, "pi_worker_failed")
        self.assertNotIsInstance(raised.exception, OSError)


class ReleaseAutoSelectionTests(unittest.TestCase):
    """End users set no env var: a release tree ships the packaged Bun worker at a fixed
    location the launcher auto-selects; corrupt -> explicit fault; never silent Node."""
    def setUp(self):
        self.env = patch.dict(os.environ)
        self.env.start()
        for name in (WORKER_ENV, NODE_BUNDLE_ENV, BUN_BUNDLE_ENV):
            os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release_dir = self.root / "runtime" / "pi-worker"
        self.release_dir.mkdir(parents=True)
        # Patch the release ROOT (not the bundle dir) so these tests exercise the real
        # release_bundle_dir() composition root/runtime/pi-worker.
        self.patch = patch("pangpang.pi_worker_command.release_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    def _populate(self):
        bun = self.release_dir / ("bun.exe" if os.name == "nt" else "bun")
        bun.write_bytes(b"test fixture bun")
        bun.chmod(0o700)
        (self.release_dir / "pi_worker.bundle.mjs").write_text("// fixture")
        return bun

    def test_release_bundle_is_auto_selected_without_any_env_var(self):
        bun = self._populate()
        argv, cwd = worker_invocation()
        self.assertEqual(argv, [str(bun.resolve()), "run", "--no-install",
                                "--no-env-file", "--no-orphans",
                                str((self.release_dir / "pi_worker.bundle.mjs").resolve())])
        self.assertEqual(cwd, str(self.release_dir.resolve()))

    def test_frozen_layout_resolves_release_bundle_from_sys_executable(self):
        # PyInstaller onedir: pi_worker_command lives under <root>/_internal/pangpang/, so the
        # source-tree parents[1] guess would miss the real <root>/runtime/pi-worker. release_root()
        # must follow sys.executable when sys.frozen is set. Simulate the real frozen layout.
        self.patch.stop() # exercise the real release_root(), not the stub
        self._populate()
        exe = self.root / ("coordinator.exe" if os.name == "nt" else "coordinator")
        exe.write_bytes(b"frozen fixture")
        with patch.object(pwc.sys, "frozen", True, create=True), \
             patch.object(pwc.sys, "executable", str(exe)):
            self.assertEqual(pwc.release_root(), self.root.resolve())
            argv, cwd = worker_invocation()
        self.assertEqual(cwd, str(self.release_dir.resolve()))
        self.assertTrue(argv[0].endswith("bun.exe") or argv[0].endswith("bun"))
        self.patch.start() # restored for tearDown symmetry

    def test_no_release_tree_falls_through_to_node_default(self):
        # A plain source checkout (NOT frozen, no release dir) legitimately uses the Node dev default.
        self.patch.stop()
        self.patch = patch("pangpang.pi_worker_command.release_root", return_value=self.root / "absent")
        self.patch.start()
        argv, cwd = worker_invocation()
        self.assertEqual(argv[0], "node")
        self.assertEqual(Path(argv[1]).name, "pi_worker.mjs")

    def test_frozen_with_absent_release_dir_fails_closed_never_node(self):
        # red line: in a FROZEN product (no source tree, no system Node) an absent
        # runtime/pi-worker/ directory is a packaging failure and must fail closed, NOT
        # silently fall back to ['node',...]. (Source checkout keeps the Node dev default;
        # the frozen flag is what distinguishes the two.)
        self.patch.stop()
        self.patch = patch("pangpang.pi_worker_command.release_root", return_value=self.root / "absent")
        self.patch.start()
        with patch.object(pwc.sys, "frozen", True, create=True):
            with self.assertRaisesRegex(ValueError, "pi_worker_incomplete_release_bundle"):
                worker_command()
            with self.assertRaisesRegex(ValueError, "pi_worker_incomplete_release_bundle"):
                worker_invocation()
            with patch("pangpang.pi_runtime.subprocess.Popen") as spawn:
                with self.assertRaises(Fault) as raised:
                    PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
            self.assertEqual(raised.exception.code, "pi_worker_invalid_binary_path")
            spawn.assert_not_called()

    def test_corrupt_release_bundle_fails_startup_and_never_uses_node(self):
        # Directory exists (release mode) but the bundle files are missing -> explicit fault,
        # NOT a silent fall-through to Node.
        with self.assertRaisesRegex(ValueError, "pi_worker_incomplete_release_bundle"):
            worker_command()
        with patch("pangpang.pi_runtime.subprocess.Popen") as spawn:
            with self.assertRaises(Fault) as raised:
                PiModel().exchange({"endpoint": "http://127.0.0.1:1"}, [], [], lambda e: {})
        self.assertEqual(raised.exception.code, "pi_worker_invalid_binary_path")
        spawn.assert_not_called()

    def test_explicit_env_selector_overrides_release_bundle(self):
        # Operator override (dev / A-B / oracle) takes precedence over release auto-discovery.
        self._populate()
        other = Path(self.tmp.name) / "nodedir"
        other.mkdir()
        node = other / ("node.exe" if os.name == "nt" else "node")
        node.write_bytes(b"x")
        node.chmod(0o700)
        (other / "pi_worker.bundle.mjs").write_text("// fixture")
        os.environ[NODE_BUNDLE_ENV] = str(other)
        argv, cwd = worker_invocation()
        self.assertEqual(argv, [str(node.resolve()), str((other / "pi_worker.bundle.mjs").resolve())])
        self.assertIsNone(cwd)


@unittest.skipUnless(os.environ.get(WORKER_ENV) or os.environ.get(NODE_BUNDLE_ENV) or os.environ.get(BUN_BUNDLE_ENV),
                     "select a packaged worker for artifact acceptance")
class PackagedWorkerTests(unittest.TestCase):
    def test_standalone_completion_ignores_working_directory_preload(self):
        endpoint = Endpoint(lambda p, n: {"content": "独立运行成功"})
        old_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "untrusted.cjs").write_text("throw new Error('ambient preload executed');\n")
                (root / "bunfig.toml").write_text('preload = ["./untrusted.cjs"]\n')
                (root / ".env").write_text("BUN_OPTIONS=--preload./untrusted.cjs\nOPENAI_API_KEY=ambient-fixture\n")
                (root / "package.json").write_text('{"type":"commonjs"}')
                try:
                    os.chdir(root) # no project sources or node_modules in this directory
                    message, _ = PiModel().complete({"endpoint": endpoint.url, "model": "test"},
                                                   [{"role": "user", "content": "你好"}], [])
                    self.assertEqual(message["content"], "独立运行成功")
                    self.assertEqual(len(endpoint.received), 1)
                finally:
                    os.chdir(old_cwd) # Windows cannot remove the working directory.
        finally:
            os.chdir(old_cwd)
            endpoint.close()


if __name__ == "__main__":
    unittest.main()
