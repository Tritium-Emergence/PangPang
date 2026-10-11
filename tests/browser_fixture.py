"""Isolated browser-test fixture. Real Pi, HTTP, device mailbox, vault and browser;
deterministic LLM/CLI discovery. Never used by the product entrypoint.

Usage: python tests/browser_fixture.py [scenario] [port]
  scenario = onboarding (default) | studio | correction | catalog-window
  port = listen port (default 8799)

`onboarding` is unchanged from the original single-fixture file so
tests/browser/onboarding.spec.mjs keeps passing verbatim. `studio` boots a
fully-configured local coordinator pre-seeded with durable state so the
behavior contracts can assert against real server truth. `correction` adds a
configured continuation router with a deterministic stub for the
correction-lineage contract. All seeding is fixture-only; no test-only route is
ever added to the product HTTP surface. Security-sensitive transitions (task
approval) are seeded via REAL service calls, not raw store.put, so the contract
exercises genuine state machines.
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_model_protocol import Endpoint
from test_bot_runtime import call, config
from pangpang.service import Service
from pangpang.runtime import Runtime
from pangpang.http import serve
from pangpang.onboarding import empty_config
from pangpang.connector import Connector
from pangpang.local_setup import LocalSetup
from pangpang.store import Store, uid
from pangpang.vault import Vault

TOKEN = "browser-access-token-32-characters"
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "onboarding"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8799


class StreamingEndpoint:
    """OpenAI-compatible chat endpoint that delivers the SSE body progressively (one
    content chunk at a time with a small gap) so the browser observably renders a
    growing streaming preview before the final message. A single immediate chunk
    races the preview out of existence. The full body length is known up front, so a
    correct Content-Length is sent (the node worker's HTTP client needs proper framing
    — streaming without Content-Length crashes it); only the WRITE is paced."""

    def __init__(self, pieces, gap=0.12):
        self.received = []
        outer = self
        frames = []
        for i, piece in enumerate(pieces):
            delta = {"role": "assistant", "content": piece} if i == 0 else {"content": piece}
            frames.append({"id": "test", "object": "chat.completion.chunk", "model": "test",
                           "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        frames.append({"id": "test", "object": "chat.completion.chunk", "model": "test",
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        blobs = [("data: " + json.dumps(f) + "\n\n").encode() for f in frames]
        blobs.append(b"data: [DONE]\n\n")
        total = sum(len(b) for b in blobs)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                outer.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(total))
                self.end_headers()
                try:
                    for i, blob in enumerate(blobs):
                        self.wfile.write(blob)
                        self.wfile.flush()
                        if i < len(blobs) - 1:
                            time.sleep(gap)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"


def run_onboarding():
    # Hermetic routing: an ambient TYPESAFE_API_KEY flips continuation routing into
    # the "Jev configured" branch, whose failed classification yields the weak
    # `fallback` route and fail-closes create_bot/setup_agent (route_confirmation_required).
    # The fixture intends no-Jev deterministic routing regardless of the runner's env.
    os.environ.pop("TYPESAFE_API_KEY", None)
    root = Path(tempfile.mkdtemp(prefix="pangpang-browser-"))
    app = Service(empty_config(), root / "cloud")
    data = root / "device"; data.mkdir()
    workspace = root / "workspace"; workspace.mkdir() # executor root must not overlap the control-plane dir (check_control_separation)
    db = Store(data / "connector.db"); identity = uid("device")
    db.put("identity", {"id": "device", "instance_id": identity}); db.close()
    v = Vault(data / "vault")
    pair = app.onboarding.pair({"code": app.onboarding.pairing_code()["code"], "instance_id": identity,
                               "public_key": v.public_key(), "name": "浏览器测试电脑", "os": "Linux"})
    v.set("device-token", pair["token"]); v.close()
    device = Connector({"host_id": pair["host_id"], "cloud_url": "http://127.0.0.1:%d" % PORT,
                        "credential_ref": "device-token", "hosts": [{"id": pair["host_id"], "transport": "local",
                        "os": "Linux", "allowed_roots": [str(workspace)]}], "installations": [], "resources": []}, data)

    def response(payload, n):
        names = [t["function"]["name"] for t in payload.get("tools", [])]
        if names == ["setup_probe"]:
            return {"content": "ready"} if payload["messages"][-1]["role"] == "tool" else call("setup_probe", value="pangpang-ready")
        text = str(payload["messages"][-1].get("content", ""))
        if payload["messages"][-1]["role"] == "tool" or "setup_result" in text:
            return {"content": "操作已完成，状态已保存。"}
        if "连接 Codex" in text:
            return call("setup_agent", host_id=pair["host_id"], action="configure", adapter="codex", auth="api_key")
        return call("create_bot", name="公众号主理", role="负责选题与文章")

    endpoint = Endpoint(response)
    (root / "model-url").write_text(endpoint.url)
    # Browser receives only test model address through the public fixture's initial model field.
    app.profiles["default"]["model"] = endpoint.url
    runtime = Runtime(app)

    def loop():
        while True:
            try: runtime.step()
            except Exception: pass
            time.sleep(.1)

    def device_loop():
        with patch.object(LocalSetup, "discover", return_value={"inventory": [{"adapter": "codex", "binary": sys.executable}]}):
            while True:
                try:
                    device.tick()
                    refs = [e.get("secret_refs", {}) for e in device.execution.installations.values()]
                    if refs and device.vault.has(next(iter(refs[0].values()), "missing")):
                        value = device.vault.get(next(iter(refs[0].values())))
                        if value == "BROWSER-SECRET-EXACT":
                            app.store.put("inventory", {"id": "browser-secret-check", "items": [{"adapter": "browser-decrypted"}]})
                except Exception: pass
                time.sleep(.1)

    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=device_loop, daemon=True).start()
    serve(app, TOKEN, port=PORT).serve_forever()


def run_studio():
    """A configured coordinator pre-seeded with durable state for the studio contracts.

    Bots: bot-main (streaming/continuation/notifications/routine/artifacts),
    文字编辑 (Bot-switch target), 通知测试 (unread badge), 审批测试 (REAL
    awaiting_approval task for the RCB transition contract), 恢复测试 (unknown task),
    取消测试 (cancel_requested task).
    Isolating the unknown/cancel/approval tasks on their own bots keeps the other
    bots' conversation activity clean."""
    root = Path(tempfile.mkdtemp(prefix="pangpang-studio-"))
    workspace = root / "workspace"; workspace.mkdir()
    endpoint = StreamingEndpoint(["好的，", "我现在就", "帮你处理", "这个问题。"])
    cfg = config(workspace)
    cfg["models"][0]["endpoint"] = endpoint.url
    app = Service(cfg, root / "data")
    app.probe("codex-local")

    def user_turn(bot_id, text, key):
        sent = app.send(bot_id, {"text": text}, key)
        return app.store.get("turn", sent["turn_id"]), sent["work_id"]

    def seed_task(bot_id, work_id, status, segs):
        exec_id = "exec-%032x" % (abs(hash(work_id + status)) % (16 ** 32))
        return app.store.put("task", {"id": uid("task"), "status": status, "host_id": "local",
            "installation_id": "codex-local", "workspace_key": ["host-local", list(segs)],
            "access_mode": "write", "resource_keys": [], "execution_id": exec_id, "spec_digest": "seed",
            "created": time.time(), "owner_bot_id": bot_id, "work_id": work_id,
            "prompt": "长时间运行的后台任务", "inputs": [], "outputs": []})

    # --- bot-main durable context -------------------------------------------
    # >10 filler works FIRST — the work-grid sorts newest-first by INSERTION order, so
    # seeding these first keeps them oldest (last pages) while the real recent works
    # below stay on page 1. This lets the grid paginate (pagination viewport-anchor
    # spec, a mid-document grid) without pushing the continuation card off page 1.
    for i in range(12):
        app.store.put("work", {"id": uid("work"), "owner_bot_id": "bot-main", "goal": "历史工作 #%02d" % i,
            "status": "active", "parent_work_id": None, "reply_to": None, "mode": "deliver_to_user",
            "depth": 0, "refs": [], "grant_write": True, "constraints": [], "handoff": None,
            "kind": "conversation", "created": 0.001 * (i + 1), "checkpoint": {}})
    user_turn("bot-main", "帮我整理上周的会议纪要", "seed-continue") # continuable top-level Work
    scroll_sent = app.send("bot-main", {"text": "我们来过一遍这个项目的进度"}, "seed-scroll")
    scroll_work = scroll_sent["work_id"]
    for i in range(8): # enough history to overflow a short viewport (scroll-stability spec)
        who = ("user", "user") if i % 2 == 0 else ("assistant", "bot-main")
        app.store.put("message", {"id": uid("msg"), "bot_id": "bot-main", "work_id": scroll_work,
            "kind": who[0], "from": who[1], "created": time.time() + i * 0.001,
            "content": "这是第 %d 条用于撑高对话高度、验证后台刷新时滚动位置稳定的历史消息。" % (i + 1)})
    # Uniquely-titled unread notifications: independent specs each consume a different one
    # (avoids shared-count coupling). Specs derive counts from server truth, never hardcode.
    app.notification("bot-main", "result", "你的周报草稿已生成，待查看。")
    app.notification("bot-main", "result", "本周数据汇总已就绪。")
    app.notification("bot-main", "result", "后台刷新触发用占位通知。")
    # One unread result bound to a Work: the 待办 inbox lists it as a clickable
    # "done, unread" item (jump to the Bot + mark read). The work_id-less ones above
    # stay in the Bot badge only (the inbox filters on work_id).
    app.notification("bot-main", "result", "进度梳理已完成，待查看。", work_id=scroll_work)
    # Pending Routine (approval + standing-authority + dual-render specs).
    turn, _ = user_turn("bot-main", "以后每天上午九点给我出一份日报", "seed-routine")
    app.create_routine("bot-main", {"instruction": "每天上午九点汇总昨日进展并产出日报",
        "timezone": "Asia/Shanghai", "schedule": {"type": "daily", "time": "09:00"}}, turn=turn)
    # >10 artifacts so the artifacts grid paginates (pagination viewport-anchor spec).
    for i in range(12):
        app.store.put("artifact", {"id": uid("artifact"), "path": str(workspace / ("产物_%02d.txt" % i)),
            "host_id": "local", "size": 128 + i, "owner_bot_id": "bot-main", "work_id": None,
            "source": "seed", "created": time.time() + i * 0.001})
    # One REAL artifact with a Chinese filename + real bytes, so the download endpoint
    # reads it and must emit an RFC 5987 UTF-8 Content-Disposition.
    report = workspace / "季度报告.html"
    report.write_text("<h1>季度报告</h1><p>示例内容</p>", encoding="utf-8")
    app.import_file("bot-main", {"host_id": "local", "path": str(report)})

    # --- second bot (Bot-switch clears continuation/attachments spec, /a-prior-bug) --
    app.create_bot({"name": "文字编辑", "role": "负责润色与排版"})

    # --- 通知测试: unread results for the badge contract. Opening a conversation does
    # not mark them read; unread stays until an explicit mark-notification. The badge
    # lives on a bot no other spec opens so the count stays stable.
    nbot = app.create_bot({"name": "通知测试", "role": "验证未读角标"})
    app.notification(nbot["id"], "result", "已回复：稿件润色。")
    app.notification(nbot["id"], "result", "已回复：排版调整。")

    # --- 审批测试: a REAL awaiting_approval task (RCB in-flight/rejected contract) ----
    abot = app.create_bot({"name": "审批测试", "role": "执行需要批准的写任务"})
    _, a_work = user_turn(abot["id"], "做一个需要我先批准的写操作", "seed-approval")
    w = app.store.get("work", a_work); w["grant_write"] = False; app.store.put("work", w)
    app.submit_task(abot["id"], a_work, {"prompt": "写入一个文件（需批准）"}) # -> awaiting_approval (real transition)

    # --- 恢复测试: an unknown task quarantining its workspace --------
    rbot = app.create_bot({"name": "恢复测试", "role": "执行长任务"})
    _, r_work = user_turn(rbot["id"], "跑一个很久的离线任务", "seed-unknown")
    seed_task(rbot["id"], r_work, "unknown", ("repo",))

    # --- 取消测试: a cancel_requested task (RCB render: 正在取消, never 已取消) --------
    cbot = app.create_bot({"name": "取消测试", "role": "执行长任务"})
    _, c_work = user_turn(cbot["id"], "开始一个可以中途取消的任务", "seed-cancel")
    seed_task(cbot["id"], c_work, "cancel_requested", ("repo2",))

    # --- 隔离测试: ONE bot with two top-level sibling Works. Work A is the current
    # conversation (no unknown); sibling Work B carries an unknown task. The 
    # action-scope contract: Work B's unknown must NOT appear in Work A's inspector
    # action targets (recursive current-Work subtree only, never a sibling).
    ibot = app.create_bot({"name": "隔离测试", "role": "验证动作作用域不跨兄弟 Work"})
    _, iso_a = user_turn(ibot["id"], "当前工作 A（无未知任务）", "seed-iso-a")
    _, iso_b = user_turn(ibot["id"], "兄弟工作 B（带未知任务）", "seed-iso-b")
    seed_task(ibot["id"], iso_b, "unknown", ("iso-b",))

    # --- 停止测试: Work-level Stop truth (P1-B). Three works:
    # stop-cr: Work.cancel_requested=true but NO cancel_requested task -> inspector must
    # show "正在取消…" (Work-level), not an actionable Stop.
    # stop-blk: a BLOCKED non-terminal Work with a FAILED task and no active task
    # (mirrors) -> Stop must STILL be offered (any non-terminal Work).
    # stop-done: terminal cancelled + lingering cancel_requested=true -> terminal reality
    # wins; inspector must NOT regress to "正在取消…" request-progress.
    sbot = app.create_bot({"name": "停止测试", "role": "验证工作级停止真相"})
    # Seed these works directly (not via a user turn) so the runtime loop cannot advance
    # them to a terminal/delivered state before the assertions — their status is fixed.
    def seed_work(bot_id, goal, status, **extra):
        wid = uid("work")
        app.store.put("work", {"id": wid, "owner_bot_id": bot_id, "goal": goal, "status": status,
            "parent_work_id": None, "reply_to": None, "mode": "deliver_to_user", "depth": 0,
            "refs": [], "grant_write": True, "constraints": [], "handoff": None,
            "kind": "conversation", "created": time.time(), "checkpoint": {}, **extra})
        return wid
    stop_cr = seed_work(sbot["id"], "工作级正在取消（无取消中任务）", "active", cancel_requested=True)
    # Exact generic-recovery shape after a hard required-output miss:
    # Work blocked + durable reason/obligation, Task failed, no active execution.
    stop_blk = seed_work(sbot["id"], "受阻但非终态（可停止）", "blocked",
                         error="required_output_missing", pending_required_outputs=["final.docx"])
    failed79 = seed_task(sbot["id"], stop_blk, "failed", ("stop-blk",))
    failed79["error"] = "required_output_missing"
    app.store.put("task", failed79)
    seed_work(sbot["id"], "已取消终态（cancel_requested 遗留）", "cancelled", cancel_requested=True)

    runtime = Runtime(app)

    def loop():
        while True:
            try: runtime.step()
            except Exception: pass
            time.sleep(.1)

    threading.Thread(target=loop, daemon=True).start()
    serve(app, TOKEN, port=PORT).serve_forever()


def run_correction():
    """Correction-lineage browser contract. Ordinary sends are NEW_TOPIC.
    Moving a previous message is a user action on the continue control, not a
    classifier result."""
    root = Path(tempfile.mkdtemp(prefix="pangpang-correction-"))
    workspace = root / "workspace"; workspace.mkdir()
    endpoint = StreamingEndpoint(["好的，", "这是", "回复。"])
    cfg = config(workspace)
    cfg["models"][0]["endpoint"] = endpoint.url
    # Plan A: continuation routing active even with the pre-commit gate OFF; a key only
    # flips continuation_configured true. classify_continuation is stubbed → no network.
    cfg["semantic_gate"] = {"mode": "off", "continuation_routing": True, "model": "jev-latest",
        "timeout_s": 0.2, "continuation_switch_threshold": 0.75, "correction_threshold": 0.85}
    os.environ["TYPESAFE_API_KEY"] = "correction-fixture-key"
    app = Service(cfg, root / "data")

    def classify(content, *args, **kwargs):
        return {"choice": "NEW_TOPIC", "confidence": 0.96, "probabilities": {}}
    app.semantic_gate.classify_continuation = classify

    runtime = Runtime(app)

    def loop():
        while True:
            try: runtime.step()
            except Exception: pass
            time.sleep(.1)

    threading.Thread(target=loop, daemon=True).start()
    serve(app, TOKEN, port=PORT).serve_forever()


def run_catalog_window():
    """One configured profile whose stored window is 32K. No seeded turns."""
    root = Path(tempfile.mkdtemp(prefix="pangpang-catalog-window-"))
    workspace = root / "workspace"
    workspace.mkdir()
    cfg = config(workspace)
    cfg["models"][0]["context_budget"] = 32000
    cfg["models"][0]["output_tokens"] = 8192
    app = Service(cfg, root / "data")
    serve(app, TOKEN, port=PORT).serve_forever()


if SCENARIO == "studio":
    run_studio()
elif SCENARIO == "correction":
    run_correction()
elif SCENARIO == "catalog-window":
    run_catalog_window()
else:
    run_onboarding()