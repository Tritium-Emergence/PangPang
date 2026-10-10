import json
import tempfile
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.runtime import Runtime
from pangpang.store import Fault


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=Service(config(self.root),self.root/'data')
    def tearDown(self):self.app.store.close();self.tmp.cleanup()
    def test_compaction_retains_original_and_constraints(self):
        class Summarizer:
            def complete(self,profile,messages,tools):
                assert not tools
                return {'content':'已经读取素材，下一步编写。未验证产物。'},{}
        r=Runtime(self.app,Summarizer())
        m=self.app.send('bot-main',{'text':'不能修改正文'},'c')
        turn=self.app.store.get('turn',m['turn_id']);turn['history']=r.context(turn)
        turn['history'].append({'role':'assistant','content':'细节-'*3000})
        turn=r.compact(turn,self.app.profiles['test'])
        self.assertIn('不能修改正文',json.dumps(turn['history'],ensure_ascii=False))
        archives=self.app.store.find('archive');self.assertEqual(len(archives),1)
        self.assertIn('细节-',json.dumps(archives[0],ensure_ascii=False))
        self.assertIn('未验证',json.dumps(turn['history'],ensure_ascii=False))
    def test_model_profile_resource_keys_forbidden(self):
        # the model pool is independent of the executor pool.
        # A model profile carrying resource_keys (the old coupling that let a busy
        # executor freeze conversations) is now rejected outright.
        cfg=config(self.root);cfg['models'][0]['resource_keys']=['qwen']
        self.app.store.close()
        with self.assertRaises(Fault):
            self.app=Service(cfg,self.root/'forbidden')
        # reopen a clean app so tearDown has a live store
        self.app=Service(config(self.root),self.root/'reopen')
    def test_explicit_host_survives_delegation(self):
        editor=self.app.create_bot({'name':'编辑','role':'编辑'})
        # Structured body field — free text mentioning a host is not a pin (F).
        m=self.app.send('bot-main',{'text':'在 local 执行','host_id':'local'},'host')
        h=self.app.delegate('bot-main',m['work_id'],{'target_bot_id':editor['id'],'goal':'编辑','request':'编辑','acceptance':['完成']})
        self.assertEqual(self.app.store.get('work',h['work_id'])['required_host_id'],'local')
    def test_root_delegation_budget_includes_repeat_handoffs(self):
        # v1.4.1 per-root delegation_count caps total fan-out on one causal
        # root. Repeated delegate on the SAME real root hits the cap.
        self.app.config['scheduler']={'max_delegations_per_root':2}
        self.app.scheduler_config=self.app._scheduler_config()
        b=self.app.create_bot({'name':'编辑','role':'编辑'})
        m=self.app.send('bot-main',{'text':'协作'},'budget')
        root=self.app.store.get('turn',m['turn_id'])['trigger_root_request_id']
        body={'target_bot_id':b['id'],'goal':'编辑','request':'编辑','acceptance':['完成']}
        self.app.delegate('bot-main',m['work_id'],body,root_request_id=root)
        self.app.delegate('bot-main',m['work_id'],body,root_request_id=root)
        with self.assertRaises(Fault):self.app.delegate('bot-main',m['work_id'],body,root_request_id=root)
    def test_durable_bot_reopens_with_latest_delivery(self):
        m=self.app.send('bot-main',{'text':'记住工作'},'state')
        turn=self.app.store.get('turn',m['turn_id']);self.app.finish_turn(turn,'本轮已交付')
        self.app.store.close();self.app=Service(config(self.root),self.root/'data')
        self.assertEqual(self.app.store.get('state','bot-main')['recent_result']['summary'],'本轮已交付')
    def test_new_work_does_not_leak_other_work_raw_content(self):
        # a NEW work must NOT receive another work's raw messages
        # in its context (that would leak another project's requests/conclusions and
        # could steer this turn). Cross-work continuity is now model-driven retrieval;
        # only a non-raw recent_work_index is offered.
        first=self.app.send('bot-main',{'text':'先比较 A 和 B'},'ctx-a')
        old=self.app.store.get('turn',first['turn_id'])
        self.app.finish_turn(old,'你要 A 还是 B？')
        old=self.app.store.get('turn',first['turn_id']);old.update(status='blocked',error='turn_budget');self.app.store.put('turn',old)
        second=self.app.send('bot-main',{'text':'先别选，告诉我风险'},'ctx-b')
        self.assertNotEqual(first['work_id'],second['work_id'])
        context=Runtime(self.app).context(self.app.store.get('turn',second['turn_id']))
        payload=json.dumps(context,ensure_ascii=False)
        # THIS work's own incoming message is present; the OTHER work's raw reply is NOT.
        self.assertIn('先别选，告诉我风险',payload)
        self.assertNotIn('你要 A 还是 B？',payload)
        # but the other work is discoverable via the non-raw index (id/goal/status)
        self.assertIn('recent_work_index',payload)
        self.assertIn(first['work_id'],payload)

if __name__=='__main__':unittest.main()
