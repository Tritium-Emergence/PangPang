// Contract #7 (render) — a cancel in flight renders as request-progress "正在取消",
// never as committed "已取消", until the server reaches the durable terminal state
// (RCB). Asserted on the task badge (server-truth status), which is Bot-independent.
import {test, expect} from '@playwright/test';
import {login, gotoWork, snapshot} from './_studio.mjs';

test('a cancel_requested task shows 正在取消, never 已取消 (RCB render)', async ({page}) => {
  await login(page);
  await gotoWork(page);
  const tasks = page.locator('#tasks');
  await expect(tasks.getByText('正在取消', {exact: false}).first()).toBeVisible({timeout: 15000});
  await expect(tasks).not.toContainText('已取消');

  const s = await snapshot(page);
  expect(s.tasks.some(t => t.status === 'cancel_requested')).toBeTruthy(); // request in progress
  expect(s.tasks.some(t => t.status === 'cancelled')).toBeFalsy(); // nothing committed-cancelled
});
