// Contract — 390×844 phone: wide chat content (markdown table, code fence, long
// unbroken id/URL, executor result card) never pans sideways, and an executor
// result card shows a human line, never raw JSON. The scroll container is
// #messages (html/body are overflow:hidden). Code and tables wrap, so they are
// not scrollports either.
import {test, expect} from '@playwright/test';
import {project} from './_parity.mjs';
import {login} from './_studio.mjs';

const msg = (id, created, content, extra = {}) => ({id, created, bot_id: 'bot-main', from: 'bot-main', kind: 'assistant', content, ...extra});
const TABLE = '| 交易类型 | 次数 | 胜率 | 平均盈亏 | 风险说明 | 备注 |\n|---|---|---|---|---|---|\n' +
  '| 高频短线 | 128 | 41% | -0.8% | 交易频率高，手续费侵蚀较大；情绪化交易占比较大 | 期权亏损集中在短线 |\n' +
  '| 股票波段 | 36 | 58% | +4.2% | 主要贡献来源，但样本仍偏少，不宜当作历史稳定状态 | 看作 alpha 来源 |\n';
const TASK = 'task-7763b48c499b44f0a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6';
const FAIL = {task_id: TASK, status: 'failed', error: 'required_output_missing', artifact_refs: [], missing_outputs: ['out/portrait.png', 'out/report.md'],
  notice: '执行结果以本 status 为准：failed。原始执行器输出仅供 operator 检查，不进入 Bot 上下文。exit=0 也不代表业务验收通过。｜耗时以 timing 为权威：executor_s=执行器实际运行秒数，total_s=从提交到现在的总秒数（含排队/重试）。',
  timing: {executor_s: 91.2, total_s: 94.6}};
const OK = {task_id: 'task-ok1', status: 'succeeded', error: null, artifact_refs: [], notice: '执行结果以本 status 为准：succeeded。',
  result_text: '{"task_id":"task-ok1","notice":"执行结果以本 status 为准：succeeded。","timing":{"executor_s":12.3,"total_s":15}}',
  result_text_truncated: false, timing: {executor_s: 12.3, total_s: 15}};
const MESSAGES = [
  msg('u1', 1, '你能用 finland-linux 上的 codex 去画一幅交易者画像吗？', {kind: 'user', from: 'user'}),
  msg('a1', 2, '分析如下：\n\n' + TABLE + '\n结论：**你没有**稳定的短线优势。'),
  msg('a2', 3, '链接 https://example.com/' + 'a'.repeat(120) + ' 与 id ' + TASK + TASK + '\n```\n' + '{"k":"' + 'x'.repeat(160) + '"}\n```'),
  msg('a2b', 3.5, '```\n' + 'only-code '.repeat(3) + 'x'.repeat(200) + '\n```'),
  msg('a3', 4, '这是在刚才分析基础上新增的绘图任务。'),
  msg('e1', 5, JSON.stringify(FAIL), {kind: 'execution_result', from: 'executor'}),
  msg('e2', 6, JSON.stringify(OK), {kind: 'execution_result', from: 'executor'}),
];

async function measure(page) {
  return page.evaluate(() => {
    const log = document.querySelector('#messages');
    const describe = (el) => el.tagName.toLowerCase() + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '');
    const over = [];
    for (const el of [log, ...log.querySelectorAll('*')]) {
      if (el.scrollWidth > el.clientWidth + 1) over.push({el: describe(el), sw: el.scrollWidth, cw: el.clientWidth});
    }
    const pans = [];
    for (const el of [document.documentElement, log, ...log.querySelectorAll('pre, .md-table-scroll')]) {
      el.scrollLeft = 10000;
      if (el.scrollLeft > 0) pans.push({el: describe(el), left: el.scrollLeft, sw: el.scrollWidth, cw: el.clientWidth});
    }
    const doc = document.documentElement;
    return {
      doc: {sw: doc.scrollWidth, cw: doc.clientWidth, left: doc.scrollLeft},
      messages: {sw: log.scrollWidth, cw: log.clientWidth, left: log.scrollLeft},
      blocks: [...log.querySelectorAll('pre, .md-table-scroll')].map((e) => ({el: describe(e), sw: e.scrollWidth, cw: e.clientWidth, left: e.scrollLeft})),
      over: over.slice(0, 20), overCount: over.length, pans,
    };
  });
}

test('wide chat content wraps, and the transcript cannot pan sideways', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await project(page, (s) => { s.tasks = []; }, MESSAGES);
  await login(page);
  const log = page.getByTestId('chat-log');
  await expect(log).toContainText('交易者画像');
  await expect(log).toContainText('高频短线');
  await expect(log).toContainText('x'.repeat(80));
  await expect(log.locator('[data-kind="execution_result"]').first()).toBeVisible();
  const m = await measure(page);
  expect(m.over, JSON.stringify(m.over)).toEqual([]);
  expect(m.pans, JSON.stringify(m.pans)).toEqual([]);
  expect(m.messages.sw).toBeLessThanOrEqual(m.messages.cw + 1);
  expect(m.messages.left).toBe(0);
  expect(m.doc.sw).toBeLessThanOrEqual(m.doc.cw + 1);
  expect(m.doc.left).toBe(0);
  expect(m.blocks.length).toBeGreaterThan(0);
  for (const block of m.blocks) {
    expect(block.sw, JSON.stringify(block)).toBeLessThanOrEqual(block.cw + 1);
    expect(block.left, JSON.stringify(block)).toBe(0);
  }
  await page.unrouteAll({behavior: 'ignoreErrors'});
});

test('executor result cards show a human line, never raw JSON', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await project(page, (s) => { s.tasks = []; }, MESSAGES);
  await login(page);
  const log = page.getByTestId('chat-log');
  const cards = log.locator('[data-kind="execution_result"]');
  await expect(cards).toHaveCount(2);
  await expect(cards.locator('pre, .evidence-raw')).toHaveCount(0);
  for (const i of [0, 1]) for (const raw of ['"task_id"', '"notice"', '"timing"', '{', 'required_output_missing', '执行结果以本 status 为准']) await expect(cards.nth(i)).not.toContainText(raw);
  await expect(cards.nth(0)).toContainText('执行失败：缺少要求的产出文件');
  await expect(cards.nth(0)).toContainText('缺少产出物：out/portrait.png、out/report.md');
  await expect(page.locator('#messages .evidence-raw')).toHaveCount(0);
  await expect(cards.nth(1)).toContainText('执行成功');
  await expect(cards.nth(1)).not.toContainText('executor_s');
  await expect(cards.nth(1)).not.toContainText('task-ok1');
  await page.unrouteAll({behavior: 'ignoreErrors'});
});

test('approval and recovery cards wrap long URLs and do not pan the transcript', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  const url = 'https://example.com/' + 'u'.repeat(200);
  await project(page, (s) => {
    s.work = s.work || [];
    s.tasks = s.tasks || [];
    s.work.push({id: 'overflow-work', owner_bot_id: 'bot-main', goal: url, kind: 'conversation', attention: 'working', status: 'active'});
    s.tasks.push({id: 'approval-long-url', work_id: 'overflow-work', owner_bot_id: 'bot-main', status: 'awaiting_approval', created: 50, prompt: url, installation_id: 'codex'});
    s.tasks.push({id: 'recovery-long-url', work_id: 'overflow-work', owner_bot_id: 'bot-main', status: 'unknown', created: 51, prompt: url});
  }, [msg('u1', 1, '确认一下', {kind: 'user', from: 'user'})]);
  await login(page);
  const log = page.getByTestId('chat-log');
  await expect(log.locator('.tcard.awaiting', {hasText: url.slice(0, 40)})).toBeVisible();
  await expect(log.locator('.bubble.warning[data-task="recovery-long-url"]')).toContainText(url.slice(0, 40));
  const m = await measure(page);
  expect(m.over, JSON.stringify(m.over)).toEqual([]);
  expect(m.pans, JSON.stringify(m.pans)).toEqual([]);
  expect(m.messages.sw).toBeLessThanOrEqual(m.messages.cw + 1);
  expect(m.messages.left).toBe(0);
  expect(m.doc.sw).toBeLessThanOrEqual(m.doc.cw + 1);
  expect(m.doc.left).toBe(0);
  await page.unrouteAll({behavior: 'ignoreErrors'});
});
