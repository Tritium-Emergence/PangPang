"""Where runtime/pi-worker and runtime/pi live.

Source checkouts and GitHub Release trees keep those directories next to
launch.py. A wheel keeps the same relative layout under pangpang/_shipped,
because pangpang/runtime.py is already a module. Frozen builds keep them
next to the executable.
"""
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def is_source_checkout():
    """True for a repo checkout or an unpacked Release zip (launch.py beside the package)."""
    if is_frozen():
        return False
    return (_PKG.parent / "launch.py").is_file()


def install_anchor():
    """Directory that contains runtime/. Tests patch release_root(), which calls this."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    shipped = _PKG / "_shipped"
    if (shipped / "runtime" / "SHIPPED.json").is_file():
        return shipped
    if is_source_checkout():
        return _PKG.parent
    return shipped
