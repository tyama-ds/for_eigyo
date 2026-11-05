import {
  classificationTitle,
  replaceHtml,
  listen,
  getState,
  $,
  $$,
  esc,
  api,
  setState,
  toast,
  on,
  busy,
} from './core.js';
import {
  researchDraftScheduleSave,
  researchDraftFlush,
  researchDraftRebase,
} from './research-draft-store.js';
import {
  researchBrief,
  researchClassificationProvenance,
  captureResearchDraft,
  researchWorkbenchState,
  syncResearchPlanningControls,
  renderResearchWorkbench,
} from './research-workbench.js';
import { queryLibraryTreeHtml } from './query-library-ui.js';
import {
  researchGuidanceState,
  researchFlowValidate,
  researchFlowAfterPlan,
  researchFlowRevealPreview,
} from './research-guidance.js';
import {
  researchProgressIsRunning,
  researchProgressStart,
  researchProgressFinish,
} from './research-progress.js';
import { updateChrome } from './workspace-chrome.js';
import { setTab, setStep } from './app.js';

export const inventionWorkbenchState = {
  inventionInputDraft: null,
  inventionPlanDraft: null,
  inventionPlanning: false,
  inventionError: '',
};

/* Invention elements → search perspectives → explicit Boolean scopes. */

function inventionPlan() {
  return getState().research_workbench?.invention?.plan || null;
}
function inventionInput(value = {}) {
  return {
    invention_text: String(value.invention_text || ''),
    manual_elements: Array.isArray(value.manual_elements) ? value.manual_elements.map(String) : [],
  };
}
function inventionDraftInput() {
  return (
    inventionWorkbenchState.inventionInputDraft ||
    (inventionWorkbenchState.inventionInputDraft = inventionInput(
      getState().research_workbench?.invention?.input || inventionPlan()?.input
    ))
  );
}
function inventionLines(value) {
  return String(value || '')
    .split(/\r?\n/)
    .map((t) => t.trim())
    .filter(Boolean);
}
function inventionPlanState(plan = inventionPlan()) {
  if (!plan) return null;
  if (inventionWorkbenchState.inventionPlanDraft?.id !== plan.id)
    inventionWorkbenchState.inventionPlanDraft = {
      id: plan.id,
      strategy: 'element',
      element_ids: plan.elements.slice(0, 1).map((e) => e.id),
      elements: plan.elements.map((e) => ({
        id: e.id,
        perspectives: e.perspectives.map((p, index) => ({
          id: p.id,
          selected: index === 0,
          terms: [...p.terms],
          classification_keys: [],
          class_mode: 'text',
        })),
      })),
      perspective_joins: Object.fromEntries(plan.elements.map((e) => [e.id, 'or'])),
      exclusions: Object.fromEntries(['all', ...plan.elements.map((e) => e.id)].map((id) => [id, []])),
      confirm_exclusions: false,
      confirm_class_intersection: false,
      preview: null,
      previewPayload: null,
    };
  return inventionWorkbenchState.inventionPlanDraft;
}
function captureInventionDraft() {
  const input = inventionDraftInput();
  if ($('#invention-text')) input.invention_text = $('#invention-text').value;
  if ($('#invention-manual-elements'))
    input.manual_elements = inventionLines($('#invention-manual-elements').value);
  researchDraftScheduleSave();
  const d = inventionWorkbenchState.inventionPlanDraft;
  if (!d || !$('#invention-elements')) return;
  if ($('#invention-strategy')) d.strategy = $('#invention-strategy').value;
  d.element_ids = $$('[data-invention-use]:checked').map((el) => el.value);
  for (const el of $$('[data-invention-perspective]')) {
    const element = d.elements.find((e) => e.id === el.dataset.inventionElement),
      p = element?.perspectives.find((p) => p.id === el.dataset.inventionPerspective);
    if (!p) continue;
    const selected = $('[data-invention-perspective-use]', el);
    if (selected) p.selected = selected.checked;
    p.terms = inventionLines($('[data-invention-terms]', el)?.value || '');
    p.class_mode = $('[data-invention-class-mode]', el)?.value || 'text';
    p.classification_keys = $$('[data-invention-class]:checked', el).map((c) => c.value);
  }
  for (const el of $$('[data-invention-join]')) d.perspective_joins[el.dataset.inventionJoin] = el.value;
  for (const el of $$('[data-invention-exclude]'))
    d.exclusions[el.dataset.inventionExclude] = inventionLines(el.value);
  d.confirm_exclusions = $('#invention-confirm-exclusions')?.checked;
  d.confirm_class_intersection = $('#invention-confirm-classes')?.checked;
}
function inventionBriefKey(brief = {}) {
  return JSON.stringify({
    purpose: String(brief.purpose || '').trim(),
    goal: String(brief.goal || '').trim(),
    keywords: String(brief.keywords || '').trim(),
    entry_mode: brief.entry_mode || 'target',
    target_ids: [...(brief.target_ids || [])].sort(),
    user_aspects: [...(brief.user_aspects || [])].sort(),
  });
}
function inventionInputKey(input) {
  const normalized = inventionInput(input);
  normalized.invention_text = normalized.invention_text.trim();
  normalized.manual_elements = normalized.manual_elements.map((t) => t.trim()).filter(Boolean);
  return JSON.stringify(normalized);
}
function inventionPlanStale() {
  const plan = inventionPlan();
  return (
    !plan ||
    inventionBriefKey(researchBrief()) !== inventionBriefKey(plan.brief) ||
    inventionInputKey(inventionDraftInput()) !== inventionInputKey(plan.input)
  );
}
function invalidateInventionPreview() {
  if (inventionWorkbenchState.inventionPlanDraft) {
    inventionWorkbenchState.inventionPlanDraft.preview = null;
    inventionWorkbenchState.inventionPlanDraft.previewPayload = null;
  }
  const host = $('#invention-query-preview');
  if (host) replaceHtml(host, '');
  const stale = inventionPlanStale(),
    notice = $('#invention-stale-plan');
  if (notice) notice.hidden = !stale;
  for (const button of $$('#invention-preview,[data-invention-single]'))
    button.disabled = stale || inventionWorkbenchState.inventionPlanning;
}
function inventionClassLabel(c) {
  return [c.kind || c.system || '', c.code || '', classificationTitle(c)].filter(Boolean).join(' ');
}
function inventionClassProvenance(c) {
  return researchClassificationProvenance(c);
}
function inventionPerspectiveHtml(element, p, d, plan) {
  const suggested = new Set([...(p.classification_keys || []), ...(p.retrieved_classification_keys || [])]),
    choices = [...(plan.classification_candidates || [])].sort(
      (a, b) => Number(suggested.has(b.key)) - Number(suggested.has(a.key))
    ),
    active = d.class_mode !== 'text';
  return `<section class="invention-perspective" data-invention-element="${esc(element.id)}" data-invention-perspective="${esc(p.id)}"><div class="invention-perspective-title"><span>検索観点</span><label class="check"><input type="checkbox" data-invention-perspective-use aria-label="${esc(element.name + ' / ' + p.name)} を採用" ${d.selected ? 'checked' : ''}><b>${esc(p.name)}</b></label></div><p class="caption">${esc(p.reason || '')}</p>${p.anchor_term ? `<p class="caption">起案時の基準語：${esc(p.anchor_term)}。言い換えと合わせて編集できます。</p>` : ''}<label>語句 · 1行に1語句（語句同士はOR）<textarea data-invention-terms rows="3" maxlength="12000" aria-label="${esc(element.name + ' / ' + p.name)} の語句">${esc(d.terms.join('\n'))}</textarea></label><label>語句と分類の使い方<select data-invention-class-mode aria-label="${esc(element.name + ' / ' + p.name)} の検索方法">${[
    ['text', '語句だけ'],
    ['class', '分類だけ'],
    ['or', '語句 OR 分類 · どちらか'],
    ['and', '語句 AND 分類 · 両方'],
  ]
    .map(
      ([value, label]) =>
        `<option value="${value}" ${d.class_mode === value ? 'selected' : ''}>${label}</option>`
    )
    .join(
      ''
    )}</select></label><details class="invention-classifications" ${active ? 'open' : ''}><summary>分類候補を選ぶ · ${d.classification_keys.length} 件${active ? '' : '（語句だけでは未使用）'}</summary><p class="caption">分類表での存在確認と、特許に付与された分類・今回の観点への適合性は別です。由来を確認して選んでください。選んだ分類同士はORになります。</p><div class="invention-class-options">${choices.map((c) => `<label class="invention-class-option"><input type="checkbox" data-invention-class value="${esc(c.key)}" ${d.classification_keys.includes(c.key) ? 'checked' : ''} ${c.selectable === false ? 'disabled' : ''}><span title="${esc([c.reason, c.path_title].filter(Boolean).join('\n'))}"><b>${esc(inventionClassLabel(c))}</b><small>${suggested.has(c.key) ? 'この観点の候補 · ' : ''}${esc(c.group || '')} · ${esc(inventionClassProvenance(c))} · ${c.verified ? '分類表に存在' : '存在未確認'}</small></span></label>`).join('') || '<p class="caption">分類候補はありません。語句で検索できます。</p>'}</div></details></section>`;
}
function inventionElementHtml(element, d, plan) {
  const selected = d.element_ids.includes(element.id),
    edit = d.elements.find((e) => e.id === element.id);
  return `<article class="invention-element ${selected ? 'selected' : ''}" data-invention-card="${esc(element.id)}"><div class="invention-element-heading"><label class="check"><input type="checkbox" data-invention-use value="${esc(element.id)}" ${selected ? 'checked' : ''}><span><small>${esc(element.id)}</small><b>${esc(element.name)}</b></span></label><button class="button small" type="button" data-invention-single="${esc(element.id)}" ${inventionPlanStale() ? 'disabled' : ''}>この要素を検索</button></div><details class="research-element-detail" ${selected ? 'open' : ''}><summary>語句・分類・根拠を編集</summary><p>${esc(element.description || '')}</p>${element.relation ? `<p class="invention-relation"><b>要素間の関係</b>${esc(element.relation)}</p>` : ''}<details class="invention-evidence" open><summary>要素の記載と検索上の役割を確認</summary>${element.evidence?.length ? element.evidence.map((e) => `<blockquote><p>${esc(e.quote)}</p><cite>${esc(e.source_id === 'invention_text' ? '貼り付けた発明の原文' : e.source_id === 'research_goal' ? '調べたい技術・解決したいこと' : e.source_id)}</cite>${e.quote_adjustment === 'boundary_ellipsis_removed' ? '<br><small class="caption">前後の省略記号を除き、原文と照合</small>' : ''}</blockquote>`).join('') : '<p class="invention-hypothesis">仮説 · 対応する原文の引用がありません。入力・資料と照合してください。</p>'}</details>${element.perspectives.length > 1 ? `<label class="invention-perspective-join">この要素で採用した検索観点を結ぶ<select data-invention-join="${esc(element.id)}" aria-label="${esc(element.name)} の観点の結び方"><option value="or" ${d.perspective_joins[element.id] === 'or' ? 'selected' : ''}>OR · いずれかの観点</option><option value="and" ${d.perspective_joins[element.id] === 'and' ? 'selected' : ''}>AND · すべての観点</option></select></label>` : ''}<div class="invention-perspectives">${element.perspectives
    .map((p) =>
      inventionPerspectiveHtml(
        element,
        p,
        edit.perspectives.find((x) => x.id === p.id),
        plan
      )
    )
    .join(
      ''
    )}</div><details class="invention-exclusion"><summary>この要素だけの除外 · 任意</summary><label>この要素の条件から除く語句（1行に1語句）<textarea data-invention-exclude="${esc(element.id)}" rows="2" maxlength="6000">${esc((d.exclusions[element.id] || []).join('\n'))}</textarea></label><p class="caption">この要素の式にだけNOTを付けます。ほかの要素には適用しません。</p></details></details></article>`;
}
function inventionWorkbenchHtml(stage = 'all', method = 'all') {
  const input = inventionDraftInput(),
    plan = inventionPlan(),
    d = inventionPlanState(plan);
  const notes = [...(plan?.questions || []), ...(plan?.classification_notes || [])];
  const disabled = inventionWorkbenchState.inventionPlanning ? 'disabled' : '';
  const editing = stage === 'elements';
  const inputHtml = `<div id="invention-input-area">
    <div class="research-input-method" role="group" aria-label="要素の整理方法"><button type="button" data-research-input-method="llm" aria-pressed="${method === 'llm'}">LLMで分解する</button><button type="button" data-research-input-method="manual" aria-pressed="${method === 'manual'}">自分で要素を入力する</button></div>
    <p id="research-input-help" class="caption">${method === 'llm' ? '上に入力した説明と選択特許を使います。請求項を追加したい場合だけ原文欄を開いてください。' : '要素を1行ずつ入力して、検索範囲を選ぶ画面へ進みます。'}</p>
    ${method === 'llm' && getState().settings.provider === 'offline' ? '<div id="research-llm-unavailable" class="notice">LLMは未設定です。手動入力でもこのまま進められます。<button id="research-connect-llm" class="button small" type="button">接続設定を開く</button></div>' : ''}
    <div class="invention-source-grid"><details class="research-optional" ${input.invention_text ? 'open' : ''}><summary>請求項・原文を追加（任意）</summary><label>発明の説明・請求項を貼り付ける <span class="caption">任意</span><textarea id="invention-text" rows="5" maxlength="20000" placeholder="構成、動作、要素のつながりが分かる原文を入力">${esc(input.invention_text)}</textarea></label></details>
    ${method !== 'llm' ? `<label>自分で分けた要素 <span class="caption">任意 · 1行に1要素</span><textarea id="invention-manual-elements" rows="4" maxlength="4808" placeholder="例：温度を取得するセンサ&#10;取得温度に応じて冷却量を変える制御部">${esc(input.manual_elements.join('\n'))}</textarea><small class="caption">最大8要素、1要素600文字。「入力した要素を整理」はこの欄を使います。</small></label>` : ''}</div>
    <div class="button-row">${method !== 'manual' ? `<button id="invention-plan-llm" class="button primary" type="button" ${disabled}>LLMで発明を要素分解</button>` : ''}${method !== 'llm' ? `<button id="invention-plan-local" class="button primary" type="button" ${disabled}>入力した要素を整理</button>` : ''}</div></div>`;
  const reviewHtml = `<div id="invention-review-area"><button type="button" id="research-edit-input" class="button small">← 入力条件を見直す</button><p id="research-condition-summary" class="caption">${esc(researchBrief().purpose || '調査目的を入力してください')} · ターゲット ${researchBrief().target_ids.length} 件</p>${plan ? `<div id="invention-stale-plan" class="notice" role="status" ${inventionPlanStale() ? '' : 'hidden'}>発明の入力や調査条件が変わっています。要素分解をやり直してからプレビュー・採用してください。<button type="button" class="button small" id="research-replan-input">入力条件へ戻る</button></div><div class="invention-plan-heading"><h3>要素の案と検索観点</h3><span class="pill">${plan.method === 'llm' ? 'LLMによる案' : '手動要素の整理'}</span></div><p class="caption">${esc(plan.summary || '')}</p><p class="caption">採用する検索観点にチェックしてください。初期選択は各要素の先頭の観点です。追加の観点は必要に応じて採用します。</p><div class="invention-strategy"><label>今回の探し方<select id="invention-strategy"><option value="element" ${d.strategy === 'element' ? 'selected' : ''}>1要素から探す</option><option value="combination" ${d.strategy === 'combination' ? 'selected' : ''}>選んだ要素の組合せ · AND</option><option value="alternatives" ${d.strategy === 'alternatives' ? 'selected' : ''}>選んだ要素のいずれか · OR</option></select></label><p id="invention-selection-help" class="caption">${esc(inventionSelectionText(d))}</p></div><div id="invention-elements" class="invention-elements">${plan.elements.map((e) => inventionElementHtml(e, d, plan)).join('')}</div><details class="invention-scope-options"><summary>除外と、分類の組合せを確認</summary><label>検索全体から除く語句 · 任意（1行に1語句）<textarea data-invention-exclude="all" rows="2" maxlength="6000">${esc(d.exclusions.all.join('\n'))}</textarea></label><p class="caption">全体のNOTは、選んだ要素を組み合わせた式の外側に適用します。要素内のNOTとは範囲が異なります。</p><label class="check"><input id="invention-confirm-exclusions" type="checkbox" ${d.confirm_exclusions ? 'checked' : ''}>入力した除外語と、NOTの適用範囲を確認した</label><label class="check"><input id="invention-confirm-classes" type="checkbox" ${d.confirm_class_intersection ? 'checked' : ''}>語句と分類、または分類同士をANDで結ぶと、同じ文献に条件の同時一致を求め、候補が狭まることを確認した</label></details>${notes.length ? `<details class="invention-questions"><summary>確認する点 · ${notes.length} 件</summary><ul>${notes.map((q) => `<li>${esc(q)}</li>`).join('')}</ul></details>` : ''}<div class="invention-bottom"><p class="caption">原文との対応と要素間の関係を見直し、まずは1要素、必要に応じて組合せへ進めます。検索結果は先行技術の候補として確認します。</p><button id="invention-preview" class="button primary" type="button" ${inventionPlanStale() ? 'disabled' : ''}>選んだ条件をプレビュー →</button></div><div id="invention-query-preview" aria-live="polite">${inventionPreviewHtml()}</div>` : '<p class="caption">目的と発明の説明を使って、次に検索する要素を整理します。</p>'}</div>`;
  return `<section id="invention-workbench" data-render-scope="${esc(plan?.id || 'invention-input')}" class="panel invention-workbench" data-stage="${stage}" aria-label="発明を要素に分けて調査する"><div class="invention-heading"><div><h2>${editing ? '要素ごとに、検索範囲を選ぶ。' : '発明の整理方法を選ぶ。'}</h2><p class="caption">${editing ? '選んだ要素だけを検索します。語句・分類の詳細は必要な要素を開いて編集できます。' : '目的と発明の説明を使って、次に検索する要素を整理します。'}</p></div><details class="panel-help"><summary role="button" aria-label="要素分解のヘルプ">?</summary><div><p class="invention-data-note">LLMには目的・技術説明・キーワード・入力した原文と要素、選択特許の名称・要約・分類を送り、保存済みの接続設定を使います。手動の整理はLLMへ送信しません。取得していない公報本文・請求項は参照しません。</p></div></details><span class="pill">${plan ? `${plan.elements.length} 要素` : '先行技術の候補を探す'}</span></div>${stage !== 'elements' ? inputHtml : ''}<p id="invention-error" class="invention-error" role="alert" ${inventionWorkbenchState.inventionError ? '' : 'hidden'}>${esc(inventionWorkbenchState.inventionError)}</p>${stage !== 'input' ? reviewHtml : ''}</section>`;
}
function inventionSelectionText(d) {
  return `${d.element_ids.length} 要素を選択 · ${d.strategy === 'combination' ? '選んだ要素をANDで結びます。' : d.strategy === 'alternatives' ? '選んだ要素をORで結びます。' : '1つの要素の観点から検索します。'}`;
}
function inventionPreviewHtml() {
  const p = inventionWorkbenchState.inventionPlanDraft?.preview;
  if (!p) return '';
  return `<div class="invention-preview"><span class="eyebrow">REVIEW SEARCH SCOPE</span><h3>検索条件とNOTの範囲</h3>${queryLibraryTreeHtml(p.query?.boolean_tree)}<pre class="expression">${esc(p.expression || '')}</pre>${p.warnings?.length ? `<ul class="invention-warnings">${p.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}<button id="invention-apply" type="button" class="button primary" ${inventionPlanStale() ? 'disabled' : ''}>この条件で検索式を作成 →</button><p class="caption">採用すると検索式の履歴に追加し、設定済みの検索サービス形式で出力できます。</p></div>`;
}
function inventionPayload() {
  captureResearchDraft();
  captureInventionDraft();
  if (inventionPlanStale())
    throw new Error('発明の入力や調査条件が変わっています。要素分解をやり直してください。');
  const d = inventionPlanState();
  if (!d.element_ids.length) throw new Error('検索する要素を選んでください。');
  if (d.strategy === 'element' && d.element_ids.length !== 1)
    throw new Error('1要素から探す場合は、要素を1つ選んでください。');
  const selected = new Set(d.element_ids),
    exclusions = Object.entries(d.exclusions)
      .filter(([scope, terms]) => (scope === 'all' || selected.has(scope)) && terms.length)
      .map(([scope, terms]) => ({ scope, terms: [...terms] }));
  const elements = d.elements
    .filter((e) => selected.has(e.id))
    .map((e) => ({
      id: e.id,
      perspectives: e.perspectives
        .filter((p) => p.selected)
        .map((p) => ({
          id: p.id,
          terms: [...p.terms],
          classification_keys: p.class_mode === 'text' ? [] : [...p.classification_keys],
          class_mode: p.class_mode,
        })),
    }));
  const empty = elements.find((e) => !e.perspectives.length);
  if (empty) {
    const name = inventionPlan().elements.find((e) => e.id === empty.id)?.name || empty.id;
    throw new Error(`「${name}」で採用する検索観点を1つ以上選んでください。`);
  }
  if (exclusions.length && !d.confirm_exclusions)
    throw new Error('除外語とNOTの適用範囲を確認し、確認欄をチェックしてください。');
  if (inventionNeedsClassConfirmation(elements, d) && !d.confirm_class_intersection)
    throw new Error(
      '採用した観点で語句と分類、または分類同士をANDで結びます。分類の組合せを確認し、確認欄をチェックしてください。'
    );
  return {
    revision: getState().research_workbench.revision,
    plan_id: d.id,
    strategy: d.strategy,
    element_ids: [...d.element_ids],
    elements,
    perspective_joins: Object.fromEntries(d.element_ids.map((id) => [id, d.perspective_joins[id] || 'or'])),
    exclusions,
    confirm_exclusions: d.confirm_exclusions,
    confirm_class_intersection: d.confirm_class_intersection,
  };
}
function inventionNeedsClassConfirmation(elements, d) {
  const counts = elements.map(
    (e) => e.perspectives.filter((p) => p.class_mode !== 'text' && p.classification_keys.length).length
  );
  return (
    elements.some(
      (e, index) =>
        e.perspectives.some((p) => p.class_mode === 'and') ||
        (d.perspective_joins[e.id] === 'and' && counts[index] > 1)
    ) ||
    (d.strategy === 'combination' && counts.filter(Boolean).length > 1)
  );
}
function inventionShowError(error) {
  inventionWorkbenchState.inventionError = error?.message || String(error || '');
  const el = $('#invention-error');
  if (el) {
    el.textContent = inventionWorkbenchState.inventionError;
    el.hidden = !inventionWorkbenchState.inventionError;
  }
}
function inventionRefresh(host = $('#invention-workbench')) {
  if (host) {
    const markup = inventionWorkbenchHtml(
      researchGuidanceState.researchFlowStage,
      researchGuidanceState.researchFlowInputMethod
    );
    const template = document.createElement('template');
    template.innerHTML = markup;
    replaceHtml(host, template.content.firstElementChild.innerHTML, {
      scope: template.content.firstElementChild.dataset.renderScope,
    });
    host.dataset.stage = researchGuidanceState.researchFlowStage;
    bindInventionWorkbench();
  }
}
function inventionRefreshSelection() {
  const d = inventionWorkbenchState.inventionPlanDraft;
  if (!d) return;
  for (const el of $$('[data-invention-use]')) el.checked = d.element_ids.includes(el.value);
  for (const el of $$('[data-invention-card]'))
    el.classList?.toggle('selected', d.element_ids.includes(el.dataset.inventionCard));
  const select = $('#invention-strategy');
  if (select) select.value = d.strategy;
  const help = $('#invention-selection-help');
  if (help) help.textContent = inventionSelectionText(d);
}
async function requestInventionPlan(useLlm) {
  if (
    inventionWorkbenchState.inventionPlanning ||
    researchWorkbenchState.researchPlanning ||
    researchProgressIsRunning()
  )
    throw new Error('要素分解または観点の起案中です。完了を待ってください。');
  captureResearchDraft();
  captureInventionDraft();
  const submitted = structuredClone(inventionDraftInput()),
    brief = structuredClone(researchBrief());
  inventionWorkbenchState.inventionPlanning = true;
  inventionShowError('');
  let progressToken = null,
    failure;
  try {
    researchFlowValidate(useLlm);
    if (submitted.invention_text.length > 20000) throw new Error('発明の説明・請求項は20000文字以内です。');
    if (submitted.manual_elements.length > 8 || submitted.manual_elements.some((t) => t.length > 600))
      throw new Error('手動要素は8件まで、1要素600文字以内です。');
    if (!useLlm && !submitted.manual_elements.length)
      throw new Error('「自分で分けた要素」に1行ずつ要素を入力してください。');
    progressToken = researchProgressStart({
      label: '発明を要素・観点に整理',
      useLlm,
      settings: getState().settings,
    });
    syncResearchPlanningControls();
    researchDraftFlush();
    const result = await api('/research/invention/plan', {
      revision: getState().research_workbench.revision,
      brief,
      ...submitted,
      use_llm: useLlm,
    });
    captureResearchDraft();
    captureInventionDraft();
    const inputEdited = inventionInputKey(inventionDraftInput()) !== inventionInputKey(submitted),
      briefEdited = inventionBriefKey(researchBrief()) !== inventionBriefKey(brief);
    setState(result);
    if (!inputEdited)
      inventionWorkbenchState.inventionInputDraft = inventionInput(
        result.research_workbench?.invention?.input || submitted
      );
    if (!briefEdited) researchWorkbenchState.researchDraft = structuredClone(result.research_workbench.brief);
    inventionWorkbenchState.inventionPlanDraft = null;
    researchDraftRebase();
    researchDraftScheduleSave();
    researchFlowAfterPlan(inputEdited || briefEdited);
    renderResearchWorkbench();
    updateChrome();
    if (inputEdited || briefEdited)
      toast('要素分解中の入力変更を保持しています。変更後の条件で要素分解をやり直してください。');
  } catch (error) {
    failure = error;
    inventionShowError(error);
    throw error;
  } finally {
    inventionWorkbenchState.inventionPlanning = false;
    if (progressToken !== null) researchProgressFinish(progressToken, failure);
    syncResearchPlanningControls();
  }
}
async function previewInvention() {
  inventionShowError('');
  try {
    const payload = inventionPayload(),
      key = JSON.stringify(payload),
      planId = payload.plan_id;
    researchDraftFlush();
    const result = await api('/research/invention/preview', payload);
    if (inventionWorkbenchState.inventionPlanDraft?.id !== planId || inventionPlanStale()) return;
    let current;
    try {
      current = JSON.stringify(inventionPayload());
    } catch {
      return;
    }
    if (current !== key) return;
    inventionWorkbenchState.inventionPlanDraft.preview = result;
    inventionWorkbenchState.inventionPlanDraft.previewPayload = key;
    const host = $('#invention-query-preview');
    if (host) {
      replaceHtml(host, inventionPreviewHtml());
      bindInventionApply();
    }
    researchFlowRevealPreview();
  } catch (error) {
    inventionShowError(error);
    throw error;
  }
}
async function applyInvention() {
  try {
    const payload = inventionPayload(),
      d = inventionWorkbenchState.inventionPlanDraft;
    if (!d.preview || d.previewPayload !== JSON.stringify(payload))
      throw new Error('条件が変わっています。もう一度プレビューしてください。');
    researchDraftFlush();
    const result = await api('/research/invention/apply', {
      ...payload,
      expected_preview_hash: d.preview.preview_hash,
    });
    captureResearchDraft();
    captureInventionDraft();
    setState(result);
    invalidateInventionPreview();
    researchDraftRebase();
    researchDraftScheduleSave();
    updateChrome();
    setTab('explore');
    setStep(1);
    toast('要素と観点から作った検索式を履歴に保存しました。');
  } catch (error) {
    inventionShowError(error);
    throw error;
  }
}
function bindInventionApply() {
  on('#invention-apply', 'click', (e) => busy(e.currentTarget, '作成中…', applyInvention));
}
function bindInventionWorkbench() {
  for (const [id, llm] of [
    ['invention-plan-llm', true],
    ['invention-plan-local', false],
  ])
    on('#' + id, 'click', (e) =>
      busy(e.currentTarget, '要素を整理しています…', () => requestInventionPlan(llm))
    );
  for (const el of $$('#invention-workbench input,#invention-workbench textarea,#invention-workbench select'))
    listen(
      el,
      'input',
      () => {
        inventionShowError('');
        captureInventionDraft();
        const d = inventionWorkbenchState.inventionPlanDraft;
        if (d && el.matches?.('[data-invention-use]') && d.strategy === 'element' && el.checked)
          d.element_ids = [el.value];
        if (d && el.id === 'invention-strategy' && d.strategy === 'element')
          d.element_ids = d.element_ids.slice(0, 1);
        invalidateInventionPreview();
        inventionRefreshSelection();
        if (el.matches?.('[data-invention-class-mode]')) {
          const detail = $('.invention-classifications', el.closest('[data-invention-perspective]'));
          if (detail) {
            detail.open = el.value !== 'text';
            const summary = $('summary', detail);
            if (summary)
              summary.textContent =
                el.value === 'text' ? '分類候補を選ぶ（語句だけでは未使用）' : '分類候補を選ぶ';
          }
        }
      },
      undefined,
      'invention-workbench.js:1'
    );
  for (const el of $$('[data-invention-single]'))
    listen(
      el,
      'click',
      () =>
        busy(el, '確認中…', async () => {
          captureResearchDraft();
          captureInventionDraft();
          const d = inventionPlanState();
          d.strategy = 'element';
          d.element_ids = [el.dataset.inventionSingle];
          inventionRefreshSelection();
          invalidateInventionPreview();
          await previewInvention();
        }),
      undefined,
      'invention-workbench.js:2'
    );
  on('#invention-preview', 'click', (e) => busy(e.currentTarget, '確認中…', previewInvention));
  bindInventionApply();
}

export {
  inventionPlan,
  inventionDraftInput,
  inventionPlanState,
  captureInventionDraft,
  inventionPlanStale,
  invalidateInventionPreview,
  inventionWorkbenchHtml,
  inventionPayload,
  inventionShowError,
  requestInventionPlan,
  previewInvention,
  applyInvention,
  bindInventionWorkbench,
};

export { inventionRefresh as render };
