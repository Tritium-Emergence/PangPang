"""Repair #87: a finite-TTL command expires whether it is pending or already
delivered, and a late report against that terminal expiry ACKs instead of
raising command_result_conflict.

The expiry marker is a local stop for retransmit (N1): it is NOT proof that the
device never executed the command, and a durable command (expires None) can
never legitimately carry it.
"""
import tempfile
import time
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class ExpireDeliveredCommandsTests(unittest.TestCase):
    DEVICE = "device-" + "0" * 32

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        cloud = config(self.root)
        cloud["hosts"][0].update(transport="connector", credential_env="TEST_DEVICE_KEY")
        self.app = Service(cloud, self.root.parent / "cloud", deployment_level="L0")
        self.addCleanup(self.app.store.close)
        self.addCleanup(self.tmp.cleanup)

    def sync(self, reports=None, receipts=None):
        return self.app.broker.sync("local", {"instance_id": self.DEVICE,
                                              "reports": reports or [], "receipts": []})

    def enqueue(self, action, payload=None):
        return self.app.broker.enqueue("local", action, payload or {})

    def deliver(self, cmd):
        delivered = self.sync()["commands"]
        self.assertEqual([item["id"] for item in delivered], [cmd["id"]])
        return self.command(cmd["id"])

    def command(self, command_id):
        return self.app.store.get("command", command_id)

    def expire(self, command_id):
        cmd = self.command(command_id)
        cmd["expires"] = time.time() - 1
        self.app.store.put("command", cmd)
        return cmd

    def test_delivered_expired_is_reaped_and_pending_still_delivered(self):
        dead = self.enqueue("artifact_read", {"path": "a.txt"})
        self.deliver(dead)
        self.expire(dead["id"])
        live = self.enqueue("artifact_read", {"path": "b.txt"})

        out = self.sync()

        self.assertEqual([c["id"] for c in out["commands"]], [live["id"]])
        reaped = self.command(dead["id"])
        self.assertEqual(reaped["status"], "done")
        self.assertEqual(reaped["result"], {"error": "command_expired"})
        self.assertEqual(self.command(live["id"])["status"], "delivered")

    def test_pending_expired_reaped_fresh_redelivered_durable_never_expired(self):
        fresh = self.enqueue("artifact_read", {"path": "fresh.txt"})
        self.deliver(fresh)
        stale = self.enqueue("artifact_read", {"path": "stale.txt"})
        self.expire(stale["id"])
        # Durable (expires None) is examined by the sweep but has no TTL to expire.
        durable = self.enqueue("configure", {"adapter": "codex"})
        self.assertIsNone(self.command(durable["id"])["expires"])

        out = self.sync()

        self.assertEqual([c["id"] for c in out["commands"]], [fresh["id"]])
        self.assertEqual(self.command(stale["id"])["status"], "done")
        self.assertEqual(self.command(stale["id"])["result"], {"error": "command_expired"})
        kept = self.command(durable["id"])
        self.assertEqual(kept["status"], "pending")
        self.assertNotIn("result", kept)

    def test_late_artifact_read_after_reclaim_acks_and_stays_expired(self):
        cmd = self.enqueue("artifact_read", {"path": "a.txt"})
        self.deliver(cmd)
        self.expire(cmd["id"])
        self.sync()
        reclaimed = self.command(cmd["id"])
        self.assertEqual(reclaimed["result"], {"error": "command_expired"})

        late = {"id": cmd["id"], "result": {"path": "a.txt", "data": "different"}}
        first = self.sync([late])
        second = self.sync([late])

        self.assertEqual(first["ack"], [cmd["id"]])
        self.assertEqual(second["ack"], [cmd["id"]])
        after = self.command(cmd["id"])
        self.assertEqual(after["status"], "done")
        self.assertEqual(after["result"], {"error": "command_expired"})
        self.assertEqual(after["revision"], reclaimed["revision"])

    def test_late_discover_reports_after_reclaim_ack_without_writes(self):
        cmd = self.enqueue("discover", {})
        self.deliver(cmd)
        self.expire(cmd["id"])
        self.sync()
        reclaimed = self.command(cmd["id"])

        bare = {"id": cmd["id"], "result": {"error": "command_expired"}}
        projected = {"id": cmd["id"], "result": {"action": "discover", "host_id": "local",
                                                 "error": "command_expired"}}
        success = {"id": cmd["id"], "result": {"inventory": [
            {"adapter": "codex", "binary": "codex", "version": "1.0"}]}}
        for report in (bare, projected, success):
            out = self.sync([report])
            self.assertEqual(out["ack"], [cmd["id"]])
            after = self.command(cmd["id"])
            self.assertEqual(after["result"], {"error": "command_expired"})
            self.assertEqual(after["revision"], reclaimed["revision"])
        self.assertEqual(self.app.store.find("inventory"), [])

    def test_real_report_in_same_sync_wins_over_expiry(self):
        cmd = self.enqueue("artifact_read", {"path": "a.txt"})
        self.deliver(cmd)
        self.expire(cmd["id"])

        real = {"path": "a.txt", "data": "real"}
        out = self.sync([{"id": cmd["id"], "result": real}])

        self.assertEqual(out["ack"], [cmd["id"]])
        stored = self.command(cmd["id"])
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["result"], real)

    def test_normal_and_durable_conflicts_still_raise(self):
        fresh = self.enqueue("artifact_read", {"path": "a.txt"})
        self.deliver(fresh)
        self.sync([{"id": fresh["id"], "result": {"data": "first"}}])
        with self.assertRaises(Fault) as caught:
            self.sync([{"id": fresh["id"], "result": {"data": "second"}}])
        self.assertEqual(caught.exception.code, "command_result_conflict")

        # A durable command (expires None) can never legitimately be expired,
        # so the marker does not whitelist its late report either.
        durable = self.enqueue("configure", {"adapter": "codex"})
        self.assertIsNone(durable["expires"])
        current = self.command(durable["id"])
        current.update(status="done", result={"error": "command_expired"})
        self.app.store.put("command", current)
        with self.assertRaises(Fault) as caught:
            self.sync([{"id": durable["id"], "result": {"error": "device_boom"}}])
        self.assertEqual(caught.exception.code, "command_result_conflict")


if __name__ == "__main__":
    unittest.main()
