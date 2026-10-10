import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from pangpang import consent
from pangpang.service import Service
from pangpang.runtime import Runtime
from pangpang.store import Fault


# Test-host policy (shared by every suite that imports this module): the test host
# is an L0 self-use host whose operator accepted the current process-group risk.
# Executor tests then launch on any OS -- Windows Job Object, a Linux cgroup scope,
# or the macOS / cgroup-less Linux process-group tier -- instead of failing with
# executor_cleanup_unsupported off Windows. Tests about the level or consent gates
# themselves pass deployment_level explicitly (None = undeclared) or drop the consent.
Service.__init__.__kwdefaults__['deployment_level']='L0'


def config(root):
    return {'default_model':'test','turn_retry_delays_s':[],'models':[{'id':'test','model':'test','endpoint':'http://127.0.0.1:1/v1','context_budget':40000}],
            'hosts':[{'id':'local','transport':'local','os':'Linux','allowed_roots':[str(root)],consent.CONSENT_KEY:consent.RISK_VERSION}],
            'installations':[{'id':'codex-local','host_id':'local','adapter':'codex','argv':[sys.executable,'-c',"import pathlib,sys; data=sys.stdin.read(); pathlib.Path('result.txt').write_text('done'); print('Executed '+data[:50])"],
                              'probe_argv':[sys.executable,'--version'],'cwd':str(root),'risk':'write'}]}


def call(tool_name, **body):
    return {'role':'assistant','tool_calls':[{'id':tool_name+'-call','type':'function','function':{'name':tool_name,'arguments':json.dumps(body)}}]}


class ScriptedModel:
    def __init__(self, service):
        self.service=service
        self.inputs=[]
    def complete(self, profile, messages, tools):
        self.inputs.append(copy.deepcopy(messages))
        # System content is prompt text (now multi-line: canonical prompt + response
        # -language block) followed by the JSON packet. The prompt carries no '{', so
        # the packet is the content from its first brace — robust to prompt newlines.
        content=messages[0]['content']
        system=json.loads(content[content.index('{'):])
        who=system['identity']['id']
        incoming=next(m for m in reversed(messages) if m['role']=='user')['content']
        if who=='bot-main':
            if 'source_work_id' in incoming:
                return {'content':'已完成：结果已交回。'}, {'prompt_tokens':123}
            last=messages[-1]
            if last['role']=='tool':
                bot=json.loads(last['content'])
                return call('delegate',target_bot_id=bot['id'],goal='制作文件',request='生成 result.txt',
                            constraints=['保留正文'],acceptance=['文件存在'],mode='return_to_sender'), {}
            return call('create_bot',name='公众号',role='文章主理'), {}
        if 'execution_result' in incoming:
            return {'content':'已生成 result.txt，执行成功。'}, {}
        return call('submit_task',prompt='生成测试产物',installation_id='codex-local',outputs=['result.txt']), {}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'workspace';self.root.mkdir()
        self.cfg=config(self.root);self.app=Service(self.cfg,self.root.parent/'data')
    def tearDown(self):
        self.app.store.close();self.tmp.cleanup()
    def test_independent_state_and_idempotency(self):
        a=self.app.send('bot-main',{'text':'做点事情'},'same')
        b=self.app.send('bot-main',{'text':'做点事情'},'same')
        self.assertEqual(a,b)
        with self.assertRaises(Fault):self.app.send('bot-main',{'text':'不同请求'},'same')
        self.assertEqual(self.app.bot('bot-main')['name'],'胖胖')
        state=self.app.store.get('state','bot-main')
        self.app.state_patch('bot-main',{'revision':state['revision'],'open_items':['TODO']})
        with self.assertRaises(Fault):self.app.state_patch('bot-main',{'revision':state['revision'],'open_items':[]})
    def test_web_bot_executor_roundtrip(self):
        self.app.probe('codex-local')
        model=ScriptedModel(self.app);runtime=Runtime(self.app,model)
        self.app.send('bot-main',{'text':'建一个公众号 Bot 并生成文件','allow_write':True},'roundtrip')
        for _ in range(100):
            runtime.step()
            if any(m.get('content')=='已完成：结果已交回。' for m in self.app.store.find('message')):break
            time.sleep(.05)
        tasks=self.app.store.find('task')
        self.assertEqual(len(tasks),1)
        self.assertEqual(tasks[0]['status'],'succeeded', tasks)
        self.assertTrue((self.root/'result.txt').exists())
        self.assertEqual(len(self.app.store.find('artifact')),1)
        self.assertTrue(any(m.get('content')=='已完成：结果已交回。' for m in self.app.store.find('message')))
        self.assertEqual(len(self.app.store.find('bot')),2)
        self.assertEqual(len(self.app.store.find('handoff')),1)
    def test_context_isolation_and_evidence(self):
        secret=self.app.send('bot-main',{'text':'PRIVATE-UNRELATED-SECRET'},'private')
        editor=self.app.create_bot({'name':'编辑','role':'编辑'})
        source=self.app.send('bot-main',{'text':'编辑正文'},'new')
        handoff=self.app.delegate('bot-main',source['work_id'],{'target_bot_id':editor['id'],'goal':'编辑','request':'改排版','acceptance':['不改文字']})
        runtime=Runtime(self.app)
        context=runtime.context(self.app.store.get('turn',handoff['turn_id']))
        self.assertNotIn('PRIVATE-UNRELATED-SECRET',json.dumps(context))
        with self.assertRaises(Fault):self.app.evidence(editor['id'],secret['message_id'])
        with self.assertRaises(Fault):self.app.delegate('bot-main',source['work_id'],{'target_bot_id':editor['id'],'goal':'x','request':'x','acceptance':['x'],'base_handoff_id':'missing'})
    def test_write_approval_and_recovery(self):
        self.app.probe('codex-local')
        # Conversation work grants write by default; withhold it on the Work so
        # this still exercises the approval -> approve -> recovery -> retry path.
        msg=self.app.send('bot-main',{'text':'工作'},'task')
        work=self.app.store.get('work',msg['work_id']);work['grant_write']=False;self.app.store.put('work',work)
        result=self.app.submit_task('bot-main',msg['work_id'],{'prompt':'工作'})
        task=self.app.store.get('task',result['task_id'])
        self.assertEqual(task['status'],'awaiting_approval')
        with self.assertRaises(Fault):self.app.approve(task['id'],{'spec_digest':'bad'})
        self.app.approve(task['id'],{'spec_digest':task['spec_digest']})
        task=self.app.store.get('task',task['id']);task['status']='running';self.app.store.put('task',task)
        self.app.recover()
        self.assertEqual(self.app.store.get('task',task['id'])['status'],'unknown')
        self.assertFalse(self.app.task_step())
        self.assertEqual(self.app.store.get('task',task['id'])['status'],'unknown')
        with self.assertRaises(Fault):self.app.retry_task(task['id'])
    def test_conversation_write_needs_no_approval(self):
        # A command the user typed in chat is itself the authorization: the task
        # should queue directly, not wait for a redundant approval click.
        self.app.probe('codex-local')
        msg=self.app.send('bot-main',{'text':'工作'},'task-auto')
        result=self.app.submit_task('bot-main',msg['work_id'],{'prompt':'工作'})
        task=self.app.store.get('task',result['task_id'])
        self.assertEqual(task['status'],'queued')
    def test_source_change_refused(self):
        p=self.root/'source.txt';p.write_text('old')
        artifact=self.app.import_file('bot-main',{'host_id':'local','path':str(p)})
        p.write_text('new')
        with self.assertRaises(Fault):self.app.transfer(artifact['id'],{'host_id':'local','path':str(self.root/'copy.txt')})
        self.assertFalse((self.root/'copy.txt').exists())
    def test_path_escape(self):
        with self.assertRaises(Fault):self.app.import_file('bot-main',{'host_id':'local','path':'/etc/passwd'})
    def test_journal_replays_without_duplicate_action(self):
        msg=self.app.send('bot-main',{'text':'工作'},'journal')
        turn=self.app.store.get('turn',msg['turn_id'])
        runtime=Runtime(self.app)
        action=call('create_bot',name='只创建一次',role='编辑')['tool_calls'][0]
        def create():return self.app.create_bot({'name':'只创建一次','role':'编辑'})
        once=self.app.store.once('tool:'+turn['id'],action['id'],action,create)
        again=self.app.store.once('tool:'+turn['id'],action['id'],action,create)
        self.assertEqual(once['id'],again['id']);self.assertEqual(len(self.app.store.find('bot')),2)
    def test_archive_active_rejected(self):
        b=self.app.create_bot({'name':'编辑','role':'编辑'})
        self.app.send(b['id'],{'text':'工作'},'x')
        with self.assertRaises(Fault):self.app.archive(b['id'])
    def test_route_ignores_probe_age(self):
        # 已删 stale/TTL：probe available 即合格，不因记录老而被拒（--version 结论不过期）。
        self.app.probe('codex-local')
        p=self.app.store.get('probe','codex-local');p['at']=time.time()-99999;self.app.store.put('probe',p)
        msg=self.app.send('bot-main',{'text':'活','allow_write':True},'age')
        w=self.app.store.get('work',msg['work_id'])
        route=self.app.route('bot-main',w,{})
        self.assertEqual(route['selected']['installation_id'],'codex-local',route)
    def test_route_rejects_never_probed_and_unavailable(self):
        # 真事实保留：从没发现过 → not_discovered；--version 失败(available=False) → discovery_failed。
        msg=self.app.send('bot-main',{'text':'活','allow_write':True},'np')
        w=self.app.store.get('work',msg['work_id'])
        route=self.app.route('bot-main',w,{}) # 未 probe
        self.assertIsNone(route['selected'])
        self.assertEqual(route['rejected'][0]['reason'],'not_discovered')
        self.app.store.put('probe',{'id':'codex-local','available':False,'reason':'binary_not_found','at':time.time()})
        route=self.app.route('bot-main',w,{})
        self.assertEqual(route['rejected'][0]['reason'],'discovery_failed')
    def test_submit_task_maps_route_failure_to_business_error(self):
        # 无合格执行器时，给模型稳定业务错误，不暴露 probe/stale 内部术语。
        from pangpang.service import ROUTE_ERROR
        self.assertEqual(ROUTE_ERROR['files_on_other_host'],'artifact_location_conflict')
        self.assertEqual(ROUTE_ERROR['bot_policy'],'executor_not_authorized')
        self.assertEqual(ROUTE_ERROR['not_discovered'],'requested_executor_not_registered')
        # 未发现任何执行器 → submit_task 抛业务错误（not_discovered→requested_executor_not_registered）
        msg=self.app.send('bot-main',{'text':'活','allow_write':True},'biz')
        try:
            self.app.submit_task('bot-main',msg['work_id'],{'prompt':'x'})
            self.fail('should raise')
        except Fault as e:
            self.assertEqual(e.code,'requested_executor_not_registered')
    def test_submit_task_targeted_reason_not_masked(self):
        # 模型指定目标执行器时，报目标自身的原因，不被非目标执行器的 reason 遮蔽(codex P2)。
        from pangpang.service import ROUTE_ERROR
        # 目标 codex-local 未发现(not_discovered→requested_executor_not_registered)；
        # 指定它 → 即使别的执行器可能有其它原因，也应报目标的注册问题。
        msg=self.app.send('bot-main',{'text':'活','allow_write':True},'tgt')
        try:
            self.app.submit_task('bot-main',msg['work_id'],{'prompt':'x','installation_id':'codex-local'})
            self.fail('should raise')
        except Fault as e:
            self.assertEqual(e.code,'requested_executor_not_registered')
    def test_catalog_hides_probe_atomic_state(self):
        # 模型侧 catalog 不暴露 probe 的 available/at/reason；只给稳定的 discovered 布尔。
        self.app.probe('codex-local')
        runtime=Runtime(self.app,ScriptedModel(self.app))
        msg=self.app.send('bot-main',{'text':'x'},'cat')
        turn=self.app.store.get('turn',msg['turn_id'])
        catalog=runtime.invoke(turn,'catalog',{})
        self.assertNotIn('probes',catalog)
        inst=next(i for i in catalog['installations'] if i['id']=='codex-local')
        self.assertIn('discovered',inst)
        self.assertNotIn('available',inst);self.assertNotIn('at',inst);self.assertNotIn('reason',inst)
    def test_create_bot_rejects_active_duplicate_name(self):
        # 同名 active bot 不允许重复创建；归档后可重建同名。
        self.app.create_bot({'name':'军火主管','role':'售前'})
        with self.assertRaises(Fault):
            self.app.create_bot({'name':'军火主管','role':'另一个'})
        # 归档第一个后，同名可再建
        for b in self.app.store.find('bot'):
            if b['name']=='军火主管':
                b['lifecycle']='archived';self.app.store.put('bot',b)
        again=self.app.create_bot({'name':'军火主管','role':'重建'})
        self.assertEqual(again['name'],'军火主管')
    def test_transient_model_failure_retries_then_succeeds(self):
        # 瞬时 model_request_failed 应重试而非直接 blocked。重试不再占住 worker
        # 睡眠，而是把 turn requeue + not_before 退避；每次 step 一试，多试后成功。
        cfg=config(self.root);cfg['turn_retry_delays_s']=[0,0]
        self.app.store.close();self.app=Service(cfg,self.root.parent/'retry-ok')
        class Flaky:
            def __init__(self):self.calls=0
            def complete(self,profile,messages,tools):
                self.calls+=1
                if self.calls<3:raise Fault('model_request_failed')
                return {'content':'终于成功了。'},{'prompt_tokens':1}
        model=Flaky();m=self.app.send('bot-main',{'text':'干活'},'flaky')
        rt=Runtime(self.app,model)
        for _ in range(5): # drain requeued retries (delays are 0 -> immediately runnable)
            if self.app.store.get('turn',m['turn_id'])['status']=='done':break
            rt.step()
        self.assertEqual(model.calls,3)
        turn=self.app.store.get('turn',m['turn_id'])
        self.assertEqual(turn['status'],'done')
        self.assertEqual(turn.get('retry_count'),2) # 两次瞬时失败后第三次成功
    def test_persistent_model_failure_blocks_after_retries(self):
        # 重试用尽仍失败 → blocked，且不无限重试。requeue 退避后每 step 一试。
        cfg=config(self.root);cfg['turn_retry_delays_s']=[0,0]
        self.app.store.close();self.app=Service(cfg,self.root.parent/'retry-fail')
        class Dead:
            def __init__(self):self.calls=0
            def complete(self,*a):self.calls+=1;raise Fault('model_request_failed')
        model=Dead();m=self.app.send('bot-main',{'text':'干活'},'dead')
        rt=Runtime(self.app,model)
        for _ in range(6): # 首次 + 2 次重试后应 blocked
            if self.app.store.get('turn',m['turn_id'])['status']=='blocked':break
            rt.step()
        self.assertEqual(model.calls,3) # 首次 + 2 次重试
        turn=self.app.store.get('turn',m['turn_id'])
        self.assertEqual(turn['status'],'blocked')
        self.assertEqual(turn['error'],'model_request_failed')
    def test_non_retryable_failure_blocks_immediately(self):
        # 不可重试错误（如 turn_budget）不应触发重试。
        cfg=config(self.root);cfg['turn_retry_delays_s']=[0,0]
        self.app.store.close();self.app=Service(cfg,self.root.parent/'no-retry')
        class Budget:
            def __init__(self):self.calls=0
            def complete(self,*a):self.calls+=1;raise Fault('context_budget')
        model=Budget();m=self.app.send('bot-main',{'text':'干活'},'budget')
        Runtime(self.app,model).step()
        self.assertEqual(model.calls,1) # 立即 blocked，不重试
        self.assertEqual(self.app.store.get('turn',m['turn_id'])['status'],'blocked')
    def test_thinking_is_explicit_not_inferred_from_qwen(self):
        # Unknown Qwen endpoints must not receive guessed vendor parameters.
        cfg=config(self.root);cfg['models'][0]['model']='qwen3.8-27b'
        self.app.store.close();self.app=Service(cfg,self.root.parent/'qwen')
        self.assertNotIn('generation_settings', self.app.profiles['test'])
        off=config(self.root);off['models'][0].update(model='qwen3.8-27b',generation_settings={'chat_template_kwargs':{'enable_thinking':False}})
        self.app.store.close();self.app=Service(off,self.root.parent/'qwen-off')
        self.assertIs(self.app.profiles['test']['generation_settings']['chat_template_kwargs']['enable_thinking'],False)
        cfg2=config(self.root);cfg2['models'][0]['model']='gpt-4o'
        self.app.store.close();self.app=Service(cfg2,self.root.parent/'gpt')
        self.assertNotIn('chat_template_kwargs',self.app.profiles['test'].get('generation_settings',{}))
    def test_retry_delays_sanitized(self):
        # 脏配置不能让 except 块内的 time.sleep 抛异常卡死 turn。
        from pangpang.runtime import _retry_delays, DEFAULT_RETRY_DELAYS
        self.assertEqual(_retry_delays({}), DEFAULT_RETRY_DELAYS)
        self.assertEqual(_retry_delays({'turn_retry_delays_s':[]}),[])
        self.assertEqual(_retry_delays({'turn_retry_delays_s':[0,0]}),[0,0])
        self.assertEqual(_retry_delays({'turn_retry_delays_s':[-1]}),DEFAULT_RETRY_DELAYS) # 负数回退
        self.assertEqual(_retry_delays({'turn_retry_delays_s':['x']}),DEFAULT_RETRY_DELAYS) # 非数回退
        self.assertEqual(_retry_delays({'turn_retry_delays_s':'nope'}),DEFAULT_RETRY_DELAYS) # 非列表回退
        self.assertEqual(_retry_delays({'turn_retry_delays_s':[999]}),[30.0]) # 上限封顶
        self.assertEqual(len(_retry_delays({'turn_retry_delays_s':[0]*99})),5) # 数量封顶

if __name__=='__main__':unittest.main()
