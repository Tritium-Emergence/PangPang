// The empty hint centers with h-full under #messages. An auto-height aria-live
// wrapper between them collapses that percentage, so the hint sticks to the top.
// Stretching the wrapper whenever the progress line is also showing overflows the
// scroller by that line (~28px); the line stays outside the live region.
import {test, expect} from '@playwright/test';
import {login} from './_studio.mjs';

let errors;
test.beforeEach(async ({page}) => {
  errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
});
test.afterEach(async ({page}) => {
  await page.unrouteAll({behavior: 'wait'});
  expect(errors).toEqual([]);
});

async function routeEmpty(page) {
  await page.route('**/api/v1/snapshot', async (route) => {
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({response});
    const s = await response.json();
    s.work = (s.work || []).map((w) => ({...w, attention: 'settled'}));
    s.turns = [];
    s.tasks = [];
    s.exchanges = [];
    await route.fulfill({json: s});
  });
  await page.route('**/api/v1/bots/bot-main/messages', async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    await route.fulfill({json: []});
  });
}

function readEmpty(messages) {
  const grid = messages.querySelector('[data-testid="chat-empty"]');
  const heading = grid.querySelector('h2');
  const hint = grid.querySelector('p');
  const cs = getComputedStyle(messages);
  const padTop = parseFloat(cs.paddingTop);
  const padBottom = parseFloat(cs.paddingBottom);
  const host = messages.getBoundingClientRect();
  const contentTop = host.top + padTop;
  const contentHeight = messages.clientHeight - padTop - padBottom;
  const blockTop = heading.getBoundingClientRect().top;
  const blockBottom = hint.getBoundingClientRect().bottom;
  const gridBox = grid.getBoundingClientRect();
  const line = messages.querySelector('[data-testid="chat-progress-line"]');
  return {
    emptyHeight: gridBox.height,
    contentHeight,
    topGap: gridBox.top - contentTop,
    bottomGap: (host.bottom - padBottom) - gridBox.bottom,
    centerDelta: (blockTop + blockBottom) / 2 - (contentTop + contentHeight / 2),
    overflow: messages.scrollHeight - messages.clientHeight,
    lineInLive: line ? line.closest('[aria-live]') !== null : false,
  };
}

test('empty chat hint fills the transcript and sits on the vertical center', async ({page}) => {
  await page.setViewportSize({width: 1280, height: 800});
  await routeEmpty(page);
  await login(page);
  const empty = page.getByTestId('chat-empty');
  await expect(empty).toBeVisible();
  // Collapsed percentage height is the text block (~62px) against a ~556px transcript.
  const zh = await page.locator('#messages').evaluate(readEmpty);
  expect(zh.contentHeight).toBeGreaterThan(400);
  expect(Math.abs(zh.emptyHeight - zh.contentHeight)).toBeLessThanOrEqual(1);
  expect(Math.abs(zh.topGap)).toBeLessThanOrEqual(1);
  expect(Math.abs(zh.bottomGap)).toBeLessThanOrEqual(1);
  expect(Math.abs(zh.centerDelta)).toBeLessThanOrEqual(2);
  expect(zh.overflow).toBeLessThanOrEqual(1);
  expect(zh.lineInLive).toBe(false);
  await expect(empty.locator('h2')).toHaveText('先接上一个能干活的 Agent。');
  await expect(empty.locator('p')).toHaveText('接好之后，任务才能自动分配。');
  await expect(empty).not.toContainText('先聊聊你想完成什么');
  await expect(empty).not.toContainText('直接说要做什么');

  await page.locator('#lang-toggle').click();
  await expect(empty.locator('h2')).toHaveText('Connect a working Agent first.');
  await expect(empty.locator('p')).toHaveText('Only then can tasks be assigned automatically.');
  await expect(empty).not.toContainText('Tell me what you want');
  const en = await page.locator('#messages').evaluate(readEmpty);
  expect(Math.abs(en.emptyHeight - en.contentHeight)).toBeLessThanOrEqual(1);
  expect(Math.abs(en.centerDelta)).toBeLessThanOrEqual(2);
  expect(en.overflow).toBeLessThanOrEqual(1);

  await page.setViewportSize({width: 390, height: 844});
  const mobile = await page.locator('#messages').evaluate(readEmpty);
  expect(mobile.contentHeight).toBeGreaterThan(300);
  expect(Math.abs(mobile.emptyHeight - mobile.contentHeight)).toBeLessThanOrEqual(1);
  expect(Math.abs(mobile.centerDelta)).toBeLessThanOrEqual(2);
  expect(mobile.overflow).toBeLessThanOrEqual(1);
});
