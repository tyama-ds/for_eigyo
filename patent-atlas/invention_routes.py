"""Practitioner workflow: invention evidence -> elements -> scoped search statements."""
import copy
import time
import uuid

from fastapi import HTTPException

import classification_bridge
import classification_explorer as explorer
from research_strategy import selected_sources
from invention_strategy import plan_invention, compose_invention_query


def register_invention_routes(app, context, services):
    state, settings, lock = context['STATE'], context['SETTINGS'], context['LOCK']
    wb, rows = services['workbench'], services['source_patents']
    check_revision, commit = services['check_revision'], services['commit']
    fingerprint, checked_brief = services['fingerprint'], services['checked_brief']
    idle = context['idle']

    def candidate_pool(brief, patents):
        # Actual seed metadata first. Existing proposed classifications stay
        # proposals; membership in this pool never makes a code mandatory.
        pool, notes = [], []
        for patent in patents:
            if patent['id'] not in brief['target_ids']:
                continue
            extracted = classification_bridge.extract(patent)
            pool = explorer.merge_candidates(pool, extracted['items'])
            notes.extend(extracted['warnings'])
        pool = explorer.merge_candidates(pool, (wb().get('plan') or {}).get('classification_candidates', []))
        pool = explorer.merge_candidates(pool, state.get('candidates', []))
        pool = [c for c in pool if c.get('selectable')]
        if len(pool) > 120:
            notes.append('分類候補はターゲット由来を優先して120件まで表示しています。')
        return pool[:120], list(dict.fromkeys(notes))

    @app.post('/api/research/invention/plan')
    def create_plan(body: dict):
        use_llm = body.get('use_llm', False)
        if not isinstance(use_llm, bool):
            raise ValueError('LLM利用はtrue/falseで指定してください。')
        with lock:
            idle(); check_revision(body)
            patents = rows()
            brief = checked_brief(body.get('brief', {}), wb()['brief'], patents)
            if not brief['purpose']:
                raise ValueError('今回の検索目的を入力してください。')
            input_data = {k: copy.deepcopy(brief[k]) for k in ('purpose', 'goal', 'keywords', 'user_aspects')}
            input_data.update(invention_text=body.get('invention_text', ''),
                              manual_elements=body.get('manual_elements', []))
            sources = selected_sources(patents, brief['target_ids'])
            candidates, notes = candidate_pool(brief, patents)
            connection = copy.deepcopy(settings)
            stamp = fingerprint((wb(), state['patents']))
        plan = plan_invention(input_data, sources, candidates, connection, use_llm=use_llm)
        plan.update(id=uuid.uuid4().hex, brief=brief, target_ids=brief['target_ids'],
                    classification_candidates=candidates, classification_notes=notes,
                    created_at=time.strftime('%Y-%m-%d %H:%M'))
        with lock:
            idle()
            if stamp != fingerprint((wb(), state['patents'])):
                raise HTTPException(409, '要素分解中に調査条件・特許群が変わりました。前の案を保持しています。')
            references = services['merge_target_references'](wb()['target_patents'],
                [p for p in patents if p['id'] in brief['target_ids']])

            def action():
                if brief != wb()['brief']:
                    wb()['plan'] = None
                wb().update(brief=brief, entry_mode=brief['entry_mode'], target_patents=references,
                            invention=dict(input=copy.deepcopy(plan['input']), plan=plan))
            return commit(action)

    def proposed(body):
        plan = (wb().get('invention') or {}).get('plan')
        if not plan or plan['id'] != body.get('plan_id'):
            raise HTTPException(409, '要素分解の案が変わっています。現在の案で確認してください。')
        if plan['brief'] != wb()['brief']:
            raise HTTPException(409, '調査条件が要素分解後に変わっています。要素を再起案してください。')
        query = compose_invention_query(plan, body, plan['classification_candidates'])
        query.update(purpose=plan['purpose'], target_ids=copy.deepcopy(plan['target_ids']),
                     prompt_version=plan['prompt_version'], keyword_context=plan['brief']['keywords'],
                     invention_plan_id=plan['id'])
        query = services['enrich_query'](query)
        warnings = list(query.pop('warnings', []))
        # This checks observed words only. It cannot claim DB recall or prove
        # whether a branch would match a patent's unavailable full text.
        protected = {p['id']: p for p in rows()
                     if p['id'] in plan['target_ids'] or p.get('label') == 'keep'}
        for exclusion in body.get('exclusions', []):
            terms = exclusion['terms']
            hits = [p['id'] for p in protected.values()
                    if any(term.casefold() in (p.get('title', '') + '\n' + p.get('abstract', '')).casefold()
                           for term in terms)]
            if hits:
                scope = '検索全体' if exclusion['scope'] == 'all' else exclusion['scope'] + ' の枝'
                warnings.append(scope + 'の除外語がターゲット・必要特許の名称/要約にも現れます: ' +
                                '、'.join(hits[:10]) + '。枝への一致や全文は未確認です。')
        if body.get('strategy') == 'element':
            warnings.append('要素単独の探索です。他の要素との組み合わせや発明全体の一致を確認したものではありません。')
        warnings.append('検索件数・全文での一致は未評価です。出力先で検索し、結果をCSVで取り込んで比較してください。')
        # Warnings are part of the preview contract, so stale review cannot be
        # applied after relevant source data or the exclusion scope changes.
        return query, list(dict.fromkeys(warnings))

    @app.post('/api/research/invention/preview')
    def preview(body: dict):
        with lock:
            check_revision(body)
            query, warnings = proposed(body)
            return dict(query=query, expression=query['expression'], warnings=warnings,
                        preview_hash=fingerprint((query, warnings)))

    @app.post('/api/research/invention/apply')
    def apply(body: dict):
        with lock:
            idle(); check_revision(body)
            query, warnings = proposed(body)
            if body.get('expected_preview_hash') != fingerprint((query, warnings)):
                raise HTTPException(409, '最新の要素・分類・除外範囲をプレビューしてから採用してください。')

            def action():
                state['keywords'] = wb()['brief']['keywords']
                saved = services['save_query'](query, '発明要素からの検索式')
                saved['note'] = '選択した発明要素・観点・分類の括弧構造を保持した検索式です。要素単独と組合せの検索を区別します。'
                saved['review_notes'] = warnings
            return commit(action)
