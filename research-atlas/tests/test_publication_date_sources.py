from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app import publication_date_sources as sources


@pytest.mark.parametrize(("paper", "expected"), [
    ({"doi": " DOI: 10.1234/ABC.def(2) "}, "10.1234/abc.def(2)"),
    ({"doi": "10.1002/(SICI)1099-0844(199912)17:4<290::AID-CBF849>3.0.CO;2-P"},
     "10.1002/(sici)1099-0844(199912)17:4<290::aid-cbf849>3.0.co;2-p"),
    ({"doi": "https://doi.org/10.1234/a%5Bb%5D%26c%3Dd"}, "10.1234/a[b]&c=d"),
    ({"doi": "https://doi.org/10.1234/ABC"}, "10.1234/abc"),
    ({"doi": "dx.doi.org/10.1234/ABC"}, "10.1234/abc"),
    ({"external_url": "https://publisher.example/article/10.1234/ABC"}, "10.1234/abc"),
    ({"source_link": "https://publisher.example/doi/pdf/10.1234/ABC"}, "10.1234/abc"),
    ({"url": "https://doi.org/10.1234%2FABC"}, "10.1234/abc"),
    ({"link": "https://www.scopus.com/inward/record.uri?eid=2-s2.0-1&doi=10.1234%2FABC&partnerID=40"}, "10.1234/abc"),
    ({"doi": "10.1234/PRIMARY", "url": "https://doi.org/10.1234/other"}, "10.1234/primary"),
    ({"doi": "malformed", "source_link": "https://doi.org/10.1234/fallback"}, "10.1234/fallback"),
    ({"doi": "10.1234/hello\n"}, None),
    ({"doi": "10.1234/hello world"}, None),
    ({"doi": "10.1234/a/../b"}, None),
    ({"doi": "10.1234/a?query=1"}, None),
    ({"doi": "10.1234/a#fragment"}, None),
    ({"doi": "10.1234/first;10.5678/second"}, None),
    ({"doi": "https://doi.org/10.1234/a?doi=10.1234/other"}, None),
    ({"doi": "https://user:pass@doi.org/10.1234/a"}, None),
    ({"doi": "https://doi.org:123/10.1234/a"}, None),
    ({"url": "https://doi.org/10.1234%252Fa"}, None),
    ({"url": "https://doi.org/10.1234/a%0A"}, None),
    ({"url": "file:///10.1234/a"}, None),
    ({"url": "https://doi.org/10.1234/a\\b"}, None),
    ({"url": "https://publisher.example/?doi=10.1234/a"}, None),
    ({"url": "https://www.scopus.com/inward/record.uri?eid=2-s2.0-1"}, None),
    ({"url": "https://www.scopus.com/inward/record.uri?doi=10.1234/a&doi=10.1234/b"}, None),
    ({"url": "https://www.scopus.com/inward/record.uri?doi=10.1234/a&target=https%3A%2F%2Fdoi.org%2F10.1234%2Fb"}, None),
    ({"url": "https://doi.org/10.1234/a", "source_link": "https://doi.org/10.1234/b"}, None),
])
def test_extract_doi_only_unambiguous_metadata(paper, expected):
    assert sources.extract_doi(paper) == expected


class ImmediateGate:
    def __init__(self):
        self.cooldowns = []
        self.acquired = 0
        self.released = 0

    def concurrency(self, pool):
        return 3 if pool == "polite" else 1

    def acquire(self, pool, stop_event):
        if stop_event is not None and stop_event.is_set():
            raise sources._Cancelled()
        self.acquired += 1

    def release(self, pool):
        self.released += 1

    def update(self, pool, headers):
        pass

    def cool_down(self, seconds, pool, *, lower_rate=False):
        self.cooldowns.append((seconds, pool, lower_rate))


@pytest.fixture
def transport(monkeypatch):
    gate = ImmediateGate()
    monkeypatch.setattr(sources, "_GATE", gate)
    state = {"calls": [], "factory": [], "handler": None, "gate": gate}

    def make_client(url, **kwargs):
        state["factory"].append((url, kwargs))

        def handle(request):
            state["calls"].append(request)
            return state["handler"](request)

        return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False, **kwargs)

    monkeypatch.setattr(sources, "http_client", make_client)
    return state


def response(message=None, **kwargs):
    return httpx.Response(200, json={"message": message or {"DOI": "10.1234/example", "published": {"date-parts": [[2024, 3, 2]]}}}, **kwargs)


def test_fetch_precise_publication_candidates_not_registration_dates(transport):
    message = {"DOI": "10.1234/EXAMPLE", "title": ["A paper"],
               "published-online": {"date-parts": [[2023, 12, 29]]},
               "published-print": {"date-parts": [[2024, 2]]},
               "published": {"date-parts": [[2024]]}, "issued": {"date-parts": [[2024, 2, 1]]},
               "created": {"date-parts": [[2020, 1, 1]]}, "deposited": {"date-parts": [[2021, 1, 1]]},
               "indexed": {"date-parts": [[2022, 1, 1]]}, "accepted": {"date-parts": [[2019, 1, 1]]}}
    transport["handler"] = lambda request: response(message)
    with sources.CrossrefDateClient("researcher@example.org") as client:
        assert client.concurrency == 3
        result = client.fetch("10.1234/EXAMPLE")
    assert result["status"] == "found"
    assert result["candidates"] == [
        {"date": "2023-12-29", "precision": "day", "kind": "published-online"},
        {"date": "2024-02", "precision": "month", "kind": "published-print"},
        {"date": "2024", "precision": "year", "kind": "published"},
        {"date": "2024-02-01", "precision": "day", "kind": "issued"},
    ]
    assert result["source_url"] == "https://api.crossref.org/works/10.1234%2Fexample"
    assert transport["calls"][0].url.host == "api.crossref.org"
    assert transport["calls"][0].url.params["mailto"] == "researcher@example.org"
    assert "researcher@example.org" not in json.dumps(result)
    assert transport["factory"][0][0] == sources._ENDPOINT
    assert transport["factory"][0][1]["timeout"].connect == 10
    assert transport["gate"].acquired == transport["gate"].released == 1


def test_future_dates_are_stored_as_exclusions_never_applicable(transport):
    future = date.today() + timedelta(days=1)
    transport["handler"] = lambda request: response({"DOI": "10.1234/example",
        "published-online": {"date-parts": [[future.year, future.month, future.day]]},
        "published-print": {"date-parts": [[date.today().year + 1]]},
        "issued": {"date-parts": [[2024]]}})
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["candidates"] == [{"date": "2024", "precision": "year", "kind": "issued"}]
    assert len(result["excluded_candidates"]) == 2
    assert all(item["reason"] == "future" for item in result["excluded_candidates"])


@pytest.mark.parametrize("parts", [[], [[2024, 2, 30]], [[2024, 13]], [[1499]], [[2024, True]],
                                  [["2024", 3]], [[2024], [2025]], [[2024, 1, 1, 1]], "2024-03-01"])
def test_invalid_date_parts_do_not_fabricate_a_date(transport, parts):
    transport["handler"] = lambda request: response({"DOI": "10.1234/example", "published": {"date-parts": parts},
                                                    "created": {"date-parts": [[2024, 1, 1]]}})
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["status"] == "not_found"
    assert result["error_kind"] == "no_publication_date"
    assert result["candidates"] == []


@pytest.mark.parametrize(("status", "kind", "retryable"), [(404, "not_found", False), (403, "blocked", True),
    (301, "redirect", False), (302, "redirect", False), (400, "http_error", False)])
def test_permanent_responses_are_not_retried_or_redirected(transport, status, kind, retryable):
    transport["handler"] = lambda request: httpx.Response(status, headers={"Location": "https://elsewhere.invalid/secret"})
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["error_kind"] == kind
    assert result["retryable"] is retryable
    assert len(transport["calls"]) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_errors_have_three_attempt_maximum(transport, status):
    transport["handler"] = lambda request: httpx.Response(status)
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["retryable"]
    assert len(transport["calls"]) == 3
    assert [entry[0] for entry in transport["gate"].cooldowns] == [1.0, 2.0, 4.0]
    assert transport["gate"].acquired == transport["gate"].released == 3


def test_transient_errors_can_recover(transport):
    transport["handler"] = lambda request: httpx.Response(503) if len(transport["calls"]) < 3 else response()
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["status"] == "found"
    assert len(transport["calls"]) == 3


@pytest.mark.parametrize("error", [httpx.ReadTimeout("secret URL not exposed"), httpx.ConnectError("secret URL not exposed")])
def test_network_failures_are_sanitized_and_retried(transport, error):
    def fail(request):
        raise error
    transport["handler"] = fail
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["error_kind"] in {"timeout", "network_error"}
    assert result["retryable"]
    assert len(transport["calls"]) == 3
    assert "secret" not in json.dumps(result)


def test_long_retry_after_returns_immediately_for_worker_to_pause(transport):
    transport["handler"] = lambda request: httpx.Response(429, headers={"Retry-After": "120"})
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["error_kind"] == "rate_limited"
    assert result["retry_after_seconds"] == 120
    assert len(transport["calls"]) == 1
    assert transport["gate"].cooldowns == [(120, "public", True)]


def test_http_date_retry_after_is_respected():
    value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    assert 118 <= sources._retry_after(value) <= 120
    assert sources._retry_after("nonsense") is None


def test_doi_mismatch_does_not_attach_another_papers_date(transport):
    transport["handler"] = lambda request: response({"DOI": "10.1234/another", "published": {"date-parts": [[2024, 1, 1]]}})
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["error_kind"] == "doi_mismatch"
    assert result["candidates"] == []


@pytest.mark.parametrize("content", [b"<html>error</html>", b"[]", b'{"message": []}', b'\xff'])
def test_malformed_json_responses_are_sanitized(transport, content):
    transport["handler"] = lambda request: httpx.Response(200, content=content)
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["error_kind"] == "invalid_response"
    assert len(transport["calls"]) == 1


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = 0
    def __iter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk


def test_timeout_after_200_headers_is_still_a_retryable_network_failure(transport):
    class InterruptedBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"message":'
            raise httpx.ReadTimeout("private transport details")

    transport["handler"] = lambda request: httpx.Response(200, stream=InterruptedBody())
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["error_kind"] == "timeout"
    assert result["retryable"]
    assert len(transport["calls"]) == 3
    assert transport["gate"].acquired == transport["gate"].released == 3


def test_streamed_response_size_is_bounded_even_without_content_length(transport):
    stream = Chunks([b"x" * 65536] * 100)
    transport["handler"] = lambda request: httpx.Response(200, stream=stream)
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["error_kind"] == "response_too_large"
    assert stream.read == 33


def test_content_length_limit_rejects_before_reading(transport):
    stream = Chunks([b"secret"])
    transport["handler"] = lambda request: httpx.Response(200, headers={"Content-Length": str(sources._MAX_RESPONSE_BYTES + 1)}, stream=stream)
    result = sources.CrossrefDateClient().fetch("10.1234/example")
    assert result["error_kind"] == "response_too_large"
    assert stream.read == 0


def test_cancellation_prevents_network_calls(transport):
    event = threading.Event()
    event.set()
    result = sources.CrossrefDateClient(stop_event=event).fetch("10.1234/example")
    assert result["error_kind"] == "cancelled"
    assert transport["calls"] == []


def test_invalid_doi_prevents_network_calls(transport):
    result = sources.CrossrefDateClient().fetch("https://internal.invalid/anything")
    assert result["error_kind"] == "invalid_doi"
    assert transport["calls"] == []


@pytest.mark.parametrize("email", ["bad", "a@b", "x@example.org\nOther: injected", "a b@example.org", "x@example.org\n", None])
def test_invalid_contact_rejected(email):
    with pytest.raises(ValueError, match="メールアドレス"):
        sources.CrossrefDateClient(email)


def test_gate_honors_lower_server_limits_and_never_raises_default_limits():
    gate = sources._HostGate()
    gate.update("polite", httpx.Headers({"x-rate-limit-limit": "4", "x-rate-limit-interval": "2s", "x-concurrency-limit": "2"}))
    assert gate.limits["polite"] == (2, 2)
    gate.update("polite", httpx.Headers({"x-rate-limit-limit": "1000", "x-rate-limit-interval": "1s", "x-concurrency-limit": "100"}))
    assert gate.limits["polite"] == (2, 2)
    gate.update("public", httpx.Headers({"x-rate-limit-limit": "nonsense", "x-rate-limit-interval": "0s", "x-concurrency-limit": "0"}))
    assert gate.limits["public"] == (5, 1)


@pytest.mark.parametrize(("limit", "interval"), [("1", "9" * 400 + "s"), ("9" * 400, "1s"),
    ("0.000" + "0" * 320 + "1", "1s"), ("1", "0.000" + "0" * 320 + "1s")])
def test_nonfinite_or_unrepresentable_header_rates_are_ignored(limit, interval):
    gate = sources._HostGate()
    gate.update("public", httpx.Headers({"x-rate-limit-limit": limit, "x-rate-limit-interval": interval}))
    assert gate.limits["public"] == (5, 1)


def test_long_gate_cooldown_defers_without_sleeping():
    gate = sources._HostGate()
    gate.cool_down(120, "public")
    with pytest.raises(sources._Deferred) as error:
        gate.acquire("public", None)
    assert error.value.seconds > 119
    assert sum(gate.active.values()) == 0


def test_gate_wait_is_cancellable():
    gate = sources._HostGate()
    event = threading.Event()
    gate.cool_down(1, "public")
    timer = threading.Timer(0.02, event.set)
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(sources._Cancelled):
            gate.acquire("public", event)
    finally:
        timer.join()
    assert time.monotonic() - start < 0.5


@pytest.mark.parametrize(("email", "expected_max"), [("", 1), ("researcher@example.org", 3)])
def test_independent_clients_share_host_concurrency(monkeypatch, email, expected_max):
    gate = sources._HostGate()
    # Accelerate spacing only; real concurrency admission remains under test.
    gate.limits = {"public": (1000, 1), "polite": (1000, 3)}
    monkeypatch.setattr(sources, "_GATE", gate)
    counters = {"active": 0, "maximum": 0}
    lock = threading.Lock()

    def handle(request):
        with lock:
            counters["active"] += 1
            counters["maximum"] = max(counters["maximum"], counters["active"])
        time.sleep(0.03)
        with lock:
            counters["active"] -= 1
        return response()

    monkeypatch.setattr(sources, "http_client", lambda url, **kwargs: httpx.Client(transport=httpx.MockTransport(handle), **kwargs))
    with sources.CrossrefDateClient(email) as first, sources.CrossrefDateClient(email) as second:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit((first if index % 2 else second).fetch, "10.1234/example") for index in range(6)]
            assert all(future.result()["status"] == "found" for future in futures)
    assert counters["maximum"] == expected_max
