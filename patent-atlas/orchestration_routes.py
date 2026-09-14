"""One shared-job, resumable CSV workflow across manual exploration and the lab."""
import copy
import json
import time
import uuid
from functools import wraps

from fastapi import HTTPException

from discovery_engine import analyze_patents, propose_plan
from discovery_routes import merge_documents, new_discovery
from orchestration_engine import (PROGRESS, checked_options, fingerprint, fresh_rows,
                                  new_orchestration, recover, refresh_steps, summary)
from workspace_import import merge_patents
from convergence_routes import record_result


def register_orchestration_routes(app, context):
    state, settings, lock = context['STATE'], context['SETTINGS'], context['LOCK']
    idle = context['idle']
    def save():
        return context['save']()
    stop = context['STOP']
    state['orchestration'] = recover(state.get('orchestration'))

    def workspace():
        if not isinstance(state.get('orchestration'), dict):
            state['orchestration'] = new_orchestration()
        return state['orchestration']

    def atomic_request(action):
        @wraps(action)
        def wrapped(*args, **kwargs):
            with lock:
                previous = copy.deepcopy(state)
                previous_job = copy.deepcopy(context['JOB'])
                try:
                    return action(*args, **kwargs)
                except Exception as exc:
                    state.clear()
                    state.update(previous)
                    context['JOB'].clear()
                    context['JOB'].update(previous_job)
                    if not isinstance(exc, (ValueError, HTTPException)):
                        raise ValueError('工程の保存に失敗しました。変更前の状態を保持しています。保存先を確認してください。') from None
                    raise
        return wrapped

    def commit(operation):
        with lock:
            before = copy.deepcopy(state)
            try:
                result = operation()
                value = workspace()
                value['summary'].update(summary(state['patents']))
                refresh_steps(value)
                save()
                return result
            except Exception:
                state.clear()
                state.update(before)
                raise

    def log(value, message):
        value['logs'] = (value.get('logs', []) +
                         [dict(time=time.strftime('%H:%M:%S'), message=message)])[-150:]

    def transition(stage, message, completed=None):
        value = workspace()
        if completed and completed not in value['completed_stages']:
            value['completed_stages'].append(completed)
        value.update(stage=stage, progress=PROGRESS[stage], message=message, checkpoint=None)
        context['progress'](value['progress'], message)
        log(value, message)

    def progress_for(start, end):
        def update(percent, message):
            with lock:
                value = workspace()
                value.update(progress=round(start + max(0, min(100, percent)) / 100 * (end - start), 1),
                             message=message)
                value['summary'].update(summary(state['patents']))
                context['progress'](value['progress'], message)
        return update

    def same_lab_topic(lab):
        return (lab.get('plan') or {}).get('keywords', '').strip().casefold() == state['keywords'].strip().casefold()

    def all_input_rows():
        lab = state.get('discovery') or {}
        if same_lab_topic(lab):
            rows, _ = merge_patents(state['patents'], lab.get('patents', []))
            return rows
        return copy.deepcopy(state['patents'])

    def sync_lab():
        lab = state.get('discovery') or new_discovery()
        if not same_lab_topic(lab):
            archives = list(lab.get('archives', []))
            if lab.get('plan'):
                archive_id = uuid.uuid4().hex
                target = context['DATA'] / f'discovery-archive-{archive_id}.json'
                target.write_text(json.dumps({k: v for k, v in lab.items() if k != 'archives'},
                                             ensure_ascii=False, allow_nan=False), encoding='utf-8')
                archives.append(dict(id=archive_id, keywords=lab['plan']['keywords'], time=time.strftime('%Y-%m-%d %H:%M')))
            lab = new_discovery()
            lab.update(archives=archives, plan=propose_plan(state['keywords'], state['candidates'], state['patents']))
            state['discovery'] = lab
        rows, _ = merge_documents(lab.get('patents', []), state['patents'], '統括モード', live_labels=True)
        analysis = analyze_patents(lab['plan'], rows, lab.get('analysis'))
        lab.update(patents=rows, analysis=analysis, status='done', mode='csv', last_error=None)
        lab['rounds'] = (lab.get('rounds', []) + [dict(round=len(lab.get('rounds', [])) + 1,
            source='統括モード', queries=[], total_documents=len(rows),
            **{field: analysis['counts'].get(field, 0) for field in ('new_documents', 'new_families', 'new_classifications')})])[-100:]
        return lab

    def evidence_candidates():
        feedback = context['classification_feedback'](state['patents'], state['selected'])
        lab = state['discovery']
        pool = context['explorer'].merge_candidates(state['candidates'],
                (lab.get('plan') or {}).get('candidate_hypotheses', []) +
                (lab.get('analysis') or {}).get('recommendations', []) + feedback['candidates'])
        context['set_classification_view'](pool, direction='seed',
            note='統括モードで公報・探索ラボ・判定結果を統合した候補です。分類の根拠を確認してください。',
            total=len(pool), next_offset=None, trail=[])
        selectable = [row for row in state['candidates'] if context['explorer'].enrich(row).get('selectable') is not False]
        selectable_keys = {row['kind'] + ':' + row['code'] for row in selectable}
        recommended = [key for key in feedback['recommended_keys'] if key in selectable_keys]
        selected = list(dict.fromkeys(state['selected'] + recommended[:30]))
        value = workspace()
        value['summary'].update(classification_count=len(selectable), warnings=feedback.get('warnings', []))
        return selectable, selected

    def preview_query(selected, include=None, exclude=None):
        before = copy.deepcopy(state)
        try:
            return copy.deepcopy(context['make_query'](refine=True, classification_keys=selected,
                include=include, exclude=exclude, preserve_conditions=True))
        finally:
            state.clear()
            state.update(before)

    def checkpoint(kind, candidates, selected, query=None):
        value = workspace()
        value.update(status='review', stage=kind, progress=PROGRESS[kind],
                     message='分類候補を確認してください。' if kind == 'classifications' else '次の検索式を確認してください。',
                     checkpoint=dict(type=kind, candidates=copy.deepcopy(candidates), selected_keys=list(selected),
                                     selection_baseline=list(selected),
                                     include_terms=(query or {}).get('include_terms', []),
                                     exclude_terms=(query or {}).get('exclude_terms', []),
                                     term_options=(query or {}).get('terms', dict(include=[], exclude=[])),
                                     expression=(query or {}).get('expression', '')))
        log(value, value['message'])

    def finish_query(selected, include=None, exclude=None):
        result = context['make_query'](refine=True, classification_keys=selected,
                                     include=include, exclude=exclude, preserve_conditions=True)
        result['orchestration_id'] = workspace()['id']
        value = workspace()
        value['summary'].update(query_id=result['id'], expression=result['expression'])
        value['dataset_fingerprint'] = fingerprint(all_input_rows())
        value['search_observation_id'] = (state.get('search_result') or {}).get('id')
        record_result(state, {}, automatic=True)
        transition('waiting_csv', '次の検索式を保存しました。検索結果のCSVを追加して再開してください。', 'query')
        value.update(status='waiting_csv')

    def run_cycle(run_id):
        try:
            while not stop.is_set():
                with lock:
                    value = workspace()
                    if value['id'] != run_id:
                        raise ValueError('統括モードの実行が変更されました。')
                    stage, options = value['stage'], copy.deepcopy(value['options'])
                if stage == 'discover':
                    def discover():
                        rows = all_input_rows()
                        if not rows:
                            raise ValueError('対話で探索または同じテーマの探索ラボへCSVを取り込んでください。')
                        # Imported lab patents join the same map and the same judgment owner.
                        state['patents'] = rows
                        clusters = context['map_patents'](rows, state['keywords'])
                        state.update(patents=rows, clusters=clusters)
                        sync_lab()
                        candidates, selected = evidence_candidates()
                        workspace()['dataset_fingerprint'] = fingerprint(all_input_rows())
                        transition('classifications', '分類探索が完了しました。', 'discover')
                        if options['review']:
                            checkpoint('classifications', candidates, selected)
                        else:
                            state['selected'] = context['normalize_selection'](selected)
                            transition('assess', '未判定の特許を確認します。', 'classifications')
                    commit(discover)
                    if workspace()['status'] == 'review':
                        return
                elif stage == 'assess':
                    with lock:
                        pending = fresh_rows(state['patents'])
                    if options['judge'] and pending:
                        context['run_llm_assessment'](dict(criteria=options['criteria'], max_items=options['max_items'],
                            threshold=options['threshold'], fresh_only=True), progress_callback=progress_for(20, 70))
                    if stop.is_set():
                        break
                    commit(lambda: transition('train',
                        '特許判定を保存しました。' if options['judge'] and pending else
                        '保存済みの判定を再利用します。' if options['judge'] else 'LLM判定をスキップしました。', 'assess'))
                elif stage == 'train':
                    if options['learning'] != 'none':
                        context['run_model_training'](options['learning'], include_agent=options['include_agent'],
                                                      progress_callback=progress_for(70, 85))
                    if stop.is_set():
                        break
                    commit(lambda: transition('refine', '分類・検索式を更新します。', 'train'))
                elif stage == 'refine':
                    def refine():
                        sync_lab()
                        candidates, selected = evidence_candidates()
                        query = preview_query(selected)
                        transition('query', '公報の判定・学習結果から次の検索式を作成しました。', 'refine')
                        if options['review']:
                            checkpoint('query', candidates, selected, query)
                        else:
                            finish_query(selected)
                    commit(refine)
                    return
                elif stage == 'query':
                    commit(lambda: finish_query(workspace()['approved_query']['selected_keys'],
                        workspace()['approved_query']['include_terms'], workspace()['approved_query']['exclude_terms']))
                    return
                else:
                    raise ValueError('再開する工程を確認してください。')
            commit(lambda: workspace().update(status='paused', message='保存済みの結果を保持して停止しました。'))
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, HTTPException)) else '処理に失敗しました。設定・入力データを確認して再開してください。'
            commit(lambda: workspace().update(status='paused' if stop.is_set() else 'error', error=message, message=message))
            raise

    def launch():
        value = workspace()
        run_id = value['id']
        value.update(status='running', error=None)
        refresh_steps(value)
        save()
        return context['begin_job'](lambda: run_cycle(run_id), kind='orchestration', mode='cycle')

    @app.post('/api/orchestration/start')
    @atomic_request
    def start(body: dict):
        with lock:
            idle()
            value = workspace()
            if value['status'] in ('review', 'paused', 'error'):
                raise ValueError('進行中の統括モードがあります。確認または再開してください。')
            options = checked_options(body, state.get('keywords', ''))
            if not state.get('keywords', '').strip():
                raise ValueError('対話で探索にキーワードを入力してください。')
            rows = all_input_rows()
            if not rows:
                raise ValueError('先にCSVを取り込んでください。')
            if options['judge'] and fresh_rows(rows) and settings['provider'] == 'offline':
                raise ValueError('LLMを接続するか、統括モードのLLM判定をオフにしてください。')
            state['orchestration'] = new_orchestration(options, cycle=value.get('cycle', 0) + 1 if value['status'] != 'idle' else 1)
            state['orchestration'].update(dataset_fingerprint=fingerprint(rows), keywords=state['keywords'])
            return launch()

    @app.post('/api/orchestration/resume')
    @atomic_request
    def resume(body: dict):
        with lock:
            idle()
            value = workspace()
            if value['status'] not in ('review', 'paused', 'error', 'waiting_csv'):
                raise ValueError('再開する統括モードがありません。')
            if value['keywords'] != state['keywords']:
                raise ValueError('探索テーマが変更されています。元のキーワードに戻して再開してください。')
            training_edits = {field: body[field] for field in ('learning', 'include_agent') if field in body}
            if training_edits:
                if value['status'] not in ('paused', 'error') or value['stage'] != 'train':
                    raise ValueError('学習方式は学習工程の停止中に変更してください。')
                value['options'] = checked_options({**value['options'], **training_edits}, value['keywords'])
            current_fingerprint = fingerprint(all_input_rows())
            if value['status'] == 'waiting_csv':
                observation = state.get('search_result') or {}
                new_result = (observation.get('id') and observation.get('keywords') == state['keywords']
                              and observation['id'] != value.get('search_observation_id'))
                if current_fingerprint == value['dataset_fingerprint'] and not new_result:
                    raise ValueError('次の検索結果のCSVを追加してから再開してください。同じ公報群での自動反復は行いません。')
                previous = value
                value = new_orchestration(copy.deepcopy(previous['options']), cycle=previous['cycle'] + 1)
                value.update(keywords=state['keywords'], dataset_fingerprint=current_fingerprint,
                             logs=previous['logs'][-100:])
                state['orchestration'] = value
            elif current_fingerprint != value['dataset_fingerprint']:
                raise ValueError('実行途中の公報データが変更されています。追加CSVは「次のCSV待ち」で取り込んでください。')
            if value['status'] == 'review':
                review = value['checkpoint']
                defaults = list(dict.fromkeys(state['selected'] + review['selected_keys']))
                selected = context['normalize_selection'](body.get('selected_keys', defaults))
                # The checkpoint form is a stable editing draft. A selection made
                # in another tab after its creation must not disappear merely
                # because that stale form does not submit the newly selected key.
                baseline = set(review.get('selection_baseline', review['selected_keys']))
                added_elsewhere = [key for key in state['selected'] if key not in baseline]
                preserved = [key for key in added_elsewhere if key not in selected]
                selected = context['normalize_selection'](list(dict.fromkeys(selected + added_elsewhere)))
                if preserved:
                    log(value, '確認待ちの間に他タブで追加した分類を保持しました: ' + '、'.join(preserved))
                if review['type'] == 'classifications':
                    state['selected'] = selected
                    transition('assess', '分類を確認しました。特許判定へ進みます。', 'classifications')
                else:
                    include = body.get('include_terms', review['include_terms'])
                    exclude = body.get('exclude_terms', review['exclude_terms'])
                    preview_query(selected, include, exclude)  # Validate before accepting edits.
                    value = workspace()  # Preview restores a detached state snapshot.
                    value['approved_query'] = dict(selected_keys=selected, include_terms=include, exclude_terms=exclude)
                    value['checkpoint'] = None
            return launch()

    @app.post('/api/orchestration/stop')
    def stop_orchestration():
        with lock:
            value = workspace()
            if context['JOB'].get('status') == 'running' and context['JOB'].get('kind') == 'orchestration':
                stop.set()
                context['JOB']['stop_requested'] = True
                value['message'] = '停止を要求しました。処理中の応答が戻ると停止します。'
            elif value['status'] == 'review':
                value['message'] = '確認待ちです。編集内容を確認して再開できます。'
            save()
        return context['public_state']()

    @app.post('/api/orchestration/reset')
    def reset_orchestration():
        with lock:
            idle()
            def reset():
                previous = workspace()
                value = new_orchestration()
                value['logs'] = previous.get('logs', [])[-100:]
                log(value, '工程をリセットしました。公報・判定・分類選択・検索式は保持しています。')
                state['orchestration'] = value
            commit(reset)
        return context['public_state']()
