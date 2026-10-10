// Contract #4 — a background refresh (SSE-triggered) keeps the chat scroll position
// stable AND does not rebuild the message DOM when the message set is unchanged
// Trigger: a REPEATABLE background mutation (create a project via
// an in-page same-origin fetch) that emits an /events tick but does not touch the
// chat's messages. Proof the refresh ran: the client re-fetches /snapshot. Proof of
// no-rebuild: a tagged existing node survives (a replaceChildren would destroy it).
import {test, expect} from '@playwright/test';
import {login, selectBot, waitQuiet, BOT} from './_studio.mjs';

test('background refresh keeps scroll stable and does not rebuild the message DOM', async ({page}) => {
  await page.setViewportSize({width: 1100, height: 500});
  await login(page);
  await selectBot(page, BOT.main);
  await waitQuiet(page, 'bot-main');

  const log = page.getByTestId('chat-log');
  const scrollable = await log.evaluate(e => { e.scrollTop = 80; return e.scrollHeight > e.clientHeight; });
  expect(scrollable).toBeTruthy();
  const before = await log.evaluate(e => e.scrollTop);
  expect(before).toBeGreaterThan(0);
  // Tag an existing message node; a rebuild (replaceChildren) would destroy it.
  expect(await log.evaluate(e => { const b = e.firstElementChild; if (!b) return false; b.setAttribute('data-probe', 'keep'); return true; })).toBeTruthy();
  // Count the client's own /snapshot fetches so we can prove a refresh actually ran.
  await page.evaluate(() => { window.__snap = 0; const o = window.fetch;
    window.fetch = (...a) => { if (String(a[0]).includes('/snapshot')) window.__snap++; return o(...a); }; });

  // A repeatable background mutation (new project) -> /events -> the page's refresh().
  await page.evaluate(async () => {
    await fetch('/api/v1/projects', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({bot_id: 'bot-main', title: '刷新触发 ' + Date.now()})});
  });
  await expect.poll(() => page.evaluate(() => window.__snap), {timeout: 10000}).toBeGreaterThan(0); // refresh ran

  await expect(log.locator('[data-probe="keep"]')).toHaveCount(1); // message DOM NOT rebuilt
  const after = await log.evaluate(e => e.scrollTop);
  expect(Math.abs(after - before)).toBeLessThanOrEqual(5); // scroll position held
});
