"""データ拡張（LLM 知識蒸留）のテスト: 配分・統計・JSON 解釈・検証・内蔵生成・モック LLM・学習への統合。"""
from __future__ import annotations

import json
import math
import os
import random
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
from tcore.engine.baseline import train_baseline  # noqa: E402
from tcore.engine.datasetup import prepare  # noqa: E402
from tcore.jobs import Job, JobCancelled  # noqa: E402
from tcore.llm import LLMClient, LLMError, is_local_host, strip_think, validate_base_url  # noqa: E402
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
        huge = augment.plan_allocation(counts, 0, "custom", custom={"A": 20000, "B": 20000, "C": 20000})
        self.assertLessEqual(sum(huge.values()), augment.MAX_TOTAL)
        self.assertEqual(augment.plan_allocation(counts, float("inf"), "balance"), augment.plan_allocation(counts, 0, "balance"))
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
        self.assertIsNone(augment.coerce_params({"temperature": "abc"})["temperature"])
        self.assertIsNone(augment.coerce_params({"temperature": 5})["temperature"])
        self.assertEqual(augment.coerce_params({"n_total": float("inf")})["n_total"], 200)
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
                                        "評価": " 普通 "}, spec, t["columns"], prof)
        self.assertIsNone(why)
        self.assertEqual(ok[t["columns"].index("評価")], "普通")
        mis, why = augment.validate_row({"レビュー本文": "最高でした。", "価格": 1, "購入回数": 1, "評価": "高評価"}, spec,
                                        t["columns"], prof)
        self.assertIsNone(mis)
        self.assertIn("不一致", why)                                        # LLM が別ラベルを返した行は却下
        nolabel, why = augment.validate_row({"レビュー本文": "普通の商品でした。", "価格": 1, "購入回数": 1}, spec, t["columns"], prof)
        self.assertIsNone(why)                                              # 目的変数が無ければグループラベルを付与
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
        self.assertTrue(d.is_dup("掃除機を購入。とても使いやすいですね。"))    # 3-gram Jaccard >= 0.85
        self.assertFalse(d.is_dup("掃除機を購入。とても使いにくいです。"))
        strict = augment.Deduper(threshold=0.99)
        strict.add("掃除機を購入。とても使いやすいです。")
        self.assertFalse(strict.is_dup("掃除機を購入。とても使いやすいですね。"))

    def test_dedupe_against_real_rows_with_two_text_columns(self):
        """テキスト列が 2 つでも、実データそのままのコピーは重複として却下される。"""
        t = imbalanced_reviews()
        spec2 = dict(SPEC, roles={"レビュー本文": "text", "商品": "text", "カテゴリ": "categorical", "価格": "numeric", "購入回数": "numeric"})
        sp = build_spec(t, spec2)
        cols = t["columns"]
        copies = [{c: r[cols.index(c)] for c in cols} for r in t["rows"] if r[-1] == "普通"]
        orig_chat = augment.LLMClient.chat
        augment.LLMClient.chat = lambda self, user, **kw: json.dumps({"rows": copies[:10]}, ensure_ascii=False)
        try:
            res = augment.run_augmentation(t, spec2, {"n_total": 20, "mode": "custom", "custom": {"普通": 20}, "seed": 1},
                                           Job("a", {}), {"llm_provider": "openai", "llm_model": "m", "use_proxy": False,
                                                          "llm_base_url": "http://127.0.0.1:9/v1"})
        finally:
            augment.LLMClient.chat = orig_chat
        self.assertEqual(len(res["rows"]), 0)
        self.assertGreater(res["rejected"].get("重複（実データ/生成済み）", 0), 0)
        self.assertEqual(sp["text_cols"], ["レビュー本文", "商品"])

    def test_profile_uses_train_rows_only(self):
        t = imbalanced_reviews()
        spec = build_spec(t, SPEC)
        tg = augment.target_groups(t, spec)
        allowed = augment.train_indices(tg["examples"], spec)
        b = prepare(t, {"family": "baseline", "spec": SPEC})
        self.assertEqual(allowed, set(b["split"]["train"]))                 # prepare と同じ分割
        prof = augment.class_profile(tg["groups"][2], spec, tg["examples"], random.Random(0), allowed=allowed)
        holdout = {tg["examples"][i]["text"] for i in tg["groups"][2]["idx"] if i not in allowed}
        self.assertTrue(holdout)
        for e in prof["examples"]:
            self.assertNotIn(e["text"], holdout)
        self.assertLess(prof["count"], tg["groups"][2]["count"])
        res = augment.run_augmentation(t, SPEC, {"n_total": 60, "mode": "balance", "seed": 3}, Job("a", {}),
                                       {"llm_provider": "builtin"})
        holdout_sents = set()
        for i in range(len(tg["examples"])):
            if i not in allowed:
                holdout_sents.update(s for s in tg["examples"][i]["text"].replace("。", "。\n").split("\n") if len(s) > 12)
        leaked = [r for r in res["rows"] if any(sent in r[0] for sent in holdout_sents if sent not in
                                                {s for j in allowed for s in tg["examples"][j]["text"].replace("。", "。\n").split("\n")})]
        self.assertEqual(leaked, [])

    def test_class_profile_and_prompt(self):
        t = imbalanced_reviews()
        spec = build_spec(t, SPEC)
        tg = augment.target_groups(t, spec)
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
        vs, vu = augment.build_verify_prompt([{"id": "ROW-A", "レビュー本文": "x"}], spec, ["a", "b"])
        self.assertIn('"_id": 0', vu)                                       # 特徴列名 'id' と衝突しない

    def test_example_obj_multiline_text(self):
        t = imbalanced_reviews()
        t2 = {**t, "rows": [list(r) for r in t["rows"]]}
        t2["rows"][0][0] = "1 行目: テスト\n2 行目も: ある"
        spec2 = build_spec(t2, dict(SPEC, roles={"レビュー本文": "text", "商品": "text", "価格": "numeric"}))
        tg = augment.target_groups(t2, spec2)
        obj = augment._example_obj(tg["examples"][0], spec2)
        self.assertEqual(obj["レビュー本文"], "1 行目: テスト\n2 行目も: ある")
        self.assertEqual(obj["商品"], t["rows"][0][2])


class TestSpecKey(unittest.TestCase):
    def test_spec_key_depends_on_roles(self):
        t = imbalanced_reviews()
        k1 = augment.spec_key(build_spec(t, SPEC))
        k2 = augment.spec_key(build_spec(t, dict(SPEC, roles={**SPEC["roles"], "商品": "categorical"})))
        k3 = augment.spec_key(build_spec(t, dict(SPEC, split={"val": 0.3, "test": 0.0, "seed": 5})))
        self.assertNotEqual(k1, k2)
        self.assertEqual(k1, k3)                                            # 分割は含めない
        self.assertTrue(k1.startswith("評価|classification|"))


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

    def test_exhausted_group_is_cut_off(self):
        t = imbalanced_reviews({"高評価": 10 ** 9, "普通": 3, "低評価": 40})
        job = Job("a", {})
        res = augment.run_augmentation(t, SPEC, {"n_total": 0, "mode": "custom", "custom": {"普通": 600}, "seed": 1},
                                       job, {"llm_provider": "builtin"})
        g = next(g for g in res["groups"] if g["label"] == "普通")
        self.assertLess(g["added"], g["alloc"])
        self.assertTrue(any("打ち切り" in line for line in job.log_lines))
        self.assertEqual(sum(1 for line in job.log_lines if "打ち切り" in line), 1)
        self.assertGreater(res["rejected"].get("重複（実データ/生成済み）", 0), 0)

    def test_builtin_is_reproducible(self):
        t = imbalanced_reviews()
        a = augment.run_augmentation(t, SPEC, {"n_total": 40, "mode": "balance", "seed": 9, "concurrency": 4},
                                     Job("a", {}), {"llm_provider": "builtin"})
        b = augment.run_augmentation(t, SPEC, {"n_total": 40, "mode": "balance", "seed": 9, "concurrency": 4},
                                     Job("b", {}), {"llm_provider": "builtin"})
        self.assertEqual(a["rows"], b["rows"])

    def test_student_check(self):
        t = imbalanced_reviews()
        run = train_baseline(t, {"family": "baseline", "spec": SPEC}, Job("t", {}))
        rid = run["run_id"]
        # ベースラインは常に多数派「高評価」を予測 → 少数クラスの合成行はすべて不一致
        drop = augment.run_augmentation(t, SPEC, {"n_total": 40, "seed": 1, "student_run": rid, "student_policy": "drop"},
                                        Job("a", {}), {"llm_provider": "builtin"})
        self.assertEqual(len(drop["rows"]), 0)
        self.assertGreater(drop["rejected"].get("生徒モデルの予測不一致", 0), 0)
        keep = augment.run_augmentation(t, SPEC, {"n_total": 40, "seed": 1, "student_run": rid, "student_policy": "keep"},
                                        Job("a", {}), {"llm_provider": "builtin"})
        self.assertGreater(len(keep["rows"]), 0)
        for m in keep["meta"]:
            self.assertFalse(m["checks"]["student_agree"])
            self.assertEqual(m["checks"]["student_pred"], "高評価")
            self.assertIsNotNone(m["checks"]["student_conf"])
        job = Job("a", {})
        skip = augment.run_augmentation(t, SPEC, {"n_total": 40, "seed": 1, "student_run": "r00000000-000000-zzzz"},
                                        job, {"llm_provider": "builtin"})
        self.assertGreater(len(skip["rows"]), 0)
        self.assertTrue(any("スキップ" in line for line in job.log_lines))
        cars = dataio.read_table_bytes((SAMPLES / "used_cars_ja.csv").read_bytes(), "used_cars_ja.csv")
        run_r = train_baseline(cars, {"family": "baseline", "spec": SPEC_REG}, Job("t", {}))
        reg = augment.run_augmentation(cars, SPEC_REG, {"n_total": 30, "mode": "equal", "seed": 1,
                                                        "student_run": run_r["run_id"], "student_policy": "keep"},
                                       Job("a", {}), {"llm_provider": "builtin"})
        self.assertTrue(all("student_agree" in m["checks"] for m in reg["meta"]))

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
        MockState.reset()

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
        # 形式不正 / 切断 / 非 http スキーム もすべて LLMError
        MockState.malformed = True
        with self.assertRaises(LLMError) as cm:
            c.chat("x")
        self.assertIn("形式が想定外", str(cm.exception))
        MockState.malformed = False
        MockState.disconnect_next = 1
        with self.assertRaises(LLMError):
            c.chat("x")
        with self.assertRaises(LLMError):
            LLMClient(self.cfg(llm_base_url="file:///etc/passwd"))
        with self.assertRaises(LLMError):
            validate_base_url("ftp://x")
        self.assertTrue(is_local_host("http://127.0.0.1:11434/v1"))
        self.assertTrue(is_local_host("http://localhost:1234"))
        self.assertTrue(is_local_host("http://192.168.1.5:8000"))
        self.assertFalse(is_local_host("https://api.openai.com/v1"))

    def test_response_format_retry(self):
        MockState.reject_response_format = True
        c = LLMClient(self.cfg())
        text = c.chat("生成 **2 件** 各行のキー: a", json_mode=True)
        self.assertIn("rows", text)
        self.assertEqual(len(MockState.calls), 2)
        self.assertIn("response_format", MockState.calls[0]["body"])
        self.assertNotIn("response_format", MockState.calls[1]["body"])
        self.assertEqual(c.stats["calls"], 1)

    def test_content_parts_and_local_proxy_bypass(self):
        self.assertEqual(LLMClient._content_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]), "ab")
        self.assertEqual(LLMClient._content_text(None), "")
        with self.assertRaises(LLMError):
            LLMClient._content_text(123)
        # ループバック宛てはプロキシ設定があっても直結される
        c = LLMClient(self.cfg(use_proxy=True, proxy_url="http://127.0.0.1:9"))
        self.assertIn("rows", c.chat("x"))

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
        MockState.malformed = True
        with self.assertRaises(LLMError) as cm:
            c.chat("x")
        self.assertIn("形式が想定外", str(cm.exception))

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
        verify_calls = [c for c in MockState.calls if "厳格な審査員" in json.dumps(c["body"], ensure_ascii=False)]
        self.assertTrue(gen_calls)
        self.assertEqual(gen_calls[0]["body"].get("response_format"), {"type": "json_object"})
        self.assertEqual(len(verify_calls), math.ceil(len(res["rows"]) / 10))
        self.assertEqual(res["llm_stats"]["calls"], len(MockState.calls))
        self.assertTrue(all(m["checks"]["teacher_label"] for m in res["meta"]))

    def test_verify_failure_keeps_rows_unverified(self):
        t = imbalanced_reviews()
        MockState.verify_fail = True
        job = Job("a", {})
        res = augment.run_augmentation(t, SPEC, {"n_total": 30, "mode": "balance", "verify_llm": True, "seed": 1},
                                       job, self.cfg())
        self.assertGreater(len(res["rows"]), 0)
        self.assertTrue(all(m["checks"]["teacher_agree"] is None for m in res["meta"]))
        self.assertTrue(any("検証呼び出しに失敗" in line for line in job.log_lines))

    def test_regression_mock_skips_teacher_verification(self):
        cars = dataio.read_table_bytes((SAMPLES / "used_cars_ja.csv").read_bytes(), "used_cars_ja.csv")
        res = augment.run_augmentation(cars, SPEC_REG, {"n_total": 30, "mode": "equal", "verify_llm": True, "seed": 1},
                                       Job("a", {}), self.cfg())
        self.assertGreater(len(res["rows"]), 0)
        self.assertFalse(any("厳格な審査員" in json.dumps(c["body"], ensure_ascii=False) for c in MockState.calls))
        self.assertTrue(all("teacher_agree" not in m["checks"] for m in res["meta"]))
        ti = cars["columns"].index("価格_万円")
        ranges = {g["key"]: g for g in augment.target_groups(cars, build_spec(cars, SPEC_REG))["groups"]}
        for r, m in zip(res["rows"], res["meta"], strict=True):
            lo, hi = ranges[m["group"]]["range"]
            self.assertTrue(lo <= float(r[ti]) <= hi)

    def test_transient_failures_are_retried_and_persistent_failures_abort_with_partial_result(self):
        t = imbalanced_reviews()
        MockState.fail_next = 2                                            # 最初の 2 回は 500 → 再試行で成功
        job = Job("a", {})
        res = augment.run_augmentation(t, SPEC, {"n_total": 30, "mode": "balance", "seed": 1, "concurrency": 1}, job, self.cfg())
        self.assertGreaterEqual(len(res["rows"]), 25)
        self.assertIsNone(res["aborted"])
        self.assertEqual(sum(1 for line in job.log_lines if "生成エラー" in line), 2)
        # 成功後に恒久的に失敗 → 連続失敗で打ち切り、採用済み行は保持
        MockState.reset()
        MockState.fail_next = 10 ** 6

        orig = augment.LLMClient.chat
        state = {"n": 0}

        def flaky(self, user, **kw):
            state["n"] += 1
            if state["n"] <= 1:
                return orig(self, user, **kw) if False else json.dumps({"rows": [{"レビュー本文": f"最初だけ成功 {i} 回目の文です。", "カテゴリ": "家電", "価格": 100 + i, "購入回数": 1, "評価": "普通"} for i in range(10)]}, ensure_ascii=False)
            raise LLMError("down")
        augment.LLMClient.chat = flaky
        try:
            job = Job("a", {})
            res = augment.run_augmentation(t, SPEC, {"n_total": 200, "mode": "custom", "custom": {"普通": 200}, "seed": 1,
                                                     "concurrency": 1}, job, self.cfg())
        finally:
            augment.LLMClient.chat = orig
        self.assertEqual(len(res["rows"]), 10)
        self.assertIsNotNone(res["aborted"])
        self.assertIn("打ち切り", res["aborted"])

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

    def test_extract_json_is_fast_on_pathological_input(self):
        import time
        t0 = time.time()
        self.assertIsNone(augment.extract_json("{" * 50000))
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(augment.extract_json("x" * 10 + '{"a": [1, 2]} trailing'), {"a": [1, 2]})


if __name__ == "__main__":
    unittest.main()
