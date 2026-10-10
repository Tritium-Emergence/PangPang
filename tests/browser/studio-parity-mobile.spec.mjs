import {test, expect} from '@playwright/test';
import {login} from './_studio.mjs';
import {PNG, project, routeArtifacts} from './_parity.mjs';

test.afterEach(async ({page}) => { await page.unrouteAll({behavior: 'wait'}); });

test('mobile parity: narrow screens and landscape keep every pane, modal and media reachable', async ({page}) => {
  const errors = [], csp = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (e) => { if (/Content Security Policy|Refused to apply|Refused to execute/i.test(e.text())) csp.push(e.text()); });
  await project(page, (s) => {
    s.artifacts = [{id: 'mobile-image', path: '手机图片.png', size: 80}]; s.tasks = [];
    s.work = [{id: 'mobile-work', owner_bot_id: 'bot-main', goal: '手机完整要求', kind: 'conversation', attention: 'settled', status: 'delivered'}];
  }, [{id: 'mobile-msg', kind: 'assistant', from: 'bot-main', content: '| 表头一 | 表头二 |\n| --- | --- |\n| 较长内容 | 手机表格 |' + '\n\n|' + Array(20).fill('宽列').join('|') + '|\n|' + Array(20).fill('---').join('|') + '|\n|' + Array(20).fill('内容').join('|') + '|', artifact_refs: ['mobile-image']}]);
  await page.route('**/api/v1/artifacts/mobile-image?download=1', (r) => r.fulfill({contentType: 'image/png', body: PNG}));
  await routeArtifacts(page, [{id: 'mobile-image', path: '手机图片.png', size: 80}]);
  await page.route('**/api/v1/hosts/*/browse', (r) => r.fulfill({json: {path: '/workspace', entries: Array.from({length: 31}, (_, i) => ({name: '手机文件' + i + '.txt', type: 'file', size: 1024}))}}));
  await page.setViewportSize({width: 390, height: 844}); await login(page);
  const pane = async (name) => {
    if (await page.locator('#nav-toggle').isVisible()) await page.locator('#nav-toggle').click();
    await page.locator(`[data-view="${name}"]`).click();
    if (await page.locator('#nav-toggle').isVisible()) {
      await expect.poll(() => page.locator('#bot-list').evaluate((e) => e.closest('aside').getBoundingClientRect().right)).toBeLessThanOrEqual(0);
    }
  };
  for (const [width, height] of [[320, 568], [360, 800], [390, 844], [768, 1024], [844, 390]]) {
    await page.setViewportSize({width, height}); await pane('chat');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    expect(await page.locator('#composer').evaluate((e) => e.getBoundingClientRect().bottom <= innerHeight + 1)).toBeTruthy();
    await expect(page.locator('#message')).toBeInViewport();
    await expect(page.locator('#send')).toBeInViewport();
    const table = page.locator('.md-table-scroll').last();
    expect(await table.evaluate(e => e.scrollWidth > e.clientWidth)).toBe(true);
    await table.evaluate(e => { e.scrollLeft = e.scrollWidth; });
    expect(await table.evaluate(e => e.scrollLeft)).toBeGreaterThan(0);
    await page.locator('#attach').click();
    await expect(page.locator('.browse-item')).toHaveCount(31);
    const box = await page.locator('#file-dialog').boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width + 1);
    await page.locator('#file-browser').evaluate((e) => { e.scrollTop = e.scrollHeight; });
    await page.locator('.browse-item').last().click(); await expect(page.locator('#file-selected')).toContainText('手机文件30');
    await page.locator('#file-close').click();
    await page.locator('#messages .image-zoom').click(); await expect(page.locator('#lightbox')).toBeVisible();
    await page.getByRole('button', {name: '关闭图片预览'}).click();
    await pane('work'); await expect(page.locator('#work-grid')).toContainText('手机完整要求');
    await page.getByRole('button', {name: '查看详情', exact: true}).click(); await expect(page.locator('#work-detail-dialog')).toContainText('完整要求'); await page.locator('#work-detail-close').click();
    await expect(page.locator('#artifacts .media-img')).toBeVisible();
    await pane('hosts'); await expect(page.locator('#settings')).toBeVisible(); await page.locator('#settings').click();
    const settings = await page.locator('#settings-dialog').boundingBox(); expect(settings.width).toBeLessThanOrEqual(width);
    await page.locator('#settings-close').click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  }
  await page.setViewportSize({width: 390, height: 844}); await pane('chat');
  await page.unrouteAll({behavior: 'wait'});
  expect(errors).toEqual([]); expect(csp).toEqual([]);
  // WebKit's screenshot synchronization injects body{} (Playwright screenshotter);
  // audit all product actions before that test-runner-only CSP event.
  await page.screenshot({path: test.info().outputPath('mobile-parity.png'), caret: 'initial'});
});

test('mobile remaining modals fit short screens and secret drafts clear on close', async ({page}) => {
  await project(page, s => {
    s.setup.requests = [{id: 'secret-mobile', host_id: 'local', status: 'needs_secret'}];
  });
  await page.route('**/api/v1/setup/pairing', r => r.fulfill({json: {code: '123456'}}));
  await page.route('**/api/v1/setup/secret-mobile/secret-target', r => r.fulfill({json: {host_id: 'local', installation_id: 'cli', endpoint: 'https://example.com', storage: 'device'}}));
  await page.setViewportSize({width: 320, height: 568}); await login(page);
  const check = async (id, close) => {
    const dialog = page.locator(id); await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox(), viewport = page.viewportSize();
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 1); expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 1);
    await page.locator(close).scrollIntoViewIfNeeded(); await expect(page.locator(close)).toBeInViewport();
    await page.locator(close).click(); await expect(dialog).toHaveCount(0);
  };
  for (const size of [{width: 320, height: 568}, {width: 844, height: 390}]) {
    await page.setViewportSize(size);
    if (await page.locator('#nav-toggle').isVisible()) await page.locator('#nav-toggle').click();
    await page.locator('#add-bot').click(); await check('#bot-dialog', '#close-dialog');
    await page.locator('[data-view="hosts"]').click();
    await page.locator('#pair-device').click(); await check('#pair-dialog', '#pair-close');
    if (await page.locator('#nav-toggle').isVisible()) await page.locator('#nav-toggle').click();
    await page.locator('[data-view="chat"]').click();
    await page.getByRole('button', {name: '打开密钥窗口'}).click();
    await page.locator('#device-secret').fill('test-only-secret'); await check('#secret-dialog', '#secret-close');
    await page.getByRole('button', {name: '打开密钥窗口'}).click(); await expect(page.locator('#device-secret')).toHaveValue('');
    await check('#secret-dialog', '#secret-close');
    if (size.width < 640) await expect(page.locator('#message')).toHaveCSS('font-size', '16px');
  }
});
