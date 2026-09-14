"""Classification API checks sharing test_app's temporary workspace only."""
import copy
import csv
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import test_app as integration
from fastapi.testclient import TestClient

module = integration.module


class ClassificationExplorerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def setUp(self):
        module.JOB.update(status='idle')
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        response = self.client.post('/api/demo')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(module.DATA, Path(integration.TEMP.name))

    def post_ok(self, path, body):
        response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_rejected_atomically(self, path, body, status=400):
        before = copy.deepcopy(module.STATE)
        saved_before = (module.DATA / 'workspace.json').read_bytes()
        response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), saved_before)

    def visible(self, state):
        by_key = {c['key']: c for c in state['candidates']}
        return [by_key[k] for k in state['classification_view']['keys']]

    def upload_targets(self, ipc='H01M 10/0562 (2010.01);\nH01M 10/0565', fterm='５Ｈ０２９ＡＭ１１、5H029AJ06'):
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=['id', 'title', 'abstract', 'ipc', 'fterm'])
        writer.writeheader()
        writer.writerows([
            dict(id='TARGET-1', title='Solid electrolyte target', abstract='Interface material', ipc=ipc, fterm=fterm),
            dict(id='TARGET-2', title='Camera target', abstract='Optical device', ipc='', fterm=''),
        ])
        response = self.client.post('/api/upload', content=stream.getvalue().encode('utf-8'), headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_keyword_overview_starts_at_main_groups_and_keeps_selection(self):
        before = self.client.get('/api/state').json()
        state = self.post_ok('/api/candidates', {'keywords': 'battery', 'use_llm': False})
        ipc = [c for c in self.visible(state) if c['kind'] == 'IPC']
        self.assertTrue(ipc)
        self.assertTrue(all(c['code'].endswith('/00') for c in ipc))
        self.assertTrue(all(c['selectable'] for c in ipc))
        self.assertEqual(state['selected'], before['selected'])
        self.assertEqual(state['queries'], before['queries'])

    def test_browsing_uses_official_parent_and_preserves_query_selection(self):
        before = self.client.get('/api/state').json()
        state = self.post_ok('/api/classifications/browse', {'kind': 'IPC', 'code': 'H01M10/0562', 'direction': 'broader'})
        rows = self.visible(state)
        self.assertIn('H01M10/0561', [c['code'] for c in rows])
        self.assertEqual(state['classification_view']['trail'][-1]['code'], 'H01M10/0561')
        self.assertEqual(state['classification_view']['focus']['code'], 'H01M10/0561')
        state = self.post_ok('/api/classifications/browse', {'kind':'IPC','code':state['classification_view']['focus']['code'],'direction':'broader'})
        self.assertEqual(state['classification_view']['focus']['code'], 'H01M10/056')
        state = self.post_ok('/api/classifications/browse', {'kind': 'IPC', 'code': 'H01M10/0561', 'direction': 'children'})
        rows = self.visible(state)
        self.assertIn('H01M10/0562', [c['code'] for c in rows])
        self.assertTrue(all(c['parent'] == 'H01M10/0561' for c in rows))
        self.assertEqual(state['selected'], before['selected'])
        self.assertEqual(state['queries'], before['queries'])
        self.assertEqual(state['patents'], before['patents'])

    def test_paging_keeps_selected_nodes_outside_current_page(self):
        before = self.client.get('/api/state').json()
        first = self.post_ok('/api/classifications/browse', {'kind': 'IPC', 'code': 'A01B', 'direction': 'children'})
        self.assertEqual(len(self.visible(first)), 24)
        self.assertIsNotNone(first['classification_view']['next_offset'])
        second = self.post_ok('/api/classifications/browse', {'kind': 'IPC', 'code': 'A01B', 'direction': 'children', 'offset': first['classification_view']['next_offset']})
        first_keys, second_keys = set(first['classification_view']['keys']), set(second['classification_view']['keys'])
        self.assertFalse(first_keys & second_keys)
        self.assertEqual(len(first_keys | second_keys), second['classification_view']['total'])
        self.assertEqual(second['selected'], before['selected'])
        self.assertTrue(set(second['selected']) <= {c['key'] for c in second['candidates']})

    def test_upper_ipc_is_selectable_but_fterm_containers_are_navigation_only(self):
        state = self.post_ok('/api/classifications/browse', {'kind': 'IPC', 'direction': 'roots'})
        self.assertEqual([c['code'] for c in self.visible(state)], list('ABCDEFGH'))
        self.assertTrue(all(c['selectable'] and c['selection_scope'] == 'subtree' for c in self.visible(state)))
        self.post_ok('/api/keywords', {'keywords': ''})
        for ipc in ('H', 'H04', 'H04L'):
            with self.subTest(code=ipc):
                self.post_ok('/api/classifications/seeds', {'ipc_text': ipc})
                self.post_ok('/api/selection', {'selected': ['IPC:' + ipc]})
                state = self.post_ok('/api/query', {})
                query = state['queries'][-1]
                self.assertEqual(query['expression'], f'(IPC="{ipc}")')
                self.assertEqual(query['classifications'][0]['selection_scope'], 'subtree')
                self.assertTrue(query['classifications'][0]['verified'])
                exported = self.post_ok('/api/query/export', {'query_id': query['id'], 'format': 'patentscope'})
                self.assertTrue(exported['can_copy'], exported['problems'])
                self.assertIn(ipc, exported['expression'])
        state = self.post_ok('/api/classifications/browse', {'kind': 'F-term', 'code': '5H029', 'direction': 'children'})
        self.assertTrue(self.visible(state))
        self.assertTrue(all(c['node_type'] == 'aspect' and not c['selectable'] for c in self.visible(state)))
        self.assert_rejected_atomically('/api/selection', {'selected': ['F-term:5H029AJ00']})
        state = self.post_ok('/api/classifications/browse', {'kind': 'F-term', 'code': '5H029AJ00', 'direction': 'children'})
        self.assertIn('F-term:5H029AJ01', state['classification_view']['keys'])
        self.assertTrue(next(c for c in self.visible(state) if c['code'] == '5H029AJ01')['selectable'])

    def test_direct_codes_normalize_formatting_and_preserve_original_text(self):
        ipc_raw = 'Ｈ０１Ｍ ０１０／０５６２ (2010.01); H01M10/0562、H01M 10/00'
        fterm_raw = '５Ｈ０２９ ＡＭ１１;5H029AJ06'
        with patch.object(module, 'complete', side_effect=AssertionError('Direct codes must not call an LLM')):
            state = self.post_ok('/api/classifications/seeds', {'ipc_text': ipc_raw, 'fterm_text': fterm_raw, 'label': 'Known example'})
        rows = self.visible(state)
        self.assertEqual({c['key'] for c in rows}, {'IPC:H01M10/0562', 'IPC:H01M10/00', 'F-term:5H029AM11', 'F-term:5H029AJ06'})
        for row in rows:
            origin = next(o for o in row['origins'] if o['type'] == 'manual')
            self.assertEqual(origin['raw'], ipc_raw if row['kind'] == 'IPC' else fterm_raw)
            self.assertEqual(origin['label'], 'Known example')
        again = self.post_ok('/api/classifications/seeds', {'ipc_text': ipc_raw, 'fterm_text': fterm_raw, 'label': 'Known example'})
        for row in self.visible(again):
            self.assertEqual(len(row['origins']), 1)

    def test_invalid_seed_input_is_rejected_without_partial_changes(self):
        invalid = [
            {}, {'patent_ids': ['not-imported']}, {'patent_ids': 'TARGET-1'}, {'patent_ids': [3]},
            {'patent_ids': [module.STATE['patents'][0]['id']] * 31},
            {'ipc_text': 123}, {'ipc_text': 'H01M10/0562,300'},
            {'ipc_text': 'H01M10/0562/H01M10/0565'},
            {'ipc_text': 'H01M10/0562', 'fterm_text': '5H029AM11,AA01'},
            {'ipc_text': 'H01M10/0562', 'select': 'false'},
            {'ipc_text': 'H01M10/0562 (2010.01) trailing'},
            {'ipc_text': ';'.join(f'H01M{i}/00' for i in range(1, 102))},
        ]
        for body in invalid:
            with self.subTest(body=body):
                self.assert_rejected_atomically('/api/classifications/seeds', body)

    def test_seed_select_is_explicit_and_skips_navigation_containers(self):
        self.post_ok('/api/selection', {'selected': []})
        state = self.post_ok('/api/classifications/seeds', {'ipc_text': 'H01M;H01M10/00', 'fterm_text': '5H029;5H029AJ00;5H029AJ06'})
        self.assertEqual(state['selected'], [])
        state = self.post_ok('/api/classifications/seeds', {'ipc_text': 'H01M;H01M10/00', 'fterm_text': '5H029;5H029AJ00;5H029AJ06', 'select': True})
        self.assertEqual(set(state['selected']), {'IPC:H01M', 'IPC:H01M10/00', 'F-term:5H029AJ06'})

    def test_unknown_ipc_broader_recovers_verified_prefix_without_remapping(self):
        for original, expected in [('H04L9999/999999', 'H04L'), ('H04Z9999/999999', 'H04'), ('H98Z9999/999999', 'H')]:
            with self.subTest(code=original):
                self.post_ok('/api/classifications/seeds', {'ipc_text': original, 'select': True})
                before = copy.deepcopy(module.STATE)
                state = self.post_ok('/api/classifications/browse', {'kind': 'IPC', 'code': original, 'direction': 'broader'})
                focus = state['classification_view']['focus']
                self.assertEqual(focus['code'], expected)
                self.assertTrue(focus['verified'] and focus['selectable'])
                self.assertIn(original, state['classification_view']['note'])
                self.assertIn('正式な親子関係や旧版との対応は未確認', state['classification_view']['note'])
                for field in ('selected', 'queries', 'patents'):
                    self.assertEqual(state[field], before[field])
                unknown = next(c for c in state['candidates'] if c['key'] == 'IPC:' + original)
                self.assertFalse(unknown['verified'])
                self.assertIsNone(unknown.get('parent'))
                self.post_ok('/api/selection', {'selected': ['IPC:' + expected]})
        # CPC must never use the IPC dictionary as its parent tree.
        state = self.post_ok('/api/classifications/browse', {'kind': 'CPC', 'code': 'H04L9999/999999', 'direction': 'broader'})
        self.assertEqual(state['classification_view']['focus']['code'], 'H04L9999/999999')
        self.assertFalse(state['classification_view']['focus']['verified'])

    def test_unknown_upper_codes_remain_unverified_and_unselectable(self):
        state = self.post_ok('/api/classifications/seeds', {'ipc_text': 'H98Z', 'cpc_text': 'H04L'})
        self.assertTrue(all(not c['selectable'] and not c['verified'] for c in self.visible(state)))
        for key in ('IPC:H98Z', 'CPC:H04L'):
            self.assert_rejected_atomically('/api/selection', {'selected': [key]})

    def test_saved_upper_candidate_selection_rules_are_refreshed_without_rewriting_evidence(self):
        self.post_ok('/api/classifications/seeds', {'ipc_text': 'H04L'})
        item = next(c for c in module.STATE['candidates'] if c['key'] == 'IPC:H04L')
        item.update(selectable=False, reason='Saved evidence', support_count=7)
        saved_item = copy.deepcopy(item)
        module.STATE['discovery']['plan'] = {'candidate_hypotheses': [copy.deepcopy(item)]}
        state = self.client.get('/api/state').json()
        current = next(c for c in state['candidates'] if c['key'] == item['key'])
        lab = state['discovery']['plan']['candidate_hypotheses'][0]
        for row in (current, lab):
            self.assertTrue(row['selectable'])
            self.assertEqual(row['selection_scope'], 'subtree')
            self.assertEqual(row['reason'], 'Saved evidence')
            self.assertEqual(row['support_count'], 7)
        self.assertEqual(item, saved_item)
        self.post_ok('/api/selection', {'selected': ['IPC:H04L']})

    def test_ipc_and_cpc_with_same_code_do_not_share_identity_or_verification(self):
        state = self.post_ok('/api/classifications/seeds', {'ipc_text': 'H01M10/0562', 'cpc_text': 'H01M10/0562'})
        matches = {c['kind']: c for c in self.visible(state)}
        self.assertEqual(set(matches), {'IPC', 'CPC'})
        self.assertTrue(matches['IPC']['verified'])
        self.assertFalse(matches['CPC']['verified'])
        self.assert_rejected_atomically('/api/selection', {'selected': ['H01M10/0562']})
        self.post_ok('/api/selection', {'selected': ['CPC:H01M10/0562']})
        state = self.post_ok('/api/query', {})
        self.assertEqual([c['kind'] for c in state['queries'][-1]['classifications']], ['CPC'])
        state = self.post_ok('/api/classifications/browse', {'kind': 'CPC', 'code': 'H01M10/0562', 'direction': 'broader'})
        self.assertTrue(all(c['kind'] == 'CPC' for c in self.visible(state)))
        self.assertEqual(state['classification_view']['trail'], [{'code': 'H01M10/0562', 'title': 'H01M10/0562', 'kind': 'CPC'}])

    def test_unlisted_classification_is_preserved_as_unverified(self):
        state = self.post_ok('/api/classifications/seeds', {'ipc_text': 'H99Z9999/999999'})
        row = self.visible(state)[0]
        self.assertEqual(row['code'], 'H99Z9999/999999')
        self.assertFalse(row['verified'])
        self.assertTrue(row['selectable'])

    def test_target_seed_retains_raw_classifications_labels_and_history(self):
        ipc_raw = 'H01M 10/0562 (2010.01);\nH01M 10/0565'
        fterm_raw = '５Ｈ０２９ＡＭ１１、5H029AJ06'
        self.upload_targets(ipc_raw, fterm_raw)
        self.post_ok('/api/labels', {'ids': ['TARGET-1'], 'label': 'keep'})
        self.post_ok('/api/labels', {'ids': ['TARGET-2'], 'label': 'exclude'})
        module.STATE['training'] = {'method': 'existing model'}
        module.STATE['patents'][0]['score'] = 0.9
        module.save()
        before = copy.deepcopy(module.STATE)
        state = self.post_ok('/api/classifications/seeds', {'patent_ids': ['TARGET-1', 'TARGET-1']})
        self.assertEqual({c['key'] for c in self.visible(state)}, {'IPC:H01M10/0562', 'IPC:H01M10/0565', 'F-term:5H029AM11', 'F-term:5H029AJ06', 'IPC:H01M10/00'})
        for row in self.visible(state):
            if row['key'] == 'IPC:H01M10/00':
                self.assertEqual(row['evidence_status'], 'derived_candidate')
                self.assertTrue(all(o['type'] == 'fterm_theme_to_ipc' for o in row['origins']))
                continue
            origins = [o for o in row['origins'] if o['type'] == 'patent']
            self.assertEqual(len(origins), 1)
            self.assertEqual(origins[0]['patent_id'], 'TARGET-1')
            self.assertEqual(origins[0]['raw'], ipc_raw if row['kind'] == 'IPC' else fterm_raw)
            self.assertEqual(origins[0]['label'], 'Solid electrolyte target')
        for field in ('patents', 'training', 'queries', 'selected', 'keywords'):
            self.assertEqual(module.STATE[field], before[field], field)

    def test_target_seed_rejects_unparsed_csv_suffix_and_missing_classifications(self):
        self.upload_targets(ipc='H01M10/0562,300', fterm='5H029AJ06')
        self.assert_rejected_atomically('/api/classifications/seeds', {'ipc_text': 'H01M10/00', 'patent_ids': ['TARGET-1']})
        self.assert_rejected_atomically('/api/classifications/seeds', {'patent_ids': ['TARGET-2']})

    def test_seed_provenance_and_selection_survive_keyword_and_view_changes(self):
        self.post_ok('/api/selection', {'selected': []})
        self.post_ok('/api/classifications/seeds', {'ipc_text': 'H01M10/0562', 'label': 'Keep this seed', 'select': True})
        self.post_ok('/api/candidates', {'keywords': 'camera', 'use_llm': False})
        self.post_ok('/api/classifications/browse', {'kind': 'IPC', 'direction': 'roots'})
        state = self.post_ok('/api/classifications/browse', {'direction': 'seeds'})
        self.assertEqual(state['selected'], ['IPC:H01M10/0562'])
        seed = next(c for c in self.visible(state) if c['key'] == 'IPC:H01M10/0562')
        self.assertTrue(any(o['label'] == 'Keep this seed' for o in seed['origins']))

    def test_saved_query_has_detached_classification_and_origin_snapshot(self):
        self.post_ok('/api/selection', {'selected': []})
        self.post_ok('/api/classifications/seeds', {'ipc_text': 'H01M10/0562', 'label': 'Original evidence', 'select': True})
        state = self.post_ok('/api/query', {})
        expected = copy.deepcopy(state['queries'][-1])
        candidate = next(c for c in module.STATE['candidates'] if c['kind'] == 'IPC' and c['code'] == 'H01M10/0562')
        candidate['title'] = 'Changed current candidate'
        candidate['origins'][0]['label'] = 'Changed current evidence'
        self.assertEqual(module.STATE['queries'][-1], expected)
        self.post_ok('/api/classifications/seeds', {'ipc_text': 'H01M10/0562', 'label': 'New evidence'})
        self.assertEqual(module.STATE['queries'][-1], expected)

    def test_classification_only_query_can_be_created_and_exported(self):
        self.post_ok('/api/keywords', {'keywords': ''})
        self.post_ok('/api/selection', {'selected': []})
        self.post_ok('/api/classifications/seeds', {'ipc_text': 'H01M10/00', 'select': True})
        state = self.post_ok('/api/query', {})
        query = state['queries'][-1]
        self.assertEqual(query['keywords'], [])
        self.assertEqual(query['expression'], '(IPC="H01M10/00")')
        exported = self.post_ok('/api/query/export', {'query_id': query['id'], 'format': 'jplatpat', 'replacements': {}})
        self.assertTrue(exported['can_copy'])
        self.assertEqual(exported['expression'], 'H01M10/00/IP')
        self.post_ok('/api/selection', {'selected': []})
        self.assert_rejected_atomically('/api/query', {})
        self.assert_rejected_atomically('/api/query', {'refine': True, 'include': ['battery'], 'exclude': []})

    def test_invalid_browse_and_busy_mutations_preserve_state(self):
        for body in ({'kind': 'FI'}, {'direction': 'unknown'}, {'direction': 'children', 'code': 'invalid'}, {'direction': 'children', 'offset': -1}):
            with self.subTest(body=body):
                self.assert_rejected_atomically('/api/classifications/browse', body)
        module.JOB['status'] = 'running'
        try:
            for path, body in (('/api/classifications/browse', {'kind': 'IPC', 'direction': 'roots'}), ('/api/classifications/seeds', {'ipc_text': 'H01M10/00'}), ('/api/keywords', {'keywords': 'new topic'})):
                with self.subTest(path=path):
                    self.assert_rejected_atomically(path, body, status=409)
        finally:
            module.JOB['status'] = 'idle'

    def test_keyword_overview_capacity_failure_is_atomic(self):
        # Adding the child fits the cap; adding its overview main group does not.
        module.STATE['selected'] = []
        module.STATE['candidates'] = [
            dict(kind='IPC', code=f'H99Z{i}/00', title=f'Saved seed {i}', verified=False,
                 selectable=True, origins=[dict(type='manual', label='Saved recommendation')])
            for i in range(1, 1500)
        ]
        module.save()
        child = module.explorer.enrich(dict(kind='IPC', code='H01M10/0562'))
        with patch.object(module, 'candidate_list', return_value=[child]):
            self.assert_rejected_atomically('/api/candidates', {'keywords': 'battery', 'use_llm': False})


if __name__ == '__main__':
    unittest.main()
