"""データ拡張（LLM 知識蒸留）のテスト: 配分・統計・JSON 解釈・検証・内蔵生成・モック LLM・学習への統合。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
_TMP = tempfile.mkdtemp(prefix="tensorium-aug-")
os.environ.setdefault("TENSORIUM_DATA_DIR", _TMP)
os.environ.setdefault("TENSORIUM_CONFIG_FILE", str(Path(_TMP) / "config.json"))

from mock_llm import MockState, start_mock_server  # noqa: E402

from tcore import augment, dataio  # noqa: E402
from tcore.engine.datasetup import prepare  # noqa: E402
from tcore.jobs import Job, JobCancelled  # noqa: E402
from tcore.llm import LLMClient, LLMError, strip_think  # noqa: E402
from tcore.prep import PrepError, build_spec  # noqa: E402

SAMPLES = ROOT / "sample_data"
SPEC = {"target": "評価", "task": "classification",
        "roles": {"レビュー本文": "text", "カテゴリ": "categorical", "商品": "ignore", "価格": "numeric", "購入回数": "numeric"},
        "split": {"val": 0.2, "test": 0.1, "seed": 1}}
SPEC_REG = {"target": "価格_万円", "task": "regression",
            "roles": {"車両説明": "text", "メーカー": "categorical", "年式": "numeric", "走行距離_万km": "numeric"},
            "split": {"val": 0.2, "test": 0.1, "seed": 1}}


def imbalanced_reviews(limits=None) -> dict:
    t = dataio.read_table_bytes((SAMPLES / "reviews_ja.csv").read_bytes(), "reviews_ja.csv")
    limits = limits or {"高評価": 10 ** 9, "普通": 20, "低評価": 40}
    seen = dict.fromkeys(limits, 0)
    rows = []
    for r in t["rows"]:
        if seen[r[-1]] < limits[r[-1]]:
            rows.append(r)
            seen[r[-1]] += 1
    return {**t, "rows": rows, "n_rows": len(rows)}


class TestPlanning(unittest.TestCase):
    def test_balance_stats(self):
        st = augment.balance_stats([100, 100, 100])
        self.assertEqual(st["ratio"], 1.0)
        self.assertAlmostEqual(st["entropy"], 1.0)
        self.assertAlmostEqual(st["gini"], 0.0)
        st2 = augment.balance_stats([300, 10])
        self.assertEqual(st2["ratio"], 30.0)
        self.assertLess(st2["entropy"], 0.3)
        self.assertGreater(st2["gini"], 0.4)
        self.assertIsNone(augment.balance_stats([5, 0])["ratio"])
        self.assertEqual(augment.balance_stats([])["k"], 0)

    def test_allocation_modes(self):
        counts = {"A": 263, "B": 40, "C": 20}
        bal = augment.plan_allocation(counts, 100, "balance", cap=True)
        self.assertEqual(sum(bal.values()), 100)
        self.assertEqual(bal["A"], 0)
        self.assertGreater(bal["C"], bal["B"])
        full = augment.plan_allocation(counts, 1000, "balance", cap=True)
        self.assertEqual(full, {"A": 0, "B": 223, "C": 243})           # 上限 = 最多クラスまで
        nocap = augment.plan_allocation(counts, 1000, "balance", cap=False)
        self.assertEqual(sum(nocap.values()), 1000)
        eq = augment.plan_allocation(counts, 100, "equal")
        self.assertEqual(sum(eq.values()), 100)
        self.assertEqual(eq["C"], 34)                                    # 余りは少ないクラスへ
        cus = augment.plan_allocation(counts, 0, "custom", custom={"A": "5", "C": 7, "zzz": 9})
        self.assertEqual(cus, {"A": 5, "B": 0, "C": 7})
        already = augment.plan_allocation({"A": 10, "B": 10}, 10, "balance")
        self.assertEqual(sum(already.values()), 10)                      # 既に均衡 → 均等
        self.assertEqual(augment.plan_allocation({}, 10), {})
        self.assertLessEqual(sum(augment.plan_allocation(counts, 10 ** 9, "equal").values()), augment.MAX_TOTAL)

    def test_target_groups_and_plan_payload(self):
        t = imbalanced_reviews()
        spec = build_spec(t, SPEC)
        tg = augment.target_groups(t, spec)
        self.assertEqual([g["label"] for g in tg["groups"]], ["高評価", "低評価", "普通"])
        counts = {g["key"]: g["count"] for g in tg["groups"]}
        alloc = augment.plan_allocation(counts, 200, "balance")
        payload = augment.plan_payload(tg["groups"], alloc)
        self.assertEqual(payload["n_alloc"], 200)
        self.assertGreater(payload["stats_after"]["entropy"], payload["stats_before"]["entropy"])
        self.assertLess(payload["stats_after"]["ratio"], payload["stats_before"]["ratio"])

    def test_regression_bins(self):
        cars = dataio.read_table_bytes((SAMPLES / "used_cars_ja.csv").read_bytes(), "used_cars_ja.csv")
        tg = augment.target_groups(cars, build_spec(cars, SPEC_REG))
        self.assertEqual(len(tg["groups"]), augment.N_BINS)
        self.assertTrue(all(g["range"] for g in tg["groups"]))
        self.assertEqual(sum(g["count"] for g in tg["groups"]), len(tg["examples"]))

    def test_coerce_params(self):
        p = augment.coerce_params({"n_total": "999999", "mode": "zzz", "batch_rows": 0, "concurrency": 99,
                                   "student_policy": "keep", "temperature": "", "instructions": "x" * 9000})
        self.assertEqual(p["n_total"], augment.MAX_TOTAL)
        self.assertEqual(p["mode"], "balance")
        self.assertEqual(p["batch_rows"], 1)
        self.assertEqual(p["concurrency"], 8)
        self.assertEqual(p["student_policy"], "keep")
        self.assertIsNone(p["temperature"])
        self.assertEqual(len(p["instructions"]), 4000)


class TestParsingAndValidation(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertEqual(augment.parse_rows('{"rows": [{"a": 1}]}'), [{"a": 1}])
        self.assertEqual(augment.parse_rows('```json\n{"rows": [{"a": 1}, {"a": 2}]}\n```'), [{"a": 1}, {"a": 2}])
        self.assertEqual(augment.parse_rows('前置き。[{"x": "y"}] 後書き'), [{"x": "y"}])
        self.assertEqual(augment.parse_rows('{"data": [{"k": "v"}]}'), [{"k": "v"}])
        self.assertEqual(augment.parse_rows('{"a": "単体オブジェクト"}'), [{"a": "単体オブジェクト"}])
        self.assertEqual(augment.parse_rows("壊れた {\"rows\": [ ..."), [])
        self.assertEqual(augment.parse_rows(""), [])
        self.assertEqual(augment.parse_rows('{"rows": [1, 2, {"ok": true}]}'), [{"ok": True}])

    def test_strip_think(self):
        self.assertEqual(strip_think("<think>考え中</think>{\"a\":1}"), '{"a":1}')
        self.assertEqual(strip_think("途中</think>本文"), "本文")
        self.assertEqual(strip_think("本文<think>途切れ"), "本文")
        self.assertEqual(strip_think("a<think>x</think>b<think>y</think>c"), "abc")

    def test_validate_row(self):
        t = imbalanced_reviews()
        spec = build_spec(t, SPEC)
        tg = augment.target_groups(t, spec)
        import random
        g = tg["groups"][2]                                             # 普通
        prof = augment.class_profile(g, spec, tg["examples"], random.Random(0))
        ok, why = augment.validate_row({"レビュー本文": "普通の商品でした。", "カテゴリ": "食品", "価格": "1,200", "購入回数": 2.7,
                                        "評価": "高評価"}, spec, t["columns"], prof)
        self.assertIsNone(why)
        self.assertEqual(ok[t["columns"].index("評価")], "普通")            # ラベルはグループで強制
        self.assertEqual(ok[t["columns"].index("購入回数")], "3")           # 整数列は丸める
        self.assertEqual(ok[t["columns"].index("商品")], "")                # 使わない列は空
        bad, why = augment.validate_row({"レビュー本文": "", "価格": 1}, spec, t["columns"], prof)
        self.assertIsNone(bad)
        self.assertIn("テキスト", why)
        # 数値が範囲外ならクリップされる
        big, _ = augment.validate_row({"レビュー本文": "とても長い本文です。", "価格": 10 ** 9, "購入回数": 1}, spec, t["columns"], prof)
        self.assertEqual(float(big[t["columns"].index("価格")]), prof["num"][0]["max"])
        neg, _ = augment.validate_row({"レビュー本文": "とても長い本文です。", "価格": -5, "購入回数": -3}, spec, t["columns"], prof)
        self.assertGreaterEqual(float(neg[t["columns"].index("価格")]), prof["num"][0]["min"])
        self.assertGreaterEqual(float(neg[t["columns"].index("購入回数")]), 1)

    def test_validate_regression_range(self):
        cars = dataio.read_table_bytes((SAMPLES / "used_cars_ja.csv").read_bytes(), "used_cars_ja.csv")
        spec = build_spec(cars, SPEC_REG)
        tg = augment.target_groups(cars, spec)
        import random
        prof = augment.class_profile(tg["groups"][0], spec, tg["examples"], random.Random(0))
        lo, hi = prof["range"]
        ok, why = augment.validate_row({"車両説明": "2015年式 トヨタ アクア", "メーカー": "トヨタ", "年式": 2015, "走行距離_万km": 5,
                                        "価格_万円": (lo + hi) / 2}, spec, cars["columns"], prof)
        self.assertIsNone(why)
        bad, why = augment.validate_row({"車両説明": "x y", "メーカー": "トヨタ", "年式": 2015, "走行距離_万km": 5,
                                         "価格_万円": hi * 50}, spec, cars["columns"], prof)
        self.assertIn("範囲外", why)

    def test_deduper(self):
        d = augment.Deduper()
        d.add("掃除機を購入。とても使いやすいです。")
        self.assertTrue(d.is_dup("掃除機を購入。とても使いやすいです。"))
        self.assertTrue(d.is_dup("掃除機を購入！ とても使いやすいです"))   # 記号・空白の違いは同一視
        self.assertFalse(d.is_dup("炊飯器は期待外れでがっかりしました。"))
        self.assertFalse(d.is_dup(""))

    def test_class_profile_and_prompt(self):
        t = imbalanced_reviews()
        spec = build_spec(t, SPEC)
        tg = augment.target_groups(t, spec)
        import random
        prof = augment.class_profile(tg["groups"][1], spec, tg["examples"], random.Random(0))
        self.assertEqual(prof["label"], "低評価")
        self.assertEqual(len(prof["examples"]), augment.FEWSHOT)
        self.assertEqual([s["col"] for s in prof["num"]], ["価格", "購入回数"])
        self.assertTrue(prof["num"][1]["integer_like"])
        self.assertTrue(prof["keywords"])
        system, user = augment.build_generation_prompt(prof, spec, 7, ["高評価", "低評価", "普通"], "丁寧語で")
        self.assertIn("**7 件**", user)
        self.assertIn("今回生成するクラス: 「低評価」", user)
        self.assertIn("丁寧語で", user)
        self.assertIn("各行のキー: レビュー本文, 価格, 購入回数, カテゴリ, 評価", user)
        self.assertIn("JSON", system)


class TestBuiltinRun(unittest.TestCase):
    def test_builtin_balances_and_train_only(self):
        t = imbalanced_reviews()
        job = Job("augment", {})
        res = augment.run_augmentation(t, SPEC, {"n_total": 150, "mode": "balance", "seed": 3}, job,
                                       {"llm_provider": "builtin"})
        self.assertGreaterEqual(len(res["rows"]), 120)                 # 重複除去で少し減る
        self.assertEqual(res["provider"], "builtin")
        self.assertGreater(res["stats_after"]["entropy"], res["stats_before"]["entropy"])
        self.assertEqual(sum(1 for m in res["meta"] if m["label"] == "高評価"), 0)
        for r in res["rows"]:
            self.assertEqual(len(r), len(t["columns"]))
            self.assertIn(r[-1], ("普通", "低評価"))
        self.assertTrue(any("均衡度" in line for line in job.log_lines))
        # 学習側: 合成行は train のみ
        combined = {**t, "rows": t["rows"] + res["rows"], "n_rows": len(t["rows"]) + len(res["rows"]),
                    "synthetic_from": len(t["rows"])}
        b0 = prepare(t, {"family": "baseline", "spec": SPEC})
        b1 = prepare(combined, {"family": "baseline", "spec": SPEC})
        self.assertEqual(sorted(b0["split"]["val"]), sorted(b1["split"]["val"]))
        self.assertEqual(sorted(b0["split"]["test"]), sorted(b1["split"]["test"]))
        self.assertEqual(len(b1["split"]["train"]), len(b0["split"]["train"]) + len(res["rows"]))
        self.assertEqual(b1["n_synthetic"], len(res["rows"]))
        self.assertEqual(b1["n_rows"], len(t["rows"]))

    def test_zero_allocation_error_and_cancel(self):
        t = imbalanced_reviews()
        with self.assertRaises(PrepError):
            augment.run_augmentation(t, SPEC, {"n_total": 0}, Job("a", {}), {"llm_provider": "builtin"})
        job = Job("a", {})
        job._cancel.set()
        with self.assertRaises(JobCancelled):
            augment.run_augmentation(t, SPEC, {"n_total": 100}, job, {"llm_provider": "builtin"})

    def test_builtin_regression(self):
        cars = dataio.read_table_bytes((SAMPLES / "used_cars_ja.csv").read_bytes(), "used_cars_ja.csv")
        res = augment.run_augmentation(cars, SPEC_REG, {"n_total": 40, "mode": "equal", "seed": 1}, Job("a", {}),
                                       {"llm_provider": "builtin"})
        self.assertGreaterEqual(len(res["rows"]), 30)
        ti = cars["columns"].index("価格_万円")
        keys = {g["key"] for g in res["groups"]}
        for r, m in zip(res["rows"], res["meta"], strict=True):
            self.assertIn(m["group"], keys)
            self.assertIsNotNone(dataio.parse_number(r[ti]))


class TestMockLLM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_mock_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        MockState.calls.clear()
        MockState.fail_next = 0
        MockState.verify_label = None
        MockState.garbage = False

    def cfg(self, **kw):
        base = {"llm_provider": "openai", "llm_base_url": self.base + "/v1", "llm_model": "mock-model",
                "llm_api_key": "", "llm_max_tokens": 512, "llm_timeout": 20, "llm_temperature": 0.5, "use_proxy": False}
        base.update(kw)
        return base

    def test_openai_chat_and_errors(self):
        c = LLMClient(self.cfg())
        text = c.chat("こんにちは", system="sys")
        self.assertIn("rows", text)
        self.assertEqual(c.stats["calls"], 1)
        self.assertGreater(c.stats["input_tokens"], 0)
        self.assertEqual(MockState.calls[-1]["body"]["model"], "mock-model")
        self.assertEqual(MockState.calls[-1]["body"]["messages"][0]["role"], "system")
        MockState.fail_next = 1
        with self.assertRaises(LLMError) as cm:
            c.chat("x")
        self.assertIn("500", str(cm.exception))
        with self.assertRaises(LLMError):
            LLMClient(self.cfg(llm_model="")).chat("x")
        with self.assertRaises(LLMError):
            LLMClient(self.cfg(llm_base_url="http://127.0.0.1:9/v1")).chat("x")
        self.assertTrue(c.test()["ok"])

    def test_anthropic_chat(self):
        c = LLMClient(self.cfg(llm_provider="anthropic", llm_base_url=self.base, llm_api_key="test-key"))
        text = c.chat("生成して **3 件** 各行のキー: a, b", system="s")
        self.assertIn("rows", text)
        call = MockState.calls[-1]
        self.assertTrue(call["path"].endswith("/v1/messages"))
        self.assertEqual(call["headers"].get("anthropic-version"), "2023-06-01")
        self.assertEqual(call["body"]["system"], "s")
        with self.assertRaises(LLMError) as cm:
            LLMClient(self.cfg(llm_provider="anthropic", llm_base_url=self.base, llm_api_key="wrong")).chat("x")
        self.assertIn("認証", str(cm.exception))

    def test_run_with_mock_llm_and_verification(self):
        t = imbalanced_reviews()
        job = Job("augment", {})
        res = augment.run_augmentation(t, SPEC, {"n_total": 60, "mode": "balance", "batch_rows": 10, "concurrency": 2,
                                                 "verify_llm": True, "dedupe": True, "seed": 1}, job, self.cfg())
        self.assertEqual(res["provider"], "openai")
        self.assertEqual(res["model"], "mock-model")
        self.assertGreaterEqual(len(res["rows"]), 50)
        self.assertTrue(all(m["checks"].get("teacher_agree") is True for m in res["meta"]))
        gen_calls = [c for c in MockState.calls if "厳格な審査員" not in json.dumps(c["body"], ensure_ascii=False)]
        self.assertTrue(gen_calls)
        self.assertEqual(gen_calls[0]["body"].get("response_format"), {"type": "json_object"})
        self.assertGreater(res["llm_stats"]["calls"], len(gen_calls) - 1)

    def test_verification_rejects_mismatch(self):
        t = imbalanced_reviews()
        MockState.verify_label = "高評価"                                 # 教師がすべて「高評価」と判定 → 全部不一致
        res = augment.run_augmentation(t, SPEC, {"n_total": 30, "mode": "balance", "verify_llm": True, "seed": 1},
                                       Job("a", {}), self.cfg())
        self.assertEqual(len(res["rows"]), 0)
        self.assertGreater(res["rejected"].get("教師 LLM のラベル不一致", 0), 0)

    def test_garbage_response_gives_clear_error(self):
        t = imbalanced_reviews()
        MockState.garbage = True
        job = Job("a", {})
        with self.assertRaises(PrepError) as cm:
            augment.run_augmentation(t, SPEC, {"n_total": 30, "mode": "balance", "seed": 1}, job, self.cfg())
        self.assertIn("失敗", str(cm.exception))
        self.assertTrue(any("JSON" in line for line in job.log_lines))


if __name__ == "__main__":
    unittest.main()
