// Contract #5 — paginating a dynamic-height grid keeps the section ANCHORED: the
// section top barely moves across the page change (app.js pageGo
// records the rect top and re-anchors via rAF). Tested on #work-grid (a mid-document
// grid, so paging is not confounded by the scroll-bottom clamp that the last section
// would hit). The pager is scrolled into view first so the click itself does not
// auto-scroll and perturb the measurement.
import {test, expect} from '@playwright/test';
import {login, gotoWork} from './_studio.mjs';

test('paginating the work grid anchors the section top across the page change', async ({page}) => {
  await login(page);
  await gotoWork(page);
  const grid = page.locator('#work-grid');
  const pager = grid.locator('.pager');
  await expect(pager).toBeVisible({timeout: 15000}); // >10 works -> multiple pages
  const next = pager.getByRole('button', {name: '下一页 ›'});
  await next.scrollIntoViewIfNeeded();

  const before = await grid.evaluate(e => e.getBoundingClientRect().top);
  await next.click();
  await expect(grid.locator('.pager-info')).toContainText('第 2');
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))); // let pageGo re-anchor
  const after = await grid.evaluate(e => e.getBoundingClientRect().top);

  expect(Math.abs(after - before)).toBeLessThan(30); // section top held — no viewport jump
});


test('artifact browser pages through the server keyset cursor', async ({page}) => {
  await login(page);
  const first = await page.evaluate(async () => {
    const r = await fetch('/api/v1/artifacts?limit=5');
    return r.json();
  });
  expect(first.items).toHaveLength(5);
  expect(first.next_cursor).toBeTruthy();
  const second = await page.evaluate(async (cursor) => {
    const r = await fetch('/api/v1/artifacts?limit=5&cursor=' + encodeURIComponent(cursor));
    return r.json();
  }, first.next_cursor);
  expect(second.items.length).toBeGreaterThan(0);
  expect(new Set(first.items.map((x) => x.id)).has(second.items[0].id)).toBe(false);

  await gotoWork(page);
  const grid = page.locator('#artifacts');
  const pager = grid.locator('.artifact-pager');
  await expect(pager).toBeVisible({timeout: 15000});
  await expect(pager.locator('.pager-info')).toContainText('第 1 页');
  const firstPageIds = await grid.locator('.artifact-card').evaluateAll((cards) => cards.map((card) => card.getAttribute('data-artifact')));
  await pager.getByRole('button', {name: '下一页 ›'}).click();
  await expect(pager.locator('.pager-info')).toContainText('第 2 页');
  const secondPageIds = await grid.locator('.artifact-card').evaluateAll((cards) => cards.map((card) => card.getAttribute('data-artifact')));
  expect(secondPageIds.length).toBeGreaterThan(0);
  expect(secondPageIds.length).toBeLessThanOrEqual(10);
  expect(secondPageIds.some((id) => firstPageIds.includes(id))).toBe(false);
});
