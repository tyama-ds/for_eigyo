"""組み合わせ RAG — 選んだ検索方式（全文・埋め込み・GraphRAG・DecisionRAG）の結果を RRF で統合し、回答を1つ作る。

どの組み合わせが効くかを、同じコーパス・同じ質問で単体のエンジンと並べて比べるためのエンジン
（Doc Vault のチャットで選べる検索方式の組み合わせを、ここで先に試す）。

- インデックスを作り直さない: 各方式は、そのエンジン（Vector RAG / GraphRAG / DecisionRAG）が構築済みの
  インデックスを読み込んで、そのエンジンの retrieve()（検索だけ。回答は作らない）を呼ぶ。
  自分のインデックスはチャンクの一覧だけ（一瞬で作れるので、無い・古いときはオーケストレーターが
  質問の前にその場で作る。auto_index）。全文（BM25）はそのチャンクの一覧から毎回計算する（構築不要）
- 使えない方式は飛ばして理由を出す: インデックスが未構築・古い（コーパスを変えた後に作り直していない。
  インデックスの corpus_rev と、このエンジンのインデックスの corpus_rev = 今のコーパスの改訂番号を比べる。
  エンジン一覧の「インデックスが古い」と同じ判定）・チャンクの区切りが合わない・埋め込みの次元が違う・
  検索に失敗した。残った方式だけで統合して答える
- 統合: 重み付き RRF（方式ごとに、順位 r のチャンクへ 重み ÷ (60 + r) を足す）。重みは GraphRAG と
  DecisionRAG が 1.5、全文と埋め込みが 1.0。全文と埋め込みはどちらも「質問と本文の近さ」で似た信号なので、
  構造をたどる方式が 2 対 1 で押し切られないようにした（Doc Vault の統合と同じ値）
- 任意で、統合した上位を LLM で並べ替える（再順位付け。LLM 1 回）
- 回答は統合した上位 6 チャンクから1回だけ生成する（LLM 未設定なら抜粋）
- 出典ごとに、どの方式で何位・スコアいくつだったか（why）と、統合後の順位・RRF スコアを返す。
  統計に方式ごとの秒数・質問時の LLM 呼び出し回数（判定・並べ替え・回答）・飛ばした方式と理由を入れる
"""
from __future__ import annotations

import dataclasses
import time

from .. import decision_api, store
from ..config import (COMBO_COMPONENTS, DEFAULT_CONFIG, chat_configured,
                      embed_configured, valid_components)
from .base import (Engine, EngineContext, build_chunks, chunk_citation,
                   extractive_answer, generate_answer)
from .bm25 import BM25Engine
from .decision import DecisionRAGEngine
from .graphrag import GraphRAGEngine
from .vector import VectorEngine

TOP_K = 6                  # 回答に使うチャンク数（ほかの組み込みエンジンと同じ）
CANDIDATES = 12            # 各方式から統合へ渡す候補数（Hybrid RAG と同じ）
RRF_K = 60
RERANK_CANDIDATES = 12     # 再順位付けで LLM に見せる、統合した上位の件数

# 方式の表示と重み。sources は読み込むインデックス（左から順に、使えるものを使う）
COMPONENT_INFO: dict[str, dict] = {
    "bm25": {
        "label": "全文（BM25）", "short": "全文", "weight": 1.0, "sources": [],
        "desc": "質問の語がそのまま出てくるチャンク。型番・固有名詞・数字に強い",
        "cost": "LLM 0 回・埋め込み 0 回",
        "index_note": "構築不要（コーパスのチャンクから毎回計算）",
    },
    "vector": {
        "label": "埋め込み（ベクトル）", "short": "埋め込み", "weight": 1.0, "sources": ["vector", "hybrid"],
        "desc": "意味の近いチャンク。言い換え・表記ゆれに強い",
        "cost": "埋め込み 1 回（質問）・LLM 0 回",
    },
    "graphrag": {
        "label": "GraphRAG（エンティティからたどる）", "short": "GraphRAG", "weight": 1.5, "sources": ["graphrag"],
        "desc": "質問に関係するエンティティと、関係でつながる先のエンティティが出てくるチャンク。"
                "文書をまたぐ関係に強い（local 検索の前半。global の map-reduce はしない）",
        "cost": "LLM 0 回（埋め込みつきで構築していれば質問の埋め込み 1 回。埋め込みと共用）",
    },
    "decision": {
        "label": "DecisionRAG（階層を判定でたどる）", "short": "DecisionRAG", "weight": 1.5, "sources": ["decision"],
        "desc": "文書 → 節 → 段落を判定モデルでたどった段落。語が一致しない質問に強いが、判定の分だけ遅い",
        "cost": "",                     # 判定回数の上限・判定モデルで変わる（_decision_cost）
    },
}
RERANK_INFO = {
    "label": "再順位付け（LLM で並べ替え）", "short": "LLM並べ替え",
    "desc": f"統合した上位 {RERANK_CANDIDATES} 件を、LLM が質問に答えるのに役立つ順に並べ替える",
    "cost": "LLM 1 回",
}
SOURCE_NAMES = {"vector": "Vector RAG", "hybrid": "Hybrid RAG", "graphrag": "GraphRAG",
                "decision": "DecisionRAG"}

RERANK_SYSTEM = "あなたは検索結果の並べ替えを行います。指示された JSON だけを出力し、説明は書きません。"
RERANK_PROMPT = """[TASK:combo_rerank]
質問に答えるのに役立つ順に、候補の番号を並べてください。役に立たない候補は省いてかまいません。

出力は次の JSON のみ:
{{"order": [3, 1, 2]}}

# 質問
{question}

# 候補
{candidates}
"""


# ---------------------------------------------------------------- 設定

def combo_settings(cfg: dict) -> tuple[list[str], bool]:
    """設定から (統合する方式, 再順位付けするか)。方式は決まった順（全文 → 埋め込み → GraphRAG → DecisionRAG）。"""
    comps = valid_components(cfg.get("combo_components"))
    if comps is None:
        comps = list(DEFAULT_CONFIG["combo_components"])
    return comps, bool(cfg.get("combo_rerank"))


def _judge_api_ready(cfg: dict) -> bool:
    return cfg.get("decision_judge") == "api" and decision_api.ready(cfg)


def _judge_ok(cfg: dict) -> bool:
    """DecisionRAG の判定ができるか（LLM か判定 API）。"""
    return chat_configured(cfg) or _judge_api_ready(cfg)


def _budget(cfg: dict) -> int:
    return int(cfg.get("decision_budget") or 8)


def _decision_cost(cfg: dict) -> str:
    if _judge_api_ready(cfg):
        return f"判定 API 最大 {_budget(cfg)} 回（LLM 0 回）"
    return f"LLM 最大 {_budget(cfg)} 回（判定）"


def _prefix(comp: str, source: str) -> str:
    """理由の頭（方式の名前とインデックスの名前が違うときだけ。例: 「埋め込み: Vector RAG の…」）。"""
    short = COMPONENT_INFO[comp]["short"]
    return "" if short == SOURCE_NAMES[source] else f"{short}: "


def variant_of(comps: list[str], rerank: bool, *, skipped: list[dict] | None = None) -> dict:
    """UI のバッジ・統合レポートの名前（例: 「全文＋埋め込み＋GraphRAG」）。"""
    label = "＋".join(COMPONENT_INFO[c]["short"] for c in comps) or "（方式なし）"
    title = "組み合わせ: " + (" ＋ ".join(COMPONENT_INFO[c]["label"] for c in comps) or "なし")
    if rerank:
        label += "・LLM並べ替え"
        title += " ／ 再順位付け（LLM）"
    if skipped:
        title += " ／ 使えなかった方式: " + "・".join(s["short"] for s in skipped)
    return {"kind": "combo", "label": label, "title": title + " ／ 統合: RRF"}


def estimate(cfg: dict, comps: list[str], rerank: bool) -> dict:
    """1 問あたりの質問時の呼び出し回数の目安（エンジンタブの表示用）。"""
    chat = chat_configured(cfg)
    judge = _budget(cfg) if "decision" in comps and not _judge_api_ready(cfg) else 0
    judge_api = _budget(cfg) if "decision" in comps and _judge_api_ready(cfg) else 0
    rr = 1 if rerank else 0
    answer = 1 if chat else 0
    embed = 1 if "vector" in comps else 0
    parts = []
    if answer:
        parts.append("回答 1")
    if judge:
        parts.append(f"判定 最大 {judge}")
    if rr:
        parts.append("並べ替え 1")
    total = answer + judge + rr
    text = (f"1 問あたり LLM {'最大 ' if judge else ''}{total} 回"
            + (f"（{'・'.join(parts)}）" if parts else ""))
    if judge_api:
        text += f" ・ 判定 API 最大 {judge_api} 回"
    if embed:
        text += " ・ 埋め込み 1 回"
    elif "graphrag" in comps:
        text += " ・ 埋め込み 0〜1 回（GraphRAG を埋め込みつきで構築していれば）"
    if not chat:
        text += " ・ 回答は抜粋（LLM 未設定）"
    return {"llm_max": total, "judge": judge, "judge_api": judge_api, "rerank": rr, "answer": answer,
            "embed": embed, "text": text}


def parse_order(data, n: int) -> list[int]:
    """再順位付けの出力（{"order": [3, 1, 2]} か [3, 1, 2]）から 1〜n の番号を重複なく。"""
    seq = data.get("order") if isinstance(data, dict) else data
    if not isinstance(seq, list):
        return []
    out: list[int] = []
    for x in seq:
        try:
            i = int(str(x).strip().strip("[]SＳ"))
        except ValueError:
            continue
        if 1 <= i <= n and i not in out:
            out.append(i)
    return out


def rrf(rankings: list[tuple[list[str], float]], position: dict[str, int]) -> list[tuple[str, float]]:
    """重み付き RRF。rankings は [(順位順のチャンク id, 重み)]。同点はコーパスの並び順。"""
    score: dict[str, float] = {}
    for ids, weight in rankings:
        for r, cid in enumerate(ids, 1):
            score[cid] = score.get(cid, 0.0) + weight / (RRF_K + r)
    return sorted(score.items(), key=lambda t: (-round(t[1], 12), position.get(t[0], 1 << 30), t[0]))


# ---------------------------------------------------------------- エンジン

class ComboEngine(Engine):
    id = "combo"
    name = "組み合わせ RAG（選んだ方式を RRF で統合）"
    kind = "builtin"
    description = ("エンジンタブで選んだ方式（全文・埋め込み・GraphRAG・DecisionRAG）の検索結果を RRF で統合 → "
                   "top-k → 生成。各エンジンの構築済みインデックスを使うので構築は一瞬。"
                   "Doc Vault で使う組み合わせを単体のエンジンと並べて比べる")
    requires_chat = False         # LLM 無しでも抜粋モードで動く（DecisionRAG・再順位付けを選ぶと必要）
    requires_embed = False        # 埋め込み（ベクトル）を選ぶと必要
    auto_index = True

    # ------------------------------------------------------------ 表示・可否
    def availability(self, cfg: dict) -> tuple[bool, str]:
        comps, rerank = combo_settings(cfg)
        if not comps:
            return False, "方式が1つも選ばれていません（エンジンタブで選ぶ）"
        if "vector" in comps and not embed_configured(cfg):
            return False, "埋め込み（ベクトル）を選んでいますが、埋め込み（Embed）が未設定です"
        if "decision" in comps and not _judge_ok(cfg):
            return False, "DecisionRAG を選んでいますが、判定に使う LLM（チャット）も判定 API も未設定です"
        if rerank and not chat_configured(cfg):
            return False, "再順位付け（LLM で並べ替え）を選んでいますが、LLM（チャット）が未設定です"
        return True, ""

    def variant(self, *, stats: dict | None = None, cfg: dict | None = None) -> dict | None:
        # 方式は構築時ではなく質問時の設定で決まる（インデックスはチャンクの一覧だけ）
        return variant_of(*combo_settings(cfg)) if cfg is not None else None

    def info(self, cfg: dict) -> dict:
        out = super().info(cfg)
        comps, rerank = combo_settings(cfg)
        out["requires"] = {"chat": ("decision" in comps and not _judge_api_ready(cfg)) or rerank,
                           "embed": "vector" in comps}
        components = []
        for cid in COMBO_COMPONENTS:
            item = {k: v for k, v in COMPONENT_INFO[cid].items() if k != "sources"}
            item["id"] = cid
            if cid == "decision":
                item["cost"] = _decision_cost(cfg)
            item["sources"] = [{"id": s, "name": SOURCE_NAMES[s]} for s in COMPONENT_INFO[cid]["sources"]]
            components.append(item)
        out["combo"] = {
            "selected": comps, "rerank": rerank, "components": components,
            "rerank_info": dict(RERANK_INFO), "variant": variant_of(comps, rerank),
            "estimate": estimate(cfg, comps, rerank),
            "top_k": TOP_K, "candidates": CANDIDATES, "rrf_k": RRF_K,
        }
        return out

    # ------------------------------------------------------------ ingest（チャンクの一覧だけ）
    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        chunks = build_chunks(corpus)
        if not chunks:
            raise ValueError("コーパスが空です")
        ctx.log("各方式は、そのエンジンの構築済みインデックスを質問時に読み込んで使います（ここでは作りません）")
        ctx.progress(1.0, "完了")
        return {"engine": self.id, "built_at": time.time(), "chunks": chunks,
                "stats": {"chunks": len(chunks)}}

    # ------------------------------------------------------------ 方式ごとの検索
    @staticmethod
    def _load_source(comp: str, corpus_rev) -> tuple[str | None, dict | None, str]:
        """方式が読むインデックス（左から順に、今のコーパスで構築済みのもの）。使えないときは理由。"""
        sources = COMPONENT_INFO[comp]["sources"]
        stale = []
        for sid in sources:
            idx = store.load_index(sid)
            if idx is None:
                continue
            if idx.get("corpus_rev") != corpus_rev:
                stale.append(sid)
                continue
            if comp == "vector" and not idx.get("chunk_vecs"):
                continue
            return sid, idx, ""
        prefix = _prefix(comp, sources[0])
        alt = "".join(f"。{SOURCE_NAMES[s]} のインデックスでも可" for s in sources[1:])
        if stale:
            return None, None, (f"{prefix}{SOURCE_NAMES[stale[0]]} のインデックスが古い（コーパスを変えた後に"
                                f"構築し直していない）ため使いませんでした（エンジンタブで構築し直す{alt}）")
        return None, None, (f"{prefix}{SOURCE_NAMES[sources[0]]} のインデックスが未構築のため使いませんでした"
                            f"（エンジンタブで先に構築{alt}）")

    @staticmethod
    def _alignment(src: dict, chunk_by_id: dict[str, dict]) -> tuple[set[str], int]:
        """読み込んだインデックスのチャンクのうち、id と本文がこちらと一致するものの id（と全体の数）。"""
        src_chunks = src.get("chunks") or []
        same = {c.get("id") for c in src_chunks
                if c.get("id") in chunk_by_id and chunk_by_id[c["id"]]["text"] == c.get("text")}
        return same, len(src_chunks)

    def _run(self, comp: str, rec: dict, own: dict, question: str, ctx: EngineContext,
             corpus_rev, chunk_by_id: dict[str, dict]) -> list[tuple[str, float]]:
        info = COMPONENT_INFO[comp]
        if comp == "bm25":                        # 構築不要（このエンジンのチャンクの一覧から）
            rec["source"] = self.id
            started = time.time()
            hits = BM25Engine().retrieve(own, question, ctx, CANDIDATES)
            rec["seconds"] = round(time.time() - started, 3)
            return hits
        started = time.time()
        sid, src, problem = self._load_source(comp, corpus_rev)
        if src is None:
            rec["load_seconds"] = round(time.time() - started, 3)
            rec["skipped"] = problem
            return []
        rec["source"] = sid
        prefix, name = _prefix(comp, sid), SOURCE_NAMES[sid]
        same, total = self._alignment(src, chunk_by_id)
        rec["load_seconds"] = round(time.time() - started, 3)
        if not same:
            rec["skipped"] = (f"{prefix}{name} のインデックスのチャンクの区切りが一致しないため使いませんでした"
                              "（エンジンタブで構築し直す）")
            return []
        partial = ""
        if len(same) < len(chunk_by_id) or len(same) < total:   # 区切りが一部だけ違う: 一致するチャンクだけ使う
            partial = (f"チャンクの区切りが一部違うため、id と本文が一致するチャンクだけ使いました"
                       f"（{name} {total} 件中 {len(same)} 件が一致）")
        started = time.time()
        detail: dict = {}
        if comp == "vector":
            vecs = src["chunk_vecs"]
            qvec = ctx.query_vec(question)
            if vecs and len(vecs[0]) != len(qvec):
                rec["skipped"] = (f"{prefix}埋め込みの次元が {name} の構築時と違う（{len(vecs[0])} ≠ {len(qvec)}。"
                                  "埋め込みモデルを変えた後に作り直していない）ため使いませんでした"
                                  f"（エンジンタブで {name} を構築し直す）")
                return []
            hits = VectorEngine().retrieve(src, question, ctx, CANDIDATES)
        elif comp == "graphrag":
            hits = GraphRAGEngine().retrieve(src, question, ctx, CANDIDATES, detail=detail)
            ents = "・".join(detail.get("entities") or [])
            how = {"embed": "埋め込み", "bm25": "字句一致"}.get(detail.get("how"), "")
            rec["note"] = (f"たどったエンティティ: {ents}（{how}で選択）・関係 {detail.get('relations', 0)} 件"
                           if ents else detail.get("note", ""))
            variant = GraphRAGEngine().variant(stats=src.get("stats") or {})
            rec["source_variant"] = variant["label"] if variant else ""
        else:
            hits = DecisionRAGEngine().retrieve(src, question, ctx, CANDIDATES, detail=detail)
            st = detail.get("stats") or {}
            judge = st.get("judge_api") or {"llm": "LLM"}.get(st.get("judge"), st.get("judge", ""))
            rec["note"] = (f"判定 {st.get('calls', 0)} 回（{judge}）・経路: {st.get('route', '')}"
                           + (f" ・ {st['note']}" if st.get("note") else ""))
            if detail.get("api"):
                rec["judge_api_calls"] = int(detail.get("calls") or 0)
        rec["seconds"] = round(time.time() - started, 3)
        if partial:
            rec["note"] = f"{rec['note']} ・ {partial}" if rec["note"] else partial
            rec["warning"] = f"{info['short']}: {partial}"
            return [(cid, s) for cid, s in hits if cid in same]
        return hits

    # ------------------------------------------------------------ 再順位付け
    def _rerank(self, ctx: EngineContext, question: str, chunks: list[dict]) -> tuple[list[int], str]:
        """LLM に並べ替えさせる。(並べ替えた 0 始まりの位置, 読めなかったときの理由)。"""
        lines = "\n".join(f"[{i}] （{c['doc_title']}）" + c["text"][:400].replace("\n", " ")
                          for i, c in enumerate(chunks, 1))
        data, info = ctx.llm.chat_json(RERANK_PROMPT.format(question=question, candidates=lines),
                                       system=RERANK_SYSTEM, temperature=0.0,
                                       max_tokens=max(int(ctx.cfg.get("max_tokens") or 0), 1024))
        order = parse_order(data, len(chunks))
        if not order:
            return list(range(len(chunks))), (info.get("reason") or "番号の並びがありませんでした")
        order += [i for i in range(1, len(chunks) + 1) if i not in order]   # 省かれた候補は後ろへ
        return [i - 1 for i in order], ""

    # ------------------------------------------------------------ query
    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        comps, rerank = combo_settings(ctx.cfg)
        if not comps:
            raise ValueError("方式が1つも選ばれていません（エンジンタブで選ぶ）")
        chunks = index["chunks"]
        chunk_by_id = {c["id"]: c for c in chunks}
        position = {c["id"]: i for i, c in enumerate(chunks)}
        # 今のコーパスの改訂番号（オーケストレーターが、古いこのインデックスを質問の前に作り直している）
        corpus_rev = index.get("corpus_rev")
        if corpus_rev is None:
            corpus_rev = store.load_corpus()["rev"]
        llm_stats = ctx.llm.stats
        chat0, embed0 = llm_stats.get("chat_calls", 0), llm_stats.get("embed_calls", 0)

        runs: list[dict] = []
        n = len(comps)
        retrieval_started = time.time()
        for j, comp in enumerate(comps):
            info = COMPONENT_INFO[comp]
            rec = {"id": comp, "label": info["label"], "short": info["short"], "weight": info["weight"],
                   "source": "", "source_variant": "", "seconds": 0.0, "load_seconds": 0.0, "hits": [],
                   "llm_calls": 0, "embed_calls": 0, "judge_api_calls": 0, "skipped": "", "note": ""}
            base, span = 0.05 + 0.6 * j / n, 0.6 / n
            ctx.progress(base, f"{info['label']} で検索")
            sub = dataclasses.replace(
                ctx, progress=lambda f, m, b=base, s=span, lab=info["short"]:
                ctx.progress(b + s * max(0.0, min(1.0, f)), f"{lab}: {m}"))
            c0, e0 = llm_stats.get("chat_calls", 0), llm_stats.get("embed_calls", 0)
            try:
                hits = self._run(comp, rec, index, question, sub, corpus_rev, chunk_by_id)
            except Exception as e:  # noqa: BLE001  1つの方式の失敗で全体を止めない
                hits = []
                rec["skipped"] = f"{info['label']} の検索に失敗したため使いませんでした: {type(e).__name__}: {e}"
            rec["llm_calls"] = llm_stats.get("chat_calls", 0) - c0
            rec["embed_calls"] = llm_stats.get("embed_calls", 0) - e0
            rec["hits"] = [[cid, round(float(s), 4)] for cid, s in hits if cid in chunk_by_id]
            if rec["skipped"]:
                ctx.log(rec["skipped"])
            elif rec["note"]:
                ctx.log(f"{info['label']}: {rec['note']}")
            runs.append(rec)
        retrieval_seconds = round(time.time() - retrieval_started, 3)

        used = [r for r in runs if not r["skipped"]]
        skipped = [r for r in runs if r["skipped"]]
        ctx.progress(0.68, "RRF で統合")
        fused = rrf([([cid for cid, _ in r["hits"]], r["weight"]) for r in used], position)
        fused_score = dict(fused)
        fused_rank = {cid: i for i, (cid, _) in enumerate(fused, 1)}
        order = [cid for cid, _ in fused]

        # 再順位付け（任意）
        rr = {"selected": rerank, "used": False, "seconds": 0.0, "llm_calls": 0, "note": ""}
        if rerank and len(order) >= 2:
            ctx.progress(0.72, "LLM で並べ替え")
            started, c0 = time.time(), llm_stats.get("chat_calls", 0)
            head = order[:RERANK_CANDIDATES]
            try:
                new, reason = self._rerank(ctx, question, [chunk_by_id[c] for c in head])
                if reason:
                    rr["note"] = f"並べ替えの出力を読めなかったため、RRF の順のまま使いました（{reason}）"
                else:
                    rr["used"] = True
                    order = [head[i] for i in new] + order[len(head):]
            except Exception as e:  # noqa: BLE001
                rr["note"] = f"並べ替えに失敗したため、RRF の順のまま使いました: {type(e).__name__}: {e}"
            rr["seconds"] = round(time.time() - started, 3)
            rr["llm_calls"] = llm_stats.get("chat_calls", 0) - c0
            if rr["note"]:
                ctx.log(rr["note"])
        elif rerank:
            rr["note"] = "候補が 2 件未満のため並べ替えませんでした"

        top = order[:TOP_K]
        passages = [chunk_by_id[c] for c in top]
        ctx.progress(0.8, "回答生成")
        started, c0 = time.time(), llm_stats.get("chat_calls", 0)
        think = ""
        if not passages:
            answer = "選んだ方式のどれでも関連するチャンクが見つかりませんでした。"
            if skipped:
                answer += "（使えなかった方式があります。下の注意を確認してください）"
            mode_used = "combo"
        elif ctx.chat_ok:
            answer, think = generate_answer(ctx, question, passages, task_tag="combo_answer")
            mode_used = "combo(RRF＋並べ替え)" if rr["used"] else "combo(RRF)"
        else:
            answer = extractive_answer(question, passages)
            mode_used = "combo(抽出)"
        answer_seconds = round(time.time() - started, 3)
        answer_calls = llm_stats.get("chat_calls", 0) - c0

        # 出典: 統合後の順位・RRF スコアと、方式ごとの順位・スコア
        ranks = {r["id"]: {cid: (i, s) for i, (cid, s) in enumerate(r["hits"], 1)} for r in used}
        citations = []
        for rank, cid in enumerate(top, 1):
            cite = chunk_citation(chunk_by_id[cid])
            cite.update(score=round(fused_score[cid], 5), rank=rank, fused_rank=fused_rank[cid])
            cite["why"] = [{"component": r["id"], "label": r["label"], "short": r["short"],
                            "rank": ranks[r["id"]][cid][0], "score": ranks[r["id"]][cid][1],
                            "weight": r["weight"]}
                           for r in used if cid in ranks[r["id"]]]
            citations.append(cite)

        judge_calls = sum(r["llm_calls"] for r in runs if r["id"] == "decision")
        judge_api_calls = sum(r["judge_api_calls"] for r in runs)
        llm_total = llm_stats.get("chat_calls", 0) - chat0
        warnings = [r["skipped"] for r in skipped] + [r["warning"] for r in used if r.get("warning")]
        if rr["note"] and rerank:
            warnings.append(rr["note"])
        stats = {"components": "＋".join(r["short"] for r in used) or "なし"}
        if skipped:
            stats["skipped"] = "・".join(r["short"] for r in skipped)
        stats.update(candidates=len(fused), retrieved=len(passages), llm_calls=llm_total)
        if judge_api_calls:
            stats["judge_api_calls"] = judge_api_calls
        stats.update(retrieval_seconds=retrieval_seconds, answer_seconds=answer_seconds)
        return {
            "answer": answer, "think": think, "mode": mode_used, "citations": citations,
            "variant": variant_of([r["id"] for r in used] or comps, rerank,
                                  skipped=[{"short": r["short"]} for r in skipped]),
            "warnings": warnings,
            "stats": stats,
            "combo": {
                "selected": comps, "used": [r["id"] for r in used],
                "skipped": [{"id": r["id"], "label": r["label"], "short": r["short"], "reason": r["skipped"]}
                            for r in skipped],
                "components": runs, "rerank": rr,
                "fused": [[cid, round(s, 6)] for cid, s in fused],
                "fusion": {"method": "RRF", "k": RRF_K, "weights": {r["id"]: r["weight"] for r in runs}},
                "top_k": TOP_K, "candidates": CANDIDATES,
                "llm_calls": {"total": llm_total, "judge": judge_calls, "rerank": rr["llm_calls"],
                              "answer": answer_calls},
                "judge_api_calls": judge_api_calls,
                "embed_calls": llm_stats.get("embed_calls", 0) - embed0,
                "timings": {"retrieval": retrieval_seconds, "rerank": rr["seconds"], "answer": answer_seconds},
            },
        }
