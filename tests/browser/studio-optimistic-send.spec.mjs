// Send paints the user bubble and clears the field before the POST settles.
// A failure removes that bubble and puts the draft back when the field is still empty.
import {test, expect} from '@playwright/test';
import {login} from './_studio.mjs';

test('send shows the user bubble before the request finishes and rolls it back on failure', async ({page}) => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await login(page);
  await page.route('**/api/v1/bots/*/messages', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    await held;
    return route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({code: 'temporary'})});
  });
  await page.locator('#message').fill('马上能看见');
  await page.locator('#send').click();
  await expect(page.locator('#message')).toHaveValue('');
  await expect(page.getByTestId('pending-msg')).toContainText('马上能看见');
  await expect(page.locator('#send')).toBeDisabled();
  release();
  await expect(page.locator('#notice')).toContainText('temporary');
  await expect(page.getByTestId('pending-msg')).toHaveCount(0);
  await expect(page.locator('#message')).toHaveValue('马上能看见');
  await expect(page.locator('#send')).toBeEnabled();
});

test('a newer draft survives a failed send', async ({page}) => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await login(page);
  await page.route('**/api/v1/bots/*/messages', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    await held;
    return route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({code: 'temporary'})});
  });
  await page.locator('#message').fill('前一条');
  await page.locator('#send').click();
  await expect(page.getByTestId('pending-msg')).toContainText('前一条');
  await page.locator('#message').fill('后一条草稿');
  release();
  await expect(page.locator('#send')).toBeEnabled();
  await expect(page.locator('#message')).toHaveValue('后一条草稿');
  await expect(page.getByTestId('pending-msg')).toHaveCount(0);
});
