"""Generate ``pangpang/_build.py`` from the build input (git HEAD).

Release builds set PANGPANG_REQUIRE_BUILD_PROVENANCE=1 and fail closed when
git provenance is unavailable, the clean-tree check itself fails to run, or
the source tree carries modified tracked or untracked files; developer
builds fall back to the explicit string "unknown". Git is always queried at
the pinned source root, never at the caller's working directory, and strict
builds additionally require that root to be the repository root itself.
A version tag pointing at HEAD (optional leading ``v``) must match
``pangpang.__version__``. The runtime never shells out to git — provenance
is baked in at build time only.
"""
import datetime
import os
import re
import subprocess

SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(root, *args):
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return ""


def _git_required(root, *args):
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        raise SystemExit("release builds cannot verify the source tree: git %s failed"
                         % " ".join(args))


def _package_version(root):
    """``pangpang.__version__`` from source, or None when that file is absent."""
    path = os.path.join(root, "pangpang", "__init__.py")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    match = re.search(r"(?m)^__version__\s*=\s*['\"]([^'\"]+)['\"]\s*$", text)
    return match.group(1) if match else None


# Optional leading v, then a public version: N.N[.N]*[{a|b|rc}N][.postN][.devN].
_VERSION_TAG = re.compile(
    r"^v?(?P<version>\d+\.\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?)$")


def _version_tag_name(tag):
    match = _VERSION_TAG.fullmatch(tag.strip())
    return match.group("version") if match else None


def _refuse_mismatched_version_tags(root):
    """Strict builds: every version tag at HEAD matches the package version."""
    declared = _package_version(root)
    mismatches = []
    for tag in _git_required(root, "tag", "--points-at", "HEAD").splitlines():
        parsed = _version_tag_name(tag)
        if parsed is None:
            continue
        if parsed != declared:
            mismatches.append("%s (%s)" % (tag.strip(), parsed))
    if mismatches:
        raise SystemExit(
            "release builds require every version tag at HEAD to match the package "
            "version %s; mismatched: %s"
            % (declared if declared is not None else "unreadable", ", ".join(mismatches)))


def collect_provenance(require=None, root=None, now=None):
    if require is None:
        require = os.environ.get("PANGPANG_REQUIRE_BUILD_PROVENANCE") == "1"
    root = root or SOURCE_ROOT
    commit = _git(root, "rev-parse", "HEAD")
    ref = _git(root, "describe", "--tags", "--always", "--dirty")
    if not commit:
        if require:
            raise SystemExit("PANGPANG_REQUIRE_BUILD_PROVENANCE=1 but git provenance is unavailable")
        commit = "unknown"
    if require:
        toplevel = _git_required(root, "rev-parse", "--show-toplevel")
        if os.path.realpath(toplevel) != os.path.realpath(root):
            raise SystemExit("release builds require the source root to be the git repository root; "
                             "git toplevel is %s — a parent repository would bake in the wrong "
                             "commit and hide ignored sources" % toplevel)
        if _git_required(root, "status", "--porcelain", "--untracked-files=all"):
            raise SystemExit("release builds require a clean tracked tree without untracked "
                             "sources; git status --porcelain -uall is not empty")
        _refuse_mismatched_version_tags(root)
    # UTC fields from an injectable clock. time.strftime is locale-sensitive and
    # a live wall clock makes equality checks flake across a second boundary.
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    return {"BUILD_COMMIT": commit,
            "BUILD_REF": ref or "unknown",
            "BUILT_AT": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


def render(info):
    return "BUILD_COMMIT = %r\nBUILD_REF = %r\nBUILT_AT = %r\n" % (
        info["BUILD_COMMIT"], info.get("BUILD_REF", "unknown"), info["BUILT_AT"])


def write_to(directory, info=None):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "_build.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render(info or collect_provenance()))
    return path
