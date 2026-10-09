"""Resumable DOI-date enrichment; observations live beside immutable datasets."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import csv
import io
import json
import math
import re
import sqlite3
import threading

from . import large_storage, storage
from .dates import normalize_paper_date, normalize_publication_date
from .ingest import _date_summary
from .job_context import submit_with_context
from .publication_date_sources import CrossrefDateClient, extract_doi
from .reports import safe_cell

_LOCK = threading.RLock()
_ACTIVE: dict[str, tuple[str, threading.Event]] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlas-publication-dates")
TARGETS = {"missing_month", "missing_day", "all"}
POLICIES = {"same_year", "online_first", "print_first"}
_PRECISION = {"unknown": 0, "year": 1, "month": 2, "day": 3}
_KINDS = {"published-online", "published-print", "published", "issued"}


class JobBusy(ValueError):
    pass


def _db():
    db = sqlite3.connect(storage.data_root() / "publication_dates.sqlite", timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, target TEXT NOT NULL,
      batch_size INTEGER NOT NULL, status TEXT NOT NULL, initialized INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error_kind TEXT, retry_at TEXT,
      pause_requested INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS jobs_dataset ON jobs(dataset_id,created_at);
    CREATE TABLE IF NOT EXISTS observations (
      id TEXT PRIMARY KEY, doi TEXT NOT NULL, status TEXT NOT NULL,
      fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cache (doi TEXT PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES observations(id));
    CREATE TABLE IF NOT EXISTS items (
      job_id TEXT NOT NULL REFERENCES jobs(id), doi TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      observation_id TEXT REFERENCES observations(id), cached INTEGER NOT NULL DEFAULT 0,
      attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job_id,doi));
    CREATE INDEX IF NOT EXISTS items_pending ON items(job_id,status,doi);
    CREATE TABLE IF NOT EXISTS papers (
      job_id TEXT NOT NULL REFERENCES jobs(id), ordinal INTEGER NOT NULL,
      paper_id TEXT NOT NULL, title TEXT NOT NULL, doi TEXT, eligible INTEGER NOT NULL,
      original TEXT NOT NULL, PRIMARY KEY(job_id,ordinal));
    CREATE INDEX IF NOT EXISTS papers_doi ON papers(job_id,doi);
    CREATE TABLE IF NOT EXISTS snapshots (
      id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), created_at TEXT NOT NULL,
      policy TEXT NOT NULL, overwrite_existing INTEGER NOT NULL, dataset_id TEXT, summary TEXT);
    CREATE TABLE IF NOT EXISTS snapshot_items (
      snapshot_id TEXT NOT NULL REFERENCES snapshots(id), doi TEXT NOT NULL,
      observation_id TEXT NOT NULL REFERENCES observations(id), PRIMARY KEY(snapshot_id,doi));
    """)
    return db


def _root_key():
    return str(storage.data_root().resolve())


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _check_id(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise KeyError(identifier)


def _row(db, identifier):
    _check_id(identifier)
    row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise KeyError(identifier)
    return row


def _update(identifier, **fields):
    fields["updated_at"] = storage.now()
    with closing(_db()) as db, db:
        _row(db, identifier)
        db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", [*fields.values(), identifier])


def _recover(db):
    active = _ACTIVE.get(_root_key())
    active_id = active[0] if active else ""
    db.execute("UPDATE jobs SET status='paused',error_kind='interrupted',pause_requested=0,updated_at=? "
               "WHERE status IN ('preparing','running') AND id<>?", (storage.now(), active_id))


def _check_cooldown(db):
    # A new job must not bypass a provider's Retry-After after an app restart.
    retry_at = db.execute("SELECT MAX(retry_at) FROM jobs").fetchone()[0]
    if retry_at and retry_at > storage.now():
        raise JobBusy("提供元から待機を求められています。表示された再開可能時刻までお待ちください。")


def _summary(db, identifier):
    row = dict(_row(db, identifier))
    papers = db.execute("SELECT COUNT(*),COALESCE(SUM(eligible),0),COALESCE(SUM(eligible AND doi IS NULL),0) "
                        "FROM papers WHERE job_id=?", (identifier,)).fetchone()
    items = db.execute("SELECT COUNT(*),COALESCE(SUM(i.status<>'pending'),0),COALESCE(SUM(i.status='pending'),0),"
        "COALESCE(SUM(o.status='found'),0),COALESCE(SUM(o.status='not_found'),0),COALESCE(SUM(o.status='error'),0),"
        "COALESCE(SUM(i.cached),0) FROM items i LEFT JOIN observations o ON o.id=i.observation_id WHERE i.job_id=?",
        (identifier,)).fetchone()
    row.update(job_id=identifier, total_papers=papers[0], eligible_papers=papers[1], missing_doi_papers=papers[2],
               unique_dois=items[0], processed_dois=items[1], remaining_dois=items[2], found_dois=items[3],
               not_found_dois=items[4], error_dois=items[5], cached_dois=items[6], pause_requested=bool(row["pause_requested"]))
    row.pop("initialized")
    row["message"] = ({"rate_limited": "提供元の制限により一時停止しました。再開可能時刻以降に再開してください。",
        "blocked": "提供元がアクセスを保留しました。待機後、接続設定を確認して再開してください。",
        "interrupted": "アプリの終了などで中断しました。保存済みの結果から再開できます。",
        "preparation_or_storage_error": "対象データの準備または保存に失敗しました。データと保存先を確認してください。",
        "worker_unavailable": "取得処理を開始できませんでした。再開をお試しください。"}.get(row.get("error_kind")) or
        {"preparing": "対象論文と重複DOIを整理しています。", "running": "発行日の候補を取得しています。",
         "paused": "今回の取得を停止しました。未取得・再試行が必要なDOIは次の回で取得できます。",
         "completed": "今回の対象DOIをすべて処理しました。取得できなかった文献は候補一覧で確認できます。",
         "error": "取得処理を続行できませんでした。保存済みの情報は残っています。"}.get(row["status"], ""))
    return row


def get_job(identifier):
    with _LOCK, closing(_db()) as db, db:
        _recover(db)
        return _summary(db, identifier)


def dataset_summary(dataset_id):
    dataset = storage.read("datasets", dataset_id, include_papers=False)
    counts = dict(total_papers=0, month_or_day=0, day=0, missing_month=0, missing_day=0, no_doi=0)
    for paper in large_storage.iter_papers(dataset, storage.data_root()):
        precision = normalize_paper_date(paper)["date_precision"]
        counts["total_papers"] += 1
        counts["month_or_day"] += precision in {"month", "day"}
        counts["day"] += precision == "day"
        counts["missing_month"] += precision not in {"month", "day"}
        counts["missing_day"] += precision != "day"
        counts["no_doi"] += not bool(extract_doi(paper))
    with _LOCK, closing(_db()) as db, db:
        _recover(db)
        jobs = [_summary(db, row[0]) for row in db.execute("SELECT id FROM jobs WHERE dataset_id=? ORDER BY created_at DESC", (dataset_id,)).fetchall()]
    return {"dataset": storage.dataset_summary(dataset), "coverage": counts, "jobs": jobs}


def create_job(dataset_id, *, target="missing_month", batch_size=500, contact_email=""):
    storage.read("datasets", dataset_id, include_papers=False)
    if target not in TARGETS or isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 20000:
        raise ValueError("取得対象・1回の取得件数を確認してください。")
    with _LOCK:
        if _ACTIVE:
            raise JobBusy("発行日を取得中です。現在の取得が終わるか、一時停止してから開始してください。")
        identifier, stamp = storage.new_id(), storage.now()
        with closing(_db()) as db, db:
            _recover(db)
            _check_cooldown(db)
            db.execute("INSERT INTO jobs(id,dataset_id,target,batch_size,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (identifier, dataset_id, target, batch_size, "paused", stamp, stamp))
        return resume_job(identifier, batch_size=batch_size, contact_email=contact_email)


def resume_job(identifier, *, batch_size=500, contact_email=""):
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 20000:
        raise ValueError("1回の取得件数は1〜20,000件で指定してください。")
    with _LOCK, closing(_db()) as db, db:
        _recover(db)
        row = _row(db, identifier)
        if _ACTIVE:
            raise JobBusy("発行日を取得中です。現在の取得が終わるか、一時停止してから再開してください。")
        if row["status"] == "completed":
            return _summary(db, identifier)
        _check_cooldown(db)
        event = threading.Event()
        root = _root_key()
        _ACTIVE[root] = (identifier, event)
        db.execute("UPDATE jobs SET status=?,batch_size=?,pause_requested=0,error_kind=NULL,retry_at=NULL,updated_at=? WHERE id=?",
                   ("running" if row["initialized"] else "preparing", batch_size, storage.now(), identifier))
        db.commit()
        try:
            submit_with_context(_EXECUTOR, run_job, identifier, event, contact_email, root)
        except Exception:
            _ACTIVE.pop(root, None)
            _update(identifier, status="error", error_kind="worker_unavailable")
            raise ValueError("取得処理を開始できませんでした。再開をお試しください。") from None
        return _summary(db, identifier)


def pause_job(identifier):
    with _LOCK:
        job = get_job(identifier)
        active = _ACTIVE.get(_root_key())
        if active and active[0] == identifier:
            active[1].set()
            _update(identifier, pause_requested=1)
        return get_job(identifier)


def _prepare(identifier, event):
    with closing(_db()) as db:
        job = dict(_row(db, identifier))
    if job["initialized"]:
        return True
    dataset = storage.read("datasets", job["dataset_id"], include_papers=False)
    with closing(_db()) as db, db:
        db.execute("DELETE FROM papers WHERE job_id=?", (identifier,))
        db.execute("DELETE FROM items WHERE job_id=?", (identifier,))
    batch = []
    for ordinal, paper in enumerate(large_storage.iter_papers(dataset, storage.data_root())):
        if event.is_set():
            return False
        original = {"year": paper.get("year"), **normalize_paper_date(paper)}
        original.pop("warnings", None)
        precision = original["date_precision"]
        eligible = job["target"] == "all" or (precision != "day" if job["target"] == "missing_day" else precision not in {"month", "day"})
        batch.append((identifier, ordinal, str(paper["id"]), str(paper.get("title") or "")[:1000], extract_doi(paper) or None, int(eligible), _json(original)))
        if len(batch) >= 250:
            _insert_papers(batch)
            batch.clear()
    if batch:
        _insert_papers(batch)
    _update(identifier, initialized=1, status="running")
    return not event.is_set()


def _insert_papers(batch):
    with closing(_db()) as db, db:
        db.executemany("INSERT INTO papers VALUES(?,?,?,?,?,?,?)", batch)
        db.executemany("INSERT OR IGNORE INTO items(job_id,doi) VALUES(?,?)", [(row[0], row[4]) for row in batch if row[5] and row[4]])


def _reuse_cache(identifier):
    found_after = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    not_found_after = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with closing(_db()) as db, db:
        rows = db.execute("SELECT i.doi,o.id,o.status FROM items i JOIN cache c ON c.doi=i.doi JOIN observations o ON o.id=c.observation_id "
            "WHERE i.job_id=? AND i.status='pending' AND ((o.status='found' AND o.fetched_at>=?) OR (o.status='not_found' AND o.fetched_at>=?))",
            (identifier, found_after, not_found_after)).fetchall()
        db.executemany("UPDATE items SET observation_id=?,status=?,cached=1 WHERE job_id=? AND doi=?",
                       [(row[1], row[2], identifier, row[0]) for row in rows])


def _clean_observation(doi, value):
    """Store only the fixed, public metadata contract, never request settings/errors."""
    def clean_candidates(items):
        candidates = []
        for item in items[:20] if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("kind") not in _KINDS:
                continue
            normalized = normalize_publication_date(item.get("date"))
            precision = normalized["date_precision"]
            raw = str(item.get("date") or "")
            if normalized["warnings"] or precision == "unknown":
                continue
            candidates.append({"date": normalized["publication_date"] or raw[:4], "precision": precision, "kind": item["kind"]})
        return candidates
    candidates = clean_candidates(value.get("candidates", []))
    excluded = []
    for item in value.get("excluded_candidates", [])[:20]:
        if not isinstance(item, dict) or item.get("kind") not in _KINDS:
            continue
        if item.get("reason") == "future":
            excluded.extend({**clean, "reason": "future"} for clean in clean_candidates([item]))
    status = value.get("status") if value.get("status") in {"found", "not_found", "error"} else "error"
    if status == "found" and not candidates:
        status = "not_found"
    # Build the canonical source URL ourselves; a provider exception cannot leak a proxy URL or email.
    from urllib.parse import quote
    result = {"doi": doi, "status": status, "provider": "crossref", "source_url": "https://api.crossref.org/works/" + quote(doi, safe=""),
              "fetched_at": storage.now(), "candidates": candidates, "excluded_candidates": excluded,
              "retryable": bool(value.get("retryable", False)) if status == "error" else False}
    if status in {"error", "not_found"}:
        allowed = {"rate_limited", "timeout", "network_error", "http_error", "invalid_response", "cancelled", "server_error", "unavailable",
                   "invalid_doi", "not_found", "no_publication_date", "doi_mismatch", "response_too_large", "blocked", "redirect"}
        result["error_kind"] = value.get("error_kind") if value.get("error_kind") in allowed else "unavailable"
        delay = value.get("retry_after_seconds")
        if isinstance(delay, (int, float)) and not isinstance(delay, bool) and math.isfinite(delay) and delay > 0:
            result["retry_after_seconds"] = math.ceil(delay)
    return result


def _record(identifier, doi, value, *, db=None):
    if db is None:
        with closing(_db()) as connection:
            return _record(identifier, doi, value, db=connection)
    payload = _clean_observation(doi, value)
    observation_id = storage.new_id()
    with db:
        db.execute("INSERT INTO observations VALUES(?,?,?,?,?)", (observation_id, doi, payload["status"], payload["fetched_at"], _json(payload)))
        state = "pending" if payload["retryable"] else payload["status"]
        db.execute("UPDATE items SET status=?,observation_id=?,cached=0,attempts=attempts+1 WHERE job_id=? AND doi=?",
                   (state, observation_id, identifier, doi))
        if payload["status"] in {"found", "not_found"}:
            db.execute("INSERT INTO cache VALUES(?,?) ON CONFLICT(doi) DO UPDATE SET observation_id=excluded.observation_id", (doi, observation_id))
    return payload


def run_job(identifier, event, contact_email="", root=None):
    root = root or _root_key()
    try:
        if not _prepare(identifier, event):
            _update(identifier, status="paused", pause_requested=0)
            return
        _reuse_cache(identifier)
        with closing(_db()) as db:
            job = dict(_row(db, identifier))
            pending = [row[0] for row in db.execute("SELECT doi FROM items WHERE job_id=? AND status='pending' ORDER BY doi LIMIT ?", (identifier, job["batch_size"]))]
        cooldown, cooldown_kind = 0, None
        if pending and not event.is_set():
            # Keep a connection open across the batch so SQLite does not perform
            # a last-connection WAL checkpoint after every single DOI. Each
            # response still gets its own transaction, committed before fetching
            # another result; no transaction remains open during network waits.
            with CrossrefDateClient(contact_email=contact_email, stop_event=event) as client, closing(_db()) as write_db:
                concurrency = max(1, min(3, int(client.concurrency)))
                with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="atlas-date-fetch") as pool:
                    active, cursor = {}, 0
                    while active or (cursor < len(pending) and not event.is_set()):
                        while len(active) < concurrency and cursor < len(pending) and not event.is_set():
                            doi = pending[cursor]
                            cursor += 1
                            active[submit_with_context(pool, client.fetch, doi)] = doi
                        if not active:
                            break
                        completed, _ = wait(active, return_when=FIRST_COMPLETED)
                        for future in completed:
                            doi = active.pop(future)
                            try:
                                value = future.result()
                            except Exception:
                                value = {"status": "error", "error_kind": "unavailable", "retryable": True}
                            payload = _record(identifier, doi, value, db=write_db)
                            delay = payload.get("retry_after_seconds", 0)
                            if payload.get("error_kind") in {"rate_limited", "blocked"}:
                                delay = max(delay, 60)
                            if delay:
                                cooldown = max(cooldown, delay)
                                cooldown_kind = payload.get("error_kind")
                                event.set()
        with closing(_db()) as db:
            remaining = db.execute("SELECT COUNT(*) FROM items WHERE job_id=? AND status='pending'", (identifier,)).fetchone()[0]
        retry_at = None
        if cooldown:
            try:
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=cooldown)).isoformat()
            except OverflowError:
                retry_at = datetime.max.replace(tzinfo=timezone.utc).isoformat()
        _update(identifier, status="paused" if remaining else "completed", pause_requested=0,
                error_kind=cooldown_kind, retry_at=retry_at)
    except Exception:
        _update(identifier, status="error", error_kind="preparation_or_storage_error", pause_requested=0)
    finally:
        with _LOCK:
            active = _ACTIVE.get(root)
            if active and active[0] == identifier:
                _ACTIVE.pop(root, None)


def choose_date(original, candidates, *, policy="same_year", overwrite_existing=False):
    if policy not in POLICIES:
        raise ValueError("日付の採用方針を確認してください。")
    year = original.get("year")
    existing = original.get("publication_date") or ""
    precision = _PRECISION.get(original.get("date_precision"), 0)
    usable = [item for item in candidates if item.get("precision") in {"month", "day"}]
    cross_year = bool(year and any(str(item["date"])[:4] != str(year) for item in usable))
    eligible = [item for item in usable if not year or policy != "same_year" or str(item["date"])[:4] == str(year)]
    kinds = ["published-online", "published-print", "published", "issued"]
    if policy == "print_first":
        kinds = ["published-print", "published-online", "published", "issued"]
    if policy == "same_year":
        eligible.sort(key=lambda item: (-_PRECISION[item["precision"]], kinds.index(item["kind"]), item["date"]))
    else:
        eligible.sort(key=lambda item: (kinds.index(item["kind"]), -_PRECISION[item["precision"]], item["date"]))
    # When filling a known month, use a compatible day even if another date kind
    # ranked first. Preserve disagreement in the audit instead of discarding it.
    if existing and not overwrite_existing:
        compatible = [item for item in eligible if item["date"].startswith(existing) and _PRECISION[item["precision"]] > precision]
        if compatible:
            eligible = compatible
    selected = eligible[0] if eligible else None
    conflict = cross_year or any(not a["date"].startswith(b["date"]) and not b["date"].startswith(a["date"]) for a in usable for b in usable)
    if not selected:
        return {"candidate": None, "apply": False, "conflict": conflict, "reason": "year_conflict" if cross_year else "no_precise_date"}
    if not overwrite_existing:
        if _PRECISION[selected["precision"]] <= precision:
            return {"candidate": selected, "apply": False, "conflict": conflict or bool(existing and selected["date"] != existing), "reason": "existing_date_kept"}
        if existing and not selected["date"].startswith(existing):
            return {"candidate": selected, "apply": False, "conflict": True, "reason": "existing_date_conflict"}
    changed = selected["date"] != existing or selected["precision"] != original.get("date_precision")
    return {"candidate": selected, "apply": changed, "conflict": conflict, "reason": "selected" if changed else "already_present"}


def _record_view(row, policy, overwrite_existing):
    original = json.loads(row["original"])
    observation = json.loads(row["payload"]) if row["payload"] else None
    candidates = observation["candidates"] if observation else []
    proposal = choose_date(original, candidates, policy=policy, overwrite_existing=overwrite_existing)
    if not row["eligible"]:
        proposal.update(apply=False, reason="outside_target")
    return {"paper_id": row["paper_id"], "title": row["title"], "doi": row["doi"], "eligible": bool(row["eligible"]),
            "original": original, "status": observation["status"] if observation else ("outside_target" if not row["eligible"] else "pending" if row["doi"] else "no_doi"),
            "observation_id": row["observation_id"], "observation": observation, "candidates": candidates,
            "proposed": proposal, "conflict": proposal["conflict"]}


_RECORD_SQL = "SELECT p.*,i.observation_id,o.payload FROM papers p LEFT JOIN items i ON i.job_id=p.job_id AND i.doi=p.doi LEFT JOIN observations o ON o.id=i.observation_id WHERE p.job_id=? ORDER BY p.ordinal"


def records(identifier, *, limit=50, offset=0, policy="same_year", overwrite_existing=False):
    if policy not in POLICIES:
        raise ValueError("日付の採用方針を確認してください。")
    with closing(_db()) as db:
        _row(db, identifier)
        total = db.execute("SELECT COUNT(*) FROM papers WHERE job_id=?", (identifier,)).fetchone()[0]
        items = [_record_view(row, policy, overwrite_existing) for row in db.execute(_RECORD_SQL + " LIMIT ? OFFSET ?", (identifier, limit, offset))]
    return {"items": items, "total": total, "offset": offset, "limit": limit, "policy": policy, "overwrite_existing": overwrite_existing}


def export_csv(identifier, *, policy="same_year", overwrite_existing=False):
    get_job(identifier)
    def chunks():
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        header = ["job_id", "paper_id", "title", "doi", "original_year", "original_date", "original_precision", "status", "observation_id",
                  "candidate_date", "candidate_precision", "candidate_kind", "source_url", "fetched_at", "policy", "proposed_date", "apply", "conflict", "reason", "excluded_candidates", "error_kind"]
        writer.writerow(header)
        yield "\ufeff" + buffer.getvalue()
        buffer.seek(0); buffer.truncate(0)
        with closing(_db()) as db:
            for row in db.execute(_RECORD_SQL, (identifier,)):
                item = _record_view(row, policy, overwrite_existing)
                observation, proposal, original = item["observation"] or {}, item["proposed"], item["original"]
                for candidate in item["candidates"] or [{}]:
                    values = [identifier, item["paper_id"], item["title"], item["doi"], original.get("year"), original.get("publication_date"), original.get("date_precision"),
                        item["status"], item["observation_id"], candidate.get("date"), candidate.get("precision"), candidate.get("kind"), observation.get("source_url"), observation.get("fetched_at"),
                        policy, (proposal.get("candidate") or {}).get("date"), proposal["apply"], proposal["conflict"], proposal["reason"],
                        _json(observation.get("excluded_candidates", [])), observation.get("error_kind", "")]
                    writer.writerow([safe_cell(value) for value in values])
                if buffer.tell() > 32768:
                    yield buffer.getvalue()
                    buffer.seek(0); buffer.truncate(0)
        if buffer.tell():
            yield buffer.getvalue()
    return chunks()


def apply_job(identifier, *, policy="same_year", overwrite_existing=False):
    if policy not in POLICIES or not isinstance(overwrite_existing, bool):
        raise ValueError("日付の採用方針を確認してください。")
    with _LOCK, closing(_db()) as db, db:
        _recover(db)
        job = dict(_row(db, identifier))
        if not job["initialized"]:
            raise JobBusy("対象論文の準備が終わってから日付を反映してください。")
        snapshot_id, stamp = storage.new_id(), storage.now()
        db.execute("INSERT INTO snapshots(id,job_id,created_at,policy,overwrite_existing) VALUES(?,?,?,?,?)", (snapshot_id, identifier, stamp, policy, int(overwrite_existing)))
        db.execute("INSERT INTO snapshot_items SELECT ?,doi,observation_id FROM items WHERE job_id=? AND observation_id IS NOT NULL", (snapshot_id, identifier))
        observations = {row["doi"]: {**json.loads(row["payload"]), "id": row["observation_id"]} for row in db.execute(
            "SELECT s.doi,s.observation_id,o.payload FROM snapshot_items s JOIN observations o ON o.id=s.observation_id WHERE s.snapshot_id=?", (snapshot_id,))}
        eligible_ids = {row[0] for row in db.execute("SELECT paper_id FROM papers WHERE job_id=? AND eligible=1", (identifier,))}
    dataset = storage.read("datasets", job["dataset_id"], include_papers=False)
    papers, applied, conflicts, changed_years = [], 0, 0, 0
    for source in large_storage.iter_papers(dataset, storage.data_root()):
        paper = deepcopy(source)
        observation = observations.get(extract_doi(paper)) if str(paper["id"]) in eligible_ids else None
        if observation:
            original = {"year": paper.get("year"), **normalize_paper_date(paper)}
            original.pop("warnings", None)
            proposal = choose_date(original, observation["candidates"], policy=policy, overwrite_existing=overwrite_existing)
            conflicts += proposal["conflict"]
            if proposal["apply"]:
                selected = proposal["candidate"]
                new_year = int(selected["date"][:4])
                changed_years += new_year != paper.get("year")
                paper.update(year=new_year, publication_date=selected["date"], date_precision=selected["precision"], date_source="crossref:" + selected["kind"])
                paper["date_enrichment"] = {"job_id": identifier, "snapshot_id": snapshot_id, "observation_id": observation["id"],
                    "provider": observation["provider"], "source_url": observation["source_url"], "kind": selected["kind"], "fetched_at": observation["fetched_at"], "policy": policy, "original": original}
                applied += 1
        papers.append(paper)
    report = deepcopy(dataset.get("report") or {})
    audit = {"job_id": identifier, "snapshot_id": snapshot_id, "parent_dataset_id": dataset["id"], "created_at": stamp,
             "policy": policy, "overwrite_existing": overwrite_existing, "applied_count": applied, "conflict_count": conflicts,
             "changed_year_count": changed_years, "unchanged_count": len(papers) - applied,
             "observation_count": len(observations), "observations_store": "publication_dates.sqlite"}
    report.update(_date_summary(papers))
    report.update(date_enrichment=audit, parent_dataset_id=dataset["id"])
    derived = storage.create_dataset(papers, dataset["name"] + " · 発行日補完", is_demo=bool(dataset.get("is_demo")), report=report)
    with closing(_db()) as db, db:
        db.execute("UPDATE snapshots SET dataset_id=?,summary=? WHERE id=?", (derived["id"], _json(audit), snapshot_id))
    return {"dataset_id": derived["id"], "dataset": storage.dataset_summary(derived), **audit}
