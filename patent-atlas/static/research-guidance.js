'use strict';

/* A guided view over the existing research and query workflows. */
let researchFlowStage = null;
let researchFlowWorkspace = null;
let researchFlowInputMethod = null;

function researchFlowHasElements(plan) {
  return Array.isArray(plan?.elements) && plan.elements.length > 0;
}
function researchFlowActions(
  workspace,
  stage = 'input',
  tab = 'workbench',
  currentStep = 0,
  entryMode = workspace?.research_workbench?.brief?.entry_mode
) {
  const hasPlan = researchFlowHasElements(workspace?.research_workbench?.invention?.plan);
  const guided = !entryMode || ['target', 'discover'].includes(entryMode);
  const patents = workspace?.patents?.length || 0,
    queries = workspace?.queries?.length || 0;
  return [
    {
      id: 'input',
      label: '調査条件',
      note: '目的・発明を入力',
      active: guided && tab === 'workbench' && stage === 'input',
    },
    {
      id: 'elements',
      label: '要素・観点',
      note: hasPlan ? '検索範囲を選ぶ' : '条件から要素を作成',
      disabled: !hasPlan,
      active: guided && hasPlan && tab === 'workbench' && stage === 'elements',
    },
    {
      id: 'query',
      label: '検索式・CSV',
      note: queries ? `${queries} 件の保存式` : '式の出力・結果の取込',
      active: tab === 'explore' && currentStep === 1,
    },
    {
      id: 'review',
      label: '結果・判定',
      note: patents ? `${patents} 件の特許` : 'CSV取込後に確認',
      disabled: !patents,
      active: tab === 'explore' && currentStep === 2,
    },
    {
      id: 'refine',
      label: '次の式へ',
      note: '要否から改善する',
      disabled: !patents || !queries,
      active: tab === 'explore' && currentStep === 3,
    },
  ];
}
function researchFlowEntryMode(brief, plan) {
  if (['target', 'discover'].includes(brief?.entry_mode)) return brief.entry_mode;
  if (['target', 'discover'].includes(plan?.brief?.entry_mode)) return plan.brief.entry_mode;
  return Array.isArray(brief?.target_ids) && brief.target_ids.length ? 'target' : 'discover';
}
function researchFlowDestination(action) {
  if (action === 'input' || action === 'elements') return { tab: 'workbench', stage: action };
  const steps = { query: 1, review: 2, refine: 3 };
  return Object.hasOwn(steps, action) ? { tab: 'explore', step: steps[action] } : null;
}
function researchFlowPreflight(brief, input, method, settings) {
  const manual =
    Array.isArray(input?.manual_elements) &&
    input.manual_elements.some((value) => typeof value === 'string' && value.trim());
  if (!String(brief?.purpose || '').trim())
    return { field: 'research-purpose', message: 'まず今回の検索目的を入力してください。' };
  if (!['llm', 'manual'].includes(method))
    return {
      field: 'research-input-help',
      message: 'LLMで分解するか、自分で要素を入力するかを選んでください。',
    };
  if (method === 'llm' && !['local', 'openai'].includes(settings?.provider))
    return {
      field: 'research-connect-llm',
      message: 'LLMは未設定です。「接続設定を開く」から設定するか、手動で要素を入力して進めてください。',
    };
  if (method === 'manual' && !manual)
    return {
      field: 'invention-manual-elements',
      message: '要素を1行に1つ入力してください。まずは2〜3個で始められます。',
    };
  if (
    method === 'llm' &&
    !String(brief?.goal || '').trim() &&
    !String(input?.invention_text || '').trim() &&
    !(brief?.target_ids || []).length &&
    !manual
  )
    return {
      field: 'research-goal',
      message: '調べたい発明・技術の説明、ターゲット特許、原文のいずれかを入力してください。',
    };
  return null;
}
function researchFlowValidate(useLlm) {
  const issue = researchFlowPreflight(
    researchBrief(),
    inventionDraftInput(),
    useLlm ? 'llm' : 'manual',
    state.settings
  );
  if (!issue) return;
  researchFlowStage = 'input';
  researchFlowInputMethod = useLlm ? 'llm' : 'manual';
  applyResearchFlowStage();
  const target = $('#' + issue.field);
  target?.focus?.();
  target?.scrollIntoView?.({ block: 'center', behavior: 'auto' });
  throw new Error(issue.message);
}
function researchFlowNavigate(action) {
  const destination = researchFlowDestination(action);
  if (!destination) return;
  const item = researchFlowActions(state, researchFlowStage, activeTab, step).find(
    (row) => row.id === action
  );
  if (item?.disabled) return;
  if (typeof captureResearchDraft === 'function' && activeTab === 'workbench') captureResearchDraft();
  if (destination.stage) {
    const brief = researchBrief(),
      mode = researchFlowEntryMode(brief, inventionPlan());
    const modeChanged = brief.entry_mode !== mode,
      wasWorkbench = activeTab === 'workbench';
    brief.entry_mode = mode;
    researchFlowStage = destination.stage;
    if (typeof researchDraftFlush === 'function') researchDraftFlush();
    setTab('workbench');
    if (modeChanged && wasWorkbench) renderResearchWorkbench();
    applyResearchFlowStage();
  } else {
    if (typeof researchDraftFlush === 'function') researchDraftFlush();
    setTab(destination.tab);
    setStep(destination.step);
  }
  $('#research-flow-header')?.scrollIntoView?.({ block: 'start', behavior: 'auto' });
}
function renderResearchFlowHeader() {
  const host = $('#research-flow-header');
  if (!host || !state) return;
  const actions = researchFlowActions(
    state,
    researchFlowStage || 'input',
    activeTab,
    step,
    researchBrief().entry_mode
  );
  host.innerHTML = `<nav class="research-flow-nav" aria-label="調査の手順">${actions.map((action, index) => `<button type="button" data-flow-action="${action.id}" ${action.disabled ? 'disabled' : ''} ${action.active ? 'aria-current="step"' : ''}><span>${index + 1}</span><b>${esc(action.label)}<small>${esc(action.note)}</small></b></button>`).join('')}</nav><div class="research-flow-save">${typeof researchDraftStatusHtml === 'function' ? researchDraftStatusHtml() : ''}<span>入力 → 範囲を選ぶ → 式を確認 → 結果で改善</span></div>`;
  $$('[data-flow-action]', host).forEach((button) =>
    button.addEventListener('click', () => researchFlowNavigate(button.dataset.flowAction))
  );
}
function researchFlowAfterPlan(edited = false) {
  researchFlowStage = edited ? 'input' : 'elements';
}
function researchWrapDetail(node, title, open = false) {
  if (!node) return null;
  if (node.parentElement?.classList.contains('research-optional')) return node.parentElement;
  const detail = document.createElement('details');
  detail.className = 'research-optional';
  detail.open = open;
  const summary = document.createElement('summary');
  summary.textContent = title;
  detail.append(summary);
  node.replaceWith(detail);
  detail.append(node);
  return detail;
}
function applyResearchInputMethod() {
  const method = researchFlowInputMethod || 'manual';
  const manual = $('#invention-manual-elements')?.closest('label');
  if (manual) manual.hidden = method !== 'manual';
  const llm = $('#invention-plan-llm'),
    local = $('#invention-plan-local');
  if (llm) {
    llm.hidden = method !== 'llm';
    llm.disabled = Boolean(inventionPlanning || researchPlanning || state.settings.provider === 'offline');
  }
  if (local) {
    local.hidden = method !== 'manual';
    local.disabled = Boolean(inventionPlanning || researchPlanning);
  }
  $$('[data-research-input-method]').forEach((button) =>
    button.setAttribute('aria-pressed', String(button.dataset.researchInputMethod === method))
  );
  const connection = $('#research-llm-unavailable');
  if (connection) connection.hidden = method !== 'llm' || state.settings.provider !== 'offline';
  const helper = $('#research-input-help');
  if (helper)
    helper.textContent =
      method === 'manual'
        ? '要素を1行ずつ入力して、検索範囲を選ぶ画面へ進みます。'
        : '上に入力した説明と選択特許を使います。請求項を追加したい場合だけ原文欄を開いてください。';
}
function applyResearchFlowStage() {
  const host = $('#research-workbench');
  if (!host) return;
  const hasPlan = researchFlowHasElements(inventionPlan());
  if (!hasPlan) researchFlowStage = 'input';
  const editing = researchFlowStage === 'elements';
  host.dataset.flowStage = researchFlowStage || 'input';
  const brief = $('.research-layout', host),
    input = $('#invention-input-area'),
    review = $('#invention-review-area');
  if (brief) brief.hidden = editing;
  if (input) input.hidden = editing;
  if (review) review.hidden = !editing;
  for (const selector of ['.research-heading', '.research-entries', '.research-resume']) {
    const node = $(selector, host);
    if (node) node.hidden = editing;
  }
  const summary = $('#research-condition-summary');
  if (summary)
    summary.textContent = `${researchBrief().purpose || '調査目的を入力してください'} · ターゲット ${researchBrief().target_ids.length} 件`;
  const legacy = $('.research-legacy-plan', host);
  if (legacy) legacy.hidden = editing;
  const headline = $('#invention-workbench .invention-heading h2');
  if (headline) headline.textContent = editing ? '要素ごとに、検索範囲を選ぶ。' : '発明の整理方法を選ぶ。';
  const intro = $('#invention-workbench .invention-heading .caption');
  if (intro)
    intro.textContent = editing
      ? '選んだ要素だけを検索します。語句・分類の詳細は必要な要素を開いて編集できます。'
      : '目的と発明の説明を使って、次に検索する要素を整理します。';
  const back = $('#research-edit-input');
  if (back) back.hidden = !editing;
  applyResearchInputMethod();
  renderResearchFlowHeader();
}
function mountResearchGuidance() {
  const host = $('#research-workbench');
  if (!host) return;
  const workspace = state.research_workbench?.id;
  if (researchFlowWorkspace !== workspace) {
    researchFlowWorkspace = workspace;
    researchFlowStage = null;
    researchFlowInputMethod = null;
  }
  const plan = inventionPlan(),
    input = inventionDraftInput();
  if (!researchFlowStage)
    researchFlowStage = researchFlowHasElements(plan) && !inventionPlanStale() ? 'elements' : 'input';
  if (!researchFlowInputMethod)
    researchFlowInputMethod =
      input.manual_elements.length || state.settings.provider === 'offline' ? 'manual' : 'llm';
  const heading = $('.research-heading', host);
  if (heading && !$('.research-resume', host) && (state.queries.length || state.patents.length)) {
    heading.insertAdjacentHTML(
      'afterend',
      `<aside class="research-resume"><div><b>保存した調査から再開</b><span>${state.queries.length} 件の検索式 · ${state.patents.length} 件の特許</span></div><div class="button-row">${state.queries.length ? '<button type="button" class="button small" data-research-resume="query">保存済みの式を開く</button>' : ''}${state.patents.length ? '<button type="button" class="button small" data-research-resume="review">取り込み済み特許を確認</button>' : ''}</div></aside>`
    );
    $$('[data-research-resume]', host).forEach((button) =>
      button.addEventListener('click', () => researchFlowNavigate(button.dataset.researchResume))
    );
  }
  const brief = $('.research-brief', host);
  if (brief && !brief.dataset.researchGuidanceMounted) {
    brief.dataset.researchGuidanceMounted = 'true';
    const goal = $('#research-goal');
    if (goal)
      goal.placeholder = '発明の構成・働き・解決したい課題を説明してください。短い説明から始められます。';
    const keyword = $('#research-keywords')?.closest('label'),
      aspects = $('#research-aspects-open');
    if (keyword) {
      const note = keyword.nextElementSibling;
      const detail = researchWrapDetail(
        keyword,
        'キーワード・観点を補足（任意）',
        Boolean(researchBrief().keywords || researchBrief().user_aspects.length)
      );
      detail.classList.add('research-extra-input');
      if (note?.classList.contains('caption')) detail.append(note);
      if (aspects) detail.append(aspects);
    }
  }
  const section = $('#invention-workbench');
  if (section && !section.dataset.researchGuidanceMounted) {
    section.dataset.researchGuidanceMounted = 'true';
    const source = $('.invention-source-grid', section),
      actions = $('#invention-plan-llm')?.parentElement,
      note = $('.invention-data-note', section);
    const inputArea = document.createElement('div');
    inputArea.id = 'invention-input-area';
    source?.before(inputArea);
    inputArea.innerHTML =
      '<div class="research-input-method" role="group" aria-label="要素の整理方法"><button type="button" data-research-input-method="llm">LLMで分解する</button><button type="button" data-research-input-method="manual">自分で要素を入力する</button></div><p id="research-input-help" class="caption"></p><div id="research-llm-unavailable" class="notice" hidden>LLMは未設定です。手動入力でもこのまま進められます。<button id="research-connect-llm" class="button small" type="button">接続設定を開く</button></div>';
    for (const node of [source, actions, note]) if (node) inputArea.append(node);
    researchWrapDetail(
      $('#invention-text')?.closest('label'),
      '請求項・原文を追加（任意）',
      Boolean(input.invention_text)
    );
    if (note) {
      const detail = researchWrapDetail(note, '送信する内容を確認');
      detail.classList.add('research-data-details');
    }
    const begin = $('#invention-stale-plan');
    if (begin) {
      const review = document.createElement('div');
      review.id = 'invention-review-area';
      const children = [];
      for (let node = begin; node; node = node.nextElementSibling) children.push(node);
      begin.before(review);
      for (const node of children) review.append(node);
      const back = document.createElement('button');
      back.type = 'button';
      back.id = 'research-edit-input';
      back.className = 'button small';
      back.textContent = '← 入力条件を見直す';
      review.prepend(back);
      back.addEventListener('click', () => researchFlowNavigate('input'));
      const condition = document.createElement('p');
      condition.id = 'research-condition-summary';
      condition.className = 'caption';
      back.after(condition);
      for (const card of $$('.invention-element', review)) {
        const content = document.createElement('details');
        content.className = 'research-element-detail';
        content.open = card.classList.contains('selected');
        const summary = document.createElement('summary');
        summary.textContent = '語句・分類・根拠を編集';
        content.append(summary);
        const children = [...card.children].slice(1);
        card.append(content);
        for (const node of children) content.append(node);
      }
      const stale = $('#invention-stale-plan');
      stale?.insertAdjacentHTML(
        'beforeend',
        ' <button type="button" class="button small" id="research-replan-input">入力条件へ戻る</button>'
      );
      on('#research-replan-input', 'click', () => researchFlowNavigate('input'));
    }
    $$('[data-research-input-method]').forEach((button) =>
      button.addEventListener('click', () => {
        captureResearchDraft();
        researchFlowInputMethod = button.dataset.researchInputMethod;
        applyResearchInputMethod();
      })
    );
    on('#research-connect-llm', 'click', () => setTab('settings'));
    $$('[data-invention-use]').forEach((box) =>
      box.addEventListener('input', () => {
        for (const card of $$('.invention-element')) {
          const detail = $('.research-element-detail', card);
          if (detail) detail.open = card.classList.contains('selected');
        }
      })
    );
  }
  if (['target', 'discover'].includes(researchBrief().entry_mode)) applyResearchFlowStage();
  else renderResearchFlowHeader();
  if (typeof renderResearchProgress === 'function') renderResearchProgress();
}
function researchFlowRevealPreview() {
  const node = $('#invention-query-preview');
  if (!node) return;
  node.setAttribute('tabindex', '-1');
  node.scrollIntoView?.({ block: 'start', behavior: 'auto' });
  node.focus?.({ preventScroll: true });
}
