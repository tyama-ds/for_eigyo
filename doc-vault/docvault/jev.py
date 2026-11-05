"""判定モデル Jev（TypeSafe）のクライアント — OpenRouter Decisions API 経由。

DecisionRAG 論文（arXiv:2610.35868）が判定に使ったモデル。テキストを生成せず、
型付きの質問に確率で答える:

  - noul  : yes/no の文が真である確率（0〜1）。候補ごとの「関連するか」の二値判定に使う
  - choice: 選択肢それぞれの確率（合計 1）。最後の絞り込み（どれが最も関連するか）に使う

1リクエストの質問は同じ ``state``（質問文と番号つき候補一覧）を共有して並列に答えられる
（＝論文の「候補を同じ文脈で一度に判定する」設定そのもの）。

    POST {endpoint}            既定 https://openrouter.ai/api/alpha/decisions
    Authorization: Bearer <OpenRouter の API キー>
    {"model": "typesafe/jev-1.14", "state": {...},
     "questions": {"c1": {"type": "noul", "instructions": ..., "criteria": {"true": ..., "false": ...}}}}
    → {"answers": {"c1": {"type": "noul", "noul": 0.93}}, "usage": {"cost": 0.0001}}

Jev は外部のクラウドサービスなので、**公開文書の候補だけ**を送る（呼び出し側で区分を確認し、
ここでも sensitivity を検査する）。API の形は公開実装（lingduoduo/LLM-Notebooks の
Jev DecisionRAG 設計書、VectifyAI/jev-doc-search）に合わせた。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from .llm import LLMError, PolicyError

DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.14"      # 1.14 は多言語（日本語）の改善をうたう版
MAX_PER_REQUEST = 50                     # 論文で共有文脈の効果が頭打ちになる候補数
MAX_CHOICE = 20                          # choice の選択肢（論文の m）

BINARY_INSTRUCTIONS = ("Does candidate {n} (or the content under it) contain information that "
                       "helps answer the query?")
BINARY_TRUE = ("Candidate {n}, or the documents/passages under it, likely contain information "
               "that answers the query.")
BINARY_FALSE = ("Candidate {n} is unrelated to the query, or mentions the topic only in a negated "
                "or incidental way.")
CHOICE_INSTRUCTIONS = "Which candidate most likely contains the answer to the query?"


def ready(cfg: dict) -> bool:
    j = cfg.get("jev") or {}
    return bool(j.get("api_key") and j.get("model") and j.get("endpoint"))


def _state(query: str, descs: list[str]) -> dict:
    return {"query": query,
            "candidates": "\n".join(f"{i}. {d}" for i, d in enumerate(descs, 1))}


def build_binary_request(model: str, query: str, descs: list[str]) -> dict:
    questions = {}
    for i in range(1, len(descs) + 1):
        questions[f"c{i}"] = {
            "type": "noul",
            "instructions": BINARY_INSTRUCTIONS.format(n=i),
            "criteria": {"true": BINARY_TRUE.format(n=i), "false": BINARY_FALSE.format(n=i)},
        }
    return {"model": model, "state": _state(query, descs), "questions": questions}


def build_choice_request(model: str, query: str, descs: list[str]) -> dict:
    return {"model": model, "state": _state(query, descs),
            "questions": {"most_relevant": {
                "type": "choice", "instructions": CHOICE_INSTRUCTIONS,
                "criteria": {f"c{i}": d[:600] for i, d in enumerate(descs, 1)}}}}


def parse_binary(resp: dict, n: int) -> dict[int, float]:
    answers = resp.get("answers")
    if not isinstance(answers, dict):
        raise LLMError(f"Jev の応答に answers がありません: {str(resp)[:200]}")
    out = {}
    for i in range(1, n + 1):
        ans = answers.get(f"c{i}")
        if not isinstance(ans, dict) or not isinstance(ans.get("noul"), (int, float)):
            raise LLMError(f"Jev の応答に c{i} の判定がありません")
        out[i] = max(0.0, min(1.0, float(ans["noul"])))
    return out


def parse_choice(resp: dict, n: int) -> dict[int, float]:
    ans = (resp.get("answers") or {}).get("most_relevant")
    if not isinstance(ans, dict):
        raise LLMError("Jev の応答に most_relevant がありません")
    probs = ans.get("probabilities") or {}
    out = {}
    for i in range(1, n + 1):
        p = probs.get(f"c{i}")
        out[i] = float(p) if isinstance(p, (int, float)) else 0.0
    if not any(out.values()) and isinstance(ans.get("choice"), str):
        try:
            out[int(ans["choice"].lstrip("c"))] = 1.0
        except ValueError:
            pass
    return out


class JevClient:
    def __init__(self, jcfg: dict):
        self.cfg = jcfg
        self.calls = 0
        self.cost = 0.0

    def _opener(self):
        if not self.cfg.get("use_proxy"):
            handler = urllib.request.ProxyHandler({})
        elif self.cfg.get("proxy_url"):
            handler = urllib.request.ProxyHandler({"http": self.cfg["proxy_url"],
                                                   "https": self.cfg["proxy_url"]})
        else:
            handler = urllib.request.ProxyHandler()
        return urllib.request.build_opener(handler)

    def post(self, body: dict, *, sensitivity: str) -> dict:
        if sensitivity != "public":
            raise PolicyError("社外秘（internal）の候補は Jev（外部サービス）へ送信できません")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.cfg.get("endpoint") or DEFAULT_ENDPOINT, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.cfg.get('api_key', '')}"})
        last: Exception | None = None
        for attempt in range(4):                       # 429 / 5xx / 通信断は再試行
            try:
                with self._opener().open(req, timeout=float(self.cfg.get("timeout") or 60)) as res:
                    resp = json.loads(res.read().decode("utf-8", errors="replace"))
                self.calls += 1
                cost = (resp.get("usage") or {}).get("cost")
                if isinstance(cost, (int, float)):
                    self.cost += float(cost)
                return resp
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:300]
                except OSError:
                    pass
                last = LLMError(f"Jev API エラー HTTP {e.code}: {detail or e.reason}")
                if e.code != 429 and e.code < 500:
                    raise last from e
            except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
                last = LLMError(f"Jev API に接続できません: {e}")
            if attempt < 3:
                time.sleep(min(4.0, 0.5 * 2 ** attempt))
        raise last if last else LLMError("Jev API に接続できません")

    def binary(self, query: str, descs: list[str], *, sensitivity: str) -> dict[int, float]:
        out: dict[int, float] = {}
        for start in range(0, len(descs), MAX_PER_REQUEST):
            part = descs[start:start + MAX_PER_REQUEST]
            resp = self.post(build_binary_request(self.cfg["model"], query, part),
                             sensitivity=sensitivity)
            for i, p in parse_binary(resp, len(part)).items():
                out[start + i] = p
        return out

    def choice(self, query: str, descs: list[str], *, sensitivity: str) -> dict[int, float]:
        descs = descs[:MAX_CHOICE]
        resp = self.post(build_choice_request(self.cfg["model"], query, descs),
                         sensitivity=sensitivity)
        return parse_choice(resp, len(descs))
