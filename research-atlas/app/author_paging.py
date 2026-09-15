"""Exact canonical network membership for paper browsing, without name guesses."""
from __future__ import annotations

import json


def raw_ids(paper):
    identifiers = set()
    for author in paper.get("authors", []):
        if not isinstance(author, dict):
            continue
        if author.get("id"):
            identifiers.add(str(author["id"]))
        aliases = author.get("aliases")
        if isinstance(aliases, (list, tuple)):
            identifiers.update(alias for alias in aliases if isinstance(alias, str) and alias)
    return identifiers


def nodes(value):
    network = value.get("network") or {}
    complete = (network.get("export_data") or {}).get("authors")
    return complete if isinstance(complete, list) else network.get("nodes", [])


def save(db, owner, value):
    """Index already computed canonical memberships before publishing manifest.

    original_ids includes aliases rejected by identity resolution, so it cannot
    establish membership. The network's full paper_ids are authoritative.
    """
    records = nodes(value)
    if not records:
        return False
    db.execute("CREATE TABLE IF NOT EXISTS canonical_author_papers (owner TEXT NOT NULL,author_id TEXT NOT NULL,paper_id TEXT NOT NULL,PRIMARY KEY(owner,author_id,paper_id)) WITHOUT ROWID")
    for node in records:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        db.executemany("INSERT OR IGNORE INTO canonical_author_papers VALUES (?,?,?)",
            ((owner, str(node["id"]), str(identifier)) for identifier in node.get("paper_ids", [])))
    return True


def inline_members(value, canonical_author_id):
    if canonical_author_id is None:
        return None
    for node in nodes(value):
        if isinstance(node, dict) and node.get("id") == canonical_author_id:
            return {str(identifier) for identifier in node.get("paper_ids", [])}
    return None


def sql_filter(db, descriptor, *, canonical_author_id=None, author_ids=None):
    owner = descriptor["owner"]
    if canonical_author_id is not None and descriptor.get("canonical_author_index"):
        found = db.execute("SELECT 1 FROM canonical_author_papers WHERE owner=? AND author_id=? LIMIT 1",
                           (owner, canonical_author_id)).fetchone()
        if found:
            return ("SELECT paper_id FROM canonical_author_papers WHERE owner=? AND author_id=?",
                    [owner, canonical_author_id])
    if author_ids is None:
        return None
    # The array is one bound parameter even when an identity has many aliases.
    return ("SELECT m.paper_id FROM members m INDEXED BY members_owner_digest "
            "JOIN papers p INDEXED BY papers_metadata ON p.digest=m.digest "
            "WHERE m.owner=? AND EXISTS(SELECT 1 FROM json_each(p.author_ids) a "
            "JOIN json_each(?) wanted ON wanted.value=a.value)",
            [owner, json.dumps(sorted(set(author_ids)), ensure_ascii=False)])
