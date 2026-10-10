import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_runtime import config
from pangpang.connector import Connector, policy_digest
from pangpang.runtime import Runtime
from pangpang.service import Service
from pangpang.store import Fault


class EchoModel:
    def __init__(self):
        self.calls=[]
    def complete(self, profile, messages, tools):
        self.calls.append(messages)
        return {'content':'ok'}, {'prompt_tokens':1,'completion_tokens':1}


class V05Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.app=Service(config(self.root),self.root/'data')
    def tearDown(self):
        self.app.store.close();self.tmp.cleanup()

    def test_direct_user_turn_has_priority_over_routine(self):
        routine=self.app.create_routine('bot-main',{'instruction':'后台日报','timezone':'Asia/Shanghai','schedule':{'type':'daily','time':'05:00'}})
        run=self.app.run_routine_now('bot-main',routine['id'])
        direct=self.app.send('bot-main',{'text':'现在先回答我'},'direct-priority')
        model=EchoModel();Runtime(self.app,model).step()
        self.assertEqual(self.app.store.get('turn',direct['turn_id'])['status'],'done')
        self.assertEqual(self.app.store.get('turn',run['turn_id'])['status'],'queued')

    def test_link_work_and_checkpoint_are_durable_and_cas(self):
        old=self.app.send('bot-main',{'text':'测试 Windows Qwen'},'old-work')
        self.app.finish_turn(self.app.store.get('turn',old['turn_id']),'默认测试完成')
        fresh=self.app.send('bot-main',{'text':'再测 low，cloud 先别装'},'redirect')
        linked=self.app.link_work(fresh['turn_id'],old['work_id'])
        self.assertEqual(linked['work']['id'],old['work_id'])
        self.assertTrue(linked['yielded']) # link ends this model context
        # the rebound message is on the target and UN-consumed, so a fresh target
        # turn rebuilds its projection there.
        msg=self.app.store.get('message',fresh['message_id'])
        self.assertEqual(msg['work_id'],old['work_id'])
        self.assertIsNone(msg.get('consumed_by_turn_id'))
        work=self.app.store.get('work',old['work_id'])
        updated=self.app.checkpoint_work('bot-main',work['id'],{'phase':'对照测试','summary':'默认完成，追加 low',
            'completed':['默认测试'],'next_actions':['thinking=low'],'blocked_on':['cloud 安装暂停']},fresh['message_id'])
        self.assertEqual(updated['checkpoint']['next_actions'],['thinking=low'])
        # checkpoint_work no longer requires (or CAS-checks) a model-supplied revision:
        # a Work has at most one running turn, and the model is never shown the work
        # revision, so a version gate only produced an unsatisfiable state_conflict loop.
        # A second call (even with a stale/absent revision) simply overwrites.
        again=self.app.checkpoint_work('bot-main',work['id'],{'revision':work['revision'],'phase':'stale-ignored'})
        self.assertEqual(again['checkpoint']['phase'],'stale-ignored')

    def test_peer_exchange_response_wakes_source_work(self):
        child=self.app.create_bot({'name':'小伍','role':'研发'})
        source=self.app.send('bot-main',{'text':'测速'},'peer-source')
        handoff=self.app.delegate('bot-main',source['work_id'],{'target_bot_id':child['id'],'goal':'Windows Qwen 测速','request':'先跑默认','acceptance':['回报速度']})
        result=self.app.send_bot_message('bot-main',source['work_id'],{'target_bot_id':child['id'],'target_work_id':handoff['work_id'],
            'text':'追加 thinking=low；cloud 暂停','requires_response':True,'refs':[]})
        exchange=self.app.store.get('exchange',result['exchange_id'])
        self.assertEqual(exchange['status'],'pending')
        self.app.finish_turn(self.app.store.get('turn',result['turn_id']),'low 已测完')
        exchange=self.app.store.get('exchange',result['exchange_id'])
        self.assertEqual(exchange['status'],'responded')
        self.assertTrue(self.app.store.find('turn',bot_id='bot-main',status='queued'))
        # v1.4.1 coalescing: the peer follow-up merged into the child's
        # still-queued handoff turn (same target work), so finishing that single
        # turn both replies to the peer AND delivers the delegated child. Both of
        # source's dependencies (exchange + child_work) settle from the one turn,
        # and the source is woken once with the coalesced results.
        source_work=self.app.store.get('work',source['work_id'])
        self.assertEqual(source_work['pending_dep_ids'],[])
        self.assertIn(source_work['status'],('active','delivered'))

    def test_child_bot_cannot_read_other_bot_private_instructions(self):
        child=self.app.create_bot({'name':'小伍','role':'研发','instructions':'CHILD-PRIVATE-INSTRUCTION'})
        peer=self.app.create_bot({'name':'小顾','role':'编辑','instructions':'PEER-PRIVATE-INSTRUCTION'})
        child_turn=self.app.send(child['id'],{'text':'看看团队'},'child-catalog')
        runtime=Runtime(self.app,EchoModel())
        turn=self.app.store.get('turn',child_turn['turn_id'])
        catalog=runtime.invoke(turn,'catalog',{})
        peer_view=next(b for b in catalog['bots'] if b['id']==peer['id'])
        main_view=next(b for b in catalog['bots'] if b['id']=='bot-main')
        self.assertNotIn('instructions',peer_view)
        self.assertNotIn('instructions',main_view)
        self.assertIn('instructions',next(b for b in catalog['bots'] if b['id']==child['id']))
        inspected=runtime.invoke(turn,'inspect_bot',{'bot_id':peer['id']})
        self.assertNotIn('instructions',inspected['bot'])
        main_turn=self.app.send('bot-main',{'text':'检查岗位'},'main-inspect')
        full=runtime.invoke(self.app.store.get('turn',main_turn['turn_id']),'inspect_bot',{'bot_id':peer['id']})
        self.assertEqual(full['bot']['instructions'],'PEER-PRIVATE-INSTRUCTION')

    def test_routine_run_now_keeps_normal_schedule_and_scheduled_run_is_idempotent(self):
        routine=self.app.create_routine('bot-main',{'instruction':'出日报','timezone':'Asia/Shanghai','schedule':{'type':'daily','time':'05:00'}})
        normal=routine['next_run_at']
        manual=self.app.run_routine_now('bot-main',routine['id'])
        after=self.app.store.get('routine',routine['id'])
        self.assertEqual(after['next_run_at'],normal)
        self.assertEqual(self.app.store.get('turn',manual['turn_id'])['priority'],3)
        a=self.app._materialize_routine(after,normal,'scheduled')
        b=self.app._materialize_routine(after,normal,'scheduled')
        self.assertEqual(a['id'],b['id'])

    def test_context_window_tokens_are_not_compared_to_serialized_bytes(self):
        profile=next(iter(self.app.profiles.values()))
        self.assertEqual(profile['context_window_tokens'],40000)
        self.assertGreaterEqual(profile['serialized_bytes_safety_limit'],256*1024)
        msg=self.app.send('bot-main',{'text':'你好'},'context-units')
        Runtime(self.app,EchoModel()).step()
        metrics=self.app.store.get('turn',msg['turn_id'])['context_metrics']
        self.assertEqual(metrics['context_window_tokens'],40000)
        self.assertIn('serialized_bytes_safety_limit',metrics)
        self.assertNotIn('input_token_upper_bound',metrics)

    def test_retry_reuses_original_prompt_without_growth(self):
        self.app.probe('codex-local')
        msg=self.app.send('bot-main',{'text':'做任务','allow_write':True},'retry')
        result=self.app.submit_task('bot-main',msg['work_id'],{'prompt':'原始任务'})
        old=self.app.store.get('task',result['task_id']);old['status']='failed';self.app.store.put('task',old)
        retried=self.app.retry_task(old['id'])
        new=self.app.store.get('task',retried['task_id'])
        self.assertEqual(new['user_prompt'],'原始任务')
        self.assertEqual(new['prompt'].count('User constraints: '),1)
        self.assertEqual(new['prompt'].count('Input files: '),1)

    def test_unknown_local_task_requires_operator_evidence(self):
        self.app.probe('codex-local')
        msg=self.app.send('bot-main',{'text':'做任务','allow_write':True},'resolve')
        result=self.app.submit_task('bot-main',msg['work_id'],{'prompt':'任务'})
        task=self.app.store.get('task',result['task_id']);task['status']='unknown';self.app.store.put('task',task)
        with self.assertRaises(Fault):
            self.app.resolve_task(task['id'],{'resolution':'confirmed_stopped','evidence':'too short'})
        resolved=self.app.resolve_task(task['id'],{'resolution':'confirmed_stopped','evidence':'已现场确认对应进程树完全停止'})
        self.assertEqual(resolved['status'],'cancelled')
        self.assertEqual(resolved['receipt']['operator_resolution'],'confirmed_stopped')


class V05ConnectorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        local=config(self.root);local['installations'][0]['enabled']=True
        local.update(host_id='local',cloud_url='http://127.0.0.1:1',credential_env='TEST_DEVICE_KEY')
        self.connector=Connector(local,self.root/'device')
    def tearDown(self):
        for run in self.connector.store.find('local_run',limit=1000):
            if run['status']=='active':
                try:self.connector.execution.cancel(run['task'])
                except Exception:pass
        self.connector.close();self.tmp.cleanup()
    def task(self):
        from pangpang.store import launch_contract, contract_digest
        t={'id':'task-test','execution_id':'exec-'+'1'*32,'spec_digest':'spec-test','host_id':'local',
           'installation_id':'codex-local','inputs':[],'outputs':[],'prompt':'test','attempt_uuid':'att-test',
           'cwd':self.connector.config['installations'][0]['cwd'],'timeout_s':3600,'access_mode':'write',
           'resource_keys':[],'policy_digest':policy_digest(self.connector.config,'codex-local')}
        # A real v2 start carries the signed contract + digest + anti-downgrade marker.
        t['contract']=launch_contract(t); t['contract_digest']=contract_digest(t)
        t['min_contract_version']=t['contract']['contract_version'] # as broker.start sends it
        return t
    def test_crash_after_launch_intent_before_spawn_recovers_as_never_started(self):
        task=self.task()
        with patch.object(self.connector.execution,'start',side_effect=RuntimeError('crash')):
            result=self.connector.start(task)
        self.assertEqual(result['error'],'local_launch_unconfirmed')
        self.assertEqual(self.connector.store.get('local_run',task['execution_id'])['status'],'launching')
        self.connector.collect()
        run=self.connector.store.get('local_run',task['execution_id'])
        self.assertEqual(run['status'],'report')
        self.assertEqual(run['receipt']['status'],'cancelled')
        self.assertTrue(run['receipt']['process_tree_confirmed'])
    def test_resolve_never_started_requires_no_runner_directory(self):
        task=self.task();self.connector.store.put('local_run',{'id':task['execution_id'],'task':task,'status':'launching','launch_phase':'intent'})
        command={'id':'cmd-resolve','action':'resolve_run','payload':{'execution_id':task['execution_id'],'resolution':'confirmed_never_started'},'expires':None}
        self.connector.execute(command)
        self.assertEqual(self.connector.store.get('local_run',task['execution_id'])['status'],'report')
        self.assertTrue(self.connector.store.get('local_run',task['execution_id'])['receipt']['process_tree_confirmed'])


if __name__=='__main__':unittest.main()
