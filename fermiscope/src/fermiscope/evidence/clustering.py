"""転載・重複資料のクラスタリング。

同じ元情報を転載した複数記事を独立した複数証拠として数えない(絶対条件9)。
判定基準:
1. content_hash が同一
2. parent_source_id / canonical_url が同一資料を指す
3. タイトル類似度が閾値以上かつ抽出値が同一
"""

from __future__ import annotations

from difflib import SequenceMatcher

from fermiscope.config import Settings
from fermiscope.domain.models import EvidenceItem


def _title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _same_primary(a: EvidenceItem, b: EvidenceItem) -> bool:
    refs_a = {r for r in (a.parent_source_id, a.canonical_url) if r}
    refs_b = {r for r in (b.parent_source_id, b.canonical_url) if r}
    # 一方が他方の原典を参照している場合(転載記事 → 一次資料)
    if a.url in refs_b or b.url in refs_a:
        return True
    return bool(refs_a & refs_b)


def cluster_evidence(items: list[EvidenceItem], settings: Settings) -> dict[str, list[str]]:
    """証拠をクラスタリングし、各 EvidenceItem.cluster_id を設定する。

    Returns:
        cluster_id -> evidence_id のリスト
    """
    threshold = settings.scoring.clustering.title_similarity_threshold
    n = len(items)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            a, b = items[i], items[j]
            if a.parameter_id != b.parameter_id:
                continue
            same = False
            if a.content_hash and a.content_hash == b.content_hash or _same_primary(a, b) or (
                a.extracted_value is not None
                and a.extracted_value == b.extracted_value
                and a.unit == b.unit
                and _title_similarity(a.title, b.title) >= threshold
            ):
                same = True
            if same:
                union(i, j)

    clusters: dict[str, list[str]] = {}
    for i, ev in enumerate(items):
        root = find(i)
        cluster_id = f"cluster_{items[root].id}"
        ev.cluster_id = cluster_id
        clusters.setdefault(cluster_id, []).append(ev.id)
    return clusters
