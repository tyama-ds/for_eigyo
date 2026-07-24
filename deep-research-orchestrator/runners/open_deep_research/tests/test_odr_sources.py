"""sources/usage/検索結果整形の純ロジックテスト (エンジン本体不要 / py3.11可)。"""

from __future__ import annotations

from odr_engine import (
    extract_usage,
    find_state_value,
    format_searx_results,
    parse_sources_section,
)

REPORT = """\
# 水素製鉄の最新動向

本文です。重要な発見 [1] があります。

## 結論

まとめ [2]。

### Sources

[1] 水素製鉄プロジェクト概要: https://example.org/h2/overview
[2] Green Steel: Market Outlook 2026: https://example.net/green-steel?x=1
- [3] タイトルなし行 https://example.com/plain
無関係な行 (URLなし)
[4] 重複URL: https://example.org/h2/overview
"""


class TestParseSources:
    def test_parses_numbered_list(self):
        sources = parse_sources_section(REPORT)
        urls = [s["url"] for s in sources]
        assert urls == [
            "https://example.org/h2/overview",
            "https://example.net/green-steel?x=1",
            "https://example.com/plain",
        ]

    def test_titles_extracted_including_colons(self):
        sources = parse_sources_section(REPORT)
        assert sources[0]["title"] == "水素製鉄プロジェクト概要"
        # タイトル内のコロンは保持される
        assert sources[1]["title"] == "Green Steel: Market Outlook 2026"

    def test_absent_section_returns_empty(self):
        assert parse_sources_section("# レポート\n\n本文のみ") == []
        assert parse_sources_section(None) == []
        assert parse_sources_section("") == []

    def test_stops_at_next_heading(self):
        report = (
            "## Sources\n[1] A: https://a.example/x\n\n## Appendix\n"
            "[9] B: https://b.example/should-not-appear\n"
        )
        sources = parse_sources_section(report)
        assert [s["url"] for s in sources] == ["https://a.example/x"]

    def test_uses_last_sources_heading(self):
        report = (
            "### Sources\n[1] old: https://old.example/\n\n本文\n\n"
            "### Sources\n[1] new: https://new.example/\n"
        )
        sources = parse_sources_section(report)
        assert [s["url"] for s in sources] == ["https://new.example/"]


class _FakeAIMessage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        self.content = "x"


class TestExtractUsage:
    def test_sums_nested_messages(self):
        update = {
            "researcher": {
                "messages": [_FakeAIMessage(100, 20), _FakeAIMessage(50, 5)],
                "other": "text",
            }
        }
        assert extract_usage(update) == (150, 25)

    def test_dict_usage_metadata(self):
        update = {"usage_metadata": {"input_tokens": 7, "output_tokens": 3}}
        assert extract_usage(update) == (7, 3)

    def test_no_usage_returns_zero(self):
        assert extract_usage({"node": {"messages": ["plain"]}}) == (0, 0)
        assert extract_usage(None) == (0, 0)
        assert extract_usage("text") == (0, 0)

    def test_malformed_usage_ignored(self):
        assert extract_usage({"usage_metadata": {"input_tokens": "abc"}}) == (0, 0)


class TestFindStateValue:
    def test_finds_final_report_nested(self):
        update = {"final_report_generation": {"final_report": "# R", "messages": []}}
        assert find_state_value(update, "final_report") == "# R"

    def test_finds_in_list(self):
        update = {"a": [{"b": {"research_brief": "brief!"}}]}
        assert find_state_value(update, "research_brief") == "brief!"

    def test_missing_returns_none(self):
        assert find_state_value({"a": {"b": 1}}, "final_report") is None
        assert find_state_value(None, "final_report") is None


class TestFormatSearxResults:
    PAYLOAD = {
        "results": [
            {"title": "結果1", "url": "https://a.example/1", "content": "snippet  one\n改行あり"},
            {"title": "結果2", "url": "https://a.example/2", "content": "snippet two"},
            {"no_url": True},
            {"title": "結果3", "url": "https://a.example/3", "content": "snippet three"},
        ]
    }

    def test_formats_title_url_snippet(self):
        text = format_searx_results("query x", self.PAYLOAD, max_results=10)
        assert 'Search results for "query x":' in text
        assert "結果1" in text and "https://a.example/1" in text
        assert "snippet one" in text  # 空白正規化される

    def test_respects_max_results(self):
        text = format_searx_results("q", self.PAYLOAD, max_results=2)
        assert "https://a.example/2" in text
        assert "https://a.example/3" not in text

    def test_empty_results(self):
        assert "(no results)" in format_searx_results("q", {"results": []}, 5)
        assert "(no results)" in format_searx_results("q", {}, 5)
        assert "(no results)" in format_searx_results("q", None, 5)
        assert "(no results)" in format_searx_results("q", {"results": "bad"}, 5)

    def test_snippet_truncated(self):
        payload = {"results": [{"title": "t", "url": "https://x.example/", "content": "a" * 2000}]}
        text = format_searx_results("q", payload, 5)
        assert "a" * 500 in text
        assert "a" * 501 not in text
