"""One-cycle judgment agent: select classifications, draft, judge, optionally train.

The route validates the request and launches ``run_agent`` as the background
job. Shared operations come from ``Services`` so the application keeps its
replaceable seams; ``refinement_terms`` is injected for the same reason.
"""

import time

import llm_judgment
from analysis_engine import refinement_terms as default_refinement_terms
from common import checked_text
from prompt_templates import SELECTION_SYSTEM
from query_service import classification_key, previous_query_for_context


def checked_agent_request(ws, body):
    """Validate the request under the lock; returns the criteria text."""
    criteria = checked_text(body.get("criteria", ""), 5000)
    if not criteria or not ws.state["keywords"]:
        raise ValueError("先に探索キーワードと判断基準を入力してください。")
    if ws.settings["provider"] == "offline":
        raise ValueError("判断エージェントにはLLMが必要です。設定タブから接続してください。")
    max_items = body.get("max_items", 100)
    threshold = body.get("threshold", 0.8)
    if body.get("mode", "lightweight") not in ("lightweight", "transformer") or not isinstance(
        body.get("train", False), bool
    ):
        raise ValueError("学習方式・学習実行の指定を確認してください。")
    with ws.lock:
        ws.idle()
        llm_judgment.prepare(
            dict(criteria=criteria, max_items=max_items, threshold=threshold, fresh_only=True),
            ws.state["patents"],
            ws.state["keywords"],
            ws.settings["provider"],
            allow_empty=True,
        )
    return criteria


def run_agent(ws, services, body, criteria, *, refinement_terms=default_refinement_terms):
    """The background job body; the caller owns ``ws.job`` and ``ws.stop``."""
    state, settings, stop = ws.state, ws.settings, ws.stop
    max_items = body.get("max_items", 100)
    threshold = body.get("threshold", 0.8)

    def log(message):
        with ws.lock:
            state["agent_log"].append(dict(time=time.strftime("%H:%M:%S"), message=message))
            state["agent_log"] = state["agent_log"][-100:]
            services.save()

    log("開始。人の確定ラベルを保護し、1サイクルで停止します。")
    if not state["candidates"]:
        items = services.candidate_list(state["keywords"], True)
        with ws.lock:
            state["candidates"] = items
    services.progress(10, "判断基準から分類候補を選んでいます")
    result = services.complete(
        settings,
        SELECTION_SYSTEM,
        {
            "keywords": state["keywords"],
            "criteria": criteria,
            "candidates": [{**c, "key": classification_key(c)} for c in state["candidates"]],
        },
    )
    chosen = result.get("selected")
    chosen = services.normalize_selection(chosen)
    if stop.is_set():
        return
    with ws.lock:
        state["selected"] = chosen
        services.make_query(bool(state["queries"]), preserve_conditions=True)
    log("分類を選択し初案を作成: " + str(result.get("reason", ""))[:500])
    result = services.run_llm_assessment(
        dict(criteria=criteria, max_items=max_items, threshold=threshold, fresh_only=True),
        lambda value, message: services.progress(20 + int(0.55 * value), message),
    )
    if stop.is_set():
        return
    log(
        f"特許判定: {result.get('completed_count', 0)}件を処理、採用{result.get('judged_count', 0)}件・保留{result.get('deferred_count', 0)}件。既存の判定は保持しました。"
    )
    if body.get("train"):
        services.run_model_training(
            body.get("mode", "lightweight"),
            True,
            lambda value, message: services.progress(75 + int(0.15 * value), message),
        )
        if stop.is_set():
            return
        log("学習完了。エージェント判断を含むため独立評価は表示しません。")
    if state["patents"]:
        with ws.lock:
            terms = refinement_terms(state["patents"])
            addition = terms["include"][:2]
            kept = [r for r in state["patents"] if r.get("label") == "keep"]
            covered = kept and all(
                any(w.casefold() in (r["title"] + " " + r.get("abstract", "")).casefold() for w in addition)
                for r in kept
            )
            # Do not introduce a mandatory vocabulary clause that drops known positives.
            previous_include = previous_query_for_context(ws).get("include_terms", [])
            current_keys = services.normalize_selection(state["selected"])
            feedback = services.classification_feedback(state["patents"], current_keys)
            next_keys = list(dict.fromkeys(current_keys + feedback["recommended_keys"]))
            services.make_query(
                True,
                None if previous_include else addition if covered else None,
                None,
                classification_keys=next_keys,
            )
        log(
            "判定済み公報から必要側の支持が多いIPCを追加し、次の検索式を提案しました。既存分類は保持し、分類のNOTは作りません。"
        )
    log("次のCSVを待っています。外部の特許検索・ダウンロードは接続されていません。")
