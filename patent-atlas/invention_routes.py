"""Practitioner workflow: invention evidence -> elements -> scoped search statements."""

import copy
import time
import uuid

from fastapi import HTTPException

import classification_bridge
import classification_explorer as explorer
import classification_retrieval
from research_strategy import selected_sources
from invention_strategy import plan_invention, compose_invention_query
from common import text_contains


def register_invention_routes(app, context, services):
    state, settings, lock = context["STATE"], context["SETTINGS"], context["LOCK"]
    wb, rows = services["workbench"], services["source_patents"]
    check_revision, commit = services["check_revision"], services["commit"]
    fingerprint, checked_brief = services["fingerprint"], services["checked_brief"]
    idle = context["idle"]

    def candidate_pool(brief, patents, input_data):
        # Actual seed metadata first. Existing proposed classifications stay
        # proposals; membership in this pool never makes a code mandatory.
        pool, notes = [], []
        for patent in patents:
            if patent["id"] not in brief["target_ids"]:
                continue
            extracted = classification_bridge.extract(patent)
            pool = explorer.merge_candidates(pool, extracted["items"])
            notes.extend(extracted["warnings"])
        # Before decomposition, only documentary/explicit seeds are supplied.
        # New classification hypotheses are retrieved AFTER elements exist.
        # User-selected seeds are deliberate input; do not silently erase them.
        explicit = [c for c in state.get("candidates", []) if explorer.key(c) in state.get("selected", [])]
        if explicit:
            pool = explorer.merge_candidates(pool, explicit)
            notes.append("対話で探索で選択済みの分類も残しています。今回の主題との適合性を確認してください。")
        pool = [explorer.enrich(c) for c in pool]
        pool = [c for c in pool if c.get("selectable")]
        if len(pool) > 120:
            notes.append("分類候補はターゲット由来を優先して120件まで表示しています。")
        return pool[:120], list(dict.fromkeys(notes))

    @app.post("/api/research/invention/plan")
    def create_plan(body: dict):
        use_llm = body.get("use_llm", False)
        if not isinstance(use_llm, bool):
            raise ValueError("LLM利用はtrue/falseで指定してください。")
        with lock:
            idle()
            check_revision(body)
            patents = rows()
            brief = checked_brief(body.get("brief", {}), wb()["brief"], patents)
            if not brief["purpose"]:
                raise ValueError("今回の検索目的を入力してください。")
            input_data = {k: copy.deepcopy(brief[k]) for k in ("purpose", "goal", "keywords", "user_aspects")}
            input_data.update(
                invention_text=body.get("invention_text", ""), manual_elements=body.get("manual_elements", [])
            )
            sources = selected_sources(patents, brief["target_ids"])
            candidates, notes = candidate_pool(brief, patents, input_data)
            connection = copy.deepcopy(settings)
            stamp = fingerprint((wb(), state["patents"]))
        plan = plan_invention(input_data, sources, candidates, connection, use_llm=use_llm)
        search_elements = [
            dict(id=p["id"], name=e["name"] + " / " + p["name"], terms=p["terms"])
            for e in plan["elements"]
            for p in e["perspectives"]
        ]
        found = classification_retrieval.retrieve_elements(search_elements, limit=96)
        if use_llm:
            import classification_planner
            from llm import complete

            found, rejected = classification_planner.review(
                brief["keywords"] + " " + input_data["invention_text"][:4000],
                search_elements,
                found,
                connection,
                complete,
            )
            if rejected:
                notes.append("提示候補にない分類キーを除外しました。")
        seeds = {c["key"]: c for c in candidates}
        candidates = explorer.merge_candidates(candidates, found)
        for candidate in candidates:
            original = seeds.get(candidate["key"])
            if original and original.get("origins"):
                candidate["hypothesis_reason"] = candidate.get("reason", "")
                for field in ("reason", "evidence_status", "group"):
                    if field in original:
                        candidate[field] = copy.deepcopy(original[field])
        candidates = [explorer.enrich(c) for c in candidates][:120]
        for e in plan["elements"]:
            for p in e["perspectives"]:
                p["retrieved_classification_keys"] = [
                    c["key"] for c in candidates if p["id"] in c.get("reviewed_element_ids", c.get("facets", []))
                ]
                # Retrieval never silently activates classification restrictions.
        import classification_planner

        missing = classification_planner.unresolved(search_elements, found)
        plan["unresolved_classification_elements"] = missing
        if missing:
            notes.append(
                "分類未発見の観点: " + "、".join(e["name"] for e in missing) + "。語句の補足で再探索できます。"
            )
        notes.append("要素分解の後で各観点から公式IPC/FIを検索しました。候補の追加は検索式の必須条件化ではありません。")
        if not use_llm:
            notes.append("手入力要素の語句による辞書検索です。LLMによる意味分解・適合性照合は未実施です。")
        plan.update(
            id=uuid.uuid4().hex,
            brief=brief,
            target_ids=brief["target_ids"],
            classification_candidates=candidates,
            classification_notes=notes,
            created_at=time.strftime("%Y-%m-%d %H:%M"),
        )
        with lock:
            idle()
            if stamp != fingerprint((wb(), state["patents"])):
                raise HTTPException(409, "要素分解中に調査条件・特許群が変わりました。前の案を保持しています。")
            references = services["merge_target_references"](
                wb()["target_patents"], [p for p in patents if p["id"] in brief["target_ids"]]
            )

            def action():
                if brief != wb()["brief"]:
                    wb()["plan"] = None
                wb().update(
                    brief=brief,
                    entry_mode=brief["entry_mode"],
                    target_patents=references,
                    invention=dict(input=copy.deepcopy(plan["input"]), plan=plan),
                )

            return commit(action)

    def proposed(body):
        plan = (wb().get("invention") or {}).get("plan")
        if not plan or plan["id"] != body.get("plan_id"):
            raise HTTPException(409, "要素分解の案が変わっています。現在の案で確認してください。")
        if plan["brief"] != wb()["brief"]:
            raise HTTPException(409, "調査条件が要素分解後に変わっています。要素を再起案してください。")
        query = compose_invention_query(plan, body, plan["classification_candidates"])
        query.update(
            purpose=plan["purpose"],
            target_ids=copy.deepcopy(plan["target_ids"]),
            prompt_version=plan["prompt_version"],
            keyword_context=plan["brief"]["keywords"],
            invention_plan_id=plan["id"],
        )
        query = services["enrich_query"](query)
        warnings = list(query.pop("warnings", []))
        # This checks observed words only. It cannot claim DB recall or prove
        # whether a branch would match a patent's unavailable full text.
        protected = {p["id"]: p for p in rows() if p["id"] in plan["target_ids"] or p.get("label") == "keep"}
        for exclusion in body.get("exclusions", []):
            terms = exclusion["terms"]
            hits = [
                p["id"]
                for p in protected.values()
                if any(text_contains(p, term, separate_fields=True) for term in terms)
            ]
            if hits:
                scope = "検索全体" if exclusion["scope"] == "all" else exclusion["scope"] + " の枝"
                warnings.append(
                    scope
                    + "の除外語がターゲット・必要特許の名称/要約にも現れます: "
                    + "、".join(hits[:10])
                    + "。枝への一致や全文は未確認です。"
                )
        if body.get("strategy") == "element":
            warnings.append("要素単独の探索です。他の要素との組み合わせや発明全体の一致を確認したものではありません。")
        warnings.append("検索件数・全文での一致は未評価です。出力先で検索し、結果をCSVで取り込んで比較してください。")
        # Warnings are part of the preview contract, so stale review cannot be
        # applied after relevant source data or the exclusion scope changes.
        return query, list(dict.fromkeys(warnings))

    @app.post("/api/research/invention/preview")
    def preview(body: dict):
        with lock:
            check_revision(body)
            query, warnings = proposed(body)
            return dict(
                query=query,
                expression=query["expression"],
                warnings=warnings,
                preview_hash=fingerprint((query, warnings)),
            )

    @app.post("/api/research/invention/apply")
    def apply(body: dict):
        with lock:
            idle()
            check_revision(body)
            query, warnings = proposed(body)
            if body.get("expected_preview_hash") != fingerprint((query, warnings)):
                raise HTTPException(409, "最新の要素・分類・除外範囲をプレビューしてから採用してください。")

            def action():
                state["keywords"] = wb()["brief"]["keywords"]
                saved = services["save_query"](query, "発明要素からの検索式")
                saved["note"] = (
                    "選択した発明要素・観点・分類の括弧構造を保持した検索式です。要素単独と組合せの検索を区別します。"
                )
                saved["review_notes"] = warnings

            return commit(action)
