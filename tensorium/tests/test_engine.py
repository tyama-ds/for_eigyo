"""torch 系ファミリーの学習・保存・予測テスト。torch が無ければ skip。

HF Hub には接続しない: hf / sbert はテスト内で作る極小 BERT（ランダム初期化）をローカルパスで使う。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="tensorium-engine-")
os.environ["TENSORIUM_DATA_DIR"] = _TMP
os.environ["TENSORIUM_CONFIG_FILE"] = str(Path(_TMP) / "config.json")
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

try:
    import torch  # noqa: F401
    HAVE_TORCH = True
except ImportError:
    HAVE_TORCH = False
try:
    import transformers  # noqa: F401
    HAVE_TF = True
except ImportError:
    HAVE_TF = False

from tcore import dataio  # noqa: E402
from tcore.jobs import Job, JobCancelled  # noqa: E402

SAMPLES = ROOT / "sample_data"
SPEC_CLS = {"target": "評価", "task": "classification",
            "roles": {"レビュー本文": "text", "カテゴリ": "categorical", "商品": "ignore", "価格": "numeric", "購入回数": "numeric"},
            "split": {"val": 0.2, "test": 0.1, "seed": 1}}
SPEC_REG = {"target": "価格_万円", "task": "regression",
            "roles": {"車両説明": "text", "メーカー": "categorical", "年式": "numeric", "走行距離_万km": "numeric",
                      "排気量_cc": "numeric", "色": "categorical", "車検": "categorical"},
            "split": {"val": 0.2, "test": 0.1, "seed": 1}}


def load(name):
    return dataio.read_table_bytes((SAMPLES / name).read_bytes(), name)


def make_tiny_bert(out: Path, texts: list[str]) -> str:
    """ランダム初期化の極小 BERT + 文字単位 WordPiece トークナイザをローカルに保存。"""
    from tokenizers import BertWordPieceTokenizer
    from transformers import BertConfig, BertModel, PreTrainedTokenizerFast

    out.mkdir(parents=True, exist_ok=True)
    chars = sorted({ch for t in texts for ch in t if not ch.isspace()})
    vocab = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"] + chars + [f"##{c}" for c in chars]
    (out / "vocab.txt").write_text("\n".join(vocab) + "\n", encoding="utf-8")
    bwp = BertWordPieceTokenizer(vocab=str(out / "vocab.txt"), lowercase=False)
    tok = PreTrainedTokenizerFast(tokenizer_object=bwp._tokenizer, unk_token="[UNK]", pad_token="[PAD]",
                                  cls_token="[CLS]", sep_token="[SEP]", mask_token="[MASK]", model_max_length=128)
    tok.save_pretrained(str(out))
    cfg = BertConfig(vocab_size=len(vocab), hidden_size=32, num_hidden_layers=1, num_attention_heads=2,
                     intermediate_size=64, max_position_embeddings=128)
    BertModel(cfg).save_pretrained(str(out))
    return str(out)


@unittest.skipUnless(HAVE_TORCH, "torch が無いため skip")
class TestTorchFamilies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reviews = load("reviews_ja.csv")
        cls.cars = load("used_cars_ja.csv")
        cls.tmp = Path(tempfile.mkdtemp(prefix="tensorium-tiny-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        shutil.rmtree(_TMP, ignore_errors=True)

    def _train(self, table, req):
        from tcore.engine.pipeline import train_run
        job = Job("train", req)
        res = train_run(table, req, job)
        self.assertIn("run_id", res)
        return res, job

    def _roundtrip(self, run_id, table, n=5):
        from tcore.engine.predictor import Predictor
        p = Predictor(run_id)
        small = {"name": "x", "columns": table["columns"], "rows": table["rows"][:n], "n_rows": n}
        out = p.predict_table(small)
        self.assertEqual(len(out["predictions"]), n)
        return p, out

    def test_scratch_classification_learns(self):
        req = {"family": "scratch", "spec": SPEC_CLS,
               "hparams": {"epochs": 8, "batch_size": 32, "lr": 1e-3, "d_model": 64, "nhead": 4, "layers": 2, "ff_dim": 128,
                           "early_stopping": 0}}
        res, job = self._train(self.reviews, req)
        m = res["metrics"]["val"]
        self.assertGreater(m["accuracy"], 0.6, "文字 Transformer がテンプレ文を学習できていない")
        self.assertEqual(len(job.curves), 8)
        self.assertTrue(job.step_losses)
        p, out = self._roundtrip(res["run_id"], self.reviews)
        self.assertIn(out["predictions"][0]["pred"], p.classes)
        self.assertEqual(len(out["actual"]), 5)

    def test_tabular_regression(self):
        req = {"family": "tabular", "spec": SPEC_REG, "hparams": {"epochs": 25, "batch_size": 64, "lr": 2e-3, "early_stopping": 0}}
        res, _ = self._train(self.cars, req)
        m = res["metrics"]["val"]
        self.assertGreater(m["r2"], 0.3)
        p, out = self._roundtrip(res["run_id"], self.cars)
        self.assertIsInstance(out["predictions"][0]["pred"], float)

    def test_early_stopping_and_cancel(self):
        req = {"family": "scratch", "spec": SPEC_CLS,
               "hparams": {"epochs": 30, "batch_size": 64, "lr": 1e-3, "d_model": 16, "nhead": 2, "layers": 1, "ff_dim": 32,
                           "early_stopping": 1}}
        res, job = self._train(self.reviews, req)
        self.assertLess(len(job.curves), 30)
        # 中止
        from tcore.engine.pipeline import train_run
        job2 = Job("train", req)
        job2._cancel.set()
        with self.assertRaises(JobCancelled):
            train_run(self.reviews, req, job2)

    def test_bad_hparams_message(self):
        from tcore.engine.pipeline import train_run
        from tcore.prep import PrepError
        req = {"family": "scratch", "spec": SPEC_CLS, "hparams": {"epochs": 1, "d_model": 30, "nhead": 4}}
        with self.assertRaises(PrepError):
            train_run(self.reviews, req, Job("t", req))

    @unittest.skipUnless(HAVE_TF, "transformers が無いため skip")
    def test_hf_finetune_local_model(self):
        path = make_tiny_bert(self.tmp / "tiny", [r[0] for r in self.reviews["rows"]])
        req = {"family": "hf", "model": path, "spec": SPEC_CLS,
               "hparams": {"epochs": 1, "batch_size": 32, "lr": 1e-3, "max_len": 48, "early_stopping": 0, "head_hidden": "16"}}
        res, job = self._train(self.reviews, req)
        self.assertIn("accuracy", res["metrics"]["val"])
        from tcore.runs import run_dir
        d = run_dir(res["run_id"])
        self.assertTrue((d / "encoder" / "config.json").exists())
        self.assertTrue((d / "head.pt").exists())
        p, out = self._roundtrip(res["run_id"], self.reviews)
        self.assertAlmostEqual(sum(out["predictions"][0]["probs"]), 1.0, places=4)

    @unittest.skipUnless(HAVE_TF, "transformers が無いため skip")
    def test_sbert_head_regression(self):
        path = make_tiny_bert(self.tmp / "tiny2", [r[0] for r in self.cars["rows"]])
        req = {"family": "sbert", "model": path, "spec": SPEC_REG,
               "hparams": {"epochs": 15, "batch_size": 64, "lr": 2e-3, "max_len": 48, "early_stopping": 0, "head_hidden": "32"}}
        res, job = self._train(self.cars, req)
        self.assertGreater(res["metrics"]["val"]["r2"], 0.2)      # 表形式特徴の融合だけでも学習できる
        p, out = self._roundtrip(res["run_id"], self.cars)
        self.assertEqual(len(out["predictions"]), 5)

    def test_model_load_error_is_explained(self):
        from tcore.engine.pipeline import train_run
        from tcore.engine.text import ModelLoadError
        req = {"family": "hf", "model": str(self.tmp / "does-not-exist"), "spec": SPEC_CLS, "hparams": {"epochs": 1}}
        if not HAVE_TF:
            self.skipTest("transformers なし")
        with self.assertRaises(ModelLoadError) as cm:
            train_run(self.reviews, req, Job("t", req))
        self.assertIn("does-not-exist", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
