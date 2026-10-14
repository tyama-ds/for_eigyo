"""Whole-corpus jobs use offline LLM doubles and real durable storage."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from app import connection_settings, corpus_llm, corpus_reporting as reports, large_storage, storage

REAL_EXTRACT = corpus_llm.extract_paper
REAL_SYNTHESIZE = corpus_llm.synthesize


def paper(identifier, **fields):
    return {"id": str(identifier), "title": "Steel paper " + str(identifier), "abstract": "Microscopy measured strength of steel.",
            "year": 2021, "topic_id": "steel", **fields}


def result(papers, **meta):
    saved = {"id": storage.new_id(), "meta": meta, "papers": papers,
             "topics": [{"id": "steel", "label": "Steel"}, {"id": "battery", "label": "Battery"}]}
    storage.save("results", saved)
    return saved


def extraction(paper, *, status="completed", covered=None):
    size = len(paper["abstract"])
    fact = {"id": "fact-" + paper["id"], "kind": "method", "statement": "Microscopy", "quote": "Microscopy", "source_start": 0, "source_end": 10, "warnings": []}
    return {"paper_id": paper["id"], "status": status, "facts": [fact], "chunks": [{"start": 0, "end": size,
        "status": "completed", "facts": [fact]}], "warnings": [], "covered_chars": size if covered is None else covered}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    assert not reports._ACTIVE
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    state = {"calls": [], "syntheses": [], "extract": None, "synthesize": None, "namespace": "test-namespace"}
    monkeypatch.setattr(corpus_llm, "resolve_model", lambda provider, model=None: model or "test-model")
    monkeypatch.setattr(corpus_llm, "cache_namespace", lambda provider, model: f"{state['namespace']}:{provider}:{model}")
    def extract(paper, provider, model, **kwargs):
        state["calls"].append(paper["id"])
        if state["extract"]:
            return state["extract"](paper, provider, model, **kwargs)
        value = extraction(paper)
        kwargs["checkpoint"]["save"](value)
        return value
    def synthesize(items, provider, model, **kwargs):
        items = list(items)
        state["syntheses"].append({"label": kwargs["label"], "items": deepcopy(items), "state": deepcopy(kwargs.get("checkpoint", {}).get("state")), "metrics": deepcopy(kwargs.get("metrics"))})
        if state["synthesize"]:
            return state["synthesize"](items, provider, model, **kwargs)
        ids = list(dict.fromkeys(identifier for item in items for identifier in (item.get("paper_ids") or [item.get("paper_id", item.get("id"))]) if identifier))
        return {"status": "completed", "text": "根拠に基づく評論", "sections": [{"title": kwargs["label"], "text": "研究の推移を解説", "paper_ids": ids}],
                "paper_ids": ids, "source_count": len(ids), "input_count": len(items), "consumed_count": len(items), "warnings": [], "negative_results": []}
    monkeypatch.setattr(corpus_llm, "extract_paper", extract)
    monkeypatch.setattr(corpus_llm, "synthesize", synthesize)
    yield tmp_path, state
    for _, event in list(reports._ACTIVE.values()):
        event.set()
    deadline = time.monotonic() + 10
    while reports._ACTIVE and time.monotonic() < deadline:
        time.sleep(.02)
    assert not reports._ACTIVE


def finish(identifier, timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = reports.get_report(identifier)
        if value["status"] not in {"preparing", "running"} and not reports._ACTIVE:
            return value
        time.sleep(.03)
    raise AssertionError("offline report did not finish")


def start(papers, **options):
    saved = result(papers)
    created = reports.create_report(saved["id"], **options)
    return saved, finish(created["id"])


def test_batch_counts_missing_abstracts_and_complete_numerical_population(ledger, monkeypatch):
    _, state = ledger
    # A near-instant double can finish within one Windows monotonic tick. Give
    # extraction a known duration while leaving polling/fixture clocks real.
    clock = [100.0]
    monkeypatch.setattr(reports, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def timed_extract(paper, provider, model, **kwargs):
        value = extraction(paper)
        kwargs["checkpoint"]["save"](value)
        clock[0] += 2.0
        return value
    state["extract"] = timed_extract
    _, value = start([paper("a"), paper("b", abstract=""), paper("c", year=2022, topic_id="battery"), paper("d")], batch_size=1)
    assert value["status"] == "paused" and value["partial"]
    assert value["counts"] == {"total": 4, "completed": 1, "missing": 1, "failed": 0, "pending": 2, "cached": 0,
                               "processed_chars": 38, "total_chars": 114}
    assert value["has_facts"]
    assert [(row["year"], row["total"]) for row in value["annual"]] == [(2021, 3), (2022, 1)]
    assert sum(row["total"] for row in value["topics"]) == 4
    reports.resume_report(value["id"], batch_size=1)
    second = finish(value["id"])
    assert second["counts"]["completed"] == 2 and state["calls"] == ["a", "c"]
    assert second["estimate"]["sampled_papers"] == 2 and second["estimate"]["remaining_seconds"] is not None
    assert second["estimate"]["seconds_per_paper"] == second["estimate"]["remaining_seconds"] == 2.0
    assert second["methods"] == [{"method": "microscopy", "paper_count": 2}]


def test_run_all_processes_beyond_a_batch_and_no_arbitrary_20k_population_limit(ledger, monkeypatch):
    _, state = ledger
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 25000)
    records = [paper(f"p{index:05d}", year=2000 + index % 20, topic_id="steel" if index % 2 else "battery") for index in range(20003)]
    saved = result(records)
    original_read = storage.read
    def manifest_only(kind, identifier, *, include_papers=True):
        assert include_papers is False
        return original_read(kind, identifier, include_papers=False)
    monkeypatch.setattr(storage, "read", manifest_only)
    value = finish(reports.create_report(saved["id"], batch_size=100)["id"], timeout=60)
    assert value["counts"]["total"] == 20003 and value["counts"]["completed"] == 100 and value["counts"]["pending"] == 19903
    reports.resume_report(value["id"], batch_size=100)
    second = finish(value["id"])
    assert second["counts"]["completed"] == 200 and second["counts"]["pending"] == 19803
    assert sum(row["total"] for row in second["annual"]) == sum(row["total"] for row in second["topics"]) == 20003
    assert reports.paper_page(value["id"], offset=20002, limit=1)["items"][0]["id"] == "p20002"
    assert len(state["calls"]) == 200


def test_run_all_reaches_last_paper_and_keeps_input_unchanged(ledger):
    saved, value = start([paper(f"p{index:04d}") for index in range(405)], batch_size=2, run_all=True)
    assert value["counts"]["completed"] == 405 and value["counts"]["pending"] == 0
    assert reports.paper_page(value["id"], offset=404, limit=1)["items"][0]["status"] == "completed"
    assert storage.read("results", saved["id"])["papers"] == saved["papers"]


def test_cache_reuses_content_after_reclustering_and_invalidates_content_provider_model_prompt(ledger, monkeypatch):
    _, state = ledger
    _, first = start([paper("a")])
    _, cached = start([paper("b", title="Steel paper a", year=2024, topic_id="battery")])
    assert cached["counts"]["cached"] == 1 and state["calls"] == ["a"]
    assert reports.paper_page(cached["id"])["items"][0]["extraction"]["paper_id"] == "b"
    start([paper("a", abstract="Microscopy measured another material.")])
    start([paper("a")], provider="openai")
    start([paper("a")], model="other-model")
    monkeypatch.setattr(corpus_llm, "PROMPT_VERSION", "next-version")
    start([paper("a")])
    assert len(state["calls"]) == 5


def test_isolated_roots_do_not_reuse_another_projects_cache(ledger, tmp_path, monkeypatch):
    _, state = ledger
    saved, first = start([paper("a")])
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "second-root"))
    _, second = start([paper("a")])
    assert second["counts"]["cached"] == 0 and state["calls"] == ["a", "a"]
    with pytest.raises(KeyError):
        reports.get_report(first["id"])


def test_restart_and_resume_only_remaining_papers(ledger):
    path, state = ledger
    _, first = start([paper("a"), paper("b")], batch_size=1)
    with sqlite3.connect(path / "corpus_reports.sqlite") as db:
        db.execute("UPDATE reports SET status='running' WHERE id=?", (first["id"],))
    assert reports.get_report(first["id"])["status"] == "paused"
    reports.resume_report(first["id"])
    assert finish(first["id"])["counts"]["completed"] == 2 and state["calls"] == ["a", "b"]


def test_cooperative_pause_saves_chunk_state_and_resume_receives_it(ledger):
    _, state = ledger
    ready, release = threading.Event(), threading.Event()
    previous = []
    def partial(paper, provider, model, **kwargs):
        value = extraction(paper, status="cancelled", covered=10)
        kwargs["checkpoint"]["save"](value)
        ready.set(); release.wait(5)
        return value
    state["extract"] = partial
    saved = result([paper("a"), paper("b")])
    created = reports.create_report(saved["id"], run_all=True)
    assert ready.wait(5)
    reports.pause_report(created["id"])
    with pytest.raises(reports.ReportBusy):
        reports.resume_report(created["id"])
    release.set()
    paused = finish(created["id"])
    assert paused["counts"]["pending"] == 2 and paused["counts"]["processed_chars"] == 10
    def resumed(paper, provider, model, **kwargs):
        previous.append(kwargs["checkpoint"]["state"])
        return extraction(paper)
    state["extract"] = resumed
    reports.resume_report(created["id"], run_all=True)
    assert finish(created["id"])["counts"]["completed"] == 2
    assert previous[0]["covered_chars"] == 10


def test_three_failures_stop_run_all_and_failed_can_be_retried(ledger):
    _, state = ledger
    state["extract"] = lambda paper, *args, **kwargs: {**extraction(paper), "status": "failed", "facts": [], "covered_chars": 0}
    _, first = start([paper(str(index)) for index in range(20)], run_all=True)
    assert first["status"] == "paused" and first["counts"]["failed"] == 3 and first["counts"]["pending"] == 17
    assert first["estimate"]["remaining_seconds"] is None and first["estimate"]["sampled_papers"] == 0
    state["extract"] = None
    reports.resume_report(first["id"], retry_failed=True, run_all=True)
    assert finish(first["id"])["counts"]["completed"] == 20


@pytest.mark.parametrize("bad", [{}, {"status": "completed", "facts": [], "chunks": [], "covered_chars": 0}, "raw-secret"])
def test_invalid_completion_is_not_claimed_as_processed(ledger, bad):
    _, state = ledger
    state["extract"] = lambda *args, **kwargs: bad
    _, report = start([paper("a")])
    assert report["counts"]["failed"] == 1 and report["counts"]["completed"] == 0
    assert "raw-secret" not in json.dumps(reports.paper_page(report["id"]))


def test_request_credentials_follow_worker_but_never_reach_disk_or_exports(ledger):
    path, state = ledger
    seen = []
    def failed(paper, provider, model, **kwargs):
        seen.append(connection_settings.current_settings().openai.api_key.get_secret_value())
        raise RuntimeError("private-token and proxy-password must not be saved")
    state["extract"] = failed
    settings = connection_settings.ConnectionSettings.model_validate({"openai": {"api_key": "private-token"}})
    with connection_settings.settings_context(settings):
        _, value = start([paper("a")])
    assert seen == ["private-token"]
    payload = json.dumps(reports.get_report(value["id"])) + json.dumps(list(reports.iter_records(value["id"])))
    assert "private-token" not in payload and "proxy-password" not in payload
    assert b"private-token" not in (path / "corpus_reports.sqlite").read_bytes()


def test_resume_refuses_changed_connection_namespace(ledger):
    _, state = ledger
    _, report = start([paper("a"), paper("b")], batch_size=1)
    state["namespace"] = "new-server"
    reports.resume_report(report["id"])
    changed = finish(report["id"])
    assert changed["status"] == "error" and "接続先" in changed["error"]
    assert state["calls"] == ["a"]


def test_hierarchical_synthesis_consumes_all_extracted_papers_and_reuses_groups(ledger):
    _, state = ledger
    _, report = start([paper("a", year=2020), paper("b", year=2021, topic_id="battery"), paper("c", year=2020)], run_all=True)
    reports.synthesize_report(report["id"])
    full = finish(report["id"])
    assert full["synthesis_status"] == "completed" and full["narrative"]["paper_ids"] == ["a", "c", "b"]
    assert len(full["groups"]) == 4 and len(state["syntheses"]) == 5
    assert [item["year"] for item in state["syntheses"][-1]["items"]] == ["2020", "2021"]
    assert state["syntheses"][-1]["metrics"]["counts"]["total"] == 3
    assert sum(row["total"] for row in state["syntheses"][-1]["metrics"]["annual"]) == 3
    assert next(call for call in state["syntheses"] if call["label"] == "2020 年")["metrics"]["total"] == 2
    reports.synthesize_report(report["id"])
    finish(report["id"])
    assert len(state["syntheses"]) == 6


def test_new_extraction_invalidates_affected_group_and_overall_not_unchanged_year(ledger):
    _, state = ledger
    _, value = start([paper("a", year=2020), paper("b", year=2021), paper("c", year=2021)], batch_size=2)
    reports.synthesize_report(value["id"])
    assert finish(value["id"])["synthesis_status"] == "partial"
    reports.resume_report(value["id"])
    done = finish(value["id"])
    assert done["narrative"] is None and done["synthesis_status"] == "not_requested"
    status = {(group["kind"], group["key"]): group["status"] for group in done["groups"]}
    assert status[("year", "2020")] == "completed" and status[("year", "2021")] == "stale"


def test_missing_or_ungrounded_corpus_never_generates_a_fictional_summary(ledger):
    _, state = ledger
    _, missing = start([paper("a", abstract="")])
    reports.synthesize_report(missing["id"])
    assert finish(missing["id"])["synthesis_status"] == "not_available"
    state["extract"] = lambda paper, *args, **kwargs: {**extraction(paper), "status": "no_grounded_facts", "facts": []}
    _, empty = start([paper("a")])
    reports.synthesize_report(empty["id"])
    assert finish(empty["id"])["synthesis_status"] == "not_available" and not state["syntheses"]


def test_group_checkpoint_survives_pause_and_is_reused(ledger):
    _, state = ledger
    _, report = start([paper("a")])
    stopped = []
    class Cancelled(Exception):
        kind = "cancelled"
    def interrupted(items, provider, model, **kwargs):
        checkpoint = kwargs["checkpoint"]
        if not checkpoint["state"]["nodes"]:
            checkpoint["save"]({"kind": "summary_node", "request_hash": "request1", "node": {"text": "saved node"}})
            raise Cancelled()
        stopped.append(checkpoint["state"]["nodes"])
        return {"status": "completed", "text": "continued", "sections": [], "paper_ids": ["a"]}
    state["synthesize"] = interrupted
    reports.synthesize_report(report["id"])
    assert finish(report["id"])["status"] == "paused"
    reports.synthesize_report(report["id"])
    finish(report["id"])
    assert stopped and stopped[0]["request1"]["node"]["text"] == "saved node"


def test_demo_and_sampling_scope_survive_into_llm_inputs_and_saved_report(ledger):
    _, state = ledger
    saved = result([paper("a")], is_demo=True, sampled=True, warnings=["original source limitation"])
    saved["dataset_name"] = "Demo steel"
    storage.save("results", saved)
    report = finish(reports.create_report(saved["id"])["id"])
    assert report["is_demo"] and report["sampled"] and report["name"] == "Demo steel"
    assert any("合成" in warning for warning in report["warnings"])
    reports.synthesize_report(report["id"])
    finish(report["id"])
    assert all(item["is_demo"] and item["sampled"] for call in state["syntheses"] for item in call["items"])


def test_stream_export_uses_one_sqlite_snapshot(ledger):
    _, state = ledger
    _, report = start([paper("a"), paper("b")], batch_size=1)
    stream = reports.iter_export(report["id"], "json")
    first = next(stream)
    reports.resume_report(report["id"])
    finish(report["id"])
    exported = json.loads(first + "".join(stream))
    assert exported["report"]["counts"]["completed"] == 1
    assert sum(p["status"] == "completed" for p in exported["papers"]) == 1


def test_real_chunk_and_hierarchy_contract_with_offline_transport(ledger, monkeypatch):
    from app import field_llm
    path, _ = ledger
    monkeypatch.setattr(corpus_llm, "extract_paper", REAL_EXTRACT)
    monkeypatch.setattr(corpus_llm, "synthesize", REAL_SYNTHESIZE)
    calls = []
    def generate(payload, schema, instructions, provider, model, **kwargs):
        calls.append(deepcopy(payload))
        assert kwargs["preserve_input"] is True
        if schema is corpus_llm.Extraction:
            quote = payload["papers"][0]["abstract"][:80]
            return {"facts": [{"kind": "method", "statement": "原文の実験記述", "quote": quote}]}, "local_llm", model
        return {"text": "抄録と計測値に基づく総合評論", "sections": [{"title": "根拠と変化", "text": "収録された実験の傾向を比較します。",
            "child_ids": [item["child_id"] for item in payload["items"]]}], "warnings": []}, "local_llm", model
    monkeypatch.setattr(field_llm, "structured_output", generate)
    _, report = start([paper("a", abstract="Microscopy measured strength. " * 400), paper("b", year=2022)], run_all=True)
    assert report["counts"]["completed"] == 2 and report["counts"]["processed_chars"] == report["counts"]["total_chars"]
    reports.synthesize_report(report["id"])
    summarized = finish(report["id"])
    assert summarized["synthesis_status"] == "completed" and summarized["narrative"]["source_count"] == 2
    assert summarized["narrative"]["metrics_node_count"] > 0
    with sqlite3.connect(path / "corpus_reports.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM summary_nodes WHERE report_id=?", (report["id"],)).fetchone()[0] > 0
    previous = len(calls)
    reports.synthesize_report(report["id"])
    assert finish(report["id"])["synthesis_status"] == "completed"
    assert len(calls) == previous, "identical hierarchy nodes should resume from durable checkpoints"


def test_large_audit_metadata_is_not_read_or_returned_on_poll_but_full_export_keeps_it(ledger):
    path, _ = ledger
    saved, report = start([paper("a")])
    ids = [f"p{index}" for index in range(20000)]
    narrative = {"status": "completed", "text": "全文の詳細評論。" * 100, "sections": [{"title": "詳細", "text": "各年度の研究内容。" * 100,
        "paper_ids": ids, "warnings": [{"code": "numeric_mismatch", "message": "照合に注意"}] * 20000}], "paper_ids": ids,
        "source_count": 20000, "provenance": {"large": "provenance-data" * 100000}, "input_contexts": ["input-data" * 100000],
        "negative_results": [{"quote": "negative finding"}] * 20000,
        "warnings": [{"code": "numeric_mismatch", "message": "照合に注意"}] * 20000}
    with closing(reports._db()) as db:
        reports._update(db, report["id"], narrative=reports._json(narrative), synthesis_status="completed")
        with db:
            db.execute("INSERT INTO groups VALUES(?,?,?,?,?,?,?,?,?)", (report["id"], "year", "2021", "2021 年", "completed", 1,
                reports._json(narrative), None, reports._json(reports._preview(narrative))))
    preview = reports.get_report(report["id"])
    assert len(json.dumps(preview)) < 25000
    assert preview["narrative"]["text"] == narrative["text"]
    assert preview["narrative"]["sections"][0]["text"] == narrative["sections"][0]["text"]
    assert preview["narrative"]["paper_ids_count"] == preview["narrative"]["warning_count"] == 20000
    assert preview["narrative"]["negative_result_count"] == 20000
    assert preview["narrative"]["sections"][0]["paper_ids_truncated"]
    assert "provenance" not in preview["narrative"] and len(preview["narrative"]["paper_ids"]) == 20
    assert "narrative" not in reports.list_reports(saved["id"])[0]
    exported = json.loads("".join(reports.iter_export(report["id"], "json")))
    assert exported["report"]["narrative"] == narrative
    assert exported["report"]["groups"][0]["narrative"] == narrative
    # Poison the full audit JSON after the preview was saved: the UI must read
    # only the independent preview column, not decode the full narrative.
    with sqlite3.connect(path / "corpus_reports.sqlite") as db:
        db.execute("UPDATE reports SET narrative='invalid-full-json' WHERE id=?", (report["id"],))
        db.execute("UPDATE groups SET narrative='invalid-full-json' WHERE report_id=?", (report["id"],))
    assert reports.get_report(report["id"])["narrative"]["text"] == narrative["text"]


def test_outer_synthesis_failure_never_leaves_active_status_stuck(ledger, monkeypatch):
    _, _ = ledger
    _, report = start([paper("a")])
    def crash(db, identifier, event):
        reports._update(db, identifier, synthesis_status="running")
        raise RuntimeError("private transport error")
    monkeypatch.setattr(reports, "_synthesize", crash)
    reports.synthesize_report(report["id"])
    failed = finish(report["id"])
    assert failed["status"] == "error" and failed["synthesis_status"] == "failed"
    assert "private" not in failed["error"]


def test_adaptive_calibration_is_durable_and_reused_on_next_batch(ledger):
    path, state = ledger
    observed = []
    calibration = {"enabled": True, "version": "corpus-adaptive-v1", "target_seconds": 120,
                   "extraction": {"max_chars": 1200}, "timeout_recoveries": 1}
    def calibrated(paper, *args, **kwargs):
        observed.append(deepcopy(kwargs["adaptive"]["state"]))
        kwargs["adaptive"]["save"](calibration)
        kwargs["adaptive"]["state"] = deepcopy(calibration)
        kwargs["progress"]({"stage": "時間切れのため入力を分割して再試行しています。"})
        with closing(reports._db()) as db:
            active = db.execute("SELECT stage FROM reports WHERE status='running'").fetchone()
            assert "時間切れ" in active[0]
        return extraction(paper)
    state["extract"] = calibrated
    _, first = start([paper("a"), paper("b")], batch_size=1)
    assert first["adaptive"] == calibration and observed == [{}]
    # A fresh worker connection loads the calibration left by the earlier run.
    reports.resume_report(first["id"], batch_size=1)
    second = finish(first["id"])
    assert observed == [{}, calibration]
    assert second["batch_size"] == 1 and second["counts"]["completed"] == 2
    with sqlite3.connect(path / "corpus_reports.sqlite") as db:
        saved = json.loads(db.execute("SELECT config FROM reports WHERE id=?", (first["id"],)).fetchone()[0])
        assert saved["adaptive"] == calibration and saved["batch_size"] == 1
    exported = json.loads("".join(reports.iter_export(first["id"], "json")))
    assert exported["report"]["adaptive"] == calibration


def test_exhausted_timeout_pauses_before_next_paper_and_preserves_retry_checkpoint(ledger):
    _, state = ledger
    checkpoints = []
    def timed_out(paper, *args, **kwargs):
        checkpoints.append(deepcopy(kwargs["checkpoint"]["state"]))
        if kwargs["checkpoint"]["state"]:
            return extraction(paper)
        value = extraction(paper, status="partial", covered=10)
        value["chunks"][0]["end"] = 10
        value["error_kind"] = "timeout_recovery_exhausted"
        kwargs["checkpoint"]["save"](value)
        return value
    state["extract"] = timed_out
    _, first = start([paper("a"), paper("b"), paper("c")], run_all=True)
    assert first["status"] == "paused" and state["calls"] == ["a"]
    assert first["counts"]["failed"] == 1 and first["counts"]["pending"] == 2
    assert first["counts"]["processed_chars"] == 10 and "上限" in first["stage"]
    record = reports.paper_page(first["id"])["items"][0]
    assert record["extraction"]["facts"] and "時間切れ" in record["error"]
    reports.resume_report(first["id"], batch_size=1, retry_failed=True)
    second = finish(first["id"])
    assert state["calls"] == ["a", "a"] and checkpoints[-1]["covered_chars"] == 10
    assert second["counts"]["completed"] == 1 and second["counts"]["pending"] == 2


def test_synthesis_timeout_exhaustion_pauses_and_reuses_saved_nodes(ledger):
    _, state = ledger
    _, report = start([paper("a", year=2020), paper("b", year=2021)])
    class Exhausted(Exception):
        kind = "timeout_recovery_exhausted"
    def stalled(items, provider, model, **kwargs):
        kwargs["checkpoint"]["save"]({"kind": "summary_node", "request_hash": "completed-child", "node": {"text": "保存済み要約"}})
        kwargs["adaptive"]["save"]({"enabled": True, "timeout_recoveries": 3, "synthesis": {"max_tokens": 600}})
        raise Exhausted("private error text")
    state["synthesize"] = stalled
    reports.synthesize_report(report["id"])
    first = finish(report["id"])
    assert first["status"] == "paused" and first["synthesis_status"] == "partial"
    assert len(state["syntheses"]) == 1 and "上限" in first["stage"] and "private" not in first["stage"]
    assert first["groups"][0]["status"] == "partial" and first["adaptive"]["timeout_recoveries"] == 3
    state["synthesize"] = None
    reports.synthesize_report(report["id"])
    second = finish(report["id"])
    assert state["syntheses"][1]["state"]["nodes"]["completed-child"]["node"]["text"] == "保存済み要約"
    assert second["synthesis_status"] == "completed"


def test_synthesis_stops_after_three_failed_groups_without_contacting_rest(ledger):
    _, state = ledger
    _, report = start([paper(str(year), year=year) for year in range(2015, 2025)])
    def unavailable(*args, **kwargs):
        raise RuntimeError("private connection details")
    state["synthesize"] = unavailable
    reports.synthesize_report(report["id"])
    stopped = finish(report["id"])
    assert stopped["status"] == "paused" and stopped["synthesis_status"] == "partial"
    assert len(state["syntheses"]) == 3 and all(group["status"] == "failed" for group in stopped["groups"])
    assert "3区分" in stopped["stage"] and "private" not in json.dumps(stopped)


def test_overall_timeout_exhaustion_preserves_completed_groups_and_pauses(ledger):
    _, state = ledger
    _, report = start([paper("a")])
    class Exhausted(Exception):
        kind = "timeout_recovery_exhausted"
    def stalled_overall(items, provider, model, **kwargs):
        if kwargs["label"] == "全期間・全分野の総合評論":
            kwargs["checkpoint"]["save"]({"kind": "summary_node", "request_hash": "overall-child", "node": {"text": "途中まで保存"}})
            raise Exhausted()
        return {"status": "completed", "text": "区分別評論", "sections": [], "paper_ids": ["a"]}
    state["synthesize"] = stalled_overall
    reports.synthesize_report(report["id"])
    stopped = finish(report["id"])
    assert stopped["status"] == "paused" and stopped["synthesis_status"] == "partial"
    assert all(group["status"] == "completed" for group in stopped["groups"])
    state["synthesize"] = None
    before = len(state["syntheses"])
    reports.synthesize_report(report["id"])
    assert finish(report["id"])["synthesis_status"] == "completed"
    assert len(state["syntheses"]) == before + 1
    assert "overall-child" in state["syntheses"][-1]["state"]["nodes"]
