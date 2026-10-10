// Contract #7 (RCB hard gate) — the committed-reality boundary for task approval.
// The client may render request progress but MUST NOT render committed reality before
// the server's durable response. The chat shows an approval card only while the task
// awaits the user, so committed reality is the card leaving the chat. Proven by HOLDING
// the intercepted approve POST on a deferred gate while a MutationObserver watches for
// the card disappearing early, plus a rejected approve that keeps it. Anchored on the
// exact seeded task's timeline node + role + server truth (snapshot) — not CSS classes.
import {test, expect} from '@playwright/test';
import {login, selectBot, snapshot, BOT} from './_studio.mjs';

const APPROVE = '**/api/v1/tasks/*/approve';

test('approval never renders committed reality before durable server truth (RCB)', async ({page}) => {
  await login(page);
  const taskId = (await snapshot(page)).tasks.find(t => t.status === 'awaiting_approval').id; // exact entity
  expect(taskId).toBeTruthy();

  await selectBot(page, BOT.approval);
  const log = page.getByTestId('chat-log');
  const card = log.locator(`[data-timeline="approval-${taskId}"]`);
  const approve = card.getByRole('button', {name: '批准执行'});
  await expect(approve).toBeVisible({timeout: 15000});
  await expect(card).toContainText('需你确认');

  // --- Part A: a REJECTED approve reverts; the card stays awaiting ------------------
  await page.route(APPROVE, route => route.fulfill({status: 500, contentType: 'application/json',
    body: JSON.stringify({code: 'approval_rejected'})}));
  await approve.click();
  await expect(page.locator('#notice')).toBeVisible({timeout: 10000});
  await expect(card).toContainText('需你确认');
  await expect(approve).toBeEnabled();
  await expect.poll(async () => (await snapshot(page)).tasks.find(t => t.id === taskId).status, {timeout: 10000})
.toBe('awaiting_approval'); // server still awaiting
  await page.unroute(APPROVE);

  // --- Part B: HOLD the approve POST; the card must not leave until it is released --
  let release;
  const held = new Promise(r => { release = r; });
  await page.route(APPROVE, async route => { await held; await route.continue(); });
  await page.evaluate((id) => {
    const node = document.querySelector('[data-testid="chat-log"]');
    window.__committed = false;
    new MutationObserver(() => { if (!node.querySelector(`[data-timeline="approval-${id}"]`)) window.__committed = true; })
.observe(node, {childList: true, subtree: true, characterData: true});
  }, taskId);
  await approve.click();
  await expect(approve).toBeDisabled(); // request-progress is allowed
  await page.waitForTimeout(1200); // while the POST is held...
  expect(await page.evaluate(() => window.__committed)).toBe(false); //...committed reality NEVER rendered
  release(); // server durably approves now
  await expect.poll(async () => !!(await snapshot(page)).tasks.find(t => t.id === taskId).approved_at, {timeout: 15000})
.toBeTruthy();
  await expect(card).toHaveCount(0, {timeout: 10000}); // leaves only after server truth
  await page.unroute(APPROVE);
});
