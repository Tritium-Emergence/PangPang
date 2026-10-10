import json
import sys
import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.runtime import Runtime
from pangpang.store import Fault, set_strict_txn_io
from pangpang.context import model_work_view, model_dependency_view, projection


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    # --- allowlist / SIS ---------------------------------------------------
    def test_model_work_view_excludes_internal_fields(self):
        m = self.app.send('bot-main', {'text': '干活'}, 'wv')
        work = self.app.store.get('work', m['work_id'])
        # inject internal fields that must NOT leak
        work['pending_dep_ids'] = ['dep-x']
        work['grant_write'] = True
        work['cancel_requested'] = False
        work['root_request_id'] = 'root-secret'
        view = model_work_view(work)
        for banned in ('pending_dep_ids', 'grant_write', 'cancel_requested', 'root_request_id',
                       'depth', 'parent_work_id', 'handoff', 'mode', 'reply_to'):
            self.assertNotIn(banned, view, banned)
        for kept in ('id', 'status', 'goal'):
            self.assertIn(kept, view)

    def test_projection_does_not_leak_pending_dep_ids(self):
        editor = self.app.create_bot({'name': '编辑', 'role': '编辑'})
        src = self.app.send('bot-main', {'text': '编辑正文'}, 'proj')
        h = self.app.delegate('bot-main', src['work_id'],
                              {'target_bot_id': editor['id'], 'goal': '编辑', 'request': '改', 'acceptance': ['ok']})
        # parent now waiting with a pending child dependency
        parent = self.app.store.get('work', src['work_id'])
        self.assertEqual(parent['status'], 'waiting')
        self.assertEqual(len(parent['pending_dep_ids']), 1)
        turn = self.app.store.get('turn', h['turn_id'])
        payload = json.dumps(projection(self.app, turn), ensure_ascii=False)
        self.assertNotIn('pending_dep_ids', payload)
        self.assertNotIn('grant_write', payload)

    # --- settle CAS / idempotency -----------------------------------------
    def test_settle_is_single_shot(self):
        editor = self.app.create_bot({'name': '编辑', 'role': '编辑'})
        src = self.app.send('bot-main', {'text': '活'}, 's1')
        self.app.delegate('bot-main', src['work_id'],
                          {'target_bot_id': editor['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        dep = self.app.store.find('dependency', source_work_id=src['work_id'])[0]
        self.app.settle_dependency(dep['id'], 'success', summary='done')
        settled = self.app.store.get('dependency', dep['id'])
        self.assertEqual(settled['status'], 'settled')
        self.assertEqual(settled['outcome'], 'success')
        # second settle is rejected (no resurrection).
        again = self.app.settle_dependency(dep['id'], 'failed', summary='late')
        self.assertEqual(again['outcome'], 'success') # unchanged
        parent = self.app.store.get('work', src['work_id'])
        self.assertEqual(parent['pending_dep_ids'], [])

    def test_child_failure_settles_parent_immediately(self):
        # A delegated child whose turn blocks must fail the parent dependency
        # right away, not wait for any deadline.
        editor = self.app.create_bot({'name': '编辑', 'role': '编辑'})
        src = self.app.send('bot-main', {'text': '活'}, 'cf')
        h = self.app.delegate('bot-main', src['work_id'],
                              {'target_bot_id': editor['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})

        class Dead:
            def complete(self, *a):
                raise Fault('model_request_failed')
        self.app.config['turn_retry_delays_s'] = []
        rt = Runtime(self.app, Dead())
        # step() picks lowest-priority queued turn first (the parent's own user
        # turn, then the child handoff turn). Drive both; only the child turn
        # (delegated) settles the parent dependency.
        for _ in range(5):
            rt.step()
        child = self.app.store.get('work', h['work_id'])
        self.assertEqual(child['status'], 'failed')
        dep = self.app.store.find('dependency', source_work_id=src['work_id'])[0]
        self.assertEqual(dep['status'], 'settled')
        self.assertEqual(dep['outcome'], 'failed')
        parent = self.app.store.get('work', src['work_id'])
        self.assertEqual(parent['pending_dep_ids'], [])
        # parent got a 'result' continuation turn
        self.assertTrue(any(t.get('work_id') == src['work_id'] and t.get('id') != h['turn_id']
                            for t in self.app.store.find('turn')))

    def test_top_level_block_settles_nothing(self):
        # A top-level conversation turn that blocks owns no parent dependency.
        m = self.app.send('bot-main', {'text': '活'}, 'top')

        class Dead:
            def complete(self, *a):
                raise Fault('model_request_failed')
        cfg = config(self.root)
        cfg['turn_retry_delays_s'] = []
        self.app.config['turn_retry_delays_s'] = []
        Runtime(self.app, Dead()).step()
        turn = self.app.store.get('turn', m['turn_id'])
        self.assertEqual(turn['status'], 'blocked')
        self.assertEqual(self.app.store.find('dependency'), [])

    # --- immediate peer failure -----------------------------
    def test_peer_turn_failure_settles_source_immediately(self):
        # when a target peer's reply turn reaches an unrecoverable
        # terminal state, its Exchange -> failed and the source Dependency -> failed
        # immediately (the source recovers on the normal scheduler cadence, NOT after
        # the peer deadline).
        peer = self.app.create_bot({'name': '同事', 'role': '协作'})
        a = self.app.send('bot-main', {'text': '主'}, 'pf-a')
        peer_work = self.app.send(peer['id'], {'text': '副'}, 'pf-b')
        self.app.send_bot_message('bot-main', a['work_id'],
            {'target_bot_id': peer['id'], 'target_work_id': peer_work['work_id'],
             'text': '帮个忙', 'requires_response': True})
        dep = self.app.store.find('dependency', source_work_id=a['work_id'])[0]
        self.assertEqual(dep['kind'], 'exchange')
        # find the target's queued peer-reply turn and drive it to a terminal fail
        peer_turn = next(t for t in self.app.store.find('turn', work_id=peer_work['work_id'])
                         if t.get('reply_exchange_id') == dep['target_id']
                         or dep['target_id'] in (t.get('reply_exchange_ids') or []))
        peer_turn['status'] = 'blocked'
        self.app.store.put('turn', peer_turn)
        self.app.fail_turn_dependency(peer_turn)
        # exchange + dependency both failed, no deadline wait
        self.assertEqual(self.app.store.get('exchange', dep['target_id'])['status'], 'failed')
        settled = self.app.store.get('dependency', dep['id'])
        self.assertEqual(settled['status'], 'settled')
        self.assertEqual(settled['outcome'], 'failed')
        self.assertEqual(self.app.store.get('work', a['work_id'])['pending_dep_ids'], [])

    # --- deadline sweeper --------------------------------------------------
    def test_requires_response_peer_to_delivered_target_rejected(self):
        # a delivered target Work is terminal — ensure_work_turn won't run a
        # turn for it, so a requires-response exchange would just wait out its 900s
        # deadline. It must be rejected up front (no phantom 900s wait, no silent
        # resurrection of a delivered Work by a peer).
        peer = self.app.create_bot({'name': '同事', 'role': '协作'})
        a = self.app.send('bot-main', {'text': '主'}, 'pd-a')
        peer_work = self.app.send(peer['id'], {'text': '副'}, 'pd-b')
        # drive the target work to delivered
        pt = self.app.store.get('turn', peer_work['turn_id'])
        self.app.finish_turn(pt, '副已答复')
        tw = self.app.store.get('work', peer_work['work_id']); tw['status'] = 'delivered'
        self.app.store.put('work', tw)
        with self.assertRaises(Fault) as cm:
            self.app.send_bot_message('bot-main', a['work_id'],
                {'target_bot_id': peer['id'], 'target_work_id': peer_work['work_id'],
                 'text': '再帮个忙', 'requires_response': True})
        # A delivered Work was completed, not cancelled. Reporting "cancelled" made the
        # Bot tell the user their finished work had been thrown away.
        self.assertEqual(cm.exception.code, 'target_work_delivered')
        self.assertIn('delegate', cm.exception.advice or '')

    def test_fyi_to_delivered_target_is_accepted_but_never_acts(self):
        """An FYI is fire-and-forget: it only
        appends to the inbox and rides along whenever some wake-worthy message next
        schedules a turn. For a DELIVERED target no turn is ever scheduled, so the
        instruction is parked, the caller still gets a success result, and link_work can
        later reopen the Work and surface it at an arbitrary time. This test pins the
        behaviour, NOT an endorsement of it: the hazard is known and
        changing it is a deliberate design decision, not a test change."""
        peer = self.app.create_bot({'name': '交付方', 'role': 'r'})
        a = self.app.send('bot-main', {'text': '主'}, 'fyi-a')
        peer_work = self.app.send(peer['id'], {'text': '副'}, 'fyi-b')
        pt = self.app.store.get('turn', peer_work['turn_id'])
        self.app.finish_turn(pt, '副已答复')
        tw = self.app.store.get('work', peer_work['work_id'])
        tw['status'] = 'delivered'
        self.app.store.put('work', tw)

        out = self.app.send_bot_message('bot-main', a['work_id'],
            {'target_bot_id': peer['id'], 'target_work_id': peer_work['work_id'],
             'text': '再加一节风险', 'requires_response': False})
        self.assertTrue(out) # the caller is told it worked
        self.assertIsNone(out.get('turn_id'))
        self.assertIsNone(self.app.ensure_work_turn(peer_work['work_id'])) #...and nothing runs
        self.assertEqual(self.app.store.get('work', peer_work['work_id'])['status'], 'delivered')

    def test_fyi_to_running_target_still_accepted(self):
        """The fix must not close the legitimate path: an FYI to a Work that is still
        running is still accepted and still rides along rather than waking it."""
        peer = self.app.create_bot({'name': '在跑', 'role': 'r'})
        a = self.app.send('bot-main', {'text': '主'}, 'live-a')
        peer_work = self.app.send(peer['id'], {'text': '副'}, 'live-b')
        out = self.app.send_bot_message('bot-main', a['work_id'],
            {'target_bot_id': peer['id'], 'target_work_id': peer_work['work_id'],
             'text': '顺便一提', 'requires_response': False})
        self.assertTrue(out)
        self.assertEqual([x['status'] for x in self.app._all(
            'exchange', target_work_id=peer_work['work_id'])], ['delivered'])

    def test_exchange_deadline_sweeps_to_timeout(self):
        import time
        peer = self.app.create_bot({'name': '同事', 'role': '协作'})
        a = self.app.send('bot-main', {'text': '主'}, 'ex-a')
        peer_work = self.app.send(peer['id'], {'text': '副'}, 'ex-b')
        self.app.send_bot_message('bot-main', a['work_id'],
            {'target_bot_id': peer['id'], 'target_work_id': peer_work['work_id'],
             'text': '帮个忙', 'requires_response': True})
        dep = self.app.store.find('dependency', source_work_id=a['work_id'])[0]
        self.assertEqual(dep['kind'], 'exchange')
        # force deadline into the past
        dep['deadline'] = time.time() - 1
        self.app.store.put('dependency', dep)
        self.app.sweep_deadlines()
        settled = self.app.store.get('dependency', dep['id'])
        self.assertEqual(settled['outcome'], 'timeout')
        ex = self.app.store.get('exchange', dep['target_id'])
        self.assertEqual(ex['status'], 'timeout')
        src = self.app.store.get('work', a['work_id'])
        self.assertEqual(src['pending_dep_ids'], [])

    # --- delegator updates -------------------------------------------------
    def _waiting_delegated_child(self, key):
        self.app.probe('codex-local')
        worker = self.app.create_bot({'name': '执行', 'role': '执行'})
        parent = self.app.send('bot-main', {'text': '交给执行'}, key + '-parent')
        handoff = self.app.delegate('bot-main', parent['work_id'],
            {'target_bot_id': worker['id'], 'goal': '处理', 'request': '先做第一步',
             'acceptance': ['完成']})
        # Model the child after its initial handoff turn has run and yielded on a
        # task dependency: no live turn, Work is waiting on its own executor task.
        parent_turn = self.app.store.get('turn', parent['turn_id'])
        parent_turn['status'] = 'done'
        self.app.store.put('turn', parent_turn)
        child_turn = self.app.store.get('turn', handoff['turn_id'])
        child_turn['status'] = 'done'
        self.app.store.put('turn', child_turn)
        self.app.submit_task(worker['id'], handoff['work_id'], {'prompt': '第一步'})
        child = self.app.store.get('work', handoff['work_id'])
        self.assertEqual(child['status'], 'waiting')
        self.assertTrue(child['pending_dep_ids'])
        return worker, parent, handoff

    def test_direct_delegator_update_wakes_waiting_child(self):
        worker, parent, handoff = self._waiting_delegated_child('du')
        sent = self.app.send_bot_message('bot-main', parent['work_id'],
            {'target_bot_id': worker['id'], 'target_work_id': handoff['work_id'],
             'text': '追加：同时检查格式，并告诉我你怎么处理', 'requires_response': True})

        self.assertIn('turn_id', sent)
        turn = self.app.store.get('turn', sent['turn_id'])
        self.assertEqual(turn['mode'], 'work_exchange')
        self.assertEqual(turn['priority'], 1)
        inbound = self.app.store.get('message', turn['message_id'])
        self.assertEqual(inbound.get('authority'), 'delegator')
        self.assertEqual(inbound.get('wake_intent'), 'delegator_update')
        # It crosses the child's pending join, but only through the dedicated
        # server-stamped mode.
        self.assertTrue(Runtime(self.app).scheduler._is_runnable(turn, 10**20))
        projected = projection(self.app, turn)
        # projection() serializes each consumed frame into the model message content.
        # Parse that actual model input instead of grepping a twice-serialized string
        # (store.encode uses compact JSON, so spacing/escaping is intentionally unstable
        # under an outer json.dumps).
        projected_frames = [json.loads(m['content']) for m in projected if m['role'] == 'user']
        delegator_frame = next(frame for frame in projected_frames
                               if frame.get('content') == '追加：同时检查格式，并告诉我你怎么处理')
        self.assertEqual(delegator_frame.get('authority'), 'delegator')

        # Child can answer this append before its original executor task finishes.
        turn['status'] = 'running'
        self.app.store.put('turn', turn)
        self.app.finish_turn(turn, '收到。原任务继续，我会额外检查格式。')
        child = self.app.store.get('work', handoff['work_id'])
        self.assertEqual(child['status'], 'waiting') # original task still pending

        parent_turns = [t for t in self.app.store.find('turn', work_id=parent['work_id'])
                        if t['status'] == 'queued']
        self.assertEqual(len(parent_turns), 1)
        reply_turn = parent_turns[0]
        self.assertEqual(reply_turn['mode'], 'work_exchange')
        self.assertEqual(reply_turn['priority'], 1)
        self.assertTrue(Runtime(self.app).scheduler._is_runnable(reply_turn, 10**20))
        reply = self.app.store.get('message', reply_turn['message_id'])
        self.assertEqual(reply.get('wake_intent'), 'delegator_reply')
        self.assertEqual(reply.get('kind'), 'peer_result')
        # Parent is still waiting on the original child_work dependency; the reply
        # crossed that join only because it belongs to this verified exchange.
        parent_work = self.app.store.get('work', parent['work_id'])
        self.assertEqual(parent_work['status'], 'waiting')
        self.assertTrue(any(self.app.store.get('dependency', d)['kind'] == 'child_work'
                            for d in parent_work['pending_dep_ids']))

    def test_user_p0_beats_delegator_work_exchange(self):
        worker, parent, handoff = self._waiting_delegated_child('du-vs-user')
        sent = self.app.send_bot_message('bot-main', parent['work_id'],
            {'target_bot_id': worker['id'], 'target_work_id': handoff['work_id'],
             'text': '追加要求', 'requires_response': True})
        exchange_turn = self.app.store.get('turn', sent['turn_id'])
        self.assertEqual(exchange_turn['mode'], 'work_exchange')
        self.assertEqual(exchange_turn['priority'], 1)

        direct = self.app.send(worker['id'],
            {'text': '用户本人现在要求先处理这个', 'work_id': handoff['work_id']},
            'du-vs-user-direct')
        merged = self.app.store.get('turn', direct['turn_id'])
        self.assertEqual(merged['id'], exchange_turn['id'])
        self.assertEqual(merged['mode'], 'interrupt')
        self.assertEqual(merged['priority'], 0)
        self.assertTrue(Runtime(self.app).scheduler._is_runnable(merged, 10**20))

    def test_unrelated_peer_cannot_interrupt_waiting_child(self):
        worker, parent, handoff = self._waiting_delegated_child('peer-no-interrupt')
        peer = self.app.create_bot({'name': '旁观者', 'role': '协作'})
        peer_work = self.app.send(peer['id'], {'text': '另一个工作'}, 'peer-source')
        sent = self.app.send_bot_message(peer['id'], peer_work['work_id'],
            {'target_bot_id': worker['id'], 'target_work_id': handoff['work_id'],
             'text': '请现在回答我', 'requires_response': True})

        self.assertNotIn('turn_id', sent)
        messages = [m for m in self.app.store.find('message', work_id=handoff['work_id'])
                    if m.get('exchange_id') == sent['exchange_id']]
        self.assertEqual(len(messages), 1)
        self.assertIsNone(messages[0].get('authority'))
        self.assertEqual(messages[0].get('wake_intent'), 'dependency_result')
        self.assertEqual([t for t in self.app.store.find('turn', work_id=handoff['work_id'])
                          if t['status'] == 'queued'], [])

    def test_delegator_fyi_is_authoritative_but_non_waking(self):
        worker, parent, handoff = self._waiting_delegated_child('du-fyi')
        sent = self.app.send_bot_message('bot-main', parent['work_id'],
            {'target_bot_id': worker['id'], 'target_work_id': handoff['work_id'],
             'text': 'FYI：客户名称刚改了', 'requires_response': False})

        self.assertNotIn('turn_id', sent)
        messages = [m for m in self.app.store.find('message', work_id=handoff['work_id'])
                    if m.get('exchange_id') == sent['exchange_id']]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].get('authority'), 'delegator')
        self.assertEqual(messages[0].get('wake_intent'), 'fyi')
        self.assertEqual([t for t in self.app.store.find('turn', work_id=handoff['work_id'])
                          if t['status'] == 'queued'], [])

    # --- task unknown settles dependency ----------------------------------
    def test_task_unknown_keeps_dependency_pending(self):
        self.app.probe('codex-local')
        m = self.app.send('bot-main', {'text': '活'}, 'tk')
        res = self.app.submit_task('bot-main', m['work_id'], {'prompt': 'x'})
        task = self.app.store.get('task', res['task_id'])
        self.assertIn('dependency_id', task)
        task['status'] = 'running'
        self.app.store.put('task', task)
        self.app.recover() # missing receipt quarantines, but does not settle
        dep = self.app.store.get('dependency', task['dependency_id'])
        self.assertEqual(dep['status'], 'pending')
        self.assertIsNone(dep.get('outcome'))

    # --- backfill ----------------------------------------------------------
    def test_backfill_is_idempotent_and_reconstructs(self):
        editor = self.app.create_bot({'name': '编辑', 'role': '编辑'})
        src = self.app.send('bot-main', {'text': '活'}, 'bf')
        self.app.delegate('bot-main', src['work_id'],
                          {'target_bot_id': editor['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        # simulate a pre-Phase-1 work: strip the field
        work = self.app.store.get('work', src['work_id'])
        del work['pending_dep_ids']
        self.app.store.put('work', work)
        before = len(self.app.store.find('dependency'))
        self.app._backfill_dependencies()
        work = self.app.store.get('work', src['work_id'])
        self.assertIn('pending_dep_ids', work)
        self.assertEqual(len(work['pending_dep_ids']), 1)
        # idempotent: running again adds nothing
        self.app._backfill_dependencies()
        work2 = self.app.store.get('work', src['work_id'])
        self.assertEqual(len(work2['pending_dep_ids']), 1)


    def test_multiple_deps_one_settle_keeps_work_waiting(self):
        # A work with two pending deps must stay waiting until BOTH settle
        # (codex P1: a single settle must not prematurely finalize).
        self.app.probe('codex-local')
        editor = self.app.create_bot({'name': '编辑', 'role': '编辑'})
        m = self.app.send('bot-main', {'text': '活', 'allow_write': True}, 'multi')
        # dep 1: delegated child
        self.app.delegate('bot-main', m['work_id'],
                          {'target_bot_id': editor['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        # dep 2: task on the same parent work
        self.app.submit_task('bot-main', m['work_id'], {'prompt': 'x'})
        work = self.app.store.get('work', m['work_id'])
        self.assertEqual(len(work['pending_dep_ids']), 2)
        self.assertEqual(work['status'], 'waiting')
        deps = self.app.store.find('dependency', source_work_id=m['work_id'])
        # settle one -> still waiting, one pending remains
        self.app.settle_dependency(deps[0]['id'], 'success', summary='one')
        work = self.app.store.get('work', m['work_id'])
        self.assertEqual(len(work['pending_dep_ids']), 1)
        # settle the second -> no pending left
        self.app.settle_dependency(deps[1]['id'], 'success', summary='two')
        work = self.app.store.get('work', m['work_id'])
        self.assertEqual(work['pending_dep_ids'], [])

    def test_deliver_to_user_child_with_other_pending_dep_not_delivered(self):
        # A deliver_to_user child success must NOT mark the parent delivered while
        # another dependency is still pending (codex P1 terminal-guard bug).
        self.app.probe('codex-local')
        worker = self.app.create_bot({'name': '执行', 'role': '执行'})
        m = self.app.send('bot-main', {'text': '活', 'allow_write': True}, 'd2u')
        self.app.delegate('bot-main', m['work_id'],
                          {'target_bot_id': worker['id'], 'goal': 'g', 'request': 'r',
                           'acceptance': ['a'], 'mode': 'deliver_to_user'})
        self.app.submit_task('bot-main', m['work_id'], {'prompt': 'x'})
        deps = self.app.store.find('dependency', source_work_id=m['work_id'])
        child_dep = next(d for d in deps if d['kind'] == 'child_work')
        self.app.settle_dependency(child_dep['id'], 'success', summary='delivered to user')
        work = self.app.store.get('work', m['work_id'])
        # still one pending (the task) -> must remain waiting, NOT delivered
        self.assertEqual(work['status'], 'waiting')
        self.assertEqual(len(work['pending_dep_ids']), 1)


if __name__ == '__main__':
    unittest.main()
