import copy
import json
import sys
import unittest
from unittest.mock import patch

import numpy as np

from projection_engine import project_patents, compute_evaluation, _align_and_frame


def rows():
    return [dict(id='JP'+str(i), title=title, abstract=text, label=None) for i, (title, text) in enumerate([
        ('自動運転の走行制御', '車両の位置と障害物を検出し走行経路を制御する'),
        ('自動運転の車両制御', '障害物の位置と道路を検出し自動運転経路を制御する'),
        ('電池製造方法', '電池の電極を積層し固体電解質を加圧する工程'),
        ('電池界面処理', '電池の電極と固体電解質の界面を接合する'),
        ('果物の選別装置', '果物を撮影して傷と色を検出して選別する'),
        ('食品の加熱方法', '食品を均一に加熱して加工する')])]


class ProjectionTests(unittest.TestCase):
    def test_pca_is_deterministic_finite_and_read_only(self):
        data = rows(); before = copy.deepcopy(data)
        a = project_patents(data); b = project_patents(data)
        self.assertEqual(a, b); self.assertEqual(data, before)
        self.assertEqual([p['id'] for p in a['points']], [r['id'] for r in data])
        self.assertIn('中心化', a['metadata']['algorithm'])
        self.assertIn('SVD', a['metadata']['representation'])
        for point in a['points']:
            self.assertTrue(0 <= point['x'] <= 1 and 0 <= point['y'] <= 1)
        json.dumps(a, allow_nan=False)

    def test_projection_does_not_use_judgment_as_a_feature(self):
        data = rows(); before = project_patents(data)
        for row in data:
            row.update(label='exclude', label_source='human', score=0)
        self.assertEqual(before, project_patents(data))

    def test_tsne_deterministic_and_parameters_disclosed(self):
        a = project_patents(rows(), 'tsne'); b = project_patents(rows(), 'tsne')
        self.assertEqual(a, b); self.assertLess(a['metadata']['perplexity'], len(rows()))
        self.assertEqual(a['method'], 'tsne'); json.dumps(a, allow_nan=False)

    def test_umap_missing_dependency_is_not_disguised_as_another_method(self):
        with patch.dict(sys.modules, {'umap': None}):
            with self.assertRaisesRegex(ValueError, 'pip install umap-learn'):
                project_patents(rows(), 'umap')

    def test_single_and_identical_documents_remain_overlapped(self):
        for data in ([dict(id='1',title='電池')], [dict(id=str(i), title='電池') for i in range(4)]):
            result = project_patents(data)
            self.assertTrue(result['metadata']['degenerate'])
            self.assertTrue(all(p['x'] == .5 and p['y'] == .5 for p in result['points']))

    def test_tiny_non_pca_and_empty_text_produce_actionable_errors(self):
        for method in ('tsne','umap'):
            with self.assertRaisesRegex(ValueError, '4件以上'):
                project_patents(rows()[:3], method)
        with self.assertRaisesRegex(ValueError, 'タイトル・要約'):
            project_patents([dict(id='x')])
        self.assertEqual(project_patents([])['points'], [])

    def test_limits_invalid_methods_and_duplicate_ids(self):
        for data in ([dict(id='x',title='a')]*2, [dict(title='a')], [dict(id=' ',title='a')]):
            with self.assertRaisesRegex(ValueError, 'ID'):
                project_patents(data)
        with self.assertRaisesRegex(ValueError, '5,000'):
            project_patents([dict(id='x')]*5001)
        with self.assertRaisesRegex(ValueError, '配置方式'):
            project_patents(rows(), 'fake-pca')

    def test_orientation_alignment_uses_ids_not_array_order(self):
        base = np.array([[0.,0.],[2.,0.],[0.,1.],[2.,1.]])
        frame, _ = _align_and_frame(base.copy(), list('abcd'), None)
        reference = [dict(id=i,x=float(p[0]),y=float(p[1])) for i,p in zip('abcd',frame)]
        rotation = np.array([[0.,1.],[-1.,0.]])
        actual, aligned = _align_and_frame((base@rotation).copy(), list('abcd'), reference[::-1])
        self.assertTrue(aligned); np.testing.assert_allclose(actual, frame)

    def test_bad_reference_rejected_even_when_not_enough_common_points(self):
        for reference in ([dict(id='a',x=float('nan'),y=0)], [dict(id='a',x=0,y=0)]*2, 'bad'):
            with self.assertRaises(ValueError):project_patents(rows(), reference=reference)


class EvaluationTests(unittest.TestCase):
    def test_human_signal_is_not_contaminated_by_llm_or_unreviewed_rows(self):
        data = rows()
        data[0].update(label='keep', label_source='human')
        data[1].update(label='exclude', label_source='human')
        data[2].update(label='keep', label_source='agent')
        data[3].update(label=None,label_source='agent',label_reason='情報不足',agent_confidence=.4)
        before = copy.deepcopy(data); e = compute_evaluation(data)
        self.assertEqual(e['signal_ratio'], .5); self.assertEqual(e['signal_noise_ratio'], 1)
        self.assertEqual(e['human_reviewed'], 2);self.assertEqual(e['machine_keep'],1)
        self.assertEqual(e['hold_count'], 1);self.assertEqual(e['unreviewed_count'], 2)
        self.assertEqual(data, before); self.assertIn('検索母集団全体', e['scope_note'])
        self.assertNotIn('recall', e)

    def test_zero_denominator_is_explicit_not_infinity(self):
        data = rows(); e = compute_evaluation(data)
        self.assertIsNone(e['signal_ratio']); self.assertEqual(e['sn_status'],'no_human_reviews')
        data[0].update(label='keep', label_source='human');e = compute_evaluation(data)
        self.assertEqual(e['signal_ratio'],1);self.assertIsNone(e['signal_noise_ratio'])
        self.assertEqual(e['sn_status'],'no_excludes');json.dumps(e,allow_nan=False)

    def test_unattributed_legacy_labels_are_not_called_human_or_llm(self):
        data = rows();data[0]['label']='keep'
        e = compute_evaluation(data)
        self.assertEqual(e['human_reviewed'],0);self.assertEqual(e['machine_keep'],0)
        self.assertEqual(e['unknown_source_count'],1);self.assertEqual(e['unreviewed_count'],5)

    def test_boundary_requires_both_human_classes_and_is_independent_of_2d(self):
        data = [dict(id='a',title='車両走行制御',label='keep',label_source='human'),
                dict(id='b',title='車両走行制御',label='exclude',label_source='human'),
                dict(id='c',title='車両走行制御',label=None)]
        e = compute_evaluation(data)
        c = next(c for c in e['boundary_candidates'] if c['id']=='c')
        self.assertEqual(c['kind'],'neighbor_disagreement')
        self.assertEqual(c['keep_neighbor'],'a');self.assertEqual(c['exclude_neighbor'],'b')
        for row in data:row.update(x=999,y=-999)
        self.assertEqual(e,compute_evaluation(data))
        data[1]['label_source']='agent'
        self.assertFalse(compute_evaluation(data)['boundary_candidates'])

    def test_model_uncertainty_and_llm_relevance_are_distinguished(self):
        data = rows();data[0].update(score=.5,score_source='lightweight')
        data[1].update(score=.5,score_source='llm',label='keep',label_source='agent')
        data[2].update(score=.5,score_source='transformer',label='keep',label_source='human')
        e = compute_evaluation(data)
        self.assertEqual([c['id'] for c in e['boundary_candidates']],['JP0'])
        self.assertEqual(e['boundary_candidates'][0]['kind'],'model_uncertainty')

    def test_target_coverage_uses_identifiers_and_is_not_called_recall(self):
        e = compute_evaluation(rows(),['JP0','missing','JP0'])
        self.assertEqual(e['target_count'],2);self.assertEqual(e['target_found'],1)
        self.assertEqual(e['missing_target_ids'],['missing'])

    def test_empty_evaluation_and_invalid_scores_are_json_safe(self):
        json.dumps(compute_evaluation([]),allow_nan=False)
        data=rows()
        for r,value in zip(data,[float('nan'),float('inf'),True,'0.5',-1,3]):r.update(score=value,score_source='lightweight')
        self.assertFalse(compute_evaluation(data)['boundary_candidates'])


if __name__ == '__main__':unittest.main()
