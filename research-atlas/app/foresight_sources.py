"""Evidence-anchored external searches for iterative research exploration.

Candidate terms are data, never executable API search syntax. Public adapters
retain their original citation provenance. Scopus credentials travel only in
headers to the documented Elsevier endpoint; Search does not provide reference
lists, and missing metadata is never synthesized.
"""
from __future__ import annotations

import copy
import os
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import httpx

from . import sources
from .bibliography import bibliography_summary, normalize_affiliations, normalize_doi
from .dates import normalize_publication_date
from .connection_settings import http_client


_SCOPUS_URL = "https://api.elsevier.com/content/search/scopus"
_SCOPUS_DOCS = "https://dev.elsevier.com/documentation/SCOPUSSearchAPI.wadl"
_SCOPUS_LOCK = threading.Lock()
_SCOPUS_LAST_REQUEST: float | None = None
_SCOPUS_INTERVAL = 0.3
_EMPTY_PUBLIC = "指定した公開 API・語句・期間で、取り込める論文が見つかりませんでした。"
_SAMPLE_WARNING = "候補テーマと根拠語に条件を付けた各年の関連度上位サンプルです。追加取得件数や年別件数だけから、分野全体の成長・年次比較・将来性を推定できません。"


def _credentials() -> tuple[str, str]:
    key = os.getenv("ELSEVIER_API_KEY", "").strip() or os.getenv("SCOPUS_API_KEY", "").strip()
    return key, os.getenv("ELSEVIER_INSTTOKEN", "").strip()


def catalog() -> list[dict]:
    """Available means configured, not a claim about live service entitlement."""
    providers = [{"id": item["id"], "name": item["name"], "available": True,
                  "reason": "API キー不要。サービス側の収録範囲と利用上限が適用されます。",
                  "docs_url": item["docs_url"]} for item in sources.catalog()]
    configured = bool(_credentials()[0])
    providers.append({"id": "scopus", "name": "Scopus", "available": configured,
                      "reason": "API キー設定済み。取得できる項目は所属機関・API の契約権限に依存します。" if configured else "ELSEVIER_API_KEY（または SCOPUS_API_KEY）が未設定です。検索式をコピーして手動検索できます。",
                      "docs_url": _SCOPUS_DOCS})
    return providers


def _terms(value, maximum: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result, seen = [], set()
    for item in value:
        if isinstance(item, dict):
            item = item.get("term") or item.get("text") or ""
        if not isinstance(item, str):
            continue
        # Remove all field/operators delimiters. The adapter itself owns syntax.
        normalized = unicodedata.normalize("NFKC", sources._text(item))
        normalized = " ".join("".join(c if c.isalnum() or c in " -" else " " for c in normalized).split())[:90].strip()
        key = normalized.casefold()
        if not key or key in {"and", "or", "not"} or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) == maximum:
            break
    return result


def _scope(assessment: dict) -> tuple[int, int]:
    meta = assessment.get("meta") if isinstance(assessment, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    values = [meta.get("start_year"), meta.get("end_year")]
    if any(isinstance(value, bool) or not re.fullmatch(r"\d{4}", str(value or "")) for value in values):
        raise ValueError("候補の再検索には、評価結果に有効な開始年・終了年が必要です。")
    start, end = map(int, values)
    if not 1500 <= start <= end <= date.today().year or end - start > 19:
        raise ValueError("候補の再検索期間は、現在年までの連続する 20 暦年以内で指定してください。")
    return start, end


def _initial_queries(assessment: dict) -> list[str]:
    """Only initial collection scope can anchor later searches, never a round."""
    result, seen = [], set()
    for report in assessment.get("meta", {}).get("source_reports", []) or []:
        if not isinstance(report, dict) or report.get("collection_purpose") == "candidate_refinement":
            continue
        request = report.get("discovery_request")
        value = request.get("query") if isinstance(request, dict) else None
        value = value if isinstance(value, str) and value.strip() else report.get("query")
        if not isinstance(value, str) or not value.strip():
            continue
        key = " ".join(value.split()).casefold()
        if key not in seen:
            result.append(value.strip())
            seen.add(key)
    return result


def _domain_tree(raw: str):
    """Parse a small portable Boolean grammar; never forward raw API syntax.

Words separated by spaces are mandatory AND terms; quoted phrases and explicit
AND/OR parentheses keep their meaning. Provider-specific fields, negation and
wildcards cannot safely be translated between these services, so reject them
instead of dropping mandatory restrictions or turning exclusions into keywords.
"""
    error = "元の公開検索式を安全に共通検索へ変換できません。通常の語句・二重引用符・AND/OR・丸括弧による検索条件を用意してください。フィールド指定・NOT・ワイルドカードを省略して検索を広げることはしていません。"
    tokens, cursor = [], 0
    for match in re.finditer(r'"[^"\\]*"|[()]|[^\s()"]+', raw):
        if raw[cursor:match.start()].strip():
            raise ValueError(error)
        cursor = match.end()
        token = match.group()
        if token in {"AND", "OR", "(", ")"}:
            tokens.append(token)
            continue
        quoted = token.startswith('"')
        value = token[1:-1] if quoted else token
        if not quoted and (value.upper() in {"NOT", "ANDNOT"} or re.search(r'[:*?\[\]{}\\]', value)
                           or cursor < len(raw) and raw[cursor] == "("):
            raise ValueError(error)
        terms = _terms([value], 1)
        if not terms or len(value) > 90:
            raise ValueError(error)
        tokens.append(("term", terms[0]))
    if raw[cursor:].strip() or not tokens or len(tokens) > 100:
        raise ValueError(error)
    position = 0

    def factor():
        nonlocal position
        if position >= len(tokens):
            raise ValueError(error)
        current = tokens[position]
        position += 1
        if current == "(":
            result = alternative()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError(error)
            position += 1
            return result
        if isinstance(current, tuple):
            return current
        raise ValueError(error)

    def conjunction():
        nonlocal position
        result = factor()
        while position < len(tokens) and tokens[position] not in {"OR", ")"}:
            if tokens[position] == "AND":
                position += 1
            result = ("AND", result, factor())
        return result

    def alternative():
        nonlocal position
        result = conjunction()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            result = ("OR", result, conjunction())
        return result

    result = alternative()
    if position != len(tokens):
        raise ValueError(error)
    return result


def _render_domain(tree, prefix="") -> str:
    if tree[0] == "term":
        return f'{prefix}"{tree[1]}"'
    def operands(node):
        if node[0] == tree[0]:
            return [*operands(node[1]), *operands(node[2])]
        return [_render_domain(node, prefix)]
    return "(" + f" {tree[0]} ".join(operands(tree)) + ")"


def _domain_terms(tree) -> list[str]:
    return [tree[1]] if tree[0] == "term" else [*_domain_terms(tree[1]), *_domain_terms(tree[2])]


def _domain_matches(paper, tree) -> bool:
    if tree[0] == "term":
        return _contains_seed(paper, [tree[1]])
    left, right = _domain_matches(paper, tree[1]), _domain_matches(paper, tree[2])
    return left and right if tree[0] == "AND" else left or right


def query_for_candidate(candidate: dict, assessment: dict) -> dict:
    """Build a reviewable query plan without making a request or exposing a key."""
    if not isinstance(candidate, dict):
        raise ValueError("再検索する研究候補の形式が不正です。")
    start, end = _scope(assessment)
    seeds = (_terms(candidate.get("seed_terms"), 2) or
             _terms(candidate.get("original_query"), 1) or
             _terms(candidate.get("keywords"), 2))
    if not seeds:
        raise ValueError("元の研究テーマを維持するための seed_terms または検索語がありません。")
    extras = [term for term in (_terms(candidate.get("terms"), 6) or _terms(candidate.get("keywords"), 6))
              if term.casefold() not in {seed.casefold() for seed in seeds}][:4]

    def group(terms, prefix=""):
        return "(" + " OR ".join(f'{prefix}"{term}"' for term in terms) + ")"

    groups = [seeds] + ([extras] if extras else [])
    generic = " AND ".join(group(terms) for terms in groups)
    scopus_terms = " AND ".join("TITLE-ABS-KEY" + group(terms) for terms in groups)
    queries = {"europepmc": generic, "arxiv": " AND ".join(group(terms, "all:") for terms in groups),
               "crossref": " ".join([*seeds, *extras]), "scopus": scopus_terms}
    initial_queries = _initial_queries(assessment)
    trees = [_domain_tree(value) for value in initial_queries]
    if trees:
        # A merged initial corpus can represent the union of several searches.
        # Keep that union, then intersect it with this candidate's updated terms.
        domain = "(" + " OR ".join(_render_domain(tree) for tree in trees) + ")"
        queries["europepmc"] = f"{domain} AND ({queries['europepmc']})"
        arxiv_domain = "(" + " OR ".join(_render_domain(tree, "all:") for tree in trees) + ")"
        queries["arxiv"] = f"{arxiv_domain} AND ({queries['arxiv']})"
        queries["scopus"] = f"TITLE-ABS-KEY{domain} AND ({queries['scopus']})"
        queries["crossref"] = " ".join(dict.fromkeys([*(term for tree in trees for term in _domain_terms(tree)), *seeds, *extras]))
    if any(len(value) > 1000 for value in queries.values()):
        raise ValueError("元の公開検索条件と候補語を合わせた検索式が長すぎます。元の必須条件を省略せず、検索範囲や候補語を絞ってください。")
    return {"seed_terms": seeds, "query_terms": extras, "start_year": start, "end_year": end,
            "queries": queries, "original_search_queries": initial_queries,
            "domain_anchor_applied": bool(trees),
            "domain_anchor_policy": "initial_queries_union_then_topic_intersection" if trees else "candidate_terms_only",
            "scopus_query": f"{queries['scopus']} AND PUBYEAR > {start - 1} AND PUBYEAR < {end + 1}"}


def _contains_seed(paper: dict, seeds: list[str]) -> bool:
    def comparable(value):
        return " ".join("".join(c if c.isalnum() else " " for c in unicodedata.normalize("NFKC", str(value)).casefold()).split())
    visible = comparable(" ".join([paper.get("title", ""), paper.get("abstract", ""), *paper.get("keywords", [])]))
    for seed in seeds:
        needle = comparable(seed)
        # Latin acronyms must not match the interior of unrelated words.
        if re.fullmatch(r"[a-z0-9 ]+", needle):
            if f" {needle} " in f" {visible} ":
                return True
        elif needle in visible:
            return True
    return False


def _value(value) -> str:
    if isinstance(value, dict):
        value = value.get("$", "")
    return sources._text(value)


def _list(value) -> list:
    return value if isinstance(value, list) else ([value] if value is not None else [])


def _scopus_paper(record: dict, requested_year: int, retrieved_at: str) -> dict:
    if not isinstance(record, dict):
        raise RuntimeError("Scopus の論文メタデータ形式を読み取れません。取り込みを中止しました。")
    eid = _value(record.get("eid"))
    eid = eid if re.fullmatch(r"2-s2\.0-\d+", eid) else ""
    identifier = re.sub(r"^SCOPUS_ID:", "", _value(record.get("dc:identifier")), flags=re.I)
    identifier = identifier if identifier.isdigit() else ""
    title = _value(record.get("dc:title"))
    cover_date = _value(record.get("prism:coverDate"))
    year = sources._year(cover_date)
    if not (eid or identifier) or not title or year != requested_year:
        raise RuntimeError("Scopus の論文 ID・タイトル・公開年が不足、または検索年と矛盾しています。取り込みを中止しました。")
    dates = normalize_publication_date(cover_date, year)
    affiliation_by_id, affiliations = {}, []
    for item in _list(record.get("affiliation")):
        if not isinstance(item, dict):
            continue
        name = ", ".join(part for key in ("affilname", "affiliation-city", "affiliation-country") if (part := _value(item.get(key))))
        cleaned = normalize_affiliations(name)
        affiliations.extend(cleaned)
        if aid := _value(item.get("afid")):
            affiliation_by_id[aid] = cleaned
    authors = []
    for item in _list(record.get("author")):
        if not isinstance(item, dict):
            continue
        name = " ".join(part for key in ("given-name", "surname") if (part := _value(item.get(key)))) or _value(item.get("authname"))
        personal_affiliations = [affiliation for aid in _list(item.get("afid")) for affiliation in affiliation_by_id.get(_value(aid), [])]
        author = sources._author(name, _value(item.get("orcid")), personal_affiliations)
        if author:
            authid = _value(item.get("authid"))
            if authid.isdigit():
                if author["id"].startswith("orcid:"):
                    author["aliases"] = [author["id"]]
                author["id"] = f"scopus:{authid}"
            authors.append(author)
    author_scope = "provided_author_list" if authors else "not_provided"
    if not authors and (creator := _value(record.get("dc:creator"))):
        authors = [sources._author(creator)]
        author_scope = "first_author_only"
    authors = sources._authors(authors)
    raw_keywords = record.get("authkeywords")
    keywords = [term.strip() for value in _list(raw_keywords) for term in re.split(r"[;|]", _value(value)) if term.strip()]
    doi = normalize_doi(_value(record.get("prism:doi")))
    citations = sources._count(record.get("citedby-count"))
    external_url = ""
    for link in _list(record.get("link")):
        if not isinstance(link, dict) or link.get("@ref") != "scopus":
            continue
        value = _value(link.get("@href"))
        try:
            parsed = urlsplit(value)
            if parsed.scheme == "https" and parsed.hostname in {"scopus.com", "www.scopus.com"} and not parsed.username:
                external_url = value
        except ValueError:
            pass
    if not external_url and doi:
        external_url = "https://doi.org/" + doi
    return {"id": eid or f"scopus:{identifier}", "title": title, "abstract": _value(record.get("dc:description")),
            "year": year, "authors": authors, "author_metadata_scope": author_scope,
            "keywords": list(dict.fromkeys(keywords)), "affiliations": normalize_affiliations(affiliations),
            "references": [], "references_status": "not_provided", "citations": citations,
            "doi": doi, "source": _value(record.get("prism:publicationName")), "citation_history": {},
            "aliases": {"eids": [eid] if eid else [], "dois": [doi] if doi else [], "ids": [f"scopus:{identifier}"] if identifier else []},
            "provenance": "public-api:scopus:v1", "providers": ["scopus"], "external_url": external_url,
            "citation_source": "scopus", "citation_snapshots": [{"provider": "scopus", "count": citations, "retrieved_at": retrieved_at}] if citations is not None else [],
            "retrieved_at": retrieved_at, "publication_date": dates["publication_date"], "date_precision": dates["date_precision"],
            "date_source": "scopus:prism:coverDate (API cover-date precision)", "date_warnings": dates["warnings"]}


def _make_scopus_client() -> httpx.Client:
    return http_client(_SCOPUS_URL, timeout=httpx.Timeout(40, connect=15),
                        headers={"User-Agent": "ResearchAtlas/1.0 (local bibliographic research client)", "Accept": "application/json"})


def _scopus_request(client, params, headers):
    global _SCOPUS_LAST_REQUEST
    with _SCOPUS_LOCK:
        if _SCOPUS_LAST_REQUEST is not None:
            delay = _SCOPUS_INTERVAL - (time.monotonic() - _SCOPUS_LAST_REQUEST)
            if delay > 0:
                time.sleep(delay)
        try:
            response = client.get(_SCOPUS_URL, params=params, headers=headers)
        except httpx.TimeoutException:
            raise RuntimeError("Scopus の応答がタイムアウトしました。時間をおいて再試行してください。取得途中の結果は保存していません。") from None
        except httpx.RequestError:
            raise RuntimeError("Scopus に接続できません。ネットワーク接続を確認してください。取得途中の結果は保存していません。") from None
        finally:
            _SCOPUS_LAST_REQUEST = time.monotonic()
    if response.status_code == 403 and params.get("view") == "COMPLETE":
        return None
    if response.status_code in {401, 403}:
        raise RuntimeError("Scopus の認証または契約権限を確認できません。API キー・接続元機関・Insttoken の設定を確認してください。")
    if response.status_code == 429:
        raise RuntimeError("Scopus API のアクセス上限に達しました。時間をおいて再試行してください。取得途中の結果は保存していません。")
    if response.status_code in {400, 422}:
        raise ValueError("Scopus が検索式を受け付けませんでした。候補の検索語を確認してください。")
    if response.status_code != 200:
        raise RuntimeError(f"Scopus から取得できませんでした（HTTP {response.status_code}）。取得途中の結果は保存していません。")
    if len(response.content) > 20 * 1024 * 1024:
        raise RuntimeError("Scopus の応答が大きすぎます。取得上限を小さくしてください。")
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("Scopus の応答を読み取れません。取り込みを中止しました。") from None
    if not isinstance(data, dict) or not isinstance(data.get("search-results"), dict):
        raise RuntimeError("Scopus の検索応答形式が不正です。取り込みを中止しました。")
    return data["search-results"]


def _scopus_discover(plan, limit, progress=None):
    key, token = _credentials()
    if not key:
        raise ValueError("Scopus API キーが未設定です。ELSEVIER_API_KEY（または SCOPUS_API_KEY）を設定するか、表示された検索式で手動検索してください。")
    if any(not value.isascii() or any(ord(character) < 32 or ord(character) == 127 for character in value) for value in (key, token)):
        raise ValueError("Scopus API 認証設定に不正な文字が含まれています。キーと Insttoken の設定を確認してください。")
    headers = {"X-ELS-APIKey": key, **({"X-ELS-Insttoken": token} if token else {})}
    retrieved_at = datetime.now(timezone.utc).isoformat()
    years = list(range(plan["start_year"], plan["end_year"] + 1))
    quota, remainder = divmod(limit, len(years))
    papers, coverage, warnings, seen, queries = [], [], [], set(), []
    view, retrieved, duplicates = "COMPLETE", 0, 0
    with _make_scopus_client() as client:
        for index, year in enumerate(years):
            allocation = quota + (index < remainder)
            if not allocation:
                coverage.append({"year": year, "total": None, "imported": 0, "allocated": 0})
                continue
            query = f'{plan["queries"]["scopus"]} AND PUBYEAR > {year - 1} AND PUBYEAR < {year + 1}'
            queries.append({"year": year, "query": query})
            offset, imported, total = 0, 0, None
            while offset < allocation and (total is None or offset < total):
                if progress:
                    progress(f"Scopus: {year} 年の候補文献を取得しています（{offset}/{allocation} 件）。")
                params = {"query": query, "count": min(25, allocation - offset), "start": offset,
                          "view": view, "sort": "-relevancy", "content": "core"}
                data = _scopus_request(client, params, headers)
                if data is None:
                    view = "STANDARD"
                    warnings.append("Scopus COMPLETE の取得権限がないため STANDARD の書誌情報を取得しました。抄録・全著者・著者キーワードは欠ける場合があります。")
                    data = _scopus_request(client, {**params, "view": view}, headers)
                total = sources._count(data.get("opensearch:totalResults"), required=True)
                records = _list(data.get("entry"))
                if total == 0:
                    break
                if not records or any(not isinstance(item, dict) or "error" in item for item in records):
                    raise RuntimeError("Scopus の検索件数と論文一覧が一致しません。取り込みを中止しました。")
                records = records[:params["count"]]
                for record in records:
                    paper = _scopus_paper(record, year, retrieved_at)
                    retrieved += 1
                    if paper["id"] in seen:
                        duplicates += 1
                        continue
                    seen.add(paper["id"])
                    papers.append(paper)
                    imported += 1
                offset += len(records)
            coverage.append({"year": year, "total": total, "imported": imported, "allocated": allocation})
    warnings.append("Scopus Search API では実参照リストを取得していません。References と年別引用履歴は未取得のまま保持します。引用数は取得時点の Scopus 累積値です。")
    missing_abstracts = sum(not paper["abstract"] for paper in papers)
    if missing_abstracts:
        warnings.append(f"抄録が提供されていない文献が {missing_abstracts} 件あります。内容分析にはタイトル・提供キーワードだけを利用できます。")
    if plan["end_year"] == date.today().year:
        warnings.append("今年は未完了の年です。過去年との件数・引用数は同じ観測期間ではありません。")
    if any(paper["author_metadata_scope"] == "first_author_only" for paper in papers):
        warnings.append("一部の文献は筆頭著者名だけが取得できました。共著ネットワークは不完全です。")
    if any(author["id"].startswith("name:") for paper in papers for author in paper["authors"]):
        warnings.append("著者 ID が未取得の場合は正規化した名前で照合します。同姓同名の識別は保証できません。")
    if any(item["allocated"] == 0 for item in coverage):
        warnings.append("取得上限が年数より少ないため、一部の年は未検索です。未検索年の件数は不明として保持します。")
    return papers, {"provider": "scopus", "retrieved_at": retrieved_at, "year_coverage": coverage,
                    "month_coverage": [], "retrieved_count": retrieved, "imported_count": len(papers),
                    "duplicates_removed": duplicates, "invalid_rows": 0, "view": view, "executed_queries": queries,
                    "truncated": any(item["total"] is None or item["total"] > item["imported"] for item in coverage),
                    "warnings": warnings, "sampling": "per_year_relevance_top", "citation_history_available": False,
                    "date_pipeline_version": 2, "metadata_pipeline_version": 3, **bibliography_summary(papers)}


def discover_for_candidate(candidate: dict, assessment: dict, provider: str, limit: int = 40, progress=None) -> tuple[list[dict], dict]:
    """Retrieve at most 100 real records; root owns round limits and merge policy."""
    if provider not in {item["id"] for item in catalog()}:
        raise ValueError("再検索サービスは Europe PMC・arXiv・Crossref・Scopus から選んでください。")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("候補ごとの外部取得上限は 1〜100 件で指定してください。")
    plan = query_for_candidate(candidate, assessment)
    query = plan["scopus_query"] if provider == "scopus" else plan["queries"][provider]
    if provider == "scopus":
        papers, report = _scopus_discover(plan, limit, progress)
    else:
        try:
            papers, report = sources.discover(provider, query, plan["start_year"], plan["end_year"], limit, progress=progress)
        except ValueError as exc:
            if not str(exc).startswith(_EMPTY_PUBLIC):
                raise
            # The existing adapter does not return its coverage on zero results.
            # Do not convert unknown hit counts or metadata rejection into zero.
            papers, report = [], {"retrieved_at": datetime.now(timezone.utc).isoformat(), "year_coverage": [],
                                  "retrieved_count": 0, "imported_count": 0, "truncated": None, "month_coverage": [],
                                  "warnings": ["取り込める文献がありませんでした。空結果時の API 総件数・年別内訳は取得できていません。"],
                                  "hit_count_status": "unavailable_on_empty_search"}
    papers, report = copy.deepcopy(papers), copy.deepcopy(report)
    before_filter_count = len(papers)
    domain_trees = [_domain_tree(value) for value in plan["original_search_queries"]]
    domain_papers = [paper for paper in papers if not domain_trees or any(_domain_matches(paper, tree) for tree in domain_trees)]
    excluded_domain = len(papers) - len(domain_papers)
    papers = domain_papers
    if provider == "crossref":
        papers = [paper for paper in papers if _contains_seed(paper, plan["seed_terms"])]
        report.setdefault("warnings", []).append("Crossref は厳密な必須語検索に対応しないため、可視タイトル・抄録・キーワードが初期検索の必須条件と候補テーマ語を満たす文献だけ採用しました。抄録未登録などによる見落としを含みます。")
    excluded = before_filter_count - len(papers)
    if domain_trees or provider == "crossref":
        for item in report.get("year_coverage", []):
            item["retrieved_before_theme_filter"] = item["imported"]
            item["imported"] = sum(paper["year"] == item["year"] for paper in papers)
    if excluded:
        report["truncated"] = True
    report.update({"provider": provider, "query": query, "scopus_query": plan["scopus_query"],
                   "seed_terms": plan["seed_terms"], "query_terms": plan["query_terms"],
                   "original_search_queries": plan["original_search_queries"], "domain_anchor_applied": plan["domain_anchor_applied"],
                   "domain_anchor_policy": plan["domain_anchor_policy"], "filtered_outside_initial_scope": excluded_domain,
                   "visible_scope_verification": {"applied": bool(domain_trees),
                       "fields": ["title", "abstract", "keywords"], "method": "normalized_terms_and_phrases_preserving_initial_boolean_query",
                       "checked_count": before_filter_count if domain_trees else 0,
                       "passed_count": len(domain_papers) if domain_trees else None, "rejected_count": excluded_domain},
                   "candidate_id": str(candidate.get("id") or ""), "candidate_label": str(candidate.get("label") or ""),
                   "start_year": plan["start_year"], "end_year": plan["end_year"], "limit": limit,
                   "imported_count": len(papers), "filtered_off_theme": excluded,
                   "status": "completed" if papers else "no_results", "collection_purpose": "candidate_refinement",
                   "annual_comparison_justified": False, "is_demo": False, "synthetic_count": 0,
                   "date_pipeline_version": 2, **bibliography_summary(papers),
                   "date_usable_count": sum(paper.get("date_precision") in {"day", "month"} for paper in papers)})
    report["warnings"] = list(dict.fromkeys([*report.get("warnings", []), _SAMPLE_WARNING]))
    if plan["domain_anchor_applied"]:
        report["warnings"].append("元の公開検索の語句を必須条件として保持し、その範囲と候補語の両方を満たす検索です。空白区切りの元語句は AND、引用句・明示 OR はその条件を保持します。")
        report["warnings"].append("API の検索一致と可視書誌の一致は別です。本文・参照中の言及だけで分野外の文献が混ざらないよう、タイトル・抄録・キーワードでも初期検索条件を検証してから採用します。抄録欠測・同義語・略語・語形変化による見落としがあり、採用は主題や結論の科学的確認を意味しません。API 取得数とこの確認後の採用数を分けて記録します。")
    else:
        report["warnings"].append("初期資料の公開検索式が保存されていないため、候補語だけで検索しています。元の収集分野との一致は未確認です。")
    return papers, report
