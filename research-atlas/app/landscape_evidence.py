"""Balanced, auditable abstract excerpts for movement and centroid critiques."""
from collections import Counter

MAX_ABSTRACT_CHARS = 4000
TOTAL_ABSTRACT_CHARS = 24000
OMISSION = "\n[…中略：抄録の中間部分を省略…]\n"


def prepare_papers(papers: list[dict]) -> list[dict]:
    """Retain complete ordinary abstracts and both ends of longer ones."""
    texts = [str(p.get("abstract") or "").strip() for p in papers]
    allocations = [0] * len(texts)
    remaining = TOTAL_ABSTRACT_CHARS
    while remaining:
        active = [i for i, text in enumerate(texts) if allocations[i] < min(len(text), MAX_ABSTRACT_CHARS)]
        if not active:
            break
        share = max(1, remaining // len(active))
        for i in active:
            size = min(share, remaining, min(len(texts[i]), MAX_ABSTRACT_CHARS) - allocations[i])
            allocations[i] += size
            remaining -= size
    result, ranks = [], Counter()
    occupied = {str(p["id"]) for p in papers}
    for paper, text, budget in zip(papers, texts, allocations):
        side = paper.get("side", "centroid")
        ranks[side] += 1
        prefix = {"before": "B", "after": "A"}.get(side, "P")
        while f"{prefix}{ranks[side]}" in occupied:
            ranks[side] += 1
        alias = f"{prefix}{ranks[side]}"
        occupied.add(alias)
        truncated = len(text) > budget
        ranges = [[0, len(text)]] if text else []
        excerpt = text
        if truncated:
            available = budget - len(OMISSION)
            head = int(available * .6)
            tail = available - head
            excerpt = text[:head] + OMISSION + text[-tail:]
            ranges = [[0, head], [len(text) - tail, len(text)]]
        result.append({**paper, "citation_id": alias,
                       "abstract": excerpt, "abstract_original_chars": len(text),
                       "abstract_sent_chars": len(excerpt), "abstract_truncated": truncated,
                       "abstract_ranges": ranges,
                       "excerpt_strategy": "head_and_tail" if truncated else "full" if text else "missing"})
    return result


def input_summary(papers: list[dict]) -> dict:
    def counts(rows):
        return {"paper_count": len(rows), "abstract_count": sum(bool(p.get("abstract", "").strip()) for p in rows)}
    count = counts(papers)
    return {**count, "missing_abstract_count": count["paper_count"] - count["abstract_count"],
            "truncated_abstract_count": sum(bool(p.get("abstract_truncated")) for p in papers),
            "abstract_chars": sum(len(p.get("abstract", "")) for p in papers),
            **{side: counts([p for p in papers if p.get("side") == side]) for side in ("before", "after", "centroid")},
            "selection": "対象の内容重心に近い論文。期間比較は前後各最大6本、単一重心は最大6本。",
            "excerpt_policy": "抄録は各最大4,000文字・合計最大24,000文字。長い抄録は冒頭と末尾を残し、省略箇所を明示。"}
