"""MultiPaperRAG v1 — 複数論文を横断して比較するためのオーケストレーション層。

「広く探す → 論文ごとに深掘り → 突き合わせて比較」を束ねる。

  add_paper × N : 論文ごとにベクトル索引（PagedRAG, title 付き）へ取り込み、
                  さらに表を pdfplumber で抽出してキャッシュする。
  compare(q)    : Stage1 横断検索で候補論文を特定 → Stage2 論文ごとに深掘り
                  → Stage3 突き合わせて比較・統合。
  compare_table(metric): 論文ごとにキャッシュ表から該当数値を LLM で抽出し、
                  横断比較表を生成。

v1 の対象は「表（数値）」まで。図/グラフは VLM が必要なため v2 で対応予定。
接続設定・プロキシ・埋め込み設定は既存（configure / settings_form）をそのまま使う。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .client import complete
from .pagedrag import PagedRAG

DEFAULT_STORAGE = "./storage/multipaper"
PAPER_EXTS = {".pdf", ".txt", ".md", ".docx", ".pptx", ".html"}


@dataclass
class Comparison:
    """論文横断の比較結果。"""

    text: str
    papers: list[str] = field(default_factory=list)
    per_paper: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        out = [self.text, "", f"── 対象論文 ({len(self.papers)}) ──"]
        for t in self.papers:
            out.append(f"■ {t}\n{self.per_paper.get(t, '').strip()}")
        return "\n".join(out)


class MultiPaperRAG:
    """複数論文の横断比較（v1: 本文の深掘り + 表の数値比較）。"""

    def __init__(
        self,
        storage_dir: str | Path = DEFAULT_STORAGE,
        *,
        top_k: int = 6,
        max_papers: int = 4,
        chunk_size: int = 1024,
        chunk_overlap: int = 128,
    ):
        self.storage_dir = Path(storage_dir)
        self.max_papers = max_papers
        self._rag = PagedRAG(
            storage_dir=str(self.storage_dir / "vectors"),
            top_k=top_k, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        )
        self.tables_dir = self.storage_dir / "tables"

    # ---- 取り込み ----------------------------------------------------------

    def add_paper(self, path: str | Path, *, title: str | None = None,
                  extract_tables: bool = True) -> str:
        """論文を1本取り込む（ベクトル索引 + 表キャッシュ）。"""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"ファイルがありません: {path}")
        book_title = self._rag.add_book(path, title=title)
        if extract_tables and path.suffix.lower() == ".pdf":
            self._cache_tables(path, book_title)
        return book_title

    def add_papers(self, docs_dir: str | Path) -> list[str]:
        """フォルダ内の論文をまとめて取り込む。"""
        docs_dir = Path(docs_dir)
        added = []
        for f in sorted(docs_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in PAPER_EXTS:
                added.append(self.add_paper(f))
        return added

    def papers(self) -> list[str]:
        return [b["title"] for b in self._rag.books()]

    # ---- Stage 1: 横断検索で候補論文を特定 ----------------------------------

    def locate(self, question: str, *, max_papers: int | None = None) -> list[str]:
        """全論文横断で検索し、関連しそうな論文 title を順に返す。"""
        ans = self._rag.query(question)
        order: list[str] = []
        for s in ans.sources:
            if s.title and s.title not in order:
                order.append(s.title)
        return order[: (max_papers or self.max_papers)]

    # ---- compare: Stage1-3 をまとめて実行 -----------------------------------

    def compare(self, question: str, *, papers: list[str] | None = None) -> Comparison:
        """質問について、候補論文ごとに深掘り→突き合わせて比較する。"""
        cands = papers or self.locate(question) or self.papers()[: self.max_papers]
        if not cands:
            raise RuntimeError("論文が取り込まれていません。add_paper()/add_papers() を実行してください。")

        per_paper: dict[str, str] = {}
        for t in cands:                                   # Stage2: 論文ごとに深掘り
            a = self._rag.query(question, title=t)
            per_paper[t] = a.text.strip()

        final = self._synthesize(question, per_paper)      # Stage3: 突き合わせ
        return Comparison(text=final, papers=cands, per_paper=per_paper)

    # ---- compare_table: 表の数値を論文横断で比較 ----------------------------

    def compare_table(self, metric: str, *, papers: list[str] | None = None) -> Comparison:
        """指定指標（例 "ImageNet accuracy"）を各論文の表から抽出し比較表を作る。"""
        cands = papers or self.papers()
        if not cands:
            raise RuntimeError("論文が取り込まれていません。")

        per_paper: dict[str, str] = {}
        for t in cands:
            tables = self._load_tables(t)
            if not tables:
                per_paper[t] = "(表データなし / pdfplumber 未抽出)"
                continue
            per_paper[t] = self._extract_metric(metric, t, tables)

        final = self._synthesize_table(metric, per_paper)
        return Comparison(text=final, papers=cands, per_paper=per_paper)

    # ---- 合成（LLM） --------------------------------------------------------

    def _synthesize(self, question: str, per_paper: dict[str, str]) -> str:
        blocks = "\n\n".join(f"[{t}]\n{ans}" for t, ans in per_paper.items())
        prompt = (
            "以下は複数の論文それぞれから得た回答です。これらを突き合わせ、論文間の"
            "共通点・相違点を明確にして比較し、質問に答えてください。各主張には論文名を"
            "明記し、可能なら簡潔な比較表（Markdown）を添えてください。\n\n"
            f"質問: {question}\n\n論文別の回答:\n{blocks}"
        )
        return complete(prompt).strip()

    def _extract_metric(self, metric: str, title: str, tables: list[dict]) -> str:
        rendered = self._tables_to_text(tables)
        prompt = (
            f"次は論文「{title}」から抽出した表です。指標「{metric}」に該当する数値を"
            "見つけ、値（と条件・データセット名・ページがあれば）を簡潔に列挙してください。"
            "該当が無ければ『該当なし』とだけ答えてください。\n\n" + rendered
        )
        return complete(prompt).strip()

    def _synthesize_table(self, metric: str, per_paper: dict[str, str]) -> str:
        blocks = "\n\n".join(f"[{t}]\n{v}" for t, v in per_paper.items())
        prompt = (
            f"以下は各論文から抽出した「{metric}」に関する数値です。論文を行、"
            "指標/条件を列にした **Markdown の比較表** を作り、続けて要点を一言で述べて"
            "ください。値が無い論文は空欄にしてください。\n\n" + blocks
        )
        return complete(prompt).strip()

    # ---- 表抽出（pdfplumber） ----------------------------------------------

    def _cache_tables(self, path: Path, title: str) -> None:
        try:
            import pdfplumber
        except ImportError:
            print("[MultiPaperRAG] 表抽出には pdfplumber が必要です（表比較をスキップ）:\n"
                  "  pip install pdfplumber")
            return
        tables: list[dict] = []
        try:
            with pdfplumber.open(str(path)) as pdf:
                for pno, page in enumerate(pdf.pages, start=1):
                    for tb in (page.extract_tables() or []):
                        if tb:
                            tables.append({"page": pno, "rows": tb})
        except Exception as e:  # noqa: BLE001
            print(f"[MultiPaperRAG] 表抽出に失敗（スキップ）: {e}")
            return
        self.tables_dir.mkdir(parents=True, exist_ok=True)
        (self.tables_dir / f"{self._safe(title)}.json").write_text(
            json.dumps(tables, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[MultiPaperRAG] {title}: 表 {len(tables)} 個を抽出")

    def _load_tables(self, title: str) -> list[dict]:
        p = self.tables_dir / f"{self._safe(title)}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return []

    @staticmethod
    def _tables_to_text(tables: list[dict], *, max_tables: int = 20) -> str:
        out = []
        for i, tb in enumerate(tables[:max_tables], start=1):
            rows = tb.get("rows", [])
            lines = [
                " | ".join("" if c is None else str(c) for c in row)
                for row in rows
            ]
            out.append(f"# 表{i} (p.{tb.get('page','?')})\n" + "\n".join(lines))
        return "\n\n".join(out)

    @staticmethod
    def _safe(title: str) -> str:
        return re.sub(r"[^\w\-.]+", "_", title)[:120] or "untitled"
