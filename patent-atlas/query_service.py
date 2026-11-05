"""Classification selection, classification view and query history on a Workspace.

Every function takes the Workspace first and touches only ``ws.state``; the
caller owns the lock and the transaction. ``refinement_terms`` is injectable
so the application keeps its replaceable seam.
"""

import copy
import time
import uuid

import classification_explorer as explorer
from analysis_engine import refinement_terms as default_refinement_terms
from catalog import keywords
from query_formats import Node, describe_tree, group, terms_of, tree_data, tree_from_data
from refinement_classifications import classification_feedback, feedback_origin
from research_services import refined_tree
from research_strategy import suggest_keyword_or_group


def classification_key(candidate):
    return candidate["kind"] + ":" + candidate["code"]


def normalize_selection(ws, values, candidates=None):
    candidates = ws.state["candidates"] if candidates is None else candidates
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError("分類の選択はキーのリストにしてください。")
    out = []
    for value in values:
        found = [c for c in candidates if classification_key(c) == value]
        if not found:
            found = [c for c in candidates if c["code"] == value]
        if len(found) != 1:
            raise ValueError("分類を体系付きで選び直してください: " + value)
        key = classification_key(found[0])
        if explorer.enrich(found[0]).get("selectable") is False:
            raise ValueError(
                "この分類は案内専用、または上位コードが未確認です。辞書で確認できるIPCか、F-termのタームを選んでください: "
                + value
            )
        if key not in out:
            out.append(key)
    return out


def set_classification_view(ws, items, **metadata):
    selected = normalize_selection(ws, ws.state["selected"])
    additional = items + ([metadata["focus"]] if metadata.get("focus") else [])
    merged = explorer.merge_candidates(ws.state["candidates"], additional)
    ws.state.update(
        candidates=merged,
        selected=selected,
        classification_view={"keys": [classification_key(c) for c in items], **metadata},
    )


def classification_overview(ws, items=None):
    items = ws.state["candidates"] if items is None else items
    coarse = explorer.overview(items, ws.state["keywords"])
    origins = [c for c in ws.state["candidates"] if c.get("origins")]
    rows = explorer.merge_candidates(coarse, origins)
    proposal = ws.state.get("candidate_proposal") or {}
    if proposal.get("llm_used") and proposal.get("keywords") == ws.state["keywords"]:
        rows = explorer.merge_candidates(rows, proposal.get("items", []))
    note = (
        "材料・製造・界面などの観点から、広い分類を表示しています。候補は探索仮説です。気になる分類の下位へ進んで適合性を確認できます。"
        if ws.state["keywords"].strip()
        else "キーワードが空欄のため、現在の候補と推薦した分類の上位群を表示しています。キーワードを入力すると他分野の候補も探せます。"
    )
    set_classification_view(ws, rows, direction="overview", note=note, total=len(rows), next_offset=None, trail=[])


def previous_query_for_context(ws, ignore_classifications=False):
    if not ws.state["queries"]:
        return {}
    previous = ws.state["queries"][-1]
    if (
        previous.get("keyword_context") != ws.state["keywords"]
        if previous.get("boolean_tree")
        else previous.get("keywords") != keywords(ws.state["keywords"])
    ):
        return {}
    if not ignore_classifications and {classification_key(c) for c in previous.get("classifications", [])} != set(
        normalize_selection(ws, ws.state["selected"])
    ):
        return {}
    return previous


def make_query(
    ws,
    refine=False,
    include=None,
    exclude=None,
    preserve_conditions=False,
    classification_keys=None,
    *,
    refinement_terms=default_refinement_terms,
):
    words = keywords(ws.state["keywords"])
    original_keys = normalize_selection(ws, ws.state["selected"])
    selected_keys = original_keys
    pool = ws.state["candidates"]
    classification_changes = None
    if classification_keys is not None:
        if (
            not refine
            or not isinstance(classification_keys, list)
            or len(classification_keys) > 1500
            or any(not isinstance(key, str) or ":" not in key for key in classification_keys)
        ):
            raise ValueError("改善案の分類は体系付きキーのリストで指定してください。")
        feedback = classification_feedback(ws.state["patents"], original_keys)
        evidence_pool = {item["key"]: item for item in feedback["candidates"]}
        allowed = {classification_key(item) for item in pool} | set(evidence_pool)
        if any(key not in allowed for key in classification_keys):
            raise ValueError("分類候補にないキーが含まれています。要否判定から再分析してください。")
        incoming = []
        evidence = []
        for key in dict.fromkeys(classification_keys):
            if key in evidence_pool:
                item = copy.deepcopy(evidence_pool[key])
                origin = feedback_origin(item)
                item.setdefault("origins", []).append(origin)
                incoming.append(item)
                evidence.append(origin)
        pool = explorer.merge_candidates(pool, incoming)
        selected_keys = normalize_selection(ws, classification_keys, pool)
        classification_changes = dict(
            added_keys=[key for key in selected_keys if key not in original_keys],
            removed_keys=[key for key in original_keys if key not in selected_keys],
            selected_keys=list(selected_keys),
            evidence=evidence,
            note=feedback["note"],
        )
        preserve_conditions = True
    selected = copy.deepcopy([c for c in pool if classification_key(c) in selected_keys])
    if not words and not selected and not (refine and ws.state["queries"] and ws.state["queries"][-1].get("boolean_tree")):
        raise ValueError("キーワードを入力するか、検索に使う分類を選んでください。")

    def quote(s):
        return '"' + s.replace('"', " ").replace("\n", " ").strip() + '"'

    base = " AND ".join("TEXT=" + quote(w) for w in words)
    classes = " OR ".join(c["kind"].upper() + "=" + quote(c["code"]) for c in selected)
    query = "(" + base + ")" if base else ""
    if classes:
        query += ("\nAND " if query else "") + "(" + classes + ")"
    previous = previous_query_for_context(ws, preserve_conditions) if refine else {}
    if refine and ws.state["queries"] and ws.state["queries"][-1].get("boolean_tree") and not previous:
        raise ValueError(
            "観点・取り込み式と現在のキーワードまたは分類が変わっています。調査ナビで案を作り直すか、分類の改善案として更新してください。元の論理条件は保持しています。"
        )
    old_include, old_exclude = previous.get("include_terms", []), previous.get("exclude_terms", [])
    for values in (include, exclude):
        if values is not None and (not isinstance(values, list) or any(not isinstance(v, str) for v in values)):
            raise ValueError("追加語・除外語は語句のリストにしてください。")
    include = list(dict.fromkeys(old_include if include is None else include))
    exclude = list(dict.fromkeys(old_exclude if exclude is None else exclude))
    term_options = refinement_terms(ws.state["patents"]) if refine else {"include": [], "exclude": []}
    term_options = {
        k: list(dict.fromkeys(term_options[k] + (old_include if k == "include" else old_exclude))) for k in term_options
    }
    for kind, chosen_terms in (("include", include), ("exclude", exclude)):
        if any(term not in term_options[kind] for term in chosen_terms):
            raise ValueError("対応する追加・除外候補にない語が含まれています。再分析してください。")
    if include:
        query += "\nAND (" + " OR ".join("TEXT=" + quote(w) for w in include) + ")"
    if exclude:
        kept = [r for r in ws.state["patents"] if r.get("label") == "keep"]
        unsafe = [
            w for w in exclude if any(w.casefold() in (r["title"] + r.get("abstract", "")).casefold() for r in kept)
        ]
        if unsafe:
            raise ValueError("必要特許にも出現するため除外できない語: " + "、".join(unsafe))
        query += "\nNOT (" + " OR ".join("TEXT=" + quote(w) for w in exclude) + ")"
    kept = [r for r in ws.state["patents"] if r.get("label") == "keep"]
    retained = sum(
        not include or any(w.casefold() in (r["title"] + " " + r.get("abstract", "")).casefold() for w in include)
        for r in kept
    )
    removed = [w for w in old_include if w not in include] + [w for w in old_exclude if w not in exclude]
    item = dict(
        id=uuid.uuid4().hex,
        version=len(ws.state["queries"]) + 1,
        created_at=time.strftime("%Y-%m-%d %H:%M"),
        expression=query,
        type="改善案" if refine else "初案",
        terms=term_options,
        keywords=words,
        classifications=selected,
        include_terms=include,
        exclude_terms=exclude,
        removed_terms=removed,
        known_keep_coverage={"retained": retained, "total": len(kept)} if include and kept else None,
        note="DB非依存の設計式です。TEXT・IPC・F-TERMは検索先の入力欄に割り当ててください。分類群はOR、キーワードはANDです。",
        changes=(
            "要否判断から特徴語を抽出。NOTは選択された語だけ適用。" if refine else "入力語と選択分類から初案を作成。"
        ),
    )
    if not refine:
        groups = {}
        for index, word in enumerate(words):
            union, _ = suggest_keyword_or_group(word)
            key = ("union", union) if union else ("single", index)
            groups.setdefault(key, []).append(Node("text", word))
        if any(len(alternatives) > 1 for alternatives in groups.values()):
            concepts = group("and", [group("or", alternatives) for alternatives in groups.values()])
            draft = dict(concept_tree=tree_data(concepts))
            base_tree, boolean_tree = refined_tree(draft, selected, include, exclude)
            item.update(
                concept_tree=tree_data(concepts),
                base_boolean_tree=base_tree,
                boolean_tree=boolean_tree,
                keyword_context=ws.state["keywords"],
                expression=describe_tree(tree_from_data(boolean_tree)),
                note="既知の代替表現を仮のORグループにまとめ、別の条件とANDで結びました。調査ナビで意味・和集合の範囲を見直せます。",
            )
            item["term_catalog"] = terms_of(item)
    if previous.get("boolean_tree"):
        base_tree, boolean_tree = refined_tree(previous, selected, include, exclude)
        for name in (
            "concept_tree",
            "facets",
            "purpose",
            "provenance",
            "target_ids",
            "prompt_version",
            "keyword_context",
            "invention_context",
            "invention_plan_id",
        ):
            if name in previous:
                item[name] = copy.deepcopy(previous[name])
        item.update(
            base_boolean_tree=base_tree,
            boolean_tree=boolean_tree,
            expression=describe_tree(tree_from_data(boolean_tree)),
            note="観点・取り込み式の論理構造を保持し、追加語と除外語を更新しています。",
        )
        item["term_catalog"] = terms_of(item)
    if classification_changes is not None:
        item["classification_changes"] = classification_changes
        item["changes"] = (
            "要否判定・学習予測の分類と特徴語から改善。FI・Fターム由来のIPCは対応づけた候補として扱い、選択した分類をOR条件に反映。NOTは選択された語だけ適用。"
        )
        ws.state.update(candidates=pool, selected=selected_keys)
    ws.state["queries"].append(item)
    if isinstance(ws.state.get("research_workbench"), dict):
        ws.state["research_workbench"]["active_query_id"] = item["id"]
    return item
