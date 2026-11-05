"""Shared workspace operations, independent of HTTP route registration."""

import copy
import time
import uuid
from fastapi import HTTPException
from catalog import keywords
from convergence_engine import build_record, summarize
from workspace_import import publication_key
from common import fingerprint


def query_matches_theme(query, theme):
    if not query:
        return False
    if query.get("boolean_tree"):
        return query.get("keyword_context") == theme
    return query.get("keywords") == keywords(theme)


def new_observation(rows, state, *, scope="csv"):
    keys = list(dict.fromkeys(publication_key(row) for row in rows))
    latest = next(
        (
            query
            for query in reversed(state.get("queries", []))
            if query_matches_theme(query, state.get("keywords", ""))
        ),
        None,
    )
    return dict(
        id=uuid.uuid4().hex,
        keywords=state.get("keywords", ""),
        imported_at=time.time(),
        publication_keys=keys,
        uploaded_count=len(keys),
        query_id=(latest or {}).get("id"),
        scope=scope,
    )


def display_search_result(state):
    observation = state.get("search_result")
    if not isinstance(observation, dict):
        return None
    return {key: value for key, value in observation.items() if key != "publication_keys"}


def observed_rows(state, observation):
    by_key = {publication_key(row): row for row in state.get("patents", [])}
    keys = observation.get("publication_keys", [])
    if any(key not in by_key for key in keys):
        raise ValueError("今回のCSVに含まれた特許が現在の地図にありません。検索結果を再度取り込んでください。")
    return [by_key[key] for key in keys]


def evidence_signature(rows):
    fields = ("title", "abstract", "ipc", "label", "label_source", "label_reason", "agent_confidence")
    values = sorted((publication_key(row), {field: row.get(field) for field in fields}) for row in rows)
    return fingerprint(values)


def tracking_for(state):
    value = state.get("convergence_tracking") or {}
    return value if value.get("keywords") == state.get("keywords", "") else {}


def display_convergence(state):
    tracking = tracking_for(state)
    records = tracking.get("records", [])
    result = summarize(records, state.get("patents", []), keywords=state.get("keywords", ""))
    blockers = result["blockers"]
    observation = state.get("search_result") or tracking.get("baseline")
    if records:
        latest = records[-1]
        if not observation or observation.get("id") != latest["id"]:
            blockers.append("新しい検索結果がまだグラフに記録されていません。")
        else:
            try:
                current = evidence_signature(observed_rows(state, observation))
            except ValueError:
                current = None
            if current != latest.get("evidence_signature"):
                blockers.append("記録後に特許・判定が変わりました。「この回の判定を更新」で反映してください。")
        recent = records[-4:]
        if any(not row.get("query_id") for row in recent):
            blockers.append("比較対象の各回で、実際に使った検索式を指定してください。")
    if blockers:
        result["eligible_to_complete"] = False
    if result["eligible_to_complete"] and tracking.get("completed_at"):
        result.update(status="completed", completed_at=tracking["completed_at"])
    elif result.get("status") == "stable" and not result["eligible_to_complete"]:
        result["status"] = "exploring"
    result["note"] += (
        " 同じ検索サイト・対象期間・集計単位で比較してください。検索全体の網羅性を保証する指標ではありません。"
    )
    # Raw fingerprints and membership are retained in the project export only.
    for record in result["records"]:
        record.pop("evidence_signature", None)
    return result


def record_result(state, body, *, automatic=False):
    theme = state.get("keywords", "")
    if not theme.strip():
        raise ValueError("先に探索キーワードを設定してください。")
    existing = state.get("convergence_tracking") or {}
    if existing.get("keywords") != theme:
        archives = list(existing.get("archives", []))
        if existing.get("records"):
            archives.append({key: copy.deepcopy(value) for key, value in existing.items() if key != "archives"})
        existing = dict(keywords=theme, records=[], archives=archives, completed_at=None)
    observation = state.get("search_result")
    if observation and observation.get("keywords") != theme:
        if automatic:
            return
        raise ValueError("CSVを取り込んだときのテーマと異なります。このテーマの検索結果を取り込んでください。")
    if "observation_id" in body and body["observation_id"] != (observation or {}).get("id"):
        raise HTTPException(409, "別のCSVが取り込まれました。最新の検索結果を確認して記録してください。")
    if not observation:
        if automatic:
            return
        if not state.get("patents"):
            raise ValueError("記録する特許がありません。先にCSVを取り込んでください。")
        observation = existing.get("baseline") or new_observation(state["patents"], state, scope="workspace_baseline")
        existing["baseline"] = observation
    rows = observed_rows(state, observation)
    records = existing["records"]
    old = next((row for row in records if row["id"] == observation["id"]), None)
    total = body.get("total_hits", (old or {}).get("total_hits"))
    if total is not None and (type(total) is not int or not len(rows) <= total <= 10**12):
        raise ValueError("検索総件数は今回のCSV件数以上の整数で入力してください。不明な場合は空欄にしてください。")
    query_id = body.get("query_id", (old or {}).get("query_id", observation.get("query_id")))
    if query_id is not None and not isinstance(query_id, str):
        raise ValueError("実際に使った検索式を選択してください。")
    query = next((row for row in state.get("queries", []) if row["id"] == query_id), None)
    if query_id is not None and not query_matches_theme(query, theme):
        raise ValueError("現在のテーマで保存された検索式を選択してください。")
    previous = [row for row in records if row["id"] != observation["id"]]
    if old and old is not records[-1]:
        raise ValueError("過去の記録は固定されています。最新の検索結果を更新してください。")
    snapshot = build_record(rows, observation, previous, total_hits=total, query=query)
    snapshot.update(
        observation_id=observation["id"],
        ordinal=len(previous) + 1,
        created_at=(old or {}).get("created_at", time.time()),
        updated_at=time.time(),
        evidence_signature=evidence_signature(rows),
        query_id=query_id,
        expression=(query or {}).get("expression", ""),
    )
    existing.update(records=previous + [snapshot], completed_at=None)
    state["convergence_tracking"] = existing
    return snapshot
