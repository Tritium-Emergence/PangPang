"""Guided setup and asynchronous, approved device configuration."""
import base64
import copy
import hashlib
import ntpath
import posixpath
import re
import secrets
import time
from urllib.parse import urlsplit
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from .store import Fault, digest, uid, encode
from .execution import ADAPTER_CATALOG, installation_id, validate_extra_argv, validate_profile_env
from .model_config import DEFAULT_OUTPUT_TOKENS, FIELDS, setup_profile


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


class Onboarding:
    def __init__(self, service):
        self.s, self.store, self.vault = service, service.store, service.vault

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
                # would fail every turn that carries it.
                try:
                    message, _ = PiModel(self.s).complete(p, [
                        {"role": "system", "content": "Look at the image, then call setup_probe with value pangpang-ready."},
                        {"role": "user", "content": "Here is the image.",
                         "images": [{"data": PROBE_PNG, "mime": "image/png"}]}], [probe])
                except Fault:
                    raise Fault("model_vision_unverified") from None
                if [c["function"]["name"] for c in message.get("tool_calls", [])] != ["setup_probe"]:
                    raise Fault("model_vision_unverified")
            candidate["models"] = [p]
            self.s.commit_managed_config(candidate, base_revision=base)
        except Exception:
            # The new secret is only referenced once the config commits (G).
            if new_ref:
                self.vault.delete(new_ref)
            raise
        if old_ref and old_ref != p.get("credential_ref"):
            self.vault.delete(old_ref)
        return {"configured": True, "tool_call_verified": True}

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
                host = {"id": host_id, "name": str(body.get("name", host_id))[:80],
                        "os": str(body.get("os", ""))[:40], "transport": "connector",
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

    def request(self, bot_id, work_id, body):
        if self.s.bot(bot_id)["kind"] != "main":
            raise Fault("main_bot_required", 403)
        host_id = body["host_id"]
        if self.s.execution.hosts.get(host_id, {}).get("transport") != "connector":
            raise Fault("connector_host_required")
        action = body["action"]
        if action == "discover":
            cmd = self.s.broker.enqueue(host_id, "discover", {"bot_id": bot_id, "work_id": work_id})
            return {"command_id": cmd["id"], "yielded": True}
        if action not in {"configure", "credential", "delete_credential"}:
            raise Fault("invalid_setup_action")
        if action == "configure":
            if body.get("adapter") not in ADAPTER_CATALOG:
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
            if adapter not in ADAPTER_CATALOG:
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

    def checked_request(self, identity, status):
        req = self.store.get("setup_request", identity)
        if req["status"] != status or req["expires"] < time.time():
            raise Fault("setup_request_expired_or_used", 409)
        if self.store.get("device", req["host_id"]).get("revoked"):
            raise Fault("device_revoked", 403)
        return req

    def approve(self, identity, body=None):
        body = body or {}
        if set(body) - {"endpoint", "profile", "env", "extra_argv"}:
            raise Fault("invalid_setup_approval", 400)
        with self.store.atomic():
            req = self.checked_request(identity, "needs_approval")
            proposal = dict(req["proposal"])
            action = "configure" if proposal["action"] == "configure" else "credential_delete"
            if action == "configure":
                # This value comes from the authenticated operator UI, never
                # from model tool arguments. Blank means use the CLI default.
                proposal["endpoint"] = endpoint(str(body.get("endpoint", "")))
                proposal["endpoint_source"] = "operator"
                profile = body.get("profile")
                env = body.get("env")
                if env is not None:
                    validate_profile_env(env)
                extra_argv = body.get("extra_argv")
                if extra_argv is not None:
                    validate_extra_argv(proposal["adapter"], extra_argv)
                # Same canonical namespace as LocalSetup, derived by the server;
                # profile is operator-supplied at approval, never model-supplied.
                proposal["installation_id"] = installation_id(proposal["adapter"], req["host_id"], profile)
                if profile:
                    proposal["profile"] = profile
                if env:
                    proposal["env"] = dict(env)
                if extra_argv:
                    proposal["extra_argv"] = list(extra_argv)
                previous = next((i for i in self._config().get("installations", [])
                                 if i["id"] == proposal["installation_id"]), {})
                if previous and previous["host_id"] != req["host_id"]:
                    raise Fault("installation_id_owned_by_other_host", 403)
                proposal["resource_keys"] = list(previous.get("resource_keys", []))
                proposal["max_concurrency"] = previous.get("max_concurrency", ADAPTER_CATALOG[proposal["adapter"]]["concurrency"])
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

    def report(self, cmd, result):
        with self.store.atomic():
            if cmd["action"] not in {"discover", "configure", "credential_install", "credential_delete"}:
                return
            payload = cmd["payload"]
            req = None
            if payload.get("request_id"):
                req = self.store.get("setup_request", payload["request_id"])
            safe = {"action": cmd["action"], "host_id": cmd["host_id"]}
            if result.get("error"):
                safe["error"] = "device_setup_failed"
            elif cmd["action"] == "discover":
                items = [{k: str(x[k])[:500] for k in ("adapter", "binary", "version") if k in x}
                         for x in result.get("inventory", [])[:20] if isinstance(x, dict)]
                self.store.put("inventory", {"id": cmd["host_id"], "items": items})
                safe["inventory"] = items
            else:
                inst = result.get("installation", {})
                if (not isinstance(inst, dict) or not all(isinstance(inst.get(k), str) for k in ("id", "host_id", "adapter"))
                        or inst.get("host_id") != cmd["host_id"] or inst.get("adapter") not in ADAPTER_CATALOG):
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
                              if adapter in ADAPTER_CATALOG else None)
                    expected_resources = payload.get("resource_keys", previous.get("resource_keys", []))
                    capacity = payload.get("max_concurrency", previous.get("max_concurrency", ADAPTER_CATALOG.get(adapter, {}).get("concurrency", 1)))
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
                # Prepare once. DB writes are staged; publication is deferred until the
                # OUTERMOST transaction commits (a later report can still roll back).
                candidate["installations"] = [i for i in candidate.get("installations", []) if i["id"] != item["id"]] + [item]
                self.s.commit_managed_config(candidate)
                probe = {k: result.get("probe", {}).get(k) for k in ("available", "version", "os", "supervision", "policy_digest")}
                self.store.put("probe", dict(probe, id=item["id"], at=time.time()))
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
