// Layer B (DOM-agnostic) unit contract: pagination clamp + slice bounds, exercised on
// the REAL production helper (web-src/pure.ts pageWindow) that usePager() consumes — no
// bundle slicing, no second implementation. Node strips TS types natively.
//
// NOTE: the former effectiveWorkStatus() contract was intentionally REMOVED here. 
// moved attention/status truth to the server projection (work.attention +
// attention_summary); the React shell MUST consume that and MUST NOT re-derive status
// client-side. That invariant is now locked by tests/test_web_v2_attention_guard.mjs
// (structural: forbids effectiveWorkStatus in web-src) and tests/browser/
// studio-attention.spec.mjs (real browser). Porting the old client derivation back
// would violate, so it is retired, not migrated — this is an architecture change,
// not a drop in coverage.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {pageWindow, PER_PAGE} from '../web-src/pure.ts';

test('pageWindow clamps the requested page into range and yields the right slice bounds', () => {
  assert.equal(PER_PAGE, 10);

  // 13 items @10/page -> 2 pages (indices 0..1).
  let w = pageWindow(13, 99); // out-of-range high
  assert.equal(w.pages, 2);
  assert.equal(w.pg, 1, 'clamped to last page index');
  assert.deepEqual([w.start, w.end], [10, 20], 'last-page window covers the remaining 3');

  w = pageWindow(13, -5); // out-of-range low
  assert.equal(w.pg, 0, 'clamped to first page index');
  assert.deepEqual([w.start, w.end], [0, 10], 'first page holds 10');

  // Empty list still yields a valid single page.
  w = pageWindow(0, 0);
  assert.equal(w.pages, 1);
  assert.equal(w.pg, 0);

  // Exact multiple: 20 items -> 2 full pages, no empty trailing page.
  w = pageWindow(20, 5);
  assert.equal(w.pages, 2);
  assert.equal(w.pg, 1);
});
