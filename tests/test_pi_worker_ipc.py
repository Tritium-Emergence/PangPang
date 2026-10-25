"""IPC frames are one JSON value per line.

The pi worker reads stdin with Node readline. That reader splits on U+2028
and U+2029 as well as CR/LF, so encode() must escape those characters inside
strings. Decoding still returns the original characters.
"""
import json
import re
import unittest

from pangpang.store import encode

# Node readline line endings (lib/internal/readline/interface.js):
# /\r?\n|\r(?!\n)|\u2028|\u2029/
_READLINE_END = re.compile(r"\r\n|\r|\n|\u2028|\u2029")


def readline_lines(wire):
    parts = _READLINE_END.split(wire)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


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
        wire = encode(payload) + "\n"
        self.assertNotIn("\u2028", wire)
        self.assertNotIn("\u2029", wire)
        self.assertIn("\\u2028", wire)
        self.assertIn("\\u2029", wire)
        lines = readline_lines(wire)
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["history"][1]["content"], content)
        self.assertEqual(parsed["history"][0]["content"], "goal\u2028")
        self.assertEqual(parsed["key\u2029"], "value\u2028")
        self.assertIn("\\u2028", parsed["note"])
        self.assertIn("\u2028", parsed["note"])
        self.assertIn("\n", parsed["note"])

    def test_each_separator_is_one_frame(self):
        for char, escape in (("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
            wire = encode({"content": "a" + char + "b"}) + "\n"
            self.assertNotIn(char, wire)
            self.assertIn(escape, wire)
            lines = readline_lines(wire)
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["content"], "a" + char + "b")

    def test_two_frames_stay_two_lines(self):
        first = {"id": 1, "result": {"history": [{"content": "x\u2028y"}]}}
        second = {"id": 2, "result": {"history": [{"content": "p\u2029q"}]}}
        wire = encode(first) + "\n" + encode(second) + "\n"
        lines = readline_lines(wire)
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["result"]["history"][0]["content"], "x\u2028y")
        self.assertEqual(json.loads(lines[1])["result"]["history"][0]["content"], "p\u2029q")

    def test_ordinary_text_encoding_unchanged(self):
        self.assertEqual(encode({"b": 1, "a": "hello\nworld"}), '{"a":"hello\\nworld","b":1}')
        self.assertEqual(encode({"text": "中文"}), '{"text":"中文"}')

    def test_raw_dumps_is_split_by_readline(self):
        raw = json.dumps({"a": "hello\u2028world"}, ensure_ascii=False) + "\n"
        lines = readline_lines(raw)
        self.assertGreater(len(lines), 1)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(lines[0])


if __name__ == "__main__":
    unittest.main()
