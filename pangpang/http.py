"""Same-origin, authenticated Web console and bounded REST/SSE surface."""
import hmac
import json
import mimetypes
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, quote

from .store import Fault, encode, uid
from .execution import artifact_bytes
from .attention import project
from .logs import get_logger

log = get_logger("http")


SENSITIVE_CONFIG_KEYS = {"credential_hash", "credential_env", "credential_ref", "secret_refs", "vault_path"}
ARTIFACT_PREVIEW_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp",
                          "audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg", "audio/flac",
                          "video/mp4", "video/webm", "video/ogg", "application/pdf"}


def public_config(value):
    """Return Web-safe configuration metadata without credential material or references."""
    if isinstance(value, dict):
        return {k: public_config(v) for k, v in value.items() if k not in SENSITIVE_CONFIG_KEYS}
    if isinstance(value, list):
        return [public_config(v) for v in value]
    return value


def serve(service, token, host="127.0.0.1", port=8790, origin=None):
    if len(token) < 24:
        raise Fault("token_must_be_at_least_24_characters")
    origin = origin or f"http://{host}:{port}"
    sessions, failures = {}, {}
    auth_lock = threading.Lock()
    web = Path(__file__).parent / "web"

    def cleanup_auth_state(now):
        """Bound in-memory auth bookkeeping; caller must hold auth_lock."""
        for sid, expiry in list(sessions.items()):
            if expiry <= now:
                sessions.pop(sid, None)
        for key, values in list(failures.items()):
            recent = [t for t in values if now - t < 60]
            if recent:
                failures[key] = recent[-10:]
            else:
                failures.pop(key, None)
        if len(sessions) > 512:
            for sid, _ in sorted(sessions.items(), key=lambda item: item[1])[:len(sessions) - 512]:
                sessions.pop(sid, None)
        if len(failures) > 256:
            oldest = sorted(failures, key=lambda key: failures[key][-1] if failures[key] else 0)
            for key in oldest[:len(failures) - 256]:
                failures.pop(key, None)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def security_headers(self, csp=None):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", csp or "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header("Referrer-Policy", "no-referrer")

        def respond(self, value, status=200, cookie=None, media="application/json; charset=utf-8", download=None, csp=None):
            data = value if isinstance(value, bytes) else encode(value).encode()
            self.send_response(status)
            self.security_headers(csp)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(data)))
            if download is not None:
                # R1: untrusted artifacts never render/execute in the admin origin.
                # Force download and strip any same-origin script/exec capability.
                self.send_header("Content-Disposition", "attachment; filename=\"download\"; filename*=UTF-8''" + quote(download, safe=""))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        def body(self, limit=256 * 1024):
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise Fault("invalid_content_length", 400)
            if size < 0 or size > limit:
                self.close_connection = True
                raise Fault("body_too_large", 413)
            try:
                result = json.loads(self.rfile.read(size) or b"{}")
            except ValueError:
                raise Fault("invalid_json", 400)
            if not isinstance(result, dict):
                raise Fault("json_object_required", 400)
            return result

        def authenticate(self, mutation=False):
            if self.headers.get("Host") != urlsplit(origin).netloc:
                raise Fault("host_rejected", 403)
            supplied_origin = self.headers.get("Origin")
            if supplied_origin and supplied_origin != origin:
                raise Fault("origin_rejected", 403)
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], token):
                return
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                sid = cookie["pangpang"].value if "pangpang" in cookie else ""
            except Exception:
                sid = ""
            with auth_lock:
                now = time.time()
                cleanup_auth_state(now)
                expires = sessions.get(sid, 0)
            if expires <= time.time():
                raise Fault("login_required", 401)
            if mutation and supplied_origin != origin:
                raise Fault("origin_required", 403)

        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                if path in {"/", "/app.js", "/app.css", "/setup.js"}:
                    if self.headers.get("Host") != urlsplit(origin).netloc:
                        raise Fault("host_rejected", 403)
                    name, media = {"/": ("index.html", "text/html; charset=utf-8"),
                                   "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                                   "/app.css": ("app.css", "text/css; charset=utf-8"),
                                   "/setup.js": ("setup.js", "text/javascript; charset=utf-8")}[path]
                    self.respond((web / name).read_bytes(), media=media)
                    return
                self.authenticate()
                parts = path.removeprefix("/api/v1/").strip("/").split("/")
                query = parse_qs(urlsplit(self.path).query)
                if path == "/api/v1/events":
                    self.events(query)
                    return
                if len(parts) == 3 and parts[0] == "bots" and parts[2] == "stream":
                    self.bot_stream(parts[1])
                    return
                if path == "/api/v1/diagnostics":
                    # Operator-only observability aggregation. authenticate()
                    # above already gated this to the logged-in operator; a Bot has
                    # no HTTP session and cannot reach it.
                    self.respond(service.diagnostics())
                    return
                if len(parts) == 3 and parts[0] == "setup" and parts[2] == "secret-target":
                    self.respond(service.onboarding.secret_target(parts[1]))
                    return
                if path == "/api/v1/snapshot":
                    # Snapshot is dashboard state, not transcript export. Pulling
                    # 1000 full Turn JSON blobs used to deserialize every model
                    # history under Store.lock only to drop history in public_turn().
                    # Keep a bounded recent window plus all live turns, and remove
                    # history in SQLite before Python JSON parsing.
                    recent_turns = service.store.find(
                        "turn", newest=True, limit=200, omit=("history",))
                    live_turns = []
                    trunc_turns = False
                    for status in ("queued", "running", "blocked"):
                        page = service.store.find(
                            "turn", status=status, limit=1000, omit=("history",))
                        live_turns.extend(page)
                        trunc_turns = trunc_turns or len(page) >= 1000
                    turns_by_id = {t["id"]: t for t in recent_turns}
                    turns_by_id.update({t["id"]: t for t in live_turns})
                    public_turns = sorted(
                        (self.public_turn(t) for t in turns_by_id.values()),
                        key=lambda t: (t.get("created", 0), t.get("id", "")))
                    # completeness WITHOUT the performance regression:
                    # read live attention facts in BOUNDED single-status index pages
                    # (objects_status) — NOT a full keyset drain of the whole backlog
                    # (that made this hot /snapshot path O(live backlog) with unbounded
                    # payload). One newest page per status proves completeness up to the
                    # cap; if any single live domain exceeds the cap the summary is
                    # flagged incomplete (counts are lower bounds) rather than reading
                    # the whole book or pretending absence == settled (RCB fail-closed).
                    LIVE_CAP = 1000
                    def live_page(kind, statuses):
                        rows, truncated = {}, False
                        for status in statuses:
                            page, cursor = service.store.find_page(kind, status=status, limit=LIVE_CAP, newest=True)
                            for o in page:
                                rows.setdefault(o["id"], o)
                            truncated = truncated or cursor is not None
                        return list(rows.values()), truncated
                    def union(display, extra):
                        by_id = {o["id"]: o for o in display}
                        for o in extra:
                            by_id.setdefault(o["id"], o)
                        return sorted(by_id.values(), key=lambda o: (o.get("created", 0), o.get("id", "")))
                    # Shipped payload stays at the original bounded display windows.
                    work = list(reversed(service.store.find("work", newest=True, limit=1000)))
                    tasks = list(reversed(service.store.find("task", newest=True, limit=1000)))
                    exchanges = list(reversed(service.store.find("exchange", newest=True, limit=500)))
                    notifications = list(reversed(service.store.find("notification", newest=True, limit=500)))
                    claims, claims_cursor = service.store.find_page("claim", status="unknown", limit=1000, newest=True)
                    routine_page, routine_cursor = service.store.find_page("routine", limit=1000, newest=True)
                    routines = [service.routine_review(r) for r in routine_page]
                    # Fact sets for the projection: the bounded display window UNION one
                    # bounded newest live page per status, so a live Work/Task/Exchange
                    # evicted from the display window is still seen (complete up to cap).
                    live_work, trunc_work = live_page("work", ("active", "waiting", "blocked"))
                    live_tasks, trunc_tasks = live_page("task", ("queued", "running", "starting",
                                                                 "cancel_requested", "unknown", "awaiting_approval"))
                    live_exch, trunc_exch = live_page("exchange", ("pending",))
                    unread, unread_cursor = service.store.find_page("notification", read=False, limit=1000, newest=True)
                    complete = not (trunc_work or trunc_tasks or trunc_exch or trunc_turns
                                    or claims_cursor is not None or unread_cursor is not None
                                    or routine_cursor is not None)
                    #: server owns the deterministic projection; the client only
                    # renders it (RCB). Projection reads the bounded fact sets above.
                    # `complete` propagates to BOTH the summary AND per-Work attention:
                    # when a live domain was truncated, a Work we cannot prove idle is
                    # reported `unknown` (fail-closed) instead of a false `settled`.
                    states, attention_summary = project(
                        union(work, live_work), public_turns, union(tasks, live_tasks),
                        union(exchanges, live_exch), routines, unread, claims,
                        now=time.time(), complete=complete,
                        notifications_complete=unread_cursor is None)
                    for w in work:
                        w["attention"] = states.get(w["id"], "settled" if complete else "unknown")
                    self.respond({"setup": service.onboarding.status(), "bots": service.store.find("bot"),
                                  "work": work,
                                  "tasks": tasks,
                                  "turns": public_turns,
                                  "artifacts": list(reversed(service.store.find("artifact", newest=True, limit=1000))),
                                  "claims": claims,
                                  "projects": service.store.find("project", limit=1000),
                                  "routines": routines,
                                  "routine_runs": list(reversed(service.store.find("routine_run", newest=True, limit=200))),
                                  "notifications": notifications,
                                  "exchanges": exchanges,
                                  "attention_summary": attention_summary,
                                  "devices": service.broker.devices(),
                                  "hosts": public_config(list(service.execution.hosts.values())),
                                  "installations": public_config(list(service.execution.installations.values())),
                                  "models": [{"id": p["id"], "model": p.get("model", ""), "configured": bool(p.get("endpoint")),
                                              "context_window_tokens": p.get("context_window_tokens")}
                                             for p in service.profiles.values()],
                                  "probes": service.store.find("probe")})
                    return
                if path == "/api/v1/setup/model-catalog":
                    from .model_config import catalog
                    self.respond(catalog()); return
                if path == "/api/v1/settings":
                    self.respond(public_config(service.config)); return
                if parts == ["hosts"]:
                    self.respond(public_config(list(service.execution.hosts.values()))); return
                if parts == ["projects"]:
                    self.respond(service.store.find("project")); return
                if parts == ["routines"]:
                    self.respond([service.routine_review(r) for r in service.store.find("routine")]); return
                if len(parts) == 3 and parts[0] == "hosts" and parts[2] == "executors":
                    self.respond(public_config([e for e in service.execution.installations.values() if e["host_id"] == parts[1]])); return
                if len(parts) == 3 and parts[0] == "turns" and parts[2] == "context":
                    turn = service.store.get("turn", parts[1])
                    self.respond({"messages": turn["history"], "metrics": turn.get("context_metrics"), "usage": turn.get("usage")}); return
                if len(parts) == 3 and parts[0] == "bots" and parts[2] in {"messages", "state"}:
                    service.bot(parts[1])
                    if parts[2] == "state":
                        result = service.store.get("state", parts[1])
                    else:
                        offset = max(0, int(query.get("offset", [0])[0]))
                        limit = min(200, max(1, int(query.get("limit", [200])[0])))
                        result = list(reversed(service.store.find("message", bot_id=parts[1], after=offset, limit=limit, newest=True)))
                    self.respond(result); return
                if parts == ["artifacts"]:
                    # Long-lived artifact browser: bounded server keyset pagination.
                    # /snapshot intentionally stays bounded; this read view lets the
                    # UI traverse canonical Artifact rows without OFFSET/full-history
                    # materialization. Cursor identity is an immutable Artifact id.
                    limit = min(100, max(1, int(query.get("limit", [20])[0])))
                    cursor_id = (query.get("cursor") or [None])[0]
                    cursor = service.store.keyset("artifact", cursor_id) if cursor_id else None
                    rows, _ = service.store.find_page("artifact", cursor=cursor, limit=limit + 1, newest=True)
                    more = len(rows) > limit
                    rows = rows[:limit]
                    self.respond({"items": rows, "next_cursor": rows[-1]["id"] if more and rows else None})
                    return
                if len(parts) == 2 and parts[0] == "artifacts":
                    artifact = service.store.get("artifact", parts[1])
                    if "download" in query:
                        data = service.execution.read_artifact(artifact)
                        raw = artifact_bytes(artifact, data)
                        # R1: an artifact is untrusted data, never admin-origin code.
                        # Finite preview exceptions preserve the existing media/PDF
                        # UI. PDF is not claimed to be free of active content; public
                        # deployments still require the isolation acceptance checks.
                        name = Path(str(artifact.get("path", ""))).name or "download"
                        safe = "".join(c for c in name if c.isprintable() and c not in '"\\/:*?<>|\r\n')[:120] or "download"
                        guessed = mimetypes.guess_type(name)[0] or ""
                        if guessed in ARTIFACT_PREVIEW_TYPES:
                            self.respond(raw, media=guessed, csp="default-src 'none'; sandbox")
                        else:
                            self.respond(raw, media="application/octet-stream",
                                         download=safe, csp="default-src 'none'; sandbox")
                    else:
                        self.respond(artifact)
                    return
                kind = {"bots": "bot", "tasks": "task", "work": "work", "routines": "routine",
                        "notifications": "notification", "exchanges": "exchange"}.get(parts[0])
                if kind:
                    result = service.store.get(kind, parts[1]) if len(parts) == 2 else service.store.find(kind, after=int(query.get("offset", [0])[0]))
                    if kind == "routine":
                        result = service.routine_review(result) if isinstance(result, dict) else [service.routine_review(r) for r in result]
                    self.respond(result)
                    return
                raise Fault("not_found", 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                self.error(error)

        @staticmethod
        def public_turn(turn):
            return {k: v for k, v in turn.items() if k != "history"}

        @staticmethod
        def event_cursor(header, query):
            """Explicit SSE cursor, or None when absent/invalid. Absent is NOT 0:
            0 is a legal cursor (empty event log); absent means 'reset'."""
            raw = header if header not in (None, "") else (query.get("after") or [None])[0]
            try:
                cursor = int(raw)
            except (TypeError, ValueError):
                return None
            return cursor if cursor >= 0 else None

        def events(self, query):
            # Events are invalidations; the snapshot is the state. One
            # continuity rule: a missing cursor, a cursor behind the retained floor
            # (pruned gap) or ahead of head (DB restored from backup) gets a single
            # reset at head, which makes the client re-snapshot. The snapshot runs
            # after head was read, so <=head is in it and >head is streamed: no
            # lost update, no history replay.
            after = self.event_cursor(self.headers.get("Last-Event-ID"), query)
            floor, head = service.store.event_bounds()
            reset = after is None or after > head or after < floor - 1
            self.send_response(200)
            self.security_headers()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if reset:
                after = head
                self.wfile.write(f"id: {head}\ndata: {encode({'type': 'reset'})}\n\n".encode())
            for _ in range(15):
                rows = service.store.events(after)
                for row in rows:
                    self.wfile.write(f"id: {row['seq']}\ndata: {encode(row)}\n\n".encode())
                    after = row["seq"]
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(1)

        def bot_stream(self, bot_id):
            """Per-bot live token stream (UI-only). Tails service.live_deltas for
            this bot and pushes accumulated assistant text as it grows, keyed by rev
            (never by text length — a post-tool assistant message restarts empty).
            Ephemeral: nothing here is persisted or in store.events. Auth is the same
            authenticate() gate as every other GET (browser EventSource → cookie)."""
            self.send_response(200)
            self.security_headers()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            sent = {} # (turn_id, attempt) -> {"rev": int, "done": bool}
            for _ in range(150): # ~150 * 120ms ≈ 18s, then the client reconnects
                snap = service.snapshot_deltas(bot_id)
                for (turn_id, _attempt), entry in snap.items():
                    prior = sent.get((turn_id, _attempt), {"rev": -1, "done": False})
                    if entry["rev"] > prior["rev"]:
                        payload = {"turn_id": turn_id, "text": entry["text"], "rev": entry["rev"]}
                        self.wfile.write(f"data: {encode(payload)}\n\n".encode())
                        prior = {"rev": entry["rev"], "done": prior["done"]}
                    if entry.get("done") and not prior["done"]:
                        self.wfile.write(f"data: {encode({'turn_id': turn_id, 'done': True})}\n\n".encode())
                        prior["done"] = True
                    sent[(turn_id, _attempt)] = prior
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(0.12)

        def do_POST(self):
            try:
                path = urlsplit(self.path).path
                if path == "/api/v1/pair":
                    if self.headers.get("Host") != urlsplit(origin).netloc or self.headers.get("Origin"):
                        raise Fault("device_origin_rejected", 403)
                    with auth_lock:
                        now = time.time(); cleanup_auth_state(now)
                        ip = "pair:" + self.client_address[0]
                        attempts = failures.get(ip, [])
                        if len(attempts) >= 10:
                            raise Fault("pairing_rate_limited", 429)
                        failures[ip] = (attempts + [now])[-10:]
                    self.respond(service.onboarding.pair(self.body(16384))); return
                if path == "/api/v1/connector/sync":
                    if self.headers.get("Host") != urlsplit(origin).netloc or self.headers.get("Origin"):
                        raise Fault("device_origin_rejected", 403)
                    host_id = service.broker.authenticate(self.headers.get("X-PangPang-Host", ""), self.headers.get("Authorization", ""))
                    self.respond(service.broker.sync(host_id, self.body(12 * 1024 * 1024))); return
                if path == "/api/v1/login":
                    if self.headers.get("Host") != urlsplit(origin).netloc or self.headers.get("Origin") != origin:
                        raise Fault("origin_rejected", 403)
                    body = self.body(); ip = self.client_address[0]
                    with auth_lock:
                        now = time.time(); cleanup_auth_state(now)
                        attempts = failures.get(ip, [])
                        if len(attempts) >= 5:
                            raise Fault("login_rate_limited", 429)
                        if not hmac.compare_digest(str(body.get("token", "")), token):
                            failures[ip] = (attempts + [now])[-5:]
                            raise Fault("invalid_login", 401)
                        sid = secrets.token_urlsafe(32)
                        sessions[sid] = now + 43200
                        cleanup_auth_state(now)
                    self.respond({"ok": True}, cookie=f"pangpang={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200" + ("; Secure" if origin.startswith("https:") else "")); return
                self.authenticate(mutation=True)
                # A chat upload carries one file (<= 8 MiB) as base64; every other
                # mutation keeps the small default bound.
                upload = path.startswith("/api/v1/bots/") and path.endswith("/uploads")
                body = self.body(12 * 1024 * 1024 if upload else 256 * 1024)
                parts = path.removeprefix("/api/v1/").strip("/").split("/")
                if path == "/api/v1/logout":
                    cookie = SimpleCookie(self.headers.get("Cookie", ""))
                    if "pangpang" in cookie:
                        with auth_lock:
                            sessions.pop(cookie["pangpang"].value, None)
                    self.respond({"ok": True}, cookie="pangpang=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"); return
                if parts == ["setup", "model"]:
                    self.respond(service.onboarding.model(body)); return
                if parts == ["setup", "model-key", "revoke"]:
                    self.respond(service.onboarding.revoke_model_key()); return
                if parts == ["setup", "pairing"]:
                    self.respond(service.onboarding.pairing_code()); return
                if len(parts) == 3 and parts[0] == "setup":
                    if parts[2] == "approve":
                        self.respond(service.onboarding.approve(parts[1], body)); return
                    if parts[2] == "secret":
                        self.respond(service.onboarding.secret(parts[1], body)); return
                key = self.headers.get("Idempotency-Key")
                routing = [None] # Work router result, computed outside Store transactions.
                normalized_chat_body = [None]
                installation_config = [None]
                def action():
                    if parts == ["settings"]:
                        raise Fault("use_guided_model_and_conversational_setup", 410)
                    if len(parts) == 3 and parts[0] == "devices" and parts[2] == "revoke":
                        return service.broker.revoke(parts[1])
                    if len(parts) == 3 and parts[0] == "hosts" and parts[2] == "execution-level":
                        return service.set_host_execution_level(parts[1], body)
                    if parts == ["bots"]:
                        return service.create_bot(body)
                    if parts == ["projects"]:
                        return service.create_project(body["bot_id"], body)
                    if len(parts) == 3 and parts[0] == "bots":
                        bot_id, verb = parts[1:]
                        if verb == "messages":
                            # Normal Web chat never supplies authoritative work_id.
                            # The original HTTP body remains the outer idempotency
                            # payload; the normalized body is deterministic and the
                            # server-produced routing result is not part of its digest.
                            send_body = normalized_chat_body[0] or body
                            return service.send(bot_id, send_body, key, routing=routing[0])
                        if verb == "update": return service.update_bot(bot_id, body)
                        if verb == "archive": return service.archive(bot_id)
                        if verb == "state": return service.state_patch(bot_id, body)
                        if verb == "files": return service.import_file(bot_id, body)
                        if verb == "uploads": return service.upload_file(bot_id, body)
                        if verb == "handoffs": return service.delegate(bot_id, body["work_id"], body)
                    if len(parts) == 3 and parts[0] == "tasks":
                        task_id, verb = parts[1:]
                        if verb == "approve": return service.approve(task_id, body)
                        if verb == "cancel": return service.cancel(task_id)
                        if verb == "reconcile": return service.reconcile(task_id)
                        if verb == "resolve": return service.resolve_task(task_id, body)
                        if verb == "retry": return service.retry_task(task_id)
                    if len(parts) == 3 and parts[0] == "routines":
                        routine_id, verb = parts[1:]
                        routine = service.store.get("routine", routine_id)
                        owner = routine["owner_bot_id"]
                        if verb == "run": return service.run_routine_now(owner, routine_id)
                        if verb == "pause": return service.pause_routine(owner, routine_id, False)
                        if verb == "resume": return service.pause_routine(owner, routine_id, True)
                        if verb == "approve": return service.approve_routine(owner, routine_id, expected_digest=body.get("digest"),
                                                                             authority=body.get("authority"))
                    if len(parts) == 3 and parts[0] == "notifications" and parts[2] == "read":
                        note = service.store.get("notification", parts[1])
                        return service.mark_notification(note["bot_id"], note["id"])
                    if len(parts) == 3 and parts[0] == "installations" and parts[2] == "probe":
                        return service.probe(parts[1])
                    if len(parts) == 3 and parts[0] == "hosts" and parts[2] == "browse":
                        return service.browse(parts[1], body)
                    if len(parts) == 3 and parts[0] == "installations" and parts[2] == "config":
                        return service.commit_installation_config(parts[1], installation_config[0])
                    if len(parts) == 3 and parts[0] == "artifacts" and parts[2] == "copy":
                        return service.transfer(parts[1], body)
                    if len(parts) == 3 and parts[0] == "turns" and parts[2] == "cancel":
                        return self.public_turn(service.cancel_turn(parts[1]))
                    # Existing work routes use the singular "work" segment (see the
                    # GET kind map); keep cancel consistent with that convention.
                    if len(parts) == 3 and parts[0] in ("work", "works") and parts[2] == "cancel":
                        return service.cancel_work(parts[1])
                    if len(parts) == 3 and parts[0] == "turns" and parts[2] == "retry":
                        turn = service.store.get("turn", parts[1])
                        if turn["status"] != "blocked": raise Fault("turn_not_blocked")
                        if turn.get("error") == "turn_budget": turn["rounds"] = 0
                        turn.update(status="queued", error=None, cancel_requested=False)
                        return self.public_turn(service.store.put("turn", turn))
                    if len(parts) == 3 and parts[0] == "claims" and parts[2] == "resolve":
                        from .runner import command
                        claim = service.store.get("claim", parts[1])
                        evidence = str(body.get("evidence", "")).strip()
                        if set(body) != {"confirmed_stopped", "evidence"} or claim["status"] != "unknown" or body.get("confirmed_stopped") is not True or len(evidence) < 10:
                            raise Fault("operator_evidence_required")
                        audit = service.store.put("operator_action", {"id": uid("op"), "action": "resolve_resource_claim",
                            "claim_id": claim["id"], "resource_key": claim["key"], "status": "requested",
                            "evidence": evidence, "created": time.time()})
                        resource = service.execution.resources[claim["key"]]
                        try:
                            if command(resource["release_argv"]):
                                raise Fault("resource_release_failed")
                        except Exception:
                            audit.update(status="failed", finished=time.time())
                            service.store.put("operator_action", audit)
                            raise
                        audit.update(status="succeeded", finished=time.time())
                        service.store.put("operator_action", audit)
                        claim.update(status="released", operator_evidence=evidence, resolved_at=time.time(), operator_action_id=audit["id"])
                        return service.store.put("claim", claim)
                    raise Fault("not_found", 404)
                external = len(parts) == 3 and (parts[2] in {"probe", "files", "uploads", "browse", "copy", "cancel",
                                                             "reconcile", "resolve"})
                if len(parts) == 3 and parts[0] == "installations" and parts[2] == "config":
                    committed = service.store.once_lookup("http:" + path, key, body)
                    if committed is not None:
                        self.respond(committed); return
                    installation_config[0] = service.prepare_installation_config(parts[1], body)
                # Work routing is a pre-commit semantic proposal and therefore MUST
                # happen outside Store.once (network I/O never holds the DB lock).
                # Committed reality wins before any fresh probabilistic judgement.
                if (len(parts) == 3 and parts[0] == "bots" and parts[2] == "messages"
                        and isinstance(body, dict)):
                    committed = service.store.once_lookup("http:" + path, key, body)
                    if committed is not None:
                        self.respond(committed); return
                    normalized = dict(body)
                    # Backward-compatible old browser payloads are demoted from
                    # authoritative work_id to a hint at the HTTP trust boundary.
                    # Only the one-shot continue_work_id (explicit UI action) is
                    # authoritative; resolve_continuation validates it strictly.
                    legacy_work = normalized.pop("work_id", None)
                    if legacy_work and not normalized.get("work_hint_id"):
                        normalized["work_hint_id"] = legacy_work
                    normalized_chat_body[0] = normalized
                    # If service.send committed but the outer HTTP journal did not
                    # (process crash in between), replay it before paying another Jev RTT.
                    inner = service.store.once_lookup("send:" + parts[1], key, normalized)
                    if inner is not None:
                        self.respond(service.store.journal_once("http:" + path, key, body, inner)); return
                    routing[0] = service.resolve_continuation(parts[1], normalized)
                self.respond(action() if external else service.store.once("http:" + path, key, body, action))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                self.error(error)

        def error(self, error):
            code = error.code if isinstance(error, Fault) else "invalid_request" if isinstance(error, (KeyError, ValueError, TypeError)) else "internal_error"
            status = error.status if isinstance(error, Fault) else 400 if code == "invalid_request" else 500
            path = urlsplit(self.path).path
            if status >= 500:
                log.exception("request failed %s code=%s", path, code)
            else:
                log.info("request rejected %s code=%s status=%s", path, code, status)
            self.respond({"code": code, "message": code, "retryable": False}, status)

    server = ThreadingHTTPServer((host, port), Handler)
    if port == 0:
        origin = f"http://{host}:{server.server_port}"
    server.daemon_threads = True
    return server
