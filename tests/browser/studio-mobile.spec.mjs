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
const LONG = '测'.repeat(80) + '\n' + ('readable words '.repeat(40)) + '\n```\nconst n = 1;\n```\n';

async function assertChatLayout(page, width, height, {coarse}) {
  await page.setViewportSize({width, height});
  const report = await page.evaluate((short) => {
    const input = document.querySelector('#message').getBoundingClientRect();
    const send = document.querySelector('#send').getBoundingClientRect();
    const upload = document.querySelector('#upload').getBoundingClientRect();
    const attach = document.querySelector('#attach').getBoundingClientRect();
    const sharesRow = (a, b) => a.top < b.bottom - 1 && b.top < a.bottom - 1;
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
    const pre = document.querySelector('#messages .md-pre');
    if (!pre) return {missing: '.md-pre'};
    fonts.push({id: '.md-pre', fs: parseFloat(getComputedStyle(pre).fontSize)});
    const raw = document.querySelector('#messages .evidence-raw');
    if (raw) fonts.push({id: '.evidence-raw', fs: parseFloat(getComputedStyle(raw).fontSize)});
    const name = document.querySelector('#bot-name');
    const nameBox = name.getBoundingClientRect();
    const root = parseFloat(getComputedStyle(document.documentElement).fontSize);
    const contentInline = (el) => {
      const s = getComputedStyle(el);
      return el.getBoundingClientRect().width
        - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight)
        - parseFloat(s.borderLeftWidth) - parseFloat(s.borderRightWidth);
    };
    const paneInline = contentInline(document.querySelector('main'));
    const composerEl = document.querySelector('#composer');
    const messagesEl = document.querySelector('#messages');
    const messageEl = document.querySelector('#message');
    const composerInline = contentInline(composerEl);
    const transcriptInline = contentInline(messagesEl);
    const narrowLimit = 40 * root + 0.5;
    const firstBlock = body.firstElementChild;
    const caps = document.querySelector('#capability-list');
    const activity = document.querySelector('#activity');
    const activityBox = activity.getBoundingClientRect();
    const pill = document.querySelector('#attention-pill');
    const pillBox = pill ? pill.getBoundingClientRect() : null;
    for (const el of document.querySelectorAll('input, textarea, select')) {
      const type = el.getAttribute('type') || '';
      if (['checkbox', 'radio', 'file', 'hidden', 'range'].includes(type)) continue;
      const box = el.getBoundingClientRect();
      if (box.width < 1 || box.height < 1) continue;
      fonts.push({id: el.id || el.tagName, fs: parseFloat(getComputedStyle(el).fontSize)});
    }
    const targets = [];
    if (matchMedia('(pointer: coarse)').matches) {
      for (const sel of ['#send', '#refresh', '#lang-toggle', '#upload', '#attach', '#nav-toggle', '#attention-pill']) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const box = el.getBoundingClientRect();
        if (box.width < 1 || box.height < 1) continue;
        if (box.width < 44 || box.height < 44) targets.push({sel, w: box.width, h: box.height});
      }
    }
    return {
      sameRow: sharesRow(input, send) && send.left >= input.right - 1 && input.width > send.width
        && sharesRow(upload, input) && sharesRow(attach, input)
        && upload.right <= attach.left + 2 && attach.right <= input.left + 2,
      uploadW: upload.width, uploadH: upload.height, attachW: attach.width, attachH: attach.height,
      inputW: input.width, sendW: send.width,
      sendInView: send.right <= innerWidth + 1 && send.bottom <= innerHeight + 1,
      overflow: document.documentElement.scrollWidth - innerWidth,
      fonts,
      maxLine, capCh, capCjk,
      lineRatio: parseFloat(bodyStyle.lineHeight) / parseFloat(bodyStyle.fontSize),
      coarse: matchMedia('(pointer: coarse)').matches,
      targets,
      inputFont: parseFloat(getComputedStyle(document.querySelector('#message')).fontSize),
      nameWidth: nameBox.width,
      nameScroll: name.scrollWidth,
      nameFont: parseFloat(getComputedStyle(name).fontSize),
      nameText: name.textContent.trim(),
      activityText: activity.textContent.trim(),
      activityWidth: activityBox.width,
      pillText: pill ? pill.innerText.trim() : '',
      pillWidth: pillBox ? pillBox.width : 0,
      pillHeight: pillBox ? pillBox.height : 0,
      composerPad: parseFloat(getComputedStyle(composerEl).paddingBottom),
      composerPadTop: parseFloat(getComputedStyle(composerEl).paddingTop),
      hintHidden: document.querySelector('#composer .compose-hint').getBoundingClientRect().height < 1,
      expectedPad: ((short || paneInline <= narrowLimit) ? 0.375 : 0.75) * root,
      root,
      messageH: messageEl.getBoundingClientRect().height,
      composerH: composerEl.getBoundingClientRect().height,
      narrowComposer: composerInline <= narrowLimit,
      narrowPane: paneInline <= narrowLimit,
      narrowTranscript: transcriptInline <= narrowLimit,
      transcriptInline,
      bodyWidth: body.getBoundingClientRect().width,
      bodyPadTop: parseFloat(bodyStyle.paddingTop),
      bodyPadLeft: parseFloat(bodyStyle.paddingLeft),
      listPadLeft: parseFloat(getComputedStyle(messagesEl).paddingLeft),
      firstBlockMargin: firstBlock ? parseFloat(getComputedStyle(firstBlock).marginTop) : 0,
      capPadTop: caps ? parseFloat(getComputedStyle(caps).paddingTop) : null,
    };
  }, height <= 500);
  expect(report.missing, JSON.stringify(report)).toBeFalsy();
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
  expect(report.nameText.length).toBeGreaterThan(0);
  expect(report.nameWidth).toBeGreaterThanOrEqual(report.nameFont * 2);
  expect(report.nameWidth).toBeGreaterThanOrEqual(report.nameScroll - 1);
  if ((width === 320 && height === 568) || (width === 390 && height === 844)) {
    expect(report.activityText.length, 'activity at ' + width).toBeGreaterThan(0);
    expect(report.activityWidth, 'activity width at ' + width).toBeGreaterThan(8);
    expect(report.pillText, 'pending count at ' + width).toMatch(/\d/);
    expect(report.pillWidth, 'pending width at ' + width).toBeGreaterThanOrEqual(44);
    expect(report.pillHeight, 'pending height at ' + width).toBeGreaterThanOrEqual(44);
  }
  expect(Math.abs(report.composerPad - report.expectedPad)).toBeLessThanOrEqual(1);
  // Empty field: one line on a narrow composer, multi-line on a wide one.
  // A short viewport keeps the existing 3.5rem field so send stays on screen.
  if (height <= 500) {
    expect(report.messageH, 'short field at ' + width).toBeGreaterThanOrEqual(3.5 * report.root - 2);
    expect(report.messageH, 'short field at ' + width).toBeLessThanOrEqual(3.5 * report.root + 2);
  } else if (report.narrowComposer) {
    expect(report.messageH, 'compact field at ' + width).toBeGreaterThanOrEqual(2.75 * report.root - 2);
    expect(report.messageH, 'compact field at ' + width).toBeLessThanOrEqual(2.75 * report.root + 2);
    if (coarse) expect(report.messageH, 'field tap size at ' + width).toBeGreaterThanOrEqual(44);
  } else {
    expect(report.messageH, 'wide field at ' + width).toBeGreaterThanOrEqual(4.5 * report.root);
  }
  if (report.narrowTranscript) {
    expect(report.lineRatio, 'narrow leading at ' + width).toBeLessThanOrEqual(1.6);
    expect(report.bodyPadTop, 'bubble pad at ' + width).toBeLessThanOrEqual(0.375 * report.root + 1);
    expect(report.bodyPadLeft, 'bubble side pad at ' + width).toBeLessThanOrEqual(0.625 * report.root + 1);
    const bubbleCap = Math.min(report.transcriptInline, report.capCh * 72 / 75, 42 * report.root);
    expect(report.bodyWidth, 'bubble fill at ' + width).toBeGreaterThanOrEqual(bubbleCap - 2);
    if (report.transcriptInline <= 28 * report.root) {
      expect(report.bodyWidth / report.transcriptInline, 'phone bubble fill at ' + width).toBeGreaterThanOrEqual(0.96);
    }
    expect(report.firstBlockMargin, 'nested block margin at ' + width).toBeLessThanOrEqual(0.5);
  } else {
    expect(report.lineRatio, 'wide leading at ' + width).toBeGreaterThanOrEqual(1.65);
    expect(report.bodyPadLeft, 'wide bubble pad at ' + width).toBeGreaterThanOrEqual(0.75 * report.root - 1);
  }
  if (report.narrowPane) {
    expect(report.listPadLeft, 'list gutter at ' + width).toBeLessThanOrEqual(0.5 * report.root + 1);
  } else {
    expect(report.listPadLeft, 'wide gutter at ' + width).toBeGreaterThanOrEqual(0.75 * report.root - 1);
  }
  if (report.capPadTop !== null && report.narrowPane) {
    expect(report.capPadTop, 'status pad at ' + width).toBeLessThanOrEqual(0.25 * report.root + 1);
  }
  // File actions sit on the field's row. With the hint hidden, that row is the composer.
  if (report.hintHidden && height > 500) {
    const rowBudget = report.messageH + report.composerPad + report.composerPadTop + 8;
    expect(report.composerH, 'one control row at ' + width).toBeLessThanOrEqual(rowBudget);
  }
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

test.describe('responsive chat, fine pointer', () => {
  test.use({hasTouch: false, isMobile: false});
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
});
