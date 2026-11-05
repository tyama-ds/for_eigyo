"""Persist observations of actual search-result imports and human review checkpoints."""

from workspace import Workspace
from services import Services

import time
from functools import wraps

from fastapi import HTTPException

from llm_judgment import is_assessed


from convergence_services import (
    query_matches_theme as query_matches_theme,
    new_observation as new_observation,
    display_search_result as display_search_result,
    observed_rows as observed_rows,
    evidence_signature as evidence_signature,
    tracking_for,
    display_convergence,
    record_result,
)


def register_convergence_routes(app, ws: Workspace, services: Services):
    state, lock = ws.state, ws.lock

    def atomic(action):
        @wraps(action)
        def wrapped(*args, **kwargs):
            with lock:
                try:
                    with ws.transaction(save=services.save):
                        action(*args, **kwargs)
                except Exception as error:
                    if not isinstance(error, (ValueError, HTTPException)):
                        raise ValueError("収束記録の保存に失敗しました。変更前の状態を保持しています。") from None
                    raise
                return services.public_state()

        return wrapped

    @app.post("/api/convergence/record")
    @atomic
    def record(body: dict):
        record_result(state, body)

    @app.post("/api/convergence/review")
    @atomic
    def review(body: dict):
        identifier, label = body.get("id"), body.get("label")
        if not isinstance(identifier, str) or label not in ("keep", "exclude"):
            raise ValueError("保留の特許を選び、必要または不要を指定してください。")
        row = next((row for row in state["patents"] if row["id"] == identifier), None)
        if not row or row.get("label") in ("keep", "exclude") or not is_assessed(row):
            raise HTTPException(
                409, "この特許はすでに判定済み、または保留ではありません。最新の状態を確認してください。"
            )
        if ("expected_reason" in body and body["expected_reason"] != row.get("label_reason")) or (
            "expected_confidence" in body and body["expected_confidence"] != row.get("agent_confidence")
        ):
            raise HTTPException(409, "保留の理由が更新されています。最新の内容を確認して判断してください。")
        row.update(label=label, label_source="human", label_reason="利用者が保留を確認して確定", score=None)
        state["training"] = None
        for patent in state["patents"]:
            patent.update(score=None, score_source=None)
        tracking = tracking_for(state)
        tracking["completed_at"] = None
        latest = (tracking.get("records") or [{}])[-1]
        observation = state.get("search_result") or tracking.get("baseline") or {}
        if latest.get("id") and latest["id"] == observation.get("id"):
            record_result(state, {})

    @app.post("/api/convergence/complete")
    @atomic
    def complete(body: dict):
        report = display_convergence(state)
        if not report["eligible_to_complete"]:
            raise ValueError("まだ完了条件を満たしていません。" + " ".join(report["blockers"]))
        state["convergence_tracking"]["completed_at"] = time.time()
