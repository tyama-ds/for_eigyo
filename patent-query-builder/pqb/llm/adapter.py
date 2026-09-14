"""LLM アダプタ。企画書 §8.3・§9.4。

3 モード（同じプロンプト・同じ出力スキーマを使い、上位コードはモードを意識しない）
  manual : プロンプトをファイル／UI に出し、人が社内許可の LLM 画面に貼って返答 JSON を貼り戻す
  api    : OpenAI 互換／Anthropic のエンドポイントを呼ぶ（プロキシと社内 CA を設定可）
  browser: 開発・検証のみ。production=true では起動できない（本実装ではフックのみ）
  mock   : オフライン用ヒューリスティック（pqb.llm.mock）。offline=true の既定

共通: JSON スキーマ検証、リトライ（最大 2 回）、n サンプル多数決、フリップ率、
マスキング、送信前確認（production）、llm_calls への記録。
ネットワーク呼び出しはこのモジュールと pqb.db.adapter に限定する。
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..util import canonical_json, new_id, now_iso, sha256_text
from . import mock as mockllm
from .schema import validate

PROMPT_DIR = Path(__file__).with_name("prompts")
SCHEMA_DIR = Path(__file__).with_name("schemas")
PROMPT_FILES = {"P1": "P1_structure.md", "P2": "P2_expand.md", "P3": "P3_classify.md",
                "P4": "P4_judge.md", "P5": "P5_transform.md", "P6": "P6_report.md"}

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """接続・API・スキーマのエラー。UI にそのまま表示できる日本語メッセージ。"""


class PendingManualResponse(Exception):
    """manual モード: 人の返答待ち。UI／CLI はプロンプトを提示して返答を受け取る。"""

    def __init__(self, prompts: list[dict]):
        super().__init__(f"{len(prompts)} 件の LLM 返答待ち（manual モード）")
        self.prompts = prompts


class PendingConfirmation(Exception):
    """production: 外部送信前の人の確認待ち。"""

    def __init__(self, prompt_id: str, prompt_text: str, masked: bool):
        super().__init__("外部送信前の確認が必要です")
        self.prompt_id, self.prompt_text, self.masked = prompt_id, prompt_text, masked


@dataclass
class LLMResult:
    prompt_id: str
    mode: str
    model: str
    samples: list = field(default_factory=list)
    majority: dict = field(default_factory=dict)
    flip_rate: float = 0.0
    needs_review: bool = False
    n: int = 1
    call_id: str = ""
    low_confidence: bool = False
    duration_ms: int = 0


# ------------------------------------------------------------------ テンプレート / JSON 抽出

def load_prompt_template(prompt_id: str) -> str:
    return (PROMPT_DIR / PROMPT_FILES[prompt_id]).read_text(encoding="utf-8")


def load_schema(prompt_id: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{prompt_id}.json").read_text(encoding="utf-8"))


def render_prompt(prompt_id: str, inputs: dict) -> str:
    text = load_prompt_template(prompt_id)

    def repl(m: re.Match) -> str:
        key = m.group(1)
        val = inputs.get(key, "")
        if isinstance(val, str):
            return val if val else "（なし）"
        return json.dumps(val, ensure_ascii=False, indent=1) if val not in (None, [], {}) else "（なし）"
    return re.sub(r"\[\[(\w+)\]\]", repl, text)


def extract_json(text: str) -> dict:
    text = _THINK_RE.sub("", text or "").strip()
    cands = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    for cand in cands:
        cand = cand.strip()
        start, end = cand.find("{"), cand.rfind("}")
        if start == -1 or end == -1 or end <= start:
            continue
        try:
            data = json.loads(cand[start:end + 1])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise LLMError("返答から JSON オブジェクトを取り出せませんでした")


# ------------------------------------------------------------------ マスキング

class Masker:
    """社名・人名・具体数値をトークンに置換し、復元表を SQLite に保存する。"""

    def __init__(self, case_id: str, store, mask_terms: list[str] | None = None, mask_numbers: bool = False):
        self.case_id, self.store = case_id, store
        self.table: dict[str, str] = {}     # token -> original
        self.kinds: dict[str, str] = {}
        counters: Counter = Counter()
        for entry in mask_terms or []:
            entry = str(entry).strip()
            if not entry:
                continue
            kind, _, orig = entry.partition(":")
            if not orig:
                kind, orig = "固有名詞", kind
            counters[kind] += 1
            token = f"【{kind}{counters[kind]}】"
            self.table[token] = orig
            self.kinds[token] = kind
        self.mask_numbers = mask_numbers
        self._num_counter = 0

    def mask(self, text: str) -> str:
        for token, orig in sorted(self.table.items(), key=lambda kv: -len(kv[1])):
            text = text.replace(orig, token)
        if self.mask_numbers:
            def repl(m: re.Match) -> str:
                self._num_counter += 1
                token = f"【数値{self._num_counter}】"
                self.table[token] = m.group(0)
                self.kinds[token] = "数値"
                return token
            text = re.sub(r"(?<![A-Za-z0-9/@,])\d{2,}(?:\.\d+)?", repl, text)
        if self.store is not None and self.table:
            self.store.save_masking(self.case_id, [(t, o, self.kinds.get(t, "")) for t, o in self.table.items()])
        return text

    def unmask(self, obj):
        if isinstance(obj, str):
            for token, orig in self.table.items():
                obj = obj.replace(token, orig)
            return obj
        if isinstance(obj, list):
            return [self.unmask(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.unmask(v) for k, v in obj.items()}
        return obj

    @property
    def active(self) -> bool:
        return bool(self.table) or self.mask_numbers


# ------------------------------------------------------------------ 多数決

def vote_key(prompt_id: str, sample: dict) -> str:
    if prompt_id == "P4":
        return str(sample.get("overall"))
    if prompt_id == "P3":
        return canonical_json(sorted({(c.get("scheme"), c.get("code")) for c in sample.get("codes", [])}))
    return canonical_json(sample)


def majority_vote(prompt_id: str, samples: list[dict]) -> tuple[dict, float]:
    counts = Counter(vote_key(prompt_id, s) for s in samples)
    top_key, top_n = counts.most_common(1)[0]
    flip = 1.0 - top_n / len(samples)
    chosen = next(s for s in samples if vote_key(prompt_id, s) == top_key)
    if prompt_id == "P4" and len(samples) > 1:
        # 観点別も多数決で合成
        per_axis: dict[str, int] = {}
        keys = {k for s in samples for k in (s.get("per_axis") or {})}
        for k in keys:
            vals = Counter(s.get("per_axis", {}).get(k) for s in samples if k in s.get("per_axis", {}))
            per_axis[k] = vals.most_common(1)[0][0]
        chosen = dict(chosen)
        chosen["per_axis"] = per_axis
    return chosen, round(flip, 4)


def code_support(samples: list[dict]) -> dict[tuple[str, str, str], float]:
    """P3: (axis_id, scheme, code) → サンプル中に現れた割合。"""
    n = len(samples) or 1
    counts: Counter = Counter()
    for s in samples:
        seen = set()
        for c in s.get("codes", []):
            key = (c.get("axis_id", ""), c.get("scheme", ""), c.get("code", ""))
            if key not in seen:
                seen.add(key)
                counts[key] += 1
    return {k: v / n for k, v in counts.items()}


# ------------------------------------------------------------------ アダプタ

class LLMAdapter:
    def __init__(self, cfg: dict, store=None, case_id: str = "", masker: Masker | None = None,
                 prompts_dir: Path | None = None):
        self.cfg = cfg
        self.llm = cfg.get("llm") or {}
        self.store = store
        self.case_id = case_id
        self.masker = masker
        self.mode = self._resolve_mode()
        self.prompts_dir = prompts_dir

    def _resolve_mode(self) -> str:
        mode = (self.llm.get("mode") or "mock").lower()
        if self.cfg.get("offline") and mode in ("api", "browser"):
            return "mock"
        if mode == "browser" and self.cfg.get("production"):
            raise LLMError("production=true では browser モードを起動できません（企画書 §6.2・§14.2）")
        return mode

    # -------------------------------------------------------------- 公開 API
    def complete(self, prompt_id: str, inputs: dict, n: int | None = None, confirmed: bool = False) -> LLMResult:
        if prompt_id not in PROMPT_FILES:
            raise KeyError(prompt_id)
        n = n or int((self.llm.get("n_samples") or {}).get(prompt_id, 1))
        budget = int((self.cfg.get("budget") or {}).get("llm_calls") or 0)
        if self.store is not None and budget and self.case_id and self.store.count_llm_calls(self.case_id) >= budget:
            raise LLMError(f"LLM 呼び出し上限（{budget} 回）に達しました")
        prompt = render_prompt(prompt_id, inputs)
        masked = False
        if self.masker and self.masker.active:
            prompt = self.masker.mask(prompt)
            masked = True
        if self.cfg.get("production") and self.mode == "api" and not confirmed:
            raise PendingConfirmation(prompt_id, prompt, masked)
        schema = load_schema(prompt_id)
        input_hash = sha256_text(prompt)
        t0 = time.time()
        if self.mode == "manual":
            samples = self._manual_samples(prompt_id, prompt, input_hash, n, schema)
        else:
            samples = [self._one_sample(prompt_id, prompt, inputs, k, schema) for k in range(1, n + 1)]
        if self.masker and masked:
            samples = [self.masker.unmask(s) for s in samples]
        majority, flip = majority_vote(prompt_id, samples)
        flip_threshold = float(self.cfg.get("flip_threshold", 0.2))
        result = LLMResult(prompt_id=prompt_id, mode=self.mode, model=self._model_name(), samples=samples,
                           majority=majority, flip_rate=flip, needs_review=flip > flip_threshold, n=n,
                           call_id=new_id("llm-"), low_confidence=bool(majority.get("low_confidence")),
                           duration_ms=int((time.time() - t0) * 1000))
        if self.store is not None:
            self.store.add_llm_call({"call_id": result.call_id, "case_id": self.case_id, "prompt_id": prompt_id,
                                     "mode": self.mode, "model": result.model, "input_hash": input_hash,
                                     "n_samples": n, "masked": int(masked), "response_json": samples,
                                     "flip_rate": flip, "status": "ok", "duration_ms": result.duration_ms})
        return result

    def _model_name(self) -> str:
        if self.mode == "mock":
            return "mock"
        if self.mode == "manual":
            return "manual"
        return self.llm.get("model") or ""

    # -------------------------------------------------------------- 1 サンプル
    def _one_sample(self, prompt_id: str, prompt: str, inputs: dict, k: int, schema: dict) -> dict:
        retries = int(self.llm.get("max_retries", 2))
        last_err = ""
        for attempt in range(retries + 1):
            try:
                if self.mode == "mock":
                    data = mockllm.generate(prompt_id, inputs, k)
                elif self.mode == "api":
                    data = extract_json(self._call_api(prompt if attempt == 0 else
                                                       prompt + f"\n\n（前回の返答はスキーマ不一致: {last_err}。スキーマ通りの JSON だけを返してください）"))
                elif self.mode == "browser":
                    raise LLMError("browser モードは本実装では提供していません（開発用フックのみ。api か manual を使用）")
                else:
                    raise LLMError(f"未知の LLM モード: {self.mode}")
            except LLMError:
                raise
            errors = validate(data, schema)
            if not errors:
                return data
            last_err = "; ".join(errors[:3])
            if self.mode == "mock":
                break
        self._record_failure(prompt_id, prompt, last_err)
        raise LLMError(f"LLM 返答がスキーマに一致しません（{prompt_id}）: {last_err}")

    def _record_failure(self, prompt_id: str, prompt: str, err: str) -> None:
        if self.store is not None:
            self.store.add_llm_call({"case_id": self.case_id, "prompt_id": prompt_id, "mode": self.mode,
                                     "model": self._model_name(), "input_hash": sha256_text(prompt), "n_samples": 1,
                                     "masked": 0, "response_json": {"error": err}, "flip_rate": None,
                                     "status": "schema_error", "duration_ms": 0})

    # -------------------------------------------------------------- manual
    def _manual_samples(self, prompt_id: str, prompt: str, input_hash: str, n: int, schema: dict) -> list[dict]:
        samples, pending = [], []
        for k in range(1, n + 1):
            call_key = f"{self.case_id}:{prompt_id}:{input_hash[:12]}:{k}"
            text = self._manual_lookup(call_key, prompt_id, prompt, k)
            if text is None:
                pending.append({"call_key": call_key, "prompt_id": prompt_id, "sample_no": k, "prompt_text": prompt})
                continue
            data = extract_json(text)
            errors = validate(data, schema)
            if errors:
                if self.store is not None:
                    self.store.update("manual_prompts", {"call_key": call_key}, {"status": "pending", "response_text": None})
                raise LLMError(f"貼り付けた返答がスキーマに一致しません（{prompt_id} #{k}）: {'; '.join(errors[:3])}")
            samples.append(data)
        if pending:
            raise PendingManualResponse(pending)
        return samples

    def _manual_lookup(self, call_key: str, prompt_id: str, prompt: str, k: int) -> str | None:
        # 1) ストア（UI から貼り付け）
        if self.store is not None:
            row = self.store.get_manual_prompt(call_key)
            if row and row.get("status") == "answered" and row.get("response_text"):
                return row["response_text"]
        # 2) ファイル（CLI 運用: prompts_out に書き出し、prompts_in から読む）
        if self.prompts_dir is not None:
            safe = call_key.replace(":", "_").replace("/", "_")
            out_dir = self.prompts_dir / "prompts_out" / (self.case_id or "case")
            in_dir = self.prompts_dir / "prompts_in" / (self.case_id or "case")
            out_dir.mkdir(parents=True, exist_ok=True)
            in_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"{safe}.md"
            if not out_file.exists():
                out_file.write_text(prompt, encoding="utf-8")
            for ext in (".json", ".txt", ".md"):
                in_file = in_dir / f"{safe}{ext}"
                if in_file.exists():
                    return in_file.read_text(encoding="utf-8")
        if self.store is not None:
            self.store.save_manual_prompt(call_key, self.case_id, prompt_id, k, prompt)
        return None

    # -------------------------------------------------------------- api
    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers = []
        if not self.llm.get("use_proxy", True):
            handlers.append(urllib.request.ProxyHandler({}))
        elif self.llm.get("proxy_url"):
            handlers.append(urllib.request.ProxyHandler({"http": self.llm["proxy_url"], "https": self.llm["proxy_url"]}))
        ca = self.llm.get("ca_bundle")
        if ca:
            ctx = ssl.create_default_context(cafile=ca)
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener(*handlers)

    def _call_api(self, prompt: str) -> str:
        base = (self.llm.get("base_url") or "").strip().rstrip("/")
        model = (self.llm.get("model") or "").strip()
        provider = (self.llm.get("provider") or "openai_compat").lower()
        if not base or not model:
            raise LLMError("LLM の base_url と model を設定してください（⚙️ 設定）")
        timeout = float(self.llm.get("request_timeout", 120.0))
        max_tokens = int(self.llm.get("max_tokens", 2048))
        temperature = float(self.llm.get("temperature", 0.2))
        headers = {"Content-Type": "application/json"}
        key = self.llm.get("api_key") or ""
        if provider == "anthropic":
            url = base + ("/v1/messages" if not base.endswith("/v1") else "/messages")
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}]}
        else:
            url = base + "/chat/completions"
            if key:
                headers["Authorization"] = f"Bearer {key}"
            body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
                    "messages": [{"role": "system", "content": "指定された JSON スキーマ以外の出力は禁止。日本語で答える。"},
                                 {"role": "user", "content": prompt}]}
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with self._build_opener().open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"LLM API エラー HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(f"LLM API に接続できません: {e}") from e
        try:
            if provider == "anthropic":
                return "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
            msg = data["choices"][0]["message"]
            return msg.get("content") or msg.get("reasoning_content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM API の返答形式が想定外です: {str(data)[:200]}") from e

    def test_connection(self) -> dict:
        if self.mode != "api":
            return {"ok": True, "message": f"モード {self.mode}（API 呼び出しなし）"}
        try:
            text = self._call_api('{"ping": true} とだけ JSON で返答してください。')
            return {"ok": True, "message": text[:120]}
        except LLMError as e:
            return {"ok": False, "message": str(e)}
