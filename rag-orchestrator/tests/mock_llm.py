"""テスト用のモック LLM サーバー / クライアント。

実 LLM なしで GraphRAG パイプラインと API フローを検証するため、
プロンプト中の [TASK:...] タグを見て決定論的な応答を返す。
埋め込みは文字バイグラムのハッシュで作る（似た文章 → 似たベクトル）。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# サンプル文書（sample_docs/）に登場する語彙。抽出モックはこれを拾う。
KNOWN_ENTITIES = [
    ("青嶺製作所", "組織"), ("SkyEdge", "製品"), ("北浜電機", "組織"),
    ("柏木周平", "人物"), ("早瀬凛", "人物"), ("むつみ食品機械", "組織"),
    ("東和精工", "組織"), ("AirFeed", "技術"), ("LinkMist", "技術"),
    ("仙台開発センター", "場所"),
    # ユニットテスト用の小型コーパスの語彙
    ("アルファ社", "組織"), ("ベータ製品", "製品"), ("ガンマ研究所", "組織"),
]

EMBED_DIM = 32


def embed_vector(text: str) -> list[float]:
    """文字バイグラムのハッシュによる決定論的な擬似埋め込み。"""
    vec = [0.0] * EMBED_DIM
    text = text.lower()
    for i in range(len(text) - 1):
        vec[hash(text[i:i + 2]) % EMBED_DIM] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def mock_chat_response(prompt: str) -> str:
    """[TASK:...] タグに応じた決定論的な応答。"""
    task_m = re.search(r"\[TASK:([a-z0-9_]+)\]", prompt)
    task = task_m.group(1) if task_m else ""

    if task == "graph_extract":
        body = prompt.split("# テキスト", 1)[-1]
        found = [(name, typ) for name, typ in KNOWN_ENTITIES if name in body]
        entities = [{"name": name, "type": typ,
                     "description": f"{name}に関する記述がある。"}
                    for name, typ in found]
        relations = [{"source": found[i][0], "target": found[i + 1][0],
                      "description": f"{found[i][0]}と{found[i + 1][0]}は関連する",
                      "strength": 6}
                     for i in range(len(found) - 1)]
        return json.dumps({"entities": entities, "relations": relations},
                          ensure_ascii=False)

    if task == "community_summary":
        names = re.findall(r"- ([^（]+)（", prompt.split("# エンティティ", 1)[-1])
        title = "・".join(n.strip() for n in names[:2]) or "コミュニティ"
        return json.dumps({"title": title,
                           "summary": f"{title} を中心とするまとまり。相互に関連が強い。"},
                          ensure_ascii=False)

    if task == "global_map":
        cid_m = re.search(r"コミュニティ要約（(C\d+)）", prompt)
        cid = cid_m.group(1) if cid_m else "C?"
        return json.dumps({"points": [
            {"text": f"{cid} の要約から言えるポイント", "score": 7}]}, ensure_ascii=False)

    if task == "global_reduce":
        return "コーパス全体の統合回答です。[C1]"

    if task.endswith("answer"):   # local_answer / vector_answer / global_fallback_answer 等
        return f"{task} による回答: 資料に基づく説明です。[S1]"

    if task == "decision_doc_summary":
        body = prompt.split("# 本文", 1)[-1]
        found = [name for name, _ in KNOWN_ENTITIES if name in body]
        return json.dumps({"summary": "・".join(found) + "について述べた文書。",
                           "keywords": found[:8]}, ensure_ascii=False)

    if task == "decide":
        return decide(prompt)

    if task == "decide_choice":
        return decide_choice(prompt)

    if task == "synthesis":
        return ("## 統合回答\n各エンジンの回答は概ね一致しています。\n"
                "## 一致点\n- 資料に基づく説明である点\n## 相違点・片方にしかない情報\nなし")

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
    return "\n".join(f"{num}={min(9, round(len(qg & _grams(desc)) / max(1, len(qg)) * 14))}"
                     for num, desc in lines)


def decide_choice(prompt: str) -> str:
    q = prompt.split("# 質問", 1)[-1].split("# 候補", 1)[0]
    qg = _grams(q)
    lines = re.findall(r"^([A-T])\. (.*)$", prompt.split("# 候補", 1)[-1], re.MULTILINE)
    return max(lines, key=lambda t: len(qg & _grams(t[1])))[0] if lines else "A"


def decide_logprobs(text: str) -> list[dict]:
    """採点の出力をトークン列にし、数字のトークンに上位候補の確率を付ける。"""
    import math
    toks = []
    for line in text.splitlines():
        num, digit = line.split("=")
        d = int(digit)
        alt = d - 1 if d > 0 else 1
        toks += [{"token": num, "logprob": -0.01}, {"token": "=", "logprob": -0.01},
                 {"token": digit, "logprob": math.log(0.7),
                  "top_logprobs": [{"token": digit, "logprob": math.log(0.7)},
                                   {"token": str(alt), "logprob": math.log(0.3)}]},
                 {"token": "\n", "logprob": -0.01}]
    return toks


def choice_logprobs(letter: str) -> list[dict]:
    import math
    other = "B" if letter == "A" else "A"
    return [{"token": letter, "logprob": math.log(0.8),
             "top_logprobs": [{"token": letter, "logprob": math.log(0.8)},
                              {"token": other, "logprob": math.log(0.2)}]}]


def jev_answer(body: dict) -> dict:
    """OpenRouter Decisions API（Jev）のモック。"""
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


REQUESTS: list[dict] = []


def openai_decision_answer(body: dict) -> dict:
    """OpenAI / Azure OpenAI の Decisions API のモック（応答の形は公式 SDK の Decision 型に合わせる）。

    predicate は質問と候補の文字の重なりの割合、choice はその正規化。候補の本文に REFUSE を含むと
    その候補の判定は refusal（回答拒否）になる。
    """
    text = str(body.get("input", ""))
    m = re.search(r"Query:\n(.*?)\n\nCandidates:\n(.*)$", text, re.S)
    qg = _grams(m.group(1) if m else "")
    cands = dict(re.findall(r"^(\d+)\. (.*)$", m.group(2) if m else "", re.MULTILINE))

    def rel(t):
        return min(1.0, len(qg & _grams(t)) / max(1, len(qg)) * 1.6)
    answers = []
    for q in body.get("questions") or []:
        name = q.get("name")
        if q.get("type") == "predicate":
            cand = cands.get(str(name or "").lstrip("c"), "")
            if "REFUSE" in cand:
                answers.append({"type": "refusal", "name": name})
            else:
                answers.append({"type": "predicate", "name": name, "probability": round(rel(cand), 3)})
        elif q.get("type") == "choice":
            raw = [(c["value"], rel(str(c.get("description", ""))) + 0.01) for c in q.get("choices") or []]
            total = sum(v for _, v in raw) or 1.0
            probs = [{"value": v, "probability": p / total} for v, p in raw]
            best = max(probs, key=lambda x: x["probability"])
            answers.append({"type": "choice", "name": name, "choice": best["value"],
                            "confidence": best["probability"], "probabilities": probs})
    n = len(text)
    return {"answers": answers, "model": body.get("model", ""),
            "usage": {"input_tokens": n, "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
                      "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0},
                      "total_tokens": n}}


class MockLLMHandler(BaseHTTPRequestHandler):
    """OpenAI 互換の /v1/chat/completions と /v1/embeddings を提供する。"""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        REQUESTS.append({"path": self.path, "body": body,
                         "auth": self.headers.get("Authorization"), "api_key": self.headers.get("api-key")})
        if self.path.endswith("/chat/completions"):
            prompt = ""
            for msg in body.get("messages", []):
                if msg.get("role") == "user":
                    prompt = msg.get("content", "")
            text = mock_chat_response(prompt)
            choice = {"message": {"role": "assistant", "content": "<think>考え中…</think>" + text}}
            if body.get("logprobs") and "[TASK:decide]" in prompt:
                choice["logprobs"] = {"content": [{"token": "<think>考え中…</think>", "logprob": 0}]
                                      + decide_logprobs(text)}
            elif body.get("logprobs") and "[TASK:decide_choice]" in prompt:
                choice["logprobs"] = {"content": [{"token": "<think>考え中…</think>", "logprob": 0}]
                                      + choice_logprobs(text)}
            out = {"choices": [choice]}
        elif self.path.split("?", 1)[0].endswith("/decisions"):
            out = openai_decision_answer(body) if "input" in body else jev_answer(body)
        elif self.path.endswith("/embeddings"):
            texts = body.get("input")
            if isinstance(texts, str):
                texts = [texts]
            out = {"data": [{"index": i, "embedding": embed_vector(t)}
                            for i, t in enumerate(texts)]}
        else:
            self.send_error(404)
            return
        data = json.dumps(out).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 静かに
        pass


def start_mock_llm() -> tuple[ThreadingHTTPServer, str]:
    """モック LLM サーバーを空きポートで起動し、(server, base_url) を返す。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockLLMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


class FakeLLMClient:
    """HTTP を介さないフェイク（エンジン単体テスト用）。LLMClient と同じ形。"""

    def __init__(self, *, with_embeddings: bool = True):
        self.with_embeddings = with_embeddings
        self.stats = {"chat_calls": 0, "chat_prompt_chars": 0,
                      "chat_completion_chars": 0, "embed_calls": 0,
                      "embed_texts": 0, "llm_seconds": 0.0}

    def chat(self, prompt, *, system="", max_tokens=None, temperature=0.0,
             want_think=False):
        self.stats["chat_calls"] += 1
        text = mock_chat_response(prompt)
        if want_think:
            return text, "（テスト用の思考過程）"
        return text

    def chat_logprobs(self, prompt, *, system="", max_tokens=None):
        self.stats["chat_calls"] += 1
        text = mock_chat_response(prompt)
        if "[TASK:decide]" in prompt:
            return text, decide_logprobs(text)
        if "[TASK:decide_choice]" in prompt:
            return text, choice_logprobs(text)
        return text, None

    def embed(self, texts):
        if not self.with_embeddings:
            from ragcore.llm import LLMError
            raise LLMError("埋め込み未設定（テスト）")
        self.stats["embed_calls"] += 1
        self.stats["embed_texts"] += len(texts)
        return [embed_vector(t) for t in texts]
