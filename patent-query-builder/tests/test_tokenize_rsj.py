import math
import unittest

from helpers import toy_docs
from pqb.stats.rsj import aggregate_code_levels, compute_stats, rank_candidates, rsj_weight
from pqb.stats.tokenize import candidate_terms, candidates_from_text, dedupe_by_postings, load_stopwords, term_postings


class TestTokenize(unittest.TestCase):
    def test_units_and_variants(self):
        toks = candidates_from_text("自動車部材に用いる高強度鋼板の製造方法。焼入れを行い、TIG-溶接する。ステンレス鋼板。")
        self.assertIn("高強度鋼板", toks)
        self.assertIn("鋼板", toks)
        self.assertIn("焼入れ", toks)                 # 送り仮名
        self.assertIn("ステンレス鋼板", toks)         # 隣接連結
        self.assertNotIn("鋼板の", toks)              # 助詞は付けない
        self.assertNotIn("焼入れを", toks)
        self.assertNotIn("製造方法", toks)            # 汎用語
        self.assertNotIn("造方法", toks)              # 汎用語の部分文字列も出さない
        self.assertNotIn("第2", candidates_from_text("第2実施形態"))

    def test_stopwords_loaded(self):
        sw = load_stopwords()
        self.assertIn("前記", sw)
        self.assertGreater(len(sw), 50)

    def test_dedupe_by_postings(self):
        docs, _ = toy_docs()
        post = term_postings(docs)
        dd = dedupe_by_postings(post)
        self.assertIn("鋼板", dd)
        self.assertIn("自動車部材", dd)
        self.assertNotIn("動車", dd)                  # 自動車部材 の内側でしか出ない
        terms = dict(candidate_terms(docs, min_tf=2))
        self.assertGreaterEqual(terms.get("焼入れ", 0), 2)


class TestRSJ(unittest.TestCase):
    def test_hand_calculation(self):
        # r=3, n=4, R=5, N=20
        expected = math.log(((3 + .5) * (20 - 4 - 5 + 3 + .5)) / ((4 - 3 + .5) * (5 - 3 + .5)))
        self.assertAlmostEqual(rsj_weight(3, 4, 5, 20), expected, places=6)
        self.assertLess(rsj_weight(0, 10, 5, 20), 0)

    def test_rank_candidates_and_levels(self):
        docs, rel = toy_docs()
        judged = [(d, rel[d.doc_id]) for d in docs]
        res = rank_candidates(judged, query_terms={"鋼板", "焼入れ"})
        feats = [t["feature"] for t in res["terms"]]
        self.assertIn("自動車部材", feats)
        self.assertTrue(all(t["w"] > 0 for t in res["terms"]))
        codes = {(c["scheme"], c["feature"]) for c in res["codes"]}
        self.assertIn(("FI", "C22C38/04"), codes)
        self.assertTrue(all(c["ow"] > 0 for c in res["codes"]))
        neg = [t["feature"] for t in res["negative_terms"]]
        self.assertIn("圧延", neg)
        std = [t for t in res["terms"] if t["feature"] == "鋼板"]
        self.assertTrue(std and std[0]["in_query"])

    def test_sample_disagreement_flag(self):
        docs, rel = toy_docs()
        judged = [(d, rel[d.doc_id]) for d in docs]
        # 標本内だけ逆の判定にして符号不一致を作る
        sample = {d.doc_id for d in docs[:12]}
        flipped = [(d, (not r) if d.doc_id in sample else r) for d, r in judged]
        stats = compute_stats(flipped, {"自動車部材", "圧延"}, None, sample)
        by = {s.feature: s for s in stats if s.kind == "term"}
        self.assertIsNotNone(by["自動車部材"].w_sample)
        self.assertTrue(any(s.needs_review for s in stats))

    def test_aggregate_levels_prefers_discriminative(self):
        docs, rel = toy_docs()
        judged = [(d, rel[d.doc_id]) for d in docs]
        stats = compute_stats(judged)
        agg = aggregate_code_levels(stats, ratio=0.5)
        chosen = {(s.scheme, s.feature) for s in agg}
        # C22C38/04 系列は 1 つの粒度だけが選ばれる
        self.assertEqual(len([c for c in chosen if c[0] == "FI" and c[1].startswith("C22C")]), 1)


if __name__ == "__main__":
    unittest.main()
