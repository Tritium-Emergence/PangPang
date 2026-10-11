""": explicit Web "continue this Work" is deterministic, Jev-free continuation.

Assertions stay on attribution and the continued_from field only; the history
projection format of continued_from belongs to and is deliberately not pinned.
"""
import http.cookiejar
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from test_bot_runtime import config
from pangpang.http import serve
from pangpang.service import Service
from pangpang.store import Fault


class ExplicitContinueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev_key = os.environ.pop("TYPESAFE_API_KEY", None)
        self.app = Service(config(self.root), self.root / "data")
        self.jev_calls = 0

        def classify(*args, **kwargs):
            self.jev_calls += 1
            return {"choice": "NEW_TOPIC", "confidence":.99, "probabilities": {}}
        self.app.semantic_gate.classify_continuation = classify

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()
        if self._prev_key is not None:
            os.environ["TYPESAFE_API_KEY"] = self._prev_key

    def _continue(self, work_id, key, **extra):
        body = dict({"text": "接着做", "continue_work_id": work_id,
                     "stream_id": "tab-x"}, **extra)
        routing = self.app.resolve_continuation("bot-main", body)
        return routing, self.app.send("bot-main", body, key, routing=routing)

    def _enable_jev(self):
        os.environ["TYPESAFE_API_KEY"] = "test-key"
        self.assertTrue(self.app.semantic_gate.continuation_configured)

    def test_explicit_continue_without_jev_targets_the_selected_work(self):
        self.assertFalse(self.app.semantic_gate.continuation_configured)
        first = self.app.send("bot-main", {"text": "阿里估值"}, "ex-a")
        routing, sent = self._continue(first["work_id"], "ex-b")
        self.assertEqual(routing["route_source"], "explicit")
        self.assertEqual(routing["route_quality"], "deterministic")
        self.assertEqual(sent["work_id"], first["work_id"])
        self.assertEqual(self.jev_calls, 0)
        turn = self.app.store.get("turn", sent["turn_id"])
        # deterministic, never `fallback`: no weak-route approval upgrade.
        self.assertEqual(turn["route_quality"], "deterministic")
        event = self.app.store.get("routing_event", sent["routing_event_id"])
        self.assertEqual(event["route_source"], "explicit")

    def test_explicit_continue_outranks_a_configured_classifier_with_zero_rtt(self):
        self._enable_jev()
        first = self.app.send("bot-main", {"text": "阿里估值"}, "rtt-a")
        routing, sent = self._continue(first["work_id"], "rtt-b")
        self.assertEqual(sent["work_id"], first["work_id"])
        self.assertEqual(routing["route_source"], "explicit")
        self.assertEqual(self.jev_calls, 0)

    def test_foreign_or_missing_work_is_rejected_identically(self):
        other = self.app.create_bot({"name": "研究员", "role": "研究"})
        foreign = self.app.send(other["id"], {"text": "别人的活"}, "foreign-a")
        for target in (foreign["work_id"], "work-does-not-exist"):
            with self.assertRaises(Fault) as caught:
                self.app.resolve_continuation("bot-main", {
                    "text": "继续", "continue_work_id": target})
            self.assertEqual(caught.exception.code, "continue_work_forbidden")
            self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.jev_calls, 0)

    def test_non_conversation_or_child_work_is_not_continuable(self):
        first = self.app.send("bot-main", {"text": "阿里估值"}, "kind-a")
        child = dict(self.app.store.get("work", first["work_id"]))
        child.update(id="work-child", parent_work_id=first["work_id"])
        self.app.store.put("work", child)
        with self.assertRaises(Fault) as caught:
            self.app.resolve_continuation("bot-main", {
                "text": "继续", "continue_work_id": "work-child"})
        self.assertEqual(caught.exception.code, "continue_work_not_continuable")

    def test_malformed_continue_work_id_is_rejected(self):
        for bad in ("", " ", 42, ["x"], "w" * 129):
            with self.assertRaises(Fault) as caught:
                self.app.resolve_continuation("bot-main", {
                    "text": "继续", "continue_work_id": bad})
            self.assertEqual(caught.exception.code, "invalid_continue_work_id")

    def test_cancelled_work_is_forked_with_continued_from_not_revived(self):
        first = self.app.send("bot-main", {"text": "阿里估值"}, "fork-a")
        work = self.app.store.get("work", first["work_id"])
        work.update(status="cancelled")
        self.app.store.put("work", work)
        _, sent = self._continue(first["work_id"], "fork-b")
        self.assertNotEqual(sent["work_id"], first["work_id"])
        fork = self.app.store.get("work", sent["work_id"])
        self.assertEqual(fork["continued_from"], first["work_id"])
        self.assertEqual(self.app.store.get("work", first["work_id"])["status"], "cancelled")

    def _deliver(self, key):
        first = self.app.send("bot-main", {"text": "阿里估值"}, key)
        work = self.app.store.get("work", first["work_id"])
        work.update(status="delivered")
        self.app.store.put("work", work)
        return first["work_id"]

    def test_delivered_work_is_forked_with_continued_from_not_revived(self):
        self._enable_jev()
        delivered = self._deliver("dlv-a")
        routing, sent = self._continue(delivered, "dlv-b")
        self.assertEqual(routing["route_source"], "explicit")
        self.assertNotEqual(sent["work_id"], delivered)
        self.assertEqual(self.app.store.get("work", sent["work_id"])["continued_from"], delivered)
        self.assertEqual(self.app.store.get("work", delivered)["status"], "delivered")
        self.assertEqual(self.jev_calls, 0)

    def test_non_explicit_routes_keep_existing_delivered_reuse(self):
        # Scope lock: only the explicit route forks delivered Works. Jev and the
        # trusted internal work_id path keep today's in-place reuse.
        self._enable_jev()
        self.app.semantic_gate.classify_continuation = (
            lambda *a, **k: {"choice": "CONTINUE_CURRENT", "confidence":.99, "probabilities": {}})
        delivered = self._deliver("keep-a")
        body = {"text": "接着做", "work_hint_id": delivered, "stream_id": "tab-k"}
        routing = self.app.resolve_continuation("bot-main", body)
        self.assertEqual(routing["route_source"], "jev")
        sent = self.app.send("bot-main", body, "keep-b", routing=routing)
        self.assertEqual(sent["work_id"], delivered)
        self.assertNotIn("continued_from", self.app.store.get("work", delivered))

        internal = self._deliver("keep-c")
        sent = self.app.send("bot-main", {"text": "接着做", "work_id": internal}, "keep-d")
        self.assertEqual(sent["work_id"], internal)

    def _restricted_terminal(self, key, status):
        first = self.app.send("bot-main", {"text": "阿里估值"}, key)
        project = self.app.create_project("bot-main", {"title": "P"})
        work = self.app.store.get("work", first["work_id"])
        work.update(status=status, project_id=project["id"],
                    constraints=["必须保留的硬约束"],
                    required_host_id="local", required_installation_id="codex-local")
        self.app.store.put("work", work)
        return work

    def _assert_restrictions_kept(self, old, new):
        for field in ("project_id", "constraints", "required_host_id", "required_installation_id"):
            self.assertEqual(new.get(field), old[field], field)

    def test_explicit_fork_keeps_durable_restrictions(self):
        for status in ("delivered", "cancelled"):
            old = self._restricted_terminal("keep-r-" + status, status)
            _, sent = self._continue(old["id"], "keep-r2-" + status)
            new = self.app.store.get("work", sent["work_id"])
            self.assertEqual(self.app.store.get("work", old["id"])["status"], status)
            self.assertEqual(new["continued_from"], old["id"])
            self._assert_restrictions_kept(old, new)
            self.assertTrue(new["grant_write"])

    def test_explicit_fork_message_level_override_still_applies(self):
        self.app.execution.hosts["other"] = dict(self.app.execution.hosts["local"])
        old = self._restricted_terminal("ovr-r", "delivered")
        _, sent = self._continue(old["id"], "ovr-r2", host_id="other")
        new = self.app.store.get("work", sent["work_id"])
        self.assertEqual(new["required_host_id"], "other")
        self.assertEqual(new["required_installation_id"], "codex-local")
        self.assertEqual(new["constraints"], old["constraints"])

    def test_non_explicit_legacy_fork_is_unchanged(self):
        # Scope lock: the trusted internal work_id fork is not part of.
        old = self._restricted_terminal("legacy-r", "cancelled")
        sent = self.app.send("bot-main", {"text": "接着做", "work_id": old["id"]}, "legacy-r2")
        new = self.app.store.get("work", sent["work_id"])
        self.assertEqual(new["continued_from"], old["id"])
        self.assertEqual(new["constraints"], [])
        self.assertNotIn("required_host_id", new)

    def test_chat_message_cannot_withhold_or_grant_write(self):
        # The per-message read-only checkbox is gone: allow_write in a chat body is
        # ignored, so it can neither silently and permanently gate a conversation's
        # later tasks behind approval nor upgrade a restricted Work.
        first = self.app.send("bot-main", {"text": "阿里估值", "allow_write": False}, "ro-a")
        self.assertTrue(self.app.store.get("work", first["work_id"])["grant_write"])
        self._continue(first["work_id"], "ro-b", allow_write=False)
        self.assertTrue(self.app.store.get("work", first["work_id"])["grant_write"])
        work = self.app.store.get("work", first["work_id"])
        work["grant_write"] = False
        self.app.store.put("work", work)
        self._continue(first["work_id"], "ro-c", allow_write=True)
        self.assertFalse(self.app.store.get("work", first["work_id"])["grant_write"])

    def test_next_plain_message_does_not_inherit_the_explicit_target(self):
        first = self.app.send("bot-main", {"text": "阿里估值"}, "once-a")
        self._continue(first["work_id"], "once-b")
        # The UI's focus hint still points at the Work, but a hint is not authority.
        plain = {"text": "冰箱怎么修", "work_hint_id": first["work_id"], "stream_id": "tab-x"}
        routing = self.app.resolve_continuation("bot-main", plain)
        self.assertEqual(routing["route_source"], "unconfigured_new")
        sent = self.app.send("bot-main", plain, "once-c", routing=routing)
        self.assertNotEqual(sent["work_id"], first["work_id"])


class ExplicitContinueHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev_key = os.environ.pop("TYPESAFE_API_KEY", None)
        self.app = Service(config(self.root), self.root / "data")
        self.token = "x" * 32
        self.server = serve(self.app, self.token, port=0)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.post("login", {"token": self.token}, "login")

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.app.store.close(); self.tmp.cleanup()
        if self._prev_key is not None:
            os.environ["TYPESAFE_API_KEY"] = self._prev_key

    def post(self, path, body, key):
        req = urllib.request.Request(
            self.origin + "/api/v1/" + path, data=json.dumps(body).encode(),
            headers={"Origin": self.origin, "Content-Type": "application/json",
                     "Idempotency-Key": key})
        with self.client.open(req) as r:
            return json.load(r)

    def test_legacy_work_id_stays_a_hint_while_continue_work_id_is_authoritative(self):
        first = self.app.send("bot-main", {"text": "阿里估值"}, "seed")
        legacy = self.post("bots/bot-main/messages",
                           {"text": "继续", "work_id": first["work_id"]}, "legacy")
        self.assertNotEqual(legacy["work_id"], first["work_id"])
        self.assertEqual(legacy["route_source"], "unconfigured_new")
        explicit = self.post("bots/bot-main/messages",
                             {"text": "继续", "continue_work_id": first["work_id"]}, "explicit")
        self.assertEqual(explicit["work_id"], first["work_id"])
        self.assertEqual(explicit["route_source"], "explicit")

    def test_foreign_target_is_refused_over_http_and_nothing_is_committed(self):
        other = self.app.create_bot({"name": "研究员", "role": "研究"})
        foreign = self.app.send(other["id"], {"text": "别人的活"}, "foreign")
        before = len(self.app.store.find("message", bot_id="bot-main", limit=1000))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("bots/bot-main/messages",
                      {"text": "继续", "continue_work_id": foreign["work_id"]}, "deny")
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(json.load(caught.exception)["code"], "continue_work_forbidden")
        after = len(self.app.store.find("message", bot_id="bot-main", limit=1000))
        self.assertEqual(before, after)

    def test_replayed_post_does_not_call_the_model_again(self):
        from unittest.mock import patch
        first = self.app.send("bot-main", {"text": "阿里估值"}, "seed-replay")
        calls = []

        class FakePiModel:
            def __init__(self, service):
                pass

            def complete(self, profile, messages, tools, on_proc=None, cancelled=None):
                calls.append(1)
                return {"role": "assistant", "content": '{"choice": "CURRENT"}'}, {}

        body = {"text": "继续核对毛利率", "work_hint_id": first["work_id"], "stream_id": "tab-replay"}
        with patch("pangpang.pi_runtime.PiModel", FakePiModel):
            sent = self.post("bots/bot-main/messages", body, "replay-key")
            replayed = self.post("bots/bot-main/messages", body, "replay-key")
        self.assertEqual(sent["work_id"], first["work_id"])
        self.assertEqual(replayed["message_id"], sent["message_id"])
        self.assertEqual(len(calls), 1)

    def test_side_effect_correction_is_refused_before_commit(self):
        target = self.app.send("bot-main", {"text": "阿里估值", "stream_id": "tab-http"}, "http-a")
        wrong = self.app.send("bot-main", {"text": "冰箱", "stream_id": "tab-http"}, "http-b")
        turn = self.app.store.get("turn", wrong["turn_id"])
        turn["history"] = [{"role": "tool", "content": "{\"task_id\": \"task-already\"}"}]
        self.app.store.put("turn", turn)
        before = len(self.app.store.find("message", bot_id="bot-main", limit=1000))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("bots/bot-main/messages", {
                "text": "移到阿里", "correct_to_work_id": target["work_id"],
                "stream_id": "tab-http", "last_seen_event_id": wrong["message_id"],
            }, "http-deny")
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(json.load(caught.exception)["code"], "routing_correction_rejected")
        after = len(self.app.store.find("message", bot_id="bot-main", limit=1000))
        self.assertEqual(before, after)
        self.assertEqual(self.app.store.find("routing_correction", limit=10), [])


STORE_TS = Path(__file__).resolve().parents[1] / "web-src" / "store.ts"
# The harness below transpiles store.ts in-process, so it needs the installed
# compiler, not just a node binary: on a checkout without `npm ci` the old
# which("node") guard let the test run and FAIL on "Cannot find module 'typescript'"
# instead of skipping.
TYPESCRIPT = Path(__file__).resolve().parents[1] / "node_modules" / "typescript"

# Runs the real React coordinator selection callback with state-setter stubs.
# Compiled app.js has no legacy global switchBot; browser specs cover its UI callers.
SWITCH_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const code = src.slice(src.indexOf('const select = useCallback('), src.indexOf('const liveSelected = useCallback('));
const ctx = vm.createContext({
  selectedRef: {current: 'bot-a'}, generation: {current: 0},
  selected: 'bot-a', attached: ['art-1'], target: 'work-a',
  messages: ['old-message'], previews: new Map([['old', 'preview']]),
  useCallback(fn) {return fn;},
});
ctx.setSelected = (id) => {ctx.selected = id;};
ctx.setMessages = (value) => {ctx.messages = value;};
ctx.setPreviews = (value) => {ctx.previews = value;};
ctx.setContinueTarget = (value) => {ctx.target = value;};
ctx.setAttached = (value) => {ctx.attached = typeof value === 'function' ? value(ctx.attached): value;};
const js = require('typescript').transpileModule(code, {compilerOptions: {target: 7, module: 99}}).outputText;
vm.runInContext(js + ';globalThis.switchBot = select;', ctx);
const out = [];
const snap = () => out.push({selected: ctx.selected, attached: [...ctx.attached], target: ctx.target});
ctx.switchBot('bot-a'); snap();
ctx.switchBot('bot-b'); snap();
ctx.switchBot('bot-a'); snap();
if (ctx.generation.current !== 2 || ctx.messages.length || ctx.previews.size) throw Error('selection isolation failed');
console.log(JSON.stringify(out));
"""


class ExplicitContinueBotSwitchTests(unittest.TestCase):
    """ review (Codex P2 / CCC 1): the continuation target is one-shot and
    per-Bot, so every Bot switch must clear it -- not only the Bot list."""

    def test_selection_is_owned_by_the_coordinator(self):
        source = STORE_TS.read_text(encoding="utf-8")
        # A state write outside select would bypass target/attachment/epoch fencing.
        self.assertEqual(source.count("setSelected(botId)"), 1)
        callback = source[source.index("const select = useCallback("):source.index("const liveSelected = useCallback(")]
        self.assertIn("setSelected(botId)", callback)
        components = STORE_TS.parent / "components"
        for path in components.glob("*.tsx"):
            self.assertNotIn("setSelected(", path.read_text(encoding="utf-8"), str(path))

    @unittest.skipUnless(shutil.which("node") and TYPESCRIPT.exists(), "node + node_modules/typescript are required by the Web UI harness")
    def test_switch_bot_clears_continuation_target_and_attachments(self):
        result = subprocess.run(["node", "-e", SWITCH_HARNESS, str(STORE_TS)],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        same, away, back = json.loads(result.stdout)
        self.assertEqual(same, {"selected": "bot-a", "attached": ["art-1"], "target": "work-a"})
        self.assertEqual(away, {"selected": "bot-b", "attached": [], "target": None})
        self.assertEqual(back, {"selected": "bot-a", "attached": [], "target": None})


if __name__ == "__main__":
    unittest.main()
