"""文献管理モードのテスト（登録・取り込み・検索・分解グラフ・AI 補完）。モック LLM を使う。"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote, urlencode

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))

import server  # noqa: E402
from mock_llm import start_mock_llm  # noqa: E402
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.library import citation, parse_bibtex, parse_ris, to_bibtex, to_ris  # noqa: E402
from mycelcore.vault import VaultError  # noqa: E402
from samples import make_docx  # noqa: E402

BIB = r"""
@article{yamada2024inventory,
  title = {在庫最適化のための{需要予測}手法},
  author = {山田 太郎 and Smith, John},
  journal = {日本経営工学会論文誌}, year = {2024}, volume = {75}, number = {2}, pages = {100--110},
  doi = {10.1234/jima.2024.001}, keywords = {在庫最適化, 需要予測}
}
@inproceedings{Lee2023,
  title = "Demand Forecasting with Transformers",
  author = "Lee, Ann and Kim, Bo",
  booktitle = "Proc. ICML", year = 2023, url = {https://example.org/lee2023}
}
@comment{ignored}
"""
RIS = """TY  - JOUR
AU  - 佐藤 花子
TI  - 物流倉庫の自動化と安全在庫
PY  - 2022/01/01
JO  - 物流学会誌
VL  - 10
SP  - 1
EP  - 12
DO  - 10.5555/logi.2022.01
KW  - 自動化
KW  - 安全在庫
ER  -
"""


class ParseTest(unittest.TestCase):
    def test_bibtex_ris_roundtrip(self):
        refs = parse_bibtex(BIB)
        self.assertEqual(len(refs), 2)
        a, b = refs
        self.assertEqual(a["title"], "在庫最適化のための需要予測手法")
        self.assertEqual(a["authors"], ["山田 太郎", "Smith, John"])
        self.assertEqual((a["year"], a["venue"], a["pages"], a["doi"]), ("2024", "日本経営工学会論文誌", "100–110", "10.1234/jima.2024.001"))
        self.assertEqual(a["keywords"], ["在庫最適化", "需要予測"])
        self.assertEqual((b["type"], b["year"], b["venue"], b["url"]), ("inproceedings", "2023", "Proc. ICML", "https://example.org/lee2023"))
        r = parse_ris(RIS)[0]
        self.assertEqual((r["type"], r["authors"], r["year"], r["pages"], r["keywords"]), ("article", ["佐藤 花子"], "2022", "1-12", ["自動化", "安全在庫"]))
        full = {**a, "key": "yamada2024", "tags": [], "status": "unread", "rating": 0, "file": "", "note": "", "summary": {}, "lang": "", "publisher": "", "id": "x"}
        again = parse_bibtex(to_bibtex(full))[0]
        self.assertEqual((again["title"], again["authors"], again["doi"]), (a["title"], a["authors"], a["doi"]))
        again_ris = parse_ris(to_ris({**r, "key": "k", "tags": [], "status": "unread", "rating": 0, "file": "", "note": "", "summary": {}, "lang": "", "id": "y"}))[0]
        self.assertEqual((again_ris["title"], again_ris["pages"]), (r["title"], "1-12"))
        self.assertIn("山田 太郎、Smith, John (2024). 在庫最適化のための需要予測手法. 日本経営工学会論文誌, 75(2), 100–110. https://doi.org/10.1234/jima.2024.001", citation(full))
        self.assertIn("vol. 75", citation(full, "ieee"))


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.llm, self.llm_url, self.reqs = start_mock_llm()
        v = root / "vault"
        (v / "論文").mkdir(parents=True)
        (v / "論文" / "需要予測.txt").write_text(
            "在庫最適化のための需要予測手法\n山田 太郎\n2024 日本経営工学会論文誌\nDOI: 10.1234/jima.2024.001\n\n"
            "# 1. はじめに\n欠品と過剰在庫を減らすため、需要予測に基づく安全在庫の最適化を提案する。\n\n"
            "# 2. 手法\n時系列モデルで需要を予測し、サービス率から安全在庫を決める。\n\n"
            "# 3. 結果\n3 拠点で在庫を 20% 削減した。季節性の強い品目では精度が落ちる。\n", encoding="utf-8")
        (v / "論文" / "倉庫自動化.txt").write_text(
            "物流倉庫の自動化と安全在庫\n佐藤 花子 2022\n\n# 概要\n自動倉庫の導入で安全在庫を見直した事例。\n\n"
            "# 結果\n入出庫の時間を半減し、安全在庫を 15% 削減した。\n", encoding="utf-8")
        make_docx(v / "論文" / "見積.docx")
        self.app = MycelApp(config_path=root / "cfg.json", vault_override=str(v), initial_load="sync", seed_sample=False)
        self.lib = self.app.library

    def tearDown(self):
        self.llm.shutdown()
        self.app.close()
        self.tmp.cleanup()

    def test_register_import_dup_and_persist(self):
        r = self.lib.register_files(["論文/需要予測.txt", "論文/倉庫自動化.txt"])
        self.assertEqual(len(r["added"]), 2)
        a = r["added"][0]
        self.assertEqual((a["title"], a["doi"], a["year"], a["file"]), ("在庫最適化のための需要予測手法", "10.1234/jima.2024.001", "2024", "論文/需要予測.txt"))
        self.assertEqual(self.lib.register_files(["論文/需要予測.txt"])["duplicates"], ["需要予測.txt"])
        imp = self.lib.import_text(BIB, tags=["読む"])
        self.assertEqual(len(imp["added"]), 1)                              # 山田論文は DOI が同じなので重複
        self.assertEqual(imp["duplicates"], ["在庫最適化のための需要予測手法"])
        lee = imp["added"][0]
        self.assertEqual(lee["key"], "Lee2023")                                # BibTeX のキーはそのまま使う
        self.assertEqual(lee["tags"], ["読む"])
        imp2 = self.lib.import_text(RIS)
        self.assertEqual(imp2["duplicates"], ["物流倉庫の自動化と安全在庫"])    # 題名が同じ
        self.assertEqual(len(self.lib.refs), 3)
        with self.assertRaises(VaultError):
            self.lib.add({"title": "在庫最適化のための需要予測手法", "year": "2024"})
        # 保存されている
        from mycelcore.library import Library
        again = Library(self.app)
        self.assertEqual({x["key"] for x in again.refs}, {x["key"] for x in self.lib.refs})
        lst = self.lib.list(tag="読む")
        self.assertEqual([x["key"] for x in lst["refs"]], ["Lee2023"])
        self.assertEqual(dict(lst["facets"]["years"])["2024"], 1)
        self.assertIn("@inproceedings{Lee2023", self.lib.export([lee["id"]], "bibtex"))
        self.assertIn("Lee2023,inproceedings", self.lib.export(None, "csv"))

    def test_update_remove_and_rename_follow(self):
        r = self.lib.register_files(["論文/需要予測.txt"])["added"][0]
        u = self.lib.update(r["id"], {"status": "read", "rating": 4, "tags": ["在庫", "重要"], "authors": "山田 太郎; 鈴木 一郎"})
        self.assertEqual((u["status"], u["rating"], u["tags"], u["authors"]), ("read", 4, ["在庫", "重要"], ["山田 太郎", "鈴木 一郎"]))
        self.app.move_item("論文/需要予測.txt", "論文/2024/需要予測.txt")
        self.assertEqual(self.lib.get(r["id"])["file"], "論文/2024/需要予測.txt")
        self.assertTrue(self.lib.list()["refs"][0]["file_ok"])
        self.assertEqual(self.lib.remove([r["id"]]), 1)
        self.assertEqual(self.lib.refs, [])

    def test_search_related_structure(self):
        self.lib.register_files(["論文/需要予測.txt", "論文/倉庫自動化.txt"])
        self.lib.add({"title": "関係ない書籍", "authors": ["誰か"], "year": "2000"})
        res = self.lib.search("安全在庫")
        self.assertEqual(res["mode"], "keyword")
        self.assertEqual({x["title"] for x in res["results"]}, {"在庫最適化のための需要予測手法", "物流倉庫の自動化と安全在庫"})
        self.assertTrue(all(any("安全在庫" in p["text"] for p in x["passages"]) for x in res["results"]))
        self.assertEqual(self.lib.search("安全在庫", year="2022")["results"][0]["year"], "2022")
        self.assertEqual(self.lib.search("関係ない")["results"][0]["title"], "関係ない書籍")        # 書誌情報だけの文献も出る
        a = next(r for r in self.lib.refs if r["year"] == "2024")
        rel = self.lib.related(a["id"])
        self.assertEqual(rel[0]["title"], "物流倉庫の自動化と安全在庫")
        self.assertIn("内容が近い", rel[0]["reasons"])
        secs = self.lib.structure(a["id"])
        self.assertEqual([s["heading"] for s in secs][:4], ["在庫最適化のための需要予測手法", "1. はじめに", "2. 手法", "3. 結果"])
        st = self.lib.embed_status()
        self.assertEqual(st["files"], 2)
        self.assertFalse(st["embed"])
        ask = self.lib.ask("在庫はどれだけ減った？")
        self.assertFalse(ask["llm"])
        self.assertTrue(ask["sources"] and all("ref_id" in s for s in ask["sources"]))

    def test_links_manual_auto_and_ai(self):
        self.lib.register_files(["論文/需要予測.txt", "論文/倉庫自動化.txt"])
        a = next(r for r in self.lib.refs if r["year"] == "2024")
        b = next(r for r in self.lib.refs if r["year"] == "2022")
        l = self.lib.link(a["id"], b["id"], "extends", "安全在庫の考え方を発展")
        self.assertEqual(l["origin"], "user")
        la, lb = self.lib.links_of(a["id"]), self.lib.links_of(b["id"])
        self.assertEqual((la[0]["label"], la[0]["outgoing"]), ("発展させている", True))
        self.assertEqual((lb[0]["label"], lb[0]["outgoing"]), ("元になった", False))
        self.lib.link(b["id"], a["id"], "extends")                                   # 向きが違えば別のつながり
        self.assertEqual(len(self.lib.links), 2)
        self.lib.link(b["id"], a["id"], "compares")
        self.lib.link(a["id"], b["id"], "compares")                                  # 向きの無い種類は同じもの
        self.assertEqual(len(self.lib.links), 3)
        self.assertIn("つながり: 発展させている", self.lib.related(a["id"])[0]["reasons"])
        g = self.lib.graph("none", [])
        link_edges = [e for e in g["edges"] if e[2] == "link"]
        self.assertEqual(len(link_edges), 3)
        self.assertIn(["r:" + a["id"], "r:" + b["id"], "link", "発展させている", True], link_edges)
        self.assertFalse(any(e[2] == "sim" for e in g["edges"]))                    # 明示的につないだ組は点線を出さない
        from mycelcore.library import Library
        self.assertEqual(len(Library(self.app).links), 3)                            # 保存されている
        self.assertEqual(self.lib.unlink(a["id"], b["id"], "compares"), 1)
        self.assertEqual(self.lib.unlink(a["id"], b["id"]), 2)
        # 引用の自動検出: 本文に他の文献の DOI・題名があれば「引用している」
        (self.app.vault.root / "論文" / "倉庫自動化.txt").write_text(
            "物流倉庫の自動化と安全在庫\n\n# 本文\n...\n\n# 参考文献\n[1] 山田, 在庫最適化のための需要予測手法, 2024. doi:10.1234/jima.2024.001\n", encoding="utf-8")
        self.app.update_index(None, wait=True)
        d = self.lib.detect_citations()
        self.assertEqual(d["added"], 1)
        self.assertEqual(self.lib.links_of(b["id"])[0]["label"], "引用している")
        self.assertEqual(self.lib.links[0]["origin"], "auto")
        self.assertEqual(self.lib.detect_citations()["added"], 0)
        # AI の提案（LLM なし → 内容の近さから / あり → 種類と理由）
        self.lib.unlink(a["id"], b["id"])
        sug = self.lib.suggest_links(a["id"])
        self.assertEqual(sug[0]["id"], b["id"])
        self.assertFalse(sug[0]["ai"])
        self.app.update_config({"base_url": self.llm_url + "/v1", "model": "mock"})
        sug = self.lib.suggest_links(a["id"])
        self.assertEqual((sug[0]["type"], sug[0]["ai"]), ("extends", True))
        self.assertTrue(sug[0]["reason"])
        self.lib.remove([b["id"]])
        self.assertEqual(self.lib.links, [])

    def test_graph_expands_from_large_to_small(self):
        self.lib.register_files(["論文/需要予測.txt", "論文/倉庫自動化.txt"], tags=["在庫"])
        a = next(r for r in self.lib.refs if r["year"] == "2024")
        b = next(r for r in self.lib.refs if r["year"] == "2022")
        g = self.lib.graph("tag", [])
        self.assertEqual([n["id"] for n in g["nodes"]], ["g:在庫"])                   # 最初はトピックだけ
        self.assertTrue(g["nodes"][0]["expandable"])
        g = self.lib.graph("tag", ["g:在庫"])
        ids = {n["id"] for n in g["nodes"]}
        self.assertEqual(ids, {"g:在庫", f"r:{a['id']}", f"r:{b['id']}"})
        self.assertIn(["g:在庫", f"r:{a['id']}", "tree"], g["edges"])
        self.assertTrue(any(e[2] == "sim" and {e[0], e[1]} == {f"r:{a['id']}", f"r:{b['id']}"} for e in g["edges"]))   # 文献同士の近さ
        g = self.lib.graph("tag", ["g:在庫", f"r:{a['id']}"])
        secs = [n for n in g["nodes"] if n["kind"] == "section"]
        self.assertEqual([s["title"] for s in secs][:2], ["在庫最適化のための需要予測手法", "1. はじめに"])
        self.assertTrue(any(e[2] == "sim" and e[0].startswith("s:") and e[1] == f"r:{b['id']}" for e in g["edges"]))  # 章 → 別の文献
        sid = secs[-1]["id"]
        g = self.lib.graph("tag", ["g:在庫", f"r:{a['id']}", sid])
        passages = [n for n in g["nodes"] if n["kind"] == "passage"]
        self.assertTrue(passages)
        self.assertTrue(all(e[2] == "tree" for e in g["edges"] if e[0] == sid and e[1].startswith("c:")))
        # 著者で分類
        self.lib.update(a["id"], {"authors": ["山田 太郎"]})
        self.lib.update(b["id"], {"authors": ["佐藤 花子"]})
        g = self.lib.graph("author", [])
        self.assertEqual({n["title"] for n in g["nodes"]}, {"山田 太郎", "佐藤 花子"})
        g = self.lib.graph("none", [])
        self.assertEqual({n["kind"] for n in g["nodes"]}, {"ref"})

    def test_ai_metadata_summary_and_note(self):
        self.app.update_config({"base_url": self.llm_url + "/v1", "model": "mock"})
        r = self.lib.register_files(["論文/見積.docx"], use_ai=True)["added"][0]
        self.assertEqual(r["title"], "在庫最適化のための需要予測手法")                   # AI がファイル名由来の題名を置き換える
        self.assertEqual((r["authors"], r["venue"], r["key"]), (["山田 太郎", "Smith, John"], "日本経営工学会論文誌", "山田2024"))
        s = self.lib.ai_summary(r["id"])
        self.assertEqual(s["summary"]["one_line"], "需要予測で在庫を 20% 削減した。")
        self.assertTrue(s["has_summary"])
        n = self.lib.create_note(r["id"])
        self.assertTrue(n["created"])
        note = self.app.note(n["path"])
        self.assertEqual(note["props"]["種別"], "文献")
        self.assertIn("## 目的・課題\n欠品と過剰在庫の削減", note["text"])
        self.assertIn("論文/見積.docx", [o["path"] for o in note["outgoing"]])
        self.assertEqual(self.lib.get(r["id"])["note"], n["path"])
        self.assertFalse(self.lib.create_note(r["id"])["created"])
        res = self.lib.ask("在庫はどれだけ減った？")
        self.assertTrue(res["llm"])


class LibraryApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        v = root / "vault"
        (v / "論文").mkdir(parents=True)
        (v / "論文" / "a.txt").write_text("論文A 安全在庫の最適化\n\n# 結果\n在庫 20% 減。\n", encoding="utf-8")
        cls.app = MycelApp(config_path=root / "cfg.json", vault_override=str(v), initial_load="sync", seed_sample=False)
        cls.httpd = server.make_server(cls.app, 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.close()
        cls.tmp.cleanup()

    def call(self, method, path, obj=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=json.dumps(obj).encode() if obj is not None else None,
                     headers={"Content-Type": "application/json"} if obj is not None else {})
        res = conn.getresponse()
        body = json.loads(res.read() or b"{}")
        conn.close()
        return res.status, body

    def test_flow(self):
        st, r = self.call("POST", "/api/library/register", {"paths": ["論文/a.txt"], "tags": ["在庫"]})
        self.assertEqual(st, 200, r)
        rid = r["added"][0]["id"]
        st, r = self.call("POST", "/api/library/import", {"text": RIS})
        self.assertEqual(len(r["added"]), 1)
        st, lst = self.call("GET", "/api/library?sort=year")
        self.assertEqual(lst["count"], 2)
        self.assertIn("在庫", dict(lst["facets"]["tags"]))
        self.assertFalse(lst["embed"]["embed"])
        st, d = self.call("GET", "/api/library/ref?id=" + rid)
        self.assertIn("@misc{", d["bibtex"]) if d["type"] == "misc" else self.assertIn("@", d["bibtex"])
        self.assertTrue(d["structure"])
        st, sr = self.call("GET", "/api/library/search?" + urlencode({"q": "安全在庫"}))
        self.assertEqual(sr["results"][0]["id"], rid)
        st, u = self.call("POST", "/api/library/update", {"id": rid, "ref": {"status": "reading", "rating": 3}})
        self.assertEqual((u["status"], u["rating"]), ("reading", 3))
        st, g = self.call("GET", "/api/library/graph?" + urlencode({"group": "tag", "expanded": "g:在庫"}))
        self.assertIn(f"r:{rid}", [n["id"] for n in g["nodes"]])
        st, ex = self.call("GET", "/api/library/export?format=ris")
        self.assertIn("TY  - ", ex["text"])
        st, bp = self.call("GET", "/api/library/by_path?path=" + quote("論文/a.txt"))
        self.assertEqual(bp["ref"]["id"], rid)
        other = r["added"][0]["id"]
        st, lk = self.call("POST", "/api/library/link", {"a": rid, "b": other, "type": "compares", "note": "比較"})
        self.assertEqual(st, 200, lk)
        self.assertEqual(lk["links"][0]["label"], "比較対象")
        st, d2 = self.call("GET", "/api/library/ref?id=" + rid)
        self.assertEqual(len(d2["links"]), 1)
        self.assertIn("cites", d2["link_types"])
        st, sg = self.call("POST", "/api/library/links/suggest", {"id": rid})
        self.assertEqual(st, 200)
        st, dt = self.call("POST", "/api/library/links/detect", {})
        self.assertEqual(st, 200)
        st, ul = self.call("POST", "/api/library/unlink", {"a": rid, "b": other})
        self.assertEqual(ul["removed"], 1)
        st, a = self.call("POST", "/api/library/ask", {"question": "在庫は？"})
        self.assertEqual(st, 200)
        self.assertFalse(a["llm"])
        st, e = self.call("POST", "/api/library/embed", {})
        self.assertEqual(st, 200, e)
        for _ in range(100):
            st, ix = self.call("GET", "/api/index/status")
            if ix["job"]["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(ix["job"]["state"], "done")
        st, e = self.call("POST", "/api/library/ai_batch", {"ids": [rid], "what": "summary"})
        self.assertEqual(st, 502, e)                                                    # LLM 未設定
        st, rm = self.call("POST", "/api/library/remove", {"ids": [rid]})
        self.assertEqual(rm["removed"], 1)


if __name__ == "__main__":
    unittest.main()
