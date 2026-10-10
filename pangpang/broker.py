"""Durable outbound-connector mailbox. Device credentials never authorize Web APIs."""
import hashlib
import hmac
import os
import re
import time
from .store import Fault, digest, uid

TERMINAL = {"succeeded", "failed", "cancelled"}


def global_profile(config):
    profiles = config.get("models", [])
    if len(profiles) != 1 or not profiles[0].get("id"):
        raise Fault("exactly_one_global_model_required", 400)
    profile = profiles[0]
    if config.get("default_model", profile["id"]) != profile["id"]:
        raise Fault("global_model_mismatch", 400)
    return profile


class Broker:
    def __init__(self, service):
        self.service, self.store = service, service.store
        # Liveness is a volatile observation, not durable state (N1): a
        # heartbeat every ~2s must not commit a device row (fsync + SSE event).
        # Published only after the sync transaction commits; empty after restart,
        # so a device reads offline until its first sync (~2s).
        self._last_seen = {}

    def authenticate(self, host_id, authorization):
        host = self.service.execution.hosts.get(host_id, {})
        if host.get("transport") != "connector":
            raise Fault("device_unauthorized", 401)
        expected = os.environ.get(host.get("credential_env", ""), "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        valid = hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), host["credential_hash"]) if host.get("credential_hash") else len(expected) >= 32 and hmac.compare_digest(authorization, "Bearer " + expected)
        if not supplied or not valid:
            raise Fault("device_unauthorized", 401)
        try:
            if self.store.get("device", host_id).get("revoked"):
                raise Fault("device_revoked", 403)
        except Fault as error:
            if error.status != 404:
                raise
        return host_id

    def online(self, host_id):
        try:
            device = self.store.get("device", host_id)
            return not device.get("revoked") and time.time() - self._last_seen.get(host_id, 0) < 90
        except Fault:
            return False

    def devices(self):
        # Presence is advisory as of this snapshot; routing re-checks online().
        return [dict(d, last_seen=self._last_seen.get(d["id"], 0), online=self.online(d["id"]))
                for d in self.store.find("device")]

    def revoke(self, host_id):
        try:
            device = self.store.get("device", host_id)
        except Fault:
            device = {"id": host_id}
        device["revoked"] = True
        return self.store.put("device", device)

    @staticmethod
    def command_id(host_id, action, key):
        """Deterministic identity of a keyed command (a task's start is keyed by its
        execution_id, and always has been), so its state is an exact lookup."""
        return "cmd-" + digest([host_id, action, key])

    def enqueue(self, host_id, action, payload, key=None):
        identity = self.command_id(host_id, action, key) if key else uid("cmd")
        with self.store.atomic():
            try:
                old = self.store.get("command", identity)
                if old["payload"] != payload:
                    raise Fault("command_identity_conflict")
                return old
            except Fault as error:
                if error.status != 404:
                    raise
            durable = {"start", "cancel", "resolve_run", "configure", "credential_install", "credential_delete"}
            return self.store.put("command", {"id": identity, "host_id": host_id, "action": action,
                "payload": payload, "status": "pending", "created": time.time(),
                "expires": None if action in durable else time.time() + 120})

    def request(self, host_id, action, payload):
        from .store import assert_no_txn_io
        assert_no_txn_io("broker.request") # polls with sleep; never under a txn
        if not self.online(host_id):
            raise Fault("connector_offline")
        cmd = self.enqueue(host_id, action, payload)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = self.store.get("command", cmd["id"])
            if current["status"] == "done":
                result = current["result"]
                if result.get("error"):
                    raise Fault(result["error"])
                return result
            time.sleep(.1)
        raise Fault("connector_operation_pending")

    def start(self, task):
        # send the explicit launch contract + its digest so the device can
        # verify what the cloud promised (cwd/timeout/access_mode/workspace/inputs/
        # attempt_uuid), instead of silently falling back to its own installation
        # defaults.
        from .store import launch_contract, contract_digest
        # capability handshake: a task the cloud stamped v2 may ONLY be sent to
        # a device that has PROVEN (via sync) it understands v2. If the device has not
        # yet proven it (never synced, or a legacy build), HOLD — return the retryable
        # 'starting' status so _try_start re-queues the launch with backoff instead of
        # sending a v2 contract a legacy device would ignore (then reply uuid-less and
        # be rejected forever). We never silently downgrade a v2 task to legacy: the
        # contract protections are the point. A genuinely legacy device simply never
        # runs a v2 task (fail-closed; its deadline eventually quarantines the task).
        need = int(task.get("contract_version", 0))
        if need >= 2:
            try:
                device = self.store.get("device", task["host_id"])
                proven = int(device.get("supported_contract_version", 0))
            except Fault:
                proven = 0
            if proven < need:
                return {"status": "starting"}
        payload = {k: task[k] for k in ("id", "execution_id", "spec_digest", "policy_digest",
                   "host_id", "installation_id", "inputs", "outputs", "prompt")}
        payload["contract"] = launch_contract(task)
        payload["contract_digest"] = contract_digest(task)
        # Top-level anti-downgrade marker: a sibling of `contract`, so stripping
        # `contract` alone still leaves this to force the device to demand it. The device
        # rejects a payload whose marker >= 2 but has no contract.
        payload["min_contract_version"] = payload["contract"]["contract_version"]
        # Top-level attempt_uuid (sibling of contract) so a device REJECT receipt (built
        # from the payload before contract adoption) still carries the fencing identity
        # even under a stripped-contract downgrade — the cloud can then fence + settle the
        # rejection cleanly instead of leaving the task in receipt_identity_mismatch.
        if task.get("attempt_uuid") is not None:
            payload["attempt_uuid"] = task["attempt_uuid"]
        # Frozen admitted supervision, also signed inside the v3 contract.
        # The device runner refuses a weaker tier and judges its own receipt against
        # it, same as the cloud. Only a device that proved v3 receives it (hold above).
        if task.get("supervision_floor"):
            payload["supervision_floor"] = task["supervision_floor"]
        # the HARD required_outputs deliverable contract. Bound inside the
        # signed v4 launch contract; mirrored top-level (like outputs/supervision_floor)
        # so a top-level tamper is caught by verify_launch_contract's payload bind.
        # Present ONLY for a v4 task (one that declared required_outputs) — a plain v3
        # task sends nothing here and the device's bind sees None==None (compat).
        if task.get("required_outputs"):
            payload["required_outputs"] = task["required_outputs"]
        # Launch authority is checked against the AUTHORITATIVE task in the same
        # transaction as the enqueue: a caller holding a stale
        # 'starting' snapshot must not enqueue after the task was cancelled, went
        # unknown, or was resolved — resolve's "no start command" proof then also
        # means "none can follow". Only a still-'starting' task of THIS attempt may
        # enqueue; resolve (which needs 'unknown') serializes on the same lock.
        with self.store.atomic():
            try:
                current = self.store.get("task", task["id"])
            except Fault as error:
                if error.status != 404:
                    raise
                current = {}
            if (current.get("status") != "starting" or current.get("execution_id") != task["execution_id"]
                    or current.get("attempt_uuid") != task.get("attempt_uuid")):
                return {"status": "start_withdrawn"}
            cmd = self.enqueue(task["host_id"], "start", payload, task["execution_id"])
        return {"command_id": cmd["id"], "transport": "connector"}

    def cancel(self, task):
        self.enqueue(task["host_id"], "cancel", {"execution_id": task["execution_id"]}, task["execution_id"])
        return {"status": "cancel_requested"}

    def resolve(self, task, resolution, evidence):
        # A start that has never been delivered can be safely superseded. Once
        # delivered, we keep it and require the device-side resolution to prove
        # terminal state; this avoids a delayed start racing a stale operator check.
        with self.store.atomic():
            # Exact lookup of THIS attempt's start command: a
            # bounded history scan that misses it must never read as "never sent".
            # Commands are never deleted, so a 404 proves the start was never enqueued.
            delivered = False
            try:
                cmd = self.store.get("command", self.command_id(task["host_id"], "start", task["execution_id"]))
            except Fault as error:
                if error.status != 404:
                    raise
                cmd = None
            if cmd is None:
                pass
            elif cmd["status"] == "pending":
                cmd.update(status="done", result={"superseded_by_operator_resolution": True})
                self.store.put("command", cmd)
            else:
                result = cmd.get("result") or {}
                never_sent = (result.get("superseded_by_operator_resolution")
                              or result.get("error") == "command_expired")
                delivered = not (cmd["status"] == "done" and never_sent)
            if not delivered:
                # The start never left the cloud (e.g. held for a device that never
                # proved the contract version): the cloud itself knows it never ran,
                # and an older device could not answer for a run it never saw.
                return {"status": "never_delivered"}
            payload = {"execution_id": task["execution_id"], "resolution": resolution,
                       "operator_evidence": evidence}
            cmd = self.enqueue(task["host_id"], "resolve_run", payload, task["execution_id"] + ":" + resolution)
        return {"status": "resolution_requested", "command_id": cmd["id"]}

    def start_never_delivered(self, task):
        """Read-only, side-effect-free classification: did THIS attempt's start
        command provably never reach the device? Mirrors resolve()'s
        never_delivered branch (404 = never enqueued; done + command_expired =
        expired while pending; done + superseded_by_operator_resolution) but
        never mutates state. A pending or delivered command is NOT eligible:
        sync may hand a pending command to the device concurrently, and a
        delivered one must never be presumed unexecuted. Returns
        (never_delivered, detail)."""
        command_id = self.command_id(task["host_id"], "start", task["execution_id"])
        with self.store.atomic():
            try:
                cmd = self.store.get("command", command_id)
            except Fault as error:
                if error.status != 404:
                    raise
                return True, {"start_command_id": command_id,
                              "start_command_state": "never_enqueued"}
            if cmd["status"] != "done":
                return False, None
            result = cmd.get("result") or {}
            if result.get("superseded_by_operator_resolution"):
                return True, {"start_command_id": command_id,
                              "start_command_state": "superseded_before_delivery"}
            if result.get("error") == "command_expired":
                return True, {"start_command_id": command_id,
                              "start_command_state": "expired_before_delivery"}
            return False, None

    def poll(self, task):
        try:
            return self.store.get("receipt", task["execution_id"])["payload"]
        except Fault as error:
            if error.status != 404:
                raise
        if not self.online(task["host_id"]):
            raise Fault("connector_offline")
        return {"status": "awaiting_receipt"}

    def sync(self, host_id, body):
        instance_id = body.get("instance_id", "")
        if not isinstance(instance_id, str) or not re.fullmatch(r"device-[a-f0-9]{32}", instance_id):
            raise Fault("device_instance_required", 400)
        reports, receipts = body.get("reports", []), body.get("receipts", [])
        if not isinstance(reports, list) or not isinstance(receipts, list) or len(reports) > 4 or len(receipts) > 16:
            raise Fault("device_batch_limit", 400)
        ack, receipt_ack, reconcile = [], [], []
        with self.store.atomic():
            if self.service.execution.hosts.get(host_id, {}).get("transport") != "connector":
                raise Fault("device_unauthorized", 401)
            try:
                device = self.store.get("device", host_id)
                if device.get("revoked"):
                    raise Fault("device_revoked", 403)
                if device.get("instance_id") and device["instance_id"] != instance_id:
                    raise Fault("device_instance_mismatch", 403)
            except Fault as error:
                if error.status != 404:
                    raise
            for report in reports:
                cmd = self.store.get("command", report["id"])
                if cmd["host_id"] != host_id:
                    raise Fault("command_host_mismatch", 403)
                result = report["result"]
                if not isinstance(result, dict):
                    raise Fault("invalid_command_report", 400)
                if cmd["status"] == "done" and cmd.get("result") != result:
                    # A superseded pending start may still have a late report only
                    # if the device had actually received it, which contradicts
                    # pending state and must be quarantined rather than overwritten.
                    raise Fault("command_result_conflict")
                if cmd["status"] != "done":
                    cmd.update(status="done", result=result)
                    self.store.put("command", cmd)
                    self.service.onboarding.report(cmd, result)
                    if cmd["action"] in {"start", "cancel", "resolve_run"} and result.get("error"):
                        rows = self.store.find("task", execution_id=cmd["payload"]["execution_id"], limit=1)
                        if rows and rows[0]["status"] not in TERMINAL:
                            current = rows[0]
                            # Device uncertainty preserves both the claim and join.
                            self.service._mark_task_unknown(current, "device_action_unconfirmed")
                ack.append(cmd["id"])
            for supplied in receipts:
                # The connector is the authority for its own OS process state.
                # Preserve that provenance explicitly; this is not independent
                # cloud attestation and a compromised device can still lie.
                receipt = dict(supplied, verification_status="device_reported", reporting_host_id=host_id)
                rows = self.store.find("task", execution_id=receipt["execution_id"], limit=1)
                if not rows or rows[0]["host_id"] != host_id or rows[0]["spec_digest"] != receipt["spec_digest"]:
                    raise Fault("receipt_identity_mismatch", 403)
                task = rows[0]
                if not self.service._receipt_matches_attempt(task, receipt):
                    raise Fault("receipt_identity_mismatch", 403)
                if receipt.get("status") not in TERMINAL or not self.service._receipt_confirmed(task, receipt):
                    if task["status"] not in TERMINAL:
                        # Only a confirmed terminal receipt settles the join. An
                        # unconfirmed terminal receipt (e.g. a pre-upgrade weak-POSIX
                        # run that could not prove tree termination) drives the task to
                        # unknown/quarantine — an IMMUTABLE OBSERVATION that is NEVER
                        # written to the `receipt` store (so a later operator-resolution
                        # CONFIRMED receipt cannot hit receipt_conflict).
                        self.service._mark_task_unknown(task, "device_execution_unverified")
                    # ACK it anyway so the device stops re-transmitting the same
                    # unconfirmed receipt every sync (it never enters receipt_ack via
                    # the save path below). Resolution is operator-driven from here.
                    receipt_ack.append(receipt["execution_id"])
                    continue
                try:
                    old = self.store.get("receipt", receipt["execution_id"])
                    if old["payload"] != receipt:
                        raise Fault("receipt_conflict")
                except Fault as error:
                    if error.status != 404:
                        raise
                    self.store.put("receipt", {"id": receipt["execution_id"], "host_id": host_id, "payload": receipt})
                receipt_ack.append(receipt["execution_id"])
                reconcile.append(task["id"])
            try:
                device = self.store.get("device", host_id)
            except Fault:
                device = {"id": host_id}
            facts = {"instance_id": instance_id, "version": str(body.get("version", ""))[:40]}
            # capability handshake: record the contract version the device
            # PROVED it understands (0 if it did not report one — a legacy device).
            # broker.start consults this so we never send a v2 contract to a device that
            # would ignore it and reply with a legacy (uuid-less) receipt.
            try:
                facts["supported_contract_version"] = int(body.get("supported_contract_version", 0))
            except (TypeError, ValueError):
                facts["supported_contract_version"] = 0
            # Durable device facts are written only when they change (N1).
            if any(device.get(k) != v for k, v in facts.items()):
                device.update(facts)
                device.pop("last_seen", None)
                self.store.put("device", device)
            seen = time.time()
            self.store.after_commit("device_seen:" + host_id,
                                    lambda: self._last_seen.__setitem__(host_id, seen))
            proven_cv = int(device.get("supported_contract_version", 0))
            commands = []
            for status in ("delivered", "pending"):
                for cmd in self.store.find("command", host_id=host_id, status=status, limit=4):
                    if cmd["status"] == "pending" and cmd["expires"] and cmd["expires"] < time.time():
                        cmd.update(status="done", result={"error": "command_expired"})
                        self.store.put("command", cmd)
                        self.service.onboarding.report(cmd, cmd["result"])
                        continue
                    # (TOCTOU): a start command carrying a vN contract may
                    # only be delivered to a device THIS sync proved supports >= vN. A
                    # device that (re)connected reporting a lower capability must not
                    # receive it — re-check at delivery, not just at enqueue. Holds the
                    # command (stays pending/delivered) until the device proves capability.
                    if cmd["action"] == "start":
                        need = int((cmd.get("payload") or {}).get("contract", {}).get("contract_version", 0))
                        if need > proven_cv:
                            continue
                    if len(commands) >= 1:
                        break
                    if cmd["status"] == "pending":
                        cmd["status"] = "delivered"
                        self.store.put("command", cmd)
                    commands.append({k: cmd[k] for k in ("id", "action", "payload", "expires")})
        for task_id in reconcile:
            self.service.reconcile(task_id)
        return {"commands": commands, "ack": ack, "receipt_ack": receipt_ack}
