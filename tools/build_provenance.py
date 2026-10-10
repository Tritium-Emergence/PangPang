"""Generate ``pangpang/_build.py`` from the build input (git HEAD).

Release builds set PANGPANG_REQUIRE_BUILD_PROVENANCE=1 and fail closed when
git provenance is unavailable, the clean-tree check itself fails to run, or
the source tree carries modified tracked or untracked files; developer
builds fall back to the explicit string "unknown". Git is always queried at
the pinned source root, never at the caller's working directory. The runtime
never shells out to git — provenance is baked in at build time only.
"""
import os
import subprocess
import time

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


def collect_provenance(require=None, root=None):
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
        if _git_required(root, "status", "--porcelain", "--untracked-files=all"):
            raise SystemExit("release builds require a clean tracked tree without untracked "
                             "sources; git status --porcelain -uall is not empty")
    return {"BUILD_COMMIT": commit,
            "BUILD_REF": ref or "unknown",
            "BUILT_AT": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def render(info):
    return "BUILD_COMMIT = %r\nBUILD_REF = %r\nBUILT_AT = %r\n" % (
        info["BUILD_COMMIT"], info.get("BUILD_REF", "unknown"), info["BUILT_AT"])


def write_to(directory, info=None):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "_build.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render(info or collect_provenance()))
    return path
