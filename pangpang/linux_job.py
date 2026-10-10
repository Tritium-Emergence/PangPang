"""Linux process-tree containment via a systemd --user transient scope (cgroup v2).

Mirrors windows_job.Job's interface (assign/alive/terminate/close) so runner's
`if job:` branches are platform-symmetric. The strong primitive is cgroup v2
`cgroup.kill`: writing "1" recursively SIGKILLs every process in the scope's
cgroup subtree. A child that calls setsid()/double-forks escapes the POSIX
process group but NOT the cgroup — verified empirically on the target host.

Threat model (RUNBOOK.md): this is the L0 "cooperative" tier. It proves the
scope's cgroup subtree is empty — covering an *accidental* tree (a CLI that exits
leaving tool children behind, even setsid ones). It does NOT defend against an adversarial same-UID payload
that actively asks the user manager to migrate itself into a sibling cgroup —
that is an L1/L2 concern and L0 already trusts the same-account executor.

Unlike a Windows Job Object (whose handle close auto-kills via KILL_ON_JOB_CLOSE),
a systemd scope does NOT die when the launching runner is SIGKILLed. RuntimeMaxSec
is set as a backstop so a leaked scope is reaped even if both runner and
coordinator vanish. close() actively kills+reaps rather than relying on the OS.
"""
import os
import subprocess
import time
from pathlib import Path

CGROUP_ROOT = "/sys/fs/cgroup"


def _user_env():
    # systemctl --user needs the caller's user bus. Inherit XDG_RUNTIME_DIR /
    # DBUS_SESSION_BUS_ADDRESS from the runner's env (present under a logged-in
    # or lingering user manager). No fabrication: if absent, systemd-run fails
    # and probe() will not have offered linux-cgroup-cooperative in the first place.
    return os.environ.copy()


def scope_argv(unit, *, runtime_max_sec=None):
    """The systemd-run prefix that wraps the real executor argv. --scope runs the
    payload in the foreground (stdin/stdout/exit-code pass through like a plain
    Popen — verified). Delegate=yes gives the scope its own cgroup subtree so
    cgroup.kill applies. --collect removes the unit's accounting when it dies."""
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            "--property=Delegate=yes", "--unit=" + unit]
    if runtime_max_sec:
        argv.append("--property=RuntimeMaxSec=%d" % int(runtime_max_sec))
    return argv


class LinuxJob:
    """cgroup v2 scope handle. Construct with the unit name used in scope_argv();
    call resolve() AFTER Popen so the systemd scope exists, then assign() is a
    no-op (the payload is already inside the scope by construction)."""

    def __init__(self, unit):
        self.unit = unit if unit.endswith(".scope") else unit + ".scope"
        self.cgroup = None # absolute /sys/fs/cgroup/... path, resolved post-spawn
        self.env = _user_env()
        self.resolve_error = None # diagnostic: why resolve() gave up

    def resolve(self, timeout=5.0):
        """Read the scope's real cgroup path from systemd (robust to slice layout
        changes — never hardcode user@UID.service/app.slice). Returns True once the
        cgroup dir exists. The runner MUST gate the real CLI launch on this
        (ready gate) so a fast-exiting or half-started payload cannot escape before
        we know its containment path."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "show", self.unit, "-p", "ControlGroup", "--value"],
                    capture_output=True, text=True, env=self.env, timeout=10)
            except (OSError, subprocess.SubprocessError) as e:
                last = "systemctl_exc:" + type(e).__name__ + ":" + str(e)[:120]
                time.sleep(0.05)
                continue
            out = r.stdout.strip()
            if out:
                path = Path(CGROUP_ROOT + out)
                if path.is_dir():
                    self.cgroup = path
                    return True
                last = "cgroup_dir_absent:" + out[:160]
            else:
                last = "empty_controlgroup rc=%d err=%s" % (r.returncode, (r.stderr or "").strip()[:120])
            time.sleep(0.05)
        self.resolve_error = last or "timeout_no_attempt"
        return False

    def assign(self, process):
        # No-op: systemd-run already placed the process (and its descendants) in
        # the scope cgroup. Present for interface symmetry with windows_job.Job.
        return None

    def alive(self):
        """True iff the scope cgroup still contains a process. Reads cgroup.events
        'populated' (recursive over the whole subtree — cgroup.procs is NOT, it
        omits child cgroups). A vanished cgroup dir means systemd already reaped an
        empty scope => provably empty => not alive."""
        if self.cgroup is None:
            return False
        try:
            for line in (self.cgroup / "cgroup.events").read_text().splitlines():
                if line.startswith("populated "):
                    return line.split()[1] == "1"
        except FileNotFoundError:
            return False
        except OSError:
            # Observation failure (not a liveness fact). Caller treats inability to
            # confirm empty as NOT-confirmed and fails closed.
            return True
        return True

    def terminate(self):
        """Recursively SIGKILL the entire scope subtree via cgroup.kill (kernel
        >=5.14). A setsid/double-forked descendant cannot survive this."""
        if self.cgroup is None:
            return
        try:
            (self.cgroup / "cgroup.kill").write_text("1")
        except FileNotFoundError:
            pass # already gone == already terminated
        except OSError:
            # Fallback to systemd stop if the kill file is briefly unavailable.
            subprocess.run(["systemctl", "--user", "stop", self.unit],
                           capture_output=True, env=self.env, timeout=10)

    def close(self):
        """Reap the scope. Unlike a Windows Job (auto-kill on handle close), a
        systemd scope persists after the runner dies, so we actively terminate +
        wait for empty before clearing systemd's record. Best-effort: never raise."""
        try:
            self.terminate()
            for _ in range(50):
                if not self.alive():
                    break
                time.sleep(0.1)
        except Exception:
            pass
        try:
            subprocess.run(["systemctl", "--user", "reset-failed", self.unit],
                           capture_output=True, env=self.env, timeout=10)
        except Exception:
            pass
