from copy import deepcopy

import pytest

from app import landscape_reports
from app.landscape_evidence import MAX_ABSTRACT_CHARS, TOTAL_ABSTRACT_CHARS, input_summary, prepare_papers
from test_landscape_reports import fixture, output, report_for, wait_job
from app.main import app
from app import field_llm
from fastapi.testclient import TestClient


def test_abstract_budget_keeps_conclusions_and_reports_actual_input_ranges():
    source = [{"id": f"paper-{i}", "side": "before" if i < 6 else "after",
               "abstract": "PURPOSE " + "abc " * 1700 + " CONCLUSION: strength improved."} for i in range(12)]
    before = deepcopy(source)
    papers = prepare_papers(source)
    assert source == before
    assert sum(p["abstract_sent_chars"] for p in papers) <= TOTAL_ABSTRACT_CHARS
    for original, p in zip(source, papers):
        assert p["abstract_sent_chars"] <= MAX_ABSTRACT_CHARS
        assert p["abstract"].startswith("PURPOSE") and p["abstract"].endswith("CONCLUSION: strength improved.")
        assert "中略" in p["abstract"] and p["abstract_truncated"]
        a, b = p["abstract_ranges"]
        assert p["abstract"].startswith(original["abstract"][a[0]:a[1]])
        assert p["abstract"].endswith(original["abstract"][b[0]:b[1]])
    summary = input_summary(papers)
    assert summary["before"] == summary["after"] == {"paper_count": 6, "abstract_count": 6}
    assert summary["truncated_abstract_count"] == 12


def test_short_and_missing_abstracts_are_distinct_and_aliases_never_collide_with_ids():
    papers = prepare_papers([{"id": "B1", "side": "before", "abstract": "Short result."},
                             {"id": "B2", "side": "before", "abstract": "  "},
                             {"id": "later", "side": "after", "abstract": None}])
    assert papers[0]["abstract"] == "Short result." and papers[0]["excerpt_strategy"] == "full"
    assert papers[1]["excerpt_strategy"] == "missing" and papers[1]["abstract_ranges"] == []
    assert not ({p["citation_id"] for p in papers} & {p["id"] for p in papers})
    assert input_summary(papers)["missing_abstract_count"] == 2


def test_short_citation_labels_resolve_and_unknown_ids_remain_unlinked(fixture):
    payload = landscape_reports.evidence_payload(report_for(fixture))
    before = next(p for p in payload["papers"] if p["side"] == "before")
    after = next(p for p in payload["papers"] if p["side"] == "after")
    value = output("前期はレーザー加熱、後期も強度の記述がある。", [before["citation_id"], after["citation_id"], "imagined-paper"])
    n = landscape_reports.validate_narrative(value, payload, "local_llm", "test")
    assert n["sections"][0]["evidence_ids"] == [before["id"], after["id"]]
    assert n["sections"][0]["unverified_evidence_ids"] == ["imagined-paper"]
    assert n["sections"][0]["text"] == value["sections"][0]["text"]
    assert n["validation"]["status"] == "warning"
    assert not any(w["code"] == "period_evidence_missing" for w in n["validation"]["warnings"])


@pytest.mark.parametrize("value", [
    "前期は加熱処理を、後期は溶接条件を検討している。抄録の結果を比較する必要がある。",
    {"headline": "内容の変化", "sections": [{"title": "前後比較", "content": "両期間でレーザー加熱を扱っている。"}]},
    {"answer": "抄録ではレーザー加熱による強度変化を検討している。", "reasoning": "DO NOT EXPOSE THIS"},
])
def test_completed_nonconforming_answer_is_visible_and_unverified(fixture, value):
    n = landscape_reports.validate_narrative(value, landscape_reports.evidence_payload(report_for(fixture)), "local_llm", "test")
    assert n["mode"] == "local_llm" and n["sections"][0]["text"]
    assert n["validation"]["status"] == "warning"
    assert any(w["code"] == "output_format_recovered" for w in n["validation"]["warnings"])
    assert "DO NOT EXPOSE" not in str(n)


@pytest.mark.parametrize("value", [None, {}, {"headline": "No analysis", "sections": []},
                                      {"reasoning": "private chain"}, output("   ")])
def test_empty_or_reasoning_only_is_not_labeled_as_generated_analysis(fixture, value):
    with pytest.raises(landscape_reports.NarrativeValidationError) as exc:
        landscape_reports.validate_narrative(value, landscape_reports.evidence_payload(report_for(fixture)), "local_llm", "test")
    assert exc.value.kind == "missing_final_answer"


def test_paper_year_and_heading_list_marker_are_not_false_numeric_mismatches(fixture):
    payload = landscape_reports.evidence_payload(report_for(fixture))
    value = output("B1は2023年に発表され、強度は900 MPaと記載されている。", ["B1", "A1"])
    value["sections"][0]["title"] = "1. 抄録の比較"
    n = landscape_reports.validate_narrative(value, payload, "local_llm", "test")
    assert not any(w["code"] == "numeric_mismatch" for w in n["validation"]["warnings"])


def test_api_preserves_unknown_reference_prose_with_warning_and_csv(fixture, monkeypatch):
    value = output("レーザー加熱による強度変化を比較している。", ["B1", "A1", "unverified-paper"])
    monkeypatch.setattr(field_llm, "structured_output", lambda *a, **kw: (value, "local_llm", "test"))
    with TestClient(app) as client:
        job = wait_job(client, fixture[0]["id"], provider="local")
        report = client.get("/api/landscape-reports/" + job["landscape_report_id"]).json()
        exported = client.get(f"/api/landscape-reports/{report['id']}/export").text
    assert report["generation_status"] == "generated"
    assert report["narrative"]["sections"][0]["text"] == value["sections"][0]["text"]
    assert report["input_summary"]["abstract_count"] == 12
    assert "unknown_evidence_id" not in report.get("generation_error_kind", "")
    assert "unverified_evidence_ids" in exported and "unverified-paper" in exported
