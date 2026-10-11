"""Startup config after the first stored settings row.

turn_budget and semantic_gate from the startup config still apply. Other
keys stay in the stored settings, and that ignore is logged.
"""
import copy
import logging
import tempfile
import unittest
from pathlib import Path

from pangpang.onboarding import apply_model_concurrency, empty_config
from pangpang.service import Service


def _capture(logger_name):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger(logger_name)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    return logger, handler, records, previous


class StartupConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.logger, self.handler, self.records, self.previous_level = _capture("pangpang.route")

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.previous_level)
        self.tmp.cleanup()

    def _messages(self):
        return [record.getMessage() for record in self.records]

    def _ignored(self):
        return [message for message in self._messages()
                if "not the live settings after the first start" in message]

    def test_first_start_uses_the_whole_file_without_an_ignore_warning(self):
        cfg = empty_config()
        cfg["turn_budget"] = 3
        cfg["delegation_budget"] = 2
        cfg["semantic_gate"] = {"mode": "shadow", "continuation_routing": False}
        app = Service(cfg, self.data)
        try:
            self.assertEqual(app.config["turn_budget"], 3)
            self.assertEqual(app.config["delegation_budget"], 2)
            self.assertEqual(app.semantic_gate.mode, "shadow")
            self.assertFalse(app.semantic_gate.continuation_routing)
            self.assertEqual(self._ignored(), [])
        finally:
            app.store.close()

    def test_later_start_applies_turn_budget_and_semantic_gate_only(self):
        first = empty_config()
        first["turn_budget"] = 8
        first["delegation_budget"] = 6
        first["semantic_gate"] = {"mode": "off", "continuation_routing": True}
        app = Service(first, self.data)
        try:
            app.commit_managed_config(copy.deepcopy(app.config))
        finally:
            app.store.close()
        self.records.clear()
        second = empty_config()
        second["turn_budget"] = 3
        second["delegation_budget"] = 1
        second["semantic_gate"] = {"mode": "enforce", "continuation_routing": False}
        second["scheduler"] = {"model_max_concurrency": 1, "reserved_user_slots": 0}
        app2 = Service(second, self.data)
        try:
            self.assertEqual(app2.config["turn_budget"], 3)
            self.assertEqual(app2.semantic_gate.mode, "enforce")
            self.assertFalse(app2.semantic_gate.continuation_routing)
            self.assertEqual(app2.config["delegation_budget"], 6)
            self.assertNotIn("scheduler", app2.config)
            self.assertTrue(self._ignored())
            candidate = copy.deepcopy(app2.config)
            apply_model_concurrency(candidate, 2)
            self.assertEqual(candidate["scheduler"]["model_max_concurrency"], 2)
        finally:
            app2.store.close()

    def test_matching_startup_config_does_not_warn(self):
        first = empty_config()
        first["turn_budget"] = 5
        first["semantic_gate"] = {"mode": "off"}
        app = Service(first, self.data)
        try:
            app.commit_managed_config(copy.deepcopy(app.config))
            stored = copy.deepcopy(app.config)
        finally:
            app.store.close()
        self.records.clear()
        app2 = Service(stored, self.data)
        try:
            self.assertEqual(app2.config["turn_budget"], 5)
            self.assertEqual(app2.semantic_gate.mode, "off")
            self.assertEqual(self._ignored(), [])
        finally:
            app2.store.close()


if __name__ == "__main__":
    unittest.main()
