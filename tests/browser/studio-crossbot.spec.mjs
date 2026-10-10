// cross-bot async isolation regression: an async completion started for Bot A must
// never modify (or briefly show on) Bot B after a switch. Covers both the chat upload
// and the device-file reference. The guard is the store's LIVE selection epoch
// (store.liveSelected()), not a stale render closure.
import {test, expect} from '@playwright/test';
import {login, selectBot, BOT} from './_studio.mjs';

test('an upload started on Bot A does not land on Bot B after a switch', async ({page}) => {
  await login(page);
  await selectBot(page, BOT.main);

  // Hold the upload response until released.
  let release;
  const held = new Promise((r) => { release = r; });
  await page.route('**/api/v1/bots/*/uploads', async (route) => { await held; await route.continue(); });

  // Start an upload on bot-main.
  await page.locator('#file-pick').setInputFiles({name: 'A的附件.txt', mimeType: 'text/plain', buffer: Buffer.from('a')});
  // Switch to another bot while the upload is in flight.
  await selectBot(page, BOT.aux);
  // Release the held upload now that we are on Bot B.
  release();
  await page.waitForTimeout(600);

  // Bot B's composer must NOT show A's attachment (no cross-bot leak).
  await expect(page.locator('#attached > *')).toHaveCount(0);
  await page.unroute('**/api/v1/bots/*/uploads');
});

test('a device-file reference started on Bot A does not land on Bot B after a switch', async ({page}) => {
  await login(page);
  await selectBot(page, BOT.main);

  let release;
  const held = new Promise((r) => { release = r; });
  await page.route('**/api/v1/bots/*/files', async (route) => { await held; await route.continue(); });

  // Open the device-file dialog, descend into the workspace, pick a file, click 引用文件.
  await page.locator('#attach').click();
  await expect(page.locator('#file-dialog')).toBeVisible();
  await page.locator('#file-browser .browse-item.dir').first().click();
  await expect(page.locator('#file-browser .browse-item.file').first()).toBeVisible({timeout: 15000});
  await page.locator('#file-browser .browse-item.file').first().click();
  await page.locator('#file-save').click(); // POST /files is now held
  // Native modal traps focus. Dismiss it while the reference is held, then switch.
  await page.locator('#file-close').click();

  // Switch bots while the reference is in flight, then release.
  await selectBot(page, BOT.aux);
  release();
  await page.waitForTimeout(600);

  await expect(page.locator('#attached > *')).toHaveCount(0);
  await page.unroute('**/api/v1/bots/*/files');
});
