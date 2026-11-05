"""DecisionRAG の判定 API クライアント — テキストを生成せず、質問に確率で答える「判定モデル」の API。

DecisionRAG（arXiv:2610.35868）の各段の判定（候補ごとに「関連するか」の確率）と、最後の選択式の
並べ替え（どれが最も答えを含むか）に使う。接続先は3つで、要求・応答の形はそれぞれ違うが使い方は同じ:

=========== ================================================= ============ ============= ========= =======
provider    送り先                                            認証         モデル        二値判定  選択式
=========== ================================================= ============ ============= ========= =======
openrouter  https://openrouter.ai/api/alpha/decisions          Bearer       typesafe/jev  noul      choice
openai      https://api.openai.com/v1/decisions                Bearer       gpt-6-luna    predicate choice
azure       {endpoint}/openai/v1/decisions                     api-key      デプロイ名    predicate choice
            （api-version を指定したら /openai/decisions?api-version=…）
=========== ================================================= ============ ============= ========= =======

- OpenRouter（Jev）: 候補一覧を ``state`` に入れ、質問は名前つきの辞書。答えも名前で返る。
  形は公開実装（lingduoduo/LLM-Notebooks の Jev DecisionRAG 設計書、VectifyAI/jev-doc-search）に合わせた
- OpenAI / Azure OpenAI: 候補一覧を ``input`` に入れ、質問は配列（1〜400 件）。答えは質問の順の配列で、
  どの質問も ``refusal``（回答拒否）になりうる。形は公式 Python SDK（openai 3.46 の
  DecisionCreateParams / Decision、Azure の URL は AzureOpenAI クライアント）に合わせた

外部サービスなので、既定では**公開文書の候補だけ**を送る。Azure OpenAI だけは、社内で社外秘の扱いが
認められた自社テナントのことがあるため、設定で「社外秘も送ってよい」を ON にしたときに限って社外秘の
候補も送る（ローカル LLM と同じ扱い）。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .llm import LLMError, PolicyError

PROVIDERS: dict[str, dict] = {
    "openrouter": {"label": "Jev（OpenRouter）",
                   "endpoint": "https://openrouter.ai/api/alpha/decisions",
                   "model": "typesafe/jev-1.14"},      # 1.14 は多言語（日本語）の改善をうたう版
    "openai": {"label": "OpenAI", "endpoint": "https://api.openai.com/v1/decisions",
               "model": "gpt-6-luna"},                  # 2026-11 時点で Decisions API の唯一のモデル
    "azure": {"label": "Azure OpenAI", "endpoint": "", "model": ""},   # リソースの URL とデプロイ名
}
MAX_PER_REQUEST = 50                     # 論文で共有文脈の効果が頭打ちになる候補数（OpenAI の上限は 400）
MAX_CHOICE = 20                          # choice の選択肢（論文の m）

BINARY_INSTRUCTIONS = ("Does candidate {n} (or the content under it) contain information that "
                       "helps answer the query?")
BINARY_TRUE = ("Candidate {n}, or the documents/passages under it, likely contain information "
               "that answers the query.")
BINARY_FALSE = ("Candidate {n} is unrelated to the query, or mentions the topic only in a negated "
                "or incidental way.")
# OpenAI の predicate は「入力についての文が真である確率」を答える。真と偽の基準を1文にまとめる
PREDICATE = ("Candidate {n}, or the documents/passages under it, contains information that helps "
             "answer the query. Treat it as false if candidate {n} is unrelated to the query, or "
             "mentions the topic only in a negated or incidental way.")
CHOICE_INSTRUCTIONS = "Which candidate most likely contains the answer to the query?"


# ---------------------------------------------------------------- 設定

def effective(cfg: dict) -> dict:
    """設定の decision_api から、選んでいる接続先の値（空欄は既定値で補う）を1つの辞書にする。"""
    api = cfg.get("decision_api") or {}
    provider = api.get("provider") if api.get("provider") in PROVIDERS else "openai"
    sub = api.get(provider) or {}
    defaults = PROVIDERS[provider]
    return {
        "provider": provider,
        "label": defaults["label"],
        "endpoint": (sub.get("endpoint") or "").strip() or defaults["endpoint"],
        "api_key": (sub.get("api_key") or "").strip(),
        "model": (sub.get("model") or "").strip() or defaults["model"],
        "api_version": (sub.get("api_version") or "").strip() if provider == "azure" else "",
        "allow_internal": bool(sub.get("allow_internal")) if provider == "azure" else False,
        "use_proxy": bool(sub.get("use_proxy", True)),
        "proxy_url": (sub.get("proxy_url") or "").strip(),
        "timeout": float(api.get("timeout") or 60),
    }


def ready(cfg: dict) -> bool:
    e = effective(cfg)
    return bool(e["api_key"] and e["model"] and e["endpoint"])


def missing(cfg: dict) -> str:
    """未設定の項目（UI・警告用）。揃っていれば空。"""
    e = effective(cfg)
    names = [n for n, ok in (("エンドポイント", e["endpoint"]), ("API キー", e["api_key"]),
                             ("デプロイ名" if e["provider"] == "azure" else "モデル", e["model"]))
             if not ok]
    return "・".join(names)


def request_url(e: dict) -> str:
    """送り先の URL。Azure はリソースの URL（https://<res>.openai.azure.com）から組み立てる。"""
    ep = e["endpoint"]
    if e["provider"] != "azure":
        return ep
    ver = e.get("api_version") or ""
    path = ep.split("?", 1)[0].rstrip("/")
    if path.endswith("/decisions"):                  # /decisions まで含む URL を貼った場合はそのまま
        url = ep
    else:
        for suffix in ("/openai/v1", "/openai"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        # api-version なしは Azure の v1 API、ありは従来の api-version つきの形（SDK と同じ）
        url = f"{path}/openai/decisions" if ver else f"{path}/openai/v1/decisions"
    if ver and "api-version=" not in url:
        url += ("&" if "?" in url else "?") + "api-version=" + urllib.parse.quote(ver)
    return url


def request_headers(e: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if e["provider"] == "azure":
        headers["api-key"] = e["api_key"]
    else:
        headers["Authorization"] = f"Bearer {e['api_key']}"
    return headers


# ---------------------------------------------------------------- 要求の形

def _state(query: str, descs: list[str]) -> dict:
    return {"query": query,
            "candidates": "\n".join(f"{i}. {d}" for i, d in enumerate(descs, 1))}


def _input_text(query: str, descs: list[str]) -> str:
    return ("Query:\n" + query + "\n\nCandidates:\n"
            + "\n".join(f"{i}. {d}" for i, d in enumerate(descs, 1)))


def build_binary_request(provider: str, model: str, query: str, descs: list[str]) -> dict:
    """候補ごとの「関連するか」を、同じ文脈（質問＋候補一覧）を共有して1リクエストで聞く。"""
    if provider == "openrouter":
        questions = {f"c{i}": {"type": "noul", "instructions": BINARY_INSTRUCTIONS.format(n=i),
                               "criteria": {"true": BINARY_TRUE.format(n=i),
                                            "false": BINARY_FALSE.format(n=i)}}
                     for i in range(1, len(descs) + 1)}
        return {"model": model, "state": _state(query, descs), "questions": questions}
    return {"model": model, "input": _input_text(query, descs),
            "questions": [{"type": "predicate", "name": f"c{i}", "instructions": PREDICATE.format(n=i)}
                          for i in range(1, len(descs) + 1)]}


def build_choice_request(provider: str, model: str, query: str, descs: list[str]) -> dict:
    if provider == "openrouter":
        return {"model": model, "state": _state(query, descs),
                "questions": {"most_relevant": {
                    "type": "choice", "instructions": CHOICE_INSTRUCTIONS,
                    "criteria": {f"c{i}": d[:600] for i, d in enumerate(descs, 1)}}}}
    return {"model": model, "input": _input_text(query, descs),
            "questions": [{"type": "choice", "name": "most_relevant", "instructions": CHOICE_INSTRUCTIONS,
                           "choices": [{"value": f"c{i}", "description": d[:600]}
                                       for i, d in enumerate(descs, 1)]}]}


# ---------------------------------------------------------------- 応答の読み取り

def _cand_no(value) -> int | None:
    if isinstance(value, str) and value.startswith("c") and value[1:].isdigit():
        return int(value[1:])
    return None


def _answer_list(resp: dict, n: int) -> list:
    """OpenAI / Azure の answers（質問の順の配列）を、名前が c1..cn の順に並べ直す。"""
    answers = resp.get("answers")
    if not isinstance(answers, list):
        raise LLMError(f"判定 API の応答に answers がありません: {str(resp)[:200]}")
    by_name = {a.get("name"): a for a in answers if isinstance(a, dict) and a.get("name")}
    out = []
    for i in range(1, n + 1):
        a = by_name.get(f"c{i}")
        if a is None and i - 1 < len(answers) and isinstance(answers[i - 1], dict) \
                and not answers[i - 1].get("name"):
            a = answers[i - 1]                       # 名前が返らないときは順番で対応づける
        out.append(a)
    return out


def parse_binary(provider: str, resp: dict, n: int) -> tuple[dict[int, float], int]:
    """({候補番号: 関連する確率}, 回答を断られた数)。"""
    out: dict[int, float] = {}
    refused = 0
    if provider == "openrouter":
        answers = resp.get("answers")
        if not isinstance(answers, dict):
            raise LLMError(f"Jev の応答に answers がありません: {str(resp)[:200]}")
        for i in range(1, n + 1):
            ans = answers.get(f"c{i}")
            if not isinstance(ans, dict) or not isinstance(ans.get("noul"), (int, float)):
                raise LLMError(f"Jev の応答に c{i} の判定がありません")
            out[i] = max(0.0, min(1.0, float(ans["noul"])))
        return out, 0
    for i, ans in enumerate(_answer_list(resp, n), 1):
        if isinstance(ans, dict) and ans.get("type") == "refusal":
            refused += 1
            out[i] = 0.0
            continue
        if not isinstance(ans, dict) or not isinstance(ans.get("probability"), (int, float)):
            raise LLMError(f"判定 API の応答に c{i} の判定がありません")
        out[i] = max(0.0, min(1.0, float(ans["probability"])))
    if n and refused == n:
        raise LLMError("判定 API がすべての候補の判定を断りました（refusal）")
    return out, refused


def parse_choice(provider: str, resp: dict, n: int) -> dict[int, float]:
    out = {i: 0.0 for i in range(1, n + 1)}
    if provider == "openrouter":
        ans = (resp.get("answers") or {}).get("most_relevant")
        if not isinstance(ans, dict):
            raise LLMError("Jev の応答に most_relevant がありません")
        probs = ans.get("probabilities") or {}
        for i in range(1, n + 1):
            p = probs.get(f"c{i}") if isinstance(probs, dict) else None
            out[i] = float(p) if isinstance(p, (int, float)) else 0.0
    else:
        answers = resp.get("answers")
        if not isinstance(answers, list) or not answers:
            raise LLMError(f"判定 API の応答に answers がありません: {str(resp)[:200]}")
        ans = next((a for a in answers if isinstance(a, dict) and a.get("name") == "most_relevant"),
                   answers[0])
        if not isinstance(ans, dict) or ans.get("type") == "refusal":
            raise LLMError("判定 API が選択式の判定を断りました（refusal）")
        for item in ans.get("probabilities") or []:
            k = _cand_no(item.get("value")) if isinstance(item, dict) else None
            if k in out and isinstance(item.get("probability"), (int, float)):
                out[k] = float(item["probability"])
    if not any(out.values()):                        # 確率が無ければ選ばれた1つを 1.0 にする
        k = _cand_no(ans.get("choice"))
        if k in out:
            out[k] = 1.0
    return out


# ---------------------------------------------------------------- クライアント

class DecisionAPIClient:
    def __init__(self, e: dict):
        self.cfg = e
        self.provider = e["provider"]
        self.label = e["label"]
        self.calls = 0
        self.cost = 0.0              # OpenRouter の usage.cost（米ドル）
        self.input_tokens = 0        # OpenAI / Azure の usage.input_tokens（出力は課金されない）
        self.refusals = 0

    @property
    def trusted(self) -> bool:
        """社外秘も送ってよい接続先か（Azure で設定を ON にしたときだけ）。"""
        return self.provider == "azure" and bool(self.cfg.get("allow_internal"))

    def allows(self, sensitivity: str) -> bool:
        return sensitivity == "public" or self.trusted

    def _opener(self):
        if not self.cfg.get("use_proxy"):
            handler = urllib.request.ProxyHandler({})
        elif self.cfg.get("proxy_url"):
            handler = urllib.request.ProxyHandler({"http": self.cfg["proxy_url"],
                                                   "https": self.cfg["proxy_url"]})
        else:
            handler = urllib.request.ProxyHandler()  # 環境変数、無ければ Windows のシステム設定
        return urllib.request.build_opener(handler)

    def post(self, body: dict, *, sensitivity: str) -> dict:
        if not self.allows(sensitivity):
            raise PolicyError(f"社外秘（internal）の候補は {self.label}（外部サービス）へ送信できません")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(request_url(self.cfg), data=data, method="POST",
                                     headers=request_headers(self.cfg))
        last: Exception | None = None
        for attempt in range(4):                       # 429 / 5xx / 通信断は再試行
            try:
                with self._opener().open(req, timeout=float(self.cfg.get("timeout") or 60)) as res:
                    resp = json.loads(res.read().decode("utf-8", errors="replace"))
                if not isinstance(resp, dict):
                    raise ValueError("応答が JSON のオブジェクトではありません")
                self.calls += 1
                usage = resp.get("usage") or {}
                if isinstance(usage.get("cost"), (int, float)):
                    self.cost += float(usage["cost"])
                if isinstance(usage.get("input_tokens"), int):
                    self.input_tokens += usage["input_tokens"]
                return resp
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:300]
                except OSError:
                    pass
                last = LLMError(f"{self.label} の判定 API エラー HTTP {e.code}: {detail or e.reason}")
                if e.code != 429 and e.code < 500:
                    raise last from e
            except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
                last = LLMError(f"{self.label} の判定 API に接続できません: {e}")
            if attempt < 3:
                time.sleep(min(4.0, 0.5 * 2 ** attempt))
        raise last if last else LLMError(f"{self.label} の判定 API に接続できません")

    def binary(self, query: str, descs: list[str], *, sensitivity: str) -> dict[int, float]:
        out: dict[int, float] = {}
        for start in range(0, len(descs), MAX_PER_REQUEST):
            part = descs[start:start + MAX_PER_REQUEST]
            resp = self.post(build_binary_request(self.provider, self.cfg["model"], query, part),
                             sensitivity=sensitivity)
            probs, refused = parse_binary(self.provider, resp, len(part))
            self.refusals += refused
            for i, p in probs.items():
                out[start + i] = p
        return out

    def choice(self, query: str, descs: list[str], *, sensitivity: str) -> dict[int, float]:
        descs = descs[:MAX_CHOICE]
        if len(descs) < 2:                             # OpenAI の choice は選択肢 2 件以上
            return {1: 1.0} if descs else {}
        resp = self.post(build_choice_request(self.provider, self.cfg["model"], query, descs),
                         sensitivity=sensitivity)
        return parse_choice(self.provider, resp, len(descs))

    def usage_text(self) -> str:
        parts = [f"{self.calls} 回"]
        if self.cost:
            parts.append(f"${self.cost:.6f}")
        if self.input_tokens:
            parts.append(f"入力 {self.input_tokens:,} トークン")
        if self.refusals:
            parts.append(f"判定を断られた候補 {self.refusals} 件")
        return "・".join(parts)


def connection_test(cfg: dict) -> dict:
    """接続テスト: 公開してよい架空の候補2件で二値判定を1回だけ行う。"""
    if not ready(cfg):
        return {"ok": False, "message": f"未設定: {missing(cfg)}"}
    e = effective(cfg)
    client = DecisionAPIClient(e)
    started = time.time()
    try:
        probs = client.binary("富士山の標高は？",
                              ["富士山: 日本で最も高い山。標高 3,776 m。",
                               "東京駅: 丸の内にある鉄道駅。1914 年開業。"], sensitivity="public")
    except LLMError as err:
        return {"ok": False, "message": str(err), "url": request_url(e)}
    good = probs.get(1, 0.0) > probs.get(2, 0.0)
    return {"ok": True, "url": request_url(e),
            "message": (f"{e['label']}（{e['model']}）: 関連あり {probs.get(1, 0):.2f} / 関連なし "
                        f"{probs.get(2, 0):.2f}"
                        + ("" if good else "（期待と逆の判定。モデルや指示の確認を）")
                        + f"・{time.time() - started:.1f} 秒・{client.usage_text()}")}
