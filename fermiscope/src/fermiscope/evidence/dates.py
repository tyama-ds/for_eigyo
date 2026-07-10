"""日付・時点文字列の解析(和暦対応)。"""

from __future__ import annotations

import re

_ERA_BASE = {"令和": 2018, "平成": 1988, "昭和": 1925}

_YEAR_PATTERNS = [
    re.compile(r"(?P<year>(19|20)\d{2})"),
]
_ERA_PATTERN = re.compile(r"(?P<era>令和|平成|昭和)(?P<num>元|\d{1,2})")


def parse_year(text: str) -> int | None:
    """文字列から西暦年を推定する。'2020年10月' '令和2年' '2020-10-01' 等に対応。"""
    if not text:
        return None
    m = _ERA_PATTERN.search(text)
    if m:
        num = 1 if m.group("num") == "元" else int(m.group("num"))
        return _ERA_BASE[m.group("era")] + num
    for pat in _YEAR_PATTERNS:
        m = pat.search(text)
        if m:
            return int(m.group("year"))
    return None
