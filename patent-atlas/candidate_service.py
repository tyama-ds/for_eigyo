"""Classification candidate generation and the display projection of saved reports.

The display helpers are pure. ``candidate_list`` reads only ``ws.settings`` for
the default connection; ``complete`` is injectable so the application keeps
its replaceable LLM seam.
"""

import copy
import re
import unicodedata

import classification_explorer as explorer
import classification_revision_notes
from discovery_services import public_discovery
from llm import complete as default_complete


def llm_classification_text(kind, code):
    """Strip only unambiguous display decoration, keeping incomplete symbols."""
    aliases = {"IPC": "IPC", "FI": "FI", "F-TERM": "F-term", "FTERM": "F-term", "F_TERM": "F-term", "Fターム": "F-term"}
    kind = aliases.get(unicodedata.normalize("NFKC", kind).strip().upper())
    if kind is None:
        raise ValueError("分類体系は IPC、FI または F-term を指定してください。")
    text = unicodedata.normalize("NFKC", code).strip()
    prefix = re.match(r"^(IPC|FI|CPC|F-TERM|FTERM|F_TERM|Fターム)\s*:\s*(.*)$", text, re.I)
    if prefix:
        if aliases.get(prefix[1].upper()) != kind:
            raise ValueError("kind とコードに付いた分類体系が一致しません。")
        text = prefix[2]
    text = re.sub(r"\s*\(\d{4}\.(?:0[1-9]|1[0-2])\)\s*$", "", text)
    return kind, text


def normalize_llm_classification(kind, code):
    """Remove unambiguous display decoration; never repair or infer a code."""
    kind, text = llm_classification_text(kind, code)
    try:
        return kind, explorer.normalize(kind, text)
    except ValueError:
        raise ValueError(
            "コード形式が不完全、または余分な文字が含まれています。1件に1つの完全なコードが必要です。"
        ) from None


def rejected_candidate_guidance(item):
    """Offer a separately reviewed broader scope, never validate a rejected code."""
    result = copy.deepcopy(item)
    try:
        kind, text = llm_classification_text(item.get("kind", ""), item.get("code", ""))
    except (ValueError, TypeError, AttributeError):
        return result
    if kind != "IPC":
        return result
    text = re.sub(r"\s+", "", text).upper()
    # Never discard FI suffixes, multiple codes, arbitrary prose or wildcards.
    match = re.fullmatch(r"([A-H]\d{2}[A-Z])(\d{1,4})(?:/(\d{1,6}))?", text)
    if not match:
        return result
    if explorer.ipc_catalog.lookup("IPC", text):
        return result
    revision = classification_revision_notes.lookup(text)
    if revision:
        result["revision"] = revision
        result["reason"] = revision["explanation"]
    scopes = []
    # A complete, missing subgroup can be explored from its existing main group.
    # A slash-less symbol is not silently completed with /00.
    if match[3] is not None:
        scopes.append(match[1] + str(int(match[2])) + "/00")
    scopes.append(match[1])
    for code in scopes:
        known = explorer.lookup("IPC", code)
        if not known or code == text:
            continue
        result["broader_candidate"] = explorer.enrich(
            {
                **known,
                "reason": f"{text} の代替と確定した分類ではなく、コードの先頭部分から確認できる広い探索起点です。正式な定義と調査テーマへの適合性を確認してください。",
                "recommendation_method": "broader_review",
            }
        )
        break
    return result


def display_candidate_proposal(proposal):
    if not isinstance(proposal, dict):
        return proposal
    result = copy.deepcopy(proposal)
    result["rejected"] = [
        rejected_candidate_guidance({**item, "index": item.get("index", index)})
        for index, item in enumerate(result.get("rejected", []), 1)
    ]
    # Older saved reports repeat every rejection in notices. The cards now carry
    # the explanation, while successful/zero-result notices remain unchanged.
    if result["rejected"]:
        result["notices"] = [
            note for note in result.get("notices", []) if not note.startswith(("LLM提案", "LLMが提案した "))
        ]
        result["notices"].insert(
            0,
            f"LLM提案{result.get('returned_count', 0)}件のうち{len(result['rejected'])}件を除外しました。詳細と確認できる上位分類を上に表示しています。",
        )
    return result


def display_labels(item, *, selection=False):
    """Project current captions and optional selection rules, preserving saved data."""
    if not isinstance(item, dict) or item.get("kind") not in ("IPC", "F-term", "CPC", "FI") or not item.get("code"):
        return item
    try:
        enriched = explorer.enrich(item)
    except (ValueError, TypeError):
        return item
    return {
        **item,
        **{
            name: value for name, value in enriched.items() if name.startswith(("title_ja", "title_en", "title_short_"))
        },
        **({name: enriched[name] for name in ("selectable", "selection_scope")} if selection else {}),
        **(
            {"matched_classifications": [display_labels(row) for row in item["matched_classifications"]]}
            if isinstance(item.get("matched_classifications"), list)
            else {}
        ),
    }


def display_discovery(discovery):
    value = copy.deepcopy(public_discovery(discovery))
    if value.get("plan"):
        value["plan"]["candidate_hypotheses"] = [
            display_labels(row, selection=True) for row in value["plan"].get("candidate_hypotheses", [])
        ]
    if value.get("analysis"):
        value["analysis"]["recommendations"] = [
            display_labels(row, selection=True) for row in value["analysis"].get("recommendations", [])
        ]
    return value


def llm_candidate_rows(result, notices, proposal=None):
    """Validate model data before touching the workspace or enriching codes."""
    if not isinstance(result, dict) or not isinstance(result.get("candidates"), list):
        raise ValueError(
            'LLMのJSONには candidates 配列が必要です。候補がない場合は {"candidates":[]} を返す必要があります。'
        )
    items = result["candidates"]
    if proposal is not None:
        proposal.update(returned_count=len(items), rejected=[], duplicate_count=0)
    if len(items) > 12:
        raise ValueError("LLMの分類候補が多すぎます。最大12件の短い候補一覧で再実行してください。")
    rows, rejected, seen, normalizations = [], [], set(), []

    def reject(index, item, reason):
        item = item if isinstance(item, dict) else {}

        def display(value):
            return value[:80] if isinstance(value, str) else "（文字列ではありません）"

        rejected.append(
            rejected_candidate_guidance(
                dict(index=index, kind=display(item.get("kind", "")), code=display(item.get("code", "")), reason=reason)
            )
        )

    for index, item in enumerate(items, 1):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("kind"), str)
            or len(item["kind"]) > 32
            or not isinstance(item.get("code"), str)
            or len(item["code"]) > 64
            or not isinstance(item.get("reason", ""), str)
        ):
            reject(index, item, "各候補には kind（IPC / F-term）と文字列の code・reason が必要です。")
            continue
        try:
            kind, code = normalize_llm_classification(item["kind"], item["code"])
        except ValueError as error:
            reject(index, item, str(error))
            continue
        if (kind, code) in seen:
            if proposal is not None:
                proposal["duplicate_count"] += 1
            continue
        seen.add((kind, code))
        known = explorer.lookup(kind, code)
        if kind == "FI" and not known:
            reject(
                index,
                {"kind": kind, "code": code},
                "収録FI表（2026.04）で未確認です。版の相違・誤記などを確認してください。",
            )
            continue
        if kind == "IPC" and not known:
            reject(
                index,
                {"kind": kind, "code": code},
                "収録IPC辞書（2026.01）で確認できません。旧版・誤記の可能性があります。",
            )
            continue
        if kind != item["kind"] or code != item["code"]:
            normalizations.append(
                dict(index=index, kind=kind, code=code, input_kind=item["kind"], input_code=item["code"])
            )
        # Model-written titles are not definitions. Use official captions when
        # available, otherwise show the code with explicit unverified status.
        row = explorer.enrich(
            dict(
                kind=kind,
                code=code,
                title=code,
                group="LLM候補",
                reason="LLMの探索仮説（適合性は未確認）：" + item.get("reason", "").strip()[:300],
                terms=[],
                score=0,
                recommendation_method="llm",
            )
        )
        rows.append(row)
    if proposal is not None:
        proposal.update(
            accepted_count=len(rows), rejected=rejected, rejected_count=len(rejected), normalizations=normalizations
        )
    if rejected:
        details = [f"{item['kind']} {item['code'] or '（コードなし）'}: {item['reason']}" for item in rejected]
        notice = f"LLM提案{len(items)}件のうち{len(rejected)}件を除外しました。" + " / ".join(details)
        if not rows:
            raise ValueError(
                "LLMの提案に反映できる分類がありません。"
                + notice
                + " 現行の完全なコード、または B60L のような上位分類で再生成してください。前回の候補・選択は保持しています。"
            )
        notices.append(notice)
    if normalizations:
        notices.append(
            "体系名・空白・全半角・接頭ラベル・版注記などの表記を統一して照合しました。"
            + " / ".join(
                f"{item['input_kind']} {item['input_code']} → {item['kind']} {item['code']}" for item in normalizations
            )
        )
    if not items:
        notices.append("LLMからの分類候補は0件でした。辞書からの探索仮説を表示します。")
    if rows:
        notices.append(
            "LLMによる分類名は採用せず、収録済みの公式名称を表示します。主題への適合性は定義を確認してください。"
        )
    return rows


def candidate_list(
    ws,
    text,
    use_llm=False,
    *,
    notices=None,
    proposal=None,
    connection=None,
    research_context=None,
    complete=default_complete,
):
    import classification_retrieval as retrieval
    import classification_planner as planner

    notices = notices if notices is not None else []
    connection = connection if connection is not None else ws.settings
    concepts = (research_context or {}).get("concepts", [])
    if concepts:
        elements = [
            dict(
                id=c["id"],
                name=c["name"],
                terms=c["terms"] + c.get("abstract_terms", []),
                relation=c.get("reason", "調査観点"),
            )
            for c in concepts
            if c.get("role") != "exclude"
        ]
    elif use_llm:
        decomposition = planner.decompose(text, connection, complete, research_context)
        elements = decomposition["elements"]
        notices.extend(decomposition["questions"])
    else:
        elements = retrieval.input_elements(text)
        notices.append(
            "LLM未使用: 入力語句で公式分類表を検索しました。自動の意味分解・定義の適合判定は行っていません。"
        )
    retrieved = retrieval.retrieve_elements(elements, limit=96 if use_llm else 48)
    rejected = []
    if use_llm:
        rows, rejected = planner.review(text, elements, retrieved, connection, complete, research_context)
        notices.append(
            f"技術を{len(elements)}要素に分解 → 公式辞書から{len(retrieved)}候補を取得 → 定義照合で{len(rows)}候補を提案しました。分類は未選択です。"
        )
    else:
        rows = retrieved
    missing = planner.unresolved(elements, rows)
    if missing:
        notices.append(
            "分類候補が見つからない要素: "
            + "、".join(e["name"] for e in missing)
            + "。要素の説明や同義語を補って再探索できます。"
        )
    for row in rows:
        row["recommendation_sources"] = [
            dict(
                type="llm" if use_llm else "element_dictionary",
                keywords=text,
                kind=row["kind"],
                code=row["code"],
                reason=row["reason"],
                source=row.get("source", ""),
            )
        ]
    result = [explorer.enrich(row) for row in rows]
    if proposal is not None:
        proposal.update(
            elements=elements,
            unresolved_elements=missing,
            pipeline="elements_dictionary_review",
            retrieved_count=len(retrieved),
            returned_count=len(rows) + len(rejected),
            accepted_count=len(rows),
            rejected=rejected,
            rejected_count=len(rejected),
            duplicate_count=0,
            items=copy.deepcopy(result) if use_llm else [],
            decomposition_method="llm" if use_llm else "input_phrases",
        )
    return result
