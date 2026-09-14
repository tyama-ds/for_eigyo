"""引用拡張（§10.9）・複合変換探索（§10.6）・SDI（Phase 4）のテスト。"""
import json
import unittest

from helpers import SAMPLE_DIR, sample_script, toy_docs
from pqb.config import load_config
from pqb.core.document import Document
from pqb.core.dsl import Block, Query, Term
from pqb.db.csv_import import export_csv_text
from pqb.gates.human import ScriptedHumanDecider
from pqb.learn.search import composite_transform, pareto_search
from pqb.learn.transforms import Transform, apply_transform
from pqb.orchestrator import Orchestrator, OrchestratorError
from pqb.store.db import Store


class TestSearch(unittest.TestCase):
    def test_pareto_search_finds_dominating_composite(self):
        docs, rel = toy_docs()
        base = Query(query_id="q", case_id="c", blocks=[Block("A", terms=[Term("鋼板", origin="input"), Term("アルミ"), Term("樹脂")]),
                                                        Block("B", terms=[Term("焼入れ", origin="input"), Term("成形")])])
        pool = {d.doc_id for d in docs if rel[d.doc_id]}
        muts = [Transform("DROP_TERM", {"axis_id": "A", "text": "アルミ"}, direction="narrow"),
                Transform("DROP_TERM", {"axis_id": "A", "text": "樹脂"}, direction="narrow"),
                Transform("DROP_TERM", {"axis_id": "B", "text": "成形"}, direction="narrow"),
                Transform("DROP_TERM", {"axis_id": "A", "text": "鋼板"}, direction="narrow"),       # プールを落とす → 不可
                Transform("ADD_TERM", {"axis_id": "A", "text": "x"}, direction="widen")]            # 広げる方向は使わない
        calls = []

        def reflect(ctx):
            calls.append(ctx)
            return [Transform("ADD_AXIS", {"axis_id": "C", "terms": [{"text": "自動車部材"}]}, direction="narrow", source="llm")]

        res = pareto_search(base, docs, pool_ids=pool, seed_ids=set(sorted(pool)[:2]), relevant=rel, mutations=muts, tau=0.95, k=10, reflect=reflect)
        self.assertEqual(res["base"]["hit_count"], 40)
        self.assertTrue(res["front"])
        best = res["front"][0]
        self.assertEqual(best["hit_count"], 20)
        self.assertEqual(best["recall_pool"], 1.0)
        self.assertGreaterEqual(len(best["steps"]), 2)
        self.assertTrue(all(st["target"].get("text") != "鋼板" for st in best["steps"]))
        self.assertTrue(calls)                                       # 反省的変異が呼ばれた
        self.assertIn("failures", calls[0])
        t = composite_transform(res["candidates"][0], res["base"])
        self.assertEqual(t.op, "COMPOSITE")
        self.assertTrue(t.local_eval)
        q2 = apply_transform(base, t)
        self.assertEqual(len([x for x in q2.block("A").adopted_terms()]), 1)

    def test_composite_apply_to_case(self):
        st = Store(":memory:")
        orc = Orchestrator(st)
        case = orc.create_case(name="x", purpose="prior_art", input_text="高強度鋼板を焼入れする。")
        cid = case["case_id"]
        orc.structure(cid)
        orc.decide_g1(cid, [{"axis_id": "A", "name": "対象", "kind": "required", "terms": ["高強度鋼板"]},
                            {"axis_id": "B", "name": "手段", "kind": "required", "terms": ["焼入れ"]}])
        orc._apply_transform_to_case(cid, {"op": "COMPOSITE", "source": "search", "target": {"steps": [
            {"op": "ADD_TERM", "target": {"axis_id": "A", "text": "高張力鋼板"}, "source": "rsj"},
            {"op": "TERM_CODE_JOIN", "target": {"axis_id": "A", "join": "AND"}, "source": "rsj"}]}}, 1, "human")
        adopted = {c["value"] for c in st.get_candidates(cid, kind="term", status="adopted")}
        self.assertIn("高張力鋼板", adopted)
        self.assertEqual(st.get_case(cid)["settings"]["join_and_axes"], ["A"])


class TestCitationExpansion(unittest.TestCase):
    def _orc(self, **over):
        cfg = load_config()
        cfg.update(over)
        st = Store(":memory:")
        return st, Orchestrator(st, cfg)

    def test_citations_judged_and_pool_grows(self):
        # 採点対象を小さくして、引用拡張が未採点の文献を拾うようにする
        st, orc = self._orc(top_k=3, sample={"m_max": 3, "s_min": 30, "m_step": 3, "z": 1.96})
        case = orc.load_sample_case()
        cid = case["case_id"]
        dec = ScriptedHumanDecider(sample_script())
        orc.structure(cid)
        orc.decide_g1(cid, dec.g1({"axes": st.get_axes(cid)}).payload["axes"])
        orc.expand(cid)
        orc.decide_g2(cid, dec.g2({"candidates": st.get_candidates(cid)}).payload["decisions"])
        orc.run_local(cid)
        pool_before = len(st.pool_ids(cid))
        r = orc.score(cid)
        cit = r["citation"]
        self.assertGreater(cit["candidates"], 0)
        self.assertGreater(cit["judged"], 0)
        sel = {j["selection"] for j in st.get_judgments(cid)}
        self.assertIn("citation", sel)
        self.assertGreater(len(st.pool_ids(cid)), pool_before)
        log = st.get_case(cid)["settings"]["citation_log"]
        self.assertEqual(log[-1]["iteration"], 1)
        # 明示実行（2 回目）は既に判定済みなので候補が減る／0 になる
        r2 = orc.expand_citations(cid)
        self.assertLessEqual(r2["judged"], cit["judged"])
        a = orc.analyze(cid)
        self.assertIn("coverage", a["metrics"])

    def test_pool_outside_population_is_reported_and_feeds_rsj(self):
        st, orc = self._orc()
        orc.load_local_index(__import__("pqb.db.csv_import", fromlist=["import_csv_bytes"]).import_csv_bytes(
            (SAMPLE_DIR / "population.csv").read_bytes(), load_config() and __import__("pqb.config", fromlist=["load_csv_dialect"]).load_csv_dialect("jplatpat"))[0])
        seeds = json.loads((SAMPLE_DIR / "seeds.json").read_text(encoding="utf-8"))
        outside = {"doc_id": "JP2099-000001A", "title": "高強度鋼板の焼入れ方法（母集団外）", "abstract": "高強度鋼板を焼入れする。",
                   "codes": {"FI": ["C21D9/46@Z"], "FT": [], "IPC": []}, "citations": []}
        case = orc.create_case(name="x", purpose="prior_art", input_text=(SAMPLE_DIR / "input.txt").read_text(encoding="utf-8"), seeds=seeds + [outside])
        cid = case["case_id"]
        dec = ScriptedHumanDecider(sample_script())
        orc.structure(cid)
        orc.decide_g1(cid, dec.g1({"axes": st.get_axes(cid)}).payload["axes"])
        orc.expand(cid)
        orc.decide_g2(cid, dec.g2({"candidates": st.get_candidates(cid)}).payload["decisions"])
        orc.run_local(cid)
        orc.score(cid)
        a = orc.analyze(cid)
        cov = a["metrics"]["coverage"]
        self.assertGreaterEqual(cov["n_outside"], 1)
        self.assertIn("JP2099-000001A", cov["pool_outside_U"])
        # 母集団外の既知文献の分類は RSJ の入力に加わる
        run, U = orc._population(st.get_case(cid))
        judged, _, _ = orc._judged_docs(st.get_case(cid), U)
        self.assertIn("JP2099-000001A", {d.doc_id for d, _ in judged})
        self.assertLess(a["metrics"]["variants"]["standard"]["recall_seed"], 1.0)     # 既知文献の取り逃しが見える


class TestSDI(unittest.TestCase):
    def test_sdi_diff_detects_new_relevant_docs(self):
        st = Store(":memory:")
        orc = Orchestrator(st)
        case = orc.load_sample_case()
        cid = case["case_id"]
        with self.assertRaises(OrchestratorError):
            orc.sdi_run(cid, source="local_index")                     # 確定前は不可
        orc.run_case(cid, ScriptedHumanDecider(sample_script()))
        r0 = orc.sdi_run(cid, variant="standard", source="local_index")
        self.assertEqual(r0["n_new"], 0)
        self.assertIn("新規文献はありません", r0["report"])
        # 新しい公報が 2 件増えた CSV（1 件は適合、1 件は非適合）
        it = st.get_case(cid)["settings"]["final_iteration"]
        std = st.get_queries(cid, it)["standard"]
        from pqb.core.match import match_docs
        hits = match_docs(std, st.local_index_docs())
        new_rel = Document("JP2026-100001A", title="高強度鋼板の焼入れ方法（新規）", abstract="自動車部材向けの高強度鋼板を加熱後に急冷して焼入れする。",
                           codes={"FI": ["C22C38/00", "C21D9/46"], "FT": ["4K037AA01"], "IPC": ["C22C38/00"]}, pub_date="2026-08-01")
        new_non = Document("JP2026-100002A", title="鋼板の搬送装置（新規）", abstract="鋼板を搬送するローラ。", codes={"FI": ["B65G39/00"]}, pub_date="2026-08-02")
        csv_text = export_csv_text(hits + [new_rel, new_non])
        pool_before = len(st.pool_ids(cid))
        r = orc.sdi_run(cid, variant="standard", text=csv_text, filename="rerun.csv")
        self.assertEqual(r["n_new"], 2)
        self.assertEqual(r["n_new_relevant"], 1)
        self.assertIn("JP2026-100001A", r["new_relevant_ids"])
        self.assertIn("JP2026-100001A", r["report"])
        self.assertEqual(len(st.pool_ids(cid)), pool_before + 1)
        hist = st.get_case(cid)["settings"]["sdi_history"]
        self.assertEqual(len(hist), 2)
        self.assertTrue(any(x["source"].startswith("sdi:") for x in st.get_runs(cid)))
        # 同じ CSV を再実行すると新規 0
        r2 = orc.sdi_run(cid, variant="standard", text=csv_text)
        self.assertEqual(r2["n_new"], 0)
        from pqb.report.build import build_markdown
        md = build_markdown(orc.case_bundle(cid))
        self.assertIn("SDI（定期監視）差分履歴", md)
        self.assertIn("被覆と引用・同族による拡張", md)


if __name__ == "__main__":
    unittest.main()
