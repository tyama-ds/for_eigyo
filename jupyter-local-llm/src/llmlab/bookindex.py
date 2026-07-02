"""BookIndex — 論文 BookRAG (arXiv:2512.03413) の文書ネイティブ索引 B = (T, G, M)。

- T: 文書の論理階層を表す木（目次に相当）。ノードは Section / Text / Table / Image。
- G: ノードから抽出したエンティティと関係の知識グラフ（KG）。
- M: GT-Link。各エンティティを抽出元のツリーノード集合へ対応付ける。

本モジュールは BookIndex の「構築」を担う:
  4.2 Tree Construction（Layout Parsing → Section Filtering）
  4.3 Graph Construction（KG Construction → Gradient-based Entity Resolution / Algorithm 1）

論文との差分（環境前提による簡略化。本実装は OpenAI 互換API + JupyterLab 完結が前提）:
  - 版面解析に MinerU を使わず、Markdown/テキストの見出し or PDF テキストの
    ヒューリスティック + LLM Section Filtering で木を作る。
  - Rerank モデルの代わりに埋め込みコサイン類似度を Gradient-based ER のスコアに用いる
    （reranker=None 時）。画像は VLM ではなくキャプション/テキストとして扱う。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------
# LLM / 埋め込みの共通ヘルパー（llmlab の OpenAI 互換クライアントを使う）
# --------------------------------------------------------------------------


def llm_text(prompt: str, *, system: str | None = None, temperature: float = 0.0) -> str:
    from .client import get_client
    from .config import get_settings

    s = get_settings()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = get_client().chat.completions.create(
        model=s.model, messages=messages, temperature=temperature
    )
    return resp.choices[0].message.content or ""


def llm_json(prompt: str, *, system: str | None = None):
    """LLM 応答を JSON として解釈する（```json フェンスや前後テキストを除去）。"""
    raw = llm_text(prompt, system=system)
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    # 最初の { … } もしくは [ … ] を抽出
    m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def embed(texts: list[str]) -> np.ndarray:
    """テキスト群を L2 正規化済みの埋め込み行列に変換する（embed_base_url を尊重）。"""
    from .client import get_embed_client
    from .config import get_settings

    if not texts:
        return np.zeros((0, 1), dtype=np.float32)
    s = get_settings()
    resp = get_embed_client().embeddings.create(model=s.embed_model, input=texts)
    vecs = np.array([d.embedding for d in resp.data], dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def progress(iterable, *, total: int | None = None, desc: str = ""):
    """tqdm があればプログレスバー、無ければ定期 print で進捗を返すイテレータ。

    JupyterLab では tqdm.auto がセル内にバーを描画する（現在のフェーズ=desc 付き）。
    """
    try:
        from tqdm.auto import tqdm

        return tqdm(iterable, total=total, desc=desc, leave=True)
    except Exception:  # noqa: BLE001
        def _gen():
            n = 0
            for x in iterable:
                n += 1
                if total and (n % 20 == 0 or n == total):
                    print(f"[BookRAG] {desc} {n}/{total}")
                elif not total and n % 20 == 0:
                    print(f"[BookRAG] {desc} {n}")
                yield x
        return _gen()


def log(msg: str) -> None:
    print(f"[BookRAG] {msg}")


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------

NODE_TYPES = ("Section", "Text", "Table", "Image")


@dataclass
class TreeNode:
    """ツリー T のノード。content と最終 type（4.2.2）を保持する。"""

    id: int
    type: str  # Section / Text / Table / Image
    content: str
    book: str
    level: int | None = None  # Section のみ意味を持つ（l=1 が最上位）
    title: str | None = None
    page: int | None = None
    parent: int | None = None
    children: list[int] = field(default_factory=list)


@dataclass
class Entity:
    """KG G の頂点。origin_nodes が GT-Link M（V→P(N)）を構成する。"""

    id: int
    name: str
    type: str
    description: str
    origin_nodes: list[int] = field(default_factory=list)


class BookIndex:
    """B = (T, G, M)。木・KG・GT-Link を一体で保持し、永続化できる。"""

    def __init__(self):
        self.nodes: dict[int, TreeNode] = {}
        self.roots: list[int] = []
        self.entities: dict[int, Entity] = {}
        self.relations: list[tuple[int, int, str]] = []  # (src_eid, dst_eid, label)
        self._node_seq = 0
        self._ent_seq = 0
        # エンティティ・ベクタDB（容量ダブリングの成長バッファ。append 償却 O(1)）
        self._entity_vecs: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        self._entity_ids: list[int] = []  # 行 -> entity id
        self._row_of: dict[int, int] = {}  # entity id -> 行
        self._count = 0
        self._dim: int | None = None

    # ---- ノード ----
    def add_node(self, **kwargs) -> TreeNode:
        nid = self._node_seq
        self._node_seq += 1
        node = TreeNode(id=nid, **kwargs)
        self.nodes[nid] = node
        return node

    def node_to_entities(self) -> dict[int, list[int]]:
        """逆引き: ツリーノード → そこに紐づくエンティティ集合。"""
        rev: dict[int, list[int]] = {}
        for e in self.entities.values():
            for n in e.origin_nodes:
                rev.setdefault(n, []).append(e.id)
        return rev

    def subtree(self, node_id: int) -> list[int]:
        """node_id を根とする部分木の全ノード id（式(5) Subtree）。"""
        out, stack = [], [node_id]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(self.nodes[cur].children)
        return out

    # ---- エンティティ（KG） ----
    def _validate_dim(self, vec: np.ndarray) -> None:
        if self._dim is not None and vec.shape[0] != self._dim:
            raise RuntimeError(
                f"埋め込み次元が既存インデックス（{self._dim}）と一致しません（{vec.shape[0]}）。"
                "埋め込みモデルを変更した場合は、storage を削除（reset()）して作り直してください。"
            )

    def add_entity(self, name: str, etype: str, desc: str, origin: int, vec: np.ndarray) -> Entity:
        vec = _normalize(vec).astype(np.float32)
        self._validate_dim(vec)  # 変異前に検証（失敗時に entities とベクタが食い違わないように）
        eid = self._ent_seq
        self._ent_seq += 1
        ent = Entity(id=eid, name=name, type=etype, description=desc, origin_nodes=[origin])
        self.entities[eid] = ent
        self._append_vec(eid, vec)
        return ent

    def merge_entity(self, target_eid: int, name: str, origin: int, vec: np.ndarray) -> None:
        """新エンティティを既存 target にマージし、GT-Link を集約（4.3.2 末尾）。"""
        vec = _normalize(vec).astype(np.float32)
        self._validate_dim(vec)  # 変異前に検証
        ent = self.entities[target_eid]
        if origin not in ent.origin_nodes:
            ent.origin_nodes.append(origin)
        row = self._row_of[target_eid]  # O(1)
        self._entity_vecs[row] = _normalize((self._entity_vecs[row] + vec) / 2.0)

    def _append_vec(self, eid: int, vec: np.ndarray) -> None:
        vec = _normalize(vec).astype(np.float32)
        self._validate_dim(vec)
        if self._dim is None:
            self._dim = int(vec.shape[0])
            self._entity_vecs = np.zeros((64, self._dim), dtype=np.float32)
        if self._count >= self._entity_vecs.shape[0]:  # 容量ダブリング
            grown = np.zeros((self._entity_vecs.shape[0] * 2, self._dim), dtype=np.float32)
            grown[: self._count] = self._entity_vecs[: self._count]
            self._entity_vecs = grown
        self._entity_vecs[self._count] = vec
        self._row_of[eid] = self._count
        self._entity_ids.append(eid)
        self._count += 1

    def search_entities(self, vec: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """エンティティ・ベクタDB に対する top_k 近傍検索（Algorithm 1 Line 1）。"""
        if self._count == 0:
            return []
        sims = self._entity_vecs[: self._count] @ _normalize(vec).astype(np.float32)
        order = np.argsort(-sims)[:top_k]
        return [(self._entity_ids[i], float(sims[i])) for i in order]

    def find_entity_by_name(self, name: str) -> int | None:
        name_l = name.strip().lower()
        for e in self.entities.values():
            if e.name.strip().lower() == name_l:
                return e.id
        return None

    # ---- 永続化 ----
    def persist(self, storage_dir: str | Path) -> None:
        d = Path(storage_dir)
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "nodes": [asdict(n) for n in self.nodes.values()],
            "roots": self.roots,
            "entities": [asdict(e) for e in self.entities.values()],
            "relations": self.relations,
            "node_seq": self._node_seq,
            "ent_seq": self._ent_seq,
            "entity_ids": self._entity_ids,
        }
        # アトミック書き込み: 一時ファイルへ書いてから os.replace（中断時の索引破損防止）
        import os

        tmp_json = d / "bookindex.json.tmp"
        tmp_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_json, d / "bookindex.json")
        tmp_npy = d / "entity_vecs.tmp.npy"
        np.save(tmp_npy, self._entity_vecs[: self._count])
        os.replace(tmp_npy, d / "entity_vecs.npy")

    @classmethod
    def load(cls, storage_dir: str | Path) -> "BookIndex":
        d = Path(storage_dir)
        payload = json.loads((d / "bookindex.json").read_text(encoding="utf-8"))
        bi = cls()
        bi.nodes = {n["id"]: TreeNode(**n) for n in payload["nodes"]}
        bi.roots = payload["roots"]
        bi.entities = {e["id"]: Entity(**e) for e in payload["entities"]}
        bi.relations = [tuple(r) for r in payload["relations"]]
        bi._node_seq = payload["node_seq"]
        bi._ent_seq = payload["ent_seq"]
        bi._entity_ids = list(payload["entity_ids"])
        vecs = np.load(d / "entity_vecs.npy")
        bi._entity_vecs = vecs.astype(np.float32)
        bi._count = vecs.shape[0]
        bi._dim = vecs.shape[1] if vecs.ndim == 2 and vecs.shape[0] else None
        bi._row_of = {eid: i for i, eid in enumerate(bi._entity_ids)}
        return bi


def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


# --------------------------------------------------------------------------
# 4.2 Tree Construction
# --------------------------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# 章番号風の見出し: "1", "1.2", "第3章" など
_NUM_HEADING = re.compile(r"^(\d+(\.\d+){0,3})\s+\S|^第[0-9一二三四五六七八九十百]+[章節編]")


def parse_blocks(path: Path, *, ocr=False, layout=False) -> list[dict]:
    """Layout Parsing（4.2.1）。文書を素朴なブロック列に分解する。

    各ブロック: {"content", "type"(Title/Text/Table/Image), "page", "font"}
    - .md/.markdown: 見出しレベルが明示的なので最も正確に木を作れる（推奨）。
    - .pdf: pypdf でページ毎テキストを取り、見出しらしさをヒューリスティック判定。
    - .docx: 見出しスタイル(Heading n)から階層を復元（比較的良好）。
    - .pptx / .xlsx: 一応読めるが BookRAG と相性が悪い（警告あり）。
    - .txt 等: 段落を Text ブロックに。
    """
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            log("UTF-8 で読めないため文字化けを置換して読み込みます（CP932 等は UTF-8 保存を推奨）")
            text = path.read_text(encoding="utf-8", errors="replace")
        return _parse_markdown(text)
    if suffix == ".pdf":
        # 版面解析つきパース（pymupdf のフォントサイズ見出し + 任意 OCR）。
        # pymupdf 未導入 or ocr/layout 無効なら pypdf のヒューリスティックへ。
        if ocr or layout not in (None, False):
            from . import docparse

            blocks = docparse.parse_pdf(path, ocr=ocr, layout=(layout or "auto"))
            if blocks is not None:
                if not blocks:
                    log("PDF からテキストを取得できませんでした（スキャンPDFなら ocr=True と "
                        "Tesseract 導入が必要）。")
                return blocks
            log("pymupdf 未導入のため pypdf で解析します（OCR/版面解析は無効）。"
                "  pip install -e \".[ocr]\"")
        return _parse_pdf(path)
    if suffix in (".docx", ".doc"):
        _warn_format(suffix)
        return _parse_docx(path)
    if suffix == ".pptx":
        _warn_format(suffix)
        return _parse_pptx(path)
    if suffix in (".xlsx", ".xls"):
        _warn_format(suffix)
        return _parse_xlsx(path)
    return _parse_plain(path.read_text(encoding="utf-8", errors="ignore"))


def _warn_format(suffix: str) -> None:
    """BookRAG と相性が悪い形式の注意（デメリットと推奨方式）を表示する。"""
    if suffix in (".docx", ".doc"):
        log("⚠ Word を BookRAG で処理します。見出しスタイル(Heading n)から階層を作りますが、"
            "スタイル未使用の文書は木が浅くなります。構造重視なら Markdown 化、"
            "単純な検索用途なら PagedRAG も検討してください。")
    elif suffix == ".pptx":
        log("⚠ PowerPoint を BookRAG で処理します。スライドは階層的な散文ではないため"
            "木/知識グラフの精度が落ちがちです。推奨: 検索は PagedRAG、要点抽出はチャット。")
    elif suffix in (".xlsx", ".xls"):
        log("⚠ Excel を BookRAG で処理します。表データは BookRAG の木/知識グラフと相性が悪く、"
            "精度が低い可能性が高いです。推奨: 集計・計算は TableQA、検索は PagedRAG、"
            "文章と表が混在するなら DocQA。")


def _parse_docx(path: Path) -> list[dict]:
    """Word。見出しスタイルを Title(level)、本文を Text、表を Table に。"""
    try:
        from docx import Document
    except ImportError:
        log(".docx には python-docx が必要です: pip install python-docx（スキップ）")
        return []
    blocks: list[dict] = []
    try:
        doc = Document(str(path))
    except Exception as e:  # noqa: BLE001
        log(f"Word の読み込みに失敗（スキップ）: {e}")
        return []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name if para.style else "") or ""
        if style.lower().startswith("heading"):
            m = re.search(r"(\d+)", style)
            level = int(m.group(1)) if m else 1
            blocks.append({"content": text, "type": "Title", "page": None, "font": None, "level": level})
        else:
            blocks.append({"content": text, "type": "Text", "page": None, "font": None, "level": None})
    for tb in doc.tables:
        rows = [" | ".join(c.text for c in row.cells) for row in tb.rows]
        rows = [r for r in rows if r.strip()]
        if rows:
            blocks.append({"content": "\n".join(rows), "type": "Table",
                           "page": None, "font": None, "level": None})
    return blocks


def _parse_pptx(path: Path) -> list[dict]:
    """PowerPoint。各スライドのタイトルを Title(level1)、本文を Text に。"""
    try:
        from pptx import Presentation
    except ImportError:
        log(".pptx には python-pptx が必要です: pip install python-pptx（スキップ）")
        return []
    blocks: list[dict] = []
    try:
        prs = Presentation(str(path))
    except Exception as e:  # noqa: BLE001
        log(f"PowerPoint の読み込みに失敗（スキップ）: {e}")
        return []
    for i, slide in enumerate(prs.slides, start=1):
        title = None
        try:
            if slide.shapes.title and slide.shapes.title.text.strip():
                title = slide.shapes.title.text.strip()
        except Exception:  # noqa: BLE001
            title = None
        blocks.append({"content": title or f"Slide {i}", "type": "Title",
                       "page": i, "font": None, "level": 1})
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            txt = shape.text_frame.text.strip()
            if txt and txt != title:
                blocks.append({"content": txt, "type": "Text", "page": i, "font": None, "level": None})
    return blocks


def _parse_xlsx(path: Path) -> list[dict]:
    """Excel。各シートを Title(level1)、行をまとめて Table に（相性は悪い）。"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        log(".xlsx には openpyxl が必要です: pip install openpyxl（スキップ）")
        return []
    blocks: list[dict] = []
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        log(f"Excel の読み込みに失敗（スキップ）: {e}")
        return []
    for ws in wb.worksheets:
        blocks.append({"content": str(ws.title), "type": "Title",
                       "page": str(ws.title), "font": None, "level": 1})
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                rows.append(" | ".join(cells))
        if rows:
            blocks.append({"content": "\n".join(rows), "type": "Table",
                           "page": str(ws.title), "font": None, "level": None})
    return blocks


def _parse_markdown(text: str) -> list[dict]:
    blocks: list[dict] = []
    buf: list[str] = []

    def flush():
        if buf:
            content = "\n".join(buf).strip()
            if content:
                is_table = "|" in content and re.search(r"\|.*\|", content) is not None
                blocks.append(
                    {"content": content, "type": "Table" if is_table else "Text",
                     "page": None, "font": None, "level": None}
                )
            buf.clear()

    for line in text.splitlines():
        m = _MD_HEADING.match(line)
        if m:
            flush()
            blocks.append({"content": m.group(2).strip(), "type": "Title",
                           "page": None, "font": float(7 - len(m.group(1))), "level": len(m.group(1))})
        else:
            buf.append(line)
    flush()
    return blocks


def _parse_plain(text: str) -> list[dict]:
    blocks = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        is_heading = bool(_NUM_HEADING.match(para)) and len(para) < 80
        blocks.append({"content": para, "type": "Title" if is_heading else "Text",
                       "page": None, "font": None, "level": None})
    return blocks


def _parse_pdf(path: Path) -> list[dict]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    blocks = []
    for pno, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(para.split())
            if not para:
                continue
            short = len(para) < 80
            is_heading = short and (bool(_NUM_HEADING.match(para)) or para.isupper())
            blocks.append({"content": para, "type": "Title" if is_heading else "Text",
                           "page": pno, "font": None, "level": None})
    return blocks


def section_filter(blocks: list[dict], *, use_llm: bool = True) -> list[dict]:
    """Section Filtering（4.2.2）。Title 候補に階層 level と最終 type を割り当てる。

    Markdown 由来で level が既知ならそのまま使う。未知（PDF/plain）の Title 候補は
    LLM でレベル判定・誤検出の Text 再分類を行う（use_llm=True）。
    """
    title_idx = [i for i, b in enumerate(blocks) if b["type"] == "Title" and b.get("level") is None]
    candidate_set = set(title_idx)  # LLM が返す id は候補集合のみ受理（無関係ブロックの破壊防止）
    if title_idx and use_llm:
        candidates = [{"id": i, "text": blocks[i]["content"][:120]} for i in title_idx]
        # 長文書対策でバッチ分割
        for start in range(0, len(candidates), 40):
            batch = candidates[start : start + 40]
            result = llm_json(
                _PROMPT_SECTION_FILTER + "\n\nCandidates JSON:\n" + json.dumps(batch, ensure_ascii=False)
            )
            if isinstance(result, list):
                for item in result:
                    if not isinstance(item, dict):  # LLM 応答の形状不正はスキップ
                        continue
                    i = item.get("id")
                    if not isinstance(i, int) or i not in candidate_set:
                        continue
                    lvl = item.get("level")
                    if lvl in (None, 0, "None", "none"):
                        blocks[i]["type"] = "Text"  # 誤検出を Text に再分類
                    else:
                        try:
                            blocks[i]["level"] = max(1, int(lvl))
                        except (TypeError, ValueError):
                            pass  # フォールバック（下の番号推定）に任せる
    # LLM 未使用 or 失敗時のフォールバック: 番号の深さからレベル推定
    for i in title_idx:
        b = blocks[i]
        if b["type"] == "Title" and b.get("level") is None:
            m = re.match(r"^(\d+(\.\d+){0,3})", b["content"])
            b["level"] = (m.group(1).count(".") + 1) if m else 1
    return blocks


def blocks_range(blocks):  # 小ヘルパ（id 範囲チェック）
    return range(len(blocks))


def _chunk_text(text: str, chunk_chars: int) -> list[str]:
    """テキストを chunk_chars 程度の塊に分割（改行境界優先）。"""
    text = text.strip()
    if len(text) <= chunk_chars:
        return [text] if text else []
    chunks, buf, size = [], [], 0
    for line in text.split("\n"):
        if size + len(line) > chunk_chars and buf:
            chunks.append("\n".join(buf).strip())
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf).strip())
    return [c for c in chunks if c]


def build_tree(bi: BookIndex, blocks: list[dict], book: str, *, chunk_chars: int = 1500) -> int:
    """フィルタ済みブロック列から木 T を組み立て、その書のルート node id を返す（4.2.2 末尾）。

    本文（Text）は段落ごとにノード化せず、**節の下でまとめて chunk_chars 程度のチャンク**に
    する。これによりノード数（= 後段の LLM 抽出回数）を大幅に削減する。
    Table / Image は構造を保つため単独ノードのまま。
    """
    root = bi.add_node(type="Section", content=book, book=book, level=0, title=book)
    bi.roots.append(root.id)
    stack: list[tuple[int, int]] = [(0, root.id)]

    buf: list[str] = []
    buf_page = [None]  # チャンク先頭ページ

    def _flush(parent: int) -> None:
        if not buf:
            return
        for chunk in _chunk_text("\n".join(buf), chunk_chars):
            node = bi.add_node(type="Text", content=chunk, book=book,
                               page=buf_page[0], parent=parent)
            bi.nodes[parent].children.append(node.id)
        buf.clear()
        buf_page[0] = None

    for b in blocks:
        parent = stack[-1][1] if stack else root.id
        if b["type"] == "Title":
            _flush(parent)
            level = b.get("level") or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else root.id
            node = bi.add_node(type="Section", content=b["content"], book=book,
                               level=level, title=b["content"], page=b.get("page"), parent=parent)
            bi.nodes[parent].children.append(node.id)
            stack.append((level, node.id))
        elif b["type"] in ("Table", "Image"):
            _flush(parent)
            node = bi.add_node(type=b["type"], content=b["content"], book=book,
                               page=b.get("page"), parent=parent)
            bi.nodes[parent].children.append(node.id)
        else:  # Text: バッファに溜めてチャンク化
            if buf_page[0] is None:
                buf_page[0] = b.get("page")
            buf.append(b["content"])

    _flush(stack[-1][1] if stack else root.id)
    return root.id


# --------------------------------------------------------------------------
# 4.3 Graph Construction + Gradient-based Entity Resolution (Algorithm 1)
# --------------------------------------------------------------------------


def build_graph(bi: BookIndex, node_ids: list[int], *, gradient_g: float = 0.6,
                er_top_k: int = 10, max_workers: int = 8, min_chars: int = 40,
                max_nodes: int = 300, er_use_llm: bool = False) -> None:
    """各ノードからエンティティ/関係を抽出し、Gradient-based ER で KG を構築する。

    速度のため、抽出（LLM 呼び出し + 埋め込み）はノード単位で **並列化** し、
    Gradient ER とグラフ構築（順序依存・BookIndex を変更する）は **逐次** で行う。
    - 短すぎるノード（min_chars 未満）は抽出をスキップ。
    - 対象ノードは max_nodes で上限（超過分は警告して打ち切り）。
    - er_use_llm=False（既定）なら曖昧マージで LLM を呼ばず、最有力候補を採用（高速）。
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [
        nid for nid in node_ids
        if bi.nodes[nid].type != "Section"
        and len(bi.nodes[nid].content.strip()) >= min_chars
    ]
    if not targets:
        # 空文書/スキャンPDF等。従来はここで黙って戻り「読めていない」ことに気づけなかった。
        log("警告: 抽出対象の本文ノードが 0 件です。文書からテキストを取得できていない可能性が"
            "あります（スキャンPDF・空文書・全ノードが min_chars 未満）。")
        return
    if len(targets) > max_nodes:
        print(f"[BookRAG] 対象ノード {len(targets)} 件を max_nodes={max_nodes} 件に打ち切ります"
              "（増やすには add_book(max_nodes=...)）")
        targets = targets[:max_nodes]

    def _work(nid: int):
        # 抽出フェーズは BookIndex を読むだけ（スレッド安全）。失敗しても全体を止めない。
        try:
            node = bi.nodes[nid]
            data = _extract_graph(node)                       # entities + relations を1回で
            ents = data["entities"]
            if not ents:
                return nid, [], [], None
            names = [f"{e['name']} ({e.get('type','')}): {e.get('description','')}" for e in ents]
            return nid, ents, data["relations"], embed(names)
        except Exception:  # noqa: BLE001
            return nid, [], [], None

    results = []
    total = len(targets)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for res in progress(ex.map(_work, targets), total=total,
                            desc=f"抽出: エンティティ/関係 ({max_workers}並列)"):
            results.append(res)

    # 空振り検知: 1件もエンティティが取れない場合は“読めていない”状態。
    # 例外/タイムアウトは握り潰すため、ここで明示的に警告する。
    found = sum(len(ents) for _, ents, _, _ in results)
    print(f"[BookRAG] 抽出エンティティ合計: {found}")
    if found == 0:
        print(
            "[BookRAG] 警告: エンティティが1件も抽出できませんでした。考えられる原因:\n"
            "  - モデルが JSON 形式で返していない（指示無視）\n"
            "  - 生成エンドポイントへのリクエストが失敗/タイムアウトしている\n"
            "  - 文書テキストが空（スキャンPDF等でテキスト抽出できていない）\n"
            "  確認: llmlab.complete('Return JSON: {\"ok\":true}') が JSON を返すか、"
            "request_timeout を延ばす、PDF にテキスト層があるか。"
        )

    # Gradient ER + グラフ構築は順序依存のため逐次（入力ノード順）。進捗も表示。
    for nid, ents, rels, vecs in progress(results, total=len(results),
                                          desc="名寄せ/グラフ構築 (KG)"):
        if not ents:
            continue
        local_name_to_eid: dict[str, int] = {}
        for e, vec in zip(ents, vecs):
            eid = gradient_entity_resolution(
                bi, e["name"], e.get("type", ""), e.get("description", ""),
                nid, vec, gradient_g=gradient_g, er_top_k=er_top_k, use_llm=er_use_llm,
            )
            local_name_to_eid[e["name"].strip().lower()] = eid
        for rel in rels:
            s = local_name_to_eid.get(str(rel.get("source", "")).strip().lower())
            t = local_name_to_eid.get(str(rel.get("target", "")).strip().lower())
            if s is not None and t is not None and s != t:
                bi.relations.append((s, t, str(rel.get("relation", "related_to"))))


def gradient_entity_resolution(bi: BookIndex, name: str, etype: str, desc: str, origin: int,
                               vec: np.ndarray, *, gradient_g: float, er_top_k: int,
                               use_llm: bool = False) -> int:
    """Algorithm 1（Gradient-based entity resolution）。返り値は確定したエンティティ id。

    新規概念なら追加、既存の別名なら最も確からしい正準エンティティへマージする。
    reranker は持たないため、ベクトルDB のコサイン類似度を rerank スコア S とみなす。
    use_llm=True のときだけ、複数の高関連候補がある場合に LLM で1つ選ぶ（遅い）。
    """
    candidates = bi.search_entities(vec, er_top_k)  # Line 1: Search
    if not candidates:
        return bi.add_entity(name, etype, desc, origin, vec).id

    # ほぼ同一（コサイン>=0.95）は勾配判定を待たず即マージする。
    # 候補が er_top_k 未満の小規模索引では「リストを使い切った」ことが Case A と
    # 区別できず、完全一致でも重複追加されてしまうため（索引成長初期の名寄せ崩壊対策）。
    if candidates[0][1] >= 0.95:
        bi.merge_entity(candidates[0][0], name, origin, vec)
        return candidates[0][0]

    # Line 2-3: rerank（=コサイン）して降順ソート済み
    scores = [s for _, s in candidates]
    sel = [candidates[0][0]]          # Line 4: Sel ← Ec[0]
    prev = scores[0]
    for (eid, sc) in candidates[1:]:  # Line 5-8: 連続スコアの急落（gradient）を検出
        if sc > gradient_g * prev:    # 緩やかな低下 → 高関連集合に含める
            sel.append(eid)
            prev = sc
        else:
            break

    if len(sel) == len(candidates):   # Line 9-10: Case A（勾配なし）→ 新規エンティティ
        return bi.add_entity(name, etype, desc, origin, vec).id

    # Line 11-14: Case B → マージ
    if len(sel) == 1 or not use_llm:
        v_sel = sel[0]                # 既定: LLM を呼ばず最有力候補を採用（高速）
    else:
        v_sel = _llm_select_entity(bi, name, etype, desc, sel)
        if v_sel is None:
            return bi.add_entity(name, etype, desc, origin, vec).id
    bi.merge_entity(v_sel, name, origin, vec)
    return v_sel


def _extract_graph(node: TreeNode) -> dict:
    """1 回の LLM 呼び出しでノードからエンティティと関係をまとめて抽出する。"""
    result = llm_json(
        _PROMPT_GRAPH_EXTRACT + f"\n\nNode type: {node.type}\nContent:\n{node.content[:2500]}"
    )
    ents: list[dict] = []
    rels: list[dict] = []
    if isinstance(result, dict):
        raw_e = result.get("entities", [])
        if isinstance(raw_e, list):
            ents = [e for e in raw_e if isinstance(e, dict) and e.get("name")]
        raw_r = result.get("relations", [])
        if isinstance(raw_r, list):
            rels = [r for r in raw_r if isinstance(r, dict) and r.get("source") and r.get("target")]
    return {"entities": ents, "relations": rels}


def _llm_select_entity(bi: BookIndex, name: str, etype: str, desc: str,
                       candidate_ids: list[int]) -> int | None:
    """Algorithm 1 Line 13: 複数の高関連候補から正準エンティティを LLM で1つ選ぶ。"""
    cands = [
        {"id": eid, "name": bi.entities[eid].name, "type": bi.entities[eid].type,
         "description": bi.entities[eid].description[:160]}
        for eid in candidate_ids
    ]
    result = llm_json(
        _PROMPT_ENTITY_RESOLUTION
        + f"\n\nNew Entity: {json.dumps({'name': name, 'type': etype, 'description': desc[:160]}, ensure_ascii=False)}"
        + f"\nCandidate Entities: {json.dumps(cands, ensure_ascii=False)}"
    )
    if isinstance(result, dict):
        sid = result.get("select_id", -1)
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return None
        return sid if sid in candidate_ids else None
    return None


# --------------------------------------------------------------------------
# プロンプト（論文 Fig.10-13 に対応／日本語文書にも対応するよう調整）
# --------------------------------------------------------------------------

_PROMPT_SECTION_FILTER = """You analyze heading candidates extracted from a document and decide,
for each, its hierarchical level (1 = top-level section, 2, 3, ...) or null if it is NOT a real
section heading (i.e., it is body text mistakenly detected as a title).
Return ONLY a JSON array: [{"id": <int>, "level": <int or null>}, ...]."""

_PROMPT_GRAPH_EXTRACT = """You are an information extraction engine. From the given document node,
extract (1) the key entities and (2) the directed relations among those entities (relations must only
reference the extracted entities). For tables, also extract the table itself as one entity plus its
row/column headers. Keep names canonical and concise, and output in the document's language.
Return ONLY JSON:
{"entities":[{"name": str, "type": str, "description": str}, ...],
 "relations":[{"source": str, "target": str, "relation": str}, ...]}."""

_PROMPT_ENTITY_RESOLUTION = """You are an Entity Resolution Adjudicator. Decide if the New Entity refers to
the EXACT same real-world concept as one of the Candidate Entities. Be strict and conservative; when in
doubt output -1. Names must be extremely similar, a direct abbreviation, or a well-known alias. Distinct
parallel concepts are NOT a match even if descriptions look similar.
Return ONLY JSON: {"select_id": <candidate id or -1>, "explanation": str}."""
