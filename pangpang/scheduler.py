"""TurnScheduler: the single authority that claims and runs model turns.

Runs at model_max_concurrency=1 so behaviour stays serial (equivalent to
the old runtime.step poll-min-pick); flips the concurrency number and
adds priority-0 reservation / per-Bot caps / fairness aging. The scheduler owns:
  - the runnable filter (single authority for "may this turn run now")
  - the claim transaction (CAS status queued->running + boot_id + started_at)
  - priority/fairness ordering
  - an Event-based wake with a 1s safety tick (no busy polling)

Iron rules: the claim + all runnable predicates run inside ONE store transaction
(rule A); nothing external runs under the lock (rule B); notify happens after the
commit and on worker completion, never while holding Store.lock.
"""
import threading
import time

from .store import Fault
from .logs import get_logger

log = get_logger("scheduler")

TERMINAL_WORK = {"delivered", "failed", "cancelled", "linked"}
RETRYABLE_TURN_ERRORS = {"model_request_failed", "model_timeout", "model_rate_limited", "model_auth_failed"}


class TurnScheduler:
    def __init__(self, service, runtime):
        self.service = service
        self.runtime = runtime
        self.store = service.store
        self.config = service.scheduler_config
        self._wake = threading.Event()
        self._stop = threading.Event()
        # fixed worker pool sized to model_max_concurrency; the dispatcher
        # claims and submits, workers run turns in parallel. A locked registry maps
        # a running turn to the Node procs of its current attempt so a hard-timeout
        # sweep can kill them; deregistration is conditional on run_attempt_id.
        self._pool = None
        self._inflight = set() # turn_ids currently submitted/running
        self._procs = {} # turn_id -> {"attempt": int, "procs": set()}
        self._timed_out = set() # (turn_id, run_attempt_id) hard-timed-out
        self._reg_lock = threading.Lock()

    @property
    def draining(self):
        """Whether graceful shutdown has begun (dispatch stopped). Read
        by Service.diagnostics so the API can surface a draining coordinator."""
        return self._stop.is_set()

    def _bot_cap(self, bot):
        if bot.get("kind") == "main":
            return self.config["main_bot_max_active_turns"]
        return self.config["service_bot_max_active_turns"]

    def _bot_reserved(self, bot):
        if bot.get("kind") == "main":
            return self.config["main_bot_reserved_user_slots"]
        return 0

    # --- kill registry -----------------------------------------
    def register_proc(self, turn_id, attempt, proc):
        # Fence against shutdown INSIDE the lock so it cannot interleave with
        # stop()'s final kill sweep (which also holds _reg_lock): if stop() already
        # fired, refuse to register and kill immediately, so a proc spawned during
        # the shutdown race cannot survive the bounded drain.
        with self._reg_lock:
            if self._stop.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            slot = self._procs.get(turn_id)
            if slot is None or slot["attempt"] != attempt:
                slot = {"attempt": attempt, "procs": set()}
                self._procs[turn_id] = slot
            slot["procs"].add(proc)

    def deregister_proc(self, turn_id, attempt, proc):
        with self._reg_lock:
            slot = self._procs.get(turn_id)
            if slot and slot["attempt"] == attempt:
                slot["procs"].discard(proc)
                if not slot["procs"]:
                    self._procs.pop(turn_id, None)

    def kill_turn_procs(self, turn_id):
        with self._reg_lock:
            slot = self._procs.get(turn_id)
            procs = list(slot["procs"]) if slot else []
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    # --- wake -------------------------------------------------------------
    def notify(self):
        """Signal that scheduling state changed. Safe to call from any thread;
        MUST be called after the outer transaction commits (rule B), never under
        Store.lock."""
        self._wake.set()

    # --- runnable filter (single authority) -------------------------------
    def _running_count(self):
        return self.store.count("turn", status="running")

    def _work_running(self, work_id, exclude=None):
        running = self.store.running_turn_id(work_id)
        return running is not None and running != exclude

    def _is_runnable(self, turn, now):
        if turn.get("status") != "queued":
            return False
        if turn.get("not_before", 0) > now:
            return False
        try:
            work = self.store.get("work", turn["work_id"])
        except Fault:
            return False
        if work.get("status") in TERMINAL_WORK or work.get("cancel_requested"):
            return False
        if self._work_running(turn["work_id"], exclude=turn["id"]):
            return False
        # A normal continuation needs an empty dependency join. P0 user interrupts
        # and the narrow P1 active-work exchange are the only modes allowed to cross
        # it. Service creates that P1 lane only from a proven direct-delegator update
        # or the terminal reply to that exact solicited exchange.
        if turn.get("mode") == "interrupt":
            if int(turn.get("priority", 2)) != 0:
                return False
        elif turn.get("mode") == "work_exchange":
            if int(turn.get("priority", 2)) != 1:
                return False
        elif work.get("pending_dep_ids"):
            return False
        return True

    def _candidates(self, now):
        # Fetch newest-first so that if the queue ever exceeds the fetch cap, a
        # recent high-priority (P0) turn is not hidden behind older low-priority
        # ones. max_global_runnable_turns (128) keeps the real queue far
        # below this cap; the ordering is defence-in-depth.
        return [t for t in self.store.find(
                "turn", status="queued", newest=True, limit=1000, omit=("history",))
                if self._is_runnable(t, now)]

    def _last_started(self, bot_id):
        # Least-recently-started Bot fairness, derived from turn.started_at (no
        # separate scheduling store). One scalar MAX via
        # store.latest_started; turn blobs are not deserialized.
        return float(self.store.latest_started(bot_id) or 0)

    def _order_key(self, turn):
        return (int(turn.get("priority", 2)),
                self._last_started(turn.get("bot_id")),
                float(turn.get("created", 0)))

    def _next_deadline(self, now):
        """Nearest FUTURE not_before / dependency deadline, for the wake timeout.
        Overdue timestamps are excluded (they must not create a zero-time loop)."""
        soonest = None
        for t in self.store.find("turn", status="queued", limit=1000, omit=("history",)):
            nb = float(t.get("not_before") or 0)
            if nb > now:
                soonest = nb if soonest is None else min(soonest, nb)
        deadline = self.store.soonest_future("dependency", "pending", "deadline", now)
        if deadline is not None and (soonest is None or deadline < soonest):
            soonest = deadline
        return soonest

    # --- claim ------------------------------------------------------------
    def _resource_busy(self, turn):
        # model turns no longer hold executor resources, so
        # there is never a shared model-resource to gate on. Kept as a stable hook.
        return False

    def _running_turns(self):
        return [t for t in self.store.find("turn", status="running", limit=1000)]

    def _admission_ok(self, turn, running):
        """Priority-0 reservation + per-Bot cap. `running` is the list
        of currently-running turns (read in the same claim transaction). A P0 turn
        (priority 0) is exempt from the non-P0 reservation ceiling."""
        max_c = self.config["model_max_concurrency"]
        reserved = self.config["reserved_user_slots"]
        is_p0 = int(turn.get("priority", 2)) == 0
        # Global: non-P0 running may not exceed (max - reserved); the reserved slots
        # are only claimable by P0.
        if not is_p0:
            non_p0 = sum(1 for t in running if int(t.get("priority", 2)) != 0)
            if non_p0 >= max_c - reserved:
                return False
        # Per-Bot cap, with the main bot reserving slots for P0 too.
        bot = self.service.bot(turn["bot_id"])
        cap = self._bot_cap(bot)
        bot_running = [t for t in running if t.get("bot_id") == turn["bot_id"]]
        if len(bot_running) >= cap:
            return False
        if not is_p0:
            bot_reserved = self._bot_reserved(bot)
            bot_non_p0 = sum(1 for t in bot_running if int(t.get("priority", 2)) != 0)
            if bot_non_p0 >= cap - bot_reserved:
                return False
        return True

    def claim_one(self):
        """Pick the highest-priority runnable turn and atomically mark it running.
        Returns the claimed turn dict, or None if nothing is runnable/claimable.
        Never busy-loops on a stale candidate: a lost/capped claim
        removes that candidate from this pass and we recompute."""
        now = time.time()
        if self._running_count() >= self.config["model_max_concurrency"]:
            return None
        candidates = sorted(self._candidates(now), key=self._order_key)
        for cand in candidates:
            # Pre-claim resource gate: if a shared model resource is busy, leave the
            # turn queued (do NOT claim then fail it with model_resource_busy).
            if self._resource_busy(cand):
                continue
            try:
                with self.store.atomic():
                    # Re-check everything inside the transaction (rule A).
                    running = self._running_turns()
                    if len(running) >= self.config["model_max_concurrency"]:
                        return None
                    fresh = self.store.get("turn", cand["id"])
                    if not self._is_runnable(fresh, time.time()):
                        continue
                    # P0 reservation + per-Bot cap: a capped candidate is skipped so
                    # a later P0/other-Bot candidate can still be considered.
                    if not self._admission_ok(fresh, running):
                        continue
                    # run_attempt_id fences late writes from a killed/recovered prior
                    # attempt: terminal writes CAS on (running, run_attempt_id=current).
                    fresh.update(status="running", run_owner_boot_id=self.service.boot_id,
                                 run_attempt_id=fresh.get("run_attempt_id", 0) + 1,
                                 started_at=time.time())
                    claimed = self.store.put("turn", fresh)
                return claimed
            except Fault as error:
                # work_already_running (partial index) or state_conflict = lost
                # claim, not a turn failure. Skip this candidate and try the next.
                if error.code in {"work_already_running", "state_conflict"}:
                    continue
                raise
        return None

    # --- execution --------------------------------------------------------
    def _run_claimed(self, turn):
        """Run an already-claimed (running) turn. Transient model failures are
        retried by REQUEUING the turn with a durable not_before backoff — the worker
        is released immediately instead of sleeping (a sleeping worker would burn a
        reserved model slot and make not_before/retry_count dead fields). The
        scheduler re-claims the turn once not_before passes."""
        from .runtime import _retry_delays
        retry_delays = _retry_delays(self.service.config)
        log.info("turn start id=%s bot=%s work=%s priority=%s",
                 turn["id"], turn.get("bot_id"), turn.get("work_id"), turn.get("priority", 2))
        try:
            self.runtime.run(self.store.get("turn", turn["id"]))
            done = self.store.get("turn", turn["id"])
            if self._stamp_metrics(done):
                self.store.put("turn", done)
            log.info("turn done id=%s status=%s rounds=%s", turn["id"], done.get("status"), done.get("rounds"))
            return
        except Exception as error:
            code = error.code if isinstance(error, Fault) else "runtime_error"
            # A non-Fault exception (runtime_error) has no business meaning — it is a
            # bug or an environment fault. Log its traceback so it is diagnosable; a
            # bare "turn blocked error=runtime_error" with no stack is a dead end.
            if not isinstance(error, Fault):
                log.exception("turn runtime_error id=%s work=%s", turn["id"], turn.get("work_id"))
            # A shared model resource that got busy AFTER our pre-claim gate is a
            # lost race, not a turn failure: requeue (leave the work runnable).
            # But only when the resource is still cleanly acquirable — if a claim
            # went unknown/quarantined (fail-closed), requeue would loop forever
            # against the gate, so block instead.
            if code == "model_resource_busy":
                if not self._resource_busy(turn):
                    current = self.store.get("turn", turn["id"])
                    current["status"] = "queued"
                    self.store.put("turn", current)
                    log.info("turn requeued id=%s resource busy", turn["id"])
                    return
                # else fall through to generic blocking (do not spin against a
                # quarantined slot; leaving it running would consume the only slot)
            current = self.store.get("turn", turn["id"])
            # A hard-timeout kill routes to deterministic crash recovery,
            # not a retry/block. Checked before the retry branch so a killed turn is
            # never requeued for a plain model retry.
            if self.hard_timed_out(turn):
                self.clear_hard_timeout(turn)
                try:
                    self.service.recover_turn(current)
                except Exception:
                    log.exception("recover_turn (hard timeout) failed id=%s", turn["id"])
                return
            # Transient model failure: retry via durable requeue + not_before backoff,
            # releasing the worker now. model_timeout is capped at ONE retry (frozen);
            # other transient codes use the full backoff schedule.
            done_count = int(current.get("retry_count", 0))
            budget = 1 if code == "model_timeout" else len(retry_delays)
            if code in RETRYABLE_TURN_ERRORS and done_count < budget:
                delay = retry_delays[min(done_count, len(retry_delays) - 1)] if retry_delays else 0
                current.update(status="queued", retry_count=done_count + 1,
                               not_before=time.time() + delay, error=None,
                               run_owner_boot_id=None)
                self.store.put("turn", current)
                log.warning("turn retry-requeued id=%s error=%s attempt=%s/%s not_before=+%ss",
                            turn["id"], code, done_count + 1, budget, delay)
                self.notify()
                return
            current.update(status="cancelled" if code == "turn_cancelled" else "blocked", error=code)
            self._stamp_metrics(current)
            self.store.put("turn", current)
            # A top-level user turn that exhausted its per-turn tool budget is handed
            # off to a fresh executor instead of dying silently (transparent recovery).
            # Best-effort: a failed handoff must never break the block settlement below.
            if code == "turn_budget":
                try:
                    self.service.try_recovery_handoff(current)
                except Exception:
                    log.exception("try_recovery_handoff failed id=%s", turn["id"])
            try:
                self.service.fail_turn_dependency(current)
            except Exception:
                log.exception("fail_turn_dependency failed id=%s", turn["id"])
            try:
                self.service.reschedule_work(current["work_id"])
            except Exception:
                log.exception("reschedule_work failed id=%s", turn["id"])
            if code == "turn_cancelled":
                log.info("turn cancelled id=%s work=%s", turn["id"], turn.get("work_id"))
            else:
                log.warning("turn blocked id=%s error=%s work=%s", turn["id"], code, turn.get("work_id"))
            return

    @staticmethod
    def _stamp_metrics(turn):
        """Record scheduling observability on any terminal turn.
        Returns True if it mutated the turn (caller persists)."""
        if turn.get("started_at") and turn.get("status") in {"done", "blocked", "cancelled"} and "finished_at" not in turn:
            turn["finished_at"] = time.time()
            turn["queue_wait_ms"] = int((turn["started_at"] - turn.get("created", turn["started_at"])) * 1000)
            turn["model_duration_ms"] = int((turn["finished_at"] - turn["started_at"]) * 1000)
            return True
        return False

    def run_one_sync(self):
        """Claim and run a single turn synchronously; return True if one ran.
        This is the test/step() entry point (concurrency=1)."""
        turn = self.claim_one()
        if turn is None:
            return False
        self._run_claimed(turn)
        return True

    # --- dispatcher loop --------------------------------------------------
    def _submit(self, turn):
        """Run a claimed turn on the worker pool (concurrency>1) or inline
        (concurrency==1). Tracks inflight so the dispatcher does not over-claim
        beyond the pool while workers are mid-turn."""
        def work():
            try:
                self._run_claimed(turn)
            except Exception:
                log.exception("worker _run_claimed failed id=%s", turn["id"])
            finally:
                with self._reg_lock:
                    self._inflight.discard(turn["id"])
                self.notify() # a freed slot may let another turn claim
        if self._pool is None:
            work()
        else:
            with self._reg_lock:
                self._inflight.add(turn["id"])
            self._pool.submit(work)

    def run_forever(self):
        import concurrent.futures
        max_c = self.config["model_max_concurrency"]
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_c) if max_c > 1 else None
        while not self._stop.is_set():
            claimed = False
            # Only claim while there is pool headroom (running + submitted-not-yet-
            # running < max). The claim txn re-counts running authoritatively; this
            # just avoids spinning ahead of the pool.
            with self._reg_lock:
                inflight = len(self._inflight)
            if self._pool is None or inflight < max_c:
                try:
                    turn = self.claim_one()
                except Exception:
                    log.exception("scheduler claim failed")
                    turn = None
                if turn is not None:
                    self._submit(turn)
                    claimed = True
            if claimed:
                continue # keep claiming until nothing runnable / pool full
            # Clear BEFORE recomputing/waiting so a notify that races the recheck
            # is not erased (lost-notify window). The 1s safety tick is a backstop.
            self._wake.clear()
            now = time.time()
            deadline = self._next_deadline(now)
            timeout = 1.0
            if deadline is not None:
                timeout = max(0.0, min(1.0, deadline - now))
            self._wake.wait(timeout=timeout)

    def sweep_hard_timeouts(self):
        """Mark + kill running turns past the hard timeout. The timeout
        mark lives in an in-memory set keyed by (turn_id, run_attempt_id) — NOT on
        the turn object — so a concurrent pi_runtime whole-object write cannot erase
        it. Only flags + kills; the owning worker's _run_claimed sees the mark and
        routes to recover_turn (no concurrent recovery of a live turn). If the
        coordinator crashes, recover() handles the orphan on next boot regardless.
        Runs on the execution_worker thread so it fires even while the pool is busy."""
        hard = int(self.service.config.get("pi_turn_hard_timeout_s", 1800))
        now = time.time()
        for t in self.store.find("turn", status="running", limit=1000):
            if t.get("run_owner_boot_id") != self.service.boot_id:
                continue # previous-boot orphan; recover() handles it
            started = float(t.get("started_at") or now)
            key = (t["id"], t.get("run_attempt_id", 0))
            if now - started <= hard or key in self._timed_out:
                continue
            with self._reg_lock:
                self._timed_out.add(key)
            self.kill_turn_procs(t["id"])

    def hard_timed_out(self, turn):
        with self._reg_lock:
            return (turn["id"], turn.get("run_attempt_id", 0)) in self._timed_out

    def clear_hard_timeout(self, turn):
        with self._reg_lock:
            self._timed_out.discard((turn["id"], turn.get("run_attempt_id", 0)))

    def stop(self, drain_s=None):
        """Graceful shutdown: stop claiming, let active workers drain up
        to drain_s, then kill any still-running Node procs so shutdown is bounded
        (a worker stuck in a long model call must not block exit indefinitely).
        queued/inbox/dependencies are left untouched; external tasks are not
        cancelled — the next boot recovers running turns."""
        self._stop.set()
        self._wake.set()
        if drain_s is None:
            drain_s = int(self.service.config.get("shutdown_drain_s", 20))
        deadline = time.time() + max(0, drain_s)
        while time.time() < deadline:
            with self._reg_lock:
                if not self._inflight:
                    break
            time.sleep(0.1)
        # Bound the wait: kill whatever is still running, then reap. A worker in a
        # retry sleep can spawn a fresh proc after a kill, so kill repeatedly for a
        # bounded grace window rather than blocking on shutdown(wait=True) forever.
        # Runs even at concurrency=1 (no pool) so a registered inline proc is killed.
        self._kill_deadline = time.time() + max(1, int(self.service.config.get("shutdown_kill_grace_s", 5)))
        while time.time() < self._kill_deadline:
            with self._reg_lock:
                stuck = list(self._procs.keys())
                inflight = bool(self._inflight)
            for tid in stuck:
                self.kill_turn_procs(tid)
            if not inflight and not stuck:
                break
            time.sleep(0.1)
        if self._pool is not None:
            self._pool.shutdown(wait=False)
