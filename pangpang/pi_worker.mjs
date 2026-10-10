import { Agent } from '@earendil-works/pi-agent-core';
import { resolveModel, streamModel, guardedFetch } from './pi_models.mjs';
import { createInterface } from 'node:readline';

const input = createInterface({input: process.stdin, crlfDelay: Infinity})[Symbol.asyncIterator]();
let sequence = 0;
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
async function read() {
  const next = await input.next();
  // Up to 4 attached images (<= 5 MiB each, base64) can ride a history line.
  if (next.done || next.value.length > 32 * 1024 * 1024) throw Error('ipc_closed_or_oversized');
  return JSON.parse(next.value);
}
async function rpc(type, body = {}) {
  const id = ++sequence;
  send({version: 1, id, type,...body});
  const reply = await read();
  if (reply.id !== id || reply.version !== 1) throw Error('ipc_identity_mismatch');
  if (reply.error) throw Error(reply.error);
  return reply.result;
}
function usage() { return {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}}; }
function toPi(history, model) {
  const names = new Map();
  return history.filter(m => m.role !== 'system').map(m => {
    if (m.role === 'user') return {role: 'user', timestamp: 0, content: m.images?.length
      ? [{type: 'text', text: m.content},...m.images.map(i => ({type: 'image', data: i.data, mimeType: i.mime}))]
: m.content};
    if (m.role === 'assistant') {
      for (const c of m.tool_calls || []) names.set(c.id, c.function.name);
      if (m._pi) return m._pi;
      const content = m.content ? [{type: 'text', text: m.content}]: [];
      for (const c of m.tool_calls || []) content.push({type: 'toolCall', id: c.id,
        name: c.function.name, arguments: JSON.parse(c.function.arguments)});
      return {role: 'assistant', content, api: model.api, provider: model.provider,
        model: model.id, usage: usage(), stopReason: m.tool_calls?.length ? 'toolUse': 'stop', timestamp: 0};
    }
    return {role: 'toolResult', toolCallId: m.tool_call_id, toolName: names.get(m.tool_call_id) || 'unknown',
      content: [{type: 'text', text: m.content}], isError: Boolean(JSON.parse(m.content).error), timestamp: 0};
  });
}
function fromPi(m) {
  return {role: 'assistant', content: m.content.filter(b => b.type === 'text').map(b => b.text).join(''),
    tool_calls: m.content.filter(b => b.type === 'toolCall').map(c => ({id: c.id, type: 'function',
      function: {name: c.name, arguments: JSON.stringify(c.arguments)}})), _pi: m};
}
async function main() {
  const init = await read();
  if (init.version !== 1) throw Error('ipc_version');
  const profile = init.profile;
  const endpoint = new URL(profile.endpoint);
  if (endpoint.username || endpoint.password || endpoint.search || endpoint.hash ||
      !(endpoint.protocol === 'https:' || endpoint.protocol === 'http:' &&
        ['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname))) throw Error('invalid_endpoint');
  const model = resolveModel(profile);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), (profile.timeout_s || 120) * 1000 * (init.budget || 8) + 30000);
  let waiting = false;
  let finalRound = false;
  const originalFetch = globalThis.fetch;
  const safeFetch = guardedFetch(originalFetch, profile.endpoint, init.secret, controller.signal, (profile.timeout_s || 120) * 1000);
  globalThis.fetch = safeFetch;
  const runStream = async (selected, context, options) => {
    await rpc('model_begin');
    if (finalRound) context = {...context, tools: []};
    return streamModel(model, context, profile, {...options, apiKey: init.secret || 'pangpang-no-auth',
      maxRetries: 0, timeoutMs: (profile.timeout_s || 120) * 1000, fetch: safeFetch,
      maxTokens: model.maxTokens, signal: controller.signal});
  };
  try {
    const tools = init.tools.map(t => ({name: t.function.name, label: t.function.name,
      description: t.function.description, parameters: t.function.parameters, executionMode: 'sequential',
      execute: async (id, args) => {
        const result = await rpc('tool', {call_id: id, name: t.function.name, args});
        waiting ||= Boolean(result.yielded);
        return {content: [{type: 'text', text: JSON.stringify(result)}], details: {}, terminate: waiting};
      }}));
    if (init.mode === 'complete') {
      const ctx = {systemPrompt: init.history.filter(m => m.role === 'system').map(m => m.content).join('\n'),
        messages: toPi(init.history, model), tools: tools.map(({name, description, parameters}) => ({name, description, parameters}))};
      const s = await runStream(model, ctx, {});
      const response = await s.result();
      await rpc('assistant', {message: fromPi(response), stop: response.stopReason, usage: response.usage, errorMessage: response.errorMessage});
      send({version: 1, type: 'finished'});
      return;
    }
    const agent = new Agent({initialState: {
      systemPrompt: init.history[0].content, model, messages: toPi(init.history, model), tools, thinkingLevel: 'off'},
      streamFn: runStream, toolExecution: 'sequential',
      transformContext: async () => {
        const result = await rpc('context');
        finalRound = Boolean(result.final_round);
        return toPi(result.history, model);
      },
      shouldStopAfterTurn: () => waiting});
    // Streaming: forward accumulated assistant text as no-id one-way 'delta'
    // notifications (they must NOT carry an id — the cloud rpc loop treats any
    // id-bearing event as a request/response and would desync). Throttled so a
    // token-by-token stream does not flood stdout with O(n^2) full-text lines.
    let deltaRev = 0, pendingText = null, deltaTimer = null;
    const DELTA_MS = 50;
    const flushDelta = () => {
      deltaTimer = null;
      if (pendingText === null) return;
      send({version: 1, type: 'delta', text: pendingText, rev: ++deltaRev});
      pendingText = null;
    };
    agent.subscribe(async event => {
      if (event.type === 'message_update' && event.message.role === 'assistant'
          && event.assistantMessageEvent?.type === 'text_delta') {
        // Accumulated text of the CURRENT assistant message (resets to empty on a
        // new assistant message after a tool round — that is why the cloud/front
        // end key on rev, never on text length).
        const text = event.message.content.filter(b => b.type === 'text').map(b => b.text).join('');
        pendingText = text;
        if (!deltaTimer) deltaTimer = setTimeout(flushDelta, DELTA_MS);
      }
      if (event.type === 'message_end' && event.message.role === 'assistant') {
        if (deltaTimer) { clearTimeout(deltaTimer); flushDelta(); } // flush tail before the final message
        await rpc('assistant', {message: fromPi(event.message), stop: event.message.stopReason, usage: event.message.usage, errorMessage: event.message.errorMessage});
      }
    });
    await agent.continue();
    if (deltaTimer) clearTimeout(deltaTimer);
    if (agent.state.errorMessage) throw Error('agent_failed');
    send({version: 1, type: 'finished', waiting});
  } finally { globalThis.fetch = originalFetch; clearTimeout(timer); }
}
main().catch(() => {send({version: 1, type: 'failed', error: 'pi_worker_failed'}); process.exitCode = 1;})
.finally(() => {process.stdin.destroy();});
