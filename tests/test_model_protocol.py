import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from test_bot_runtime import config, call
from pangpang.runtime import Runtime
from pangpang.service import Service


class Endpoint:
    def __init__(self, respond):
        self.received = []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.received.append(payload)
                message = respond(payload, len(outer.received))
                if isinstance(message, int):
                    self.send_response(message); self.send_header("Content-Length", "0"); self.end_headers(); return
                calls = message.get("tool_calls", [])
                delta = dict(message)
                if calls: delta["tool_calls"] = [dict(c, index=i) for i, c in enumerate(calls)]
                frame = {"id": "test", "object": "chat.completion.chunk", "model": "test",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if calls else "stop"}]}
                data = ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode()
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data))); self.end_headers()
                try: self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


class ProtocolTests(unittest.TestCase):
    def test_real_pi_executes_tool_and_continues(self):
        endpoint = Endpoint(lambda p, n: call("create_bot", name="协议测试", role="编辑") if n == 1 else {"content": "岗位已建立。"})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); cfg=config(root); cfg["models"][0]["endpoint"]=endpoint.url
                app=Service(cfg,root/"data")
                try:
                    sent=app.send("bot-main", {"text":"创建编辑岗位"}, "protocol")
                    Runtime(app).step()
                    turn=app.store.get("turn",sent["turn_id"])
                    self.assertEqual(turn["status"],"done",turn.get("error"))
                    self.assertEqual(len(app.store.find("bot")),2)
                    self.assertTrue(endpoint.received[0]["stream"])
                    self.assertIn("创建编辑岗位", str(endpoint.received[0]["messages"]))
                    self.assertEqual(endpoint.received[1]["messages"][-1]["role"],"tool")
                finally: app.store.close()
        finally: endpoint.close()

    def test_no_hidden_retry_or_false_success(self):
        endpoint=Endpoint(lambda p,n:429)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);cfg=config(root);cfg["models"][0]["endpoint"]=endpoint.url
                app=Service(cfg,root/"data")
                try:
                    sent=app.send("bot-main",{"text":"hello"},"error")
                    Runtime(app).step()
                    self.assertEqual(len(endpoint.received),1)
                    self.assertEqual(app.store.get("turn",sent["turn_id"])["status"],"blocked")
                    self.assertEqual(len(app.store.find("bot")),1)
                finally:app.store.close()
        finally:endpoint.close()


    def test_streaming_delta_reaches_service_and_cleans_up(self):
        # End-to-end (real node worker): a text response must drive record_delta with
        # accumulated text + monotonic rev, and finish_delta must clear it afterwards
        # (turn done -> buffer swept). Proves the no-id delta does NOT desync the rpc
        # loop (the turn still completes and persists its assistant message).
        endpoint = Endpoint(lambda p, n: {"content": "你好，我在。"})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); cfg=config(root); cfg["models"][0]["endpoint"]=endpoint.url
                app=Service(cfg,root/"data")
                try:
                    seen=[]
                    orig=app.record_delta
                    def spy(turn_id, attempt, text, rev):
                        seen.append((turn_id, attempt, text, rev)); return orig(turn_id, attempt, text, rev)
                    app.record_delta=spy
                    sent=app.send("bot-main", {"text":"在吗"}, "stream")
                    Runtime(app).step()
                    turn=app.store.get("turn",sent["turn_id"])
                    self.assertEqual(turn["status"],"done",turn.get("error"))
                    # deltas arrived for THIS turn, rev strictly increasing
                    mine=[d for d in seen if d[0]==sent["turn_id"]]
                    self.assertTrue(mine, "expected at least one streaming delta")
                    revs=[d[3] for d in mine]
                    self.assertEqual(revs, sorted(revs))
                    self.assertEqual(len(set(revs)), len(revs))
                    # final accumulated text is the answer
                    self.assertIn("你好", mine[-1][2])
                    # buffer cleaned up after finish_delta (done tombstone swept or gone)
                    snap=app.snapshot_deltas("bot-main")
                    live=[k for k,v in snap.items() if k[0]==sent["turn_id"] and not v.get("done")]
                    self.assertEqual(live, [], "live (non-done) delta must not linger after turn done")
                finally: app.store.close()
        finally: endpoint.close()


if __name__=="__main__":unittest.main()
