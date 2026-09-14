"""Read-only search-result convergence snapshots; no training loss is invented.

Each point is a reviewed search result set. IPC subclass composition is an
explicit, reproducible topic proxy, not semantic analysis of the patent text.
"""
from __future__ import annotations

import copy
import math
import re
import unicodedata
from collections import Counter

import classification_catalog
from classification_explorer import parse_codes
from llm_judgment import is_assessed


THRESHOLDS = dict(new_keep_rate=.05, topic_shift_rate=.05, hit_change_rate=.05)
REQUIRED_STREAK = 3
_PUBLIC = ('schema_version', 'id', 'keywords', 'imported_at', 'scope', 'uploaded_count',
           'total_hits', 'result_complete', 'keep_count', 'exclude_count', 'deferred_count',
           'unprocessed_count', 'human_count', 'keep_with_ipc_count', 'ipc_coverage_rate',
           'new_keep_count', 'new_keep_rate', 'new_ipc_count', 'new_ipc_rate',
           'topic_shift_rate', 'hit_change_rate', 'missing_identifier_count',
           'duplicate_count', 'query', 'comparable', 'stable_transition',
           'comparability_reasons', 'observation_id', 'ordinal', 'created_at', 'updated_at',
           'query_id', 'expression')


def publication_key(value):
    """Use the same publication identity despite spaces, hyphens or full width."""
    text = unicodedata.normalize('NFKC', str(value or '')).strip().upper()
    return re.sub(r'[^A-Z0-9]', '', text) or text


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _rate(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 1 and math.isfinite(value))


def _priority(row):
    if row.get('label') in ('keep', 'exclude'):
        source = row.get('label_source')
        return 4 if source == 'human' else 3 if source == 'agent' else 2
    return int(is_assessed(row))


def _rows(rows):
    unique, duplicates, missing = {}, 0, 0
    for index, row in enumerate(rows if isinstance(rows, (list, tuple)) else []):
        if not isinstance(row, dict):
            missing += 1
            continue
        key = publication_key(row.get('id'))
        if not key:
            key = f'missing-publication-id:{index}'
            missing += 1
        if key in unique:
            duplicates += 1
            if _priority(row) <= _priority(unique[key]):
                continue
        unique[key] = row
    return unique, duplicates, missing


def _subclasses(row):
    raw = row.get('ipc') or ''
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        raw = '; '.join(raw)
    try:
        codes = parse_codes('IPC', raw)
    except (TypeError, ValueError):
        return set()
    # A historical subgroup can still supply its explicitly written subclass;
    # this neither declares the subgroup current nor infers a replacement.
    return {code[:4] for code in codes if len(code) >= 4
            and classification_catalog.lookup('IPC', code[:4]) is not None}


def _counts(rows):
    return dict(keep_count=sum(row.get('label') == 'keep' for row in rows),
                exclude_count=sum(row.get('label') == 'exclude' for row in rows),
                deferred_count=sum(row.get('label') not in ('keep', 'exclude')
                                   and is_assessed(row) for row in rows),
                unprocessed_count=sum(not is_assessed(row) for row in rows),
                human_count=sum(row.get('label_source') == 'human'
                                and row.get('label') in ('keep', 'exclude') for row in rows))


def _history(records, *, skip_id=None, keywords=None):
    result = {}
    for record in records if isinstance(records, (list, tuple)) else []:
        if not isinstance(record, dict) or not isinstance(record.get('id'), str) or not record['id']:
            continue
        if record['id'] == skip_id:
            continue
        if keywords is not None and str(record.get('keywords') or '').strip() != keywords:
            continue
        # Repeated updates of a result set are one observation, never extra epochs.
        if record['id'] in result:
            del result[record['id']]
        result[record['id']] = record
    return list(result.values())


def _keys(record, field):
    value = record.get(field)
    return set(value) if isinstance(value, list) and all(isinstance(x, str) for x in value) else None


def _distribution(record):
    raw = record.get('ipc_distribution')
    if not isinstance(raw, dict) or not raw:
        return None
    if not all(isinstance(key, str) and _rate(value) for key, value in raw.items()):
        return None
    total = sum(raw.values())
    if not math.isclose(total, 1, abs_tol=1e-6):
        return None
    return raw


def _evidence_reasons(record, prefix):
    reasons = []
    count_fields = ('uploaded_count', 'keep_count', 'exclude_count', 'deferred_count',
                    'unprocessed_count', 'keep_with_ipc_count', 'missing_identifier_count')
    if type(record.get('schema_version')) is not int or record['schema_version'] != 1 or not all(_integer(record.get(k)) for k in count_fields):
        return [prefix + 'の記録形式を確認できません。新しい検索結果から記録してください。']
    if (sum(record[key] for key in ('keep_count', 'exclude_count', 'deferred_count', 'unprocessed_count')) != record['uploaded_count']
            or record['keep_with_ipc_count'] > record['keep_count']):
        return [prefix + 'の判定件数の整合性を確認できません。新しい検索結果から記録してください。']
    if record.get('scope') != 'csv':
        reasons.append(prefix + 'は作業領域の基準点です。検索式に対応するCSV結果が必要です。')
    if not _integer(record.get('total_hits')):
        reasons.append(prefix + 'の検索サービス側の総ヒット件数が未入力です。')
    elif record.get('total_hits') != record['uploaded_count'] or record.get('result_complete') is not True:
        reasons.append(prefix + 'は検索結果の一部です。全件のCSVが必要です。')
    if record['keep_count'] == 0:
        reasons.append(prefix + 'に必要判定の特許がありません。')
    elif record['keep_with_ipc_count'] < record['keep_count'] or not _distribution(record):
        reasons.append(prefix + 'の必要特許にIPCサブクラスの情報が不足しています。')
    if record['deferred_count'] or record['unprocessed_count']:
        reasons.append(prefix + 'に保留または未処理の特許が残っています。')
    if record['missing_identifier_count']:
        reasons.append(prefix + 'に公報番号がない特許があります。')
    return reasons


def _comparison(record, previous):
    reasons = _evidence_reasons(record, '今回')
    if previous is None:
        reasons.append('比較には次の検索結果が必要です。最初の点では変化率を計算しません。')
    else:
        reasons.extend(_evidence_reasons(previous, '前回'))
        if not all(_rate(record.get(key)) for key in THRESHOLDS):
            reasons.append('比較可能な新規必要率・IPC構成変化・ヒット件数変化がそろっていません。')
        if not _integer(record.get('new_ipc_count')):
            reasons.append('新しいIPCの数を比較できません。')
    comparable = not reasons
    stable = (comparable and all(record[key] <= threshold for key, threshold in THRESHOLDS.items())
              and record['new_ipc_count'] == 0)
    return comparable, stable, list(dict.fromkeys(reasons))


def build_record(rows, observation, previous_records, *, total_hits=None, query=None):
    """Create one detached observed-result snapshot from its current verdicts.

    A missing or invalid total remains unknown; CSV size never substitutes for
    the external database's total. Counts and completeness use unique publications.
    """
    observation = observation if isinstance(observation, dict) else {}
    unique, duplicates, missing = _rows(rows)
    keywords = str(observation.get('keywords') or '').strip()
    identifier = str(observation.get('id') or '')
    previous_records = _history(previous_records, skip_id=identifier, keywords=keywords)
    previous = previous_records[-1] if previous_records else None
    valid_total = total_hits if _integer(total_hits) and total_hits >= len(unique) else None
    record = dict(schema_version=1, id=identifier, keywords=keywords,
                  imported_at=str(observation.get('imported_at') or ''),
                  scope=observation.get('scope') if observation.get('scope') in ('csv', 'workspace_baseline') else 'workspace_baseline',
                  uploaded_count=len(unique), total_hits=valid_total, duplicate_count=duplicates,
                  missing_identifier_count=missing, **_counts(list(unique.values())))
    record['result_complete'] = record['scope'] == 'csv' and valid_total is not None and valid_total == len(unique)
    record['query'] = {key: copy.deepcopy(query[key]) for key in ('id', 'format', 'platform', 'created_at')
                       if isinstance(query, dict) and isinstance(query.get(key), (str, int))}
    keep = {key: row for key, row in unique.items() if row.get('label') == 'keep'}
    distribution, classified = Counter(), 0
    for row in keep.values():
        subclasses = _subclasses(row)
        if subclasses:
            classified += 1
            for subclass in subclasses:
                distribution[subclass] += 1 / len(subclasses)
    record.update(publication_keys=sorted(unique), keep_publication_keys=sorted(keep),
                  ipc_subclasses=sorted(distribution), keep_with_ipc_count=classified,
                  ipc_coverage_rate=classified / len(keep) if keep else None,
                  ipc_distribution={key: value / classified for key, value in sorted(distribution.items())} if classified else {})
    record.update(new_keep_count=None, new_keep_rate=None, new_ipc_count=None,
                  new_ipc_rate=None, topic_shift_rate=None, hit_change_rate=None)
    if previous is not None:
        past_keeps = [_keys(item, 'keep_publication_keys') for item in previous_records]
        if all(keys is not None for keys in past_keeps):
            new_keep = set(keep) - set().union(*past_keeps)
            record.update(new_keep_count=len(new_keep), new_keep_rate=len(new_keep) / len(keep) if keep else None)
        past_classes = [_keys(item, 'ipc_subclasses') for item in previous_records]
        if all(keys is not None for keys in past_classes):
            new_classes = set(distribution) - set().union(*past_classes)
            record.update(new_ipc_count=len(new_classes),
                          new_ipc_rate=len(new_classes) / len(distribution) if distribution else None)
        prior_distribution = _distribution(previous)
        current_distribution = _distribution(record)
        if prior_distribution and current_distribution:
            symbols = set(prior_distribution) | set(current_distribution)
            record['topic_shift_rate'] = min(1.0, sum(abs(prior_distribution.get(key, 0) - current_distribution.get(key, 0))
                                                       for key in symbols) / 2)
        prior_total = previous.get('total_hits')
        if valid_total is not None and _integer(prior_total):
            record['hit_change_rate'] = abs(valid_total - prior_total) / max(valid_total, prior_total, 1)
    record['comparable'], record['stable_transition'], record['comparability_reasons'] = _comparison(record, previous)
    return record


def summarize(records, current_rows, *, keywords=''):
    """Expose chart data and conservative completion gates without raw patent IDs."""
    history = _history(records, keywords=str(keywords or '').strip())
    public, streak = [], 0
    previous = None
    for number, record in enumerate(history, 1):
        comparable, stable, reasons = _comparison(record, previous)
        streak = streak + 1 if stable else 0
        item = {key: copy.deepcopy(record[key]) for key in _PUBLIC if key in record}
        item.update(number=number, comparable=comparable, stable_transition=stable,
                    comparability_reasons=reasons)
        # Unknown legacy numeric values must not reach JSON as NaN or Infinity.
        for key in ('new_keep_rate', 'new_ipc_rate', 'ipc_coverage_rate', 'topic_shift_rate', 'hit_change_rate'):
            if not _rate(item.get(key)):
                item[key] = None
        public.append(item)
        previous = record
    unique, _, missing = _rows(current_rows)
    current_counts = _counts(list(unique.values()))
    pending = dict(deferred=current_counts['deferred_count'], unprocessed=current_counts['unprocessed_count'])
    blockers = []
    if not history:
        blockers.append('まだ検索結果の記録がありません。CSVを取り込み、要否を確認してください。')
    elif public[-1]['comparability_reasons']:
        blockers.extend(public[-1]['comparability_reasons'])
    if streak < REQUIRED_STREAK:
        blockers.append(f'安定した比較が連続{REQUIRED_STREAK}回必要です（現在{streak}回）。')
    if pending['deferred']:
        blockers.append(f'作業領域の保留{pending["deferred"]}件を人が確認してください。')
    if pending['unprocessed']:
        blockers.append(f'作業領域に未処理{pending["unprocessed"]}件があります。')
    if missing:
        blockers.append('作業領域に公報番号がない特許があります。')
    eligible = bool(history and streak >= REQUIRED_STREAK and not blockers)
    status = 'stable' if eligible else 'insufficient' if len(history) < 2 or not public[-1]['comparable'] else 'exploring'
    return dict(records=public, status=status, stable_streak=streak, required_streak=REQUIRED_STREAK,
                eligible_to_complete=eligible, blockers=list(dict.fromkeys(blockers)),
                thresholds=copy.deepcopy(THRESHOLDS), pending=pending,
                note='検索結果の変化を示すグラフです。学習損失ではありません。トピックの変化は必要特許のIPCサブクラス構成による目安です。'
                     '新規必要率・IPC構成変化・総ヒット件数変化が各5%以下、新しいIPCなしの比較が3回連続し、保留・未処理がなくなると完了候補になります。'
                     '検索漏れがないことを保証する判定ではありません。')
