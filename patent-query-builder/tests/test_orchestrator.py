import json
import unittest

from helpers import SAMPLE_DIR, sample_script
from pqb.config import load_config
from pqb.gates.human import ScriptedHumanDecider
from pqb.llm import mock
from pqb.llm.adapter import PendingManualResponse
from pqb.orchestrator import Orchestrator, OrchestratorError
from pqb.report.build import build_html, build_markdown
from pqb.store.db import Store
from pqb.ui.excel import design_workbook, read_xlsx, write_xlsx


class TestSemiAuto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st = Store(":memory:")
        cls.orc = Orchestrator(cls.st)
        cls.case = cls.orc.load_sample_case()
        cls.cid = cls.case["case_id"]
        cls.summary = cls.orc.run_case(cls.cid, ScriptedHumanDecider(sample_script()))
        cls.bundle = cls.orc.case_bundle(cls.cid)

    def test_finalized_with_two_iterations(self):
        self.assertEqual(self.summary["status"], "finalized")
        self.assertEqual(self.summary["iteration"], 2)
        std = self.summary["variants"]["standard"]
        self.assertEqual(std["recall_seed"], 1.0)
        self.assertEqual(std["recall_pool"], 1.0)
        self.assertGreater(std["hit_count"], 0)
        self.assertEqual(self.summary["pareto"]["recommended"], "standard")

    def test_three_variants_rendered_and_roundtrip(self):
        latest = [q for q in self.bundle["queries"] if q["iteration"] == 2]
        self.assertEqual(sorted(q["variant"] for q in latest), ["broad", "narrow", "standard"])
        for q in latest:
            dialects = {r["dialect"] for r in q["renderings"]}
            self.assertEqual(dialects, {"generic", "jplatpat"})
            self.assertTrue(all(r["roundtrip_ok"] for r in q["renderings"]))
            self.assertTrue(q["parent_query_id"].endswith(f"/v1/{q['variant']}"))
        jp = next(r for r in latest if r["variant"] == "standard")["renderings"]
        text = next(r["text"] for r in jp if r["dialect"] == "jplatpat")
        self.assertIn("*[", text)
        self.assertIn("/FI", text)

    def test_axes_fixed_and_decisions_logged(self):
        axes = self.bundle["axes"]
        self.assertEqual([a["axis_id"] for a in axes], ["A", "B", "C"])
        self.assertTrue(all(a["fixed"] for a in axes))
        gates = [(d["gate"], d["actor"]) for d in self.bundle["decisions"]]
        self.assertIn(("G1", "human"), gates)
        self.assertIn(("G2", "human"), gates)
        self.assertIn(("G4", "human"), gates)
        self.assertEqual([d["action"] for d in self.bundle["decisions"] if d["gate"] == "G4"], ["iterate", "finalize"])

    def test_candidates_have_origins_and_rsj_stats(self):
        cands = self.bundle["candidates"]
        adopted = [c for c in cands if c["status"] == "adopted" and c["kind"] == "term"]
        self.assertIn(("A", "高強度鋼板"), [(c["axis_id"], c["value"]) for c in cands if c["kind"] == "term"])
        self.assertTrue(any(c["origin"].startswith("rsj:iter2") for c in cands))
        self.assertTrue(any(c["rsj_w"] is not None for c in adopted))
        self.assertTrue(all(c["dict_known"] is not None for c in cands if c["kind"] == "code"))
        seed_codes = [c for c in cands if c["kind"] == "code" and c["origin"].startswith("seed")]
        self.assertTrue(seed_codes)

    def test_metrics_estimates_and_stop(self):
        lm = self.bundle["latest_metrics"]
        self.assertIn("standard", lm["estimates"])
        est = lm["estimates"]["standard"]
        self.assertEqual(est["basis"], "U")
        self.assertLessEqual(est["ci_low"], est["recall_hat"])
        self.assertIn("reasons", lm["stop"])
        self.assertTrue(lm["judged"]["N"] >= 30)
        self.assertGreater(len(self.bundle["transforms"]), 0)
        self.assertTrue(any(t["local_eval"] for t in self.bundle["transforms"]))
        self.assertTrue(lm["cal"].get("ok"))
        self.assertEqual(lm["retention"], 1.0)

    def test_report_and_excel(self):
        md = build_markdown(self.bundle)
        for section in ("検索式（3 案）", "検索観点表", "評価ログ", "変換候補", "版履歴", "注記"):
            self.assertIn(section, md)
        self.assertIn("jplatpat", md)
        html = build_html(self.bundle)
        self.assertIn("<table>", html)
        self.assertIn("根拠レポート", html)
        data = design_workbook(self.bundle)
        sheets = read_xlsx(data)
        self.assertGreater(len(sheets["語候補"]), 5)
        self.assertGreater(len(sheets["検索式"]), 6)

    def test_apply_excel_changes_candidate(self):
        st = Store(":memory:")
        orc = Orchestrator(st)
        case = orc.load_sample_case()
        cid = case["case_id"]
        script = sample_script()
        dec = ScriptedHumanDecider(script)
        orc.structure(cid)
        orc.decide_g1(cid, dec.g1({"axes": st.get_axes(cid)}).payload["axes"])
        orc.expand(cid)
        sheets = read_xlsx(design_workbook(orc.case_bundle(cid)))
        hdr = sheets["語候補"][0]
        i_id, i_st = hdr.index("候補ID"), hdr.index("採否（採用／棄却／保留）")
        target = next(r for r in sheets["語候補"][1:] if r[i_st] == "保留")
        target[i_st] = "採用"
        res = orc.apply_excel(cid, write_xlsx(sheets))
        self.assertEqual(res["decisions"], 1)
        c = next(c for c in st.get_candidates(cid) if c["candidate_id"] == target[i_id])
        self.assertEqual(c["status"], "adopted")


class TestAuto(unittest.TestCase):
    def test_policy_runs_to_finalized(self):
        st = Store(":memory:")
        orc = Orchestrator(st)
        case = orc.load_sample_case()
        s = orc.run_auto(case["case_id"], max_iterations=3)
        self.assertEqual(s["status"], "finalized")
        self.assertEqual(s["variants"]["standard"]["recall_seed"], 1.0)
        self.assertEqual(s["variants"]["standard"]["recall_pool"], 1.0)
        b = orc.case_bundle(case["case_id"])
        self.assertTrue(all(d["actor"] == "policy" for d in b["decisions"]))
        adopted_terms = {c["value"] for c in b["candidates"] if c["kind"] == "term" and c["status"] == "adopted"}
        self.assertIn("高強度鋼板", adopted_terms)
        self.assertIn("焼入れ", adopted_terms)
        self.assertLessEqual(s["iteration"], 3)


class TestStepsAndErrors(unittest.TestCase):
    def setUp(self):
        self.st = Store(":memory:")
        self.orc = Orchestrator(self.st)

    def test_create_case_errors_and_seeds(self):
        with self.assertRaises(OrchestratorError):
            self.orc.create_case(name="x", purpose="prior_art", input_text="  ")
        case = self.orc.create_case(name="x", purpose="prior_art", input_text="高強度鋼板を焼入れする。",
                                    seeds=["JP2020-000001A", {"doc_id": "JP2020-000002A", "title": "t", "codes": {"FI": ["C22C38/00"]}}])
        self.assertEqual(case["case_id"], "CASE-0001")
        self.assertEqual(set(self.st.pool_ids(case["case_id"])), {"JP2020-000001A", "JP2020-000002A"})
        self.assertTrue(self.orc.dictionary.known("FI", "C22C38/00"))     # 縮退モードで観測

    def test_gate_guards(self):
        case = self.orc.create_case(name="x", purpose="prior_art", input_text="高強度鋼板を焼入れする。自動車部材。")
        cid = case["case_id"]
        with self.assertRaises(OrchestratorError):
            self.orc.expand(cid)                                          # G1 前
        self.orc.structure(cid)
        self.assertEqual(self.st.get_case(cid)["status"], "g1_pending")
        with self.assertRaises(OrchestratorError):
            self.orc.decide_g1(cid, [{"axis_id": "A", "name": "x", "kind": "auxiliary"}])   # 必須なし
        self.orc.decide_g1(cid, [{"axis_id": "A", "name": "対象", "kind": "required", "terms": ["高強度鋼板"]},
                                 {"axis_id": "B", "name": "手段", "kind": "required", "terms": ["焼入れ"]}])
        self.assertEqual(len(self.st.get_candidates(cid, status="adopted")), 2)
        with self.assertRaises(OrchestratorError):
            self.orc.score(cid)                                           # 母集団なし
        res = self.orc.decide_g2(cid, [])
        self.assertEqual(set(res["queries"]), {"broad", "standard", "narrow"})
        with self.assertRaises(OrchestratorError):
            self.orc.import_run(cid, "standard", text="")

    def test_import_csv_run_and_count_only(self):
        case = self.orc.create_case(name="x", purpose="prior_art", input_text="高強度鋼板を焼入れする。", seeds=["JP2020-000001A"])
        cid = case["case_id"]
        self.orc.structure(cid)
        self.orc.decide_g1(cid, [{"axis_id": "A", "name": "対象", "kind": "required", "terms": ["高強度鋼板"]}])
        self.orc.decide_g2(cid, [])
        csv = "文献番号,発明の名称,要約,FI\nJP2020-000001A,高強度鋼板,焼入れ,C22C38/00\nJP2020-000002A,鋼板,圧延,C21D8/02\n"
        r = self.orc.import_run(cid, "standard", data=csv.encode("cp932"), hit_count=1500, filename="hits.csv")
        self.assertEqual((r["hit_count"], r["n_docs"], r["recall_seed"]), (1500, 2, 1.0))
        r2 = self.orc.import_run(cid, "broad", docs=[], hit_count=9000, source="count_only")
        self.assertEqual(r2["n_docs"], 0)
        runs = self.st.get_runs(cid)
        self.assertEqual(len(runs), 2)
        self.orc.score(cid)
        a = self.orc.analyze(cid)
        self.assertEqual(a["metrics"]["variants"]["standard"]["hit_count"], 1500)
        self.assertEqual(a["metrics"]["population"]["variant"], "standard")     # 文献のある実行が母集団

    def test_manual_mode_end_to_end_step(self):
        cfg = load_config()
        cfg["llm"] = dict(cfg["llm"], mode="manual")
        orc = Orchestrator(self.st, cfg)
        case = orc.create_case(name="x", purpose="prior_art", input_text="高強度鋼板を焼入れする。自動車部材向け。")
        cid = case["case_id"]
        with self.assertRaises(PendingManualResponse) as ctx:
            orc.structure(cid)
        key = ctx.exception.prompts[0]["call_key"]
        self.assertEqual(len(self.st.pending_manual_prompts(cid)), 1)
        answer = mock.generate("P1", {"input_text": case["input_text"], "purpose_label": "先行技術調査", "seed_docs": []})
        r = orc.answer_manual(cid, key, json.dumps(answer, ensure_ascii=False))
        self.assertEqual(r["pending"], 0)
        res = orc.structure(cid)
        self.assertEqual(res["llm"]["mode"], "manual")
        self.assertEqual(self.st.get_case(cid)["status"], "g1_pending")


if __name__ == "__main__":
    unittest.main()
