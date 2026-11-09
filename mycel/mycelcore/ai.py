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
from collections import Counter

from . import links as L
from .config import chat_configured, embed_configured
from .llm import LLMClient, LLMError

def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class AIService:
    def __init__(self, index, get_config, app=None):
        self.index = index
        self.get_config = get_config
        self.app = app                      # 人物・組織など、索引の外の手がかりに使う（無くても動く）
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
                "retrieval": "hybrid" if embedded else "keyword",
                "rag": {"top_k": int(cfg.get("rag_top_k") or 6), "pool": int(cfg.get("rag_pool") or 60),
                        "rerank": cfg.get("rag_rerank", "none"), "rerank_pool": int(cfg.get("rag_rerank_pool") or 24)}}

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
                 prefixes: list[str] | None = None, paths: set[str] | None = None,
                 query_vec: list[float] | None = None, pool: int | None = None) -> list[dict]:
        """関連するチャンクを返す。prefixes（フォルダ）や paths（ファイルの集合）で検索対象を絞る。
        query_vec を渡すと埋め込みの API 呼び出しを省く。戻り値の各行に mode（keyword / vector / both）を付ける。
        pool（候補の深さ）を省くと設定 rag_pool を使う。"""
        exclude = exclude or set()
        if pool is None:
            pool = int(self.get_config().get("rag_pool") or 60)
        tpl = self._template_prefix()        # テンプレートは空欄の雛形なので対象外
        ranks: dict[int, float] = {}
        modes: dict[int, set] = {}
        for r, (cid, _) in enumerate(self.index.search_chunks(query, pool, prefixes, tpl, paths)):
            ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (60 + r)
            modes.setdefault(cid, set()).add("keyword")
        cfg = self.get_config()
        if embed_configured(cfg):
            vecs = self._vectors(cfg["embed_model"])
            if vecs:
                try:
                    qv = query_vec or LLMClient(cfg).embed([query])[0]
                    sims = []
                    for ref in self.index.chunk_refs():
                        v = vecs.get(ref["hash"])
                        if v is None:
                            continue
                        p = ref["path"]
                        if prefixes is not None and not any(self.index._under(p, x) for x in prefixes):
                            continue
                        if paths is not None and p not in paths:
                            continue
                        if any(p == t or p.startswith(t + "/") for t in tpl):
                            continue
                        sims.append((cosine(qv, v), ref["id"]))
                    sims.sort(reverse=True)
                    for r, (_, cid) in enumerate(sims[:pool]):
                        ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (60 + r)
                        modes.setdefault(cid, set()).add("vector")
                except LLMError:
                    pass                              # 意味検索が落ちてもキーワードで続行
        order = sorted(ranks, key=lambda i: -ranks[i])
        rows = self.index.get_chunks(order[:k * 4])
        out = []
        for cid in order:
            c = rows.get(cid)
            if not c or c["path"] in exclude:
                continue
            m = modes.get(cid, set())
            out.append({**c, "score": round(ranks[cid], 5), "mode": "both" if len(m) > 1 else next(iter(m), "keyword")})
            if len(out) >= k:
                break
        return out

    def embed_query(self, text: str) -> list[float] | None:
        cfg = self.get_config()
        if not embed_configured(cfg):
            return None
        try:
            return LLMClient(cfg).embed([text])[0]
        except LLMError:
            return None

    # ------------------------------------------------------------ リランク
    def rerank(self, question: str, hits: list[dict], k: int, cfg: dict | None = None, backend: str | None = None) -> list[dict]:
        """LLM に候補の関連度を 0〜10 で採点させ、上位 k 件に並べ直す。失敗したら元の順のまま。"""
        cfg = cfg or self.get_config()
        if len(hits) <= k or not chat_configured(cfg, backend):
            return hits[:k]
        listing = "\n\n".join(f"[{i}] 「{h['title']}」{(' > ' + h['heading']) if h.get('heading') and h['heading'] != h['title'] else ''}\n{h['text'][:700]}"
                                for i, h in enumerate(hits, 1))
        try:
            raw = LLMClient(cfg, backend).chat(
                "[TASK:rerank]\n次の質問に答える根拠として、各候補がどれだけ役に立つかを 0〜10 で採点してください。"
                "質問に直接答える情報があれば高く、話題が同じだけなら低く。出力は JSON 配列だけ: "
                '[{"n": 候補番号, "score": 点数}, ...]（全候補を含める）\n\n'
                f"# 質問\n{question}\n\n# 候補\n{listing}", temperature=0.0)
            data = _parse_json_list(raw)
            if not isinstance(data, list):
                return hits[:k]
            scores: dict[int, float] = {}
            for row in data:
                if isinstance(row, dict):
                    try:
                        scores[int(row.get("n"))] = float(row.get("score"))
                    except (TypeError, ValueError):
                        pass
            if not scores:
                return hits[:k]
        except LLMError:
            return hits[:k]
        order = sorted(range(1, len(hits) + 1), key=lambda n: (-scores.get(n, -1.0), n))
        out = []
        for n in order[:k]:
            h = dict(hits[n - 1])
            h["rerank"] = scores.get(n)
            out.append(h)
        return out

    def retrieve_for_answer(self, question: str, k: int | None = None, backend: str | None = None, **kw) -> list[dict]:
        """質問用の検索。設定の深さ（rag_pool / rag_top_k）とリランク（rag_rerank）を適用する。
        backend は回答と同じ接続先（リランクの候補も同じ先にしか送らない）。"""
        cfg = self.get_config()
        k = k or int(cfg.get("rag_top_k") or 6)
        if cfg.get("rag_rerank") == "llm" and chat_configured(cfg, backend):
            cand = self.retrieve(question, k=max(k, int(cfg.get("rag_rerank_pool") or 24)), **kw)
            return self.rerank(question, cand, k, cfg, backend)
        return self.retrieve(question, k=k, **kw)

    # ------------------------------------------------------------ 質問
    def ask(self, question: str, path: str | None = None, history: list | None = None,
            prefixes: list[str] | None = None, paths: set[str] | None = None, k: int | None = None,
            system_extra: str = "", backend: str | None = None) -> dict:
        question = (question or "").strip()
        if not question:
            raise LLMError("質問を入力してください")
        cfg = self.get_config()
        hits = self.retrieve_for_answer(question, k=k, backend=backend, prefixes=prefixes, paths=paths)
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
        if not chat_configured(cfg, backend):
            return {"answer": "", "sources": sources, "llm": False, "backend": "local",
                    "message": "LLM が未設定のため、関連するノート・資料だけを表示しています。"}
        system = (system_extra or "あなたはユーザーのノートと資料（Word・PDF・メールなど）に基づいて答えるアシスタントです。"
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
        client = LLMClient(cfg, backend)
        answer = client.chat(messages, system=system)
        return {"answer": answer, "sources": sources, "llm": True, "backend": client.backend,
                "model": client.cfg.get("model", ""), "host": client.host}

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

    # ------------------------------------------------------------ つながりの候補（文書単位）
    @staticmethod
    def _keywords(text: str, shared: list[str], limit: int = 6) -> list[str]:
        """共通するバイグラムを、元の文中でつながっている範囲にまとめて読める語にする（「倉庫」「庫自」→「倉庫自動化」）。"""
        if not shared:
            return []
        sset = set(shared)
        words: Counter = Counter()
        for seg in re.split(r"[\s\u3000、。，．,.!?！？「」『』（）()\[\]【】:：;；/／|｜\-–—…・'\"]+", text):
            if not seg:
                continue
            sl = seg.lower()                      # 照合は小文字、表示は元の表記
            if sl.isascii():
                if sl in sset:
                    words[seg] += 1
                continue
            i = 0
            while i < len(sl) - 1:
                if sl[i:i + 2] in sset:
                    j = i + 2
                    while j < len(sl) and sl[j - 1:j + 1] in sset:
                        j += 1
                    words[seg[i:j]] += 1
                    i = j
                else:
                    i += 1
        # 前後の助詞・送り仮名を落とし（「ムの」「と保守」→「保守」）、長い語を優先しつつ、他の語に含まれる語は省く
        picked: list[str] = []
        cleaned: Counter = Counter()
        for w, n in words.items():
            w = re.sub(r"^[のとはがをにでもやへ]+|[のとはがをにでもやへ]+$", "", w) if not w.isascii() else w
            if len(w) >= 2 and not re.fullmatch(r"[\u3041-\u309f]+", w):
                cleaned[w] += n
        for w, _ in sorted(cleaned.items(), key=lambda x: (-len(x[0]), -x[1])):
            if any(w in p for p in picked):
                continue
            picked.append(w)
            if len(picked) >= limit:
                break
        return picked

    def _doc_vectors(self, model: str) -> dict[str, list[float]]:
        """文書ごとの埋め込み（チャンクの平均。API は呼ばない）。"""
        vecs = self._vectors(model)
        if not vecs:
            return {}
        acc: dict[str, list[float]] = {}
        cnt: Counter = Counter()
        for ref in self.index.chunk_refs():
            v = vecs.get(ref["hash"])
            if v is None:
                continue
            a = acc.get(ref["path"])
            if a is None:
                acc[ref["path"]] = list(v)
            else:
                for i, x in enumerate(v):
                    a[i] += x
            cnt[ref["path"]] += 1
        for p, a in acc.items():
            n = cnt[p]
            norm = math.sqrt(sum((x / n) ** 2 for x in a)) or 1.0
            acc[p] = [x / n / norm for x in a]
        return acc

    def _entities_of(self, path: str) -> dict[tuple, str]:
        if self.app is None or not hasattr(self.app, "people"):
            return {}
        try:
            return {(e["type"], e["key"]): e["name"] for e in self.app.people.of(path)}
        except Exception:  # noqa: BLE001 - 手がかりが取れなくても候補は出す
            return {}

    def suggest_related(self, path: str, k: int = 8, use_llm: bool = False, kind: str = "") -> list[dict]:
        """この資料（ノート）とつながりそうなノート・資料。既につながっている・リンク済みのものは除く。

        近さは文書全体の主題語の重なり（全文）を基本に、埋め込みがあれば文書ベクトルの近さを合わせ、
        同じ人物・組織／同じタグ／同じリンク先／同じフォルダを加点する。理由は語で説明する（LLM は任意）。"""
        item = self.index.get(path)
        if not item or item["status"] != "ok":
            return []
        rel = self.index.relations
        skip = {path} | ({r["path"] for r in rel.for_path(path)} if rel else set())
        skip |= {o["path"] for o in self.index.outgoing(path) if o["path"]}
        skip |= {b["path"] for b in self.index.backlinks(path) if b.get("path")}
        tpl = self._template_prefix()
        pool = max(k * 5, 40)
        kw = {h["path"]: h for h in self.index.similar_docs(path, k=pool, exclude_prefixes=tpl)}
        cfg = self.get_config()
        vec_sims: dict[str, float] = {}
        if embed_configured(cfg):
            dv = self._doc_vectors(cfg["embed_model"])
            me = dv.get(path)
            if me:
                sims = sorted(((sum(a * b for a, b in zip(me, v)), p) for p, v in dv.items() if p != path), reverse=True)
                vec_sims = {p: s for s, p in sims[:pool]}
        my_ents = self._entities_of(path)
        my_tags = set(self.index.tags_of(path))
        my_out = {o["path"] for o in self.index.outgoing(path) if o["path"]}
        my_folder = item.get("folder", "")
        text = item.get("text") or ""
        cands: dict[str, dict] = {}
        for p in set(kw) | set(vec_sims):
            if p in skip:
                continue
            it = self.index.get(p)
            if not it or it["status"] != "ok" or (kind and it["kind"] != kind):
                continue
            if any(p == t or p.startswith(t + "/") for t in tpl):
                continue
            h = kw.get(p)
            kws = self._keywords(text, h["shared"]) if h else []
            s_kw = h["score"] if h else 0.0
            s_vec = max(0.0, vec_sims.get(p, 0.0))
            base = max(s_kw, 0.6 * s_kw + 0.4 * s_vec) if vec_sims else s_kw
            if vec_sims and not h:
                base = 0.7 * s_vec
            why = []
            if kws:
                why.append("共通の語: " + "・".join(kws[:5]))
            bonus = 0.0
            ents = self._entities_of(p) if my_ents else {}
            shared_e = [my_ents[e] for e in my_ents if e in ents]
            if shared_e:
                bonus += min(0.3, 0.12 * len(shared_e)); why.append("同じ人物・組織: " + "・".join(shared_e[:3]))
            shared_t = sorted(my_tags & set(self.index.tags_of(p))) if my_tags else []
            if shared_t:
                bonus += min(0.2, 0.1 * len(shared_t)); why.append("同じタグ: " + "・".join("#" + t for t in shared_t[:3]))
            if my_out:
                co = my_out & {o["path"] for o in self.index.outgoing(p) if o["path"]}
                if co:
                    bonus += min(0.15, 0.08 * len(co)); why.append(f"同じリンク先 {len(co)} 件")
            if my_folder and it.get("folder") == my_folder:
                bonus += 0.04
            if s_vec >= 0.75 and (not h or s_kw < 0.3):
                why.append("意味が近い（埋め込み）")
            score = round(min(1.0, base + bonus), 3)
            if score < 0.08:
                continue
            _, body, _ = L.split_frontmatter(it.get("text") or "")
            cands[p] = {"path": p, "title": it["title"], "kind": it["kind"], "grp": it.get("grp", ""),
                        "snippet": body[:160].replace("\n", " "), "score": score, "reason": "",
                        "why": " ／ ".join(why), "keywords": kws[:6], "entities": shared_e[:3], "tags": shared_t[:3],
                        "vector": round(s_vec, 3) if vec_sims else None}
        out = sorted(cands.values(), key=lambda c: -c["score"])[:k]
        if use_llm and out:
            judged = self.pick_related(self._subject(item), out)
            byp = {c["path"]: c for c in judged}
            # LLM が選んだものを先に、理由を付けて。選ばれなかったものも（薄い候補として）後ろに残す
            out = [byp[c["path"]] for c in out if c["path"] in byp] + [{**c, "llm_skipped": True} for c in out if c["path"] not in byp]
        return out

    def propose_relations(self, prefix: str, per_item: int = 2, limit: int = 300,
                          min_ratio: float = 0.3) -> list[dict]:
        """フォルダ内の資料について、内容の近い資料の組をまとめて提案する（キーワード索引だけで速く）。
        近さは「自分自身との一致度」を 1 とした比で表す。"""
        rel = self.index.relations
        docs = [n for n in self.index.notes() if n["kind"] == "doc" and n["status"] == "ok"
                and self.index._under(n["path"], prefix)][:limit]
        seen: set[frozenset] = set()
        out = []
        tpl = self._template_prefix()
        for d in docs:
            n = 0
            for h in self.index.similar_docs(d["path"], k=12, exclude_prefixes=tpl):
                p, ratio = h["path"], h["score"]
                other = self.index.get(p)
                if not other or other["kind"] != "doc" or ratio < min_ratio:
                    continue
                pair = frozenset((d["path"], p))
                if pair in seen or (rel and rel.exists(d["path"], p)):
                    continue
                seen.add(pair)
                out.append({"a": d["path"], "a_title": d["title"], "a_grp": d["grp"],
                            "b": p, "b_title": other["title"], "b_grp": other["grp"],
                            "score": round(min(ratio, 1.0), 3),
                            "keywords": self._keywords(self.index.get(d["path"]).get("text") or "", h["shared"], 4)})
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


def _parse_json_list(text: str):
    """JSON 配列を取り出す（コードフェンスや前置きがあっても）。"""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


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
