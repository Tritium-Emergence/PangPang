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


def source_root():
    return Path(__file__).resolve().parents[1]


def ensure_model_runtime(root, venv, parser):
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
    stamp = venv / "pangpang-node-version"
    if not stamp.exists() or stamp.read_text() != wanted or not (root / "node_modules").exists():
        subprocess.run([npm, "ci", "--omit=dev", "--ignore-scripts"], check=True)
        stamp.write_text(wanted)


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
    token = os.environ.get("PANGPANG_TOKEN", "")
    if token:
        return token
    vault = Vault(data / "vault")
    try:
        if not vault.has("web-access-token"):
            if not sys.stdout.isatty():
                parser.error("首次非交互启动请设置 PANGPANG_TOKEN；访问密钥不写入服务日志")
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
    parser.add_argument("role", choices=["cloud", "connector"])
    parser.add_argument("--data")
    parser.add_argument("--show-access-key", action="store_true")
    return parser


def dispatch(args, rest, parser):
    """In-process cloud/connector start. ``sys.argv`` matches the historical launch contract."""
    if args.role == "cloud":
        data = Path(args.data or "data-v2").expanduser().absolute()
        os.environ["PANGPANG_TOKEN"] = resolve_access_token(data, args.show_access_key, parser)
        from pangpang.__main__ import main as role_main
    else:
        data = Path(args.data or "connector-data").expanduser().absolute()
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


def main(argv=None):
    """``python -m pangpang.cli cloud|connector``. Installed by pip; no venv and no runtime download."""
    from pangpang.layout import is_source_checkout
    from pangpang.product_runtime import prepare_product
    parser = _parser()
    args, rest = parser.parse_known_args(argv)
    if sys.version_info < (3, 11):
        _python_too_old(parser)
    prepare_product(args.role, parser)
    # Editable checkout: Python deps came from pip. Cloud still needs the Node
    # bootstrap when no Bun worker is shipped. A wheel never reaches this branch
    # (prepare_product already required the bundled worker).
    if is_source_checkout() and args.role == "cloud":
        root = source_root()
        os.chdir(root)
        ensure_model_runtime(root, root / ".venv", parser)
    return dispatch(args, rest, parser)


def source_main(argv=None):
    """``python launch.py``. Venv bootstrap for a checkout or an unpacked Release zip."""
    from pangpang.product_runtime import prepare_product
    parser = _parser()
    args, rest = parser.parse_known_args(argv)
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
    if args.role == "cloud":
        ensure_model_runtime(root, venv, parser)
        data = Path(args.data or "data-v2").expanduser().absolute()
        os.environ["PANGPANG_TOKEN"] = resolve_access_token(data, args.show_access_key, parser)
        module = "pangpang"
    else:
        data = Path(args.data or "connector-data").expanduser().absolute()
        module = "pangpang.connector"
    os.execv(sys.executable, [sys.executable, "-m", module, "--data", str(data), *rest])


if __name__ == "__main__":
    raise SystemExit(main())
