"""Integration checks. All data and model artifacts live in a temporary directory."""
import copy
import csv
import io
import json
import os
import runpy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='patent-atlas-test-')
os.environ['PATENT_ATLAS_DATA'] = TEMP.name
from fastapi.testclient import TestClient
import app as module
from analysis_engine import demo_rows, parse_csv, refinement_terms, train

class AppTests(unittest.TestCase):
    def test_individual_initial_query_uses_union_and_refinement_keeps_it(self):
        module.STATE.update(keywords='自動運転 自動車 運転システム', selected=[], queries=[])
        first = module.make_query()
        self.assertIn(' OR ', first['expression'])
        self.assertIn(' AND ', first['expression'])
        self.assertEqual(first['concept_tree']['children'][0]['op'], 'or')
        self.assertEqual(module.export_query(first, 'jplatpat')['expression'], '[[自動運転/TX+運転システム/TX]*自動車/TX]')
        second = module.make_query(True, [], [])
        self.assertEqual(second['boolean_tree'], first['boolean_tree'])
        context = module.llm_judgment.snapshot_query_context(second)
        self.assertEqual(context['search_conditions'], first['boolean_tree'])
        # A previously saved unstructured AND query is not rewritten on refine.
        old = copy.deepcopy(first)
        for field in ('boolean_tree', 'concept_tree', 'base_boolean_tree', 'keyword_context'):
            old.pop(field, None)
        module.STATE['queries'] = [old]
        old_refined = module.make_query(True, [], [])
        self.assertNotIn('boolean_tree', old_refined)
        self.assertNotIn(' OR ', old_refined['expression'])
        self.assertEqual(module.STATE['queries'][0], old)

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def setUp(self):
        module.JOB.update(status='idle')
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        response = self.client.post('/api/demo')
        self.assertEqual(response.status_code, 200)

    def wait_job(self):
        deadline = time.monotonic() + 30
        while module.JOB['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertNotEqual(module.JOB['status'], 'running')

    def test_csv_encoding_mapping_and_deduplication(self):
        source = '公報番号,発明の名称,要約\nJP-1,固体電池,界面抵抗低減\nJP-1,重複,無視\nJP-2,液系電池,冷却装置\n'
        for encoding in ('utf-8-sig','cp932','utf-16'):
            result = parse_csv(source.encode(encoding))
            self.assertEqual(len(result['rows']), 2)
            self.assertEqual(result['duplicates'], 1)
            self.assertEqual(result['rows'][0]['title'], '固体電池')
        self.assertTrue(parse_csv(b'custom,text\nBattery,solid')['needs_mapping'])
        mapped = parse_csv(b'custom,text\nBattery,solid', {'title':'custom','abstract':'text'})
        self.assertEqual(mapped['rows'][0]['abstract'], 'solid')

    def test_full_lightweight_workflow(self):
        rows = module.STATE['patents']
        for label, selected in [('keep',rows[:8]),('exclude',rows[48:56])]:
            response = self.client.post('/api/labels',json={'ids':[r['id'] for r in selected],'label':label})
            self.assertEqual(response.status_code,200)
        self.client.post('/api/train',json={'mode':'lightweight'})
        self.wait_job()
        self.assertEqual(module.JOB['status'],'done',module.JOB)
        result = self.client.get('/api/state').json()
        self.assertIsNotNone(result['training']['metrics'])
        self.assertTrue(all(0<=r['score']<=1 for r in result['patents']))
        terms=self.client.get('/api/refinement').json()
        response=self.client.post('/api/query',json={'refine':True,'include':terms['include'][:1],'exclude':terms['exclude'][:1]})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['queries'][-1]['version'],2)
        self.assertEqual(self.client.get('/api/export/labels').status_code,200)

    def test_settings_secret_boundary_and_atomicity(self):
        self.client.post('/api/settings',json={'provider':'openai','base_url':'https://first.example/v1','api_key':'test-secret'})
        public=self.client.get('/api/state').text
        self.assertNotIn('test-secret',public)
        self.assertNotIn('test-secret',(Path(TEMP.name)/'workspace.json').read_text(encoding='utf-8'))
        self.client.post('/api/settings',json={'base_url':'https://second.example/v1','api_key':''})
        self.assertEqual(module.SETTINGS['api_key'],'')
        original=module.SETTINGS.copy()
        response=self.client.post('/api/settings',json={'provider':'invalid','model':'not-saved'})
        self.assertEqual(response.status_code,400)
        self.assertEqual(module.SETTINGS,original)

    def test_classification_layout_default_and_persistence(self):
        self.assertEqual(module.DEFAULT_SETTINGS['classification_layout'],'semantic')
        self.assertEqual(self.client.get('/api/state').json()['settings']['classification_layout'],'semantic')
        before=copy.deepcopy(module.STATE)
        for layout in ('grid','semantic'):
            with self.subTest(layout=layout):
                response=self.client.post('/api/settings',json={'classification_layout':layout})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['settings']['classification_layout'],layout)
                self.assertEqual(self.client.get('/api/state').json()['settings']['classification_layout'],layout)
                self.assertEqual(module.SETTINGS['classification_layout'],layout)
                persisted=json.loads((Path(TEMP.name)/'workspace.json').read_text(encoding='utf-8'))
                self.assertEqual(persisted['settings']['classification_layout'],layout)
                self.assertEqual(module.STATE,before)

    def test_invalid_classification_layout_preserves_all_settings_and_state(self):
        self.client.post('/api/settings',json={'classification_layout':'grid','api_key':'retained-secret'})
        original_settings=module.SETTINGS.copy()
        original_state=copy.deepcopy(module.STATE)
        original_disk=(Path(TEMP.name)/'workspace.json').read_bytes()
        for invalid in ('random','',None,True,[],{}):
            with self.subTest(layout=invalid):
                response=self.client.post('/api/settings',json={'classification_layout':invalid,'model':'not-saved','clear_secrets':True})
                self.assertEqual(response.status_code,400)
                self.assertEqual(module.SETTINGS,original_settings)
                self.assertEqual(module.STATE,original_state)
                self.assertEqual((Path(TEMP.name)/'workspace.json').read_bytes(),original_disk)

    def test_saved_classification_layout_migration_and_reload(self):
        for saved_settings,expected in (({},'semantic'),({'classification_layout':'grid'},'grid'),
                                        ({'classification_layout':'semantic'},'semantic'),
                                        ({'classification_layout':'random'},'semantic'),
                                        ({'classification_layout':None},'semantic')):
            with self.subTest(settings=saved_settings),tempfile.TemporaryDirectory(prefix='patent-atlas-settings-') as folder:
                saved_settings={**saved_settings,'model':'preserved-model'}
                workspace=Path(folder)/'workspace.json'
                workspace.write_text(json.dumps({'settings':saved_settings,'state':{'keywords':'preserved-keyword'}}),encoding='utf-8')
                with patch.dict(os.environ,{'PATENT_ATLAS_DATA':folder}):
                    loaded=runpy.run_path(str(module.ROOT/'app.py'))
                self.assertEqual(loaded['SETTINGS']['classification_layout'],expected)
                self.assertEqual(loaded['SETTINGS']['model'],'preserved-model')
                self.assertEqual(loaded['STATE']['keywords'],'preserved-keyword')

    def test_cross_origin_and_busy_mutations(self):
        response=self.client.post('/api/demo',headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status_code,403)
        module.JOB['status']='running'
        self.assertEqual(self.client.post('/api/labels',json={'ids':[],'label':'keep'}).status_code,409)
        module.JOB['status']='idle'

    def test_llm_timeout_default_boundaries_and_persistence(self):
        self.assertEqual(module.DEFAULT_SETTINGS['llm_timeout'], 120)
        self.assertEqual(self.client.get('/api/state').json()['settings']['llm_timeout'], 120)
        before = copy.deepcopy(module.STATE)
        for value in (30, 300, 600, 120):
            with self.subTest(value=value):
                response = self.client.post('/api/settings', json={'llm_timeout': value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['settings']['llm_timeout'], value)
                persisted = json.loads((Path(TEMP.name) / 'workspace.json').read_text(encoding='utf-8'))
                self.assertEqual(persisted['settings']['llm_timeout'], value)
                self.assertEqual(module.STATE, before)

    def test_invalid_llm_timeout_preserves_settings_state_and_disk(self):
        self.client.post('/api/settings', json={'llm_timeout': 300, 'api_key': 'retained-secret'})
        original_settings = module.SETTINGS.copy()
        original_state = copy.deepcopy(module.STATE)
        original_disk = (Path(TEMP.name) / 'workspace.json').read_bytes()
        for value in (None, True, False, 29, 601, 120.0, '300', '', [], {}):
            with self.subTest(value=value):
                response = self.client.post('/api/settings', json={'llm_timeout': value, 'model': 'not-saved', 'clear_secrets': True})
                self.assertEqual(response.status_code, 400)
                self.assertIn('30〜600秒', response.json()['detail'])
                self.assertEqual(module.SETTINGS, original_settings)
                self.assertEqual(module.STATE, original_state)
                self.assertEqual((Path(TEMP.name) / 'workspace.json').read_bytes(), original_disk)

    def test_saved_llm_timeout_migration_and_reload_preserves_other_settings(self):
        for saved_settings, expected in (({}, 120), ({'llm_timeout': 300}, 300),
                                        ({'llm_timeout': 600}, 600), ({'llm_timeout': None}, 120),
                                        ({'llm_timeout': True}, 120), ({'llm_timeout': '300'}, 120),
                                        ({'llm_timeout': 5}, 120)):
            with self.subTest(settings=saved_settings), tempfile.TemporaryDirectory(prefix='patent-atlas-settings-') as folder:
                workspace = Path(folder) / 'workspace.json'
                saved_settings = {**saved_settings, 'model': 'preserved-model', 'base_url': 'http://127.0.0.1:1234/v1'}
                workspace.write_text(json.dumps({'settings': saved_settings, 'state': {'keywords': 'preserved-keyword'}}), encoding='utf-8')
                original = workspace.read_bytes()
                with patch.dict(os.environ, {'PATENT_ATLAS_DATA': folder}):
                    loaded = runpy.run_path(str(module.ROOT / 'app.py'))
                self.assertEqual(loaded['SETTINGS']['llm_timeout'], expected)
                self.assertEqual(loaded['SETTINGS']['model'], 'preserved-model')
                self.assertEqual(loaded['SETTINGS']['base_url'], 'http://127.0.0.1:1234/v1')
                self.assertEqual(loaded['STATE']['keywords'], 'preserved-keyword')
                self.assertEqual(workspace.read_bytes(), original)

    def test_case_insensitive_exclusion_protection(self):
        rows=[dict(title='SOLID BATTERY',abstract='',label='keep'),dict(title='solid battery',abstract='',label='exclude')]
        self.assertEqual(refinement_terms(rows)['exclude'],[])

    def test_refinement_preserves_previous_conditions_and_reports_removal(self):
        rows=module.STATE['patents']
        for row in rows[:4]:
            row.update(label='keep',label_source='human')
        for row in rows[48:52]:
            row.update(label='exclude',label_source='human')
        terms=refinement_terms(rows)
        first=module.make_query(True,terms['include'][:1],terms['exclude'][:1])
        second=module.make_query(True)
        self.assertEqual(first['expression'],second['expression'])
        third=module.make_query(True,[],[])
        self.assertEqual(set(third['removed_terms']),set(first['include_terms']+first['exclude_terms']))

    def test_single_family_can_train_without_holdout(self):
        rows=demo_rows()[:4]+demo_rows()[48:52]
        for i,r in enumerate(rows):
            r.update(label='keep' if i<4 else 'exclude',label_source='human',family='one-family')
        result=train(rows,'lightweight','',False,TEMP.name,lambda *args:None,lambda:False)
        self.assertIsNone(result['metrics'])
        self.assertEqual(result['train_count'],8)

    def test_agent_invalid_confidence_does_not_corrupt_state(self):
        module.SETTINGS['provider']='local'
        first=module.STATE['patents'][0]['id']
        responses=[{'selected':[],'reason':'test'},{'decisions':[{'id':first,'decision':'keep','confidence':'NaN','reason':'test'}]}]
        with patch.object(module,'complete',side_effect=responses):
            self.client.post('/api/agent',json={'criteria':'固体電池','max_items':1})
            self.wait_job()
        self.assertEqual(module.JOB['status'],'error')
        self.assertEqual(self.client.get('/api/state').status_code,200)
        self.assertIsNone(module.STATE['patents'][0]['label'])

    def test_agent_preserves_human_and_records_reasons(self):
        module.SETTINGS['provider']='local'
        rows=module.STATE['patents']
        rows[0].update(label='keep',label_source='human')
        module.STATE['training']={'old':True}
        def response(settings,system,payload,**kwargs):
            if 'candidates' in payload:
                return {'selected':['H01M10/0562'],'reason':'全固体電池の電解質'}
            return {'decisions':[{'id':r['id'],'decision':'exclude','confidence':.95,'relevance':.1,'reason':'テスト判断の根拠'} for r in payload['patents']]}
        with patch.object(module,'complete',side_effect=response):
            self.client.post('/api/agent',json={'criteria':'固体電池','max_items':2})
            self.wait_job()
        self.assertEqual(module.JOB['status'],'done',module.JOB)
        self.assertEqual(rows[0]['label_source'],'human')
        self.assertEqual(rows[1]['label_source'],'agent')
        self.assertEqual(module.STATE['training']['mode'], 'llm')
        self.assertEqual(module.STATE['training']['completed_count'], 2)
        self.assertIn('CSV',module.STATE['agent_log'][-1]['message'])

    def test_upload_with_custom_columns(self):
        response=self.client.post('/api/upload',content=b'name,summary\nSolid battery,Electrolyte interface\nLiquid cell,Cooling mechanism',headers={'Content-Type':'application/octet-stream'})
        self.assertTrue(response.json()['needs_mapping'])
        response=self.client.post('/api/upload',params={'mapping':json.dumps({'title':'name','abstract':'summary'})},content=b'name,summary\nSolid battery,Electrolyte interface\nLiquid cell,Cooling mechanism',headers={'Content-Type':'application/octet-stream'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json()['patents']),2)

    def test_query_export_uses_snapshot_without_creating_history(self):
        q=copy.deepcopy(module.STATE['queries'][0]);before=copy.deepcopy(module.STATE)
        response=self.client.post('/api/query/export',json={'query_id':q['id'],'format':'jplatpat'})
        self.assertEqual(response.status_code,200)
        self.assertIn('/IP',response.json()['expression'])
        self.assertEqual(module.STATE,before)
        self.assertEqual(self.client.post('/api/query/export',json={'query_id':'missing','format':'jplatpat'}).status_code,404)

    def test_same_code_ipc_and_cpc_have_independent_identity(self):
        response=self.client.post('/api/classification',json={'code':'H01M10/0562','kind':'CPC','title':'CPC候補'})
        self.assertEqual(response.status_code,200)
        matches=[c for c in response.json()['candidates'] if c['code']=='H01M10/0562']
        self.assertEqual({c['key'] for c in matches},{'IPC:H01M10/0562','CPC:H01M10/0562'})
        self.assertEqual(self.client.post('/api/selection',json={'selected':['H01M10/0562']}).status_code,400)
        self.client.post('/api/selection',json={'selected':['CPC:H01M10/0562']})
        response=self.client.post('/api/query',json={})
        self.assertEqual([c['kind'] for c in response.json()['queries'][-1]['classifications']],['CPC'])

    def test_agent_keeps_previous_refinements(self):
        module.SETTINGS['provider']='local'
        module.STATE.update(keywords='battery',candidates=[],selected=[],queries=[dict(id='old',version=1,keywords=['battery'],classifications=[],include_terms=['lithium'],exclude_terms=['camera'])])
        for r in module.STATE['patents']:
            r.update(label='keep',label_source='human')
        # Existing human-labelled text does not contain these English exclusions.
        with patch.object(module,'candidate_list',return_value=[]),patch.object(module,'complete',return_value={'selected':[],'reason':'継続'}):
            self.client.post('/api/agent',json={'criteria':'battery','max_items':1})
            self.wait_job()
        self.assertEqual(module.JOB['status'],'done',module.JOB)
        self.assertEqual(module.STATE['queries'][-1]['include_terms'],['lithium'])
        self.assertEqual(module.STATE['queries'][-1]['exclude_terms'],['camera'])
        self.assertEqual(module.STATE['queries'][-1]['removed_terms'],[])

    def test_stale_refinement_is_not_preselected_after_context_change(self):
        module.STATE['queries'][-1].update(include_terms=['lithium'],exclude_terms=['camera'])
        module.STATE['keywords']='new topic'
        response=self.client.get('/api/refinement').json()
        self.assertEqual(response['active_include'],[]);self.assertEqual(response['active_exclude'],[])
        self.assertNotIn('camera',response['exclude'])

    def test_agent_final_proposal_keeps_previous_include_clause(self):
        module.SETTINGS['provider']='local'
        rows=module.STATE['patents'][:3]
        rows[0].update(title='solid lithium battery',abstract='',label='keep',label_source='human')
        rows[1].update(title='camera optical device',abstract='',label='exclude',label_source='human')
        rows[2].update(title='undecided device',abstract='',label=None,label_source=None)
        module.STATE.update(patents=rows,keywords='battery',candidates=[],selected=[],queries=[dict(id='old',version=1,keywords=['battery'],classifications=[],include_terms=['solid'],exclude_terms=['camera'])])
        answers=[{'selected':[],'reason':'継続'},{'decisions':[dict(id=rows[2]['id'],decision='unsure',confidence=.5,relevance=.5,reason='保留')]}]
        with patch.object(module,'candidate_list',return_value=[]),patch.object(module,'complete',side_effect=answers):
            self.client.post('/api/agent',json={'criteria':'battery','max_items':1})
            self.wait_job()
        self.assertEqual(module.JOB['status'],'done',module.JOB)
        self.assertEqual(module.STATE['queries'][-1]['include_terms'],['solid'])
        self.assertEqual(module.STATE['queries'][-1]['exclude_terms'],['camera'])
        self.assertEqual(module.STATE['queries'][-1]['removed_terms'],[])

    def test_missing_agent_selection_does_not_clear_classifications(self):
        module.SETTINGS['provider']='local'
        before=copy.deepcopy(module.STATE)
        with patch.object(module,'complete',return_value={}):
            self.client.post('/api/agent',json={'criteria':'固体電池','max_items':1})
            self.wait_job()
        self.assertEqual(module.JOB['status'],'error')
        self.assertEqual(module.STATE['selected'],before['selected'])
        self.assertEqual(module.STATE['queries'],before['queries'])

    def test_missing_agent_decisions_stop_job(self):
        module.SETTINGS['provider']='local'
        with patch.object(module,'complete',side_effect=[{'selected':[],'reason':'test'},{}]):
            self.client.post('/api/agent',json={'criteria':'固体電池','max_items':1})
            self.wait_job()
        self.assertEqual(module.JOB['status'],'error')
        self.assertIsNone(module.STATE['patents'][0]['label'])

    def test_exclusion_protection_scans_beyond_training_truncation(self):
        rows=[dict(title='必要な文書',abstract='長文'*4000+' camera',label='keep'),dict(title='camera',abstract='camera sensor',label='exclude')]
        self.assertNotIn('camera',refinement_terms(rows)['exclude'])

@unittest.skipUnless(os.environ.get('TEST_TRANSFORMER')=='1','Run with TEST_TRANSFORMER=1 for actual local Transformer training')
class TransformerTest(unittest.TestCase):
    def test_real_transformer_weights_change(self):
        import torch
        from transformers import BertConfig, BertForSequenceClassification, BertTokenizerFast
        folder=Path(TEMP.name)/'tiny-bert'
        folder.mkdir(exist_ok=True)
        vocab=['[PAD]','[UNK]','[CLS]','[SEP]','[MASK]','solid','battery','electrolyte','interface','camera','image','inspection','device']
        (folder/'vocab.txt').write_text('\n'.join(vocab),encoding='utf-8')
        tokenizer=BertTokenizerFast(vocab_file=str(folder/'vocab.txt'))
        tokenizer.save_pretrained(folder)
        torch.manual_seed(4)
        model=BertForSequenceClassification(BertConfig(vocab_size=len(vocab),hidden_size=16,num_hidden_layers=1,num_attention_heads=2,intermediate_size=32,num_labels=2,max_position_embeddings=512))
        initial=model.bert.embeddings.word_embeddings.weight.detach().clone()
        model.save_pretrained(folder,safe_serialization=True)
        rows=[dict(id=str(i),title='solid battery electrolyte interface' if i<6 else 'camera image inspection device',abstract='',label='keep' if i<6 else 'exclude',label_source='human',family=str(i)) for i in range(12)]
        output=Path(TEMP.name)/'trained'
        result=train(rows,'transformer',str(folder),False,str(output),lambda *args:None,lambda:False)
        trained=BertForSequenceClassification.from_pretrained(output)
        self.assertFalse(torch.equal(initial,trained.bert.embeddings.word_embeddings.weight))
        self.assertEqual(len(result['scores']),12)
        self.assertTrue(all(0<=s<=1 for s in result['scores'].values()))

if __name__=='__main__':
    unittest.main(verbosity=2)
