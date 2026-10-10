""" step 2: cancel asks the executor to stop, then escalates.

(a) a child that traps SIGINT and exits 0 is recorded cancelled, the marker
    exists, and SIGKILL is not what stopped it.
(b) a child that ignores SIGINT and SIGTERM is SIGKILLed after the grace, and
    the process group is confirmed empty.
(c) a second cancel during the grace does not reset the clock or repeat a signal.
A later supervise() during the grace returns without adopting the attempt. The
supervisor that launched the child still reaches SIGKILL.
"""
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang import runner
from pangpang.service import Service


def _leader_exited(pid):
    """True once this process's child has exited, without reaping it.

    Same check supervise uses. `_exit_watcher` follows waitid(WNOWAIT) where
    Python provides it, and kqueue NOTE_EXIT otherwise (macOS before 3.13).
    Registration ESRCH on an already-dead unreaped child is already mapped
    to exited inside that watcher.
    """
    return runner._exit_watcher(pid)()


def _script(ready, marker=None):
    """A child that reports readiness only after its signal disposition is set."""
    lines = ["import signal, pathlib, time"]
    if marker is None:
        lines += [
            "signal.signal(signal.SIGINT, signal.SIG_IGN)",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        ]
    else:
        lines += [
            "def stop(signum, frame):",
            " pathlib.Path(%r).write_text('sigint')" % str(marker),
            " raise SystemExit(0)",
            "signal.signal(signal.SIGINT, stop)",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        ]
    lines += [
        "pathlib.Path(%r).write_text('up')" % str(ready),
        "while True:",
        " time.sleep(0.05)",
    ]
    return "\n".join(lines)


@unittest.skipUnless(os.name == "posix" and runner._unreaped_exit_supported(),
                     "POSIX cancel escalation needs an unreaped-exit watch")
class GracefulCancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.app = Service(config(self.workspace), self.root / "data")
        self.app.store.put("probe", {
            "id": "codex-local", "available": True,
            "supervision": runner.PGROUP_COOPERATIVE})
        self.thread = None
        self.directory = None

    def tearDown(self):
        if self.thread is not None and self.thread.is_alive() and self.directory is not None:
            started = runner.read_json_bounded(self.directory / "started.json") or {}
            pid = started.get("pid")
            if isinstance(pid, int) and pid > 0:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
            self.thread.join(5)
        self.app.store.close()
        self.tmp.cleanup()

    def _start(self, code, *, grace, term):
        install = self.app.execution.installations["codex-local"]
        install["argv"] = [sys.executable, "-c", code]
        install["prompt_mode"] = "none"
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True}, "grace")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "task"})
        task = self.app.store.get("task", submitted["task_id"])
        task["attempt_uuid"] = "attempt-grace"
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        host = self.app.execution.hosts[task["host_id"]]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        self.app.execution.prepare(host, str(directory), self.app.execution.spec(task))
        self.directory = directory
        signals, killed_alive, errors = [], [], []
        real_killpg = os.killpg

        def spy_bound(pgid, sig, _real=real_killpg):
            if sig == signal.SIGKILL and not _leader_exited(pgid):
                killed_alive.append(int(pgid))
            if sig:
                signals.append((int(sig), time.monotonic(), int(pgid)))
            return _real(pgid, sig)

        def work():
            try:
                with patch.object(runner, "_detect_supervision", return_value=runner.PGROUP_COOPERATIVE), \
                        patch.object(runner, "CANCEL_GRACE_S", grace), \
                        patch.object(runner, "CANCEL_TERM_WAIT_S", term), \
                        patch.object(runner.os, "killpg", spy_bound):
                    runner.supervise(directory)
            except Exception as error: # surfaced by the join assertion
                errors.append(error)

        self.thread = threading.Thread(target=work)
        self.thread.start()
        deadline = time.monotonic() + 10
        ready = self.workspace / "ready"
        while time.monotonic() < deadline:
            if (directory / "started.json").exists() and ready.exists():
                break
            time.sleep(0.02)
        self.assertTrue((directory / "started.json").exists(), "executor did not start")
        self.assertTrue(ready.exists(), "executor did not install its signal disposition")
        return task, signals, killed_alive, errors

    def _finish(self, task, errors, *, cancels=1, again=False):
        task = self.app.store.get("task", task["id"])
        task["status"] = "running"
        self.app.store.put("task", task)
        self.app.cancel(task["id"])
        if again:
            # The live supervisor has already latched the clock. A second
            # supervise() must return without adopting the attempt.
            started = time.monotonic()
            runner.supervise(self.directory)
            self.assertLess(time.monotonic() - started, 0.5)
        for _ in range(cancels - 1):
            self.assertFalse(runner.request_cancel(self.directory))
        self.thread.join(15)
        self.assertFalse(self.thread.is_alive(), "supervisor did not finish")
        self.assertEqual(errors, [])
        receipt = runner.inspect(self.directory)
        self.app.reconcile(task["id"])
        task = self.app.store.get("task", task["id"])
        return task, receipt

    def _sigs(self, signals, kind):
        return [item for item in signals if item[0] == kind]

    def test_sigint_exit_is_cancelled_without_sigkill(self):
        # (a) the handler writes the marker and exits 0. The settle that follows
        # may SIGKILL the already-exited group; that is the existing empty-group
        # gate, and it is not what stopped the executor.
        marker = self.workspace / "marker.txt"
        task, signals, killed_alive, errors = self._start(
            _script(self.workspace / "ready", marker), grace=0.4, term=0.2)
        task, receipt = self._finish(task, errors)
        self.assertEqual(task["status"], "cancelled", task)
        self.assertEqual(receipt["status"], "cancelled", receipt)
        self.assertEqual(receipt.get("exit_code"), 0, receipt)
        self.assertEqual(marker.read_text(), "sigint")
        self.assertEqual(killed_alive, [])
        self.assertEqual(len(self._sigs(signals, signal.SIGINT)), 1)
        self.assertEqual(self._sigs(signals, signal.SIGTERM), [])
        self.assertEqual(receipt.get("termination_evidence"), "pgroup-empty")
        # No field already records graceful-vs-escalated. exit_code is the
        # existing wait status (0: the handler exited). Do not invent one.
        self.assertNotIn("termination_reason", receipt)

    def test_ignored_signals_escalate_to_sigkill(self):
        # (b) grace is patched down. The group is empty only after SIGKILL.
        task, signals, killed_alive, errors = self._start(
            _script(self.workspace / "ready"), grace=0.3, term=0.2)
        task, receipt = self._finish(task, errors)
        self.assertEqual(task["status"], "cancelled", task)
        self.assertEqual(receipt.get("exit_code"), -signal.SIGKILL, receipt)
        self.assertEqual(len(self._sigs(signals, signal.SIGINT)), 1)
        self.assertEqual(len(self._sigs(signals, signal.SIGTERM)), 1)
        self.assertEqual(killed_alive, [signals[0][2]])
        sigint_at = self._sigs(signals, signal.SIGINT)[0][1]
        sigterm_at = self._sigs(signals, signal.SIGTERM)[0][1]
        sigkill_at = self._sigs(signals, signal.SIGKILL)[0][1]
        self.assertLess(sigint_at, sigterm_at)
        self.assertLess(sigterm_at, sigkill_at)
        self.assertGreaterEqual(sigterm_at - sigint_at, 0.25)
        self.assertEqual(receipt.get("termination_evidence"), "pgroup-empty")
        self.assertIs(receipt.get("process_tree_confirmed"), True)
        started = runner.read_json_bounded(self.directory / "started.json")
        self.assertIs(runner.group_alive(started["pid"]), False)

    def test_second_cancel_does_not_reset_or_repeat(self):
        # (c) the second request lands during the grace. Escalation still
        # happens once, on the original clock.
        task, signals, killed_alive, errors = self._start(
            _script(self.workspace / "ready"), grace=1.0, term=0.2)
        task["status"] = "running"
        self.app.store.put("task", task)
        self.app.cancel(task["id"])
        time.sleep(0.4)
        self.assertFalse(runner.request_cancel(self.directory))
        self.thread.join(15)
        self.assertFalse(self.thread.is_alive(), "supervisor did not finish")
        self.assertEqual(errors, [])
        receipt = runner.inspect(self.directory)
        self.app.reconcile(task["id"])
        task = self.app.store.get("task", task["id"])
        self.assertEqual(task["status"], "cancelled", task)
        self.assertEqual(len(self._sigs(signals, signal.SIGINT)), 1, signals)
        self.assertEqual(len(self._sigs(signals, signal.SIGTERM)), 1, signals)
        self.assertEqual(killed_alive, [self._sigs(signals, signal.SIGINT)[0][2]])
        delta = self._sigs(signals, signal.SIGTERM)[0][1] - self._sigs(signals, signal.SIGINT)[0][1]
        # One grace (1.0s), not a clock restarted by the cancel 0.4s in.
        self.assertGreaterEqual(delta, 0.85)
        self.assertLess(delta, 1.25)
        self.assertEqual(receipt.get("exit_code"), -signal.SIGKILL, receipt)
        self.assertEqual(receipt.get("termination_evidence"), "pgroup-empty")

    def test_restarted_supervise_does_not_adopt_and_owner_still_kills(self):
        task, signals, killed_alive, errors = self._start(
            _script(self.workspace / "ready"), grace=0.6, term=0.2)
        task, receipt = self._finish(task, errors, again=True)
        self.assertEqual(task["status"], "cancelled", task)
        self.assertEqual(len(self._sigs(signals, signal.SIGINT)), 1, signals)
        self.assertEqual(len(self._sigs(signals, signal.SIGTERM)), 1, signals)
        self.assertEqual(killed_alive, [self._sigs(signals, signal.SIGINT)[0][2]])
        self.assertEqual(receipt.get("termination_evidence"), "pgroup-empty")
        pids = {item[2] for item in signals}
        started = runner.read_json_bounded(self.directory / "started.json")
        self.assertEqual(pids, {started["pid"]})


class CancelFileTests(unittest.TestCase):
    def test_second_cancel_leaves_the_file_unchanged(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        self.assertTrue(runner.request_cancel(directory))
        first = (directory / "cancel").stat().st_mtime_ns
        time.sleep(0.02)
        self.assertFalse(runner.request_cancel(directory))
        self.assertEqual((directory / "cancel").stat().st_mtime_ns, first)
        if os.name != "nt":  # Windows has no POSIX permission bits; ACLs govern
            self.assertEqual((directory / "cancel").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
