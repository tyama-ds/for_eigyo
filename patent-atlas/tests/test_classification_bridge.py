"""Offline FI/theme bridge and FI-only restoration; no live state or network."""
import copy
import unittest

from analysis_engine import backfill_fi, parse_csv
import classification_bridge as bridge
import classification_explorer as explorer
import discovery_engine
import fterm_catalog
from refinement_classifications import classification_feedback, feedback_origin


class ClassificationBridgeTests(unittest.TestCase):
    def row(self, **extra):
        return dict(dict(id='JP1', title='Vehicle control', abstract='', ipc='', fterm='', fi='',
                         label='keep', label_source='human', family='family-1'), **extra)

    def test_csv_keeps_fi_separate_and_backfill_preserves_all_judgment_fields(self):
        raw = 'id,title,abstract,FI\nJP1,Vehicle control,,"G08G1/16@A,G06T7/00,650@Z"\n'.encode()
        imported = parse_csv(raw)['rows']
        self.assertEqual(imported[0]['fi'], 'G08G1/16@A,G06T7/00,650@Z')
        self.assertEqual(imported[0]['ipc'], '')
        original = [self.row(score=.95, score_source='llm', label_reason='Human decision', llm_decision={'decision': 'keep'})]
        before = copy.deepcopy(original)
        restored, report = backfill_fi(original, imported)
        self.assertEqual(report['updated_count'], 1)
        self.assertEqual(restored[0]['fi'], imported[0]['fi'])
        restored[0]['fi'] = ''
        self.assertEqual(restored, original)
        self.assertEqual(original, before)

    def test_backfill_rejects_mismatched_or_ambiguous_records_and_nonempty_conflicts(self):
        cases = [([self.row()], [self.row(title='Different', fi='G08G1/16')]),
                 ([self.row()], [self.row(abstract='Different', fi='G08G1/16')]),
                 ([self.row(content_key='old')], [self.row(content_key='new', fi='G08G1/16')]),
                 ([self.row()], [self.row(fi='G08G1/16'), self.row(fi='G08G1/16')]),
                 ([self.row(fi='G06T7/00')], [self.row(fi='G08G1/16')])]
        for original, incoming in cases:
            with self.subTest(original=original, incoming=incoming):
                result, report = backfill_fi(original, incoming)
                self.assertEqual(result, original)
                self.assertEqual(report['updated_count'], 0)
                self.assertTrue(report['conflicts'])

    def test_fi_suffixes_are_consumed_without_claiming_assigned_ipc(self):
        row = self.row(fi='Ｇ０８Ｇ １／１６＠Ａ, G06T7/00,650@Z; G05D105:00')
        before = copy.deepcopy(row)
        result = bridge.extract(row)
        self.assertEqual({item['key'] for item in result['items']}, {'IPC:G08G1/16', 'IPC:G06T7/00'})
        self.assertTrue(all(item['evidence_status'] == 'derived_candidate' and item['verified'] for item in result['items']))
        self.assertTrue(any('索引記号' in warning for warning in result['warnings']))
        origin = result['items'][0]['classification_origins'][0]
        self.assertEqual(origin['type'], 'fi_to_ipc')
        self.assertIn('版差', origin['warning'])
        self.assertEqual(origin['ipc_version'], '2026.01')
        self.assertEqual(row, before)

    def test_malformed_fi_is_not_partially_recovered(self):
        row = self.row(fi='G08G1/16@A,garbage')
        result = bridge.extract(row)
        self.assertEqual(result['items'], [])
        self.assertTrue(result['errors'])
        with self.assertRaises(ValueError):
            bridge.extract(row, strict=True)

    def test_unknown_fi_base_uses_only_verified_subclass_with_warning(self):
        item = bridge.extract(self.row(fi='H04L9999/999999'))['items'][0]
        self.assertEqual(item['code'], 'H04L')
        self.assertEqual(item['evidence_status'], 'derived_candidate')
        self.assertEqual(item['origins'][0]['method'], 'subclass_fallback')
        self.assertIn('現行辞書にない', item['reason'])
        self.assertEqual(bridge.extract(self.row(fi='H98Z9999/999999'))['items'], [])

    def test_explicit_ipc_and_fi_candidate_merge_without_double_counting(self):
        row = self.row(ipc='G08G1/16', fi='G08G1/16@A')
        items = bridge.extract(row)['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['evidence_status'], 'patent_metadata')
        self.assertEqual({o['type'] for o in items[0]['origins']}, {'patent', 'fi_to_ipc'})
        feedback = classification_feedback([row])['candidates'][0]
        self.assertEqual((feedback['keep_count'], feedback['explicit_metadata_count'], feedback['derived_candidate_count']), (1, 1, 1))

    def test_all_theme_snapshot_supports_vehicle_and_battery_without_prefix_guessing(self):
        self.assertEqual(fterm_catalog.theme_metadata()['theme_count'], 3390)
        for term, expected in [('3D232AA01', 'B62D6/00'), ('5H181AA01', 'G08G'), ('5H029AM11', 'H01M10/00')]:
            with self.subTest(term=term):
                result = bridge.extract(self.row(fterm=term))
                ipc = next(item for item in result['items'] if item['kind'] == 'IPC')
                self.assertEqual(ipc['code'], expected)
                self.assertEqual(ipc['evidence_status'], 'derived_candidate')
                origin = ipc['origins'][0]
                self.assertEqual(origin['source_version'], '2026-06')
                self.assertTrue(origin['fi_range'])
                self.assertIn('個別Fターム', origin['warning'])
                self.assertEqual(origin['term_verified'], term == '5H029AM11')

    def test_unsupported_theme_is_visible_and_never_fabricates_ipc(self):
        result = bridge.extract(self.row(fterm='9Z999AA01'))
        self.assertFalse(any(item['kind'] == 'IPC' for item in result['items']))
        self.assertTrue(any('未収録' in warning for warning in result['warnings']))
        self.assertEqual(result['items'][0]['evidence_status'], 'unverified_metadata')

    def test_seed_lab_and_refinement_share_derived_candidate_provenance(self):
        patent = self.row(fi='G08G1/16@A')
        before = copy.deepcopy(patent)
        seed = explorer.seed_items({'patent_ids': ['JP1']}, [patent])[0]
        lab = discovery_engine.analyze_patents({'english_terms': ['vehicle']}, [patent])['recommendations'][0]
        feedback = classification_feedback([patent])['candidates'][0]
        for item in (seed, lab, feedback):
            self.assertEqual(item['key'], 'IPC:G08G1/16')
            self.assertEqual(item['evidence_status'], 'derived_candidate')
            self.assertEqual(item['classification_origins'][0]['type'], 'fi_to_ipc')
        self.assertEqual(lab['explicit_metadata_count'], 0)
        self.assertEqual(feedback['explicit_metadata_count'], 0)
        origin = feedback_origin(feedback)
        feedback['classification_origins'][0]['raw'] = 'changed'
        self.assertEqual(origin['classification_origins'][0]['raw'], 'G08G1/16@A')
        self.assertEqual(patent, before)

    def test_lab_ai_support_is_distinct_and_not_double_bonus_for_human_family(self):
        rows = [self.row(ipc='G08G1/16'), self.row(id='JP2', ipc='G08G1/16', label_source='agent'),
                self.row(id='JP3', ipc='G08G1/16', label_source='agent', family='family-2')]
        item = discovery_engine.analyze_patents({'english_terms': ['vehicle']}, rows)['recommendations'][0]
        self.assertEqual((item['positive_count'], item['ai_positive_count']), (1, 2))
        self.assertEqual((item['positive_family_count'], item['ai_positive_family_count']), (1, 2))
        self.assertEqual(item['score'], 2 + 1.5 + .75)
        self.assertEqual(item['score_weights']['agent_keep'], .75)
        self.assertEqual(item['evidence'][0]['label_source'], 'human')

    def test_unknown_ipc_is_not_official_lab_or_refinement_evidence(self):
        row = self.row(ipc='H99Z9999/999999')
        self.assertEqual(bridge.extract(row)['items'][0]['evidence_status'], 'unverified_metadata')
        self.assertEqual(classification_feedback([row])['candidates'], [])
        self.assertEqual(discovery_engine.analyze_patents({}, [row])['recommendations'], [])

    def test_candidate_merge_retains_both_explicit_and_derived_provenance(self):
        explicit = bridge.extract(self.row(ipc='G08G1/16'))['items']
        derived = bridge.extract(self.row(id='JP2', fi='G08G1/16@A'))['items']
        merged = explorer.merge_candidates(explicit, derived)[0]
        self.assertEqual(merged['evidence_status'], 'patent_metadata')
        self.assertEqual({origin['type'] for origin in merged['classification_origins']}, {'patent', 'fi_to_ipc'})
        self.assertEqual({origin['patent_id'] for origin in merged['classification_origins']}, {'JP1', 'JP2'})


if __name__ == '__main__':
    unittest.main()
