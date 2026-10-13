import assert from 'node:assert/strict';
import test from 'node:test';
import {activitySummary} from '../web-src/pure.ts';
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

test('starting is not labeled as already running', () => {
  const started = 1_700_000_100;
  const text = activitySummary({
    status: 'starting',
    created: started - 40,
    started,
    required_outputs: [],
    activity: {status: 'starting', age_s: 4, last_log_at: null, observation_capped: 'no', recent: [], present_outputs: []},
  }, zh);
  assert.match(text, new RegExp(`启动于 ${hms(started)}`));
  assert.equal(text.includes(hms(started - 40)), false);
  assert.equal(text.includes('已运行'), false);
  assert.equal(text.includes('4s'), false);
  assert.match(text, /暂无可观测活动/);
  assert.match(text, /未声明必需文件/);
  assert.equal(text.includes('尚未产出'), false);
});

test('queued uses the created clock and ignores snapshot age', () => {
  const created = 1_700_000_040;
  const text = activitySummary({
    status: 'queued',
    created,
    started: created + 80,
    required_outputs: [],
    activity: {status: 'queued', age_s: 90, last_log_at: null, observation_capped: 'no', recent: [], present_outputs: []},
  }, zh);
  assert.match(text, new RegExp(`排队于 ${hms(created)}`));
  assert.equal(text.includes(hms(created + 80)), false);
  assert.equal(text.includes('90s'), false);
  assert.equal(text.includes('已运行'), false);
});

test('running duration, absolute log time, latest step, and cap', () => {
  const stamp = Math.floor(Date.now() / 1000);
  const started = stamp - 12;
  const text = activitySummary({
    status: 'running',
    started,
    required_outputs: ['out.txt'],
    activity: {
      status: 'running', age_s: 12, last_log_at: stamp, observation_capped: 'maybe',
      recent: [{tool: 'read', path: 'src/a.py'}],
      present_outputs: [{path: 'out.txt', present: true}],
    },
  }, zh);
  assert.match(text, new RegExp(`开始于 ${hms(started)}`));
  assert.equal(text.includes('已运行'), false);
  assert.equal(text.includes('12s'), false);
  assert.match(text, new RegExp(hms(stamp)));
  assert.match(text, /最近一步 read src\/a\.py/);
  assert.match(text, /已出现 out\.txt/);
  assert.match(text, /日志可能已达上限/);
  assert.equal(text.includes('尚未产出'), false);
  assert.equal(text.includes('未声明必需文件'), false);
});

test('undeclared required files keep that copy when other outputs exist', () => {
  const text = activitySummary({
    status: 'running',
    required_outputs: [],
    outputs: ['made.txt'],
    activity: {status: 'running', age_s: 3, last_log_at: 1_700_000_000, observation_capped: 'no', recent: [{tool: 'write', path: 'made.txt'}], present_outputs: []},
  }, zh);
  assert.match(text, /未声明必需文件/);
  assert.equal(text.includes('尚未产出'), false);
  assert.equal(text.includes('暂无可观测活动'), false);
});

test('english card copy exists for the same states', () => {
  const started = 1_700_000_200;
  const starting = activitySummary({
    status: 'starting',
    started,
    required_outputs: [],
    activity: {status: 'starting', age_s: 2, last_log_at: null, observation_capped: 'no', recent: [], present_outputs: []},
  }, en);
  assert.match(starting, new RegExp(`starting at ${hms(started)}`));
  assert.match(starting, /no required files declared/);
  assert.match(starting, /no observable activity yet/);
  assert.equal(starting.toLowerCase().includes('running'), false);
  assert.equal(starting.includes('2s'), false);
  const capped = activitySummary({
    status: 'running',
    started: started + 9,
    required_outputs: ['a.txt'],
    activity: {status: 'running', age_s: 9, last_log_at: null, observation_capped: 'maybe', recent: [], present_outputs: [{path: 'a.txt', present: false}]},
  }, en);
  assert.match(capped, new RegExp(`started at ${hms(started + 9)}`));
  assert.equal(capped.includes('9s'), false);
  assert.match(capped, /log may have reached its cap/);
  assert.match(capped, /no observable activity yet/);
});
