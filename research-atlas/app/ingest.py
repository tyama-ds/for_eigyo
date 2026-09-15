"""Scopus CSV ingestion and explicitly synthetic, reproducible demonstration data.

Annual citation counts are newly received citations in a calendar year. An absent
count remains absent; it never becomes an observed zero.
"""

from __future__ import annotations

import copy
import codecs
import csv
import hashlib
import io
import random
import re
import struct
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Iterator
from typing import BinaryIO

from .dates import merge_publication_dates, normalize_paper_date, normalize_publication_date
from .bibliography import (attach_author_affiliations, bibliography_summary, normalize_affiliations,
                           normalize_references, references_status)
from .text_metadata import is_test_summary
from .limits import MAX_IMPORT_ROWS

# Reference lists can exceed csv's default 128 KiB per-field limit. Let the
# platform's CSV parser represent any field; paper-count limits remain separate.
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:  # Some Windows Python versions accept a C long here.
    csv.field_size_limit((1 << (8 * struct.calcsize("l") - 1)) - 1)


def _key(value: str) -> str:
    return re.sub(r"[\s_\-:：()（）\[\].]+", "", unicodedata.normalize("NFKC", value).casefold())


_ALIASES = {
    "title": ["Title", "Document title", "Article title", "タイトル", "論文タイトル", "論文名", "文献名"],
    "abstract": ["Abstract", "抄録", "要旨", "概要"],
    "year": ["Year", "Publication year", "Pub year", "出版年", "発行年", "刊行年"],
    "authors": ["Authors", "Author", "著者", "著者名"],
    "author_names": ["Author full names", "Authors full names", "著者フルネーム"],
    "author_ids": ["Author(s) ID", "Author IDs", "Author ID", "Authors ID", "著者ID"],
    "keywords": ["Author Keywords", "Keywords", "Author keywords", "著者キーワード", "キーワード"],
    "index_keywords": ["Index Keywords", "Indexed keywords", "索引キーワード"],
    "citations": ["Cited by", "Citations", "Citation count", "Times cited", "被引用数", "引用数", "被引用回数", "引用回数"],
    "eid": ["EID", "Scopus EID", "Scopus ID", "文献ID"],
    "doi": ["DOI", "Digital object identifier"],
    "source": ["Source title", "Source", "Journal", "誌名", "掲載誌", "出版物名"],
    "provenance": ["Data provenance", "Research Atlas provenance", "データ来歴"],
    "publication_date": ["Publication date", "Cover Date", "Date", "発行日", "出版日", "公開日"],
    "month": ["Month", "Publication month", "発行月", "出版月"],
    "date_precision": ["Date precision", "日付精度"],
    "date_source": ["Date source", "日付出典"],
    "affiliations": ["Affiliations", "Affiliation", "Affilations", "所属", "所属機関", "機関"],
    "author_affiliations": ["Authors with affiliations", "Author affiliations", "著者別所属", "著者と所属"],
    "references": ["References", "Reference DOIs", "Reference DOI", "Cited references", "参考文献", "引用文献", "参照DOI"],
    "references_status": ["References status", "参照収録状態"],
}
_LOOKUP = {_key(alias): canonical for canonical, aliases in _ALIASES.items() for alias in aliases}
_MISSING = {"", "na", "n/a", "n.a.", "nan", "null", "none", "-", "—", "[no abstract available]", "[no citation count available]", "不明", "欠損"}
SYNTHETIC_PROVENANCE = "synthetic:research-atlas:v1"
_DEMO_DISCLAIMER = "All bibliographic records and quantitative observations in this demonstration are artificially generated."


def _clean(value: object) -> str:
    return str(value or "").strip()


def _optional(value: str) -> str:
    value = _clean(value)
    return "" if value.casefold() in _MISSING else value


def _integer(value: str, label: str, *, optional: bool = False) -> int | None:
    value = unicodedata.normalize("NFKC", _clean(value))
    if value.casefold() in _MISSING:
        if optional:
            return None
        raise ValueError(f"{label}が空欄です")
    if re.fullmatch(r"\d{1,3}(?:[, ]\d{3})+(?:\.0+)?", value):
        value = value.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:\.0+)?", value):
        raise ValueError(f"{label}は 0 以上の整数で指定してください（値: {value[:40]}）")
    result = int(value.split(".")[0])
    if result > 10**12:
        raise ValueError(f"{label}が大きすぎます")
    return result


def _year(value: str) -> int:
    result = _integer(value, "年")
    if result is None or not 1500 <= result <= 2100:
        raise ValueError("年は 1500〜2100 の西暦で指定してください")
    return result


def _doi(value: str) -> str:
    value = _optional(value)
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value, flags=re.I).strip().casefold()


def _annual_header(header: str) -> int | None:
    value = unicodedata.normalize("NFKC", header).strip()
    match = re.fullmatch(r"(?:annual\s+)?(?:citations?|cited\s*by|被引用(?:数|回数)?|引用(?:数|回数)?)[\s_\-:：()]*((?:19|20)\d{2})\)?(?:年)?", value, flags=re.I)
    if not match:
        match = re.fullmatch(r"((?:19|20)\d{2})(?:年)?[\s_\-:：()]*(?:citations?|cited\s*by|被引用(?:数|回数)?|引用(?:数|回数)?)", value, flags=re.I)
    return int(match.group(1)) if match else None


class _BorrowedBinary(io.BufferedIOBase):
    """A TextIOWrapper may close this adapter without closing its caller's file."""

    def __init__(self, source: BinaryIO):
        super().__init__()
        self.source = source

    def readable(self):
        return True

    def seekable(self):
        return True

    def read(self, size=-1):
        if size < 0:
            raise ValueError("CSV の読み取りサイズを指定してください。")
        return self.source.read(size)

    def read1(self, size=-1):
        return self.read(size)

    def seek(self, offset, whence=io.SEEK_SET):
        return self.source.seek(offset, whence)

    def tell(self):
        return self.source.tell()


def _csv_encoding(source: BinaryIO) -> str:
    """Validate the complete encoding in bounded chunks before yielding rows.

    Checking only the header would choose UTF-8 for a CP932 file whose first
    non-ASCII character occurs late in a long abstract/reference field.
    """
    source.seek(0)
    prefix = source.read(2)
    encodings = ["utf-16"] if prefix in (b"\xff\xfe", b"\xfe\xff") else ["utf-8-sig", "cp932"]
    encoding_error = "CSV の文字コードを読み取れません。UTF-8 または CP932（Shift-JIS）の CSV を使用してください。"
    for encoding in encodings:
        source.seek(0)
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        nonempty = False
        try:
            while True:
                chunk = source.read(1024 * 1024)
                decoded = decoder.decode(chunk, final=not chunk)
                if "\x00" in decoded:
                    raise ValueError(encoding_error)
                nonempty = nonempty or bool(decoded.strip())
                if not chunk:
                    break
        except UnicodeDecodeError:
            continue
        if not nonempty:
            raise ValueError("CSV ファイルが空です。Scopus から書誌情報を CSV 形式で出力してください。")
        source.seek(0)
        return encoding
    raise ValueError(encoding_error)


def _skip_csv_prefix(stream: io.TextIOWrapper) -> None:
    """Skip leading BOM/blank lines without reading a potentially huge line."""
    while True:
        position = stream.tell()
        chunk = stream.read(65536)
        stripped = chunk.lstrip("\ufeff\r\n")
        if stripped or not chunk:
            stream.seek(position)
            stream.read(len(chunk) - len(stripped))
            return


def _read_csv(content: bytes | BinaryIO, *, max_rows: int, label: str) -> tuple[Iterator[tuple[int, list[str]]], list[str], str, str, list[dict]]:
    source = io.BytesIO(content) if isinstance(content, bytes) else content
    if not hasattr(source, "read") or not hasattr(source, "seek") or not source.seekable():
        raise ValueError("CSV は読み取りとシークに対応するバイナリファイルで指定してください。")
    encoding = _csv_encoding(source)
    stream = io.TextIOWrapper(_BorrowedBinary(source), encoding=encoding, newline="")
    _skip_csv_prefix(stream)
    sample = stream.read(65536)
    # Excel's optional delimiter preamble is not a bibliographic header.
    preamble = re.match(r"sep=([,;\t])\r?\n", sample, flags=re.I)
    if preamble:
        delimiter = preamble.group(1)
    else:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            first_line = sample.partition("\n")[0]
            delimiter = max(",;\t", key=first_line.count)
        # The header is reliable even when malformed rows confuse Sniffer.
        candidates = []
        for candidate in ",;\t":
            try:
                head = next(csv.reader(io.StringIO(sample), delimiter=candidate, strict=True))
                recognized = sum(_key(cell) in _LOOKUP or _annual_header(cell) is not None for cell in head)
                candidates.append((recognized, len(head), candidate))
            except (csv.Error, StopIteration):
                pass
        if candidates:
            best = max(candidates)
            if best[0] >= 2:
                delimiter = best[2]
    stream.seek(0)
    _skip_csv_prefix(stream)
    if preamble:
        stream.read(preamble.end())
    reader = csv.reader(stream, delimiter=delimiter, strict=True)
    try:
        headers = [cell.strip() for cell in next(reader)]
    except (csv.Error, StopIteration) as exc:
        stream.close()
        raise ValueError("CSV の見出し行を読み取れません。引用符と区切り文字を確認してください。") from exc
    errors = []
    def rows():
        # Iterate records as they are normalized; don't retain a second full
        # collection of raw abstracts/reference strings alongside the corpus.
        count = 0
        try:
            for values in reader:
                if not values or not any(cell.strip() for cell in values):
                    continue
                count += 1
                if count > max_rows:
                    raise ValueError(f"{label}は 1 回 {max_rows:,} 行までです。CSV を分割し、既存データへの追加を繰り返してください。")
                if len(values) != len(headers):
                    errors.append({"row": reader.line_num, "reason": f"列数が見出しと一致しません（{len(values)} / {len(headers)}）。抄録内の引用符や区切り文字を確認してください"})
                    continue
                yield reader.line_num, values
        except csv.Error as exc:
            errors.append({"row": reader.line_num, "reason": f"CSV の引用符が不正です。この位置以降を読み取れませんでした: {exc}"})
        finally:
            stream.close()
    return rows(), headers, encoding, delimiter, errors


def _columns(headers: list[str]) -> tuple[dict[str, list[int]], dict[int, list[int]]]:
    columns: dict[str, list[int]] = defaultdict(list)
    annual: dict[int, list[int]] = defaultdict(list)
    for index, header in enumerate(headers):
        field = _LOOKUP.get(_key(header))
        if field:
            columns[field].append(index)
        else:
            year = _annual_header(header)
            if year is not None:
                annual[year].append(index)
    return dict(columns), dict(annual)


def _values(row: list[str], columns: dict[str, list[int]], field: str) -> list[str]:
    return [_optional(row[index]) for index in columns.get(field, []) if _optional(row[index])]


def _first(row: list[str], columns: dict[str, list[int]], field: str) -> str:
    return next(iter(_values(row, columns, field)), "")


def _publication_date(row, columns, headers, year, warnings, row_number):
    candidates = []
    for index in columns.get("publication_date", []):
        if _optional(row[index]):
            candidates.append((normalize_publication_date(row[index], year), "csv:" + headers[index]))
    for index in columns.get("month", []):
        month = unicodedata.normalize("NFKC", _optional(row[index])).strip().removesuffix("月").strip()
        if month:
            value = [year, int(month.split(".")[0])] if re.fullmatch(r"\d{1,2}(?:\.0+)?", month) else f"{month} {year}"
            candidates.append((normalize_publication_date(value, year), "csv:" + headers[index] + " + Year"))
    notices = [notice for result, _ in candidates for notice in result["warnings"]]
    explicit = [(result, source) for result, source in candidates if result["publication_date"]]
    if explicit:
        chosen, source = explicit[0]
        if any(result["publication_date"][:7] != chosen["publication_date"][:7]
               or (len(result["publication_date"]) == len(chosen["publication_date"]) == 10 and result["publication_date"] != chosen["publication_date"])
               for result, _ in explicit[1:]):
            notices.append("複数の日付列または Month 列が矛盾するため、月次分析の対象外にしました。")
    else:
        chosen, source = normalize_publication_date(None, year), candidates[0][1] if candidates else "csv:Year"
    if notices:
        chosen = normalize_publication_date(None, year)
    fields = normalize_paper_date({"year": year, "publication_date": chosen["publication_date"],
                                  "date_precision": _first(row, columns, "date_precision") or chosen["date_precision"],
                                  "date_source": _first(row, columns, "date_source") or source})
    fields.pop("warnings")
    warnings.extend(f"{row_number} 行目: {notice}" for notice in dict.fromkeys(notices))
    return fields


def _date_summary(papers):
    counts = dict.fromkeys(("day", "month", "year", "unknown"), 0)
    for paper in papers:
        precision = paper.get("date_precision", "year" if paper.get("year") else "unknown")
        counts[precision if precision in counts else "unknown"] += 1
    return {"date_pipeline_version": 2, "date_precision_counts": counts,
            "date_usable_count": sum(bool(paper.get("publication_date")) and paper.get("date_precision") in {"month", "day"} for paper in papers)}


def _split_author_names(value: str, expected: int) -> list[str]:
    if ";" in value or "；" in value or "|" in value:
        return [part.strip() for part in re.split(r"[;；|]", value) if part.strip()]
    parts = [part.strip() for part in value.split(",") if part.strip()]
    # A comma in 'Smith, John' or 'Smith, J.' belongs to the name.
    # Older exports used 'Smith J., Jones K.': split only with strong evidence.
    initials = r"(?:[A-ZÀ-ÖØ-Þ]\.?\s*){1,5}"
    if len(parts) > 1 and all(re.search(rf"\s{initials}$", part) for part in parts):
        return parts
    if expected > 1 and len(parts) == 2 * expected and all(re.fullmatch(initials, part) for part in parts[1::2]):
        return [f"{parts[index]}, {parts[index + 1]}" for index in range(0, len(parts), 2)]
    return [value.strip()] if value.strip() else []


def _author_identity(value: str) -> str:
    """Add the Scopus namespace to bare Scopus IDs; preserve supplied namespaces."""
    identifier = _clean(value)
    if identifier.isdigit():
        return "scopus:" + identifier
    if match := re.fullmatch(r"scopus\s*:\s*(.+)", identifier, re.I):
        return "scopus:" + match.group(1).strip()
    if match := re.fullmatch(r"(?:orcid\s*:\s*|https?://(?:www\.)?orcid\.org/)(\d{4}-\d{4}-\d{4}-\d{3}[\dXx])/?(?:[?#].*)?", identifier, re.I):
        return "orcid:" + match.group(1).upper()
    return identifier


def _authors(row: list[str], columns: dict[str, list[int]], warnings: list[str], row_number: int) -> list[dict]:
    raw_ids = _first(row, columns, "author_ids")
    ids = [part.strip() for part in re.split(r"[;；|]", raw_ids) if part.strip()]
    if len(ids) <= 1 and "," in raw_ids:
        ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
    full_names = _split_author_names(_first(row, columns, "author_names"), len(ids))
    abbreviated_names = _split_author_names(_first(row, columns, "authors"), len(ids))
    names = full_names or abbreviated_names
    # Some Scopus exports omit authors from the full-name column, while the
    # abbreviated names and IDs remain complete and aligned. Prefer that complete
    # pair, then use explicit embedded IDs to recover the available full names.
    if full_names and ids and len(full_names) != len(ids) and len(abbreviated_names) == len(ids):
        names = list(abbreviated_names)
        positions = defaultdict(list)
        for index, identifier in enumerate(ids):
            positions[_author_identity(identifier)].append(index)
        recovered = 0
        for name in full_names:
            embedded = re.search(r"\s*\((\d{5,})\)\s*$", name)
            matches = positions.get(_author_identity(embedded.group(1)), []) if embedded else []
            if len(matches) == 1:
                names[matches[0]] = name
                recovered += 1
        warnings.append(f"{row_number} 行目: 著者フルネーム列が不完全なため、件数が一致する Authors と著者 ID を使用しました。フルネーム {recovered} 件は明示された ID で照合し、ほかの著者も省略せず保持しました。")
    if not names and ids:
        return [{"id": identifier, "name": f"Author {identifier}"}
                for identifier in dict.fromkeys(_author_identity(author_id) for author_id in ids)]
    aligned = len(ids) == len(names)
    if ids and not aligned:
        warnings.append(f"{row_number} 行目: 著者名と著者 ID の件数が異なるため、位置による ID の対応付けを行いませんでした。")
    result, seen = [], set()
    for index, name in enumerate(names):
        embedded = re.search(r"\s*\((\d{5,})\)\s*$", name)
        author_id = embedded.group(1) if embedded else (ids[index] if aligned else "")
        clean_name = name[:embedded.start()].strip() if embedded else name
        identity = _author_identity(author_id) if author_id else "name:" + hashlib.sha256(_key(clean_name).encode()).hexdigest()[:16]
        if identity not in seen:
            result.append({"id": identity, "name": clean_name})
            seen.add(identity)
    return result


def _keywords(values: list[str]) -> list[str]:
    result, seen = [], set()
    for value in values:
        for term in re.split(r"[;；|]" if re.search(r"[;；|]", value) else r",", value):
            term = re.sub(r"\s+", " ", term).strip()
            if term and term.casefold() not in seen:
                result.append(term)
                seen.add(term.casefold())
    return result


def _identity_keys(paper: dict) -> set[str]:
    aliases = paper.get("aliases", {})
    result = {f"eid:{_clean(eid).casefold()}" for eid in aliases.get("eids", []) if eid}
    if paper.get("id"):
        result.add(f"eid:{_clean(paper['id']).casefold()}")
    result.update(f"doi:{_doi(doi)}" for doi in [paper.get("doi", ""), *aliases.get("dois", [])] if _doi(doi))
    result.add(f"title:{_key(paper['title'])}:{paper['year']}")
    return result


def _is_synthetic(paper: dict) -> bool:
    if _clean(paper.get("provenance")).casefold() == SYNTHETIC_PROVENANCE:
        return True
    if is_test_summary(paper.get("abstract")):
        return True
    # Recognize older exported demos without relying on an editable filename or
    # broad terms such as "synthetic" that also occur in real research papers.
    return bool(
        re.fullmatch(r"SYNTHETIC-\d{5}", _clean(paper.get("id")))
        and _DEMO_DISCLAIMER in paper.get("abstract", "")
        and paper.get("source") in {f"Synthetic Journal of {topic['name']}" for topic in _DEMO_TOPICS}
    )


def _provenance_report(papers: list[dict], warnings: list[str]) -> dict:
    count = sum(_is_synthetic(paper) for paper in papers)
    test_summaries = sum(is_test_summary(paper.get("abstract")) for paper in papers)
    if test_summaries:
        warnings.append(f"{test_summaries} 件の抄録に [TEST SUMMARY; TITLE-BASED; JA] があり、タイトル由来の日本語テスト要約として扱います。原論文の抄録ではなく、このマーカーだけで書誌情報全体が架空とは判定しません。")
    if count == len(papers) and count:
        warnings.append(f"読み込んだ {count} 件は合成・テスト用データの来歴があるため、検証用として扱います。実際の研究動向を示す集合としては扱えません。")
    elif count:
        warnings.append(f"合成・テスト用データの来歴がある {count} 件と、そのマーカーのないデータ {len(papers) - count} 件が混在しています。この集合から実際の研究動向を判断しないでください。")
    return {"synthetic_count": count, "test_summary_count": test_summaries, "is_demo": count > 0}


def _merge_papers(first: dict, second: dict, warnings: list[str]) -> dict:
    merged = copy.deepcopy(first)
    first_dois, second_dois = set(first["aliases"]["dois"]), set(second["aliases"]["dois"])
    if first_dois and second_dois and not first_dois.intersection(second_dois):
        warnings.append("共通 EID に異なる DOI が対応しています。先に読み込んだ DOI とすべての DOI 別名を保持しました。")
    dates, date_warnings = merge_publication_dates(first, second)
    merged.update(dates)
    warnings.extend(date_warnings)
    if _is_synthetic(first) or _is_synthetic(second):
        merged["provenance"] = SYNTHETIC_PROVENANCE
    for field in ("abstract", "doi", "source"):
        if not merged[field] and second[field]:
            merged[field] = second[field]
    if merged["citations"] is None:
        merged["citations"] = second["citations"]
    elif second["citations"] is not None and merged["citations"] != second["citations"]:
        warnings.append(f"重複論文「{first['title'][:60]}」の累積引用数が異なるため、先に読み込んだ値を保持しました。")
    for field in ("eids", "dois"):
        merged["aliases"][field] = list(dict.fromkeys([*first["aliases"][field], *second["aliases"][field]]))
    merged["keywords"] = _keywords([*first["keywords"], *second["keywords"]])
    merged["affiliations"] = normalize_affiliations([*first.get("affiliations", []), *second.get("affiliations", [])])
    merged["references"] = normalize_references([*first.get("references", []), *second.get("references", [])])
    merged["references_status"] = "provided" if "provided" in (references_status(first), references_status(second)) else "not_provided"
    known_authors = {author["id"]: author for author in merged["authors"]}
    for author in second["authors"]:
        if author["id"] not in known_authors:
            target = copy.deepcopy(author)
            merged["authors"].append(target)
            known_authors[author["id"]] = target
        else:
            target = known_authors[author["id"]]
            affiliations = normalize_affiliations([*target.get("affiliations", []), *author.get("affiliations", [])])
            if affiliations:
                target["affiliations"] = affiliations
    for year, count in second["citation_history"].items():
        if year in merged["citation_history"] and merged["citation_history"][year] != count:
            warnings.append(f"重複論文「{first['title'][:60]}」の {year} 年引用数が矛盾するため、先に読み込んだ値を保持しました。")
        else:
            merged["citation_history"][year] = count
    return merged


def _report(errors: list[dict], warnings: list[str], count: int, duplicates: int, encoding: str, delimiter: str) -> dict:
    if errors:
        warnings.append(f"不正な {len(errors)} 行を除外しました。詳細は row_errors を参照してください。")
    # Repeated warnings should not overwhelm API responses for large CSV files.
    distinct = list(dict.fromkeys(warnings))
    return {"warnings": distinct[:100] + ([f"ほか {len(distinct) - 100} 件の警告があります。"] if len(distinct) > 100 else []), "duplicates_removed": duplicates, "invalid_rows": len(errors), "imported_count": count, "row_errors": errors[:100], "row_errors_truncated": len(errors) > 100, "encoding": encoding, "delimiter": delimiter}


def parse_scopus_csv(content: bytes | BinaryIO) -> tuple[list[dict], dict]:
    """Read Scopus exports; retain usable rows and report malformed rows."""
    rows, headers, encoding, delimiter, errors = _read_csv(content, max_rows=MAX_IMPORT_ROWS, label="論文 CSV")
    columns, annual = _columns(headers)
    if "title" not in columns or "year" not in columns:
        raise ValueError("CSV には Title（タイトル）と Year（出版年）の見出しが必要です。Scopus の CSV 書誌情報を選択してください。")
    papers: dict[int, dict] = {}
    identities: dict[str, int] = {}
    titles: dict[str, set[int]] = defaultdict(set)
    parents: dict[int, int] = {}
    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def register(index, keys):
        for key in keys:
            if key.startswith("title:"):
                titles[key].add(index)
            else:
                identities[key] = index
    warnings: list[str] = []
    duplicates = 0
    for row_number, row in rows:
        try:
            title = _first(row, columns, "title")
            if not title:
                raise ValueError("タイトルが空欄です")
            year = _year(_first(row, columns, "year"))
            citations = _integer(_first(row, columns, "citations"), "累積引用数", optional=True)
            history = {}
            for citation_year, indices in annual.items():
                values = [_integer(row[index], f"{citation_year} 年の引用数", optional=True) for index in indices]
                observed = {value for value in values if value is not None}
                if len(observed) > 1:
                    raise ValueError(f"{citation_year} 年の引用列が重複し値が矛盾しています")
                if observed:
                    if citation_year < year:
                        raise ValueError(f"出版年より前の {citation_year} 年に引用数が指定されています")
                    history[str(citation_year)] = observed.pop()
            eid = _first(row, columns, "eid")
            doi = _doi(_first(row, columns, "doi"))
            identity = eid or ("doi:" + doi if doi else "paper:" + hashlib.sha256(f"{_key(title)}:{year}".encode()).hexdigest()[:20])
            paper = {"id": identity, "title": title, "abstract": _first(row, columns, "abstract"), "year": year, "authors": _authors(row, columns, warnings, row_number), "keywords": _keywords(_values(row, columns, "keywords") + _values(row, columns, "index_keywords")), "citations": citations, "doi": doi, "source": _first(row, columns, "source"), "citation_history": history, "aliases": {"eids": [eid] if eid else [], "dois": [doi] if doi else []}, "provenance": _first(row, columns, "provenance")}
            paper.update(_publication_date(row, columns, headers, year, warnings, row_number))
            for raw in _values(row, columns, "author_affiliations"):
                paper["authors"], unmatched = attach_author_affiliations(paper["authors"], raw)
                if unmatched:
                    warnings.append(f"{row_number} 行目: 著者と明示的に対応付けられない所属項目 {unmatched} 個を著者へ割り当てませんでした。位置順で所属を補完していません。")
            paper["affiliations"] = normalize_affiliations([
                *_values(row, columns, "affiliations"),
                *(item for author in paper["authors"] for item in author.get("affiliations", []))])
            raw_references = _values(row, columns, "references")
            paper["references"] = normalize_references(raw_references)
            reference_state = _first(row, columns, "references_status")
            paper["references_status"] = ("provided" if paper["references"] else reference_state
                                          if reference_state in {"provided", "not_provided"}
                                          else "provided" if any(raw_references) else "not_provided")
            if any(raw_references) and not paper["references"] and any(value.strip() != "[]" for value in raw_references):
                warnings.append(f"{row_number} 行目: 参照文献に DOI・標準 ID を確認できませんでした。参照の矢印を推定していません。")
            if _is_synthetic(paper):
                paper["provenance"] = SYNTHETIC_PROVENANCE
        except ValueError as exc:
            errors.append({"row": row_number, "reason": str(exc)})
            continue
        keys = _identity_keys(paper)
        strong = {root(identities[key]) for key in keys if key in identities}
        title_matches = {root(index) for key in keys if key.startswith("title:") for index in titles.get(key, ())} - strong
        direct_dois = set(paper["aliases"]["dois"])
        for index in strong:
            direct_dois.update(papers[index]["aliases"]["dois"])
        compatible = {index for index in title_matches if not direct_dois or not papers[index]["aliases"]["dois"]
                      or direct_dois.intersection(papers[index]["aliases"]["dois"])}
        if title_matches - compatible:
            warnings.append("同じタイトル・出版年でも DOI が異なる論文は、タイトルだけで統合しませんでした。")
        if not direct_dois:
            doi_sets = [set(papers[index]["aliases"]["dois"]) for index in compatible if papers[index]["aliases"]["dois"]]
            if any(not left.intersection(right) for index, left in enumerate(doi_sets) for right in doi_sets[index + 1:]):
                compatible = set()
                warnings.append("同じタイトル・出版年に異なる DOI が対応するため、DOI のない論文は別レコードとして保持しました。")
        matches = sorted(strong | compatible)
        if matches:
            target = matches[0]
            # A bridging DOI / EID can join groups created earlier in the file.
            merged = papers[target]
            for other in matches[1:]:
                merged = _merge_papers(merged, papers.pop(other), warnings)
                # Retain old aliases via path-compressed roots. A bridge must
                # not scan/rewrite the entire corpus index for each duplicate.
                parents[other] = target
                duplicates += 1
            merged = _merge_papers(merged, paper, warnings)
            papers[target] = merged
            duplicates += 1
            register(target, keys | _identity_keys(merged))
        else:
            papers[row_number] = paper
            parents[row_number] = row_number
            register(row_number, keys)
    result = list(papers.values())
    if not result:
        detail = " / ".join(f"{error['row']} 行: {error['reason']}" for error in errors[:3])
        raise ValueError("有効な論文データがありません。" + (f" {detail}" if detail else "タイトルと出版年を含むデータ行が必要です。"))
    missing_abstracts = sum(not paper["abstract"] for paper in result)
    missing_citations = sum(paper["citations"] is None for paper in result)
    if missing_abstracts:
        warnings.append(f"{missing_abstracts} 件の抄録が欠損しています。タイトルとキーワードを用いた分析になります。")
    if missing_citations:
        warnings.append(f"{missing_citations} 件の累積引用数が不明です。0 件として補完しません。")
    if not annual:
        warnings.append("年別引用履歴がありません。累積被引用数だけでは年ごとの引用増加を算出できません。別途、年別引用 CSV を追加できます。")
    provenance = _provenance_report(result, warnings)
    report = _report(errors, warnings, len(result), duplicates, encoding, delimiter)
    report.update(provenance)
    report.update(_date_summary(result))
    report.update(bibliography_summary(result))
    return result, report


def attach_citation_history(papers: list[dict], content: bytes | BinaryIO) -> tuple[list[dict], dict]:
    """Attach long-form annual citation observations without mutating inputs.

    Conflicting incoming observations for the same paper/year are all rejected.
    Existing observations survive such conflicts. Missing values are not updates.
    """
    rows, headers, encoding, delimiter, errors = _read_csv(content, max_rows=100000, label="年別引用 CSV")
    columns, _ = _columns(headers)
    if "year" not in columns or "citations" not in columns or not ({"eid", "doi"} & columns.keys()):
        raise ValueError("年別引用 CSV には EID または DOI、Year、Citations が必要です。Citations はその年に新たに受けた引用数です。")
    result = copy.deepcopy(papers)
    identities: dict[str, set[int]] = defaultdict(set)
    for index, paper in enumerate(result):
        for key in _identity_keys(paper):
            if not key.startswith("title:"):
                identities[key].add(index)
    candidates: dict[tuple[int, str], list[tuple[int, int]]] = defaultdict(list)
    missing_rows = 0
    for row_number, row in rows:
        try:
            eid = _first(row, columns, "eid")
            doi = _doi(_first(row, columns, "doi"))
            if not eid and not doi:
                raise ValueError("EID と DOI がどちらも空欄です")
            matches = set()
            if eid:
                matches.update(identities.get(f"eid:{eid.casefold()}", set()))
            if doi:
                matches.update(identities.get(f"doi:{doi}", set()))
            if not matches:
                raise ValueError("EID / DOI に一致する論文がありません")
            if len(matches) > 1:
                raise ValueError("EID と DOI が異なる論文に一致するため、対応を確定できません")
            index = matches.pop()
            year = _year(_first(row, columns, "year"))
            if year < result[index]["year"]:
                raise ValueError("出版年より前の引用履歴は登録できません")
            count = _integer(_first(row, columns, "citations"), "年別引用数", optional=True)
            if count is None:
                missing_rows += 1
                continue
            candidates[(index, str(year))].append((row_number, count))
        except ValueError as exc:
            errors.append({"row": row_number, "reason": str(exc)})
    applied, duplicates, updated = 0, 0, set()
    for (index, year), observations in candidates.items():
        counts = {count for _, count in observations}
        previous = result[index].get("citation_history", {}).get(year)
        if len(counts) > 1 or (previous is not None and previous not in counts):
            for row_number, _ in observations:
                errors.append({"row": row_number, "reason": f"同じ論文の {year} 年の引用数が重複または既存値と矛盾するため、この年の更新を拒否しました"})
            continue
        result[index].setdefault("citation_history", {})[year] = counts.pop()
        applied += 1
        updated.add(index)
        duplicates += len(observations) - 1
    if not applied:
        detail = " / ".join(f"{error['row']} 行: {error['reason']}" for error in errors[:3])
        raise ValueError("取り込める年別引用数がありません。空欄は欠損として扱います。" + (" " + detail if detail else ""))
    warnings = ["年別引用数は各年に新たに受けた引用数です。未提供年は欠損のまま保持し、累積被引用数は変更しません。"]
    if missing_rows:
        warnings.append(f"{missing_rows} 行の引用数が空欄のため、更新を行いませんでした。")
    provenance = _provenance_report(result, warnings)
    report = _report(errors, warnings, applied, duplicates, encoding, delimiter)
    report.update({"updated_papers": len(updated), "annual_values_imported": applied, "missing_rows": missing_rows})
    report.update(provenance)
    report.update(_date_summary(result))
    report.update(bibliography_summary(result))
    return result, report


_DEMO_TOPICS = [
    {"name": "Solid-state batteries", "counts": [18, 24, 32, 40, 45], "citation_rate": 5.7,
     "keywords": ["solid-state battery", "solid electrolyte", "lithium metal", "ionic conductivity", "dendrite suppression", "sulfide electrolyte", "interface engineering", "fast charging"],
     "objects": ["sulfide electrolyte membranes", "lithium metal interfaces", "garnet ceramic separators", "composite cathode coatings", "anode-free battery stacks"],
     "methods": ["operando impedance spectroscopy", "atomistic ion-transport simulations", "pressure-controlled cycling", "cryo-electron microscopy", "automated electrochemical screening"],
     "goals": ["suppress dendrite propagation during fast charging", "reduce grain-boundary resistance at room temperature", "stabilize long-duration lithium plating", "improve battery cycle life under practical loading"],
     "settings": ["electric vehicle packs", "grid storage modules", "low-temperature mobility", "roll-to-roll cell manufacturing"]},
    {"name": "Autonomous AI agents", "counts": [3, 6, 12, 29, 55], "citation_rate": 8.5,
     "keywords": ["autonomous agents", "large language models", "tool use", "multi-agent planning", "retrieval augmented generation", "agent memory", "reasoning evaluation", "human feedback"],
     "objects": ["tool-using language agents", "multi-agent planning systems", "retrieval-augmented research assistants", "persistent agent memory", "hierarchical reasoning policies"],
     "methods": ["tool-use benchmark evaluation", "reinforcement learning from feedback", "retrieval-based memory ablation", "simulation of cooperative planning", "adversarial instruction testing"],
     "goals": ["increase reliability of long-horizon task execution", "ground agent responses in retrieved evidence", "reduce cascading errors in cooperative reasoning", "improve calibrated abstention during tool selection"],
     "settings": ["scientific discovery workflows", "software maintenance tasks", "robotic task planning", "enterprise document retrieval"]},
    {"name": "Quantum sensing", "counts": [11, 13, 15, 18, 20], "citation_rate": 4.8,
     "keywords": ["quantum sensing", "nitrogen vacancy centers", "spin coherence", "magnetometry", "quantum metrology", "diamond defects", "atomic clocks", "photon collection"],
     "objects": ["diamond nitrogen-vacancy ensembles", "squeezed spin states", "room-temperature quantum magnetometers", "integrated atomic clocks", "single-photon readout structures"],
     "methods": ["Ramsey interferometry", "dynamical decoupling sequences", "spin-echo spectroscopy", "Bayesian phase estimation", "confocal photon counting"],
     "goals": ["extend spin coherence in noisy environments", "increase magnetic-field sensitivity", "reduce systematic drift in quantum measurement", "improve photon collection from diamond defects"],
     "settings": ["biomagnetic imaging", "subsurface navigation", "nanoscale materials characterization", "precision timing networks"]},
    {"name": "Neuromorphic computing", "counts": [7, 10, 16, 24, 38], "citation_rate": 6.2,
     "keywords": ["neuromorphic computing", "spiking neural networks", "memristors", "event-driven processing", "in-memory computing", "synaptic plasticity", "edge intelligence", "analog crossbars"],
     "objects": ["memristive synaptic arrays", "spiking neural network accelerators", "event-driven vision processors", "analog crossbar circuits", "adaptive neuron devices"],
     "methods": ["surrogate-gradient spike training", "hardware-in-the-loop calibration", "synaptic plasticity measurements", "event-stream benchmark testing", "device-aware circuit simulation"],
     "goals": ["reduce inference energy at the edge", "maintain accuracy under device variability", "enable online learning with local plasticity", "lower latency for sparse sensory events"],
     "settings": ["wearable sensing devices", "mobile robotic vision", "always-on acoustic monitoring", "battery-powered edge nodes"]},
    {"name": "Perovskite photovoltaics", "counts": [24, 28, 30, 31, 31], "citation_rate": 7.4,
     "keywords": ["perovskite solar cells", "tandem photovoltaics", "defect passivation", "power conversion efficiency", "encapsulation", "halide segregation", "slot-die coating", "solar stability"],
     "objects": ["perovskite-silicon tandem cells", "halide perovskite absorbers", "passivated grain boundaries", "lead-reduced solar modules", "flexible photovoltaic films"],
     "methods": ["time-resolved photoluminescence", "accelerated damp-heat testing", "slot-die coating optimization", "transient absorption spectroscopy", "outdoor maximum-power tracking"],
     "goals": ["improve operational stability under illumination", "reduce non-radiative recombination losses", "control halide segregation during thermal cycling", "scale uniform films to large-area solar modules"],
     "settings": ["rooftop tandem modules", "building-integrated solar panels", "flexible energy harvesting", "pilot-scale photovoltaic production"]},
    {"name": "Photonic interconnects", "counts": [4, 7, 13, 23, 35], "citation_rate": 6.8,
     "keywords": ["silicon photonics", "co-packaged optics", "optical interconnects", "microring resonators", "wavelength division multiplexing", "photonic packaging", "optical bandwidth", "datacenter networking"],
     "objects": ["co-packaged optical engines", "silicon microring modulators", "wavelength-multiplexed photonic links", "heterogeneous laser-chip assemblies", "integrated optical switching fabrics"],
     "methods": ["wafer-scale optical characterization", "thermal crosstalk compensation", "high-speed bit-error measurements", "electromagnetic device optimization", "fiber-attachment tolerance analysis"],
     "goals": ["increase bandwidth density between computing chips", "reduce energy per transmitted bit", "stabilize microring wavelengths under thermal load", "improve manufacturing yield of optical packages"],
     "settings": ["AI accelerator clusters", "datacenter switching fabrics", "chiplet communication links", "rack-scale optical networks"]},
    {"name": "Carbon capture materials", "counts": [22, 21, 20, 18, 16], "citation_rate": 5.3,
     "keywords": ["carbon capture", "direct air capture", "metal organic frameworks", "CO2 adsorption", "amine sorbents", "regeneration energy", "carbon dioxide separation", "porous materials"],
     "objects": ["amine-functionalized porous sorbents", "metal-organic framework adsorbents", "humidity-tolerant capture membranes", "electrochemical CO2 separation cells", "structured carbon capture beds"],
     "methods": ["cyclic breakthrough experiments", "density-functional adsorption calculations", "temperature-swing desorption analysis", "life-cycle process assessment", "operando infrared spectroscopy"],
     "goals": ["lower sorbent regeneration energy", "retain CO2 capacity under humid conditions", "improve selectivity at atmospheric concentrations", "extend adsorbent durability over repeated cycles"],
     "settings": ["direct air capture systems", "cement plant flue gas", "distributed capture units", "low-grade waste-heat recovery"]},
    {"name": "Blockchain IoT systems", "counts": [35, 29, 22, 15, 9], "citation_rate": 2.5,
     "keywords": ["blockchain IoT", "distributed ledger", "smart contracts", "consensus protocols", "device identity", "supply chain provenance", "permissioned blockchain", "edge consensus"],
     "objects": ["permissioned IoT ledgers", "lightweight device identity protocols", "smart-contract provenance registries", "edge blockchain gateways", "distributed consensus networks"],
     "methods": ["Byzantine-fault consensus simulation", "smart-contract security analysis", "gateway throughput benchmarking", "end-to-end latency measurements", "formal device authentication verification"],
     "goals": ["reduce consensus overhead on constrained devices", "verify provenance across organizational boundaries", "maintain device identity during network partitions", "improve auditability of sensor data exchange"],
     "settings": ["cold-chain logistics", "industrial sensor networks", "agricultural traceability", "connected manufacturing facilities"]},
]


def demo_papers() -> list[dict]:
    """Return 869 synthetic papers for 2021–2025; no real publication claims.

    Topic trajectories are designed scenarios, not measurements. Authors,
    abstracts, titles, journals, citation events, and identifiers are fabricated.
    """
    rng = random.Random(20260911)
    surnames = ["Aster", "Boreal", "Citrine", "Dovetail", "Ember", "Fable", "Glimmer", "Harbor", "Islet", "Juniper", "Kestrel", "Lumen", "Morrow", "Nimbus", "Opal", "Prism"]
    given_names = ["Nora", "Eli", "Mira", "Theo", "Lina", "Arlo", "Sora", "Vera"]
    author_pool = [{"id": f"synthetic-author-{index + 1:03d}", "name": f"{surnames[index % 16]}, {given_names[index // 16]} [Synthetic]"} for index in range(128)]
    qualifiers = ["Comparative evaluation", "Mechanistic study", "Robust optimization", "Experimental validation", "Scalable design", "Performance assessment", "Multiscale analysis", "Reliability investigation"]
    results = ["The results identify a reproducible operating window and expose a trade-off between performance and robustness.", "Controlled comparisons show improved performance relative to the reference configuration, while long-duration validation remains necessary.", "Sensitivity analysis identifies the dominant loss mechanisms and highlights conditions where the improvement does not generalize.", "Repeated measurements support the proposed mechanism, although variation between devices limits immediate scale-up."]
    papers = []
    for topic_index, topic in enumerate(_DEMO_TOPICS):
        for year_index, count in enumerate(topic["counts"]):
            year = 2021 + year_index
            for within_year in range(count):
                number = len(papers) + 1
                obj = rng.choice(topic["objects"])
                method = rng.choice(topic["methods"])
                goal = rng.choice(topic["goals"])
                setting = rng.choice(topic["settings"])
                title = f"{rng.choice(qualifiers)} of {obj} for {setting}: study {year}-{within_year + 1:03d}"
                keywords = [*topic["keywords"][:3], *rng.sample(topic["keywords"][3:], 2)]
                abstract = (f"{topic['name']} offers a pathway to address practical limitations in {setting}. "
                            f"This synthetic study investigates {obj} using {method} to {goal}. "
                            f"The analysis considers {keywords[0]}, {keywords[1]}, and {keywords[3]} through controlled comparisons, "
                            f"parameter sweeps, and independent validation cases. {rng.choice(results)} "
                            f"The findings connect {keywords[2]} with {keywords[4]} and outline follow-up experiments for {setting}. "
                            f"{_DEMO_DISCLAIMER}")
                own_pool = list(range(topic_index * 16, topic_index * 16 + 16))
                author_indices = rng.sample(own_pool, rng.randint(2, 4))
                if rng.random() < 0.23:
                    other_topic = (topic_index + rng.choice([-1, 1, 2])) % len(_DEMO_TOPICS)
                    author_indices.append(rng.randrange(other_topic * 16, other_topic * 16 + 16))
                history = {}
                impact = rng.lognormvariate(-0.2, 0.65)
                for received_year in range(year, 2026):
                    age = received_year - year
                    age_factor = 0.3 if age == 0 else (1.0 if age == 1 else max(0.5, 1.2 - 0.13 * (age - 2)))
                    attention = 1 + ((received_year - 2021) * (0.16 if topic_index in (1, 3, 5) else -0.04 if topic_index == 7 else 0.03))
                    mean = topic["citation_rate"] * impact * age_factor * attention
                    history[str(received_year)] = max(0, round(rng.gauss(mean, max(0.7, mean**0.5))))
                paper_id = f"SYNTHETIC-{number:05d}"
                papers.append({"id": paper_id, "title": title, "abstract": abstract, "year": year, "authors": [copy.deepcopy(author_pool[index]) for index in author_indices], "keywords": keywords, "citations": sum(history.values()), "doi": "", "source": f"Synthetic Journal of {topic['name']}", "citation_history": history, "aliases": {"eids": [paper_id], "dois": []}, "provenance": SYNTHETIC_PROVENANCE, "publication_date": "", "date_precision": "year", "date_source": "synthetic:year-only"})
    rng.shuffle(papers)
    return papers
