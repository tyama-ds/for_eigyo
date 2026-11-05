'use strict';

/* One visible workflow; legacy panel names are internal destinations, not tabs. */
let researchFlowStage = null;
let researchFlowWorkspace = null;
let researchFlowInputMethod = null;
let researchFlowEditor = 'invention';

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
  const wb = workspace?.research_workbench || {};
  const hasPlan = researchFlowHasElements(wb.invention?.plan) || Boolean(wb.plan);
  const canEdit =
    hasPlan || entryMode === 'tools' || (entryMode === 'examples' && Boolean(wb.library?.length));
  const patents = workspace?.patents?.length || 0,
    queries = workspace?.queries?.length || 0;
  const visible =
    tab === 'workbench'
      ? stage
      : tab === 'explore'
        ? ['elements', 'query', 'review', 'refine'][currentStep]
        : { discovery: 'elements', agent: 'review', orchestration: 'refine' }[tab];
  return [
    { id: 'input', label: '調査条件', note: '目的・発明を入力' },
    {
      id: 'elements',
      label: '要素・観点',
      note: '検索範囲を選ぶ',
      disabled: !canEdit,
      reason: '調査条件から要素・観点を作成するか、過去の式・個別機能を選んでください。',
    },
    { id: 'query', label: '検索式・CSV', note: queries ? `${queries} 件の保存式` : '式の出力・結果の取込' },
    {
      id: 'review',
      label: '結果・判定',
      note: patents ? `${patents} 件の特許` : 'CSV取込後に確認',
      disabled: !patents,
      reason: '「検索式・CSV」で特許一覧を取り込んでください。',
    },
    {
      id: 'refine',
      label: '次の式へ',
      note: '要否から改善する',
      disabled: !patents || !queries,
      reason: '検索式を保存し、特許一覧を取り込んでください。',
    },
  ].map((row) => ({ ...row, active: row.id === visible && (!row.disabled || tab !== 'workbench') }));
}
function researchFlowEntryMode(brief, plan) {
  if (['target', 'discover', 'examples', 'tools'].includes(brief?.entry_mode)) return brief.entry_mode;
  if (['target', 'discover'].includes(plan?.brief?.entry_mode)) return plan.brief.entry_mode;
  return brief?.target_ids?.length ? 'target' : 'discover';
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
  const item = researchFlowActions(
    state,
    researchFlowStage,
    activeTab,
    step,
    researchBrief().entry_mode
  ).find((row) => row.id === action);
  if (item?.disabled) return;
  if (typeof captureResearchDraft === 'function' && activeTab === 'workbench') captureResearchDraft();
  if (destination.stage) researchFlowStage = destination.stage;
  if (typeof researchDraftFlush === 'function') researchDraftFlush();
  const same = activeTab === destination.tab;
  setTab(destination.tab);
  if (destination.stage && same) renderResearchWorkbench(destination.stage);
  if (destination.step !== undefined) setStep(destination.step);
  $('#research-flow-header')?.scrollIntoView?.({ block: 'start', behavior: 'auto' });
}
function researchFlowTools(stage) {
  const all = {
    elements: [
      ['workbench', 0, '発明要素', 'invention'],
      ['workbench', 0, '観点から作る', 'concepts'],
      ['explore', 0, '分類コンステレーション'],
      ['discovery', 0, '探索ラボ'],
    ],
    review: [
      ['explore', 2, '特許マップ・要否判定'],
      ['agent', 0, '判断エージェント'],
    ],
    refine: [
      ['explore', 3, '検索式の改善'],
      ['orchestration', 0, '統括・収束'],
    ],
  };
  const tools = all[stage] || [];
  return stage === 'elements' && researchBrief().entry_mode === 'examples'
    ? [['workbench', 0, '過去の式', 'library'], ...tools]
    : tools;
}
function researchOpenTool(tab, targetStep, editor) {
  if (activeTab === 'workbench' && typeof captureResearchDraft === 'function') captureResearchDraft();
  if (tab === 'workbench') {
    researchFlowStage = 'elements';
    researchFlowEditor = editor || 'invention';
  }
  const same = activeTab === tab;
  setTab(tab);
  if (tab === 'explore') setStep(Number(targetStep));
  else if (tab === 'workbench' && same) renderResearchWorkbench('elements');
  renderResearchFlowHeader();
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
  const stage = actions.find((row) => row.active)?.id;
  host.innerHTML = `<div class="research-flow-nav">${actions
    .map(
      (action, index) =>
        `<button type="button" data-flow-action="${action.id}" ${action.disabled ? `disabled title="${esc(action.reason)}"` : ''} ${action.active ? 'aria-current="step"' : ''}><span>${index + 1}</span><b>${esc(action.label)}<small>${esc(action.note)}</small></b></button>`
    )
    .join('')}</div>
    <div class="research-flow-save">${typeof researchDraftStatusHtml === 'function' ? researchDraftStatusHtml() : ''}</div>
    <div class="research-stage-tools" role="group" aria-label="この段階で使う機能">${researchFlowTools(stage)
      .map(
        ([tab, s, label, editor]) =>
          `<button type="button" class="button small" data-stage-tool="${tab}" data-stage-step="${s}" data-stage-editor="${editor || ''}" aria-pressed="${activeTab === tab && (tab !== 'workbench' || researchFlowEditor === editor)}">${label}</button>`
      )
      .join('')}</div>`;
  $$('[data-flow-action]', host).forEach((button) =>
    button.addEventListener('click', () => researchFlowNavigate(button.dataset.flowAction))
  );
  $$('[data-stage-tool]', host).forEach((button) =>
    button.addEventListener('click', () =>
      researchOpenTool(button.dataset.stageTool, button.dataset.stageStep, button.dataset.stageEditor)
    )
  );
  $('.workspace-heading')?.classList.toggle(
    'is-compact',
    Boolean(state.patents.length || state.queries.length)
  );
}
function researchFlowAfterPlan(edited = false) {
  researchFlowStage = edited ? 'input' : 'elements';
}
function prepareResearchFlow(stage) {
  const workspace = state.research_workbench?.id;
  if (researchFlowWorkspace !== workspace) {
    researchFlowWorkspace = workspace;
    researchFlowStage = null;
    researchFlowInputMethod = null;
    researchFlowEditor = researchBrief().entry_mode === 'examples' ? 'library' : 'invention';
  }
  if (stage) researchFlowStage = stage;
  if (!researchFlowStage)
    researchFlowStage =
      researchFlowHasElements(inventionPlan()) && !inventionPlanStale() ? 'elements' : 'input';
  if (!researchFlowInputMethod)
    researchFlowInputMethod =
      inventionDraftInput().manual_elements.length || state.settings.provider === 'offline'
        ? 'manual'
        : 'llm';
  return researchFlowStage;
}
function applyResearchInputMethod() {
  // Only pending-operation state changes after rendering; stage/method select the markup itself.
  const running = Boolean(inventionPlanning || researchPlanning);
  const llm = $('#invention-plan-llm'),
    local = $('#invention-plan-local');
  if (llm) llm.disabled = running || state.settings.provider === 'offline';
  if (local) local.disabled = running;
}
function applyResearchFlowStage() {
  renderResearchWorkbench(researchFlowStage || 'input');
}
function mountResearchGuidance() {
  $$('[data-research-input-method]').forEach((button) =>
    button.addEventListener('click', () => {
      captureResearchDraft();
      researchFlowInputMethod = button.dataset.researchInputMethod;
      renderResearchWorkbench('input');
    })
  );
  $$('[data-research-resume]').forEach((button) =>
    button.addEventListener('click', () => researchFlowNavigate(button.dataset.researchResume))
  );
  on('#research-connect-llm', 'click', () => setTab('settings'));
  on('#research-edit-input', 'click', () => researchFlowNavigate('input'));
  on('#research-replan-input', 'click', () => researchFlowNavigate('input'));
  applyResearchInputMethod();
  renderResearchFlowHeader();
  if (typeof renderResearchProgress === 'function') renderResearchProgress();
}
function researchFlowRevealPreview() {
  const node = $('#invention-query-preview');
  if (!node) return;
  node.setAttribute('tabindex', '-1');
  node.scrollIntoView?.({ block: 'start', behavior: 'auto' });
  node.focus?.({ preventScroll: true });
}
