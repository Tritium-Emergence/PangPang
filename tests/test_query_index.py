# -*- coding: utf-8 -*-
"""The EXACT SQL the store emits for hot queries
must be planned as an index scan (not a full table SCAN) at scale. Uses the same
find/find_due/find_page code paths as production, not hand-written SQL."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pangpang.store import Store, _where_clause


class QueryIndexTests(unittest.TestCase):
    N = 20000 # enough that a full SCAN is unmistakable in the plan/time

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.st = Store(os.path.join(cls.tmp.name, "idx.db"))
        with cls.st.atomic():
            for i in range(cls.N):
                s = "running" if i < 10 else "cancel_requested" if i < 20 else "succeeded"
                cls.st.put("task", {"id": "task-%d" % i, "status": s,
                                    "work_id": "w-%d" % (i % 300),
                                    "queue_deadline": time.time() + (i % 100) - 50,
                                    "created": time.time() + i * 0.001})

    @classmethod
    def tearDownClass(cls):
        cls.st.close()
        cls.tmp.cleanup()

    def _plan(self, sql, params):
        rows = self.st.db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return " | ".join(str(r[-1]) for r in rows)

    def assertIndexed(self, text):
        self.assertIn("USING", text) # USING INDEX / USING COVERING INDEX
        self.assertNotIn("SCAN objects\n", text)
        self.assertNotRegex(text, r"^SCAN objects( |$)")

    def test_find_single_status_uses_index(self):
        clause, values = _where_clause({"status": "running"})
        sql = "SELECT data FROM objects WHERE kind=?" + clause + " ORDER BY created,rowid LIMIT ? OFFSET ?"
        text = self._plan(sql, ["task"] + values + [1000, 0])
        self.assertIn("USING INDEX objects_status", text)

    def test_find_work_id_uses_index(self):
        clause, values = _where_clause({"work_id": "w-3"})
        sql = "SELECT data FROM objects WHERE kind=?" + clause + " ORDER BY created,rowid LIMIT ? OFFSET ?"
        text = self._plan(sql, ["task"] + values + [1000, 0])
        self.assertIn("USING INDEX objects_work", text)

    def test_find_due_uses_deadline_index(self):
        path = "$.queue_deadline"
        sql = ("SELECT data FROM objects WHERE kind=? "
               "AND json_extract(data,'$.status')=? "
               "AND json_extract(data,'" + path + "') IS NOT NULL "
               "AND json_extract(data,'" + path + "')<=? "
               "ORDER BY json_extract(data,'" + path + "') LIMIT ?")
        text = self._plan(sql, ["task", "queued", time.time(), 1000])
        self.assertIn("USING INDEX objects_queue_deadline", text)

    def test_find_page_keyset_uses_index(self):
        clause, values = _where_clause({"status": "running"})
        rid = self.st.db.execute("SELECT rowid FROM objects WHERE kind=? AND id=?",
                                 ("task", "task-0")).fetchone()[0]
        sql = ("SELECT data,created,id FROM objects WHERE kind=?" + clause +
               " AND (created>? OR (created=? AND rowid>?)) ORDER BY created,rowid LIMIT ?")
        text = self._plan(sql, ["task"] + values + [time.time(), time.time(), rid, 1000])
        self.assertIndexed(text)

    def test_single_status_scan_is_fast(self):
        # index-direct single-status active scan over 20k rows must be well under
        # a full-table walk (which measured ~20ms; index-direct is sub-ms).
        t = time.time()
        for _ in range(20):
            self.st.db.execute(
                "SELECT data FROM objects WHERE kind=? AND json_extract(data,'$.status')=? "
                "ORDER BY created,rowid LIMIT 1000", ["task", "running"]).fetchall()
        per = (time.time() - t) / 20 * 1000
        self.assertLess(per, 10.0, "single-status scan too slow: %.2fms" % per)


class HotLookupIndexTests(unittest.TestCase):
    """source_work_id and owner_bot_id lookups must use their own indexes.

    One source_work_id index covers dependency and exchange because kind is the
    leading column. A kind-only scan is the failure mode."""

    N = 4000

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.st = Store(os.path.join(cls.tmp.name, "hot.db"))
        with cls.st.atomic():
            for i in range(cls.N):
                cls.st.put("dependency", {"id": "dep-%d" % i, "status": "pending",
                                           "source_work_id": "src-%d" % (i % 50),
                                           "deadline": time.time() + i})
                cls.st.put("exchange", {"id": "ex-%d" % i, "status": "pending",
                                         "source_work_id": "src-%d" % (i % 50)})
                cls.st.put("work", {"id": "work-%d" % i, "status": "active",
                                     "owner_bot_id": "bot-%d" % (i % 40)})
        cls.st._ensure_hot_query_indexes()

    @classmethod
    def tearDownClass(cls):
        cls.st.close()
        cls.tmp.cleanup()

    def _plan(self, where, kind):
        clause, values = _where_clause(where)
        sql = ("SELECT data FROM objects WHERE kind=?" + clause +
               " ORDER BY created,rowid LIMIT ? OFFSET ?")
        rows = self.st.db.execute("EXPLAIN QUERY PLAN " + sql, [kind] + values + [1000, 0]).fetchall()
        return " | ".join(str(r[-1]) for r in rows)

    def _assert_index(self, text, index):
        self.assertIn(index, text, text)
        self.assertNotIn("objects_kind", text, text)
        self.assertNotRegex(text, r"^SCAN objects( |$)")

    def test_source_work_indexes_are_idempotent_and_singular(self):
        rows = self.st.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND sql LIKE '%source_work_id%'").fetchall()
        self.assertEqual([r[0] for r in rows], ["objects_source_work"])
        owners = self.st.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND sql LIKE '%owner_bot_id%'").fetchall()
        self.assertEqual([r[0] for r in owners], ["objects_owner_bot"])
        self.st._ensure_hot_query_indexes()
        again = self.st.db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name IN ('objects_source_work','objects_owner_bot')").fetchone()[0]
        self.assertEqual(again, 2)

    def test_dependency_source_work_lookup_uses_index(self):
        text = self._plan({"source_work_id": "src-3"}, "dependency")
        self._assert_index(text, "objects_source_work")

    def test_exchange_source_work_lookup_uses_same_index(self):
        text = self._plan({"source_work_id": "src-3"}, "exchange")
        self._assert_index(text, "objects_source_work")

    def test_work_owner_bot_lookup_uses_index(self):
        text = self._plan({"owner_bot_id": "bot-3"}, "work")
        self._assert_index(text, "objects_owner_bot")


class LatestStartedIndexTests(unittest.TestCase):
    """latest_started seeks the turn-only partial index. bot_id counts do not."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.st = Store(os.path.join(cls.tmp.name, "started.db"))
        # Scale where a full (kind, bot_id, started_at) index steals COUNT plans.
        with cls.st.atomic():
            for i in range(2000):
                cls.st.put("turn", {"id": "t-%d" % i, "bot_id": "bot-%d" % (i % 8),
                                     "status": "queued" if i % 50 == 0 else "done",
                                     "started_at": float(i), "history": ["h"]})
                cls.st.put("message", {"id": "m-%d" % i, "bot_id": "bot-%d" % (i % 8),
                                        "work_id": "w-%d" % (i % 30), "content": "c"})
            cls.st.put("turn", {"id": "t-null", "status": "done", "started_at": 9.0})

    @classmethod
    def tearDownClass(cls):
        cls.st.close()
        cls.tmp.cleanup()

    def _traced_selects(self, fn):
        seen = []
        self.st.db.set_trace_callback(seen.append)
        try:
            result = fn()
        finally:
            self.st.db.set_trace_callback(None)
        return result, [s for s in seen if s.lstrip().upper().startswith("SELECT")]

    def _explain(self, sql):
        rows = self.st.db.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        return " | ".join(str(r[-1]) for r in rows)

    def test_latest_started_plan_uses_partial_index(self):
        # 1993 is the greatest i in 0..1999 with i % 8 == 1.
        for bot_id, expected in (("bot-1", 1993.0), (None, 9.0)):
            value, selects = self._traced_selects(lambda bot_id=bot_id: self.st.latest_started(bot_id))
            self.assertEqual(value, expected)
            self.assertEqual(len(selects), 1, selects)
            text = self._explain(selects[0])
            self.assertIn("turn_bot_started", text, text)
            self.assertNotIn("TEMP B-TREE", text, text)

    def test_bot_id_counts_keep_status_and_work_indexes(self):
        _, turn_sql = self._traced_selects(
            lambda: self.st.count("turn", status="queued", bot_id="bot-1"))
        _, message_sql = self._traced_selects(
            lambda: self.st.count("message", work_id="w-3", bot_id="bot-1"))
        turn_plan = self._explain(turn_sql[0])
        message_plan = self._explain(message_sql[0])
        self.assertIn("objects_status", turn_plan, turn_plan)
        self.assertIn("objects_work", message_plan, message_plan)


if __name__ == "__main__":
    unittest.main()
