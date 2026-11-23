import {test, expect} from '@playwright/test';
import {project} from './_parity.mjs';
import {login, gotoWork} from './_studio.mjs';

test('an expired setup card can be denied and does not stay above chat', async ({page}) => {
  let denied = false;
  await project(page, (s) => {
    s.setup.model.configured = true;
    if (denied) {
      s.setup.requests = [];
      return;
    }
    s.setup.requests = [{
      id: 'setup-expired',
      status: 'needs_approval',
      host_id: 'host-local',
      expires: 1,
      proposal: {
        action: 'declare_adapter',
        adapter: 'gemini-opencode',
        command: ['gemini-opencode.cmd', 'run'],
        adapter_row: {
          binary: 'gemini-opencode.cmd', argv: ['run'], concurrency: 1, priority: 30,
          use_when: 'Work-Windows only',
        },
      },
    }];
  });
  await page.route('**/api/v1/setup/setup-expired/approve', (route) => route.fulfill({
    status: 409,
    contentType: 'application/json',
    body: JSON.stringify({code: 'setup_request_expired_or_used', message: 'setup_request_expired_or_used', retryable: false}),
  }));
  await page.route('**/api/v1/setup/setup-expired/deny', (route) => {
    denied = true;
    return route.fulfill({json: {denied: true}});
  });
  await login(page);
  const card = page.locator('#setup-panel .mini', {hasText: 'gemini-opencode'});
  await expect(card).toBeVisible();
  await expect(card).toContainText('gemini-opencode.cmd');
  await expect(card).toContainText('Work-Windows only');
  await expect(card).not.toContainText('"binary"');
  await expect(card).not.toContainText('setup_request_expired_or_used');
  await card.getByRole('button', {name: '应用这些设置'}).click();
  const fault = card.getByRole('alert');
  await expect(fault).toContainText('这份确认已经过期');
  await expect(fault).not.toContainText('setup_request_expired_or_used');
  await expect(card).toBeVisible();
  await gotoWork(page);
  await expect(card).toBeVisible();
  await card.getByRole('button', {name: '拒绝'}).click();
  await expect(page.locator('#setup-panel .mini', {hasText: 'gemini-opencode'})).toHaveCount(0);
  await page.locator('[data-view="chat"]').click();
  await expect(page.locator('#setup-panel .mini', {hasText: 'gemini-opencode'})).toHaveCount(0);
});
