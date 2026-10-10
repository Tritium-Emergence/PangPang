"""Regression tests for the multi-task orchestration semantics behind the
manual A/B split (issue #9): a dependent chain must complete with real
artifacts and a real handoff; cancelling one of two concurrent independent
tasks must not disturb the other; and tasks that share a workspace (or one
installation at max_concurrency) serialize by design instead of deadlocking.
These encode the product guarantees the 600s manual window kept conflating
with "parallel fan-out", using fixture executors only."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang.service import Service


def _stub(body):
    return [sys.executable, "-c", body]


class OrchestrationFanoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.store.close()
        self.tmp.cleanup()

    def _boot(self, cfg):
        self.app = Service(cfg, self.root.parent / "data")
        for installation_id in self.app.execution.installations:
            self.app.probe(installation_id)
        return self.app

    def _base_cfg(self):
        return config(self.root)

    def _pair_cfg(self, cwd_a, cwd_b):
        cfg = self._base_cfg()
        base = cfg["installations"][0]
        cfg["installations"] = [dict(base, id="inst-a", cwd=str(cwd_a)),
                                dict(base, id="inst-b", cwd=str(cwd_b))]
        return cfg

    def _argv(self, installation_id, body):
        self.app.execution.installations[installation_id]["argv"] = _stub(body)

    def _submit(self, tag, prompt, *, installation_id=None, outputs=(), refs=()):
        body = {"prompt": prompt}
        if outputs:
            body["outputs"] = list(outputs)
        if refs:
            body["artifact_refs"] = list(refs)
        if installation_id:
            body["installation_id"] = installation_id
        m = self.app.send("bot-main", {"text": prompt, "allow_write": True}, tag)
        return self.app.submit_task("bot-main", m["work_id"], body)["task_id"]

    def _drain(self, task_ids, timeout=30.0):
        deadline = time.monotonic() + timeout
        terminal = {"succeeded", "failed", "cancelled"}
        while time.monotonic() < deadline:
            self.app.task_step()
            rows = [self.app.store.get("task", tid) for tid in task_ids]
            if all(row["status"] in terminal for row in rows):
                return rows
            time.sleep(0.02)
        self.fail("tasks did not settle: " + str(
            [dict(self.app.store.get("task", tid)) for tid in task_ids]))

    def _statuses(self, task_ids):
        return [self.app.store.get("task", tid)["status"] for tid in task_ids]

    def test_dependent_chain_completes_with_real_artifacts_and_handoff(self):
        self._boot(self._base_cfg())
        self._argv("codex-local",
                   "import pathlib; pathlib.Path('research.txt').write_text('RESEARCH'); print('research done')")
        a = self._submit("chain-a", "research", outputs=["research.txt"])
        self.assertEqual(self._drain([a])[0]["status"], "succeeded")
        art_a = self.app.store.find("artifact", task_id=a)[0]
        self.assertEqual((self.root / "research.txt").read_text(), "RESEARCH")

        self._argv("codex-local",
                   "import pathlib; pathlib.Path('guide.txt').write_text(pathlib.Path('research.txt').read_text() + '|GUIDE'); print('guide done')")
        b = self._submit("chain-b", "guide", outputs=["guide.txt"], refs=[art_a["id"]])
        self.assertEqual(self._drain([b])[0]["status"], "succeeded")
        task_b = self.app.store.get("task", b)
        self.assertEqual(task_b["inputs"],
                         [{"artifact_id": art_a["id"], "path": art_a["path"], "digest": art_a["digest"]}])
        art_b = self.app.store.find("artifact", task_id=b)[0]
        self.assertEqual((self.root / "guide.txt").read_text(), "RESEARCH|GUIDE")

        self._argv("codex-local",
                   "import pathlib; pathlib.Path('checklist.txt').write_text(pathlib.Path('guide.txt').read_text() + '|CHECK'); print('checklist done')")
        c = self._submit("chain-c", "checklist", outputs=["checklist.txt"], refs=[art_b["id"]])
        self.assertEqual(self._drain([c])[0]["status"], "succeeded")
        self.assertEqual((self.root / "checklist.txt").read_text(), "RESEARCH|GUIDE|CHECK")

        self.assertEqual(len(self.app.store.find("artifact")), 3)
        for tid in (a, b, c):
            task = self.app.store.get("task", tid)
            echoes = [m for m in self.app.store.find("message", work_id=task["work_id"])
                      if m.get("kind") == "execution_result"]
            self.assertTrue(echoes, "join never echoed back into work " + task["work_id"])

    def test_three_tasks_queued_at_once_drain_serially_on_one_installation(self):
        self._boot(self._base_cfg())
        self._argv("codex-local",
                   "import time, pathlib; time.sleep(0.15); pathlib.Path('result.txt').write_text('done'); print('ok')")
        ids = [self._submit("fan-%d" % i, "p%d" % i) for i in range(3)]
        self.app.task_step()
        statuses = self._statuses(ids)
        self.assertNotEqual(statuses[0], "queued")
        self.assertEqual(statuses[1:], ["queued", "queued"])
        deadline = time.monotonic() + 30
        terminal = {"succeeded", "failed", "cancelled"}
        max_concurrent = 0
        while time.monotonic() < deadline:
            self.app.task_step()
            rows = [self.app.store.get("task", tid) for tid in ids]
            max_concurrent = max(max_concurrent,
                                 sum(1 for r in rows if r["status"] in {"starting", "running"}))
            if all(r["status"] in terminal for r in rows):
                break
            time.sleep(0.02)
        else:
            self.fail("fan-out did not drain")
        self.assertEqual([r["status"] for r in rows], ["succeeded", "succeeded", "succeeded"])
        self.assertEqual(max_concurrent, 1)

    def test_cancel_one_of_two_concurrent_tasks_leaves_the_other_running(self):
        a_dir = self.root / "a"
        b_dir = self.root / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        self._boot(self._pair_cfg(a_dir, b_dir))
        self._argv("inst-b", "import time; time.sleep(60)")
        self._argv("inst-a",
                   "import time, pathlib; time.sleep(0.8); pathlib.Path('result.txt').write_text('done'); print('ok')")
        victim = self._submit("iso-victim", "long", installation_id="inst-b")
        survivor = self._submit("iso-survivor", "short", installation_id="inst-a")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.app.task_step()
            if self._statuses([victim, survivor]) == ["running", "running"]:
                break
            time.sleep(0.02)
        self.assertEqual(self._statuses([victim, survivor]), ["running", "running"],
                         "independent tasks on distinct installations must run concurrently")
        self.app.cancel(victim)
        rows = {r["id"]: r for r in self._drain([victim, survivor])}
        self.assertEqual(rows[victim]["status"], "cancelled")
        self.assertTrue(rows[victim]["receipt"]["process_tree_confirmed"])
        self.assertEqual(rows[survivor]["status"], "succeeded")
        self.assertTrue((a_dir / "result.txt").exists())
        self.assertFalse((b_dir / "result.txt").exists())
        self.assertEqual(self.app.store.find("artifact", task_id=victim), [])

    def test_shared_workspace_serializes_across_installations(self):
        self._boot(self._pair_cfg(self.root, self.root))
        self._argv("inst-a",
                   "import time, pathlib; time.sleep(0.4); pathlib.Path('x.txt').write_text('a'); print('a')")
        self._argv("inst-b", "import pathlib; pathlib.Path('y.txt').write_text('b'); print('b')")
        first = self._submit("ws-a", "a", installation_id="inst-a", outputs=["x.txt"])
        second = self._submit("ws-b", "b", installation_id="inst-b", outputs=["y.txt"])
        self.app.task_step()
        self.app.task_step()
        self.assertEqual(self._statuses([first, second]), ["running", "queued"],
                         "a second writer on the same workspace must wait, not run concurrently")
        rows = self._drain([first, second])
        self.assertEqual([r["status"] for r in rows], ["succeeded", "succeeded"])


if __name__ == "__main__":
    unittest.main()
