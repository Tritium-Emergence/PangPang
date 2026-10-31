// Chat denseline follows task activity already on the snapshot. Status and
// revision stay put; only the projected log clock and latest step change.
import {test, expect} from '@playwright/test';
import {login} from './_studio.mjs';

const LOG_A = 1_700_000_010;
const LOG_B = 1_700_000_080;

function hms(unix) {
  const d = new Date(unix * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function runningTask(id, started, activity) {
  return {
    id, status: 'running', revision: 3, work_id: 'herbs', owner_bot_id: 'bot-main',
    installation_id: id === 'step-a' ? 'executor-newer' : 'executor-older',
    prompt: id === 'step-a' ? 'research 香草' : 'older step',
    created: started - 10, started, activity,
  };
}

let errors;
test.beforeEach(async ({page}) => {
  errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.addInitScript(() => {
    window.EventSource = class {
      close() {}
      set onopen(fn) { if (typeof fn === 'function') queueMicrotask(() => fn()); }
      set onmessage(_fn) {}
      set onerror(_fn) {}
    };
  });
});
test.afterEach(async ({page}) => {
  await page.unrouteAll({behavior: 'wait'});
  expect(errors).toEqual([]);
});

async function routeChat(page, counts, activityFor) {
  await page.route('**/api/v1/snapshot', async (route) => {
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({response});
    counts.snapshot += 1;
    const s = await response.json();
    const next = activityFor();
    s.work = [{id: 'herbs', owner_bot_id: 'bot-main', goal: '香草三步指南', kind: 'conversation', status: 'active', attention: 'working', created: 100}];
    s.turns = [];
    s.exchanges = [];
    // Snapshot tasks are oldest-first. The newer row is second on purpose.
    s.tasks = [
      runningTask('step-b', 1_690_000_000, {
        status: 'running', last_log_at: 1_690_000_001, recent: [{tool: 'list', path: 'older.md'}],
      }),
      runningTask('step-a', 1_700_000_000, {
        status: 'running', last_log_at: next.logAt, observation_capped: 'maybe',
        recent: [{tool: next.tool, path: next.path}],
        present_outputs: [{path: 'out.txt', present: true}],
      }),
    ];
    await route.fulfill({json: s});
  });
  await page.route('**/api/v1/bots/bot-main/messages', async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({response});
    counts.messages += 1;
    await route.fulfill({json: [{id: 'u-herbs', created: 1, bot_id: 'bot-main', from: 'user', kind: 'user', content: '帮我做香草三步指南', work_id: 'herbs'}]});
  });
}

test('denseline updates from activity while status and revision stay put', async ({page}) => {
  const counts = {snapshot: 0, messages: 0};
  let advanced = false;
  await routeChat(page, counts, () => advanced
    ? {logAt: LOG_B, tool: 'write', path: 'src/b.py'}
    : {logAt: LOG_A, tool: 'read', path: 'src/a.py'});
  await login(page);
  const line = page.getByTestId('chat-progress-line');
  const live = page.locator('.thinking[role="status"]');
  await expect(line).toContainText(`最后日志 ${hms(LOG_A)}`);
  await expect(line).toContainText('read src/a.py');
  await expect(line).toContainText('·+1');
  await expect(line).not.toContainText('older.md');
  await expect(line).not.toContainText('未声明必需文件');
  await expect(line).not.toContainText('日志可能已达上限');
  await expect(line).not.toContainText('已出现');
  await expect(line).toHaveAttribute('aria-hidden', 'true');
  expect(await line.evaluate((el) => el.closest('[aria-live], [role="status"]'))).toBeNull();
  await expect(live).toContainText('已派 executor-newer 执行：research 香草');
  await expect(live).not.toContainText('executor-older');
  await expect(live).not.toContainText('older step');
  const liveText = await live.innerText();
  const before = {snapshot: counts.snapshot, messages: counts.messages};
  advanced = true;
  await expect(line).toContainText(`最后日志 ${hms(LOG_B)}`, {timeout: 12000});
  await expect(line).toContainText('write src/b.py');
  await expect(line).not.toContainText('src/a.py');
  expect(await live.innerText()).toBe(liveText);
  await expect(live).not.toContainText(hms(LOG_B));
  const snapDelta = counts.snapshot - before.snapshot;
  expect(snapDelta).toBeGreaterThanOrEqual(1);
  expect(snapDelta).toBeLessThanOrEqual(2);
  expect(counts.messages).toBe(before.messages);
});

test('hiding the page stops the progress poll', async ({page}) => {
  const counts = {snapshot: 0, messages: 0};
  await routeChat(page, counts, () => ({logAt: LOG_A, tool: 'read', path: 'src/a.py'}));
  await login(page);
  await expect(page.getByTestId('chat-progress-line')).toContainText('read src/a.py');
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForTimeout(500);
  const snap = counts.snapshot;
  await page.waitForTimeout(10000);
  expect(counts.snapshot).toBe(snap);
});

test('an idle chat does not poll the snapshot', async ({page}) => {
  const counts = {snapshot: 0, messages: 0};
  await page.route('**/api/v1/snapshot', async (route) => {
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({response});
    counts.snapshot += 1;
    const s = await response.json();
    s.work = [{id: 'done', owner_bot_id: 'bot-main', goal: '完成', kind: 'conversation', status: 'delivered', attention: 'settled', created: 100}];
    s.turns = [];
    s.tasks = [];
    s.exchanges = [];
    await route.fulfill({json: s});
  });
  await page.route('**/api/v1/bots/bot-main/messages', async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({response});
    counts.messages += 1;
    await route.fulfill({json: [{id: 'u-done', created: 1, bot_id: 'bot-main', from: 'user', kind: 'user', content: '好了', work_id: 'done'}]});
  });
  await login(page);
  await expect(page.getByTestId('chat-log')).toContainText('好了');
  await expect(page.locator('.thinking')).toHaveCount(0);
  await expect.poll(() => counts.snapshot).toBeGreaterThan(0);
  await page.waitForTimeout(300);
  const snap = counts.snapshot;
  const msgs = counts.messages;
  await page.waitForTimeout(10000);
  expect(counts.snapshot).toBe(snap);
  expect(counts.messages).toBe(msgs);
});
