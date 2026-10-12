import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';

const read = (p) => fs.readFileSync(p, 'utf8');
const api = read('web-src/api.ts');
const setup = read('web-src/components/Setup.tsx');
const zh = read('web-src/i18n/zh-CN.ts');
const en = read('web-src/i18n/en-US.ts');

// First-run model setup is where a wrong key/endpoint is decided. A raw code in the
// dialog ("model_auth_failed") is not actionable; friendlyError maps errors.<code>
// to a localized sentence with the code appended for inspectability.
test('an advised fault is shown as code and advice', () => {
  assert.ok(api.includes('${code}: ${advice}'), 'the client must show advice with the code');
});

test('model setup failures surface localized hints, not raw codes', () => {
  assert.ok(setup.includes('friendlyError(err)'), 'SettingsDialog must map setup errors through friendlyError');
});

test('setup probe error codes have i18n entries in both locales', () => {
  const codes = [
    'model_endpoint_and_name_required', 'choose_key_or_no_auth',
    'endpoint_requires_https_or_loopback', 'new_endpoint_requires_credential_choice',
    'model_endpoint_requires_https_or_loopback', 'model_endpoint_must_not_contain_credentials',
    'model_credential_missing', 'model_tool_call_unverified', 'model_tool_arguments_unverified',
    'model_vision_unverified', 'model_request_failed', 'model_rate_limited',
    'model_timeout', 'model_auth_failed',
  ];
  for (const c of codes) {
    assert.ok(zh.includes(c + ':'), 'zh-CN missing errors.' + c);
    assert.ok(en.includes(c + ':'), 'en-US missing errors.' + c);
  }
});
