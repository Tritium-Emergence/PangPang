// Contract — unread is surfaced as a bot-list badge whose count matches server
// truth and updates live; there is no inline notification strip in the chat. Opening
// or viewing a conversation does not mark notifications read. Unread stays until an
// explicit mark-notification, and the badge follows that server truth.
import {test, expect} from '@playwright/test';
import {login, selectBot, snapshot, markNotificationRead, BOT} from './_studio.mjs';

const unreadFor = async (page, botId) => (await snapshot(page)).notifications.filter(n => n.bot_id === botId && !n.read);
const badgeFor = (page, name) => page.locator('#bot-list .bot-item', {hasText: name}).locator('.unread-count');
const isReadPost = (req) => req.method() === 'POST' && /\/notifications\/[^/]+\/read(?:\?|$)/.test(req.url());

// The removed read-on-view effect posted as soon as a chat mounted. Wait long enough
// for that request to show up, and fail if viewing the chat issued one.
async function expectNoAutoRead(page, readPosts) {
  const before = readPosts.length;
  try {
    await page.waitForRequest(isReadPost, {timeout: 1500});
    throw new Error('viewing the chat posted a notification read');
  } catch (err) {
    if (String(err).includes('viewing the chat posted a notification read')) throw err;
    if (!/Timeout/.test(String(err))) throw err;
  }
  expect(readPosts).toHaveLength(before);
}

test('unread stays until an explicit read; opening a chat leaves server unread counts unchanged', async ({page}) => {
  const readPosts = [];
  page.on('request', (req) => { if (isReadPost(req)) readPosts.push(req.url()); });

  await login(page); // bot-main's chat is open on login
  await expect(page.locator('#notifications')).toHaveCount(0);

  const mainUnread = await unreadFor(page, 'bot-main');
  expect(mainUnread.length).toBeGreaterThan(0);
  const mainBadge = badgeFor(page, BOT.main);
  await expect(mainBadge).toHaveText(String(mainUnread.length));

  const notifyId = (await snapshot(page)).bots.find(b => b.name === BOT.notify).id;
  const unread = await unreadFor(page, notifyId);
  expect(unread.length).toBeGreaterThan(1);
  const badge = badgeFor(page, BOT.notify);
  await expect(badge).toHaveText(String(unread.length)); // badge for a bot not being viewed

  await selectBot(page, BOT.notify); // opening this chat must not read
  await expect(mainBadge).toHaveText(String(mainUnread.length));
  await selectBot(page, BOT.main); // the login bot's chat must not read either
  await expectNoAutoRead(page, readPosts);
  expect(await unreadFor(page, notifyId)).toHaveLength(unread.length);
  expect(await unreadFor(page, 'bot-main')).toHaveLength(mainUnread.length);
  await expect(badge).toHaveText(String(unread.length));
  await expect(mainBadge).toHaveText(String(mainUnread.length));

  await markNotificationRead(page, unread[0].id); // explicit API read -> SSE -> live badge
  await expect.poll(async () => (await unreadFor(page, notifyId)).length).toBe(unread.length - 1);
  await expect(badge).toHaveText(String(unread.length - 1));
  expect(await unreadFor(page, 'bot-main')).toHaveLength(mainUnread.length);
  await expect(mainBadge).toHaveText(String(mainUnread.length));
  expect(readPosts).toHaveLength(1);
});
