"""テスト用のモック LLM サーバ（OpenAI 互換 / Azure OpenAI の両形式）。

プロンプトの [TASK:...] を見て決定論的な応答を返す。受け取った要求は ``requests`` に残す。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMBED_DIM = 16


def embed_vector(text: str) -> list[float]:
    vec = [0.0] * EMBED_DIM
    for i in range(len(text) - 1):
        vec[sum(map(ord, text[i:i + 2])) % EMBED_DIM] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def chat_response(messages: list[dict]) -> str:
    prompt = messages[-1]["content"] if messages else ""
    task = (re.search(r"\[TASK:(\w+)\]", prompt) or [None, ""])[1]
    if task == "ask":
        titles = re.findall(r"\[\d+\] ノート「([^」]+)」", prompt)
        cited = " ".join(f"[[{t}]]" for t in titles[:2])
        return f"<think>考え中</think>ノートによると、稼働率を重視しています。{cited}"
    if task == "summarize":
        return '```json\n{"summary": "A社の更改案件。決裁者は稼働率重視。", "tags": ["顧客", "要フォロー"]}\n```'
    if task == "transform":
        return "- 整えた文章"
    return "接続OK"


def start_mock_llm():
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            known = self.path.startswith(("/v1/", "/openai/deployments/"))
            if known and "/chat/completions" in self.path:
                out = {"choices": [{"message": {"role": "assistant", "content": chat_response(body.get("messages", []))}}]}
            elif known and "/embeddings" in self.path:
                inputs = body.get("input") or []
                out = {"data": [{"index": i, "embedding": embed_vector(t)} for i, t in enumerate(inputs)]}
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(out).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", requests


if __name__ == "__main__":
    import time
    srv, url, _ = start_mock_llm()
    print(url, flush=True)
    while True:
        time.sleep(3600)
