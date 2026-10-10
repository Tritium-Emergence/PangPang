"""Migration shim for the pre-GA ``XIAN_PANGPANG_*`` environment prefix.

The GA rename replaced the ``XIAN_`` prefix (``XIAN_PANGPANG_TOKEN`` ->
``PANGPANG_TOKEN``). A legacy deployment that still exports only the old
names must keep working for one release cycle, while a present new name
always wins — even when its value is rejected — so a partial rename can
never silently downgrade the effective secret (fail-closed).
"""
import logging
import os

MIGRATED_ENV_NAMES = (
    "PANGPANG_TOKEN",
    "PANGPANG_DEVICE_TOKEN",
    "PANGPANG_PI_WORKER",
    "PANGPANG_PI_NODE_BUNDLE",
    "PANGPANG_PI_BUN_BUNDLE",
    "PANGPANG_LOG_LEVEL",
    "PANGPANG_SEMANTIC_GATE_URL",
    "PANGPANG_SEARCH_PROVIDER",
)

_warned = set()


def legacy_name(name):
    """Return the pre-GA name for a current ``PANGPANG_*`` variable, else None."""
    if name.startswith("PANGPANG_"):
        return "XIAN_" + name
    return None


def _warn_legacy(name, legacy):
    if name in _warned:
        return
    _warned.add(name)
    logging.getLogger("pangpang").getChild("env").warning(
        "%s is deprecated; rename it to %s (values are never logged)", legacy, name)


def has_env(name):
    """True when the current or legacy name is present in the environment."""
    if name in os.environ:
        return True
    legacy = legacy_name(name)
    if legacy is not None and legacy in os.environ:
        _warn_legacy(name, legacy)
        return True
    return False


def env_value(name, default=""):
    """Read one config variable: new name first, legacy name as a deprecated
    fallback, default otherwise. A present new name is returned as-is even
    when invalid; rejecting it is the caller's job (fail-closed)."""
    if name in os.environ:
        return os.environ[name]
    legacy = legacy_name(name)
    if legacy is not None and legacy in os.environ:
        _warn_legacy(name, legacy)
        return os.environ[legacy]
    return default


def with_legacy_names(names):
    """Project a collection of variable names to also cover their legacy
    spellings, so redaction can't be bypassed via the old prefix."""
    out = set()
    for name in names:
        out.add(name)
        legacy = legacy_name(name)
        if legacy is not None:
            out.add(legacy)
    return out
