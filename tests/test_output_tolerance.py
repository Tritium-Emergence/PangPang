import sys
import tempfile
import time
import unittest
from pathlib import Path
from test_bot_runtime import config
from pangpang.service import Service
from pangpang.store import Fault


class OutputToleranceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()
        self.cfg = config(self.root)

    def tearDown(self):
        if hasattr(self, 'app'):
            self.app.store.close()
        self.tmp.cleanup()

    def _run(self, code, outputs, required_outputs=None):
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', code]
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 'task', 'allow_write': True}, 'exec')
        body = {'prompt': 'task', 'outputs': outputs}
        if required_outputs is not None:
            body['required_outputs'] = required_outputs
        res = self.app.submit_task('bot-main', msg['work_id'], body)
        task = self.app.store.get('task', res['task_id'])
        self.app.task_step()
        # drive to terminal
        for _ in range(200):
            self.app.task_step()
            task = self.app.store.get('task', task['id'])
            if task['status'] in ('succeeded', 'failed', 'cancelled', 'unknown'):
                break
            time.sleep(.03)
        return task

    def test_bare_label_declared_missing_fails_business_keeps_harvest(self):
        # exit=0 + a real file, but outputs=['docx','source_list'] resolve in-root
        # and are not files, so the runner records declared_output_missing. That
        # existing set fails the business verdict. The raw receipt stays the
        # execution fact (succeeded) and the harvested file is still registered —
        # unlike, which overwrote the receipt from a label exception.
        code = "open('real.docx','w').write('x'); print('done')"
        task = self._run(code, ['docx', 'source_list'])
        self.assertEqual(task['status'], 'failed', task.get('receipt'))
        self.assertEqual(task['error'], 'declared_output_missing')
        r = task['receipt']
        self.assertEqual(r['status'], 'succeeded')
        warned = {w['output'] for w in r.get('output_warnings', [])}
        self.assertEqual(warned, {'docx', 'source_list'})
        self.assertTrue(all(w['issue'] == 'declared_output_missing' for w in r['output_warnings']))
        arts = r.get('artifacts', [])
        self.assertEqual(len(arts), 1)
        self.assertTrue(arts[0]['path'].endswith('real.docx'))
        self.assertEqual(arts[0]['source'], 'cwd_fallback')
        self.assertNotEqual(self.app.store.get('work', task['work_id'])['status'], 'delivered')

    def test_79_output_dir_write_satisfies_the_declaration(self):
        # The delivery prompt requires the file under $XPP_OUTPUT_DIR. An
        # executor that obeys that and does not also write cwd/report.docx
        # must not be declared_output_missing. The file is registered once.
        code = ("import os; d=os.environ['XPP_OUTPUT_DIR']; "
                "open(os.path.join(d,'report.docx'),'w').write('ok'); print('done')")
        task = self._run(code, ['report.docx'])
        self.assertEqual(task['status'], 'succeeded', task.get('receipt'))
        self.assertEqual(task['receipt']['status'], 'succeeded')
        self.assertEqual(task['receipt'].get('exit_code'), 0)
        warned = [w for w in task['receipt'].get('output_warnings', [])
                  if w.get('output') == 'report.docx']
        self.assertEqual(warned, [])
        paths = [a['path'] for a in task['receipt'].get('artifacts', [])
                 if a['path'].endswith('report.docx')]
        self.assertEqual(len(paths), 1)
        self.assertEqual(self.app._unresolved_declared_outputs(task['work_id']), [])
        self._finish_continuation(task['work_id'], '报告已写入产物目录。')
        work = self.app.store.get('work', task['work_id'])
        self.assertNotEqual(work['status'], 'blocked')
        self.assertNotEqual(work.get('error'), 'declared_output_missing')

    def test_79_output_dir_file_beats_stale_cwd_copy(self):
        # A pre-existing cwd/report.docx must not bind the declaration when
        # this run wrote a different report.docx under $XPP_OUTPUT_DIR.
        import hashlib
        stale = b'S' * 20
        fresh = b'F' * 25
        (self.root / 'report.docx').write_bytes(stale)
        code = ("import os; p=os.path.join(os.environ['XPP_OUTPUT_DIR'],'report.docx'); "
                "open(p,'wb').write(b'F'*25); print('ok')")
        task = self._run(code, ['report.docx'])
        self.assertEqual(task['status'], 'succeeded', task.get('receipt'))
        self.assertEqual(task['receipt']['status'], 'succeeded')
        warned = [w for w in task['receipt'].get('output_warnings', [])
                  if w.get('issue') == 'declared_output_missing']
        self.assertEqual(warned, [])
        arts = [a for a in task['receipt'].get('artifacts', [])
                if a['path'].endswith('report.docx')]
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]['size'], 25)
        self.assertEqual(arts[0]['digest'], hashlib.sha256(fresh).hexdigest())
        self.assertIn('.xpp-out', arts[0]['path'])
        self.assertNotEqual(arts[0]['digest'], hashlib.sha256(stale).hexdigest())

    def test_79_missing_from_cwd_and_output_dir_still_blocks(self):
        # Absent from both places the runner is allowed to look.
        code = "print('wrote nothing')"
        task = self._run(code, ['report.docx'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'declared_output_missing')
        self.assertEqual(task['receipt']['status'], 'succeeded')
        warned = {w['output'] for w in task['receipt'].get('output_warnings', [])
                  if w.get('issue') == 'declared_output_missing'}
        self.assertIn('report.docx', warned)
        self._finish_continuation(task['work_id'], '没有这个文件。')
        work = self.app.store.get('work', task['work_id'])
        self.assertEqual(work['status'], 'blocked')
        self.assertNotEqual(work['status'], 'delivered')

    def test_relative_output_resolved_against_task_cwd(self):
        # A workspace-relative output path that really exists IS collected (resolved
        # against the task cwd, not the runner process dir).
        code = "open('out.docx','w').write('hello'); print('ok')"
        task = self._run(code, ['out.docx'])
        self.assertEqual(task['status'], 'succeeded', task.get('receipt'))
        arts = task['receipt'].get('artifacts', [])
        self.assertEqual(len(arts), 1)
        self.assertTrue(arts[0]['path'].endswith('out.docx'))
        self.assertEqual(task['receipt'].get('output_warnings', []), [])

    def test_declared_output_missing_fails_and_work_is_not_delivered(self):
        # Real runner: exit=0, declared in-root path never written. The warning
        # is computed by the runner; complete_task (via task_step) must fail the
        # business status and must not deliver the Work.
        code = "print('did not write the declared file')"
        task = self._run(code, ['out.docx'])
        self.assertEqual(task['status'], 'failed', task.get('receipt'))
        self.assertEqual(task['error'], 'declared_output_missing')
        self.assertEqual(task['receipt']['status'], 'succeeded')
        self.assertEqual(task['receipt']['exit_code'], 0)
        issues = {w['issue'] for w in task['receipt'].get('output_warnings', [])}
        self.assertIn('declared_output_missing', issues)
        dep = self.app.store.get('dependency', task['dependency_id'])
        self.assertEqual(dep['outcome'], 'failed')
        self.assertNotEqual(self.app.store.get('work', task['work_id'])['status'], 'delivered')
        self._finish_continuation(task['work_id'], '缺少声明的文件。')
        work = self.app.store.get('work', task['work_id'])
        self.assertEqual(work['status'], 'blocked')
        self.assertEqual(work['error'], 'declared_output_missing')
        self.assertNotEqual(work['status'], 'delivered')

    def _finish_continuation(self, work_id, content):
        turn = next(iter(self.app._all("turn", work_id=work_id, status="queued")), None)
        self.assertIsNotNone(turn, "expected a queued continuation turn")
        for m in self.app._work_inbox(work_id):
            m['consumed_by_turn_id'] = turn['id']
            self.app.store.put('message', m)
        self.app.finish_turn(turn, content)

    def test_submit_task_keeps_shape_validation(self):
        # Non-string-list outputs still rejected (shape guard retained); string labels
        # accepted (no content path hard-reject).
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print(1)']
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'e2')
        with self.assertRaises(Fault) as cm:
            self.app.submit_task('bot-main', msg['work_id'], {'prompt': 't', 'outputs': [123]})
        self.assertEqual(cm.exception.code, 'invalid_outputs')
        # string labels accepted
        r = self.app.submit_task('bot-main', msg['work_id'], {'prompt': 't', 'outputs': ['whatever']})
        self.assertIn('task_id', r)

    def test_failed_settle_leads_with_status_not_untrusted_tail(self):
        # A failed task's dependency payload/summary must lead with status/error and
        # NOT surface the executor's self-report as the answer (SIS: status is
        # authoritative; tail is untrusted).
        self.app = Service(config(self.root), self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'e3')
        res = self.app.submit_task('bot-main', msg['work_id'], {'prompt': 't'})
        task = self.app.store.get('task', res['task_id'])
        receipt = {"execution_id": task['execution_id'], "spec_digest": task['spec_digest'],
                   "status": "failed", "process_tree_confirmed": True, "supervision": "windows-job-object",
                   "exit_code": 1, "artifacts": [],
                   "error": "path_outside_allowed_roots",
                   "tail": "《文档》已完成并通过全部验收项 ✅"}
        self.app.complete_task(task, receipt)
        dep = self.app.store.get('dependency', res['dependency_id'])
        self.assertEqual(dep['outcome'], 'failed')
        self.assertTrue(dep['summary'].startswith('failed')) # status-first, not the tail
        payload = dep.get('result_payload', {})
        self.assertEqual(payload.get('status'), 'failed')
        # Raw executor free text is operator-only and must not exist in the
        # model-facing dependency payload at all.
        self.assertNotIn('untrusted_executor_tail', payload)
        self.assertNotIn('result_text', payload) # failed runs carry no result text

    def test_succeeded_settle_carries_bounded_untrusted_result_text(self):
        # A succeeded task's dependency payload carries the executor output tail as a
        # BOUNDED, explicitly-untrusted result_text (so query/research tasks — whose
        # answer is text, not a file — can be accepted and delivered in one pass
        # instead of re-dispatching). A long tail is truncated (tail-anchored) and
        # flagged. status remains authoritative; the summary leads with it.
        self.app = Service(config(self.root), self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'e5')
        res = self.app.submit_task('bot-main', msg['work_id'], {'prompt': 't'})
        task = self.app.store.get('task', res['task_id'])
        # The final answer sits at the END of the executor output; tail-anchored
        # truncation must preserve it (drop the verbose head, keep the answer).
        long_tail = ("x" * 6000) + "ANSWER-TAIL"
        receipt = {"execution_id": task['execution_id'], "spec_digest": task['spec_digest'],
                   "status": "succeeded", "process_tree_confirmed": True, "supervision": "windows-job-object",
                   "artifacts": [],
                   "tail": long_tail}
        self.app.complete_task(task, receipt)
        dep = self.app.store.get('dependency', res['dependency_id'])
        self.assertEqual(dep['outcome'], 'success')
        self.assertTrue(dep['summary'].startswith('succeeded')) # status-first, not the tail
        payload = dep.get('result_payload', {})
        self.assertEqual(payload.get('status'), 'succeeded')
        self.assertIn('ANSWER-TAIL', payload.get('result_text', '')) # final answer preserved
        self.assertLessEqual(len(payload['result_text']), self.app.RESULT_TEXT_MAX)
        self.assertTrue(payload['result_text_truncated'])
        #...and it is explicitly framed as untrusted data in the notice.
        self.assertIn('不可信', payload['notice'])

    def test_retry_reuses_outputs_through_tolerant_path(self):
        # retry_task re-enters submit_task with the old task's outputs; a bare-label
        # output must not make the retry fail submission (centralized tolerant path).
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', "open('r.docx','w').write('x'); print('ok')"]
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'e4')
        res = self.app.submit_task('bot-main', msg['work_id'], {'prompt': 't', 'outputs': ['docx']})
        task = self.app.store.get('task', res['task_id'])
        # force it terminal so retry is allowed
        task['status'] = 'failed'
        self.app.store.put('task', task)
        out = self.app.retry_task(res['task_id'])
        self.assertIn('task_id', out)
        retried = self.app.store.get('task', out['task_id'])
        self.assertEqual(retried['outputs'], ['docx']) # outputs carried over, submission ok

    def test_undeclared_file_in_output_dir_is_harvested(self):
        # The core fix: a file the model NEVER declared, written to $XPP_OUTPUT_DIR,
        # is auto-registered as an artifact (source=output_dir).
        code = ("import os; d=os.environ['XPP_OUTPUT_DIR']; "
                "open(os.path.join(d,'chart.png'),'wb').write(b'\\x89PNG_data'); print('ok')")
        task = self._run(code, []) # NOTHING declared
        self.assertEqual(task['status'], 'succeeded', task.get('receipt'))
        arts = task['receipt'].get('artifacts', [])
        self.assertEqual(len(arts), 1)
        self.assertTrue(arts[0]['path'].endswith('chart.png'))
        self.assertEqual(arts[0]['source'], 'output_dir')

    def test_undeclared_media_in_cwd_is_harvested_by_fallback(self):
        # A media file written straight to cwd (not the output dir, not declared)
        # is still caught by the shallow cwd fallback (source=cwd_fallback).
        code = "open('plot.png','wb').write(b'\\x89PNG'); print('ok')"
        task = self._run(code, [])
        self.assertEqual(task['status'], 'succeeded', task.get('receipt'))
        arts = task['receipt'].get('artifacts', [])
        names = {a['path'].split('/')[-1].split('\\')[-1]: a for a in arts}
        self.assertIn('plot.png', names)
        self.assertEqual(names['plot.png']['source'], 'cwd_fallback')

    def test_cwd_fallback_ignores_non_media_and_preexisting(self):
        # Fallback must NOT vacuum source/temp files: a new.py is ignored (not a
        # deliverable extension), and a pre-existing media file is ignored (only
        # files created DURING the run qualify).
        preexist = self.root / 'preexisting.png'
        preexist.write_bytes(b'old')
        # cwd defaults to installation cwd; config() sets it to self.root — write
        # both a new.py (wrong ext) and re-touch nothing, only produce a script.
        code = "open('helper.py','w').write('x=1'); print('ok')"
        self.cfg['installations'][0]['cwd'] = str(self.root)
        task = self._run(code, [])
        self.assertEqual(task['status'], 'succeeded', task.get('receipt'))
        arts = task['receipt'].get('artifacts', [])
        got = {a['path'].split('/')[-1].split('\\')[-1] for a in arts}
        self.assertNotIn('helper.py', got) # wrong extension
        self.assertNotIn('preexisting.png', got) # existed before the run

    def test_declared_and_harvested_dedupe_by_hash(self):
        # A file both declared AND present in the output dir must register once.
        code = ("import os; d=os.environ['XPP_OUTPUT_DIR']; "
                "p=os.path.join(d,'out.pdf'); open(p,'wb').write(b'PDFDATA'); print(p)")
        task = self._run(code, [])
        self.assertEqual(task['status'], 'succeeded', task.get('receipt'))
        arts = task['receipt'].get('artifacts', [])
        self.assertEqual(len(arts), 1) # not double-counted

    # ---------- Required Deliverable Contract — business adjudication ----------
    # These are DETERMINISTIC: they submit a task (no subprocess) and complete it with
    # a hand-built CONFIRMED receipt, so they do not depend on local process-tree
    # supervision (which is racy in a sandbox). The runner's provenance-boundary logic
    # is unit-tested separately in RequiredOutputVerifyTests.

    def _adjudicate(self, required_outputs, required_output_results, *, outputs=None,
                    output_warnings=None):
        """Submit a task then complete_task it with a hand-built succeeded receipt;
        return (task, work, dependency) after control-plane adjudication."""
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print(1)']
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'adj')
        body = {'prompt': 't'}
        if outputs is not None:
            body['outputs'] = outputs
        if required_outputs is not None:
            body['required_outputs'] = required_outputs
        res = self.app.submit_task('bot-main', msg['work_id'], body)
        task = self.app.store.get('task', res['task_id'])
        receipt = {"execution_id": task['execution_id'], "spec_digest": task['spec_digest'],
                   "status": "succeeded", "process_tree_confirmed": True,
                   "supervision": "windows-job-object", "exit_code": 0, "artifacts": [],
                   "tail": "全部完成并通过验收 ✅"}
        if required_output_results is not None:
            receipt["required_output_results"] = required_output_results
        if output_warnings is not None:
            receipt["output_warnings"] = output_warnings
        self.app.complete_task(task, receipt)
        return (self.app.store.get('task', task['id']),
                self.app.store.get('work', msg['work_id']),
                self.app.store.get('dependency', res['dependency_id']))

    def test_79_required_output_missing_fails(self):
        # exit 0 but the hard deliverable was not produced -> business status
        # failed / required_output_missing; the dependency carries the business verdict.
        task, work, dep = self._adjudicate(
            ['final.docx'], [{'output': 'final.docx', 'present': False, 'detail': 'missing_or_empty'}])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'required_output_missing')
        self.assertEqual(work.get('pending_required_outputs'), ['final.docx'])
        self.assertEqual(dep['outcome'], 'failed')

    def test_79_raw_receipt_preserved_while_business_fails(self):
        # (RCB/SIS layering): the raw receipt keeps the TRUE execution fact
        # (status=succeeded, exit_code=0) EVEN when business adjudication fails. Both
        # coexist; neither overwrites the other.
        task, _, _ = self._adjudicate(
            ['final.docx'], [{'output': 'final.docx', 'present': False}])
        self.assertEqual(task['status'], 'failed') # business truth
        self.assertEqual(task['receipt']['status'], 'succeeded') # raw execution fact
        self.assertEqual(task['receipt']['exit_code'], 0)

    def test_79_required_output_present_succeeds(self):
        # the deliverable is verified present inside the boundary -> succeeded,
        # no marker, dependency success.
        task, work, dep = self._adjudicate(
            ['final.docx'], [{'output': 'final.docx', 'present': True, 'detail': None}])
        self.assertEqual(task['status'], 'succeeded')
        self.assertIsNone(task.get('error'))
        self.assertFalse(work.get('pending_required_outputs'))
        self.assertEqual(dep['outcome'], 'success')

    def test_79_fail_closed_without_evidence(self):
        # A hard contract with NO verification evidence in the receipt (previously runner
        # / lost field) must fail closed — a contract is never satisfied on absent proof.
        task, _, _ = self._adjudicate(['final.docx'], None)
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'required_output_missing')

    def test_79_negative_control_legacy_outputs_without_missing_set_succeed(self):
        # Declaring soft outputs is not itself a failure, and a non-missing
        # warning (the label/path exception) does not flip the verdict.
        # The business failure is only the receipt's declared_output_missing set.
        task, work, _ = self._adjudicate(
            None, None, outputs=['final.docx', 'source_list'],
            output_warnings=[{"output": "source_list", "issue": "ValueError:path_outside_allowed_roots"}])
        self.assertEqual(task['status'], 'succeeded')
        self.assertFalse(work.get('pending_required_outputs'))
        self.assertNotEqual(work['status'], 'delivered') # no fast-accept; planner not finished

    def test_79_declared_output_missing_set_fails_business_verdict(self):
        # The runner already computed the set. complete_task must not leave the
        # task succeeded, and finishing the continuation must not deliver the Work.
        task, work, dep = self._adjudicate(
            None, None, outputs=['out.docx'],
            output_warnings=[{"output": "out.docx", "issue": "declared_output_missing"}])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'declared_output_missing')
        self.assertEqual(task['receipt']['status'], 'succeeded')
        self.assertEqual(dep['outcome'], 'failed')
        self.assertNotIn('declared_output_missing', work.get('error') or '')
        self.assertNotEqual(work['status'], 'delivered')
        self._finish_adjudicated(work['id'])
        work = self.app.store.get('work', work['id'])
        self.assertEqual(work['status'], 'blocked')
        self.assertEqual(work['error'], 'declared_output_missing')
        self.assertNotEqual(work['status'], 'delivered')

    def test_79_declared_missing_fails_when_required_contract_is_met(self):
        # required_outputs satisfied does not whitewash a non-empty
        # declared_output_missing set on the same receipt.
        task, work, dep = self._adjudicate(
            ['final.docx'], [{'output': 'final.docx', 'present': True}],
            outputs=['notes.docx'],
            output_warnings=[{"output": "notes.docx", "issue": "declared_output_missing"}])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'declared_output_missing')
        self.assertFalse(work.get('pending_required_outputs'))
        self.assertEqual(dep['outcome'], 'failed')
        self._finish_adjudicated(work['id'])
        work = self.app.store.get('work', work['id'])
        self.assertEqual(work['status'], 'blocked')
        self.assertNotEqual(work['status'], 'delivered')

    def test_79_real_declarations_keep_a_delivery_debt(self):
        # Seven real declarations. Six are declared_output_missing
        # (jev_topic_rank.json was the one file that existed). String shape
        # must not drop them. complete_task + finish_turn are the real path.
        seven = ['完整产物目录', '信源扫描', 'jev_topic_rank.json', '每篇 MD-HTML',
                 '配图', '微信 draft-add 回执', '短报']
        missing = [s for s in seven if s != 'jev_topic_rank.json']
        task, work, dep = self._adjudicate(
            None, None, outputs=seven,
            output_warnings=[{"output": s, "issue": "declared_output_missing"} for s in missing])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'declared_output_missing')
        self.assertEqual(task['receipt']['status'], 'succeeded')
        warned = [w['output'] for w in task['receipt']['output_warnings']
                  if w['issue'] == 'declared_output_missing']
        self.assertEqual(warned, missing)
        self.assertEqual(len(missing), 6)
        self.assertEqual(dep['outcome'], 'failed')
        debt = self.app._unresolved_declared_outputs(work['id'])
        self.assertEqual(debt, missing)
        self.assertNotIn('jev_topic_rank.json', debt)
        for phrase in seven:
            self.assertIn(phrase, task['prompt'])
        self._finish_adjudicated(work['id'])
        work = self.app.store.get('work', work['id'])
        self.assertNotEqual(work['status'], 'delivered')
        self.assertEqual(work['status'], 'blocked')
        self.assertEqual(work['error'], 'declared_output_missing')

    def test_79_unrelated_success_does_not_clear_a_missing_declaration(self):
        # A later succeeded task clears a key only when THAT receipt declares
        # the same key and does not list it missing. A different file does not.
        task, work, _ = self._adjudicate(
            None, None, outputs=['docx', 'out.docx'],
            output_warnings=[
                {"output": "docx", "issue": "declared_output_missing"},
                {"output": "out.docx", "issue": "declared_output_missing"},
            ])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(self.app._unresolved_declared_outputs(work['id']), ['docx', 'out.docx'])
        work_id = work['id']
        second = self.app.submit_task('bot-main', work_id, {'prompt': 't', 'outputs': ['report.docx']})
        task2 = self.app.store.get('task', second['task_id'])
        receipt2 = {"execution_id": task2['execution_id'], "spec_digest": task2['spec_digest'],
                    "status": "succeeded", "process_tree_confirmed": True,
                    "supervision": "windows-job-object", "exit_code": 0, "artifacts": [],
                    "output_warnings": []}
        self.app.complete_task(task2, receipt2)
        self.assertEqual(self.app.store.get('task', task2['id'])['status'], 'succeeded')
        self.assertEqual(self.app._unresolved_declared_outputs(work_id), ['docx', 'out.docx'])
        for _ in range(4):
            turn = next(iter(self.app._all("turn", work_id=work_id, status="queued")), None)
            if turn is None:
                break
            for m in self.app._work_inbox(work_id):
                m['consumed_by_turn_id'] = turn['id']
                self.app.store.put('message', m)
            self.app.finish_turn(turn, '另一份文件写好了。')
        work = self.app.store.get('work', work_id)
        self.assertEqual(work['status'], 'blocked')
        self.assertEqual(work['error'], 'declared_output_missing')
        self.assertNotEqual(work['status'], 'delivered')

    def test_79_later_success_without_the_warning_can_deliver(self):
        # A later execution that declares the same output and does not list it
        # missing clears the obligation. No file-freshness probe: the receipt set
        # is the only evidence.
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print(1)']
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'repair')
        work_id = msg['work_id']
        first = self.app.submit_task('bot-main', work_id, {'prompt': 't', 'outputs': ['out.docx']})
        task = self.app.store.get('task', first['task_id'])
        receipt = {"execution_id": task['execution_id'], "spec_digest": task['spec_digest'],
                   "status": "succeeded", "process_tree_confirmed": True,
                   "supervision": "windows-job-object", "exit_code": 0, "artifacts": [],
                   "output_warnings": [{"output": "out.docx", "issue": "declared_output_missing"}]}
        self.app.complete_task(task, receipt)
        self.assertEqual(self.app.store.get('task', task['id'])['status'], 'failed')
        second = self.app.submit_task('bot-main', work_id, {'prompt': 't', 'outputs': ['out.docx']})
        task2 = self.app.store.get('task', second['task_id'])
        receipt2 = {"execution_id": task2['execution_id'], "spec_digest": task2['spec_digest'],
                    "status": "succeeded", "process_tree_confirmed": True,
                    "supervision": "windows-job-object", "exit_code": 0, "artifacts": [],
                    "output_warnings": []}
        self.app.complete_task(task2, receipt2)
        self.assertEqual(self.app.store.get('task', task2['id'])['status'], 'succeeded')
        # The first continuation is still queued; finish it while the repair dep
        # may already be settled. Drain queued turns until the Work can deliver.
        for _ in range(4):
            turn = next(iter(self.app._all("turn", work_id=work_id, status="queued")), None)
            if turn is None:
                break
            for m in self.app._work_inbox(work_id):
                m['consumed_by_turn_id'] = turn['id']
                self.app.store.put('message', m)
            self.app.finish_turn(turn, '已写出 out.docx。')
        self.assertEqual(self.app.store.get('work', work_id)['status'], 'delivered')

    def test_79_late_declared_missing_does_not_create_a_delivery_obligation(self):
        # A late receipt for an already-settled unknown dependency records the
        # business failure on the task and does not make the Work undeliverable.
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print(1)']
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'late-dec')
        work_id = msg['work_id']
        res = self.app.submit_task('bot-main', work_id, {'prompt': 't', 'outputs': ['late.docx']})
        task = self.app.store.get('task', res['task_id'])
        dep = self.app.store.get('dependency', res['dependency_id'])
        dep.update(status='settled', outcome='unknown')
        self.app.store.put('dependency', dep)
        task['status'] = 'unknown'
        self.app.store.put('task', task)
        receipt = {"execution_id": task['execution_id'], "spec_digest": task['spec_digest'],
                   "status": "succeeded", "process_tree_confirmed": True,
                   "supervision": "windows-job-object", "exit_code": 0, "artifacts": [],
                   "output_warnings": [{"output": "late.docx", "issue": "declared_output_missing"}]}
        self.app.complete_task(task, receipt)
        self.assertEqual(self.app.store.get('task', task['id'])['status'], 'failed')
        self.assertEqual(self.app.store.get('task', task['id'])['error'], 'declared_output_missing')
        self.assertEqual(self.app.store.get('dependency', res['dependency_id'])['outcome'], 'unknown')
        # The late failure is on the task. It does not become a delivery obligation.
        self.assertEqual(self.app._unresolved_declared_outputs(work_id), [])

    def test_79_required_missing_still_beats_declared_warning(self):
        # Do not weaken required_outputs: an unmet hard contract keeps that error
        # even when the receipt also lists declared_output_missing.
        task, work, dep = self._adjudicate(
            ['final.docx'], [{'output': 'final.docx', 'present': False}],
            outputs=['final.docx'],
            output_warnings=[{"output": "final.docx", "issue": "declared_output_missing"}])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'required_output_missing')
        self.assertEqual(work.get('pending_required_outputs'), ['final.docx'])
        self.assertEqual(dep['outcome'], 'failed')

    def _finish_adjudicated(self, work_id):
        turn = next(iter(self.app._all("turn", work_id=work_id, status="queued")), None)
        self.assertIsNotNone(turn, "expected a queued continuation turn")
        for m in self.app._work_inbox(work_id):
            m['consumed_by_turn_id'] = turn['id']
            self.app.store.put('message', m)
        self.app.finish_turn(turn, '无法交付。')

    def test_79_submit_rejects_malformed_required_outputs(self):
        # shape/safety guard at admission: non-string-list, absolute paths and
        # parent-escape are a fast 400 (invalid_required_outputs).
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print(1)']
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'ro')
        for bad in ([123], ['/etc/passwd'], ['../escape.docx'], ['sub/../../x'], ['']):
            with self.assertRaises(Fault) as cm:
                self.app.submit_task('bot-main', msg['work_id'],
                                     {'prompt': 't', 'required_outputs': bad})
            self.assertEqual(cm.exception.code, 'invalid_required_outputs', bad)
        r = self.app.submit_task('bot-main', msg['work_id'],
                                 {'prompt': 't', 'required_outputs': ['out/final.docx']})
        self.assertIn('task_id', r) # a clean relative path is accepted

    def test_79_outputs_reuse_required_path_admission(self):
        # outputs now enters the executor prompt, so it uses the same relative
        # path admission as required_outputs. Empty stays allowed. A rejected
        # list is not injected.
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print(1)']
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'out-admit')
        too_long = 'x' * 513
        for bad in (['../../../etc/passwd'], ['/etc/passwd'], [too_long], ['']):
            with self.assertRaises(Fault) as cm:
                self.app.submit_task('bot-main', msg['work_id'],
                                     {'prompt': 't', 'outputs': bad})
            self.assertEqual(cm.exception.code, 'invalid_outputs', bad)
            self.assertEqual(cm.exception.advice,
                             'outputs 必须是产物目录下的相对路径，绝对路径会被拒绝。')
        ok = self.app.submit_task('bot-main', msg['work_id'],
                                  {'prompt': 't', 'outputs': []})
        self.assertIn('task_id', ok)
        for task in self.app._all('task', work_id=msg['work_id']):
            prompt = task.get('prompt') or ''
            self.assertNotIn('../../../etc/passwd', prompt)
            self.assertNotIn('/etc/passwd', prompt)
            self.assertNotIn(too_long, prompt)

    def test_79_retry_preserves_required_outputs(self):
        # retry_task carries the hard contract so a repair re-enters under the same
        # acceptance (recovery flow).
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', "print('x')"]
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'rr')
        res = self.app.submit_task('bot-main', msg['work_id'],
                                   {'prompt': 't', 'required_outputs': ['final.docx']})
        task = self.app.store.get('task', res['task_id'])
        task['status'] = 'failed'; self.app.store.put('task', task)
        out = self.app.retry_task(res['task_id'])
        retried = self.app.store.get('task', out['task_id'])
        self.assertEqual(retried['required_outputs'], ['final.docx'])

    def test_79_fast_accept_refused_for_required_outputs(self):
        # a required_outputs task is a file-delivery task; it must
        # never be fast-accepted on the executor text tail.
        from pangpang.task_gate import TaskResultGate
        from pangpang.semantic_gate import SemanticGate
        self.app = Service(self.cfg, self.root.parent / 'data')
        gate = TaskResultGate(self.app, SemanticGate(self.app))
        receipt = {"status": "succeeded", "process_tree_confirmed": True,
                   "tail": "all done", "artifacts": []}
        base = {"id": "t1", "access_mode": "verified_read"}
        self.assertTrue(gate.eligible(base, receipt)) # control: eligible without it
        self.assertFalse(gate.eligible(dict(base, required_outputs=['final.docx']), receipt))

    def test_79_prefix_collision_does_not_whitewash(self):
        # two DISTINCT contract paths sharing a 200-char prefix must not collide.
        # The present one must NOT whitewash the different missing one. (The old
        # 200-char identity key collapsed them and passed the task falsely.)
        prefix = 'd' * 200
        p1, p2 = prefix + '/one.docx', prefix + '/two.docx'
        task, work, dep = self._adjudicate(
            [p1, p2], [{'output': p1, 'present': True}, {'output': p2, 'present': False}])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], 'required_output_missing')
        self.assertEqual(work.get('pending_required_outputs'), [p2]) # ONLY the missing path
        self.assertEqual(dep['outcome'], 'failed')

    def test_90_hard_contract_is_followable_without_a_shell(self):
        # ②: the HARD contract is stated relative to $XPP_OUTPUT_DIR, but the
        # directory only exists once the runner creates it, and an executor restricted
        # to file tools has NO shell to expand a variable — it would write to cwd and
        # the delivery would fail (reproduced live with claude-code: Read+Write alone
        # landed the file in cwd / cwd_fallback; only adding Bash fixed it).
        # So the runner must bind the LITERAL directory into the prompt before launch.
        # This executor proves the prompt alone is sufficient: it locates the output
        # boundary ONLY by reading its prompt — no os.environ, no shell — and writes
        # the contract file there. Pre-fix the prompt carries no literal path, the
        # assert trips, and the task fails with required_output_missing.
        code = ("import sys,re,pathlib;"
                "p=sys.stdin.read();"
                "pathlib.Path('prompt.txt').write_text(p,encoding='utf-8');"
                "m=re.search(r'[^\\s（）]*\\.xpp-out[^\\s（）]*', p);"
                "assert m, 'prompt names no literal output dir';"
                "pathlib.Path(m.group(0),'final.docx').write_bytes(b'x')")
        task = self._run(code, [], ['final.docx'])
        self.assertEqual(task['status'], 'succeeded', task.get('receipt'))
        r = task['receipt']
        self.assertEqual([x['present'] for x in r['required_output_results']], [True])
        # Produced INSIDE the boundary (provenance), not rescued by the cwd fallback.
        self.assertEqual([a['source'] for a in r.get('artifacts', [])], ['output_dir'])
        # And nothing unexpandable is left in what the executor actually received.
        received = (self.root / 'prompt.txt').read_text(encoding='utf-8')
        self.assertNotIn('$XPP_OUTPUT_DIR', received)
        self.assertIn('final.docx', received)

    def test_79_required_outputs_wired_into_tool_contract_and_prompt(self):
        # the model-facing submit_task schema must expose required_outputs (closed
        # schema would otherwise reject the field), and the control plane must
        # deterministically inject the EXACT paths into the executor prompt and store
        # them on the task — proving the real model→tool→executor chain carries it.
        from pangpang.runtime import TOOLS
        schema = next(t['function']['parameters'] for t in TOOLS
                      if t['function']['name'] == 'submit_task')
        self.assertIn('required_outputs', schema['properties'])
        self.assertFalse(schema.get('additionalProperties', True)) # closed schema admits it
        desc = next(t['function']['description'] for t in TOOLS
                    if t['function']['name'] == 'submit_task')
        self.assertIn('outputs: artifacts to register are file paths, not type labels', desc)
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print(1)']
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'tc')
        res = self.app.submit_task('bot-main', msg['work_id'],
                                   {'prompt': '出一份报告', 'outputs': ['notes.md'],
                                    'required_outputs': ['out/report.docx']})
        task = self.app.store.get('task', res['task_id'])
        self.assertEqual(task['required_outputs'], ['out/report.docx'])
        self.assertIn('out/report.docx', task['prompt']) # exact path injected verbatim
        self.assertIn('notes.md', task['prompt']) # declared outputs, same prompt
        self.assertIn('Declared artifacts', task['prompt'])
        self.assertIn('Mandatory deliverables', task['prompt']) # the HARD-contract clause
        self.assertIn('XPP_OUTPUT_DIR', task['prompt']) # anchored to the output boundary


class BindOutputDirTests(unittest.TestCase):
    """ ②: runner.bind_output_dir — the prompt must name the output boundary by
    its real path by the time the executor reads it. Pure string binding: no
    filesystem, no model text, no shell."""

    def _bind(self, prompt, output_dir):
        from pangpang import runner
        return runner.bind_output_dir(prompt, output_dir)

    def test_every_mention_becomes_the_literal_dir(self):
        self.assertEqual(self._bind('写到 $XPP_OUTPUT_DIR，核验 $XPP_OUTPUT_DIR 非空', '/w/.xpp-out/e-1'),
                         '写到 /w/.xpp-out/e-1，核验 /w/.xpp-out/e-1 非空')

    def test_no_output_dir_invents_no_path(self):
        # A read-only cwd degrades to output_dir=None. There is no truthful path to
        # substitute (and the env var is equally absent), so leave the text alone
        # rather than point the executor somewhere that does not exist.
        for empty in (None, ''):
            self.assertEqual(self._bind('写到 $XPP_OUTPUT_DIR', empty), '写到 $XPP_OUTPUT_DIR')

    def test_neighbouring_names_and_backslashes_survive(self):
        # Token boundary like a shell: $XPP_OUTPUT_DIR_BACKUP is a different name.
        # And a Windows path must land verbatim — a template replacement would read
        # its backslashes as escapes (\\x -> \x08 and friends).
        out = self._bind('$XPP_OUTPUT_DIR_BACKUP 与 $XPP_OUTPUT_DIR', 'C:\\ws\\.xpp-out\\e-1')
        self.assertEqual(out, '$XPP_OUTPUT_DIR_BACKUP 与 C:\\ws\\.xpp-out\\e-1')


class RequiredOutputVerifyTests(unittest.TestCase):
    """Unit tests for runner.verify_required_outputs — the provenance boundary.
    Pure/local: no subprocess, no Service."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'ws'; self.root.mkdir()
        self.out = self.root / '.xpp-out' / 'exec-1'; self.out.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _verify(self, required, output_dir=True):
        from pangpang import runner
        od = self.out if output_dir else None
        return runner.verify_required_outputs(required, od, [str(self.root)])

    def test_present_nonempty_file_in_boundary(self):
        (self.out / 'final.docx').write_text('real deliverable')
        self.assertTrue(self._verify(['final.docx'])[0]['present'])

    def test_missing_file(self):
        r = self._verify(['final.docx'])
        self.assertFalse(r[0]['present'])
        self.assertEqual(r[0]['detail'], 'missing_or_empty')

    def test_empty_file_is_not_a_deliverable(self):
        (self.out / 'final.docx').write_text('') # zero bytes
        self.assertFalse(self._verify(['final.docx'])[0]['present'])

    def test_stale_cwd_file_does_not_count(self):
        # a same-named file OUTSIDE the per-execution output dir (in cwd) does
        # NOT satisfy the contract — provenance, not mere existence.
        (self.root / 'final.docx').write_text('stale pre-existing')
        self.assertFalse(self._verify(['final.docx'])[0]['present'])

    def test_parent_escape_rejected(self):
        (self.root / 'secret.docx').write_text('x')
        self.assertFalse(self._verify(['../secret.docx'])[0]['present'])

    def test_absolute_path_outside_boundary_rejected(self):
        self.assertFalse(self._verify(['/etc/passwd'])[0]['present'])

    def test_symlink_escape_rejected(self):
        target = self.root / 'outside.docx'; target.write_text('leak')
        link = self.out / 'final.docx'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlink not permitted on this platform')
        # checked() resolves the symlink to a path outside the boundary -> relative_to fails
        self.assertFalse(self._verify(['final.docx'])[0]['present'])

    def test_symlinked_parent_sees_present_file_and_missing_or_empty(self):
        # A symlinked parent (macOS /var -> /private/var, or a symlinked TMPDIR)
        # must not turn relative_to into ValueError. A real nonempty file is
        # present; a missing required file is missing_or_empty.
        real = Path(self.tmp.name) / 'real-parent'
        real.mkdir()
        link = Path(self.tmp.name) / 'linked-parent'
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink not permitted on this platform')
        root = link / 'ws'
        root.mkdir()
        out = root / '.xpp-out' / 'exec-1'
        out.mkdir(parents=True)
        (out / 'final.docx').write_text('real deliverable')
        from pangpang import runner
        present = runner.verify_required_outputs(['final.docx'], out, [str(root)])
        missing = runner.verify_required_outputs(['missing.docx'], out, [str(root)])
        self.assertTrue(present[0]['present'], present)
        self.assertFalse(missing[0]['present'])
        self.assertEqual(missing[0]['detail'], 'missing_or_empty')

    def test_no_output_dir_fails_closed(self):
        r = self._verify(['final.docx'], output_dir=False)
        self.assertFalse(r[0]['present'])
        self.assertEqual(r[0]['detail'], 'no_output_dir')

    def test_empty_contract_is_empty(self):
        self.assertEqual(self._verify([]), [])


class RequiredOutputWorkInvariantTests(unittest.TestCase):
    """A Work with an unrepaired hard required-output acceptance failure must never
    deliver. Deterministic: completion via hand-built receipts, turns finished by
    mimicking the scheduler's inbox-consume (no model, no subprocess)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'ws'; self.root.mkdir()
        self.cfg = config(self.root)
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print(1)']
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')

    def tearDown(self):
        self.app.store.close(); self.tmp.cleanup()

    def _submit(self, work_id):
        res = self.app.submit_task('bot-main', work_id,
                                   {'prompt': 't', 'required_outputs': ['final.docx']})
        return self.app.store.get('task', res['task_id'])

    def _receipt(self, task, present):
        return {"execution_id": task['execution_id'], "spec_digest": task['spec_digest'],
                "status": "succeeded", "process_tree_confirmed": True,
                "supervision": "windows-job-object", "exit_code": 0, "artifacts": [],
                "required_output_results": [{"output": "final.docx", "present": present}],
                "tail": "完成 ✅"}

    def _finish_continuation(self, work_id, content):
        """Mimic the scheduler: consume the Work inbox into its queued continuation
        turn, then finish that turn (no model, no subprocess)."""
        turn = next(iter(self.app._all("turn", work_id=work_id, status="queued")), None)
        self.assertIsNotNone(turn, "expected a queued continuation turn")
        for m in self.app._work_inbox(work_id):
            m['consumed_by_turn_id'] = turn['id']
            self.app.store.put('message', m)
        self.app.finish_turn(turn, content)

    def test_79_work_blocked_when_no_repair(self):
        # hard acceptance failure, planner creates NO repair -> Work blocked,
        # never delivered.
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'wb')
        task = self._submit(msg['work_id'])
        self.app.complete_task(task, self._receipt(task, present=False))
        self.assertTrue(self.app.store.get('work', msg['work_id']).get('pending_required_outputs'))
        self._finish_continuation(msg['work_id'], '缺少交付文件，无法完成。')
        work = self.app.store.get('work', msg['work_id'])
        self.assertEqual(work['status'], 'blocked') # NOT delivered
        self.assertEqual(work['error'], 'required_output_missing')

    def test_79_complete_work_chokepoint_refuses_delivery(self):
        # The single delivery transition refuses while the marker is set, regardless
        # of caller (defence in depth behind finish_turn's routing).
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'ck')
        task = self._submit(msg['work_id'])
        self.app.complete_task(task, self._receipt(task, present=False))
        work = self.app.store.get('work', msg['work_id'])
        with self.app.store.atomic():
            self.assertFalse(self.app._complete_work(work, 'done'))
        self.assertNotEqual(self.app.store.get('work', msg['work_id'])['status'], 'delivered')

    def test_79_work_waits_then_delivers_on_repair(self):
        # planner creates a repair dependency -> Work waiting; repair truly
        # succeeds -> marker cleared -> delivered.
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'wr')
        work_id = msg['work_id']
        task1 = self._submit(work_id)
        self.app.complete_task(task1, self._receipt(task1, present=False))
        task2 = self._submit(work_id) # the repair, same hard contract
        self._finish_continuation(work_id, '已安排重做。')
        self.assertEqual(self.app.store.get('work', work_id)['status'], 'waiting')
        self.assertTrue(self.app.store.get('work', work_id).get('pending_required_outputs'))
        self.app.complete_task(task2, self._receipt(task2, present=True))
        self.assertFalse(self.app.store.get('work', work_id).get('pending_required_outputs'))
        self._finish_continuation(work_id, '已交付 final.docx。')
        self.assertEqual(self.app.store.get('work', work_id)['status'], 'delivered')

    def _submit_req(self, work_id, required):
        res = self.app.submit_task('bot-main', work_id, {'prompt': 't', 'required_outputs': required})
        return self.app.store.get('task', res['task_id'])

    def _receipt_for(self, task, present_map):
        """present_map: {declared_path: present_bool}."""
        return {"execution_id": task['execution_id'], "spec_digest": task['spec_digest'],
                "status": "succeeded", "process_tree_confirmed": True,
                "supervision": "windows-job-object", "exit_code": 0, "artifacts": [],
                "required_output_results": [{"output": p, "present": v} for p, v in present_map.items()],
                "tail": "完成"}

    def test_79_unrelated_success_does_not_clear_sibling_contract(self):
        # counter-example: A owes a.docx (missing); a DIFFERENT task B owes b.png
        # and succeeds. B's success must remove ONLY the path it really produced — it
        # must NOT clear A's still-missing a.docx. The Work still owes a.docx and must
        # not be deliverable. (A boolean marker would wrongly be cleared here.)
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'uc')
        work_id = msg['work_id']
        a = self._submit_req(work_id, ['a.docx'])
        self.app.complete_task(a, self._receipt_for(a, {'a.docx': False}))
        self.assertEqual(self.app.store.get('work', work_id).get('pending_required_outputs'), ['a.docx'])
        b = self._submit_req(work_id, ['b.png'])
        self.app.complete_task(b, self._receipt_for(b, {'b.png': True}))
        self.assertEqual(self.app.store.get('work', work_id).get('pending_required_outputs'), ['a.docx'])
        work = self.app.store.get('work', work_id)
        with self.app.store.atomic():
            self.assertFalse(self.app._complete_work(work, 'done')) # a.docx still owed

    def test_79_late_receipt_does_not_mutate_work_obligations(self):
        # a late confirmed receipt for a task whose dependency was ALREADY settled
        # (unknown) must not touch the Work's acceptance obligations — the late-result
        # no-reopen invariant covers delivery obligations, not just the dependency.
        msg = self.app.send('bot-main', {'text': 't', 'allow_write': True}, 'lr')
        work_id = msg['work_id']
        task = self._submit_req(work_id, ['late.docx'])
        dep = self.app._dependency_for('task', task['id'])
        dep.update(status='settled', outcome='unknown')
        self.app.store.put('dependency', dep)
        task['status'] = 'unknown'; self.app.store.put('task', task)
        work = self.app.store.get('work', work_id)
        work['pending_required_outputs'] = ['other.docx'] # a pre-existing, unrelated obligation
        self.app.store.put('work', work)
        self.app.complete_task(task, self._receipt_for(task, {'late.docx': False}))
        # late.docx must NOT be added; other.docx must NOT be touched.
        self.assertEqual(self.app.store.get('work', work_id).get('pending_required_outputs'), ['other.docx'])


class HarvestUnitTests(unittest.TestCase):
    """Direct unit tests for the security-hardened harvest primitives."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _budget(self):
        from pangpang import runner
        return {"files": runner.HARVEST_MAX_FILES, "bytes": runner.HARVEST_MAX_TOTAL_BYTES}

    def test_oversized_file_is_rejected(self):
        from pangpang import runner
        big = self.root / 'huge.png'
        big.write_bytes(b'x' * 32)
        # Temporarily shrink the per-file cap so a tiny file trips it.
        orig = runner.HARVEST_MAX_FILE_BYTES
        runner.HARVEST_MAX_FILE_BYTES = 8
        try:
            got = runner.harvest_dir(self.root, [str(self.root)], recursive=False,
                                     source='cwd_fallback', budget=self._budget(),
                                     seen_digests=set(), new_only_names=set(),
                                     exts=runner.HARVEST_FALLBACK_EXTS)
        finally:
            runner.HARVEST_MAX_FILE_BYTES = orig
        self.assertEqual(got, []) # 32 bytes > 8-byte cap -> not harvested

    def test_symlinked_file_is_not_harvested(self):
        from pangpang import runner
        target = self.root / 'secret.png'
        target.write_bytes(b'\x89PNG-secret')
        link = self.root / 'link.png'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlink not permitted on this platform')
        got = runner.harvest_dir(self.root, [str(self.root)], recursive=False,
                                 source='cwd_fallback', budget=self._budget(),
                                 seen_digests=set(), new_only_names=set(),
                                 exts=runner.HARVEST_FALLBACK_EXTS)
        names = {Path(a['path']).name for a in got}
        # The symlink itself must never be collected; only the real regular file.
        self.assertNotIn('link.png', names)

    def test_directory_symlink_not_traversed(self):
        from pangpang import runner
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'leak.png').write_bytes(b'\x89PNGleak')
        inside = self.root / 'work'
        inside.mkdir()
        try:
            (inside / 'escape').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink not permitted on this platform')
        got = runner.harvest_dir(inside, [str(self.root)], recursive=True,
                                 source='output_dir', budget=self._budget(),
                                 seen_digests=set())
        names = {Path(a['path']).name for a in got}
        self.assertNotIn('leak.png', names) # dir symlink not followed

    def test_venv_toolchain_noise_does_not_consume_harvest_budget(self):
        # .venv-offline (and the same pip/venv trees outside it) is isolation
        # noise, not a deliverable. More of those files than HARVEST_MAX_FILES
        # sit beside a few real docs/images; harvest must return only the real
        # files and leave the unused file slots for them.
        from pangpang import runner
        real = {
            'report.docx': b'docx-bytes',
            'chart.png': b'\x89PNG-chart',
            'notes.md': b'# notes\n',
            'result.txt': b'ok',
            'script.py': b'print(1)\n',
            'bundle.zip': b'PK\x03\x04zip',
            'LICENSE': b'root license',
            'docs/readme.md': b'readme',
            'images/photo.jpg': b'\xff\xd8jpg',
        }
        for rel, data in real.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        noise = [
            'docs/pyvenv.cfg',
            'loose.pyc',
            'pyvenv.cfg',
            'Activate.ps1',
            'activate',
            'activate.csh',
            'venv/pyvenv.cfg',
            'venv/bin/activate',
            'site-packages/mod.py',
            '__pycache__/x.cpython-312.pyc',
            'widget.egg-info/PKG-INFO',
            'widget.egg-info/SOURCES.txt',
            'pkg-1.0.dist-info/LICENSE',
            'pkg-1.0.dist-info/RECORD',
            'pkg-1.0.dist-info/WHEEL',
            'pkg-1.0.dist-info/METADATA',
            '.venv-offline/pyvenv.cfg',
            '.venv-offline/bin/activate',
            '.venv-offline/bin/Activate.ps1',
            '.venv-offline/lib/python3.12/site-packages/pkg/__init__.py',
            '.venv-offline/lib/python3.12/site-packages/pkg/__pycache__/mod.cpython-312.pyc',
            '.venv-offline/lib/python3.12/site-packages/pkg-1.2.dist-info/LICENSE',
            '.venv-offline/lib/python3.12/site-packages/pkg-1.2.dist-info/RECORD',
            '.venv-offline/lib/python3.12/site-packages/pkg-1.2.dist-info/WHEEL',
            '.venv-offline/lib/python3.12/site-packages/pkg-1.2.dist-info/METADATA',
        ]
        for i in range(runner.HARVEST_MAX_FILES + 8):
            noise.append('.venv-offline/lib/python3.12/site-packages/'
                         'pkg-1.2.dist-info/extra-%d.txt' % i)
        for index, rel in enumerate(noise):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'noise-%d' % index)
        budget = self._budget()
        got = runner.harvest_dir(self.root, [str(self.root)], recursive=True,
                                 source='output_dir', budget=budget, seen_digests=set())
        root = self.root.resolve()
        names = sorted(Path(a['path']).resolve().relative_to(root).as_posix() for a in got)
        self.assertEqual(names, sorted(real))
        self.assertTrue(all(a['source'] == 'output_dir' for a in got))
        self.assertEqual(budget['files'], runner.HARVEST_MAX_FILES - len(real))
        self.assertEqual(budget['bytes'],
                         runner.HARVEST_MAX_TOTAL_BYTES - sum(len(v) for v in real.values()))


class ResultNotificationTests(unittest.TestCase):
    # An executor task result is an input to the Bot, not a result for the user: it
    # does not notify. The Work reaching the user notifies once, which is what the
    # bot badge and done_unread count.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'workspace'; self.root.mkdir()
        self.cfg = config(self.root)
        self.cfg['installations'][0]['argv'] = [sys.executable, '-c', 'print(1)']
        self.app = Service(self.cfg, self.root.parent / 'data')
        self.app.probe('codex-local')

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def unread(self):
        return [n for n in self.app.store.find('notification', bot_id='bot-main') if not n.get('read')]

    def attention(self, work_id):
        from pangpang.attention import project
        s = self.app.store
        states, summary = project(s.find('work'), s.find('turn'), s.find('task'), s.find('exchange'),
                                  s.find('routine'), s.find('notification'), s.find('claim'))
        return states.get(work_id), summary['done_unread']

    def test_task_result_does_not_notify_and_delivery_notifies_once(self):
        msg = self.app.send('bot-main', {'text': '整理周报', 'allow_write': True}, 'note')
        work_id = msg['work_id']
        res = self.app.submit_task('bot-main', work_id, {'prompt': 't'})
        task = self.app.store.get('task', res['task_id'])
        self.app.complete_task(task, {"execution_id": task['execution_id'], "spec_digest": task['spec_digest'],
                                      "status": "succeeded", "process_tree_confirmed": True,
                                      "supervision": "windows-job-object", "exit_code": 0, "artifacts": [],
                                      "output_warnings": []})
        self.assertEqual(self.app.store.get('task', task['id'])['status'], 'succeeded')
        self.assertEqual(self.unread(), [])
        for _ in range(4):
            turn = next(iter(self.app._all("turn", work_id=work_id, status="queued")), None)
            if turn is None:
                break
            for m in self.app._work_inbox(work_id):
                m['consumed_by_turn_id'] = turn['id']
                self.app.store.put('message', m)
            self.app.finish_turn(turn, '周报已整理好。')
        self.assertEqual(self.app.store.get('work', work_id)['status'], 'delivered')
        notes = self.unread()
        self.assertEqual([(n['kind'], n['work_id']) for n in notes], [('result', work_id)])
        self.assertTrue(notes[0]['title'].startswith('已回复：'), notes[0]['title'])
        self.assertEqual(self.attention(work_id), ('done_unread', 1))
        self.app.mark_notification('bot-main', notes[0]['id'])
        self.assertEqual(self.attention(work_id), ('settled', 0))


if __name__ == '__main__':
    unittest.main()
