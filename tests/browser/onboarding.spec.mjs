import {test, expect} from '@playwright/test';
test('first setup, real Pi bot creation, device configuration and sealed credential', async ({page}) => {
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  await page.setViewportSize({width:1366,height:768});
  await page.addInitScript(() => localStorage.setItem('pangpang.uiLocale.v1', 'zh-CN'));
  await page.goto('/');
  await page.locator('#token').fill('browser-access-token-32-characters');
  await page.locator('#login-form button').click();
  await page.getByRole('button',{name:'设置模型', exact:true}).click();
  const endpoint=await page.locator('#model-name').inputValue();
  await page.locator('#model-endpoint').fill(endpoint);
  await page.locator('#model-name').fill('test');
  await page.locator('#model-secret').fill('BROWSER-MODEL-KEY');
  await page.locator('#model-save').click();
  await expect(page.locator('#settings-dialog')).not.toBeVisible({timeout:20000});
  await expect(page.locator('#work-select')).toHaveCount(0);
  await expect(page.locator('#project-select')).toHaveCount(0);
  await expect(page.locator('.compose-hint')).toContainText('自己关联已有工作和项目');
  await expect(page.locator('#notifications')).toHaveCount(0); // no inline notification strip in the chat
  // A clean install ships exactly one bot, the default 「胖胖」; 「公众号主理」 is
  // user-created (owner ruling). Assert the clean state, then create it
  // through the real Pi tool round-trip.
  await expect(page.locator('#bot-list .bot-item')).toHaveCount(1);
  await expect(page.locator('#bot-list .bot-item')).toHaveText(/胖胖/);
  await page.locator('#message').fill('创建公众号主理');
  await page.locator('#send').click();
  await expect(page.locator('#bot-list')).toContainText('公众号主理',{timeout:20000});
  await page.locator('#message').fill('连接 Codex');
  await page.locator('#send').click();
  await page.getByRole('button',{name:'应用这些设置'}).click({timeout:20000});
  await page.getByRole('button',{name:'打开密钥窗口'}).click({timeout:20000});
  await page.locator('#device-secret').fill('BROWSER-SECRET-EXACT');
  await page.locator('#secret-save').click();
  await expect(page.locator('#secret-dialog')).not.toBeVisible();
  await expect.poll(async()=>page.evaluate(async()=> {
    const s=await (await fetch('/api/v1/snapshot')).json();
    return s.setup.inventory.some(i=>i.id==='browser-secret-check');
  }),{timeout:20000}).toBeTruthy();
  const snapshot=await page.evaluate(async()=>await (await fetch('/api/v1/snapshot')).json());
  const serialized=JSON.stringify(snapshot);
  expect(serialized).not.toContain('BROWSER-SECRET-EXACT');
  expect(serialized).not.toContain('BROWSER-MODEL-KEY');
  expect(Array.isArray(snapshot.routines)).toBeTruthy();
  expect(Array.isArray(snapshot.notifications)).toBeTruthy();
  expect(Array.isArray(snapshot.exchanges)).toBeTruthy();
  await expect(page.locator('#messages')).not.toContainText('BROWSER-SECRET-EXACT');

  for(const size of [{width:1280,height:720},{width:1366,height:768},{width:1536,height:864},{width:1920,height:1080}]){
    await page.setViewportSize(size);
    await expect(page.locator('#composer')).toBeVisible();
    await expect(page.locator('#send')).toBeVisible();
    const overflow=await page.evaluate(()=>({
      horizontal:document.documentElement.scrollWidth>document.documentElement.clientWidth,
      vertical:document.documentElement.scrollHeight>document.documentElement.clientHeight
    }));
    expect(overflow.horizontal).toBeFalsy();
    expect(overflow.vertical).toBeFalsy();
  }

  await page.locator('[data-view="work"]').click();
  await expect(page.locator('#projects-grid')).toBeVisible();
  await expect(page.locator('#routines')).toBeVisible();
  await expect(page.locator('#recovery')).toBeVisible();
  await expect(page.locator('#routines')).toContainText('暂无例行任务');
  await page.locator('[data-view="hosts"]').click();
  await expect(page.locator('#hosts')).toContainText('浏览器测试电脑');
  await page.locator('#pair-device').click();
  await expect(page.locator('#pair-code')).toHaveText(/^[A-Za-z0-9_-]{32}$/);
  await page.locator('#pair-close').click();
  await page.screenshot({path:'test-results/workspace.png',fullPage:true});
  expect(errors).toEqual([]);
});
