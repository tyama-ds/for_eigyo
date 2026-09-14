"""Model JSON must be validated before any workspace mutation."""
import copy
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_app as integration

module = integration.module


class LLMCandidateTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(module.app)
        module.JOB.update(status='idle')
        module.SETTINGS.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        self.assertEqual(self.client.post('/api/demo').status_code, 200)

    def test_wrong_candidate_shape_is_a_clear_400_and_preserves_workspace(self):
        invalid = [{}, {'candidates': None}, {'candidates': 'IPC'},
                   {'candidates': {}}, {'candidates': [None]},
                   {'candidates': [{'kind': 'IPC', 'code': 123}]},
                   {'candidates': [{'kind': 'IPC', 'code': 'B60L53/00', 'reason': {}}]}]
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        for response in invalid:
            with self.subTest(response=response), patch.object(module, 'complete', return_value=response):
                result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
            self.assertEqual(result.status_code, 400, result.text)
            self.assertIn('LLM', result.json()['detail'])
            self.assertEqual(module.STATE, before)
            self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_logged_codes_use_current_definitions_and_exclude_unconfirmed_ipc(self):
        notices = []
        rows = module.llm_candidate_rows({'candidates': [
            {'kind': 'IPC', 'code': 'B60L 53/00', 'title': '自動車の操縦装置', 'reason': '運転の仮説'},
            {'kind': 'IPC', 'code': 'H04L25/00', 'title': '自動車の制御装置', 'reason': '通信の仮説'},
            {'kind': 'IPC', 'code': 'H04L29/06', 'title': '車両の自動制御システム'},
            {'kind': 'IPC', 'code': 'G05L51/00', 'title': '車両の駐車装置'},
        ]}, notices)
        self.assertEqual([row['code'] for row in rows], ['B60L53/00', 'H04L25/00'])
        self.assertIn('charging batteries', rows[0]['title_official'])
        self.assertEqual(rows[1]['title_official'], 'Baseband systems')
        self.assertNotIn('操縦装置', rows[0]['title'])
        self.assertTrue(all(row['verified'] for row in rows))
        self.assertTrue(any('G05L51/00' in note and '2026.01' in note for note in notices))

    def test_valid_response_does_not_change_selection_or_patents_and_explains_rejections(self):
        before = copy.deepcopy(module.STATE)
        response = {'candidates': [{'kind': 'IPC', 'code': 'B60L53/00', 'reason': '充電の観点'},
                                   {'kind': 'IPC', 'code': 'G05L51/00', 'reason': '未確認'}]}
        with patch.object(module, 'complete', return_value=response):
            result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
        self.assertEqual(result.status_code, 200, result.text)
        for field in ('selected', 'patents', 'queries', 'training', 'discovery'):
            self.assertEqual(module.STATE[field], before[field], field)
        self.assertIn('G05L51/00', result.json()['classification_view']['note'])
        self.assertNotIn('G05L51/00', [row['code'] for row in result.json()['candidates']])

    def test_lm_studio_mixed_upper_codes_keeps_three_valid_candidates(self):
        before = copy.deepcopy(module.STATE)
        codes = ['B60L', 'G05D', 'H04N', 'B60L23', 'H04N21', 'G05L']
        response = {'candidates': [dict(kind='IPC', code=code, reason='ログの探索仮説')
                                   for code in codes]}
        with patch.object(module, 'complete', return_value=response):
            result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
        self.assertEqual(result.status_code, 200, result.text)
        state = result.json()
        report = state['candidate_proposal']
        self.assertEqual((report['returned_count'], report['accepted_count'], report['rejected_count']),
                         (6, 3, 3))
        self.assertEqual([row['code'] for row in report['items']], codes[:3])
        self.assertEqual([row['code'] for row in report['rejected']], codes[3:])
        rejected = {row['code']: row for row in report['rejected']}
        self.assertIn('不完全', rejected['B60L23']['reason'])
        self.assertIn('不完全', rejected['H04N21']['reason'])
        self.assertIn('収録IPC辞書', rejected['G05L']['reason'])
        for row in report['items']:
            self.assertTrue(row['verified'] and row['selectable'])
            self.assertEqual(row['selection_scope'], 'subtree')
            self.assertIn(row['key'], state['classification_view']['keys'])
        for field in ('selected', 'patents', 'queries', 'training', 'discovery'):
            self.assertEqual(module.STATE[field], before[field], field)

    def test_unambiguous_kind_labels_and_versions_are_normalized_and_recorded(self):
        cases = [
            (' ipc ', 'ＩＰＣ：Ｂ６０Ｗ　００３０／１８ （２０２６．０１）', 'IPC', 'B60W30/18'),
            ('Fterm', 'FTERM: 5h029 am12 (2026.06)', 'F-term', '5H029AM12'),
            ('F_TERM', 'F-term: 5H029AM12', 'F-term', '5H029AM12'),
            ('Ｆターム', 'Fターム: 5H029AM12', 'F-term', '5H029AM12'),
        ]
        for input_kind, input_code, kind, code in cases:
            with self.subTest(kind=input_kind, code=input_code):
                report, notices = {}, []
                rows = module.llm_candidate_rows({'candidates': [dict(kind=input_kind,
                    code=input_code, reason='表記ゆれのある提案')]}, notices, report)
                self.assertEqual([(row['kind'], row['code']) for row in rows], [(kind, code)])
                self.assertEqual(report['accepted_count'], 1)
                self.assertEqual(report['rejected_count'], 0)
                self.assertEqual(report['normalizations'], [dict(index=1, kind=kind, code=code,
                    input_kind=input_kind, input_code=input_code)])
                self.assertTrue(any(input_code in notice and code in notice for notice in notices))

    def test_malformed_rows_and_reason_do_not_discard_a_valid_row(self):
        before = copy.deepcopy(module.STATE)
        response = {'candidates': [None, False, dict(kind='IPC', code=123),
            dict(kind='IPC', code='B60L', reason={}), dict(kind=None, code='G05D'),
            dict(kind='IPC', code='H04N', reason='画像の観点')]}
        with patch.object(module, 'complete', return_value=response):
            result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
        self.assertEqual(result.status_code, 200, result.text)
        report = result.json()['candidate_proposal']
        self.assertEqual((report['returned_count'], report['accepted_count'], report['rejected_count']),
                         (6, 1, 5))
        self.assertEqual([row['code'] for row in report['items']], ['H04N'])
        self.assertEqual([row['index'] for row in report['rejected']], [1, 2, 3, 4, 5])
        self.assertTrue(all('文字列' in row['reason'] for row in report['rejected']))
        for field in ('selected', 'patents', 'queries', 'training', 'discovery'):
            self.assertEqual(module.STATE[field], before[field], field)

    def test_ambiguous_suffixes_and_subgroup_digits_are_never_repaired(self):
        invalid = [
            ('IPC', 'B60W30/18; G05D1/00'),
            ('IPC', 'B60W30/18, 100'),
            ('IPC', 'B60W30/18*'),
            ('F-term', '5H029AM12A'),
            ('IPC', 'CPC: B60W30/18'),
            ('IPC', 'B60W30/18 (2026.13)'),
            ('IPC', 'B60W30/18 (2026.1)'),
            ('IPC', 'H01M10/562'),
        ]
        report, notices = {}, []
        rows = module.llm_candidate_rows({'candidates': [
            dict(kind='IPC', code='H01M10/0562', reason='先頭ゼロを含む有効な小群'),
            *[dict(kind=kind, code=code, reason='補正禁止') for kind, code in invalid],
        ]}, notices, report)
        self.assertEqual([row['code'] for row in rows], ['H01M10/0562'])
        self.assertEqual(report['accepted_count'], 1)
        self.assertEqual(report['rejected_count'], len(invalid))
        self.assertEqual([(row['kind'], row['code']) for row in report['rejected']], invalid)
        self.assertIn('収録IPC辞書', report['rejected'][-1]['reason'])
        self.assertEqual(report['normalizations'], [])

    def test_all_invalid_rows_identify_the_codes_and_preserve_previous_report_and_disk(self):
        # Establish a successful previous report, so the failed request must
        # preserve its visible history as well as the selection and patents.
        with patch.object(module, 'complete', return_value={
                'candidates': [dict(kind='IPC', code='H04N', reason='前回の理由')]}):
            previous = self.client.post('/api/candidates', json={'keywords': '画像', 'use_llm': True})
        self.assertEqual(previous.status_code, 200, previous.text)
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        response = {'candidates': [dict(kind='IPC', code=code) for code in ['B60L23', 'H04N21', 'G05L']]}
        with patch.object(module, 'complete', return_value=response):
            result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
        self.assertEqual(result.status_code, 400, result.text)
        detail = result.json()['detail']
        for code in ('B60L23', 'H04N21', 'G05L'):
            self.assertIn(code, detail)
        self.assertIn('不完全', detail)
        self.assertIn('収録IPC辞書', detail)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_llm_upper_ipc_survives_overview_and_can_be_selected_for_initial_query(self):
        before = copy.deepcopy(module.STATE)
        with patch.object(module, 'complete', return_value={'candidates': [
                {'kind': 'IPC', 'code': 'H04L', 'reason': '通信の観点'}]}):
            response = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        self.assertIn('IPC:H04L', state['classification_view']['keys'])
        row = next(c for c in state['candidates'] if c['key'] == 'IPC:H04L')
        self.assertTrue(row['selectable'] and row['verified'])
        self.assertEqual(row['selection_scope'], 'subtree')
        self.assertEqual(state['selected'], before['selected'])
        response = self.client.post('/api/selection', json={'selected': ['IPC:H04L']})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post('/api/query', json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['queries'][-1]['classifications'][0]['code'], 'H04L')

    def test_all_unconfirmed_ipc_is_reported_without_overwriting_work(self):
        before = copy.deepcopy(module.STATE)
        with patch.object(module, 'complete', return_value={'candidates': [{'kind': 'IPC', 'code': 'G05L51/00'}]}):
            result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
        self.assertEqual(result.status_code, 400)
        self.assertIn('収録IPC辞書', result.json()['detail'])
        self.assertEqual(module.STATE, before)

    def test_unrecorded_fterm_is_explicitly_unverified_without_model_title(self):
        row = module.llm_candidate_rows({'candidates': [
            {'kind': 'F-term', 'code': '5H030AA01', 'title': 'モデルが作った定義', 'reason': '仮説'}]}, [])[0]
        self.assertFalse(row['verified'])
        self.assertEqual(row['title'], '5H030AA01')
        self.assertIn('未確認', row['reason'])

    def test_truncated_llm_json_error_leaves_all_state_and_file_unchanged(self):
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        with patch.object(module, 'complete', side_effect=ValueError('LLMのJSONが途中で終了しています。')):
            result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_false_string_cannot_accidentally_send_keywords_to_llm(self):
        with patch.object(module, 'complete') as complete:
            result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': 'false'})
        self.assertEqual(result.status_code, 400)
        complete.assert_not_called()

    def test_exact_llm_codes_reasons_and_duplicate_seed_are_visible_and_reported(self):
        before = copy.deepcopy(module.STATE)
        response = {'candidates': [
            {'kind': 'IPC', 'code': 'H01M10/0562', 'reason': '固体電解質というLLMの理由'},
            {'kind': 'IPC', 'code': 'B60W30/18', 'reason': '運転制御というLLMの理由'},
            {'kind': 'IPC', 'code': 'G05L51/00', 'reason': '未確認分類'},
            {'kind': 'IPC', 'code': 'B60W 30/18', 'reason': '重複'},
        ]}
        with patch.object(module, 'complete', return_value=response):
            result = self.client.post('/api/candidates', json={'keywords': '全固体電池', 'use_llm': True})
        self.assertEqual(result.status_code, 200, result.text)
        state = result.json()
        report = state['candidate_proposal']
        self.assertEqual((report['returned_count'], report['accepted_count'], report['new_count'],
                          report['existing_count'], report['rejected_count'], report['duplicate_count']),
                         (4, 2, 1, 1, 1, 1))
        self.assertEqual(report['rejected'][0]['code'], 'G05L51/00')
        for field in ('selected', 'queries', 'patents', 'training', 'discovery'):
            self.assertEqual(module.STATE[field], before[field])
        report_rows = {row['key']: row for row in report['items']}
        self.assertFalse(report_rows['IPC:H01M10/0562']['is_new'])
        self.assertTrue(report_rows['IPC:B60W30/18']['is_new'])
        for key, phrase in [('IPC:H01M10/0562', '固体電解質というLLMの理由'),
                            ('IPC:B60W30/18', '運転制御というLLMの理由')]:
            self.assertIn(key, state['classification_view']['keys'])
            row = next(row for row in state['candidates'] if row['key'] == key)
            self.assertIn(phrase, row['reason'])
            self.assertIn(phrase, row['llm_reason'])
            self.assertTrue(row['llm_proposed'])
            self.assertTrue(any(source['type'] == 'llm' and phrase in source['reason'] for source in row['recommendation_sources']))
        # Returning to the broad view keeps the latest exact proposals available.
        overview = self.client.post('/api/classifications/browse', json={'direction': 'overview'})
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertIn('IPC:B60W30/18', overview.json()['classification_view']['keys'])
        self.assertIn('IPC:H01M10/00', overview.json()['classification_view']['keys'])
        self.assertEqual(overview.json()['candidate_proposal'], report)

    def test_empty_llm_and_dictionary_refresh_report_what_was_used(self):
        with patch.object(module, 'complete', return_value={'candidates': []}):
            result = self.client.post('/api/candidates', json={'keywords': 'battery', 'use_llm': True})
        self.assertEqual(result.status_code, 200, result.text)
        report = result.json()['candidate_proposal']
        self.assertTrue(report['llm_used'])
        self.assertEqual(report['accepted_count'], 0)
        self.assertTrue(report['notices'])
        with patch.object(module, 'complete') as complete:
            result = self.client.post('/api/candidates', json={'keywords': 'battery', 'use_llm': False})
        self.assertEqual(result.status_code, 200, result.text)
        complete.assert_not_called()
        fresh = result.json()['candidate_proposal']
        self.assertNotEqual(fresh['id'], report['id'])
        self.assertFalse(fresh['llm_used'])
        self.assertEqual(fresh['items'], [])

    def test_waiting_for_model_does_not_lock_state_and_concurrent_changes_win(self):
        entered, release = threading.Event(), threading.Event()
        results = []

        def complete(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise ValueError('Test model timed out')
            return {'candidates': [{'kind': 'IPC', 'code': 'B60W30/18', 'reason': '仮説'}]}

        def request():
            results.append(self.client.post('/api/candidates', json={'keywords': 'autonomous vehicle', 'use_llm': True}))

        with patch.object(module, 'complete', side_effect=complete):
            worker = threading.Thread(target=request)
            worker.start()
            self.assertTrue(entered.wait(3))
            try:
                self.assertTrue(module.LOCK.acquire(timeout=.5), 'LLM requests must release the workspace lock')
                module.LOCK.release()
                self.assertEqual(self.client.get('/api/state').status_code, 200)
                change = self.client.post('/api/selection', json={'selected': []})
                self.assertEqual(change.status_code, 200, change.text)
                current = copy.deepcopy(module.STATE)
                disk = (module.DATA / 'workspace.json').read_bytes()
            finally:
                release.set()
                worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].status_code, 409, results[0].text)
        self.assertEqual(module.STATE, current)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_save_failure_keeps_previous_report_and_workspace(self):
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        with patch.object(module, 'complete', return_value={'candidates': [{'kind': 'IPC', 'code': 'H04L', 'reason': '通信'}]}), \
                patch.object(module, 'save', side_effect=ValueError('保存失敗')):
            result = self.client.post('/api/candidates', json={'keywords': 'communications', 'use_llm': True})
        self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)


if __name__ == '__main__':
    unittest.main()
