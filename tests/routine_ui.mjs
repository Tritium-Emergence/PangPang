// Real app.js + setup.js rendering and snapshot-bound approval in Chromium.
import {chromium, expect} from '@playwright/test';
const [origin, id] = process.argv.slice(2);
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage();
  const errors=[];
  page.on('pageerror', e=>errors.push(e.message));
  await page.goto(origin);
  await page.locator('#token').fill('x'.repeat(32));
  await page.locator('#login-form button').click();
  await page.locator('[data-view="work"]').click();
  const panel=page.locator('#routines');
  await expect(panel.getByRole('button',{name:'批准并启用'})).toBeVisible();
  await expect(panel.getByRole('button',{name:'恢复',exact:true})).toHaveCount(0);
  await expect(panel.getByRole('button',{name:'现在跑一次',exact:true})).toHaveCount(0);
  await expect(panel).toContainText('Bot 长期指令');
  await expect(panel).toContainText('默认不预授写入');
  await expect(panel).not.toContainText('只读');
  const reviewed=await page.evaluate(id=>snapshot.routines.find(r=>r.id===id).approval_digest,id);
  const requestPromise=page.waitForRequest(r=>r.url().endsWith(`/routines/${id}/approve`));
  await panel.getByRole('button',{name:'批准并启用'}).click();
  const request=await requestPromise;
  expect(request.postDataJSON()).toEqual({digest:reviewed,authority:{grant_write:false}});
  await expect(panel.getByRole('button',{name:'暂停',exact:true})).toBeVisible();
  await expect(panel.getByRole('button',{name:'现在跑一次',exact:true})).toBeVisible();
  await expect(panel).toContainText('授权：未预授写入');
  await expect(panel).not.toContainText('只读');
  expect(errors).toEqual([]);
  console.log('PASS: final render, displayed policy, exact snapshot digest, approved actions');
} finally {
  await browser.close();
}
