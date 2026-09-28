"""AI 機能: ノートに質問（RAG）・要約・リンク候補・文章の書き換え。

検索（retrieval）は 2 段構え:
- キーワード: 文字バイグラムの BM25。日本語でも分かち書きなしで動き、LLM 設定がなくても使える
- 意味検索: 埋め込みモデルが設定され、インデックスを作ってあれば併用（RRF で統合）
"""
from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter

from . import links as L
from .config import chat_configured, embed_configured
from .llm import LLMClient, LLMError

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\s!-/:-@\[-`{-~、。「」『』（）！？・：；，．【】〈〉《》…―]+")


def bigrams(text: str) -> list[str]:
    """文字バイグラム（英数字の語はそのまま 1 トークン）。"""
    text = text.lower()
    out: list[str] = []
    for seg in _PUNCT_RE.split(text):
        if not seg:
            continue
        if seg.isascii():
            out.append(seg)
            continue
        if len(seg) == 1:
            out.append(seg)
        out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class _BM25:
    def __init__(self, docs: list[str], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(bigrams(d)) for d in docs]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 1.0
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: str) -> list[float]:
        q = set(bigrams(query))
        out = []
        for tf, ln in zip(self.tfs, self.lens):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            out.append(s)
        return out


class AIService:
    def __init__(self, index, get_config):
        self.index = index
        self.get_config = get_config
        self._bm25_cache: tuple[tuple, list[dict], _BM25] | None = None
        self._lock = threading.Lock()
        self.embed_status = {"state": "idle", "done": 0, "total": 0, "message": ""}

    # ------------------------------------------------------------ 状態
    def status(self) -> dict:
        cfg = self.get_config()
        chunks = self.index.chunks()
        embedded = 0
        if embed_configured(cfg):
            have = self.index.embeddings(cfg["embed_model"])
            embedded = sum(1 for c in chunks if c["hash"] in have)
        return {"chat": chat_configured(cfg), "embed": embed_configured(cfg),
                "chunks": len(chunks), "embedded": embedded,
                "retrieval": "hybrid" if embedded else "keyword",
                "job": dict(self.embed_status)}

    # ------------------------------------------------------------ 検索
    def _bm25(self) -> tuple[list[dict], _BM25]:
        with self._lock:
            key = (self.index.rev, self.get_config().get("template_folder", ""))
            if self._bm25_cache and self._bm25_cache[0] == key:
                return self._bm25_cache[1], self._bm25_cache[2]
            # テンプレートは中身が空欄の雛形なので検索対象から外す
            tpl = self.get_config().get("template_folder", "").strip().strip("/")
            chunks = [c for c in self.index.chunks()
                      if not (tpl and (c["path"].startswith(tpl + "/")))]
            bm = _BM25([f"{c['title']} {c['heading']}\n{c['text']}" for c in chunks])
            self._bm25_cache = (key, chunks, bm)
            return chunks, bm

    def retrieve(self, query: str, k: int = 6, exclude: set[str] | None = None) -> list[dict]:
        exclude = exclude or set()
        chunks, bm = self._bm25()
        if not chunks:
            return []
        kw = bm.scores(query)
        ranks: dict[int, float] = {}
        order = sorted((i for i in range(len(chunks)) if kw[i] > 0), key=lambda i: -kw[i])
        for r, i in enumerate(order[:50]):
            ranks[i] = ranks.get(i, 0.0) + 1.0 / (60 + r)
        cfg = self.get_config()
        if embed_configured(cfg):
            vecs = self.index.embeddings(cfg["embed_model"])
            if vecs:
                try:
                    qv = LLMClient(cfg).embed([query])[0]
                    sims = [(cosine(qv, vecs[c["hash"]]), i) for i, c in enumerate(chunks) if c["hash"] in vecs]
                    sims.sort(reverse=True)
                    for r, (_, i) in enumerate(sims[:50]):
                        ranks[i] = ranks.get(i, 0.0) + 1.0 / (60 + r)
                except LLMError:
                    pass                              # 意味検索が落ちてもキーワードで続行
        out = []
        for i in sorted(ranks, key=lambda i: -ranks[i]):
            c = chunks[i]
            if c["path"] in exclude:
                continue
            out.append({**c, "score": round(ranks[i], 5)})
            if len(out) >= k:
                break
        return out

    # ------------------------------------------------------------ 質問
    def ask(self, question: str, path: str | None = None, history: list | None = None) -> dict:
        question = (question or "").strip()
        if not question:
            raise LLMError("質問を入力してください")
        cfg = self.get_config()
        hits = self.retrieve(question, k=6)
        if path:
            note = self.index.get(path)
            if note and all(h["path"] != path for h in hits):
                hits.insert(0, {"path": path, "title": note["title"], "heading": "",
                                "text": note["text"][:2000], "score": 0})
        sources, blocks = [], []
        for n, h in enumerate(hits, 1):
            sources.append({"n": n, "path": h["path"], "title": h["title"], "heading": h["heading"]})
            head = f" > {h['heading']}" if h["heading"] and h["heading"] != h["title"] else ""
            blocks.append(f"[{n}] ノート「{h['title']}」{head}\n{h['text']}")
        if not chat_configured(cfg):
            return {"answer": "", "sources": sources, "llm": False,
                    "message": "LLM が未設定のため、関連するノートだけを表示しています。"}
        system = ("あなたはユーザーの個人ノート（Markdown）に基づいて答えるアシスタントです。"
                  "与えられたノートの抜粋だけを根拠に日本語で簡潔に答えてください。"
                  "根拠にしたノートは [[ノート名]] の形で本文中に示してください。"
                  "ノートに書かれていないことは推測せず「ノートには記載がありません」と答えてください。")
        prompt = ("[TASK:ask]\n# ノートの抜粋\n" + ("\n\n".join(blocks) or "（該当なし）")
                  + (f"\n\n# 現在開いているノート\n{self.index.get(path)['title']}" if path and self.index.get(path) else "")
                  + f"\n\n# 質問\n{question}")
        messages = []
        for turn in (history or [])[-6:]:
            if isinstance(turn, dict) and turn.get("role") in ("user", "assistant") and isinstance(turn.get("content"), str):
                messages.append({"role": turn["role"], "content": turn["content"][:4000]})
        messages.append({"role": "user", "content": prompt})
        answer = LLMClient(cfg).chat(messages, system=system)
        return {"answer": answer, "sources": sources, "llm": True}

    # ------------------------------------------------------------ 要約とタグ
    def summarize(self, path: str) -> dict:
        note = self.index.get(path)
        if not note:
            raise LLMError("ノートが見つかりません")
        cfg = self.get_config()
        existing = [t["tag"] for t in self.index.tags()][:80]
        prompt = ("[TASK:summarize]\n次のノートを 3 行以内で要約し、付けるとよいタグを最大 5 個提案してください。"
                  "タグは既存タグを優先して再利用してください。\n"
                  '出力は JSON のみ: {"summary": "…", "tags": ["…"]}\n'
                  f"# 既存タグ\n{', '.join(existing) or '（なし）'}\n"
                  f"# ノート「{note['title']}」\n{note['text'][:6000]}")
        raw = LLMClient(cfg).chat(prompt, temperature=0.0)
        data = _parse_json(raw)
        if not isinstance(data, dict):
            return {"summary": raw.strip(), "tags": []}
        tags = [str(t).lstrip("#").strip() for t in data.get("tags") or [] if str(t).strip()]
        return {"summary": str(data.get("summary") or "").strip(), "tags": tags[:5]}

    # ------------------------------------------------------------ リンク候補
    def suggest_links(self, path: str, k: int = 6) -> list[dict]:
        note = self.index.get(path)
        if not note:
            return []
        linked = {o["path"] for o in self.index.outgoing(path) if o["path"]}
        _, body, _ = L.split_frontmatter(note["text"])
        query = f"{note['title']}\n{body[:3000]}"
        hits = self.retrieve(query, k=k * 4, exclude=linked | {path})
        out: dict[str, dict] = {}
        for h in hits:
            if h["path"] not in out:
                out[h["path"]] = {"path": h["path"], "title": h["title"],
                                  "heading": h["heading"], "snippet": h["text"][:120],
                                  "score": h["score"]}
        return list(out.values())[:k]

    # ------------------------------------------------------------ 書き換え
    PRESETS = {
        "tidy": "誤字を直し、読みやすい箇条書きに整えてください。事実や数字は変えないでください。",
        "summary": "3 行以内に要約してください。",
        "email": "社外向けの丁寧なメール文に書き直してください。件名も付けてください。",
        "actions": "文中から次のアクション（誰が・何を・いつまでに）を抽出し、"
                   "Markdown のチェックリスト（- [ ] ）で出力してください。",
        "continue": "文脈に沿って続きを書いてください。",
    }

    def transform(self, text: str, preset: str = "", instruction: str = "") -> str:
        text = (text or "").strip()
        if not text:
            raise LLMError("対象の文章が空です")
        inst = instruction.strip() or self.PRESETS.get(preset, "")
        if not inst:
            raise LLMError("指示を入力してください")
        prompt = f"[TASK:transform]\n# 指示\n{inst}\n\n# 対象の文章\n{text[:8000]}\n\n結果の本文だけを Markdown で出力してください。"
        return LLMClient(self.get_config()).chat(prompt)

    # ------------------------------------------------------------ 埋め込みインデックス
    def build_embeddings_async(self) -> dict:
        if self.embed_status["state"] == "running":
            return dict(self.embed_status)
        cfg = self.get_config()
        if not embed_configured(cfg):
            raise LLMError("埋め込みモデルが未設定です（設定の「LLM」で Embed モデルを登録してください）")
        self.embed_status = {"state": "running", "done": 0, "total": 0, "message": ""}
        threading.Thread(target=self._build_embeddings, args=(cfg,), daemon=True).start()
        return dict(self.embed_status)

    def _build_embeddings(self, cfg: dict) -> None:
        try:
            model = cfg["embed_model"]
            have = self.index.embeddings(model)
            todo = {}
            for c in self.index.chunks():
                if c["hash"] not in have:
                    todo[c["hash"]] = f"{c['title']} {c['heading']}\n{c['text']}"
            items = list(todo.items())
            self.embed_status["total"] = len(items)
            client = LLMClient(cfg)
            for i in range(0, len(items), 32):
                batch = items[i:i + 32]
                vecs = client.embed([t for _, t in batch])
                self.index.put_embeddings(model, [(h, v) for (h, _), v in zip(batch, vecs)])
                self.embed_status["done"] = min(len(items), i + len(batch))
            self.index.prune_embeddings()
            self.embed_status.update(state="done", message=f"{len(items)} 件を更新しました")
        except Exception as e:  # noqa: BLE001 - UI に表示する
            self.embed_status.update(state="error", message=str(e))


def _parse_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None
