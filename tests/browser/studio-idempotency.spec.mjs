// browser half: a transient / network-ambiguous send failure must NOT clear an
// explicit continueTarget — the user's explicit intent survives for a retry; only
// deterministic continue_work_* invalid errors clear it. The KEY-REUSE half is
// proven deterministically in tests/test_web_idempotency_ui.mjs (no browser race).
import {test, expect} from '@playwright/test';
import {login, selectBot, gotoWork} from './_studio.mjs';

test('a transient send failure does NOT clear an explicit continueTarget', async ({page}) => {
  await login(page);
  await selectBot(page, '隔离测试');

  // Establish an explicit continuation target on Work A.
  await gotoWork(page);
  await page.locator('#work-grid .panel', {hasText: '当前工作 A'}).first().getByRole('button', {name: '继续此 Work'}).click();
  await expect(page.getByTestId('continue-target')).toBeVisible();

  // A transient (non-continue_work_*) failure must keep the target.
  await page.route('**/api/v1/bots/*/messages', (route) =>
    route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({code: 'model_request_failed'})}));
  const box = page.getByRole('textbox');
  await box.fill('带继续目标的一条消息');
  await expect(box).toHaveValue('带继续目标的一条消息');
  await page.getByRole('button', {name: '发送 →'}).click();
  await expect(page.locator('#notice')).toBeVisible({timeout: 10000}); // transient failure surfaced
  await expect(page.getByTestId('continue-target')).toBeVisible(); // continueTarget NOT cleared
  await page.unroute('**/api/v1/bots/*/messages');
});
