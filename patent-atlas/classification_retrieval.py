"""Element-based retrieval from official definitions. No technology/code profiles.

An index hit establishes textual support, not relevance, novelty or recall.
IPC and FI are searched independently; codes come exclusively from their tables.
"""
from array import array
from collections import defaultdict
from functools import lru_cache
from heapq import nlargest
import math
import re
import unicodedata

import classification_catalog as ipc
import fi_catalog as fi
from classification_translations import _bundle

STOP = set('a an and are as at be by for from in into is it of on or the to with using used thereof therefor'.split())


def phrases(text):
    text = unicodedata.normalize('NFKC', str(text))
    parts = re.findall(r'"([^"]+)"|([^",、;；\n]+)', text)
    return list(dict.fromkeys(t.strip() for pair in parts for t in pair if t.strip()))[:24]


def clean(text):
    # Parenthetical exclusion/cross-references must not cause unrelated hits.
    text = unicodedata.normalize('NFKC', str(text)).casefold()
    for _ in range(8):
        newer = re.sub(r'\([^()]*\)|\[[^\[\]]*\]', ' ', text)
        if newer == text: break
        text = newer
    return text


def tokens(text):
    values = set()
    for word in re.findall(r'[a-z]{2,}|[一-龯々ぁ-んァ-ヶー]{2,}', text):
        if word.isascii():
            if word in STOP: continue
            if word.endswith('ies') and len(word)>4: word=word[:-3]+'y'
            elif word.endswith('s') and not word.endswith('ss') and len(word)>3: word=word[:-1]
            values.add(word)
        else:
            values.update(word[i:i+2] for i in range(len(word)-1))
    return values


@lru_cache(maxsize=2)
def _index(kind):
    rows = ipc._catalog()[1] if kind == 'IPC' else fi._data()['records']
    japanese = _bundle()['records'] if kind == 'IPC' else {}
    docs, postings = [], defaultdict(lambda: array('I'))
    for row in rows.values():
        # Upper classifications remain available through the hierarchy browser.
        if '/' not in row[0]: continue
        ja = japanese.get(row[0], {}).get('title_ja', '') if kind == 'IPC' else row[5]
        title = clean(row[1] + ' ' + ja)
        number = len(docs); docs.append((row[0], title, row[2]))
        for token in tokens(title): postings[token].append(number)
    codes = {doc[0]: i for i, doc in enumerate(docs)}
    children = defaultdict(list)
    for i,doc in enumerate(docs):
        if doc[2] in codes: children[codes[doc[2]]].append(i)
    return docs, dict(postings), codes, dict(children)


@lru_cache(maxsize=512)
def _rank(kind, term):
    query = clean(term); required = tokens(query)
    if not required: return ()
    docs, postings, codes, children = _index(kind)
    scores = defaultdict(float)
    own_ids = set().union(*(postings.get(t, ()) for t in required))
    for token in required:
        ids = postings.get(token, ())
        weight = math.log(1+len(docs)/(1+len(ids)))
        for number in ids: scores[number] += weight
        # An inherited definition must be discoverable before the top-N cut.
        # Otherwise a narrow entry with an elliptical title is never retrieved.
        visited = set(ids); stack = [c for i in ids for c in children.get(i, ())]
        while stack:
            number = stack.pop()
            if number in visited: continue
            visited.add(number)
            if number in own_ids: scores[number] += weight*.6
            stack.extend(children.get(number, ()))
    ranked = []
    for number, own_score in nlargest(500, scores.items(), key=lambda item:item[1]):
        code, title, parent = docs[number]
        own = tokens(title); context = set(own)
        # Group ancestors supply inherited context (e.g. secondary cells),
        # never broad section/class/subclass headings or their cross-references.
        for _ in range(16):
            if parent not in codes: break
            _, text, parent = docs[codes[parent]]; context.update(tokens(text))
        coverage = len(required & context)/len(required)
        threshold = .65 if re.search(r'[一-龯ぁ-んァ-ヶ]', query) else 1
        if coverage < threshold or not required & own: continue
        kanji = set(re.findall(r'[一-龯]', query))
        if kanji and not kanji <= set(''.join(context)): continue
        exact = query in title
        score = coverage*10 + (8 if exact else 0) + own_score/math.sqrt(max(4,len(own)))
        # Prefer a broad entry over a long specialised entry with equal support.
        ranked.append((score, code, coverage, exact))
    ranked.sort(key=lambda item:(-item[0], len(item[1]), item[1]))
    return tuple(ranked[:100])


def search_definitions(kind, term, limit=8):
    rows = []
    for score, code, coverage, exact in _rank(kind, term)[:max(0,min(100,limit))]:
        row = ipc.lookup('IPC', code) if kind == 'IPC' else fi.lookup(code)
        row.update(retrieval_score=round(score,3), lexical_coverage=round(coverage,3),
                   match_type='phrase' if exact else 'definition_terms')
        rows.append(row)
    return rows


def _with_broader(kind, rows):
    result, seen = [], set()
    for row in rows:
        relatives = [row]
        current = row
        while current.get('parent') and '/' in current['parent']:
            current = ipc.parent('IPC', current['code']) if kind == 'IPC' else fi.parent(current['code'])
            if not current: break
            if current['level']=='main_group':
                current.update(retrieval_score=row['retrieval_score'], lexical_coverage=row['lexical_coverage'],
                               match_type='broader_of_match', matched_code=row['code'])
                relatives.append(current)
                break
        for candidate in relatives:
            if candidate['code'] not in seen:
                seen.add(candidate['code']); result.append(candidate)
    return result[:8]


def input_elements(text):
    return [dict(id=f'e{i}', name=term, terms=[term], relation='入力語句')
            for i,term in enumerate(phrases(text)[:8],1)]


def retrieve_elements(elements, limit=48):
    result, buckets = {}, []
    for element in elements[:16]:
        bucket, term_buckets = [], []
        terms = list(dict.fromkeys(element.get('terms', [])))[:8]
        for term in terms:
            kind_buckets = []
            for kind in ('IPC','FI'):
                kind_bucket = []
                for row in _with_broader(kind, search_definitions(kind, term, limit=4)):
                    key = kind+':'+row['code']
                    match = dict(id=element['id'], name=element['name'], term=term,
                                 match_type=row['match_type'], lexical_coverage=row['lexical_coverage'],
                                 matched_code=row.get('matched_code',row['code']))
                    if key not in result:
                        row.update(key=key, reason='', facets=[element['id']], group=element['name'], terms=[],
                                   matched_elements=[], recommendation_method='element_dictionary',
                                   evidence_status='hypothesis', relevance_status='review_pending')
                        result[key] = row
                    hit = result[key]
                    if match not in hit['matched_elements']: hit['matched_elements'].append(match)
                    if term not in hit['terms']: hit['terms'].append(term)
                    if element['id'] not in hit['facets']: hit['facets'].append(element['id'])
                    kind_bucket.append(key)
                kind_buckets.append(kind_bucket)
            term_buckets.append([b[i] for i in range(8) for b in kind_buckets if i<len(b)])
        for i in range(max((len(b) for b in term_buckets), default=0)):
            for b in term_buckets:
                if i<len(b) and b[i] not in bucket: bucket.append(b[i])
        # Round-robin across elements and classification systems: early terms
        # must not consume the complete display budget.
        buckets.append(bucket)
    ordered = []
    for position in range(max((len(b) for b in buckets), default=0)):
        for bucket in buckets:
            if position<len(bucket) and bucket[position] not in ordered: ordered.append(bucket[position])
    for key in ordered:
        row = result[key]
        row['reason'] = ' / '.join(dict.fromkeys('要素「'+m['name']+'」の語「'+m['term']+'」'+
            ('に一致した '+m['matched_code']+' の上位分類' if m['match_type']=='broader_of_match' else 'と分類定義を照合')
            for m in row['matched_elements']))[:700] + '。語句一致・階層による候補です。技術への適合性は未確認です。'
    return [result[key] for key in ordered[:limit]]


def retrieve(text, limit=24):
    return retrieve_elements(input_elements(text), limit=limit)
