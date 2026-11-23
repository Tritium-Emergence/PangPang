// Confirmation cards and the secret window: sentences only, and the
// proposal lines show the values that would actually be saved.
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import {landingFacts, setupCardSentence, setupFaultRefreshes, visibleSetupCards} from '../web-src/pure.ts';
import zhCN from '../web-src/i18n/zh-CN.ts';
import enUS from '../web-src/i18n/en-US.ts';

const setup = fs.readFileSync(new URL('../web-src/components/Setup.tsx', import.meta.url), 'utf8');
const display = fs.readFileSync(new URL('../web-src/display.ts', import.meta.url), 'utf8');

function known(dict) {
  return (code) => dict.errors[code] || '';
}

test('setup card and secret window show a sentence and never a raw fault', () => {
  const codes = ['setup_request_expired_or_used', 'setup_request_already_resolved', 'installation_config_changed', 'cli_not_installed'];
  for (const dict of [zhCN, enUS]) {
    const generic = dict.setup.cardRetry;
    assert.equal(generic.includes('setup_request'), false);
    for (const code of codes) {
      const sentence = setupCardSentence(code, known(dict), generic);
      assert.equal(sentence, dict.errors[code]);
      assert.equal(sentence.includes(code), false);
      const withAdvice = setupCardSentence(code + ': {"path":"secret"} leftover', known(dict), generic);
      assert.equal(withAdvice, dict.errors[code]);
    }
    const unknown = setupCardSentence('fixture_mystery_fault: {"raw":true}', known(dict), generic);
    assert.equal(unknown, generic);
    assert.equal(unknown.includes('fixture_mystery_fault'), false);
    assert.equal(unknown.includes('{'), false);
    const blob = setupCardSentence('{"code":"cli_not_installed"}', known(dict), generic);
    assert.equal(blob, generic);
  }
  assert.equal(setupFaultRefreshes('setup_request_already_resolved'), true);
  assert.equal(setupFaultRefreshes('setup_request_expired_or_used'), false);
  assert.equal(setupFaultRefreshes('cli_not_installed'), false);
  assert.match(setup, /setupCardFault/);
  assert.match(setup, /id="secret-error"/);
  assert.doesNotMatch(display, /setupCardFault[\s\S]*friendlyError/);
  assert.doesNotMatch(setup, /JSON\.stringify/);
});

test('a proposal shows env values, credential ids, and extra argv, including clears', () => {
  const home = landingFacts({env: {CODEX_HOME: '/profiles/a'}, secret_refs: {CODEX_API_KEY: 'cred-a'}, extra_argv: ['--strict']});
  const other = landingFacts({env: {CODEX_HOME: '/profiles/b'}, secret_refs: {CODEX_API_KEY: 'cred-b'}, extra_argv: ['--loose']});
  assert.deepEqual(home, [
    {kind: 'env', name: 'CODEX_HOME', value: '/profiles/a'},
    {kind: 'secret', name: 'CODEX_API_KEY', id: 'cred-a'},
    {kind: 'argv', args: ['--strict']},
  ]);
  assert.notDeepEqual(home, other);
  assert.equal(other[0].value, '/profiles/b');
  assert.equal(other[1].id, 'cred-b');
  assert.deepEqual(landingFacts({extra_env: {OPENAI_BASE_URL: 'http://127.0.0.1:9/v1'}, secret_env: 'OPENAI_API_KEY'}), [
    {kind: 'env', name: 'OPENAI_BASE_URL', value: 'http://127.0.0.1:9/v1'},
    {kind: 'secret-env', name: 'OPENAI_API_KEY'},
  ]);
  assert.deepEqual(landingFacts({env: {}, secret_refs: {}, extra_argv: []}), [
    {kind: 'env-clear'}, {kind: 'secrets-clear'}, {kind: 'argv-clear'},
  ]);
  assert.deepEqual(landingFacts({cwd: '/work', profile: 'notes'}), []);
  assert.deepEqual(landingFacts({env: {NESTED: {no: 'json'}}}), []);
  assert.equal(JSON.stringify(home).includes('{"env"'), false);
});

test('the pending panel keeps an expired card until deny, then drops it', () => {
  const expired = {id: 'old', status: 'needs_approval', expires: 1};
  const waiting = {id: 'live', status: 'needs_approval'};
  const secret = {id: 'key', status: 'needs_secret', expires: 1};
  const denied = {id: 'old', status: 'denied', expires: 1};
  assert.deepEqual(visibleSetupCards([expired, waiting, secret, denied]).map((row) => row.id),
    ['old', 'live', 'key']);
});
