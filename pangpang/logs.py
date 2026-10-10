"""Lightweight logging for the coordinator. stdout only; the launcher already
redirects stdout+stderr to a log file. No third-party deps, no rotation."""
import logging
import sys

from .env_compat import env_value

_configured = False


def configure_logging():
    """Install a single stdout handler. Idempotent — safe to call more than once."""
    global _configured
    if _configured:
        return
    level_name = env_value("PANGPANG_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger("pangpang")
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                                            datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(name):
    """Return a child of the 'pangpang' logger. Works even before configure_logging."""
    return logging.getLogger("pangpang").getChild(name)
