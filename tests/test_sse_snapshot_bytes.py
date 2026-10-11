"""S5 regressions for mobile bytes: snapshot gzip and stream suffixes.

The durable snapshot stays the authority. These tests measure bytes; they do
not claim a handset or a 3,002-Work production database.
"""
import gzip
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pangpang.http import live_stream_frame, serve
from pangpang.service import Service
from pangpang.store import encode
from test_bot_runtime import config


class StreamFrameTests(unittest.TestCase):
    def test_a_growing_reply_sends_suffixes_and_a_restart_sends_the_full_text(self):
        text = ""
        sent = ""
        sent_rev = -1
        delta_bytes = 0
        full_bytes = 0
        for i in range(1, 201):
            text += "token "
            frame = live_stream_frame("turn-1", {"text": text, "rev": i}, sent, sent_rev)
            delta_bytes += len(encode(frame).encode())
            full_bytes += len(encode({"turn_id": "turn-1", "rev": i, "text": text}).encode())
            if i == 1:
                self.assertEqual(frame["text"], text)
                self.assertNotIn("delta", frame)
            else:
                self.assertEqual(frame["delta"], "token ")
                self.assertEqual(frame["base_rev"], i - 1)
                self.assertNotIn("text", frame)
            sent, sent_rev = text, i
        self.assertLessEqual(delta_bytes * 10, full_bytes, "delta=%s full=%s" % (delta_bytes, full_bytes))
        print("stream_delta=%d stream_full=%d ratio=%.2f" % (delta_bytes, full_bytes, full_bytes / delta_bytes))
        restarted = live_stream_frame("turn-1", {"text": "ok", "rev": 202}, sent, sent_rev)
        self.assertEqual(restarted["text"], "ok")
        self.assertNotIn("delta", restarted)


class SnapshotByteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = Service(config(root), root / "data")
        self.token = "x" * 32
        self.server = serve(self.app, self.token, port=0)
        self.origin = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        req = urllib.request.Request(
            self.origin + "/api/v1/login", data=json.dumps({"token": self.token}).encode(),
            headers={"Origin": self.origin, "Content-Type": "application/json", "Idempotency-Key": "login"})
        self.client.open(req).close()

    def tearDown(self):
        time.sleep(0.2)
        self.server.shutdown()
        self.server.server_close()
        self.app.store.close()
        self.tmp.cleanup()

    def _get(self, path, headers=None):
        req = urllib.request.Request(self.origin + path, headers=headers or {})
        with self.client.open(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()

    def test_snapshot_gzip_is_the_same_document_and_at_least_ten_times_smaller(self):
        for i in range(1000):
            self.app.store.put("work", {
                "id": "work-%04d" % i, "owner_bot_id": "bot-main", "goal": ("deliverable %04d " % i) + ("x" * 240),
                "status": "active", "kind": "conversation", "created": 1_700_000_000 + i,
            })
        status, _headers, plain = self._get("/api/v1/snapshot")
        self.assertEqual(status, 200)
        status, headers, wired = self._get("/api/v1/snapshot", {"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        inflated = gzip.decompress(wired)
        self.assertEqual(inflated, plain)
        self.assertLessEqual(len(wired) * 10, len(plain), "wire=%s plain=%s" % (len(wired), len(plain)))
        print("snapshot_plain=%d snapshot_gzip=%d ratio=%.2f" % (len(plain), len(wired), len(plain) / len(wired)))
        body = json.loads(plain)
        self.assertGreaterEqual(len(body["work"]), 1000)

    def test_generic_settings_post_stays_closed(self):
        req = urllib.request.Request(
            self.origin + "/api/v1/settings", data=json.dumps({"model_max_concurrency": 1}).encode(),
            headers={"Origin": self.origin, "Content-Type": "application/json", "Idempotency-Key": "settings"})
        try:
            self.client.open(req)
            self.fail("settings post must stay closed")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 410)
            payload = json.loads(error.read().decode())
            self.assertEqual(payload["code"], "use_guided_model_and_conversational_setup")


if __name__ == "__main__":
    unittest.main()
