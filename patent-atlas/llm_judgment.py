"""Example-guided LLM judgments, with validated and independently committed batches.

This evaluates patent text using a configured model; it does not train model weights.
Network access and workspace writes are injected by the caller.
"""
import copy
import hashlib
import json
import math
import re
from collections import Counter
from prompt_templates import JUDGMENT_SYSTEM, PROMPT_VERSION
from query_formats import tree_from_data, tree_data


BATCH_SIZE = 4
EXAMPLES_PER_LABEL = 3
SYSTEM = JUDGMENT_SYSTEM
RETRY_INSTRUCTION = '''
前回の回答は判定の件数・ID・必須項目の形式要件を満たしませんでした。
前回の回答の一部を修正して返すのではなく、このバッチ全件の decisions を作り直してください。
required_ids の各IDを正確に1回ずつ出力し、required_decision_count 件すべてについて
decision、confidence、relevance、reason を指定の形式で必ず含めてください。'''


def _finite_probability(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + 'は0〜1の数値で指定してください。')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError(name + 'は0〜1の有限な数値で指定してください。') from None
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError(name + 'は0〜1の有限な数値で指定してください。')
    return number


def is_assessed(row):
    """Recognize a saved verdict, including deferred judgments from older agents."""
    if row.get('label') in ('keep', 'exclude'):
        return True
    if row.get('label_source') != 'agent' or not str(row.get('label_reason') or '').strip():
        return False
    value = row.get('agent_confidence')
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def snapshot_query_context(query):
    """Only a saved, applied query supplied by the caller can provide context.

    Never read a workbench plan, proposed abstract terms, or arbitrary request
    context. Keep adopted terms and roles, not unsaved suggestions.
    """
    if query is None:
        return None
    if not isinstance(query, dict):
        raise ValueError('採用した検索式の判定用情報を確認してください。')
    purpose = query.get('purpose', '')
    facets = query.get('facets', [])
    if not isinstance(purpose, str) or len(purpose) > 500 or not isinstance(facets, list) or len(facets) > 8:
        raise ValueError('採用した検索式の目的・観点を確認してください。')
    concepts = []
    for facet in facets:
        if not isinstance(facet, dict):
            raise ValueError('採用した検索式の観点を確認してください。')
        name, role, terms = facet.get('name'), facet.get('role'), facet.get('terms')
        if (not isinstance(name, str) or not name.strip() or len(name) > 160
                or role not in ('required', 'exclude')
                or not isinstance(terms, list) or not 1 <= len(terms) <= 24
                or any(not isinstance(term, str) or not term.strip() or len(term) > 160 for term in terms)):
            raise ValueError('採用した検索式の観点名・役割・語句を確認してください。')
        union = facet.get('or_group', '')
        if not isinstance(union, str) or (union and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', union)) or (role == 'exclude' and union):
            raise ValueError('採用した検索式の和集合グループを確認してください。')
        concept = dict(name=name.strip(), role=role, terms=[term.strip() for term in terms])
        if union:
            concept['or_group'] = union
        concepts.append(concept)
    conditions = tree_data(tree_from_data(query['boolean_tree'])) if query.get('boolean_tree') else None
    if not purpose.strip() and not concepts and conditions is None:
        return None
    query_id = query.get('id')
    if not isinstance(query_id, str) or not query_id or len(query_id) > 200:
        raise ValueError('採用した検索式のIDを確認してください。')
    context = dict(query_id=query_id, purpose=purpose.strip(), concepts=concepts,
                   prompt_version=PROMPT_VERSION)
    if conditions is not None:
        context['search_conditions'] = conditions
    return context


def context_fingerprint(context):
    if context is None:
        return None
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode('utf-8')).hexdigest()


def prepare(body, rows, keywords, provider, *, allow_empty=False, adopted_query=None):
    """Validate synchronously before a job or workspace mutation is started."""
    criteria = body.get('criteria', '')
    if not isinstance(criteria, str) or len(criteria) > 5000:
        raise ValueError('LLMの判断基準は5000文字以内の文字列にしてください。')
    criteria = criteria.strip()
    query_context = snapshot_query_context(adopted_query)
    criteria_source = 'explicit' if criteria else 'keywords'
    if not criteria and query_context:
        parts = []
        if query_context['purpose']:
            parts.append('調査目的: ' + query_context['purpose'])
        groups = {}
        for index, concept in enumerate(query_context['concepts']):
            key = ('union', concept['or_group']) if concept.get('or_group') else ('single', index)
            groups.setdefault(key, []).append(concept)
        for alternatives in groups.values():
            if len(alternatives) > 1:
                parts.append('和集合（いずれかの観点）: ' + ' OR '.join(c['name'] for c in alternatives))
            else:
                concept = alternatives[0]
                label = '必須観点' if concept['role'] == 'required' else '除外観点'
                parts.append(label + ': ' + concept['name'])
        if any(len(alternatives) > 1 for alternatives in groups.values()):
            parts.append('和集合の中は代替条件です。全項目の一致を要求せず、別の必須観点との組合せを評価してください。')
        if query_context.get('search_conditions'):
            parts.append('採用済み検索式のsearch_conditionsがAND・OR・NOTの範囲を示します。同じOR内はどれかの条件で足ります。')
        parts.append('採用済み検索式のconceptsに示した語句・観点の意味を判断してください。語の文字列一致だけで要否を決めないでください。')
        criteria = '\n'.join(parts)
        criteria_source = 'adopted_query'
    if not criteria:
        criteria = keywords.strip() if isinstance(keywords, str) else ''
    if not criteria or len(criteria) > 5000:
        raise ValueError('LLMの判断基準、または探索キーワードを入力してください。')
    max_items = body.get('max_items', 100)
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 5000:
        raise ValueError('LLMで判定する上限件数は1〜5000の整数にしてください。')
    threshold = _finite_probability(body.get('threshold', .8), 'LLMの確信度の閾値')
    if threshold < .5:
        raise ValueError('LLMの確信度の閾値は0.5〜1で指定してください。')
    fresh_only = body.get('fresh_only', False)
    if not isinstance(fresh_only, bool):
        raise ValueError('未処理分のみの指定は真偽値にしてください。')
    if not rows and not allow_empty:
        raise ValueError('先にCSVを取り込んで特許地図を作成してください。')
    ids = [row.get('id') for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError('特許データのIDに不足または重複があります。CSVを確認してください。')
    pending = [row for row in rows if not row.get('label') and not (fresh_only and is_assessed(row))]
    # Review fresh patents first, then revisit previously deferred agent judgments.
    pending.sort(key=lambda row: row.get('label_source') == 'agent')
    if not pending and not allow_empty:
        raise ValueError('未判定の特許がありません。人の確定判定はLLMで上書きしません。')
    if pending and provider == 'offline':
        raise ValueError('LLMにおまかせを使うには、設定タブでLLM APIまたはLocal LLMを接続してください。')
    return dict(criteria=criteria, max_items=max_items, threshold=threshold,
                target_ids=[row['id'] for row in pending[:max_items]], available_count=len(pending),
                criteria_source=criteria_source, query_context=query_context,
                context_fingerprint=context_fingerprint(query_context))


def _patent_text(row, *, example=False):
    ipc = row.get('ipc') or []
    if isinstance(ipc, str):
        ipc = [ipc]
    text = dict(title=str(row.get('title') or '')[:250],
                abstract=str(row.get('abstract') or '')[:350 if example else 1000],
                ipc=[code[:80] for code in ipc if isinstance(code, str)][:8],
                fi=str(row.get('fi') or '')[:800], fterm=str(row.get('fterm') or '')[:800])
    if not example:
        text['id'] = row['id']
    return text


def human_examples(rows):
    examples = []
    for label in ('keep', 'exclude'):
        found = [row for row in rows if row.get('label_source') == 'human' and row.get('label') == label]
        for row in found[:EXAMPLES_PER_LABEL]:
            examples.append(dict(**_patent_text(row, example=True), decision=label,
                                 reason=str(row.get('label_reason') or '')[:200]))
    return examples


def response_schema(expected_ids):
    """Constrain local generation; application validation still checks unique IDs."""
    return {
        'type': 'object', 'additionalProperties': False, 'required': ['decisions'],
        'properties': {'decisions': {
            'type': 'array', 'minItems': len(expected_ids), 'maxItems': len(expected_ids),
            'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['id', 'decision', 'confidence', 'relevance', 'reason'],
                'properties': {
                    'id': {'type': 'string', 'enum': list(expected_ids)},
                    'decision': {'type': 'string', 'enum': ['keep', 'exclude', 'unsure']},
                    'confidence': {'type': 'number'},
                    'relevance': {'type': 'number'},
                    'reason': {'type': 'string'},
                },
            },
        }},
    }


def validate_decisions(result, expected_ids, threshold):
    if not isinstance(result, dict) or not isinstance(result.get('decisions'), list):
        raise ValueError('LLMのJSONには decisions 配列が必要です。このバッチは保存せず停止しました。')
    decisions = result['decisions']
    expected = set(expected_ids)
    if len(decisions) != len(expected):
        raise ValueError('LLMが対象特許の一部を省略、または余分な判定を返しました。このバッチは保存せず停止しました。')
    seen, updates = set(), []
    for decision in decisions:
        if not isinstance(decision, dict) or not isinstance(decision.get('id'), str):
            raise ValueError('LLMの判定に有効な特許IDがありません。このバッチは保存せず停止しました。')
        patent_id = decision['id']
        if patent_id not in expected or patent_id in seen:
            raise ValueError('LLMが入力にないIDまたは重複判定を返しました。このバッチは保存せず停止しました。')
        seen.add(patent_id)
        verdict = decision.get('decision')
        if verdict not in ('keep', 'exclude', 'unsure'):
            raise ValueError('LLMの判定は keep / exclude / unsure のいずれかである必要があります。')
        confidence = _finite_probability(decision.get('confidence'), 'LLMの確信度 confidence')
        relevance = _finite_probability(decision.get('relevance'), 'LLMの関連度 relevance')
        reason = decision.get('reason')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError('LLMの判定理由 reason は1000文字以内の非空文字列である必要があります。')
        label = verdict if verdict != 'unsure' and confidence >= threshold else None
        updates.append(dict(id=patent_id, label=label, label_source='agent', label_reason=reason.strip(),
                            agent_confidence=confidence, llm_relevance=relevance,
                            llm_decision=verdict, score=round(relevance, 4) if label else None,
                            score_source='llm' if label else None))
    if seen != expected:
        raise ValueError('LLMが対象特許の一部を省略しました。このバッチは保存せず停止しました。')
    return updates


def run(rows, options, request, publish, cancelled):
    """Publish each fully validated batch; retain prior batches on stop or error.

    request(system, payload) returns the complete JSON object from llm.complete.
    publish(updates, metadata) must atomically protect current human labels and save.
    """
    by_id = {row['id']: row for row in rows}
    targets = [by_id[patent_id] for patent_id in options['target_ids']]
    examples = human_examples(rows)
    metadata = dict(mode='llm', method='LLM判定（手動要否を参考例として参照・モデルの重み学習なし）',
                    metrics=None, train_count=0, judged_count=0, total_count=len(targets), completed_count=0,
                    deferred_count=0, protected_count=0, includes_agent_labels=True, criteria=options['criteria'],
                    threshold=options['threshold'], max_items=options['max_items'],
                    available_count=options['available_count'], example_count=len(examples),
                    human_label_counts=dict(Counter(row['label'] for row in rows
                                                   if row.get('label_source') == 'human' and row.get('label') in ('keep', 'exclude'))),
                    label_counts={'keep': 0, 'exclude': 0}, batch_size=BATCH_SIZE,
                    evaluation_note='重み学習・独立評価は行っていません。関連度と確信度はLLMの推定値です。低確信度とunsureは保留します。',
                    status='running', stage='判定中', progress=0, error=None)
    metadata.update(criteria_source=options.get('criteria_source', 'explicit'),
                    query_context=copy.deepcopy(options.get('query_context')),
                    context_fingerprint=options.get('context_fingerprint'))
    committed_metadata = copy.deepcopy(metadata)

    def emit(updates=(), status=None, stage=None):
        nonlocal committed_metadata
        if status:
            metadata['status'] = status
        metadata['stage'] = stage or {'running': '判定中', 'completed': '判定完了', 'cancelled': '停止', 'error': 'エラー'}[metadata['status']]
        metadata['progress'] = int(100 * metadata['completed_count'] / max(1, metadata['total_count']))
        metadata['message'] = f'{metadata["stage"]} {metadata["completed_count"]}/{metadata["total_count"]}'
        try:
            committed = publish(list(updates), copy.deepcopy(metadata))
        except Exception:
            # Counts are staged until the workspace has persisted the entire batch.
            metadata.clear()
            metadata.update(copy.deepcopy(committed_metadata))
            raise
        if isinstance(committed, dict):
            # The workspace may reject an in-flight batch after a stop request,
            # or preserve a human judgment made since the input snapshot.
            metadata.update(copy.deepcopy(committed))
        committed_metadata = copy.deepcopy(metadata)

    try:
        emit()
        for start in range(0, len(targets), BATCH_SIZE):
            if cancelled():
                emit(status='cancelled')
                return metadata
            batch = targets[start:start+BATCH_SIZE]
            emit()
            expected_ids = [row['id'] for row in batch]
            payload = dict(criteria=options['criteria'], human_examples=examples,
                           patents=[_patent_text(row) for row in batch],
                           required_ids=expected_ids, required_decision_count=len(batch))
            if options.get('query_context'):
                context = options['query_context']
                payload.update(purpose=context['purpose'], concepts=copy.deepcopy(context['concepts']),
                               adopted_query_id=context['query_id'], criteria_source=options['criteria_source'])
                if context.get('search_conditions'):
                    payload['search_conditions'] = copy.deepcopy(context['search_conditions'])
            for attempt in range(2):
                if attempt:
                    emit(stage='判定形式を再確認中')
                if cancelled():
                    emit(status='cancelled')
                    return metadata
                # Only schema validation failures receive this corrective request.
                # Transport errors and exhausted JSON parser retries pass through.
                result = request(SYSTEM + (RETRY_INSTRUCTION if attempt else ''), copy.deepcopy(payload))
                if cancelled():
                    emit(status='cancelled')
                    return metadata
                try:
                    updates = validate_decisions(result, expected_ids, options['threshold'])
                except ValueError:
                    if attempt:
                        raise
                else:
                    break
            metadata['completed_count'] += len(updates)
            for update in updates:
                if update['label']:
                    metadata['judged_count'] += 1
                    metadata['label_counts'][update['label']] += 1
                else:
                    metadata['deferred_count'] += 1
            emit(updates)
        emit(status='cancelled' if cancelled() else 'completed')
        return metadata
    except Exception as exc:
        if cancelled():
            emit(status='cancelled')
            return metadata
        metadata['error'] = str(exc) if isinstance(exc, ValueError) else 'LLMの判定処理に失敗しました。保存済みのバッチは保持しています。'
        emit(status='error')
        raise
