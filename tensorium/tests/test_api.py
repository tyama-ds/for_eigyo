"""server.py の API テスト（標準ライブラリのみ。学習はベースラインなので torch 不要）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="tensorium-test-")
os.environ["TENSORIUM_DATA_DIR"] = _TMP                       # import 前に保存先をテスト用へ
os.environ["TENSORIUM_CONFIG_FILE"] = str(Path(_TMP) / "config.json")

import server as srv  # noqa: E402

SAMPLES = ROOT / "sample_data"
SPEC_CLS = {"target": "評価", "task": "classification",
            "roles": {"レビュー本文": "text", "カテゴリ": "categorical", "商品": "ignore", "価格": "numeric", "購入回数": "numeric"},
            "split": {"val": 0.2, "test": 0.1, "seed": 7}}
SPEC_REG = {"target": "価格_万円", "task": "regression",
            "roles": {"車両説明": "text", "メーカー": "categorical", "年式": "numeric", "走行距離_万km": "numeric"},
            "split": {"val": 0.2, "test": 0.0, "seed": 7}}


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = srv.make_server(0)
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"
        cls.th = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def call(self, method, path, body=None, headers=None, raw=None):
        h = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def wait_job(self, job_id, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            _, j = self.call("GET", f"/api/jobs/{job_id}")
            if j["status"] not in ("running", "queued"):
                return j
            time.sleep(0.2)
        self.fail("job timeout")


class TestStaticAndMeta(ApiTestCase):
    def test_static(self):
        for path, ctype in (("/", "text/html"), ("/style.css", "text/css"), ("/app.js", "javascript"), ("/charts.js", "javascript")):
            with urllib.request.urlopen(self.base + path) as r:
                self.assertEqual(r.status, 200, path)
                self.assertIn(ctype, r.headers["Content-Type"], path)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/../server.py")

    def test_meta_endpoints(self):
        st, status = self.call("GET", "/api/status")
        self.assertEqual(st, 200)
        self.assertIn("version", status)
        st, cat = self.call("GET", "/api/catalog")
        self.assertEqual({f["id"] for f in cat["families"]}, {"hf", "sbert", "scratch", "tabular", "baseline"})
        st, env = self.call("GET", "/api/env")
        self.assertIn("libs", env)
        self.assertTrue(env["families_available"]["baseline"])
        st, s = self.call("GET", "/api/samples")
        self.assertEqual(len(s["samples"]), 3)

    def test_settings_roundtrip(self):
        st, cfg = self.call("POST", "/api/settings", {"device": "cpu", "hf_offline": True, "hf_token": "secret", "unknown": 1})
        self.assertEqual(st, 200)
        self.assertEqual(cfg["device"], "cpu")
        self.assertTrue(cfg["hf_offline"])
        self.assertEqual(cfg["hf_token"], "")                       # 秘密は返さない
        self.assertTrue(cfg["hf_token_set"])
        self.assertNotIn("unknown", cfg)
        self.call("POST", "/api/settings", {"hf_offline": False})


class TestDatasetFlow(ApiTestCase):
    def test_upload_csv_and_summary(self):
        raw = (SAMPLES / "reviews_ja.csv").read_bytes()
        st, r = self.call("POST", "/api/dataset/upload", raw=raw, headers={"X-Filename": quote("レビュー.csv")})
        self.assertEqual(st, 200, r)
        ds = r["dataset"]
        self.assertEqual(ds["name"], "レビュー.csv")
        self.assertEqual(ds["n_rows"], 600)
        self.assertEqual(ds["suggest"]["target"], "評価")
        st, d = self.call("GET", "/api/dataset")
        self.assertEqual(d["dataset"]["n_rows"], 600)

    def test_upload_xlsx_and_sheet_switch(self):
        raw = (SAMPLES / "sales_memo_ja.xlsx").read_bytes()
        st, r = self.call("POST", "/api/dataset/upload", raw=raw, headers={"X-Filename": "sales_memo_ja.xlsx"})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["dataset"]["sheet"], "商談ログ")
        st, r2 = self.call("POST", "/api/dataset/sheet", {"sheet": "ない"})
        self.assertEqual(st, 400)
        self.assertIn("シート", r2["error"])

    def test_bad_upload(self):
        st, r = self.call("POST", "/api/dataset/upload", raw=b"", headers={"X-Filename": "x.csv"})
        self.assertEqual(st, 400)
        self.assertIn("error", r)
        st, r = self.call("POST", "/api/dataset/sample", {"name": "../../etc/passwd"})
        self.assertEqual(st, 400)

    def test_validate_spec(self):
        self.call("POST", "/api/dataset/sample", {"name": "reviews_ja.csv"})
        st, r = self.call("POST", "/api/spec/validate", {"spec": SPEC_CLS})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["n_valid"], 600)
        self.assertEqual(r["n_classes"], 3)
        self.assertEqual(sum(r["split_counts"].values()), 600)
        self.call("POST", "/api/dataset/sample", {"name": "used_cars_ja.csv"})
        st, r = self.call("POST", "/api/spec/validate", {"spec": SPEC_REG})
        self.assertIn("target_hist", r)
        st, r = self.call("POST", "/api/spec/validate", {"spec": {"target": "存在しない"}})
        self.assertEqual(st, 400)


class TestTrainPredictBaseline(ApiTestCase):
    def test_full_flow(self):
        self.call("POST", "/api/dataset/sample", {"name": "reviews_ja.csv"})
        st, r = self.call("POST", "/api/train", {"family": "baseline", "spec": SPEC_CLS, "name": "ベースライン試験"})
        self.assertEqual(st, 200, r)
        job = self.wait_job(r["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        run_id = job["result"]["run_id"]
        self.assertGreater(len(job["log"]), 0)

        st, runs = self.call("GET", "/api/runs")
        ids = [m["id"] for m in runs["runs"]]
        self.assertIn(run_id, ids)
        st, meta = self.call("GET", f"/api/runs/{run_id}")
        self.assertEqual(meta["name"], "ベースライン試験")
        self.assertEqual(meta["family"], "baseline")
        self.assertIn("accuracy", meta["metrics"]["val"])
        self.assertEqual(len(meta["eval"]["val"]["plot"]["confusion"]), 3)
        self.assertEqual(meta["n_train"] + meta["n_val"] + meta["n_test"], 600)

        # 手入力予測
        st, p = self.call("POST", "/api/predict", {"run_id": run_id, "rows": [
            {"レビュー本文": "最高でした", "カテゴリ": "家電", "価格": "1000", "購入回数": "1"}]})
        self.assertEqual(st, 200, p)
        self.assertEqual(len(p["predictions"]), 1)
        self.assertIn(p["predictions"][0]["pred"], meta["classes"])
        self.assertAlmostEqual(sum(p["predictions"][0]["probs"]), 1.0, places=5)
        # 必要列が無い
        st, p = self.call("POST", "/api/predict", {"run_id": run_id, "rows": [{"foo": "bar"}]})
        self.assertEqual(st, 400)
        self.assertIn("列", p["error"])
        # ファイル一括予測（目的変数列があるので actual が返る）
        raw = (SAMPLES / "reviews_ja.csv").read_bytes()
        st, p = self.call("POST", "/api/predict/upload", raw=raw, headers={"X-Filename": "reviews_ja.csv", "X-Run-Id": run_id})
        self.assertEqual(st, 200, p)
        self.assertEqual(len(p["predictions"]), 600)
        self.assertEqual(len(p["actual"]), 600)
        self.assertEqual(p["target"], "評価")

        # リネーム・削除
        st, rn = self.call("POST", f"/api/runs/{run_id}/rename", {"name": "改名"})
        self.assertEqual(rn["name"], "改名")
        st, d = self.call("POST", f"/api/runs/{run_id}/delete")
        self.assertTrue(d["deleted"])
        st, _ = self.call("GET", f"/api/runs/{run_id}")
        self.assertEqual(st, 404)

    def test_regression_baseline_and_bad_requests(self):
        self.call("POST", "/api/dataset/sample", {"name": "used_cars_ja.csv"})
        st, r = self.call("POST", "/api/train", {"family": "baseline", "spec": SPEC_REG})
        job = self.wait_job(r["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertIn("rmse", job["result"]["metrics"]["val"])
        self.assertIsNone(job["result"]["metrics"]["test"])           # test 0% → None
        st, r = self.call("POST", "/api/train", {"family": "nope", "spec": SPEC_REG})
        self.assertEqual(st, 400)
        st, r = self.call("POST", "/api/train", {"family": "baseline", "spec": {"target": "価格_万円", "roles": {}}})
        self.assertEqual(st, 400)
        st, r = self.call("GET", "/api/jobs/no-such-job")
        self.assertEqual(st, 404)
        self.assertFalse(self.call("POST", "/api/jobs/x/cancel")[1]["cancelled"])


if __name__ == "__main__":
    unittest.main()
