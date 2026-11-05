"""Classification input regressions using public format examples and synthetic CSVs.

No live workspace, API or LLM is read or changed by these tests.
"""
import copy
import csv
import io
import unittest

from analysis_engine import parse_csv
import classification_bridge as bridge
import classification_explorer as explorer
from refinement_classifications import classification_feedback
import discovery_engine


class ClassificationImportAccuracyTests(unittest.TestCase):
    @staticmethod
    def csv_bytes(headers, values):
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerow(values)
        return stream.getvalue().encode('utf-8-sig')

    def test_ipcpub_symbols_and_printed_codes_share_normalization(self):
        self.assertEqual(explorer.normalize('IPC', 'G06F0016000000'), 'G06F16/00')
        self.assertEqual(explorer.parse_codes('IPC', 'G06F0016000000; G06F16/00; H01M0010056200'),
                         ['G06F16/00', 'H01M10/0562'])
        row = {'id': 'EXAMPLE', 'ipc': 'G06F0016000000; H01M0010056200'}
        before = copy.deepcopy(row)
        result = bridge.extract(row, strict=True)
        self.assertEqual({item['key'] for item in result['items']},
                         {'IPC:G06F16/00', 'IPC:H01M10/0562'})
        for item in result['items']:
            self.assertTrue(item['verified'])
            self.assertEqual(item['evidence_status'], 'patent_metadata')
            origin = item['classification_origins'][0]
            self.assertEqual((origin['type'], origin['field'], origin['raw']),
                             ('patent', 'ipc', before['ipc']))
        self.assertEqual(row, before)

    def test_square_and_round_edition_indicators_do_not_change_symbols(self):
        raw = 'Ｇ０６Ｆ１６／００［２０１９．０１］; B60W60/00 (2020.01)'
        self.assertEqual(explorer.parse_codes('IPC', raw), ['G06F16/00', 'B60W60/00'])
        result = bridge.extract({'id': 'EXAMPLE', 'ipc': raw}, strict=True)
        self.assertEqual(len(result['items']), 2)
        self.assertTrue(all(item['origins'][0]['raw'] == raw for item in result['items']))

    def test_malformed_symbols_are_not_partially_accepted(self):
        invalid = ['G06F001600000', 'G06F00160000000', 'G06F0016000000junk',
                   'junkG06F0016000000', 'G06F16/00,16/30', 'G06F16/00[unknown]',
                   'G06F16/00[2019.01)']
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    explorer.parse_codes('IPC', raw)
                result = bridge.extract({'id': 'EXAMPLE', 'ipc': raw})
                self.assertEqual(result['items'], [])
                self.assertTrue(result['errors'])

    def test_ipc_format_support_does_not_guess_cpc_or_fterm(self):
        with self.assertRaises(ValueError):
            explorer.parse_codes('CPC', 'G06F0016000000')
        with self.assertRaises(ValueError):
            explorer.parse_codes('F-term', 'G06F0016000000')
        self.assertEqual(explorer.parse_codes('F-term', '５Ｈ０２９ＡＭ１１'), ['5H029AM11'])
        result = bridge.extract({'cpc': 'G06F16/00'})
        self.assertEqual(result['items'][0]['kind'], 'CPC')
        self.assertFalse(result['items'][0]['verified'])

    def test_printed_fi_letter_and_expansion_suffixes_are_consumed(self):
        cases = [('G03F 1/92 A', 'G03F1/92'), ('H01L 21/30 531 M', 'H01L21/30'),
                 ('H01L21/30 531M', 'H01L21/30'), ('G03F1/92@A', 'G03F1/92'),
                 ('H01L21/30,531@M', 'H01L21/30')]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(bridge.parse_fi(raw), [{'base': expected, 'raw': raw}])
        row = {'id': 'EXAMPLE', 'ipc': '', 'fi': 'G03F 1/92 A'}
        before = copy.deepcopy(row)
        result = bridge.extract(row, strict=True)
        self.assertTrue(any(i['kind']=='FI' for i in result['items']))
        result['items'] = [i for i in result['items'] if i['kind']=='IPC']
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['evidence_status'], 'derived_candidate')
        self.assertEqual(item['origins'][0]['type'], 'fi_to_ipc')
        self.assertEqual(item['origins'][0]['raw'], row['fi'])
        self.assertEqual(row, before)

    def test_fi_suffix_never_consumes_next_symbol_section(self):
        cases = ['G03F1/92 G06F3/08', 'G03F1/92 A G06F3/08',
                 'G03F1/92 531 G06F3/08', 'G03F1/92 531 M G06F3/08']
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual([entry['base'] for entry in bridge.parse_fi(raw)],
                                 ['G03F1/92', 'G06F3/08'])
        for raw in ('G03F1/92 AB', 'G03F1/92 junk', 'G03F1/92A', 'G03F1/92 531 GARBAGE'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    bridge.parse_fi(raw)

    def test_fullwidth_csv_headers_retain_separate_classification_systems(self):
        headers = ['発明の名称', 'ＩＰＣ', 'ＦＩ', 'Ｆターム', 'ＣＰＣ']
        values = ['Synthetic example', 'G06F0016000000', 'G03F 1/92 A',
                  '5H029AM11', 'Y02T10/70']
        parsed = parse_csv(self.csv_bytes(headers, values))
        self.assertFalse(parsed['needs_mapping'])
        row = parsed['rows'][0]
        self.assertEqual([row[key] for key in ('ipc', 'fi', 'fterm', 'cpc')], values[1:])
        self.assertEqual(parsed['mapping']['ipc'], 'ＩＰＣ')
        result = bridge.extract(row, strict=True)
        direct_ipc = next(item for item in result['items'] if item['key'] == 'IPC:G06F16/00')
        self.assertEqual(direct_ipc['origins'][0]['field'], 'ipc')
        self.assertTrue(any(item['key'] == 'CPC:Y02T10/70' for item in result['items']))

    def test_manual_and_imported_fi_keep_the_same_complete_suffix(self):
        for raw in ('B01D 9/02 601 A', 'B01D9/02 601A', 'B01D9/02,601@A',
                    'Ｂ０１Ｄ９／０２　６０１Ａ (2026.04)'):
            with self.subTest(raw=raw):
                manual = explorer.parse_codes('FI', raw)
                result = bridge.extract({'fi': raw}, strict=True)
                self.assertEqual(manual, ['B01D9/02,601@A'])
                imported = next(item for item in result['items'] if item['kind'] == 'FI')
                self.assertEqual(imported['code'], manual[0])
                self.assertTrue(imported['verified'])
                ipc = next(item for item in result['items'] if item['kind'] == 'IPC')
                self.assertEqual(ipc['evidence_status'], 'derived_candidate')
                self.assertEqual(ipc['origins'][0]['method'], 'official_concordance')

    def test_manual_fi_keeps_legacy_suffixes_and_symbol_boundaries(self):
        for raw in ('G03F1/92A', 'G03F1/92 A', 'G03F1/92@A'):
            self.assertEqual(explorer.parse_codes('FI', raw), ['G03F1/92@A'])
        for raw in ('B01D9/02,601A', 'B01D9/02,601 A', 'B01D9/02,601@A'):
            self.assertEqual(explorer.parse_codes('FI', raw), ['B01D9/02,601@A'])
        self.assertEqual(explorer.parse_codes('FI', 'B01D9/02 601 A G03F1/92 A; B01D9/02,601@A'),
                         ['B01D9/02,601@A', 'G03F1/92@A'])
        for raw in ('G06F3/1', 'B01D9/02 601 AB', 'B01D9/02 601A garbage', 'G05D105:00'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                explorer.parse_codes('FI', raw)

    def test_invalid_fi_reports_the_field_without_losing_valid_ipc(self):
        for fi in ('G06F3/1', 'B01D9/02,601@A; G06F3/1'):
            with self.subTest(fi=fi):
                row = {'id': 'SYNTHETIC', 'ipc': 'G06F16/00', 'fi': fi,
                       'cpc': 'Y02T10/70', 'label': 'keep', 'label_source': 'human'}
                before = copy.deepcopy(row)
                result = bridge.extract(row)
                self.assertEqual({item['key'] for item in result['items']},
                                 {'IPC:G06F16/00', 'CPC:Y02T10/70'})
                self.assertTrue(any('FI' in error for error in result['errors']))
                self.assertTrue(any('FI' in warning for warning in result['warnings']))
                with self.assertRaisesRegex(ValueError, 'FI'):
                    bridge.extract(row, strict=True)
                for report, field in ((classification_feedback([row]), 'candidates'),
                                      (discovery_engine.analyze_patents({}, [row]), 'recommendations')):
                    item = next(item for item in report[field] if item['key'] == 'IPC:G06F16/00')
                    self.assertEqual(item['evidence_status'], 'patent_metadata')
                    self.assertEqual(item['derived_candidate_count'], 0)
                    self.assertTrue(any('FI' in warning for warning in report['warnings']))
                self.assertEqual(row, before)

    def test_parenthetical_ipc_header_and_explicit_mapping(self):
        for header in ('国際特許分類（ＩＰＣ）', 'ＩＰＣ（国際特許分類）', 'ＩＰＣ分類記号'):
            with self.subTest(header=header):
                parsed = parse_csv(self.csv_bytes(['title', header], ['Example', 'G06F16/00']))
                self.assertEqual(parsed['rows'][0]['ipc'], 'G06F16/00')
                self.assertEqual(parsed['mapping']['ipc'], header)
        raw = self.csv_bytes(['title', 'ＩＰＣ', 'chosen'], ['Example', 'G06F16/00', 'H01M10/00'])
        self.assertEqual(parse_csv(raw, mapping={'ipc': 'chosen'})['rows'][0]['ipc'], 'H01M10/00')

    def test_mixed_classification_header_is_not_guessed_as_ipc(self):
        raw = self.csv_bytes(['title', 'IPC/CPC', 'FI'], ['Example', 'Y02T10/70', 'G03F1/92 A'])
        row = parse_csv(raw)['rows'][0]
        self.assertEqual(row['ipc'], '')
        self.assertEqual(row['cpc'], '')
        self.assertEqual(row['fi'], 'G03F1/92 A')

    def test_oversize_classification_never_silently_truncates(self):
        for field in ('ipc', 'fi', 'fterm', 'cpc'):
            with self.subTest(field=field):
                raw = self.csv_bytes(['title', field], ['Example', 'G06F16/00;' * 1300])
                with self.assertRaisesRegex(ValueError, '12,000'):
                    parse_csv(raw)
                result = bridge.extract({field: ['G06F16/00'] * 1300})
                self.assertEqual(result['items'], [])
                self.assertTrue(any('12,000' in error for error in result['errors']))


if __name__ == '__main__':
    unittest.main()
