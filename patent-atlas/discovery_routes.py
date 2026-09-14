"""An evidence workspace for discovery; manual selections and labels stay owned by their UI."""
import copy
import json
import math
import re
import time
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import Response

from analysis_engine import parse_csv
from discovery_engine import propose_plan, analyze_patents, validate_llm_plan
from patent_search import search_ops


def new_discovery():
    return dict(id=uuid.uuid4().hex, plan=None, analysis=None, patents=[], rounds=[], logs=[],
                status='idle', mode='csv', last_error=None, archives=[])


def public_discovery(value):
    if not isinstance(value, dict):
        return None
    return {**{k: v for k, v in value.items() if k != 'patents'}, 'document_count': len(value.get('patents', []))}


def _document_key(row):
    return re.sub(r'[^A-Z0-9]', '', str(row['id']).upper()) or 'raw:' + str(row['id'])


def merge_documents(existing, incoming, source, live_labels=False):
    """Accumulate publications, and refresh human decisions only from their owner."""
    rows = {_document_key(row): copy.deepcopy(row) for row in existing}
    before = set(rows)
    for incoming_row in incoming:
        row = copy.deepcopy(incoming_row)
        key = _document_key(row)
        old = rows.get(key, {})
        merged = {**old, **{k: v for k, v in row.items() if v not in ('', None)}}
        merged['id'] = old.get('id') or row['id']
        if old.get('label_source') == 'human' and not live_labels:
            for field in ('label', 'label_source', 'label_reason'):
                merged[field] = old.get(field)
        if live_labels:
            for field in ('label', 'label_source', 'label_reason'):
                merged[field] = row.get(field)
        merged.setdefault('source', source)
        merged['synthetic'] = bool(old.get('synthetic') or row.get('synthetic') or str(row['id']).startswith('DEMO-'))
        origins = list(old.get('discovery_sources', []))
        if source not in origins:
            origins.append(source)
        merged['discovery_sources'] = origins[-20:]
        rows[key] = merged
    if len(rows) > 5000:
        raise ValueError('探索ラボの蓄積上限は5,000公報です。結果を書き出し、新しい計画を作成してください。')
    return list(rows.values()), len(set(rows) - before)


def register_discovery_routes(app, context):
    state, settings, lock = context['STATE'], context['SETTINGS'], context['LOCK']
    save, idle, public_state = context['save'], context['idle'], context['public_state']
    stop_event = context['STOP']

    def workspace():
        if not isinstance(state.get('discovery'), dict):
            state['discovery'] = new_discovery()
        return state['discovery']

    def require_plan():
        value = workspace()
        if not value.get('plan'):
            raise ValueError('先に探索計画を作成してください。')
        return value

    def log(value, message):
        value['logs'] = (value.get('logs', []) + [dict(time=time.strftime('%H:%M:%S'), message=message)])[-100:]

    def evidence_queries(analysis, used):
        priority = {'classification': 0, 'vocabulary': 1}
        queries = {q['id']: q for q in (analysis or {}).get('next_queries', []) if q['id'] not in used}
        return sorted(queries.values(), key=lambda q: priority.get(q.get('facet_id'), 2))

    def summarize(value, rows, source, queries=None):
        previous = value.get('analysis')
        result = analyze_patents(value['plan'], rows, previous)
        counts = result['counts']
        value.update(patents=rows, analysis=result, status='done', last_error=None)
        value['rounds'] = (value.get('rounds', []) + [dict(
            round=len(value.get('rounds', [])) + 1, source=source, queries=queries or [],
            new_documents=counts.get('new_documents', 0), new_families=counts.get('new_families', 0),
            new_classifications=counts.get('new_classifications', 0), total_documents=len(rows))])[-100:]
        log(value, f'{source}: {len(rows)}公報を蓄積。新規分類 {counts.get("new_classifications", 0)}件。')
        return result

    @app.post('/api/discovery/plan')
    def create_plan(body: dict):
        topic = context['checked_text'](body.get('keywords', ''), 2000)
        if not topic:
            raise ValueError('探索する技術を入力してください。')
        english = context['checked_text'](body.get('english_terms', ''), 1000)
        # Validate user vocabulary before an optional paid/network model call.
        additions = [part.strip() for part in re.split(r'[,;\n、]+', english) if part.strip()]
        if len(additions) > 8 or any(len(part) > 120 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 \-]*', part) for part in additions):
            raise ValueError('英語の起点語は英数字・空白・ハイフンの語句を、改行またはカンマで8個以内に指定してください。')
        use_llm = body.get('use_llm', False)
        if not isinstance(use_llm, bool):
            raise ValueError('LLM利用の指定を確認してください。')
        with lock:
            idle()
            old_id = workspace()['id']
            same_topic = topic.casefold() == state['keywords'].strip().casefold()
            candidates = copy.deepcopy(state['candidates']) if same_topic else []
            patents = copy.deepcopy(state['patents'])
            connection = settings.copy()
        llm_plan = None
        if use_llm:
            llm_plan = context['complete'](connection,
                '技術調査の探索観点を装置、材料、プロセス、界面、性能、用途に分ける。'
                '架空の特許・分類コード・出典は作らない。日本語の説明と短い英語の検索語句を提案する。'
                '観点idは device, material, process, interface, performance, application から選ぶ。'
                '英語主題語は最大8個、各観点の terms は最大12個、english_terms は最大4個とする。'
                '形式 {"english_terms":["topic synonym"],"facets":[{"id":"process","terms":["製造"],"english_terms":["manufacturing"]}]}。',
                {'keywords': topic, 'english_terms': english})
            validate_llm_plan(llm_plan)
        plan = propose_plan(topic, candidates, patents, llm_plan=llm_plan, manual_english_terms=additions)
        plan['user_english_terms'] = english
        with lock:
            idle()
            old = workspace()
            if old['id'] != old_id:
                raise HTTPException(409, '別の計画が先に保存されました。最新の計画を確認してください。')
            archives = list(old.get('archives', []))
            if old.get('plan'):
                archive_id = uuid.uuid4().hex
                target = context['DATA'] / f'discovery-archive-{archive_id}.json'
                archived = {k: v for k, v in old.items() if k != 'archives'}
                target.write_text(json.dumps(archived, ensure_ascii=False, allow_nan=False), encoding='utf-8')
                archives.append(dict(id=archive_id, keywords=old['plan']['keywords'], time=time.strftime('%Y-%m-%d %H:%M')))
            value = new_discovery()
            value.update(plan=plan, status='planned', archives=archives)
            log(value, '探索計画を作成。辞書の分類は仮説です。CSVの公報データで確認します。')
            if use_llm:
                for notice in plan['llm_proposal']['notices'][:2]:
                    log(value, notice)
            state['discovery'] = value
            save()
        return public_state()

    @app.post('/api/discovery/analyze')
    def analyze_workspace():
        with lock:
            idle()
            value = require_plan()
            if not state['patents']:
                raise ValueError('手動探索にCSVを読み込むか、探索ラボへCSVを追加してください。')
            rows, _ = merge_documents(value['patents'], state['patents'], '手動探索のCSV', live_labels=True)
            summarize(value, rows, '手動探索のCSV')
            value['mode'] = 'csv'
            save()
        return public_state()

    @app.post('/api/discovery/import')
    async def import_csv(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 20 * 1024 * 1024:
                raise ValueError('CSVは20MB以下にしてください。')
        parsed = parse_csv(bytes(raw))
        if parsed['needs_mapping']:
            raise ValueError('発明の名称列が見つかりません。列名を「発明の名称」または「title」にするか、手動探索で列を割り当てて読み込んでください。')
        with lock:
            idle()
            value = require_plan()
            rows, added = merge_documents(value['patents'], parsed['rows'], '探索ラボへ追加したCSV')
            summarize(value, rows, 'CSV追加')
            value['mode'] = 'csv'
            log(value, f'CSVから新規{added}公報を追加。ファイル内の重複{parsed["duplicates"]}件・名称なし{parsed["skipped"]}行を除外。')
            save()
        return public_state()

    @app.post('/api/discovery/adopt')
    def adopt(body: dict):
        keys = body.get('keys', [])
        if not isinstance(keys, list) or not keys or len(keys) > 100 or any(not isinstance(key, str) for key in keys):
            raise ValueError('手動探索へ送る分類を1〜100件選んでください。')
        with lock:
            idle()
            value = require_plan()
            pool = {c['key']: c for c in value['plan']['candidate_hypotheses']}
            pool.update({c['key']: c for c in (value.get('analysis') or {}).get('recommendations', [])})
            if any(key not in pool for key in keys):
                raise ValueError('候補にない分類が指定されています。最新の分析から選んでください。')
            items = []
            for key in dict.fromkeys(keys):
                item = copy.deepcopy(pool[key])
                evidence = item.get('evidence', [])
                ids = [entry['patent_id'] for entry in evidence[:8]]
                label = '探索ラボ: ' + value['plan']['keywords'][:100]
                origin = dict(type='discovery', label=label, discovery_id=value['id'],
                              evidence_status=item.get('evidence_status', 'hypothesis'), patent_ids=ids)
                item.setdefault('origins', []).append(origin)
                item['reason'] = ('公報の分類欄から抽出。適合性は要確認。根拠: ' + ', '.join(ids)) if ids else '探索ラボの辞書仮説。関連特許で裏付けてから選んでください。'
                items.append(context['explorer'].enrich(item))
            context['set_classification_view'](items, direction='seed', note='探索ラボから追加した候補です。検索に使う分類を選んでください。', total=len(items), next_offset=None, trail=[])
            log(value, f'{len(items)}分類を手動探索へ追加しました。検索に使う分類の選択は利用者が行います。')
            save()
        return public_state()

    @app.get('/api/discovery/export')
    def export_discovery(archive_id: str = ''):
        with lock:
            value = workspace()
            if archive_id:
                if not re.fullmatch(r'[0-9a-f]{32}', archive_id) or not any(a['id'] == archive_id for a in value.get('archives', [])):
                    raise HTTPException(404, '保存した計画が見つかりません。')
                target = context['DATA'] / f'discovery-archive-{archive_id}.json'
                if not target.is_file():
                    raise HTTPException(404, '保存した計画ファイルが見つかりません。')
                text = target.read_text(encoding='utf-8')
            else:
                text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        return Response(text, media_type='application/json', headers={'Content-Disposition': 'attachment; filename="patent-discovery.json"'})

    @app.post('/api/discovery/run')
    def run_discovery(body: dict):
        limits = {}
        for name, default, high in [('max_rounds', 2, 3), ('max_queries', 6, 12), ('per_query', 10, 25)]:
            number = body.get(name, default)
            if type(number) is not int or not 1 <= number <= high:
                raise ValueError(f'{name} は1〜{high}の整数にしてください。')
            limits[name] = number
        with lock:
            idle()
            value = require_plan()
            if not settings.get('ops_key') or not settings.get('ops_secret'):
                raise ValueError('EPO OPSの認証情報が未設定です。CSVでの検証は接続設定なしで利用できます。')
            if not value['plan'].get('english_terms'):
                raise ValueError('英語の起点語を指定して探索計画を作り直してください。')
            value_id, connection = value['id'], settings.copy()

        def action():
            with lock:
                value = workspace()
                if value['id'] != value_id:
                    raise ValueError('探索計画が変更されました。実行し直してください。')
                value.update(status='running', mode='epo_ops', last_error=None)
                original = list(value['plan']['queued_queries'])
                used = {q['id'] for r in value['rounds'] for q in r.get('queries', [])}
                initial = [q for q in original if q['id'] not in used]
                refinement = evidence_queries(value.get('analysis'), used)
                log(value, 'EPO OPS再帰検索を開始。上限または停止要求で終了します。')
                save()
            performed = 0
            pending_attempts = []
            try:
                for _ in range(limits['max_rounds']):
                    if stop_event.is_set() or performed >= limits['max_queries']:
                        break
                    per_round = max(1, math.ceil(limits['max_queries'] / limits['max_rounds']))
                    batch = []
                    # Keep an unsearched perspective in every round, then try evidence-led queries.
                    sources = [initial, refinement, initial, refinement]
                    while len(batch) < min(per_round, limits['max_queries'] - performed):
                        found = None
                        for queue in sources:
                            while queue and queue[0]['id'] in used:
                                queue.pop(0)
                            if queue:
                                found = queue.pop(0)
                                sources = sources[1:] + sources[:1]
                                break
                        if found is None:
                            break
                        batch.append(found)
                        used.add(found['id'])
                    if not batch:
                        log(value, '未実行の検索候補がなくなったため終了しました。網羅性の保証ではありません。')
                        break
                    attempts = []
                    pending_attempts = attempts
                    for query in batch:
                        if stop_event.is_set():
                            break
                        context['progress'](int(90 * performed / limits['max_queries']), f'探索ラボ: {performed + 1}/{limits["max_queries"]} 検索')
                        found = search_ops(query['query'], connection, limits['per_query'])
                        performed += 1
                        with lock:
                            rows, added = merge_documents(value['patents'], found['patents'], 'EPO OPS: ' + query['query'])
                            value['patents'] = rows
                            attempts.append({**query, 'result_count': len(found['patents']), 'new_documents': added,
                                             'total': found.get('total'), 'truncated': found.get('truncated', False),
                                             'request_count': found.get('request_count'), 'unavailable_count': found.get('unavailable_count', 0),
                                             'total_is_lower_bound': found.get('total_is_lower_bound', False)})
                            log(value, f'{query["label"]}: {len(found["patents"])}公報取得・新規{added}公報。')
                            save()
                        if stop_event.wait(1.2):
                            break
                    if attempts:
                        with lock:
                            result = summarize(value, value['patents'], 'EPO OPS', attempts)
                            pending_attempts = []
                            value['status'] = 'running'
                            refinement = evidence_queries(result, used)
                            save()
                        if not result['counts'].get('new_documents') and not result['counts'].get('new_classifications') and not initial:
                            log(value, '今回の探索では新しい公報・分類が増えなかったため終了しました。')
                            break
                with lock:
                    value['status'] = 'stopped' if stop_event.is_set() else 'done'
                    log(value, '停止要求で終了しました。' if stop_event.is_set() else f'今回の検索を終了しました（{performed}クエリ）。結果を確認して次の探索へ進めます。')
                    save()
            except Exception as error:
                with lock:
                    if pending_attempts:
                        # A later request failure must not discard successful-query evidence.
                        summarize(value, value['patents'], 'EPO OPS（一部取得）', pending_attempts)
                    value.update(status='error', last_error=str(error) if isinstance(error, ValueError) else '検索に失敗しました。接続設定と取得元の応答を確認してください。')
                    log(value, value['last_error'])
                    save()
                raise
        return context['begin_job'](action)
