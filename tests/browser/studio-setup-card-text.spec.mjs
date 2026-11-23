import {test, expect} from '@playwright/test';
import {project} from './_parity.mjs';
import {login} from './_studio.mjs';

test('confirmation errors are sentences, and the proposal shows what will land', async ({page}) => {
  await project(page, (s) => {
    s.setup.model.configured = true;
    s.setup.requests = [
      {
        id: 'setup-changed',
        status: 'needs_approval',
        host_id: 'local',
        proposal: {
          action: 'executor_config',
          adapter: 'codex',
          installation_id: 'codex-local',
          draft: {
            cwd: '/dir-changed',
            env: {CODEX_HOME: '/profiles/work'},
            secret_refs: {CODEX_API_KEY: 'cred-work'},
            extra_argv: ['--strict-config'],
          },
        },
      },
      {
        id: 'setup-clear',
        status: 'needs_approval',
        host_id: 'local',
        proposal: {
          action: 'executor_config',
          adapter: 'codex',
          installation_id: 'codex-other',
          draft: {cwd: '/dir-clear', env: {}, secret_refs: {}, extra_argv: []},
        },
      },
      {
        id: 'setup-plain',
        status: 'needs_approval',
        host_id: 'local',
        proposal: {
          action: 'executor_config',
          adapter: 'codex',
          installation_id: 'codex-plain',
          draft: {cwd: '/dir-plain'},
        },
      },
    ];
  });
  await page.route('**/api/v1/setup/setup-changed/approve', (route) => route.fulfill({
    status: 409,
    contentType: 'application/json',
    body: JSON.stringify({
      code: 'installation_config_changed',
      message: 'installation_config_changed',
      advice: '{"stale":true}',
      retryable: false,
    }),
  }));
  await page.route('**/api/v1/setup/setup-plain/approve', (route) => route.fulfill({
    status: 409,
    contentType: 'application/json',
    body: JSON.stringify({
      code: 'fixture_mystery_fault',
      message: 'fixture_mystery_fault: {"raw":true}',
      advice: '{"raw":true}',
      retryable: false,
    }),
  }));
  await login(page);
  const changed = page.locator('#setup-panel .mini', {hasText: '/dir-changed'});
  await expect(changed).toContainText('CODEX_HOME=/profiles/work');
  await expect(changed).toContainText('cred-work');
  await expect(changed).toContainText('--strict-config');
  await expect(changed).not.toContainText('"env"');
  await expect(changed).not.toContainText('{');
  await changed.getByRole('button', {name: '应用这些设置'}).click();
  const changedFault = changed.getByRole('alert');
  await expect(changedFault).toContainText('配置已被其他请求更新');
  await expect(changedFault).not.toContainText('installation_config_changed');
  await expect(changedFault).not.toContainText('stale');

  const cleared = page.locator('#setup-panel .mini', {hasText: '/dir-clear'});
  await expect(cleared).toContainText('将移除已有的环境变量');
  await expect(cleared).toContainText('将移除已有的凭据引用');
  await expect(cleared).toContainText('将移除已有的附加参数');
  const plain = page.locator('#setup-panel .mini', {hasText: '/dir-plain'});
  await expect(plain).not.toContainText('将移除已有的环境变量');
  await expect(plain).not.toContainText('将移除已有的凭据引用');
  await expect(plain).not.toContainText('将移除已有的附加参数');
  await plain.getByRole('button', {name: '应用这些设置'}).click();
  const plainFault = plain.getByRole('alert');
  await expect(plainFault).toContainText('这次没有完成');
  await expect(plainFault).not.toContainText('fixture_mystery_fault');
  await expect(plainFault).not.toContainText('raw');
});

test('the secret window shows a sentence instead of the raw failure', async ({page}) => {
  await project(page, (s) => {
    s.setup.model.configured = true;
    s.setup.requests = [{
      id: 'secret-card',
      status: 'needs_secret',
      host_id: 'local',
      proposal: {action: 'credential', installation_id: 'codex-local'},
    }];
  });
  await page.route('**/api/v1/setup/secret-card/secret-target', (route) => route.fulfill({
    json: {
      public_key: 'not-a-key', context: 'secret-card', host_id: 'local',
      installation_id: 'codex-local', endpoint: '', storage: '设备',
    },
  }));
  await login(page);
  await page.getByRole('button', {name: '打开密钥窗口'}).click();
  await page.locator('#device-secret').fill('super-secret-value');
  await page.locator('#secret-save').click();
  const fault = page.locator('#secret-error');
  await expect(fault).toContainText('这次没有完成');
  await expect(fault).not.toContainText('not-a-key');
  await expect(fault).not.toContainText('super-secret-value');
  await expect(fault).not.toContainText('{');
});
