// Contract #3 — explicit Work continuation: the one-shot target AND the pending
// attachments both belong to the Bot they were chosen for and are cleared on a Bot
// switch (/ commit 4673997); the next send carries continue_work_id equal to the
// targeted Work.
import {test, expect} from '@playwright/test';
import {login, selectBot, gotoWork, snapshot, BOT} from './_studio.mjs';

const GOAL = '帮我整理上周的会议纪要';

// The work-grid is ONE global newest-first grid (all bots) paginated at 10/page —
// identical to the frozen 1.0 shell. The target lives wherever recency places it, so
// walk back to page 1 then forward until the card appears (never a page-1 assumption).
async function findWorkCard(page, goal) {
  const prev = page.locator('#work-grid .pager button', {hasText: '上一页'});
  while ((await prev.count()) > 0 && !(await prev.isDisabled())) { await prev.click(); }
  for (let i = 0; i < 10; i++) {
    const card = page.locator('#work-grid .panel', {hasText: goal}).first();
    if ((await card.count()) > 0) return card;
    const next = page.locator('#work-grid .pager button', {hasText: '下一页'});
    if ((await next.count()) === 0 || (await next.isDisabled())) break;
    await next.click();
  }
  return page.locator('#work-grid .panel', {hasText: goal}).first();
}

async function startContinue(page) {
  await gotoWork(page);
  const card = await findWorkCard(page, GOAL);
  await card.getByRole('button', {name: '继续此 Work'}).click();
  await expect(page.getByTestId('continue-target')).toBeVisible();
  await expect(page.getByTestId('continue-target')).toContainText(GOAL.slice(0, 10));
}

test('continuation target & attachments clear on Bot switch; send targets the exact Work', async ({page}) => {
  await login(page);
  await selectBot(page, BOT.main);
  const target = (await snapshot(page)).work.find(w => w.goal === GOAL);
  expect(target).toBeTruthy();

  // An attachment and a continuation target both belong to this Bot.
  await page.locator('#file-pick').setInputFiles({name: '切换前附件.txt', mimeType: 'text/plain', buffer: Buffer.from('x')});
  await expect(page.locator('#attached > *')).toHaveCount(1, {timeout: 20000});
  await startContinue(page);

  // Switching Bot clears BOTH (attachments do not leak across Bots — a prior bug).
  await page.locator('#bot-list .bot-item', {hasText: BOT.aux}).click();
  await expect(page.getByTestId('continue-target')).toBeHidden();
  await expect(page.locator('#attached > *')).toHaveCount(0);

  // Re-establish on bot-main and send: continue_work_id equals the targeted Work, and
  // the one-shot target is cleared afterward.
  await selectBot(page, BOT.main);
  await startContinue(page);
  // The move control is part of the one-shot target. Leaving it unchecked must
  // not send a correction.
  await expect(page.getByTestId('move-previous-message')).toBeVisible();
  const [req] = await Promise.all([
    page.waitForRequest(r => r.url().includes('/messages') && r.method() === 'POST'),
    (async () => {
      await page.getByRole('textbox').fill('接着上面的继续补充一点');
      await page.getByRole('button', {name: '发送 →'}).click();
    })(),
  ]);
  expect(req.postDataJSON().continue_work_id).toBe(target.id);
  expect(req.postDataJSON().correct_to_work_id).toBeFalsy();
  await expect(page.getByTestId('continue-target')).toBeHidden();
});

test('checking move-previous sends correct_to_work_id and does not hide a rejected move', async ({page}) => {
  await login(page);
  await selectBot(page, BOT.main);
  await startContinue(page);
  await page.getByTestId('move-previous-message').check();
  const [req] = await Promise.all([
    page.waitForRequest(r => r.url().includes('/messages') && r.method() === 'POST'),
    (async () => {
      await page.getByRole('textbox').fill('把上一条也归到这个工作');
      await page.getByRole('button', {name: '发送 →'}).click();
    })(),
  ]);
  const body = req.postDataJSON();
  // The checkbox names the same Work the continue chip is already showing.
  expect(body.continue_work_id).toBeTruthy();
  expect(body.correct_to_work_id).toBe(body.continue_work_id);
  // This tab has no adjacent previous message, so the server rejects the move
  // and the one-shot target stays. A rejected correction must not look successful.
  await expect(page.getByTestId('continue-target')).toBeVisible();
  await expect(page.getByRole('button', {name: '发送 →'})).toBeEnabled();
});
