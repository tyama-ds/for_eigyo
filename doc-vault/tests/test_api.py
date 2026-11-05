"""API 統合テスト: モック LLM + 実 HTTP サーバーで、取り込み → 検索 → 相談 → グラフまで通す。

ローカル / クラウドの2プロファイルを別モデル名でモック LLM に向け、
**社外秘文書の本文がクラウド側モデルへ一度も送られていないこと**を送信記録から検証する。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_llm  # noqa: E402
from docvault import config, db  # noqa: E402
from docvault.indexer import INDEXER  # noqa: E402
from docvault.retrieval import INDEX  # noqa: E402

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
INTERNAL_MARKERS = ("温度センサーを 9 月 10 日に交換", "ファーム更新で解決", "是正処置記録")


class TestAPIFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows: 取り込みスレッドの DB 接続
        tmp = Path(cls.tmp.name)
        config.CONFIG_FILE = tmp / "config.json"
        db.set_data_dir(tmp / "data")
        INDEX.loaded_for = None
        cls.mock_server, cls.mock_base = start = mock_llm.start_mock_llm()
        import server as appserver
        cls.appserver = appserver
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), appserver.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        INDEXER.wait_idle(30)
        cls.server.shutdown()
        cls.mock_server.shutdown()
        db.close()
        cls.tmp.cleanup()

    # ------------------------------------------------------------ helpers
    @classmethod
    def api(cls, path, body=None, *, raw: bytes | None = None, status_ok=True):
        url = cls.base + path
        if raw is not None:
            req = urllib.request.Request(url, data=raw, method="POST",
                                         headers={"Content-Type": "application/octet-stream"})
        elif body is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with _opener.open(req, timeout=60) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if status_ok:
                return json.loads(e.read().decode("utf-8"))
            raise

    @classmethod
    def ask(cls, chat_id, question):
        req = urllib.request.Request(cls.base + f"/api/chats/{chat_id}/ask",
                                     data=json.dumps({"question": question}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        events = []
        with _opener.open(req, timeout=60) as res:
            for line in res:
                line = line.decode("utf-8").strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return events

    @classmethod
    def wait_ready(cls, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = cls.api("/api/status")
            if not st["indexer"]["current"] and not st["indexer"]["pending"]:
                return
            time.sleep(0.2)
        raise AssertionError("取り込みが終わりません")

    # ------------------------------------------------------------ tests
    def test_00_health_and_defaults(self):
        self.assertTrue(self.api("/api/health")["ok"])
        cfg = self.api("/api/config")
        self.assertEqual(cfg["policy"]["default_sensitivity"], "internal")
        self.assertNotIn("api_key", cfg["profiles"]["cloud"])     # 秘密は返さない

    def test_01_configure(self):
        cfg = self.api("/api/config", {
            "profiles": {
                "local": {"provider": "openai", "base_url": self.mock_base, "model": "local-chat",
                          "embed_model": "local-embed", "api_key": "secret-local"},
                "cloud": {"provider": "openai", "base_url": self.mock_base, "model": "cloud-chat",
                          "embed_model": "cloud-embed"},
            },
            "policy": {"public_ingest_profile": "cloud", "chat_mode": "auto"},
        })
        self.assertTrue(cfg["profiles"]["local"]["chat_ready"])
        self.assertTrue(cfg["profiles"]["local"]["has_api_key"])
        self.assertNotIn("api_key", cfg["profiles"]["local"])
        test = self.api("/api/config/test", {"profile": "local"})
        self.assertTrue(test["chat"]["ok"], test)
        self.assertTrue(test["embed"]["ok"], test)

    def test_02_ingest_samples(self):
        r = self.api("/api/samples", {})
        self.assertEqual(len(r["docs"]), 4)
        self.wait_ready()
        docs = {d["filename"]: d for d in self.api("/api/docs")["docs"]}
        for name, d in docs.items():
            self.assertEqual(d["status"], "ready", (name, d["error"], d["warnings"]))
            self.assertTrue(d["flags"].get("bm25"))
            self.assertTrue(d["flags"].get("vector"), (name, d["warnings"]))
            self.assertTrue(d["flags"].get("graph"), (name, d["warnings"]))
            self.assertTrue(d["summary"], name)
        # 区分に応じたプロファイルで処理されている
        minutes = docs["03_品質会議_議事録_2026-09.md"]
        self.assertEqual(minutes["sensitivity"], "internal")
        self.assertEqual(minutes["flags"]["graph"], "local")
        self.assertTrue(minutes["flags"]["vector"].startswith("local:"))
        company = docs["01_会社概要.md"]
        self.assertEqual(company["flags"]["graph"], "cloud")
        self.assertTrue(company["flags"]["vector"].startswith("cloud:"))
        # 同じ内容の再アップロードは重複登録しない
        again = self.api("/api/samples", {})
        self.assertFalse(any(x["created"] for x in again["docs"]))
        # フォルダ集計
        folders = {f["path"]: f["count"] for f in self.api("/api/docs")["folders"]}
        self.assertEqual(folders["社内"], 2)
        self.assertEqual(folders[""], 4)

    def test_03_doc_view_and_file(self):
        docs = self.api("/api/docs")["docs"]
        xlsx = next(d for d in docs if d["ext"] == ".xlsx")
        view = self.api(f"/api/docs/{xlsx['id']}/chunks")
        self.assertIn("ネジ緩み", view["chunks"][0]["text"])
        self.assertTrue(view["chunks"][0]["locator"].startswith("不具合集計!行"))
        with _opener.open(self.base + f"/api/docs/{xlsx['id']}/file?download=1", timeout=10) as res:
            self.assertIn("attachment", res.headers["Content-Disposition"])
            self.assertTrue(res.read().startswith(b"PK"))

    def test_04_search(self):
        r = self.api("/api/search", {"q": "熱処理炉 温度"})
        titles = [g["title"] for g in r["results"]]
        self.assertTrue(titles)
        self.assertTrue(any("議事録" in t or "品質会議" in t for t in titles), titles)
        self.assertTrue(r["results"][0]["hits"][0]["snippet"])

    def test_05_chat_auto_internal_goes_local(self):
        chat = self.api("/api/chats", {})["chat"]
        events = self.ask(chat["id"], "熱処理炉の件で北浜電機への対応はどう決まった？")
        types = [e["type"] for e in events]
        self.assertEqual(types[-1], "done", events[-3:])
        done = events[-1]["message"]
        self.assertIn("[S1]", done["content"])
        self.assertNotIn("<think>", done["content"])
        self.assertTrue(done["meta"]["route"]["retrieval"])
        self.assertEqual(done["meta"]["sensitivity"], "internal")
        self.assertEqual(done["meta"]["profile"], "local")         # 社外秘を含む → ローカル
        self.assertTrue(any(s["used"] for s in done["meta"]["sources"]))
        self.assertTrue(self.api(f"/api/chats/{chat['id']}")["chat"]["touched_internal"])
        deltas = "".join(e["text"] for e in events if e["type"] == "delta")
        self.assertIn("SkyEdge", deltas)

    def test_06_chat_cloud_mode_excludes_internal(self):
        chat = self.api("/api/chats", {"mode": "cloud"})["chat"]
        events = self.ask(chat["id"], "SkyEdge S-2 の給電方式は？")
        done = events[-1]["message"]
        self.assertEqual(done["meta"]["profile"], "cloud")
        self.assertTrue(done["meta"]["sources"])
        for s in done["meta"]["sources"]:
            self.assertEqual(s["sensitivity"], "public")

    def test_06b_cloud_mode_after_internal_drops_history(self):
        chat = self.api("/api/chats", {"mode": "local"})["chat"]
        self.ask(chat["id"], "熱処理炉の温度センサーはいつ交換した？")
        self.assertTrue(self.api(f"/api/chats/{chat['id']}")["chat"]["touched_internal"])
        self.api(f"/api/chats/{chat['id']}/update", {"mode": "cloud"})
        done = self.ask(chat["id"], "SkyEdge S-2 の保護等級は？")[-1]["message"]
        self.assertEqual(done["meta"]["profile"], "cloud")
        self.assertTrue(any("会話履歴を送りません" in w for w in done["meta"]["warnings"]))

    def test_07_chat_summary_and_chitchat(self):
        chat = self.api("/api/chats", {"mode": "local"})["chat"]
        events = self.ask(chat["id"], "資料全体を要約して")
        done = events[-1]["message"]
        self.assertEqual(done["meta"]["route"]["type"], "summary")
        kinds = {s["kind"] for s in done["meta"]["sources"]}
        self.assertIn("doc", kinds)
        events = self.ask(chat["id"], "ありがとう")
        done = events[-1]["message"]
        self.assertFalse(done["meta"]["route"]["retrieval"])
        self.assertEqual(done["meta"]["sources"], [])
        msgs = self.api(f"/api/chats/{chat['id']}")["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant"])

    def test_08_relation_uses_graph(self):
        chat = self.api("/api/chats", {"mode": "local"})["chat"]
        events = self.ask(chat["id"], "北浜電機と仙台工場の関係は？")
        done = events[-1]["message"]
        self.assertEqual(done["meta"]["route"]["type"], "relation")
        self.assertIn("グラフ PPR", done["meta"]["via"])

    def test_09_scope_limits_documents(self):
        docs = self.api("/api/docs")["docs"]
        spec = next(d for d in docs if d["ext"] == ".docx")
        chat = self.api("/api/chats", {"mode": "local", "scope": {"doc_ids": [spec["id"]]}})["chat"]
        events = self.ask(chat["id"], "保証期間は？")
        done = events[-1]["message"]
        self.assertTrue(done["meta"]["sources"])
        self.assertTrue(all(s["doc_id"] == spec["id"] for s in done["meta"]["sources"]))

    def test_10_graph_api(self):
        g = self.api("/api/graph")
        self.assertNotIn("error", g)
        names = {n["name"] for n in g["nodes"]}
        self.assertIn("北浜電機", names)
        self.assertTrue(g["edges"])
        self.assertTrue(any(n["docs"] for n in g["nodes"]))
        one = next(d for d in self.api("/api/docs")["docs"] if d["ext"] == ".xlsx")
        g1 = self.api(f"/api/graph?doc={one['id']}")
        self.assertLessEqual(len(g1["nodes"]), len(g["nodes"]))

    def test_11_no_internal_text_reached_cloud(self):
        """送信記録全体で、クラウド側モデルへ社外秘の本文が送られていないこと。"""
        leaked = []
        cloud_calls = 0
        for req in mock_llm.REQUESTS:
            body = req["body"]
            if body.get("model") not in ("cloud-chat", "cloud-embed"):
                continue
            cloud_calls += 1
            text = json.dumps(body, ensure_ascii=False)
            for marker in INTERNAL_MARKERS:
                if marker in text:
                    leaked.append((body.get("model"), marker))
        self.assertEqual(leaked, [])
        self.assertGreater(cloud_calls, 0)        # 公開文書・クラウドモードではクラウドを使っている

    def test_12_update_delete_reindex(self):
        docs = self.api("/api/docs")["docs"]
        md = next(d for d in docs if d["filename"].startswith("01_"))
        r = self.api(f"/api/docs/{md['id']}/update",
                     {"title": "会社概要（改）", "folder": "/公開資料/会社/", "tags": ["会社", "概要"],
                      "sensitivity": "internal"})
        self.assertEqual(r["doc"]["title"], "会社概要（改）")
        self.assertEqual(r["doc"]["folder"], "公開資料/会社")
        self.wait_ready()
        d = self.api(f"/api/docs/{md['id']}")["doc"]
        self.assertTrue(d["flags"]["vector"].startswith("local:"))   # 区分変更で埋め込みを作り直す
        self.assertTrue(self.api(f"/api/docs/{md['id']}/reindex", {"stages": ["summary"]})["ok"])
        self.wait_ready()
        self.assertTrue(self.api(f"/api/docs/{md['id']}/delete", {})["ok"])
        self.assertIn("error", self.api(f"/api/docs/{md['id']}"))
        r = self.api("/api/search", {"q": "代表取締役社長"})
        self.assertFalse(any(g["doc_id"] == md["id"] for g in r["results"]))

    def test_13_upload_validation(self):
        r = self.api("/api/docs/upload?name=x.exe", raw=b"MZ")
        self.assertIn("error", r)
        r = self.api("/api/docs/upload?name=note.txt&folder=a/b&sensitivity=public&tags=t1,t2",
                     raw="テスト用のメモ。新しい試作品 X-9 の評価は良好。".encode("utf-8"))
        self.assertTrue(r["created"])
        self.assertEqual(r["doc"]["sensitivity"], "public")
        self.assertEqual(r["doc"]["tags"], ["t1", "t2"])
        self.wait_ready()
        broken = self.api("/api/docs/upload?name=broken.docx", raw=b"not a zip")
        self.wait_ready()
        d = self.api(f"/api/docs/{broken['doc']['id']}")["doc"]
        self.assertEqual(d["status"], "error")
        self.assertIn("壊れて", d["error"])


if __name__ == "__main__":
    unittest.main()
