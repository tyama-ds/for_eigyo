"""組み込み GraphRAG エンジン。

Microsoft Research の GraphRAG（note 記事「GraphRAGってどうなの？2026年最新の研究から」
https://note.com/niti_technology/n/nfa976ab900a8 で紹介されている方式）を、
標準ライブラリのみ・ローカルLLM（OpenAI互換）前提で再実装したもの。

パイプライン（ingest）:
  1. チャンク分割
  2. LLM によるエンティティ / 関係抽出（チャンクごと）
  3. 同名エンティティのマージ → ナレッジグラフ構築
  4. コミュニティ検出（ラベル伝播法。本家の Leiden 法の軽量代替）
  5. コミュニティごとの LLM 要約（コミュニティレポート）
  6. エンティティ・チャンクの埋め込み（設定時のみ。local 検索の精度向上用）

検索（query）:
  - global: コミュニティ要約への map-reduce（コーパス全体に関する質問向け）
  - local : 質問に関連するエンティティ近傍（関係・要約・原文チャンク）から回答
  - auto  : 質問中に既知エンティティがあれば local、なければ global
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict

from ..llm import LLMError
from ..textutil import BM25, extract_json, top_k_cosine
from .base import ANSWER_SYSTEM, Engine, EngineContext, build_chunks

MAX_SUMMARIZED_COMMUNITIES = 25   # LLM 要約を作るコミュニティ数の上限（大きい順）
MAX_MAPPED_COMMUNITIES = 20       # global 検索で map するコミュニティ数の上限
DESC_MERGE_LIMIT = 600            # マージ後のエンティティ説明文の最大長

EXTRACT_SYSTEM = (
    "あなたはナレッジグラフ構築のための情報抽出器です。"
    "指示された JSON だけを出力し、他の文章は書かないでください。"
)

EXTRACT_PROMPT = """[TASK:graph_extract]
以下のテキストから、重要なエンティティ（人物・組織・場所・製品・技術・概念・イベント等）と、
エンティティ間の関係を抽出してください。

出力は次の JSON のみ:
{{"entities": [{{"name": "エンティティ名", "type": "種別", "description": "本文に基づく1〜2文の説明"}}],
 "relations": [{{"source": "エンティティ名", "target": "エンティティ名", "description": "関係の説明", "strength": 5}}]}}

- name は本文中の表記をそのまま使う
- relations の source / target は entities の name のいずれかと一致させる
- strength は関係の強さ（1〜10 の整数）

# テキスト
{text}
"""

COMMUNITY_PROMPT = """[TASK:community_summary]
以下はナレッジグラフから検出された、互いに関連の強いエンティティ群（コミュニティ）です。
このコミュニティが何についてのまとまりか、タイトルと要約を作ってください。

出力は次の JSON のみ:
{{"title": "コミュニティの短いタイトル", "summary": "エンティティと関係を踏まえた5文以内の要約"}}

# エンティティ
{entities}

# 関係
{relations}
"""

GLOBAL_MAP_PROMPT = """[TASK:global_map]
質問に答える材料として、以下のコミュニティ要約がどの程度役立つかを判定し、
役立つ場合は要約から言えるポイントを抽出してください。

出力は次の JSON のみ:
{{"points": [{{"text": "質問に関係するポイント（1文）", "score": 7}}]}}

- score はそのポイントの重要度（0〜10 の整数）。関係が無ければ points は空配列にする

# コミュニティ要約（{cid}）
{summary}

# 質問
{question}
"""

GLOBAL_REDUCE_PROMPT = """[TASK:global_reduce]
以下は、コーパス全体をコミュニティ単位で要約した資料から抽出された、質問に関係するポイントの一覧です。
これらを統合して質問に回答してください。根拠にしたポイントは文末に [C3] のようにコミュニティIDで示してください。

# ポイント一覧
{points}

# 質問
{question}

# 回答
"""

LOCAL_ANSWER_PROMPT = """[TASK:local_answer]
以下のナレッジグラフ情報（エンティティ・関係・コミュニティ要約）と原文抜粋を根拠に、質問へ回答してください。
根拠にした原文は文末に [S1] のように番号で示してください。

# エンティティ
{entities}

# 関係
{relations}

# コミュニティ要約
{communities}

# 原文抜粋
{chunks}

# 質問
{question}

# 回答
"""


def _norm_key(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def label_propagation(nodes: list[str], edges: dict[tuple[str, str], float],
                      *, max_iter: int = 20) -> dict[str, int]:
    """決定論的なラベル伝播法によるコミュニティ検出。

    本家 GraphRAG は Leiden 法（graspologic 依存）を使うが、依存なしで同趣旨の
    「密に繋がったノード群」を検出する。ノードは毎回ソート順に走査し、
    タイは最小ラベルを選ぶため結果は決定論的。
    """
    node_set = set(nodes)
    adj: dict[str, dict[str, float]] = defaultdict(dict)
    for (a, b), w in edges.items():
        if a == b or a not in node_set or b not in node_set:
            continue
        adj[a][b] = adj[a].get(b, 0.0) + w
        adj[b][a] = adj[b].get(a, 0.0) + w

    ordered = sorted(nodes)
    labels = {n: i for i, n in enumerate(ordered)}
    for _ in range(max_iter):
        changed = False
        for n in ordered:
            if not adj[n]:
                continue
            weight_by_label: dict[int, float] = defaultdict(float)
            for m, w in adj[n].items():
                weight_by_label[labels[m]] += w
            best = min(lbl for lbl, w in weight_by_label.items()
                       if w == max(weight_by_label.values()))
            if best != labels[n]:
                labels[n] = best
                changed = True
        if not changed:
            break
    return labels


class GraphRAGEngine(Engine):
    id = "graphrag"
    name = "GraphRAG（組み込み）"
    kind = "builtin"
    description = (
        "Microsoft GraphRAG 方式: エンティティ抽出→ナレッジグラフ→コミュニティ検出→"
        "コミュニティ要約。global（全体質問）/ local（個別質問）の2モード検索"
    )
    requires_chat = True
    requires_embed = False        # 埋め込みは任意（local 検索の精度が上がる）

    # ------------------------------------------------------------ ingest
    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        chunks = build_chunks(corpus)
        if not chunks:
            raise ValueError("コーパスが空です")

        # 1-2. チャンクごとにエンティティ / 関係を抽出
        raw_entities: list[dict] = []
        raw_relations: list[dict] = []
        parse_failures = 0
        for i, chunk in enumerate(chunks):
            ctx.progress(0.05 + 0.55 * i / len(chunks),
                         f"エンティティ抽出 {i + 1}/{len(chunks)}")
            text = ctx.llm.chat(EXTRACT_PROMPT.format(text=chunk["text"]),
                                system=EXTRACT_SYSTEM, temperature=0.0)
            data = extract_json(text)
            if not isinstance(data, dict):
                parse_failures += 1
                ctx.log(f"抽出JSONの解析に失敗（chunk {chunk['id']}）")
                continue
            for ent in data.get("entities") or []:
                if isinstance(ent, dict) and str(ent.get("name", "")).strip():
                    raw_entities.append({
                        "name": str(ent["name"]).strip(),
                        "type": str(ent.get("type", "")).strip() or "その他",
                        "description": str(ent.get("description", "")).strip(),
                        "chunk": chunk["id"],
                    })
            for rel in data.get("relations") or []:
                if not isinstance(rel, dict):
                    continue
                src, tgt = str(rel.get("source", "")).strip(), str(rel.get("target", "")).strip()
                if not src or not tgt or _norm_key(src) == _norm_key(tgt):
                    continue
                try:
                    strength = max(1, min(10, int(rel.get("strength", 5))))
                except (TypeError, ValueError):
                    strength = 5
                raw_relations.append({
                    "source": src, "target": tgt,
                    "description": str(rel.get("description", "")).strip(),
                    "strength": strength, "chunk": chunk["id"],
                })

        # 3. エンティティのマージとグラフ構築
        ctx.progress(0.62, "グラフ構築")
        entities = self._merge_entities(raw_entities)
        entity_keys = set(entities)
        relations, edge_weights = self._merge_relations(raw_relations, entity_keys)
        for rel in relations:
            entities[rel["source"]]["degree"] += 1
            entities[rel["target"]]["degree"] += 1

        # 4. コミュニティ検出
        ctx.progress(0.66, "コミュニティ検出")
        labels = label_propagation(sorted(entity_keys), edge_weights)
        groups: dict[int, list[str]] = defaultdict(list)
        for key in sorted(entity_keys):
            groups[labels.get(key, -1)].append(key)
        communities = []
        for i, members in enumerate(sorted(groups.values(), key=lambda m: (-len(m), m[0]))):
            communities.append({"id": f"C{i + 1}", "entity_keys": members,
                                "size": len(members), "title": "", "summary": ""})

        # 5. コミュニティ要約（サイズ2以上、大きい順に上限まで）
        targets = [c for c in communities if c["size"] >= 2][:MAX_SUMMARIZED_COMMUNITIES]
        for j, com in enumerate(targets):
            ctx.progress(0.68 + 0.2 * j / max(1, len(targets)),
                         f"コミュニティ要約 {j + 1}/{len(targets)}")
            ent_lines = "\n".join(
                f"- {entities[k]['name']}（{entities[k]['type']}）: {entities[k]['description']}"
                for k in com["entity_keys"][:30])
            member_set = set(com["entity_keys"])
            rel_lines = "\n".join(
                f"- {entities[r['source']]['name']} → {entities[r['target']]['name']}: "
                f"{r['description']}（強さ{r['strength']}）"
                for r in relations
                if r["source"] in member_set and r["target"] in member_set)[:4000]
            text = ctx.llm.chat(
                COMMUNITY_PROMPT.format(entities=ent_lines[:4000],
                                        relations=rel_lines or "（なし）"),
                system=EXTRACT_SYSTEM, temperature=0.0)
            data = extract_json(text)
            if isinstance(data, dict):
                com["title"] = str(data.get("title", "")).strip()[:80]
                com["summary"] = str(data.get("summary", "")).strip()[:2000]
            if not com["title"]:
                com["title"] = "・".join(entities[k]["name"] for k in com["entity_keys"][:3])
            if not com["summary"]:
                com["summary"] = " / ".join(
                    entities[k]["description"] for k in com["entity_keys"][:5])[:1000]

        # 6. 埋め込み（任意）
        entity_list = [entities[k] for k in sorted(entity_keys)]
        entity_vecs = chunk_vecs = None
        if ctx.embed_ok:
            try:
                ctx.progress(0.9, "埋め込み計算")
                entity_vecs = ctx.llm.embed(
                    [f"{e['name']}: {e['description']}"[:1000] for e in entity_list]
                ) if entity_list else []
                chunk_vecs = ctx.llm.embed([c["text"][:2000] for c in chunks])
            except LLMError as e:
                ctx.log(f"埋め込みをスキップしました: {e}")
                entity_vecs = chunk_vecs = None

        ctx.progress(1.0, "完了")
        return {
            "engine": self.id,
            "built_at": time.time(),
            "chunks": chunks,
            "entities": entity_list,
            "relations": relations,
            "communities": communities,
            "entity_vecs": entity_vecs,
            "chunk_vecs": chunk_vecs,
            "stats": {
                "chunks": len(chunks),
                "entities": len(entity_list),
                "relations": len(relations),
                "communities": len(communities),
                "summarized_communities": len(targets),
                "extract_parse_failures": parse_failures,
                "has_embeddings": entity_vecs is not None,
            },
        }

    @staticmethod
    def _merge_entities(raw: list[dict]) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        names: dict[str, Counter] = defaultdict(Counter)
        types: dict[str, Counter] = defaultdict(Counter)
        for ent in raw:
            key = _norm_key(ent["name"])
            names[key][ent["name"]] += 1
            types[key][ent["type"]] += 1
            cur = merged.setdefault(key, {"key": key, "name": ent["name"], "type": "",
                                          "descriptions": [], "chunks": [], "degree": 0})
            if ent["description"] and ent["description"] not in cur["descriptions"]:
                cur["descriptions"].append(ent["description"])
            if ent["chunk"] not in cur["chunks"]:
                cur["chunks"].append(ent["chunk"])
        for key, ent in merged.items():
            ent["name"] = names[key].most_common(1)[0][0]
            ent["type"] = types[key].most_common(1)[0][0]
            ent["description"] = " / ".join(ent["descriptions"][:4])[:DESC_MERGE_LIMIT]
            del ent["descriptions"]
        return merged

    @staticmethod
    def _merge_relations(raw: list[dict], entity_keys: set[str],
                         ) -> tuple[list[dict], dict[tuple[str, str], float]]:
        by_pair: dict[tuple[str, str], dict] = {}
        for rel in raw:
            src, tgt = _norm_key(rel["source"]), _norm_key(rel["target"])
            if src not in entity_keys or tgt not in entity_keys:
                continue
            pair = (min(src, tgt), max(src, tgt))
            cur = by_pair.setdefault(pair, {"source": src, "target": tgt,
                                            "descriptions": [], "strength": 0, "chunks": []})
            if rel["description"] and rel["description"] not in cur["descriptions"]:
                cur["descriptions"].append(rel["description"])
            cur["strength"] = max(cur["strength"], rel["strength"])
            if rel["chunk"] not in cur["chunks"]:
                cur["chunks"].append(rel["chunk"])
        relations = []
        edge_weights: dict[tuple[str, str], float] = {}
        for pair, rel in sorted(by_pair.items()):
            rel["description"] = " / ".join(rel["descriptions"][:3])[:400]
            del rel["descriptions"]
            relations.append(rel)
            edge_weights[pair] = float(rel["strength"])
        return relations, edge_weights

    # ------------------------------------------------------------ query
    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        if mode not in ("auto", "global", "local"):
            mode = "auto"
        if mode == "auto":
            mode = "local" if self._mentions_entity(index, question) else "global"
        if mode == "global":
            return self._query_global(index, question, ctx)
        return self._query_local(index, question, ctx)

    @staticmethod
    def _mentions_entity(index: dict, question: str) -> bool:
        q = question.casefold()
        return any(len(e["name"]) >= 2 and e["name"].casefold() in q
                   for e in index["entities"])

    def _query_global(self, index: dict, question: str, ctx: EngineContext) -> dict:
        communities = [c for c in index["communities"] if c["summary"]]
        communities = communities[:MAX_MAPPED_COMMUNITIES]
        points: list[dict] = []
        for i, com in enumerate(communities):
            ctx.progress(0.1 + 0.6 * i / max(1, len(communities)),
                         f"global map {i + 1}/{len(communities)}")
            text = ctx.llm.chat(
                GLOBAL_MAP_PROMPT.format(cid=com["id"], question=question,
                                         summary=f"{com['title']}\n{com['summary']}"),
                system=EXTRACT_SYSTEM, temperature=0.0)
            data = extract_json(text)
            for p in (data.get("points") if isinstance(data, dict) else None) or []:
                if not isinstance(p, dict) or not str(p.get("text", "")).strip():
                    continue
                try:
                    score = max(0, min(10, int(p.get("score", 0))))
                except (TypeError, ValueError):
                    score = 0
                if score >= 2:
                    points.append({"text": str(p["text"]).strip()[:500],
                                   "score": score, "community": com["id"]})
        points.sort(key=lambda p: (-p["score"], p["community"]))
        points = points[:12]
        used = {p["community"] for p in points}

        ctx.progress(0.8, "global reduce")
        think = ""
        if points:
            point_lines = "\n".join(
                f"- [{p['community']}] （重要度{p['score']}）{p['text']}" for p in points)
            answer, think = ctx.llm.chat(
                GLOBAL_REDUCE_PROMPT.format(points=point_lines, question=question),
                system=ANSWER_SYSTEM, temperature=0.0, want_think=True)
        else:
            answer = ("コミュニティ要約からは質問に関係する情報が見つかりませんでした。"
                      "個別の事柄への質問であれば local モードを試してください。")
        citations = [
            {"type": "community", "ref": c["id"], "title": c["title"],
             "snippet": c["summary"][:200]}
            for c in communities if c["id"] in used
        ]
        return {"answer": answer, "think": think, "mode": "global", "citations": citations,
                "stats": {"mapped_communities": len(communities), "points": len(points)}}

    def _query_local(self, index: dict, question: str, ctx: EngineContext) -> dict:
        entities = index["entities"]
        relations = index["relations"]
        by_key = {e["key"]: e for e in entities}

        # 関連エンティティの選定（埋め込みがあれば意味検索、無ければ BM25）
        ctx.progress(0.1, "エンティティ検索")
        top_entities: list[dict] = []
        if index.get("entity_vecs"):
            try:
                qvec = ctx.llm.embed([question])[0]
                for i, score in top_k_cosine(qvec, index["entity_vecs"], k=8):
                    if score > 0:
                        top_entities.append(entities[i])
            except LLMError as e:
                ctx.log(f"質問の埋め込みに失敗、字句検索へフォールバック: {e}")
        if not top_entities and entities:
            bm25 = BM25([_entity_tokens(e) for e in entities])
            top_entities = [entities[i] for i, _ in bm25.top_k(question, k=8)]
        if not top_entities:
            top_entities = sorted(entities, key=lambda e: -e["degree"])[:5]

        top_keys = {e["key"] for e in top_entities}

        # 近傍情報の収集
        ctx.progress(0.35, "近傍情報の収集")
        rel_hits = [r for r in relations
                    if r["source"] in top_keys or r["target"] in top_keys]
        rel_hits.sort(key=lambda r: -r["strength"])
        rel_hits = rel_hits[:20]

        com_hits = [c for c in index["communities"]
                    if c["summary"] and top_keys & set(c["entity_keys"])][:3]

        chunks = index["chunks"]
        chunk_by_id = {c["id"]: c for c in chunks}
        candidate_ids: list[str] = []
        for ent in top_entities:
            candidate_ids.extend(ent.get("chunks") or [])
        seen: set[str] = set()
        candidates = [chunk_by_id[cid] for cid in candidate_ids
                      if cid in chunk_by_id and not (cid in seen or seen.add(cid))]
        if index.get("chunk_vecs"):
            try:
                qvec = ctx.llm.embed([question])[0]
                ranked = top_k_cosine(qvec, index["chunk_vecs"], k=5)
                candidates = [chunks[i] for i, s in ranked if s > 0] or candidates
            except LLMError:
                pass
        elif candidates:
            bm25 = BM25([_chunk_tokens(c) for c in candidates])
            hits = bm25.top_k(question, k=5)
            if hits:
                candidates = [candidates[i] for i, _ in hits]
        top_chunks = candidates[:5]

        # 回答生成
        ctx.progress(0.7, "回答生成")
        ent_lines = "\n".join(f"- {e['name']}（{e['type']}）: {e['description']}"
                              for e in top_entities)
        rel_lines = "\n".join(
            f"- {by_key[r['source']]['name']} → {by_key[r['target']]['name']}: "
            f"{r['description']}"
            for r in rel_hits) or "（なし）"
        com_lines = "\n".join(f"- [{c['id']}] {c['title']}: {c['summary'][:400]}"
                              for c in com_hits) or "（なし）"
        chunk_lines = "\n\n".join(
            f"[S{i + 1}] （{c['doc_title']}）\n{c['text'][:1500]}"
            for i, c in enumerate(top_chunks)) or "（なし）"
        answer, think = ctx.llm.chat(
            LOCAL_ANSWER_PROMPT.format(entities=ent_lines[:4000], relations=rel_lines[:3000],
                                       communities=com_lines[:2500],
                                       chunks=chunk_lines[:8000], question=question),
            system=ANSWER_SYSTEM, temperature=0.0, want_think=True)

        citations = (
            [{"type": "entity", "ref": e["key"], "title": e["name"],
              "snippet": e["description"][:200]} for e in top_entities[:5]]
            + [{"type": "community", "ref": c["id"], "title": c["title"],
                "snippet": c["summary"][:200]} for c in com_hits]
            + [{"type": "chunk", "ref": c["id"], "title": c["doc_title"],
                "snippet": c["text"][:200]} for c in top_chunks]
        )
        return {"answer": answer, "think": think, "mode": "local", "citations": citations,
                "stats": {"entities": len(top_entities), "relations": len(rel_hits),
                          "chunks": len(top_chunks)}}


def _entity_tokens(entity: dict) -> list[str]:
    from ..textutil import tokenize
    return tokenize(f"{entity['name']} {entity['type']} {entity['description']}")


def _chunk_tokens(chunk: dict) -> list[str]:
    from ..textutil import tokenize
    return tokenize(chunk["text"])
