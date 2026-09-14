"""Shared, provenance-preserving classification extraction. No patent rows are edited.

FI bases and F-term theme coverage produce exploratory IPC candidates, never
assertions that an IPC was assigned to a publication. All emitted IPC candidates
are checked against the bundled IPC edition. Old symbols are not remapped.
"""
from copy import deepcopy
from functools import lru_cache
import re
import unicodedata

import classification_catalog as ipc_catalog
import fterm_catalog


FI_SOURCE = 'https://www.jpo.go.jp/system/patent/gaiyo/bunrui/fi/f_i_kaisei.html'
FI_WARNING = 'FIとIPCには版差があります。IPC部分の照合は関連候補であり、公報への付与IPCや正式な新旧対応を確認したものではありません。'


def _raw(value):
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return '; '.join(value)
    if not isinstance(value, str):
        raise ValueError('分類欄は文字列または文字列配列で指定してください。')
    if len(value) > 12000:
        raise ValueError('分類欄は12,000文字以内で指定してください。')
    return value


def parse_fi(value):
    """Consume FI entries completely, including comma expansion / @ identifiers.

    Commas preceding a complete IPC-shaped FI base separate entries; commas
    followed by three digits belong to the FI expansion. Invalid trailing text
    invalidates the field, rather than silently producing a partly parsed hit.
    """
    text = unicodedata.normalize('NFKC', _raw(value)).strip().upper()
    if not text:
        return []
    base = r'[A-H]\s*\d{2}\s*[A-Z]\s*\d{1,4}\s*[/:]\s*\d{1,6}'
    entry = re.compile(r'(' + base + r')(?P<suffix>(?:\s+\d{3})(?:\s*[A-Z])?|(?:\s*,\s*\d{3})?(?:\s*@\s*[A-Z])?)')
    result, position = [], 0
    while position < len(text):
        match = entry.match(text, position)
        if not match:
            raise ValueError(f'FI の未解析部分があります: {text[position:position+50]}')
        position = match.end()
        version = re.match(r'\s*\(\d{4}\.\d{2}\)', text[position:])
        if version:
            position += version.end()
        result.append(dict(base=ipc_catalog.normalize_code(match[1]), raw=match[0].strip()))
        if position < len(text):
            delimiter = re.match(r'[\s,;、；|]+', text[position:])
            if not delimiter:
                raise ValueError(f'FI の未解析部分があります: {text[position:position+50]}')
            position += delimiter.end()
    return result


@lru_cache(maxsize=4096)
def _known(code):
    return ipc_catalog.lookup('IPC', code)


def _fi_candidate(base):
    known = _known(base)
    if known:
        return known, 'exact_base'
    # A deleted FI base is NOT assigned a guessed main group or replacement.
    # A verified subclass prefix is explicitly presented as a broader hypothesis.
    known = _known(base[:4])
    return (known, 'subclass_fallback') if known else (None, 'unsupported')


@lru_cache(maxsize=4096)
def _theme_ipc_codes(theme):
    record = fterm_catalog.theme_mapping(theme)
    if not record or not record.get('fi_range'):
        return ()
    coverage = re.sub(r'\s+', '', unicodedata.normalize('NFKC', record['fi_range'])).upper()
    codes = []
    # Inspect explicit subclass spans in the official FI range, not digits in
    # the F-term symbol. A range spanning several main groups falls back to the
    # whole subclass; endpoint-only selection would falsely omit the middle.
    spans = list(re.finditer(r'[A-H]\d{2}[A-Z]', coverage))
    for index, match in enumerate(spans):
        subclass = match[0]
        chunk = coverage[match.end():spans[index+1].start() if index+1 < len(spans) else len(coverage)]
        main_groups = set(re.findall(r'(\d{1,4})/\d{1,6}', chunk))
        target = subclass
        if len(main_groups) == 1:
            main = subclass + str(int(next(iter(main_groups)))) + '/00'
            if _known(main):
                target = main
        if _known(target) and target not in codes:
            codes.append(target)
    return tuple(codes)


def extract(patent, *, strict=False):
    """Return detached items, warnings, errors; strict rejects malformed fields.

    Unknown explicit codes remain selectable seeds with unverified_metadata;
    callers requiring dictionary validation must filter verified. Derived IPC
    candidates are always dictionary-verified. `classification_origins` carries
    the same provenance as `origins` through later UI/lab-specific origin merges.
    """
    import classification_explorer as explorer  # lazy to avoid a seed import cycle

    items, warnings, errors, fterms = {}, [], [], []
    patent_id = str(patent.get('id', ''))

    def add(kind, code, origin, status, reason):
        origin = dict(origin, patent_id=patent_id, label=str(patent.get('title', ''))[:300])
        item = explorer.enrich(dict(kind=kind, code=code, origins=[origin], evidence_status=status, reason=reason))
        old = items.get(item['key'])
        if old:
            if origin not in old['origins']:
                old['origins'].append(origin)
            if status == 'patent_metadata':
                old['evidence_status'] = status
                old['reason'] = reason
        else:
            items[item['key']] = item

    for kind, field in [('IPC', 'ipc'), ('F-term', 'fterm'), ('CPC', 'cpc')]:
        try:
            raw = _raw(patent.get(field) or '')
            codes = explorer.parse_codes(kind, raw)
        except ValueError as error:
            errors.append(f'{patent_id}: {kind}欄に未解析部分があるため、その欄を候補集計から除きました。{error}')
            continue
        for code in codes:
            verified = explorer.lookup(kind, code) is not None
            add(kind, code, dict(type='patent', field=field, raw=raw, source='uploaded_metadata',
                                 source_version='not_provided', code_verified=verified),
                'patent_metadata' if verified else 'unverified_metadata',
                '取り込んだ公報の分類欄に記載。技術への適合性は別途確認してください。' if verified else
                '取り込んだ分類欄に記載されていますが、収録辞書でコードの存在を確認できません。')
            if kind == 'IPC' and not verified:
                warnings.append(f'{patent_id}: IPC {code} は公式辞書で確認できません。付与IPCの確認済み根拠には使いません。')
            if kind == 'F-term':
                fterms.append((code, raw))
    try:
        fi_entries = parse_fi(patent.get('fi') or '')
    except ValueError as error:
        fi_entries = []
        errors.append(f'{patent_id}: {error}。FI欄からの候補抽出を省略しました。')
    for entry in fi_entries:
        if ':' in entry['base']:
            warnings.append(f'{patent_id}: FI欄のコロン付き索引記号 {entry["raw"]} は原文を保持し、IPCコードへ直接変換しません。')
            continue
        known, method = _fi_candidate(entry['base'])
        if not known:
            warnings.append(f'{patent_id}: FI {entry["raw"]} のIPC部分・上位サブクラスを公式辞書で確認できません。')
            continue
        warning = FI_WARNING
        if method == 'subclass_fallback':
            warning += f' {entry["base"]} は現行辞書にないため、同じ接頭部の上位 {known["code"]} を探索候補にしました。'
        origin = dict(type='fi_to_ipc', field='fi', raw=entry['raw'], fi_base=entry['base'], method=method,
                      source=FI_SOURCE, source_version='FI edition not provided', ipc_version=known['version'], warning=warning)
        add('IPC', known['code'], origin, 'derived_candidate', f'FI {entry["raw"]} からのIPC候補。' + warning)
    for code, raw in fterms:
        theme = code[:5]
        mapping = fterm_catalog.theme_mapping(theme)
        if not mapping or not mapping.get('fi_range') or not mapping.get('has_fterms'):
            warnings.append(f'{patent_id}: F-term {code} のテーマ {theme} はFI対応範囲が未収録のため、IPC候補へ展開できません。')
            continue
        codes = _theme_ipc_codes(theme)
        if not codes:
            warnings.append(f'{patent_id}: テーマ {theme} のFI範囲を現行IPCの上位候補へ展開できません。')
            continue
        warning = ('テーマのFIカバー範囲を含む上位IPCの探索候補です。個別Fタームに対応する正確なIPC・公報への付与を示すものではありません。' + FI_WARNING)
        if not fterm_catalog.lookup(code):
            warning += ' テーマの存在のみ確認済みで、個別Fタームコードは未確認です。'
        if mapping.get('deactivated'):
            warning += ' このテーマは解析停止です。過去の付与期間と改正情報を確認してください。'
        for candidate in codes:
            known = _known(candidate)
            origin = dict(type='fterm_theme_to_ipc', field='fterm', raw=raw, fterm=code, theme=theme,
                          fi_range=mapping['fi_range'], theme_title=mapping['title'],
                          method='theme_range_upper_ipc', source=mapping['source'], source_version=mapping['source_version'],
                          ipc_version=known['version'], deactivated=mapping['deactivated'],
                          maintenance=mapping['maintenance'], term_verified=fterm_catalog.lookup(code) is not None, warning=warning)
            add('IPC', candidate, origin, 'derived_candidate', f'F-term {code} → テーマ {theme} のFI範囲からの上位IPC候補。' + warning)
    if strict and errors:
        raise ValueError('\n'.join(errors))
    for item in items.values():
        item['classification_origins'] = deepcopy(item['origins'])
    return dict(items=list(items.values()), warnings=list(dict.fromkeys(warnings + errors)), errors=errors)
