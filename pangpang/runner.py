"""Standalone, per-task supervisor. Also uploaded unchanged to SSH hosts.

No daemon installation. The supervisor survives a dropped coordinator connection.
Only a completed receipt with matching execution identity releases capacity.
"""
import argparse
import base64
import errno
import hashlib
import json
import os
import platform
import re
try:
    import fcntl
except ImportError: # Windows has no fcntl; the lock is best-effort there.
    fcntl = None
import secrets
import shutil
import signal
import stat as statmod
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from pathlib import Path


RECEIPT_MAX_BYTES = 1024 * 1024
# L0 process-group cooperative tier. Selected ONLY when the
# spec carries allow_pgroup_cooperative (the host operator's current risk consent,
# see pangpang/consent.py) and no stronger backend exists on this host.
PGROUP_COOPERATIVE = "posix-pgroup-cooperative"

# How long a cancel waits after SIGINT before it hardens. One constant, not a
# config knob: there is no existing kill-timeout setting to reuse. A second
# cancel does not move this clock. The short wait after SIGTERM is the same
# bound the loop already used before SIGKILL.
CANCEL_GRACE_S = 10
CANCEL_TERM_WAIT_S = 3

# Supervision tiers that can confirm their own scope is empty, with the only
# termination evidence each can carry, and their coverage order. Shared by the
# runner (pre-launch refusal), the cloud (commit) and the connector (report /
# resolve) so all three judge a receipt identically.
TREE_EVIDENCE = {"windows-job-object": None,
                 "linux-cgroup-cooperative": "cgroup-v2-populated-zero",
                 PGROUP_COOPERATIVE: "pgroup-empty"}
SUPERVISION_RANK = {PGROUP_COOPERATIVE: 1, "linux-cgroup-cooperative": 2, "windows-job-object": 3}
KNOWN_SUPERVISION = set(TREE_EVIDENCE) | {"posix-process-group", "macos-pgroup-postverify"}


# Controlled paths that end an attempt WITHOUT ever spawning the CLI, each bound to
# the only outcome it may report. Written explicitly by those paths; a missing
# field is never read as "not launched".
NOT_LAUNCHED_OUTCOME = {"runner_prelaunch": "failed", # runner refused/failed before spawn
                        "device_rejected": "failed", # connector gate refused the start
                        "never_started": "cancelled", # no run directory ever existed
                        "queued_cancel": "cancelled"} # cancelled before any launch


def receipt_proof_ok(receipt, floor=None):
    """Is this receipt a terminal proof that may settle its attempt? Pure: depends
    only on the receipt and the attempt's supervision floor frozen at submit —
    never on the CURRENT deployment level or consent, which govern new launches
    only. Always: process_tree_confirmed is True, the label (if any) is known and
    any evidence is exactly what that label produces. Then, for an attempt with a
    frozen floor:
      - operator_resolution (operator evidence recorded cloud-side) may only cancel;
      - not_launched must name a controlled pre-launch path and its one outcome;
      - anything else is treated as having run the CLI, whatever its status, and
        needs a tree-capable label with its evidence, at least the floor.
    A legacy floor-less attempt (submitted before the floor existed, possibly by a device
    that never writes not_launched) keeps the previous rule: a non-success
    without exit_code is taken as not launched.
    Not attestation: L0 trusts the same-account device; this rejects incomplete or
    contradictory reports (version skew, bugs)."""
    if receipt.get("process_tree_confirmed") is not True:
        return False
    label, evidence = receipt.get("supervision"), receipt.get("termination_evidence")
    status = receipt.get("status")
    if label is not None and label not in KNOWN_SUPERVISION:
        return False
    if evidence is not None and (label not in TREE_EVIDENCE or evidence != TREE_EVIDENCE[label]):
        return False
    if floor is None:
        if status != "succeeded" and "exit_code" not in receipt:
            return True
    elif receipt.get("operator_resolution"):
        return status == "cancelled"
    elif "not_launched" in receipt:
        return (NOT_LAUNCHED_OUTCOME.get(receipt["not_launched"]) == status
                and evidence is None and "exit_code" not in receipt)
    elif floor not in SUPERVISION_RANK:
        return False # an unknown floor is never "no floor"
    if label not in TREE_EVIDENCE or evidence != TREE_EVIDENCE[label]:
        return False
    return SUPERVISION_RANK[label] >= SUPERVISION_RANK.get(floor, 0)


def check_control_separation(roots, control_roots):
    """Reject overlapping real paths, including ancestor roots and junctions.

    This is a path-policy check, not OS confinement of the CLI process.
    """
    for root in roots:
        workspace = Path(root).resolve()
        for control in control_roots:
            protected = Path(control).resolve()
            if workspace == protected or workspace in protected.parents or protected in workspace.parents:
                raise ValueError("workspace_overlaps_control_plane")


INBOX_DIR = ".pangpang-inbox"
LIST_MAX_ENTRIES = 500


def _inbox_name(raw):
    """A safe single path component for an uploaded file's name."""
    name = Path(str(raw or "").replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).lstrip(".")[:120]
    return name or "upload"


def receive_file(spec):
    """Store bytes sent from the chat (paste / drag / pick) on THIS host, inside its
    first allowed root: <root>/.pangpang-inbox/<YYYYMMDD>/<random>-<name>. The device
    picks the place (the cloud does not know a connector's roots); exclusive create,
    bounded like any transfer, and the target is re-checked inside the roots."""
    roots = spec["allowed_roots"]
    if not roots:
        raise ValueError("no_allowed_roots")
    data = base64.b64decode(spec["data"], validate=True)
    if len(data) > HARVEST_MAX_FILE_BYTES:
        raise ValueError("transfer_limit_8MiB")
    folder = Path(roots[0]).expanduser().resolve() / INBOX_DIR / time.strftime("%Y%m%d")
    target = checked(str(folder / (secrets.token_hex(4) + "-" + _inbox_name(spec["path"]))), roots, False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = checked(str(target), roots, False) # the folder chain may not have existed before
    with open(target, "xb") as out:
        out.write(data)
    return {"path": str(target), "digest": hashlib.sha256(data).hexdigest(), "size": len(data)}


def list_dir(spec):
    """Directory listing for the file picker, confined to the allowed roots. An empty
    path lists the roots themselves. Bounded; entries that cannot be stat'ed are
    skipped; symlinks are reported as such and never followed out of the roots."""
    roots = spec["allowed_roots"]
    if not spec.get("path"):
        return {"path": None, "parent": None, "truncated": False,
                "entries": [{"name": str(Path(r).expanduser().resolve()), "type": "dir"} for r in roots]}
    p = checked(spec["path"], roots)
    if not p.is_dir():
        raise ValueError("not_a_directory")
    entries, truncated = [], False
    with os.scandir(p) as it:
        for entry in it:
            if len(entries) >= LIST_MAX_ENTRIES:
                truncated = True
                break
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            kind = ("link" if statmod.S_ISLNK(st.st_mode) else "dir" if statmod.S_ISDIR(st.st_mode)
                    else "file" if statmod.S_ISREG(st.st_mode) else None)
            if kind:
                entries.append({"name": entry.name, "type": kind, "size": st.st_size if kind == "file" else None,
                                "mtime": int(st.st_mtime)})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    in_root = any(p == Path(r).expanduser().resolve() for r in roots)
    return {"path": str(p), "parent": None if in_root else str(p.parent), "truncated": truncated, "entries": entries}


def read_file_bytes(path):
    """Read and hash the same bounded regular-file descriptor."""
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as source:
        if not statmod.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("regular_file_required")
        raw = source.read(HARVEST_MAX_FILE_BYTES + 1)
    if len(raw) > HARVEST_MAX_FILE_BYTES:
        raise ValueError("transfer_limit_8MiB")
    return {"data": base64.b64encode(raw).decode(), "digest": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def archive_artifacts(directory, artifacts, roots, execution_id):
    """Supervisor-owned versions; later workspace edits cannot change a delivery."""
    archive = directory / "artifacts"
    archive.mkdir(exist_ok=True)
    for item in artifacts:
        try:
            content = read_file_bytes(checked(item["path"], roots))
            if content["digest"] != item["digest"]:
                raise ValueError("source_changed")
            target = archive / item["digest"]
            try:
                with target.open("xb") as out:
                    out.write(base64.b64decode(content["data"]))
                    out.flush()
                    os.fsync(out.fileno())
            except FileExistsError:
                if file_hash(target) != item["digest"]:
                    raise ValueError("snapshot_digest_mismatch")
            item["snapshot_execution_id"] = execution_id
        except (OSError, ValueError) as error:
            item["snapshot_error"] = type(error).__name__

# Auto-harvest limits. A run may produce artifacts the model never declared
# (e.g. matplotlib writes chart.png). We collect them from a dedicated output
# dir (primary) + a shallow cwd scan for new media/docs (fallback), bounded so
# a runaway executor cannot fill the DB or stall the harvest.
HARVEST_MAX_FILES = 64
# Keep in lockstep with the download `read` transfer cap (8 MiB, see the
# `read` action below): a larger cap would let us register an artifact that
# can never be downloaded ("transfer_limit_8MiB"). Raising both to stream big
# media is deferred to future media-preview work.
HARVEST_MAX_FILE_BYTES = 8 * 1024 * 1024
HARVEST_MAX_TOTAL_BYTES = 256 * 1024 * 1024
# Bound the harvest itself (not just what we accept): a dir stuffed with
# millions of duplicate/filtered files must not stall a task's terminal receipt.
HARVEST_MAX_SCAN = 20000 # entries looked at across a single harvest_dir call
HARVEST_MAX_SECONDS = 30 # wall-clock ceiling per harvest_dir call
# cwd-fallback only picks up *new* files with these deliverable extensions, so a
# task run inside a real project dir does not vacuum up source/temp/build files.
HARVEST_FALLBACK_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff",
    ".mp4", ".webm", ".mov", ".mkv", ".avi",
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac",
    ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".md", ".html", ".htm",
}


def _harvest_one(path, roots):
    """Securely hash+size a candidate file. Returns an artifact dict or None.

    Refuses anything that is not a plain regular file (symlink / junction /
    FIFO / device / socket) and re-validates the path stays inside allowed
    roots. Opens the file once (non-blocking, no-follow) and stats the
    *descriptor* (not the path) so a swap between check and read cannot
    redirect us (TOCTOU) or block us on a FIFO."""
    try:
        p = checked(str(path), roots) # resolves, rejects out-of-root + reparse
    except (ValueError, OSError, RuntimeError):
        return None
    try:
        # O_NONBLOCK: a file swapped to a FIFO after the is_file() check would
        # otherwise block os.open(O_RDONLY) forever (no writer) and stall the
        # whole harvest with no timeout. O_NOFOLLOW: refuse a symlinked final
        # component. Then verify the *opened descriptor* is a regular file.
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(str(p), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not statmod.S_ISREG(st.st_mode):
            return None
        if st.st_size > HARVEST_MAX_FILE_BYTES:
            return None
        h = hashlib.sha256()
        read = 0
        # Cap the ACTUAL bytes read, not the pre-read stat: a file that grows
        # after fstat() cannot make us read past the per-file limit.
        while read < HARVEST_MAX_FILE_BYTES:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            h.update(block)
            read += len(block)
        if read > HARVEST_MAX_FILE_BYTES:
            return None
        return {"path": str(p), "digest": h.hexdigest(), "size": read}
    finally:
        os.close(fd)


def harvest_dir(root, roots, *, recursive, source, budget, seen_digests,
                new_only_names=None, exts=None):
    """Collect artifacts under `root`, bounded by a shared `budget`.

    budget: mutable {"files": n, "bytes": n} decremented as we accept files.
    seen_digests: set of content hashes already collected (cross-source dedupe).
    new_only_names: if given, only names NOT in this set qualify (cwd fallback).
    exts: if given, only these lowercased suffixes qualify (cwd fallback)."""
    out = []
    # Cap the number of entries we even LOOK at (independent of how many we
    # accept), and cap wall-clock, so a directory stuffed with millions of
    # tiny/duplicate files cannot stall the harvest (files that dedupe or fail
    # a filter do not consume `budget`, so scanning must be bounded separately).
    scanned = 0
    deadline = time.time() + HARVEST_MAX_SECONDS
    try:
        base = Path(root)
        if not base.is_dir() or base.is_symlink():
            return out
        # Enumerate candidate files WITHOUT following directory symlinks — a
        # symlinked subdir could otherwise walk us out of the run dir / into a
        # huge external tree (checked() would reject the leaf, but the traversal
        # itself must not escape). os.walk(followlinks=False) is the guarantee.
        if recursive:
            def gen():
                for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                    for name in filenames:
                        yield Path(dirpath) / name
            it = gen()
        else:
            it = iter(base.iterdir())
        for entry in it:
            if budget["files"] <= 0 or budget["bytes"] <= 0:
                break
            scanned += 1
            if scanned > HARVEST_MAX_SCAN or time.time() > deadline:
                break
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
            except OSError:
                continue
            if new_only_names is not None and entry.name in new_only_names:
                continue
            if exts is not None and entry.suffix.lower() not in exts:
                continue
            art = _harvest_one(entry, roots)
            if not art or art["digest"] in seen_digests:
                continue
            if art["size"] > budget["bytes"]:
                continue
            seen_digests.add(art["digest"])
            budget["files"] -= 1
            budget["bytes"] -= art["size"]
            out.append(dict(art, source=source))
    except OSError:
        pass
    return out


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def checked(raw, roots, must_exist=True):
    p = Path(raw).expanduser().resolve(strict=must_exist)
    if not any(p.is_relative_to(Path(r).expanduser().resolve()) for r in roots):
        raise ValueError("path_outside_allowed_roots")
    if os.name == "nt":
        # Refuse reparse components even when their resolved target is in-root.
        q = Path(raw).absolute()
        for part in [q, *q.parents]:
            if part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("reparse_path_not_supported")
    return p


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# Token boundary, so a neighbouring name ($XPP_OUTPUT_DIR_BACKUP) is NOT a mention
# of this one — the same rule a shell applies.
OUTPUT_DIR_VAR = re.compile(r"\$XPP_OUTPUT_DIR(?![A-Za-z0-9_])")


def execution_output_dir(cwd, execution_id):
    """Deliverable directory supervise() creates: <cwd>/.xpp-out/<execution_id>.

    Pure join of the task cwd and execution id. No filesystem access, so the
    control plane can name the same directory from values it already stored.
    """
    return Path(cwd) / ".xpp-out" / str(execution_id)


def bind_output_dir(prompt, output_dir):
    """: name the output boundary LITERALLY in the executor prompt.

    The control plane writes the delivery obligation (and the HARD
    required_outputs contract) relative to $XPP_OUTPUT_DIR, because only the runner
    knows the real directory — it is created per execution, after the prompt is
    built. But an executor restricted to file tools has NO shell to expand a
    variable, so it would write to cwd and miss the contract. Substituting here, at
    the one place that knows the path, closes that gap for every executor shape.

    Deterministic and system-generated (SIS): the directory comes from the runner,
    never from model text. With no output_dir (read-only cwd degrades to none) the
    prompt is left alone — there is no truthful path to put there, and the env var
    is equally absent."""
    # A lambda replacement, never a template: a Windows path is full of backslashes
    # and re.sub would read them as escapes.
    return prompt if not output_dir else OUTPUT_DIR_VAR.sub(lambda _: str(output_dir), prompt)


# Claude Code online inject. The string is the stdin_protocol
# value. Session id is UUID(execution_id hex); it is not stored.
CLAUDE_STREAM_JSON = "claude-stream-json"
# Codex app-server JSON-RPC on the same stdin the supervisor already owns.
# Not claude-stream-json. `codex exec` (closed stdin) is not this protocol.
CODEX_APP_SERVER = "codex-app-server"
# Tier 2. Not an open stdin. First launch passes `--session-id`; a mailbox
# interrupt SIGINTs the turn, then the next launch is `--resume` of that same
# derived id. The mark file is the run directory's sibling of `cancel`: the
# cgroup child reads it, and it is not a second store.
CLI_SESSION_RESUME = "cli-session-resume"
# ACP on the same stdin the supervisor already owns. Not an interjection
# tier and not a resume. The string is the spec's session_protocol.
ACP = "acp"
ACP_PROTOCOL_VERSION = 1
_SESSION_PROTOCOLS = frozenset({ACP})
# Best-effort denylist of silent-accept flags. It only sees the forms
# written below (`--flag` and `--flag=value`). It is not a security
# boundary: a split `--flag value`, an unknown alias, or a later argv
# edit can slip past it. The real guarantee is that the runner never
# answers a permission request on the user's behalf: without a bridge it
# leaves the request pending (`acp_leave_pending`); with the chat
# bridge declared it writes back only the user's explicit chat decision.
_ACP_SILENT_ACCEPT = frozenset({
    "--yolo",
    "--approve-for-me",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "--permission-mode=bypassPermissions",
    "--permission-mode=dontAsk",
})
SESSION_RESUME_MARK = "session-resume"
STEER_FILE = "injects.jsonl"
STEER_MAX_BYTES = 1024 * 1024
# Chat confirmation for a catalog-declared permission bridge. One
# small file beside the run directory. Undeclared adapters never touch it.
PERMISSION_BRIDGE_CHAT = "chat"
PERMISSION_FILE = "permissions.jsonl"
PERMISSION_MAX_BYTES = 256 * 1024
# How long an unanswered prompt stays open before a denial is written.
# Shorter only when the spec names permission_timeout_s inside this cap.
# There is no path that turns the wait into an allow.
PERMISSION_WAIT_S = 120.0
# After a result event, keep stdin open this long so a mailbox row already
# waiting is written, then close so a CLI that reads until EOF can exit.
STREAM_IDLE_CLOSE_S = 0.25


def session_id_from_execution(execution_id):
    """`--session-id` for an attempt. `exec-` + uuid4().hex, nothing else."""
    if not isinstance(execution_id, str) or not re.fullmatch(r"exec-[0-9a-f]{32}", execution_id):
        raise ValueError("invalid_execution_id")
    return str(uuid.UUID(hex=execution_id[5:]))


def acp_argv(argv):
    """ACP launch argv. The prompt stays off the process list.

    This does not append a protocol subcommand or an approval flag.
    `_ACP_SILENT_ACCEPT` is best-effort, not a security boundary: it
    rejects the listed `--flag` and `--flag=value` forms only. The real
    guarantee is that the runner never answers a permission request on the
    user's behalf (`acp_leave_pending`; the chat bridge, when declared,
    writes back only the user's explicit decision).
    """
    cleaned = []
    for part in argv:
        if not isinstance(part, str):
            cleaned.append(part)
            continue
        if "{prompt}" in part:
            continue
        name = part.split("=", 1)[0]
        if part in _ACP_SILENT_ACCEPT or name in _ACP_SILENT_ACCEPT:
            raise ValueError("acp_silent_accept_forbidden")
        cleaned.append(part)
    return cleaned


def executor_argv(spec, prompt, *, resume=False):
    """CLI argv. Stream-json keeps the prompt off the process list and appends
    `--flag=value` protocol flags, plus `--verbose` (Claude Code refuses
    `--print` + stream-json without it). Permission flags already in argv stay.
    `--fork-session` is refused: the session id is this attempt's own.

    Codex app-server rewrites a stored `exec` invocation into `app-server` on
    stdio. Sandbox and approval values are not renamed here; the session sends
    them on `thread/start`. An argv that is not the Codex CLI is left alone so
    a non-Codex command keeps the closed-stdin path.

    Tier 2 session-resume appends `--session-id` or, once `resume` is set,
    `--resume` of that same derived id. It never passes both, and it never
    adds an approval flag.

    ACP is a session_protocol. It does not append flags. An unregistered
    session_protocol is refused rather than falling through to closed stdin.
    """
    raw = [part for part in (spec.get("argv") or [])]
    session_protocol = spec.get("session_protocol")
    if session_protocol is not None:
        if spec.get("stdin_protocol") or spec.get("resume_protocol"):
            raise ValueError("session_protocol_conflict")
        if session_protocol == ACP:
            return acp_argv(raw)
        raise ValueError("session_protocol_unsupported:" + str(session_protocol))
    protocol = spec.get("stdin_protocol")
    if protocol == CLAUDE_STREAM_JSON:
        argv = [part for part in raw if "{prompt}" not in part]
        session = session_id_from_execution(spec["execution_id"])
        # Claude Code 2.1.x exits immediately when --print is combined with
        # --output-format=stream-json unless --verbose is also present.
        argv += [
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--verbose",
            "--replay-user-messages",
            "--session-id=" + session,
        ]
        if any(part == "--fork-session" or part.startswith("--fork-session=") for part in argv):
            raise ValueError("fork_session_forbidden")
        return argv
    if protocol == CODEX_APP_SERVER:
        if any(part == "fork" or (isinstance(part, str) and part.startswith("--fork")) for part in raw):
            raise ValueError("fork_session_forbidden")
        plan = codex_app_server_plan(raw)
        if plan is None:
            return [part.replace("{prompt}", prompt) for part in raw]
        return plan[0]
    resume_protocol = spec.get("resume_protocol")
    if resume_protocol == CLI_SESSION_RESUME:
        return cli_session_argv(raw, spec.get("execution_id"), resume=resume)
    if protocol:
        raise ValueError("stdin_protocol_unsupported:" + str(protocol))
    if resume_protocol:
        raise ValueError("resume_protocol_unsupported:" + str(resume_protocol))
    return [part.replace("{prompt}", prompt) for part in raw]


_SESSION_FLAG_NAMES = {"--session-id", "--resume", "-r"}


def cli_session_argv(argv, execution_id, *, resume):
    """One identity. First process: `--session-id=<derived>`. Continuation:
    `--resume=<same id>` and not `--session-id` (the CLI rejects both).

    The prompt stays off the process list. `--fork-session` is a second
    identity and is refused. Approval flags are not added."""
    session = session_id_from_execution(execution_id)
    cleaned = []
    skip_value = False
    for part in argv:
        if skip_value:
            skip_value = False
            continue
        if not isinstance(part, str):
            cleaned.append(part)
            continue
        if part == "--fork-session" or part.startswith("--fork-session="):
            raise ValueError("fork_session_forbidden")
        if "{prompt}" in part:
            continue
        name, sep, _value = part.partition("=")
        if name in _SESSION_FLAG_NAMES:
            if not sep:
                skip_value = True
            continue
        cleaned.append(part)
    if resume:
        cleaned.append("--resume=" + session)
    else:
        cleaned.append("--session-id=" + session)
    return cleaned


# `codex app-server --help` (0.160.0) accepts these. `--sandbox` is not among
# them: placed after the subcommand it is an error, and placed before it the
# thread still comes up read-only / on-request. The stored token's value is
# sent as `thread/start.sandbox` / `approvalPolicy`, which use the same strings.
_CODEX_APP_SERVER_VALUE_FLAGS = {"-c", "--config", "--enable", "--disable", "--listen"}
_CODEX_APP_SERVER_BOOL_FLAGS = {"--strict-config", "--stdio", "--analytics-default-enabled"}
_CODEX_SANDBOX_FLAGS = {"-s", "--sandbox"}
_CODEX_APPROVAL_FLAGS = {"-a", "--ask-for-approval"}
_CODEX_MODEL_FLAGS = {"-m", "--model"}
_CODEX_CD_FLAGS = {"-C", "--cd"}
_CODEX_EXEC_ONLY_VALUE_FLAGS = {
    "--color", "--output-schema", "--thread-source", "--local-provider",
    "-p", "--profile", "-i", "--image", "--add-dir", "-o", "--output-last-message",
}
_CODEX_EXEC_ONLY_BOOL_FLAGS = {
    "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
    "--ignore-rules", "--oss", "--worktree", "--search", "--no-alt-screen",
    "--no-daemon", "--dangerously-bypass-hook-trust",
    # Automatic review. Not forwarded: this channel never approves a prompt.
    "--approve-for-me",
}
_CODEX_VALUE_FLAGS = (
    _CODEX_APP_SERVER_VALUE_FLAGS | _CODEX_SANDBOX_FLAGS | _CODEX_APPROVAL_FLAGS
    | _CODEX_MODEL_FLAGS | _CODEX_CD_FLAGS | _CODEX_EXEC_ONLY_VALUE_FLAGS
)


def _codex_flag_name(token):
    if isinstance(token, str) and token.startswith("--"):
        name, sep, value = token.partition("=")
        return name, (value if sep else None)
    return token, None


def _codex_take(tokens, index):
    """One flag and its value, plus the index just past them."""
    token = tokens[index]
    name, inline = _codex_flag_name(token)
    if inline is not None or name not in _CODEX_VALUE_FLAGS:
        return name, inline, [token], index + 1
    if index + 1 < len(tokens):
        return name, tokens[index + 1], [token, tokens[index + 1]], index + 2
    return name, None, [token], index + 1


def codex_app_server_plan(argv):
    """`(launch_argv, thread_start_params)` for a Codex CLI argv, else None.

    None means this argv is not `exec` / `app-server` (a python stub, or any
    other command). The caller keeps the closed-stdin launch.

    `thread/start` carries sandbox and approval only. `exec` with no approval
    flag is `approvalPolicy: never`: codex 0.160.0 logs `approval_policy=never`
    for `codex exec` even when config.toml says `on-request`, while a bare
    `app-server` thread comes up `on-request`. `never` means the server does
    not ask; it is not an accept. A stored `--ask-for-approval` value is sent
    unchanged and still not answered. `--approve-for-me` is dropped.
    `--dangerously-bypass-approvals-and-sandbox` is the measured exec effect
    `sandbox=danger-full-access` plus `approvalPolicy=never`, unless a stored
    `--sandbox` or `--ask-for-approval` already names the value.
    """
    if not argv:
        return None
    saw_cli = False
    positionals = [argv[0]]
    passthrough = []
    sandbox = None
    approval = None
    bypass = False
    model = None
    cwd = None
    listen = False
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in {"exec", "app-server"}:
            saw_cli = True
            index += 1
            continue
        if token == "-" or (isinstance(token, str) and "{prompt}" in token):
            index += 1
            continue
        if isinstance(token, str) and not token.startswith("-"):
            positionals.append(token)
            index += 1
            continue
        name, value, _taken, index = _codex_take(argv, index)
        if name in _CODEX_SANDBOX_FLAGS and value:
            sandbox = value
            continue
        if name in _CODEX_APPROVAL_FLAGS and value:
            approval = value
            continue
        if name == "--dangerously-bypass-approvals-and-sandbox":
            bypass = True
            continue
        if name in _CODEX_MODEL_FLAGS and value:
            model = value
            continue
        if name in _CODEX_CD_FLAGS and value:
            cwd = value
            continue
        if name in _CODEX_EXEC_ONLY_VALUE_FLAGS or name in _CODEX_EXEC_ONLY_BOOL_FLAGS:
            continue
        if name == "--listen":
            if value not in {None, "stdio://"}:
                raise ValueError("codex_stdio_required")
            listen = True
            passthrough.extend(_taken)
            continue
        if name == "--stdio":
            listen = True
            passthrough.extend(_taken)
            continue
        if name in _CODEX_APP_SERVER_VALUE_FLAGS or name in _CODEX_APP_SERVER_BOOL_FLAGS:
            passthrough.extend(_taken)
            continue
        passthrough.extend(_taken)
    if not saw_cli:
        return None
    launch = [*positionals, "app-server", *passthrough]
    if not listen:
        launch.append("--listen=stdio://")
    params = {}
    if sandbox:
        params["sandbox"] = sandbox
    elif bypass:
        params["sandbox"] = "danger-full-access"
    if model:
        params["model"] = model
    if cwd:
        params["cwd"] = cwd
    # Exec does not ask. A stored approval value wins over that default.
    params["approvalPolicy"] = approval if approval else "never"
    return launch, params


def stdin_session_protocol(spec, argv):
    """Which open-stdin session this launch actually speaks, or None.

    ACP speaks when session_protocol names the registered protocol. Claude
    always speaks stream-json when the spec says so. Codex speaks app-server
    only when the argv was rewritten into that subcommand. A declared stdin
    protocol on a foreign argv does not open a JSON-RPC session.
    """
    if spec.get("session_protocol") == ACP:
        return ACP
    protocol = spec.get("stdin_protocol")
    if protocol == CLAUDE_STREAM_JSON:
        return protocol
    if protocol == CODEX_APP_SERVER and any(part == "app-server" for part in argv[1:]):
        return protocol
    return None


def user_message_frame(text, message_uuid, session_id):
    """One steer frame: user text, uuid, session. No priority.

    `priority:"now"` would background a shell or subagent and break
    process-group supervision, so the key is absent (Claude's default is the
    next tool boundary). No tools, permissions, model, or cwd.
    """
    if not isinstance(text, str) or text == "":
        raise ValueError("steer_text")
    if not isinstance(message_uuid, str) or not message_uuid:
        raise ValueError("steer_uuid")
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": message_uuid,
        "session_id": session_id,
    }


def interrupt_frame(request_id):
    """Tier-2 interrupt on the same stdin. `cancel_queued` drops messages that
    have not been consumed so a resend does not deliver them twice."""
    return {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": "interrupt", "cancel_queued": True},
    }


def _json_object(line):
    try:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        value = json.loads(line)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def apply_stream_event(state, event):
    """Fold one stdout event into the running extraction.

    Delivery is the client uuids a result event says its turn consumed:
    `user_message_uuids`, or on older CLIs that lack it (2.1.251 does) the
    single `user_message_uuid`, as the CLI's schema prescribes. The single
    field names only the last message of a merged batch, so earlier ones stay
    undelivered: an under-claim, never a false delivery. The `isReplay` echo
    is not delivery; an interrupt with `cancel_queued` can still drop it.
    A permission `control_request` is ignored: claude-stream-json does not
    declare `permission_bridge`, so this channel does not approve tools.
    """
    if not isinstance(event, dict):
        return
    kind = event.get("type")
    if kind == "control_request":
        return
    if kind != "result":
        return
    # A later user frame belongs to a turn after this event. The session
    # compares this counter with the seq it wrote at.
    state["result_seq"] = state.get("result_seq", 0) + 1
    result = event.get("result")
    if isinstance(result, str):
        state["result_text"] = result
    uuids = event.get("user_message_uuids")
    if not isinstance(uuids, list):
        uuids = [event.get("user_message_uuid")]
    confirmed = state.setdefault("confirmed", set())
    for item in uuids:
        if isinstance(item, str) and item:
            confirmed.add(item)


def summarize_stream(blob):
    """Final result text and the set of delivered user-message uuids.

    The last `type=result` event wins. Earlier assistant chatter is not the
    result. None when the stream never produced a result event.
    """
    state = {"result_text": None, "confirmed": set()}
    for line in str(blob).splitlines():
        event = _json_object(line)
        if event is not None:
            apply_stream_event(state, event)
    return state["result_text"], set(state["confirmed"])


def steer_actions(rows, confirmed, written, seen_interrupts):
    """What to write next on the shared stdin.

    User rows are injects. An interrupt row writes `cancel_queued` and then
    only the injects whose uuids are not in `confirmed`. Already-written
    injects are not sent again until that interrupt.
    """
    actions = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("op") == "interrupt":
            key = row.get("id")
            if not isinstance(key, str) or key in seen_interrupts:
                continue
            seen_interrupts.add(key)
            actions.append(("interrupt", None))
            for earlier in rows:
                if not isinstance(earlier, dict) or earlier.get("op") != "user":
                    continue
                uid = earlier.get("uuid")
                if isinstance(uid, str) and uid not in confirmed:
                    actions.append(("user", earlier))
                    written.add(uid)
            continue
        if row.get("op") != "user":
            continue
        uid = row.get("uuid")
        if not isinstance(uid, str) or uid in written:
            continue
        actions.append(("user", row))
        written.add(uid)
    return actions


def read_steer_mailbox(directory):
    path = Path(directory) / STEER_FILE
    try:
        st = os.lstat(str(path))
    except OSError:
        return []
    if not statmod.S_ISREG(st.st_mode) or st.st_size > STEER_MAX_BYTES:
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    rows = []
    for line in raw.splitlines():
        event = _json_object(line)
        if event is not None and "priority" not in event:
            rows.append(event)
    return rows


def _append_steer_row(directory, row):
    if "priority" in row:
        raise ValueError("priority_forbidden")
    path = Path(directory) / STEER_FILE
    data = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd = os.open(str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def append_inject(directory, message_uuid, text):
    """Idempotent mailbox append. The line is user text plus uuid, never a priority."""
    if not isinstance(message_uuid, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", message_uuid):
        raise ValueError("invalid_inject")
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 8000:
        raise ValueError("invalid_inject")
    text = text.strip()
    for row in read_steer_mailbox(directory):
        if row.get("op") == "user" and row.get("uuid") == message_uuid:
            if row.get("text") != text:
                raise ValueError("inject_conflict")
            return {"status": "accepted", "uuid": message_uuid}
    _append_steer_row(directory, {"op": "user", "uuid": message_uuid, "text": text})
    return {"status": "accepted", "uuid": message_uuid}


def append_interrupt(directory, interrupt_id):
    """Same mailbox as inject. The supervisor writes cancel_queued, then resends."""
    if not isinstance(interrupt_id, str) or not interrupt_id or len(interrupt_id) > 80:
        raise ValueError("invalid_interrupt")
    for row in read_steer_mailbox(directory):
        if row.get("op") == "interrupt" and row.get("id") == interrupt_id:
            return {"status": "accepted", "id": interrupt_id}
    _append_steer_row(directory, {"op": "interrupt", "id": interrupt_id})
    return {"status": "accepted", "id": interrupt_id}


def accept_inject(directory, payload):
    if not isinstance(payload, dict) or "priority" in payload:
        raise ValueError("priority_forbidden")
    interrupt_id = payload.get("interrupt")
    if interrupt_id is None:
        return append_inject(directory, payload.get("uuid"), payload.get("text"))
    # The sentence lands first. A reader never sees the interrupt without it.
    if not isinstance(interrupt_id, str) or not interrupt_id or len(interrupt_id) > 80:
        raise ValueError("invalid_interrupt")
    result = append_inject(directory, payload.get("uuid"), payload.get("text"))
    append_interrupt(directory, interrupt_id)
    return result


def resume_continuation(directory, acted_interrupts, sent_uuids):
    """Unacted interrupt ids and the user rows not yet sent to a resumed turn.

    None when no new interrupt is waiting. The text list is empty when the
    interrupt row arrived before its sentence; the caller waits rather than
    stopping the turn with nothing to continue.
    """
    fresh = []
    texts = []
    for row in read_steer_mailbox(directory):
        if not isinstance(row, dict):
            continue
        if row.get("op") == "interrupt":
            key = row.get("id")
            if isinstance(key, str) and key not in acted_interrupts:
                fresh.append(key)
            continue
        if row.get("op") != "user":
            continue
        uid = row.get("uuid")
        if isinstance(uid, str) and uid not in sent_uuids and isinstance(row.get("text"), str) and row["text"]:
            texts.append(row)
    if not fresh:
        return None
    return fresh, texts


def mark_session_resume(directory):
    """The continuation mark. Same run directory as the cancel file."""
    path = Path(directory) / SESSION_RESUME_MARK
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def stream_idle_due(result_seq, written_at_seq, pending, idle_since, now):
    """Whether stdin may close, and the idle timestamp to keep.

    Close only after a result newer than the last frame written on this pipe,
    and only once the mailbox has nothing left to send. A write at seq N is
    not covered by result N.
    """
    if pending or not isinstance(result_seq, int) or result_seq <= written_at_seq:
        return False, None
    if idle_since is None:
        return False, now
    if now - idle_since >= STREAM_IDLE_CLOSE_S:
        return True, idle_since
    return False, idle_since


def _write_frame(proc, frame):
    """One JSON line. A priority key is a hard error, not a stripped field."""
    if not isinstance(frame, dict) or "priority" in frame:
        raise ValueError("priority_forbidden")
    request = frame.get("request")
    if isinstance(request, dict) and request.get("subtype") == "interrupt" and request.get("cancel_queued") is not True:
        raise ValueError("interrupt_requires_cancel_queued")
    proc.stdin.write(json.dumps(frame, ensure_ascii=False).encode() + b"\n")
    proc.stdin.flush()


def stream_json_session(proc, directory, spec, prompt, state, lock, stop):
    """Keep stdin open. The first prompt and later injects share this pipe.

    An interrupt row on the mailbox is not a second stop: cancel still signals
    the process group. Interrupt here is the tier-2 write on the same channel.
    """
    session_id = session_id_from_execution(spec["execution_id"])
    prompt_uuid = str(uuid.uuid5(uuid.UUID(session_id), "prompt"))
    written, seen_interrupts = set(), set()
    # The opening prompt is a frame written before any result event.
    written_at_seq = 0
    try:
        _write_frame(proc, user_message_frame(prompt, prompt_uuid, session_id))
        idle_since = None
        while not stop.is_set():
            rows = read_steer_mailbox(directory)
            with lock:
                confirmed = set(state["confirmed"])
                seq = state.get("result_seq", 0)
            actions = steer_actions(rows, confirmed, written, seen_interrupts)
            if actions:
                # Sample seq before the write. A result that lands during the
                # write is newer and may close later; this result is not.
                written_at_seq = seq
                idle_since = None
            for kind, row in actions:
                if stop.is_set():
                    break
                if kind == "interrupt":
                    _write_frame(proc, interrupt_frame(str(uuid.uuid4())))
                else:
                    _write_frame(proc, user_message_frame(row["text"], row["uuid"], session_id))
            if not actions:
                due, idle_since = stream_idle_due(
                    seq, written_at_seq, False, idle_since, time.monotonic())
                if due:
                    break
            stop.wait(0.05)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass


def codex_is_server_request(event):
    """A JSON-RPC request from the server (it has a method and an id).

    Approval, elicitation, and dynamic tool calls arrive this way. The client
    would answer them with a result. This channel never does.
    """
    return isinstance(event, dict) and isinstance(event.get("method"), str) and "id" in event and "result" not in event


def _codex_note_thread(state, thread):
    """Adopt the first thread id the server returns. A forked thread is a
    second identity and is ignored."""
    if not isinstance(thread, dict) or thread.get("forkedFromId"):
        return
    tid = thread.get("id")
    if isinstance(tid, str) and tid and not state.get("thread_id"):
        state["thread_id"] = tid


def _codex_note_turn(state, turn, *, active):
    if not state.get("thread_id") or not isinstance(turn, dict):
        return
    owner = turn.get("threadId")
    if isinstance(owner, str) and owner != state["thread_id"]:
        return
    tid = turn.get("id")
    if isinstance(tid, str) and tid:
        state["turn_id"] = tid
        state["turn_active"] = active


def apply_codex_event(state, event):
    """Fold one app-server stdout object into the shared extraction state.

    Delivery is `userMessage.clientId`, the echo of `clientUserMessageId` on
    `turn/start` and `turn/steer` (codex 0.160.0 schema). A steer response's
    `turnId` is not that echo. Server requests, including approval RPCs, are
    ignored: codex does not declare `permission_bridge`. Not approved.
    The last `agentMessage` with `phase: final_answer` is the text; a
    commentary message does not replace it.
    """
    if not isinstance(event, dict) or codex_is_server_request(event):
        return
    method = event.get("method")
    if "id" in event and isinstance(event.get("result"), dict):
        result = event["result"]
        _codex_note_thread(state, result.get("thread"))
        _codex_note_turn(state, result.get("turn"), active=True)
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    if method == "thread/started":
        _codex_note_thread(state, params.get("thread"))
    elif method == "turn/started":
        _codex_note_turn(state, params.get("turn"), active=True)
    elif method == "turn/completed":
        state["result_seq"] = state.get("result_seq", 0) + 1
        state["turn_active"] = False
    elif method in {"item/completed", "item/started"}:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        if item.get("type") == "userMessage":
            client_id = item.get("clientId")
            if isinstance(client_id, str) and client_id:
                state.setdefault("confirmed", set()).add(client_id)
        if method == "item/completed" and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
            if item.get("phase") == "final_answer":
                state["result_text"] = item["text"]
                state["result_final"] = True
            elif not state.get("result_final"):
                state["result_text"] = item["text"]


def acp_agent_request(event):
    """A JSON-RPC request the agent sends to the client (method and id, no result)."""
    return (isinstance(event, dict)
            and isinstance(event.get("method"), str)
            and "id" in event
            and "result" not in event
            and "error" not in event)


def acp_leave_pending(state, event):
    """ hook. Record the agent request and do not answer it.

    session/request_permission and elicitation/create arrive here, and so
    does any other agent request (fs/*, terminal/*). Nothing in this
    function writes a JSON-RPC result. A `selected` / `allow_*` outcome or
    an elicitation `accept` would approve the request; do not add one.
    """
    pending = state.setdefault("acp_pending", [])
    if len(pending) >= 20:
        return
    item = {"method": event.get("method")}
    ident = event.get("id")
    if isinstance(ident, str) or (isinstance(ident, int) and not isinstance(ident, bool)):
        item["id"] = ident
    pending.append(item)


_ACP_BRIDGE_METHODS = frozenset({"session/request_permission", "elicitation/create"})
_APPROVE_UTTERANCES = frozenset({"approve", "allow", "yes", "y", "同意", "允许", "批准", "可以"})
_DENY_UTTERANCES = frozenset({"deny", "reject", "no", "n", "拒绝", "不允许", "不同意", "不行"})


def note_acp_bridge_request(state, event):
    """Remember a bounded summary for a later chat decision. Does not answer.

    The receipt's `acp_pending` list stays method+id (`acp_leave_pending`).
    Options live here so an explicit allow can name `allow_once` and nothing
    wider. fs/* and terminal/* are not stored: answering them would read or
    run on the user's behalf.
    """
    if not acp_agent_request(event):
        return
    method = event.get("method")
    if method not in _ACP_BRIDGE_METHODS:
        return
    ident = event.get("id")
    if not (isinstance(ident, str) or (isinstance(ident, int) and not isinstance(ident, bool))):
        return
    key = str(ident)
    requests = state.setdefault("acp_requests", {})
    if key in requests or len(requests) >= 20:
        return
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    options = []
    for option in params.get("options") or []:
        if not isinstance(option, dict):
            continue
        option_id = option.get("optionId")
        kind = option.get("kind")
        if isinstance(option_id, str) and option_id and isinstance(kind, str):
            options.append({"optionId": option_id[:80], "kind": kind[:40]})
        if len(options) >= 8:
            break
    tool = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
    title = tool.get("title") if isinstance(tool.get("title"), str) else ""
    if method == "elicitation/create":
        message = params.get("message") if isinstance(params.get("message"), str) else ""
        title = title or message
    requests[key] = {"id": ident, "method": method, "title": title[:200], "options": options}


def _exact_permission_word(token):
    if not isinstance(token, str) or not token or len(token) > 16:
        return None
    if any(ch.isspace() for ch in token):
        return None
    folded = token.lower()
    if token in _APPROVE_UTTERANCES or folded in _APPROVE_UTTERANCES:
        return "approve"
    if token in _DENY_UTTERANCES or folded in _DENY_UTTERANCES:
        return "deny"
    return None


def classify_permission_utterance(text, request_id=None):
    """Map one chat message to approve or deny, or None when it is not explicit.

    The whole message must be the decision word, or that word plus the request
    id. A longer sentence is ambiguous and must not allow.
    """
    if not isinstance(text, str):
        return None
    token = text.strip()
    if len(token) > 80:
        return None
    token = token.strip("。.!！?？")
    if request_id is not None:
        suffix = str(request_id).strip()
        if suffix and token.endswith(suffix) and token != suffix:
            head = token[:-len(suffix)].strip()
            named = _exact_permission_word(head)
            if named is not None:
                return named
    return _exact_permission_word(token)


def permission_wait_s(spec):
    raw = spec.get("permission_timeout_s") if isinstance(spec, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return PERMISSION_WAIT_S
    if not (0 < raw <= PERMISSION_WAIT_S):
        return PERMISSION_WAIT_S
    return float(raw)


def _permission_rows_from(raw):
    rows = []
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeError:
            return rows
    for line in str(raw).splitlines():
        event = _json_object(line)
        if isinstance(event, dict) and event.get("op") in {"ask", "decide"}:
            rows.append(event)
    return rows


def read_permission_rows(directory):
    path = Path(directory) / PERMISSION_FILE
    try:
        st = os.lstat(str(path))
    except OSError:
        return []
    if not statmod.S_ISREG(st.st_mode) or st.st_size > PERMISSION_MAX_BYTES:
        return []
    try:
        return _permission_rows_from(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return []


def _with_permission_file(directory, mutate):
    """Read-modify-append under an exclusive lock. `mutate(rows)` returns
    `(result, extra_row_or_None)`. The first decision for an id is not rewritten."""
    path = Path(directory) / PERMISSION_FILE
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, PERMISSION_MAX_BYTES + 1)
        if len(raw) > PERMISSION_MAX_BYTES:
            result, extra = mutate([])
        else:
            result, extra = mutate(_permission_rows_from(raw))
        if extra is not None:
            data = (json.dumps(extra, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            os.lseek(fd, 0, os.SEEK_END)
            os.write(fd, data)
            os.fsync(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return result
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def permission_decision_row(rows, request_id):
    request_id = str(request_id)
    for row in rows:
        if row.get("op") == "decide" and str(row.get("request_id")) == request_id:
            return row
    return None


def effective_permission_decision(row):
    """What may be sent. Approve only for an explicit chat row with a message id.

    A timeout, a denial, or an ambiguous row is a denial. Missing is still pending.
    """
    if not isinstance(row, dict) or row.get("op") != "decide":
        return None
    decision = row.get("decision")
    source = row.get("source")
    message_id = row.get("message_id")
    if (decision == "approve" and source == "chat"
            and isinstance(message_id, str) and message_id):
        return "approve"
    if decision == "deny" and source in {"chat", "timeout"}:
        return "deny"
    if decision is not None or source is not None:
        return "deny"
    return None


def permission_grants_allow(rows, request_id):
    """True only when a recorded chat decision explicitly approves this id."""
    return effective_permission_decision(permission_decision_row(rows, request_id)) == "approve"


def append_permission_ask(directory, detail):
    request_id = str(detail["id"])

    def mutate(rows):
        for row in rows:
            if row.get("op") == "ask" and str(row.get("request_id")) == request_id:
                return row, None
        extra = {"op": "ask", "request_id": request_id, "method": detail["method"],
                 "title": detail.get("title") or "", "options": list(detail.get("options") or [])}
        return extra, extra

    return _with_permission_file(directory, mutate)


def append_permission_decision(directory, request_id, decision, *, source, message_id=None):
    """Append one decision. The first row for this id wins; a later one cannot
    upgrade a denial into an allow, and a timeout cannot approve."""
    request_id = str(request_id)
    if decision not in {"approve", "deny"}:
        raise ValueError("permission_decision")
    if source == "timeout":
        if decision != "deny":
            raise ValueError("timeout_cannot_approve")
        if message_id is not None:
            raise ValueError("timeout_cannot_approve")
    elif source == "chat":
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("permission_message")
    else:
        raise ValueError("permission_source")

    def mutate(rows):
        existing = permission_decision_row(rows, request_id)
        if existing is not None:
            return existing, None
        extra = {"op": "decide", "request_id": request_id, "decision": decision, "source": source}
        if message_id is not None:
            extra["message_id"] = message_id
        return extra, extra

    return _with_permission_file(directory, mutate)


def _allow_once_option(options):
    """The option id whose kind is allow_once. allow_always is never selected."""
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict) or option.get("kind") != "allow_once":
            continue
        option_id = option.get("optionId")
        if isinstance(option_id, str) and option_id and option_id != "allow_always":
            return option_id
    return None


def acp_permission_frame(detail, decision):
    """JSON-RPC result for one agent request. Not a new request.

    Approve selects allow_once only. Anything else is a denial: cancelled for
    a permission prompt, declined for elicitation (no form content is collected).
    fs/* and terminal/* return None so they stay unanswered.
    """
    ident = detail.get("id")
    method = detail.get("method")
    if method == "session/request_permission":
        if decision == "approve":
            option_id = _allow_once_option(detail.get("options"))
            if option_id is None:
                outcome = {"outcome": "cancelled"}
            else:
                outcome = {"outcome": "selected", "optionId": option_id}
        else:
            outcome = {"outcome": "cancelled"}
        return {"jsonrpc": "2.0", "id": ident, "result": {"outcome": outcome}}
    if method == "elicitation/create":
        return {"jsonrpc": "2.0", "id": ident, "result": {"outcome": {"outcome": "declined"}}}
    return None


def _remember_permission_sent(state, lock, detail, decision, source, frame):
    outcome = (frame.get("result") or {}).get("outcome") or {}
    sent = outcome.get("outcome")
    if sent == "selected" and not (decision == "approve" and source == "chat"):
        return
    item = {"request_id": str(detail["id"]), "method": detail.get("method"),
            "decision": decision, "source": source, "outcome": sent}
    if sent == "selected":
        item["option_id"] = outcome.get("optionId")
    with lock:
        rows = state.setdefault("permission_decisions", [])
        if any(row.get("request_id") == item["request_id"] for row in rows):
            return
        if len(rows) < 20:
            rows.append(item)


def _bridge_acp_permissions(proc, directory, state, lock, answered, asked_at, wait_s):
    """Surface new permission prompts and write back one result per decision.

    An unanswered prompt stays pending until `wait_s`, then a denial is written.
    Approve is sent only when the file holds an explicit chat decision.
    """
    now = time.monotonic()
    with lock:
        requests = {key: dict(value) for key, value in (state.get("acp_requests") or {}).items()}
    for key, detail in requests.items():
        if key in answered:
            continue
        method = detail.get("method")
        frame = None
        effect = None
        source = None
        if method == "session/request_permission":
            if key not in asked_at:
                asked_at[key] = now
                append_permission_ask(directory, detail)
            row = permission_decision_row(read_permission_rows(directory), key)
            if row is None and now - asked_at[key] >= wait_s:
                row = append_permission_decision(directory, key, "deny", source="timeout")
            if row is None:
                continue
            effect = effective_permission_decision(row)
            if effect is None:
                continue
            source = row.get("source") if row.get("source") in {"chat", "timeout"} else "timeout"
            if effect != "approve":
                effect = "deny"
            frame = acp_permission_frame(detail, effect)
        elif method == "elicitation/create":
            if key not in asked_at:
                asked_at[key] = now
            if now - asked_at[key] < wait_s:
                continue
            effect, source = "deny", "timeout"
            frame = acp_permission_frame(detail, "deny")
        if frame is None or effect is None:
            continue
        outcome = (frame.get("result") or {}).get("outcome") or {}
        if outcome.get("outcome") == "selected" and not (effect == "approve" and source == "chat"):
            continue
        _write_frame(proc, frame)
        answered.add(key)
        _remember_permission_sent(state, lock, detail, effect, source, frame)


def apply_acp_event(state, event):
    """Fold one ACP stdout object into the shared extraction state.

    The answer is the concatenation of `agent_message_chunk` text. A thought
    chunk is not the answer. `session/prompt`'s `stopReason` ends the turn.
    Agent requests are left pending by `acp_leave_pending`.
    """
    if not isinstance(event, dict):
        return
    if acp_agent_request(event):
        acp_leave_pending(state, event)
        note_acp_bridge_request(state, event)
        return
    pending = state.get("pending_rpc")
    if isinstance(pending, dict) and "id" in event and event.get("id") == pending.get("id"):
        if "error" in event or not isinstance(event.get("result"), dict):
            state["acp_failed"] = True
            state["pending_rpc"] = None
            return
        result = event["result"]
        method = pending.get("method")
        if method == "initialize":
            if result.get("protocolVersion") == ACP_PROTOCOL_VERSION:
                state["initialized"] = True
            else:
                state["acp_failed"] = True
        elif method == "session/new":
            session_id = result.get("sessionId")
            if isinstance(session_id, str) and session_id:
                state["acp_session_id"] = session_id
            else:
                state["acp_failed"] = True
        elif method == "session/prompt":
            state["result_seq"] = state.get("result_seq", 0) + 1
            reason = result.get("stopReason")
            if isinstance(reason, str) and reason:
                state["stop_reason"] = reason
        state["pending_rpc"] = None
        return
    if event.get("method") != "session/update":
        return
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    update = params.get("update") if isinstance(params.get("update"), dict) else {}
    if update.get("sessionUpdate") != "agent_message_chunk":
        return
    content = update.get("content") if isinstance(update.get("content"), dict) else {}
    if content.get("type") == "text" and isinstance(content.get("text"), str):
        state["result_text"] = (state.get("result_text") or "") + content["text"]


def apply_stdin_event(protocol, state, event):
    if protocol == CODEX_APP_SERVER:
        apply_codex_event(state, event)
    elif protocol == ACP:
        apply_acp_event(state, event)
    else:
        apply_stream_event(state, event)


def _acp_rpc(proc, method, params, request_id):
    """One ACP JSON-RPC request. No approval outcome and no priority."""
    frame = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    _write_frame(proc, frame)


def _acp_call(proc, state, lock, method, params, request_id):
    """Remember the id before the write so the response cannot land first."""
    with lock:
        state["pending_rpc"] = {"id": request_id, "method": method}
    _acp_rpc(proc, method, params, request_id)


def acp_session(proc, directory, spec, prompt, state, lock, stop):
    """One ACP turn on the stdin the supervisor already owns.

    initialize, then session/new, then one session/prompt whose text is a
    single text block. Client capabilities do not offer fs or terminal.
    Permission and elicitation requests are recorded by `acp_leave_pending`.
    When the spec declares `permission_bridge: chat`, a permission prompt is
    written beside this run and the JSON-RPC result is sent only after an
    explicit chat decision, or a denial when that wait expires. The mailbox
    is not read: this is not an online inject.
    """
    cwd = spec.get("cwd")
    if not isinstance(cwd, str) or cwd == "":
        raise ValueError("acp_cwd")
    if not os.path.isabs(cwd):
        cwd = os.path.abspath(cwd)
    if not isinstance(prompt, str) or prompt == "":
        raise ValueError("steer_text")
    request_id = 1
    phase = "initialize"
    prompt_sent = False
    written_at_seq = 0
    idle_since = None
    answered = set()
    asked_at = {}
    bridge = spec.get("permission_bridge") == PERMISSION_BRIDGE_CHAT
    wait_s = permission_wait_s(spec)
    try:
        while not stop.is_set():
            with lock:
                failed = bool(state.get("acp_failed"))
                initialized = bool(state.get("initialized"))
                session_id = state.get("acp_session_id")
                waiting = state.get("pending_rpc") is not None
                seq = state.get("result_seq", 0)
            if failed:
                break
            if phase == "initialize":
                if initialized:
                    phase = "new"
                elif not waiting:
                    _acp_call(proc, state, lock, "initialize", {
                        "protocolVersion": ACP_PROTOCOL_VERSION,
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False},
                            "terminal": False,
                        },
                        "clientInfo": {"name": "pangpang", "version": "0"},
                    }, request_id)
                    request_id += 1
            elif phase == "new":
                if isinstance(session_id, str) and session_id:
                    phase = "prompt"
                elif not waiting:
                    _acp_call(proc, state, lock, "session/new", {
                        "cwd": cwd,
                        "mcpServers": [],
                    }, request_id)
                    request_id += 1
            elif not prompt_sent:
                _acp_call(proc, state, lock, "session/prompt", {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                }, request_id)
                request_id += 1
                prompt_sent = True
                written_at_seq = seq
                idle_since = None
            else:
                if bridge:
                    _bridge_acp_permissions(
                        proc, directory, state, lock, answered, asked_at, wait_s)
                due, idle_since = stream_idle_due(
                    seq, written_at_seq, False, idle_since, time.monotonic())
                if due:
                    break
            stop.wait(0.05)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass


def _open_stdin_session(protocol):
    """The open-stdin loop for a protocol string the runner implements."""
    if protocol == CLAUDE_STREAM_JSON:
        return stream_json_session
    if protocol == CODEX_APP_SERVER:
        return codex_app_server_session
    if protocol == ACP:
        return acp_session
    raise ValueError("session_protocol_unsupported:" + str(protocol))


def _codex_rpc(proc, method, params, request_id):
    """One app-server message. No `jsonrpc` header (the CLI omits it) and no
    priority, approval decision, or sandbox override on a steer."""
    frame = {"method": method}
    if request_id is not None:
        frame["id"] = request_id
    if params is not None:
        frame["params"] = params
    _write_frame(proc, frame)


def codex_turn_input(text, message_id):
    """User text only. No tools, permissions, model, cwd, or priority."""
    if not isinstance(text, str) or text == "":
        raise ValueError("steer_text")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("steer_uuid")
    return [{"type": "text", "text": text}], message_id


def codex_app_server_session(proc, directory, spec, prompt, state, lock, stop):
    """Keep stdin open on `codex app-server`. The first prompt is `turn/start`.
    A later mailbox row is `turn/steer` on that same turn, or another
    `turn/start` on the same thread once the turn has completed. `thread/fork`
    is never sent. The thread id is whatever `thread/start` returns."""
    plan = codex_app_server_plan(spec.get("argv") or [])
    thread_params = dict(plan[1]) if plan else {"approvalPolicy": "never"}
    prompt_uuid = str(uuid.uuid5(
        uuid.UUID(session_id_from_execution(spec["execution_id"])), "prompt"))
    request_id = 1
    try:
        _codex_rpc(proc, "initialize", {
            "clientInfo": {"name": "pangpang", "title": "PangPang", "version": "0"},
        }, request_id)
        request_id += 1
        _codex_rpc(proc, "initialized", {}, None)
        _codex_rpc(proc, "thread/start", thread_params, request_id)
        request_id += 1
        written, seen_interrupts = set(), set()
        written_at_seq = 0
        prompt_sent = False
        awaiting_turn = False
        hold_injects = False
        idle_since = None
        while not stop.is_set():
            with lock:
                thread_id = state.get("thread_id")
                turn_id = state.get("turn_id")
                turn_active = bool(state.get("turn_active"))
                seq = state.get("result_seq", 0)
                confirmed = set(state.get("confirmed") or ())
            if turn_active:
                awaiting_turn = False
            if hold_injects and not turn_active:
                hold_injects = False
            if thread_id and not prompt_sent:
                user_input, _ = codex_turn_input(prompt, prompt_uuid)
                _codex_rpc(proc, "turn/start", {
                    "threadId": thread_id,
                    "input": user_input,
                    "clientUserMessageId": prompt_uuid,
                }, request_id)
                request_id += 1
                prompt_sent = True
                awaiting_turn = True
                written_at_seq = seq
                idle_since = None
            actions = []
            mailbox_ready = (
                prompt_sent and thread_id and not awaiting_turn and not hold_injects
                and (turn_active or seq > 0))
            if mailbox_ready:
                rows = read_steer_mailbox(directory)
                actions = steer_actions(rows, confirmed, written, seen_interrupts)
            if actions:
                written_at_seq = seq
                idle_since = None
            defer_resend = False
            for index, (kind, row) in enumerate(actions):
                if stop.is_set():
                    break
                if kind == "interrupt":
                    defer_resend = True
                    hold_injects = True
                    if turn_active and isinstance(turn_id, str):
                        _codex_rpc(proc, "turn/interrupt", {
                            "threadId": thread_id, "turnId": turn_id,
                        }, request_id)
                        request_id += 1
                    continue
                if defer_resend:
                    written.discard(row["uuid"])
                    continue
                user_input, _ = codex_turn_input(row["text"], row["uuid"])
                if turn_active and isinstance(turn_id, str):
                    _codex_rpc(proc, "turn/steer", {
                        "threadId": thread_id,
                        "expectedTurnId": turn_id,
                        "input": user_input,
                        "clientUserMessageId": row["uuid"],
                    }, request_id)
                    request_id += 1
                    continue
                _codex_rpc(proc, "turn/start", {
                    "threadId": thread_id,
                    "input": user_input,
                    "clientUserMessageId": row["uuid"],
                }, request_id)
                request_id += 1
                awaiting_turn = True
                for later_kind, later_row in actions[index + 1:]:
                    if later_kind == "user" and isinstance(later_row, dict):
                        written.discard(later_row.get("uuid"))
                break
            if not actions:
                due, idle_since = stream_idle_due(
                    seq, written_at_seq, False, idle_since, time.monotonic())
                if due and prompt_sent:
                    break
            stop.wait(0.05)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass


def _consume_stream_lines(pending, block, final=False):
    data = pending + block
    lines, start = [], 0
    while True:
        cut = data.find(b"\n", start)
        if cut < 0:
            break
        lines.append(data[start:cut])
        start = cut + 1
    rest = data[start:]
    if final and rest:
        lines.append(rest)
        rest = b""
    return rest, lines


def verify_required_outputs(required_outputs, output_dir, roots):
    """ HARD deliverable contract. Each entry MUST resolve to a real, non-empty
    file produced INSIDE this execution's output boundary (output_dir =
    $XPP_OUTPUT_DIR). A pre-existing same-named file in cwd does NOT satisfy it:
    provenance — "a file exists" is not "this run produced it". Returns per-item
    EVIDENCE only ({output, present, detail}); it NEVER decides the receipt status
    (the control plane adjudicates business success from this). Pure + local:
    O(#required_outputs) stat, no scan, no network, no watcher.

    Hardened: entries are resolved through `checked` (in allowed_roots, symlinks
    resolved). output_dir is resolved the same way, once, and then relative_to
    confines the candidate, so neither an absolute path, a `..` escape, nor a
    symlink pointing outside the boundary can be claimed as a produced deliverable.
    An unresolved output_dir (symlinked parent such as /var -> /private/var) would
    raise ValueError for a file that is actually inside the boundary."""
    results = []
    # Same resolution checked() applies to the candidate (expanduser + resolve,
    # strict=False). Once, before the loop. relative_to still confines.
    out = None if output_dir is None else Path(output_dir).expanduser().resolve(strict=False)
    for raw in required_outputs or []:
        present, detail = False, None
        try:
            if out is None:
                detail = "no_output_dir"
            else:
                cand = str(raw)
                full = Path(cand) if os.path.isabs(cand) else (out / cand)
                resolved = checked(str(full), roots, False) # in-root, symlinks resolved
                resolved.relative_to(out) # must stay inside the execution boundary
                if resolved.is_file() and resolved.stat().st_size > 0:
                    present = True
                else:
                    detail = "missing_or_empty"
        except (ValueError, OSError, RuntimeError) as error:
            detail = type(error).__name__
        # Echo the FULL contract string as identity (capped at the 512-char admission
        # bound, never 200). Two distinct declared paths can share a long common
        # prefix; a 200-char key would collide them and let one present file whitewash
        # a different missing one. The 512 cap mirrors submit_task's
        # per-item limit, so for every admissible contract this is the exact string.
        results.append({"output": str(raw)[:512], "present": present, "detail": detail})
    return results



def command(argv, timeout=15):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=timeout).returncode


def _detect_supervision(allow_pgroup=False):
    """Probe the STRONGEST process-tree containment this host can actually perform,
    by real operation — never by parsing a kernel version string.

    - Windows: Job Object (windows_job) — unconditional strong.
    - Linux: if a throwaway `systemd-run --user --scope` scope can be created AND
      its cgroup exposes cgroup.kill, this host can contain+whole-tree-kill an
      accidental process tree -> 'linux-cgroup-cooperative' (L0-gated at route).
    - macOS / Linux without cgroup: 'posix-pgroup-cooperative' ONLY when the host
      operator recorded the current risk consent (allow_pgroup). It proves just
      that the CLI's process group is empty; a setsid/double-fork descendant
      escapes it (L0-gated at route). Without consent the legacy labels remain
      ('macos-pgroup-postverify' / 'posix-process-group', always rejected), so the
      runner never silently falls back to the group tier.
    - other POSIX: 'posix-process-group'."""
    if os.name == "nt":
        return "windows-job-object"
    if sys.platform.startswith("linux") and _probe_linux_cgroup_scope():
        return "linux-cgroup-cooperative"
    if (allow_pgroup and (sys.platform == "darwin" or sys.platform.startswith("linux"))
            and _unreaped_exit_supported()):
        return PGROUP_COOPERATIVE
    if sys.platform == "darwin":
        return "macos-pgroup-postverify"
    return "posix-process-group"


def _probe_linux_cgroup_scope():
    """Real capability test: create a transient user scope running /bin/true and
    confirm its cgroup carries cgroup.kill. Best-effort; any failure => not capable
    (falls back to the always-rejected posix label). Leaves no residue."""
    if not shutil.which("systemd-run"):
        return False
    unit = "xpp-probe-" + os.urandom(4).hex()
    try:
        rc = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect",
             "--property=Delegate=yes", "--unit=" + unit, "/bin/true"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15).returncode
        if rc != 0:
            return False
    except (OSError, subprocess.SubprocessError):
        return False
    # A live scope for /bin/true is too short to introspect; instead confirm the
    # cgroup2 controller filesystem and cgroup.kill feature exist at the user
    # delegated root (the same primitive the real scope will use).
    try:
        uid = os.getuid()
        base = "/sys/fs/cgroup/user.slice/user-%d.slice/user@%d.service" % (uid, uid)
        return os.path.isdir(base) and _cgroup_kill_supported()
    except OSError:
        return False


def _cgroup_kill_supported():
    """cgroup.kill exists on any populated user scope (kernel >=5.14). Detect by
    presence on an existing session/app scope, not by version number."""
    root = "/sys/fs/cgroup/user.slice"
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            if "cgroup.kill" in filenames:
                return True
            # bound the walk — user hierarchies are shallow
            if dirpath.count("/") - root.count("/") > 4:
                dirnames[:] = []
    except OSError:
        return False
    return False


def _receipt_supervision(job, pgroup=False):
    """Label the supervision actually used for THIS run, from the job instance.
    pgroup => the consented L0 process-group tier; None => bare process group; a
    LinuxJob (has.cgroup attr) => cgroup scope; else a Windows Job Object."""
    if pgroup:
        return PGROUP_COOPERATIVE
    if job is None:
        return "posix-process-group"
    if hasattr(job, "cgroup"):
        return "linux-cgroup-cooperative"
    return "windows-job-object"


def _enforce_pinned_digests(spec):
    """Refuse to spawn when a catalog pin does not match the file that would run.
    Absent pins change nothing. A missing file is a mismatch."""
    for item in spec.get("pinned_digests") or []:
        path = str(item.get("path") or "")
        expected = str(item.get("sha256") or "")
        actual = ""
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            actual = digest.hexdigest()
        except OSError:
            actual = ""
        if not expected or actual != expected:
            raise ValueError("binary_digest_mismatch")


def probe(spec):
    binary = shutil.which(spec["argv"][0])
    if not binary:
        return {"available": False, "reason": "binary_not_found"}
    with tempfile.TemporaryFile() as out:
        result = subprocess.run(spec.get("probe_argv", [binary, "--version"]),
                                stdout=out, stderr=subprocess.STDOUT, timeout=15)
        out.seek(0)
        version = out.read(4096).decode(errors="replace")
    return {"available": result.returncode == 0, "binary": binary,
            "version": version, "os": platform.system(),
            "supervision": _detect_supervision(spec.get("allow_pgroup_cooperative") is True)}


def group_alive(pgid):
    """True unless the OS CONFIRMS no process is left in the group. A permission or
    other observation error cannot prove emptiness, so it reads as alive (fail
    closed: the run then stays unknown and keeps its claims)."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _unreaped_exit_supported():
    """Can this host observe a child's exit WITHOUT reaping it? Required by the
    group tier: while the exited leader stays an unreaped zombie its pid — which
    is also the pgid — cannot be reused, so a group signal cannot hit an unrelated
    new group. Linux: waitid(WNOWAIT). macOS (os.waitid only from Python 3.13):
    kqueue EVFILT_PROC NOTE_EXIT."""
    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        return True
    import select
    return hasattr(select, "kqueue") and hasattr(select, "KQ_NOTE_EXIT")


def _exit_watcher(pid):
    """Callable: True once child `pid` has exited, never reaping it."""
    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        def exited():
            try:
                return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
            except ChildProcessError:
                return True
        return exited
    import select
    kq = select.kqueue()
    try:
        kq.control([select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD,
                                  fflags=select.KQ_NOTE_EXIT)], 0, 0)
    except OSError:
        kq.close()
        return lambda: True # ESRCH: the child is already exiting
    done = [False]

    def exited():
        if not done[0] and kq.control(None, 1, 0):
            done[0] = True
            kq.close()
        return done[0]
    return exited


def _settle_process_group(proc, why=None):
    """Bounded group cleanup for the L0 process-group tier. The leader has exited
    but is NOT reaped yet, so SIGKILL to -pgid can only reach this run's group.
    Only after that is the leader reaped; emptiness is then observed with signal 0
    alone (never another kill, so a later reuse of the number is never signalled).
    True only when the OS confirms the group is empty — which proves nothing about
    descendants that left the group (setsid / double fork).

    EPERM from that SIGKILL means no member could be signalled (POSIX kill(2)).
    macOS returns it when the only member is this run's own unreaped zombie
    leader. It is not taken as proof of anything: the leader is reaped and the
    signal-0 check below decides, where a member we may not signal still reads
    as alive.

    `why`, when given, receives the steps that produced False so the receipt can
    name them. Diagnostics only: the result is the same with or without it."""
    steps = []
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        steps.append("killpg_EPERM")
    except OSError as error:
        if why is not None:
            why.append("killpg_" + errno.errorcode.get(error.errno, str(error.errno)))
        return False
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        if why is not None:
            why.extend(steps + ["leader_wait_timeout"])
        return False
    for _ in range(30):
        if not group_alive(proc.pid):
            return True
        time.sleep(.1)
    if why is not None:
        why.extend(steps)
        # group_alive() folds "a member exists" and "cannot observe" into True;
        # one more signal-0 read tells them apart for the receipt.
        try:
            os.killpg(proc.pid, 0)
            why.append("group_alive_after_3s")
        except ProcessLookupError:
            why.append("group_empty_after_deadline")
        except OSError as error:
            why.append("group_probe_" + errno.errorcode.get(error.errno, str(error.errno)))
    return False


# --- OS process facts -------------------
# The coordinator observes a DETACHED attempt through the OS, in-process: no
# spawn. A bare pid is NOT attempt identity (it is reused); callers combine
# these facts with attempt fencing (cmdline match / start-time window) before
# drawing a liveness conclusion.

def process_alive(pid):
    """Three-state OS liveness. Returns:
      True — a process with this pid exists right now (OS-confirmed);
      False — CONFIRMED gone (the OS says no such process);
      None — UNAVAILABLE: the observation itself failed (permission/handle/OS
               error), so liveness is unknowable this instant. A None must NEVER
               be treated as 'dead' by callers (that would manufacture a spurious
               unknown); it is an observation failure -> keep observing.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        ERROR_INVALID_PARAMETER = 87 # no such pid -> confirmed dead
        ERROR_ACCESS_DENIED = 5 # exists but not queryable -> alive
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            # Distinguish a live process from a signalled-exit zombie handle.
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            if not ok:
                return None # could not read exit state: unavailable, not dead
            STILL_ACTIVE = 259
            return code.value == STILL_ACTIVE
        err = ctypes.get_last_error()
        if err == ERROR_INVALID_PARAMETER:
            return False # no such process: confirmed dead
        if err == ERROR_ACCESS_DENIED:
            return True # exists, owned by another context
        return None # any other OpenProcess failure: unavailable
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False # confirmed gone
    except PermissionError:
        return True # exists but owned by someone else
    except OSError:
        return None # observation failed: unavailable, not dead
    return True


def process_started_at(pid):
    """Best-effort process start time (unix seconds); None when unavailable.
    Fences pid reuse: a reused pid's start time falls outside the attempt window."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None

        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        try:
            creation, exit_t, kernel_t, user_t = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                            ctypes.byref(kernel_t), ctypes.byref(user_t)):
                return None
            ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            # 100ns ticks since 1601-01-01 -> unix seconds
            return ft / 10_000_000.0 - 11_644_473_600.0
        finally:
            kernel32.CloseHandle(handle)
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            raw = f.read().decode("ascii", errors="replace")
        # field 22 (starttime, clock ticks since boot); comm (field 2) may hold
        # spaces/parens, so split after the last ')'. rest[0] == state (field 3).
        rest = raw.rsplit(")", 1)[1].split()
        hz = os.sysconf("SC_CLK_TCK")
        btime = None
        with open("/proc/stat", "rb") as f:
            for line in f:
                if line.startswith(b"btime "):
                    btime = int(line.split()[1])
                    break
        if btime is None:
            return None
        return btime + int(rest[19]) / hz
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def process_cmdline(pid):
    """argv list, or None when the platform does not expose it cheaply."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            raw = f.read(8192)
        args = [a.decode("utf-8", errors="replace") for a in raw.split(b"\0") if a]
        return args or None
    except OSError:
        return None


def is_supervisor_cmdline(args, directory):
    """Definitive local attempt identity: the supervisor's argv is exactly
    [python, <...>/runner.py, run, <directory>], and the run directory is unique
    per execution_id — so a cmdline match cannot be a reused pid (A4)."""
    if not args or len(args) < 4:
        return False
    if os.path.basename(args[1]).replace(".py", "") != "runner" or args[2] != "run":
        return False
    try:
        return os.path.realpath(args[3]) == os.path.realpath(str(directory))
    except OSError:
        return False


def request_cancel(directory):
    """Durable cancel for one attempt. The first call creates the file; a later
    call leaves it in place, so the grace clock (first observation inside the
    running supervisor) is not moved and the signal is not repeated."""
    path = Path(directory) / "cancel"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def _signal_group(pid, sig):
    """Signal the executor's process group. ProcessLookupError means it is
    already gone. Any other OS error propagates: a failed signal is not a stop."""
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return False
    return True


def supervise(directory):
    # A later supervise() does not adopt this attempt: it does not spawn, and it
    # does not signal. The process that launched the executor owns the cancel
    # escalation through SIGKILL. A coordinator restart leaves that process
    # running; killing a recorded pgid from a new process is not safe (the id
    # can be reused once the leader is reaped).
    if any((directory / name).exists() for name in ("supervisor.json", "started.json", "receipt.json")):
        return # pre-claim runner upgrade: never take over an existing attempt
    # Durable, never-reclaimed fence: even a direct/repeated `run` cannot spawn
    # another side-effect process for this execution. A crashed owner requires
    # reconciliation, never an automatic takeover.
    try:
        with (directory / "supervisor.claim").open("x", encoding="utf-8") as claim:
            json.dump({"pid": os.getpid()}, claim)
            claim.flush()
            os.fsync(claim.fileno())
    except FileExistsError:
        return
    spec = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
    execution_id = spec["execution_id"]
    attempt_uuid = spec.get("attempt_uuid")
    receipt = {"execution_id": execution_id, "spec_digest": spec["spec_digest"],
               "attempt_uuid": attempt_uuid, "started": time.time()}
    # Self-report supervisor identity EARLY (before the executor spawns): a
    # coordinator that lost the start response can still fence this attempt's
    # liveness and adopt it.
    write_json(directory / "supervisor.json",
               {"pid": os.getpid(), "execution_id": execution_id, "attempt_uuid": attempt_uuid})
    acquired, proc, job = [], None, None
    pgroup = False
    session = reader = stderr_reader = None
    confirmed = True
    try:
        # A pinned binary or guard that does not match its catalog digest never starts.
        _enforce_pinned_digests(spec)
        # Decide the supervision BEFORE any side effect (resource acquire, CLI
        # spawn). Re-detected here at spawn (never trust only the cached probe).
        # A tier that can never confirm its scope, or one weaker than the floor
        # frozen at submit, is refused outright: running first and reporting
        # unknown afterwards cannot undo what the CLI already wrote;
        # — no silent downgrade, e.g. cgroup -> process group).
        floor = spec.get("supervision_floor")
        # The group tier needs BOTH the host consent and a frozen floor: a spec
        # without a floor (older control side, previously task) was never routed on the
        # group tier, so falling back to it would be a silent downgrade.
        tier = _detect_supervision(spec.get("allow_pgroup_cooperative") is True and floor is not None)
        if floor is not None and floor not in SUPERVISION_RANK:
            raise ValueError("supervision_floor_invalid") # unknown != no floor
        if tier not in SUPERVISION_RANK or SUPERVISION_RANK[tier] < SUPERVISION_RANK.get(floor, 0):
            raise ValueError("supervision_unavailable:" + str(tier))
        roots = spec["allowed_roots"]
        check_control_separation(roots, spec.get("control_roots", [str(directory)]))
        cwd = checked(spec["cwd"], roots)
        for item in spec.get("inputs", []):
            if file_hash(checked(item["path"], roots)) != item["digest"]:
                raise ValueError("source_changed")
        for resource in spec.get("resources", []):
            if command(resource["acquire_argv"]):
                raise ValueError("resource_busy")
            acquired.append(resource)
            write_json(directory / "claims.json", {"execution_id": execution_id, "keys": [x["key"] for x in acquired]})
        # Dedicated output dir (primary harvest channel). Lives under the task
        # cwd so it stays inside allowed_roots — artifacts must be re-readable
        # via the same checked() boundary when the user later downloads them.
        # A per-execution subdir keeps concurrent tasks in the same cwd from
        # cross-attributing each other's output. Best-effort: a read-only cwd
        # (verified_read task) can't create it — we degrade to no output_dir
        # rather than fail an otherwise-valid run.
        output_dir = None
        try:
            candidate = execution_output_dir(cwd, execution_id)
            candidate.mkdir(parents=True, exist_ok=True)
            # Re-validate AFTER creation: reject if.xpp-out was pre-seeded as a
            # symlink/junction pointing out of (or elsewhere within) the roots,
            # which would otherwise let the executor write — and us harvest —
            # outside the intended per-execution sandbox.
            output_dir = checked(str(candidate), roots)
        except (ValueError, OSError, RuntimeError):
            output_dir = None
        #: argv/stdin carry the prompt, so bind the real output dir into it here —
        # after output_dir is known and before the executor is launched.
        prompt = bind_output_dir(spec["prompt"], output_dir)
        turn_text = prompt
        acted_interrupts, sent_uuids = set(), set()
        # cwd first-layer snapshot for the fallback: only files that appear
        # AFTER the run (and match a deliverable extension) get auto-collected,
        # so pre-existing source/input files are never treated as deliverables.
        # If the snapshot fails we DISABLE the fallback (cwd_before=None) rather
        # than treat every pre-existing media/doc file as newly produced.
        try:
            cwd_before = {p.name for p in cwd.iterdir()}
        except OSError:
            cwd_before = None
        env_keys = {"PATH", "HOME", "USERPROFILE", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL"}
        env_keys.update(spec.get("env_allowlist", []))
        env = {key: value for key, value in os.environ.items()
               if key in env_keys and key not in {"PANGPANG_TOKEN", "XIAN_PANGPANG_TOKEN"}}
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        env.update({key: value for key, value in spec.get("extra_env", {}).items()
                    if not key.startswith(("PANGPANG_", "XIAN_PANGPANG_"))})
        # Set XPP_OUTPUT_DIR *after* extra_env so a stray same-named config entry
        # cannot redirect the executor away from the harvested output dir.
        if output_dir is not None:
            env["XPP_OUTPUT_DIR"] = str(output_dir)
        injected = []
        if spec.get("secret_refs"):
            from vault import Vault
            vault = Vault(spec["vault_path"])
            try:
                for name, ref in spec["secret_refs"].items():
                    value = vault.get(ref)
                    env[name] = value
                    injected.append(value)
            finally: vault.close()
        truncated = [False]
        while True:
            continuing = (directory / SESSION_RESUME_MARK).is_file()
            if continuing and (directory / "job-assigned").exists():
                (directory / "job-assigned").unlink()
            # Captured before a cgroup wrapper replaces argv. The child inherits
            # this pipe and execs the same executor_argv, so the session still
            # speaks to the CLI.
            cli_argv = executor_argv(spec, turn_text, resume=continuing)
            session_protocol = stdin_session_protocol(spec, cli_argv)
            argv = list(cli_argv)
            scope_unit = None
            if os.name == "nt":
                from windows_job import Job
                job = Job()
                argv = [sys.executable, str(Path(__file__).resolve()), "child", str(directory)]
            elif tier == PGROUP_COOPERATIVE:
                # L0 process-group tier: start_new_session below makes the CLI the
                # leader of its own group; cleanup is _settle_process_group.
                pgroup = True
            elif tier == "linux-cgroup-cooperative":
                # L0 cooperative strong-ish containment: run the executor inside a
                # systemd --user transient scope so cgroup.kill can whole-tree-kill it.
                # The RuntimeMaxSec backstop reaps a leaked scope if BOTH runner and
                # coordinator vanish (a systemd scope, unlike a Windows Job, does not
                # die with its launcher).
                # The payload is the same gated child Windows already uses. It does not
                # exec the CLI until job-assigned exists, and that file is created only
                # after resolve() below. A short command therefore cannot exit — and
                # --collect cannot drop the unit — before the scope cgroup is known.
                from linux_job import LinuxJob, scope_argv
                scope_unit = "xpp-" + execution_id
                backstop = int(spec.get("timeout_s", 3600)) + 300
                argv = scope_argv(scope_unit, runtime_max_sec=backstop) + [
                    sys.executable, str(Path(__file__).resolve()), "child", str(directory)]
                job = LinuxJob(scope_unit)
                # `systemd-run --user` must reach the caller's user bus to register the
                # scope. Those vars are NOT in the executor env allowlist above, so add
                # them for the wrapped launch. Under --scope the executor shares this
                # env, but XDG_RUNTIME_DIR is not a security boundary (a same-UID process
                # can construct /run/user/$UID/bus itself) and in L0 the executor is
                # already trusted — so this is not an isolation regression.
                for _bus in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
                    if os.environ.get(_bus):
                        env[_bus] = os.environ[_bus]
            stream_state = {"result_text": None, "confirmed": set()}
            stream_lock = threading.Lock()
            stop_session = threading.Event()
            session = None
            reader = None
            stderr_reader = None
            # An open-stdin protocol keeps stderr off the stdout parser. Other
            # executors stay on the merged pipe they already had.
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE if session_protocol else subprocess.STDOUT,
                                    start_new_session=os.name != "nt", env=env)
            confirmed = False
            if job:
                job.assign(proc)
                # Windows: AssignProcessToJobObject has already contained this pid, so
                # the child may launch the CLI now. Linux assign() does not contain
                # anything; touching here would release the CLI before the scope exists.
                if scope_unit is None:
                    (directory / "job-assigned").touch()
            # Ready gate: the child is waiting. Resolve the scope's real cgroup path
            # before releasing it to exec the CLI. If resolution fails, kill the
            # launched process group and fail closed. Do not start the CLI, and do
            # not fall through to a process group.
            if scope_unit is not None and not job.resolve():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                raise ValueError("cgroup_scope_unresolved:" + str(getattr(job, "resolve_error", None)))
            if scope_unit is not None:
                (directory / "job-assigned").touch()
            started = dict(receipt, launcher_pid=proc.pid)
            if scope_unit is not None:
                started.update(supervision="linux-cgroup-cooperative", unit=job.unit,
                               control_group=str(job.cgroup))
            else:
                started.update(pid=proc.pid, pgid=proc.pid, started_at=process_started_at(proc.pid))
                if pgroup:
                    started["supervision"] = PGROUP_COOPERATIVE
            write_json(directory / "started.json", started)
            log_lock = threading.Lock()
            def drain(stream, parse):
                total = 0
                pending = b""
                if injected:
                    from redaction import Redactor
                    redactor = Redactor(injected)
                else:
                    redactor = None
                with open(directory / "output.log", "ab") as out:
                    while True:
                        # read1, not read(n). A non-interactive pipe's read(n) keeps
                        # issuing raw reads until n bytes or EOF, so a JSON-RPC
                        # handshake shorter than n never reaches the session while
                        # the process is alive. read1 returns one raw read.
                        block = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                        finished = not block
                        if parse and block:
                            pending, lines = _consume_stream_lines(pending, block, final=False)
                            if lines:
                                with stream_lock:
                                    for line in lines:
                                        event = _json_object(line)
                                        if event is not None:
                                            apply_stdin_event(session_protocol, stream_state, event)
                        logged = redactor.feed(block, final=finished) if redactor else block
                        left = max(0, spec.get("log_bytes", 4 * 1024 * 1024) - total)
                        with log_lock:
                            out.write(logged[:left])
                            out.flush()
                        total += len(logged)
                        truncated[0] |= len(logged) > left
                        if finished:
                            if parse and pending:
                                _, lines = _consume_stream_lines(pending, b"", final=True)
                                with stream_lock:
                                    for line in lines:
                                        event = _json_object(line)
                                        if event is not None:
                                            apply_stdin_event(session_protocol, stream_state, event)
                            break
            reader = threading.Thread(target=drain, args=(proc.stdout, bool(session_protocol)), daemon=True)
            reader.start()
            stderr_reader = None
            if session_protocol and proc.stderr is not None:
                stderr_reader = threading.Thread(target=drain, args=(proc.stderr, False), daemon=True)
                stderr_reader.start()
            if session_protocol:
                target = _open_stdin_session(session_protocol)
                session = threading.Thread(
                    target=target,
                    args=(proc, directory, spec, prompt, stream_state, stream_lock, stop_session),
                    daemon=True)
                session.start()
            else:
                def feed(text=turn_text, resume_turn=continuing, process=proc):
                    try:
                        if spec.get("prompt_mode", "stdin") == "stdin" or resume_turn:
                            process.stdin.write(text.encode())
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                threading.Thread(target=feed, daemon=True).start()
            pending_ids, pending_rows = None, None
            resume_signal_sent = False
            cancelled, timed_out = False, False
            # The group tier must not reap the leader before the group is killed.
            exited = _exit_watcher(proc.pid) if pgroup else (lambda: proc.poll() is not None)
            # Latched on the first look at the cancel file. A second cancel does not
            # move cancel_at or send a signal that was already sent.
            cancel_at = None
            sigint_sent = False
            sigterm_sent = False
            term_at = None
            while not exited():
                if (directory / "cancel").exists():
                    cancelled = True
                timed_out = time.time() - receipt["started"] > spec.get("timeout_s", 3600)
                # POSIX cancel: SIGINT, then SIGTERM, then the existing hard kill
                # below this loop. Windows has no CREATE_NEW_PROCESS_GROUP on the
                # child, so CTRL_BREAK_EVENT has no group to land on; cancel there
                # keeps the immediate Job Object termination. A timeout is not a
                # cancel: it still stops immediately, and it preempts a grace window.
                if cancelled and os.name != "nt" and not timed_out:
                    now = time.monotonic()
                    if cancel_at is None:
                        cancel_at = now
                    if not sigint_sent:
                        _signal_group(proc.pid, signal.SIGINT)
                        sigint_sent = True
                    elif not sigterm_sent and now - cancel_at >= CANCEL_GRACE_S:
                        _signal_group(proc.pid, signal.SIGTERM)
                        sigterm_sent = True
                        term_at = now
                    elif sigterm_sent and term_at is not None and now - term_at >= CANCEL_TERM_WAIT_S:
                        break
                    time.sleep(.05)
                    continue
                if cancelled or timed_out:
                    if job:
                        job.terminate()
                    else:
                        os.killpg(proc.pid, signal.SIGTERM)
                    if pgroup:
                        # Bounded grace; _settle_process_group force-kills whatever is left.
                        deadline = time.time() + CANCEL_TERM_WAIT_S
                        while not exited() and time.time() < deadline:
                            time.sleep(.05)
                        break
                    try:
                        proc.wait(CANCEL_TERM_WAIT_S)
                    except subprocess.TimeoutExpired:
                        if job:
                            job.terminate()
                        else:
                            os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(5)
                    break
                if (spec.get("resume_protocol") == CLI_SESSION_RESUME
                        and os.name != "nt" and not resume_signal_sent):
                    found = resume_continuation(directory, acted_interrupts, sent_uuids)
                    if found and found[1]:
                        _signal_group(proc.pid, signal.SIGINT)
                        resume_signal_sent = True
                        pending_ids, pending_rows = found
                time.sleep(.1)
            # Tier 2: the turn stopped and a sentence is waiting. Reap this
            # process, then launch `--resume` of the same derived session id.
            # Cancel and timeout do not resume. No resume protocol: one turn.
            resume_rows = None
            if (spec.get("resume_protocol") == CLI_SESSION_RESUME
                    and os.name != "nt" and not cancelled and not timed_out):
                if not pending_rows:
                    found = resume_continuation(directory, acted_interrupts, sent_uuids)
                    if found and found[1]:
                        pending_ids, pending_rows = found
                if pending_rows:
                    resume_rows = (pending_ids, pending_rows)
            if resume_rows is None:
                break
            stop_session.set()
            if session is not None:
                session.join(2)
            if reader is not None:
                reader.join(2)
            if stderr_reader is not None:
                stderr_reader.join(2)
            if pgroup:
                if not _settle_process_group(proc, []):
                    raise ValueError("process_tree_unverified")
            else:
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    raise ValueError("resume_leader_wait_timeout")
                if job is not None:
                    job.close()
                    job = None
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
            ids, rows = resume_rows
            for key in ids:
                acted_interrupts.add(key)
            turn_text = "\n".join(row["text"] for row in rows)
            for row in rows:
                sent_uuids.add(row["uuid"])
            mark_session_resume(directory)
            confirmed = False
        # A CLI may exit leaving tool children behind. Terminate and verify the
        # group before writing a terminal receipt or releasing a shared slot.
        alive = job.alive if job else lambda: group_alive(proc.pid)
        settle_why = []
        if pgroup:
            confirmed = _settle_process_group(proc, settle_why)
        elif alive():
            if job:
                job.terminate()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        for _ in range(0 if pgroup else 30):
            if not alive():
                # A Windows Job Object (KILL_ON_JOB_CLOSE) contains ALL descendants,
                # so an empty job is a true tree-termination fact. A Linux systemd
                # scope with cgroup.kill is equivalent: a child that called setsid()
                # escaped the process group but NOT the cgroup, so an empty cgroup
                # (populated 0) proves the scope's subtree is empty (L0: not against a
                # same-UID payload that migrates itself out). A bare POSIX process
                # group is NEITHER: setsid escapes it, so an empty group proves
                # nothing about escaped descendants. A bare group is refused before
                # launch; only the consented L0 group tier (settled above) confirms,
                # and only its own group scope.
                if job:
                    confirmed = True
                break
            time.sleep(.1)
        stop_session.set()
        if session is not None:
            session.join(2)
        reader.join(2)
        if stderr_reader is not None:
            stderr_reader.join(2)
        if not confirmed:
            raise ValueError("process_tree_unverified" + "".join(":" + w for w in settle_why))
        artifacts, output_warnings = [], []
        budget = {"files": HARVEST_MAX_FILES, "bytes": HARVEST_MAX_TOTAL_BYTES}
        seen_digests = set()
        # (1) Declared outputs. `outputs` is the declared file list. This loop
        # NEVER overwrites receipt.status (that stays exit-code). A relative
        # entry is satisfied by output_dir/<raw> first — the directory the
        # delivery prompt tells the executor to write — and then by cwd/<raw>.
        # A stale cwd file must not bind the declaration ahead of the fresh
        # output-dir file. declared_output_missing is recorded only when
        # neither place has the file. No other directory is consulted. No
        # freshness check: a file that is present is enough. A hit is harvested
        # here; the output_dir pass below shares seen_digests, so the same
        # bytes are not registered twice. Service.complete_task reads the
        # declared_output_missing subset when it adjudicates business success.
        for raw in spec.get("outputs", []):
            try:
                places = []
                if output_dir is not None and not os.path.isabs(str(raw)):
                    places.append(str(Path(output_dir) / str(raw)))
                places.append(str(raw) if os.path.isabs(str(raw)) else str(Path(cwd) / str(raw)))
                found = None
                for candidate in places:
                    p = checked(candidate, roots, False)
                    if p.is_file():
                        found = p
                        break
                if found is None:
                    output_warnings.append({"output": str(raw)[:200], "issue": "declared_output_missing"})
                else:
                    art = _harvest_one(found, roots)
                    if art and art["digest"] not in seen_digests and art["size"] <= budget["bytes"] and budget["files"] > 0:
                        seen_digests.add(art["digest"])
                        budget["files"] -= 1
                        budget["bytes"] -= art["size"]
                        artifacts.append(dict(art, source="declared"))
            except (ValueError, OSError, RuntimeError) as error:
                # Any per-item failure — bad label, out-of-root, symlink loop, a file
                # that vanished between is_file() and hash/stat — stays a warning of
                # its exception type. It must not escape to the outer handler, and it
                # is not declared_output_missing (that issue is the only outputs
                # warning the control plane treats as a business failure).
                output_warnings.append({"output": str(raw)[:200], "issue": type(error).__name__ + ":" + str(error)[:120]})
        # (2) Dedicated output dir — the primary channel. Full recursive harvest;
        # anything the executor wrote to $XPP_OUTPUT_DIR is a deliverable by
        # construction, so no extension filter here. Skipped if the dir could
        # not be created/validated (read-only cwd, or a rejected symlink seed).
        if output_dir is not None:
            artifacts += harvest_dir(output_dir, roots, recursive=True, source="output_dir",
                                     budget=budget, seen_digests=seen_digests)
        # (3) cwd shallow fallback — catches undeclared files (chart.png) written
        # straight to cwd. Only NEW first-layer files with a deliverable
        # extension qualify, so pre-existing source/input files are never
        # collected and a modified-in-place file is not treated as a deliverable.
        # Disabled entirely when the pre-run snapshot failed (cwd_before is None),
        # so we never mistake the whole pre-existing tree for new output.
        if cwd_before is not None:
            artifacts += harvest_dir(cwd, roots, recursive=False, source="cwd_fallback",
                                     budget=budget, seen_digests=seen_digests,
                                     new_only_names=cwd_before, exts=HARVEST_FALLBACK_EXTS)
        # (4) Required outputs — the HARD deliverable contract (distinct from the
        # soft `outputs` hint above, which NEVER gates). Evidence only; never flips
        # receipt.status (raw execution fact stays exit-code-driven). The control
        # plane (Service.complete_task) adjudicates business success from this.
        required_results = verify_required_outputs(spec.get("required_outputs", []), output_dir, roots)
        archive_artifacts(directory, artifacts, roots, execution_id)
        if session_protocol == ACP:
            # Not an interjection. The pending list is the hook's record
            # (acp_leave_pending): requests that were not answered.
            with stream_lock:
                result_text = stream_state.get("result_text")
                pending = list(stream_state.get("acp_pending") or ())
                acp_session_id = stream_state.get("acp_session_id")
                stop_reason = stream_state.get("stop_reason")
                decisions = list(stream_state.get("permission_decisions") or ())
            if injected and isinstance(result_text, str):
                for secret in injected:
                    result_text = result_text.replace(secret, "[redacted]")
            receipt["session_protocol"] = ACP
            if isinstance(acp_session_id, str) and acp_session_id:
                receipt["acp_session_id"] = acp_session_id
            if isinstance(stop_reason, str) and stop_reason:
                receipt["acp_stop_reason"] = stop_reason
            # A prompt whose result was actually written is not still pending.
            # An unanswered one stays on acp_pending and is not a decision.
            if decisions:
                answered_ids = {str(item.get("request_id")) for item in decisions}
                pending = [item for item in pending if str(item.get("id")) not in answered_ids]
                receipt["permission_decisions"] = decisions
            if pending:
                receipt["acp_pending"] = pending
            if isinstance(result_text, str):
                receipt["result_text"] = result_text[-65536:]
        elif session is not None:
            with stream_lock:
                confirmed_ids = set(stream_state["confirmed"])
                result_text = stream_state.get("result_text")
            if injected and isinstance(result_text, str):
                for secret in injected:
                    result_text = result_text.replace(secret, "[redacted]")
            steer_rows = [row for row in read_steer_mailbox(directory)
                          if row.get("op") == "user" and isinstance(row.get("uuid"), str)]
            receipt["interjection_tier"] = 1
            receipt["injects"] = [{"uuid": row["uuid"], "delivered": row["uuid"] in confirmed_ids}
                                  for row in steer_rows]
            if isinstance(result_text, str):
                receipt["result_text"] = result_text[-65536:]
        elif spec.get("resume_protocol") == CLI_SESSION_RESUME:
            # Not an online inject. `resumed` means the sentence was the stdin
            # of a later `--resume` of the derived session id. No delivery ack.
            steer_rows = [row for row in read_steer_mailbox(directory)
                          if row.get("op") == "user" and isinstance(row.get("uuid"), str)]
            receipt["interjection_tier"] = 2
            receipt["session_id"] = session_id_from_execution(execution_id)
            receipt["resumed"] = [{"uuid": row["uuid"], "resumed": row["uuid"] in sent_uuids}
                                  for row in steer_rows]
        receipt.update(status="cancelled" if cancelled else "failed" if timed_out or proc.returncode else "succeeded",
                       exit_code=proc.returncode, artifacts=artifacts, output_warnings=output_warnings[:20],
                       required_output_results=required_results,
                       truncated=truncated[0], timed_out=timed_out)
    except Exception as error:
        # Avoid propagating command output or credentials into diagnostic data.
        receipt.update(status="failed" if confirmed else "unknown", error=type(error).__name__ + ":" + str(error)[:200])
    finally:
        # Close only streams no thread is blocked in. close() takes the same
        # buffer lock as read()/write(); a setsid child that inherited the
        # pipe keeps read() parked, and closing from here would deadlock
        # before the receipt is durable. Process exit still drops the fd.
        if proc is not None:
            idle_streams = []
            if reader is None or not reader.is_alive():
                idle_streams.append(proc.stdout)
            if stderr_reader is None or not stderr_reader.is_alive():
                idle_streams.append(proc.stderr)
            if session is None or not session.is_alive():
                idle_streams.append(proc.stdin)
            for stream in idle_streams:
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if job:
            job.close()
        if pgroup and proc is not None and proc.returncode is None:
            # Error path after launch: the leader is not reaped, so the pgid is
            # still ours. Best-effort stop; the run stays unknown regardless.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        if confirmed:
            for resource in reversed(acquired):
                try:
                    if command(resource["release_argv"]):
                        confirmed = False
                except Exception:
                    confirmed = False
        # process_tree_confirmed = "this attempt may release its claims and commit
        # its terminal state". Its SOURCE is read from the other fields: here, a
        # runner cleanup proof (`supervision` + `termination_evidence`) or an
        # explicit `not_launched`; elsewhere operator_resolution. Never "every
        # descendant ended".
        receipt.update(ended=time.time(), process_tree_confirmed=confirmed,
                       supervision=_receipt_supervision(job, pgroup))
        if proc is None:
            # The CLI was never spawned: nothing to terminate (explicit, never
            # inferred from a missing field).
            receipt["not_launched"] = "runner_prelaunch"
        if job is not None and getattr(job, "cgroup", None) is not None and confirmed:
            # L0 contract: populated 0 after cgroup.kill proves the scope's cgroup
            # subtree is empty. Record HOW it was proven so a reader never mistakes
            # this for adversarial (L1/L2) isolation.
            receipt["termination_evidence"] = "cgroup-v2-populated-zero"
        if pgroup and proc is not None and confirmed:
            # Only the CLI's own process group is proven empty; an escaped
            # descendant may still run and write.
            receipt["termination_evidence"] = "pgroup-empty"
        if not confirmed:
            receipt["status"] = "unknown"
        write_json(directory / "receipt.json", receipt)


def read_json_bounded(path, limit=RECEIPT_MAX_BYTES):
    """Read a small JSON object file with regular-file + size guards. Never
    raises: a failed observation must look like 'no fact', not crash the
    observer. Symlinks are refused (read hardening)."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return None
    if not statmod.S_ISREG(st.st_mode) or st.st_size > limit:
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
        if len(raw) > limit:
            return None
        value = json.loads(raw)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_tail_bounded(path, n=16384):
    """Bounded tail read (O(1) seek; never the whole growing log)."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return None
    if not statmod.S_ISREG(st.st_mode):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - n))
            return f.read(n).decode("utf-8", errors="replace")
    except OSError:
        return None


def inspect(directory, *, include_tail=False):
    """Pure, side-effect-free observation of a run directory. Called
    in-process for local transports — no subprocess spawn per poll.

    Returns a receipt-shaped dict. No receipt on disk is reported as
    {'status': 'unknown'} WITHOUT identity fields: that is the observation
    ABSENCE state, not a runner verdict, and callers must never treat a bare
    'unknown' as a fact. A runner-written receipt always carries
    execution_id + spec_digest (and, for new attempts, attempt_uuid)."""
    result = {"status": "unknown"}
    receipt = read_json_bounded(str(directory / "receipt.json"))
    if receipt is not None:
        status = receipt.get("status")
        if (isinstance(status, str) and status in {"succeeded", "failed", "cancelled", "unknown"}
                and isinstance(receipt.get("execution_id"), str)
                and isinstance(receipt.get("spec_digest"), str)):
            result.update(receipt)
    if include_tail:
        # An open-stdin attempt stores its final text on the receipt. The raw
        # event log is not that result.
        extracted = result.get("result_text")
        if isinstance(extracted, str):
            result["tail"] = extracted
        else:
            tail = read_tail_bounded(str(directory / "output.log"))
            if tail is not None:
                result["tail"] = tail
    return result


def _stdin_json():
    """Read a JSON payload from stdin as UTF-8, independent of the process locale.
    execution._run sends UTF-8 bytes (execution.py); decoding stdin with the system
    codec instead (cp936/GBK on a Chinese Windows host) corrupts non-ASCII payloads
    into surrogates and crashes downstream (e.g. an inject's text.encode()), which
    surfaces as runner_rejected_request with no mailbox file written."""
    return json.loads(sys.stdin.buffer.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run", "start", "child", "inspect", "cancel", "inject", "probe", "hash", "read", "write",
                                           "receive", "list", "artifact_read"])
    parser.add_argument("directory", nargs="?")
    args = parser.parse_args()
    if args.action == "artifact_read":
        spec = _stdin_json()
        sha = spec.get("digest", "")
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("invalid_artifact_digest")
        directory = Path(args.directory).resolve()
        path = checked(str(directory / "artifacts" / sha), [str(directory)])
        result = read_file_bytes(path)
        if result["digest"] != sha:
            raise ValueError("snapshot_digest_mismatch")
    elif args.action in {"probe", "hash", "read", "write", "receive", "list"}:
        spec = _stdin_json()
        if args.action == "probe":
            result = probe(spec)
        elif args.action == "receive":
            result = receive_file(spec)
        elif args.action == "list":
            result = list_dir(spec)
        else:
            p = checked(spec["path"], spec["allowed_roots"], args.action != "write")
            if args.action == "write":
                # Exclusive create only. Never overwrite a user's working file.
                data = base64.b64decode(spec["data"], validate=True)
                with open(p, "xb") as out:
                    out.write(data)
                result = {"path": str(p), "digest": file_hash(p)}
            elif args.action == "hash":
                result = {"path": str(p), "digest": file_hash(p), "size": p.stat().st_size}
            else:
                result = read_file_bytes(p)
    else:
        directory = Path(args.directory).resolve()
        if args.action == "child":
            # Outlast LinuxJob.resolve. Its budget is 5s, and one systemctl
            # call inside that loop uses timeout=10, so resolve can return
            # after this process has already started. Exiting 125 first makes
            # the receipt report a CLI that never ran.
            deadline = time.monotonic() + 5.0 + 10
            while True:
                if (directory / "job-assigned").exists():
                    spec = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
                    # Launched with the supervisor's env, so the bound dir is available
                    # here too — the child must not hand the CLI a raw $XPP_OUTPUT_DIR.
                    bound = bind_output_dir(spec["prompt"], os.environ.get("XPP_OUTPUT_DIR"))
                    argv = executor_argv(
                        spec, bound, resume=(directory / SESSION_RESUME_MARK).is_file())
                    # Inherit cwd: supervise() launched this child with
                    # cwd=checked(spec["cwd"]). The raw spec value may be
                    # unexpanded (~) or re-resolve differently by now.
                    sys.exit(subprocess.call(argv))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(.1, remaining))
            sys.exit(125)
        if args.action == "run":
            supervise(directory)
            return
        if args.action == "start":
            # The claim is durable BEFORE Popen. Losing the ACK or dying between
            # claim and spawn cannot cause a second launch on retry.
            try:
                with (directory / "start.claim").open("x", encoding="utf-8") as claim:
                    json.dump({"claimed_at": time.time()}, claim)
                    claim.flush()
                    os.fsync(claim.fileno())
            except FileExistsError:
                result = read_json_bounded(directory / "start.json") or {"status": "starting"}
            else:
                # Upgrade fence: pre-claim runners may already exist. Any legacy
                # launch evidence forbids creating a second supervisor as well.
                legacy = read_json_bounded(directory / "supervisor.json")
                if legacy or any((directory / name).exists() for name in ("started.json", "receipt.json", "supervisor.claim")):
                    result = {"status": "starting"}
                else:
                    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", str(directory)],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            start_new_session=os.name != "nt")
                    result = {"supervisor_pid": proc.pid}
                with (directory / "start.json").open("x", encoding="utf-8") as ack:
                    json.dump(result, ack)
                    ack.flush()
                    os.fsync(ack.fileno())
        elif args.action == "cancel":
            request_cancel(directory)
            result = {"status": "cancel_requested"}
        elif args.action == "inject":
            result = accept_inject(directory, _stdin_json())
        else:
            # Remote (SSH) inspect keeps the legacy always-tail behaviour: the
            # 16KB tail transfer is part of the remote RPC contract. Local
            # transports call inspect() in-process with tail only at settle.
            result = inspect(directory, include_tail=True)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
