"""Bounded work projections. Archives remain evidence, never new authority."""
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .store import Fault, encode
from .replies import history_reply_text
from .history import compacted_history, corrections_for, corrected_into, COMPACTED_WORKS
from .execution import task_interjection_tier
from .planner_prompt import PLANNER_SYSTEM_HEAD, PLANNER_SYSTEM_TAIL, WEB_SEARCH_GUIDANCE, NOTICE, detect_language, response_language_block


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _authoritative_now(service):
    """System-authoritative current time for the prompt (SIS: time is a
    deterministic fact the model must NOT guess from training memory). Rebuilt every
    turn; uses the configured default timezone (Asia/Shanghai unless overridden)."""
    tzname = str((service.config or {}).get("default_timezone", "Asia/Shanghai"))
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        tz, tzname = ZoneInfo("Asia/Shanghai"), "Asia/Shanghai"
    now = datetime.fromtimestamp(time.time(), tz)
    return {"iso": now.isoformat(timespec="seconds"), "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"), "weekday": _WEEKDAYS[now.weekday()], "timezone": tzname}


def _trigger_context(service, work, incoming, now_tz_name):
    """What kicked off this turn + the timezone that actually governs it. `now` is the
    PROCESSING moment in the default tz; a routine fires on ITS OWN timezone, so
    relative-time reasoning ('9am daily', 'in 10 min') must use that. received_at is
    when the trigger message was created (not when the queue got to it)."""
    tz_name = now_tz_name
    scheduled_for = None
    trig = {"kind": str(incoming.get("kind") or "user")}
    if work.get("kind") == "routine" and work.get("routine_id"):
        try:
            routine = service.store.get("routine", work["routine_id"])
            tz_name = str(routine.get("timezone") or tz_name)
            # THIS fire's scheduled_for is frozen on the Work at materialize time.
            # routine.next_run_at has already advanced to the NEXT fire, so it is only
            # a last-resort fallback for legacy works that predate the frozen field.
            scheduled_for = work.get("scheduled_for") or routine.get("next_run_at")
        except Exception:
            pass
    trig["effective_timezone"] = tz_name
    for key, ts in (("received_at", incoming.get("created")), ("scheduled_for", scheduled_for)):
        if ts:
            try:
                trig[key] = datetime.fromtimestamp(float(ts), ZoneInfo(tz_name)).isoformat(timespec="seconds")
            except Exception:
                pass
    return trig


# SIS red line: the model sees only business fields. Internal scheduler/kernel
# fields (pending_dep_ids, cancel_requested, grant_write, depth, parent_work_id,
# handoff, reply_to, mode, linked_to, root_request_id, run_owner_*) must never
# enter the prompt — adding one to Work would otherwise leak it. Deliberate
# exception: required_host_id / required_installation_id are read-only restriction
# facts the model must see to explain a work_routing_constraint rejection (F);
# it still cannot change them.
_WORK_FIELDS = ("id", "kind", "status", "goal", "constraints", "checkpoint", "refs", "project_id",
                "required_host_id", "required_installation_id")
_DEP_FIELDS = ("kind", "status", "outcome", "summary")
_PROJECT_FIELDS = ("id", "title", "goal")


def model_work_view(work):
    return {k: work[k] for k in _WORK_FIELDS if k in work}


def model_dependency_view(dep, *, own=False, store=None, installations=None, hosts=None):
    # `own=True` (read_work on the Bot's OWN Work) safely exposes the dependency
    # id so the model can call cancel_dependency(dep_id). Cross-Work projections
    # keep it hidden.
    view = {k: dep[k] for k in _DEP_FIELDS if k in dep}
    if own:
        view["id"] = dep["id"]
        #: a pending dependency is work in motion. Tool calls never enter the
        # cross-turn chat history, so name WHAT is running from the authoritative
        # target record; otherwise a stop turn sees only an unlabeled dep and the
        # model concludes from its own narration that nothing was dispatched.
        if store is not None and dep.get("status") == "pending":
            target = _dependency_target(store, dep, installations, hosts)
            if target:
                view["target"] = target
    return view


def _dependency_target(store, dep, installations=None, hosts=None):
    kind = dep.get("kind")
    if kind not in ("task", "child_work", "exchange"):
        return None
    try:
        target = store.get(kind if kind != "child_work" else "work", dep.get("target_id"))
    except Fault:
        return None
    if kind == "task":
        view = {"task_id": target["id"], "status": target["status"], "installation_id": target.get("installation_id"),
                "request": str(target.get("user_prompt") or "")[:300],
                #: the planner can inject only when it can see this. Computed
                # by the predicate inject_task_message uses, never stored.
                "interjection_tier": task_interjection_tier(
                    (installations or {}).get(target.get("installation_id")),
                    (hosts or {}).get(target.get("host_id")))}
        asks = []
        for item in (target.get("permission_asks") or [])[:8]:
            if isinstance(item, dict) and isinstance(item.get("request_id"), str):
                asks.append({"request_id": item["request_id"][:80],
                             "title": str(item.get("title") or "")[:200],
                             "method": str(item.get("method") or "")[:80]})
        if asks:
            view["permission_asks"] = asks
        return view
    if kind == "child_work":
        return {"work_id": target["id"], "bot_id": target.get("owner_bot_id"), "status": target.get("status"),
                "goal": str(target.get("goal") or "")[:300]}
    return {"exchange_id": target["id"], "target_bot_id": target.get("target_bot_id"), "status": target.get("status")}


def model_project_view(project):
    return {k: project[k] for k in _PROJECT_FIELDS if k in project}


def _in_flight(store, work, now):
    """Pending dependencies of a waiting Work, as kind/status/age_s only.
    Age uses task.started when the target is a task, otherwise created.
    No dependency id and no task text. Each dependency is a primary-key read;
    a missing row is skipped."""
    items = []
    for dep_id in work.get("pending_dep_ids") or []:
        try:
            dep = store.get("dependency", dep_id)
        except Fault as error:
            if error.code == "not_found":
                continue
            raise
        if dep.get("status") != "pending":
            continue
        kind = dep.get("kind")
        status = dep.get("status")
        anchor = dep.get("created")
        target_id = dep.get("target_id")
        if kind == "task" and target_id:
            try:
                task = store.get("task", target_id)
            except Fault:
                task = None
            if task:
                status = task.get("status") or status
                anchor = task.get("started") or task.get("created") or anchor
        elif kind == "child_work" and target_id:
            try:
                child = store.get("work", target_id)
            except Fault:
                child = None
            if child and child.get("status"):
                status = child["status"]
        elif kind == "exchange" and target_id:
            try:
                exchange = store.get("exchange", target_id)
            except Fault:
                exchange = None
            if exchange and exchange.get("status"):
                status = exchange["status"]
        try:
            age = max(0, int(now - float(anchor)))
        except (TypeError, ValueError):
            age = 0
        items.append({"kind": kind, "status": status, "age_s": age})
    return items


def _frame(message, current_work_id):
    content = history_reply_text(message["content"]) if message["kind"] == "assistant" else message["content"]
    frame = {"source": message["from"], "kind": message["kind"], "content": content,
             "evidence_ref": message["id"], "work_id": message.get("work_id"),
             "artifact_refs": list(message.get("artifact_refs") or []),
             "continuity_only": message.get("work_id") != current_work_id}
    if message.get("authority") == "delegator":
        frame["authority"] = "delegator"
    return frame


# Artifact fields safe to show the model: enough to identify/route a file, never the
# absolute path or full digest. `source` is the server-recorded provenance
# (user_import / task id / source artifact id). kind/mime don't exist in the artifact
# schema yet -> emit null rather than guess.
def _basename(path):
    return re.split(r"[\\/]", str(path or ""))[-1][:120]


def model_artifact_view(service, ref):
    """Bounded, allowlisted view of one artifact ref for the evidence manifest. The
    ref may not resolve to an artifact (it can be any evidence id) -> return a minimal
    stub so the model still knows a ref exists. content stays behind read_evidence."""
    try:
        a = service.store.get("artifact", ref)
    except Exception:
        return {"ref": ref, "resolved": False}
    return {"ref": ref, "resolved": True, "name": _basename(a.get("path")),
            "size": a.get("size"), "host_id": a.get("host_id"),
            "kind": a.get("kind"), "mime": a.get("mime"), # not in schema yet -> None
            "source": a.get("source"),
            # provenance is server-recorded; a record proves recorded metadata, not
            # that the file still exists or still matches its digest.
            "verification_status": "recorded_metadata_only"}


def _evidence_manifest(service, consumed, work):
    """Files attached to THIS turn's messages + the Work's refs, so the model never
    claims 'no attachment' or edits the wrong file (it currently sees only ids).
    Bounded to 5 entries; content is never inlined (read_evidence for that)."""
    refs, seen = [], set()
    for m in consumed:
        for r in (m.get("artifact_refs") or []):
            if r not in seen:
                seen.add(r); refs.append(r)
    for r in (work.get("refs") or []):
        if r not in seen:
            seen.add(r); refs.append(r)
    total = len(refs)
    manifest = [model_artifact_view(service, r) for r in refs[:5]]
    return {"items": manifest, "total": total, "truncated": total > len(manifest)} if total else None


MAX_TURN_IMAGES = 4


def _turn_images(service, message, seen):
    """Image refs the user attached to THIS turn's message, for a vision planner.
    Only a reference ({sha, mime}) is stored in the turn; the Pi bridge loads the
    kept bytes when it talks to the model, and only if the profile can see images.
    Earlier turns' images are not re-sent; read_evidence still reaches the file."""
    if message.get("from") != "user" or message.get("kind") != "user":
        return {}
    images = []
    for ref in message.get("artifact_refs") or []:
        if len(seen) >= MAX_TURN_IMAGES:
            break
        try:
            artifact = service.store.get("artifact", ref)
        except Fault:
            continue
        sha = artifact.get("model_image")
        if sha and sha not in seen:
            seen.add(sha)
            images.append({"sha": sha, "mime": artifact.get("mime"), "ref": ref})
    return {"images": images} if images else {}


def projection(service, turn):
    seen_images = set()
    bot = service.bot(turn["bot_id"])
    state = service.store.get("state", bot["id"])
    work = service.store.get("work", turn["work_id"])
    # a turn coalesces an ordered set of inbox messages. The trigger
    # (last by causal sequence) is the "current evidence"; all consumed messages
    # are injected as ordered frames so the model sees the full join once.
    msg_ids = turn.get("message_ids") or ([turn["message_id"]] if turn.get("message_id") else [])
    consumed = []
    for mid in msg_ids:
        try:
            consumed.append(service.store.get("message", mid))
        except Exception:
            continue
    consumed.sort(key=lambda m: (m.get("sequence", 0), m.get("created", 0)))
    if not consumed:
        consumed = [service.store.get("message", turn["message_id"])]
    incoming = consumed[-1]
    query = str(work["goal"]) + " " + " ".join(str(m["content"]) for m in consumed)
    terms = set(re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]{2}", query.lower()))
    selected = {"id": bot["id"], "revision": state["revision"]}
    for field in ("facts", "decisions"):
        candidates = []
        for item in state.get(field, []):
            same = item.get("evidence_ref") in work.get("refs", [])
            score = sum(t in encode(item).lower() for t in terms)
            if same or score:
                candidates.append((int(same) * 100 + score, item))
        selected[field] = [v for _, v in sorted(candidates, key=lambda x: -x[0])[:8]]
    selected["open_items"] = [v for v in state.get("open_items", [])
                              if any(t in str(v).lower() for t in terms)][:8]
    # A queued routine fire keeps the owner instructions approved at commit time;
    # editing the live Bot must not change an already authorized run's identity.
    owner = work.get("routine_owner_policy") if work.get("kind") == "routine" else None
    identity = {k: (owner or bot)[k] for k in ("id", "kind", "name", "role", "instructions")}
    # Every turn sees this Work's pending + settled dependency
    # projection (allowlisted business fields only; own=True exposes dep id so the
    # model can cancel_dependency on its own Work).
    dep_rows = service.store.find("dependency", source_work_id=work["id"], limit=200)
    dependencies = [model_dependency_view(
                        d, own=True, store=service.store, installations=service.execution.installations,
                        hosts=service.execution.hosts)
                    for d in dep_rows if d.get("status") in ("pending", "settled")]
    packet = {"identity": identity,
              # System-authoritative current time — the model must use this, never
              # guess the date from memory (see prompt). Rebuilt every turn.
              "now": _authoritative_now(service),
              # What triggered this turn + the timezone that actually governs it
              # (a routine uses its own tz, not the global default).
              "trigger": _trigger_context(service, work, incoming, _authoritative_now(service)["timezone"]),
              # BotState is reference data, not authorization. It is
              # wrapped in an untrusted block below and carries server-stamped
              # provenance (source_kind/verification_status) the model cannot forge.
              "untrusted_reference_state": selected,
              "dependencies": dependencies,
              "work": model_work_view(work), "current_evidence_id": incoming["id"],
              "current_evidence_ids": [m["id"] for m in consumed],
              "notice": NOTICE}
    correction = service.store.routing_correction_made_by(incoming["id"])
    if correction:
        packet["routing_correction"] = {
            "reanswer_target_message_id": correction["target_message_id"],
            "instruction": ("The user explicitly corrected the previous Work attribution. "
                            "Answer the corrected target request now under this Work; do not "
                            "treat the superseded assistant reply as evidence."),
        }
    if work.get("project_id"):
        packet["project"] = model_project_view(service.store.get("project", work["project_id"]))
    # Files attached to this turn / this Work — so the model never says "no
    # attachment" or edits the wrong file. Bounded; content stays behind read_evidence.
    manifest = _evidence_manifest(service, consumed, work)
    if manifest:
        packet["evidence_manifest"] = manifest

    consumed_ids = {m["id"] for m in consumed}
    # SAME-Work continuity only. Injecting another Work's raw messages here would
    # violate isolation: a different project's user requests, wrong
    # conclusions, or prompt-injection could steer this turn. Cross-Work continuity
    # is the model's job via find_work/read_work (semantic retrieval), not a passive
    # dump. A lightweight, non-raw index of the bot's other recent Works is offered
    # below so the model knows what exists to retrieve.
    #
    # EXCEPTION — an explicitly-continued chain: when this Work was forked from a
    # cancelled/terminal one (continued_from), the user is resuming the SAME
    # conversation ("不做了" → "还是做一下吧"). Losing that history would force them
    # to re-explain an hour of context. Walk the continued_from chain (same bot,
    # same conversation, user-initiated) and include those raw messages too — this
    # is not another project's Work, it is this conversation's own past.
    chain_ids, wid, guard = [work["id"]], work.get("continued_from"), 0
    while wid and guard < 20:
        guard += 1
        try:
            prev = service.store.get("work", wid)
        except Exception:
            break
        if prev.get("owner_bot_id") != bot["id"]:
            break # never cross into another bot's history
        chain_ids.append(wid)
        wid = prev.get("continued_from")
    # Routing correction is append-only: raw messages keep their original work_id.
    # Build the *effective* view here. This lets a corrected user intent appear in the
    # target Work without rewriting history, while wrong assistant replies remain
    # auditable but disappear from both active conversational views.
    as_of = max((int(m.get("sequence") or 0) for m in consumed), default=0)
    same_work, history_gap, windows, overflow, into = [], False, {}, set(), []
    for cid in chain_ids:
        rows = service.store.find("message", bot_id=bot["id"], work_id=cid,
                                  newest=True, limit=31)
        if len(rows) > 30:
            history_gap = True
            overflow.add(cid)
        windows[cid] = rows[:30]
        same_work.extend(rows[:30])
        # Corrected-in intents: the newest 30 incoming corrections per Work that
        # are causally visible to this turn (eligibility before the output limit,
        # under a fixed examination budget; an exhausted budget or an ignored
        # unknown-causality correction marks the view incomplete).
        incoming_corrections, _, more, unknown = corrected_into(service.store, cid, as_of=as_of, limit=30)
        history_gap = history_gap or more or unknown
        into += [c["target_message_id"] for _, c, _, _ in incoming_corrections]
    # One definition shared with search_history (history.corrections_for), read
    # only for this bounded window and frozen to this turn's causal high-water.
    eff = corrections_for(service.store, [m["id"] for m in same_work] + into, as_of=as_of)
    by_target, superseded_ids = eff.by_target, eff.superseded
    # Unprovable effective truth fails closed: hidden and marked, never raw.
    history_gap = history_gap or eff.truncated or eff.unknown or bool(eff.uncertain)

    # Remove raw messages whose latest effective route points elsewhere.
    filtered, hidden_ids = [], set(superseded_ids) | eff.uncertain
    for message in same_work:
        correction = by_target.get(message["id"])
        if message["id"] in hidden_ids:
            continue
        if correction and correction.get("new_work_id") not in chain_ids:
            hidden_ids.add(message["id"])
            continue
        filtered.append(message)
    same_work = filtered

    # Add corrected-in user intents whose raw row lives in another Work.
    existing_ids = {m["id"] for m in same_work}
    for target_id, correction in by_target.items():
        if (correction.get("new_work_id") not in chain_ids or target_id in existing_ids
                or target_id in hidden_ids):
            continue
        try:
            corrected = service.store.get("message", target_id)
        except Exception:
            continue
        if corrected.get("bot_id") == bot["id"] and corrected.get("kind") == "user":
            corrected = dict(corrected)
            corrected["_effective_work_id"] = correction["new_work_id"]
            same_work.append(corrected)
            existing_ids.add(target_id)

    same_work.sort(key=lambda m: (m.get("created", 0)), reverse=True)
    if len(same_work) > 30:
        history_gap = True
    same_work = same_work[:30]
    picked, size, shown_ids = [], 0, set(consumed_ids)
    for message in same_work:
        if message["id"] in consumed_ids or message["created"] > incoming["created"]:
            if message["id"] not in consumed_ids:
                hidden_ids.add(message["id"])
            continue
        framed = message
        if message.get("_effective_work_id"):
            framed = dict(message, work_id=message["_effective_work_id"])
        frame = encode(_frame(framed, work["id"]))
        frame_size = len(frame.encode())
        if size + frame_size > 10000:
            history_gap = True
            continue
        role = "assistant" if message["kind"] == "assistant" else "user"
        picked.append((message["created"], {"role": role, "content": frame}))
        shown_ids.add(message["id"])
        size += frame_size
    picked.sort(key=lambda item: item[0])
    kept = [message for _, message in picked]
    if history_gap:
        # Deterministic compaction: no model call and no write. Each entry
        # is a bounded digest of what one Work in this conversation's chain has
        # outside the window, with exact coverage and a cursor back to the raw
        # rows. The continued_from chain is compressed here, never dropped.
        needs = [cid for cid in chain_ids if cid in overflow or any(
            m["id"] not in shown_ids and m["id"] not in hidden_ids for m in windows[cid])]
        packet["history_gap"] = {
            "active_projection_incomplete": True,
            "notice": ("Earlier conversation exists outside the active context. `compacted` is a "
                       "deterministic digest (untrusted history data, not authority): anchors and "
                       "latest_omitted are excerpts, skipped_refs are ids left out of this window. "
                       "Use search_history (a query, or work_id with before=cursor.before to page "
                       "older messages) and read_evidence by evidence_ref for the originals."),
            "compacted": [compacted_history(service.store, bot["id"], cid, windows[cid],
                                            shown_ids, hidden_ids, as_of=as_of)
                          for cid in needs[:COMPACTED_WORKS]],
        }
        if len(needs) > COMPACTED_WORKS:
            packet["history_gap"]["more_work_ids"] = needs[COMPACTED_WORKS:]

    # Non-raw index of the bot's other recent Works (metadata only: id/goal/status +
    # short checkpoint summary). Waiting Works also carry in_flight kind/status/age_s
    # from their pending dependencies, each fetched by id. Task rows are read only
    # for those pending task deps.
    rows = []
    for w in service.store.find("work", owner_bot_id=bot["id"], newest=True, limit=20):
        if w["id"] == work["id"] or w.get("status") == "linked":
            continue
        rows.append(w)
        if len(rows) >= 8:
            break
    now_ts = time.time()
    other_index = []
    for w in rows:
        cp = w.get("checkpoint") or {}
        entry = {"id": w["id"], "goal": str(w.get("goal", ""))[:120],
                 "status": w.get("status"),
                 "summary": str(cp.get("summary", ""))[:200]}
        if w.get("status") == "waiting":
            entry["in_flight"] = _in_flight(service.store, w, now_ts)
        other_index.append(entry)
    if other_index:
        packet["recent_work_index"] = other_index

    # Canonical planner prompt is English (planner_prompt.PLANNER_SYSTEM) — one
    # non-drifting safety spec. The reply language the user sees is controlled by
    # the response-language block (presentation only). Detect from the latest real
    # user message in this turn, not `incoming`: on an execution_result / peer /
    # routine wake, incoming is machine text (mostly English) and would mislead a
    # Chinese user's reply language. Fall back to incoming when no user line exists.
    user_msg = next((m for m in reversed(consumed) if m.get("kind") == "user" or m.get("from") == "user"), incoming)
    lang = detect_language(user_msg.get("content"))
    prompt = PLANNER_SYSTEM_HEAD
    if service.capabilities()["web_search"]:
        prompt += WEB_SEARCH_GUIDANCE
    prompt += PLANNER_SYSTEM_TAIL + response_language_block(lang)
    return [{"role": "system", "content": prompt + encode(packet)}, *kept,
            *[dict({"role": "user", "content": encode(dict(
                {"source": m["from"], "kind": m["kind"], "content": m["content"]},
                **({"authority": "delegator"} if m.get("authority") == "delegator" else {})))},
                   **_turn_images(service, m, seen_images))
              for m in consumed]]
