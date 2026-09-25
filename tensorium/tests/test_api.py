"""server.py の API テスト（標準ライブラリのみ。学習はベースラインなので torch 不要）。"""
from __future__ import annotations

import csv
import io
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
            with urllib.request.urlopen(req, timeout=180) as r:
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


class TestAugmentFlow(ApiTestCase):
    def test_profile_plan_generate_train(self):
        self.call("POST", "/api/dataset/sample", {"name": "reviews_ja.csv"})
        self.call("POST", "/api/settings", {"llm_provider": "builtin"})
        st, prof = self.call("POST", "/api/augment/profile", {"spec": SPEC_CLS})
        self.assertEqual(st, 200, prof)
        self.assertEqual(prof["task"], "classification")
        self.assertEqual(len(prof["groups"]), 3)
        self.assertIn("entropy", prof["stats"])
        self.assertIsNone(prof["current"])
        st, plan = self.call("POST", "/api/augment/plan", {"spec": SPEC_CLS, "n_total": 120, "mode": "balance"})
        self.assertEqual(st, 200, plan)
        self.assertEqual(plan["n_alloc"], 120)
        self.assertEqual(len(plan["counts_after"]), 3)
        self.assertGreaterEqual(plan["stats_after"]["entropy"], plan["stats_before"]["entropy"])
        st, plan2 = self.call("POST", "/api/augment/plan", {"spec": SPEC_CLS, "n_total": 0, "mode": "custom",
                                                            "custom": {"普通": 5}})
        self.assertEqual(plan2["n_alloc"], 5)
        # 生成（内蔵）
        st, r = self.call("POST", "/api/augment/start", {"spec": SPEC_CLS, "params": {"n_total": 120, "mode": "balance"}})
        self.assertEqual(st, 200, r)
        job = self.wait_job(r["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertGreater(job["result"]["n_rows"], 60)
        st, cur = self.call("GET", "/api/augment")
        aug = cur["augment"]
        self.assertEqual(aug["target"], "評価")
        self.assertEqual(len(aug["rows"]), min(aug["n_rows"], 200))
        self.assertEqual(len(aug["meta"]), len(aug["rows"]))
        self.assertTrue(aug["enabled"])
        st, status = self.call("GET", "/api/status")
        self.assertEqual(status["augment"]["n_rows"], aug["n_rows"])
        # CSV エクスポート
        with urllib.request.urlopen(self.base + "/api/augment/export?kind=synthetic") as res:
            text = res.read().decode("utf-8-sig")
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), aug["n_rows"] + 1)
        self.assertTrue(lines[0].endswith("_source,_group"))
        with urllib.request.urlopen(self.base + "/api/augment/export?kind=all") as res:
            all_lines = res.read().decode("utf-8-sig").strip().splitlines()
        self.assertEqual(len(all_lines), 600 + aug["n_rows"] + 1)
        # 学習: 合成データを含める / 含めない で val / test が同じ
        st, r0 = self.call("POST", "/api/train", {"family": "baseline", "spec": SPEC_CLS, "use_synthetic": False})
        j0 = self.wait_job(r0["job_id"])
        st, r1 = self.call("POST", "/api/train", {"family": "baseline", "spec": SPEC_CLS, "use_synthetic": True})
        j1 = self.wait_job(r1["job_id"])
        self.assertEqual(j1["status"], "done", j1.get("error"))
        self.assertEqual(j1["params"]["n_synthetic"], aug["n_rows"])
        _, m0 = self.call("GET", f"/api/runs/{j0['result']['run_id']}")
        _, m1 = self.call("GET", f"/api/runs/{j1['result']['run_id']}")
        self.assertEqual(m0["n_synthetic"], 0)
        self.assertEqual(m1["n_synthetic"], aug["n_rows"])
        self.assertEqual((m0["n_val"], m0["n_test"]), (m1["n_val"], m1["n_test"]))
        self.assertEqual(m1["n_train"], m0["n_train"] + aug["n_rows"])
        self.assertEqual(m1["dataset"]["n_rows"], 600)
        # 無効化すると含まれない
        st, en = self.call("POST", "/api/augment/enable", {"enabled": False})
        self.assertFalse(en["enabled"])
        st, r2 = self.call("POST", "/api/train", {"family": "baseline", "spec": SPEC_CLS, "use_synthetic": True})
        j2 = self.wait_job(r2["job_id"])
        self.assertNotIn("n_synthetic", j2["params"])
        # 目的変数が違う spec なら profile.current は None、学習にも混ざらない
        other = dict(SPEC_CLS, target="カテゴリ", roles={"レビュー本文": "text", "評価": "categorical"})
        st, prof2 = self.call("POST", "/api/augment/profile", {"spec": other})
        self.assertIsNone(prof2["current"])
        st, _ = self.call("POST", "/api/augment/clear")
        st, cur = self.call("GET", "/api/augment")
        self.assertIsNone(cur["augment"])
        st, r = self.call("POST", "/api/augment/enable", {"enabled": True})
        self.assertEqual(st, 400)

    def test_custom_mode_roles_and_csrf(self):
        self.call("POST", "/api/dataset/sample", {"name": "reviews_ja.csv"})
        self.call("POST", "/api/settings", {"llm_provider": "builtin"})
        # custom で全 0 は 400
        st, r = self.call("POST", "/api/augment/start", {"spec": SPEC_CLS, "params": {"mode": "custom", "custom": {}}})
        self.assertEqual(st, 400)
        st, r = self.call("POST", "/api/augment/start", {"spec": SPEC_CLS, "params": {"mode": "custom", "custom": {"普通": 30}}})
        self.assertEqual(st, 200, r)
        job = self.wait_job(r["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertTrue(job["result"]["stored"])
        _, cur = self.call("GET", "/api/augment")
        aug = cur["augment"]
        added = {g["label"]: g["added"] for g in aug["groups"]}
        self.assertEqual(added["高評価"], 0)
        self.assertEqual(added["低評価"], 0)
        self.assertGreater(added["普通"], 0)
        self.assertLessEqual(added["普通"], 30)
        # 目的変数が違う spec では学習に混ざらない
        other = dict(SPEC_CLS, target="カテゴリ", roles={"レビュー本文": "text", "評価": "categorical"})
        st, r2 = self.call("POST", "/api/train", {"family": "baseline", "spec": other, "use_synthetic": True})
        j2 = self.wait_job(r2["job_id"])
        self.assertEqual(j2["status"], "done", j2.get("error"))
        self.assertNotIn("n_synthetic", j2["params"])
        _, m2 = self.call("GET", f"/api/runs/{j2['result']['run_id']}")
        self.assertEqual(m2["n_synthetic"], 0)
        # ロールを変えた spec でも混ざらない（生成時と説明変数が違う）
        changed = dict(SPEC_CLS, roles={**SPEC_CLS["roles"], "商品": "categorical"})
        st, r3 = self.call("POST", "/api/train", {"family": "baseline", "spec": changed, "use_synthetic": True})
        j3 = self.wait_job(r3["job_id"])
        self.assertNotIn("n_synthetic", j3["params"])
        st, prof = self.call("POST", "/api/augment/profile", {"spec": changed})
        self.assertIsNone(prof["current"])
        st, status = self.call("GET", "/api/status")
        self.assertEqual(status["augment"]["spec_key"], aug["spec_key"])
        # CSV エクスポート: csv で読み戻し、危険な先頭文字はエスケープされる
        with srv._state_lock:
            srv.STATE["augment"]["rows"][0][0] = '=HYPERLINK("http://evil")\n改行, "引用"'
        with urllib.request.urlopen(self.base + "/api/augment/export?kind=all") as res:
            text = res.read().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(len(rows), 600 + aug["n_rows"] + 1)
        self.assertEqual(rows[0][-2:], ["_source", "_group"])
        self.assertEqual({r[-2] for r in rows[1:]}, {"real", "synthetic"})
        first_syn = next(r for r in rows[1:] if r[-2] == "synthetic")
        self.assertTrue(first_syn[0].startswith("'=HYPERLINK"))
        self.assertIn("\n", first_syn[0])
        # CSRF ガード: 外部 Origin は 403、text/plain の JSON は 415、Infinity は 400
        st, r = self.call("POST", "/api/augment/enable", {"enabled": True}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, r = self.call("POST", "/api/augment/enable", {"enabled": True}, headers={"Origin": f"http://127.0.0.1:{self.http.server_address[1]}"})
        self.assertEqual(st, 200)
        req = urllib.request.Request(self.base + "/api/augment/enable", data=b'{"enabled": true}', method="POST",
                                     headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 415)
        st, r = self.call("POST", "/api/augment/plan", raw=b'{"spec": {}, "n_total": Infinity}',
                          headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)
        # データを差し替えると合成データは消える
        self.call("POST", "/api/dataset/sample", {"name": "used_cars_ja.csv"})
        _, cur = self.call("GET", "/api/augment")
        self.assertIsNone(cur["augment"])

    def test_preview_limit_and_full(self):
        self.call("POST", "/api/dataset/sample", {"name": "used_cars_ja.csv"})
        self.call("POST", "/api/settings", {"llm_provider": "builtin"})
        st, r = self.call("POST", "/api/augment/start", {"spec": SPEC_REG, "params": {"n_total": 260, "mode": "equal", "dedupe": False}})
        self.assertEqual(st, 200, r)
        job = self.wait_job(r["job_id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        _, cur = self.call("GET", "/api/augment")
        self.assertEqual(cur["augment"]["n_rows"], 260)
        self.assertEqual(len(cur["augment"]["rows"]), 200)
        _, full = self.call("GET", "/api/augment?full=1")
        self.assertEqual(len(full["augment"]["rows"]), 260)
        self.assertEqual(len(full["augment"]["meta"]), 260)

    def test_start_validation(self):
        self.call("POST", "/api/dataset/sample", {"name": "reviews_ja.csv"})
        self.call("POST", "/api/settings", {"llm_provider": "openai", "llm_model": ""})
        st, r = self.call("POST", "/api/augment/start", {"spec": SPEC_CLS, "params": {"n_total": 10}})
        self.assertEqual(st, 400)
        self.assertIn("モデル名", r["error"])
        self.call("POST", "/api/settings", {"llm_provider": "builtin"})
        st, r = self.call("POST", "/api/augment/start", {"spec": SPEC_CLS, "params": {"n_total": 0}})
        self.assertEqual(st, 400)
        self.assertIn("0", r["error"])
        st, r = self.call("POST", "/api/settings", {"llm_base_url": "file:///etc/passwd"})
        self.assertEqual(st, 400)
        st, cfg = self.call("POST", "/api/settings", {"llm_api_key": "sk-x"})
        self.assertTrue(cfg["llm_api_key_set"])
        st, cfg = self.call("POST", "/api/settings", {"llm_api_key": None})
        self.assertTrue(cfg["llm_api_key_set"])                             # null は変更なし
        st, cfg = self.call("POST", "/api/settings", {"llm_api_key_clear": True})
        self.assertFalse(cfg["llm_api_key_set"])
        import stat

        from tcore.config import CONFIG_FILE
        mode = stat.S_IMODE(os.stat(CONFIG_FILE).st_mode)
        self.assertEqual(mode & 0o077, 0)                                   # 他ユーザーから読めない
        st, r = self.call("POST", "/api/augment/profile", {"spec": {"target": "nope"}})
        self.assertEqual(st, 400)
        st, r = self.call("POST", "/api/llm/test")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])                                        # builtin は常に OK


if __name__ == "__main__":
    unittest.main()
