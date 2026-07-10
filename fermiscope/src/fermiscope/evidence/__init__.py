"""証拠の抽出・採点・クラスタリング・矛盾検出。"""

from fermiscope.evidence.clustering import cluster_evidence
from fermiscope.evidence.contradiction import detect_contradictions
from fermiscope.evidence.dates import parse_year
from fermiscope.evidence.ranker import infer_source_class, rank_evidence

__all__ = [
    "cluster_evidence",
    "detect_contradictions",
    "infer_source_class",
    "parse_year",
    "rank_evidence",
]
