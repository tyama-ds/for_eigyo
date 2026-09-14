"""Language captions must never reinterpret IPC symbols or alter search inputs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import classification_catalog as catalog
import classification_explorer as explorer
import classification_translations as translations


def table(code, title='試験用の日本語定義'):
    return f'<table><tr><td><a name="{code}">{code}</a></td><td class="explanationListArea">・・{title}</td><td>Not a caption</td></tr></table>'


class TranslationTests(unittest.TestCase):
    def test_official_snapshot_is_bounded_and_version_is_explicit(self):
        bundle = translations._bundle()
        self.assertEqual(bundle['metadata']['count'], len(bundle['records']))
        self.assertFalse(bundle['metadata']['complete'])
        self.assertEqual(bundle['metadata']['symbol_validation_version'], 'WIPO 2026.01')
        self.assertTrue(bundle['metadata']['version'].startswith('PMGS snapshot '))
        self.assertGreater(len(bundle['records']), 1000)
        self.assertTrue(set(bundle['records']) <= set(catalog._catalog()[1]))
        codes = json.loads((translations.BUNDLE.parent / 'japanese_snapshot_codes.json').read_text(encoding='utf-8'))
        self.assertTrue(set(codes) <= set(bundle['records']))
        for code in ('H01M10/0562', 'C04B35/00', 'B60L53/00', 'G01N27/00', 'A', 'H'):
            self.assertTrue(catalog.lookup('IPC', code)['title_ja'])

    def test_canonical_title_and_hierarchy_stay_english_and_unchanged(self):
        before = copy.deepcopy(catalog._catalog()[1]['H01M10/0562'])
        row = catalog.lookup('IPC', 'H01M 10/0562')
        self.assertEqual(row['title'], before[1])
        self.assertEqual(row['title_en'], before[1])
        self.assertEqual(row['parent'], 'H01M10/0561')
        self.assertEqual(row['title_en_status'], 'official')
        self.assertEqual(row['title_ja_status'], 'official_translation')
        self.assertEqual(row['title_ja_parent_code'], 'H01M10/0561')
        self.assertIn('無機物のみからなる電解質', row['title_ja_parent'])
        self.assertEqual(row['title_en_parent'], catalog._catalog()[1]['H01M10/0561'][1])
        self.assertNotEqual(row['title_ja_version'], row['version'])
        self.assertEqual(catalog._catalog()[1]['H01M10/0562'], before)
        local = explorer.enrich({'kind': 'IPC', 'code': 'H01M10/0562'})
        self.assertEqual(local['title'], local['title_short_ja'])
        self.assertEqual(local['title_en'], row['title_en'])
        self.assertEqual(local['title_ja'], row['title_ja'])

    def test_unknown_system_and_untranslated_titles_are_not_fabricated(self):
        self.assertIsNone(catalog.lookup('IPC', 'H99Z999/99'))
        self.assertIsNone(catalog.lookup('CPC', 'H01M10/0562'))
        row = catalog.lookup('IPC', 'A01B1/04')
        self.assertEqual(row['title_ja_status'], 'unavailable')
        self.assertEqual(row['title_ja'], '')
        self.assertEqual(translations.label_fields('CPC', 'H01M10/0562'), {})

    def test_fterm_app_english_is_distinct_from_official_japanese(self):
        row = explorer.enrich({'kind': 'F-term', 'code': '5H029AM11'})
        self.assertEqual(row['title_ja'], '固体電解質')
        self.assertEqual(row['title_ja_status'], 'official')
        self.assertEqual(row['title_en'], 'Solid electrolytes')
        self.assertEqual(row['title_en_status'], 'app_translation')
        self.assertEqual(explorer.enrich({'kind':'F-term', 'code':'5H029AK01'})['title_en_status'], 'unavailable')

    def test_parser_takes_only_named_rows_and_their_own_caption(self):
        html = table('A01B1/04', '手工具 <span>その構造</span>')
        html += '<tr><td><a name="notes">note</a></td><td class="explanationArea">not a classification</td></tr>'
        self.assertEqual(translations.parse_table(html), {'A01B1/04': '手工具 その構造'})
        records = translations.caption_records({'A01B1/04':'手工具','A01B999/99':'推測'}, 'ipcList/ipcListA01B1_00.html', catalog._catalog()[1], '2026-09-12')
        self.assertEqual(list(records), ['A01B1/04'])
        self.assertEqual(records['A01B1/04']['title_ja_version'], 'PMGS snapshot 2026-09-12')

    def test_explicit_fetch_uses_proxy_and_caches_only_language_fields(self):
        real_client = httpx.Client
        calls, options = [], []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=table('A01B1/04'))
        def make_client(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(handler))
        old_path, old_cache = translations._cache_path, translations._cache.copy()
        try:
            with tempfile.TemporaryDirectory() as temp, patch.object(translations.httpx, 'Client', side_effect=make_client):
                result = translations.fetch_translations(['A01B1/04'], {'proxy':'http://secret:token@proxy.local:8080'}, temp)
                self.assertEqual(result['missing'], [])
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0].startswith(translations.BASE))
                self.assertEqual(options[0]['proxy'], 'http://secret:token@proxy.local:8080')
                self.assertFalse(options[0]['trust_env'])
                self.assertFalse(options[0]['follow_redirects'])
                stored = (Path(temp) / 'ipc-japanese-captions.json').read_text(encoding='utf-8')
                self.assertNotIn('secret', stored)
                self.assertNotIn('token', json.dumps(result))
                translations.fetch_translations(['A01B1/04'], {}, temp)
                self.assertEqual(len(calls), 1)
        finally:
            translations._cache_path, translations._cache = old_path, old_cache

    def test_bad_input_never_fetches_and_provider_errors_are_redacted(self):
        with patch.object(translations.httpx, 'Client') as client:
            for value in ([], ['A01B1/04']*49, ['https://example.com'], ['H99Z999/99'], 'A01B1/04'):
                with self.assertRaises(ValueError):
                    translations.fetch_translations(value, {})
            client.assert_not_called()
        real_client = httpx.Client
        def handler(request):
            raise httpx.ConnectError('secret-proxy-password', request=request)
        with patch.object(translations.httpx, 'Client', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler))):
            result = translations.fetch_translations(['A01B1/04'], {})
        self.assertEqual(result['missing'], ['A01B1/04'])
        self.assertNotIn('secret-proxy-password', json.dumps(result))
        self.assertTrue(result['warnings'])

    def test_explicit_fetch_stops_at_four_tables(self):
        codes = ['A01B1/04', 'A01C1/00', 'A01D1/00', 'A01F1/00', 'A01G2/00']
        urls = {translations.BASE + translations.table_path(catalog.lookup('IPC', code)): code for code in codes}
        self.assertEqual(len(urls), 5)
        real_client, calls = httpx.Client, []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=table(urls[str(request.url)]))
        old_path, old_cache = translations._cache_path, translations._cache.copy()
        try:
            with tempfile.TemporaryDirectory() as temp, patch.object(translations.httpx, 'Client', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler))):
                result = translations.fetch_translations(codes, {}, temp)
                self.assertEqual(len(calls), 4)
                self.assertEqual(result['missing'], ['A01G2/00'])
                self.assertTrue(result['warnings'])
        finally:
            translations._cache_path, translations._cache = old_path, old_cache

    def test_invalid_proxy_errors_do_not_expose_configuration(self):
        with patch.object(translations.httpx, 'Client', side_effect=ValueError('secret-token')):
            with self.assertRaises(ValueError) as caught:
                translations.fetch_translations(['A01B1/04'], {})
        self.assertNotIn('secret-token', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
