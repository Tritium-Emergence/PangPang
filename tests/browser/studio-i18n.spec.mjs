// Bilingual i18n: UI locale selects labels only (never state/permission logic).
// Verifies an en-US first paint and a zh-CN first paint off the stored preference,
// and that the known checkpoint phase is localized (the delivered-in-English
// fix). The locale is read from localStorage before first render; a runtime toggle
// control is a later batch, so these assert the stored-preference paths.
import {test, expect} from '@playwright/test';
import {TOKEN} from './_studio.mjs';
import {project} from './_parity.mjs';

const work = (id, extra = {}) => ({id, owner_bot_id: 'bot-main', goal: id, kind: 'conversation', status: 'active', attention: 'working', created: 100,...extra});

async function loginAt(page, locale) {
  await page.addInitScript((l) => localStorage.setItem('pangpang.uiLocale.v1', l), locale);
  await page.goto('/');
  await page.locator('#token').fill(TOKEN);
  await page.locator('#login-form button').click();
  await expect(page.locator('#bot-list .bot-item').first()).toBeVisible({timeout: 20000});
}

async function seed(page) {
  await project(page, (s) => {
    s.setup.capabilities = [
      {capability: 'model', state: 'ready', reason: null, operator_action: null},
      {capability: 'device', state: 'unconfigured', reason: 'no_device_paired', operator_action: null},
    ];
    s.work = [work('fresh', {goal: 'a fresh work', checkpoint: {phase: 'new', summary: 'just in'}}),
              work('done', {goal: 'done work', checkpoint: {phase: 'delivered', summary: 'done'}})];
  });
}

let errors;
test.beforeEach(async ({page}) => { errors = []; page.on('pageerror', (e) => errors.push(e.message)); });
test.afterEach(async ({page}) => { await page.unrouteAll({behavior: 'wait'}); expect(errors).toEqual([]); });

test('en-US first paint: capability rows and known phase in English; raw codes stay inspectable', async ({page}) => {
  await seed(page);
  await loginAt(page, 'en-US');
  const list = page.locator('#capability-list');
  const m = list.locator('[data-capability="model"]');
  await expect(m).toContainText('Chat model'); await expect(m).toContainText('Ready');
  await expect(list.locator('[data-capability="device"]')).toContainText('Not configured');
  for (const zh of ['对话模型', '就绪', '未配置']) await expect(list).not.toContainText(zh);
  // SIS: the raw code remains the title attribute; judgments use the code, not the label.
  await expect(list.locator('[data-capability="device"]')).toHaveAttribute('title', 'no_device_paired');

  await page.locator('[data-view="work"]').click();
  const facts = page.locator('#work-grid .facts');
  await expect(facts.filter({hasText: 'Just started'})).toHaveCount(1); //: known phase localized
  await expect(facts.filter({hasText: 'Delivered'})).toHaveCount(1);
  await expect(page.locator('#work-grid')).not.toContainText('刚开始');
});

test('zh-CN first paint: the same projection renders in Chinese', async ({page}) => {
  await seed(page);
  await loginAt(page, 'zh-CN');
  const list = page.locator('#capability-list');
  const m = list.locator('[data-capability="model"]');
  await expect(m).toContainText('对话模型'); await expect(m).toContainText('就绪');
  await expect(list.locator('[data-capability="device"]')).toContainText('未配置');
  for (const en of ['Chat model', 'Ready', 'Not configured']) await expect(list).not.toContainText(en);

  await page.locator('[data-view="work"]').click();
  await expect(page.locator('#work-grid .facts').filter({hasText: '刚开始'})).toHaveCount(1);
});

test('header language toggle flips the shell live and persists', async ({page}) => {
  await seed(page);
  await loginAt(page, 'zh-CN');
  const list = page.locator('#capability-list');
  const toggle = page.locator('#lang-toggle');
  await expect(list.locator('[data-capability="model"]')).toContainText('对话模型');
  await expect(toggle).toHaveText('EN'); // shows the language it switches TO
  await toggle.click(); // -> en-US, no reload
  await expect(list.locator('[data-capability="model"]')).toContainText('Chat model');
  await expect(list).not.toContainText('对话模型');
  await expect(toggle).toHaveText('中');
  // Persisted to localStorage so the choice survives a reload.
  const stored = await page.evaluate(() => localStorage.getItem('pangpang.uiLocale.v1'));
  expect(stored).toBe('en-US');
  await toggle.click(); // back to zh-CN
  await expect(list.locator('[data-capability="model"]')).toContainText('对话模型');
});

