from copy import deepcopy
import csv
from io import StringIO

import pytest

from app import foresight_exports as exports


def warning(location, number, unit=""):
    return {"code": "numeric_mismatch", "location": location,
            "message": "根拠または集計指標に対応しない数値が含まれています。",
            "unmatched_numbers": [number],
            "unmatched_quantities": [{"value": number, "unit": unit}] if unit else []}


def validation(*warnings):
    return {"status": "warning" if warnings else "passed", "warnings": list(warnings)}


def warning_assessment():
    section_warning = warning("sections/0/text", "99", "%")
    fact_warning = warning("facts/0/statement", "1200", "MPa")
    return {
        "id": "1234567890abcdef1234567890abcdef", "revision": 1,
        "dataset_name": "表示確認用の合成データ（研究結果ではありません）",
        "created_at": "2026-09-14T12:00:00Z", "result_id": "synthetic-qa-only",
        "warnings": ["この文書は警告表示の検証用です。実在の研究成果や将来予測を示しません。"],
        "papers": [{"id": "qa-paper", "title": "Synthetic tensile-strength fixture", "year": 2025}],
        "candidates": [{
            "id": "qa-topic", "label": "ステンレス鋼の引張強度（表示確認用）",
            "growth": {"available": True, "growth_pct": 10.0,
                       "reason": "表示確認用の収録件数です。",
                       "series": [{"year": year, "count": count} for year, count in [(2021, 2), (2022, 3), (2023, 4), (2024, 5), (2025, 6)]]},
            "readiness": {"stage_label": "未判定", "evidence_coverage_score": None},
            "narrative": {
                "headline": "数値照合警告を含む評論の表示例", "mode": "synthetic_qa", "model": "LLM未接続",
                "numeric_hash": "unchanged-synthetic-numeric-hash",
                "validation": validation(section_warning),
                "sections": [
                    {"title": "支持の確認", "kind": "support", "text": "引張強度は 99% 改善するという記述を、未修正のまま表示しています。",
                     "evidence_ids": ["qa-paper"], "validation": validation(section_warning)},
                    {"title": "反証の確認", "kind": "counter", "text": "この数値は引用文と一致しません。実験条件と原著を確認する必要があります。",
                     "evidence_ids": ["qa-paper"], "validation": validation()},
                    {"title": "今後の見通し", "kind": "outlook", "text": "研究の進展は追加検証の結果に依存します。", "validation": validation()},
                    {"title": "次の研究", "kind": "next_steps", "text": "測定条件をそろえた比較と反証例の検索を進めます。", "validation": validation()},
                ],
                "caveats": ["合成データによる表示例です。"],
            },
            "content_facts": [{
                "id": "qa-fact", "paper_id": "qa-paper", "quote": "We measured tensile strength of 900 MPa.",
                "statement": "引張強度は 1200 MPa だった。", "verification": "quote_checked_numeric_warning",
                "validation": validation(fact_warning),
            }],
            "recommendations": [],
        }],
        "rounds": [], "numeric_hash": "synthetic-unchanged",
    }


@pytest.fixture
def rendered_text(monkeypatch):
    from reportlab.platypus import Paragraph as OriginalParagraph
    import reportlab.platypus
    items = []
    class RecordingParagraph(OriginalParagraph):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            items.append(self.getPlainText())
    monkeypatch.setattr(reportlab.platypus, "Paragraph", RecordingParagraph)
    return items


def test_pdf_keeps_original_commentary_and_fact_with_global_and_local_warnings(rendered_text):
    assessment = warning_assessment()
    before = deepcopy(assessment)
    document = exports.assessment_pdf(assessment)
    text = "\n".join(rendered_text)
    assert document.startswith(b"%PDF-") and len(document) > 5000
    assert text.count("数値照合に失敗・要確認") >= 5
    assert "このレポートのLLM出力" in text
    assert "この節" in text and "この根拠の要約" in text
    assert "引張強度は 99% 改善するという記述を、未修正のまま表示しています。" in text
    assert "引張強度は 1200 MPa だった。" in text
    assert "We measured tensile strength of 900 MPa." in text
    assert "要確認の数値：99、99 %" in text
    assert "要確認の数値：1200、1200 MPa" in text
    assert "quote_and_numbers_checked" not in text
    assert "synthetic-unchanged" in text
    assert assessment == before


def test_pdf_headline_or_caveat_numeric_warning_is_visible_without_section_warning(rendered_text):
    assessment = warning_assessment()
    candidate = assessment["candidates"][0]
    candidate["content_facts"] = []
    narrative = candidate["narrative"]
    narrative["sections"] = []
    narrative["headline"] = "需要は 777% 増えるという表示確認用の主張"
    narrative["caveats"] = ["2039 年に実用化するという表示確認用の主張"]
    narrative["validation"] = validation(warning("headline", "777", "%"), warning("caveats/0", "2039", "年"))
    exports.assessment_pdf(assessment)
    text = "\n".join(rendered_text)
    assert narrative["headline"] in text and narrative["caveats"][0] in text
    assert "777 %" in text and "2039 年" in text
    assert "照合箇所：評論の見出し" in text and "照合箇所：注意事項 1" in text


def test_pdf_section_warning_without_global_metadata_still_warns_cover(rendered_text):
    assessment = warning_assessment()
    candidate = assessment["candidates"][0]
    candidate["narrative"].pop("validation")
    candidate["content_facts"] = []
    exports.assessment_pdf(assessment)
    text = "\n".join(rendered_text)
    assert "このレポートのLLM出力" in text and "この節" in text


def test_pdf_fact_warning_cannot_disappear_beyond_regular_excerpt_limit(rendered_text):
    assessment = warning_assessment()
    candidate = assessment["candidates"][0]
    candidate.pop("narrative")
    warning_fact = candidate["content_facts"][0]
    normal_fact = {"paper_id": "qa-paper", "quote": "A synthetic non-numeric excerpt.", "verification": "quote_and_numbers_checked", "validation": validation()}
    candidate["content_facts"] = [deepcopy(normal_fact) for _ in range(12)] + [warning_fact]
    exports.assessment_pdf(assessment)
    text = "\n".join(rendered_text)
    assert "先頭12件と数値要確認の根拠" in text
    assert warning_fact["statement"] in text and "1200 MPa" in text
    assert "このレポートのLLM出力" in text


def test_pdf_passed_validation_adds_no_warning(rendered_text):
    assessment = warning_assessment()
    candidate = assessment["candidates"][0]
    candidate["narrative"]["validation"] = validation()
    for section in candidate["narrative"]["sections"]:
        section["validation"] = validation()
    candidate["content_facts"][0].update(validation=validation(), verification="quote_and_numbers_checked")
    exports.assessment_pdf(assessment)
    text = "\n".join(rendered_text)
    assert "数値照合に失敗・要確認" not in text
    assert "quote_and_numbers_checked" in text


def test_csv_retains_warning_details_original_text_and_verification():
    assessment = warning_assessment()
    rows = list(csv.DictReader(StringIO(exports.assessment_csv(assessment).lstrip("\ufeff"))))
    values = {row["JSON Pointer"]: row["Value"] for row in rows}
    base = "/candidates/0"
    assert values[base + "/narrative/validation/status"] == "warning"
    assert values[base + "/narrative/sections/0/validation/warnings/0/unmatched_quantities/0/unit"] == "%"
    assert values[base + "/content_facts/0/verification"] == "quote_checked_numeric_warning"
    assert values[base + "/content_facts/0/validation/warnings/0/unmatched_numbers/0"] == "1200"
    assert values[base + "/content_facts/0/statement"] == "引張強度は 1200 MPa だった。"
    assert values[base + "/narrative/sections/0/text"] == assessment["candidates"][0]["narrative"]["sections"][0]["text"]


def test_pdf_selected_candidate_does_not_warn_for_other_candidate(rendered_text):
    assessment = warning_assessment()
    assessment["candidates"].append({"id": "clean", "label": "警告のない別候補", "commentary": {"summary": "定性集計です。"}})
    exports.assessment_pdf(assessment, "clean")
    assert "数値照合に失敗・要確認" not in "\n".join(rendered_text)
