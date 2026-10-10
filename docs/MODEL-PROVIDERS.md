# Global model providers

The supervisor and child Bots share this model configuration. Executor model and
account settings remain independent. Python owns credentials, permissions and
durable work. Pi owns model request formats, streams, tools and thinking replay.

## Setup

1. Open **Settings → Global conversation model**.
2. Select a provider and a model from the pinned Pi catalog. The protocol and base
   address follow the catalog. Enter an API key, or explicitly choose no auth for
   a keyless endpoint. Saving checks a tool call and the subsequent tool result.
3. Use **Custom / local / third-party** for an unlisted model or a proxy. Select
   the protocol explicitly. Anthropic usually needs a root base URL; Chat
   Completions/Responses usually include `/v1`; Gemini usually includes `/v1beta`.
4. Leave thinking at **server default** unless you need an explicit setting.
   Supported catalog levels follow Pi metadata. Custom endpoints must be verified
   against their own server documentation.

The API-key paths shipped here are OpenAI Chat Completions, OpenAI Responses,
Anthropic Messages and Google Generative AI. Catalog entries are generated from
`@earendil-works/pi-ai@0.85.1` for these protocols. Providers requiring OAuth,
ambient cloud credentials, or unresolved account/gateway URL templates are not
advertised by this setup flow. This is not a claim to support every Pi auth flow,
or to have live-tested every catalog model. A subscription is not an API key.

## Qwen and other compatible services

Do not infer a wire format from the model name. In the advanced Chat Completions
settings, select the format documented by the endpoint:

| Format | Pi request representation |
| --- | --- |
| `auto` | Pi compatibility detection and catalog metadata |
| `openai` | `reasoning_effort` |
| `qwen` | Top-level `enable_thinking` |
| `qwen-chat-template` | `chat_template_kwargs.enable_thinking` and `preserve_thinking` |
| `deepseek`, `zai`, `openrouter`, `together` | Pi's corresponding built-in mapping |

The default setting does not inject thinking switches. An explicit low/medium/
high setting uses Pi `streamSimple`; off uses its off mapping. Some models cannot
disable reasoning. Known unsupported levels are rejected before storing a key.
Thinking budget fields and `max_tokens` vs `max_completion_tokens` can be selected
for a custom server. Do not send an unsupported field merely because the model
is Qwen. The server's chat template and tool parser must also be configured.

Existing explicit `generation_settings` survive an unchanged profile unless the
operator clears **keep old generation settings**. Changing provider, model,
endpoint or thinking mode clears incompatible old parameters. New configurations
no longer auto-enable thinking for Qwen or inject a cloud provider's reasoning-effort default. Pre-provider
profiles retain their historical token-limit field until reconfigured.

## Boundary and maintenance

- The approved endpoint remains the request's only origin. Redirects are refused.
- Credentials come from the existing vault/environment reference. Keyless requests
  strip Authorization, Anthropic and Google key headers. No new credential store.
- Pi's full assistant blocks are retained under `_pi` for same-model tool replay,
  including opaque thinking signatures; these are not rendered as normal chat.
- Each isolated worker loads only the selected API adapter at runtime. The release
  bundle contains the four adapters, not Pi's whole provider/OAuth registry.
- The generated catalog is data only, served on an authenticated endpoint when the
  settings dialog opens. It is not copied into every dashboard snapshot.
- After updating the pinned Pi dependency, run `npm ci` and regenerate the model
  catalog data from the pinned Pi SDK, then review the generated data. Keep the
  Pi MIT notice in source and packaged worker artifacts.

## Verification

`test_model_providers.py` uses actual Pi SDKs against local protocol fixtures. It
checks tool round trips, Claude/Gemini signatures across serialized worker calls,
Qwen parameter placement/reasoning replay, no hidden retries, redirect refusal,
and keyless headers. Existing model/onboarding/safety tests remain required.
The browser contract checks catalog selection, third-party configuration, mobile
dialog bounds and preservation of operator input after a failed probe.

Live API credentials, actual local serving configurations and Windows/macOS release
execution require their own release-installation acceptance evidence.
