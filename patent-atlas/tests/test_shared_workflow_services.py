"""Regression checks for shared judgments and non-destructive CSV imports."""
import copy
import csv
import io
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_app as integration
import llm_judgment
from workspace_import import merge_patents

module = integration.module


class SharedWorkflowServices(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(module.app)
        module.JOB.update(status='idle', id='test-shared', kind=None)
        module.STOP.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        self.client.post('/api/demo')
        self.client.post('/api/upload', content='id,title,abstract\nJP1,vehicle control,sensor\nJP2,factory robot,motor'.encode())
        module.STATE['patents'][0].update(label='keep', label_source='human', label_reason='manual', score=.8)
        module.STATE['patents'][1].update(label=None, label_source='agent', label_reason='needs detail',
            agent_confidence=.4, llm_decision='unsure', llm_relevance=.5, score=None)
        module.STATE['training'] = dict(mode='llm', status='completed', criteria='vehicle', completed_count=1)
        module.save()

    def test_fresh_only_skips_saved_deferred_but_respects_explicit_human_reset(self):
        rows = copy.deepcopy(module.STATE['patents'])
        opts = llm_judgment.prepare({'fresh_only': True}, rows, 'vehicle', 'local', allow_empty=True)
        self.assertEqual(opts['target_ids'], [])
        rows[1].update(label_source=None, label_reason='')
        self.assertEqual(llm_judgment.prepare({'fresh_only': True}, rows, 'vehicle', 'local')['target_ids'], ['JP2'])
        with self.assertRaises(ValueError):
            llm_judgment.prepare({'fresh_only': 'yes'}, rows, 'vehicle', 'local')

    def test_empty_shared_assessment_preserves_prior_results_without_network(self):
        before = copy.deepcopy(module.STATE)
        with patch.object(module, 'complete') as complete:
            result = module.run_llm_assessment({'criteria': 'vehicle', 'fresh_only': True})
        self.assertTrue(result['skipped'])
        self.assertEqual(module.STATE, before)
        complete.assert_not_called()

    def test_agent_reuses_saved_judgments_and_fi_without_rejudging(self):
        module.STATE['keywords'] = 'vehicle'
        module.STATE['candidates'] = [module.explorer.enrich({'kind': 'IPC', 'code': 'B60W'})]
        module.STATE['selected'] = ['IPC:B60W']
        module.STATE['queries'] = []
        module.STATE['patents'][0]['fi'] = 'G05D1/00,101'
        before = copy.deepcopy(module.STATE['patents'])
        with patch.dict(module.SETTINGS, {'provider': 'local'}), patch.object(
            module, 'complete', return_value={'selected': ['IPC:B60W'], 'reason': 'vehicle control'}
        ) as complete:
            response = self.client.post('/api/agent', json={'criteria': 'vehicle', 'max_items': 100})
            self.assertEqual(response.status_code, 200, response.text)
            deadline = time.monotonic() + 10
            while module.JOB['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(module.JOB['status'], 'done', module.JOB)
        self.assertEqual(complete.call_count, 1, 'Classification selection only; no patent rejudgment')
        self.assertEqual(module.STATE['patents'], before)
        self.assertIn('IPC:G05D1/00', module.STATE['selected'])
        self.assertIn('IPC="G05D1/00"', module.STATE['queries'][-1]['expression'])
        self.assertEqual(len(module.STATE['queries']), 2)

    def test_append_keeps_decisions_scores_and_main_ids_and_fills_classification(self):
        before = copy.deepcopy(module.STATE['patents'])
        raw = 'id,title,abstract,FI\nJP-1,vehicle control,sensor,"G05D1/00,101"\nJP3,vehicle navigation,map,G08G1/00'
        response = self.client.post('/api/upload?merge=true', content=raw.encode())
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()['patents']
        self.assertEqual([row['id'] for row in rows], ['JP1', 'JP2', 'JP3'])
        for old, new in zip(before, rows):
            for field in ('label', 'label_source', 'label_reason', 'score', 'agent_confidence', 'llm_decision'):
                self.assertEqual(old.get(field), new.get(field), field)
        self.assertEqual(rows[0]['fi'], 'G05D1/00,101')
        self.assertEqual(response.json()['training']['completed_count'], 1)
        self.assertTrue(response.json()['training']['dataset_changed'])
        self.assertIsNone(rows[-1]['label'])

    def test_conflicting_append_and_write_failure_leave_saved_state_intact(self):
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        response = self.client.post('/api/upload?merge=true', content=b'id,title,abstract\nJP-1,different,sensor')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(module.STATE, before)
        with patch.object(module, 'save', side_effect=ValueError('disk failure')):
            response = self.client.post('/api/upload?merge=true', content=b'id,title,abstract\nJP3,new content,details')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_fi_only_enrichment_preserves_every_non_fi_field(self):
        before = copy.deepcopy(module.STATE)
        raw = 'id,title,abstract,FI\nJP1,vehicle control,sensor,"G05D1/00,101"\nJP2,factory robot,motor,B25J9/00'
        response = self.client.post('/api/classifications/enrich-import', content=raw.encode())
        self.assertEqual(response.status_code, 200, response.text)
        for old, new in zip(before['patents'], response.json()['patents']):
            self.assertEqual({k: v for k, v in old.items() if k != 'fi'}, {k: v for k, v in new.items() if k != 'fi'})
            self.assertTrue(new['fi'])
        self.assertEqual(response.json()['training'], before['training'])
        exported = self.client.get('/api/export/labels').content.decode('utf-8-sig')
        self.assertEqual(list(csv.DictReader(io.StringIO(exported)))[0]['fi'], 'G05D1/00,101')

    def test_enrichment_conflict_is_atomic_and_running_jobs_block_import(self):
        before = copy.deepcopy(module.STATE)
        raw = b'id,title,abstract,FI\nJP1,vehicle control,sensor,G05D1/00\nJP2,changed,motor,B25J9/00'
        self.assertEqual(self.client.post('/api/classifications/enrich-import', content=raw).status_code, 400)
        self.assertEqual(module.STATE, before)
        module.JOB['status'] = 'running'
        try:
            self.assertEqual(self.client.post('/api/upload?merge=true', content=b'id,title\nNEW,new').status_code, 409)
        finally:
            module.JOB['status'] = 'idle'
        self.assertEqual(module.STATE, before)


if __name__ == '__main__':
    unittest.main()
