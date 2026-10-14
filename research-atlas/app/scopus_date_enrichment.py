"""Offline Scopus browser observations, joined by EID to immutable datasets.

This module never opens Scopus or stores browser credentials. A bounded browser
queue and its result file are the only bridge to a user's authenticated browser.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import re
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

from . import author_paging, large_paging, large_storage, storage
from .dates import normalize_paper_date, normalize_publication_date
from .reports import safe_cell

TARGETS = {"missing_month", "missing_day", "all"}
STATUSES = {"found", "requires_access", "not_found", "selector_changed", "identity_mismatch", "timeout", "error"}
_LOCK = threading.RLock()
_PRECISION = {"unknown": 0, "year": 1, "month": 2, "day": 3}
QUEUE_SCHEMA = "research-atlas.scopus-date-queue.v1"
RESULT_SCHEMA = "research-atlas.scopus-date-results.v1"


class SnapshotConflict(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise KeyError("データが見つかりません。")
    return value


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def canonical_eid(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(?:scopus:)?(2-s2\.0-[1-9]\d{0,19})", value.strip(), re.I)
    return match[1].lower() if match else None


def canonical_url(eid):
    return "https://www.scopus.com/pages/publications/" + eid.removeprefix("2-s2.0-") + "?origin=resultslist"


def url_eid(value):
    """Accept fixed Scopus record routes only; reject login/redirect/proxy URLs."""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in {"scopus.com", "www.scopus.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.fragment):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        eids = {canonical_eid(item) for item in query.get("eid", [])}
        if None in eids or len(eids) > 1:
            return None
        if match := re.fullmatch(r"/pages/publications/([1-9]\d{0,19})/?", parsed.path):
            result = "2-s2.0-" + match[1]
            return result if not eids or eids == {result} else None
        if parsed.path.lower() in {"/inward/record.uri", "/record/display.uri", "/inward/record/display.uri"} and len(eids) == 1:
            return next(iter(eids))
    except (ValueError, UnicodeError):
        pass
    return None


def paper_identity(paper):
    values = set()
    if eid := canonical_eid(paper.get("id")):
        values.add(eid)
    aliases = paper.get("aliases") if isinstance(paper.get("aliases"), dict) else {}
    for value in aliases.get("eids", []) if isinstance(aliases.get("eids"), list) else []:
        if eid := canonical_eid(value):
            values.add(eid)
    invalid_scopus_link = False
    for key in ("source_link", "external_url"):
        value = paper.get(key)
        if not value:
            continue
        if eid := url_eid(value):
            values.add(eid)
        else:
            try:
                invalid_scopus_link |= urlsplit(str(value)).hostname in {"scopus.com", "www.scopus.com"}
            except ValueError:
                pass
    if len(values) > 1:
        return {"eid": None, "url": None, "reason": "identity_conflict"}
    if invalid_scopus_link:
        return {"eid": None, "url": None, "reason": "invalid_scopus_url"}
    if not values:
        return {"eid": None, "url": None, "reason": "no_eid"}
    eid = next(iter(values))
    return {"eid": eid, "url": canonical_url(eid), "reason": "matched"}


def _db():
    db = sqlite3.connect(storage.data_root() / "scopus_publication_dates.sqlite", timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,dataset_id TEXT NOT NULL,target TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS jobs_dataset ON jobs(dataset_id,created_at);
      CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,eid TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,imported_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS cache(eid TEXT PRIMARY KEY,observation_id TEXT NOT NULL REFERENCES observations(id));
      CREATE TABLE IF NOT EXISTS papers(job_id TEXT NOT NULL REFERENCES jobs(id),ordinal INTEGER NOT NULL,paper_id TEXT NOT NULL,title TEXT NOT NULL,eid TEXT,url TEXT,eligible INTEGER NOT NULL,identity_reason TEXT NOT NULL,original TEXT NOT NULL,PRIMARY KEY(job_id,ordinal));
      CREATE INDEX IF NOT EXISTS papers_eid ON papers(job_id,eid);
      CREATE TABLE IF NOT EXISTS items(job_id TEXT NOT NULL REFERENCES jobs(id),eid TEXT NOT NULL,status TEXT NOT NULL,observation_id TEXT REFERENCES observations(id),PRIMARY KEY(job_id,eid));
      CREATE INDEX IF NOT EXISTS items_status ON items(job_id,status,eid);
      CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS batch_items(batch_id TEXT NOT NULL REFERENCES batches(id),eid TEXT NOT NULL,PRIMARY KEY(batch_id,eid));
      CREATE TABLE IF NOT EXISTS imports(batch_id TEXT NOT NULL REFERENCES batches(id),observation_id TEXT NOT NULL REFERENCES observations(id),PRIMARY KEY(batch_id,observation_id));
      CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),created_at TEXT NOT NULL,today TEXT NOT NULL,allow_year_change INTEGER NOT NULL,overwrite_existing INTEGER NOT NULL,dataset_id TEXT,summary TEXT);
      CREATE TABLE IF NOT EXISTS snapshot_items(snapshot_id TEXT NOT NULL REFERENCES snapshots(id),eid TEXT NOT NULL,status TEXT NOT NULL,observation_id TEXT REFERENCES observations(id),PRIMARY KEY(snapshot_id,eid));
    """)
    return db


def _job(db, identifier):
    row = db.execute("SELECT * FROM jobs WHERE id=?", (_id(identifier),)).fetchone()
    if row is None:
        raise KeyError("取得ジョブが見つかりません。")
    return row


def _summary(db, identifier):
    value = dict(_job(db, identifier))
    counts = db.execute("SELECT COUNT(*),COALESCE(SUM(eligible),0),COALESCE(SUM(identity_reason='no_eid'),0),COALESCE(SUM(identity_reason IN ('identity_conflict','invalid_scopus_url')),0) FROM papers WHERE job_id=?", (identifier,)).fetchone()
    states = {row[0]: row[1] for row in db.execute("SELECT status,COUNT(*) FROM items WHERE job_id=? GROUP BY status", (identifier,))}
    total, pending, found = sum(states.values()), states.get("pending", 0), states.get("found", 0)
    return {**value, "job_id": identifier, "status": "ready" if pending else "completed",
            "total_papers": counts[0], "eligible_papers": counts[1], "no_eid_papers": counts[2], "identity_conflicts": counts[3],
            "unique_eids": total, "pending_eids": pending, "remaining_eids": pending, "found_eids": found,
            "processed_eids": total - pending, "error_eids": total - pending - found, "status_counts": states}


def get_job(identifier):
    with closing(_db()) as db:
        return _summary(db, identifier)


def dataset_summary(dataset_id):
    dataset = storage.read("datasets", _id(dataset_id), include_papers=False)
    counts = dict.fromkeys(("total_papers", "month_or_day", "day", "missing_month", "missing_day", "eligible_identity", "no_eid", "identity_conflicts"), 0)
    for paper in large_storage.iter_papers(dataset, storage.data_root()):
        precision, identity = normalize_paper_date(paper)["date_precision"], paper_identity(paper)
        counts["total_papers"] += 1
        counts["month_or_day"] += precision in {"month", "day"}
        counts["day"] += precision == "day"
        counts["missing_month"] += precision not in {"month", "day"}
        counts["missing_day"] += precision != "day"
        counts["eligible_identity"] += identity["eid"] is not None
        counts["no_eid"] += identity["reason"] == "no_eid"
        counts["identity_conflicts"] += identity["reason"] in {"identity_conflict", "invalid_scopus_url"}
    with closing(_db()) as db:
        jobs = [_summary(db, row[0]) for row in db.execute("SELECT id FROM jobs WHERE dataset_id=? ORDER BY created_at DESC", (dataset_id,)).fetchall()]
    return {"dataset": storage.dataset_summary(dataset), "coverage": counts, "jobs": jobs}


def create_job(dataset_id, *, target="missing_month"):
    if target not in TARGETS:
        raise ValueError("取得対象を確認してください。")
    dataset = storage.read("datasets", _id(dataset_id), include_papers=False)
    identifier, stamp = storage.new_id(), storage.now()
    with _LOCK, closing(_db()) as db, db:
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (identifier, dataset_id, target, stamp, stamp))
        for ordinal, paper in enumerate(large_storage.iter_papers(dataset, storage.data_root())):
            original = {"year": paper.get("year"), **normalize_paper_date(paper)}
            original.pop("warnings", None)
            identity, precision = paper_identity(paper), original["date_precision"]
            eligible = target == "all" or (precision != "day" if target == "missing_day" else precision not in {"month", "day"})
            db.execute("INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?)", (identifier, ordinal, str(paper["id"]), str(paper.get("title") or ""), identity["eid"], identity["url"], int(eligible), identity["reason"], _json(original)))
            if eligible and identity["eid"]:
                cached = db.execute("SELECT observation_id FROM cache WHERE eid=?", (identity["eid"],)).fetchone()
                db.execute("INSERT OR IGNORE INTO items VALUES(?,?,?,?)", (identifier, identity["eid"], "found" if cached else "pending", cached[0] if cached else None))
        return _summary(db, identifier)


def queue(identifier, *, batch_size=100, retry_errors=False):
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 500 or not isinstance(retry_errors, bool):
        raise ValueError("1回の取得件数は1〜500件で指定してください。")
    with _LOCK, closing(_db()) as db, db:
        _job(db, identifier)
        rows = db.execute("SELECT i.eid,MIN(p.title) AS title FROM items i JOIN papers p ON p.job_id=i.job_id AND p.eid=i.eid WHERE i.job_id=? AND (i.status='pending' OR (? AND i.status<>'found')) GROUP BY i.eid ORDER BY MIN(p.ordinal) LIMIT ?", (identifier, int(retry_errors), batch_size)).fetchall()
        batch_id, stamp = storage.new_id(), storage.now()
        db.execute("INSERT INTO batches VALUES(?,?,?)", (batch_id, identifier, stamp))
        db.executemany("INSERT INTO batch_items VALUES(?,?)", ((batch_id, row["eid"]) for row in rows))
        return {"schema": QUEUE_SCHEMA, "job_id": identifier, "batch_id": batch_id, "created_at": stamp,
                "entries": [{"eid": row["eid"], "url": canonical_url(row["eid"]), "title": row["title"][:1000]} for row in rows]}


def _observation(value):
    if not isinstance(value, dict) or set(value) - {"eid", "url", "status", "date_text", "matched_by", "observed_at"}:
        raise ValueError("取得結果の形式を確認してください。")
    eid, url, status = value.get("eid"), value.get("url"), value.get("status")
    if canonical_eid(eid) != eid or not isinstance(url, str) or len(url) > 2048 or status not in STATUSES:
        raise ValueError("取得結果のEID・URL・状態を確認してください。")
    text, matched, observed = value.get("date_text", ""), value.get("matched_by", ""), value.get("observed_at")
    if not isinstance(text, str) or len(text) > 512 or matched not in {"", "testid", "label"} or not isinstance(observed, str) or len(observed) > 50:
        raise ValueError("日付原文・照合方法・取得日時を確認してください。")
    try:
        stamp = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError("取得日時はタイムゾーン付きISO形式で指定してください。") from None
    payload = {"eid": eid, "url": url, "status": status, "date_text": text, "matched_by": matched,
               "observed_at": observed, "provider": "scopus", "kind": "publication-date", "candidate": None, "validation_error": None}
    if url_eid(url) != eid:
        payload["validation_error"] = "identity_mismatch"
        if status == "found":
            payload["status"] = "identity_mismatch"
    elif status == "found":
        normalized = normalize_publication_date(text)
        if not matched:
            payload["validation_error"] = "missing_match_method"
        elif normalized["date_precision"] not in {"month", "day"}:
            payload["validation_error"] = "year_only" if normalized["date_precision"] == "year" else "invalid_date"
        else:
            payload["candidate"] = {"date": normalized["publication_date"], "precision": normalized["date_precision"], "kind": "publication-date"}
    return payload


def import_results(identifier, payload):
    if (not isinstance(payload, dict) or set(payload) != {"schema", "job_id", "batch_id", "observations"}
            or payload["schema"] != RESULT_SCHEMA or payload["job_id"] != identifier
            or not isinstance(payload["observations"], list) or not 1 <= len(payload["observations"]) <= 500):
        raise ValueError("このジョブの取得結果JSONを選択してください（1〜500件）。")
    batch_id = _id(payload["batch_id"])
    observations = [_observation(row) for row in payload["observations"]]
    if len({row["eid"] for row in observations}) != len(observations):
        raise ValueError("1つの結果ファイルに同じEIDを複数回含めないでください。")
    with _LOCK, closing(_db()) as db, db:
        _job(db, identifier)
        batch = db.execute("SELECT job_id FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch[0] != identifier:
            raise ValueError("取得キューと結果ファイルのジョブが一致しません。")
        members = {row[0] for row in db.execute("SELECT eid FROM batch_items WHERE batch_id=?", (batch_id,))}
        if any(row["eid"] not in members for row in observations):
            raise ValueError("取得キューに含まれないEIDがあります。元の結果ファイルを確認してください。")
        imported, duplicates = 0, 0
        for row in observations:
            data = _json(row)
            oid = hashlib.sha256(data.encode("utf-8")).hexdigest()
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)", (oid, row["eid"], row["status"], data, storage.now()))
            inserted = db.execute("INSERT OR IGNORE INTO imports VALUES(?,?)", (batch_id, oid)).rowcount
            if not inserted:
                duplicates += 1
                continue
            imported += 1
            effective_status = "error" if row["validation_error"] and row["status"] == "found" else row["status"]
            current = db.execute("SELECT i.status,i.observation_id,o.payload FROM items i LEFT JOIN observations o ON o.id=i.observation_id WHERE i.job_id=? AND i.eid=?", (identifier, row["eid"])).fetchone()
            previous = json.loads(current["payload"]) if current["payload"] else None
            newer = not previous or datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")) > datetime.fromisoformat(previous["observed_at"].replace("Z", "+00:00"))
            # A later error must never discard a successfully observed date.
            if current["status"] != "found" or (effective_status == "found" and newer):
                db.execute("UPDATE items SET status=?,observation_id=? WHERE job_id=? AND eid=?", (effective_status, oid, identifier, row["eid"]))
            if effective_status == "found":
                cached = db.execute("SELECT o.payload FROM cache c JOIN observations o ON o.id=c.observation_id WHERE c.eid=?", (row["eid"],)).fetchone()
                if not cached or datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")) > datetime.fromisoformat(json.loads(cached[0])["observed_at"].replace("Z", "+00:00")):
                    db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (row["eid"], oid))
        db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (storage.now(), identifier))
        return {**_summary(db, identifier), "imported": imported, "duplicates": duplicates}


def _snapshot(db, identifier, snapshot_id, allow_year_change, overwrite_existing):
    _job(db, identifier)
    if not isinstance(allow_year_change, bool) or not isinstance(overwrite_existing, bool):
        raise ValueError("採用方針を確認してください。")
    if snapshot_id:
        row = db.execute("SELECT * FROM snapshots WHERE id=? AND job_id=?", (_id(snapshot_id), identifier)).fetchone()
        if row is None:
            raise KeyError("候補スナップショットが見つかりません。")
        if bool(row["allow_year_change"]) != allow_year_change or bool(row["overwrite_existing"]) != overwrite_existing:
            raise SnapshotConflict("採用方針がプレビュー時と異なります。候補を再表示してください。")
        return dict(row)
    snapshot_id = storage.new_id()
    db.execute("INSERT INTO snapshots(id,job_id,created_at,today,allow_year_change,overwrite_existing) VALUES(?,?,?,?,?,?)", (snapshot_id, identifier, storage.now(), _today(), int(allow_year_change), int(overwrite_existing)))
    db.execute("INSERT INTO snapshot_items SELECT ?,eid,status,observation_id FROM items WHERE job_id=?", (snapshot_id, identifier))
    return dict(db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone())


def choose_date(original, observation, *, allow_year_change=False, overwrite_existing=False, today=None):
    candidate = (observation or {}).get("candidate")
    answer = {"candidate": candidate, "apply": False, "conflict": False, "reason": "no_precise_date"}
    if not candidate or (observation or {}).get("validation_error"):
        answer["reason"] = (observation or {}).get("validation_error") or "no_precise_date"
        return answer
    value, precision = candidate["date"], candidate["precision"]
    existing = original.get("publication_date") or ""
    today = today or _today()
    if value > today[:len(value)]:
        return {**answer, "conflict": True, "reason": "future_date"}
    if original.get("year") and str(original["year"]) != value[:4]:
        answer["conflict"] = True
        if not allow_year_change:
            return {**answer, "reason": "year_conflict"}
    if not overwrite_existing:
        if _PRECISION[precision] <= _PRECISION.get(original.get("date_precision"), 0):
            return {**answer, "conflict": answer["conflict"] or bool(existing and existing != value), "reason": "existing_date_kept"}
        if existing and not value.startswith(existing):
            return {**answer, "conflict": True, "reason": "existing_date_conflict"}
    changed = existing != value or original.get("date_precision") != precision
    return {**answer, "apply": changed, "reason": "selected" if changed else "already_present"}


_RECORD_SQL = "SELECT p.*,s.status AS item_status,s.observation_id,o.payload FROM papers p LEFT JOIN snapshot_items s ON s.snapshot_id=? AND s.eid=p.eid LEFT JOIN observations o ON o.id=s.observation_id WHERE p.job_id=? ORDER BY p.ordinal"


def _record(row, snapshot):
    observation = json.loads(row["payload"]) if row["payload"] else None
    original = json.loads(row["original"])
    proposal = choose_date(original, observation, allow_year_change=bool(snapshot["allow_year_change"]), overwrite_existing=bool(snapshot["overwrite_existing"]), today=snapshot["today"])
    if not row["eligible"] or row["identity_reason"] != "matched":
        proposal.update(apply=False, reason="outside_target" if not row["eligible"] else row["identity_reason"])
    return {"paper_id": row["paper_id"], "title": row["title"], "eid": row["eid"], "url": row["url"],
            "eligible": bool(row["eligible"]), "identity_reason": row["identity_reason"], "original": original,
            "status": row["item_status"] or ("outside_target" if not row["eligible"] else row["identity_reason"] if not row["eid"] else "pending"),
            "observation_id": row["observation_id"], "observation": observation, "proposed": proposal, "conflict": proposal["conflict"]}


def records(identifier, *, offset=0, limit=50, allow_year_change=False, overwrite_existing=False, snapshot_id=None):
    if isinstance(offset, bool) or isinstance(limit, bool) or not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 100:
        raise ValueError("表示範囲を確認してください。")
    with _LOCK, closing(_db()) as db, db:
        snapshot = _snapshot(db, identifier, snapshot_id, allow_year_change, overwrite_existing)
        total = db.execute("SELECT COUNT(*) FROM papers WHERE job_id=?", (identifier,)).fetchone()[0]
        items = [_record(row, snapshot) for row in db.execute(_RECORD_SQL + " LIMIT ? OFFSET ?", (snapshot["id"], identifier, limit, offset))]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "snapshot_id": snapshot["id"], "allow_year_change": allow_year_change, "overwrite_existing": overwrite_existing}


def export_records(identifier, *, format="csv", allow_year_change=False, overwrite_existing=False, snapshot_id=None):
    if format not in {"csv", "json"}:
        raise ValueError("出力形式はCSVまたはJSONを指定してください。")
    with _LOCK, closing(_db()) as db, db:
        snapshot = _snapshot(db, identifier, snapshot_id, allow_year_change, overwrite_existing)
    def chunks():
        # A read transaction keeps this stream's audit consistent during imports.
        with closing(_db()) as db:
            db.execute("BEGIN")
            sql = "SELECT b.id AS batch_id,o.id AS observation_id,o.imported_at,o.payload FROM batches b JOIN imports i ON i.batch_id=b.id JOIN observations o ON o.id=i.observation_id WHERE b.job_id=? ORDER BY o.imported_at,b.id,o.id"
            if format == "json":
                yield '{"schema":"research-atlas.scopus-date-audit.v1","job":' + _json(_summary(db, identifier)) + ',"snapshot":' + _json(snapshot) + ',"records":['
                first = True
                for row in db.execute(_RECORD_SQL, (snapshot["id"], identifier)):
                    yield ("" if first else ",") + _json(_record(row, snapshot))
                    first = False
                yield '],"observations":['
                first = True
                for row in db.execute(sql, (identifier,)):
                    item = {**json.loads(row["payload"]), "batch_id": row["batch_id"], "observation_id": row["observation_id"], "imported_at": row["imported_at"]}
                    yield ("" if first else ",") + _json(item)
                    first = False
                yield "]}"
            else:
                buffer = io.StringIO(newline="")
                writer = csv.writer(buffer)
                fields = ["row_type", "job_id", "snapshot_id", "batch_id", "observation_id", "paper_id", "title", "eid", "url", "status", "date_text", "matched_by", "observed_at", "imported_at", "original_year", "original_date", "original_precision", "candidate_date", "candidate_precision", "apply", "reason", "validation_error"]
                writer.writerow(fields)
                yield "\ufeff" + buffer.getvalue()
                buffer.seek(0); buffer.truncate(0)
                for row in db.execute(_RECORD_SQL, (snapshot["id"], identifier)):
                    item = _record(row, snapshot)
                    observation, candidate, original = item["observation"] or {}, item["proposed"]["candidate"] or {}, item["original"]
                    values = ["record", identifier, snapshot["id"], "", item["observation_id"], item["paper_id"], item["title"], item["eid"], item["url"], item["status"], observation.get("date_text"), observation.get("matched_by"), observation.get("observed_at"), "", original.get("year"), original.get("publication_date"), original.get("date_precision"), candidate.get("date"), candidate.get("precision"), item["proposed"]["apply"], item["proposed"]["reason"], observation.get("validation_error")]
                    writer.writerow([safe_cell(value) for value in values])
                    if buffer.tell() > 32768:
                        yield buffer.getvalue()
                        buffer.seek(0); buffer.truncate(0)
                for row in db.execute(sql, (identifier,)):
                    item = json.loads(row["payload"])
                    candidate = item.get("candidate") or {}
                    values = ["observation", identifier, snapshot["id"], row["batch_id"], row["observation_id"], "", "", item["eid"], item["url"], item["status"], item["date_text"], item["matched_by"], item["observed_at"], row["imported_at"], "", "", "", candidate.get("date"), candidate.get("precision"), "", "", item.get("validation_error")]
                    writer.writerow([safe_cell(value) for value in values])
                    if buffer.tell() > 32768:
                        yield buffer.getvalue()
                        buffer.seek(0); buffer.truncate(0)
                if buffer.tell():
                    yield buffer.getvalue()
    return chunks()


def _save_streamed_dataset(papers, dataset, report):
    """Publish an indexed revision without materializing a 20,000-paper list."""
    owner, identifier, root = storage.new_id(), storage.new_id(), storage.data_root()
    counts = dict.fromkeys(("day", "month", "year", "unknown"), 0)
    count, usable, years = 0, 0, set()
    with closing(large_storage._database(root)) as db, db:
        large_paging.initialize(db)
        for ordinal, paper in enumerate(papers):
            payload = json.dumps(paper, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            authors = [a for a in paper.get("authors", []) if isinstance(a, dict)]
            search = " ".join([str(paper.get("title") or ""), str(paper.get("abstract") or ""), str(paper.get("doi") or ""), " ".join(map(str, paper.get("keywords", []))), " ".join(str(a.get("name") or "") for a in authors)]).casefold()
            db.execute("INSERT OR IGNORE INTO papers VALUES(?,?,?,?,?,?,?,?)", (digest, payload, str(paper.get("title") or "").casefold(), paper.get("year"), paper.get("citations"), paper.get("topic_id"), search, _json(sorted(author_paging.raw_ids(paper)))))
            db.execute("INSERT INTO members VALUES(?,?,?,?)", (owner, ordinal, str(paper["id"]), digest))
            large_paging.add(db, owner, paper, digest)
            precision = paper.get("date_precision", "year" if paper.get("year") else "unknown")
            counts[precision if precision in counts else "unknown"] += 1
            usable += bool(paper.get("publication_date")) and precision in {"month", "day"}
            if isinstance(paper.get("year"), int):
                years.add(paper["year"])
            count += 1
        large_paging.finish(db, owner, count)
    report.update(date_pipeline_version=2, date_precision_counts=counts, date_usable_count=usable)
    value = {"id": identifier, "name": (dataset["name"] + " · Scopus出版日補完")[:200], "created_at": storage.now(), "is_demo": bool(dataset.get("is_demo")), "paper_count": count, "papers": [], "report": report,
             "_large_store": {"owner": owner, "count": count, "canonical_author_index": False}}
    # Calculate normal dataset defaults from only the extremal years, never the corpus.
    summary_value = {**value, "papers": [{"year": y} for y in ([min(years), max(years)] if years else [])]}
    value["_dataset_summary"] = storage.dataset_summary(summary_value)
    return storage.save("datasets", value)


def apply_job(identifier, *, allow_year_change=False, overwrite_existing=False, snapshot_id=None):
    with _LOCK, closing(_db()) as db, db:
        job = dict(_job(db, identifier))
        snapshot = _snapshot(db, identifier, snapshot_id, allow_year_change, overwrite_existing)
        if snapshot["dataset_id"]:
            derived = storage.read("datasets", snapshot["dataset_id"], include_papers=False)
            return {"dataset_id": derived["id"], "dataset": storage.dataset_summary(derived), **json.loads(snapshot["summary"])}
        db.commit()  # Frozen snapshot is durable even if dataset writing fails.
        dataset = storage.read("datasets", job["dataset_id"], include_papers=False)
        audit = {"job_id": identifier, "snapshot_id": snapshot["id"], "parent_dataset_id": dataset["id"], "created_at": storage.now(), "allow_year_change": allow_year_change, "overwrite_existing": overwrite_existing,
                 "applied_count": 0, "conflict_count": 0, "changed_year_count": 0, "unchanged_count": 0, "observations_store": "scopus_publication_dates.sqlite"}
        report = deepcopy(dataset.get("report") or {})
        report.update(scopus_date_enrichment=audit, parent_dataset_id=dataset["id"])
        def patched():
            cursor = iter(db.execute(_RECORD_SQL, (snapshot["id"], identifier)))
            for ordinal, source in enumerate(large_storage.iter_papers(dataset, storage.data_root())):
                row = next(cursor, None)
                if row is None or ordinal != row["ordinal"] or str(source["id"]) != row["paper_id"]:
                    raise ValueError("元データと候補の対応が変化しています。取得ジョブを作り直してください。")
                item = _record(row, snapshot)
                paper = deepcopy(source)
                proposal = item["proposed"]
                audit["conflict_count"] += proposal["conflict"]
                if proposal["apply"]:
                    candidate, observation = proposal["candidate"], item["observation"]
                    year = int(candidate["date"][:4])
                    audit["changed_year_count"] += year != paper.get("year")
                    paper.update(year=year, publication_date=candidate["date"], date_precision=candidate["precision"], date_source="scopus:publication-date")
                    # Preserve earlier enrichment metadata, including Crossref provenance.
                    paper["scopus_date_enrichment"] = {"job_id": identifier, "snapshot_id": snapshot["id"], "observation_id": item["observation_id"], "provider": "scopus", "kind": "publication-date", "source_url": observation["url"], "date_text": observation["date_text"], "matched_by": observation["matched_by"], "observed_at": observation["observed_at"], "original": item["original"]}
                    audit["applied_count"] += 1
                else:
                    audit["unchanged_count"] += 1
                yield paper
            if next(cursor, None) is not None:
                raise ValueError("元データの論文件数が一致しません。")
        derived = _save_streamed_dataset(patched(), dataset, report)
        db.execute("UPDATE snapshots SET dataset_id=?,summary=? WHERE id=?", (derived["id"], _json(audit), snapshot["id"]))
        return {"dataset_id": derived["id"], "dataset": storage.dataset_summary(derived), **audit}
