"""相談で使う RAG の選択（検索方式）。

検索方式（相談ごと。既定は設定タブ）:
  auto     … 自動: 質問の種類で方式を選ぶ（事実・比較 → DecisionRAG＋ハイブリッド、関係 → GraphRAG＋
              ハイブリッド、要約 → 文書要約＋コミュニティ要約）。各方式の ON/OFF は設定タブに従う
  fast     … 速さ優先: 全文（BM25）＋埋め込み＋GraphRAG。LLM は回答の1回だけ（ルーター・根拠チェックを省く）
  custom   … 選んで使う: チェックした方式を、質問の種類にかかわらず毎回使う
  decision … DecisionRAG のみ（論文どおり検索器を使わない。見つからなければ全文検索で補う）
  classic  … 従来型: 自動から DecisionRAG を除いたもの

検索の部品（retrievers）は LLM を使わないもの（BM25・埋め込み・GraphRAG の PPR・文書要約・
マルチクエリ）が速く、DecisionRAG（判定）とコミュニティ要約（初回の要約）が LLM を呼ぶ。
補助（helpers）の質問ルーター・根拠チェック（CRAG）・再順位付けは、それぞれ LLM を1回呼ぶ。
"""
from __future__ import annotations

from .config import RAG_HELPERS, RAG_METHODS, RAG_RETRIEVERS, STRATEGIES, clean_methods

STRATEGY_LABEL = {"auto": "自動", "fast": "速さ優先", "custom": "選んで使う",
                  "decision": "DecisionRAG のみ", "classic": "従来型"}
FAST_METHODS = ("bm25", "vector", "graph")

# 画面の説明と、1問あたりの呼び出しの目安（cost: LLM の呼び出し回数。"budget" は判定回数の上限＋1）
METHODS: dict[str, dict] = {
    "bm25": {"label": "全文（BM25）", "kind": "retriever", "llm": 0, "embed": 0,
             "desc": "語の一致で探す。型番・社内用語に強い。最速"},
    "vector": {"label": "埋め込み（ベクトル）", "kind": "retriever", "llm": 0, "embed": 1,
               "desc": "意味の近さで探す。言い換えに強い。埋め込みモデルが必要"},
    "graph": {"label": "GraphRAG", "kind": "retriever", "llm": 0, "embed": 0,
              "desc": "質問のエンティティから関係をたどる（PageRank）。人・組織・経緯の質問に強い。グラフが必要"},
    "decision": {"label": "DecisionRAG", "kind": "retriever", "llm": "budget", "embed": 0,
                 "desc": "フォルダ→文書→節→段落を判定モデルでたどる。言い換え・条件の判断に強いが LLM を何度も呼ぶ"},
    "summary": {"label": "文書要約（RAPTOR）", "kind": "retriever", "llm": 0, "embed": 0,
                "desc": "取り込み時に作った文書ごとの要約を資料に加える。要点・全体像の質問に"},
    "community": {"label": "コミュニティ要約", "kind": "retriever", "llm": 5, "embed": 0,
                  "desc": "グラフの関連の強いまとまりを要約して加える（LazyGraphRAG）。初回だけ LLM で要約し以後は保存分"},
    "multi": {"label": "マルチクエリ", "kind": "retriever", "llm": 0, "embed": "targets",
              "desc": "比較の質問で、比べる対象ごとに検索して偏りを防ぐ（比較の質問のときだけ）"},
    "router": {"label": "質問ルーター", "kind": "helper", "llm": 1, "embed": 0,
               "desc": "LLM で質問の種類を判定し、会話を踏まえて質問を補う（OFF なら規則で判定）"},
    "crag": {"label": "根拠チェック（CRAG）", "kind": "helper", "llm": 1, "embed": 0,
             "desc": "資料で答えられるかを LLM が判定し、足りなければ1回だけ再検索"},
    "rerank": {"label": "再順位付け", "kind": "helper", "llm": 1, "embed": 0,
               "desc": "LLM が資料を役立つ順に並べ替える（精度↑・時間↑）"},
}


def auto_methods(cfg: dict) -> set[str]:
    """自動で使いうる方式（設定タブの ON/OFF に従う。実際に使うかは質問の種類で決める）。"""
    r = cfg["rag"]
    out = {"bm25", "vector", "graph", "multi", "community", "summary"}
    if r.get("decision"):
        out.add("decision")
    for key, method in (("router", "router"), ("corrective", "crag"), ("rerank", "rerank")):
        if r.get(key):
            out.add(method)
    return out


def resolve(chat: dict, cfg: dict) -> dict:
    """相談の検索方式 → {strategy, label, dynamic, methods, router, crag, rerank}。

    dynamic=True（自動・従来型）は、methods の中から質問の種類で使うものを選ぶ。
    dynamic=False（速さ優先・選んで使う・DecisionRAG のみ）は、methods を毎回使う。
    """
    strategy = chat.get("strategy") if chat.get("strategy") in STRATEGIES else "auto"
    if strategy in ("auto", "classic"):
        methods = auto_methods(cfg)
        if strategy == "classic":
            methods.discard("decision")
        dynamic = True
    elif strategy == "fast":
        methods, dynamic = set(FAST_METHODS), False
    elif strategy == "decision":
        methods = {"decision"} | (auto_methods(cfg) & set(RAG_HELPERS))
        dynamic = False
    else:
        chosen = chat.get("methods")
        if not chosen:
            chosen = cfg["rag"].get("custom_methods") or list(FAST_METHODS)
        methods, dynamic = set(clean_methods(chosen)), False
    return {"strategy": strategy, "label": STRATEGY_LABEL[strategy], "dynamic": dynamic,
            "methods": methods, "router": "router" in methods, "crag": "crag" in methods,
            "rerank": "rerank" in methods}


def llm_estimate(methods, cfg: dict, *, dynamic: bool = False) -> dict:
    """1問あたりの呼び出しの目安（画面の表示用）: {"llm_max", "embed", "parts"}。

    dynamic（自動・従来型）は質問の種類で使う方式が変わるため、DecisionRAG（事実・比較）と
    コミュニティ要約（要約）は同じ質問で重ならないものとして最大を数える。
    """
    budget = int(cfg["rag"].get("decision_budget", 8)) + (1 if cfg["rag"].get("decision_choice", True) else 0)
    parts, embed, cost = [], 0, {}
    for m in RAG_METHODS:
        if m not in methods:
            continue
        info = METHODS[m]
        n = budget if info["llm"] == "budget" else int(info["llm"])
        cost[m] = n
        if n:
            parts.append(f"{info['label']} 最大 {n}" if m in ("decision", "community") else f"{info['label']} {n}")
        if info["embed"] == 1:
            embed += 1
    if dynamic:
        c = cost.get
        total = 1 + c("router", 0) + c("rerank", 0) + max(c("decision", 0) + c("crag", 0), c("community", 0),
                                                          c("crag", 0))
    else:
        total = 1 + sum(cost.values())
    return {"llm_max": total, "embed": embed, "parts": ["回答 1"] + parts}


def catalog(cfg: dict) -> dict:
    """画面用の一覧（方式の説明・検索方式ごとの方式と呼び出しの目安）。"""
    strategies = {}
    for s in STRATEGIES:
        r = resolve({"strategy": s, "methods": cfg["rag"].get("custom_methods")}, cfg)
        strategies[s] = {"label": r["label"], "dynamic": r["dynamic"],
                         "methods": [m for m in RAG_METHODS if m in r["methods"]],
                         "estimate": llm_estimate(r["methods"], cfg, dynamic=r["dynamic"])}
    return {"methods": {m: dict(METHODS[m], id=m) for m in RAG_METHODS},
            "order": list(RAG_METHODS), "retrievers": list(RAG_RETRIEVERS), "helpers": list(RAG_HELPERS),
            "strategies": strategies, "decision_budget": int(cfg["rag"].get("decision_budget", 8))}
