// P1-B Work-level Stop truth. The Current-Work inspector derives "stopping" from
// the WORK's own durable cancel_requested — never from a task — and offers Stop for ANY
// non-terminal Work regardless of task activity.
import {test, expect} from '@playwright/test';
import {login, selectBot, gotoWork, snapshot} from './_studio.mjs';

async function makeCurrent(page, goalText) {
  await gotoWork(page);
  await page.locator('#work-grid .panel', {hasText: goalText}).first().getByRole('button', {name: '继续此 Work'}).click();
  await expect(page.getByTestId('work-inspector')).toContainText(goalText);
}

test('Work.cancel_requested shows 正在取消 with no actionable Stop (even without a cancel_requested task)', async ({page}) => {
  await login(page);
  await selectBot(page, '停止测试');
  await makeCurrent(page, '工作级正在取消');
  const inspector = page.getByTestId('work-inspector');
  await expect(inspector).toContainText('正在取消'); // Work-level durable truth
  await expect(inspector.getByRole('button', {name: '停止此工作'})).toHaveCount(0); // no actionable Stop
  await expect(inspector).not.toContainText('已取消'); // never optimistic
});

test(' required_output_missing renders through generic blocked/recovery truth and still offers Stop', async ({page}) => {
  await login(page);
  await selectBot(page, '停止测试');
  await makeCurrent(page, '受阻但非终态');

  // Server truth is the real shape; React must consume the generic blocked
  // projection rather than inventing a bespoke required-output UI/state machine.
  const s = await snapshot(page);
  const w = s.work.find((x) => (x.goal || '').includes('受阻但非终态'));
  expect(w?.status).toBe('blocked');
  expect(w?.error).toBe('required_output_missing');
  expect(w?.pending_required_outputs).toEqual(['final.docx']);
  const t = s.tasks.find((x) => x.work_id === w?.id);
  expect(t?.status).toBe('failed');
  expect(t?.error).toBe('required_output_missing');

  const inspector = page.getByTestId('work-inspector');
  await expect(inspector).toContainText('受阻'); // server attention projection
  await expect(inspector.getByRole('button', {name: '停止此工作'})).toBeVisible(); // non-terminal Work stays controllable
});


test('terminal cancelled Work wins over a lingering cancel_requested flag', async ({page}) => {
  await login(page);
  await selectBot(page, '停止测试');
  await makeCurrent(page, '已取消终态');
  const inspector = page.getByTestId('work-inspector');
  await expect(inspector).not.toContainText('正在取消'); // terminal reality wins
  await expect(inspector.getByRole('button', {name: '停止此工作'})).toHaveCount(0); // terminal: no Stop
});
