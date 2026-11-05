"""ナレッジグラフ（取り込み時の抽出・文書横断のグラフ構築・検索）。

採用した手法と理由（文書管理用途向けの組み合わせ）:
  - 抽出: Microsoft GraphRAG / LightRAG と同じく、チャンクごとに LLM でエンティティ・関係を
    抽出し、加えて「上位概念キーワード」（LightRAG の high-level keys）も取る。
    **文書単位で保存**するので、文書の追加・削除がグラフ全体の再構築なしに反映される
    （LightRAG の増分更新の考え方。文書が増え続ける文書管理に必須）。
  - 関係の端点がエンティティ一覧に無いときは端点を補う（ローカル LLM で頻発する欠落対策）。
  - local 検索: HippoRAG 2 の Personalized PageRank。質問に出るエンティティを種に PPR を
    回し、エンティティのスコアをチャンクへ伝播させる（多段の関係をたどる質問に強い）。
  - global 検索: LazyGraphRAG の考え方で、コミュニティ要約は**取り込み時に作らず**、質問時に
    関連の高いコミュニティだけ要約してキャッシュする（取り込みコストを大幅に削減）。
  - コミュニティ検出は依存なしのラベル伝播法（本家の Leiden 法の軽量代替）。
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import unicodedata
from collections import Counter, defaultdict

from . import db
from .textutil import BM25Index, as_str_list, extract_json, parse_score

ENTITY_TYPES = "組織 / 人物 / 製品 / 技術 / 設備 / 工程 / 材料 / 規格・法令 / 場所 / 日付・イベント / 指標 / 文書 / 概念"

EXTRACT_SYSTEM = ("あなたはナレッジグラフ構築のための情報抽出器です。"
                  "指示された JSON だけを出力し、他の文章は書かないでください。")

EXTRACT_PROMPT = """[TASK:graph_extract]
以下は文書「{title}」（{locator}）の一部です。重要なエンティティと、エンティティ間の関係、
この部分の主題を表す上位概念キーワードを抽出してください。

出力は次の JSON のみ:
{{"entities": [{{"name": "名称", "type": "種別", "description": "本文に基づく1〜2文の説明"}}],
 "relations": [{{"source": "名称", "target": "名称", "description": "関係の説明", "strength": 5}}],
 "keywords": ["上位概念キーワード（3〜6個）"]}}

- 種別は次から選ぶ: {types}
- name は本文中の表記をそのまま使う（略称と正式名があれば正式名）
- relations の source / target は entities の name と一致させる
- strength は関係の強さ（1〜10 の整数）
- 本文に無いことは書かない

# テキスト
{text}
"""

COMMUNITY_PROMPT = """[TASK:community_summary]
以下はナレッジグラフで互いに関連の強いエンティティ群（コミュニティ）です。
何についてのまとまりか、タイトルと要約を作ってください。

出力は次の JSON のみ:
{{"title": "短いタイトル", "summary": "エンティティと関係を踏まえた5文以内の要約"}}

# エンティティ
{entities}

# 関係
{relations}
"""

# 法人格などの接頭・接尾辞（表記ゆれでエンティティが分断されるのを防ぐ）
_CORP_RE = re.compile(
    r"^(?:株式会社|有限会社|合同会社|合資会社|一般社団法人|公益財団法人|\(株\)|㈱|\(有\))"
    r"|(?:株式会社|有限会社|合同会社|合資会社|\(株\)|㈱|\(有\)"
    r"|inc\.?|corp\.?|co\.,?\s*ltd\.?|ltd\.?|k\.k\.)$")


def norm_key(name: str) -> str:
    """エンティティ名の照合キー（NFKC + 空白除去 + 小文字化 + 法人格除去）。"""
    base = "".join(unicodedata.normalize("NFKC", str(name)).split()).casefold()
    s, prev = base, None
    while prev != s:
        prev = s
        s = _CORP_RE.sub("", s).strip("・,、。.-　 「」『』\"'")
    return s or base


# ---------------------------------------------------------------- 抽出（取り込み時）

def extract_chunk(llm, profile: str, sensitivity: str, title: str, chunk: dict) -> dict | None:
    """1チャンクからエンティティ・関係・キーワードを抽出する。解析失敗時は1回だけ再要求。"""
    prompt = EXTRACT_PROMPT.format(title=title[:80], locator=chunk.get("locator") or "",
                                   text=chunk["text"][:3500], types=ENTITY_TYPES)
    for attempt in range(2):
        text = llm.chat(profile, prompt if attempt == 0 else prompt
                        + "\n\n【再出力】前回の出力は JSON として解析できませんでした。"
                          "指示した JSON のみを出力してください。",
                        sensitivity=sensitivity, system=EXTRACT_SYSTEM,
                        max_tokens=4096, purpose="ingest")
        data = extract_json(text)
        if isinstance(data, dict):
            return _clean_extraction(data)
    return None


def _clean_extraction(data: dict) -> dict:
    ents, rels = [], []
    for e in data.get("entities") or []:
        if isinstance(e, str):
            e = {"name": e}
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        if not name or len(name) > 80:
            continue
        ents.append({"name": name, "type": str(e.get("type", "")).strip()[:20] or "概念",
                     "description": str(e.get("description", "")).strip()[:400]})
    known = {norm_key(e["name"]) for e in ents}
    for r in data.get("relations") or []:
        if not isinstance(r, dict):
            continue
        src, tgt = str(r.get("source", "")).strip(), str(r.get("target", "")).strip()
        if not src or not tgt or norm_key(src) == norm_key(tgt) or len(src) > 80 or len(tgt) > 80:
            continue
        rels.append({"source": src, "target": tgt,
                     "description": str(r.get("description", "")).strip()[:300],
                     "strength": parse_score(r.get("strength"), default=5, lo=1, hi=10)})
        for side in (src, tgt):           # 端点の補完
            if norm_key(side) not in known:
                known.add(norm_key(side))
                ents.append({"name": side, "type": "概念", "description": ""})
    return {"entities": ents, "relations": rels,
            "keywords": as_str_list(data.get("keywords"), limit=8, maxlen=40)}


def save_extraction(doc_id: str, chunk_id: str, data: dict) -> None:
    em = [(norm_key(e["name"]), e["name"], e["type"], e["description"], doc_id, chunk_id)
          for e in data["entities"]]
    rm = []
    for r in data["relations"]:
        a, b = norm_key(r["source"]), norm_key(r["target"])
        if a > b:
            a, b = b, a
        rm.append((a, b, r["description"], r["strength"], doc_id, chunk_id))
    stmts = [("INSERT INTO entity_mentions(entity_key,name,type,description,doc_id,chunk_id) "
              "VALUES(?,?,?,?,?,?)", row) for row in em]
    stmts += [("INSERT INTO relation_mentions(source,target,description,strength,doc_id,chunk_id) "
               "VALUES(?,?,?,?,?,?)", row) for row in rm]
    import json
    stmts.append(("UPDATE chunks SET keywords=? WHERE id=?",
                  (json.dumps(data.get("keywords") or [], ensure_ascii=False), chunk_id)))
    db.write_many(stmts)


# ---------------------------------------------------------------- グラフ（検索時）

class KnowledgeGraph:
    """entity_mentions / relation_mentions から作る文書横断グラフ（許可された文書だけ）。"""

    def __init__(self, allowed_docs: set[str] | None, doc_sens: dict[str, str]):
        self.entities: dict[str, dict] = {}
        self.relations: dict[tuple[str, str], dict] = {}
        self.adj: dict[str, dict[str, float]] = defaultdict(dict)
        self.chunk_entities: dict[str, set[str]] = defaultdict(set)
        names: dict[str, Counter] = defaultdict(Counter)
        types: dict[str, Counter] = defaultdict(Counter)
        for r in db.query("SELECT entity_key,name,type,description,doc_id,chunk_id FROM entity_mentions"):
            if allowed_docs is not None and r["doc_id"] not in allowed_docs:
                continue
            key = r["entity_key"]
            ent = self.entities.setdefault(key, {"key": key, "name": r["name"], "type": "",
                                                 "descs": [], "docs": set(), "chunks": set(),
                                                 "sensitivity": "public"})
            names[key][r["name"]] += 1
            types[key][r["type"]] += 1
            if r["description"] and r["description"] not in ent["descs"] and len(ent["descs"]) < 6:
                ent["descs"].append(r["description"])
            ent["docs"].add(r["doc_id"])
            ent["chunks"].add(r["chunk_id"])
            if doc_sens.get(r["doc_id"], "internal") != "public":
                ent["sensitivity"] = "internal"
            self.chunk_entities[r["chunk_id"]].add(key)
        for key, ent in self.entities.items():
            ent["name"] = names[key].most_common(1)[0][0]
            ent["type"] = types[key].most_common(1)[0][0] or "概念"
            ent["description"] = " / ".join(ent.pop("descs"))[:600]
        for r in db.query("SELECT source,target,description,strength,doc_id,chunk_id "
                          "FROM relation_mentions"):
            if allowed_docs is not None and r["doc_id"] not in allowed_docs:
                continue
            a, b = r["source"], r["target"]
            if a not in self.entities or b not in self.entities or a == b:
                continue
            rel = self.relations.setdefault((a, b), {"source": a, "target": b, "descs": [],
                                                     "strength": 0, "chunks": set(), "count": 0})
            if r["description"] and r["description"] not in rel["descs"] and len(rel["descs"]) < 3:
                rel["descs"].append(r["description"])
            rel["strength"] = max(rel["strength"], int(r["strength"] or 5))
            rel["chunks"].add(r["chunk_id"])
            rel["count"] += 1
        for (a, b), rel in self.relations.items():
            rel["description"] = " / ".join(rel.pop("descs"))[:400]
            w = rel["strength"] * (1 + math.log(rel["count"]))
            self.adj[a][b] = w
            self.adj[b][a] = w
        self._ent_index: BM25Index | None = None

    # -------------------------------------------------- 種エンティティ
    def entity_index(self) -> BM25Index:
        if self._ent_index is None:
            idx = BM25Index()
            for key, e in self.entities.items():
                idx.add(key, f"{e['name']} {e['name']} {e['type']} {e['description'][:200]}")
            self._ent_index = idx
        return self._ent_index

    def seed_entities(self, question: str, keywords: list[str]) -> dict[str, float]:
        """質問・キーワードに対応するエンティティ（PPR の種）とその重み。"""
        seeds: dict[str, float] = defaultdict(float)
        q = norm_key(question)
        for key, e in self.entities.items():
            if len(key) >= 2 and key in q:
                seeds[key] += 2.0 + min(1.0, len(key) / 10)
        for kw in keywords:
            k = norm_key(kw)
            if len(k) < 2:
                continue
            if k in self.entities:
                seeds[k] += 1.5
                continue
            for key in self.entities:
                if len(key) >= 2 and (k in key or key in k):
                    seeds[key] += 0.6
        for key, score in self.entity_index().search(" ".join([question, *keywords]), k=6):
            seeds[key] += min(1.0, score / 8)
        return dict(sorted(seeds.items(), key=lambda t: -t[1])[:12])

    # -------------------------------------------------- Personalized PageRank
    def ppr(self, seeds: dict[str, float], *, damping: float = 0.8, iters: int = 30) -> dict[str, float]:
        if not seeds:
            return {}
        total = sum(seeds.values())
        p = {k: v / total for k, v in seeds.items()}
        rank = dict(p)
        out_w = {n: sum(nb.values()) for n, nb in self.adj.items()}
        for _ in range(iters):
            nxt: dict[str, float] = defaultdict(float)
            for k, v in p.items():
                nxt[k] += (1 - damping) * v
            dangling = 0.0
            for node, r in rank.items():
                nbrs = self.adj.get(node)
                if not nbrs:
                    dangling += r
                    continue
                share = damping * r / out_w[node]
                for nb, w in nbrs.items():
                    nxt[nb] += share * w
            for k, v in p.items():            # 行き止まりの質量は種へ戻す
                nxt[k] += damping * dangling * v
            delta = sum(abs(nxt.get(k, 0) - rank.get(k, 0)) for k in set(nxt) | set(rank))
            rank = dict(nxt)
            if delta < 1e-6:
                break
        return rank

    def rank_chunks(self, rank: dict[str, float], k: int = 20) -> list[tuple[str, float]]:
        """エンティティの PPR スコアをチャンクへ伝播（出現エンティティの和、頻出語は割引）。"""
        scores: dict[str, float] = defaultdict(float)
        for key, r in rank.items():
            ent = self.entities.get(key)
            if not ent or r <= 0:
                continue
            damp = 1.0 / math.log(2 + len(ent["chunks"]))
            for cid in ent["chunks"]:
                scores[cid] += r * damp
        return sorted(scores.items(), key=lambda t: (-t[1], t[0]))[:k]

    def context(self, rank: dict[str, float], *, n_ent: int = 10, n_rel: int = 16) -> dict:
        top = [k for k, _ in sorted(rank.items(), key=lambda t: -t[1])
               if k in self.entities][:n_ent]
        tops = set(top)
        rels = [r for (a, b), r in self.relations.items() if a in tops or b in tops]
        rels.sort(key=lambda r: -(r["strength"] * (2 if (r["source"] in tops and r["target"] in tops) else 1)))
        return {"entities": [self.entities[k] for k in top], "relations": rels[:n_rel]}

    # -------------------------------------------------- コミュニティ
    def communities(self) -> list[dict]:
        nodes = sorted(self.entities)
        labels = label_propagation(nodes, self.adj)
        groups: dict[int, list[str]] = defaultdict(list)
        for n in nodes:
            groups[labels[n]].append(n)
        out = []
        for i, members in enumerate(sorted(groups.values(), key=lambda m: (-len(m), m[0]))):
            sens = "internal" if any(self.entities[m]["sensitivity"] != "public" for m in members) \
                else "public"
            out.append({"id": f"C{i + 1}", "members": members, "size": len(members),
                        "sensitivity": sens,
                        "signature": hashlib.sha1("\n".join(members).encode("utf-8")).hexdigest()})
        return out

    def community_text(self, com: dict, *, limit: int = 30) -> tuple[str, str]:
        members = sorted(com["members"], key=lambda k: -len(self.adj.get(k, {})))[:limit]
        mset = set(com["members"])
        ent_lines = "\n".join(f"- {self.entities[k]['name']}（{self.entities[k]['type']}）: "
                              f"{self.entities[k]['description'][:200]}" for k in members)
        rel_lines = "\n".join(
            f"- {self.entities[r['source']]['name']} — {self.entities[r['target']]['name']}: "
            f"{r['description'][:150]}"
            for (a, b), r in self.relations.items() if a in mset and b in mset)[:3500]
        return ent_lines[:4000], rel_lines or "（なし）"

    def to_payload(self, *, limit: int = 300) -> dict:
        """GUI のグラフ表示用（graphview.js の形式）。"""
        coms = self.communities()
        com_of = {m: c["id"] for c in coms for m in c["members"]}
        deg = {k: len(self.adj.get(k, {})) for k in self.entities}
        keep = sorted(self.entities, key=lambda k: (-deg[k], k))[:limit]
        kset = set(keep)
        titles = {d["id"]: d["title"] for d in db.list_docs()}
        nodes = [{"id": k, "name": self.entities[k]["name"], "type": self.entities[k]["type"],
                  "degree": deg[k], "community": com_of.get(k, ""),
                  "description": self.entities[k]["description"][:300],
                  "docs": [titles.get(d, d) for d in sorted(self.entities[k]["docs"])][:8],
                  "doc_ids": sorted(self.entities[k]["docs"])[:8]}
                 for k in keep]
        edges = [{"source": a, "target": b, "strength": r["strength"],
                  "description": r["description"][:200]}
                 for (a, b), r in self.relations.items() if a in kset and b in kset]
        cached = {row["signature"]: row for row in db.query(
            "SELECT signature,title,summary FROM community_cache")}
        communities = []
        for c in coms:
            hit = cached.get(c["signature"])
            names = [self.entities[m]["name"] for m in c["members"][:3]]
            communities.append({"id": c["id"], "size": c["size"],
                                "title": hit["title"] if hit else "・".join(names)[:80],
                                "summary": hit["summary"] if hit else
                                ("メンバー: " + "、".join(self.entities[m]["name"] for m in c["members"][:10])
                                 if c["size"] >= 2 else "")})
        return {"nodes": nodes, "edges": edges, "communities": communities,
                "total_nodes": len(self.entities), "total_edges": len(self.relations),
                "truncated": len(self.entities) > limit}


def label_propagation(nodes: list[str], adj: dict[str, dict[str, float]],
                      *, max_iter: int = 20) -> dict[str, int]:
    """決定論的なラベル伝播法（ソート順に走査・タイは最小ラベル）。"""
    labels = {n: i for i, n in enumerate(nodes)}
    for _ in range(max_iter):
        changed = False
        for n in nodes:
            nbrs = adj.get(n)
            if not nbrs:
                continue
            weight: dict[int, float] = defaultdict(float)
            for m, w in nbrs.items():
                if m in labels:
                    weight[labels[m]] += w
            if not weight:
                continue
            best_w = max(weight.values())
            best = min(lbl for lbl, w in weight.items() if w == best_w)
            if best != labels[n]:
                labels[n] = best
                changed = True
        if not changed:
            break
    return labels


def summarize_community(llm, profile: str, graph: KnowledgeGraph, com: dict) -> dict:
    """コミュニティ要約（キャッシュ優先。無ければ LLM で作って保存）。"""
    row = db.one("SELECT title,summary FROM community_cache WHERE signature=?", (com["signature"],))
    if row:
        return {"title": row["title"], "summary": row["summary"]}
    ent_lines, rel_lines = graph.community_text(com)
    title = "・".join(graph.entities[m]["name"] for m in com["members"][:3])[:80]
    summary = ""
    if llm is not None and profile:
        text = llm.chat(profile, COMMUNITY_PROMPT.format(entities=ent_lines, relations=rel_lines),
                        sensitivity=com["sensitivity"], system=EXTRACT_SYSTEM,
                        max_tokens=2048, purpose="ingest")
        data = extract_json(text)
        if isinstance(data, dict):
            title = str(data.get("title") or title).strip()[:80]
            summary = str(data.get("summary") or "").strip()[:1500]
    if not summary:
        return {"title": title, "summary": ent_lines[:800]}
    db.write("INSERT OR REPLACE INTO community_cache(signature,title,summary,sensitivity,created_at) "
             "VALUES(?,?,?,?,?)", (com["signature"], title, summary, com["sensitivity"], db.now()))
    return {"title": title, "summary": summary}


# ---------------------------------------------------------------- キャッシュ

_cache_lock = threading.Lock()
_cache: dict[tuple, KnowledgeGraph] = {}


def get_graph(allowed_docs: set[str] | None, doc_sens: dict[str, str]) -> KnowledgeGraph:
    """リビジョン × 許可文書集合 ごとにグラフをキャッシュする。"""
    key = (db.revision(), str(db.DATA_DIR),
           hashlib.sha1("\n".join(sorted(allowed_docs)).encode()).hexdigest()
           if allowed_docs is not None else "*",
           hashlib.sha1(repr(sorted(doc_sens.items())).encode()).hexdigest())
    with _cache_lock:
        g = _cache.get(key)
        if g is None:
            if len(_cache) > 8:
                _cache.clear()
            g = KnowledgeGraph(allowed_docs, doc_sens)
            _cache[key] = g
        return g
