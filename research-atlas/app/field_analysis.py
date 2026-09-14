"""Evidence-based field reports; no map-distance, migration or causal inference."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import math
import re
import unicodedata
from urllib.parse import unquote

import numpy as np


AUTHOR_LIMIT = 80
BRIDGE_LIMIT = 30
CITATION_LIMIT = 100
INSTITUTION_LIMIT = 40
EVIDENCE_LIMIT = 40
NEIGHBOR_LIMIT = 8
MENTION_LIMIT = 8

# This is a deliberately finite vocabulary of mentions, not an experimental
# method classifier. Abbreviations can have more than one meaning.
METHODS = [
    ("xrd", "X線回折（XRD）", r"\b(?:x[ -]ray diffraction|XRD)\b|X線回折"),
    ("sem", "走査電子顕微鏡（SEM）", r"\b(?:scanning electron microscop\w*|SEM)\b|走査電子顕微鏡"),
    ("tem", "透過電子顕微鏡（TEM）", r"\b(?:transmission electron microscop\w*|TEM)\b|透過電子顕微鏡"),
    ("ebsd", "電子線後方散乱回折（EBSD）", r"\b(?:electron backscatter diffraction|EBSD)\b|電子線後方散乱回折"),
    ("tensile", "引張試験", r"\b(?:tensile test\w*|tension test\w*|uniaxial tensile)\b|引張(?:り)?試験"),
    ("fatigue", "疲労試験", r"\bfatigue test\w*\b|疲労試験"),
    ("indentation", "押込み・硬さ試験", r"\b(?:nanoindentation|indentation test\w*|hardness test\w*|vickers hardness)\b|押込み試験|硬さ試験|ナノインデンテーション"),
    ("fem", "有限要素法（FEM/FEA）", r"\b(?:finite[ -]element\w*|FEM|FEA)\b|有限要素"),
    ("md", "分子動力学", r"\bmolecular dynamics\b|分子動力学"),
    ("dft", "密度汎関数理論（DFT）", r"\b(?:density functional theory|DFT)\b|密度汎関数"),
    ("ml", "機械学習", r"\bmachine learning\b|機械学習"),
    ("dl", "深層学習・ニューラルネットワーク", r"\b(?:deep learning|neural network\w*)\b|深層学習|ニューラルネットワーク"),
    ("rf", "ランダムフォレスト", r"\brandom forest\w*\b|ランダムフォレスト"),
    ("lpbf", "レーザー粉末床溶融（LPBF/SLM）", r"\b(?:laser powder bed fusion|selective laser melting|L[ -]?PBF|SLM)\b|レーザー粉末床溶融|選択的レーザー溶融"),
    ("waam", "ワイヤアーク積層造形（WAAM）", r"\b(?:wire arc additive manufacturing|WAAM)\b|ワイヤアーク積層"),
    ("heat", "熱処理・焼なまし", r"\b(?:heat treatment\w*|annealing|tempering)\b|熱処理|焼なまし|焼鈍|焼戻し"),
    ("rolling", "圧延", r"\b(?:(?:cold|hot)[ -]rolling|rolling process)\b|圧延"),
    ("welding", "溶接", r"\b(?:welding|welded)\b|溶接"),
    ("electrochem", "電気化学測定", r"\b(?:electrochemical impedance|potentiodynamic polarization|cyclic voltammetry)\b|電気化学インピーダンス|動電位分極|サイクリックボルタンメトリー"),
    ("spectroscopy", "分光分析", r"\b(?:spectroscopy|spectrometry|FTIR|Raman)\b|分光分析|分光法"),
    ("pcr", "PCR・定量PCR", r"\b(?:polymerase chain reaction|quantitative PCR|qPCR|PCR)\b|定量PCR|ポリメラーゼ連鎖反応"),
    ("rna_seq", "RNAシーケンシング", r"\b(?:RNA sequencing|(?:sc)?RNA[ -]?seq)\b|RNAシーケンシング"),
    ("crispr", "CRISPR", r"\bCRISPR(?:[ -]Cas\w*)?\b"),
    ("rct", "ランダム化比較試験（RCT）", r"\b(?:randomi[sz]ed controlled trial\w*|RCT)\b|ランダム化比較試験|無作為化比較試験"),
    ("review", "系統的レビュー・メタ分析", r"\b(?:systematic review\w*|meta[ -]analys[ie]s)\b|システマティックレビュー|系統的レビュー|メタ分析"),
    ("interview_survey", "インタビュー・調査", r"\b(?:interview\w*|survey\w*|questionnaire\w*)\b|インタビュー|質問票|アンケート"),
    ("regression", "回帰分析", r"\bregression\b|回帰分析"),
    ("monte_carlo", "モンテカルロ法", r"\bMonte[ -]Carlo\b|モンテカルロ"),
]


def _text(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _key(value):
    return _text(value).casefold()


def _list(value):
    return value if isinstance(value, list) else []


def _year(paper):
    try:
        value = int(paper.get("year"))
        return value if 1800 <= value <= 2200 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _paper_sort(paper):
    return (-(_year(paper) or 0), str(paper.get("id", "")))


def _unique_ids(papers, limit):
    return list(dict.fromkeys(str(p["id"]) for p in sorted(papers, key=_paper_sort)))[:limit]


def store_nmf_geometry(papers, topics, membership, details):
    """Persist full-corpus latent centroids and document weights, never 2D points.

    Mutates only analytics' freshly normalized paper copies. Unassigned latent
    components retain their mass and dimensions rather than being renormalized.
    """
    if membership is None:
        return None
    values = np.asarray(membership, dtype=float)
    if values.ndim != 2 or len(values) != len(papers) or not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("NMF の保存用文書–トピック重みが不正です。")
    components = {}
    for label, component in details.get("label_to_component", {}).items():
        if 0 <= int(component) < values.shape[1]:
            components[f"topic-{int(label) + 1}"] = int(component)
    valid_topics = [t for t in topics if not t.get("is_outlier") and t["id"] in components]
    for paper, row in zip(papers, values):
        paper["topic_weights"] = {topic: round(float(row[component]), 10) for topic, component in components.items()}
        paper["topic_weights_unassigned_mass"] = round(max(0.0, float(row.sum()) - sum(row[c] for c in components.values())), 10)
    centroids = []
    topic_ids = []
    for topic in valid_topics:
        indices = [i for i, paper in enumerate(papers) if paper.get("topic_id") == topic["id"]]
        if indices:
            topic_ids.append(topic["id"])
            centroids.append(values[indices].mean(axis=0))
    similarities = []
    for index, first in enumerate(centroids):
        for other in range(index + 1, len(centroids)):
            second = centroids[other]
            denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
            value = float(np.dot(first, second)) / denominator if denominator > 0 else 0.0
            similarities.append({"source_topic_id": topic_ids[index], "target_topic_id": topic_ids[other],
                                 "similarity": round(min(1.0, max(0.0, value)), 10)})
    return {"schema_version": 1, "method": "nmf_centroid_cosine", "topic_ids": topic_ids,
            "centroids": [np.round(value, 10).tolist() for value in centroids],
            "similarities": similarities, "component_topics": {str(component): topic for topic, component in components.items()},
            "paper_count": len(papers), "latent_dimensions": values.shape[1],
            "basis": "全対象論文の正規化 NMF 文書–トピック重みを分類群内で平均した重心の cosine 類似度。2D 地図の座標は使いません。"}


def _author_identity(papers):
    """Resolve explicit aliases, protecting ambiguous name-only bridges."""
    parent, records, alias_owners = {}, [], defaultdict(set)

    def find(identifier):
        parent.setdefault(identifier, identifier)
        while parent[identifier] != identifier:
            parent[identifier] = parent[parent[identifier]]
            identifier = parent[identifier]
        return identifier

    def union(first, second):
        first, second = find(first), find(second)
        if first != second:
            parent[max(first, second)] = min(first, second)

    for paper in papers:
        for author in _list(paper.get("authors")):
            if isinstance(author, str):
                author = {"name": author}
            if not isinstance(author, dict):
                continue
            identifier = _text(author.get("id")) or "name:" + _key(author.get("name"))
            if identifier == "name:":
                continue
            aliases = {_text(value) for value in _list(author.get("aliases")) if _text(value)} | {identifier}
            find(identifier)
            for alias in aliases:
                alias_owners[alias].add(identifier)
            records.append((paper, author, identifier, aliases))
    for alias, owners in alias_owners.items():
        if not alias.startswith("name:"):
            for owner in owners:
                union(alias, owner)
    ambiguous = 0
    for alias, owners in alias_owners.items():
        if alias.startswith("name:"):
            known = {find(owner) for owner in owners if not owner.startswith("name:")}
            if len(known) > 1:
                ambiguous += 1
                continue
            for owner in owners:
                union(alias, owner)
    groups = defaultdict(list)
    for paper, author, identifier, aliases in records:
        groups[find(identifier)].append((paper, author, identifier, aliases))
    output = {}
    for entries in groups.values():
        identifiers = {entry[2] for entry in entries}
        aliases = set().union(*(entry[3] for entry in entries))
        identifiers |= {alias for alias in aliases if not alias.startswith("name:")}
        rank = lambda identifier: (0 if identifier.startswith("orcid:") else 1 if identifier.startswith("scopus:") else 3 if identifier.startswith("name:") else 2, identifier)
        identifier = min(identifiers, key=rank)
        basis = ("orcid" if identifier.startswith("orcid:") else "scopus_id" if identifier.startswith("scopus:")
                 else "name_estimate" if identifier.startswith("name:") else "provided_id")
        if basis != "name_estimate" and any(entry[2].startswith("name:") for entry in entries):
            basis = "name_alias_estimate"
        names = [_text(entry[1].get("name")) for entry in entries if _text(entry[1].get("name"))]
        name_counts = Counter(names)
        name = sorted(name_counts, key=lambda value: (-name_counts[value], value))[0] if names else identifier
        output[identifier] = {"id": identifier, "name": name, "identity_basis": basis,
                              "papers": {str(entry[0]["id"]): entry[0] for entry in entries}}
    return output, ambiguous


def _shared_authors(authors, focus_id, neighbor_id):
    rows, transitions = [], []
    if neighbor_id is None:
        return rows, transitions
    for author in authors.values():
        first = [p for p in author["papers"].values() if p.get("topic_id") == focus_id and _year(p) is not None]
        second = [p for p in author["papers"].values() if p.get("topic_id") == neighbor_id and _year(p) is not None]
        if not first or not second:
            continue
        focus_years = sorted({_year(p) for p in first})
        neighbor_years = sorted({_year(p) for p in second})
        evidence = _unique_ids(first, len(first)) + _unique_ids(second, len(second))
        rows.append({key: author[key] for key in ("id", "name", "identity_basis")} |
                    {"focus_years": focus_years, "neighbor_years": neighbor_years,
                     "evidence_ids": evidence, "evidence_total": len(first) + len(second)})
        start, end = focus_years[0], neighbor_years[0]
        earliest = sorted([p for p in first if _year(p) == start], key=lambda p: str(p["id"]))[:1]
        earliest += sorted([p for p in second if _year(p) == end], key=lambda p: str(p["id"]))[:1]
        transitions.append({"author_id": author["id"], "name": author["name"], "identity_basis": author["identity_basis"],
                            "direction": "focus_to_neighbor" if start < end else "neighbor_to_focus" if start > end else "same_year",
                            "from_year": min(start, end), "to_year": max(start, end),
                            "evidence_ids": [str(p["id"]) for p in earliest]})
    rows.sort(key=lambda row: (-row["evidence_total"], row["id"]))
    transitions.sort(key=lambda row: (row["from_year"], row["to_year"], row["author_id"]))
    return rows, transitions


def _neighbor_rows(result, focus, topics, authors):
    geometry = result.get("field_geometry") or {}
    cosine = geometry.get("method") == "nmf_centroid_cosine" and result.get("meta", {}).get("topic_model") == "nmf"
    scores = {}
    if cosine:
        for row in geometry.get("similarities", []):
            value = row.get("similarity")
            if isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1:
                scores[frozenset((row.get("source_topic_id"), row.get("target_topic_id")))] = float(value)
    focus_terms = {_key(term) for term in focus.get("keywords", []) if _key(term)}
    rows = []
    for other in topics:
        if other["id"] == focus["id"]:
            continue
        pair = frozenset((focus["id"], other["id"]))
        if cosine and pair in scores:
            similarity, method = scores[pair], "nmf_centroid_cosine"
        else:
            terms = {_key(term) for term in other.get("keywords", []) if _key(term)}
            similarity = len(terms & focus_terms) / len(terms | focus_terms) if terms and focus_terms else None
            method = "representative_keyword_jaccard"
        shared, _ = _shared_authors(authors, focus["id"], other["id"])
        rows.append({"topic_id": other["id"], "label": other["label"], "similarity": round(similarity, 6) if similarity is not None else None,
                     "method": method, "shared_author_count": len(shared)})
    return sorted(rows, key=lambda row: (row["similarity"] is None, -(row["similarity"] or 0), -row["shared_author_count"], row["topic_id"]))


def _doi(value):
    value = unquote(_key(value))
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else ""


def _identifiers(paper):
    aliases = paper.get("aliases") if isinstance(paper.get("aliases"), dict) else {}
    values = set()
    for value in [paper.get("id"), *aliases.get("ids", []), *aliases.get("eids", [])]:
        if _text(value):
            values.add("id:" + _key(value))
    for value in [paper.get("doi"), *aliases.get("dois", [])]:
        if _doi(value):
            values.add("doi:" + _doi(value))
    return values


def _citation_links(all_papers, focus_papers, neighbor_papers, selected):
    identifiers = defaultdict(set)
    for paper in all_papers:
        for identifier in _identifiers(paper):
            identifiers[identifier].add(str(paper["id"]))
    lookup = {str(paper["id"]): paper for paper in all_papers}
    focus_ids, neighbor_ids = {str(p["id"]) for p in focus_papers}, {str(p["id"]) for p in neighbor_papers}
    rows, known_sources, reference_total, ambiguous, matched = [], 0, 0, 0, 0
    for paper in focus_papers + neighbor_papers:
        references = _list(paper.get("references"))
        known_sources += bool(references) or paper.get("references_status") == "provided"
        for reference in references:
            if isinstance(reference, str):
                reference = {"doi": reference} if _doi(reference) else {"id": reference}
            if not isinstance(reference, dict):
                continue
            reference_total += 1
            targets = set()
            for key in _identifiers(reference):
                targets |= identifiers.get(key, set())
            if len(targets) > 1:
                ambiguous += 1
                continue
            if not targets:
                continue
            matched += 1
            target = next(iter(targets))
            source = str(paper["id"])
            if selected and ((source in focus_ids and target in neighbor_ids) or (source in neighbor_ids and target in focus_ids)):
                rows.append({"source_id": source, "target_id": target, "source_topic_id": paper["topic_id"],
                             "target_topic_id": lookup[target]["topic_id"]})
    rows = list({(row["source_id"], row["target_id"]): row for row in rows}.values())
    rows.sort(key=lambda row: (row["source_id"], row["target_id"]))
    n = len(focus_papers) + len(neighbor_papers)
    coverage = {"available": known_sources > 0 and selected, "papers_with_references": known_sources,
                "papers_total": n, "coverage_pct": round(100 * known_sources / n, 2) if n else 0.0,
                "references_total": reference_total, "matched_references": matched,
                "ambiguous_references": ambiguous, "cross_field_links_total": len(rows),
                "reason": "comparison_not_selected" if not selected else "observed_references" if known_sources else "references_not_provided",
                "meaning": "取得済み参照リストを識別子で照合した引用元→引用先。被引用回数・意味類似から矢印を補完しません。"}
    return rows, coverage


def _affiliations(paper):
    values = list(_list(paper.get("affiliations")))
    for author in _list(paper.get("authors")):
        if isinstance(author, dict):
            values.extend(_list(author.get("affiliations")))
    return {_key(value): _text(value) for value in values if isinstance(value, str) and _text(value)}


def _institutions(focus, neighbor, selected):
    rows, covered = {}, [0, 0]
    for group, papers in enumerate((focus, neighbor)):
        for paper in papers:
            names = _affiliations(paper)
            covered[group] += bool(names)
            for key, name in names.items():
                row = rows.setdefault(key, {"name": name, "focus_count": 0, "neighbor_count": 0 if selected else None, "evidence_ids": []})
                row["focus_count" if group == 0 else "neighbor_count"] += 1
                row["evidence_ids"].append(str(paper["id"]))
    ordered = sorted(rows.values(), key=lambda row: (-(row["focus_count"] + (row["neighbor_count"] or 0)), row["name"]))
    for row in ordered:
        row["evidence_total"] = len(row["evidence_ids"])
        row["evidence_ids"] = sorted(set(row["evidence_ids"]))
    return {"focus_coverage_pct": round(100 * covered[0] / len(focus), 2) if focus else 0.0,
            "neighbor_coverage_pct": round(100 * covered[1] / len(neighbor), 2) if neighbor else None,
            "rows": ordered[:INSTITUTION_LIMIT], "_export_rows": ordered,
            "total_rows": len(ordered), "display_limit": INSTITUTION_LIMIT,
            "notes": ["機関数は、論文または著者に明示された所属文字列を論文単位で重複排除して集計します。欠測から機関を推定しません。",
                      "論文単位の所属比較です。特定研究者の所属・移籍・共同研究先を推定しません。名称の表記揺れ・部局や所在地の違いは残ります。"]}


def _methods(focus, neighbor, selected):
    rows = []
    for identifier, name, expression in METHODS:
        pattern = re.compile(expression, re.IGNORECASE)
        counts, evidence, mention_groups = [0, 0], [], [[], []]
        for group, papers in enumerate((focus, neighbor)):
            for paper in sorted(papers, key=_paper_sort):
                abstract = str(paper.get("abstract") or "")
                match = pattern.search(abstract)
                if match:
                    counts[group] += 1
                    evidence.append(str(paper["id"]))
                    if len(mention_groups[group]) < MENTION_LIMIT:
                        mention_groups[group].append({"paper_id": str(paper["id"]), "field": "abstract",
                                                      "snippet": _text(abstract[max(0, match.start() - 90):match.end() + 120])})
        if sum(counts):
            mentions = [group[index] for index in range(MENTION_LIMIT) for group in mention_groups if index < len(group)][:MENTION_LIMIT]
            rows.append({"id": identifier, "name": name, "focus_count": counts[0], "neighbor_count": counts[1] if selected else None,
                         "evidence_ids": evidence, "evidence_total": len(evidence), "mentions": mentions})
    rows.sort(key=lambda row: (-(row["focus_count"] + (row["neighbor_count"] or 0)), row["id"]))
    return {"rows": rows, "total_rows": len(rows), "vocabulary_size": len(METHODS), "mention_display_limit": MENTION_LIMIT,
            "focus_abstract_coverage_pct": round(100 * sum(bool(_text(p.get("abstract"))) for p in focus) / len(focus), 2) if focus else 0.0,
            "neighbor_abstract_coverage_pct": round(100 * sum(bool(_text(p.get("abstract"))) for p in neighbor) / len(neighbor), 2) if neighbor else None,
            "notes": ["固定語彙に一致した抄録中の言及を、論文ごとに1回数えます。実験で使用したことや効果の有無は判定しません。",
                      "否定文・先行研究の紹介も含み、略語は別の意味を持つ場合があります。辞書外の手法・同義語や抄録欠測を取りこぼします。原文の抜粋で確認してください。"]}


def _topic_summary(topic, papers, years):
    counts = Counter(_year(paper) for paper in papers)
    result = {key: deepcopy(topic.get(key)) for key in ("id", "label", "keywords", "forecast", "backtest", "status", "score", "explanation")}
    result["count"] = len(papers)
    result["series"] = [{"year": year, "count": counts[year]} for year in years]
    last = max(years) if years else 0
    result["forecast"] = [row for row in _list(result.get("forecast"))
                          if isinstance(row, dict) and isinstance(row.get("year"), int) and last < row["year"] <= last + 3
                          and isinstance(row.get("value"), (int, float)) and math.isfinite(row["value"]) and row["value"] >= 0][:3]
    result["keywords"] = _list(result.get("keywords"))
    return result


def _observations(report, focus_papers, neighbor_papers):
    focus, neighbor = report["focus"], report["neighbor"]
    output = [{"section": "overview", "title": "分野の構成", "text": f"{focus['label']}は取得集合内で{focus['count']}論文です。代表語は「{'・'.join(focus['keywords'][:5]) or '情報不足'}」です。", "evidence_ids": _unique_ids(focus_papers, 3)}]
    neighbors = focus["neighbors"]
    if neighbors:
        first = (next((row for row in report["export_data"]["neighbors"] if row["topic_id"] == neighbor["id"]), neighbors[0])
                 if neighbor else neighbors[0])
        metric = "NMF重心cosine" if first["method"] == "nmf_centroid_cosine" else "代表語Jaccard"
        value = f"{first['similarity']:.3f}" if first["similarity"] is not None else "算出不可"
        text = f"{'選択した隣接分野' if neighbor else '上位の隣接候補'}は{first['label']}（{metric}={value}、共通著者{first['shared_author_count']}人）です。"
        if neighbor:
            text += f"共通代表語は「{'・'.join(report['keywords']['shared'][:4]) or '一致なし'}」、対象だけの代表語は「{'・'.join(report['keywords']['focus_only'][:3]) or '一致なし'}」、隣接だけの代表語は「{'・'.join(report['keywords']['neighbor_only'][:3]) or '一致なし'}」です。"
        text += "類似性は引用・技術移転・因果的影響を意味しません。"
        output.append({"section": "neighbors", "title": "隣接分野との比較", "text": text, "evidence_ids": []})
    if neighbor:
        counts = report["connections"]["transition_counts"]
        output.append({"section": "researchers", "title": "両分野の研究者", "text": f"両分野に出現する著者は{report['connections']['shared_authors_total']}人です。この集合での初出年は対象→隣接が{counts['focus_to_neighbor']}人、隣接→対象が{counts['neighbor_to_focus']}人、同年が{counts['same_year']}人です。研究の移動や所属移籍を確認した数ではありません。", "evidence_ids": list(dict.fromkeys(e for row in report["connections"]["transitions"][:2] for e in row["evidence_ids"]))})
    coverage = report["connections"]["citation_coverage"]
    if coverage["available"]:
        text = f"参照リストが取得された論文は{coverage['papers_with_references']}/{coverage['papers_total']}件で、対象と隣接分野の間に識別子で照合できた引用は{report['connections']['citation_links_total']}本です。取得範囲内の引用であり、技術の影響量を推定したものではありません。"
    else:
        text = "両分野の取得済み参照リストによる引用関係は観測できていません。累積被引用数・意味類似から引用矢印を作っていません。"
    output.append({"section": "citations", "title": "引用による接続", "text": text,
                   "evidence_ids": list(dict.fromkeys(identifier for row in report["connections"]["citation_links"][:2]
                                                      for identifier in (row["source_id"], row["target_id"])))})
    for section, title in (("institutions", "所属の言及"), ("methods", "抄録中の手法言及")):
        rows = report[section]["rows"]
        if rows:
            text = "、".join(f"{row['name']}は対象{row['focus_count']}件" + (f"・隣接{row['neighbor_count']}件" if neighbor else "") for row in rows[:3])
            evidence = list(dict.fromkeys(identifier for row in rows[:2] for identifier in row["evidence_ids"][:2]))
        else:
            text, evidence = ("所属情報が不足しているため機関差を判定できません。" if section == "institutions" else "固定語彙に一致する抄録中の手法言及がありません。手法が使われていないという意味ではありません。"), []
        output.append({"section": section, "title": title, "text": text, "evidence_ids": evidence})
    predictions = focus["forecast"]
    if predictions:
        last = predictions[-1]
        text = f"既存の論文件数モデルのシナリオでは{last['year']}年に約{float(last.get('value', 0)):.1f}件です。最大3年の取得集合内の外挿で、未知技術の発明、性能や実用化の予測ではありません。"
        if (focus.get("backtest") or {}).get("folds", 0) == 0:
            text += "この期間では予測検証を行えていません。"
        if neighbor and neighbor["forecast"]:
            other = neighbor["forecast"][-1]
            text += f"隣接分野の保存済みシナリオは{other['year']}年に約{float(other['value']):.1f}件です。"
    else:
        text = "保存済みの最大3年以内の数値シナリオがなく、新しい将来値を作っていません。"
    output.append({"section": "outlook", "title": "将来シナリオの読み方", "text": text, "evidence_ids": _unique_ids(focus_papers, 2)})
    return output


def build_field_report(result: dict, topic_id: str, neighbor_id: str | None = None) -> dict:
    """Build a deterministic report solely from the stored analysis and papers."""
    papers = _list(result.get("papers"))
    topics = [topic for topic in _list(result.get("topics")) if not topic.get("is_outlier") and topic.get("status") != "unclassified"]
    topic_lookup = {str(topic["id"]): topic for topic in topics}
    if topic_id not in topic_lookup:
        raise ValueError("対象の分類済み分野が見つかりません。未分類群は分野レポートの対象外です。")
    if neighbor_id is not None and (neighbor_id not in topic_lookup or neighbor_id == topic_id):
        raise ValueError("比較する隣接分野には、対象と異なる分類済み分野を指定してください。")
    focus_papers = [paper for paper in papers if paper.get("topic_id") == topic_id]
    years = sorted(set(result.get("meta", {}).get("years", [])) | {_year(paper) for paper in papers if _year(paper) is not None})
    authors, ambiguous_names = _author_identity(papers)
    focus = _topic_summary(topic_lookup[topic_id], focus_papers, years)
    neighbors = _neighbor_rows(result, focus, topics, authors)
    auto_selected = False
    if neighbor_id is None:
        nearest = next((row for row in neighbors if row["similarity"] is not None and row["similarity"] > 0), None)
        if nearest:
            neighbor_id = nearest["topic_id"]
            auto_selected = True
    neighbor_papers = [paper for paper in papers if neighbor_id is not None and paper.get("topic_id") == neighbor_id]
    neighbor = _topic_summary(topic_lookup[neighbor_id], neighbor_papers, years) if neighbor_id else None
    focus["neighbors"], focus["neighbors_total"] = neighbors[:NEIGHBOR_LIMIT], len(neighbors)
    focus["neighbors_display_limit"] = NEIGHBOR_LIMIT
    shared, transitions = _shared_authors(authors, topic_id, neighbor_id)
    transition_counts = {direction: sum(row["direction"] == direction for row in transitions)
                         for direction in ("focus_to_neighbor", "neighbor_to_focus", "same_year")}
    bridges = []
    is_nmf = result.get("meta", {}).get("topic_model") == "nmf" and (result.get("field_geometry") or {}).get("method") == "nmf_centroid_cosine"
    if is_nmf and neighbor:
        for paper in focus_papers + neighbor_papers:
            weights = paper.get("topic_weights") or {}
            first, second = weights.get(topic_id), weights.get(neighbor_id)
            if all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1 for value in (first, second)) and min(first, second) >= 0.1:
                bridges.append({"id": str(paper["id"]), "title": paper.get("title", ""), "year": _year(paper),
                                "focus_weight": first, "neighbor_weight": second})
        bridges.sort(key=lambda row: (-min(row["focus_weight"], row["neighbor_weight"]), -(row["year"] or 0), row["id"]))
    citation_links, citation_coverage = _citation_links(papers, focus_papers, neighbor_papers, neighbor is not None)
    methods = {row["method"] for row in neighbors}
    basis_notes = ["著者の方向は、この取得集合内で両分野に最初に現れた出版年の順序です。同年には方向を付けません。経歴全体の研究転向・機関移籍・技術の影響を意味しません。",
                   "ORCID・Scopus IDと明示されたID別名を優先します。name: IDやその別名を介した照合は名前に基づく推定です。同姓同名・表記揺れが残ります。",
                   "橋渡し候補はNMFの両成分重みが各0.10以上の対象・隣接分野の論文です。閾値は探索用で、因果関係や学際性の検証ではありません。"]
    if "representative_keyword_jaccard" in methods:
        basis_notes.append("NMFの潜在表現による保存済み類似度がない組合せは代表語集合のJaccard係数で比較します。NMF類似度や2D地図距離ではなく、同義語統合もしていません。")
    if not neighbor:
        basis_notes.append("隣接分野を選択すると、両分野の著者初出年・橋渡し候補・引用関係を比較します。未選択の比較値は欠測です。")
    if not is_nmf:
        basis_notes.append("保存済みNMF文書重みがないため、橋渡し候補は算出していません。空一覧は候補が存在しないという意味ではありません。")
    if ambiguous_names:
        basis_notes.append(f"名前由来の別名が複数の明示IDに結び付く {ambiguous_names} ケースは、その名前だけで同一人物に統合していません。")
    if not citation_coverage["available"]:
        basis_notes.append("両分野の参照リストによる引用関係は観測できていません。被引用回数や意味類似度から矢印を補いません。")
    focus_terms = {_key(value): _text(value) for value in focus["keywords"] if _key(value)}
    neighbor_terms = {_key(value): _text(value) for value in (neighbor["keywords"] if neighbor else []) if _key(value)}
    meta = result.get("meta", {})
    report = {"schema_version": 1, "result_id": result.get("id"), "topic_model": meta.get("topic_model", "kmeans"),
              "focus": focus, "neighbor": neighbor, "neighbors": neighbors[:NEIGHBOR_LIMIT],
              "neighbors_total": len(neighbors), "neighbors_display_limit": NEIGHBOR_LIMIT,
              "annual": [{"year": year, "focus_count": sum(_year(p) == year for p in focus_papers),
                          "neighbor_count": sum(_year(p) == year for p in neighbor_papers) if neighbor else None} for year in years],
              "connections": {"shared_authors": shared[:AUTHOR_LIMIT], "shared_authors_total": len(shared),
                              "transitions": transitions[:AUTHOR_LIMIT], "transitions_total": len(transitions),
                              "transition_counts": transition_counts, "authors_display_limit": AUTHOR_LIMIT,
                              "bridge_papers": bridges[:BRIDGE_LIMIT], "bridge_papers_total": len(bridges), "bridge_display_limit": BRIDGE_LIMIT,
                              "citation_links": citation_links[:CITATION_LIMIT], "citation_links_total": len(citation_links), "citation_display_limit": CITATION_LIMIT,
                              "citation_coverage": citation_coverage, "basis_notes": basis_notes},
              "institutions": _institutions(focus_papers, neighbor_papers, neighbor is not None),
              "methods": _methods(focus_papers, neighbor_papers, neighbor is not None),
              "keywords": {"shared": [focus_terms[key] for key in sorted(focus_terms.keys() & neighbor_terms.keys())],
                           "focus_only": [focus_terms[key] for key in sorted(focus_terms.keys() - neighbor_terms.keys())],
                           "neighbor_only": [neighbor_terms[key] for key in sorted(neighbor_terms.keys() - focus_terms.keys())]},
              "scope": {"dataset_name": result.get("dataset_name", ""), "period": [min(years), max(years)] if years else [],
                        "providers": deepcopy(meta.get("providers", [])), "sampled": bool(meta.get("sampled")), "is_demo": bool(meta.get("is_demo")),
                        "topic_model": meta.get("topic_model", "kmeans"), "similarity_method": sorted(methods),
                        "neighbor_auto_selected": auto_selected,
                        "corpus_papers": len(papers), "focus_papers": len(focus_papers), "neighbor_papers": len(neighbor_papers) if neighbor else None},
              "limitations": ["すべての件数は取得集合内の実論文から計算します。表示上限を適用した一覧と全件集計を区別してください。",
                              "トピック分類・NMF重みは全対象期間を使った回顧的分析です。類似性や著者初出年は技術移転・因果的影響・新規性を証明しません。",
                              "引用リスト・所属・抄録の欠測が残ります。記載がないことと実際に存在しないことは同じではありません。",
                              "将来の数値は保存済みの最大3年の論文件数シナリオです。技術性能・実用化成功確率や校正済み信頼区間ではありません。"]}
    if meta.get("sampled"):
        report["limitations"].append("関連度上位など件数上限付きの標本です。年別・月別件数や分野差を母集団全体の増減へ一般化できません。")
    if meta.get("is_demo"):
        report["limitations"].insert(0, "合成・テストデータを含む分析です。実際の論文動向や研究者・機関の活動の判断には使用できません。")
    report["export_data"] = {"shared_authors": shared, "transitions": transitions,
                             "bridge_papers": bridges, "citation_links": citation_links,
                             "institutions": report["institutions"].pop("_export_rows"),
                             "methods": report["methods"]["rows"], "neighbors": neighbors}
    report["observations"] = _observations(report, focus_papers, neighbor_papers)
    priority = [identifier for row in report["observations"] for identifier in row["evidence_ids"]]
    priority += [identifier for row in shared[:5] for identifier in row["evidence_ids"][:2]]
    priority += [identifier for row in citation_links[:5] for identifier in (row["source_id"], row["target_id"])]
    priority += [row["id"] for row in bridges[:5]]
    # Alternate field samples so the evidence display does not fill with one side.
    ordered_groups = [sorted(focus_papers, key=_paper_sort), sorted(neighbor_papers, key=_paper_sort)]
    for index in range(max(map(len, ordered_groups), default=0)):
        priority.extend(str(group[index]["id"]) for group in ordered_groups if index < len(group))
    priority = list(dict.fromkeys(priority))
    lookup = {str(paper["id"]): paper for paper in papers}
    report["evidence_papers"] = []
    for identifier in priority[:EVIDENCE_LIMIT]:
        paper = lookup[identifier]
        row = {key: deepcopy(paper.get(key)) for key in ("id", "title", "year", "topic_id", "authors", "doi")}
        abstract = str(paper.get("abstract") or "")
        row.update(abstract=abstract[:4000], abstract_truncated=len(abstract) > 4000,
                   affiliations=list(_affiliations(paper).values()))
        report["evidence_papers"].append(row)
    report["evidence_papers_total"], report["evidence_display_limit"] = len(priority), EVIDENCE_LIMIT
    return report
