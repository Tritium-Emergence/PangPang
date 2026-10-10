"""Deterministic attention projection.

A long-term butler must not require a normal user to read task_id, dependency
JSON, route quality, receipt digests or the DB to know whether 胖胖 is working.
This module projects the *existing* durable control-plane facts into a small set
of user-facing attention states. It is a PURE, deterministic function of durable
facts — it is NOT a second state machine and it holds no state of its own.

RCB: the server decides the attention state; the client only renders it. The
projection never invents reality (e.g. it never calls a dead process "done") and
a terminal Work is never re-labelled by later signals.

Correctness of the underlying facts belongs to the modules this projection is
built on. It only consumes their final semantics:
  * — pending dependency ``target`` (what is running + which executor).
  * — ``unknown`` task quarantine + evidence-based settle; conversation
    waiting scoped to the current Work's real turn/task/exchange activity.

Lifecycle wins over activity (RCB). A *terminal* Work (delivered/failed/
cancelled/linked/archived) is a committed outcome and is NEVER re-labelled by
lingering activity — it can only be ``done_unread`` (unread result) or
``settled``. Only a *non-terminal* Work is classified by attention/activity:
  needs_attention > blocked > working > settled
This is why e.g. a cancelled Work whose device-cancel is still unconfirmed shows
``settled``/``done_unread`` (its outcome), not ``working`` — the "still stopping"
and "orphan executor to clean up" facts are surfaced separately (a progress note
and a task-level recovery card), never by overwriting the terminal state.

``scheduled`` is a Routine fact, not a Work fact, so it is reported only in the
system summary (counting armed/approved routines) rather than forced onto a Work.
"""
import time

WORK_TERMINAL = {"delivered", "failed", "cancelled", "linked", "archived"}
TURN_ACTIVE = {"queued", "running", "starting"}
TASK_ACTIVE = {"queued", "running", "starting", "cancel_requested"}
# A blocked turn that handed off a recovery task is still *working* while that
# task is in flight (auto-recovering); otherwise the Work is genuinely blocked.
RECOVERY_ACTIVE = {"queued", "running", "starting", "awaiting_approval"}
UNREAD_KINDS = ("result", "peer_message")
# `unknown` is NOT a sixth business state: it is the fail-closed projection marker
# emitted ONLY when the snapshot could not prove a Work idle because a live domain
# was truncated (see `complete` below). It matches the system's existing fail-closed
# `unknown` philosophy — absence of evidence is never rendered as "settled".
STATES = ("needs_attention", "blocked", "working", "done_unread", "settled", "unknown")


def project(work, turns, tasks, exchanges, routines, notifications, claims, *, bot_id=None, now=None, complete=True, notifications_complete=True):
    """Project durable facts into attention states.

    Returns ``(states, summary)`` where ``states`` maps every work id to exactly
    one of:data:`STATES`, and ``summary`` is a per-state count for the main
    interface. Inputs are the lists the snapshot already fetched, so this adds
    zero DB reads; work-id indices keep it O(n * delegation-depth) with depth
    bounded shallow (delegation nests at most a few levels in practice).

    Deterministic: for the same facts AND the same ``now`` the output is identical
    (``now`` is only read to decide whether a routine's ``next_run_at`` is in the
    future for the ``scheduled`` count). ``now`` defaults to wall-clock for callers
    that do not care; the snapshot passes one fixed ``now`` per request.

    ``complete`` is the caller's assertion that the fact lists are the full live
    domain (no bounded page was truncated). When it is False a Work that would
    classify as ``settled`` is instead reported ``unknown`` — RCB fail-closed: we
    cannot prove a Work idle when its own evidence might lie past a truncated cap,
    so we never render it as a settled "已就绪". States backed by evidence we DID
    see (needs_attention/blocked/working/done_unread) stay truthful.
    """
    work = list(work or [])
    turns = list(turns or [])
    tasks = list(tasks or [])
    exchanges = list(exchanges or [])
    routines = list(routines or [])
    notifications = list(notifications or [])
    claims = list(claims or [])

    tasks_by_work, turns_by_work = {}, {}
    for t in tasks:
        tasks_by_work.setdefault(t.get("work_id"), []).append(t)
    for t in turns:
        turns_by_work.setdefault(t.get("work_id"), []).append(t)
    task_by_id = {t.get("id"): t for t in tasks}
    children = {}
    for w in work:
        children.setdefault(w.get("parent_work_id"), []).append(w)

    pending_exchange = set()
    for x in exchanges:
        if x.get("status") == "pending":
            for key in ("source_work_id", "target_work_id", "work_id"):
                wid = x.get(key)
                if wid:
                    pending_exchange.add(wid)

    unread_by_work = {}
    for n in notifications:
        if not n.get("read") and n.get("kind") in UNREAD_KINDS and n.get("work_id"):
            unread_by_work.setdefault(n["work_id"], True)

    def own_signals(w):
        """States implied by this Work's OWN turns / tasks / exchanges."""
        wid = w.get("id")
        wt = tasks_by_work.get(wid, [])
        wr = turns_by_work.get(wid, [])
        signals = set()
        if any(t.get("status") == "awaiting_approval" for t in wt):
            signals.add("needs_attention")
        if any(t.get("status") == "unknown" for t in wt):
            signals.add("blocked")
        if any(t.get("status") in TASK_ACTIVE for t in wt):
            signals.add("working")
        if any(t.get("status") in TURN_ACTIVE for t in wr):
            signals.add("working")
        if wid in pending_exchange:
            signals.add("working")
        terminal = w.get("status") in WORK_TERMINAL
        for t in wr:
            if t.get("status") != "blocked" or terminal:
                continue # a terminal Work's blocked turn is stale, not blocking
            rec = t.get("recovery_dispatched")
            rtask = task_by_id.get(rec) if rec else None
            if rtask and rtask.get("status") in RECOVERY_ACTIVE:
                signals.add("working") # auto-recovering handoff still in flight
            else:
                signals.add("blocked")
        return signals

    work_by_id = {w.get("id"): w for w in work}
    live_ids = {w.get("id") for w in work if w.get("status") not in WORK_TERMINAL}

    def rolled_signals(w, seen):
        """Own signals plus those of non-terminal descendants, so a delegating
        parent reflects its child. Rollup never crosses a terminal Work (a dead
        parent cannot be re-animated; a live child under it is an effective root,
        see below). Bounded by delegation depth (shallow); zero DB reads."""
        wid = w.get("id")
        if wid in seen:
            return set()
        seen.add(wid)
        signals = set(own_signals(w))
        for child in children.get(wid, []):
            if child.get("status") not in WORK_TERMINAL:
                signals |= rolled_signals(child, seen)
        return signals

    def classify(w):
        # RCB: a terminal Work's committed outcome is never overwritten by lingering
        # activity (task/turn truncation is irrelevant to a committed outcome). The
        # ONE exception is its own read-state: if we saw an unread result it is
        # done_unread; if the notification domain was truncated and we did NOT see one,
        # we cannot prove it read, so fail-closed to unknown rather than a false settled.
        if w.get("status") in WORK_TERMINAL:
            if unread_by_work.get(w.get("id")):
                return "done_unread"
            return "settled" if notifications_complete else "unknown"
        signals = rolled_signals(w, set())
        if "needs_attention" in signals:
            return "needs_attention"
        if "blocked" in signals:
            return "blocked"
        if "working" in signals:
            return "working"
        # No positive signal seen. If the live domains were truncated this Work's
        # own evidence might lie past the cap, so we cannot prove it idle: report
        # `unknown` (fail-closed), never a false `settled`.
        return "settled" if complete else "unknown"

    states = {w.get("id"): classify(w) for w in work}

    # Summary counts "threads": one unit per effective-root Work. A Work is an
    # effective root when its parent is absent (paginated/archived/dangling) or
    # terminal — so a live child under a dead/missing parent is still counted,
    # and a live child under a live parent rolls up into it (never double-counted).
    now = time.time() if now is None else now
    def is_root(w):
        parent = w.get("parent_work_id")
        return not parent or parent not in live_ids
    counts = {s: 0 for s in STATES}
    for w in work:
        if is_root(w):
            counts[states[w.get("id")]] += 1
    pending_routines = scheduled = 0
    for r in routines:
        if r.get("approval_state") == "pending":
            pending_routines += 1
        elif r.get("active") and r.get("approval_state") == "approved" and (r.get("next_run_at") or 0) > now:
            scheduled += 1
    # done_unread counts unread result/peer *items* (the bell badge), not Works.
    done_unread = sum(1 for n in notifications
                      if not n.get("read") and n.get("kind") in UNREAD_KINDS
                      and (bot_id is None or n.get("bot_id") == bot_id))
    # blocked = stuck non-terminal threads (their own unknown tasks included) +
    # orphan unknown tasks whose Work is already terminal/absent (executor still
    # to clean up, thread otherwise done) + workless resource claims. No overlap.
    orphan_unknown = sum(1 for t in tasks if t.get("status") == "unknown"
                         and work_by_id.get(t.get("work_id"), {}).get("status", "__absent__") in WORK_TERMINAL | {"__absent__"})
    summary = {
        "needs_attention": counts["needs_attention"] + pending_routines,
        "working": counts["working"],
        "done_unread": done_unread,
        "blocked": counts["blocked"] + orphan_unknown + sum(1 for c in claims if c.get("status") == "unknown"),
        "scheduled": scheduled,
        # Root Works we could not prove idle under truncation (fail-closed). Zero
        # when complete. Lets the UI say "N 项状态未确定" honestly instead of
        # claiming the five counts above are exact or a strict lower bound.
        "indeterminate": counts["unknown"],
        "complete": bool(complete),
    }
    return states, summary
