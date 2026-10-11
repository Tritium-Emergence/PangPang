import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from test_bot_runtime import config
from pangpang.execution import task_interjection_tier
from pangpang.service import Service, supervision_ok, STRONG_SUPERVISION, L0_COOPERATIVE_SUPERVISION
from pangpang.runtime import Runtime
from pangpang.store import Fault


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'workspace';self.root.mkdir()
        self.cfg=config(self.root)
    def tearDown(self):
        if hasattr(self,'app'):self.app.store.close()
        self.tmp.cleanup()
    def start_task(self,code):
        self.cfg['installations'][0]['argv']=[sys.executable,'-c',code]
        self.app=Service(self.cfg,self.root.parent/'data');self.app.probe('codex-local')
        msg=self.app.send('bot-main',{'text':'task','allow_write':True},'exec')
        result=self.app.submit_task('bot-main',msg['work_id'],{'prompt':'task'})
        self.app.task_step()
        return self.app.store.get('task',result['task_id'])
    def test_cancel_waits_for_receipt(self):
        task=self.start_task('import time;time.sleep(60)')
        directory=Path(self.app.execution.directory(self.app.execution.hosts['local'],task['execution_id']))
        for _ in range(100):
            if (directory/'started.json').exists():break
            time.sleep(.03)
        self.assertTrue((directory/'started.json').exists())
        self.app.cancel(task['id'])
        self.assertEqual(self.app.store.get('task',task['id'])['status'],'cancel_requested')
        for _ in range(100):
            self.app.task_step()
            task=self.app.store.get('task',task['id'])
            if task['status']=='cancelled':break
            time.sleep(.05)
        self.assertEqual(task['status'],'cancelled',task)
        self.assertTrue(task['receipt']['process_tree_confirmed'])
    def test_disconnect_receipt_survives_restart(self):
        # a coordinator restart must NOT manufacture
        # unknown for a detached runner. recover() re-observes: while the attempt is
        # alive it stays active; once its receipt lands it settles to the real
        # outcome (succeeded), never a spurious unknown.
        task=self.start_task('import time;time.sleep(.2);print("persisted-result")')
        self.app.recover()
        self.assertIn(self.app.store.get('task',task['id'])['status'],('running','starting','succeeded'))
        for _ in range(100):
            self.app.task_step()
            task=self.app.store.get('task',task['id'])
            if task['status']=='succeeded':break
            try:task=self.app.reconcile(task['id']);break
            except Fault:pass
            time.sleep(.05)
        self.assertEqual(task['status'],'succeeded',task)
        self.assertIn('persisted-result',task['receipt']['tail'])
        # Collecting the same receipt twice cannot notify twice.
        count=len(self.app.store.find('message'))
        self.app.reconcile(task['id'])
        self.assertEqual(count,len(self.app.store.find('message')))
    def test_resource_release_on_success(self):
        marker=self.root/'slot';events=self.root/'events'
        acquire=[sys.executable,'-c',f"from pathlib import Path;Path({str(marker)!r}).open('x').close();Path({str(events)!r}).write_text('acquired')"]
        release=[sys.executable,'-c',f"from pathlib import Path;Path({str(marker)!r}).unlink();p=Path({str(events)!r});p.write_text(p.read_text()+' released')"]
        self.cfg['resources']=[{'key':'qwen','acquire_argv':acquire,'release_argv':release}]
        self.cfg['installations'][0]['resource_keys']=['qwen']
        task=self.start_task('print("ok")')
        for _ in range(100):
            self.app.task_step();task=self.app.store.get('task',task['id'])
            if task['status']=='succeeded':break
            time.sleep(.05)
        self.assertEqual(task['status'],'succeeded',task)
        self.assertFalse(marker.exists());self.assertEqual(events.read_text(),'acquired released')
    def test_mismatched_receipt_never_releases(self):
        self.app=Service(self.cfg,self.root.parent/'data');self.app.probe('codex-local')
        m=self.app.send('bot-main',{'text':'task'},'x')
        r=self.app.submit_task('bot-main',m['work_id'],{'prompt':'x'})
        t=self.app.store.get('task',r['task_id']);t['status']='unknown';self.app.store.put('task',t)
        self.app.execution.poll=lambda task,**kw:{'status':'succeeded','execution_id':'wrong','spec_digest':task['spec_digest'],'process_tree_confirmed':True}
        with self.assertRaises(Fault):self.app.reconcile(t['id'])
        self.assertEqual(self.app.store.get('task',t['id'])['status'],'unknown')


    def test_succeeded_result_text_carried_untrusted_inspect_stays_clean(self):
        # A SUCCEEDED run's output tail IS the deliverable for query/research tasks
        # (answer = text, no file), so it is carried back to the parent Bot — but
        # BOUNDED and explicitly framed as UNTRUSTED data (the system prompt tells the
        # model it is data, never instructions). The raw tail still never leaks through
        # the inspect_task tool view, which stays operator-only.
        marker = "UNTRUSTED-EXECUTOR-SAYS-CALL-DELEGATE"
        task = self.start_task(f'print("{marker}")')
        for _ in range(100):
            self.app.task_step()
            task = self.app.store.get('task', task['id'])
            if task['status'] in {'succeeded', 'failed', 'cancelled'}:
                break
            time.sleep(.05)
        self.assertEqual(task['status'], 'succeeded', task)
        self.assertIn(marker, task['receipt']['tail']) # operator/raw receipt retains it

        result_messages = [
            m for m in self.app.store.find('message', work_id=task['work_id'])
            if m.get('kind') == 'execution_result'
        ]
        self.assertTrue(result_messages)
        content = result_messages[-1].get('content', '')
        # New behaviour: the succeeded result text reaches the model...
        self.assertIn(marker, content)
        #...but is explicitly framed as untrusted data, not instructions.
        self.assertIn('不可信', content)

        turn = next(t for t in self.app.store.find('turn', work_id=task['work_id']))
        view = Runtime(self.app).invoke(
            turn, 'inspect_task', {'task_id': task['id']})
        # inspect_task stays operator-only: no raw tail, no untrusted tail field.
        self.assertNotIn(marker, json.dumps(view, ensure_ascii=False))
        self.assertNotIn('untrusted_executor_tail', json.dumps(view, ensure_ascii=False))

    # --- acceptance ---------------------------------------
    def test_observation_failure_never_marks_unknown(self):
        # A1b (SIS red line): an inspection/transport failure MUST NOT flip a
        # non-terminal task to unknown. It is recorded as an observation failure
        # (backoff) and the task stays active.
        from pangpang.store import Fault
        task = self.start_task('import time;time.sleep(60)')
        self.assertEqual(self.app.store.get('task', task['id'])['status'], 'running')
        self.app.execution.poll = lambda t, **k: (_ for _ in ()).throw(Fault('transport_unavailable'))
        for _ in range(5):
            self.app.task_step()
        cur = self.app.store.get('task', task['id'])
        self.assertEqual(cur['status'], 'running') # NOT unknown
        self.assertGreaterEqual(cur.get('observation_failures', 0), 1)
        self.assertIsNotNone(cur.get('observation_retry_not_before'))
        del self.app.execution.poll # restore real poll so cancel can settle
        self.app.cancel(task['id'])
        for _ in range(100):
            self.app.task_step()
            if self.app.store.get('task', task['id'])['status'] in ('cancelled', 'unknown'):
                break
            time.sleep(.05)

    def test_stale_attempt_receipt_is_fenced(self):
        # A4: a receipt from a DIFFERENT attempt_uuid (retry / reused exec dir) must
        # never drive this attempt's terminal transition.
        from pangpang.store import Fault
        task = self.start_task('import time;time.sleep(60)')
        cur = self.app.store.get('task', task['id'])
        self.assertTrue(cur.get('attempt_uuid'))
        foreign = {'status': 'succeeded', 'process_tree_confirmed': True,
                   'execution_id': cur['execution_id'], 'spec_digest': cur['spec_digest'],
                   'attempt_uuid': 'a-different-attempt', 'tail': 'stale'}
        self.app.execution.poll = lambda t, **k: foreign
        for _ in range(3):
            self.app.task_step()
        self.assertEqual(self.app.store.get('task', task['id'])['status'], 'running') # fenced
        # reconcile() must also reject the foreign receipt
        with self.assertRaises(Fault):
            self.app.reconcile(task['id'])
        del self.app.execution.poll # restore real poll so cancel can settle
        self.app.cancel(task['id'])
        for _ in range(100):
            self.app.task_step()
            if self.app.store.get('task', task['id'])['status'] in ('cancelled', 'unknown'):
                break
            time.sleep(.05)

    def test_foreign_unknown_receipt_never_marks_unknown(self):
        # a status='unknown' receipt that fails attempt fencing (foreign/
        # stale execution_id) must NOT flip this attempt to unknown. Only a receipt
        # THIS attempt's runner wrote (identity match) may report runner_unverified.
        task = self.start_task('import time;time.sleep(60)')
        foreign = {'status': 'unknown', 'execution_id': 'exec-' + '0'*32,
                   'spec_digest': 'wrong', 'attempt_uuid': 'someone-else'}
        self.app.execution.poll = lambda t, **k: foreign
        for _ in range(3):
            self.app.task_step()
        self.assertEqual(self.app.store.get('task', task['id'])['status'], 'running') # not unknown
        del self.app.execution.poll
        self.app.cancel(task['id'])
        for _ in range(100):
            self.app.task_step()
            if self.app.store.get('task', task['id'])['status'] in ('cancelled', 'unknown'):
                break
            time.sleep(.05)
            time.sleep(.05)

    def test_cancel_send_failure_never_marks_unknown(self):
        # (SIS): a failed cancel SEND is an observation failure, not a task
        # death. The task stays cancel_requested (device_cancel_sent unset) so
        # task_step retries the send on backoff — a transport error never fabricates
        # unknown.
        task = self.start_task('import time;time.sleep(60)')
        self.assertEqual(self.app.store.get('task', task['id'])['status'], 'running')
        self.app.execution.cancel = lambda t, **k: (_ for _ in ()).throw(Fault('cancel_transport_unavailable'))
        cur = self.app.cancel(task['id'])
        self.assertEqual(cur['status'], 'cancel_requested') # NOT unknown
        self.assertFalse(cur.get('device_cancel_sent')) # send did not land
        self.assertGreaterEqual(cur.get('observation_failures', 0), 1)
        del self.app.execution.cancel # restore real cancel so the task can settle
        for _ in range(100):
            self.app.task_step()
            if self.app.store.get('task', task['id'])['status'] in ('cancelled', 'unknown'):
                break
            time.sleep(.05)


class FastAcceptTests(unittest.TestCase):
    """Phase C method B: Jev fast-accept delivers a read/query task result directly
    without waking the planner. Blast radius is locked by deterministic gates."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()
        self.cfg = config(self.root)
        # verified_read installation (query-shaped) + the current fail-closed gate.
        self.cfg['installations'][0]['risk'] = 'read'
        self.cfg['installations'][0]['permission_verified'] = True
        self.cfg['task_result_gate'] = {'mode': 'enforce', 'fail_open': False}

    def tearDown(self):
        if hasattr(self, 'app'): self.app.store.close()
        self.tmp.cleanup()

    def _run_query_task(self, code, jev, expect_fast_accept=False):
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', code]
        self.app = Service(self.cfg, self.root.parent / 'data'); probe = self.app.probe('codex-local')
        if expect_fast_accept and probe.get('supervision') == 'posix-pgroup-cooperative':
            # This host only offers the L0 process-group tier, whose receipts never
            # fast-accept by design (test_pgroup_tier covers that refusal).
            # The fast path itself is exercised on Job Object / cgroup hosts.
            self.skipTest('fast-accept needs a strong supervision tier; this host has the process-group tier')
        # verified_read => no write-approval needed; task goes straight to queued.
        self.app.task_result_gate.gate._call = jev
        msg = self.app.send('bot-main', {'text': 'q'}, 'faq')
        res = self.app.submit_task('bot-main', msg['work_id'], {'prompt': 'q'})
        work_id = self.app.store.get('task', res['task_id'])['work_id']
        # The bot's own launch turn (created by send()) is not driven by a scheduler
        # in this test; clear it so it does not count as a 'live turn' that (correctly)
        # blocks fast-accept. In production the launch turn is already done by the time
        # the task settles. Cancel the queued turns for this Work.
        for t in self.app.store.find('turn', work_id=work_id, limit=10):
            if t['status'] in ('queued', 'running'):
                t['status'] = 'done'; self.app.store.put('turn', t)
        for _ in range(200):
            self.app.task_step()
            t = self.app.store.get('task', res['task_id'])
            if t['status'] in ('succeeded', 'failed', 'unknown', 'cancelled'): break
            time.sleep(.05)
        return res, work_id, t

    def test_accept_delivers_without_planner_turn(self):
        jev = lambda *a, **k: {'disposition': 'ACCEPT', 'confidence': 1.0, 'model': 'jev', 'latency_ms': 1}
        res, work_id, task = self._run_query_task('print("the answer is 42")', jev, expect_fast_accept=True)
        self.assertEqual(task['status'], 'succeeded', task)
        work = self.app.store.get('work', work_id)
        self.assertEqual(work['status'], 'delivered') # direct delivery
        # planner (27B) was NOT woken: no continuation turn is queued/running
        live = [t for t in self.app.store.find('turn', work_id=work_id, limit=10)
                if t['status'] in ('queued', 'running')]
        self.assertEqual(live, [])
        deliveries = [m for m in self.app.store.find('message', work_id=work_id)
                      if m.get('kind') == 'delivery' and m.get('fast_accept')]
        self.assertTrue(deliveries)
        self.assertIn('42', deliveries[-1]['content'])

    def test_escalate_wakes_planner(self):
        jev = lambda *a, **k: {'disposition': 'ESCALATE', 'confidence': 1.0, 'model': 'jev', 'latency_ms': 1}
        res, work_id, task = self._run_query_task('print("partial maybe")', jev)
        self.assertEqual(task['status'], 'succeeded', task)
        work = self.app.store.get('work', work_id)
        self.assertNotEqual(work['status'], 'delivered') # planner must decide
        # a continuation turn was queued to wake the planner
        live = [t for t in self.app.store.find('turn', work_id=work_id, limit=10)
                if t['status'] in ('queued', 'running')]
        self.assertTrue(live)

    def test_gate_unavailable_never_directly_delivers(self):
        from pangpang.semantic_gate import GateUnavailable
        def jev(*a, **k): raise GateUnavailable('request_failed')
        res, work_id, task = self._run_query_task('print("answer needing planner verification")', jev)
        self.assertEqual(task['status'], 'succeeded', task)
        work = self.app.store.get('work', work_id)
        self.assertNotEqual(work['status'], 'delivered') # absence of a verdict is not authority
        live = [t for t in self.app.store.find('turn', work_id=work_id, limit=10)
                if t['status'] in ('queued', 'running')]
        self.assertTrue(live)
        fast = [m for m in self.app.store.find('message', work_id=work_id)
                if m.get('kind') == 'delivery' and m.get('fast_accept')]
        self.assertEqual(fast, [])

    def test_live_user_turn_blocks_fast_accept(self):
        # Safety: a live user/queued turn on the Work means the user may have
        # interrupted; fast-accept must NOT flip the Work terminal and strand it.
        # Here the launch turn is left queued (not cleared), so even with ACCEPT the
        # result must go to the inbox for the planner, not a direct delivery.
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print("42")']
        self.app = Service(self.cfg, self.root.parent / 'data'); self.app.probe('codex-local')
        self.app.task_result_gate.gate._call = lambda *a, **k: {
            'disposition': 'ACCEPT', 'confidence': 1.0, 'model': 'jev', 'latency_ms': 1}
        msg = self.app.send('bot-main', {'text': 'q'}, 'faq')
        res = self.app.submit_task('bot-main', msg['work_id'], {'prompt': 'q'})
        work_id = self.app.store.get('task', res['task_id'])['work_id']
        # deliberately DO NOT clear the queued launch turn
        for _ in range(200):
            self.app.task_step()
            t = self.app.store.get('task', res['task_id'])
            if t['status'] in ('succeeded', 'failed', 'unknown', 'cancelled'): break
            time.sleep(.05)
        self.assertEqual(t['status'], 'succeeded')
        work = self.app.store.get('work', work_id)
        self.assertNotEqual(work['status'], 'delivered') # blocked by the live turn
        fast = [m for m in self.app.store.find('message', work_id=work_id)
                if m.get('kind') == 'delivery' and m.get('fast_accept')]
        self.assertEqual(fast, [])


class AdapterCatalogTests(unittest.TestCase):
    """adapter is validated against the extensible ADAPTER_CATALOG, not a frozen
    literal set: an off-catalog adapter is rejected; adding a catalog row makes a
    brand-new adapter valid with NO other code change (SIS: argv still comes from
    the catalog, never from a model). Constructs Execution directly (no Service /
    vault) so a rejected build leaks no open DB handle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, adapter):
        return {"hosts": [{"id": "local", "transport": "local", "os": "Linux",
                           "allowed_roots": [str(self.root)]}],
                "installations": [{"id": "x-local", "host_id": "local", "adapter": adapter,
                                   "argv": [sys.executable], "cwd": str(self.root), "risk": "write"}]}

    def test_off_catalog_adapter_rejected(self):
        from pangpang.execution import Execution
        with self.assertRaises(Fault) as cm:
            Execution(self._cfg("bogus-cli"), self.root.parent / "data")
        self.assertEqual(cm.exception.code, "invalid_adapter")

    def test_catalog_row_makes_new_adapter_valid(self):
        from pangpang import execution
        from pangpang.execution import Execution
        execution.ADAPTER_CATALOG["brand-new-cli"] = {
            "binary": "brand-new-cli", "argv": ["-p"], "concurrency": 1, "priority": 40}
        try:
            ex = Execution(self._cfg("brand-new-cli"), self.root.parent / "data") # must not raise
            self.assertEqual(ex.installations["x-local"]["adapter"], "brand-new-cli")
            # catalog default priority (40) is what routing would fall back to.
            self.assertEqual(execution.ADAPTER_CATALOG["brand-new-cli"]["priority"], 40)
        finally:
            execution.ADAPTER_CATALOG.pop("brand-new-cli", None)

class CopyIntegrityTests(unittest.TestCase):
    # R7: copy() must recompute the hash from the actual decoded bytes BEFORE
    # sending them to the target, so a compromised source returning
    # "old digest + tampered bytes" can never land a tampered file.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / "ws"; self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _exec(self):
        from pangpang.execution import Execution
        cfg = {"hosts": [{"id": "src", "transport": "local", "os": "Linux", "allowed_roots": [str(self.root)]},
                         {"id": "dst", "transport": "local", "os": "Linux", "allowed_roots": [str(self.root)]}],
               "installations": [], "resources": []}
        return Execution(cfg, self.root.parent / "data")

    def test_tampered_bytes_rejected_before_target_write(self):
        import base64, hashlib
        from pangpang.store import Fault
        ex = self._exec()
        good = b"trusted artifact content"
        digest = hashlib.sha256(good).hexdigest()
        artifact = {"id": "a1", "host_id": "src", "path": str(self.root / "orig.txt"), "digest": digest}
        writes = []
        # source returns the ORIGINAL digest field but TAMPERED bytes.
        ex.read_artifact = lambda a: {"digest": digest, "data": base64.b64encode(b"EVIL tampered bytes").decode()}
        ex.inspect_file = lambda *a, **k: writes.append(a) or {"digest": "whatever"}
        with self.assertRaises(Fault) as cm:
            ex.copy(artifact, "dst", str(self.root / "dest.txt"))
        self.assertEqual(cm.exception.code, "source_changed")
        self.assertEqual(writes, [], "target write must NOT happen for tampered bytes")

    def test_honest_bytes_pass(self):
        import base64, hashlib
        ex = self._exec()
        good = b"trusted artifact content"
        digest = hashlib.sha256(good).hexdigest()
        artifact = {"id": "a1", "host_id": "src", "path": str(self.root / "orig.txt"), "digest": digest}
        ex.read_artifact = lambda a: {"digest": digest, "data": base64.b64encode(good).decode()}
        ex.inspect_file = lambda *a, **k: {"digest": digest}
        self.assertEqual(ex.copy(artifact, "dst", str(self.root / "dest.txt"))["digest"], digest)


class SupervisionGateTests(unittest.TestCase):
    """Strong-supervision allowlist. Only an executor whose probed
    supervision can PROVE whole-tree termination may be routed/admitted/started."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()
        self.app = Service(config(self.root), self.root.parent / 'data')
    def tearDown(self):
        self.app.store.close(); self.tmp.cleanup()

    def _work(self):
        return self.app.store.get('work', self.app.send('bot-main', {'text': 'x', 'allow_write': True}, 'sg')['work_id'])

    def test_weak_supervision_rejected_at_route(self):
        # An available executor whose supervision is a bare POSIX process group (cannot
        # contain a setsid() escapee) is rejected — not with an implementation term but
        # the stable business error.
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'posix-process-group'})
        route = self.app.route('bot-main', self._work(), {})
        self.assertIsNone(route['selected'])
        self.assertEqual(route['rejected'][0]['reason'], 'platform_supervision_unverified')
        try:
            self.app.submit_task('bot-main', self._work()['id'], {'prompt': 'x', 'installation_id': 'codex-local'})
            self.fail('should raise')
        except Fault as e:
            # Installed but cleanup capability insufficient — a stable business
            # fact distinct from "not installed", no impl leak.
            self.assertEqual(e.code, 'executor_cleanup_unsupported')

    def test_strong_supervision_admitted(self):
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'windows-job-object'})
        route = self.app.route('bot-main', self._work(), {})
        self.assertEqual(route['selected']['installation_id'], 'codex-local')

    def test_unavailable_not_masked_by_supervision(self):
        # discovery_failed (the more fundamental fact) is never overwritten by the
        # supervision check.
        self.app.store.put('probe', {'id': 'codex-local', 'available': False, 'reason': 'binary_not_found'})
        route = self.app.route('bot-main', self._work(), {})
        self.assertEqual(route['rejected'][0]['reason'], 'discovery_failed')

    def test_queued_weak_task_fails_terminally_not_stuck(self):
        # A task queued under the OLD code (strong executor) whose probe later becomes
        # weak must not sit queued forever — it fails with the stable error so the join
        # settles and the owner can replan.
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'windows-job-object'})
        res = self.app.submit_task('bot-main', self._work()['id'], {'prompt': 'x', 'installation_id': 'codex-local'})
        task = self.app.store.get('task', res['task_id'])
        self.assertEqual(task['status'], 'queued')
        # Simulate the executor becoming weak-supervision (e.g. moved to a POSIX host).
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'posix-process-group'})
        self.app._admit_queued_tasks(time.time())
        task = self.app.store.get('task', res['task_id'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'executor_cleanup_unsupported')

    def test_linux_cgroup_cooperative_admitted_under_L0(self):
        # The Linux systemd/cgroup cooperative tier passes route ONLY because the
        # operator explicitly declared deployment_level L0.
        app = Service(config(self.root), self.root.parent / 'data2', deployment_level='L0')
        try:
            app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'linux-cgroup-cooperative'})
            work = app.store.get('work', app.send('bot-main', {'text': 'x', 'allow_write': True}, 'l0')['work_id'])
            route = app.route('bot-main', work, {})
            self.assertEqual(route['selected']['installation_id'], 'codex-local')
        finally:
            app.store.close()

    def test_undeclared_level_is_not_L0(self):
        # an omitted declaration is "undeclared", never an implicit L0.
        # Strong supervision still runs; every L0 cooperative tier is refused.
        self.app.store.close()
        self.app = Service(config(self.root), self.root.parent / 'data-undeclared', deployment_level=None)
        self.assertIsNone(self.app.deployment_level)
        for label in ('linux-cgroup-cooperative', 'posix-pgroup-cooperative'):
            self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': label})
            route = self.app.route('bot-main', self._work(), {})
            self.assertIsNone(route['selected'], label)
            self.assertEqual(route['rejected'][0]['reason'], 'platform_supervision_unverified')
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'windows-job-object'})
        self.assertEqual(self.app.route('bot-main', self._work(), {})['selected']['installation_id'], 'codex-local')

    def test_invalid_level_is_a_config_error(self):
        # Never a silent fallback to L0.
        with self.assertRaises(Fault) as caught:
            Service(config(self.root), self.root.parent / 'data3', deployment_level='l0')
        self.assertEqual(caught.exception.code, 'invalid_deployment_level')

    def test_pgroup_cooperative_admitted_only_under_L0(self):
        from pangpang import consent
        cfg = config(self.root)
        cfg['hosts'][0][consent.CONSENT_KEY] = consent.RISK_VERSION
        for level, admitted in (('L0', True), ('L1', False), ('L2', False)):
            app = Service(cfg, self.root.parent / ('data-' + level), deployment_level=level)
            try:
                app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'posix-pgroup-cooperative'})
                work = app.store.get('work', app.send('bot-main', {'text': 'x', 'allow_write': True}, level)['work_id'])
                self.assertEqual(app.route('bot-main', work, {})['selected'] is not None, admitted, level)
                self.assertEqual(app._start_denied({'host_id': 'local', 'installation_id': 'codex-local'}) is None,
                                 admitted, level)
            finally:
                app.store.close()

    def test_linux_cgroup_cooperative_rejected_under_L1(self):
        # Same probe, stricter deployment: the cooperative tier is NOT adversarial
        # isolation, so L1/L2 refuse it (only unconditional strong supervision passes).
        app = Service(config(self.root), self.root.parent / 'data2', deployment_level='L1')
        try:
            app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'linux-cgroup-cooperative'})
            work = app.store.get('work', app.send('bot-main', {'text': 'x', 'allow_write': True}, 'l1')['work_id'])
            route = app.route('bot-main', work, {})
            self.assertIsNone(route['selected'])
            self.assertEqual(route['rejected'][0]['reason'], 'platform_supervision_unverified')
        finally:
            app.store.close()

    def test_macos_stub_always_rejected(self):
        # macOS post-verify stub is defined but never routable this phase — a
        # process-group + post-check cannot prove an unrecorded setsid child is gone.
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'macos-pgroup-postverify'})
        route = self.app.route('bot-main', self._work(), {})
        self.assertIsNone(route['selected'])
        self.assertEqual(route['rejected'][0]['reason'], 'platform_supervision_unverified')

    def test_supervision_predicate_central(self):
        # The single source of truth used by route/admission/start.
        from pangpang.service import supervision_ok
        self.assertTrue(supervision_ok('windows-job-object', 'L0'))
        self.assertTrue(supervision_ok('windows-job-object', 'L2')) # strong: any level
        self.assertTrue(supervision_ok('linux-cgroup-cooperative', 'L0'))
        self.assertFalse(supervision_ok('linux-cgroup-cooperative', 'L1'))
        self.assertFalse(supervision_ok('linux-cgroup-cooperative', 'L2'))
        self.assertFalse(supervision_ok('linux-cgroup-cooperative', None))
        self.assertTrue(supervision_ok('posix-pgroup-cooperative', 'L0'))
        self.assertFalse(supervision_ok('posix-pgroup-cooperative', 'L1'))
        self.assertFalse(supervision_ok('posix-pgroup-cooperative', None))
        self.assertTrue(supervision_ok('windows-job-object', None))
        self.assertFalse(supervision_ok('posix-process-group', 'L0'))
        self.assertFalse(supervision_ok('macos-pgroup-postverify', 'L0'))
        self.assertFalse(supervision_ok(None, 'L0'))


class ConnectorExecutionLevelTests(unittest.TestCase):
    """: a personal/LAN control-only cloud (deployment_level=None) must not keep
    a paired connector whose device proves a cooperative tier hard-rejected. The
    connector is governed by its own server-authored host.execution_level, which
    OVERRIDES the coordinator level when set and INHERITS it when absent. A device
    self-report can never write that field."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()
        self._apps = []

    def tearDown(self):
        for app in self._apps:
            app.store.close()
        self.tmp.cleanup()

    def _cloud(self, *, deployment_level=None, execution_level="__absent__"):
        cfg = config(self.root)
        host = cfg['hosts'][0]
        host.update(transport='connector', os='Linux')
        # consent.CONSENT_KEY on a connector host is irrelevant to the cloud gate;
        # leave it (harmless) so the fixture mirrors the shared config().
        if execution_level != "__absent__":
            host['execution_level'] = execution_level
        app = Service(cfg, self.root.parent / ('d-' + str(id(cfg))), deployment_level=deployment_level)
        self._apps.append(app)
        # A paired connector is online when its heartbeat is recent and the device
        # row is not revoked; make it so, else route() reports connector_offline
        # (the more fundamental fact) and masks the supervision reason under test.
        app.store.put('device', {'id': 'local', 'instance_id': 'device-' + '0' * 32})
        app.broker._last_seen['local'] = time.time()
        return app

    def _route_reason(self, app, label):
        app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': label})
        work = app.store.get('work', app.send('bot-main', {'text': 'x', 'allow_write': True},
                                              label + str(id(app)))['work_id'])
        route = app.route('bot-main', work, {})
        return route['selected'], (route['rejected'][0]['reason'] if route['rejected'] else None)

    def test_old_behaviour_connector_rejected_when_cloud_undeclared(self):
        # BEFORE the fix a control-only+origin cloud is deployment_level=None, so a
        # Linux connector's cooperative label was rejected. With no execution_level set
        # it still inherits None → still rejected. This is the state observed.
        app = self._cloud(deployment_level=None)
        for label in ('linux-cgroup-cooperative', 'posix-pgroup-cooperative'):
            selected, reason = self._route_reason(app, label)
            self.assertIsNone(selected, label)
            self.assertEqual(reason, 'platform_supervision_unverified', label)

    def test_connector_execution_level_L0_admits_without_cloud_deployment_level(self):
        # THE fix: operator opens ONE connector with execution_level=L0; the cloud
        # stays deployment_level=None. The device-proven cooperative tiers now route.
        app = self._cloud(deployment_level=None, execution_level='L0')
        for label in ('linux-cgroup-cooperative', 'posix-pgroup-cooperative'):
            selected, _ = self._route_reason(app, label)
            self.assertIsNotNone(selected, label)
            self.assertEqual(selected['installation_id'], 'codex-local', label)

    def test_connector_inherits_cloud_L0_when_level_absent(self):
        # Pre- contract preserved: an explicit --deployment-level L0 on the cloud
        # already admitted its connectors; absence of a per-connector field inherits it.
        app = self._cloud(deployment_level='L0')
        selected, _ = self._route_reason(app, 'linux-cgroup-cooperative')
        self.assertIsNotNone(selected)

    def test_connector_L1_overrides_cloud_L0_rejecting_cooperative(self):
        # A per-connector L1 overrides a permissive cloud L0 the strict way: only
        # unconditional strong supervision stays eligible on that device.
        app = self._cloud(deployment_level='L0', execution_level='L1')
        selected, reason = self._route_reason(app, 'linux-cgroup-cooperative')
        self.assertIsNone(selected)
        self.assertEqual(reason, 'platform_supervision_unverified')
        selected, _ = self._route_reason(app, 'windows-job-object')
        self.assertIsNotNone(selected)

    def test_both_absent_fail_closed(self):
        # Neither side declares L0 → cooperative stays rejected (fail-closed default).
        app = self._cloud(deployment_level=None)
        selected, reason = self._route_reason(app, 'posix-pgroup-cooperative')
        self.assertIsNone(selected)
        self.assertEqual(reason, 'platform_supervision_unverified')

    def test_strong_supervision_connector_always_admitted(self):
        # Windows Job Object connector routes regardless of either level (unchanged;
        # this is the Windows path that already PASSed).
        app = self._cloud(deployment_level=None)
        selected, _ = self._route_reason(app, 'windows-job-object')
        self.assertIsNotNone(selected)

    def test_set_host_execution_level_is_operator_only_and_connector_only(self):
        # The field is writable ONLY through the operator-authenticated service method;
        # a device report cannot reach it. Validate inputs + connector-only.
        app = self._cloud(deployment_level=None)
        out = app.set_host_execution_level('local', {'level': 'L0'})
        self.assertEqual(out, {'host_id': 'local', 'execution_level': 'L0'})
        self.assertEqual(app.execution.hosts['local'].get('execution_level'), 'L0')
        selected, _ = self._route_reason(app, 'linux-cgroup-cooperative')
        self.assertIsNotNone(selected)
        # null clears the override (restores inheritance), not a magic value.
        app.set_host_execution_level('local', {'level': None})
        self.assertNotIn('execution_level', app.execution.hosts['local'])
        with self.assertRaises(Fault) as bad:
            app.set_host_execution_level('local', {'level': 'L9'})
        self.assertEqual(bad.exception.code, 'invalid_execution_level')
        with self.assertRaises(Fault) as missing:
            app.set_host_execution_level('nope', {'level': 'L0'})
        self.assertEqual(missing.exception.code, 'host_not_found')

    def test_execution_level_refused_on_local_host(self):
        # local/ssh isolation is the coordinator's --deployment-level; a per-host
        # override would let the two paths diverge, so it is refused.
        cfg = config(self.root) # host 'local' stays transport=local
        app = Service(cfg, self.root.parent / 'd-local', deployment_level=None)
        self._apps.append(app)
        with self.assertRaises(Fault) as caught:
            app.set_host_execution_level('local', {'level': 'L0'})
        self.assertEqual(caught.exception.code, 'execution_level_connector_only')

    def test_empty_or_typo_body_never_clears_an_override(self):
        # Codex P2: body.get('level') made {} (or a misspelled key) look like a
        # deliberate null and silently remove a stricter L1/L2 override, dropping the
        # device to the inherited (possibly L0) level and admitting cooperative
        # supervision. The 'level' key is REQUIRED; only {"level": null} clears.
        app = self._cloud(deployment_level=None)
        app.set_host_execution_level('local', {'level': 'L1'})
        self.assertEqual(app.execution.hosts['local'].get('execution_level'), 'L1')
        for bad_body in ({}, {'lvl': 'L0'}, {'Level': None}):
            with self.assertRaises(Fault) as caught:
                app.set_host_execution_level('local', bad_body)
            self.assertEqual(caught.exception.code, 'execution_level_required', bad_body)
            # the stricter override must survive the rejected request untouched
            self.assertEqual(app.execution.hosts['local'].get('execution_level'), 'L1', bad_body)
        # an explicit null is still the one way to clear (restores inheritance)
        app.set_host_execution_level('local', {'level': None})
        self.assertNotIn('execution_level', app.execution.hosts['local'])

    def test_blocked_connector_on_L1_coordinator_still_points_to_execution_level(self):
        # Codex P2: on an L1/L2 coordinator the old ordering returned the 'strong
        # supervision only' hint before the connector branch, hiding the real recovery.
        # A per-device execution_level=L0 OVERRIDES the coordinator level, so that
        # recovery exists even here and the hint must name it.
        app = self._cloud(deployment_level='L1')
        selected, reason = self._route_reason(app, 'linux-cgroup-cooperative')
        self.assertIsNone(selected)
        self.assertEqual(reason, 'platform_supervision_unverified')
        route = app.route('bot-main', app.store.get(
            'work', app.send('bot-main', {'text': 'x', 'allow_write': True},
                             'hint' + str(id(app)))['work_id']), {})
        code, hint = app._supervision_unverified_action(route['rejected'])
        self.assertEqual(code, 'set_host_execution_level')
        self.assertIn('execution_level=L0', hint)
        # setting that device L0 then actually admits it on the same L1 coordinator.
        app.set_host_execution_level('local', {'level': 'L0'})
        selected, _ = self._route_reason(app, 'linux-cgroup-cooperative')
        self.assertIsNotNone(selected)


class ControlOnlyTests(unittest.TestCase):
    """Control-only coordinator never runs a local executor (SECURITY L1),
    as a RUNTIME hard gate — even a local host already in the DB is neutralised."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()
        self.app = Service(config(self.root), self.root.parent / 'data', control_only=True)
        # A fully valid local executor (strong supervision) — the ONLY thing stopping
        # it is control-only mode.
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'windows-job-object'})
    def tearDown(self):
        self.app.store.close(); self.tmp.cleanup()
    def _work(self):
        return self.app.store.get('work', self.app.send('bot-main', {'text': 'x', 'allow_write': True}, 'co')['work_id'])

    def test_local_rejected_at_route(self):
        route = self.app.route('bot-main', self._work(), {})
        self.assertIsNone(route['selected'])
        self.assertEqual(route['rejected'][0]['reason'], 'control_only_no_local_executor')

    def test_submit_local_raises_stable_error(self):
        try:
            self.app.submit_task('bot-main', self._work()['id'], {'prompt': 'x', 'installation_id': 'codex-local'})
            self.fail('should raise')
        except Fault as e:
            self.assertEqual(e.code, 'requested_executor_not_available')

    def test_queued_local_task_fails_terminally(self):
        # Flip control_only OFF to queue a local task under the old regime, then ON to
        # prove the admission gate terminates it (existing-task migration net).
        self.app.control_only = False
        res = self.app.submit_task('bot-main', self._work()['id'], {'prompt': 'x', 'installation_id': 'codex-local'})
        self.app.control_only = True
        self.app._admit_queued_tasks(time.time())
        task = self.app.store.get('task', res['task_id'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'requested_executor_not_available')

    def test_recover_quarantines_running_local_attempt(self):
        # A local task left 'running' from a previous (non-control) boot must be
        # quarantined (unknown) AND its leftover process signalled to stop (
        # marking unknown alone would leave a physical process running).
        self.app.control_only = False
        res = self.app.submit_task('bot-main', self._work()['id'], {'prompt': 'x', 'installation_id': 'codex-local'})
        t = self.app.store.get('task', res['task_id'])
        t.update(status='running', supervisor={'supervisor_pid': 99999}, started=time.time())
        self.app.store.put('task', t)
        self.app.control_only = True
        cancels = []
        self.app.execution.cancel = lambda task: cancels.append(task['id'])
        self.app._recover_task(res['task_id'])
        self.assertEqual(self.app.store.get('task', res['task_id'])['status'], 'unknown')
        self.assertIn(res['task_id'], cancels, "leftover local process must be cancelled, not just relabelled")

    def test_denied_starting_with_probe_error_quarantines_not_fails(self):
        # (RCB): a denied 'starting' task whose launch-trace probe cannot be observed
        # must go to unknown (quarantine), NEVER terminal-failed+settled — observation
        # unavailable is not proof of never-started.
        self.app.control_only = False
        res = self.app.submit_task('bot-main', self._work()['id'], {'prompt': 'x', 'installation_id': 'codex-local'})
        t = self.app.store.get('task', res['task_id'])
        t.update(status='starting', started=time.time(), attempt_uuid='att')
        self.app.store.put('task', t)
        self.app.control_only = True # now denied
        self.app._launch_trace_definitely_absent = lambda directory: False # probe unavailable
        self.app._deny_starting_task(t, 'requested_executor_not_available')
        cur = self.app.store.get('task', res['task_id'])
        self.assertEqual(cur['status'], 'unknown')
        self.assertNotEqual(cur.get('error'), 'requested_executor_not_available') # not settled as failure

    def test_launch_trace_probe_layer_distinguishes_errno(self):
        # real probe layer: os.lstat errno decides. ELOOP/EACCES (non-absence) ->
        # NOT definitely-absent (observation unavailable); ENOENT -> definitely absent.
        import os as _os, errno as _errno
        from pathlib import Path as _P
        orig = _os.lstat
        try:
            _os.lstat = lambda p: (_ for _ in ()).throw(OSError(_errno.ELOOP, "loop"))
            self.assertFalse(self.app._launch_trace_definitely_absent(_P("/whatever")),
                             "ELOOP is observation-unavailable, not proof of absence")
            _os.lstat = lambda p: (_ for _ in ()).throw(OSError(_errno.EACCES, "denied"))
            self.assertFalse(self.app._launch_trace_definitely_absent(_P("/whatever")),
                             "EACCES is observation-unavailable, not proof of absence")
            _os.lstat = lambda p: (_ for _ in ()).throw(OSError(_errno.ENOENT, "nope"))
            self.assertTrue(self.app._launch_trace_definitely_absent(_P("/whatever")),
                            "ENOENT proves the run directory is absent")
        finally:
            _os.lstat = orig

    def test_denied_starting_definitely_never_started_fails(self):
        # The other branch: filesystem DEFINITIVELY shows no trace -> terminal fail is
        # correct (nothing ran, so no side effect to mask).
        self.app.control_only = False
        res = self.app.submit_task('bot-main', self._work()['id'], {'prompt': 'x', 'installation_id': 'codex-local'})
        t = self.app.store.get('task', res['task_id'])
        t.update(status='starting', started=time.time(), attempt_uuid='att')
        self.app.store.put('task', t)
        self.app.control_only = True
        self.app._launch_trace_definitely_absent = lambda directory: True # proven absent
        self.app._deny_starting_task(t, 'requested_executor_not_available')
        cur = self.app.store.get('task', res['task_id'])
        self.assertEqual(cur['status'], 'failed')
        self.assertEqual(cur['error'], 'requested_executor_not_available')


class AttemptEvidenceTests(unittest.TestCase):
    """Five-state attempt-liveness evidence + deadline. A reused/foreign pid,
    an identity-unavailable pid past deadline, and an overran SAME must all quarantine;
    the model only ever sees the stable business error."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()
        self.app = Service(config(self.root), self.root.parent / 'data')
        self.app.store.put('probe', {'id': 'codex-local', 'available': True, 'supervision': 'windows-job-object'})
    def tearDown(self):
        self.app.store.close(); self.tmp.cleanup()

    def _running_local_task(self, started_offset=0.0):
        import time as _t
        m = self.app.send('bot-main', {'text': 'x', 'allow_write': True}, 'ev')
        r = self.app.submit_task('bot-main', m['work_id'], {'prompt': 'x', 'installation_id': 'codex-local'})
        t = self.app.store.get('task', r['task_id'])
        t.update(status='running', supervisor={'supervisor_pid': 4321},
                 attempt_uuid='att', started=_t.time() + started_offset)
        self.app.store.put('task', t)
        return t

    def _patch(self, alive, cmdline, started_at):
        from pangpang import runner
        runner.process_alive = staticmethod(lambda pid: alive)
        runner.process_cmdline = staticmethod(lambda pid: cmdline)
        runner.process_started_at = staticmethod(lambda pid: started_at)

    def test_foreign_pid_quarantines_now(self):
        from pangpang import runner
        orig = (runner.process_alive, runner.process_cmdline, runner.process_started_at)
        try:
            t = self._running_local_task()
            # pid exists, cmdline is a DIFFERENT process (reused pid): FOREIGN -> unknown.
            self._patch(True, ['python', 'something', 'run', '/other/dir'], None)
            self.app._local_attempt_state(t)
            cur = self.app.store.get('task', t['id'])
            self.assertEqual(cur['status'], 'unknown')
            self.assertEqual(cur['unknown_reason'], 'attempt_pid_foreign')
            self.assertEqual(cur['error'], 'execution_state_unknown') # stable, no leak
        finally:
            runner.process_alive, runner.process_cmdline, runner.process_started_at = orig

    def test_identity_unavailable_past_deadline_quarantines(self):
        from pangpang import runner
        orig = (runner.process_alive, runner.process_cmdline, runner.process_started_at)
        try:
            # started far in the past so it is past timeout_s+grace; macOS-like: no
            # cmdline, no start-time -> IDENTITY_UNAVAILABLE -> deadline -> unknown.
            t = self._running_local_task(started_offset=-999999)
            self._patch(True, None, None)
            self.app._local_attempt_state(t)
            cur = self.app.store.get('task', t['id'])
            self.assertEqual(cur['status'], 'unknown')
            self.assertEqual(cur['unknown_reason'], 'execution_state_unknown')
        finally:
            runner.process_alive, runner.process_cmdline, runner.process_started_at = orig

    def test_identity_unavailable_within_deadline_stays_running(self):
        from pangpang import runner
        orig = (runner.process_alive, runner.process_cmdline, runner.process_started_at)
        try:
            t = self._running_local_task() # started now: within deadline
            self._patch(True, None, None)
            self.app._local_attempt_state(t)
            self.assertEqual(self.app.store.get('task', t['id'])['status'], 'running')
        finally:
            runner.process_alive, runner.process_cmdline, runner.process_started_at = orig

    def test_same_overran_deadline_quarantines(self):
        from pangpang import runner
        orig = (runner.process_alive, runner.process_cmdline, runner.process_started_at)
        try:
            # SAME (start-time matches) but overran timeout_s+grace: must not be an
            # unbounded free pass -> quarantine.
            import time as _t
            t = self._running_local_task(started_offset=-999999)
            self._patch(True, None, _t.time() - 999999) # start-time in the (old) window
            self.app._local_attempt_state(t)
            cur = self.app.store.get('task', t['id'])
            self.assertEqual(cur['status'], 'unknown')
            self.assertEqual(cur['unknown_reason'], 'execution_overran_deadline')
        finally:
            runner.process_alive, runner.process_cmdline, runner.process_started_at = orig

    def test_unknown_task_with_live_same_leftover_is_cancelled_once(self):
        # a task already 'unknown' whose SAME supervisor is still alive past the
        # deadline (a leftover from an earlier denial) must be cancelled once — not left
        # running forever. _apply_observation's unknown-reconcile branch drives it.
        from pangpang import runner
        orig = (runner.process_alive, runner.process_cmdline, runner.process_started_at)
        try:
            import time as _t
            t = self._running_local_task(started_offset=-999999)
            t = self.app.store.get('task', t['id']); t['status'] = 'unknown'
            self.app.store.put('task', t)
            self._patch(True, None, _t.time() - 999999) # SAME via start-time window, past deadline
            cancels = []
            self.app.execution.cancel = lambda task: cancels.append(task['id'])
            # A non-terminal observation on the unknown task triggers the leftover cancel.
            self.app._apply_observation(t, {"status": "running"})
            self.assertEqual(cancels, [t['id']])
            # Idempotent: a second reconcile must NOT re-cancel.
            self.app._apply_observation(self.app.store.get('task', t['id']), {"status": "running"})
            self.assertEqual(cancels, [t['id']], "leftover cancel must be one-shot")
        finally:
            runner.process_alive, runner.process_cmdline, runner.process_started_at = orig


class SshHostOsTests(unittest.TestCase):
    """An ssh host's OS is load-bearing (command construction + path case),
    so a blank/unknown OS must be rejected at config validation, not defaulted to POSIX."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()
    def tearDown(self):
        self.tmp.cleanup()

    def _cfg_with_ssh(self, os_value):
        cfg = config(self.root)
        cfg['hosts'].append({'id': 'ssh-h', 'transport': 'ssh', 'target': 'user@host',
                             'runner_root': '/opt/runner', 'os': os_value, 'allowed_roots': ['/opt/runner']})
        return cfg

    def test_ssh_host_without_os_rejected(self):
        from pangpang.execution import Execution
        with self.assertRaises(Fault) as cm:
            Execution(self._cfg_with_ssh(''), self.root.parent / 'd')
        self.assertEqual(cm.exception.code, 'ssh_host_os_required')

    def test_ssh_host_with_known_os_accepted(self):
        from pangpang.execution import Execution
        Execution(self._cfg_with_ssh('linux'), self.root.parent / 'd2') # no raise


class LaunchContractVerifyTests(unittest.TestCase):
    """The device-side single-entry contract gate. Whole-contract signature
    + explicit schema + payload bind, with inputs compared CANONICALLY."""
    def _task(self, **over):
        from pangpang.store import launch_contract, digest
        task = {"execution_id": "exec-" + "a" * 32, "installation_id": "codex-local",
                "cwd": "/repo", "timeout_s": 900, "access_mode": "write",
                "workspace_key": ["h", ["repo"]], "inputs": [], "outputs": ["out.txt"],
                "resource_keys": [], "prompt": "hi", "attempt_uuid": "att",
                "policy_digest": "pd"}
        task.update(over)
        task["contract"] = launch_contract(task)
        task["contract_digest"] = digest(task["contract"])
        return task

    def test_valid_contract_passes(self):
        from pangpang.store import verify_launch_contract
        self.assertIsNone(verify_launch_contract(self._task()))

    def test_artifact_input_not_spuriously_rejected(self):
        # regression: a real connector task's top-level input carries artifact_id;
        # the contract only keeps path/digest. Canonical comparison must NOT reject it.
        from pangpang.store import verify_launch_contract
        task = self._task(inputs=[{"artifact_id": "art-1", "path": "/repo/in.txt", "digest": "d1"}])
        self.assertIsNone(verify_launch_contract(task), "artifact_id on top-level input must not mismatch")

    def test_deleted_field_resigned_rejected(self):
        # deleting a field and re-signing the shrunk contract must still fail —
        # the schema check catches it even though the (recomputed) digest matches.
        from pangpang.store import verify_launch_contract, digest
        task = self._task()
        del task["contract"]["prompt"]
        task["contract_digest"] = digest(task["contract"]) # re-sign the shrunk contract
        self.assertEqual(verify_launch_contract(task), "local_contract_schema_invalid")

    def test_wrong_version_rejected(self):
        from pangpang.store import verify_launch_contract, digest
        task = self._task()
        task["contract"]["contract_version"] = 999
        task["contract_digest"] = digest(task["contract"])
        self.assertEqual(verify_launch_contract(task), "local_contract_version_unsupported")

    def test_stale_digest_rejected(self):
        from pangpang.store import verify_launch_contract
        task = self._task()
        task["contract"]["timeout_s"] = 999999 # tamper, digest stays old
        self.assertEqual(verify_launch_contract(task), "local_contract_digest_mismatch")

    def test_split_payload_top_level_altered_rejected(self):
        from pangpang.store import verify_launch_contract
        task = self._task()
        task["prompt"] = "EVIL" # top-level altered; contract.prompt still original+signed
        self.assertEqual(verify_launch_contract(task), "local_contract_payload_mismatch")

    def test_downgrade_stripped_contract_with_marker_rejected(self):
        # dropping the `contract` field but leaving the top-level
        # min_contract_version marker must be REJECTED, not fall through to legacy.
        from pangpang.store import verify_launch_contract
        task = self._task()
        task["min_contract_version"] = 2 # broker.start sends this sibling marker
        del task["contract"]; del task["contract_digest"]
        self.assertEqual(verify_launch_contract(task), "local_contract_required")

    def test_full_downgrade_rejected_by_device_capability(self):
        # full downgrade: delete contract + digest + marker ALL. A v2 device
        # (its own compiled-in capability, not the mutable payload) still requires a
        # contract — the stripped payload must NOT slip through as legacy.
        from pangpang.store import verify_launch_contract
        task = self._task()
        del task["contract"]; del task["contract_digest"]
        task.pop("min_contract_version", None)
        # default device_contract_version == CONTRACT_VERSION (2)
        self.assertEqual(verify_launch_contract(task), "local_contract_required")

    def test_genuine_legacy_device_passes_through(self):
        # A TRUE legacy device (capability 0) with no contract/marker takes legacy path.
        from pangpang.store import verify_launch_contract
        self.assertIsNone(verify_launch_contract({"execution_id": "e", "prompt": "x"},
                                                 device_contract_version=0))


class ControlSeparationTests(unittest.TestCase):
    """Executor workspace roots must not overlap the control-plane dir (runner.check_control_separation)."""
    def sep(self, roots, control):
        from pangpang.runner import check_control_separation
        check_control_separation(roots, control)

    def test_ancestor_workspace_rejected(self):
        # Regression for the browser fixture: allowed_root == whole temp root while
        # the connector's control data lives at root/device → ancestor overlap.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                self.sep([str(root)], [str(root / "device")])

    def test_descendant_workspace_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "sub").mkdir()
            with self.assertRaises(ValueError):
                self.sep([str(root / "sub")], [str(root)])

    def test_equal_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                self.sep([str(root)], [str(root)])

    def test_sibling_workspace_allowed(self):
        # The fixed fixture layout: root/workspace vs root/device do not overlap.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "workspace").mkdir(); (root / "device").mkdir()
            self.sep([str(root / "workspace")], [str(root / "device")]) # no raise


def _ssh_host():
    return {"id": "ssh-h", "transport": "ssh", "os": "linux", "target": "user@host",
            "runner_root": "/opt/pangpang-runner", "python": "python3",
            "allowed_roots": ["/opt/work"]}


def _with_ssh(root, *, ssh_only=False):
    cfg = config(root)
    host = _ssh_host()
    inst = {"id": "codex-ssh-h", "host_id": "ssh-h", "adapter": "codex",
            "argv": [sys.executable, "-c", "print(1)"], "cwd": "/opt/work",
            "risk": "write", "priority": 1}
    if ssh_only:
        cfg["hosts"] = [host]
        cfg["installations"] = [inst]
    else:
        cfg["hosts"].append(host)
        cfg["installations"].append(inst)
    return cfg


class SshRetireGateTests(unittest.TestCase):
    """SSH stays loadable. New launches are refused and never retargeted.
    Unfinished rows are inventoried, not copied, settled, or rerun.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.app = Service(_with_ssh(self.root), self.root.parent / "data")
        self.app.store.put("probe", {"id": "codex-local", "available": True,
                                     "supervision": "windows-job-object"})
        self.app.store.put("probe", {"id": "codex-ssh-h", "available": True,
                                     "supervision": "windows-job-object"})

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _send(self, key):
        return self.app.send("bot-main", {"text": "task", "allow_write": True}, key)

    def test_clean_config_without_ssh_does_not_error(self):
        app = Service(config(self.root), self.root.parent / "clean-data")
        try:
            listed = app.ssh_inventory()
            self.assertEqual(listed["hosts"], [])
            self.assertEqual(listed["attempts"], [])
            self.assertEqual(listed["fallback"], "none")
            app.store.put("probe", {"id": "codex-local", "available": True,
                                    "supervision": "windows-job-object"})
            route = app.route("bot-main", {"id": "w"}, {})
            self.assertEqual(route["selected"]["host_id"], "local")
        finally:
            app.store.close()

    def test_inventory_is_read_only_and_refuses_migration(self):
        sent = self._send("inv")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "stay"})
        task = self.app.store.get("task", submitted["task_id"])
        task["host_id"] = "ssh-h"
        task["installation_id"] = "codex-ssh-h"
        task["status"] = "unknown"
        self.app.store.put("task", task)
        before = [(row["id"], row["status"], row["host_id"]) for row in self.app.store.find("task")]
        with patch.object(self.app.execution, "start") as start, \
                patch.object(self.app.execution, "prepare") as prepare:
            listed = self.app.ssh_inventory()
            again = self.app.ssh_inventory()
            start.assert_not_called()
            prepare.assert_not_called()
        self.assertEqual(listed, again)
        self.assertEqual(listed["launch"], "refused")
        self.assertEqual(listed["fallback"], "none")
        self.assertEqual(listed["hosts"][0]["host_id"], "ssh-h")
        self.assertEqual(listed["hosts"][0]["source"], "managed_config.hosts")
        self.assertEqual(listed["hosts"][0]["migration"], "refused")
        self.assertEqual(listed["hosts"][0]["migration_reason"],
                         "ssh_identity_not_equivalent_to_local_or_connector")
        self.assertEqual(listed["attempts"][0]["task_id"], task["id"])
        self.assertEqual(listed["attempts"][0]["close"], "terminal_receipt_or_operator")
        self.assertEqual(listed["attempts"][0]["side_effects"], "possible")
        self.assertFalse(listed["attempts"][0]["auto_migrate"])
        self.assertFalse(listed["attempts"][0]["auto_rerun"])
        after = [(row["id"], row["status"], row["host_id"]) for row in self.app.store.find("task")]
        self.assertEqual(before, after)
        self.assertEqual(self.app.execution.hosts["ssh-h"]["transport"], "ssh")

    def test_named_ssh_target_is_not_retargeted(self):
        sent = self._send("pin")
        with patch.object(self.app.execution, "start") as start, \
                patch("pangpang.execution.subprocess.run") as run:
            with self.assertRaises(Fault) as caught:
                self.app.submit_task("bot-main", sent["work_id"],
                                     {"prompt": "go", "installation_id": "codex-ssh-h"})
            start.assert_not_called()
            run.assert_not_called()
        self.assertEqual(caught.exception.code, "ssh_executor_retired")
        self.assertIn("不会改派", caught.exception.advice)
        self.assertEqual(self.app.store.find("task"), [])
        self.assertEqual(self.app.execution.hosts["ssh-h"]["transport"], "ssh")
        route = self.app.route("bot-main", {"id": "w"}, {"host_id": "ssh-h"})
        self.assertIsNone(route["selected"])
        ssh_row = next(row for row in route["rejected"] if row["host_id"] == "ssh-h")
        self.assertEqual(ssh_row["reason"], "ssh_transport_retired")

    def test_unconstrained_route_skips_ssh_even_when_it_sorts_first(self):
        sent = self._send("pick-local")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "local"})
        task = self.app.store.get("task", submitted["task_id"])
        self.assertEqual(task["host_id"], "local")
        self.assertEqual(task["installation_id"], "codex-local")
        self.assertEqual(self.app.execution.hosts["ssh-h"]["transport"], "ssh")

    def test_queued_ssh_attempt_fails_closed_without_launch_or_rerun(self):
        sent = self._send("queued")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "old"})
        task = self.app.store.get("task", submitted["task_id"])
        task["host_id"] = "ssh-h"
        task["installation_id"] = "codex-ssh-h"
        self.app.store.put("task", task)
        with patch.object(self.app.execution, "start") as start, \
                patch.object(self.app.execution, "prepare") as prepare:
            self.app.task_step()
            start.assert_not_called()
            prepare.assert_not_called()
        got = self.app.store.get("task", task["id"])
        self.assertEqual(got["status"], "failed")
        self.assertEqual(got["error"], "ssh_executor_retired")
        self.assertEqual(got["host_id"], "ssh-h")
        self.assertEqual(len(self.app.store.find("task")), 1)
        dep = self.app.store.get("dependency", got["dependency_id"])
        self.assertEqual(dep["status"], "settled")
        self.assertEqual(dep["outcome"], "failed")
        self.assertEqual(dep["result_payload"]["status"], "failed")
        self.assertEqual(dep["result_payload"]["error"], "ssh_executor_retired")
        listed = self.app.ssh_inventory()
        self.assertEqual(listed["attempts"], [])

    def test_starting_ssh_attempt_stays_unknown_and_is_not_rerun(self):
        sent = self._send("starting")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "maybe"})
        task = self.app.store.get("task", submitted["task_id"])
        task.update(status="starting", host_id="ssh-h", installation_id="codex-ssh-h",
                    started=time.time(), attempt_uuid="att")
        self.app.store.put("task", task)
        with patch.object(self.app.execution, "start") as start:
            self.app.task_step()
            start.assert_not_called()
        got = self.app.store.get("task", task["id"])
        self.assertEqual(got["status"], "unknown")
        self.assertEqual(got["error"], "execution_state_unknown")
        self.assertEqual(got["host_id"], "ssh-h")
        self.assertEqual(self.app.store.get("dependency", got["dependency_id"])["status"], "pending")
        self.assertEqual(len(self.app.store.find("task")), 1)
        row = self.app.ssh_inventory()["attempts"][0]
        self.assertEqual(row["close"], "terminal_receipt_or_operator")
        self.assertEqual(row["side_effects"], "possible")

    def test_running_ssh_attempt_is_observed_not_relaunched_or_completed(self):
        sent = self._send("running")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "live"})
        task = self.app.store.get("task", submitted["task_id"])
        task.update(status="running", host_id="ssh-h", installation_id="codex-ssh-h",
                    started=time.time(), attempt_uuid="att")
        self.app.store.put("task", task)
        with patch.object(self.app.execution, "start") as start, \
                patch.object(self.app.execution, "poll", return_value={"status": "running"}) as poll:
            self.app.task_step()
            start.assert_not_called()
            self.assertGreater(poll.call_count, 0)
        got = self.app.store.get("task", task["id"])
        self.assertEqual(got["status"], "running")
        self.assertNotIn("receipt", got)

    def test_unknown_ssh_attempt_is_not_auto_resolved(self):
        sent = self._send("unk")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "held"})
        task = self.app.store.get("task", submitted["task_id"])
        task.update(status="unknown", host_id="ssh-h", installation_id="codex-ssh-h",
                    error="execution_state_unknown", unknown_reason="receipt_missing",
                    started=time.time(), attempt_uuid="att")
        self.app.store.put("task", task)
        with patch.object(self.app.execution, "start") as start, \
                patch.object(self.app.execution, "prepare") as prepare, \
                patch.object(self.app.execution, "poll", return_value={"status": "running"}):
            self.app.task_step()
            start.assert_not_called()
            prepare.assert_not_called()
        got = self.app.store.get("task", task["id"])
        self.assertEqual(got["status"], "unknown")
        self.assertEqual(len(self.app.store.find("task")), 1)

    def test_removed_ssh_channel_does_not_spawn_or_settle(self):
        # Held deletion: poll/prepare/probe/cancel must not open ssh, and a
        # running attempt stays running instead of being marked succeeded.
        sent = self._send("channel")
        submitted = self.app.submit_task("bot-main", sent["work_id"], {"prompt": "live"})
        task = self.app.store.get("task", submitted["task_id"])
        task.update(status="running", host_id="ssh-h", installation_id="codex-ssh-h",
                    started=time.time(), attempt_uuid="att")
        self.app.store.put("task", task)
        with patch("pangpang.execution.subprocess.run") as run:
            self.app.task_step()
            with self.assertRaises(Fault) as prepared:
                self.app.execution.prepare(self.app.execution.hosts["ssh-h"], "/tmp/pangpang-ssh-nope")
            with self.assertRaises(Fault) as probed:
                self.app.execution.probe("codex-ssh-h")
            with self.assertRaises(Fault) as cancelled:
                self.app.execution.cancel(task)
            run.assert_not_called()
        self.assertEqual(prepared.exception.code, "ssh_transport_retired")
        self.assertEqual(probed.exception.code, "ssh_transport_retired")
        self.assertEqual(cancelled.exception.code, "ssh_transport_retired")
        got = self.app.store.get("task", task["id"])
        self.assertEqual(got["status"], "running")
        self.assertEqual(got.get("last_observation_error"), "ssh_transport_retired")
        self.assertNotIn("receipt", got)
        self.assertEqual(self.app.execution.hosts["ssh-h"]["transport"], "ssh")
        self.assertEqual(len(self.app.store.find("task")), 1)
        self.assertEqual(task_interjection_tier(
            {"adapter": "gemini"}, {"transport": "ssh", "os": "Linux"}), 3)

    def test_start_refuses_before_any_ssh_subprocess(self):
        with patch("pangpang.execution.subprocess.run") as run, \
                patch.object(self.app.execution, "prepare") as prepare:
            with self.assertRaises(Fault) as caught:
                self.app.execution.start({
                    "host_id": "ssh-h", "installation_id": "codex-ssh-h",
                    "execution_id": "exec-" + "ab" * 16})
            self.assertEqual(caught.exception.code, "ssh_transport_retired")
            run.assert_not_called()
            prepare.assert_not_called()

    def test_probe_available_is_not_execution_verified(self):
        facts = self.app.installation_execution_facts("codex-local")
        self.assertTrue(facts["discovered"])
        self.assertFalse(facts["execution_verified"])
        self.assertTrue(facts["supervision_admitted"])
        self.assertFalse(facts["os_sandbox"])
        self.assertEqual(facts["native_cli_retest"], "unverified")
        for claim in facts["claims_from_probe"].values():
            self.assertFalse(claim)
        weak = Service(_with_ssh(self.root), self.root.parent / "weak-data", deployment_level=None)
        try:
            weak.store.put("probe", {"id": "codex-local", "available": True,
                                     "supervision": "macos-pgroup-postverify"})
            weak_facts = weak.installation_execution_facts("codex-local")
            self.assertTrue(weak_facts["discovered"])
            self.assertFalse(weak_facts["execution_verified"])
            self.assertFalse(weak_facts["supervision_admitted"])
            self.assertFalse(weak_facts["os_sandbox"])
            self.assertFalse(supervision_ok("macos-pgroup-postverify", "L0"))
            self.assertFalse(supervision_ok("posix-process-group", "L0"))
            self.assertNotIn("posix-pgroup-cooperative", STRONG_SUPERVISION)
            self.assertNotIn("linux-cgroup-cooperative", STRONG_SUPERVISION)
            self.assertIn("linux-cgroup-cooperative", L0_COOPERATIVE_SUPERVISION)
            self.assertFalse(supervision_ok("linux-cgroup-cooperative", "L2"))
            self.assertTrue(supervision_ok("posix-pgroup-cooperative", "L0"))
        finally:
            weak.store.close()
        task = self.app.store.get("task", self.app.submit_task(
            "bot-main", self._send("proved")["work_id"], {"prompt": "ran"})["task_id"])
        task.update(status="succeeded", receipt={
            "status": "succeeded", "execution_id": task["execution_id"],
            "spec_digest": task["spec_digest"], "process_tree_confirmed": True})
        self.app.store.put("task", task)
        proved = self.app.installation_execution_facts("codex-local")
        self.assertTrue(proved["execution_verified"])
        task["receipt"] = {"status": "succeeded", "execution_id": task["execution_id"],
                           "spec_digest": task["spec_digest"], "process_tree_confirmed": True,
                           "not_launched": "runner_prelaunch"}
        self.app.store.put("task", task)
        self.assertFalse(self.app.installation_execution_facts("codex-local")["execution_verified"])
        caps = self.app.effective_capabilities()
        executor = next(row for row in caps if row["capability"] == "executor")
        self.assertIn("executor_evidence", executor["detail"])
        self.assertTrue(any(row["installation_id"] == "codex-local" and row["discovered"]
                            and not row["execution_verified"]
                            for row in executor["detail"]["executor_evidence"]))

    def test_ssh_spec_does_not_point_at_the_coordinator_vault(self):
        task = {"installation_id": "codex-ssh-h", "execution_id": "exec-" + "a" * 32,
                "spec_digest": "d", "prompt": "p", "inputs": []}
        spec = self.app.execution.spec(task)
        self.assertIsNone(spec["vault_path"])
        local = dict(task, installation_id="codex-local")
        local_spec = self.app.execution.spec(local)
        self.assertTrue(str(local_spec["vault_path"]).endswith("vault"))

    def test_connector_correction_stays_tier3_and_is_not_local_inject(self):
        self.assertEqual(task_interjection_tier(
            {"adapter": "claude-code"}, {"transport": "connector", "os": "Linux"}), 3)
        self.assertEqual(task_interjection_tier(
            {"adapter": "codex"}, {"transport": "connector", "os": "Linux"}), 3)
        self.app.execution.hosts["local"]["transport"] = "connector"
        with patch.object(self.app.execution, "call") as call:
            with self.assertRaises(Fault) as caught:
                self.app.execution.inject(
                    {"host_id": "local", "execution_id": "exec-" + "c" * 32}, "mid", "hello")
            self.assertEqual(caught.exception.code, "interjection_unsupported")
            call.assert_not_called()
        self.assertEqual(self.app.execution.hosts["local"]["transport"], "connector")

    def test_ssh_only_config_reports_retirement_not_a_local_fallback(self):
        app = Service(_with_ssh(self.root, ssh_only=True), self.root.parent / "ssh-only")
        try:
            app.store.put("probe", {"id": "codex-ssh-h", "available": True,
                                    "supervision": "windows-job-object"})
            entry = next(row for row in app.effective_capabilities() if row["capability"] == "executor")
            self.assertEqual(entry["state"], "blocked")
            self.assertEqual(entry["reason"], "ssh_transport_retired")
            self.assertEqual(entry["operator_action_code"], "retire_ssh_executor")
            self.assertIn("不会改派", entry["operator_action"])
            self.assertTrue(entry["detail"]["executor_evidence"][0]["discovered"])
            self.assertFalse(entry["detail"]["executor_evidence"][0]["execution_verified"])
        finally:
            app.store.close()


if __name__=='__main__':unittest.main()
