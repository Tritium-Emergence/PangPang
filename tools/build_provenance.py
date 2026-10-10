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
    if not commit:
        if require:
            raise SystemExit("PANGPANG_REQUIRE_BUILD_PROVENANCE=1 but git provenance is unavailable")
        commit = "unknown"
    return {"BUILD_COMMIT": commit,
            "BUILT_AT": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def render(info):
    return "BUILD_COMMIT = %r\nBUILT_AT = %r\n" % (info["BUILD_COMMIT"], info["BUILT_AT"])


def write_to(directory, info=None):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "_build.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render(info or collect_provenance()))
    return path
