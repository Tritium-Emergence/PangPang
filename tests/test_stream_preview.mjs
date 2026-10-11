// Stream preview reducer used by the live message view.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {applyPreviewFrame, mergeRefreshScope} from '../web-src/pure.ts';

test('a suffix extends the preview and a gap or a shrink does not invent text', () => {
  let cur = applyPreviewFrame(undefined, {text: '你好', rev: 1});
  assert.deepEqual(cur, {text: '你好', rev: 1, done: false});
  cur = applyPreviewFrame(cur, {delta: '，世界', rev: 2, base_rev: 1});
  assert.equal(cur.text, '你好，世界');
  assert.equal(cur.rev, 2);
  // Stale rev is ignored.
  assert.equal(applyPreviewFrame(cur, {text: 'nope', rev: 2}), cur);
  // A suffix whose base the client does not hold is dropped.
  assert.equal(applyPreviewFrame(cur, {delta: '!', rev: 4, base_rev: 3}), cur);
  // A tool round restarts from a full frame, even when the text is shorter.
  cur = applyPreviewFrame(cur, {text: '好', rev: 5});
  assert.equal(cur.text, '好');
  cur = applyPreviewFrame(cur, {done: true});
  assert.equal(cur.done, true);
  assert.equal(cur.text, '好');
  // done with no preview does not create a phantom stream
  assert.equal(applyPreviewFrame(undefined, {done: true}), undefined);
});

test('only message.changed skips the dashboard snapshot', () => {
  assert.equal(mergeRefreshScope('messages', 'message.changed'), 'messages');
  assert.equal(mergeRefreshScope('all', 'message.changed'), 'all');
  for (const type of ['turn.changed', 'work.changed', 'reset', undefined, '']) {
    assert.equal(mergeRefreshScope('messages', type), 'all');
  }
});
