// An admitted executor is not "ready" until execution_verified. Discovery
// (version probe) renders the other label in both locales, and stays off the green dot.
import {test, expect} from '@playwright/test';
import {login} from './_studio.mjs';
import {project} from './_parity.mjs';

const evidence = (verified) => ({
  capability: 'executor',
  state: 'ready',
  reason: null,
  operator_action_code: null,
  operator_action: null,
  detail: {
    selected: {installation_id: 'codex-local'},
    executor_evidence: [{installation_id: 'codex-local', discovered: true, execution_verified: verified}],
  },
});

let verified = false;
let errors;

test.beforeEach(async ({page}) => {
  verified = false;
  errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
});
test.afterEach(async ({page}) => {
  await page.unrouteAll({behavior: 'wait'});
  expect(errors).toEqual([]);
});

async function seed(page) {
  await project(page, (s) => {
    s.setup.capabilities = [
      {capability: 'model', state: 'ready', reason: null, operator_action_code: null, operator_action: null},
      evidence(verified),
    ];
    s.hosts = [{id: 'local', name: '本机', transport: 'local'}];
    s.installations = [{id: 'codex-local', host_id: 'local', adapter: 'codex'}];
    s.probes = [{id: 'codex-local', available: true, version: 'CLI 9'}];
    s.devices = [];
  });
}

test('discovered executor is not labeled ready until execution_verified', async ({page}) => {
  await seed(page);
  await login(page);
  await page.locator('[data-view="hosts"]').click();
  const list = page.locator('#capability-list');
  const executor = list.locator('[data-capability="executor"]');
  const model = list.locator('[data-capability="model"]');
  await expect(executor).toContainText('执行器');
  await expect(executor).toContainText('已发现，未验证');
  await expect(executor).not.toContainText('就绪');
  await expect(executor).toHaveAttribute('data-state', 'ready');
  await expect(executor).toHaveAttribute('data-execution-verified', 'false');
  await expect(model).toContainText('就绪');
  const unverifiedDot = await executor.locator('.cap-dot').evaluate((el) => getComputedStyle(el).backgroundColor);
  const readyDot = await model.locator('.cap-dot').evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(unverifiedDot).not.toBe(readyDot);

  await page.locator('[data-view="hosts"]').click();
  const card = page.locator('#hosts .panel').filter({hasText: 'codex-local'});
  await expect(card).toContainText('已发现，未验证');
  await expect(card).toContainText('CLI 9');
  await expect(card).not.toContainText('就绪');
  await expect(card.locator('[data-execution-verified="false"]')).toBeVisible();

  await page.locator('#lang-toggle').click();
  await expect(executor).toContainText('discovered, not verified');
  await expect(executor).not.toContainText('Ready');
  await expect(model).toContainText('Ready');
  await expect(card).toContainText('discovered, not verified');
  await expect(card).toContainText('CLI 9');

  verified = true;
  await page.locator('#refresh').click();
  await expect(executor).toContainText('Ready');
  await expect(executor).not.toContainText('discovered, not verified');
  await expect(executor).toHaveAttribute('data-execution-verified', 'true');
  const verifiedDot = await executor.locator('.cap-dot').evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(verifiedDot).toBe(readyDot);
  await expect(card).toContainText('Ready');
  await expect(card).toContainText('CLI 9');
  await expect(card).not.toContainText('discovered, not verified');
  await expect(card.locator('[data-execution-verified="true"]')).toBeVisible();
});
