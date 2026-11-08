"""JSON を書かせる呼び出し（LLM.chat_json）とグラフ抽出のやり直しの決め方。

ローカル LLM が JSON を書き終えずに（同じ関係の繰り返し・止まらない思考・JSON の後の説明）出力の上限まで
生成し、切れた出力を丸ごと捨てて同じ呼び出しを繰り返していたため、小さな文書のグラフ抽出に何分もかかって
失敗していた問題の回帰テスト。rag-orchestrator の tests/test_json_call.py と同じ型を再現する。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from docvault import config, graph  # noqa: E402
from docvault.llm import LLM  # noqa: E402

GOOD = {"entities": [{"name": "青嶺製作所", "type": "組織", "description": "センサーの会社。"},
                     {"name": "SkyEdge S-2", "type": "製品", "description": "無線センサー。"}],
        "relations": [{"source": "青嶺製作所", "target": "SkyEdge S-2", "description": "製造する", "strength": 8}],
        "keywords": ["センサー"]}
REL = '{"source": "青嶺製作所", "target": "SkyEdge S-2", "description": "製造する", "strength": 8}, '


def produce(mode: str, prompt: str):
    good = json.dumps(GOOD, ensure_ascii=False)
    if mode == "good" or ("/no_think" in prompt and mode == "think"):
        yield good
        return
    if mode == "think":
        yield "<think>"
        while True:
            yield "まず本文を読み、エンティティを考える。次に関係を考える。"
    if mode == "loop":
        yield good[:good.index('"relations": [') + len('"relations": [')]
        while True:
            yield REL
    if mode == "loop_after":
        yield good
        while True:
            yield "\n以上が抽出結果です。補足として、"
    if mode == "prose":
        yield "この文書には青嶺製作所が登場します。"


class FakeLLM(BaseHTTPRequestHandler):
    mode = "good"
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        prompt = "\n".join(m["content"] for m in body["messages"])
        call = {"no_think": "/no_think" in prompt, "temperature": body.get("temperature"), "prompt": prompt}
        FakeLLM.calls.append(call)
        limit, used = int(body.get("max_tokens") or 1024), 0
        if not body.get("stream"):                         # 通常の呼び出しには一括で返す
            text = "".join(c for _, c in zip(range(1), produce("good", prompt)))
            data = json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                                            "finish_reason": "stop"}]}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        finish = "stop"
        try:
            for c in produce(FakeLLM.mode, prompt):
                n = max(1, len(c) // 3)
                if used + n > limit:
                    finish = "length"
                    break
                used += n
                time.sleep(n / 20000)
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': c}}]}, ensure_ascii=False)}\n\n"
                                 .encode())
                self.wfile.flush()
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}]})}\n\n"
                             .encode() + b"data: [DONE]\n\n")
        except ConnectionError:            # 切断（Linux は BrokenPipe・Reset、Windows は Aborted）
            call["closed_early"] = used
            return
        call["tokens"] = used

    def log_message(self, *args):
        pass


class TestChatJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def llm(self, model):
        cfg = config._merge(config.DEFAULT_CONFIG, {})
        cfg["profiles"]["local"].update(provider="openai", base_url=self.base, model=model, use_proxy=False)
        return LLM(cfg)

    def extract(self, mode, model="llama3.1:8b"):
        FakeLLM.mode = mode
        FakeLLM.calls = []
        started = time.time()
        data, info = graph.extract_chunk(self.llm(model), "local", "internal", "品質",
                                         {"text": "青嶺製作所は SkyEdge S-2 を製造する。", "locator": "p.1"})
        return data, info, time.time() - started

    def test_good(self):
        data, info, _ = self.extract("good")
        self.assertEqual([e["name"] for e in data["entities"]], ["青嶺製作所", "SkyEdge S-2"])

    def test_repetition_is_cut_early_and_salvaged(self):
        data, info, sec = self.extract("loop")
        self.assertEqual(info["finish"], "repetition")
        self.assertTrue(info["salvaged"])
        self.assertTrue(data["relations"])
        self.assertEqual(len(FakeLLM.calls), 1)                  # 取り出せたのでやり直さない
        time.sleep(0.2)
        self.assertLess(FakeLLM.calls[0].get("closed_early", 9999), 1000)
        self.assertLess(sec, 5)

    def test_stops_when_json_closes(self):
        data, info, _ = self.extract("loop_after")
        self.assertEqual(info["finish"], "json_complete")
        self.assertTrue(data["entities"])

    def test_qwen3_thinking_is_turned_off(self):
        data, info, _ = self.extract("think", model="qwen3:14b")
        self.assertTrue(data["entities"])
        self.assertTrue(FakeLLM.calls[0]["no_think"])

    def test_endless_thinking_is_cut_and_reported(self):
        data, info, sec = self.extract("think", model="deepseek-r1:14b")
        self.assertIsNone(data)
        self.assertIn("思考", info["reason"])
        self.assertEqual([c["temperature"] for c in FakeLLM.calls], [0.0, 0.5])   # 繰り返しは温度を上げて1回だけ
        self.assertLess(sec, 5)

    def test_prose_retries_once_with_reminder(self):
        data, info, _ = self.extract("prose")
        self.assertIsNone(data)
        self.assertEqual(len(FakeLLM.calls), 2)
        self.assertIn("【再出力】", FakeLLM.calls[1]["prompt"])
        self.assertIn("やり直しも失敗", info["reason"])

    def test_ingest_calls_turn_off_qwen3_thinking(self):
        FakeLLM.mode = "good"
        FakeLLM.calls = []
        self.llm("qwen3:14b").chat("local", "要約して", sensitivity="internal", purpose="ingest")
        self.llm("qwen3:14b").chat("local", "質問への回答", sensitivity="internal", purpose="answer")
        self.assertEqual([c["no_think"] for c in FakeLLM.calls], [True, False])   # 回答では思考を止めない


if __name__ == "__main__":
    unittest.main()
