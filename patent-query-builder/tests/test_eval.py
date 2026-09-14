import random
import unittest

from pqb.eval.metrics import evaluate, is_pareto_improvement, pareto_front, satisfies_constraints, select_by_policy
from pqb.eval.sampling import draw_sample, estimate_recall, stratified_sample, wilson_interval


class TestMetrics(unittest.TestCase):
    def test_evaluate(self):
        m = evaluate({"a", "b", "c"}, 300, ["a", "b", "x"], {"a", "b"}, {"a", "c", "z"}, {"a": True, "b": False}, k=30, complexity=5)
        self.assertEqual(m.recall_seed, 1.0)
        self.assertAlmostEqual(m.recall_pool, 2 / 3)
        self.assertEqual(m.pool_missing, ["z"])
        self.assertEqual(m.p_at_k, 0.5)
        self.assertEqual(m.hit_count, 300)
        self.assertFalse(satisfies_constraints(m, 0.95))
        m2 = evaluate({"a", "b"}, None, [], {"a"}, set(), {}, k=10)
        self.assertEqual(m2.hit_count, 2)
        self.assertIsNone(m2.recall_pool)
        self.assertTrue(satisfies_constraints(m2, 0.95))

    def test_pareto(self):
        items = [{"id": 1, "hit_count": 100, "p_at_k": 0.5, "complexity": 3},
                 {"id": 2, "hit_count": 200, "p_at_k": 0.4, "complexity": 3},
                 {"id": 3, "hit_count": 150, "p_at_k": 0.9, "complexity": 9}]
        front = pareto_front(items)
        self.assertEqual({i["id"] for i in front}, {1, 3})
        self.assertTrue(is_pareto_improvement(items[0], items[1]))
        self.assertFalse(is_pareto_improvement(items[1], items[0]))
        pick = select_by_policy([dict(i, recall_pool=1.0, variant=str(i["id"])) for i in front], "range_then_simple", [120, 200])
        self.assertEqual(pick["id"], 3)
        pick = select_by_policy([dict(i, recall_pool=1.0) for i in front], "recall_first", [120, 200])
        self.assertEqual(pick["id"], 1)


class TestSampling(unittest.TestCase):
    def test_wilson_known_value(self):
        lo, hi = wilson_interval(27, 30)
        self.assertAlmostEqual(lo, 0.7438, places=3)
        self.assertAlmostEqual(hi, 0.9654, places=3)
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_estimate(self):
        est = estimate_recall({f"D{i}": i < 30 for i in range(60)}, {f"D{i}" for i in range(27)}, 1000)
        self.assertEqual((est.m, est.s, est.t), (60, 30, 27))
        self.assertAlmostEqual(est.recall_hat, 0.9)
        self.assertFalse(est.low_confidence)
        self.assertAlmostEqual(est.est_relevant_total, 500.0)
        self.assertTrue(estimate_recall({"a": True}, {"a"}, 10).low_confidence)

    def test_draw_sample_deterministic(self):
        ids = [f"D{i}" for i in range(100)]
        a = draw_sample(ids, 10, seed=42, exclude={"D1"})
        b = draw_sample(ids, 10, seed=42, exclude={"D1"})
        self.assertEqual(a, b)
        self.assertNotIn("D1", a)
        s = stratified_sample({"x": ids[:50], "y": ids[50:]}, 10, seed=1)
        self.assertEqual(len(s), 10)

    def test_synthetic_coverage(self):
        """正解を知っている合成母集団で、95% Wilson 区間が真値を含む頻度 ≥ 90%（§12.2）。"""
        rng = random.Random(7)
        N, hits = 2000, 0
        trials = 200
        for t in range(trials):
            relevant = {f"D{i}": (i % 4 == 0) for i in range(N)}          # 25% が適合
            true_recall = 0.8
            q_hits = {d for d, r in relevant.items() if r and rng.random() < true_recall}
            sample = draw_sample(list(relevant), 240, seed=t)
            est = estimate_recall({d: relevant[d] for d in sample}, q_hits, N)
            actual = len(q_hits) / sum(relevant.values())
            if est.ci_low <= actual <= est.ci_high:
                hits += 1
        self.assertGreaterEqual(hits / trials, 0.90)


if __name__ == "__main__":
    unittest.main()
