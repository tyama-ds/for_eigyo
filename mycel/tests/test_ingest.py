"""AI 取り込み（文書 → つながったノートの下書き → 保存）のテスト。モック LLM を使う。"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))

import server  # noqa: E402
from mock_llm import start_mock_llm  # noqa: E402
from mycelcore import links as L  # noqa: E402
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.ingest import _split, safe_name  # noqa: E402
from mycelcore.vault import VaultError  # noqa: E402
from samples import make_docx  # noqa: E402


class HelperTest(unittest.TestCase):
    def test_split_and_safe_name(self):
        parts = _split("あ" * 2500 + "\n\n" + "い" * 100 + "\n\n" + "う" * 50, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("".join(parts).replace("\n", ""), "あ" * 2500 + "い" * 100 + "う" * 50)
        self.assertEqual(safe_name('見積: A社/B社 [案] #1?'), "見積 A社 B社 案 1")
        self.assertEqual(safe_name("..."), "無題")


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.llm, self.llm_url, self.reqs = start_mock_llm()
        self.app = MycelApp(config_path=root / "cfg.json", vault_override=str(root / "vault"), initial_load="sync")
        self.docx = make_docx(root / "見積書.docx").read_bytes()

    def tearDown(self):
        self.llm.shutdown()
        self.app.close()
        self.tmp.cleanup()

    def llm_on(self, **extra):
        self.app.update_config({"base_url": self.llm_url + "/v1", "model": "mock", **extra})

    def run_job(self, ids=None, **opts):
        self.app.ingest.start(ids, opts)
        st = self.app.jobs.wait(20)
        self.assertEqual(st["state"], "done", st)
        return st

    def test_upload_draft_and_save_with_llm(self):
        self.llm_on()
        d = self.app.ingest.upload("見積書.docx", self.docx)
        self.assertEqual(d["status"], "queued")
        self.run_job()
        d = self.app.ingest.get(d["id"])
        self.assertEqual(d["status"], "ready", d)
        self.assertTrue(d["llm"])
        self.assertEqual(d["note_path"], "取り込み/A社 見積の概要.md")
        self.assertEqual(d["original_path"], "資料/見積書.docx")
        md = d["markdown"]
        self.assertIn('元の資料: [[資料/見積書.docx]]', md)
        self.assertIn("> **要約** A社向けの概算見積", md)
        self.assertIn("- 初期費用 1,200万円", md)
        self.assertIn("[[田中部長（A社）|田中部長]]", md)            # 既存ノートの名前に合わせてリンク
        self.assertIn("- 顧客・取引先: A社", md)                      # 未作成の名前は [[ ]] にしない（人物・組織でつなぐ）
        self.assertIn("## 関連ノート", md)
        self.assertIn("— 同じ顧客の案件", md)
        self.assertIn("#取り込み #見積 #A社", md)
        self.assertNotIn("## 本文", md)                               # 原本を保存するので本文は省く
        # RAG: 既存ノートの抜粋を候補として LLM に渡している
        link_req = [r for r in self.reqs if "[TASK:relate]" in r["body"]["messages"][-1]["content"]]
        self.assertTrue(link_req)
        res = self.app.ingest_save([d["id"]])
        self.assertEqual(res["errors"], [])
        path = res["saved"][0]["path"]
        self.assertTrue((self.app.vault.root / "資料" / "見積書.docx").is_file())
        self.assertIsNotNone(self.app.index.get("資料/見積書.docx"))   # 原本は資料として登録
        note = self.app.note(path)
        self.assertIn("資料/見積書.docx", [o["path"] for o in note["outgoing"]])
        self.assertIn(path, [b["path"] for b in self.app.index.backlinks("人物/田中部長（A社）.md")])
        self.assertEqual(self.app.ingest.get(d["id"])["status"], "saved")
        people = {(e["type"], e["key"]) for e in self.app.people.of(path)}         # LLM の名前を人物・組織として登録
        self.assertIn(("person", "田中"), people)
        self.assertIn(("org", "a"), people)
        self.assertIn(("org", "a"), {(e["type"], e["key"]) for e in self.app.people.of("資料/見積書.docx")})
        with self.assertRaises(VaultError):
            self.app.ingest.edit(d["id"], markdown="x")

    def test_long_document_is_read_in_parts(self):
        self.llm_on(ingest_chunk_chars=500)
        body = "\n\n".join(f"第{i}章 " + "本文" * 200 for i in range(6))
        d = self.app.ingest.upload("長い報告.txt", body.encode("utf-8"))
        self.run_job(keep_original=False)
        d = self.app.ingest.get(d["id"])
        maps = [r for r in self.reqs if "[TASK:ingest_map]" in r["body"]["messages"][-1]["content"]]
        self.assertGreaterEqual(len(maps), 6)
        final = [r for r in self.reqs if "[TASK:ingest]" in r["body"]["messages"][-1]["content"]][-1]
        self.assertIn("部分ごとの要点", final["body"]["messages"][-1]["content"])
        self.assertIn("## 本文", d["markdown"])                      # 原本を残さないので本文を入れる
        self.assertIn("元のファイル: 長い報告.txt", d["markdown"])

    def test_without_llm_still_makes_draft(self):
        self.app.update_config({"model": ""})
        d = self.app.ingest.upload("見積書.docx", self.docx)
        self.run_job()
        d = self.app.ingest.get(d["id"])
        self.assertEqual(d["status"], "ready")
        self.assertFalse(d["llm"])
        self.assertEqual(d["note_path"], "取り込み/見積書.md")
        self.assertTrue(d["markdown"].startswith("---\n種別: 取り込み資料"))
        self.assertEqual(L.split_frontmatter(d["markdown"])[0]["種別"], "取り込み資料")

    def test_llm_down_falls_back(self):
        self.app.update_config({"base_url": "http://127.0.0.1:9/v1", "model": "m", "request_timeout": 5})
        d = self.app.ingest.upload("見積書.docx", self.docx)
        self.run_job()
        d = self.app.ingest.get(d["id"])
        self.assertEqual(d["status"], "ready")
        self.assertIn("LLM を使えなかった", d["error"])

    def test_existing_doc_edit_discard_and_errors(self):
        self.llm_on()
        (self.app.vault.root / "資料").mkdir()
        (self.app.vault.root / "資料" / "既存.docx").write_bytes(self.docx)
        self.app.update_index(None, wait=True)
        d = self.app.ingest.add_paths(["資料/既存.docx"])[0]
        self.assertEqual(d["origin"], "doc")
        bad = self.app.ingest.upload("壊れ.docx", b"broken")
        self.run_job()
        self.assertEqual(self.app.ingest.get(bad["id"])["status"], "error")
        d = self.app.ingest.get(d["id"])
        self.assertIn('[[資料/既存.docx]]', d["markdown"])
        self.assertEqual(d["original_path"], "資料/既存.docx")
        d = self.app.ingest.edit(d["id"], markdown="# 手で直した\n", note_path="取り込み/手直し")
        self.assertEqual(d["note_path"], "取り込み/手直し.md")
        res = self.app.ingest_save([d["id"], bad["id"]])
        self.assertEqual(res["saved"][0]["path"], "取り込み/手直し.md")
        self.assertEqual(len(res["errors"]), 1)
        self.assertEqual(self.app.ingest.discard([bad["id"]]), 1)
        with self.assertRaises(VaultError):
            self.app.ingest.upload("写真.png", b"\x89PNG")
        with self.assertRaises(VaultError):
            self.app.ingest.upload("メモ.md", b"# x")
        with self.assertRaises(VaultError):
            self.app.ingest.add_paths(["ホーム"])

    def test_name_collision_on_save(self):
        self.llm_on()
        a = self.app.ingest.upload("見積書.docx", self.docx)
        b = self.app.ingest.upload("見積書.docx", self.docx)
        self.run_job()
        a, b = self.app.ingest.get(a["id"]), self.app.ingest.get(b["id"])
        self.assertNotEqual(a["note_path"], b["note_path"])
        self.assertNotEqual(a["original_path"], b["original_path"])
        (self.app.vault.root / "資料").mkdir(exist_ok=True)
        (self.app.vault.root / a["original_path"]).write_bytes(b"other")   # 下書き後に同名ができた
        res = self.app.ingest_save([a["id"], b["id"]])
        self.assertEqual(res["errors"], [])
        saved = self.app.note(res["saved"][0]["path"])
        orig = res["saved"][0]["original"]
        self.assertNotEqual(orig, a["original_path"])
        self.assertIn(f"[[{orig}]]", saved["text"])


class IngestApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.llm, cls.llm_url, _ = start_mock_llm()
        cls.app = MycelApp(config_path=root / "cfg.json", vault_override=str(root / "vault"), initial_load="sync")
        cls.app.update_config({"base_url": cls.llm_url + "/v1", "model": "mock"})
        cls.docx = make_docx(root / "x.docx").read_bytes()
        cls.httpd = server.make_server(cls.app, 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.llm.shutdown()
        cls.app.close()
        cls.tmp.cleanup()

    def call(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=data, headers=headers or {})
        res = conn.getresponse()
        body = json.loads(res.read() or b"{}")
        conn.close()
        return res.status, body

    def post(self, path, obj):
        return self.call("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"})

    def upload(self, name, data, **headers):
        h = {"Content-Type": "application/octet-stream", "X-Filename": quote(name)}
        h.update(headers)
        return self.call("POST", "/api/ingest/upload", data, h)

    def test_flow(self):
        st, d = self.upload("A社 見積.docx", self.docx)
        self.assertEqual(st, 200, d)
        st, lst = self.call("GET", "/api/ingest")
        self.assertTrue(lst["llm"]["chat"])
        self.assertTrue(lst["llm"]["local"])
        self.assertIn(d["id"], [x["id"] for x in lst["drafts"]])
        st, job = self.post("/api/ingest/run", {"ids": [d["id"]], "options": {"dest_folder": "受信"}})
        self.assertEqual(st, 200, job)
        for _ in range(100):
            st, ix = self.call("GET", "/api/index/status")
            if ix["job"]["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(ix["job"]["kind"], "ingest")
        self.assertEqual(ix["job"]["state"], "done", ix)
        st, lst = self.call("GET", "/api/ingest")
        dd = next(x for x in lst["drafts"] if x["id"] == d["id"])
        self.assertTrue(dd["note_path"].startswith("受信/"))
        self.assertEqual(lst["options"]["dest_folder"], "受信")         # 次回の既定に残る
        st, r = self.post("/api/ingest/save", {"ids": [d["id"]]})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["saved"][0]["path"].startswith("受信/"))

    def test_upload_guards(self):
        st, _ = self.upload("x.docx", self.docx, Origin="http://evil.example")
        self.assertEqual(st, 403)
        st, _ = self.call("POST", "/api/ingest/upload", self.docx, {"Content-Type": "text/plain", "X-Filename": "x.docx"})
        self.assertEqual(st, 415)
        st, _ = self.upload("../../x.png", b"\x89PNG")
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/api/note/save", b"{}", {"Content-Type": "application/octet-stream"})
        self.assertEqual(st, 415)

    def test_models(self):
        st, r = self.post("/api/llm/models", {"base_url": self.llm_url + "/v1"})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["models"], ["mock", "mock-embed"])
        st, r = self.post("/api/llm/models", {"base_url": "http://127.0.0.1:9/v1"})
        self.assertEqual(st, 502)


if __name__ == "__main__":
    unittest.main()
