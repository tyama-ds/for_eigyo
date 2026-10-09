from copy import deepcopy
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from app import corpus_llm as corpus, field_llm, local_llm_stream
from app.connection_settings import ConnectionSettings, settings_context
from test_llm_context import Answer, install_transport, completed, papers_payload


def paper(text, **extra):
    return {"id": "p1", "title": "Steel research", "abstract": text, **extra}


def quoted(text, *, kind="result", statement=None):
    return {"kind": kind, "statement": statement or text, "quote": text}


def extraction_mock(monkeypatch, action=None):
    calls = []
    def generate(payload, schema, instructions, provider, model, **kwargs):
        calls.append(deepcopy(payload))
        assert kwargs["preserve_input"] is True
        assert schema is corpus.Extraction
        text = payload["papers"][0]["abstract"]
        if action:
            output = action(payload, kwargs)
        else:
            output = {"facts": [quoted(text[-min(100, len(text)):])]}
        return output, "local_llm", model
    monkeypatch.setattr(field_llm, "structured_output", generate)
    return calls


def test_all_abstract_characters_and_tail_are_processed_without_omission(monkeypatch):
    calls = extraction_mock(monkeypatch)
    text = "First material. " * 500 + "Final limitation: no improvement was observed."
    snapshots = []
    value = corpus.extract_paper(paper(text), "local", "test-model", checkpoint=snapshots.append)
    assert value["status"] == "completed"
    assert value["covered_chars"] == value["abstract_chars"] == len(text)
    assert "".join(call["papers"][0]["abstract"] for call in calls) == text
    assert any("no improvement" in fact["quote"] for fact in value["facts"])
    assert all(text[fact["source_start"]:fact["source_end"]] == fact["quote"] for fact in value["facts"])
    assert snapshots[-1] == value
    assert not any("中略" in call["papers"][0]["abstract"] for call in calls)


@pytest.mark.parametrize("mode", ["error", "silent_reduction", "output_limit"])
def test_context_pressure_splits_and_eventually_sends_the_entire_abstract(monkeypatch, mode):
    successful = []
    def respond(payload, options):
        text = payload["papers"][0]["abstract"]
        if len(text) > 250:
            if mode == "silent_reduction":
                options["input_context"].update(payload={"bad": "shortened"}, metadata={"reduced": True})
                return {"facts": []}
            raise local_llm_stream.LocalStreamError("too long", kind="token_limit" if mode == "output_limit" else "context_budget")
        successful.append(text)
        return {"facts": [quoted(text[-30:])]}
    calls = extraction_mock(monkeypatch, respond)
    text = "Beginning sentence. " * 60 + "Ending condition was adverse."
    value = corpus.extract_paper(paper(text), "local", "test-model")
    assert value["status"] == "completed"
    assert "".join(successful) == text
    assert value["covered_chars"] == len(text)
    assert len(calls) > len(successful)


def test_unmatched_quotations_are_rejected_but_valid_facts_remain(monkeypatch):
    extraction_mock(monkeypatch, lambda payload, options: {"facts": [quoted("Measured 900 MPa."), quoted("Invented 950 MPa.")]})
    value = corpus.extract_paper(paper("Measured 900 MPa. End."), "local", "test-model")
    assert value["status"] == "failed"
    assert value["covered_chars"] == 0
    assert [fact["quote"] for fact in value["facts"]] == ["Measured 900 MPa."]
    assert any(warning["code"] == "quote_mismatch" for warning in value["warnings"])


def test_numeric_mismatch_keeps_generated_statement_with_warning(monkeypatch):
    extraction_mock(monkeypatch, lambda payload, options: {"facts": [quoted("Measured 900 MPa.", statement="Measured 950 GPa.")]})
    value = corpus.extract_paper(paper("Measured 900 MPa."), "local", "test-model")
    fact = value["facts"][0]
    assert value["status"] == "completed"
    assert fact["statement"] == "Measured 950 GPa."
    assert fact["verification"] == "quote_checked_numeric_warning"
    assert fact["warnings"][0]["code"] == "numeric_mismatch"
    assert {"value": "900", "unit": "MPa"} in fact["numbers"]
    assert not any(number["value"] == "950" for number in fact["numbers"])


@pytest.mark.parametrize("raw", [None, "<think>unfinished", {"facts": "not a list"}, {"facts": [{"kind": "made_up", "quote": "Steel", "statement": "Steel"}]}])
def test_invalid_outputs_never_claim_successful_coverage(monkeypatch, raw):
    extraction_mock(monkeypatch, lambda payload, options: raw)
    value = corpus.extract_paper(paper("Steel results are available."), "local", "test-model")
    assert value["status"] == "failed"
    assert value["covered_chars"] == 0
    assert value["facts"] == []
    assert value["chunks"][0]["error_kind"] == "invalid_output"


def test_empty_facts_distinguish_processed_text_from_grounded_evidence(monkeypatch):
    extraction_mock(monkeypatch, lambda payload, options: {"facts": []})
    text = "The experimental result is reported."
    value = corpus.extract_paper(paper(text), "local", "test-model")
    assert value["covered_chars"] == len(text)
    assert value["status"] == "no_grounded_facts"
    assert value["grounded_fact_count"] == 0
    assert {warning["code"] for warning in value["warnings"]} == {"no_facts", "no_grounded_facts"}


def test_retry_after_no_grounded_facts_calls_model_again_and_can_recover(monkeypatch):
    extraction_mock(monkeypatch, lambda payload, options: {"facts": []})
    text = "The experimental result is reported."
    first = corpus.extract_paper(paper(text), "local", "test-model")
    calls = extraction_mock(monkeypatch)
    retried = corpus.extract_paper(paper(text), "local", "test-model", checkpoint={"state": first})
    assert first["status"] == "no_grounded_facts"
    assert len(calls) == 1
    assert retried["status"] == "completed" and retried["facts"]


def test_missing_abstract_never_calls_llm(monkeypatch):
    calls = extraction_mock(monkeypatch)
    value = corpus.extract_paper(paper("   "), "local", "test-model")
    assert value["status"] == "missing_abstract" and not calls
    assert value["covered_chars"] == 0


def test_cancel_and_resume_reuse_completed_chunks_without_losing_tail(monkeypatch):
    monkeypatch.setattr(corpus, "INITIAL_CHUNK_CHARS", 180)
    calls = extraction_mock(monkeypatch)
    event, saved = threading.Event(), []
    def save(value):
        saved.append(value)
        event.set()
    text = "A scientific measurement is described. " * 20
    first = corpus.extract_paper(paper(text), "local", "test-model", checkpoint=save, cancelled=event)
    assert first["status"] == "cancelled" and len(calls) == 1
    calls.clear()
    second = corpus.extract_paper(paper(text), "local", "test-model", checkpoint={"state": first})
    assert second["status"] == "completed"
    assert second["chunks"][0]["reused"]
    assert "".join(call["papers"][0]["abstract"] for call in calls) == text[180:]
    assert second["covered_chars"] == len(text)


def test_checkpoint_cannot_be_reused_for_changed_abstract_or_endpoint(monkeypatch):
    calls = extraction_mock(monkeypatch)
    value = corpus.extract_paper(paper("Original abstract result."), "local", "test-model")
    calls.clear()
    corpus.extract_paper(paper("Different abstract result."), "local", "test-model", checkpoint={"state": value})
    assert len(calls) == 1
    calls.clear()
    with settings_context(ConnectionSettings(local={"url": "http://127.0.0.1:1234/v1", "backend": "openai_compatible"})):
        corpus.extract_paper(paper("Original abstract result."), "local", "test-model", checkpoint={"state": value})
    assert len(calls) == 1


def test_checkpoint_storage_errors_are_not_misreported_as_model_failure(monkeypatch):
    extraction_mock(monkeypatch)
    def fail(snapshot):
        raise OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        corpus.extract_paper(paper("Original abstract result."), "local", "test-model", checkpoint=fail)


def test_scope_reaches_model_and_invalidates_resume(monkeypatch):
    calls = extraction_mock(monkeypatch)
    p = paper("Original abstract result.", is_demo=True, sampled=True, source_warnings=["bounded retrieval"])
    value = corpus.extract_paper(p, "local", "test-model")
    assert calls[0]["scope"] == {"is_demo": True, "sampled": True, "source_warnings": ["bounded retrieval"]}
    calls.clear()
    corpus.extract_paper({**p, "is_demo": False}, "local", "test-model", checkpoint={"state": value})
    assert len(calls) == 1


def test_namespace_ignores_credentials_but_distinguishes_endpoint_and_backend():
    with settings_context(ConnectionSettings(local={"api_key": "first", "model": "test-model"})):
        first = corpus.cache_namespace("local", "test-model")
    with settings_context(ConnectionSettings(local={"api_key": "second", "model": "test-model"})):
        assert corpus.cache_namespace("local", "test-model") == first
    with settings_context(ConnectionSettings(local={"url": "http://127.0.0.1:5555", "model": "test-model"})):
        assert corpus.cache_namespace("local", "test-model") != first
    assert "test-model" not in first


def test_resolve_model_uses_explicit_or_browser_choice_without_per_paper_discovery(monkeypatch):
    monkeypatch.setattr(field_llm, "local_status", lambda: (_ for _ in ()).throw(AssertionError("unexpected network")))
    assert corpus.resolve_model("local", "test-model") == "test-model"
    with settings_context(ConnectionSettings(local={"model": "browser-model"})):
        assert corpus.resolve_model("local") == "browser-model"
    with settings_context(ConnectionSettings(openai={"model": "gpt-test", "api_key": "example"})):
        assert corpus.resolve_model("openai") == "gpt-test"
        with pytest.raises(ValueError):
            corpus.resolve_model("openai", "different")


def test_resolve_default_local_model_is_explicitly_discovered(monkeypatch):
    monkeypatch.setattr(field_llm, "local_status", lambda: {"available": True, "default_model": "loaded-model"})
    assert corpus.resolve_model("local") == "loaded-model"
    with pytest.raises(ValueError):
        corpus.resolve_model("none")


def inputs(count=20):
    return [{"paper_id": f"paper-{index}", "title": "Experimental study", "year": 2021+index%4, "topic_id": "steel",
             "extraction": {"facts": [{"id": f"fact-{index}", "kind": "negative_result" if index == count-1 else "result",
                                       "statement": "No improvement." if index == count-1 else "An effect was measured.",
                                       "quote": "No improvement." if index == count-1 else "An effect was measured.", "warnings": []}]}}
            for index in range(count)]


def synthesis_mock(monkeypatch, action=None):
    calls = []
    def generate(payload, schema, instructions, provider, model, **kwargs):
        calls.append(deepcopy(payload))
        assert kwargs["preserve_input"] is True
        assert schema is corpus.Summary
        if action:
            value = action(payload, kwargs)
        else:
            value = {"text": "研究対象と結果を比較しました。", "sections": [{"title": "研究の推移", "text": "条件によって結果は異なります。",
                     "child_ids": [item["child_id"] for item in payload["items"]]}], "warnings": []}
        return value, "local_llm", model
    monkeypatch.setattr(field_llm, "structured_output", generate)
    return calls


def test_hierarchical_synthesis_consumes_all_inputs_and_retains_minority_negative(monkeypatch):
    calls = synthesis_mock(monkeypatch)
    value = corpus.synthesize(inputs(40), "local", "test-model", label="全期間")
    assert value["source_count"] == value["input_count"] == value["consumed_count"] == 40
    assert value["input_node_count"] == value["consumed_node_count"] == 40
    assert value["hierarchy_levels"] > 1
    assert value["negative_results"][0]["paper_ids"] == ["paper-39"]
    assert value["negative_results"][0]["quote"] == "No improvement."
    assert len(value["sections"][0]["paper_ids"]) == 40
    assert corpus.COMPRESSION_WARNING in value["warnings"]
    assert max(len(call["items"]) for call in calls) <= corpus.SUMMARY_BATCH_ITEMS
    assert all("paper-39" not in json.dumps(call) for call in calls)
    assert any('"year": 2021' in item["text"] for call in calls for item in call["items"])
    for entry in value["provenance"].values():
        assert all(child in value["provenance"] for child in entry["children"])


def test_summary_keeps_prose_but_flags_unverified_references_and_numbers(monkeypatch):
    synthesis_mock(monkeypatch, lambda payload, options: {"text": "Strength reached 999 GPa.", "sections": [
        {"title": "結果", "text": "Strength reached 999 GPa.", "child_ids": ["invented-child"]}], "warnings": []})
    value = corpus.synthesize(inputs(1), "local", "test-model", label="結果")
    assert value["text"] == "Strength reached 999 GPa."
    assert value["sections"][0]["paper_ids"] == []
    codes = {warning.get("code") for warning in value["warnings"] if isinstance(warning, dict)}
    assert {"numeric_mismatch", "unverified_references"} <= codes


def test_gemma_inline_id_fixture_recovers_exact_evidence_without_numeric_id_noise(monkeypatch):
    # Offline regression from the real synthetic Gemma smoke response. The
    # model put exact child IDs in prose while every child_ids array was empty.
    original_ids = ["n-b11938588d2b6e7ca2c1d5df", "n-d62f24f5cbc311ee7a420732", "n-a9a6b8fb0372435d4859b2d4",
                    "n-67abc670493858b526f65bb7", "n-6832d36498902cf75f4bb087"]
    fixture = {"text": "本合成データは、2024年の鋼板クーロンの熱処理に関する研究を模倣したものです。研究課題として、熱処理後の引張強度の変化が挙げられます (n-b11938588d2b6e7ca2c1d5df)。実験方法としては、熱処理後の引張強度測定が行われました (n-d62f24f5cbc311ee7a420732)。結果として、強度が500MPaから600MPaに増加したことが示されています (n-a9a6b8fb0372435d4859b2d4)。ただし、サンプルが1つのみであるため、結果の再現性が課題です (n-67abc670493858b526f65bb7, n-6832d36498902cf75f4bb087)。",
        "sections": [
            {"title": "研究課題と方法", "text": "本合成データは鋼板クーロンの熱処理に焦点を当てています (n-b11938588d2b6e7ca2c1d5df)。引張強度測定が用いられ、その結果が報告されています (n-d62f24f5cbc311ee7a420732)。本データは、その一例として、特定の条件下での強度増加を示唆しています (n-a9a6b8fb0372435d4859b2d4)。", "child_ids": []},
            {"title": "結果と限界", "text": "本データはサンプル数が1つのみであるため、結果の一般化には限界があります (n-67abc670493858b526f65bb7, n-6832d36498902cf75f4bb087)。", "child_ids": []},
            {"title": "将来の研究展望", "text": "異なる熱処理温度や時間で複数のサンプルを作成し、引張強度を測定することで、より詳細なデータを得ることができます。", "child_ids": []}],
        "warnings": ["本データは合成されたものであり、実際の研究結果ではありません。"]}
    facts = [
        {"id": "f-1ae8fb83784f0cbe5d07e2a8", **quoted("heat treatment of a steel coupon", kind="material", statement="steel coupon"), "warnings": []},
        {"id": "f-e77868a638c4292c8c23ca68", **quoted("We measured tensile strength after heat treatment of a steel coupon.", kind="method", statement="tensile strength was measured"), "warnings": []},
        {"id": "f-b6217442dee9f24bdc9c6992", **quoted("The strength increased from 500 MPa to 600 MPa.", statement="strength increased from 500 MPa to 600 MPa"), "warnings": []},
        {"id": "f-c3d71916d43b75a2374f9169", **quoted("Only one sample was measured, so the result needs replication.", kind="limitation", statement="only one sample was measured"), "warnings": []},
        {"id": "f-08885a8532447ea3162d1d37", **quoted("so the result needs replication.", kind="limitation", statement="result needs replication"), "warnings": []}]
    generated = []
    def respond(payload, options):
        encoded = json.dumps(fixture, ensure_ascii=False)
        for original, node in zip(original_ids, payload["items"]):
            encoded = encoded.replace(original, node["child_id"])
        output = json.loads(encoded)
        generated.append(output)
        return output
    synthesis_mock(monkeypatch, respond)
    value = corpus.synthesize([{"paper_id": "synthetic-corpus-smoke", "year": 2024, "is_demo": True,
                                "extraction": {"facts": facts}}], "local", "test-model", label="2024年")
    assert value["raw_text"] == generated[0]["text"]
    assert "n-" not in value["text"] and "〔根拠参照〕" in value["text"]
    assert value["text_paper_ids"] == ["synthetic-corpus-smoke"]
    assert value["sections"][0]["raw_text"] == generated[0]["sections"][0]["text"]
    assert value["sections"][0]["paper_ids"] == value["sections"][1]["paper_ids"] == ["synthetic-corpus-smoke"]
    assert len(value["sections"][0]["recovered_inline_child_ids"]) == 3
    assert not any(warning["code"] in {"numeric_mismatch", "unverified_references"} for warning in value["sections"][0]["warnings"])
    # The genuine existing word-one vs digit-1 mismatch remains visible. No
    # identifier digits become measured quantities, and no semantic mistranslation
    # such as the model's "鋼板クーロン" is claimed to be scientifically verified.
    mismatches = [warning for warning in value["warnings"] if isinstance(warning, dict) and warning.get("code") == "numeric_mismatch"]
    assert mismatches and all(warning["unmatched_numbers"] == ["1"] for warning in mismatches)
    assert "鋼板クーロン" in value["text"]
    assert value["sections"][2]["paper_ids"] == []


def test_unknown_inline_ids_remain_unverified_not_numeric_quantities(monkeypatch):
    unknown = "n-012345678901234567890123"
    def respond(payload, options):
        text = f"Strength is 999 GPa ({unknown})."
        return {"text": text, "sections": [{"title": "結果", "text": text, "child_ids": []}], "warnings": []}
    synthesis_mock(monkeypatch, respond)
    value = corpus.synthesize(inputs(1), "local", "test-model", label="全体")
    section = value["sections"][0]
    assert section["child_ids"] == section["paper_ids"] == []
    assert section["unverified_child_ids"] == [unknown]
    assert "〔未照合の参照〕" in section["text"] and unknown in section["raw_text"]
    numeric = [warning for warning in section["warnings"] if warning["code"] == "numeric_mismatch"]
    assert numeric[0]["unmatched_numbers"] == ["999"]
    assert numeric[0]["unmatched_quantities"] == [{"value": "999", "unit": "GPa"}]
    assert not section["recovered_inline_child_ids"]


def test_inline_reference_recovery_uses_exact_case_and_no_prefix_or_fuzzy_match(monkeypatch):
    def respond(payload, options):
        known = payload["items"][0]["child_id"]
        different = known.upper()
        text = f"Reference ({different}), embedded value z{known}suffix."
        return {"text": text, "sections": [{"title": "結果", "text": text, "child_ids": []}], "warnings": []}
    calls = synthesis_mock(monkeypatch, respond)
    value = corpus.synthesize(inputs(1), "local", "test-model", label="全体")
    section = value["sections"][0]
    assert not section["child_ids"] and not section["recovered_inline_child_ids"]
    assert section["unverified_child_ids"] == [calls[0]["items"][0]["child_id"].upper()]


def test_recovered_inline_references_survive_recursive_summary_and_checkpoint(monkeypatch):
    def respond(payload, options):
        text = "研究の比較 (" + ", ".join(item["child_id"] for item in payload["items"]) + ")。"
        return {"text": text, "sections": [{"title": "比較", "text": text, "child_ids": []}], "warnings": []}
    calls = synthesis_mock(monkeypatch, respond)
    events = {}
    value = corpus.synthesize(inputs(20), "local", "test-model", label="全体",
                              checkpoint={"save": lambda event: events.update({event["request_hash"]: event})})
    assert value["sections"][0]["paper_ids"] == [f"paper-{i}" for i in range(20)]
    assert value["text_paper_ids"] == value["paper_ids"]
    assert not any(isinstance(warning, dict) and warning.get("code") == "numeric_mismatch" for warning in value["warnings"])
    for call in calls:
        assert all(not corpus._OPAQUE_CHILD_ID.search(item["text"]) for item in call["items"])
    calls.clear()
    reopened = corpus.synthesize(inputs(20), "local", "test-model", label="全体", checkpoint={"state": {"nodes": events}})
    assert not calls
    assert reopened["sections"][0]["paper_ids"] == value["sections"][0]["paper_ids"]
    assert reopened["raw_text"] == value["raw_text"]


def test_summary_resumes_completed_nodes_from_incremental_checkpoints(monkeypatch):
    calls = synthesis_mock(monkeypatch)
    events, event = {}, threading.Event()
    def save(value):
        events[value["request_hash"]] = value
        event.set()
    with pytest.raises(corpus.CorpusCancelled):
        corpus.synthesize(inputs(20), "local", "test-model", label="全期間", checkpoint={"save": save}, cancelled=event)
    assert len(calls) == len(events) == 1
    previous_call = calls[0]
    calls.clear()
    value = corpus.synthesize(inputs(20), "local", "test-model", label="全期間", checkpoint={"state": {"nodes": events}})
    assert value["source_count"] == 20
    assert previous_call not in calls
    assert all(entry["kind"] == "summary_node" and len(entry["children"]) <= 8 for entry in events.values())
    assert value["input_node_count"] == value["consumed_node_count"] == 20


def test_previous_summaries_carry_year_scope_negative_evidence_and_source_identity(monkeypatch):
    calls = synthesis_mock(monkeypatch)
    first = corpus.synthesize(inputs(3), "local", "test-model", label="2021年")
    calls.clear()
    value = corpus.synthesize([{**first, "label": "2021年", "year": 2021, "is_demo": True, "sampled": True}],
                              "local", "test-model", label="全期間")
    assert value["source_count"] == 3
    assert len(value["negative_results"]) == 1
    assert '"year": 2021' in calls[0]["items"][0]["text"]
    assert '"is_demo": true' in calls[0]["items"][0]["text"]


def test_summary_invalid_json_is_not_replaced_by_fake_generated_prose(monkeypatch):
    synthesis_mock(monkeypatch, lambda payload, options: {"text": "incomplete"})
    with pytest.raises(ValueError):
        corpus.synthesize(inputs(3), "local", "test-model", label="全期間")


def test_summary_missing_input_is_consumed_as_missing_evidence(monkeypatch):
    synthesis_mock(monkeypatch)
    value = corpus.synthesize([{"paper_id": "missing", "extraction": {"facts": [], "status": "failed"}}, *inputs(2)],
                              "local", "test-model", label="全期間")
    assert value["source_count"] == value["consumed_count"] == 3
    assert value["missing_evidence_count"] == 1
    assert value["evidence_source_count"] == 2
    again = corpus.synthesize([{**value, "label": "2021年"}], "local", "test-model", label="全体")
    assert again["missing_evidence_count"] == 1
    assert again["evidence_source_count"] == 2


def test_summary_context_overflow_splits_without_discarding_any_item(monkeypatch):
    successful_inputs = set()
    def respond(payload, options):
        if sum(len(item["text"]) for item in payload["items"]) > 1000:
            raise local_llm_stream.LocalStreamError("too long", kind="context_budget")
        successful_inputs.update(item["child_id"] for item in payload["items"])
        return {"text": "複数の研究を比較しました。", "sections": [{"title": "比較", "text": "結果は条件に依存します。",
                 "child_ids": [item["child_id"] for item in payload["items"]]}], "warnings": []}
    synthesis_mock(monkeypatch, respond)
    value = corpus.synthesize(inputs(16), "local", "test-model", label="期間比較")
    assert value["source_count"] == value["input_node_count"] == value["consumed_node_count"] == 16
    assert value["paper_ids"] == [f"paper-{i}" for i in range(16)]
    assert len(successful_inputs) >= 16


def test_single_oversized_summary_input_keeps_both_halves_and_traceability(monkeypatch):
    leaf_text = []
    def respond(payload, options):
        if sum(len(item["text"]) for item in payload["items"]) > 800:
            raise local_llm_stream.LocalStreamError("too long", kind="context_budget")
        leaf_text.extend(item["text"] for item in payload["items"])
        return {"text": "条件と結果。", "sections": [{"title": "知見", "text": "内容の比較。",
                "child_ids": [item["child_id"] for item in payload["items"]]}], "warnings": []}
    synthesis_mock(monkeypatch, respond)
    records = inputs(1)
    records[0]["extraction"]["facts"][0]["quote"] = "first sentinel " + "experiment. " * 180 + "last sentinel"
    value = corpus.synthesize(records, "local", "test-model", label="長い根拠")
    assert any("first sentinel" in text for text in leaf_text)
    assert any("last sentinel" in text for text in leaf_text)
    assert value["consumed_node_count"] == 1
    for entry in value["provenance"].values():
        assert all(child in value["provenance"] for child in entry["children"])


def test_nonshrinking_context_reduction_stops_instead_of_recursing_indefinitely(monkeypatch):
    def respond(payload, options):
        if len(payload["items"]) > 1:
            raise local_llm_stream.LocalStreamError("too long", kind="context_budget")
        return {"text": "長い回答。" * 200, "sections": [{"title": "知見", "text": "長い回答。" * 200,
                "child_ids": [item["child_id"] for item in payload["items"]]}], "warnings": []}
    calls = synthesis_mock(monkeypatch, respond)
    with pytest.raises(local_llm_stream.LocalStreamError) as error:
        corpus.synthesize(inputs(2), "local", "test-model", label="全体")
    assert error.value.kind == "context_budget"
    assert len(calls) == 3


def test_twenty_thousand_inputs_are_all_consumed_offline(monkeypatch):
    calls = synthesis_mock(monkeypatch)
    value = corpus.synthesize(inputs(20000), "local", "test-model", label="20,000件のオフライン検証")
    assert value["source_count"] == value["consumed_count"] == 20000
    assert value["consumed_node_count"] == value["input_node_count"] == 20000
    assert value["paper_ids"][0] == "paper-0" and value["paper_ids"][-1] == "paper-19999"
    assert value["negative_results"][0]["paper_ids"] == ["paper-19999"]
    assert len(calls) < 5000


def test_measured_counts_are_separate_nodes_and_numeric_evidence_without_inflating_sources(monkeypatch):
    def respond(payload, options):
        return {"text": "収録集合の対象は123件、抽出済みは100件です。", "sections": [
            {"title": "処理範囲", "text": "対象123件、抽出済み100件、未処理23件。",
             "child_ids": [item["child_id"] for item in payload["items"]]}], "warnings": []}
    calls = synthesis_mock(monkeypatch, respond)
    value = corpus.synthesize(inputs(2), "local", "test-model", label="全体", metrics={
        "total": 123, "completed": 100, "pending": 23, "annual": [{"year": 2021, "total": 123}]})
    assert value["source_count"] == value["input_count"] == value["consumed_count"] == 2
    assert value["metrics_node_count"] == 2 and value["paper_node_count"] == 2
    assert value["input_node_count"] == value["consumed_node_count"] == 4
    assert not any(isinstance(warning, dict) and warning.get("code") == "numeric_mismatch" for warning in value["warnings"])
    assert any('"kind": "computed_metrics"' in item["text"] for call in calls for item in call["items"])


def test_strict_gateway_rejects_any_input_reduction_before_generation(monkeypatch):
    calls = []
    install_transport(monkeypatch, lambda request: calls.append(request))
    context = {}
    with settings_context(ConnectionSettings(local={"backend": "openai_compatible", "url": "http://127.0.0.1:1234/v1", "context_window": 2048})):
        with pytest.raises(local_llm_stream.LocalStreamError) as error:
            field_llm.structured_output(papers_payload(), Answer, "Instructions", "local", input_context=context, preserve_input=True)
    assert error.value.kind == "context_budget"
    assert not calls
    assert context["payload"] == papers_payload()
    assert context["metadata"]["reduction_rejected"]


def test_strict_gateway_does_not_retry_server_overflow_with_a_smaller_payload(monkeypatch):
    calls = []
    def fail(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"message": "maximum context length is 1024 tokens"}})
    install_transport(monkeypatch, fail)
    with settings_context(ConnectionSettings(local={"backend": "openai_compatible", "url": "http://127.0.0.1:1234/v1"})):
        with pytest.raises(local_llm_stream.LocalStreamError) as error:
            field_llm.structured_output({"papers": []}, Answer, "Instructions", "local", preserve_input=True)
    assert error.value.kind == "context_length"
    assert len(calls) == 1


@pytest.mark.parametrize(("status", "reason", "text", "error_kind"), [
    ("incomplete", "max_output_tokens", '{"answer":', "token_limit"),
    ("incomplete", "content_filter", '{"answer":', "incomplete"),
    ("completed", None, '{"answer":', "malformed_json"),
    ("completed", None, '{"answer":"ok"}', None),
])
def test_strict_openai_checks_completion_before_parsing_truncated_json(monkeypatch, status, reason, text, error_kind):
    calls = []
    response = SimpleNamespace(status=status, incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        error=None, output_text=text, output=[SimpleNamespace(type="message", role="assistant", status="completed",
                                                              content=[SimpleNamespace(type="output_text")])])
    def create(**kwargs):
        calls.append(kwargs)
        return response
    @contextmanager
    def client(**kwargs):
        yield SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(field_llm, "openai_client", client)
    with settings_context(ConnectionSettings(openai={"model": "gpt-test", "api_key": "example"})):
        if error_kind:
            with pytest.raises(local_llm_stream.LocalStreamError) as error:
                field_llm.structured_output({}, Answer, "Instructions", "openai", preserve_input=True)
            assert error.value.kind == error_kind
        else:
            parsed, mode, chosen = field_llm.structured_output({}, Answer, "Instructions", "openai", preserve_input=True)
            assert parsed.answer == "ok" and mode == "openai" and chosen == "gpt-test"
    assert len(calls) == 1 and calls[0]["store"] is False
    assert calls[0]["text"]["format"]["strict"] is True
