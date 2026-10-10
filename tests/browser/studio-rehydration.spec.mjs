// Contract (client side) — a FRESH browser context (zero client state: no
// sessionStorage work-hints) rehydrates the whole UI purely from server truth. Also
// the explicit fresh-navigation smoke: a real nav + login, no
// internal functions invoked. Server-side restart persistence is covered by
// tests/test_web_restart.py (a Service reopened on the same data dir). Anchored on
// bot-list count + visible text + server truth, not CSS classes.
import {test, expect} from '@playwright/test';
import {login, selectBot, snapshot, BOT} from './_studio.mjs';

test('a fresh client rehydrates durable state (bots, quarantine card, artifacts) from server truth', async ({browser}) => {
  const ctx = await browser.newContext({baseURL: 'http://127.0.0.1:8798'});
  const page = await ctx.newPage();
  await page.goto('/');
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0); // zero client state

  await login(page);
  const s = await snapshot(page);
  await expect(page.locator('#bot-list .bot-item')).toHaveCount(s.bots.filter(b => b.lifecycle !== 'archived').length);

  // Durable quarantine survives a fresh client: the unknown recovery card re-renders.
  await selectBot(page, BOT.recovery);
  await expect(page.getByTestId('chat-log')).toContainText('需要处理', {timeout: 15000});

  // Artifacts re-render from server truth (visible, not just present in the JSON).
  await page.locator('[data-view="work"]').click();
  await expect(page.locator('#artifacts')).toContainText('季度报告', {timeout: 10000});
  await ctx.close();
});
