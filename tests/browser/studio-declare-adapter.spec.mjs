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

test('an approve 409 stays visible on the setup card', async ({page}) => {
  await project(page, (s) => {
    s.setup.model.configured = true;
    s.setup.requests = [{
      id: 'setup-missing',
      status: 'needs_approval',
      host_id: 'host-local',
      proposal: {
        action: 'executor_config',
        adapter: 'dsk-claude',
        installation_id: null,
        command: ['~/.local/bin/dsk-claude', '--stdio'],
        draft: {cwd: '/work'},
      },
    }];
  });
  await page.route('**/api/v1/setup/setup-missing/approve', (route) => route.fulfill({
    status: 409,
    contentType: 'application/json',
    body: JSON.stringify({
      code: 'cli_not_installed',
      message: 'cli_not_installed: executable ~/.local/bin/dsk-claude is not installed on PATH',
      advice: 'executable ~/.local/bin/dsk-claude is not installed on PATH',
      retryable: false,
    }),
  }));
  await login(page);
  const card = page.locator('#setup-panel .mini', {hasText: 'dsk-claude'});
  await card.getByRole('button', {name: '应用这些设置'}).click();
  const fault = card.getByRole('alert');
  await expect(fault).toContainText('cli_not_installed');
  await expect(fault).toContainText('not installed on PATH');
  await expect(card.getByRole('button', {name: '应用这些设置'})).toBeVisible();
});
