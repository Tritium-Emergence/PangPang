"""Rewrite dist/SHA256SUMS over the shipped release artifacts.

Two-space format for `sha256sum -c`. The platform-neutral source zip is
excluded on purpose: rollback and mirror verification compare the per-OS
artifacts, and the source zip duplicates one of them by design.

    python tools/sha256sums.py
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release


def main():
    dist = build_release.root / "dist"
    source_zip = "PangPang-%s.zip" % build_release.read_version()
    files = sorted(p for p in dist.iterdir()
                   if p.suffix in (".zip", ".whl", ".gz") and p.name != source_zip)
    if not files:
        raise SystemExit("no artifacts found in " + str(dist))
    lines = [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name for p in files]
    with open(dist / "SHA256SUMS", "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    print(len(files), "artifacts")


if __name__ == "__main__":
    main()
