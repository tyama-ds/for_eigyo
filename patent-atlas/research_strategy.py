"""Bounded research plans built from explicit user inputs and selected seeds.

No I/O occurs in the offline path. The optional LLM path delegates transport to
the existing adapter, validates the complete response, and never mutates caller
state. The route must commit the returned plan atomically after success.
"""
from __future__ import annotations

import copy
import re

from catalog import keywords as parse_keywords
from llm import complete
from prompt_templates import PROMPT_VERSION, RESEARCH_PLAN_SYSTEM

MAX_CONCEPTS = 8
MAX_TERMS = 12
MAX_SOURCES = 20
_ROLES = {'required', 'optional', 'exclude'}
_MODES = {'target', 'discover', 'examples', 'tools'}
_DEFAULT_PURPOSE = '探索・母集団作成'
_OR_GROUP_PATTERN = r'[A-Za-z][A-Za-z0-9_-]{0,31}'
# These are deliberately small, reviewable retrieval axes, not a synonym
# classifier. Unknown phrases stay independent until a user or LLM reviews
# their meaning. In particular, a broad functional phrase is not evidence of
# strict synonymy or of relevance to the selected patents.
_OFFLINE_OR_AXES = (
    ('driving', ('自動運転', '自律走行', '自動走行', '運転システム',
                 'autonomous driving', 'automated driving', 'self driving', 'driving system'),
     '自動運転・運転システムの探索軸。厳密な同義語ではなく、代替表現として広めに拾う仮の和集合です。'),
    ('battery', ('電池', 'battery', 'batteries'),
     '電池の日本語・英語表記を和集合にします。対象となる電池の種類は結果で確認してください。'),
    ('solid_electrolyte', ('固体電解質', 'solid electrolyte', 'solid electrolytes'),
     '固体電解質の日本語・英語表記を和集合にします。材料の詳細は別の観点で確認してください。'),
)


def suggest_keyword_or_group(term):
    """Return a conservative retrieval-axis (ID, reason), or ('', '') for unknown text.

    The result only proposes a union; it never adds terms or equates the input
    with a mandatory user aspect. Callers must preserve explicit AND/NOT rules.
    """
    folded = term.casefold()
    for group_id, phrases, reason in _OFFLINE_OR_AXES:
        if folded in phrases:
            return group_id, reason
    return '', ''


def _text(value, name, limit, *, empty=True):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'{name}は{limit}文字以内の文字列にしてください。')
    value = value.strip()
    if not empty and not value:
        raise ValueError(f'{name}が空です。')
    return value


def _strings(value, name, maximum, length, *, empty_items=False):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f'{name}は{maximum}件以内の配列にしてください。')
    result = [_text(item, name, length, empty=empty_items) for item in value]
    if len({item.casefold() for item in result}) != len(result):
        raise ValueError(f'{name}に重複があります。')
    return result


def normalize_brief(brief):
    """Validate without dropping or modifying the caller's input object."""
    if not isinstance(brief, dict):
        raise ValueError('探索条件はJSONオブジェクトにしてください。')
    mode = brief.get('entry_mode', 'discover')
    if not isinstance(mode, str) or mode not in _MODES:
        raise ValueError('開始方法はtarget / discover / examples / toolsから選んでください。')
    result = dict(entry_mode=mode,
                  purpose=_text(brief.get('purpose', ''), '検索目的', 300),
                  goal=_text(brief.get('goal', ''), '調査したい内容', 5000),
                  keywords=_text(brief.get('keywords', ''), 'キーワード', 5000),
                  user_aspects=_strings(brief.get('user_aspects', []), '観点', MAX_CONCEPTS, 160),
                  target_ids=_strings(brief.get('target_ids', []), 'ターゲットID', MAX_SOURCES, 200))
    # Keep the existing application's phrase/keyword syntax; never parse raw
    # Boolean expressions into a trusted query here.
    terms = parse_keywords(result['keywords'])
    for term in terms:
        _term(term)
    if not (result['goal'] or terms or result['user_aspects'] or result['target_ids']):
        raise ValueError('調査したい内容、キーワード、観点、ターゲットのいずれかを入力してください。')
    return result


def _term(value):
    value = _text(value, '検索語', 160, empty=False)
    # Renderer-owned Boolean syntax cannot be smuggled through a term field.
    if (re.search(r'[\x00-\x1f\x7f*?\[\]{}|<>:=]', value)
            or re.search(r'\b(?:AND|OR|NOT|ANDNOT|NEAR|ADJ|PROX)\b', value)
            or re.search(r'/(?:IP|FI|FT|CP|TX|TI|AB|CL)\b', value, re.I)):
        raise ValueError('検索語にはBoolean式・フィールド指定・ワイルドカードではなく単語やフレーズを指定してください。')
    return value


def _term_array(value, name):
    """Validate raw bounds before any OR-equivalent duplicate removal."""
    if not isinstance(value, list) or len(value) > MAX_TERMS:
        raise ValueError(f'{name}は{MAX_TERMS}件以内の配列にしてください。')
    return [_term(term) for term in value]


def _unique_terms(terms, preferred=()):
    # If the exact user spelling was present, keep it as the representative of
    # an otherwise equivalent casefold group. Never restore a missing input.
    literal = {term.casefold(): term for term in preferred if term in terms}
    seen, unique = set(), []
    for term in terms:
        folded = term.casefold()
        if folded not in seen:
            seen.add(folded)
            unique.append(literal.get(folded, term))
    return unique


def _metadata_values(value, name):
    if value in (None, ''):
        return []
    if isinstance(value, str):
        # Do not split FI commas or infer IPC from another classification system.
        value = [piece.strip() for piece in re.split(r'[;；\n|]', value) if piece.strip()]
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError(f'ターゲットの{name}メタデータを確認してください。')
    result = []
    for item in value:
        code = _text(item, name, 200, empty=False)
        if code not in result:
            result.append(code)
    return result


def selected_sources(patents, target_ids):
    """Return only selected IDs; classification metadata is never inferred."""
    if not isinstance(patents, list):
        raise ValueError('特許一覧は配列にしてください。')
    selected, seen = {}, set()
    for row in patents:
        if not isinstance(row, dict) or row.get('id') not in target_ids:
            continue
        patent_id = row['id']
        if patent_id in seen:
            raise ValueError('ターゲットIDが特許一覧で重複しています。')
        seen.add(patent_id)
        title = row.get('title') or ''
        abstract = row.get('abstract') or ''
        if not isinstance(title, str) or not isinstance(abstract, str):
            raise ValueError('ターゲットのタイトル・要約を確認してください。')
        selected[patent_id] = dict(
            id=patent_id, title=title[:250], abstract=abstract[:1500],
            abstract_available=bool(abstract.strip()), claims_available=False,
            text_truncated=len(title) > 250 or len(abstract) > 1500,
            evidence_scope='title_and_abstract' if abstract.strip() else 'title_only',
            ipc=_metadata_values(row.get('ipc'), 'IPC'),
            fi=_metadata_values(row.get('fi'), 'FI'),
            fterm=_metadata_values(row.get('fterm'), 'F-term'),
            cpc=_metadata_values(row.get('cpc'), 'CPC'))
    missing = [patent_id for patent_id in target_ids if patent_id not in selected]
    if missing:
        raise ValueError('選択されたターゲットが特許一覧にありません。取り込みと選択を確認してください。')
    return [selected[patent_id] for patent_id in target_ids]


def _evidence_ids(terms, sources):
    return [row['id'] for row in sources
            if any(term.casefold() in (row['title'] + '\n' + row['abstract']).casefold()
                   for term in terms)]


def _aspect_role(value):
    match = re.match(r'^(必須|任意|除外|required|optional|exclude)\s*[:：]\s*(.+)$', value, re.I)
    if not match:
        return 'required', value
    role = {'必須': 'required', '任意': 'optional', '除外': 'exclude'}.get(match[1], match[1].lower())
    return role, match[2].strip()


def _locked_and_aspect(value):
    return bool(re.match(r'^(必須|required)\s*[:：]', value, re.I))


def _anchors(brief, sources):
    concepts, omitted_keywords = [], []
    for index, aspect in enumerate(brief['user_aspects'], 1):
        role, literal = _aspect_role(aspect)
        _term(literal)
        concepts.append(dict(id=f'aspect{index}', name=aspect, role=role, terms=[literal],
                             abstract_terms=[], reason='利用者が指定した観点。検索語への分解は未実施です。',
                             evidence_ids=_evidence_ids([literal], sources), or_group='', group_reason='',
                             locked_and=_locked_and_aspect(aspect)))
    existing_terms = {term.casefold() for concept in concepts for term in concept['terms']}
    for index, term in enumerate(parse_keywords(brief['keywords']), 1):
        if term.casefold() in existing_terms:
            continue
        if len(concepts) >= MAX_CONCEPTS:
            omitted_keywords.append(term)
            continue
        or_group, group_reason = suggest_keyword_or_group(term)
        concepts.append(dict(id=f'keyword{index}', name=term, role='required', terms=[term],
                             abstract_terms=[], reason='入力キーワード。原文を保持しています。',
                             evidence_ids=_evidence_ids([term], sources),
                             or_group=or_group, group_reason=group_reason, locked_and=False))
        existing_terms.add(term.casefold())
    return concepts, omitted_keywords


def _questions(brief, sources, omitted_keywords):
    questions = []
    if brief['entry_mode'] == 'discover' and not sources:
        questions.append('まず広いキーワード検索で実在する代表特許を探し、要約・付与分類を確認してターゲットに選びます。分類候補は並行して比較できます。')
    if brief['entry_mode'] == 'target' and not sources:
        questions.append('ターゲット特許を取り込み、対象の特許を選択してください。選択前の計画はキーワード由来の仮説です。')
    if sources and any(not row['abstract_available'] for row in sources):
        questions.append('要約がないターゲットがあります。タイトルと付与分類だけでは技術要素を確認できないため、要約や原文を確認してください。')
    if sources:
        questions.append('この計画では請求項全文を読み込んでいません。必須構成や作用関係が目的に合うか、原文と照合してください。')
    if omitted_keywords:
        questions.append('8観点の上限により未割当のキーワードがあります。元の入力は保持しています。観点を整理してから検索式に適用してください。')
    if not brief['purpose']:
        questions.append('検索の目的は探索・母集団作成でよいですか。目的に応じた期間・地域・必要な観点を確認してください。')
    return questions[:5]


def response_schema(source_ids):
    """A bounded schema plus stricter semantic validation after generation."""
    evidence = {'type': 'string'}
    if source_ids:
        evidence['enum'] = list(source_ids)
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['purpose', 'summary', 'concepts', 'questions'],
        'properties': {
            'purpose': {'type': 'string'}, 'summary': {'type': 'string'},
            'questions': {'type': 'array', 'maxItems': 5, 'items': {'type': 'string'}},
            'concepts': {'type': 'array', 'minItems': 1, 'maxItems': MAX_CONCEPTS,
                         'items': {'type': 'object', 'additionalProperties': False,
                                   'required': ['id', 'name', 'role', 'terms', 'abstract_terms', 'reason', 'evidence_ids'],
                                   'properties': {
                                       'id': {'type': 'string'}, 'name': {'type': 'string'},
                                       'role': {'type': 'string', 'enum': sorted(_ROLES)},
                                       'terms': {'type': 'array', 'minItems': 1, 'maxItems': MAX_TERMS, 'items': {'type': 'string'}},
                                       'abstract_terms': {'type': 'array', 'maxItems': MAX_TERMS, 'items': {'type': 'string'}},
                                       'reason': {'type': 'string'},
                                       'or_group': {'type': 'string', 'maxLength': 32,
                                                    'pattern': r'^$|^[A-Za-z][A-Za-z0-9_-]{0,31}$'},
                                       'group_reason': {'type': 'string', 'maxLength': 300},
                                       'evidence_ids': {'type': 'array', 'maxItems': len(source_ids), 'items': evidence},
                                   }}},
        },
    }


def _validate_result(result, anchors, source_ids, purpose):
    if not isinstance(result, dict) or set(result) != {'purpose', 'summary', 'concepts', 'questions'}:
        raise ValueError('LLMの探索計画にはpurpose / summary / concepts / questionsが必要です。')
    returned_purpose = _text(result['purpose'], '検索目的', 300, empty=False)
    if returned_purpose != purpose:
        raise ValueError('LLMが検索目的を書き換えました。元の目的を保持して再実行してください。')
    summary = _text(result['summary'], '探索方針', 300, empty=False)
    questions = _strings(result['questions'], '確認事項', 5, 200)
    items = result['concepts']
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CONCEPTS:
        raise ValueError('LLMの観点は1〜8件の配列である必要があります。')
    expected = {item['id']: item for item in anchors}
    seen, concepts, deduplicated = set(), [], False
    independent_inputs = 0
    required_fields = {'id', 'name', 'role', 'terms', 'abstract_terms', 'reason', 'evidence_ids'}
    allowed_fields = required_fields | {'or_group', 'group_reason'}
    for item in items:
        if not isinstance(item, dict) or not required_fields <= set(item) or not set(item) <= allowed_fields:
            raise ValueError('LLMの観点に必要な項目が不足しているか、不要な項目が含まれています。')
        concept_id = _text(item['id'], '観点ID', 64, empty=False)
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', concept_id) or concept_id in seen:
            raise ValueError('LLMの観点IDの形式または重複を確認してください。')
        seen.add(concept_id)
        name = _text(item['name'], '観点名', 160, empty=False)
        role = item['role']
        if not isinstance(role, str) or role not in _ROLES:
            raise ValueError('LLMの観点roleはrequired / optional / excludeのみです。')
        anchor = expected.get(concept_id)
        # Older models may omit new fields. Keep the explainable anchor default
        # in that case; an explicit empty string still means independent AND.
        or_group = _text(item.get('or_group', anchor.get('or_group', '') if anchor else ''), '和集合グループID', 32)
        if or_group and not re.fullmatch(_OR_GROUP_PATTERN, or_group):
            raise ValueError('和集合グループIDは英字で始まる32文字以内の英数字・ハイフン・アンダースコアにしてください。')
        default_group_reason = anchor.get('group_reason', '') if anchor and or_group == anchor.get('or_group') else ''
        group_reason = _text(item.get('group_reason', default_group_reason), '和集合の理由', 300)
        if not or_group:
            group_reason = ''
        raw_terms = _term_array(item['terms'], '検索語')
        raw_abstract_terms = _term_array(item['abstract_terms'], '抽象語')
        terms = _unique_terms(raw_terms, anchor['terms'] if anchor else ())
        concrete = {term.casefold() for term in terms}
        abstract_terms = [term for term in _unique_terms(raw_abstract_terms)
                          if term.casefold() not in concrete]
        deduplicated |= len(terms) + len(abstract_terms) < len(raw_terms) + len(raw_abstract_terms)
        if not terms or len(terms) + len(abstract_terms) > MAX_TERMS:
            raise ValueError('各観点に検索語を1つ以上、抽象語と合わせて12語以内で指定してください。')
        reason = _text(item['reason'], '提案理由', 300, empty=False)
        evidence_ids = _strings(item['evidence_ids'], '根拠の特許ID', MAX_SOURCES, 200)
        if not set(evidence_ids) <= set(source_ids):
            raise ValueError('LLMが未選択・存在しない特許を根拠として返しました。')
        if anchor:
            if name != anchor['name'] or role != anchor['role']:
                raise ValueError('LLMが利用者の観点名または役割を変更しました。元の条件を保持して再実行してください。')
            if concept_id.startswith('keyword') and not set(anchor['terms']) <= set(terms):
                raise ValueError('LLMが入力キーワードを省略しました。元の条件を保持して再実行してください。')
        elif role == 'exclude':
            raise ValueError('LLMが利用者の指定にない除外観点を作成しました。除外候補は先に確認してください。')
        locked_and = bool(anchor and anchor.get('locked_and'))
        if or_group and (role == 'exclude' or locked_and):
            # A declared requirement cannot become an alternative to another
            # condition merely because the model reused a group identifier.
            or_group, group_reason = '', ''
            independent_inputs += 1
        concepts.append(dict(id=concept_id, name=name, role=role, terms=terms,
                             abstract_terms=abstract_terms, reason=reason, evidence_ids=evidence_ids,
                             or_group=or_group, group_reason=group_reason, locked_and=locked_and))
    # Every raw proposal has passed validation before applying the output cap.
    # Preserve missing user inputs literally; never guess that a new model ID
    # is a renamed anchor based on similar names or terms.
    by_id = {concept['id']: concept for concept in concepts}
    ordered, restored = [], 0
    for anchor in anchors:
        if anchor['id'] in by_id:
            ordered.append(by_id[anchor['id']])
        else:
            literal = copy.deepcopy(anchor)
            literal['reason'] = 'LLMがこの入力観点のIDを返さなかったため、利用者の原文を保持しました。同義語生成・機能抽象化は未実施です。'
            ordered.append(literal)
            restored += 1
    additions = [concept for concept in concepts if concept['id'] not in expected]
    remaining = MAX_CONCEPTS - len(ordered)
    omitted = max(0, len(additions) - remaining)
    concepts = ordered + additions[:remaining]
    notices = []
    if restored:
        notices.append(f'LLM応答にない入力観点{restored}件を利用者の原文で保持しました（抽象化未実施）')
    if omitted:
        notices.append(f'入力観点を優先し、8観点の上限によりLLM追加提案{omitted}件を省略しました')
    if deduplicated:
        notices.append('同一語の重複を整理しました（大小文字を同一視し、具体語を優先）')
    if independent_inputs:
        notices.append(f'利用者の必須・除外条件{independent_inputs}件は他観点との和集合にせず独立条件として保持しました')
    if notices:
        questions = ['。'.join(notices) + '。'] + questions
    questions = questions[:5]
    return dict(purpose=purpose, summary=summary, concepts=concepts, questions=questions)


def plan_research(brief, patents, settings, *, use_llm=False):
    """Return a JSON-safe proposal; no state writes, search, or implicit API use."""
    if not isinstance(use_llm, bool):
        raise ValueError('LLMを使うかどうかは真偽値にしてください。')
    normalized = normalize_brief(brief)
    sources = selected_sources(patents, normalized['target_ids'])
    source_ids = [row['id'] for row in sources]
    anchors, omitted_keywords = _anchors(normalized, sources)
    questions = _questions(normalized, sources, omitted_keywords)
    purpose = normalized['purpose'] or _DEFAULT_PURPOSE
    if use_llm:
        if not isinstance(settings, dict):
            raise ValueError('LLM接続設定を確認してください。')
        payload_anchors = copy.deepcopy(anchors)
        for anchor in payload_anchors:
            if anchor['id'].startswith('keyword'):
                anchor['preserve_terms'] = list(anchor['terms'])
        payload = dict(brief=copy.deepcopy(normalized), sources=copy.deepcopy(sources),
                       effective_purpose=purpose, anchor_concepts=payload_anchors,
                       unmapped_keywords=omitted_keywords)
        result = complete(copy.deepcopy(settings), RESEARCH_PLAN_SYSTEM, payload,
                          response_schema=response_schema(source_ids))
        plan = _validate_result(result, anchors, source_ids, purpose)
        # Missing source text and unassigned input conditions are facts from
        # Python, so they remain visible even if the model omits them.
        plan['questions'] = list(dict.fromkeys(questions + plan['questions']))[:5]
        plan['method'] = 'llm'
    else:
        concepts = copy.deepcopy(anchors)
        if not concepts and sources:
            for index, row in enumerate(sources[:MAX_CONCEPTS], 1):
                title = row['title'].strip()
                if not title or len(title) > 160:
                    continue
                try:
                    title = _term(title)
                except ValueError:
                    continue
                concepts.append(dict(id=f'seed{index}', name=title, role='optional', terms=[title],
                                     abstract_terms=[], reason='選択した特許のタイトル原文。構成要素の分解・機能抽象化は未実施です。',
                                     evidence_ids=[row['id']], or_group='', group_reason='', locked_and=False))
        if not concepts:
            questions.insert(0, '説明文から観点を分解するにはLLMで計画するか、キーワード・観点を入力してください。')
        plan = dict(purpose=purpose,
                    summary='入力語と指定観点を保持し、既知の同一探索軸だけ仮の和集合（OR）に整理しました。独立した観点は積集合（AND）です。未知語の同義語生成・機能抽象化は未実施です。既知の必要例を検索結果で回収できるか確認します。',
                    concepts=concepts, questions=questions[:5], method='offline')
    plan.update(prompt_version=PROMPT_VERSION, brief=copy.deepcopy(normalized),
                sources=sources, source_ids=source_ids, unmapped_keywords=omitted_keywords)
    return plan
