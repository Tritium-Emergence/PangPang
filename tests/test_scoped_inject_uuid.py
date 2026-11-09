"""Repair #78 P0-2B: inject mailbox UUID scoped by (turn_id, call_id).

Tip 0fd8028 hashed bare call_id while the journal is turn-isolated
(\"tool:\" + turn_id). Same call_id across turns could inject_conflict or
silently alias. Product path now hashes xpp-inject-v2:turn:call; legacy
(turn_id=None) keeps the old formula. No real model.
"""
import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

from pangpang import runner
from pangpang.service import inject_message_uuid


def _legacy_uuid(call_id):
    digest = hashlib.sha256(b"xpp-inject:" + str(call_id).encode()).hexdigest()
    return str(uuid.UUID(hex=digest[:32]))


def _v2_uuid(turn_id, call_id):
    digest = hashlib.sha256(
        b"xpp-inject-v2:" + str(turn_id).encode() + b":" + str(call_id).encode()
    ).hexdigest()
    return str(uuid.UUID(hex=digest[:32]))


class ScopedInjectUuidTests(unittest.TestCase):
    def test_legacy_none_matches_tip_bare_call_id_formula(self):
        call_id = "call-reuse"
        self.assertEqual(inject_message_uuid(call_id), _legacy_uuid(call_id))
        self.assertEqual(inject_message_uuid(call_id, None), _legacy_uuid(call_id))

    def test_v2_differs_from_legacy_for_same_call_id(self):
        call_id = "call-reuse"
        turn_id = "turn-a"
        legacy = inject_message_uuid(call_id)
        scoped = inject_message_uuid(call_id, turn_id)
        self.assertNotEqual(legacy, scoped)
        self.assertEqual(scoped, _v2_uuid(turn_id, call_id))

    def test_same_turn_and_call_is_stable(self):
        turn_id, call_id = "turn-stable", "call-1"
        a = inject_message_uuid(call_id, turn_id)
        b = inject_message_uuid(call_id, turn_id)
        self.assertEqual(a, b)
        self.assertEqual(a, _v2_uuid(turn_id, call_id))

    def test_different_turns_same_call_differ(self):
        call_id = "call-reuse"
        a = inject_message_uuid(call_id, "turn-1")
        b = inject_message_uuid(call_id, "turn-2")
        self.assertNotEqual(a, b)

    def test_cross_turn_reuse_no_longer_collides_in_mailbox(self):
        """Two turns reuse call_id with different text.

        Tip/legacy shape: same UUID → second append_inject raises inject_conflict.
        v2 shape: distinct UUIDs → both rows accepted.
        """
        call_id = "call-reuse"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            legacy_uid = inject_message_uuid(call_id)
            runner.append_inject(directory, legacy_uid, "first turn text")
            with self.assertRaises(ValueError) as raised:
                runner.append_inject(directory, legacy_uid, "second turn different text")
            self.assertEqual(str(raised.exception), "inject_conflict")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            uid_a = inject_message_uuid(call_id, "turn-1")
            uid_b = inject_message_uuid(call_id, "turn-2")
            self.assertNotEqual(uid_a, uid_b)
            self.assertEqual(
                runner.append_inject(directory, uid_a, "first turn text")["status"],
                "accepted")
            self.assertEqual(
                runner.append_inject(directory, uid_b, "second turn different text")["status"],
                "accepted")
            # Same UUID + same text stays idempotent.
            self.assertEqual(
                runner.append_inject(directory, uid_a, "first turn text")["status"],
                "accepted")
            # Same UUID + different text still conflicts.
            with self.assertRaises(ValueError) as raised:
                runner.append_inject(directory, uid_a, "changed text same uuid")
            self.assertEqual(str(raised.exception), "inject_conflict")


if __name__ == "__main__":
    unittest.main()
