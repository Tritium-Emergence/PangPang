"""IPC frames are one JSON value per line.

The pi worker reads stdin with Node readline. That reader splits on U+2028
and U+2029 as well as CR/LF. Those characters are escaped on the stdin frame
only. store.encode() and digest() keep them raw, matching bytes already stored.
"""
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from pangpang.pi_runtime import _ipc_line
from pangpang.store import (Store, digest, encode, launch_contract,
                            verify_launch_contract)

# Node readline line endings (lib/internal/readline/interface.js):
# /\r?\n|\r(?!\n)|\u2028|\u2029/
_READLINE_END = re.compile(r"\r\n|\r|\n|\u2028|\u2029")


def readline_lines(wire):
    parts = _READLINE_END.split(wire)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def _base_encode(value):
    """Canonical JSON with U+2028 and U+2029 left raw, matching store.encode()."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _base_digest(value):
    return hashlib.sha256(_base_encode(value).encode()).hexdigest()


class PiWorkerIpcEncodeTests(unittest.TestCase):
    def test_line_separators_survive_readline_framing(self):
        content = "hello\u2028world\u2029"
        payload = {
            "version": 1,
            "history": [
                {"role": "system", "content": "goal\u2028"},
                {"role": "user", "content": content},
            ],
            "note": "already\\u2028 escaped text \u2028 and a\nreal newline",
            "key\u2029": "value\u2028",
        }
        wire = _ipc_line(payload)
        self.assertNotIn("\u2028", wire)
        self.assertNotIn("\u2029", wire)
        self.assertIn("\\u2028", wire)
        self.assertIn("\\u2029", wire)
        self.assertTrue(wire.endswith("\n"))
        lines = readline_lines(wire)
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["history"][1]["content"], content)
        self.assertEqual(parsed["history"][0]["content"], "goal\u2028")
        self.assertEqual(parsed["key\u2029"], "value\u2028")
        self.assertIn("\\u2028", parsed["note"])
        self.assertIn("\u2028", parsed["note"])
        self.assertIn("\n", parsed["note"])
        self.assertIn("\u2028", encode(payload))
        self.assertEqual(encode(payload), _base_encode(payload))

    def test_each_separator_is_one_frame(self):
        for char, escape in (("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
            wire = _ipc_line({"content": "a" + char + "b"})
            self.assertNotIn(char, wire)
            self.assertIn(escape, wire)
            lines = readline_lines(wire)
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["content"], "a" + char + "b")

    def test_two_frames_stay_two_lines(self):
        first = {"id": 1, "result": {"history": [{"content": "x\u2028y"}]}}
        second = {"id": 2, "result": {"history": [{"content": "p\u2029q"}]}}
        wire = _ipc_line(first) + _ipc_line(second)
        lines = readline_lines(wire)
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["result"]["history"][0]["content"], "x\u2028y")
        self.assertEqual(json.loads(lines[1])["result"]["history"][0]["content"], "p\u2029q")

    def test_ordinary_text_encoding_unchanged(self):
        self.assertEqual(encode({"b": 1, "a": "hello\nworld"}), '{"a":"hello\\nworld","b":1}')
        self.assertEqual(_ipc_line({"b": 1, "a": "hello\nworld"}), '{"a":"hello\\nworld","b":1}\n')
        self.assertEqual(encode({"text": "中文"}), '{"text":"中文"}')

    def test_raw_dumps_is_split_by_readline(self):
        raw = json.dumps({"a": "hello\u2028world"}, ensure_ascii=False) + "\n"
        lines = readline_lines(raw)
        self.assertGreater(len(lines), 1)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(lines[0])


class PersistedSeparatorTests(unittest.TestCase):
    def test_base_dedup_row_replays_under_current_digest(self):
        payload = {"note": "line\u2028para\u2029"}
        result = {"echo": "a\u2028b"}
        self.assertEqual(encode(payload), _base_encode(payload))
        self.assertEqual(digest(payload), _base_digest(payload))
        self.assertIn("\u2028", _base_encode(payload))
        tmp = tempfile.TemporaryDirectory()
        try:
            path = str(Path(tmp.name) / "state.db")
            writer = Store(path)
            writer.db.execute(
                "INSERT INTO dedup VALUES(?,?,?,?)",
                ("scope", "key-1", _base_digest(payload), _base_encode(result)))
            writer.close()
            reader = Store(path)
            try:
                self.assertEqual(reader.once_lookup("scope", "key-1", payload), result)
                called = []
                self.assertEqual(
                    reader.once("scope", "key-1", payload, lambda: called.append(1)), result)
                self.assertEqual(called, [])
                self.assertEqual(
                    reader.journal_once("scope", "key-1", payload, {"fresh": True}), result)
            finally:
                reader.close()
        finally:
            tmp.cleanup()

    def test_base_contract_digest_still_verifies(self):
        task = {"execution_id": "exec-" + "a" * 32, "installation_id": "codex-local",
                "cwd": "/repo", "timeout_s": 900, "access_mode": "write",
                "workspace_key": ["h", ["repo"]], "inputs": [], "outputs": ["out.txt"],
                "resource_keys": [], "prompt": "run\u2028now\u2029", "attempt_uuid": "att",
                "policy_digest": "pd"}
        contract = launch_contract(task)
        task["contract"] = contract
        task["contract_digest"] = _base_digest(contract)
        task["min_contract_version"] = contract["contract_version"]
        self.assertIn("\u2028", _base_encode(contract))
        self.assertEqual(digest(contract), _base_digest(contract))
        self.assertIsNone(verify_launch_contract(task))


if __name__ == "__main__":
    unittest.main()
