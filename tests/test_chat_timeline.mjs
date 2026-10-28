// Main-chat timeline visibility. Imports the production helper in web-src/pure.ts
// that Chat.tsx uses, so a setup_result cannot render as a raw JSON bubble.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {visibleChatMessages} from '../web-src/pure.ts';

const discover = {
  id: 'setup-discover',
  kind: 'setup_result',
  from: 'device',
  content: {
    action: 'discover',
    host_id: 'host-1',
    inventory: [
      {adapter: 'claude-code', binary: '/usr/local/bin/claude', version: '2.1.252'},
      {adapter: 'sample-adapter', binary: '/home/example/.local/bin/sample-adapter', version: '1.0'},
    ],
  },
};

test('setup_result payloads are omitted from the chat timeline', () => {
  const user = {id: 'u1', kind: 'user', from: 'user', content: '先把执行器注册一下'};
  const declared = {
    id: 'setup-declare',
    kind: 'setup_result',
    from: 'device',
    content: {action: 'declare_adapter', host_id: 'host-1', declared_adapters: [{adapter: 'sample-adapter'}]},
  };
  const configured = {
    id: 'setup-configure',
    kind: 'setup_result',
    from: 'device',
    consumed_by_turn_id: 't1',
    content: {action: 'configure', host_id: 'host-1', accepted: true, installation_id: 'inst-1'},
  };
  const summary = {id: 'a1', kind: 'assistant', from: 'bot-main', turn_id: 't1', content: '探测到 claude-code，目前尚未注册。'};
  const visible = visibleChatMessages([user, discover, declared, configured, summary]);
  assert.deepEqual(visible.map((m) => m.id), ['u1', 'a1']);
  assert.equal(visible.some((m) => m.kind === 'setup_result' || m.from === 'device'), false);
  assert.equal(JSON.stringify(visible).includes('"action":"discover"'), false);
  assert.equal(JSON.stringify(visible).includes('/usr/local/bin/claude'), false);
  assert.equal(visible.at(-1).content, summary.content);
});

test('a setup_result is omitted before the steward has replied', () => {
  assert.deepEqual(visibleChatMessages([discover]), []);
});

test('folded execution_result rows stay hidden and a waiting one stays visible', () => {
  const interim = {id: 'a0', kind: 'assistant', turn_id: 't0', interim: true, content: '…'};
  const delivered = {id: 'a1', kind: 'assistant', turn_id: 't1', content: '完成了'};
  const folded = {id: 'er-folded', kind: 'execution_result', consumed_by_turn_id: 't1', content: {status: 'succeeded'}};
  const byDelivery = {id: 'er-del', kind: 'execution_result', consumed_by_delivery_id: 'dep-1', content: {status: 'succeeded'}};
  const waiting = {id: 'er-wait', kind: 'execution_result', content: {status: 'succeeded', result_text: 'ok'}};
  const onInterimTurn = {id: 'er-interim', kind: 'execution_result', consumed_by_turn_id: 't0', content: {status: 'running'}};
  const visible = visibleChatMessages([folded, byDelivery, waiting, onInterimTurn, interim, delivered]);
  assert.deepEqual(visible.map((m) => m.id), ['er-wait', 'er-interim', 'a0', 'a1']);
});
