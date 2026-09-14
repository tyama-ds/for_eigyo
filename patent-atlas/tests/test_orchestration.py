"""The orchestration uses only test_app's temporary data directory and mocked LLMs."""
import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_app as integration
from discovery_routes import new_discovery
from orchestration_engine import checked_options, fingerprint, fresh_rows, new_orchestration, recover


module = integration.module


def patent(identifier, **extra):
    return dict(id=identifier, title='固体電池の界面製造方法 ' + identifier, abstract='電池の界面層を形成する。',
                ipc=['H01M10/0562'], fterm=[], fi=[], applicant='Example', year='2025',
                family='', label=None, label_source=None, label_reason='', score=None, **extra)


def respond(settings, system, payload, **kwargs):
    return {'decisions': [dict(id=identifier, decision='keep', confidence=.95, relevance=.9,
                               reason='電池界面の技術が対象です。') for identifier in payload['required_ids']]}


class OrchestrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def setUp(self):
        self.assertEqual(module.DATA, Path(integration.TEMP.name))
        self.assertNotEqual(module.JOB.get('status'), 'running')
        module.JOB.update(status='idle', kind=None, error=None)
        module.STOP.clear()
        module.SETTINGS.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        candidate = module.explorer.enrich(dict(kind='IPC', code='H01M10/058'))
        module.STATE.clear()
        module.STATE.update(keywords='全固体電池', candidates=[candidate], selected=[candidate['key']],
                            patents=[patent('JP1'), patent('JP2')], clusters=[], queries=[], training=None,
                            agent_log=[], demo=False, import_info=None, classification_view=None,
                            discovery=new_discovery(), orchestration=new_orchestration())
        module.save()

    def wait_job(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with module.LOCK:
                if module.JOB['status'] != 'running':
                    return
            time.sleep(.01)
        self.fail('orchestration job did not finish')

    def post(self, path, body=None):
        response = self.client.post('/api/orchestration/' + path, json=body or {})
        self.assertEqual(response.status_code, 200, response.text)
        if path in ('start', 'resume'):
            self.wait_job()
        return response

    def test_review_cycle_preserves_human_and_existing_selection_until_explicit_edit(self):
        module.STATE['patents'][0].update(label='keep', label_source='human', label_reason='私の対象')
        before = copy.deepcopy(module.STATE['patents'][0])
        self.post('start', dict(judge=False))
        self.assertEqual(module.JOB['status'], 'done', module.JOB)
        value = module.STATE['orchestration']
        self.assertEqual((value['status'], value['stage']), ('review', 'classifications'))
        self.assertEqual(module.STATE['selected'], ['IPC:H01M10/058'])
        self.assertEqual(module.STATE['queries'], [])
        self.assertIn('IPC:H01M10/0562', value['checkpoint']['selected_keys'])
        self.post('resume')
        value = module.STATE['orchestration']
        self.assertEqual((value['status'], value['stage']), ('review', 'query'))
        self.assertTrue(value['checkpoint']['expression'])
        self.assertEqual(module.STATE['queries'], [])
        self.post('resume', dict(selected_keys=['IPC:H01M10/058']))
        self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')
        self.assertEqual(module.STATE['selected'], ['IPC:H01M10/058'])
        self.assertEqual(len(module.STATE['queries']), 1)
        for field in ('label', 'label_source', 'label_reason'):
            self.assertEqual(module.STATE['patents'][0][field], before[field])
        saved = json.loads((module.DATA / 'workspace.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['state']['orchestration']['status'], 'waiting_csv')

    def test_automatic_cycle_uses_same_llm_and_does_not_rejudge_saved_or_deferred_patents(self):
        module.SETTINGS.update(provider='local')
        module.STATE['patents'][0].update(label='keep', label_source='human', label_reason='人の判断')
        module.STATE['patents'].append(patent('JP3'))
        module.STATE['patents'][1].update(label_source='agent', label_reason='情報不足',
                                         agent_confidence=.6, llm_decision='unsure')
        with patch.object(module, 'complete', side_effect=respond) as mocked:
            self.post('start', dict(review=False))
        self.assertEqual(module.JOB['status'], 'done', module.JOB)
        self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')
        self.assertEqual(len(mocked.call_args_list), 1)
        self.assertEqual(mocked.call_args.args[2]['required_ids'], ['JP3'])
        self.assertEqual(module.STATE['orchestration']['summary']['deferred'], 1)
        self.assertEqual(module.STATE['orchestration']['summary']['unprocessed'], 0)
        self.assertEqual(module.STATE['patents'][0]['label_source'], 'human')

    def test_offline_all_assessed_can_reuse_results_without_llm(self):
        for row in module.STATE['patents']:
            row.update(label='keep', label_source='agent', label_reason='以前の判定')
        with patch.object(module, 'run_llm_assessment') as mocked:
            self.post('start', dict(review=False))
        self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')
        mocked.assert_not_called()

    def test_model_training_is_shared_and_opt_in(self):
        with patch.object(module, 'run_model_training') as mocked:
            self.post('start', dict(judge=False, review=False, learning='lightweight', include_agent=True))
        self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')
        self.assertEqual(mocked.call_args.args, ('lightweight',))
        self.assertTrue(mocked.call_args.kwargs['include_agent'])

    def test_training_failure_can_resume_with_learning_disabled(self):
        with patch.object(module, 'run_model_training', side_effect=ValueError('教師ラベル不足')):
            self.post('start', dict(judge=False, review=False, learning='lightweight'))
        self.assertEqual(module.STATE['orchestration']['stage'], 'train')
        self.assertEqual(module.STATE['orchestration']['status'], 'error')
        self.post('resume', dict(learning='none', include_agent=False))
        self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')

    def test_stop_discards_pending_batch_and_resume_reuses_persisted_stages(self):
        module.SETTINGS.update(provider='local')
        def stop_response(*args, **kwargs):
            module.STOP.set()
            return respond(*args, **kwargs)
        with patch.object(module, 'complete', side_effect=stop_response):
            self.post('start', dict(review=False))
        self.assertEqual(module.JOB['status'], 'cancelled')
        self.assertEqual(module.STATE['orchestration']['status'], 'paused')
        self.assertEqual(module.STATE['orchestration']['stage'], 'assess')
        self.assertFalse(any(row['label'] for row in module.STATE['patents']))
        rounds = len(module.STATE['discovery']['rounds'])
        with patch.object(module, 'complete', side_effect=respond):
            self.post('resume')
        self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')
        self.assertEqual(len(module.STATE['discovery']['rounds']), rounds + 1)

    def test_error_retains_stage_and_resume_does_not_duplicate_query(self):
        module.SETTINGS.update(provider='local')
        with patch.object(module, 'complete', side_effect=ValueError('接続を確認してください。')):
            self.post('start', dict(review=False))
        self.assertEqual(module.JOB['status'], 'error')
        self.assertEqual(module.STATE['orchestration']['stage'], 'assess')
        with patch.object(module, 'complete', side_effect=respond):
            self.post('resume')
        self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')
        self.assertEqual(len(module.STATE['queries']), 1)

    def test_new_csv_is_required_before_next_cycle(self):
        self.post('start', dict(judge=False, review=False))
        response = self.client.post('/api/orchestration/resume', json={})
        self.assertEqual(response.status_code, 400)
        module.STATE['patents'].append(patent('JP3'))
        self.post('resume')
        self.assertEqual(module.STATE['orchestration']['cycle'], 2)
        self.assertEqual(module.STATE['orchestration']['summary']['documents'], 3)
        self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')

    def test_lab_patents_are_shared_for_same_topic_and_human_decisions_win(self):
        from discovery_engine import propose_plan
        lab_row = patent('JP1')
        lab_row.update(label='exclude', label_source='agent')
        module.STATE['discovery'].update(plan=propose_plan('全固体電池', [], []),
                                          patents=[lab_row, patent('LAB3')])
        module.STATE['patents'][0].update(label='keep', label_source='human', label_reason='手動')
        self.post('start', dict(judge=False, review=False))
        self.assertEqual({row['id'] for row in module.STATE['patents']}, {'JP1', 'JP2', 'LAB3'})
        row = next(row for row in module.STATE['discovery']['patents'] if row['id'] == 'JP1')
        self.assertEqual((row['label'], row['label_source']), ('keep', 'human'))

    def test_lab_conflicting_text_is_not_overwritten_or_rejudged(self):
        from discovery_engine import propose_plan
        row = patent('JP1')
        row.update(abstract='別の本文')
        module.STATE['discovery'].update(plan=propose_plan('全固体電池', [], []), patents=[row])
        before = copy.deepcopy(module.STATE)
        response = self.client.post('/api/orchestration/start', json={'judge': False})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(module.STATE, before)

    def test_midcycle_csv_change_is_rejected_and_reset_keeps_owned_results(self):
        self.post('start', dict(judge=False))
        module.STATE['patents'].append(patent('JP3'))
        response = self.client.post('/api/orchestration/resume', json={})
        self.assertEqual(response.status_code, 400)
        patents = copy.deepcopy(module.STATE['patents'])
        selection = copy.deepcopy(module.STATE['selected'])
        self.post('reset')
        self.assertEqual(module.STATE['orchestration']['status'], 'idle')
        self.assertEqual(module.STATE['patents'], patents)
        self.assertEqual(module.STATE['selected'], selection)

    def select_in_other_tab(self, key):
        kind, code = key.split(':', 1)
        result = self.client.post('/api/classification', json={'kind': kind, 'code': code})
        self.assertEqual(result.status_code, 200, result.text)
        result = self.client.post('/api/selection', json={'selected': module.STATE['selected'] + [key]})
        self.assertEqual(result.status_code, 200, result.text)

    def test_classification_review_preserves_late_manual_addition_and_honors_original_removal(self):
        self.post('start', dict(judge=False))
        original = 'IPC:H01M10/058'
        late = 'IPC:A61K9/00'
        checkpoint = module.STATE['orchestration']['checkpoint']
        self.assertIn(original, checkpoint['selection_baseline'])
        self.assertNotIn(late, checkpoint['selection_baseline'])
        self.select_in_other_tab(late)
        # The old GUI form submits neither the newly added key nor the explicitly
        # unchecked original key. Only the later addition must survive.
        self.post('resume', {'selected_keys': []})
        self.assertIn(late, module.STATE['selected'])
        self.assertNotIn(original, module.STATE['selected'])
        self.assertEqual(module.STATE['orchestration']['stage'], 'query')
        self.assertTrue(any(late in entry['message'] for entry in module.STATE['orchestration']['logs']))

    def test_query_review_preserves_late_selection_even_if_candidate_was_already_listed(self):
        late = 'IPC:A61K9/00'
        result = self.client.post('/api/classification', json={'kind': 'IPC', 'code': 'A61K9/00'})
        self.assertEqual(result.status_code, 200)
        self.post('start', dict(judge=False))
        self.post('resume')
        checkpoint = module.STATE['orchestration']['checkpoint']
        self.assertIn(late, [item['key'] for item in checkpoint['candidates']])
        self.assertNotIn(late, checkpoint['selection_baseline'])
        # Older persisted checkpoints without the new snapshot are supported.
        checkpoint.pop('selection_baseline')
        self.select_in_other_tab(late)
        self.post('resume', {'selected_keys': [], 'include_terms': [], 'exclude_terms': []})
        self.assertEqual(module.STATE['selected'], [late])
        self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')
        self.assertEqual([item['key'] for item in module.STATE['queries'][-1]['classifications']], [late])

    def test_invalid_inputs_and_overlapping_jobs_do_not_start(self):
        for options in [dict(max_items=0), dict(max_items=True), dict(threshold=float('nan')),
                        dict(review='yes'), dict(learning='automatic')]:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    checked_options(options, 'battery')
        response = self.client.post('/api/orchestration/start', json={})
        self.assertEqual(response.status_code, 400)  # offline, with fresh records
        module.JOB.update(status='running', kind='training')
        response = self.client.post('/api/orchestration/start', json={'judge': False})
        self.assertEqual(response.status_code, 409)
        response = self.client.post('/api/orchestration/reset', json={})
        self.assertEqual(response.status_code, 409)
        module.JOB.update(status='idle')

    def test_start_save_failure_restores_state_and_does_not_start_job(self):
        before = copy.deepcopy(module.STATE)
        job = copy.deepcopy(module.JOB)
        with patch.object(module, 'save', side_effect=OSError('test write failure')):
            response = self.client.post('/api/orchestration/start', json={'judge': False})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(module.STATE, before)
        self.assertEqual(module.JOB, job)

    def test_resume_save_failure_restores_checkpoint_and_selection(self):
        self.post('start', dict(judge=False))
        before = copy.deepcopy(module.STATE)
        job = copy.deepcopy(module.JOB)
        with patch.object(module, 'save', side_effect=OSError('test write failure')):
            response = self.client.post('/api/orchestration/resume', json={'selected_keys': []})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(module.STATE, before)
        self.assertEqual(module.JOB, job)

    def test_recovery_and_content_fingerprint_exclude_mutable_judgments(self):
        rows = module.STATE['patents']
        before = fingerprint(rows)
        rows[0].update(label='keep', score=.99, x=.5)
        self.assertEqual(fingerprint(rows), before)
        rows[0]['abstract'] = 'updated text'
        self.assertNotEqual(fingerprint(rows), before)
        value = new_orchestration()
        value.update(status='running', stage='assess')
        restored = recover(value)
        self.assertEqual((restored['status'], restored['stage']), ('paused', 'assess'))
        old = dict(label=None, label_source='agent', label_reason='情報不足', agent_confidence=.5)
        self.assertEqual(fresh_rows([old]), [])
        old.update(label_source=None)
        self.assertEqual(fresh_rows([old]), [old])
