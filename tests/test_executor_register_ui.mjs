// Computers page: one confirmation registers a detected catalog executor.
// Cancel does not post. A different-scope card stays a card.
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import zhCN from '../web-src/i18n/zh-CN.ts';
import enUS from '../web-src/i18n/en-US.ts';

const hosts = readFileSync(new URL('../web-src/components/HostsView.tsx', import.meta.url), 'utf8');

test('both locales disclose the one registration and that a run is still separate', () => {
  for (const dict of [zhCN, enUS]) {
    for (const key of ['registerConfirm', 'registerStarted', 'registerAlready', 'registerWorking', 'registerPending'])
      assert.equal(typeof dict.hosts[key], 'string', `hosts.${key}`);
    assert.equal(typeof dict.errors.setup_scope_pending, 'string');
  }
  assert.match(zhCN.hosts.registerConfirm, /取消则不会登记/);
  assert.match(zhCN.hosts.registerConfirm, /不会允许它执行任务/);
  assert.match(zhCN.hosts.registerStarted, /第一次运行仍需你批准/);
  assert.match(enUS.hosts.registerConfirm, /Cancel registers nothing/);
  assert.match(enUS.hosts.registerConfirm, /does not authorize task runs/);
  assert.match(enUS.hosts.connectWorkspaceDesc, /one confirmation/);
});

test('the register button confirms once, posts the default scope, and does not approve a second card', () => {
  assert.match(hosts, /hosts\.registerConfirm/);
  assert.match(hosts, /if \(!confirm\(t\('hosts\.registerConfirm'/);
  assert.match(hosts, /action: 'configure', adapter/);
  assert.equal(hosts.includes("action: 'configure', adapter: String(x.adapter), cwd"), false);
  assert.equal(hosts.includes('/approve'), false);
  assert.match(hosts, /status === 'needs_approval'/);
  assert.match(hosts, /status === 'configuring'/);
  assert.match(hosts, /hosts\.registerWorking/);
  assert.match(hosts, /friendlyError\(err\)/);
});
