import pytest

from app.text_metadata import analysis_abstract, is_test_summary


@pytest.mark.parametrize("leading", ["", " ", "\n\t", "\u3000"])
def test_exact_leading_marker_is_recognized_and_removed_only_for_analysis(leading):
    original = leading + "[TEST SUMMARY; TITLE-BASED; JA] \n  電池材料の量子計算。\n二行目。  "
    assert is_test_summary(original)
    assert analysis_abstract(original) == "電池材料の量子計算。\n二行目。  "
    assert "[TEST SUMMARY; TITLE-BASED; JA]" in original


@pytest.mark.parametrize("text", [
    "A summary of synthetic data research.",
    "[SUMMARY] A real abstract.",
    "[TEST SUMMARY; TITLE-BASED; EN] A different convention.",
    "[test summary; title-based; ja] Not the exact explicit marker.",
    "A study of [TEST SUMMARY; TITLE-BASED; JA] markers.",
    "[TEST SUMMARY; TITLE-BASED; JA; OTHER] Unknown marker.",
    "  Ordinary abstract with surrounding whitespace.  ",
    "",
])
def test_unrelated_words_and_nonleading_or_unknown_markers_are_not_rewritten(text):
    assert not is_test_summary(text)
    assert analysis_abstract(text) == text


def test_absent_text_and_marker_without_content_remain_empty_for_analysis():
    assert not is_test_summary(None) and analysis_abstract(None) == ""
    assert is_test_summary("[TEST SUMMARY; TITLE-BASED; JA]")
    assert analysis_abstract("[TEST SUMMARY; TITLE-BASED; JA] \n") == ""
