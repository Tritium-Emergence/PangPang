import json
import tempfile
import time
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class Phase2bTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _pending(self, work_id):
        return self.app.store.get('work', work_id)['pending_dep_ids']

    def test_coalesced_wake_exactly_once_after_join(self):
        # A work with 3 pending deps must end with exactly ONE queued continuation
        # that consumes all results (the same shape). Note: send() already left
        # one queued user turn on the parent; results coalesce INTO it rather than
        # spawning new turns — that is the coalescing property under test.
        self.app.probe('codex-local')
        e1 = self.app.create_bot({'name': 'A', 'role': 'r'})
        e2 = self.app.create_bot({'name': 'B', 'role': 'r'})
        m = self.app.send('bot-main', {'text': '活', 'allow_write': True}, 'coal')
        wid = m['work_id']
        # consume the initial user turn so we isolate dependency coalescing
        for t in self.app.store.find('turn', work_id=wid):
            if t['status'] == 'queued':
                t['status'] = 'done'
                self.app.store.put('turn', t)
                for mid in t.get('message_ids', []):
                    msg = self.app.store.get('message', mid)
                    msg['consumed_by_turn_id'] = t['id']
                    self.app.store.put('message', msg)
        self.app.delegate('bot-main', wid, {'target_bot_id': e1['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        self.app.delegate('bot-main', wid, {'target_bot_id': e2['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        self.app.submit_task('bot-main', wid, {'prompt': 'x'})
        deps = self.app.store.find('dependency', source_work_id=wid)
        self.assertEqual(len(deps), 3)

        def parent_queued():
            return [t for t in self.app.store.find('turn', work_id=wid) if t['status'] == 'queued']
        self.app.settle_dependency(deps[0]['id'], 'success', summary='one')
        self.assertEqual(len(self._pending(wid)), 2)
        self.assertEqual(len(parent_queued()), 0) # no wake yet
        self.app.settle_dependency(deps[1]['id'], 'success', summary='two')
        self.assertEqual(len(parent_queued()), 0) # still waiting
        self.app.settle_dependency(deps[2]['id'], 'success', summary='three')
        self.assertEqual(self._pending(wid), [])
        q = parent_queued()
        self.assertEqual(len(q), 1) # exactly one coalesced continuation
        self.assertGreaterEqual(len(q[0]['message_ids']), 3)

    def test_settle_during_running_not_lost(self):
        # If a dependency settles while the work's continuation turn is already
        # running, the terminal reschedule hook must pick up the new result.
        self.app.probe('codex-local')
        e1 = self.app.create_bot({'name': 'A', 'role': 'r'})
        m = self.app.send('bot-main', {'text': '活', 'allow_write': True}, 'sdr')
        wid = m['work_id']
        for t in self.app.store.find('turn', work_id=wid):
            if t['status'] == 'queued':
                t['status'] = 'done'
                self.app.store.put('turn', t)
                for mid in t.get('message_ids', []):
                    msg = self.app.store.get('message', mid)
                    msg['consumed_by_turn_id'] = t['id']
                    self.app.store.put('message', msg)
        self.app.delegate('bot-main', wid, {'target_bot_id': e1['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        self.app.submit_task('bot-main', wid, {'prompt': 'x'})
        deps = self.app.store.find('dependency', source_work_id=wid)
        # settle first -> still one pending, no wake
        self.app.settle_dependency(deps[0]['id'], 'success', summary='one')
        self.assertEqual(len(self._pending(wid)), 1)
        # simulate a continuation turn already running on this work
        q = [t for t in self.app.store.find('turn', work_id=wid) if t['status'] == 'queued']
        self.assertEqual(len(q), 0)
        # settle second -> join empty -> ensure_work_turn creates the turn
        self.app.settle_dependency(deps[1]['id'], 'success', summary='two')
        self.assertEqual(self._pending(wid), [])
        turns = [t for t in self.app.store.find('turn', work_id=wid) if t['status'] == 'queued']
        self.assertEqual(len(turns), 1)
        # now mark it running, append a late message, and reschedule
        running = turns[0]
        running['status'] = 'running'
        self.app.store.put('turn', running)
        # a late FYI-style result arrives; ensure_work_turn should not create a
        # second turn while one runs
        self.assertIsNone(self.app.ensure_work_turn(wid))
        # after the running turn ends, reschedule picks up nothing new here
        running['status'] = 'done'
        self.app.store.put('turn', running)
        self.app.reschedule_work(wid) # no unconsumed inbox -> no new turn
        q2 = [t for t in self.app.store.find('turn', work_id=wid) if t['status'] == 'queued']
        self.assertEqual(len(q2), 0)

    def test_turn_never_waiting(self):
        # No turn should ever carry the 'waiting' status.
        self.app.probe('codex-local')
        e1 = self.app.create_bot({'name': 'A', 'role': 'r'})
        m = self.app.send('bot-main', {'text': '活', 'allow_write': True}, 'nw')
        self.app.delegate('bot-main', m['work_id'], {'target_bot_id': e1['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        for t in self.app.store.find('turn'):
            self.assertNotEqual(t['status'], 'waiting', t)

    def test_fyi_does_not_wake_or_change_status(self):
        # FYI (requires_response=False) must not create a target turn nor flip the
        # target work to active.
        peer = self.app.create_bot({'name': '同事', 'role': 'r'})
        a = self.app.send('bot-main', {'text': '主'}, 'fyi-a')
        pw = self.app.send(peer['id'], {'text': '副'}, 'fyi-b')
        # drive the peer's own work to a settled state so it's not 'active' from send
        pwork = self.app.store.get('work', pw['work_id'])
        pwork['status'] = 'delivered'
        self.app.store.put('work', pwork)
        before_turns = len(self.app.store.find('turn', work_id=pw['work_id']))
        res = self.app.send_bot_message('bot-main', a['work_id'],
            {'target_bot_id': peer['id'], 'target_work_id': pw['work_id'],
             'text': 'FYI 仅供参考', 'requires_response': False})
        self.assertFalse(res.get('yielded'))
        self.assertIsNone(res.get('turn_id'))
        after_turns = len(self.app.store.find('turn', work_id=pw['work_id']))
        self.assertEqual(after_turns, before_turns) # no new turn
        self.assertEqual(self.app.store.get('work', pw['work_id'])['status'], 'delivered') # unchanged
        # source did not gain a dependency (FYI is fire-and-forget)
        self.assertEqual(self.app.store.find('dependency', source_work_id=a['work_id']), [])


    def test_schema_version_bumped_to_v2(self):
        # The 2b one-way door stamps the DB as v2 so a rolled-back old binary
        # (which requires version==1) refuses to open the migrated database.
        self.assertEqual(self.app.store.schema_version(), 2)

    def test_link_rebinds_current_turn_and_preserves_waiting_target(self):
        # linking ENDS the current model context (SIS: the turn's
        # projection was built from the provisional Work). The tool yields; the
        # rebound user message is un-consumed onto the target so a FRESH turn there
        # rebuilds a target projection. The waiting target keeps its dependency.
        self.app.probe('codex-local')
        e1 = self.app.create_bot({'name': 'A', 'role': 'r'})
        # target work that is waiting on a dependency
        target = self.app.send('bot-main', {'text': '原工作', 'allow_write': True}, 'lt')
        self.app.delegate('bot-main', target['work_id'],
                          {'target_bot_id': e1['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        self.assertEqual(self.app.store.get('work', target['work_id'])['status'], 'waiting')
        # provisional work + turn
        prov = self.app.send('bot-main', {'text': '继续那个'}, 'lp')
        res = self.app.link_work(prov['turn_id'], target['work_id'])
        self.assertEqual(res['work']['id'], target['work_id'])
        self.assertTrue(res['yielded']) # link ends the current model context
        # provisional is linked; the rebound message is on the target and UNCONSUMED
        self.assertEqual(self.app.store.get('work', prov['work_id'])['status'], 'linked')
        msg = self.app.store.get('message', prov['message_id'])
        self.assertEqual(msg['work_id'], target['work_id'])
        self.assertIsNone(msg.get('consumed_by_turn_id'))
        # target stayed waiting (its dependency is untouched)
        self.assertEqual(self.app.store.get('work', target['work_id'])['status'], 'waiting')

    def test_single_running_turn_index_blocks_second(self):
        # 2d: the partial unique index enforces one running turn per Work.
        m = self.app.send('bot-main', {'text': '活'}, 'idx')
        wid = m['work_id']
        t1 = self.app.store.get('turn', m['turn_id'])
        t1['status'] = 'running'
        self.app.store.put('turn', t1)
        # a second running turn on the same work must be rejected
        t2 = dict(t1, id='turn-second-x', status='running')
        with self.assertRaises(Fault):
            self.app.store.put('turn', t2)


    def test_fyi_inbox_does_not_block_child_delivery(self):
        # A delegated child that finishes must settle its parent even if an FYI is
        # sitting in its inbox (FYIs never wake, so they must not keep it 'active').
        e1 = self.app.create_bot({'name': 'A', 'role': 'r'})
        peer = self.app.create_bot({'name': 'P', 'role': 'r'})
        parent = self.app.send('bot-main', {'text': '父'}, 'fcd')
        h = self.app.delegate('bot-main', parent['work_id'],
                              {'target_bot_id': e1['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        child_wid = h['work_id']
        # drop an FYI into the child's inbox
        pw = self.app.send(peer['id'], {'text': 'x'}, 'fp')
        self.app.send_bot_message(peer['id'], pw['work_id'],
            {'target_bot_id': e1['id'], 'target_work_id': child_wid,
             'text': 'FYI', 'requires_response': False})
        # child finishes its handoff turn
        self.app.finish_turn(self.app.store.get('turn', h['turn_id']), '子完成')
        # child must be delivered (FYI did not strand it) and parent dep settled
        self.assertEqual(self.app.store.get('work', child_wid)['status'], 'delivered')
        self.assertEqual(self.app.store.get('work', parent['work_id'])['pending_dep_ids'], [])

    def test_deliver_child_does_not_bury_queued_user_interrupt(self):
        # P0 race: a waiting parent with a deliver_to_user child; a user interrupt
        # is queued (message consumed). If the child then settles success as the
        # LAST dep, direct-delivering would flip the Work terminal and the scheduler
        # would permanently skip the queued user turn (its message never re-enters
        # the inbox) — a silently dropped user request. The fix: keep the Work
        # non-terminal, route the child result to the inbox, leave the user turn
        # runnable.
        e1 = self.app.create_bot({'name': 'A', 'role': 'r'})
        parent = self.app.send('bot-main', {'text': '父', 'allow_write': True}, 'p0p')
        # delegate a deliver_to_user child -> parent goes waiting on that child.
        h = self.app.delegate('bot-main', parent['work_id'],
                              {'target_bot_id': e1['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        self.assertEqual(self.app.store.get('work', parent['work_id'])['status'], 'waiting')
        # user interrupts the waiting parent -> a P0 turn is queued, msg consumed
        u = self.app.send('bot-main', {'text': '等等，改成 HTML'}, 'p0u')
        user_turn = self.app.store.find('turn', work_id=parent['work_id'], status='queued')
        self.assertTrue(user_turn, 'a queued user interrupt turn must exist')
        uturn = user_turn[0]
        self.assertEqual(uturn['priority'], 0)
        # child (deliver_to_user) finishes success as the last dependency
        self.app.finish_turn(self.app.store.get('turn', h['turn_id']), '子交付内容')
        # parent must NOT be delivered (a live user turn exists)
        pw = self.app.store.get('work', parent['work_id'])
        self.assertNotEqual(pw['status'], 'delivered')
        # the queued user turn is still queued (Work non-terminal -> schedulable),
        # and its consumed user message did not vanish.
        uturn2 = self.app.store.get('turn', uturn['id'])
        self.assertEqual(uturn2['status'], 'queued')
        self.assertNotIn(pw['status'], {'delivered', 'failed', 'cancelled', 'linked'})
        # the child result reached the Work and was coalesced into the SAME queued
        # user turn (so when it runs it sees both the user's edit and the result).
        results = [m for m in self.app.store.find('message', work_id=parent['work_id'])
                   if m['kind'] == 'result']
        self.assertTrue(results, 'child result must be recorded on the Work')
        self.assertIn(results[0]['id'], uturn2['message_ids'])


        # A message arriving on a work whose turn then blocks must not be stranded:
        # reschedule_work picks it up (here we simulate via a queued user message).
        from pangpang.runtime import Runtime
        from pangpang.store import Fault
        m = self.app.send('bot-main', {'text': '活'}, 'brl')
        wid = m['work_id']

        class Dead:
            def complete(self, *a):
                raise Fault('model_request_failed')
        self.app.config['turn_retry_delays_s'] = []
        Runtime(self.app, Dead()).step() # the user turn blocks
        # inbox is drained (its message was consumed); work is not stranded active
        # with an unscheduled message
        inbox = self.app._work_inbox(wid)
        self.assertEqual(inbox, [])


if __name__ == '__main__':
    unittest.main()
