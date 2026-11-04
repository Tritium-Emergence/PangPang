// Static guard: the chat never renders a payload as JSON. Executor results,
// handoff/result cards and the shared Value renderer must turn data into human
// lines; a JSON.stringify or an evidence-raw <pre> in these files is a regression.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';

const read = (p) => readFileSync(new URL(`../web-src/${p}`, import.meta.url), 'utf8')
  .replace(/^\s*\/\/.*$/gm, '');

test('chat message renderers never stringify a payload into the UI', () => {
  for (const f of ['components/MessageBubble.tsx', 'components/Facts.tsx']) {
    const src = read(f);
    assert.doesNotMatch(src, /JSON\.stringify/, `${f} renders JSON`);
    assert.doesNotMatch(src, /evidence-raw/, `${f} renders a raw evidence <pre>`);
  }
});

test('execution_result has its own human renderer, not the generic field bag', () => {
  const src = read('components/MessageBubble.tsx');
  assert.match(src, /kind === 'execution_result'[^\n]*<ExecutionResult/);
  for (const lang of ['zh-CN', 'en-US']) {
    const i18n = read(`i18n/${lang}.ts`);
    for (const code of ['required_output_missing', 'declared_output_missing', 'queue_timeout', 'missingOutputs'])
      assert.match(i18n, new RegExp(`${code}:`), `${lang} lacks ${code}`);
  }
});
