"""Bounded DOI metadata lookups, without fetching user-supplied web addresses.

Crossref's single-record API currently permits public 5/s (one concurrent) and
polite 10/s (three concurrent). Advertised lower limits and Retry-After always
take precedence. Publication dates remain distinct from registration timestamps.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx

from .connection_settings import http_client


_ENDPOINT = "https://api.crossref.org/works/"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_INLINE_WAIT = 5.0
# Legacy SICI DOIs can contain angle/square brackets, ampersands and equals.
# They are safe identifiers here because the entire DOI is URL-quoted at a
# fixed API endpoint. URL query/fragment delimiters remain ambiguous and fail.
_DOI = re.compile(r"10\.\d{4,9}/[A-Za-z0-9._;()/+:\-<>\[\]&=]+", re.ASCII)
_DOI_START = re.compile(r"(?<![A-Za-z0-9])10\.\d{4,9}/", re.ASCII)
_KINDS = ("published-online", "published-print", "published", "issued")
_DEFAULTS = {"public": (5.0, 1), "polite": (10.0, 3)}


def _raw_doi(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    # Check before strip: embedded line breaks are not harmless whitespace.
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    value = value.strip()
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    if not _DOI.fullmatch(value) or len(_DOI_START.findall(value)) != 1:
        return None
    if any(part in {".", ".."} for part in value.split("/")):
        return None
    return value.casefold()


def _doi_from_value(value: object, *, allow_plain: bool) -> str | None:
    if not isinstance(value, str) or len(value) > 8192:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    value = value.strip()
    if allow_plain and (doi := _raw_doi(value)):
        return doi
    # Bare doi.org links are common in exported DOI columns.
    if re.match(r"^(?:dx\.)?doi\.org/", value, flags=re.I):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.port not in {None, 80, 443} or parsed.fragment or "\\" in value:
            return None
        host = parsed.hostname.casefold().rstrip(".")
        path = unquote(parsed.path, errors="strict")
    except (ValueError, UnicodeError):
        return None
    if "%" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        return None
    if parsed.query:
        # Scopus exports can supply a DOI in Link even when the DOI cell is empty.
        # Never mine arbitrary query strings or fetch the Scopus link itself.
        if host not in {"scopus.com", "www.scopus.com"}:
            return None
        try:
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=24)
        except ValueError:
            return None
        values = [item for key, item in pairs if key.casefold() == "doi"]
        if len(values) != 1 or _DOI_START.search(path):
            return None
        if any(_DOI_START.search(item) or "://" in item for key, item in pairs if key.casefold() != "doi"):
            return None
        return _raw_doi(values[0])
    if host in {"doi.org", "dx.doi.org"}:
        return _raw_doi(path.lstrip("/"))
    starts = list(_DOI_START.finditer(path))
    if len(starts) != 1 or starts[0].start() == 0 or path[starts[0].start() - 1] != "/":
        return None
    return _raw_doi(path[starts[0].start():])


def extract_doi(paper: dict) -> str | None:
    """Read a normalized DOI; ambiguous identifiers and unsafe URLs stay unknown."""
    if doi := _doi_from_value(paper.get("doi"), allow_plain=True):
        return doi
    candidates = {_doi_from_value(paper.get(key), allow_plain=False)
                  for key in ("external_url", "source_link", "url", "link", "Link")}
    candidates.discard(None)
    return next(iter(candidates)) if len(candidates) == 1 else None


class _Cancelled(Exception):
    pass


class _Deferred(Exception):
    def __init__(self, seconds: float):
        self.seconds = max(0.0, seconds)


class _HostGate:
    """One process-wide gate, shared even across independent import jobs.

    Mixed public/polite traffic is deliberately conservative: public calls cannot
    overlap other calls, and any server cooldown applies to the whole host.
    """
    def __init__(self):
        self.condition = threading.Condition()
        self.limits = dict(_DEFAULTS)
        self.active: Counter[str] = Counter()
        self.last_start = -math.inf
        self.blocked_until = 0.0

    def concurrency(self, pool: str) -> int:
        with self.condition:
            return self.limits[pool][1]

    def acquire(self, pool: str, stop_event: threading.Event | None):
        with self.condition:
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise _Cancelled()
                now = time.monotonic()
                cooldown = self.blocked_until - now
                if cooldown > _MAX_INLINE_WAIT:
                    raise _Deferred(cooldown)
                rate, concurrent = self.limits[pool]
                active_limits = [self.limits[key][1] for key, count in self.active.items() if count]
                cap = min([concurrent, *active_limits])
                delay = max(cooldown, self.last_start + 1.0 / rate - now, 0.0)
                if delay > _MAX_INLINE_WAIT:
                    raise _Deferred(delay)
                if sum(self.active.values()) < cap and delay <= 0:
                    self.active[pool] += 1
                    self.last_start = now
                    return
                self.condition.wait(timeout=min(delay if delay > 0 else 0.1, 0.1))

    def release(self, pool: str):
        with self.condition:
            self.active[pool] -= 1
            self.condition.notify_all()

    def update(self, pool: str, headers: httpx.Headers):
        with self.condition:
            rate, concurrent = self.limits[pool]
            advertised = headers.get("x-rate-limit-limit", "")
            interval = headers.get("x-rate-limit-interval", "")
            if re.fullmatch(r"\d+(?:\.\d+)?", advertised) and re.fullmatch(r"\d+(?:\.\d+)?s", interval):
                denominator = float(interval[:-1])
                numerator = float(advertised)
                if math.isfinite(denominator) and math.isfinite(numerator) and denominator > 0 and numerator > 0:
                    advertised_rate = numerator / denominator
                    if advertised_rate > 0 and math.isfinite(advertised_rate) and math.isfinite(1.0 / advertised_rate):
                        rate = min(rate, advertised_rate)
            advertised_concurrency = headers.get("x-concurrency-limit", "")
            if re.fullmatch(r"\d+", advertised_concurrency) and int(advertised_concurrency) > 0:
                concurrent = min(concurrent, int(advertised_concurrency))
            self.limits[pool] = (rate, concurrent)
            self.condition.notify_all()

    def cool_down(self, seconds: float, pool: str, *, lower_rate: bool = False):
        with self.condition:
            self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)
            if lower_rate:
                rate, concurrent = self.limits[pool]
                self.limits[pool] = (max(min(rate, 0.1), rate / 2.0), min(concurrent, 1))
            self.condition.notify_all()


_GATE = _HostGate()


def _retry_after(value: str) -> float | None:
    value = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        seconds = float(value)
        return seconds if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _publication_candidates(message: dict) -> tuple[list[dict], list[dict]]:
    candidates, excluded = [], []
    today = date.today()
    for kind in _KINDS:
        field = message.get(kind)
        dates = field.get("date-parts") if isinstance(field, dict) else None
        if not isinstance(dates, list) or len(dates) != 1 or not isinstance(dates[0], list):
            continue
        parts = dates[0]
        if not 1 <= len(parts) <= 3 or any(type(part) is not int for part in parts):
            continue
        if not 1500 <= parts[0] <= 2100:
            continue
        try:
            date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
        except ValueError:
            continue
        item = {"date": "-".join(f"{part:04d}" if index == 0 else f"{part:02d}" for index, part in enumerate(parts)),
                "precision": ("year", "month", "day")[len(parts) - 1], "kind": kind}
        if tuple(parts) > (today.year, today.month, today.day)[:len(parts)]:
            excluded.append({**item, "reason": "future"})
        else:
            candidates.append(item)
    return candidates, excluded


class CrossrefDateClient:
    """Thread-safe fixed-endpoint client; no credentials are written to results."""
    def __init__(self, contact_email: str = "", stop_event: threading.Event | None = None):
        if not isinstance(contact_email, str) or any(ord(char) < 32 or ord(char) == 127 for char in contact_email):
            raise ValueError("Crossref の連絡先には有効なメールアドレスを指定してください。")
        contact_email = contact_email.strip()
        if contact_email and (len(contact_email) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}", contact_email)):
            raise ValueError("Crossref の連絡先には有効なメールアドレスを指定してください。")
        self._contact_email = contact_email
        self._pool = "polite" if contact_email else "public"
        self._stop_event = stop_event
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()

    @property
    def concurrency(self) -> int:
        return _GATE.concurrency(self._pool)

    def __enter__(self):
        self._transport()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self):
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _transport(self) -> httpx.Client:
        with self._client_lock:
            if self._client is None:
                self._client = http_client(_ENDPOINT, timeout=httpx.Timeout(20.0, connect=10.0),
                    headers={"Accept": "application/json", "User-Agent": "ResearchAtlas/2 publication-date-enrichment"},
                    limits=httpx.Limits(max_connections=3, max_keepalive_connections=3))
            return self._client

    def fetch(self, doi: str) -> dict:
        normalized = _raw_doi(doi)
        base = {"doi": normalized or "", "status": "error", "provider": "crossref",
                "source_url": _ENDPOINT + quote(normalized, safe="") if normalized else "",
                "fetched_at": datetime.now(timezone.utc).isoformat(), "candidates": [], "retryable": False}
        if normalized is None:
            return {**base, "error_kind": "invalid_doi"}
        params = {"mailto": self._contact_email} if self._contact_email else None
        for attempt in range(3):
            acquired = False
            status, retry_after = None, None
            try:
                _GATE.acquire(self._pool, self._stop_event)
                acquired = True
                with self._transport().stream("GET", base["source_url"], params=params) as response:
                    status = response.status_code
                    _GATE.update(self._pool, response.headers)
                    retry_after = _retry_after(response.headers.get("retry-after", ""))
                    # Set host-wide backoff before releasing this request's slot:
                    # another waiting worker must not race past a received 429.
                    if status == 403:
                        _GATE.cool_down(retry_after or 60.0, self._pool)
                    elif status == 429 or 500 <= status < 600:
                        _GATE.cool_down(max(retry_after or 0.0, float(2 ** attempt)), self._pool,
                                        lower_rate=status == 429)
                    if status == 200:
                        length = response.headers.get("content-length", "")
                        if length.isdigit() and int(length) > _MAX_RESPONSE_BYTES:
                            return {**base, "error_kind": "response_too_large"}
                        chunks, size = [], 0
                        for chunk in response.iter_bytes(chunk_size=65536):
                            if self._stop_event is not None and self._stop_event.is_set():
                                raise _Cancelled()
                            size += len(chunk)
                            if size > _MAX_RESPONSE_BYTES:
                                return {**base, "error_kind": "response_too_large"}
                            chunks.append(chunk)
                        try:
                            payload = json.loads(b"".join(chunks))
                        except (ValueError, UnicodeError, RecursionError):
                            return {**base, "error_kind": "invalid_response"}
                        message = payload.get("message") if isinstance(payload, dict) else None
                        if not isinstance(message, dict):
                            return {**base, "error_kind": "invalid_response"}
                        if _raw_doi(message.get("DOI")) != normalized:
                            return {**base, "error_kind": "doi_mismatch"}
                        candidates, excluded = _publication_candidates(message)
                        title = message.get("title")
                        title = title[0] if isinstance(title, list) and title else ""
                        result = {**base, "status": "found" if candidates else "not_found", "candidates": candidates,
                                  "excluded_candidates": excluded}
                        if isinstance(title, str):
                            result["title"] = re.sub(r"[\x00-\x1f\x7f]", " ", title).strip()[:1000]
                        if not candidates:
                            result["error_kind"] = "no_publication_date"
                        return result
            except _Cancelled:
                return {**base, "error_kind": "cancelled", "retryable": True}
            except _Deferred as error:
                return {**base, "error_kind": "rate_limited", "retryable": True,
                        "retry_after_seconds": round(error.seconds, 3)}
            except httpx.TimeoutException:
                status = None
                error_kind = "timeout"
            except httpx.RequestError:
                status = None
                error_kind = "network_error"
            finally:
                if acquired:
                    _GATE.release(self._pool)
            if status == 404:
                return {**base, "status": "not_found", "error_kind": "not_found"}
            if status == 403:
                return {**base, "error_kind": "blocked", "retryable": True,
                        "retry_after_seconds": retry_after or 60.0}
            if status is not None and 300 <= status < 400:
                return {**base, "error_kind": "redirect"}
            if status is not None and status != 429 and not 500 <= status < 600:
                return {**base, "error_kind": "http_error", "http_status": status}
            if status is not None:
                error_kind = "rate_limited" if status == 429 else "server_error"
            delay = max(retry_after or 0.0, float(2 ** attempt))
            if status is None:
                _GATE.cool_down(delay, self._pool)
            if attempt == 2 or delay > _MAX_INLINE_WAIT:
                return {**base, "error_kind": error_kind, "retryable": True, "retry_after_seconds": delay}
        raise AssertionError("bounded retry loop exhausted without result")
