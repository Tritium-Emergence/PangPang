"""Build provenance projection. The setuptools build hook writes
``pangpang/_build.py`` into the built artifact; a plain source checkout has
no such file and reports ``unknown`` explicitly (never a guessed commit)."""
from . import __version__


def build_identity():
    try:
        from . import _build
    except ImportError:
        return "unknown", "unknown", "unknown"
    return (str(getattr(_build, "BUILD_COMMIT", "") or "unknown"),
            str(getattr(_build, "BUILD_REF", "") or "unknown"),
            str(getattr(_build, "BUILT_AT", "") or "unknown"))


def version_string():
    commit, ref, built_at = build_identity()
    if commit == "unknown":
        return __version__ + " (build unknown)"
    if ref != "unknown":
        return "%s (build %s @ %s, built %s)" % (__version__, commit[:7], ref, built_at)
    return "%s (build %s, built %s)" % (__version__, commit[:7], built_at)


def build_line():
    commit, ref, built_at = build_identity()
    if ref != "unknown":
        return "build: %s ref=%s (built %s)\n" % (commit, ref, built_at)
    return "build: %s (built %s)\n" % (commit, built_at)
