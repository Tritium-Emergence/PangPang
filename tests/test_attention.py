# -*- coding: utf-8 -*-
"""Unit tests for the attention projection (pangpang/attention.py).

The projection is a pure, deterministic function of durable facts. These tests
pin the contract: the five user-facing states, the priority order, the RCB
invariant that a terminal Work is never re-labelled by lingering activity, and
the summary counts. No Service / Store / HTTP is involved.
"""
import time
import unittest

from pangpang.attention import project


def W(wid, status="active", parent=None, bot="bot-main"):
    return {"id": wid, "status": status, "parent_work_id": parent, "owner_bot_id": bot}


def TASK(wid, status, tid=None):
    return {"id": tid or ("task-" + wid + "-" + status), "work_id": wid, "status": status,
            "owner_bot_id": "bot-main", "installation_id": "inst", "host_id": "host"}


def TURN(wid, status, **extra):
    return {"id": extra.pop("id", "turn-" + wid + "-" + status), "work_id": wid,
            "bot_id": "bot-main", "status": status, **extra}


def run(work=(), turns=(), tasks=(), exchanges=(), routines=(), notifications=(), claims=(), bot_id=None, complete=True, notifications_complete=True):
    return project(list(work), list(turns), list(tasks), list(exchanges),
                   list(routines), list(notifications), list(claims), bot_id=bot_id,
                   complete=complete, notifications_complete=notifications_complete)


class PerWorkState(unittest.TestCase):
    def test_working_from_active_turn_or_task(self):
        for turn_status in ("queued", "running", "starting"):
            states, _ = run(work=[W("w")], turns=[TURN("w", turn_status)])
            self.assertEqual(states["w"], "working", turn_status)
        for task_status in ("queued", "running", "starting", "cancel_requested"):
            states, _ = run(work=[W("w")], tasks=[TASK("w", task_status)])
            self.assertEqual(states["w"], "working", task_status)

    def test_pending_exchange_is_working(self):
        states, _ = run(work=[W("w")], exchanges=[{"status": "pending", "source_work_id": "w"}])
        self.assertEqual(states["w"], "working")

    def test_needs_attention_from_awaiting_approval(self):
        states, _ = run(work=[W("w")], tasks=[TASK("w", "awaiting_approval")])
        self.assertEqual(states["w"], "needs_attention")

    def test_unknown_task_is_blocked_never_working(self):
        # Even with a concurrently-running task, the unknown quarantine dominates
        # over working (blocked > working), and unknown never maps to done/working.
        states, _ = run(work=[W("w", "waiting")],
                        tasks=[TASK("w", "unknown"), TASK("w", "running")])
        self.assertEqual(states["w"], "blocked")

    def test_blocked_turn_not_recovering_is_blocked(self):
        for rec in (None, False):
            states, _ = run(work=[W("w")], turns=[TURN("w", "blocked", recovery_dispatched=rec)])
            self.assertEqual(states["w"], "blocked", rec)

    def test_blocked_turn_recovery_in_flight_is_working(self):
        states, _ = run(work=[W("w")],
                        turns=[TURN("w", "blocked", recovery_dispatched="task-r")],
                        tasks=[{"id": "task-r", "work_id": "w", "status": "running"}])
        self.assertEqual(states["w"], "working")

    def test_blocked_turn_with_missing_recovery_task_is_blocked(self):
        # recovery_dispatched points at a task not present (paginated/absent):
        # fail safe to blocked, never silently settled.
        states, _ = run(work=[W("w")], turns=[TURN("w", "blocked", recovery_dispatched="task-gone")])
        self.assertEqual(states["w"], "blocked")

    def test_priority_order(self):
        # needs_attention > blocked > working within one Work.
        states, _ = run(work=[W("w", "waiting")],
                        tasks=[TASK("w", "awaiting_approval"), TASK("w", "unknown"), TASK("w", "running")])
        self.assertEqual(states["w"], "needs_attention")
        states, _ = run(work=[W("w", "waiting")], tasks=[TASK("w", "unknown"), TASK("w", "running")])
        self.assertEqual(states["w"], "blocked")

    def test_idle_non_terminal_is_settled(self):
        states, _ = run(work=[W("w", "waiting")])
        self.assertEqual(states["w"], "settled")


class RcbTerminalInvariant(unittest.TestCase):
    """A terminal Work's committed outcome is never overwritten by activity."""

    def test_cancelled_work_with_lingering_cancel_requested_task_is_not_working(self):
        states, _ = run(work=[W("w", "cancelled")], tasks=[TASK("w", "cancel_requested")])
        self.assertEqual(states["w"], "settled")

    def test_terminal_work_with_awaiting_approval_is_not_needs_attention(self):
        states, _ = run(work=[W("w", "delivered")], tasks=[TASK("w", "awaiting_approval")])
        self.assertEqual(states["w"], "settled")

    def test_terminal_work_with_unknown_task_is_not_blocked_at_work_level(self):
        states, _ = run(work=[W("w", "cancelled")], tasks=[TASK("w", "unknown")])
        self.assertEqual(states["w"], "settled")

    def test_terminal_work_with_unread_result_is_done_unread(self):
        states, _ = run(work=[W("w", "delivered")],
                        notifications=[{"id": "n", "bot_id": "bot-main", "kind": "result",
                                        "work_id": "w", "read": False}])
        self.assertEqual(states["w"], "done_unread")

    def test_terminal_work_with_read_result_is_settled(self):
        states, _ = run(work=[W("w", "delivered")],
                        notifications=[{"id": "n", "bot_id": "bot-main", "kind": "result",
                                        "work_id": "w", "read": True}])
        self.assertEqual(states["w"], "settled")

    def test_all_terminal_statuses_short_circuit(self):
        for term in ("delivered", "failed", "cancelled", "linked", "archived"):
            states, _ = run(work=[W("w", term)], turns=[TURN("w", "running")],
                            tasks=[TASK("w", "awaiting_approval")])
            self.assertEqual(states["w"], "settled", term)


class DescendantRollup(unittest.TestCase):
    def test_parent_reflects_live_child_activity(self):
        states, _ = run(work=[W("p", "waiting"), W("c", "active", parent="p", bot="peer")],
                        tasks=[TASK("c", "running")])
        self.assertEqual(states["p"], "working")

    def test_parent_reflects_child_needs_attention(self):
        states, _ = run(work=[W("p", "waiting"), W("c", "active", parent="p", bot="peer")],
                        tasks=[TASK("c", "awaiting_approval")])
        self.assertEqual(states["p"], "needs_attention")

    def test_terminal_child_does_not_roll_up(self):
        states, _ = run(work=[W("p", "waiting"), W("c", "cancelled", parent="p")],
                        tasks=[TASK("c", "cancel_requested")])
        self.assertEqual(states["p"], "settled")


class SummaryCounts(unittest.TestCase):
    def test_threads_counted_once_parent_child_not_double(self):
        _, s = run(work=[W("p", "waiting"), W("c", "active", parent="p", bot="peer")],
                   tasks=[TASK("c", "running")])
        self.assertEqual(s["working"], 1) # one thread, not two

    def test_live_child_under_terminal_parent_is_its_own_root(self):
        # Dead parent (settled) + its still-live spun-off child (working) = both visible.
        _, s = run(work=[W("p", "delivered"), W("c", "active", parent="p", bot="peer")],
                   tasks=[TASK("c", "running")])
        self.assertEqual(s["working"], 1)

    def test_child_with_absent_parent_is_effective_root(self):
        _, s = run(work=[W("c", "active", parent="p-missing")], tasks=[TASK("c", "running")])
        self.assertEqual(s["working"], 1)

    def test_routines_contribute_scheduled_and_needs_attention(self):
        future = time.time() + 3600
        routines = [
            {"id": "r1", "approval_state": "pending", "active": False},
            {"id": "r2", "approval_state": "approved", "active": True, "next_run_at": future},
        ]
        _, s = run(routines=routines)
        self.assertEqual(s["needs_attention"], 1)
        self.assertEqual(s["scheduled"], 1)

    def test_past_due_or_inactive_routine_not_scheduled(self):
        routines = [
            {"id": "r1", "approval_state": "approved", "active": True, "next_run_at": time.time() - 10},
            {"id": "r2", "approval_state": "approved", "active": False, "next_run_at": time.time() + 10},
        ]
        _, s = run(routines=routines)
        self.assertEqual(s["scheduled"], 0)

    def test_blocked_counts_threads_plus_orphan_unknown_plus_claims(self):
        work = [W("w", "waiting"), W("dead", "cancelled")]
        tasks = [TASK("w", "unknown"), TASK("dead", "unknown")] # one live-thread, one orphan
        claims = [{"id": "c", "key": "k", "status": "unknown"}]
        _, s = run(work=work, tasks=tasks, claims=claims)
        # 1 blocked thread (w) + 1 orphan unknown (dead) + 1 claim
        self.assertEqual(s["blocked"], 3)

    def test_done_unread_counts_unread_items_bot_scoped(self):
        notes = [
            {"id": "n1", "bot_id": "bot-main", "kind": "result", "work_id": "w", "read": False},
            {"id": "n2", "bot_id": "bot-main", "kind": "result", "work_id": "w", "read": True},
            {"id": "n3", "bot_id": "bot-other", "kind": "peer_message", "work_id": "x", "read": False},
        ]
        _, s = run(notifications=notes)
        self.assertEqual(s["done_unread"], 2) # global: two unread
        _, s2 = run(notifications=notes, bot_id="bot-main")
        self.assertEqual(s2["done_unread"], 1) # bot-scoped


class IncompleteFailClosed(unittest.TestCase):
    """complete=False (a live domain was truncated): a Work we cannot prove idle
    must be `unknown`, never a false `settled`. Positive evidence stays truthful."""

    def test_idle_nonterminal_becomes_unknown_when_incomplete(self):
        # Its own unknown/approval/unread fact could lie past the truncated cap, so
        # with no visible signal we must NOT render "settled" (RCB fail-closed).
        states, s = run(work=[W("w", "active")], complete=False)
        self.assertEqual(states["w"], "unknown")
        self.assertEqual(s["complete"], False)
        self.assertEqual(s["indeterminate"], 1)

    def test_visible_evidence_states_stay_truthful_when_incomplete(self):
        # Evidence we DID see is still authoritative under incompleteness.
        states, _ = run(work=[W("w", "active")], tasks=[TASK("w", "running")], complete=False)
        self.assertEqual(states["w"], "working")
        states, _ = run(work=[W("w", "waiting")], tasks=[TASK("w", "unknown")], complete=False)
        self.assertEqual(states["w"], "blocked")
        states, _ = run(work=[W("w", "active")], tasks=[TASK("w", "awaiting_approval")], complete=False)
        self.assertEqual(states["w"], "needs_attention")

    def test_terminal_work_never_indeterminate_even_when_incomplete(self):
        # A committed outcome is durable; truncation elsewhere cannot make it unknown.
        states, _ = run(work=[W("w", "delivered")], complete=False)
        self.assertEqual(states["w"], "settled")
        states, _ = run(work=[W("w", "delivered")],
                        notifications=[{"id": "n", "bot_id": "bot-main", "kind": "result",
                                        "work_id": "w", "read": False}], complete=False)
        self.assertEqual(states["w"], "done_unread")

    def test_complete_true_keeps_settled(self):
        states, s = run(work=[W("w", "active")], complete=True)
        self.assertEqual(states["w"], "settled")
        self.assertEqual(s["complete"], True)
        self.assertEqual(s["indeterminate"], 0)

    def test_unknown_is_a_known_state(self):
        from pangpang.attention import STATES
        self.assertIn("unknown", STATES)

    def test_terminal_unread_truncation_rule(self):
        # The three cases: unread seen -> done_unread; notif domain complete and no
        # unread seen -> settled; notif domain TRUNCATED and no unread seen -> unknown
        # (cannot prove the terminal Work was read; fail-closed, never a false settled).
        seen_unread = [{"id": "n", "bot_id": "bot-main", "kind": "result", "work_id": "w", "read": False}]
        states, _ = run(work=[W("w", "delivered")], notifications=seen_unread, notifications_complete=False)
        self.assertEqual(states["w"], "done_unread")
        states, _ = run(work=[W("w", "delivered")], notifications_complete=True)
        self.assertEqual(states["w"], "settled")
        states, _ = run(work=[W("w", "delivered")], notifications_complete=False)
        self.assertEqual(states["w"], "unknown")

    def test_task_truncation_does_not_make_terminal_work_unknown(self):
        # read-state is independent of task backlog: a complete notification domain
        # keeps a terminal Work settled even when the live task domain was truncated.
        states, _ = run(work=[W("w", "delivered")], complete=False, notifications_complete=True)
        self.assertEqual(states["w"], "settled")


class DeterminismAndStability(unittest.TestCase):
    def test_pure_and_idempotent(self):
        work = [W("a", "waiting"), W("b", "delivered"), W("c", "active", parent="a")]
        tasks = [TASK("a", "unknown"), TASK("c", "running")]
        notes = [{"id": "n", "bot_id": "bot-main", "kind": "result", "work_id": "b", "read": False}]
        first = run(work=work, tasks=tasks, notifications=notes)
        second = run(work=work, tasks=tasks, notifications=notes)
        self.assertEqual(first, second)

    def test_input_order_independent(self):
        work = [W("a", "waiting"), W("c", "active", parent="a")]
        tasks = [TASK("c", "running")]
        s1 = run(work=work, tasks=tasks)
        s2 = run(work=list(reversed(work)), tasks=tasks)
        self.assertEqual(s1[0], s2[0])
        self.assertEqual(s1[1], s2[1])

    def test_every_work_gets_exactly_one_known_state(self):
        from pangpang.attention import STATES
        states, _ = run(work=[W("a"), W("b", "delivered"), W("c", "waiting")])
        for v in states.values():
            self.assertIn(v, STATES)
        self.assertEqual(set(states), {"a", "b", "c"})

    def test_empty_inputs(self):
        states, s = run()
        self.assertEqual(states, {})
        self.assertEqual(s, {"needs_attention": 0, "working": 0, "done_unread": 0,
                             "blocked": 0, "scheduled": 0, "indeterminate": 0, "complete": True})


if __name__ == "__main__":
    unittest.main()
