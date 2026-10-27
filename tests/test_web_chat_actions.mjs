// Reply plain text and sidebar order. Both import the production helpers in
// web-src/pure.ts — the same module the React shell uses.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {messagePlainText, orderBots} from '../web-src/pure.ts';

test('messagePlainText copies the unwrapped reply body', () => {
  const frame = JSON.stringify({kind: 'assistant', source: 'bot-x', content: '真正的答复', work_id: 'w1'});
  assert.equal(messagePlainText(frame), '真正的答复');
  assert.equal(messagePlainText('普通的 markdown 回复'), '普通的 markdown 回复');
  assert.equal(messagePlainText({summary: '结果'}), JSON.stringify({summary: '结果'}));
  assert.equal(messagePlainText(null), '');
});

test('orderBots keeps the steward first and sorts the rest by last turn time', () => {
  const bots = [
    {id: 'edit', kind: 'service', name: '文字编辑'},
    {id: 'main', kind: 'main', name: '胖胖'},
    {id: 'note', kind: 'service', name: '通知测试'},
    {id: 'quiet', kind: 'service', name: '审批测试'},
    {id: 'older', kind: 'service', name: '恢复测试'},
  ];
  const turns = [
    {bot_id: 'note', created: 400},
    {bot_id: 'edit', created: 400},
    {bot_id: 'edit', created: 100},
    {bot_id: 'main', created: 9999},
    {bot_id: 'older', created: 50},
    {bot_id: 'quiet', created: Number.NaN},
    {bot_id: 'missing', created: 10},
  ];
  assert.deepEqual(orderBots(bots, turns).map((b) => b.id), ['main', 'edit', 'note', 'older', 'quiet']);
});

test('orderBots with no turn times keeps the incoming order after the steward', () => {
  const bots = [
    {id: 'b', kind: 'service'},
    {id: 'a', kind: 'service'},
    {id: 'main', kind: 'main'},
  ];
  assert.deepEqual(orderBots(bots, []).map((b) => b.id), ['main', 'b', 'a']);
  assert.deepEqual(orderBots(bots, [{bot_id: 'a'}]).map((b) => b.id), ['main', 'b', 'a']);
});
