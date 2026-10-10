# Contract (coordinator restart) — durable state survives a true process
# restart: a Service reopened on the SAME data directory rehydrates bots, work and
# artifacts from the store. Playwright's fresh-context test covers the client side;
# this covers the server side without complicating the browser gate with process
# control (precedent: tests/test_context.py reopening a Service on the same dir).
import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import uid


class WebRestartTests(unittest.TestCase):
    def test_coordinator_restart_rehydrates_durable_state(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "workspace"; root.mkdir()
        data = root.parent / "data"
        app = Service(config(root), data)
        bot = app.create_bot({"name": "持久测试", "role": "验证重启后状态恢复"})
        sent = app.send(bot["id"], {"text": "记住这个工作"}, "restart-1")
        work_id = sent["work_id"]
        art = app.store.put("artifact", {"id": uid("artifact"), "path": str(root / "季度报告.txt"),
            "host_id": "local", "size": 10, "owner_bot_id": bot["id"], "work_id": None,
            "source": "seed", "created": 1.0})
        app.store.close()

        # Reopen on the SAME data directory == a coordinator restart.
        app2 = Service(config(root), data)
        try:
            self.assertTrue(any(b["id"] == bot["id"] for b in app2.store.find("bot")))
            self.assertEqual(app2.store.get("work", work_id)["id"], work_id)
            self.assertTrue(any(a["id"] == art["id"] for a in app2.store.find("artifact")))
        finally:
            app2.store.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
