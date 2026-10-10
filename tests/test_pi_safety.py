import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pangpang.service import Service
from pangpang.runtime import Runtime
from pangpang.pi_runtime import PiModel
from pangpang.store import Fault, encode
from test_bot_runtime import config, call
from test_model_protocol import Endpoint

class PiSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.runtime = Runtime(self.app)
    def tearDown(self):
        self.app.store.close(); self.tmp.cleanup()
    def turn(self):
        sent = self.app.send("bot-main", {"text": "测试可靠执行"}, "safety")
        return self.app.store.get("turn", sent["turn_id"])
    def test_action_commit_before_result_ack_never_repeats(self):
        t = self.turn(); c = call("create_bot", name="可靠岗位", role="编辑")["tool_calls"][0]
        t["history"] = self.runtime.context(t) + [{"role": "assistant", "tool_calls": [c]}]
        self.app.store.put("turn", t)
        original = self.app.store.put
        def fail_ack(kind, value, *args, **kw):
            if kind == "turn": raise RuntimeError("lost acknowledgment")
            return original(kind, value, *args, **kw)
        with patch.object(self.app.store, "put", side_effect=fail_ack):
            with self.assertRaises(RuntimeError): self.runtime.durable_tool(t, c)
        self.assertEqual(len(self.app.store.find("bot")), 2)
        self.runtime.durable_tool(t, c)
        self.assertEqual(len(self.app.store.find("bot")), 2)
    def test_model_turn_acquires_no_executor_resource(self):
        # a model turn must NOT acquire any executor resource
        # lock — the model pool is independent of the executor pool, so a busy
        # executor cannot freeze conversations. model_begin acquires nothing and no
        # claim is created for the turn.
        t = self.turn()
        def exchange(profile, history, tools, handler, **kwargs):
            handler({"type": "context"})
            handler({"type": "model_begin"})
            handler({"type": "assistant", "stop": "error"})
        with patch.object(PiModel, "exchange", side_effect=exchange), patch("pangpang.pi_runtime.command", return_value=0) as command:
            with self.assertRaises(Fault): self.runtime.model.run(self.runtime, t, next(iter(self.app.profiles.values())))
            # no acquire/release command was ever issued for a model turn
            self.assertEqual(command.call_count, 0)
        self.assertEqual(self.app.store.find("claim"), [])
    def test_projection_cannot_replace_full_state_without_read(self):
        t = self.turn()
        with self.assertRaises(Fault):
            self.runtime.invoke(t, "propose_state_patch", {"revision": 1, "facts": []})
        self.runtime.invoke(t, "read_state", {})
        state = self.app.store.get("state", "bot-main")
        result = self.runtime.invoke(t, "propose_state_patch", {"revision": state["revision"], "facts": []})
        self.assertGreater(result["revision"], state["revision"])
    def test_invalid_nested_arguments_have_no_side_effect(self):
        t = self.turn()
        c = call("create_bot", name={"bad": "type"}, role="编辑")["tool_calls"][0]
        result = self.runtime.durable_tool(t, c)
        self.assertIn("error", result)
        self.assertEqual(len(self.app.store.find("bot")), 1)
    def test_cancel_waiting_model_stops_worker_without_business_action(self):
        import threading
        entered, finish = threading.Event(), threading.Event()
        def respond(p, n):
            entered.set(); finish.wait(8)
            return call("create_bot", name="不应创建", role="编辑")
        endpoint = Endpoint(respond)
        worker = None
        try:
            next(iter(self.app.profiles.values()))["endpoint"] = endpoint.url
            t = self.turn()
            worker = threading.Thread(target=self.runtime.step)
            worker.start()
            self.assertTrue(entered.wait(60))
            current = self.app.store.get("turn", t["id"])
            current["cancel_requested"] = True
            self.app.store.put("turn", current)
            worker.join(4)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.app.store.get("turn", t["id"])["status"], "cancelled")
            self.assertEqual(len(self.app.store.find("bot")), 1)
        finally:
            finish.set()
            if worker: worker.join(10)
            endpoint.close()

    def test_mixed_waiting_batch_stops_after_durable_results(self):
        first = call("setup_agent", host_id="device-test", action="discover")["tool_calls"][0]
        first["id"] = "call-wait"
        second = call("create_bot", name="批次岗位", role="编辑")["tool_calls"][0]
        second["id"] = "call-create"
        endpoint = Endpoint(lambda p,n: {"tool_calls": [first, second], "content": ""})
        try:
            self.app.execution.hosts["device-test"] = {"id": "device-test", "transport": "connector"}
            next(iter(self.app.profiles.values()))["endpoint"] = endpoint.url
            t = self.turn(); self.runtime.step()
            # a yielded turn is terminal 'done' (waiting now lives on the
            # Work, not the turn). The safety property under test is unchanged: the
            # turn stopped after the durable setup_agent result and did NOT make a
            # second model call to act on the create_bot in the same batch.
            self.assertEqual(self.app.store.get("turn", t["id"])["status"], "done")
            self.assertEqual(len(endpoint.received), 1)
            self.assertEqual(len(self.app.store.find("bot")), 2)
            self.assertEqual(len(self.app.store.find("command")), 1)
        finally: endpoint.close()

if __name__ == "__main__": unittest.main()
