"""Shared workspace operations, independent of HTTP route registration."""

import copy
import uuid
from common import normalize_publication_id


def new_discovery():
    return dict(
        id=uuid.uuid4().hex,
        plan=None,
        analysis=None,
        patents=[],
        rounds=[],
        logs=[],
        status="idle",
        mode="csv",
        last_error=None,
        archives=[],
    )


def public_discovery(value):
    if not isinstance(value, dict):
        return None
    return {**{k: v for k, v in value.items() if k != "patents"}, "document_count": len(value.get("patents", []))}


def _document_key(row):
    return normalize_publication_id(row["id"])


def merge_documents(existing, incoming, source, live_labels=False):
    """Accumulate publications, and refresh human decisions only from their owner."""
    rows = {_document_key(row): copy.deepcopy(row) for row in existing}
    before = set(rows)
    for incoming_row in incoming:
        row = copy.deepcopy(incoming_row)
        key = _document_key(row)
        old = rows.get(key, {})
        merged = {**old, **{k: v for k, v in row.items() if v not in ("", None)}}
        merged["id"] = old.get("id") or row["id"]
        if old.get("label_source") == "human" and not live_labels:
            for field in ("label", "label_source", "label_reason"):
                merged[field] = old.get(field)
        if live_labels:
            for field in ("label", "label_source", "label_reason"):
                merged[field] = row.get(field)
        merged.setdefault("source", source)
        merged["synthetic"] = bool(old.get("synthetic") or row.get("synthetic") or str(row["id"]).startswith("DEMO-"))
        origins = list(old.get("discovery_sources", []))
        if source not in origins:
            origins.append(source)
        merged["discovery_sources"] = origins[-20:]
        rows[key] = merged
    if len(rows) > 5000:
        raise ValueError("探索ラボの蓄積上限は5,000公報です。結果を書き出し、新しい計画を作成してください。")
    return list(rows.values()), len(set(rows) - before)
