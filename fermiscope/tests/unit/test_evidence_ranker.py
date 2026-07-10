"""証拠採点のテスト。"""

from fermiscope.domain.enums import SourceClass
from fermiscope.domain.models import EvidenceItem, ParameterEstimate
from fermiscope.evidence.ranker import infer_source_class, rank_evidence


def make_param(**kwargs) -> ParameterEstimate:
    defaults = dict(
        id="p1", name="テスト値", unit="dimensionless", definition="テストの定義",
        target_geography="東京都",
    )
    defaults.update(kwargs)
    return ParameterEstimate(**defaults)


def make_evidence(**kwargs) -> EvidenceItem:
    defaults = dict(url="https://stats.example-gov.jp/data.html", parameter_id="p1")
    defaults.update(kwargs)
    return EvidenceItem(**defaults)


def test_government_source_scores_higher_than_anonymous(settings):
    param = make_param()
    gov = rank_evidence(
        make_evidence(
            publisher="総務省統計局",
            extracted_value=100.0,
            exact_definition="定義明示",
            methodology_summary="全数調査",
            geography="東京都",
            time_period="2025年",
            publication_date="2025年",
        ),
        param, settings, reference_year=2026,
    )
    anon = rank_evidence(
        make_evidence(url="https://forum.example-bbs.jp/x", extracted_value=100.0),
        param, settings, reference_year=2026,
    )
    assert gov.source_class == SourceClass.S
    assert anon.source_class == SourceClass.E
    assert gov.evidence_score > anon.evidence_score + 30
    assert gov.scoring_reasons and anon.scoring_reasons  # 採点理由が保存される
    assert 0 <= anon.evidence_score <= 100
    assert 0 <= gov.evidence_score <= 100


def test_stale_data_penalty(settings):
    param = make_param()
    old = rank_evidence(
        make_evidence(publisher="総務省統計局", extracted_value=1.0,
                      publication_date="1996年", time_period="1996年"),
        param, settings, reference_year=2026,
    )
    assert "stale_data_penalty" in old.penalties_applied
    assert old.subscores["time_fit"] <= 20


def test_secondary_citation_penalty(settings):
    param = make_param()
    reprint = rank_evidence(
        make_evidence(parent_source_id="https://original.example-gov.jp/stat",
                      extracted_value=1.0),
        param, settings, reference_year=2026,
    )
    assert "secondary_citation_penalty" in reprint.penalties_applied
    assert reprint.subscores["primaryness"] <= 40


def test_conflict_of_interest_for_market_wide_claims(settings):
    param = make_param(name="ピアノ市場全体の販売台数", description="市場規模")
    corp = rank_evidence(
        make_evidence(url="https://maker.example.co.jp/ir.html",
                      publisher="株式会社サンプル楽器", extracted_value=1000.0),
        param, settings, reference_year=2026,
    )
    assert "conflict_of_interest_penalty" in corp.penalties_applied


def test_patent_evidence_use_dependent(settings):
    # 市場普及率の証拠として特許 → 直接性が低い
    market_param = make_param(name="EV充電器の市場普及率")
    patent_market = rank_evidence(
        make_evidence(url="https://jplatpat.example-gov.jp/patent/123",
                      publisher="特許庁", title="特許公報", extracted_value=0.5),
        market_param, settings, reference_year=2026,
    )
    assert patent_market.subscores["parameter_directness"] <= 30
    # 出願日等の法的事実 → 直接性が高い
    legal_param = make_param(name="特許出願件数")
    patent_legal = rank_evidence(
        make_evidence(url="https://jplatpat.example-gov.jp/patent/123",
                      publisher="特許庁", title="特許公報 請求項",
                      exact_definition="出願日と請求項の記載", extracted_value=120.0),
        legal_param, settings, reference_year=2026,
    )
    assert patent_legal.subscores["parameter_directness"] >= 85


def test_nationwide_data_for_regional_param_reduced_fit(settings):
    param = make_param(target_geography="東京都")
    ev = rank_evidence(
        make_evidence(publisher="総務省統計局", extracted_value=1.0, geography="日本"),
        param, settings, reference_year=2026,
    )
    assert 40 <= ev.subscores["geography_fit"] <= 60


def test_domain_hint_fallback(settings):
    ev = make_evidence(url="https://something.go.jp/stats")
    assert infer_source_class(ev, settings) == SourceClass.S
    ev2 = make_evidence(url="https://random.example.com/blog-post")
    assert infer_source_class(ev2, settings) in (SourceClass.D, SourceClass.E)
