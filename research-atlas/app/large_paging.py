"""Compact, indexed metadata for browsing immutable large-corpus revisions.

Large abstract payloads never enter the sort. An owner-first index supplies the
requested page, then only those payloads are read. Text contains-search scans a
covering text index rather than randomly fetching every multi-kilobyte record.
"""
from __future__ import annotations

import json


def initialize(db):
    statements = [
        "CREATE TABLE IF NOT EXISTS paper_views (owner TEXT NOT NULL,paper_id TEXT NOT NULL,digest TEXT NOT NULL,title TEXT NOT NULL,year INTEGER NOT NULL,citations INTEGER NOT NULL,topic TEXT,PRIMARY KEY(owner,paper_id)) WITHOUT ROWID",
        "CREATE TABLE IF NOT EXISTS paper_views_ready (owner TEXT PRIMARY KEY,count INTEGER NOT NULL)",
        "CREATE INDEX IF NOT EXISTS paper_views_year ON paper_views(owner,year DESC,paper_id,digest)",
        "CREATE INDEX IF NOT EXISTS paper_views_citations ON paper_views(owner,citations DESC,paper_id,digest)",
        "CREATE INDEX IF NOT EXISTS paper_views_title ON paper_views(owner,title,paper_id,digest)",
        "CREATE INDEX IF NOT EXISTS paper_views_digest ON paper_views(owner,digest)",
        "CREATE INDEX IF NOT EXISTS paper_views_topic ON paper_views(owner,topic)",
        "CREATE INDEX IF NOT EXISTS papers_metadata ON papers(digest,title,year,citations,topic,author_ids)",
        "CREATE INDEX IF NOT EXISTS papers_search_text ON papers(digest,search_text)",
        "CREATE INDEX IF NOT EXISTS members_owner_digest ON members(owner,digest,paper_id)",
    ]
    for statement in statements:
        db.execute(statement)


def add(db, owner, paper, digest):
    db.execute("INSERT INTO paper_views VALUES (?,?,?,?,?,?,?)", (
        owner, str(paper["id"]), digest, str(paper.get("title") or "").casefold(),
        paper.get("year") if paper.get("year") is not None else -1,
        paper.get("citations") if paper.get("citations") is not None else -1, paper.get("topic_id")))


def finish(db, owner, count):
    db.execute("INSERT OR REPLACE INTO paper_views_ready VALUES (?,?)", (owner, count))


def ensure_owner(db, owner, count):
    """One-time migration for already saved revisions; new saves index inline."""
    initialize(db)
    ready = db.execute("SELECT count FROM paper_views_ready WHERE owner=?", (owner,)).fetchone()
    if ready and ready[0] == count:
        return
    with db:
        db.execute("DELETE FROM paper_views WHERE owner=?", (owner,))
        db.execute("INSERT INTO paper_views SELECT m.owner,m.paper_id,m.digest,COALESCE(p.title,''),COALESCE(p.year,-1),COALESCE(p.citations,-1),p.topic "
                   "FROM members m INDEXED BY members_owner_digest JOIN papers p INDEXED BY papers_metadata ON p.digest=m.digest WHERE m.owner=?", (owner,))
        observed = db.execute("SELECT COUNT(*) FROM paper_views WHERE owner=?", (owner,)).fetchone()[0]
        if observed != count:
            raise ValueError("保存データの論文件数が一致しません。")
        finish(db, owner, count)


def page(db, owner, count, *, offset, limit, query, topic_id, year, sort, author_filter=None):
    """author_filter is an application-owned SQL subquery and bound parameters."""
    ensure_owner(db, owner, count)
    clauses, params = ["v.owner=?"], [owner]
    prefix = ""
    if query:
        prefix = "WITH matched AS MATERIALIZED (SELECT digest FROM papers INDEXED BY papers_search_text WHERE instr(search_text,?)>0) "
        params.insert(0, query.casefold())
        clauses.append("v.digest IN (SELECT digest FROM matched)")
    for column, value in (("v.topic", topic_id), ("v.year", year)):
        if value is not None:
            clauses.append(column + "=?")
            params.append(value)
    if author_filter is not None:
        subquery, author_params = author_filter
        clauses.append("v.paper_id IN (" + subquery + ")")
        params.extend(author_params)
    source = " FROM paper_views v WHERE " + " AND ".join(clauses)
    filtered = bool(query or topic_id is not None or year is not None or author_filter is not None)
    total = db.execute(prefix + "SELECT COUNT(*)" + source, params).fetchone()[0] if filtered else count
    order = {"year": "v.year DESC", "citations": "v.citations DESC", "title": "v.title ASC"}[sort]
    # The unfiltered order is already present in the owner-first B-tree.
    digests = [row[0] for row in db.execute(prefix + "SELECT v.digest" + source +
        " ORDER BY " + order + ",v.paper_id LIMIT ? OFFSET ?", [*params, limit, offset])]
    lookup = {}
    if digests:
        placeholders = ",".join("?" for _ in digests)
        rows = db.execute("SELECT digest,payload FROM papers WHERE digest IN (" + placeholders + ")", digests)
        lookup = {row[0]: json.loads(row[1]) for row in rows}
    return {"items": [lookup[digest] for digest in digests], "total": total, "offset": offset, "limit": limit}
