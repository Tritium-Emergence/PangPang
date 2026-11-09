"""Repair #78 P0-2: one resend after a Codex steer is explicitly rejected.

The mailbox inject is still `turn/steer` on the running task. A matching
JSON-RPC `error` response now retires that steer's association and releases one
bounded resend chance for the same UUID in this session. The error text is
never read, and a success result (or a missing ACK) is not delivery. A second
explicit reject leaves `written` in place and the receipt stays
`delivered=false`; the run still exits 0 on a completed native turn.

The fold tests drive `apply_codex_event` directly. The session tests drive the
real `codex_app_server_session -> _codex_rpc -> apply_codex_event` path over an
in-memory stdin stub. The supervise tests run a local app-server stub through
the real runner and the existing prepare/inspect path. No real Codex, Claude,
Jev, or external service is used.
"""
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang import runner
from pangpang.service import Service


THREAD = "thr_from_server"
TURN = "turn_from_server"
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
UUID_C = "33333333-3333-4333-8333-333333333333"


def _session_state(*, thread=THREAD, turn=TURN):
    state = {"result_text": None, "confirmed": set()}
    state["codex_written"] = set()
    state["codex_steer_pending"] = {}
    state["codex_steer_retry_used"] = set()
    if thread:
        runner.apply_codex_event(state, {"id": 1, "result": {
            "thread": {"id": thread, "forkedFromId": None}}})
    if turn:
        runner.apply_codex_event(state, {"id": 2, "result": {
            "turn": {"id": turn, "status": "inProgress"}}})
    return state


def _register(state, request_id, uid):
    state["codex_steer_pending"][request_id] = uid
    state["codex_written"].add(uid)


def _reject(request_id, message="rejected"):
    return {"id": request_id, "error": {"code": -32602, "message": message}}


def _confirm(uid):
    return {"method": "item/completed", "params": {"item": {
        "type": "userMessage", "id": "um-" + uid, "clientId": uid,
        "content": []}}}


def _snapshot(state):
    return (set(state["codex_written"]), dict(state["codex_steer_pending"]),
            set(state["codex_steer_retry_used"]), set(state["confirmed"]))


class SteerRejectFoldTests(unittest.TestCase):
    def test_matching_reject_releases_once_and_retry_reject_stays_written(self):
        state = _session_state()
        row = {"op": "user", "uuid": UUID_A, "text": "加一句"}
        # Selection writes the UUID. Without the reject fold the row is never
        # selected again; this is the baseline this repair removes.
        actions = runner.steer_actions([row], set(), state["codex_written"], set())
        self.assertEqual([kind for kind, _ in actions], ["user"])
        self.assertIn(UUID_A, state["codex_written"])
        self.assertEqual(
            runner.steer_actions([row], set(), state["codex_written"], set()), [])

        _register(state, 4, UUID_A)
        runner.apply_codex_event(state, _reject(4))
        self.assertEqual(state["codex_steer_retry_used"], {UUID_A})
        self.assertNotIn(UUID_A, state["codex_written"])
        self.assertEqual(state["codex_steer_pending"], {})
        self.assertEqual(state["confirmed"], set())

        # The next selection is allowed exactly once: same row, same UUID.
        actions = runner.steer_actions([row], set(), state["codex_written"], set())
        self.assertEqual([kind for kind, _ in actions], ["user"])
        self.assertIn(UUID_A, state["codex_written"])

        # A second explicit reject consumes no new quota and keeps it written.
        _register(state, 5, UUID_A)
        runner.apply_codex_event(state, _reject(5))
        self.assertEqual(state["codex_steer_retry_used"], {UUID_A})
        self.assertIn(UUID_A, state["codex_written"])
        self.assertEqual(state["codex_steer_pending"], {})
        self.assertEqual(
            runner.steer_actions([row], set(), state["codex_written"], set()), [])

        # Duplicate and stale ids plus a later turn cannot refresh the quota.
        runner.apply_codex_event(state, _reject(4))
        runner.apply_codex_event(state, _reject(5))
        runner.apply_codex_event(state, {"method": "turn/completed", "params": {
            "turn": {"id": TURN, "status": "completed"}}})
        runner.apply_codex_event(state, {"id": 6, "result": {
            "turn": {"id": "turn_2", "status": "inProgress"}}})
        self.assertEqual(state["codex_steer_retry_used"], {UUID_A})
        self.assertIn(UUID_A, state["codex_written"])
        self.assertEqual(
            runner.steer_actions([row], set(), state["codex_written"], set()), [])

        # Rejection is the key's presence, not its payload truthiness.
        for name, error in (("empty", {}), ("null", None),
                            ("message", {"message": "x"})):
            with self.subTest(error=name):
                case = _session_state()
                _register(case, 7, UUID_B)
                runner.apply_codex_event(case, {"id": 7, "error": error})
                self.assertEqual(case["codex_steer_retry_used"], {UUID_B})
                self.assertNotIn(UUID_B, case["codex_written"])

    def test_only_current_pending_response_can_release(self):
        state = _session_state()
        _register(state, 4, UUID_A)
        for name, event in (
                ("unknown_id", {"id": 99, "error": {}}),
                ("bool_id", {"id": True, "error": {}}),
                ("float_id", {"id": 4.0, "error": {}}),
                ("string_id", {"id": "4", "error": {}}),
                ("null_id", {"id": None, "error": {}}),
                ("list_id", {"id": [4], "error": {}}),
                ("dict_id", {"id": {"id": 4}, "error": {}}),
                ("server_request", {
                    "id": 4, "method": "item/commandExecution/requestApproval",
                    "params": {}}),
                ("no_result_or_error", {"id": 4})):
            with self.subTest(name=name):
                before = _snapshot(state)
                runner.apply_codex_event(state, event)
                self.assertEqual(_snapshot(state), before)

        # A new steer for the same UUID retires the old id; its late error is
        # inert and the current id still releases once.
        state["codex_steer_pending"].pop(4)
        _register(state, 5, UUID_A)
        runner.apply_codex_event(state, _reject(4))
        self.assertIn(UUID_A, state["codex_written"])
        self.assertEqual(state["codex_steer_retry_used"], set())
        self.assertEqual(state["codex_steer_pending"], {5: UUID_A})
        runner.apply_codex_event(state, _reject(5))
        self.assertNotIn(UUID_A, state["codex_written"])
        self.assertEqual(state["codex_steer_retry_used"], {UUID_A})

        # Pending id=1 must not be released by a bool or float that compares
        # equal to 1: the match is an exact integer.
        one = _session_state()
        _register(one, 1, UUID_A)
        for name, event in (("bool_true", {"id": True, "error": {}}),
                            ("bool_false_zero", {"id": False, "error": {}}),
                            ("float_one", {"id": 1.0, "error": {}})):
            with self.subTest(name=name):
                before = _snapshot(one)
                runner.apply_codex_event(one, event)
                self.assertEqual(_snapshot(one), before)
        self.assertEqual(one["codex_steer_pending"], {1: UUID_A})

        # Another session's state and id never leak in.
        other = _session_state()
        _register(other, 9, UUID_B)
        runner.apply_codex_event(state, _reject(9))
        self.assertIn(UUID_B, other["codex_written"])
        self.assertEqual(other["codex_steer_retry_used"], set())
        self.assertEqual(other["codex_steer_pending"], {9: UUID_B})

        # A completed turn does not invalidate a still-pending steer: its late
        # error is a current response and releases the one chance.
        late = _session_state()
        _register(late, 10, UUID_A)
        runner.apply_codex_event(late, {"method": "turn/completed", "params": {
            "turn": {"id": TURN, "status": "completed"}}})
        self.assertFalse(late["turn_active"])
        runner.apply_codex_event(late, _reject(10))
        self.assertNotIn(UUID_A, late["codex_written"])
        self.assertEqual(late["codex_steer_retry_used"], {UUID_A})

        # result + error is a rejection; its result is not folded into a turn.
        # Complete the old turn first, so it is the early return - not the
        # "current turn still active" guard - that keeps the simultaneous
        # result from rewriting the finished turn.
        dual = _session_state()
        runner.apply_codex_event(dual, {"method": "turn/completed", "params": {
            "turn": {"id": TURN, "status": "completed"}}})
        self.assertFalse(dual["turn_active"])
        before_turn = dual["turn_id"]
        before_active = dual["turn_active"]
        before_outcome = dict(dual["native_outcome"])
        before_seq = dual["result_seq"]
        _register(dual, 8, UUID_A)
        runner.apply_codex_event(dual, {
            "id": 8,
            "result": {"turn": {"id": "turn_from_result", "status": "inProgress"}},
            "error": {"message": "rejected"}})
        self.assertNotIn(UUID_A, dual["codex_written"])
        self.assertEqual(dual["codex_steer_retry_used"], {UUID_A})
        self.assertEqual(dual["turn_id"], before_turn)
        self.assertEqual(dual["turn_active"], before_active)
        self.assertEqual(dual["result_seq"], before_seq)
        self.assertEqual(dual.get("native_outcome"), before_outcome)
        self.assertEqual(dual["codex_steer_pending"], {})

    def test_result_silence_and_transport_failure_are_not_delivery_or_retry(self):
        state = _session_state()
        _register(state, 4, UUID_A)
        runner.apply_codex_event(state, {"id": 4, "result": {"turnId": TURN}})
        self.assertEqual(state["codex_steer_pending"], {})
        self.assertIn(UUID_A, state["codex_written"])
        self.assertEqual(state["codex_steer_retry_used"], set())
        self.assertEqual(state["confirmed"], set())

        # No response at all: written and pending are untouched.
        _register(state, 5, UUID_B)
        self.assertIn(UUID_B, state["codex_written"])
        self.assertEqual(state["codex_steer_pending"], {5: UUID_B})
        self.assertEqual(state["codex_steer_retry_used"], set())

        # A local write failure is not a provider reject.
        class BrokenStdin:
            def write(self, data):
                raise BrokenPipeError("closed")

            def flush(self):
                pass

        class BrokenProc:
            stdin = BrokenStdin()

        with self.assertRaises(BrokenPipeError):
            runner._codex_rpc(BrokenProc(), "turn/steer", {
                "threadId": THREAD, "expectedTurnId": TURN,
                "input": [{"type": "text", "text": "x"}],
                "clientUserMessageId": UUID_B}, 6)
        self.assertEqual(state["codex_steer_pending"], {5: UUID_B})
        self.assertIn(UUID_B, state["codex_written"])
        self.assertEqual(state["codex_steer_retry_used"], set())

        # The rejection's free text never enters the session state.
        sentinel = "SENTINEL_RPC_ERROR_TEXT"
        _register(state, 7, UUID_C)
        runner.apply_codex_event(state, _reject(7, sentinel))
        self.assertNotIn(sentinel, json.dumps(state, default=list))
        self.assertEqual(state["codex_steer_retry_used"], {UUID_C})


class _BoundedStop:
    """A stop event that also bounds the number of session wait cycles."""

    def __init__(self, cycles):
        self.cycles = cycles
        self.waits = 0
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, seconds):
        self.waits += 1
        if self.waits >= self.cycles:
            self._set = True
        return self._set


class _StubStdin:
    """In-memory stdin: every frame is folded back immediately."""

    def __init__(self, harness):
        self.harness = harness
        self.closed = False

    def write(self, data):
        # The session must never hold the state lock across the pipe write.
        if not self.harness.lock.acquire(timeout=1):
            raise AssertionError("RPC written while the session lock was held")
        try:
            frame = json.loads(data.decode("utf-8"))
            self.harness.frames.append(frame)
            events = self.harness.responder(frame)
        finally:
            self.harness.lock.release()
        for event in events:
            with self.harness.lock:
                runner.apply_stdin_event(
                    runner.CODEX_APP_SERVER, self.harness.state, event)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _SessionProtocol:
    """Minimal app-server responder for the in-memory session tests."""

    def __init__(self, harness, steer_modes=(), *, echo_starts=False,
                 complete_starts=False):
        self.h = harness
        self.steer_modes = list(steer_modes)
        self.echo_starts = echo_starts
        self.complete_starts = complete_starts
        self.prompt_uuid = harness.prompt_uuid()
        self.prompt_sent = False
        self.turn_counter = 0
        self.current_turn = None
        self.steer_count = 0
        self.steer_ids = []

    def __call__(self, frame):
        method = frame.get("method")
        rid = frame.get("id")
        if method == "initialize" and rid is not None:
            return [{"id": rid, "result": {"userAgent": "stub"}}]
        if method == "thread/start" and rid is not None:
            return [{"id": rid, "result": {
                "thread": {"id": THREAD, "forkedFromId": None}}}]
        if method == "turn/start" and rid is not None:
            return self._turn_start(frame)
        if method == "turn/steer" and rid is not None:
            return self._steer(frame)
        if method == "turn/interrupt" and rid is not None:
            return self._interrupt(frame)
        return []

    def _new_turn(self, frame, *, echo):
        self.turn_counter += 1
        self.current_turn = "turn_%d" % self.turn_counter
        events = [
            {"id": frame["id"], "result": {
                "turn": {"id": self.current_turn, "status": "inProgress"}}},
            {"method": "turn/started", "params": {
                "turn": {"id": self.current_turn, "status": "inProgress"}}},
        ]
        if echo:
            cid = (frame.get("params") or {}).get("clientUserMessageId")
            events.append(_confirm(cid))
        return events

    def _completion(self):
        return {"method": "turn/completed", "params": {
            "turn": {"id": self.current_turn, "status": "completed"}}}

    def _turn_start(self, frame):
        params = frame.get("params") or {}
        if not self.prompt_sent and params.get("clientUserMessageId") == self.prompt_uuid:
            self.prompt_sent = True
            return self._new_turn(frame, echo=False)
        events = self._new_turn(frame, echo=self.echo_starts)
        if self.complete_starts:
            events.append(self._completion())
        return events

    def _steer(self, frame):
        mode = (self.steer_modes[self.steer_count]
                if self.steer_count < len(self.steer_modes) else "reject")
        self.steer_count += 1
        self.steer_ids.append(frame["id"])
        cid = (frame.get("params") or {}).get("clientUserMessageId")
        if mode == "reject":
            return [_reject(frame["id"], "stub reject")]
        if mode == "silent":
            return []
        if mode == "confirm":
            return [
                {"id": frame["id"], "result": {"turnId": self.current_turn}},
                _confirm(cid),
            ]
        if mode == "confirm_then_error":
            return [_confirm(cid), _reject(frame["id"], "late reject")]
        if mode == "reject_then_confirm":
            return [_reject(frame["id"], "stub reject"), _confirm(cid)]
        if mode == "result_only":
            return [{"id": frame["id"], "result": {"turnId": self.current_turn}}]
        if mode == "reject_after_complete":
            return [
                self._completion(),
                _reject(frame["id"], "stub reject"),
            ]
        raise AssertionError("unknown steer mode " + str(mode))

    def _interrupt(self, frame):
        return [{"id": frame["id"], "result": {}}]


class _SessionHarness:
    def __init__(self, directory, responder, *, cycles=8, stop=None):
        self.directory = Path(directory)
        self.state = {"result_text": None, "confirmed": set()}
        self.lock = threading.Lock()
        self.stop = stop if stop is not None else _BoundedStop(cycles)
        self.frames = []
        self.responder = responder
        self.stdin = _StubStdin(self)
        self.proc = type("Proc", (), {"stdin": self.stdin})()
        self.prompt = "task"
        self.execution_id = "exec-" + uuid.uuid4().hex
        self.spec = {"argv": ["codex", "app-server"],
                     "execution_id": self.execution_id}
        self.thread = None

    def prompt_uuid(self):
        return str(uuid.uuid5(
            uuid.UUID(runner.session_id_from_execution(self.execution_id)),
            "prompt"))

    def start(self):
        self.thread = threading.Thread(
            target=runner.codex_app_server_session,
            args=(self.proc, self.directory, self.spec, self.prompt,
                  self.state, self.lock, self.stop),
            daemon=True)
        self.thread.start()

    def join(self, timeout=5):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise AssertionError("session did not stop")

    def frames_of(self, method):
        return [frame for frame in self.frames if frame.get("method") == method]


def _run_session(directory, protocol_factory, mailbox=(), *, cycles=8):
    for uid, text in mailbox:
        runner.append_inject(directory, uid, text)
    harness = _SessionHarness(directory, None, cycles=cycles)
    harness.responder = protocol_factory(harness)
    harness.start()
    return harness


def _complete_turn_on_read(harness, protocol, nth):
    """Wrap `read_steer_mailbox`: on read `nth`, fold the current turn's
    completion under the real session lock, outside the session's lock."""
    real_read = runner.read_steer_mailbox
    reads = []

    def read_then_complete(directory):
        rows = real_read(directory)
        reads.append(True)
        if len(reads) == nth:
            with harness.lock:
                runner.apply_codex_event(harness.state, {
                    "method": "turn/completed", "params": {
                        "turn": {"id": protocol.current_turn,
                                 "status": "completed"}}})
        return rows

    return read_then_complete, reads


class SteerRejectSessionTests(unittest.TestCase):
    def test_confirmation_wins_across_reject_and_send_admission(self):
        # (a) confirm before the late reject: pending is retired, written kept.
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            harness = _run_session(
                directory,
                lambda h: _SessionProtocol(h, ["confirm_then_error"]),
                [(UUID_A, "加一句")], cycles=8)
            harness.join()
            self.assertEqual(len(harness.frames_of("turn/steer")), 1)
            self.assertIn(UUID_A, harness.state["confirmed"])
            self.assertIn(UUID_A, harness.state["codex_written"])
            self.assertEqual(harness.state["codex_steer_retry_used"], set())
            self.assertEqual(harness.state["codex_steer_pending"], {})

        # (b) reject then confirm before the next admission: rejected written is
        # restored, so the pool selects nothing and no second frame is sent.
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            harness = _run_session(
                directory,
                lambda h: _SessionProtocol(h, ["reject_then_confirm"]),
                [(UUID_A, "加一句")], cycles=8)
            harness.join()
            self.assertEqual(len(harness.frames_of("turn/steer")), 1)
            self.assertIn(UUID_A, harness.state["confirmed"])
            self.assertIn(UUID_A, harness.state["codex_written"])
            self.assertEqual(harness.state["codex_steer_retry_used"], {UUID_A})

        # (c) reject -> resend admitted -> confirm -> late error: two frames,
        # confirmation is not downgraded, and no third automatic send.
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            harness = _run_session(
                directory,
                lambda h: _SessionProtocol(h, ["reject", "confirm_then_error"]),
                [(UUID_A, "加一句")], cycles=10)
            harness.join()
            steers = harness.frames_of("turn/steer")
            self.assertEqual(len(steers), 2)
            self.assertEqual(steers[0]["params"]["clientUserMessageId"], UUID_A)
            self.assertEqual(steers[1]["params"]["clientUserMessageId"], UUID_A)
            self.assertNotEqual(steers[0]["id"], steers[1]["id"])
            self.assertIn(UUID_A, harness.state["confirmed"])
            self.assertIn(UUID_A, harness.state["codex_written"])
            self.assertEqual(harness.state["codex_steer_retry_used"], {UUID_A})

        # (d) actions selected but not yet admitted: a confirmation folded at
        # selection makes admission skip the frame. The skipped action does not
        # advance written_at_seq, so the existing idle close still happens.
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            runner.append_inject(directory, UUID_A, "加一句")
            stop = threading.Event()
            harness = _SessionHarness(
                directory, None, stop=stop)
            harness.responder = _SessionProtocol(harness)
            real_select = runner.steer_actions
            selected = []

            def select_then_confirm(rows, confirmed, written, seen):
                actions = real_select(rows, confirmed, written, seen)
                if actions and not selected:
                    selected.append(True)
                    # The session holds this lock across selection.
                    runner.apply_codex_event(harness.state, _confirm(UUID_A))
                    runner.apply_codex_event(harness.state, {
                        "method": "turn/completed", "params": {
                            "turn": {"id": harness.responder.current_turn,
                                     "status": "completed"}}})
                return actions

            with patch.object(runner, "steer_actions", new=select_then_confirm):
                with patch.object(runner, "STREAM_IDLE_CLOSE_S", 0.0):
                    harness.start()
                    harness.join()
            self.assertTrue(selected, "selection never ran")
            self.assertEqual(harness.frames_of("turn/steer"), [])
            self.assertIn(UUID_A, harness.state["confirmed"])
            self.assertIn(UUID_A, harness.state["codex_written"])
            self.assertEqual(harness.state["codex_steer_retry_used"], set())

    def test_confirmation_at_admission_boundary_blocks_resend(self):
        # The first steer is rejected. On the resend, the frame is constructed
        # outside the lock but the pending id is not registered yet; a
        # confirmation folded exactly there must be seen by the admission
        # recheck, so no second frame is sent and written stays restored.
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            runner.append_inject(directory, UUID_A, "加一句")
            harness = _SessionHarness(directory, None, cycles=8)
            harness.responder = _SessionProtocol(harness, ["reject"])
            real_input = runner.codex_turn_input
            admissions = []

            def input_then_confirm(text, message_id):
                result = real_input(text, message_id)
                if message_id == UUID_A:
                    admissions.append(message_id)
                    if len(admissions) == 2:
                        with harness.lock:
                            runner.apply_codex_event(
                                harness.state, _confirm(UUID_A))
                return result

            with patch.object(runner, "codex_turn_input",
                              new=input_then_confirm):
                harness.start()
                harness.join()

            self.assertEqual(len(admissions), 2)
            self.assertEqual(len(harness.frames_of("turn/steer")), 1)
            self.assertIn(UUID_A, harness.state["confirmed"])
            self.assertIn(UUID_A, harness.state["codex_written"])
            self.assertEqual(harness.state["codex_steer_pending"], {})
            self.assertEqual(harness.state["codex_steer_retry_used"], {UUID_A})

    def test_resend_start_on_new_turn_is_not_closed_by_previous_completion(self):
        # First steer is rejected. The next read observes turn_1/completed
        # outside the lock (seq=1, turn inactive), so the resend is a
        # same-thread turn/start for turn_2. Its admission must record seq=1:
        # the older completion must not idle-close the still-active new turn.
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            runner.append_inject(directory, UUID_A, "加一句")
            cycles = 10
            harness = _SessionHarness(directory, None, cycles=cycles)
            protocol = _SessionProtocol(harness, ["reject"])
            harness.responder = protocol
            read, reads = _complete_turn_on_read(harness, protocol, 2)

            with patch.object(runner, "read_steer_mailbox", new=read):
                with patch.object(runner, "STREAM_IDLE_CLOSE_S", 0.0):
                    harness.start()
                    harness.join()

            self.assertGreaterEqual(len(reads), 3)
            steers = harness.frames_of("turn/steer")
            self.assertEqual(len(steers), 1)
            starts = harness.frames_of("turn/start")
            self.assertEqual(len(starts), 2)
            self.assertEqual(starts[1]["params"]["clientUserMessageId"], UUID_A)
            self.assertEqual(starts[1]["params"]["threadId"], THREAD)
            self.assertNotIn("expectedTurnId", starts[1]["params"])
            self.assertEqual(harness.state["result_seq"], 1)
            self.assertEqual(harness.state["turn_id"], "turn_2")
            self.assertTrue(harness.state["turn_active"])
            # The session ran its full bounded budget instead of closing idle.
            self.assertEqual(harness.stop.waits, cycles)
            self.assertTrue(harness.stop.is_set())

    def test_defer_and_later_action_discard_keep_confirmed_written(self):
        # (a) An explicit interrupt defers an unconfirmed row. A confirmation
        # folded during the interrupt write wins: written is kept.
        class _DeferConfirmProtocol(_SessionProtocol):
            def _interrupt(self, frame):
                return [{"id": frame["id"], "result": {}},
                        _confirm(UUID_A)]

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            runner.append_interrupt(directory, "interrupt-1")
            runner.append_inject(directory, UUID_A, "加一句")
            harness = _SessionHarness(directory, None, cycles=8)
            harness.responder = _DeferConfirmProtocol(harness)
            harness.start()
            harness.join()

            self.assertEqual(len(harness.frames_of("turn/interrupt")), 1)
            self.assertEqual(harness.frames_of("turn/steer"), [])
            self.assertIn(UUID_A, harness.state["confirmed"])
            self.assertIn(UUID_A, harness.state["codex_written"])
            self.assertEqual(harness.state["codex_steer_pending"], {})
            self.assertEqual(harness.state["codex_steer_retry_used"], set())

        # (b) Two rows are selected and the first opens a turn/start. The
        # later row is confirmed during that write; the post-start discard
        # must restore it, not drop it.
        class _LaterConfirmProtocol(_SessionProtocol):
            def _turn_start(self, frame):
                events = super()._turn_start(frame)
                cid = (frame.get("params") or {}).get("clientUserMessageId")
                if cid == UUID_A:
                    events = events + [_confirm(UUID_B)]
                return events

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            runner.append_inject(directory, UUID_A, "第一句")
            runner.append_inject(directory, UUID_B, "第二句")
            harness = _SessionHarness(directory, None, cycles=8)
            protocol = _LaterConfirmProtocol(harness)
            harness.responder = protocol
            real_read = runner.read_steer_mailbox
            reads = []
            kept_at_next_read = []

            def read_and_snapshot(path):
                rows = real_read(path)
                reads.append(True)
                if len(reads) == 1:
                    with harness.lock:
                        runner.apply_codex_event(harness.state, {
                            "method": "turn/completed", "params": {
                                "turn": {"id": protocol.current_turn,
                                         "status": "completed"}}})
                elif len(reads) == 2:
                    # Before this selection can re-add it, observe written as
                    # the discard left it.
                    with harness.lock:
                        kept_at_next_read.append(
                            UUID_B in harness.state["codex_written"])
                return rows

            with patch.object(runner, "read_steer_mailbox",
                              new=read_and_snapshot):
                harness.start()
                harness.join()

            self.assertEqual(kept_at_next_read, [True])
            self.assertEqual(harness.frames_of("turn/steer"), [])
            self.assertEqual(len(harness.frames_of("turn/start")), 2)
            self.assertIn(UUID_B, harness.state["confirmed"])
            self.assertIn(UUID_B, harness.state["codex_written"])
            self.assertEqual(harness.state["codex_steer_pending"], {})
            self.assertEqual(harness.state["codex_steer_retry_used"], set())

    def test_write_failure_after_pending_registration_does_not_resend(self):
        # The session registers the pending id before the frame. If the pipe
        # write/flush then fails, that is not a provider reject: no resend, no
        # confirmation, and the exiting session clears the pending map.
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            runner.append_inject(directory, UUID_A, "加一句")
            harness = _SessionHarness(directory, None, cycles=8)
            harness.responder = _SessionProtocol(harness)
            real_write = harness.stdin.write
            registered = []

            def failing_write(data):
                frame = json.loads(data.decode("utf-8"))
                if frame.get("method") == "turn/steer":
                    with harness.lock:
                        registered.append(
                            dict(harness.state["codex_steer_pending"]))
                    raise BrokenPipeError("closed")
                return real_write(data)

            harness.stdin.write = failing_write
            harness.start()
            harness.join()

            self.assertEqual(len(registered), 1)
            self.assertEqual(list(registered[0].values()), [UUID_A])
            self.assertEqual(harness.frames_of("turn/steer"), [])
            self.assertEqual(harness.state["codex_steer_pending"], {})
            self.assertEqual(harness.state["codex_steer_retry_used"], set())
            self.assertEqual(harness.state["confirmed"], set())
            self.assertIn(UUID_A, harness.state["codex_written"])
            self.assertEqual(len(harness.frames_of("turn/start")), 1)

    def test_prompt_and_inject_start_errors_do_not_consume_resend_quota(self):
        # (a) The initial prompt's turn/start errors. It is not a steer
        # response, so no association and no resend quota.
        class _PromptErrorProtocol(_SessionProtocol):
            def _turn_start(self, frame):
                self.prompt_sent = True
                return [{"id": frame["id"],
                         "error": {"code": -32000, "message": "no"}}]

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            harness = _SessionHarness(directory, None, cycles=3)
            harness.responder = _PromptErrorProtocol(harness)
            harness.start()
            harness.join()

            self.assertEqual(len(harness.frames_of("turn/start")), 1)
            self.assertEqual(harness.frames_of("turn/steer"), [])
            self.assertEqual(harness.state["codex_steer_pending"], {})
            self.assertEqual(harness.state["codex_steer_retry_used"], set())

        # (b) The inject's own turn/start (turn already completed) errors. It
        # is outside this repair's association: no quota, no automatic retry.
        class _RejectingInjectStartProtocol(_SessionProtocol):
            def _turn_start(self, frame):
                params = frame.get("params") or {}
                if (not self.prompt_sent
                        and params.get("clientUserMessageId") == self.prompt_uuid):
                    return super()._turn_start(frame)
                return [{"id": frame["id"],
                         "error": {"code": -32000, "message": "start rejected"}}]

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            runner.append_inject(directory, UUID_A, "加一句")
            harness = _SessionHarness(directory, None, cycles=6)
            protocol = _RejectingInjectStartProtocol(harness)
            harness.responder = protocol
            read, _ = _complete_turn_on_read(harness, protocol, 1)

            with patch.object(runner, "read_steer_mailbox", new=read):
                harness.start()
                harness.join()

            self.assertEqual(len(harness.frames_of("turn/start")), 2)
            self.assertEqual(harness.frames_of("turn/steer"), [])
            self.assertEqual(harness.state["codex_steer_pending"], {})
            self.assertEqual(harness.state["codex_steer_retry_used"], set())
            self.assertIn(UUID_A, harness.state["codex_written"])

    def test_pending_precedes_immediate_response_and_retry_is_bounded(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            harness = _run_session(
                directory,
                lambda h: _SessionProtocol(h, ["reject", "reject"]),
                [(UUID_A, "加一句")], cycles=12)
            harness.join()
            # The resend reused the same mailbox row: no new row was appended.
            rows = [row for row in runner.read_steer_mailbox(directory)
                    if row.get("op") == "user"]

        steers = harness.frames_of("turn/steer")
        # The first error is folded inside the write. Two frames prove the
        # pending id was registered before the frame reached the stub.
        self.assertEqual(len(steers), 2)
        self.assertEqual([frame["params"]["clientUserMessageId"] for frame in steers],
                         [UUID_A, UUID_A])
        self.assertEqual([frame["params"]["input"] for frame in steers],
                         [[{"type": "text", "text": "加一句"}]] * 2)
        self.assertNotEqual(steers[0]["id"], steers[1]["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["uuid"], UUID_A)
        # The second reject kept written and consumed no further quota.
        self.assertIn(UUID_A, harness.state["codex_written"])
        self.assertEqual(harness.state["codex_steer_retry_used"], {UUID_A})
        self.assertEqual(harness.state["codex_steer_pending"], {})
        self.assertEqual(harness.state["confirmed"], set())
        # One prompt turn/start only; the retry did not start a new turn.
        self.assertEqual(len(harness.frames_of("turn/start")), 1)

    def test_interrupt_and_new_attempt_retire_old_ids_without_refilling_retry(self):
        # A real session consumes the one automatic resend first: steer #1 is
        # rejected, steer #2 stays pending. An explicit interrupt then retires
        # that pending association and defers the row. The deferred row is
        # resent as a same-thread turn/start; the old steer's late error
        # arrives at the new attempt and is stale. The quota is not refreshed.
        class _RetireProtocol(_SessionProtocol):
            def __init__(self, harness, directory):
                super().__init__(harness)
                self.directory = directory
                self.second_steer_id = None
                self.pending_at_resend = None
                self.late_error_sent = False

            def _steer(self, frame):
                self.steer_count += 1
                self.steer_ids.append(frame["id"])
                if self.steer_count == 1:
                    return [_reject(frame["id"], "first reject")]
                self.second_steer_id = frame["id"]
                # The explicit interrupt is appended while this steer is
                # pending, so the next mailbox read defers it.
                runner.append_interrupt(self.directory, "interrupt-1")
                return []

            def _interrupt(self, frame):
                # Completing the turn releases hold_injects; the old steer
                # response is withheld until the new attempt.
                return [{"id": frame["id"], "result": {}}, self._completion()]

            def _turn_start(self, frame):
                if not self.prompt_sent:
                    return super()._turn_start(frame)
                events = []
                if (self.second_steer_id is not None
                        and not self.late_error_sent):
                    self.late_error_sent = True
                    # Real session state: the interrupt and the new attempt
                    # must already have retired the pending association.
                    self.pending_at_resend = dict(
                        self.h.state.get("codex_steer_pending") or {})
                    events.append(_reject(self.second_steer_id, "late reject"))
                events.extend(super()._turn_start(frame))
                return events

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            runner.append_inject(directory, UUID_A, "加一句")
            harness = _SessionHarness(directory, None, cycles=12)
            protocol = _RetireProtocol(harness, directory)
            harness.responder = protocol
            harness.start()
            harness.join()

        steers = harness.frames_of("turn/steer")
        self.assertEqual(len(steers), 2)
        self.assertNotEqual(steers[0]["id"], steers[1]["id"])
        starts = [frame for frame in harness.frames_of("turn/start")
                  if (frame.get("params") or {}).get("clientUserMessageId") == UUID_A]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["params"]["threadId"], THREAD)
        self.assertNotIn("expectedTurnId", starts[0]["params"])
        self.assertEqual(harness.frames_of("turn/interrupt")[0]["params"]["turnId"],
                         "turn_1")
        # The interrupt retired the pending steer before the new attempt.
        self.assertEqual(protocol.pending_at_resend, {})
        self.assertTrue(protocol.late_error_sent)
        # The late error is stale: no new quota, no third automatic send.
        self.assertEqual(harness.state["codex_steer_retry_used"], {UUID_A})
        self.assertEqual(harness.state["codex_steer_pending"], {})
        self.assertEqual(harness.state["confirmed"], set())
        self.assertIn(UUID_A, harness.state["codex_written"])


SUPERVISE_STUB = r"""
import json, pathlib, sys
base = pathlib.Path(__file__).resolve().parent
scenario = json.loads((base / "scenario.json").read_text(encoding="utf-8"))
marker = scenario["marker"]
thread_id = "thr_from_server"
turn_counter = 0
current_turn = None
seen = []
steer_count = 0

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def mark(name, data=""):
    (base / (marker + "-" + name)).write_text(data, encoding="utf-8")

def echo(client_id):
    emit({"method": "item/completed", "params": {"item": {
        "type": "userMessage", "id": "um-%d" % len(seen), "clientId": client_id,
        "content": []}}})

def start_turn(frame, echo_client):
    global turn_counter, current_turn
    turn_counter += 1
    current_turn = "turn_%d" % turn_counter
    emit({"id": frame["id"], "result": {"turn": {"id": current_turn, "status": "inProgress"}}})
    emit({"method": "turn/started", "params": {"turn": {"id": current_turn, "status": "inProgress"}}})
    if echo_client:
        echo((frame.get("params") or {}).get("clientUserMessageId"))

def complete(status="completed"):
    emit({"method": "turn/completed", "params": {"turn": {"id": current_turn, "status": status}}})

while True:
    line = sys.stdin.readline()
    if not line:
        break
    msg = json.loads(line)
    seen.append(msg)
    method = msg.get("method")
    if method == "initialize" and "id" in msg:
        emit({"id": msg["id"], "result": {"userAgent": "stub"}})
    elif method == "thread/start" and "id" in msg:
        emit({"id": msg["id"], "result": {"thread": {"id": thread_id, "forkedFromId": None}}})
        emit({"method": "thread/started", "params": {"thread": {"id": thread_id, "forkedFromId": None}}})
    elif method == "turn/start" and "id" in msg:
        is_prompt = turn_counter == 0
        start_turn(msg, echo_client=not is_prompt)
        if is_prompt:
            mark("ready")
        elif scenario.get("complete_starts", False):
            complete()
    elif method == "turn/steer" and "id" in msg:
        modes = scenario.get("steers") or []
        mode = modes[steer_count] if steer_count < len(modes) else "reject"
        steer_count += 1
        cid = (msg.get("params") or {}).get("clientUserMessageId")
        if mode == "reject":
            emit({"id": msg["id"], "error": {"code": -32602, "message": "SENTINEL_RPC_ERROR"}})
            if scenario.get("complete_after_steers", False) and steer_count >= len(modes):
                complete()
        elif mode == "reject_after_complete":
            complete()
            emit({"id": msg["id"], "error": {"code": -32602, "message": "SENTINEL_RPC_ERROR"}})
        elif mode == "confirm":
            emit({"id": msg["id"], "result": {"turnId": current_turn}})
            echo(cid)
            complete()
        elif mode == "result_only":
            emit({"id": msg["id"], "result": {"turnId": current_turn}})
            if scenario.get("complete_after_steers", False) and steer_count >= len(modes):
                complete()
        else:
            raise SystemExit(3)
    elif method == "turn/interrupt" and "id" in msg:
        emit({"id": msg["id"], "result": {}})
mark("eof", json.dumps(seen))
"""


@unittest.skipUnless(os.name == "posix", "process-group supervise is POSIX")
class SteerRejectSuperviseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub_codex.py"
        self.stub.write_text(SUPERVISE_STUB, encoding="utf-8")
        cfg = config(self.workspace)
        cfg["installations"][0]["argv"] = [
            sys.executable, str(self.stub), "exec",
            "--sandbox", "workspace-write", "-"]
        self.app = Service(cfg, self.root / "data")
        self.app.store.put("probe", {
            "id": "codex-local", "available": True,
            "supervision": runner.PGROUP_COOPERATIVE})
        self.thread = None
        self.directory = None

    def tearDown(self):
        if self.directory is not None:
            started = runner.read_json_bounded(self.directory / "started.json") or {}
            pid = started.get("pid")
            if self.thread is not None and self.thread.is_alive():
                if isinstance(pid, int) and pid > 0:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                self.thread.join(5)
        self.app.store.close()
        self.tmp.cleanup()

    def _scenario(self, steers, **extra):
        marker = "run-" + uuid.uuid4().hex[:10]
        body = {"steers": steers, "marker": marker}
        body.update(extra)
        (self.root / "scenario.json").write_text(
            json.dumps(body), encoding="utf-8")
        return marker

    def _await(self, marker, name, timeout=10):
        path = self.root / (marker + "-" + name)
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), "stub did not reach " + name)

    def _frames(self, marker):
        path = self.root / (marker + "-eof")
        self.assertTrue(path.exists(), "stub did not record stdin frames")
        return json.loads(path.read_text(encoding="utf-8"))

    def _prepare(self):
        sent = self.app.send("bot-main", {"text": "task", "allow_write": True},
                             "steer-" + uuid.uuid4().hex[:8])
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "task"})
        task = self.app.store.get("task", submitted["task_id"])
        task["attempt_uuid"] = "attempt-" + uuid.uuid4().hex
        self.app.store.put("task", task)
        task = self.app.store.get("task", task["id"])
        host = self.app.execution.hosts[task["host_id"]]
        directory = Path(self.app.execution.directory(host, task["execution_id"]))
        spec = self.app.execution.spec(task)
        self.assertEqual(spec["stdin_protocol"], runner.CODEX_APP_SERVER)
        self.app.execution.prepare(host, str(directory), spec)
        self.directory = directory
        return task, sent, directory

    def _run(self, marker, directory, message, text):
        def work():
            with patch.object(runner, "_detect_supervision",
                              return_value=runner.PGROUP_COOPERATIVE):
                runner.supervise(directory)
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        self._await(marker, "ready")
        runner.append_inject(directory, message, text)
        self.thread.join(20)
        self.assertFalse(self.thread.is_alive(), "runner did not finish")
        return runner.inspect(directory, include_tail=True)

    def test_reject_then_single_resend_is_confirmed_on_receipt(self):
        # Active turn: steer -> explicit reject -> one steer resend -> confirm.
        marker = self._scenario(["reject", "confirm"])
        task, sent, directory = self._prepare()
        message = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        receipt = self._run(marker, directory, message, "加一句")
        self.assertEqual(receipt["status"], "succeeded")
        self.assertFalse(receipt["timed_out"])
        self.assertTrue(receipt["process_tree_confirmed"])
        self.assertEqual(receipt["injects"],
                         [{"uuid": message, "delivered": True}])
        frames = self._frames(marker)
        steers = [frame for frame in frames if frame.get("method") == "turn/steer"]
        self.assertEqual(len(steers), 2)
        self.assertEqual([frame["params"]["clientUserMessageId"] for frame in steers],
                         [message, message])
        self.assertEqual([frame["params"]["input"] for frame in steers],
                         [[{"type": "text", "text": "加一句"}]] * 2)
        self.assertNotEqual(steers[0]["id"], steers[1]["id"])
        self.assertEqual(steers[0]["params"]["expectedTurnId"], "turn_1")
        # No structured receipt field copies the provider's error text. The
        # tail is the existing output.log record, not a new field.
        structured = {key: receipt.get(key) for key in (
            "native_outcome", "injects", "error")}
        self.assertNotIn("SENTINEL_RPC_ERROR", json.dumps(structured))
        self.assertNotIn("SENTINEL_RPC_ERROR", json.dumps(frames))

        # Completion is observed before the reject folds: the resend is a
        # same-thread turn/start with the same UUID and text.
        marker = self._scenario(["reject_after_complete"], complete_starts=True)
        task, sent, directory = self._prepare()
        message = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        receipt = self._run(marker, directory, message, "补一句")
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["injects"],
                         [{"uuid": message, "delivered": True}])
        frames = self._frames(marker)
        steers = [frame for frame in frames if frame.get("method") == "turn/steer"]
        starts = [frame for frame in frames if frame.get("method") == "turn/start"]
        self.assertEqual(len(steers), 1)
        self.assertEqual(len(starts), 2)
        self.assertEqual(steers[0]["params"]["clientUserMessageId"], message)
        resent = starts[1]["params"]
        self.assertEqual(resent["threadId"], THREAD)
        self.assertEqual(resent["clientUserMessageId"], message)
        self.assertEqual(resent["input"], [{"type": "text", "text": "补一句"}])
        self.assertNotIn("expectedTurnId", resent)
        self.assertNotEqual(starts[0]["id"], starts[1]["id"])

    def test_two_rejects_remain_undelivered_without_third_send(self):
        # Both the original and the single resend are rejected. The turn then
        # completes and the process exits 0: two frames, delivered=false.
        marker = self._scenario(["reject", "reject"], complete_after_steers=True)
        task, sent, directory = self._prepare()
        message = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        receipt = self._run(marker, directory, message, "再补一句")
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertNotIn("error", receipt)
        self.assertEqual(receipt["injects"],
                         [{"uuid": message, "delivered": False}])
        steers = [frame for frame in self._frames(marker)
                  if frame.get("method") == "turn/steer"]
        self.assertEqual(len(steers), 2)
        self.assertEqual([frame["params"]["clientUserMessageId"] for frame in steers],
                         [message, message])
        self.assertNotEqual(steers[0]["id"], steers[1]["id"])

        # A success result without the userMessage echo is not delivery, and
        # the bounded quota does not resend it.
        marker = self._scenario(["result_only"], complete_after_steers=True)
        task, sent, directory = self._prepare()
        message = "abababab-abab-4bab-8bab-abababababab"
        receipt = self._run(marker, directory, message, "只回 result")
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["injects"],
                         [{"uuid": message, "delivered": False}])
        steers = [frame for frame in self._frames(marker)
                  if frame.get("method") == "turn/steer"]
        self.assertEqual(len(steers), 1)


if __name__ == "__main__":
    unittest.main()
