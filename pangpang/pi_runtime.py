"""Pi worker transport. Python owns durable actions; Pi owns the live loop."""
import base64
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from .store import Fault, encode, uid
from .runner import command
from .logs import get_logger
from .pi_worker_command import _SELECTORS, worker_invocation

log = get_logger("model")


def _ipc_line(value):
    """One stdin frame for the pi worker.

    Canonical JSON, then U+2028 and U+2029 escaped so Node readline keeps the
    frame on one line. Decoding restores those characters.
    """
    text = encode(value).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return text + "\n"


def _usage_brief(usage):
    u = usage or {}
    return "reasoning=%s output=%s input=%s total=%s" % (
        u.get("reasoning"), u.get("output"), u.get("input"), u.get("totalTokens"))


def _safe_detail(text):
    # errorMessage 是网关原始错误文本，可能回显请求片段/上游响应体。截断 + 去换行/控制字符，
    # 保留诊断价值（错误码、状态）同时限制 PII/正文泄漏，也避免多行污染日志。
    if not text:
        return None
    flat = " ".join(str(text).split())
    return flat[:200] + ("…" if len(flat) > 200 else "")


def _model_error_code(error_message):
    """Distinguish a rate-limit (HTTP 429 / quota) and an auth failure (401/403, e.g.
    a wrong or unauthorized API key — first-run setup's most common mistake) from a
    generic model failure so diagnostics can count them separately and the
    UI can give an actionable hint. All remain retryable (a flaky gateway can emit a
    transient 401); the code only affects observability, so a heuristic on the
    gateway error text is enough."""
    text = str(error_message or "").lower()
    if "429" in text or "rate limit" in text or "rate_limit" in text or "too many requests" in text or "quota" in text:
        return "model_rate_limited"
    # 401/403 need token boundaries: bare substring matching misreads ports and ids
    # (ECONNREFUSED 127.0.0.1:4010, req_403abc, x401-large) as auth failures.
    if re.search(r"\b401\b|\b403\b", text) or "unauthorized" in text or "unauthenticated" in text \
            or "forbidden" in text or "invalid api key" in text or "incorrect api key" in text \
            or "api key not valid" in text or "authentication" in text:
        return "model_auth_failed"
    return "model_request_failed"


def _put_turn(store, t):
    """Persist a turn while preserving sweep-set flags (hard_timeout_requested /
    cancel_requested) that may have landed between the caller's read and this
    write — otherwise a killed worker would block instead of recovering."""
    live = store.get("turn", t["id"])
    for f in ("hard_timeout_requested", "cancel_requested"):
        if live.get(f) and not t.get(f):
            t[f] = live[f]
    return store.put("turn", t)


class PiModel:
    def __init__(self, service=None):
        self.service = service

    def credentials(self, profile):
        parsed = urlsplit(profile.get("endpoint", ""))
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise Fault("model_endpoint_requires_https_or_loopback")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise Fault("model_endpoint_must_not_contain_credentials")
        from .model_config import validate_profile
        validate_profile(profile)
        if profile.get("credential_ref"):
            try:
                return self.service.vault.get(profile["credential_ref"])
            except Exception:
                raise Fault("model_credential_missing") from None
        key = os.environ.get(profile.get("credential_env", ""), "")
        if profile.get("credential_env") and not key:
            raise Fault("model_credential_missing")
        return key

    def with_images(self, profile, history):
        """Turn the image refs a projection put on user messages into image data for
        a vision profile; strip them otherwise. A missing/corrupt copy is dropped,
        never fatal (the file is still reachable through read_evidence)."""
        vision = profile.get("vision") is True
        out = []
        for m in history:
            refs = m.get("images")
            if not refs:
                out.append(m)
                continue
            m = {k: v for k, v in m.items() if k != "images"}
            if vision:
                images = []
                for ref in refs:
                    if isinstance(ref.get("data"), str): # inline (setup probe), not a stored ref
                        images.append({"data": ref["data"], "mime": ref.get("mime")})
                        continue
                    raw = self.service.model_image(ref.get("sha")) if self.service else None
                    if raw is not None:
                        images.append({"data": base64.b64encode(raw).decode(), "mime": ref.get("mime")})
                if images:
                    m["images"] = images
            out.append(m)
        return out

    # None waits for a free model slot. Routing sets a finite bound on its own
    # instance so a busy local model degrades instead of blocking the send.
    _slot_timeout = None

    def exchange(self, profile, history, tools, handler, mode="run", budget=8, cancelled=None, on_proc=None,
                 on_delta=None, turn_id=None, attempt=0):
        """One model worker. run() and complete() both enter here, so they share
        one in-flight count with each other."""
        sched = getattr(self.service, "scheduler", None) if self.service is not None else None
        held = False
        if sched is not None:
            if not sched.acquire_model_call(self._slot_timeout):
                raise Fault("model_busy")
            held = True
        try:
            return self._exchange_impl(
                profile, history, tools, handler, mode, budget, cancelled, on_proc,
                on_delta, turn_id, attempt)
        finally:
            if held:
                sched.release_model_call()

    def _exchange_impl(self, profile, history, tools, handler, mode="run", budget=8, cancelled=None, on_proc=None,
                       on_delta=None, turn_id=None, attempt=0):
        secret = self.credentials(profile)
        env = {k: v for k, v in os.environ.items()
               if k in {"PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL"}}
        try:
            argv, cwd = worker_invocation()
        except (ValueError, OSError):
            raise Fault("pi_worker_invalid_binary_path") from None
        try:
            proc = subprocess.Popen(argv,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", env=env, cwd=cwd)
        except OSError:
            selected = any(name in os.environ for name in _SELECTORS)
            code = "pi_worker_binary_unavailable" if selected else "pi_worker_unavailable_run_npm_ci"
            raise Fault(code) from None
        # Register the worker so a hard-timeout sweep can kill it. The
        # callback returns a deregister thunk run in the finally below.
        _dereg = on_proc(proc) if on_proc else None
        inbox = queue.Queue(maxsize=8)
        def read():
            try:
                while True:
                    line = proc.stdout.readline(2 * 1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line.encode()) > 2 * 1024 * 1024:
                        inbox.put({"type": "failed"})
                        break
                    inbox.put(json.loads(line))
            except Exception:
                inbox.put({"type": "failed"})
            finally:
                inbox.put(None)
        threading.Thread(target=read, daemon=True).start()
        try:
            init = {"version": 1, "mode": mode, "profile": profile, "secret": secret,
                    "history": self.with_images(profile, history), "tools": tools, "budget": budget}
            # A worker that exits before reading stdin raises BrokenPipeError
            # (OSError) here. That is a dead worker, the existing pi_worker_failed.
            try:
                proc.stdin.write(_ipc_line(init))
                proc.stdin.flush()
            except OSError:
                raise Fault("pi_worker_failed") from None
            expected = 1
            # hard per-turn ceiling. A stuck Node worker is
            # bounded by this deadline; on expiry the proc is killed and the turn
            # raises model_timeout -> recover_turn. Configurable; capped at the
            # spec default so a bad config cannot disable the ceiling.
            hard = min(1800, int((self.service.config.get("pi_turn_hard_timeout_s", 1800) if self.service else 1800)))
            deadline = time.monotonic() + min(hard, profile.get("timeout_s", 120) * budget + 60)
            while True:
                if cancelled and cancelled():
                    raise Fault("turn_cancelled")
                try:
                    event = inbox.get(timeout=min(2, max(.01, deadline - time.monotonic())))
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        raise Fault("model_timeout")
                    continue
                if event is None or event["type"] == "failed":
                    raise Fault("pi_worker_failed")
                if event.get("version") != 1:
                    raise Fault("pi_protocol_error")
                # Streaming delta: a no-id one-way notification (must NOT carry an id
                # — the rpc loop below expects a strictly-increasing id per request).
                # Handled and skipped before the id/sequence check so it never desyncs.
                if event["type"] == "delta":
                    if "id" in event:
                        raise Fault("pi_protocol_error") # delta must be one-way, no id
                    if on_delta and isinstance(event.get("text"), str):
                        on_delta(turn_id, attempt, event["text"], int(event.get("rev", 0)))
                    continue
                if event["type"] == "finished":
                    return event
                if event.get("id") != expected:
                    raise Fault("pi_sequence_error")
                expected += 1
                result = handler(event)
                if event["type"] == "context" and isinstance(result, dict) and "history" in result:
                    result = dict(result, history=self.with_images(profile, result["history"]))
                proc.stdin.write(_ipc_line({"version": 1, "id": event["id"], "result": result}))
                proc.stdin.flush()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            # close() flushes, so the same dead pipe raises BrokenPipeError again
            # and would replace pi_worker_failed on the way out.
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.stdout.close()
            if _dereg:
                _dereg()

    def complete(self, profile, messages, tools, on_proc=None, cancelled=None):
        result = {}
        def handle(event):
            if event["type"] == "assistant":
                if event["stop"] in {"error", "aborted", "length"}:
                    log.warning("model failed (verify) stop=%s %s detail=%s",
                                event["stop"], _usage_brief(event.get("usage")), _safe_detail(event.get("errorMessage")))
                    raise Fault(_model_error_code(event.get("errorMessage")))
                result.update(message=event["message"], usage=event["usage"])
            return {}
        # cancelled lets a caller abandon a one-shot completion without orphaning
        # the Node worker: exchange() checks it at its poll loop and kills the
        # process in its finally (exchange's own deadline is ~timeout_s+60).
        self.exchange(profile, messages, tools, handle, mode="complete", budget=1,
                      on_proc=on_proc, cancelled=cancelled)
        return result["message"], result["usage"]

    def run(self, runtime, turn, profile):
        store = runtime.store
        if not turn["history"]:
            turn["history"] = runtime.context(turn)
            turn = store.put("turn", turn)
        last = next((m for m in reversed(turn["history"]) if m.get("tool_calls")), {})
        if last.get("tool_calls"):
            for call in last["tool_calls"]:
                runtime.durable_tool(turn, call)
            turn = store.get("turn", turn["id"])
            runtime.settle_batch_prose(turn)
        if any(m["role"] == "tool" and json.loads(m["content"]).get("yielded")
               for m in turn["history"][-8:]):
            turn["status"] = "done"; _put_turn(store, turn)
            runtime.service.reschedule_work(turn["work_id"]); return
        state = {"turn": turn, "claims": [], "answer": None}
        def release(uncertain=False):
            for claim, resource in reversed(state["claims"]):
                ok = False
                if not uncertain:
                    try:
                        ok = command(resource["release_argv"]) == 0
                    except Exception:
                        pass
                claim["status"] = "released" if ok else "unknown"
                store.put("claim", claim)
                if not ok and not uncertain:
                    raise Fault("model_resource_release_unconfirmed")
            state["claims"] = []
        def handle(event):
            t = state["turn"]
            if event["type"] == "context":
                t = store.get("turn", t["id"])
                if t.get("cancel_requested"):
                    raise Fault("turn_cancelled")
                if t["rounds"] >= runtime.service.config.get("turn_budget", 8):
                    raise Fault("turn_budget")
                final_round = runtime.final_round(t)
                tools = [] if final_round else runtime.tools_for(t)
                sizes = {"messages_bytes": len(encode(t["history"]).encode()),
                         "tools_bytes": len(encode(tools).encode())}
                serialized = sum(sizes.values())
                safety = int(profile.get("serialized_bytes_safety_limit", 256 * 1024))
                if serialized > safety:
                    t = runtime.compact(t, profile)
                    sizes["messages_bytes"] = len(encode(t["history"]).encode())
                    serialized = sum(sizes.values())
                    if serialized > safety:
                        raise Fault("context_budget")
                t.update(status="running", context_metrics=dict(sizes, serialized_bytes=serialized,
                    serialized_bytes_safety_limit=safety, context_window_tokens=profile.get("context_window_tokens"),
                    final_round=final_round, model_round=t["rounds"] + 1,
                    method="serialized byte safety limit; token window is independent"))
                state["turn"] = _put_turn(store, t)
                return {"history": runtime.model_history(t), "final_round": final_round}
            if event["type"] == "model_begin":
                log.debug("model call turn=%s model=%s max_tokens=%s tools=%s",
                          t.get("id"), profile.get("model"), profile.get("output_tokens", 8192),
                          len(runtime.tools_for(t)))
                # model pool is independent of executor pool.
                # Model profiles carry no resource_keys, so no executor lock is
                # acquired for a model call — a busy executor cannot freeze it.
                return {}
            if event["type"] == "assistant":
                if event["stop"] in {"error", "aborted"}:
                    log.warning("model failed stop=%s %s detail=%s",
                                event["stop"], _usage_brief(event.get("usage")), _safe_detail(event.get("errorMessage")))
                    raise Fault(_model_error_code(event.get("errorMessage")))
                release()
                if event["stop"] == "length":
                    log.warning("model truncated stop=length %s detail=%s",
                                _usage_brief(event.get("usage")), _safe_detail(event.get("errorMessage")))
                    raise Fault("model_response_truncated")
                t = store.get("turn", t["id"])
                if t.get("cancel_requested"):
                    raise Fault("turn_cancelled")
                message = event["message"]
                calls = message.get("tool_calls", [])
                final_round = runtime.final_round(t)
                prior_ids = {c["id"] for m in t["history"] for c in m.get("tool_calls", [])}
                if len(calls) > 8 or len({c["id"] for c in calls}) != len(calls) or any(c["id"] in prior_ids for c in calls):
                    raise Fault("invalid_tool_batch")
                t["rounds"] += 1; t["usage"] = event["usage"]
                message = runtime.accept_reply(t, message)
                if final_round and calls:
                    state["turn"] = _put_turn(store, t)
                    raise Fault("turn_budget")
                t["history"].append(message)
                state["turn"] = _put_turn(store, t)
                if not calls:
                    state["answer"] = message.get("content", "")
                else:
                    # Same contract as legacy Runtime.run: prose is persisted before
                    # the tools run only when every call is readonly. Any batch
                    # containing checkpoint_work waits until those tools commit.
                    # While link_work can still rebind, the prose stays in history.
                    runtime.maybe_precommit_readonly(state["turn"], calls, message.get("content"))
                return {}
            if event["type"] == "tool":
                log.debug("tool call turn=%s name=%s call_id=%s", t.get("id"), event.get("name"), event.get("call_id"))
                t = store.get("turn", t["id"])
                call = next((c for m in reversed(t["history"]) for c in m.get("tool_calls", [])
                             if c["id"] == event["call_id"]), None)
                if not call or call["function"]["name"] != event["name"] or json.loads(call["function"]["arguments"]) != event["args"]:
                    raise Fault("unjournalled_tool")
                result = runtime.durable_tool(t, call)
                state["turn"] = store.get("turn", t["id"])
                runtime.settle_batch_prose(state["turn"])
                return result
            raise Fault("pi_protocol_error")
        try:
            sched = getattr(runtime, "scheduler", None)
            tid, attempt = turn["id"], turn.get("run_attempt_id", 0)
            def on_proc(proc):
                if not sched:
                    return None
                sched.register_proc(tid, attempt, proc)
                return lambda: sched.deregister_proc(tid, attempt, proc)
            result = self.exchange(profile, turn["history"], runtime.tools_for(turn), handle,
                                   budget=runtime.service.config.get("turn_budget", 8),
                                   cancelled=lambda: bool(store.get("turn", turn["id"]).get("cancel_requested"))
                                                     or (sched.hard_timed_out(turn) if sched else False),
                                   on_proc=on_proc,
                                   on_delta=getattr(runtime.service, "record_delta", None), turn_id=tid, attempt=attempt)
            if result.get("waiting"):
                t = state["turn"]; t["status"] = "done"; _put_turn(store, t)
                runtime.service.reschedule_work(t["work_id"])
            elif state["answer"] is not None:
                runtime.service.finish_turn(state["turn"], state["answer"])
            else:
                raise Fault("model_empty_response")
        finally:
            release(uncertain=True)
            # Mark the live-delta buffer done AFTER the terminal persistence above
            # (finish_turn / done / raise) so a streaming client's follow-up refresh
            # reads the durable final message, not an empty inbox. Covers the raise
            # paths too (the outer scheduler set the turn blocked/queued by then).
            finish = getattr(runtime.service, "finish_delta", None)
            if finish:
                finish(tid, attempt)
