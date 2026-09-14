"""Offline bilingual display captions, with explicit bounded PMGS cache updates.

The Japanese publication is a dated PMGS snapshot, not a claim that its wording
has been checked against the WIPO 2026.01 English edition. Codes are cross-checked
against that edition; neither hierarchy nor classification selection is changed.
"""
from datetime import datetime, timezone
from functools import lru_cache
import gzip
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import ssl
import threading
import unicodedata

import httpx

BASE = 'https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/IPC/ja/'
BUNDLE = Path(__file__).resolve().parent / 'resources' / 'ipc' / 'ipc_ja_pmgs.json.gz'
MAX_TABLES = 4
MAX_BYTES = 4 * 1024 * 1024
_cache_path = None
_cache = {}
_lock = threading.RLock()

# Display aids written for this app; these are not an official English F-term
# publication. Keys and Japanese definitions remain the bundled PMGS originals.
FTERM_EN = {
    '5H029': 'Secondary batteries (other storage batteries)',
    '5H029AJ00': 'Purpose and effects (at least one must be assigned)',
    '5H029AJ01': 'Battery performance',
    '5H029AJ06': 'Reduction of internal resistance',
    '5H029AK00': 'Positive-electrode active materials (at least one must be assigned)',
    '5H029AL00': 'Negative-electrode active materials (at least one must be assigned)',
    '5H029AM00': 'Electrolyte materials (one or more assigned)',
    '5H029AM01': 'Non-aqueous electrolytes',
    '5H029AM09': 'Molten salts',
    '5H029AM11': 'Solid electrolytes',
    '5H029AM12': 'Alkali-metal-ion-conducting solid electrolytes',
    '5H029BJ00': 'Shape, structure, auxiliary equipment or means',
    '5H029CJ00': 'Manufacturing and processing',
    '5H029DJ00': 'Battery components and their forms (distinctive features)',
    '5H029EJ00': 'Component materials (excluding active materials and electrolytes)',
    '5H029HJ00': 'Numerical limits, relative magnitudes and specified ranges',
}


def configure_cache(data_dir):
    """Load only the dedicated display-caption cache; no credentials are stored."""
    global _cache_path, _cache
    with _lock:
        _cache_path = Path(data_dir) / 'ipc-japanese-captions.json'
        _cache = {}
        if _cache_path.is_file():
            try:
                raw = json.loads(_cache_path.read_text(encoding='utf-8'))
                _cache = {k: v for k, v in raw.items() if _valid_caption(k, v)}
            except (OSError, ValueError, TypeError, AttributeError):
                _cache = {}


def _valid_caption(code, row):
    return (isinstance(row, dict) and row.get('code') == code and
            isinstance(row.get('title_ja'), str) and bool(row['title_ja'].strip()) and
            str(row.get('title_ja_source', '')).startswith(BASE) and
            row.get('title_ja_status') == 'official_translation')


@lru_cache(maxsize=1)
def _bundle():
    if not BUNDLE.is_file():
        return {'metadata': {'count': 0, 'complete': False}, 'records': {}}
    with gzip.open(BUNDLE, 'rt', encoding='utf-8') as handle:
        return json.load(handle)


def label_fields(kind, code):
    if kind == 'F-term':
        title = FTERM_EN.get(code, '')
        return {'title_en': title, 'title_en_status': 'app_translation' if title else 'unavailable',
                'title_en_source': '', 'title_en_version': ''}
    if kind != 'IPC':
        return {}
    with _lock:
        row = _cache.get(code) or _bundle()['records'].get(code)
    if row:
        return {k: v for k, v in row.items() if k.startswith('title_ja')}
    return {'title_ja': '', 'title_ja_status': 'unavailable', 'title_ja_source': '', 'title_ja_version': ''}


def metadata():
    result = dict(_bundle()['metadata'])
    with _lock:
        result['cached_count'] = len(_cache)
    return result


def _plain(value):
    return re.sub(r'\s+', ' ', value).strip().lstrip('・').strip()


class CaptionParser(HTMLParser):
    """Read named IPC rows and their own explanation cell, ignoring notes/links."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.code = None
        self.capture = False
        self.parts = []
        self.rows = {}

    def _finish(self):
        title = _plain(''.join(self.parts))
        if self.code and title:
            self.rows[self.code] = title
        self.capture = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tr':
            self._finish()
            self.code = None
        if tag == 'a' and attrs.get('name'):
            code = re.sub(r'\s+', '', unicodedata.normalize('NFKC', attrs['name'])).upper()
            if re.fullmatch(r'[A-H](?:\d{2}(?:[A-Z](?:\d{1,4}/\d{2,6})?)?)?', code):
                self.code = code
        if tag == 'td':
            self._finish()
            self.capture = attrs.get('class') in ('explanationArea', 'explanationListArea')
        elif tag == 'img' and self.capture:
            self.parts.append('[図]')

    def handle_endtag(self, tag):
        if tag in ('td', 'tr'):
            self._finish()

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def parse_table(html):
    parser = CaptionParser()
    parser.feed(html)
    parser.close()
    return parser.rows


def table_path(node):
    """Choose a published PMGS page using verified official hierarchy only."""
    code, level = node['code'], node['level']
    if level == 'section':
        return 'ipcSection/ipcSection.html'
    if level == 'class':
        return f'ipcClass/ipcClass{code[0]}.html'
    if level == 'subclass':
        return f'ipcSubClass/ipcSubClass{code[:3]}.html'
    if level == 'main_group':
        return f'ipcMainGroup/ipcMainGroup{code[:4]}.html'
    main = next((c for c in node['ancestors'] if c.endswith('/00')), None)
    if not main:
        raise ValueError('公式IPCの親分類を確認できません。')
    return 'ipcList/ipcList' + main.replace('/', '_') + '.html'


def obtain_table(client, path):
    """Fixed host, bounded body, no redirects or retries."""
    with client.stream('GET', BASE + path) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise ValueError('日本語分類表の応答が4MBを超えました。')
    return parse_table(bytes(content).decode('utf-8-sig'))


def caption_records(rows, path, valid_codes, retrieved_at=None):
    stamp = retrieved_at or datetime.now(timezone.utc).date().isoformat()
    return {code: {'kind': 'IPC', 'code': code, 'title_ja': title,
                  'title_ja_status': 'official_translation', 'title_ja_source': BASE + path,
                  'title_ja_version': 'PMGS snapshot ' + stamp}
            for code, title in rows.items() if code in valid_codes and title}


def fetch_translations(codes, settings, cache_dir=None):
    """Explicit UI action: at most 48 known codes and four pages with short timeouts."""
    import classification_catalog as catalog
    if not isinstance(codes, list) or not 1 <= len(codes) <= 48 or not all(isinstance(c, str) for c in codes):
        raise ValueError('IPCコードを1～48件で指定してください。')
    nodes = []
    for code in dict.fromkeys(codes):
        node = catalog.lookup('IPC', code)
        if node is None:
            raise ValueError('現行の内蔵IPC辞書で確認できないコードです。')
        nodes.append(node)
    if cache_dir is not None and _cache_path != Path(cache_dir) / 'ipc-japanese-captions.json':
        configure_cache(cache_dir)
    paths = list(dict.fromkeys(table_path(n) for n in nodes if not label_fields('IPC', n['code']).get('title_ja')))
    warnings = []
    if len(paths) > MAX_TABLES:
        warnings.append('一度に4表まで取得します。未収録が残る場合はもう一度取得できます。')
    if paths:
        try:
            verify = ssl.create_default_context(cafile=settings['ca_bundle']) if settings.get('ca_bundle') else True
        except (OSError, ssl.SSLError) as exc:
            raise ValueError('CA証明書の設定を確認してください。') from exc
        updates = {}
        valid_codes = catalog._catalog()[1]
        # Each phase is bounded; overall four small public pages are requested only.
        try:
            client = httpx.Client(proxy=settings.get('proxy') or None, verify=verify, timeout=5.0,
                                  trust_env=False, follow_redirects=False,
                                  headers={'User-Agent': 'PatentAtlas-ClassificationCaptions/1.0'})
        except (ValueError, httpx.HTTPError) as exc:
            raise ValueError('日本語分類の取得に使うプロキシ設定を確認してください。') from exc
        with client:
            for path in paths[:MAX_TABLES]:
                try:
                    rows = obtain_table(client, path)
                    updates.update(caption_records(rows, path, valid_codes))
                    if not rows:
                        warnings.append('取得した分類表から日本語名称を読み取れませんでした。')
                except (httpx.HTTPError, UnicodeError, ValueError):
                    warnings.append('日本語分類表を取得できませんでした。接続・プロキシ設定を確認してください。')
                    break
        if updates:
            with _lock:
                _cache.update(updates)
                if _cache_path:
                    _cache_path.parent.mkdir(parents=True, exist_ok=True)
                    temp = _cache_path.with_suffix('.tmp')
                    temp.write_text(json.dumps(_cache, ensure_ascii=False), encoding='utf-8')
                    temp.replace(_cache_path)
    translations = []
    missing = []
    for node in nodes:
        # Re-read the lightweight display projection after cache updates so newly
        # obtained parent captions are available without mutating stored records.
        node = catalog.lookup('IPC', node['code'])
        fields = label_fields('IPC', node['code'])
        if not fields.get('title_ja'):
            missing.append(node['code'])
        translations.append({'kind': 'IPC', 'code': node['code'], 'title_en': node['title'],
                             'title_en_status': 'official', 'title_en_source': node['source'],
                             'title_en_version': node['version'],
                             **{k: v for k, v in node.items() if k.startswith('title_') and '_parent' in k},
                             **fields})
    return {'translations': translations, 'missing': missing, 'warnings': list(dict.fromkeys(warnings)),
            'metadata': metadata()}
