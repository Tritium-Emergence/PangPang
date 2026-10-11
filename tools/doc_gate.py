"""Run the validation commands published in README.md, in that order.

The unittest command is the documented clean-environment gate. Test modules
must load under that interpreter without a separate pytest install.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# These strings are the README.md Validation block. RUNBOOK repeats them.
NEEDLES = (
    "python -m pip install -r requirements.txt",
    "npm ci --ignore-scripts",
    'python -m unittest discover -s tests -p "test_*.py"',
)

COMMANDS = [
    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
    ["npm", "ci", "--ignore-scripts"],
    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
]


def main():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in NEEDLES:
        if needle not in readme:
            sys.stderr.write("README validation block is missing: %s\n" % needle)
            return 2
    for command in COMMANDS:
        print("+ " + " ".join(command), flush=True)
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
