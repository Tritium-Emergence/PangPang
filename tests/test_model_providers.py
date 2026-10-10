"""Real Pi SDKs + local wire fixtures: no paid keys and no vendor availability claims."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from pangpang.model_config import catalog, setup_profile, validate_profile
from pangpang.pi_runtime import PiModel
from pangpang.service import Service, normalize_profile
from pangpang.onboarding import empty_config
from pangpang.store import Fault
from test_model_protocol import Endpoint
from test_bot_runtime import call

PROBE = {"type": "function", "function": {"name": "setup_probe", "description": "Probe",
    "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}}


def wire(api, first):
    """Return native SSE including opaque continuity metadata, not just final text."""
    if api == "anthropic-messages":
        events = [{"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant",
            "model": "test", "content": [], "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}]
        blocks = [{"type": "thinking", "thinking": "fixture", "signature": "opaque-signature"},
                  {"type": "tool_use", "id": "toolu_test", "name": "setup_probe", "input": {"value": "pangpang-ready"}}] if first else [{"type": "text", "text": "ready"}]
        for i, block in enumerate(blocks):
            events.append({"type": "content_block_start", "index": i, "content_block": block})
            if block['type'] == 'tool_use':
                events.append({"type": "content_block_delta", "index": i,
                               "delta": {"type": "input_json_delta", "partial_json": json.dumps(block['input'])}})
            events.append({"type": "content_block_stop", "index": i})
        events += [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if first else "end_turn"}, "usage": {"output_tokens": 4}}, {"type": "message_stop"}]
    elif api == "google-generative-ai":
        parts = [{"functionCall": {"name": "setup_probe", "args": {"value": "pangpang-ready"}},
                  "thoughtSignature": "c2lnbmF0dXJl"}] if first else [{"text": "ready"}]
        events = [{"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
                   "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4, "totalTokenCount": 14}}]
    else:
        item = {"type": "function_call", "id": "fc_test", "call_id": "call_test", "name": "setup_probe",
                "arguments": '{"value":"pangpang-ready"}', "status": "completed"} if first else {
                "type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "ready", "annotations": []}]}
        events = [{"type": "response.created", "response": {"id": "resp_test", "status": "in_progress"}},
                  {"type": "response.output_item.added", "output_index": 0, "item": item},
                  {"type": "response.output_item.done", "output_index": 0, "item": item},
                  {"type": "response.completed", "response": {"id": "resp_test", "status": "completed", "output": [item],
                   "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}}]
    return ''.join(('event: ' + e['type'] + '\n' if 'type' in e else '') + 'data: ' + json.dumps(e) + '\n\n' for e in events).encode()


class NativeEndpoint:
    def __init__(self, api, status=200, redirect=None):
        self.received = []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.received.append((self.path, dict(self.headers), payload))
                if status != 200:
                    self.send_response(status)
                    if redirect: self.send_header('Location', redirect)
                    self.send_header('Content-Length', '0'); self.end_headers(); return
                data = wire(api, len(outer.received) == 1)
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}' + ('' if api == 'anthropic-messages' else '/v1')
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


class ProviderTests(unittest.TestCase):
    def test_no_native_hidden_retry_or_redirect(self):
        for api in ('anthropic-messages', 'google-generative-ai', 'openai-responses', 'openai-completions'):
            for status in (429, 307):
                with self.subTest(api=api, status=status):
                    target = NativeEndpoint(api)
                    ep = NativeEndpoint(api, status=status, redirect=target.url)
                    try:
                        with self.assertRaises(Fault):
                            PiModel().complete({'model': 'test', 'api': api, 'endpoint': ep.url},
                                               [{'role': 'user', 'content': 'probe'}], [PROBE])
                        self.assertEqual(len(ep.received), 1)
                        self.assertEqual(target.received, [], 'redirect must never receive the request')
                        headers = {k.lower(): v for k, v in ep.received[0][1].items()}
                        for key in ('authorization', 'x-api-key', 'x-goog-api-key'):
                            self.assertNotIn(key, headers, 'keyless endpoints must not receive placeholder credentials')
                    finally:
                        ep.close(); target.close()

    def _complete(self, ep, api, which, profile, history):
        # On failure the Fault alone does not say whether the local fixture ever saw
        # the request (worker connect failure vs protocol handling) or which leg died;
        # surface only path/count — never headers or payloads.
        try:
            return PiModel().complete(profile, history, [PROBE])
        except Exception as error:
            raise AssertionError("api=%s %s completion failed (%s); endpoint saw %d request(s): %s"
                                 % (api, which, error, len(ep.received), [p for p, _, _ in ep.received])) from error

    def test_native_protocols_tool_roundtrip_and_signature_replay(self):
        for api in ('anthropic-messages', 'google-generative-ai', 'openai-responses'):
            with self.subTest(api=api):
                ep = NativeEndpoint(api)
                try:
                    profile = {'model': 'test', 'provider': 'custom', 'api': api, 'endpoint': ep.url,
                               'reasoning_level': 'low', 'credential_env': 'FIXTURE_MODEL_KEY'}
                    history = [{'role': 'user', 'content': 'Test the tool.'}]
                    with patch.dict(os.environ, {'FIXTURE_MODEL_KEY': 'fixture-key'}):
                        msg, _ = self._complete(ep, api, 'first', profile, history)
                        c = msg['tool_calls'][0]
                        self.assertEqual(json.loads(c['function']['arguments']), {'value': 'pangpang-ready'})
                        # Serialize as the control plane does, across separate worker processes.
                        msg = json.loads(json.dumps(msg))
                        history += [msg, {'role': 'tool', 'tool_call_id': c['id'], 'content': '{"value":"pangpang-ready"}'}]
                        final, _ = self._complete(ep, api, 'second', profile, history)
                    self.assertEqual(final['content'], 'ready')
                    path, headers, first = ep.received[0]
                    second = ep.received[1][2]
                    if api == 'anthropic-messages':
                        self.assertEqual(path.split('?')[0], '/v1/messages')
                        self.assertEqual(headers.get('x-api-key'), 'fixture-key')
                        self.assertEqual(first['thinking']['type'], 'enabled')
                        self.assertIn('opaque-signature', json.dumps(second))
                        self.assertIn('tool_result', json.dumps(second))
                    elif api == 'google-generative-ai':
                        self.assertIn(':streamGenerateContent', path)
                        self.assertEqual(headers.get('x-goog-api-key'), 'fixture-key')
                        self.assertIn('thinkingConfig', first['generationConfig'])
                        self.assertIn('c2lnbmF0dXJl', json.dumps(second))
                        self.assertIn('functionResponse', json.dumps(second))
                    else:
                        self.assertEqual(path, '/v1/responses')
                        # HTTP field names are case-insensitive. Bun sends
                        # 'Authorization'; a case-sensitive 'authorization' key misses it.
                        folded = {k.lower(): v for k, v in headers.items()}
                        self.assertEqual(folded.get('authorization'), 'Bearer fixture-key')
                        self.assertEqual(first['reasoning']['effort'], 'low')
                        self.assertIn('function_call_output', json.dumps(second))
                finally: ep.close()

    def test_qwen_formats_and_reasoning_replay(self):
        for fmt, level in [('qwen', 'low'), ('qwen-chat-template', 'low'), ('qwen-chat-template', 'off'), ('qwen', 'default'), ('auto', 'default')]:
            with self.subTest(format=fmt, level=level):
                ep = Endpoint(lambda p, n: dict(call('setup_probe', value='pangpang-ready'), reasoning_content='fixture') if n == 1 else {'content': 'ready'})
                try:
                    profile = {'model': 'qwen-arbitrary-alias', 'provider': 'custom', 'api': 'openai-completions',
                               'endpoint': ep.url, 'thinking_format': fmt, 'reasoning_level': level}
                    history = [{'role': 'user', 'content': 'probe'}]
                    msg, _ = PiModel().complete(profile, history, [PROBE])
                    history += [msg, {'role': 'tool', 'tool_call_id': msg['tool_calls'][0]['id'], 'content': '{}'}]
                    final, _ = PiModel().complete(profile, history, [PROBE])
                    self.assertEqual(final['content'], 'ready')
                    first, second = ep.received
                    if fmt == 'qwen' and level != 'default': self.assertTrue(first['enable_thinking'])
                    elif fmt == 'qwen-chat-template': self.assertIs(first['chat_template_kwargs']['enable_thinking'], level != 'off')
                    else:
                        self.assertNotIn('chat_template_kwargs', first)
                        self.assertNotIn('enable_thinking', first)
                    assistant = next(m for m in second['messages'] if m['role'] == 'assistant')
                    self.assertEqual(assistant['reasoning_content'], 'fixture')
                finally: ep.close()

    def test_model_names_do_not_inject_thinking(self):
        for endpoint in ('https://api.cerebras.ai/v1', 'https://third-party.example/v1'):
            p = normalize_profile({'model': 'qwen', 'endpoint': endpoint})
            self.assertNotIn('generation_settings', p)
        legacy = {'model': 'qwen', 'generation_settings': {'chat_template_kwargs': {'enable_thinking': False}}}
        self.assertEqual(normalize_profile(legacy)['generation_settings'], legacy['generation_settings'])

    def test_catalog_and_switch_clear_stale_parameters(self):
        self.assertEqual(catalog()['pi_version'], '0.85.1')
        old = {'model': 'qwen', 'endpoint': 'https://old.example/v1', 'generation_settings': {'chat_template_kwargs': {'enable_thinking': True}}}
        key, model = next(iter(catalog()['providers']['anthropic'].items()))
        p = setup_profile(old, {'provider': 'anthropic', 'model': key})
        self.assertEqual(p['api'], 'anthropic-messages')
        self.assertEqual(p['endpoint'], model['baseUrl'])
        self.assertNotIn('generation_settings', p)
        with self.assertRaises(Fault): setup_profile(old, {'provider': 'not-a-provider', 'model': 'qwen'})
        with self.assertRaises(Fault): validate_profile(dict(p, thinking_format='qwen'))
        with self.assertRaises(Fault): validate_profile(dict(p, generation_settings={'messages': []}))

    def test_same_legacy_config_preserves_explicit_settings(self):
        old = {'endpoint': 'https://local.example/v1', 'model': 'qwen', 'generation_settings': {'chat_template_kwargs': {'enable_thinking': False}}}
        p = setup_profile(old, {'endpoint': old['endpoint'], 'model': old['model']})
        self.assertEqual(p['generation_settings'], old['generation_settings'])
        p = setup_profile(old, {'endpoint': old['endpoint'], 'model': old['model'], 'reasoning_level': 'low', 'thinking_format': 'qwen'})
        self.assertNotIn('generation_settings', p)


if __name__ == '__main__': unittest.main()
