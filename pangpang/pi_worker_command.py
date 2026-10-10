"""Pi transport selection. Priority: explicit operator selector ->
deterministic release-bundled Bun worker -> Node default (plain source checkout only).
A selected or release-discovered worker never silently falls back to Node.
A pip/frozen install, or any tree with runtime/SHIPPED.json, commits to the
bundled Bun worker: missing or unhashed bytes fail closed."""
import os
import sys
from pathlib import Path

from .env_compat import env_value, has_env

WORKER_ENV = "PANGPANG_PI_WORKER" # compiled single-file (bun --compile); preview
NODE_BUNDLE_ENV = "PANGPANG_PI_NODE_BUNDLE" # Node 24 runtime + bundle
BUN_BUNDLE_ENV = "PANGPANG_PI_BUN_BUNDLE" # pinned packaged Bun runtime + worker bundle; formal shape

_SELECTORS = (WORKER_ENV, NODE_BUNDLE_ENV, BUN_BUNDLE_ENV)


def release_root():
    """The directory a release is laid out under.

    Frozen (PyInstaller onedir): the executable's directory
    (``<root>/coordinator(.exe)`` + ``<root>/runtime/...``).
    Wheel: ``pangpang/_shipped`` (same ``runtime/`` relative layout).
    Source checkout or unpacked Release zip: the repo root, next to launch.py.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    from .layout import install_anchor
    return install_anchor()


# Fixed, deterministic location of the packaged Bun worker inside a release tree. The end
# user never sets an env var: if this directory exists, the launcher is in "release mode"
# and commits to it (missing/corrupt files -> explicit fault, never Node). The env selectors
# above remain for development only. Computed as a function so a
# frozen build resolves it from sys.executable, not the source __file__ layout.
def release_bundle_dir():
    return release_root() / "runtime" / "pi-worker"


def compiled_worker():
    value = env_value(WORKER_ENV, None)
    if value is None:
        return None
    path = Path(value)
    if not value or not path.is_absolute() or not path.is_file():
        raise ValueError("pi_worker_invalid_binary_path")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise ValueError("pi_worker_binary_not_executable")
    return path.resolve()


def _node_bundle_invocation():
    directory = env_value(NODE_BUNDLE_ENV, None)
    if directory is None:
        return None
    root = Path(directory)
    if not directory or not root.is_absolute() or not root.is_dir():
        raise ValueError("pi_worker_invalid_node_bundle")
    node = root / ("node.exe" if os.name == "nt" else "node")
    script = root / "pi_worker.bundle.mjs"
    if not node.is_file() or not script.is_file():
        raise ValueError("pi_worker_incomplete_node_bundle")
    if os.name != "nt" and not os.access(node, os.X_OK):
        raise ValueError("pi_worker_binary_not_executable")
    return ([str(node.resolve()), str(script.resolve())], None)


def _bun_bundle_from_dir(root, incomplete_code):
    """Resolve a packaged Bun bundle directory (shared by the operator env selector and the
    release auto-discovery path) into (argv, cwd). `root` is already known to be a directory.

    Security: `--no-env-file` disables.env autoload and `--no-install` disables runtime
    auto-install (offline, deterministic); cwd is pinned to the artifact dir, which is clean
    (no bunfig.toml/.env), so a hostile working directory can never inject a bunfig `preload`.
    Verified empirically that bun reads bunfig/.env only from $cwd, not ancestors, and that
    `-c <other>` does NOT suppress a $cwd bunfig — hence cwd-pinning, not a flag, is the real
    mitigation. `--no-orphans` makes the worker exit (and kill its descendants) if the
    coordinator parent dies, so a coordinator crash/restart leaves no orphaned worker.
    BUN_OPTIONS/NODE_OPTIONS are already stripped by the caller's env allowlist. No fallback
    to Node on any failure."""
    bun = root / ("bun.exe" if os.name == "nt" else "bun")
    script = root / "pi_worker.bundle.mjs"
    if not bun.is_file() or not script.is_file():
        raise ValueError(incomplete_code)
    if os.name != "nt" and not os.access(bun, os.X_OK):
        raise ValueError("pi_worker_binary_not_executable")
    argv = [str(bun.resolve()), "run", "--no-install", "--no-env-file", "--no-orphans", str(script.resolve())]
    return (argv, str(root.resolve()))


def _bun_bundle_invocation():
    """Operator-selected packaged Bun bundle (dev only). The formal shape."""
    directory = env_value(BUN_BUNDLE_ENV, None)
    if directory is None:
        return None
    root = Path(directory)
    if not directory or not root.is_absolute() or not root.is_dir():
        raise ValueError("pi_worker_invalid_bun_bundle")
    return _bun_bundle_from_dir(root, "pi_worker_incomplete_bun_bundle")


def _product_commits_to_bundle():
    """Pip, frozen, and SHIPPED release trees must not fall through to system Node."""
    from .layout import is_frozen, is_source_checkout
    if is_frozen() or not is_source_checkout():
        return True
    return (release_root() / "runtime" / "SHIPPED.json").is_file()


def _release_bundle_invocation():
    """Release mode: a packaged Bun worker shipped at the fixed release_bundle_dir(). Its
    existence is the signal — the end user sets no env var. Once present we commit to it;
    missing/corrupt files raise (explicit startup fault), never Node.

    A frozen build, a pip install, or a tree with runtime/SHIPPED.json has no system-Node
    default: an absent bundle is a packaging failure and fails closed. A plain source
    checkout (not frozen, no SHIPPED.json) still returns None so the Node dev default applies.
    When SHIPPED.json is present the Bun binary and worker bundle are hashed before use."""
    root = release_bundle_dir()
    if not root.is_dir():
        if _product_commits_to_bundle():
            raise ValueError("pi_worker_incomplete_release_bundle")
        return None
    invocation = _bun_bundle_from_dir(root, "pi_worker_incomplete_release_bundle")
    if (release_root() / "runtime" / "SHIPPED.json").is_file():
        from .product_runtime import verify_bun
        verify_bun(release_root())
    return invocation


def _packaged_invocation():
    """Return (argv, cwd) for an operator-selected OR release-bundled worker, or None for the
    Node default (source checkout only). At most one env selector may be set; an explicit
    selector overrides release auto-discovery (dev only). cwd is None unless the shape
    pins it (Bun bundle)."""
    if len([name for name in _SELECTORS if has_env(name)]) > 1:
        raise ValueError("pi_worker_conflicting_selection")
    for resolver in (_bun_bundle_invocation, _node_bundle_invocation):
        inv = resolver()
        if inv is not None:
            return inv
    binary = compiled_worker()
    # One argv element preserves spaces; never interpret operator input as shell
    # syntax. Do not retry with Node after any selected worker failure.
    if binary is not None:
        return ([str(binary)], None)
    # No explicit selector: in a release tree, deterministically use the bundled worker.
    return _release_bundle_invocation()


def packaged_worker_command():
    inv = _packaged_invocation()
    return None if inv is None else inv[0]


def worker_invocation():
    """Return (argv, cwd). cwd=None inherits the caller's cwd (Node default / compiled /
    Node bundle); the packaged Bun bundle pins cwd to its own artifact dir. Invalid,
    missing, or conflicting selection — including an incomplete release bundle — raises;
    the caller fails explicitly and never falls back to Node. The Node default is returned
    only in a plain source checkout with no selector and no release bundle."""
    inv = _packaged_invocation()
    if inv is not None:
        return inv
    return (["node", str(Path(__file__).with_name("pi_worker.mjs"))], None)


def worker_command():
    return worker_invocation()[0]

