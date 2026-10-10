"""python -m pangpang --config config/runtime.json --data data-v2"""
import argparse
import copy
import json
import os
import sys
import threading
import time
from pathlib import Path

from .http import serve
from .runtime import Runtime
from .service import Service
from .vault import secure_directory
from .logs import configure_logging, get_logger
from .build_info import build_line, version_string
from .env_compat import env_value

# main() reads sys.platform, and tests replace this module's sys binding with a
# platform-only stand-in. Keep a reference to the real module for stdout.
_SYS = sys

log = get_logger("main")


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# First-run consent for auto-registering this machine as a local execution host.
# Same mechanism as the L0 process-group tier consent (pangpang.consent): a
# versioned risk text, recorded only by the operator, invalidated by bumping the
# version. Stored at the top level of the managed config because it gates the
# registration itself, before any host entry exists.
LOCAL_EXEC_CONSENT_KEY = "local_exec_consent"
LOCAL_EXEC_CONSENT_VERSION = "local-exec-consent-v1"
LOCAL_EXEC_RISK_TEXT = """\
本机执行器首次注册 —— 风险确认（{version}）

注册后这台机器成为 transport=local 执行主机：模型驱动的 CLI 将直接在
{workspace} 下以你的账号权限读写文件，不受沙箱约束；执行器环境会注入
所配置 CLI 的 API key。控制数据目录与 Vault 不在工作区内，但同账号进程
理论上可访问它们。

已阅读并接受以上风险？输入 y 确认注册本机执行器: """


class ProcessLock:
    def __init__(self, path):
        self.file = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise


def _self_check_field(value):
    """One column. None/empty is "-". Tabs and newlines would break the line protocol."""
    if not value:
        return "-"
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def render_self_check(service):
    """Format Service.effective_capabilities. Does not invent checks or wording.

    One tab-separated line per entry, in projection order:
    capability, state, reason, operator_action. Returns (text, exit_code).
    exit_code is 1 when any state is "blocked" or "unknown" (unknown has no
    authoritative observation, so the check must not report green). unconfigured
    is reported and still exits 0. Startup prints the same text and keeps serving,
    because the operator actions point at the Web UI.
    """
    lines = ["self-check:"]
    failed = False
    for entry in service.effective_capabilities():
        if entry["state"] in ("blocked", "unknown"):
            failed = True
        lines.append("\t".join((
            _self_check_field(entry["capability"]),
            _self_check_field(entry["state"]),
            _self_check_field(entry.get("reason")),
            _self_check_field(entry.get("operator_action")),
)))
    return "\n".join(lines) + "\n", (1 if failed else 0)


def write_self_check(text):
    out = _SYS.stdout
    if hasattr(out, "buffer"):
        out.buffer.write(text.encode("utf-8"))
        out.buffer.flush()
    else:
        out.write(text)


def install_routine_scheduler_gate(service):
    """Prevent the foreground/runtime and execution worker from sweeping routines concurrently."""
    routine_step = service.routine_step
    gate = threading.Lock()

    def guarded_routine_step():
        # routine_step scans all due routines, so a concurrent duplicate sweep can
        # safely be skipped. The next worker tick will retry after the active sweep.
        if not gate.acquire(blocking=False):
            return False
        try:
            return routine_step()
        finally:
            gate.release()

    service.routine_step = guarded_routine_step
    return guarded_routine_step


def main():
    parser = argparse.ArgumentParser(description="PangPang Bot Console")
    parser.add_argument("--version", action="version", version=version_string())
    parser.add_argument("--config", default="config/runtime.json")
    parser.add_argument("--data", default="data-v2")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--origin", help="Exact external origin behind a trusted HTTPS reverse proxy")
    parser.add_argument("--workspace", help="Executor workspace; defaults to a sibling <data>-workspace, separate from control data")
    parser.add_argument("--control-only", action="store_true",
                        help="Control plane only: never register or run a local executor (route/admit/start/recover all refuse transport=local). For server/public deployments where execution belongs on separately-identified devices (SECURITY L1).")
    parser.add_argument("--deployment-level", choices=["L0", "L1", "L2"], default=None,
                        help="Operator isolation declaration. On POSIX (macOS, Linux), omitting this flag for a personal start (not --control-only, no --origin) infers L0. On Windows the omitted level is UNDECLARED and only Windows Job Object supervision runs. L0 = personal self-use, same-account executor trusted; enables the L0 cooperative tiers (Linux cgroup scope when a systemd --user scope is available, otherwise the POSIX process group, same as macOS). L1/L2 = stronger isolation; only unconditional strong supervision is accepted.")
    parser.add_argument("--allow-local-exec", action="store_true",
                        help="Grant first-run consent to register this machine as a local execution host without an interactive prompt "
                             "(workspace root, non-sandboxed CLI, injected API keys). Equivalent to confirming the risk text in a terminal.")
    parser.add_argument("--check", action="store_true",
                        help="Print each effective capability's state and operator action, then exit. "
                             "Exit status is 1 when any capability is blocked or unknown. Does not listen. "
                             "Not a read-only query: first-run local host registration still runs (subject to first-run consent).")
    args = parser.parse_args()
    configure_logging()
    # launch.py and `python -m pangpang` omit this flag. A personal POSIX start
    # (macOS, Linux) infers L0. Linux uses the cgroup cooperative tier when a
    # systemd --user scope is available, otherwise the process group (same as
    # macOS). Consent is not written. --control-only, --origin, an explicit
    # level, and Windows stay as given. --origin is remote-access evidence
    # (RCB): do not infer a personal L0 from it. Tests replace this module's
    # sys with a platform stand-in, so a simulated win32 start stays undeclared
    # on a POSIX runner and a simulated POSIX start infers on a Windows runner.
    # sys.platform is the stand-in-able platform signal here; os.name is not,
    # because it also selects ProcessLock's locking backend.
    if (args.deployment_level is None and sys.platform != "win32"
            and not args.control_only and not args.origin):
        args.deployment_level = "L0"
        log.info("POSIX personal start omitted --deployment-level; using L0")
    if args.host not in LOOPBACK_HOSTS:
        parser.error("Coordinator must bind to loopback; expose it only through a trusted HTTPS reverse proxy")
    token = env_value("PANGPANG_TOKEN", "")
    if len(token) < 24:
        parser.error("Set PANGPANG_TOKEN (or legacy XIAN_PANGPANG_TOKEN) to at least 24 random characters")
    data = secure_directory(args.data)
    from .adapters_file import merge as merge_custom_adapters
    from .store import Fault
    try:
        merge_custom_adapters(data / "adapters.json")
    except Fault as error:
        _SYS.stderr.write("%s: %s\n" % (error.code, error.advice))
        return 1
    try:
        lock = ProcessLock(data / "coordinator.lock")
    except OSError:
        if not args.check:
            raise
        _SYS.stderr.write("协调器正在运行并持有进程锁。请先停止服务，再执行 --check。\n")
        _SYS.stderr.flush()
        return 1
    from .onboarding import empty_config
    config_file = Path(args.config)
    service = Service(json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else empty_config(), data,
                      control_only=args.control_only, deployment_level=args.deployment_level)
    service.config_path = Path(args.config).resolve()
    # A model cannot start a process, so registration cannot be triggered by a
    # bot. First-run registration additionally requires the operator's explicit
    # consent (interactive confirmation or --allow-local-exec); an already
    # registered local host needs no re-consent. Failure here must not block serving.
    if service.control_only:
        # Do NOT auto-register a local executor. If one is already in the DB, the
        # runtime hard gate (route/admit/start/recover) neutralises it; warn the
        # operator so they can remove it deliberately (we never auto-delete config).
        existing_local = [h for h in service.execution.hosts.values() if h.get("transport") == "local"]
        if existing_local:
            log.warning("control-only: %d local executor host(s) exist in config but are DISABLED "
                        "by the runtime gate; remove them deliberately if unwanted (not auto-deleted)",
                        len(existing_local))
        else:
            log.info("control-only: no local executor registered; execution runs on connector/ssh devices only")
    else:
        workspace = args.workspace or data.with_name(data.name + "-workspace")
        consented = (any(h.get("transport") == "local" for h in service.execution.hosts.values())
                     or service.config.get(LOCAL_EXEC_CONSENT_KEY) == LOCAL_EXEC_CONSENT_VERSION)
        if not consented:
            granted = args.allow_local_exec
            if not granted and _SYS.stdin.isatty():
                print(LOCAL_EXEC_RISK_TEXT.format(version=LOCAL_EXEC_CONSENT_VERSION, workspace=workspace))
                granted = input().strip().lower() in {"y", "yes"}
            if granted:
                candidate = copy.deepcopy(service.config)
                candidate[LOCAL_EXEC_CONSENT_KEY] = LOCAL_EXEC_CONSENT_VERSION
                service.commit_managed_config(candidate)
                consented = True
            else:
                log.warning("local executor not registered: first-run consent not granted "
                            "(confirm in an interactive terminal, or pass --allow-local-exec; "
                            "for a control-plane-only deployment use --control-only)")
        if consented:
            try:
                result = service.ensure_local_host(workspace, discover=True)
                if result.get("skipped"):
                    log.info("local execution host already registered: %s", result["host_id"])
                else:
                    found = ", ".join(result["installations"]) or "no local CLI found"
                    log.info("registered local execution host %s (workspace %s); discovered: %s",
                             result["host_id"], result["workspace"], found)
            except Exception as error:
                log.warning("local host auto-registration skipped: %s", error)
        # Existing installations are not rediscovered. L0 still has to replace a
        # stored macos-pgroup-postverify / posix-process-group marker, or the
        # log's "L0 enables the cooperative tiers" never reaches the probe row.
        if service.deployment_level == "L0":
            service.refresh_l0_local_probes()
    if service.deployment_level is None:
        log.warning("deployment level not declared: only Windows Job Object executors can run; "
                    "pass --deployment-level L0 to enable the L0 cooperative tiers")
    if not args.check:
        for warning in service.capability_warnings():
            log.warning("capability: %s", warning)
    text, status = render_self_check(service)
    write_self_check(text)
    _SYS.stderr.write(build_line())
    _SYS.stderr.flush()
    if args.check:
        # Same projection startup prints, after the first-run registration above.
        # This does not listen. It is not a read-only query. Release the DB
        # and the process lock before returning so a later start is not blocked.
        service.store.close()
        lock.file.close()
        return status
    install_routine_scheduler_gate(service)
    runtime = Runtime(service)
    stop = threading.Event()
    # the TurnScheduler owns turn dispatch (claim + run). Its Event wake
    # with a 1s safety tick replaces the old 0.15s busy poll.
    def worker():
        try:
            runtime.scheduler.run_forever()
        except Exception:
            log.exception("scheduler dispatcher stopped")
    def execution_worker():
        # event-driven control plane + cheap
        # reconciliation clock. No fixed 0.5s global blind scan. Mirrors
        # scheduler.run_forever's lost-notify-safe ordering: CLEAR the wake at the
        # top of each cycle (BEFORE doing work), drain to quiescence, then wait.
        # A notify that races the drain sets the Event again; because we only wait
        # after a full quiescent drain and the clear happened before that work, the
        # set survives to the wait (which returns immediately). A notify during the
        # wait wakes it. There is no window where a committed change is both missed
        # by the drain and erased by the clear.
        wake = service.execution_wake
        while not stop.is_set():
            wake.clear()
            progressed = True
            try:
                # Drain to quiescence: keep stepping while durable state changes
                # (a completion may unblock a dependent task admission, etc.).
                while progressed and not stop.is_set():
                    progressed = False
                    # sweep model hard-timeouts FIRST so a slow/remote task
                    # reconcile inside task_step (e.g. a 30s SSH poll) cannot delay
                    # killing a model turn that has already blown its hard timeout.
                    # Mitigation only — a timeout that fires DURING this pass's polls
                    # still waits for the next pass (full fix needs bounded parallel
                    # remote observation, tracked as follow-up).
                    progressed |= bool(runtime.scheduler.sweep_hard_timeouts())
                    progressed |= bool(service.task_step())
                    progressed |= bool(service.routine_step())
                    progressed |= bool(service.sweep_deadlines())
                    runtime.scheduler.notify() # settled deps / new tasks may free turns
            except Exception:
                log.exception("execution worker step failed")
            if stop.is_set():
                break # re-check before sleeping so shutdown is prompt
            try:
                now = time.time()
                timeout = service.EXECUTION_SAFETY_TICK_S
                deadline = service.next_execution_deadline(now)
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - now))
                if service.has_active_local_task():
                    timeout = min(timeout, service.RECONCILE_CADENCE_S)
            except Exception:
                log.exception("execution worker schedule failed")
                timeout = service.RECONCILE_CADENCE_S
            wake.wait(timeout=timeout)
    try:
        server = serve(service, token, args.host, args.port, args.origin)
    except OSError as error:
        # Bind is the only place a busy port surfaces. Name the address, the OS
        # error, and the existing --port flag, then exit. Workers have not
        # started, and the capability projection does not probe the port.
        _SYS.stderr.write("cannot bind %s:%s: %s; pass --port to use another port\n" % (args.host, args.port, error))
        _SYS.stderr.flush()
        service.store.close()
        lock.file.close()
        return 1
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=execution_worker, daemon=True).start()
    log.info("PangPang listening on %s", args.origin or f"http://{args.host}:{args.port}")
    # Serve in a thread so the main thread can run the graceful-shutdown sequence
    # with the HTTP API STILL UP: on stop we set draining, keep serving
    # so /api/v1/diagnostics reports draining:true, drain workers, THEN close the
    # server. serve_forever() blocking the main thread would hide the draining state.
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    try:
        while http_thread.is_alive():
            http_thread.join(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        service.notify_execution() # wake the execution worker out of its wait to exit
        runtime.scheduler.stop() # sets draining; diagnostics reflects it while served
        server.shutdown() # stop accepting new requests after the drain window
        server.server_close()
        lock.file.close()
        # Leave durable tasks for reconciliation; never clear claims on exit.


if __name__ == "__main__":
    raise SystemExit(main())
