import copy
import json
import ssl
import unittest
from unittest.mock import patch

import httpx

import llm


SETTINGS = {'provider': 'local', 'base_url': 'http://127.0.0.1:1234/v1',
            'model': 'google/gemma-3n-e4b', 'api_key': 'test-secret',
            'proxy': 'http://proxy.invalid:8080', 'bypass_local': True}
PAYLOAD = {'keywords': '自動運転、次世代自動車'}
SYSTEM = '分類候補を {"candidates":[]} 形式で返してください。'


def response(content='{"ok":true}', finish='stop', envelope=None, status=200):
    if envelope is None:
        envelope = {'choices': [{'message': {'role': 'assistant', 'content': content},
                                 'finish_reason': finish}]}
    return httpx.Response(status, json=envelope,
                          request=httpx.Request('POST', SETTINGS['base_url'] + '/chat/completions'))


class LLMTests(unittest.TestCase):
    def run_requests(self, responses, settings=None, *, response_schema=None):
        calls = []
        clients = []

        class FakeClient:
            def __init__(self, **kwargs):
                clients.append(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, **kwargs):
                calls.append((url, copy.deepcopy(kwargs)))
                result = responses.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

        self.calls, self.clients = calls, clients
        with patch.object(llm.httpx, 'Client', FakeClient):
            return llm.complete(settings or SETTINGS, SYSTEM, PAYLOAD,
                                response_schema=response_schema)

    def test_response_schema_is_forwarded_unchanged_on_both_json_attempts(self):
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                  'required': ['ok'], 'additionalProperties': False}
        original_schema, original_settings = copy.deepcopy(schema), copy.deepcopy(SETTINGS)
        self.assertEqual(self.run_requests([response('{"ok":'), response()],
                                          response_schema=schema), {'ok': True})
        self.assertEqual(len(self.calls), 2)
        for _, kwargs in self.calls:
            self.assertEqual(kwargs['json']['response_format'], {
                'type': 'json_schema', 'json_schema': {
                    'name': 'patent_judgments', 'strict': True, 'schema': original_schema}})
            self.assertEqual(kwargs['json']['max_tokens'], 4096)
        self.assertEqual(schema, original_schema)
        self.assertEqual(SETTINGS, original_settings)
        self.assertEqual(self.calls[0][1]['json']['messages'][1],
                         self.calls[1][1]['json']['messages'][1])

    def test_schema_is_detached_from_caller_and_fresh_for_each_attempt(self):
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
        original = copy.deepcopy(schema)
        calls = []

        def request(_base, _headers, body, _proxy, _verify, *, read_timeout):
            calls.append(copy.deepcopy(body))
            body['response_format']['json_schema']['schema']['properties']['ok']['type'] = 'string'
            return response('{"ok":') if len(calls) == 1 else response()

        with patch.object(llm, '_request', side_effect=request):
            self.assertEqual(llm.complete(SETTINGS, SYSTEM, PAYLOAD,
                                          response_schema=schema), {'ok': True})
        self.assertEqual(schema, original)
        self.assertEqual(len(calls), 2)
        for body in calls:
            self.assertEqual(body['response_format']['json_schema']['schema'], original)

    def test_invalid_response_schema_fails_before_http(self):
        cycle = {}
        cycle['properties'] = cycle
        invalid = [[], 'object', True, {'value': object()}, {'value': float('nan')},
                   {'value': float('inf')}, cycle]
        for schema in invalid:
            with self.subTest(schema_type=type(schema).__name__), patch.object(llm.httpx, 'Client') as client:
                with self.assertRaisesRegex(ValueError, '応答スキーマ'):
                    llm.complete(SETTINGS, SYSTEM, PAYLOAD, response_schema=schema)
                client.assert_not_called()

    def test_schema_http_and_transport_failures_do_not_fallback_or_retry(self):
        schema = {'type': 'object'}
        for status in (400, 401, 403, 422, 429, 500):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, f'HTTP {status}') as caught:
                self.run_requests([response(envelope={'error': 'response_format unsupported test-secret'},
                                            status=status)], response_schema=schema)
            self.assertEqual(len(self.calls), 1)
            self.assertNotIn('test-secret', str(caught.exception))
        with self.assertRaisesRegex(ValueError, '接続できません'):
            self.run_requests([httpx.ConnectError('test-secret')], response_schema=schema)
        self.assertEqual(len(self.calls), 1)

    def test_plain_fenced_and_harmless_prose_objects(self):
        samples = ['{"ok":true}', '```json\n{"ok":true}\n```',
                   '提案です。\n```JSON\n{"ok":true}\n```\n以上です。',
                   '回答: {"ok":true}\n以上です。',
                   '<think>{"analysis": "not the answer"}</think>\n{"ok":true}',
                   '\ufeff{"ok":true}', '```json\n{"ok":true}']
        for content in samples:
            with self.subTest(content=content):
                self.assertEqual(self.run_requests([response(content)]), {'ok': True})
                self.assertEqual(len(self.calls), 1)

    def test_supplied_stop_truncation_retries_original_request_once(self):
        complete_items = [{'code': f'H04L{i}/00', 'kind': 'IPC', 'title': '車両',
                           'reason': '関連候補'} for i in range(10)]
        unfinished = ('```json\n{"candidates":[' + ','.join(json.dumps(item) for item in complete_items)
                      + ',{"code":"G05L51/00","kind":"IPC","title":"車両の駐車装置",\n      ')
        result = self.run_requests([response(unfinished, finish='stop'), response('{"candidates":[]}')])
        self.assertEqual(result, {'candidates': []})
        self.assertEqual(len(self.calls), 2)
        first, second = (call[1]['json'] for call in self.calls)
        self.assertEqual(first['messages'][1], second['messages'][1])
        self.assertEqual(json.loads(second['messages'][1]['content']), PAYLOAD)
        self.assertEqual(first['max_tokens'], 4096)
        self.assertEqual(second['max_tokens'], 4096)
        self.assertFalse(first['stream'])
        self.assertNotIn('response_format', first)
        self.assertIn('短い1文', second['messages'][0]['content'])
        self.assertNotIn('G05L51/00', json.dumps(second, ensure_ascii=False))
        self.assertEqual(len(second['messages']), 2)

    def test_repeated_stop_truncation_reports_incomplete_not_connection(self):
        with self.assertRaisesRegex(ValueError, '途中で終了.*1回再試行') as caught:
            self.run_requests([response('```json\n{"candidates": [{"title":"secret'),
                               response('```json\n{"candidates": [{"title":"secret')])
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('接続できません', str(caught.exception))
        self.assertIn('停止文字列', str(caught.exception))

    def test_length_is_not_silently_accepted_even_when_json_is_closed(self):
        self.assertEqual(self.run_requests([response('{"ok":true}', finish='length'),
                                           response('{"ok":false}')]), {'ok': False})
        self.assertEqual(len(self.calls), 2)
        with self.assertRaisesRegex(ValueError, '出力上限.*1回再試行'):
            self.run_requests([response(finish='length'), response(finish='length')])

    def test_rejects_partial_nested_ambiguous_and_nonstandard_json(self):
        samples = ['{"broken": [{"ok":true},', '[{"ok":true}]',
                   '{"ok":true}\n{"ok":false}',
                   '{"ok":true} false', '{"ok":true} 1', '{"ok":true} "another value"',
                   '```json\n{"ok":true}\n```\n```json\n{"ok":false}\n```',
                   '{"ok":true,"ok":false}', '{"x":NaN}', '"{\\"ok\\":true}"',
                   'null', 'A message with no JSON', '```python\n{"ok":true}\n```']
        for content in samples:
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, '1回再試行'):
                self.run_requests([response(content), response(content)])
            self.assertEqual(len(self.calls), 2)

    def test_deep_json_produces_format_errors_and_no_recursion_error(self):
        nested = '{"value":' + '[' * 2000 + '0' + ']' * 2000 + '}'
        with self.assertRaisesRegex(ValueError, '入れ子が深すぎます.*1回再試行'):
            self.run_requests([response(nested), response(nested)])
        self.assertEqual(len(self.calls), 2)
        raw_envelope = httpx.Response(200, content=nested.encode(),
                                     request=httpx.Request('POST', SETTINGS['base_url']))
        with self.assertRaisesRegex(ValueError, '応答本体'):
            self.run_requests([raw_envelope])
        self.assertEqual(len(self.calls), 1)

    def test_invalid_envelopes_are_not_connection_errors_and_do_not_retry(self):
        samples = [[], {'choices': []}, {'choices': 'bad'}, {'choices': [None]},
                   {'choices': [{'message': None}]},
                   {'choices': [{'message': {'content': None}}]},
                   {'choices': [{'message': {'content': []}}]},
                   {'choices': [{'message': {'content': '  '}}]}]
        for envelope in samples:
            with self.subTest(envelope=envelope), self.assertRaises(ValueError) as caught:
                self.run_requests([response(envelope=envelope)])
            self.assertEqual(len(self.calls), 1)
            self.assertNotIn('接続できません', str(caught.exception))
            self.assertNotIn('1回再試行', str(caught.exception))

    def test_refusal_and_non_json_envelopes_do_not_retry(self):
        samples = [response(envelope={'choices': [{'message': {'content': '{"ok":true}',
                                                              'refusal': 'sensitive refusal'}}]}),
                   response(finish='content_filter'), response(finish='tool_calls'),
                   httpx.Response(200, content=b'<html>secret gateway detail</html>',
                                  request=httpx.Request('POST', SETTINGS['base_url']))]
        for item in samples:
            with self.subTest(response=item), self.assertRaises(ValueError) as caught:
                self.run_requests([item])
            self.assertEqual(len(self.calls), 1)
            self.assertNotIn('secret', str(caught.exception))
            self.assertNotIn('sensitive', str(caught.exception))
            self.assertNotIn('接続できません', str(caught.exception))

    def test_http_and_connection_failures_do_not_retry_or_leak_details(self):
        for status in (302, 401, 403, 429, 500):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, f'HTTP {status}'):
                self.run_requests([response(envelope={'error': 'test-secret'}, status=status)])
            self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(ValueError, '接続できません') as caught:
            self.run_requests([httpx.ConnectError('test-secret proxy password')])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('test-secret', str(caught.exception))

    def test_response_size_limit_does_not_retry(self):
        with patch.object(llm, 'MAX_RESPONSE_BYTES', 10):
            with self.assertRaisesRegex(ValueError, '応答が大きすぎます'):
                self.run_requests([response()])
        self.assertEqual(len(self.calls), 1)

    def test_auth_proxy_ca_and_redirect_safeguards_survive_retry(self):
        settings = dict(SETTINGS, base_url='https://model.invalid/v1', ca_bundle='custom.pem')
        original = copy.deepcopy(settings)
        context = object()
        with patch.object(llm.ssl, 'create_default_context', return_value=context) as create:
            self.run_requests([response('bad output'), response()], settings)
        create.assert_called_once_with(cafile='custom.pem')
        for options in self.clients:
            self.assertEqual(options['proxy'], SETTINGS['proxy'])
            self.assertFalse(options['trust_env'])
            self.assertFalse(options['follow_redirects'])
            self.assertIs(options['verify'], context)
        for url, kwargs in self.calls:
            self.assertEqual(url, 'https://model.invalid/v1/chat/completions')
            self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-secret')
            self.assertNotIn('api_key', kwargs['json'])
        self.assertEqual(settings, original)
        self.run_requests([response()])
        self.assertIsNone(self.clients[0]['proxy'])
        self.run_requests([response()], dict(SETTINGS, bypass_local=False))
        self.assertEqual(self.clients[0]['proxy'], SETTINGS['proxy'])

    def test_response_timeout_is_configurable_without_changing_other_limits_or_settings(self):
        for value in (None, 30, 300, 600):
            with self.subTest(value=value):
                settings = dict(SETTINGS)
                if value is not None:
                    settings['llm_timeout'] = value
                original = copy.deepcopy(settings)
                self.run_requests([response('bad JSON'), response()], settings)
                self.assertEqual(len(self.clients), 2)
                for options in self.clients:
                    timeout = options['timeout']
                    self.assertEqual(timeout.read, value if value is not None else 120)
                    self.assertEqual(timeout.connect, 10)
                    self.assertEqual(timeout.write, 120)
                    self.assertEqual(timeout.pool, 120)
                self.assertEqual(settings, original)

    def test_timeout_has_actionable_message_without_leaking_or_retrying(self):
        errors = (httpx.ReadTimeout('test-secret'), httpx.ConnectTimeout('test-secret'),
                  httpx.WriteTimeout('test-secret'), httpx.PoolTimeout('test-secret'))
        for error in errors:
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(ValueError, '待ち時間.*超過') as caught:
                self.run_requests([error], dict(SETTINGS, llm_timeout=300))
            self.assertEqual(len(self.calls), 1)
            message = str(caught.exception)
            self.assertIn('設定', message)
            self.assertNotIn('接続できません', message)
            self.assertNotIn('test-secret', message)
            if isinstance(error, httpx.ReadTimeout):
                self.assertIn('300秒', message)

    def test_invalid_timeout_fails_before_network(self):
        for value in (None, True, False, 29, 601, 120.0, '300', '', [], {}, float('nan')):
            with self.subTest(value=value), patch.object(llm.httpx, 'Client') as client:
                with self.assertRaisesRegex(ValueError, '30〜600秒の整数'):
                    llm.complete(dict(SETTINGS, llm_timeout=value), SYSTEM, PAYLOAD)
                client.assert_not_called()

    def test_invalid_settings_and_ca_fail_before_http(self):
        for settings in ({'provider': 'offline'}, dict(SETTINGS, base_url='https://user:secret@model.invalid')):
            with self.subTest(settings=settings), patch.object(llm.httpx, 'Client') as client:
                with self.assertRaises(ValueError):
                    llm.complete(settings, SYSTEM, PAYLOAD)
                client.assert_not_called()
        with patch.object(llm.ssl, 'create_default_context', side_effect=ssl.SSLError('secret path')):
            with patch.object(llm.httpx, 'Client') as client, self.assertRaisesRegex(ValueError, 'CA証明書') as caught:
                llm.complete(dict(SETTINGS, ca_bundle='secret.pem'), SYSTEM, PAYLOAD)
            client.assert_not_called()
            self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
