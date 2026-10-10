"""Operator-owned adapter catalog extension: <data>/adapters.json.

Custom rows join the built-in ADAPTER_CATALOG at the earliest startup point of
each side (coordinator and connector read their OWN data directory; both sides
must place the same file — argv stays operator-defined, never model-defined).
Any violation fails closed at startup with the file, line and field named.
"""
import json
import os
import re
import stat
from pathlib import Path

from .store import Fault

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


def _bad(path, line, subject, message):
    raise Fault("invalid_adapters_file", 500,
                advice="%s: line %s: %s: %s" % (path, line, subject, message))


def _line_of(text, needle):
    index = text.find('"' + needle + '"')
    return 1 + text.count("\n", 0, index) if index >= 0 else 1


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
    text = path.read_text(encoding="utf-8")
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
