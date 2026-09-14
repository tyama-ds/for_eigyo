"""CSV exports for report prose, comparison measurements, and every source paper."""
from __future__ import annotations

import csv
import io
import json

from .reports import safe_cell


def _csv(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([safe_cell(v) if isinstance(v, str) else v for v in row])
    return "\ufeff" + stream.getvalue()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def report_csv(report: dict) -> str:
    narrative = report.get("narrative", {})
    context = [report["id"], report["result_id"], report["focus"]["label"],
               (report.get("neighbor") or {}).get("label", ""), narrative.get("mode", "deterministic"),
               narrative.get("model") or ""]
    rows = [context + ["見出し", narrative.get("headline", ""), "", _json(report.get("scope", {}))]]
    rows.extend(context + [s.get("title", ""), s.get("text", ""), _json(s.get("evidence_ids", [])), ""]
                for s in narrative.get("sections", []))
    rows.extend(context + ["数値からの観測: " + s.get("title", s.get("section", "")), s.get("text", ""),
                           _json(s.get("evidence_ids", [])), ""] for s in report.get("observations", []))
    rows.extend(context + ["解釈上の限界", text, "", ""]
                for text in dict.fromkeys([*narrative.get("caveats", []), *report.get("limitations", [])]))
    if report.get("llm_error"):
        rows.append(context + ["LLM生成エラー", report["llm_error"], "", "定型レポートを保存"])
    return _csv(["Report ID", "Analysis ID", "Focus field", "Neighbor field", "Report mode", "LLM model",
                 "Section", "Text", "Evidence paper IDs (JSON)", "Scope / note"], rows)


def data_csv(report: dict) -> str:
    """A tidy table plus lossless measurement leaves for reproducible downstream work.

    Annual/forecast rows are immediately pivotable. Detail rows retain every saved
    indicator and its type so no nested author/reference/coverage evidence is lost.
    """
    rows = []
    context = [report["id"], report["result_id"], report["focus"]["id"],
               (report.get("neighbor") or {}).get("id", "")]
    def add(section, item, year, metric, value, unit="", evidence=None):
        kind = "null" if value is None else "bool" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "text"
        rows.append(context + [section, item, year, metric, value, kind, unit,
                               _json(evidence or [])])
    for item in report.get("annual", []):
        add("annual", "focus", item["year"], "publication_count", item["focus_count"], "papers")
        add("annual", "neighbor", item["year"], "publication_count", item.get("neighbor_count"), "papers")
    for side in ("focus", "neighbor"):
        for point in (report.get(side) or {}).get("forecast", []):
            for key in ("value", "lower", "upper"):
                add("forecast", side, point["year"], key, point.get(key), "papers / exploratory scenario")
    def flatten(value, path=""):
        if isinstance(value, dict):
            if not value:
                add("detail", path, "", "empty_object", "{}", "json")
            for key, item in value.items():
                flatten(item, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            if not value:
                add("detail", path, "", "empty_array", "[]", "json")
            for index, item in enumerate(value):
                flatten(item, path + "/" + str(index))
        else:
            add("detail", path, "", "value", value)
    # JSON Pointer paths preserve all underlying rows, metadata, missingness and
    # explicit totals. Prose and paper text have their own dedicated exports.
    flatten({key: value for key, value in report.items()
             if key not in {"narrative", "evidence_papers", "observations", "llm_error"}})
    return _csv(["Report ID", "Analysis ID", "Focus topic ID", "Neighbor topic ID", "Section",
                 "Item / JSON Pointer", "Year", "Metric", "Value", "Value type", "Unit",
                 "Evidence paper IDs (JSON)"], rows)


def papers_csv(report: dict, result: dict) -> str:
    selected = {report["focus"]["id"]}
    if report.get("neighbor"):
        selected.add(report["neighbor"]["id"])
    labels = {t["id"]: t["label"] for t in result["topics"]}
    primary_fields = {"id", "topic_id", "title", "year", "publication_date", "abstract", "authors",
                      "affiliations", "keywords", "doi", "citations", "citation_source", "references",
                      "references_status", "topic_weights", "providers", "external_url"}
    rows = []
    for p in result["papers"]:
        if p.get("topic_id") not in selected:
            continue
        rows.append([report["id"], result["id"], p["id"], p.get("topic_id"), labels.get(p.get("topic_id")),
                     p["title"], p["year"], p.get("publication_date", ""), p.get("abstract", ""),
                     _json(p.get("authors", [])), _json(p.get("affiliations", [])),
                     _json(p.get("keywords", [])), p.get("doi", ""), p.get("citations"),
                     p.get("citation_source", ""), _json(p.get("references", [])),
                     p.get("references_status", "not_provided"), _json(p.get("topic_weights", {})),
                     _json(p.get("providers", [])), p.get("external_url", ""),
                     p.get("date_precision", ""), p.get("date_source", ""), p.get("source", ""),
                     p.get("retrieved_at", ""), _json(p.get("citation_history")),
                     _json(p.get("citation_snapshots")), _json(p.get("citation_history_snapshots")),
                     _json(p.get("aliases")), p.get("provenance", ""), _json(p.get("provenances")),
                     _json(p.get("is_outlier")),
                     _json({key: value for key, value in p.items() if key not in primary_fields})])
    return _csv(["Report ID", "Analysis ID", "Paper ID", "Topic ID", "Topic", "Title", "Year",
                 "Publication date", "Abstract", "Authors (JSON)", "Affiliations (JSON)", "Keywords (JSON)",
                 "DOI", "Cumulative citations", "Citation source", "References (JSON)", "References status",
                 "Topic weights (JSON)", "Providers (JSON)", "External URL", "Date precision", "Date source",
                 "Source title", "Retrieved at", "Citation history (JSON)", "Citation snapshots (JSON)",
                 "Citation history snapshots (JSON)", "Aliases (JSON)", "Provenance", "Provenances (JSON)",
                 "Is outlier (JSON)", "Additional paper metadata (JSON)"], rows)
