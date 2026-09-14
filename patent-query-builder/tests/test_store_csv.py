import unittest

from helpers import sample_query, toy_docs
from pqb.config import load_config, load_csv_dialect
from pqb.core.document import Document
from pqb.db.adapter import DBAdapter, DBError
from pqb.db.csv_import import export_csv_text, import_csv_bytes, parse_csv_text
from pqb.store.db import Store

CSV = ("文献番号,発明の名称,要約,FI,Fターム,IPC,公知日\n"
       "JP2020-000001A,高強度鋼板,焼入れする,C22C38/00;C21D9/46,4K037AA01,C22C 38/00,2020-01-01\n"
       "JP2020-000002A,鋼板,圧延,C22C38/04,,C22C38/04,2020-02-01\n")


class TestCSV(unittest.TestCase):
    def test_parse_mapping_and_codes(self):
        docs, info = parse_csv_text(CSV, load_csv_dialect("jplatpat"))
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0].codes["FI"], ["C22C38/00", "C21D9/46"])
        self.assertEqual(docs[0].codes["IPC"], ["C22C38/00"])
        self.assertEqual(info["columns"]["title"], "発明の名称")
        self.assertIn("claims", info["missing"])
        self.assertTrue(info["has_abstract"])
        self.assertEqual(docs[1].rank, 2)

    def test_encodings_and_delimiter(self):
        for enc, expect in (("cp932", "cp932"), ("utf-8-sig", "utf-8-sig"), ("utf-8", "utf-8")):
            docs, info = import_csv_bytes(CSV.encode(enc), load_csv_dialect("jplatpat"))
            self.assertEqual(len(docs), 2, enc)
            self.assertEqual(info["encoding"], expect)
        tsv = CSV.replace(",", "\t").replace("C22C38/00;C21D9/46", "C22C38/00;C21D9/46")
        docs, info = parse_csv_text(tsv, load_csv_dialect("jplatpat"))
        self.assertEqual(info["delimiter"], "\t")
        self.assertEqual(len(docs), 2)

    def test_generic_dialect_and_export(self):
        docs, _ = toy_docs(5)
        text = export_csv_text(docs)
        self.assertTrue(text.startswith("﻿"))
        back, info = parse_csv_text(text, load_csv_dialect("generic"))
        self.assertEqual([d.doc_id for d in back], [d.doc_id for d in docs])
        self.assertEqual(back[0].codes, docs[0].codes)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.st = Store(":memory:")
        self.case = self.st.create_case(case_id="C1", name="t", purpose="prior_art", input_text="x", seeds=["JP1"])

    def test_case_axes_candidates(self):
        self.assertEqual(self.st.get_case("C1")["seeds"], ["JP1"])
        self.st.replace_axes("C1", [{"axis_id": "A", "name": "対象", "kind": "required", "terms": ["鋼板"]}], "human")
        self.assertEqual(self.st.get_axes("C1")[0]["terms"], ["鋼板"])
        row = self.st.add_candidate("C1", {"kind": "term", "axis_id": "A", "value": "鋼板", "origin": "input", "status": "adopted",
                                            "R": 3, "N": 10, "r": 2, "n": 4})
        c = self.st.get_candidates("C1")[0]
        self.assertEqual((c["R"], c["N"], c["r"], c["n"]), (3, 10, 2, 4))
        self.st.update_candidate_stats(row["candidate_id"], {"R": 5, "N": 20, "needs_review": True})
        c = self.st.get_candidates("C1")[0]
        self.assertEqual(c["R"], 5)
        self.assertTrue(c["needs_review"])
        self.st.decide_candidate(row["candidate_id"], "rejected", "02", actor="human")
        self.assertEqual(self.st.get_candidates("C1", status="rejected")[0]["decided_by"], "human")
        self.assertIsNotNone(self.st.find_candidate("C1", "term", "A", "鋼板"))

    def test_queries_runs_judgments_pool(self):
        q = sample_query()
        q.provenance = {"iteration": 1}
        self.st.save_query("C1", q)
        self.assertEqual(self.st.get_query(q.query_id).signature(), q.signature())
        self.st.save_renderings(q.query_id, "jplatpat", [{"part_no": 1, "text": "x", "chars": 1}], True)
        self.assertTrue(self.st.get_renderings(q.query_id)[0]["roundtrip_ok"])
        docs, _ = toy_docs(10)
        run_id = self.st.add_run(case_id="C1", query_id=q.query_id, iteration=1, variant="standard", dialect="jplatpat",
                                 source="csv", hit_count=10, docs=docs)
        self.assertEqual(len(self.st.run_documents(run_id)), 10)
        self.assertEqual(self.st.latest_run("C1", "standard")["n_docs"], 10)
        self.st.add_judgment(case_id="C1", doc_id="D000", iteration=1, selection="topk", judge="llm", overall=3)
        self.st.add_judgment(case_id="C1", doc_id="D000", iteration=1, selection="review", judge="human", overall=0)
        eff = self.st.effective_judgments("C1")
        self.assertEqual(eff["D000"]["judge"], "human")           # 人の判定を優先
        self.st.add_to_pool("C1", ["D003"], "judgment", 1)
        self.assertEqual(self.st.pool_ids("C1"), {"D003"})
        self.st.remove_from_pool("C1", "D003")
        self.assertEqual(self.st.pool_ids("C1"), set())
        self.assertEqual(self.st.load_local_index(docs), 10)
        self.assertEqual(len(self.st.local_index_docs()), 10)

    def test_misc_tables_and_delete(self):
        self.st.add_decision("C1", 1, "G1", "human", "fix_axes", {"axes": []})
        self.st.add_llm_call({"case_id": "C1", "prompt_id": "P1", "mode": "mock", "model": "mock", "input_hash": "h",
                              "n_samples": 1, "masked": 0, "response_json": [{"a": 1}], "flip_rate": 0.0, "status": "ok", "duration_ms": 1})
        self.assertEqual(self.st.count_llm_calls("C1"), 1)
        self.st.save_manual_prompt("k1", "C1", "P1", 1, "prompt")
        self.assertEqual(len(self.st.pending_manual_prompts("C1")), 1)
        self.assertTrue(self.st.answer_manual_prompt("k1", "{}"))
        self.assertFalse(self.st.answer_manual_prompt("nope", "{}"))
        self.st.bump_term_dictionary("鋼板", "鋼帯", "synonym", "llm", True)
        self.st.bump_term_dictionary("鋼板", "鋼帯", "synonym", "llm", True)
        self.assertEqual(self.st.term_synonyms("鋼板")[0]["adopted"], 2)
        self.st.save_transforms("C1", 1, [{"op": "ADD_TERM", "target": {"axis_id": "A", "text": "x"}, "source": "rsj"}])
        self.assertEqual(self.st.get_transforms("C1")[0]["target"]["text"], "x")
        self.st.delete_case("C1")
        self.assertIsNone(self.st.get_case("C1"))
        self.assertEqual(self.st.count_llm_calls("C1"), 0)


class TestDBAdapter(unittest.TestCase):
    def test_modes(self):
        cfg = load_config()
        st = Store(":memory:")
        db = DBAdapter(cfg, st)
        with self.assertRaises(DBError):
            db.import_csv("a,b\n", load_csv_dialect("jplatpat"))
        res = db.import_csv(CSV.encode("cp932"), load_csv_dialect("jplatpat"), query_id="q", hit_count=1234)
        self.assertEqual((res.hit_count, len(res.documents), res.source), (1234, 2, "csv"))
        docs, _ = toy_docs(30)
        st.load_local_index(docs)
        q = sample_query()
        q.blocks[0].terms = [__import__("pqb.core.dsl", fromlist=["Term"]).Term("鋼板")]
        res = db.run_local(q)
        self.assertEqual(res.source, "local_index")
        self.assertGreater(res.hit_count, 0)
        self.assertEqual(res.documents[0].rank, 1)
        with self.assertRaises(DBError):
            db.run(q, mode="csv")
        with self.assertRaises(DBError):
            db.run_api(q, "x")                       # offline=true では呼ばない
        cfg2 = dict(cfg, offline=False)
        with self.assertRaises(DBError):
            DBAdapter(cfg2, st).run_api(q, "x")      # base_url 未設定


if __name__ == "__main__":
    unittest.main()
