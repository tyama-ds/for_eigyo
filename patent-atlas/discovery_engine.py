"""Bounded, offline planning and evidence aggregation for classification discovery.

Catalogue matches are hypotheses; only explicit classification metadata in a
patent supplies documentary support. None of these scores estimates recall or
establishes technical relevance. This module does not search or change labels.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import re
import unicodedata

import classification_catalog
import classification_bridge
import classification_explorer as explorer
from catalog import keywords as parse_keywords, suggest as suggest_seeds


MAX_RECOMMENDATIONS = 60
MAX_QUERIES = 12
_FACETS = [
    dict(id='device', label='装置・構造', terms=['装置', '構造', '構成', '部材'], english_terms=['structure', 'device', 'component', 'assembly']),
    dict(id='material', label='材料・組成', terms=['材料', '組成', '化合物', '基材'], english_terms=['material', 'composition', 'compound', 'substrate']),
    dict(id='process', label='製造・プロセス', terms=['製造', '工程', '塗工', '塗布', '積層', '焼結', '成膜', '加圧'], english_terms=['manufacturing', 'coating', 'lamination', 'sintering']),
    dict(id='interface', label='界面・接合', terms=['界面', '接合', '密着', '接触', '被覆'], english_terms=['interface', 'interfacial', 'bonding', 'surface treatment']),
    dict(id='performance', label='性能・評価', terms=['性能', '効率', '評価', '劣化', '安全', '寿命', '検査'], english_terms=['measurement', 'efficiency', 'durability', 'testing']),
    dict(id='application', label='用途・システム', terms=['用途', '利用', '搭載', '応用', 'システム'], english_terms=['application', 'integration', 'use', 'system']),
]
_STOPWORDS = set('a an and are as at be been being by can claim claims comprise comprises comprising device disclosed disclosure embodiment embodiments for from has have having in invention is it method more of on one or patent present provided provides said such than that the their thereof these this through to two use used using was were wherein which with'.split())


def _unique(values, limit=24):
    result, seen = [], set()
    for value in values:
        if not isinstance(value, str):
            continue
        value = unicodedata.normalize('NFKC', value).strip()
        if value and len(value) <= 160 and value.casefold() not in seen:
            result.append(value)
            seen.add(value.casefold())
        if len(result) >= limit:
            break
    return result


def _english(values, limit=8):
    # Values, never executable fragments. Reject operators, wildcards, quotes,
    # control characters and punctuation with special CQL meanings.
    return _unique((v for v in values if isinstance(v, str) and
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 '\-/,.]{0,99}", v.strip()) and
                    re.search(r'[A-Za-z]', v) and
                    not re.search(r'\b(?:AND|OR|NOT|PROX)\b', v)), limit)


def _definition_words(text):
    """Literal definition words, without cross-reference text in parentheses."""
    clean, depth = [], 0
    for char in text:
        if char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)
        elif not depth:
            clean.append(char)
    words = set()
    stop = {'a', 'an', 'and', 'or', 'of', 'for', 'by', 'in', 'to', 'with', 'the', 'thereof', 'therefor', 'e', 'g', 'i'}
    for word in re.findall(r'[a-z]+', ''.join(clean).casefold()):
        if word in stop:
            continue
        if word.startswith('manufactur'):
            word = 'manufactur'
        elif word.endswith('ies') and len(word) > 4:
            word = word[:-3] + 'y'
        elif word.endswith('es') and word[:-2].endswith(('ss', 'sh', 'ch')):
            word = word[:-2]
        elif word.endswith('s') and not word.endswith('ss') and len(word) > 3:
            word = word[:-1]
        if word.endswith('ing') and len(word) > 6:
            word = word[:-3]
        words.add(word)
    return frozenset(words)


@lru_cache(maxsize=2048)
def _nearby_definitions(code):
    """Only the symbol and two group parents; macro headings cannot justify a hit."""
    result = []
    item = classification_catalog.lookup('IPC', code)
    while item and '/' in item['code'] and len(result) < 3:
        result.append(_definition_words(item['title']))
        item = classification_catalog.parent('IPC', item['code'])
    return tuple(result)


def _dictionary_matches(text, limit=2):
    required = _definition_words(text)
    if not required:
        return []
    ranked = []
    for row in classification_catalog.search('IPC', text, limit=200):
        if '/' not in row['code']:
            continue
        definitions = _nearby_definitions(row['code'])
        if not definitions or not (required & definitions[0]):
            continue
        # Broad ancestor headings often enumerate alternatives (e.g. lithium,
        # sodium and potassium). Such words cannot validate an unrelated child.
        if not required <= set().union(*definitions):
            continue
        locations = [next(i for i, words in enumerate(definitions) if word in words) for word in required]
        own_matches = len(required & definitions[0])
        precision = own_matches / max(1, len(definitions[0]))
        score = sum((1, .6, .3)[position] for position in locations) * 4 + precision * 16
        ranked.append((score, row))
    # Equal textual support should expose the broader process before a highly
    # specialised sibling (e.g. sintering generally before reaction sintering).
    ranked.sort(key=lambda value: (-value[0], len(value[1]['ancestors'])))
    return [row for _, row in ranked[:limit]]


def _query(topic_terms, facet_id, label, terms=(), ipc_codes=(), reason=''):
    topics, aspects = _english(topic_terms, 6), _english(terms, 4)
    codes = []
    for raw in ipc_codes:
        known = classification_catalog.lookup('IPC', raw)
        if known and '/' in known['code']:
            codes.append(known['code'])
    codes = _unique(codes, 3)
    if not topics:
        return None
    chunks = ['(' + ' or '.join(f'ta = "{v}"' for v in topics) + ')']
    if aspects:
        chunks.append('(' + ' or '.join(f'ta = "{v}"' for v in aspects) + ')')
    if codes:
        chunks.append('(' + ' or '.join(f'ic = "{v}"' for v in codes) + ')')
    query = ' and '.join(chunks)
    return dict(id='q-' + sha256(query.encode()).hexdigest()[:16], facet_id=facet_id,
                label=label, topic_terms=topics, terms=aspects, ipc_codes=codes,
                query=query, reason=reason, evidence_status='exploratory', executable=True)


def _synthetic(patent):
    return bool(patent.get('synthetic') or patent.get('demo') or
                str(patent.get('id', '')).upper().startswith('DEMO-') or
                str(patent.get('content_key', '')).lower().startswith('demo-'))


def _family(patent):
    family = str(patent.get('family') or patent.get('family_id') or '').strip()
    # IDs with absent family data remain separate documents, not asserted families.
    return ('synthetic:' if _synthetic(patent) else '') + ('family:' + family if family else 'document:' + str(patent['id']))


def _facets_for(patent, facets):
    text = (str(patent.get('title', '')) + ' ' + str(patent.get('abstract', ''))).casefold()
    return [f['id'] for f in facets if any(t.casefold() in text for t in f['terms'] + f['english_terms'])]


def _classifications(patent, warnings):
    result = classification_bridge.extract(patent)
    warnings.update(result['warnings'])
    return [item for item in result['items'] if item['selectable'] and
            (item['kind'] != 'IPC' or item['verified'])]


def validate_llm_plan(value):
    """Reject unrelated or malformed model JSON before replacing a saved plan."""
    fields = ('english_terms', 'facets', 'candidate_hypotheses')
    if not isinstance(value, dict) or not any(key in value for key in fields):
        raise ValueError('LLMの探索計画に english_terms または facets がありません。既存の計画は保持しました。')
    for name in fields:
        if name not in value:
            continue
        rows = value[name]
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError(f'LLMの探索計画の {name} は100件以内の配列で返してください。')
        if name == 'english_terms':
            if any(not isinstance(term, str) for term in rows):
                raise ValueError('LLMの english_terms には文字列を指定してください。')
        elif name == 'facets':
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                    raise ValueError('LLMの facets には id を持つ観点を指定してください。')
                if not any(key in row for key in ('terms', 'english_terms')):
                    raise ValueError('LLMの観点に terms または english_terms がありません。')
                for key in ('terms', 'english_terms'):
                    if key in row and (not isinstance(row[key], list) or len(row[key]) > 100 or
                                       any(not isinstance(term, str) for term in row[key])):
                        raise ValueError(f'LLMの観点の {key} には100件以内の文字列配列を指定してください。')
        elif any(not isinstance(row, dict) or not isinstance(row.get('kind'), str) or
                 not isinstance(row.get('code'), str) for row in rows):
            raise ValueError('LLMの分類仮説には kind と code を指定してください。')
    if not (any(term.strip() for term in value.get('english_terms', [])) or
            any(any(term.strip() for term in row.get('terms', []) + row.get('english_terms', []))
                for row in value.get('facets', [])) or value.get('candidate_hypotheses')):
        raise ValueError('LLMから探索に使える語句・観点が返りませんでした。既存の計画は保持しました。')


def propose_plan(keywords: str, candidates: list, patents: list, llm_plan: dict | None = None,
                 *, manual_english_terms: list | None = None) -> dict:
    """Make six independent search aspects while keeping the original subject.

    Optional llm_plan accepts english_terms, facets [{id, terms, english_terms}],
    and candidate_hypotheses [{kind, code}]. It cannot supply executed CQL or an
    asserted classification definition. Dictionary descriptions are authoritative.
    """
    # Manual keyword search accepts 3,000 characters. The discovery HTTP route
    # enforces its own 2,000-character limit before calling this shared engine.
    if not isinstance(keywords, str) or len(keywords) > 3000:
        raise ValueError('探索のキーワードは3,000文字以内で入力してください。')
    topic_terms = parse_keywords(keywords)
    if not topic_terms:
        raise ValueError('再帰探索の起点となる技術キーワードを入力してください。')
    used_llm = llm_plan is not None
    if used_llm:
        validate_llm_plan(llm_plan)
    llm_plan = llm_plan or {}
    notices = []
    facets = deepcopy(_FACETS)
    normalized = unicodedata.normalize('NFKC', keywords).casefold()
    english = _english(topic_terms)
    solid_battery = '全固体' in normalized or bool(re.search(r'(?:all[ -]?)?solid[ -]state\s+batter', normalized))
    if solid_battery:
        english = _unique(['all-solid-state battery', 'solid-state battery'] + english, 8)
        facets[1]['english_terms'] = ['solid electrolyte', 'sulfide', 'oxide', 'polymer electrolyte']
        facets[1]['terms'] += ['電解質', 'セラミック', '硫化物', '酸化物']
        facets[0]['english_terms'] = ['cell structure', 'electrode', 'separator']
        facets[0]['terms'] += ['電池', 'セル', '電極']
        facets[4]['english_terms'] = ['impedance', 'conductivity', 'degradation', 'testing']
        facets[4]['terms'] += ['抵抗', '伝導']
        facets[5]['english_terms'] = ['application', 'vehicle', 'energy storage', 'system']
        facets[5]['terms'] += ['車両', '蓄電']
    english = _unique(english + _english(manual_english_terms or [], 8), 8)
    before_english = {term.casefold() for term in english}
    supplied_english = llm_plan.get('english_terms', [])
    english = _unique(english + _english(supplied_english, 100), 8)
    accepted_english = [term for term in english if term.casefold() not in before_english]
    before_facets = deepcopy(facets)
    supplied_facets = llm_plan.get('facets', [])
    if isinstance(supplied_facets, list):
        for extra in supplied_facets[:12]:
            facet = next((f for f in facets if f['id'] == extra.get('id')), None)
            if facet:
                if isinstance(extra.get('terms'), list):
                    facet['terms'] = _unique(extra['terms'] + facet['terms'], 12)
                if isinstance(extra.get('english_terms'), list):
                    facet['english_terms'] = _unique(_english(extra['english_terms'], 4) + facet['english_terms'], 4)
            else:
                notices.append('装置・材料・プロセス・界面・性能・用途に対応しない観点を除外しました。')
    accepted_facets = []
    for facet, original in zip(facets, before_facets):
        extra = dict(id=facet['id'], label=facet['label'])
        for key in ('terms', 'english_terms'):
            previous = {term.casefold() for term in original[key]}
            extra[key] = [term for term in facet[key] if term.casefold() not in previous]
        if extra['terms'] or extra['english_terms']:
            accepted_facets.append(extra)
    queries = []
    broad = _query(english, 'topic', '主題を広く検索', reason='既存IPCで制限せず、起点の技術語から探します。')
    if broad:
        queries.append(broad)
    for facet in facets:
        query = _query(english, facet['id'], facet['label'], facet['english_terms'],
                       reason='主題語 AND 技術観点。既存IPCの外側も探索します。')
        if query:
            queries.append(query)
    hypotheses = {}

    def add_hypothesis(raw, facet_ids, reason):
        if not isinstance(raw, dict) or raw.get('kind') not in ('IPC', 'F-term', 'CPC'):
            return
        try:
            item = explorer.enrich({'kind': raw['kind'], 'code': raw.get('code', '')})
        except ValueError:
            return
        # Unknown LLM classifications cannot become dictionary-backed hypotheses.
        if not item['verified'] or not item['selectable']:
            return
        previous = hypotheses.get(item['key'], {})
        hypotheses[item['key']] = dict(item, evidence_status='hypothesis', relevance_status='review_pending',
                                       facets=_unique(previous.get('facets', []) + facet_ids), evidence=[],
                                       support_count=0, family_count=0, reason=reason)

    for candidate in suggest_seeds(keywords):
        matched_facets = _facets_for({'title': candidate['title'], 'abstract': ' '.join(candidate.get('terms', []))}, facets)
        add_hypothesis(candidate, matched_facets, '入力語に対応する分類候補。コードの存在は確認済みですが、適合性は公報で確認します。')
    for candidate in candidates[:120]:
        add_hypothesis(candidate, [], '既存候補を起点に保存。収録特許による裏付けは別途確認します。')
        if len(hypotheses) >= 18:
            break
    # Search the full official dictionary by independent aspect, so the compact
    # seed catalogue never bounds what can be discovered. These broad matches
    # explicitly remain hypotheses until actual patent metadata is collected.
    qualified = {
        'device': ['secondary cells', 'non-aqueous electrodes'],
        'material': ['solid electrolyte', 'lithium compounds', 'ceramic compositions'],
        'process': ['manufacture secondary cells', 'manufacture electrodes accumulators', 'ceramic sintering', 'coating electrodes'],
        'interface': ['electrodes coating', 'joining ceramic', 'electrolyte interface'],
        'performance': ['battery testing', 'ionic conductivity', 'electrochemical impedance'],
        'application': ['electric vehicles batteries', 'energy storage batteries'],
    } if solid_battery else {}
    for facet in facets:
        searches = qualified.get(facet['id']) or [subject + ' ' + term for subject in english[:2] for term in facet['english_terms'][:2]]
        for term in searches[:4]:
            for row in _dictionary_matches(term):
                add_hypothesis(row, [facet['id']], f'公式IPC名称と観点「{facet["label"]} / {term}」の語句一致。主題への適合性は未確認です。')
    supplied_hypotheses = llm_plan.get('candidate_hypotheses', [])
    before_hypothesis_keys = set(hypotheses)
    if isinstance(supplied_hypotheses, list):
        for candidate in supplied_hypotheses[:24]:
            add_hypothesis(candidate, [], 'LLMの探索仮説。コードの存在だけを公式辞書で確認済みです。')
    warnings = ['観点・分類候補は探索仮説です。分類の網羅性や特許の関連性を保証する数値ではありません。']
    if not english:
        warnings.append('外部検索用の英語主題語がありません。英語を併記するかLLM翻訳を設定してください。CSVの分析は利用できます。')
    if solid_battery:
        warnings.append('全固体電池の英語対訳を補いました。検索語の追加条件は実行前に確認してください。')
    final_hypotheses = list(hypotheses.values())[:MAX_RECOMMENDATIONS]
    accepted_candidate_keys = [item['key'] for item in final_hypotheses if item['key'] not in before_hypothesis_keys]
    received_counts = dict(english_terms=len(supplied_english), facets=len(supplied_facets),
                           facet_terms=sum(len(row.get('terms', [])) + len(row.get('english_terms', [])) for row in supplied_facets),
                           classifications=len(supplied_hypotheses))
    applied_counts = dict(english_terms=len(accepted_english), facets=len(accepted_facets),
                          facet_terms=sum(len(row['terms']) + len(row['english_terms']) for row in accepted_facets),
                          classifications=len(accepted_candidate_keys))
    if used_llm:
        notices.insert(0, f'LLMから英語主題語{received_counts["english_terms"]}件・観点{received_counts["facets"]}件を受信。英語主題語{applied_counts["english_terms"]}件・観点語{applied_counts["facet_terms"]}件を追加しました。')
        if received_counts['english_terms'] > applied_counts['english_terms']:
            notices.append('英語主題語の重複、検索語として使えない文字、合計8語の上限による除外があります。入力語・既定語を優先します。')
        if received_counts['facet_terms'] > applied_counts['facet_terms']:
            notices.append('観点語の重複・不適切な形式・観点ごとの上限（日本語等12語、英語4語）により追加しなかった語があります。')
        if len(supplied_facets) > 12:
            notices.append('観点の入力は先頭12件を処理しました。')
        if received_counts['classifications'] > applied_counts['classifications']:
            notices.append('分類仮説は公式辞書で確認できるものを最大60件まで表示し、重複・辞書未確認・上限超過は新規追加に数えません。')
        if not any(applied_counts.values()):
            notices.append('今回のLLM提案で新しく追加された語句・分類はありません。既定語との重複または除外条件を確認してください。')
    if len(english) > 6:
        notices.append('英語主題語は8語まで保持します。各検索案で使う主題語は先頭6語です。未使用: ' + '、'.join(english[6:]))
    actual_query_terms = {term.casefold() for term in english[:6]}
    affected_facets = {facet['id'] for facet in accepted_facets if facet['english_terms']}
    query_ids = [query['id'] for query in queries[:MAX_QUERIES]
                 if any(term.casefold() in actual_query_terms for term in accepted_english) or query['facet_id'] in affected_facets]
    proposal = dict(used=used_llm, created_at=datetime.now(timezone.utc).isoformat() if used_llm else None,
                    english_terms=accepted_english, facets=accepted_facets, candidate_keys=accepted_candidate_keys,
                    notices=list(dict.fromkeys(notices))[:24], received_counts=received_counts, applied_counts=applied_counts,
                    query_ids=query_ids)
    return dict(version=1, keywords=keywords.strip(), topic_terms=topic_terms, english_terms=english,
                facets=facets, queued_queries=queries[:MAX_QUERIES],
                candidate_hypotheses=final_hypotheses, warnings=warnings, llm_proposal=proposal)


def analyze_patents(plan: dict, patents: list, previous: dict | None = None) -> dict:
    """Aggregate explicit metadata, preserving human decisions and provenance.

    Unknown relevance contributes review-pending evidence. Excluded documents
    contribute only separate negative counts, never positive support or terms.
    Family support is deduplicated; absent families fall back to publication IDs
    and are separately counted so that the UI need not pretend families are known.
    """
    previous = previous if isinstance(previous, dict) else {}
    facets = plan.get('facets') or deepcopy(_FACETS)
    warnings = set()
    by_id = {}
    for patent in patents[:10000]:
        if not isinstance(patent, dict) or not str(patent.get('id', '')).strip():
            continue
        patent_id = str(patent['id']).strip()
        old = by_id.get(patent_id)
        if old is None or patent.get('label_source') == 'human':
            by_id[patent_id] = dict(patent, id=patent_id)
    groups, excluded, synthetic = {}, 0, 0
    new_term_docs, new_term_families = defaultdict(set), defaultdict(set)
    topic_stop = set(re.findall(r'[a-z][a-z-]{2,}', ' '.join(plan.get('english_terms', [])).lower()))
    for patent_id, patent in by_id.items():
        family = _family(patent)
        is_excluded = patent.get('label') == 'exclude'
        is_positive = patent.get('label') == 'keep' and patent.get('label_source') == 'human'
        is_ai_positive = patent.get('label') == 'keep' and patent.get('label_source') == 'agent'
        is_synthetic = _synthetic(patent)
        excluded += int(is_excluded)
        synthetic += int(is_synthetic)
        aspect_ids = _facets_for(patent, facets)
        evidence = dict(patent_id=patent_id, title=str(patent.get('title', ''))[:300],
                        family_id=str(patent.get('family') or patent.get('family_id') or ''),
                        family_key=family, source=deepcopy(patent.get('source') or patent.get('provenance') or 'uploaded_metadata'),
                        synthetic=is_synthetic, label=patent.get('label'), label_source=patent.get('label_source'),
                        facets=aspect_ids)
        for item in _classifications(patent, warnings):
            if item['key'] not in groups:
                groups[item['key']] = dict(item=item, evidence=[], families=set(), positive_families=set(),
                                          negative_families=set(), positives=0, negatives=0, synthetic=0,
                                          facets=set(), docs=set(), negative_ids=[], real_families=set(),
                                          ai_positives=0, ai_negatives=0, human_negatives=0, ai_positive_families=set(), ai_negative_families=set(),
                                          human_negative_families=set(), metadata_ids=set(), derived_ids=set(),
                                          unverified_ids=set(), classification_origins=[])
            group = groups[item['key']]
            for origin in item.get('classification_origins', []):
                if origin not in group['classification_origins']:
                    group['classification_origins'].append(deepcopy(origin))
            if is_excluded:
                group['negatives'] += 1
                group['negative_families'].add(family)
                group['ai_negative_families' if patent.get('label_source') == 'agent' else 'human_negative_families'].add(family)
                group['ai_negatives' if patent.get('label_source') == 'agent' else 'human_negatives'] += 1
                if len(group['negative_ids']) < 12:
                    group['negative_ids'].append(patent_id)
                continue
            group['docs'].add(patent_id)
            has_explicit = any(origin.get('type') == 'patent' and origin.get('code_verified') for origin in item['classification_origins'])
            has_derived = any(origin.get('type') in ('fi_to_ipc', 'fterm_theme_to_ipc') for origin in item['classification_origins'])
            if has_explicit:
                group['metadata_ids'].add(patent_id)
            if has_derived:
                group['derived_ids'].add(patent_id)
            if not has_explicit and not has_derived:
                group['unverified_ids'].add(patent_id)
            group['families'].add(family)
            group['facets'].update(aspect_ids)
            group['synthetic'] += int(is_synthetic)
            if not is_synthetic:
                group['real_families'].add(family)
            if is_positive:
                group['positives'] += 1
                group['positive_families'].add(family)
            if is_ai_positive:
                group['ai_positives'] += 1
                group['ai_positive_families'].add(family)
            entry_evidence = dict(evidence, evidence_status=item['evidence_status'],
                                  classification_origins=deepcopy(item['classification_origins']))
            # Retain one representative per family, preferring a human keep.
            represented = next((i for i, e in enumerate(group['evidence']) if e['family_key'] == family), None)
            current_priority = 2 if is_positive else 1 if is_ai_positive else 0
            old_priority = (2 if group['evidence'][represented].get('label_source') == 'human' else 1) if represented is not None and group['evidence'][represented].get('label') == 'keep' else 0
            if represented is not None and current_priority > old_priority:
                group['evidence'][represented] = deepcopy(entry_evidence)
            elif represented is None and (len(group['evidence']) < 16 or (is_positive and not any(e.get('label_source') == 'human' and e.get('label') == 'keep' for e in group['evidence']))):
                if len(group['evidence']) >= 16:
                    group['evidence'].pop()
                group['evidence'].append(deepcopy(entry_evidence))
        if not is_excluded:
            text = (str(patent.get('title', '')) + ' ' + str(patent.get('abstract', '')))[:16000].lower()
            for term in set(re.findall(r'\b[a-z][a-z-]{3,39}\b', text)) - _STOPWORDS - topic_stop:
                new_term_docs[term].add(patent_id)
                new_term_families[term].add(family)
    recommendations = []
    for group in groups.values():
        if not group['docs']:
            continue
        family_count = len(group['families'])
        positive_family_count = len(group['positive_families'])
        negative_family_count = len(group['negative_families'])
        ai_positive_family_count = len(group['ai_positive_families'])
        ai_bonus_count = len(group['ai_positive_families'] - group['positive_families'])
        score = round(family_count + 1.5 * positive_family_count + .75 * ai_bonus_count
                      - .5 * len(group['human_negative_families'])
                      - .25 * len(group['ai_negative_families'] - group['human_negative_families']), 2)
        item = deepcopy(group['item'])
        reason = f'分類根拠: {len(group["docs"])}公報、{family_count}集計単位（ファミリー不明は公報単位）。'
        reason += f' 分類欄で確認 {len(group["metadata_ids"])}公報 / FI・テーマ対応からの候補 {len(group["derived_ids"])}公報。'
        if group['derived_ids']:
            reason += ' 対応からのIPCは探索候補で、公報に付与されたIPCではありません。'
        reason += ' 利用者の「必要」による支持あり。' if positive_family_count else ' 技術への適合性は未判定です。'
        if ai_positive_family_count:
            reason += f' AIの「必要」{group["ai_positives"]}公報を別集計し、{ai_bonus_count}集計単位に0.75加点（人の支持は1.5、同一単位は人を優先）。'
        if group['synthetic']:
            reason += f' 合成デモ {group["synthetic"]}公報を含みます。'
        item.update(evidence_status='patent_metadata' if group['metadata_ids'] else 'derived_candidate' if group['derived_ids'] else 'unverified_metadata',
                    relevance_status='human_supported' if positive_family_count else 'agent_supported' if ai_positive_family_count else 'review_pending',
                    support_count=len(group['docs']), family_count=family_count,
                    positive_count=group['positives'], positive_family_count=positive_family_count,
                    ai_positive_count=group['ai_positives'], ai_positive_family_count=ai_positive_family_count,
                    explicit_metadata_count=len(group['metadata_ids']), derived_candidate_count=len(group['derived_ids']),
                    unverified_metadata_count=len(group['unverified_ids']),
                    classification_origins=group['classification_origins'],
                    score_weights=dict(base_family=1, human_keep=1.5, agent_keep=.75, human_exclude=-.5, agent_exclude=-.25),
                    negative_count=group['negatives'], negative_family_count=negative_family_count,
                    ai_negative_count=group['ai_negatives'], human_negative_count=group['human_negatives'],
                    ai_negative_family_count=len(group['ai_negative_families']), human_negative_family_count=len(group['human_negative_families']),
                    excluded_patent_ids=group['negative_ids'], synthetic_count=group['synthetic'],
                    real_family_count=len(group['real_families']),
                    facets=sorted(group['facets']), evidence=group['evidence'], score=score, reason=reason,
                    origins=[dict(type='discovery', label='特許の分類・FI・テーマ対応根拠', patent_id=e['patent_id'],
                                  synthetic=e['synthetic']) for e in group['evidence']])
        recommendations.append(item)
    recommendations.sort(key=lambda r: (-r['positive_family_count'], -r['score'], r['key']))
    all_keys = sorted(r['key'] for r in recommendations)
    terms = [dict(term=term, support_count=len(ids), family_count=len(new_term_families[term]),
                  patent_ids=sorted(ids)[:6], reason='対象公報の名称・要約から抽出した語。関連性は未判定です。')
             for term, ids in new_term_docs.items()]
    terms.sort(key=lambda t: (-t['family_count'], t['term']))
    terms = terms[:18]
    next_queries = []
    # Half the budget stays outside classifications; the other half follows
    # documentary IPC support. Every query still contains the original subject.
    for facet in facets[:3]:
        query = _query(plan.get('english_terms', []), facet['id'], facet['label'], facet['english_terms'][2:4],
                       reason='分類条件を付けず、別の観点語で取りこぼしの候補を探します。')
        if query:
            next_queries.append(query)
    for term in terms[:3]:
        query = _query(plan.get('english_terms', []), 'vocabulary', '発見語: ' + term['term'], [term['term']],
                       reason='実際の名称・要約から得た語を主題語にANDします。')
        if query:
            next_queries.append(query)
    for item in recommendations:
        if len(next_queries) >= MAX_QUERIES:
            break
        if item['kind'] != 'IPC' or not item['verified'] or not item['real_family_count']:
            continue
        query = _query(plan.get('english_terms', []), 'classification', '分類から探索: ' + item['code'],
                       ipc_codes=[item['code']], reason=('特許に記載されたIPCと主題語を組み合わせ、関連範囲を確かめます。' if item['explicit_metadata_count'] else
                                                      'FI・Fタームのテーマ範囲から得たIPC候補と主題語を組み合わせます。付与IPCではなく探索仮説です。'))
        if query:
            next_queries.append(query)
    next_queries = list({q['id']: q for q in next_queries}.values())[:MAX_QUERIES]
    families = sorted({_family(p) for p in by_id.values() if p.get('label') != 'exclude'})
    seen_ids = sorted(by_id)
    old_ids = set(previous.get('seen_patent_ids', []))
    old_keys = set(previous.get('seen_classification_keys', []))
    old_families = set(previous.get('seen_family_ids', []))
    if synthetic:
        warnings.add('合成デモは実在特許の検索結果ではありません。外部IPC探索の裏付けには使いません。')
    fallback_count = sum(not bool(p.get('family') or p.get('family_id')) for p in by_id.values() if p.get('label') != 'exclude')
    if fallback_count:
        warnings.add(f'{fallback_count}公報はファミリー情報がないため公報単位で集計しました。')
    warnings.add('支持件数は分類欄とFI・Fターム対応からの候補の集計です（両方に同じ公報を含む場合あり）。由来を区別し、AIの必要支持は人と別集計・低い重みで扱います。関連性・網羅性・検索再現率を表す値ではありません。')
    counts = dict(documents=len(by_id), families=len(families), family_fallback_documents=fallback_count,
                  excluded=excluded, synthetic=synthetic, classifications=len(all_keys),
                  new_documents=len(set(seen_ids) - old_ids), new_classifications=len(set(all_keys) - old_keys),
                  new_families=len(set(families) - old_families))
    return dict(recommendations=recommendations[:MAX_RECOMMENDATIONS], new_terms=terms, next_queries=next_queries,
                counts=counts, seen_patent_ids=seen_ids, seen_family_ids=families, seen_classification_keys=all_keys,
                warnings=sorted(warnings), no_new_evidence=not counts['new_documents'] and not counts['new_classifications'])
