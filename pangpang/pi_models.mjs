// Pi owns request mapping, streaming and reasoning replay. This file only selects
// an adapter and supplies operator-owned configuration. No new model framework.
import catalog from './model_catalog.json' with {type: 'json'};

const adapters = {
  'openai-completions': () => import('@earendil-works/pi-ai/api/openai-completions'),
  'openai-responses': () => import('@earendil-works/pi-ai/api/openai-responses'),
  'anthropic-messages': () => import('@earendil-works/pi-ai/api/anthropic-messages'),
  'google-generative-ai': () => import('@earendil-works/pi-ai/api/google-generative-ai'),
};

export function resolveModel(profile) {
  const provider = profile.provider || 'custom';
  const known = catalog.providers[provider]?.[profile.model];
  if (provider !== 'custom' && !known) throw Error('model_not_in_pi_catalog_use_custom');
  const api = profile.api || 'openai-completions';
  if (!Object.hasOwn(adapters, api) || known && known.api !== api) throw Error('model_provider_api_mismatch');
  const model = known ? structuredClone(known): {
    id: profile.model, name: profile.model, api, provider: 'pangpang',
    reasoning: profile.reasoning_level && profile.reasoning_level !== 'default' ||
      profile.thinking_format && profile.thinking_format !== 'auto' || false,
    input: ['text'], contextWindow: 32000, maxTokens: 8192,
    cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}};
  model.baseUrl = profile.endpoint.replace(/\/$/, '');
  model.input = profile.vision === true ? ['text', 'image']: ['text'];
  model.contextWindow = Math.min(known?.contextWindow || Infinity,
    profile.context_window_tokens || profile.context_budget || model.contextWindow);
  model.maxTokens = Math.min(known?.maxTokens || Infinity, profile.output_tokens || model.maxTokens);
  if (api === 'openai-completions') {
    model.compat = {...model.compat};
    if (!known) Object.assign(model.compat, {supportsStore: false, supportsDeveloperRole: false});
    if (profile.thinking_format && profile.thinking_format !== 'auto') {
      model.compat.thinkingFormat = profile.thinking_format;
      // Qwen's switch is not evidence that this server also supports OpenAI effort.
      if (['qwen', 'qwen-chat-template'].includes(profile.thinking_format)) model.compat.supportsReasoningEffort = false;
    }
    if (profile.thinking_budget_field) model.compat.thinkingTokenBudgetField = profile.thinking_budget_field;
    if (profile.max_tokens_field && profile.max_tokens_field !== 'auto') model.compat.maxTokensField = profile.max_tokens_field;
    else if (!profile.provider && !profile.api) {
      // Compatibility for already-stored pre-provider profiles only. New profiles
      // use Pi detection or an explicit operator override; never infer thinking.
      model.compat.maxTokensField = /gpt/i.test(profile.model || '') || /cerebras\.ai/i.test(profile.endpoint || '')
        ? 'max_completion_tokens': 'max_tokens';
    }
  }
  return model;
}

export async function streamModel(model, context, profile, options) {
  const adapter = await adapters[model.api]();
  const level = profile.reasoning_level || 'default';
  const gen = profile.generation_settings || {};
  const opts = {...options, temperature: gen.temperature};
  if (profile.thinking_budget) opts.thinkingBudgets = Object.fromEntries(
    ['minimal', 'low', 'medium', 'high'].map(k => [k, profile.thinking_budget]));
  // Do not inject Chat Completions' stream/model/parameter shape into native APIs.
  if (model.api === 'openai-completions') {
    opts.onPayload = payload => {
      const result = {...payload};
      if (level === 'default') {
        // Pi's low-level adapter may serialize "off" for undefined effort.
        // Our explicit default means leave this choice to the server instead.
        for (const key of ['enable_thinking', 'thinking', 'reasoning', 'reasoning_effort',
          'chat_template_kwargs', 'thinking_token_budget', 'thinking_budget', 'thinking_budget_tokens']) delete result[key];
      }
      return {...result,...gen};
    };
  }
  // Google uses global fetch rather than a custom fetch option. The worker installs
  // its same-origin guard globally before any request (one isolated process/turn).
  if (model.api === 'google-generative-ai') delete opts.fetch;
  if (level === 'default') return adapter.stream(model, context, opts);
  return adapter.streamSimple(model, context, {...opts, reasoning: level === 'off' ? undefined: level});
}

export function guardedFetch(rawFetch, endpoint, secret, signal, timeoutMs = 120000) {
  const origin = new URL(endpoint).origin;
  return async (input, init) => {
    const request = new Request(input, init);
    if (new URL(request.url).origin !== origin) throw Error('endpoint_changed');
    const headers = new Headers(request.headers);
    if (!secret) for (const key of ['authorization', 'x-api-key', 'x-goog-api-key']) headers.delete(key);
    const signals = [request.signal, signal, AbortSignal.timeout(timeoutMs)].filter(Boolean);
    return rawFetch(new Request(request, {headers, redirect: 'error', signal: AbortSignal.any(signals)}));
  };
}
