import unittest

from helpers import SAMPLE_DIR, sample_query
from mock_db_api import start_mock_db_api
from pqb.config import load_config, load_csv_dialect
from pqb.core.dsl import Term
from pqb.db.adapter import DBAdapter, DBError
from pqb.db.csv_import import import_csv_bytes
from pqb.orchestrator import Orchestrator
from pqb.store.db import Store


class TestDBApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs, _ = import_csv_bytes((SAMPLE_DIR / "population.csv").read_bytes(), load_csv_dialect("jplatpat"))
        cls.server, cls.base, cls.calls = start_mock_db_api(cls.docs)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def cfg(self, **db):
        cfg = load_config()
        cfg["offline"] = False
        dbcfg = dict(cfg["db"], mode="api", api_base_url=self.base, api_key="k", use_proxy=False, page_size=20, max_pages=10)
        dbcfg.update(db)
        cfg["db"] = dbcfg
        cfg["llm"] = dict(cfg["llm"], mode="mock")
        return cfg

    def test_search_pagination_and_access_log(self):
        st = Store(":memory:")
        st.create_case(case_id="C1", name="n", purpose="prior_art", input_text="x", seeds=[])
        db = DBAdapter(self.cfg(), st, "C1")
        q = sample_query()
        q.blocks[0].terms = [Term("鋼板", fields=["TI", "AB"])]
        before = self.calls["search"]
        res = db.run_api(q, "rendered", "jplatpat")
        self.assertEqual(res.source, "api")
        self.assertGreater(res.hit_count, 20)
        self.assertEqual(len(res.documents), res.hit_count)          # 全ページ取得
        self.assertGreaterEqual(self.calls["search"] - before, 2)     # ページング
        self.assertEqual(res.documents[0].rank, 1)
        self.assertEqual(st.count_db_access("C1"), self.calls["search"] - before)
        self.assertTrue(all(a == "Bearer k" for a in self.calls["auth"][-2:]))
        # 被引用エッジが登録される
        self.assertTrue(any(st.citing_ids(d.doc_id) for d in res.documents))

    def test_fetch_document_and_limit(self):
        st = Store(":memory:")
        st.create_case(case_id="C2", name="n", purpose="prior_art", input_text="x", seeds=[])
        db = DBAdapter(self.cfg(access_limit=3), st, "C2")
        d = db.fetch_document(self.docs[0].doc_id)
        self.assertEqual(d.doc_id, self.docs[0].doc_id)
        self.assertTrue(st.has_document(d.doc_id))
        self.assertIsNone(db.fetch_document("JP0000-000000A"))
        self.assertEqual(st.count_db_access("C2"), 2)
        db.fetch_document(self.docs[1].doc_id)
        with self.assertRaises(DBError):
            db.fetch_document(self.docs[2].doc_id)                   # 上限 3 回
        self.assertEqual(db.access_count(), 3)

    def test_offline_and_errors(self):
        cfg = self.cfg()
        cfg["offline"] = True
        with self.assertRaises(DBError):
            DBAdapter(cfg, None).run_api(sample_query(), "x")
        cfg = self.cfg(api_base_url="http://127.0.0.1:9/")            # 接続不可
        with self.assertRaises(DBError):
            DBAdapter(cfg, None).run_api(sample_query(), "x")
        srv, base, _ = start_mock_db_api(self.docs, fail_search=True)
        try:
            with self.assertRaises(DBError):
                DBAdapter(self.cfg(api_base_url=base), None).run_api(sample_query(), "x")
        finally:
            srv.shutdown()

    def test_auto_run_via_api_and_sdi_api(self):
        st = Store(":memory:")
        orc = Orchestrator(st, self.cfg(access_limit=200))
        orc.import_code_dictionary((SAMPLE_DIR / "codes_subset.csv").read_text(encoding="utf-8"))
        import json
        seeds = json.loads((SAMPLE_DIR / "seeds.json").read_text(encoding="utf-8"))
        case = orc.create_case(name="api", purpose="prior_art", input_text=(SAMPLE_DIR / "input.txt").read_text(encoding="utf-8"), seeds=seeds)
        s = orc.run_auto(case["case_id"], max_iterations=2)
        self.assertEqual(s["status"], "finalized")
        self.assertEqual(s["variants"]["standard"]["recall_seed"], 1.0)
        self.assertGreater(s["db_access"], 0)
        runs = st.get_runs(case["case_id"])
        self.assertTrue(all(r["source"] == "api" for r in runs))
        r = orc.sdi_run(case["case_id"], variant="standard", source="api")
        self.assertEqual(r["source"], "api")
        self.assertEqual(r["n_new"], 0)
        self.assertIn("SDI 差分報告", r["report"])


if __name__ == "__main__":
    unittest.main()
