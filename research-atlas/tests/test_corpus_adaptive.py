from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from app import corpus_adaptive, corpus_llm as corpus, field_llm, local_llm_stream
from test_corpus_llm import extraction_mock, synthesis_mock, paper, quoted, inputs


def coverage(value, text):
    chunks = sorted(value["chunks"], key=lambda chunk: chunk["start"])
    assert chunks[0]["start"] == 0 and chunks[-1]["end"] == len(text)
    assert all(a["end"] == b["start"] for a, b in zip(chunks, chunks[1:]))
    assert "".join(text[c["start"]:c["end"]] for c in chunks) == text


@pytest.mark.parametrize("kind", sorted(corpus_adaptive.TIMEOUT_KINDS))
def test_timeout_splits_and_retries_every_character_with_audited_global_budget(monkeypatch, kind):
    successful, updates = [], []
    def respond(payload, options):
        text = payload["papers"][0]["abstract"]
        if len(text) > 450:
            raise local_llm_stream.LocalStreamError("private server detail", kind=kind)
        successful.append(text)
        return {"facts": [quoted(text[-30:])]}
    calls = extraction_mock(monkeypatch, respond)
    text = "Results showed a material effect. " * 24
    adaptive = {"save": updates.append}
    value = corpus.extract_paper(paper(text), "local", "test-model", adaptive=adaptive)
    assert value["status"] == "completed" and "".join(successful) == text
    coverage(value, text)
    assert len(calls) == len(successful) + 1
    assert value["adaptive"]["timeout_recoveries"] == adaptive["state"]["timeout_recoveries"] == 1
    assert adaptive["state"]["events"][-1]["kind"] == kind
    assert "private server detail" not in str(updates)


def test_per_paper_budget_exhaustion_stops_calls_and_keeps_all_unfinished_ranges(monkeypatch):
    def timeout(*_):
        raise local_llm_stream.LocalStreamError("slow", kind="total_timeout")
    calls = extraction_mock(monkeypatch, timeout)
    text = "Scientific results. " * 800
    adaptive = {}
    value = corpus.extract_paper(paper(text), "local", "test-model", adaptive=adaptive)
    assert len(calls) == 4 and adaptive["state"]["timeout_recoveries"] == 3
    assert value["error_kind"] == "timeout_recovery_exhausted"
    assert value["status"] == "failed" and not value["covered_chars"]
    coverage(value, text)


def test_indivisible_input_is_retried_once_only(monkeypatch):
    calls = extraction_mock(monkeypatch, lambda *_: (_ for _ in ()).throw(local_llm_stream.LocalStreamError("slow", kind="read_timeout")))
    value = corpus.extract_paper(paper("Small input with one finding."), "local", "test-model")
    assert len(calls) == 2 and calls[0] == calls[1]
    assert value["error_kind"] == "timeout_recovery_exhausted"
    assert value["adaptive"]["timeout_recoveries"] == 1


@pytest.mark.parametrize("kind", ["connect_timeout", "write_timeout", "pool_timeout", "transport_timeout", "generation_failed", None])
def test_connection_auth_and_memory_style_failures_never_trigger_recovery(monkeypatch, kind):
    calls = extraction_mock(monkeypatch, lambda *_: (_ for _ in ()).throw(local_llm_stream.LocalStreamError("unauthorized / out of memory", kind=kind)))
    adaptive = {}
    value = corpus.extract_paper(paper("A single small abstract with results."), "local", "test-model", adaptive=adaptive)
    assert len(calls) == 1 and value["status"] == "failed"
    assert adaptive["state"]["timeout_recoveries"] == 0


def test_cancellation_after_timeout_prevents_retry_and_keeps_input(monkeypatch):
    event = threading.Event()
    def respond(*_):
        event.set()
        raise local_llm_stream.LocalStreamError("slow", kind="first_response_timeout")
    calls = extraction_mock(monkeypatch, respond)
    text = "A full result. " * 100
    value = corpus.extract_paper(paper(text), "local", "test-model", cancelled=event)
    assert len(calls) == 1 and value["status"] == "cancelled"
    assert value["adaptive"]["timeout_recoveries"] == 0
    coverage(value, text)


def test_successful_uncached_speed_shrinks_later_papers_but_not_cache_or_mock_time(monkeypatch):
    clock = iter([0.0, 600.0])
    monkeypatch.setattr(corpus, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    calls = extraction_mock(monkeypatch)
    adaptive = {}
    text = "x" * 5000
    first = corpus.extract_paper(paper(text), "local", "test-model", adaptive=adaptive)
    assert adaptive["state"]["extraction"]["max_chars"] == 2500
    assert adaptive["state"]["extraction"]["successful_requests"] == 1
    before = deepcopy(adaptive["state"])
    calls.clear()
    corpus.extract_paper(paper(text), "local", "test-model", adaptive=adaptive, checkpoint={"state": first})
    assert not calls and adaptive["state"] == before
    monkeypatch.setattr(corpus, "time", SimpleNamespace(monotonic=lambda: 100.0))
    corpus.extract_paper(paper(text, id="other"), "local", "test-model", adaptive=adaptive)
    assert [len(p["papers"][0]["abstract"]) for p in calls] == [2500, 2500]
    assert adaptive["state"]["extraction"]["successful_requests"] == 1


def test_adaptation_applies_to_remaining_chunks_of_same_paper(monkeypatch):
    ticks = iter([0, 600, 600, 600, 600, 600])
    monkeypatch.setattr(corpus, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    calls = extraction_mock(monkeypatch)
    text = "x" * 10_000
    value = corpus.extract_paper(paper(text), "local", "test-model", adaptive={})
    assert [len(p["papers"][0]["abstract"]) for p in calls] == [5000, 2500, 2500]
    assert value["status"] == "completed"
    coverage(value, text)


def test_policy_state_is_namespace_bound_bounded_and_text_free():
    malicious = {"version": corpus_adaptive.POLICY_VERSION, "cache_namespace": "same", "extra_secret": "password",
        "extraction": {"max_chars": 9999999, "ewma_seconds_per_unit": float("nan")},
        "synthesis": {"max_tokens": -1, "max_items": True}, "timeout_recoveries": -1,
        "events": [{"phase": "extraction", "reason": "timeout_split", "kind": ["private"], "previous_limit": 10**1000, "raw": "secret"},
                   {"phase": ["extraction"], "reason": "timeout_split"}]}
    handle = {"state": malicious}
    policy = corpus_adaptive.AdaptivePolicy(handle, "same", "local")
    policy.persist()
    assert policy.limit("extraction") == 5000 and policy.limit("synthesis") == 2400
    assert "password" not in str(handle) and "private" not in str(handle) and "secret" not in str(handle)
    policy.recovery("extraction", "read_timeout", divisible=True, request_key="x", units=400)
    assert policy.limit("extraction") == 200
    next_model = corpus_adaptive.AdaptivePolicy(handle, "different", "local")
    assert next_model.limit("extraction") == 5000 and next_model.state["timeout_recoveries"] == 0


def summary(payload):
    return {"text": "知見。", "sections": [{"title": "比較", "text": "結果。", "child_ids": [n["child_id"] for n in payload["items"]]}], "warnings": []}


def test_summary_timeout_split_checkpoints_resume_completed_branches(monkeypatch):
    events, stopped = {}, threading.Event()
    def respond(payload, options):
        if len(payload["items"]) > 2:
            raise local_llm_stream.LocalStreamError("slow", kind="total_timeout")
        return summary(payload)
    calls = synthesis_mock(monkeypatch, respond)
    def save(value):
        events[value["request_hash"]] = deepcopy(value)
        if value.get("node"):
            stopped.set()
    adaptive = {}
    with pytest.raises(corpus.CorpusCancelled):
        corpus.synthesize(inputs(4), "local", "test-model", label="全体", adaptive=adaptive,
                          checkpoint={"save": save}, cancelled=stopped)
    original, successful = deepcopy(calls[0]), deepcopy(calls[-1])
    assert any(row.get("record_type") == "split_plan" for row in events.values())
    calls.clear()
    value = corpus.synthesize(inputs(4), "local", "test-model", label="全体", adaptive=adaptive,
        checkpoint={"state": {"nodes": events}, "save": lambda row: events.update({row["request_hash"]: row})})
    assert original not in calls and successful not in calls
    assert value["source_count"] == value["consumed_count"] == 4
    assert value["input_node_count"] == value["consumed_node_count"] == 4
    for row in value["provenance"].values():
        assert all(key in value["provenance"] for key in row["children"])


def test_summary_budget_exhaustion_stops_entire_invocation(monkeypatch):
    calls = synthesis_mock(monkeypatch, lambda *_: (_ for _ in ()).throw(local_llm_stream.LocalStreamError("slow", kind="read_timeout")))
    adaptive = {}
    with pytest.raises(local_llm_stream.LocalStreamError) as error:
        corpus.synthesize(inputs(40), "local", "test-model", label="全体", adaptive=adaptive)
    assert error.value.kind == "timeout_recovery_exhausted"
    assert len(calls) == 4 and adaptive["state"]["timeout_recoveries"] == 3


def test_summary_measured_speed_applies_to_later_batches_without_losing_inputs(monkeypatch):
    ticks = iter([0, 600])
    last = [600]
    def tick():
        return next(ticks, last[0])
    monkeypatch.setattr(corpus, "time", SimpleNamespace(monotonic=tick))
    calls = synthesis_mock(monkeypatch, lambda payload, _: summary(payload))
    adaptive = {}
    value = corpus.synthesize(inputs(24), "local", "test-model", label="全体", adaptive=adaptive)
    assert len(calls[0]["items"]) == 8
    assert max(len(call["items"]) for call in calls[1:]) <= 4
    assert value["consumed_count"] == value["source_count"] == 24
    assert adaptive["state"]["synthesis"]["successful_requests"] == 1
    assert adaptive["state"]["synthesis"]["max_tokens"] == 1200


def test_openai_does_not_use_local_timeout_recovery_or_calibration(monkeypatch):
    calls = extraction_mock(monkeypatch, lambda *_: (_ for _ in ()).throw(local_llm_stream.LocalStreamError("slow", kind="total_timeout")))
    value = corpus.extract_paper(paper("A small scientific result."), "openai", "test-model", adaptive={})
    assert len(calls) == 1 and value["status"] == "failed"
    assert value["adaptive"]["timeout_recoveries"] == 0


def test_learned_soft_cap_does_not_prevent_merge_when_summaries_exceed_input_budget(monkeypatch):
    def respond(payload, _):
        return {"text": "材料条件による相違があり追加実験が必要です。" * 25,
            "sections": [{"title": "研究", "text": "材料条件による相違があり追加実験が必要です。" * 25,
                          "child_ids": [n["child_id"] for n in payload["items"]]}], "warnings": []}
    calls = synthesis_mock(monkeypatch, respond)
    adaptive = {"state": {"version": corpus_adaptive.POLICY_VERSION, "cache_namespace": corpus.cache_namespace("local", "test-model"),
                          "synthesis": {"max_tokens": 320, "max_items": 2}}}
    value = corpus.synthesize(inputs(12), "local", "test-model", label="全体", adaptive=adaptive)
    assert value["status"] == "completed" and value["consumed_count"] == 12
    assert value["input_node_count"] == value["consumed_node_count"] == 12
    assert len(calls) < 50
    assert adaptive["state"]["synthesis"]["max_tokens"] == 320


def test_adaptive_persistence_failure_is_not_misreported_as_llm_failure(monkeypatch):
    extraction_mock(monkeypatch)
    def fail(_):
        raise OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        corpus.extract_paper(paper("A scientific result was recorded."), "local", "test-model", adaptive={"save": fail})
