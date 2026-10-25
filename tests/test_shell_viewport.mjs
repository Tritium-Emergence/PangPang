// Chat shell vs visualViewport. A shorter viewport is the keyboard and pins the
// shell; a full-height pan is a stuck scroll and must be reset instead.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {shellFrameCss, viewportAction} from '../web-src/pure.ts';

test('an aligned viewport does not override the shell', () => {
  assert.deepEqual(viewportAction(null, {innerHeight: 800}), {type: 'none'});
  assert.deepEqual(viewportAction({height: 800, offsetTop: 0}, {innerHeight: 800}), {type: 'none'});
  assert.deepEqual(viewportAction({height: 801, offsetTop: 0}, {innerHeight: 800}), {type: 'none'});
  assert.deepEqual(viewportAction({height: NaN, offsetTop: 0}, {innerHeight: 800}), {type: 'none'});
  assert.deepEqual(viewportAction({height: 500, offsetTop: 0}, null), {type: 'none'});
});

test('a shorter visual viewport pins the shell inside the layout viewport', () => {
  assert.deepEqual(viewportAction({height: 500, offsetTop: 0}, {innerHeight: 800}), {type: 'pin', top: 0, height: 500});
  assert.deepEqual(viewportAction({height: 420.4, offsetTop: 118.2}, {innerHeight: 800}), {type: 'pin', top: 118, height: 420});
  assert.deepEqual(viewportAction({height: 500, offsetTop: -40}, {innerHeight: 800}), {type: 'pin', top: 0, height: 500});
  // A pan that would push the shell past the layout viewport is clamped.
  assert.deepEqual(viewportAction({height: 500, offsetTop: 700}, {innerHeight: 800}), {type: 'pin', top: 300, height: 500});
});

test('a full-height pan is a stuck scroll, not a pin', () => {
  assert.deepEqual(viewportAction({height: 800, offsetTop: 160}, {innerHeight: 800}), {type: 'reset'});
  assert.deepEqual(viewportAction({height: 799, offsetTop: 40}, {innerHeight: 800}), {type: 'reset'});
});

test('the pin rule is a fixed shell rectangle', () => {
  assert.equal(shellFrameCss({top: 12.6, height: 500.2}), '#shell{position:fixed;top:13px;left:0;right:0;height:500px}');
});

test('the document shell does not use a second 100dvh height', () => {
  const css = fs.readFileSync('web-src/styles.css', 'utf8');
  assert.match(css, /body\s*\{[^}]*position:\s*fixed/);
  assert.match(css, /html\s*\{[^}]*overflow:\s*hidden/);
  assert.match(css, /overscroll-behavior:\s*none/);
  assert.doesNotMatch(css, /\.shell-root\s*\{[^}]*height:\s*100dvh/);
});
