// Contract #8 — an unknown execution surfaces as a NAMED action card (visible
// "需要处理" + an explicit 停止此工作 action), not an anonymous spinner, and the global
// activity pill reflects workspace quarantine (app.js:195).
// Anchored on visible text + role + server truth, not CSS classes.
import {test, expect} from '@playwright/test';
import {login, selectBot, snapshot, BOT} from './_studio.mjs';

test('an unknown task renders a named recovery action card, not anonymous dots', async ({page}) => {
  await login(page);
  await selectBot(page, BOT.recovery);
  const log = page.getByTestId('chat-log');
  await expect(log).toContainText('需要处理', {timeout: 15000}); // named, not anonymous
  await expect(log.getByRole('button', {name: '停止此工作'})).toBeVisible(); // explicit action offered

  const s = await snapshot(page);
  expect(s.tasks.some(t => t.status === 'unknown')).toBeTruthy(); // server truth: really unknown
  await expect(page.locator('#activity')).toHaveText('执行通道已隔离'); // quarantine reflected
});
