from copy import deepcopy

from app.llm_context import prepare


def test_context_budget_prunes_period_references_to_omitted_papers():
    before = [f"before-{index}" for index in range(20)]
    after = [f"after-{index}" for index in range(20)]
    payload = {
        "movement": {
            "from_count": 750,
            "to_count": 900,
            "cosine_distance": .125,
            "evidence_before": before,
            "evidence_after": after,
            "nested": {"evidence_ids": [before[-1], after[-1]]},
        },
        "papers": [
            {"id": identifier, "side": side, "period": period,
             "abstract": "Measured alloy structure and mechanical properties. " * 160}
            for side, period, identifiers in (("before", "2023", before), ("after", "2024", after))
            for identifier in identifiers
        ],
    }
    original = deepcopy(payload)
    actual, audit = prepare(payload, "Compare the two periods.", {"type": "object"},
                            context_window=5000, context_source="browser", output_tokens=1000)
    assert audit["fits"] and audit["omitted_paper_ids"]
    assert payload == original
    sent = {paper["id"] for paper in actual["papers"]}
    assert actual["movement"]["evidence_before"] == [pid for pid in before if pid in sent]
    assert actual["movement"]["evidence_after"] == [pid for pid in after if pid in sent]
    assert actual["movement"]["evidence_before"] and actual["movement"]["evidence_after"]
    assert actual["movement"]["nested"]["evidence_ids"] == [
        pid for pid in original["movement"]["nested"]["evidence_ids"] if pid in sent
    ]
    for key in ("from_count", "to_count", "cosine_distance"):
        assert actual["movement"][key] == original["movement"][key]
