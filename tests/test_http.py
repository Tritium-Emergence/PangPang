import http.cookiejar
import json
import threading
import unittest
import urllib.error
import urllib.request
import tempfile
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.http import serve


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.app=Service(config(self.root),self.root/"data")
        self.token='x'*32
        self.server=serve(self.app,self.token,port=0)
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.app.store.close();self.tmp.cleanup()
    def request(self,path,body=None,origin=True,key='http-test'):
        headers={}
        if origin:headers['Origin']=self.origin if origin is True else origin
        if body is not None:headers.update({'Content-Type':'application/json','Idempotency-Key':key})
        req=urllib.request.Request(self.origin+'/api/v1/'+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
        return self.client.open(req)
    def login(self):
        with self.request('login',{'token':self.token},key='login') as r:
            self.assertEqual(r.status,200)

    def test_chat_upload_accepts_a_file_above_the_default_body_bound(self):
        import base64
        self.login()
        ws = self.root / 'ws'; ws.mkdir()
        self.app.execution.hosts['local']['allowed_roots'] = [str(ws)]
        self.app.execution.installations['codex-local']['cwd'] = str(ws) # browse is scoped to the work dir
        raw = b'\0' * (300 * 1024) # bigger than the 256 KiB bound of other mutations
        body = {'host_id': 'local', 'name': 'big.bin', 'data': base64.b64encode(raw).decode()}
        with self.request('bots/bot-main/uploads', body, key='up-1') as r:
            art = json.load(r)
        self.assertEqual(Path(art['path']).read_bytes(), raw)
        with self.request('hosts/local/browse', {'path': str(ws)}, key='br-1') as r:
            self.assertEqual(json.load(r)['entries'][0]['name'], '.pangpang-inbox')
        with self.assertRaises(urllib.error.HTTPError) as big:
            self.request('bots/bot-main/messages', {'text': 'x' * (300 * 1024)}, key='big-msg')
        self.assertEqual(big.exception.code, 413)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request('bots/bot-main/uploads', body, origin=False, key='up-2')
        self.assertIn(denied.exception.code, (401, 403))

    def test_attention_projection_survives_bounded_window_truncation(self):
        # a live Work/Task/unread Notification pushed past the snapshot's
        # newest-N window must NOT be silently projected as settled. The snapshot
        # unions live rows by status, so attention stays truthful under truncation.
        self.login()
        store = self.app.store
        # A non-terminal Work whose only running Task is old; overflow the 1000 task window.
        store.put('work', {'id': 'work-live', 'owner_bot_id': 'bot-main', 'goal': 'g',
                           'status': 'active', 'created': 1.0})
        store.put('task', {'id': 'task-live', 'work_id': 'work-live', 'owner_bot_id': 'bot-main',
                           'status': 'running', 'installation_id': 'i', 'host_id': 'h', 'created': 2.0})
        for i in range(1001): # newer terminal tasks evict task-live from the newest window
            store.put('task', {'id': 'task-fill-%d' % i, 'work_id': 'work-live', 'owner_bot_id': 'bot-main',
                               'status': 'succeeded', 'installation_id': 'i', 'host_id': 'h', 'created': 100.0 + i})
        # A terminal Work with an old unread result; overflow the 500 notification window.
        store.put('work', {'id': 'work-done', 'owner_bot_id': 'bot-main', 'goal': 'g',
                           'status': 'delivered', 'created': 1.5})
        store.put('notification', {'id': 'note-unread', 'bot_id': 'bot-main', 'kind': 'result',
                                   'work_id': 'work-done', 'read': False, 'created': 2.5})
        for i in range(501):
            store.put('notification', {'id': 'note-fill-%d' % i, 'bot_id': 'bot-main', 'kind': 'result',
                                       'work_id': 'work-done', 'read': True, 'created': 100.0 + i})
        with self.request('snapshot') as r:
            snap = json.load(r)
        by_id = {w['id']: w for w in snap['work']}
        # Without the live union both of these would read 'settled' (false negative).
        self.assertIn('work-live', by_id, 'live Work with out-of-window running task was dropped')
        self.assertEqual(by_id['work-live']['attention'], 'working')
        self.assertIn('work-done', by_id)
        self.assertEqual(by_id['work-done']['attention'], 'done_unread')
        self.assertGreaterEqual(snap['attention_summary']['working'], 1)
        self.assertGreaterEqual(snap['attention_summary']['done_unread'], 1)

    def test_attention_bounded_and_honest_under_large_live_backlog(self):
        # round 3/4: a huge single-domain live backlog must keep the hot
        # /snapshot path BOUNDED (fixed payload ceiling, no full drain) AND honest
        # (summary flagged incomplete), with no false `settled` for a Work whose
        # live task is within the bounded page.
        self.login()
        store = self.app.store
        for i in range(1001): # 1001 blocked threads: an unknown task on a live Work
            store.put('work', {'id': 'w-%04d' % i, 'owner_bot_id': 'bot-main', 'goal': 'g',
                               'status': 'active', 'created': float(i)})
            store.put('task', {'id': 't-%04d' % i, 'work_id': 'w-%04d' % i, 'owner_bot_id': 'bot-main',
                               'status': 'unknown', 'installation_id': 'i', 'host_id': 'h', 'created': float(i)})
        for i in range(1001): # and 1001 workless unknown resource claims
            store.put('claim', {'id': 'c-%04d' % i, 'key': 'k-%d' % i, 'status': 'unknown', 'created': float(i)})
        with self.request('snapshot') as r:
            snap = json.load(r)
        # Bounded payload: the full live backlog is never shipped (fixed ceilings).
        self.assertLessEqual(len(snap['work']), 1000)
        self.assertLessEqual(len(snap['tasks']), 1000)
        self.assertLessEqual(len(snap['claims']), 1000)
        # Honest: a single live domain exceeded the cap -> summary flagged incomplete.
        self.assertIs(snap['attention_summary']['complete'], False)
        self.assertGreaterEqual(snap['attention_summary']['blocked'], 1)
        # No false settled: the newest shipped Work's unknown task is within the page.
        by_id = {w['id']: w for w in snap['work']}
        self.assertEqual(by_id['w-1000']['attention'], 'blocked')

    def test_attention_live_turn_backlog_feeds_completeness(self):
        # round 4 (point 1): >1000 live turns must mark the snapshot incomplete,
        # so the system never renders "no running turn" as "idle". The live-turn
        # fetch is 1000-capped, so truncation must flow into attention_summary.complete.
        self.login()
        store = self.app.store
        for i in range(1001):
            store.put('turn', {'id': 'tn-%04d' % i, 'work_id': 'w-%04d' % i, 'bot_id': 'bot-main',
                               'status': 'running', 'created': float(i)})
        with self.request('snapshot') as r:
            snap = json.load(r)
        self.assertIs(snap['attention_summary']['complete'], False)
        self.assertLessEqual(len(snap['turns']), 1000 + 200) # live-turn page + recent window, bounded

    def test_attention_per_work_fail_closed_when_its_fact_is_outside_cap(self):
        # round 4 (point 2, the real RCB hole): a display Work whose OWN critical
        # fact is truncated out of the live page must NOT be projected `settled`
        # ("已就绪"). Under incompleteness an unproven-idle Work is `unknown`.
        self.login()
        store = self.app.store
        # Overflow the task live page with OTHER works' unknowns so the domain truncates.
        for i in range(1001):
            store.put('work', {'id': 'w-%04d' % i, 'owner_bot_id': 'bot-main', 'goal': 'g',
                               'status': 'active', 'created': float(i)})
            store.put('task', {'id': 't-%04d' % i, 'work_id': 'w-%04d' % i, 'owner_bot_id': 'bot-main',
                               'status': 'unknown', 'installation_id': 'i', 'host_id': 'h', 'created': float(i)})
        # One idle-looking display Work inserted LAST so it is the newest row and IS
        # shipped (store orders by insertion time, not the JSON `created` field).
        store.put('work', {'id': 'w-display', 'owner_bot_id': 'bot-main', 'goal': 'g',
                           'status': 'active', 'created': 9999.0})
        with self.request('snapshot') as r:
            snap = json.load(r)
        by_id = {w['id']: w for w in snap['work']}
        self.assertIn('w-display', by_id) # newest -> shipped
        self.assertIs(snap['attention_summary']['complete'], False)
        # The display Work cannot be proven idle under truncation -> fail-closed unknown.
        self.assertEqual(by_id['w-display']['attention'], 'unknown')
        self.assertGreaterEqual(snap['attention_summary']['indeterminate'], 1)

    def test_attention_recent_unread_result_done_unread_even_when_incomplete(self):
        # round 3: with a large unread backlog the summary is incomplete,
        # but a recent unread result (within the bounded newest page) still drives
        # done_unread; notifications payload stays bounded.
        self.login()
        store = self.app.store
        for i in range(1001): # old unread peer backlog
            store.put('notification', {'id': 'n-%04d' % i, 'bot_id': 'bot-main', 'kind': 'peer_message',
                                       'work_id': 'w-x', 'read': False, 'created': float(i)})
        store.put('work', {'id': 'w-target', 'owner_bot_id': 'bot-main', 'goal': 'g',
                           'status': 'delivered', 'created': 5000.0})
        store.put('notification', {'id': 'n-target', 'bot_id': 'bot-main', 'kind': 'result',
                                   'work_id': 'w-target', 'read': False, 'created': 5000.0}) # recent unread result
        with self.request('snapshot') as r:
            snap = json.load(r)
        by_id = {w['id']: w for w in snap['work']}
        self.assertEqual(by_id['w-target']['attention'], 'done_unread')
        self.assertIs(snap['attention_summary']['complete'], False)
        self.assertLessEqual(len(snap['notifications']), 500)

    def test_attention_terminal_unread_outside_notification_cap_not_settled(self):
        # round 5 (the one fix): a terminal Work whose unread result is pushed
        # PAST the notification live page must NOT be rendered settled ("已就绪").
        # The notification domain is truncated and we did not see the unread, so the
        # terminal Work is fail-closed to unknown (never a false read/settled).
        self.login()
        store = self.app.store
        store.put('work', {'id': 'w-target', 'owner_bot_id': 'bot-main', 'goal': 'g',
                           'status': 'delivered', 'created': 1.0})
        # Oldest-inserted so it is deterministically evicted from the newest-1000
        # unread page under find_page's (created DESC, rowid DESC) order when the
        # coarse insert clock ties (Windows time.time()).
        store.put('notification', {'id': 'n-0000-target', 'bot_id': 'bot-main', 'kind': 'result',
                                   'work_id': 'w-target', 'read': False, 'created': 1.0})
        # 1001 NEWER, higher-id unread notifications evict the target from the page.
        for i in range(1001):
            store.put('notification', {'id': 'n-%04d' % (i + 1), 'bot_id': 'bot-main', 'kind': 'peer_message',
                                       'work_id': 'w-x', 'read': False, 'created': 100.0 + i})
        with self.request('snapshot') as r:
            snap = json.load(r)
        by_id = {w['id']: w for w in snap['work']}
        self.assertIn('w-target', by_id) # terminal Work created first... still shipped?
        self.assertNotEqual(by_id['w-target']['attention'], 'settled')
        self.assertEqual(by_id['w-target']['attention'], 'unknown')

    def test_installation_config_full_form_runs_path_checks_outside_transaction(self):
        from pangpang.store import set_strict_txn_io
        from unittest.mock import patch
        self.login()
        sub = self.root / 'new-workspace'; sub.mkdir()
        self.app.execution.hosts['local']['allowed_roots'] = [str(sub)]
        body = {'max_concurrency': 3, 'timeout_s': 120, 'priority': 8,
                'cwd': str(sub), 'use_when': '配置窗口保存', 'enabled': True}
        set_strict_txn_io(True)
        try:
            with self.request('installations/codex-local/config', body, key='config-save') as response:
                result = json.load(response)
            self.assertEqual(result['max_concurrency'], 3)
            self.assertEqual(Path(result['cwd']), sub.resolve())
            with self.request('snapshot') as response:
                live = next(i for i in json.load(response)['installations'] if i['id'] == 'codex-local')
            self.assertEqual(live['timeout_s'], 120)
            saved = self.app.store.get('settings', 'managed')['config']['installations'][0]
            self.assertEqual(saved['max_concurrency'], 3)
            with patch.object(self.app, 'prepare_installation_config', side_effect=AssertionError('replay must not redo path I/O')):
                with self.request('installations/codex-local/config', body, key='config-save') as response:
                    self.assertEqual(json.load(response), result)
        finally:
            set_strict_txn_io(False)

    def test_artifact_listing_keyset_crosses_snapshot_cap(self):
        self.login()
        # The UI snapshot is intentionally bounded at 1000. The history endpoint
        # must still traverse canonical rows beyond that window without OFFSET.
        wanted = set()
        for i in range(1005):
            artifact_id = 'artifact-history-%04d' % i
            wanted.add(artifact_id)
            self.app.store.put('artifact', {
                'id': artifact_id, 'host_id': 'local',
                'path': str(self.root / ('history-%04d.txt' % i)),
                'size': i, 'created': float(i),
            })
        with self.request('snapshot') as response:
            snap = json.load(response)
        self.assertLessEqual(len(snap['artifacts']), 1000)

        seen = []
        cursor = None
        for _ in range(20):
            path = 'artifacts?limit=100'
            if cursor:
                path += '&cursor=' + cursor
            with self.request(path) as response:
                page = json.load(response)
            ids = [row['id'] for row in page['items']]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(set(ids).isdisjoint(seen))
            seen.extend(ids)
            cursor = page['next_cursor']
            if not cursor:
                break

        self.assertTrue(wanted.issubset(set(seen)))
        self.assertIsNone(cursor)

    def test_artifact_unicode_download_and_tampered_bytes(self):
        import base64
        import hashlib
        from unittest.mock import patch
        from urllib.parse import quote
        self.login()
        raw = b'<script>alert(1)</script>'
        name = '报告😀.html'
        artifact = {'id': 'artifact-unicode', 'host_id': 'local', 'path': str(self.root / name),
                    'digest': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
        self.app.store.put('artifact', artifact)
        with patch.object(self.app.execution, 'read_artifact', return_value={
                'data': base64.b64encode(raw).decode(), 'digest': artifact['digest']}):
            with self.request('artifacts/artifact-unicode?download=1') as response:
                self.assertEqual(response.read(), raw)
                self.assertEqual(response.headers['Content-Type'], 'application/octet-stream')
                self.assertEqual(response.headers['Content-Disposition'],
                                 'attachment; filename="download"; filename*=UTF-8\'\'' + quote(name, safe=''))
                self.assertEqual(response.headers['Content-Security-Policy'], "default-src 'none'; sandbox")
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        with patch.object(self.app.execution, 'read_artifact', return_value={
                'data': base64.b64encode(b'evil').decode(), 'digest': artifact['digest']}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('artifacts/artifact-unicode?download=1')
            self.assertEqual(error.exception.code, 409)

    def test_routine_snapshot_digest_is_required_and_stale_review_rejected(self):
        self.login()
        r = self.app.create_routine('bot-main', {'instruction': '日报', 'timezone': 'UTC',
            'schedule': {'type': 'interval', 'minutes': 5}})
        with self.request('snapshot') as response:
            reviewed = json.load(response)['routines'][0]
        self.app.update_bot('bot-main', {'instructions': '新长期指令', 'revision': self.app.bot('bot-main')['revision']})
        for idx, body in enumerate(({}, {'digest': reviewed['approval_digest']})):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('routines/' + r['id'] + '/approve', body, key='approve-' + str(idx))
            self.assertEqual(error.exception.code, 409)
        with self.request('routines/' + r['id']) as response:
            current = json.load(response)
        self.assertEqual(current['approval_state'], 'pending')
        self.assertEqual(current['owner_policy']['instructions'], '新长期指令')
        with self.request('routines/' + r['id'] + '/approve', {'digest': current['approval_digest']}, key='approve-fresh') as response:
            self.assertEqual(json.load(response)['approval_state'], 'approved')
    def test_http_auth_and_csrf(self):
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('snapshot')
        self.assertEqual(e.exception.code,401)
        with self.request('login',{'token':self.token}) as r:
            self.assertIn('HttpOnly',r.headers['Set-Cookie'])
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('bots',{'name':'bad','role':'x'},origin='https://evil.invalid')
        self.assertEqual(e.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('bots',{'name':'bad','role':'x'},origin=False)
        self.assertEqual(e.exception.code,403)
        with self.request('bots',{'name':'持久 Bot','role':'编辑'}) as r:bot=json.load(r)
        with self.request('bots',{'name':'持久 Bot','role':'编辑'}) as r:again=json.load(r)
        self.assertEqual(bot['id'],again['id'])
        with self.request('snapshot') as r:self.assertEqual(len(json.load(r)['bots']),2)
        with self.request('logout',{}) as r:self.assertEqual(r.status,200)
        with self.assertRaises(urllib.error.HTTPError):self.request('snapshot')
    def test_chat_work_id_is_demoted_to_hint_and_router_runs_before_commit(self):
        self.login()
        first = self.app.send('bot-main', {'text': '阿里股价'}, 'seed-work')
        seen = []
        def route(bot_id, body):
            seen.append(dict(body))
            return {'work_id': None, 'work_hint_id': body.get('work_hint_id'),
                    'route_source': 'jev', 'route_quality': 'confident',
                    'proposal': {'choice': 'NEW_TOPIC', 'confidence':.99},
                    'stream_id': body.get('stream_id'),
                    'last_seen_event_id': body.get('last_seen_event_id'),
                    'correction_target_message_id': None,
                    'correction_superseded_message_ids': []}
        self.app.resolve_continuation = route
        legacy = {'text': '冰箱怎么修', 'work_id': first['work_id'],
                  'stream_id': 'tab-http'}
        with self.request('bots/bot-main/messages', legacy, key='route-http') as r:
            result = json.load(r)
        self.assertNotEqual(result['work_id'], first['work_id'])
        self.assertEqual(len(seen), 1)
        self.assertNotIn('work_id', seen[0])
        self.assertEqual(seen[0]['work_hint_id'], first['work_id'])

        # Committed HTTP reality is replayed before any new semantic judgement.
        self.app.resolve_continuation = lambda *_: self.fail('router must not rerun on replay')
        with self.request('bots/bot-main/messages', legacy, key='route-http') as r:
            replay = json.load(r)
        self.assertEqual(replay, result)

    def test_http_model_failure_not_fake_success(self):
        from pangpang.runtime import Runtime
        self.login()
        with self.request('bots/bot-main/messages',{'text':'test'}) as r:message=json.load(r)
        Runtime(self.app).step()
        turn=self.app.store.get('turn',message['turn_id'])
        self.assertEqual(turn['status'],'blocked')
        self.assertEqual(turn['error'],'model_request_failed')
        self.assertFalse(any(m['kind']=='assistant' for m in self.app.store.find('message')))
    def test_long_chat_opens_on_newest_page(self):
        self.login()
        for i in range(205):
            self.app.store.put('message',{'id':f'msg-{i:03d}','bot_id':'bot-main','work_id':'history',
                'kind':'user','from':'user','content':str(i),'created':float(i)})
        with self.request('bots/bot-main/messages') as r:messages=json.load(r)
        self.assertEqual(len(messages),200)
        self.assertEqual(messages[0]['content'],'5')
        self.assertEqual(messages[-1]['content'],'204')
        with self.request('bots/bot-main/messages?offset=200&limit=200') as r:older=json.load(r)
        self.assertEqual([m['content'] for m in older],['0','1','2','3','4'])
    def test_web_config_projection_hides_credential_metadata(self):
        self.login()
        host=self.app.config['hosts'][0]
        host['credential_hash']='do-not-return-this-hash'
        host['credential_env']='DO_NOT_RETURN_THIS_ENV'
        with self.request('settings') as r:settings=json.load(r)
        self.assertNotIn('credential_hash',json.dumps(settings))
        self.assertNotIn('credential_env',json.dumps(settings))
        with self.request('snapshot') as r:snapshot=json.load(r)
        self.assertNotIn('credential_hash',json.dumps(snapshot['hosts']))
        self.assertNotIn('credential_env',json.dumps(snapshot['hosts']))


    def test_snapshot_omits_turn_history_and_keeps_old_live_turn(self):
        self.login()
        live = self.app.store.put("turn", {
            "id": "turn-live-old", "bot_id": "bot-main", "work_id": "work-live-old",
            "message_id": "msg-live-old", "message_ids": ["msg-live-old"],
            "status": "running", "history": [{"role": "user", "content": "LIVE-HISTORY"}],
            "created": 1.0, "rounds": 1, "priority": 2,
        })
        for i in range(220):
            self.app.store.put("turn", {
                "id": f"turn-done-{i:03d}", "bot_id": "bot-main",
                "work_id": f"work-done-{i:03d}", "message_id": f"msg-done-{i:03d}",
                "message_ids": [f"msg-done-{i:03d}"], "status": "done",
                "history": [{"role": "assistant", "content": "X" * 2000}],
                "created": float(i + 2), "rounds": 1, "priority": 2,
            })
        with self.request('snapshot') as r:
            snapshot = json.load(r)
        turns = snapshot["turns"]
        self.assertIn(live["id"], {t["id"] for t in turns})
        self.assertLessEqual(len(turns), 201)
        self.assertTrue(all("history" not in t for t in turns))
        self.assertNotIn("LIVE-HISTORY", json.dumps(turns))

    def test_artifact_download_forces_attachment_for_active_content(self):
        # R1: HTML (active content) is served as octet-stream attachment with a
        # locked sandbox CSP so it can never execute in the admin session; a PNG
        # (safe, non-active) is served inline for preview.
        import hashlib
        self.login()
        # workspace must not overlap the control-plane data dir (R3 guard); use a
        # sibling dir and point the local host's allowed_roots at it.
        ws = self.root / "ws"; ws.mkdir()
        self.app.execution.hosts["local"]["allowed_roots"] = [str(ws)]
        html = b"<html><script>fetch('/api/v1/snapshot')</script></html>"
        (ws / "evil.html").write_bytes(html)
        self.app.store.put("artifact", {"id": "art-html", "host_id": "local",
            "path": str(ws / "evil.html"), "digest": hashlib.sha256(html).hexdigest(),
            "owner_bot_id": "bot-main", "work_id": None, "size": len(html)})
        with self.request('artifacts/art-html?download=1') as r:
            self.assertEqual(r.headers['Content-Type'], 'application/octet-stream')
            self.assertIn('attachment', r.headers['Content-Disposition'])
            self.assertEqual(r.headers['Content-Security-Policy'], "default-src 'none'; sandbox")
            self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        (ws / "pic.png").write_bytes(png)
        self.app.store.put("artifact", {"id": "art-png", "host_id": "local",
            "path": str(ws / "pic.png"), "digest": hashlib.sha256(png).hexdigest(),
            "owner_bot_id": "bot-main", "work_id": None, "size": len(png)})
        with self.request('artifacts/art-png?download=1') as r:
            self.assertEqual(r.headers['Content-Type'], 'image/png')
            self.assertIsNone(r.headers.get('Content-Disposition')) # inline preview
            self.assertEqual(r.headers['Content-Security-Policy'], "default-src 'none'; sandbox")

if __name__=='__main__':unittest.main()
