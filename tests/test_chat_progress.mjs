// Chat denseline and the soft-refresh gate. Imports the production helpers.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {
  CHAT_PROGRESS_PATH_MAX,
  CHAT_PROGRESS_REFRESH_MS,
  chatActivityLine,
  chatProgressRefreshDue,
  mergeRefreshWant,
  pickChatProgressTask,
  refreshWant,
  truncateChatProgressPath,
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

function hms(unix) {
  const d = new Date(unix * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function task(id, extra = {}) {
  return {id, status: 'running', started: 1_700_000_000, created: 1_699_000_000, ...extra};
}

test('denseline is last log time plus the latest closed-set step', () => {
  const logAt = 1_700_000_010;
  const text = chatActivityLine([task('run', {
    activity: {
      status: 'running',
      last_log_at: logAt,
      observation_capped: 'maybe',
      recent: [{tool: 'read', path: 'src/a.py'}, {tool: 'write', path: 'src/b.py'}],
      present_outputs: [{path: 'out.txt', present: true}],
    },
    required_outputs: [],
  })], zh);
  assert.equal(text, `最后日志 ${hms(logAt)} · write src/b.py`);
  assert.equal(text.includes('未声明必需文件'), false);
  assert.equal(text.includes('日志可能已达上限'), false);
  assert.equal(text.includes('已出现'), false);
  assert.equal(text.includes('暂无可观测活动'), false);
  assert.equal(text.includes('src/a.py'), false);
});

test('a later last_log_at changes the line when status is unchanged', () => {
  const base = {status: 'running', recent: [{tool: 'read', path: 'notes.md'}]};
  const first = chatActivityLine([task('run', {activity: {...base, last_log_at: 1_700_000_010}})], zh);
  const second = chatActivityLine([task('run', {activity: {...base, last_log_at: 1_700_000_011}})], zh);
  assert.notEqual(first, second);
  assert.match(first, new RegExp(hms(1_700_000_010)));
  assert.match(second, new RegExp(hms(1_700_000_011)));
  assert.match(second, /read notes\.md/);
});

test('english copy and a step with no path', () => {
  const logAt = 1_700_000_020;
  const text = chatActivityLine([task('run', {
    activity: {last_log_at: logAt, recent: [{tool: 'execute'}]},
  })], en);
  assert.equal(text, `last log ${hms(logAt)} · execute`);
  assert.equal(text.includes('no required files'), false);
  assert.equal(text.includes('log may have reached its cap'), false);
});

test('missing activity keeps the coarse sentence by returning no line', () => {
  assert.equal(chatActivityLine([task('run')], zh), null);
  assert.equal(chatActivityLine([task('run', {activity: null})], zh), null);
  // No recent array is not the empty-list startup gap.
  assert.equal(chatActivityLine([task('run', {activity: {last_log_at: null}})], zh), null);
  assert.equal(chatActivityLine([task('quiet'), task('loud', {
    started: 1_600_000_000,
    activity: {last_log_at: 1_700_000_030, recent: [{tool: 'read', path: 'secret.py'}]},
  })], zh), null);
});

test('running with an empty recent list past startup names a hidden tool tail', () => {
  const logAt = 1_700_000_010;
  const zhText = chatActivityLine([task('run', {
    activity: {status: 'running', last_log_at: logAt, recent: []},
  })], zh);
  assert.equal(zhText, `最后日志 ${hms(logAt)} · 运行中 / 工具暂不可见`);
  assert.equal(zhText.includes('等待首条工具'), false);
  assert.equal(zhText.includes('暂无可观测活动'), false);
  assert.equal(zhText.includes('{{'), false);
  const enText = chatActivityLine([task('run', {
    activity: {last_log_at: logAt, recent: []},
  })], en);
  assert.equal(enText, `last log ${hms(logAt)} · running / tools temporarily not visible`);
  assert.equal(enText.includes('waiting for the first tool'), false);
  assert.equal(enText.includes('{{'), false);
  // No log, no cap, no output: nothing says the run got past startup, so this
  // stays the true early-waiting copy.
  assert.equal(chatActivityLine([task('run', {
    activity: {last_log_at: null, recent: []},
  })], zh), '启动中 / 等待首条工具');
  const extra = chatActivityLine([
    task('older', {started: 1_600_000_000, activity: {last_log_at: logAt, recent: [{tool: 'read', path: 'old.py'}]}}),
    task('run', {activity: {last_log_at: logAt, recent: []}}),
  ], zh);
  assert.equal(extra, `最后日志 ${hms(logAt)} · 运行中 / 工具暂不可见 ·+1`);
  assert.equal(extra.includes('old.py'), false);
  assert.equal(extra.includes('read'), false);
});

test('empty recent with delivered outputs shows them alongside the honest running copy', () => {
  const logAt = 1_700_000_010;
  const text = chatActivityLine([task('run', {
    activity: {
      status: 'running',
      last_log_at: logAt,
      recent: [],
      present_outputs: [
        {path: 'missing.txt', present: false},
        {path: 'out.txt', present: true},
      ],
    },
  })], zh);
  assert.equal(text, `最后日志 ${hms(logAt)} · 运行中 / 工具暂不可见 · ${zh('workpane.activityPresent', {files: 'out.txt'})}`);
  assert.equal(text.includes('等待首条工具'), false);
  assert.equal(text.includes('启动中'), false);
  assert.equal(text.includes('missing.txt'), false);
  assert.equal(text.includes('{'), false);
  assert.equal(text.includes('['), false);
  // Delivered files are never the only progress signal.
  assert.notEqual(text, zh('workpane.activityPresent', {files: 'out.txt'}));
  const alone = chatActivityLine([task('run', {
    activity: {last_log_at: null, recent: [], present_outputs: [{path: 'notes.md', present: true}]},
  })], zh);
  assert.equal(alone, `运行中 / 工具暂不可见 · ${zh('workpane.activityPresent', {files: 'notes.md'})}`);
  assert.equal(alone.includes('等待首条工具'), false);
  const enText = chatActivityLine([task('run', {
    activity: {
      last_log_at: logAt,
      recent: [],
      present_outputs: [{path: 'out.txt', present: true}, {path: 'b.txt', present: true}],
    },
  })], en);
  assert.equal(enText, `last log ${hms(logAt)} · running / tools temporarily not visible · ${en('workpane.activityPresent', {files: 'out.txt, b.txt'})}`);
  assert.equal(enText.includes('waiting for the first tool'), false);
  assert.equal(enText.includes('starting /'), false);
  assert.equal(enText.includes('{'), false);
  const absent = chatActivityLine([task('run', {
    activity: {last_log_at: null, recent: [], present_outputs: [{path: 'a.txt', present: false}]},
  })], zh);
  assert.equal(absent, '启动中 / 等待首条工具');
  const long = 'dir/'.repeat(20) + 'file.py';
  const cut = truncateChatProgressPath(long);
  const truncated = chatActivityLine([task('run', {
    activity: {recent: [], present_outputs: [{path: long, present: true}, {path: 'a\nb.py', present: true}]},
  })], zh);
  assert.equal(truncated, `运行中 / 工具暂不可见 · ${zh('workpane.activityPresent', {files: cut})}`);
  assert.equal(truncated.includes('等待首条工具'), false);
  assert.equal(chatActivityLine([task('run', {
    activity: {recent: [], present_outputs: [{path: 'a\nb.py', present: true}]},
  })], zh), '启动中 / 等待首条工具');
  const extra = chatActivityLine([
    task('older', {started: 1_600_000_000, activity: {recent: [{tool: 'read', path: 'old.py'}]}}),
    task('run', {activity: {recent: [], present_outputs: [{path: 'out.txt', present: true}]}}),
  ], zh);
  assert.equal(extra, `运行中 / 工具暂不可见 · ${zh('workpane.activityPresent', {files: 'out.txt'})} ·+1`);
  assert.equal(extra.includes('等待首条工具'), false);
  assert.equal(extra.includes('old.py'), false);
});

test('a capped observation with an empty recent list is honest, not frozen', () => {
  const logAt = 1_700_000_010;
  const text = chatActivityLine([task('run', {
    activity: {
      status: 'running',
      last_log_at: logAt,
      observation_capped: 'maybe',
      recent: [],
      present_outputs: [{path: 'out.txt', present: true}],
    },
  })], zh);
  assert.equal(text, `运行中 / 工具暂不可见 · ${zh('workpane.activityCapped')} · ${zh('workpane.activityPresent', {files: 'out.txt'})}`);
  // The frozen last_log_at is not presented as liveness while capped.
  assert.equal(text.includes('最后日志'), false);
  assert.equal(text.includes(hms(logAt)), false);
  assert.equal(text.includes('运行中'), true);
  assert.equal(text.includes('工具暂不可见'), true);
  assert.equal(text.includes(zh('workpane.activityCapped')), true);
  assert.equal(text.includes('已出现 out.txt'), true);
  // Never looks like only the old stuck line: outputs alone or clock + outputs.
  assert.notEqual(text, zh('workpane.activityPresent', {files: 'out.txt'}));
  const frozen = chatActivityLine([task('run', {
    activity: {last_log_at: logAt, recent: [], present_outputs: [{path: 'out.txt', present: true}]},
  })], zh);
  assert.equal(frozen, `最后日志 ${hms(logAt)} · 运行中 / 工具暂不可见 · ${zh('workpane.activityPresent', {files: 'out.txt'})}`);
  assert.notEqual(text, frozen);

  const enText = chatActivityLine([task('run', {
    activity: {
      last_log_at: logAt,
      observation_capped: 'maybe',
      recent: [],
      present_outputs: [{path: 'out.txt', present: true}],
    },
  })], en);
  assert.equal(enText, `running / tools temporarily not visible · ${en('workpane.activityCapped')} · ${en('workpane.activityPresent', {files: 'out.txt'})}`);
  assert.equal(enText.includes('last log'), false);
  assert.equal(enText.includes('waiting for the first tool'), false);

  // Capped without outputs still says the log is capped and drops the clock.
  const bare = chatActivityLine([task('run', {
    activity: {last_log_at: logAt, observation_capped: 'maybe', recent: []},
  })], zh);
  assert.equal(bare, `运行中 / 工具暂不可见 · ${zh('workpane.activityCapped')}`);
  assert.equal(bare.includes('最后日志'), false);
  assert.equal(bare.includes('等待首条工具'), false);
});

test('the denseline activity shape carries the existing cap flag', () => {
  const pure = fs.readFileSync('web-src/pure.ts', 'utf8');
  const start = pure.indexOf('export interface ChatProgressTask');
  const end = pure.indexOf('function finiteClock', start);
  assert.ok(start > 0 && end > start);
  assert.match(pure.slice(start, end), /observation_capped\?: string;/);
});

test('a real step, a non-running status, or a missing recent list omits the waiting phrase', () => {
  const logAt = 1_700_000_010;
  const withTool = chatActivityLine([task('run', {
    activity: {last_log_at: logAt, recent: [{tool: 'read', path: 'src/a.py'}]},
  })], zh);
  assert.equal(withTool, `最后日志 ${hms(logAt)} · read src/a.py`);
  assert.equal(withTool.includes('等待首条工具'), false);
  assert.equal(withTool.includes('启动中'), false);
  const unknown = chatActivityLine([task('run', {
    activity: {last_log_at: logAt, recent: [{tool: 'Bash', path: 'skip.py'}]},
  })], zh);
  assert.equal(unknown, `最后日志 ${hms(logAt)}`);
  assert.equal(unknown.includes('skip.py'), false);
  assert.equal(unknown.includes('Bash'), false);
  assert.equal(unknown.includes('等待首条工具'), false);
  assert.equal(chatActivityLine([task('run', {
    activity: {last_log_at: null, recent: [{tool: 'Bash', path: 'skip.py'}]},
  })], zh), null);
  assert.equal(chatActivityLine([task('run', {activity: {recent: [{}]}})], zh), null);
  for (const status of ['queued', 'starting', 'cancel_requested', 'succeeded', 'failed', 'cancelled']) {
    const clockOnly = chatActivityLine([task('run', {
      status,
      activity: {last_log_at: logAt, recent: []},
    })], zh);
    assert.equal(clockOnly, `最后日志 ${hms(logAt)}`);
    assert.equal(clockOnly.includes('等待首条工具'), false);
    assert.equal(chatActivityLine([task('run', {
      status,
      activity: {last_log_at: null, recent: []},
    })], zh), null);
  }
  assert.equal(chatActivityLine([task('run', {
    activity: {status: 'running', last_log_at: logAt},
  })], zh), `最后日志 ${hms(logAt)}`);
  assert.equal(chatActivityLine([task('run', {
    activity: {status: 'running', last_log_at: null},
  })], zh), null);
  assert.equal(chatActivityLine([task('done', {
    status: 'cancelled',
    activity: {last_log_at: null, recent: []},
  })], zh), null);
});

test('names outside the closed set are skipped', () => {
  const text = chatActivityLine([task('run', {
    activity: {last_log_at: null, recent: [{tool: 'read', path: 'ok.py'}, {tool: 'Bash', path: 'skip.py'}]},
  })], zh);
  assert.equal(text, 'read ok.py');
  assert.equal(text.includes('Bash'), false);
  assert.equal(text.includes('skip.py'), false);
});

test('paths are truncated and control characters drop the path', () => {
  const long = 'dir/'.repeat(20) + 'file.py';
  assert.ok(long.length > CHAT_PROGRESS_PATH_MAX);
  const cut = truncateChatProgressPath(long);
  assert.equal(cut.length, CHAT_PROGRESS_PATH_MAX);
  assert.equal(cut.startsWith('…'), true);
  assert.equal(cut.endsWith('file.py'), true);
  const text = chatActivityLine([task('run', {
    activity: {last_log_at: null, recent: [{tool: 'edit', path: long}]},
  })], en);
  assert.equal(text, `edit ${cut}`);
  assert.equal(chatActivityLine([task('run', {
    activity: {recent: [{tool: 'read', path: 'a\nb.py'}]},
  })], zh), 'read');
  assert.equal(truncateChatProgressPath('src\\a.py'), 'src/a.py');
  assert.equal(truncateChatProgressPath('src\\\\nested\\b.py'), 'src//nested/b.py');
  assert.equal(chatActivityLine([task('run', {
    activity: {recent: [{tool: 'read', path: 'src\\a.py'}]},
  })], zh), 'read src/a.py');
});

test('oldest-first snapshot order still assigns the status subject and the line to one task', () => {
  const older = task('step-b', {
    started: 1_690_000_000,
    created: 1_689_999_990,
    installation_id: 'executor-older',
    prompt: 'older step',
    activity: {last_log_at: 1_690_000_001, recent: [{tool: 'list', path: 'older.md'}]},
  });
  const newer = task('step-a', {
    started: 1_700_000_000,
    created: 1_699_999_990,
    installation_id: 'executor-newer',
    prompt: 'research 香草',
    activity: {last_log_at: 1_700_000_010, recent: [{tool: 'read', path: 'src/a.py'}]},
  });
  const tasks = [older, newer];
  assert.ok(tasks[0].created < tasks[1].created);
  const picked = pickChatProgressTask(tasks);
  const line = chatActivityLine(tasks, zh);
  assert.equal(picked.task.id, 'step-a');
  assert.notEqual(picked.task.id, tasks[0].id);
  assert.equal(picked.task.installation_id, 'executor-newer');
  assert.equal(picked.task.prompt, 'research 香草');
  assert.match(line, /read src\/a\.py ·\+1$/);
  assert.equal(line.includes('older.md'), false);
  assert.equal(line.includes('executor-older'), false);
});

test('primary task is newest started, then id, and does not follow the log clock', () => {
  const older = task('b', {started: 100, activity: {last_log_at: 9_000, recent: [{tool: 'read', path: 'old.py'}]}});
  const newer = task('a', {started: 200, activity: {last_log_at: 150, recent: [{tool: 'write', path: 'new.py'}]}});
  const tiedLate = task('m', {started: 200, created: 50, activity: {last_log_at: 8_000, recent: [{tool: 'list', path: 'late.py'}]}});
  const tiedEarly = task('c', {started: 200, created: 999, activity: {last_log_at: 1, recent: [{tool: 'search', path: 'early.py'}]}});
  assert.equal(pickChatProgressTask([older, newer]).task.id, 'a');
  assert.equal(pickChatProgressTask([newer, older]).task.id, 'a');
  assert.equal(pickChatProgressTask([tiedLate, tiedEarly, older]).task.id, 'c');
  assert.equal(pickChatProgressTask([tiedEarly, tiedLate, older]).task.id, 'c');
  const queued = task('q', {started: undefined, created: 500, activity: {recent: [{tool: 'fetch', path: 'q.py'}]}});
  assert.equal(pickChatProgressTask([older, queued]).task.id, 'q');
  const line = chatActivityLine([older, newer], zh);
  assert.match(line, /write new\.py ·\+1$/);
  assert.equal(line.includes('old.py'), false);
  const moved = chatActivityLine([
    {...older, activity: {...older.activity, last_log_at: 99_000}},
    newer,
  ], zh);
  assert.match(moved, /write new\.py ·\+1$/);
});

test('soft refresh is bounded and snapshot-only does not pull messages', () => {
  assert.equal(CHAT_PROGRESS_REFRESH_MS >= 5_000 && CHAT_PROGRESS_REFRESH_MS <= 10_000, true);
  const shots = Math.floor(60_000 / CHAT_PROGRESS_REFRESH_MS);
  assert.equal(shots <= 12, true);
  assert.equal(shots >= 6, true);
  assert.equal(chatProgressRefreshDue({visible: false, hasProgressWork: true, activeTaskCount: 4}), false);
  assert.equal(chatProgressRefreshDue({visible: true, hasProgressWork: false, activeTaskCount: 0}), false);
  assert.equal(chatProgressRefreshDue({visible: true, hasProgressWork: true, activeTaskCount: 0}), true);
  assert.equal(chatProgressRefreshDue({visible: true, hasProgressWork: false, activeTaskCount: 1}), true);
  assert.deepEqual(refreshWant('snapshot'), {snapshot: true, messages: false});
  assert.deepEqual(refreshWant('messages'), {snapshot: false, messages: true});
  assert.deepEqual(refreshWant('all'), {snapshot: true, messages: true});
  let pending = {snapshot: false, messages: false};
  for (let i = 0; i < 20; i++) pending = mergeRefreshWant(pending, 'snapshot');
  assert.deepEqual(pending, {snapshot: true, messages: false});
  pending = mergeRefreshWant(pending, 'all');
  assert.deepEqual(pending, {snapshot: true, messages: true});
});

test('empty chat copy asks for a working agent before tasks can be assigned', () => {
  assert.equal(zh('chat.emptyTitle'), '先接上一个能干活的 Agent。');
  assert.equal(zh('chat.emptyHint'), '接好之后，任务才能自动分配。');
  assert.equal(en('chat.emptyTitle'), 'Connect a working Agent first.');
  assert.equal(en('chat.emptyHint'), 'Only then can tasks be assigned automatically.');
  assert.equal(zh('chat.emptyTitle').includes('先聊聊'), false);
  assert.equal(en('chat.emptyTitle').includes('Tell me what you want'), false);
});

test('the empty hint stretches the live region only while the chat is empty', () => {
  const chat = fs.readFileSync('web-src/components/Chat.tsx', 'utf8');
  assert.match(chat, /className=\{showEmpty \? 'h-full' : undefined\}/);
  assert.equal(chat.includes('aria-live="polite" className="h-full"'), false);
  const liveAt = chat.indexOf('aria-live="polite"');
  const noteAt = chat.indexOf('Outside the live region');
  assert.equal(chat.slice(liveAt, noteAt).includes('data-testid="chat-progress-line"'), false);
});

test('the progress clock is outside the live region and previews still suppress the bubble', () => {
  const chat = fs.readFileSync('web-src/components/Chat.tsx', 'utf8');
  const liveAt = chat.indexOf('aria-live="polite"');
  const noteAt = chat.indexOf('Outside the live region');
  const lineAt = chat.indexOf('data-testid="chat-progress-line"');
  assert.ok(liveAt > 0 && liveAt < noteAt && noteAt < lineAt);
  assert.equal(chat.slice(liveAt, noteAt).includes('chat-progress-line'), false);
  assert.match(chat, /aria-hidden="true"/);
  assert.match(chat, /streaming: livePreviews\.length > 0/);
  const pure = fs.readFileSync('web-src/pure.ts', 'utf8');
  assert.match(pure, /if \(input\.streaming\) return '';/);
  assert.match(chat, /progress && progressLine/);
  assert.match(chat, /pickChatProgressTask\(activity\.tasks\)/);
  assert.equal(chat.includes('activity.tasks[0]'), false);
  assert.match(chat, /refreshSnapshot\('snapshot'\)/);
  assert.match(chat, /visibilitychange/);
  const store = fs.readFileSync('web-src/store.ts', 'utf8');
  assert.match(store, /refreshWant\(scope\)/);
  assert.equal(store.includes('loadMessages') && store.includes("want.messages"), true);
});
