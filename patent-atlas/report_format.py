"""検索式を、特許庁「特許出願技術動向調査」報告書の資料編にある検索式表の形に整える。

報告書の資料編は、調査条件（データベース・検索日・対象期間・対象国/地域・集計単位）の表と、
集合番号つきで段階的に組み立てた検索式の表（No. / 条件 / 検索式 / ヒット件数）で構成される。
ここでは保存済み検索式の論理木を上位の AND / NOT 単位の集合に分解し、選んだ検索サービスの
構文で各集合を表し、集合番号で結合した行と最終式を添える。ヒット件数は検索実行後に記入する欄で、
取り込み済み CSV での該当数は参考値として別列に出す。

期間や対象国の条件は検索サービスごとに入力欄が異なるため、論理式へは埋め込まず調査条件の行として
示す。各サービスの日付フィールドの構文は照合していないので、ここでは生成しない。
"""

from __future__ import annotations

import csv
import io
import re
import time

from query_formats import PROFILES, _render, build_tree, export_query, walk_tree
from query_scoring import evaluate, row_codes

BASES = {"priority": "優先権主張年（ファミリー最先）", "application": "出願年", "publication": "公開年"}
UNITS = {"family": "パテントファミリー単位", "publication": "公報単位"}
REGION_LABELS = {
    "JP": "日本",
    "US": "米国",
    "EP": "欧州（EPO）",
    "CN": "中国",
    "KR": "韓国",
    "WO": "PCT 出願",
    "TW": "台湾",
    "DE": "ドイツ",
}


def _component_label(node, facets):
    text_terms = [n.value for n in walk_tree(node) if n.op == "text"]
    classes = [n for n in walk_tree(node) if n.op == "class"]
    if classes and not text_terms:
        systems = sorted({n.system for n in classes})
        return "分類（" + " / ".join(systems) + "）"
    if text_terms and not classes:
        names = []
        for facet in facets or []:
            terms = [t for t in facet.get("terms", []) if isinstance(t, str)]
            if terms and all(t in text_terms for t in terms) and facet.get("name"):
                names.append(str(facet["name"]))
        if names:
            return "観点: " + " / ".join(dict.fromkeys(names))
        return "キーワード" if len(text_terms) == 1 else "キーワード群（OR）"
    return "複合条件"


def _components(tree):
    """Split the tree into positive AND components and an optional NOT component."""
    negative = None
    if tree.op == "not":
        tree, negative = tree.children
    positives = list(tree.children) if tree.op == "and" else [tree]
    return positives, negative


def _local_hits(node, rows):
    if rows is None:
        return None
    hits = 0
    for row in rows:
        if evaluate(node, row, row_codes(row)):
            hits += 1
    return hits


def _checked_options(options):
    options = dict(options or {})
    basis = options.get("basis", "priority")
    if basis not in BASES:
        raise ValueError("期間の基準は優先権主張年・出願年・公開年から選んでください。")
    unit = options.get("unit", "family")
    if unit not in UNITS:
        raise ValueError("集計単位はファミリー単位か公報単位を選んでください。")
    years = []
    for key in ("period_from", "period_to"):
        value = options.get(key)
        if value in (None, ""):
            years.append(None)
            continue
        if not isinstance(value, (int, str)) or not re.fullmatch(r"\d{4}", str(value)):
            raise ValueError("対象期間は西暦4桁で指定してください。")
        years.append(int(value))
    if years[0] and years[1] and years[0] > years[1]:
        raise ValueError("対象期間の開始年は終了年以前にしてください。")
    regions = options.get("regions", [])
    if isinstance(regions, str):
        regions = [r.strip() for r in re.split(r"[,、\s]+", regions) if r.strip()]
    if not isinstance(regions, list) or any(not isinstance(r, str) or len(r) > 40 for r in regions):
        raise ValueError("対象国・地域はコードまたは名称のリストで指定してください。")
    search_date = options.get("search_date") or time.strftime("%Y-%m-%d")
    if not isinstance(search_date, str) or len(search_date) > 40:
        raise ValueError("検索日の形式を確認してください。")
    database = options.get("database", "")
    if not isinstance(database, str) or len(database) > 200:
        raise ValueError("データベース名の形式を確認してください。")
    return dict(
        basis=basis,
        unit=unit,
        period_from=years[0],
        period_to=years[1],
        regions=regions,
        search_date=search_date.strip(),
        database=database.strip(),
    )


def _conditions(options, profile, syntax):
    period = (
        f"{options['period_from'] or '—'}年〜{options['period_to'] or '—'}年"
        if options["period_from"] or options["period_to"]
        else "（記入）"
    )
    regions = [REGION_LABELS.get(r.upper(), r) for r in options["regions"]] or ["（記入）"]
    database = options["database"] or (
        "Derwent Innovation（DWPI 付きコレクション）"
        if syntax == "derwent_innovation"
        else profile["name"]
    )
    return [
        dict(label="使用データベース", value=database),
        dict(label="検索式の構文", value=f"{profile['name']}（{profile['interface']}）"),
        dict(label="検索日", value=options["search_date"]),
        dict(label="対象期間", value=f"{BASES[options['basis']]} {period}"),
        dict(label="対象国・地域", value="、".join(regions)),
        dict(
            label="集計単位",
            value=UNITS[options["unit"]]
            + ("（DWPI ファミリー）" if options["unit"] == "family" and syntax.startswith("derwent_") else ""),
        ),
        dict(label="対象文献", value="特許（PCT 出願を含む）。実用新案は含めない"),
    ]


def build_report(query, syntax="derwent_innovation", *, options=None, rows=None, replacements=None,
                 omit_unsupported=False, allow_unverified=False, descendants=False, language="auto"):
    """Return the report tables for one saved query.

    The final expression and its problems/warnings come from export_query so the
    report never passes a query the single-expression output would hold back.
    """
    if syntax not in PROFILES:
        raise ValueError("出力する検索サービスを選んでください。")
    options = _checked_options(options)
    profile = PROFILES[syntax]
    exported = export_query(
        query,
        syntax,
        replacements=replacements,
        omit_unsupported=omit_unsupported,
        allow_unverified=allow_unverified,
        descendants=descendants,
        language=language,
    )
    tree = build_tree(query, replacements, {c["kind"] for c in exported.get("omitted_classifications", [])})
    emitted = [(replacements or {}).get(n.value, n.value) for n in walk_tree(tree) if n.op == "text"]
    has_ja = any(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", t) for t in emitted)
    actual_language = ("ja" if has_ja else "en") if language == "auto" else language
    facets = query.get("facets") if isinstance(query.get("facets"), list) else []
    positives, negative = _components(tree)
    steps = []
    for node in positives:
        steps.append(
            dict(
                no=len(steps) + 1,
                label=_component_label(node, facets),
                expression=_render(node, syntax, actual_language, descendants),
                local_hits=_local_hits(node, rows),
                kind="positive",
            )
        )
    positive_refs = [f"#{s['no']}" for s in steps]
    combination = " AND ".join(positive_refs) if len(positive_refs) > 1 else positive_refs[0]
    if negative is not None:
        steps.append(
            dict(
                no=len(steps) + 1,
                label="除外条件（NOT）",
                expression=_render(negative, syntax, actual_language, descendants),
                local_hits=_local_hits(negative, rows),
                kind="negative",
            )
        )
        combination = f"({combination}) NOT #{steps[-1]['no']}" if len(positive_refs) > 1 else f"{combination} NOT #{steps[-1]['no']}"
    steps.append(
        dict(
            no=len(steps) + 1,
            label="母集団（集合の結合）",
            expression=combination,
            local_hits=_local_hits(tree, rows),
            kind="combination",
        )
    )
    notes = [
        "ヒット件数は検索サービスで実行した結果を記入する欄です。"
        + ("取込CSV該当数は取り込み済みの判定対象に対する参考値で、検索母集団の件数ではありません。" if rows is not None else ""),
        "対象期間・対象国・地域は各検索サービスの日付・国指定欄で設定してください。日付フィールドの構文は論理式に含めていません。",
        "集合番号（#1 など）は報告書での表記です。検索サービスに貼り付けるときは「最終式」を使ってください。",
    ]
    if syntax.startswith("derwent_"):
        notes.append("件数をファミリー単位で数える場合は DWPI ファミリーの集計方法を報告書に明記してください。")
    result = dict(
        syntax=syntax,
        name=profile["name"],
        query_id=query.get("id"),
        query_version=query.get("version"),
        conditions=_conditions(options, profile, syntax),
        steps=steps,
        final_expression=exported["expression"] if exported.get("can_copy") else None,
        can_copy=bool(exported.get("can_copy")),
        problems=exported.get("problems", []),
        warnings=exported.get("warnings", []),
        notes=notes,
        local_rows=len(rows) if rows is not None else None,
    )
    result["markdown"] = to_markdown(result)
    result["tsv"] = to_tsv(result)
    return result


def _cell(value):
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def to_markdown(report):
    show_local = report.get("local_rows") is not None
    lines = ["### 調査条件", "", "| 項目 | 内容 |", "|---|---|"]
    lines += [f"| {_cell(c['label'])} | {_cell(c['value'])} |" for c in report["conditions"]]
    lines += ["", "### 検索式", ""]
    head = "| No. | 条件 | 検索式 | ヒット件数 |" + (" 取込CSV該当数（参考） |" if show_local else "")
    lines += [head, "|---|---|---|---|" + ("---|" if show_local else "")]
    for step in report["steps"]:
        row = f"| {step['no']} | {_cell(step['label'])} | `{_cell(step['expression'])}` | （記入） |"
        if show_local:
            row += f" {_cell(step['local_hits'])} |"
        lines.append(row)
    if report.get("final_expression"):
        lines += ["", "最終式（検索サービスへ貼り付け）:", "", "```", report["final_expression"], "```"]
    if report.get("problems"):
        lines += ["", "出力を保留している事項:"] + [f"- {p}" for p in report["problems"]]
    lines += ["", "注記:"] + [f"- {n}" for n in report["notes"] + report.get("warnings", [])]
    return "\n".join(lines) + "\n"


def to_tsv(report):
    show_local = report.get("local_rows") is not None
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(["項目", "内容"])
    for c in report["conditions"]:
        writer.writerow([c["label"], c["value"]])
    writer.writerow([])
    writer.writerow(["No.", "条件", "検索式", "ヒット件数"] + (["取込CSV該当数（参考）"] if show_local else []))
    for step in report["steps"]:
        writer.writerow(
            [step["no"], step["label"], step["expression"], ""] + ([step["local_hits"]] if show_local else [])
        )
    return stream.getvalue()
