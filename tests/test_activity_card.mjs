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

test('starting is not labeled as already running', () => {
  const text = activitySummary({
    status: 'starting',
    required_outputs: [],
    activity: {status: 'starting', age_s: 4, last_log_at: null, observation_capped: 'no', recent: [], present_outputs: []},
  }, zh);
  assert.match(text, /启动中 4s/);
  assert.equal(text.includes('已运行'), false);
  assert.match(text, /暂无可观测活动/);
  assert.match(text, /未声明必需文件/);
  assert.equal(text.includes('尚未产出'), false);
});

test('running duration, absolute log time, latest step, and cap', () => {
  const stamp = Math.floor(Date.now() / 1000);
  const clock = new Date(stamp * 1000);
  const hh = String(clock.getHours()).padStart(2, '0');
  const mm = String(clock.getMinutes()).padStart(2, '0');
  const ss = String(clock.getSeconds()).padStart(2, '0');
  const text = activitySummary({
    status: 'running',
    required_outputs: ['out.txt'],
    activity: {
      status: 'running', age_s: 12, last_log_at: stamp, observation_capped: 'maybe',
      recent: [{tool: 'read', path: 'src/a.py'}],
      present_outputs: [{path: 'out.txt', present: true}],
    },
  }, zh);
  assert.match(text, /已运行 12s/);
  assert.match(text, new RegExp(`${hh}:${mm}:${ss}`));
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
  const starting = activitySummary({
    status: 'starting',
    required_outputs: [],
    activity: {status: 'starting', age_s: 2, last_log_at: null, observation_capped: 'no', recent: [], present_outputs: []},
  }, en);
  assert.match(starting, /starting 2s/);
  assert.match(starting, /no required files declared/);
  assert.match(starting, /no observable activity yet/);
  assert.equal(starting.toLowerCase().includes('running'), false);
  const capped = activitySummary({
    status: 'running',
    required_outputs: ['a.txt'],
    activity: {status: 'running', age_s: 9, last_log_at: null, observation_capped: 'maybe', recent: [], present_outputs: [{path: 'a.txt', present: false}]},
  }, en);
  assert.match(capped, /running 9s/);
  assert.match(capped, /log may have reached its cap/);
  assert.match(capped, /no observable activity yet/);
});
