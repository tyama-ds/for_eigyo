"""prep（spec / 分割 / エンコーダ）と metrics のテスト（標準ライブラリのみ）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tcore import metrics as M  # noqa: E402
from tcore import prep  # noqa: E402
from tcore.catalog import HPARAMS, PRESETS, coerce_hparams, default_hparams  # noqa: E402

TABLE = {
    "name": "t.csv", "columns": ["text", "num", "cat", "y"],
    "rows": [[f"文章{i}", str(i), "A" if i % 2 else "B", str(i * 2.0)] for i in range(20)] + [["欠損行", "1", "A", ""]],
    "n_rows": 21,
}


class TestSpec(unittest.TestCase):
    def test_build_spec(self):
        spec = prep.build_spec(TABLE, {"target": "y", "task": "regression",
                                       "roles": {"text": "text", "num": "numeric", "cat": "categorical", "y": "numeric"}})
        self.assertEqual(spec["roles"]["y"], "target")
        self.assertEqual(spec["text_cols"], ["text"])
        self.assertEqual(spec["split"]["val"], 0.15)

    def test_errors(self):
        with self.assertRaises(prep.PrepError):
            prep.build_spec(TABLE, {"target": "nope"})
        with self.assertRaises(prep.PrepError):
            prep.build_spec(TABLE, {"target": "y", "roles": {}})            # 説明変数なし
        with self.assertRaises(prep.PrepError):
            prep.build_spec(TABLE, {"target": "y", "roles": {"num": "numeric"}, "split": {"val": 0.5, "test": 0.5}})

    def test_make_examples_drops_missing_target(self):
        spec = prep.build_spec(TABLE, {"target": "y", "task": "regression", "roles": {"text": "text", "num": "numeric"}})
        ex = prep.make_examples(TABLE, spec)
        self.assertEqual(len(ex), 20)
        self.assertEqual(ex[3]["y"], 6.0)
        self.assertEqual(ex[3]["num"], [3.0])
        ex2 = prep.make_examples(TABLE, spec, require_target=False)
        self.assertEqual(len(ex2), 21)

    def test_multi_text_join(self):
        spec = prep.build_spec(TABLE, {"target": "y", "task": "regression", "roles": {"text": "text", "cat": "text"}})
        ex = prep.make_examples(TABLE, spec)
        self.assertIn("text: 文章0", ex[0]["text"])
        self.assertIn("cat: B", ex[0]["text"])


class TestSplit(unittest.TestCase):
    def test_random_split(self):
        s = prep.split_indices(100, 0.2, 0.1, 1)
        self.assertEqual(len(s["train"]) + len(s["val"]) + len(s["test"]), 100)
        self.assertEqual(len(s["test"]), 10)
        self.assertEqual(len(s["val"]), 20)
        self.assertEqual(set(s["train"]) & set(s["val"]), set())
        self.assertEqual(s, prep.split_indices(100, 0.2, 0.1, 1))   # 決定論

    def test_stratified_keeps_rare_class_in_train(self):
        labels = ["A"] * 50 + ["B"] * 8 + ["C"] * 1
        s = prep.split_indices(len(labels), 0.2, 0.2, 3, labels)
        self.assertIn(58, s["train"])                                   # C は 1 件 → 学習へ
        train_labels = [labels[i] for i in s["train"]]
        self.assertEqual(set(train_labels), {"A", "B", "C"})
        self.assertEqual(len(s["train"]) + len(s["val"]) + len(s["test"]), 59)

    def test_no_val_no_test(self):
        s = prep.split_indices(10, 0, 0, 0)
        self.assertEqual(len(s["train"]), 10)


class TestEncoders(unittest.TestCase):
    def test_roundtrip(self):
        spec = prep.build_spec(TABLE, {"target": "cat", "task": "classification",
                                       "roles": {"text": "text", "num": "numeric", "y": "numeric"}})
        ex = prep.make_examples(TABLE, spec)
        pre = prep.Preproc(spec).fit(ex)
        self.assertEqual(sorted(pre.labels.classes), ["A", "B"])
        z = pre.num.transform([0.0, 10.0])
        self.assertEqual(len(z), 2)
        self.assertEqual(pre.num.transform([None, None]), [0.0, 0.0])
        d = pre.to_dict()
        pre2 = prep.Preproc.from_dict(d)
        self.assertEqual(pre2.labels.transform("B"), pre.labels.transform("B"))
        self.assertEqual(pre2.labels.transform("unknown"), -1)

    def test_categorical_vocab(self):
        v = prep.CategoricalVocab().fit([["x", "p"], ["y", "p"], ["x", ""]])
        self.assertEqual(v.cardinalities, [3, 2])
        self.assertEqual(v.transform(["x", "q"]), [1, prep.UNK])

    def test_target_scaler(self):
        t = prep.TargetScaler().fit([10.0, 20.0, 30.0])
        self.assertAlmostEqual(t.inverse(t.transform(25.0)), 25.0)

    def test_class_weights(self):
        w = prep.class_weights([0, 0, 0, 1], 2)
        self.assertGreater(w[1], w[0])
        self.assertAlmostEqual(sum(w) / 2, 1.0)


class TestMetrics(unittest.TestCase):
    def test_regression(self):
        m = M.regression_metrics([1, 2, 3, 4], [1.1, 1.9, 3.2, 3.8])
        self.assertAlmostEqual(m["mae"], 0.15, places=6)
        self.assertGreater(m["r2"], 0.95)
        self.assertAlmostEqual(m["pearson"], 0.99, delta=0.02)
        self.assertIsNotNone(m["mape"])
        perfect = M.regression_metrics([1, 2, 3], [1, 2, 3])
        self.assertEqual(perfect["rmse"], 0.0)
        self.assertEqual(perfect["r2"], 1.0)
        const = M.regression_metrics([2, 2, 2], [1, 2, 3])
        self.assertEqual(const["r2"], 0.0)

    def test_classification(self):
        y = [0, 0, 1, 1, 2]
        p = [0, 1, 1, 1, 2]
        probs = [[.8, .1, .1], [.4, .5, .1], [.1, .8, .1], [.2, .7, .1], [.1, .1, .8]]
        m = M.classification_metrics(y, p, ["a", "b", "c"], probs)
        self.assertAlmostEqual(m["accuracy"], 0.8)
        self.assertEqual(m["confusion"][0], [1, 1, 0])
        self.assertEqual(m["per_class"][2]["f1"], 1.0)
        self.assertGreater(m["log_loss"], 0)
        self.assertNotIn("auc", m)

    def test_binary_auc(self):
        m = M.classification_metrics([0, 0, 1, 1], [0, 0, 1, 1], ["neg", "pos"], [[.9, .1], [.6, .4], [.3, .7], [.2, .8]])
        self.assertEqual(m["auc"], 1.0)
        self.assertEqual(m["positive_class"], "pos")
        self.assertAlmostEqual(M.binary_auc([0, 1, 0, 1], [.5, .5, .5, .5]), 0.5)

    def test_primary_metric(self):
        name, v = M.primary_metric("regression", {"rmse": 2.0})
        self.assertEqual((name, v), ("rmse", -2.0))
        self.assertEqual(M.primary_metric("classification", {"f1_macro": 0.7}), ("f1_macro", 0.7))

    def test_downsample(self):
        self.assertEqual(len(M.downsample(list(range(5000)), 100)), 100)
        self.assertEqual(M.downsample([1, 2], 100), [1, 2])


class TestCatalog(unittest.TestCase):
    def test_hparams_coerce(self):
        hp = coerce_hparams("scratch", "classification", {"epochs": "5", "lr": "abc", "d_model": 99999, "tokenizer": "zzz"})
        self.assertEqual(hp["epochs"], 5)
        self.assertEqual(hp["lr"], default_hparams("scratch", "classification")["lr"])
        self.assertEqual(hp["d_model"], 1024)
        self.assertEqual(hp["tokenizer"], "auto")
        self.assertNotIn("loss", hp)                                     # 回帰専用は除外
        self.assertIn("class_weight", hp)
        self.assertEqual(coerce_hparams("baseline", "regression", {}), {})

    def test_presets_consistent(self):
        ids = [p["id"] for p in PRESETS]
        self.assertEqual(len(ids), len(set(ids)))
        for p in PRESETS:
            self.assertTrue(set(p["families"]) <= {"hf", "sbert"}, p["id"])
            self.assertIn(p["lang"], ("ja", "multi", "en"))
        for fam, fields in HPARAMS.items():
            keys = [f["key"] for f in fields]
            self.assertEqual(len(keys), len(set(keys)), fam)


if __name__ == "__main__":
    unittest.main()
