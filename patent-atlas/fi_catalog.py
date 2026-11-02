"""A bounded, independent FI snapshot. Never validate FI against the IPC table."""
from functools import lru_cache
import copy
import gzip
import json
from pathlib import Path
import re
import unicodedata

PATTERN = r'[A-H]\d{2}[A-Z]\d{1,4}/\d{2,6}(?:,\d{3})?(?:@[A-Z])?'
DATA = Path(__file__).resolve().parent / 'resources' / 'fi' / 'fi_ev_202604.json.gz'


def normalize(code):
    code = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(code))).upper()
    # Conventional display notation -> the documented J-PlatPat FI notation.
    code = re.sub(r'(?<=[0-9])([A-Z])$', r'@\1', code)
    if not re.fullmatch(PATTERN, code):
        raise ValueError('FIは完全なコードで入力してください（例 B60L15/20@A）。')
    return code


@lru_cache(maxsize=1)
def _data():
    with gzip.open(DATA, 'rt', encoding='utf-8') as f:
        return json.load(f)


def lookup(code):
    row = _data()['records'].get(normalize(code))
    return copy.deepcopy(row) if row else None


def parent(code):
    row = lookup(code)
    return lookup(row['parent']) if row and row.get('parent') else None


def children(code=None):
    return [copy.deepcopy(row) for row in _data()['records'].values() if row.get('parent') == (code or None)]


def search(text, limit=30):
    text = unicodedata.normalize('NFKC', str(text)).casefold().strip()
    if not text:
        return []
    words = text.split()
    return [copy.deepcopy(row) for row in _data()['records'].values()
            if row['code'].casefold().startswith(text.replace(' ', '')) or
            all(w in row['path_title'].casefold() for w in words)][:limit]


def metadata():
    return copy.deepcopy(_data()['metadata'])
