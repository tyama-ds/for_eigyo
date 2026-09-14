"""Publication dates with explicit precision; never invent a month or day."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime


_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_MONTHS = {name: index for index, name in enumerate(_MONTH_NAMES, 1)} | {name[:3]: index for index, name in enumerate(_MONTH_NAMES, 1)} | {"sept": 9}


def _known_year(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d{4}", str(value or "")):
        return None
    number = int(value)
    return number if 1500 <= number <= 2100 else None


def _parts(value):
    if isinstance(value, datetime):
        return [value.year, value.month, value.day]
    if isinstance(value, date):
        return [value.year, value.month, value.day]
    if isinstance(value, (list, tuple)):
        if not 1 <= len(value) <= 3 or any(isinstance(part, bool) or not re.fullmatch(r"\d+", str(part)) for part in value):
            raise ValueError("invalid date parts")
        return [int(part) for part in value]
    value = unicodedata.normalize("NFKC", str(value)).strip()
    value = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value, flags=re.I)
    value = value.replace("年", "-").replace("月", "-").replace("日", "").rstrip("-")
    if re.fullmatch(r"\d{4}(?:[-/.]\d{1,2}){0,2}", value):
        return [int(part) for part in re.split(r"[-/.]", value)]
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return [parsed.year, parsed.month, parsed.day]
    numeric = re.fullmatch(r"(\d{1,2})[-/.](\d{4})", value)
    if numeric:
        return [int(numeric[2]), int(numeric[1])]
    numeric = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", value)
    if numeric:
        first, second, year = map(int, numeric.groups())
        if first <= 12 and second <= 12 and first != second:
            raise ValueError("ambiguous numeric date")
        return [year, second, first] if first > 12 else [year, first, second]
    tokens = re.sub(r"[,./-]+", " ", value.casefold()).split()
    months = [(index, _MONTHS[token]) for index, token in enumerate(tokens) if token in _MONTHS]
    if len(months) == 1 and len(tokens) in (2, 3):
        index, month = months[0]
        others = [token for i, token in enumerate(tokens) if i != index]
        if not all(token.isdigit() for token in others):
            raise ValueError("invalid named date")
        years = [int(token) for token in others if len(token) == 4]
        days = [int(token) for token in others if len(token) != 4]
        if len(years) == 1 and len(days) <= 1:
            return [years[0], month, *days]
    raise ValueError("unrecognized date")


def normalize_publication_date(value, year=None) -> dict:
    """Return ``publication_date``, ``date_precision``, and Japanese ``warnings``.

    Strings may be ISO dates, English month dates, Japanese dates, or unambiguous
    numeric dates. Crossref-style ``[year, month?, day?]`` is also supported.
    A supplied publication year is authoritative. Invalid/conflicting dates lose
    their month/day while the known year remains usable for annual analysis.
    """
    known = _known_year(year)
    fallback = {"publication_date": "", "date_precision": "year" if known else "unknown", "warnings": []}
    if value is None or value == "" or (isinstance(value, str) and value.strip().casefold() in {"", "n/a", "na", "unknown", "null", "none", "不明", "-"}):
        return fallback
    try:
        parts = _parts(value)
        if not 1500 <= parts[0] <= 2100:
            raise ValueError("invalid year")
        if len(parts) >= 2:
            # The temporary day here validates the calendar, never becomes output.
            date(parts[0], parts[1], parts[2] if len(parts) == 3 else 1)
    except (ValueError, TypeError, OverflowError):
        fallback["warnings"].append("公開日が不正または月日の順序が曖昧なため、月次分析の対象外にしました。既存の出版年は保持します。")
        return fallback
    if known is not None and parts[0] != known:
        fallback["warnings"].append("公開日と出版年が矛盾するため、月次分析の対象外にしました。既存の出版年は保持します。")
        return fallback
    if len(parts) == 1:
        return {"publication_date": "", "date_precision": "year", "warnings": []}
    return {"publication_date": "-".join(f"{part:04d}" if index == 0 else f"{part:02d}" for index, part in enumerate(parts)),
            "date_precision": "day" if len(parts) == 3 else "month", "warnings": []}


def normalize_paper_date(paper: dict) -> dict:
    """Normalize the three paper fields while respecting declared coarse precision."""
    result = normalize_publication_date(paper.get("publication_date"), paper.get("year"))
    declared = str(paper.get("date_precision") or "").strip().casefold()
    if declared in {"year", "unknown"}:
        result["publication_date"] = ""
        result["date_precision"] = "year" if _known_year(paper.get("year")) is not None else declared
    elif declared == "month" and result["date_precision"] == "day":
        result["publication_date"] = result["publication_date"][:7]
        result["date_precision"] = "month"
    result["date_source"] = str(paper.get("date_source") or "").strip()
    return result


def merge_publication_dates(first: dict, second: dict) -> tuple[dict, list[str]]:
    """Keep an existing explicit month; fill a year-only record from the same year."""
    left, right = normalize_paper_date(first), normalize_paper_date(second)
    warnings = [*left.pop("warnings"), *right.pop("warnings")]
    a, b = left["publication_date"], right["publication_date"]
    base_year = _known_year(first.get("year"))
    incoming_year = _known_year(second.get("year"))
    if base_year is not None and incoming_year is not None and base_year != incoming_year:
        warnings.append("重複論文の出版年が矛盾するため、既存の出版年・公開日・精度を保持しました。")
        return left, warnings
    if b:
        if base_year is None or incoming_year != base_year or int(b[:4]) != base_year:
            warnings.append("重複論文の公開日と既存出版年が矛盾するため、追加の日付を採用せず既存値を保持しました。")
        elif a and (a[:7] != b[:7] or (len(a) == len(b) == 10 and a != b)):
            warnings.append("重複論文の公開月または公開日が矛盾するため、既存の明示日付と精度を保持しました。")
        elif not a:
            left = right
    return left, warnings
