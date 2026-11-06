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
# Fixed tracking ref. Operators may override the remote/branch pair via env,
# but values must stay simple names — no paths, URLs, or flags.
_REMOTE_OK = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_BRANCH_OK = re.compile(r"^[A-Za-z0-9._/-]{1,80}$")
_DEFAULT_REMOTE = "origin"
_DEFAULT_BRANCH = "main"


def tracking_ref():
    remote = os.environ.get("PANGPANG_UPGRADE_REMOTE", _DEFAULT_REMOTE).strip() or _DEFAULT_REMOTE
    branch = os.environ.get("PANGPANG_UPGRADE_BRANCH", _DEFAULT_BRANCH).strip() or _DEFAULT_BRANCH
    if not _REMOTE_OK.fullmatch(remote) or not _BRANCH_OK.fullmatch(branch):
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
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False, capture_output=True, text=True, timeout=timeout,
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
    except Fault:
        return None
    ref_proc = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    ref = (ref_proc.stdout or "").strip() if ref_proc.returncode == 0 else "unknown"
    if not ref:
        ref = "unknown"
    return {"commit": commit, "ref": ref[:80]}


def install_identity():
    """What is running now. Baked build first; else git HEAD; else unknown."""
    commit, ref, built_at = build_identity()
    if commit not in ("", "unknown"):
        return {
            "version": __version__,
            "commit": commit,
            "ref": ref,
            "built_at": built_at,
            "source": "build",
        }
    root = install_root()
    if root is not None:
        git = _git_checkout_identity(root)
        if git:
            return {
                "version": __version__,
                "commit": git["commit"],
                "ref": git["ref"],
                "built_at": "unknown",
                "source": "git",
            }
    return {
        "version": __version__,
        "commit": "unknown",
        "ref": "unknown",
        "built_at": "unknown",
        "source": "unknown",
    }


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
    fetch = _git(root, ["fetch", "--no-tags", remote, branch], timeout=120)
    if fetch.returncode != 0:
        raise Fault(
            "git_fetch_failed", 400,
            advice=(fetch.stderr or fetch.stdout or "").strip()[:400] or None,
        )
    tip = _rev_parse(root, "%s/%s" % (remote, branch))
    return {
        "version": __version__,
        "current_commit": current,
        "tip_commit": tip,
        "tracking_ref": "%s/%s" % (remote, branch),
        "already_current": current == tip,
        "source": "git",
    }


def apply_upgrade(target_commit, root=None):
    """Reset this checkout to ``target_commit`` when it sits on the tracking tip history."""
    root = Path(root) if root is not None else install_root()
    if root is None:
        raise Fault("install_not_git_checkout", 400)
    target = require_target_commit(target_commit)
    remote, branch = tracking_ref()
    current = _rev_parse(root, "HEAD")
    if current == target:
        return {
            "status": "already_current",
            "commit": current,
            "previous": current,
            "tracking_ref": "%s/%s" % (remote, branch),
        }
    fetch = _git(root, ["fetch", "--no-tags", remote, branch], timeout=120)
    if fetch.returncode != 0:
        raise Fault(
            "git_fetch_failed", 400,
            advice=(fetch.stderr or fetch.stdout or "").strip()[:400] or None,
        )
    tip = _rev_parse(root, "%s/%s" % (remote, branch))
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
    return {
        "status": "updated",
        "commit": target,
        "previous": current,
        "tracking_ref": "%s/%s" % (remote, branch),
        "tip_commit": tip,
    }


def schedule_self_restart(delay=1.0):
    """Spawn the same argv detached, then exit this process.

    Supervised installs (launchd KeepAlive / systemd) can simply exit; spawning
    a successor also covers manually started Windows / shell sessions.
    """
    argv = [sys.executable, *sys.argv]

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
            os._exit(0)

    threading.Thread(target=_restart, name="pangpang-self-restart", daemon=True).start()
