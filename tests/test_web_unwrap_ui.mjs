// Layer B (DOM-agnostic) unit contract: security- and RCB-sensitive PURE functions.
// These import the REAL production module (web-src/pure.ts) that the React shell also
// imports — no bundle slicing, no esbuild/vm, no second implementation. Node strips the
// TS types natively (baseline Node >=22.19), so the test exercises the exact shipped code.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {safeUrl, unwrapFrame, mediaKind} from '../web-src/pure.ts';

test('safeUrl allows http/https/relative/data-image and blocks script & other schemes (XSS)', () => {
  for (const u of ['http://x/y', 'https://x/y', '/rel/path', './rel', 'data:image/png;base64,AAA']) assert.equal(safeUrl(u), u, u);
  for (const u of ['javascript:alert(1)', ' javascript:alert(1)', 'data:text/html,<script>', 'vbscript:x', 'file:///etc/passwd']) assert.equal(safeUrl(u), '', u);
});

test('unwrapFrame unwraps an echoed context frame to its inner content, else returns input unchanged', () => {
  // A frame = {kind, content:string, and (evidence_ref|source)}: show only.content.
  assert.equal(unwrapFrame(JSON.stringify({kind: 'assistant', source: 'bot-x', content: '真正的答复', work_id: 'w1'})), '真正的答复');
  assert.equal(unwrapFrame(JSON.stringify({kind: 'assistant', evidence_ref: 'msg-1', content: '另一个答复'})), '另一个答复');
  assert.equal(unwrapFrame('普通的 markdown 回复'), '普通的 markdown 回复');
  assert.equal(unwrapFrame('{not valid json'), '{not valid json');
  const noKind = JSON.stringify({content: 'x'}); // missing kind + evidence/source -> not a frame
  assert.equal(unwrapFrame(noKind), noKind);
  assert.deepEqual(unwrapFrame({a: 1}), {a: 1}); // non-string passthrough
});

test('mediaKind classifies by file extension (case-insensitive)', () => {
  assert.equal(mediaKind('cover.PNG'), 'image');
  assert.equal(mediaKind('clip.mp4'), 'video');
  assert.equal(mediaKind('voice.mp3'), 'audio');
  assert.equal(mediaKind('季度报告.html'), 'other');
  assert.equal(mediaKind('noext'), 'other');
});
