// A device setup_result is model input. The main chat shows the steward's
// written summary and does not dump the raw discover JSON.
import {test, expect} from '@playwright/test';
import {project} from './_parity.mjs';
import {login} from './_studio.mjs';

const msg = (id, created, content, extra = {}) => ({
  id, created, bot_id: 'bot-main', from: 'bot-main', kind: 'assistant', content, ...extra,
});

let errors;
test.beforeEach(async ({page}) => { errors = []; page.on('pageerror', (e) => errors.push(e.message)); });
test.afterEach(async ({page}) => {
  await page.unrouteAll({behavior: 'ignoreErrors'});
  expect(errors).toEqual([]);
});

test('discover and other setup_result payloads stay out of the chat timeline', async ({page}) => {
  await project(page, (s) => { s.tasks = []; }, [
    msg('user-ask', 1, '先把执行器注册一下', {kind: 'user', from: 'user'}),
    msg('disc', 2, {
      action: 'discover',
      host_id: 'host-2aac39ed29c8796f',
      inventory: [
        {adapter: 'claude-code', binary: '/usr/local/bin/claude', version: '2.1.252'},
        {adapter: 'sample-adapter', binary: '/home/example/.local/bin/sample-adapter', version: '1.0'},
      ],
    }, {kind: 'setup_result', from: 'device'}),
    msg('decl', 3, {
      action: 'declare_adapter',
      host_id: 'host-2aac39ed29c8796f',
      declared_adapters: [{adapter: 'sample-adapter', binary: 'sample-adapter'}],
    }, {kind: 'setup_result', from: 'device'}),
    msg('cfg', 4, {
      action: 'configure',
      host_id: 'host-2aac39ed29c8796f',
      accepted: true,
      installation_id: 'inst-sample',
    }, {kind: 'setup_result', from: 'device', consumed_by_turn_id: 'turn-1'}),
    msg('sum', 5, '探测到 claude-code 和 sample-adapter，目前尚未注册。', {turn_id: 'turn-1'}),
  ]);
  await login(page);
  const log = page.getByTestId('chat-log');
  await expect(log).toContainText('先把执行器注册一下');
  await expect(log).toContainText('探测到 claude-code 和 sample-adapter，目前尚未注册。');
  expect(await page.locator('#messages [data-timeline]').evaluateAll((es) => es.map((e) => e.dataset.timeline))).toEqual([
    'message-user-ask', 'message-sum',
  ]);
  await expect(log.locator('[data-kind="setup_result"]')).toHaveCount(0);
  await expect(log.locator('.evidence-raw')).toHaveCount(0);
  await expect(log.locator('.source', {hasText: 'device'})).toHaveCount(0);
  await expect(log).not.toContainText('/usr/local/bin/claude');
  await expect(log).not.toContainText('host-2aac39ed29c8796f');
  await expect(log).not.toContainText('"inventory"');
  await page.unrouteAll({behavior: 'ignoreErrors'});
});
