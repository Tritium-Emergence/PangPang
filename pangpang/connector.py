"""Outbound-only host connector: python -m pangpang.connector.

No listening port, LLM client or Bot state. Local installation configuration is
authoritative; cloud commands cannot supply argv, credentials or allowed roots.
"""
import argparse
import copy
import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .adapters_file import merge as merge_custom_adapters
from .env_compat import env_value, with_legacy_names
from .__main__ import ProcessLock
from .execution import Execution
from .runner import checked, receipt_proof_ok
from .store import Store, Fault, digest, encode, uid
from .vault import secure_directory


TERMINAL = {"succeeded", "failed", "cancelled"}


def cloud_connection_fault(exc):
    """Classify a failed coordinator connection without disabling TLS verification.

    Certificate failures stay distinct from a refused connection so the operator
    can fix trust. The advice never suggests skipping CA checks.
    """
    reason = exc.reason if isinstance(exc, urllib.error.URLError) and exc.reason is not None else exc
    text = str(reason).lower()
    if "hostname mismatch" in text or ("hostname" in text and "certificate" in text):
        return Fault(
            "connector_tls_hostname_mismatch",
            advice="证书主机名与协调器地址不一致。Connector 只接受 https:// 或本机 http://loopback。")
    if isinstance(reason, ssl.SSLCertVerificationError) or "certificate verify failed" in text:
        return Fault(
            "connector_tls_untrusted",
            advice="Python 证书校验失败。不要关闭 CA 验证，也不要改用明文。"
                   "把私有 CA 装进这台机器上 Python 实际使用的信任库；"
                   "macOS 上的 Python 通常不读钥匙串，需要 SSL_CERT_FILE。")
    return Fault(
        "cloud_connection_failed",
        advice="无法连接协调器。确认地址是 https:// 或本机 loopback，证书受信，且服务已在运行。")


def policy_digest(config, installation_id):
    installation = next(e for e in config["installations"] if e["id"] == installation_id)
    host = next(h for h in config["hosts"] if h["id"] == installation["host_id"])
    resources = [r for r in config.get("resources", []) if r["key"] in installation.get("resource_keys", [])]
    return digest({"installation": installation, "host": host, "resources": resources})


def authorization_digest(config, installation_id):
    """policy_digest without routing metadata.

    priority and use_when choose which installation is preferred. They are not
    part of what a run is allowed to execute, so they are removed before the
    same installation + host + resources digest. The grant record is stored
    outside the installation, so it is not hashed either.
    """
    installation = next(e for e in config["installations"] if e["id"] == installation_id)
    installation = {k: v for k, v in installation.items() if k not in ("priority", "use_when")}
    host = next(h for h in config["hosts"] if h["id"] == installation["host_id"])
    resources = [r for r in config.get("resources", []) if r["key"] in installation.get("resource_keys", [])]
    return digest({"installation": installation, "host": host, "resources": resources})


def execution_manifest(config, installation_id):
    """Operator-visible launch facts. Secret values are not included."""
    installation = next(e for e in config["installations"] if e["id"] == installation_id)
    host = next(h for h in config["hosts"] if h["id"] == installation["host_id"])
    command = [str(x)[:500] for x in list(installation.get("argv") or [])
               + list(installation.get("extra_argv") or [])][:32]
    roots = [str(r)[:4096] for r in (host.get("allowed_roots") or []) if isinstance(r, str)][:32]
    refs = installation.get("secret_refs") or {}
    credential_ids = sorted(str(v)[:200] for v in refs.values() if isinstance(v, str))
    return {"command": command, "cwd": str(installation.get("cwd") or "")[:4096],
            "allowed_roots": roots, "credential_ids": credential_ids}


def sanitize_manifest(value):
    """Keep only the manifest fields a device is allowed to report."""
    if not isinstance(value, dict):
        return None
    command = value.get("command")
    if (not isinstance(command, list) or len(command) > 32
            or not all(isinstance(x, str) and len(x) <= 500 for x in command)):
        command = []
    cwd = value.get("cwd") if isinstance(value.get("cwd"), str) and len(value.get("cwd")) <= 4096 else ""
    roots = value.get("allowed_roots")
    if (not isinstance(roots, list) or len(roots) > 32
            or not all(isinstance(x, str) and len(x) <= 4096 for x in roots)):
        roots = []
    ids = value.get("credential_ids")
    if (not isinstance(ids, list) or len(ids) > 32
            or not all(isinstance(x, str) and len(x) <= 200 for x in ids)):
        ids = []
    return {"command": command, "cwd": cwd, "allowed_roots": roots, "credential_ids": list(ids)}


class Connector:
    def __init__(self, config, data):
        self.config = copy.deepcopy(config)
        self.data = secure_directory(data)
        self.lock = ProcessLock(self.data / "connector.lock")
        self.store = Store(self.data / "connector.db")
        try:
            self.instance_id = self.store.get("identity", "device")["instance_id"]
        except Fault:
            self.instance_id = uid("device")
            self.store.put("identity", {"id": "device", "instance_id": self.instance_id})
        from .vault import Vault
        self.vault = Vault(self.data / "vault")
        self.config_revision = 0 # published settings/managed revision (G)
        try:
            managed = self.store.get("settings", "managed")
            self.config, self.config_revision = managed["config"], managed["revision"]
            config = self.config
        except Fault as error:
            if error.status != 404:
                raise
        merge_custom_adapters(self.data / "adapters.json")
        self.host_id = config["host_id"]
        hosts = config.get("hosts", [])
        if len(hosts) != 1 or hosts[0]["id"] != self.host_id or hosts[0].get("transport") != "local":
            raise Fault("connector_requires_one_local_host")
        if any(e["host_id"] != self.host_id for e in config.get("installations", [])):
            raise Fault("connector_installation_host_mismatch")
        try:
            self.execution = Execution(self.config, self.data,
                                       tolerate_unknown_adapters=self.config_revision != 0)
        except Exception:
            self.close()
            raise
        self.stop = threading.Event()
        endpoint = config["cloud_url"].rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise Fault("connector_requires_https_or_loopback")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise Fault("connector_requires_bare_origin")
        self.endpoint = endpoint + "/api/v1/connector/sync"
        self.credential_env = config.get("credential_env", "PANGPANG_DEVICE_TOKEN")
        blocked_env = with_legacy_names({self.credential_env, "PANGPANG_TOKEN"})
        for installation in self.execution.installations.values():
            installation["env_allowlist"] = [k for k in installation.get("env_allowlist", [])
                                            if k not in blocked_env]

    def close(self):
        self.store.close()
        self.vault.close()
        self.lock.file.close()

    def request(self, body):
        if self.config.get("credential_ref"):
            try:
                token = self.vault.get(self.config["credential_ref"])
            except Exception:
                raise Fault("device_credential_missing") from None
        else:
            token = env_value(self.credential_env, "")
        if len(token) < 32:
            raise Fault("device_credential_missing")
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token,
                   "X-PangPang-Host": self.host_id}
        request = urllib.request.Request(self.endpoint, data=encode(body).encode(), headers=headers)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(12 * 1024 * 1024 + 1)
            if len(raw) > 12 * 1024 * 1024:
                raise Fault("connector_response_limit")
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise cloud_connection_fault(exc) from None

    def reject_run(self, task, error):
        receipt = {"execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
                   "attempt_uuid": self._attempt_uuid(task),
                   "status": "failed", "process_tree_confirmed": True, "error": error, "artifacts": [],
                   "not_launched": "device_rejected"}
        self.store.put("local_run", {"id": task["execution_id"], "task": task, "status": "report", "receipt": receipt})

    def never_started(self, record, reason="confirmed_never_started"):
        task = record["task"]
        receipt = {"execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
                   "attempt_uuid": self._attempt_uuid(task),
                   "status": "cancelled", "process_tree_confirmed": True, "error": reason, "artifacts": [],
                   "not_launched": "never_started"}
        record.update(status="report", receipt=receipt, resolution=reason)
        self.store.put("local_run", record)
        return receipt

    @staticmethod
    def _attempt_uuid(task):
        """The attempt's uuid for a receipt: every legitimate v2 receipt
        path must carry it so cloud fencing accepts it. Prefer the merged task field;
        fall back to the contract (an early reject may fire before the merge)."""
        return task.get("attempt_uuid") or (task.get("contract") or {}).get("attempt_uuid")

    def _reject_workspace_conflict(self, task):
        """Device-side twin of Service._task_admissible. Uses the same
        shared canonical-workspace helpers so cloud admission and device execution
        agree. Only active local runs (launching/active) count. Rejects when the
        installation is at max_concurrency, or an active run holds an overlapping
        workspace with a conflicting access mode (write/write, write/read)."""
        from .store import canonical_workspace, workspace_overlaps
        host = self.execution.hosts[self.host_id]
        inst = self.execution.installations[task["installation_id"]]

        def ws(t):
            return canonical_workspace(host, t.get("cwd") or self.execution.installations[t["installation_id"]].get("cwd", ""))

        my_ws = ws(task)
        my_write = task.get("access_mode", "write") != "verified_read"
        # Status-scoped + paginated: a large terminal local_run backlog must never
        # hide a launching/active run (which would let us over-admit past capacity or
        # miss a workspace conflict). launching counts as busy — an unconfirmed launch
        # may already be running (unknown != idle).
        active = [r["task"] for r in self._active_runs()
                  if r.get("task", {}).get("execution_id") != task["execution_id"]]
        if sum(1 for t in active if t.get("installation_id") == task["installation_id"]) >= int(inst.get("max_concurrency", 1)):
            raise Fault("local_installation_at_capacity")
        for other in active:
            if workspace_overlaps(my_ws, ws(other)):
                if my_write or other.get("access_mode", "write") != "verified_read":
                    raise Fault("local_workspace_busy")

    def start(self, task):
        # apply the cloud's launch contract BEFORE anything else, so the
        # effective task carries the cloud-promised cwd/timeout_s/access_mode/
        # attempt_uuid/workspace_key instead of silently falling back to this device's
        # installation defaults. Merge first so the idempotency compare below sees the
        # same effective task on re-delivery. A missing/failed contract is rejected for
        # a v2 payload; a legacy (no-contract) payload keeps the old behaviour.
        # Single-entry device gate (SIS): schema + version + signature + payload bind
        # (inputs compared canonically so a legitimate artifact_id does not spuriously
        # mismatch). Called UNCONDITIONALLY so a downgraded payload (contract
        # stripped but the top-level min_contract_version marker present) is rejected
        # instead of falling through to the legacy path.
        from .store import canonical_workspace, verify_launch_contract
        err = verify_launch_contract(task)
        if err:
            self.reject_run(task, err)
            return {"rejected": True}
        contract = task.get("contract")
        if contract is not None:
            # Adopt every cloud-promised field as authoritative (no fallback to the
            # device installation defaults).
            task = dict(task)
            for k in ("cwd", "timeout_s", "access_mode", "attempt_uuid", "resource_keys", "workspace_key",
                      "supervision_floor", "required_outputs"):
                if contract.get(k) is not None:
                    task[k] = contract[k]
            task["contract_version"] = contract.get("contract_version")
        try:
            previous = self.store.get("local_run", task["execution_id"])
            if previous.get("task") != task:
                raise Fault("execution_spec_conflict")
            return {"accepted": True}
        except Fault as error:
            if error.status != 404:
                raise
        try:
            if task["host_id"] != self.host_id:
                raise Fault("wrong_device")
            self.execution.directory(self.execution.hosts[self.host_id], task["execution_id"])
            installation = self.execution.installations[task["installation_id"]]
            if task["installation_id"] in self.execution.unknown_adapters:
                raise Fault("unknown_adapter",
                            advice=self.execution.unknown_adapters[task["installation_id"]]["advice"])
            if installation.get("enabled") is not True:
                raise Fault("local_installation_disabled")
            live_policy = policy_digest(self.config, installation["id"])
            if task.get("policy_digest") != live_policy:
                raise Fault("local_policy_changed_probe_and_resubmit")
            # The cloud freezes the digest the operator approved. Re-check it
            # against this device's config and refuse before any process starts.
            frozen_auth = task.get("authorization_digest")
            if frozen_auth is not None and frozen_auth not in (
                    live_policy, authorization_digest(self.config, installation["id"])):
                raise Fault("local_policy_changed_probe_and_resubmit")
            # the cloud's workspace_key must equal what THIS device computes for
            # the launch cwd. Proves the cloud's workspace claim matches the real target
            # directory identity (catches an OS/case disagreement), not just that the
            # field survived transit. cwd defaults to the installation's when the
            # contract omitted it (legacy).
            if contract is not None and task.get("workspace_key") is not None:
                host = self.execution.hosts[self.host_id]
                cwd = task.get("cwd") or installation.get("cwd", "")
                device_ws = canonical_workspace(host, cwd)
                claimed = task["workspace_key"]
                if [device_ws[0], list(device_ws[1])] != [claimed[0], list(claimed[1])]:
                    raise Fault("local_workspace_identity_mismatch")
            # Device-side workspace invariant: the connector is the ground
            # truth for running processes, so it re-enforces the overlap rule rather
            # than serializing the whole machine. Disjoint / verified-read tasks run
            # concurrently; an overlapping writer is held until the active one clears.
            self._reject_workspace_conflict(task)
            roots = self.execution.hosts[self.host_id]["allowed_roots"]
            checked(installation["cwd"], roots)
            for item in task["inputs"]:
                checked(item["path"], roots)
            # `outputs` are NOT validated at launch: they are an optional declaration
            # of files the executor may produce, collected (and checked() before any
            # read/hash) at completion by the runner. A bare label or an invalid path
            # must not stop a valid task from starting — the runner tolerates it and
            # records an output warning. cwd/inputs above remain hard gates.
        except (Fault, KeyError, ValueError, OSError) as error:
            self.reject_run(task, error.code if isinstance(error, Fault) else "local_policy_rejected")
            return {"rejected": True}
        record = self.store.put("local_run", {"id": task["execution_id"], "task": task, "status": "launching",
                                               "launch_phase": "intent", "created": time.time()})
        try:
            started = self.execution.start(task)
        except Exception:
            record["uncertain"] = True
            self.store.put("local_run", record)
            return {"error": "local_launch_unconfirmed"}
        record.update(status="active", launch_phase="supervisor_confirmed", supervisor=started)
        self.store.put("local_run", record)
        return {"accepted": True}

    def execute(self, command):
        try:
            previous = self.store.get("local_command", command["id"])
            if previous["fingerprint"] != digest(command):
                raise Fault("command_replay_changed")
            if previous["status"] == "executing":
                previous.update(status="report", result={"error": "local_operation_unconfirmed"})
                self.store.put("local_command", previous)
            return
        except Fault as error:
            if error.status != 404:
                raise
        record = self.store.put("local_command", {"id": command["id"], "fingerprint": digest(command), "status": "executing"})
        action, body = command["action"], command["payload"]
        try:
            if command.get("expires") and command["expires"] < time.time():
                raise Fault("command_expired")
            if action in {"discover", "configure", "credential_install", "credential_delete"}:
                from .local_setup import LocalSetup
                setup = LocalSetup(self)
                if action == "discover":
                    result = setup.discover()
                elif action == "configure":
                    result = setup.configure(body)
                else:
                    result = setup.credential(body, delete=action == "credential_delete")
            elif action == "start":
                result = self.start(body)
            elif action == "cancel":
                run = self.store.get("local_run", body["execution_id"])
                directory = Path(self.execution.directory(self.execution.hosts[self.host_id], body["execution_id"]))
                if run["status"] == "launching" and not directory.exists():
                    self.never_started(run)
                    result = {"status": "confirmed_never_started"}
                elif run["status"] in {"launching", "active"}:
                    result = self.execution.cancel(run["task"])
                else:
                    result = {"status": "already_finished"}
            elif action == "resolve_run":
                run = self.store.get("local_run", body["execution_id"])
                resolution = body.get("resolution")
                if resolution not in {"confirmed_never_started", "confirmed_stopped"}:
                    raise Fault("invalid_operator_resolution")
                task = run["task"]
                # only a CONFIRMED terminal receipt is immutable (the cloud
                # may hold it — rewriting would trip receipt_conflict). Early-return just
                # for that case. A report/acknowledged run whose receipt is UNCONFIRMED
                # (e.g. the path where the cloud ACKs an unconfirmed receipt without
                # storing it) must STILL be resolvable — operator authority is the only
                # way it can reach a confirmed terminal, so fall through.
                _rc = run.get("receipt") or {}
                # Same pure predicate the cloud commits with:
                # a receipt the cloud rejected (and therefore never stored) is not
                # "already finalized" here either, so the operator path below still
                # produces an acceptable terminal instead of a dead-end "resolved".
                _confirmed_terminal = _rc.get("status") in TERMINAL and receipt_proof_ok(_rc, task.get("supervision_floor"))
                if run.get("status") in ("report", "acknowledged") and _confirmed_terminal:
                    result = {"resolved": True, "status": _rc.get("status", "already_finalized")}
                    record.update(status="report", result=result)
                    self.store.put("local_command", record)
                    return
                directory = Path(self.execution.directory(self.execution.hosts[self.host_id], body["execution_id"]))
                if resolution == "confirmed_never_started":
                    if directory.exists():
                        raise Fault("local_never_started_not_proven")
                    self.never_started(run)
                    result = {"resolved": True, "status": "confirmed_never_started"}
                elif not directory.exists():
                    self.never_started(run, "confirmed_stopped_no_runner")
                    result = {"resolved": True, "status": "confirmed_stopped_no_runner"}
                else:
                    receipt = self.execution.poll(task)
                    if receipt.get("status") in TERMINAL and receipt_proof_ok(receipt, task.get("supervision_floor")):
                        # A genuine confirmed terminal now exists. Attach a one-shot tail
                        # and record it (write directly — this is operator resolution
                        # finalizing an unconfirmed run, so the report/ack guard in
                        # _finalize_terminal_receipt must not suppress it).
                        if "tail" not in receipt:
                            try:
                                enriched = self.execution.poll(task, tail=True)
                                if enriched.get("tail") is not None:
                                    receipt = dict(receipt, tail=enriched["tail"])
                            except (Fault, TypeError):
                                pass
                        run.update(status="report", receipt=receipt,
                                   resolution="confirmed_stopped_already_terminal")
                        self.store.put("local_run", run)
                        result = {"resolved": True, "status": receipt["status"]}
                    else:
                        # The runner could not prove tree termination (e.g. weak-POSIX
                        # setsid escape) — cancelling and re-polling would loop forever.
                        # The operator supplied evidence cloud-side (resolve_task requires
                        # it), so operator authority IS the terminal proof: attempt cancel,
                        # then write an operator-authorized trusted terminal receipt (mirrors
                        # never_started and the local-transport operator path). It carries
                        # attempt_uuid so _receipt_matches_attempt fences it.
                        try:
                            self.execution.cancel(task)
                        except Exception:
                            pass
                        receipt = {"execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
                                   "attempt_uuid": self._attempt_uuid(task), "status": "cancelled",
                                   "process_tree_confirmed": True, "operator_resolution": "confirmed_stopped",
                                   "operator_evidence": body.get("operator_evidence", ""),
                                   "tail": "Operator resolved: confirmed_stopped", "artifacts": []}
                        run.update(status="report", receipt=receipt, resolution="operator_confirmed_stopped")
                        self.store.put("local_run", run)
                        result = {"resolved": True, "status": "cancelled"}
            elif action == "probe":
                if self.execution.installations[body["installation_id"]].get("enabled") is not True:
                    raise Fault("local_installation_disabled")
                result = self.execution.probe(body["installation_id"])
                result["policy_digest"] = policy_digest(self.config, body["installation_id"])
                result["authorization_digest"] = authorization_digest(self.config, body["installation_id"])
                result["manifest"] = execution_manifest(self.config, body["installation_id"])
            elif action == "file":
                if body["action"] not in {"hash", "read", "write", "receive", "list"}:
                    raise Fault("file_action_rejected")
                if body["action"] in {"write", "receive"} and self.config.get("allow_file_receive") is not True:
                    raise Fault("local_file_receive_disabled")
                result = self.execution.inspect_file(self.execution.hosts[self.host_id], body["path"],
                                                      body["action"], body.get("data"))
            elif action == "artifact_read":
                result = self.execution.read_artifact({"host_id": self.host_id,
                    "snapshot_execution_id": body["execution_id"], "digest": body["digest"]})
            else:
                raise Fault("device_action_rejected")
        except Exception as error:
            result = {"error": error.code if isinstance(error, Fault) else "local_operation_failed"}
        record.update(status="report", result=result)
        self.store.put("local_command", record)

    def _active_runs(self):
        for status in ("launching", "active"):
            cursor = None
            while True:
                page, cursor = self.store.find_page("local_run", status=status, cursor=cursor)
                yield from page
                if cursor is None:
                    break

    def collect(self):
        for record in self._active_runs():
            task = record["task"]
            directory = Path(self.execution.directory(self.execution.hosts[self.host_id], task["execution_id"]))
            if record["status"] == "launching" and not directory.exists():
                self.never_started(record)
                continue
            if record["status"] == "launching" and (directory / "started.json").exists():
                record.update(status="active", launch_phase="started_marker")
                self.store.put("local_run", record)
            try:
                receipt = self.execution.poll(task)
            except Fault:
                continue
            if receipt.get("execution_id"):
                if receipt.get("status") in TERMINAL and receipt_proof_ok(receipt, task.get("supervision_floor")):
                    # on the FIRST non-report -> report transition, read a
                    # bounded tail ONCE and merge it into this confirmed terminal
                    # receipt, then persist tail + report atomically. The tail IS the
                    # deliverable for text-answer tasks (no file). Done exactly once so
                    # a later re-transmit sends an identical receipt (broker receipt
                    # immutability); we never re-read a growing log or overwrite the
                    # confirmed receipt with a fresh (possibly transient) poll.
                    self._finalize_terminal_receipt(record, task, receipt)
                else:
                    record["receipt"] = receipt
                    self.store.put("local_run", record)

    def _finalize_terminal_receipt(self, record, task, receipt):
        """Attach a one-shot bounded tail to a confirmed terminal receipt and mark the
        run for report. Shared by collect() and resolve_run so the tail path has a
        single entry (SIS). Only merges the tail INTO the already-validated terminal
        receipt — a failed tail read leaves the receipt intact (never downgrades it).

A run that has ALREADY been reported/acknowledged is IMMUTABLE — the
        cloud may already hold its receipt, so rewriting it (even re-attaching a tail)
        would change the payload and trip receipt_conflict on the next upload. Only a
        run not yet in a report/acknowledged state may be finalized here."""
        if record.get("status") in ("report", "acknowledged"):
            return
        if "tail" not in receipt:
            try:
                enriched = self.execution.poll(task, tail=True)
                if enriched.get("tail") is not None:
                    receipt = dict(receipt, tail=enriched["tail"])
            except (Fault, TypeError):
                pass # tail is best-effort; the confirmed receipt still settles the join
        record.update(status="report", receipt=receipt)
        self.store.put("local_run", record)

    def tick(self):
        self.collect()
        reports = [{"id": r["id"], "result": r["result"]} for r in self.store.find("local_command", status="report", limit=1)]
        runs = self.store.find("local_run", status="report", limit=16)
        receipts = [r["receipt"] for r in runs]
        for record in self._active_runs():
            if len(receipts) >= 16:
                break
            if record["status"] in {"launching", "active"} and record.get("receipt"):
                receipts.append(record["receipt"])
        from .store import CONTRACT_VERSION
        response = self.request({"version": __version__, "instance_id": self.instance_id,
                                 "supported_contract_version": CONTRACT_VERSION,
                                 "reports": reports, "receipts": receipts})
        for identity in response.get("ack", []):
            record = self.store.get("local_command", identity)
            record["status"] = "acknowledged"
            self.store.put("local_command", record)
        for identity in response.get("receipt_ack", []):
            record = self.store.get("local_run", identity)
            # 'report' = a confirmed terminal receipt the cloud has now recorded.
            # 'active' = a terminal-but-UNCONFIRMED receipt (weak-POSIX tree could not
            # be proven): the cloud acks it after driving the task to unknown, so retire
            # it here too — otherwise the device re-transmits the same unconfirmed
            # receipt every tick. Resolution is operator-driven from unknown.
            if record["status"] in {"report", "active"}:
                record["status"] = "acknowledged"
                self.store.put("local_run", record)
        for command in response.get("commands", []):
            self.execute(command)

    def run(self):
        failures = 0
        while not self.stop.is_set():
            try:
                self.tick()
                failures = 0
                delay = 2
            except Exception as exc:
                failures = min(failures + 1, 5)
                delay = min(30, 2 ** failures) + random.random()
                if isinstance(exc, Fault) and exc.advice:
                    print("%s: %s" % (exc.code, exc.advice), flush=True)
                else:
                    print("Cloud sync unavailable; local execution and receipts retained.", flush=True)
            self.stop.wait(delay)


def paired_config(args):
    import getpass
    import platform
    from .vault import Vault
    directory = secure_directory(args.data)
    lock = ProcessLock(directory / "connector.lock")
    db = Store(directory / "connector.db")
    vault = Vault(directory / "vault")
    try:
        try:
            return db.get("settings", "managed")["config"]
        except Fault as error:
            if error.status != 404:
                raise
        cloud = (args.cloud or input("云端地址（HTTPS）: ")).rstrip("/")
        parsed = urlsplit(cloud)
        if parsed.path not in {"", "/"} or parsed.username or parsed.password or parsed.query or parsed.fragment or not (
                parsed.scheme == "https" or parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise Fault("connector_requires_bare_https_origin")
        workspace = Path(args.workspace or input("允许 Agent 使用的工作目录: ")).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise Fault("workspace_directory_required")
        from .runner import check_control_separation
        check_control_separation([workspace], [directory])
        try:
            identity = db.get("identity", "device")["instance_id"]
        except Fault:
            identity = uid("device")
            db.put("identity", {"id": "device", "instance_id": identity})
        code = getpass.getpass("网页生成的配对码（输入不显示）: ")
        payload = {"code": code, "instance_id": identity, "public_key": vault.public_key(),
                   "name": args.name or platform.node(), "os": platform.system()}
        request = urllib.request.Request(cloud + "/api/v1/pair", data=encode(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                result = json.loads(response.read(16384))
        except urllib.error.HTTPError as exc:
            # HTTPError is a URLError. A 4xx means the coordinator answered.
            if 400 <= exc.code <= 499:
                raise Fault(
                    "pairing_code_rejected",
                    advice="The coordinator is reachable, so the code is wrong or expired. "
                           "Generate a new one in the web UI.") from None
            fault = cloud_connection_fault(exc)
            if fault.code == "cloud_connection_failed":
                raise Fault("pairing_failed_check_code_and_cloud", advice=fault.advice) from None
            raise fault from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            fault = cloud_connection_fault(exc)
            if fault.code == "cloud_connection_failed":
                raise Fault("pairing_failed_check_code_and_cloud", advice=fault.advice) from None
            raise fault from None
        except Exception:
            raise Fault("pairing_failed_check_code_and_cloud") from None
        vault.set("device-token", result["token"])
        config = {"host_id": result["host_id"], "cloud_url": cloud, "credential_ref": "device-token",
                  "hosts": [{"id": result["host_id"], "transport": "local", "os": platform.system(),
                              "allowed_roots": [str(workspace)]}], "installations": [], "resources": []}
        db.put("settings", {"id": "managed", "config": config})
        print("配对成功。主机已显示在网页；请通过与胖胖对话连接本机 CLI。", flush=True)
        return config
    finally:
        vault.close(); db.close(); lock.file.close()


def main():
    parser = argparse.ArgumentParser(description="PangPang outbound host connector")
    parser.add_argument("--config")
    parser.add_argument("--cloud")
    parser.add_argument("--workspace")
    parser.add_argument("--name")
    parser.add_argument("--data", default=None)
    args = parser.parse_args()
    from .cli import DataLocationError, resolve_data_dir
    from .layout import is_source_checkout
    try:
        args.data = str(resolve_data_dir("connector", args.data, source_checkout=is_source_checkout()))
    except DataLocationError as error:
        parser.error(str(error))
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else paired_config(args)
        merge_custom_adapters(Path(args.data) / "adapters.json")
        connector = Connector(config, args.data)
    except Fault as error:
        print("%s: %s" % (error.code, error.advice or error.code), flush=True)
        return 1
    try:
        connector.run()
    except KeyboardInterrupt:
        connector.stop.set()
    finally:
        connector.close()


if __name__ == "__main__":
    main()
