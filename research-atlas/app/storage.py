"""Small immutable local dataset/result store; never persists API keys."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = threading.RLock()


def data_root() -> Path:
    path = Path(os.getenv("ATLAS_DATA_DIR", str(ROOT / "data")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(kind: str, identifier: str) -> Path:
    if kind not in {"datasets", "results", "field_reports", "author_networks", "assessments"} or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise KeyError("データが見つかりません。")
    folder = data_root() / kind
    folder.mkdir(exist_ok=True)
    return folder / f"{identifier}.json"


def save(kind: str, value: dict) -> dict:
    path = _path(kind, value["id"])
    with LOCK:
        temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return value


def read(kind: str, identifier: str) -> dict:
    with LOCK:
        try:
            return json.loads(_path(kind, identifier).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise KeyError("データが見つかりません。") from exc


def _summary_year(value) -> int | None:
    """Accept stored integer years without coercing booleans or fractional values."""
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}", text):
        return None
    year = int(text)
    return year if 1900 <= year <= 2100 else None


def _summary_period(start, end, current_year: int) -> tuple[int, int] | None:
    start, end = _summary_year(start), _summary_year(end)
    if start is None or end is None or start > end or start > current_year:
        return None
    return start, min(end, current_year)


def dataset_summary(value: dict) -> dict:
    result = {key: value[key] for key in ("id", "name", "paper_count", "is_demo", "created_at")}
    report = value.get("report") if isinstance(value.get("report"), dict) else {}
    request = report.get("discovery_request") if isinstance(report.get("discovery_request"), dict) else {}
    current_year = date.today().year
    # A search's declared scope includes years with zero retrieved papers.
    # Retain it instead of narrowing the range to the observed publication years.
    period = (_summary_period(request.get("start_year"), request.get("end_year"), current_year)
              or _summary_period(report.get("start_year"), report.get("end_year"), current_year))
    if period is None:
        papers = value.get("papers") if isinstance(value.get("papers"), (list, tuple)) else []
        years = [year for paper in papers if isinstance(paper, dict)
                 and (year := _summary_year(paper.get("year"))) is not None]
        if years:
            period = _summary_period(min(years), max(years), current_year)
    start, end = period or (current_year - 5, current_year - 1)
    if end - start > 19:
        start = end - 19
        result["analysis_defaults_note"] = "分析APIの期間上限に合わせ、初期期間を最後の20暦年に絞っています。"
    result["analysis_defaults"] = {"start_year": start, "end_year": end}
    start_month = request.get("start_month") or report.get("start_month")
    end_month = request.get("end_month") or report.get("end_month")
    valid_month = lambda month: isinstance(month, str) and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month)
    if request and valid_month(start_month) and valid_month(end_month) and start_month <= end_month:
        latest = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        anchor = min(end_month, latest)
        result["analysis_defaults"].update(window_months=3,
            anchor_month=anchor if start <= int(anchor[:4]) <= end else None)
    return result


def list_datasets() -> list[dict]:
    folder = data_root() / "datasets"
    if not folder.exists():
        return []
    values = []
    with LOCK:
        for path in folder.glob("*.json"):
            try:
                values.append(dataset_summary(json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, KeyError, OSError):
                continue
    return sorted(values, key=lambda v: v["created_at"], reverse=True)


def create_dataset(papers: list[dict], name: str, is_demo: bool = False, report: dict | None = None) -> dict:
    return save("datasets", {"id": new_id(), "name": name[:200], "is_demo": is_demo,
        "created_at": now(), "paper_count": len(papers), "papers": papers, "report": report or {}})
