"""Private, authenticated encrypted storage. Never put secret values in Store."""
import base64
import csv
import os
import re
import sqlite3
import stat
import subprocess
import threading
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _windows_system_binary(name):
    """Resolve a Windows system tool without consulting PATH or environment roots.

    Security boundary: secure_directory() runs before the control-plane data store is
    opened. A writable current directory/PATH entry must never be able to replace
    whoami.exe or icacls.exe at this point.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_system_directory = kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_system_directory.restype = wintypes.UINT
    buf = ctypes.create_unicode_buffer(32768)
    size = get_system_directory(buf, len(buf))
    if not size or size >= len(buf):
        raise RuntimeError("windows_system_directory_unavailable")
    binary = Path(buf.value) / name
    if not binary.is_file():
        raise RuntimeError("windows_system_tool_unavailable")
    return str(binary)


def _secure_windows_directory(path):
    """Apply a current-account-only ACL using trusted System32 binaries.

    Use the numeric SID rather than an account name so quoting/localisation cannot
    change the ACL principal. The subprocess argv form means the SID/path are never
    interpreted by a command shell.
    """
    whoami = _windows_system_binary("whoami.exe")
    icacls = _windows_system_binary("icacls.exe")
    raw = subprocess.check_output(
        [whoami, "/user", "/fo", "csv", "/nh"], text=True).strip()
    try:
        row = next(csv.reader([raw]))
        sid = row[-1].strip() if len(row) >= 2 else ""
    except (csv.Error, StopIteration):
        sid = ""
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
        raise RuntimeError("windows_account_sid_unavailable")
    subprocess.run(
        [icacls, str(path), "/inheritance:r", "/grant:r", "*" + sid + ":(OI)(CI)F"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _linklike_directory(path):
    """True when the final directory entry is a symlink/reparse-point redirect.

    Parent symlinks are allowed (normal OS layouts use them); the operator-selected
    data-directory entry itself is not. Check this BEFORE resolve(), otherwise the
    redirect is dereferenced and the security check becomes a no-op.
    """
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attrs & reparse)


def secure_directory(directory):
    """Create/repair a private application directory using the OS account boundary."""
    # Keep the final path component unresolved until after the link/reparse check.
    # Path.resolve().is_symlink() is ineffective because resolve() already follows
    # the link. absolute() fixes the working directory without destroying that fact.
    raw = Path(directory).expanduser().absolute()
    raw.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _linklike_directory(raw):
        raise RuntimeError("unsafe_data_directory")
    path = raw.resolve()
    if os.name != "nt":
        info = os.stat(path, follow_symlinks=False)
        owner = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
        if info.st_uid != owner:
            raise RuntimeError("unsafe_data_directory_owner")
        os.chmod(path, 0o700)
    else:
        _secure_windows_directory(path)
    return path


class Vault:
    def __init__(self, directory):
        self.path = secure_directory(directory)
        keyfile = self.path / "master.key"
        try:
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as out:
                out.write(Fernet.generate_key())
        if os.name != "nt":
            os.chmod(keyfile, 0o600)
        self.fernet = Fernet(keyfile.read_bytes())
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path / "secrets.db", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS secrets (id TEXT PRIMARY KEY, value BLOB NOT NULL)")
        self.db.commit()

    def set(self, ref, value):
        if not isinstance(value, str) or not value or len(value.encode()) > 16384:
            raise ValueError("invalid_secret")
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO secrets VALUES (?,?)", (ref, self.fernet.encrypt(value.encode())))
            self.db.commit()

    def get(self, ref):
        with self.lock:
            row = self.db.execute("SELECT value FROM secrets WHERE id=?", (ref,)).fetchone()
        if row is None:
            raise ValueError("secret_missing")
        return self.fernet.decrypt(row[0]).decode()

    def has(self, ref):
        with self.lock:
            return self.db.execute("SELECT 1 FROM secrets WHERE id=?", (ref,)).fetchone() is not None

    def delete(self, ref):
        with self.lock:
            self.db.execute("DELETE FROM secrets WHERE id=?", (ref,))
            self.db.commit()

    def close(self):
        self.db.close()

    def public_key(self):
        with self.lock:
            if not self.has("device-private-key"):
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                self.set("device-private-key", key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
            key = serialization.load_pem_private_key(self.get("device-private-key").encode(), None)
        return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.DER,
                                serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    def unseal(self, envelope, context):
        key = serialization.load_pem_private_key(self.get("device-private-key").encode(), None)
        aes = key.decrypt(base64.b64decode(envelope["key"], validate=True),
                          padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        return AESGCM(aes).decrypt(base64.b64decode(envelope["nonce"], validate=True),
                    base64.b64decode(envelope["ciphertext"], validate=True), context.encode()).decode()
