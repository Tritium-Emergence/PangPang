// Contract #1/#2 — SSE streaming preview GROWS through multiple partial renders and
// hands off to exactly one final message, with no flash-loss and no permanent
// duplicate. Anchored on stable testids (stream-preview /
// assistant-msg) + visible result, not CSS classes.
import {test, expect} from '@playwright/test';
import {login, selectBot, waitQuiet, BOT} from './_studio.mjs';

test('streaming preview grows then hands off to one final message (no flash-loss, no dup)', async ({page}) => {
  await login(page);
  await selectBot(page, BOT.main);
  await waitQuiet(page, 'bot-main'); // drain seed turns before measuring

  // Observe BEFORE sending: distinct partial preview texts, and whether the count of
  // persisted (final) assistant messages ever DECREASED.
  await page.evaluate(() => {
    const log = document.querySelector('[data-testid="chat-log"]');
    const finals = () => log.querySelectorAll('[data-testid="assistant-msg"]').length;
    const w = window.__stream = {previews: new Set(), decreased: false, last: finals()};
    new MutationObserver(() => {
      const body = log.querySelector('[data-testid="stream-preview"] .body');
      if (body && body.textContent) w.previews.add(body.textContent);
      const c = finals();
      if (c < w.last) w.decreased = true;
      w.last = c;
    }).observe(log, {childList: true, subtree: true, characterData: true});
  });
  const finals = page.locator('[data-testid="chat-log"] [data-testid="assistant-msg"]');
  const before = await finals.count();

  await page.getByRole('textbox').fill('给我讲讲这个项目的最新进展');
  await page.getByRole('button', {name: '发送 →'}).click();

  await expect(finals).toHaveCount(before + 1, {timeout: 30000}); // one new final, no dup
  await expect(finals.last()).toContainText('帮你处理这个问题');
  await expect(page.locator('[data-testid="stream-preview"]')).toHaveCount(0); // transient preview gone

  const w = await page.evaluate(() => ({n: window.__stream.previews.size, decreased: window.__stream.decreased}));
  expect(w.n).toBeGreaterThanOrEqual(2); // the preview actually GREW through ≥2 distinct partial renders
  expect(w.decreased).toBeFalsy(); // persisted-message count never dropped — no flash-loss
});
