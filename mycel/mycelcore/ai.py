"""AI 機能: ノートに質問（RAG）・要約・リンク候補・文章の書き換え。

検索（retrieval）は 2 段構え:
- キーワード: 文字バイグラムの BM25。索引（転置インデックス）は読み込み時に作ってあるので、
  質問のたびに資料を処理し直さない。LLM 設定がなくても使える
- 意味検索: 埋め込みモデルが設定されていれば、「更新」のときに埋め込みを作り、質問時に併用（RRF で統合）

検索対象は読み込み範囲の一部（フォルダ・外部フォルダ）に絞れる。
"""
from __future__ import annotations

import json
import math
import re
import threading

from . import links as L
from .config import chat_configured, embed_configured
from .llm import LLMClient, LLMError

def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class AIService:
    def __init__(self, index, get_config):
        self.index = index
        self.get_config = get_config
        self._lock = threading.Lock()
        self._vec_model = ""
        self._vecs: dict[str, list[float]] | None = None      # 埋め込みのメモリキャッシュ

    # ------------------------------------------------------------ 状態
    def status(self) -> dict:
        cfg = self.get_config()
        chunks = self.index.chunk_count()
        embedded = self.index.embedded_count(cfg["embed_model"]) if embed_configured(cfg) else 0
        return {"chat": chat_configured(cfg), "embed": embed_configured(cfg),
                "chunks": chunks, "embedded": embedded,
                "retrieval": "hybrid" if embedded else "keyword"}

    def _template_prefix(self) -> list[str]:
        tpl = self.get_config().get("template_folder", "").strip().strip("/")
        return [tpl] if tpl else []

    def _vectors(self, model: str) -> dict[str, list[float]]:
        with self._lock:
            if self._vecs is None or self._vec_model != model:
                self._vecs = self.index.embeddings(model)
                self._vec_model = model
            return self._vecs

    # ------------------------------------------------------------ 検索
    def retrieve(self, query: str, k: int = 6, exclude: set[str] | None = None,
                 prefixes: list[str] | None = None) -> list[dict]:
        """関連するチャンクを返す。prefixes で検索対象（フォルダ・外部フォルダ）を絞る。"""
        exclude = exclude or set()
        tpl = self._template_prefix()        # テンプレートは空欄の雛形なので対象外
        ranks: dict[int, float] = {}
        for r, (cid, _) in enumerate(self.index.search_chunks(query, 60, prefixes, tpl)):
            ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (60 + r)
        cfg = self.get_config()
        if embed_configured(cfg):
            vecs = self._vectors(cfg["embed_model"])
            if vecs:
                try:
                    qv = LLMClient(cfg).embed([query])[0]
                    sims = []
                    for ref in self.index.chunk_refs():
                        v = vecs.get(ref["hash"])
                        if v is None:
                            continue
                        p = ref["path"]
                        if prefixes is not None and not any(self.index._under(p, x) for x in prefixes):
                            continue
                        if any(p == t or p.startswith(t + "/") for t in tpl):
                            continue
                        sims.append((cosine(qv, v), ref["id"]))
                    sims.sort(reverse=True)
                    for r, (_, cid) in enumerate(sims[:60]):
                        ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (60 + r)
                except LLMError:
                    pass                              # 意味検索が落ちてもキーワードで続行
        order = sorted(ranks, key=lambda i: -ranks[i])
        rows = self.index.get_chunks(order[:k * 4])
        out = []
        for cid in order:
            c = rows.get(cid)
            if not c or c["path"] in exclude:
                continue
            out.append({**c, "score": round(ranks[cid], 5)})
            if len(out) >= k:
                break
        return out

    # ------------------------------------------------------------ 質問
    def ask(self, question: str, path: str | None = None, history: list | None = None,
            prefixes: list[str] | None = None) -> dict:
        question = (question or "").strip()
        if not question:
            raise LLMError("質問を入力してください")
        cfg = self.get_config()
        hits = self.retrieve(question, k=6, prefixes=prefixes)
        if path:
            note = self.index.get(path)
            if note and all(h["path"] != path for h in hits):
                hits.insert(0, {"path": path, "title": note["title"], "heading": "", "kind": note["kind"],
                                "text": note["text"][:2000], "score": 0})
        sources, blocks = [], []
        for n, h in enumerate(hits, 1):
            sources.append({"n": n, "path": h["path"], "title": h["title"], "heading": h["heading"]})
            head = f" > {h['heading']}" if h["heading"] and h["heading"] != h["title"] else ""
            label = "ノート" if h.get("kind", "note") == "note" else "資料"
            blocks.append(f"[{n}] {label}「{h['title']}」{head}\n{h['text']}")
        if not chat_configured(cfg):
            return {"answer": "", "sources": sources, "llm": False,
                    "message": "LLM が未設定のため、関連するノート・資料だけを表示しています。"}
        system = ("あなたはユーザーのノートと資料（Word・PDF・メールなど）に基づいて答えるアシスタントです。"
                  "与えられた抜粋だけを根拠に日本語で簡潔に答えてください。"
                  "根拠にしたノート・資料は [[名前]] の形で本文中に示してください（資料は拡張子付きの名前）。"
                  "書かれていないことは推測せず「ノートや資料には記載がありません」と答えてください。")
        prompt = ("[TASK:ask]\n# ノート・資料の抜粋\n" + ("\n\n".join(blocks) or "（該当なし）")
                  + (f"\n\n# 現在開いているノート・資料\n{self.index.get(path)['title']}" if path and self.index.get(path) else "")
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

    # ------------------------------------------------------------ つながり（資料同士）
    def pick_related(self, subject: str, cands: list[dict], client: LLMClient | None = None) -> list[dict]:
        """RAG で集めた候補から、LLM に本当に関係するものを選ばせて理由を付ける。
        LLM が無い・失敗したときは検索順のまま返す。"""
        cfg = self.get_config()
        if not cands:
            return []
        if client is None:
            if not chat_configured(cfg):
                return cands
            client = LLMClient(cfg)
        lst = "\n".join(f"{i}. 「{c['title']}」 {c.get('snippet', '')[:160]}" for i, c in enumerate(cands, 1))
        try:
            raw = client.chat(
                "[TASK:relate]\n対象の文書と、既存のノート・資料の候補があります。"
                "本当に関係がある候補だけを選び、関係を 20 字程度で説明してください"
                "（例: 同じ顧客の案件 / この見積の改訂版 / 前提となる仕様）。関係が薄いものは選ばないでください。\n"
                '出力は JSON 配列だけ: [{"n": 候補番号, "reason": "関係"}]\n'
                f"# 対象の文書\n{subject[:1500]}\n# 候補\n{lst}", temperature=0.0)
        except LLMError:
            return cands
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        try:
            data = json.loads(m.group(0)) if m else None
        except ValueError:
            data = None
        if not isinstance(data, list):
            return cands
        out = []
        for it in data:
            try:
                c = cands[int(it.get("n")) - 1]
            except (TypeError, ValueError, IndexError, AttributeError):
                continue
            if c not in out:
                out.append({**c, "reason": str(it.get("reason") or "").strip()[:60]})
        return out

    def _subject(self, item: dict) -> str:
        _, body, _ = L.split_frontmatter(item["text"] or "")
        return f"{item['title']}\n{body[:1500]}"

    def suggest_related(self, path: str, k: int = 6, use_llm: bool = True) -> list[dict]:
        """この資料（ノート）とつながりそうなノート・資料。既につながっているものは除く。"""
        item = self.index.get(path)
        if not item or item["status"] != "ok":
            return []
        rel = self.index.relations
        skip = {path} | ({r["path"] for r in rel.for_path(path)} if rel else set())
        skip |= {o["path"] for o in self.index.outgoing(path) if o["path"]}
        hits = self.retrieve(self._subject(item), k=k * 4, exclude=skip)
        cands: dict[str, dict] = {}
        for h in hits:
            if h["path"] not in cands:
                it = self.index.get(h["path"]) or {}
                cands[h["path"]] = {"path": h["path"], "title": h["title"], "kind": h.get("kind", "note"),
                                    "grp": it.get("grp", ""), "snippet": h["text"][:160].replace("\n", " "),
                                    "score": h["score"], "reason": ""}
        cands_l = list(cands.values())[:max(k + 2, 8)]
        return (self.pick_related(self._subject(item), cands_l) if use_llm else cands_l)[:k]

    def propose_relations(self, prefix: str, per_item: int = 2, limit: int = 300,
                          min_ratio: float = 0.3) -> list[dict]:
        """フォルダ内の資料について、内容の近い資料の組をまとめて提案する（キーワード索引だけで速く）。
        近さは「自分自身との一致度」を 1 とした比で表す。"""
        rel = self.index.relations
        docs = [n for n in self.index.notes() if n["kind"] == "doc" and n["status"] == "ok"
                and self.index._under(n["path"], prefix)][:limit]
        seen: set[frozenset] = set()
        out = []
        for d in docs:
            item = self.index.get(d["path"])
            q = self._subject(item)[:800]
            hits = self.index.search_chunks(q, 40)
            chunks = self.index.get_chunks([cid for cid, _ in hits])
            best: dict[str, float] = {}
            for cid, sc in hits:
                c = chunks.get(cid)
                if c and sc > best.get(c["path"], 0):
                    best[c["path"]] = sc
            base = best.get(d["path"]) or max(best.values(), default=0)
            if not base:
                continue
            ranked = sorted(((p, sc / base) for p, sc in best.items() if p != d["path"]), key=lambda x: -x[1])
            n = 0
            for p, ratio in ranked:
                other = self.index.get(p)
                if not other or other["kind"] != "doc" or ratio < min_ratio:
                    continue
                pair = frozenset((d["path"], p))
                if pair in seen or (rel and rel.exists(d["path"], p)):
                    continue
                seen.add(pair)
                out.append({"a": d["path"], "a_title": d["title"], "a_grp": d["grp"],
                            "b": p, "b_title": other["title"], "b_grp": other["grp"],
                            "score": round(min(ratio, 1.0), 3)})
                n += 1
                if n >= per_item:
                    break
        out.sort(key=lambda x: -x["score"])
        return out[:200]

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

    # ------------------------------------------------------------ 埋め込み（「更新」のときだけ作る）
    def embed_pending(self, prefixes: list[str] | None = None, cancel=None, progress=None) -> int:
        """埋め込みがまだ無いチャンクだけ埋め込む。戻り値は作った件数。"""
        cfg = self.get_config()
        if not embed_configured(cfg):
            return 0
        model = cfg["embed_model"]
        todo = self.index.chunks_without_embedding(model, prefixes)
        client = LLMClient(cfg)
        done = 0
        for i in range(0, len(todo), 32):
            if cancel is not None and cancel.is_set():
                from .index import Cancelled
                raise Cancelled()
            batch = todo[i:i + 32]
            if progress:
                progress("意味検索の索引", done, len(todo), batch[0]["path"])
            vecs = client.embed([f"{c['title']} {c['heading']}\n{c['text']}" for c in batch])
            items = [(c["hash"], v) for c, v in zip(batch, vecs)]
            self.index.put_embeddings(model, items)
            with self._lock:
                if self._vecs is not None and self._vec_model == model:
                    self._vecs.update(items)
            done += len(batch)
        if prefixes is None:
            self.index.prune_embeddings()
        return done


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
