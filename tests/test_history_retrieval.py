""" Durable Work: deterministic compaction + scoped historical retrieval."""
import functools
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_bot_runtime import config
from pangpang import history
from pangpang.context import projection
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault, Store, uid


def packet_of(app, turn_id):
    system = projection(app, app.store.get("turn", turn_id))[0]["content"]
    # The system prose (English canonical prompt + multi-line response-language
    # block) precedes the JSON packet and carries no '{', so the packet starts at
    # the first brace — robust to the prose's newlines.
    return json.loads(system[system.index("{"):])


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / "data")
        self.t0 = time.time() - 10000

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def add(self, work_id, content, *, bot_id="bot-main", kind="user"):
        self.t0 += 1
        return self.app.store.put("message", {
            "id": uid("msg"), "bot_id": bot_id, "work_id": work_id, "kind": kind,
            "from": "user" if kind == "user" else bot_id, "content": content,
            "created": self.t0})["id"]

    def long_work(self, n=45, key="long"):
        """A Work whose first n messages predate its current user message."""
        first = self.app.send("bot-main", {"text": "起步"}, key + "-0")
        ids = [self.add(first["work_id"], "历史-%02d 第%d条记录" % (i, i)) for i in range(n)]
        current = self.app.send("bot-main", {"text": "现在继续", "work_id": first["work_id"]}, key + "-1")
        return first["work_id"], ids, current

    def fallback(self):
        self.app.store._history_mode = "scan"

    # --- compaction -------------------------------------------------------

    def test_compaction_has_provenance_coverage_and_exact_cursor(self):
        work_id, ids, current = self.long_work()
        gap = packet_of(self.app, current["turn_id"])["history_gap"]
        entry = next(e for e in gap["compacted"] if e["work_id"] == work_id)
        total = self.app.store.count("message", work_id=work_id, bot_id="bot-main")
        self.assertEqual(entry["coverage"]["total_messages"], total)
        self.assertEqual(entry["coverage"]["not_in_context"],
                         total - entry["coverage"]["in_context"])
        # Anchors point back at the ORIGINAL first rows, not at a derived object.
        self.assertEqual(entry["anchors"][0]["evidence_ref"],
                         self.app.store.find("message", work_id=work_id, limit=1)[0]["id"])
        for ref in [e["evidence_ref"] for e in entry["anchors"] + entry["latest_omitted"]]:
            self.assertEqual(self.app.read_evidence("bot-main", ref)["record_kind"], "message")
        # Exactness: every message not in context is either skipped_ref or reachable
        # by paging older than the cursor.
        paged, before = [], entry["cursor"]["before"]
        while before:
            page = history.browse(self.app.store, "bot-main", work_id, before=before)
            paged += [m["evidence_ref"] for m in page["messages"]]
            before = page["next_before"]
        self.assertEqual(len(paged) + entry["skipped_total"] + entry["hidden_total"]
                         + entry["coverage"]["in_context"], total)
        self.assertIn(ids[0], paged)

    def test_coverage_identity_separates_hidden_rows(self):
        # rows hidden by design (here: newer than this turn) are
        # counted in hidden_total, never offered as retrievable omitted history.
        work_id, _, current = self.long_work(key="hidden")
        incoming = self.app.store.get("message", current["message_id"])
        late = self.app.store.put("message", {
            "id": uid("msg"), "bot_id": "bot-main", "work_id": work_id, "kind": "user",
            "from": "user", "content": "本轮之后才到的消息", "created": incoming["created"] + 100})["id"]
        entry = next(e for e in packet_of(self.app, current["turn_id"])["history_gap"]["compacted"]
                     if e["work_id"] == work_id)
        self.assertEqual(entry["hidden_total"], 1)
        self.assertNotIn(late, entry["skipped_refs"])
        self.assertNotIn(late, json.dumps(entry["anchors"] + entry["latest_omitted"]))
        paged, before = 0, entry["cursor"]["before"]
        while before:
            page = history.browse(self.app.store, "bot-main", work_id, before=before)
            paged += len(page["messages"])
            before = page["next_before"]
        self.assertEqual(entry["coverage"]["in_context"] + entry["skipped_total"]
                         + entry["hidden_total"] + paged, entry["coverage"]["total_messages"])

    def test_compaction_is_deterministic_and_writes_nothing(self):
        _, _, current = self.long_work()
        store = self.app.store
        before = store.db.execute("SELECT max(seq) FROM events").fetchone()[0]
        first = packet_of(self.app, current["turn_id"])["history_gap"]
        second = packet_of(self.app, current["turn_id"])["history_gap"]
        self.assertEqual(first, second)
        self.assertEqual(store.db.execute("SELECT max(seq) FROM events").fetchone()[0], before)

    def test_continued_from_chain_history_stays_visible(self):
        old_id, old_ids, _ = self.long_work(key="chain")
        old = self.app.store.get("work", old_id)
        old["status"] = "cancelled"
        self.app.store.put("work", old)
        resumed = self.app.send("bot-main", {"text": "还是做一下吧", "work_id": old_id}, "chain-2")
        self.assertNotEqual(resumed["work_id"], old_id)
        self.assertEqual(self.app.store.get("work", resumed["work_id"])["continued_from"], old_id)
        gap = packet_of(self.app, resumed["turn_id"])["history_gap"]
        entry = next(e for e in gap["compacted"] if e["work_id"] == old_id)
        first_old = self.app.store.find("message", work_id=old_id, limit=1)[0]["id"]
        self.assertEqual(entry["anchors"][0]["evidence_ref"], first_old)
        self.assertIsNotNone(entry["cursor"]["before"])
        # Older chain history is still retrievable, never dropped.
        hits = history.search(self.app.store, "bot-main", "历史-00", work_id=old_id)["hits"]
        self.assertEqual([h["evidence_ref"] for h in hits], [old_ids[0]])

    def test_short_work_has_no_gap(self):
        sent = self.app.send("bot-main", {"text": "简单问题"}, "short")
        self.assertNotIn("history_gap", packet_of(self.app, sent["turn_id"]))

    # --- retrieval scope ----------------------------------------------------

    def _scope_case(self):
        other = self.app.create_bot({"name": "编辑", "role": "编辑"})
        mine = self.app.send("bot-main", {"text": "我的工作"}, "mine")
        theirs = self.app.send(other["id"], {"text": "别人的工作"}, "theirs")
        own_ref = self.add(mine["work_id"], "胖胖每天出日报给老板")
        foreign_ref = self.add(theirs["work_id"], "机密日报 不应泄漏", bot_id=other["id"])
        return other, mine, theirs, own_ref, foreign_ref

    def _assert_scoped(self):
        other, mine, theirs, own_ref, foreign_ref = self._scope_case()
        for query in ("日报", "出日报", "日报 胖胖"):
            refs = [h["evidence_ref"] for h in history.search(self.app.store, "bot-main", query)["hits"]]
            self.assertEqual(refs, [own_ref], query)
        with self.assertRaises(Fault) as caught:
            history.search(self.app.store, "bot-main", "日报", work_id=theirs["work_id"])
        self.assertEqual(caught.exception.code, "work_forbidden")
        with self.assertRaises(Fault):
            history.browse(self.app.store, "bot-main", theirs["work_id"])
        with self.assertRaises(Fault) as caught:
            history.browse(self.app.store, "bot-main", mine["work_id"], before=foreign_ref)
        self.assertEqual(caught.exception.code, "cursor_forbidden")

    def test_retrieval_never_crosses_bots_fts(self):
        if self.app.store.history_index_mode() != "fts5_trigram":
            self.skipTest("SQLite without FTS5 trigram; covered by the scan test")
        self._assert_scoped()

    def test_retrieval_never_crosses_bots_scan(self):
        self.fallback()
        self._assert_scoped()

    def test_two_char_chinese_query_hits_on_every_path(self):
        sent = self.app.send("bot-main", {"text": "安排"}, "cn")
        ref = self.add(sent["work_id"], "请每天出日报")
        for mode in ("fts5_trigram", "scan"):
            if mode == "fts5_trigram" and self.app.store.history_index_mode() != mode:
                continue
            self.app.store._history_mode = mode
            result = history.search(self.app.store, "bot-main", "日报")
            self.assertEqual([h["evidence_ref"] for h in result["hits"]], [ref])
            # 2 chars cannot use trigram: it always goes through the bounded path.
            self.assertEqual(result["retrieval"], "bounded_scan")

    def test_fts_path_used_for_longer_terms(self):
        if self.app.store.history_index_mode() != "fts5_trigram":
            self.skipTest("SQLite without FTS5 trigram")
        sent = self.app.send("bot-main", {"text": "安排"}, "fts")
        ref = self.add(sent["work_id"], "Qwen-Claude 生成周报")
        result = history.search(self.app.store, "bot-main", "qwen-claude")
        self.assertEqual(result["retrieval"], "fts5_trigram")
        self.assertEqual([h["evidence_ref"] for h in result["hits"]], [ref])
        self.assertTrue(result["exhaustive"])
        # FTS syntax in a query is a literal phrase, never an operator.
        self.assertEqual(history.search(self.app.store, "bot-main", 'x" OR scope:"bot')["hits"], [])

    def test_index_row_never_grants_visibility(self):
        # The index is derived: an entry claiming bot-main's scope for another
        # Bot's row must be re-authorized against the original row and rejected.
        store = self.app.store
        if store.history_index_mode() != "fts5_trigram":
            self.skipTest("SQLite without FTS5 trigram")
        _, _, _, own_ref, foreign_ref = self._scope_case()
        rowid = store.db.execute("SELECT rowid FROM objects WHERE kind='message' AND id=?",
                                 (foreign_ref,)).fetchone()[0]
        top = store.db.execute("SELECT max(rowid) FROM objects").fetchone()[0]
        with store.atomic():
            store.db.execute("INSERT INTO counters VALUES(?,?) ON CONFLICT(name) DO UPDATE "
                             "SET value=excluded.value", (store.HISTORY_FTS_CURSOR, top))
            store.db.execute("INSERT INTO history_fts(rowid,body,scope) VALUES(?,?,?)",
                             (rowid, "机密日报 不应泄漏", "bot-main"))
        self.assertIn(rowid, [r[0] for r in store.db.execute( # the index really is poisoned
            "SELECT rowid FROM history_fts WHERE history_fts MATCH ?",
            ('scope:"bot-main" AND body:"机密日报"',))])
        self.assertEqual(history.search(store, "bot-main", "机密日报")["hits"], [])

    def test_index_scope_never_widens_work_scope_same_bot(self):
        # a stale/corrupt index entry claiming Work A's scope for a
        # row of the SAME Bot's Work B must not leak B into search(work_id=A).
        store = self.app.store
        if store.history_index_mode() != "fts5_trigram":
            self.skipTest("SQLite without FTS5 trigram")
        work_a = self.app.send("bot-main", {"text": "工作甲"}, "scope-a")["work_id"]
        work_b = self.app.send("bot-main", {"text": "工作乙"}, "scope-b")["work_id"]
        ref_b = self.add(work_b, "乙工作的专属内容")
        rowid = store.db.execute("SELECT rowid FROM objects WHERE kind='message' AND id=?",
                                 (ref_b,)).fetchone()[0]
        top = store.db.execute("SELECT max(rowid) FROM objects").fetchone()[0]
        with store.atomic():
            store.db.execute("INSERT INTO counters VALUES(?,?) ON CONFLICT(name) DO UPDATE "
                             "SET value=excluded.value", (store.HISTORY_FTS_CURSOR, top))
            store.db.execute("INSERT INTO history_fts(rowid,body,scope) VALUES(?,?,?)",
                             (rowid, "乙工作的专属内容", "bot-main " + work_a))
        self.assertIn(rowid, [r[0] for r in store.db.execute( # the index really is poisoned
            "SELECT rowid FROM history_fts WHERE history_fts MATCH ?",
            ('scope:"%s" AND body:"专属内容"' % work_a,))])
        result = history.search(store, "bot-main", "专属内容", work_id=work_a)
        self.assertEqual(result["hits"], [])
        # The unscoped Bot-wide query still finds it: the row is the Bot's own.
        self.assertEqual([h["evidence_ref"] for h in
                          history.search(store, "bot-main", "专属内容")["hits"]], [ref_b])

    def test_long_message_tail_is_indexed_never_false_exhaustive(self):
        # a keyword beyond 32k chars must be found; the index never
        # holds a prefix, so "0 hits + exhaustive" cannot be a false fact.
        store = self.app.store
        if store.history_index_mode() != "fts5_trigram":
            self.skipTest("SQLite without FTS5 trigram")
        sent = self.app.send("bot-main", {"text": "长消息"}, "long-tail")
        ref = self.add(sent["work_id"], "填" * 40000 + " 尾部唯一关键词", kind="assistant")
        result = history.search(store, "bot-main", "尾部唯一关键词")
        self.assertEqual(result["retrieval"], "fts5_trigram")
        self.assertFalse(result["hits"] == [] and result["exhaustive"])
        self.assertEqual([h["evidence_ref"] for h in result["hits"]], [ref])

    # --- -----------------------------------------------------------------

    def _tool(self, turn_id, **args):
        call = {"id": uid("call"), "function": {"name": "search_history", "arguments": json.dumps(args)}}
        return Runtime(self.app).durable_tool({"id": turn_id}, call)

    def test_history_tool_is_frozen_to_turn_high_water(self):
        # a message that arrives while turn T runs belongs to the
        # next turn; T's search/browse must not read it early.
        first = self.app.send("bot-main", {"text": "开始任务"}, "hw-1")
        turn = self.app.store.get("turn", first["turn_id"])
        turn.update(status="running", history=[{"role": "system", "content": "started"}])
        self.app.store.put("turn", turn)
        later = self.app.send("bot-main", {"text": "后到的补充要求 唯一标记",
                                           "work_id": first["work_id"]}, "hw-2")
        self.assertNotIn(later["message_id"], self.app.store.get("turn", first["turn_id"])["message_ids"])
        self.assertEqual(self._tool(first["turn_id"], query="唯一标记")["hits"], [])
        browsed = self._tool(first["turn_id"], work_id=first["work_id"])
        self.assertNotIn(later["message_id"], [m["evidence_ref"] for m in browsed["messages"]])
        self.assertIn(first["message_id"], [m["evidence_ref"] for m in browsed["messages"]])
        # The row exists; only the causal snapshot hides it. It is still unconsumed,
        # left for its own turn.
        self.assertEqual([h["evidence_ref"] for h in
                          history.search(self.app.store, "bot-main", "唯一标记")["hits"]],
                         [later["message_id"]])
        self.assertIsNone(self.app.store.get("message", later["message_id"]).get("consumed_by_turn_id"))
        self.app.finish_turn(self.app.store.get("turn", first["turn_id"]), "已处理开始任务")
        nxt = [t for t in self.app.store.find("turn", work_id=first["work_id"], limit=100)
               if later["message_id"] in (t.get("message_ids") or [])]
        self.assertEqual(len(nxt), 1)
        self.assertGreater(history.turn_high_water(self.app.store, nxt[0]),
                           history.turn_high_water(self.app.store, turn))

    def test_link_work_moves_message_scope_without_stale_index(self):
        # link_work rewrites message.work_id in place (same rowid).
        # Work scope comes from the canonical row, so an already-indexed message
        # follows the link and nothing claims a false exhaustive miss.
        store = self.app.store
        if store.history_index_mode() != "fts5_trigram":
            self.skipTest("SQLite without FTS5 trigram")
        target = self.app.send("bot-main", {"text": "既有工作"}, "link-target")["work_id"]
        entry = self.app.send("bot-main", {"text": "关联前的消息 链接标记词"}, "link-entry")
        self.assertEqual(len(history.search(store, "bot-main", "链接标记词", work_id=entry["work_id"])["hits"]), 1)
        self.app.link_work(entry["turn_id"], target)
        self.assertEqual(store.get("message", entry["message_id"])["work_id"], target)
        moved = history.search(store, "bot-main", "链接标记词", work_id=target)
        self.assertEqual([h["evidence_ref"] for h in moved["hits"]], [entry["message_id"]])
        self.assertEqual(moved["hits"][0]["work_id"], target)
        stale = history.search(store, "bot-main", "链接标记词", work_id=entry["work_id"])
        self.assertEqual(stale["hits"], [])

    def test_catch_up_has_byte_budget(self):
        # the lock hold is bounded by bytes as well as rows; a
        # partial catch-up is reported as non-exhaustive, and repeats converge.
        store = self.app.store
        if store.history_index_mode() != "fts5_trigram":
            self.skipTest("SQLite without FTS5 trigram")
        store.history_index_catch_up()
        sent = self.app.send("bot-main", {"text": "预算"}, "budget")
        for i in range(6):
            self.add(sent["work_id"], "预算条目%d " % i + "字" * 1000)
        cursor = lambda: store.db.execute("SELECT value FROM counters WHERE name=?",
                                          (store.HISTORY_FTS_CURSOR,)).fetchone()[0]
        before = cursor()
        self.assertFalse(store.history_index_catch_up(byte_budget=2500))
        # It stops at the first row that crosses the budget: the small "预算" user
        # message plus one ~3000-byte entry, leaving the other five for later calls.
        top = store.db.execute("SELECT max(rowid) FROM objects").fetchone()[0]
        self.assertLess(cursor(), top)
        self.assertEqual(store.db.execute("SELECT count(*) FROM objects WHERE kind='message' "
                                          "AND rowid>? AND rowid<=?", (before, cursor())).fetchone()[0], 2)
        store.history_index_catch_up = functools.partial(Store.history_index_catch_up, store,
                                                         byte_budget=2500)
        partial = history.search(store, "bot-main", "预算条目5")
        self.assertFalse(partial["exhaustive"])
        rounds = 0
        while not Store.history_index_catch_up(store, byte_budget=2500):
            rounds += 1
            self.assertLess(rounds, 20)
        del store.history_index_catch_up
        done = history.search(store, "bot-main", "预算条目5")
        self.assertEqual(len(done["hits"]), 1)
        self.assertTrue(done["exhaustive"])

    def test_retrieval_uses_effective_history_after_routing_correction(self):
        # a superseded wrong reply never comes back as history text;
        # a corrected user intent belongs to its effective Work (same helper as
        # the context projection).
        wrong = self.app.send("bot-main", {"text": "旧话题"}, "fix-old")["work_id"]
        right = self.app.send("bot-main", {"text": "新话题"}, "fix-new")["work_id"]
        intent = self.add(wrong, "被纠正的用户意图 纠正标记")
        reply = self.add(wrong, "错误归属的回复 纠正标记", kind="assistant")
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": intent, "old_work_id": wrong,
            "new_work_id": right, "superseded_message_ids": [reply], "created": time.time()})
        for mode in ("fts5_trigram", "scan"):
            if mode == "fts5_trigram" and self.app.store.history_index_mode() != mode:
                continue
            self.app.store._history_mode = mode
            hits = history.search(self.app.store, "bot-main", "纠正标记")["hits"]
            self.assertEqual([(h["evidence_ref"], h["work_id"]) for h in hits], [(intent, right)], mode)
            self.assertEqual([h["evidence_ref"] for h in history.search(
                self.app.store, "bot-main", "纠正标记", work_id=right)["hits"]], [intent], mode)
            self.assertEqual(history.search(self.app.store, "bot-main", "纠正标记", work_id=wrong)["hits"],
                             [], mode)
        stubs = {m["evidence_ref"]: m for m in history.browse(self.app.store, "bot-main", wrong)["messages"]}
        self.assertEqual(stubs[reply], {"evidence_ref": reply, "kind": "assistant", "hidden": "routing_correction"})
        self.assertEqual(stubs[intent]["effective_work_id"], right)
        self.assertNotIn("excerpt", stubs[intent])

    # --- -----------------------------------------------------------------

    def test_future_correction_does_not_rewrite_running_turn_history(self):
        # a routing correction made by a message after this turn's
        # high-water must not change this turn's view; the next turn sees it.
        old = self.app.send("bot-main", {"text": "旧工作"}, "fc-old")["work_id"]
        new = self.app.send("bot-main", {"text": "新工作"}, "fc-new")["work_id"]
        intent = self.add(old, "未来纠错标记 用户意图")
        reply = self.add(old, "未来纠错标记 旧回复", kind="assistant")
        first = self.app.send("bot-main", {"text": "继续旧工作", "work_id": old}, "fc-run")
        turn = self.app.store.get("turn", first["turn_id"])
        turn.update(status="running", history=[{"role": "system", "content": "started"}])
        self.app.store.put("turn", turn)
        later = self.app.send("bot-main", {"text": "刚才那句其实属于新工作", "work_id": old}, "fc-later")
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": intent, "old_work_id": old,
            "new_work_id": new, "correcting_message_id": later["message_id"],
            "superseded_message_ids": [reply], "created": time.time()})
        before = self._tool(first["turn_id"], query="未来纠错标记")
        self.assertEqual({(h["evidence_ref"], h["work_id"]) for h in before["hits"]},
                         {(intent, old), (reply, old)})
        browsed = {m["evidence_ref"]: m for m in self._tool(first["turn_id"], work_id=old)["messages"]}
        self.assertNotIn("hidden", browsed[intent])
        self.assertNotIn("hidden", browsed[reply])
        self.assertNotIn(intent, [m["evidence_ref"] for m in self._tool(first["turn_id"], work_id=new)["messages"]])
        self.app.finish_turn(self.app.store.get("turn", first["turn_id"]), "已处理")
        nxt = next(t for t in self.app.store.find("turn", work_id=old, limit=100)
                   if later["message_id"] in (t.get("message_ids") or []))
        after = self._tool(nxt["id"], query="未来纠错标记")
        self.assertEqual([(h["evidence_ref"], h["work_id"]) for h in after["hits"]], [(intent, new)])

    def _drain(self, work_id, as_of=None):
        """Follow both browse streams to the end; return (raw refs, corrected-in entries)."""
        raw, moved, before = [], [], None
        for _ in range(50):
            page = history.browse(self.app.store, "bot-main", work_id, before=before, as_of=as_of)
            raw += [m["evidence_ref"] for m in page["messages"]]
            before = page["next_before"]
            if not before:
                break
        before, first = None, True
        for _ in range(50):
            page = history.browse(self.app.store, "bot-main", work_id, before=before, as_of=as_of) \
                if not first else history.browse(self.app.store, "bot-main", work_id, as_of=as_of)
            first = False
            moved += page["corrected_in"]
            before = page["corrected_in_next"]
            if not before:
                break
        return raw, moved

    def test_browse_corrected_in_stream_lists_each_row_once(self):
        # under the later contract: raw provenance paging plus a
        # bounded, cursor-paged corrected-in stream; no keyword needed.
        src = self.app.send("bot-main", {"text": "来源工作"}, "ci-src")["work_id"]
        dst = self.app.send("bot-main", {"text": "目标工作"}, "ci-dst")["work_id"]
        own = [self.add(dst, "目标自有 %d" % i) for i in range(16)]
        moved = []
        for i in range(history.MAX_HITS + 3): # more than one corrected-in page
            mid = self.add(src, "被移入的意图 %d" % i)
            moved.append(mid)
            self.app.store.put("routing_correction", {
                "id": uid("route-correction"), "target_message_id": mid, "old_work_id": src,
                "new_work_id": dst, "superseded_message_ids": [], "created": time.time()})
        # One message moved into dst twice is still listed once (latest correction).
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": moved[0], "old_work_id": dst,
            "new_work_id": dst, "superseded_message_ids": [], "created": time.time()})
        raw, entries = self._drain(dst)
        refs = [e["evidence_ref"] for e in entries]
        self.assertEqual(sorted(refs), sorted(moved))
        self.assertTrue(set(own) <= set(raw))
        self.assertFalse(set(moved) & set(raw))
        entry = next(e for e in entries if e["evidence_ref"] == moved[-1])
        self.assertEqual(entry["raw_work_id"], src)
        self.assertIn("被移入的意图", entry["excerpt"])
        # A corrected-in cursor belongs to its target Work only.
        with self.assertRaises(Fault) as caught:
            history.browse(self.app.store, "bot-main", src,
                           before=history.browse(self.app.store, "bot-main", dst)["corrected_in_next"])
        self.assertEqual(caught.exception.code, "cursor_forbidden")

    def test_browse_raw_paging_crosses_rows_after_high_water(self):
        # Raw rows past the turn's high-water only occupy the newest end; paging
        # still reaches every earlier raw row, and the corrected-in stream is separate.
        src = self.app.send("bot-main", {"text": "来源"}, "gap-src")["work_id"]
        sent = self.app.send("bot-main", {"text": "目标"}, "gap-dst")
        dst = sent["work_id"]
        moved = self.add(src, "最早的移入意图")
        middle = [self.add(dst, "中间原始 %d" % i) for i in range(3)]
        for i in range(history.BROWSE_PAGES * (history.MAX_HITS + 1) + 5):
            self.t0 += 1
            self.app.store.put("message", {"id": uid("msg"), "bot_id": "bot-main", "work_id": dst,
                                           "kind": "user", "from": "user", "content": "本轮之后 %d" % i,
                                           "created": self.t0, "sequence": 10 ** 9})
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": moved, "old_work_id": src,
            "new_work_id": dst, "correcting_message_id": sent["message_id"],
            "superseded_message_ids": [], "created": time.time()})
        raw, entries = self._drain(dst, as_of=10 ** 6)
        self.assertTrue(set(middle) <= set(raw))
        self.assertEqual(len(raw), len(set(raw)))
        self.assertEqual([e["evidence_ref"] for e in entries], [moved])

    def test_more_than_1000_corrections_keep_latest_truth(self):
        # effective history is never "the oldest 1000"; lookups go
        # by message, so the 1002nd correction is found like the first.
        work = self.app.send("bot-main", {"text": "长期工作"}, "many")["work_id"]
        other = self.app.send("bot-main", {"text": "别处"}, "many-other")["work_id"]
        reply = self.add(work, "千条纠错之后 被替换的回复", kind="assistant")
        with self.app.store.atomic():
            for i in range(1001):
                self.app.store.put("routing_correction", {
                    "id": uid("route-correction"), "target_message_id": uid("msg"),
                    "old_work_id": work, "new_work_id": other, "superseded_message_ids": [],
                    "created": time.time()})
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": uid("msg"), "old_work_id": work,
            "new_work_id": other, "superseded_message_ids": [reply], "created": time.time()})
        self.assertEqual(history.search(self.app.store, "bot-main", "被替换的回复")["hits"], [])
        stub = {m["evidence_ref"]: m for m in history.browse(self.app.store, "bot-main", work)["messages"]}[reply]
        self.assertEqual(stub["hidden"], "routing_correction")
        self.assertIn(reply, history.corrections_for(self.app.store, [reply])[1])

    # --- -----------------------------------------------------------------

    def _vm_steps(self, fn):
        steps = [0]

        def tick():
            steps[0] += 1
            return 0
        self.app.store.db.set_progress_handler(tick, 100)
        try:
            fn()
        finally:
            self.app.store.db.set_progress_handler(None, 0)
        return steps[0]

    def test_correction_reads_do_not_grow_with_work_lifetime(self):
        # one context projection / one browse page / one scoped search
        # must not read a Work's whole correction log. SQLite VM steps are measured
        # at 100 and at 10,000 corrections on the same Work; the cost must stay flat.
        work = self.app.send("bot-main", {"text": "长期驻留"}, "bound-a")["work_id"]
        other = self.app.send("bot-main", {"text": "别处"}, "bound-b")["work_id"]
        for i in range(40):
            self.add(work, "日常对话 边界标记 %d" % i)
        current = self.app.send("bot-main", {"text": "继续", "work_id": work}, "bound-c")

        def corrections(n):
            with self.app.store.atomic():
                for i in range(n):
                    self.app.store.put("routing_correction", {
                        "id": uid("route-correction"), "target_message_id": uid("msg"),
                        "old_work_id": work if i % 2 else other, "new_work_id": other if i % 2 else work,
                        "correcting_message_id": current["message_id"],
                        "superseded_message_ids": [uid("msg")], "created": time.time()})

        def measure():
            turn = self.app.store.get("turn", current["turn_id"])
            as_of = history.turn_high_water(self.app.store, turn)
            return (self._vm_steps(lambda: projection(self.app, turn)),
                    self._vm_steps(lambda: history.browse(self.app.store, "bot-main", work, as_of=as_of)),
                    self._vm_steps(lambda: history.search(self.app.store, "bot-main", "边界标记",
                                                          work_id=work, as_of=as_of)))
        self.app.store._history_mode = "scan"
        corrections(100)
        small = measure()
        corrections(9900)
        large = measure()
        for name, a, b in zip(("projection", "browse", "scoped search"), small, large):
            self.assertLess(b, a * 1.5 + 50, "%s grew %d -> %d steps with 100x corrections" % (name, a, b))

    def test_unknown_correction_causality_fails_closed(self):
        # a correction whose correcting message cannot
        # be found has no provable causal position. Inside a turn the rows it
        # touches have unprovable effective truth: they are hidden and flagged,
        # never shown under their raw Work.
        work = self.app.send("bot-main", {"text": "工作"}, "unk-work")["work_id"]
        elsewhere = self.app.send("bot-main", {"text": "别处"}, "unk-else")["work_id"]
        reply = self.add(work, "因果未知标记 回复", kind="assistant")
        intent = self.add(work, "因果未知标记 意图")
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": intent, "old_work_id": work,
            "new_work_id": elsewhere, "correcting_message_id": "msg-deleted-or-missing",
            "superseded_message_ids": [reply], "created": time.time()})
        turn = self.app.send("bot-main", {"text": "看看", "work_id": work}, "unk-turn")["turn_id"]
        found = self._tool(turn, query="因果未知标记")
        self.assertEqual(found["hits"], [])
        self.assertTrue(found["correction_causality_unknown"])
        self.assertFalse(found["exhaustive"])
        browsed = self._tool(turn, work_id=work)
        self.assertTrue(browsed["correction_causality_unknown"])
        stubs = {m["evidence_ref"]: m for m in browsed["messages"]}
        for ref in (reply, intent):
            self.assertEqual(stubs[ref]["hidden"], "correction_uncertain")
            self.assertNotIn("excerpt", stubs[ref])
        context = json.dumps(projection(self.app, self.app.store.get("turn", turn)), ensure_ascii=False)
        self.assertNotIn("因果未知标记", context)
        self.assertTrue(packet_of(self.app, turn)["history_gap"]["active_projection_incomplete"])

    def test_same_target_correction_history_is_budgeted(self):
        # one candidate corrected 100 vs 10,000 times (and one reply
        # superseded 100 vs 10,000 times) must cost the same; the latest causal
        # correction still decides.
        work = self.app.send("bot-main", {"text": "反复纠正"}, "same-a")
        other = self.app.send("bot-main", {"text": "另一处"}, "same-b")["work_id"]
        intent = self.add(work["work_id"], "反复被纠正的意图 同目标标记")
        reply = self.add(work["work_id"], "反复被替换的回复 同目标标记", kind="assistant")
        current = self.app.send("bot-main", {"text": "继续", "work_id": work["work_id"]}, "same-c")
        made = [0]

        def corrections(n):
            with self.app.store.atomic():
                for i in range(n):
                    made[0] += 1
                    self.app.store.put("routing_correction", {
                        "id": uid("route-correction"), "target_message_id": intent,
                        "old_work_id": work["work_id"],
                        "new_work_id": other if made[0] % 2 else work["work_id"],
                        "correcting_message_id": work["message_id"],
                        "superseded_message_ids": [reply], "created": time.time()})

        def measure():
            turn = self.app.store.get("turn", current["turn_id"])
            as_of = history.turn_high_water(self.app.store, turn)
            steps = (self._vm_steps(lambda: projection(self.app, turn)),
                     self._vm_steps(lambda: history.corrections_for(self.app.store, [intent, reply], as_of=as_of)),
                     self._vm_steps(lambda: history.browse(self.app.store, "bot-main", work["work_id"], as_of=as_of)),
                     self._vm_steps(lambda: history.search(self.app.store, "bot-main", "同目标标记",
                                                           work_id=work["work_id"], as_of=as_of)))
            eff = history.corrections_for(self.app.store, [intent, reply], as_of=as_of)
            self.assertFalse(eff.truncated or eff.uncertain)
            self.assertIn(reply, eff.superseded)
            self.assertEqual(eff.by_target[intent]["new_work_id"],
                             work["work_id"] if made[0] % 2 == 0 else other)
            return steps
        self.app.store._history_mode = "scan"
        corrections(100)
        small = measure()
        corrections(9900)
        large = measure()
        for name, a, b in zip(("projection", "corrections_for", "browse", "scoped search"), small, large):
            self.assertLess(b, a * 1.5 + 50, "%s grew %d -> %d steps with 100x same-target corrections" % (name, a, b))

    def test_future_corrections_cannot_push_out_visible_corrected_in(self):
        # corrections made after the turn's high-water must not take
        # the limited corrected-in slots of a correction that already existed.
        src = self.app.send("bot-main", {"text": "来源"}, "push-src")["work_id"]
        dst = self.app.send("bot-main", {"text": "目标"}, "push-dst")
        moved = self.add(src, "早已移入的意图 挤出标记")
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": moved, "old_work_id": src,
            "new_work_id": dst["work_id"], "correcting_message_id": dst["message_id"],
            "superseded_message_ids": [], "created": time.time()})
        current = self.app.send("bot-main", {"text": "继续", "work_id": dst["work_id"]}, "push-turn")
        turn = self.app.store.get("turn", current["turn_id"])
        turn.update(status="running", history=[{"role": "system", "content": "started"}])
        self.app.store.put("turn", turn)
        future_src = self.app.store.put("message", {
            "id": uid("msg"), "bot_id": "bot-main", "work_id": dst["work_id"], "kind": "user",
            "from": "user", "content": "后到的纠错来源", "created": time.time(), "sequence": 10 ** 9})["id"]

        def future(n):
            with self.app.store.atomic():
                for i in range(n):
                    self.app.store.put("routing_correction", {
                        "id": uid("route-correction"), "target_message_id": self.add(src, "未来移入 %d" % i),
                        "old_work_id": src, "new_work_id": dst["work_id"], "correcting_message_id": future_src,
                        "superseded_message_ids": [], "created": time.time()})
        future(40) # more than the 30 context slots and the 10 browse slots
        self.app.store._history_mode = "scan"
        packet = packet_of(self.app, current["turn_id"])
        self.assertIn("早已移入的意图", json.dumps(projection(self.app, turn), ensure_ascii=False))
        self.assertNotIn("未来移入", json.dumps(projection(self.app, turn), ensure_ascii=False))
        self.assertNotIn("history_gap", packet)
        hits = self._tool(current["turn_id"], query="挤出标记", work_id=dst["work_id"])["hits"]
        self.assertEqual([h["evidence_ref"] for h in hits], [moved])
        listed = self._tool(current["turn_id"], work_id=dst["work_id"])["corrected_in"]
        self.assertEqual([e["evidence_ref"] for e in listed], [moved])
        # Without a cutoff (a later turn's view) the future corrections are there.
        self.assertEqual(len(history.browse(self.app.store, "bot-main", dst["work_id"])["corrected_in"]),
                         history.MAX_HITS)
        # Beyond the examination budget the turn is told its view is incomplete
        # instead of silently losing the earlier correction.
        future(history.INTO_SLACK)
        self.assertTrue(packet_of(self.app, current["turn_id"])["history_gap"]["active_projection_incomplete"])

    def test_compaction_excerpts_use_effective_history(self):
        # Rows outside the active window (anchors / latest_omitted) are excerpted
        # only if they are still this Work's effective history.
        first = self.app.send("bot-main", {"text": "起步"}, "cx-0")
        wrong = self.add(first["work_id"], "早期被纠正替换的回复", kind="assistant")
        for i in range(44):
            self.add(first["work_id"], "历史 %02d" % i)
        # The correction's own target is unrelated to this window, so only the
        # superseded lookup of the excerpted rows themselves can catch it.
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": uid("msg"),
            "old_work_id": first["work_id"], "new_work_id": "work-elsewhere",
            "correcting_message_id": first["message_id"],
            "superseded_message_ids": [wrong], "created": time.time()})
        current = self.app.send("bot-main", {"text": "继续", "work_id": first["work_id"]}, "cx-1")
        entry = packet_of(self.app, current["turn_id"])["history_gap"]["compacted"][0]
        excerpted = [e["evidence_ref"] for e in entry["anchors"] + entry["latest_omitted"]]
        self.assertNotIn(wrong, excerpted)
        self.assertNotIn("早期被纠正替换的回复", json.dumps(entry, ensure_ascii=False))

    def test_superseded_lookup_backfills_existing_corrections_once(self):
        # the one-time upgrade backfill indexes corrections that
        # predate the lookup table; later writes are kept by the triggers.
        reply = self.add(self.app.send("bot-main", {"text": "旧"}, "bf")["work_id"], "旧回复", kind="assistant")
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": uid("msg"), "old_work_id": "w",
            "new_work_id": "x", "superseded_message_ids": [reply], "created": time.time()})
        store = self.app.store
        for trigger in ("history_superseded_ins", "history_superseded_upd", "history_superseded_del"):
            store.db.execute("DROP TRIGGER " + trigger)
        store.db.execute("DROP TABLE history_superseded") # as before the upgrade
        store.close()
        self.app = Service(config(self.root), self.root / "data")
        self.assertIn(reply, history.corrections_for(self.app.store, [reply])[1])
        later = self.add(self.app.send("bot-main", {"text": "新"}, "bf2")["work_id"], "新回复", kind="assistant")
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": uid("msg"), "old_work_id": "w",
            "new_work_id": "x", "superseded_message_ids": [later], "created": time.time()})
        self.assertIn(later, history.corrections_for(self.app.store, [later])[1])

    # --- -----------------------------------------------------------------

    def _future_source(self, work_id):
        return self.app.store.put("message", {
            "id": uid("msg"), "bot_id": "bot-main", "work_id": work_id, "kind": "user", "from": "user",
            "content": "后到的纠错来源", "created": time.time(), "sequence": 10 ** 9})["id"]

    def test_budget_overflow_never_falls_back_to_raw_work(self):
        # M raw A; C0 (visible) moves M to B; then BUDGET+1 future
        # corrections of M. Inside the turn M's effective Work is unprovable: it
        # must not reappear as A's history anywhere.
        a = self.app.send("bot-main", {"text": "工作A"}, "ovf-a")
        b = self.app.send("bot-main", {"text": "工作B"}, "ovf-b")["work_id"]
        c = self.app.send("bot-main", {"text": "工作C"}, "ovf-c")["work_id"]
        moved = self.add(a["work_id"], "溢出标记 被移走的意图")
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": moved, "old_work_id": a["work_id"],
            "new_work_id": b, "correcting_message_id": a["message_id"],
            "superseded_message_ids": [], "created": time.time()})
        turn = self.app.send("bot-main", {"text": "继续A", "work_id": a["work_id"]}, "ovf-turn")["turn_id"]
        source = self._future_source(c)
        with self.app.store.atomic():
            for i in range(history.CORRECTION_BUDGET + 1):
                self.app.store.put("routing_correction", {
                    "id": uid("route-correction"), "target_message_id": moved, "old_work_id": b,
                    "new_work_id": c, "correcting_message_id": source,
                    "superseded_message_ids": [], "created": time.time()})
        eff = history.corrections_for(self.app.store, [moved],
                                      as_of=history.turn_high_water(self.app.store, self.app.store.get("turn", turn)))
        self.assertIn(moved, eff.uncertain)
        self.assertNotIn(moved, eff.by_target)
        context = json.dumps(projection(self.app, self.app.store.get("turn", turn)), ensure_ascii=False)
        self.assertNotIn("溢出标记", context)
        self.assertTrue(packet_of(self.app, turn)["history_gap"]["active_projection_incomplete"])
        for mode in ("fts5_trigram", "scan"):
            if mode == "fts5_trigram" and self.app.store.history_index_mode() != mode:
                continue
            self.app.store._history_mode = mode
            for scope in (a["work_id"], None):
                found = self._tool(turn, query="溢出标记", **({"work_id": scope} if scope else {}))
                self.assertEqual(found["hits"], [], (mode, scope))
                self.assertFalse(found["exhaustive"])
                self.assertTrue(found["correction_history_truncated"])
        stub = {m["evidence_ref"]: m for m in self._tool(turn, work_id=a["work_id"])["messages"]}[moved]
        self.assertEqual(stub, {"evidence_ref": moved, "kind": "user", "hidden": "correction_uncertain"})
        into_b = self._tool(turn, work_id=b)
        self.assertNotIn(moved, [e["evidence_ref"] for e in into_b["corrected_in"]])
        self.assertTrue(into_b["correction_history_truncated"])
        # Once the corrections are causally visible (a later turn), the truth is
        # provable again and M shows up where it now belongs.
        later = history.search(self.app.store, "bot-main", "溢出标记", work_id=c)
        self.assertEqual([(h["evidence_ref"], h["work_id"]) for h in later["hits"]], [(moved, c)])

    def test_uncertain_rows_are_not_excerpted(self):
        # (compaction): an anchor whose effective truth is
        # unprovable is not excerpted from its raw Work.
        first = self.app.send("bot-main", {"text": "起步"}, "unc-0")
        moved = self.add(first["work_id"], "不可证明归属的早期意图")
        for i in range(44):
            self.add(first["work_id"], "历史 %02d" % i)
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": moved, "old_work_id": first["work_id"],
            "new_work_id": "work-elsewhere", "correcting_message_id": "msg-missing",
            "superseded_message_ids": [], "created": time.time()})
        current = self.app.send("bot-main", {"text": "继续", "work_id": first["work_id"]}, "unc-1")
        entry = packet_of(self.app, current["turn_id"])["history_gap"]["compacted"][0]
        self.assertNotIn(moved, [e["evidence_ref"] for e in entry["anchors"] + entry["latest_omitted"]])
        self.assertNotIn("不可证明归属", json.dumps(entry, ensure_ascii=False))

    def test_unknown_corrected_in_is_never_false_complete(self):
        # an ignored corrected-in of unknown causality must mark
        # context incomplete and bounded search non-exhaustive.
        dst = self.app.send("bot-main", {"text": "目标"}, "uci-dst")
        src = self.app.send("bot-main", {"text": "来源"}, "uci-src")["work_id"]
        moved = self.add(src, "未知来源移入 标记")
        current = self.app.send("bot-main", {"text": "继续", "work_id": dst["work_id"]}, "uci-turn")
        self.assertNotIn("history_gap", packet_of(self.app, current["turn_id"])) # control
        self.app.store.put("routing_correction", {
            "id": uid("route-correction"), "target_message_id": moved, "old_work_id": src,
            "new_work_id": dst["work_id"], "correcting_message_id": "msg-missing",
            "superseded_message_ids": [], "created": time.time()})
        self.assertTrue(packet_of(self.app, current["turn_id"])["history_gap"]["active_projection_incomplete"])
        for mode in ("fts5_trigram", "scan"):
            if mode == "fts5_trigram" and self.app.store.history_index_mode() != mode:
                continue
            self.app.store._history_mode = mode
            found = self._tool(current["turn_id"], query="标记", work_id=dst["work_id"])
            self.assertEqual(found["hits"], [], mode)
            self.assertFalse(found["exhaustive"], mode)
            self.assertTrue(found["correction_causality_unknown"], mode)

    # --- degradation / bounds -------------------------------------------------

    def test_missing_fts_or_trigram_degrades_without_failing(self):
        # SQLite without FTS5 / trigram (< 3.34): creating the index fails.
        store = Store(str(self.root / "old-sqlite.db"))
        try:
            store._history_tokenizer = "no_such_tokenizer_on_this_build"
            self.assertEqual(store.history_index_mode(), "scan")
            self.assertEqual(store.history_index_mode(), "scan") # probed once, cached
            self.assertIsNone(store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='history_fts'").fetchone())
        finally:
            store.close()
        # A DB carried to a machine where the existing index cannot be queried.
        store = Store(str(self.root / "moved.db"))
        try:
            store.db.execute("CREATE TABLE history_fts(body, scope)")
            self.assertEqual(store.history_index_mode(), "scan")
        finally:
            store.close()
        # Retrieval keeps working in the degraded mode, including 3+ char terms.
        self.fallback()
        sent = self.app.send("bot-main", {"text": "安排"}, "degrade")
        ref = self.add(sent["work_id"], "季度总结报告")
        result = history.search(self.app.store, "bot-main", "总结报告")
        self.assertEqual(result["retrieval"], "bounded_scan")
        self.assertEqual([h["evidence_ref"] for h in result["hits"]], [ref])

    def test_catch_up_is_bounded_and_incremental(self):
        if self.app.store.history_index_mode() != "fts5_trigram":
            self.skipTest("SQLite without FTS5 trigram")
        sent = self.app.send("bot-main", {"text": "安排"}, "catch")
        for i in range(30):
            self.add(sent["work_id"], "增量索引条目 %d" % i)
        store = self.app.store
        store.db.execute("DELETE FROM counters WHERE name=?", (store.HISTORY_FTS_CURSOR,))
        store.db.execute("DELETE FROM history_fts")
        self.assertFalse(store.history_index_catch_up(window=5, max_windows=2))
        cursor = store.db.execute("SELECT value FROM counters WHERE name=?",
                                  (store.HISTORY_FTS_CURSOR,)).fetchone()[0]
        self.assertEqual(cursor, 10)
        while not store.history_index_catch_up(window=5, max_windows=2):
            pass
        self.assertEqual(len(history.search(store, "bot-main", "增量索引条目")["hits"]), history.MAX_HITS)

    def test_scan_is_scope_bounded(self):
        self.fallback()
        for i in range(history.SCAN_WORKS + 2):
            sent = self.app.send("bot-main", {"text": "工作 %d" % i}, "scan-%d" % i)
            self.add(sent["work_id"], "标记 %d" % i)
        result = history.search(self.app.store, "bot-main", "标记")
        self.assertFalse(result["exhaustive"])
        self.assertLessEqual(len(result["hits"]), history.MAX_HITS)

    # --- runtime tool / invariants -----------------------------------------------

    def test_tool_is_readonly_and_runs_without_model_or_thread(self):
        class NoModel:
            def complete(self, *args, **kwargs):
                raise AssertionError("retrieval must not call a model")
        runtime = Runtime(self.app, NoModel())
        self.assertIn("search_history", Runtime.READONLY_TOOLS)
        self.assertIn("search_history", {t["function"]["name"] for t in runtime.tools_for(
            {"bot_id": "bot-main"})})
        sent = self.app.send("bot-main", {"text": "安排"}, "tool")
        ref = self.add(sent["work_id"], "上周的日报草稿")
        threads = threading.active_count()
        statuses = {w["status"] for w in self.app.store.find("work", limit=1000)}
        call = {"id": "call-history", "function": {
            "name": "search_history", "arguments": json.dumps({"query": "日报"})}}
        result = runtime.durable_tool({"id": sent["turn_id"]}, call)
        self.assertEqual([h["evidence_ref"] for h in result["hits"]], [ref])
        self.assertIn("不可信", result["notice"])
        self.assertEqual(threading.active_count(), threads)
        self.assertEqual({w["status"] for w in self.app.store.find("work", limit=1000)}, statuses)
        # Journaled like any read-only tool: a replay returns the recorded result.
        self.assertEqual(runtime.durable_tool({"id": sent["turn_id"]}, call), result)
        empty = {"id": "call-empty", "function": {"name": "search_history", "arguments": "{}"}}
        self.assertEqual(runtime.durable_tool({"id": sent["turn_id"]}, empty)["error"],
                         "query_or_work_id_required")

    def test_read_work_and_search_share_one_visibility_predicate(self):
        other = self.app.create_bot({"name": "编辑", "role": "编辑"})
        theirs = self.app.send(other["id"], {"text": "别人的"}, "pred")
        with self.assertRaises(Fault) as caught:
            history.visible_work(self.app.store, "bot-main", theirs["work_id"])
        self.assertEqual(caught.exception.code, "work_forbidden")
        mine = self.app.send("bot-main", {"text": "我的"}, "pred-mine")
        runtime = Runtime(self.app)
        turn = self.app.store.get("turn", mine["turn_id"])
        with self.assertRaises(Fault):
            runtime.invoke(turn, "read_work", {"work_id": theirs["work_id"]})


if __name__ == "__main__":
    unittest.main()
