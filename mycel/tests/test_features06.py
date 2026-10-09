"""v0.6: 版の表示、作成日・更新日、Vault の切り替え、RAG の深さとリランク、画像（VLM）、文書管理の気づき。"""
from __future__ import annotations

import http.client
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))

import server  # noqa: E402
from mock_llm import start_mock_llm  # noqa: E402
from mycelcore import __version__  # noqa: E402
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.llm import LLMError  # noqa: E402
from mycelcore.vault import VaultError  # noqa: E402
from samples import make_docx  # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7357e5e0000000049454e44ae426082")


class VersionTest(unittest.TestCase):
    def test_client_and_server_version_match(self):
        js = (BASE / "static" / "app.js").read_text(encoding="utf-8")
        m = re.search(r'const CLIENT_VERSION = "([^"]+)"', js)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), __version__)
        self.assertNotEqual(__version__, "0.1.0")


class FeatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.llm, self.llm_url, self.reqs = start_mock_llm()
        self.app = MycelApp(config_path=self.root / "cfg.json", vault_override=str(self.root / "vault"), initial_load="sync")

    def tearDown(self):
        self.llm.shutdown()
        self.app.close()
        self.tmp.cleanup()

    def llm_on(self, **extra):
        self.app.update_config({"base_url": self.llm_url + "/v1", "model": "mock", **extra})

    def prompts(self, start=0):
        return [json.dumps(r["body"], ensure_ascii=False) for r in self.reqs[start:]]

    # ---- 作成日・更新日
    def test_created_and_modified_dates(self):
        n = self.app.note("ホーム.md")
        self.assertTrue(n["created"] and n["modified"])
        self.assertLessEqual(n["created"], n["modified"] + 1)
        before = n["created"]
        time.sleep(0.05)
        self.app.save("ホーム.md", n["text"] + "\n追記", n["version"])
        n2 = self.app.note("ホーム.md")
        self.assertEqual(round(n2["created"], 3), round(before, 3))      # 作成日は変わらない
        self.assertGreaterEqual(n2["modified"], n["modified"])
        # frontmatter の作成日が優先される
        r = self.app.create("tmp/日付.md", text="---\n作成日: 2024-03-05\n---\n# 日付\n")
        d = self.app.note(r["path"])
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(d["created"])), "2024-03-05")
        (self.root / "vault" / "資料").mkdir(exist_ok=True)
        doc = make_docx(self.root / "vault" / "資料" / "a.docx")
        self.app.index.refresh("資料/a.docx")
        dd = self.app.note("資料/a.docx")
        self.assertTrue(dd["created"] and dd["modified"])

    # ---- Vault の切り替え
    def test_vault_switch_and_recent(self):
        cur = str(self.app.vault.root)
        lst = self.app.vault_list()
        self.assertTrue(lst["vaults"][0]["current"])
        other = self.root / "docs-vault"
        with self.assertRaises(VaultError):
            self.app.vault_switch(str(other))                       # 無ければ作らない
        with self.assertRaises(VaultError):
            self.app.vault_switch("relative/path", create=True)
        r = self.app.vault_switch(str(other), create=True, seed=False)
        self.assertEqual(Path(r["vault"]).resolve(), other.resolve())
        self.assertTrue(other.is_dir())
        self.assertEqual(self.app.index_status()["notes"], 0)             # サンプルは入れていない
        self.assertIn(str(Path(cur).resolve()), [v["path"] for v in r["vaults"]])
        self.assertIn(str(Path(cur).resolve()), [str(Path(p).resolve()) for p in self.app.config()["recent_vaults"]])
        self.app.create("メモ.md", text="# メモ\n")
        # 元に戻る
        r2 = self.app.vault_switch(cur)
        self.assertEqual(Path(r2["vault"]).resolve(), Path(cur).resolve())
        self.assertIsNotNone(self.app.index.get("ホーム.md"))
        self.assertIn(str(other.resolve()), [str(Path(p).resolve()) for p in self.app.config()["recent_vaults"]])
        # 新規 Vault にサンプルを入れる
        r3 = self.app.vault_switch(str(self.root / "v3"), create=True, seed=True)
        self.assertGreater(self.app.index_status()["notes"], 0)
        self.assertEqual(len(r3["vaults"]), 3)

    # ---- RAG の深さとリランク
    def test_rag_depth_config_and_llm_rerank(self):
        cfg = self.app.config()
        self.assertEqual((cfg["rag_top_k"], cfg["rag_pool"], cfg["rag_rerank"]), (6, 60, "none"))
        self.app.update_config({"rag_top_k": 2, "rag_pool": 5000, "rag_rerank": "bogus", "rag_rerank_pool": 1})
        cfg = self.app.config()
        self.assertEqual((cfg["rag_top_k"], cfg["rag_pool"], cfg["rag_rerank"], cfg["rag_rerank_pool"]), (2, 1000, "none", 4))
        self.llm_on()
        hits = self.app.ai.retrieve_for_answer("A社の決裁者")
        self.assertEqual(len(hits), 2)
        self.assertFalse(any("[TASK:rerank]" in p for p in self.prompts()))
        st = self.app.ai.status()
        self.assertEqual(st["rag"]["top_k"], 2)
        # LLM リランク: 候補を多めに集めて採点し、上位だけ渡す
        self.app.update_config({"rag_rerank": "llm", "rag_rerank_pool": 8, "rag_top_k": 3})
        n0 = len(self.reqs)
        hits = self.app.ai.retrieve_for_answer("A社 稼働率 提案 商談 更改")
        rer = [p for p in self.prompts(n0) if "[TASK:rerank]" in p]
        self.assertEqual(len(rer), 1)
        self.assertEqual(len(hits), 3)
        self.assertTrue(all("rerank" in h for h in hits))
        self.assertGreaterEqual(hits[0]["rerank"], hits[-1]["rerank"])
        self.assertIn("稼働率", hits[0]["text"])                          # モックは「稼働率」を含む候補を最高点にする
        # 質問でも使われる
        n1 = len(self.reqs)
        res = self.app.ai.ask("A社 稼働率 提案 商談 更改")
        self.assertTrue(any("[TASK:rerank]" in p for p in self.prompts(n1)))
        self.assertLessEqual(len(res["sources"]), 3)

    # ---- 画像
    def test_images_are_docs_and_vlm_caption(self):
        img = self.app.upload_file("添付", "グラフ.png", PNG)
        self.assertEqual(img["status"], "ok")
        it = self.app.index.get("添付/グラフ.png")
        self.assertEqual(it["grp"], "image")
        self.assertEqual(it["text"], "")
        d = self.app.note("添付/グラフ.png")
        self.assertTrue(d["readonly"])
        self.assertIsNone(d["caption"])
        self.assertEqual(self.app.images_status(), {"total": 1, "captioned": 0, "vlm": False, "model": ""})
        with self.assertRaises(LLMError):
            self.app.caption_image("添付/グラフ.png")                     # VLM 未設定
        self.llm_on(vlm_model="mock-vl")
        n0 = len(self.reqs)
        r = self.app.caption_image("添付/グラフ.png")
        self.assertIn("売上推移", r["caption"])
        body = self.reqs[n0]["body"]
        self.assertEqual(body["model"], "mock-vl")
        parts = body["messages"][-1]["content"]
        self.assertTrue(any(p["type"] == "image_url" and p["image_url"]["url"].startswith("data:image/png;base64,") for p in parts))
        # 説明が本文になり、検索と質問の根拠になる
        it = self.app.index.get("添付/グラフ.png")
        self.assertIn("売上推移", it["text"])
        self.assertTrue(any(h["path"] == "添付/グラフ.png" for h in self.app.ai.retrieve("売上推移 グラフ", k=5)))
        d = self.app.note("添付/グラフ.png")
        self.assertEqual(d["caption"]["model"], "mock-vl")
        self.assertEqual(self.app.images_status()["captioned"], 1)
        # 名前を変えても説明は付いてくる
        self.app.move_item("添付/グラフ.png", "添付/売上.png")
        self.assertIn("売上推移", self.app.index.get("添付/売上.png")["text"])
        # まとめて読む（未読だけ）
        self.app.upload_file("添付", "写真2.png", PNG)
        self.app.caption_images()
        st = self.app.jobs.wait(20)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["result"]["done"], 1)
        self.assertEqual(self.app.images_status()["captioned"], 2)
        # 画像は AI 取り込みの対象外
        with self.assertRaises(VaultError):
            self.app.ingest.upload("x.png", PNG)

    # ---- 文書管理: 気づき・この文書に質問・Vault 全体の関連
    def test_library_insights_ask_doc_and_vault_related(self):
        self.llm_on()
        (self.root / "vault" / "資料").mkdir(exist_ok=True)
        make_docx(self.root / "vault" / "資料" / "報告書.docx")
        self.app.index.refresh("資料/報告書.docx")
        r = self.app.library.register_files(["資料/報告書.docx"], use_ai=False)["added"][0]
        rel = self.app.library.related_in_vault(r["id"])
        self.assertTrue(rel)
        self.assertTrue(all(x["path"] != "資料/報告書.docx" for x in rel))
        self.assertIn("顧客/A社 生産管理システム更改.md", [x["path"] for x in rel])
        ins = self.app.library.insights(r["id"])
        self.assertIn("初期費用 1,200万円", ins["takeaways"])
        self.assertEqual(len(ins["connections"]), 1)
        self.assertTrue(ins["connections"][0]["path"])
        self.assertTrue(ins["actions"])
        self.assertEqual(self.app.library.get(r["id"])["insights"]["takeaways"], ins["takeaways"])
        prompt = [p for p in self.prompts() if "[TASK:insights]" in p][-1]
        self.assertIn("関連する文書", prompt)
        res = self.app.library.ask_doc(r["id"], "初期費用は？")
        self.assertTrue(res["llm"])
        self.assertTrue(all(s["path"] == "資料/報告書.docx" for s in res["sources"]))
        self.assertEqual(res["sources"][0]["ref_id"], r["id"])


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.llm, cls.llm_url, cls.reqs = start_mock_llm()
        cls.app = MycelApp(config_path=root / "cfg.json", vault_override=str(root / "vault"), initial_load="sync")
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

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        conn.request(method, path, body=data, headers=h)
        res = conn.getresponse()
        payload, ctype, disp = res.read(), res.getheader("Content-Type"), res.getheader("Content-Disposition") or ""
        conn.close()
        try:
            return res.status, json.loads(payload), ctype, disp
        except ValueError:
            return res.status, payload, ctype, disp

    def test_state_version_image_inline_and_vault_api(self):
        st, s, *_ = self.call("GET", "/api/state")
        self.assertEqual(s["version"], __version__)
        self.assertIn("attachment_folder", s)
        self.assertFalse(s["vlm"])
        # 画像のアップロード（octet-stream）と inline 配信
        st, up, *_ = self.call("POST", "/api/file/upload", raw=PNG, headers={"Content-Type": "application/octet-stream", "X-Filename": "%E5%9B%B3.png", "X-Folder": "%E6%B7%BB%E4%BB%98"})
        self.assertEqual(st, 200, up)
        self.assertEqual(up["path"], "添付/図.png")
        st, data, ctype, disp = self.call("GET", "/api/file?" + urlencode({"path": "添付/図.png", "inline": "1"}))
        self.assertEqual((st, ctype), (200, "image/png"))
        self.assertTrue(disp.startswith("inline"))
        self.assertEqual(data, PNG)
        st, data, ctype, disp = self.call("GET", "/api/file?" + urlencode({"path": "添付/図.png"}))
        self.assertEqual(ctype, "application/octet-stream")
        self.assertTrue(disp.startswith("attachment"))
        st, n, *_ = self.call("GET", "/api/note?" + urlencode({"path": "添付/図.png"}))
        self.assertEqual(n["grp"], "image")
        st, ims, *_ = self.call("GET", "/api/images")
        self.assertEqual(ims["total"], 1)
        st, ai, *_ = self.call("GET", "/api/ai/status")
        self.assertEqual(ai["rag"]["top_k"], 6)
        self.assertEqual(ai["images"]["total"], 1)
        st, v, *_ = self.call("GET", "/api/vault/list")
        self.assertTrue(v["vaults"][0]["current"])
        st, e, *_ = self.call("POST", "/api/vault/switch", {"path": "nope"})
        self.assertEqual(st, 400)
        st, cfg, *_ = self.call("POST", "/api/config", {"rag_rerank": "llm", "rag_top_k": 9, "vlm_model": "llava"})
        self.assertEqual((cfg["rag_rerank"], cfg["rag_top_k"], cfg["vlm_model"]), ("llm", 9, "llava"))
