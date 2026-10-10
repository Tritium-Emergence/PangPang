// correctness gate (structural guard). The React shell MUST render attention
// states from the server projection (work.attention + attention_summary, produced by
// pangpang/attention.py) and MUST NOT re-derive them client-side. This test fails if
// the React source ever reintroduces an effectiveWorkStatus-style precedence or
// computes Working/Blocked/Done from raw turns/tasks. It is a correctness gate, not a
// legacy-cleanup nicety (integration red line).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {test} from 'node:test';

function walk(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, {withFileTypes: true})) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(e.name)) out.push(p);
  }
  return out;
}

const files = walk('web-src');
// Strip // line comments and /* */ block comments so the guard checks real code only
// (a comment that merely mentions effectiveWorkStatus must not trip it).
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
const source = files.map((f) => strip(fs.readFileSync(f, 'utf8'))).join('\n');

test('React shell consumes server attention truth (does not re-derive it)', () => {
  // Positive: the shell actually reads the server projection.
  assert.match(source, /attention_summary/, 'shell must read attention_summary');
  assert.match(source, /\.attention\b/, 'shell must read work.attention');

  // Negative: no client-side attention precedence derivation (checked on comment-free
  // source).
  assert.doesNotMatch(source, /effectiveWorkStatus/, 'must not port effectiveWorkStatus()');
  assert.doesNotMatch(source, /function\s+\w*[Ss]tatus\s*\([^)]*turns/, 'must not derive status from turns in the client');
});

test('AttentionBar is a server-driven 待办 inbox (counts stay server-owned)', () => {
  const bar = strip(fs.readFileSync('web-src/components/AttentionBar.tsx', 'utf8'));
  // The inbox lists actionable items the server projected; it still reads the server
  // summary (for the "更多" overflow) and per-item attention/notification facts.
  assert.match(bar, /attention_summary/, 'inbox must read attention_summary (overflow count)');
  assert.match(bar, /\.attention\b/, 'inbox must read work.attention (server projection)');
  // It must not inspect raw turns/tasks to derive state (same red line).
  assert.doesNotMatch(bar, /\bturns\b|\btasks\b/, 'AttentionBar must not inspect turns/tasks');
});
