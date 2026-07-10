"""転載クラスタリングと矛盾検出のテスト。"""

from fermiscope.domain.models import EvidenceItem, ParameterEstimate
from fermiscope.evidence.clustering import cluster_evidence
from fermiscope.evidence.contradiction import detect_contradictions


def ev(**kwargs) -> EvidenceItem:
    defaults = dict(url="https://a.example.jp/", parameter_id="p1", accepted=True)
    defaults.update(kwargs)
    return EvidenceItem(**defaults)


def param(**kwargs) -> ParameterEstimate:
    defaults = dict(id="p1", name="テスト", unit="dimensionless")
    defaults.update(kwargs)
    return ParameterEstimate(**defaults)


def test_same_content_hash_clustered(settings):
    a = ev(content_hash="abc")
    b = ev(url="https://b.example.jp/", content_hash="abc")
    c = ev(url="https://c.example.jp/", content_hash="xyz")
    clusters = cluster_evidence([a, b, c], settings)
    assert a.cluster_id == b.cluster_id != c.cluster_id
    assert len(clusters) == 2


def test_reprint_citing_primary_clustered(settings):
    primary = ev(url="https://gov.example-gov.jp/stat.csv")
    reprint = ev(url="https://news.example.jp/article",
                 parent_source_id="https://gov.example-gov.jp/stat.csv")
    clusters = cluster_evidence([primary, reprint], settings)
    assert primary.cluster_id == reprint.cluster_id
    assert len(clusters) == 1


def test_similar_title_same_value_clustered(settings):
    a = ev(title="ピアノの普及率は10.4%に低下 内閣府調査", extracted_value=10.4, unit="percent")
    b = ev(url="https://b.example.jp/", title="ピアノの普及率は10.4%に低下:内閣府調査より",
           extracted_value=10.4, unit="percent")
    cluster_evidence([a, b], settings)
    assert a.cluster_id == b.cluster_id


def test_different_parameters_not_clustered(settings):
    a = ev(content_hash="abc")
    b = ev(url="https://b.example.jp/", content_hash="abc", parameter_id="p2")
    cluster_evidence([a, b], settings)
    assert a.cluster_id != b.cluster_id


def test_contradiction_detected_with_time_analysis(settings):
    p = param()
    recent = ev(extracted_value=10.4, unit="percent", evidence_score=85,
                time_period="2024年3月", exact_definition="普及率は保有世帯の割合")
    old = ev(url="https://old.example.jp/", extracted_value=22.0, unit="percent",
             evidence_score=60, time_period="1996年3月",
             exact_definition="普及率は保有世帯の割合")
    cluster_evidence([recent, old], settings)
    contradictions = detect_contradictions(p, [recent, old], settings)
    assert len(contradictions) == 1
    con = contradictions[0]
    assert con.ratio > 2.0
    assert "time" in con.analysis
    assert "平均で隠さず" in con.note


def test_compatible_values_no_contradiction(settings):
    p = param()
    a = ev(extracted_value=10.0, unit="percent", evidence_score=80)
    b = ev(url="https://b.example.jp/", extracted_value=11.0, unit="percent", evidence_score=75)
    cluster_evidence([a, b], settings)
    assert detect_contradictions(p, [a, b], settings) == []


def test_definition_mismatch_marks_incompatible(settings):
    p = param()
    a = ev(extracted_value=10.0, unit="percent", evidence_score=85,
           exact_definition="世帯あたりの保有割合")
    b = ev(url="https://b.example.jp/", extracted_value=45.0, unit="percent",
           evidence_score=50, exact_definition="人口あたりの経験率")
    cluster_evidence([a, b], settings)
    contradictions = detect_contradictions(p, [a, b], settings)
    assert contradictions and "definition" in contradictions[0].analysis
    # 低スコア側が統合から除外される(表示は残る)
    assert b.incompatible_reason
    assert not a.incompatible_reason


def test_reprints_not_treated_as_contradiction_pair(settings):
    """同一クラスタ内の証拠同士は矛盾ペアとして数えない。"""
    p = param()
    a = ev(extracted_value=10.0, unit="percent", evidence_score=85, content_hash="same")
    b = ev(url="https://b.example.jp/", extracted_value=30.0, unit="percent",
           evidence_score=40, content_hash="same")
    cluster_evidence([a, b], settings)
    assert detect_contradictions(p, [a, b], settings) == []
