"""Host-scoped scheduling limits: concurrency and task timeout.

These knobs live on the cloud-managed host record so device reports cannot
silently overwrite operator choices. They are scheduling policy, not part of
what a run is allowed to execute: strip SCHEDULING_HOST_KEYS from authorization
and policy digests so raising/lowering a limit never forces re-approval.

Where the old pins lived
------------------------
* Concurrency 1: built-in adapter catalog declares concurrency:1; auto-register
  and LocalSetup.configure copy that onto installation.max_concurrency;
  Service._task_admissible and Connector._reject_workspace_conflict count by
  installation against that cap. config/hosts.example.json had unused
  ``parallel: 1``.
* Timeout 3600: submit_task / launch_contract / runner fall through to 3600
  when timeout_s is missing on the task / installation.
"""

from __future__ import annotations

from copy import deepcopy

SCHEDULING_HOST_KEYS = frozenset({"max_concurrency", "timeout_s", "parallel"})

CONCURRENCY_MIN = 1
CONCURRENCY_MAX = 16
CONCURRENCY_DEFAULT = 1

TIMEOUT_MIN = 30
TIMEOUT_MAX = 86400
TIMEOUT_DEFAULT = 3600


def _reject_bool(value, code):
    if isinstance(value, bool):
        raise ValueError(code)


def parse_concurrency(value):
    """Strict int in [1, 16]. Rejects bools and non-integers."""
    code = "invalid_max_concurrency"
    _reject_bool(value, code)
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    else:
        raise ValueError(code)
    if not (CONCURRENCY_MIN <= n <= CONCURRENCY_MAX):
        raise ValueError(code)
    return n


def parse_timeout(value):
    """Strict int in [30, 86400]. Rejects bools and non-integers."""
    code = "invalid_timeout_s"
    _reject_bool(value, code)
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    else:
        raise ValueError(code)
    if not (TIMEOUT_MIN <= n <= TIMEOUT_MAX):
        raise ValueError(code)
    return n


def soft_timeout(value):
    """Best-effort timeout from a stored installation field (legacy configs)."""
    if value is None:
        return None
    try:
        return parse_timeout(int(value))
    except (TypeError, ValueError):
        return None


def host_without_scheduling(host):
    """Shallow copy of a host dict without scheduling knobs (for digests)."""
    if not isinstance(host, dict):
        return host
    return {k: v for k, v in host.items() if k not in SCHEDULING_HOST_KEYS}


def explicit_host_concurrency(host):
    """Return the operator-set host concurrency, or None if unset/invalid.

    Prefers max_concurrency; falls back to legacy unused ``parallel``.
    """
    if not isinstance(host, dict):
        return None
    for key in ("max_concurrency", "parallel"):
        if key not in host or host[key] is None:
            continue
        try:
            return parse_concurrency(host[key])
        except ValueError:
            return None
    return None


def explicit_host_timeout(host):
    if not isinstance(host, dict) or host.get("timeout_s") is None:
        return None
    try:
        return parse_timeout(host["timeout_s"])
    except ValueError:
        return None


def host_concurrency_limit(host, installation=None):
    """Return (cap, scope) for admission.

    scope is 'host' when the host record has an explicit cap, else
    'installation' using installation.max_concurrency (default 1).
    """
    cap = explicit_host_concurrency(host)
    if cap is not None:
        return cap, "host"
    inst = installation or {}
    try:
        n = int(inst.get("max_concurrency", CONCURRENCY_DEFAULT))
    except (TypeError, ValueError):
        n = CONCURRENCY_DEFAULT
    if isinstance(inst.get("max_concurrency"), bool):
        n = CONCURRENCY_DEFAULT
    n = max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, n))
    return n, "installation"


def task_timeout_s(task_body=None, host=None, installation=None):
    """Resolve execution timeout for a new task snapshot.

    Order: per-task body timeout_s > host timeout_s > installation timeout_s > 3600.
    """
    body = task_body or {}
    if body.get("timeout_s") is not None:
        return parse_timeout(body["timeout_s"])
    host_t = explicit_host_timeout(host or {})
    if host_t is not None:
        return host_t
    inst_t = soft_timeout((installation or {}).get("timeout_s"))
    if inst_t is not None:
        return inst_t
    return TIMEOUT_DEFAULT


def limits_view(host):
    """Public projection of host scheduling limits for API / UI."""
    host = host or {}
    conc = explicit_host_concurrency(host)
    timeout = explicit_host_timeout(host)
    _, scope = host_concurrency_limit(host, {})
    if conc is not None:
        scope = "host"
    else:
        scope = "installation"
    return {
        "max_concurrency": conc,
        "timeout_s": timeout,
        "scope": scope,
        "concurrency_bounds": {"min": CONCURRENCY_MIN, "max": CONCURRENCY_MAX, "default": CONCURRENCY_DEFAULT},
        "timeout_bounds": {"min": TIMEOUT_MIN, "max": TIMEOUT_MAX, "default": TIMEOUT_DEFAULT},
    }


def apply_host_limits(host, body):
    """Return a deep-copied host with limits applied.

    body keys: max_concurrency, timeout_s (only). null clears the field.
    Empty / missing both raises empty_host_limits. Unknown keys raise
    unsupported_host_field.
    """
    if body is None or not isinstance(body, dict):
        raise ValueError("empty_host_limits")
    unknown = set(body) - {"max_concurrency", "timeout_s"}
    if unknown:
        raise ValueError("unsupported_host_field")
    if "max_concurrency" not in body and "timeout_s" not in body:
        raise ValueError("empty_host_limits")

    out = deepcopy(host) if isinstance(host, dict) else {}
    if "max_concurrency" in body:
        value = body["max_concurrency"]
        if value is None:
            out.pop("max_concurrency", None)
            out.pop("parallel", None)  # clear legacy alias too
        else:
            out["max_concurrency"] = parse_concurrency(value)
            out.pop("parallel", None)
    if "timeout_s" in body:
        value = body["timeout_s"]
        if value is None:
            out.pop("timeout_s", None)
        else:
            out["timeout_s"] = parse_timeout(value)
    return out
