// Deterministic unit contract for durable send idempotency (web-src/idempotency.ts),
// run under node:test — no browser, no server, no race. Proves the exact
// mechanism: the SAME stable intent reuses the persisted per-bot key across a retry; a
// DIFFERENT intent mints a new key; the slot is per-bot; clearPending retires it.
import assert from 'node:assert/strict';
import test from 'node:test';
import {webcrypto} from 'node:crypto';
import fs from 'node:fs';
import vm from 'node:vm';
import {transformSync} from 'esbuild';

// Transpile the real TS module (esbuild is already a vite dependency) and evaluate it in
// a sandbox, so the test exercises the SHIPPED logic, not a copy.
const js = transformSync(fs.readFileSync('web-src/idempotency.ts', 'utf8'),
  {loader: 'ts', format: 'cjs'}).code;
const module = {exports: {}};
const ctx = vm.createContext({module, exports: module.exports, TextEncoder, crypto: webcrypto, JSON, Array, Uint8Array});
vm.runInContext(js, ctx);
const {sendKeyFor, clearPending, pendingKey} = module.exports;

function kv() {
  const m = new Map();
  return {getItem: (k) => (m.has(k) ? m.get(k): null), setItem: (k, v) => m.set(k, v), removeItem: (k) => m.delete(k)};
}
let n = 0;
const uuid = () => 'uuid-' + (++n);

test('same stable intent reuses the persisted per-bot key (retry of an unconfirmed send)', async () => {
  const store = kv();
  const intent = {text: '同一条', refs: [], continueWorkId: null};
  const k1 = await sendKeyFor('bot-main', intent, store, webcrypto.subtle, uuid);
  const k2 = await sendKeyFor('bot-main', intent, store, webcrypto.subtle, uuid); // retry, slot persisted
  assert.equal(k2, k1, 'retry must reuse the key');
});

test('a move target is part of the stable intent (retry keeps it; dropping it mints a new key)', async () => {
  const store = kv();
  const moved = {text: '这条属于那件', refs: [], continueWorkId: 'work-b', correctToWorkId: 'work-b'};
  const k1 = await sendKeyFor('bot-main', moved, store, webcrypto.subtle, uuid);
  const k2 = await sendKeyFor('bot-main', moved, store, webcrypto.subtle, uuid);
  assert.equal(k2, k1, 'retry of the same move must reuse the key');
  const dropped = {text: '这条属于那件', refs: [], continueWorkId: 'work-b', correctToWorkId: null};
  const k3 = await sendKeyFor('bot-main', dropped, store, webcrypto.subtle, uuid);
  assert.notEqual(k3, k1, 'dropping the move is a different send');
});

test('a different intent mints a NEW key', async () => {
  const store = kv();
  const a = await sendKeyFor('bot-main', {text: 'A', refs: [], continueWorkId: null}, store, webcrypto.subtle, uuid);
  const b = await sendKeyFor('bot-main', {text: 'B', refs: [], continueWorkId: null}, store, webcrypto.subtle, uuid);
  assert.notEqual(b, a);
});

test('the pending slot is per-bot (two bots never overwrite each other)', async () => {
  const store = kv();
  const intent = {text: 'x', refs: [], continueWorkId: null};
  const main = await sendKeyFor('bot-main', intent, store, webcrypto.subtle, uuid);
  const aux = await sendKeyFor('bot-aux', intent, store, webcrypto.subtle, uuid);
  assert.notEqual(aux, main);
  const mainRetry = await sendKeyFor('bot-main', intent, store, webcrypto.subtle, uuid);
  assert.equal(mainRetry, main); // bot-main slot untouched by bot-aux
  assert.ok(store.getItem(pendingKey('bot-main')));
  assert.ok(store.getItem(pendingKey('bot-aux')));
});

test('clearPending retires the key so the next same-intent send mints fresh', async () => {
  const store = kv();
  const intent = {text: 'y', refs: [], continueWorkId: null};
  const k1 = await sendKeyFor('bot-main', intent, store, webcrypto.subtle, uuid);
  clearPending('bot-main', store);
  assert.equal(store.getItem(pendingKey('bot-main')), null);
  const k2 = await sendKeyFor('bot-main', intent, store, webcrypto.subtle, uuid);
  assert.notEqual(k2, k1, 'after clear, a confirmed send does not reuse the old key');
});
