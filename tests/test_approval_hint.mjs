// Bot-level approval hint and connector progress copy. The hint does not
// depend on the live preview or on activity.tasks, and the inspector pointer
// does not approve or stop an out-of-scope task.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {
  activitySummary,
  botAwaitingApproval,
  chatActivityLine,
  chatStatusSentence,
  displayedReachability,
  outOfScopeApprovals,
  pendingApprovalHint,
  progressPhaseText,
} from '../web-src/pure.ts';
import zhCN from '../web-src/i18n/zh-CN.ts';
import enUS from '../web-src/i18n/en-US.ts';

function translator(dict) {
  return (key, vars) => {
    let cur = dict;
    for (const part of key.split('.')) cur = cur?.[part];
    let text = String(cur ?? key);
    if (vars) {
      for (const [name, value] of Object.entries(vars)) text = text.replaceAll(`{{${name}}}`, String(value));
    }
    return text;
  };
}

const zh = translator(zhCN);
const en = translator(enUS);

test('pending approval hint counts bot work and ignores the other conversation', () => {
  const tasks = [
    {id: 'a', status: 'awaiting_approval', work_id: 'w-other'},
    {id: 'b', status: 'running', work_id: 'w-mine'},
    {id: 'c', status: 'awaiting_approval', work_id: 'w-mine'},
    {id: 'd', status: 'awaiting_approval', work_id: 'w-also'},
  ];
  const owned = new Set(['w-mine', 'w-also']);
  const awaiting = botAwaitingApproval(tasks, owned);
  assert.deepEqual(awaiting.map((task) => task.id), ['c', 'd']);
  assert.equal(pendingApprovalHint(awaiting.length, zh), '有 2 项待你批准，点下方卡片');
  assert.match(pendingApprovalHint(1, en), /1 waiting for your approval/);
  assert.equal(pendingApprovalHint(0, zh), null);
  const scope = new Set(['w-mine']);
  const elsewhere = outOfScopeApprovals(awaiting, scope);
  assert.deepEqual(elsewhere.map((task) => task.id), ['d']);
});

test('chat keeps the sticky card and renders the hint outside the stream gate', () => {
  const chat = fs.readFileSync('web-src/components/Chat.tsx', 'utf8');
  assert.match(chat, /botAwaitingApproval/);
  assert.match(chat, /pendingApprovalHint/);
  assert.match(chat, /data-testid="approval-pin"/);
  assert.match(chat, /data-testid="approval-hint"/);
  assert.match(chat, /splitPinnedTimeline/);
  const hintAt = chat.indexOf('data-testid="approval-hint"');
  const hintWindow = chat.slice(Math.max(0, hintAt - 240), hintAt + 80);
  assert.equal(hintWindow.includes('livePreviews'), false);
  assert.equal(hintWindow.includes('activity.tasks'), false);
  assert.equal(hintWindow.includes('api('), false);
  assert.match(chat, /chatStatusSentence/);
});

test('inspector points at the sticky card and does not approve out of scope', () => {
  const inspector = fs.readFileSync('web-src/components/Inspector.tsx', 'utf8');
  assert.match(inspector, /outOfScopeApprovals/);
  assert.match(inspector, /data-testid="approval-scope-hint"/);
  const hintAt = inspector.indexOf('data-testid="approval-scope-hint"');
  const hintBlock = inspector.slice(hintAt, hintAt + 500);
  assert.match(hintBlock, /approval-pin/);
  assert.equal(hintBlock.includes('ApprovalButtons'), false);
  assert.equal(hintBlock.includes('api('), false);
  assert.equal(hintBlock.includes('/cancel'), false);
  assert.match(inspector, /awaiting\.map/);
});

test('connector copy follows the phase and does not claim execution without a report', () => {
  const dispatched = activitySummary({
    status: 'running',
    started: 1_700_000_100,
    reachability: {phase: 'dispatched'},
    activity: {status: 'running', last_log_at: null, recent: [], present_outputs: []},
  }, zh);
  assert.equal(dispatched, '已派发，等待设备进展');
  assert.equal(dispatched.includes('执行中'), false);
  assert.equal(dispatched.includes('开始于'), false);
  assert.equal(dispatched.includes('启动中'), false);
  assert.equal(dispatched.includes('暂无可观测活动'), false);

  const unreachable = activitySummary({
    status: 'running',
    reachability: {phase: 'unreachable'},
  }, zh);
  assert.equal(unreachable, '暂时联系不上设备，等待重新连接');

  const active = activitySummary({
    status: 'running',
    started: 1_700_000_050,
    reachability: {phase: 'active', last_log_at: 1_700_000_100},
    activity: {
      status: 'running', last_log_at: 1_700_000_100, recent: [{tool: 'read', path: 'src/a.py'}],
      present_outputs: [],
    },
  }, zh);
  assert.match(active, /read src\/a\.py/);
  assert.match(active, /最后日志/);
  assert.equal(active.includes('已派发，等待设备进展'), false);

  assert.equal(chatActivityLine([{
    id: 'quiet', status: 'running', reachability: {phase: 'dispatched'},
    activity: {last_log_at: null, recent: []},
  }], zh), null);
  assert.equal(progressPhaseText('dispatched', zh), '已派发，等待设备进展');
  assert.equal(progressPhaseText('active', zh), null);

  const cancelling = activitySummary({
    status: 'cancel_requested',
    reachability: {phase: 'unreachable'},
    activity: {status: 'cancel_requested', last_log_at: 1_700_000_100, recent: [{tool: 'read', path: 'keep.py'}]},
  }, zh);
  assert.match(cancelling, /read keep\.py/);
  assert.equal(cancelling.includes('暂时联系不上'), false);
  assert.equal(displayedReachability({status: 'succeeded', reachability: {phase: 'unreachable'}}), null);
  assert.equal(displayedReachability({status: 'cancel_requested', reachability: {phase: 'dispatched'}}), null);
});

test('offline reachability wins over delegated in-progress copy', () => {
  const delegate = {name: '编辑', text: '去写周报'};
  const offline = chatStatusSentence({
    streaming: false,
    thinking: true,
    recovering: true,
    delegate,
    task: {status: 'running', installation_id: 'codex', prompt: '整理', reachability: {phase: 'unreachable'}},
    awaitingResult: true,
  }, zh);
  assert.equal(offline, '暂时联系不上设备，等待重新连接');
  assert.equal(offline.includes('执行中'), false);
  assert.equal(offline.includes('进行中'), false);
  const starting = chatStatusSentence({
    streaming: false, thinking: false, recovering: false, delegate,
    task: {status: 'starting', reachability: {phase: 'unreachable'}},
    awaitingResult: false,
  }, zh);
  assert.equal(starting, '暂时联系不上设备，等待重新连接');
});

test('cancel_requested and terminal copy do not fall back to in-progress', () => {
  const base = {streaming: false, thinking: true, recovering: false, delegate: {name: '编辑', text: '继续'}, awaitingResult: true};
  const cancelling = chatStatusSentence({...base, task: {status: 'cancel_requested', installation_id: 'codex', prompt: '停'}}, zh);
  assert.equal(cancelling, '正在取消');
  assert.equal(cancelling.includes('进行中'), false);
  assert.equal(cancelling.includes('执行中'), false);
  for (const [status, label] of [['succeeded', '执行成功'], ['failed', '执行失败'], ['cancelled', '已取消']]) {
    const text = chatStatusSentence({...base, task: {status, prompt: '完'}}, zh);
    assert.equal(text, label);
    assert.equal(text.includes('进行中'), false);
  }
  const quietCancel = chatActivityLine([{
    id: 'stop', status: 'cancel_requested', activity: {last_log_at: null, recent: []},
  }], zh);
  assert.equal(quietCancel, null);
  assert.notEqual(quietCancel, '进行中');
});
