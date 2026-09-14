"""Classification browsing and explicit seeds. Browsing never edits query selection."""
import copy
import re
import unicodedata

import classification_catalog as ipc_catalog
import fterm_catalog
import classification_bridge
from classification_translations import label_fields
from catalog import CATALOG, keywords, suggest


def key(item):
    return item['kind'] + ':' + item['code']


def normalize(kind, code):
    if kind not in ('IPC', 'CPC', 'F-term') or not isinstance(code, str):
        raise ValueError('分類体系とコードを確認してください。')
    code = re.sub(r'\s+', '', unicodedata.normalize('NFKC', code)).upper()
    section = '[A-HY]' if kind == 'CPC' else '[A-H]'
    pattern = section + r'(?:\d{2}(?:[A-Z](?:\d{1,4}/\d{1,6})?)?)?' if kind != 'F-term' else r'\d[A-Z]\d{3}(?:[A-Z]{2}\d{2})?'
    if not re.fullmatch(pattern, code):
        raise ValueError(f'{kind} のコード形式を確認してください: {code}')
    if kind in ('IPC', 'CPC'):
        match = re.fullmatch(r'([A-HY]\d{2}[A-Z])(\d+)/(\d+)', code)
        if match:
            code = match[1] + str(int(match[2])) + '/' + match[3]
    return code


def parse_codes(kind, text):
    """Consume every character; unknown suffixes must never be silently discarded."""
    if not isinstance(text, str) or len(text) > 12000:
        raise ValueError('分類は12,000文字以内で入力してください。')
    text = unicodedata.normalize('NFKC', text).strip().upper()
    section = '[A-HY]' if kind == 'CPC' else '[A-H]'
    pattern = section + r'(?:\s*\d{2}(?:\s*[A-Z](?:\s*\d{1,4}\s*/\s*\d{1,6})?)?)?' if kind != 'F-term' else r'\d[A-Z]\d{3}(?:\s*[A-Z]{2}\d{2})?'
    out, pos = [], 0
    while pos < len(text):
        match = re.match(pattern, text[pos:])
        if not match:
            raise ValueError(f'{kind} の未解析部分があります: {text[pos:pos+50]}。体系ごとの欄に完全なコードを入力してください。')
        code = normalize(kind, match[0])
        pos += match.end()
        version = re.match(r'\s*\(\d{4}\.\d{2}\)', text[pos:])
        if version:
            pos += version.end()
        if code not in out:
            out.append(code)
        if pos < len(text):
            delimiter = re.match(r'[\s,;、；|]+', text[pos:])
            if not delimiter:
                raise ValueError(f'{kind} のコードの後に未解析部分があります: {text[pos:pos+50]}')
            pos += delimiter.end()
    if len(out) > 100:
        raise ValueError('一度に追加できる分類は体系ごとに100件までです。')
    return out


def lookup(kind, code):
    code = normalize(kind, code)
    return fterm_catalog.lookup(code) if kind == 'F-term' else ipc_catalog.lookup(kind, code)


def parent(kind, code):
    return fterm_catalog.parent(code) if kind == 'F-term' else ipc_catalog.parent(kind, code)


def children(kind, code=None):
    return fterm_catalog.children(code) if kind == 'F-term' else ipc_catalog.children(kind, code)


def enrich(item):
    kind, code = item['kind'], normalize(item['kind'], item['code'])
    known = lookup(kind, code)
    local = next((c for c in CATALOG if c['kind'] == kind and c['code'] == code), None)
    result = {**copy.deepcopy(item), **(known or {}), 'kind': kind, 'code': code}
    if known:
        result['title_official'] = known['title']
    if kind == 'F-term':
        result.update(title_ja=(known or local or {}).get('title', ''),
                      title_ja_status='official' if known else 'app_translation' if local else 'unavailable',
                      title_ja_source=(known or local or {}).get('source', ''),
                      **label_fields(kind, code))
    if local:
        result.update(title=local['title'], source=local['source'])
        result['title_short_ja'] = local['title']
    result['terms'] = list(dict.fromkeys(
        [term for term in item.get('terms', []) if isinstance(term, str)] +
        (local.get('terms', []) if local else [])))[:48]
    result.setdefault('title', code)
    result.setdefault('source', 'https://www.j-platpat.inpit.go.jp/p1101')
    result['verified'] = bool(known or local)
    result['key'] = key(result)
    result['selectable'] = bool(re.fullmatch(r'[A-HY]\d{2}[A-Z]\d{1,4}/\d{1,6}', code)) if kind != 'F-term' else bool(re.fullmatch(r'\d[A-Z]\d{3}[A-Z]{2}\d{2}', code))
    upper_ipc = kind == 'IPC' and bool(known) and '/' not in code
    if upper_ipc:
        result['selectable'] = True
    if known and known.get('selectable') is False:
        result['selectable'] = False
    result['selection_scope'] = 'subtree' if upper_ipc else 'code'
    result.setdefault('reason', '公式分類の階層から表示。技術への適合性は定義を確認してください。' if known else '利用者の指定。存在・定義は未確認です。')
    result.setdefault('group', '分類探索')
    result.setdefault('score', 0)
    result.setdefault('origins', [])
    return result


def merge_candidates(existing, incoming, limit=1500):
    merged = {key(c): copy.deepcopy(c) for c in existing}
    for item in incoming:
        k = key(item)
        old = merged.get(k, {})
        origins = copy.deepcopy(old.get('origins', []))
        for origin in item.get('origins', []):
            if origin not in origins:
                origins.append(copy.deepcopy(origin))
        merged[k] = {**old, **copy.deepcopy(item), 'origins': origins}
        # Classification browsing and dictionary enrichment may replace a
        # caption, but must not erase the recorded model recommendation.
        sources = copy.deepcopy(item.get('recommendation_sources', []))
        for source in old.get('recommendation_sources', []):
            if source.get('type') == 'llm' and source not in sources:
                sources.append(copy.deepcopy(source))
        if sources:
            merged[k]['recommendation_sources'] = sources[-48:]
        class_origins = copy.deepcopy(old.get('classification_origins', []))
        for origin in item.get('classification_origins', []):
            if origin not in class_origins:
                class_origins.append(copy.deepcopy(origin))
        if class_origins:
            merged[k]['classification_origins'] = class_origins
            if any(o.get('type') == 'patent' and o.get('code_verified') for o in class_origins):
                merged[k]['evidence_status'] = 'patent_metadata'
            elif any(o.get('type') in ('fi_to_ipc', 'fterm_theme_to_ipc') for o in class_origins):
                merged[k]['evidence_status'] = 'derived_candidate'
    if len(merged) > limit:
        raise ValueError('候補の保存上限1,500件に達しました。現在の候補から選択してください。')
    return list(merged.values())


def overview(items, text, limit=24):
    """Show broad groups without losing why their more precise children matched."""
    result = {}
    for item in items:
        original = enrich(item)
        c = original
        if c['kind'] == 'IPC' and c['verified']:
            p = parent('IPC', c['code'])
            while p and '/' in p['code']:
                c = enrich(p)
                p = parent('IPC', c['code'])
        elif c['kind'] == 'F-term':
            p = parent('F-term', c['code'])
            if p:
                c = enrich(p)
        node_key = key(c)
        existing = result.get(node_key)
        if existing is None:
            existing = copy.deepcopy(c)
            existing['facets'] = []
            existing['recommendation_sources'] = []
            existing['matched_classifications'] = []
            existing['terms'] = list(existing.get('terms', []))
            result[node_key] = existing
        for field in ('facets', 'terms', 'recommendation_sources'):
            for value in original.get(field, []):
                if value not in existing[field]:
                    existing[field].append(copy.deepcopy(value))
            existing[field] = existing[field][:48]
        matched = {name: copy.deepcopy(original[name]) for name in
                   ('kind', 'code', 'title', 'title_official', 'reason', 'source', 'facets',
                    'llm_reason', 'llm_proposed', 'recommendation_method', 'recommendation_sources') if name in original}
        if matched not in existing['matched_classifications']:
            existing['matched_classifications'].append(matched)
        if original.get('evidence_status'):
            # A child's documentary classification does not assert that the
            # parent code itself was present in its patent metadata.
            existing['evidence_status'] = 'hypothesis'
            existing['relevance_status'] = 'review_pending'
        reason = original.get('reason', '')
        if node_key != key(original):
            reason = f'起点 {original["code"]} を含む広い分類。' + reason
        if not existing.get('_overview_reason'):
            existing['reason'] = reason or '公式分類の上位群から表示。技術への適合性は確認してください。'
            existing['_overview_reason'] = True
    if not result:
        for word in keywords(text):
            for c in ipc_catalog.search('IPC', word, limit=12):
                c = enrich(c)
                result.setdefault(key(c), c)
    rows = list(result.values())
    for row in rows:
        row.pop('_overview_reason', None)
    if len(rows) <= limit:
        return rows
    # Select existing verified hypotheses by viewpoint diversity; never invent
    # a section or add a code simply to fill a quota.
    chosen, covered_facets, covered_sections = [], set(), set()
    pending = list(rows)
    while pending and len(chosen) < limit:
        def novelty(row):
            facets = set(row.get('facets', []))
            section = row['code'][0] if row['kind'] == 'IPC' else row['kind']
            return 2 * len(facets - covered_facets) + int(section not in covered_sections)
        best = max(range(len(pending)), key=lambda index: novelty(pending[index]))
        row = pending.pop(best)
        chosen.append(row)
        covered_facets.update(row.get('facets', []))
        covered_sections.add(row['code'][0] if row['kind'] == 'IPC' else row['kind'])
    chosen_keys = {key(row) for row in chosen}
    return [row for row in rows if key(row) in chosen_keys]


def browse(kind='IPC', code=None, direction='children', text='', offset=0):
    if kind not in ('IPC', 'CPC', 'F-term'):
        raise ValueError('分類体系を確認してください。')
    focus = enrich({'kind': kind, 'code': code}) if code else None
    note = ''
    if direction == 'search':
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError('分類コードまたは名称を300文字以内で入力してください。')
        rows = ipc_catalog.search(kind, text, limit=300) if kind != 'F-term' else []
        if kind == 'F-term':
            stack = list(children(kind))
            while stack:
                item = stack.pop(0)
                if text.casefold() in (item['code'] + ' ' + item['title']).casefold():
                    rows.append(item)
                stack.extend(children(kind, item['code']))
        # Japanese seed names complement the official English IPC captions.
        if kind == 'IPC':
            for local in suggest(text):
                if local['kind'] == kind and key(local) not in {key(x) for x in rows}:
                    rows.insert(0, local)
        note = '公式IPCの名称検索は英語。日本語は内蔵の対訳がある語に対応します。' if kind == 'IPC' else ''
    elif direction == 'roots':
        focus = None
        rows = children(kind)
    elif direction == 'broader':
        if not focus:
            raise ValueError('広げる起点の分類を選んでください。')
        p = parent(kind, focus['code'])
        fallback_code = None
        if not p and kind == 'IPC' and not focus['verified']:
            # Recover a verified section/class/subclass prefix only. This does
            # not assert an official parent for an unknown or obsolete symbol.
            for length in (4, 3, 1):
                if length >= len(focus['code']):
                    continue
                p = ipc_catalog.lookup('IPC', focus['code'][:length])
                if p:
                    fallback_code = focus['code']
                    p['reason'] = f'{fallback_code} は現行辞書で未確認です。コード先頭が一致する上位候補として {p["code"]} を表示しています。正式な親子関係や旧版との対応は未確認です。'
                    break
        rows = ([p] + children(kind, p['code'])) if p else [focus]
        if p:
            focus = enrich(p)
        note = '上位分類と、その直下の分類を表示しています。選択条件はまだ変わりません。' if p else 'この辞書では上位分類を取得できません。'
        if fallback_code:
            note = p['reason'] + ' 選択条件はまだ変わりません。'
    elif direction == 'children':
        rows = children(kind, focus['code'] if focus else None)
        if not rows and focus:
            rows = [focus]
            note = 'この辞書では下位分類がありません。未収録の体系・テーマは公式分類表で確認できます。'
        else:
            note = '下位分類を表示しています。親をORで残すと狭まらないため、必要に応じて選択を置き換えてください。'
    else:
        raise ValueError('探索方向を確認してください。')
    if kind == 'CPC':
        note = 'CPCの階層辞書は未収録です。直接指定は可能です。IPCの階層へは読み替えません。'
    if kind == 'F-term':
        note += ' F-termの階層辞書は5H029のみ収録しています。'
    unique = {key(c): c for c in rows}
    rows = list(unique.values())
    if not isinstance(offset, int) or offset < 0:
        raise ValueError('表示位置を確認してください。')
    trail, cursor = [], focus
    while cursor and len(trail) < 30:
        trail.insert(0, {'code': cursor['code'], 'title': cursor['title'], 'kind': kind,
                         **{k: v for k, v in enrich(cursor).items() if k.startswith('title_ja') or k.startswith('title_en')}})
        cursor = parent(kind, cursor['code'])
    return dict(items=[enrich(c) for c in rows[offset:offset+24]], focus=focus, trail=trail, total=len(rows), offset=offset,
                next_offset=offset+24 if offset+24 < len(rows) else None, note=note, direction=direction, kind=kind, code=code, text=text)


def seed_items(body, patents):
    ids = body.get('patent_ids', [])
    if not isinstance(ids, list) or len(ids) > 30 or any(not isinstance(i, str) for i in ids):
        raise ValueError('ターゲット特許は30件まで選択してください。')
    indexed = {p['id']: p for p in patents}
    if any(i not in indexed for i in ids):
        raise ValueError('取り込み済みの特許にないIDが指定されています。')
    batches = []
    for kind, field in [('IPC', 'ipc_text'), ('F-term', 'fterm_text'), ('CPC', 'cpc_text')]:
        text = body.get(field, '')
        codes = parse_codes(kind, text)
        batches.append((kind, codes, dict(type='manual', label=str(body.get('label', '利用者の推薦'))[:160], raw=text)))
    for patent_id in dict.fromkeys(ids):
        p = indexed[patent_id]
        extracted = classification_bridge.extract(p, strict=True)
        if not extracted['items']:
            raise ValueError(f'{patent_id} には利用できるIPC・FI・F-term・CPCが取り込まれていません。分類を直接入力してください。')
        # Notes remain attached to the seed even if an unsupported F-term theme
        # cannot provide IPC. The original imported fields are never rewritten.
        for item in extracted['items']:
            item['classification_warnings'] = list(extracted['warnings'])
        batches.append((None, extracted['items'], None))
    result = []
    for kind, codes, origin in batches:
        if kind is None:
            result.extend(codes)
            continue
        for code in codes:
            result.append(enrich(dict(kind=kind, code=code, origins=[origin], reason='ターゲット特許・利用者推薦を起点にした候補。技術への適合性は利用者の判断で選びます。')))
    if not result:
        raise ValueError('IPC・F-term・CPCを入力するか、取り込み済み特許を選んでください。')
    return merge_candidates([], result)
