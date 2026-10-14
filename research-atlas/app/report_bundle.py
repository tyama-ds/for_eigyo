"""Read saved reports into a shared, portable document model. No LLM calls."""
from __future__ import annotations

import json


TITLES = {"corpus": "全件抄録レポート", "landscape": "重心・話題変化レポート",
          "annual": "年次推移レポート", "field": "分野・隣接領域レポート",
          "foresight": "有望領域の探索・推薦レポート", "result": "研究動向の分析レポート"}
SCOPE_NOTE = ("保存済みの分析・評論を出力しています。出力時にLLM生成や座標の再計算は行いません。"
              "件数は取得した文献集合内の観測です。抄録に基づく解釈と将来シナリオは原典と合わせて確認してください。"
              "図は静止画です。時相の高さは期間を表し、重心の矢印は論文構成の変化であって因果関係を示しません。")


def text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def _warnings(value):
    if not isinstance(value, dict):
        return []
    warnings = [*value.get("warnings", []), *value.get("validation", {}).get("warnings", [])]
    output = []
    for row in warnings:
        if isinstance(row, dict):
            message = str(row.get("message") or row.get("code") or text(row))
            unmatched = [*row.get("unmatched_numbers", []), *row.get("unmatched_quantities", [])]
            if unmatched:
                message += " / 未照合: " + text(unmatched)
            output.append(message)
        else:
            output.append(str(row))
    if value.get("validation", {}).get("status") == "warning" and not output:
        output.append("照合に注意が必要です。原文の数値・単位・引用を確認してください。")
    return list(dict.fromkeys(output))


def _section(bundle, title, content):
    if content is not None and str(content).strip():
        bundle["sections"].append({"title": str(title), "text": text(content)})


def _references(ids, count=None):
    total = max(len(ids), int(count or 0))
    if not total:
        return ""
    shown = ", ".join(map(str, ids[:12]))
    return "根拠ID: " + shown + (f"（全{total:,}件。残りのIDはExcel全指標・CSV/JSONを参照）" if total > 12 else "")


def _narrative(bundle, value, label):
    if not value:
        _section(bundle, label, "この評論はまだ生成されていません。保存済みの計測値を掲載しています。")
        return
    if isinstance(value, str):
        _section(bundle, label, value)
        return
    notices = _warnings(value)
    bundle["warnings"].extend(label + "：" + note for note in notices)
    intro = value.get("headline") or value.get("text")
    if intro:
        _section(bundle, label, intro)
    if value.get("headline") and value.get("text"):
        _section(bundle, label + " / 概要", value["text"])
    mode = value.get("mode")
    if mode:
        _section(bundle, label + " / 生成情報", "生成方式: " + text(mode) + " / モデル: " + text(value.get("model")))
    for key, title in (("findings", "観測"), ("hypotheses", "仮説")):
        for item in value.get(key, []):
            _section(bundle, label + " / " + title, item)
    if value.get("evidence_ids"):
        _section(bundle, label + " / 根拠", _references(value["evidence_ids"]))
    for section in value.get("sections", []):
        title = label + " / " + str(section.get("title") or "評論")
        notes = _warnings(section)
        prefix = "[!] " + "\n[!] ".join(notes) + "\n\n" if notes else ""
        ids = section.get("paper_ids", section.get("evidence_ids", []))
        sources = "\n" + _references(ids, section.get("paper_ids_count")) if ids else ""
        _section(bundle, ("[!] " if notes or notices else "") + title, prefix + text(section.get("text")) + sources)
        bundle["warnings"].extend(title + "：" + note for note in notes)
        if section.get("unverified_evidence_ids"):
            bundle["warnings"].append(title + "：未確認の根拠ID " + text(section["unverified_evidence_ids"]))
    for note in value.get("caveats", []):
        _section(bundle, label + " / 解釈の限界", note)


def _table(bundle, title, columns, rows):
    rows = list(rows)
    if rows:
        bundle["tables"].append({"title": title, "columns": columns, "rows": rows})


def _records_table(bundle, title, rows, fields):
    _table(bundle, title, [label for _, label in fields],
           ([text(row.get(key)) if isinstance(row.get(key), (dict, list)) else row.get(key)
             for key, _ in fields] for row in rows))


def _provenance(bundle, report, label="分析条件"):
    fields = ("scope", "projection", "projection_id", "interval", "generation_status", "synthesis_status", "status",
              "requested_provider", "provider", "model", "prompt_version")
    values = [f"{key}: {text(report[key])}" for key in fields if report.get(key) is not None]
    selection = report.get("selection") or {}
    if selection:
        values.append("根拠選択: " + str(selection.get("method_label", selection.get("selection_method", "重心近傍")))
                      + " / 各期間の上限 " + text(selection.get("papers_per_period", 6)) + "件"
                      + (" / 抄録ありに限定" if selection.get("abstract_only") else ""))
    summary = report.get("input_summary") or {}
    if summary.get("selection"):
        values.append(str(summary["selection"]))
    _section(bundle, label, "\n".join(values))
    if report.get("llm_error"):
        bundle["warnings"].append(label + "：" + report["llm_error"])
    if report.get("generation_status") in {"failed", "partial", "cancelled", "preparing", "generating", "pending", "skipped"}:
        bundle["warnings"].append(label + "：未完了または一部結果です。生成状態 " + str(report["generation_status"]))


def _landscape(bundle, report, label="重心分析"):
    _provenance(bundle, report, label + " / 条件")
    _narrative(bundle, report.get("narrative"), label)
    for item in report.get("observations", []):
        _section(bundle, label + " / " + str(item.get("title", "計測からの観測")), item.get("text"))
    metric = report.get("movement") or report.get("centroid") or {}
    labels = {"from_period": "前期", "to_period": "後期", "period_id": "期間", "count": "論文件数",
              "from_count": "前期件数", "to_count": "後期件数", "period_count": "当該期間の対象数",
              "share_of_period": "期間内構成比", "distance_2d": "投影平面での距離", "cosine_distance": "内容表現のコサイン距離",
              "p_value": "p値", "q_value": "多重検定補正q値", "status": "判定", "count_scope": "集計範囲"}
    _table(bundle, label + " / 計測値", ["指標", "値"],
           ([title, metric[key]] for key, title in labels.items() if key in metric))
    _evidence(bundle, report.get("evidence_papers", []), label)
    bundle["warnings"].extend(map(str, report.get("limitations", [])))


def _evidence(bundle, papers, label):
    _records_table(bundle, label + " / 根拠論文", papers,
                   [("id", "論文ID"), ("title", "論文名"), ("year", "出版年"), ("period", "期間")])
    # Saved excerpts are included, not silently upgraded to full text.
    for paper in papers:
        abstract = paper.get("abstract")
        if abstract:
            _section(bundle, label + " / 保存された抄録・抜粋: " + str(paper.get("title") or paper.get("id")), abstract)
            if paper.get("abstract_truncated") or paper.get("abstract_omitted"):
                _section(bundle, "抜粋について", "保存時に原文の一部を省略しています。完全な原文は元の書誌データを確認してください。")


def _audit(value, path=""):
    """Lazy typed leaves; avoid copies of very large saved comparison tables."""
    if isinstance(value, dict):
        if not value:
            yield [path, "object", "{}"]
        for key, item in value.items():
            if key.startswith("_retrieval"):
                continue
            yield from _audit(item, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        if not value:
            yield [path, "array", "[]"]
        for index, item in enumerate(value):
            yield from _audit(item, path + "/" + str(index))
    else:
        yield [path, "null" if value is None else "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "text", value]


def build_bundle(kind, report, result, *, candidate_id=None):
    if kind not in TITLES:
        raise ValueError("レポートの種類を確認してください。")
    bundle = {"title": TITLES[kind], "subtitle": result.get("dataset_name", report.get("name", "")),
              "report_id": report["id"], "result_id": result["id"], "created_at": report.get("created_at", ""),
              "sections": [], "tables": [], "figures": [], "warnings": _warnings(report), "scope_note": SCOPE_NOTE}
    if report.get("is_demo") or result.get("is_demo") or result.get("meta", {}).get("is_demo"):
        bundle["warnings"].append("合成・テストデータを含む架空のデモです。実際の研究動向を示すものではありません。")
    if kind == "corpus":
        from .corpus_exports import SCOPE_NOTE as CORPUS_NOTE
        bundle["scope_note"] += CORPUS_NOTE
        counts = report.get("counts", {})
        fields = [("total", "対象"), ("completed", "抽出済み"), ("missing", "抄録なし"), ("failed", "失敗"), ("pending", "未処理")]
        _records_table(bundle, "全件処理の状況", [counts], fields)
        if report.get("partial") or any(counts.get(key) for key in ("missing", "failed", "pending")):
            bundle["warnings"].append("一部の抄録が未処理・失敗・欠測です。評論は抽出できた範囲に基づきます。")
        _section(bundle, "処理範囲", f"処理文字数: {counts.get('processed_chars', 0):,} / {counts.get('total_chars', 0):,}。処理文字数は評論に採用された文字数とは異なります。")
        _provenance(bundle, report)
        for key, title, first in (("annual", "年別集計", ("year", "年")), ("topics", "分野別集計", ("label", "分野"))):
            rows = [{**row, "count": row.get("count", row.get("total"))} for row in report.get(key, [])]
            _records_table(bundle, title, rows, [first, ("count", "対象"), *fields[1:]])
        _records_table(bundle, "抽出された研究手法", report.get("methods", []), [("method", "手法"), ("paper_count", "言及論文件数")])
        _narrative(bundle, report.get("narrative"), "総合評論")
        for group in report.get("groups", []):
            label = str(group.get("label") or group.get("key") or "年・分野別評論")
            if group.get("status") != "completed":
                bundle["warnings"].append(label + "：一部結果または更新待ちです。保存済みの評論は前の処理時点の内容を含む場合があります。")
            _narrative(bundle, group.get("summary", group.get("narrative")), label)
    elif kind == "landscape":
        _landscape(bundle, report)
    elif kind == "annual":
        _provenance(bundle, report)
        _section(bundle, report.get("overview", {}).get("title", "年次概況"), report.get("overview", {}).get("text"))
        _records_table(bundle, "年別の計測", report.get("annual_rows", []), [("year", "年"), ("count", "対象分野件数"),
            ("period_count", "期間の論文総数"), ("share_of_period", "構成比"), ("coverage_status", "観測状況")])
        for row in report.get("years", []):
            label = str(row.get("year", "年不明")) + "年"
            if row.get("report"):
                _landscape(bundle, row["report"], label)
            else:
                _section(bundle, label, row.get("llm_error") or row.get("coverage_note") or "この年の評論は保存されていません。")
        for row in report.get("transitions", []):
            label = text(row.get("from_period")) + " → " + text(row.get("to_period"))
            if row.get("report"):
                _landscape(bundle, row["report"], label)
            else:
                _section(bundle, label, row.get("llm_error") or "この期間比較の評論は保存されていません。")
        bundle["warnings"].extend(map(str, report.get("limitations", [])))
    elif kind == "field":
        _provenance(bundle, report)
        _section(bundle, "比較対象", text(report.get("focus", {}).get("label")) + " / " + text((report.get("neighbor") or {}).get("label")))
        _narrative(bundle, report.get("narrative"), "分野・隣接領域の評論")
        for row in report.get("observations", []):
            _section(bundle, row.get("title", "計測からの観測"), row.get("text"))
        _records_table(bundle, "年別の論文数", report.get("annual", []), [("year", "年"), ("focus_count", "対象分野"), ("neighbor_count", "隣接分野")])
        for key, title in (("institutions", "所属機関の比較"), ("methods", "研究手法の比較")):
            _records_table(bundle, title, report.get(key, {}).get("rows", []), [("name", "名称"), ("focus_count", "対象分野"), ("neighbor_count", "隣接分野")])
        _evidence(bundle, report.get("evidence_papers", []), "分野の分析")
        bundle["warnings"].extend(map(str, report.get("limitations", [])))
        bundle["warnings"].extend(map(str, report.get("connections", {}).get("basis_notes", [])))
    elif kind == "foresight":
        candidates = [row for row in report.get("candidates", []) if not candidate_id or row.get("id") == candidate_id]
        if candidate_id and not candidates:
            raise KeyError(candidate_id)
        _section(bundle, "評価の読み方", "研究の伸びと実用化に関する根拠の記載状況を分けて評価します。根拠言及充足度は成熟度・成功確率ではありません。")
        _table(bundle, "候補の比較", ["候補", "取得集合内の増減率 (%)", "根拠言及充足度 (%)", "実用化段階"],
               ([row.get("label"), row.get("growth", {}).get("growth_pct"), row.get("readiness", {}).get("evidence_coverage_score"), row.get("readiness", {}).get("stage_label")] for row in candidates))
        for row in candidates:
            label = row.get("label", row["id"])
            _section(bundle, label + " / 研究動向", row.get("growth", {}).get("reason"))
            _narrative(bundle, row.get("narrative") or {"sections": [{"title": key, "text": text(value)} for key, value in row.get("commentary", {}).items()]}, label)
            if row.get("llm_error"):
                bundle["warnings"].append(label + "：" + row["llm_error"])
            for fact in row.get("content_facts") or row.get("evidence", []):
                notes = _warnings(fact)
                if fact.get("verification") == "quote_checked_numeric_warning" and not notes:
                    notes = ["数値・単位の照合に注意が必要です。"]
                content = "\n".join([*("[!] " + notice for notice in notes), text(fact.get("statement")), "原文引用: " + text(fact.get("quote", fact.get("text"))), "論文ID: " + text(fact.get("paper_id")),
                    "検証状態: " + text(fact.get("verification", "未検証")), "論旨: " + text(fact.get("stance", "unknown")), "帰属: " + text(fact.get("attribution", "unknown"))])
                _section(bundle, ("[!] " if notes else "") + label + " / 根拠", content)
                bundle["warnings"].extend(label + "：" + notice for notice in notes)
            _records_table(bundle, label + " / 年別件数", row.get("growth", {}).get("series", []), [("year", "年"), ("count", "件数")])
            _records_table(bundle, label + " / 実用化の根拠", row.get("readiness", {}).get("dimensions", []), [("label", "項目"), ("status", "状態"), ("paper_count", "言及論文件数")])
            for note in row.get("readiness", {}).get("notes", []):
                _section(bundle, label + " / 根拠の限界", note)
    else:
        _section(bundle, "分析条件", text(result.get("meta", {})))
        _records_table(bundle, "年別論文数", result.get("timeline", []), [("year", "年"), ("papers", "件数")])
        _records_table(bundle, "分野ごとの指標", result.get("topics", []), [("label", "分野"), ("count", "論文数"), ("growth_pct", "増減率 (%)"), ("citation_total", "既知の被引用数"), ("score", "探索スコア")])
        for row in result.get("topics", []):
            _section(bundle, row.get("label", row.get("id", "分野")), "代表語: " + "、".join(map(str, row.get("keywords", []))))
        _narrative(bundle, result.get("narrative") or result.get("insights"), "保存済みの解釈")
        bundle["warnings"].extend(map(str, result.get("meta", {}).get("warnings", [])))
    audit = {key: value for key, value in report.items() if key not in {"papers", "_large_store", "document_embeddings", "embedding_vectors"}}
    if candidate_id and kind == "foresight":
        audit["candidates"] = candidates
        audit["rounds"] = [row for row in report.get("rounds", []) if row.get("candidate_id") == candidate_id]
        audit["feedback"] = [row for row in report.get("feedback", []) if row.get("candidate_id") == candidate_id]
    bundle["tables"].append({"title": "保存データの全指標", "columns": ["JSON Pointer", "型", "値"], "rows": _audit(audit), "xlsx_only": True})
    bundle["warnings"] = list(dict.fromkeys(filter(None, bundle["warnings"])))
    return bundle
