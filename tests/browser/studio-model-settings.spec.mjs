import {test, expect} from '@playwright/test';
import {login} from './_studio.mjs';

test('Pi catalog selection, explicit third-party thinking, and mobile settings', async ({page}) => {
  await login(page);
  await page.locator('[data-view="hosts"]').click();
  await page.locator('#settings').click();
  await expect(page.locator('#model-provider option[value="anthropic"]')).toHaveCount(1);
  await page.locator('#model-provider').selectOption('anthropic');
  const id = await page.locator('#pi-models option').first().getAttribute('value');
  await page.locator('#model-name').fill(id);
  await expect(page.locator('#model-endpoint')).toHaveValue('https://api.anthropic.com');
  await expect(page.locator('#model-api')).toHaveCount(0);
  await page.locator('#model-provider').selectOption('custom');
  await page.locator('#model-name').fill('qwen-local');
  await page.locator('#model-endpoint').fill('http://127.0.0.1:9999/v1');
  await page.locator('#model-reasoning').selectOption('low');
  await page.locator('#settings-dialog summary').click();
  await page.locator('#model-thinking-format').selectOption('qwen-chat-template');
  await page.locator('#model-no-auth').check();
  await page.setViewportSize({width: 390, height: 844});
  const bounds = await page.locator('#settings-dialog').boundingBox();
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  expect(await page.locator('#settings-dialog').evaluate(e => e.scrollWidth <= e.clientWidth)).toBeTruthy();
  let submitted;
  await page.route('**/api/v1/setup/model', async route => {
    submitted = route.request().postDataJSON();
    await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({code: 'fixture_validation_failed', message: 'fixture_validation_failed', retryable: false})});
  });
  await page.locator('#model-save').click();
  await expect(page.locator('#model-error')).toContainText('fixture_validation_failed');
  expect(submitted).toMatchObject({provider: 'custom', api: 'openai-completions', reasoning_level: 'low', thinking_format: 'qwen-chat-template', no_auth: true});
  // Failed validation must keep the operator's choices available for correction.
  await expect(page.locator('#model-thinking-format')).toHaveValue('qwen-chat-template');
});

test('an upstream 401 surfaces as the real 409 model_auth_failed with a localized actionable hint, not a raw code', async ({page}) => {
  // First-run's most common mistake: a subscription login token pasted as an API key.
  // The save must surface the localized remediation sentence AND the raw code.
  await page.addInitScript(() => localStorage.setItem('pangpang.uiLocale.v1', 'zh-CN'));
  await login(page);
  await page.locator('[data-view="hosts"]').click();
  await page.locator('#settings').click();
  await page.locator('#model-provider').selectOption('custom');
  await page.locator('#model-name').fill('qwen-local');
  await page.locator('#model-endpoint').fill('https://gateway.invalid/v1');
  // Deliberately not sk-prefixed: secret scanners flag sk-* shapes.
  await page.locator('#model-secret').fill('subscription-token-not-an-api-key');
  await page.route('**/api/v1/setup/model', async route => {
    // Real chain: upstream 401 -> Fault('model_auth_failed') -> store.py Fault default 409,
    // http.py responds retryable:false; the UI translates by code only.
    await route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({code: 'model_auth_failed', message: 'model_auth_failed', retryable: false})});
  });
  await page.locator('#model-save').click();
  const err = page.locator('#model-error');
  await expect(err).toContainText('鉴权失败');
  await expect(err).toContainText('model_auth_failed');
});
