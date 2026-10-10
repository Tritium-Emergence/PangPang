"""Generate ``pangpang/_build.py`` from the build input (git HEAD).

Release builds set PANGPANG_REQUIRE_BUILD_PROVENANCE=1 and fail closed when
git provenance is unavailable; developer builds fall back to the explicit
string "unknown". The runtime never shells out to git — provenance is baked
in at build time only.
"""
import os
import subprocess
import time


def collect_provenance(require=None):
    if require is None:
        require = os.environ.get("PANGPANG_REQUIRE_BUILD_PROVENANCE") == "1"
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=True).stdout.strip()
    except Exception:
        commit = ""
    try:
        ref = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        ref = ""
    if not commit:
        if require:
            raise SystemExit("PANGPANG_REQUIRE_BUILD_PROVENANCE=1 but git provenance is unavailable")
        commit = "unknown"
    if require and ref.endswith("-dirty"):
        raise SystemExit("release builds must come from a clean tree; "
                         "ref %r carries local modifications" % ref)
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
