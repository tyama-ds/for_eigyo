import copy
import pytest

from app import foresight_llm as module, field_llm


PAPERS=[{"id":"p1","title":"Steel","year":2025,"abstract":"We measured tensile strength of 900 MPa. Fatigue durability remains uncertain."}]


def fact(**changes):
    return {"paper_id":"p1","source_field":"abstract","quote":"We measured tensile strength of 900 MPa.",
            "kind":"result","statement":"引張強度は900 MPaと報告された。","stance":"support","attribution":"own_result",**changes}


def selection(payload):
    item=fact()
    return {"excerpt_id":payload["papers"][0]["excerpts"][0]["id"], **{k:item[k] for k in ("kind","stance","attribution")}}


def critique(fid):
    return {"headline":"強度と耐久性の検証","sections":[
        {"kind":kind,"title":kind,"text":"観測結果を確認する。" if kind=="support" else "根拠が不足しているため検証が必要。",
         "fact_ids":[fid] if kind=="support" else []} for kind in ("support","counter","outlook","next_steps")],"caveats":[]}


@pytest.mark.parametrize("change",[{"paper_id":"invented"},{"quote":"This is a fabricated experiment."},
    {"kind":"unsupported-kind"},{"quote":None}])
def test_invalid_evidence_rejected(change):
    with pytest.raises(RuntimeError): module.validate_facts({"facts":[fact(**change)]},PAPERS)


def test_quote_offsets_and_number_units():
    value=module.validate_facts({"facts":[fact(),fact()]},PAPERS)
    assert len(value)==1 and PAPERS[0]["abstract"][value[0]["start"]:value[0]["end"]]==value[0]["quote"]
    assert value[0]["validation"]=={"status":"passed","warnings":[]}
    assert value[0]["verification"]=="quote_and_numbers_checked"


@pytest.mark.parametrize("statement,numbers,quantities",[
    ("1000 MPaになった",["1000"],[{"value":"1000","unit":"MPa"}]),
    ("900 GPaになった",[],[{"value":"900","unit":"GPa"}]),
    ("-900 MPaになった",["-900"],[{"value":"-900","unit":"MPa"}]),
])
def test_mismatched_fact_numbers_are_visible_with_precise_warning(statement,numbers,quantities):
    generated={"facts":[fact(statement=statement)]}
    before=copy.deepcopy(generated)
    validated=module.validate_facts(generated,PAPERS)[0]
    assert validated["statement"]==statement and generated==before
    assert validated["verification"]=="quote_checked_numeric_warning"
    assert validated["validation"]["status"]=="warning"
    warning=validated["validation"]["warnings"][0]
    assert warning["code"]=="numeric_mismatch" and warning["location"]=="statement"
    assert warning["unmatched_numbers"]==numbers
    assert warning["unmatched_quantities"]==quantities
    assert warning["message"] and any(ord(character)>127 for character in warning["message"])


@pytest.mark.parametrize("where,location",[("headline","headline"),("title","sections/0/title"),
    ("text","sections/0/text"),("caveats","caveats/0")])
def test_unsupported_numbers_anywhere_are_shown_with_location_warning(where,location):
    facts=module.validate_facts({"facts":[fact()]},PAPERS)
    value=critique(facts[0]["id"])
    if where in {"title","text"}: value["sections"][0][where]="実用化成功率99%"
    elif where=="caveats": value[where]=["2029年に商用化"]
    else: value[where]="実用化成功率99%"
    before=copy.deepcopy(value)
    validated=module.validate_critique(value,facts,{},"local_llm","test")
    assert value==before and validated["validation"]["status"]=="warning"
    warnings=validated["validation"]["warnings"]
    warning=next(item for item in warnings if item["location"]==location)
    assert warning["code"]=="numeric_mismatch"
    assert warning["unmatched_numbers"]==(["2029"] if where=="caveats" else ["99%"])
    if where in {"title","text"}:
        assert validated["sections"][0][where]==value["sections"][0][where]
        assert validated["sections"][0]["validation"]["status"]=="warning"
        assert any(item["location"]==location for item in validated["sections"][0]["validation"]["warnings"])
    elif where=="caveats":
        assert validated["caveats"][0]==value["caveats"][0]
    else:
        assert validated[where]==value[where]
    assert not any("原文一致と数値参照は機械検査済み" in caveat for caveat in validated["caveats"])


def test_warning_fact_statement_never_whitelists_numbers_for_critique():
    facts=module.validate_facts({"facts":[fact(statement="1000 MPaを測定した。") ]},PAPERS)
    value=critique(facts[0]["id"])
    value["headline"]="1000 MPaの性能"
    value["sections"][0]["text"]="1000 MPaを達成した。"
    metrics={"growth":{"score":42},"readiness":{"stage":"unassessed"}}
    before=copy.deepcopy(metrics)
    validated=module.validate_critique(value,facts,metrics,"local_llm","test")
    locations={warning["location"] for warning in validated["validation"]["warnings"]}
    assert {"headline","sections/0/text",f"facts/{facts[0]['id']}/statement"}<=locations
    assert validated["sections"][0]["text"]==value["sections"][0]["text"]
    assert metrics==before and validated["numeric_hash"]==module.digest(before)


def test_fact_warning_is_carried_even_when_critique_does_not_repeat_bad_number():
    facts=module.validate_facts({"facts":[fact(statement="900 GPaを測定した。") ]},PAPERS)
    validated=module.validate_critique(critique(facts[0]["id"]),facts,{},"local_llm","test")
    assert validated["validation"]["status"]=="warning"
    assert any(item["location"]==f"facts/{facts[0]['id']}/statement" for item in validated["validation"]["warnings"])
    assert validated["sections"][0]["validation"]["status"]=="passed"


def test_matching_quote_and_metrics_numbers_have_passed_validation():
    facts=module.validate_facts({"facts":[fact()]},PAPERS)
    value=critique(facts[0]["id"])
    value["sections"][0]["text"]="引張強度は900 MPa、観測スコアは42。"
    validated=module.validate_critique(value,facts,{"score":42},"local_llm","test")
    assert validated["validation"]=={"status":"passed","warnings":[]}
    assert all(section["validation"]=={"status":"passed","warnings":[]} for section in validated["sections"])


def test_unknown_fact_and_missing_counter_are_rejected():
    facts=module.validate_facts({"facts":[fact()]},PAPERS)
    value=critique("invented")
    with pytest.raises(RuntimeError): module.validate_critique(value,facts,{},"local_llm","test")
    value=critique(facts[0]["id"]);value["sections"][1]["text"]="問題はない。"
    with pytest.raises(RuntimeError): module.validate_critique(value,facts,{},"local_llm","test")


def test_generate_two_stage_bounded_scope_immutable(monkeypatch):
    a={"papers":PAPERS,"meta":{"base_paper_ids":[f"secret{i}" for i in range(10000)],"base_paper_years":{"hidden":2025},"paper_count":10000},
       "candidates":[{"id":"t1","label":"steel","paper_ids":["p1"],"growth":{"available":False},"readiness":{"stage":"unassessed"}}]}
    before=copy.deepcopy(a);calls=[]
    def generate(payload,schema,prompt,provider,model=None):
        calls.append(payload)
        if "papers" in payload: return {"facts":[selection(payload)]},"local_llm","test"
        assert "base_paper_ids" not in payload["scope"] and "base_paper_years" not in payload["scope"]
        return {s["kind"]:{k:s[k] for k in ("text","fact_ids")} for s in critique(payload["facts"][0]["id"])["sections"]},"local_llm","test"
    monkeypatch.setattr(field_llm,"structured_output",generate)
    out=module.generate(a,"t1","local")
    assert a==before and len(calls)==2
    assert out["candidates"][0]["growth"]==a["candidates"][0]["growth"]
    assert out["candidates"][0]["narrative"]["numeric_hash"]


def test_generate_retains_numeric_warning_commentary_and_original_calculated_metrics(monkeypatch):
    assessment={"papers":PAPERS,"meta":{},"candidates":[{"id":"t1","paper_ids":["p1"],
        "growth":{"score":42,"series":[{"year":2025,"count":10}]},"readiness":{"stage":"unassessed"}}]}
    before=copy.deepcopy(assessment)
    calls=[]
    def generate(payload,schema,prompt,provider,model=None):
        calls.append(payload)
        if "papers" in payload:
            return {"facts":[fact(statement="900 GPaを達成した。") ]},"openai","test"
        assert payload["facts"][0]["validation"]["status"]=="warning"
        result=critique(payload["facts"][0]["id"])
        result["sections"][0]["text"]="900 GPaを達成したため、有望性を検討する。"
        return result,"openai","test"
    monkeypatch.setattr(field_llm,"structured_output",generate)
    result=module.generate(assessment,"t1","openai")
    candidate=result["candidates"][0]
    assert len(calls)==2 and assessment==before
    assert candidate["growth"]==before["candidates"][0]["growth"]
    assert candidate["readiness"]==before["candidates"][0]["readiness"]
    assert candidate["content_facts"][0]["statement"]=="900 GPaを達成した。"
    assert candidate["narrative"]["sections"][0]["text"]=="900 GPaを達成したため、有望性を検討する。"
    assert candidate["narrative"]["validation"]["status"]=="warning"
    assert candidate["narrative"]["numeric_hash"]==module.digest(module.numerical_payload(before["candidates"][0]))
    assert "llm_error" not in candidate


def test_generated_summary_not_sent(monkeypatch):
    paper={**PAPERS[0],"abstract":"[TEST SUMMARY; TITLE-BASED; JA] " + PAPERS[0]["abstract"]}
    a={"papers":[paper],"meta":{},"candidates":[{"id":"t1","paper_ids":["p1"]}]}
    monkeypatch.setattr(field_llm,"structured_output",lambda *a,**kw: pytest.fail("Do not call LLM"))
    with pytest.raises(ValueError,match="実抄録"): module.generate(a,"t1","local")


def test_local_stream_progress_covers_both_phases_without_changing_metrics(monkeypatch):
    assessment={"papers":PAPERS,"meta":{},"candidates":[{"id":"t1","paper_ids":["p1"],
                "growth":{"available":False},"readiness":{"stage":"unassessed"}}]}
    before=copy.deepcopy(assessment)
    stages=[]
    def generate(payload,schema,prompt,provider,model=None,*,progress=None):
        progress({"elapsed_seconds":181.2,"received_chars":1000})
        if "papers" in payload:
            assert schema.model_json_schema()["properties"]["facts"]["maxItems"]==6
            return {"facts":[selection(payload)]},"local_llm","test"
        return {s["kind"]:{k:s[k] for k in ("text","fact_ids")} for s in critique(payload["facts"][0]["id"])["sections"]},"local_llm","test"
    monkeypatch.setattr(field_llm,"structured_output",generate)
    out=module.generate(assessment,"t1","local",progress=stages.append)
    assert any("1/2" in s and "3分01秒" in s for s in stages)
    assert any("2/2" in s and "1,000文字" in s for s in stages)
    assert all("900 MPa" not in s for s in stages)
    assert assessment==before
    assert out["candidates"][0]["growth"]==before["candidates"][0]["growth"]
    assert out["candidates"][0]["narrative"]["extraction_fact_limit"]==6


def test_local_extraction_over_budget_is_not_silently_truncated(monkeypatch):
    assessment={"papers":PAPERS,"meta":{},"candidates":[{"id":"t1","paper_ids":["p1"]}]}
    calls=[]
    def generate(*args,**kwargs):
        calls.append(args)
        return {"facts":[selection(args[0]) for _ in range(7)]},"local_llm","test"
    monkeypatch.setattr(field_llm,"structured_output",generate)
    with pytest.raises(RuntimeError,match="不正な抽出形式"):
        module.generate(assessment,"t1","local")
    assert len(calls)==1
    assert "content_facts" not in assessment["candidates"][0]
