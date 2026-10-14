// Copy contract for a manual unknown-task resolve: the prompt and the note on a
// resolved card both name the text as an operator-written audit record that the
// system did not verify. The note text is the operator field, never a system proof.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import zhCN from '../web-src/i18n/zh-CN.ts';
import enUS from '../web-src/i18n/en-US.ts';
import {operatorStatement} from '../web-src/pure.ts';

const zhLabel = zhCN.workpane.operatorResolveNoteLabel;
const enLabel = enUS.workpane.operatorResolveNoteLabel;

test('both locales call the resolve text an unverified human audit record', () => {
  assert.match(zhLabel, /审计记录/);
  assert.match(zhLabel, /人工陈述/);
  assert.match(zhLabel, /系统未核验这段文字/);
  assert.equal(zhLabel.includes('核验依据'), false);
  assert.match(enLabel, /Audit record/);
  assert.match(enLabel, /human statement/);
  assert.match(enLabel, /did not verify this text/);
  assert.equal(enLabel.toLowerCase().includes('verification evidence'), false);
});

test('the resolve prompt reuses that label and still requires 10 characters', () => {
  assert.match(zhCN.workpane.recoveryEvidencePrompt, /\{\{label\}\}/);
  assert.match(zhCN.workpane.recoveryEvidencePrompt, /10/);
  assert.equal(zhCN.workpane.recoveryEvidencePrompt.includes('核验依据'), false);
  assert.match(enUS.workpane.recoveryEvidencePrompt, /\{\{label\}\}/);
  assert.match(enUS.workpane.recoveryEvidencePrompt, /10/);
  assert.equal(enUS.workpane.recoveryEvidencePrompt.toLowerCase().includes('verification evidence'), false);
});

test('a resolved card reads the operator note and ignores system termination evidence', () => {
  const text = '已确认执行器进程不存在，目录无锁文件。';
  assert.equal(operatorStatement({
    status: 'cancelled',
    receipt: {
      operator_evidence: `  ${text}  `,
      operator_resolution: 'confirmed_stopped',
      termination_evidence: 'pgroup-empty',
      tail: 'Operator resolved: confirmed_stopped',
    },
  }), text);
  assert.equal(operatorStatement({
    status: 'cancelled',
    receipt: {termination_evidence: 'cgroup-v2-populated-zero', tail: 'done'},
  }), null);
  assert.equal(operatorStatement({status: 'unknown'}), null);
  assert.equal(operatorStatement(null), null);
  assert.equal(operatorStatement({operator_evidence: '   ', receipt: {operator_evidence: '  '}}), null);
  assert.equal(operatorStatement({operator_evidence: 'operator confirms process stopped'}), 'operator confirms process stopped');
  assert.equal(operatorStatement({
    operator_evidence: 'pending note that is long enough',
    receipt: {operator_evidence: 'committed note that is long enough'},
  }), 'committed note that is long enough');
});

test('the task card shows the shared label with the operator note', () => {
  const work = fs.readFileSync('web-src/components/WorkPane.tsx', 'utf8');
  assert.match(work, /operatorStatement\(t\)/);
  assert.match(work, /data-testid="operator-resolve-note"/);
  assert.match(work, /workpane\.operatorResolveNoteLabel/);
  assert.match(work, /tr\('workpane\.recoveryEvidencePrompt', \{label: tr\('workpane\.operatorResolveNoteLabel'\)\}\)/);
});
