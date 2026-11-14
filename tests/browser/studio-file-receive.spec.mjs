// 我的电脑: a paired computer shows 允许接收文件. Missing counts as off.
// The checkbox posts that computer's allow flag; the local coordinator has no switch.
import {test, expect} from '@playwright/test';
import {login} from './_studio.mjs';
import {project} from './_parity.mjs';

test('paired computer can turn on file receive from my computers', async ({page}) => {
  let reported = undefined;
  await project(page, (s) => {
    const limits = (s.hosts && s.hosts[0] && s.hosts[0].limits) || {};
    s.hosts = [...(s.hosts || []), {
      id: 'win', name: 'Work-Windows', transport: 'connector', os: 'Windows', limits,
    }];
    const device = {id: 'win', online: true};
    if (reported !== undefined) device.allow_file_receive = reported;
    s.devices = [...(s.devices || []), device];
  });
  await page.route('**/api/v1/hosts/win/file-receive', async (route) => {
    expect(route.request().postDataJSON()).toEqual({allow: true});
    reported = true;
    await route.fulfill({json: {host_id: 'win', allow_file_receive: true, restart_required: false}});
  });
  await login(page);
  await page.locator('[data-view="hosts"]').click();
  await expect(page.locator('#hosts-view')).toBeVisible();
  await expect(page.locator('#host-file-receive-local')).toHaveCount(0);
  const box = page.locator('#host-file-receive-win');
  await expect(box).toBeVisible();
  await expect(box).not.toBeChecked();
  await expect(page.locator('label:has(#host-file-receive-win)')).toContainText('允许接收文件');
  // The checkbox stays on the last reported value until the connector answers,
  // so the click is not synchronous. Wait for the saved notice, then the check.
  await box.click();
  await expect(page.locator('#notice')).toContainText('已更新');
  await expect(box).toBeChecked();
});
