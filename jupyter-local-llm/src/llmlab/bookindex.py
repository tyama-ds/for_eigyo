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
        self._entity_vecs: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        self._entity_ids: list[int] = []  # _entity_vecs の行に対応する entity id

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
    def add_entity(self, name: str, etype: str, desc: str, origin: int, vec: np.ndarray) -> Entity:
        eid = self._ent_seq
        self._ent_seq += 1
        ent = Entity(id=eid, name=name, type=etype, description=desc, origin_nodes=[origin])
        self.entities[eid] = ent
        self._append_vec(eid, vec)
        return ent

    def merge_entity(self, target_eid: int, name: str, origin: int, vec: np.ndarray) -> None:
        """新エンティティを既存 target にマージし、GT-Link を集約（4.3.2 末尾）。"""
        ent = self.entities[target_eid]
        if origin not in ent.origin_nodes:
            ent.origin_nodes.append(origin)
        # ベクトルは平均で更新（DB ←Update に相当）
        row = self._entity_ids.index(target_eid)
        self._entity_vecs[row] = _normalize((self._entity_vecs[row] + vec) / 2.0)

    def _append_vec(self, eid: int, vec: np.ndarray) -> None:
        vec = _normalize(vec).reshape(1, -1)
        if self._entity_vecs.size == 0:
            self._entity_vecs = vec
        else:
            self._entity_vecs = np.vstack([self._entity_vecs, vec])
        self._entity_ids.append(eid)

    def search_entities(self, vec: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """エンティティ・ベクタDB に対する top_k 近傍検索（Algorithm 1 Line 1）。"""
        if self._entity_vecs.size == 0:
            return []
        sims = self._entity_vecs @ _normalize(vec)
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
        (d / "bookindex.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        np.save(d / "entity_vecs.npy", self._entity_vecs)

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
        bi._entity_ids = payload["entity_ids"]
        bi._entity_vecs = np.load(d / "entity_vecs.npy")
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


def parse_blocks(path: Path) -> list[dict]:
    """Layout Parsing（4.2.1）。文書を素朴なブロック列に分解する。

    各ブロック: {"content", "type"(Title/Text/Table/Image), "page", "font"}
    - .md/.markdown: 見出しレベルが明示的なので最も正確に木を作れる。
    - .pdf: pypdf でページ毎テキストを取り、見出しらしさをヒューリスティック判定。
    - .txt 等: 段落を Text ブロックに。
    """
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        return _parse_markdown(path.read_text(encoding="utf-8"))
    if suffix == ".pdf":
        return _parse_pdf(path)
    return _parse_plain(path.read_text(encoding="utf-8", errors="ignore"))


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
                    i = item.get("id")
                    if i in blocks_range(blocks):
                        lvl = item.get("level")
                        if lvl in (None, 0, "None", "none"):
                            blocks[i]["type"] = "Text"  # 誤検出を Text に再分類
                        else:
                            blocks[i]["level"] = int(lvl)
    # LLM 未使用 or 失敗時のフォールバック: 番号の深さからレベル推定
    for i in title_idx:
        b = blocks[i]
        if b["type"] == "Title" and b.get("level") is None:
            m = re.match(r"^(\d+(\.\d+){0,3})", b["content"])
            b["level"] = (m.group(1).count(".") + 1) if m else 1
    return blocks


def blocks_range(blocks):  # 小ヘルパ（id 範囲チェック）
    return range(len(blocks))


def build_tree(bi: BookIndex, blocks: list[dict], book: str) -> int:
    """フィルタ済みブロック列から木 T を組み立て、その書のルート node id を返す（4.2.2 末尾）。"""
    root = bi.add_node(type="Section", content=book, book=book, level=0, title=book)
    bi.roots.append(root.id)
    # (level, node_id) のスタックで親子の入れ子を決める
    stack: list[tuple[int, int]] = [(0, root.id)]

    for b in blocks:
        if b["type"] == "Title":
            level = b.get("level") or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else root.id
            node = bi.add_node(type="Section", content=b["content"], book=book,
                               level=level, title=b["content"], page=b.get("page"), parent=parent)
            bi.nodes[parent].children.append(node.id)
            stack.append((level, node.id))
        else:
            parent = stack[-1][1] if stack else root.id
            node = bi.add_node(type=b["type"], content=b["content"], book=book,
                               page=b.get("page"), parent=parent)
            bi.nodes[parent].children.append(node.id)
    return root.id


# --------------------------------------------------------------------------
# 4.3 Graph Construction + Gradient-based Entity Resolution (Algorithm 1)
# --------------------------------------------------------------------------


def build_graph(bi: BookIndex, node_ids: list[int], *, gradient_g: float = 0.6,
                er_top_k: int = 10, max_workers: int = 8, min_chars: int = 40) -> None:
    """各ノードからエンティティ/関係を抽出し、Gradient-based ER で KG を構築する。

    速度のため、抽出（LLM 呼び出し + 埋め込み）はノード単位で **並列化** し、
    Gradient ER とグラフ構築（順序依存・BookIndex を変更する）は **逐次** で行う。
    短すぎるノード（min_chars 未満）は抽出をスキップして無駄な呼び出しを省く。
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [
        nid for nid in node_ids
        if bi.nodes[nid].type != "Section"
        and len(bi.nodes[nid].content.strip()) >= min_chars
    ]
    if not targets:
        return

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
        for i, res in enumerate(ex.map(_work, targets), start=1):  # map は入力順を保持
            results.append(res)
            if i % 20 == 0 or i == total:
                print(f"[BookRAG] 抽出 {i}/{total} ノード")

    # Gradient ER + グラフ構築は順序依存のため逐次（入力ノード順）。
    for nid, ents, rels, vecs in results:
        if not ents:
            continue
        local_name_to_eid: dict[str, int] = {}
        for e, vec in zip(ents, vecs):
            eid = gradient_entity_resolution(
                bi, e["name"], e.get("type", ""), e.get("description", ""),
                nid, vec, gradient_g=gradient_g, er_top_k=er_top_k,
            )
            local_name_to_eid[e["name"].strip().lower()] = eid
        for rel in rels:
            s = local_name_to_eid.get(str(rel.get("source", "")).strip().lower())
            t = local_name_to_eid.get(str(rel.get("target", "")).strip().lower())
            if s is not None and t is not None and s != t:
                bi.relations.append((s, t, str(rel.get("relation", "related_to"))))


def gradient_entity_resolution(bi: BookIndex, name: str, etype: str, desc: str, origin: int,
                               vec: np.ndarray, *, gradient_g: float, er_top_k: int) -> int:
    """Algorithm 1（Gradient-based entity resolution）。返り値は確定したエンティティ id。

    新規概念なら追加、既存の別名なら最も確からしい正準エンティティへマージする。
    reranker は持たないため、ベクトルDB のコサイン類似度を rerank スコア S とみなす。
    """
    candidates = bi.search_entities(vec, er_top_k)  # Line 1: Search
    if not candidates:
        return bi.add_entity(name, etype, desc, origin, vec).id

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
    if len(sel) == 1:
        v_sel = sel[0]
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
