"""相談（RAG）パイプライン。

    質問 ─▶ ルーター ─▶ 戦略別の検索 ─▶ 根拠の十分性チェック ─▶（再検索）─▶ 再順位付け ─▶ 回答生成
            │            fact     : DecisionRAG（階層を判定モデルでたどる）+ Hybrid（BM25 + 埋め込み、RRF）
            │            relation : グラフ PPR（HippoRAG 2）+ Hybrid
            │            summary  : 文書要約（RAPTOR）+ コミュニティ要約（LazyGraphRAG）+ Hybrid
            │            compare  : DecisionRAG + 対象ごとのマルチクエリ（RAG-Fusion）
            │            chat     : 検索しない（Adaptive-RAG / D2-RAG の「検索要否」判定）
            └ 会話履歴から代名詞・省略を補った単独質問へ書き換え

データ区分（社外秘 / 公開）に応じて、各 LLM 呼び出しのプロファイル（ローカル / クラウド）を選ぶ。
社外秘の資料を1つでも根拠に含む回答はローカル LLM で生成する。
"""
from __future__ import annotations

import math
import re
import time
from typing import Iterator

from . import db, decision, decision_api, folders, graph
from .config import embed_space, load_config
from .llm import LLM, LLMError, strip_think
from .retrieval import INDEX, load_chunks
from .textutil import BM25Index, as_str_list, extract_json, rrf_fuse

JUDGE_LABELS = {"llm": "LLM", "jev": "Jev",
                **{k: v["label"] for k, v in decision_api.PROVIDERS.items()}}

ANSWER_SYSTEM = (
    "あなたは社内文書管理システムのアシスタントです。"
    "質問には、与えられた参考資料に書かれていることを根拠に、日本語で正確かつ簡潔に答えます。"
    "根拠にした資料は文末に [S1] [D2] [C1] のように資料番号で示します（複数可）。"
    "資料に書かれていないことは推測で補わず「資料からは分かりません」と述べ、"
    "一般知識で補足する場合は「（一般的には）」と明示します。"
    "数値・日付・固有名詞は資料の表記どおりに書きます。"
)
CHAT_SYSTEM = (
    "あなたは社内文書管理システムのアシスタントです。日本語で丁寧かつ簡潔に答えます。"
    "この質問では文書検索を行っていません。社内固有の事実について聞かれた場合は、"
    "推測せず、文書を指定して質問し直すよう案内してください。"
)

ROUTER_PROMPT = """[TASK:route]
あなたは社内文書検索システムの質問ルーターです。会話履歴と最新の質問を読み、次の JSON だけを出力してください。

{{"retrieval": true, "type": "fact", "standalone": "単独で意味の通る質問文", "keywords": ["検索語"], "entities": ["対象"]}}

- retrieval: 文書を検索する必要があるか。挨拶・お礼・雑談・一般常識・直前の回答の言い換え/翻訳/整形の依頼は false
- type: fact=特定の事実・数値・手順・定義 / relation=人・組織・製品・設備などのつながり・経緯・因果・影響 /
        summary=文書や資料群の全体要約・傾向・一覧・論点整理 / compare=複数の対象の比較・違い / chat=雑談
- standalone: 会話履歴を踏まえ、代名詞（それ・この資料 等）や省略を補った質問文
- keywords: 検索に使う具体的な語（固有名詞・型番・専門用語）3〜8 個
- entities: 質問に出てくる固有の対象（人・組織・製品・設備・規格など）

# 会話履歴（古い順）
{history}

# 最新の質問
{question}
"""

GRADE_PROMPT = """[TASK:grade]
質問に答えるのに、以下の資料で十分かを判定してください。次の JSON だけを出力します。
{{"sufficient": true, "missing": "不足している情報（十分なら空）", "query": "不足を補うための検索文（十分なら空）"}}

# 質問
{question}

# 資料
{passages}
"""

RERANK_PROMPT = """[TASK:rerank]
質問に答える根拠として役立つ順に、資料の番号を並べ替えてください。無関係な資料は除外します。
次の JSON だけを出力: {{"ranking": [3, 1, 5]}}

# 質問
{question}

# 資料
{passages}
"""

_CHAT_RE = re.compile(r"^(こんにちは|こんばんは|おはよう|ありがとう|どうも|はじめまして|よろしく|"
                      r"お疲れ|thanks|thank you|hello|hi)\W*$", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"要約|まとめ|概要|全体|傾向|一覧|総括|論点|ポイント|要点|サマリ|俯瞰|全部")
_RELATION_RE = re.compile(r"関係|関連|つながり|繋がり|経緯|影響|原因|なぜ|理由|誰が|どの部署|関与|背景|因果")
_COMPARE_RE = re.compile(r"比較|違い|差|異なる|どちら|vs|ＶＳ|対比")
_REWRITE_RE = re.compile(r"^(もっと|より)?(短く|詳しく|簡単に|わかりやすく|分かりやすく|箇条書き|表に|英語で|翻訳)")


# ---------------------------------------------------------------- 文脈・プロファイル選択

class Ctx:
    """1回の相談の実行文脈（設定・LLM・区分・統計）。"""

    def __init__(self, chat: dict, cfg: dict):
        self.cfg = cfg
        self.llm = LLM(cfg)
        self.chat = chat
        self.mode = chat.get("mode") or cfg["policy"]["chat_mode"]
        if self.mode not in ("auto", "local", "cloud"):
            self.mode = "auto"
        self.touched = bool(chat.get("touched_internal"))
        self.steps: list[dict] = []
        self.warnings: list[str] = []
        self.t0 = time.time()

    def step(self, name: str, detail: str = "") -> dict:
        s = {"name": name, "detail": detail, "t": round(time.time() - self.t0, 2)}
        self.steps.append(s)
        return s

    def profile(self, kind: str, sensitivity: str) -> str | None:
        """kind: answer | helper。sensitivity: 送るデータの区分。使えるプロファイルか None。"""
        local_ok, cloud_ok = self.llm.ready("local"), self.llm.ready("cloud")
        if sensitivity != "public" or self.mode == "local":
            return "local" if local_ok else None
        if self.mode == "cloud":
            return "cloud" if cloud_ok else None
        order = ("cloud", "local") if kind == "answer" else ("local", "cloud")
        for name in order:
            if (name == "local" and local_ok) or (name == "cloud" and cloud_ok):
                return name
        return None

    @property
    def history_sensitivity(self) -> str:
        return "internal" if self.touched else "public"


def allowed_docs(chat: dict, mode: str) -> tuple[set[str], dict[str, str]]:
    """相談先（フォルダ・個別の文書・タグ）とモードから検索対象の文書集合を作る。"""
    scope = chat.get("scope") or {}
    allowed: set[str] = set()
    sens: dict[str, str] = {}
    for d in db.list_docs():
        sens[d["id"]] = d["sensitivity"]
        if d["status"] not in ("ready", "processing", "queued") or not d["chars"]:
            continue
        if not folders.in_scope(d, scope):
            continue
        if mode == "cloud" and d["sensitivity"] != "public":
            continue                     # クラウドモードでは社外秘を検索対象から外す
        allowed.add(d["id"])
    return allowed, sens


# ---------------------------------------------------------------- ルーター

def heuristic_route(question: str) -> dict:
    q = question.strip()
    if _CHAT_RE.match(q) or len(q) <= 3:
        typ, retrieval = "chat", False
    elif _REWRITE_RE.match(q):
        typ, retrieval = "chat", False
    elif _COMPARE_RE.search(q):
        typ, retrieval = "compare", True
    elif _SUMMARY_RE.search(q):
        typ, retrieval = "summary", True
    elif _RELATION_RE.search(q):
        typ, retrieval = "relation", True
    else:
        typ, retrieval = "fact", True
    return {"retrieval": retrieval, "type": typ, "standalone": q, "keywords": [], "entities": [],
            "by": "rule"}


def route(ctx: Ctx, question: str, history: list[dict]) -> dict:
    plan = heuristic_route(question)
    if not ctx.cfg["rag"]["router"]:
        return plan
    prof = ctx.profile("helper", ctx.history_sensitivity)
    if not prof:
        return plan
    hist = "\n".join(f"{'ユーザー' if m['role'] == 'user' else 'アシスタント'}: {m['content'][:400]}"
                     for m in history[-6:]) or "（なし）"
    try:
        text = ctx.llm.chat(prof, ROUTER_PROMPT.format(history=hist, question=question),
                            sensitivity=ctx.history_sensitivity,
                            system="指示された JSON だけを出力してください。",
                            max_tokens=1024, purpose="router")
    except LLMError as e:
        ctx.warnings.append(f"ルーターを使えませんでした（規則で判定）: {e}")
        return plan
    data = extract_json(text)
    if not isinstance(data, dict):
        return plan
    typ = str(data.get("type", "")).strip().lower()
    if typ not in ("fact", "relation", "summary", "compare", "chat"):
        typ = plan["type"]
    retrieval = data.get("retrieval")
    if not isinstance(retrieval, bool):
        retrieval = typ != "chat"
    if typ == "chat":
        retrieval = False
    standalone = str(data.get("standalone") or "").strip() or question
    return {"retrieval": retrieval, "type": typ, "standalone": standalone[:1000],
            "keywords": as_str_list(data.get("keywords"), limit=8, maxlen=40),
            "entities": as_str_list(data.get("entities"), limit=6, maxlen=40), "by": prof}


# ---------------------------------------------------------------- 検索

def _qvecs(ctx: Ctx, text: str, allowed: set[str]) -> dict[str, list[float]]:
    """質問の埋め込み（検索対象に含まれる空間ごと）。送ってよいプロファイルだけ使う。"""
    out = {}
    for space in INDEX.spaces_for(allowed):
        prof = space.split(":", 1)[0]
        if prof not in ("local", "cloud") or space != embed_space(ctx.cfg, prof):
            continue                       # 設定変更で埋め込みモデルが変わった空間は使えない
        if prof == "cloud" and (ctx.mode == "local" or ctx.touched):
            continue
        try:
            out[space] = ctx.llm.embed(prof, [text[:2000]],
                                       sensitivity="public" if prof == "cloud" else "internal")[0]
        except LLMError as e:
            ctx.warnings.append(f"質問の埋め込みに失敗（字句検索のみ）: {e}")
    return out


def _signal_name(name: str) -> str:
    if name == "bm25":
        return "BM25（全文の語の一致）"
    space = name.split(":", 1)[1] if ":" in name else name
    prof, _, model = space.partition(":")
    return f"埋め込み（{model or space}・{'ローカル' if prof == 'local' else 'クラウド'}）"


def _hybrid_detail(ctx, text, allowed, k, qvecs=None) -> tuple[list[tuple[str, float]], dict[str, list]]:
    """ハイブリッド検索（BM25 + 埋め込みの RRF）と、チャンクごとの内訳（信号ごとの順位・スコア）。"""
    sig = INDEX.signals(text, allowed_docs=allowed, k=k,
                        qvecs=qvecs if qvecs is not None else _qvecs(ctx, text, allowed))
    if not sig:
        return [], {}
    fused = rrf_fuse([[cid for cid, _ in ranked] for _, ranked in sig])[:k]
    sub: dict[str, list] = {}
    for name, ranked in sig:
        label = _signal_name(name)
        note = "BM25 スコア（語の一致の強さ）" if name == "bm25" else "コサイン類似度（1 に近いほど意味が近い）"
        for r, (cid, sc) in enumerate(ranked, 1):
            sub.setdefault(cid, []).append({"name": label, "rank": r, "score": round(float(sc), 4),
                                            "note": note, "sub": True})
    return fused, sub


def _hybrid(ctx, text, allowed, k, qvecs=None):
    return _hybrid_detail(ctx, text, allowed, k, qvecs)[0]


class Fusion:
    """検索の信号（DecisionRAG・ハイブリッド・グラフ PPR…）を RRF で融合し、採用根拠を記録する。

    why[chunk_id] = {"signals": [{name, rank, score, weight, note}], "fused": RRF スコア, "fused_rank": 順位}
    画面では出典にマウスを載せると、この内訳（どの検索で何位・スコアいくつ）が出る。
    """

    def __init__(self):
        self.rankings: list[list[str]] = []
        self.weights: list[float] = []
        self.why: dict[str, dict] = {}

    def add(self, name: str, ranked: list[tuple[str, float]], weight: float, *, note: str = "",
            sub: dict[str, list] | None = None) -> None:
        if not ranked:
            return
        self.rankings.append([cid for cid, _ in ranked])
        self.weights.append(weight)
        for r, (cid, sc) in enumerate(ranked, 1):
            w = self.why.setdefault(cid, {"signals": []})
            w["signals"].append({"name": name, "rank": r, "score": round(float(sc), 4), "weight": weight,
                                 "note": note})
            if sub and cid in sub:
                w["signals"].extend(sub[cid])

    def fuse(self) -> list[str]:
        if not self.rankings:
            return []
        fused = rrf_fuse(self.rankings, weights=self.weights)
        for rank, (cid, sc) in enumerate(fused, 1):
            self.why.setdefault(cid, {"signals": []}).update(fused=round(sc, 5), fused_rank=rank)
        return [cid for cid, _ in fused]


def retrieve(ctx: Ctx, plan: dict, allowed: set[str], doc_sens: dict[str, str]) -> dict:
    """戦略別に検索し {passages, graph, communities, doc_summaries, via} を返す。"""
    k = int(ctx.cfg["rag"]["top_k"])
    q = plan["standalone"]
    query_text = " ".join([q, *plan["keywords"]])
    ev = {"passages": [], "graph": None, "communities": [], "doc_summaries": [], "via": [],
          "decision": None, "why": {}, "methods": []}
    fusion = Fusion()
    strategy = ctx.chat.get("strategy") or "auto"

    # DecisionRAG: 検索器を使わず階層をたどる（自動では事実・比較の質問に使う）
    dec_rank: list[str] = []
    if ctx.cfg["rag"]["decision"] and strategy != "classic" and (
            strategy == "decision" or plan["type"] in ("fact", "compare")):
        api_client = None
        if ctx.cfg["rag"].get("decision_judge") == "api":
            if decision_api.ready(ctx.cfg):
                api_client = decision_api.DecisionAPIClient(decision_api.effective(ctx.cfg))
            else:
                e = decision_api.effective(ctx.cfg)
                ctx.warnings.append(f"DecisionRAG の判定モデルに判定 API（{e['label']}）が選ばれていますが、"
                                    f"{decision_api.missing(ctx.cfg)} が未設定です（LLM で判定します）")
        beam = int(ctx.chat.get("decision_beam") or 0) or int(ctx.cfg["rag"]["decision_beam"])
        res = decision.navigate(ctx, q, allowed, k=k * 2, beam=beam,
                                budget=int(ctx.cfg["rag"]["decision_budget"]),
                                use_choice=bool(ctx.cfg["rag"].get("decision_choice", True)),
                                api_client=api_client)
        ev["decision"] = {"path": res["path"], "calls": res["calls"], "soft": res["soft"],
                          "beam": res["beam"],
                          "hits": len(res["hits"]), "error": res["error"],
                          "choice": res["choice"], "judges": res["judges"],
                          "api": res["api"]}
        if res["error"]:
            ctx.warnings.append(f"DecisionRAG: {res['error']}")
        if res["api"] and res["api"]["refusals"]:
            ctx.warnings.append(f"DecisionRAG: {res['api']['label']} が {res['api']['refusals']} 件の候補の"
                                "判定を断りました（関連なしとして扱いました）")
        route_txt = " → ".join("・".join(f"{x['title']}({x['path']})" for x in lv["kept"][:2])
                               for lv in res["path"] if lv.get("kept"))
        judge_txt = "・".join(JUDGE_LABELS.get(j, j) for j in res["judges"]) or "なし"
        ctx.step("DecisionRAG", f"ビーム幅 {res['beam']}・判定 {res['calls']} 回（{judge_txt}{'・確率' if res['soft'] else ''}"
                 f"{'・選択式で並べ替え' if res['choice'] else ''}）・段落 {len(res['hits'])} 件"
                 + (f": {route_txt}" if route_txt else ""))
        dec_rank = [cid for cid, _ in res["hits"]]
        if dec_rank:
            ev["via"].append(f"DecisionRAG（ビーム幅 {res['beam']}・展開 {len(res['path'])} 回・"
                             f"判定 {res['calls']} 回）")
            ev["methods"].append({"id": "decision", "label": "DecisionRAG",
                                  "detail": f"文書の階層を判定モデル（{judge_txt}）でたどる・判定 {res['calls']} 回",
                                  "hits": len(dec_rank)})
    dec_note = "経路スコア（たどった各段の判定確率の幾何平均）" + ("・選択式で並べ替え済み" if dec_rank and
                                                                 ev["decision"] and ev["decision"]["choice"] else "")
    if strategy == "decision":
        if dec_rank:
            # 論文どおり第1段の検索器を使わない
            fusion.add("DecisionRAG", res["hits"], 1.0, note=dec_note)
            fusion.fuse()
            ev["why"] = fusion.why
            ev["passages"] = _diversify(dec_rank, k=k * 2, per_doc=None)
            return ev
        ctx.warnings.append("DecisionRAG で段落が見つからなかったため、従来の検索で補いました")
    if dec_rank:
        fusion.add("DecisionRAG", res["hits"], 1.5, note=dec_note)

    qv = _qvecs(ctx, query_text, allowed)
    base, sub = _hybrid_detail(ctx, query_text, allowed, k * 3, qv)
    fusion.add("ハイブリッド（BM25＋埋め込み）" if qv else "BM25（全文の語の一致）",
               base if qv else [(cid, next((x["score"] for x in sub.get(cid, []) if x["rank"]), sc))
                                for cid, sc in base],
               1.0, note="BM25 と埋め込みの RRF スコア" if qv else "BM25 スコア", sub=sub if qv else None)
    ev["via"].append("ハイブリッド（BM25+埋め込み）" if qv else "BM25")
    ev["methods"].append({"id": "hybrid" if qv else "bm25",
                          "label": "ハイブリッド（BM25＋埋め込み）" if qv else "BM25（全文検索）",
                          "detail": ("語の一致（BM25）と意味の近さ（埋め込みモデル: "
                                     + "・".join(sp.partition(":")[2] or sp for sp in qv) + "）を RRF で統合")
                          if qv else "語の一致で探す（埋め込みモデル未設定のため意味検索なし）",
                          "hits": len(base)})

    if plan["type"] in ("relation", "compare", "summary") or plan["entities"]:
        g = graph.get_graph(allowed, doc_sens)
        if g.entities:
            seeds = g.seed_entities(q, plan["keywords"] + plan["entities"])
            if seeds:
                rank = g.ppr(seeds)
                graph_ranked = g.rank_chunks(rank, k=k * 3)
                if graph_ranked:
                    # relation は グラフを主、他は補助として融合
                    fusion.add("GraphRAG（グラフ PPR）", graph_ranked, 1.5 if plan["type"] == "relation" else 0.8,
                               note="質問のエンティティから Personalized PageRank でたどったスコア")
                    ev["via"].append("グラフ PPR")
                    ev["methods"].append({"id": "graph", "label": "GraphRAG",
                                          "detail": "質問のエンティティ（"
                                          + "・".join(g.entities[s]["name"] for s in list(seeds)[:3])
                                          + "）から関係をたどる（Personalized PageRank）・"
                                          + graph_modes_text(allowed),
                                          "hits": len(graph_ranked)})
                ctxg = g.context(rank)
                if ctxg["entities"]:
                    ev["graph"] = {
                        "entities": [{"name": e["name"], "type": e["type"],
                                      "description": e["description"][:240],
                                      "sensitivity": e["sensitivity"]} for e in ctxg["entities"]],
                        "relations": [{"source": g.entities[r["source"]]["name"],
                                       "target": g.entities[r["target"]]["name"],
                                       "description": r["description"][:200]}
                                      for r in ctxg["relations"]],
                        "sensitivity": "internal" if any(
                            e["sensitivity"] != "public" for e in ctxg["entities"]) else "public",
                    }

    if plan["type"] == "compare":
        targets = (plan["entities"] or plan["keywords"])[:4]
        for t in targets:
            fusion.add(f"マルチクエリ「{t[:16]}」", _hybrid(ctx, f"{t} {q}", allowed, k), 1.0,
                       note="比較の対象ごとに検索した RRF スコア")
        if targets:
            ev["via"].append(f"マルチクエリ（{len(targets)}対象）")
            ev["methods"].append({"id": "multi", "label": "マルチクエリ",
                                  "detail": "比較の対象ごとに検索: " + "・".join(targets), "hits": 0})

    if plan["type"] == "summary":
        ev["doc_summaries"] = _doc_summaries(q + " " + " ".join(plan["keywords"]), allowed, limit=5)
        if ev["doc_summaries"]:
            ev["via"].append("文書要約")
            ev["methods"].append({"id": "summary", "label": "文書要約（RAPTOR）",
                                  "detail": "取り込み時に作った文書ごとの要約", "hits": len(ev["doc_summaries"])})
        scope_docs = (ctx.chat.get("scope") or {}).get("doc_ids") or []
        if len(allowed) > 3 and not (0 < len(scope_docs) <= 3):
            ev["communities"] = _communities(ctx, q, plan, allowed, doc_sens)
            if ev["communities"]:
                ev["via"].append("コミュニティ要約")
                ev["methods"].append({"id": "community", "label": "コミュニティ要約（LazyGraphRAG）",
                                      "detail": "グラフで関連の強いまとまりを、質問時に必要な分だけ要約",
                                      "hits": len(ev["communities"])})
        else:
            # 特定文書の要約: 文書全体からまんべんなく代表チャンクを取る
            spread = []
            for d in allowed:
                chs = db.doc_chunks(d)
                step = max(1, len(chs) // 6)
                spread.extend(c["id"] for c in chs[::step][:6])
            fusion.add("文書全体から均等に", [(cid, 0.0) for cid in spread], 1.0,
                       note="要約の質問のため、文書の最初から最後まで均等に取った箇所")

    fused = fusion.fuse()
    ev["why"] = fusion.why
    ev["passages"] = _diversify(fused, k=k * 2, per_doc=None if len(allowed) <= 2 else 4)
    return ev


def graph_modes_text(allowed: set[str]) -> str:
    """検索対象の文書のグラフが、どの方式・どの LLM で作られたか（回答の「RAG の流れ」に出す）。"""
    from .readings import MODE_LABEL
    counts: dict[str, int] = {}
    for r in db.query("SELECT doc_id, mode, label FROM readings WHERE stage='graph' AND active=1"):
        if r["doc_id"] in allowed:
            key = MODE_LABEL.get(r["mode"] or "standard", r["mode"])
            if r["mode"] != "terms":
                key += f": {r['label']}"
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "グラフのある文書なし"
    return "グラフ " + "／".join(f"{k}（{n} 文書）" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def _diversify(cids: list[str], *, k: int, per_doc: int | None) -> list[dict]:
    chunks = load_chunks(cids[:k * 3])
    out, per = [], {}
    for cid in cids:
        ch = chunks.get(cid)
        if not ch:
            continue
        if per_doc and per.get(ch["doc_id"], 0) >= per_doc:
            continue
        per[ch["doc_id"]] = per.get(ch["doc_id"], 0) + 1
        out.append(ch)
        if len(out) >= k:
            break
    return out


def _doc_summaries(query: str, allowed: set[str], *, limit: int) -> list[dict]:
    docs = [d for d in db.list_docs() if d["id"] in allowed and d["summary"]]
    if not docs:
        return []
    if len(docs) <= limit:
        return docs
    idx = BM25Index()
    for d in docs:
        idx.add(d["id"], f"{d['title']} {' '.join(d['keywords'])} {d['summary']}")
    hits = idx.search(query, k=limit)
    order = [key for key, _ in hits]
    for key, sc in hits:
        by_id = next(d for d in docs if d["id"] == key)
        by_id["_score"] = round(float(sc), 3)
    for d in docs:                      # 字句が当たらない一般的な要約依頼でも新しい順に補完
        if len(order) >= limit:
            break
        if d["id"] not in order:
            order.append(d["id"])
    by = {d["id"]: d for d in docs}
    return [by[i] for i in order[:limit]]


def _communities(ctx: Ctx, q: str, plan: dict, allowed: set[str], doc_sens) -> list[dict]:
    """関連の高いコミュニティだけ要約する（LazyGraphRAG 方式。要約はキャッシュ）。"""
    g = graph.get_graph(allowed, doc_sens)
    coms = [c for c in g.communities() if c["size"] >= 2]
    if not coms:
        return []
    idx = BM25Index()
    for c in coms:
        idx.add(c["id"], " ".join(f"{g.entities[m]['name']} {g.entities[m]['description'][:80]}"
                                  for m in c["members"][:40]))
    hits = idx.search(" ".join([q, *plan["keywords"]]), k=len(coms))
    score = {cid: s for cid, s in hits}
    ranked = sorted(coms, key=lambda c: -(score.get(c["id"], 0) + 0.3 * math.log(1 + c["size"])))[:5]
    out = []
    for rank, c in enumerate(ranked, 1):
        prof = ctx.profile("helper", c["sensitivity"])
        try:
            s = graph.summarize_community(ctx.llm, prof, g, c)
        except LLMError as e:
            ctx.warnings.append(f"コミュニティ要約に失敗: {e}")
            s = graph.summarize_community(None, None, g, c)
        out.append({"id": c["id"], "title": s["title"], "summary": s["summary"],
                    "size": c["size"], "sensitivity": c["sensitivity"],
                    "why": {"signals": [{"name": "コミュニティの BM25", "rank": rank,
                                         "score": round(float(score.get(c["id"], 0)), 3),
                                         "note": "質問とメンバーのエンティティ名・説明の語の一致"
                                                 f"（エンティティ {c['size']} 件。大きいまとまりを少し優先）"}]}})
    return out


# ---------------------------------------------------------------- 根拠チェック・再順位付け

def _passage_lines(passages: list[dict], limit: int = 600) -> str:
    return "\n\n".join(f"[{i + 1}] （{p['doc_title']} {p['locator']}）{p['text'][:limit]}"
                       for i, p in enumerate(passages))


def _sens_of(passages: list[dict]) -> str:
    return "internal" if any(p["sensitivity"] != "public" for p in passages) else "public"


def corrective(ctx: Ctx, plan: dict, ev: dict, allowed, doc_sens) -> dict:
    """CRAG: 資料が不十分なら書き換えた検索文で再検索して統合する（1回まで）。"""
    if not ctx.cfg["rag"]["corrective"] or plan["type"] not in ("fact", "relation", "compare"):
        return ev
    top = ev["passages"][:6]
    if not top:
        return ev
    sens = "internal" if _sens_of(top) == "internal" or ctx.touched else "public"
    prof = ctx.profile("helper", sens)
    if not prof:
        return ev
    try:
        text = ctx.llm.chat(prof, GRADE_PROMPT.format(question=plan["standalone"],
                                                      passages=_passage_lines(top, 500)),
                            sensitivity=sens, system="指示された JSON だけを出力してください。",
                            max_tokens=1024, purpose="grade")
    except LLMError as e:
        ctx.warnings.append(f"根拠チェックを省略: {e}")
        return ev
    data = extract_json(text)
    if not isinstance(data, dict) or data.get("sufficient") is not False:
        ctx.step("根拠チェック", "十分")
        return ev
    new_q = str(data.get("query") or "").strip()
    if not new_q or new_q == plan["standalone"]:
        ctx.step("根拠チェック", "不足（再検索語なし）")
        return ev
    ctx.step("根拠チェック", f"不足 → 再検索: {new_q[:60]}")
    alt = dict(plan, standalone=new_q,
               type="relation" if plan["type"] == "fact" else plan["type"])
    saved = ctx.chat.get("strategy")
    ctx.chat = dict(ctx.chat, strategy="classic")       # 再検索では判定を繰り返さない
    try:
        ev2 = retrieve(ctx, alt, allowed, doc_sens)
    finally:
        ctx.chat = dict(ctx.chat, strategy=saved)
    seen = {p["id"] for p in ev["passages"]}
    merged = ev["passages"][:4] + [p for p in ev2["passages"] if p["id"] not in seen] \
        + ev["passages"][4:]
    ev["passages"] = merged[: len(ev["passages"]) + 4]
    for p in ev2["passages"]:
        if p["id"] not in seen:
            w = dict(ev2.get("why", {}).get(p["id"]) or {"signals": []})
            w["crag"] = new_q[:80]
            ev.setdefault("why", {})[p["id"]] = w
    ev["via"].append("再検索（CRAG）")
    ev.setdefault("methods", []).append({"id": "crag", "label": "再検索（CRAG）",
                                         "detail": f"根拠が足りないと判定 → 「{new_q[:40]}」で再検索",
                                         "hits": len([p for p in ev2["passages"] if p["id"] not in seen])})
    if ev2.get("graph") and not ev.get("graph"):
        ev["graph"] = ev2["graph"]
    return ev


def rerank(ctx: Ctx, question: str, passages: list[dict], k: int, why: dict | None = None) -> list[dict]:
    if not ctx.cfg["rag"]["rerank"] or len(passages) <= k:
        return passages[:k]
    cand = passages[:20]
    sens = "internal" if _sens_of(cand) == "internal" or ctx.touched else "public"
    prof = ctx.profile("helper", sens)
    if not prof:
        return passages[:k]
    try:
        text = ctx.llm.chat(prof, RERANK_PROMPT.format(question=question,
                                                       passages=_passage_lines(cand, 300)),
                            sensitivity=sens, system="指示された JSON だけを出力してください。",
                            max_tokens=1024, purpose="grade")
    except LLMError:
        return passages[:k]
    data = extract_json(text)
    order = data.get("ranking") if isinstance(data, dict) else data
    if not isinstance(order, list):
        return passages[:k]
    picked = []
    for x in order:
        try:
            i = int(x) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(cand) and cand[i] not in picked:
            picked.append(cand[i])
    if not picked:
        return passages[:k]
    ctx.step("再順位付け", f"{len(cand)}件 → {min(k, len(picked))}件")
    if why is not None:
        for i, p in enumerate(picked[:k], 1):
            why.setdefault(p["id"], {"signals": []})["rerank"] = {"rank": i, "before": cand.index(p) + 1}
    return picked[:k]


# ---------------------------------------------------------------- 回答

def build_context(ev: dict) -> tuple[str, list[dict]]:
    """回答プロンプトの参考資料ブロックと、出典リスト（番号つき）を作る。"""
    blocks, sources = [], []
    for i, p in enumerate(ev["passages"], 1):
        head = f"[S{i}] 文書「{p['doc_title']}」{p['locator']}" + \
               (f"（{p['heading'][:40]}）" if p.get("heading") else "")
        blocks.append(f"{head}\n{p['text'][:1800]}")
        why = dict((ev.get("why") or {}).get(p["id"]) or {})
        why["position"] = i
        sources.append({"ref": f"S{i}", "kind": "chunk", "doc_id": p["doc_id"],
                        "doc_title": p["doc_title"], "locator": p["locator"], "page": p["page"],
                        "chunk_id": p["id"], "ext": p.get("ext", ""),
                        "snippet": p["text"][:300], "sensitivity": p["sensitivity"], "why": why})
    for i, d in enumerate(ev["doc_summaries"], 1):
        blocks.append(f"[D{i}] 文書「{d['title']}」の要約\n{d['summary'][:1500]}")
        summary_reading = (d.get("flags") or {}).get("summary_label") or ""
        sources.append({"ref": f"D{i}", "kind": "doc", "doc_id": d["id"], "doc_title": d["title"],
                        "locator": "文書要約", "page": None, "snippet": d["summary"][:300],
                        "sensitivity": d["sensitivity"], "ext": d["ext"],
                        "why": {"position": i, "signals": [{
                            "name": "要約の BM25", "rank": i, "score": d.get("_score", 0),
                            "note": "質問と文書要約・キーワードの語の一致"
                                    + (f"（要約: {summary_reading}）" if summary_reading else "")}]}})
    for i, c in enumerate(ev["communities"], 1):
        blocks.append(f"[C{i}] テーマ「{c['title']}」（関連エンティティ {c['size']} 件の要約）\n"
                      f"{c['summary'][:1200]}")
        sources.append({"ref": f"C{i}", "kind": "community", "title": c["title"],
                        "doc_title": f"テーマ: {c['title']}", "locator": "コミュニティ要約",
                        "snippet": c["summary"][:300], "sensitivity": c["sensitivity"],
                        "why": {**(c.get("why") or {}), "position": i}})
    g = ev.get("graph")
    if g:
        ent = "\n".join(f"- {e['name']}（{e['type']}）: {e['description']}" for e in g["entities"])
        rel = "\n".join(f"- {r['source']} — {r['target']}: {r['description']}" for r in g["relations"])
        blocks.append(f"[G] ナレッジグラフ（文書から抽出した関係。出典番号は付けない）\n"
                      f"エンティティ:\n{ent}\n関係:\n{rel or '（なし）'}")
    return "\n\n".join(blocks), sources


def evidence_sensitivity(ev: dict) -> str:
    items = ([p["sensitivity"] for p in ev["passages"]]
             + [d["sensitivity"] for d in ev["doc_summaries"]]
             + [c["sensitivity"] for c in ev["communities"]]
             + ([ev["graph"]["sensitivity"]] if ev.get("graph") else []))
    return "internal" if any(s != "public" for s in items) else "public"


def extractive_answer(sources: list[dict], reason: str) -> str:
    if not sources:
        return f"関連する資料が見つかりませんでした。（{reason}）"
    lines = [f"（{reason}。関連箇所の抜粋を表示します）", ""]
    for s in sources[:5]:
        lines.append(f"- **{s['doc_title']}** {s.get('locator') or ''}: "
                     f"{s['snippet'][:200].replace(chr(10), ' ')} [{s['ref']}]")
    return "\n".join(lines)


def ask(chat_id: str, question: str) -> Iterator[dict]:
    """1問の相談を実行し、UI 向けイベントを順に返す（SSE で中継される）。"""
    cfg = load_config()
    chat = db.get_chat(chat_id)
    if not chat:
        yield {"type": "error", "message": "相談が見つかりません"}
        return
    question = question.strip()[:4000]
    history = [m for m in db.chat_messages(chat_id) if m["role"] in ("user", "assistant")]
    db.add_message(chat_id, "user", question)
    if chat["title"] in ("", "新しい相談"):
        db.update_chat(chat_id, title=question.replace("\n", " ")[:40])
    ctx = Ctx(chat, cfg)
    if ctx.mode == "cloud" and ctx.touched:
        # クラウドモードでは社外秘を検索しないので、履歴さえ送らなければクラウドで処理できる
        history = []
        ctx.touched = False
        ctx.warnings.append("この相談は社外秘資料を参照済みのため、クラウドへは会話履歴を送りません")

    # 1. ルーティング
    yield {"type": "status", "text": "質問を分析しています…"}
    plan = route(ctx, question, history)
    allowed, doc_sens = allowed_docs(chat, ctx.mode)
    if not allowed and plan["retrieval"]:
        ctx.warnings.append("検索対象の文書がありません（文書を取り込むか、相談の範囲を見直してください）")
    scope_info = folders.describe_scope(chat.get("scope") or {})
    route_info = {"type": plan["type"], "retrieval": plan["retrieval"] and bool(allowed),
                  "standalone": plan["standalone"], "keywords": plan["keywords"],
                  "entities": plan["entities"], "by": plan["by"], "mode": ctx.mode,
                  "docs_in_scope": len(allowed), "scope_label": scope_info["label"]}
    ctx.step("ルーター", f"{plan['type']}（{'検索する' if route_info['retrieval'] else '検索しない'}）")
    yield {"type": "route", "route": route_info}

    # 2. 検索
    ev = {"passages": [], "graph": None, "communities": [], "doc_summaries": [], "via": [],
          "decision": None, "why": {}, "methods": []}
    route_info["strategy"] = chat.get("strategy") or "auto"
    if route_info["retrieval"]:
        yield {"type": "status", "text": "資料を検索しています…"}
        ev = retrieve(ctx, plan, allowed, doc_sens)
        ctx.step("検索", " + ".join(ev["via"]) + f"（{len(ev['passages'])}件）")
        if ev["passages"]:
            yield {"type": "status", "text": "根拠を確認しています…"}
            ev = corrective(ctx, plan, ev, allowed, doc_sens)
        ev["passages"] = rerank(ctx, plan["standalone"], ev["passages"], int(cfg["rag"]["top_k"]),
                                why=ev.setdefault("why", {}))
        if any("rerank" in w for w in ev["why"].values()):
            ev["methods"].append({"id": "rerank", "label": "再順位付け", "detail": "LLM で根拠として役立つ順に並べ替え",
                                  "hits": len(ev["passages"])})

    context, sources = build_context(ev)
    sens = evidence_sensitivity(ev)
    if sens == "internal" or ctx.touched:
        hist_sens = "internal"
    else:
        hist_sens = "public"
    prof = ctx.profile("answer", hist_sens)
    yield {"type": "sources", "sources": sources, "via": ev["via"], "sensitivity": sens,
           "profile": prof}

    # 3. 回答生成（ストリーミング）
    messages = []
    for m in history[-6:]:
        messages.append({"role": m["role"], "content": m["content"][:2000]})
    if route_info["retrieval"]:
        user = (f"# 参考資料\n{context or '（該当する資料は見つかりませんでした）'}\n\n"
                f"# 質問\n{plan['standalone']}"
                + (f"\n（元の質問: {question}）" if plan["standalone"] != question else ""))
        system = ANSWER_SYSTEM
    else:
        user, system = question, CHAT_SYSTEM
    messages.append({"role": "user", "content": user})

    answer = ""
    if prof is None:
        reason = ("社外秘の資料を含むためローカル LLM が必要です（未設定）"
                  if hist_sens == "internal" and ctx.llm.ready("cloud")
                  else "LLM が未設定です（設定タブで接続先を登録してください）")
        answer = extractive_answer(sources, reason)
        yield {"type": "delta", "text": answer}
    else:
        yield {"type": "status", "text": f"回答を生成しています（{'ローカル' if prof == 'local' else 'クラウド'} LLM）…"}
        ctx.step("回答生成", "ローカル LLM" if prof == "local" else "クラウド LLM")
        from .indexer import INDEXER
        try:
            with INDEXER.chat_session():     # 取り込みのグラフ抽出には、回答が終わるまで LLM を待ってもらう
                for piece in ctx.llm.stream(prof, messages, sensitivity=hist_sens, system=system,
                                            purpose="answer"):
                    answer += piece
                    yield {"type": "delta", "text": piece}
        except LLMError as e:
            msg = f"\n\n⚠ 回答の生成に失敗しました: {e}"
            if not answer:
                msg = extractive_answer(sources, f"回答の生成に失敗: {e}")
            answer += msg
            yield {"type": "delta", "text": msg}
    answer = strip_think(answer) or answer

    used = sorted(set(re.findall(r"\[((?:S|D|C)\d+)\]", answer)))
    for s in sources:
        s["used"] = s["ref"] in used
    if route_info["retrieval"] and sens == "internal" and not chat["touched_internal"]:
        db.update_chat(chat_id, touched_internal=True)
    answer_model = ""
    if prof:
        pp = cfg["profiles"][prof]
        answer_model = pp.get("model") or ""
    meta = {"route": route_info, "sources": sources, "via": ev["via"], "profile": prof,
            "methods": ev.get("methods") or [], "answer_model": answer_model,
            "decision": ev.get("decision"),
            "sensitivity": sens, "steps": ctx.steps, "warnings": ctx.warnings,
            "elapsed": round(time.time() - ctx.t0, 2),
            "llm": {"calls": ctx.llm.stats["calls"], "embed_texts": ctx.llm.stats["embed_texts"]}}
    msg = db.add_message(chat_id, "assistant", answer, meta)
    yield {"type": "done", "message": msg}
