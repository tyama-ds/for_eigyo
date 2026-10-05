"""資料同士のつながりと、フォルダ・資料の整理（移動・名前変更・削除・追加）のテスト。"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
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
from mycelcore.relations import Relations  # noqa: E402
from mycelcore.vault import VaultError  # noqa: E402
from samples import make_docx, make_pptx  # noqa: E402


class RelationsUnitTest(unittest.TestCase):
    def test_add_remove_rename_drop(self):
        with tempfile.TemporaryDirectory() as t:
            r = Relations(Path(t))
            r.add("a/x.pdf", "b/y.docx", "改訂版", "ai")
            r.add("b/y.docx", "a/x.pdf", "", "user")               # 逆向きも同じつながり
            self.assertEqual(len(r.items), 1)
            self.assertEqual(r.items[0]["origin"], "user")
            self.assertEqual(r.items[0]["label"], "改訂版")
            self.assertEqual(r.for_path("b/y.docx")[0]["path"], "a/x.pdf")
            r.rename("a", "c/a")                                    # フォルダの移動
            self.assertEqual(r.items[0]["src"], "c/a/x.pdf")
            self.assertEqual(Relations(Path(t)).items[0]["src"], "c/a/x.pdf")   # 保存されている
            with self.assertRaises(ValueError):
                r.add("p", "p")
            r.add("c/a/x.pdf", "z.md")
            self.assertEqual(r.drop("c/a"), 2)
            self.assertEqual(r.items, [])

    def test_rewrite_link_prefix(self):
        text = "[[資料/見積.docx]] [[資料/古い/仕様#節|仕様]] [[資料館]]\n```\n[[資料/x]]\n```"
        out, n = L.rewrite_link_prefix(text, "資料", "営業/資料")
        self.assertEqual(n, 2)
        self.assertIn("[[営業/資料/見積.docx]]", out)
        self.assertIn("[[営業/資料/古い/仕様#節|仕様]]", out)
        self.assertIn("[[資料館]]", out)
        self.assertIn("[[資料/x]]", out)


class OrganizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.llm, self.llm_url, self.reqs = start_mock_llm()
        v = root / "vault"
        (v / "資料" / "見積").mkdir(parents=True)
        make_docx(v / "資料" / "見積" / "A社 見積.docx")
        make_docx(v / "資料" / "見積" / "A社 見積 改訂.docx")
        make_pptx(v / "資料" / "B社 提案.pptx")
        (v / "メモ.md").write_text("# メモ\n[[資料/見積/A社 見積.docx]] と [[B社 提案.pptx]]\n", encoding="utf-8")
        self.app = MycelApp(config_path=root / "cfg.json", vault_override=str(v), initial_load="sync")

    def tearDown(self):
        self.llm.shutdown()
        self.app.close()
        self.tmp.cleanup()

    def test_relations_in_views_and_graph(self):
        a, b = "資料/見積/A社 見積.docx", "資料/見積/A社 見積 改訂.docx"
        self.app.relate(a, b, "改訂版")
        d = self.app.note(b)
        self.assertEqual(d["relations"][0]["path"], a)
        self.assertEqual(d["relations"][0]["label"], "改訂版")
        g = self.app.index.graph()
        self.assertIn([a, b, "rel"], g["edges"])
        self.assertIn(b, {n["id"] for n in g["nodes"]})              # つながりのある資料はグラフに出る
        local = self.app.index.graph(b, 1)
        self.assertIn(a, {n["id"] for n in local["nodes"]})
        self.assertEqual(self.app.folder_view("資料/見積")["items"][0]["relations"], 1)
        self.app.unrelate(b, a)
        self.assertEqual(self.app.note(a)["relations"], [])
        with self.assertRaises(VaultError):
            self.app.relate(a, "資料/無い.pdf")

    def test_suggest_and_propose(self):
        a = "資料/見積/A社 見積.docx"
        sug = self.app.ai.suggest_related(a)
        self.assertIn("資料/見積/A社 見積 改訂.docx", [s["path"] for s in sug])       # 内容がほぼ同じ
        props = self.app.ai.propose_relations("資料")
        pair = next(p for p in props if {p["a"], p["b"]} == {a, "資料/見積/A社 見積 改訂.docx"})
        self.assertGreater(pair["score"], 0.8)
        self.app.relate_many([{"a": pair["a"], "b": pair["b"]}])
        self.assertEqual(self.app.relations.items[0]["origin"], "ai")
        self.assertFalse(any({p["a"], p["b"]} == {pair["a"], pair["b"]} for p in self.app.ai.propose_relations("資料")))
        # LLM があれば候補を選ばせて理由を付ける
        self.app.update_config({"base_url": self.llm_url + "/v1", "model": "mock"})
        sug = self.app.ai.suggest_related("資料/B社 提案.pptx")
        self.assertTrue(sug)
        self.assertEqual(sug[0]["reason"], "同じ顧客の案件")

    def test_move_doc_updates_links_relations_and_index(self):
        a = "資料/見積/A社 見積.docx"
        self.app.relate(a, "資料/B社 提案.pptx")
        cid_before = self.app.index.chunk_count()
        r = self.app.move_item(a, "資料/2026/A社 見積 v1")                 # 拡張子は自動で付く
        new = "資料/2026/A社 見積 v1.docx"
        self.assertEqual(r["path"], new)
        self.assertEqual(r["updated"], ["メモ.md"])
        self.assertTrue((self.app.vault.root / new).is_file())
        self.assertIsNone(self.app.index.get(a))
        self.assertEqual(self.app.index.get(new)["title"], "A社 見積 v1.docx")
        self.assertEqual(self.app.index.chunk_count(), cid_before)          # 読み直していない
        self.assertIn(f"[[{new}]]", self.app.note("メモ")["text"])
        self.assertIn("メモ.md", [b["path"] for b in self.app.index.backlinks(new)])
        self.assertEqual(self.app.relations.for_path("資料/B社 提案.pptx")[0]["path"], new)
        self.assertIn(new, [h["path"] for h in self.app.index.search("初期費用")])
        # 名前だけのリンクは名前が変わったら書き換える
        r = self.app.move_item("資料/B社 提案.pptx", "資料/B社 提案 最終.pptx")
        self.assertIn("[[B社 提案 最終.pptx]]", self.app.note("メモ")["text"])
        with self.assertRaises(VaultError) as cm:                           # 同名のファイルがある
            self.app.move_item("資料/見積/A社 見積 改訂.docx", new)
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(VaultError):
            self.app.move_item("資料/B社 提案 最終.pptx", "../外.pptx")

    def test_folder_create_rename_delete(self):
        self.assertEqual(self.app.create_folder("営業/2026")["path"], "営業/2026")
        self.assertIn("営業/2026", self.app.tree()["folders"])
        with self.assertRaises(VaultError):
            self.app.create_folder("営業/2026")
        with self.assertRaises(VaultError):
            self.app.create_folder(".mycel/x")
        self.app.relate("資料/見積/A社 見積.docx", "資料/B社 提案.pptx")
        self.app.save_scope({"exclude": ["資料/見積/古い"]})
        r = self.app.rename_folder("資料", "営業/資料")
        self.assertEqual(r["moved"], 3)
        self.assertEqual(r["updated"], ["メモ.md"])
        self.assertIn("[[営業/資料/見積/A社 見積.docx]]", self.app.note("メモ")["text"])
        self.assertIsNotNone(self.app.index.get("営業/資料/B社 提案.pptx"))
        self.assertEqual(self.app.scope.data["exclude"], ["営業/資料/見積/古い"])
        self.assertEqual(self.app.relations.items[0]["src"], "営業/資料/見積/A社 見積.docx")
        with self.assertRaises(VaultError):
            self.app.rename_folder("営業", "営業/中")
        r = self.app.delete_folder("営業/資料/見積")
        self.assertEqual(r["removed"], 2)
        self.assertIsNone(self.app.index.get("営業/資料/見積/A社 見積.docx"))
        self.assertEqual(self.app.relations.items, [])
        self.assertTrue((self.app.vault.root / r["trash"]).is_dir())

    def test_delete_and_upload_doc(self):
        r = self.app.delete_item("資料/B社 提案.pptx")
        self.assertTrue((self.app.vault.root / r["trash"]).is_file())
        self.assertIsNone(self.app.index.get("資料/B社 提案.pptx"))
        data = (self.app.vault.root / "資料/見積/A社 見積.docx").read_bytes()
        up = self.app.upload_file("資料/見積", "A社 見積.docx", data)
        self.assertEqual(up["path"], "資料/見積/A社 見積 (2).docx")
        self.assertEqual(up["status"], "ok")
        self.assertIsNotNone(self.app.index.get(up["path"]))
        with self.assertRaises(VaultError):
            self.app.upload_file("資料", "写真.png", b"\x89PNG")
        with self.assertRaises(VaultError):
            self.app.upload_file("@s1", "a.docx", data)

    def test_external_docs_are_read_only_but_relatable(self):
        ext = Path(self.tmp.name) / "ext"
        ext.mkdir()
        make_docx(ext / "共有見積.docx")
        self.app.save_scope({"sources": [{"path": str(ext), "label": "共有"}]})
        self.app.update_index(["@s1"], wait=True)
        with self.assertRaises(VaultError):
            self.app.move_item("@s1/共有見積.docx", "x")
        with self.assertRaises(VaultError):
            self.app.delete_item("@s1/共有見積.docx")
        with self.assertRaises(VaultError):
            self.app.rename_folder("@s1", "x")
        self.app.relate("@s1/共有見積.docx", "資料/見積/A社 見積.docx", "同じ内容")
        self.assertEqual(self.app.note("@s1/共有見積.docx")["relations"][0]["label"], "同じ内容")
        fv = self.app.folder_view("")
        self.assertIn("共有", [f["name"] for f in fv["folders"]])
        self.assertFalse(self.app.folder_view("@s1")["editable"])


class OrganizeApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.docx = make_docx(root / "x.docx").read_bytes()
        cls.app = MycelApp(config_path=root / "cfg.json", vault_override=str(root / "vault"), initial_load="sync")
        cls.httpd = server.make_server(cls.app, 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
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

    def test_flow(self):
        st, r = self.post("/api/folder/create", {"path": "資料/見積"})
        self.assertEqual(st, 200, r)
        st, up = self.call("POST", "/api/file/upload", self.docx, {
            "Content-Type": "application/octet-stream", "X-Filename": quote("見積.docx"), "X-Folder": quote("資料/見積")})
        self.assertEqual(st, 200, up)
        self.assertEqual(up["path"], "資料/見積/見積.docx")
        st, up2 = self.call("POST", "/api/file/upload", self.docx, {
            "Content-Type": "application/octet-stream", "X-Filename": quote("見積 改訂.docx"), "X-Folder": quote("資料/見積")})
        st, fv = self.call("GET", "/api/folder?path=" + quote("資料/見積"))
        self.assertEqual([i["title"] for i in fv["items"]], ["見積 改訂.docx", "見積.docx"])
        st, sg = self.call("GET", "/api/relations/suggest?path=" + quote(up["path"]))
        self.assertIn(up2["path"], [s["path"] for s in sg["suggestions"]])
        st, r = self.post("/api/relations/add", {"a": up["path"], "b": up2["path"], "label": "改訂版"})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["relations"][0]["label"], "改訂版")
        st, r = self.post("/api/item/move", {"path": up["path"], "new_path": "資料/見積 旧"})
        self.assertEqual(r["path"], "資料/見積 旧.docx")
        st, r = self.post("/api/folder/rename", {"path": "資料/見積", "new_path": "資料/見積書"})
        self.assertEqual(st, 200, r)
        st, n = self.call("GET", "/api/note?path=" + quote("資料/見積 旧.docx"))
        self.assertEqual(n["relations"][0]["path"], "資料/見積書/見積 改訂.docx")
        st, r = self.post("/api/relations/propose", {"prefix": "資料"})
        self.assertEqual(st, 200)
        st, r = self.post("/api/item/delete", {"path": "資料/見積 旧.docx"})
        self.assertEqual(st, 200, r)
        st, r = self.post("/api/folder/delete", {"path": "資料/見積書"})
        self.assertEqual(st, 200, r)
        st, r = self.call("POST", "/api/file/upload", self.docx, {"Content-Type": "text/plain", "X-Filename": "a.docx"})
        self.assertEqual(st, 415)


if __name__ == "__main__":
    unittest.main()
