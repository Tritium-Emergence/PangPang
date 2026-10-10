// Contract — mobile viewport (390×844): no horizontal overflow, the Chat/Composer
// stay the primary surface and reachable, and the sidebar is a real drawer (hidden by
// default, opened via the header toggle, closes on selection). Chromium cannot prove
// iOS safe-area insets — that needs real-device verification, out of scope here.
import {test, expect} from '@playwright/test';
import {login} from './_studio.mjs';

test('mobile: chat usable, no overflow, sidebar is a working drawer', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  // CSP gate: fail if the server's style-src 'self' / script-src 'self' blocks anything
  // (would mean an inline style/script slipped back in).
  const csp = [];
  page.on('console', (msg) => { if (/Content Security Policy|Refused to apply|Refused to execute/i.test(msg.text())) csp.push(msg.text()); });
  await login(page);

  // Chat + composer are the primary surface and on-screen (bot-main is the default).
  await expect(page.getByTestId('composer')).toBeVisible();
  await expect(page.locator('#message')).toBeVisible();
  const m = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth, iw: window.innerWidth,
    cb: document.querySelector('[data-testid="composer"]').getBoundingClientRect().bottom,
    ih: window.innerHeight,
  }));
  expect(m.sw).toBeLessThanOrEqual(m.iw + 1); // no horizontal overflow
  expect(m.cb).toBeLessThanOrEqual(m.ih + 1); // composer not pushed off-screen

  // The sidebar is a drawer: off-canvas by default, so the composer owns the width.
  const toggle = page.locator('#nav-toggle');
  await expect(toggle).toBeVisible();
  const botItem = page.locator('#bot-list .bot-item').first();
  // closed: the bot-list button is off the left edge (not within the viewport).
  const closedBox = await botItem.boundingBox();
  expect(closedBox === null || closedBox.x < 0).toBeTruthy();

  // open the drawer -> bot-list is now within the viewport and clickable.
  await toggle.click();
  await expect(botItem).toBeInViewport();
  await botItem.click(); // selecting closes the drawer
  await expect(botItem).not.toBeInViewport();
  await expect(page.getByTestId('composer')).toBeVisible();
  expect(csp).toEqual([]); // no CSP violations (no inline style/script)
});
