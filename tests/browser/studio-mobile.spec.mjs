// Contract — mobile viewport (390×844): no horizontal overflow, the Chat/Composer
// stay the primary surface and reachable, and the sidebar is a real drawer (hidden by
// default, opened via the header toggle, closes on selection). Chromium cannot prove
// iOS safe-area insets — that needs real-device verification, out of scope here.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {test, expect} from '@playwright/test';
import {login} from './_studio.mjs';
import {project} from './_parity.mjs';

const SHOTS = path.join(os.tmpdir(), 'responsive-shots');
fs.mkdirSync(SHOTS, {recursive: true});

async function saveShot(page, width, height) {
  await page.screenshot({path: path.join(SHOTS, `responsive_${width}x${height}.png`)});
}

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

// Phone, foldable, tablet, and desktop widths. Touch is emulated on the coarse set.
const COARSE_SIZES = [[320, 568], [360, 800], [390, 844], [430, 932], [600, 960], [768, 1024], [915, 412], [1024, 768]];
const FINE_SIZES = [[1280, 800], [1920, 1080]];
const LONG = '测'.repeat(80) + '\n' + ('readable words '.repeat(40));

async function assertChatLayout(page, width, height, {coarse}) {
  await page.setViewportSize({width, height});
  const report = await page.evaluate(() => {
    const input = document.querySelector('#message').getBoundingClientRect();
    const send = document.querySelector('#send').getBoundingClientRect();
    const body = document.querySelector('#messages .body');
    const bodyStyle = getComputedStyle(body);
    const probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;font:' + bodyStyle.font;
    probe.textContent = '0'.repeat(75);
    document.body.appendChild(probe);
    const capCh = probe.getBoundingClientRect().width;
    probe.textContent = '测'.repeat(45);
    const capCjk = probe.getBoundingClientRect().width;
    probe.remove();
    let maxLine = 0;
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!String(node.textContent).trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const rect of range.getClientRects()) maxLine = Math.max(maxLine, rect.width);
    }
    const fonts = [{id: '#message', fs: parseFloat(getComputedStyle(document.querySelector('#message')).fontSize)}];
    fonts.push({id: '.body', fs: parseFloat(bodyStyle.fontSize)});
    for (const el of document.querySelectorAll('input, textarea, select')) {
      const type = el.getAttribute('type') || '';
      if (['checkbox', 'radio', 'file', 'hidden', 'range'].includes(type)) continue;
      const box = el.getBoundingClientRect();
      if (box.width < 1 || box.height < 1) continue;
      fonts.push({id: el.id || el.tagName, fs: parseFloat(getComputedStyle(el).fontSize)});
    }
    const targets = [];
    if (matchMedia('(pointer: coarse)').matches) {
      for (const sel of ['#send', '#refresh', '#lang-toggle', '#upload', '#attach', '#nav-toggle']) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const box = el.getBoundingClientRect();
        if (box.width < 1 || box.height < 1) continue;
        if (box.width < 44 || box.height < 44) targets.push({sel, w: box.width, h: box.height});
      }
    }
    return {
      sameRow: input.top < send.bottom - 1 && send.top < input.bottom - 1 && send.left >= input.right - 1 && input.width > send.width,
      sendInView: send.right <= innerWidth + 1 && send.bottom <= innerHeight + 1,
      overflow: document.documentElement.scrollWidth - innerWidth,
      fonts,
      maxLine, capCh, capCjk,
      lineRatio: parseFloat(bodyStyle.lineHeight) / parseFloat(bodyStyle.fontSize),
      coarse: matchMedia('(pointer: coarse)').matches,
      targets,
      inputFont: parseFloat(getComputedStyle(document.querySelector('#message')).fontSize),
    };
  });
  expect(report.sameRow, JSON.stringify({width, height, report})).toBeTruthy();
  expect(report.sendInView, JSON.stringify(report)).toBeTruthy();
  expect(report.overflow).toBeLessThanOrEqual(1);
  for (const font of report.fonts) expect(font.fs, font.id + ' at ' + width).toBeGreaterThanOrEqual(16);
  expect(report.maxLine).toBeLessThanOrEqual(report.capCh + 2);
  expect(report.maxLine).toBeLessThanOrEqual(report.capCjk + 2);
  expect(report.lineRatio).toBeGreaterThanOrEqual(1.5);
  expect(report.lineRatio).toBeLessThanOrEqual(1.8);
  expect(report.coarse).toBe(coarse);
  if (coarse) expect(report.targets, JSON.stringify(report.targets)).toEqual([]);
  await saveShot(page, width, height);
}

test.describe('responsive chat, coarse pointer', () => {
  test.use({hasTouch: true, isMobile: true, viewport: {width: 390, height: 844}});
  test('viewport matrix keeps one composer row, readable type, and 44px targets', async ({page}) => {
    test.setTimeout(120000);
    await project(page, () => {}, [{id: 'measure-msg', kind: 'assistant', from: 'bot-main', content: LONG}]);
    await page.setViewportSize({width: 390, height: 844});
    await login(page);
    for (const [width, height] of COARSE_SIZES) await assertChatLayout(page, width, height, {coarse: true});
    await page.unrouteAll({behavior: 'ignoreErrors'});
  });
});

test('viewport matrix on a fine pointer keeps one composer row and the measure cap', async ({page}) => {
  test.setTimeout(90000);
  await project(page, () => {}, [{id: 'measure-msg', kind: 'assistant', from: 'bot-main', content: LONG}]);
  await page.setViewportSize({width: 1280, height: 800});
  await login(page);
  const safeArea = await page.evaluate(() => {
    for (const sheet of document.styleSheets) {
      let rules;
      try { rules = [...sheet.cssRules]; } catch { continue; }
      const visit = (list) => {
        for (const rule of list) {
          if (rule.cssRules) { if (visit([...rule.cssRules])) return true; }
          if (String(rule.selectorText || '').includes('composer') && String(rule.style?.paddingBottom || '').includes('safe-area-inset-bottom')) return true;
        }
        return false;
      };
      if (visit(rules)) return true;
    }
    return false;
  });
  expect(safeArea).toBeTruthy();
  for (const [width, height] of FINE_SIZES) await assertChatLayout(page, width, height, {coarse: false});
  await page.unrouteAll({behavior: 'ignoreErrors'});
});
