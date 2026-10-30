"""Deterministic release bundles; explicit allowlist excludes credentials/state.

    python tools/build_release.py                 # platform-neutral source ZIP
    python tools/build_release.py --with-runtime  # that ZIP, plus per-OS wheels and zips
                                                  # that ship hash-pinned Bun and pi

Artifacts are built only from Git-tracked files of a clean checkout (never from
untracked local files) and pass the hygiene scan on the exact bytes written;
any hit fails the build and names the file and rule. The scan blocks runtime/
credential files, private-key blocks, private home paths, private-network
addresses and common credential prefixes; it is not a general secret scanner,
so a reviewed source tree plus a secret review remain the release precondition.
(split: snapshot export and the leaked-identifier denylist live in the
private one-off exporter, not here.)
Version single source of truth: ``pangpang/__init__.py::__version__``.
"""
import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]

ALLOWED = ["README.md", "README.zh.md", "SECURITY.md", "LICENSE", "NOTICE",
           "TRADEMARKS.md", "CONTRIBUTING.md", "THIRD_PARTY_NOTICES",
           "launch.py", "requirements.txt", "package.json", "package-lock.json",
           "pangpang", "deploy", "config/runtime.example.json", "config/connector.example.json",
           # core docs only (+ owner 2026-10-08); process/status docs stay private
           "docs/INDEX.md", "docs/REQ.md", "docs/SPEC.md", "docs/BOT-AGENT-ARCHITECTURE.md",
           "docs/RUNBOOK.md", "docs/MODEL-PROVIDERS.md", "docs/attention-contract.md", "docs/INSTALL.md",
           "docs/firstrun-setup-contract.md", "docs/SPEC-work-continuity-v3.md",
           "docs/SPEC-perf-exec-v2.md", "docs/SPEC-durable-work-v1.md",
           # license texts referenced by THIRD_PARTY_NOTICES for code that
           # ships inside the web bundle / worker runtime
           "tools/third_party"]
FORBIDDEN_NAMES = {"master.key", "secrets.db", "bots.db", "connector.db"}
FORBIDDEN_DIRS = {"cloud-data", "connector-data", "connector-data-verify", "data-v2", "vault"}
FORBIDDEN_SUFFIXES = {".db", ".key", ".pem", ".pyc", ".env"}

HOME_PATH = re.compile(rb"(?:/home/|/Users/|[A-Za-z]:[\\/]+Users[\\/]+)[A-Za-z0-9._-]+")
PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")
PRIVATE_IP = re.compile(rb"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))\.\d{1,3}\.\d{1,3}\b")
KEY_PREFIX = re.compile(rb"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                        rb"|AKIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")
JWT = re.compile(rb"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b")


def read_version():
    text = (root / "pangpang" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "(\d+\.\d+\.\d+)"$', text, re.M)
    if not match:
        raise SystemExit("pangpang/__init__.py must define __version__ = \"X.Y.Z\"")
    return match.group(1)


def unsafe_path(rel):
    # casefolded: Windows checkouts are case-insensitive, so SECRETS.DB or
    #.ENV resolve to the same file as secrets.db /.env and must be caught.
    rel = Path(rel)
    name = rel.name.casefold()
    return (rel.suffix.casefold() in FORBIDDEN_SUFFIXES or name in FORBIDDEN_NAMES
            or name == ".env" or name.startswith(".env.")
            or bool(FORBIDDEN_DIRS & {p.casefold() for p in rel.parts}))


def content_findings(rel, data):
    """Return rule names hit by one file's bytes (empty list == clean)."""
    hits = []
    if HOME_PATH.search(data):
        hits.append("home-directory path")
    if PRIVATE_KEY.search(data):
        hits.append("private key block")
    if PRIVATE_IP.search(data):
        hits.append("private-network address")
    if KEY_PREFIX.search(data):
        hits.append("credential prefix")
    if JWT.search(data):
        hits.append("JWT token")
    return hits


def scan(entries):
    """entries: iterable of (relative posix path, bytes). Returns ["path: rule",...]."""
    problems = []
    for rel, data in entries:
        if unsafe_path(rel):
            problems.append(rel + ": runtime/credential file")
        problems.extend(rel + ": " + hit for hit in content_findings(rel, data))
    return problems


def git(*args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def tracked_files():
    return [f for f in git("ls-files", "-z").decode("utf-8").split("\0") if f]


def release_candidates():
    dirs = tuple(name + "/" for name in ALLOWED)
    return sorted(f for f in tracked_files() if f in ALLOWED or f.startswith(dirs))


def clean_checkout(files):
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise SystemExit("release build requires a clean checkout: commit or stash tracked changes first")
    links = [f for f in files if (root / f).is_symlink()]
    if links:
        raise SystemExit("symlink in release set: " + ", ".join(links))
    return files


def build(name, files, out=root / "dist"):
    entries = [(rel, (root / rel).read_bytes()) for rel in files]
    problems = scan(entries)
    if problems:
        raise SystemExit("release hygiene failed:\n " + "\n ".join(problems))
    out.mkdir(exist_ok=True)
    archive = out / (name + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in entries:
            info = zipfile.ZipInfo(name + "/" + rel, (2026, 9, 14, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, data)
    with zipfile.ZipFile(archive) as z:
        if any(unsafe_path(n.split("/", 1)[1]) for n in z.namelist()):
            archive.unlink(missing_ok=True)
            raise SystemExit("runtime state leaked into release archive")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    sums = out / "SHA256SUMS"
    lines = sums.read_text(encoding="utf-8").splitlines() if sums.exists() else []
    lines = sorted([l for l in lines if not l.endswith(" " + archive.name)] + [checksum + " " + archive.name])
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(archive.name, checksum)
    return archive


def main(argv):
    with_runtime = argv == ["--with-runtime"]
    if argv and not with_runtime:
        raise SystemExit(__doc__)
    build("PangPang-" + read_version(), clean_checkout(release_candidates()))
    if with_runtime:
        # Imported lazily so the source-zip path does not load the downloader.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import assemble_distribution
        assemble_distribution.assemble_all(require_clean=True)


if __name__ == "__main__":
    main(sys.argv[1:])
