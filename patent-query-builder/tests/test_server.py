import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import helpers  # noqa: F401
from pqb import config as cfgmod
from pqb.llm import mock
from pqb.orchestrator import Orchestrator
from pqb.store.db import Store

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TestServerAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cfgmod.RUNTIME_CONFIG_FILE = tmp / "pqb.config.json"
        cfgmod.DATA_DIR = tmp / "data"
        import server as appserver
        cls.appserver = appserver
        appserver.set_orc(Orchestrator(Store(":memory:")))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), appserver.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def api(self, path, body=None, method=None, raw=False):
        req = urllib.request.Request(self.base + path, method=method or ("POST" if body is not None else "GET"))
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with _opener.open(req, data=data, timeout=120) as resp:
                content = resp.read()
                return (resp.status, content) if raw else (resp.status, json.loads(content.decode("utf-8")))
        except urllib.error.HTTPError as e:
            content = e.read()
            try:
                return e.code, json.loads(content.decode("utf-8"))
            except ValueError:
                return e.code, {"raw": content.decode("utf-8", "replace")}

    def test_full_flow(self):
        st, d = self.api("/api/health")
        self.assertEqual(st, 200)
        self.assertEqual(d["app"], "patent-query-builder")
        st, d = self.api("/api/config")
        self.assertEqual(d["config"]["llm"]["mode"], "mock")
        self.assertNotIn("api_key", d["config"]["llm"])
        st, d = self.api("/api/config", {"top_k": 20, "llm": {"api_key": "secret"}})
        self.assertEqual(d["config"]["top_k"], 20)
        self.assertTrue(d["config"]["llm"]["has_api_key"])
        st, d = self.api("/api/demo", {})
        self.assertEqual(st, 200)
        cid = d["case"]["case_id"]
        st, d = self.api("/api/cases")
        self.assertEqual(d["cases"][0]["case_id"], cid)
        st, d = self.api(f"/api/cases/{cid}/structure", {})
        self.assertEqual(st, 200)
        self.assertTrue(d["axes"])
        axes = [{"axis_id": "A", "name": "対象物", "kind": "required", "terms": ["高強度鋼板"]},
                {"axis_id": "B", "name": "手段", "kind": "required", "terms": ["焼入れ"]},
                {"axis_id": "C", "name": "用途", "kind": "auxiliary", "terms": ["自動車部材"]}]
        st, d = self.api(f"/api/cases/{cid}/g1", {"axes": axes})
        self.assertEqual(st, 200)
        st, d = self.api(f"/api/cases/{cid}/expand", {})
        self.assertGreater(d["added"]["terms"], 0)
        st, b = self.api(f"/api/cases/{cid}")
        decisions = [{"candidate_id": c["candidate_id"], "status": "adopted" if (c["kind"] == "code" and c["dict_known"]) or c["value"] in ("高張力鋼板", "急冷") else "rejected", "reason_code": "01"}
                     for c in b["candidates"] if c["status"] == "candidate"]
        st, d = self.api(f"/api/cases/{cid}/g2", {"decisions": decisions, "broad_drop_axis": "B"})
        self.assertEqual(st, 200)
        self.assertEqual(d["notes"]["broad_drop_axis"], "B")
        st, d = self.api(f"/api/cases/{cid}/runs_local", {})
        self.assertEqual(st, 200)
        self.assertEqual(d["standard"]["recall_seed"], 1.0)
        # CSV 取り込み（件数のみ、CSV 付き）
        st, d = self.api(f"/api/cases/{cid}/runs", {"variant": "narrow", "hit_count": 42})
        self.assertEqual(d["hit_count"], 42)
        csv_b64 = __import__("base64").b64encode((helpers.SAMPLE_DIR / "population.csv").read_bytes()).decode()
        st, d = self.api(f"/api/cases/{cid}/runs", {"variant": "broad", "csv_base64": csv_b64, "hit_count": 120, "filename": "population.csv"})
        self.assertEqual((st, d["n_docs"]), (200, 120))
        st, d = self.api(f"/api/cases/{cid}/score", {})
        self.assertGreater(d["judged"], 0)
        st, d = self.api(f"/api/cases/{cid}/analyze", {})
        self.assertIn("standard", d["metrics"]["variants"])
        st, d = self.api(f"/api/cases/{cid}/citations", {})
        self.assertEqual(st, 200)
        self.assertIn("candidates", d)
        st, b = self.api(f"/api/cases/{cid}")
        self.assertEqual(b["case"]["status"], "g4_pending")
        self.assertIn("coverage", b["latest_metrics"])
        tf = [{"transform_id": t["transform_id"], "status": "rejected"} for t in b["transforms"]]
        doc = next(j for j in b["judgment_table"] if not j["seed"])
        st, d = self.api(f"/api/cases/{cid}/g4", {"judgments": [{"doc_id": doc["doc_id"], "overall": 3, "comment": "人が確認"}], "transforms": tf, "action": "finalize"})
        self.assertEqual(d["status"], "finalized")
        st, d = self.api(f"/api/cases/{cid}/sdi", {"variant": "standard", "source": "local_index"})
        self.assertEqual(st, 200)
        self.assertEqual(d["n_new"], 0)
        self.assertIn("report", d)
        st, d = self.api(f"/api/cases/{cid}/sdi", {"variant": "standard", "source": "csv"})
        self.assertEqual(st, 400)
        st, content = self.api(f"/api/cases/{cid}/report?format=md", raw=True)
        self.assertIn("検索式 根拠レポート", content.decode("utf-8"))
        st, content = self.api(f"/api/cases/{cid}/report?format=html&download=1", raw=True)
        self.assertIn(b"<table>", content)
        st, content = self.api(f"/api/cases/{cid}/excel", raw=True)
        self.assertTrue(content.startswith(b"PK"))
        st, d = self.api(f"/api/cases/{cid}/summary")
        self.assertEqual(d["status"], "finalized")
        st, d = self.api("/api/documents?ids=" + doc["doc_id"])
        self.assertEqual(d["documents"][0]["doc_id"], doc["doc_id"])
        st, d = self.api("/api/prompts/P1")
        self.assertIn("検索観点", d["template"])
        st, d = self.api(f"/api/cases/{cid}", None, "DELETE")
        self.assertEqual(d["deleted"], cid)
        st, d = self.api(f"/api/cases/{cid}")
        self.assertEqual(st, 400)

    def test_manual_pending_409_and_errors(self):
        self.api("/api/config", {"llm": {"mode": "manual"}})
        try:
            st, d = self.api("/api/cases", {"name": "manual", "purpose": "prior_art", "input_text": "高強度鋼板を焼入れする。自動車部材向け。", "seeds": "JP1\nJP2"})
            self.assertEqual(st, 200)
            cid = d["case"]["case_id"]
            self.assertEqual(d["case"]["seeds"], ["JP1", "JP2"])
            st, d = self.api(f"/api/cases/{cid}/structure", {})
            self.assertEqual(st, 409)
            self.assertEqual(d["kind"], "manual")
            key = d["prompts"][0]["call_key"]
            answer = mock.generate("P1", {"input_text": "高強度鋼板を焼入れする。自動車部材向け。", "purpose_label": "x", "seed_docs": []})
            st, d = self.api(f"/api/cases/{cid}/manual", {"call_key": key, "text": json.dumps(answer, ensure_ascii=False)})
            self.assertEqual(d["pending"], 0)
            st, d = self.api(f"/api/cases/{cid}/structure", {})
            self.assertEqual(st, 200)
            self.assertEqual(d["llm"]["mode"], "manual")
            st, d = self.api("/api/cases", {"name": "bad", "purpose": "prior_art", "input_text": ""})
            self.assertEqual(st, 400)
            st, d = self.api("/api/nope")
            self.assertEqual(st, 404)
        finally:
            self.api("/api/config", {"llm": {"mode": "mock"}})


if __name__ == "__main__":
    unittest.main()
