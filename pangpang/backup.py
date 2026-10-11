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
_ALLOWED_PARTS = frozenset({
    "bots.db", "connector.db", "vault/master.key", "vault/secrets.db", "adapters.json",
})


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
    runs = _read_rows(db_path, "local_run")
    open_runs = [{"id": row.get("id"), "status": row.get("status")}
                 for row in runs if not _local_run_settled(row)]
    if open_runs:
        blocked.append("local_run_not_terminal")
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
        "local_run_nonterminal": open_runs,
        "ssh_host_ids": ssh_hosts,
        "semantic_gate_mode": mode,
        "device_instance_ids": [row.get("instance_id") for row in identities if row.get("instance_id")],
        "host_ids": [host.get("id") for host in hosts if isinstance(host, dict) and host.get("id")],
    }, blocked


def _local_run_settled(row):
    """Acknowledged is not settlement. The receipt must be a confirmed terminal.

    The connector also marks an unconfirmed receipt acknowledged so it will
    not be uploaded again. That run stays open until the receipt proves a
    terminal outcome.
    """
    if row.get("status") != "acknowledged":
        return False
    receipt = row.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("status") not in TERMINAL_TASK:
        return False
    task = row.get("task") if isinstance(row.get("task"), dict) else {}
    from .runner import receipt_proof_ok
    return receipt_proof_ok(receipt, task.get("supervision_floor")) is True


def _part(root, relative):
    path = root / relative
    return {"path": relative, "sha256": _sha256(path), "bytes": path.stat().st_size}


def create_consistent_backup(data, dest, *, confirm=False, interactive=None, now=None):
    """Copy the recoverable set into a new directory and write a manifest last.

    ``upgrade_allowed`` is false when a non-terminal task, a connector run
    without a confirmed terminal receipt, an SSH host, or a non-off
    semantic-gate mode is present. Those rows are left unchanged.
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
        _restrict_directory(partial)
        try:
            database = partial / _DB_NAME[role]
            _copy_sqlite(data / _DB_NAME[role], database)
            vault_dir = partial / "vault"
            vault_dir.mkdir(mode=0o700)
            _restrict_directory(vault_dir)
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


def _windows_acl():
    return os.name == "nt"


def _restrict_directory(path):
    """Same account boundary as the vault directory. POSIX mode, Windows ACL."""
    if _windows_acl():
        from .vault import _secure_windows_directory
        _secure_windows_directory(path)
    else:
        os.chmod(path, 0o700)


def _part_name_ok(name):
    return isinstance(name, str) and name in _ALLOWED_PARTS


def _part_file(root, name):
    """A whitelisted file under ``root``. Rejects links and any other name."""
    if not _part_name_ok(name):
        raise BackupRefusal("manifest_invalid", "清单部件不在允许列表内。")
    current = Path(root)
    for piece in name.split("/"):
        current = current / piece
        if current.is_symlink():
            raise BackupRefusal("manifest_invalid", "清单部件是符号链接，已拒绝：%s" % name)
    if not current.is_file():
        raise BackupRefusal("checksum_mismatch", "组件校验失败：%s。不会写入目标数据目录。" % name)
    root_resolved = Path(root).resolve()
    resolved = current.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise BackupRefusal("manifest_invalid", "清单部件不在备份目录内。")
    return current


def _load_manifest(backup):
    path = Path(backup) / "MANIFEST.json"
    if path.is_symlink() or not path.is_file():
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
    raw_parts = manifest.get("parts")
    if not isinstance(required, list) or not isinstance(raw_parts, list):
        raise BackupRefusal("manifest_invalid", "清单部件格式不正确。")
    if any(not _part_name_ok(name) for name in required):
        raise BackupRefusal("manifest_invalid", "清单部件不在允许列表内。")
    parts = {}
    for part in raw_parts:
        if not isinstance(part, dict) or not _part_name_ok(part.get("path")):
            raise BackupRefusal("manifest_invalid", "清单部件不在允许列表内。")
        parts[part["path"]] = part
    role = manifest.get("role")
    database = _DB_NAME.get(role)
    if database is None or database not in required:
        raise BackupRefusal("incomplete_backup", "清单缺少数据库或 Vault 组件，不能恢复。")
    other = "connector.db" if role == "cloud" else "bots.db"
    if other in parts or other in required:
        raise BackupRefusal("manifest_invalid", "清单同时包含两种数据库。")
    if any(name not in parts for name in required):
        raise BackupRefusal("incomplete_backup", "清单缺少数据库或 Vault 组件，不能恢复。")
    for name in ("vault/master.key", "vault/secrets.db"):
        if name not in required:
            raise BackupRefusal("incomplete_backup", "清单没有把 %s 列为恢复所必需。只恢复 SQLite 不能回滚。" % name)
    for name, part in parts.items():
        file_path = _part_file(backup, name)
        recorded = part.get("sha256")
        if not isinstance(recorded, str) or _sha256(file_path) != recorded:
            raise BackupRefusal("checksum_mismatch", "组件校验失败：%s。不会写入目标数据目录。" % name)
    manifest["parts"] = parts
    return manifest


def _restore_names(manifest):
    names = list(manifest["required_parts"])
    if "adapters.json" in manifest["parts"] and "adapters.json" not in names:
        names.append("adapters.json")
    return names


def _top_levels(names):
    tops = []
    for name in names:
        top = name.split("/", 1)[0]
        if top not in tops:
            tops.append(top)
    return tops


def _aside_names(top):
    """Files that must leave with this top-level entry so a replaced database keeps no old journal."""
    if top.endswith(".db"):
        return [top, top + "-wal", top + "-shm"]
    return [top]


def _fill_staging(backup, staging, names):
    staging.mkdir(mode=0o700)
    _restrict_directory(staging)
    for name in names:
        source = _part_file(backup, name)
        dest = staging / name
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if dest.parent != staging:
            _restrict_directory(dest.parent)
        shutil.copy2(source, dest)
        if not _windows_acl():
            os.chmod(dest, 0o600)


def _rollback_swap(data, aside, tops, moved_out):
    """Put aside files back. Return False when a previous file could not be returned.

    A top still in the target and absent from ``moved_out`` never left. It is
    the original file, not a file brought in from the staging directory.
    """
    restored = True
    moved = set(moved_out)
    for top in tops:
        if top not in moved:
            continue
        landed = data / top
        if landed.exists() or landed.is_symlink():
            junk = aside / (top + ".incoming")
            try:
                if junk.exists() or junk.is_symlink():
                    if junk.is_dir() and not junk.is_symlink():
                        shutil.rmtree(junk)
                    else:
                        junk.unlink()
                os.rename(landed, junk)
            except OSError:
                restored = False
    for name in reversed(moved_out):
        src = aside / name
        dest = data / name
        if not (src.exists() or src.is_symlink()):
            continue
        if dest.exists() or dest.is_symlink():
            restored = False
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.rename(src, dest)
        except OSError:
            restored = False
    return restored


def _recovery_left_behind(aside):
    """True when aside still holds a database, vault, or a vault moved aside as incoming."""
    relatives = (
        "vault/master.key", "vault/secrets.db", "bots.db", "connector.db", "adapters.json",
        "vault.incoming/master.key", "vault.incoming/secrets.db",
    )
    return any((aside / name).exists() or (aside / name).is_symlink() for name in relatives)


def _remove_tree(path):
    if path.exists() or path.is_symlink():
        shutil.rmtree(path)


def _swap_into(staging, data, tops):
    """Move verified tops into an existing directory. On failure the previous files return."""
    aside = data.parent / (data.name + ".restore-aside")
    if aside.exists():
        raise BackupRefusal("dest_exists", "未完成的恢复暂存已存在：%s。不会继续写入。" % aside)
    aside.mkdir(mode=0o700)
    _restrict_directory(aside)
    moved_out = []
    try:
        for top in tops:
            for name in _aside_names(top):
                src = data / name
                if src.exists() or src.is_symlink():
                    dest = aside / name
                    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.rename(src, dest)
                    moved_out.append(name)
        for top in tops:
            os.rename(staging / top, data / top)
    except Exception as error:
        try:
            restored = _rollback_swap(data, aside, tops, moved_out)
        except Exception as rollback_error:
            raise BackupRefusal(
                "restore_incomplete",
                "恢复没有完成。目标目录 %s 可能缺件，或与 %s 里的旧文件混合。"
                "不要使用该目录。旧文件在 %s。回滚错误：%s。前向错误：%s"
                % (data, aside, aside, rollback_error, error)) from None
        if not restored or _recovery_left_behind(aside):
            raise BackupRefusal(
                "restore_incomplete",
                "恢复没有完成。目标目录 %s 缺件，或与 %s 里的旧文件混合。"
                "不要使用该目录，先处理 %s。前向错误：%s" % (data, aside, aside, error)) from None
        try:
            _remove_tree(aside)
        except OSError as cleanup_error:
            raise BackupRefusal(
                "restore_cleanup_failed",
                "目标目录 %s 已放回恢复前的文件，但 %s 未能删除（%s）。"
                "下次恢复前先处理该目录。前向错误：%s" % (data, aside, cleanup_error, error)) from None
        raise
    try:
        _remove_tree(aside)
    except OSError as error:
        raise BackupRefusal(
            "restore_cleanup_failed",
            "恢复已写入 %s，但旧的数据库和 Vault 仍在 %s（%s）。"
            "不能把这次当作已清理完成。下次恢复前先把该目录移走或删除。"
            % (data, aside, error)) from None


def restore_consistent_backup(backup, data, *, confirm=False, interactive=None):
    """Replace the recoverable set from a verified manifest. Does not start or migrate.

    Files are written to a sibling staging directory and checked again. The
    previous directory is renamed aside only after that, and put back if the
    swap fails.
    """
    backup = Path(backup).expanduser().absolute()
    data = Path(data).expanduser().absolute()
    manifest = _load_manifest(backup)
    if interactive is None:
        interactive = sys.stdin.isatty()
    _confirm("恢复到", data, confirm, interactive)
    role = manifest.get("role")
    if data.exists() and any((data / name).exists() for name in ("bots.db", "connector.db", "vault")):
        existing = _role(data) if (data / "bots.db").is_file() or (data / "connector.db").is_file() else role
        if existing != role:
            raise BackupRefusal("role_mismatch", "目标目录的角色与备份不一致，已停止。")
    names = _restore_names(manifest)
    parent = data.parent
    parent.mkdir(parents=True, exist_ok=True)
    needed = sum((backup / name).stat().st_size for name in names)
    if shutil.disk_usage(parent).free < needed + 1024 * 1024:
        raise BackupRefusal("insufficient_space", "目标卷剩余空间不足，已停止，未写入备份。")
    staging = parent / (data.name + ".restore-partial")
    if staging.exists():
        raise BackupRefusal("dest_exists", "未完成的恢复暂存已存在：%s。不会继续写入。" % staging)
    lock = _require_stopped(data, role) if data.exists() else None
    try:
        try:
            _fill_staging(backup, staging, names)
            for name in names:
                file_path = _part_file(staging, name)
                if _sha256(file_path) != manifest["parts"][name]["sha256"]:
                    raise BackupRefusal("checksum_mismatch", "组件校验失败：%s。不会写入目标数据目录。" % name)
            if data.exists():
                _swap_into(staging, data, _top_levels(names))
                try:
                    _remove_tree(staging)
                except OSError as error:
                    raise BackupRefusal(
                        "restore_cleanup_failed",
                        "恢复已写入 %s，但暂存目录 %s 未能删除（%s）。下次恢复前先处理该目录。"
                        % (data, staging, error)) from None
            else:
                os.rename(staging, data)
        except BackupRefusal as refusal:
            try:
                _remove_tree(staging)
            except OSError as cleanup_error:
                raise BackupRefusal(
                    refusal.code,
                    "%s 另外，暂存目录 %s 未能删除：%s" % (refusal, staging, cleanup_error)) from None
            raise
        except Exception as error:
            try:
                _remove_tree(staging)
            except OSError as cleanup_error:
                raise BackupRefusal(
                    "restore_failed",
                    "恢复没有完成，目标目录未改入新文件：%s。暂存目录 %s 未能删除：%s"
                    % (error, staging, cleanup_error)) from None
            raise BackupRefusal("restore_failed", "恢复没有完成，目标目录保持原样：%s" % error) from None
        _restrict_directory(data)
        vault_dir = data / "vault"
        if vault_dir.is_dir() and not vault_dir.is_symlink():
            _restrict_directory(vault_dir)
    finally:
        if lock is not None:
            lock.close()
    return {"path": str(data), "role": role, "source_data": manifest.get("source_data")}
