"""Qwen/OpenAI-compatible reasoning envelopes must preserve only final JSON."""
import copy
import json
import unittest
from unittest.mock import patch

import httpx

import llm


SETTINGS = {'provider': 'local', 'base_url': 'http://127.0.0.1:1234/v1',
            'model': 'qwen3-8b', 'bypass_local': True}
SYSTEM = '分類候補をJSONで返してください。'
PAYLOAD = {'keywords': '自動運転'}
_MISSING = object()


def response(content='{"ok":true}', *, finish='stop', **message_fields):
    message = {'role': 'assistant', **message_fields}
    if content is not _MISSING:
        message['content'] = content
    return httpx.Response(200, json={'choices': [{
        'message': message, 'finish_reason': finish,
    }]}, request=httpx.Request('POST', SETTINGS['base_url'] + '/chat/completions'))


class LLMReasoningTests(unittest.TestCase):
    def complete(self, responses, *, settings=None, response_schema=None):
        with patch.object(llm, '_request', side_effect=responses) as request:
            self.request = request
            return llm.complete(settings or SETTINGS, SYSTEM, PAYLOAD,
                                response_schema=response_schema)

    def test_closed_repeated_case_insensitive_thoughts_precede_final_answer(self):
        variants = [
            '<think>{"decoy":true}</think>\n{"ok":true}',
            '\ufeff <THINK>{"decoy":true}</THINK>\n```json\n{"ok":true}\n```',
            '<think>first</think>\n<Think>{"decoy":true}</tHiNk>\n{"ok":true}',
            'let me consider {"decoy":true}</think>{"ok":true}',
            '{"decoy":true}</THINK>\n{"ok":true}',
            '</think>{"ok":true}',
            '<think>Let me read "a phrase</think>{"ok":true}',
        ]
        for content in variants:
            with self.subTest(content=content):
                self.assertEqual(self.complete([response(content)]), {'ok': True})
                self.assertEqual(self.request.call_count, 1)

    def test_literal_thought_tags_inside_final_json_remain_unchanged(self):
        expected = {'title': '<think>literal</think>',
                    'reason': 'unclosed <think> and </THINK> are data',
                    'quoted': '\\"<think>{"decoy":true}</think>\\"'}
        answer = json.dumps(expected)
        for content in (answer, '<think>reasoning</think>\n' + answer,
                        '```json\n' + answer + '\n```'):
            with self.subTest(content=content):
                self.assertEqual(self.complete([response(content)]), expected)
                self.assertEqual(self.request.call_count, 1)

    def test_unclosed_nested_and_malformed_thoughts_never_supply_decoy_json(self):
        variants = [
            '<think>{"decoy":true}',
            '<THINK>{"decoy":true}',
            '<think><think>nested</think>{"decoy":true}</think>',
            '<think><think>nested</think>{"decoy":true}',
            '<think analysis>{"decoy":true}</think>',
            'Thinking: <think analysis>{"decoy":true}',
            'prefix < think>{"decoy":true}',
            '<think>analysis</think>{"final":true}</think>{"decoy":true}',
        ]
        for content in variants:
            with self.subTest(content=content):
                with self.assertRaisesRegex(ValueError, '1回再試行'):
                    self.complete([response(content), response(content)])
                self.assertEqual(self.request.call_count, 2)

    def test_explicit_text_blocks_concatenate_without_inserting_characters(self):
        content = [
            {'type': 'text', 'text': '{"key":"自'},
            {'type': 'output_text', 'text': '動運転","n":1'},
            {'type': 'text', 'text': '23,"ok":tr'},
            {'type': 'output_text', 'text': 'ue}'},
        ]
        self.assertEqual(self.complete([response(content)]),
                         {'key': '自動運転', 'n': 123, 'ok': True})
        self.assertEqual(self.request.call_count, 1)

    def test_reasoning_blocks_and_fields_are_not_final_answer(self):
        for kind in ('reasoning', 'thinking', 'analysis'):
            with self.subTest(kind=kind):
                content = [{'type': kind, 'text': '{"decoy":true}'},
                           {'type': 'text', 'text': '{"ok":true}'}]
                self.assertEqual(self.complete([response(
                    content, reasoning_content='{"other_decoy":true}',
                    reasoning='{"another_decoy":true}')]), {'ok': True})
                self.assertEqual(self.request.call_count, 1)
        for field in ('reasoning_content', 'reasoning'):
            with self.subTest(field=field):
                self.assertEqual(self.complete([response(**{
                    field: '{"decoy":true}'})]), {'ok': True})

    def test_unsupported_content_parts_are_non_retriable_even_alongside_valid_text(self):
        variants = [
            [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}],
            [{'type': 'unknown', 'text': '{"decoy":true}'}],
            [{'type': 'text', 'text': {'value': '{"decoy":true}'}}],
            [{'text': '{"decoy":true}'}],
            ['{"decoy":true}'],
            {'type': 'text', 'text': '{"decoy":true}'},
        ]
        for invalid in variants:
            for content in (invalid, [{'type': 'text', 'text': '{"ok":true}'},
                                      *invalid] if isinstance(invalid, list) else invalid):
                with self.subTest(content=content):
                    with self.assertRaises(ValueError) as caught:
                        self.complete([response(content)])
                    self.assertNotIn('1回再試行', str(caught.exception))
                    self.assertEqual(self.request.call_count, 1)

    def test_reasoning_only_responses_retry_once_and_never_parse_reasoning(self):
        variants = []
        for content in (None, '', '  ', _MISSING, []):
            for field in ('reasoning_content', 'reasoning'):
                variants.append(response(content, **{field: '{"decoy":true}'}))
        for kind in ('reasoning', 'thinking', 'analysis'):
            variants.append(response([{'type': kind, 'text': '{"decoy":true}'}]))
        for index, first in enumerate(variants):
            with self.subTest(variant=index):
                self.assertEqual(self.complete([first, response()]), {'ok': True})
                self.assertEqual(self.request.call_count, 2)
                with self.assertRaisesRegex(ValueError, '思考') as caught:
                    self.complete([first, first])
                self.assertIn('1回再試行', str(caught.exception))
                self.assertEqual(self.request.call_count, 2)

    def test_length_precedes_missing_empty_or_null_content_and_retries_once(self):
        for content in (_MISSING, None, '', '  ', [], '{"ok":true}'):
            with self.subTest(content=content):
                truncated = response(content, finish='length')
                self.assertEqual(self.complete([truncated, response()]), {'ok': True})
                self.assertEqual(self.request.call_count, 2)
                with self.assertRaisesRegex(ValueError, '出力上限') as caught:
                    self.complete([truncated, truncated])
                self.assertIn('1回再試行', str(caught.exception))
                self.assertEqual(self.request.call_count, 2)

    def test_empty_without_reasoning_or_length_does_not_retry(self):
        for content in (_MISSING, None, '', '  ', []):
            with self.subTest(content=content):
                with self.assertRaises(ValueError) as caught:
                    self.complete([response(content)])
                self.assertNotIn('1回再試行', str(caught.exception))
                self.assertEqual(self.request.call_count, 1)

    def test_refusal_filter_and_tool_call_envelopes_never_yield_good_text(self):
        variants = [response(refusal='declined'),
                    response(finish='content_filter'),
                    response(finish='tool_calls'),
                    response(tool_calls=[{'type': 'function', 'function': {
                        'name': 'unexpected', 'arguments': '{}'}}])]
        for index, envelope in enumerate(variants):
            with self.subTest(variant=index):
                with self.assertRaises(ValueError) as caught:
                    self.complete([envelope])
                self.assertNotIn('1回再試行', str(caught.exception))
                self.assertEqual(self.request.call_count, 1)

    def test_multipart_and_thought_normalization_preserve_json_guards(self):
        for invalid in ('{"ok":true,"ok":false}', '[{"ok":true}]',
                        '{"ok":true}{"other":true}'):
            variants = ['<think>analysis</think>' + invalid,
                        [{'type': 'text', 'text': invalid[:5]},
                         {'type': 'text', 'text': invalid[5:]}]]
            for content in variants:
                with self.subTest(content=content):
                    with self.assertRaisesRegex(ValueError, '1回再試行'):
                        self.complete([response(content), response(content)])
                    self.assertEqual(self.request.call_count, 2)

    def test_hybrid_local_qwen3_retry_hint_preserves_payload_and_schema(self):
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                  'required': ['ok'], 'additionalProperties': False}
        original_schema = copy.deepcopy(schema)
        expected_format = {'type': 'json_schema', 'json_schema': {
            'name': 'patent_judgments', 'strict': True, 'schema': original_schema}}
        for model in ('qwen3:8b', 'qwen/qwen3-8b'):
            for initial in (response(None, reasoning_content='{"draft":true}'),
                            response(None, finish='length')):
                with self.subTest(model=model, response=initial.json()):
                    settings = {**SETTINGS, 'model': model}
                    self.assertEqual(self.complete([initial, response()], settings=settings,
                                                   response_schema=schema), {'ok': True})
                    first, retry = [call.args[2] for call in self.request.call_args_list]
                    self.assertNotIn('/no_think', first['messages'][0]['content'])
                    self.assertTrue(retry['messages'][0]['content'].endswith('/no_think'))
                    for body in (first, retry):
                        self.assertEqual(body['messages'][1], {
                            'role': 'user', 'content': json.dumps(PAYLOAD, ensure_ascii=False)})
                        self.assertEqual(body['response_format'], expected_format)
                        self.assertEqual(body['model'], model)
                        self.assertNotIn('chat_template_kwargs', body)
                    self.assertEqual(settings['model'], model)
                    self.assertEqual(schema, original_schema)

    def test_qwen_hint_is_not_added_for_ordinary_json_correction(self):
        self.assertEqual(self.complete([response('{"ok":'), response()]), {'ok': True})
        self.assertEqual(self.request.call_count, 2)
        for call in self.request.call_args_list:
            self.assertNotIn('/no_think', call.args[2]['messages'][0]['content'])

    def test_qwen_hint_is_not_added_for_unsupported_models_or_other_providers(self):
        settings_variants = [
            {**SETTINGS, 'model': model}
            for model in ('qwen3-4b-thinking-2507', 'qwen3.5-9b', 'qwen2.5',
                          'google/gemma-3n-e4b')
        ] + [
            {**SETTINGS, 'provider': provider, 'model': 'qwen3:8b'}
            for provider in ('openai', 'compatible')
        ]
        for settings in settings_variants:
            for initial in (response(None, reasoning_content='still thinking'),
                            response(None, finish='length')):
                with self.subTest(settings=settings, response=initial.json()):
                    self.assertEqual(self.complete([initial, response()], settings=settings),
                                     {'ok': True})
                    self.assertEqual(self.request.call_count, 2)
                    for call in self.request.call_args_list:
                        self.assertNotIn('/no_think', call.args[2]['messages'][0]['content'])


if __name__ == '__main__':
    unittest.main()
