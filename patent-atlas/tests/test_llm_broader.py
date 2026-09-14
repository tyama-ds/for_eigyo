"""A rejected model code may offer a reviewed wider scope, never an automatic repair."""
import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_app as integration

module = integration.module


class LLMBroaderCandidateTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(module.app)
        module.JOB.update(status='idle')
        module.SETTINGS.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        self.assertEqual(self.client.post('/api/demo').status_code, 200)

    def logged_proposal(self):
        codes = ['B60L23', 'H04N21', 'G05D1/02', 'B60L33/00', 'H04N7/00', 'G05D1/00']
        response = {'candidates': [dict(kind='IPC', code=code, reason=f'検証用の仮説: {code}')
                                   for code in codes]}
        with patch.object(module, 'complete', return_value=response):
            result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['candidate_proposal']

    def assert_unchanged(self, before, disk):
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_logged_response_separates_rejected_symbols_from_verified_broader_choices(self):
        report = self.logged_proposal()
        self.assertEqual((report['returned_count'], report['accepted_count'], report['rejected_count']),
                         (6, 2, 4))
        self.assertEqual([row['code'] for row in report['items']], ['H04N7/00', 'G05D1/00'])
        rejected = report['rejected']
        self.assertEqual([row['code'] for row in rejected], ['B60L23', 'H04N21', 'G05D1/02', 'B60L33/00'])
        self.assertEqual([row['broader_candidate']['code'] for row in rejected],
                         ['B60L', 'H04N', 'G05D1/00', 'B60L'])
        self.assertEqual([row['index'] for row in rejected], [1, 2, 3, 4])
        for row in rejected:
            self.assertTrue(row['broader_candidate']['verified'])
            self.assertTrue(row['broader_candidate']['selectable'])
            self.assertNotIn(row['code'], [item['code'] for item in module.STATE['candidates']])
            self.assertFalse(row.get('broader_added', False))
        retired = rejected[2]
        self.assertEqual(retired['revision']['status'], 'retired')
        self.assertEqual(retired['revision']['last_verified_version'], '2023.01')
        self.assertEqual(retired['revision']['changed_version'], '2024.01')
        self.assertIn('1対1の置換ではない', retired['reason'])
        self.assertTrue(retired['revision']['sources'])

    def test_public_state_projects_old_report_without_mutating_workspace_or_disk(self):
        report = self.logged_proposal()
        legacy = copy.deepcopy(module.STATE['candidate_proposal'])
        for row in legacy['rejected']:
            for field in ('broader_candidate', 'revision', 'index'):
                row.pop(field, None)
            row['reason'] = '旧レポートの未確認理由'
        legacy['notices'] = ['LLM提案6件のうち4件を除外しました。旧理由の繰り返し', '名称は公式名称を利用します。']
        module.STATE['candidate_proposal'] = legacy
        module.save()
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        result = self.client.get('/api/state')
        self.assertEqual(result.status_code, 200, result.text)
        projected = result.json()['candidate_proposal']
        self.assertEqual(projected['id'], report['id'])
        self.assertEqual([row['index'] for row in projected['rejected']], [1, 2, 3, 4])
        self.assertEqual(projected['rejected'][2]['revision']['status'], 'retired')
        self.assertNotIn('旧理由の繰り返し', ' '.join(projected['notices']))
        self.assertIn('名称は公式名称を利用します。', projected['notices'])
        self.assert_unchanged(before, disk)
        projected['rejected'][0]['broader_candidate']['code'] = 'modified'
        self.assert_unchanged(before, disk)

    def test_explicit_broader_add_preserves_labels_query_plan_and_llm_reason(self):
        report = self.logged_proposal()
        before = copy.deepcopy(module.STATE)
        prior_reason = next(row for row in before['candidates'] if row['code'] == 'G05D1/00')['reason']
        with patch.object(module, 'complete') as complete:
            result = self.client.post('/api/candidates/broader', json={
                'proposal_id': report['id'], 'rejected_index': 3})
        self.assertEqual(result.status_code, 200, result.text)
        complete.assert_not_called()
        for field in ('keywords', 'selected', 'patents', 'queries', 'training', 'discovery'):
            self.assertEqual(module.STATE[field], before[field], field)
        row = next(row for row in module.STATE['candidates'] if row['code'] == 'G05D1/00')
        self.assertEqual(row['reason'], prior_reason)
        self.assertIn('検証用の仮説: G05D1/00', row['llm_reason'])
        self.assertTrue(any(origin['type'] == 'reviewed_broader' and origin['from_code'] == 'G05D1/02'
                            and origin['proposal_id'] == report['id'] for origin in row['origins']))
        self.assertEqual(module.STATE['classification_view']['keys'], ['IPC:G05D1/00'])
        self.assertTrue(module.STATE['candidate_proposal']['rejected'][2]['broader_added'])
        self.assertNotIn('G05D1/02', [row['code'] for row in module.STATE['candidates']])
        self.assertEqual(module.STATE['candidate_proposal']['accepted_count'], 2)
        self.assertEqual(module.STATE['candidate_proposal']['rejected_count'], 4)
        again = self.client.post('/api/candidates/broader', json={
            'proposal_id': report['id'], 'rejected_index': 3})
        self.assertEqual(again.status_code, 200, again.text)
        repeated = next(row for row in module.STATE['candidates'] if row['code'] == 'G05D1/00')
        self.assertEqual(repeated['origins'], row['origins'])

    def test_broader_add_can_add_a_new_verified_scope_without_selecting_it(self):
        report = self.logged_proposal()
        module.STATE['candidates'] = [row for row in module.STATE['candidates'] if row['code'] != 'B60L']
        module.STATE['selected'] = [key for key in module.STATE['selected'] if key != 'IPC:B60L']
        module.save()
        before = copy.deepcopy(module.STATE)
        result = self.client.post('/api/candidates/broader', json={
            'proposal_id': report['id'], 'rejected_index': 1})
        self.assertEqual(result.status_code, 200, result.text)
        row = next(row for row in module.STATE['candidates'] if row['code'] == 'B60L')
        self.assertTrue(row['verified'] and row['selectable'])
        self.assertEqual(module.STATE['classification_view']['keys'], ['IPC:B60L'])
        for field in ('selected', 'patents', 'queries', 'training', 'discovery'):
            self.assertEqual(module.STATE[field], before[field], field)
        self.assertNotIn('IPC:B60L', module.STATE['selected'])
        self.assertNotIn('B60L23', [row['code'] for row in module.STATE['candidates']])

    def test_stale_proposal_or_changed_theme_is_409_and_keeps_data(self):
        report = self.logged_proposal()
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        for proposal_id in (None, '', 'old-report', 123):
            with self.subTest(proposal_id=proposal_id):
                result = self.client.post('/api/candidates/broader', json={
                    'proposal_id': proposal_id, 'rejected_index': 1})
                self.assertEqual(result.status_code, 409, result.text)
                self.assert_unchanged(before, disk)
        module.STATE['keywords'] = '全固体電池'
        module.save()
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        result = self.client.post('/api/candidates/broader', json={
            'proposal_id': report['id'], 'rejected_index': 1})
        self.assertEqual(result.status_code, 409, result.text)
        self.assert_unchanged(before, disk)

    def test_invalid_or_non_rejected_index_is_400_and_keeps_data(self):
        report = self.logged_proposal()
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        for index in (None, True, False, '1', 1.0, [], {}, 0, -1, 5, 6, 99):
            with self.subTest(index=index):
                result = self.client.post('/api/candidates/broader', json={
                    'proposal_id': report['id'], 'rejected_index': index})
                self.assertEqual(result.status_code, 400, result.text)
                self.assert_unchanged(before, disk)

    def test_original_response_index_is_used_instead_of_rejected_list_position(self):
        response = {'candidates': [dict(kind='IPC', code='H04N7/00', reason='画像'),
                                   dict(kind='IPC', code='B60L23', reason='不完全') ]}
        with patch.object(module, 'complete', return_value=response):
            result = self.client.post('/api/candidates', json={'keywords': '自動運転', 'use_llm': True})
        self.assertEqual(result.status_code, 200, result.text)
        report = result.json()['candidate_proposal']
        self.assertEqual(report['rejected'][0]['index'], 2)
        bad = self.client.post('/api/candidates/broader', json={'proposal_id': report['id'], 'rejected_index': 1})
        self.assertEqual(bad.status_code, 400, bad.text)
        good = self.client.post('/api/candidates/broader', json={'proposal_id': report['id'], 'rejected_index': 2})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(good.json()['classification_view']['keys'], ['IPC:B60L'])

    def test_save_failure_rolls_back_candidate_origin_display_and_report(self):
        report = self.logged_proposal()
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        with patch.object(module, 'save', side_effect=ValueError('保存失敗')):
            result = self.client.post('/api/candidates/broader', json={
                'proposal_id': report['id'], 'rejected_index': 3})
        self.assertEqual(result.status_code, 400, result.text)
        self.assertIn('保存失敗', result.json()['detail'])
        self.assert_unchanged(before, disk)

    def test_unsafe_suffix_other_system_or_missing_subclass_has_no_fallback(self):
        cases = [('IPC', 'B60L23*'), ('IPC', 'B60L23@A'), ('IPC', 'B60L23/00,100'),
                 ('IPC', 'B60L23; H04N21'), ('IPC', 'B60L23 (2026.13)'),
                 ('IPC', 'CPC: B60L23'), ('CPC', 'G05D1/02'), ('F-term', 'G05D1/02'),
                 ('F-term', '5H029AM12A'), ('IPC', 'G05L51/00'), ('IPC', 'Z99Z1/02')]
        for kind, code in cases:
            with self.subTest(kind=kind, code=code):
                original = dict(kind=kind, code=code, reason='元の除外理由')
                before = copy.deepcopy(original)
                result = module.rejected_candidate_guidance(original)
                self.assertNotIn('broader_candidate', result)
                self.assertNotIn('revision', result)
                self.assertEqual(original, before)
        report = self.logged_proposal()
        module.STATE['candidate_proposal']['rejected'].append(dict(index=7, kind='IPC', code='G05L51/00', reason='未確認'))
        module.save()
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        result = self.client.post('/api/candidates/broader', json={
            'proposal_id': report['id'], 'rejected_index': 7})
        self.assertEqual(result.status_code, 400, result.text)
        self.assert_unchanged(before, disk)


if __name__ == '__main__':
    unittest.main()
