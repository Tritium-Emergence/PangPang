"""Jev pre-commit semantic gate.

Pi Core proposes; Jev judges semantic readiness; Python commits reality.
This module stays above the RCB: it performs deterministic, read-only state
projection and one typed Jev decision. It never grants authority or mutates Work.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .logs import get_logger
from .env_compat import env_value, with_legacy_names
from .store import Fault, assert_no_txn_io, digest, encode


log = get_logger("semantic_gate")

GUARDED_TOOLS = frozenset({
    "link_work", "cancel_dependency", "send_bot_message", "delegate", "submit_task",
})
DISPOSITIONS = frozenset({"EXECUTE", "REVISE", "ASK_USER"})
QUESTION = {
    "type": "choice",
    "instructions": (
        "Judge whether the proposed consequential action is semantically resolved "
        "enough to commit, using only the supplied complete current-turn causal "
        "evidence, authoritative system state, and structural candidate domain. "
        "Do not grant permissions. Do not choose a target yourself when multiple "
        "materially reasonable interpretations remain. For send_bot_message, a "
        "candidate_domain_scope of source_work_existing_relations is intentionally "
        "not a global list of every peer Work; never infer uniqueness merely because "
        "an unrelated peer is absent from that scoped evidence."
),
    "criteria": {
        "EXECUTE": (
            "The proposed action is clearly supported by the complete causal evidence "
            "and supplied system state. No material alternative interpretation requires "
            "a user choice, and no missing system fact must be read before committing."
),
        "REVISE": (
            "The proposal is not ready, but missing certainty can be obtained from "
            "existing system state/evidence or additional agent reasoning without "
            "asking the user to make a choice."
),
        "ASK_USER": (
            "Two or more materially reasonable interpretations remain, or a required "
            "choice belongs to the user and cannot safely be inferred. Do not guess."
),
    },
}


class GateUnavailable(Exception):
    pass


# Work-continuity router. “Same Work” is NOT “same topic”: the question is whether
# reusing an existing Work's state materially helps understand/complete the user's
# current intent. Jev proposes; Python validates candidates + hysteresis before commit.
CONTINUATION_CHOICES = frozenset({"CONTINUE_CURRENT", "CONTINUE_OTHER", "NEW_TOPIC"})
CONTINUATION_QUESTION = {
    "type": "choice",
    "instructions": (
        "Route this user turn into a persistent Work context. Choose CURRENT only "
        "when the current Work's existing state materially helps understand or complete "
        "the new intent, including pronoun/ellipsis follow-ups and requests derived from "
        "the current work (for example: turn an analysis into an article). Choose one "
        "CANDIDATE_n only when that exact supplied Work is clearly the better existing "
        "context. Choose NEW when the request stands alone and reusing current state has "
        "little value or would introduce interference. Do not equate lexical/topic "
        "similarity with Work continuity."
),
    "criteria": {
        "CONTINUE_CURRENT": "Reuse the supplied current Work context.",
        "CONTINUE_OTHER": "Reuse one supplied candidate Work (candidate_work_id must exactly match it).",
        "NEW_TOPIC": "Create a new independent Work context.",
    },
}
CORRECTION_CHOICES = frozenset({"CORRECTION", "NOT_CORRECTION"})
CORRECTION_QUESTION = {
    "type": "choice",
    "instructions": (
        "Decide whether the new user message explicitly corrects, retracts, or redirects "
        "the supplied previous_user_message (examples: '不是，我说的是阿里', '刚才那句是说A不是B'). "
        "Ordinary continuation, elaboration, disagreement with content, or a fresh topic "
        "is NOT_CORRECTION. This only identifies intent; application code chooses the "
        "only eligible previous turn and commits any correction."
),
    "criteria": {
        "CORRECTION": "The user explicitly says the immediately prior eligible intent was assigned/interpreted wrongly.",
        "NOT_CORRECTION": "No explicit correction of that prior intent.",
    },
}


@dataclass(frozen=True)
class GateSnapshot:
    state: dict | None
    read_set_digest: str | None
    state_bytes: int
    candidate_count: int
    eligible_total: int
    local_result: dict | None = None


def secret_env_names(config):
    """Env var names holding known secrets: the always-sensitive pair plus any
    `credential_env` referenced anywhere in config. Shared by every external-Jev
    projection path so a new entry point can't bypass existing redaction."""
    names = {"TYPESAFE_API_KEY", "PANGPANG_TOKEN"}
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "credential_env" and isinstance(item, str):
                    names.add(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(config)
    return tuple(sorted(with_legacy_names(names)))


def redact_known_secrets(env_names, value):
    """Recursively replace any known-secret VALUE (resolved from env_names via
    os.environ) with a marker. Only the external-Jev projection is redacted; the
    raw object stays intact for schema validation / dedup digest / commit."""
    secrets = sorted({os.environ.get(n, "") for n in env_names if os.environ.get(n)},
                     key=len, reverse=True)
    def red(v):
        if isinstance(v, str):
            t = v
            for s in secrets:
                t = t.replace(s, "[REDACTED_KNOWN_SECRET]")
            return t
        if isinstance(v, list):
            return [red(x) for x in v]
        if isinstance(v, dict):
            return {str(k): red(x) for k, x in v.items()}
        return v
    return red(value)


class CandidateSetBuilder:
    """Build the exact bounded semantic read-set from authoritative Store facts.

    No generative model is called here. A snapshot is built under one SQLite
    transaction so count+paging+deref see one consistent reality. The digest of
    the Jev-visible state is re-built inside Store.once before commit to close the
    Jev network TOCTOU window.
    """

    def __init__(self, service, max_state_bytes=65536):
        self.service = service
        self.store = service.store
        try:
            configured = int(max_state_bytes)
        except (TypeError, ValueError):
            configured = 65536
        # Keep the byte budget at or below the FINAL spec default. The provider
        # publishes token limits, not byte limits; a larger arbitrary byte cap can
        # only increase avoidable 4xx/unavailable failures without improving safety.
        self.max_state_bytes = max(8192, min(configured, 65536))
        self.secret_env_names = secret_env_names(self.service.config)

    def _redact_value(self, value):
        """Recursively redact known secrets from Jev-visible proposal data (thin
        wrapper over the shared module helper; kept for existing call sites)."""
        return redact_known_secrets(self.secret_env_names, value)

    def _redact(self, value):
        return redact_known_secrets(self.secret_env_names, str(value or ""))

    def _all(self, kind, **where):
        """Return a proved-complete raw domain despite Store.find's 1000 clamp.
        Keyset (created,id) pagination, not OFFSET — constant per-page cost and no
        skip/duplicate under concurrent inserts."""
        rows, cursor = [], None
        while True:
            page, cursor = self.store.find_page(kind, cursor=cursor, limit=1000, **where)
            rows.extend(page)
            if cursor is None:
                break
        total = self.store.count(kind, **where)
        return rows, len(rows) == total, total

    def _causal_frames(self, turn):
        ids = turn.get("message_ids") or ([turn["message_id"]] if turn.get("message_id") else [])
        frames = []
        for mid in ids:
            try:
                msg = self.store.get("message", mid)
            except Fault:
                return None
            frame = {
                "id": msg["id"],
                "source": msg.get("from"),
                "kind": msg.get("kind"),
                "content": self._redact(msg.get("content")),
                "sequence": msg.get("sequence", 0),
                "created": msg.get("created"),
            }
            if msg.get("authority") == "delegator":
                frame["authority"] = "delegator"
            if msg.get("wake_intent"):
                frame["wake_intent"] = msg.get("wake_intent")
            frames.append(frame)
        frames.sort(key=lambda m: (m.get("sequence", 0), m.get("created", 0), m["id"]))
        return frames

    def _bulk_list(self, values, max_items=3):
        values = list(values or [])
        items = [self._redact(item) for item in values[:max_items]]
        return {"items": items, "total": len(values), "truncated": len(values) > len(items)}

    def _project_work(self, work):
        cp = work.get("checkpoint") or {}
        out = {
            "id": work["id"],
            "owner_bot_id": work.get("owner_bot_id"),
            "goal": self._redact(work.get("goal")),
            "status": work.get("status"),
            "created": work.get("created"),
            "phase": self._redact(cp.get("phase")),
            "summary": self._redact(cp.get("summary")),
            # FINAL: bulk checkpoint arrays are explicitly
            # lossy, never silently omitted. Their omission metadata is part of
            # the semantic read-set digest.
            "completed": self._bulk_list(cp.get("completed")),
            "next_actions": self._bulk_list(cp.get("next_actions")),
            "blocked_on": self._bulk_list(cp.get("blocked_on")),
            "constraints": [self._redact(x) for x in (work.get("constraints") or [])],
            "required_host_id": work.get("required_host_id"),
            "required_installation_id": work.get("required_installation_id"),
        }
        project_id = work.get("project_id")
        if project_id:
            try:
                project = self.store.get("project", project_id)
                out["project"] = {
                    "id": project["id"],
                    "title": self._redact(project.get("title")),
                    "goal": self._redact(project.get("goal")),
                }
            except Fault:
                out["project_missing"] = True
        return out

    def _project_task(self, task):
        return {
            "id": task["id"],
            "status": task.get("status"),
            "prompt": self._redact(task.get("prompt")),
            "host_id": task.get("host_id"),
            "installation_id": task.get("installation_id"),
            "created": task.get("created"),
        }

    def _project_exchange(self, exchange):
        return {
            "id": exchange["id"],
            "status": exchange.get("status"),
            "text": self._redact(exchange.get("text")),
            "source_bot_id": exchange.get("source_bot_id"),
            "target_bot_id": exchange.get("target_bot_id"),
            "source_work_id": exchange.get("source_work_id"),
            "target_work_id": exchange.get("target_work_id"),
            "requires_response": exchange.get("requires_response"),
            "created": exchange.get("created"),
        }

    def _project_dependency(self, dep):
        out = {
            "id": dep["id"], "kind": dep.get("kind"), "status": dep.get("status"),
            "target_id": dep.get("target_id"), "created": dep.get("created"),
        }
        kind, target = dep.get("kind"), dep.get("target_id")
        try:
            if kind == "child_work":
                out["target"] = self._project_work(self.store.get("work", target))
            elif kind == "task":
                out["target"] = self._project_task(self.store.get("task", target))
            elif kind == "exchange":
                out["target"] = self._project_exchange(self.store.get("exchange", target))
        except Fault:
            out["target_missing"] = True
        return out

    def _link_candidates(self, turn):
        rows, complete, raw_total = self._all("work", owner_bot_id=turn["bot_id"])
        candidates = [
            self._project_work(w) for w in rows
            if w["id"] != turn["work_id"] and w.get("status") not in {"linked", "cancelled", "failed"}
        ]
        candidates.sort(key=lambda x: x["id"])
        return candidates, complete, len(candidates)

    def _pending_dependencies(self, work_id, kinds=None):
        rows, complete, raw_total = self._all("dependency", source_work_id=work_id)
        selected = [d for d in rows if d.get("status") == "pending" and (not kinds or d.get("kind") in kinds)]
        projected = [self._project_dependency(d) for d in selected]
        projected.sort(key=lambda x: x["id"])
        return projected, complete, len(projected)

    def _turn_referenced_peer_work_ids(self, turn):
        """Peer Work ids already established by successful tool results this turn.

        Arbitrary model-supplied ids are deliberately ignored: a mere attempted
        reference must not grant cross-Bot read visibility. Successful results are
        already durable evidence that the path established that relation.
        """
        ids = set()
        for message in turn.get("history", []):
            if message.get("role") != "tool":
                continue
            try:
                value = json.loads(message.get("content", "{}"))
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict) or value.get("error"):
                continue
            wid = value.get("target_work_id")
            if isinstance(wid, str) and wid:
                ids.add(wid)
        return ids

    def _peer_candidates(self, turn, proposed_target):
        source_work = self.store.get("work", turn["work_id"])
        ids = {proposed_target}
        child_rows, child_complete, child_total = self._all("work", parent_work_id=source_work["id"])
        ids.update(w["id"] for w in child_rows)
        exchange_rows, exchange_complete, exchange_total = self._all("exchange", source_work_id=source_work["id"])
        ids.update(x.get("target_work_id") for x in exchange_rows if x.get("target_work_id"))
        ids.update(self._turn_referenced_peer_work_ids(turn))
        candidates = []
        for wid in sorted(ids):
            try:
                w = self.store.get("work", wid)
            except Fault:
                continue
            item = self._project_work(w)
            direct_child = bool(
                w.get("parent_work_id") == source_work["id"] and w.get("reply_to") == turn["bot_id"]
)
            item["direct_child"] = direct_child
            # FINAL: relationship is semantic metadata only.
            # It must never become a second admission rule.
            item["relationship"] = "direct_child" if direct_child else "ordinary_peer"
            item["proposed"] = wid == proposed_target
            candidates.append(item)
        return candidates, bool(child_complete and exchange_complete), len(candidates)

    def _delegate_candidates(self, turn):
        rows, complete, raw_total = self._all("bot")
        candidates = [{
            "id": b["id"], "name": self._redact(b.get("name")),
            "role": self._redact(b.get("role")),
            "kind": b.get("kind"), "lifecycle": b.get("lifecycle"),
        } for b in rows if b["id"] != turn["bot_id"] and b.get("lifecycle", "active") == "active"]
        candidates.sort(key=lambda x: x["id"])
        return candidates, complete, len(candidates)

    def _effective_delegate_arguments(self, turn, body, work):
        """Project the semantic payload Service.delegate would actually inherit.

        A base handoff is not just an opaque id: its payload changes the child Work
        that will be created. Validate ownership inside this same read snapshot so
        an unrelated handoff can never be projected to the external gate.
        """
        payload = dict(body)
        base_id = body.get("base_handoff_id")
        if base_id:
            base = self.store.get("handoff", base_id)
            if (base.get("target_bot_id") != body.get("target_bot_id")
                    or base.get("source_bot_id") != turn["bot_id"]
                    or base.get("source_work_id") != work["id"]):
                raise Fault("handoff_base_mismatch")
            payload = dict(base.get("payload") or {}, **body)

        # Mirror the deterministic shape that Service.delegate will commit. Do not
        # send a Jev request for a legacy/malformed base payload that Service would
        # reject after the gate.
        for field in ("goal", "request"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip() or len(value.encode()) > 4000:
                raise Fault("invalid_text", 400)
            payload[field] = value.strip()
        for field in ("facts", "completed", "constraints", "open_questions",
                      "artifact_refs", "acceptance"):
            if not isinstance(payload.get(field, []), list):
                raise Fault("invalid_handoff", 400)
            payload.setdefault(field, [])
        if not payload["acceptance"]:
            raise Fault("acceptance_required", 400)
        payload.setdefault("mode", "return_to_sender")
        if payload["mode"] not in {"return_to_sender", "deliver_to_user"}:
            raise Fault("invalid_handoff_mode", 400)

        # Service.delegate always inherits the source Work's constraints.
        payload["constraints"] = list(dict.fromkeys(
            list(work.get("constraints") or []) + payload["constraints"]))
        return payload

    def _build_locked(self, turn, tool_name, body):
        frames = self._causal_frames(turn)
        if frames is None:
            return GateSnapshot(None, None, 0, 0, 0, {
                "semantic_gate": "ASK_USER", "semantic_gate_barrier": True,
                "reason_class": "causal_evidence_incomplete",
            })
        work = self.store.get("work", turn["work_id"])
        candidates, complete, eligible_total = [], True, 0
        target = None
        extra = {}
        proposed_arguments = body

        if tool_name == "link_work":
            candidates, complete, eligible_total = self._link_candidates(turn)
            target = next((x for x in candidates if x["id"] == body.get("work_id")), None)
        elif tool_name == "cancel_dependency":
            candidates, complete, eligible_total = self._pending_dependencies(work["id"])
            target = next((x for x in candidates if x["id"] == body.get("dep_id")), None)
        elif tool_name == "send_bot_message" and isinstance(body.get("dep_id"), str) and body.get("dep_id").strip():
            candidates, complete, eligible_total = self._pending_dependencies(
                work["id"], kinds={"task"})
            target = next((x for x in candidates if x["id"] == body.get("dep_id")), None)
        elif tool_name == "send_bot_message":
            target_id = body.get("target_work_id")
            # Repeat the narrow admission identity check INSIDE the same snapshot
            # that will be sent to Jev. A preflight done before this transaction is
            # not enough for privacy: a mismatched/foreign target must never have
            # its Work summary projected to the external gate.
            target_work = self.store.get("work", target_id)
            target_bot_id = body.get("target_bot_id")
            if (target_work.get("owner_bot_id") != target_bot_id
                    or target_bot_id == turn["bot_id"]):
                raise Fault("invalid_peer_exchange", 400)
            candidates, complete, eligible_total = self._peer_candidates(turn, target_id)
            target = next((x for x in candidates if x["id"] == target_id), None)
            extra["candidate_domain_scope"] = "source_work_existing_relations"
        elif tool_name == "delegate":
            candidates, complete, eligible_total = self._delegate_candidates(turn)
            target = next((x for x in candidates if x["id"] == body.get("target_bot_id")), None)
            proposed_arguments = self._effective_delegate_arguments(turn, body, work)
            if body.get("base_handoff_id"):
                extra["base_handoff_id"] = body["base_handoff_id"]
        elif tool_name == "submit_task":
            candidates, complete, eligible_total = self._pending_dependencies(
                work["id"], kinds={"task", "child_work"}
)
            extra["pending_obligations_are_discriminators"] = True
        else:
            raise Fault("semantic_gate_tool_not_supported", 400)

        if not complete:
            return GateSnapshot(None, None, 0, len(candidates), eligible_total, {
                "semantic_gate": "ASK_USER", "semantic_gate_barrier": True,
                "reason_class": "candidate_set_incomplete",
            })

        state = {
            "causal_evidence_frames": frames,
            "current_work": self._project_work(work),
            "proposed_action": {"tool": tool_name, "arguments": self._redact_value(proposed_arguments)},
            "target": target,
            "candidates": candidates,
            "candidate_domain_complete": True,
            **extra,
        }
        raw = encode(state).encode()
        if len(raw) > self.max_state_bytes:
            return GateSnapshot(None, None, len(raw), len(candidates), eligible_total, {
                "semantic_gate": "ASK_USER", "semantic_gate_barrier": True,
                "reason_class": "state_projection_over_budget",
            })
        return GateSnapshot(
            state=state, read_set_digest=digest(state), state_bytes=len(raw),
            candidate_count=len(candidates), eligible_total=eligible_total,
)

    def build(self, turn, tool_name, body):
        # BEGIN IMMEDIATE is intentional: all reads that define the semantic
        # snapshot (count, paging, deref, causal frames) must see one reality.
        with self.store.atomic():
            current = self.store.get("turn", turn["id"])
            return self._build_locked(current, tool_name, body)


class SemanticGate:
    ENDPOINT = "https://api.typesafe.ai/v1/systemone"

    def __init__(self, service):
        self.service = service
        # Operator-overridable endpoint (SECURITY.md egress inventory): self-hosted
        # or proxied deployments set PANGPANG_SEMANTIC_GATE_URL instead of patching
        # the source. An empty value keeps the default.
        self.endpoint = env_value("PANGPANG_SEMANTIC_GATE_URL") or self.ENDPOINT
        cfg = service.config.get("semantic_gate") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        self.mode = str(cfg.get("mode", "off")).lower()
        if self.mode not in {"off", "shadow", "enforce"}:
            self.mode = "off"
        # Work-continuity routing is a separate control plane from the pre-commit
        # tool gate. It defaults ON and degrades deterministically without a key,
        # so sticky-tab breaking works out of the box without forcing the
        # whole gate into enforce or making Jev a hard deploy dependency.
        self.continuation_routing = bool(cfg.get("continuation_routing", True))
        requested = cfg.get("tools")
        if isinstance(requested, list):
            requested = frozenset(x for x in requested if isinstance(x, str))
            self.tools = requested & GUARDED_TOOLS
        else:
            self.tools = GUARDED_TOOLS
        self.model = str(cfg.get("model", "jev-latest"))
        try:
            timeout = float(cfg.get("timeout_s", 2.0))
        except (TypeError, ValueError):
            timeout = 2.0
        self.timeout_s = max(0.1, min(timeout, 10.0))
        try:
            self.continuation_breaker_failures = max(1, min(int(
                cfg.get("continuation_breaker_failures", 3)), 20))
        except (TypeError, ValueError):
            self.continuation_breaker_failures = 3
        try:
            self.continuation_breaker_cooldown_s = max(1.0, min(float(
                cfg.get("continuation_breaker_cooldown_s", 30.0)), 600.0))
        except (TypeError, ValueError):
            self.continuation_breaker_cooldown_s = 30.0
        self._continuation_failures = 0
        self._continuation_open_until = 0.0
        # Known-secret env names for redacting any external-Jev projection (shared
        # helper, same set the CandidateSetBuilder uses — no bypass via new entry).
        self.secret_env_names = secret_env_names(service.config)

    def is_guarded(self, tool_name):
        return self.mode in {"shadow", "enforce"} and tool_name in self.tools

    @property
    def continuation_configured(self):
        """True when continuation routing is enabled AND a key is present. Lets the
        caller tell 'deliberately-off / no-key' (fresh Work, explicit route_source)
        apart from 'configured but Jev failed'
        (fallback, weak route)."""
        return bool(self.continuation_routing and os.environ.get("TYPESAFE_API_KEY"))

    @property
    def continuation_breaker_open(self):
        """True while the continuation circuit breaker is open (endpoint-layer
        failures reached the threshold; cooling down). Routing must treat this as
        'configured but degraded' — deterministic fresh Work — not as healthy and
        not as the weak fallback route."""
        return time.monotonic() < self._continuation_open_until

    def _call(self, state, question=None, question_key="commit_disposition", choices=DISPOSITIONS):
        key = os.environ.get("TYPESAFE_API_KEY", "")
        if not key:
            raise GateUnavailable("missing_key")
        payload = {
            "model": self.model,
            "state": encode(state),
            "questions": {question_key: question or QUESTION},
        }
        request = urllib.request.Request(
            self.endpoint, data=encode(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        assert_no_txn_io("jev_systemone")
        started = time.monotonic()
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=self.timeout_s) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise GateUnavailable("response_too_large")
            data = json.loads(raw)
            answer = data["answers"][question_key]
            choice = answer["choice"]
            if choice not in choices:
                raise GateUnavailable("invalid_choice")
            return {
                "disposition": choice,
                "probabilities": answer.get("probabilities", {}),
                "confidence": answer.get("confidence"),
                "model": data.get("model", self.model),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except urllib.error.HTTPError as error:
            raise GateUnavailable("rate_limited" if error.code == 429 else "request_failed") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise GateUnavailable("request_failed") from None

    def judge(self, state):
        return self._call(state)

    def classify_continuation(self, new_message, current, candidates, previous_user=None):
        """Low-latency Work routing proposal.

        Provider-visible routing choices are bounded slots (CURRENT / CANDIDATE_n /
        NEW), never Work IDs. The caller still re-validates the normalized candidate
        against its authoritative snapshot before commit.
        """
        if not self.continuation_routing:
            return None
        now = time.monotonic()
        if now < self._continuation_open_until:
            return None
        key = os.environ.get("TYPESAFE_API_KEY", "")
        if not key:
            return None

        candidate_state, slot_to_work, criteria = [], {}, {}
        if current:
            criteria["CURRENT"] = "Reuse the supplied current Work context."
        for index, candidate in enumerate(candidates or [], 1):
            slot = "CANDIDATE_" + str(index)
            slot_to_work[slot] = candidate.get("work_id")
            item = dict(candidate)
            item["slot"] = slot
            candidate_state.append(item)
            criteria[slot] = "Reuse this exact supplied candidate Work."
        criteria["NEW"] = "Create a new independent Work context."
        continuation_question = {
            "type": "choice",
            "instructions": CONTINUATION_QUESTION["instructions"],
            "criteria": criteria,
        }

        state = redact_known_secrets(self.secret_env_names, {
            "new_message": str(new_message)[:2000],
            "current_work": current,
            "candidate_works": candidate_state,
            "previous_user_message": previous_user,
        })
        questions = {"continuation": continuation_question}
        if previous_user:
            questions["correction"] = CORRECTION_QUESTION
        payload = {"model": self.model, "state": encode(state), "questions": questions}
        request = urllib.request.Request(
            self.endpoint, data=encode(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        assert_no_txn_io("jev_continuation")
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=self.timeout_s) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return None
            answers = json.loads(raw)["answers"]
            answer = answers["continuation"]
            raw_choice = answer.get("choice")
            if raw_choice == "CURRENT" and current:
                choice, candidate_work_id = "CONTINUE_CURRENT", None
            elif raw_choice == "NEW":
                choice, candidate_work_id = "NEW_TOPIC", None
            elif raw_choice in slot_to_work and slot_to_work[raw_choice]:
                choice, candidate_work_id = "CONTINUE_OTHER", slot_to_work[raw_choice]
            else:
                return None
            result = {"choice": choice, "candidate_slot": raw_choice,
                      "candidate_work_id": candidate_work_id,
                      "confidence": answer.get("confidence"),
                      "probabilities": answer.get("probabilities", {})}
            correction = answers.get("correction")
            if correction and correction.get("choice") in CORRECTION_CHOICES:
                result.update(
                    correction=correction.get("choice") == "CORRECTION",
                    correction_confidence=correction.get("confidence"),
                    correction_probabilities=correction.get("probabilities", {}))
            self._continuation_failures = 0
            self._continuation_open_until = 0.0
            return result
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            self._continuation_failures += 1
            if self._continuation_failures >= self.continuation_breaker_failures:
                self._continuation_open_until = (
                    time.monotonic() + self.continuation_breaker_cooldown_s)
                self._continuation_failures = 0
            return None
        except (ValueError, KeyError, TypeError):
            return None

    def shadow(self, state, *, turn_id=None, work_id=None, tool_name=None, metrics=None):
        if self.mode != "shadow":
            return
        metrics = dict(metrics or {})
        def run():
            decision, gate_error = None, None
            try:
                decision = self._call(state)
            except GateUnavailable as error:
                gate_error = str(error)
            log.info(
                "semantic gate turn_id=%s work_id=%s tool_name=%s gate_model=%s "
                "latency_ms=%s disposition=%s probabilities=%s confidence=%s "
                "gate_mode=shadow gate_error=%s reason_class=%s eligible_total=%s "
                "candidate_count=%s candidate_set_complete=%s causal_frame_count=%s "
                "projection_complete=%s state_bytes=%s semantic_read_set_digest_before=%s "
                "semantic_read_set_stale=not_checked final_result=executed",
                turn_id, work_id, tool_name,
                (decision or {}).get("model", self.model),
                (decision or {}).get("latency_ms"),
                (decision or {}).get("disposition"),
                encode((decision or {}).get("probabilities", {})),
                (decision or {}).get("confidence"),
                gate_error, "gate_unavailable" if gate_error else None,
                metrics.get("eligible_total"), metrics.get("candidate_count"),
                metrics.get("candidate_set_complete"), metrics.get("causal_frame_count"),
                metrics.get("projection_complete"), metrics.get("state_bytes"),
                metrics.get("read_set_digest"),
)
        threading.Thread(target=run, name="jev-shadow", daemon=True).start()
