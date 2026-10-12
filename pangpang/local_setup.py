"""Fixed device adapters and locally enforced workspace policy."""
import copy
import shutil
from .execution import (Execution, ADAPTER_CATALOG, binary_matches_pin, fetch_pinned_pi,
                       installation_id, materialize_executor_env, pi_models_api, secret_env_name,
                       validate_extra_argv, validate_profile_env, write_pi_provider_config, _PI_BASE_URL_ENV)
from .runner import checked, probe
from .store import Fault, digest


class LocalSetup:
    def __init__(self, connector):
        self.c = connector

    def idle(self):
        if any(self.c.store.find("local_run", status=s, limit=1) for s in ("launching", "active")):
            raise Fault("device_busy_configuration_waits")

    def discover(self):
        def inventory():
            items = []
            pinned_absent = False
            for adapter, cat in ADAPTER_CATALOG.items():
                binary = shutil.which(cat["binary"])
                if cat.get("pinned") and not binary:
                    pinned_absent = True
                if not binary or not binary_matches_pin(cat, binary):
                    continue
                try:
                    version = probe({"argv": [binary]}).get("version", "")[:500]
                except Exception:
                    version = "version_unverified"
                items.append({"adapter": adapter, "binary": binary, "version": version})
            return items, pinned_absent

        items, pinned_absent = inventory()
        # Fresh install, no user CLI: the pinned binary is the only executor.
        # Anyone who already has qwen/claude/codex never downloads it.
        if pinned_absent and not items and fetch_pinned_pi():
            items, _pinned_absent = inventory()
        return {"inventory": items}

    def save(self, candidate):
        """Durable write first, then publish (G): validate by building the
        Execution, CAS against the revision this process loaded, and only then
        swap the live config. A failed write leaves memory untouched."""
        execution = Execution(candidate, self.c.data)
        row = self.c.store.put("settings", {"id": "managed", "config": candidate},
                               expected=self.c.config_revision)
        self.c.config, self.c.execution, self.c.config_revision = candidate, execution, row["revision"]

    def info(self, identity):
        from .connector import authorization_digest, execution_manifest, policy_digest
        e = self.c.execution.installations[identity]
        p = self.c.execution.probe(identity)
        p["policy_digest"] = policy_digest(self.c.config, identity)
        p["authorization_digest"] = authorization_digest(self.c.config, identity)
        p["manifest"] = execution_manifest(self.c.config, identity)
        # Report resource_keys + max_concurrency so the cloud onboarding preserves
        # the device-owned executor capacity; without them the cloud
        # falls back to defaults and can over-admit a capacity-1 installation.
        inst = {k: e[k] for k in ("id", "host_id", "adapter", "cwd")}
        inst["resource_keys"] = list(e.get("resource_keys", []))
        inst["max_concurrency"] = int(e.get("max_concurrency", 1))
        return {"installation": inst,
                "probe": p, "authentication": "not_verified_by_version_probe"}

    def configure(self, body):
        self.idle()
        if any(k in body for k in ("slot_acquire_args", "slot_release_args")):
            raise Fault("operator_owned_setup_field", 400)
        adapter = body["adapter"]
        found = next((x for x in self.discover()["inventory"] if x["adapter"] == adapter), None)
        if not found:
            raise Fault("cli_not_installed")
        host = self.c.execution.hosts[self.c.host_id]
        from .runner import check_control_separation
        check_control_separation(host["allowed_roots"], [self.c.data])
        cwd = str(checked(body.get("cwd") or host["allowed_roots"][0], host["allowed_roots"]))
        profile = body.get("profile")
        identity = installation_id(adapter, self.c.host_id, profile)
        old = self.c.execution.installations.get(identity, {})
        env = body.get("env", old.get("env"))
        if env is not None:
            validate_profile_env(env)
        extra_argv = body.get("extra_argv", old.get("extra_argv"))
        if extra_argv is not None:
            validate_extra_argv(adapter, extra_argv)
        cat = ADAPTER_CATALOG[adapter]
        e = {"id": identity, "host_id": self.c.host_id, "adapter": adapter, "enabled": True,
             "argv": [found["binary"], *cat["argv"]],
             "cwd": cwd, "prompt_mode": "stdin", "risk": "write", "resource_keys": old.get("resource_keys", []),
             "max_concurrency": old.get("max_concurrency", cat["concurrency"]), "env_allowlist": []}
        if env:
            e["env"] = dict(env)
        if extra_argv:
            e["extra_argv"] = list(extra_argv)
        # SIS: executor capacity is the GENERIC max_concurrency,
        # not a product-level qwen special case. PangPang does not hardcode "qwen must
        # hold qwen-shared". A real shared external device lock is legitimate but must
        # come from operator/device policy as explicit resource_keys (preserved above
        # from `old`), never inferred from the adapter name.
        extra = materialize_executor_env(cat, self.c.data)
        if body.get("endpoint"):
            from .onboarding import endpoint
            url = endpoint(body["endpoint"])
            if cat.get("models_json"):
                extra = dict(extra or {})
                extra[_PI_BASE_URL_ENV] = url
                # approve() copies the Bot profile's model and api onto this payload.
                # A missing api is the Bot default (openai-completions).
                model_id = body.get("model") or ""
                api = pi_models_api({"api": body["api"]} if body.get("api") else {})
                if not api:
                    raise Fault("pi_api_unsupported")
                if not model_id:
                    raise Fault("model_endpoint_and_name_required")
                write_pi_provider_config(extra["PI_CODING_AGENT_DIR"], url, secret_env_name(adapter),
                                         model_id, api)
            else:
                key = "OPENAI_BASE_URL" if adapter == "codex" else "ANTHROPIC_BASE_URL"
                extra = {key: url}
        if extra:
            e["extra_env"] = extra
        if body.get("auth") == "api_key":
            env_name = secret_env_name(adapter)
            if e.get("extra_env") == old.get("extra_env") and old.get("secret_refs"):
                e["secret_refs"] = old["secret_refs"]
            else:
                e["secret_refs"] = {env_name: "pending-" + digest([identity, e.get("extra_env")])}
        elif old.get("secret_refs") and e.get("extra_env") != old.get("extra_env"):
            raise Fault("endpoint_change_requires_new_api_key")
        candidate = copy.deepcopy(self.c.config)
        candidate["installations"] = [x for x in candidate["installations"] if x["id"] != identity] + [e]
        self.save(candidate)
        return self.info(identity)

    def credential(self, body, delete=False):
        self.idle()
        e = self.c.execution.installations[body["installation_id"]]
        from .connector import policy_digest
        if body.get("expected_policy_digest") != policy_digest(self.c.config, e["id"]):
            raise Fault("credential_target_changed_request_new_window")
        env = secret_env_name(e["adapter"])
        ref = "cli-" + digest([e["id"], env])
        candidate = copy.deepcopy(self.c.config)
        target = next(x for x in candidate["installations"] if x["id"] == e["id"])
        target.setdefault("secret_refs", {})[env] = ref
        if delete:
            self.save(candidate)
            self.c.vault.delete(ref) # only after the durable config commit (G)
            return self.info(e["id"])
        # `ref` is stable on purpose: policy_digest covers secret_refs, and a new
        # ref per rotation would fail every task already submitted against the old
        # digest (connector.start: local_policy_changed_probe_and_resubmit). So the
        # swap is rolled back instead: if the config commit fails, the previous
        # secret (or its absence) is restored and the executor never sees it (G).
        value = self.c.vault.unseal(body["sealed"], body["request_id"])
        previous = self.c.vault.get(ref) if self.c.vault.has(ref) else None
        self.c.vault.set(ref, value)
        try:
            self.save(candidate)
        except BaseException:
            if previous is None:
                self.c.vault.delete(ref)
            else:
                self.c.vault.set(ref, previous)
            raise
        return self.info(e["id"])
