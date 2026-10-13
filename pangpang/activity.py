"""Computed task activity for the planner packet and task cards.

The value is derived when a packet or snapshot is built. It is not stored.
File reads run only for local tasks, and only outside a store transaction.
Recent steps come from complete single-line JSON already written to the run
log. Fragments and unrecognized records are ignored.
"""
import json
import os
import re
import stat as statmod
import time
from pathlib import Path

from . import runner
from .store import Fault, assert_no_txn_io


_TAIL_BYTES = 16384
_DEFAULT_LOG_BYTES = 4 * 1024 * 1024
_PATH_MAX = 160
_ACTIVE = frozenset({"queued", "starting", "running", "cancel_requested"})
_REDACTED = "<redacted-path>"

# Executor tool names are mapped onto this closed set. Arbitrary names are not
# passed through, and command or message text is never copied.
_TOOLS = frozenset({"read", "write", "edit", "delete", "move", "search", "execute", "fetch", "list"})
_CLAUDE_TOOLS = {
    "Read": "read", "NotebookRead": "read",
    "Write": "write",
    "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Bash": "execute", "Shell": "execute",
    "Grep": "search", "Glob": "search",
    "LS": "list",
    "WebFetch": "fetch", "WebSearch": "fetch",
}
_PATH_KEYS = ("file_path", "path", "notebook_path")
_SECRET_PREFIXES = ("sk-", "ghp_", "AKIA")
_HEX_SECRET = re.compile(r"^[0-9a-fA-F]{32,}$")
_B64_SECRET = re.compile(r"^[A-Za-z0-9+/]{32,}={0,2}$")
_B64URL_SECRET = re.compile(r"^[A-Za-z0-9_-]{32,}$")


def activity_projection(task, *, now=None, directory=None, local=True, roots=None, cwd=None):
    """Activity for one task. Non-local tasks return status and age only."""
    moment = time.time() if now is None else float(now)
    status = task.get("status")
    age = _age_s(task, moment)
    if not local:
        return {"status": status, "age_s": age}
    assert_no_txn_io("activity_projection")
    last_log_at = None
    capped = "no"
    recent = []
    log_path = Path(directory) / "output.log" if directory else None
    if log_path is not None:
        try:
            st = os.lstat(log_path)
        except OSError:
            st = None
        if st is not None and statmod.S_ISREG(st.st_mode):
            last_log_at = int(st.st_mtime)
            if st.st_size >= _log_cap(directory):
                capped = "maybe"
            recent = _recent(log_path, cwd, st.st_size)
    return {
        "status": status,
        "age_s": age,
        "last_log_at": last_log_at,
        "observation_capped": capped,
        "recent": recent,
        "present_outputs": _present_outputs(task, cwd, roots),
    }


def attach_dependency_activity(service, dependencies, *, now=None):
    """Add target.activity on each pending task dependency already in the list.

    Task rows are loaded before any run-log read, so the store lock is not held
    across file I/O. Settled dependencies and non-task dependencies are left
    unchanged.
    """
    pending = []
    for view in dependencies or []:
        if view.get("status") != "pending" or view.get("kind") != "task":
            continue
        target = view.get("target")
        if isinstance(target, dict) and target.get("task_id"):
            pending.append(view)
    if not pending:
        return dependencies
    rows = {}
    for view in pending:
        task_id = view["target"]["task_id"]
        try:
            rows[task_id] = service.store.get("task", task_id)
        except Fault:
            rows[task_id] = None
    moment = time.time() if now is None else now
    for view in pending:
        task = rows.get(view["target"]["task_id"])
        if task:
            view["target"]["activity"] = _project_task(service, task, now=moment)
    return dependencies


def annotate_snapshot_tasks(service, tasks, *, now=None):
    """Attach activity on active local task rows. Other rows are left untouched."""
    chosen = []
    for task in tasks or []:
        if task.get("status") not in _ACTIVE:
            continue
        host = service.execution.hosts.get(task.get("host_id")) or {}
        if host.get("transport") != "local":
            continue
        chosen.append(task)
    if not chosen:
        return tasks
    moment = time.time() if now is None else now
    for task in chosen:
        task["activity"] = _project_task(service, task, now=moment)
    return tasks


def _project_task(service, task, *, now):
    host = service.execution.hosts.get(task.get("host_id")) or {}
    if host.get("transport") != "local":
        return activity_projection(task, now=now, local=False)
    return activity_projection(
        task, now=now, directory=_run_directory(service, task, host), local=True,
        roots=list(host.get("allowed_roots") or []), cwd=task.get("cwd"))


def _run_directory(service, task, host):
    execution_id = task.get("execution_id")
    if not isinstance(execution_id, str):
        return None
    try:
        return service.execution.directory(host, execution_id)
    except Fault:
        return None


def _age_s(task, now):
    status = task.get("status")
    if status == "queued":
        anchor = task.get("created")
    elif status in {"starting", "running"}:
        anchor = task.get("started")
        if anchor is None:
            anchor = task.get("created")
    else:
        anchor = task.get("started")
        if anchor is None:
            anchor = task.get("created")
    try:
        anchor = float(anchor)
    except (TypeError, ValueError):
        return 0
    return max(0, int(now - anchor))


def _log_cap(directory):
    spec = runner.read_json_bounded(str(Path(directory) / "spec.json")) if directory else None
    if not isinstance(spec, dict):
        return _DEFAULT_LOG_BYTES
    value = spec.get("log_bytes", _DEFAULT_LOG_BYTES)
    if type(value) is int and value > 0:
        return value
    return _DEFAULT_LOG_BYTES


def _present_outputs(task, cwd, roots):
    required = task.get("required_outputs")
    if not isinstance(required, list) or not required:
        return []
    sample = [item for item in required[:10]]
    output_dir = None
    if isinstance(cwd, str) and cwd and isinstance(task.get("execution_id"), str):
        output_dir = str(runner.execution_output_dir(cwd, task["execution_id"]))
    checked = runner.verify_required_outputs(sample, output_dir, list(roots or []))
    present = []
    for raw, item in zip(sample, checked):
        flag = bool(item.get("present")) if isinstance(item, dict) else False
        present.append({"path": _display_path(raw, cwd), "present": flag})
    return present


def _display_path(raw, cwd):
    cleaned = sanitize_rel_path(raw, cwd) if isinstance(raw, str) else None
    return cleaned or _REDACTED


def sanitize_rel_path(raw, cwd):
    """cwd-relative path, or None when the path is rejected.

    A secret-like segment or a joined path longer than 160 characters returns
    ``<redacted-path>`` instead of the original text.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return None
    text = raw.replace("\\", "/")
    if text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        return None
    parts = []
    for seg in text.split("/"):
        if seg in {"", "."}:
            continue
        if seg == "..":
            return None
        if _secret_segment(seg):
            return _REDACTED
        parts.append(seg)
    if not parts:
        return None
    rel = "/".join(parts)
    if len(rel) > _PATH_MAX:
        return _REDACTED
    if isinstance(cwd, str) and cwd and not _stays_in_cwd(cwd, parts):
        return None
    return rel


def _secret_segment(seg):
    if seg.startswith(_SECRET_PREFIXES):
        return True
    return bool(_HEX_SECRET.fullmatch(seg) or _B64_SECRET.fullmatch(seg) or _B64URL_SECRET.fullmatch(seg))


def _stays_in_cwd(cwd, parts):
    try:
        base = Path(cwd).resolve()
    except OSError:
        return False
    current = base
    for seg in parts:
        current = current / seg
        try:
            st = os.lstat(current)
        except OSError:
            break
        if not statmod.S_ISLNK(st.st_mode):
            continue
        try:
            current = Path(os.path.realpath(current))
        except OSError:
            return False
        if not _is_inside(current, base):
            return False
    try:
        final = current.resolve(strict=False)
    except OSError:
        return False
    return _is_inside(final, base)


def _is_inside(path, base):
    try:
        Path(path).resolve(strict=False).relative_to(base)
    except (ValueError, OSError):
        return False
    return True


def _recent(path, cwd, size):
    text = runner.read_tail_bounded(path, _TAIL_BYTES)
    if not isinstance(text, str) or not text:
        return []
    if size > _TAIL_BYTES:
        cut = text.find("\n")
        if cut < 0:
            return []
        text = text[cut + 1:]
    if not text.endswith("\n"):
        cut = text.rfind("\n")
        if cut < 0:
            return []
        text = text[:cut + 1]
    steps = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            steps.extend(_event_steps(event, cwd))
    return [step for step in steps if step.get("tool") in _TOOLS][-3:]


def _event_steps(event, cwd):
    steps = _claude_steps(event, cwd)
    if steps:
        return steps
    item = _codex_item(event)
    if item is not None:
        return _codex_steps(item, cwd)
    return _acp_steps(event, cwd)


def _claude_steps(event, cwd):
    if event.get("type") != "assistant" or not isinstance(event.get("message"), dict):
        return []
    content = event["message"].get("content")
    if not isinstance(content, list):
        return []
    steps = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool = _CLAUDE_TOOLS.get(block.get("name"))
        if not tool:
            continue
        steps.append(_step(tool, _input_path(block.get("input")), cwd))
    return steps


def _input_path(value):
    if not isinstance(value, dict):
        return None
    for key in _PATH_KEYS:
        if isinstance(value.get(key), str):
            return value[key]
    return None


def _codex_item(event):
    if event.get("type") in {"item.completed", "item.started"} and isinstance(event.get("item"), dict):
        return event["item"]
    if event.get("method") in {"item/completed", "item/started"}:
        params = event.get("params")
        if isinstance(params, dict) and isinstance(params.get("item"), dict):
            return params["item"]
    return None


def _codex_steps(item, cwd):
    kind = item.get("type")
    if kind in {"command_execution", "commandExecution"}:
        return [_step("execute", None, cwd)]
    if kind in {"file_change", "fileChange"}:
        changes = item.get("changes")
        if not isinstance(changes, list) or not changes:
            return [_step("edit", None, cwd)]
        steps = []
        for change in changes:
            path = change.get("path") if isinstance(change, dict) else None
            steps.append(_step("edit", path if isinstance(path, str) else None, cwd))
        return steps
    return []


def _acp_steps(event, cwd):
    if event.get("method") != "session/update":
        return []
    params = event.get("params")
    update = params.get("update") if isinstance(params, dict) else None
    if not isinstance(update, dict):
        return []
    if update.get("sessionUpdate") not in {"tool_call", "tool_call_update"}:
        return []
    kind = update.get("kind")
    if kind not in _TOOLS:
        return []
    locations = update.get("locations")
    if not isinstance(locations, list) or not locations:
        return [_step(kind, None, cwd)]
    steps = []
    for loc in locations:
        path = loc.get("path") if isinstance(loc, dict) else None
        steps.append(_step(kind, path if isinstance(path, str) else None, cwd))
    return steps


def _step(tool, raw_path, cwd):
    item = {"tool": tool}
    if isinstance(raw_path, str):
        cleaned = sanitize_rel_path(raw_path, cwd)
        if cleaned:
            item["path"] = cleaned
    return item
