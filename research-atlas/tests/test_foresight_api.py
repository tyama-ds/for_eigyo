import copy
import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import storage, foresight_llm, foresight_sources, foresight_exports
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ELSEVIER_API_KEY", raising=False)
    monkeypatch.delenv("SCOPUS_API_KEY", raising=False)
    with TestClient(app) as value:
        yield value


@pytest.fixture
def result(client):
    papers = [{"id": f"p{i}", "title": f"Steel tensile fatigue testing {i}",
               "abstract": "We measured tensile strength of 900 MPa. However, fatigue durability remains uncertain. Independent validation is needed.",
               "year": 2021 + i % 5, "authors": [{"id": f"a{i}", "name": f"Author {i}"}],
               "keywords": ["steel", "tensile", "fatigue"], "doi": f"10.5555/test{i}", "citations": None,
               "topic_id": "t1", "citation_history": {}, "source": "Fixture"} for i in range(15)]
    value = {"id": storage.new_id(), "dataset_id": storage.new_id(), "dataset_name": "Fixture",
             "meta": {"years": list(range(2021,2026)), "start_year":2021,"end_year":2025,"sampled":False,"is_demo":False},
             "papers": papers, "topics": [{"id":"t1","label":"Steel tensile strength","keywords":["steel","tensile","fatigue"],"count":15}]}
    return storage.save("results", value)


def wait(client, response):
    assert response.status_code == 200, response.text
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/"+response.json()["job_id"]).json()
        if job["status"] in {"completed","failed"}:
            return job
        time.sleep(.02)
    pytest.fail("Assessment job timed out")


def assessment(client, result):
    job=wait(client,client.post("/api/assessments",json={"result_id":result["id"]}))
    assert job["status"]=="completed", job
    return client.get("/api/assessments/"+job["assessment_id"]).json()


def test_create_refine_export_and_immutable_history(client,result):
    before=json.dumps(result)
    value=assessment(client,result)
    cid=value["candidates"][0]["id"]
    job=wait(client,client.post(f"/api/assessments/{value['id']}/explore",json={"candidate_id":cid,"max_rounds":3,
        "feedback":[{"paper_id":"p0","relevance":"relevant"},{"paper_id":"p1","relevance":"irrelevant"}]}))
    assert job["status"]=="completed",job
    revised=client.get("/api/assessments/"+job["assessment_id"]).json()
    assert revised["id"]!=value["id"] and 1<=len(revised["rounds"])<=3
    assert revised["rounds"][0]["negative_ids"]==["p1"]
    assert storage.read("assessments",value["id"])==value
    assert json.dumps(storage.read("results",result["id"]))==before
    listing=client.get("/api/assessments",params={"result_id":result["id"]}).json()
    assert listing["assessments"][0]["id"]==revised["id"]
    query=client.get(f"/api/assessments/{revised['id']}/queries",params={"candidate_id":cid}).json()
    assert "TITLE-ABS-KEY" in query["scopus_query"]
    for fmt in ("json","csv","pdf"):
        response=client.get(f"/api/assessments/{revised['id']}/export?format={fmt}")
        assert response.status_code==200,response.text[:200] if fmt!="pdf" else response.status_code
        if fmt=="pdf": assert response.content.startswith(b"%PDF-") and len(response.content)>5000
        if fmt=="csv": assert response.text.startswith("\ufeff") and "/rounds/0/" in response.text


def test_external_addition_keeps_original_series_and_discovery(client,result,monkeypatch):
    value=assessment(client,result)
    baseline=copy.deepcopy(value["candidates"][0]["growth"]["series"])
    incoming={**result["papers"][0],"id":"new","doi":"10.5555/new","title":"New steel strength pilot validation","year":2025}
    calls=[]
    def discover(candidate, current, provider, limit, progress=None):
        calls.append(candidate["original_query"])
        return [incoming],{"provider":provider,"query":"steel AND validation","retrieved_at":"2026-09-14","warnings":[],"annual_comparison_justified":False}
    monkeypatch.setattr(foresight_sources,"discover_for_candidate",discover)
    job=wait(client,client.post(f"/api/assessments/{value['id']}/explore",json={"candidate_id":"t1","mode":"external","max_rounds":3}))
    assert job["status"]=="completed",job
    revised=storage.read("assessments",job["assessment_id"])
    assert len(revised["papers"])==16
    assert revised["candidates"][0]["growth"]["series"]==baseline
    assert not revised["candidates"][0]["growth"]["available"]
    assert revised["rounds"][0]["added_papers"]==1
    assert revised["rounds"][0]["discovery_report"]["query"]=="steel AND validation"
    assert len(set(calls))==1


def test_external_failure_preserves_last_success(client,result,monkeypatch):
    value=assessment(client,result)
    calls=0
    def discover(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls==2: raise RuntimeError("公開APIが混雑しています。")
        return [],{"provider":"europepmc","query":"steel","warnings":[]}
    monkeypatch.setattr(foresight_sources,"discover_for_candidate",discover)
    job=wait(client,client.post(f"/api/assessments/{value['id']}/explore",json={"candidate_id":"t1","mode":"external","max_rounds":3}))
    assert job["status"]=="failed" and job["assessment_id"]!=value["id"]
    assert len(storage.read("assessments",job["assessment_id"])["rounds"])==1


@pytest.mark.parametrize("patch",[{"max_rounds":4},{"limit":1000},{"candidate_id":"missing"},{"embedding":"bogus"},
    {"feedback":[{"paper_id":"unknown","relevance":"relevant"}]},{"mode":"external","provider":"scopus"}])
def test_invalid_exploration(client,result,patch):
    value=assessment(client,result)
    response=client.post(f"/api/assessments/{value['id']}/explore",json={"candidate_id":"t1",**patch})
    assert response.status_code==422


def test_demo_no_real_external_merge(client,result):
    result["meta"]["is_demo"]=True
    storage.save("results",result)
    value=assessment(client,result)
    response=client.post(f"/api/assessments/{value['id']}/explore",json={"candidate_id":"t1","mode":"external"})
    assert response.status_code==422 and "合成" in response.text


def test_llm_failure_keeps_numeric_report(client,result,monkeypatch):
    value=assessment(client,result)
    monkeypatch.setattr(foresight_llm,"generate",lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError("LLM未接続")))
    job=wait(client,client.post(f"/api/assessments/{value['id']}/commentaries",json={"candidate_id":"t1","provider":"local"}))
    assert job["status"]=="failed"
    revised=storage.read("assessments",job["assessment_id"])
    assert revised["numeric_hash"]==value["numeric_hash"]
    assert revised["candidates"][0]["llm_error"]=="LLM未接続"
    assert client.get(f"/api/assessments/{revised['id']}/export?format=pdf").status_code==200


def test_numeric_mismatch_completes_saves_and_exports_with_warning(client, result, monkeypatch):
    from app import field_llm
    value = assessment(client, result)
    original = copy.deepcopy(value)
    calls = []
    def structured(payload, schema, prompt, provider, model=None, **kwargs):
        calls.append(payload)
        if len(calls) == 1:
            return {"facts": [{"excerpt_id": payload["papers"][0]["excerpts"][0]["id"],
                               "kind": "result", "stance": "support", "attribution": "own_result"}]}, "local_llm", "fixture"
        fid = payload["facts"][0]["id"]
        return {kind: {"text": "引張強度は900 GPaとする生成文です。" if kind == "support" else "原文の条件を確認する必要があります。",
                       "fact_ids": [fid]} for kind in ("support", "counter", "outlook", "next_steps")}, "local_llm", "fixture"
    monkeypatch.setattr(field_llm, "structured_output", structured)
    job = wait(client, client.post(f"/api/assessments/{value['id']}/commentaries",
                                   json={"candidate_id": "t1", "provider": "local"}))
    assert job["status"] == "completed" and job["validation_status"] == "warning"
    assert "数値照合に失敗" in job["stage"] and "error" not in job
    revised = client.get("/api/assessments/" + job["assessment_id"]).json()
    assert revised["numeric_hash"] == value["numeric_hash"]
    assert revised["parent_id"] == value["id"] and storage.read("assessments", value["id"]) == original
    candidate = revised["candidates"][0]
    assert "llm_error" not in candidate
    narrative = candidate["narrative"]
    assert narrative["validation"]["status"] == "warning"
    assert narrative["sections"][0]["text"] == "引張強度は900 GPaとする生成文です。"
    assert narrative["sections"][0]["validation"]["warnings"][0]["unmatched_quantities"] == [{"value": "900", "unit": "GPa"}]
    exported = client.get(f"/api/assessments/{revised['id']}/export?format=json").json()
    assert exported["candidates"][0]["narrative"] == narrative
    csv_text = client.get(f"/api/assessments/{revised['id']}/export?format=csv").text
    assert "/narrative/validation/warnings/0/unmatched_quantities/0/unit" in csv_text
    assert "900 GPa" in csv_text


def test_commentary_progress_is_visible_before_any_revision_is_saved(client, result, monkeypatch):
    from threading import Event
    entered, release = Event(), Event()
    value = assessment(client, result)
    files_before = set((storage.data_root() / "assessments").glob("*.json"))
    def generate(current, candidate_id, provider, model=None, *, progress=None):
        progress("1/2 抄録から根拠を抽出：3分01秒・1,200文字受信（検証前）")
        entered.set()
        assert release.wait(10)
        return copy.deepcopy(current)
    monkeypatch.setattr(foresight_llm, "generate", generate)
    response = client.post(f"/api/assessments/{value['id']}/commentaries", json={"candidate_id": "t1", "provider": "local"})
    try:
        assert entered.wait(5)
        running = client.get("/api/jobs/" + response.json()["job_id"]).json()
        assert running["status"] == "running"
        assert "1,200文字受信（検証前）" in running["stage"]
        assert "assessment_id" not in running
        assert set((storage.data_root() / "assessments").glob("*.json")) == files_before
    finally:
        release.set()
    job = wait(client, response)
    assert job["status"] == "completed"
    assert storage.read("assessments", job["assessment_id"])["numeric_hash"] == value["numeric_hash"]


def test_disk_failure_terminates_job(client,result,monkeypatch):
    original=storage.save
    def save(kind,value):
        if kind=="assessments": raise OSError("private filesystem path")
        return original(kind,value)
    monkeypatch.setattr(storage,"save",save)
    job=wait(client,client.post("/api/assessments",json={"result_id":result["id"]}))
    assert job["status"]=="failed" and "private" not in job["error"]


def test_csv_preserves_prose_nulls_and_formula_safety():
    text=foresight_exports.assessment_csv({"id":"a","revision":2,"candidates":[{"label":"=cmd()\n長文","growth":None}]})
    rows=list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    assert next(r for r in rows if r["JSON Pointer"]=="/candidates/0/label")["Value"]=="'=cmd()\n長文"
    assert next(r for r in rows if r["JSON Pointer"]=="/candidates/0/growth")["Value type"]=="null"


@pytest.mark.parametrize("kind", ["demo", "missing", "test_summary"])
def test_ineligible_commentary_fails_before_job_or_llm_and_keeps_revision(client, result, monkeypatch, kind):
    from app.main import JOBS
    if kind == "demo":
        result["meta"]["is_demo"] = True
    else:
        for paper in result["papers"]:
            paper["abstract"] = "" if kind == "missing" else "[TEST SUMMARY; TITLE-BASED; JA] This is generated test text."
    storage.save("results", result)
    value = assessment(client, result)
    before = storage.read("assessments", value["id"])
    files_before = set((storage.data_root() / "assessments").glob("*.json"))
    jobs_before = set(JOBS)
    monkeypatch.setattr(foresight_llm, "generate", lambda *a, **kw: pytest.fail("Do not queue an ineligible LLM job"))
    response = client.post(f"/api/assessments/{value['id']}/commentaries", json={"candidate_id": "t1", "provider": "local"})
    assert response.status_code == 422
    assert "実抄録" in response.json()["detail"] or "実論文の抄録" in response.json()["detail"]
    assert set(JOBS) == jobs_before
    assert set((storage.data_root() / "assessments").glob("*.json")) == files_before
    assert storage.read("assessments", value["id"]) == before


def test_demo_can_still_request_deterministic_commentary(client, result, monkeypatch):
    from app import field_llm
    result["meta"]["is_demo"] = True
    storage.save("results", result)
    value = assessment(client, result)
    monkeypatch.setattr(field_llm, "structured_output", lambda *a, **kw: pytest.fail("Do not call LLM for deterministic report"))
    job = wait(client, client.post(f"/api/assessments/{value['id']}/commentaries", json={"candidate_id": "t1", "provider": "none"}))
    assert job["status"] == "completed", job
    revised = storage.read("assessments", job["assessment_id"])
    assert revised["numeric_hash"] == value["numeric_hash"]
    assert revised["candidates"][0].get("narrative") is None
