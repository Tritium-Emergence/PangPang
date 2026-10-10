export const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=', 'base64');
export async function project(page, mutate, messages) {
  await page.route('**/api/v1/snapshot', async (route) => {
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({response});
    const s = await response.json(); mutate(s);
    return route.fulfill({response, json: s});
  });
  if (messages) await page.route('**/api/v1/bots/bot-main/messages', async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch();
    return response.ok() ? route.fulfill({response, json: messages}): route.fulfill({response});
  });
}

// Mirror the server's artifact history contract (pangpang/http.py "artifacts" over
// Store.find_page(newest=True)): newest-first rows, a limit+1 probe to learn whether
// a further page exists, and next_cursor = the last returned row's id, exclusive.
// The artifact browser reads THIS endpoint; /snapshot no longer feeds its cards, so
// a spec that needs artifact cards must route it. One deliberate divergence: the
// server 404s an unknown cursor (Store.keyset raises not_found) and no spec sends
// one, so this double does not model that branch.
export async function routeArtifacts(page, items) {
  const newestFirst = [...items].reverse();
  await page.route('**/api/v1/artifacts?*', (route) => {
    const q = new URL(route.request().url()).searchParams;
    const limit = Math.min(100, Math.max(1, parseInt(q.get('limit') || '20', 10)));
    const cursor = q.get('cursor');
    const start = cursor ? newestFirst.findIndex((a) => a.id === cursor) + 1: 0;
    const probe = newestFirst.slice(start, start + limit + 1);
    const rows = probe.slice(0, limit);
    const more = probe.length > limit;
    return route.fulfill({json: {items: rows, next_cursor: more && rows.length ? rows[rows.length - 1].id: null}});
  });
}

export const WEBM = Buffer.from('GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAIREU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEeTbuMU6uEHFO7a1OsggH77AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjAuMTYuMTAwV0GNTGF2ZjYwLjE2LjEwMESJiEB/QAAAAAAAFlSua8GuAQAAAAAAADjXgQFzxYjdeChcLXQReZyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhA7msoDgibCBELqBEJqBAhJUw2f8c3OgY8CAZ8iaRaOHRU5DT0RFUkSHjUxhdmY2MC4xNi4xMDBzc9ZjwItjxYjdeChcLXQReWfIoUWjh0VOQ09ERVJEh5RMYXZjNjAuMzEuMTAyIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDAuNTAwMDAwMDAwAB9DtnXX54EAo7uBAACAsAIAnQEqEAAQAABHCIWFiIWEiAICAnWqA/gCDP37AP77aJf/PTNeYP8nP/25H+3I/25H/7bCAKOVgQD6ALEBAAEQEAAYABhYL/QACAAAHFO7a5G7j7OBALeK94EB8YIBn/CBAw==', 'base64');

export const WAV = Buffer.from('UklGRmYDAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgATElTVBoAAABJTkZPSVNGVA4AAABMYXZmNjAuMTYuMTAwAGRhdGEgAwAAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIA=', 'base64');
