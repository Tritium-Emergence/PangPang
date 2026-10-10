"""Migration shim for the pre-GA ``XIAN_PANGPANG_*`` environment prefix.

The GA rename replaced the ``XIAN_`` prefix (``XIAN_PANGPANG_TOKEN`` ->
``PANGPANG_TOKEN``). Only variables with verifiable pre-GA legacy usage are
listed in ``LEGACY_ENV_NAMES``; every other ``PANGPANG_*`` variable reads its
current name only — no mechanical prefix fallback, because inventing a legacy
contract for a variable that never had one could silently resurrect a stale
value (e.g. a runtime bundle selector) that no upgrade site ever set. A
present new name always wins — even when its value is empty or rejected —
so a partial rename can never silently downgrade the effective secret
(fail-closed).
"""
import logging
import os

LEGACY_ENV_NAMES = {
    "PANGPANG_TOKEN": "XIAN_PANGPANG_TOKEN",
    "PANGPANG_DEVICE_TOKEN": "XIAN_PANGPANG_DEVICE_TOKEN",
    "PANGPANG_PI_WORKER": "XIAN_PANGPANG_PI_WORKER",
    "PANGPANG_LOG_LEVEL": "XIAN_PANGPANG_LOG_LEVEL",
    "PANGPANG_SEMANTIC_GATE_URL": "XIAN_PANGPANG_SEMANTIC_GATE_URL",
}

MIGRATED_ENV_NAMES = tuple(LEGACY_ENV_NAMES)

_warned = set()


def legacy_name(name):
    """Return the verified pre-GA name for a current variable, else None."""
    return LEGACY_ENV_NAMES.get(name)


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
    fallback (only for variables with a verified legacy contract), default
    otherwise. A present new name is returned as-is even when empty or
    invalid — an explicit empty value is operator intent to unset, so the
    legacy name is never consulted and never silently substituted; rejecting
    an invalid value is the caller's job (fail-closed)."""
    if name in os.environ:
        return os.environ[name]
    legacy = legacy_name(name)
    if legacy is not None and legacy in os.environ:
        _warn_legacy(name, legacy)
        return os.environ[legacy]
    return default


def with_legacy_names(names):
    """Project a collection of variable names to also cover their verified
    legacy spellings, so redaction can't be bypassed via the old prefix."""
    out = set()
    for name in names:
        out.add(name)
        legacy = legacy_name(name)
        if legacy is not None:
            out.add(legacy)
    return out
