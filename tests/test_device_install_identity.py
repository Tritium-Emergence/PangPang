"""Device sync persists install identity fields for the hosts panel."""
import unittest
import uuid

from tests.test_event_liveness_hotpath import HeartbeatLivenessTests


class DeviceInstallIdentity(HeartbeatLivenessTests):
    def test_sync_stores_commit_ref_source(self):
        commit = "c" * 40
        self.app.broker.sync("local", dict(
            self.body,
            version="0.9.1",
            commit=commit,
            ref="main",
            source="git",
            supported_contract_version=1,
        ))
        device = self.app.store.get("device", "local")
        self.assertEqual(device["version"], "0.9.1")
        self.assertEqual(device["commit"], commit)
        self.assertEqual(device["ref"], "main")
        self.assertEqual(device["source"], "git")

    def test_sync_coerces_bad_source_and_ignores_bad_commit(self):
        self.app.broker.sync("local", dict(
            self.body,
            version="0.9.1",
            commit="not-a-sha",
            ref="main",
            source="weird",
            supported_contract_version=1,
        ))
        device = self.app.store.get("device", "local")
        self.assertEqual(device.get("commit"), "unknown")
        self.assertEqual(device["source"], "unknown")


if __name__ == "__main__":
    unittest.main()
