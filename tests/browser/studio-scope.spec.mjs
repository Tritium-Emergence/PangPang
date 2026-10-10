// action-scope regression: a sibling Work B's unknown task must NOT appear in the
// Current-Work inspector's action targets when Work A is the current Work. The inspector
// acts only on the recursive current-Work subtree (scope), never on a sibling. The chat
// may still surface B as a bot-wide sibling recovery card — that is a notice, not an
// in-scope action target. (Restores the P1 action-scope fix in React.)
import {test, expect} from '@playwright/test';
import {login, selectBot, snapshot, gotoWork} from './_studio.mjs';

test('sibling Work B unknown is not in Work A inspector action scope ', async ({page}) => {
  await login(page);
  await selectBot(page, '隔离测试');

  const s = await snapshot(page);
  const workA = s.work.find((w) => (w.goal || '').includes('当前工作 A'));
  const workB = s.work.find((w) => (w.goal || '').includes('兄弟工作 B'));
  expect(workA && workB).toBeTruthy();
  const unknownB = s.tasks.find((t) => t.status === 'unknown' && t.work_id === workB.id);
  expect(unknownB).toBeTruthy();

  // Make Work A the current Work (explicit continue from the Work pane).
  await gotoWork(page);
  await page.locator('#work-grid .panel', {hasText: '当前工作 A'}).first().getByRole('button', {name: '继续此 Work'}).click();

  // The inspector now scopes to Work A: it must NOT expose Work B's unknown task as an
  // action target (no data-task for unknownB inside the inspector).
  const inspector = page.getByTestId('work-inspector');
  await expect(inspector).toContainText('当前工作 A');
  await expect(inspector.locator(`[data-task="${unknownB.id}"]`)).toHaveCount(0);
  // And the inspector must not offer resolve/reconcile for the sibling's unknown.
  await expect(inspector).not.toContainText('兄弟工作 B');
});
