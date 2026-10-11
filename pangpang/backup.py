"""Stop-consistent backup of one data directory.

The recoverable set is the SQLite database, the Vault master key, and the
Vault secret store. Managed config and device identity live in that database.
A copy of the database file alone is not a rollback. This module does not
start the service, does not migrate schema, and does not rewrite SSH or
semantic-gate settings. Callers must stop the coordinator and connector
first; a held process lock refuses the operation.
"""
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from .build_info import release_identity

FORMAT = "pangpang-backup-v1"
# Task rows outside this set are still in flight or not a confirmed terminal.
TERMINAL_TASK = {"succeeded", "failed", "cancelled"}
_DB_NAME = {"cloud": "bots.db", "connector": "connector.db"}
_LOCK_NAME = {"cloud": "coordinator.lock", "connector": "connector.lock"}


class BackupRefusal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lock(path):
    """Same exclusive non-blocking lock as ``pangpang.__main__.ProcessLock``."""
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise
    return handle


def _role(data):
    cloud, connector = (data / "bots.db").is_file(), (data / "connector.db").is_file()
    if cloud and connector:
        raise BackupRefusal("ambiguous_data_directory", "同一目录里同时有 bots.db 和 connector.db，无法判断角色。")
    if cloud:
        return "cloud"
    if connector:
        return "connector"
    raise BackupRefusal("data_not_found", "目录里没有 bots.db 或 connector.db。不会创建新的空数据目录。")


def _require_stopped(data, role):
    try:
        return _lock(data / _LOCK_NAME[role])
    except OSError:
        raise BackupRefusal(
            "service_running",
            "进程锁被占用。先停止协调器和连接器再备份；运行中的复制不能当作一致回滚点。") from None


def _confirm(action, data, confirm, interactive):
    if confirm:
        return
    if not interactive:
        raise BackupRefusal(
            "confirmation_required",
            "非交互环境必须传入 --confirm。不会在未确认时备份或恢复。")
    prompt = "%s %s\n再次输入该绝对路径以确认: " % (action, data)
    typed = input(prompt).strip()
    if typed != str(data):
        raise BackupRefusal("confirmation_mismatch", "输入的路径与目标不一致，已停止。")


def _copy_sqlite(source, dest):
    src = sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(dest, 0o600)


def _read_rows(db_path, kind):
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            return [json.loads(row[0]) for row in
                    connection.execute("SELECT data FROM objects WHERE kind=?", (kind,))]
        except sqlite3.Error as error:
            raise BackupRefusal("database_unreadable", "无法读取备份中的 %s：%s" % (kind, error)) from None
    finally:
        connection.close()


def _inventory(db_path):
    """Read-only facts. Does not migrate SSH hosts or semantic-gate mode."""
    blocked = []
    tasks = _read_rows(db_path, "task")
    nonterminal = [{"id": task.get("id"), "status": task.get("status")}
                   for task in tasks if task.get("status") not in TERMINAL_TASK]
    claims = _read_rows(db_path, "claim")
    # ``released`` is the only claim state that is finished. unknown stays blocked.
    open_claims = [row.get("id") for row in claims if row.get("status") != "released"]
    if nonterminal or open_claims:
        blocked.append("task_not_terminal")
    settings = _read_rows(db_path, "settings")
    managed = next((row.get("config") or {} for row in settings if row.get("id") == "managed"), {})
    if not isinstance(managed, dict):
        managed = {}
    hosts = managed.get("hosts") or []
    ssh_hosts = [host.get("id") for host in hosts if isinstance(host, dict) and host.get("transport") == "ssh"]
    if ssh_hosts:
        blocked.append("ssh_host_present")
    gate = managed.get("semantic_gate") if isinstance(managed.get("semantic_gate"), dict) else {}
    mode = str((gate or {}).get("mode", "off")).lower()
    if mode not in {"off", "shadow", "enforce"}:
        mode = "unrecognized"
    if mode != "off":
        blocked.append("semantic_gate_mode_not_off")
    identities = _read_rows(db_path, "identity")
    return {
        "task_nonterminal": nonterminal,
        "open_claim_ids": open_claims,
        "ssh_host_ids": ssh_hosts,
        "semantic_gate_mode": mode,
        "device_instance_ids": [row.get("instance_id") for row in identities if row.get("instance_id")],
        "host_ids": [host.get("id") for host in hosts if isinstance(host, dict) and host.get("id")],
    }, blocked


def _part(root, relative):
    path = root / relative
    return {"path": relative, "sha256": _sha256(path), "bytes": path.stat().st_size}


def create_consistent_backup(data, dest, *, confirm=False, interactive=None, now=None):
    """Copy the recoverable set into a new directory and write a manifest last.

    ``upgrade_allowed`` is false when a non-terminal task, an SSH host, or a
    non-off semantic-gate mode is present. Those rows are left unchanged.
    """
    data = Path(data).expanduser().absolute()
    dest = Path(dest).expanduser().absolute()
    if not data.is_dir():
        raise BackupRefusal("data_not_found", "数据目录不存在：%s" % data)
    if dest.exists():
        raise BackupRefusal("dest_exists", "备份目标已存在：%s。不会覆盖旧备份。" % dest)
    role = _role(data)
    vault_key = data / "vault" / "master.key"
    vault_db = data / "vault" / "secrets.db"
    if not vault_key.is_file() or not vault_db.is_file():
        raise BackupRefusal(
            "vault_missing",
            "缺少 vault/master.key 或 vault/secrets.db。只复制 SQLite 不能当作可回滚备份。")
    if interactive is None:
        interactive = sys.stdin.isatty()
    _confirm("备份", data, confirm, interactive)
    needed = (data / _DB_NAME[role]).stat().st_size + vault_key.stat().st_size + vault_db.stat().st_size
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(parent).free
    if free < needed + 1024 * 1024:
        raise BackupRefusal("insufficient_space", "目标卷剩余空间不足，已停止，未写入备份。")
    partial = dest.with_name(dest.name + ".partial")
    if partial.exists():
        raise BackupRefusal("dest_exists", "未完成的备份目录已存在：%s。不会继续写入。" % partial)
    lock = _require_stopped(data, role)
    try:
        partial.mkdir(mode=0o700)
        try:
            database = partial / _DB_NAME[role]
            _copy_sqlite(data / _DB_NAME[role], database)
            vault_dir = partial / "vault"
            vault_dir.mkdir(mode=0o700)
            for name in ("master.key", "secrets.db"):
                target = vault_dir / name
                shutil.copy2(data / "vault" / name, target)
                os.chmod(target, 0o600)
            adapters = data / "adapters.json"
            if adapters.is_file():
                copied = partial / "adapters.json"
                shutil.copy2(adapters, copied)
                os.chmod(copied, 0o600)
            inventory, blocked = _inventory(database)
            if now is None:
                now = datetime.datetime.now(datetime.timezone.utc)
            parts = [_part(partial, _DB_NAME[role]),
                     _part(partial, "vault/master.key"),
                     _part(partial, "vault/secrets.db")]
            if adapters.is_file():
                parts.append(_part(partial, "adapters.json"))
            manifest = {
                "format": FORMAT,
                "role": role,
                "source_data": str(data),
                "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "parts": parts,
                "required_parts": [_DB_NAME[role], "vault/master.key", "vault/secrets.db"],
                "sqlite_only_is_rollback": False,
                "version_is_rollback_identity": False,
                "rollback_requires": [
                    "database", "vault_master_key", "vault_secrets",
                    "managed_config_inside_database", "device_identity_inside_database"],
                "build": release_identity(),
                "upgrade_allowed": not blocked,
                "upgrade_blocked": blocked,
                "inventory": inventory,
            }
            manifest_path = partial / "MANIFEST.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")
            os.chmod(manifest_path, 0o600)
            os.chmod(partial, 0o700)
            partial.rename(dest)
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            raise
    finally:
        lock.close()
    return {"path": str(dest), "upgrade_allowed": not blocked, "upgrade_blocked": blocked, "manifest": manifest}


def _load_manifest(backup):
    path = Path(backup) / "MANIFEST.json"
    if not path.is_file():
        raise BackupRefusal("manifest_missing", "备份里没有 MANIFEST.json。")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise BackupRefusal("manifest_invalid", "MANIFEST.json 无法解析：%s" % error) from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupRefusal("manifest_invalid", "不是可识别的备份清单。")
    if manifest.get("sqlite_only_is_rollback") is not False:
        raise BackupRefusal("sqlite_only_not_rollback", "清单声称仅 SQLite 即可回滚，已拒绝恢复。")
    required = manifest.get("required_parts")
    parts = {part.get("path"): part for part in manifest.get("parts") or [] if isinstance(part, dict)}
    if (not isinstance(required, list) or any(name not in parts for name in required)
            or not any(name in required for name in _DB_NAME.values())):
        raise BackupRefusal("incomplete_backup", "清单缺少数据库或 Vault 组件，不能恢复。")
    for name in ("vault/master.key", "vault/secrets.db"):
        if name not in required:
            raise BackupRefusal("incomplete_backup", "清单没有把 %s 列为恢复所必需。只恢复 SQLite 不能回滚。" % name)
    for name, part in parts.items():
        file_path = Path(backup) / name
        recorded = part.get("sha256")
        if not file_path.is_file() or not recorded or _sha256(file_path) != recorded:
            raise BackupRefusal("checksum_mismatch", "组件校验失败：%s。不会写入目标数据目录。" % name)
    return manifest


def restore_consistent_backup(backup, data, *, confirm=False, interactive=None):
    """Replace the recoverable set from a verified manifest. Does not start or migrate."""
    backup = Path(backup).expanduser().absolute()
    data = Path(data).expanduser().absolute()
    manifest = _load_manifest(backup)
    if interactive is None:
        interactive = sys.stdin.isatty()
    _confirm("恢复到", data, confirm, interactive)
    role = manifest.get("role")
    if role not in _DB_NAME:
        raise BackupRefusal("manifest_invalid", "清单没有合法角色。")
    if data.exists() and any((data / name).exists() for name in ("bots.db", "connector.db", "vault")):
        existing = _role(data) if (data / "bots.db").is_file() or (data / "connector.db").is_file() else role
        if existing != role:
            raise BackupRefusal("role_mismatch", "目标目录的角色与备份不一致，已停止。")
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _require_stopped(data, role)
    try:
        for name in manifest["required_parts"]:
            target = data / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(backup / name, target)
            os.chmod(target, 0o600)
        adapters = backup / "adapters.json"
        if adapters.is_file():
            target = data / "adapters.json"
            shutil.copy2(adapters, target)
            os.chmod(target, 0o600)
        os.chmod(data, 0o700)
        vault_dir = data / "vault"
        if vault_dir.is_dir():
            os.chmod(vault_dir, 0o700)
    finally:
        lock.close()
    return {"path": str(data), "role": role, "source_data": manifest.get("source_data")}
