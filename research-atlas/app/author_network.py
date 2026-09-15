"""Conservative author identity and deterministic coauthorship grouping.

All identity and publication counts use the full corpus. Community detection and
edge export use the explicitly bounded display-author induced graph, before the
edge drawing cap. Layout positions express groups, not scientific distance.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import math
import re
import unicodedata

import numpy as np

from .bibliography import normalize_affiliations


AUTHOR_LIMIT = 120
EDGE_LIMIT = 240
GROUPS = {"id", "name", "institution", "community", "topic"}
COLORS = ["#42e8cf", "#9d8cff", "#5fa8ff", "#ffbc6b", "#ff7ea8", "#b5e875",
          "#58d7ff", "#dc93f5", "#f1dd72", "#90b9a5"]


def _text(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _name_key(value):
    value = _text(value).casefold()
    parts = value.split(",")
    if len(parts) == 2 and all(part.strip() for part in parts):
        value = parts[1] + " " + parts[0]
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)).strip()


def _identifier(value):
    value = _text(value)
    value = re.sub(r"^https?://(?:www\.)?orcid\.org/", "orcid:", value, flags=re.I)
    return re.sub(r"^(orcid|scopus|name):", lambda match: match[1].lower() + ":", value, flags=re.I)


def _weak(identifier):
    return identifier.startswith("name:")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _year(value):
    try:
        numeric = int(value)
        return numeric if not isinstance(value, bool) and float(value) == numeric and 1800 <= numeric <= 2200 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _citations(value):
    try:
        numeric = float(value)
        return int(numeric) if not isinstance(value, bool) and math.isfinite(numeric) and 0 <= numeric <= 2**53 - 1 and numeric.is_integer() else None
    except (ValueError, TypeError, OverflowError):
        return None


def _institution(value):
    """Strip explicit department segments, preserving campus/location text."""
    text = _text(value)
    parts = [part.strip() for part in text.split(",")]
    if len(parts) > 1:
        anchors = [index for index, part in enumerate(parts)
                   if re.search(r"\b(?:university|universit[eéä]t?|universidad|institute|institution|college|hospital)\b|大学", part, re.I)]
        if len(anchors) == 1:
            def department(part):
                return bool(re.search(r"^(?:the\s+)?(?:graduate\s+)?(?:department|faculty|school|division|laboratory|lab|center|centre|unit)\b", part, re.I)
                            or re.search(r"\b(?:laboratory|lab|department|division|faculty|school)$", part, re.I)
                            or re.search(r"(?:学部|研究科|学科|研究室)$", part))
            kept = [part for index, part in enumerate(parts) if index == anchors[0] or not department(part)]
            if kept != parts:
                return ", ".join(kept), "department_segment_heuristic_with_single_institution_anchor"
    # Do not cut inside institution names such as 総合研究大学院大学.
    match = re.match(r"^(.+大学)\s*(?:大学院.+|[^,，]{1,12}学部.*|[^,，]{1,12}研究科.*)$", text)
    if match:
        return match[1].strip(), "explicit_japanese_department_suffix_removed"
    return text, "normalized_exact_string"


def _author_records(papers):
    parent, strong, records = {}, {}, []
    owners = defaultdict(set)
    missing_names = 0

    def find(identifier):
        if identifier not in parent:
            parent[identifier] = identifier
            strong[identifier] = set() if _weak(identifier) else {identifier}
        while parent[identifier] != identifier:
            parent[identifier] = parent[parent[identifier]]
            identifier = parent[identifier]
        return identifier

    def union(first, second):
        first, second = find(first), find(second)
        if first != second:
            first, second = sorted((first, second))
            parent[second] = first
            strong[first] |= strong.pop(second)

    for paper in papers:
        for raw in paper.get("authors") or []:
            raw = {"name": raw} if isinstance(raw, str) else raw
            if not isinstance(raw, dict):
                continue
            original = _text(raw.get("id"))
            supplied = _identifier(original)
            name = _text(raw.get("name"))
            key = _name_key(name)
            if not supplied and not key:
                missing_names += 1
                continue
            primary = supplied if supplied and not _weak(supplied) else "name:" + _digest(key) if key else supplied
            raw_aliases = raw.get("aliases") if isinstance(raw.get("aliases"), (list, tuple)) else []
            aliases = {_identifier(item) for item in raw_aliases if isinstance(item, str) and _text(item)}
            if supplied:
                aliases.add(supplied)
            aliases.add(primary)
            find(primary)
            for alias in aliases:
                owners[alias].add(primary)
            records.append({"paper": paper, "primary": primary, "original": original, "supplied": supplied,
                            "name": name, "aliases": aliases, "raw_aliases": [item for item in raw_aliases if isinstance(item, str)],
                            "affiliations": normalize_affiliations(raw.get("affiliations", [])),
                            "name_estimated": not supplied or _weak(supplied)})
    # Explicit non-name aliases are links supplied by the data source.
    for alias in sorted(owners):
        if not _weak(alias):
            for owner in sorted(owners[alias]):
                union(alias, owner)
    ambiguous_aliases = 0
    for alias in sorted(owners):
        if not _weak(alias):
            continue
        roots = {find(owner) for owner in owners[alias]}
        known_roots = {root for root in roots if strong[root]}
        # Include roots reached through earlier name aliases to stop transitive
        # name-only bridges between two different explicit identities.
        if len(known_roots) > 1:
            ambiguous_aliases += 1
            continue
        for owner in sorted(owners[alias]):
            union(alias, owner)
    groups = defaultdict(list)
    for record in records:
        groups[find(record["primary"])].append(record)
    nodes = []
    paper_authors = defaultdict(set)
    affiliation_associations = 0
    for entries in groups.values():
        candidates = set().union(*(entry["aliases"] for entry in entries))
        # Ambiguous, unjoined aliases must not become the identity's canonical ID.
        candidates = {item for item in candidates if find(item) == find(entries[0]["primary"])}
        rank = lambda value: (0 if value.startswith("orcid:") else 1 if value.startswith("scopus:") else 3 if _weak(value) else 2, value)
        identifier = min(candidates, key=rank)
        member_papers = {str(entry["paper"]["id"]): entry["paper"] for entry in entries}
        name_papers, affiliation_papers = defaultdict(set), defaultdict(set)
        affiliation_originals, affiliation_names, affiliation_basis = defaultdict(set), defaultdict(set), defaultdict(set)
        known_affiliation_papers = set()
        for entry in entries:
            paper_id = str(entry["paper"]["id"])
            paper_authors[paper_id].add(identifier)
            if entry["name"]:
                name_papers[entry["name"]].add(paper_id)
            if entry["affiliations"]:
                known_affiliation_papers.add(paper_id)
            for original in entry["affiliations"]:
                display, basis = _institution(original)
                key = display.casefold()
                affiliation_papers[key].add(paper_id)
                affiliation_originals[key].add(original)
                affiliation_names[key].add(display)
                affiliation_basis[key].add(basis)
        affiliation_associations += len(known_affiliation_papers)
        names = sorted(name_papers, key=lambda value: (-len(name_papers[value]), -len(value), value))
        affiliations = [{"name": sorted(affiliation_names[key])[0], "normalized_name": key,
                         "paper_count": len(affiliation_papers[key]), "paper_ids": sorted(affiliation_papers[key]),
                         "original_names": sorted(affiliation_originals[key]), "normalization_basis": sorted(affiliation_basis[key])}
                        for key in sorted(affiliation_papers, key=lambda key: (-len(affiliation_papers[key]), key))]
        original_ids = sorted({entry["original"] for entry in entries if entry["original"]}
                              | {alias for entry in entries for alias in entry["raw_aliases"] if _text(alias)})
        if _weak(identifier):
            basis = "name_estimate"
        elif any(entry["name_estimated"] for entry in entries):
            basis = "explicit_alias_with_name_estimate"
        elif len({entry["supplied"] for entry in entries}) > 1:
            basis = "explicit_alias"
        else:
            basis = "orcid" if identifier.startswith("orcid:") else "scopus_id" if identifier.startswith("scopus:") else "provided_id"
        known_citations = [_citations(paper.get("citations")) for paper in member_papers.values()]
        topic_counts = Counter(str(paper.get("topic_id") or "unknown") for paper in member_papers.values())
        primary_topic = min(topic_counts, key=lambda value: (-topic_counts[value], value))
        nodes.append({"id": identifier, "label": names[0] if names else identifier,
                      "names": names, "original_ids": original_ids, "identity_basis": basis,
                      "count": len(member_papers), "citations": sum(value for value in known_citations if value is not None),
                      "citation_known_papers": sum(value is not None for value in known_citations),
                      "paper_ids": sorted(member_papers), "years": sorted({_year(paper.get("year")) for paper in member_papers.values()} - {None}),
                      "topic_id": primary_topic, "topic_counts": dict(sorted(topic_counts.items())),
                      "affiliations": affiliations, "affiliation_known_papers": len(known_affiliation_papers),
                      "original_affiliations": sorted(set().union(*affiliation_originals.values())) if affiliation_originals else [],
                      "cluster_id": None, "cluster_label": None, "x": None, "y": None})
    nodes.sort(key=lambda node: (-node["count"], node["id"]))
    names_to_ids = defaultdict(set)
    for node in nodes:
        for name in node["names"]:
            names_to_ids[_name_key(name)].add(node["id"])
    ambiguous_names = sum(len(identities) > 1 for identities in names_to_ids.values())
    return nodes, paper_authors, {"ambiguous_name_groups": ambiguous_names, "ambiguous_aliases": ambiguous_aliases,
                                  "author_records_without_id_or_name": missing_names,
                                  "author_paper_associations_with_affiliations": affiliation_associations}


def _edges(nodes, papers):
    """At most 7,140 intersections; avoid expanding huge author lists per paper."""
    paper_ids = [str(paper["id"]) for paper in papers]
    lookup = {identifier: index for index, identifier in enumerate(paper_ids)}
    masks = []
    for node in nodes:
        mask = 0
        for identifier in node["paper_ids"]:
            mask |= 1 << lookup[identifier]
        masks.append(mask)
    evidence_cache, edges = {}, []
    for first, a in enumerate(nodes):
        for second in range(first + 1, len(nodes)):
            common = masks[first] & masks[second]
            if not common:
                continue
            if common not in evidence_cache:
                remaining, evidence = common, []
                while remaining:
                    bit = remaining & -remaining
                    evidence.append(paper_ids[bit.bit_length() - 1])
                    remaining -= bit
                evidence_cache[common] = evidence
            source, target = sorted((a["id"], nodes[second]["id"]))
            edges.append({"source": source, "target": target, "weight": common.bit_count(), "paper_ids": evidence_cache[common]})
    return sorted(edges, key=lambda edge: (-edge["weight"], edge["source"], edge["target"]))


def _communities(nodes, edges):
    """Greedily merge the pair with greatest positive weighted modularity gain."""
    identifiers = sorted(node["id"] for node in nodes)
    if not identifiers:
        return {}, 0.0
    lookup = {identifier: index for index, identifier in enumerate(identifiers)}
    weights = np.zeros((len(nodes), len(nodes)), dtype=float)
    for edge in edges:
        a, b = lookup[edge["source"]], lookup[edge["target"]]
        weights[a, b] = weights[b, a] = edge["weight"]
    total = float(weights.sum() / 2)
    groups = [{identifier} for identifier in identifiers]
    if not total:
        return {identifier: frozenset({identifier}) for identifier in identifiers}, 0.0
    fractions = weights.sum(axis=1) / (2 * total)
    modularity = -float(np.dot(fractions, fractions))
    while len(groups) > 1:
        gains = weights / total - 2 * np.outer(fractions, fractions)
        gains[np.tril_indices(len(groups))] = -np.inf
        first, second = np.unravel_index(np.argmax(gains), gains.shape)
        gain = float(gains[first, second])
        if gain <= 1e-12:
            break
        modularity += gain
        groups[first] |= groups[second]
        weights[first] += weights[second]
        weights[:, first] += weights[:, second]
        weights[first, first] = 0
        fractions[first] += fractions[second]
        groups.pop(second)
        weights = np.delete(np.delete(weights, second, axis=0), second, axis=1)
        fractions = np.delete(fractions, second)
    return {identifier: frozenset(group) for group in groups for identifier in group}, modularity


def _assign_groups(all_nodes, displayed, edges, group_by, topics):
    labels = {str(topic["id"]): str(topic.get("label") or topic["id"]) for topic in topics or []}
    community_groups, modularity = _communities(displayed, edges) if group_by == "community" else ({}, None)
    communities = sorted(set(community_groups.values()), key=lambda group: (-len(group), sorted(group)))
    community_numbers = {group: index + 1 for index, group in enumerate(communities)}
    for node in all_nodes:
        if group_by == "id":
            key, label = node["id"], node["id"]
        elif group_by == "name":
            key = _name_key(node["label"]) if node["names"] else "missing:" + node["id"]
            label = node["label"] if node["names"] else "名前未取得 · " + node["id"]
        elif group_by == "institution":
            primary = node["affiliations"][0] if node["affiliations"] else None
            key, label = (primary["normalized_name"], primary["name"]) if primary else ("unknown", "所属未取得")
        elif group_by == "topic":
            key, label = node["topic_id"], labels.get(node["topic_id"], node["topic_id"])
            if key == "unknown":
                label = "研究分野未取得"
        else:
            members = community_groups.get(node["id"])
            if members is None:
                node["cluster_label"] = "未分類（表示範囲外）"
                continue
            key = "\0".join(sorted(members))
            label = f"共著コミュニティ {community_numbers[members]}"
            if len(members) == 1:
                label += "（単独）"
        node["cluster_id"] = f"{group_by}-" + _digest(key)
        node["cluster_label"] = label
    return modularity


def _layout(nodes):
    members = defaultdict(list)
    for node in nodes:
        members[node["cluster_id"]].append(node)
    clusters = []
    for identifier, values in members.items():
        labels = Counter(node["cluster_label"] for node in values)
        label = min(labels, key=lambda value: (-labels[value], value))
        paper_ids = sorted(set().union(*(set(node["paper_ids"]) for node in values)))
        clusters.append({"id": identifier, "label": label,
                         "node_ids": sorted(node["id"] for node in values), "paper_ids": paper_ids,
                         "author_count": len(values), "paper_count": len(paper_ids)})
    clusters.sort(key=lambda group: (-group["author_count"], -group["paper_count"], group["label"], group["id"]))
    columns = max(1, math.ceil(math.sqrt(len(clusters))))
    rows = max(1, math.ceil(len(clusters) / columns))
    width, height = 0.86 / columns, 0.86 / rows
    palette_index = 0
    for index, cluster in enumerate(clusters):
        if cluster["label"] in {"所属未取得", "研究分野未取得"}:
            cluster["color"] = "#8b96aa"
        else:
            cluster["color"] = COLORS[palette_index % len(COLORS)]
            palette_index += 1
        center = np.array([0.07 + width * (index % columns + 0.5), 0.07 + height * (index // columns + 0.5)])
        cluster.update(x=round(float(center[0]), 6), y=round(float(center[1]), 6))
        ordered = sorted(members[cluster["id"]], key=lambda node: (-node["count"], node["id"]))
        for offset, node in enumerate(ordered):
            radius = 0.0 if len(ordered) == 1 else 0.38 * min(width, height) * math.sqrt((offset + 0.5) / len(ordered))
            angle = offset * math.pi * (3 - math.sqrt(5))
            position = center + radius * np.array([math.cos(angle), math.sin(angle)])
            node.update(x=round(float(position[0]), 6), y=round(float(position[1]), 6), color=cluster["color"])
    return clusters


def build_author_network(papers: list[dict], group_by="community", topics=None) -> dict:
    from .limits import MAX_DATASET_PAPERS
    if group_by not in GROUPS:
        raise ValueError("共著者の表示方法は id・name・institution・community・topic から選んでください。")
    if len(papers) > MAX_DATASET_PAPERS:
        raise ValueError(f"共著者ネットワークの分析上限は {MAX_DATASET_PAPERS:,} 論文です。")
    identifiers = [str(paper.get("id") or "") for paper in papers]
    if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("論文IDは欠測や重複のない値である必要があります。")
    ordered_papers = sorted(papers, key=lambda paper: str(paper["id"]))
    all_nodes, paper_authors, identity_stats = _author_records(ordered_papers)
    displayed = all_nodes[:AUTHOR_LIMIT]
    all_edges = _edges(displayed, ordered_papers)
    modularity = _assign_groups(all_nodes, displayed, all_edges, group_by, topics)
    clusters = _layout(displayed)
    warnings = ["研究者の同定は全表示方法で共通です。異なる明示IDを名前が同じという理由だけで統合しません。ID欠測の名前一致や名前由来の別名による照合は推定です。",
                "共著辺は同じ論文に記載された著者間の関係です。引用・所属移籍・研究の影響を示す矢印ではありません。",
                "被引用数は既知の累積値だけを著者別に合計します。共著者間で同じ論文の値が重複するため、ノードの値を足して論文総引用数にしないでください。"]
    if len(all_nodes) > AUTHOR_LIMIT:
        warnings.append(f"全 {len(all_nodes)} 人のうち論文数上位 {AUTHOR_LIMIT} 人を表示・辺計算の対象にしています。全著者の属性はCSV用データに保持しています。")
    if len(all_edges) > EDGE_LIMIT:
        warnings.append(f"表示著者間の全 {len(all_edges)} 辺のうち共著件数上位 {EDGE_LIMIT} 辺を描画します。グループ計算とCSV用データには表示著者間の全辺を使います。")
    if identity_stats["ambiguous_name_groups"]:
        warnings.append(f"同じ正規化名で別IDのノードがある候補群は {identity_stats['ambiguous_name_groups']} 群です。同一人物とは断定しません。")
    if identity_stats["ambiguous_aliases"]:
        warnings.append(f"複数の明示IDにつながる曖昧な名前別名 {identity_stats['ambiguous_aliases']} 件は、ID間の統合に使いません。")
    if identity_stats["author_records_without_id_or_name"]:
        warnings.append(f"IDも名前もない著者記載 {identity_stats['author_records_without_id_or_name']} 件は人物として同定できません。論文は総数に保持します。")
    notes = {"id": "同定した研究者IDごとに1グループを表示します。",
             "name": "正規化した代表フルネームが一致する別IDノードを同じ候補グループに置きます。名前の一致は人物の同一性を証明しません。",
             "institution": "各著者に明示された所属だけを使います。論文全体の所属を著者へ配布しません。主表示所属は所属が明示された論文数最多、同数なら正規化名順です。全所属と原文字列・論文根拠を保持します。",
             "community": "表示対象著者間の全共著辺を用いて、重み付きmodularityが最大に増える2群を決定的に統合します。正の増分がなくなった時点で止め、接続のない著者は単独群にします。研究組織や真の研究分野を認定する分類ではありません。",
             "topic": "その著者の論文が最も多く割り当てられた研究分野で表示します。同数なら分野ID順です。複数分野の件数は各ノードに保持しています。"}
    warnings.append(notes[group_by])
    affiliations_known = sum(bool(node["affiliations"]) for node in all_nodes)
    associations = sum(node["count"] for node in all_nodes)
    stats = {"authors_total": len(all_nodes), "papers_total": len(ordered_papers),
             "papers_with_identified_authors": len(paper_authors), "displayed_authors": len(displayed),
             "displayed_edges": min(EDGE_LIMIT, len(all_edges)), "edges_total": len(all_edges), "edges_scope": "displayed_authors",
             "clusters_total": len(clusters), "clusters_scope": "displayed_authors",
             "author_display_limit": AUTHOR_LIMIT, "edge_display_limit": EDGE_LIMIT,
             "authors_with_explicit_affiliations": affiliations_known,
             "affiliation_coverage_pct": round(100 * affiliations_known / len(all_nodes), 2) if all_nodes else 0.0,
             "author_paper_associations_total": associations,
             "author_paper_affiliation_coverage_pct": round(100 * identity_stats["author_paper_associations_with_affiliations"] / associations, 2) if associations else 0.0,
             "community_modularity": round(modularity, 8) if modularity is not None else None,
             **identity_stats}
    return {"nodes": displayed, "edges": all_edges[:EDGE_LIMIT], "clusters": clusters, "group_by": group_by,
            "truncated": len(all_nodes) > AUTHOR_LIMIT or len(all_edges) > EDGE_LIMIT,
            "stats": stats, "warnings": warnings,
            "methodology": ["各論文について同じcanonical著者は1人、同じ著者ペアは1辺として数えます。自己辺は生成しません。",
                            "論文数・引用既知件数・著者属性は全対象論文から集計し、表示上限とは分けます。",
                            "所属名はUnicode・空白を正規化し、単一の大学等の名称を含む場合だけDepartmentやLaboratory等の部局候補部分を規則でまとめます。原文字列と規則の根拠を保持し、大学名・所在地が異なる文字列は推測で統合しません。",
                            notes[group_by],
                            "ノード座標はグループを見やすく配置するための座標です。距離やグループの面積に技術的近さ・引用強度の意味はありません。"],
            "export_data": {"authors": all_nodes, "edges": all_edges, "clusters": clusters}}
