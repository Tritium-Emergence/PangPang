"""Operator-owned adapter catalog extension: <data>/adapters.json.

Custom rows join the built-in ADAPTER_CATALOG at the earliest startup point of
each side (coordinator and connector read their OWN data directory — argv
stays operator-defined, never model-defined). For a connector host the file is
device-only: sync carries a bounded routing projection (validate_declared), and
the connector can merge one row after the operator approves that exact row.
The cloud does not run that command. A local host is this coordinator: the
same approval writes the row into the coordinator data directory and merges
it. That write does not launch the binary. Any violation fails closed with
the file, line and field named.
"""
import json
import ntpath
import os
import posixpath
import re
import stat
import threading
from pathlib import Path

from .store import Fault, assert_no_txn_io

MAX_FILE_BYTES = 262144
MAX_ROWS = 64
MAX_BINARY_CHARS = 4096
MAX_ARGV_ITEMS = 64
MAX_ARGV_ITEM_CHARS = 4096
MAX_ARGV_TOTAL_CHARS = 16384
MAX_USE_WHEN_CHARS = 500
MAX_ENV_ITEMS = 32
MAX_ENV_VALUE_CHARS = 4096
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
ALLOWED_KEYS = frozenset(("binary", "argv", "concurrency", "priority", "use_when",
                          "secret_env", "extra_env"))
REQUIRED_KEYS = ("binary", "argv", "concurrency", "priority", "use_when")


def _builtin_names():
    from .execution import ADAPTER_CATALOG
    return frozenset(ADAPTER_CATALOG) - _merged_names


_merged_names = set()


def is_merged(name):
    """True when this name was merged from an adapters file, not a built-in row."""
    return name in _merged_names


_file_locks_guard = threading.Lock()
_file_locks = {}


def _adapters_file_lock(path):
    """One lock for the read-modify-write of a single adapters file.

    Distinct from the store transaction. Two approvals can otherwise each
    read a stale copy, and the later replace drops the other row.
    """
    key = os.path.normcase(os.path.abspath(path))
    with _file_locks_guard:
        lock = _file_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _file_locks[key] = lock
        return lock


def _bad(path, line, subject, message):
    raise Fault("invalid_adapters_file", 500,
                advice="%s: line %s: %s: %s" % (path, line, subject, message))


def _line_of(text, needle):
    index = text.find('"' + needle + '"')
    return 1 + text.count("\n", 0, index) if index >= 0 else 1


def _path_basename(binary):
    """True when binary is one PATH segment, not a directory, drive, or tilde."""
    if not binary or binary in (".", ".."):
        return False
    if "~" in binary or "/" in binary or "\\" in binary:
        return False
    return ntpath.basename(binary) == binary and posixpath.basename(binary) == binary


def _clean_row(path, line, name, row):
    if not isinstance(row, dict):
        _bad(path, line, name, "row must be an object")
    unknown = sorted(set(row) - ALLOWED_KEYS)
    if unknown:
        _bad(path, line, name,
             "keys %s are not allowed on custom rows (trust and protocol fields are built-in only)" % unknown)
    for key in REQUIRED_KEYS:
        if key not in row:
            _bad(path, line, name, "'%s' is required" % key)
    binary = row["binary"]
    if not isinstance(binary, str) or not binary or "\x00" in binary or len(binary) > MAX_BINARY_CHARS:
        _bad(path, line, name, "binary must be a non-empty string")
    if not _path_basename(binary):
        _bad(path, line, name, "binary must be a single PATH basename (no '~', '/', or '\\')")
    argv = row["argv"]
    if not isinstance(argv, list) or len(argv) > MAX_ARGV_ITEMS:
        _bad(path, line, name, "argv must be a list of at most %d strings" % MAX_ARGV_ITEMS)
    total = 0
    for item in argv:
        if not isinstance(item, str) or "\x00" in item or len(item) > MAX_ARGV_ITEM_CHARS:
            _bad(path, line, name, "argv entries must be plain strings without NUL")
        total += len(item)
    if total > MAX_ARGV_TOTAL_CHARS:
        _bad(path, line, name, "argv exceeds %d characters in total" % MAX_ARGV_TOTAL_CHARS)
    concurrency = row["concurrency"]
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 64:
        _bad(path, line, name, "concurrency must be an integer between 1 and 64")
    priority = row["priority"]
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000:
        _bad(path, line, name, "priority must be an integer between 0 and 1000")
    use_when = row["use_when"]
    if not isinstance(use_when, str) or not use_when.strip() or len(use_when) > MAX_USE_WHEN_CHARS:
        _bad(path, line, name, "use_when must be a non-empty string of at most %d characters"
             % MAX_USE_WHEN_CHARS)
    clean = {"binary": binary, "argv": list(argv), "concurrency": concurrency,
             "priority": priority, "use_when": use_when}
    if "secret_env" in row:
        secret = row["secret_env"]
        if not isinstance(secret, str) or not ENV_NAME.fullmatch(secret):
            _bad(path, line, name, "secret_env must be an environment variable name")
        clean["secret_env"] = secret
    if "extra_env" in row:
        extra = row["extra_env"]
        if not isinstance(extra, dict) or len(extra) > MAX_ENV_ITEMS:
            _bad(path, line, name, "extra_env must be an object of at most %d entries" % MAX_ENV_ITEMS)
        for key, value in extra.items():
            if not isinstance(key, str) or not ENV_NAME.fullmatch(key) \
                    or not isinstance(value, str) or "\x00" in value or len(value) > MAX_ENV_VALUE_CHARS:
                _bad(path, line, name, "extra_env must map environment variable names to plain strings")
        clean["extra_env"] = dict(extra)
    return clean


def load(path):
    path = Path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    except OSError as error:
        _bad(path, 0, "file", "cannot be inspected (%s)" % error.strerror)
    if not stat.S_ISREG(info.st_mode):
        _bad(path, 0, "file", "must be a regular file")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        _bad(path, 0, "file", "must be owned by the account running PangPang")
    if hasattr(os, "geteuid") and stat.S_IMODE(info.st_mode) & 0o022:
        _bad(path, 0, "file", "must not be group- or world-writable")
    if info.st_size > MAX_FILE_BYTES:
        _bad(path, 0, "file", "exceeds %d bytes" % MAX_FILE_BYTES)
    return _rows_from_text(path, path.read_text(encoding="utf-8"))


def _rows_from_text(path, text):
    """Content rules shared by load() and a candidate that has not been renamed into place."""
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        _bad(path, 0, "file", "exceeds %d bytes" % MAX_FILE_BYTES)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        _bad(path, error.lineno, "json", error.msg)
    except UnicodeDecodeError as error:
        _bad(path, 0, "json", "must be UTF-8 text")
    if not isinstance(document, dict):
        _bad(path, 1, "root", "must be an object of adapter rows")
    if len(document) > MAX_ROWS:
        _bad(path, 1, "root", "at most %d adapters are allowed" % MAX_ROWS)
    builtin = _builtin_names()
    rows = {}
    for name, row in document.items():
        line = _line_of(text, name) if isinstance(name, str) else 1
        if not isinstance(name, str) or not NAME.fullmatch(name):
            _bad(path, line, str(name), "adapter name must match %s" % NAME.pattern)
        if name in builtin:
            _bad(path, line, name, "collides with a built-in adapter; built-in rows cannot be overridden")
        rows[name] = _clean_row(path, line, name, row)
    return rows


DECLARED_ROW_KEYS = frozenset(("concurrency", "priority", "use_when"))


def validate_declared(payload):
    """Cloud-side check of a connector's declared_adapters wire projection.

    A connector reports which operator-written adapters.json rows it runs so the
    cloud need not hold a copy. Only routing metadata may cross: argv, binary and
    env material stay device-side. The cloud re-validates everything — a device
    report is not trusted for built-in collisions or name shape. Returns the
    clean snapshot ({} is an explicit withdrawal) or None when the field is
    absent (a legacy device, not an empty snapshot).
    """
    if payload is None:
        return None
    if not isinstance(payload, dict) or len(payload) > MAX_ROWS:
        _declared_bad("an object of at most %d adapter names" % MAX_ROWS)
    if len(json.dumps(payload, ensure_ascii=False)) > MAX_FILE_BYTES:
        _declared_bad("at most %d bytes when encoded" % MAX_FILE_BYTES)
    builtin = _builtin_names()
    clean = {}
    for name, row in payload.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            _declared_bad("adapter name must match %s" % NAME.pattern, name)
        if name in builtin:
            _declared_bad("declared adapter collides with a built-in", name)
        if not isinstance(row, dict) or set(row) - DECLARED_ROW_KEYS:
            _declared_bad("only concurrency, priority and use_when may be declared", name)
        out = {}
        concurrency = row.get("concurrency")
        if concurrency is not None:
            if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 64:
                _declared_bad("concurrency must be an integer between 1 and 64", name)
            out["concurrency"] = concurrency
        priority = row.get("priority")
        if priority is not None:
            if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000:
                _declared_bad("priority must be an integer between 0 and 1000", name)
            out["priority"] = priority
        use_when = row.get("use_when")
        if use_when is not None:
            if not isinstance(use_when, str) or not use_when.strip() or len(use_when) > MAX_USE_WHEN_CHARS:
                _declared_bad("use_when must be a non-empty string of at most %d characters"
                              % MAX_USE_WHEN_CHARS, name)
            out["use_when"] = use_when
        clean[name] = out
    return clean


def _declared_bad(message, name=None):
    raise Fault("invalid_declared_adapters", 400,
                advice="declared_adapters: %s%s" % (str(name) + ": " if name is not None else "", message))


def clean_declaration(name, row):
    """One custom adapter row, checked with the same rules as adapters.json.

    Does not read or write a file and does not change the built-in catalog.
    """
    path = "<declaration>"
    try:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            _bad(path, 1, str(name), "adapter name must match %s" % NAME.pattern)
        if name in _builtin_names():
            _bad(path, 1, name, "collides with a built-in adapter; built-in rows cannot be overridden")
        return _clean_row(path, 1, name, row)
    except Fault as error:
        if error.code != "invalid_adapters_file":
            raise
        raise Fault("invalid_adapter_declaration", 400, advice=error.advice) from None


def declared_rows(path):
    """Rows stored in one adapters file, in the shape a connector reports."""
    rows = load(path)
    return [{"adapter": name, **rows[name]} for name in sorted(rows)]


def _write_document(path, rows):
    path = Path(path)
    payload = json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # Every content rule load() enforces, before the destination is replaced.
    _rows_from_text(path, payload)
    tmp = path.with_name("." + path.name + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as error:
        try:
            tmp.unlink()
        except OSError:
            pass
        _bad(path, 0, "file", "cannot be written (%s)" % error.strerror)


def write_merged_row(path, name, row):
    """Merge one approved row into an adapters file and the live catalog.

    An identical stored row is left as-is. A different stored row is refused.
    Built-in catalog rows are never replaced. The read, conflict check,
    replace, and catalog merge of this file run under that file's lock.
    """
    assert_no_txn_io("adapters_file.write_merged_row")
    path = Path(path)
    with _adapters_file_lock(path):
        clean = clean_declaration(name, row)
        current = load(path)
        if name in current and current[name] != clean:
            raise Fault("adapter_declaration_conflict", 409,
                        advice="%s: %s: conflicts with an adapter row already stored" % (path, name))
        if current.get(name) != clean:
            current[name] = clean
            _write_document(path, current)
        merge(path)
    return clean


def merge(path):
    rows = load(path)
    from .execution import ADAPTER_CATALOG
    for name, row in rows.items():
        current = ADAPTER_CATALOG.get(name)
        if current is not None:
            if current == row:
                continue
            try:
                line = _line_of(Path(path).read_text(encoding="utf-8"), name)
            except OSError:
                line = 1
            _bad(path, line, name, "conflicts with an adapter row already merged from another file")
        ADAPTER_CATALOG[name] = row
        _merged_names.add(name)
    return sorted(rows)
