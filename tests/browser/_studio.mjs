// Shared helpers for the studio behavior contracts. Not a spec (filename lacks
// ".spec.") so Playwright never collects it. Helpers anchor on server truth
// (/api/v1/snapshot) + stable ids/testids/roles so they survive the shell.
import {expect} from '@playwright/test';

export const TOKEN = 'browser-access-token-32-characters';
export const BOT = {main: '胖胖', aux: '文字编辑', notify: '通知测试', approval: '审批测试', recovery: '恢复测试', cancel: '取消测试'};

export async function login(page) {
  // Pin zh-CN so the existing specs' Chinese assertions are stable regardless of
  // the test browser's language. i18n reads this key before first render.
  await page.addInitScript(() => localStorage.setItem('pangpang.uiLocale.v1', 'zh-CN'));
  await page.goto('/');
  await page.locator('#token').fill(TOKEN);
  await page.locator('#login-form button').click();
  await expect(page.locator('#bot-list .bot-item').first()).toBeVisible({timeout: 20000});
}

export function snapshot(page) {
  return page.evaluate(async () => (await fetch('/api/v1/snapshot', {credentials: 'same-origin'})).json());
}

export async function selectBot(page, name) {
  await page.locator('#bot-list .bot-item', {hasText: name}).click();
  await expect(page.locator('#bot-name')).toHaveText(name, {timeout: 10000});
}

export async function gotoWork(page) {
  await page.locator('[data-view="work"]').click();
  await expect(page.locator('#work-view')).toBeVisible();
}

// Wait until a bot has no runnable turn, so a spec that measures/sends is not
// racing background seed turns still being drained by the fixture runtime.
export async function waitQuiet(page, botId) {
  await expect.poll(async () => {
    const s = await snapshot(page);
    return (s.turns || []).filter(t => t.bot_id === botId && ['queued', 'running', 'starting'].includes(t.status)).length;
  }, {timeout: 30000}).toBe(0);
}

// A real server mutation that emits an SSE /events tick (the production background-
// refresh trigger). Issued as an in-page same-origin fetch (like the app itself), so
// the browser attaches Origin + cookie and the CSRF/Origin gate accepts it. There is
// no notification panel (unread surfaces as a bot-list badge). Opening a chat does
// not mark notifications read, so an explicit read goes through the real API.
export async function markNotificationRead(page, id) {
  const status = await page.evaluate(async (nid) => {
    const r = await fetch('/api/v1/notifications/' + nid + '/read', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()}, body: '{}',
    });
    return r.status;
  }, id);
  if (status !== 200) throw new Error('read failed ' + status);
}
