"""Public samples and catalog hints stay neutral and still describe the executor."""
import json
import re
import unittest
from pathlib import Path

from pangpang.execution import ADAPTER_CATALOG
from pangpang.model_config import WINDOW_FIT_OUTPUT_TOKENS

ROOT = Path(__file__).resolve().parents[1]
_CJK = re.compile(r"[\u4e00-\u9fff]")
_PRIVATE = ("伍定远", "胖胖", "linux-gpu", "win-a", "win-b", "mac-a", "mac-b",
            "qwen-shared", "cloud-small")


class PublicSampleTests(unittest.TestCase):
    def test_catalog_use_when_is_a_public_capability_hint(self):
        expected = {
            "claude-code": "Edit code and local files.",
            "codex": "image_gen",
            "gemini": "Tool confirmations are not approved automatically.",
            "pi": "Cannot run commands.",
            "acp": "ACP session:",
        }
        self.assertEqual(set(expected), set(ADAPTER_CATALOG))
        for name, needle in expected.items():
            text = ADAPTER_CATALOG[name]["use_when"]
            self.assertIn(needle, text)
            self.assertNotRegex(text, _CJK)
            self.assertNotIn("跑构建", text)
            self.assertTrue(text.strip())

    def test_example_files_are_minimal_public_placeholders(self):
        persons = json.loads((ROOT / "config" / "persons.example.json").read_text(encoding="utf-8"))
        runtime = json.loads((ROOT / "config" / "runtime.example.json").read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in persons], ["supervisor", "local-executor"])
        self.assertEqual(persons[0]["name"], "Supervisor")
        self.assertEqual([host["id"] for host in runtime["hosts"]], ["local"])
        self.assertEqual(runtime["models"][0]["output_tokens"], WINDOW_FIT_OUTPUT_TOKENS)
        self.assertEqual(runtime["models"][0]["context_budget"], 32000)
        self.assertEqual(runtime["resources"], [])
        self.assertEqual(runtime["turn_budget"], 8)
        blob = json.dumps({"persons": persons, "runtime": runtime}, ensure_ascii=False)
        for private in _PRIVATE:
            self.assertNotIn(private, blob)


if __name__ == "__main__":
    unittest.main()
