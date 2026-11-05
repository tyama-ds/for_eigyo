import {
  classificationTitle,
  replaceHtml,
  listen,
  getState,
  $,
  $$,
  esc,
  html,
  on,
  busy,
  api,
  setState,
  toast,
} from './core.js';
import {
  captureInventionDraft,
  inventionWorkbenchState,
  invalidateInventionPreview,
  inventionWorkbenchHtml,
  inventionShowError,
  bindInventionWorkbench,
} from './invention-workbench.js';
import {
  researchDraftScheduleSave,
  researchDraftFlush,
  researchDraftRebase,
} from './research-draft-store.js';
import {
  researchProgressIsRunning,
  researchProgressStart,
  researchProgressFinish,
} from './research-progress.js';
import {
  applyResearchInputMethod,
  prepareResearchFlow,
  researchGuidanceState,
  researchOpenTool,
  mountResearchGuidance,
} from './research-guidance.js';
import { queryLibraryTreeHtml, queryLibraryHtml, bindQueryLibrary } from './query-library-ui.js';
import { setTab, setStep } from './app.js';
import { appState } from './ui-session.js';
import { uploadFile } from './query-output.js';
import { updateChrome } from './workspace-chrome.js';

export const researchWorkbenchState = {
  researchDraft: null,
  researchPlanDraft: null,
  researchImportPending: false,
  researchPlanning: false,
};

/* Four entry paths use one existing workspace and preserve connection/output settings. */

const researchEntries = [
  ['target', '01', 'ターゲットから作る', '特許の分類と技術要素を出発点に、周辺へ広げる。'],
  ['discover', '02', 'ターゲットを探す', '漠然とした説明を観点に分け、最初の特許を見つける。'],
  ['examples', '03', '過去の式から作る', '検索式例の論理構造を保ち、今回の技術へ調整する。'],
  ['tools', '04', '個別の機能を使う', '分類探索・マップ・要否判定などを直接開く。'],
];
function researchBrief() {
  return (
    researchWorkbenchState.researchDraft ||
    (researchWorkbenchState.researchDraft = structuredClone(
      getState().research_workbench?.brief || {
        entry_mode: 'target',
        purpose: '',
        goal: '',
        keywords: getState().keywords || '',
        user_aspects: [],
        target_ids: [],
      }
    ))
  );
}
function captureResearchDraft() {
  captureInventionDraft();
  const brief = researchBrief();
  for (const [field, id] of [
    ['purpose', 'research-purpose'],
    ['goal', 'research-goal'],
    ['keywords', 'research-keywords'],
  ])
    if ($('#' + id)) brief[field] = $('#' + id).value;
  if ($('#research-target-options'))
    brief.target_ids = $$('.research-target input:checked').map((el) => el.value);
  captureResearchPlan();
  researchDraftScheduleSave();
}
function captureResearchPlan() {
  if (!$('#research-concepts') || !researchWorkbenchState.researchPlanDraft) return;
  researchWorkbenchState.researchPlanDraft.concepts = $$('.research-concept').map((el) => {
    const previous = researchWorkbenchState.researchPlanDraft.concepts.find(
      (c) => c.id === el.dataset.concept
    );
    return {
      ...previous,
      id: el.dataset.concept,
      selected: $('input[data-use]', el).checked,
      role: $('select[data-role]', el).value,
      or_group: $('select[data-or-group]', el)?.value || previous?.or_group || '',
      terms: $('textarea', el)
        .value.split(/\r?\n/)
        .map((t) => t.trim())
        .filter(Boolean),
    };
  });
  researchWorkbenchState.researchPlanDraft.classification_keys = $$('.research-class input:checked').map(
    (el) => el.value
  );
  researchWorkbenchState.researchPlanDraft.acknowledge_unmapped = $('#research-unmapped-ack')?.checked;
  researchDraftScheduleSave();
}
function researchPlanningActive() {
  return (
    researchWorkbenchState.researchPlanning ||
    inventionWorkbenchState.inventionPlanning ||
    researchProgressIsRunning()
  );
}
function syncResearchPlanningControls() {
  for (const id of ['research-plan-llm', 'research-plan-local']) {
    const button = $('#' + id);
    if (button) button.disabled = researchPlanningActive();
  }
  applyResearchInputMethod();
}
function researchPlanState(plan) {
  if (!plan) return null;
  if (researchWorkbenchState.researchPlanDraft?.id !== plan.id)
    researchWorkbenchState.researchPlanDraft = {
      id: plan.id,
      concepts: plan.concepts.map((c) => ({
        ...c,
        selected: c.role === 'required',
        role: c.role === 'exclude' ? 'exclude' : 'required',
        or_group: c.or_group || researchOwnGroup(c, plan),
        terms: [...c.terms],
      })),
      classification_keys: [],
      preview: null,
    };
  return researchWorkbenchState.researchPlanDraft;
}
function researchOwnGroup(concept, plan) {
  const reserved = new Set(plan.concepts.map((c) => c.or_group).filter(Boolean));
  let key = `ui_${plan.concepts.indexOf(concept) + 1}`;
  while (reserved.has(key)) key += '_s';
  return key;
}
function researchConceptGroupLocked(concept, plan) {
  if (concept.locked_and || concept.role === 'exclude') return true;
  // Older saved plans predate locked_and. Preserve a user's explicit must-have aspect.
  return (plan.brief?.user_aspects || getState().research_workbench?.brief?.user_aspects || []).some(
    (aspect) =>
      /^(必須|required)\s*[:：]/i.test(aspect) &&
      (aspect === concept.name || aspect.replace(/^(必須|required)\s*[:：]\s*/i, '') === concept.name)
  );
}
function researchConceptGroup(concept, draft, plan) {
  return researchConceptGroupLocked(concept, plan)
    ? `locked:${concept.id}`
    : draft.or_group || researchOwnGroup(concept, plan);
}
function researchGroupOptions(plan, draft, current) {
  const source = plan.concepts.find((c) => c.id === current.id),
    own = researchOwnGroup(source, plan);
  if (researchConceptGroupLocked(source, plan) || current.role === 'exclude') {
    const label =
      current.role === 'exclude'
        ? '除外は独立した条件'
        : source.role === 'exclude'
          ? '独立条件 · グループの結合なし'
          : '明示した必須条件 · 独立';
    return html`<option value="${esc(current.or_group || own)}">${label}</option>`;
  }
  const choices = new Map();
  for (const c of plan.concepts) {
    const d = draft.concepts.find((x) => x.id === c.id);
    if (d.role === 'exclude' || researchConceptGroupLocked(c, plan)) continue;
    const key = researchConceptGroup(c, d, plan);
    if (!choices.has(key)) choices.set(key, []);
    choices.get(key).push(c.name);
  }
  const labels = new Map([...choices].map(([key, names]) => [key, `条件 · ${names.join(' / ')}`]));
  const separate = !choices.has(own)
    ? own
    : plan.concepts.map((c) => researchOwnGroup(c, plan)).find((key) => !choices.has(key));
  if (separate && choices.get(current.or_group)?.length > 1)
    labels.set(separate, 'この観点だけの独立条件にする');
  return [...labels]
    .map(([key, label]) => researchGroupOptionHtml(key, label, current.or_group === key))
    .join('');
}
function researchGroupOptionHtml(key, label, selected) {
  return html`<option value="${esc(key)}" ${selected ? 'selected' : ''}>${esc(label)}</option>`;
}
function researchLogicTermsHtml(c) {
  const terms = c.terms.length
    ? c.terms.map((t) => html`<span>${esc(t)}</span>`).join('<b class="research-logic-or">OR</b>')
    : '<i>語句を入力</i>';
  return html`<span class="research-logic-terms">${terms}</span>`;
}
function researchLogicSetHtml(members, index) {
  const concepts = members
    .map(
      (c) => html`<div class="research-logic-concept"><span class="research-logic-name">${esc(c.name)}</span>
        ${researchLogicTermsHtml(c)}</div>`
    )
    .join('<b class="research-logic-union">OR · 和集合</b>');
  return html`<div class="research-logic-set">
    <small>条件${index + 1} · ${members.length > 1 ? 'この中のどれか' : '語句のいずれか'}</small>${concepts}</div>`;
}
function researchLogicHtml(plan, draft) {
  const groups = new Map(),
    excluded = [];
  for (const c of plan.concepts) {
    const d = draft.concepts.find((x) => x.id === c.id);
    if (!d.selected) continue;
    if (d.role === 'exclude') {
      excluded.push(d);
      continue;
    }
    const key = researchConceptGroup(c, d, plan);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ ...d, name: c.name });
  }
  const groupHtml = [...groups.values()]
    .map(researchLogicSetHtml)
    .join('<b class="research-logic-intersection">AND<br><small>積集合</small></b>');
  const excludeHtml = excluded.length
    ? html`<div class="research-logic-exclude"><b>NOT · 除外</b>
        ${excluded.map(researchLogicTermsHtml).join('<b class="research-logic-or">OR</b>')}</div>`
    : '';
  return html`<div class="research-logic-heading"><b>現在の語句の組合せ</b>
    <span>同じ条件に入れるとOR、別の条件にするとAND</span></div>
    <div class="research-logic-equation">
    ${groupHtml || '<p class="caption">含める観点を選ぶと、組合せを確認できます。</p>'}</div>${excludeHtml}
    ${draft.classification_keys.length ? '<p class="caption">選択した分類の和集合を、この語句の式にANDで加えます。</p>' : ''}`;
}
function refreshResearchLogic() {
  const plan = getState().research_workbench?.plan,
    draft = researchWorkbenchState.researchPlanDraft;
  if (!plan || !draft) return;
  const host = $('#research-logic');
  if (host) replaceHtml(host, researchLogicHtml(plan, draft));
  for (const element of $$('.research-concept')) {
    const current = draft.concepts.find((c) => c.id === element.dataset.concept),
      source = plan.concepts.find((c) => c.id === element.dataset.concept),
      select = $('select[data-or-group]', element);
    if (select) {
      replaceHtml(select, researchGroupOptions(plan, draft, current));
      select.disabled = researchConceptGroupLocked(source, plan) || current.role === 'exclude';
    }
    const help = $('.research-group-help', element);
    if (help)
      help.textContent =
        '同じ条件を選んだ観点同士はOR、別の条件とはAND。' +
        (researchConceptGroupLocked(source, plan) && source.role !== 'exclude'
          ? '明示した必須観点は独立条件を保持します。'
          : source.group_reason
            ? ' 起案時のまとめ方：' + source.group_reason
            : '');
  }
}
function researchTargetNoteHtml(missing, derived) {
  if (!missing.length) return '';
  const derivedNote = derived.length
    ? `うち ${derived.length} 件にはFIまたはFタームがあり、対応が確認できる範囲からIPCの探索候補を提示します。`
    : '';
  return html`<p class="research-target-provenance">${missing.length} 件は取り込みデータの公報IPC欄が未収録です。
    ${derivedNote}候補と公報の付与IPCは区別します。原本にIPCがないという意味ではありません。</p>`;
}
function researchTargetRowHtml(p, checked, hasValue) {
  const extra = [
    p.fi ? ' · FI ' + esc(p.fi) : '',
    p.fterm ? ' · Fターム ' + esc(p.fterm) : '',
    p.cpc ? ' · CPC ' + esc(p.cpc) : '',
  ];
  return html`<label class="research-target" data-search="${esc((p.id + ' ' + p.title).toLocaleLowerCase())}">
    <input type="checkbox" value="${esc(p.id)}" ${checked ? 'checked' : ''}><span><b>${esc(p.title)}</b>
    <small>${esc(p.id)} · 公報IPC：${hasValue(p.ipc) ? esc(p.ipc) : '未収録'}${extra}</small></span></label>`;
}
function researchTargetOptions() {
  const ids = new Set(researchBrief().target_ids),
    references = new Map((getState().research_workbench?.target_patents || []).map((p) => [p.id, p]));
  for (const p of getState().patents) {
    const old = references.get(p.id) || {},
      merged = { ...p };
    for (const field of ['title', 'abstract', 'ipc', 'fi', 'fterm', 'cpc'])
      if (!merged[field] || (Array.isArray(merged[field]) && !merged[field].length))
        merged[field] = old[field] || merged[field];
    references.set(p.id, merged);
  }
  const rows = [...references.values()].sort((a, b) => Number(ids.has(b.id)) - Number(ids.has(a.id)));
  const hasValue = (value) =>
    Array.isArray(value) ? value.some((v) => String(v).trim()) : Boolean(String(value || '').trim());
  const missing = rows.filter((p) => !hasValue(p.ipc)),
    derived = missing.filter((p) => hasValue(p.fi) || hasValue(p.fterm));
  return (
    researchTargetNoteHtml(missing, derived) +
    (rows.map((p) => researchTargetRowHtml(p, ids.has(p.id), hasValue)).join('') ||
      '<p class="caption">CSVを追加するか、ターゲット特許の情報を入力してください。</p>')
  );
}
function researchConceptHtml(c, plan, draft) {
  const d = draft.concepts.find((x) => x.id === c.id),
    locked = researchConceptGroupLocked(c, plan) || d.role === 'exclude';
  const help =
    researchConceptGroupLocked(c, plan) && c.role !== 'exclude'
      ? '明示した必須観点は独立条件を保持します。'
      : esc(c.group_reason || '');
  const evidence = c.evidence_ids?.length ? ' / 根拠：' + c.evidence_ids.map(esc).join('、') : '';
  return html`<article class="research-concept" data-concept="${esc(c.id)}"><div class="panel-heading">
    <label class="check"><input data-use type="checkbox" ${d.selected ? 'checked' : ''}>
    <b>${esc(c.name)}</b></label>
    <select data-role aria-label="${esc(c.name)} の条件">
    <option value="required" ${d.role === 'required' ? 'selected' : ''}>含める</option>
    <option value="exclude" ${d.role === 'exclude' ? 'selected' : ''}>除外 · NOT</option></select></div>
    <label class="research-group-choice">和集合グループ<select data-or-group aria-label="${esc(c.name)} の和集合グループ"
    ${locked ? 'disabled' : ''}>${researchGroupOptions(plan, draft, d)}</select></label>
    <p class="caption research-group-help">同じ条件を選んだ観点同士はOR、別の条件とはAND。${help}</p>
    <label>語句 · 1行に1語句（この欄の語はOR）
    <textarea rows="3" maxlength="5000">${esc(d.terms.join('\n'))}</textarea></label>
    <p class="caption">${esc(c.reason)}${evidence}</p>${researchExpansionHtml(c.abstract_terms)}</article>`;
}
function researchAbstractButtonHtml(t) {
  return html`<button class="button small" data-abstract="${esc(t)}" type="button">＋ ${esc(t)}</button>`;
}
function researchExpansionHtml(terms) {
  if (!terms?.length) return '';
  return html`<div class="research-expansion"><span>機能・概念を広げる</span>
    ${terms.map(researchAbstractButtonHtml)}</div>`;
}
function researchUnmappedHtml(plan, draft) {
  if (!plan.unmapped_keywords?.length) return '';
  return html`<div class="notice">観点に未割当の入力語：${plan.unmapped_keywords.map(esc).join('、')}。
    上の語句欄に割り当ててください。
    <label class="check"><input id="research-unmapped-ack" type="checkbox"
    ${draft.acknowledge_unmapped ? 'checked' : ''}>未割当の語を今回の式から省略することを確認した</label></div>`;
}
function researchClassHtml(c, selected) {
  const tag = researchClassificationProvenance(c),
    hypothesis = tag.includes('仮説'),
    origins = c.origins || [];
  return html`<label class="research-class ${hypothesis ? 'hypothesis' : ''}">
    <input type="checkbox" value="${esc(c.key)}"
    ${selected.has(c.key) ? 'checked' : ''} ${c.selectable ? '' : 'disabled'}>
    <span><small>${tag} · ${c.verified ? '分類表に存在' : '存在未確認'}</small><b>${esc(c.kind)} ${esc(c.code)}</b>
    <span>${esc(classificationTitle(c))}</span>
    <small>${esc(c.reason || '')}${origins.map((o) => (o.patent_id ? ' / ' + esc(o.patent_id) : ''))}</small>
    </span></label>`;
}
function researchClassificationsHtml(plan, selected) {
  return html`<details class="research-classifications" open>
    <summary>分類の根拠を比較する · ${plan.classification_candidates.length} 件</summary>
    <p class="caption">実付与・対応付け・上位への拡張・観点からの仮説を区別しています。分類は任意です。
    選択すると分類群のORを、観点の式にANDで加えます。</p>
    <div class="research-class-grid">
    ${plan.classification_candidates.map((c) => researchClassHtml(c, selected))}</div></details>`;
}
function researchNotesHtml(plan) {
  const notes = [...(plan.questions || []), ...(plan.classification_notes || [])];
  if (!notes.length) return '';
  return html`<details class="research-notes"><summary>確認する点・根拠の限界</summary>
    <ul>${notes.map((t) => html`<li>${esc(t)}</li>`)}</ul></details>`;
}
function researchPlanHtml(plan) {
  if (!plan)
    return html`<div class="research-plan-empty"><span>◎</span><h3>観点はまだありません</h3>
      <p>条件を整理すると、観点・語句・根拠となる分類をここで比較できます。</p></div>`;
  const draft = researchPlanState(plan),
    selected = new Set(draft.classification_keys);
  return html`<div class="panel-heading"><div><h2>観点を組み立てる</h2></div>
    <span class="pill">${plan.method === 'llm' ? 'LLMによる案' : '入力の整理'}</span></div>
    <p class="muted">${esc(plan.summary)}</p>
    <p class="caption">採用する観点にチェック。代替表現・近い技術は同じ条件にまとめてOR（和集合）、
    併せて満たす別の観点はAND（積集合）にします。一般語・専門語だけで区別せず、意味に合わせて調整してください。
    除外は明示的に選んだ観点だけです。</p>
    <div id="research-logic" class="research-logic" aria-live="polite">${researchLogicHtml(plan, draft)}</div>
    <div id="research-concepts">${plan.concepts.map((c) => researchConceptHtml(c, plan, draft))}</div>
    ${researchUnmappedHtml(plan, draft)}
    ${researchClassificationsHtml(plan, selected)}
    ${researchNotesHtml(plan)}
    <div class="button-row"><button class="button primary" id="research-preview">検索条件をプレビュー →</button></div>
    <div id="research-query-preview" aria-live="polite">${researchPreviewHtml()}</div>`;
}
function researchPreviewHtml() {
  const p = researchWorkbenchState.researchPlanDraft?.preview;
  return p
    ? html`<div class="research-preview"><h3>今回の検索条件</h3>${queryLibraryTreeHtml(p.query.boolean_tree)}
        <pre class="expression">${esc(p.expression)}</pre>
        <p class="caption">採用後、保存済みの検索サービス形式で出力します。分類が未確認の場合は出力画面で確認できます。</p>
        <button class="button primary" id="research-apply">この条件で検索式を作成 →</button></div>`
    : '';
}
function invalidateResearchPreview() {
  if (researchWorkbenchState.researchPlanDraft) researchWorkbenchState.researchPlanDraft.preview = null;
  if ($('#research-query-preview')) replaceHtml($('#research-query-preview'), '');
  const notice = $('#research-stale-plan');
  if (notice) notice.hidden = !(getState().research_workbench?.plan && researchBriefChanged());
  invalidateInventionPreview();
}
function researchPlanPayload() {
  captureResearchDraft();
  if (researchBriefChanged())
    throw new Error('入力条件が観点案の作成後に変わっています。観点をもう一度起案してください。');
  captureResearchPlan();
  const plan = getState().research_workbench.plan;
  return {
    revision: getState().research_workbench.revision,
    plan_id: researchWorkbenchState.researchPlanDraft.id,
    concepts: researchWorkbenchState.researchPlanDraft.concepts
      .filter((c) => c.selected)
      .map(({ id, role, terms, or_group }) => ({
        id,
        role,
        terms,
        or_group:
          role === 'exclude' ||
          researchConceptGroupLocked(
            plan.concepts.find((c) => c.id === id),
            plan
          )
            ? ''
            : or_group,
      })),
    classification_keys: researchWorkbenchState.researchPlanDraft.classification_keys,
    acknowledge_unmapped: researchWorkbenchState.researchPlanDraft.acknowledge_unmapped,
  };
}
function researchToolHtml([tab, s, title, description]) {
  return html`<button class="panel research-tool" data-open-tab="${tab}" data-open-step="${s}"><span>↗</span>
    <h3>${title}</h3><p>${description}</p></button>`;
}
function researchToolsHtml() {
  const tools = [
    ['explore', 0, '分類を探索', 'キーワード・分類コンステレーション'],
    ['explore', 1, '検索式とCSV', 'DB別の出力・検索結果の取込'],
    ['explore', 2, '特許マップ・要否判定', 'PCA / t-SNE / UMAP、学習と境界の確認'],
    ['explore', 3, '検索式を改善', '必要・不要の語句と分類を反映'],
    ['discovery', 0, '探索ラボ', '分類を拡張・CSVまたは設定済みOPSで探索'],
    ['agent', 0, '判断エージェント', '指定した基準で特許の要否を判定'],
    ['orchestration', 0, '統括・収束を見る', '反復の進行と収束履歴'],
    ['settings', 0, '表示・接続設定', '既存のLLM・プロキシ・表示設定'],
  ];
  return html`<div class="research-tools">${tools.map(researchToolHtml)}</div>`;
}
function researchPurposeFieldHtml(brief) {
  return html`<label>今回の検索目的 <span class="required">必須</span>
    <input id="research-purpose" maxlength="300" value="${esc(brief.purpose)}" list="research-purpose-options"
    placeholder="例：周辺技術の把握、先行技術調査">
    <datalist id="research-purpose-options"><option value="技術動向・周辺技術の把握">
    <option value="新規性・進歩性の検討に向けた先行技術調査"><option value="競合の技術領域を把握する">
    <option value="特定の構成・機能に関連する特許を集める"></datalist></label>`;
}
function researchExtraInputHtml(brief) {
  const aspects = brief.user_aspects.length ? '· ' + brief.user_aspects.length + ' 件' : '（任意）';
  return html`<details class="research-optional research-extra-input"
    ${brief.keywords || brief.user_aspects.length ? 'open' : ''}>
    <summary>キーワード・観点を補足（任意）</summary>
    <label>手掛かりのキーワード<input id="research-keywords" maxlength="2000" value="${esc(brief.keywords)}"
    placeholder='例：全固体電池 界面抵抗'></label>
    <p class="caption">語句は空白区切り。英語の句は二重引用符で囲みます。</p>
    <button class="button subtle" id="research-aspects-open">想定している観点を入力 ${aspects} ↗</button></details>`;
}
function researchTargetsHtml(brief, mode) {
  return html`<details class="research-targets" ${mode === 'target' ? 'open' : ''}>
    <summary>ターゲット特許を指定 · ${brief.target_ids.length} 件</summary>
    <p class="caption">名称・要約と、取り込んだIPC / FI / Fターム / CPCを参照します。最大20件。
    FIのIPC部・Fタームの対応候補は根拠を区別します。</p>
    <div class="button-row"><button class="button small" id="research-upload">CSVを追加</button>
    <button class="button small" id="research-target-add">特許情報を入力</button></div>
    <input id="research-csv-file" type="file" accept=".csv,.tsv" hidden>
    <input id="research-target-filter" type="search" placeholder="番号・名称で候補を絞る" aria-label="ターゲット候補を検索">
    <div id="research-target-options">${researchTargetOptions()}</div>
    ${mode === 'discover' ? '<button class="button small" id="research-promote">選んだ特許から作る →</button>' : ''}
    </details>`;
}
function researchPlanActionsHtml() {
  return html`<div class="research-plan-actions">
    <button class="button primary" id="research-plan-llm">LLMで観点・広げ方を起案 ↗</button>
    <button class="button" id="research-plan-local">入力をそのまま整理</button>
    <p class="caption">LLMには目的・観点と、選択した特許の名称・要約・分類を送ります。保存済みの接続設定を使います。
    「入力を整理」は通信しません。</p></div>`;
}
function researchBriefHtml(brief, mode, concepts = false) {
  const discoverNote =
    '<div class="notice">おすすめは併用方式。広いKWで実在特許を探し、仮のIPCも比較します。' +
    '最初から分類で絞りすぎず、見つけた特許をターゲットにして深めます。</div>';
  return html`<div class="research-layout research-invention-brief"><div class="panel research-brief">
    <h2>調査の目的と技術</h2>${mode === 'discover' ? discoverNote : ''}${researchPurposeFieldHtml(brief)}
    <label>調べたい技術・解決したいこと<textarea id="research-goal" rows="4" maxlength="5000"
    placeholder="発明の構成・働き・解決したい課題を説明してください。短い説明から始められます。">${esc(brief.goal)}</textarea></label>
    ${researchExtraInputHtml(brief)}
    ${researchTargetsHtml(brief, mode)}
    ${concepts ? researchPlanActionsHtml() : ''}<div id="research-error" role="alert"></div></div></div>`;
}
function researchModeHtml([id, , title, description], mode) {
  return html`<label title="${esc(description)}">
    <input type="radio" name="research-mode" data-research-mode="${id}" value="${id}"
    ${mode === id ? 'checked' : ''}>
    <span>${title}</span></label>`;
}
function researchResumeButtonHtml(target, label) {
  return html`<button type="button" class="button small" data-research-resume="${target}">${label}</button>`;
}
function researchResumeHtml() {
  const queries = getState().queries.length,
    patents = getState().patents.length;
  if (!queries && !patents) return '';
  return html`<aside class="research-resume"><div><b>保存した調査から再開</b>
    <span>${queries} 件の検索式 · ${patents} 件の特許</span></div><div class="button-row">
    ${queries ? researchResumeButtonHtml('query', '保存済みの式を開く') : ''}
    ${patents ? researchResumeButtonHtml('review', '取り込み済み特許を確認') : ''}</div></aside>`;
}
function researchStalePlanHtml(wb) {
  return html`<div id="research-stale-plan" class="notice" role="status"
    ${wb.plan && researchBriefChanged() ? '' : 'hidden'}>表示している内容は前回の案です。
    入力条件が変わったため、観点をもう一度起案してからプレビュー・採用してください。</div>`;
}
function researchStageContentHtml(stage, mode, wb, brief, editor, method) {
  const showLibrary = mode === 'examples' && (stage === 'input' || editor === 'library');
  if (showLibrary) return queryLibraryHtml(wb);
  if (stage === 'input' && mode === 'tools') return researchToolsHtml();
  if (stage === 'elements' && editor === 'concepts')
    return html`${researchBriefHtml(brief, mode, true)}<div class="panel research-plan">
      ${researchStalePlanHtml(wb)}${researchPlanHtml(wb.plan)}</div>`;
  return (stage === 'input' ? researchBriefHtml(brief, mode) : '') + inventionWorkbenchHtml(stage, method);
}
function renderResearchWorkbench(stage, host = $('#research-workbench'), snapshot = getState()) {
  if (!host) return;
  stage = prepareResearchFlow(stage);
  const wb = snapshot.research_workbench || {},
    brief = researchBrief(),
    mode = brief.entry_mode;
  const editor = researchGuidanceState.researchFlowEditor;
  const method = researchGuidanceState.researchFlowInputMethod;
  const modes = html`<fieldset class="research-modes"><legend>調査の開始方法</legend>
    ${researchEntries.map((entry) => researchModeHtml(entry, mode))}</fieldset>`;
  const showLibrary = mode === 'examples' && (stage === 'input' || editor === 'library');
  const heading = '<div class="research-heading"><h2>調査条件</h2></div>';
  const content = researchStageContentHtml(stage, mode, wb, brief, editor, method);
  replaceHtml(host, html`${stage === 'input' ? heading + modes + researchResumeHtml() : ''}${content}`);
  if (host.dataset) host.dataset.flowStage = stage;
  bindResearchBrief();
  if (showLibrary) {
    bindQueryLibrary();
    mountResearchGuidance();
    return;
  }
  if (mode === 'tools' && stage === 'input') {
    mountResearchGuidance();
    return;
  }
  bindResearchTargets();
  bindResearchPlan();
  bindInventionWorkbench();
  syncResearchPlanningControls();
  mountResearchGuidance();
}
async function previewResearchPlan() {
  if ($('#research-error')) $('#research-error').textContent = '';
  try {
    const body = researchPlanPayload(),
      key = JSON.stringify(body),
      planId = body.plan_id;
    researchDraftFlush();
    const result = await api('/research/preview', body);
    if (researchWorkbenchState.researchPlanDraft?.id !== planId || researchBriefChanged()) return;
    let current;
    try {
      current = JSON.stringify(researchPlanPayload());
    } catch {
      return;
    }
    if (current !== key) return;
    researchWorkbenchState.researchPlanDraft.preview = result;
    const host = $('#research-query-preview');
    if (host) {
      replaceHtml(host, researchPreviewHtml());
      bindResearchApply();
    }
  } catch (error) {
    if ($('#research-error')) $('#research-error').textContent = error.message;
    throw error;
  }
}
function bindResearchApply() {
  on('#research-apply', 'click', (e) =>
    busy(e.currentTarget, '作成中…', async () => {
      const preview = researchWorkbenchState.researchPlanDraft.preview,
        body = researchPlanPayload(),
        submitted = JSON.stringify(researchBrief());
      if (!preview) throw new Error('最新の検索条件をプレビューしてから採用してください。');
      researchDraftFlush();
      const result = await api('/research/apply', { ...body, expected_preview_hash: preview.preview_hash });
      captureResearchDraft();
      const edited = JSON.stringify(researchBrief()) !== submitted;
      setState(result);
      if (!edited)
        researchWorkbenchState.researchDraft = structuredClone(getState().research_workbench.brief);
      if (researchWorkbenchState.researchPlanDraft) researchWorkbenchState.researchPlanDraft.preview = null;
      researchDraftRebase();
      researchDraftScheduleSave();
      updateChrome();
      setTab('explore');
      setStep(1);
      toast('検索条件を履歴に保存しました。検索サービスの形式で出力します。');
    })
  );
}
function initResearchWorkbench() {
  on('#research-aspects-close', 'click', () => $('#research-aspects-dialog').close());
  on('#research-aspects-form', 'submit', (e) => {
    e.preventDefault();
    const values = $('#research-aspects-text')
      .value.split(/\r?\n/)
      .map((t) => t.trim())
      .filter(Boolean);
    if (values.length > 8 || values.some((t) => t.length > 160))
      throw new Error('観点は8件まで、1件160文字以内です。');
    researchBrief().user_aspects = values;
    invalidateResearchPreview();
    researchDraftScheduleSave();
    $('#research-aspects-dialog').close();
    renderResearchWorkbench();
  });
  on('#research-target-close', 'click', () => $('#research-target-dialog').close());
  on('#research-target-form', 'submit', async (e) => {
    e.preventDefault();
    captureResearchDraft();
    const values = Object.fromEntries(new FormData(e.currentTarget));
    values.target_ids = [...researchBrief().target_ids];
    await busy($('#research-target-save'), '追加中…', async () => {
      researchDraftFlush();
      const result = await api('/research/targets/add', values);
      captureResearchDraft();
      const selected = [...researchBrief().target_ids];
      setState(result);
      researchBrief().target_ids = [
        ...new Set([...selected, getState().research_workbench.last_added_target_id || values.id]),
      ];
      invalidateResearchPreview();
      $('#research-target-dialog').close();
      e.target.reset();
      renderResearchWorkbench();
      researchDraftRebase();
      researchDraftScheduleSave();
      updateChrome();
    });
  });
  renderResearchWorkbench();
}

function researchClassificationProvenance(c) {
  const origins = [...(c.classification_origins || []), ...(c.origins || [])],
    types = new Set(origins.map((o) => o.type));
  const tags = [];
  if (types.has('patent'))
    tags.push(
      origins.some((o) => o.type === 'patent' && o.field === 'ipc')
        ? '公報のIPC欄から抽出'
        : '公報の分類欄から抽出'
    );
  if (types.has('fi_to_ipc'))
    tags.push(
      origins.some((o) => o.type === 'fi_to_ipc' && o.method === 'subclass_fallback')
        ? 'FIからのIPC候補（上位へ拡張・付与IPCとは別）'
        : 'FIからのIPC候補（付与IPCとは別）'
    );
  if (types.has('fterm_theme_to_ipc')) tags.push('Fターム対応範囲のIPC候補（付与IPCとは別）');
  if (types.has('target_parent')) tags.push('上位へ拡張');
  return tags.join(' / ') || (c.llm_proposed ? 'LLMからの仮説' : '観点からの仮説');
}
function researchBriefChanged() {
  const plan = getState().research_workbench?.plan;
  if (!plan) return true;
  const brief = researchBrief(),
    saved = plan.brief || getState().research_workbench.brief;
  return (
    ['purpose', 'goal', 'keywords', 'entry_mode'].some(
      (key) => String(brief[key] || '').trim() !== String(saved[key] || '').trim()
    ) ||
    ['target_ids', 'user_aspects'].some(
      (key) =>
        JSON.stringify([...(brief[key] || [])].sort()) !== JSON.stringify([...(saved[key] || [])].sort())
    )
  );
}

async function requestResearchPlan(useLlm) {
  if (researchPlanningActive()) throw new Error('観点または要素を起案中です。完了を待ってください。');
  captureResearchDraft();
  const submitted = structuredClone(researchBrief());
  researchWorkbenchState.researchPlanning = true;
  let progressToken = null,
    failure;
  if ($('#research-error')) $('#research-error').textContent = '';
  try {
    progressToken = researchProgressStart({
      label: '観点・広げ方を作成',
      useLlm,
      settings: getState().settings,
    });
    syncResearchPlanningControls();
    researchDraftFlush();
    const result = await api('/research/plan', {
      revision: getState().research_workbench.revision,
      brief: submitted,
      use_llm: useLlm,
    });
    captureResearchDraft();
    const edited = JSON.stringify(researchBrief()) !== JSON.stringify(submitted);
    setState(result);
    if (!edited) researchWorkbenchState.researchDraft = structuredClone(getState().research_workbench.brief);
    researchWorkbenchState.researchPlanDraft = null;
    researchDraftRebase();
    researchDraftScheduleSave();
    if (!edited) researchGuidanceState.researchFlowEditor = 'concepts';
    if (appState.activeTab === 'workbench') renderResearchWorkbench(edited ? 'input' : 'elements');
    updateChrome();
    if (edited) toast('起案中の入力変更は保持しています。新しい条件で観点を再作成してください。');
  } catch (error) {
    failure = error;
    if ($('#research-error')) $('#research-error').textContent = error.message;
    throw error;
  } finally {
    researchWorkbenchState.researchPlanning = false;
    if (progressToken !== null) researchProgressFinish(progressToken, failure);
    syncResearchPlanningControls();
  }
}

function bindResearchBrief() {
  $$('.research-brief input,.research-brief textarea').forEach((el) => {
    if (el.id === 'research-target-filter') return;
    listen(
      el,
      'input',
      () => {
        inventionShowError('');
        captureResearchDraft();
        invalidateResearchPreview();
      },
      undefined,
      'research-workbench.js:1'
    );
  });
  $$('[data-research-mode]').forEach((el) =>
    listen(
      el,
      'click',
      () => {
        captureResearchDraft();
        researchWorkbenchState.researchDraft.entry_mode = el.dataset.researchMode;
        researchGuidanceState.researchFlowEditor =
          researchWorkbenchState.researchDraft.entry_mode === 'examples' ? 'library' : 'invention';
        researchGuidanceState.researchFlowStage = 'input';
        invalidateResearchPreview();
        researchDraftScheduleSave();
        renderResearchWorkbench();
      },
      undefined,
      'research-workbench.js:2'
    )
  );
  $$('[data-open-tab]').forEach((el) =>
    listen(
      el,
      'click',
      () => {
        researchOpenTool(el.dataset.openTab, el.dataset.openStep, el.dataset.openEditor);
      },
      undefined,
      'research-workbench.js:3'
    )
  );
}
function bindResearchTargets() {
  on('#research-aspects-open', 'click', () => {
    captureResearchDraft();
    $('#research-aspects-text').value = researchWorkbenchState.researchDraft.user_aspects.join('\n');
    $('#research-aspects-dialog').showModal();
  });
  on('#research-target-filter', 'input', (e) => {
    $$('.research-target').forEach(
      (el) => (el.hidden = !el.dataset.search.includes(e.target.value.toLocaleLowerCase()))
    );
  });
  on('#research-upload', 'click', () => $('#research-csv-file').click());
  on('#research-csv-file', 'change', async (e) => {
    captureResearchDraft();
    researchDraftFlush();
    researchWorkbenchState.researchImportPending = true;
    appState.pendingImportMode = 'merge';
    try {
      await uploadFile(e.target.files[0]);
    } catch (error) {
      researchWorkbenchState.researchImportPending = false;
      throw error;
    }
  });
  on('#research-target-add', 'click', () => {
    captureResearchDraft();
    $('#research-target-dialog').showModal();
  });
  on('#research-promote', 'click', () => {
    captureResearchDraft();
    if (!researchWorkbenchState.researchDraft.target_ids.length)
      throw new Error('ターゲット特許を選んでください。');
    researchWorkbenchState.researchDraft.entry_mode = 'target';
    invalidateResearchPreview();
    renderResearchWorkbench();
  });
}
function bindResearchPlan() {
  for (const [id, llm] of [
    ['research-plan-llm', true],
    ['research-plan-local', false],
  ])
    on('#' + id, 'click', (e) =>
      busy(e.currentTarget, '観点を整理しています…', () => requestResearchPlan(llm))
    );
  $$('[data-abstract]').forEach((el) =>
    listen(
      el,
      'click',
      () => {
        const field = $('textarea', el.closest('.research-concept'));
        const values = field.value.split(/\r?\n/);
        if (!values.includes(el.dataset.abstract))
          field.value += (field.value ? '\n' : '') + el.dataset.abstract;
        captureResearchPlan();
        invalidateResearchPreview();
        refreshResearchLogic();
      },
      undefined,
      'research-workbench.js:4'
    )
  );
  $$('.research-plan input,.research-plan textarea,.research-plan select').forEach((el) =>
    listen(
      el,
      'input',
      () => {
        captureResearchPlan();
        invalidateResearchPreview();
        refreshResearchLogic();
      },
      undefined,
      'research-workbench.js:5'
    )
  );
  on('#research-preview', 'click', (e) => busy(e.currentTarget, '確認中…', previewResearchPlan));
  bindResearchApply();
  refreshResearchLogic();
}
export {
  researchBrief,
  captureResearchDraft,
  captureResearchPlan,
  syncResearchPlanningControls,
  researchPlanState,
  researchGroupOptions,
  researchLogicHtml,
  researchTargetOptions,
  researchPlanHtml,
  invalidateResearchPreview,
  researchPlanPayload,
  renderResearchWorkbench,
  previewResearchPlan,
  initResearchWorkbench,
  researchClassificationProvenance,
  requestResearchPlan,
};

export function render(host, snapshot, stage) {
  return renderResearchWorkbench(stage, host, snapshot);
}
