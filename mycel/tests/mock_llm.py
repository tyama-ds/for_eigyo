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
        titles = re.findall(r"\[\d+\] (?:ノート|資料)「([^」]+)」", prompt)
        cited = " ".join(f"[[{t}]]" for t in titles[:2])
        return f"<think>考え中</think>ノートによると、稼働率を重視しています。{cited}"
    if task == "summarize":
        return '```json\n{"summary": "A社の更改案件。決裁者は稼働率重視。", "tags": ["顧客", "要フォロー"]}\n```'
    if task == "ingest_map":
        part = (re.search(r"（(\d+)/(\d+)）", prompt) or [None, "?"])[1]
        return f"- 部分{part}の要点: 初期費用 1,200万円"
    if task == "ingest":
        return ('考えた結果です。```json\n{"title": "A社 見積の概要", "doc_type": "見積書", "date": "2026-09-01",'
                ' "summary": "A社向けの概算見積。初期費用は1,200万円。", "points": ["初期費用 1,200万円", "保守 月額30万円"],'
                ' "entities": {"customers": ["A社"], "people": ["田中部長"], "products": ["生産管理システム"],'
                ' "projects": [], "others": ["保守"]}, "tags": ["見積", "A社"]}\n```')
    if task in ("ingest_links", "relate"):
        return '[{"n": 1, "reason": "同じ顧客の案件"}]'
    if task == "libmeta":
        return ('{"title": "在庫最適化のための需要予測手法", "authors": ["山田 太郎", "Smith, John"], "year": "2024",'
                ' "venue": "日本経営工学会論文誌", "volume": "75", "issue": "2", "pages": "100-110", "doi": "10.1234/jima.2024.001",'
                ' "type": "article", "lang": "ja", "abstract": "需要予測に基づく在庫最適化の手法を提案する。", "keywords": ["在庫最適化", "需要予測"]}')
    if task == "liblinks":
        return '[{"n": 1, "type": "extends", "reason": "同じ在庫最適化を需要予測で発展"}]'
    if task == "libsummary":
        return ('```json\n{"one_line": "需要予測で在庫を 20% 削減した。", "purpose": "欠品と過剰在庫の削減", "method": "時系列モデルと安全在庫の最適化",'
                ' "results": "3 拠点で在庫 20% 減", "limitations": "季節性の強い品目では精度が落ちる", "keywords": ["在庫最適化", "安全在庫"]}\n```')
    if task == "entities":
        return ('```json\n[{"name": "田中", "type": "person", "title": "部長", "org": "A社", "role": "決裁者",'
                ' "evidence": "田中部長が決裁"}, {"name": "山本 一郎", "type": "person", "org": "B社", "role": "窓口",'
                ' "evidence": "B社の山本さん"}, {"name": "A社", "type": "org", "role": "顧客"}]\n```')
    if task == "profile":
        refs = re.findall(r"^\[(\d+)\]", prompt, re.M)
        return "## 所属と立場\nA社の部長で決裁者（推定）。" + "".join(f"[{r}]" for r in refs[:2])
    if task == "transform":
        return "- 整えた文章"
    return "接続OK"


def start_mock_llm():
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            if self.path == "/v1/models":
                data = json.dumps({"data": [{"id": "mock"}, {"id": "mock-embed"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()

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
