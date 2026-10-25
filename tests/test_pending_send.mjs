// Local send bubble: it stays until a new durable user message carries the same
// text, and a failed send puts the draft back only when the field is still empty.
import assert from 'node:assert/strict';
import test from 'node:test';
import {reconcilePendingSends, restoreComposerDraft} from '../web-src/pure.ts';

const pending = (id, text, created, knownIds) => ({id, botId: 'bot-main', text, created, knownIds});
const user = (id, content, created) => ({id, from: 'user', kind: 'user', content, created});

test('a pending bubble stays while the send has not landed', () => {
  const open = [pending('p1', '是的', 100, ['old'])];
  const messages = [user('old', '是的', 90)];
  assert.deepEqual(reconcilePendingSends(open, messages), open);
});

test('a new durable user message with the same text retires the oldest pending only', () => {
  const first = pending('p1', '是的', 100, ['old']);
  const second = pending('p2', '是的', 101, ['old']);
  const messages = [user('old', '是的', 90), user('new', ' 是的 ', 102)];
  assert.deepEqual(reconcilePendingSends([second, first], messages).map((p) => p.id), ['p2']);
  const both = [user('old', '是的', 90), user('a', '是的', 102), user('b', '是的', 103)];
  assert.deepEqual(reconcilePendingSends([first, second], both), []);
});

test('a non-user row and a different text do not retire the bubble', () => {
  const open = [pending('p1', '是的', 100, [])];
  const messages = [
    {id: 'a', from: 'bot-main', kind: 'assistant', content: '是的', created: 101},
    user('b', '不是', 101),
  ];
  assert.deepEqual(reconcilePendingSends(open, messages), open);
});

test('an older message is not a match when it was already on screen', () => {
  const open = [pending('p1', 'hello', 200, ['m1'])];
  assert.deepEqual(reconcilePendingSends(open, [user('m1', 'hello', 190)]), open);
});

test('clock skew of a few seconds still matches, a much older row does not', () => {
  const open = [pending('p1', 'hello', 100, [])];
  assert.deepEqual(reconcilePendingSends(open, [user('old', 'hello', 50)]), open);
  assert.deepEqual(reconcilePendingSends(open, [user('skew', 'hello', 80)]), []);
});

test('a failed send restores the draft only when the field was left empty', () => {
  const sent = {text: '前一条', attached: [{id: 'a'}, {id: 'b'}]};
  assert.deepEqual(restoreComposerDraft({text: '', attached: []}, sent), sent);
  assert.deepEqual(
    restoreComposerDraft({text: '后一条草稿', attached: [{id: 'c'}]}, sent),
    {text: '后一条草稿', attached: [{id: 'a'}, {id: 'b'}, {id: 'c'}]},
  );
  assert.deepEqual(
    restoreComposerDraft({text: '', attached: [{id: 'b'}, {id: 'c'}]}, sent),
    {text: '前一条', attached: [{id: 'a'}, {id: 'b'}, {id: 'c'}]},
  );
});
