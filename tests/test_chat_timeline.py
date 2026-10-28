import os
import subprocess
import unittest
from pathlib import Path


class ChatTimelineTests(unittest.TestCase):
    def test_setup_result_is_omitted_from_the_timeline(self):
        env = os.environ.copy()
        env.pop("TYPESAFE_API_KEY", None)
        proc = subprocess.run(
            ["node", "--experimental-strip-types", "--test", "tests/test_chat_timeline.mjs"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)


if __name__ == "__main__":
    unittest.main()
