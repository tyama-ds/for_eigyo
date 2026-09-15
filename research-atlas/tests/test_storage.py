from copy import deepcopy
from datetime import date

import pytest


def test_existing_snapshot_can_be_read_while_new_payload_is_prepared(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app import large_storage
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    original = storage.create_dataset([{"id": "original", "year": 2025}], "Original")
    entered, release = Event(), Event()
    real_externalize = large_storage.externalize
    def preparing(value, root):
        entered.set()
        assert release.wait(10)
        return real_externalize(value, root)
    monkeypatch.setattr(large_storage, "externalize", preparing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(storage.create_dataset, [{"id": "new", "year": 2024}], "New")
        try:
            assert entered.wait(3)
            reading = pool.submit(storage.read, "datasets", original["id"])
            assert reading.result(timeout=3) == original
        finally:
            release.set()
        assert saving.result(timeout=5)["paper_count"] == 1

from app import storage


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 14)
    monkeypatch.setattr(storage, "date", FixedDate)


def dataset(papers=None, report=None, **changes):
    return {"id": "a" * 32, "name": "Fixture", "paper_count": len(papers) if isinstance(papers, list) else 0,
            "is_demo": False, "created_at": "2026-09-14T00:00:00Z", "papers": papers,
            "report": report, **changes}


def test_csv_defaults_follow_actual_years_and_do_not_mutate_dataset():
    value = dataset([{"year": 2025}, {"year": 2025}])
    before = deepcopy(value)
    summary = storage.dataset_summary(value)
    assert summary["analysis_defaults"] == {"start_year": 2025, "end_year": 2025}
    assert value == before
    assert "papers" not in summary


def test_demo_defaults_follow_actual_range():
    summary = storage.dataset_summary(dataset([{"year": 2021}, {"year": 2025}], is_demo=True))
    assert summary["analysis_defaults"] == {"start_year": 2021, "end_year": 2025}
    assert summary["is_demo"] is True


def test_search_defaults_preserve_requested_zero_paper_years_and_request_precedence():
    summary = storage.dataset_summary(dataset([{"year": 2025}], {
        "discovery_request": {"start_year": 2021, "end_year": 2026},
        "start_year": 2023, "end_year": 2025}))
    assert summary["analysis_defaults"] == {"start_year": 2021, "end_year": 2026}


def test_search_report_range_fills_absent_request_range():
    summary = storage.dataset_summary(dataset([], {
        "discovery_request": {"provider": "crossref"}, "start_year": 2020, "end_year": 2025}))
    assert summary["analysis_defaults"] == {"start_year": 2020, "end_year": 2025}


def test_existing_monthly_search_anchor_stays_on_latest_complete_month():
    summary = storage.dataset_summary(dataset([{"year": 2026}], {
        "discovery_request": {"provider": "europepmc", "start_year": 2025, "end_year": 2026},
        "start_year": 2025, "end_year": 2026, "start_month": "2025-10", "end_month": "2026-09"}))
    assert summary["analysis_defaults"] == {"start_year": 2025, "end_year": 2026,
                                            "window_months": 3, "anchor_month": "2026-08"}


def test_monthly_search_can_use_request_month_fields_and_historical_end():
    summary = storage.dataset_summary(dataset([], {"discovery_request": {
        "start_year": 2024, "end_year": 2025, "start_month": "2024-09", "end_month": "2025-08"}}))
    assert summary["analysis_defaults"]["anchor_month"] == "2025-08"


def test_monthly_anchor_outside_selected_years_is_none(monkeypatch):
    class JanuaryDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 1, 10)
    monkeypatch.setattr(storage, "date", JanuaryDate)
    summary = storage.dataset_summary(dataset([], {"discovery_request": {
        "start_year": 2026, "end_year": 2026, "start_month": "2026-01", "end_month": "2026-01"}}))
    assert summary["analysis_defaults"]["anchor_month"] is None


def test_long_csv_range_is_limited_to_fifty_calendar_years_with_note():
    summary = storage.dataset_summary(dataset([{"year": 1900}, {"year": 2025}]))
    assert summary["analysis_defaults"] == {"start_year": 1976, "end_year": 2025}
    assert "50暦年" in summary["analysis_defaults_note"]


@pytest.mark.parametrize("report", [None, {"start_year": 2025, "end_year": 2030}])
def test_future_end_year_is_clipped_to_current_year(report):
    summary = storage.dataset_summary(dataset([{"year": 2025}, {"year": 2030}], report))
    assert summary["analysis_defaults"] == {"start_year": 2025, "end_year": 2026}


@pytest.mark.parametrize("papers", [None, [], "invalid", {}, [None, {}, {"year": True}, {"year": 2025.5}],
                                    [{"year": 1800}, {"year": 2200}], [{"year": 2030}]])
def test_empty_invalid_or_only_unsupported_years_have_valid_fallback(papers):
    summary = storage.dataset_summary(dataset(papers, report="invalid"))
    assert summary["analysis_defaults"] == {"start_year": 2021, "end_year": 2025}


def test_invalid_search_range_falls_back_to_valid_paper_years():
    summary = storage.dataset_summary(dataset([{"year": "2024"}, {"year": None}], {
        "discovery_request": {"start_year": 2026, "end_year": 2021},
        "start_year": "invalid", "end_year": 2025, "start_month": "bad", "end_month": []}))
    assert summary["analysis_defaults"] == {"start_year": 2024, "end_year": 2024}
