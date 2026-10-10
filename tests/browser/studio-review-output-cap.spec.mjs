// / review findings, against the committed shell with snapshot fixtures.
import {test, expect} from '@playwright/test';
import {login, gotoWork} from './_studio.mjs';
import {project} from './_parity.mjs';

const msg = (id, created, content, extra = {}) => ({id, created, bot_id: 'bot-main', from: 'bot-main', kind: 'assistant', content,...extra});
const work = (id, extra = {}) => ({id, owner_bot_id: 'bot-main', goal: id, kind: 'conversation', status: 'active', attention: 'working', created: 100,...extra});
let errors;
test.beforeEach(async ({page}) => { errors = []; page.on('pageerror', (e) => errors.push(e.message)); });
test.afterEach(async ({page}) => { await page.unrouteAll({behavior: 'wait'}); expect(errors).toEqual([]); });

test('-1 capability rows and a new Work phase render in Chinese; codes stay inspectable', async ({page}) => {
  await project(page, (s) => {
    s.setup.capabilities = [
      {capability: 'model', state: 'ready', reason: null, operator_action_code: null, operator_action: null},
      {capability: 'device', state: 'unconfigured', reason: 'no_device_paired', operator_action_code: 'pair_device', operator_action: '在 Web「配对设备」生成配对码并在目标主机运行连接器。'},
      {capability: 'semantic_gate', state: 'ready', reason: null, operator_action_code: null, operator_action: null},
    ];
    s.work = [work('fresh', {goal: '新工作', checkpoint: {phase: 'new', summary: '刚收到的要求'}})];
  });
  await login(page);
  const list = page.locator('#capability-list');
  // Capability pills: label + state text, with a state-colored dot (data-state).
  const model = list.locator('[data-capability="model"]');
  await expect(model).toContainText('对话模型');
  await expect(model).toContainText('就绪');
  await expect(model).toHaveAttribute('data-state', 'ready');
  const device = list.locator('[data-capability="device"]');
  await expect(device).toContainText('设备');
  await expect(device).toContainText('未配置');
  await expect(device).toHaveAttribute('data-state', 'unconfigured');
  // The action text comes from the operator_action_code -> capabilityAction map
  // (localized), surfaced via the pill's title — never the server's raw Chinese
  // operator_action, and the raw code never leaks into visible text.
  await expect(device).toHaveAttribute('title', /配对设备/);
  await expect(list).toContainText('语义校验');
  for (const code of ['no_device_paired', 'semantic_gate', 'unconfigured']) await expect(list).not.toContainText(code);

  await gotoWork(page);
  const facts = page.locator('#work-grid .facts').first();
  await expect(facts).toContainText('当前阶段刚开始');
  await expect(facts).not.toContainText('new');
});

test('-2 a Work-scoped progress bubble names its Work; the Bot-wide thinking bubble does not', async ({page}) => {
  let thinking = false;
  await project(page, (s) => {
    s.work = [work('herbs', {goal: '香草三步指南'}), work('lan', {goal: '局域网备份', status: 'delivered', attention: 'settled'})];
    s.turns = thinking ? [{id: 'think', bot_id: 'bot-main', work_id: 'herbs', status: 'running'}]: []; s.exchanges = [];
    s.tasks = [{id: 'step-a', status: 'running', work_id: 'herbs', installation_id: 'inst-local-claude-code', prompt: 'research 香草'}];
  }, [
    msg('u-herbs', 1, '帮我做香草三步指南', {from: 'user', kind: 'user', work_id: 'herbs'}),
    msg('a-lan', 2, '局域网备份任务已取消。', {work_id: 'lan'}),
  ]);
  await login(page);
  const bubble = page.locator('.thinking');
  await expect(bubble).toContainText('已派 inst-local-claude-code 执行：research 香草');
  await expect(bubble.locator('.source')).toContainText('香草三步指南');
  // Any queued/running turn of this Bot (any Work) shows 正在思考…; it names no Work.
  thinking = true; await page.locator('#refresh').click();
  await expect(bubble).toContainText('正在思考');
  await expect(bubble.locator('.source')).toHaveText('胖胖');
});

test(' a blocked current Work shows the recorded reason, and only a recorded one', async ({page}) => {
  let inSnapshot = true;
  await project(page, (s) => {
    s.work = [work('herbs', {goal: '香草三步指南', attention: 'blocked'})];
    s.tasks = []; s.exchanges = [];
    s.turns = inSnapshot ? [{id: 'wake', bot_id: 'bot-main', work_id: 'herbs', status: 'blocked', error: 'model_response_truncated', created: 3}]: [];
  }, [msg('u-herbs', 1, '帮我做香草三步指南', {from: 'user', kind: 'user', work_id: 'herbs'})]);
  await login(page);
  const inspector = page.locator('#work-inspector');
  await expect(inspector).toContainText('受阻');
  // The action it names exists: the settings dialog's 输出上限 field (test below).
  await expect(inspector.getByTestId('blocked-reason')).toHaveText('受阻原因：模型回复超过输出上限（output_tokens）被截断，这一轮已停止。可在「设置全局对话模型」调高输出上限，再发消息让它继续。（model_response_truncated）');
  // The snapshot's turn window is bounded; a blocked turn outside it yields no reason, not a guess.
  inSnapshot = false; await page.locator('#refresh').click();
  await expect(inspector).toContainText('受阻');
  await expect(inspector.getByTestId('blocked-reason')).toHaveCount(0);
});

test(' the settings dialog shows and sets the output cap, never below the floor', async ({page}) => {
  await login(page);
  await page.locator('[data-view="hosts"]').click();
  await page.locator('#settings').click();
  const cap = page.locator('#model-output-tokens');
  await expect(cap).toHaveValue('32768');
  await expect(cap).toHaveAttribute('min', '32768');
  // A catalog model whose own maximum is lower: that maximum is both the value and the floor.
  const low = await page.evaluate(async () => {
    const c = await (await fetch('/api/v1/setup/model-catalog', {credentials: 'same-origin'})).json();
    for (const [provider, models] of Object.entries(c.providers))
      for (const [id, m] of Object.entries(models)) if (m.maxTokens < 32768) return {provider, id, max: String(m.maxTokens)};
  });
  await page.locator('#model-provider').selectOption(low.provider);
  await page.locator('#model-name').fill(low.id);
  await expect(cap).toHaveValue(low.max);
  await expect(cap).toHaveAttribute('min', low.max);
  await page.locator('#model-provider').selectOption('custom');
  await page.locator('#model-name').fill('qwen-local');
  await page.locator('#model-endpoint').fill('http://127.0.0.1:9999/v1');
  await page.locator('#model-no-auth').check();
  await expect(cap).toHaveValue('32768');
  let submitted;
  await page.route('**/api/v1/setup/model', async (route) => {
    submitted = route.request().postDataJSON();
    await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({code: 'fixture_stop', message: 'fixture_stop', retryable: false})});
  });
  await cap.fill('1200');
  await page.locator('#model-save').click(); // below the floor: the form does not submit
  expect(submitted).toBeUndefined();
  await cap.fill('65536');
  await page.locator('#model-save').click();
  await expect(page.locator('#model-error')).toContainText('fixture_stop');
  expect(submitted).toMatchObject({provider: 'custom', model: 'qwen-local', output_tokens: 65536});
});
