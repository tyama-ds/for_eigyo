"""Key-free discovery through documented, fixed public bibliographic APIs.

Every year receives an equal retrieval budget. These are relevance-ranked
samples, never an unbiased publication census or inferred citation histories.
"""
from __future__ import annotations

import copy
import calendar
import hashlib
import re
import shlex
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote

import httpx

from .dates import normalize_publication_date
from .connection_settings import http_client
from .bibliography import bibliography_summary, normalize_affiliations, normalize_references, normalize_reference_id


PROVIDERS = [
    {"id": "europepmc", "name": "Europe PMC", "description": "生命科学・医学を中心に、関連分野の論文とプレプリントを検索。提供される抄録も取得します。", "limitations": "収録分野に偏りがあります。年は最初の公開日を優先。引用数は Europe PMC 内の累積値で、Scopus の値とは異なります。", "docs_url": "https://europepmc.org/RestfulWebService"},
    {"id": "arxiv", "name": "arXiv", "description": "物理・情報・数学などのプレプリントを検索。通常の語句は AND 検索、ti:・all:・cat: などの API 検索式にも対応します。", "limitations": "査読前の研究を含みます。年は初回投稿日です。API は被引用数を提供しません。リクエストは 3 秒以上の間隔で処理します。", "docs_url": "https://info.arxiv.org/help/api/user-manual.html"},
    {"id": "crossref", "name": "Crossref", "description": "分野横断の DOI 登録メタデータを検索。雑誌論文・会議論文・書籍・プレプリント等を含みます。", "limitations": "抄録・著者情報が未登録の文献があります。関連度検索は語句の厳密一致ではありません。引用数は Crossref に登録された参照に基づく累積値です。", "docs_url": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/"},
]
_ENDPOINTS = {
    "europepmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
    "arxiv": "https://export.arxiv.org/api/query",
    "crossref": "https://api.crossref.org/works",
}
_NAMES = {provider["id"]: provider["name"] for provider in PROVIDERS}
_ARXIV_LOCK = threading.Lock()
_ARXIV_LAST_REQUEST: float | None = None
_ARXIV_INTERVAL = 3.0
_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom", "open": "http://a9.com/-/spec/opensearch/1.1/"}


def catalog() -> list[dict]:
    return copy.deepcopy(PROVIDERS)


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "br", "div", "sec", "title", "li", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "div", "sec", "title", "li", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def _text(value) -> str:
    if value is None:
        return ""
    parser = _PlainText()
    parser.feed(str(value))
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def _doi(value) -> str:
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", _text(value), flags=re.I).strip().casefold()


def _count(value, *, required=False) -> int | None:
    if value is None or value == "":
        if required:
            raise RuntimeError("公開 API の応答に検索件数がありません。時間をおいて再試行してください。")
        return None
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value).strip()):
        raise RuntimeError("公開 API が不正な件数を返しました。取り込みを中止しました。")
    return int(value)


def _author(name, orcid=None, affiliations=None) -> dict | None:
    name = _text(name)
    if not name:
        return None
    match = re.search(r"(?:^|/)(\d{4}-\d{4}-\d{4}-\d{3}[\dX])$", _text(orcid).upper())
    if match:
        identifier = "orcid:" + match.group(1)
    else:
        # Provider-neutral exact-name identity. This is not person disambiguation.
        normalized = "".join(character for character in unicodedata.normalize("NFKC", name).casefold() if character.isalnum())
        identifier = "name:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    result = {"id": identifier, "name": name}
    if normalized := normalize_affiliations(affiliations):
        result["affiliations"] = normalized
    return result


def _authors(values: list[dict | None]) -> list[dict]:
    unique = {}
    for author in values:
        if author:
            if author["id"] not in unique:
                unique[author["id"]] = copy.deepcopy(author)
            elif author.get("affiliations"):
                previous = unique[author["id"]]
                previous["affiliations"] = normalize_affiliations([*previous.get("affiliations", []), *author["affiliations"]])
    return list(unique.values())


def _make_client(provider: str = "europepmc") -> httpx.Client:
    return http_client(_ENDPOINTS[provider], timeout=httpx.Timeout(45.0, connect=15.0),
                        headers={"User-Agent": "ResearchAtlas/1.0 (local bibliographic research client)", "Accept": "application/json, application/atom+xml, application/xml"})


def _request(client: httpx.Client, provider: str, params: dict) -> httpx.Response:
    global _ARXIV_LAST_REQUEST

    def send():
        try:
            response = client.get(_ENDPOINTS[provider], params=params)
        except httpx.TimeoutException:
            raise RuntimeError(f"{_NAMES[provider]} の応答がタイムアウトしました。検索範囲を絞るか、時間をおいて再試行してください。取得途中の結果は保存していません。") from None
        except httpx.RequestError:
            raise RuntimeError(f"{_NAMES[provider]} に接続できません。ネットワーク接続を確認してください。取得途中の結果は保存していません。") from None
        if response.status_code == 429:
            raise RuntimeError(f"{_NAMES[provider]} のアクセス上限に達しました。しばらく待って再試行してください。取得途中の結果は保存していません。")
        if response.status_code in {400, 422}:
            raise ValueError(f"{_NAMES[provider]} が検索式を受け付けませんでした。語句と検索記法を確認してください。")
        if response.status_code != 200:
            raise RuntimeError(f"{_NAMES[provider]} からデータを取得できませんでした（HTTP {response.status_code}）。時間をおいて再試行してください。取得途中の結果は保存していません。")
        if len(response.content) > 20 * 1024 * 1024:
            raise RuntimeError(f"{_NAMES[provider]} の応答が大きすぎます。取得上限を小さくしてください。")
        return response

    if provider != "arxiv":
        return send()
    # Serialize both the delay and the request, including simultaneous discovery jobs.
    with _ARXIV_LOCK:
        if _ARXIV_LAST_REQUEST is not None:
            delay = _ARXIV_INTERVAL - (time.monotonic() - _ARXIV_LAST_REQUEST)
            if delay > 0:
                time.sleep(delay)
        try:
            return send()
        finally:
            _ARXIV_LAST_REQUEST = time.monotonic()


def _json(response: httpx.Response, provider: str) -> dict:
    try:
        result = response.json()
    except ValueError:
        raise RuntimeError(f"{_NAMES[provider]} の応答形式を読み取れません。取り込みを中止しました。") from None
    if not isinstance(result, dict):
        raise RuntimeError(f"{_NAMES[provider]} の応答形式が不正です。取り込みを中止しました。")
    return result


def _base(provider: str, identifier: str, title, abstract, year, authors, keywords, citations, doi, source, external_url, retrieved_at, publication_date=None, date_source="", affiliations=None, references=None) -> dict:
    title = _text(title)
    if not identifier or not title or not isinstance(year, int) or not 1500 <= year <= date.today().year:
        raise RuntimeError(f"{_NAMES[provider]} の論文メタデータに ID・タイトル・有効な公開年が不足しています。取り込みを中止しました。")
    doi = _doi(doi)
    count = _count(citations)
    dates = normalize_publication_date(publication_date, year)
    normalized_authors = _authors(authors)
    return {"id": f"{provider}:{identifier}", "title": title, "abstract": _text(abstract), "year": year,
            "authors": normalized_authors, "keywords": list(dict.fromkeys(term for item in keywords if (term := _text(item)))),
            "affiliations": normalize_affiliations([*normalize_affiliations(affiliations),
                *(item for author in normalized_authors for item in author.get("affiliations", []))]),
            "references": normalize_references(references),
            "references_status": "provided" if references is not None else "not_provided",
            "citations": count, "doi": doi, "source": _text(source), "citation_history": {},
            "aliases": {"eids": [], "dois": [doi] if doi else []}, "provenance": f"public-api:{provider}:v1",
            "providers": [provider], "external_url": external_url, "citation_source": provider,
            "citation_snapshots": [{"provider": provider, "count": count, "retrieved_at": retrieved_at}] if count is not None else [],
            "retrieved_at": retrieved_at, "publication_date": dates["publication_date"],
            "date_precision": dates["date_precision"], "date_source": date_source or f"{provider}:year-only",
            "date_warnings": dates["warnings"]}


def _year(value) -> int | None:
    match = re.match(r"^(\d{4})(?:\D|$)", str(value or ""))
    return int(match.group(1)) if match else None


def _europepmc(client, query, year, allocation, retrieved_at, from_date=None, to_date=None):
    first, last = from_date or f"{year}-01-01", to_date or f"{year}-12-31"
    response = _request(client, "europepmc", {"query": f"({query}) AND FIRST_PDATE:[{first} TO {last}]", "format": "json", "resultType": "core", "pageSize": max(1, allocation), "cursorMark": "*"})
    data = _json(response, "europepmc")
    total = _count(data.get("hitCount"), required=True)
    records = data.get("resultList", {}).get("result")
    if not isinstance(records, list):
        raise RuntimeError("Europe PMC の論文一覧を読み取れません。取り込みを中止しました。")
    papers = []
    for record in records[:allocation]:
        identifier = _text(record.get("id"))
        source_id = _text(record.get("source"))
        authors = []
        for author in (record.get("authorList") or {}).get("author", []) or []:
            name = " ".join(part for part in [_text(author.get("firstName")), _text(author.get("lastName"))] if part) or author.get("fullName") or author.get("collectiveName")
            aid = author.get("authorId") or {}
            affiliations = (author.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or []
            authors.append(_author(name, aid.get("value") if str(aid.get("type", "")).upper() == "ORCID" else None,
                                   affiliations=normalize_affiliations(affiliations) + normalize_affiliations(author.get("affiliation"))))
        journal = (record.get("journalInfo") or {}).get("journal") or {}
        keywords = (record.get("keywordList") or {}).get("keyword") or []
        if not isinstance(keywords, list):
            keywords = [keywords]
        if not identifier or not source_id:
            raise RuntimeError("Europe PMC の文献識別子が欠損しています。取り込みを中止しました。")
        papers.append(_base("europepmc", source_id + ":" + identifier, record.get("title"), record.get("abstractText"), _year(record.get("firstPublicationDate")) or _year(record.get("pubYear")), authors, keywords, record.get("citedByCount"), record.get("doi"), journal.get("title") or (record.get("bookOrReportDetails") or {}).get("publisher"), f"https://europepmc.org/article/{quote(source_id, safe='')}/{quote(identifier, safe='')}", retrieved_at,
                            publication_date=record.get("firstPublicationDate") or record.get("pubYear"),
                            date_source="europepmc:firstPublicationDate (提供元が欠損月日を補完する場合があり、原典の日付精度は未検証)" if record.get("firstPublicationDate") else "europepmc:pubYear",
                            affiliations=record.get("affiliation")))
        papers[-1]["aliases"]["ids"] = [identifier for value in [
            f"pmid:{record['pmid']}" if record.get("pmid") else "",
            record.get("pmcid") or ""] if (identifier := normalize_reference_id(value))]
    return papers, total


def _crossref(client, query, year, allocation, retrieved_at, from_date=None, to_date=None):
    first, last = from_date or f"{year}-01-01", to_date or f"{year}-12-31"
    response = _request(client, "crossref", {"query": query, "filter": f"from-pub-date:{first},until-pub-date:{last}", "rows": allocation, "sort": "relevance", "order": "desc"})
    data = _json(response, "crossref")
    message = data.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("items"), list):
        raise RuntimeError("Crossref の論文一覧を読み取れません。取り込みを中止しました。")
    total = _count(message.get("total-results"), required=True)
    papers = []
    for record in message["items"][:allocation]:
        doi = _doi(record.get("DOI"))
        titles = record.get("title") or []
        title = titles[0] if isinstance(titles, list) and titles else titles if isinstance(titles, str) else ""
        pub_year, pub_date, date_source = None, None, "crossref:year-only"
        for field in ("published", "issued", "published-online", "published-print", "posted"):
            parts = (record.get(field) or {}).get("date-parts") or []
            if parts and isinstance(parts[0], list) and parts[0]:
                pub_year = _year(parts[0][0])
                if pub_year:
                    pub_date, date_source = parts[0], "crossref:" + field
                    break
        authors = [_author(" ".join(part for part in [_text(a.get("given")), _text(a.get("family"))] if part) or a.get("name"), a.get("ORCID"), affiliations=a.get("affiliation")) for a in record.get("author", []) or []]
        containers = record.get("container-title") or []
        source = containers[0] if isinstance(containers, list) and containers else containers if isinstance(containers, str) else record.get("publisher")
        papers.append(_base("crossref", doi, title, record.get("abstract"), pub_year, authors, record.get("subject") or [], record.get("is-referenced-by-count"), doi, source, "https://doi.org/" + quote(doi, safe="/"), retrieved_at, publication_date=pub_date, date_source=date_source,
                            references=record.get("reference")))
    return papers, total


def _arxiv_query(query: str) -> str:
    if re.search(r"\b(?:all|ti|au|abs|cat|id|jr|rn|co|submittedDate|lastUpdatedDate):", query):
        return query
    try:
        terms = shlex.split(query)
    except ValueError:
        raise ValueError("arXiv の検索語句の引用符が閉じていません。") from None
    if not terms:
        raise ValueError("検索語句を入力してください。")
    return " AND ".join('all:"' + term.replace('"', '\\"') + '"' for term in terms)


def _arxiv(client, query, year, allocation, retrieved_at, from_date=None, to_date=None):
    first = (from_date or f"{year}-01-01").replace("-", "") + "0000"
    last = (to_date or f"{year}-12-31").replace("-", "") + "2359"
    response = _request(client, "arxiv", {"search_query": f"({_arxiv_query(query)}) AND submittedDate:[{first} TO {last}]", "start": 0, "max_results": max(1, allocation), "sortBy": "relevance", "sortOrder": "descending"})
    if b"<!DOCTYPE" in response.content.upper() or b"<!ENTITY" in response.content.upper():
        raise RuntimeError("arXiv の XML 応答形式が不正です。取り込みを中止しました。")
    try:
        feed = ET.fromstring(response.content)
    except ET.ParseError:
        raise RuntimeError("arXiv の XML 応答を読み取れません。取り込みを中止しました。") from None
    entries = feed.findall("atom:entry", _NS)
    if any("api/errors" in (entry.findtext("atom:id", default="", namespaces=_NS)) for entry in entries):
        raise ValueError("arXiv が検索式のエラーを返しました。語句と検索記法を確認してください。")
    total = _count(feed.findtext("open:totalResults", default=None, namespaces=_NS), required=True)
    papers = []
    for entry in entries[:allocation]:
        raw_id = entry.findtext("atom:id", default="", namespaces=_NS)
        identifier = re.sub(r"v\d+$", "", re.sub(r"^https?://(?:export\.)?arxiv\.org/abs/", "", raw_id))
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z0-9.-]+/\d{7})", identifier):
            raise RuntimeError("arXiv の文献識別子を読み取れません。取り込みを中止しました。")
        authors = [_author(author.findtext("atom:name", default="", namespaces=_NS), author.findtext("arxiv:orcid", default=None, namespaces=_NS),
                           affiliations=["".join(item.itertext()) for item in author.findall("arxiv:affiliation", _NS)]) for author in entry.findall("atom:author", _NS)]
        papers.append(_base("arxiv", identifier, entry.findtext("atom:title", namespaces=_NS), entry.findtext("atom:summary", namespaces=_NS), _year(entry.findtext("atom:published", namespaces=_NS)), authors, [category.get("term", "") for category in entry.findall("atom:category", _NS)], None, entry.findtext("arxiv:doi", namespaces=_NS), "arXiv (preprint)", "https://arxiv.org/abs/" + quote(identifier, safe="/"), retrieved_at, publication_date=entry.findtext("atom:published", namespaces=_NS), date_source="arxiv:published (初回投稿日)"))
    return papers, total


def _periods(start_year, end_year, start_month, end_month):
    if start_month is None and end_month is None:
        return [{"year": year, "month": None, "first": f"{year}-01-01", "last": f"{year}-12-31"} for year in range(start_year, end_year + 1)]
    if any(not isinstance(value, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", value) for value in (start_month, end_month)):
        raise ValueError("月別検索では開始月と終了月の両方を YYYY-MM 形式で指定してください。")
    if int(start_month[:4]) != start_year or int(end_month[:4]) != end_year:
        raise ValueError("開始月・終了月の年は、開始年・終了年と一致させてください。")
    if start_month > end_month:
        raise ValueError("開始月は終了月以前にしてください。")
    today = date.today()
    if end_month > today.strftime("%Y-%m"):
        raise ValueError("未来の月は検索できません。終了月を今月以前にしてください。")
    first_index = int(start_month[:4]) * 12 + int(start_month[5:]) - 1
    last_index = int(end_month[:4]) * 12 + int(end_month[5:]) - 1
    if last_index - first_index >= 24:
        raise ValueError("月別検索は連続する 24 か月以内で指定してください。")
    result = []
    for index in range(first_index, last_index + 1):
        year, month0 = divmod(index, 12)
        month = month0 + 1
        last_day = min(date(year, month, calendar.monthrange(year, month)[1]), today)
        result.append({"year": year, "month": f"{year:04d}-{month:02d}", "first": f"{year:04d}-{month:02d}-01", "last": last_day.isoformat()})
    return result


def discover(provider: str, query: str, start_year: int, end_year: int, limit: int = 250, progress=None,
             start_month: str | None = None, end_month: str | None = None) -> tuple[list[dict], dict]:
    """Return requested year/month samples or raise; never hide a failed period."""
    if provider not in _ENDPOINTS:
        raise ValueError("公開データ提供元を Europe PMC・arXiv・Crossref から選択してください。")
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError("検索語句は 1〜1,000 文字で入力してください。")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start_year, end_year, limit)):
        raise ValueError("開始年・終了年・取得上限は整数で指定してください。")
    if not 1500 <= start_year <= end_year <= date.today().year or end_year - start_year >= 20:
        raise ValueError("検索期間は 1500 年以降から今年までの、連続する 20 年以内で指定してください。")
    if not 1 <= limit <= 1000:
        raise ValueError("取得上限は全期間の合計で 1〜1,000 件にしてください。")
    periods = _periods(start_year, end_year, start_month, end_month)
    monthly = start_month is not None
    query = query.strip()
    retrieved_at = datetime.now(timezone.utc).isoformat()
    base, remainder = divmod(limit, len(periods))
    budgets = [base + (index < remainder) for index in range(len(periods))]
    loader = {"europepmc": _europepmc, "crossref": _crossref, "arxiv": _arxiv}[provider]
    papers, coverage, seen = [], [], set()
    duplicates = 0
    with _make_client(provider) as client:
        for period, allocation in zip(periods, budgets):
            year = period["year"]
            label = period["month"] if monthly else f"{year} 年"
            if progress:
                progress(f"{_NAMES[provider]}：{label} の関連度上位を取得中（上限 {allocation} 件）")
            try:
                if monthly:
                    incoming, total = loader(client, query, year, allocation, retrieved_at, from_date=period["first"], to_date=period["last"])
                else:
                    incoming, total = loader(client, query, year, allocation, retrieved_at)
            except (ValueError, RuntimeError):
                raise
            except (KeyError, TypeError, AttributeError, IndexError):
                raise RuntimeError(f"{_NAMES[provider]} の {label} のメタデータ形式が不正です。取り込みを中止しました。") from None
            count = 0
            for paper in incoming:
                if paper["year"] != year:
                    raise RuntimeError(f"{_NAMES[provider]} が検索年と異なる公開年の文献を返しました。年別集計を混同しないため取り込みを中止しました。")
                if monthly and paper.get("publication_date") and paper["publication_date"][:7] != period["month"]:
                    paper["publication_date"], paper["date_precision"] = "", "year"
                    paper.setdefault("date_warnings", []).append("API の検索月と提供された公開月が矛盾するため、既存の出版年を保持して月次分析の対象外にしました。検索枠の月を公開日として補完していません。")
                if paper["id"] in seen:
                    duplicates += 1
                    continue
                seen.add(paper["id"])
                papers.append(paper)
                count += 1
            coverage.append({"month" if monthly else "year": period["month"] if monthly else year, "total": total, "imported": count, "allocated": allocation})
    if not papers:
        raise ValueError("指定した公開 API・語句・期間で、取り込める論文が見つかりませんでした。検索条件を変更してください。")
    unit = "月" if monthly else "年"
    warnings = [f"各{unit}に取得上限を配分した関連度上位のサンプルです。取り込んだ件数の増減は、分野全体の論文数・研究動向を表しません。母集団のトレンドとして解釈しないでください。",
                f"各{unit}の総ヒット件数は提供 API の検索結果件数です。検索記法・収録範囲・重複・更新時期が異なるため、サービス間の単純比較には使えません。",
                "年別に新たに受けた引用数は提供されていません。累積引用数から引用履歴を作成していません。",
                next(item["limitations"] for item in PROVIDERS if item["id"] == provider)]
    if monthly:
        warnings.append("月枠へ件数を配分した標本から、母集団の月次増加率や減少率を推論できません。年別カバー率の総件数も、指定した月の API ヒット件数を合計した値です。")
    if provider == "europepmc":
        warnings.append("Europe PMC の firstPublicationDate は、元資料で欠けている月日を提供元が補完する場合があります。API が示す日付を保持していますが、原典の日付精度は未検証です。")
    date_warnings = list(dict.fromkeys(warning for paper in papers for warning in paper.get("date_warnings", [])))
    warnings.extend(date_warnings)
    date_usable_count = sum(bool(paper.get("publication_date")) and paper.get("date_precision") in {"day", "month"} for paper in papers)
    if date_usable_count < len(papers):
        warnings.append(f"公開月を確定できない {len(papers) - date_usable_count} 件は、年次分析にのみ利用します。月初・年初の日付を補っていません。")
    name_authors = sum(author["id"].startswith("name:") for paper in papers for author in paper["authors"])
    if name_authors:
        warnings.append("ORCID がない著者は正規化した氏名で仮に対応付けています。同姓同名の混同・氏名の異表記による分割があり、厳密な著者同定ではありません。")
    missing = sum(not paper["abstract"] for paper in papers)
    if missing:
        warnings.append(f"抄録が提供されていない文献が {missing} 件あります。タイトル・提供キーワードを用いるため分析の情報量が限られます。")
    bibliography = bibliography_summary(papers)
    if bibliography["affiliation_papers"] < len(papers):
        warnings.append("所属が取得できない論文があります。所属機関や著者の所属先を推定して補完していません。")
    warnings.append("参照文献の DOI・標準 ID は提供された項目だけを保存します。未収録・識別子のない参照は照合できず、引用数から参照関係を作成していません。")
    if end_year == date.today().year:
        warnings.append("今年は未完了の年です。過去年との件数・引用数の比較に注意してください。")
    if monthly and end_month == date.today().strftime("%Y-%m"):
        warnings.append("今月は本日までの検索です。月途中の件数を完了月と同じ条件として比較しないでください。")
    truncated = any(item["total"] > item["imported"] for item in coverage)
    if truncated:
        warnings.append(f"取得上限または重複除去のため、全ヒット文献を取り込んでいません。{unit}ごとの総件数と取込件数を確認してください。")
    year_coverage = coverage
    if monthly:
        grouped = {}
        for item in coverage:
            year = int(item["month"][:4])
            row = grouped.setdefault(year, {"year": year, "total": 0, "imported": 0, "allocated": 0})
            for key in ("total", "imported", "allocated"):
                row[key] += item[key]
        year_coverage = list(grouped.values())
    report = {"provider": provider, "query": query, "start_year": start_year, "end_year": end_year, "limit": limit,
              "retrieved_at": retrieved_at, "year_coverage": year_coverage, "month_coverage": coverage if monthly else [], "truncated": truncated, "warnings": warnings,
              "imported_count": len(papers), "retrieved_count": len(papers), "duplicates_removed": duplicates, "invalid_rows": 0,
              "is_demo": False, "synthetic_count": 0, "sampling": "per_month_relevance_top" if monthly else "per_year_relevance_top", "citation_history_available": False,
              "start_month": start_month, "end_month": end_month, "date_pipeline_version": 2, "date_usable_count": date_usable_count}
    report.update(bibliography)
    if progress:
        progress(f"{_NAMES[provider]}：{len(papers)} 件の取得完了")
    return papers, report
