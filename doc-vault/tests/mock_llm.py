"""テスト用のモック LLM サーバー（OpenAI 互換 /chat/completions・/embeddings、ストリーミング対応）。

プロンプト中の [TASK:...] タグを見て決定論的な応答を返す。実 LLM なしで
取り込み（グラフ抽出・要約）→ 相談（ルーター・根拠チェック・回答）を通しで検証できる。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KNOWN_ENTITIES = [
    ("青嶺製作所", "組織"), ("SkyEdge S-2", "製品"), ("北浜電機", "組織"), ("柏木周平", "人物"),
    ("早瀬凛", "人物"), ("むつみ食品機械", "組織"), ("AirFeed", "技術"), ("仙台工場", "場所"),
    ("品質保証部", "組織"), ("ISO 9001", "規格・法令"), ("熱処理炉", "設備"),
]
EMBED_DIM = 48
REQUESTS: list[dict] = []          # テストから送信内容を検査するための記録


def embed_vector(text: str) -> list[float]:
    vec = [0.0] * EMBED_DIM
    text = text.lower()
    for i in range(len(text) - 1):
        vec[sum(ord(c) for c in text[i:i + 2]) % EMBED_DIM] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def respond(prompt: str) -> str:
    m = re.search(r"\[TASK:([a-z0-9_]+)\]", prompt)
    task = m.group(1) if m else ""
    if task == "graph_extract":
        body = prompt.split("# テキスト", 1)[-1]
        found = [(n, t) for n, t in KNOWN_ENTITIES if n in body]
        ents = [{"name": n, "type": t, "description": f"{n}に関する記述。"} for n, t in found]
        rels = [{"source": found[i][0], "target": found[i + 1][0],
                 "description": f"{found[i][0]}は{found[i + 1][0]}と関係する", "strength": 7}
                for i in range(len(found) - 1)]
        return "```json\n" + json.dumps({"entities": ents, "relations": rels,
                                         "keywords": ["品質", "製造"]}, ensure_ascii=False) + "\n```"
    if task == "community_summary":
        return json.dumps({"title": "製造と品質のまとまり", "summary": "製品・工場・品質保証の関係をまとめたテーマ。"},
                          ensure_ascii=False)
    if task == "doc_summary":
        title = re.search(r"文書「(.+?)」", prompt)
        body = prompt.split("# 本文", 1)[-1]
        ents = [n for n, _ in KNOWN_ENTITIES if n in body]
        return json.dumps({"summary": f"{title.group(1) if title else '文書'}の要約。"
                                      f"{'・'.join(ents)}について述べる。",
                           "keywords": ents[:6] or ["製品", "品質"]}, ensure_ascii=False)
    if task == "doc_part_summary":
        return "部分の要約です。"
    if task == "route":
        q = prompt.split("# 最新の質問", 1)[-1].strip()
        if re.match(r"^(こんにちは|ありがとう)", q):
            return json.dumps({"retrieval": False, "type": "chat", "standalone": q,
                               "keywords": [], "entities": []}, ensure_ascii=False)
        typ = "summary" if "要約" in q or "全体" in q else "relation" if "関係" in q else "fact"
        ents = [n for n, _ in KNOWN_ENTITIES if n in q]
        return json.dumps({"retrieval": True, "type": typ, "standalone": q,
                           "keywords": ents or [q[:10]], "entities": ents}, ensure_ascii=False)
    if task == "decide":
        return decide(prompt)
    if task == "decide_choice":
        return decide_choice(prompt)
    if task == "grade":
        return json.dumps({"sufficient": True, "missing": "", "query": ""})
    if task == "rerank":
        return json.dumps({"ranking": [2, 1, 3]})
    if "# 参考資料" in prompt:
        return "<think>資料を確認中</think>資料によると、SkyEdge S-2 は青嶺製作所の主力製品です [S1]。"
    return "接続OK"


def _grams(text: str) -> set[str]:
    text = text.lower()
    out = set(re.findall(r"[a-z0-9][a-z0-9\-]+", text))
    for run in re.findall(r"[぀-ヿ㐀-鿿]+", text):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def decide(prompt: str) -> str:
    """DecisionRAG の採点: 質問と候補の文字バイグラムの重なりで 0〜9。"""
    q = prompt.split("# 質問", 1)[-1].split("# 候補", 1)[0]
    qg = _grams(q)
    lines = re.findall(r"^\[(\d+)\] (.*)$", prompt.split("# 候補", 1)[-1], re.MULTILINE)
    out = []
    for num, desc in lines:
        hit = len(qg & _grams(desc)) / max(1, len(qg))
        out.append(f"{num}={min(9, round(hit * 14))}")
    return "\n".join(out)


def decide_choice(prompt: str) -> str:
    """選択式: 質問との重なりが最も大きい候補の記号を1文字。"""
    q = prompt.split("# 質問", 1)[-1].split("# 候補", 1)[0]
    qg = _grams(q)
    lines = re.findall(r"^([A-T])\. (.*)$", prompt.split("# 候補", 1)[-1], re.MULTILINE)
    best = max(lines, key=lambda t: len(qg & _grams(t[1])))[0] if lines else "A"
    return best


def choice_logprobs(letter: str) -> list[dict]:
    import math
    other = "B" if letter == "A" else "A"
    return [{"token": letter, "logprob": math.log(0.8),
             "top_logprobs": [{"token": letter, "logprob": math.log(0.8)},
                              {"token": other, "logprob": math.log(0.2)}]}]


def decide_logprobs(text: str) -> list[dict]:
    """採点の出力をトークン列にし、数字のトークンに上位候補の確率を付ける。"""
    import math
    toks = []
    for line in text.splitlines():
        num, digit = line.split("=")
        d = int(digit)
        alt = d - 1 if d > 0 else 1
        toks.append({"token": num, "logprob": -0.01, "top_logprobs": []})
        toks.append({"token": "=", "logprob": -0.01, "top_logprobs": []})
        toks.append({"token": digit, "logprob": math.log(0.7),
                     "top_logprobs": [{"token": digit, "logprob": math.log(0.7)},
                                      {"token": str(alt), "logprob": math.log(0.3)}]})
        toks.append({"token": "\n", "logprob": -0.01, "top_logprobs": []})
    return toks


def jev_answer(body: dict) -> dict:
    """OpenRouter Decisions API（Jev）のモック: noul は重なりの割合、choice はその正規化。"""
    state = body.get("state") or {}
    qg = _grams(str(state.get("query", "")))
    cands = dict(re.findall(r"^(\d+)\. (.*)$", str(state.get("candidates", "")), re.MULTILINE))

    def rel(text):
        return min(1.0, len(qg & _grams(text)) / max(1, len(qg)) * 1.6)
    answers = {}
    for name, q in (body.get("questions") or {}).items():
        if q.get("type") == "noul":
            answers[name] = {"type": "noul", "noul": round(rel(cands.get(name.lstrip("c"), "")), 3)}
        elif q.get("type") == "choice":
            raw = {k: rel(v) + 0.01 for k, v in (q.get("criteria") or {}).items()}
            total = sum(raw.values()) or 1.0
            probs = {k: v / total for k, v in raw.items()}
            best = max(probs, key=probs.get)
            answers[name] = {"type": "choice", "choice": best, "confidence": probs[best],
                             "probabilities": probs}
    return {"answers": answers, "usage": {"cost": 0.0001}}


class MockLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        REQUESTS.append({"path": self.path, "body": body})
        if self.path.endswith("/chat/completions"):
            prompt = "\n".join(str(m.get("content", "")) for m in body.get("messages", [])
                               if m.get("role") == "user")
            last = [m for m in body.get("messages", []) if m.get("role") == "user"]
            text = respond(str(last[-1].get("content", "")) if last else prompt)
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for i in range(0, len(text), 7):
                    chunk = {"choices": [{"delta": {"content": text[i:i + 7]}}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            choice = {"message": {"role": "assistant", "content": text}}
            last_text = last[-1].get("content", "") if last else ""
            if body.get("logprobs") and "[TASK:decide]" in last_text:
                choice["logprobs"] = {"content": decide_logprobs(text)}
            elif body.get("logprobs") and "[TASK:decide_choice]" in last_text:
                choice["logprobs"] = {"content": choice_logprobs(text)}
            out = {"choices": [choice]}
        elif self.path.endswith("/decisions"):
            out = jev_answer(body)
        elif self.path.endswith("/embeddings"):
            texts = body.get("input")
            texts = [texts] if isinstance(texts, str) else texts
            out = {"data": [{"index": i, "embedding": embed_vector(t)} for i, t in enumerate(texts)]}
        else:
            self.send_error(404)
            return
        data = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def start_mock_llm() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockLLMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"
