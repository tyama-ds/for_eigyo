"""Immutable, provenance-aware merging of normalized publication records.

Bibliographic identifiers establish identity; a title match alone never joins
different explicit DOIs. Citation counts from different catalogues are separate
measurements, not numbers to add or to replace with their maximum.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from itertools import chain
import hashlib
import math
import re
import unicodedata
from urllib.parse import unquote, urlsplit

from .ingest import SYNTHETIC_PROVENANCE, _is_synthetic
from .dates import merge_publication_dates, normalize_paper_date
from .bibliography import bibliography_summary, normalize_affiliations, normalize_references, references_status
from .limits import MAX_DATASET_PAPERS


def _text(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _key(value):
    return "".join(character for character in _text(value).casefold() if character.isalnum())


def _doi(value):
    value = _text(value).strip("<>\"'")
    if re.match(r"^https?://(?:dx\.)?doi\.org/", value, re.I):
        value = unquote(urlsplit(value).path.lstrip("/"))
    value = re.sub(r"^doi\s*:\s*", "", value, flags=re.I).strip().casefold()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else ""


def _count(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if math.isfinite(number) and 0 <= number <= 2 ** 53 - 1 and number.is_integer():
            return int(number)
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _list(value):
    return value if isinstance(value, list) else ([] if value is None or value == "" else [value])


def _union(first, second, key=_text):
    output, seen = [], set()
    for value in [*first, *second]:
        identity = key(value)
        if identity and identity not in seen:
            seen.add(identity)
            output.append(deepcopy(value))
    return output


def _author_name(value):
    name = _text(value)
    if name.count(",") == 1:
        family, given = name.split(",", 1)
        name = given.strip() + " " + family.strip()
    return _key(name)


def _author_id_quality(identifier):
    if not identifier or identifier.casefold().startswith(("name:", "unknown:", "anonymous:")):
        return 0
    return 2 if identifier.casefold().startswith(("scopus:", "orcid:", "openalex:")) or identifier.isdigit() else 1


def _authors(first, second, counters):
    prepared = []
    for raw in [*first, *second]:
        author = deepcopy(raw) if isinstance(raw, dict) else {"name": str(raw)}
        name = _text(author.get("name"))
        identifier = _text(author.get("id"))
        if not name and not identifier:
            continue
        name = name or identifier
        identifier = identifier or "name:" + hashlib.sha256(_author_name(name).encode()).hexdigest()[:16]
        author.update(id=identifier, name=name)
        affiliations = normalize_affiliations(author.get("affiliations"))
        if affiliations:
            author["affiliations"] = affiliations
        author["aliases"] = _union(_list(author.get("aliases")), [identifier])
        prepared.append(author)

    # Only provided persistent IDs/aliases can connect different persistent IDs.
    # A shared name hash is not an explicit identity assertion between people.
    parents = {}
    def root(identifier):
        parents.setdefault(identifier, identifier)
        if parents[identifier] != identifier:
            parents[identifier] = root(parents[identifier])
        return parents[identifier]
    def persistent(author):
        return {_text(item) for item in author["aliases"] if _author_id_quality(_text(item))}
    for author in prepared:
        keys = sorted(persistent(author))
        if keys:
            for key in keys[1:]:
                parents[root(key)] = root(keys[0])
    name_groups = defaultdict(set)
    for author in prepared:
        name_groups[_author_name(author["name"])].update(root(key) for key in persistent(author))
    counters["author_name_conflicts"] += sum(len(groups) > 1 for groups in name_groups.values())

    output = []
    for author in prepared:
        identifier, name, aliases = author["id"], author["name"], author["aliases"]
        roots = {root(key) for key in persistent(author)}
        exact = [item for item in output if roots & {root(key) for key in persistent(item)}]
        if not roots:
            exact = [item for item in output if not persistent(item)
                     and set(aliases) & set(item["aliases"])]
        if exact:
            match = exact[0]
        else:
            # Look across all supplied records before resolving a missing ID, so
            # author ordering cannot choose one of two same-name explicit IDs.
            candidates = [item for item in output
                          if _author_name(item["name"]) == _author_name(name)
                          and not (roots and persistent(item))]
            unique_name = len(name_groups[_author_name(name)]) <= 1
            match = candidates[0] if len(candidates) == 1 and unique_name else None
            if candidates and (len(candidates) > 1 or not unique_name):
                counters["author_name_ambiguities"] += 1
        if match is None:
            output.append(author)
            continue
        if match["id"] != identifier:
            if _author_id_quality(match["id"]) and _author_id_quality(identifier):
                counters["author_identifier_aliases"] += 1
            if _author_id_quality(identifier) > _author_id_quality(match["id"]):
                match["id"] = identifier
        if re.match(r"^(?:author\s+|scopus:|orcid:|name:)", match["name"], re.I) or len(_key(name)) > len(_key(match["name"])):
            match["name"] = name
        match["aliases"] = _union(match.get("aliases", []), aliases)
        affiliations = normalize_affiliations([*match.get("affiliations", []), *author.get("affiliations", [])])
        if affiliations:
            match["affiliations"] = affiliations
    return output


def _source(paper):
    explicit = _text(paper.get("citation_source")).casefold()
    if explicit:
        return explicit
    if _is_synthetic(paper):
        return "synthetic"
    providers = paper.get("providers") or []
    return providers[0] if len(providers) == 1 else ""


def _snapshots(paper):
    result = []
    for raw in _list(paper.get("citation_snapshots")):
        if not isinstance(raw, dict):
            continue
        count = _count(raw.get("count"))
        if count is not None:
            entry = deepcopy(raw)
            entry.update(provider=_text(raw.get("provider") or _source(paper) or "unknown").casefold(),
                         count=count, retrieved_at=_text(raw.get("retrieved_at")))
            result.append(entry)
    if paper.get("citations") is not None and not any(row["provider"] == (_source(paper) or "unknown")
                                                     and row["count"] == paper["citations"] for row in result):
        result.append({"provider": _source(paper) or "unknown", "count": paper["citations"],
                       "retrieved_at": _text(paper.get("retrieved_at"))})
    return _union(result, [], key=lambda row: (row["provider"], row["count"], row["retrieved_at"]))


def _history_snapshots(paper):
    result = []
    for raw in _list(paper.get("citation_history_snapshots")):
        if isinstance(raw, dict) and isinstance(raw.get("history"), dict):
            history = {str(int(year)): count for year, value in raw["history"].items()
                       if str(year).isdigit() and (count := _count(value)) is not None}
            if history:
                result.append({"provider": _text(raw.get("provider") or "unknown").casefold(), "history": history,
                               "retrieved_at": _text(raw.get("retrieved_at"))})
    recorded = {(year, count) for snapshot in result if snapshot["provider"] == (_source(paper) or "unknown")
                for year, count in snapshot["history"].items()}
    if paper["citation_history"] and not all(item in recorded for item in paper["citation_history"].items()):
        result.append({"provider": _source(paper) or "unknown", "history": deepcopy(paper["citation_history"]),
                       "retrieved_at": paper["retrieved_at"]})
    return _union(result, [], key=_history_snapshot_key)


def _history_snapshot_key(item):
    return item["provider"], item["retrieved_at"], tuple(sorted(item["history"].items()))


def _normalize(raw, counters):
    if not isinstance(raw, dict):
        raise ValueError("統合する論文データは辞書の配列で指定してください。")
    # Normalizers below return new nested structures. Copy retained extension
    # fields only, avoiding deep-copying every author/reference/snapshot twice.
    rebuilt = {"authors", "affiliations", "references", "keywords", "providers", "aliases",
               "citation_history", "citation_snapshots", "citation_history_snapshots"}
    paper = {key: value if key in rebuilt else deepcopy(value) for key, value in raw.items()}
    paper["id"] = _text(paper.get("id"))
    if not paper["id"]:
        raise ValueError("統合する論文に ID がありません。先に書誌情報を正規化してください。")
    paper["title"] = _text(paper.get("title"))
    paper["abstract"] = str(paper.get("abstract") or "")
    year = _count(paper.get("year"))
    paper["year"] = year
    dates = normalize_paper_date(paper)
    if dates.pop("warnings"):
        counters["date_invalid_records"] += 1
    paper.update(dates)
    paper["doi"] = _doi(paper.get("doi"))
    paper["source"] = _text(paper.get("source"))  # Journal/source title, never a citation provider.
    paper["providers"] = _union([], [_text(item).casefold() for item in _list(paper.get("providers"))])
    paper["keywords"] = _union([], [_text(item) for item in _list(paper.get("keywords"))], key=lambda value: _text(value).casefold())
    paper["authors"] = _authors([], _list(paper.get("authors")), counters)
    paper["affiliations"] = normalize_affiliations([*normalize_affiliations(paper.get("affiliations")),
        *(item for author in paper["authors"] for item in author.get("affiliations", []))])
    paper["references"] = normalize_references(paper.get("references"))
    paper["references_status"] = references_status(paper)
    paper["citations"] = _count(paper.get("citations"))
    paper["citation_history"] = {str(int(year)): count for year, value in (paper.get("citation_history") or {}).items()
                                 if str(year).isdigit() and (count := _count(value)) is not None}
    paper["citation_source"] = _source(paper)
    paper["retrieved_at"] = _text(paper.get("retrieved_at"))
    paper["external_url"] = _text(paper.get("external_url"))
    original_aliases = paper.get("aliases") if isinstance(paper.get("aliases"), dict) else {}
    aliases = deepcopy(original_aliases)
    aliases["eids"] = _union([], [_text(item) for item in _list(aliases.get("eids"))])
    if paper["id"].casefold().startswith("2-s2.0-"):
        aliases["eids"] = _union(aliases["eids"], [paper["id"]])
    aliases["ids"] = _union(_list(aliases.get("ids")), [paper["id"]], key=lambda value: _text(value).casefold())
    aliases["dois"] = _union([], [_doi(item) for item in [paper["doi"], *_list(aliases.get("dois"))]], key=_doi)
    titles = [item for item in _list(aliases.get("titles")) if isinstance(item, dict) and item.get("title") and _count(item.get("year")) is not None]
    if paper["title"] and year is not None:
        titles.append({"title": paper["title"], "year": year})
    aliases["titles"] = _union(titles, [], key=lambda item: (_key(item["title"]), int(item["year"])))
    paper["aliases"] = aliases
    paper["citation_snapshots"] = _snapshots(paper)
    paper["citation_history_snapshots"] = _history_snapshots(paper)
    if _is_synthetic(raw):
        paper["provenance"] = SYNTHETIC_PROVENANCE
    return paper


def _strong_keys(paper):
    aliases = paper["aliases"]
    identities = {"id:" + _text(item).casefold() for item in [paper["id"], *aliases["ids"], *aliases["eids"]] if _text(item)}
    identities.update("doi:" + item for item in aliases["dois"] if item)
    return identities


def _title_keys(paper):
    return {(_key(item["title"]), int(item["year"])) for item in paper["aliases"]["titles"] if _key(item["title"])}


def _compatible_dois(first, second):
    left, right = set(first["aliases"]["dois"]), set(second["aliases"]["dois"])
    return not left or not right or bool(left & right)


def _has_counts(paper):
    return paper["citations"] is not None or bool(paper["citation_history"])


def _merge_pair(first, second, counters):
    # Both normalized records are already privately owned by this merge. Only
    # the nested values mutated below need another copy; other collections are
    # replaced with freshly normalized unions.
    merged = first.copy()
    merged["citation_history"] = first["citation_history"].copy()
    merged["aliases"] = first["aliases"].copy()
    if not _compatible_dois(first, second):
        counters["doi_identifier_conflicts"] += 1
    merged["citation_snapshots"] = _union(first["citation_snapshots"], second["citation_snapshots"],
                                         key=lambda row: (row["provider"], row["count"], row["retrieved_at"]))
    merged["citation_history_snapshots"] = _union(first["citation_history_snapshots"], second["citation_history_snapshots"],
                                                 key=_history_snapshot_key)
    first_source, second_source = first["citation_source"], second["citation_source"]
    same_source = bool(first_source and first_source != "unknown" and second_source and first_source == second_source)
    first_measured, second_measured = _has_counts(first), _has_counts(second)
    may_combine = same_source or not first_measured or not second_measured
    if first["citations"] is not None and second["citations"] is not None and first["citations"] != second["citations"]:
        counters["citation_count_conflicts"] += 1
    if not first_measured and second_measured:
        merged["citations"] = second["citations"]
        merged["citation_history"] = deepcopy(second["citation_history"])
        merged["citation_source"] = second_source
    elif may_combine:
        if merged["citations"] is None and second["citations"] is not None:
            merged["citations"] = second["citations"]
        for year, count in second["citation_history"].items():
            if year in merged["citation_history"] and merged["citation_history"][year] != count:
                counters["citation_history_conflicts"] += 1
            else:
                merged["citation_history"].setdefault(year, count)
        if not merged["citation_source"] and second_source:
            merged["citation_source"] = second_source
    else:
        # An existing annual series also counts as a measurement. Keep its source coherent.
        if second["citation_history"] or (first["citations"] is None and second["citations"] is not None):
            counters["citation_source_conflicts"] += 1
    for field in ("title", "abstract", "doi", "source", "external_url", "retrieved_at", "year"):
        if not merged.get(field) and second.get(field):
            merged[field] = deepcopy(second[field])
    dates, date_warnings = merge_publication_dates(merged, second)
    merged.update(dates)
    if date_warnings:
        counters["date_conflicts"] += 1
    merged["providers"] = _union(first["providers"], second["providers"])
    merged["keywords"] = _union(first["keywords"], second["keywords"], key=lambda value: _text(value).casefold())
    merged["authors"] = _authors(first["authors"], second["authors"], counters)
    merged["affiliations"] = normalize_affiliations([*first["affiliations"], *second["affiliations"]])
    merged["references"] = normalize_references([*first["references"], *second["references"]])
    merged["references_status"] = "provided" if "provided" in (references_status(first), references_status(second)) else "not_provided"
    for field in ("ids", "eids", "dois"):
        merged["aliases"][field] = _union(first["aliases"][field], second["aliases"][field], key=lambda value: _text(value).casefold())
    merged["aliases"]["titles"] = _union(first["aliases"]["titles"], second["aliases"]["titles"],
                                         key=lambda item: (_key(item["title"]), int(item["year"])))
    provenances = _union(_list(first.get("provenances")) + _list(first.get("provenance")),
                         _list(second.get("provenances")) + _list(second.get("provenance")))
    if provenances:
        merged["provenances"] = provenances
    if _is_synthetic(first) or _is_synthetic(second):
        merged["provenance"] = SYNTHETIC_PROVENANCE
    elif not merged.get("provenance") and second.get("provenance"):
        merged["provenance"] = deepcopy(second["provenance"])
    return merged


def merge_papers(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], dict]:
    """Return a new merged corpus and an auditable report, without modifying inputs.

    ``imported_count`` is the final number of unique records; ``merged_count``
    and ``duplicates_removed`` both count absorbed duplicate records, including
    duplicates already present in either input. Existing IDs win in input order.
    """
    if not isinstance(existing, list) or not isinstance(incoming, list):
        raise ValueError("統合する論文データは配列で指定してください。")
    counters = Counter()
    records, parents = {}, {}
    strong_index, title_index = {}, defaultdict(set)

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def matches(index, keys):
        return {root(item) for key in keys for item in index.get(key, ())}

    def register(index):
        for key in _strong_keys(records[index]):
            # A strong identifier has one live root after each iteration.
            # Hundreds of thousands of one-element sets waste substantial RAM.
            strong_index[key] = index
        for key in _title_keys(records[index]):
            title_index[key].add(index)

    for index, raw in enumerate(chain(existing, incoming)):
        current = _normalize(raw, counters)
        strong = {root(strong_index[key]) for key in _strong_keys(current) if key in strong_index}
        title_matches = matches(title_index, _title_keys(current)) - strong
        compatible = {item for item in title_matches if _compatible_dois(records[item], current)}
        counters["title_doi_conflicts"] += len(title_matches - compatible)
        if strong:
            # A missing DOI on a bridge record cannot bypass a DOI already known at the anchor.
            direct_dois = set(current["aliases"]["dois"])
            for item in strong:
                direct_dois.update(records[item]["aliases"]["dois"])
            if direct_dois:
                compatible = {item for item in compatible if not records[item]["aliases"]["dois"]
                              or direct_dois.intersection(records[item]["aliases"]["dois"])}
            else:
                doi_sets = [set(records[item]["aliases"]["dois"]) for item in compatible if records[item]["aliases"]["dois"]]
                if any(not left.intersection(right) for i, left in enumerate(doi_sets) for right in doi_sets[i + 1:]):
                    counters["ambiguous_matches"] += 1
                    compatible = set()
        candidates = strong | compatible
        if not strong and len(candidates) > 1:
            doi_sets = [set(records[item]["aliases"]["dois"]) for item in candidates if records[item]["aliases"]["dois"]]
            if any(not left.intersection(right) for i, left in enumerate(doi_sets) for right in doi_sets[i + 1:]):
                counters["ambiguous_matches"] += 1
                candidates = set()
        if not candidates:
            parents[index] = index
            records[index] = current
            register(index)
            continue
        target = min(candidates)
        merged = records[target]
        for other in sorted(candidates - {target}):
            merged = _merge_pair(merged, records.pop(other), counters)
            parents[other] = target
            counters["duplicates_removed"] += 1
        merged = _merge_pair(merged, current, counters)
        records[target] = merged
        counters["duplicates_removed"] += 1
        if index >= len(existing):
            counters["matched_incoming_count"] += 1
        register(target)
    output = [records[index] for index in sorted(records)]
    if len(output) > MAX_DATASET_PAPERS:
        raise ValueError(f"重複を除いた統合後の論文が {MAX_DATASET_PAPERS:,} 件を超えます。別のデータセットに分けてください。")
    synthetic_count = sum(_is_synthetic(paper) for paper in output)
    warnings = []
    if counters["date_invalid_records"]:
        warnings.append(f"公開日が不正または出版年と矛盾する {counters['date_invalid_records']} 件は、出版年を保持して月次分析の対象外にしました。")
    if counters["date_conflicts"]:
        warnings.append(f"重複論文の日付が {counters['date_conflicts']} 件で矛盾し、既存の出版年・明示日付・精度を保持しました。月情報のない既存論文には同じ出版年の日付のみ補完します。")
    if counters["citation_count_conflicts"]:
        warnings.append(f"重複論文の累積引用数が {counters['citation_count_conflicts']} 件で異なりました。既存の既知値を保持し、各サービスの値を citation_snapshots に保存しました。引用数の加算・最大値への置換は行っていません。")
    if counters["citation_history_conflicts"]:
        warnings.append(f"同じサービスの年別引用数が {counters['citation_history_conflicts']} 箇所で異なり、既存値を保持しました。")
    if counters["citation_source_conflicts"]:
        warnings.append(f"引用履歴の出典が異なる、または不明な重複論文 {counters['citation_source_conflicts']} 件では、既存の計数系列を保持しました。追加の年別系列は citation_history_snapshots に分離して保存し、混合しません。既存に年別系列がある場合、別出典の累積値は snapshots のみに保持することがあります。")
    if counters["title_doi_conflicts"]:
        warnings.append(f"同じタイトル・出版年でも DOI が異なる組合せ {counters['title_doi_conflicts']} 件は、タイトルだけで統合しませんでした。")
    if counters["ambiguous_matches"]:
        warnings.append(f"同じタイトル・出版年に複数の異なる DOI が対応する {counters['ambiguous_matches']} 件は、曖昧な照合として別レコードに保持しました。")
    if counters["doi_identifier_conflicts"]:
        warnings.append(f"共通 ID に複数 DOI がある組合せ {counters['doi_identifier_conflicts']} 件を検出しました。既存 DOI を表示値として保持し、DOI 別名を保存しました。")
    if counters["author_identifier_aliases"]:
        warnings.append("明示された著者ID・別名の対応により、既存の実IDと提供された別名を保持しました。異なる実IDを氏名の一致だけでは統合していません。")
    if counters["author_name_conflicts"] or counters["author_name_ambiguities"]:
        warnings.append("同名でも異なる明示IDを持つ著者は別人として保持しました。ID欠測の著者に同名候補が複数ある場合も、所属やIDを推定して統合していません。")
    if synthetic_count:
        warnings.append(f"合成デモの由来がある {synthetic_count} 件を含みます。この統合集合は実際の研究動向を示すデータとして扱えません。")
    citation_conflicts = sum(counters[key] for key in ("citation_count_conflicts", "citation_history_conflicts", "citation_source_conflicts"))
    report = {"existing_count": len(existing), "incoming_count": len(incoming), "imported_count": len(output),
              "added_count": sum(index >= len(existing) for index in records),
              "merged_count": counters["duplicates_removed"], "duplicates_removed": counters["duplicates_removed"],
              "matched_incoming_count": counters["matched_incoming_count"], "invalid_rows": 0,
              "citation_conflicts": citation_conflicts, "citation_count_conflicts": counters["citation_count_conflicts"],
              "citation_history_conflicts": counters["citation_history_conflicts"],
              "citation_source_conflicts": counters["citation_source_conflicts"],
              "identity_conflicts": counters["title_doi_conflicts"] + counters["doi_identifier_conflicts"],
              "ambiguous_matches": counters["ambiguous_matches"], "is_demo": synthetic_count > 0,
              "synthetic_count": synthetic_count, "warnings": warnings,
              "date_pipeline_version": 2, "date_conflicts": counters["date_conflicts"],
              "date_invalid_records": counters["date_invalid_records"],
              "date_usable_count": sum(bool(paper.get("publication_date")) and paper.get("date_precision") in {"day", "month"} for paper in output)}
    report.update(bibliography_summary(output))
    return output, report
