import {test, expect} from '@playwright/test';
import {login, snapshot} from './_studio.mjs';

const ACT = ['needs_attention', 'blocked'];

// real-browser gate: the attention projection reaches the client and renders in the
// right pane's 待处理 panel — the five summary counts verbatim, plus the loaded Works the
// server marks as needing the user, each opening the place to act on it.
test('待处理 renders the server projection and each item opens its Work ', async ({page}) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await login(page);

  // The server owns the projection: snapshot carries attention_summary (five
  // numeric buckets) and per-work attention. The client does not re-derive it.
  const snap = await snapshot(page);
  for (const k of ['needs_attention', 'working', 'done_unread', 'blocked', 'scheduled']) {
    expect(typeof snap.attention_summary[k]).toBe('number');
  }
  for (const w of (snap.work || [])) {
    expect(['needs_attention', 'blocked', 'working', 'done_unread', 'settled']).toContain(w.attention);
  }

  // The 待办 inbox lists only actionable items (no non-clickable count row any more).
  await expect(page.locator('#attention-bar')).toHaveCount(0);
  const panel = page.getByTestId('attention-panel');
  await expect(panel).toBeVisible({timeout: 20000});
  const live = await snapshot(page);

  // Listed Works are exactly the loaded ones the server marks needs_attention/blocked,
  // minus a child whose listed parent already carries it.
  const marked = live.work.filter(w => ACT.includes(w.attention));
  const ids = new Set(marked.map(w => w.id));
  const expected = marked.filter(w => !w.parent_work_id || !ids.has(w.parent_work_id)).map(w => w.id).sort();
  expect(expected.length).toBeGreaterThan(0);
  const listed = await panel.locator('[data-work]').evaluateAll(els => els.map(e => e.dataset.work).sort());
  expect(listed).toEqual(expected);

  // Clicking an item opens its Bot's chat with that Work current in the inspector.
  const target = live.work.find(w => w.id === expected[0]);
  const owner = live.bots.find(b => b.id === target.owner_bot_id).name;
  await panel.locator(`[data-work="${target.id}"]`).click();
  await expect(page.locator('#bot-name')).toHaveText(owner);
  await expect(page.getByTestId('chat-log')).toBeVisible();
  await expect(page.getByTestId('work-inspector')).toContainText(target.goal);
  expect(errors).toEqual([]);
});

test('narrow screens: the 待处理 count is one button that opens the Work pane', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await login(page);
  const s = (await snapshot(page)).attention_summary;
  const n = s.needs_attention + s.blocked;
  expect(n).toBeGreaterThan(0);
  await expect(page.getByTestId('attention-panel')).toBeHidden();
  const pill = page.locator('#attention-pill');
  await expect(pill).toHaveText(`待处理 ${n}`);
  await pill.click();
  await expect(page.locator('#work-view')).toBeVisible();
});

// Unread completed results and peer messages are not 待处理 cards. They stay on
// the bot-list badge. The panel lists only items the user must act on.
test('unread results stay off 待处理 and on the bot badge', async ({page}) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await login(page);
  const panel = page.getByTestId('attention-panel');
  await expect(panel).toBeVisible();
  const live = await snapshot(page);
  const unread = (live.notifications || []).filter(x => !x.read && (x.kind === 'result' || x.kind === 'peer_message'));
  expect(unread.length).toBeGreaterThan(0);
  const bound = unread.filter(x => x.work_id);
  expect(bound.length, 'fixture provides a work-bound unread result').toBeGreaterThan(0);

  await expect(panel.locator('[data-notification]')).toHaveCount(0);
  await expect(panel).not.toContainText('已完成待看');
  for (const nf of bound) await expect(panel).not.toContainText(nf.title);

  await expect(panel.locator('[data-work]').first()).toBeVisible();
  await expect(panel.locator('[data-routine]').first()).toBeVisible();

  const botId = bound[0].bot_id;
  const bot = live.bots.find(b => b.id === botId);
  const count = (live.notifications || []).filter(n => n.bot_id === botId && !n.read).length;
  await expect(page.locator('#bot-list .bot-item', {hasText: bot.name}).locator('.unread-count')).toHaveText(String(count));
  expect(errors).toEqual([]);
});

// A pending routine is a clickable inbox item that opens the Work pane to approve it.
test('a pending routine lists as a clickable item that opens the Work pane', async ({page}) => {
  await login(page);
  const panel = page.getByTestId('attention-panel');
  const routine = panel.locator('[data-routine]').first();
  await expect(routine).toBeVisible();
  await routine.click();
  await expect(page.locator('#work-view')).toBeVisible();
});
