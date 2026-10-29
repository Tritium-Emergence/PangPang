# -*- coding: utf-8 -*-
""": a mid-run inject with Chinese text failed on a cp936/GBK Windows host with
runner_rejected_request and no injects.jsonl. Root cause: execution._run sends the
payload as UTF-8 bytes, but the runner's inject action read sys.stdin with the system
locale; under GBK the Chinese text decoded into surrogates and append_inject's
text.encode() raised, so the runner subprocess exited non-zero before writing the
mailbox. The fix reads stdin as UTF-8 regardless of locale. This reproduces the
transport boundary with a real runner subprocess — no executor CLI needed."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pangpang import runner
from pangpang.store import encode


def _run_inject(directory, payload, extra_env=None):
    """Invoke `runner.py inject <dir>` exactly as execution._run does: UTF-8 bytes on
    stdin. extra_env lets a test force a non-UTF-8 stdio locale (the Windows cp936
    condition) on any platform."""
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None) # don't let UTF-8 mode mask a locale-decode bug
    env.pop("PYTHONIOENCODING", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, runner.__file__, "inject", str(directory)],
        input=encode(payload).encode("utf-8"),
        capture_output=True, env=env, timeout=60,
)
    return proc


class InjectEncodingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _payload(self, text):
        return {"uuid": str(uuid.uuid4()), "text": text}

    def _assert_landed(self, proc, text):
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        mailbox = self.dir / "injects.jsonl"
        self.assertTrue(mailbox.is_file(), "injects.jsonl must be written")
        rows = [json.loads(line) for line in mailbox.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(r.get("op") == "user" and r.get("text") == text for r in rows), rows)

    def test_chinese_inject_lands_under_gbk_locale(self):
        # The condition: a Chinese Windows host runs the inject subprocess with a
        # GBK stdio locale. The payload is UTF-8 on the wire; the runner must not decode
        # it with the locale codec.
        text = "中途追加：后半段改成写三首关于备份的俳句，不要取消当前任务。"
        proc = _run_inject(self.dir, self._payload(text), {"PYTHONIOENCODING": "gbk"})
        self._assert_landed(proc, text)

    def test_chinese_inject_lands_default_locale(self):
        text = "删除草稿箱里的所有文章，保留最新一篇。"
        self._assert_landed(_run_inject(self.dir, self._payload(text)), text)


if __name__ == "__main__":
    unittest.main()
