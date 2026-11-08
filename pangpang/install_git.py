"""Bounded git-checkout identity and self-upgrade.

Production installs are often a git checkout (not a baked wheel). Reading
``git rev-parse HEAD`` of *this* install root is the running truth, not a
guess. Upgrade only fetches a fixed tracking ref and resets to a confirmed
full SHA that is an ancestor of that tip — never an arbitrary shell command.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .build_info import build_identity
from .layout import is_source_checkout
from .store import Fault

_SHA = re.compile(r"^[0-9a-f]{40}$")
# Simple names only: no leading dash/slash, no URLs, no flags, no absolute paths.
_REMOTE_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_BRANCH_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$")
_DEFAULT_REMOTE = "origin"
_DEFAULT_BRANCH = "main"
_IDENTITY_CACHE = {"at": 0.0, "value": None}
_IDENTITY_TTL = 5.0


def tracking_ref():
    remote = os.environ.get("PANGPANG_UPGRADE_REMOTE", _DEFAULT_REMOTE).strip() or _DEFAULT_REMOTE
    branch = os.environ.get("PANGPANG_UPGRADE_BRANCH", _DEFAULT_BRANCH).strip() or _DEFAULT_BRANCH
    if (not _REMOTE_OK.fullmatch(remote) or not _BRANCH_OK.fullmatch(branch)
            or remote.startswith("-") or branch.startswith("-") or branch.startswith("/")):
        raise Fault("invalid_upgrade_tracking_ref", 400)
    return remote, branch


def install_root():
    """Repo root for a source checkout; None for wheels / frozen builds."""
    if not is_source_checkout():
        return None
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists() and not (root / ".git").is_file():
        return None
    return root


def _git(root, args, timeout=30):
    kwargs = {}
    if os.name == "nt":
        # Background git queries must not pop a console window on Windows.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False, capture_output=True, text=True, timeout=timeout, **kwargs,
    )


def _rev_parse(root, rev):
    proc = _git(root, ["rev-parse", rev], timeout=5)
    if proc.returncode != 0:
        raise Fault("git_rev_parse_failed", 400, advice=(proc.stderr or "").strip()[:300] or None)
    commit = (proc.stdout or "").strip()
    if not _SHA.fullmatch(commit):
        raise Fault("git_rev_parse_failed", 400)
    return commit


def _git_checkout_identity(root):
    try:
        commit = _rev_parse(root, "HEAD")
        ref_proc = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
        ref = (ref_proc.stdout or "").strip() if ref_proc.returncode == 0 else "unknown"
        if not ref:
            ref = "unknown"
        return {"commit": commit, "ref": ref[:80]}
    except (Fault, OSError, subprocess.SubprocessError):
        return None


def install_identity():
    """What is running now. Baked build first; else git HEAD; else unknown.

    Never raises into heartbeat/snapshot callers: a missing git binary or a
    timed-out rev-parse degrades to ``source=unknown``.
    """
    now = time.monotonic()
    cached = _IDENTITY_CACHE.get("value")
    if cached is not None and now - float(_IDENTITY_CACHE.get("at") or 0) < _IDENTITY_TTL:
        return dict(cached)
    try:
        commit, ref, built_at = build_identity()
        if commit not in ("", "unknown"):
            value = {
                "version": __version__,
                "commit": commit,
                "ref": ref,
                "built_at": built_at,
                "source": "build",
            }
        else:
            root = install_root()
            git = _git_checkout_identity(root) if root is not None else None
            if git:
                value = {
                    "version": __version__,
                    "commit": git["commit"],
                    "ref": git["ref"],
                    "built_at": "unknown",
                    "source": "git",
                }
            else:
                value = {
                    "version": __version__,
                    "commit": "unknown",
                    "ref": "unknown",
                    "built_at": "unknown",
                    "source": "unknown",
                }
    except Exception:
        value = {
            "version": __version__,
            "commit": "unknown",
            "ref": "unknown",
            "built_at": "unknown",
            "source": "unknown",
        }
    _IDENTITY_CACHE["at"] = now
    _IDENTITY_CACHE["value"] = dict(value)
    return value


def require_target_commit(value):
    commit = str(value or "").strip().lower()
    if not _SHA.fullmatch(commit):
        raise Fault("invalid_target_commit", 400)
    return commit




def plan_upgrade(root=None):
    """Fetch the tracking ref and report current HEAD vs tip."""
    root = Path(root) if root is not None else install_root()
    if root is None:
        raise Fault("install_not_git_checkout", 400)
    remote, branch = tracking_ref()
    current = _rev_parse(root, "HEAD")
    # `--` keeps remote/branch from being parsed as git options.
    fetch = _git(root, ["fetch", "--no-tags", "--", remote, branch], timeout=120)
    if fetch.returncode != 0:
        raise Fault(
            "git_fetch_failed", 400,
            advice=(fetch.stderr or fetch.stdout or "").strip()[:400] or None,
        )
    # FETCH_HEAD is whatever this fetch just retrieved — independent of whether
    # remote.origin.fetch maps the branch into refs/remotes/*.
    tip = _rev_parse(root, "FETCH_HEAD")
    return {
        "version": __version__,
        "current_commit": current,
        "tip_commit": tip,
        "tracking_ref": "%s/%s" % (remote, branch),
        "already_current": current == tip,
        "source": "git",
    }


def require_tracking_ancestor(target_commit, root=None):
    """Cloud preflight: reject a target the device's tracking ref can never yield.

    A device may only reset to an ancestor of the tracking tip it just fetched
    (see ``apply_upgrade``). The cloud already knows its own tracking tip, so
    checking it here names the real cause before a command is queued: most
    often the cloud's own HEAD is a branch/PR tip that is not on origin/main.
    Otherwise the device would fetch only origin/main, fail cat-file or
    ancestry, and report a device-side fault for a cloud-side mistake.

    Returns the resolved tracking tip.
    """
    root = Path(root) if root is not None else install_root()
    if root is None:
        raise Fault("install_not_git_checkout", 400)
    target = require_target_commit(target_commit)
    tip = plan_upgrade(root=root)["tip_commit"]
    ancestor = _git(root, ["merge-base", "--is-ancestor", target, tip], timeout=5)
    if ancestor.returncode != 0:
        raise Fault("cloud_upgrade_target_not_on_tracking", 400)
    return tip


def apply_upgrade(target_commit, root=None):
    """Reset this checkout to ``target_commit`` when it sits on the tracking tip history."""
    root = Path(root) if root is not None else install_root()
    if root is None:
        raise Fault("install_not_git_checkout", 400)
    target = require_target_commit(target_commit)
    remote, branch = tracking_ref()
    current = _rev_parse(root, "HEAD")
    fetch = _git(root, ["fetch", "--no-tags", "--", remote, branch], timeout=120)
    if fetch.returncode != 0:
        raise Fault(
            "git_fetch_failed", 400,
            advice=(fetch.stderr or fetch.stdout or "").strip()[:400] or None,
        )
    tip = _rev_parse(root, "FETCH_HEAD")
    if current == target:
        # Still require target to sit on the fetched tracking history.
        ancestor = _git(root, ["merge-base", "--is-ancestor", target, tip], timeout=5)
        if ancestor.returncode != 0:
            raise Fault("target_commit_not_on_tracking_ref", 400)
        return {
            "status": "already_current",
            "commit": current,
            "previous": current,
            "tracking_ref": "%s/%s" % (remote, branch),
            "tip_commit": tip,
        }
    probe = _git(root, ["cat-file", "-e", "%s^{commit}" % target], timeout=5)
    if probe.returncode != 0:
        raise Fault("target_commit_unavailable", 400)
    ancestor = _git(root, ["merge-base", "--is-ancestor", target, tip], timeout=5)
    if ancestor.returncode != 0:
        raise Fault("target_commit_not_on_tracking_ref", 400)
    reset = _git(root, ["reset", "--hard", target], timeout=60)
    if reset.returncode != 0:
        raise Fault(
            "git_reset_failed", 400,
            advice=(reset.stderr or reset.stdout or "").strip()[:400] or None,
        )
    _IDENTITY_CACHE["value"] = None
    return {
        "status": "updated",
        "commit": target,
        "previous": current,
        "tracking_ref": "%s/%s" % (remote, branch),
        "tip_commit": tip,
    }


def _data_arg_from_argv(argv):
    for i, item in enumerate(argv):
        if item == "--data" and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith("--data="):
            return item.split("=", 1)[1]
    return None


def restart_argv(module, data=None):
    """Rebuild a ``python -m <module> --data …`` argv for the current role.

    Prefer the live process data directory (``data=``) when the caller has it —
    ``python -m pangpang`` defaults ``--data`` to a *relative* ``data-v2`` that
    follows cwd, which is not the same as ``resolve_data_dir``'s source-checkout
    absolute path.
    """
    if module not in {"pangpang", "pangpang.connector"}:
        raise Fault("invalid_restart_module", 400)
    if data:
        data = str(Path(data).expanduser().resolve())
    else:
        data = _data_arg_from_argv(sys.argv)
    if not data:
        from .cli import DataLocationError, resolve_data_dir
        role = "cloud" if module == "pangpang" else "connector"
        try:
            data = str(resolve_data_dir(role, None, source_checkout=is_source_checkout()))
        except DataLocationError as error:
            raise Fault("restart_data_arg_missing", 400, advice=str(error)[:400]) from error
    rest = []
    argv = list(sys.argv)
    i = 0
    # Drop the executable / script / -m module prefix; keep trailing role flags.
    while i < len(argv):
        item = argv[i]
        if item in {sys.executable, "-m", "pangpang", "pangpang.connector"}:
            i += 1
            continue
        if item.endswith("pangpang/__main__.py") or item.endswith("pangpang\\__main__.py"):
            i += 1
            continue
        if item.endswith("connector.py") and "pangpang" in item.replace("\\", "/"):
            i += 1
            continue
        if item == "--data":
            i += 2
            continue
        if item.startswith("--data="):
            i += 1
            continue
        # Skip the original script path when launched as a file.
        if i == 0:
            i += 1
            continue
        rest.append(item)
        i += 1
    return [sys.executable, "-m", module, "--data", data, *rest]


def schedule_self_restart(module, delay=1.0, data=None):
    """Spawn ``python -m <module> --data …`` detached, then exit this process.

    Supervised installs (launchd KeepAlive / systemd Restart=always) can also
    simply exit; spawning a successor covers manually started sessions.
    """
    argv = restart_argv(module, data=data)

    def _restart():
        time.sleep(max(0.2, float(delay)))
        kwargs = {"close_fds": True}
        if os.name == "nt":
            flags = 0
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if flags:
                kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(argv, **kwargs)
        finally:
            # Non-zero so Restart=on-failure supervisors also come back.
            os._exit(1)

    threading.Thread(target=_restart, name="pangpang-self-restart", daemon=True).start()
