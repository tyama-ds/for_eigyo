"""Pure state and validation helpers for the CSV orchestration cycle."""
import hashlib
import json
import math
import time
import uuid

from llm_judgment import is_assessed


STEPS = [('discover', '分類探索'), ('classifications', '分類の確認'),
         ('assess', '特許の判定'), ('train', '学習'), ('refine', '分類・検索式更新'),
         ('query', '次の式を確認'), ('waiting_csv', '次のCSV待ち')]
PROGRESS = dict(discover=0, classifications=15, assess=20, train=70,
                refine=85, query=95, waiting_csv=100)


def fingerprint(patents):
    """Content identity excludes mutable decisions, scores and visualization positions."""
    fields = ('id', 'title', 'abstract', 'ipc', 'fi', 'fterm', 'applicant', 'year', 'family', 'family_id')
    rows = [{field: row.get(field) for field in fields} for row in patents]
    rows.sort(key=lambda row: str(row.get('id')))
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def checked_options(body, keywords):
    if not isinstance(body, dict):
        raise ValueError('統括モードの設定を確認してください。')
    criteria = body.get('criteria', '')
    if not isinstance(criteria, str) or len(criteria) > 5000:
        raise ValueError('判断基準は5000文字以内で入力してください。')
    criteria = criteria.strip() or keywords.strip()
    if not criteria:
        raise ValueError('探索キーワードまたは判断基準を入力してください。')
    learning = body.get('learning', 'none')
    if learning not in ('none', 'lightweight', 'transformer'):
        raise ValueError('学習モードを確認してください。')
    options = dict(criteria=criteria, learning=learning)
    for name, default in [('judge', True), ('include_agent', False), ('review', True)]:
        value = body.get(name, default)
        if not isinstance(value, bool):
            raise ValueError('統括モードの切り替えは真偽値で指定してください。')
        options[name] = value
    limit = body.get('max_items', 5000)
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError('判定上限は1〜5000件で指定してください。')
    threshold = body.get('threshold', .8)
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold) or not .5 <= threshold <= 1):
        raise ValueError('確信度の閾値は0.5〜1で指定してください。')
    options.update(max_items=limit, threshold=threshold)
    return options


def summary(patents):
    return dict(documents=len(patents), keep=sum(row.get('label') == 'keep' for row in patents),
                exclude=sum(row.get('label') == 'exclude' for row in patents),
                deferred=sum(not row.get('label') and is_assessed(row) for row in patents),
                unprocessed=sum(not is_assessed(row) for row in patents),
                human=sum(row.get('label_source') == 'human' for row in patents))


def fresh_rows(patents):
    return [row for row in patents if not is_assessed(row)]


def new_orchestration(options=None, *, cycle=1):
    return dict(id=uuid.uuid4().hex, status='idle', stage='discover', progress=0,
                message='', options=options or {}, checkpoint=None, steps=[], logs=[],
                summary={}, error=None, cycle=cycle, completed_stages=[],
                dataset_fingerprint=None, keywords=None, created_at=time.time())


def refresh_steps(value):
    done = set(value.get('completed_stages', []))
    value['steps'] = [dict(id=stage, label=label,
        status=('done' if stage in done else
                'review' if stage == value['stage'] and value['status'] == 'review' else
                'active' if stage == value['stage'] else 'pending')) for stage, label in STEPS]


def recover(value):
    """A process restart never resumes a network request automatically."""
    if not isinstance(value, dict):
        value = new_orchestration()
    if value.get('status') == 'running':
        value.update(status='paused', error=None,
                     message='アプリの終了で中断しました。保存済みの判定を保持し、再開できます。')
    refresh_steps(value)
    return value
