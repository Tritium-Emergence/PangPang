// Contract #3 (correction lineage) — the OTHER half of "explicit Work continue /
// correction lineage". A user correction ("不是，我说的是刚才…")
// re-attributes a mis-routed message to the TARGET Work, and the control plane records
// it APPEND-ONLY: the raw message keeps its original work_id; a routing_correction fact
// carries the new lineage. This is the behavior most at risk of being lost when the
// shell reworks tab adjacency / last_seen / work hints, so it is pinned at the
// HTTP/browser layer (not just the Python classifier unit test). Driven entirely
// through the normal browser send path against deterministic fixture routing.
import {test, expect} from '@playwright/test';
import {login, selectBot, snapshot, waitQuiet, BOT} from './_studio.mjs';

async function send(page, text) {
  await page.getByRole('textbox').fill(text);
  const [response] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/messages') && r.request().method() === 'POST' && r.request().postDataJSON()?.text === text),
    page.getByRole('button', {name: '发送 →'}).click(),
  ]);
  expect(response.ok()).toBeTruthy();
  await expect(page.getByRole('button', {name: '发送 →'})).toBeEnabled();
  return response.json();
}

test('a correction re-attributes to the target Work (append-only; raw message unchanged)', async ({page}) => {
  await login(page);
  await selectBot(page, BOT.main);

  // 1) Establish two conversation Works in THIS tab: A (阿里) then B (冰箱). The second
  // message is the one that will later be corrected.
  const sentA = await send(page, '阿里估值怎么样');
  await waitQuiet(page, 'bot-main');
  const sentB = await send(page, '冰箱怎么样');
  await waitQuiet(page, 'bot-main');
  // Wait for this tab to observe the actual latest event, not an arbitrary delay.
  const latest = await page.evaluate(async () => (await (await fetch('/api/v1/bots/bot-main/messages')).json()).at(-1).id);
  await expect.poll(() => page.evaluate(() => { const sid = sessionStorage.getItem('conversationStreamId:bot-main'); return sessionStorage.getItem('lastSeenEventId:bot-main:' + sid); })).toBe(latest);
  const s = await snapshot(page);
  const workA = s.work.find(w => w.id === (sentA.resolved_work_id || sentA.work_id));
  const workB = s.work.find(w => w.id === (sentB.resolved_work_id || sentB.work_id));
  expect(workA && workB).toBeTruthy();
  expect(workA.id).not.toBe(workB.id);

  // 2) Send the correction and capture the real /messages POST response (server truth).
  const [resp] = await Promise.all([
    page.waitForResponse(r => r.url().includes('/messages') && r.request().method() === 'POST'),
    send(page, '不是，我说的是刚才阿里那个'),
  ]);
  const body = await resp.json();
  expect(body.resolved_work_id || body.work_id).toBe(workA.id); // committed to the TARGET Work
  expect(body.routing_correction_id).toBeTruthy(); // a routing_correction fact exists

  // 3) Append-only: the raw mis-attributed message still belongs to Work B (not rewritten).
  const msgs = await page.evaluate(async () => (await fetch('/api/v1/bots/bot-main/messages', {credentials: 'same-origin'})).json());
  const raw = msgs.find(m => m.kind === 'user' && m.work_id === workB.id && (m.content || '').includes('冰箱怎么样'));
  expect(raw).toBeTruthy();
  expect(raw.work_id).toBe(workB.id);
});
