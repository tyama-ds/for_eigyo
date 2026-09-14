import unittest

from helpers import sample_query, toy_docs
from pqb.core.dsl import Block, Code, Query, Term
from pqb.core.variants import build_variants, choose_broad_drop
from pqb.learn.boolean import DecisionTree, tree_transforms
from pqb.learn.cal import CALModel, cal_round
from pqb.learn.transforms import Transform, TransformError, apply_transform, direction_of, local_evaluate, propose_from_stats
from pqb.stats.rsj import rank_candidates


class TestTransforms(unittest.TestCase):
    def setUp(self):
        self.q = sample_query()

    def test_apply_each_op(self):
        q = apply_transform(self.q, Transform("ADD_TERM", {"axis_id": "A", "text": "ハイテン"}, source="rsj"), 2)
        self.assertIn("ハイテン", [t.text for t in q.block("A").adopted_terms()])
        self.assertTrue(q.block("A").terms[-1].origin.startswith("rsj:iter2"))
        q = apply_transform(q, Transform("DROP_TERM", {"axis_id": "A", "text": "ハイテン"}))
        self.assertNotIn("ハイテン", [t.text for t in q.block("A").adopted_terms()])
        q = apply_transform(q, Transform("ADD_CODE", {"axis_id": "B", "scheme": "FI", "code": "C21D1/18"}))
        self.assertEqual(q.block("B").adopted_codes()[0].code, "C21D1/18")
        q = apply_transform(q, Transform("CODE_LEVEL_UP", {"axis_id": "A", "scheme": "FI", "code": "C22C38/00"}))
        self.assertEqual([c.code for c in q.block("A").adopted_codes()], ["C22C"])
        q = apply_transform(q, Transform("CODE_LEVEL_DOWN", {"axis_id": "A", "scheme": "FI", "code": "C22C", "new_code": "C22C38/04"}))
        self.assertEqual([c.code for c in q.block("A").adopted_codes()], ["C22C38/04"])
        q = apply_transform(q, Transform("ADD_AXIS", {"axis_id": "C", "name": "用途", "terms": [{"text": "自動車部材"}]}))
        self.assertEqual(len(q.active_blocks()), 3)
        q = apply_transform(q, Transform("TERM_CODE_JOIN", {"axis_id": "A", "join": "AND"}))
        self.assertEqual(q.block("A").effective_join(), "AND")
        q = apply_transform(q, Transform("FIELD_CHANGE", {"axis_id": "B", "fields": ["TX"]}))
        self.assertEqual(q.block("B").adopted_terms()[0].fields, ["TX"])
        q = apply_transform(q, Transform("SPLIT_BLOCK", {"axis_id": "A", "terms": ["高張力鋼板"], "new_axis_id": "A2"}))
        self.assertIsNotNone(q.block("A2"))
        q = apply_transform(q, Transform("MERGE_BLOCK", {"axis_id": "A", "other_axis_id": "A2"}))
        self.assertIsNone(q.block("A2"))
        q = apply_transform(q, Transform("DROP_AXIS", {"axis_id": "C"}))
        self.assertIsNone(q.block("C"))
        q = apply_transform(q, Transform("PROXIMITY_ON", {"axis_id": "A", "distance": 3}))
        self.assertEqual(q.block("A").proximity, 3)
        with self.assertRaises(TransformError):
            apply_transform(q, Transform("ADD_EXCLUSION", {"terms": [{"text": "アルミ"}]}))
        q = apply_transform(q, Transform("ADD_EXCLUSION", {"terms": [{"text": "アルミ"}]}), exclusions_enabled=True)
        self.assertEqual(len(q.active_exclusions()), 1)
        with self.assertRaises(TransformError):
            apply_transform(q, Transform("DROP_TERM", {"axis_id": "A", "text": "無い語"}))
        with self.assertRaises(TransformError):
            apply_transform(q, Transform("DROP_AXIS", {"axis_id": "ZZ"}))
        self.assertEqual(direction_of("TERM_CODE_JOIN", {"join": "AND"}), "narrow")
        self.assertEqual(direction_of("FIELD_CHANGE", {"axis_id": "B", "fields": ["TX"]}, self.q), "widen")

    def test_local_evaluate_and_regression(self):
        docs, rel = toy_docs()
        q = Query(query_id="q", case_id="c", blocks=[Block("A", terms=[Term("鋼板", origin="input")]), Block("B", terms=[Term("焼入れ")])])
        pool = {d.doc_id for d in docs if rel[d.doc_id]}
        t = local_evaluate(Transform("ADD_AXIS", {"axis_id": "C", "terms": [{"text": "自動車部材"}]}, direction="narrow"), q, docs, pool, rel)
        self.assertTrue(t.local_eval)
        self.assertEqual(t.pred_recall_pool, 1.0)
        self.assertFalse(t.regression)
        # プールの文献を落とす変換は退行として棄却
        t2 = local_evaluate(Transform("ADD_AXIS", {"axis_id": "C", "terms": [{"text": "ホットスタンプ"}]}), q, docs, pool, rel)
        self.assertTrue(t2.regression)
        self.assertEqual(t2.status, "rejected")
        t3 = local_evaluate(Transform("DROP_TERM", {"axis_id": "A", "text": "存在しない"}), q, docs, pool, rel)
        self.assertEqual(t3.status, "invalid")

    def test_propose_from_stats(self):
        docs, rel = toy_docs()
        q = Query(query_id="q", case_id="c", blocks=[Block("A", terms=[Term("鋼板", origin="input"), Term("圧延", origin="llm:P2")]), Block("B", terms=[Term("焼入れ", origin="input")])])
        judged = [(d, rel[d.doc_id]) for d in docs]
        stats = rank_candidates(judged, query_terms={"鋼板", "焼入れ", "圧延"})
        ts = propose_from_stats(q, stats, [{"axis_id": "C", "name": "用途", "terms": [{"text": "自動車部材"}], "codes": []}], 1,
                                axis_terms={"C": ["自動車部材"]})
        ops = {(t.op, t.target.get("text") or t.target.get("axis_id")) for t in ts}
        self.assertIn(("DROP_TERM", "圧延"), ops)
        self.assertNotIn(("DROP_TERM", "鋼板"), ops)          # 入力由来は守る
        self.assertIn(("ADD_AXIS", "C"), ops)
        self.assertTrue(any(t.op == "ADD_CODE" or t.op.startswith("CODE_LEVEL") for t in ts))
        add_terms = [t for t in ts if t.op == "ADD_TERM"]
        self.assertTrue(all(t.target["axis_id"] in ("A", "B") for t in add_terms))


class TestTreeAndCAL(unittest.TestCase):
    def test_tree_finds_feature(self):
        X = [{("term", "a")} if i % 2 == 0 else {("term", "b")} for i in range(20)]
        y = [i % 2 == 0 for i in range(20)]
        tree = DecisionTree(max_depth=2, min_samples_leaf=2).fit(X, y, [("term", "a"), ("term", "b")])
        paths = tree.positive_paths()
        self.assertEqual(len(paths), 1)
        self.assertTrue(tree.predict({("term", "a")}))
        docs, rel = toy_docs()
        q = Query(query_id="q", case_id="c", blocks=[Block("A", terms=[Term("鋼板")]), Block("B", terms=[Term("焼入れ")])])
        ts, dnf = tree_transforms([(d, rel[d.doc_id]) for d in docs], q)
        self.assertTrue(dnf)
        self.assertTrue(all(t.source == "tree" for t in ts))

    def test_cal_ranks_positives(self):
        docs, rel = toy_docs()
        labels = {d.doc_id: rel[d.doc_id] for d in docs[:20]}
        res = cal_round(docs, labels, {"epochs": 6}, k=5)
        self.assertTrue(res["ok"])
        top = sorted(res["scores"].items(), key=lambda x: -x[1])[:10]
        self.assertTrue(all(rel[d] for d, _ in top))
        self.assertTrue(all(b not in labels for b in res["batch"]))
        self.assertTrue(res["top_features"])
        res2 = cal_round(docs, {d.doc_id: True for d in docs[:6] if rel[d.doc_id]}, {"epochs": 3}, k=3)
        self.assertTrue(res2["provisional_negatives"])
        m = CALModel({"epochs": 2}).fit(docs[:10], [rel[d.doc_id] for d in docs[:10]])
        self.assertTrue(0 <= m.score(docs[0]) <= 1)


class TestVariants(unittest.TestCase):
    def test_build_variants_rules(self):
        case = {"case_id": "C", "purpose": "prior_art", "date_from": "2000-01-01", "countries": ["JP"]}
        axes = [{"axis_id": "A", "name": "対象", "kind": "required"}, {"axis_id": "B", "name": "手段", "kind": "required"},
                {"axis_id": "C", "name": "用途", "kind": "auxiliary"}]
        cands = [{"kind": "term", "axis_id": "A", "value": "高強度鋼板", "status": "adopted", "origin": "input"},
                 {"kind": "term", "axis_id": "A", "value": "棄却語", "status": "rejected", "origin": "llm:P2"},
                 {"kind": "code", "axis_id": "A", "value": "C22C38/04", "scheme": "FI", "status": "adopted", "origin": "seed"},
                 {"kind": "term", "axis_id": "B", "value": "焼入れ", "status": "adopted", "origin": "input"},
                 {"kind": "term", "axis_id": "C", "value": "自動車部材", "status": "adopted", "origin": "input"}]
        self.assertEqual(choose_broad_drop(axes, cands), "B")            # コードを持つ A を残す
        qs, notes = build_variants(case, axes, cands, 1)
        self.assertEqual(notes["broad_drop_axis"], "B")
        self.assertEqual([b.axis_id for b in qs["broad"].active_blocks()], ["A"])
        self.assertEqual(qs["broad"].blocks[0].adopted_codes()[0].code, "C22C38/00")      # 一段粗く
        self.assertEqual(qs["broad"].blocks[0].adopted_terms()[0].fields, ["TX"])
        self.assertEqual([b.axis_id for b in qs["standard"].active_blocks()], ["A", "B"])
        self.assertEqual(qs["standard"].blocks[0].adopted_terms()[0].fields, ["AB", "CL"])
        self.assertEqual([b.axis_id for b in qs["narrow"].active_blocks()], ["A", "B", "C"])
        self.assertEqual(qs["narrow"].blocks[0].effective_join(), "AND")
        self.assertEqual(qs["narrow"].blocks[1].effective_join(), "OR")
        self.assertIn("棄却語", [t.text for t in qs["standard"].blocks[0].terms])        # 履歴として残る
        self.assertEqual(qs["standard"].query_id, "C/v1/standard")
        self.assertIsNone(choose_broad_drop(axes[:1], cands))
        qs2, _ = build_variants(case, axes, cands, 2, broad_drop_axis="A", parent_ids={"standard": "C/v1/standard"})
        self.assertEqual([b.axis_id for b in qs2["broad"].active_blocks()], ["B"])
        self.assertEqual(qs2["standard"].provenance["parent_query_id"], "C/v1/standard")


if __name__ == "__main__":
    unittest.main()
