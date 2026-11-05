import { bindLasso, insidePolygon as discoveryPointInPolygon } from './lasso.js';
import {
  judgmentLabel,
  classificationKey as discoveryKey,
  replaceHtml,
  listen,
  getState,
  $,
  setState,
  esc,
  html,
  $$,
  on,
  api,
  toast,
  run,
} from './core.js';
import PatentClassLabels from './classification-labels.js';
import PatentClassLayout from './classification-layout.js';
import { updateChrome } from './workspace-chrome.js';
import { setTab, setStep } from './app.js';
import { appState } from './ui-session.js';
import { captureClassDraft } from './classification-ui.js';

export const discoveryUiState = {
  discoveryDraft: null,
  discoveryRequestPending: false,
  discoveryVisibleCount: 24,
  discoveryResultsSignature: '',
  discoveryHistorySignature: '',
  discoveryPlanRequestState: null,
  discoveryFocusKey: null,
  discoveryLassoMode: false,
  discoveryMapCache: null,
  discoveryMapObserver: null,
  discoveryAdopting: false,
};
/* Discovery is a reviewable workspace: importing evidence never selects a class. */

const discoveryPicked = new Set();

const discoveryCompare = {
  mode: 'map',
  facet: 'all',
  source: 'all',
  search: '',
  selectedOnly: false,
  language: 'ja',
  zoom: 'normal',
};

function discoveryItems(discovery = getState().discovery || {}) {
  const recommendations = discovery.analysis?.recommendations || [];
  const known = new Set(recommendations.map(discoveryKey));
  return [
    ...recommendations,
    ...(discovery.plan?.candidate_hypotheses || []).filter((item) => !known.has(discoveryKey(item))),
  ];
}
function captureDiscoveryDraft() {
  if (!$('#discovery-keywords')) return;
  discoveryUiState.discoveryDraft = {
    keywords: $('#discovery-keywords').value,
    english_terms: $('#discovery-english').value,
    use_llm: $('#discovery-use-llm').checked,
    max_rounds: $('#discovery-round-limit').value,
    max_queries: $('#discovery-query-limit').value,
    per_query: $('#discovery-result-limit').value,
  };
}
function resetDiscoveryDraft() {
  discoveryUiState.discoveryDraft = null;
  discoveryPicked.clear();
  discoveryUiState.discoveryVisibleCount = 24;
  discoveryUiState.discoveryPlanRequestState = null;
  discoveryUiState.discoveryFocusKey = null;
  discoveryUiState.discoveryLassoMode = false;
  discoveryUiState.discoveryMapCache = null;
  discoveryUiState.discoveryMapObserver?.disconnect();
  discoveryUiState.discoveryMapObserver = null;
  Object.assign(discoveryCompare, {
    mode: 'map',
    facet: 'all',
    source: 'all',
    search: '',
    selectedOnly: false,
    language: 'ja',
    zoom: 'normal',
  });
  discoveryUiState.discoveryResultsSignature = '';
  discoveryUiState.discoveryHistorySignature = '';
  const root = $('#discovery-content');
  if (root) replaceHtml(root, '');
}
function discoveryTextList(values) {
  return (values || [])
    .map((value) => (typeof value === 'string' ? value : value.term || value.label || ''))
    .filter(Boolean);
}
function discoverySource(source) {
  return typeof source === 'string' ? source : JSON.stringify(source || 'CSV');
}
function discoveryPlanChanged() {
  const plan = getState().discovery?.plan;
  return Boolean(
    plan &&
    $('#discovery-keywords') &&
    ($('#discovery-keywords').value.trim() !== (plan.keywords || '').trim() ||
      ($('#discovery-english')?.value || '').trim() !== (plan.user_english_terms || '').trim())
  );
}
function mergeDiscoveryResponse(result, adopt = false) {
  const fields = adopt ? ['candidates', 'classification_view', 'discovery', 'job'] : ['discovery', 'job'];
  setState({
    ...getState(),
    ...Object.fromEntries(
      fields
        .filter((field) => Object.prototype.hasOwnProperty.call(result, field))
        .map((field) => [field, result[field]])
    ),
  });
}
function discoveryWarnings(values) {
  return values?.length
    ? html`<div class="discovery-warnings" role="note">
        ${values.map((value) => html`<p>${esc(value)}</p>`)}</div>`
    : '';
}
function discoveryLlmTagsHtml(values) {
  return values.length
    ? html`<div class="discovery-llm-tags">${values.map((value) => html`<span>${esc(value)}</span>`)}</div>`
    : '<span class="caption">追加なし（重複・検証結果は下の記録を参照）</span>';
}
function discoveryLlmFacetHtml(f) {
  const terms = [...discoveryTextList(f.terms), ...discoveryTextList(f.english_terms)];
  return html`<div class="discovery-llm-facet"><strong>${esc(f.label || f.id)}</strong>
    ${discoveryLlmTagsHtml(terms)}</div>`;
}
function discoveryLlmTargetsHtml(english, facets, codes) {
  return html`<div class="discovery-llm-targets"><article><b>主題の英語検索語</b>
    <small>反映先：探索計画の主題語（検索案は先頭6語まで）</small>${discoveryLlmTagsHtml(english)}</article>
    <article><b>補った技術観点</b><small>反映先：探索の観点・分類候補の語句照合</small>
    ${facets.length ? facets.map(discoveryLlmFacetHtml) : '<span class="caption">追加なし（既存の観点を維持）</span>'}
    </article>
    ${codes.length ? discoveryLlmCodesHtml(codes) : ''}
    </div>`;
}
function discoveryLlmCodesHtml(codes) {
  return html`<article><b>返された分類コード</b><small>反映先：存在を確認できた探索仮説</small>
    ${discoveryLlmTagsHtml(codes)}</article>`;
}
function discoveryLlmProposalHtml(plan) {
  if (!plan) return '';
  const proposal = plan.llm_proposal;
  if (!proposal)
    return html`<div class="panel discovery-llm-report"><h3>この計画にはLLMの反映記録がありません</h3>
      <p class="caption">以前の版で作成した計画のため、どの語をLLMが返したかは特定できません。
      次回の計画作成から、受け取った提案と反映先をここに表示します。</p></div>`;
  if (!proposal.used)
    return html`<div class="panel discovery-llm-report"><h3>今回はLLMを使わず計画を作成しました</h3>
      <p class="caption">入力語・分類辞書・既存候補を使っています。
      「LLMで観点・検索語を補う」にチェックして計画を作ると、追加された語と反映先を確認できます。</p></div>`;
  const english = discoveryTextList(proposal.english_terms),
    facets = (proposal.facets || []).filter((f) => f.terms?.length || f.english_terms?.length),
    codes = discoveryTextList(proposal.candidate_keys);
  const changed = english.length || facets.length || codes.length;
  return html`<section class="panel discovery-llm-report" aria-label="探索ラボのLLM提案と反映先">
    <div class="panel-heading"><div><h3>${changed ? 'LLMの提案を反映しました' : 'LLMの提案を確認しました · 新規追加なし'}</h3></div>
    <span class="pill">主題語 ${english.length} · 観点 ${facets.length}</span></div>
    <p class="caption">ラボのLLMには英語の主題語と技術観点を依頼します。分類候補は、その語を分類辞書に照合して探します。
    候補の追加・選択と検索の実行は別の操作です。</p>
    ${discoveryLlmTargetsHtml(english, facets, codes)}${discoveryWarnings(proposal.notices)}
    <p class="caption">対話で探索へは、下の比較画面で分類を選び「選んだ分類を手動探索へ」で引き継ぎます。
    統括ワークフローでは、同じテーマの計画を処理に利用します。</p></section>`;
}
function discoveryHelpHtml() {
  return html`<details class="panel-help"><summary role="button" aria-label="探索ラボのヘルプ">?</summary>
    <div><h3>仕組みと改善の考え方</h3><div class="discovery-architecture-grid">
    <div><h3>いま動く仕組み</h3><p>観点ごとの検索案を作り、CSVまたはEPO OPSで得た公報の分類を集計します。
    公報・ファミリー・要否判断を根拠として、分類候補と次の検索語を更新します。
    自動検索では重複を除き、回数の上限まで繰り返します。</p></div>
    <div><h3>手動探索へつなぐ</h3><p>採用したい分類だけをコンステレーションへ送ります。
    検索に使う分類の選択は、手動探索で確認して決められます。
    検索式の出力形式や、既存のTransformer学習の操作も続けて使えます。</p></div>
    <div><h3>次の拡張案</h3><p>引用・被引用の追跡、分野ごとの探索漏れの評価、少数の関連特許からの能動学習へ拡張できます。
    現在の改善は探索条件の更新であり、このタブでTransformerを再学習するものではありません。</p></div></div>
    <p class="caption">得られた候補は、検索語と取り込んだ特許の範囲に依存します。
    公報の同時出現は技術的な関連性の手掛かりです。IPC候補を自動採用せず、出典と内容を確認できる構成にしています。</p>
    </div></details>`;
}
function discoveryPlanFormHtml(draft) {
  return html`<div class="discovery-form"><label for="discovery-keywords">調べたい技術・キーワード</label>
    <textarea id="discovery-keywords" rows="2" placeholder="例：全固体電池、界面の安定化、量産プロセス">
    ${esc(draft.keywords)}</textarea>
    <div class="discovery-plan-actions">
    <button id="discovery-plan" class="button primary discovery-action">探索計画を作る ↗</button>
    <label class="check"><input type="checkbox" id="discovery-use-llm" ${draft.use_llm ? 'checked' : ''}>
    LLMで観点・検索語を補う</label></div>
    <p class="caption" id="discovery-llm-note">LLMの返り値は英語検索語と技術観点です。計画作成後に、追加された語と反映先を表示します。
    チェックした場合だけキーワードを設定中のLLMへ送ります。</p>
    <p class="caption" id="discovery-plan-note" aria-live="polite"></p></div>`;
}
function discoveryCsvPanelHtml() {
  return html`<div class="panel discovery-csv"><div class="discovery-section-title">
    <span class="discovery-step-number">01</span><div><h3>まずはCSVで育てる</h3>
    <p class="caption">検索結果を追加し、分類の広がりを確認します。</p></div><span class="pill">API設定不要</span></div>
    <div class="discovery-csv-actions">
    <button id="discovery-analyze" class="button discovery-action">取込済みCSVで検証</button>
    <span class="caption" id="discovery-manual-count"></span></div>
    <div class="discovery-upload"><label for="discovery-csv-file">次の検索結果CSV</label>
    <input type="file" id="discovery-csv-file" accept=".csv,.tsv,text/csv,text/tab-separated-values">
    <button id="discovery-import" class="button primary discovery-action">次のCSVを追加して分析 ↗</button></div>
    <p class="caption">CSV / TSV・20 MBまで。発明の名称・要約・IPC・公報番号などを含めてください。
    公報番号が同じ文献は重複をまとめ、手動探索の特許と判断はそのまま保持します。</p></div>`;
}
function discoveryLimitFieldHtml(id, label, min, max, value) {
  return html`<label>${label}<input id="${id}" type="number" min="${min}" max="${max}" step="1"
    value="${esc(value)}"></label>`;
}
function discoveryRemotePanelHtml(draft) {
  return html`<details class="panel discovery-remote"><summary><span class="discovery-section-title">
    <span class="discovery-step-number">02</span><span><b>外部検索を自動化する</b><small>EPO OPS · 任意の拡張</small></span>
    <span class="discovery-summary-arrow">⌄</span></span></summary>
    <p class="caption">公開特許のメタデータを取得し、見つかったIPCを次の検索へつなげます。</p>
    <label for="discovery-english">英語のトピック語（任意）</label>
    <textarea id="discovery-english" rows="2" placeholder='例：solid state battery, solid electrolyte'>
    ${esc(draft.english_terms)}</textarea>
    <p class="caption">英語検索に使う語です。変更後は「探索計画を作る」で反映します。</p>
    <div class="discovery-limits">
    ${discoveryLimitFieldHtml('discovery-round-limit', '最大ラウンド', 1, 3, draft.max_rounds)}
    ${discoveryLimitFieldHtml('discovery-query-limit', '検索回数の上限', 1, 12, draft.max_queries)}
    ${discoveryLimitFieldHtml('discovery-result-limit', '1検索の取得件数', 5, 25, draft.per_query)}</div>
    <p class="caption">キーワード・英語検索語・IPCをEPOへ送信します。上限や候補の収束で停止します。</p>
    <p class="notice" id="discovery-ops-status"></p>
    <div class="button-row">
    <button id="discovery-run" class="button primary discovery-action">EPO OPSで再帰検索 ↗</button>
    <button id="discovery-settings" class="button subtle small">接続・Proxy設定 →</button></div></details>`;
}
function discoveryFormHtml(draft) {
  return html`<div class="panel discovery-start">${discoveryHelpHtml()}
    <div class="discovery-intro"><h2>探索ラボ</h2>
    <p class="muted">製品だけでなく、材料・製法・界面などの観点で探索。
    CSVを追加するたびに、実際の公報が持つ分類と次の検索案を見直します。</p></div>
    ${discoveryPlanFormHtml(draft)}</div>
    <div class="discovery-workflows">${discoveryCsvPanelHtml()}${discoveryRemotePanelHtml(draft)}</div>
    <div id="discovery-llm-request-status" role="status" aria-live="polite"></div>
    <div id="discovery-progress" aria-live="polite"></div><div id="discovery-results"></div>
    <div id="discovery-history"></div>`;
}
function discoveryHasEvidence(item) {
  return ['patent_metadata', 'derived_candidate'].includes(item?.evidence_status);
}
function discoveryOriginRowHtml(o) {
  return html`<p><b>${o.type === 'fi_to_ipc' ? 'FIからの候補' : 'Fタームのテーマ範囲からの候補'}</b><br>
    ${esc(o.raw || '')} · ${esc(o.patent_id || '')}<br>
    ${esc(o.warning || '公報に付与されたIPCと同一とは限りません。')}
    ${o.source_version ? html`<br>対応表：${esc(o.source_version)}` : ''}
    ${o.ipc_version ? ` · IPC：${esc(o.ipc_version)}` : ''}</p>`;
}
function discoveryOriginHtml(item) {
  const origins = (item.classification_origins || item.origins || []).filter((o) =>
    ['fi_to_ipc', 'fterm_theme_to_ipc'].includes(o.type)
  );
  return origins.length
    ? html`<details class="discovery-origin"><summary>IPC候補の対応元を確認 (${origins.length})</summary>
        ${origins.slice(0, 12).map(discoveryOriginRowHtml)}</details>`
    : '';
}
function discoveryCandidateSourceLabel(item, metadata, syntheticCount, supportCount) {
  const base =
    item.evidence_status === 'derived_candidate'
      ? 'FI・Fターム対応からのIPC候補（付与IPCとは別）'
      : metadata
        ? syntheticCount && syntheticCount === supportCount
          ? '架空サンプル由来'
          : '公報由来候補'
        : '辞書候補・探索仮説';
  return base + (item.verified === false ? ' · 分類定義未確認' : '');
}
function discoveryCandidateLabelHtml(item, key, preview, sourceLabel, synthetic) {
  return html`<label class="discovery-candidate-label" title="${esc(preview)}">
    <input type="checkbox" data-discovery-key="${esc(key)}"
    ${discoveryPicked.has(key) ? 'checked' : ''} ${item.selectable === false ? 'disabled' : ''}>
    <span><small class="discovery-source ${synthetic ? 'synthetic' : ''}">${esc(sourceLabel)}</small>
    <b>${esc(item.kind)} ${esc(item.code)}</b>
    <span class="discovery-candidate-title">${esc(discoveryTitle(item))}</span></span></label>`;
}
function discoveryEvidenceCountHtml(item) {
  const ai = Number(item.ai_negative_count || 0),
    human = Number(item.human_negative_count || 0);
  return html`<div class="discovery-evidence-count"><span><b>${Number(item.support_count || 0)}</b> 公報</span>
    <span><b>${Number(item.family_count || 0)}</b> ファミリー等</span>
    <span>人の必要 ${Number(item.positive_count || 0)} / AIの必要 ${Number(item.ai_positive_count || 0)}</span>
    <span>不要 ${Number(item.negative_count || 0)}（人 ${human} / AI ${ai}）</span>
    </div>`;
}
function discoveryEvidenceRowHtml(row) {
  return html`<li><b>${esc(row.patent_id || '公報番号なし')}${row.synthetic ? '<em>架空サンプル</em>' : ''}</b>
    <span>${esc(row.title || '名称なし')}</span>
    <small>${esc(discoverySource(row.source))} · ${judgmentLabel(row.label)}
    ${row.family_id ? ` · ファミリー ${esc(row.family_id)}` : ''}</small></li>`;
}
function discoveryEvidenceMoreHtml(item, evidence) {
  return html`<p class="caption">支持する公報 ${Number(item.support_count)} 件から、
    ファミリー等の代表公報 ${evidence.length} 件を表示。</p>`;
}
function discoveryEvidenceListHtml(item, evidence) {
  const more = item.support_count > evidence.length;
  return html`<ul>${evidence.map(discoveryEvidenceRowHtml).join('') || '<li>表示できる公報情報がありません。</li>'}</ul>
    ${more ? discoveryEvidenceMoreHtml(item, evidence) : ''}`;
}
function discoveryEvidenceDetailsHtml(item, key, evidence, metadata) {
  const hypothesis =
    '<p class="caption">分類辞書に基づく探索候補です。公報中の出現件数を示すものではありません。' +
    'CSV検証または外部検索を行うと、実際の公報から得た候補を比較できます。</p>';
  return html`<details class="discovery-evidence" data-discovery-detail="${esc(key)}">
    <summary>${metadata ? '根拠の公報を見る' : '候補の位置づけ'} <span>↗</span></summary>
    ${metadata ? discoveryEvidenceListHtml(item, evidence) : hypothesis}</details>`;
}
function discoveryTagsHtml(values) {
  return html`<div class="discovery-tags">${values.map((value) => html`<span>${esc(value)}</span>`)}</div>`;
}
function discoveryCandidateHtml(item) {
  const key = discoveryKey(item),
    evidence = item.evidence || [],
    metadata = discoveryHasEvidence(item);
  const syntheticCount = Number(item.synthetic_count ?? evidence.filter((row) => row.synthetic).length);
  const supportCount = Number(item.support_count ?? evidence.length);
  const sourceLabel = discoveryCandidateSourceLabel(item, metadata, syntheticCount, supportCount);
  const reason =
    item.reason ||
    (metadata
      ? '取り込んだ特許の分類に含まれます。出典と要否判断を確認してください。'
      : '探索の観点から挙げた仮説です。公報での検証はまだありません。');
  const preview = [
    item.title,
    reason,
    ...evidence.slice(0, 4).map((row) => `${row.patent_id || ''} ${row.title || ''}`),
  ]
    .filter(Boolean)
    .join('\n');
  const facets = discoveryTextList(item.facets).map(
    (value) => getState().discovery?.plan?.facets?.find((facet) => facet.id === value)?.label || value
  );
  const tone = `${metadata ? 'with-evidence' : 'hypothesis'} ${discoveryPicked.has(key) ? 'chosen' : ''}`;
  return html`<article class="discovery-candidate ${tone}">
    ${discoveryCandidateLabelHtml(item, key, preview, sourceLabel, syntheticCount)}
    ${facets.length ? discoveryTagsHtml(facets) : ''}
    <p class="discovery-reason">${esc(reason)}</p>${discoveryOriginHtml(item)}
    ${metadata ? discoveryEvidenceCountHtml(item) : ''}
    ${discoveryEvidenceDetailsHtml(item, key, evidence, metadata)}</article>`;
}
function discoveryLabels(item) {
  return PatentClassLabels.bilingual(item);
}
function discoveryTitle(item, language = discoveryCompare.language) {
  return PatentClassLabels.label(item, language);
}
function discoveryDefinitionHtml(language, item, labels) {
  const ja = language === 'ja',
    status = (code) => PatentClassLabels.status(code);
  const name = ja ? labels.ja : labels.en,
    statusCode = ja ? labels.jaStatus : labels.enStatus,
    version = ja ? item.title_ja_version : item.title_en_version,
    parentTitle = ja ? item.title_ja_parent : item.title_en_parent,
    parentCode = item.parent || (ja ? item.title_ja_parent_code : item.title_en_parent_code) || '';
  const fallback = ja ? '日本語名称は未収録です。英語の定義を確認してください。' : '英語名称は未収録です。';
  return html`<span class="discovery-definition-language">${ja ? '日本語' : 'English'}
    ${name ? ` · ${esc(status(statusCode))}` : ''}${version ? ` · ${esc(version)}` : ''}</span>
    <p lang="${language}">${esc(name || fallback)}
    ${parentTitle ? discoveryDefinitionParentHtml(ja, parentCode, parentTitle) : ''}</p>`;
}
function discoveryDefinitionParentHtml(ja, parentCode, parentTitle) {
  const label = ja ? '上位：' : 'Parent: ';
  return html`<span class="discovery-definition-parent">
    ${label}${esc(parentCode)} ${esc(parentTitle)}</span>`;
}
function discoveryBilingualHtml(item) {
  const labels = discoveryLabels(item);
  return html`<div class="discovery-definition">${discoveryDefinitionHtml('ja', item, labels)}
    ${discoveryDefinitionHtml('en', item, labels)}</div>`;
}
function discoveryEvidenceType(item) {
  if (item.evidence_status === 'derived_candidate') return 'derived';
  if (!discoveryHasEvidence(item)) return 'hypothesis';
  const count = Number(item.support_count ?? item.evidence?.length ?? 0);
  const synthetic = Number(
    item.synthetic_count ?? (item.evidence || []).filter((row) => row.synthetic).length
  );
  return synthetic > 0 && synthetic >= count ? 'synthetic' : 'patent';
}
function discoveryEvidenceLabel(item) {
  const type = discoveryEvidenceType(item);
  return type === 'derived'
    ? 'FI・Fターム対応のIPC候補 · 付与IPCとは別'
    : type === 'synthetic'
      ? '架空サンプル由来'
      : type === 'patent'
        ? '公報由来候補'
        : '辞書候補・探索仮説';
}
function discoveryFilteredItems(items = discoveryItems()) {
  const search = discoveryCompare.search.trim().toLocaleLowerCase();
  return items.filter((item) => {
    const labels = discoveryLabels(item);
    return (
      (discoveryCompare.source === 'all' || discoveryEvidenceType(item) === discoveryCompare.source) &&
      (discoveryCompare.facet === 'all' || (item.facets || []).includes(discoveryCompare.facet)) &&
      (!discoveryCompare.selectedOnly || discoveryPicked.has(discoveryKey(item))) &&
      (!search ||
        [item.kind, item.code, item.title, labels.ja, labels.en]
          .filter(Boolean)
          .join(' ')
          .toLocaleLowerCase()
          .includes(search))
    );
  });
}
function discoveryModeTabHtml(mode, label) {
  const active = discoveryCompare.mode === mode;
  return html`<button data-discovery-mode="${mode}" class="${active ? 'active' : ''}"
    aria-pressed="${active}">${label}</button>`;
}
function discoverySourceFilterHtml([value, label, count]) {
  const active = value === discoveryCompare.source;
  return html`<button data-discovery-source="${value}" class="${active ? 'active' : ''}"
    aria-pressed="${active}"><i class="${value}"></i>${label}<b>${count}</b></button>`;
}
function discoveryFacetOptionHtml(facet) {
  return html`<option value="${esc(facet.id)}" ${facet.id === discoveryCompare.facet ? 'selected' : ''}>
    ${esc(facet.label)}</option>`;
}
function discoveryCompareFiltersHtml() {
  const facets = getState().discovery?.plan?.facets || [];
  return html`<div class="discovery-compare-filters"><select id="discovery-facet-filter" aria-label="技術の観点">
    <option value="all">すべての観点</option>${facets.map(discoveryFacetOptionHtml)}</select>
    <input id="discovery-class-search" aria-label="分類コード・名称で絞り込む" placeholder="分類コード・名称を探す"
    value="${esc(discoveryCompare.search)}">
    <select id="discovery-label-language" aria-label="地図・一覧の名称表示">
    <option value="ja" ${discoveryCompare.language === 'ja' ? 'selected' : ''}>日本語を優先</option>
    <option value="en" ${discoveryCompare.language === 'en' ? 'selected' : ''}>English</option></select>
    <label class="check"><input id="discovery-selected-only" type="checkbox"
    ${discoveryCompare.selectedOnly ? 'checked' : ''}>選択済み</label></div>`;
}
function discoveryAdoptionHtml(items) {
  const picked = [...discoveryPicked].filter((key) =>
    items.some((item) => discoveryKey(item) === key && item.selectable !== false)
  ).length;
  return html`<div class="discovery-adoption"><div><strong id="discovery-picked-count">${picked}</strong>
    <span> 件を手動探索へ</span><p>選んだ分類を候補に追加し、検索条件は移動先で確認できます。</p></div>
    <button id="discovery-adopt" class="button primary discovery-action">選んだ分類を手動探索へ送る →</button></div>`;
}
function discoveryComparisonHtml(items) {
  const counts = { patent: 0, derived: 0, hypothesis: 0, synthetic: 0 };
  items.forEach((item) => counts[discoveryEvidenceType(item)]++);
  const sources = [
    ['all', 'すべて', items.length],
    ['patent', '公報由来', counts.patent],
    ['derived', 'FI・Fターム対応', counts.derived],
    ['hypothesis', '探索仮説', counts.hypothesis],
    ['synthetic', '架空サンプル', counts.synthetic],
  ];
  return html`<section class="panel discovery-compare-panel" aria-labelledby="discovery-compare-heading">
    <div class="discovery-compare-heading"><div><h3 id="discovery-compare-heading">分類候補を見比べる</h3>
    <p>近い技術を見渡して、根拠を確かめる。</p></div>
    <div class="discovery-view-tabs" role="group" aria-label="分類候補の表示">${discoveryModeTabHtml('map', '◌ 地図')}
    ${discoveryModeTabHtml('list', '☷ 一覧')}</div></div>
    <div class="discovery-source-filters" role="group" aria-label="候補の出典で絞り込み">
    ${sources.map(discoverySourceFilterHtml)}</div>
    ${discoveryCompareFiltersHtml()}
    <div class="discovery-compare-status"><span id="discovery-visible-status"></span>
    <button id="discovery-clear" class="button subtle small">選択を解除</button></div>
    <div id="discovery-comparison-body">${discoveryComparisonBodyHtml()}</div>
    ${discoveryAdoptionHtml(items)}</section>`;
}
function discoveryZoomOptionHtml(value, label) {
  const selected = discoveryCompare.zoom === value ? 'selected' : '';
  return html`<option value="${value}" ${selected}>${label}</option>`;
}
function discoveryMapViewHtml(items) {
  const lasso = discoveryUiState.discoveryLassoMode;
  return html`<div class="discovery-map-tools"><span>クリックで選択 · 重ねて根拠を表示</span>
    <button id="discovery-lasso-toggle" class="button subtle small ${lasso ? 'active' : ''}"
    aria-pressed="${lasso}">◌ 囲んで選択</button>
    <select id="discovery-map-zoom" aria-label="地図の表示倍率">${discoveryZoomOptionHtml('normal', '標準')}
    ${discoveryZoomOptionHtml('fit', '全体を見る')}${discoveryZoomOptionHtml('large', '拡大')}</select></div>
    <div class="discovery-map-viewport" id="discovery-map-viewport" tabindex="0"
    aria-label="分類候補の地図。地図内をスクロールできます">
    <svg id="discovery-class-map" class="discovery-star-map ${lasso ? 'lasso-on' : ''}" role="group"
    aria-label="意味の近さから配置した分類候補"></svg>
    ${!items.length ? '<div class="discovery-filter-empty">該当する候補はありません。<br>出典・観点・検索語を見直してください。</div>' : ''}
    </div>
    <div class="discovery-map-caption">距離と見出しは技術の近さの目安です。色は出典、円内の数字は支持公報数、? は仮説です。<br>
    未収録の名称はもう一方の言語を表示します。地図内をスクロールできます。「全体を見る」で配置全体を確認できます。</div>`;
}
function discoveryListRowHtml(item) {
  const key = discoveryKey(item),
    picked = discoveryPicked.has(key);
  const support = discoveryHasEvidence(item)
    ? html`<span>${Number(item.support_count || 0)} 公報<br>${Number(item.family_count || 0)} ファミリー等</span>`
    : '<span>公報による検証待ち</span>';
  return html`<tr class="${picked ? 'chosen' : ''}" data-discovery-row="${esc(key)}">
    <td><input type="checkbox" data-discovery-key="${esc(key)}" aria-label="${esc(item.code)} を選択"
    ${picked ? 'checked' : ''} ${item.selectable === false ? 'disabled' : ''}></td>
    <td><button class="discovery-list-focus" data-discovery-focus="${esc(key)}">
    <b>${esc(item.kind)} ${esc(item.code)}</b><span>${esc(discoveryTitle(item))}</span></button></td>
    <td><small class="discovery-list-source ${discoveryEvidenceType(item)}">
    ${esc(discoveryEvidenceLabel(item))}</small>${support}</td></tr>`;
}
function discoveryListViewHtml(items) {
  const empty = '<tr><td colspan="3">該当する候補はありません。絞り込み条件を見直してください。</td></tr>';
  return html`<div class="discovery-list-viewport"><table class="discovery-comparison-table">
    <thead><tr><th>選択</th><th>分類・名称</th><th>根拠</th></tr></thead>
    <tbody>${items.map(discoveryListRowHtml).join('') || empty}</tbody></table></div>`;
}
function discoveryComparisonBodyHtml() {
  const items = discoveryFilteredItems();
  if (!items.some((item) => discoveryKey(item) === discoveryUiState.discoveryFocusKey))
    discoveryUiState.discoveryFocusKey = items[0] ? discoveryKey(items[0]) : null;
  const focus = items.find((item) => discoveryKey(item) === discoveryUiState.discoveryFocusKey);
  const view = discoveryCompare.mode === 'map' ? discoveryMapViewHtml(items) : discoveryListViewHtml(items);
  const empty = '<p class="caption">分類を選ぶと、名称・出典・根拠の公報を確認できます。</p>';
  return html`<div class="discovery-comparison-layout"><div class="discovery-map-side">${view}</div>
    <aside class="discovery-inspector" id="discovery-inspector" aria-label="確認中の分類の詳細">
    <div class="discovery-inspector-heading"><span>INSPECT THE EVIDENCE</span><h4>分類と根拠</h4></div>
    <div id="discovery-inspector-content">${focus ? discoveryInspectorHtml(focus) : empty}</div>
    </aside></div>`;
}
function discoveryInspectorHtml(item) {
  const card = discoveryCandidateHtml(item).replace(
    '<details class="discovery-evidence"',
    '<details open class="discovery-evidence"'
  );
  return card.replace(
    '<p class="discovery-reason">',
    `${discoveryBilingualHtml(item)}<p class="discovery-reason">`
  );
}
function renderDiscoveryInspector(item) {
  if (!item || !$('#discovery-inspector-content')) return;
  discoveryUiState.discoveryFocusKey = discoveryKey(item);
  replaceHtml($('#discovery-inspector-content'), discoveryInspectorHtml(item));
  $$('.discovery-star').forEach((node) =>
    node.classList.toggle('focused', node.dataset.discoveryFocus === discoveryUiState.discoveryFocusKey)
  );
  bindDiscoveryCheckboxes($('#discovery-inspector-content'));
}
function discoveryShortTitle(item) {
  const title = discoveryTitle(item);
  return title.length > 20 ? title.slice(0, 18) + '…' : title;
}
function discoveryStarRegionHtml(group) {
  const b = group.bounds;
  const title = PatentClassLabels.group
    ? PatentClassLabels.group(group.label, discoveryCompare.language)
    : group.label;
  return html`<g><rect x="${b.x}" y="${b.y}" width="${b.width}" height="${b.height}" rx="24"/>
    <text x="${b.x + 13}" y="${b.y + 22}" data-width="${Math.max(1, b.width - 26)}">${esc(title)}
    <tspan> · ${group.memberIndices.length}</tspan></text></g>`;
}
function discoveryStarLinkHtml(link, positions) {
  const a = positions[link.from],
    b = positions[link.to];
  return a && b ? html`<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}"/>` : '';
}
function discoveryStarHtml(item, index, position) {
  const key = discoveryKey(item),
    selected = discoveryPicked.has(key),
    focused = key === discoveryUiState.discoveryFocusKey;
  const label = item.kind + ' ' + item.code,
    title = discoveryTitle(item),
    evidence = discoveryEvidenceLabel(item);
  const mark = selected ? '✓' : discoveryHasEvidence(item) ? Number(item.support_count || 0) : '?';
  const tone = `${discoveryEvidenceType(item)} ${selected ? 'chosen' : ''} ${focused ? 'focused' : ''}`;
  return html`<g class="discovery-star ${tone}" data-discovery-focus="${esc(key)}"
    data-discovery-index="${index}"
    data-discovery-selectable="${item.selectable !== false}"
    transform="translate(${position[0]} ${position[1]})" role="button" tabindex="0"
    aria-label="${esc(label + ' ' + title + ' · ' + evidence)}" aria-pressed="${selected}">
    <title>${esc(label + '\n' + title + '\n' + evidence)}</title><circle r="32"/>
    <text class="discovery-star-kind" y="-5">${esc(item.kind)}</text>
    <text class="discovery-star-mark" y="15">${mark}</text>
    <text class="discovery-star-code" y="49">${esc(item.code)}</text>
    <text class="discovery-star-title" y="69">${esc(discoveryShortTitle(item))}</text></g>`;
}
function discoveryMapHtml(items, layout) {
  const { positions, groups = [], links = [] } = layout;
  return html`<g class="discovery-star-regions" aria-hidden="true">${groups.map(discoveryStarRegionHtml)}</g>
    <g class="discovery-star-links" aria-hidden="true">
    ${links.map((link) => discoveryStarLinkHtml(link, positions))}</g>
    <g>${items.map((item, index) => discoveryStarHtml(item, index, positions[index]))}</g>
    <path id="discovery-lasso-path" class="discovery-lasso-path"/>`;
}
function bindDiscoveryStar(node, item, svg) {
  const pick = () => {
    renderDiscoveryInspector(item);
    toggleDiscoveryPicked(discoveryKey(item));
  };
  listen(
    node,
    'mouseenter',
    () => {
      if (!svg.dataset.drawing) renderDiscoveryInspector(item);
    },
    undefined,
    'discovery-ui.js:1'
  );
  listen(node, 'focus', () => renderDiscoveryInspector(item), undefined, 'discovery-ui.js:2');
  listen(
    node,
    'click',
    () => {
      if (!svg.dataset.dragged) pick();
    },
    undefined,
    'discovery-ui.js:3'
  );
  listen(
    node,
    'keydown',
    (event) => {
      if (event.key === ' ' || event.key === 'Enter') {
        event.preventDefault();
        pick();
      }
    },
    undefined,
    'discovery-ui.js:4'
  );
}
function fitDiscoveryLabels(svg) {
  $$('.discovery-star-title, .discovery-star-code', svg).forEach((label) => {
    if (label.getComputedTextLength() > 164) {
      label.setAttribute('textLength', '164');
      label.setAttribute('lengthAdjust', 'spacingAndGlyphs');
    }
  });
  $$('.discovery-star-regions text', svg).forEach((label) => {
    const width = Number(label.dataset.width);
    if (width > 0 && label.getComputedTextLength() > width) {
      label.setAttribute('textLength', String(width));
      label.setAttribute('lengthAdjust', 'spacingAndGlyphs');
    }
  });
}
function renderDiscoveryMap() {
  const svg = $('#discovery-class-map'),
    viewport = $('#discovery-map-viewport');
  if (!svg || !viewport) return;
  discoveryUiState.discoveryMapObserver?.disconnect();
  const items = discoveryFilteredItems(),
    width = viewport.clientWidth > 720 ? 900 : 620;
  const semanticItems = items.map((item) => ({
    ...item,
    title: discoveryLabels(item).ja || item.title,
    title_en: discoveryLabels(item).en,
  }));
  const cacheKey = JSON.stringify([width, semanticItems]);
  if (discoveryUiState.discoveryMapCache?.key !== cacheKey) {
    const layout = PatentClassLayout.compute(semanticItems, { mode: 'semantic', width });
    discoveryUiState.discoveryMapCache = { key: cacheKey, layout };
  }
  const layout = discoveryUiState.discoveryMapCache.layout;
  svg.setAttribute('viewBox', `0 0 ${layout.width} ${layout.height}`);
  svg.setAttribute('width', layout.width);
  svg.setAttribute('height', layout.height);
  replaceHtml(svg, discoveryMapHtml(items, layout));
  updateDiscoveryMapZoom();
  fitDiscoveryLabels(svg);
  $$('.discovery-star', svg).forEach((node) =>
    bindDiscoveryStar(node, items[Number(node.dataset.discoveryIndex)], svg)
  );
  bindDiscoveryLasso(svg, items, layout.positions);
  if (window.ResizeObserver) {
    discoveryUiState.discoveryMapObserver = new window.ResizeObserver(() => {
      const wanted = viewport.clientWidth > 720 ? 900 : 620;
      if (wanted !== discoveryUiState.discoveryMapCache?.layout.width && viewport.clientWidth)
        renderDiscoveryMap();
    });
    discoveryUiState.discoveryMapObserver.observe(viewport);
  }
}
function updateDiscoveryMapZoom() {
  const svg = $('#discovery-class-map');
  if (!svg) return;
  svg.style.width = discoveryCompare.zoom === 'large' ? '150%' : '100%';
  svg.style.height = discoveryCompare.zoom === 'fit' ? '100%' : 'auto';
}

function applyDiscoveryLasso(keys) {
  if (discoveryUiState.discoveryAdopting) return;
  const allowed = new Set(
    discoveryFilteredItems()
      .filter((item) => item.selectable !== false)
      .map(discoveryKey)
  );
  for (const key of keys) if (allowed.has(key)) discoveryPicked.add(key);
  updateDiscoveryPickedDisplay();
}
function bindDiscoveryLasso(svg, items, positions) {
  bindLasso(svg, {
    enabled: () => discoveryUiState.discoveryLassoMode,
    dragAtThreshold: true,
    coordinate: (event) => {
      const point = svg.createSVGPoint();
      point.x = event.clientX;
      point.y = event.clientY;
      return point.matrixTransform(svg.getScreenCTM().inverse());
    },
    path: () => $('#discovery-lasso-path', svg),
    complete: (polygon) =>
      applyDiscoveryLasso(
        items
          .filter((item, index) =>
            discoveryPointInPolygon({ x: positions[index][0], y: positions[index][1] }, polygon)
          )
          .map(discoveryKey)
      ),
  });
}
function toggleDiscoveryPicked(key, checked) {
  if (discoveryUiState.discoveryAdopting) return;
  const item = discoveryItems().find((row) => discoveryKey(row) === key);
  if (!item || item.selectable === false) return;
  if (checked === undefined ? !discoveryPicked.has(key) : checked) discoveryPicked.add(key);
  else discoveryPicked.delete(key);
  updateDiscoveryPickedDisplay();
  if (discoveryCompare.selectedOnly) renderDiscoveryComparisonBody();
}
function updateDiscoveryPickedDisplay() {
  $$('[data-discovery-key]').forEach((node) => {
    node.checked = discoveryPicked.has(node.dataset.discoveryKey);
    node.closest('.discovery-candidate')?.classList.toggle('chosen', node.checked);
  });
  $$('.discovery-star').forEach((node) => {
    const selected = discoveryPicked.has(node.dataset.discoveryFocus),
      item = discoveryItems().find((row) => discoveryKey(row) === node.dataset.discoveryFocus);
    node.classList.toggle('chosen', selected);
    node.setAttribute('aria-pressed', String(selected));
    const mark = $('.discovery-star-mark', node);
    if (mark)
      mark.textContent = selected ? '✓' : discoveryHasEvidence(item) ? Number(item.support_count || 0) : '?';
  });
  $$('[data-discovery-row]').forEach((node) =>
    node.classList.toggle('chosen', discoveryPicked.has(node.dataset.discoveryRow))
  );
  updateDiscoveryVisibleStatus();
  updateDiscoveryControls();
}
function updateDiscoveryVisibleStatus() {
  const node = $('#discovery-visible-status');
  if (!node) return;
  const visible = discoveryFilteredItems(),
    all = discoveryItems(),
    hiddenPicked = [...discoveryPicked].filter(
      (key) => !visible.some((item) => discoveryKey(item) === key)
    ).length;
  const extra = hiddenPicked ? ` · 絞り込み外に選択 ${hiddenPicked} 件` : '';
  node.textContent = `${visible.length} / ${all.length} 件を表示${extra}`;
}
function bindDiscoveryCheckboxes(root = $('#discovery-results')) {
  if (!root) return;
  $$('[data-discovery-key]', root).forEach((node) =>
    listen(
      node,
      'change',
      () => toggleDiscoveryPicked(node.dataset.discoveryKey, node.checked),
      undefined,
      'discovery-ui.js:9'
    )
  );
}
function renderDiscoveryComparisonBody() {
  const body = $('#discovery-comparison-body');
  if (!body) return;
  discoveryUiState.discoveryMapObserver?.disconnect();
  replaceHtml(body, discoveryComparisonBodyHtml());
  bindDiscoveryComparisonBody();
  updateDiscoveryVisibleStatus();
  updateDiscoveryControls();
}
function bindDiscoveryComparisonBody() {
  renderDiscoveryMap();
  bindDiscoveryCheckboxes($('#discovery-comparison-body'));
  $$('[data-discovery-focus].discovery-list-focus').forEach((node) => {
    const show = () =>
      renderDiscoveryInspector(
        discoveryItems().find((item) => discoveryKey(item) === node.dataset.discoveryFocus)
      );
    listen(node, 'mouseenter', show, undefined, 'discovery-ui.js:10');
    listen(node, 'focus', show, undefined, 'discovery-ui.js:11');
    listen(node, 'click', show, undefined, 'discovery-ui.js:12');
  });
  on('#discovery-lasso-toggle', 'click', () => {
    discoveryUiState.discoveryLassoMode = !discoveryUiState.discoveryLassoMode;
    const button = $('#discovery-lasso-toggle');
    button.classList.toggle('active', discoveryUiState.discoveryLassoMode);
    button.setAttribute('aria-pressed', String(discoveryUiState.discoveryLassoMode));
    $('#discovery-class-map')?.classList.toggle('lasso-on', discoveryUiState.discoveryLassoMode);
  });
  on('#discovery-map-zoom', 'change', (event) => {
    discoveryCompare.zoom = event.target.value;
    updateDiscoveryMapZoom();
  });
}
function bindDiscoveryComparison() {
  $$('[data-discovery-mode]').forEach((node) =>
    listen(
      node,
      'click',
      () => {
        discoveryCompare.mode = node.dataset.discoveryMode;
        $$('[data-discovery-mode]').forEach((button) => {
          const active = button.dataset.discoveryMode === discoveryCompare.mode;
          button.classList.toggle('active', active);
          button.setAttribute('aria-pressed', String(active));
        });
        renderDiscoveryComparisonBody();
      },
      undefined,
      'discovery-ui.js:13'
    )
  );
  $$('[data-discovery-source]').forEach((node) =>
    listen(
      node,
      'click',
      () => {
        discoveryCompare.source = node.dataset.discoverySource;
        $$('[data-discovery-source]').forEach((button) => {
          const active = button.dataset.discoverySource === discoveryCompare.source;
          button.classList.toggle('active', active);
          button.setAttribute('aria-pressed', String(active));
        });
        renderDiscoveryComparisonBody();
      },
      undefined,
      'discovery-ui.js:14'
    )
  );
  on('#discovery-facet-filter', 'change', (event) => {
    discoveryCompare.facet = event.target.value;
    renderDiscoveryComparisonBody();
  });
  on('#discovery-class-search', 'input', (event) => {
    discoveryCompare.search = event.target.value;
    renderDiscoveryComparisonBody();
  });
  on('#discovery-selected-only', 'change', (event) => {
    discoveryCompare.selectedOnly = event.target.checked;
    renderDiscoveryComparisonBody();
  });
  on('#discovery-label-language', 'change', (event) => {
    discoveryCompare.language = event.target.value;
    renderDiscoveryComparisonBody();
  });
  bindDiscoveryComparisonBody();
  updateDiscoveryVisibleStatus();
}
function discoveryCopyQueryButtonHtml(value) {
  return html`<button class="button subtle small discovery-copy-query" data-discovery-query="${esc(value)}"
    >式をコピー</button>`;
}
function discoveryQueryHtml(query) {
  return html`<div class="discovery-query"><div><b>${esc(query.label || query.facet_id || '追加探索')}</b>
    <span>${esc(query.reason || '')}</span>
    ${query.query ? discoveryCopyQueryButtonHtml(query.query) : ''}</div>
    <pre>${esc(query.query || '')}</pre></div>`;
}
function discoveryFacetHtml(facet, index) {
  const english = facet.english_terms?.length
    ? html`<small>${esc(discoveryTextList(facet.english_terms).join(' · '))}</small>`
    : '';
  const number = String(index + 1).padStart(2, '0');
  return html`<div class="discovery-facet"><span class="discovery-facet-number">${number}</span>
    <h3>${esc(facet.label)}</h3><p>${esc(discoveryTextList(facet.terms).join(' / '))}</p>${english}</div>`;
}
function discoveryPlanPanelHtml(plan) {
  const facets = plan.facets || [],
    plannedQueries = plan.queued_queries || [];
  const noQueries = '<p class="caption">英語検索語を指定し、計画を作り直すと検索案が表示されます。</p>';
  return html`<div class="panel discovery-plan-panel"><div class="panel-heading"><div><h3>探索の観点</h3></div>
    <span class="pill">${facets.length} 観点</span></div>
    <p class="caption">探索テーマ：${esc(plan.keywords || '')}</p>
    <div class="discovery-facets">${facets.map(discoveryFacetHtml)}</div>${discoveryWarnings(plan.warnings)}
    <details class="discovery-query-details" data-discovery-detail="plan-queries">
    <summary>最初の検索案を確認 <span>${plannedQueries.length} 件 · EPO OPS形式</span></summary>
    ${plannedQueries.map(discoveryQueryHtml).join('') || noQueries}</details></div>`;
}
function discoveryMetricHtml([label, value]) {
  return html`<div><strong>${Number(value || 0).toLocaleString()}</strong><small>${label}</small></div>`;
}
function discoveryAnalysisHtml(analysis) {
  const recommendations = analysis.recommendations || [],
    nextQueries = analysis.next_queries || [],
    newTerms = discoveryTextList(analysis.new_terms);
  const metrics = [
    ['対象公報', analysis.counts?.documents],
    ['ファミリー等の集計単位', analysis.counts?.families],
    ['公報・対応づけの分類候補', recommendations.length],
    ['架空サンプル', analysis.counts?.synthetic],
  ];
  const excluded = Number(analysis.counts?.excluded);
  return html`<div class="panel discovery-analysis"><div class="panel-heading"><div><h3>公報の分析</h3></div>
    <a class="button subtle small" href="/api/discovery/export" download>探索記録を保存 ↓</a></div>
    <p class="caption">ファミリーIDがない公報は、公報ごとに1集計単位として数えます。</p>
    <div class="discovery-metrics">${metrics.map(discoveryMetricHtml)}</div>
    ${excluded ? html`<p class="caption">不要と判断された特許 ${excluded} 件を区別して評価しています。</p>` : ''}
    ${discoveryWarnings(analysis.warnings)}
    ${newTerms.length ? discoveryNewTermsHtml(newTerms) : ''}
    ${nextQueries.length ? discoveryNextQueriesHtml(nextQueries) : ''}</div>`;
}
function discoveryNewTermsHtml(newTerms) {
  return html`<div class="discovery-new-terms"><b>次の探索に使える語</b>
    <div class="discovery-tags">${newTerms.map((value) => html`<span>${esc(value)}</span>`)}</div></div>`;
}
function discoveryNextQueriesHtml(nextQueries) {
  return html`<details class="discovery-query-details" data-discovery-detail="next-queries" open>
    <summary>次の検索案 <span>${nextQueries.length} 件 · EPO OPS形式</span></summary>
    ${nextQueries.map(discoveryQueryHtml)}</details>`;
}
function discoveryResultsHtml(discovery) {
  const plan = discovery.plan,
    analysis = discovery.analysis,
    items = discoveryItems(discovery);
  if (!plan && !analysis)
    return html`<div class="panel empty-state" data-empty="discovery"><span class="empty-state-icon">◎</span>
      <h3>探索計画はまだありません</h3>
      <p>まず探索計画を作り、CSVで検証してください。分類候補は根拠のあるものと探索仮説を分けて表示します。</p></div>`;
  return html`${discoveryLlmProposalHtml(plan)}${discoveryComparisonHtml(items)}
    ${plan ? discoveryPlanPanelHtml(plan) : ''}
    ${analysis ? discoveryAnalysisHtml(analysis) : ''}`;
}
function discoveryArchiveHtml(archive) {
  const id = esc(encodeURIComponent(String(archive.id)));
  return html`<div class="history-item"><span>${esc(archive.keywords || '探索計画')}</span>
    <small>${esc(archive.time || '')}</small>
    <a class="button subtle small" href="/api/discovery/export?archive_id=${id}" download>記録を保存 ↓</a></div>`;
}
function discoveryArchivesHtml(archives) {
  if (!archives.length) return '';
  return html`<div class="discovery-query-details"><h3>以前の計画</h3>
    <p class="caption">計画を作り直す前の公報・分析・検索記録をJSONで保存できます。</p>
    ${archives.map(discoveryArchiveHtml)}</div>`;
}
function discoveryRoundRowHtml(round) {
  return html`<tr><td>${Number(round.round || 0)}</td><td>${(round.queries || []).length}</td>
    <td>${Number(round.new_documents || 0)}</td><td>${Number(round.new_classifications || 0)}</td>
    <td>${Number(round.total_documents || 0)}</td></tr>`;
}
function discoveryRoundQueryHtml(query) {
  const total =
    query.total != null
      ? ` · ヒット総数 ${Number(query.total).toLocaleString()} 件${query.total_is_lower_bound ? '以上' : ''}`
      : '';
  const truncated = query.truncated
    ? html`<p class="caption">検索結果の一部（先頭から取得できた ${Number(query.result_count || 0)} 件）を分析しています。
        ヒット全件を取得した結果ではありません。</p>`
    : '';
  return html`<div class="discovery-query"><div><b>${esc(query.label || query.id || '')}</b>
    <span>取得 ${Number(query.result_count || 0)} 件 · 新規 ${Number(query.new_documents || 0)} 件${total}</span>
    </div>
    <pre>${esc(query.query || '')}</pre>${truncated}</div>`;
}
function discoveryRoundDetailsHtml(round) {
  const number = Number(round.round || 0),
    csvRound = '<p class="caption">CSVに基づく分析ラウンドです。</p>';
  return html`<details class="discovery-query-details" data-discovery-detail="round-${number}">
    <summary>ラウンド ${number} の検索と結果</summary>
    ${(round.queries || []).map(discoveryRoundQueryHtml).join('') || csvRound}</details>`;
}
function discoveryRoundsHtml(rounds) {
  if (!rounds.length) return '';
  return html`<div class="table-scroll"><table><thead><tr><th>ラウンド</th><th>検索数</th><th>新規公報</th>
    <th>新規分類</th><th>累積公報</th></tr></thead><tbody>${rounds.map(discoveryRoundRowHtml)}</tbody></table></div>
    ${rounds.map(discoveryRoundDetailsHtml)}`;
}
function discoveryLogHtml(logs) {
  if (!logs.length) return '';
  return html`<ol class="discovery-log">${logs
    .slice(-40)
    .map(
      (log) => html`<li><time>${esc(log.time || '')}</time><span>${esc(log.message || '')}</span></li>`
    )}</ol>`;
}
function discoveryHistoryHtml(discovery) {
  const rounds = discovery.rounds || [],
    logs = discovery.logs || [],
    archives = discovery.archives || [];
  if (!rounds.length && !logs.length && !archives.length) return '';
  const archiveNote = archives.length ? ` · 以前の計画 ${archives.length} 件` : '';
  return html`<details class="panel discovery-history" data-discovery-detail="history"
    ${discovery.status === 'running' ? 'open' : ''}>
    <summary>探索の記録 <span>${rounds.length} ラウンド${archiveNote}</span></summary>
    ${discoveryRoundsHtml(rounds)}${discoveryLogHtml(logs)}${discoveryArchivesHtml(archives)}</details>`;
}

function discoveryStatusHtml(discovery, snapshot) {
  const statusLabels = {
    idle: '開始前',
    planned: '探索計画を作成済み',
    running: '探索しています',
    done: '分析が完了しました',
    error: '探索を確認してください',
    stopped: '探索を停止しました',
  };
  if (!discovery.status || discovery.status === 'idle') return '';
  const running = discovery.status === 'running',
    stop = '<button id="discovery-stop" class="button subtle small">停止する</button>';
  return html`<div class="discovery-status ${discovery.status === 'error' ? 'error' : ''}">
    <span class="${running ? 'discovery-pulse' : ''}"></span>
    <b>${esc(statusLabels[discovery.status] || discovery.status)}</b>
    ${discovery.last_error ? html`<p>${esc(discovery.last_error)}</p>` : '<p>候補を確認し、必要な範囲を手動探索へ引き継げます。</p>'}
    ${running && snapshot.job?.status === 'running' ? stop : ''}</div>`;
}
function renderDiscovery(root = $('#discovery-content'), snapshot = getState()) {
  if (!root || !snapshot) return;
  const discovery = snapshot.discovery || {};
  if (!$('#discovery-keywords')) {
    discoveryUiState.discoveryDraft ||= {
      keywords: discovery.plan?.keywords || snapshot.keywords || '',
      english_terms: discovery.plan?.user_english_terms || '',
      use_llm: false,
      max_rounds: 2,
      max_queries: 6,
      per_query: 10,
    };
    replaceHtml(root, discoveryFormHtml(discoveryUiState.discoveryDraft));
    discoveryUiState.discoveryResultsSignature = '';
    discoveryUiState.discoveryHistorySignature = '';
    bindDiscoveryForm();
  }
  const resultSignature = JSON.stringify([
    discovery.plan,
    discovery.analysis,
    discoveryUiState.discoveryVisibleCount,
  ]);
  if (resultSignature !== discoveryUiState.discoveryResultsSignature) {
    discoveryUiState.discoveryResultsSignature = resultSignature;
    const allowed = new Set(
      discoveryItems(discovery)
        .filter((item) => item.selectable !== false)
        .map(discoveryKey)
    );
    for (const key of discoveryPicked) if (!allowed.has(key)) discoveryPicked.delete(key);
    const restore = replaceHtml($('#discovery-results'), discoveryResultsHtml(discovery));
    bindDiscoveryResults();
    restore?.();
  }
  const historySignature = JSON.stringify([discovery.rounds, discovery.logs, discovery.archives]);
  if (historySignature !== discoveryUiState.discoveryHistorySignature) {
    discoveryUiState.discoveryHistorySignature = historySignature;
    replaceHtml($('#discovery-history'), discoveryHistoryHtml(discovery));
  }
  replaceHtml($('#discovery-progress'), discoveryStatusHtml(discovery, snapshot));
  on('#discovery-stop', 'click', async () => {
    await api('/job/stop', {});
    toast('停止を依頼しました。実行中の通信が終わると停止します。');
  });
  $('#discovery-manual-count').textContent =
    `手動探索に ${Number(snapshot.patents?.length || 0)} 件${snapshot.demo ? ' · 架空サンプル' : ''}`;
  const opsReady = Boolean(snapshot.settings?.ops_key_set && snapshot.settings?.ops_secret_set);
  $('#discovery-ops-status').textContent = opsReady
    ? 'EPO OPSの認証情報は設定済みです。Proxyが必要な環境では接続設定も確認してください。'
    : 'EPO OPSのキーとシークレットは未設定です。CSVの分析はこのまま利用できます。' +
      '自動検索を使う際に接続設定を行ってください。';
  updateDiscoveryControls();
  renderDiscoveryPlanRequest();
}
function discoveryPlanRequestHtml(request) {
  if (!request) return '';
  if (request.status === 'running')
    return html`<p class="discovery-plan-request pending">探索計画を作成中です。
      提案の検証と保存が終わるまで、現在の計画を表示しています。</p>`;
  if (request.status === 'error')
    return html`<p class="discovery-plan-request error">今回の計画は作成できませんでした：${esc(request.message)}<br>
      表示中の結果は変更前の計画です。</p>`;
  return '';
}
function renderDiscoveryPlanRequest() {
  const host = $('#discovery-llm-request-status');
  if (!host) return;
  replaceHtml(host, discoveryPlanRequestHtml(discoveryUiState.discoveryPlanRequestState));
}
function discoveryPlanNote() {
  if (!getState().discovery?.plan)
    return '① 探索計画を作成 → ② CSVで検証 → ③ 次のCSVを追加、の順に進めます。';
  const lead = discoveryPlanChanged()
    ? 'テーマ・英語検索語が変更されています。「探索計画を作る」で反映してからCSVを分析してください。'
    : '現在の探索計画でCSVを分析します。';
  return `${lead} 計画を作り直すと新しい蓄積を開始し、以前の計画は記録に保存します。`;
}
function updateDiscoveryControls() {
  const unavailable = discoveryUiState.discoveryRequestPending || getState().job?.status === 'running';
  const needsPlan = !getState().discovery?.plan || discoveryPlanChanged();
  $$('#discovery-content .discovery-action').forEach((button) => {
    button.disabled = Boolean(unavailable);
  });
  if ($('#discovery-run'))
    $('#discovery-run').disabled = Boolean(
      unavailable || !getState().settings?.ops_key_set || !getState().settings?.ops_secret_set || needsPlan
    );
  if ($('#discovery-analyze'))
    $('#discovery-analyze').disabled = Boolean(unavailable || !getState().patents?.length || needsPlan);
  if ($('#discovery-import'))
    $('#discovery-import').disabled = Boolean(
      unavailable || !$('#discovery-csv-file')?.files?.length || needsPlan
    );
  if ($('#discovery-plan-note')) $('#discovery-plan-note').textContent = discoveryPlanNote();
  const allowed = new Set(
    discoveryItems()
      .filter((item) => item.selectable !== false)
      .map(discoveryKey)
  );
  const selected = [...discoveryPicked].filter((key) => allowed.has(key));
  if ($('#discovery-adopt')) $('#discovery-adopt').disabled = Boolean(unavailable || !selected.length);
  if ($('#discovery-picked-count')) $('#discovery-picked-count').textContent = selected.length;
  $$('[data-discovery-key]').forEach((node) => {
    const item = discoveryItems().find((item) => discoveryKey(item) === node.dataset.discoveryKey);
    node.disabled = Boolean(discoveryUiState.discoveryAdopting || item?.selectable === false);
  });
  $$('.discovery-star').forEach((node) =>
    node.setAttribute(
      'aria-disabled',
      String(discoveryUiState.discoveryAdopting || node.dataset.discoverySelectable === 'false')
    )
  );
}
async function requestDiscovery(path, body, options) {
  if (discoveryUiState.discoveryRequestPending || getState().job?.status === 'running') return false;
  captureDiscoveryDraft();
  discoveryUiState.discoveryRequestPending = true;
  updateDiscoveryControls();
  if (path === '/discovery/plan') {
    discoveryUiState.discoveryPlanRequestState = { status: 'running' };
    renderDiscoveryPlanRequest();
  }
  try {
    const result = await api(path, body, options);
    mergeDiscoveryResponse(result.job_id ? await api('/state') : result);
    if (path === '/discovery/plan') discoveryUiState.discoveryPlanRequestState = null;
    updateChrome();
    return true;
  } catch (error) {
    if (path === '/discovery/plan') {
      discoveryUiState.discoveryPlanRequestState = {
        status: 'error',
        message: error.message || String(error),
      };
      renderDiscoveryPlanRequest();
    }
    throw error;
  } finally {
    discoveryUiState.discoveryRequestPending = false;
    if (appState.activeTab === 'discovery') renderDiscovery();
  }
}
function discoveryLimits(draft) {
  const limits = { max_rounds: [1, 3], max_queries: [1, 12], per_query: [5, 25] },
    result = {};
  for (const [name, [min, max]] of Object.entries(limits)) {
    const value = Number(draft[name]);
    if (!Number.isInteger(value) || value < min || value > max)
      throw new Error('検索の上限は、表示されている範囲の整数で指定してください。');
    result[name] = value;
  }
  return result;
}
function bindDiscoveryForm() {
  $$('#discovery-content input:not([type=file]), #discovery-content textarea').forEach((node) =>
    listen(
      node,
      'input',
      () => {
        captureDiscoveryDraft();
        updateDiscoveryControls();
      },
      undefined,
      'discovery-ui.js:15'
    )
  );
  on('#discovery-plan', 'click', async () => {
    captureDiscoveryDraft();
    if (!discoveryUiState.discoveryDraft.keywords.trim())
      throw new Error('調べたい技術・キーワードを入力してください。');
    if (
      await requestDiscovery('/discovery/plan', {
        keywords: discoveryUiState.discoveryDraft.keywords,
        english_terms: discoveryUiState.discoveryDraft.english_terms,
        use_llm: discoveryUiState.discoveryDraft.use_llm,
      })
    )
      toast('探索計画を作成しました。CSVで候補を検証できます。');
  });
  on('#discovery-analyze', 'click', async () => {
    if (await requestDiscovery('/discovery/analyze', {}))
      toast('取込済みの特許から、分類候補を見直しました。');
  });
  on('#discovery-csv-file', 'change', updateDiscoveryControls);
  on('#discovery-import', 'click', async () => {
    const file = $('#discovery-csv-file')?.files?.[0];
    if (!file) throw new Error('追加するCSVを選んでください。');
    if (file.size > 20 * 1024 * 1024) throw new Error('CSVは20 MB以下にしてください。');
    if (
      await requestDiscovery('/discovery/import', undefined, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: file,
      })
    ) {
      $('#discovery-csv-file').value = '';
      updateDiscoveryControls();
      toast('CSVを追加して、分類候補と次の検索案を見直しました。');
    }
  });
  on('#discovery-run', 'click', async () => {
    captureDiscoveryDraft();
    if (await requestDiscovery('/discovery/run', discoveryLimits(discoveryUiState.discoveryDraft)))
      toast('自動検索を開始しました。上限内で探索を繰り返します。');
  });
  on('#discovery-settings', 'click', () => {
    captureDiscoveryDraft();
    setTab('settings');
    $('#ops-key')?.focus();
  });
}
function bindDiscoveryResults() {
  $$('.discovery-copy-query').forEach((node) =>
    listen(
      node,
      'click',
      run(async () => {
        await navigator.clipboard.writeText(node.dataset.discoveryQuery);
        toast('EPO OPS形式の検索式をコピーしました。');
      }),
      undefined,
      'discovery-ui.js:16'
    )
  );
  bindDiscoveryComparison();
  on('#discovery-clear', 'click', () => {
    discoveryPicked.clear();
    updateDiscoveryPickedDisplay();
    if (discoveryCompare.selectedOnly) renderDiscoveryComparisonBody();
  });
  on('#discovery-adopt', 'click', adoptDiscoveryClasses);
}
async function adoptDiscoveryClasses() {
  if (discoveryUiState.discoveryRequestPending || getState().job?.status === 'running') return;
  const allowed = new Set(
    discoveryItems()
      .filter((item) => item.selectable !== false)
      .map(discoveryKey)
  );
  const keys = [...discoveryPicked].filter((key) => allowed.has(key));
  if (!keys.length) throw new Error('送る分類にチェックを入れてください。');
  captureDiscoveryDraft();
  captureClassDraft();
  discoveryUiState.discoveryRequestPending = true;
  discoveryUiState.discoveryAdopting = true;
  updateDiscoveryControls();
  try {
    await appState.selectionSave;
    mergeDiscoveryResponse(await api('/discovery/adopt', { keys }), true);
    updateChrome();
    discoveryPicked.clear();
    discoveryUiState.discoveryResultsSignature = '';
    setTab('explore');
    if (appState.step !== 0) setStep(0);
    toast(`${keys.length} 件を候補へ追加しました。検索に使う分類を選んでください。`);
  } finally {
    discoveryUiState.discoveryRequestPending = false;
    discoveryUiState.discoveryAdopting = false;
    if (appState.activeTab === 'discovery') renderDiscovery();
  }
}

export {
  discoveryPicked,
  discoveryCompare,
  discoveryItems,
  captureDiscoveryDraft,
  discoveryPlanChanged,
  discoveryLlmProposalHtml,
  discoveryHasEvidence,
  discoveryCandidateHtml,
  discoveryTitle,
  discoveryBilingualHtml,
  discoveryEvidenceType,
  discoveryEvidenceLabel,
  discoveryFilteredItems,
  discoveryComparisonHtml,
  discoveryComparisonBodyHtml,
  discoveryInspectorHtml,
  discoveryMapHtml,
  renderDiscoveryMap,
  discoveryPointInPolygon,
  applyDiscoveryLasso,
  toggleDiscoveryPicked,
  updateDiscoveryPickedDisplay,
  discoveryQueryHtml,
  discoveryResultsHtml,
  discoveryHistoryHtml,
  renderDiscovery,
  updateDiscoveryControls,
  requestDiscovery,
  discoveryLimits,
  adoptDiscoveryClasses,
};

export { renderDiscovery as render };
