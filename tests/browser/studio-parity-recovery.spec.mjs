// User-visible contracts. Responses below isolate PRESENTATION fixtures per
// browser; approval/stop/idempotency still run against the real server in their
// existing specs. No test-only backend routes and no client-side reality engine.
import {test, expect} from '@playwright/test';
import {login, gotoWork, BOT, selectBot} from './_studio.mjs';

import {PNG, WEBM, WAV, project, routeArtifacts} from './_parity.mjs';
const msg = (id, created, content, extra = {}) => ({id, created, bot_id: 'bot-main', from: 'bot-main', kind: 'assistant', content,...extra});
const work = (id, extra = {}) => ({id, owner_bot_id: 'bot-main', goal: id, kind: 'conversation', status: 'active', attention: 'working', created: 100,...extra});
let errors;
test.beforeEach(async ({page}) => { errors = []; page.on('pageerror', (e) => errors.push(e.message)); });
test.afterEach(async ({page}) => { await page.unrouteAll({behavior: 'wait'}); expect(errors).toEqual([]); });

test('four independent pagers bound cards to ten and report the exact loaded total', async ({page}) => {
  await project(page, (s) => {
    s.work = Array.from({length: 23}, (_, i) => work('w' + i));
    s.routines = Array.from({length: 23}, (_, i) => ({id: 'r' + i, instruction: '计划' + i, active: true, approval_state: 'approved', schedule: {type: 'daily', time: '04:00'}, timezone: 'Asia/Shanghai'}));
    s.tasks = Array.from({length: 23}, (_, i) => ({id: 't' + i, status: 'succeeded', prompt: '任务' + i}));
    s.claims = [];
  });
  await routeArtifacts(page, Array.from({length: 23}, (_, i) => ({id: 'a' + i, path: 'file' + i + '.txt'})));
  await login(page); await gotoWork(page);
  // Work/Routine/Task page a bounded snapshot, so they can report its exact total.
  for (const id of ['work-grid', 'routines', 'tasks']) {
    const grid = page.locator('#' + id), pager = grid.locator('.pager');
    await expect(grid.locator(':scope >.panel')).toHaveCount(10);
    await expect(pager.getByRole('button', {name: '‹ 上一页'})).toBeDisabled();
    await expect(pager).toContainText('共 23 项');
    await pager.getByRole('button', {name: '下一页 ›'}).click();
    await expect(pager).toContainText('第 2');
    await pager.getByRole('button', {name: '下一页 ›'}).click();
    await expect(grid.locator(':scope >.panel')).toHaveCount(3);
    await expect(pager.getByRole('button', {name: '下一页 ›'})).toBeDisabled();
    await pager.getByRole('button', {name: '‹ 上一页'}).click();
    await expect(pager).toContainText('第 2');
    await expect(grid.locator(':scope >.panel')).toHaveCount(10);
  }
  // Artifact pages the server keyset cursor instead: same 10-card bound and the same
  // first/last-page disabling, but no total — it has not loaded the whole history and
  // the pager must not imply it has (requirement 08).
  const art = page.locator('#artifacts'), artPager = art.locator('.artifact-pager');
  await expect(art.locator(':scope >.panel')).toHaveCount(10);
  await expect(artPager.getByRole('button', {name: '‹ 上一页'})).toBeDisabled();
  await expect(artPager.locator('.pager-info')).toContainText('第 1 页');
  await expect(artPager).not.toContainText('共');
  await artPager.getByRole('button', {name: '下一页 ›'}).click();
  await expect(artPager.locator('.pager-info')).toContainText('第 2 页');
  await artPager.getByRole('button', {name: '下一页 ›'}).click();
  await expect(art.locator(':scope >.panel')).toHaveCount(3);
  await expect(artPager.getByRole('button', {name: '下一页 ›'})).toBeDisabled();
  await artPager.getByRole('button', {name: '‹ 上一页'}).click();
  await expect(artPager.locator('.pager-info')).toContainText('第 2 页');
  await expect(art.locator(':scope >.panel')).toHaveCount(10);
  await expect(page.locator('#artifacts')).not.toContainText('上限');
});

// An exchange is in scope if EITHER end is: a delegation's source Work can sit outside
// the current subtree while its target Work is inside. The chat kept a second copy of
// the predicate that short-circuited on source_work_id (`a || b`), so it disagreed with
// scope.ts on this exact row and fell back to the generic waiting line.
test('a pending exchange whose source Work is out of scope still names the delegate', async ({page}) => {
  await project(page, (s) => {
    s.tasks = []; s.turns = []; s.claims = [];
    s.work = [work('w-cur', {goal: '当前工作'}), work('w-outside', {goal: '范围外', parent_work_id: undefined})];
    s.exchanges = [{id: 'ex-split', source_work_id: 'w-outside', work_id: 'w-cur', source_bot_id: 'bot-main', target_bot_id: 'bot-aux', status: 'pending', request: '拆开的往来'}];
  }, [{id: 'u', created: 1, kind: 'user', from: 'user', work_id: 'w-cur', content: '请继续'}]);
  await login(page);
  await expect(page.locator('#messages')).toContainText('拆开的往来');
  await expect(page.locator('#messages')).not.toContainText('正在等待执行结果');
});

// A task status display.ts knows about must not leak its raw English name: the task
// card kept a private copy of the status map that was missing `pending` and `blocked`.
test('task cards translate every status display.ts knows', async ({page}) => {
  await project(page, (s) => {
    s.tasks = [{id: 't-1', status: 'pending', prompt: '待执行任务'}, {id: 't-2', status: 'blocked', prompt: '受阻任务'}];
  });
  await login(page); await gotoWork(page);
  // Assert the status element itself, not the card text: the card also prints the task
  // id in its evidence JSON, so a substring check would hit "t-pending" and pass/fail
  // for the wrong reason.
  for (const [status, label] of [['pending', '待执行'], ['blocked', '受阻']]) {
    await expect(page.locator(`#tasks .task[data-status="${status}"] span`).first()).toHaveText(label);
  }
});

// A failed artifact page read must report the failure and otherwise change NOTHING:
// the reader keeps their page, their cards and a working 下一页. Pinned because the
// tempting "recovery" — resetting the cursor chain to page 1 — would yank someone
// browsing deep history back to the top on one transient failure. RCB cuts the other
// way here: the server owns the rows, and a read that did not happen is not a fact
// about where the reader is.
test('a failed artifact page read reports and leaves the reader where they were', async ({page}) => {
  await project(page, (s) => {});
  const items = Array.from({length: 15}, (_, i) => ({id: 'a' + i, path: 'f' + i + '.txt'}));
  const newestFirst = [...items].reverse();
  let dead = false;
  await page.route('**/api/v1/artifacts?*', (route) => {
    const q = new URL(route.request().url()).searchParams, cursor = q.get('cursor');
    if (cursor && dead) return route.fulfill({status: 404, json: {code: 'not_found'}});
    const start = cursor ? newestFirst.findIndex((a) => a.id === cursor) + 1: 0;
    const probe = newestFirst.slice(start, start + 11), rows = probe.slice(0, 10);
    return route.fulfill({json: {items: rows, next_cursor: probe.length > 10 && rows.length ? rows[rows.length - 1].id: null}});
  });
  await login(page); await gotoWork(page);
  const pager = page.locator('#artifacts .artifact-pager');
  await expect(pager.locator('.pager-info')).toContainText('第 1 页');
  const firstIds = await page.locator('#artifacts .artifact-card').evaluateAll((cs) => cs.map((c) => c.getAttribute('data-artifact')));
  dead = true;
  await pager.getByRole('button', {name: '下一页 ›'}).click();
  await expect(page.locator('#notice .notice-msg')).toContainText('not_found');
  await expect(pager.locator('.pager-info')).toContainText('第 1 页');
  expect(await page.locator('#artifacts .artifact-card').evaluateAll((cs) => cs.map((c) => c.getAttribute('data-artifact')))).toEqual(firstIds);
  //...and forward paging still works once the server does.
  dead = false;
  await pager.getByRole('button', {name: '下一页 ›'}).click();
  await expect(pager.locator('.pager-info')).toContainText('第 2 页');
  await expect(page.locator('#artifacts >.panel')).toHaveCount(5);
});

test('warm palette and desktop scrollbars match ', async ({page}) => {
  await login(page);
  expect(await page.evaluate(() => ['--surface', '--raised', '--soft', '--ink', '--brand', '--muted', '--line'].map((n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()))).toEqual(['#faf7f1', '#fffdf9', '#f2ede4', '#2b2723', '#1f6f6b', '#8a8178', '#e9e3da']);
  expect(await page.locator('#messages').evaluate((e) => getComputedStyle(e).scrollbarWidth)).not.toBe('none');
  await gotoWork(page);
  expect(await page.locator('#work-view').evaluate((e) => getComputedStyle(e).scrollbarWidth)).not.toBe('none');
  await page.locator('[data-view="hosts"]').click();
  expect(await page.locator('#hosts-view').evaluate((e) => getComputedStyle(e).scrollbarWidth)).not.toBe('none');
});

test('only an awaiting approval card joins the chat timeline, at its created-time position', async ({page}) => {
  await project(page, (s) => {
    s.work = [work('timeline')];
    s.tasks = [{id: 'historical', work_id: 'timeline', created: 200, approved_at: 240, status: 'succeeded', prompt: '旧批准'}, {id: 'pending', work_id: 'timeline', created: 250, status: 'awaiting_approval', prompt: '待批准'}];
  }, [msg('later', 300, '后来的消息'), msg('earlier', 100, '更早的消息')]);
  await login(page);
  // An already-approved task is a record of the task, not a conversation entry.
  expect(await page.locator('#messages [data-timeline]').evaluateAll((es) => es.map((e) => e.dataset.timeline))).toEqual(['message-earlier', 'approval-pending', 'message-later']);
  await expect(page.locator('.tcard')).toHaveCount(1);
  await expect(page.getByTestId('chat-log')).not.toContainText('旧批准');
  await expect(page.locator('.source time')).toHaveCount(2);
  await expect(page.locator('.source time').first()).toHaveText(/\d{1,2}-\d{1,2} \d{2}:\d{2}/);
  await expect(page.locator('.source time').first()).toHaveAttribute('datetime', '1970-01-01T00:01:40.000Z');
  await expect(page.locator('.tcard.awaiting')).toContainText('需你确认');
  await expect(page.locator('.tcard.awaiting time')).toHaveText(/\d{1,2}-\d{1,2} \d{2}:\d{2}/);
  await page.locator('[data-timeline="approval-pending"]').evaluate(e => { window.timelineNode = e; });
  const refreshed = page.waitForResponse(r => r.url().endsWith('/snapshot'));
  await page.locator('#refresh').click(); await (await refreshed).finished();
  expect(await page.locator('[data-timeline="approval-pending"]').evaluate(e => e === window.timelineNode)).toBe(true);
});

test('structured handoff/result and peer exchange retain every fact and unknown fields', async ({page}) => {
  await project(page, (s) => { s.tasks = []; s.exchanges = [{id: 'ex', target_bot_id: 'bot-main', source_bot_id: 'bot-peer', status: 'pending'}]; }, [
    msg('h', 1, {target_bot_id: 'bot-main', goal: '完整目标', request: '完整要求', acceptance: ['验收'], constraints: ['约束事实'], facts: ['已知事实'], completed: ['已完成事实'], open_questions: ['待问事实'], mode: 'deliver_to_user'}, {kind: 'handoff'}),
    msg('r', 2, {summary: '结果摘要', execution_result: {exit_code: 0}, artifact_refs: ['产物引用'], outputs: ['输出'], evidence_refs: ['凭据'], error: '执行错误事实', source_work_id: 'source-work-proof', future_field: {retained: true}, constructor: '原型同名字段也是事实', verification_status: 'bot_report_not_independent_verification'}, {kind: 'result'}),
    msg('empty', 2, {summary: '只有摘要', outputs: [], evidence_refs: [], acceptance: []}, {kind: 'result'}),
    msg('objitem', 2, {summary: '对象条目', outputs: [{path: '/workspace/报告.md', bytes: 2048, sha256: 'deadbeef'}]}, {kind: 'result'}),
    msg('back', 2, {mode: 'return_to_sender'}, {kind: 'handoff'}),
    msg('p', 3, '跨 Bot 发出', {kind: 'peer_out', exchange_id: 'ex', to: 'bot-main'}),
    msg('q', 4, '跨 Bot 收到', {kind: 'peer_result', exchange_id: 'ex', from: 'bot-peer'}),
  ]);
  await login(page);
  for (const text of ['交接给', '完整目标', '完整要求', '验收标准', '验收', '结果摘要', '输出', '凭据', '执行错误事实', 'source-work-proof', '约束事实', '已知事实', '已完成事实', '待问事实', '交付给用户', '回交发起方', 'exit_code', '产物引用', 'future_field', 'retained', 'Bot 自述，未经独立核验', '已发送给 胖胖', '消息来自 bot-peer', '往来 · 等待回应']) await expect(page.locator('#messages')).toContainText(text);
  // A field whose name is an Object.prototype member is still a fact; `key in labels`
  // walked the prototype chain and dropped it from both the labelled list and 其他.
  await expect(page.locator('#messages')).toContainText('原型同名字段也是事实');
  // An empty array renders NOTHING — a heading over an empty list claims a fact the
  // payload does not carry.
  const empty = page.locator('.bubble.result', {hasText: '只有摘要'});
  for (const title of ['产出文件', '证据', '验收标准']) await expect(empty).not.toContainText(title);
  // An output/evidence ITEM keeps its one-line rendering instead of a JSON dump.
  const objitem = page.locator('.bubble.result', {hasText: '对象条目'});
  await expect(objitem.locator('li').first()).toHaveText('/workspace/报告.md');
  await expect(objitem.locator('.evidence-raw')).toHaveCount(0);
});

test('markdown tables and rules render safely with existing formatting intact', async ({page}) => {
  await project(page, (s) => { s.tasks = []; }, [msg('markdown', 1, '# 标题\n\n**粗体** *斜体* `代码`\n\n| 左列 | 右列 |\n| --- | ---: |\n| A | **B** |\n| 管\\|道 | 第二格 |\n\n---\n\n> 引用\n\n1. 有序\n\n- 无序\n\n```js\nalert(1)\n```\n\n[安全](https://example.com) [危险](javascript:alert(1)) <script>window.injected=1</script>')]);
  await login(page);
  await expect(page.locator('.md-table th')).toHaveCount(2); await expect(page.locator('.md-table td strong')).toHaveText('B');
  // An escaped pipe belongs to its cell, and splitting must not need a lookbehind
  // (esbuild lowers one to a new RegExp that throws on Safari < 16.4, blanking the app).
  await expect(page.locator('.md-table tbody tr').nth(1).locator('td').first()).toHaveText('管|道');
  await expect(page.locator('.md-table tbody tr').nth(1).locator('td')).toHaveCount(2);
  await expect(page.locator('#messages em')).toHaveText('斜体');
  await expect(page.locator('#messages p code')).toHaveText('代码');
  await expect(page.locator('.md-table th').nth(1)).toHaveCSS('text-align', 'right');
  await expect(page.locator('.md-table th').first()).toHaveCSS('text-align', 'left');
  await expect(page.locator('.md-hr')).toHaveCount(1); await expect(page.locator('.md-pre')).toContainText('alert(1)');
  await expect(page.locator('#messages h2')).toHaveText('标题');
  await expect(page.locator('#messages blockquote')).toContainText('引用');
  await expect(page.locator('#messages ol')).toContainText('有序'); await expect(page.locator('#messages ul')).toContainText('无序');
  await expect(page.locator('#messages a[href^="javascript:"]')).toHaveCount(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
});

test('workspace browser restores width, scroll area, sizes, truncation and friendly errors', async ({page}) => {
  let fail = false;
  await page.route('**/api/v1/hosts/*/browse', (r) => r.fulfill({status: fail ? 400: 200, json: fail ? {code: 'host_no_workspace'}: {path: '/workspace', parent: '', truncated: true, entries: Array.from({length: 31}, (_, i) => ({name: '文件' + i + '.txt', type: 'file', size: 2048}))}}));
  await login(page); await page.locator('#attach').click();
  await expect(page.locator('.browse-item')).toHaveCount(31);
  expect((await page.locator('#file-dialog').boundingBox()).width).toBeCloseTo(720, 0);
  const box = await page.locator('#file-browser').evaluate((e) => ({height: e.clientHeight, scroll: e.scrollHeight, width: e.clientWidth}));
  expect(box.height).toBeGreaterThan(300); expect(box.scroll).toBeGreaterThan(box.height);
  await expect(page.locator('.browse-size').first()).toHaveText('2.0 KB');
  const positions = await page.locator('.browse-item').first().evaluate((e) => ({name: e.querySelector('.browse-name').getBoundingClientRect().right, size: e.querySelector('.browse-size').getBoundingClientRect().left}));
  expect(positions.size).toBeGreaterThan(positions.name);
  await expect(page.locator('#file-browser')).toContainText('只显示前 500 项');
  await expect(page.locator('#file-dialog')).toContainText('引用不会修改原稿');
  await page.locator('.browse-item').first().click(); await expect(page.locator('.browse-item').first()).toHaveClass(/picked/);
  await page.keyboard.press('Escape'); await expect(page.locator('#file-dialog')).toHaveCount(0);
  await page.locator('#attach').click(); await page.mouse.click(1, 1); await expect(page.locator('#file-dialog')).toHaveCount(0);
  fail = true; await page.locator('#attach').click();
  await expect(page.locator('#file-dialog')).toContainText('未配置执行器工作目录');
});

test('paste upload fences the whole queue including Enter and retains a thumbnail', async ({page}) => {
  const release = []; const held = [0, 1].map((i) => new Promise((r) => { release[i] = r; })); let posts = 0, uploaded = 0;
  await page.route('**/api/v1/bots/*/uploads', async (r) => { const i = uploaded++; await held[i]; return r.fulfill({json: {id: 'uploaded-' + i, name: i ? '第二.png': '粘贴.png'}}); });
  page.on('request', (r) => { if (r.method() === 'POST' && r.url().endsWith('/messages')) posts++; });
  await login(page); await page.locator('#message').fill('带图片发送');
  await page.locator('#message').evaluate((e, b64) => { const data = new DataTransfer(); const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)); data.items.add(new File([bytes], '粘贴.png', {type: 'image/png'})); data.items.add(new File([bytes], '第二.png', {type: 'image/png'})); e.dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: data})); }, PNG.toString('base64'));
  await expect(page.locator('#attached')).toContainText('粘贴.png（上传中…）'); await expect(page.locator('#send')).toBeDisabled();
  await page.locator('#message').press('Enter'); expect(posts).toBe(0);
  release[0](); await expect.poll(() => uploaded).toBe(2);
  await expect(page.locator('#attached')).toContainText('第二.png（上传中…）'); await expect(page.locator('#send')).toBeDisabled();
  release[1](); await expect(page.locator('#attached')).not.toContainText('上传中'); await expect(page.locator('#send')).toBeEnabled();
  await expect(page.locator('.chip-thumb')).toHaveCount(2);
});

test('drag/drop highlights composer and a failed upload clears its fence', async ({page}) => {
  let release; const held = new Promise((r) => { release = r; });
  await page.route('**/api/v1/bots/*/uploads', async (r) => { await held; return r.fulfill({status: 400, json: {code: 'invalid_upload'}}); });
  await login(page);
  await page.locator('#composer').evaluate((e) => { const d = new DataTransfer(); d.items.add(new File(['a'], '拖入.txt')); e.dispatchEvent(new DragEvent('dragover', {bubbles: true, cancelable: true, dataTransfer: d})); });
  await expect(page.locator('#composer')).toHaveClass(/dropping/);
  await page.locator('#composer').evaluate((e) => { const d = new DataTransfer(); d.items.add(new File(['a'], '拖入.txt')); e.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: d})); });
  await expect(page.locator('#composer')).not.toHaveClass(/dropping/); await expect(page.locator('#attached')).toContainText('上传中');
  release(); await expect(page.locator('#notice')).toContainText('文件内容无效'); await expect(page.locator('#attached > *')).toHaveCount(0); await expect(page.locator('#send')).toBeEnabled();
});

test('artifacts and chat share image/video/audio previews, downloads and size/error fallback', async ({page}) => {
  const arts = [{id: 'image', path: '图片.png', size: 80}, {id: 'video', path: '视频.webm', size: 80}, {id: 'audio', path: '音频.wav', size: 80}, {id: 'big', path: '巨大.png', size: 9 * 1024 * 1024}, {id: 'bad', path: '失败.png', size: 80}, {id: 'doc', path: '文档.txt', size: 80}];
  const requests = [];
  await page.route('**/api/v1/artifacts/*?download=1', async (r) => { const id = r.request().url().split('/').pop().split('?')[0]; requests.push(id); return id === 'video' ? r.fulfill({contentType: 'video/webm', body: WEBM}): id === 'audio' ? r.fulfill({contentType: 'audio/wav', body: WAV}): id === 'image' ? r.fulfill({contentType: 'image/png', body: PNG}): id === 'bad' ? r.fulfill({status: 404}): r.fulfill({contentType: 'application/octet-stream', body: ''}); });
  await project(page, (s) => { s.artifacts = arts; s.tasks = []; }, [msg('media', 1, '媒体消息', {artifact_refs: arts.map((a) => a.id)})]);
  await routeArtifacts(page, arts); // chat resolves artifact_refs from /snapshot; the #artifacts cards come from the keyset endpoint
  await login(page);
  for (const section of ['#messages', '#artifacts']) {
    if (section === '#artifacts') await gotoWork(page);
    await expect(page.locator(section + ' .media-img')).toHaveCount(1);
    await expect(page.locator(section)).toContainText('文件较大，请下载查看'); await expect(page.locator(section)).toContainText('预览加载失败');
    await expect(page.locator(section + ' a[download]')).toHaveCount(6);
    await expect(page.locator(section + ' video[controls]')).toHaveCount(1);
    await expect(page.locator(section + ' audio[controls]')).toHaveCount(1);
    for (const tag of ['video', 'audio']) await expect.poll(() => page.locator(section + ' ' + tag).evaluate((m) => m.readyState)).toBeGreaterThanOrEqual(1);
    await page.locator(section + ' .image-zoom').click(); await expect(page.locator('#lightbox')).toBeVisible();
    await page.keyboard.press('Escape'); await expect(page.locator('#lightbox')).toHaveCount(0);
  }
  expect(requests).not.toContain('big'); expect(requests).not.toContain('doc');
});

test('work/project/checkpoint/exchange and task evidence remain inspectable', async ({page}) => {
  await project(page, (s) => {
    s.work = [work('detail', {project_id: 'p', goal: '完整工作要求', checkpoint: {phase: '研究', summary: '摘要', completed: ['完成项目'], next_actions: ['下一步行动'], blocked_on: ['等待依赖']}})];
    s.projects = [{id: 'p', title: '项目', goal: '项目目标', owner_bot_id: 'bot-main'}];
    s.exchanges = [{id: 'ex', source_work_id: 'detail', source_bot_id: 'bot-main', target_bot_id: 'peer', status: 'pending', request: '消息历史'}];
    s.tasks = [{id: 'task-evidence', status: 'unknown', created: 100, prompt: '证据任务', work_id: 'detail', route: {chosen: 'codex'}, inputs: ['输入'], outputs: ['输出'], receipt: {pid: 123}, error: '证据错误'}];
  });
  await login(page); await gotoWork(page);
  for (const text of ['项目目标', '1 项工作', '1 项仍需关注']) await expect(page.locator('#projects-grid')).toContainText(text);
  for (const text of ['创建于', '研究', '摘要', '完成项目', '下一步行动', '等待依赖', '推进中']) await expect(page.locator('#work-grid')).toContainText(text);
  await page.getByRole('button', {name: '查看详情', exact: true}).click();
  for (const text of ['完整工作要求', 'Bot 间消息', '消息历史', '胖胖 → peer']) await expect(page.locator('#work-detail-dialog')).toContainText(text);
  await page.locator('#work-detail-dialog summary').click();
  await expect(page.locator('#work-detail-dialog details')).toContainText('source_work_id');
  await expect(page.locator('#work-detail-dialog details')).toContainText('detail');
  await page.locator('#work-detail-close').click();
  await page.locator('#tasks summary').click();
  for (const text of ['chosen', 'codex', '输入', '输出', 'pid', '123', '证据错误', '同目录的后续任务会排队']) await expect(page.locator('#tasks')).toContainText(text);
  for (const area of ['#tasks', '#recovery']) for (const name of ['确认从未启动', '确认已停止']) await expect(page.locator(area).getByRole('button', {name})).toBeVisible();
});

test('routine schedule, authority restrictions and derived Work truth are visible', async ({page}) => {
  await project(page, (s) => {
    s.routines = ['daily', 'weekly', 'interval', 'one-shot'].map((type, i) => ({id: 'r' + i, instruction: type, approval_state: i === 0 ? 'pending': 'approved', active: i < 2, schedule: {type, time: '04:00', weekdays: ['mon', 'fri'], minutes: 30, at: '2026-10-05T04:00:00'}, timezone: 'Asia/Shanghai', next_run_at: 100, last_run_at: 90, owner_policy: {name: '例行 Bot', role: '研究岗位', instructions: '长期研究指令'}, authority: {grant_write: false, required_host_id: 'host-only', required_installation_id: 'cli-only'}}));
    s.work = [work('routine-work', {routine_id: 'r0', attention: 'blocked'})];
  });
  await login(page); await gotoWork(page);
  for (const text of ['每天 04:00', '每周 mon,fri 04:00', '每 30 分钟', '一次 · 2026-10-05', 'Asia/Shanghai', '下次', '上次', '待批准', '待触发', '已暂停', '已触发', 'host-only', 'cli-only', '研究岗位', '长期研究指令', '受阻']) await expect(page.locator('#routines')).toContainText(text);
  await expect(page.locator('#routines input[type="checkbox"]')).not.toBeChecked();
  await expect(page.locator('#routines')).not.toContainText('只读');
  await expect(page.locator('#routines')).toContainText('已派生工作：1 项（已加载）受阻');
  for (const timestamp of [100, 90]) {
    const value = await page.evaluate(n => new Date(n * 1000).toLocaleString('zh-CN', {hour12: false}), timestamp);
    await expect(page.locator('#routines')).toContainText(value);
  }
});

test('continue selects owning Bot before installing the exact one-shot target', async ({page}) => {
  await project(page, (s) => { s.work = [work('other-owner', {goal: '另一个 Bot 的工作', owner_bot_id: s.bots.find((b) => b.name === BOT.aux).id})]; });
  await login(page); await gotoWork(page);
  await page.locator('#work-grid').getByRole('button', {name: '打开 Bot', exact: true}).click();
  await expect(page.locator('#bot-name')).toHaveText(BOT.aux);
  await expect(page.locator('#continue-target')).toHaveCount(0);
  await selectBot(page, BOT.main); await gotoWork(page);
  await page.locator('#work-grid').getByRole('button', {name: '继续此 Work'}).click();
  await expect(page.locator('#bot-name')).toHaveText(BOT.aux);
  await expect(page.locator('#continue-target')).toContainText('下一条消息将继续此 Work：另一个 Bot 的工作');
  let sent;
  await page.route('**/api/v1/bots/*/messages', (r) => { if (r.request().method() !== 'POST') return r.continue(); sent = r.request().postDataJSON(); return r.fulfill({status: 503, json: {code: 'temporary'}}); });
  await page.locator('#message').fill('继续'); await page.locator('#send').click(); await expect(page.locator('#notice')).toContainText('temporary');
  expect(sent.continue_work_id).toBe('other-owner');
  await selectBot(page, BOT.main); await expect(page.locator('#continue-target')).toHaveCount(0);
});

test('setup proposal and device evidence restore cwd, auth, Qwen policy, version and seen time', async ({page}) => {
  await project(page, (s) => {
    s.setup.requests = [{id: 'setup-test', status: 'needs_approval', host_id: 'local', proposal: {action: 'configure', adapter: 'qwen-claude', cwd: '/allowed/project', auth: 'existing'}}];
    s.probes = s.installations.map((i) => ({id: i.id, available: true, version: 'CLI 1.2.3'}));
    s.hosts.push({id: 'presence', transport: 'connector', name: '在线设备'}); s.devices.push({id: 'presence', online: true, last_seen: 100});
  });
  await login(page);
  for (const text of ['/allowed/project', 'CLI 已有登录', '使用设备已登记的资源策略', 'API 地址只能由你确认']) await expect(page.locator('#setup-panel')).toContainText(text);
  await page.locator('[data-view="hosts"]').click(); await expect(page.locator('#hosts')).toContainText('CLI 1.2.3'); await expect(page.locator('#hosts')).toContainText('截至');
  const seen = await page.evaluate(() => new Date(100000).toLocaleString('zh-CN', {hour12: false}));
  await expect(page.locator('.device-status span[title]')).toHaveAttribute('title', seen);
});

test('live progress describes current durable activity and removes stale recovery', async ({page}) => {
  let mode = 'thinking';
  await project(page, (s) => {
    s.work = [work('current', {status: 'waiting', attention: 'working'})]; s.turns = []; s.tasks = []; s.exchanges = [];
    if (mode === 'thinking') s.turns = [{id: 'turn', bot_id: 'bot-main', work_id: 'current', status: 'running'}];
    if (mode === 'executor' || mode === 'recovery') s.tasks = [{id: 'recovery-task', status: 'running', work_id: 'current', installation_id: 'Codex'}];
    if (mode === 'recovery') s.turns = [{id: 'turn', bot_id: 'bot-main', work_id: 'current', status: 'blocked', recovery_dispatched: 'recovery-task'}];
    if (mode === 'delegate-child') s.work.push(work('child', {parent_work_id: 'current', owner_bot_id: 'peer', goal: '子 Bot 研究'}));
    if (mode === 'delegate') s.exchanges = [{id: 'ex', source_work_id: 'current', target_bot_id: 'peer', request: '研究', status: 'pending'}];
    if (mode === 'other-work') { s.work[0].status = 'delivered'; s.work[0].attention = 'settled'; s.work.push(work('background', {goal: '后台工作'})); }
    if (mode === 'settled') { s.work[0].status = 'delivered'; s.work[0].attention = 'settled'; s.turns = [{id: 'old', work_id: 'current', status: 'blocked', recovery_dispatched: 'old-task'}]; }
  }, [msg('user', 1, '当前问题', {from: 'user', kind: 'user', work_id: 'current'})]);
  await login(page); await expect(page.locator('#messages')).toContainText('正在思考');
  for (const [next, text] of [['waiting', '正在等待执行结果'], ['executor', '已派 Codex 执行'], ['delegate', '已交给 peer'], ['delegate-child', '子 Bot 研究'], ['recovery', '这个有点复杂']]) { mode = next; await page.locator('#refresh').click(); await expect(page.locator('#messages')).toContainText(text); }
  mode = 'settled'; await page.locator('#refresh').click(); await expect(page.locator('.thinking')).toHaveCount(0);
  mode = 'other-work'; await page.locator('#refresh').click(); await expect(page.locator('.thinking')).toContainText('后台工作');
  await expect(page.locator('.thinking')).toContainText('正在等待执行结果');
  await expect(page.locator('#work-inspector')).not.toContainText('后台工作');
});

test('routing fallback and Work switch are reported from the send response', async ({page}) => {
  let result = {route_quality: 'fallback'};
  await login(page);
  await page.route('**/api/v1/bots/*/messages', (r) => r.request().method() === 'POST' ? r.fulfill({json: result}): r.continue());
  await page.locator('#message').fill('第一条'); await page.locator('#send').click(); await expect(page.locator('#notice')).toContainText('路由服务暂时降级'); await expect(page.locator('#send')).toBeEnabled();
  result = {work_switched: true}; await page.locator('#message').fill('第二条'); await page.locator('#send').click(); await expect(page.locator('#notice')).toContainText('已切换到新的 Work 上下文');
});

// A user can write the next message while the previous POST is still in flight.
test('send completion preserves a newer draft and newly added attachments', async ({page}) => {
  let release; const held = new Promise((r) => { release = r; }); let body;
  await login(page);
  await page.route('**/api/v1/bots/*/messages', async (r) => {
    if (r.request().method() !== 'POST') return r.continue();
    body = r.request().postDataJSON(); await held; return r.fulfill({json: {}});
  });
  await page.route('**/api/v1/bots/*/uploads', (r) => r.fulfill({json: {id: 'new-file', name: '后加.txt'}}));
  await page.locator('#message').fill('前一条'); await page.locator('#send').click();
  await expect.poll(() => body?.text).toBe('前一条');
  await page.locator('#message').fill('后一条草稿');
  await page.locator('#file-pick').setInputFiles({name: '后加.txt', mimeType: 'text/plain', buffer: Buffer.from('next')});
  await expect(page.locator('#attached')).toContainText('后加.txt');
  release(); await expect(page.locator('#send')).toBeEnabled();
  await expect(page.locator('#message')).toHaveValue('后一条草稿');
  await expect(page.locator('#attached')).toContainText('后加.txt');
  expect(body.refs).not.toContain('new-file');
});
