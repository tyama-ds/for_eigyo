"""Ground local selections in authoritative source spans, without transcription."""
from copy import deepcopy
import hashlib
import json

import pytest

from app import foresight_llm as module


def paper(identifier="p1", abstract="We measured tensile strength of 900 MPa. Durability did not improve."):
    return {"id": identifier, "title": "Strength study", "year": 2025, "abstract": abstract}


def selection(identifier, **changes):
    return {"excerpt_id": identifier, "kind": "result",
            "stance": "support", "attribution": "own_result", **changes}


def restore(papers, choices):
    _, schema, catalog = module.local_extraction_input(papers)
    facts, spans = module.restore_local_facts({"facts": choices}, schema, catalog)
    return module.validate_facts(facts, papers, source_spans=spans)


def test_selection_restores_exact_unicode_crlf_source_without_mutating_inputs():
    papers = [paper(abstract="  引張強度は900 MPaだった。\r\nDurability did not improve.\r\n ")]
    original = deepcopy(papers)
    payload, schema, catalog = module.local_extraction_input(papers)
    identifier = payload["papers"][0]["excerpts"][0]["id"]
    selected = {"facts": [selection(identifier)]}
    previous = deepcopy(selected)
    facts, spans = module.restore_local_facts(selected, schema, catalog)
    validated = module.validate_facts(facts, papers, source_spans=spans)
    value = validated[0]
    assert value["quote"] == catalog[identifier]["quote"] == payload["papers"][0]["excerpts"][0]["text"]
    assert "\r\n" in value["quote"] and "did not improve" in value["quote"]
    assert papers[0]["abstract"][value["start"]:value["end"]] == value["quote"]
    assert value["source_hash"] == hashlib.sha256(papers[0]["abstract"].encode()).hexdigest()
    assert value["semantic_validation"] == "not_human_verified"
    assert papers == original and selected == previous


@pytest.mark.parametrize("changes", [
    {"excerpt_id": "excerpt-9999"}, {"excerpt_id": None},
    {"paper_id": "another-paper"}, {"quote": "An invented quotation."},
    {"statement": "引張強度は1000 MPaになった。"},
    {"kind": "invented-kind"}, {"stance": "certain"},
])
def test_unknown_ids_and_source_overrides_are_rejected(changes):
    papers = [paper()]
    _, schema, catalog = module.local_extraction_input(papers)
    identifier = next(iter(catalog))
    with pytest.raises(RuntimeError):
        module.restore_local_facts({"facts": [selection(identifier, **changes)]}, schema, catalog)


def test_stale_id_outside_current_selection_schema_is_rejected():
    _, _, old_catalog = module.local_extraction_input([paper(), paper("p2")])
    _, schema, current_catalog = module.local_extraction_input([paper()])
    stale = next(identifier for identifier in old_catalog if identifier not in current_catalog)
    with pytest.raises(RuntimeError):
        module.restore_local_facts({"facts": [selection(stale)]}, schema, current_catalog)


def test_seven_selections_fail_instead_of_silently_truncating():
    _, schema, catalog = module.local_extraction_input([paper()])
    selected = {"facts": [selection(next(iter(catalog))) for _ in range(7)]}
    with pytest.raises(RuntimeError):
        module.restore_local_facts(selected, schema, catalog)
    assert len(selected["facts"]) == 7


@pytest.mark.parametrize("statement", ["1000 MPaと報告された。", "900 GPaと報告された。", "-900 MPaと報告された。"])
def test_id_grounding_reports_number_sign_or_unit_mismatch_without_hiding_statement(statement):
    papers = [paper()]
    _, schema, catalog = module.local_extraction_input(papers)
    facts, spans = module.restore_local_facts({"facts": [selection(next(iter(catalog)))]}, schema, catalog)
    facts["facts"][0]["statement"] = statement
    validated = module.validate_facts(facts, papers, source_spans=spans)
    assert validated[0]["statement"] == statement
    assert validated[0]["verification"] == "quote_checked_numeric_warning"
    assert validated[0]["validation"]["status"] == "warning"
    assert validated[0]["validation"]["warnings"][0]["location"] == "statement"


def test_identical_text_in_different_papers_keeps_selected_paper_identity():
    papers = [paper("first"), paper("second")]
    _, _, catalog = module.local_extraction_input(papers)
    second = next(identifier for identifier, source in catalog.items() if source["paper_id"] == "second")
    facts = restore(papers, [selection(second)])
    assert [fact["paper_id"] for fact in facts] == ["second"]


def test_second_identical_span_within_one_paper_uses_authoritative_offset():
    repeated = "The material " + "x" * 850 + " did not improve fatigue durability."
    # A natural boundary ends each <1000-character excerpt after the same block.
    papers = [paper(abstract=repeated + "\r\n" + repeated)]
    payload, _, catalog = module.local_extraction_input(papers)
    spans = payload["papers"][0]["excerpts"]
    assert len(spans) == 2 and spans[0]["text"] == spans[1]["text"]
    identifier = spans[1]["id"]
    values = restore(papers, [selection(identifier, stance="counter")])
    assert values[0]["start"] == catalog[identifier]["start"] > papers[0]["abstract"].index(values[0]["quote"])
    assert values[0]["end"] == catalog[identifier]["end"]


def test_long_unbroken_source_is_bounded_without_rewriting_or_400_character_limit():
    papers = [paper(abstract="材" * 2400)]
    payload, _, catalog = module.local_extraction_input(papers)
    texts = [item["text"] for item in payload["papers"][0]["excerpts"]]
    assert "".join(texts) == papers[0]["abstract"]
    assert all(12 <= len(text) <= 1000 for text in texts)
    identifier = next(key for key, source in catalog.items() if len(source["quote"]) == 1000)
    facts = restore(papers, [selection(identifier, stance="neutral")])
    assert len(facts[0]["quote"]) == 1000


def test_selection_input_preserves_twelve_paper_bound_and_truncation_metadata():
    full_abstract = "Observed microstructural response under tensile loading. " + "材" * 2450
    originals = [paper(f"p{index}", full_abstract) for index in range(14)]
    assessment = {"papers": originals, "meta": {"is_demo": False}}
    candidate = {"paper_ids": [item["id"] for item in originals]}
    bounded = module.paper_payload(assessment, candidate)
    payload, _, catalog = module.local_extraction_input(bounded)
    assert len(bounded) == len(payload["papers"]) == 12
    assert all(item["abstract"] == full_abstract[:2400] and item["abstract_truncated"] for item in bounded)
    assert all(row["abstract_truncated"] for row in payload["papers"])
    assert {source["paper_id"] for source in catalog.values()} == {item["id"] for item in bounded}
    by_id = {item["id"]: item for item in bounded}
    for source in catalog.values():
        assert 0 <= source["start"] < source["end"] <= 2400
        assert source["quote"] == by_id[source["paper_id"]]["abstract"][source["start"]:source["end"]]
    assert all(item["abstract"] == full_abstract for item in originals)


def test_duplicate_selection_deduplicates_without_changing_source():
    papers = [paper()]
    _, _, catalog = module.local_extraction_input(papers)
    choice = selection(next(iter(catalog)))
    one = restore(papers, [choice])
    duplicated = restore(papers, [choice, deepcopy(choice)])
    assert duplicated == one


def test_no_selected_facts_is_valid_but_no_usable_source_fails():
    papers = [paper()]
    assert restore(papers, []) == []
    with pytest.raises(ValueError):
        module.local_extraction_input([paper(abstract="  \r\n ")])


def test_negation_stays_verbatim_and_application_label_is_not_a_scientific_claim():
    papers = [paper(abstract="Treatment did not improve fatigue durability.")]
    _, _, catalog = module.local_extraction_input(papers)
    values = restore(papers, [selection(next(iter(catalog)))])
    # ID/source and number checks do not constitute an entailment classifier.
    assert "did not improve" in values[0]["quote"]
    assert values[0]["semantic_validation"] == "not_human_verified"
    assert "原文抜粋" in values[0]["statement"] and "専門家" in values[0]["statement"]


@pytest.mark.parametrize("span", [{"start": -1, "end": 12}, {"start": 1, "end": 40}, {"start": 0, "end": 99999}])
def test_mismatched_authoritative_offsets_are_rejected(span):
    papers = [paper()]
    _, schema, catalog = module.local_extraction_input(papers)
    facts, spans = module.restore_local_facts({"facts": [selection(next(iter(catalog)))]}, schema, catalog)
    spans[next(iter(spans))] = span
    with pytest.raises(RuntimeError):
        module.validate_facts(facts, papers, source_spans=spans)


def critique_fixture(abstract=None):
    papers = [paper()] if abstract is None else [paper(abstract=abstract)]
    _, _, catalog = module.local_extraction_input(papers)
    facts = restore(papers, [selection(next(iter(catalog)))])
    raw = {kind: {"text": "選択された原文を確認し、追加検証の必要性を検討する。", "fact_ids": [facts[0]["id"]]}
           for kind in ("support", "counter", "outlook", "next_steps")}
    return facts, module.local_critique_schema(facts), raw


def test_local_critique_valid_blocks_restore_canonical_sections_and_preserve_inputs():
    facts, schema, raw = critique_fixture()
    original = deepcopy(raw)
    canonical = module.restore_local_critique(raw, schema)
    validated = module.validate_critique(canonical, facts, {"growth": {"score": 42}}, "local_llm", "test")
    assert [section["kind"] for section in validated["sections"]] == ["support", "counter", "outlook", "next_steps"]
    assert all(section["title"] and section["text"] == original[section["kind"]]["text"] for section in validated["sections"])
    assert all(section["evidence_ids"] == [facts[0]["paper_id"]] for section in validated["sections"])
    assert raw == original


def test_local_critique_rejects_unknown_fact_ids():
    _, schema, raw = critique_fixture()
    raw["support"]["fact_ids"] = ["fact-not-supplied"]
    with pytest.raises(RuntimeError):
        module.restore_local_critique(raw, schema)


@pytest.mark.parametrize("text", ["成功率99%です。", "900 GPaです。", "-900 MPaです。", "９００ MPaです。"])
def test_local_critique_preserves_unsupported_number_sign_or_unit_with_warning(text):
    facts, schema, raw = critique_fixture()
    raw["outlook"]["text"] = text
    canonical = module.restore_local_critique(raw, schema)
    validated = module.validate_critique(canonical, facts, {}, "local_llm", "test")
    outlook = next(section for section in validated["sections"] if section["kind"] == "outlook")
    assert outlook["text"] == text
    assert outlook["validation"]["status"] == validated["validation"]["status"] == "warning"
    assert any(warning["location"] == "sections/2/text" for warning in validated["validation"]["warnings"])


def test_generation_schema_avoids_regex_while_python_flags_unsupported_quantities():
    facts, schema, raw = critique_fixture()
    wire_schema = schema.model_json_schema()
    assert '"pattern":' not in json.dumps(wire_schema)
    assert module.restore_local_critique(raw, schema)["sections"]
    for text in ("成功率99%です。", "900 GPaを達成した。"):
        raw["support"]["text"] = text
        canonical = module.restore_local_critique(raw, schema)
        validated = module.validate_critique(canonical, facts, {}, "local_llm", "test")
        assert validated["sections"][0]["text"] == text
        assert validated["sections"][0]["validation"]["status"] == "warning"
        assert validated["validation"]["warnings"]


def test_local_prompt_prefers_qualitative_prose_and_permits_only_grounded_exact_copy():
    prompt = module.LOCAL_CRITIQUE_PROMPT
    assert "1/3/5-year" not in prompt
    assert "Use only exact numeric tokens" not in prompt
    assert "Prefer qualitative prose" in prompt
    assert "copy a material designation or numerical token exactly as it appears in the cited excerpt" in prompt
    assert "including sign and unit" in prompt
    assert "conditional outlooks in words only" in prompt
    assert "Never invent quantities or forecasts" in prompt
    assert "Do not write any numerical claims" not in prompt
    # The nonlocal route retains its original numerical/horizon instructions.
    assert "1/3/5-year" in module.CRITIQUE_PROMPT
    assert "Use only exact numeric tokens" in module.CRITIQUE_PROMPT


@pytest.mark.parametrize("text", ["17-4PH材の試験結果が報告された。", "900 MPaの引張強度が報告された。", "17-4PH材で900 MPaが報告された。"])
def test_grounded_material_designations_and_measurements_are_permitted(text):
    facts, schema, raw = critique_fixture("The 17-4PH stainless steel achieved tensile strength of 900 MPa. Fatigue durability remains uncertain.")
    raw["support"]["text"] = text
    canonical = module.restore_local_critique(raw, schema)
    result = module.validate_critique(canonical, facts, {}, "local_llm", "test")
    support = next(section for section in result["sections"] if section["kind"] == "support")
    assert support["text"] == text
    assert support["fact_ids"] == [facts[0]["id"]]
    assert support["evidence_ids"] == [facts[0]["paper_id"]]
    validated_pass = {"status": "passed", "warnings": []}
    assert support["validation"] == validated_pass
    assert result["validation"] == validated_pass


def test_measurement_in_an_uncited_source_does_not_silence_numeric_warning():
    papers = [paper("p1"), paper("p2", "Tensile strength of 700 MPa was reported for the material.")]
    _, _, catalog = module.local_extraction_input(papers)
    facts = restore(papers, [selection(identifier) for identifier in catalog])
    second = next(fact for fact in facts if fact["paper_id"] == "p2")
    raw = {kind: {"text": "原文に基づいて追加検証を検討する。", "fact_ids": [second["id"]]}
           for kind in ("support", "counter", "outlook", "next_steps")}
    raw["support"]["text"] = "900 MPaが報告された。"
    canonical = module.restore_local_critique(raw, module.local_critique_schema(facts))
    validated = module.validate_critique(canonical, facts, {}, "local_llm", "test")
    assert validated["sections"][0]["text"] == "900 MPaが報告された。"
    assert validated["sections"][0]["validation"]["status"] == "warning"
    assert any("900" in warning["unmatched_numbers"] for warning in validated["validation"]["warnings"])


@pytest.mark.parametrize("kind", ["support", "counter", "outlook", "next_steps"])
def test_local_critique_requires_every_named_block(kind):
    _, schema, raw = critique_fixture()
    del raw[kind]
    with pytest.raises(RuntimeError):
        module.restore_local_critique(raw, schema)


@pytest.mark.parametrize("kind", ["support", "counter"])
def test_uncited_support_or_counter_paragraph_is_not_adopted(kind):
    facts, schema, raw = critique_fixture()
    ungrounded = "独立した追試により完全な成功が証明されています。"
    raw[kind] = {"text": ungrounded, "fact_ids": []}
    canonical = module.restore_local_critique(raw, schema)
    validated = module.validate_critique(canonical, facts, {}, "local_llm", "test")
    section = next(item for item in validated["sections"] if item["kind"] == kind)
    assert "不足" in section["text"] and "保留" in section["text"]
    assert section["fact_ids"] == section["evidence_ids"] == []
    assert ungrounded not in str(canonical) and ungrounded not in str(validated)
    assert any("採用せず" in caveat for caveat in validated["caveats"])
    assert raw[kind]["text"] == ungrounded


def test_no_selected_facts_allows_only_empty_critique_references():
    schema = module.local_critique_schema([])
    raw = {kind: {"text": "根拠が不足しているため、追加の原文が必要です。", "fact_ids": []}
           for kind in ("support", "counter", "outlook", "next_steps")}
    canonical = module.restore_local_critique(raw, schema)
    assert module.validate_critique(canonical, [], {}, "local_llm", "test")["sections"]
    raw["next_steps"]["fact_ids"] = ["invented"]
    with pytest.raises(RuntimeError):
        module.restore_local_critique(raw, schema)


@pytest.mark.parametrize("field", ["headline", "title", "caveats"])
def test_local_critique_does_not_accept_generated_heading_or_caveat_overrides(field):
    _, schema, raw = critique_fixture()
    raw[field] = "都合のよい独自の見出し"
    with pytest.raises(RuntimeError):
        module.restore_local_critique(raw, schema)
