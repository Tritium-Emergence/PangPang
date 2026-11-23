// Confirmation cards: an expired click shows a sentence, not a raw code,
// and the proposal is not dumped as JSON.
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import {confirmationFaultText, setupClickExpired, visibleSetupCards} from '../web-src/pure.ts';
import zhCN from '../web-src/i18n/zh-CN.ts';
import enUS from '../web-src/i18n/en-US.ts';

const setup = fs.readFileSync(new URL('../web-src/components/Setup.tsx', import.meta.url), 'utf8');
const code = 'setup_request_expired_or_used';

test('an expired confirmation is one sentence and the raw code stays off the card', () => {
  for (const dict of [zhCN, enUS]) {
    const sentence = dict.errors[code];
    assert.equal(typeof sentence, 'string');
    assert.equal(sentence.includes(code), false);
    assert.equal(confirmationFaultText(code, sentence), sentence);
    assert.equal(confirmationFaultText(code + ': leftover', sentence), sentence);
  }
  assert.equal(confirmationFaultText('cli_not_installed', zhCN.errors[code]), null);
  assert.equal(confirmationFaultText(code, code), null);
  assert.equal(confirmationFaultText(code, 'failed (' + code + ')'), null);
});

test('the pending panel keeps an expired card until deny, then drops it', () => {
  const expired = {id: 'old', status: 'needs_approval', expires: 1};
  const waiting = {id: 'live', status: 'needs_approval'};
  const secret = {id: 'key', status: 'needs_secret', expires: 1};
  const denied = {id: 'old', status: 'denied', expires: 1};
  assert.deepEqual(visibleSetupCards([expired, waiting, secret, denied]).map((row) => row.id),
    ['old', 'live', 'key']);
  assert.equal(setupClickExpired(expired.expires, 10), true);
  assert.equal(setupClickExpired(undefined, 10), false);
  assert.equal(setupClickExpired(10, 10), false);
  assert.match(setup, /visibleSetupCards/);
  assert.doesNotMatch(setup, /JSON\.stringify/);
});
