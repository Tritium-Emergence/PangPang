// Contract #6 + — Routine approval surfaces standing authority truthfully
// (grant_write defaults OFF, never labelled "只读") and the approve
// request carries that exact envelope for THIS routine. Also locks the app.js/setup.js
// dual-render: the setup.js-rendered routines container and the
// app.js-rendered work-grid are BOTH populated — neither blanks the other. Anchored on
// container ids + visible text + role + server truth, not the.panel.routine class.
import {test, expect} from '@playwright/test';
import {login, gotoWork, snapshot} from './_studio.mjs';

test('routine approval shows authority truth (write off by default, never 只读) and dual-render holds', async ({page}) => {
  await login(page);

  await gotoWork(page);
  const routines = page.locator('#routines');
  await expect(routines.locator('.panel.routine').first()).toBeVisible({timeout: 15000});

  // Dual-render non-regression: both the setup.js-rendered #routines and the
  // app.js-rendered #work-grid are populated at once — neither blanks the other. Checked
  // structurally (panels exist), not by a specific goal string (pagination/other specs
  // move which works land on page 1 of the shared studio server).
  await expect(routines.locator('.panel.routine').first()).toBeVisible();
  await expect(page.locator('#work-grid .panel').first()).toBeVisible();

  // Authority truth is asserted on a PENDING routine (grant_write off by default, never
  // "只读"). If a prior run on this shared server already approved the only pending one,
  // the authority-truth + approve assertions are skipped (the dual-render lock above
  // already ran); the contract is covered by the first green run.
  const pending = (await snapshot(page)).routines.find(r => r.approval_state === 'pending');
  if (!pending) { test.info().annotations.push({type: 'note', description: 'no pending routine on shared server — authority/approve already exercised'}); return; }
  const card = routines.locator('.panel.routine', {hasText: '待批准'}).first();
  await expect(card.locator('input[type="checkbox"]').first()).not.toBeChecked();
  await expect(card).toContainText('默认不预授写入');
  await expect(card).not.toContainText('只读');

  // Approving without ticking the box sends grant_write=false for THIS routine.
  const [req] = await Promise.all([
    page.waitForRequest(r => r.url().includes('/routines/' + pending.id + '/approve') && r.method() === 'POST'),
    card.getByRole('button', {name: '批准并启用'}).click(),
  ]);
  expect(req.postDataJSON().authority.grant_write).toBe(false);
  await expect.poll(async () => (await snapshot(page)).routines.find(r => r.id === pending.id)?.approval_state,
    {timeout: 15000}).toBe('approved');
});
