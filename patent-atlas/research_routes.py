"""Four research entry paths share the existing workspace, settings and query history."""

from workspace import Workspace
from research_services import ResearchServices

import copy
import hashlib
import time
import uuid
import threading

from fastapi import HTTPException

import classification_bridge
import classification_explorer as explorer
from catalog import keywords
from query_formats import Node, group, tree_data
from workspace_import import merge_patents, publication_key
from common import fingerprint, text_contains

from research_services import (
    merge_target_references,
    new_workbench as new_workbench,
    checked_brief,
    concept_conditions,
    refined_tree as refined_tree,
)


def register_research_routes(app, ws: Workspace, services: ResearchServices):
    from research_strategy import plan_research
    from query_library import parse_example, adapt_example
    from projection_engine import project_patents, compute_evaluation

    state, settings, lock = ws.state, ws.settings, ws.lock
    idle, save, public = ws.idle, services.app.save, services.app.public_state
    cache = {}
    projection_lock = threading.Lock()

    workbench, check_revision, source_patents = services.workbench, services.check_revision, services.source_patents
    commit, enrich_query, save_query = services.commit, services.enrich_query, services.save_query

    @app.post("/api/research/brief")
    def update_brief(body: dict):
        with lock:
            idle()
            check_revision(body)
            brief = checked_brief(body, workbench()["brief"], source_patents())

            def action():
                wb = workbench()
                if wb["brief"] != brief:
                    wb["plan"] = None
                wb.update(brief=brief, entry_mode=brief["entry_mode"])

            return commit(action)

    @app.post("/api/research/plan")
    def create_plan(body: dict):
        use_llm = body.get("use_llm", False)
        if not isinstance(use_llm, bool):
            raise ValueError("LLM利用はtrue/falseで指定してください。")
        with lock:
            idle()
            check_revision(body)
            brief = checked_brief(body.get("brief", {}), workbench()["brief"], source_patents())
            if not brief["purpose"] or not (brief["goal"] or brief["keywords"] or brief["target_ids"]):
                raise ValueError("調査目的と、技術の説明・キーワード・ターゲットのいずれかを入力してください。")
            if brief["entry_mode"] == "target" and not brief["target_ids"]:
                raise ValueError("ターゲット特許を選んでください。ない場合は「ターゲットを探す」を使えます。")
            rows, connection = source_patents(), settings.copy()
            stamp = fingerprint((workbench(), state["patents"]))
        plan = plan_research(brief, rows, connection, use_llm=use_llm)
        candidates, notes = [], []
        for row in rows:
            if row["id"] not in brief["target_ids"]:
                continue
            if row.get("seed_reference_fields"):
                notes.append(
                    row["id"]
                    + ": 今回のCSVの空欄を保存済みターゲット参照から補完: "
                    + "、".join(row["seed_reference_fields"])
                )
            result = classification_bridge.extract(row)
            candidates = explorer.merge_candidates(candidates, result["items"])
            notes.extend(result["warnings"])
            for item in result["items"]:
                parent = explorer.parent(item["kind"], item["code"]) if item["kind"] in ("IPC", "F-term") else None
                if parent:
                    parent = explorer.enrich(
                        {
                            **parent,
                            "reason": f"ターゲット {row['id']} の {item['code']} から上位へ拡張。",
                            "origins": [{"type": "target_parent", "patent_id": row["id"], "source_code": item["code"]}],
                        }
                    )
                    candidates = explorer.merge_candidates(candidates, [parent])
        all_terms = list(
            dict.fromkeys(
                keywords(brief["keywords"])
                + [t for c in plan["concepts"] for t in c.get("terms", []) + c.get("abstract_terms", [])]
            )
        )
        used_terms, vocabulary = [], ""
        for term in all_terms:
            token = '"' + term.replace('"', " ") + '"'
            if len(used_terms) >= 24 or len(vocabulary) + len(token) + 1 > 2900:
                break
            used_terms.append(term)
            vocabulary += (" " if vocabulary else "") + token
        if len(used_terms) < len(all_terms):
            notes.append(
                "辞書による分類仮説は先頭"
                + str(len(used_terms))
                + "語を使用しています。残りの語は観点・検索式に保持します。"
            )
        classification_proposal = {}
        if use_llm:
            try:
                hypotheses = services.app.candidate_list(
                    vocabulary or brief["keywords"],
                    True,
                    notices=notes,
                    proposal=classification_proposal,
                    connection=connection,
                    research_context=dict(purpose=plan["purpose"], concepts=plan["concepts"], sources=plan["sources"]),
                )
                classification_proposal["status"] = "completed"
            except ValueError as error:
                hypotheses = services.app.candidate_list(vocabulary or brief["keywords"], False)
                classification_proposal.update(status="failed", error=str(error))
                notes.append(
                    "LLMの分類提案を取得できませんでした。観点案・ターゲットの根拠・辞書仮説は保持しています: "
                    + str(error)
                )
        else:
            hypotheses = services.app.candidate_list(vocabulary or brief["keywords"], False)
            classification_proposal["status"] = "not_requested"
        for item in hypotheses:
            if len(candidates) >= 120:
                break
            item = {
                **item,
                "reason": "観点の語句に基づく探索仮説。公報への付与を示しません。 " + item.get("reason", ""),
                "origins": item.get("origins", []) + [{"type": "concept_hypothesis"}],
            }
            existing = next((c for c in candidates if c["key"] == item["key"]), None)
            if existing:
                item["reason"] = existing.get("reason", item["reason"])
                for field in ("evidence_status", "classification_origins"):
                    if field in existing:
                        item[field] = copy.deepcopy(existing[field])
            candidates = explorer.merge_candidates(candidates, [item])
        plan.update(
            id=uuid.uuid4().hex,
            classification_candidates=candidates,
            classification_notes=list(dict.fromkeys(notes)),
            classification_proposal=classification_proposal,
            target_ids=brief["target_ids"],
            created_at=time.strftime("%Y-%m-%d %H:%M"),
        )
        with lock:
            idle()
            if stamp != fingerprint((workbench(), state["patents"])):
                raise HTTPException(
                    409, "計画作成中に条件・特許群が変わりました。以前の計画を保持しました。再実行してください。"
                )
            references = merge_target_references(
                workbench()["target_patents"], [row for row in rows if row["id"] in brief["target_ids"]]
            )
            return commit(
                lambda: workbench().update(
                    brief=brief, entry_mode=brief["entry_mode"], plan=plan, target_patents=references
                )
            )

    def proposed_query(body):
        plan = workbench().get("plan")
        if not plan or body.get("plan_id") != plan["id"]:
            raise HTTPException(409, "観点案が更新されています。現在の計画を確認してください。")
        node, facets = concept_conditions(plan, body)
        present = {t.casefold() for facet in facets for t in facet["terms"]}
        dropped = [t for t in plan.get("unmapped_keywords", []) if t.casefold() not in present]
        if dropped and body.get("acknowledge_unmapped") is not True:
            raise ValueError(
                "観点に未反映の入力語があります。語句に追加するか、省略する語を確認してください: " + "、".join(dropped)
            )
        concept_tree = tree_data(node)
        selected = body.get("classification_keys", [])
        available = {c["key"]: c for c in plan["classification_candidates"]}
        if (
            not isinstance(selected, list)
            or len(selected) > 120
            or any(not isinstance(k, str) or k not in available for k in selected)
        ):
            raise ValueError("計画に含まれる分類から選んでください。")
        classes = [available[key] for key in dict.fromkeys(selected)]
        if any(not c.get("selectable") for c in classes):
            raise ValueError("案内用の分類は選べません。下位または上位の検索可能な分類を選んでください。")
        excluded = [t for f in facets if f["role"] == "exclude" for t in f["terms"]]
        current_keep_ids = {r["id"] for r in state["patents"] if r.get("label") == "keep"}
        protected_ids = current_keep_ids | set(plan["target_ids"])
        kept = [r for r in source_patents() if r["id"] in protected_ids]
        if any(text_contains(r, t) for t in excluded for r in kept):
            raise ValueError(
                "ターゲットまたは必要特許にも現れる語を除外しようとしています。除外観点を見直してください。"
            )
        if classes:
            node = group("and", [node, group("or", [Node("class", c["code"], c["kind"]) for c in classes])])
        return enrich_query(
            dict(
                boolean_tree=tree_data(node),
                concept_tree=concept_tree,
                facets=facets,
                classifications=copy.deepcopy(classes),
                purpose=workbench()["brief"]["purpose"],
                target_ids=plan["target_ids"],
                omitted_input_keywords=dropped,
                prompt_version=plan.get("prompt_version"),
                include_terms=[],
                exclude_terms=[],
            )
        )

    @app.post("/api/research/preview")
    def preview(body: dict):
        with lock:
            check_revision(body)
            query = proposed_query(body)
            return dict(query=query, expression=query["expression"], preview_hash=fingerprint(query))

    @app.post("/api/research/apply")
    def apply(body: dict):
        with lock:
            idle()
            check_revision(body)
            query = proposed_query(body)
            if body.get("expected_preview_hash") != fingerprint(query):
                raise HTTPException(409, "最新の条件でプレビューを確認してから検索式を作成してください。")

            def action():
                state["keywords"] = workbench()["brief"]["keywords"]
                save_query(query, "観点からの初案")

            return commit(action)

    @app.post("/api/research/targets/add")
    def add_target(body: dict):
        with lock:
            idle()
            current_ids = checked_brief(
                {"target_ids": body.get("target_ids", workbench()["brief"]["target_ids"])},
                workbench()["brief"],
                source_patents(),
            )["target_ids"]
            row = {}
            for field in ("id", "title", "abstract", "ipc", "fi", "fterm", "cpc", "applicant"):
                value = body.get(field, "")
                if not isinstance(value, str) or len(value) > (200 if field == "id" else 12000):
                    raise ValueError("特許の各欄を文字列・上限以内で入力してください。")
                row[field] = value.strip()
            if not row["id"] or not row["title"]:
                raise ValueError("公報番号と発明の名称を入力してください。")
            classification_bridge.extract(row, strict=True)
            row.update(
                label=None,
                label_source=None,
                label_reason="",
                score=None,
                content_key=hashlib.sha256((row["title"] + row["abstract"]).encode()).hexdigest()[:16],
            )
            rows, report = merge_patents(state["patents"], [row])
            canonical_id = next(p["id"] for p in rows if publication_key(p) == publication_key(row))
            if canonical_id not in current_ids and len(current_ids) >= 20:
                raise ValueError("ターゲットは20件までです。選択を整理してください。")
            ids = current_ids + ([] if canonical_id in current_ids else [canonical_id])
            sources = {p["id"]: p for p in source_patents()}
            sources[canonical_id] = next(p for p in rows if p["id"] == canonical_id)
            references = merge_target_references(
                workbench()["target_patents"], [sources[identifier] for identifier in ids]
            )

            def action():
                clusters = services.app.map_patents(rows, state["keywords"])
                state.update(patents=rows, clusters=clusters)
                if report["added"] and state.get("training"):
                    state["training"].update(
                        dataset_changed=True,
                        added_since_training=state["training"].get("added_since_training", 0) + report["added"],
                    )
                workbench()["brief"]["target_ids"] = ids
                workbench()["last_added_target_id"] = canonical_id
                workbench()["target_patents"] = references
                workbench()["plan"] = None

            return commit(action)

    @app.post("/api/research/library/import")
    def import_example(body: dict):
        entry = parse_example(
            body.get("text", ""),
            source_format=body.get("source_format", "jplatpat"),
            name=body.get("name", ""),
            purpose=body.get("purpose", ""),
        )
        with lock:
            idle()
            if len(workbench()["library"]) >= 50:
                raise ValueError("式例は50件までです。必要な式を書き出して整理してください。")
            return commit(lambda: workbench()["library"].append(entry))

    def adapted(body):
        entry = next((e for e in workbench()["library"] if e["id"] == body.get("example_id")), None)
        if entry is None:
            raise ValueError("式例が見つかりません。")
        query = adapt_example(entry, body.get("replacements", {}), body.get("class_replacements", {}))
        purpose = body.get("purpose", workbench()["brief"]["purpose"] or entry.get("purpose", ""))
        if not isinstance(purpose, str) or len(purpose) > 300:
            raise ValueError("今回の調査目的は300文字以内で指定してください。")
        query["purpose"] = purpose
        return enrich_query(query)

    @app.post("/api/research/library/suggest")
    def suggest_example(body: dict):
        from query_adaptation import suggest_adaptation

        with lock:
            idle()
            entry = next((copy.deepcopy(e) for e in workbench()["library"] if e["id"] == body.get("example_id")), None)
            if entry is None:
                raise ValueError("式例が見つかりません。")
            connection = settings.copy()
        return suggest_adaptation(entry, body.get("purpose", ""), body.get("goal", ""), connection)

    @app.post("/api/research/library/adapt")
    def preview_example(body: dict):
        with lock:
            query = adapted(body)
            return dict(
                query=query,
                expression=query["expression"],
                preview_hash=fingerprint(query),
                warnings=[
                    "技術分野を変える場合は各語と分類の意味を確認してください。モデル重みの学習ではなく検索条件の調整です。"
                ],
            )

    @app.post("/api/research/library/apply")
    def apply_example(body: dict):
        with lock:
            idle()
            query = adapted(body)
            if body.get("expected_preview_hash") != fingerprint(query):
                raise HTTPException(409, "置換後の式をプレビューしてから採用してください。")
            return commit(lambda: save_query(query, "過去の式から調整"))

    @app.post("/api/map/project")
    def project(body: dict):
        method = body.get("method", "pca")
        if not isinstance(method, str) or method not in ("pca", "tsne", "umap"):
            raise ValueError("配置方式はpca・tsne・umapから選んでください。")
        with lock:
            rows = copy.deepcopy(state["patents"])
            revision = fingerprint(rows)
            targets = list(workbench()["brief"]["target_ids"])
        cache_key = (fingerprint([(p["id"], p["title"], p.get("abstract", "")) for p in rows]), method)
        # Reference orientation must be recomputed per request, not cached across callers.
        if not projection_lock.acquire(blocking=False):
            raise HTTPException(409, "配置の計算中です。完了してから切り替えてください。")
        try:
            if body.get("reference") is None and cache_key in cache:
                projection = copy.deepcopy(cache[cache_key])
            else:
                projection = project_patents(rows, method=method, reference=body.get("reference"))
                if body.get("reference") is None:
                    if len(cache) >= 3:
                        cache.clear()
                    cache[cache_key] = copy.deepcopy(projection)
            evaluation = compute_evaluation(rows, target_ids=targets)
        finally:
            projection_lock.release()
        with lock:
            if revision != fingerprint(state["patents"]) or targets != workbench()["brief"]["target_ids"]:
                raise HTTPException(409, "配置計算中に特許・判定が変わりました。もう一度配置してください。")
        return dict(projection=projection, evaluation=evaluation, dataset_revision=revision)

    @app.get("/api/map/evaluation")
    def evaluation():
        with lock:
            rows, targets = copy.deepcopy(state["patents"]), list(workbench()["brief"]["target_ids"])
            revision = fingerprint(rows)
        result = compute_evaluation(rows, target_ids=targets)
        with lock:
            if revision != fingerprint(state["patents"]) or targets != workbench()["brief"]["target_ids"]:
                raise HTTPException(409, "評価中に特許群またはターゲットが変わりました。再表示してください。")
        return result

    @app.post("/api/research/adopt")
    def adopt(body: dict):
        with lock:
            idle()
            query = next((q for q in state["queries"] if q["id"] == body.get("query_id")), None)
            if query is None:
                raise ValueError("評価する検索式を選んでください。")
            note = body.get("note", "")
            if not isinstance(note, str) or not note.strip() or len(note) > 2000:
                raise ValueError("採用理由を1〜2,000文字で入力してください。")
            rows, targets = copy.deepcopy(state["patents"]), list(workbench()["brief"]["target_ids"])
            observation = copy.deepcopy(state.get("search_result") or {})
            if observation.get("query_id") != query["id"] and body.get("allow_unlinked") is not True:
                raise ValueError(
                    "この特許群と検索式の実行履歴の対応が未確認です。参考評価として採用する場合は、その旨を確認してください。"
                )
            revision = fingerprint((rows, targets, observation))
            query_id = query["id"]
        assessment = compute_evaluation(rows, target_ids=targets)
        with lock:
            idle()
            if revision != fingerprint(
                (state["patents"], workbench()["brief"]["target_ids"], state.get("search_result") or {})
            ):
                raise HTTPException(409, "評価中に特許群・観測が変わりました。もう一度採用を記録してください。")
            record = dict(
                query_id=query_id,
                note=note.strip(),
                created_at=time.strftime("%Y-%m-%d %H:%M"),
                evaluation=assessment,
                dataset_revision=fingerprint(rows),
                search_result_id=observation.get("id"),
                observed_query_id=observation.get("query_id"),
                query_matches_observation=observation.get("query_id") == query_id,
                scope="現在の特許群に対する判断指標。母集団全体の再現率・検索式の実行評価ではありません。",
            )
            return commit(lambda: workbench().update(adoptions=(workbench()["adoptions"] + [record])[-100:]))
