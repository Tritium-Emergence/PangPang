import {test, expect} from '@playwright/test';
import {project} from './_parity.mjs';
import {login} from './_studio.mjs';

test('custom adapter approval shows the full row and posts an empty body', async ({page}) => {
  await project(page, (s) => {
    s.setup.model.configured = true;
    s.setup.requests = [{
      id: 'setup-declare',
      status: 'needs_approval',
      host_id: 'host-declare',
      proposal: {
        action: 'declare_adapter',
        adapter: 'my-echo',
        command: ['my-echo', '--stdio'],
        adapter_row: {
          binary: 'my-echo', argv: ['--stdio'], concurrency: 1, priority: 20, use_when: 'echo a line',
        },
      },
    }];
    s.hosts.push({id: 'host-declare', name: 'Declare host', transport: 'connector'});
  });
  let body;
  await page.route('**/api/v1/setup/setup-declare/approve', (route) => {
    body = route.request().postDataJSON();
    return route.fulfill({json: {accepted: true}});
  });
  await login(page);
  const card = page.locator('#setup-panel .mini', {hasText: 'my-echo'});
  await expect(card).toContainText('my-echo');
  await expect(card).toContainText('--stdio');
  await expect(card).toContainText('echo a line');
  await expect(card).toContainText('concurrency');
  await expect(card.locator('input[type=url]')).toHaveCount(0);
  await card.getByRole('button', {name: '应用这些设置'}).click();
  expect(body).toEqual({});
});
