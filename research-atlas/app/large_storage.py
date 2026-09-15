"""Indexed paper storage shared by immutable large-corpus revisions.

Paper payloads are content-addressed so appending CSV batches does not duplicate
unchanged abstracts in every revision. The JSON manifest is published last.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .limits import LARGE_CORPUS_THRESHOLD
from . import author_paging, large_paging


def _database(root):
    folder = Path(root) / "large"
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "papers.sqlite3", timeout=120)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
            digest TEXT PRIMARY KEY, payload TEXT NOT NULL,
            title TEXT, year INTEGER, citations INTEGER, topic TEXT,
            search_text TEXT, author_ids TEXT
        );
        CREATE TABLE IF NOT EXISTS members (
            owner TEXT NOT NULL, ordinal INTEGER NOT NULL,
            paper_id TEXT NOT NULL, digest TEXT NOT NULL REFERENCES papers(digest),
            PRIMARY KEY(owner, ordinal), UNIQUE(owner, paper_id)
        );
        CREATE INDEX IF NOT EXISTS members_digest ON members(digest);
    """)
    return db


def display_network(value):
    """Keep measured totals; bound only evidence lists sent to the browser."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "export_data":
                continue
            if (key.endswith("paper_ids") or key.endswith("evidence_ids")) and isinstance(item, list) and len(item) > 50:
                result[key] = item[:50]
                result[key + "_total"] = len(item)
                result[key + "_truncated"] = True
            else:
                result[key] = display_network(item)
        return result
    if isinstance(value, list):
        return [display_network(item) for item in value]
    return value


def externalize(value, root):
    papers = value.get("papers")
    is_large = isinstance(papers, list) and len(papers) > LARGE_CORPUS_THRESHOLD
    network_only = len(value.get("export_data", {}).get("authors", [])) > LARGE_CORPUS_THRESHOLD
    if not is_large and not network_only:
        return value
    owner = uuid.uuid4().hex
    manifest = {**value}
    descriptor = {"owner": owner, "count": len(papers) if is_large else 0}
    if is_large:
        with closing(_database(root)) as db, db:
            large_paging.initialize(db)
            for index, paper in enumerate(papers):
                payload = json.dumps(paper, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                authors = [a for a in paper.get("authors", []) if isinstance(a, dict)]
                search = " ".join([str(paper.get("title") or ""), str(paper.get("abstract") or ""),
                    str(paper.get("doi") or ""), " ".join(map(str, paper.get("keywords", []))),
                    " ".join(str(a.get("name") or "") for a in authors)]).casefold()
                ids = sorted(author_paging.raw_ids(paper))
                db.execute("INSERT OR IGNORE INTO papers VALUES (?,?,?,?,?,?,?,?)", (
                    digest, payload, str(paper.get("title") or "").casefold(), paper.get("year"),
                    paper.get("citations"), paper.get("topic_id"), search, json.dumps(ids)))
                db.execute("INSERT INTO members VALUES (?,?,?,?)", (owner, index, str(paper["id"]), digest))
                large_paging.add(db, owner, paper, digest)
            large_paging.finish(db, owner, len(papers))
            descriptor["canonical_author_index"] = author_paging.save(db, owner, value)
        manifest["papers"] = []
    for key in ("network", "export_data"):
        if key not in value:
            continue
        name = f"{owner}.{key}.json"
        target = Path(root) / "large" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as output:
            json.dump(value[key], output, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        descriptor[key] = name
        manifest[key] = display_network(value[key]) if key == "network" else {}
    manifest["_large_store"] = descriptor
    return manifest


def restore(value, root, include_papers=True):
    descriptor = value.get("_large_store")
    if not descriptor or not include_papers:
        return value
    if descriptor.get("count"):
        value["papers"] = list(iter_papers(value, root))
    for key in ("network", "export_data"):
        if key in descriptor:
            value[key] = json.loads(_aux_path(descriptor, root, key).read_text(encoding="utf-8"))
    value.pop("_large_store", None)
    value.pop("_dataset_summary", None)
    return value


def iter_papers(value, root):
    descriptor = value.get("_large_store")
    if not descriptor or not descriptor.get("count"):
        yield from value.get("papers", [])
        return
    with closing(_database(root)) as db:
        cursor = db.execute("SELECT p.payload FROM members m JOIN papers p ON p.digest=m.digest WHERE m.owner=? ORDER BY m.ordinal",
                            (descriptor["owner"],))
        count = 0
        for row in cursor:
            count += 1
            yield json.loads(row[0])
        if count != descriptor["count"]:
            raise ValueError("保存データの論文件数が一致しません。データフォルダー全体のバックアップを確認してください。")


def _aux_path(descriptor, root, key):
    name = descriptor[key]
    if name != f"{descriptor['owner']}.{key}.json" or Path(name).name != name:
        raise ValueError("保存データの参照が不正です。")
    return Path(root) / "large" / name


def export_json(value, root):
    """Yield complete public JSON in UTF-8 chunks of at most 64 KiB.

    Indexed papers are read one payload at a time; full network/export auxiliaries
    stream directly from their JSON files. Memory does not grow with corpus size
    (apart from the already loaded manifest and the largest individual paper).
    """
    descriptor = value.get("_large_store") or {}
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    chunk_size = 64 * 1024

    def parts():
        yield b"{"
        first = True
        for key, item in value.items():
            if key in {"_large_store", "_dataset_summary"}:
                continue
            if not first:
                yield b","
            first = False
            yield encoder.encode(key).encode("utf-8")
            yield b":"
            if key == "papers" and descriptor.get("count"):
                yield b"["
                with closing(_database(root)) as db:
                    cursor = db.execute("SELECT p.payload FROM members m JOIN papers p ON p.digest=m.digest WHERE m.owner=? ORDER BY m.ordinal",
                                        (descriptor["owner"],))
                    count = 0
                    for row in cursor:
                        if count:
                            yield b","
                        count += 1
                        # Do not decode/serialize a paper that is already JSON.
                        yield row[0].encode("utf-8")
                    if count != descriptor["count"]:
                        raise ValueError("保存データの論文件数が一致しません。データフォルダー全体のバックアップを確認してください。")
                yield b"]"
            elif key in {"network", "export_data"} and key in descriptor:
                with _aux_path(descriptor, root, key).open("rb") as source:
                    while data := source.read(chunk_size):
                        yield data
            else:
                for text in encoder.iterencode(item):
                    yield text.encode("utf-8")
        yield b"}"

    pending = bytearray()
    for data in parts():
        # Coalesce tiny encoder tokens but split long Unicode payloads by byte
        # length; consumers concatenate bytes before decoding the JSON stream.
        offset = 0
        while offset < len(data):
            length = min(chunk_size - len(pending), len(data) - offset)
            pending.extend(data[offset:offset + length])
            offset += length
            if len(pending) == chunk_size:
                yield bytes(pending)
                pending.clear()
    if pending:
        yield bytes(pending)


def paper_page(value, root, *, offset=0, limit=200, query="", topic_id=None, author_id=None, author_ids=None,
               canonical_author_id=None, year=None, sort="year"):
    descriptor = value.get("_large_store")
    wanted_authors = set(map(str, author_ids)) if author_ids is not None else ({str(author_id)} if author_id is not None else None)
    if not descriptor or not descriptor.get("count"):
        papers = value.get("papers", [])
        canonical_members = author_paging.inline_members(value, canonical_author_id)
        def matches(p):
            if topic_id is not None and p.get("topic_id") != topic_id:
                return False
            if year is not None and p.get("year") != year:
                return False
            if canonical_members is not None:
                if str(p.get("id")) not in canonical_members:
                    return False
            elif wanted_authors is not None and not wanted_authors.intersection(author_paging.raw_ids(p)):
                return False
            text = " ".join([str(p.get("title") or ""), str(p.get("abstract") or ""), str(p.get("doi") or ""),
                              " ".join(map(str, p.get("keywords", []))),
                              " ".join(str(a.get("name") or "") for a in p.get("authors", []) if isinstance(a, dict))])
            return not query or query.casefold() in text.casefold()
        papers = [p for p in papers if matches(p)]
        key = ((lambda p: (str(p.get("title") or "").casefold(), str(p["id"]))) if sort == "title" else
               (lambda p: (-(p.get(sort) if p.get(sort) is not None else -1), str(p["id"]))))
        papers.sort(key=key)
        return {"items": papers[offset:offset + limit], "total": len(papers), "offset": offset, "limit": limit}
    with closing(_database(root)) as db:
        author_filter = author_paging.sql_filter(db, descriptor,
            canonical_author_id=canonical_author_id, author_ids=wanted_authors)
        return large_paging.page(db, descriptor["owner"], descriptor["count"], offset=offset, limit=limit,
            query=query, topic_id=topic_id, year=year, sort=sort, author_filter=author_filter)


def paper_by_id(value, root, identifier):
    descriptor = value.get("_large_store")
    if descriptor and descriptor.get("count"):
        with closing(_database(root)) as db:
            row = db.execute("SELECT p.payload FROM members m JOIN papers p ON p.digest=m.digest WHERE m.owner=? AND m.paper_id=?",
                             (descriptor["owner"], identifier)).fetchone()
        if row:
            return json.loads(row[0])
    else:
        for paper in value.get("papers", []):
            if str(paper.get("id")) == identifier:
                return paper
    raise KeyError("論文が見つかりません。")


def papers_by_ids(value, root, identifiers):
    wanted = list(dict.fromkeys(map(str, identifiers)))
    descriptor = value.get("_large_store")
    if not descriptor or not descriptor.get("count"):
        lookup = {str(p.get("id")): p for p in value.get("papers", [])}
    else:
        lookup = {}
        with closing(_database(root)) as db:
            for start in range(0, len(wanted), 500):
                batch = wanted[start:start + 500]
                if not batch:
                    continue
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute("SELECT m.paper_id,p.payload FROM members m JOIN papers p ON p.digest=m.digest WHERE m.owner=? AND m.paper_id IN (" + placeholders + ")",
                                  [descriptor["owner"], *batch])
                lookup.update({row[0]: json.loads(row[1]) for row in rows})
    return [lookup[identifier] for identifier in wanted if identifier in lookup]
