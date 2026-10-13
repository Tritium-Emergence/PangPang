import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pangpang.service import Service
from pangpang.onboarding import empty_config
from pangpang.connector import Connector
from pangpang.local_setup import LocalSetup
from pangpang.store import Store, Fault, uid, encode
from pangpang.vault import Vault
from pangpang.redaction import Redactor
from pangpang.runtime import Runtime
from test_model_protocol import Endpoint
from test_bot_runtime import call


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'workspace';self.root.mkdir()
        self.app=Service(empty_config(),self.root.parent/"cloud")
    def tearDown(self):
        self.app.store.close();self.tmp.cleanup()

    def device(self):
        data=self.root.parent/"device";data.mkdir()
        store=Store(data/"connector.db");identity=uid("device")
        store.put("identity",{"id":"device","instance_id":identity});store.close()
        vault=Vault(data/"vault");pub=vault.public_key()
        code=self.app.onboarding.pairing_code()["code"]
        body={"code":code,"instance_id":identity,"public_key":pub,"name":"My PC","os":"Linux"}
        pair=self.app.onboarding.pair(body)
        vault.set("device-token",pair["token"]);vault.close()
        cfg={"host_id":pair["host_id"],"cloud_url":"http://127.0.0.1:1","credential_ref":"device-token",
             "hosts":[{"id":pair["host_id"],"transport":"local","os":"Linux","allowed_roots":[str(self.root)]}],
             "installations":[],"resources":[]}
        return Connector(cfg,data),body,pair

    def test_pair_registers_and_replay_is_bound_to_identity(self):
        c,body,pair=self.device()
        try:
            self.assertEqual(self.app.onboarding.pair(body),pair)
            self.assertEqual(self.app.broker.authenticate(pair["host_id"],"Bearer "+pair["token"]),pair["host_id"])
            with self.assertRaises(Fault):self.app.onboarding.pair(dict(body,instance_id=uid("device")))
            self.app.broker.revoke(pair["host_id"])
            with self.assertRaises(Fault):self.app.onboarding.pair(body)
            self.assertNotIn(pair["token"],encode(self.app.config))
        finally:c.close()

    def test_approved_configuration_and_device_sealed_key(self):
        c,body,pair=self.device()
        try:
            work=self.app.send("bot-main",{"text":"Connect Codex"},"setup")
            req=self.app.onboarding.request("bot-main",work["work_id"],{"host_id":c.host_id,
                "action":"configure","adapter":"codex","auth":"api_key"})
            self.assertEqual(len(c.execution.installations),0)
            self.app.onboarding.approve(req["request_id"])
            command=self.app.store.find("command",action="configure")[0]
            with patch.object(LocalSetup,"discover",return_value={"inventory":[{"adapter":"codex","binary":sys.executable}]}):
                c.execute(command)
            result=c.store.get("local_command",command["id"])["result"]
            self.assertNotIn("error",result,result)
            self.app.onboarding.report(command,result)
            self.assertEqual(len(self.app.execution.installations),1)
            self.assertFalse(next(iter(self.app.execution.installations.values()))["enabled"])
            secret_req=next(r for r in self.app.store.find("setup_request") if r["status"]=="needs_secret")
            target=self.app.onboarding.secret_target(secret_req["id"])
            key=serialization.load_der_public_key(base64.b64decode(target["public_key"]))
            aes=AESGCM.generate_key(bit_length=256);nonce=os.urandom(12);secret="TEST-SECRET-NEVER-IN-CHAT"
            sealed={"key":base64.b64encode(key.encrypt(aes,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))).decode(),
                    "nonce":base64.b64encode(nonce).decode(),
                    "ciphertext":base64.b64encode(AESGCM(aes).encrypt(nonce,secret.encode(),target["context"].encode())).decode()}
            self.app.onboarding.secret(secret_req["id"],{"sealed":sealed})
            cmd=self.app.store.find("command",action="credential_install")[0];c.execute(cmd)
            result=c.store.get("local_command",cmd["id"])["result"];self.assertNotIn("error",result,result)
            self.app.onboarding.report(cmd,result)
            self.assertTrue(next(iter(self.app.execution.installations.values()))["enabled"])
            inst=next(iter(c.execution.installations.values()))
            self.assertEqual(c.vault.get(inst["secret_refs"]["CODEX_API_KEY"]),secret)
            with self.assertRaises(Fault):
                LocalSetup(c).credential(dict(cmd["payload"], expected_policy_digest="stale-policy"))
            for record in [self.app.config,c.config,self.app.store.find("message"),self.app.store.find("command"),c.store.find("local_command")]:
                self.assertNotIn(secret,encode(record))
            self.assertNotIn(secret.encode(),(c.data/"vault"/"secrets.db").read_bytes())
            with self.assertRaises(Fault):self.app.onboarding.secret(secret_req["id"],{"sealed":sealed})
            with self.assertRaises(Exception):c.vault.unseal(sealed,"wrong-request")
        finally:c.close()

    def test_cli_endpoint_and_qwen_resource_policy_are_not_model_controlled(self):
        c,body,pair=self.device()
        try:
            work=self.app.send("bot-main",{"text":"Connect agent"},"security-setup")
            with self.assertRaises(Fault) as caught:
                self.app.onboarding.request("bot-main",work["work_id"],{"host_id":c.host_id,"action":"configure",
                    "adapter":"codex","auth":"api_key","endpoint":"https://attacker.example/v1"})
            self.assertEqual(caught.exception.code,"operator_owned_setup_field")
            with self.assertRaises(Fault) as caught:
                self.app.onboarding.request("bot-main",work["work_id"],{"host_id":c.host_id,"action":"configure",
                    "adapter":"codex","uses_qwen":True,"slot_acquire_args":["--exec","bad"],"slot_release_args":["release"]})
            self.assertEqual(caught.exception.code,"operator_owned_setup_field")
            req=self.app.onboarding.request("bot-main",work["work_id"],{"host_id":c.host_id,"action":"configure",
                "adapter":"codex","auth":"api_key"})
            self.app.onboarding.approve(req["request_id"],{"endpoint":"https://trusted.example/v1"})
            command=self.app.store.find("command",action="configure",newest=True,limit=1)[0]
            self.assertEqual(command["payload"]["endpoint"],"https://trusted.example/v1")
            self.assertEqual(command["payload"]["endpoint_source"],"operator")
            from pangpang.execution import ADAPTER_CATALOG
            migrated = {"binary": "legacy-executor", "argv": ["-p"], "concurrency": 2, "priority": 10,
                        "use_when": "迁移后的私人执行器。"}
            ADAPTER_CATALOG["legacy-executor"] = dict(migrated)
            try:
                with patch.object(LocalSetup,"discover",return_value={"inventory":[{"adapter":"legacy-executor","binary":sys.executable}]}), \
                     patch("pangpang.local_setup.shutil.which",return_value="/trusted/qwen-slot"):
                    with self.assertRaises(Fault) as rejected:
                        LocalSetup(c).configure({"adapter":"legacy-executor","uses_qwen":True,"slot_acquire_args":["anything"]})
                    self.assertEqual(rejected.exception.code,"operator_owned_setup_field")
                    LocalSetup(c).configure({"adapter":"legacy-executor","uses_qwen":True})
                # Capacity is the generic max_concurrency from the adapter row. A private
                # adapter name does not create a product resource.
                self.assertFalse([r for r in c.config.get("resources",[]) if r["key"]=="qwen-shared"])
                inst=c.execution.installations["legacy-executor-"+c.host_id]
                self.assertEqual(inst.get("resource_keys",[]),[])
                self.assertEqual(inst["max_concurrency"],2)
            finally:
                ADAPTER_CATALOG.pop("legacy-executor", None)
        finally:c.close()

    def test_model_setup_uses_real_pi_probe_and_persists_only_reference(self):
        endpoint=Endpoint(lambda p,n: call("setup_probe",value="pangpang-ready") if n==1 else {"content":"ready"})
        try:
            self.app.onboarding.model({"endpoint":endpoint.url,"model":"test","secret":"SETUP-KEY-NOT-IN-DB"})
            self.assertTrue(self.app.onboarding.status()["model"]["has_secret"])
            self.assertEqual(len(endpoint.received),2)
            self.assertNotIn("SETUP-KEY-NOT-IN-DB",encode(self.app.store.find("settings")))
            self.assertNotIn("SETUP-KEY-NOT-IN-DB",encode(endpoint.received))
        finally:endpoint.close()

    def test_switching_to_gpt_drops_inherited_vllm_params(self):
        # 旧 qwen profile 带 chat_template_kwargs；换到 gpt 模型时必须丢弃继承的 vllm 专用参数，
        # 否则 gpt 端点报 400。且第二验证轮即使又调工具/无 content 也应通过（reasoning 模型行为）。
        old = next(iter(self.app.profiles.values()))
        old["model"] = "qwen3.8-27b"
        from pangpang.service import normalize_profile
        old["generation_settings"] = {"chat_template_kwargs": {"enable_thinking": True}}
        normalize_profile(old)
        self.assertIn("chat_template_kwargs", old.get("generation_settings", {}))
        # gpt 模型：两轮都调工具（第二轮不出 content），模拟 gpt-5 行为
        endpoint = Endpoint(lambda p, n: call("setup_probe", value="pangpang-ready"))
        try:
            old["endpoint"] = endpoint.url # 同端点，保留凭据路径
            self.app.onboarding.model({"endpoint": endpoint.url, "model": "gpt-5-mini", "no_auth": True})
            saved = next(iter(self.app.profiles.values()))
            self.assertEqual(saved["model"], "gpt-5-mini")
            self.assertNotIn("chat_template_kwargs", saved.get("generation_settings", {}))
            for payload in endpoint.received:
                self.assertNotIn("chat_template_kwargs", payload)
                self.assertIn("max_completion_tokens", payload)
                self.assertNotIn("max_tokens", payload)
        finally:
            endpoint.close()

    def test_legacy_environment_key_is_preserved_when_unchanged(self):
        from pangpang.pi_runtime import PiModel
        p = next(iter(self.app.profiles.values()))
        p.update(endpoint="https://legacy.example/v1", model="test", credential_env="LEGACY_TEST_KEY")
        seen = []
        def complete(model, profile, messages, tools):
            seen.append(model.credentials(profile))
            return (call("setup_probe", value="pangpang-ready") if len(seen) == 1 else {"content":"ready"}), {}
        with patch.dict(os.environ, {"LEGACY_TEST_KEY":"legacy-private-value"}), patch.object(PiModel, "complete", complete):
            self.app.onboarding.model({"endpoint":p["endpoint"], "model":"test"})
        self.assertEqual(seen, ["legacy-private-value", "legacy-private-value"])
        self.assertEqual(next(iter(self.app.profiles.values()))["credential_env"], "LEGACY_TEST_KEY")
        self.assertNotIn("legacy-private-value", encode(self.app.store.find("settings")))

    def test_endpoint_change_does_not_reuse_old_key_or_fall_back(self):
        from pangpang.pi_runtime import PiModel
        p = next(iter(self.app.profiles.values()))
        p.update(endpoint="https://old.example/v1", model="test", credential_ref="old-key")
        self.app.vault.set("old-key", "OLD-PRIVATE-KEY")
        with self.assertRaises(Fault):
            self.app.onboarding.model({"endpoint":"https://new.example/v1","model":"test"})
        self.app.onboarding.revoke_model_key()
        self.assertFalse(self.app.vault.has("old-key"))
        with patch.dict(os.environ, {"OPENAI_API_KEY":"ambient-key"}):
            with self.assertRaises(Fault):
                PiModel(self.app).credentials(next(iter(self.app.profiles.values())))

    def test_redaction_across_chunk_boundaries(self):
        secret="BOUNDARY-SECRET"
        for split in range(len(secret)+1):
            r=Redactor([secret])
            out=r.feed(("prefix "+secret[:split]).encode())+r.feed((secret[split:]+" suffix").encode())+r.feed(b"",final=True)
            self.assertEqual(out,b"prefix [REDACTED] suffix")

    def test_unrelated_unknown_does_not_block_cloud_conversation(self):
        self.app.store.put("task",{"id":"task-unknown","status":"unknown","installation_id":"missing"})
        class Model:
            def complete(self,*args):return {"content":"我还在，可以继续安排工作。"},{}
        sent=self.app.send("bot-main",{"text":"还能对话吗"},"live")
        Runtime(self.app,Model()).step()
        self.assertEqual(self.app.store.get("turn",sent["turn_id"])["status"],"done")
        self.assertFalse(self.app.task_step())
        self.assertEqual(self.app.store.get("task","task-unknown")["status"],"unknown")

    def test_report_cannot_overwrite_installation_owned_by_other_host(self):
        # R2: a paired device must not hijack an installation id that already
        # belongs to a different host (which would reroute that executor's tasks).
        self.app.config["hosts"] = [
            {"id": "host-A", "transport": "local", "os": "Linux", "allowed_roots": [str(self.root)]},
            {"id": "host-B", "transport": "local", "os": "Linux", "allowed_roots": [str(self.root)]}]
        self.app.config["installations"] = [{"id": "codex-local", "host_id": "host-A", "adapter": "codex",
            "argv": ["device-owned-command"], "cwd": str(self.root), "enabled": True, "prompt_mode": "stdin",
            "risk": "write", "max_concurrency": 1, "resource_keys": []}]
        from pangpang.execution import Execution
        self.app.execution = Execution(self.app.config, self.app.data)
        self.app.execution.broker = self.app.broker
        cmd = {"id": "cmd1", "action": "configure", "host_id": "host-B", "payload": {}}
        result = {"installation": {"id": "codex-local", "host_id": "host-B", "adapter": "codex", "cwd": str(self.root)},
                  "probe": {"available": True, "version": "x"}}
        with self.assertRaises(Fault) as cm:
            self.app.onboarding.report(cmd, result)
        self.assertEqual(cm.exception.code, "installation_id_owned_by_other_host")
        # host-A's record is untouched
        self.assertEqual(self.app.execution.installations["codex-local"]["host_id"], "host-A")

    def test_report_with_unknown_resource_key_is_rejected_and_not_persisted(self):
        # R2: a report referencing a non-existent resource used to return 200 and
        # land in the DB, then break the NEXT service load. Validate the whole
        # candidate config first; reject and leave DB + live config intact.
        self.app.config["hosts"] = [{"id": "host-A", "transport": "local", "os": "Linux", "allowed_roots": [str(self.root)]}]
        from pangpang.execution import Execution
        self.app.execution = Execution(self.app.config, self.app.data)
        self.app.execution.broker = self.app.broker
        cmd = {"id": "cmd2", "action": "configure", "host_id": "host-A", "payload": {}}
        result = {"installation": {"id": "codex-local", "host_id": "host-A", "adapter": "codex",
                  "cwd": str(self.root), "resource_keys": ["no-such-resource"]},
                  "probe": {"available": True, "version": "x"}}
        with self.assertRaises(Fault):
            self.app.onboarding.report(cmd, result)
        self.assertNotIn("codex-local", self.app.execution.installations)
        # DB must not carry the poisoned installation (either no settings record at
        # all, or a record without it — both mean "not persisted").
        try:
            saved = self.app.store.get("settings", "managed")["config"]
            self.assertFalse(any(i["id"] == "codex-local" for i in saved.get("installations", [])))
        except Fault:
            pass

if __name__=="__main__":unittest.main()
