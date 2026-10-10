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

// 待办 inbox: an unread result bound to a Work lists as a clickable "done, unread"
// item that jumps to its Bot's chat and marks the notification read (the follow-up
// entry the old dead count never gave). work_id-less unread stays in the Bot badge.
test('an unread result item opens its Bot and marks the notification read', async ({page}) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await login(page);
  const panel = page.getByTestId('attention-panel');
  const live = await snapshot(page);
  const nf = (live.notifications || []).find(x => !x.read && x.kind === 'result' && x.work_id);
  expect(nf, 'fixture provides a work-bound unread result').toBeTruthy();
  const item = panel.locator(`[data-notification="${nf.id}"]`);
  await expect(item).toBeVisible();
  await expect(item).toContainText(nf.title);
  const posts = [];
  page.on('request', r => { if (r.method() === 'POST' && r.url().includes(`/notifications/${nf.id}/read`)) posts.push(r.url()); });
  await item.click();
  // Jumps into that Bot's chat.
  const owner = live.bots.find(b => b.id === nf.bot_id).name;
  await expect(page.locator('#bot-name')).toHaveText(owner);
  await expect(page.getByTestId('chat-log')).toBeVisible();
  // Marks it read (best-effort POST fired) and the item leaves the inbox after refresh.
  await expect.poll(() => posts.length).toBeGreaterThan(0);
  await expect(panel.locator(`[data-notification="${nf.id}"]`)).toHaveCount(0);
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
