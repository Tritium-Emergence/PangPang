import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';

const read = (p) => fs.readFileSync(p, 'utf8');
const work = read('web-src/components/WorkPane.tsx');
const hosts = read('web-src/components/HostsView.tsx');
const setup = read('web-src/components/Setup.tsx');
const side = read('web-src/components/Sidebar.tsx');
const checklistPath = 'web-src/CUTOVER-CHECKLIST.md';
const checklist = fs.existsSync(checklistPath) ? read(checklistPath): null;

test('cutover control-plane entrypoints are all wired in React', () => {
  for (const p of [
    'routines/','/run',"'pause'","'resume'",
    'tasks/','/retry','/reconcile','/resolve',
    'claims/','/resolve','artifacts/','/copy','projects',
  ]) assert.ok(work.includes(p), 'WorkPane missing '+p);
  for (const p of ['installations/','/config','devices/','/revoke']) assert.ok(hosts.includes(p), 'HostsView missing '+p);
  assert.ok(setup.includes('setup/model-key/revoke'), 'Settings missing model key revoke');
  for (const p of ['bots/','/update','/archive']) assert.ok(side.includes(p), 'Sidebar missing '+p);
});

test('cutover checklist has no deferred product controls', (t) => {
  if (checklist === null) return t.skip('cutover checklist is private-only');
  assert.doesNotMatch(checklist, /⏳|deferred — MUST wire/i);
});

// The committed bundle must not carry regex syntax newer than the build target.
// Vite's default target includes safari14, so esbuild silently lowers a lookbehind or
// a named capture group from a LITERAL to new RegExp("(?<...") — which parses fine and
// then throws SyntaxError when constructed on Safari < 16.4. There is no error boundary
// in this app, so such a throw during render unmounts the whole React root: a blank
// page, not a degraded widget. esbuild emits no warning, and a modern Linux WebKit
// cannot reproduce it, so only this byte-level guard catches the class.
test('committed bundle carries no post-target regex syntax', () => {
  const bundle = read('pangpang/web/app.js');
  assert.doesNotMatch(bundle, /\(\?</, 'app.js contains (?< — a lookbehind or named group that throws on Safari < 16.4');
});
