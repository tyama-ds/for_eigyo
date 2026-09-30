"""Annual chapters assembled from one measured landscape and existing reports.

This module adds no LLM prompt. Every generated chapter uses the ordinary
centroid or movement report generator with its existing evidence and validation.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date

from . import centroid_reports, landscape, landscape_reports, storage
from .field_exports import _csv
from .landscape_reports_api import _generation_failure
from .limits import MAX_ANALYSIS_YEARS

STORE_KIND = "annual_landscape_reports"
TERMINAL_STATUSES = {"generated", "not_requested", "not_generated", "partial", "failed", "cancelled"}
_WAITING = {"pending", "generating"}


class AnnualCancelled(Exception):
    """A cooperative stop between chapters, never an interrupted LLM answer."""


def validate_selection(result, topic_id, start_year, end_year):
    if (isinstance(start_year, bool) or isinstance(end_year, bool)
            or not isinstance(start_year, int) or not isinstance(end_year, int)
            or not 1500 <= start_year <= end_year <= date.today().year):
        raise ValueError("年次レポートの開始年・終了年を確認してください。")
    if end_year - start_year >= MAX_ANALYSIS_YEARS:
        raise ValueError(f"年次レポートは最大{MAX_ANALYSIS_YEARS}年の範囲を指定してください。")
    topic = next((item for item in result.get("topics", []) if str(item.get("id")) == topic_id), None)
    if (topic is None or topic_id == "all" or topic.get("is_outlier")
            or topic.get("status") == "unclassified"):
        raise ValueError("年次レポートを作る分類済みトピックを1つ選択してください。")
    return {"id": topic_id, "label": str(topic.get("label") or topic_id)}


def initial_report(options, result):
    topic = validate_selection(result, options["topic_id"], options["start_year"], options["end_year"])
    return {"id": storage.new_id(), "kind": "annual", "result_id": options["result_id"],
            "created_at": storage.now(), "projection": options["projection"],
            "projection_id": options.get("projection_id"), "interval": "year", "scope": options["scope"],
            "topic": topic, "meta": {}, "start_year": options["start_year"], "end_year": options["end_year"],
            "include_transitions": options.get("include_transitions", False), "provider": options["provider"],
            "generation_status": "preparing", "cancel_requested": False,
            "annual_rows": [], "years": [], "transitions": [],
            "overview": {"mode": "deterministic", "title": f"{topic['label']}：年次レポート", "text": "年別の計測値と根拠論文を確認しています。"},
            "limitations": [], "progress": {"completed": 0, "total": 0, "llm_calls": 0, "planned_llm_calls": 0}}


def _check_cancel(cancelled):
    if cancelled and cancelled():
        raise AnnualCancelled()


def _metric_row(year, center, period, scope):
    period_count = int((period or {}).get("count", 0))
    count = int((center or {}).get("count", 0))
    observed = bool(period_count)
    coverage_status = "observed" if count else "topic_zero" if observed else "no_corpus_observation"
    if not observed:
        note = "選択した分析範囲にこの年の論文がありません。取得状況が確認できず、分野全体の0件とは判断できません。"
    elif not count:
        note = "この年の分析対象論文はありますが、選択したトピックの論文は0件です。"
    else:
        note = "選択した分析範囲に含まれる論文の観測です。分野全体の網羅性は確認していません。"
    return {"year": year, "period_id": str(year), "count": count, "period_count": period_count,
            "share_of_period": (center.get("share_of_period", count / period_count) if center else 0.0) if observed else None,
            "terms": deepcopy((center or {}).get("terms", [])),
            "valid_vector_count": int((center or {}).get("valid_vector_count", 0)),
            "count_scope": (center or period or {}).get("count_scope", "full_corpus" if scope == "full" else "display_sample"),
            "observed": observed, "coverage_status": coverage_status, "coverage_note": note}


def _child_state(child, provider):
    child["requested_provider"] = provider
    if provider == "none":
        child["generation_status"] = "not_requested"
    elif not any(str(p.get("abstract") or "").strip() for p in child.get("evidence_papers", [])):
        child["generation_status"] = "skipped"
        child["generation_error_kind"], child["llm_error"] = _generation_failure(
            landscape_reports.NarrativeValidationError("", kind="missing_abstracts"))
    else:
        child["generation_status"] = "pending"
    return {key: child[key] for key in ("generation_status", "generation_error_kind", "llm_error") if key in child}


def _preparation_failure(row, exc):
    row["generation_status"] = "failed"
    row["generation_error_kind"], row["llm_error"] = _generation_failure(exc)


def update_progress(report):
    entries = [*report["years"], *report["transitions"]]
    report["progress"]["completed"] = sum(row["generation_status"] not in _WAITING for row in entries)
    planned = sum(
        row.get("report") is not None and row["generation_status"] in {"pending", "generating", "generated", "failed"}
        and row["report"].get("requested_provider") != "none" for row in entries)
    report["progress"]["planned_llm_calls"] = max(report["progress"]["planned_llm_calls"], planned)


def overview(report):
    rows = report["annual_rows"]
    first, last = rows[0], rows[-1]
    comparable = len(rows) > 1 and first["observed"] and last["observed"]
    terms_comparable = comparable and first["count"] > 0 and last["count"] > 0
    before = [str(item["term"]) for item in first["terms"] if item.get("term")]
    after = [str(item["term"]) for item in last["terms"] if item.get("term")]
    before_set, after_set = set(before), set(after)
    gaps = [row["year"] for row in rows if not row["observed"]]
    text = (f"{report['start_year']}年から{report['end_year']}年の「{report['topic']['label']}」を、"
            f"{'分析結果の全件' if report['scope'] == 'full' else '表示標本'}の同じ定義で年別に整理しました。"
            f"選択範囲内の当該トピックの論文は合計{sum(row['count'] for row in rows)}件です。")
    if comparable:
        text += (f"指定範囲の始点は{first['count']}件、終点は{last['count']}件で、"
                 f"各年の分析対象全体に占める割合は{first['share_of_period']:.1%}と{last['share_of_period']:.1%}です。")
    elif len(rows) == 1:
        text += "単一年のため年をまたぐ変化は比較していません。"
    else:
        text += "指定範囲の始点または終点に論文の観測がなく、両端の変化量を比較していません。"
    if gaps:
        text += "観測のない年：" + "、".join(map(str, gaps)) + "年。これらを分野全体の論文ゼロ年とは扱いません。"
    if terms_comparable:
        left = "、".join(term for term in before if term not in after_set) or "該当なし"
        entered = "、".join(term for term in after if term not in before_set) or "該当なし"
        text += f"保存済みの上位特徴語リストで始点だけにある語は「{left}」、終点だけにある語は「{entered}」です。技術の初出や消滅を意味しません。"
    text += "件数・割合・特徴語は取得集合内の記述であり、世界全体の人気、引用動態や因果関係を示すものではありません。"
    return {"mode": "deterministic", "title": f"{report['topic']['label']}：{first['year']}–{last['year']}年の概観",
            "text": text, "start_year": first["year"], "end_year": last["year"],
            "from_count": first["count"], "to_count": last["count"],
            "count_change": last["count"] - first["count"] if comparable else None,
            "from_share": first["share_of_period"], "to_share": last["share_of_period"],
            "share_change": last["share_of_period"] - first["share_of_period"] if comparable else None,
            "total_topic_papers": sum(row["count"] for row in rows), "endpoint_comparison_available": comparable,
            "term_comparison_available": terms_comparable,
            "terms_entered": [term for term in after if term not in before_set] if terms_comparable else [],
            "terms_left": [term for term in before if term not in after_set] if terms_comparable else [],
            "terms_shared": [term for term in after if term in before_set] if terms_comparable else [],
            "term_change_scope": "membership_of_saved_top_term_lists_not_first_occurrence_or_disappearance",
            "gap_years": gaps}


def prepare_report(result_id, projection, scope, topic_id, start_year, end_year, *, projection_id=None,
                   provider="none", include_transitions=False, report_id=None, on_prepare=None, cancelled=None):
    _check_cancel(cancelled)
    result = storage.read("results", result_id, include_papers=False)
    options = dict(result_id=result_id, projection=projection, scope=scope, topic_id=topic_id,
                   start_year=start_year, end_year=end_year, projection_id=projection_id,
                   provider=provider, include_transitions=include_transitions)
    report = initial_report(options, result)
    if report_id:
        report["id"] = report_id
    snapshot = landscape.build_landscape(result_id, projection=projection, interval="year", scope=scope)
    landscape_reports._report_landscape(result_id, projection, "year", scope, snapshot)
    _check_cancel(cancelled)
    if projection_id and snapshot["projection_id"] != projection_id:
        raise ValueError("座標・分析範囲の版が変わりました。年単位のマップを再取得してください。")
    periods = {int(row["id"]): row for row in snapshot.get("periods", [])}
    if not periods or start_year < min(periods) or end_year > max(periods):
        raise ValueError("開始年・終了年は、年単位のランドスケープに含まれる期間内を指定してください。")
    centers = {int(row["period_id"]): row for row in snapshot.get("centroids", []) if row["topic_id"] == topic_id}
    movements = sorted((row for row in snapshot.get("movements", []) if include_transitions
                        and row["topic_id"] == topic_id
                        and start_year <= int(row["from_period"]) < int(row["to_period"]) <= end_year),
                       key=lambda row: (int(row["from_period"]), int(row["to_period"]), row["id"]))
    report["projection_id"] = snapshot["projection_id"]
    report["meta"] = {**deepcopy(snapshot.get("meta", {})),
                      "is_demo": bool(result.get("meta", {}).get("is_demo") or result.get("is_demo")),
                      "available_start_year": min(periods), "available_end_year": max(periods),
                      "collection_coverage": "not_established_by_record_presence",
                      "overview_mode": "deterministic_no_additional_llm",
                      "llm_call_count_definition": "existing_generator_attempts_with_usable_abstracts"}
    limits = [line for line in landscape_reports.LIMITATIONS if scope == "sample" or "最大400" not in line]
    report["limitations"] = landscape_reports._unique_text([
        *limits, *snapshot.get("warnings", []), *snapshot.get("interpretation", {}).get("limitations", []),
        "年次の件数・割合は選択した分析範囲内の観測です。取得漏れ、表示標本、出版年不明の除外を含み、分野全体の人気・引用数の変化とは解釈できません。",
        "特徴語の変化は保存済み上位語リストへの出入りです。技術の初出、消滅、採用率を意味しません。",
        "観測のない年と、他トピックの論文はあるが当該トピックが0件の年を区別します。分野全体の0件を推定しません。",
        "全期間の概観は計測値の定型要約です。年別評論と任意の期間比較には既存の生成指示をそのまま使用し、全期間をまとめる追加LLM呼出しは行いません。",
        "中止は子レポートの間で反映されます。生成中の回答が完了するまで待つ場合があり、保存済みの計測値・評論は残ります。"])
    if report["meta"]["is_demo"]:
        report["limitations"].insert(0, "合成・テストデータを含む架空のデモです。実際の研究動向の判断には使えません。")
    report["annual_rows"] = [_metric_row(year, centers.get(year), periods.get(year), scope)
                             for year in range(start_year, end_year + 1)]
    report["overview"] = overview(report)
    report["progress"]["total"] = len(report["annual_rows"]) + len(movements)

    def publish():
        update_progress(report)
        if on_prepare:
            on_prepare(report)
        _check_cancel(cancelled)

    publish()
    for metric in report["annual_rows"]:
        _check_cancel(cancelled)
        year, center = metric["year"], centers.get(metric["year"])
        row = {**deepcopy(metric), "report": None, "generation_status": "skipped"}
        if center and metric["count"]:
            if not center.get("evidence_ids"):
                row.update(generation_error_kind="missing_evidence",
                           llm_error="内容表現で照合できる代表論文がないため、この年の評論は生成しません。計測件数は保持しています。")
            else:
                try:
                    child = centroid_reports.prepare_report(result_id, projection, "year", topic_id, str(year),
                        snapshot["projection_id"], scope, landscape_snapshot=snapshot)
                    row.update(report=child, **_child_state(child, provider))
                except Exception as exc:
                    _preparation_failure(row, exc)
        report["years"].append(row)
        publish()
    for movement in movements:
        _check_cancel(cancelled)
        row = {"from_period": movement["from_period"], "to_period": movement["to_period"],
               "movement_id": movement["id"], "gap_periods": movement.get("gap_periods", 0),
               "gap_note": movement.get("explanation", "") if movement.get("gap_periods") else "",
               "report": None, "generation_status": "pending"}
        try:
            child = landscape_reports.prepare_report(result_id, projection, "year", movement["id"],
                snapshot["projection_id"], scope, landscape_snapshot=snapshot)
            row.update(report=child, **_child_state(child, provider))
        except Exception as exc:
            _preparation_failure(row, exc)
        report["transitions"].append(row)
        publish()
    report["generation_status"] = "generating" if any(row["generation_status"] == "pending" for row in [*report["years"], *report["transitions"]]) else final_status(report)
    return report


def final_status(report):
    entries = [*report["years"], *report["transitions"]]
    issues = any(row["generation_status"] == "failed" or row.get("llm_error") for row in entries)
    failed = any(row["generation_status"] == "failed" for row in entries)
    generated = any(row["generation_status"] == "generated" for row in entries)
    if report["provider"] == "none":
        return "partial" if failed else "not_requested"
    if generated:
        return "partial" if issues else "generated"
    return "failed" if failed else "not_generated"


def cancel_remaining(report):
    for row in [*report["years"], *report["transitions"]]:
        if row["generation_status"] in _WAITING:
            row["generation_status"] = "cancelled"
            if row.get("report"):
                row["report"]["generation_status"] = "cancelled"
    report["cancel_requested"] = True
    report["generation_status"] = "cancelled"
    update_progress(report)


def generate_children(report, *, model=None, cancelled=None, on_save=None, on_progress=None):
    """Sequentially reuse the existing generator; never lose successful siblings."""
    for kind, entries in (("year", report["years"]), ("transition", report["transitions"])):
        for row in entries:
            _check_cancel(cancelled)
            if row["generation_status"] != "pending":
                continue
            child = row["report"]
            row["generation_status"] = child["generation_status"] = "generating"
            report["generation_status"] = "generating"
            report["progress"]["llm_calls"] += 1
            label = f"{row['year']}年" if kind == "year" else f"{row['from_period']}→{row['to_period']}年"
            report["progress"]["active"] = {"kind": kind, "label": label}
            if on_save:
                on_save(report)
            def progress(event):
                if on_progress:
                    on_progress(label, event)
            try:
                child["narrative"] = landscape_reports.generate(child, report["provider"], model, progress=progress)
                row["generation_status"] = child["generation_status"] = "generated"
            except Exception as exc:
                row["generation_status"] = child["generation_status"] = "failed"
                kind_code, error = _generation_failure(exc)
                for value in (row, child):
                    value.update(generation_error_kind=kind_code, llm_error=error)
                child["narrative"]["validation"] = {"status": "warning", "warnings": [{"code": "llm_failed", "message": error}]}
            update_progress(report)
            report["progress"].pop("active", None)
            if on_save:
                on_save(report)
    _check_cancel(cancelled)
    report["generation_status"] = final_status(report)
    update_progress(report)
    return report


def export_csv(report):
    """Readable annual rows plus typed detail leaves for every chapter."""
    context = [report["id"], report["result_id"], report.get("projection_id") or "", report["scope"], report["topic"]["id"]]
    rows = []
    def add(section, period, key, value):
        kind = "null" if value is None else "bool" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "text"
        rows.append(context + [section, period, key, "" if value is None else value, kind])
    for row in report.get("annual_rows", []):
        for key in ("count", "period_count", "share_of_period", "valid_vector_count", "coverage_status", "coverage_note", "count_scope"):
            add("annual_metrics", row["year"], key, row.get(key))
    def flatten(value, path=""):
        if isinstance(value, dict):
            if not value:
                add("detail", "", path, "{}")
            for key, item in value.items():
                flatten(item, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            if not value:
                add("detail", "", path, "[]")
            for index, item in enumerate(value):
                flatten(item, path + "/" + str(index))
        else:
            add("detail", "", path, value)
    flatten(report)
    return _csv(["Annual report ID", "Analysis ID", "Projection ID", "Scope", "Topic ID",
                 "Section", "Year", "Metric / JSON Pointer", "Value", "Value type"], rows)
