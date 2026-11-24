"""2026-11 の追加機能のテスト（モック LLM + 実 HTTP サーバー）。

  - 読み込み結果: LLM ごとに要約・グラフを残し、使うものを選べる（別の LLM で読み込んでも前の結果は残る）
  - フォルダ: 作成・名前の変更・移動・削除（文書は消えない）・文書の移動・相談先の書き換え
  - 相談先: フォルダの複数指定と個別の文書（和集合）・回答に相談先を記録
  - 採用根拠: 出典ごとに、どの検索で何位・スコアいくつだったか（マウスオーバーで表示）
  - グラフの作り方: 高速（説明なし）・語の共起（LLM なし）
  - 文字起こし: Whisper 互換 API への中継（ローカル既定・クラウドは許可制）
  - 旧データの移行: 読み込み結果の導入前の要約・グラフ・埋め込みを「以前の取り込み」として残す
  - エンティティ辞書: 別名をまとめる・種別・外す語・本文から探す・関係の追加と削除・グラフ画面の手直し
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DOCVAULT_QUIET", "1")

import mock_llm  # noqa: E402
from docvault import config, db, entitydict, folders, graph, readings, speech, terms  # noqa: E402
from docvault.indexer import INDEXER  # noqa: E402
from docvault.retrieval import INDEX  # noqa: E402

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakeWhisper(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeWhisper.received.append({"path": self.path, "ctype": self.headers.get("Content-Type", ""),
                                     "auth": self.headers.get("Authorization") or self.headers.get("api-key"),
                                     "body": body})
        data = json.dumps({"text": " 品質会議の決定事項は？ "}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def form_fields(body: bytes, ctype: str) -> dict[str, bytes]:
    boundary = ctype.split("boundary=", 1)[1].encode()
    out = {}
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, value = part.split(b"\r\n\r\n", 1)
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        out[name] = value.rsplit(b"\r\n", 1)[0]
    return out


class TestFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(cls.tmp.name)
        config.CONFIG_FILE = tmp / "config.json"
        db.set_data_dir(tmp / "data")
        INDEX.loaded_for = None
        cls.mock_server, cls.mock_base = mock_llm.start_mock_llm()
        import server as appserver
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), appserver.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.whisper = ThreadingHTTPServer(("127.0.0.1", 0), FakeWhisper)
        threading.Thread(target=cls.whisper.serve_forever, daemon=True).start()
        cls.whisper_url = f"http://127.0.0.1:{cls.whisper.server_address[1]}"
        cls.api("/api/config", {"profiles": {
            "local": {"provider": "openai", "base_url": cls.mock_base, "model": "local-chat",
                      "embed_model": "local-embed"},
            "cloud": {"provider": "openai", "base_url": cls.mock_base, "model": "cloud-chat"}}})
        cls.api("/api/samples", {})
        assert INDEXER.wait_idle(60)
        docs = cls.api("/api/docs")["docs"]
        cls.internal = next(d for d in docs if d["sensitivity"] == "internal" and "議事録" in d["title"])
        cls.public = next(d for d in docs if d["sensitivity"] == "public")

    @classmethod
    def tearDownClass(cls):
        INDEXER.wait_idle(30)
        cls.server.shutdown()
        cls.whisper.shutdown()
        cls.mock_server.shutdown()
        db.close()
        cls.tmp.cleanup()

    @classmethod
    def api(cls, path, body=None, *, raw=None, ctype="application/octet-stream"):
        url = cls.base + path
        if raw is not None:
            req = urllib.request.Request(url, data=raw, method="POST", headers={"Content-Type": ctype})
        elif body is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with _opener.open(req, timeout=60) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8"))

    def ask(self, chat_id, question):
        req = urllib.request.Request(self.base + f"/api/chats/{chat_id}/ask",
                                     data=json.dumps({"question": question}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with _opener.open(req, timeout=60) as res:
            for line in res:
                if line.startswith(b"data: "):
                    ev = json.loads(line[6:])
                    if ev["type"] == "done":
                        return ev["message"]
        self.fail("回答が返りませんでした")

    def readings_of(self, doc_id, stage):
        return [r for r in self.api(f"/api/docs/{doc_id}/readings")["readings"] if r["stage"] == stage]

    # ------------------------------------------------------------ 読み込み結果
    def test_01_readings_keep_each_llm(self):
        d = self.internal
        g0 = self.readings_of(d["id"], "graph")
        self.assertEqual(len(g0), 1)
        self.assertTrue(g0[0]["label"].startswith("local-chat（ローカル"), g0[0]["label"])
        self.assertTrue(g0[0]["active"])
        doc = self.api(f"/api/docs/{d['id']}")["doc"]
        self.assertEqual(doc["flags"]["graph"], "local")
        self.assertIn("local-chat", doc["flags"]["graph_label"])
        # 別のモデル・高速モードで読み込む → 結果が増え、新しい方に切り替わる
        r = self.api(f"/api/docs/{d['id']}/read", {"profile": "local", "model": "other-model",
                                                   "graph_mode": "fast", "stages": ["summary", "graph"]})
        self.assertEqual(r, {"ok": True})
        INDEXER.wait_idle(60)
        gs = self.readings_of(d["id"], "graph")
        ss = self.readings_of(d["id"], "summary")
        self.assertEqual(len(gs), 2)
        self.assertEqual(len(ss), 2)
        new = next(x for x in gs if "other-model" in x["label"])
        old = next(x for x in gs if "local-chat" in x["label"])
        self.assertTrue(new["active"])
        self.assertEqual(new["mode"], "fast")
        self.assertGreater(new["entities"], 0)
        prompts = [m["content"] for r in mock_llm.REQUESTS if r["body"].get("model") == "other-model"
                   for m in r["body"].get("messages", []) if m.get("role") == "user"]
        self.assertTrue(any("graph_extract_fast" in p for p in prompts))   # 高速モードのプロンプト
        self.assertTrue(all('"description"' not in p for p in prompts if "graph_extract_fast" in p))
        # 前の結果に戻す
        self.assertEqual(self.api(f"/api/docs/{d['id']}/readings/{old['id']}/activate", {}), {"ok": True})
        gs = self.readings_of(d["id"], "graph")
        self.assertTrue(next(x for x in gs if x["id"] == old["id"])["active"])
        self.assertFalse(next(x for x in gs if x["id"] == new["id"])["active"])
        # グラフは使用中の結果だけで作る
        mentions = {r["reading_id"] for r in db.query(
            "SELECT DISTINCT reading_id FROM entity_mentions WHERE doc_id=?", (d["id"],))}
        self.assertEqual(mentions, {old["id"], new["id"]})
        kg = graph.KnowledgeGraph({d["id"]}, {d["id"]: "internal"})
        rows = db.one("SELECT COUNT(*) n FROM relation_mentions WHERE reading_id=? AND source<>target",
                      (old["id"],))["n"]
        self.assertGreater(rows, 0)
        self.assertEqual(sum(r["count"] for r in kg.relations.values()), rows)   # 使用中の結果の言及だけ
        # 使用中を消すと、残りの結果に切り替わる
        self.api(f"/api/docs/{d['id']}/readings/{old['id']}/delete", {})
        gs = self.readings_of(d["id"], "graph")
        self.assertEqual([x["id"] for x in gs], [new["id"]])
        self.assertTrue(gs[0]["active"])

    def test_02_internal_doc_cannot_be_read_by_cloud(self):
        r = self.api(f"/api/docs/{self.internal['id']}/read", {"profile": "cloud", "stages": ["summary"]})
        self.assertIn("社外秘", r["error"])
        r = self.api(f"/api/docs/{self.public['id']}/read", {"profile": "cloud", "stages": ["summary"]})
        self.assertEqual(r, {"ok": True})
        INDEXER.wait_idle(30)
        labels = [x["label"] for x in self.readings_of(self.public["id"], "summary")]
        self.assertTrue(any(x.startswith("cloud-chat（クラウド") for x in labels), labels)

    def test_03_terms_mode_graph_without_llm(self):
        before = len(mock_llm.REQUESTS)
        self.api(f"/api/docs/{self.public['id']}/read", {"graph_mode": "terms", "stages": ["graph"]})
        INDEXER.wait_idle(30)
        self.assertEqual(len(mock_llm.REQUESTS), before)            # LLM を1回も呼ばない
        g = next(x for x in self.readings_of(self.public["id"], "graph") if x["mode"] == "terms")
        self.assertEqual(g["label"], "語の共起（LLM なし）")
        self.assertGreater(g["entities"], 3)

    def test_04_same_chunks_keep_readings_on_reextract(self):
        d = self.public
        n = len(db.query("SELECT id FROM readings WHERE doc_id=?", (d["id"],)))
        self.api(f"/api/docs/{d['id']}/reindex", {"stages": ["extract"]})
        INDEXER.wait_idle(30)
        self.assertEqual(len(db.query("SELECT id FROM readings WHERE doc_id=?", (d["id"],))), n)

    # ------------------------------------------------------------ フォルダ
    def test_10_folders(self):
        self.assertTrue(self.api("/api/folders", {"path": "検証/空のフォルダ"})["ok"])
        tree = {f["path"]: f for f in self.api("/api/folders")["folders"]}
        self.assertIn("検証", tree)
        self.assertEqual(tree["検証/空のフォルダ"]["count"], 0)
        self.assertEqual(tree[""]["count"], len(self.api("/api/docs")["docs"]))
        src = self.internal["folder"]
        top = src.split("/")[0]
        chat = self.api("/api/chats", {"scope": {"folders": [src]}})["chat"]
        # 名前の変更: 配下の文書・サブフォルダ・相談先も書き換わる
        r = self.api("/api/folders/rename", {"path": top, "to": "検証/移動先"})
        self.assertTrue(r["ok"], r)
        doc = self.api(f"/api/docs/{self.internal['id']}")["doc"]
        self.assertTrue(doc["folder"].startswith("検証/移動先"))
        moved_scope = self.api(f"/api/chats/{chat['id']}")["chat"]["scope"]["folders"]
        self.assertEqual(moved_scope, ["検証/移動先" + src[len(top):]])
        self.assertIn("error", self.api("/api/folders/rename", {"path": "検証", "to": "検証/中"}))
        # 文書の移動
        self.assertEqual(self.api("/api/docs/move", {"doc_ids": [self.public["id"]], "folder": "検証/空のフォルダ"})["moved"], 1)
        tree = {f["path"]: f for f in self.api("/api/folders")["folders"]}
        self.assertEqual(tree["検証/空のフォルダ"]["count"], 1)
        # 削除: 文書は消えず1つ上へ
        n_docs = len(self.api("/api/docs")["docs"])
        self.assertTrue(self.api("/api/folders/delete", {"path": "検証/空のフォルダ"})["ok"])
        self.assertEqual(self.api(f"/api/docs/{self.public['id']}")["doc"]["folder"], "検証")
        self.assertEqual(len(self.api("/api/docs")["docs"]), n_docs)
        # 一括操作
        r = self.api("/api/docs/bulk", {"doc_ids": [self.public["id"], self.internal["id"]],
                                        "action": "move", "folder": "まとめ"})
        self.assertEqual(r["moved"], 2)
        self.assertIn("まとめ", {f["path"] for f in self.api("/api/folders")["folders"]})

    def test_11_scope_union_and_description(self):
        docs = db.list_docs()
        a, b = docs[0], docs[1]
        scope = folders.clean_scope({"folder": a["folder"], "doc_ids": [b["id"]]})   # 旧形式の folder も読む
        self.assertEqual(scope["folders"], [a["folder"]])
        self.assertTrue(folders.in_scope(a, scope))
        self.assertTrue(folders.in_scope(b, scope))                 # フォルダ外でも個別に選んだ文書は入る
        info = folders.describe_scope(scope)
        self.assertIn("📁", info["label"])
        self.assertIn("📄", info["label"])
        self.assertEqual(folders.describe_scope({})["label"], "📚 すべての文書")

    # ------------------------------------------------------------ 相談: 相談先・採用根拠・RAG の流れ
    def test_20_answer_records_scope_methods_and_why(self):
        chat = self.api("/api/chats", {"scope": {"doc_ids": [self.internal["id"]]}})["chat"]
        self.assertEqual(chat["scope_info"]["docs"], 1)
        msg = self.ask(chat["id"], "北浜電機からのクレームの内容と、品質保証部の関係は？")
        meta = msg["meta"]
        self.assertIn("📄", meta["route"]["scope_label"])
        self.assertEqual(meta["route"]["docs_in_scope"], 1)
        ids = [m["id"] for m in meta["methods"]]
        self.assertTrue({"hybrid", "bm25"} & set(ids), ids)
        self.assertTrue(meta["answer_model"])
        s1 = meta["sources"][0]
        why = s1["why"]
        self.assertEqual(why["position"], 1)
        self.assertEqual(why["fused_rank"], 1)
        names = [x["name"] for x in why["signals"]]
        self.assertTrue(any("BM25" in n for n in names), names)
        self.assertTrue(all("rank" in x and "score" in x for x in why["signals"]))
        chats = self.api("/api/chats")["chats"]
        self.assertIn("scope_info", chats[0])

    # ------------------------------------------------------------ 使う RAG（検索方式）
    def ask_counting(self, body, question):
        chat = self.api("/api/chats", body)["chat"]
        start = len(mock_llm.REQUESTS)
        msg = self.ask(chat["id"], question)
        sent = mock_llm.REQUESTS[start:]
        chats = [r for r in sent if r["path"].endswith("/chat/completions")]
        embeds = [r for r in sent if r["path"].endswith("/embeddings")]
        return chat, msg["meta"], chats, embeds

    def signal_names(self, meta):
        return {x["name"].split("（")[0] for s in meta["sources"] for x in (s.get("why") or {}).get("signals", [])}

    def test_40_fast_strategy_calls_llm_only_for_the_answer(self):
        q = "SkyEdge S-2 の筐体固定ネジの不具合の件数は？"
        _, meta, chats, embeds = self.ask_counting({"strategy": "auto"}, q)
        self.assertGreater(len(chats), 1)                      # 自動: ルーター・判定・根拠チェック＋回答
        _, meta, chats, embeds = self.ask_counting({"strategy": "fast"}, q)
        self.assertEqual(len(chats), 1)                         # 速さ優先: 回答だけ
        self.assertEqual(meta["route"]["strategy_label"], "速さ優先")
        self.assertEqual(meta["route"]["by"], "rule")           # ルーターは規則で判定
        self.assertTrue(embeds)                                 # 質問の埋め込みは作る
        self.assertNotIn("decision", [m["id"] for m in meta["methods"]])
        self.assertTrue(meta["sources"])

    def test_41_custom_methods_are_used_as_selected(self):
        q = "SkyEdge S-2 の筐体固定ネジの不具合の件数は？"
        # 全文（BM25）だけ: 埋め込みも LLM の補助も使わない
        chat, meta, chats, embeds = self.ask_counting({"strategy": "custom", "methods": ["bm25"]}, q)
        self.assertEqual(chat["methods"], ["bm25"])
        self.assertEqual(len(chats), 1)
        self.assertEqual(embeds, [])
        self.assertEqual([m["id"] for m in meta["methods"]], ["bm25"])
        self.assertEqual(self.signal_names(meta), {"BM25"})
        # 埋め込みだけ
        _, meta, _, embeds = self.ask_counting({"strategy": "custom", "methods": ["vector"]}, q)
        self.assertTrue(embeds)
        self.assertEqual([m["id"] for m in meta["methods"]], ["vector"])
        self.assertEqual(self.signal_names(meta), {"埋め込み"})
        # 文書要約を選ぶと、要約の質問でなくても文書要約を資料に加える
        _, meta, _, _ = self.ask_counting({"strategy": "custom", "methods": ["bm25", "summary"]}, q)
        self.assertTrue(any(s["kind"] == "doc" for s in meta["sources"]), meta["sources"])
        # 知らない方式は捨てる・方式の更新
        chat = self.api("/api/chats", {"strategy": "custom", "methods": ["bm25", "magic"]})["chat"]
        self.assertEqual(chat["methods"], ["bm25"])
        chat = self.api(f"/api/chats/{chat['id']}/update", {"methods": ["vector", "graph", "crag"]})["chat"]
        self.assertEqual(chat["methods"], ["vector", "graph", "crag"])

    def test_42_custom_decision_only_uses_no_retriever(self):
        _, meta, chats, _ = self.ask_counting({"strategy": "custom", "methods": ["decision"]},
                                              "北浜電機から受けた指摘は何件？")
        ids = [m["id"] for m in meta["methods"]]
        self.assertEqual(ids, ["decision"])
        self.assertGreater(len(chats), 2)                       # 判定 + 回答

    def test_43_default_strategy_and_catalog(self):
        cfg = self.api("/api/config")
        cat = cfg["rag_methods"]
        self.assertEqual(cat["strategies"]["fast"]["estimate"]["llm_max"], 1)
        self.assertGreater(cat["strategies"]["auto"]["estimate"]["llm_max"], 3)
        self.assertIn("decision", cat["strategies"]["auto"]["methods"])
        self.assertNotIn("decision", cat["strategies"]["classic"]["methods"])
        self.api("/api/config", {"rag": {"default_strategy": "custom", "custom_methods": ["graph", "bm25", "x"]}})
        cfg = self.api("/api/config")
        self.assertEqual(cfg["rag"]["custom_methods"], ["bm25", "graph"])
        chat = self.api("/api/chats", {})["chat"]
        self.assertEqual(chat["strategy"], "custom")
        self.assertEqual(chat["methods"], ["bm25", "graph"])
        self.api("/api/config", {"rag": {"default_strategy": "auto"}})

    # ------------------------------------------------------------ 文字起こし
    def test_30_transcribe_via_local_whisper(self):
        wav = speech.test_tone()
        r = self.api("/api/transcribe?lang=ja-JP", raw=wav, ctype="audio/wav")
        self.assertIn("未設定", r["error"])
        self.api("/api/config", {"speech": {"whisper": {"url": self.whisper_url + "/v1", "model": "small",
                                                        "api_key": "sk-test"}}})
        self.assertNotIn("api_key", self.api("/api/config")["speech"]["whisper"])   # キーは返さない
        FakeWhisper.received = []
        r = self.api("/api/transcribe?lang=ja-JP", raw=wav, ctype="audio/wav")
        self.assertEqual(r["text"], "品質会議の決定事項は？")
        got = FakeWhisper.received[0]
        self.assertEqual(got["path"], "/v1/audio/transcriptions")
        self.assertEqual(got["auth"], "Bearer sk-test")
        fields = form_fields(got["body"], got["ctype"])
        self.assertEqual(fields["model"], b"small")
        self.assertEqual(fields["language"], b"ja")
        self.assertEqual(fields["file"][:4], b"RIFF")
        # whisper.cpp の server（/inference）は model を送らない
        self.api("/api/config", {"speech": {"whisper": {"url": self.whisper_url + "/inference"}}})
        FakeWhisper.received = []
        self.api("/api/transcribe", raw=wav, ctype="audio/wav")
        self.assertNotIn("model", form_fields(FakeWhisper.received[0]["body"], FakeWhisper.received[0]["ctype"]))
        # クラウドの接続先は許可が無ければ送らない
        self.api("/api/config", {"speech": {"whisper": {"url": self.whisper_url + "/v1", "location": "cloud"}}})
        FakeWhisper.received = []
        r = self.api("/api/transcribe", raw=wav, ctype="audio/wav")
        self.assertIn("クラウド", r["error"])
        self.assertEqual(FakeWhisper.received, [])
        self.api("/api/config", {"speech": {"allow_cloud": True}})
        self.assertEqual(self.api("/api/transcribe", raw=wav, ctype="audio/wav")["text"], "品質会議の決定事項は？")
        self.assertTrue(self.api("/api/config/test", {"speech": True})["ok"])
        self.api("/api/config", {"speech": {"allow_cloud": False, "whisper": {"location": "local"}}})

    def test_31_azure_whisper_url(self):
        cfg = config.load_config()
        cfg["speech"]["whisper"].update(provider="azure", url="https://res.openai.azure.com", model="whisper",
                                        api_key="k")
        url, headers, kind = speech.target(cfg)
        self.assertEqual(url, "https://res.openai.azure.com/openai/deployments/whisper/audio/transcriptions"
                              "?api-version=2024-06-01")
        self.assertEqual(headers, {"api-key": "k"})

    # ------------------------------------------------------------ グラフ: フォルダで色分け・フォルダ/文書の全体図
    def test_50_graph_views_by_folder(self):
        ent = self.api("/api/graph?view=entities&limit=300")
        self.assertEqual(ent["view"], "entities")
        self.assertTrue(all("folder" in n and n["folders"] for n in ent["nodes"]))
        folders = self.api("/api/graph?view=folders")
        self.assertEqual(folders["view"], "folders")
        self.assertTrue(folders["nodes"])
        node = folders["nodes"][0]
        self.assertEqual(node["kind"], "folder")
        self.assertEqual(node["id"], "g:" + node["key"])
        self.assertTrue(node["size"] > 0 and node["doc_count"] > 0)
        for e in folders["edges"]:
            self.assertTrue(e["shared"] >= 1 and 1 <= e["strength"] <= 10)
            self.assertIn("共通のエンティティ", e["description"])
        docs = self.api("/api/graph?view=docs")
        self.assertEqual({n["kind"] for n in docs["nodes"]}, {"doc"})
        titles = {d["title"] for d in self.api("/api/docs")["docs"]}
        self.assertTrue({n["name"] for n in docs["nodes"]} <= titles)


    def test_51_entity_dict_api_edits_graph_and_restores(self):
        before = self.api("/api/graph?limit=2000")
        node = max(before["nodes"], key=lambda n: (n["degree"], n["id"]))
        r = self.api("/api/entity-dict/rename", {"key": node["id"], "name": node["name"], "to": "テスト用の正式名"})
        self.assertTrue(r["ok"], r)
        key = r["renamed"]["key"]
        self.assertEqual(r["entries"][0]["name"], "テスト用の正式名")           # 辞書の一覧も返す
        after = {n["id"]: n for n in self.api("/api/graph?limit=2000")["nodes"]}
        self.assertNotIn(node["id"], after)
        self.assertEqual((after[key]["name"], after[key]["dict"], after[key]["degree"]),
                         ("テスト用の正式名", True, node["degree"]))
        self.assertIn(node["name"], after[key]["aliases"])
        self.api("/api/entity-dict/type", {"key": key, "name": "テスト用の正式名", "type": "注目"})
        typed = {n["id"]: n for n in self.api("/api/graph?limit=2000")["nodes"]}
        self.assertEqual(typed[key]["type"], "注目")
        self.assertIn("注目", self.api("/api/entity-dict")["types"])
        bad = self.api("/api/entity-dict/entry", {"name": "別の名前", "aliases": [node["name"]]})
        self.assertIn("登録済み", bad["error"])
        self.assertIn("error", self.api("/api/entity-dict/unknown", {}))
        # グラフから外す → 外す語を空にすると元に戻る
        self.api("/api/entity-dict/exclude", {"key": key, "name": "テスト用の正式名"})
        gone = {n["id"] for n in self.api("/api/graph?limit=2000")["nodes"]}
        self.assertNotIn(key, gone)
        self.assertNotIn(node["id"], gone)
        st = self.api("/api/entity-dict")
        self.assertEqual(st["entries"], [])
        self.assertEqual({x["name"] for x in st["excludes"]}, {"テスト用の正式名", node["name"]})
        self.api("/api/entity-dict/excludes", {"names": []})
        back = {n["id"]: n for n in self.api("/api/graph?limit=2000")["nodes"]}
        self.assertEqual((back[node["id"]]["name"], back[node["id"]]["degree"]), (node["name"], node["degree"]))


class TestGraphGroups(unittest.TestCase):
    """フォルダのまとめ方と、共通のエンティティでつなぐ全体図（ありふれたエンティティは軽く数える）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db.set_data_dir(Path(self.tmp.name))
        for doc_id, folder in (("d1", "A"), ("d2", "A/x"), ("d3", "B"), ("d4", "B")):
            db.write("INSERT INTO documents(id,title,filename,ext,sha256,folder,sensitivity,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,0,0)", (doc_id, f"文書{doc_id}", f"{doc_id}.md", ".md", doc_id, folder,
                                                   "internal"))
        ents = {"d1": ["共通社", "Y製品"], "d2": ["共通社", "Z設備"], "d3": ["共通社", "Y製品", "Z設備"],
                "d4": ["共通社"]}
        for doc_id, names in ents.items():
            graph.save_extraction(doc_id, f"{doc_id}#0", graph._clean_extraction(
                {"entities": [{"name": n} for n in names], "relations": []}))
        self.g = graph.KnowledgeGraph(None, {d: "internal" for d in ents})
        self.folder_of = {"d1": "A", "d2": "A/x", "d3": "B", "d4": "B"}

    def tearDown(self):
        db.close()
        self.tmp.cleanup()

    def test_folder_group(self):
        self.assertEqual(graph.folder_group("社内/品質/2026", "", 0), "社内/品質/2026")
        self.assertEqual(graph.folder_group("社内/品質/2026", "", 1), "社内")
        self.assertEqual(graph.folder_group("社内/品質/2026", "社内", 1), "社内/品質")
        self.assertEqual(graph.folder_group("社内", "社内", 1), "社内")
        self.assertEqual(graph.folder_group("", "", 2), "")

    def test_folder_overview_weights_rare_shared_entities(self):
        out = graph.group_payload(self.g, self.folder_of, kind="folder")
        keys = {n["key"] for n in out["nodes"]}
        self.assertEqual(keys, {"A", "A/x", "B"})
        edges = {tuple(sorted((e["source"], e["target"]))): e for e in out["edges"]}
        ab, axb, a_ax = edges[("g:A", "g:B")], edges[("g:A/x", "g:B")], edges[("g:A", "g:A/x")]
        self.assertEqual((ab["shared"], axb["shared"], a_ax["shared"]), (2, 2, 1))
        # 3 フォルダすべてに出る「共通社」だけでつながる A–A/x は、固有の語も共有する組より弱い
        self.assertLess(a_ax["strength"], ab["strength"])
        self.assertIn("Y製品", ab["description"])
        by_key = {n["key"]: n for n in out["nodes"]}
        self.assertEqual(by_key["B"]["doc_count"], 2)
        self.assertEqual(by_key["B"]["size"], 3)

    def test_depth_merges_subfolders_and_entity_colors(self):
        merged = {d: graph.folder_group(f, "", 1) for d, f in self.folder_of.items()}
        out = graph.group_payload(self.g, merged, kind="folder")
        self.assertEqual({n["key"] for n in out["nodes"]}, {"A", "B"})
        payload = self.g.to_payload(group_of=self.folder_of)
        common = next(n for n in payload["nodes"] if n["name"] == "共通社")
        self.assertEqual(common["folder"], "B")                       # 出てくる文書が最も多いフォルダ
        self.assertEqual(set(common["folders"]), {"A", "A/x", "B"})   # 複数のフォルダ → 画面で輪
        docs = graph.group_payload(self.g, {d: d for d in self.folder_of}, kind="doc",
                                   color_of={d: f for d, f in self.folder_of.items()})
        self.assertEqual({n["folder"] for n in docs["nodes"]}, {"A", "A/x", "B"})


class TestEntityDict(unittest.TestCase):
    """エンティティ辞書とグラフ画面の手直し（グラフを組み立てるときに当て、抽出結果は書き換えない）。"""

    TEXTS = {
        "d1": "北浜電機の購買部と SkyEdge S-2 の追加発注を協議した。北電の担当は早瀬。AirFeeder は別の製品。",
        "d2": "熱処理炉の点検で AirFeed の給電を試験した。品質保証部が確認した。",
        "d3": "AirFeed は SkyEdge S-2 に搭載する。",
    }
    ENTS = {
        "d1": ([("北浜電機", "組織"), ("北電", "組織"), ("SkyEdge S-2", "製品")],
               [("北浜電機", "SkyEdge S-2"), ("北電", "SkyEdge S-2")]),
        "d2": ([("熱処理炉", "設備"), ("品質保証部", "組織"), ("確認", "概念")],
               [("熱処理炉", "品質保証部"), ("品質保証部", "確認")]),
        "d3": ([("AirFeed", "製品"), ("SkyEdge S-2", "製品")], [("AirFeed", "SkyEdge S-2")]),
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db.set_data_dir(Path(self.tmp.name))
        for doc_id, text in self.TEXTS.items():
            db.write("INSERT INTO documents(id,title,filename,ext,sha256,folder,sensitivity,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,0,0)", (doc_id, f"文書{doc_id}", f"{doc_id}.md", ".md", doc_id, "",
                                                   "internal"))
            db.write("INSERT INTO chunks(id,doc_id,ord,text) VALUES(?,?,0,?)", (f"{doc_id}#0", doc_id, text))
            ents, rels = self.ENTS[doc_id]
            graph.save_extraction(doc_id, f"{doc_id}#0", graph._clean_extraction({
                "entities": [{"name": n, "type": t} for n, t in ents],
                "relations": [{"source": a, "target": b, "description": f"{a}と{b}"} for a, b in rels]}))
        self.sens = {d: "internal" for d in self.TEXTS}

    def tearDown(self):
        db.close()
        self.tmp.cleanup()

    def g(self):
        return graph.get_graph(None, self.sens)

    def rel(self, g, a, b):
        ka, kb = graph.norm_key(a), graph.norm_key(b)
        return g.relations.get((ka, kb) if ka <= kb else (kb, ka))

    def test_aliases_merge_type_and_question_by_alias(self):
        before = self.g()
        self.assertIn(graph.norm_key("北電"), before.entities)
        entitydict.save_entry({"name": "北浜電機", "type": "顧客", "aliases": "北電、北浜電機株式会社",
                               "description": "主要顧客"})
        g = self.g()
        self.assertIsNot(g, before)                                   # 辞書を変えたらグラフを作り直す
        self.assertNotIn(graph.norm_key("北電"), g.entities)
        ent = g.entities[graph.norm_key("北浜電機")]
        self.assertEqual((ent["name"], ent["type"], ent["dict"]), ("北浜電機", "顧客", True))
        self.assertTrue(ent["description"].startswith("主要顧客"))
        self.assertEqual(ent["aliases"], ["北電"])                     # 「北浜電機株式会社」は同じ照合キーなので省く
        self.assertEqual(self.rel(g, "北浜電機", "SkyEdge S-2")["count"], 2)   # 北電の関係もまとまる
        self.assertIn(graph.norm_key("北浜電機"), g.seed_entities("北電への発注は？", []))
        node = next(n for n in g.to_payload()["nodes"] if n["name"] == "北浜電機")
        self.assertEqual((node["dict"], node["aliases"]), (True, ["北電"]))
        # 抽出結果は書き換えない（辞書を消せば元に戻る）
        entitydict.delete_entry(graph.norm_key("北浜電機"))
        self.assertIn(graph.norm_key("北電"), self.g().entities)

    def test_excludes_remove_entity_and_its_relations(self):
        entitydict.set_excludes("確認\n資料")
        g = self.g()
        self.assertNotIn(graph.norm_key("確認"), g.entities)
        self.assertIsNone(self.rel(g, "品質保証部", "確認"))
        self.assertEqual([x["name"] for x in entitydict.excludes()], ["確認", "資料"])
        entitydict.set_excludes([])
        self.assertIn(graph.norm_key("確認"), self.g().entities)

    def test_match_in_text_adds_chunks_and_sentence_cooccurrence(self):
        key = graph.norm_key("AirFeed")
        self.assertEqual(self.g().entities[key]["chunks"], {"d3#0"})
        entitydict.save_entry({"name": "AirFeed", "type": "製品"})          # 本文から探す（既定）
        g = self.g()
        ent = g.entities[key]
        self.assertEqual(ent["chunks"], {"d2#0", "d3#0"})                   # LLM が拾わなかった d2 にも
        self.assertEqual(ent["found"], 1)                                   # 「AirFeeder」は別の語（d1 は無し）
        self.assertEqual(ent["docs"], {"d2", "d3"})
        self.assertIsNotNone(self.rel(g, "AirFeed", "熱処理炉"))             # 同じ文に出てくる
        self.assertEqual(self.rel(g, "AirFeed", "熱処理炉")["strength"], 3)
        self.assertIsNone(self.rel(g, "AirFeed", "品質保証部"))             # 別の文
        # 辞書にだけある語（LLM が1度も拾っていない）も本文から見つかればエンティティになる
        entitydict.save_entry({"name": "購買部", "type": "組織", "description": "北浜電機の部署"})
        ent = self.g().entities[graph.norm_key("購買部")]
        self.assertEqual((ent["docs"], ent["type"]), ({"d1"}, "組織"))
        self.assertTrue(ent["description"].startswith("北浜電機の部署"))
        # 本文から探さない設定なら足さない
        entitydict.save_entry({"name": "AirFeed", "type": "製品", "match": False}, old_key=key)
        self.assertEqual(self.g().entities[key]["chunks"], {"d3#0"})

    def test_rename_and_merge_from_graph(self):
        hq = graph.norm_key("熱処理炉")
        r = entitydict.rename(hq, "熱処理炉", "熱処理炉 A 号機")
        self.assertFalse(r["merged"])
        g = self.g()
        self.assertNotIn(hq, g.entities)
        self.assertEqual(g.entities[r["key"]]["name"], "熱処理炉 A 号機")
        self.assertIn("熱処理炉", g.entities[r["key"]]["aliases"])
        # ほかのエンティティと同じ名前にすると統合する（その間の関係は消え、ほかの関係は付け替わる）
        qa = graph.norm_key("品質保証部")
        r2 = entitydict.rename(qa, "品質保証部", "熱処理炉 A 号機")
        self.assertTrue(r2["merged"])
        self.assertEqual(r2["key"], r["key"])
        g = self.g()
        self.assertNotIn(qa, g.entities)
        ent = g.entities[r["key"]]
        self.assertEqual(set(ent["aliases"]), {"熱処理炉", "品質保証部"})
        self.assertNotIn((r["key"], r["key"]), g.relations)
        self.assertIsNotNone(self.rel(g, "熱処理炉 A 号機", "確認"))
        # 自分の別名を正式名にする
        r3 = entitydict.rename(r["key"], "熱処理炉 A 号機", "熱処理炉")
        self.assertEqual((r3["key"], r3["merged"]), (hq, False))
        ent = self.g().entities[hq]
        self.assertEqual(ent["name"], "熱処理炉")
        self.assertEqual(set(ent["aliases"]), {"熱処理炉 A 号機", "品質保証部"})
        # 種別の変更
        entitydict.set_type(hq, "熱処理炉", "重要設備")
        self.assertEqual(self.g().entities[hq]["type"], "重要設備")
        with self.assertRaises(ValueError):
            entitydict.rename(hq, "熱処理炉", "  ")

    def test_relations_added_and_hidden_and_exclude_dict_entity(self):
        a, s2 = graph.norm_key("AirFeed"), graph.norm_key("SkyEdge S-2")
        entitydict.hide_relation(a, s2, "AirFeed", "SkyEdge S-2")
        self.assertIsNone(self.rel(self.g(), "AirFeed", "SkyEdge S-2"))
        entitydict.add_relation("熱処理炉", "SkyEdge S-2", "筐体の熱処理", 8)
        rel = self.rel(self.g(), "熱処理炉", "SkyEdge S-2")
        self.assertTrue(rel["manual"])
        self.assertEqual((rel["description"], rel["strength"]), ("筐体の熱処理", 8))
        edge = next(e for e in self.g().to_payload()["edges"] if e["manual"])
        self.assertEqual({edge["source"], edge["target"]}, {graph.norm_key("熱処理炉"), s2})
        self.assertEqual(len(entitydict.relations()), 2)
        entitydict.delete_relation(s2, a)                                 # 取り消し（向きは問わない）
        self.assertIsNotNone(self.rel(self.g(), "AirFeed", "SkyEdge S-2"))
        with self.assertRaises(ValueError):
            entitydict.add_relation("熱処理炉", "熱処理炉")
        # 辞書のエンティティをグラフから外すと、名前と別名をまとめて外す語にする
        entitydict.save_entry({"name": "北浜電機", "aliases": ["北電"]})
        entitydict.exclude_entity(graph.norm_key("北浜電機"), "北浜電機")
        g = self.g()
        self.assertNotIn(graph.norm_key("北浜電機"), g.entities)
        self.assertNotIn(graph.norm_key("北電"), g.entities)
        self.assertEqual(entitydict.entries(), [])
        self.assertEqual({x["name"] for x in entitydict.excludes()}, {"北浜電機", "北電"})
        # 登録し直すと、外す語から消える
        entitydict.save_entry({"name": "北浜電機"})
        self.assertIn(graph.norm_key("北浜電機"), self.g().entities)

    def test_matcher_word_boundaries_spaces_and_longest(self):
        rx = entitydict._terms_regex(["airfeed", "skyedge s-2", "北浜電機", "北浜"])
        found = lambda t: [m.group(0) for m in rx.finditer(t)]  # noqa: E731
        self.assertEqual(found("XAirFeed と AirFeeder"), [])                 # 英数字の語は単語の区切りで
        self.assertEqual(found("AirFeed は"), ["AirFeed"])
        self.assertEqual(found("SKYEDGE  S-2 と SkyEdgeS-2"), ["SKYEDGE  S-2", "SkyEdgeS-2"])  # 大小・空白
        self.assertEqual(found("北浜電機の件と北浜の件"), ["北浜電機", "北浜"])  # 長いほうを優先

    def test_conflicts_and_bulk(self):
        entitydict.save_entry({"name": "北浜電機", "aliases": "北電"})
        with self.assertRaisesRegex(ValueError, "北浜電機"):
            entitydict.save_entry({"name": "北浜グループ", "aliases": "北電"})
        with self.assertRaisesRegex(ValueError, "登録済み"):
            entitydict.save_entry({"name": "北浜電機株式会社"})
        r = entitydict.bulk_add("名前,種別,別名\n北浜電機,顧客,KHD|北浜\nSkyEdge S-2\t製品\tS-2\n"
                                "# コメント\n北浜グループ,組織,北電\n")
        self.assertEqual((r["added"], r["updated"]), (1, 1))
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("5 行目", r["errors"][0])
        e = entitydict.get(graph.norm_key("北浜電機"))
        self.assertEqual((e["type"], e["aliases"]), ("顧客", ["北電", "KHD", "北浜"]))
        st = entitydict.state()
        self.assertIn("顧客", st["types"])
        self.assertIn("組織", st["types"])


class TestTerms(unittest.TestCase):
    def test_extract_terms(self):
        data = terms.extract_terms("青嶺製作所は仙台工場で産業用ワイヤレスセンサー SkyEdge S-2 を製造している。"
                                   "原因は熱処理炉の温度ばらつき。品質保証部は ISO9001 に基づき是正処置を実施した。")
        names = [e["name"] for e in data["entities"]]
        for want in ("青嶺製作所", "SkyEdge S-2", "産業用ワイヤレスセンサー", "熱処理炉", "ISO9001", "品質保証部"):
            self.assertIn(want, names)
        self.assertNotIn("原因", names)                              # 一般的すぎる語は除く
        types = {e["name"]: e["type"] for e in data["entities"]}
        self.assertEqual(types["青嶺製作所"], "組織")
        self.assertEqual(types["熱処理炉"], "設備")
        pairs = {(r["source"], r["target"]) for r in data["relations"]}
        self.assertIn(("SkyEdge S-2", "青嶺製作所"), pairs)         # 同じ文に出る語どうし
        self.assertTrue(all(r["description"] for r in data["relations"]))
        self.assertEqual(terms.extract_terms("。。"), {"entities": [], "relations": [], "keywords": []})


class TestMigration(unittest.TestCase):
    def test_old_database_becomes_legacy_readings(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "docvault.sqlite3"
            c = sqlite3.connect(path)
            c.executescript("""
              CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, filename TEXT, ext TEXT, size INTEGER DEFAULT 0,
                sha256 TEXT, folder TEXT DEFAULT '', tags TEXT DEFAULT '[]', sensitivity TEXT DEFAULT 'internal',
                owner TEXT DEFAULT 'local', visibility TEXT DEFAULT 'private', status TEXT DEFAULT 'ready',
                stage TEXT DEFAULT '', progress REAL DEFAULT 0, error TEXT DEFAULT '', warnings TEXT DEFAULT '[]',
                flags TEXT DEFAULT '{}', pages INTEGER, chars INTEGER DEFAULT 0, summary TEXT DEFAULT '',
                keywords TEXT DEFAULT '[]', created_at REAL, updated_at REAL);
              CREATE TABLE entity_mentions (entity_key TEXT, name TEXT, type TEXT, description TEXT,
                doc_id TEXT, chunk_id TEXT);
              CREATE TABLE relation_mentions (source TEXT, target TEXT, description TEXT, strength INTEGER,
                doc_id TEXT, chunk_id TEXT);
              CREATE TABLE vectors (chunk_id TEXT, space TEXT, doc_id TEXT, vec BLOB, PRIMARY KEY (chunk_id, space));
              CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, owner TEXT, mode TEXT, scope TEXT,
                touched_internal INTEGER, created_at REAL, updated_at REAL);
            """)
            c.execute("INSERT INTO documents(id,title,filename,ext,sha256,flags,summary,keywords,created_at,updated_at)"
                      " VALUES('d1','t','t.md','.md','x','{\"graph\":\"local\",\"summary\":\"local\"}','古い要約',"
                      "'[\"品質\"]',0,0)")
            c.execute("INSERT INTO entity_mentions VALUES('a社','A社','組織','','d1','d1#0')")
            c.execute("INSERT INTO vectors VALUES('d1#0','local:bge-m3','d1',x'00')")
            c.commit()
            c.close()
            db.set_data_dir(Path(tmp))
            try:
                rs = readings.list_for("d1")
                stages = {r["stage"]: r for r in rs}
                self.assertEqual(set(stages), {"graph", "summary", "embed"})
                self.assertTrue(all(r["active"] for r in rs))
                self.assertIn("以前の取り込み", stages["graph"]["label"])
                self.assertEqual(stages["summary"]["data"]["summary"], "古い要約")
                self.assertEqual(db.one("SELECT reading_id FROM entity_mentions")["reading_id"], stages["graph"]["id"])
                self.assertIn(graph.norm_key("A社"), graph.KnowledgeGraph(None, {"d1": "internal"}).entities)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
