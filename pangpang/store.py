"""Transactional state, outbox and action journal in one SQLite database."""
import contextlib
import hashlib
import json
import sqlite3
import threading
import time
import uuid


EVENT_RETENTION = 4096
EVENT_PRUNE_EVERY = 1024


def encode(value):
    """Compact canonical JSON.

    U+2028 and U+2029 stay legal inside JSON strings, but Node readline treats
    them as line breaks. Escaping them keeps one IPC frame on one line.
    Decoding restores the original characters. Other non-ASCII is left as-is.
    """
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def uid(prefix):
    return prefix + "-" + uuid.uuid4().hex


# --- OS family classification -----------------------------------
# A host's OS decides remote command construction and path case semantics, so it
# must be classified by EXACT token match, never a prefix (prefix let "linuxXYZ" /
# "windows-evil" / "macaroni" pass). Map the platform.system()-style label to one of
# {win, mac, posix}; unknown -> None (caller fails closed). Case-insensitive; the
# label is normalized (strip, first token) so "Windows"/"windows"/"Linux" all match
# but a made-up token does not.
_OS_FAMILY = {
    "windows": "win", "win32": "win", "win": "win", "cygwin": "win", "msys": "win",
    "darwin": "mac", "macos": "mac", "mac": "mac", "osx": "mac",
    "linux": "posix", "linux2": "posix", "freebsd": "posix", "openbsd": "posix",
    "netbsd": "posix", "dragonfly": "posix", "sunos": "posix", "solaris": "posix", "aix": "posix",
}

def os_family(os_label):
    """Return 'win' | 'mac' | 'posix' for a KNOWN OS label, else None (unknown/blank).
    Exact match on the FULL normalized (strip+lower) label — not a prefix and not the
    first whitespace token, so 'linuxXYZ' AND 'linux evil' both fail."""
    return _OS_FAMILY.get(str(os_label or "").strip().lower())


def _where_clause(where):
    """Build the AND-clause for find/count.

    Field names are internal constants (validated alnum/underscore) so the JSON
    path is inlined as a SQL LITERAL — never a bound parameter — so a composite
    expression index `json_extract(data,'$.status')` can actually be chosen by
    the planner (a `json_extract(data,?)` shape matches no expression index).
    VALUES are always bound parameters. A tuple/list/set value expands to an
    `IN (?,?,...)` set membership. NOTE: an IN(...) over an expression index does
    NOT keep the created-ordered index (the planner falls back to the kind index +
    a residual filter); hot active scans therefore use single-status lookups (see
    Service._active_tasks), and IN is reserved for non-hot multi-value filters.
    """
    sql, params = "", []
    for field, value in where.items():
        if not field.replace("_", "").isalnum():
            raise ValueError("invalid filter")
        path = "'$." + field + "'"
        if isinstance(value, (tuple, list, set)):
            options = list(value)
            if not options:
                sql += " AND 0" # empty set matches nothing (deterministic)
                continue
            placeholders = ",".join("?" for _ in options)
            sql += " AND json_extract(data," + path + ") IN (" + placeholders + ")"
            params += options
        else:
            sql += " AND json_extract(data," + path + ") IS ?"
            params.append(value)
    return sql, params



def canonical_workspace(host, cwd):
    """Canonical (host_id, path-components) workspace identity.
    Local host: realpath resolves symlinks/`..`. Remote host: fold `.`/`..`
    lexically (device already canonicalized, but a model-influenced cwd may still
    carry `..`; without folding /repo/x/../shared and /repo/shared alias). Case
    folding follows the TARGET host's OS, never the controller's: Windows &
    macOS fold; Linux/POSIX do not; an unknown REMOTE OS returns a host-wide
    quarantine (empty segments) rather than guess. Returns (host_id, tuple of
    segments) so overlap is a component-prefix test, never a string prefix (/repo
    does not overlap /repo2)."""
    import os
    from pathlib import Path
    raw = str(cwd or "")
    if host.get("transport") == "local":
        # realpath touches the filesystem — never inside a store transaction (rule B):
        # a slow mount/reparse/AV stall would hold the global DB lock. Callers cache
        # this at register/config time; the guard catches any future hot-path misuse.
        assert_no_txn_io("canonical_workspace(local realpath)")
        try:
            raw = str(Path(raw).resolve())
        except Exception:
            raw = str(Path(raw))
    norm = raw.replace("\\", "/")
    segs = []
    for s in norm.split("/"):
        if s in ("", "."):
            continue
        if s == "..":
            if segs:
                segs.pop() # lexical parent; realpath already handled the local case
            continue
        segs.append(s)
    # Case folding is a property of the TARGET device's filesystem, never the
    # controller's OS: a Windows controller managing a Linux device must not
    # fold /Repo and /repo together. Decide from the host's own OS:
    # - Windows: case-insensitive -> fold.
    # - macOS (darwin): the default volume is case-INSENSITIVE; only fold (a rename
    # that changes case is not a new workspace). A case-sensitive APFS volume is
    # rarer — conservative folding over-serializes (never fails open). Registering
    # per-root case sensitivity is a follow-up.
    # - Linux / other known POSIX: case-sensitive -> do not fold.
    # - Unknown/missing OS on a REMOTE host: fail-closed. We cannot know the
    # filesystem semantics, so a wrong guess could alias two distinct dirs (fail
    # open). Return a host-wide quarantine (empty segments) so it conservatively
    # serializes rather than risk a missed conflict. A local host with no os hint
    # falls back to the controller's own os (local target == controller).
    fam = os_family(host.get("os"))
    if fam in ("win", "mac"):
        # Windows always case-insensitive; macOS default volume case-insensitive
        # (conservative fold — never fails open; per-root sensitivity is a follow-up).
        segs = [s.lower() for s in segs]
    elif fam == "posix":
        pass # case-sensitive POSIX
    elif host.get("transport") == "local":
        # No/unknown os hint but it's this machine: the controller's os is authoritative.
        if os.name == "nt":
            segs = [s.lower() for s in segs]
    else:
        # Unknown remote OS: fail-closed host-wide quarantine (overlaps everything on
        # this host) rather than guess case semantics.
        return (host["id"], ())
    return (host["id"], tuple(segs))


def workspace_overlaps(a, b):
    """Two canonical workspaces conflict when same host and one segment path is a
    prefix of the other (parent/child overlap). An empty segment tuple is host-wide
    (quarantine) and overlaps everything on that host."""
    if a[0] != b[0]:
        return False
    sa, sb = a[1], b[1]
    n = min(len(sa), len(sb))
    return sa[:n] == sb[:n]


# --- Connector launch contract --------------------------------------
# The cloud and the device hold DIFFERENT projections of a task's installation/host
# (the cloud has a connector projection with proxy/credential metadata; the device
# has its real argv/secret_refs). They therefore CANNOT reproduce the cloud's whole
# spec_digest. The launch contract is the explicit, allowlisted subset of the START
# spec that BOTH sides can reconstruct byte-for-byte, so each can compute the same
# contract_digest and the device can refuse a spec that does not match what the cloud
# promised. argv/secret_refs/env are intentionally OUT — they live only on the device
# and are gated separately by policy_digest. Anything the runner actually launches
# with that affects the promise (cwd, timeout, access mode, workspace identity, the
# input file digests, the outputs hint, the prompt) IS in.
# v3 binds the supervision floor frozen at submit. A device reports the
# highest version it speaks; broker.start holds a v3 task until the device proves 3,
# so a device that would ignore the floor never runs it. v2 stays accepted for
# legacy (floor-less) tasks.
CONTRACT_VERSION = 4

# Every field launch_contract emits, per version. The device requires the exact set:
# contract_digest is only a hash — it cannot detect a DELETED field
# re-signed with a fresh digest; an explicit required-key schema can.
_CONTRACT_V2_KEYS = frozenset({
    "contract_version", "execution_id", "installation_id", "cwd", "timeout_s",
    "access_mode", "workspace_key", "inputs", "outputs", "resource_keys",
    "prompt", "attempt_uuid", "policy_digest"})
# v4 adds the HARD required_outputs deliverable contract: those relative paths
# MUST survive cloud→device bound/signed, or the device runner never knows to verify
# them and every connector hard-deliverable task would fail-closed. A task carries v4
# ONLY when it declares required_outputs; a plain task stays v3 (compat preserved).
CONTRACT_KEYS = {2: _CONTRACT_V2_KEYS,
                 3: _CONTRACT_V2_KEYS | {"supervision_floor"},
                 4: _CONTRACT_V2_KEYS | {"supervision_floor", "required_outputs"}}
CONTRACT_REQUIRED_KEYS = CONTRACT_KEYS[CONTRACT_VERSION]


def contract_version_for(task):
    """v4 iff the task declares a HARD required_outputs contract (those files
    must be bound cloud→device); else v3 iff it carries a frozen supervision floor
    (every task submitted by this version); a legacy floor-less task keeps v2."""
    if task.get("required_outputs"):
        return 4
    return 3 if task.get("supervision_floor") is not None else 2

def _contract_inputs(task):
    """Canonical (path,digest) input list — order-stable, and STRIPPED of device-side
    fields like artifact_id that the two sides do not share."""
    return sorted(({"path": i.get("path"), "digest": i.get("digest")}
                   for i in task.get("inputs", [])),
                  key=lambda x: (str(x["path"]), str(x["digest"])))

def launch_contract(task):
    """Deterministic, order-stable subset of the task both cloud and device share.
    inputs are normalized to sorted (path,digest) pairs so list order cannot change
    the digest. workspace_key is included as [host, [segs]] so a workspace-identity
    disagreement is a contract mismatch, not a silent alias. attempt_uuid is the
    per-attempt identity fence. Callers digest this via digest()."""
    wk = task.get("workspace_key")
    version = contract_version_for(task)
    contract = {
        "contract_version": version,
        "execution_id": task.get("execution_id"),
        "installation_id": task.get("installation_id"),
        "cwd": task.get("cwd", ""),
        "timeout_s": int(task.get("timeout_s", 3600)),
        "access_mode": task.get("access_mode", "write"),
        "workspace_key": [wk[0], list(wk[1])] if wk else None,
        "inputs": _contract_inputs(task),
        "outputs": list(task.get("outputs", [])),
        "resource_keys": sorted(task.get("resource_keys", [])),
        "prompt": task.get("prompt", ""),
        "attempt_uuid": task.get("attempt_uuid"),
        "policy_digest": task.get("policy_digest"),
    }
    if version >= 3:
        contract["supervision_floor"] = task.get("supervision_floor")
    if version >= 4:
        #: the hard deliverable contract, bound and signed so a device cannot be
        # handed a stripped/altered path set. Order-stable list (submit order is the
        # declared order; it is not sorted — a path's position is not semantic but the
        # list identity must round-trip exactly for the digest/bind to hold).
        contract["required_outputs"] = list(task.get("required_outputs", []))
    return contract

def verify_launch_contract(task, device_contract_version=CONTRACT_VERSION):
    """Device-side gate (single entry — SIS). Given the received task (which
    carries `contract` + `contract_digest` + top-level fields), return an error code or
    None. `device_contract_version` is THIS device's own compiled-in capability — the
    authority for whether a contract is required (a stripped
    payload with contract+digest+marker ALL deleted must NOT slip through as legacy; the
    device knows it speaks v2 regardless of what the mutable payload claims). Checks:
      - required: a contract is required when EITHER this device speaks >= 2 OR the
        top-level `min_contract_version` marker says so. If required but `contract` is
        absent -> REJECT `local_contract_required`. Only a genuinely legacy device
        (device_contract_version < 2) with no marker takes the legacy path;
      - schema: contract has EXACTLY the required keys and contract_version == 2;
      - signature: digest(contract) == contract_digest (any value tamper);
      - payload bind: the top-level fields the device also reads
        (execution_id/installation_id/outputs/prompt/policy_digest + canonical inputs)
        match the signed contract (inputs canonical so a legitimate artifact_id does not
        spuriously mismatch)."""
    contract = task.get("contract")
    try:
        min_cv = int(task.get("min_contract_version", 0))
    except (TypeError, ValueError):
        min_cv = 0
    required_cv = max(int(device_contract_version or 0), min_cv)
    if contract is None:
        # A contract is mandatory whenever this DEVICE speaks v2 (its own capability,
        # not the deletable payload marker) or the marker demands it. Only a true legacy
        # device with no marker may proceed without one.
        if required_cv >= 2:
            return "local_contract_required"
        return None # legacy (no-contract) payload: caller keeps old behaviour
    version = contract.get("contract_version")
    # Known, spoken by this device, and not below the cloud's marker (a v3 task
    # re-shaped as v2 would drop its floor).
    if (version not in CONTRACT_KEYS or version > int(device_contract_version or 0)
            or version < min_cv):
        return "local_contract_version_unsupported"
    if set(contract.keys()) != CONTRACT_KEYS[version]:
        return "local_contract_schema_invalid"
    if digest(contract) != task.get("contract_digest"):
        return "local_contract_digest_mismatch"
    # supervision_floor is bound too: a top-level floor that is missing, weaker or
    # present on a v2 contract does not match the signed one.
    # required_outputs is bound the same way: a stripped/altered top-level hard
    # contract no longer matches the signed v4 contract (fail-closed). For v2/v3 both
    # sides are absent (None == None) so the compat path is unaffected.
    for k in ("execution_id", "installation_id", "outputs", "prompt", "policy_digest",
              "supervision_floor", "required_outputs"):
        if task.get(k) != contract.get(k):
            return "local_contract_payload_mismatch"
    if _contract_inputs(task) != contract.get("inputs"):
        return "local_contract_payload_mismatch"
    if version >= 3:
        from .runner import SUPERVISION_RANK
        if contract["supervision_floor"] not in SUPERVISION_RANK:
            return "local_contract_floor_invalid"
    return None


def contract_digest(task):
    return digest(launch_contract(task))


class Fault(Exception):
    def __init__(self, code, status=409, *, reason=None, retryable_in_same_turn=None, advice=None):
        super().__init__(code)
        self.code, self.status = code, status
        # Optional structured fields for budget/backpressure faults.
        self.reason = reason
        self.retryable_in_same_turn = retryable_in_same_turn
        self.advice = advice

    def detail(self):
        """Structured tool-fault body; None fields omitted."""
        out = {"error": self.code}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.retryable_in_same_turn is not None:
            out["retryable_in_same_turn"] = self.retryable_in_same_turn
        if self.advice is not None:
            out["advice"] = self.advice
        return out


# Iron rule B: no non-SQLite blocking I/O while holding a store transaction.
# A thread-local depth counter lets external-I/O entry points (runner.command,
# execution.start/cancel/poll/copy) assert they are NOT nested inside atomic().
# SQLite writes themselves are fine — only Popen/HTTP/realpath/IPC are barred.
_txn = threading.local()


def in_transaction():
    return getattr(_txn, "depth", 0) > 0


def assert_no_txn_io(what):
    """Raise if called while a store transaction is open. Service turns this on
    unconditionally (production invariant); set_strict_txn_io is a test-only hook."""
    if _strict_txn_io[0] and in_transaction():
        raise Fault("io_inside_transaction:" + str(what), 500)


_strict_txn_io = [False]


def set_strict_txn_io(enabled):
    _strict_txn_io[0] = bool(enabled)


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.depth = 0
        # kinds mutated in the CURRENT outer txn.
        # Flushed to on_commit(kinds) AFTER the COMMIT succeeds (never before —
        # a pre-commit wake would see no change). on_commit is an in-memory Event
        # set; failures there must never poison the store, so it is best-effort.
        self._dirty_kinds = set()
        self._after_commit = {}
        self.on_commit = None
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS meta(version INTEGER NOT NULL);
        INSERT INTO meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM meta);
        CREATE TABLE IF NOT EXISTS objects(
          kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
          revision INTEGER NOT NULL, created REAL NOT NULL, PRIMARY KEY(kind,id));
        CREATE INDEX IF NOT EXISTS objects_kind ON objects(kind,created);
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, entity_id TEXT,
          revision INTEGER, created REAL);
        CREATE TABLE IF NOT EXISTS dedup(
          scope TEXT, key TEXT, digest TEXT NOT NULL, result TEXT NOT NULL,
          PRIMARY KEY(scope,key));
        CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
        """)
        # v1 = older schema; v2 = state machine (turn drops waiting,
        # coalesced wakeups). Old binaries check `!= 1` and refuse a v2 DB — that is
        # the deliberate rollback guard for a one-way migration. New code accepts
        # both and migrates 1->2 after the backfill (Service.recover path).
        if self.db.execute("SELECT version FROM meta").fetchone()[0] not in (1, 2):
            raise Fault("database_version_unsupported")
        # at most one running turn per Work is an invariant, not a hope.
        # Partial expression index enforces it at the storage layer (single-active
        # turn); a duplicate INSERT/UPDATE raises IntegrityError, translated to a
        # domain conflict by put(). Preflight repair guards against legacy dupes.
        self._ensure_single_running_turn_index()
        self._ensure_hot_query_indexes()
        self._ensure_history_correction_indexes()

    def _ensure_hot_query_indexes(self):
        """Composite expression indexes for the hot execution/scheduler queries.
        find/count inline the JSON path as a literal
        (never a bound parameter) so the planner can actually choose these; the
        leading `kind` + trailing `created` columns match the WHERE/ORDER shape
        (`kind=? AND json_extract(data,'$.field') IN/IS ? ORDER BY created,rowid`).
        json_extract on well-formed JSON never fails, so creation is safe and
        idempotent (IF NOT EXISTS). This is a pure read-path optimization — stored
        data and semantics are unchanged; a rolled-back binary simply ignores them."""
        specs = [
            ("objects_status", "json_extract(data,'$.status'),created"),
            ("objects_work", "json_extract(data,'$.work_id'),created"),
            ("objects_stream", "json_extract(data,'$.stream_id'),created"),
            ("objects_deadline", "json_extract(data,'$.deadline')"),
            ("objects_queue_deadline", "json_extract(data,'$.queue_deadline')"),
            # kind is the leading column, so one source_work_id index serves both
            # dependency and exchange lookups. owner_bot_id is the work/artifact
            # owner scan. Same shape as the other hot-query specs; IF NOT EXISTS.
            ("objects_source_work", "json_extract(data,'$.source_work_id'),created"),
            ("objects_owner_bot", "json_extract(data,'$.owner_bot_id'),created"),
        ]
        for name, cols in specs:
            self.db.execute("CREATE INDEX IF NOT EXISTS %s ON objects(kind,%s)" % (name, cols))
        # A full (kind, bot_id, started_at) index is eligible for every bot_id
        # equality, so with no sqlite_stat1 the planner picks it for COUNT too
        # and then json_extracts every turn. find/count bind kind as a parameter,
        # which cannot imply a partial index's kind='turn', so only latest_started
        # (literal kind, INDEXED BY) can use this.
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS turn_bot_started ON objects("
            "json_extract(data,'$.bot_id'), json_extract(data,'$.started_at')) "
            "WHERE kind='turn' AND json_extract(data,'$.started_at') IS NOT NULL")

    def _ensure_history_correction_indexes(self):
        """Bounded routing-correction lookups for history. Partial expression
        indexes serve the scalar fields. superseded_message_ids is a JSON array,
        which no expression index can serve, so a derived lookup table maps each
        superseded message id to its correction row. SQLite triggers keep it in step
        with `objects` (no Python write-path change, never stale, no catch-up). It is
        a lookup path only: callers re-read the canonical correction row."""
        for name, field in (("routing_correction_target", "target_message_id"),
                            ("routing_correction_into", "new_work_id"),
                            ("routing_correction_source", "correcting_message_id")):
            self.db.execute("CREATE INDEX IF NOT EXISTS %s ON objects(json_extract(data,'$.%s')) "
                            "WHERE kind='routing_correction'" % (name, field))
        refill = ("INSERT OR IGNORE INTO history_superseded(message_id,correction_rowid) "
                  "SELECT value, NEW.rowid FROM json_each(NEW.data,'$.superseded_message_ids') "
                  "WHERE type='text';")
        with self.atomic():
            fresh = not self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='history_superseded'").fetchone()
            self.db.execute("CREATE TABLE IF NOT EXISTS history_superseded("
                            "message_id TEXT NOT NULL, correction_rowid INTEGER NOT NULL, "
                            "PRIMARY KEY(message_id,correction_rowid)) WITHOUT ROWID")
            self.db.execute("CREATE TRIGGER IF NOT EXISTS history_superseded_ins AFTER INSERT ON objects "
                            "WHEN NEW.kind='routing_correction' BEGIN " + refill + " END")
            self.db.execute("CREATE TRIGGER IF NOT EXISTS history_superseded_upd AFTER UPDATE ON objects "
                            "WHEN NEW.kind='routing_correction' BEGIN "
                            "DELETE FROM history_superseded WHERE correction_rowid=NEW.rowid; " + refill + " END")
            self.db.execute("CREATE TRIGGER IF NOT EXISTS history_superseded_del AFTER DELETE ON objects "
                            "WHEN OLD.kind='routing_correction' BEGIN "
                            "DELETE FROM history_superseded WHERE correction_rowid=OLD.rowid; END")
            if fresh:
                self.db.execute("INSERT OR IGNORE INTO history_superseded(message_id,correction_rowid) "
                                "SELECT j.value, o.rowid FROM objects o, "
                                "json_each(o.data,'$.superseded_message_ids') j "
                                "WHERE o.kind='routing_correction' AND j.type='text'")

    def _ensure_single_running_turn_index(self):
        try:
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_running_turn_per_work "
                "ON objects(json_extract(data,'$.work_id')) "
                "WHERE kind='turn' AND json_extract(data,'$.status')='running'")
            return
        except sqlite3.IntegrityError:
            pass
        # Legacy duplicate running turns (should not occur single-threaded). Demote
        # all-but-one per work to queued so the invariant index can be created, then
        # retry. Startup recover() re-evaluates these turns.
        rows = self.db.execute(
            "SELECT id,data FROM objects WHERE kind='turn' "
            "AND json_extract(data,'$.status')='running'").fetchall()
        by_work = {}
        for rid, data in rows:
            obj = json.loads(data)
            by_work.setdefault(obj.get("work_id"), []).append((rid, obj))
        for work_id, group in by_work.items():
            for rid, obj in group[1:]:
                obj["status"] = "queued"
                self.db.execute("UPDATE objects SET data=? WHERE kind='turn' AND id=?",
                                (encode(obj), rid))
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_running_turn_per_work "
            "ON objects(json_extract(data,'$.work_id')) "
            "WHERE kind='turn' AND json_extract(data,'$.status')='running'")

    def schema_version(self):
        with self.lock:
            return self.db.execute("SELECT version FROM meta").fetchone()[0]

    def set_schema_version(self, version):
        with self.atomic():
            self.db.execute("UPDATE meta SET version=?", (int(version),))

    def next_seq(self, name="message"):
        """Monotonic durable sequence for causal ordering. Atomic
        under the store lock; unaffected by wall-clock collisions."""
        with self.atomic():
            row = self.db.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
            value = (row[0] if row else 0) + 1
            self.db.execute("INSERT INTO counters VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                            (name, value))
            return value

    @contextlib.contextmanager
    def atomic(self):
        with self.lock:
            outer = self.depth == 0
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
            self.depth += 1
            _txn.depth = getattr(_txn, "depth", 0) + 1
            committed = False
            try:
                yield
                if outer:
                    self.db.execute("COMMIT")
                    committed = True
            except BaseException:
                if outer:
                    self.db.execute("ROLLBACK")
                raise
            finally:
                self.depth -= 1
                _txn.depth -= 1
                if outer:
                    kinds = self._dirty_kinds
                    self._dirty_kinds = set()
                    callbacks, self._after_commit = self._after_commit, {}
                    # Publish prepared in-memory state before releasing the lock or
                    # waking workers. Rollbacks discard it, including nested writes.
                    if committed:
                        for callback in callbacks.values():
                            callback()
                    # Fire AFTER commit only. Best-effort:
                    # a wake callback must never turn a durable commit into a failure.
                    if committed and kinds and self.on_commit:
                        try:
                            self.on_commit(kinds)
                        except Exception:
                            pass

    def staged(self, key):
        """True when the current transaction already staged an after_commit for key."""
        return self.depth > 0 and key in self._after_commit

    def after_commit(self, key, callback):
        """Stage an assignment-only publication at the outermost commit.

        Repeated updates to one key publish only the final prepared value. The
        callback must not perform I/O or validation that could fail after commit.
        """
        if not self.depth:
            raise RuntimeError("after_commit requires an atomic transaction")
        self._after_commit[key] = callback

    def compare_and_set(self, kind, key, mutate, expect=None):
        """Atomic conditional update: re-read inside the transaction, check the
        `expect(obj)` predicate, and only then apply `mutate(obj)` and persist.
        Under the global RLock this is equivalent to a conditional UPDATE and is
        the primitive for state transitions (dependency settle, waiting<->active,
        turn claim). Returns (ok, obj): ok=False (with current obj) when the
        predicate fails, so callers can treat a lost race idempotently."""
        with self.atomic():
            obj = self.get(kind, key)
            if expect is not None and not expect(obj):
                return False, obj
            mutate(obj)
            return True, self.put(kind, obj)

    def get(self, kind, key):
        with self.lock:
            row = self.db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind, key)).fetchone()
            if not row:
                raise Fault("not_found", 404)
            return json.loads(row[0])

    def find(self, kind, *, limit=200, after=0, newest=False, omit=(), **where):
        # Field names are internal constants, values are always bound parameters.
        # omit lets read-heavy UI paths remove bulky top-level JSON fields in
        # SQLite before Python deserializes them. It is projection only; stored data
        # and authority semantics are unchanged.
        omit = tuple(omit or ())
        for field in omit:
            if not isinstance(field, str) or not field.replace("_", "").isalnum():
                raise ValueError("invalid projection")
        if omit:
            select = "json_remove(data," + ",".join("?" for _ in omit) + ")"
            params = ["$." + field for field in omit] + [kind]
        else:
            select = "data"
            params = [kind]
        sql = "SELECT " + select + " FROM objects WHERE kind=?"
        clause, values = _where_clause(where)
        sql += clause
        params += values
        sql += (" ORDER BY created DESC,rowid DESC" if newest else " ORDER BY created,rowid") + " LIMIT ? OFFSET ?"
        params += [min(max(limit, 1), 1000), max(after, 0)]
        # Multi-row reads hold Store.lock only for SQL + fetchall; JSON decode is
        # pure CPU and runs after release (N4). Inside an outer atomic() the
        # caller still holds the RLock, by design.
        with self.lock:
            rows = self.db.execute(sql, params).fetchall()
        return [json.loads(r[0]) for r in rows]

    def running_turn_id(self, work_id):
        """The id of this Work's running Turn, or None (C). Answered by the
        storage invariant itself — one_running_turn_per_work is a partial UNIQUE
        index, so this is one index seek with no JSON blob scan and no row cap.
        INDEXED BY turns a missing/unusable index into a SQL error, not a scan."""
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM objects INDEXED BY one_running_turn_per_work "
                "WHERE kind='turn' AND json_extract(data,'$.status')='running' "
                "AND json_extract(data,'$.work_id')=?", (work_id,)).fetchone()
        return row[0] if row else None

    def count(self, kind, **where):
        """Unbounded COUNT for a kind with optional json_extract equality filters.
        Used where find()'s 1000-row clamp would undercount (e.g. backpressure)."""
        sql = "SELECT COUNT(*) FROM objects WHERE kind=?"
        params = [kind]
        clause, values = _where_clause(where)
        sql += clause
        params += values
        with self.lock:
            return self.db.execute(sql, params).fetchone()[0]

    def find_due(self, kind, status, deadline_field, now, *, limit=1000):
        """Predicate-pushdown for wall-clock sweeps: return
        rows of (kind,status) whose numeric deadline_field is set and <= now, ORDER
        BY that deadline ascending (most overdue first). Replaces 'fetch all pending,
        compare in Python' with an index-eligible SQL filter (objects_deadline /
        objects_queue_deadline). deadline_field is an internal constant, inlined as a
        SQL literal (never a bound parameter) like find(); now/status/limit bind."""
        if not deadline_field.replace("_", "").isalnum():
            raise ValueError("invalid deadline field")
        path = "$." + deadline_field
        sql = ("SELECT data FROM objects WHERE kind=? "
               "AND json_extract(data,'$.status')=? "
               "AND json_extract(data,'" + path + "') IS NOT NULL "
               "AND json_extract(data,'" + path + "')<=? "
               "ORDER BY json_extract(data,'" + path + "') LIMIT ?")
        with self.lock:
            rows = self.db.execute(sql, [kind, status, now, min(max(limit, 1), 1000)]).fetchall()
        return [json.loads(r[0]) for r in rows]

    def soonest_future(self, kind, status, deadline_field, now):
        """The nearest FUTURE deadline_field value for (kind,status), or None.
        Index-direct MIN via ORDER BY... LIMIT 1 —
        O(log n), NOT an O(backlog) deserialize-and-scan (which the per-second
        worker wake would otherwise re-introduce). Only the scalar is read; no row
        is deserialized. deadline_field is an internal constant inlined as a literal."""
        if not deadline_field.replace("_", "").isalnum():
            raise ValueError("invalid deadline field")
        path = "$." + deadline_field
        sql = ("SELECT json_extract(data,'" + path + "') FROM objects WHERE kind=? "
               "AND json_extract(data,'$.status')=? "
               "AND json_extract(data,'" + path + "')>? "
               "ORDER BY json_extract(data,'" + path + "') LIMIT 1")
        with self.lock:
            row = self.db.execute(sql, [kind, status, now]).fetchone()
        return row[0] if row and row[0] is not None else None

    def latest_started(self, bot_id):
        """Newest turn.started_at for one bot, or None.

        Partial index turn_bot_started, forced with INDEXED BY so a missing
        index is a SQL error rather than a scan. Only the scalar is read.
        Rows with no started_at are absent; callers treat None as 0. `IS ?`
        matches find()'s NULL bot_id comparison."""
        sql = ("SELECT json_extract(data,'$.started_at') FROM objects "
               "INDEXED BY turn_bot_started "
               "WHERE kind='turn' AND json_extract(data,'$.bot_id') IS ? "
               "AND json_extract(data,'$.started_at') IS NOT NULL "
               "ORDER BY json_extract(data,'$.started_at') DESC LIMIT 1")
        with self.lock:
            row = self.db.execute(sql, [bot_id]).fetchone()
        return row[0] if row and row[0] is not None else None

    def find_page(self, kind, *, cursor=None, limit=1000, newest=False, **where):
        """Keyset-paginated scan for _all. The cursor
        value stays (created, id) — NOT OFFSET (which gets linearly more expensive
        and can skip/repeat rows under concurrent inserts). Equal timestamps break
        by rowid, the same insertion order find() uses, not by id. Returns
        (rows, next_cursor); next_cursor is None when the page was not full (end
        of set). newest=False walks ascending; newest=True walks descending.
        Equal timestamps neither drop nor duplicate when the cursor id resolves
        to a rowid. A cursor id with no row compares created alone and drops
        other rows that share that timestamp."""
        params = [kind]
        sql = "SELECT data,created,id FROM objects WHERE kind=?"
        clause, values = _where_clause(where)
        sql += clause
        params += values
        limit = min(max(limit, 1), 1000)
        with self.lock:
            if cursor is not None:
                found = self.db.execute("SELECT rowid FROM objects WHERE kind=? AND id=?",
                                        (kind, cursor[1])).fetchone()
                rid = found[0] if found else None
                if rid is None:
                    sql += " AND created<?" if newest else " AND created>?"
                    params.append(cursor[0])
                else:
                    sql += (" AND (created<? OR (created=? AND rowid<?))" if newest
                            else " AND (created>? OR (created=? AND rowid>?))")
                    params += [cursor[0], cursor[0], rid]
            sql += (" ORDER BY created DESC,rowid DESC LIMIT ?" if newest
                    else " ORDER BY created,rowid LIMIT ?")
            params.append(limit)
            rows = self.db.execute(sql, params).fetchall()
        objs = [json.loads(r[0]) for r in rows]
        next_cursor = (rows[-1][1], rows[-1][2]) if len(rows) == limit else None
        return objs, next_cursor

    def put(self, kind, obj, expected=None):
        with self.atomic():
            row = self.db.execute("SELECT revision,created FROM objects WHERE kind=? AND id=?", (kind, obj["id"])).fetchone()
            revision = row[0] if row else 0
            if expected is not None and revision != expected:
                raise Fault("state_conflict")
            data = dict(obj, revision=revision + 1)
            try:
                self.db.execute("INSERT INTO objects VALUES(?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,revision=excluded.revision",
                                (kind, obj["id"], encode(data), revision + 1, row[1] if row else time.time()))
            except sqlite3.IntegrityError:
                # The single-running-turn partial index rejected a second running
                # turn for the same Work (invariant).
                raise Fault("work_already_running") from None
            # Events carry IDs only; private body/state never broadcast through SSE.
            seq = self.db.execute("INSERT INTO events(type,entity_id,revision,created) VALUES(?,?,?,?)",
                                  (kind + ".changed", obj["id"], revision + 1, time.time())).lastrowid
            # Events are invalidations only (B): keep a bounded tail. A client
            # whose cursor falls below the retained floor is reset by /events, never
            # replayed. AUTOINCREMENT keeps seq monotonic across deletes. dedup is
            # the exactly-once record and is never pruned.
            if seq % EVENT_PRUNE_EVERY == 0:
                self.db.execute("DELETE FROM events WHERE seq<=?", (seq - EVENT_RETENTION,))
            self._dirty_kinds.add(kind)
            return data

    def once_lookup(self, scope, key, payload):
        """Read-only replay lookup for pre-gate idempotency.

        This MUST run before any probabilistic semantic gate. A committed action
        is reality already; recovery may replay it, but a fresh Jev decision must
        never be allowed to hide or reinterpret that committed result.
        """
        if not key or len(key) > 200:
            raise Fault("idempotency_key_required", 400)
        expected = digest(payload)
        with self.lock:
            row = self.db.execute("SELECT digest,result FROM dedup WHERE scope=? AND key=?", (scope, key)).fetchone()
        if not row:
            return None
        if row[0] != expected:
            raise Fault("idempotency_conflict")
        return json.loads(row[1])

    def once(self, scope, key, payload, action):
        if not key or len(key) > 200:
            raise Fault("idempotency_key_required", 400)
        with self.atomic():
            row = self.db.execute("SELECT digest,result FROM dedup WHERE scope=? AND key=?", (scope, key)).fetchone()
            if row:
                if row[0] != digest(payload):
                    raise Fault("idempotency_conflict")
                return json.loads(row[1])
            result = action()
            self.db.execute("INSERT INTO dedup VALUES(?,?,?,?)", (scope, key, digest(payload), encode(result)))
            return result

    def journal_once(self, scope, key, payload, result):
        """Idempotency journal for an ALREADY-COMPUTED result (Invariant): lets a
        read-only external call (e.g. web_search HTTP) run OUTSIDE the transaction and
        only record its result here, so no blocking network I/O is ever held under the
        store lock. Same dedup semantics as once(): a committed result wins; a payload
        digest mismatch on the same key is a conflict. Crash between the external call
        and this journal simply re-runs the read-only call on replay (no real-world
        side effect)."""
        if not key or len(key) > 200:
            raise Fault("idempotency_key_required", 400)
        with self.atomic():
            row = self.db.execute("SELECT digest,result FROM dedup WHERE scope=? AND key=?", (scope, key)).fetchone()
            if row:
                if row[0] != digest(payload):
                    raise Fault("idempotency_conflict")
                return json.loads(row[1])
            self.db.execute("INSERT INTO dedup VALUES(?,?,?,?)", (scope, key, digest(payload), encode(result)))
            return result

    def events(self, after=0):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 200", (after,))]

    def event_bounds(self):
        """(floor, head): the oldest retained seq and the high-water seq ever
        issued. With no retained rows floor is head+1, so cursor==head is still
        continuous."""
        with self.lock:
            low, high = self.db.execute("SELECT MIN(seq),MAX(seq) FROM events").fetchone()
            row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        head = max(row[0] if row else 0, high or 0)
        return (low if low is not None else head + 1), head

    def keyset(self, kind, key):
        """Stored (created, id) keyset of one row: the cursor find_page() expects.
        find_page resolves id to that row's rowid when timestamps tie."""
        with self.lock:
            row = self.db.execute("SELECT created FROM objects WHERE kind=? AND id=?", (kind, key)).fetchone()
        if not row:
            raise Fault("not_found", 404)
        return (row[0], key)

    # --- Historical retrieval index -------------------------------------
    # A derived, contentless FTS5 trigram index over message text. It is never a
    # source of truth or authority: hits are rowids that callers re-read from
    # `objects` and re-authorize. It is maintained lazily from the retrieval call
    # (bounded catch-up by objects.rowid cursor), never from put/atomic/once, so
    # the shared write path and startup stay unchanged. Any SQLite without FTS5 or
    # the trigram tokenizer (< 3.34) degrades to "scan" mode instead of failing.
    HISTORY_FTS_CURSOR = "history_fts_rowid"
    _history_tokenizer = "trigram" # overridable in tests to simulate an old SQLite

    def history_index_mode(self):
        """'fts5_trigram' when usable on this SQLite build, else 'scan'. Probed once."""
        mode = getattr(self, "_history_mode", None)
        if mode:
            return mode
        with self.lock:
            try:
                with self.atomic():
                    exists = self.db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='history_fts'").fetchone()
                    if not exists:
                        self.db.execute(
                            "CREATE VIRTUAL TABLE history_fts USING fts5("
                            "body, scope, content='', tokenize='" + self._history_tokenizer + "')")
                        # A fresh index restarts its cursor (e.g. a DB moved from a
                        # machine whose SQLite lacked FTS5).
                        self.db.execute("DELETE FROM counters WHERE name=?", (self.HISTORY_FTS_CURSOR,))
                    self.db.execute("SELECT rowid FROM history_fts WHERE history_fts MATCH '\"probe\"' LIMIT 1").fetchall()
                self._history_mode = "fts5_trigram"
            except sqlite3.Error:
                self._history_mode = "scan"
            return self._history_mode

    def history_index_catch_up(self, *, window=2000, max_windows=10, byte_budget=4 * 1024 * 1024):
        """Index messages inserted since the cursor, under a row budget AND a source
        byte budget: at most max_windows short transactions of `window` objects
        rowids, stopping as soon as `byte_budget` content bytes were tokenized in
        this call (each transaction always advances by at least one row). The
        FULL stored content is indexed, never a prefix. The index holds only the
        immutable text and bot_id; a message's Work can change in place (link_work),
        so Work scope is always read from the canonical row at query time.
        Returns True when the index is current."""
        spent = 0
        for _ in range(max_windows):
            with self.atomic():
                row = self.db.execute("SELECT value FROM counters WHERE name=?",
                                      (self.HISTORY_FTS_CURSOR,)).fetchone()
                cursor = row[0] if row else 0
                top = self.db.execute("SELECT max(rowid) FROM objects").fetchone()[0] or 0
                if cursor >= top:
                    return True
                hi, batch = min(cursor + window, top), []
                rows = self.db.execute(
                    "SELECT rowid, coalesce(json_extract(data,'$.content'),''), "
                    "coalesce(json_extract(data,'$.bot_id'),'') FROM objects "
                    "WHERE rowid>? AND rowid<=? AND kind='message' ORDER BY rowid", (cursor, hi))
                try:
                    for rowid, body, bot in rows: # lazy: rows past the budget are never read
                        body = str(body)
                        batch.append((rowid, body, bot))
                        spent += len(body.encode())
                        if spent >= byte_budget:
                            hi = rowid
                            break
                finally:
                    rows.close()
                self.db.executemany("INSERT INTO history_fts(rowid,body,scope) VALUES(?,?,?)", batch)
                self.db.execute("INSERT INTO counters VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                                (self.HISTORY_FTS_CURSOR, hi))
            if spent >= byte_budget:
                break
        with self.lock:
            row = self.db.execute("SELECT value FROM counters WHERE name=?", (self.HISTORY_FTS_CURSOR,)).fetchone()
            top = self.db.execute("SELECT max(rowid) FROM objects").fetchone()[0] or 0
        return (row[0] if row else 0) >= top

    def history_match(self, expression, *, bot_id, work_id=None, max_sequence=None, limit=200,
                      correction_budget=16):
        """Newest-first message rows whose index entry matches an FTS5 expression
        (built from quoted phrases only). Bot, Work and causal-sequence scope are
        filtered on the CANONICAL objects row inside SQL, before LIMIT, so a stale
        index entry can neither widen nor silently narrow a scoped result."""
        sql = ("SELECT o.data FROM history_fts f JOIN objects o ON o.rowid=f.rowid "
               "WHERE history_fts MATCH ? AND o.kind='message' "
               "AND json_extract(o.data,'$.bot_id')=?")
        params = [expression, bot_id]
        if work_id is not None:
            # Raw rows of the Work, or rows corrected INTO it: a per-candidate
            # indexed EXISTS, so cost follows the matches, not the correction log.
            # Raw rows of the Work, or candidates whose newest `correction_budget`
            # corrections include one into it, or whose correction history exceeds
            # that budget (the caller then decides and flags truncation). Each
            # check is a bounded index seek; unary + drops column affinity so the
            # expression index is seekable.
            target = ("SELECT {col} FROM objects INDEXED BY routing_correction_target "
                      "WHERE kind='routing_correction' AND json_extract(data,'$.target_message_id')=+o.id ")
            sql += (" AND (json_extract(o.data,'$.work_id')=? OR ? IN ("
                    + target.format(col="json_extract(data,'$.new_work_id')") + "ORDER BY rowid DESC LIMIT ?)"
                    " OR EXISTS (" + target.format(col="1") + "LIMIT 1 OFFSET ?))")
            params += [work_id, work_id, correction_budget, correction_budget]
        if max_sequence is not None:
            sql += " AND coalesce(json_extract(o.data,'$.sequence'),0)<=?"
            params.append(max_sequence)
        sql += " ORDER BY f.rowid DESC LIMIT ?"
        params.append(min(max(limit, 1), 1000))
        with self.lock:
            rows = self.db.execute(sql, params).fetchall()
        return [json.loads(r[0]) for r in rows]

    # Each correction comes with its correcting message's causal position:
    # (rowid, correction, source_found, source_sequence). One JOIN, no N+1.
    _CORRECTION_SELECT = (
        "SELECT c.rowid, c.data, m.id IS NOT NULL, json_extract(m.data,'$.sequence') "
        "FROM objects c LEFT JOIN objects m ON m.kind='message' "
        "AND m.id=json_extract(c.data,'$.correcting_message_id') ")

    def _correction_rows(self, sql, params):
        with self.lock:
            rows = self.db.execute(sql, params).fetchall()
        return [(r[0], json.loads(r[1]), bool(r[2]), r[3]) for r in rows]

    # Correction rowid order equals causal order: Service.send stamps the
    # correcting message's sequence and inserts its correction in one serialized
    # transaction. So "newest K by rowid" is "causally latest K" and a fixed
    # per-message budget is exact; callers flag anything beyond it.
    def routing_corrections_for_target(self, message_id, *, limit):
        """Newest-first corrections whose target is this message (bounded)."""
        return self._correction_rows(
            self._CORRECTION_SELECT.replace("FROM objects c ", "FROM objects c INDEXED BY routing_correction_target ")
            + "WHERE c.kind='routing_correction' AND json_extract(c.data,'$.target_message_id')=? "
            "ORDER BY c.rowid DESC LIMIT ?", [message_id, min(max(limit, 1), 1000)])

    def routing_corrections_superseding(self, message_id, *, limit):
        """Oldest-first corrections that list this message as superseded (bounded)."""
        return self._correction_rows(
            self._CORRECTION_SELECT + "JOIN history_superseded s ON s.correction_rowid=c.rowid "
            "WHERE s.message_id=? ORDER BY s.correction_rowid LIMIT ?",
            [message_id, min(max(limit, 1), 1000)])

    def routing_corrections_into(self, work_id, *, before=None, limit=11):
        """Newest-first corrections whose new_work_id is `work_id`, at most `limit`
        rows examined; `before` is a correction id (keyset by storage rowid).
        Causal eligibility is decided by the caller on exactly these rows."""
        inner = ("SELECT rowid FROM objects INDEXED BY routing_correction_into "
                 "WHERE kind='routing_correction' AND json_extract(data,'$.new_work_id')=?")
        params = [work_id]
        if before:
            inner += " AND rowid<(SELECT rowid FROM objects WHERE kind='routing_correction' AND id=?)"
            params.append(before)
        inner += " ORDER BY rowid DESC LIMIT ?"
        params.append(min(max(limit, 1), 1000))
        return self._correction_rows(
            self._CORRECTION_SELECT + "WHERE c.rowid IN (" + inner + ") ORDER BY c.rowid DESC", params)

    def routing_correction_made_by(self, message_id):
        """The newest correction created by this correcting message (indexed)."""
        rows = self._correction_rows(
            self._CORRECTION_SELECT.replace("FROM objects c ", "FROM objects c INDEXED BY routing_correction_source ")
            + "WHERE c.kind='routing_correction' "
            "AND json_extract(c.data,'$.correcting_message_id')=? ORDER BY c.rowid DESC LIMIT 1",
            [message_id])
        return rows[0][1] if rows else None

    def close(self):
        with self.lock:
            self.db.close()
            callback = getattr(self, "on_close", None)
            if callback:
                self.on_close = None
                callback()
