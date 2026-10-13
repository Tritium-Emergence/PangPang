// Setup must not call an admitted executor "ready" until execution_verified.
// The helper reads that stored field; it does not decide admission.
import assert from 'node:assert/strict';
import test from 'node:test';
import {executionFactsFor, executorSetupTone, installationSetupTone} from '../web-src/pure.ts';

const admitted = (verified, discovered = true) => ({
  capability: 'executor',
  state: 'ready',
  detail: {
    selected: {installation_id: 'codex-local'},
    executor_evidence: [{installation_id: 'codex-local', discovered, execution_verified: verified}],
  },
});

test('discovered without execution_verified is not the ready tone', () => {
  assert.equal(executorSetupTone(admitted(false)), 'discovered_not_verified');
  assert.equal(installationSetupTone({installation_id: 'codex-local', discovered: true, execution_verified: false}, {available: true}), 'discovered_not_verified');
});

test('ready only when execution_verified is true', () => {
  assert.equal(executorSetupTone(admitted(true)), 'ready');
  assert.equal(installationSetupTone({installation_id: 'codex-local', discovered: true, execution_verified: true}, {available: true}), 'ready');
});

test('a missing receipt on an admitted executor is not ready', () => {
  assert.equal(executorSetupTone({capability: 'executor', state: 'ready', detail: {selected: {installation_id: 'codex-local'}}}), 'discovered_not_verified');
  assert.equal(installationSetupTone(undefined, {available: true}), 'discovered_not_verified');
});

test('verified but not discovered is not the ready tone', () => {
  assert.equal(executorSetupTone(admitted(true, false)), 'discovered_not_verified');
  assert.equal(installationSetupTone(
    {installation_id: 'codex-local', discovered: false, execution_verified: true},
    {available: false},
  ), null);
  assert.equal(installationSetupTone(
    {installation_id: 'codex-local', discovered: false, execution_verified: true},
    undefined,
  ), null);
  // The card's probe row is the current check. Unavailable stays unavailable
  // even if a stored discovered flag and an old receipt are both true.
  assert.equal(installationSetupTone(
    {installation_id: 'codex-local', discovered: true, execution_verified: true},
    {available: false},
  ), null);
});

test('blocked and non-executor rows keep their own state label', () => {
  assert.equal(executorSetupTone({capability: 'executor', state: 'blocked', detail: admitted(false).detail}), null);
  assert.equal(executorSetupTone({capability: 'model', state: 'ready'}), null);
  assert.equal(installationSetupTone(undefined, undefined), null);
  assert.equal(installationSetupTone({discovered: false, execution_verified: false}, {available: false}), null);
});

test('facts are read from the executor capability evidence', () => {
  const rows = [admitted(false), {capability: 'model', state: 'ready'}];
  assert.equal(executionFactsFor(rows, 'codex-local').execution_verified, false);
  assert.equal(executionFactsFor(rows, 'missing'), undefined);
});
