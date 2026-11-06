"""JSON を書かせる呼び出し（chat_json）と GraphRAG の再試行の決め方。

500 字の文書の GraphRAG 構築に 10 分かかって「抽出JSONの解析に失敗」した問題の回帰テスト。原因は、
ローカル LLM が JSON を書き終えずに（同じ関係の繰り返し・止まらない思考・JSON の後の説明）出力の上限まで
生成し、その切れた出力を丸ごと捨てたうえで、温度 0 の同じ呼び出しをもう一度繰り返していたこと。

ここでは、型ごとに振る舞いを再現する偽の LLM（ストリーミング対応）を立てて確かめる:
  - JSON が閉じたら・同じ内容の繰り返しに入ったら、上限を待たずに打ち切る
  - Qwen3 系には /no_think を付けて思考を止める
  - 上限で切れた JSON からは、書けた要素までを取り出す
  - やり直しは理由に応じて1回だけ（上限で切れたものは繰り返さない）
"""
from __future__ import annotations

import ast
import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from ragcore.engines.graphrag import GraphRAGEngine  # noqa: E402
from ragcore.llm import LLMClient  # noqa: E402
from ragcore.textutil import find_repetition, json_end, salvage_json  # noqa: E402

GOOD = {"entities": [{"name": "青嶺製作所", "type": "組織", "description": "センサーの会社。"},
                     {"name": "SkyEdge S-2", "type": "製品", "description": "無線センサー。"}],
        "relations": [{"source": "青嶺製作所", "target": "SkyEdge S-2", "description": "製造する", "strength": 8}]}
REL = '{"source": "青嶺製作所", "target": "SkyEdge S-2", "description": "製造する", "strength": 8}, '


def produce(mode: str, prompt: str):
    """(本文の断片, 思考欄の断片) を順に返す。上限までの打ち切りはサーバ側で行う。"""
    good = json.dumps(GOOD, ensure_ascii=False)
    if mode == "good" or ("/no_think" in prompt and mode in ("think", "reasoning")):
        yield good, ""
        return
    if mode == "think":
        yield "<think>", ""
        while True:
            yield "まず本文を読み、エンティティを考える。次に関係を考える。", ""
    if mode == "reasoning":
        while True:
            yield "", "Let me think about the entities and relations again. "
    if mode == "loop":
        yield good[:good.index('"relations": [') + len('"relations": [')], ""
        while True:
            yield REL, ""
    if mode == "loop_after":
        yield good, ""
        while True:
            yield "\n以上が抽出結果です。補足として、", ""
    if mode == "long":                      # 繰り返しではないが長すぎて上限で切れる
        yield '{"entities": [', ""
        i = 0
        while True:
            yield json.dumps({"name": f"部品{i}", "type": "製品", "description": f"説明{i * 7}"},
                             ensure_ascii=False) + ", ", ""
            i += 1
    if mode == "prose":
        yield "この文書には青嶺製作所と SkyEdge S-2 が登場します。", ""


class FakeLLM(BaseHTTPRequestHandler):
    mode = "good"
    stream_ok = True
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        prompt = "\n".join(m["content"] for m in body["messages"])
        call = {"stream": bool(body.get("stream")), "no_think": "/no_think" in prompt,
                "temperature": body.get("temperature"), "prompt": prompt}
        FakeLLM.calls.append(call)
        limit = int(body.get("max_tokens") or 1024)
        used, content, reasoning, finish = 0, "", "", "stop"
        stream = call["stream"] and FakeLLM.stream_ok
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
        try:
            for c, r in produce(FakeLLM.mode, prompt):
                n = max(1, (len(c) + len(r)) // 3)          # 1 トークン ≈ 3 文字
                if used + n > limit:
                    finish = "length"
                    break
                used += n
                time.sleep(n / 20000)
                content += c
                reasoning += r
                if stream:
                    delta = {k: v for k, v in (("content", c), ("reasoning_content", r)) if v}
                    self.wfile.write(f"data: {json.dumps({'choices': [{'delta': delta}]}, ensure_ascii=False)}\n\n"
                                     .encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            call["closed_early"] = used
            return
        call["tokens"] = used
        if stream:
            try:
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}]})}\n\n"
                                 .encode() + b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        msg = {"role": "assistant", "content": content, **({"reasoning_content": reasoning} if reasoning else {})}
        data = json.dumps({"choices": [{"message": msg, "finish_reason": finish}]}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class TestJsonParts(unittest.TestCase):
    def test_salvage_truncated_json(self):
        self.assertEqual(salvage_json('{"entities": [{"name": "A"}, {"name": "B"}, {"name": "C'),
                         {"entities": [{"name": "A"}, {"name": "B"}]})
        self.assertEqual(salvage_json('{"entities": [], "relations": [{"source": "A", "target": "B"}, {"sou'),
                         {"entities": [], "relations": [{"source": "A", "target": "B"}]})
        self.assertEqual(salvage_json('```json\n{"entities": [{"name": "A", "type": "組'),
                         {"entities": [{"name": "A"}]})
        self.assertIsNone(salvage_json('{"a": "途中'))
        self.assertIsNone(salvage_json("JSON ではない文章"))

    def test_json_end(self):
        self.assertEqual(json_end('前置き {"a": [1, "}", {"b": 2}]} 後ろ'), 29)
        self.assertIsNone(json_end('{"a": [1'))

    def test_find_repetition(self):
        self.assertTrue(find_repetition('{"relations": [' + REL * 4))
        self.assertFalse(find_repetition('{"relations": [' + REL * 2))
        self.assertTrue(find_repetition("まず本文を読み、エンティティを考える。次に関係を考える。" * 10))
        self.assertFalse(find_repetition("".join(f"エンティティ{i}は製品である。" for i in range(60))))
        self.assertFalse(find_repetition("{\n" + " " * 400))

    def test_same_as_doc_vault(self):
        """doc-vault の textutil と同じ実装であること（片方だけ直さない）。"""
        def defs(path):
            return {n.name: ast.dump(n) for n in ast.parse(path.read_text(encoding="utf-8")).body
                    if isinstance(n, ast.FunctionDef)}
        ours = defs(APP_DIR / "ragcore" / "textutil.py")
        theirs = defs(APP_DIR.parent / "doc-vault" / "docvault" / "textutil.py")
        for name in ("json_end", "salvage_json", "find_repetition"):
            self.assertEqual(ours[name], theirs[name], f"{name} が doc-vault と違う")


class TestChatJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeLLM.calls = []
        FakeLLM.stream_ok = True

    def call(self, mode, model="llama3.1:8b", max_tokens=4096):
        FakeLLM.mode = mode
        llm = LLMClient({"base_url": self.base, "model": model, "max_tokens": max_tokens,
                         "use_proxy": False, "request_timeout": 30})
        started = time.time()
        data, info = llm.chat_json("[TASK:graph_extract]\n抽出して", system="JSON のみ", max_tokens=max_tokens)
        return data, info, time.time() - started

    def test_good(self):
        data, info, _ = self.call("good")
        self.assertEqual(data, GOOD)
        self.assertTrue(FakeLLM.calls[0]["stream"])

    def test_repetition_is_cut_early_and_salvaged(self):
        data, info, sec = self.call("loop")
        self.assertEqual(info["finish"], "repetition")
        self.assertTrue(info["salvaged"])
        self.assertEqual([e["name"] for e in data["entities"]], ["青嶺製作所", "SkyEdge S-2"])
        self.assertTrue(data["relations"])
        time.sleep(0.2)
        self.assertLess(FakeLLM.calls[0].get("closed_early", 9999), 1000)   # 上限 4096 まで待たない
        self.assertLess(sec, 5)

    def test_stops_when_json_closes(self):
        data, info, _ = self.call("loop_after")
        self.assertEqual(data, GOOD)
        self.assertEqual(info["finish"], "json_complete")
        time.sleep(0.2)
        self.assertIn("closed_early", FakeLLM.calls[0])

    def test_qwen3_thinking_is_turned_off(self):
        data, info, _ = self.call("think", model="qwen3:14b")
        self.assertEqual(data, GOOD)
        self.assertTrue(FakeLLM.calls[0]["no_think"])
        data, info, _ = self.call("reasoning", model="Qwen3-30B-A3B")
        self.assertEqual(data, GOOD)

    def test_endless_thinking_of_other_models_is_reported(self):
        for mode in ("think", "reasoning"):
            data, info, sec = self.call(mode, model="deepseek-r1:14b")
            self.assertIsNone(data)
            self.assertFalse(FakeLLM.calls[-1]["no_think"])
            self.assertIn("思考", info["reason"])
            self.assertLess(sec, 5)

    def test_length_cut_without_repetition(self):
        data, info, _ = self.call("long", max_tokens=300)
        self.assertEqual(info["finish"], "length")
        self.assertTrue(info["salvaged"])
        self.assertGreater(len(data["entities"]), 3)

    def test_server_without_streaming(self):
        FakeLLM.stream_ok = False
        data, info, _ = self.call("good")
        self.assertEqual(data, GOOD)
        data, info, _ = self.call("long", max_tokens=300)
        self.assertTrue(info["salvaged"])


class StubLLM:
    """chat_json の結果を順に返すスタブ（GraphRAG の再試行の決め方を調べる）。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def chat_json(self, prompt, *, system="", max_tokens=None, temperature=0.0, on_progress=None):
        self.calls.append({"prompt": prompt, "temperature": temperature})
        data, finish, reason = self.results.pop(0)
        return data, {"finish": finish, "content_chars": 10, "think_chars": 0, "seconds": 1.0,
                      "salvaged": False, "max_tokens": max_tokens, "reason": reason}


class TestRetryPolicy(unittest.TestCase):
    def run_policy(self, results):
        llm = StubLLM(results)
        ctx = type("Ctx", (), {"llm": llm, "cfg": {"max_tokens": 4096}})()
        data, retried, info = GraphRAGEngine()._chat_json(ctx, "[TASK:graph_extract] 抽出して")
        return llm, data, retried, info

    def test_no_retry_after_length_cut(self):
        llm, data, retried, info = self.run_policy([(None, "length", "上限で切れた")])
        self.assertEqual((len(llm.calls), retried, data), (1, False, None))   # 同じ呼び出しを繰り返さない

    def test_repetition_retries_with_higher_temperature(self):
        llm, data, retried, _ = self.run_policy([(None, "repetition", "繰り返し"), (GOOD, "json_complete", "")])
        self.assertEqual([c["temperature"] for c in llm.calls], [0.0, 0.5])
        self.assertEqual((data, retried), (GOOD, True))

    def test_prose_retries_with_reminder(self):
        llm, data, retried, info = self.run_policy([(None, "stop", "文章"), (None, "stop", "また文章")])
        self.assertIn("【再出力】", llm.calls[1]["prompt"])
        self.assertIn("やり直しも失敗", info["reason"])
        self.assertEqual(info["seconds"], 2.0)


if __name__ == "__main__":
    unittest.main()
