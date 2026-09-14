"""Extract IPC metadata and documented FI/theme-derived IPC candidates separately."""
from __future__ import annotations

import copy
import math
import re

import classification_explorer as explorer
import classification_bridge


_BUCKETS = ('keep', 'exclude', 'predicted_keep', 'predicted_exclude')


def _decision(row):
    if row.get('label') in ('keep', 'exclude'):
        return row['label']
    score = row.get('score')
    if row.get('label') or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        return None
    if 0.8 <= score <= 1:
        return 'predicted_keep'
    if 0 <= score <= 0.2:
        return 'predicted_exclude'
    return None


def _priority(row):
    if row.get('label') in ('keep', 'exclude'):
        return {'human': 3, 'agent': 2}.get(row.get('label_source'), 1)
    return 0


def classification_feedback(patents, active_keys=()):
    """Count publications and known families separately; predictions never become labels.

    Recommendations require more labelled keep families than exclude families.
    A shared IPC is never emitted as a NOT condition, even if exclusions dominate.
    Unknown symbols and malformed fields are reported instead of repaired.
    """
    rows, warnings = {}, []
    duplicate_publications = 0
    for index, row in enumerate(patents):
        publication_id = str(row.get('id') or f'行{index + 1}')
        identity = re.sub(r'[^A-Z0-9]', '', publication_id.upper()) or publication_id
        if identity in rows:
            duplicate_publications += 1
            if _priority(row) <= _priority(rows[identity][1]):
                continue
        rows[identity] = (publication_id, row)
    if duplicate_publications:
        warnings.append(f'重複公報{duplicate_publications}件を支持数から除きました。同じ公報の判定は人の判断を優先します。')

    grouped, missing_families = {}, 0
    for identity, (publication_id, row) in rows.items():
        bucket = _decision(row)
        if bucket is None:
            continue
        extracted = classification_bridge.extract(row)
        warnings.extend(extracted['warnings'])
        classifications = [item for item in extracted['items'] if item['kind'] == 'IPC' and item['verified'] and item['selectable']]
        family = str(row.get('family') or row.get('family_id') or '').strip()
        family_key = 'family:' + family if family else 'publication:' + identity
        if classifications and not family:
            missing_families += 1
        for classification in classifications:
            key = classification['key']
            if key not in grouped:
                item = copy.deepcopy(classification)
                item.update(evidence=[], classification_origins=[], explicit_metadata_ids=[], derived_candidate_ids=[],
                            **{name + '_ids': [] for name in _BUCKETS})
                grouped[key] = (item, {name: set() for name in _BUCKETS})
            item, families = grouped[key]
            provenance = classification['classification_origins']
            if any(origin['type'] == 'patent' for origin in provenance):
                item['explicit_metadata_ids'].append(publication_id)
            if any(origin['type'] in ('fi_to_ipc', 'fterm_theme_to_ipc') for origin in provenance):
                item['derived_candidate_ids'].append(publication_id)
            for origin in provenance:
                if origin not in item['classification_origins']:
                    item['classification_origins'].append(copy.deepcopy(origin))
            item[bucket + '_ids'].append(publication_id)
            families[bucket].add(family_key)
            item['evidence'].append(dict(patent_id=publication_id, decision=bucket,
                                         label_source=row.get('label_source'),
                                         score=row.get('score') if bucket.startswith('predicted_') else None,
                                         evidence_status=classification['evidence_status'],
                                         classification_origins=copy.deepcopy(provenance),
                                         family_id=family, family_key=family_key))

    candidates = []
    for item, families in grouped.values():
        for bucket in _BUCKETS:
            item[bucket + '_count'] = len(item[bucket + '_ids'])
            item[bucket + '_family_count'] = len(families[bucket])
        item['family_count'] = len(set().union(*families.values()))
        item['publication_count'] = len(item['evidence'])
        item['duplicate_family_count'] = item['publication_count'] - item['family_count']
        item['explicit_metadata_count'] = len(item['explicit_metadata_ids'])
        item['derived_candidate_count'] = len(item['derived_candidate_ids'])
        item['evidence_status'] = 'patent_metadata' if item['explicit_metadata_count'] else 'derived_candidate'
        item['human_keep_count'] = sum(e['decision'] == 'keep' and e.get('label_source') == 'human' for e in item['evidence'])
        item['agent_keep_count'] = sum(e['decision'] == 'keep' and e.get('label_source') == 'agent' for e in item['evidence'])
        item['origins'] = copy.deepcopy(item['classification_origins'])
        item['recommended'] = item['keep_family_count'] > item['exclude_family_count']
        reasons = [f'必要 {item["keep_count"]}公報 / 不要 {item["exclude_count"]}公報',
                   f'ファミリー等では必要 {item["keep_family_count"]} / 不要 {item["exclude_family_count"]}']
        reasons.append(f'公報IPC欄 {item["explicit_metadata_count"]} / FI・テーマ対応候補 {item["derived_candidate_count"]}公報（重複あり）')
        if item['derived_candidate_count']:
            reasons.append('FI・Fターム由来は対応範囲を使った探索候補で、公報に付与されたIPCではありません。FIとIPCの版差・範囲の広がりを確認してください')
        if item['agent_keep_count']:
            reasons.append(f'必要判定の内訳: 人 {item["human_keep_count"]} / AI {item["agent_keep_count"]}公報。AI判定は人の確認と区別します')
        if item['predicted_keep_count'] or item['predicted_exclude_count']:
            reasons.append(f'未判定の学習予測: 必要寄り {item["predicted_keep_count"]} / 不要寄り {item["predicted_exclude_count"]}公報。確定判定とは別集計')
        if item['keep_count'] and item['exclude_count']:
            reasons.append('必要・不要の両方にある分類のため、分類をNOT条件にしません')
        if item['recommended']:
            reasons.append('確定した必要判定のファミリー支持が多いため、追加候補として推奨')
        elif not item['keep_count'] and item['predicted_keep_count']:
            reasons.append('予測だけでは推奨を確定しません。公報の要否を確認してください')
        else:
            reasons.append('必要側の支持が優勢ではないため、自動推奨はしません')
        if item['duplicate_family_count']:
            reasons.append(f'同じファミリー等の重複 {item["duplicate_family_count"]}公報を推奨判定では重ねて数えません')
        item['reason'] = '。'.join(reasons) + '。'
        candidates.append(item)
    candidates.sort(key=lambda item: (not item['recommended'], -item['keep_family_count'],
                                     -item['predicted_keep_family_count'], item['exclude_family_count'], item['key']))
    if missing_families:
        warnings.append(f'{missing_families}公報にファミリーIDがなく、公報ごとに集計しました。同族関係は推測していません。')
    return dict(candidates=candidates, active_keys=list(dict.fromkeys(active_keys)),
                recommended_keys=[item['key'] for item in candidates if item['recommended']],
                note='公報IPC欄と、FI・Fタームの公式テーマ範囲から得たIPC候補を由来別に集計。対応候補は付与IPCではありません。人・AIの判定と学習予測を区別し、ファミリー等の重複を考慮します。選んだ分類をOR条件に使い、分類のNOTは作りません。',
                warnings=list(dict.fromkeys(warnings)))


def feedback_origin(candidate):
    """A detached evidence snapshot stored with each explicitly adopted IPC."""
    fields = ('key', 'code', 'kind', 'keep_count', 'exclude_count', 'predicted_keep_count',
              'predicted_exclude_count', 'keep_family_count', 'exclude_family_count',
              'predicted_keep_family_count', 'predicted_exclude_family_count', 'family_count',
              'publication_count', 'duplicate_family_count', 'keep_ids', 'exclude_ids',
              'predicted_keep_ids', 'predicted_exclude_ids', 'evidence', 'reason', 'recommended',
              'evidence_status', 'classification_origins', 'explicit_metadata_count', 'derived_candidate_count',
              'explicit_metadata_ids', 'derived_candidate_ids', 'human_keep_count', 'agent_keep_count')
    return dict(type='refinement', label='要否判定・学習予測と分類由来のIPC候補',
                **copy.deepcopy({field: candidate[field] for field in fields if field in candidate}))
