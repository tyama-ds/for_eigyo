"""Display caption projection does not change patent/search workspace state."""
import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_app as integration

module = integration.module


class TranslationRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(module.app)
        module.JOB.update(status='idle')
        self.client.post('/api/demo')

    def test_existing_manual_and_lab_items_receive_labels_without_rewriting_snapshots(self):
        item = {'kind': 'IPC', 'code': 'C01D15/00', 'title': 'Saved canonical title',
                'key': 'IPC:C01D15/00', 'reason': 'Saved relevance reason', 'selectable': True}
        module.STATE['candidates'].append(copy.deepcopy(item))
        module.STATE['discovery']['plan'] = {'keywords': 'Existing plan', 'candidate_hypotheses': [copy.deepcopy(item)]}
        module.STATE['discovery']['analysis'] = {'recommendations': [copy.deepcopy(item)], 'counts': {}}
        module.save()
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        response = self.client.get('/api/state')
        self.assertEqual(response.status_code, 200)
        value = response.json()
        manual = next(row for row in value['candidates'] if row['key'] == item['key'])
        for row in (manual, value['discovery']['plan']['candidate_hypotheses'][0], value['discovery']['analysis']['recommendations'][0]):
            self.assertTrue(row['title_ja'])
            self.assertEqual(row['title_en'], 'Lithium compounds')
            self.assertEqual(row['title'], item['title'])
            self.assertEqual(row['reason'], item['reason'])
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_translation_action_is_independent_from_manual_work_and_returns_no_settings(self):
        before = copy.deepcopy(module.STATE)
        with patch.object(module.classification_translations, 'fetch_translations', return_value={
                'translations': [{'kind': 'IPC', 'code': 'C01D15/00', 'title_ja': 'リチウム化合物'}],
                'warnings': [], 'missing': []}) as fetch:
            response = self.client.post('/api/classifications/translations', json={'codes': ['C01D15/00']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_args.args[0], ['C01D15/00'])
        self.assertNotIn('settings', response.json())
        self.assertEqual(module.STATE, before)

    def test_invalid_translation_request_is_rejected_without_network_or_state_changes(self):
        before = copy.deepcopy(module.STATE)
        with patch('classification_translations.httpx.Client') as network:
            response = self.client.post('/api/classifications/translations', json={'codes': ['NO-SUCH-CODE']})
        self.assertEqual(response.status_code, 400)
        network.assert_not_called()
        self.assertEqual(module.STATE, before)


if __name__ == '__main__':
    unittest.main()
