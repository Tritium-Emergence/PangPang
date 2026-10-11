"""Shared launch path. Documented entry: ``python -m pangpang.cli``.

``python -m pangpang.cli cloud|connector`` matches ``python launch.py cloud|connector``.
pip also installs a ``pangpang`` script (``pangpang.exe`` on Windows). That launcher
is optional; use ``-m`` if Windows blocks the exe. Neither path creates a venv or
downloads Bun or pi. A source checkout still bootstraps through :func:`source_main`
(``launch.py``).
"""
import argparse
import hashlib
import os
import secrets
import subprocess
import sys
from pathlib import Path

from .build_info import version_string
from .env_compat import env_value


def source_root():
    return Path(__file__).resolve().parents[1]


class DataLocationError(Exception):
    """Default data directory cannot be chosen without hiding an existing tree."""


# Historical source-checkout defaults. An installed package does not use these,
# because they follow the process working directory.
_LEGACY_DATA = {"cloud": "data-v2", "connector": "connector-data"}
_DATA_EVIDENCE = ("bots.db", "connector.db", "vault", "coordinator.lock", "connector.lock")


def stable_data_dir(role):
    """Official-install data directory. Independent of the process working directory."""
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        root = os.environ.get("XDG_DATA_HOME")
        base = Path(root) if root else Path.home() / ".local" / "share"
    name = "pangpang" if role == "cloud" else "pangpang-connector"
    return (base / name).expanduser().absolute()


def _has_data_evidence(path):
    return path.is_dir() and any((path / name).exists() for name in _DATA_EVIDENCE)


def resolve_data_dir(role, explicit, *, source_checkout):
    """Absolute data path for this start.

    An explicit ``--data`` is used as given (relative paths follow the current
    directory). A source checkout's default stays next to the repository, not
    the caller's directory. An installed package uses :func:`stable_data_dir`.
    A legacy relative directory that already holds a database, vault, or lock
    is not migrated and is not replaced by a new empty tree: startup stops and
    names both paths.
    """
    if explicit:
        return Path(explicit).expanduser().absolute()
    if source_checkout:
        return (source_root() / _LEGACY_DATA[role]).absolute()
    legacy = Path(_LEGACY_DATA[role]).expanduser().absolute()
    stable = stable_data_dir(role)
    if _has_data_evidence(legacy) and legacy != stable:
        raise DataLocationError(
            "当前目录已有旧的相对数据目录 %s（含数据库、Vault 或进程锁）。"
            "正式安装的默认目录是 %s，与启动时的工作目录无关。"
            "不会自动搬迁，也不会另建一个空用户。请显式传入 --data 指向要使用的目录。"
            % (legacy, stable))
    return stable


def _node_stamp(root, venv):
    """Stamp lives in the repo venv when that directory exists, else under node_modules.

    An editable install often has no ``<repo>/.venv``. Writing the stamp there
    raised FileNotFoundError before the coordinator started.
    """
    parent = venv if venv.is_dir() else root / "node_modules"
    return parent / "pangpang-node-version"


def ensure_model_runtime(root, venv, parser, *, omit_dev=True):
    from pangpang.pi_worker_command import packaged_worker_command, release_bundle_dir
    try:
        if packaged_worker_command() is not None:
            return # The selected/release-bundled worker carries its own JS runtime/deps.
    except (ValueError, OSError) as e:
        # Keep the precise failure code (e.g. pi_worker_invalid_bun_bundle,
        # pi_worker_conflicting_selection, or pi_worker_incomplete_release_bundle) so the
        # operator sees the real problem, not a one-size-fits-all message. A corrupt release
        # bundle fails startup here; it never silently falls through to Node.
        parser.error("worker 不可用（%s）：发行版自带的 Bun worker 应在 %s；开发/对照可用 "
                     "PANGPANG_PI_WORKER / _NODE_BUNDLE / _BUN_BUNDLE（绝对路径，至多一个）" % (e, release_bundle_dir()))
    try:
        version = subprocess.check_output(["node", "--version"], text=True).strip().lstrip("v")
        if tuple(map(int, version.split(".")[:2])) < (22, 19):
            raise ValueError()
    except (OSError, ValueError):
        # engines.node is ">=22.19.0"; the (22, 19) check above stays. Hints are text only.
        parser.error("云端需要 Node.js >=22.19；主机连接器不需要 Node。"
                     "macOS: brew install node@22; "
                     "Debian/Ubuntu: apt install nodejs npm, or NodeSource Node.js 22 packages; "
                     "Windows: winget install -e --id OpenJS.NodeJS.22")
    npm = "npm.cmd" if os.name == "nt" else "npm"
    wanted = hashlib.sha256((root / "package-lock.json").read_bytes()).hexdigest()
    stamp = _node_stamp(root, venv)
    if not stamp.exists() or stamp.read_text(encoding="utf-8") != wanted or not (root / "node_modules").exists():
        # ``npm ci --omit=dev`` deletes devDependencies. The launch.py bootstrap
        # of a Release zip wants that. An editable checkout must not.
        command = [npm, "ci", "--ignore-scripts"]
        if omit_dev:
            command.append("--omit=dev")
        subprocess.run(command, check=True, cwd=root)
        stamp = _node_stamp(root, venv)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(wanted, encoding="utf-8")


def resolve_access_token(data, show_access_key, parser):
    """The single first-run secret contract, shared by source and frozen builds: an existing
    PANGPANG_TOKEN wins; otherwise an interactive first run generates one (shown once),
    while a non-interactive first run fails closed (never auto-provisions a secret silently).
    Returns the token to export; the caller sets PANGPANG_TOKEN before starting."""
    from pangpang.vault import Vault, secure_directory
    # Secure/reject the root before any child Vault path is opened. Keep the caller's
    # final component unresolved until secure_directory has checked symlink/reparse
    # redirection; only then canonicalize for subsequent use.
    data = secure_directory(data)
    token = env_value("PANGPANG_TOKEN", "")
    if token:
        return token
    vault = Vault(data / "vault")
    try:
        if not vault.has("web-access-token"):
            if not sys.stdout.isatty():
                parser.error("首次非交互启动请设置 PANGPANG_TOKEN（旧名 XIAN_PANGPANG_TOKEN 仍兼容一个版本）；访问密钥不写入服务日志")
            vault.set("web-access-token", secrets.token_urlsafe(32))
            show_access_key = True
        token = vault.get("web-access-token")
        if show_access_key:
            if not sys.stdout.isatty():
                parser.error("--show-access-key 只允许交互终端")
            print("网页访问密钥（请保存在密码管理器，不发送到聊天）:", token, flush=True)
    finally:
        vault.close()
    return token


def _parser():
    parser = argparse.ArgumentParser(
        description="PangPang. Documented entry: python -m pangpang.cli cloud|connector. "
                    "The pip script pangpang (pangpang.exe on Windows) is optional; use -m if that exe is blocked.")
    parser.add_argument("--version", action="version", version=version_string())
    parser.add_argument("role", choices=["cloud", "connector", "backup", "restore"])
    parser.add_argument("--data")
    parser.add_argument("--show-access-key", action="store_true")
    return parser


def dispatch(args, rest, parser):
    """In-process cloud/connector start. ``sys.argv`` matches the historical launch contract."""
    from pangpang.layout import is_source_checkout
    try:
        data = resolve_data_dir(args.role, args.data, source_checkout=is_source_checkout())
    except DataLocationError as error:
        parser.error(str(error))
    if args.role == "cloud":
        os.environ["PANGPANG_TOKEN"] = resolve_access_token(data, args.show_access_key, parser)
        from pangpang.__main__ import main as role_main
    else:
        from pangpang.connector import main as role_main
    sys.argv = ["pangpang", "--data", str(data), *rest]
    return role_main()


def run_frozen(args, rest, parser):
    """Frozen (PyInstaller) product entry: Python + deps are inside the executable, so there
    is NO venv/pip/npm bootstrap. The packaged Bun worker is auto-discovered from the release
    tree (pi_worker_command.release_bundle_dir()). The SAME first-run token contract runs, so
    a clean user never has to know PANGPANG_TOKEN exists. Dispatch in-process (no re-exec
    of a system interpreter — there isn't one)."""
    if args.role == "cloud":
        # Fail closed at startup if the release-bundled worker is missing/corrupt, instead of
        # deferring to the first Pi exchange. Never falls through to Node (packaged_worker_command
        # raises for an incomplete release bundle; it only returns None in a source checkout).
        from pangpang.pi_worker_command import packaged_worker_command, release_bundle_dir
        try:
            packaged_worker_command()
        except (ValueError, OSError) as e:
            parser.error("发行版自带的 Bun worker 不可用（%s）：应在 %s" % (e, release_bundle_dir()))
    from pangpang.product_runtime import prepare_product
    prepare_product(args.role, parser)
    return dispatch(args, rest, parser)


def _python_too_old(parser):
    # Printed hints only. This path never runs brew, apt, or winget.
    parser.error("Python 3.11+ required. "
                 "macOS: brew install python@3.13; "
                 "Debian/Ubuntu: apt install python3 python3-venv; "
                 "Windows: winget install -e --id Python.Python.3.13")


def _run_backup_command(role, rest, data=None):
    """Stop-consistent backup or restore. Does not start the coordinator or migrate.

    The top-level parser already accepts ``--data``, so the value may arrive
    here instead of remaining in ``rest``.
    """
    from pangpang.backup import BackupRefusal, create_consistent_backup, restore_consistent_backup
    sub = argparse.ArgumentParser(prog="pangpang " + role)
    sub.add_argument("--data", default=data)
    if role == "backup":
        sub.add_argument("--dest", required=True)
    else:
        sub.add_argument("--backup", required=True)
    sub.add_argument("--confirm", action="store_true")
    opts = sub.parse_args(rest)
    if not opts.data:
        sub.error("--data is required")
    try:
        if role == "backup":
            result = create_consistent_backup(opts.data, opts.dest, confirm=opts.confirm)
        else:
            result = restore_consistent_backup(opts.backup, opts.data, confirm=opts.confirm)
    except BackupRefusal as error:
        sys.stderr.write("%s: %s\n" % (error.code, error))
        return 2
    print(result["path"], flush=True)
    if role == "backup" and not result["upgrade_allowed"]:
        sys.stderr.write("backup written; upgrade blocked: %s\n" % ",".join(result["upgrade_blocked"]))
        return 3
    return 0


def main(argv=None):
    """``python -m pangpang.cli cloud|connector``. Installed by pip; no venv and no runtime download."""
    from pangpang.layout import is_source_checkout
    from pangpang.product_runtime import prepare_product
    parser = _parser()
    args, rest = parser.parse_known_args(argv)
    if args.role in {"backup", "restore"}:
        return _run_backup_command(args.role, rest, args.data)
    if sys.version_info < (3, 11):
        _python_too_old(parser)
    prepare_product(args.role, parser)
    # Editable checkout: Python deps came from pip. Cloud still needs the Node
    # bootstrap when no Bun worker is shipped. A wheel never reaches this branch
    # (prepare_product already required the bundled worker). omit_dev stays false
    # so ``npm ci`` does not delete devDependencies, and a missing repo ``.venv``
    # is not required.
    if is_source_checkout() and args.role == "cloud":
        root = source_root()
        os.chdir(root)
        ensure_model_runtime(root, root / ".venv", parser, omit_dev=False)
    return dispatch(args, rest, parser)


def source_main(argv=None):
    """``python launch.py``. Venv bootstrap for a checkout or an unpacked Release zip."""
    from pangpang.product_runtime import prepare_product
    parser = _parser()
    args, rest = parser.parse_known_args(argv)
    if args.role in {"backup", "restore"}:
        return _run_backup_command(args.role, rest, args.data)
    if getattr(sys, "frozen", False):
        return run_frozen(args, rest, parser)
    root = source_root()
    os.chdir(root)
    if sys.version_info < (3, 11):
        _python_too_old(parser)
    venv = root / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if Path(sys.prefix).resolve() != venv.resolve():
        if not python.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        return subprocess.call([str(python), str(root / "launch.py"), *sys.argv[1:]])
    wanted = hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest()
    stamp = venv / "pangpang-python-version"
    if not stamp.exists() or stamp.read_text() != wanted:
        subprocess.run([sys.executable, "-m", "pip", "install", "--require-hashes",
                        "-r", "requirements.txt"], check=True)
        stamp.write_text(wanted)
    prepare_product(args.role, parser)
    try:
        data = resolve_data_dir(args.role, args.data, source_checkout=True)
    except DataLocationError as error:
        parser.error(str(error))
    if args.role == "cloud":
        ensure_model_runtime(root, venv, parser)
        os.environ["PANGPANG_TOKEN"] = resolve_access_token(data, args.show_access_key, parser)
        module = "pangpang"
    else:
        module = "pangpang.connector"
    os.execv(sys.executable, [sys.executable, "-m", module, "--data", str(data), *rest])


if __name__ == "__main__":
    raise SystemExit(main())
