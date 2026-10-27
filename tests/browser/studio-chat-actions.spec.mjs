// Status strip lives on 我的电脑. Assistant replies expose 拷贝 / 回复.
// Reply binds the composer to the tapped message's work id.
// The steward (kind main) stays first in the bot list; other bots follow
// the newest snapshot turn time.
import {test, expect} from '@playwright/test';
import {login, selectBot, snapshot, BOT} from './_studio.mjs';
import {project} from './_parity.mjs';

test('status strip is on my computers and absent from chat and work', async ({page}) => {
  await project(page, (s) => {
    s.setup.capabilities = [
      {capability: 'model', state: 'ready', reason: null, operator_action_code: null, operator_action: null},
    ];
  });
  await login(page);
  await expect(page.locator('#capability-list')).toHaveCount(0);
  await page.locator('[data-view="work"]').click();
  await expect(page.locator('#capability-list')).toHaveCount(0);
  await page.locator('[data-view="hosts"]').click();
  await expect(page.locator('#capability-list')).toBeVisible();
  await expect(page.locator('[data-capability="model"]')).toContainText('对话模型');
  await page.locator('[data-view="chat"]').click();
  await expect(page.locator('#capability-list')).toHaveCount(0);
});

test('copy writes the assistant plain text; reply chains the tapped work', async ({page}) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], {origin: 'http://127.0.0.1:8798'});
  await login(page);
  await selectBot(page, BOT.main);
  const snap = await snapshot(page);
  const works = (snap.work || []).filter((w) => w.owner_bot_id === 'bot-main' && w.kind === 'conversation' && !w.parent_work_id && w.goal);
  expect(works.length).toBeGreaterThanOrEqual(2);
  const [first, second] = works;
  const frame = JSON.stringify({kind: 'assistant', source: 'bot-main', content: '真正的答复', evidence_ref: 'msg-x'});
  await page.route('**/api/v1/bots/bot-main/messages', (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    return route.fulfill({json: [
      {id: 'user-1', created: 1, bot_id: 'bot-main', from: 'user', kind: 'user', content: '先说这个', work_id: first.id},
      {id: 'asst-1', created: 2, bot_id: 'bot-main', from: 'bot-main', kind: 'assistant', content: frame, work_id: first.id},
      {id: 'asst-2', created: 3, bot_id: 'bot-main', from: 'bot-main', kind: 'assistant', content: '第二段答复', work_id: second.id},
      {id: 'asst-3', created: 4, bot_id: 'bot-main', from: 'bot-main', kind: 'assistant', content: '没有工作'},
    ]});
  });
  await page.locator('#refresh').click();
  await expect(page.getByTestId('assistant-msg')).toHaveCount(3);
  await expect(page.locator('.bubble.user').getByTestId('msg-copy')).toHaveCount(0);
  const framed = page.getByTestId('assistant-msg').filter({hasText: '真正的答复'});
  const copy = framed.getByTestId('msg-copy');
  await expect(copy).toHaveText('拷贝');
  await copy.click();
  await expect(copy).toHaveText('✓');
  await expect(copy).toHaveAttribute('data-copied', 'true');
  await expect(copy).toHaveAttribute('aria-label', '拷贝');
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('真正的答复');

  await page.evaluate(() => {
    navigator.clipboard.writeText = () => Promise.reject(new Error('denied'));
  });
  const failed = page.getByTestId('assistant-msg').filter({hasText: '第二段答复'}).getByTestId('msg-copy');
  await expect(failed).toHaveText('拷贝');
  await failed.click();
  await failed.evaluate(() => new Promise((resolve) => setTimeout(resolve, 250)));
  await expect(failed).toHaveText('拷贝');
  await expect(failed).toHaveAttribute('data-copied', 'false');

  await expect(copy).toHaveText('拷贝', {timeout: 4500});
  await expect(copy).toHaveAttribute('data-copied', 'false');

  const unbound = page.getByTestId('assistant-msg').filter({hasText: '没有工作'});
  await expect(unbound.getByTestId('msg-reply')).toBeDisabled();

  await page.getByTestId('assistant-msg').filter({hasText: '第二段答复'}).getByTestId('msg-reply').click();
  await expect(page.getByTestId('continue-target')).toContainText(String(second.goal).slice(0, 12));
  await framed.getByTestId('msg-reply').click();
  await expect(page.getByTestId('continue-target')).toContainText(String(first.goal).slice(0, 12));

  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().includes('/messages') && r.method() === 'POST'),
    (async () => {
      await page.getByRole('textbox').fill('接着这一条');
      await page.getByRole('button', {name: '发送 →'}).click();
    })(),
  ]);
  expect(req.postDataJSON().continue_work_id).toBe(first.id);
});

test('steward stays first and other bots follow last turn time', async ({page}) => {
  await project(page, (s) => {
    const id = (name) => s.bots.find((b) => b.name === name).id;
    s.turns = [
      {id: 't-stop', bot_id: id('停止测试'), created: 500, status: 'succeeded'},
      {id: 't-edit', bot_id: id('文字编辑'), created: 400, status: 'succeeded'},
      {id: 't-note', bot_id: id('通知测试'), created: 400, status: 'succeeded'},
      {id: 't-main', bot_id: id('胖胖'), created: 1, status: 'succeeded'},
      {id: 't-bad', bot_id: id('审批测试'), status: 'succeeded'},
    ];
  });
  await login(page);
  const labels = await page.locator('#bot-list .bot-item').evaluateAll((els) => els.map((el) => el.textContent || ''));
  const at = (name) => labels.findIndex((text) => text.includes(name));
  expect(at('胖胖')).toBe(0);
  const ranked = ['停止测试', '文字编辑', '通知测试', '审批测试', '恢复测试', '取消测试', '隔离测试'];
  for (let i = 1; i < ranked.length; i++) expect(at(ranked[i - 1]), ranked[i - 1] + ' before ' + ranked[i]).toBeLessThan(at(ranked[i]));
});
