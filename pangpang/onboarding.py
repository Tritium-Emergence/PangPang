"""Guided setup and asynchronous, approved device configuration."""
import base64
import copy
import hashlib
import ntpath
import os
import posixpath
import re
import secrets
import shutil
import threading
import time
from urllib.parse import urlsplit
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from . import coordinator_env
from .store import Fault, digest, uid, encode
from .execution import (ADAPTER_CATALOG, binary_matches_pin, endpoint_extra_env,
                       installation_id, validate_extra_argv, validate_profile_env,
                       validate_secret_refs, validate_staged_executor)
from .logs import get_logger
from .model_config import DEFAULT_OUTPUT_TOKENS, FIELDS, setup_profile

log = get_logger("onboarding")

# Fields of one adapters.json row. They are accepted only on declare_adapter.
# configure must not carry them: argv stays the approved declaration, not a
# later model edit of the configure call.
_DECLARATION_FIELDS = {"binary", "argv", "concurrency", "priority", "use_when", "secret_env", "extra_env"}


def apply_model_concurrency(candidate, value):
    """Persist the operator's model slot count on the existing scheduler dict.

    Concurrency 1 is the local-model setting; reserved_user_slots must then be 0
    or the next start rejects the config. A value above 1 keeps a reserved slot
    that is already legal and otherwise uses 1. No other scheduler key is written.
    """
    if type(value) is not int or not 1 <= value <= 16:
        raise Fault("invalid_max_concurrency", 400)
    sched = dict(candidate.get("scheduler") or {})
    sched["model_max_concurrency"] = value
    reserved = sched.get("reserved_user_slots", 0 if value == 1 else 1)
    if type(reserved) is not int or (value == 1) or not 0 <= reserved < value:
        reserved = 0 if value == 1 else 1
    sched["reserved_user_slots"] = reserved
    candidate["scheduler"] = sched
    return candidate

# A 16x16 red PNG: proves a "supports images" model endpoint accepts image input.
PROBE_PNG = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFklEQVR4nGO4o6FBEmIY1TCqYfhqAAAyBCwQhvh37QAAAABJRU5ErkJggg=="


def empty_config():
    return {"default_model": "default", "models": [{"id": "default", "model": "", "endpoint": "",
            "context_window_tokens": 40000, "serialized_bytes_safety_limit": 262144, "output_tokens": DEFAULT_OUTPUT_TOKENS}],
            "hosts": [], "installations": [], "resources": []}


def endpoint(value):
    if not value:
        return ""
    u = urlsplit(value)
    if u.username or u.password or u.query or u.fragment or not (
        u.scheme == "https" or u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise Fault("endpoint_requires_https_or_loopback", 400)
    return value.rstrip("/")


# A device fault code. Free-form text is not copied into the setup result.
_SETUP_FAULT_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

# Advice is operator-facing text we already attach to these faults. Other
# structured codes stay visible as the code alone.
_SETUP_ADVICE_CODES = {
    "invalid_adapter",
    "invalid_adapter_declaration",
    "adapter_declaration_conflict",
    "adapter_not_in_catalog",
    "cli_not_installed",
}

# Used when the device reports the code and does not send advice.
_DEFAULT_SETUP_ADVICE = {
    "cli_not_installed": "executable is not installed on PATH",
    "adapter_not_in_catalog": "adapter is not in this host's live catalog",
    "adapter_declaration_conflict": "conflicts with an adapter row already stored",
}


def _visible_setup_error(code, advice):
    """setup_result text for one device fault. Free-form text is not copied."""
    if not isinstance(code, str) or _SETUP_FAULT_CODE.fullmatch(code) is None:
        return "device_setup_failed"
    text = advice if code in _SETUP_ADVICE_CODES and isinstance(advice, str) else None
    if not isinstance(text, str) or not text.strip():
        text = _DEFAULT_SETUP_ADVICE.get(code)
    folded = " ".join(text.split())[:500] if isinstance(text, str) else ""
    return code + ": " + folded if folded else code


class Onboarding:
    def __init__(self, service):
        self.s, self.store, self.vault = service, service.store, service.vault
        self._declare_gates_guard = threading.Lock()
        self._declare_gates = {}

    def _declare_gate(self, identity):
        """The claim for one local declaration card. Approve and deny share it."""
        with self._declare_gates_guard:
            gate = self._declare_gates.get(identity)
            if gate is None:
                gate = threading.Lock()
                self._declare_gates[identity] = gate
            return gate

    def _local_declare_gate(self, identity):
        """The claim when this id is a local declare_adapter card, else None."""
        req = self.store.get("setup_request", identity)
        proposal = req.get("proposal") or {}
        if proposal.get("action") != "declare_adapter":
            return None
        host = self.s.execution.hosts.get(req.get("host_id"), {})
        if host.get("transport") != "local":
            return None
        return self._declare_gate(identity)

    def _config(self):
        # Includes earlier reports staged in this batch, without publishing them.
        try:
            return self.store.get("settings", "managed")["config"]
        except Fault as error:
            if error.status != 404:
                raise
            return copy.deepcopy(self.s.config) # never mutate the live dict before commit

    def status(self, bot_id=None):
        p = next(iter(self.s.profiles.values()))
        stored_c = int((self.s.config.get("scheduler") or {}).get(
            "model_max_concurrency", self.s.scheduler_config["model_max_concurrency"]))
        live_c = int(self.s.scheduler_config["model_max_concurrency"])
        return {"model": {"endpoint": p.get("endpoint", ""), "model": p.get("model", ""), "vision": p.get("vision") is True,
                "output_tokens": p.get("output_tokens"),
                "context_window_tokens": p.get("context_window_tokens"),
                **{k: p[k] for k in FIELDS if k in p},
                "has_legacy_generation_settings": bool(p.get("generation_settings")),
                "configured": bool(p.get("endpoint")), "has_secret": bool(p.get("credential_ref") and self.vault.has(p["credential_ref"]))},
                "model_max_concurrency": stored_c,
                "model_concurrency_restart_required": stored_c != live_c,
                "capabilities": self.s.effective_capabilities(bot_id),
                "coordinator_env": coordinator_env.projection(self.vault),
                "requests": self.store.find("setup_request", newest=True, limit=30),
                "inventory": self.store.find("inventory", limit=100)}

    def model(self, body):
        from .pi_runtime import PiModel
        # The candidate is born here, from this snapshot; its revision is the CAS
        # token. Any managed-config change during the probes below is a conflict
        # for the operator to retry, never silently overwritten (O5).
        candidate, base = self.s.managed_snapshot()
        if body.get("model_max_concurrency") is not None:
            apply_model_concurrency(candidate, body.get("model_max_concurrency"))
        old = candidate["models"][0]
        old_ref = old.get("credential_ref")
        if body.get("secret") and body.get("no_auth"):
            raise Fault("choose_key_or_no_auth", 400)
        p = setup_profile(old, body)
        p["endpoint"] = endpoint(p["endpoint"])
        from .service import normalize_profile
        normalize_profile(p)
        if not p["endpoint"] or not p["model"]:
            raise Fault("model_endpoint_and_name_required", 400)
        if body.get("secret") or body.get("no_auth"):
            p.pop("credential_env", None)
        new_ref = None
        if body.get("secret"):
            new_ref = uid("model-key")
            self.vault.set(new_ref, body["secret"])
            p["credential_ref"] = new_ref
        elif body.get("no_auth"):
            p.pop("credential_ref", None)
        elif (p["endpoint"], p.get("provider"), p.get("api")) != (old.get("endpoint"), old.get("provider", "custom"), old.get("api", "openai-completions")):
            raise Fault("new_endpoint_requires_credential_choice", 400)
        probe = {"type": "function", "function": {"name": "setup_probe", "description": "Return the probe value",
                  "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}}
        messages = [{"role": "system", "content": "Call setup_probe with value pangpang-ready. After its result say ready."},
                    {"role": "user", "content": "Test the tool now."}]
        vision_unverified = False
        try:
            message, _ = PiModel(self.s).complete(p, messages, [probe])
            calls = message.get("tool_calls", [])
            if len(calls) != 1 or calls[0]["function"]["name"] != "setup_probe":
                raise Fault("model_tool_call_unverified")
            import json
            if json.loads(calls[0]["function"]["arguments"]) != {"value": "pangpang-ready"}:
                raise Fault("model_tool_arguments_unverified")
            messages.extend([message, {"role": "tool", "tool_call_id": calls[0]["id"], "content": '{"value":"pangpang-ready"}'}])
            # 续接轮：只要模型能消费工具结果并正常返回（不报错）即算通过。不强求“出文字且不再调工具”——
            # reasoning 模型（gpt-5 等）拿到结果后再调一次工具确认是合理行为，content 可能为空。
            PiModel(self.s).complete(p, messages, [probe])
            if p["vision"]:
                # The endpoint must accept an image in the request, else attaching one
                # would fail every turn that carries it. But vision is optional: a
                # failed image probe disables the capability instead of refusing an
                # otherwise usable model — setup saves config, the probe is diagnostics.
                try:
                    message, _ = PiModel(self.s).complete(p, [
                        {"role": "system", "content": "Look at the image, then call setup_probe with value pangpang-ready."},
                        {"role": "user", "content": "Here is the image.",
                         "images": [{"data": PROBE_PNG, "mime": "image/png"}]}], [probe])
                    if [c["function"]["name"] for c in message.get("tool_calls", [])] != ["setup_probe"]:
                        raise Fault("model_vision_unverified")
                except Fault:
                    p["vision"] = False
                    vision_unverified = True
            candidate["models"] = [p]
            self.s.commit_managed_config(candidate, base_revision=base)
        except Exception:
            # The new secret is only referenced once the config commits (G).
            if new_ref:
                self.vault.delete(new_ref)
            raise
        if old_ref and old_ref != p.get("credential_ref"):
            self.vault.delete(old_ref)
        return {"configured": True, "tool_call_verified": True,
                "vision_unverified": vision_unverified}

    def revoke_model_key(self):
        candidate, base = self.s.managed_snapshot()
        p = dict(candidate["models"][0])
        old_ref = p.get("credential_ref")
        p.pop("credential_env", None)
        p["credential_ref"] = uid("revoked-key")
        candidate["models"] = [p]
        self.s.commit_managed_config(candidate, base_revision=base)
        # Delete the old secret only after the durable config stops referencing it
        # (G): a failed commit must not leave config pointing at a deleted key.
        if old_ref:
            self.vault.delete(old_ref)
        return {"revoked": True, "inflight_request_may_continue": True}

    def pairing_code(self):
        code = secrets.token_urlsafe(24)
        self.store.put("pairing", {"id": digest(code), "expires": time.time() + 600, "status": "pending"})
        return {"code": code, "expires_in": 600}

    def pair(self, body):
        identity = str(body.get("instance_id", ""))
        if not re.fullmatch(r"device-[a-f0-9]{32}", identity):
            raise Fault("device_identity_required", 400)
        pub = str(body.get("public_key", ""))
        try:
            key = serialization.load_der_public_key(base64.b64decode(pub, validate=True))
            if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
                raise ValueError()
        except Exception:
            raise Fault("device_public_key_required", 400) from None
        created = [] # vault refs this call wrote; removed if the pairing does not commit
        try:
            with self.store.atomic():
                record = self.store.get("pairing", digest(str(body.get("code", ""))))
                if record["expires"] < time.time():
                    raise Fault("pairing_expired", 403)
                if record["status"] == "used":
                    if record["instance_id"] != identity or record["public_key"] != pub:
                        raise Fault("pairing_already_used", 403)
                    host_id = record["host_id"]
                    if self.store.get("device", host_id).get("revoked"):
                        raise Fault("device_revoked", 403)
                    return {"host_id": host_id, "token": self.vault.get("pair:" + record["id"])}
                host_id = "host-" + digest(identity)[:16]
                if host_id in self.s.execution.hosts:
                    raise Fault("device_already_registered", 409)
                # Deliberately inside the transaction: "pair:<code>" is also what a
                # replay returns above, so the token may be written only after the
                # status check under the store lock; a write before it would replace
                # the token an already-paired device holds. A failed commit removes
                # it below (Vault does not roll back with the store).
                token = secrets.token_urlsafe(32)
                self.vault.set("pair:" + record["id"], token)
                created.append("pair:" + record["id"])
                # A connector is always governed by its own execution_level
                # (never by inheritance). Defaulting L0 at pair time removes the
                # silent-None dead end: L0 alone admits nothing by itself —
                # the cooperative labels only appear in a probe after the device's
                # OWN operator records consent, and consent is unwritable here.
                host = {"id": host_id, "name": str(body.get("name", host_id))[:80],
                        "os": str(body.get("os", ""))[:40], "transport": "connector",
                        "execution_level": "L0",
                        "credential_hash": hashlib.sha256(token.encode()).hexdigest(), "allowed_roots": []}
                # The host becomes authenticatable only when this commits (G):
                # no live-config mutation before the durable write.
                candidate = copy.deepcopy(self.s.config)
                candidate.setdefault("hosts", []).append(host)
                self.store.put("device", {"id": host_id, "instance_id": identity, "public_key": pub})
                record.update(status="used", instance_id=identity, public_key=pub, host_id=host_id)
                self.store.put("pairing", record)
                self.s.commit_managed_config(candidate)
                self.s.broker.enqueue(host_id, "discover", {}, "pair")
                return {"host_id": host_id, "token": token}
        except BaseException:
            for ref in created:
                self.vault.delete(ref)
            raise

    def _stage_adapter_declaration(self, host_id, bot_id, work_id, body):
        """Open an approval card for one custom row. Does not write a file."""
        from .adapters_file import clean_declaration
        row = {k: body[k] for k in _DECLARATION_FIELDS if k in body}
        for key in set(body) - {"host_id", "action", "adapter", *_DECLARATION_FIELDS}:
            row[key] = body[key]
        name = body.get("adapter")
        clean = clean_declaration(name, row)
        proposal = {"action": "declare_adapter", "adapter": name, "adapter_row": clean,
                    "command": [clean["binary"], *clean["argv"]]}
        req = self.store.put("setup_request", {
            "id": uid("setup"), "host_id": host_id, "bot_id": bot_id, "work_id": work_id,
            "proposal": proposal, "status": "needs_approval", "expires": time.time() + 3600})
        return {"request_id": req["id"], "status": req["status"], "yielded": True}

    def _project_declared(self, host_id, reported, expected_name, expected_row):
        """The rows a matching declare report would store. Does not write.

        Only the approved name is validated and kept. Sibling rows are not
        imported, so a legacy absolute-path sibling does not reject this row.
        A mismatch stores nothing.
        """
        from .adapters_file import MAX_ROWS, clean_declaration
        if not isinstance(reported, list) or len(reported) > MAX_ROWS:
            raise Fault("invalid_declared_adapters", 400,
                        advice="declared_adapters must be a list of at most %d rows" % MAX_ROWS)
        expected = clean_declaration(expected_name, expected_row)
        matched = None
        for item in reported:
            if not isinstance(item, dict):
                raise Fault("invalid_declared_adapters", 400,
                            advice="each declared_adapters entry must be an object")
            if item.get("adapter") != expected_name:
                continue
            body = {k: v for k, v in item.items() if k != "adapter"}
            matched = clean_declaration(expected_name, body)
        if matched != expected:
            raise Fault("adapter_declaration_mismatch", 403,
                        advice="the reported adapter row does not match the approved declaration")
        try:
            previous = dict(self.store.get("declared_adapters", host_id).get("rows") or {})
        except Fault as error:
            if error.status != 404:
                raise
            previous = {}
        previous[expected_name] = expected
        return previous

    def _accept_declared(self, host_id, reported, expected_name, expected_row):
        """Remember one approved row when the device report matches it exactly."""
        previous = self._project_declared(host_id, reported, expected_name, expected_row)
        self.store.put("declared_adapters", {"id": host_id, "rows": previous})
        remembered = dict(previous)

        def publish():
            host_rows = dict(self.s.execution.declared_adapters.get(host_id) or {})
            host_rows.update(remembered)
            self.s.execution.set_declared_adapters(host_id, host_rows)
        self.store.after_commit("declared:" + host_id, publish)
        return previous

    def _bounded_result(self, cmd, result):
        """Fields safe to persist for discover and declare_adapter. No writes."""
        safe = {"action": cmd["action"], "host_id": cmd["host_id"]}
        payload = cmd.get("payload") or {}
        if result.get("error"):
            safe["error"] = _visible_setup_error(result.get("error"), result.get("advice"))
            return safe
        if cmd["action"] == "discover":
            items = [{k: str(x[k])[:500] for k in ("adapter", "binary", "version") if k in x}
                     for x in result.get("inventory", [])[:20] if isinstance(x, dict)]
            safe["inventory"] = items
            return safe
        stored = self._project_declared(cmd["host_id"], result.get("declared_adapters"),
                                        payload.get("adapter"), payload.get("row"))
        safe["declared_adapters"] = [{"adapter": name, **row} for name, row in sorted(stored.items())]
        return safe

    def _finish_setup_report(self, cmd, safe):
        payload = cmd["payload"]
        req = None
        if payload.get("request_id"):
            req = self.store.get("setup_request", payload["request_id"])
        if req:
            req["status"] = "failed" if safe.get("error") else "done"
            self.store.put("setup_request", req)
        bot_id = req["bot_id"] if req else payload.get("bot_id")
        work_id = req["work_id"] if req else payload.get("work_id")
        if bot_id and work_id:
            work = self.store.get("work", work_id)
            root = self.s._new_root("operator", "setup:" + str(cmd.get("id", "")))["id"]
            self.s.new_turn(bot_id, work, safe, "setup_result", "device", root_request_id=root)

    _BOUNDED_REPORT_FAULTS = {"adapter_declaration_mismatch", "invalid_declared_adapters",
                              "invalid_adapter_declaration"}

    def same_bounded_result(self, stored, projected):
        """True when a repeated declare or discover report matches what was stored.

        A fault recorded before advice was appended is the bare code. The same
        report now projects ``code: advice``. A setup fault recorded as
        ``device_setup_failed`` is the same report once its code is preserved.
        """
        if stored == projected:
            return True
        if not isinstance(stored, dict) or not isinstance(projected, dict):
            return False
        if set(stored) != set(projected):
            return False
        for key, value in stored.items():
            other = projected[key]
            if value == other:
                continue
            if key != "error" or not self._bare_fault_error(value, other):
                return False
        return True

    def _bare_fault_error(self, stored, projected):
        if not isinstance(stored, str) or not isinstance(projected, str):
            return False
        if stored in self._BOUNDED_REPORT_FAULTS:
            prefix = stored + ": "
            return projected.startswith(prefix) and len(projected) > len(prefix)
        if stored != "device_setup_failed" or projected == "device_setup_failed":
            return False
        code = projected.split(": ", 1)[0]
        return _SETUP_FAULT_CODE.fullmatch(code) is not None

    def _report_bounded(self, cmd, result, *, apply):
        """Persist discover and declare_adapter as the checked projection only."""
        try:
            safe = self._bounded_result(cmd, result)
        except Fault as error:
            if error.code not in self._BOUNDED_REPORT_FAULTS:
                raise
            advice = error.advice
            folded = " ".join(advice.split())[:500] if isinstance(advice, str) else ""
            text = error.code + ": " + folded if folded else error.code
            safe = {"action": cmd["action"], "host_id": cmd["host_id"], "error": text}
        if not apply:
            return safe
        with self.store.atomic():
            if not safe.get("error") and cmd["action"] == "discover":
                self.store.put("inventory", {"id": cmd["host_id"], "items": safe["inventory"]})
            elif not safe.get("error") and cmd["action"] == "declare_adapter":
                payload = cmd.get("payload") or {}
                self._accept_declared(cmd["host_id"], result.get("declared_adapters"),
                                      payload.get("adapter"), payload.get("row"))
            self._finish_setup_report(cmd, safe)
        return safe

    def request(self, bot_id, work_id, body):
        if self.s.bot(bot_id)["kind"] != "main":
            raise Fault("main_bot_required", 403)
        host_id = body["host_id"]
        action = body["action"]
        if action == "probe":
            return self.s.operator_probe(host_id, body.get("installation_id"))
        host = self.s.execution.hosts.get(host_id, {})
        if host.get("transport") == "local" and action in {"declare_adapter", "configure"}:
            return self._request_local(bot_id, work_id, host_id, host, action, body)
        if host.get("transport") != "connector":
            raise Fault("connector_host_required")
        if action == "discover":
            cmd = self.s.broker.enqueue(host_id, "discover", {"bot_id": bot_id, "work_id": work_id})
            return {"command_id": cmd["id"], "yielded": True}
        if action == "declare_adapter":
            return self._stage_adapter_declaration(host_id, bot_id, work_id, body)
        if _DECLARATION_FIELDS & set(body):
            raise Fault("invalid_setup_request_field", 400)
        if action not in {"configure", "credential", "delete_credential"}:
            raise Fault("invalid_setup_action")
        if action == "configure":
            if not self.s.adapter_known(host_id, body.get("adapter")):
                raise Fault("adapter_required")
            # Endpoint and qwen resource argv are operator/device-owned security
            # policy. A model proposal can request the adapter/auth mode only.
            if any(k in body for k in ("endpoint", "slot_acquire_args", "slot_release_args",
                                       "profile", "env", "extra_argv")):
                raise Fault("operator_owned_setup_field", 400)
            body = dict(body, auth=body.get("auth", "existing"), endpoint="", endpoint_source="operator")
        else:
            inst = next((i for i in self._config().get("installations", [])
                         if i["id"] == body.get("installation_id")), {})
            if inst.get("host_id") != host_id:
                raise Fault("installation_host_mismatch")
            body = dict(body, expected_policy_digest=self.store.get("probe", inst["id"])["policy_digest"],
                        endpoint=inst.get("endpoint", ""))
        req = self.store.put("setup_request", {"id": uid("setup"), "host_id": host_id, "bot_id": bot_id,
                  "work_id": work_id, "proposal": body, "status": "needs_secret" if action == "credential" else "needs_approval",
                  "expires": time.time() + 3600})
        return {"request_id": req["id"], "status": req["status"], "yielded": True}

    def _request_local(self, bot_id, work_id, host_id, host, action, body):
        """Declare or configure on this coordinator. Neither call writes a row."""
        if action == "declare_adapter":
            return self._stage_adapter_declaration(host_id, bot_id, work_id, body)
        if _DECLARATION_FIELDS & set(body):
            raise Fault("invalid_setup_request_field", 400)
        if any(k in body for k in ("endpoint", "slot_acquire_args", "slot_release_args",
                                   "profile", "env", "extra_argv")):
            raise Fault("operator_owned_setup_field", 400)
        if not self.s.adapter_known(host_id, body.get("adapter")):
            raise Fault("adapter_required")
        staged = {"adapter": body.get("adapter")}
        if body.get("cwd"):
            staged["cwd"] = body["cwd"]
        req = self.stage_executor(bot_id, work_id, None, host, staged, body)
        return {"request_id": req["id"], "status": req["status"], "yielded": True}

    def operator_request(self, body):
        action = body.get("action", "configure")
        allowed = {"configure": {"host_id", "action", "adapter", "cwd", "auth"},
                   "credential": {"host_id", "action", "installation_id"},
                   "delete_credential": {"host_id", "action", "installation_id"}}
        if action not in allowed:
            raise Fault("invalid_setup_action")
        if set(body) - allowed[action]:
            raise Fault("invalid_setup_request_field", 400)
        host_id = body.get("host_id")
        if self.s.execution.hosts.get(host_id, {}).get("transport") != "connector":
            raise Fault("connector_host_required")
        if action == "configure":
            adapter = body.get("adapter")
            if not self.s.adapter_known(host_id, adapter):
                raise Fault("adapter_required")
            if body.get("auth", "existing") not in {"existing", "api_key"}:
                raise Fault("invalid_setup_auth", 400)
            try:
                inventory = self.store.get("inventory", host_id)
            except Fault as error:
                if error.status != 404:
                    raise
                inventory = None
            discovered = {str(x.get("adapter")) for x in (inventory or {}).get("items", [])}
            if adapter not in discovered:
                raise Fault("adapter_not_in_inventory",
                            advice="only adapters the device itself reported in its latest "
                                   "discovery can be registered; run executor discovery on that computer first")
            proposal = {"action": "configure", "adapter": adapter,
                        "auth": body.get("auth", "existing"), "endpoint": "", "endpoint_source": "operator"}
            if body.get("cwd"):
                proposal["cwd"] = body["cwd"]
            status = "needs_approval"
        else:
            inst = next((i for i in self._config().get("installations", [])
                         if i["id"] == body.get("installation_id")), {})
            if inst.get("host_id") != host_id:
                raise Fault("installation_host_mismatch")
            proposal = dict(body, expected_policy_digest=self.store.get("probe", inst["id"])["policy_digest"],
                            endpoint=inst.get("endpoint", ""))
            status = "needs_secret" if action == "credential" else "needs_approval"
        req = self.store.put("setup_request", {"id": uid("setup"), "host_id": host_id, "bot_id": None,
                  "work_id": None, "origin": "operator", "proposal": proposal, "status": status,
                  "expires": time.time() + 3600})
        return {"request_id": req["id"], "status": req["status"], "yielded": True}

    def stage_executor(self, bot_id, work_id, inst, host, staged, raw):
        """One setup_request for execution changes. The installation is not written."""
        host_id = inst["host_id"] if inst else raw.get("host_id")
        adapter = (inst or {}).get("adapter") or raw.get("adapter") or staged.get("adapter")
        local = (host or {}).get("transport") == "local"
        # A local coordinator builds argv from its own catalog, so the adapter
        # must be there. A connector host runs its own commands: its declaration
        # is the membership truth for that host.
        if local:
            if adapter not in ADAPTER_CATALOG:
                raise Fault("unknown_adapter")
        elif not self.s.adapter_known(host_id, adapter):
            raise Fault("unknown_adapter")
        validate_staged_executor(adapter, host_id, staged)
        proposal = {
            "action": "executor_config" if local else "configure",
            "installation_id": None if inst is None else inst["id"],
            "adapter": adapter,
            "draft": {k: staged[k] for k in staged if k != "adapter"},
            "base_revision": self.s.config_revision,
            "auth": raw.get("auth", "existing"),
            "endpoint": "",
            "endpoint_source": "operator",
        }
        if local and inst is None:
            sibling = self._sibling_local_installation(host_id, adapter)
            argv = (sibling or {}).get("argv") or []
            if argv:
                proposal["command"] = [argv[0], *ADAPTER_CATALOG[adapter]["argv"]]
            else:
                shown = self._catalog_command(adapter)
                if shown:
                    proposal["command"] = shown
        req = self.store.put("setup_request", {
            "id": uid("setup"), "host_id": host_id, "bot_id": bot_id, "work_id": work_id,
            "origin": "executor_proposal", "proposal": proposal, "status": "needs_approval",
            "expires": time.time() + 3600})
        return req

    def deny(self, identity):
        gate = self._local_declare_gate(identity)
        if gate is None:
            return self._deny_request(identity)
        with gate:
            return self._deny_request(identity)

    def _deny_request(self, identity):
        with self.store.atomic():
            req = self.checked_request(identity, "needs_approval")
            req["status"] = "denied"
            self.store.put("setup_request", req)
        return {"denied": True}

    def _sibling_local_installation(self, host_id, adapter):
        """An already registered local row of this adapter. Its binary is reused."""
        rows = [i for i in self.s.execution.installations.values()
                if i.get("host_id") == host_id and i.get("adapter") == adapter
                and i.get("id") not in self.s.execution.unknown_adapters
                and i.get("argv")]
        return sorted(rows, key=lambda i: i["id"])[0] if rows else None

    def _catalog_command(self, adapter):
        """The command shown for a custom row that has no installation yet."""
        from .adapters_file import is_merged
        if not is_merged(adapter):
            return None
        cat = ADAPTER_CATALOG.get(adapter) or {}
        binary, argv = cat.get("binary"), cat.get("argv")
        if not isinstance(binary, str) or not binary or not isinstance(argv, list):
            return None
        return [binary, *argv]

    def _resolved_custom_binary(self, adapter):
        """PATH resolution of one custom catalog binary. Built-ins stay on startup discovery.

        expanduser runs before which. Only that expanded value is looked up, so a
        stored path cannot match a different binary of the same name on PATH.
        """
        from .adapters_file import is_merged
        if not is_merged(adapter):
            return None
        cat = ADAPTER_CATALOG.get(adapter) or {}
        name = cat.get("binary")
        if not isinstance(name, str) or not name or "\x00" in name:
            return None
        expanded = os.path.expanduser(name)
        if not expanded or "\x00" in expanded:
            return None
        found = shutil.which(expanded)
        if not found or not binary_matches_pin(cat, found):
            return None
        return found

    def _apply_new_routing(self, row, routing):
        """Copy priority and use_when onto a row that has no sibling yet."""
        if "priority" in routing:
            raw = routing["priority"]
            if isinstance(raw, bool):
                raise Fault("invalid_priority", 400)
            if isinstance(raw, int):
                value = raw
            elif isinstance(raw, float) and raw.is_integer():
                value = int(raw)
            else:
                raise Fault("invalid_priority", 400)
            if not 1 <= value <= 999:
                raise Fault("invalid_priority", 400)
            row["priority"] = value
        if "use_when" in routing:
            text = routing["use_when"]
            if text is None or (isinstance(text, str) and not text.strip()):
                return
            if not isinstance(text, str) or len(text) > 500:
                raise Fault("invalid_use_when", 400)
            row["use_when"] = text.strip()

    def _prepare_new_local_profile(self, proposal, draft, body, host_id):
        """Build a local profile row from a catalog command. Does not write it."""
        adapter = proposal.get("adapter")
        if adapter not in ADAPTER_CATALOG or not host_id:
            raise Fault("unknown_adapter")
        profile = draft.get("profile") or None
        iid = installation_id(adapter, host_id, profile)
        if iid in self.s.execution.installations:
            raise Fault("installation_already_exists", 409)
        sibling = self._sibling_local_installation(host_id, adapter)
        if sibling:
            binary = sibling["argv"][0]
        else:
            binary = self._resolved_custom_binary(adapter)
            if not binary:
                stored = (ADAPTER_CATALOG.get(adapter) or {}).get("binary")
                label = stored if isinstance(stored, str) and stored and "\x00" not in stored else (adapter or "unknown")
                raise Fault("cli_not_installed",
                            advice="executable %s is not installed on PATH" % str(label)[:200])
        host = self.s.execution.hosts.get(host_id, {})
        from . import runner
        try:
            runner.check_control_separation(host.get("allowed_roots") or [], [self.s.data])
        except ValueError:
            raise Fault("workspace_overlaps_control_plane", 400) from None
        roots = host.get("allowed_roots") or []
        fallback = roots[0] if sibling is None and roots else ""
        try:
            cwd = str(runner.checked(str(draft.get("cwd") or (sibling or {}).get("cwd") or fallback).strip(),
                                     roots))
        except (ValueError, OSError):
            raise Fault("cwd_outside_allowed_roots", 400) from None
        base = sibling or {}
        row = {
            "id": iid, "host_id": host_id, "adapter": adapter,
            "argv": [binary, *ADAPTER_CATALOG[adapter]["argv"]],
            "cwd": cwd, "enabled": True,
            "prompt_mode": base.get("prompt_mode", "stdin"),
            "risk": base.get("risk", "write"),
            "timeout_s": base.get("timeout_s", 3600),
            "max_concurrency": base.get("max_concurrency", ADAPTER_CATALOG[adapter]["concurrency"]),
            "env_allowlist": list(base.get("env_allowlist") or []),
            "resource_keys": list(base.get("resource_keys") or []),
        }
        if base.get("probe_argv"):
            row["probe_argv"] = list(base["probe_argv"])
        if profile:
            row["profile"] = profile
        if draft.get("env"):
            row["env"] = dict(draft["env"])
        if draft.get("extra_argv"):
            row["extra_argv"] = list(draft["extra_argv"])
        if draft.get("secret_refs"):
            row["secret_refs"] = dict(draft["secret_refs"])
        routing = {k: draft[k] for k in ("priority", "use_when") if k in draft}
        if routing and sibling:
            checked = self.s.prepare_installation_config(sibling["id"], routing)["updated"]
            for key in routing:
                if key in checked:
                    row[key] = checked[key]
        elif routing:
            self._apply_new_routing(row, routing)
        endpoint_value = (body or {}).get("endpoint")
        model, api = "", None
        if endpoint_value and ADAPTER_CATALOG[adapter].get("models_json"):
            from .broker import global_profile
            profile = global_profile(self.s.config)
            model, api = profile.get("model") or "", profile.get("api") or "openai-completions"
        extra = endpoint_extra_env(adapter, self.s.data, endpoint_value, model=model, api=api)
        if extra:
            row["extra_env"] = extra
        self.s._stamp_workspace_key(row)
        return row

    def _finish_local_executor(self, identity, proposal, candidate, iid, creating=False):
        with self.store.atomic():
            current_req = self.checked_request(identity, "needs_approval")
            if current_req.get("proposal", {}).get("base_revision") != self.s.config_revision:
                raise Fault("installation_config_changed", 409)
            if creating and iid in self.s.execution.installations:
                raise Fault("installation_already_exists", 409)
            self.s.commit_managed_config(candidate, base_revision=proposal.get("base_revision"))
            current_req["status"] = "done"
            self.store.put("setup_request", current_req)
            finished = {"id": current_req["id"], "bot_id": current_req.get("bot_id"),
                        "work_id": current_req.get("work_id"), "host_id": current_req.get("host_id"),
                        "action": (current_req.get("proposal") or {}).get("action")}
        result = {"accepted": True, "installation_id": iid}
        # The installation is already committed. A probe failure stays a probe result.
        probe = self._probe_approved_executor(iid)
        if probe is not None:
            result["probe"] = probe
            self._publish_setup_probe(finished, iid, probe)
        return result

    def _probe_approved_executor(self, installation_id):
        """The operator probe, on the host that owns this installation.

        Unknown-adapter rows are not probed. A failure is returned as a probe
        result and does not undo the approval that just committed.
        """
        if installation_id in self.s.execution.unknown_adapters:
            return None
        inst = self.s.execution.installations.get(installation_id)
        if not inst or not self.s.execution.knows_adapter(inst.get("adapter"), inst.get("host_id")):
            return None
        try:
            stored = self.s.probe(installation_id)
        except Fault as error:
            return {"available": False, "reason": error.code, "probed": True}
        except Exception:
            log.exception("executor probe failed after approval installation=%s", installation_id)
            return {"available": False, "reason": "probe_failed", "probed": True}
        view = {"available": stored.get("available") is True, "probed": True}
        if stored.get("reason"):
            view["reason"] = stored.get("reason")
        if stored.get("supervision"):
            view["supervision"] = stored.get("supervision")
        return view

    def _publish_setup_probe(self, req, installation_id, probe):
        """Tell the assistant the approval finished and what the probe found."""
        bot_id, work_id = req.get("bot_id"), req.get("work_id")
        if not bot_id or not work_id or not probe:
            return
        try:
            work = self.store.get("work", work_id)
        except Fault:
            return
        safe = {"action": req.get("action") or "executor_config", "host_id": req.get("host_id"),
                "accepted": True, "installation_id": installation_id, "probe": probe}
        root = self.s._new_root("operator", "setup:" + str(req.get("id") or installation_id))["id"]
        self.s.new_turn(bot_id, work, safe, "setup_result", "device", root_request_id=root)

    def _approve_local_executor(self, identity, body=None):
        """Operator confirms a local execution change. Run authorization is unchanged."""
        body = body or {}
        req = self.checked_request(identity, "needs_approval")
        proposal = req.get("proposal") or {}
        draft = dict(proposal.get("draft") or {})
        iid = proposal.get("installation_id")
        if not iid:
            prepared = self._prepare_new_local_profile(proposal, draft, body, req.get("host_id"))
            candidate = copy.deepcopy(self.s.config)
            candidate["installations"] = list(candidate.get("installations") or []) + [prepared]
            return self._finish_local_executor(identity, proposal, candidate, prepared["id"], creating=True)
        inst = self.s.execution.installations.get(iid)
        if not inst:
            raise Fault("installation_not_found", 404)
        fields = {k: draft[k] for k in ("cwd", "extra_argv", "enabled", "max_concurrency", "timeout_s") if k in draft}
        prepared = self.s.prepare_installation_config(iid, fields) if fields else None
        updated = dict(prepared["updated"] if prepared else inst)
        if "env" in draft:
            validate_profile_env(draft["env"])
            if draft["env"]:
                updated["env"] = dict(draft["env"])
            else:
                updated.pop("env", None)
        if "secret_refs" in draft:
            validate_secret_refs(draft["secret_refs"])
            if draft["secret_refs"]:
                updated["secret_refs"] = dict(draft["secret_refs"])
            else:
                updated.pop("secret_refs", None)
        if draft.get("profile"):
            if installation_id(updated.get("adapter"), updated.get("host_id"), draft["profile"]) != iid:
                raise Fault("installation_approval_mismatch", 403)
            updated["profile"] = draft["profile"]
        candidate = copy.deepcopy(self.s.config)
        candidate["installations"] = [updated if i.get("id") == iid else i
                                      for i in candidate.get("installations", [])]
        return self._finish_local_executor(identity, proposal, candidate, iid)

    def checked_request(self, identity, status):
        req = self.store.get("setup_request", identity)
        if req["status"] != status or req["expires"] < time.time():
            raise Fault("setup_request_expired_or_used", 409)
        # A local installation is this coordinator, not a paired device. Connector
        # requests still require the device row and refuse a revoked one.
        host = self.s.execution.hosts.get(req.get("host_id"), {})
        if host.get("transport") != "local":
            if self.store.get("device", req["host_id"]).get("revoked"):
                raise Fault("device_revoked", 403)
        return req

    def _approve_local_declaration(self, identity):
        """Write one approved row into this coordinator's adapters file.

        The card is claimed before any file write, so deny cannot commit
        while this approval is in progress. The file write stays outside the
        store transaction. The request remains needs_approval until that
        write has succeeded.
        """
        with self._declare_gate(identity):
            req = self.checked_request(identity, "needs_approval")
            proposal = dict(req.get("proposal") or {})
            host_id = req.get("host_id")
            if self.s.execution.hosts.get(host_id, {}).get("transport") != "local":
                raise Fault("connector_host_required")
            from .adapters_file import clean_declaration, write_merged_row
            name = proposal.get("adapter")
            clean = clean_declaration(name, proposal.get("adapter_row"))
            if (clean != proposal.get("adapter_row")
                    or proposal.get("command") != [clean["binary"], *clean["argv"]]):
                raise Fault("adapter_declaration_mismatch", 403)
            write_merged_row(self.s.data / "adapters.json", name, clean)
            self.s.execution.refresh_unknown_adapters()
            with self.store.atomic():
                current = self.checked_request(identity, "needs_approval")
                current_proposal = current.get("proposal") or {}
                if (current_proposal.get("adapter_row") != clean
                        or current_proposal.get("command") != [clean["binary"], *clean["argv"]]):
                    raise Fault("adapter_declaration_mismatch", 403)
                current["status"] = "done"
                self.store.put("setup_request", current)
                self._publish_local_declaration(current, name, clean)
            return {"accepted": True}

    def _publish_local_declaration(self, req, name, clean):
        """Tell the assistant the approved row was stored. No command is launched."""
        bot_id, work_id = req.get("bot_id"), req.get("work_id")
        if not bot_id or not work_id:
            return
        try:
            work = self.store.get("work", work_id)
        except Fault:
            return
        safe = {"action": "declare_adapter", "host_id": req.get("host_id"),
                "declared_adapters": [{"adapter": name, **clean}]}
        root = self.s._new_root("operator", "setup:" + str(req.get("id") or name))["id"]
        self.s.new_turn(bot_id, work, safe, "setup_result", "device", root_request_id=root)

    def approve(self, identity, body=None):
        body = body or {}
        if set(body) - {"endpoint", "profile", "env", "extra_argv"}:
            raise Fault("invalid_setup_approval", 400)
        peeked = self.store.get("setup_request", identity)
        proposal = peeked.get("proposal") or {}
        if (proposal.get("action") == "declare_adapter"
                and self.s.execution.hosts.get(peeked.get("host_id"), {}).get("transport") == "local"):
            if body:
                raise Fault("invalid_setup_approval", 400)
            return self._approve_local_declaration(identity)
        if proposal.get("action") == "executor_config":
            creating = not (peeked.get("proposal") or {}).get("installation_id")
            if creating:
                if set(body) - {"endpoint"}:
                    raise Fault("invalid_setup_approval", 400)
            elif body:
                raise Fault("invalid_setup_approval", 400)
            return self._approve_local_executor(identity, body if creating else None)
        with self.store.atomic():
            req = self.checked_request(identity, "needs_approval")
            proposal = dict(req["proposal"])
            if proposal.get("action") == "declare_adapter":
                if body:
                    raise Fault("invalid_setup_approval", 400)
                if self.s.execution.hosts.get(req["host_id"], {}).get("transport") != "connector":
                    raise Fault("connector_host_required")
                from .adapters_file import clean_declaration
                name = proposal.get("adapter")
                clean = clean_declaration(name, proposal.get("adapter_row"))
                if (clean != proposal.get("adapter_row")
                        or proposal.get("command") != [clean["binary"], *clean["argv"]]):
                    raise Fault("adapter_declaration_mismatch", 403)
                payload = {"request_id": req["id"], "adapter": name, "row": clean}
                self.s.broker.enqueue(req["host_id"], "declare_adapter", payload, req["id"])
                req["status"] = "configuring"
                self.store.put("setup_request", req)
                return {"accepted": True}
            action = "configure" if proposal["action"] == "configure" else "credential_delete"
            if action == "configure":
                # Endpoint comes from the authenticated operator UI, never from
                # model tool arguments. A displayed draft is applied only because
                # this call is that confirmation. Blank endpoint means the CLI default.
                draft = proposal.get("draft") or {}
                if "base_revision" in proposal and proposal["base_revision"] != self.s.config_revision:
                    raise Fault("installation_config_changed", 409)
                if any(key in draft for key in ("enabled", "max_concurrency", "timeout_s", "secret_refs")):
                    raise Fault("unsupported_installation_field", 400)
                proposal["endpoint"] = endpoint(str(body.get("endpoint", "")))
                proposal["endpoint_source"] = "operator"
                profile = body["profile"] if "profile" in body else draft.get("profile")
                env = body["env"] if "env" in body else draft.get("env")
                if env is not None:
                    validate_profile_env(env)
                extra_argv = body["extra_argv"] if "extra_argv" in body else draft.get("extra_argv")
                if extra_argv is not None:
                    validate_extra_argv(proposal["adapter"], extra_argv)
                # Same canonical namespace as LocalSetup, derived by the server.
                # A model draft is not sent to the device until this operator approval.
                # An existing installation keeps its id. A profile that would rename
                # it is refused. Omitting profile keeps the profile already stored.
                locked = proposal.get("installation_id")
                if locked:
                    if profile and installation_id(proposal["adapter"], req["host_id"], profile) != locked:
                        raise Fault("installation_approval_mismatch", 403)
                    if not profile:
                        previous_locked = next((i for i in self._config().get("installations", [])
                                                if i["id"] == locked), {})
                        if previous_locked.get("profile"):
                            profile = previous_locked["profile"]
                    proposal["installation_id"] = locked
                else:
                    proposal["installation_id"] = installation_id(proposal["adapter"], req["host_id"], profile)
                if profile:
                    proposal["profile"] = profile
                if env:
                    proposal["env"] = dict(env)
                if extra_argv:
                    proposal["extra_argv"] = list(extra_argv)
                if draft.get("cwd"):
                    proposal["cwd"] = draft["cwd"]
                previous = next((i for i in self._config().get("installations", [])
                                 if i["id"] == proposal["installation_id"]), {})
                if previous and previous["host_id"] != req["host_id"]:
                    raise Fault("installation_id_owned_by_other_host", 403)
                proposal["resource_keys"] = list(previous.get("resource_keys", []))
                if not self.s.adapter_known(req["host_id"], proposal["adapter"]):
                    raise Fault("adapter_required")
                # Approved store row, then the device's routing projection, then
                # the built-in catalog. 1 is the last resort.
                declared_row = (self.s.declared_row(req["host_id"], proposal["adapter"])
                                or self.s.execution.host_declared(req["host_id"], proposal["adapter"])
                                or {})
                proposal["max_concurrency"] = previous.get("max_concurrency",
                                                           declared_row.get("concurrency",
                                                           ADAPTER_CATALOG.get(proposal["adapter"], {}).get("concurrency", 1)))
                if proposal["adapter"] == "pi":
                    from .broker import global_profile
                    profile = global_profile(self.s.config)
                    proposal["model"] = profile.get("model") or ""
                    proposal["api"] = profile.get("api") or "openai-completions"
                req["proposal"] = proposal
            elif body:
                raise Fault("invalid_setup_approval", 400)
            payload = dict(proposal, request_id=req["id"])
            self.s.broker.enqueue(req["host_id"], action, payload, req["id"])
            req["status"] = "configuring"
            self.store.put("setup_request", req)
        return {"accepted": True}

    def coordinator_env_target(self, name):
        """Public key and AEAD context for one allowlisted coordinator env name.

        The context is derived here. A client-supplied context is ignored on save.
        """
        return {"public_key": self.vault.public_key(), "context": coordinator_env.context_for(name),
                "name": name}

    def set_coordinator_env(self, body):
        if not isinstance(body, dict) or set(body) != {"name", "sealed"}:
            raise Fault("sealed_credential_required", 400)
        return coordinator_env.install(self.vault, body.get("name"), body.get("sealed"))

    def clear_coordinator_env(self, body):
        if not isinstance(body, dict) or set(body) != {"name"}:
            raise Fault("coordinator_env_not_allowlisted", 400)
        return coordinator_env.clear(self.vault, body.get("name"))

    def secret_target(self, identity):
        req = self.checked_request(identity, "needs_secret")
        device = self.store.get("device", req["host_id"])
        return {"public_key": device["public_key"], "context": req["id"], "host_id": req["host_id"],
                "installation_id": req["proposal"]["installation_id"], "endpoint": req["proposal"].get("endpoint", ""),
                "storage": "所选设备的加密凭据库；密钥不进入聊天或模型"}

    def secret(self, identity, body):
        sealed = body.get("sealed", {})
        if set(body) != {"sealed"} or set(sealed) != {"key", "nonce", "ciphertext"} or len(encode(sealed)) > 32000:
            raise Fault("sealed_credential_required", 400)
        with self.store.atomic():
            req = self.checked_request(identity, "needs_secret")
            self.s.broker.enqueue(req["host_id"], "credential_install",
                {"installation_id": req["proposal"]["installation_id"], "request_id": identity, "sealed": sealed,
                 "expected_policy_digest": req["proposal"]["expected_policy_digest"]}, identity)
            req["status"] = "configuring"
            self.store.put("setup_request", req)
        return {"accepted": True}

    def report(self, cmd, result, *, apply=True):
        if cmd["action"] in {"discover", "declare_adapter"}:
            return self._report_bounded(cmd, result, apply=apply)
        with self.store.atomic():
            if cmd["action"] not in {"configure", "credential_install", "credential_delete"}:
                return
            payload = cmd["payload"]
            req = None
            if payload.get("request_id"):
                req = self.store.get("setup_request", payload["request_id"])
            safe = {"action": cmd["action"], "host_id": cmd["host_id"]}
            if result.get("error"):
                safe["error"] = _visible_setup_error(result.get("error"), result.get("advice"))
            else:
                inst = result.get("installation", {})
                if (not isinstance(inst, dict) or not all(isinstance(inst.get(k), str) for k in ("id", "host_id", "adapter"))
                        or inst.get("host_id") != cmd["host_id"]
                        or not self.s.adapter_known(cmd["host_id"], inst.get("adapter"))):
                    raise Fault("invalid_installation_report")
                item = {k: inst.get(k) for k in ("id", "host_id", "adapter", "cwd")}
                candidate = copy.deepcopy(self._config())
                previous = next((i for i in candidate.get("installations", []) if i["id"] == item["id"]), {})
                # Existing ownership and the approved server namespace both apply.
                if previous and previous.get("host_id") != cmd["host_id"]:
                    raise Fault("installation_id_owned_by_other_host", 403)
                if cmd["action"] == "configure":
                    adapter = payload.get("adapter")
                    target = (installation_id(adapter, cmd["host_id"], payload.get("profile"))
                              if self.s.adapter_known(cmd["host_id"], adapter) else None)
                    expected_resources = payload.get("resource_keys", previous.get("resource_keys", []))
                    declared_row = (self.s.declared_row(cmd["host_id"], adapter)
                                    or self.s.execution.host_declared(cmd["host_id"], adapter)
                                    or {})
                    capacity = payload.get("max_concurrency", previous.get("max_concurrency",
                                 declared_row.get("concurrency",
                                 ADAPTER_CATALOG.get(adapter, {}).get("concurrency", 1))))
                    expected_cwd = payload.get("cwd")
                else:
                    target = payload.get("installation_id")
                    adapter = previous.get("adapter")
                    expected_resources = previous.get("resource_keys", [])
                    capacity = previous.get("max_concurrency", 1)
                    expected_cwd = previous.get("cwd")
                if (not target or item["id"] != target or item["adapter"] != adapter
                        or (payload.get("installation_id") and payload["installation_id"] != target)):
                    raise Fault("installation_approval_mismatch", 403)
                from pathlib import PurePosixPath, PureWindowsPath
                cwd = item["cwd"]
                if (not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\x00" in cwd
                        or not (PurePosixPath(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute())):
                    raise Fault("invalid_installation_cwd", 400)
                if expected_cwd:
                    paths = ntpath if PureWindowsPath(cwd).is_absolute() else posixpath
                    if (not isinstance(expected_cwd, str)
                            or paths.normcase(paths.normpath(cwd)) != paths.normcase(paths.normpath(expected_cwd))):
                        raise Fault("installation_approval_mismatch", 403)
                resources = inst.get("resource_keys", [])
                concurrency = inst.get("max_concurrency", capacity)
                if not isinstance(resources, list) or resources != expected_resources:
                    raise Fault("installation_resource_policy_mismatch", 403)
                if type(concurrency) is not int or not 1 <= concurrency <= min(capacity, 16):
                    raise Fault("invalid_installation_concurrency", 400)
                item["endpoint"] = payload.get("endpoint", "") if cmd["action"] == "configure" else previous.get("endpoint", "")
                item.update(argv=["device-owned-command"], enabled=True, risk="write", enforces_read_only=False,
                            resource_keys=resources,
                            max_concurrency=concurrency,
                            prompt_mode="stdin")
                if cmd["action"] == "configure" and payload.get("auth") == "api_key":
                    item["enabled"] = False
                if cmd["action"] == "credential_delete":
                    item["enabled"] = False
                # Routing metadata is operator-owned on the cloud copy. A device
                # report replaces execution fields and must not clear or replace it.
                for key in ("priority", "use_when"):
                    if key in previous:
                        item[key] = previous[key]
                if cmd["action"] == "configure" and item["adapter"] not in ADAPTER_CATALOG:
                    declared = self.s.declared_row(cmd["host_id"], item["adapter"]) or {}
                    for key in ("priority", "use_when"):
                        if key not in item and key in declared:
                            item[key] = declared[key]
                    self.s._stamp_workspace_key(item)
                # The approved profile is what the installation id was derived from.
                # A rebuild that drops it makes the next configure compute a different id.
                profile = payload.get("profile") if cmd["action"] == "configure" else None
                if not profile:
                    profile = previous.get("profile")
                if profile:
                    item["profile"] = profile
                # Prepare once. DB writes are staged; publication is deferred until the
                # OUTERMOST transaction commits (a later report can still roll back).
                candidate["installations"] = [i for i in candidate.get("installations", []) if i["id"] != item["id"]] + [item]
                self.s.commit_managed_config(candidate)
                from .connector import sanitize_manifest
                probe_src = result.get("probe") if isinstance(result.get("probe"), dict) else {}
                probe = {k: probe_src.get(k) for k in ("available", "version", "os", "supervision", "policy_digest", "authorization_digest")}
                manifest = sanitize_manifest(probe_src.get("manifest"))
                if manifest:
                    probe["manifest"] = manifest
                self.store.put("probe", dict(probe, id=item["id"], at=time.time()))
                if isinstance(result.get("probe"), dict):
                    probe_view = {"available": probe.get("available") is True, "probed": True}
                    reason = probe_src.get("reason")
                    if isinstance(reason, str) and reason:
                        probe_view["reason"] = reason
                    safe["probe"] = probe_view
                if cmd["action"] == "configure":
                    self.s._drop_pending_device(item["id"])
                else:
                    self.s._persist_pending_device_grant(item["id"])
                safe["installation"] = item
                if req and payload.get("auth") == "api_key" and cmd["action"] == "configure":
                    if req.get("bot_id"):
                        self.request(req["bot_id"], req["work_id"], {"action": "credential",
                                     "host_id": cmd["host_id"], "installation_id": item["id"]})
                        safe["next"] = "在专用窗口输入密钥"
                    else:
                        safe["next"] = "密钥尚未录入：该执行器已注册但保持停用，请创建凭据录入请求完成录入"
            if req:
                req["status"] = "failed" if safe.get("error") else "done"
                self.store.put("setup_request", req)
            bot_id = req["bot_id"] if req else payload.get("bot_id")
            work_id = req["work_id"] if req else payload.get("work_id")
            if bot_id and work_id:
                work = self.store.get("work", work_id)
                # A setup result is a fresh causal entry point; give it a root so the
                # setup_result turn is budgeted like any other model turn.
                root = self.s._new_root("operator", "setup:" + str(cmd.get("id", "")))["id"]
                self.s.new_turn(bot_id, work, safe, "setup_result", "device", root_request_id=root)
