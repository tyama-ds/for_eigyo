"""EvidenceExtractor — 文書からの値・単位・メタデータ抽出。

抽出順序(要件§7):
1. JSON / CSV / HTML表などの構造化データ
2. 明示的な数値パターンと単位(ルールベース、日英対応)
3. PDF本文(2と同じパターン抽出)
4. それでも困難な場合のみ LLM による構造化抽出(ai_assistedフラグ+Python側検証)

LLMが出した数値だけを保存することはない。抽出根拠(抜粋)が
元文書に実在することを必ず検証する(捏造防止)。
"""

from __future__ import annotations

import csv
import io
import re

from fermiscope.domain.enums import DocumentType, SearchPurpose
from fermiscope.domain.models import EvidenceItem, ParameterEstimate
from fermiscope.evidence.normalize import expected_units_for
from fermiscope.research.fetcher import FetchedDocument

# 数値表現: 「約7,227,180世帯」「10.4%」「1.5 tunings」「7万2千」「1億2000万」等。
# 数値部(group 1)は「数字+スケール」が連続する複合表現をまとめて捕捉する。
# こうしないと「7万2千」を「7万」と「2千」に分断し、下位桁(2千)を取りこぼす。
_NUMBER_PATTERN = re.compile(
    r"(?:約|およそ|平均|Average\s+|approx\.?\s+)?"
    r"((?:[0-9][0-9,]*(?:\.[0-9]+)?\s*[兆億万千]\s*)*[0-9][0-9,]*(?:\.[0-9]+)?(?:\s*[兆億万千])?)"
    r"\s*"
    r"(世帯|人|名|台|件|回|円|%|パーセント|店舗|店|社|日間?|時間|km|kg|"
    r"tunings?|jobs?|days?|households?|people|persons?|yen)?",
)

# 複合数値内の1セグメント(「7万」「2千」)を取り出す。
_NUMBER_SEGMENT = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(兆|億|万|千)?")

_SCALE = {"兆": 1e12, "億": 1e8, "万": 1e4, "千": 1e3, None: 1.0, "": 1.0}

_UNIT_NORMALIZE = {
    "世帯": "household",
    "households": "household",
    "household": "household",
    "人": "person",
    "名": "person",
    "people": "person",
    "person": "person",
    "persons": "person",
    "台": "item",
    "件": "event",
    "回": "event",
    "tuning": "event",
    "tunings": "event",
    "job": "event",
    "jobs": "event",
    "円": "JPY",
    "yen": "JPY",
    "%": "percent",
    "パーセント": "percent",
    "日": "day",
    "日間": "day",
    "day": "day",
    "days": "day",
    "時間": "hour",
    "店": "store",
    "店舗": "store",
    "社": "company",
}

# ラベル付きメタデータ(発行:… / Publisher: … 等)
_META_PATTERNS = {
    "publisher": re.compile(r"(?:発行|出典|発表元|作成|Publisher)\s*[::]\s*([^\n]{1,60})"),
    "publication_date": re.compile(
        r"(?:公表日|発行日|掲載日|発表日|公開日|Published)\s*[::]\s*([^\n]{1,30})"
    ),
    "methodology": re.compile(r"(?:調査方法|推計方法|Method(?:ology)?)\s*[::]\s*([^\n]{1,200})"),
    "geography": re.compile(r"(?:対象地域|地域|Region)\s*[::]\s*([^\n]{1,40})"),
    "time_period": re.compile(
        r"(?:調査時点|対象期間|基準日|調査年月?|Period)\s*[::]\s*([^\n]{1,40})"
    ),
    "population": re.compile(r"(?:調査対象|母集団|対象)\s*[::]\s*([^\n]{1,80})"),
    "definition": re.compile(r"(?:定義|Definition)\s*[::]\s*([^\n]{1,200})"),
    "source_ref": re.compile(r"(?:一次資料|引用元|元データ|データ出所|Source)\s*[::]\s*(\S{1,200})"),
    "revision_date": re.compile(r"(?:訂正日|改訂日|Revised)\s*[::]\s*([^\n]{1,30})"),
}

_RANGE_PATTERN = re.compile(
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%)?\s*(?:[〜~]|-|から|to)\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
)


def parse_japanese_number(num_text: str, scale_text: str | None = None) -> float:
    """日本語数値を float へ変換する。

    - 単一セグメント指定(num_text + scale_text)としても、
    - 複合数値文字列(「7万2千」を num_text に丸ごと渡す)としても解釈できる。

    複合の場合は連続する (数字, スケール) を合算し、下位桁の切り捨てを防ぐ。
    数字が1つも含まれない場合は ValueError。
    """
    if scale_text is not None:
        return float(num_text.replace(",", "")) * _SCALE.get(scale_text, 1.0)
    total = 0.0
    matched = False
    for seg in _NUMBER_SEGMENT.finditer(num_text):
        digits = seg.group(1)
        if not digits:
            continue
        matched = True
        total += float(digits.replace(",", "")) * _SCALE.get(seg.group(2), 1.0)
    if not matched:
        raise ValueError(f"数値として解釈できません: {num_text!r}")
    return total


def _has_scale(num_run: str) -> bool:
    """複合数値文字列がスケール語(兆・億・万・千)を含むか。"""
    return any(s in num_run for s in ("兆", "億", "万", "千"))


def _doc_meta(doc: FetchedDocument) -> dict[str, str]:
    meta: dict[str, str] = {}
    for key, pattern in _META_PATTERNS.items():
        m = pattern.search(doc.text)
        if m:
            meta[key] = m.group(1).strip()
    return meta


_META_LINE_PATTERN = re.compile(
    r"^\s*(?:発行|出典|発表元|作成|公表日|発行日|掲載日|発表日|公開日|調査方法|推計方法|"
    r"定義|注記?|一次資料|引用元|元データ|データ出所|訂正日|改訂日|"
    r"Publisher|Published|Method(?:ology)?|Definition|Source|Revised)\s*[::].*$",
    re.MULTILINE,
)


def _value_text(text: str) -> str:
    """値抽出用テキスト: メタデータ行(調査方法・定義等)の数値を拾わないよう
    同じ長さの空白で置換する(文字位置は保存される)。"""
    return _META_LINE_PATTERN.sub(lambda m: " " * len(m.group(0)), text)


def _excerpt_around(text: str, pos: int, width: int = 110) -> str:
    start = max(0, pos - width)
    end = min(len(text), pos + width)
    return ("…" if start > 0 else "") + text[start:end].replace("\n", " ").strip() + (
        "…" if end < len(text) else ""
    )


_EN_STOPWORDS = {"per", "day", "days", "year", "years", "the", "and", "for", "with", "rate"}


def _param_aliases(param: ParameterEstimate) -> list[str]:
    """検索語をトークン化した抽出用の別名リスト(長い順)。"""
    aliases: set[str] = set()
    for term in [*param.search_terms_ja, *param.search_terms_en, param.name]:
        if not term:
            continue
        aliases.add(term)
        for token in term.split():
            if token.isdigit():
                continue
            if token.isascii():
                if len(token) >= 4 and token.lower() not in _EN_STOPWORDS:
                    aliases.add(token)
            elif len(token) >= 2:
                aliases.add(token)
    return sorted(aliases, key=len, reverse=True)


def _match_aliases(text: str, aliases: list[str]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for alias in aliases:
        if alias.isascii():
            # 英語別名は単語境界つきで照合("per" が "Period" に一致するのを防ぐ)
            pattern = re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(alias))
        for m in pattern.finditer(text):
            out.append((alias, m.start()))
    return out


def _base_item(
    doc: FetchedDocument,
    param: ParameterEstimate,
    query: str,
    purpose: SearchPurpose,
    meta: dict[str, str],
) -> EvidenceItem:
    return EvidenceItem(
        url=doc.url,
        canonical_url=doc.final_url,
        title=doc.title or meta.get("publisher", "") or doc.url,
        publisher=meta.get("publisher", ""),
        publication_date=meta.get("publication_date", ""),
        revision_date=meta.get("revision_date", ""),
        retrieval_date=doc.fetched_at,
        document_type=doc.doc_type if doc.doc_type in DocumentType else DocumentType.UNKNOWN,
        search_query=query,
        search_purpose=purpose,
        parameter_id=param.id,
        geography=meta.get("geography", ""),
        population_definition=meta.get("population", ""),
        time_period=meta.get("time_period", ""),
        exact_definition=meta.get("definition", ""),
        methodology_summary=meta.get("methodology", ""),
        content_hash=doc.content_hash,
        parent_source_id=meta.get("source_ref", ""),
    )


def _acceptable(unit_norm: str, num_text: str, expected: set[str]) -> bool:
    """抽出候補の単位がパラメータの期待単位に適合するか。"""
    if unit_norm:
        return unit_norm in expected
    # 単位なし: 小数のみ、かつ単位なしを許容するパラメータのみ
    return "" in expected and "." in num_text


def _try_number_near(
    text: str,
    pos: int,
    alias_len: int,
    expected: set[str],
    window: int = 120,
) -> tuple[float, str, int] | None:
    """alias位置の近傍から期待単位に適合する数値を探す。"""
    seg_start = max(0, pos - 40)
    segment = text[seg_start : pos + alias_len + window]
    # (優先度, 距離, 値, 単位, 位置)。明示的な単位つき数値を単位なし小数より優先する。
    best: tuple[int, int, float, str, int] | None = None
    for m in _NUMBER_PATTERN.finditer(segment):
        abs_pos = seg_start + m.start()
        # alias自身に含まれる数字(「1日あたり」の1等)はスキップ
        if pos <= abs_pos < pos + alias_len:
            continue
        # 「1回以上」「3件未満」のような修飾数値は値ではないのでスキップ
        tail = segment[m.end() : m.end() + 2]
        if tail[:1] in ("以", "超") or tail[:2] in ("未満",):
            continue
        num_run, unit = m.group(1), m.group(2)
        unit_norm = _UNIT_NORMALIZE.get((unit or "").lower(), "") if unit else ""
        has_scale = _has_scale(num_run)
        if not _acceptable(unit_norm, num_run, expected) and not (has_scale and not unit):
            continue
        try:
            value = parse_japanese_number(num_run)
        except ValueError:
            continue
        priority = 0 if unit_norm else 1
        candidate = (priority, abs(abs_pos - pos), value, unit_norm, abs_pos)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    if best is None:
        return None
    return best[2], best[3], best[4]


def _try_range_near(text: str, num_pos: int, value: float) -> tuple[float | None, float | None]:
    """数値位置の近傍から範囲表現(1.0〜2.0 / range 180 - 220)を探す。"""
    segment = text[num_pos : num_pos + 120]
    for m in _RANGE_PATTERN.finditer(segment):
        try:
            lo = float(m.group(1).replace(",", ""))
            hi = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        if lo < hi and lo <= value <= hi:
            return lo, hi
    return None, None


# 列ヘッダーの意味分類(年・地域列は値として採らない)
_YEAR_COL_RE = re.compile(
    r"^\s*(年|年度|年次|対象年|調査年|基準年|year|date|時点|期間|月|日付|day)\s*$", re.IGNORECASE
)
_GEO_COL_RE = re.compile(
    r"^\s*(地域|都道府県|県|府|市|region|prefecture|area|都市|国|country)\s*$", re.IGNORECASE
)
# 値列らしいヘッダー語
_VALUE_WORD_RE = re.compile(
    r"値|value|人口|台数|件数|金額|population|count|total|amount|売上|規模|数$|数量", re.IGNORECASE
)


def _infer_column_unit(header_cell: str) -> str:
    """列ヘッダーから単位を推定する(不明なら空文字)。"""
    h = header_cell or ""
    if "%" in h or "率" in h or "パーセント" in h:
        return "percent"
    if "人口" in h or "人数" in h or h.strip() in ("人", "名"):
        return "person"
    if "世帯" in h:
        return "household"
    if "台" in h:
        return "item"
    if "件" in h or "回" in h:
        return "event"
    if "金額" in h or "売上" in h or "円" in h or "規模" in h:
        return "JPY"
    if "店" in h:
        return "store"
    if "社" in h:
        return "company"
    return ""


# 列ヘッダーの単位スケール語 → 乗数(長い接頭辞を先に判定)。
_HEADER_SCALE_WORDS: tuple[tuple[str, float], ...] = (
    ("百万", 1e6),
    ("千", 1e3),
    ("万", 1e4),
    ("億", 1e8),
    ("兆", 1e12),
)


def _column_scale(header_cell: str) -> tuple[float, str]:
    """列ヘッダーの単位スケール語(千人・万人・百万円・億円等)から乗数を返す。

    「人口(千人)」→(1000, "千")。括弧内の単位表記を優先し、無ければヘッダー全体を見る。
    スケール語が無ければ (1.0, "")。
    """
    h = header_cell or ""
    m = re.search(r"[(（]([^)）]*)[)）]", h)
    scope = m.group(1) if m else h
    for word, mult in _HEADER_SCALE_WORDS:
        if word in scope:
            return mult, word
    return 1.0, ""


def _parse_cell_number(cell: str) -> tuple[float, str] | None:
    """セル文字列から (数値, 正規化単位) を取り出す。数値が無ければ None。"""
    text = (cell or "").strip()
    if not text:
        return None
    m = _NUMBER_PATTERN.search(text)
    if not m:
        return None
    try:
        value = parse_japanese_number(m.group(1))
    except ValueError:
        return None
    unit_norm = _UNIT_NORMALIZE.get((m.group(2) or "").lower(), "") if m.group(2) else ""
    return value, unit_norm


def _extract_from_grid(
    doc: FetchedDocument,
    param: ParameterEstimate,
    query: str,
    purpose: SearchPurpose,
    header: list[str],
    data_rows: list[list[str]],
    source_label: str,
) -> list[EvidenceItem]:
    """ヘッダー・列意味に基づき値列を決めてから抽出する(行の最初の数値を採らない)。

    - 年・地域列は値として採用しない(時点・地域として付随情報に回す)。
    - パラメータ別名/期待単位/値語に一致する列を値列とする。
    - 複数候補があれば先頭を採用しつつ、候補と判断理由を locator に残す(自動確定しない)。
    - 単位はセル→列ヘッダーの順で推定し、根拠がなければ空欄(目標単位を無条件採用しない)。
    """
    aliases = _param_aliases(param)
    expected = expected_units_for(param.unit)
    meta = _doc_meta(doc)
    ncols = max([len(header), *(len(r) for r in data_rows)], default=0)
    if ncols == 0:
        return []

    def col_header(i: int) -> str:
        return header[i] if i < len(header) else ""

    year_cols = {i for i in range(ncols) if _YEAR_COL_RE.search(col_header(i))}
    geo_cols = {i for i in range(ncols) if _GEO_COL_RE.search(col_header(i))}
    excluded = year_cols | geo_cols

    alias_cols = [
        i for i in range(ncols)
        if i not in excluded and any(a and a in col_header(i) for a in aliases)
    ]
    unit_word_cols = [
        i for i in range(ncols)
        if i not in excluded
        and (_VALUE_WORD_RE.search(col_header(i)) or (_infer_column_unit(col_header(i)) or "?") in expected)
    ]
    numeric_cols = [
        i for i in range(ncols)
        if i not in excluded and any(
            r[i].strip() and _parse_cell_number(r[i]) is not None for r in data_rows if i < len(r)
        )
    ]

    candidates = alias_cols or unit_word_cols or numeric_cols
    if not candidates:
        return []
    value_col = candidates[0]
    basis = "別名一致" if alias_cols else ("値語/単位一致" if unit_word_cols else "年・地域を除く数値列")
    reason = f"値列=「{col_header(value_col)}」({basis})"
    if len(candidates) > 1:
        reason += f" / 候補: {[col_header(i) for i in candidates]}(先頭を採用)"

    header_alias_match = bool(alias_cols) or any(
        a and a in col_header(i) for i in range(ncols) for a in aliases
    )

    chosen: tuple[int, list[str]] | None = None
    for r_idx, row in enumerate(data_rows):
        if any(a and a in " ".join(row) for a in aliases):
            chosen = (r_idx, row)
            break
    if chosen is None and header_alias_match:
        for r_idx, row in enumerate(data_rows):
            if value_col < len(row) and _parse_cell_number(row[value_col]) is not None:
                chosen = (r_idx, row)
                break
    if chosen is None:
        return []

    r_idx, row = chosen
    if value_col >= len(row):
        return []
    parsed = _parse_cell_number(row[value_col])
    if parsed is None:
        return []
    value, cell_unit = parsed
    unit = cell_unit or _infer_column_unit(col_header(value_col))
    unit_basis = "セル単位" if cell_unit else ("列ヘッダー" if unit else "不明")
    # 単位が判明していて期待単位に合わない場合は採らない(空欄を無条件採用しない)
    if unit and unit not in expected and "" not in expected:
        return []
    # 列ヘッダーのスケール語(千人・万人・百万円・億円等)を値に反映する。
    # セル自体がスケール語を含む場合(「1,410万」)は二重適用しない。
    scale_note = ""
    scale_mult, scale_word = _column_scale(col_header(value_col))
    if scale_mult != 1.0 and not _has_scale(row[value_col]):
        value *= scale_mult
        scale_note = f"; 列スケール={scale_word}(×{scale_mult:g})"

    time_val = next(
        (row[i] for i in sorted(year_cols) if i < len(row) and row[i].strip()), ""
    )
    geo_val = next(
        (row[i] for i in sorted(geo_cols) if i < len(row) and row[i].strip()), ""
    )
    item = _base_item(doc, param, query, purpose, meta)
    item.extracted_value = value
    item.unit = unit
    if time_val and not item.time_period:
        item.time_period = time_val
    if geo_val and not item.geography:
        item.geography = geo_val
    item.short_supporting_excerpt = f"{','.join(header)} | {','.join(row)}"[:220]
    item.locator = (
        f"{source_label} 行{r_idx + 1} 列{value_col + 1}: {reason}; 単位根拠={unit_basis}{scale_note}"
    )
    item.extraction_method = "structured"
    return [item]


def extract_from_tables(
    doc: FetchedDocument,
    param: ParameterEstimate,
    query: str,
    purpose: SearchPurpose,
) -> list[EvidenceItem]:
    """HTML/Excel 表から、列意味に基づいて値を抽出する。"""
    for t_idx, table in enumerate(doc.tables):
        if not table:
            continue
        header = table[0]
        data_rows = table[1:] if len(table) > 1 else table
        items = _extract_from_grid(
            doc, param, query, purpose, header, data_rows, f"表{t_idx + 1}"
        )
        if items:
            return items
    return []


def extract_from_csv(
    doc: FetchedDocument,
    param: ParameterEstimate,
    query: str,
    purpose: SearchPurpose,
) -> list[EvidenceItem]:
    try:
        reader = csv.reader(io.StringIO(doc.text))
        rows = [row for row in reader if row]
    except csv.Error:
        return []
    if len(rows) < 2:
        return []
    # ヘッダ行の探索(ラベル行が先行する場合に対応)
    header_idx = 0
    for i, row in enumerate(rows):
        if len(row) >= 2 and not any(":" in c or ":" in c for c in row):
            header_idx = i
            break
    header = rows[header_idx]
    data_rows = rows[header_idx + 1 :]
    return _extract_from_grid(doc, param, query, purpose, header, data_rows, "CSV")


def extract_from_text(
    doc: FetchedDocument,
    param: ParameterEstimate,
    query: str,
    purpose: SearchPurpose,
) -> list[EvidenceItem]:
    """本文からパラメータ別名の近傍数値を抽出する(HTML/PDF/TEXT共通)。"""
    aliases = _param_aliases(param)
    expected = expected_units_for(param.unit)
    meta = _doc_meta(doc)
    value_text = _value_text(doc.text)  # メタ行の数値(標本数等)を拾わない
    matches = _match_aliases(value_text, aliases)
    items: list[EvidenceItem] = []
    used_positions: list[int] = []
    for alias, pos in sorted(matches, key=lambda t: t[1]):
        if any(abs(pos - u) < 60 for u in used_positions):
            continue
        found = _try_number_near(value_text, pos, len(alias), expected)
        if not found:
            continue
        value, unit, num_pos = found
        used_positions.append(pos)
        item = _base_item(doc, param, query, purpose, meta)
        item.extracted_value = value
        item.unit = unit
        low, high = _try_range_near(value_text, num_pos, value)
        item.extracted_low = low
        item.extracted_high = high
        item.short_supporting_excerpt = _excerpt_around(doc.text, num_pos)
        item.locator = f"本文 文字位置{num_pos}"
        item.extraction_method = "pdf" if doc.doc_type == DocumentType.PDF else "pattern"
        items.append(item)
        if len(items) >= 1:
            break
    return items


def extract_evidence(
    doc: FetchedDocument,
    param: ParameterEstimate,
    query: str,
    purpose: SearchPurpose,
) -> list[EvidenceItem]:
    """ルールベース抽出の統合入口(構造化 → パターン)。"""
    items: list[EvidenceItem] = []
    if doc.doc_type == DocumentType.CSV:
        items = extract_from_csv(doc, param, query, purpose)
    elif doc.doc_type in (DocumentType.HTML, DocumentType.XLSX) and doc.tables:
        # HTML表・Excelシートは表(セル値)からの構造化抽出を優先する
        items = extract_from_tables(doc, param, query, purpose)
    if not items:
        items = extract_from_text(doc, param, query, purpose)
    return items


def _value_supported_by_excerpt(value: float, excerpt: str) -> bool:
    """抽出値が抜粋テキスト中の数値表現から復元できるかを検査する。

    抜粋に現れる各数値(スケール語・パーセント・桁区切りを解釈)を実数化し、
    value と相対誤差 1% 以内で一致するものがあれば「根拠あり」とみなす。
    """
    normalized = excerpt.replace(",", "").replace(",", "")
    candidates: list[float] = []
    for m in _NUMBER_PATTERN.finditer(normalized):
        num_run, unit = m.group(1), m.group(2)
        try:
            base = parse_japanese_number(num_run)
        except ValueError:
            continue
        candidates.append(base)
        # 「%」表記は比率(/100)としても解釈し得る
        if unit in ("%", "パーセント"):
            candidates.append(base / 100.0)
    # スケール語を伴わない裸の数字も拾う(単位パターンに一致しなかった場合の保険)
    for m in re.finditer(r"[0-9]+(?:\.[0-9]+)?", normalized):
        try:
            candidates.append(float(m.group(0)))
        except ValueError:
            continue
    target = abs(float(value))
    for c in candidates:
        c = abs(c)
        if target == 0.0:
            if c == 0.0:
                return True
        elif abs(c - target) <= max(abs(target), abs(c)) * 0.01:
            return True
    return False


def validate_llm_extraction(
    doc: FetchedDocument,
    param: ParameterEstimate,
    payload: dict,
) -> tuple[bool, str]:
    """LLM抽出結果のPython側検証(型・根拠実在・単位・範囲)。

    - value が数値であること
    - excerpt が元文書に実在すること(捏造防止)
    - unit が文字列であること、low <= high であること
    """
    value = payload.get("value")
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False, "valueが数値ではありません"
    excerpt = payload.get("excerpt", "")
    if not isinstance(excerpt, str) or len(excerpt) < 5:
        return False, "根拠抜粋がありません"
    normalized_doc = re.sub(r"\s+", "", doc.text)
    normalized_excerpt = re.sub(r"\s+", "", excerpt)
    # 抜粋『全体』が元文書に連続して実在すること。先頭数百文字だけの照合では、
    # 実在文の後ろに捏造値を継ぎ足す(先頭は本物・末尾に架空の数値)手口で
    # 任意の値を通せてしまうため、抜粋の全長を照合する。
    if not normalized_excerpt or normalized_excerpt not in normalized_doc:
        return False, "根拠抜粋が元文書に連続して見つかりません(捏造・切り貼りの可能性)"
    # 抽出値そのものが抜粋に含まれることを必須化する(値の捏造防止)。
    # 桁区切り・スケール語(万/億等)・%小数の差異を吸収して照合する。
    if not _value_supported_by_excerpt(value, excerpt):
        return False, "抽出値が根拠抜粋に含まれていません(値の捏造の可能性)"
    unit = payload.get("unit", "")
    if not isinstance(unit, str):
        return False, "unitが文字列ではありません"
    low, high = payload.get("low"), payload.get("high")
    for name, v in (("low", low), ("high", high)):
        if v is not None and (not isinstance(v, int | float) or isinstance(v, bool)):
            return False, f"{name}が数値ではありません"
        # 不確実性区間(low/high)も原文に裏付けが必要(捏造区間の受理を防ぐ)。
        # 裏付けのない区間を黙って採用するより、未解決として扱う方が安全。
        if v is not None and not _value_supported_by_excerpt(float(v), excerpt):
            return False, f"{name}({v})が根拠抜粋に含まれていません(区間の捏造の可能性)"
    if low is not None and high is not None and low > high:
        return False, "low > high です"
    return True, ""
