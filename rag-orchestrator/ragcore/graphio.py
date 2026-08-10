"""外部エンジン（LightRAG / nano-graphrag）のグラフ読み込み。

どちらも既定のグラフストレージ（NetworkX）が作業ディレクトリへ
GraphML（graph_chunk_entity_relation.graphml）を保存する。これを標準ライブラリの
ElementTree で解析し、GUI のグラフ表示（組み込み GraphRAG と同じ形式）へ変換する。
コミュニティは両ライブラリとも持たないため、ラベル伝播法で自動グループを作って
色分けに使う。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

GRAPHML_FILENAME = "graph_chunk_entity_relation.graphml"


def _local(tag: str) -> str:
    """名前空間つきタグ名からローカル名を取り出す。"""
    return tag.rsplit("}", 1)[-1]


def _clean(value: str | None) -> str:
    """LightRAG 旧版がエンティティ名を "NAME" と引用符で包む場合があるため剥がす。"""
    return (value or "").strip().strip('"').strip()


def load_graphml(path: Path) -> tuple[list[dict], list[dict]]:
    """GraphML を (nodes, edges) に解析する。失敗時は OSError / ParseError。"""
    tree = ET.parse(path)
    # <key id="d0" attr.name="entity_type"/> の対応表（data 要素の key を属性名へ）
    keys: dict[str, str] = {}
    for el in tree.iter():
        if _local(el.tag) == "key":
            keys[el.get("id") or ""] = el.get("attr.name") or el.get("id") or ""

    nodes: list[dict] = []
    edges: list[dict] = []
    for el in tree.iter():
        kind = _local(el.tag)
        if kind not in ("node", "edge"):
            continue
        attrs: dict[str, str] = {}
        for data in el:
            if _local(data.tag) == "data":
                attrs[keys.get(data.get("key") or "", data.get("key") or "")] = data.text or ""
        if kind == "node":
            node_id = el.get("id") or ""
            if not node_id:
                continue
            nodes.append({
                "id": node_id,
                "name": _clean(attrs.get("entity_id")) or _clean(node_id),
                "type": _clean(attrs.get("entity_type")) or "その他",
                "description": _clean(attrs.get("description"))[:300],
            })
        else:
            src, tgt = el.get("source") or "", el.get("target") or ""
            if not src or not tgt:
                continue
            try:
                weight = float(_clean(attrs.get("weight")) or 5.0)
            except ValueError:
                weight = 5.0
            edges.append({
                "source": src,
                "target": tgt,
                "strength": max(1, min(10, int(round(weight)))),
                "description": _clean(attrs.get("description"))[:200],
            })
    return nodes, edges


def graphml_graph_payload(path: Path, *, max_nodes: int = 300) -> dict:
    """GraphML から GUI グラフ表示用ペイロードを作る（/api/graph と同じ形式）。"""
    from .engines.graphrag import label_propagation

    if not path.exists():
        return {"error": "グラフファイルが見つかりません。"
                         "先にこのエンジンのインデックス構築を実行してください"
                         f"（期待するファイル: {path}）"}
    try:
        nodes, edges = load_graphml(path)
    except (OSError, ET.ParseError) as e:
        return {"error": f"GraphML の読み込みに失敗しました: {e}"}
    if not nodes:
        return {"error": "グラフにノードがありません（インデックス構築が失敗していないか"
                         "エンジンのエラー表示を確認してください）"}

    # 次数と無向エッジ重み
    degree: dict[str, int] = {n["id"]: 0 for n in nodes}
    edge_weights: dict[tuple[str, str], float] = {}
    for e in edges:
        if e["source"] not in degree or e["target"] not in degree \
                or e["source"] == e["target"]:
            continue
        degree[e["source"]] += 1
        degree[e["target"]] += 1
        pair = (min(e["source"], e["target"]), max(e["source"], e["target"]))
        edge_weights[pair] = edge_weights.get(pair, 0.0) + e["strength"]

    # 色分け用の自動グループ（LightRAG 等はコミュニティ要約を持たない）
    labels = label_propagation(sorted(degree), edge_weights)
    groups: dict[int, list[str]] = defaultdict(list)
    for node_id in sorted(degree):
        groups[labels.get(node_id, -1)].append(node_id)
    name_of = {n["id"]: n["name"] for n in nodes}
    com_of: dict[str, str] = {}
    communities = []
    for i, members in enumerate(sorted(groups.values(), key=lambda m: (-len(m), m[0]))):
        cid = f"C{i + 1}"
        for node_id in members:
            com_of[node_id] = cid
        member_names = [name_of[m] for m in members]
        communities.append({
            "id": cid,
            "size": len(members),
            "title": "・".join(member_names[:3])[:80],
            # 要約は無いのでメンバー一覧を出す（サイズ1は一覧に出さない）
            "summary": ("メンバー: " + "、".join(member_names[:8])
                        + ("…" if len(members) > 8 else "")) if len(members) >= 2 else "",
        })

    kept = sorted(nodes, key=lambda n: (-degree[n["id"]], n["id"]))[:max_nodes]
    kept_ids = {n["id"] for n in kept}
    out_nodes = [{"id": n["id"], "name": n["name"], "type": n["type"],
                  "degree": degree[n["id"]], "community": com_of.get(n["id"], ""),
                  "description": n["description"]} for n in kept]
    out_edges = [e for e in edges
                 if e["source"] in kept_ids and e["target"] in kept_ids]
    return {"nodes": out_nodes, "edges": out_edges, "communities": communities,
            "truncated": len(nodes) > max_nodes}
