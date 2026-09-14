"""SQLite ストア（版管理・チェックポイント・監査ログ）。企画書 §9.7・付録C。

1 プロセス 1 接続（check_same_thread=False）＋ RLock で ThreadingHTTPServer から使う。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from ..core.document import Document
from ..util import new_id, now_iso

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _load_json(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.init()

    # ------------------------------------------------------------ 基本
    def init(self) -> None:
        with self.lock:
            self.conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows) -> None:
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, row: dict, replace: bool = False) -> None:
        cols = list(row)
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        self.execute(sql, [row[c] for c in cols])

    def update(self, table: str, key: dict, values: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        where = " AND ".join(f"{k} = ?" for k in key)
        self.execute(f"UPDATE {table} SET {sets} WHERE {where}", [*values.values(), *key.values()])

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.insert("meta", {"key": key, "value": value}, replace=True)

    # ------------------------------------------------------------ cases
    def create_case(self, *, case_id: str, name: str, purpose: str, input_text: str,
                    seeds: list[str], countries: list[str] | None = None, date_from: str = "",
                    date_to: str = "", dialect: str = "jplatpat", csv_dialect: str = "jplatpat",
                    settings: dict | None = None, actor: str = "human") -> dict:
        now = now_iso()
        self.insert("cases", {
            "case_id": case_id, "name": name, "purpose": purpose, "input_text": input_text,
            "countries": _j(countries or ["JP"]), "date_from": date_from or "", "date_to": date_to or "",
            "seeds_json": _j(seeds), "status": "new", "iteration": 1, "dialect": dialect,
            "csv_dialect": csv_dialect, "settings_json": _j(settings or {}),
            "created_at": now, "updated_at": now, "actor": actor})
        return self.get_case(case_id)

    def get_case(self, case_id: str) -> dict | None:
        row = self.one("SELECT * FROM cases WHERE case_id = ?", (case_id,))
        return self._case_row(row) if row else None

    def _case_row(self, row: dict) -> dict:
        row = dict(row)
        row["seeds"] = _load_json(row.pop("seeds_json", None), [])
        row["countries"] = _load_json(row.get("countries"), ["JP"])
        row["settings"] = _load_json(row.pop("settings_json", None), {})
        return row

    def list_cases(self) -> list[dict]:
        return [self._case_row(r) for r in self.query("SELECT * FROM cases ORDER BY created_at DESC")]

    def update_case(self, case_id: str, **values) -> None:
        if "seeds" in values:
            values["seeds_json"] = _j(values.pop("seeds"))
        if "settings" in values:
            values["settings_json"] = _j(values.pop("settings"))
        if "countries" in values and not isinstance(values["countries"], str):
            values["countries"] = _j(values["countries"])
        values["updated_at"] = now_iso()
        self.update("cases", {"case_id": case_id}, values)

    def delete_case(self, case_id: str) -> None:
        for table in ("axes", "candidates", "queries", "runs", "judgments", "pool", "samples",
                      "transforms", "iterations", "decisions", "llm_calls", "manual_prompts",
                      "masking", "logs", "db_access"):
            self.execute(f"DELETE FROM {table} WHERE case_id = ?", (case_id,))
        self.execute("DELETE FROM renderings WHERE query_id NOT IN (SELECT query_id FROM queries)")
        self.execute("DELETE FROM run_docs WHERE run_id NOT IN (SELECT run_id FROM runs)")
        self.execute("DELETE FROM cases WHERE case_id = ?", (case_id,))

    # ------------------------------------------------------------ axes
    def replace_axes(self, case_id: str, axes: list[dict], actor: str) -> None:
        self.execute("DELETE FROM axes WHERE case_id = ?", (case_id,))
        now = now_iso()
        for i, a in enumerate(axes):
            self.insert("axes", {
                "case_id": case_id, "axis_id": a["axis_id"], "name": a.get("name", ""),
                "kind": a.get("kind", "required"), "definition": a.get("definition", ""),
                "origin": a.get("origin", "llm:P1"), "evidence": a.get("evidence", ""),
                "terms_json": _j(list(a.get("terms") or [])), "category": a.get("category", "other"),
                "fixed": 1 if a.get("fixed") else 0, "sort_order": i, "created_at": now, "actor": actor})

    def get_axes(self, case_id: str) -> list[dict]:
        rows = self.query("SELECT * FROM axes WHERE case_id = ? ORDER BY sort_order", (case_id,))
        for r in rows:
            r["fixed"] = bool(r["fixed"])
            r["terms"] = _load_json(r.pop("terms_json", None), [])
        return rows

    # ------------------------------------------------------------ candidates
    def add_candidate(self, case_id: str, cand: dict, actor: str = "system") -> dict:
        row = {
            "candidate_id": cand.get("candidate_id") or new_id("cand-"), "case_id": case_id,
            "iteration": cand.get("iteration", 1), "kind": cand["kind"], "axis_id": cand.get("axis_id", ""),
            "value": cand["value"], "scheme": cand.get("scheme", ""), "level": cand.get("level", ""),
            "title": cand.get("title", ""), "variant_kind": cand.get("variant_kind", ""),
            "origin": cand.get("origin", ""), "origin_doc": cand.get("origin_doc", ""),
            "confidence": cand.get("confidence"), "r": cand.get("r"), "n": cand.get("n"),
            "r_total": cand.get("R"), "n_total": cand.get("N"), "rsj_w": cand.get("rsj_w"), "offer_w": cand.get("offer_w"),
            "w_sample": cand.get("w_sample"), "flip_rate": cand.get("flip_rate"),
            "needs_review": 1 if cand.get("needs_review") else 0,
            "dict_known": None if cand.get("dict_known") is None else int(bool(cand.get("dict_known"))),
            "status": cand.get("status", "candidate"), "reason_code": cand.get("reason_code", ""),
            "note": cand.get("note", ""), "decided_by": cand.get("decided_by", ""),
            "decided_at": cand.get("decided_at", ""), "created_at": now_iso(), "actor": actor}
        self.insert("candidates", row)
        return row

    def find_candidate(self, case_id: str, kind: str, axis_id: str, value: str, scheme: str = "") -> dict | None:
        return self.one("SELECT * FROM candidates WHERE case_id=? AND kind=? AND axis_id=? AND value=? AND scheme=?",
                        (case_id, kind, axis_id, value, scheme or ""))

    def get_candidates(self, case_id: str, kind: str | None = None, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM candidates WHERE case_id = ?"
        params: list = [case_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY kind, axis_id, COALESCE(offer_w, 0) DESC, created_at"
        rows = self.query(sql, params)
        for r in rows:
            r["needs_review"] = bool(r["needs_review"])
            r["dict_known"] = None if r["dict_known"] is None else bool(r["dict_known"])
            r["R"] = r.pop("r_total", None)
            r["N"] = r.pop("n_total", None)
        return rows

    def decide_candidate(self, candidate_id: str, status: str, reason_code: str = "", note: str | None = None,
                         actor: str = "human", axis_id: str | None = None) -> None:
        values = {"status": status, "reason_code": reason_code or "", "decided_by": actor, "decided_at": now_iso()}
        if note is not None:
            values["note"] = note
        if axis_id:
            values["axis_id"] = axis_id
        self.update("candidates", {"candidate_id": candidate_id}, values)

    def update_candidate_stats(self, candidate_id: str, stats: dict) -> None:
        allowed = {k: stats[k] for k in ("r", "n", "rsj_w", "offer_w", "w_sample", "needs_review",
                                          "flip_rate", "title", "dict_known", "level") if k in stats}
        if "R" in stats:
            allowed["r_total"] = stats["R"]
        if "N" in stats:
            allowed["n_total"] = stats["N"]
        if "needs_review" in allowed:
            allowed["needs_review"] = int(bool(allowed["needs_review"]))
        if allowed:
            self.update("candidates", {"candidate_id": candidate_id}, allowed)

    # ------------------------------------------------------------ queries / renderings
    def save_query(self, case_id: str, query, actor: str = "system") -> None:
        self.insert("queries", {
            "query_id": query.query_id, "case_id": case_id,
            "iteration": query.provenance.get("iteration", 1), "variant": query.variant,
            "dsl_json": query.to_json(indent=None),
            "parent_query_id": query.provenance.get("parent_query_id"),
            "created_at": now_iso(), "actor": actor}, replace=True)

    def get_query(self, query_id: str):
        from ..core.dsl import Query
        row = self.one("SELECT dsl_json FROM queries WHERE query_id = ?", (query_id,))
        return Query.from_json(row["dsl_json"]) if row else None

    def get_queries(self, case_id: str, iteration: int | None = None) -> dict:
        from ..core.dsl import Query
        sql = "SELECT * FROM queries WHERE case_id = ?"
        params: list = [case_id]
        if iteration is not None:
            sql += " AND iteration = ?"
            params.append(iteration)
        sql += " ORDER BY iteration, variant"
        out = {}
        for r in self.query(sql, params):
            out[r["variant"]] = Query.from_json(r["dsl_json"])
        return out

    def query_history(self, case_id: str) -> list[dict]:
        return self.query("SELECT query_id, iteration, variant, parent_query_id, created_at, actor "
                          "FROM queries WHERE case_id = ? ORDER BY iteration, variant", (case_id,))

    def save_renderings(self, query_id: str, dialect: str, parts: list[dict], roundtrip_ok: bool) -> None:
        self.execute("DELETE FROM renderings WHERE query_id = ? AND dialect = ?", (query_id, dialect))
        for p in parts:
            self.insert("renderings", {"query_id": query_id, "dialect": dialect, "part_no": p["part_no"],
                                       "text": p["text"], "chars": p["chars"],
                                       "roundtrip_ok": int(bool(roundtrip_ok))})

    def get_renderings(self, query_id: str) -> list[dict]:
        rows = self.query("SELECT * FROM renderings WHERE query_id = ? ORDER BY dialect, part_no", (query_id,))
        for r in rows:
            r["roundtrip_ok"] = bool(r["roundtrip_ok"])
        return rows

    # ------------------------------------------------------------ documents / runs
    def upsert_documents(self, docs) -> None:
        now = now_iso()
        rows, code_rows = [], []
        for d in docs:
            rows.append((d.doc_id, d.title, d.abstract, d.claims, d.pub_date, d.applicant,
                         _j(list(d.citations)), _j(d.to_dict()), now))
            for scheme, code in d.all_codes():
                code_rows.append((d.doc_id, scheme, code))
        cite_rows = [(d.doc_id, c) for d in docs for c in d.citations if c and c != d.doc_id]
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO documents (doc_id, title, abstract, claims, pub_date, applicant, "
                "citations_json, raw_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self.conn.executemany("DELETE FROM doc_codes WHERE doc_id = ?", [(d.doc_id,) for d in docs])
            self.conn.executemany("INSERT OR IGNORE INTO doc_codes (doc_id, scheme, code) VALUES (?,?,?)",
                                  code_rows)
            self.conn.executemany("INSERT OR IGNORE INTO doc_citations (doc_id, cited_id) VALUES (?,?)", cite_rows)
            self.conn.commit()

    def add_citation_edges(self, pairs) -> None:
        """(引用する文献, 引用される文献) の組を登録する（被引用情報の取り込み用）。"""
        self.executemany("INSERT OR IGNORE INTO doc_citations (doc_id, cited_id) VALUES (?,?)",
                         [(a, b) for a, b in pairs if a and b and a != b])

    def cited_ids(self, doc_id: str) -> list[str]:
        return [r["cited_id"] for r in self.query("SELECT cited_id FROM doc_citations WHERE doc_id = ? ORDER BY cited_id", (doc_id,))]

    def citing_ids(self, doc_id: str) -> list[str]:
        return [r["doc_id"] for r in self.query("SELECT doc_id FROM doc_citations WHERE cited_id = ? ORDER BY doc_id", (doc_id,))]

    def has_document(self, doc_id: str) -> bool:
        return self.one("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)) is not None

    def all_run_doc_ids(self, case_id: str) -> set[str]:
        return {r["doc_id"] for r in self.query(
            "SELECT DISTINCT rd.doc_id AS doc_id FROM run_docs rd JOIN runs r ON r.run_id = rd.run_id WHERE r.case_id = ?", (case_id,))}

    # ------------------------------------------------------------ DB API アクセス記録
    def record_db_access(self, case_id: str, kind: str, endpoint: str, hit_count: int | None) -> None:
        self.insert("db_access", {"case_id": case_id, "kind": kind, "endpoint": endpoint, "hit_count": hit_count,
                                  "created_at": now_iso()})

    def count_db_access(self, case_id: str | None = None) -> int:
        if case_id:
            return self.one("SELECT COUNT(*) AS c FROM db_access WHERE case_id = ?", (case_id,))["c"]
        return self.one("SELECT COUNT(*) AS c FROM db_access")["c"]

    def get_documents(self, doc_ids) -> dict[str, Document]:
        ids = list(doc_ids)
        out: dict[str, Document] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in self.query(f"SELECT raw_json FROM documents WHERE doc_id IN ({marks})", chunk):
                d = Document.from_dict(_load_json(r["raw_json"], {}))
                out[d.doc_id] = d
        return out

    def get_document(self, doc_id: str) -> Document | None:
        return self.get_documents([doc_id]).get(doc_id)

    def add_run(self, *, case_id: str, query_id: str, iteration: int, variant: str, dialect: str,
                source: str, hit_count: int, docs, csv_path: str = "", info: dict | None = None,
                actor: str = "human") -> str:
        run_id = new_id("run-")
        self.upsert_documents(docs)
        self.insert("runs", {"run_id": run_id, "case_id": case_id, "query_id": query_id,
                             "iteration": iteration, "variant": variant, "dialect": dialect,
                             "source": source, "hit_count": hit_count, "csv_path": csv_path,
                             "info_json": _j(info or {}), "executed_at": now_iso(), "actor": actor})
        rows = [(run_id, d.doc_id, d.rank if d.rank is not None else i + 1) for i, d in enumerate(docs)]
        self.executemany("INSERT OR REPLACE INTO run_docs (run_id, doc_id, rank) VALUES (?,?,?)", rows)
        return run_id

    def get_runs(self, case_id: str, iteration: int | None = None) -> list[dict]:
        sql = "SELECT * FROM runs WHERE case_id = ?"
        params: list = [case_id]
        if iteration is not None:
            sql += " AND iteration = ?"
            params.append(iteration)
        sql += " ORDER BY executed_at"
        rows = self.query(sql, params)
        for r in rows:
            r["info"] = _load_json(r.pop("info_json", None), {})
            r["n_docs"] = self.one("SELECT COUNT(*) AS c FROM run_docs WHERE run_id = ?", (r["run_id"],))["c"]
        return rows

    def latest_run(self, case_id: str, variant: str, iteration: int | None = None) -> dict | None:
        sql = "SELECT * FROM runs WHERE case_id = ? AND variant = ?"
        params: list = [case_id, variant]
        if iteration is not None:
            sql += " AND iteration = ?"
            params.append(iteration)
        sql += " ORDER BY executed_at DESC, rowid DESC LIMIT 1"
        row = self.one(sql, params)
        if row:
            row["info"] = _load_json(row.pop("info_json", None), {})
            row["n_docs"] = self.one("SELECT COUNT(*) AS c FROM run_docs WHERE run_id = ?", (row["run_id"],))["c"]
        return row

    def run_doc_ids(self, run_id: str) -> list[str]:
        return [r["doc_id"] for r in self.query(
            "SELECT doc_id FROM run_docs WHERE run_id = ? ORDER BY rank", (run_id,))]

    def run_documents(self, run_id: str) -> list[Document]:
        ids = self.run_doc_ids(run_id)
        docs = self.get_documents(ids)
        out = []
        for i, doc_id in enumerate(ids):
            d = docs.get(doc_id)
            if d:
                d.rank = i + 1
                out.append(d)
        return out

    # ------------------------------------------------------------ local index（オフライン DB）
    def load_local_index(self, docs, replace: bool = False) -> int:
        self.upsert_documents(docs)
        if replace:
            self.execute("DELETE FROM local_index")
        now = now_iso()
        self.executemany("INSERT OR REPLACE INTO local_index (doc_id, added_at) VALUES (?, ?)",
                         [(d.doc_id, now) for d in docs])
        return self.local_index_size()

    def local_index_size(self) -> int:
        return self.one("SELECT COUNT(*) AS c FROM local_index")["c"]

    def local_index_docs(self) -> list[Document]:
        ids = [r["doc_id"] for r in self.query("SELECT doc_id FROM local_index ORDER BY rowid")]
        docs = self.get_documents(ids)
        return [docs[i] for i in ids if i in docs]

    # ------------------------------------------------------------ judgments / pool
    def add_judgment(self, *, case_id: str, doc_id: str, iteration: int, selection: str, judge: str,
                     overall: int, per_axis: dict | None = None, flip_rate: float | None = None,
                     needs_review: bool = False, rationale: str = "") -> str:
        jid = new_id("jdg-")
        self.insert("judgments", {"judgment_id": jid, "case_id": case_id, "doc_id": doc_id,
                                  "iteration": iteration, "selection": selection, "judge": judge,
                                  "overall": overall, "per_axis_json": _j(per_axis or {}),
                                  "flip_rate": flip_rate, "needs_review": int(bool(needs_review)),
                                  "rationale": rationale, "created_at": now_iso()})
        return jid

    def get_judgments(self, case_id: str) -> list[dict]:
        rows = self.query("SELECT * FROM judgments WHERE case_id = ? ORDER BY created_at", (case_id,))
        for r in rows:
            r["per_axis"] = _load_json(r.pop("per_axis_json", None), {})
            r["needs_review"] = bool(r["needs_review"])
        return rows

    def effective_judgments(self, case_id: str) -> dict[str, dict]:
        """文献ごとの有効な判定（人 > 既知 > LLM > 分類器。同順位なら新しいもの）。"""
        rank = {"human": 4, "seed": 3, "llm": 2, "classifier": 1}
        best: dict[str, dict] = {}
        for j in self.get_judgments(case_id):
            cur = best.get(j["doc_id"])
            if cur is None or (rank.get(j["judge"], 0), j["created_at"]) >= (rank.get(cur["judge"], 0), cur["created_at"]):
                best[j["doc_id"]] = j
        return best

    def add_to_pool(self, case_id: str, doc_ids, source: str, iteration: int) -> None:
        self.executemany("INSERT OR IGNORE INTO pool (case_id, doc_id, source, added_iteration) VALUES (?,?,?,?)",
                         [(case_id, d, source, iteration) for d in doc_ids])

    def remove_from_pool(self, case_id: str, doc_id: str) -> None:
        self.execute("DELETE FROM pool WHERE case_id = ? AND doc_id = ?", (case_id, doc_id))

    def get_pool(self, case_id: str) -> list[dict]:
        return self.query("SELECT * FROM pool WHERE case_id = ? ORDER BY added_iteration, doc_id", (case_id,))

    def pool_ids(self, case_id: str) -> set[str]:
        return {r["doc_id"] for r in self.get_pool(case_id)}

    # ------------------------------------------------------------ samples / transforms / iterations
    def save_sample(self, *, case_id: str, iteration: int, population_query_id: str, target_query_id: str,
                    seed: int, doc_ids: list[str], estimate=None) -> str:
        sid = new_id("smp-")
        est = estimate.to_dict() if estimate else {}
        self.insert("samples", {"sample_id": sid, "case_id": case_id, "iteration": iteration,
                                "population_query_id": population_query_id, "target_query_id": target_query_id,
                                "seed": seed, "m": est.get("m", len(doc_ids)), "s": est.get("s"), "t": est.get("t"),
                                "recall_hat": est.get("recall_hat"), "ci_low": est.get("ci_low"),
                                "ci_high": est.get("ci_high"), "doc_ids_json": _j(doc_ids),
                                "created_at": now_iso()})
        return sid

    def get_samples(self, case_id: str) -> list[dict]:
        rows = self.query("SELECT * FROM samples WHERE case_id = ? ORDER BY created_at", (case_id,))
        for r in rows:
            r["doc_ids"] = _load_json(r.pop("doc_ids_json", None), [])
        return rows

    def sample_doc_ids(self, case_id: str) -> set[str]:
        out: set[str] = set()
        for s in self.get_samples(case_id):
            out.update(s["doc_ids"])
        return out

    def save_transforms(self, case_id: str, iteration: int, transforms: list[dict]) -> None:
        self.execute("DELETE FROM transforms WHERE case_id = ? AND iteration = ? AND status = 'candidate'",
                     (case_id, iteration))
        for t in transforms:
            self.insert("transforms", {
                "transform_id": t.get("transform_id") or new_id("tf-"), "case_id": case_id,
                "iteration": iteration, "op": t["op"], "target_json": _j(t.get("target", {})),
                "source": t.get("source", ""), "direction": t.get("direction", ""),
                "pred_hits": t.get("pred_hits"), "pred_recall_pool": t.get("pred_recall_pool"),
                "pred_p_at_k": t.get("pred_p_at_k"), "local_eval": int(bool(t.get("local_eval"))),
                "regression": int(bool(t.get("regression"))), "reason": t.get("reason", ""),
                "status": t.get("status", "candidate"), "decided_by": "", "decided_at": "",
                "created_at": now_iso()}, replace=True)

    def get_transforms(self, case_id: str, iteration: int | None = None) -> list[dict]:
        sql = "SELECT * FROM transforms WHERE case_id = ?"
        params: list = [case_id]
        if iteration is not None:
            sql += " AND iteration = ?"
            params.append(iteration)
        sql += " ORDER BY iteration, COALESCE(pred_recall_pool, 0) DESC, COALESCE(pred_hits, 0)"
        rows = self.query(sql, params)
        for r in rows:
            r["target"] = _load_json(r.pop("target_json", None), {})
            r["local_eval"] = bool(r["local_eval"])
            r["regression"] = bool(r["regression"])
        return rows

    def decide_transform(self, transform_id: str, status: str, actor: str) -> None:
        self.update("transforms", {"transform_id": transform_id},
                    {"status": status, "decided_by": actor, "decided_at": now_iso()})

    def save_iteration_metrics(self, case_id: str, iteration: int, metrics: dict) -> None:
        self.insert("iterations", {"case_id": case_id, "iteration": iteration, "metrics_json": _j(metrics),
                                   "created_at": now_iso()}, replace=True)

    def get_iterations(self, case_id: str) -> list[dict]:
        rows = self.query("SELECT * FROM iterations WHERE case_id = ? ORDER BY iteration", (case_id,))
        for r in rows:
            r["metrics"] = _load_json(r.pop("metrics_json", None), {})
        return rows

    # ------------------------------------------------------------ decisions / llm / logs
    def add_decision(self, case_id: str, iteration: int, gate: str, actor: str, action: str, payload: dict) -> str:
        did = new_id("dec-")
        self.insert("decisions", {"decision_id": did, "case_id": case_id, "iteration": iteration,
                                  "gate": gate, "actor": actor, "action": action,
                                  "payload_json": _j(payload), "created_at": now_iso()})
        return did

    def get_decisions(self, case_id: str) -> list[dict]:
        rows = self.query("SELECT * FROM decisions WHERE case_id = ? ORDER BY created_at", (case_id,))
        for r in rows:
            r["payload"] = _load_json(r.pop("payload_json", None), {})
        return rows

    def add_llm_call(self, row: dict) -> None:
        row = dict(row)
        row.setdefault("call_id", new_id("llm-"))
        row.setdefault("created_at", now_iso())
        if not isinstance(row.get("response_json"), str):
            row["response_json"] = _j(row.get("response_json"))
        self.insert("llm_calls", row)

    def count_llm_calls(self, case_id: str) -> int:
        return self.one("SELECT COUNT(*) AS c FROM llm_calls WHERE case_id = ?", (case_id,))["c"]

    def get_llm_calls(self, case_id: str, limit: int = 200) -> list[dict]:
        return self.query("SELECT call_id, prompt_id, mode, model, n_samples, masked, flip_rate, status, "
                          "duration_ms, created_at FROM llm_calls WHERE case_id = ? ORDER BY created_at DESC LIMIT ?",
                          (case_id, limit))

    def save_manual_prompt(self, call_key: str, case_id: str, prompt_id: str, sample_no: int, prompt_text: str) -> None:
        if self.one("SELECT 1 FROM manual_prompts WHERE call_key = ?", (call_key,)):
            return
        self.insert("manual_prompts", {"call_key": call_key, "case_id": case_id, "prompt_id": prompt_id,
                                       "sample_no": sample_no, "prompt_text": prompt_text, "response_text": None,
                                       "status": "pending", "created_at": now_iso(), "answered_at": None})

    def answer_manual_prompt(self, call_key: str, response_text: str) -> bool:
        if not self.one("SELECT 1 FROM manual_prompts WHERE call_key = ?", (call_key,)):
            return False
        self.update("manual_prompts", {"call_key": call_key},
                    {"response_text": response_text, "status": "answered", "answered_at": now_iso()})
        return True

    def get_manual_prompt(self, call_key: str) -> dict | None:
        return self.one("SELECT * FROM manual_prompts WHERE call_key = ?", (call_key,))

    def pending_manual_prompts(self, case_id: str) -> list[dict]:
        return self.query("SELECT * FROM manual_prompts WHERE case_id = ? AND status = 'pending' ORDER BY created_at",
                          (case_id,))

    def log(self, case_id: str, iteration: int, gate: str, actor: str, action: str, message: str = "") -> None:
        self.insert("logs", {"case_id": case_id, "iteration": iteration, "gate": gate, "actor": actor,
                             "action": action, "message": message, "created_at": now_iso()})

    def get_logs(self, case_id: str, limit: int = 500) -> list[dict]:
        return self.query("SELECT * FROM logs WHERE case_id = ? ORDER BY log_id DESC LIMIT ?", (case_id, limit))

    # ------------------------------------------------------------ masking
    def save_masking(self, case_id: str, rows: list[tuple[str, str, str]]) -> None:
        self.executemany("INSERT OR REPLACE INTO masking (case_id, token, original, kind) VALUES (?,?,?,?)",
                         [(case_id, tok, orig, kind) for tok, orig, kind in rows])

    def get_masking(self, case_id: str) -> dict[str, str]:
        return {r["token"]: r["original"] for r in self.query("SELECT * FROM masking WHERE case_id = ?", (case_id,))}

    # ------------------------------------------------------------ dictionaries
    def upsert_dictionary_codes(self, rows: list[dict]) -> None:
        self.executemany("INSERT OR REPLACE INTO dictionary_codes (scheme, code, title, parent, level, origin) "
                         "VALUES (?,?,?,?,?,?)",
                         [(r["scheme"], r["code"], r.get("title", ""), r.get("parent", ""), r.get("level", ""),
                           r.get("origin", "official")) for r in rows])

    def dictionary_code_rows(self) -> list[dict]:
        return self.query("SELECT * FROM dictionary_codes ORDER BY scheme, code")

    def bump_term_dictionary(self, term: str, synonym: str, kind: str, origin: str, adopted: bool) -> None:
        row = self.one("SELECT * FROM dictionary_terms WHERE term = ? AND synonym = ?", (term, synonym))
        if row is None:
            self.insert("dictionary_terms", {"term": term, "synonym": synonym, "kind": kind, "origin": origin,
                                             "adopted": int(adopted), "rejected": int(not adopted)})
        else:
            col = "adopted" if adopted else "rejected"
            self.execute(f"UPDATE dictionary_terms SET {col} = {col} + 1 WHERE term = ? AND synonym = ?",
                         (term, synonym))

    def term_synonyms(self, term: str) -> list[dict]:
        return self.query("SELECT * FROM dictionary_terms WHERE term = ? ORDER BY adopted DESC", (term,))

    def bump_axis_code_dictionary(self, axis_name: str, scheme: str, code: str, adopted: bool) -> None:
        row = self.one("SELECT * FROM dictionary_axis_codes WHERE axis_name=? AND scheme=? AND code=?",
                       (axis_name, scheme, code))
        if row is None:
            self.insert("dictionary_axis_codes", {"axis_name": axis_name, "scheme": scheme, "code": code,
                                                  "adopted": int(adopted), "rejected": int(not adopted)})
        else:
            col = "adopted" if adopted else "rejected"
            self.execute(f"UPDATE dictionary_axis_codes SET {col} = {col} + 1 WHERE axis_name=? AND scheme=? AND code=?",
                         (axis_name, scheme, code))

    def dictionary_summary(self) -> dict:
        return {
            "codes": self.one("SELECT COUNT(*) AS c FROM dictionary_codes")["c"],
            "codes_official": self.one("SELECT COUNT(*) AS c FROM dictionary_codes WHERE origin = 'official'")["c"],
            "terms": self.one("SELECT COUNT(*) AS c FROM dictionary_terms")["c"],
            "axis_codes": self.one("SELECT COUNT(*) AS c FROM dictionary_axis_codes")["c"],
        }
