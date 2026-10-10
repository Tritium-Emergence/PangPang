"""Build provenance projection. The setuptools build hook writes
``pangpang/_build.py`` into the built artifact; a plain source checkout has
no such file and reports ``unknown`` explicitly (never a guessed commit)."""
from . import __version__


def build_identity():
    try:
        from ._build import BUILT_AT, BUILD_COMMIT
    except ImportError:
        return "unknown", "unknown"
    return (str(BUILD_COMMIT) or "unknown", str(BUILT_AT) or "unknown")


def version_string():
    commit, built_at = build_identity()
    if commit == "unknown":
        return __version__ + " (build unknown)"
    return "%s (build %s, built %s)" % (__version__, commit[:7], built_at)


def build_line():
    commit, built_at = build_identity()
    return "build: %s (built %s)\n" % (commit, built_at)
