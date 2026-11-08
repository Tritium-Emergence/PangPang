// Repair #86: a device upgrade button is only offered for a device that reported
// its own install is a git checkout. A build/unknown install is not an upgradable
// checkout; an unreported source is not proven either, so the button stays hidden
// and the server preflight / connector adjudicates the unknown case.
import assert from 'node:assert/strict';
import test from 'node:test';
import {canUpgradeDevice, commitsDiffer} from '../web-src/pure.ts';
import zhCN from '../web-src/i18n/zh-CN.ts';
import enUS from '../web-src/i18n/en-US.ts';

const CLOUD = 'a'.repeat(40);
const ELSEWHERE = 'b'.repeat(40);
const host = {transport: 'connector'};

test('only a git source with a known cloud commit can be upgraded in place', () => {
  assert.equal(canUpgradeDevice(host, {online: true, source: 'git', commit: ELSEWHERE}, CLOUD), true);
  assert.equal(canUpgradeDevice(host, {online: true, source: 'git', commit: 'unknown'}, CLOUD), true);
  assert.equal(canUpgradeDevice(host, {online: true, source: 'git'}, CLOUD), true);
  // Already on the target commit: nothing to upgrade.
  assert.equal(canUpgradeDevice(host, {online: true, source: 'git', commit: CLOUD}, CLOUD), false);
  assert.equal(canUpgradeDevice(host, {online: true, source: 'git', commit: ELSEWHERE}, 'unknown'), false);
  assert.equal(canUpgradeDevice(host, {online: false, source: 'git', commit: ELSEWHERE}, CLOUD), false);
  assert.equal(canUpgradeDevice(host, {revoked: true, online: true, source: 'git', commit: ELSEWHERE}, CLOUD), false);
  assert.equal(canUpgradeDevice({transport: 'local'}, {online: true, source: 'git', commit: ELSEWHERE}, CLOUD), false);
});

test('build, unknown and unreported sources never render an upgrade button', () => {
  for (const source of ['build', 'unknown', undefined, '']) {
    assert.equal(
      canUpgradeDevice(host, {online: true, source, commit: ELSEWHERE}, CLOUD),
      false,
      `source=${String(source)} must not be upgradable`);
  }
});

test('unknown commits are not treated as a mismatch', () => {
  assert.equal(commitsDiffer(CLOUD, 'unknown'), false);
  assert.equal(commitsDiffer('unknown', ELSEWHERE), false);
  assert.equal(commitsDiffer(CLOUD, ELSEWHERE), true);
});

test('upgrade copy is present in both locales and never promises a queued result', () => {
  for (const dict of [zhCN, enUS]) {
    for (const key of ['upgradeUnavailable', 'upgradeSourceUnknown', 'upgradeUnconfirmed', 'upgradePending'])
      assert.equal(typeof dict.hosts[key], 'string', `hosts.${key} missing`);
    for (const code of ['install_not_git_checkout', 'upgrade_blocked_active_runs', 'git_fetch_failed',
      'target_commit_unavailable', 'target_commit_not_on_tracking_ref', 'git_reset_failed'])
      assert.equal(typeof dict.errors[code], 'string', `errors.${code} missing`);
    assert.equal('upgradeQueued' in dict.hosts, false, 'queued-is-terminal copy must be gone');
  }
});
