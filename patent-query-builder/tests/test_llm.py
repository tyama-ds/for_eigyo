import tempfile
import unittest
from pathlib import Path

from pqb.config import load_config
from pqb.llm import mock
from pqb.llm.adapter import (LLMAdapter, LLMError, Masker, PendingConfirmation, PendingManualResponse, code_support,
                             extract_json, load_schema, majority_vote, render_prompt)
from pqb.llm.schema import validate
from pqb.store.db import Store

TEXT = "本発明は、自動車部材向けの高強度鋼板の製造方法に関する。鋼板を加熱した後に焼入れを行い、焼戻しにより靭性を確保する。これにより耐水素脆化特性が向上する。"


def _axes():
    return [{"axis_id": "A", "name": "対象物", "kind": "required", "definition": "", "terms": ["高強度鋼板"]},
            {"axis_id": "B", "name": "手段", "kind": "required", "definition": "", "terms": ["焼入れ"]},
            {"axis_id": "C", "name": "用途", "kind": "auxiliary", "definition": "", "terms": ["自動車部材"]}]


class TestSchema(unittest.TestCase):
    def test_validate(self):
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 3},
                                                                       "b": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
                                                                       "c": {"type": "string", "enum": ["x", "y"]}}}
        self.assertEqual(validate({"a": 1, "b": ["p"], "c": "x"}, schema), [])
        errs = validate({"a": 5, "b": ["p", "q", "r"], "c": "z"}, schema)
        self.assertEqual(len(errs), 3)
        self.assertTrue(validate({}, schema))
        self.assertTrue(validate({"a": True}, schema))            # bool は integer ではない
        pat = {"type": "object", "patternProperties": {".*": {"type": "integer", "minimum": 0, "maximum": 3}}}
        self.assertEqual(validate({"A": 3}, pat), [])
        self.assertTrue(validate({"A": 4}, pat))


class TestHelpers(unittest.TestCase):
    def test_render_prompt_and_extract(self):
        text = render_prompt("P4", {"axes": _axes(), "overall_rule": "min", "doc": {"doc_id": "D1"}})
        self.assertIn('"axis_id": "A"', text)
        self.assertNotIn("[[", text)
        self.assertEqual(extract_json('<think>x</think>ここ ```json\n{"a": 1}\n``` 末尾'), {"a": 1})
        self.assertEqual(extract_json('前置き {"a": {"b": [1,2]}} 後置き'), {"a": {"b": [1, 2]}})
        with self.assertRaises(LLMError):
            extract_json("json なし")

    def test_majority_and_support(self):
        samples = [{"overall": 2, "per_axis": {"A": 3, "B": 2}}, {"overall": 2, "per_axis": {"A": 3, "B": 1}}, {"overall": 3, "per_axis": {"A": 3, "B": 3}}]
        chosen, flip = majority_vote("P4", samples)
        self.assertEqual(chosen["overall"], 2)
        self.assertAlmostEqual(flip, 1 / 3, places=3)
        self.assertEqual(chosen["per_axis"]["A"], 3)
        s3 = [{"codes": [{"axis_id": "A", "scheme": "FI", "code": "X"}]}, {"codes": [{"axis_id": "A", "scheme": "FI", "code": "X"}, {"axis_id": "A", "scheme": "FI", "code": "Y"}]}]
        sup = code_support(s3)
        self.assertEqual(sup[("A", "FI", "X")], 1.0)
        self.assertEqual(sup[("A", "FI", "Y")], 0.5)

    def test_masker(self):
        st = Store(":memory:")
        st.create_case(case_id="C", name="n", purpose="prior_art", input_text="", seeds=[])
        m = Masker("C", st, ["社名:日本製鉄", "田中"], True)
        masked = m.mask("日本製鉄の田中が引張強度1200MPaの鋼板")
        self.assertNotIn("日本製鉄", masked)
        self.assertNotIn("1200", masked)
        self.assertEqual(m.unmask({"x": masked})["x"], "日本製鉄の田中が引張強度1200MPaの鋼板")
        self.assertEqual(st.get_masking("C")["【社名1】"], "日本製鉄")


class TestMockSchemas(unittest.TestCase):
    def test_all_prompts_valid(self):
        axes = _axes()
        cases = {
            "P1": {"input_text": TEXT, "purpose_label": "先行技術調査", "seed_docs": []},
            "P2": {"axes": axes, "terms_by_axis": {"A": ["高強度鋼板"], "B": ["焼入れ"]}, "relevant_docs": [{"doc_id": "JP1", "title": "高張力鋼板のホットスタンプ", "abstract": "焼入れ後に焼戻し"}], "synonyms_hint": {}},
            "P3": {"axes": axes, "terms_by_axis": {"A": ["高強度鋼板"]}, "seed_code_counts": {"FI": [{"code": "C22C38/00", "count": 2, "title": "鉄合金"}]}, "seed_count": 2, "known_codes": []},
            "P4": {"axes": axes, "overall_rule": "min_required", "doc": {"doc_id": "D1", "title": "高強度鋼板の焼入れ方法", "abstract": "自動車部材"}},
            "P5": {"dsl_summary": {"blocks": [{"axis_id": "A", "terms": ["高強度鋼板"], "codes": []}]}, "axes": axes,
                   "stats": {"terms": [{"feature": "高張力鋼板", "r": 3, "R": 5, "w": 2.0, "ow": 6.0}], "negative_terms": [{"feature": "高強度鋼板", "n": 9, "w": -1.0}], "codes": [{"scheme": "FI", "feature": "C21D1/18", "r": 3, "R": 5, "ow": 4.0, "title": ""}], "negative_codes": []}, "ops": []},
            "P6": {"axes": axes, "candidates": [{"kind": "term", "axis_id": "A", "value": "高張力鋼板", "origin": "llm:P2", "N": 10, "R": 4, "r": 3, "rsj_w": 1.2}]},
        }
        for pid, inputs in cases.items():
            out = mock.generate(pid, inputs)
            self.assertEqual(validate(out, load_schema(pid)), [], pid)
        p1 = mock.generate("P1", cases["P1"])
        self.assertEqual(p1["axes"][0]["terms"], ["高強度鋼板"])
        self.assertTrue(any(a["terms"] == ["焼入れ"] for a in p1["axes"]))
        p4 = mock.generate("P4", cases["P4"])
        self.assertEqual(p4["overall"], 3)      # A=3, B=3（名称に出現）→ 必須の最小値
        p4b = mock.generate("P4", {"axes": axes, "overall_rule": "min_required", "doc": {"doc_id": "D2", "title": "高強度鋼板の圧延方法", "abstract": "冷間圧延"}})
        self.assertEqual(p4b["overall"], 0)     # B（焼入れ）が無い


class TestAdapter(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.st = Store(":memory:")
        self.st.create_case(case_id="C1", name="n", purpose="prior_art", input_text=TEXT, seeds=[])

    def test_mock_records_calls_and_budget(self):
        ad = LLMAdapter(self.cfg, self.st, "C1")
        r = ad.complete("P1", {"input_text": TEXT, "purpose_label": "x", "seed_docs": []})
        self.assertEqual(r.mode, "mock")
        self.assertEqual(self.st.count_llm_calls("C1"), 1)
        cfg = dict(self.cfg, budget={"llm_calls": 1})
        with self.assertRaises(LLMError):
            LLMAdapter(cfg, self.st, "C1").complete("P1", {"input_text": TEXT, "purpose_label": "x", "seed_docs": []})

    def test_flip_rate_with_jitter(self):
        mock.JITTER = 0.9
        try:
            ad = LLMAdapter(self.cfg, self.st, "C1")
            flips = []
            for i in range(6):
                r = ad.complete("P4", {"axes": _axes(), "overall_rule": "min", "doc": {"doc_id": f"D{i}", "title": "高強度鋼板の焼入れ", "abstract": "自動車部材"}})
                flips.append(r.flip_rate)
            self.assertTrue(any(f > 0 for f in flips))
            self.assertTrue(any(f > self.cfg["flip_threshold"] for f in flips))
        finally:
            mock.JITTER = 0.0

    def test_manual_mode_pending_then_answer(self):
        cfg = dict(self.cfg, llm=dict(self.cfg["llm"], mode="manual"))
        with tempfile.TemporaryDirectory() as tmp:
            ad = LLMAdapter(cfg, self.st, "C1", prompts_dir=Path(tmp))
            inputs = {"input_text": TEXT, "purpose_label": "x", "seed_docs": []}
            with self.assertRaises(PendingManualResponse) as ctx:
                ad.complete("P1", inputs)
            prompts = ctx.exception.prompts
            self.assertEqual(len(prompts), 1)
            self.assertTrue(list((Path(tmp) / "prompts_out" / "C1").glob("*.md")))
            # 不正な返答はスキーマエラー
            self.st.answer_manual_prompt(prompts[0]["call_key"], '{"axes": []}')
            with self.assertRaises(LLMError):
                ad.complete("P1", inputs)
            # 正しい返答
            answer = mock.generate("P1", inputs)
            import json
            self.st.answer_manual_prompt(prompts[0]["call_key"], json.dumps(answer, ensure_ascii=False))
            r = ad.complete("P1", inputs)
            self.assertEqual(r.mode, "manual")
            self.assertEqual(r.majority["axes"][0]["axis_id"], "A")
            # ファイル経由（prompts_in）
            ad2 = LLMAdapter(cfg, self.st, "C1", prompts_dir=Path(tmp))
            with self.assertRaises(PendingManualResponse) as ctx2:
                ad2.complete("P4", {"axes": _axes(), "overall_rule": "min", "doc": {"doc_id": "D1", "title": "t"}}, n=1)
            key = ctx2.exception.prompts[0]["call_key"].replace(":", "_").replace("/", "_")
            (Path(tmp) / "prompts_in" / "C1" / f"{key}.json").write_text('{"per_axis": {"A": 1}, "overall": 1, "rationale": "x"}', encoding="utf-8")
            r = ad2.complete("P4", {"axes": _axes(), "overall_rule": "min", "doc": {"doc_id": "D1", "title": "t"}}, n=1)
            self.assertEqual(r.majority["overall"], 1)

    def test_production_guards(self):
        cfg = dict(self.cfg, offline=False, production=True, llm=dict(self.cfg["llm"], mode="browser"))
        with self.assertRaises(LLMError):
            LLMAdapter(cfg, self.st, "C1")
        cfg = dict(self.cfg, offline=False, production=True, llm=dict(self.cfg["llm"], mode="api"))
        ad = LLMAdapter(cfg, self.st, "C1")
        with self.assertRaises(PendingConfirmation):
            ad.complete("P1", {"input_text": TEXT, "purpose_label": "x", "seed_docs": []})
        with self.assertRaises(LLMError):        # 承認後: base_url 未設定で接続エラー（ネットワークは呼ばない）
            ad.complete("P1", {"input_text": TEXT, "purpose_label": "x", "seed_docs": []}, confirmed=True)

    def test_offline_forces_mock(self):
        cfg = dict(self.cfg, offline=True, llm=dict(self.cfg["llm"], mode="api"))
        self.assertEqual(LLMAdapter(cfg, self.st, "C1").mode, "mock")


if __name__ == "__main__":
    unittest.main()
