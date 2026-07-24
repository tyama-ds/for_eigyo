"""正規化・比較・引用検証の単体テスト (DB不要部分)。"""

from __future__ import annotations

from app.normalizer.normalize import canonicalize_url, normalize_claim_text
from app.synthesis.synthesize import validate_citations


class TestCanonicalizeUrl:
    def test_strips_tracking_and_fragment(self):
        url = "https://Example.org/reports/abc/?utm_source=x&utm_medium=y&page=2#section"
        assert canonicalize_url(url) == "https://example.org/reports/abc?page=2"

    def test_keeps_meaningful_query(self):
        assert canonicalize_url("https://a.com/p?q=llm&fbclid=123") == "https://a.com/p?q=llm"

    def test_trailing_slash_normalized(self):
        assert canonicalize_url("https://a.com/path/") == "https://a.com/path"
        assert canonicalize_url("https://a.com/") == "https://a.com/"

    def test_invalid_url_passthrough(self):
        assert canonicalize_url("not a url") == "not a url"


class TestNormalizeClaimText:
    def test_nfkc_whitespace_case(self):
        assert normalize_claim_text("  市場規模は  １２０億ドル。") == "市場規模は 120億ドル"
        assert normalize_claim_text("Growth IS 12%.") == "growth is 12%"


class TestValidateCitations:
    REGISTRY = {
        "S1": {"sid": "S1", "url": "https://example.org/a?utm_source=m",
               "canonical_url": "https://example.org/a", "title": "A", "excerpt": "ex",
               "engines": ["mock-fast"], "source_ids": ["src1"]},
        "S2": {"sid": "S2", "url": "https://example.org/b",
               "canonical_url": "https://example.org/b", "title": "B", "excerpt": None,
               "engines": ["mock-slow"], "source_ids": ["src2"]},
    }

    def test_known_citations_resolved(self):
        report = "発見1 [S1]。発見2 [S2]。"
        cleaned, citations, warnings = validate_citations(report, self.REGISTRY)
        assert "[S1]" in cleaned and "[S2]" in cleaned
        assert {c["sid"] for c in citations} == {"S1", "S2"}
        assert citations[0]["engines"]
        assert warnings == []

    def test_unknown_citation_removed_with_warning(self):
        report = "捏造引用 [S99] と正しい引用 [S1]。"
        cleaned, citations, warnings = validate_citations(report, self.REGISTRY)
        assert "[S99]" not in cleaned
        assert "[S1]" in cleaned
        assert any("S99" in w for w in warnings)
        assert {c["sid"] for c in citations} == {"S1"}

    def test_invented_url_warned(self):
        report = "参考 [S1]。詳細は https://invented.example.net/xyz を参照。"
        _, _, warnings = validate_citations(report, self.REGISTRY)
        assert any("invented.example.net" in w for w in warnings)

    def test_registry_urls_not_warned(self):
        report = "参考 https://example.org/b [S2]。"
        _, _, warnings = validate_citations(report, self.REGISTRY)
        assert warnings == []
