"""Durable whole-corpus abstract extraction and hierarchical report jobs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata

from . import corpus_llm, large_storage, storage
from .job_context import submit_with_context

_LOCK = threading.RLock()
_ACTIVE: dict[str, tuple[str, threading.Event]] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlas-corpus-report")
STATUSES = {"pending", "completed", "missing", "failed"}


class ReportBusy(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _root():
    return str(storage.data_root().resolve())


def _preview(value):
    """Keep all report prose, but bound source/audit metadata used by the UI."""
    if not isinstance(value, dict):
        return value
    def metadata(item):
        result = {}
        ids = item.get("paper_ids") or []
        result.update(paper_ids=ids[:20], paper_ids_count=len(ids), paper_ids_truncated=len(ids) > 20)
        warnings, seen, kinds = [], set(), Counter()
        source = item.get("warnings") or []
        for warning in source:
            code = str(warning.get("code") or "notice") if isinstance(warning, dict) else "notice"
            kinds[code] += 1
            if isinstance(warning, dict):
                compact = {key: warning[key] for key in ("code", "message", "location") if key in warning}
                for key in ("unmatched_numbers", "unmatched_quantities", "unknown_ids"):
                    if isinstance(warning.get(key), list):
                        compact[key] = warning[key][:20]
            else:
                compact = str(warning)
            identity = _json(compact)
            if identity not in seen:
                seen.add(identity)
                if len(warnings) < 20:
                    warnings.append(compact)
        result.update(warnings=warnings, warning_count=len(source), warnings_truncated=len(source) > len(warnings),
                      warning_summary=[{"code": code, "count": count} for code, count in sorted(kinds.items())])
        return result
    result = {key: value[key] for key in ("text", "headline", "status", "model", "prompt_version", "source_count", "input_count", "consumed_count",
        "missing_evidence_count", "hierarchy_levels", "input_node_count", "consumed_node_count", "paper_node_count", "metrics_node_count") if key in value}
    result.update(metadata(value))
    result["sections"] = [{**{key: section[key] for key in ("title", "text", "kind") if key in section}, **metadata(section)}
                          for section in value.get("sections", []) if isinstance(section, dict)]
    result.update(preview=True, negative_result_count=len(value.get("negative_results") or []))
    return result


def _db():
    db = sqlite3.connect(storage.data_root() / "corpus_reports.sqlite", timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS reports (
      id TEXT PRIMARY KEY,result_id TEXT NOT NULL,provider TEXT NOT NULL,model TEXT,
      namespace TEXT,prompt_version TEXT,config TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,
      initialized INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
      error TEXT,elapsed_seconds REAL NOT NULL DEFAULT 0,measured_papers INTEGER NOT NULL DEFAULT 0,
      successful_seconds REAL NOT NULL DEFAULT 0,successful_papers INTEGER NOT NULL DEFAULT 0,
      revision INTEGER NOT NULL DEFAULT 0,synthesis_status TEXT NOT NULL DEFAULT 'not_requested',narrative TEXT,narrative_preview TEXT);
    CREATE INDEX IF NOT EXISTS reports_result ON reports(result_id,created_at);
    CREATE TABLE IF NOT EXISTS papers (
      report_id TEXT NOT NULL REFERENCES reports(id),ordinal INTEGER NOT NULL,paper_id TEXT NOT NULL,
      title TEXT NOT NULL,year INTEGER,topic_id TEXT NOT NULL,abstract TEXT NOT NULL,payload TEXT NOT NULL,
      abstract_chars INTEGER NOT NULL,status TEXT NOT NULL,processed_chars INTEGER NOT NULL DEFAULT 0,
      cached INTEGER NOT NULL DEFAULT 0,extraction TEXT,error TEXT,content_hash TEXT NOT NULL,cache_key TEXT NOT NULL,
      fact_count INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(report_id,ordinal),UNIQUE(report_id,paper_id));
    CREATE INDEX IF NOT EXISTS papers_pending ON papers(report_id,status,ordinal);
    CREATE INDEX IF NOT EXISTS papers_year ON papers(report_id,year,ordinal);
    CREATE INDEX IF NOT EXISTS papers_topic ON papers(report_id,topic_id,ordinal);
    CREATE TABLE IF NOT EXISTS cache (cache_key TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS methods (report_id TEXT NOT NULL,ordinal INTEGER NOT NULL,method TEXT NOT NULL,
      PRIMARY KEY(report_id,ordinal,method));
    CREATE INDEX IF NOT EXISTS methods_summary ON methods(report_id,method);
    CREATE TABLE IF NOT EXISTS groups (
      report_id TEXT NOT NULL,kind TEXT NOT NULL,group_key TEXT NOT NULL,label TEXT NOT NULL,
      status TEXT NOT NULL,source_count INTEGER NOT NULL DEFAULT 0,narrative TEXT,error TEXT,narrative_preview TEXT,
      PRIMARY KEY(report_id,kind,group_key));
    CREATE TABLE IF NOT EXISTS summary_nodes (
      report_id TEXT NOT NULL,kind TEXT NOT NULL,group_key TEXT NOT NULL,request_hash TEXT NOT NULL,payload TEXT NOT NULL,
      PRIMARY KEY(report_id,kind,group_key,request_hash));
    """)
    if "fact_count" not in {row[1] for row in db.execute("PRAGMA table_info(papers)")}:
        db.execute("ALTER TABLE papers ADD COLUMN fact_count INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE papers SET fact_count=COALESCE(json_array_length(extraction,'$.facts'),0) WHERE extraction IS NOT NULL")
        db.commit()
    db.execute("CREATE INDEX IF NOT EXISTS papers_facts ON papers(report_id,fact_count)")
    report_columns = {row[1] for row in db.execute("PRAGMA table_info(reports)")}
    for name, declaration in (("successful_seconds", "REAL NOT NULL DEFAULT 0"), ("successful_papers", "INTEGER NOT NULL DEFAULT 0")):
        if name not in report_columns:
            db.execute(f"ALTER TABLE reports ADD COLUMN {name} {declaration}")
    for table in ("reports", "groups"):
        if "narrative_preview" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            with db:
                db.execute(f"ALTER TABLE {table} ADD COLUMN narrative_preview TEXT")
                rows = db.execute(f"SELECT rowid,narrative FROM {table} WHERE narrative IS NOT NULL").fetchall()
                db.executemany(f"UPDATE {table} SET narrative_preview=? WHERE rowid=?",
                               [(_json(_preview(json.loads(row[1]))), row[0]) for row in rows])
    return db


def _row(db, identifier, *, full=False):
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise KeyError(identifier)
    column = "narrative" if full else "narrative_preview AS narrative"
    row = db.execute("SELECT id,result_id,provider,model,namespace,prompt_version,config,status,stage,initialized,created_at,updated_at,error,"
                     "elapsed_seconds,measured_papers,successful_seconds,successful_papers,revision,synthesis_status," + column + " FROM reports WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise KeyError(identifier)
    return row


def _update(db, identifier, **fields):
    if "narrative" in fields:
        fields["narrative_preview"] = _json(_preview(json.loads(fields["narrative"]))) if fields["narrative"] else None
    fields["updated_at"] = storage.now()
    with db:
        db.execute("UPDATE reports SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", [*fields.values(), identifier])


def _recover(db):
    active = _ACTIVE.get(_root())
    with db:
        db.execute("UPDATE reports SET status='paused',stage='中断した処理を保存済みの位置から再開できます。',"
                   "synthesis_status=CASE WHEN synthesis_status='running' THEN 'partial' ELSE synthesis_status END "
                   "WHERE status IN ('preparing','running') AND id<>?", (active[0] if active else "",))


def _counts(db, identifier, column=None):
    grouping = f",{column} AS group_key" if column else ""
    query = "SELECT COUNT(*) AS total,SUM(status='completed') AS completed,SUM(status='missing') AS missing," \
            "SUM(status='failed') AS failed,SUM(status='pending') AS pending,SUM(cached) AS cached," \
            "SUM(processed_chars) AS processed_chars,SUM(abstract_chars) AS total_chars" + grouping + " FROM papers WHERE report_id=?"
    if column:
        query += f" GROUP BY {column} ORDER BY {column}"
    result = []
    for row in db.execute(query, (identifier,)):
        item = {key: (value if key == "group_key" else int(value or 0)) for key, value in dict(row).items()}
        result.append(item)
    return result if column else result[0]


def _summary(db, identifier, *, full=False):
    row = dict(_row(db, identifier, full=full))
    config = json.loads(row.pop("config"))
    counts = _counts(db, identifier)
    has_facts = bool(db.execute("SELECT 1 FROM papers WHERE report_id=? AND fact_count>0 LIMIT 1", (identifier,)).fetchone())
    labels = config.get("topic_labels", {})
    annual = [{**item, "year": item.pop("group_key", None), "count": item["total"]} for item in _counts(db, identifier, "year")]
    topics = [{**item, "topic_id": item["group_key"], "label": labels.get(item["group_key"], item["group_key"] or "未分類"), "count": item["total"]}
              for item in _counts(db, identifier, "topic_id")]
    for item in annual + topics:
        item.pop("group_key", None)
    methods_total = db.execute("SELECT COUNT(DISTINCT method) FROM methods WHERE report_id=?", (identifier,)).fetchone()[0]
    method_sql = "SELECT method,COUNT(*) AS paper_count FROM methods WHERE report_id=? GROUP BY method ORDER BY paper_count DESC,method"
    methods = [dict(item) for item in db.execute(method_sql + ("" if full else " LIMIT 200"), (identifier,))]
    narrative_column = "narrative" if full else "narrative_preview AS narrative"
    groups = [{"kind": item["kind"], "key": item["group_key"], "label": item["label"], "status": item["status"], "source_count": item["source_count"],
               "narrative": json.loads(item["narrative"]) if item["narrative"] else None, "error": item["error"]}
              for item in db.execute("SELECT kind,group_key,label,status,source_count,error," + narrative_column + " FROM groups WHERE report_id=? ORDER BY kind,group_key", (identifier,))]
    measured = row["successful_papers"]
    per_paper = row["successful_seconds"] / measured if measured else None
    partial = bool(counts["pending"] or counts["failed"] or counts["missing"] or counts["total"] == 0)
    warnings = ["対象はこの分析結果に含まれる論文の抄録です。本文を読んだ分析ではありません。",
                "研究手法は抽出された表記を正規化して集計しています。同義語の意味的な統合は保証していません。",
                "出版年・トピックの集計対象と、LLMによる抽出が成功した範囲を区別してください。"]
    if partial:
        warnings.append(f"部分結果です。未処理 {counts['pending']} 件、失敗 {counts['failed']} 件、抄録欠測 {counts['missing']} 件があります。")
    if config.get("is_demo"):
        warnings.append("合成されたデモデータです。実際の研究動向として解釈しないでください。")
    if config.get("sampled"):
        warnings.append("元の収集・分析結果に収録範囲の制限があります。世界全体の研究動向を表すものではありません。")
    if not full and methods_total > len(methods):
        warnings.append("画面とPDFの研究手法一覧は上位200表記です。全表記はCSV / JSONに保存できます。")
    warnings.extend(config.get("source_warnings", []))
    row.update(report_id=identifier, batch_size=config.get("batch_size", 100), run_all=config.get("run_all", False),
               counts=counts, has_facts=has_facts, annual=annual, topics=topics, methods=methods, methods_total=methods_total, methods_truncated=methods_total > len(methods),
               groups=groups, partial=partial, is_demo=config.get("is_demo", False), sampled=config.get("sampled", False), name=config.get("name", "全件抄録レポート"),
               warnings=warnings, abstract_based=True, estimate={"elapsed_seconds": round(row["elapsed_seconds"], 2),
               "sampled_papers": measured, "seconds_per_paper": round(per_paper, 2) if per_paper else None,
               "remaining_seconds": round(per_paper * counts["pending"], 1) if per_paper else None},
               narrative=json.loads(row["narrative"]) if row["narrative"] else None)
    row.pop("initialized")
    row.pop("namespace", None)
    return row


def get_report(identifier):
    with _LOCK, closing(_db()) as db:
        _recover(db)
        return _summary(db, identifier)


def list_reports(result_id):
    storage.read("results", result_id, include_papers=False)
    with _LOCK, closing(_db()) as db:
        _recover(db)
        result = []
        for identifier, in db.execute("SELECT id FROM reports WHERE result_id=? ORDER BY created_at DESC", (result_id,)).fetchall():
            row = dict(_row(db, identifier))
            config = json.loads(row["config"])
            counts = _counts(db, identifier)
            item = {key: row[key] for key in ("id", "result_id", "provider", "model", "status", "stage", "created_at", "updated_at", "error", "synthesis_status")}
            item.update(report_id=identifier, counts=counts, partial=bool(counts["pending"] or counts["failed"] or counts["missing"] or not counts["total"]),
                        is_demo=config.get("is_demo", False), sampled=config.get("sampled", False), name=config.get("name", "全件抄録レポート"), abstract_based=True)
            result.append(item)
        return result


def _options(batch_size, run_all):
    if type(batch_size) is not int or not 1 <= batch_size <= 20000 or type(run_all) is not bool:
        raise ValueError("1回の処理件数は1〜20,000件、全件実行はオン・オフで指定してください。")


def create_report(result_id, *, provider="local", model=None, batch_size=100, run_all=False):
    _options(batch_size, run_all)
    if provider not in {"local", "openai"}:
        raise ValueError("LLM接続先を確認してください。")
    result = storage.read("results", result_id, include_papers=False)
    with _LOCK, closing(_db()) as db:
        if _ACTIVE:
            raise ReportBusy("全件抄録レポートを処理中です。現在の処理を一時停止してから開始してください。")
        _recover(db)
        identifier, stamp = storage.new_id(), storage.now()
        meta = result.get("meta") or {}
        config = {"batch_size": batch_size, "run_all": run_all, "requested_model": model,
                  "is_demo": bool(meta.get("is_demo") or result.get("is_demo")), "sampled": bool(meta.get("sampled")),
                  "source_warnings": [str(value) for value in meta.get("warnings", [])],
                  "name": str(result.get("dataset_name") or result.get("name") or meta.get("dataset_name") or "全件抄録レポート"),
                  "topic_labels": {str(topic["id"]): str(topic.get("label") or topic["id"]) for topic in result.get("topics", [])}}
        with db:
            db.execute("INSERT INTO reports(id,result_id,provider,config,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (identifier, result_id, provider, _json(config), "paused", "対象論文の準備待ち", stamp, stamp))
        return _start(identifier, "extract", batch_size=batch_size, run_all=run_all)


def _start(identifier, operation, *, batch_size=100, run_all=False, retry_failed=False):
    _options(batch_size, run_all)
    with _LOCK, closing(_db()) as db:
        _recover(db)
        row = _row(db, identifier)
        if _ACTIVE:
            raise ReportBusy("全件抄録レポートを処理中です。現在の処理が終わるか一時停止してから続行してください。")
        if operation == "synthesize" and not row["initialized"]:
            raise ReportBusy("対象論文の準備が終わってから総合評論を生成してください。")
        event = threading.Event()
        root = _root()
        _ACTIVE[root] = (identifier, event)
        config = json.loads(row["config"])
        config.update(batch_size=batch_size, run_all=run_all)
        with db:
            if retry_failed:
                db.execute("UPDATE papers SET status='pending',error=NULL WHERE report_id=? AND status='failed'", (identifier,))
            db.execute("UPDATE reports SET config=?,status=?,stage=?,error=NULL,updated_at=? WHERE id=?",
                (_json(config), "running" if row["initialized"] else "preparing", "総合評論を準備しています。" if operation == "synthesize" else "全件抄録を準備しています。", storage.now(), identifier))
        try:
            submit_with_context(_EXECUTOR, _run, identifier, operation, event, root)
        except Exception:
            _ACTIVE.pop(root, None)
            _update(db, identifier, status="error", error="処理を開始できませんでした。再開をお試しください。")
            raise ValueError("処理を開始できませんでした。再開をお試しください。") from None
        return _summary(db, identifier)


def resume_report(identifier, *, batch_size=100, run_all=False, retry_failed=False):
    return _start(identifier, "extract", batch_size=batch_size, run_all=run_all, retry_failed=retry_failed)


def synthesize_report(identifier):
    report = get_report(identifier)
    return _start(identifier, "synthesize", batch_size=report["batch_size"], run_all=report["run_all"])


def pause_report(identifier):
    with _LOCK, closing(_db()) as db:
        _row(db, identifier)
        active = _ACTIVE.get(_root())
        if active and active[0] == identifier:
            active[1].set()
            _update(db, identifier, stage="一時停止を要求しました。現在のLLM応答を保存して停止します。")
        else:
            _recover(db)
        return _summary(db, identifier)


def _hash(paper):
    content = {key: paper.get(key) for key in ("title", "abstract", "is_demo", "sampled", "source_warnings")}
    return hashlib.sha256(_json(content).encode("utf-8")).hexdigest()


def _cache_key(content, report):
    return hashlib.sha256(_json([content, report["provider"], report["model"], report["namespace"], report["prompt_version"]]).encode()).hexdigest()


def _methods(extraction):
    values = set()
    for fact in extraction.get("facts", []):
        if isinstance(fact, dict) and fact.get("kind") in {"method", "methods"}:
            text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(fact.get("statement") or ""))).strip().casefold()
            if text:
                values.add(text)
    return values


def _prepare(db, identifier, event):
    report = dict(_row(db, identifier))
    config = json.loads(report["config"])
    chosen = report["model"] or corpus_llm.resolve_model(report["provider"], config.get("requested_model"))
    namespace = corpus_llm.cache_namespace(report["provider"], chosen)
    version = str(corpus_llm.PROMPT_VERSION)
    if report["prompt_version"] and report["prompt_version"] != version:
        raise ValueError("抽出処理の版が変わりました。新しい全件抄録レポートを作成してください。")
    if report["namespace"] and report["namespace"] != namespace:
        raise ValueError("接続先が変更されています。同じLLM接続先に戻すか、新しい全件抄録レポートを作成してください。")
    _update(db, identifier, model=chosen, namespace=namespace, prompt_version=version)
    if report["initialized"]:
        return not event.is_set()
    report.update(model=chosen, namespace=namespace, prompt_version=version)
    result = storage.read("results", report["result_id"], include_papers=False)
    with db:
        db.execute("DELETE FROM papers WHERE report_id=?", (identifier,))
        db.execute("DELETE FROM methods WHERE report_id=?", (identifier,))
        db.execute("DELETE FROM groups WHERE report_id=?", (identifier,))
        db.execute("DELETE FROM summary_nodes WHERE report_id=?", (identifier,))
    batch, method_batch = [], []
    for ordinal, source in enumerate(large_storage.iter_papers(result, storage.data_root())):
        if event.is_set():
            return False
        paper = {"id": str(source["id"]), "title": str(source.get("title") or ""), "abstract": str(source.get("abstract") or ""),
                 "year": source.get("year"), "topic_id": str(source.get("topic_id") or ""), "doi": str(source.get("doi") or ""),
                 "is_demo": config.get("is_demo", False), "sampled": config.get("sampled", False), "source_warnings": config.get("source_warnings", [])}
        content = _hash(paper)
        key = _cache_key(content, report)
        char_count = len(paper["abstract"]) if paper["abstract"].strip() else 0
        cached = db.execute("SELECT payload FROM cache WHERE cache_key=?", (key,)).fetchone() if char_count else None
        extraction = json.loads(cached[0]) if cached else None
        if extraction:
            extraction["paper_id"] = paper["id"]
            for method in _methods(extraction):
                method_batch.append((identifier, ordinal, method))
        state = "missing" if not char_count else "completed" if cached else "pending"
        batch.append((identifier, ordinal, paper["id"], paper["title"], paper["year"], paper["topic_id"], paper["abstract"], _json(paper),
                      char_count, state, int(extraction.get("covered_chars", char_count)) if extraction else 0, int(bool(cached)),
                      _json(extraction) if extraction else None, None, content, key, len(extraction.get("facts", [])) if extraction else 0))
        if len(batch) >= 250:
            _insert(db, batch, method_batch)
            batch.clear(); method_batch.clear()
    _insert(db, batch, method_batch)
    _update(db, identifier, initialized=1, status="running", stage="全論文の抄録と数値集計の準備ができました。")
    return not event.is_set()


def _insert(db, papers, methods):
    with db:
        db.executemany("INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", papers)
        db.executemany("INSERT OR IGNORE INTO methods VALUES(?,?,?)", methods)


def _save_extraction(db, identifier, row, extraction, *, final=False, elapsed=0):
    status = extraction.get("status")
    state = {"completed": "completed", "missing_abstract": "missing", "cancelled": "pending"}.get(status, "failed") if final else "pending"
    covered = min(row["abstract_chars"], max(0, int(extraction.get("covered_chars", 0))))
    error = ("抄録から照合済みの根拠を抽出できませんでした。" if status == "no_grounded_facts" else
             "一部または全部の抄録を抽出できませんでした。保存された結果を確認し、失敗分を再試行してください。") if state == "failed" else None
    with db:
        db.execute("UPDATE papers SET status=?,processed_chars=?,extraction=?,error=?,fact_count=? WHERE report_id=? AND ordinal=?",
            (state, covered, _json(extraction), error, len(extraction.get("facts", [])), identifier, row["ordinal"]))
        db.execute("DELETE FROM methods WHERE report_id=? AND ordinal=?", (identifier, row["ordinal"]))
        db.executemany("INSERT OR IGNORE INTO methods VALUES(?,?,?)", [(identifier, row["ordinal"], method) for method in _methods(extraction)])
        db.execute("UPDATE groups SET status='stale' WHERE report_id=? AND ((kind='year' AND group_key=?) OR (kind='topic' AND group_key=?))",
            (identifier, str(row["year"]), row["topic_id"]))
        db.execute("DELETE FROM summary_nodes WHERE report_id=? AND (kind='overall' OR (kind='year' AND group_key=?) OR (kind='topic' AND group_key=?))",
            (identifier, str(row["year"]), row["topic_id"]))
        successful = final and elapsed > 0 and state == "completed"
        db.execute("UPDATE reports SET revision=revision+1,narrative=NULL,narrative_preview=NULL,synthesis_status='not_requested',elapsed_seconds=elapsed_seconds+?,"
                   "measured_papers=measured_papers+?,successful_seconds=successful_seconds+?,successful_papers=successful_papers+?,updated_at=? WHERE id=?",
                   (elapsed, int(final and elapsed > 0), elapsed if successful else 0, int(successful), storage.now(), identifier))
        if final and state == "completed":
            db.execute("INSERT INTO cache VALUES(?,?,?) ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at",
                       (row["cache_key"], _json(extraction), storage.now()))


def _extract(db, identifier, event):
    report = dict(_row(db, identifier))
    config = json.loads(report["config"])
    limit = None if config["run_all"] else config["batch_size"]
    attempted, after, failures = 0, -1, 0
    while not event.is_set() and (limit is None or attempted < limit):
        row = db.execute("SELECT * FROM papers WHERE report_id=? AND status='pending' AND ordinal>? ORDER BY ordinal LIMIT 1", (identifier, after)).fetchone()
        if row is None:
            break
        row = dict(row)
        after = row["ordinal"]
        _update(db, identifier, stage=f"抄録を抽出中 · 論文 {row['ordinal'] + 1:,} · 今回 {attempted + 1:,} 件目")
        paper = json.loads(row["payload"])
        state = json.loads(row["extraction"]) if row["extraction"] else None
        # A previous run of another report may have populated the same cache.
        cached = db.execute("SELECT payload FROM cache WHERE cache_key=?", (row["cache_key"],)).fetchone()
        if cached:
            extraction = json.loads(cached[0]); extraction["paper_id"] = paper["id"]
            _save_extraction(db, identifier, row, extraction, final=True)
            with db:
                db.execute("UPDATE papers SET cached=1 WHERE report_id=? AND ordinal=?", (identifier, row["ordinal"]))
            continue
        started = time.monotonic()
        def checkpoint(value):
            _save_extraction(db, identifier, row, value)
        try:
            extraction = corpus_llm.extract_paper(paper, report["provider"], report["model"],
                checkpoint={"state": state, "save": checkpoint}, cancelled=event.is_set)
            if (not isinstance(extraction, dict) or extraction.get("status") not in {"completed", "partial", "failed", "cancelled", "missing_abstract", "no_grounded_facts"}
                    or not isinstance(extraction.get("facts"), list) or not isinstance(extraction.get("chunks"), list)
                    or type(extraction.get("covered_chars")) is not int
                    or not 0 <= extraction["covered_chars"] <= row["abstract_chars"]
                    or (extraction["status"] == "completed" and extraction["covered_chars"] != row["abstract_chars"])):
                raise ValueError("invalid extraction")
            if extraction["status"] == "completed" and not extraction["facts"]:
                extraction["status"] = "no_grounded_facts"
        except Exception:
            saved = db.execute("SELECT extraction FROM papers WHERE report_id=? AND ordinal=?", (identifier, row["ordinal"])).fetchone()[0]
            extraction = json.loads(saved) if saved else {"paper_id": paper["id"], "facts": [], "chunks": [], "covered_chars": 0}
            extraction.update(status="failed", warnings=["LLMの応答または結果の保存を完了できませんでした。既存の抽出済み範囲は保持しています。"])
        _save_extraction(db, identifier, row, extraction, final=True, elapsed=time.monotonic() - started)
        attempted += 1
        failures = failures + 1 if extraction["status"] in {"failed", "partial", "no_grounded_facts"} else 0
        if failures >= 3:
            _update(db, identifier, status="paused", stage="3件続けて抽出できなかったため一時停止しました。LLM接続設定と失敗内容を確認し、失敗分を再試行してください。")
            return
    counts = _counts(db, identifier)
    _update(db, identifier, status="paused" if counts["pending"] else "completed",
            stage="今回の抽出を保存しました。残りは再開できます。" if counts["pending"] else "対象抄録の処理が終わりました。欠測・失敗件数を確認し、総合評論を生成できます。")


def _public_record(row):
    metadata = json.loads(row["payload"])
    return {"id": row["paper_id"], "paper_id": row["paper_id"], "title": row["title"], "year": row["year"], "topic_id": row["topic_id"],
            "is_demo": metadata.get("is_demo", False), "sampled": metadata.get("sampled", False),
            "abstract": row["abstract"], "abstract_chars": row["abstract_chars"], "processed_chars": row["processed_chars"],
            "status": row["status"], "cached": bool(row["cached"]), "error": row["error"],
            "extraction": json.loads(row["extraction"]) if row["extraction"] else None}


def _group_records(db, identifier, kind, key):
    column = "year" if kind == "year" else "topic_id"
    value = None if kind == "year" and key == "None" else int(key) if kind == "year" else key
    for row in db.execute(f"SELECT * FROM papers WHERE report_id=? AND {column} IS ? AND extraction IS NOT NULL ORDER BY ordinal", (identifier, value)):
        item = _public_record(row)
        if item["status"] == "completed" or (item["extraction"] or {}).get("facts"):
            yield item


def _summary_checkpoint(db, identifier, kind, key):
    nodes = {row["request_hash"]: json.loads(row["payload"]) for row in db.execute(
        "SELECT request_hash,payload FROM summary_nodes WHERE report_id=? AND kind=? AND group_key=?", (identifier, kind, key))}
    def save(value):
        if not isinstance(value, dict) or value.get("kind") != "summary_node" or not isinstance(value.get("request_hash"), str):
            raise ValueError("invalid summary checkpoint")
        with db:
            db.execute("INSERT INTO summary_nodes VALUES(?,?,?,?,?) ON CONFLICT(report_id,kind,group_key,request_hash) DO UPDATE SET payload=excluded.payload",
                (identifier, kind, key, value["request_hash"], _json(value)))
    return {"state": {"nodes": nodes}, "save": save}


def _synthesize(db, identifier, event):
    report = dict(_row(db, identifier))
    config = json.loads(report["config"])
    counts = _counts(db, identifier)
    has_facts = bool(db.execute("SELECT 1 FROM papers WHERE report_id=? AND fact_count>0 LIMIT 1", (identifier,)).fetchone())
    if not has_facts:
        _update(db, identifier, status="paused" if counts["pending"] else "completed", synthesis_status="not_available",
            stage="総合評論の根拠となる抽出結果がありません。未処理・失敗・抄録欠測を確認してください。", narrative=None)
        return
    _update(db, identifier, synthesis_status="running", status="running", stage="全抽出結果から年別・分野別の評論を作成しています。")
    plan = []
    metric_rows = {"year": _counts(db, identifier, "year"), "topic": _counts(db, identifier, "topic_id")}
    for kind, column in (("year", "year"), ("topic", "topic_id")):
        for row in metric_rows[kind]:
            key = str(row["group_key"])
            label = f"{key} 年" if kind == "year" else config.get("topic_labels", {}).get(key, key or "未分類")
            metrics = {**row, "scope": "selected_analysis_result", "kind": kind, column: row["group_key"], "is_demo": config.get("is_demo", False), "sampled": config.get("sampled", False)}
            metrics.pop("group_key")
            plan.append((kind, key, label, metrics))
    for kind, key, label, metrics in plan:
        if event.is_set():
            _update(db, identifier, status="paused", synthesis_status="partial", stage="総合評論を一時停止しました。保存した年別・分野別の評論は再利用できます。")
            return
        existing = db.execute("SELECT status FROM groups WHERE report_id=? AND kind=? AND group_key=?", (identifier, kind, key)).fetchone()
        if existing and existing[0] == "completed":
            continue
        records = list(_group_records(db, identifier, kind, key))
        if not records:
            continue
        _update(db, identifier, stage=f"{label} の全抽出結果を統合しています。")
        with db:
            db.execute("INSERT INTO groups(report_id,kind,group_key,label,status,source_count,narrative,error) VALUES(?,?,?,?,?,?,NULL,NULL) ON CONFLICT(report_id,kind,group_key) DO UPDATE SET status='running',error=NULL,source_count=excluded.source_count",
                (identifier, kind, key, label, "running", len(records)))
        try:
            narrative = corpus_llm.synthesize(records, report["provider"], report["model"], label=label,
                checkpoint=_summary_checkpoint(db, identifier, kind, key), cancelled=event.is_set, metrics=metrics)
            if not isinstance(narrative, dict) or narrative.get("status") != "completed":
                raise ValueError("invalid synthesis")
            state, error = "completed", None
        except Exception as exc:
            if getattr(exc, "kind", None) == "cancelled" or event.is_set():
                with db:
                    db.execute("UPDATE groups SET status='partial' WHERE report_id=? AND kind=? AND group_key=?", (identifier, kind, key))
                _update(db, identifier, status="paused", synthesis_status="partial", stage="評論の途中経過を保存して一時停止しました。再開時は保存済みの部分を再利用します。")
                return
            narrative, state = None, "failed"
            error = "この区分の評論を生成できませんでした。抽出済みの結果は保存されています。"
        with db:
            db.execute("INSERT INTO groups VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(report_id,kind,group_key) DO UPDATE SET "
                "label=excluded.label,status=excluded.status,source_count=excluded.source_count,narrative=COALESCE(excluded.narrative,groups.narrative),"
                "error=excluded.error,narrative_preview=COALESCE(excluded.narrative_preview,groups.narrative_preview)",
                (identifier, kind, key, label, state, len(records), _json(narrative) if narrative else None, error, _json(_preview(narrative)) if narrative else None))
    if event.is_set():
        _update(db, identifier, status="paused", synthesis_status="partial", stage="区分別の評論を保存して一時停止しました。")
        return
    # The annual partition covers each extracted paper once. If a year failed,
    # do not pretend an overall synthesis represents all extracted papers.
    annual = db.execute("SELECT * FROM groups WHERE report_id=? AND kind='year' ORDER BY group_key", (identifier,)).fetchall()
    failed = any(row["status"] != "completed" for row in annual)
    if failed or not annual:
        _update(db, identifier, status="completed", synthesis_status="partial", stage="区分別の一部評論を生成できませんでした。再度、総合評論を実行すると未完了区分を再試行します。")
        return
    _update(db, identifier, stage="全期間の評論を統合しています。")
    try:
        items = [{**json.loads(row["narrative"]), "label": row["label"], "year": row["group_key"],
                  "is_demo": config.get("is_demo", False), "sampled": config.get("sampled", False)} for row in annual]
        narrative = corpus_llm.synthesize(items, report["provider"], report["model"], label="全期間・全分野の総合評論",
            checkpoint=_summary_checkpoint(db, identifier, "overall", "all"), cancelled=event.is_set,
            metrics={"scope": "selected_analysis_result", "counts": counts,
                     "annual": [metrics for kind, _, _, metrics in plan if kind == "year"],
                     "topics": [metrics for kind, _, _, metrics in plan if kind == "topic"]})
        if not isinstance(narrative, dict) or narrative.get("status") != "completed":
            raise ValueError("invalid synthesis")
        partial = bool(counts["pending"] or counts["failed"] or counts["missing"])
        any_group_failed = db.execute("SELECT 1 FROM groups WHERE report_id=? AND status<>'completed' LIMIT 1", (identifier,)).fetchone()
        _update(db, identifier, status="completed", synthesis_status="partial" if partial or any_group_failed else "completed",
                stage="抄録に基づく総合評論を保存しました。対象範囲と欠測・失敗件数を確認してください。", narrative=_json(narrative))
    except Exception as exc:
        if getattr(exc, "kind", None) == "cancelled" or event.is_set():
            _update(db, identifier, status="paused", synthesis_status="partial", stage="全体評論の途中経過を保存して一時停止しました。")
        else:
            _update(db, identifier, status="completed", synthesis_status="failed", stage="全体の評論を生成できませんでした。保存した年別・分野別の評論は利用できます。")


def _run(identifier, operation, event, root):
    try:
        with closing(_db()) as db:
            if not _prepare(db, identifier, event):
                _update(db, identifier, status="paused", stage="対象論文の準備を一時停止しました。再開できます。")
                return
            if operation == "extract":
                _extract(db, identifier, event)
            else:
                _synthesize(db, identifier, event)
    except Exception as exc:
        allowed = {"接続先が変更されています。同じLLM接続先に戻すか、新しい全件抄録レポートを作成してください。",
                   "抽出処理の版が変わりました。新しい全件抄録レポートを作成してください。"}
        message = str(exc) if isinstance(exc, ValueError) and str(exc) in allowed else "処理を続行できませんでした。LLM接続設定・対象データ・保存先を確認してください。保存済み結果は残っています。"
        with closing(_db()) as db:
            current = _row(db, identifier)
            _update(db, identifier, status="error", error=message, stage=message,
                    synthesis_status="failed" if current["synthesis_status"] == "running" else current["synthesis_status"])
    finally:
        with _LOCK:
            if _ACTIVE.get(root, (None,))[0] == identifier:
                _ACTIVE.pop(root, None)


def paper_page(identifier, *, offset=0, limit=50, status=None):
    if status is not None and status not in STATUSES:
        raise ValueError("論文の処理状態を確認してください。")
    with closing(_db()) as db:
        _row(db, identifier)
        where, args = "report_id=?", [identifier]
        if status:
            where += " AND status=?"; args.append(status)
        total = db.execute("SELECT COUNT(*) FROM papers WHERE " + where, args).fetchone()[0]
        items = [_public_record(row) for row in db.execute("SELECT * FROM papers WHERE " + where + " ORDER BY ordinal LIMIT ? OFFSET ?", [*args, limit, offset])]
    return {"items": items, "total": total, "offset": offset, "limit": limit}


def iter_records(identifier):
    with closing(_db()) as db:
        db.execute("BEGIN")
        _row(db, identifier)
        for row in db.execute("SELECT * FROM papers WHERE report_id=? ORDER BY ordinal", (identifier,)):
            yield _public_record(row)


def iter_export(identifier, format):
    from . import corpus_exports
    with closing(_db()) as db:
        db.execute("BEGIN")
        report = _summary(db, identifier, full=True)
        records = (_public_record(row) for row in db.execute("SELECT * FROM papers WHERE report_id=? ORDER BY ordinal", (identifier,)))
        function = corpus_exports.iter_csv if format == "csv" else corpus_exports.iter_json
        yield from function(report, records)
