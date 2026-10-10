import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Service(config(self.root), self.root / 'data')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_cancel_work_marks_cancelled_and_closes_admission(self):
        m = self.app.send('bot-main', {'text': '活'}, 'cw')
        wid = m['work_id']
        self.app.cancel_work(wid)
        self.assertEqual(self.app.store.get('work', wid)['status'], 'cancelled')
        # admission N: delegate/submit after cancel must fail work_cancelled
        e = self.app.create_bot({'name': 'A', 'role': 'r'})
        with self.assertRaises(Fault) as cm:
            self.app.delegate('bot-main', wid, {'target_bot_id': e['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        self.assertEqual(cm.exception.code, 'work_cancelled')
        with self.assertRaises(Fault) as cm2:
            self.app.submit_task('bot-main', wid, {'prompt': 'x'})
        self.assertEqual(cm2.exception.code, 'work_cancelled')

    def test_cancel_work_recurses_into_children_and_settles(self):
        e = self.app.create_bot({'name': 'A', 'role': 'r'})
        parent = self.app.send('bot-main', {'text': '父'}, 'cwr')
        h = self.app.delegate('bot-main', parent['work_id'],
                              {'target_bot_id': e['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        self.app.cancel_work(parent['work_id'])
        self.assertEqual(self.app.store.get('work', parent['work_id'])['status'], 'cancelled')
        # child recursively cancelled
        self.assertEqual(self.app.store.get('work', h['work_id'])['status'], 'cancelled')
        # parent's child dependency settled cancelled (no dangling pending)
        deps = self.app.store.find('dependency', source_work_id=parent['work_id'])
        self.assertTrue(all(d['status'] == 'settled' for d in deps))

    def test_cancel_target_settles_source_immediately(self):
        # A waits on B (peer); cancel B -> A's dep cancelled at once.
        peer = self.app.create_bot({'name': 'B', 'role': 'r'})
        a = self.app.send('bot-main', {'text': 'A'}, 'ma')
        bwork = self.app.send(peer['id'], {'text': 'B'}, 'mb')
        res = self.app.send_bot_message('bot-main', a['work_id'],
            {'target_bot_id': peer['id'], 'target_work_id': bwork['work_id'],
             'text': '帮忙', 'requires_response': True})
        depid = res['dependency_id']
        self.assertEqual(self.app.store.get('dependency', depid)['status'], 'pending')
        self.app.cancel_work(bwork['work_id'])
        dep = self.app.store.get('dependency', depid)
        self.assertEqual(dep['status'], 'settled')
        self.assertEqual(dep['outcome'], 'cancelled')

    def test_cancel_dependency_authorization(self):
        # a Bot may only cancel a dependency of its OWN current Work.
        e = self.app.create_bot({'name': 'A', 'role': 'r'})
        m = self.app.send('bot-main', {'text': '活'}, 'cda')
        h = self.app.delegate('bot-main', m['work_id'],
                              {'target_bot_id': e['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        dep = self.app.store.find('dependency', source_work_id=m['work_id'])[0]
        # the main bot's current turn on work m may cancel its own dep
        turn = [t for t in self.app.store.find('turn', work_id=m['work_id'])][0]
        res = self.app.cancel_dependency('bot-main', turn['id'], dep['id'])
        self.assertEqual(res['status'], 'cancelled')
        self.assertEqual(self.app.store.get('dependency', dep['id'])['outcome'], 'cancelled')
        # child recursively cancelled (it was owned by this work)
        self.assertEqual(self.app.store.get('work', h['work_id'])['status'], 'cancelled')

    def test_cancel_dependency_forbidden_cross_work(self):
        e = self.app.create_bot({'name': 'A', 'role': 'r'})
        m1 = self.app.send('bot-main', {'text': '一'}, 'x1')
        self.app.delegate('bot-main', m1['work_id'],
                          {'target_bot_id': e['id'], 'goal': 'g', 'request': 'r', 'acceptance': ['a']})
        dep1 = self.app.store.find('dependency', source_work_id=m1['work_id'])[0]
        m2 = self.app.send('bot-main', {'text': '二'}, 'x2')
        turn2 = [t for t in self.app.store.find('turn', work_id=m2['work_id'])][0]
        # turn on work m2 cannot cancel work m1's dependency
        with self.assertRaises(Fault) as cm:
            self.app.cancel_dependency('bot-main', turn2['id'], dep1['id'])
        self.assertEqual(cm.exception.code, 'dependency_forbidden')

    def test_send_to_cancelled_target_rejected(self):
        peer = self.app.create_bot({'name': 'B', 'role': 'r'})
        a = self.app.send('bot-main', {'text': 'A'}, 'sa')
        bwork = self.app.send(peer['id'], {'text': 'B'}, 'sb')
        self.app.cancel_work(bwork['work_id'])
        with self.assertRaises(Fault) as cm:
            self.app.send_bot_message('bot-main', a['work_id'],
                {'target_bot_id': peer['id'], 'target_work_id': bwork['work_id'],
                 'text': '帮忙', 'requires_response': True})
        self.assertEqual(cm.exception.code, 'target_work_cancelled')

    def test_cancel_idempotent_on_terminal(self):
        m = self.app.send('bot-main', {'text': '活'}, 'idem')
        self.app.cancel_work(m['work_id'])
        # second cancel is a no-op, no error
        self.app.cancel_work(m['work_id'])
        self.assertEqual(self.app.store.get('work', m['work_id'])['status'], 'cancelled')

    def test_send_to_cancelled_work_forks_new(self):
        # 用户改主意（"不做了"→"还是做一下吧"）应能继续，且**拿回上下文**：带已 cancelled 的
        # work_id 发消息不复活终态旧 work（no-resurrection），而是 fork 一个新 active work，
        # 继承旧 work 的 goal 并记 continued_from（projection 顺链取回旧对话历史，不必重讲）。
        m = self.app.send('bot-main', {'text': '活'}, 'res')
        old_goal = self.app.store.get('work', m['work_id'])['goal']
        self.app.cancel_work(m['work_id'])
        r = self.app.send('bot-main', {'text': '继续', 'work_id': m['work_id']}, 'res2')
        old = self.app.store.get('work', m['work_id'])
        new = self.app.store.get('work', r['work_id'])
        self.assertIn(old['status'], {'cancelled', 'linked', 'failed'}) # 旧 work 未被复活
        self.assertNotEqual(r['work_id'], m['work_id']) # fork 到新 work
        self.assertEqual(new['status'], 'active')
        self.assertEqual(new.get('continued_from'), m['work_id']) # 链指向旧 work
        self.assertEqual(new.get('goal'), old_goal) # 继承目标/上下文

    def test_cancel_dependency_child_work_consumes_own_echo(self):
        # Exact incident topology: parent is already waiting on a delegated child;
        # a later user correction gets an interrupt turn and deterministically
        # withdraws the child dependency without waking the child Bot.
        worker = self.app.create_bot({'name': 'A', 'role': 'r'})
        m = self.app.send('bot-main', {'text': '父'}, 'echo-child')
        h = self.app.delegate('bot-main', m['work_id'],
                              {'target_bot_id': worker['id'], 'goal': 'g',
                               'request': 'r', 'acceptance': ['a']})
        dep = self.app.store.find('dependency', source_work_id=m['work_id'])[0]
        first = self.app.store.get('turn', m['turn_id'])
        first['status'] = 'done'
        self.app.store.put('turn', first)

        correction = self.app.send('bot-main',
            {'text': '停止刚才的派活', 'work_id': m['work_id']}, 'echo-child-stop')
        turn = self.app.store.get('turn', correction['turn_id'])
        self.assertEqual(turn['mode'], 'interrupt')
        turn['status'] = 'running'
        self.app.store.put('turn', turn)

        self.app.cancel_dependency('bot-main', turn['id'], dep['id'])

        self.assertEqual(self.app.store.get('dependency', dep['id'])['outcome'], 'cancelled')
        self.assertEqual(self.app.store.get('work', h['work_id'])['status'], 'cancelled')
        echoes = [x for x in self.app.store.find('message', work_id=m['work_id'])
                  if x.get('kind') == 'result' and x.get('consumed_by_turn_id') == turn['id']]
        self.assertEqual(len(echoes), 1)
        self.app.finish_turn(turn, '已停止旧派活')
        queued = [t for t in self.app.store.find('turn', work_id=m['work_id'])
                  if t['status'] == 'queued']
        self.assertEqual(queued, [])
        self.assertEqual(self.app.store.get('work', m['work_id'])['status'], 'delivered')

    def test_cancel_dependency_queued_task_consumes_own_echo(self):
        self.app.probe('codex-local')
        m = self.app.send('bot-main', {'text': '执行'}, 'echo-task')
        submitted = self.app.submit_task('bot-main', m['work_id'], {'prompt': 'x'})
        task = self.app.store.get('task', submitted['task_id'])
        dep = self.app.store.get('dependency', task['dependency_id'])
        turn = self.app.store.get('turn', m['turn_id'])
        turn['status'] = 'running'
        self.app.store.put('turn', turn)

        self.app.cancel_dependency('bot-main', turn['id'], dep['id'])

        self.assertEqual(self.app.store.get('task', task['id'])['status'], 'cancelled')
        self.assertEqual(self.app.store.get('dependency', dep['id'])['outcome'], 'cancelled')
        echoes = [x for x in self.app.store.find('message', work_id=m['work_id'])
                  if x.get('kind') == 'execution_result'
                  and x.get('consumed_by_turn_id') == turn['id']]
        self.assertEqual(len(echoes), 1)
        self.app.finish_turn(turn, '已停止旧任务')
        queued = [t for t in self.app.store.find('turn', work_id=m['work_id'])
                  if t['status'] == 'queued']
        self.assertEqual(queued, [])

    def test_cancel_dependency_running_task_keeps_physical_cancel_async(self):
        # Safety: withdrawing the obligation must not claim a running process is
        # already stopped. Existing device cancellation remains asynchronous.
        self.app.probe('codex-local')
        m = self.app.send('bot-main', {'text': '执行'}, 'echo-running-task')
        submitted = self.app.submit_task('bot-main', m['work_id'], {'prompt': 'x'})
        task = self.app.store.get('task', submitted['task_id'])
        task['status'] = 'running'
        self.app.store.put('task', task)
        dep = self.app.store.get('dependency', task['dependency_id'])
        turn = self.app.store.get('turn', m['turn_id'])
        turn['status'] = 'running'
        self.app.store.put('turn', turn)

        self.app.cancel_dependency('bot-main', turn['id'], dep['id'])

        self.assertEqual(self.app.store.get('dependency', dep['id'])['outcome'], 'cancelled')
        self.assertEqual(self.app.store.get('task', task['id'])['status'], 'cancel_requested')
        echoes = [x for x in self.app.store.find('message', work_id=m['work_id'])
                  if x.get('kind') == 'execution_result'
                  and x.get('consumed_by_turn_id') == turn['id']]
        self.assertEqual(len(echoes), 1)

    def test_cancel_dependency_exchange_consumes_own_echo(self):
        peer = self.app.create_bot({'name': 'B', 'role': 'r'})
        source = self.app.send('bot-main', {'text': '主'}, 'echo-ex-src')
        target = self.app.send(peer['id'], {'text': '副'}, 'echo-ex-dst')
        # Remove the target's unrelated initial turn so the exchange withdrawal
        # can prove its own queued peer obligation is cancelled cleanly.
        for t in self.app.store.find('turn', work_id=target['work_id']):
            if t['status'] == 'queued':
                t['status'] = 'done'
                self.app.store.put('turn', t)
        sent = self.app.send_bot_message('bot-main', source['work_id'],
            {'target_bot_id': peer['id'], 'target_work_id': target['work_id'],
             'text': '帮忙', 'requires_response': True})
        dep = self.app.store.get('dependency', sent['dependency_id'])
        turn = self.app.store.get('turn', source['turn_id'])
        turn['status'] = 'running'
        self.app.store.put('turn', turn)

        self.app.cancel_dependency('bot-main', turn['id'], dep['id'])

        self.assertEqual(self.app.store.get('dependency', dep['id'])['outcome'], 'cancelled')
        self.assertEqual(self.app.store.get('exchange', dep['target_id'])['status'], 'cancelled')
        echoes = [x for x in self.app.store.find('message', work_id=source['work_id'])
                  if x.get('kind') == 'peer_result'
                  and x.get('consumed_by_turn_id') == turn['id']]
        self.assertEqual(len(echoes), 1)
        peer_turns = [t for t in self.app.store.find('turn', work_id=target['work_id'])
                      if t['status'] == 'queued']
        self.assertEqual(peer_turns, [])
        self.app.finish_turn(turn, '已撤回请求')
        queued = [t for t in self.app.store.find('turn', work_id=source['work_id'])
                  if t['status'] == 'queued']
        self.assertEqual(queued, [])

    def test_withdraw_empties_target_turn(self):
        # Cancelling a source Work whose only outstanding work is a requires-response
        # peer must withdraw the peer's queued turn so the target does not execute it.
        peer = self.app.create_bot({'name': 'B', 'role': 'r'})
        a = self.app.send('bot-main', {'text': 'A'}, 'wa')
        # consume A's own user turn so only the peer exchange remains
        for t in self.app.store.find('turn', work_id=a['work_id']):
            if t['status'] == 'queued':
                t['status'] = 'done'
                self.app.store.put('turn', t)
        bwork = self.app.send(peer['id'], {'text': 'B'}, 'wb')
        for t in self.app.store.find('turn', work_id=bwork['work_id']):
            if t['status'] == 'queued':
                t['status'] = 'done'
                self.app.store.put('turn', t)
        res = self.app.send_bot_message('bot-main', a['work_id'],
            {'target_bot_id': peer['id'], 'target_work_id': bwork['work_id'],
             'text': '帮忙', 'requires_response': True})
        # target now has a queued peer turn
        target_queued = [t for t in self.app.store.find('turn', work_id=bwork['work_id']) if t['status'] == 'queued']
        self.assertEqual(len(target_queued), 1)
        # cancel the SOURCE work -> withdraw the exchange
        self.app.cancel_work(a['work_id'])
        # the target's peer turn (sole content = withdrawn exchange) is cancelled
        tt = self.app.store.get('turn', target_queued[0]['id'])
        self.assertEqual(tt['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
