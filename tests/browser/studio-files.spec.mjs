// Contract #9 + — chat upload produces an attachment chip; the device-file
// browser lets you SELECT a file and reference it (chip added); and a Chinese-named
// artifact downloads with an RFC 5987 UTF-8 Content-Disposition.
import {test, expect} from '@playwright/test';
import {login, selectBot, snapshot, BOT} from './_studio.mjs';

test('chinese-named artifact downloads with an RFC 5987 UTF-8 Content-Disposition', async ({page}) => {
  await login(page);
  const s = await snapshot(page);
  const art = s.artifacts.find(a => (a.path || '').includes('季度报告'));
  expect(art).toBeTruthy();
  const resp = await page.request.get('/api/v1/artifacts/' + encodeURIComponent(art.id) + '?download=1');
  expect(resp.ok()).toBeTruthy();
  const cd = resp.headers()['content-disposition'] || '';
  expect(cd).toContain("filename*=UTF-8''");
  expect(cd).toContain('%E5%AD%A3%E5%BA%A6%E6%8A%A5%E5%91%8A'); // 季度报告 percent-encoded
});

test('chat upload yields an attachment chip', async ({page}) => {
  await login(page);
  await selectBot(page, BOT.main);
  await page.locator('#file-pick').setInputFiles({name: '测试上传.txt', mimeType: 'text/plain', buffer: Buffer.from('hello pangpang')});
  await expect(page.locator('#attached')).toContainText('测试上传.txt', {timeout: 20000});
});

test('the device-file browser selects a workspace file and references it as a chip', async ({page}) => {
  await login(page);
  await selectBot(page, BOT.main);
  await page.locator('#attach').click();
  await expect(page.locator('#file-dialog')).toBeVisible();
  // The top level lists the allowed root as a directory (runner.py:141); descend into it.
  const root = page.locator('#file-browser .browse-item.dir').first();
  await expect(root).toBeVisible({timeout: 15000});
  await root.click();
  const file = page.locator('#file-browser .browse-item.file').first();
  await expect(file).toBeVisible({timeout: 15000}); // workspace files listed
  await file.click();
  const save = page.locator('#file-save');
  await expect(save).toBeEnabled(); // a file is selected
  await save.click();
  await expect(page.locator('#file-dialog')).toBeHidden();
  await expect(page.locator('#attached > *')).toHaveCount(1); // referenced file became an attachment
});
