"""BookRAG — 論文 (arXiv:2512.03413) 忠実版の RAG。

BookIndex B=(T,G,M) の上で、Information Foraging Theory に着想した
エージェント型検索（Section 5）を行う:

  5.2 Agent-based Planning : クエリ分類（single-hop / multi-hop / global）→ Operator Plan
  5.3 Structured Execution : Selector で「情報パッチ」へ絞り込み → Reasoner（Graph/Text）
                             → Skyline_Ranker（Pareto 前線）→ Synthesizer（Map/Reduce）

オペレータ（Table 3）:
  Formulator : Decompose, Extract
  Selector   : Filter_Modal, Filter_Range, Select_by_Entity, Select_by_Section
  Reasoner   : Graph_Reasoning(PageRank×GT-Link), Text_Reasoning(rerank), Skyline_Ranker
  Synthesizer: Map, Reduce

使い方::

    import llmlab
    llmlab.configure(base_url=..., api_key=..., model=..., embed_model=...)

    book = llmlab.BookRAG()
    book.add_book("./docs/handbook.pdf", title="Handbook")   # BookIndex を構築
    ans = book.ask("How does X differ from Y?")               # エージェント検索
    print(ans)                                                # 回答＋根拠＋分類/プラン

注意: 論文は単一文書が対象。本実装は複数文書を1つの BookIndex に統合できる
（KG は横断的にマージされる）。標準ベクトル RAG が欲しい場合は PagedRAG/DocRAG を使う。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import bookindex as bx
from .bookindex import BookIndex

DEFAULT_STORAGE = "./storage/bookindex"
DEFAULT_GRADIENT_G = 0.6
DEFAULT_ER_TOP_K = 10
DEFAULT_MAX_EVIDENCE = 10


@dataclass
class Evidence:
    node_id: int
    title: str | None
    page: int | None
    s_graph: float
    s_text: float
    snippet: str

    def __str__(self) -> str:
        loc = []
        if self.title:
            loc.append(self.title)
        if self.page:
            loc.append(f"p.{self.page}")
        head = " / ".join(loc) if loc else f"node#{self.node_id}"
        return f"- [{head}] (G={self.s_graph:.2f}, T={self.s_text:.2f}) {self.snippet}"


@dataclass
class BookAnswer:
    text: str
    category: str = "single-hop"
    plan: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    sub_answers: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        out = [self.text, "", f"── 分類: {self.category} / プラン: {' → '.join(self.plan)} ──"]
        if self.evidence:
            out.append("根拠ノード:")
            out += [str(e) for e in self.evidence]
        return "\n".join(out)


class BookRAG:
    """論文忠実な BookRAG。BookIndex 構築 + エージェント検索。"""

    def __init__(
        self,
        storage_dir: str | Path = DEFAULT_STORAGE,
        *,
        gradient_g: float = DEFAULT_GRADIENT_G,
        er_top_k: int = DEFAULT_ER_TOP_K,
        max_evidence: int = DEFAULT_MAX_EVIDENCE,
        chunk_chars: int = 1500,    # 本文チャンクの目安サイズ（大きいほどノード=LLM呼出が減る）
        max_nodes: int = 300,       # 取り込み対象ノードの上限（超過は打ち切り）
        max_workers: int = 8,       # 抽出フェーズの並列数
        er_use_llm: bool = False,   # 名寄せで LLM を使うか（既定 False=高速）
        reranker=None,              # 再ランク: None/"cosine"/"local"/"endpoint"/dict（Text_Reasoning と ER の両方に使用）
        vlm: bool = False,          # True で PDF の図を VLM が読解し Image ノード化（画像対応モデルが必要）
        vlm_model: str | None = None,  # vlm=True 時の画像対応モデル名（省略時は接続設定の model）
    ):
        from .rerank import make_reranker

        self.storage_dir = Path(storage_dir)
        self.gradient_g = gradient_g
        self.er_top_k = er_top_k
        self.max_evidence = max_evidence
        self.chunk_chars = chunk_chars
        self.max_nodes = max_nodes
        self.max_workers = max_workers
        self.er_use_llm = er_use_llm
        self.vlm = vlm
        self.vlm_model = vlm_model
        self._reranker = make_reranker(reranker)
        self._bi: BookIndex | None = None

    # ===== Offline Indexing =====

    def add_book(self, path: str | Path, *, title: str | None = None,
                 use_llm_sections: bool = False, max_nodes: int | None = None,
                 chunk_chars: int | None = None, ocr=False, layout=False,
                 vlm: bool | None = None, force: bool = False) -> str:
        """文書を取り込み、BookIndex（Tree + KG + GT-Link）へ統合する。

        既定は速度重視（見出し判定は LLM 不使用、本文はチャンク化、ノード数に上限）。
        精度を上げたいときは use_llm_sections=True / max_nodes を増やす / er_use_llm=True。

        PDF の版面解析・OCR（要 `pip install -e ".[ocr]"` + Tesseract）:
        - layout="auto": pymupdf のフォントサイズで見出し階層を判定（pypdf より高精度）
        - layout="mineru": MinerU(magic_pdf) 導入時はそれを使う
        - ocr="auto": テキストの薄いページのみ OCR / ocr=True: 全ページ OCR
        - vlm=True: 図を VLM が読解して Image ノード化（画像対応モデルが必要。
          省略時はコンストラクタの vlm 設定に従う。通常のローカルLLMのみなら False のまま）
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"ファイルがありません: {path}")
        use_vlm = self.vlm if vlm is None else vlm
        if use_vlm:
            bx.log("vlm=True: 図の読解には画像入力対応モデルが必要です"
                   f"（使用モデル: {self.vlm_model or '接続設定の model'}）")
        bi = self._index(create=True)
        book = title or path.stem
        # 同一タイトルの二重取り込みを防ぐ（木とKGが重複し検索結果も二重になるため）
        if any(bi.nodes[r].title == book for r in bi.roots) and not force:
            bx.log(f"「{book}」は取り込み済みのためスキップします"
                   "（再取り込みは force=True、索引の作り直しは reset()）")
            return book

        bx.log(f"[1/5] 解析（レイアウト{'+OCR' if ocr else ''}{'+VLM' if use_vlm else ''}）: {path.name}")
        blocks = bx.parse_blocks(path, ocr=ocr, layout=layout,      # 4.2.1 Layout Parsing
                                 vlm=use_vlm, vlm_model=self.vlm_model)
        bx.log(f"[2/5] 見出し判定{'（LLM）' if use_llm_sections else '（ヒューリスティック）'}"
               f": ブロック {len(blocks)} 個")
        blocks = bx.section_filter(blocks, use_llm=use_llm_sections)  # 4.2.2 Section Filtering
        bx.log("[3/5] 木（BookIndex Tree）を構築中…")
        root = bx.build_tree(bi, blocks, book,                     # 木 T（本文はチャンク化）
                             chunk_chars=chunk_chars or self.chunk_chars)
        new_nodes = bi.subtree(root)
        bx.log(f"[4/5] 知識グラフ構築（抽出→名寄せ）: ノード {len(new_nodes)} 個")
        bx.build_graph(bi, new_nodes, gradient_g=self.gradient_g,  # 4.3 KG + Gradient ER
                       er_top_k=self.er_top_k, max_workers=self.max_workers,
                       max_nodes=max_nodes or self.max_nodes, er_use_llm=self.er_use_llm,
                       reranker=self._reranker)  # Algorithm 1 の Rerank model R
        bx.log("[5/5] 保存中…")
        bi.persist(self.storage_dir)
        bx.log(f"完了: {book}（エンティティ {len(bi.entities)} / 関係 {len(bi.relations)}）")
        return book

    def info(self) -> dict:
        bi = self._index(create=False)
        if bi is None:
            return {"books": [], "nodes": 0, "entities": 0, "relations": 0}
        sections = [bi.nodes[r].title for r in bi.roots]
        return {
            "books": sections,
            "nodes": len(bi.nodes),
            "entities": len(bi.entities),
            "relations": len(bi.relations),
        }

    def reset(self) -> None:
        import shutil

        if self.storage_dir.exists():
            shutil.rmtree(self.storage_dir)
        self._bi = None

    # ===== Online Retrieval（5. Agent-based Retrieval） =====

    def query(self, question: str) -> BookAnswer:
        bi = self._index(create=False)
        if bi is None or not bi.nodes:
            raise RuntimeError("BookIndex が空です。add_book() を実行してください。")

        category = self._classify(question)            # 5.2 Query Classification
        if category == "multi-hop":
            return self._run_complex(bi, question)
        if category == "global":
            return self._run_global(bi, question)
        return self._run_single_hop(bi, question)

    def ask(self, question: str) -> BookAnswer:
        return self.query(question)

    # ---- プラン: Single-hop（式(9)(10)） ----
    def _run_single_hop(self, bi: BookIndex, question: str) -> BookAnswer:
        plan = ["Classify(single-hop)"]
        entity_ids = self._op_extract(bi, question)            # Formulator: Extract
        if entity_ids:
            plan.append("Select_by_Entity")
            ns = self._op_select_by_entity(bi, entity_ids)
        else:
            plan.append("Select_by_Section")
            ns = self._op_select_by_section(bi, question)
        text, evidence = self._p_std(bi, question, ns, entity_ids, plan)
        return BookAnswer(text=text, category="single-hop", plan=plan, evidence=evidence)

    def _p_std(self, bi, question, ns, entity_ids, plan):
        """P_std = (Graph ∥ Text) → Skyline → Reduce（式(10)）。"""
        plan += ["Graph_Reasoning∥Text_Reasoning", "Skyline_Ranker", "Reduce"]
        s_graph = self._op_graph_reasoning(bi, ns, entity_ids)     # Reasoner: Graph
        s_text = self._op_text_reasoning(bi, ns, question)         # Reasoner: Text
        retained = self._op_skyline(ns, s_graph, s_text)           # Skyline_Ranker（式(14)）
        evidence = self._to_evidence(bi, retained, s_graph, s_text)
        text = self._op_reduce(question, evidence)                 # Synthesizer: Reduce
        return text, evidence

    # ---- プラン: Complex / Multi-hop（式(11)） ----
    def _run_complex(self, bi: BookIndex, question: str) -> BookAnswer:
        plan = ["Classify(multi-hop)", "Decompose"]
        subqs = self._op_decompose(question)                       # Formulator: Decompose
        retrieval_qs = [sq for sq in subqs if sq.get("type") != "synthesis"]
        sub_answers, all_evidence = [], []
        for sq in bx.progress(retrieval_qs, total=len(retrieval_qs),
                              desc="multi-hop: サブ質問"):
            q = sq["question"]
            ids = self._op_extract(bi, q)
            ns = self._op_select_by_entity(bi, ids) if ids else self._op_select_by_section(bi, q)
            text, ev = self._p_std(bi, q, ns, ids, [])             # P_s をサブ問題へ適用
            sub_answers.append(f"Q: {q}\nA: {text}")               # Map（部分回答）
            all_evidence += ev
        plan += ["P_s(sub-questions)", "Map", "Reduce"]
        final = self._op_reduce(question, all_evidence, partials=sub_answers)  # Reduce（統合）
        return BookAnswer(text=final, category="multi-hop", plan=plan,
                          evidence=all_evidence[: self.max_evidence], sub_answers=sub_answers)

    # ---- プラン: Global Aggregation（式(12)） ----
    def _run_global(self, bi: BookIndex, question: str) -> BookAnswer:
        plan = ["Classify(global)"]
        spec = self._op_make_filters(question)                     # Fig.12 のフィルタ生成
        ns = list(bi.nodes.keys())
        filters = spec.get("filters", [])
        if not isinstance(filters, list):  # LLM 応答の形状不正に耐える
            filters = []
        for f in filters:
            if not isinstance(f, dict):
                continue
            ftype = f.get("filter_type")
            if ftype in ("image", "table", "section"):
                ns = self._op_filter_modal(bi, ns, ftype)          # Selector: Filter_Modal
                plan.append(f"Filter_Modal({ftype})")
            elif ftype == "page":
                ns = self._op_filter_range(bi, ns, f.get("filter_value"))  # Selector: Filter_Range
                plan.append(f"Filter_Range({f.get('filter_value')})")
        # COUNT/LIST の集計を max_evidence で歪めないよう、全件数を明示しつつ広めに渡す
        total = len(ns)
        evidence = self._to_evidence(bi, ns, {}, {}, limit=min(total, 100))
        operation = str(spec.get("operation", "ANALYZE") or "ANALYZE")
        operation = f"{operation}（フィルタ後の該当ノード総数: {total} 件。件数を問われたらこの総数を使う）"

        # 論文 5.3: Map が各ブロックを分析→Reduce が統合。件数が多いときはバッチ Map を
        # 並列実行して部分所見を作り、Reduce に渡す（少数なら直接 Reduce で十分）。
        partials = None
        if len(evidence) > self.max_evidence:
            plan.append(f"Map({len(evidence)}件をバッチ分析)")
            partials = self._op_map_batches(question, evidence, operation)
        plan += [f"Reduce({operation})"]
        text = self._op_reduce(question, evidence[: self.max_evidence] if partials else evidence,
                               operation=operation, partials=partials)
        return BookAnswer(text=text, category="global", plan=plan,
                          evidence=evidence[: self.max_evidence])

    def _op_map_batches(self, question: str, evidence: list[Evidence],
                        operation: str, *, batch_size: int = 10) -> list[str]:
        """Synthesizer/Map: 根拠ブロックをバッチごとに分析し部分所見を生成（並列）。"""
        from concurrent.futures import ThreadPoolExecutor

        batches = [evidence[i:i + batch_size] for i in range(0, len(evidence), batch_size)]

        def _map_one(pair):
            bi_idx, batch = pair
            ctx = "\n".join(
                f"- ({e.title or ''} {'p.'+str(e.page) if e.page else ''}) {e.snippet}"
                for e in batch
            )
            prompt = (
                "以下は文書から取り出した断片の一部です。質問に関係する事実・数値・項目を"
                "漏れなく抽出し、箇条書きで簡潔に列挙してください（このバッチ分のみ）。\n\n"
                f"質問: {question}\n集計操作: {operation}\n\n断片（バッチ {bi_idx+1}/{len(batches)}）:\n{ctx}"
            )
            try:
                return bx.llm_text(prompt).strip()
            except Exception as e:  # noqa: BLE001
                return f"(バッチ {bi_idx+1} の分析に失敗: {e})"

        with ThreadPoolExecutor(max_workers=4) as ex:
            return list(bx.progress(ex.map(_map_one, enumerate(batches)),
                                    total=len(batches), desc="global: Map バッチ分析 (4並列)"))

    # ===== Operators =====

    def _op_extract(self, bi: BookIndex, query: str) -> list[int]:
        """Formulator/Extract（式(3)）: クエリのキーエンティティを KG にリンク。"""
        result = bx.llm_json(_P_EXTRACT + f"\n\nQuery: {query}")
        names = []
        if isinstance(result, dict):
            names = result.get("entities", [])
        elif isinstance(result, list):
            names = result
        names = [n for n in names if isinstance(n, str)] if names else []

        matched: list[int] = []
        for n in names:
            eid = bi.find_entity_by_name(n)
            if eid is None:
                cand = bi.search_entities(bx.embed([n])[0], 1)  # 近傍でリンク
                if cand and cand[0][1] >= 0.6:
                    eid = cand[0][0]
            if eid is not None and eid not in matched:
                matched.append(eid)
        return matched

    def _op_select_by_entity(self, bi: BookIndex, entity_ids: list[int]) -> list[int]:
        """Selector/Select_by_Entity（式(5)）: エンティティが属する節の部分木を集める。"""
        target_sections: set[int] = set()
        for eid in entity_ids:
            for nid in bi.entities[eid].origin_nodes:
                target_sections.add(self._section_ancestor(bi, nid))
        ns: set[int] = set()
        for s in target_sections:
            ns.update(bi.subtree(s))
        return [n for n in ns if bi.nodes[n].type != "Section"] or list(ns)

    def _op_select_by_section(self, bi: BookIndex, query: str) -> list[int]:
        """Selector/Select_by_Section: LLM が関連節を選び、その部分木を集める。"""
        sections = [(nid, n.title) for nid, n in bi.nodes.items()
                    if n.type == "Section" and n.level and n.level >= 1]
        if not sections:
            return list(bi.nodes.keys())
        listing = [{"id": nid, "title": t} for nid, t in sections][:200]
        result = bx.llm_json(
            _P_SELECT_SECTION + f"\n\nQuery: {query}\nSections: {json.dumps(listing, ensure_ascii=False)}"
        )
        chosen = result.get("section_ids", []) if isinstance(result, dict) else []
        if not isinstance(chosen, list):
            chosen = []
        section_ids = {nid for nid, _ in sections}  # Section ノードのみ受理（LLM の誤 id 対策）
        chosen = [c for c in chosen if isinstance(c, int) and c in section_ids]
        if not chosen:  # フォールバック: タイトルのコサインで上位節
            qv = bx.embed([query])[0]
            tvs = bx.embed([t for _, t in sections])
            sims = tvs @ qv
            chosen = [sections[i][0] for i in np.argsort(-sims)[:3]]
        ns: set[int] = set()
        for s in chosen:
            ns.update(bi.subtree(s))
        return [n for n in ns if bi.nodes[n].type != "Section"] or list(ns)

    def _op_filter_modal(self, bi: BookIndex, ns: list[int], modal: str) -> list[int]:
        target = {"image": "Image", "table": "Table", "section": "Section"}.get(modal)
        return [n for n in ns if bi.nodes[n].type == target]

    def _op_filter_range(self, bi: BookIndex, ns: list[int], value) -> list[int]:
        if not value:
            return ns
        try:
            if "-" in str(value):
                a, b = str(value).split("-", 1)
                lo, hi = int(a), int(b)
            else:
                lo = hi = int(value)
        except ValueError:
            return ns
        # page は PDF 由来なら int、Excel/PPTX 由来はシート名等の str のことがある。
        # int のみ範囲比較の対象にする（str と int の比較は TypeError になるため）。
        return [n for n in ns
                if isinstance(bi.nodes[n].page, int) and lo <= bi.nodes[n].page <= hi]

    def _op_graph_reasoning(self, bi: BookIndex, ns: list[int], start_entities: list[int]) -> dict[int, float]:
        """Reasoner/Graph_Reasoning（式(6)(7)）: 部分グラフ上の PageRank を GT-Link でノードへ写像。"""
        rev = bi.node_to_entities()
        sub_entities = [e for e in {e for n in ns for e in rev.get(n, [])}]
        if not sub_entities:
            return {n: 0.0 for n in ns}

        sub_set = set(sub_entities)
        edges = [(s, t) for s, t, _ in bi.relations if s in sub_set and t in sub_set]
        pers = {e: 1.0 for e in start_entities if e in sub_set} or None
        ig = _personalized_pagerank(sub_entities, edges, personalization=pers)  # I_G（式(6)）

        # S_G = I_G × M : エンティティ重要度をツリーノードへ集約（式(7)）
        s_graph = {n: 0.0 for n in ns}
        for n in ns:
            for e in rev.get(n, []):
                s_graph[n] += ig.get(e, 0.0)
        return _minmax(s_graph)

    def _op_text_reasoning(self, bi: BookIndex, ns: list[int], query: str) -> dict[int, float]:
        """Reasoner/Text_Reasoning: ノード内容とクエリの意味的関連度 S_T。

        reranker が設定されていればそれで再スコア（既定は埋め込みコサイン=従来挙動）。
        """
        if not ns:
            return {}
        contents = [bi.nodes[n].content[:500] or " " for n in ns]
        try:
            scores = self._reranker.rerank(query, contents)
        except Exception as e:  # noqa: BLE001
            print(f"[BookRAG] rerank に失敗したためコサインで代替: {e}")
            from .rerank import CosineReranker
            scores = CosineReranker().rerank(query, contents)
        # 0-1 に正規化して返す。CrossEncoder のロジット（±10 等）をそのまま使うと、
        # Skyline 後の (S_G + S_T) ソートでグラフスコア（minmax 済み）との釣り合いが崩れる。
        # 単調変換なので Pareto 支配関係（Skyline の中身）は変わらない。
        return _minmax({n: float(scores[i]) for i, n in enumerate(ns)})

    def _op_skyline(self, ns: list[int], s_graph: dict, s_text: dict) -> list[int]:
        """Skyline_Ranker（式(14)）: (S_G, S_T) の Pareto 前線を残す（top-k 固定ではない）。"""
        pts = [(n, s_graph.get(n, 0.0), s_text.get(n, 0.0)) for n in ns]
        skyline = []
        for n, g, t in pts:
            dominated = any(
                (g2 >= g and t2 >= t) and (g2 > g or t2 > t)
                for m, g2, t2 in pts if m != n
            )
            if not dominated:
                skyline.append((n, g, t))
        # 過大な候補は (S_G+S_T) 上位で抑制（論文の平均保持数 ~10 に整合）
        skyline.sort(key=lambda x: x[1] + x[2], reverse=True)
        return [n for n, _, _ in skyline[: self.max_evidence]]

    def _op_decompose(self, query: str) -> list[dict]:
        """Formulator/Decompose（式(2)）: 複合質問を独立サブ問題へ分解。"""
        result = bx.llm_json(_P_DECOMPOSE + f"\n\nQuery: {query}")
        subs = result.get("sub_questions", []) if isinstance(result, dict) else []
        return [s for s in subs if isinstance(s, dict) and s.get("question")] or [
            {"question": query, "type": "retrieval"}
        ]

    def _op_make_filters(self, query: str) -> dict:
        result = bx.llm_json(_P_FILTER + f"\n\nQuery: {query}")
        return result if isinstance(result, dict) else {"filters": [], "operation": "ANALYZE"}

    def _op_reduce(self, question: str, evidence: list[Evidence], *, partials=None, operation=None) -> str:
        """Synthesizer/Reduce: 根拠と部分回答を統合して最終回答を生成（式(15)）。"""
        ctx = []
        for i, e in enumerate(evidence, 1):
            loc = f"{e.title or ''} {('p.'+str(e.page)) if e.page else ''}".strip()
            ctx.append(f"[{i}] ({loc}) {e.snippet}")
        prompt = _P_REDUCE + f"\n\nQuestion: {question}\n"
        if operation:
            prompt += f"Aggregation operation: {operation}\n"
        if partials:
            prompt += "Sub-answers:\n" + "\n".join(partials) + "\n"
        prompt += "Evidence:\n" + ("\n".join(ctx) if ctx else "(none)")
        return bx.llm_text(prompt).strip()

    # ===== helpers =====

    def _classify(self, question: str) -> str:
        result = bx.llm_json(_P_CLASSIFY + f"\n\nUser Query: {question}")
        cat = (result or {}).get("category", "single-hop") if isinstance(result, dict) else "single-hop"
        cat = str(cat).lower()
        if cat in ("simple", "single", "single-hop"):
            return "single-hop"
        if cat in ("complex", "multi", "multi-hop"):
            return "multi-hop"
        if cat in ("global", "aggregation", "global aggregation"):
            return "global"
        return "single-hop"

    def _section_ancestor(self, bi: BookIndex, nid: int) -> int:
        cur = nid
        while bi.nodes[cur].type != "Section" and bi.nodes[cur].parent is not None:
            cur = bi.nodes[cur].parent
        return cur

    def _to_evidence(self, bi: BookIndex, ns: list[int], s_graph: dict, s_text: dict,
                     *, limit: int | None = None) -> list[Evidence]:
        ev = []
        for n in ns[: (limit if limit is not None else self.max_evidence)]:
            node = bi.nodes[n]
            txt = node.content.strip().replace("\n", " ")
            ev.append(Evidence(
                node_id=n, title=node.title or node.book, page=node.page,
                s_graph=s_graph.get(n, 0.0), s_text=s_text.get(n, 0.0),
                snippet=(txt[:140] + "…") if len(txt) > 140 else txt,
            ))
        return ev

    def _index(self, *, create: bool) -> BookIndex | None:
        if self._bi is not None:
            return self._bi
        if (self.storage_dir / "bookindex.json").exists():
            self._bi = BookIndex.load(self.storage_dir)
        elif create:
            self._bi = BookIndex()
        return self._bi


def _personalized_pagerank(node_ids, edges, *, personalization=None, alpha=0.85, iters=100, tol=1e-8):
    """有向部分グラフ上の（Personalized）PageRank。power iteration の自前実装。

    networkx.pagerank は scipy を要するため、依存を増やさないよう numpy だけで実装する。
    """
    n = len(node_ids)
    if n == 0:
        return {}
    idx = {nid: i for i, nid in enumerate(node_ids)}
    out: list[list[int]] = [[] for _ in range(n)]
    for s, t in edges:
        out[idx[s]].append(idx[t])

    if personalization:
        p = np.array([personalization.get(nid, 0.0) for nid in node_ids], dtype=np.float64)
        p = p / p.sum() if p.sum() > 0 else np.ones(n) / n
    else:
        p = np.ones(n) / n

    r = np.ones(n) / n
    for _ in range(iters):
        rn = np.zeros(n)
        dangling = 0.0
        for i in range(n):
            if out[i]:
                share = r[i] / len(out[i])
                for j in out[i]:
                    rn[j] += share
            else:
                dangling += r[i]
        rn = alpha * (rn + dangling * p) + (1 - alpha) * p
        if np.abs(rn - r).sum() < tol:
            r = rn
            break
        r = rn
    return {node_ids[i]: float(r[i]) for i in range(n)}


def _minmax(d: dict[int, float]) -> dict[int, float]:
    if not d:
        return d
    vals = list(d.values())
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        return {k: 0.0 for k in d}
    return {k: (v - lo) / (hi - lo) for k, v in d.items()}


# --------------------------------------------------------------------------
# プロンプト（論文 Fig.10-12 を踏襲。多言語文書に対応するよう簡潔化）
# --------------------------------------------------------------------------

_P_CLASSIFY = """You are an expert query analyzer. Classify the user's question into exactly one of:
"single-hop", "multi-hop", or "global".
- single-hop: answerable from a SINGLE contiguous location (one paragraph/table/figure), even if it
  requires light reasoning, as long as all needed data is in that one place.
- multi-hop: requires decomposition into multiple sub-questions, each answered by a separate retrieval.
- global: requires aggregation (count/list/summarize) over items identified by a structural filter.
Return ONLY JSON: {"category": "single-hop|multi-hop|global"}."""

_P_EXTRACT = """Identify the key entities mentioned in the query (concepts, names, sections, objects).
Return ONLY JSON: {"entities": [str, ...]}."""

_P_SELECT_SECTION = """Given a query and the list of document sections (id, title), choose the section ids
most likely to contain the answer. Return ONLY JSON: {"section_ids": [int, ...]}."""

_P_DECOMPOSE = """You are a query decomposition expert. Break the complex question into independent,
parallelizable retrieval sub-questions plus a final synthesis step. Retrieval sub-questions MUST NOT
depend on each other's answers. Return ONLY JSON:
{"sub_questions": [{"question": str, "type": "retrieval|synthesis"}, ...]}."""

_P_FILTER = """Analyze a global query and return the filtering steps and final aggregation.
filter_type ∈ ["section","image","table","page"]; for image/table, filter_value MUST be null;
operation ∈ ["COUNT","LIST","SUMMARIZE","ANALYZE"].
Return ONLY JSON: {"filters": [{"filter_type": str, "filter_value": str|null}, ...], "operation": str}."""

_P_REDUCE = """You are a careful assistant. Using ONLY the provided evidence (and sub-answers if any),
answer the question concisely and accurately. If an aggregation operation is given, perform it. Cite the
evidence indices like [1], [2] where relevant. Answer in the question's language."""
