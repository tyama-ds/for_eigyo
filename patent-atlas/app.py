"""Patent Atlas — single-user, local Python application."""
import copy
import csv
import io
import json
import math
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from analysis_engine import ALIASES, demo_rows, map_patents, parse_csv, refinement_terms, train
from catalog import CATALOG, keywords, suggest
from llm import complete, validate_timeout
from prompt_templates import CLASSIFICATION_SYSTEM, SELECTION_SYSTEM
import llm_judgment
from query_formats import formats as query_formats, export_query
from refinement_classifications import classification_feedback, feedback_origin
import classification_explorer as explorer
import classification_translations
import classification_revision_notes
from discovery_routes import new_discovery, public_discovery, register_discovery_routes
from orchestration_routes import register_orchestration_routes
from convergence_routes import (display_convergence, display_search_result, new_observation,
                                register_convergence_routes)
from workspace_import import merge_patents
from research_routes import register_research_routes, refined_tree
from query_formats import Node, group, tree_data, tree_from_data, describe_tree, terms_of
from research_strategy import suggest_keyword_or_group

ROOT = Path(__file__).parent
DATA = Path(os.environ.get('PATENT_ATLAS_DATA', str(ROOT / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
classification_translations.configure_cache(DATA)
LOCK = threading.RLock()
DEFAULT_SETTINGS = dict(provider='offline', base_url='http://localhost:11434/v1', model='qwen3:8b', api_key='', proxy='', ca_bundle='', bypass_local=True,
                        transformer_model='', allow_model_download=False, classification_layout='semantic', ops_key='', ops_secret='', llm_timeout=120)
SECRET_SETTINGS = ('api_key', 'proxy', 'ops_key', 'ops_secret')
STATE = dict(keywords='', candidates=[], selected=[], patents=[], clusters=[], queries=[], training=None, agent_log=[], demo=False, import_info=None, classification_view=None, candidate_proposal=None, discovery=None)
SETTINGS = DEFAULT_SETTINGS.copy()
try:
    saved = json.loads((DATA / 'workspace.json').read_text(encoding='utf-8'))
    STATE.update(saved.get('state', {}))
    SETTINGS.update(saved.get('settings', {}))
except (FileNotFoundError, ValueError):
    pass
if SETTINGS['classification_layout'] not in ('semantic', 'grid'):
    SETTINGS['classification_layout'] = DEFAULT_SETTINGS['classification_layout']
try:
    SETTINGS['llm_timeout'] = validate_timeout(SETTINGS.get('llm_timeout', 120))
except ValueError:
    SETTINGS['llm_timeout'] = DEFAULT_SETTINGS['llm_timeout']
if not isinstance(STATE.get('discovery'), dict):
    STATE['discovery'] = new_discovery()
elif STATE['discovery'].get('status') == 'running':
    STATE['discovery'].update(status='stopped', last_error='前回の探索はアプリの終了で中断しました。保存済みの結果から再開できます。')
if (STATE.get('training') or {}).get('mode') == 'llm' and STATE['training'].get('status') == 'running':
    STATE['training'].update(status='cancelled', stage='中断', message='前回のLLM判定はアプリの終了で中断しました。保存済みの判定を保持しています。')
JOB = dict(id=None, status='idle', progress=0, message='', error=None, kind=None, mode=None, started_at=None, completed_at=None, stop_requested=False)
STOP = threading.Event()
app = FastAPI(title='Patent Atlas', docs_url='/api/docs')

@app.middleware('http')
async def local_security(request: Request, call_next):
    host = (request.url.hostname or '').lower()
    if host not in ('localhost', '127.0.0.1', '::1', 'testserver'):
        return JSONResponse({'detail': 'ローカル接続のみ利用できます。'}, status_code=403)
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': '異なるオリジンからの変更は許可されません。'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.exception_handler(ValueError)
async def value_error(request, error):
    return JSONResponse({'detail': str(error)}, status_code=400)

def save():
    settings = {k: v for k, v in SETTINGS.items() if k not in SECRET_SETTINGS}
    target = DATA / 'workspace.json'
    temp = DATA / 'workspace.tmp'
    temp.write_text(json.dumps(dict(state=STATE, settings=settings), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(target)

def idle():
    if JOB['status'] == 'running':
        raise HTTPException(409, '処理中です。完了を待つか停止してください。')

def classification_key(candidate):
    return candidate['kind']+':'+candidate['code']

def normalize_selection(values, candidates=None):
    candidates=STATE['candidates'] if candidates is None else candidates
    if not isinstance(values,list) or any(not isinstance(v,str) for v in values):
        raise ValueError('分類の選択はキーのリストにしてください。')
    out=[]
    for value in values:
        found=[c for c in candidates if classification_key(c)==value]
        if not found:
            found=[c for c in candidates if c['code']==value]
        if len(found)!=1:
            raise ValueError('分類を体系付きで選び直してください: '+value)
        key=classification_key(found[0])
        if explorer.enrich(found[0]).get('selectable') is False:
            raise ValueError('この分類は案内専用、または上位コードが未確認です。辞書で確認できるIPCか、F-termのタームを選んでください: '+value)
        if key not in out:
            out.append(key)
    return out

def display_labels(item, *, selection=False):
    """Project current captions and optional selection rules, preserving saved data."""
    if not isinstance(item, dict) or item.get('kind') not in ('IPC', 'F-term', 'CPC') or not item.get('code'):
        return item
    try:
        enriched = explorer.enrich(item)
    except (ValueError, TypeError):
        return item
    return {**item, **{name: value for name, value in enriched.items()
                      if name.startswith(('title_ja', 'title_en', 'title_short_'))},
            **({name: enriched[name] for name in ('selectable', 'selection_scope')} if selection else {}),
            **({'matched_classifications': [display_labels(row) for row in item['matched_classifications']]}
               if isinstance(item.get('matched_classifications'), list) else {})}


def display_discovery(discovery):
    value = copy.deepcopy(public_discovery(discovery))
    if value.get('plan'):
        value['plan']['candidate_hypotheses'] = [display_labels(row, selection=True) for row in value['plan'].get('candidate_hypotheses', [])]
    if value.get('analysis'):
        value['analysis']['recommendations'] = [display_labels(row, selection=True) for row in value['analysis'].get('recommendations', [])]
    return value


def public_state():
    with LOCK:
        return copy.deepcopy({**{key: value for key, value in STATE.items() if key != 'convergence_tracking'},
                              'convergence':display_convergence(STATE), 'search_result':display_search_result(STATE),
                              'selected':normalize_selection(STATE['selected']),
                              'candidate_proposal':display_candidate_proposal(STATE.get('candidate_proposal')),
                              'discovery':display_discovery(STATE['discovery']),
                              'candidates':[{**display_labels(c, selection=True),'key':classification_key(c)} for c in STATE['candidates']],
                              'classification_catalog':{'ipc':explorer.ipc_catalog.metadata(),'fterm':explorer.fterm_catalog.metadata()},
                              'settings': {**{k: v for k, v in SETTINGS.items() if k not in SECRET_SETTINGS},
                                           'api_key_set': bool(SETTINGS['api_key']), 'proxy_set': bool(SETTINGS['proxy']),
                                           'ops_key_set': bool(SETTINGS['ops_key']), 'ops_secret_set': bool(SETTINGS['ops_secret'])}, 'job': JOB})

def progress(value, message):
    with LOCK:
        JOB.update(progress=value, message=message)

def begin_job(action, *, kind=None, mode=None):
    with LOCK:
        idle()
        STOP.clear()
        JOB.update(id=uuid.uuid4().hex, status='running', progress=0, message='準備中', error=None,
                   kind=kind, mode=mode, started_at=time.time(), completed_at=None, stop_requested=False)
    def worker():
        try:
            action()
            with LOCK:
                # Publish terminal job status only after the workspace commit.
                save()
                if STOP.is_set():
                    JOB.update(status='cancelled', message='停止しました')
                else:
                    JOB.update(status='done', progress=100, message='完了しました')
                JOB['completed_at'] = time.time()
        except Exception as exc:
            with LOCK:
                try:
                    save()
                except Exception:
                    exc = ValueError('保存に失敗しました。保存先の空き容量と書き込み権限を確認してください。')
                JOB.update(status='cancelled' if STOP.is_set() else 'error', error=str(exc) if isinstance(exc, (ValueError, HTTPException)) else '処理に失敗しました。設定・入力データ・依存パッケージを確認してください。', message='処理を終了しました')
                JOB['completed_at'] = time.time()
    threading.Thread(target=worker, daemon=True).start()
    return {'job_id': JOB['id']}

def checked_text(value, limit=3000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'入力は{limit}文字以内の文字列にしてください。')
    return value.strip()

def llm_classification_text(kind, code):
    """Strip only unambiguous display decoration, keeping incomplete symbols."""
    aliases = {'IPC': 'IPC', 'F-TERM': 'F-term', 'FTERM': 'F-term',
               'F_TERM': 'F-term', 'Fターム': 'F-term'}
    kind = aliases.get(unicodedata.normalize('NFKC', kind).strip().upper())
    if kind is None:
        raise ValueError('分類体系は IPC または F-term を指定してください。')
    text = unicodedata.normalize('NFKC', code).strip()
    prefix = re.match(r'^(IPC|CPC|F-TERM|FTERM|F_TERM|Fターム)\s*:\s*(.*)$', text, re.I)
    if prefix:
        if aliases.get(prefix[1].upper()) != kind:
            raise ValueError('kind とコードに付いた分類体系が一致しません。')
        text = prefix[2]
    text = re.sub(r'\s*\(\d{4}\.(?:0[1-9]|1[0-2])\)\s*$', '', text)
    return kind, text


def normalize_llm_classification(kind, code):
    """Remove unambiguous display decoration; never repair or infer a code."""
    kind, text = llm_classification_text(kind, code)
    try:
        return kind, explorer.normalize(kind, text)
    except ValueError:
        raise ValueError('コード形式が不完全、または余分な文字が含まれています。1件に1つの完全なコードが必要です。') from None


def rejected_candidate_guidance(item):
    """Offer a separately reviewed broader scope, never validate a rejected code."""
    result = copy.deepcopy(item)
    try:
        kind, text = llm_classification_text(item.get('kind', ''), item.get('code', ''))
    except (ValueError, TypeError, AttributeError):
        return result
    if kind != 'IPC':
        return result
    text = re.sub(r'\s+', '', text).upper()
    # Never discard FI suffixes, multiple codes, arbitrary prose or wildcards.
    match = re.fullmatch(r'([A-H]\d{2}[A-Z])(\d{1,4})(?:/(\d{1,6}))?', text)
    if not match:
        return result
    if explorer.ipc_catalog.lookup('IPC', text):
        return result
    revision = classification_revision_notes.lookup(text)
    if revision:
        result['revision'] = revision
        result['reason'] = revision['explanation']
    scopes = []
    # A complete, missing subgroup can be explored from its existing main group.
    # A slash-less symbol is not silently completed with /00.
    if match[3] is not None:
        scopes.append(match[1] + str(int(match[2])) + '/00')
    scopes.append(match[1])
    for code in scopes:
        known = explorer.lookup('IPC', code)
        if not known or code == text:
            continue
        result['broader_candidate'] = explorer.enrich({**known,
            'reason': f'{text} の代替と確定した分類ではなく、コードの先頭部分から確認できる広い探索起点です。正式な定義と調査テーマへの適合性を確認してください。',
            'recommendation_method': 'broader_review'})
        break
    return result


def display_candidate_proposal(proposal):
    if not isinstance(proposal, dict):
        return proposal
    result = copy.deepcopy(proposal)
    result['rejected'] = [rejected_candidate_guidance({**item, 'index': item.get('index', index)})
                          for index, item in enumerate(result.get('rejected', []), 1)]
    # Older saved reports repeat every rejection in notices. The cards now carry
    # the explanation, while successful/zero-result notices remain unchanged.
    if result['rejected']:
        result['notices'] = [note for note in result.get('notices', [])
                             if not note.startswith(('LLM提案', 'LLMが提案した '))]
        result['notices'].insert(0, f'LLM提案{result.get("returned_count", 0)}件のうち{len(result["rejected"])}件を除外しました。詳細と確認できる上位分類を上に表示しています。')
    return result


def llm_candidate_rows(result, notices, proposal=None):
    """Validate model data before touching the workspace or enriching codes."""
    if not isinstance(result, dict) or not isinstance(result.get('candidates'), list):
        raise ValueError('LLMのJSONには candidates 配列が必要です。候補がない場合は {"candidates":[]} を返す必要があります。')
    items = result['candidates']
    if proposal is not None:
        proposal.update(returned_count=len(items), rejected=[], duplicate_count=0)
    if len(items) > 12:
        raise ValueError('LLMの分類候補が多すぎます。最大12件の短い候補一覧で再実行してください。')
    rows, rejected, seen, normalizations = [], [], set(), []

    def reject(index, item, reason):
        item = item if isinstance(item, dict) else {}
        def display(value):
            return value[:80] if isinstance(value, str) else '（文字列ではありません）'
        rejected.append(rejected_candidate_guidance(dict(index=index, kind=display(item.get('kind', '')),
                             code=display(item.get('code', '')), reason=reason)))

    for index, item in enumerate(items, 1):
        if (not isinstance(item, dict) or not isinstance(item.get('kind'), str) or len(item['kind']) > 32 or
                not isinstance(item.get('code'), str) or len(item['code']) > 64 or
                not isinstance(item.get('reason', ''), str)):
            reject(index, item, '各候補には kind（IPC / F-term）と文字列の code・reason が必要です。')
            continue
        try:
            kind, code = normalize_llm_classification(item['kind'], item['code'])
        except ValueError as error:
            reject(index, item, str(error))
            continue
        if (kind, code) in seen:
            if proposal is not None:
                proposal['duplicate_count'] += 1
            continue
        seen.add((kind, code))
        known = explorer.lookup(kind, code)
        if kind == 'IPC' and not known:
            reject(index, {'kind': kind, 'code': code}, '収録IPC辞書（2026.01）で確認できません。旧版・誤記の可能性があります。')
            continue
        if kind != item['kind'] or code != item['code']:
            normalizations.append(dict(index=index, kind=kind, code=code,
                                       input_kind=item['kind'], input_code=item['code']))
        # Model-written titles are not definitions. Use official captions when
        # available, otherwise show the code with explicit unverified status.
        row = explorer.enrich(dict(kind=kind, code=code, title=code, group='LLM候補',
            reason='LLMの探索仮説（適合性は未確認）：' + item.get('reason', '').strip()[:300],
            terms=[], score=0, recommendation_method='llm'))
        rows.append(row)
    if proposal is not None:
        proposal.update(accepted_count=len(rows), rejected=rejected, rejected_count=len(rejected),
                        normalizations=normalizations)
    if rejected:
        details = [f'{item["kind"]} {item["code"] or "（コードなし）"}: {item["reason"]}' for item in rejected]
        notice = f'LLM提案{len(items)}件のうち{len(rejected)}件を除外しました。' + ' / '.join(details)
        if not rows:
            raise ValueError('LLMの提案に反映できる分類がありません。' + notice +
                             ' 現行の完全なコード、または B60L のような上位分類で再生成してください。前回の候補・選択は保持しています。')
        notices.append(notice)
    if normalizations:
        notices.append('体系名・空白・全半角・接頭ラベル・版注記などの表記を統一して照合しました。' +
                       ' / '.join(f'{item["input_kind"]} {item["input_code"]} → {item["kind"]} {item["code"]}' for item in normalizations))
    if not items:
        notices.append('LLMからの分類候補は0件でした。辞書からの探索仮説を表示します。')
    if rows:
        notices.append('LLMによる分類名は採用せず、収録済みの公式名称を表示します。主題への適合性は定義を確認してください。')
    return rows


def candidate_list(text, use_llm=False, *, notices=None, proposal=None, connection=None, research_context=None):
    candidates = suggest(text)
    seed_keys = {classification_key(row) for row in candidates}
    llm_rows = []
    if use_llm:
        payload = {'keywords': text}
        if research_context:
            payload['research_context'] = copy.deepcopy(research_context)
        result = complete(connection if connection is not None else SETTINGS, CLASSIFICATION_SYSTEM, payload)
        llm_rows = llm_candidate_rows(result, notices if notices is not None else [], proposal)
        # A repeated code is still an LLM recommendation: preserve its reason
        # even when a keyword seed already supplied the same classification.
        candidates = explorer.merge_candidates(candidates, llm_rows)
    initial = [explorer.enrich(c) for c in candidates]
    # Imported at call time: discovery_engine uses explorer for official-code
    # checks, but generating manual candidates never reads/writes the lab state.
    from discovery_engine import propose_plan
    plan = propose_plan(text, initial, [])
    facets = {facet['id']: facet for facet in plan['facets']}
    rows = explorer.merge_candidates(initial, plan['candidate_hypotheses'])
    llm_by_key = {classification_key(row): row for row in llm_rows}
    for row in rows:
        row.update(evidence_status='hypothesis', relevance_status='review_pending')
        row.setdefault('facets', [])
        related_terms = list(row.get('terms', []))
        for facet_id in row['facets']:
            facet = facets.get(facet_id, {})
            related_terms.extend(facet.get('terms', []) + facet.get('english_terms', []))
        row['terms'] = list(dict.fromkeys(related_terms))[:48]
        row['recommendation_sources'] = [dict(type='keyword_facets', keywords=text,
            kind=row['kind'], code=row['code'], facets=list(row['facets']),
            reason=row.get('reason', ''), source=row.get('source', ''))]
        proposed = llm_by_key.get(classification_key(row))
        if proposed:
            if classification_key(row) not in seed_keys and row.get('reason', '').startswith('既存候補を起点に保存'):
                row['recommendation_sources'] = []
            row.update(reason=proposed['reason'], llm_reason=proposed['reason'],
                       llm_proposed=True, recommendation_method='llm')
            row['recommendation_sources'].append(dict(type='llm', keywords=text,
                kind=row['kind'], code=row['code'], reason=proposed['reason']))
    result = [explorer.enrich(row) for row in rows]
    if proposal is not None:
        by_key = {classification_key(row): row for row in result}
        proposal['items'] = [copy.deepcopy(by_key[key]) for key in llm_by_key]
    return result


def set_classification_view(items, **metadata):
    selected=normalize_selection(STATE['selected'])
    additional=items+([metadata['focus']] if metadata.get('focus') else [])
    merged=explorer.merge_candidates(STATE['candidates'],additional)
    STATE.update(candidates=merged,selected=selected,classification_view={'keys':[classification_key(c) for c in items],**metadata})


def classification_overview(items=None):
    items=STATE['candidates'] if items is None else items
    coarse=explorer.overview(items,STATE['keywords'])
    origins=[c for c in STATE['candidates'] if c.get('origins')]
    rows=explorer.merge_candidates(coarse,origins)
    proposal = STATE.get('candidate_proposal') or {}
    if proposal.get('llm_used') and proposal.get('keywords') == STATE['keywords']:
        rows = explorer.merge_candidates(rows, proposal.get('items', []))
    note = ('材料・製造・界面などの観点から、広い分類を表示しています。候補は探索仮説です。気になる分類の下位へ進んで適合性を確認できます。'
            if STATE['keywords'].strip() else 'キーワードが空欄のため、現在の候補と推薦した分類の上位群を表示しています。キーワードを入力すると他分野の候補も探せます。')
    set_classification_view(rows, direction='overview',note=note,total=len(rows),next_offset=None,trail=[])

def previous_query_for_context(ignore_classifications=False):
    if not STATE['queries']:
        return {}
    previous=STATE['queries'][-1]
    if (previous.get('keyword_context') != STATE['keywords'] if previous.get('boolean_tree') else previous.get('keywords') != keywords(STATE['keywords'])):
        return {}
    if not ignore_classifications and {classification_key(c) for c in previous.get('classifications',[])} != set(normalize_selection(STATE['selected'])):
        return {}
    return previous

def make_query(refine=False, include=None, exclude=None, preserve_conditions=False, classification_keys=None):
    words = keywords(STATE['keywords'])
    original_keys=normalize_selection(STATE['selected'])
    selected_keys=original_keys
    pool=STATE['candidates']
    classification_changes=None
    if classification_keys is not None:
        if not refine or not isinstance(classification_keys,list) or len(classification_keys)>1500 or any(not isinstance(key,str) or ':' not in key for key in classification_keys):
            raise ValueError('改善案の分類は体系付きキーのリストで指定してください。')
        feedback=classification_feedback(STATE['patents'],original_keys)
        evidence_pool={item['key']:item for item in feedback['candidates']}
        allowed={classification_key(item) for item in pool} | set(evidence_pool)
        if any(key not in allowed for key in classification_keys):
            raise ValueError('分類候補にないキーが含まれています。要否判定から再分析してください。')
        incoming=[]
        evidence=[]
        for key in dict.fromkeys(classification_keys):
            if key in evidence_pool:
                item=copy.deepcopy(evidence_pool[key])
                origin=feedback_origin(item)
                item.setdefault('origins',[]).append(origin)
                incoming.append(item)
                evidence.append(origin)
        pool=explorer.merge_candidates(pool,incoming)
        selected_keys=normalize_selection(classification_keys,pool)
        classification_changes=dict(added_keys=[key for key in selected_keys if key not in original_keys],
                                    removed_keys=[key for key in original_keys if key not in selected_keys],
                                    selected_keys=list(selected_keys), evidence=evidence,
                                    note=feedback['note'])
        preserve_conditions=True
    selected = copy.deepcopy([c for c in pool if classification_key(c) in selected_keys])
    if not words and not selected and not (refine and STATE['queries'] and STATE['queries'][-1].get('boolean_tree')):
        raise ValueError('キーワードを入力するか、検索に使う分類を選んでください。')
    def quote(s):
        return '"' + s.replace('"', ' ').replace('\n', ' ').strip() + '"'
    base = ' AND '.join('TEXT=' + quote(w) for w in words)
    classes = ' OR '.join(c['kind'].upper() + '=' + quote(c['code']) for c in selected)
    query = '(' + base + ')' if base else ''
    if classes:
        query += ('\nAND ' if query else '') + '(' + classes + ')'
    previous = previous_query_for_context(preserve_conditions) if refine else {}
    if refine and STATE['queries'] and STATE['queries'][-1].get('boolean_tree') and not previous:
        raise ValueError('観点・取り込み式と現在のキーワードまたは分類が変わっています。調査ナビで案を作り直すか、分類の改善案として更新してください。元の論理条件は保持しています。')
    old_include, old_exclude = previous.get('include_terms', []), previous.get('exclude_terms', [])
    for values in (include,exclude):
        if values is not None and (not isinstance(values,list) or any(not isinstance(v,str) for v in values)):
            raise ValueError('追加語・除外語は語句のリストにしてください。')
    include = list(dict.fromkeys(old_include if include is None else include))
    exclude = list(dict.fromkeys(old_exclude if exclude is None else exclude))
    term_options = refinement_terms(STATE['patents']) if refine else {'include': [], 'exclude': []}
    term_options = {k: list(dict.fromkeys(term_options[k] + (old_include if k == 'include' else old_exclude))) for k in term_options}
    for kind,chosen_terms in (('include',include),('exclude',exclude)):
        if any(term not in term_options[kind] for term in chosen_terms):
            raise ValueError('対応する追加・除外候補にない語が含まれています。再分析してください。')
    if include:
        query += '\nAND (' + ' OR '.join('TEXT=' + quote(w) for w in include) + ')'
    if exclude:
        kept = [r for r in STATE['patents'] if r.get('label') == 'keep']
        unsafe = [w for w in exclude if any(w.casefold() in (r['title'] + r.get('abstract', '')).casefold() for r in kept)]
        if unsafe:
            raise ValueError('必要特許にも出現するため除外できない語: ' + '、'.join(unsafe))
        query += '\nNOT (' + ' OR '.join('TEXT=' + quote(w) for w in exclude) + ')'
    kept = [r for r in STATE['patents'] if r.get('label') == 'keep']
    retained = sum(not include or any(w.casefold() in (r['title'] + ' ' + r.get('abstract','')).casefold() for w in include) for r in kept)
    removed = [w for w in old_include if w not in include] + [w for w in old_exclude if w not in exclude]
    item = dict(id=uuid.uuid4().hex, version=len(STATE['queries'])+1, created_at=time.strftime('%Y-%m-%d %H:%M'), expression=query,
                type='改善案' if refine else '初案', terms=term_options, keywords=words, classifications=selected,
                include_terms=include, exclude_terms=exclude, removed_terms=removed,
                known_keep_coverage={'retained':retained,'total':len(kept)} if include and kept else None,
                note='DB非依存の設計式です。TEXT・IPC・F-TERMは検索先の入力欄に割り当ててください。分類群はOR、キーワードはANDです。',
                changes=('要否判断から特徴語を抽出。NOTは選択された語だけ適用。' if refine else '入力語と選択分類から初案を作成。'))
    if not refine:
        groups = {}
        for index, word in enumerate(words):
            union, _ = suggest_keyword_or_group(word)
            key = ('union', union) if union else ('single', index)
            groups.setdefault(key, []).append(Node('text', word))
        if any(len(alternatives) > 1 for alternatives in groups.values()):
            concepts = group('and', [group('or', alternatives) for alternatives in groups.values()])
            draft = dict(concept_tree=tree_data(concepts))
            base_tree, boolean_tree = refined_tree(draft, selected, include, exclude)
            item.update(concept_tree=tree_data(concepts), base_boolean_tree=base_tree,
                        boolean_tree=boolean_tree, keyword_context=STATE['keywords'],
                        expression=describe_tree(tree_from_data(boolean_tree)),
                        note='既知の代替表現を仮のORグループにまとめ、別の条件とANDで結びました。調査ナビで意味・和集合の範囲を見直せます。')
            item['term_catalog'] = terms_of(item)
    if previous.get('boolean_tree'):
        base_tree, boolean_tree = refined_tree(previous, selected, include, exclude)
        for name in ('concept_tree', 'facets', 'purpose', 'provenance', 'target_ids', 'prompt_version', 'keyword_context', 'invention_context', 'invention_plan_id'):
            if name in previous:
                item[name] = copy.deepcopy(previous[name])
        item.update(base_boolean_tree=base_tree, boolean_tree=boolean_tree,
                    expression=describe_tree(tree_from_data(boolean_tree)),
                    note='観点・取り込み式の論理構造を保持し、追加語と除外語を更新しています。')
        item['term_catalog'] = terms_of(item)
    if classification_changes is not None:
        item['classification_changes']=classification_changes
        item['changes']='要否判定・学習予測の分類と特徴語から改善。FI・Fターム由来のIPCは対応づけた候補として扱い、選択した分類をOR条件に反映。NOTは選択された語だけ適用。'
        STATE.update(candidates=pool,selected=selected_keys)
    STATE['queries'].append(item)
    if isinstance(STATE.get('research_workbench'), dict):
        STATE['research_workbench']['active_query_id'] = item['id']
    return item

@app.get('/api/state')
def get_state():
    return public_state()

@app.get('/api/query/formats')
def output_formats():
    return query_formats()

@app.post('/api/query/export')
def output_query(body: dict):
    with LOCK:
        query_id=body.get('query_id')
        item=next((q for q in STATE['queries'] if q['id']==query_id),None) if query_id else (STATE['queries'][-1] if STATE['queries'] else None)
        if item is None:
            raise HTTPException(404,'出力する検索式が見つかりません。まず初案を作成してください。')
        snapshot=copy.deepcopy(item)
    return export_query(snapshot,body.get('format','jplatpat'),replacements=body.get('replacements'),
                        omit_unsupported=body.get('omit_unsupported',False),allow_unverified=body.get('allow_unverified',False),
                        descendants=body.get('descendants',False),language=body.get('language','auto'))

@app.post('/api/candidates')
def candidates(body: dict):
    watched_fields = ('keywords', 'candidates', 'selected', 'classification_view', 'candidate_proposal')
    with LOCK:
        idle()
        text = checked_text(body.get('keywords', ''))
        if not text:
            raise ValueError('調べたい技術のキーワードを入力してください。')
        use_llm = body.get('use_llm', False)
        if not isinstance(use_llm, bool):
            raise ValueError('LLM利用の指定は true / false で指定してください。')
        before = {name: copy.deepcopy(STATE.get(name)) for name in watched_fields}
        selected = normalize_selection(STATE['selected'])
        connection = copy.deepcopy(SETTINGS)
    # Slow model requests must not prevent state/progress reads or other UI
    # interactions. Commit only if the classification workspace is unchanged.
    notices = []
    proposal = dict(id=uuid.uuid4().hex, created_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                    keywords=text, llm_used=use_llm, returned_count=0, accepted_count=0,
                    new_count=0, existing_count=0, rejected_count=0, duplicate_count=0,
                    items=[], rejected=[], notices=notices)
    items = candidate_list(text, use_llm, notices=notices, proposal=proposal, connection=connection)
    old_keys = {classification_key(row) for row in before['candidates']}
    for row in proposal['items']:
        row['is_new'] = classification_key(row) not in old_keys
        row['status'] = 'new' if row['is_new'] else 'existing'
    proposal['new_count'] = sum(row['is_new'] for row in proposal['items'])
    proposal['existing_count'] = len(proposal['items']) - proposal['new_count']
    pool = explorer.merge_candidates(before['candidates'], items)
    rows = explorer.merge_candidates(explorer.overview(items, text), [c for c in pool if c.get('origins')])
    # Show the exact model proposals alongside broad groups, including repeated
    # codes. Users can inspect and select what the model actually suggested.
    rows = explorer.merge_candidates(rows, proposal['items'])
    pool = explorer.merge_candidates(pool, rows)
    note = '材料・製造・界面などの観点から推薦しています。コードの存在と主題への適合性は別です。下位へ進んで定義を確認してください。'
    if use_llm:
        note += f' LLM提案{proposal["returned_count"]}件のうち、候補に{proposal["accepted_count"]}件を反映（新規{proposal["new_count"]}件・既存{proposal["existing_count"]}件）。提案コードを上位分類と一緒に表示しています。'
    if notices:
        note += ' ' + ' '.join(notices)
    with LOCK:
        idle()
        if any(STATE.get(name) != before[name] for name in watched_fields) or (use_llm and SETTINGS != connection):
            raise HTTPException(409, '候補の生成中に分類・選択・キーワードまたはLLM設定が変更されました。現在の操作を保持しました。内容を確認して再実行してください。')
        STATE.update(keywords=text,candidates=pool,selected=selected,classification_view=dict(keys=[classification_key(c) for c in rows],direction='overview',note=note,total=len(rows),next_offset=None,trail=[]))
        STATE['candidate_proposal'] = proposal
        try:
            save()
        except Exception:
            STATE.update(before)
            raise
    return public_state()

@app.post('/api/selection')
def selection(body: dict):
    with LOCK:
        idle()
        selected = body.get('selected', [])
        STATE['selected'] = normalize_selection(selected)
        save()
    return public_state()

@app.post('/api/candidates/broader')
def add_rejected_broader_candidate(body: dict):
    """Explicitly add a verified broader scope, without selecting it for a query."""
    with LOCK:
        idle()
        proposal = STATE.get('candidate_proposal') or {}
        if body.get('proposal_id') != proposal.get('id') or not proposal.get('llm_used') or proposal.get('keywords') != STATE['keywords']:
            raise HTTPException(409, '候補の生成結果またはテーマが変わりました。最新の候補を確認してください。')
        index = body.get('rejected_index')
        if type(index) is not int:
            raise ValueError('確認する除外候補を指定してください。')
        rejected = next((item for position, item in enumerate(proposal.get('rejected', []), 1)
                         if item.get('index', position) == index), None)
        if rejected is None:
            raise ValueError('この生成結果に指定された除外候補はありません。')
        item = rejected_candidate_guidance(rejected).get('broader_candidate')
        if not item or not item.get('verified') or not item.get('selectable'):
            raise ValueError('この候補から確認できる上位IPCはありません。分類指定から確認してください。')
        before = {name: copy.deepcopy(STATE.get(name)) for name in ('candidates', 'selected', 'classification_view', 'candidate_proposal')}
        try:
            old = next((row for row in STATE['candidates'] if classification_key(row) == item['key']), None)
            row = copy.deepcopy(old or item)
            origin = dict(type='reviewed_broader', label='除外候補から利用者が確認した上位IPC',
                          from_code=rejected['code'], proposal_id=proposal['id'])
            row.setdefault('origins', [])
            if origin not in row['origins']:
                row['origins'].append(origin)
            set_classification_view([row], direction='seed', focus=row, total=1, next_offset=None, trail=[],
                note=f'{rejected["code"]} を置換せず、確認済みの広い探索起点 {item["code"]} を候補に追加しました。検索式に使うには選択してください。')
            rejected['broader_added'] = True
            save()
        except Exception:
            STATE.update(before)
            raise
    return public_state()


@app.post('/api/classification')
def add_classification(body: dict):
    with LOCK:
        idle()
        kind = body.get('kind')
        code = explorer.normalize(kind, checked_text(body.get('code', ''), 32))
        STATE['selected'] = normalize_selection(STATE['selected'])
        item=explorer.enrich(dict(code=code,kind=kind,title=checked_text(body.get('title',code),120),origins=[{'type':'manual','label':'利用者の推薦'}],reason='利用者が推薦した分類'))
        set_classification_view([item],direction='seed',note='推薦した分類を起点に探索できます。',total=1,next_offset=None,trail=[])
        save()
    return public_state()


@app.post('/api/classifications/browse')
def browse_classifications(body: dict):
    with LOCK:
        idle()
        direction=body.get('direction','children')
        if direction=='overview':
            classification_overview(candidate_list(STATE['keywords'],False) if STATE['keywords'].strip() else None)
        elif direction=='seeds':
            items=[c for c in STATE['candidates'] if c.get('origins')]
            set_classification_view(items,direction='seed',note='利用者の推薦・ターゲット由来の分類です。',total=len(items),next_offset=None,trail=[])
        else:
            view=explorer.browse(kind=body.get('kind','IPC'),code=body.get('code'),direction=direction,text=body.get('text',''),offset=body.get('offset',0))
            items=view.pop('items')
            set_classification_view(items,**view)
        save()
    return public_state()


@app.post('/api/classifications/translations')
def classification_translations_update(body: dict):
    # Public classification codes only; network requests do not hold the workspace
    # lock or replace concurrent manual selections, query history, or lab plans.
    with LOCK:
        settings = copy.deepcopy(SETTINGS)
    return classification_translations.fetch_translations(body.get('codes'), settings, cache_dir=DATA)


@app.post('/api/classifications/seeds')
def classification_seeds(body: dict):
    with LOCK:
        idle()
        select=body.get('select',False)
        if not isinstance(select,bool):
            raise ValueError('分類の選択指定を確認してください。')
        items=explorer.seed_items(body,STATE['patents'])
        set_classification_view(items,direction='seed',note='推薦した分類を表示しています。分類の出典と、どの特許から来たかを確認できます。',total=len(items),next_offset=None,trail=[])
        if select:
            STATE['selected']=list(dict.fromkeys(STATE['selected']+[classification_key(c) for c in items if c['selectable']]))
        save()
    return public_state()


@app.post('/api/keywords')
def update_keywords(body: dict):
    with LOCK:
        idle()
        text=checked_text(body.get('keywords',''))
        keywords(text)
        STATE['keywords']=text
        save()
    return public_state()

@app.post('/api/query')
def query(body: dict):
    with LOCK:
        idle()
        refine=body.get('refine',False)
        if not isinstance(refine,bool):
            raise ValueError('改善案の指定は true / false にしてください。')
        if 'classification_keys' in body and (not refine or not isinstance(body['classification_keys'],list)):
            raise ValueError('改善案の分類は体系付きキーのリストで指定してください。')
        before=copy.deepcopy(STATE)
        pending=DATA / 'workspace.tmp'
        pending_before=pending.read_bytes() if pending.exists() else None
        try:
            make_query(refine,body.get('include'),body.get('exclude'),classification_keys=body.get('classification_keys'))
            result=public_state()
            save()
        except Exception:
            STATE.clear()
            STATE.update(before)
            if pending_before is None:
                pending.unlink(missing_ok=True)
            else:
                pending.write_bytes(pending_before)
            raise
    return result

@app.get('/api/refinement')
def refinement():
    with LOCK:
        result = refinement_terms(STATE['patents'])
        previous = previous_query_for_context(ignore_classifications=True)
        for kind in ('include','exclude'):
            result[kind] = list(dict.fromkeys(result[kind] + previous.get(kind + '_terms', [])))
        result['active_include']=previous.get('include_terms',[])
        result['active_exclude']=previous.get('exclude_terms',[])
        result['classifications']=classification_feedback(STATE['patents'],normalize_selection(STATE['selected']))
        return result

@app.post('/api/upload')
async def upload(request: Request):
    # Stream with a hard cap so an absent/incorrect Content-Length cannot bypass it.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError('CSVは20MB以下にしてください。')
    mapping = json.loads(request.query_params.get('mapping', '{}'))
    if not isinstance(mapping, dict):
        raise ValueError('列の割り当てを確認してください。')
    merge_mode = request.query_params.get('merge', 'false')
    if merge_mode not in ('true', 'false'):
        raise ValueError('追加・置換の指定を確認してください。')
    with LOCK:
        idle()
        parsed = parse_csv(bytes(raw), mapping)
        if parsed['needs_mapping']:
            return parsed
        rows = parsed.pop('rows')
        observation = new_observation(rows, STATE)
        if merge_mode == 'true':
            rows, report = merge_patents(STATE['patents'], rows)
            parsed.update(import_mode='merge', merge=report)
        else:
            parsed['import_mode'] = 'replace'
        clusters = map_patents(rows, STATE['keywords'])
        previous = copy.deepcopy(STATE)
        training_metadata = copy.deepcopy(STATE['training']) if merge_mode == 'true' else None
        if training_metadata and report['added']:
            training_metadata.update(dataset_changed=True, added_since_training=report['added'])
        try:
            STATE.update(patents=rows, clusters=clusters, demo=False, training=training_metadata,
                         import_info=parsed, search_result=observation)
            save()
        except Exception:
            STATE.clear()
            STATE.update(previous)
            raise
    return public_state()


@app.post('/api/classifications/enrich-import')
async def enrich_classification_import(request: Request):
    """Fill FI for matching existing rows without reimporting or retraining."""
    from analysis_engine import backfill_fi
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError('CSVは20MB以下にしてください。')
    mapping = json.loads(request.query_params.get('mapping', '{}'))
    if not isinstance(mapping, dict):
        raise ValueError('列の割り当てを確認してください。')
    parsed = parse_csv(bytes(raw), mapping)
    if parsed['needs_mapping'] or not parsed['mapping'].get('fi'):
        raise ValueError('公報番号・発明の名称・FIを含むCSVを指定してください。')
    with LOCK:
        idle()
        rows, report = backfill_fi(STATE['patents'], parsed['rows'])
        if report['conflicts']:
            raise ValueError('公報番号と内容の不一致、または既存FIとの不一致があるため補完を中止しました。既存判定は変更していません。')
        previous_rows = STATE['patents']
        previous_import = copy.deepcopy(STATE.get('import_info'))
        try:
            STATE['patents'] = rows
            STATE['import_info'] = {**(STATE.get('import_info') or {}), 'fi_enrichment': report}
            save()
        except Exception:
            STATE['patents'] = previous_rows
            STATE['import_info'] = previous_import
            raise
    return public_state()

@app.post('/api/demo')
def demo():
    with LOCK:
        idle()
        words = '全固体電池 固体電解質 界面抵抗'
        rows = demo_rows()
        STATE.update(keywords=words, candidates=[explorer.enrich(c) for c in suggest(words)], selected=['H01M10/0562', '5H029AJ06'], patents=rows,
                     clusters=map_patents(rows, words), training=None, queries=[], agent_log=[], demo=True,
                     import_info=None, candidate_proposal=None, search_result=None, convergence_tracking=None)
        make_query()
        classification_overview()
        save()
    return public_state()

@app.post('/api/labels')
def labels(body: dict):
    with LOCK:
        idle()
        ids = body.get('ids', [])
        label = body.get('label')
        if label not in ('keep', 'exclude', None) or not isinstance(ids, list):
            raise ValueError('判定を確認してください。')
        if not set(ids) <= {r['id'] for r in STATE['patents']}:
            raise ValueError('存在しない特許IDです。')
        for row in STATE['patents']:
            if row['id'] in ids:
                row.update(label=label, label_source='human' if label else None, label_reason='利用者による判断' if label else '')
                row['score'] = None
        STATE['training'] = None
        for row in STATE['patents']:
            row['score'] = None
        save()
    return public_state()

def assessment_options(body, *, allow_empty=False):
    """Snapshot only the applied query; unsaved workbench plans are not criteria."""
    workbench = STATE.get('research_workbench') or {}
    active_id = workbench.get('active_query_id')
    adopted = next((query for query in STATE['queries'] if active_id and query.get('id') == active_id), None)
    return llm_judgment.prepare(body, STATE['patents'], STATE['keywords'], SETTINGS['provider'],
                                allow_empty=allow_empty, adopted_query=copy.deepcopy(adopted))


def run_llm_assessment(body, progress_callback=None, *, _prepared=None):
    """Synchronous shared judgment service; the caller owns JOB and STOP."""
    report_progress = progress_callback or progress
    with LOCK:
        if _prepared is None:
            options = assessment_options(body, allow_empty=True)
            rows_snapshot = copy.deepcopy(STATE['patents'])
            settings_snapshot = copy.deepcopy(SETTINGS)
        else:
            options, rows_snapshot, settings_snapshot = _prepared
        if not options['target_ids']:
            return dict(skipped=True, status='completed', completed_count=0, total_count=0, judged_count=0, deferred_count=0)
    initialized = False

    def publish(updates, metadata):
        nonlocal initialized
        with LOCK:
            previous_rows = [(row, copy.deepcopy(row)) for row in STATE['patents']]
            previous_training = copy.deepcopy(STATE['training'])
            previous_orchestration = copy.deepcopy(STATE.get('orchestration'))
            previous_progress = {key: JOB[key] for key in ('progress', 'message')}
            previous_initialized = initialized
            temp_path = DATA / 'workspace.tmp'
            previous_temp = temp_path.read_bytes() if temp_path.exists() else None
            try:
                if not initialized:
                    # An error-only report after failed initialization must
                    # preserve the scores that existed before this job.
                    if metadata['status'] == 'running':
                        prior = STATE.get('training') or {}
                        same_judgment = (
                            prior.get('mode') == 'llm'
                            and prior.get('criteria') == options['criteria']
                            and prior.get('provider') == settings_snapshot.get('provider')
                            and prior.get('model') == settings_snapshot.get('model')
                            and prior.get('context_fingerprint') == options.get('context_fingerprint')
                        )
                        for row in STATE['patents']:
                            if (same_judgment and row.get('label') in ('keep', 'exclude')
                                    and row.get('label_source') == 'agent'
                                    and row.get('score_source') == 'llm'):
                                continue
                            row['score'] = None
                            row['score_source'] = None
                    initialized = True
                current = {row['id']: row for row in STATE['patents']}
                if updates and STOP.is_set():
                    previous = STATE.get('training') or {}
                    for key in ('completed_count', 'judged_count', 'deferred_count', 'protected_count', 'label_counts', 'progress'):
                        if key in previous:
                            metadata[key] = copy.deepcopy(previous[key])
                    metadata.update(status='cancelled', stage='停止',
                                    message=f'停止 {metadata["completed_count"]}/{metadata["total_count"]}')
                    updates = []
                for update in updates:
                    row = current.get(update['id'])
                    if row is None or row.get('label_source') == 'human' or row.get('label'):
                        metadata['protected_count'] += 1
                        if update['label']:
                            metadata['judged_count'] -= 1
                            metadata['label_counts'][update['label']] -= 1
                        else:
                            metadata['deferred_count'] -= 1
                        continue
                    row.update({key: value for key, value in update.items() if key != 'id'})
                metadata.update(job_id=JOB['id'], provider=settings_snapshot['provider'],
                                model=settings_snapshot.get('model', ''))
                STATE['training'] = metadata
                report_progress(metadata['progress'], metadata['message'])
                save()
            except Exception:
                for row, snapshot in previous_rows:
                    row.clear()
                    row.update(snapshot)
                STATE['training'] = previous_training
                if isinstance(previous_orchestration, dict) and isinstance(STATE.get('orchestration'), dict):
                    STATE['orchestration'].clear()
                    STATE['orchestration'].update(previous_orchestration)
                elif previous_orchestration is not None:
                    STATE['orchestration'] = previous_orchestration
                JOB.update(previous_progress)
                initialized = previous_initialized
                try:
                    if previous_temp is None:
                        temp_path.unlink(missing_ok=True)
                    else:
                        temp_path.write_bytes(previous_temp)
                except OSError:
                    raise ValueError('LLM判定の保存に失敗し、一時ファイルも復元できませんでした。保存先の空き容量と書き込み権限を確認してください。') from None
                raise ValueError('LLM判定の保存に失敗しました。未保存のバッチを取り消し、直前に保存できた判定を保持しています。保存先の空き容量と書き込み権限を確認してください。') from None
            return copy.deepcopy(metadata)

    def request_judgment(system, payload):
        if settings_snapshot.get('provider') == 'local':
            return complete(settings_snapshot, system, payload,
                            response_schema=llm_judgment.response_schema(payload['required_ids']))
        return complete(settings_snapshot, system, payload)

    return llm_judgment.run(rows_snapshot, options, request_judgment, publish, STOP.is_set)

def run_model_training(mode, include_agent=False, progress_callback=None):
    """Train using the same service from the map, agent and orchestration."""
    if mode not in ('lightweight', 'transformer'):
        raise ValueError('学習方式は軽量学習かTransformerを選んでください。')
    report_progress = progress_callback or progress
    with LOCK:
        rows = copy.deepcopy(STATE['patents'])
        settings = copy.deepcopy(SETTINGS)
        job_id = JOB['id']
    result = train(rows, mode, settings['transformer_model'], settings['allow_model_download'],
                   str(DATA / 'models' / job_id), report_progress, STOP.is_set, bool(include_agent))
    with LOCK:
        if STOP.is_set():
            return dict(status='cancelled')
        old_rows = copy.deepcopy(STATE['patents'])
        old_training = copy.deepcopy(STATE['training'])
        try:
            for row in STATE['patents']:
                row['score'] = round(result['scores'][row['id']], 4)
                row['score_source'] = mode
            result.pop('scores')
            result.update(mode=mode, job_id=job_id, status='completed')
            STATE['training'] = result
            save()
        except Exception:
            for row, previous in zip(STATE['patents'], old_rows):
                row.clear()
                row.update(previous)
            STATE['training'] = old_training
            raise
    return result


@app.post('/api/train')
def training(body: dict):
    mode = body.get('mode', 'lightweight')
    if mode not in ('lightweight', 'transformer', 'llm'):
        raise ValueError('学習モードを確認してください。')
    with LOCK:
        idle()
        if mode == 'llm':
            options = assessment_options(body)
            prepared = options, copy.deepcopy(STATE['patents']), copy.deepcopy(SETTINGS)
            action = lambda: run_llm_assessment(body, _prepared=prepared)
        else:
            action = lambda: run_model_training(mode, bool(body.get('include_agent')))
        return begin_job(action, kind='training', mode=mode)

@app.post('/api/job/stop')
def stop():
    STOP.set()
    with LOCK:
        JOB['stop_requested'] = True
    return {'message': '停止要求を送信しました。実行中のAPI応答・モデル読込後に停止します。'}

@app.post('/api/settings')
def settings(body: dict):
    with LOCK:
        idle()
        proposed = SETTINGS.copy()
        for k in DEFAULT_SETTINGS:
            if k not in body:
                continue
            if k in SECRET_SETTINGS and not body[k]:
                continue
            if k == 'llm_timeout':
                proposed[k] = validate_timeout(body[k])
            elif isinstance(DEFAULT_SETTINGS[k], bool):
                if not isinstance(body[k], bool):
                    raise ValueError('設定値の形式を確認してください。')
                proposed[k] = body[k]
            else:
                proposed[k] = checked_text(body[k], 2000)
        old_url, new_url = urlparse(SETTINGS['base_url']), urlparse(proposed['base_url'])
        if (old_url.scheme, old_url.netloc) != (new_url.scheme, new_url.netloc) and not body.get('api_key'):
            proposed['api_key'] = ''
        if body.get('clear_secrets'):
            proposed.update(api_key='', proxy='')
        if body.get('clear_ops'):
            proposed.update(ops_key='', ops_secret='')
        if proposed['provider'] not in ('offline', 'openai', 'local'):
            raise ValueError('接続種別を確認してください。')
        if proposed['classification_layout'] not in ('semantic', 'grid'):
            raise ValueError('分類の配置方式は「意味の近さ」または「整列」を選んでください。')
        if new_url.scheme not in ('http','https') or not new_url.hostname or new_url.username or new_url.password or new_url.query or new_url.fragment:
            raise ValueError('API URLは認証情報やクエリを含まない http(s):// のURLにしてください。')
        SETTINGS.update(proposed)
        save()
    return public_state()

@app.post('/api/settings/test')
def test_connection():
    result = complete(SETTINGS, '接続テストです。{"ok":true} と返してください。', {'test': True})
    if result.get('ok') is not True:
        raise ValueError('APIに接続しましたがJSON応答を検証できませんでした。')
    return {'ok': True, 'message': '接続とJSON応答を確認しました。'}

@app.post('/api/agent')
def agent(body: dict):
    criteria = checked_text(body.get('criteria', ''), 5000)
    if not criteria or not STATE['keywords']:
        raise ValueError('先に探索キーワードと判断基準を入力してください。')
    if SETTINGS['provider'] == 'offline':
        raise ValueError('判断エージェントにはLLMが必要です。設定タブから接続してください。')
    max_items = body.get('max_items', 100)
    threshold = body.get('threshold', .8)
    if body.get('mode', 'lightweight') not in ('lightweight', 'transformer') or not isinstance(body.get('train', False), bool):
        raise ValueError('学習方式・学習実行の指定を確認してください。')
    with LOCK:
        idle()
        llm_judgment.prepare(dict(criteria=criteria, max_items=max_items, threshold=threshold, fresh_only=True),
                             STATE['patents'], STATE['keywords'], SETTINGS['provider'], allow_empty=True)
    def log(message):
        with LOCK:
            STATE['agent_log'].append(dict(time=time.strftime('%H:%M:%S'), message=message))
            STATE['agent_log'] = STATE['agent_log'][-100:]
            save()
    def action():
        log('開始。人の確定ラベルを保護し、1サイクルで停止します。')
        if not STATE['candidates']:
            items = candidate_list(STATE['keywords'], True)
            with LOCK:
                STATE['candidates'] = items
        progress(10, '判断基準から分類候補を選んでいます')
        result = complete(SETTINGS, SELECTION_SYSTEM,
                          {'keywords': STATE['keywords'], 'criteria': criteria, 'candidates': [{**c,'key':classification_key(c)} for c in STATE['candidates']]})
        chosen = result.get('selected')
        chosen=normalize_selection(chosen)
        if STOP.is_set():
            return
        with LOCK:
            STATE['selected'] = chosen
            make_query(bool(STATE['queries']),preserve_conditions=True)
        log('分類を選択し初案を作成: ' + str(result.get('reason', ''))[:500])
        result = run_llm_assessment(
            dict(criteria=criteria, max_items=max_items, threshold=threshold, fresh_only=True),
            lambda value, message: progress(20 + int(.55 * value), message))
        if STOP.is_set():
            return
        log(f"特許判定: {result.get('completed_count', 0)}件を処理、採用{result.get('judged_count', 0)}件・保留{result.get('deferred_count', 0)}件。既存の判定は保持しました。")
        if body.get('train'):
            run_model_training(body.get('mode', 'lightweight'), True,
                               lambda value, message: progress(75 + int(.15 * value), message))
            if STOP.is_set():
                return
            log('学習完了。エージェント判断を含むため独立評価は表示しません。')
        if STATE['patents']:
            with LOCK:
                terms = refinement_terms(STATE['patents'])
                addition = terms['include'][:2]
                kept = [r for r in STATE['patents'] if r.get('label') == 'keep']
                covered = kept and all(any(w.casefold() in (r['title'] + ' ' + r.get('abstract','')).casefold() for w in addition) for r in kept)
                # Do not introduce a mandatory vocabulary clause that drops known positives.
                previous_include = previous_query_for_context().get('include_terms', [])
                current_keys = normalize_selection(STATE['selected'])
                feedback = classification_feedback(STATE['patents'], current_keys)
                next_keys = list(dict.fromkeys(current_keys + feedback['recommended_keys']))
                make_query(True, None if previous_include else addition if covered else None, None,
                           classification_keys=next_keys)
            log('判定済み公報から必要側の支持が多いIPCを追加し、次の検索式を提案しました。既存分類は保持し、分類のNOTは作りません。')
        log('次のCSVを待っています。外部の特許検索・ダウンロードは接続されていません。')
    return begin_job(action, kind='agent', mode=body.get('mode', 'lightweight') if body.get('train') else None)

@app.get('/api/export')
def export():
    with LOCK:
        data = json.dumps(STATE, ensure_ascii=False, indent=2)
    return Response(data, media_type='application/json', headers={'Content-Disposition': 'attachment; filename="patent-atlas-project.json"'})

@app.get('/api/export/labels')
def export_labels():
    fields = ['id','title','abstract','applicant','ipc','fi','fterm','cpc','year','label','label_source','label_reason','score']
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    with LOCK:
        for r in STATE['patents']:
            writer.writerow({k: "'" + v if isinstance(v, str) and v.lstrip().startswith(('=','+','-','@')) else v for k,v in r.items()})
    return Response(stream.getvalue().encode('utf-8-sig'), media_type='text/csv', headers={'Content-Disposition':'attachment; filename="patent-labels.csv"'})

@app.get('/')
def index():
    return FileResponse(ROOT / 'static' / 'index.html')

if STATE['candidates']:
    STATE['candidates']=[explorer.enrich(c) for c in STATE['candidates']]
    STATE['selected']=normalize_selection(STATE['selected'])
    if (STATE.get('classification_view') or {}).get('focus'):
        STATE['classification_view']['focus']=explorer.enrich(STATE['classification_view']['focus'])
    if not STATE.get('classification_view'):
        classification_overview()

register_discovery_routes(app, globals())
register_orchestration_routes(app, globals())
register_convergence_routes(app, globals())
register_research_routes(app, globals())
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
