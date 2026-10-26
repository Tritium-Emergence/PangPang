"""Allowlisted coordinator environment values, sealed into the vault.

The browser seals a value to this coordinator's vault public key (the same
envelope as a device credential). Plaintext is accepted only inside
vault.unseal, then stored encrypted and copied into this process's
environment. The semantic gate and web search keep reading os.environ.
A sealed value is applied again on the next process start and replaces an
inherited value for that name. An inherited value is left alone until a
sealed value is saved. Clearing removes the vault entry and drops the
process value only when it still matches what was stored.

Save and clear for one name share a lock that covers both the vault update
and the process-env update. Those two stay paired: a clear cannot delete
the vault row and leave the saved value only in this process.
"""
import contextlib
import os
import threading

from .logs import get_logger
from .store import Fault, encode

log = get_logger("coordinator_env")

# Optional coordinator secrets the Web UI may seal. Exact names only.
# PANGPANG_TOKEN is the operator login secret and is intentionally absent.
COORDINATOR_ENV_ALLOWLIST = (
    "TYPESAFE_API_KEY",
    "TAVILY_API_KEY",
)

_FORBIDDEN_CHARS = "\r\n\x00"

# One lock per allowlisted name. Install and clear take it across the vault
# mutation and the matching os.environ mutation so the two cannot tear.
_guard = threading.Lock()
_locks = {}


class _NameLock:
    """A lock plus the count of threads blocked in acquire.

    The count is the number of callers that have announced themselves and
    not yet entered. It stays raised while a caller waits, and drops once
    that caller holds the lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._meta = threading.Lock()
        self._waiting = 0

    def acquire(self):
        with self._meta:
            self._waiting += 1
        try:
            self._lock.acquire()
        finally:
            with self._meta:
                self._waiting -= 1

    def release(self):
        self._lock.release()

    def waiting(self):
        with self._meta:
            return self._waiting


def _name_lock(name):
    with _guard:
        lock = _locks.get(name)
        if lock is None:
            lock = _NameLock()
            _locks[name] = lock
        return lock


@contextlib.contextmanager
def _section(name):
    lock = _name_lock(name)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _between_vault_and_env(name):
    """Inside the name lock, after the vault update and before the process env update."""
    return None

def is_allowlisted(name):
    return isinstance(name, str) and name in COORDINATOR_ENV_ALLOWLIST


def ref_for(name):
    if not is_allowlisted(name):
        raise Fault("coordinator_env_not_allowlisted", 400)
    return "coordinator-env:" + name


def context_for(name):
    """AEAD context bound to the allowlisted name. Recomputed on the server."""
    return ref_for(name)


def projection(vault):
    """Presence only. Never the secret."""
    rows = []
    for name in COORDINATOR_ENV_ALLOWLIST:
        ref = ref_for(name)
        with _section(name):
            rows.append({
                "name": name,
                "configured": bool(os.environ.get(name)),
                "sealed": bool(vault.has(ref)),
            })
    return rows


def _usable(value):
    return (isinstance(value, str) and bool(value) and value.strip()
            and len(value.encode()) <= 16384
            and not any(char in value for char in _FORBIDDEN_CHARS))


def apply_sealed(vault):
    """Copy sealed allowlisted values into this process.

    A missing row leaves an inherited value in place. A row that cannot be
    decrypted is not applied and is not logged with its contents.
    """
    for name in COORDINATOR_ENV_ALLOWLIST:
        ref = ref_for(name)
        with _section(name):
            try:
                if not vault.has(ref):
                    continue
                value = vault.get(ref)
            except Exception:
                log.warning("coordinator_env_unreadable name=%s", name)
                continue
            if not _usable(value):
                log.warning("coordinator_env_unreadable name=%s", name)
                continue
            os.environ[name] = value


def _require_sealed(sealed):
    if not isinstance(sealed, dict) or set(sealed) != {"key", "nonce", "ciphertext"}:
        raise Fault("sealed_credential_required", 400)
    if any(not isinstance(sealed[part], str) or not sealed[part] for part in sealed):
        raise Fault("sealed_credential_required", 400)
    if len(encode(sealed)) > 32000:
        raise Fault("sealed_credential_required", 400)


def install(vault, name, sealed):
    context = context_for(name)
    _require_sealed(sealed)
    try:
        value = vault.unseal(sealed, context)
    except Exception:
        raise Fault("sealed_credential_rejected", 400) from None
    if not _usable(value):
        raise Fault("invalid_coordinator_env", 400)
    ref = ref_for(name)
    with _section(name):
        vault.set(ref, value)
        _between_vault_and_env(name)
        os.environ[name] = value
        return {"name": name, "configured": True, "sealed": True}


def clear(vault, name):
    ref = ref_for(name)
    with _section(name):
        previous = None
        if vault.has(ref):
            try:
                previous = vault.get(ref)
            except Exception:
                previous = None
            vault.delete(ref)
        # The name lock is still held. The live value is the one this row
        # stored, or a different parent value that returns on the next start.
        # Drop only the stored value.
        _between_vault_and_env(name)
        if isinstance(previous, str) and previous and os.environ.get(name) == previous:
            os.environ.pop(name, None)
        return {
            "name": name,
            "configured": bool(os.environ.get(name)),
            "sealed": False,
        }
