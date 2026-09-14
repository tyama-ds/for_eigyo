"""Bounded EPO OPS bibliography search; credentials and tokens are never persisted.

API contract: https://ops.epo.org/wsdl/ops.yaml
Reference: https://link.epo.org/web/searching-for-patents/data/en-ops-v3.2-documentation-version-1.3.20.pdf
Only explicit OPS configuration enables outbound requests. CSV discovery needs none.
"""
import hashlib
import json
import re
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import httpx

from classification_explorer import parse_codes


TOKEN_URL = 'https://ops.epo.org/3.2/auth/accesstoken'
SEARCH_URL = 'https://ops.epo.org/3.2/rest-services/published-data/search/biblio'
MAX_RESULTS = 25
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TOKEN_CACHE = {}
_TOKEN_LOCK = threading.Lock()


class OPSError(ValueError):
    """A redacted, user-facing provider failure."""


def ops_settings_status(settings):
    """Return only presence flags; suitable for the browser's settings status."""
    key = bool(str(settings.get('ops_key') or '').strip())
    secret = bool(str(settings.get('ops_secret') or '').strip())
    return {'configured': key and secret, 'key_set': key, 'secret_set': secret,
            'provider': 'epo_ops', 'registration_url': 'https://developers.epo.org/'}


def _text(node):
    return re.sub(r'\s+', ' ', ' '.join(node.itertext())).strip() if node is not None else ''


def _child_text(node, name):
    return _text(node.find('./{*}' + name)) if node is not None else ''


def _language(nodes):
    nodes = list(nodes)
    return next((n for n in nodes if n.get('lang', '').lower() == 'en'), nodes[0] if nodes else None)


def _ipc_codes(document):
    codes, raw_values, unparsed = [], [], []
    for item in document.findall('.//{*}classification-ipcr'):
        raw = _child_text(item, 'text')
        if not raw:
            raw = ''.join(_child_text(item, key) for key in ('section', 'class', 'subclass', 'main-group'))
            subgroup = _child_text(item, 'subgroup')
            raw += '/' + subgroup if subgroup else ''
        raw_values.append(raw)
    for item in document.findall('.//{*}classification-ipc'):
        for name in ('main-classification', 'further-classification'):
            raw_values.extend(_text(n) for n in item.findall('./{*}' + name))
    for raw in raw_values:
        # ST.36's text form appends version/date/status metadata after the symbol.
        match = re.match(r'^\s*([A-H]\s*\d{2}\s*[A-Z]\s*\d{1,4}\s*/\s*\d{1,6})(?=\s|$)', raw.upper())
        try:
            parsed = parse_codes('IPC', match[1] if match else raw)
        except ValueError:
            unparsed.append(raw[:300])
            continue
        for code in parsed:
            if code not in codes:
                codes.append(code)
    return codes, raw_values, unparsed


def parse_ops_xml(content, limit=MAX_RESULTS):
    """Parse the namespace-qualified OPS search/biblio exchange-document response."""
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError('OPSの応答が4MBを超えました。検索条件を絞ってください。')
    try:
        xml = content.decode('utf-8-sig') if isinstance(content, bytes) else content
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', xml, re.I):
            raise ValueError()
        root = ET.fromstring(xml)
    except (ValueError, ET.ParseError, UnicodeError):
        raise ValueError('OPSの検索応答をXMLとして読み取れません。') from None
    search = root if root.tag.rsplit('}', 1)[-1] == 'biblio-search' else root.find('.//{*}biblio-search')
    if search is None:
        raise ValueError('OPSの応答に検索結果がありません。検索式と接続を確認してください。')
    raw_total = search.get('total-result-count', '')
    total = int(raw_total) if raw_total.isdigit() else None
    patents, seen, unavailable = [], set(), 0
    documents = search.findall('.//{*}exchange-document')
    for document in documents:
        if document.get('status'):
            unavailable += 1
            continue
        bibliography = document.find('./{*}bibliographic-data')
        reference = document.find('.//{*}publication-reference')
        docdb = reference.find('./{*}document-id[@document-id-type="docdb"]') if reference is not None else None
        country = document.get('country') or _child_text(docdb, 'country')
        number = document.get('doc-number') or _child_text(docdb, 'doc-number')
        kind = document.get('kind') or _child_text(docdb, 'kind')
        identifier = (country + number + kind).upper()
        if not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{1,30}', identifier):
            unavailable += 1
            continue
        if identifier in seen:
            continue
        seen.add(identifier)
        if len(patents) >= limit:
            break
        title = _text(_language(document.findall('.//{*}invention-title')))
        abstract = _text(_language(document.findall('./{*}abstract')))
        publication_date = _child_text(docdb, 'date')
        if not publication_date and reference is not None:
            publication_date = _text(reference.find('.//{*}date'))
        applicants = {}
        parties = document.findall('.//{*}applicant')
        parties.sort(key=lambda n: n.get('data-format') != 'epodoc')
        for applicant in parties:
            name = _text(applicant.find('.//{*}applicant-name/{*}name'))
            if name:
                applicants.setdefault(applicant.get('sequence') or name, name)
        ipc, raw_ipc, unparsed = _ipc_codes(bibliography) if bibliography is not None else ([], [], [])
        family = document.get('family-id', '')[:100]
        patents.append({
            'id': identifier, 'title': title[:12000] or identifier, 'abstract': abstract[:12000],
            'ipc': '; '.join(ipc), 'fterm': '', 'applicant': '; '.join(applicants.values())[:12000],
            'year': publication_date[:4] if re.match(r'^\d{4}', publication_date) else '',
            'family_id': family, 'family': family,
            'source_url': 'https://worldwide.espacenet.com/patent/search?' + urlencode({'q': 'pn=' + identifier}),
            'source': 'EPO OPS', 'ipc_raw': raw_ipc, 'ipc_unparsed': unparsed,
        })
    return {'patents': patents, 'total': total,
            'truncated': (total > len(patents)) if total is not None else len(documents) >= limit,
            'unavailable_count': unavailable, 'total_is_lower_bound': total == 10000}


def _request(client, method, url, *, max_bytes=MAX_RESPONSE_BYTES, **kwargs):
    with client.stream(method, url, **kwargs) as response:
        status = response.status_code
        # Never surface arbitrary provider bodies, proxy details or authorization headers.
        if status in (403, 429, 503):
            raise OPSError(f'OPSがHTTP {status}を返しました。認証・利用上限・サービス状態を確認し、時間をおいて再実行してください。自動再試行は停止しました。')
        if status == 401:
            raise OPSError('OPSの認証に失敗しました。Consumer Key / Secret と登録状態を確認してください。')
        if status >= 300:
            raise OPSError(f'OPSがHTTP {status}を返しました。OPS用の検索式と接続設定を確認してください。')
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise OPSError('OPSの応答サイズが上限を超えました。検索条件を絞ってください。')
            chunks.append(chunk)
        return b''.join(chunks)


def search_ops(query: str, settings: dict, limit: int = 20):
    """One page, at most 25 records; no retries, paging or non-OPS outbound URLs.

    Token acquisition adds one request on a cache miss. The calling discovery loop
    owns its search budget. No settings are changed or saved by this provider.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 2000 or re.search(r'[\x00-\x1f\x7f]', query):
        raise ValueError('OPS検索式は制御文字を含まない1〜2,000文字にしてください。')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError('OPSの1検索あたりの取得件数は1〜25件です。')
    if not ops_settings_status(settings)['configured']:
        raise ValueError('OPS APIが未設定です。CSVで試すか、設定でConsumer Key / Secretを指定してください。')
    key, secret = str(settings['ops_key']).strip(), str(settings['ops_secret']).strip()
    if any(len(value) > 1000 or re.search(r'[\x00-\x1f\x7f]', value) for value in (key, secret)):
        raise ValueError('OPSの認証情報の形式を確認してください。')
    try:
        verify = ssl.create_default_context(cafile=settings['ca_bundle']) if settings.get('ca_bundle') else True
    except (OSError, ssl.SSLError):
        raise ValueError('CA証明書ファイルを読み込めません。パスとPEM形式を確認してください。') from None
    cache_key = hashlib.sha256((key + '\0' + secret).encode()).hexdigest()
    request_count = 0
    try:
        with httpx.Client(proxy=settings.get('proxy') or None, trust_env=False,
                          verify=verify, timeout=httpx.Timeout(30, connect=15), follow_redirects=False) as client:
            with _TOKEN_LOCK:
                cached = _TOKEN_CACHE.get(cache_key)
                if cached and cached['expires'] > time.monotonic():
                    token = cached['token']
                else:
                    body = _request(client, 'POST', TOKEN_URL, max_bytes=65536,
                                    auth=httpx.BasicAuth(key, secret), data={'grant_type': 'client_credentials'},
                                    headers={'Accept': 'application/json'})
                    request_count += 1
                    try:
                        auth_data = json.loads(body)
                        token = auth_data['access_token']
                        lifetime = max(0, min(1200, int(auth_data.get('expires_in', 1199))) - 30)
                        if not isinstance(token, str) or not token or len(token) > 4096 or re.search(r'[\x00-\x20\x7f]', token):
                            raise ValueError()
                    except (KeyError, TypeError, ValueError, OverflowError):
                        raise OPSError('OPSの認証応答を読み取れません。') from None
                    _TOKEN_CACHE.clear()
                    _TOKEN_CACHE[cache_key] = {'token': token, 'expires': time.monotonic() + lifetime}
            body = _request(client, 'GET', SEARCH_URL,
                            params={'q': query.strip(), 'Range': f'1-{limit}'},
                            headers={'Accept': 'application/xml', 'Authorization': 'Bearer ' + token})
            request_count += 1
    except OPSError:
        with _TOKEN_LOCK:
            _TOKEN_CACHE.pop(cache_key, None)
        raise
    except (httpx.HTTPError, OSError, ValueError):
        raise ValueError('OPSへ接続できません。プロキシ、CA証明書、ネットワークを確認してください。') from None
    result = parse_ops_xml(body, limit)
    return {**result, 'query': query.strip(), 'provider': 'epo_ops', 'request_count': request_count}
