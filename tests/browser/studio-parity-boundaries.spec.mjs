// Additional boundary evidence for the parity matrix.
// Routes isolate network timing and inspect requests; backend validation is covered
// separately by Python integration tests, not claimed by these UI fixtures.
import {test, expect} from '@playwright/test';
import {login, gotoWork, BOT} from './_studio.mjs';
import {project, PNG, routeArtifacts} from './_parity.mjs';

test.afterEach(async ({page}) => { await page.unrouteAll({behavior: 'wait'}); });

test('file browser navigates parents, switches hosts and rejects stale browse responses', async ({page}) => {
  await project(page, s => { s.hosts.push({id: 'second-host', name: '第二设备', transport: 'connector'}); });
  let release, received = false, finished = false;
  const held = new Promise(r => { release = r; });
  await page.route('**/api/v1/hosts/*/browse', async r => {
    const path = r.request().postDataJSON().path;
    if (r.request().url().includes('/second-host/')) return r.fulfill({json: {path: '/second', entries: [{name: '新设备.txt', type: 'file'}]}});
    if (path === '/root/slow') { received = true; await held; await r.fulfill({json: {path, entries: [{name: '过期.txt', type: 'file'}]}}); finished = true; return; }
    return r.fulfill({json: path === '/root/sub' ? {path, parent: '/root', entries: [{name: '内部.txt', type: 'file'}]}: {path: '/root', parent: '', entries: [{name: 'sub', type: 'dir'}, {name: 'slow', type: 'dir'}]}});
  });
  await login(page); await page.locator('#attach').click();
  await page.locator('.browse-item').filter({hasText: 'sub'}).click();
  await page.locator('.browse-item.file').click(); await expect(page.locator('#file-selected')).toContainText('/root/sub/内部.txt');
  await page.getByRole('button', {name: '↑ 上一级'}).click(); await expect(page.locator('#file-save')).toBeDisabled();
  await page.locator('.browse-item').filter({hasText: 'slow'}).click(); await expect.poll(() => received).toBe(true);
  await page.locator('#file-host').selectOption('second-host'); await expect(page.locator('#file-browser')).toContainText('新设备.txt');
  release(); await expect.poll(() => finished).toBe(true);
  // A completed refresh provides another browser/server turn before the negative assertion.
  await page.locator('#file-browser').evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  await expect(page.locator('#file-browser')).not.toContainText('过期.txt');
  await expect(page.locator('.browse-path')).toHaveText('/second');
});

test('native modal contains keyboard focus and restores the opener after Escape', async ({page}) => {
  await page.route('**/api/v1/hosts/*/browse', r => r.fulfill({json: {path: '', entries: [{name: '文件.txt', type: 'file'}]}}));
  await login(page); await page.locator('#attach').click();
  const dialog = page.locator('#file-dialog');
  await expect(page.locator('.browse-item')).toHaveCount(1);
  for (const key of ['Tab', 'Shift+Tab']) {
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press(key);
      // At a native dialog wrap Chromium can briefly focus body; background
      // controls must remain unreachable even then.
      expect(await page.evaluate(() => document.activeElement === document.body || !!document.activeElement.closest('#file-dialog'))).toBe(true);
    }
  }
  await page.locator('#message').evaluate(e => e.focus());
  expect(await page.locator('#message').evaluate(e => e === document.activeElement)).toBe(false);
  await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0); await expect(page.locator('#attach')).toBeFocused();
});

// The test above opens the dialog with a pointer click, which only proves opener
// restoration on an engine that focuses a <button> when it is pressed. WebKit/Safari
// does not (a platform rule, not a bug), so there document.activeElement is still
// <body> when the modal mounts and Escape left focus stranded at the top of the
// document. Focus-on-press IS the default action of mousedown, so preventing it
// reproduces the WebKit rule exactly in any engine — including the Chromium project,
// where this defect was otherwise invisible. The throwaway probe button asserts the
// emulation really took effect, so this test can never pass for the wrong reason.
test('a modal restores its opener even when the engine does not focus the pressed control', async ({page}) => {
  await page.route('**/api/v1/hosts/*/browse', r => r.fulfill({json: {path: '', entries: [{name: '文件.txt', type: 'file'}]}}));
  await login(page);
  await page.evaluate(() => document.addEventListener('mousedown', (e) => e.preventDefault(), true));
  await page.evaluate(() => document.body.append(Object.assign(document.createElement('button'),
    {id: 'focus-probe', textContent: '探针', style: 'position:fixed;top:0;left:0;z-index:99'})));
  await page.locator('#focus-probe').click();
  expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
  await page.evaluate(() => document.getElementById('focus-probe').remove());

  await page.locator('#attach').click();
  await expect(page.locator('.browse-item')).toHaveCount(1);
  expect(await page.evaluate(() => !!document.activeElement.closest('#file-dialog'))).toBe(true);
  await page.keyboard.press('Escape');
  await expect(page.locator('#file-dialog')).toHaveCount(0);
  await expect(page.locator('#attach')).toBeFocused();
});

test('FileReader itself is fenced and dragleave clears highlight', async ({page}) => {
  let uploads = 0, sends = 0;
  await page.addInitScript(() => {
    const read = FileReader.prototype.readAsDataURL;
    window.releaseReads = [];
    FileReader.prototype.readAsDataURL = function(blob) { window.releaseReads.push(() => read.call(this, blob)); };
  });
  await page.route('**/api/v1/bots/*/uploads', r => { uploads++; return r.fulfill({json: {id: 'read-file', name: 'read.txt'}}); });
  page.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/messages')) sends++; });
  await login(page); await page.locator('#message').fill('读取期间不得发送');
  await page.locator('#file-pick').setInputFiles({name: 'read.txt', mimeType: 'text/plain', buffer: Buffer.from('a')});
  await expect(page.locator('#send')).toBeDisabled(); await page.locator('#message').press('Enter');
  expect(uploads).toBe(0); expect(sends).toBe(0);
  await page.evaluate(() => window.releaseReads.splice(0).forEach(r => r()));
  await expect(page.locator('#attached')).toContainText('read.txt'); await expect(page.locator('#send')).toBeEnabled(); expect(uploads).toBe(1);
  await page.locator('#composer').evaluate(e => { const d = new DataTransfer(); d.items.add(new File(['a'], 'a')); e.dispatchEvent(new DragEvent('dragover', {bubbles: true, dataTransfer: d})); });
  await expect(page.locator('#composer')).toHaveClass(/dropping/);
  await page.locator('#composer').dispatchEvent('dragleave', {relatedTarget: null}); await expect(page.locator('#composer')).not.toHaveClass(/dropping/);
});

test('upload accepts exactly 8 MiB and rejects one byte over without sending it', async ({page}) => {
  const names = [];
  await page.route('**/api/v1/bots/*/uploads', r => { names.push(r.request().postDataJSON().name); return r.fulfill({json: {id: 'boundary', name: 'exact.bin'}}); });
  await login(page);
  await page.locator('#file-pick').setInputFiles([
    {name: 'over.bin', mimeType: 'application/octet-stream', buffer: Buffer.alloc(8 * 1024 * 1024 + 1)},
    {name: 'exact.bin', mimeType: 'application/octet-stream', buffer: Buffer.alloc(8 * 1024 * 1024)},
  ]);
  await expect(page.locator('#notice')).toContainText('over.bin: 单个文件最大 8MB');
  await expect(page.locator('#send')).toBeEnabled(); await expect(page.locator('#attached')).toContainText('exact.bin');
  expect(names).toEqual(['exact.bin']); await expect(page.locator('#attached')).not.toContainText('over.bin');
});

test('Markdown image zoom opens and closes by button, backdrop and Escape', async ({page}) => {
  await project(page, s => { s.tasks = []; }, [{id: 'md-image', from: 'bot-main', kind: 'assistant', content: '![Markdown图片](/api/v1/artifacts/md-image?download=1)'}]);
  await page.route('**/api/v1/artifacts/md-image?download=1', r => r.fulfill({contentType: 'image/png', body: PNG}));
  await login(page);
  await expect.poll(() => page.locator('.md-img').evaluate(e => e.naturalWidth)).toBe(1);
  const hit = await page.locator('#messages .image-zoom').boundingBox(); expect(hit.width).toBeGreaterThanOrEqual(44); expect(hit.height).toBeGreaterThanOrEqual(44);
  for (const close of ['button', 'backdrop', 'escape']) {
    await page.locator('#messages .image-zoom').click(); await expect(page.locator('#lightbox')).toBeVisible();
    if (close === 'button') await page.getByRole('button', {name: '关闭图片预览'}).click();
    if (close === 'backdrop') await page.mouse.click(1, 1);
    if (close === 'escape') await page.keyboard.press('Escape');
    await expect(page.locator('#lightbox')).toHaveCount(0);
  }
});

test('recovery submits distinct conclusions and evidence, cancellation submits nothing', async ({page}) => {
  await project(page, s => { s.tasks = [{id: 'resolve-proof', status: 'unknown', prompt: '隔离任务'}]; });
  const submitted = [];
  await page.route('**/api/v1/tasks/resolve-proof/resolve', r => { submitted.push(r.request().postDataJSON()); return r.fulfill({json: {}}); });
  await login(page); await gotoWork(page);
  const evidence = '已经核对设备进程与启动记录，核验结论属实';
  let cancel = false;
  page.on('dialog', async d => { if (cancel) await d.dismiss(); else await d.accept(d.type() === 'prompt' ? evidence: undefined); });
  for (const [label, resolution] of [['确认从未启动', 'confirmed_never_started'], ['确认已停止', 'confirmed_stopped']]) {
    await page.locator('#tasks').getByRole('button', {name: label}).click();
    await expect.poll(() => submitted.some(x => x.resolution === resolution)).toBe(true);
    expect(submitted.at(-1)).toEqual({resolution, evidence});
  }
  cancel = true; await page.locator('#tasks').getByRole('button', {name: '确认已停止'}).click();
  await expect(page.locator('#tasks').getByRole('button', {name: '确认已停止'})).toBeEnabled(); expect(submitted).toHaveLength(2);
});

test('a new local profile card submits the typed endpoint and shows the command', async ({page}) => {
  await project(page, (s) => {
    s.setup.requests = [{
      id: 'new-local', status: 'needs_approval', host_id: 'local',
      proposal: {
        action: 'executor_config', installation_id: null, adapter: 'codex',
        command: ['/usr/bin/codex', 'exec', '-'],
        draft: {profile: 'work', cwd: '/work'},
      },
    }];
  });
  await login(page);
  const panel = page.locator('#setup-panel');
  await expect(panel.locator('input[type="url"]')).toBeVisible();
  await expect(panel).toContainText('/usr/bin/codex');
  await expect(panel).toContainText('exec');
  let sent;
  await page.route('**/api/v1/setup/new-local/approve', (r) => {
    sent = r.request().postDataJSON();
    return r.fulfill({json: {accepted: true}});
  });
  await panel.locator('input[type="url"]').fill('https://127.0.0.1:9/v1');
  await panel.getByRole('button', {name: '应用这些设置'}).click();
  await expect.poll(() => sent).toEqual({endpoint: 'https://127.0.0.1:9/v1'});
});

test('an existing executor card does not ask for an endpoint', async ({page}) => {
  await project(page, (s) => {
    s.setup.requests = [{
      id: 'edit-local', status: 'needs_approval', host_id: 'local',
      proposal: {action: 'executor_config', installation_id: 'codex-local', adapter: 'codex', draft: {cwd: '/work'}},
    }];
  });
  await login(page);
  await expect(page.locator('#setup-panel input[type="url"]')).toHaveCount(0);
});

test('hosts keeps pairing and the device list without an executor edit form', async ({page}) => {
  await login(page);
  await page.locator('[data-view="hosts"]').click();
  await expect(page.locator('#hosts .device-section')).toHaveCount(1);
  await expect(page.locator('#pair-device')).toHaveCount(1);
  await expect(page.locator('#hosts details')).toHaveCount(0);
  await expect(page.getByText('保存配置')).toHaveCount(0);
  await expect(page.getByText('配置执行器')).toHaveCount(0);
});

test('a projected remote device stays visible without an executor edit form', async ({page}) => {
  await project(page, s => {
    s.hosts.push({id: 'home-pc', name: '家里的开发机', transport: 'connector'});
    s.devices.push({id: 'home-pc', online: true});
    s.installations.push({id: 'codex-home-pc', host_id: 'home-pc', adapter: 'codex', cwd: '/home/dev/proj',
      max_concurrency: 1, timeout_s: 3600, enabled: true, priority: 30, use_when: '写代码'});
  });
  await login(page);
  await page.locator('[data-view="hosts"]').click();
  const remote = page.locator('#hosts .device-section').filter({hasText: '家里的开发机'});
  await expect(remote).toHaveCount(1);
  await expect(remote.getByText('codex-home-pc')).toHaveCount(1);
  await expect(remote.getByRole('button', {name: '撤销设备连接'})).toHaveCount(1);
  await expect(remote.locator('[data-config]')).toHaveCount(0);
  await expect(page.locator('#hosts details')).toHaveCount(0);
  await expect(page.getByText('保存配置')).toHaveCount(0);
});

test('sidebar avatar and busy indicator follow server attention, not raw work status', async ({page}) => {
  let attention = 'working';
  await project(page, s => { s.work = [{id: 'busy-proof', owner_bot_id: 'bot-main', status: 'active', attention}]; });
  await login(page);
  const main = page.locator('#bot-list .bot-item').filter({hasText: BOT.main});
  await expect(main.locator('.bot-avatar')).toHaveText('◉'); await expect(main.getByLabel('推进中')).toHaveCount(1);
  await expect(page.locator('#bot-list .bot-item').filter({hasText: BOT.aux}).locator('.bot-avatar')).toHaveText('◇');
  for (const value of ['blocked', 'settled', 'unknown']) {
    attention = value; await page.locator('#refresh').click(); await expect(main.getByLabel('推进中')).toHaveCount(0);
  }
  attention = 'working'; await page.locator('#refresh').click(); await expect(main.getByLabel('推进中')).toHaveCount(1);
});

for (const kind of ['uploads', 'files']) test(`late ${kind} completion cannot attach after Bot A to B to A`, async ({page}) => {
  let release, received = false;
  const held = new Promise(r => { release = r; });
  await page.route(`**/api/v1/bots/*/${kind}`, async r => { received = true; await held; await r.fulfill({json: {id: 'stale-attachment', name: '过期附件.txt'}}); });
  await page.route('**/api/v1/hosts/*/browse', r => r.fulfill({json: {path: '/root', entries: [{name: '引用.txt', type: 'file'}]}}));
  await login(page);
  if (kind === 'uploads') await page.locator('#file-pick').setInputFiles({name: '上传.txt', mimeType: 'text/plain', buffer: Buffer.from('a')});
  else {
    await page.locator('#attach').click(); await page.locator('.browse-item.file').click(); await page.locator('#file-save').click();
    await expect(page.locator('#file-save')).toBeDisabled(); await page.locator('#file-close').click();
  }
  await expect.poll(() => received).toBe(true);
  await page.locator('#bot-list .bot-item').filter({hasText: BOT.aux}).click(); await expect(page.locator('#bot-name')).toHaveText(BOT.aux);
  await page.locator('#bot-list .bot-item').filter({hasText: BOT.main}).click(); await expect(page.locator('#bot-name')).toHaveText(BOT.main);
  const response = page.waitForResponse(r => r.url().endsWith('/' + kind) && r.request().method() === 'POST');
  release(); await (await response).finished();
  // Drain in-flight handlers and refresh through the actual UI before inspecting.
  await page.unrouteAll({behavior: 'wait'}); await page.locator('#refresh').click();
  await expect(page.locator('#attached > *')).toHaveCount(0); await expect(page.locator('#send')).toBeEnabled();
});

test('a thousand loaded artifacts remain ten DOM cards and counts never claim full history', async ({page}) => {
  await routeArtifacts(page, Array.from({length: 1000}, (_, i) => ({id: `bounded-${i}`, path: `文件${i}.txt`})));
  await login(page); await gotoWork(page);
  await expect(page.locator('#artifacts >.panel')).toHaveCount(10);
  await expect(page.locator('#artifacts .artifact-pager .pager-info')).toContainText('第 1 页');
  await expect(page.locator('#artifacts .artifact-pager')).not.toContainText('共');
  await expect(page.locator('#work-view')).toContainText('服务端 keyset 分页');
  await page.locator('#artifacts .artifact-pager').getByRole('button', {name: '下一页 ›'}).click();
  await expect(page.locator('#artifacts >.panel')).toHaveCount(10);
  await expect(page.locator('#artifacts .artifact-pager .pager-info')).toContainText('第 2 页');
});
