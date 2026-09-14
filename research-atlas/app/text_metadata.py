"""Explicit test-text markers; original bibliographic text remains unchanged."""

import re


_TEST_SUMMARY_PREFIX = re.compile(r"^\s*\[TEST SUMMARY; TITLE-BASED; JA\]")


def is_test_summary(text: str | None) -> bool:
    """Recognize only the known leading marker, with optional preceding whitespace."""
    return isinstance(text, str) and bool(_TEST_SUMMARY_PREFIX.match(text))


def analysis_abstract(text: str | None) -> str:
    """Remove that metadata prefix for analysis; do not rewrite other abstracts."""
    if text is None:
        return ""
    match = _TEST_SUMMARY_PREFIX.match(text)
    return text[match.end():].lstrip() if match else text
