"""Phase 1-3: 出典の権威性と証拠互換性の厳格化の回帰テスト。"""

from __future__ import annotations

from fermiscope.domain.enums import SourceClass
from fermiscope.domain.models import EvidenceItem, ParameterEstimate
from fermiscope.estimation.fusion import fuse_evidence
from fermiscope.evidence.contradiction import _text_conflict
from fermiscope.evidence.ranker import infer_source_class


def _ev(**kw) -> EvidenceItem:
    kw.setdefault("url", "https://example.com/a")
    kw.setdefault("parameter_id", "p")
    return EvidenceItem(**kw)


# ---- 出典の権威性: 自己申告だけでは S にしない ----


def test_self_reported_publisher_on_untrusted_domain_not_s(settings):
    ev = _ev(url="https://evil.example.com/page", publisher="総務省統計局")
    assert infer_source_class(ev, settings) != SourceClass.S


def test_government_domain_corroborates_s(settings):
    ev = _ev(url="https://www.stat.go.jp/data", publisher="総務省統計局")
    assert infer_source_class(ev, settings) == SourceClass.S


def test_body_source_url_alone_does_not_promote(settings):
    """本文の `一次資料: <政府URL>` だけでは権威を S へ昇格しない(なりすまし対策)。"""
    ev = _ev(
        url="https://news.example.com/article",  # 実際に取得したのは非信頼ドメイン
        publisher="総務省統計局",
        parent_source_id="https://www.stat.go.jp/data",  # 本文の自己申告URL
    )
    assert infer_source_class(ev, settings) != SourceClass.S


def test_mock_gov_domain_still_s(settings):
    # 同梱デモの擬似政府ドメインは S を維持(オフライン動作の互換)
    ev = _ev(url="https://stats.example-gov.jp/x", publisher="総務省統計局")
    assert infer_source_class(ev, settings) == SourceClass.S


def test_lookalike_gov_domain_not_trusted(settings):
    """`evil-example-gov.jp` を `example-gov.jp` として信頼しない(サブドメイン境界一致)。"""
    ev = _ev(url="https://evil-example-gov.jp/x", publisher="総務省統計局")
    assert infer_source_class(ev, settings) != SourceClass.S
    # ラベル境界を越える偽装(prefix)も政府ドメイン扱いしない
    ev2 = _ev(url="https://notexample-gov.jp/x")
    assert infer_source_class(ev2, settings) != SourceClass.S


def test_real_intl_org_domain_class_a(settings):
    """実在の国際機関ドメイン(WHO/World Bank)は A に分類する。"""
    assert infer_source_class(_ev(url="https://www.who.int/data"), settings) == SourceClass.A
    assert (
        infer_source_class(_ev(url="https://data.worldbank.org/x"), settings) == SourceClass.A
    )


def test_mock_domains_separated_from_real_search(settings_brave):
    """モック用擬似ドメインは実検索設定(brave等)へ混入しない。"""
    # brave 設定では example-gov.jp は S ヒントを持たない(一般 .jp = D 相当)
    ev = _ev(url="https://stats.example-gov.jp/x")
    assert infer_source_class(ev, settings_brave) != SourceClass.S
    # 実在ドメインの分類は実検索設定でも維持される
    assert infer_source_class(_ev(url="https://www.who.int/data"), settings_brave) == SourceClass.A


# ---- 証拠互換性: 統合前に地域・期間を判定 ----


def _param(**kw) -> ParameterEstimate:
    kw.setdefault("id", "p")
    kw.setdefault("name", "店舗数")
    kw.setdefault("unit", "store")
    kw.setdefault("definition", "対象地域の店舗数")
    return ParameterEstimate(**kw)


def _scored_ev(value, geo="", unit="store", tp="", score=80.0, eid="e") -> EvidenceItem:
    return EvidenceItem(
        id=eid, url=f"https://stats.example-gov.jp/{eid}", parameter_id="p",
        extracted_value=value, unit=unit, geography=geo, time_period=tp,
        evidence_score=score, accepted=True, source_class=SourceClass.S,
    )


def test_different_regions_not_fused(settings):
    """東京の店舗数と大阪の店舗数を同一パラメータとして統合しない。"""
    param = _param(target_geography="東京都")
    tokyo = _scored_ev(1000, geo="東京都", eid="tokyo")
    osaka = _scored_ev(800, geo="大阪府", eid="osaka")
    fuse_evidence(param, [tokyo, osaka], settings, reference_year=2026)
    assert osaka.incompatible_reason  # 大阪は除外
    assert not tokyo.incompatible_reason
    assert param.central == 1000  # 東京のみで統合


def test_daily_and_annual_not_fused_without_conversion(settings):
    """日次値と年次値を無換算で統合しない。"""
    param = _param(name="1日あたり廃棄数", unit="item/day")
    daily = _scored_ev(500, unit="item", tp="1日", eid="daily")
    annual = _scored_ev(180000, unit="item", tp="1年間", eid="annual")
    fuse_evidence(param, [daily, annual], settings, reference_year=2026)
    assert annual.incompatible_reason  # 年次は除外
    assert not daily.incompatible_reason


def test_nationwide_total_count_not_reused_as_regional_total(settings):
    """全国の合計値(店舗数=外延量)を地域の合計へ流用しない(明示按分が必要)。"""
    param = _param(target_geography="東京都", unit="store")  # 店舗数=外延的な合計
    nationwide = _scored_ev(5000, geo="日本", eid="jp", unit="store")
    fuse_evidence(param, [nationwide], settings, reference_year=2026)
    assert nationwide.incompatible_reason  # 全国合計→地域合計は流用不可
    # 唯一の証拠が非互換なので値は捏造せず未解決化する
    assert param.central is None


def test_nationwide_ratio_allowed_for_regional_param(settings):
    """全国の比率(強度量)は地域へ適用可能=非互換ではない。"""
    param = _param(target_geography="東京都", name="該当率", unit="dimensionless")
    nationwide = _scored_ev(0.12, geo="日本", eid="jp", unit="dimensionless")
    fuse_evidence(param, [nationwide], settings, reference_year=2026)
    assert not nationwide.incompatible_reason
    assert param.central == 0.12


def test_nationwide_per_capita_allowed_for_regional_param(settings):
    """全国の1人あたり値(強度量)は地域へ適用可能=非互換ではない。"""
    param = _param(target_geography="東京都", name="1人あたり消費", unit="item/person")
    nationwide = _scored_ev(3.0, geo="日本", eid="jp", unit="item/person")
    fuse_evidence(param, [nationwide], settings, reference_year=2026)
    assert not nationwide.incompatible_reason
    assert param.central == 3.0


# ---- 定義の表記揺れだけで除外しない ----


def test_wording_variation_not_treated_as_conflict():
    assert _text_conflict("二人以上の世帯", "二人以上の世帯 ") is False
    assert _text_conflict("2人以上の世帯", "2人以上の世帯") is False
    assert _text_conflict("全世帯", "二人以上の世帯") is True  # 本当に違えば衝突
