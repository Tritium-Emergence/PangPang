import {test, expect} from '@playwright/test';
import {login, gotoWork} from './_studio.mjs';

test('task cards show live activity by status', async ({page}) => {
  await login(page);
  await gotoWork(page);
  const starting = page.locator('[data-activity-status="starting"]');
  await expect(starting).toBeVisible();
  await expect(starting).toContainText('启动于');
  await expect(starting).toContainText(/\d{2}:\d{2}:\d{2}/);
  await expect(starting).not.toContainText('已运行');
  await expect(starting).not.toContainText('排队于');
  await expect(starting).toContainText('暂无可观测活动');
  await expect(starting).toContainText('未声明必需文件');

  const reading = page.locator('[data-activity-status="running"]', {hasText: 'src/a.py'});
  await expect(reading).toBeVisible();
  await expect(reading).toContainText('开始于');
  await expect(reading).not.toContainText('已运行');
  await expect(reading).not.toContainText('启动于');
  await expect(reading).toContainText(/\d{2}:\d{2}:\d{2}/);
  await expect(reading).toContainText('未声明必需文件');
  await expect(reading).not.toContainText('尚未产出');

  const capped = page.locator('[data-activity-status="running"]', {hasText: '日志可能已达上限'});
  await expect(capped).toBeVisible();
  await expect(page.locator('#tasks')).not.toContainText('尚未产出');
});
