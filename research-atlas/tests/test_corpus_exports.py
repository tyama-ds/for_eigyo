from __future__ import annotations

import csv
import io
import json

from app import corpus_exports as exports


def report():
    return {"id": "a" * 32, "result_id": "b" * 32, "status": "paused", "partial": True,
            "counts": {"total": 4, "completed": 1, "missing": 1, "failed": 1, "pending": 1,
                       "total_chars": 19, "processed_chars": 6, "cached": 0},
            "annual": [{"year": 2024, "total": 4, "completed": 1, "missing": 1, "failed": 1, "pending": 1}],
            "topics": [{"topic_id": "t1", "label": "試験 <steel>", "count": 4, "completed": 1}],
            "methods": [{"method": "引張試験", "paper_count": 1}],
            "groups": [{"label": "2024年", "narrative": {"sections": [{"title": "根拠と相違", "text": "結果の評論。", "paper_ids": ["p1"]}]}}],
            "narrative": {"sections": [{"title": "研究の伸び", "text": "原文に基づく仮説。", "paper_ids": ["p1"]}]},
            "warnings": ["数値照合に注意が必要です。"]}


def records():
    yield {"paper_id": "p1", "title": "=1+1", "year": 2024, "topic_id": "t1", "status": "completed",
           "abstract": "ABC 12", "extraction": {"facts": [{"id": "f1", "kind": "result", "statement": "12 MPa",
               "quote": "ABC 12", "source_start": 0, "source_end": 6, "numbers": [{"value": "12", "unit": "MPa"}],
               "warnings": ["単位を確認"]}], "chunks": [{"start": 0, "end": 6, "status": "completed"}]}}
    for key, state in (("p2", "missing"), ("p3", "failed"), ("p4", "pending")):
        yield {"paper_id": key, "title": "@unsafe", "status": state, "extraction": None, "error": "安全なエラー" if state == "failed" else ""}


def test_json_includes_all_states_and_supporting_positions():
    value = json.loads("".join(exports.iter_json(report(), records())))
    assert len(value["papers"]) == 4
    assert {item["status"] for item in value["papers"]} == {"completed", "missing", "failed", "pending"}
    fact = value["papers"][0]["extraction"]["facts"][0]
    assert value["papers"][0]["abstract"][fact["source_start"]:fact["source_end"]] == fact["quote"]
    assert value["report"]["narrative"]["sections"][0]["paper_ids"] == ["p1"]
    assert "抄録" in value["scope_note"]


def test_csv_quotes_formulas_and_keeps_missing_failed_pending_and_chunks():
    text = "".join(exports.iter_csv(report(), records()))
    assert text.startswith("\ufeff")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    paper_rows = [row for row in rows if row["row_type"] == "paper"]
    assert len(paper_rows) == 4
    assert paper_rows[0]["title"] == "'=1+1"
    assert paper_rows[1]["title"] == "'@unsafe"
    fact = next(row for row in rows if row["row_type"] == "fact")
    assert (fact["source_start"], fact["source_end"], fact["quote"]) == ("0", "6", "ABC 12")
    assert json.loads(fact["numbers_json"])[0]["unit"] == "MPa"
    assert json.loads(rows[0]["details_json"])["counts"]["failed"] == 1
    assert next(row for row in rows if row["row_type"] == "chunk")["source_end"] == "6"


def test_streaming_is_lazy_and_has_no_twenty_thousand_record_cutoff():
    visited = []
    def generate():
        for index in range(20003):
            visited.append(index)
            yield {"paper_id": str(index), "status": "pending"}
    stream = exports.iter_json(report(), generate())
    opening = next(stream)
    assert visited == []
    first = next(stream)
    assert visited == [0]
    value = json.loads(opening + first + "".join(stream))
    assert len(value["papers"]) == 20003
    visited.clear()
    stream = exports.iter_csv(report(), generate())
    next(stream)  # header
    next(stream)  # report
    assert visited == []
    assert sum(1 for line in stream if next(csv.reader(io.StringIO(line)))[0] == "paper") == 20003


def test_pdf_uses_saved_reviews_warns_and_escapes_markup(monkeypatch):
    # Helvetica keeps this test portable; Japanese-font visual QA runs separately.
    monkeypatch.setattr(exports, "_font", lambda: "Helvetica")
    from reportlab.platypus import SimpleDocTemplate
    seen = []
    real = SimpleDocTemplate.build
    def capture(self, story, **kwargs):
        seen.extend(story)
        return real(self, story, **kwargs)
    monkeypatch.setattr(SimpleDocTemplate, "build", capture)
    value = exports.report_pdf(report())
    assert value.startswith(b"%PDF-")
    prose = "\n".join(getattr(item, "text", "") for item in seen)
    assert "未処理・失敗・欠測" in prose
    assert "研究の伸び" in prose and "原文に基づく仮説" in prose
    assert "2024年" in prose and "根拠と相違" in prose
    assert "数値照合に注意" in prose
    assert "0から数え" in prose


def test_pdf_renders_all_groups_and_long_prose_across_pages(monkeypatch):
    monkeypatch.setattr(exports, "_font", lambda: "Helvetica")
    data = report()
    data["groups"] = [{"label": f"Group {i}", "narrative": {"sections": [
        {"title": f"Section {i}", "text": "A detailed observation and its limitations. " * 180}]}} for i in range(25)]
    from reportlab.platypus import SimpleDocTemplate
    real = SimpleDocTemplate.build
    seen = []
    def capture(self, story, **kwargs):
        seen.extend(getattr(item, "text", "") for item in story)
        return real(self, story, **kwargs)
    monkeypatch.setattr(SimpleDocTemplate, "build", capture)
    result = exports.report_pdf(data)
    assert result.startswith(b"%PDF-")
    assert all(f"Group {i}" in seen for i in range(25))
