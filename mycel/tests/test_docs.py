"""資料（Word・Excel・PDF など）の読み込み、読み込み範囲、手動更新のテスト。"""
from __future__ import annotations

import http.client
import json
import os
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
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.extract import ExtractError, decode_text, extract, is_supported, pdf_available  # noqa: E402
from mycelcore.jobs import JobBusy, JobRunner  # noqa: E402
from mycelcore.scope import Scope, ScopeError  # noqa: E402
from mycelcore.vault import VaultError  # noqa: E402
from samples import make_all, make_docx, make_pdf  # noqa: E402


class ExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.files = make_all(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_office_and_mail(self):
        docx = extract(self.files["docx"])
        self.assertIn("# 見積書", docx)
        self.assertIn("- 初期費用 1,200万円", docx)
        self.assertIn("| 保守 | 月額 30万円 |", docx)
        self.assertIn("A社", extract(self.files["xlsx"]))
        self.assertIn("|", extract(self.files["xlsx"]))
        self.assertIn("スライド", extract(self.files["pptx"]))
        eml = extract(self.files["eml"])
        self.assertIn("見積", eml)

    def test_text_like(self):
        self.assertIn("稼働率99%", extract(self.files["txt"]))           # cp932 を判別
        self.assertIn("| 4月 | 120 |", extract(self.files["csv"]))
        html = extract(self.files["html"])
        self.assertIn("製品紹介", html)
        self.assertIn("在庫の一元化", html)
        self.assertNotIn("alert", html)

    def test_decode(self):
        self.assertEqual(decode_text("日本語".encode("euc_jp")), "日本語")
        self.assertEqual(decode_text("﻿abc".encode("utf-8")), "abc")

    def test_unsupported_and_broken(self):
        self.assertFalse(is_supported("写真.png"))
        self.assertTrue(is_supported("a.DOCX"))
        bad = Path(self.tmp.name) / "壊れた.docx"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(ExtractError):
            extract(bad)

    @unittest.skipUnless(pdf_available(), "pypdf がない")
    def test_pdf(self):
        self.assertIn("Mycel PDF sample text", extract(self.files["pdf"]))


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        (self.vault / ".mycel").mkdir(parents=True)
        (self.vault / "a").mkdir()
        (self.vault / "a" / "n.md").write_text("# n", encoding="utf-8")
        (self.vault / "b").mkdir()
        (self.vault / "b" / "x.txt").write_text("x", encoding="utf-8")
        (self.vault / "b" / "img.png").write_bytes(b"\x89PNG")
        self.ext = root / "ext"
        (self.ext / "sub").mkdir(parents=True)
        make_docx(self.ext / "sub" / "見積.docx")
        self.scope = Scope(self.vault, self.vault / ".mycel")

    def tearDown(self):
        self.tmp.cleanup()

    def paths(self, prefixes=None):
        return sorted(p for p, *_ in self.scope.walk(prefixes))

    def test_walk_exclude_types_and_sources(self):
        self.assertEqual(self.paths(), ["a/n.md", "b/x.txt"])
        self.scope.save({"sources": [{"path": str(self.ext), "label": "共有"}]})
        self.assertIn("@s1/sub/見積.docx", self.paths())
        self.assertEqual(self.paths(["@s1"]), ["@s1/sub/見積.docx"])
        self.assertEqual(self.paths(["a"]), ["a/n.md"])
        self.scope.save({"exclude": ["b"], "types": ["note", "word"]})
        self.assertEqual(self.paths(), ["@s1/sub/見積.docx", "a/n.md"])
        # 保存した設定は読み直しても残る
        again = Scope(self.vault, self.vault / ".mycel")
        self.assertEqual(again.data["exclude"], ["b"])
        self.assertEqual(again.sources()[1].label, "共有")

    def test_list_dir_states(self):
        self.scope.save({"max_mb": 1})
        r = self.scope.list_dir("b")
        self.assertEqual([f["name"] for f in r["files"]], ["x.txt"])
        self.assertEqual(r["unsupported"], 1)
        self.assertEqual(r["files"][0]["state"], "in")
        self.scope.save({"types": ["note"]})
        self.assertEqual(self.scope.list_dir("b")["files"][0]["state"], "type_off")

    def test_reject_bad_sources_and_paths(self):
        with self.assertRaises(ScopeError):
            self.scope.save({"sources": [{"path": str(self.vault / "a")}]})     # Vault と重なる
        with self.assertRaises(ScopeError):
            self.scope.save({"sources": [{"path": str(self.ext)}, {"path": str(self.ext / "sub")}]})
        with self.assertRaises(ScopeError):
            self.scope.abs_path("a/../../secret.txt")
        with self.assertRaises(ScopeError):
            self.scope.abs_path("@s9/x.docx")


class JobRunnerTest(unittest.TestCase):
    def test_single_job_and_cancel(self):
        runner = JobRunner()

        def slow(job):
            for i in range(200):
                if job.cancel.is_set():
                    from mycelcore.index import Cancelled
                    raise Cancelled()
                job.progress("x", i, 200)
                time.sleep(0.01)
            return {"ok": 1}

        runner.start("update", "t", None, slow)
        with self.assertRaises(JobBusy):
            runner.start("scan", "t2", None, slow)
        self.assertTrue(runner.cancel())
        st = runner.wait(5)
        self.assertEqual(st["state"], "cancelled")
        st = runner.start("update", "t3", None, lambda job: {"n": 1}, wait=True)
        self.assertEqual(runner.status()["result"], {"n": 1})


class ManualUpdateTest(unittest.TestCase):
    """初回だけ自動で読み込み、以降は「更新」を押したときだけ読み込む。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        make_all(self.vault / "資料")
        self.ext = root / "ext"
        (self.ext / "見積").mkdir(parents=True)
        (self.ext / "提案").mkdir()
        make_docx(self.ext / "見積" / "A社.docx")
        (self.ext / "提案" / "B社.txt").write_text("B社への提案 物流DX", encoding="utf-8")
        self.app = MycelApp(config_path=root / "cfg.json", vault_override=str(self.vault), initial_load="sync")

    def tearDown(self):
        self.app.plugins.unload()
        self.app.index.close()
        self.tmp.cleanup()

    def test_initial_load_reads_docs(self):
        st = self.app.index_status()
        self.assertEqual(st["notes"], 0)            # 空でない Vault にはサンプルを入れない
        self.assertGreaterEqual(st["docs"], 7 if pdf_available() else 6)
        self.assertIsNone(self.app.index.get("資料/写真.png"))
        d = self.app.note("資料/見積書.docx")
        self.assertTrue(d["readonly"])
        self.assertEqual(d["kind"], "doc")
        self.assertIn("1,200万円", d["text"])
        hits = self.app.index.search("稼働率")
        self.assertEqual(hits[0]["path"], "資料/議事メモ.txt")
        self.assertEqual(hits[0]["kind"], "doc")

    def test_docs_are_read_only(self):
        with self.assertRaises(VaultError):
            self.app.save("資料/見積書.docx", "x", None)
        with self.assertRaises(VaultError):
            self.app.delete("資料/見積書.docx")
        with self.assertRaises(VaultError):
            self.app.rename("資料/見積書.docx", "別名")

    def test_changes_wait_for_update_and_scope_linked_update(self):
        self.app.save_scope({"sources": [{"path": str(self.ext), "label": "共有"}]})
        self.assertIsNone(self.app.index.get("@s1/見積/A社.docx"))       # 範囲を変えただけでは読まない
        r = self.app.check_index(["@s1"], wait=True)["result"]
        self.assertEqual(r["added"], 2)
        tree = self.app.scope_tree("@s1/見積")
        self.assertEqual(tree["files"][0]["status"], "new")
        # 選んだフォルダだけ更新
        self.app.update_index(["@s1/見積"], wait=True)
        self.assertIsNotNone(self.app.index.get("@s1/見積/A社.docx"))
        self.assertIsNone(self.app.index.get("@s1/提案/B社.txt"))
        self.assertEqual(self.app.index_status()["pending"]["added"], 1)
        self.assertEqual(self.app.scope_tree("@s1/見積")["files"][0]["status"], "indexed")
        # 変更も「更新」まで反映しない
        p = self.ext / "見積" / "A社.docx"
        make_docx(p)
        os.utime(p, (time.time() + 5, time.time() + 5))
        self.app.check_index(None, wait=True)
        self.assertEqual(self.app.scope_tree("@s1/見積")["files"][0]["status"], "modified")
        self.assertTrue(self.app.note("@s1/見積/A社.docx")["stale"])
        self.app.update_index(None, wait=True)
        st = self.app.index_status()["pending"]
        self.assertEqual(st, {"added": 0, "modified": 0, "deleted": 0})
        # 除外すると、更新で消える
        self.app.save_scope({"exclude": ["@s1/提案"]})
        self.app.update_index(["@s1"], wait=True)
        self.assertIsNone(self.app.index.get("@s1/提案/B社.txt"))
        self.assertIsNotNone(self.app.index.get("@s1/見積/A社.docx"))
        # 外部フォルダを外すと、更新で消える
        self.app.save_scope({"sources": []})
        self.app.update_index(["@s1"], wait=True)
        self.assertIsNone(self.app.index.get("@s1/見積/A社.docx"))

    def test_ai_retrieve_limited_to_prefix(self):
        self.app.save_scope({"sources": [{"path": str(self.ext), "label": "共有"}]})
        self.app.update_index(None, wait=True)
        allhits = {h["path"] for h in self.app.ai.retrieve("物流DX 提案", k=10)}
        self.assertIn("@s1/提案/B社.txt", allhits)
        only = {h["path"] for h in self.app.ai.retrieve("物流DX 提案", k=10, prefixes=["@s1"])}
        self.assertTrue(only)
        self.assertTrue(all(p.startswith("@s1/") for p in only))
        vault = {h["path"] for h in self.app.ai.retrieve("物流DX 提案", k=10, prefixes=[""])}
        self.assertTrue(all(not p.startswith("@") for p in vault))

    def test_import_doc_as_note(self):
        r = self.app.import_doc("資料/見積書.docx")
        self.assertEqual(r["path"], "取り込み/見積書.md")
        n = self.app.note(r["path"])
        self.assertIn("1,200万円", n["text"])
        self.assertIn("[[資料/見積書.docx]]", n["text"])
        self.assertIn(r["path"], {b["path"] for b in self.app.index.backlinks("資料/見積書.docx")})
        self.assertEqual(self.app.import_doc("資料/見積書.docx")["path"], "取り込み/見積書 (2).md")

    def test_error_file_is_reported(self):
        before = self.app.index_status()["errors"]     # pypdf が無い環境では PDF もエラー扱い
        (self.vault / "資料" / "壊れた.xlsx").write_bytes(b"broken")
        self.app.update_index(None, wait=True)
        st = self.app.index_status()
        self.assertEqual(st["errors"], before + 1)
        d = self.app.note("資料/壊れた.xlsx")
        self.assertEqual(d["status"], "error")
        self.assertTrue(d["error"])


class DocsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        make_all(root / "vault" / "資料")
        cls.ext = root / "ext"
        cls.ext.mkdir()
        make_pdf(cls.ext / "案内.pdf")
        cls.llm, cls.llm_url, _ = start_mock_llm()
        cls.app = MycelApp(config_path=root / "cfg.json", vault_override=str(root / "vault"), initial_load="sync")
        cls.httpd = server.make_server(cls.app, 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.llm.shutdown()
        cls.app.plugins.unload()
        cls.app.index.close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        res = conn.getresponse()
        payload = res.read()
        headers = dict(res.getheaders())
        conn.close()
        try:
            return res.status, json.loads(payload), headers
        except ValueError:
            return res.status, payload, headers

    def get(self, _path, **params):
        return self.call("GET", _path + ("?" + urlencode(params) if params else ""))

    def post(self, path, body=None):
        return self.call("POST", path, body or {})

    def wait_job(self):
        for _ in range(100):
            st, ix, _ = self.get("/api/index/status")
            if not ix["job"] or ix["job"]["state"] != "running":
                return ix
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_scope_api_and_update(self):
        st, sc, _ = self.get("/api/scope")
        self.assertEqual(st, 200)
        self.assertEqual(sc["sources"][0]["id"], "vault")
        self.assertIn("pdf", [g["id"] for g in sc["type_groups"]])
        st, sc, _ = self.post("/api/scope", {"sources": [{"path": str(self.ext), "label": "外部"}]})
        self.assertEqual(st, 200, sc)
        self.assertEqual(sc["sources"][1]["prefix"], "@s1")
        st, tree, _ = self.get("/api/scope/tree", path="@s1")
        self.assertEqual(tree["files"][0]["status"], "new")
        st, job, _ = self.post("/api/index/update", {"prefixes": ["@s1"]})
        self.assertEqual(st, 200)
        ix = self.wait_job()
        self.assertEqual(ix["job"]["state"], "done")
        st, t, _ = self.get("/api/tree")
        self.assertIn("@s1", [s["prefix"] for s in t["sources"]])
        if pdf_available():
            self.assertIn("@s1/案内.pdf", [n["path"] for n in t["notes"]])
        st, br, _ = self.get("/api/scope/browse", path=str(self.ext.parent))
        self.assertIn("ext", [d["name"] for d in br["dirs"]])
        st, err, _ = self.post("/api/scope", {"sources": [{"path": str(self.ext / "無い")}]})
        self.assertEqual(st, 400)

    def test_doc_view_and_download(self):
        st, d, _ = self.get("/api/note", path="資料/案件一覧.xlsx")
        self.assertEqual(st, 200)
        self.assertTrue(d["readonly"])
        st, v, _ = self.get("/api/note/version", path="資料/案件一覧.xlsx")
        self.assertTrue(v["exists"])
        st, raw, h = self.get("/api/file", path="資料/ページ.html")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", h["Content-Disposition"])
        self.assertIn(b"<html>", raw)
        st, _, _ = self.get("/api/file", path="../cfg.json")
        self.assertIn(st, (400, 403, 404))
        st, e, _ = self.post("/api/note/save", {"path": "資料/案件一覧.xlsx", "text": "x"})
        self.assertEqual(st, 403)

    def test_busy_and_ask_with_prefixes(self):
        st, r, _ = self.post("/api/ai/ask", {"question": "稼働率の条件は", "prefixes": ["資料"]})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["sources"])
        self.assertTrue(all(s["path"].startswith("資料/") for s in r["sources"]))
        st, body, _ = self.post("/api/config", {"provider": "openai", "base_url": self.llm_url + "/v1", "model": "mock"})
        st, r, _ = self.post("/api/ai/ask", {"question": "稼働率の条件は"})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["llm"])
        self.post("/api/config", {"base_url": "", "model": ""})


if __name__ == "__main__":
    unittest.main()
