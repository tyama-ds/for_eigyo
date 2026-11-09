import {
  debounce,
  replaceHtml,
  listen,
  esc,
  html,
  $,
  $$,
  api,
  safeUrl,
  on,
  toast,
  download,
  getState,
  run,
  setState,
} from './core.js';
import { setStep, setTab } from './app.js';
import { appState, outputEdits } from './ui-session.js';
import { classificationChangesHtml } from './refinement-ui.js';
import {
  researchWorkbenchState,
  captureResearchDraft,
  renderResearchWorkbench,
} from './research-workbench.js';
import { researchDraftFlush } from './research-draft-store.js';
import { updateChrome } from './workspace-chrome.js';

const scheduleOutput = debounce(requestOutput, 350);

/* Search-service output UI. Shares the application state; exports do not mutate a saved query. */
function queryFormatOptionHtml(f) {
  const selected = appState.outputFormat === f.id ? 'selected' : '';
  return html`<option value="${esc(f.id)}" ${selected}>${esc(f.name)}</option>`;
}
function queryOutputTermHtml(t, edits) {
  return html`<label><span>${esc(t)}</span><input class="output-term" data-term="${esc(t)}"
    value="${esc(edits[t] ?? t)}" aria-label="出力用語句 ${esc(t)}" maxlength="500"></label>`;
}
function queryOutputControlsHtml() {
  return html`<label class="output-target-label">出力する検索サービス<select id="output-format">
    ${appState.formatCatalog.map(queryFormatOptionHtml)}</select></label>
    <div class="output-options"><label>分類範囲<select id="output-descendants">
    <option value="code">DB標準のコード指定</option><option value="descendants">下位分類も明示する</option></select></label>
    <label>検索する本文の言語<select id="output-language"><option value="auto">語句から自動判定</option>
    <option value="en">英語</option><option value="ja">日本語</option></select></label></div>
    <div id="output-feedback" aria-live="polite"></div><pre class="expression" id="expression">変換しています…</pre>
    <div id="output-instructions" class="notice"></div>`;
}
function queryTermEditorHtml(terms, edits) {
  return html`<details class="output-term-editor"><summary>出力用の語句を編集・英語に置き換える</summary>
    <p class="caption">各行は元の1語句に対応します。空白を含む入力も1つのフレーズとして出力します。元の検索式は変更しません。</p>
    <div class="output-term-grid">${terms.map((t) => queryOutputTermHtml(t, edits))}</div>
    <button class="button small" id="reset-output-terms" type="button">元の語句に戻す</button></details>
    <label class="check output-risk"><input id="omit-unsupported" type="checkbox">
    未対応分類を今回の出力だけから外す（検索範囲が変わります）</label>
    <label class="check output-risk"><input id="allow-unverified" type="checkbox">未確認分類を確認のうえ含める</label>
    <div class="output-meta" id="output-meta"></div>
    <div class="button-row output-actions">
    <a class="button" id="open-search-service" href="#" target="_blank" rel="noopener noreferrer"
    >検索サービスを開く ↗</a>
    <button class="button subtle" id="download-query" disabled>検索条件を保存 ↓</button></div>`;
}
/* 技術動向調査報告書の資料編にならった表（調査条件・集合番号つき検索式）。 */
const REPORT_DEFAULTS = {
  period_from: '',
  period_to: '',
  basis: 'priority',
  regions: 'JP, US, EP, CN, KR, WO',
  search_date: '',
  unit: 'family',
  database: '',
  local_hits: true,
};
function reportOptions() {
  try {
    return { ...REPORT_DEFAULTS, ...JSON.parse(localStorage.getItem('patent-report-options') || '{}') };
  } catch {
    return { ...REPORT_DEFAULTS };
  }
}
function reportSelectHtml(id, label, choices, current) {
  const options = choices.map(
    ([value, text]) => html`<option value="${value}" ${current === value ? 'selected' : ''}>${text}</option>`
  );
  return html`<label>${label}<select id="${id}">${options}</select></label>`;
}
function queryReportHtml() {
  const o = reportOptions();
  return html`<details class="output-report" id="output-report"><summary>技術動向調査の報告書形式（調査条件・検索式表）</summary>
    <p class="caption">特許庁の特許出願技術動向調査報告書の資料編と同じ構成で、調査条件の表と集合番号つきの検索式表を作ります。
    検索式の構文は上で選んだ検索サービスに従います。ヒット件数は検索実行後に記入する欄です。</p>
    <div class="report-options">
    <label>対象期間（開始年）<input id="report-period-from" type="number" min="1900" max="2100"
      value="${esc(o.period_from)}" placeholder="2013"></label>
    <label>対象期間（終了年）<input id="report-period-to" type="number" min="1900" max="2100"
      value="${esc(o.period_to)}" placeholder="2022"></label>
    ${reportSelectHtml(
      'report-basis',
      '期間の基準',
      [
        ['priority', '優先権主張年'],
        ['application', '出願年'],
        ['publication', '公開年'],
      ],
      o.basis
    )}
    <label>対象国・地域<input id="report-regions" value="${esc(o.regions)}"
      placeholder="JP, US, EP, CN, KR, WO"></label>
    <label>検索日<input id="report-search-date" type="date" value="${esc(o.search_date)}"></label>
    ${reportSelectHtml(
      'report-unit',
      '集計単位',
      [
        ['family', 'パテントファミリー単位'],
        ['publication', '公報単位'],
      ],
      o.unit
    )}
    <label>データベース名（任意）<input id="report-database" value="${esc(o.database)}"
      placeholder="Derwent Innovation（DWPI 付きコレクション）"></label>
    <label class="check"><input id="report-local-hits" type="checkbox" ${o.local_hits ? 'checked' : ''}>
      取り込み済みCSVでの該当数を参考列に出す</label></div>
    <div class="button-row"><button class="button" id="make-report" type="button">表を作成</button>
    <button class="button small" id="copy-report" type="button" disabled>Markdown をコピー ⧉</button>
    <button class="button small subtle" id="download-report" type="button" disabled>TSV を保存 ↓</button></div>
    <div id="report-feedback" aria-live="polite"></div>
    <pre class="expression report-preview" id="report-preview">「表を作成」で、現在の検索式から表を作ります。</pre></details>`;
}
function reportRequestBody(q) {
  const o = {
    period_from: $('#report-period-from')?.value || '',
    period_to: $('#report-period-to')?.value || '',
    basis: $('#report-basis')?.value || 'priority',
    regions: $('#report-regions')?.value || '',
    search_date: $('#report-search-date')?.value || '',
    unit: $('#report-unit')?.value || 'family',
    database: $('#report-database')?.value || '',
    local_hits: $('#report-local-hits')?.checked ?? true,
  };
  try {
    localStorage.setItem('patent-report-options', JSON.stringify(o));
  } catch {
    /* per-viewer convenience only */
  }
  const { local_hits, ...options } = o;
  return {
    query_id: q.id,
    format: $('#output-format').value,
    options,
    local_hits,
    replacements: Object.fromEntries($$('.output-term').map((el) => [el.dataset.term, el.value])),
    omit_unsupported: $('#omit-unsupported').checked,
    allow_unverified: $('#allow-unverified').checked,
    descendants: $('#output-descendants').value === 'descendants',
    language: $('#output-language').value,
  };
}
async function requestReport(q) {
  const feedback = $('#report-feedback'),
    preview = $('#report-preview');
  if (!preview) return;
  $('#copy-report').disabled = true;
  $('#download-report').disabled = true;
  try {
    const result = await api('/query/report', reportRequestBody(q));
    if ($('#query-output')?.dataset.queryId !== q.id) return;
    appState.currentReport = result;
    preview.textContent = result.markdown;
    const problems = result.problems.length
      ? html`<div class="output-problems"><b>最終式の出力を保留しています</b>
          <ul>${result.problems.map((p) => html`<li>${esc(p)}</li>`)}</ul></div>`
      : '';
    replaceHtml(feedback, problems);
    $('#copy-report').disabled = false;
    $('#download-report').disabled = false;
  } catch (error) {
    replaceHtml(feedback, html`<div class="output-problems">${esc(error.message)}</div>`);
  }
}
function bindReport(q) {
  on('#make-report', 'click', () => requestReport(q));
  on('#copy-report', 'click', async () => {
    const r = appState.currentReport;
    if (!r || r.query_id !== q.id) throw new Error('先に表を作成してください。');
    await navigator.clipboard.writeText(r.markdown);
    toast('報告書形式の表（Markdown）をコピーしました。');
  });
  on('#download-report', 'click', () => {
    const r = appState.currentReport;
    if (!r || r.query_id !== q.id) throw new Error('先に表を作成してください。');
    download(`patent-query-v${q.version}-report-${r.syntax}.tsv`, r.tsv);
  });
}
function querySourceHtml(q) {
  const logic = q.boolean_tree
    ? '観点内OR・観点間AND、または取り込み式の論理構造を保持しています。'
    : 'キーワードはAND、分類はOR。追加語はOR群をANDで結び、除外語はNOTにします。';
  const coverage = q.known_keep_coverage;
  return html`<details class="source-expression"><summary>元の設計式・変更内容</summary>
    <pre class="expression">${esc(q.expression)}</pre><p class="caption">${logic}</p>
    ${q.removed_terms?.length ? queryRemovedTermsHtml(q.removed_terms) : ''}
    ${coverage ? queryCoverageHtml(coverage) : ''}</details>`;
}
function queryRemovedTermsHtml(terms) {
  return html`<p class="caption">前回から解除した語：${terms.map(esc).join('、')}</p>`;
}
function queryCoverageHtml(coverage) {
  return html`<p class="caption">追加語の条件で残る既知の必要特許：${coverage.retained} / ${coverage.total} 件
    （タイトル・要約の文字列照合）。</p>`;
}
function queryCard(q) {
  if (!q)
    return html`<div class="empty-state"><div class="empty-state-icon">⌘</div><h3>分類を選ぶと初案を作成できます</h3>
      <button class="button primary" id="back-class">Step 00へ →</button></div>`;
  if (!appState.formatCatalog.some((f) => f.id === appState.outputFormat)) appState.outputFormat = 'jplatpat';
  const terms = q.term_catalog || [
      ...new Set([...(q.keywords || []), ...(q.include_terms || []), ...(q.exclude_terms || [])]),
    ],
    edits = outputEdits.get(q.id) || {};
  return html`<div id="query-output" data-query-id="${esc(q.id)}"><div class="panel-heading">
    <h3>検索式 <span class="pill">v${q.version} · ${esc(q.type)}</span></h3>
    <button class="button small" id="copy-query" disabled>この形式をコピー ⧉</button></div>
    ${queryOutputControlsHtml()}
    ${queryTermEditorHtml(terms, edits)}
    ${queryReportHtml()}
    ${classificationChangesHtml(q.classification_changes)}${querySourceHtml(q)}</div>`;
}
function outputPending() {
  appState.currentOutput = null;
  $('#copy-query')?.setAttribute('disabled', '');
  $('#download-query')?.setAttribute('disabled', '');
  for (const selector of ['#output-meta', '#output-instructions', '#output-feedback'])
    if ($(selector)) $(selector).textContent = '';
  $('#open-search-service')?.removeAttribute('href');
}
async function requestOutput(q) {
  if (!$('#query-output') || $('#query-output').dataset.queryId !== q.id) return;
  const serial = ++appState.exportRequest;
  outputPending();
  const replacements = Object.fromEntries($$('.output-term').map((el) => [el.dataset.term, el.value]));
  outputEdits.set(q.id, replacements);
  const body = {
    query_id: q.id,
    format: $('#output-format').value,
    replacements,
    omit_unsupported: $('#omit-unsupported').checked,
    allow_unverified: $('#allow-unverified').checked,
    descendants: $('#output-descendants').value === 'descendants',
    language: $('#output-language').value,
  };
  $('#expression').textContent = '変換しています…';
  try {
    const result = await api('/query/export', body);
    if (serial !== appState.exportRequest || $('#query-output')?.dataset.queryId !== q.id) return;
    appState.currentOutput = result;
    $('#expression').textContent = result.can_copy
      ? result.expression
      : '条件を確認すると、この形式の検索式を表示します。';
    replaceHtml($('#output-feedback'), outputFeedbackHtml(result));
    $('#output-instructions').textContent = result.instructions;
    replaceHtml($('#output-meta'), outputMetaHtml(result));
    const url = safeUrl(result.url);
    if (url) $('#open-search-service').href = url;
    else $('#open-search-service').removeAttribute('href');
    $('#open-search-service').textContent = '検索サービスを開く ↗';
    $('#copy-query').disabled = !result.can_copy;
    $('#download-query').disabled = !result.can_copy;
  } catch (error) {
    if (serial !== appState.exportRequest || $('#query-output')?.dataset.queryId !== q.id) return;
    $('#expression').textContent = '出力を保留しています。';
    replaceHtml($('#output-feedback'), `<div class="output-problems">${esc(error.message)}</div>`);
  }
}
function bindQuery(q) {
  on('#back-class', 'click', () => setStep(0));
  if (!q) return;
  requestOutput(q);
  bindReport(q);
  on('#output-format', 'change', async () => {
    appState.outputFormat = $('#output-format').value;
    localStorage.setItem('patent-output-format', appState.outputFormat);
    $('#omit-unsupported').checked = false;
    $('#allow-unverified').checked = false;
    $('#output-descendants').value = 'code';
    await requestOutput(q);
  });
  for (const selector of [
    '#output-descendants',
    '#output-language',
    '#omit-unsupported',
    '#allow-unverified',
  ])
    on(selector, 'change', () => requestOutput(q));
  scheduleOutput.cancel();
  $$('.output-term').forEach((el) =>
    listen(
      el,
      'input',
      () => {
        ++appState.exportRequest;
        outputPending();
        scheduleOutput(q);
      },
      undefined,
      'query-output.js:1'
    )
  );
  on('#reset-output-terms', 'click', async () => {
    $$('.output-term').forEach((el) => (el.value = el.dataset.term));
    await requestOutput(q);
  });
  on('#copy-query', 'click', async () => {
    if (!appState.currentOutput?.can_copy || appState.currentOutput.query_id !== q.id)
      throw new Error('出力条件を確認してください。');
    await navigator.clipboard.writeText(appState.currentOutput.expression);
    toast(appState.currentOutput.name + ' 形式をコピーしました。');
  });
  on('#download-query', 'click', () => {
    const r = appState.currentOutput;
    if (!r?.can_copy || r.query_id !== q.id) throw new Error('出力条件を確認してください。');
    download(`patent-query-v${q.version}-${r.format_id}.txt`, queryDownloadText(q, r));
  });
}
function outputFeedbackHtml(result) {
  const problems = result.problems.length
    ? html`<div class="output-problems"><b>出力を保留しています</b>
        <ul>${result.problems.map((p) => html`<li>${esc(p)}</li>`)}</ul></div>`
    : '';
  return problems + (result.warnings.length ? outputWarningsHtml(result) : '');
}
function outputWarningsHtml(result) {
  const open = result.omitted_classifications.length || result.replacements.length ? 'open' : '';
  return html`<details class="output-warnings" ${open}>
    <summary>変換上の確認事項 · ${result.warnings.length} 件</summary>
    <ul>${result.warnings.map((w) => html`<li>${esc(w)}</li>`)}</ul></details>`;
}
function outputSourceHtml(s) {
  const url = safeUrl(s.url);
  return url
    ? html`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)} ↗</a>`
    : html`<span>${esc(s.title)}</span>`;
}
function outputMetaHtml(result) {
  return html`<div class="query-field-row"><span>対象フィールド</span><b>${esc(result.text_scope)}</b></div>
    <p class="caption">${esc(result.classification_note)}</p>
    <p class="caption">${esc(result.verification)} · 照合日 ${esc(result.verified_at)}</p>
    <div class="output-sources">${result.sources.map(outputSourceHtml)}</div>`;
}
function queryDownloadText(q, r) {
  return [
    `${r.name} / v${q.version}`,
    '',
    r.expression,
    '',
    r.instructions,
    `対象: ${r.text_scope}`,
    `分類: ${r.classification_note}`,
    '',
    r.warnings.join('\n'),
    '',
    `${r.verification} (${r.verified_at})`,
    r.sources.map((s) => s.title + ' ' + s.url).join('\n'),
  ].join('\n');
}
function historyItemHtml(q) {
  return html`<div class="history-item"><span class="pill">v${q.version}</span><span>${esc(q.type)}</span>
    <small>${esc(q.created_at)}</small>
    <button class="button subtle history-view" data-id="${esc(q.id)}">形式を選んで出力</button></div>`;
}
function historyHtml() {
  const queries = getState().queries;
  if (!queries.length) return '';
  return html`<div class="panel query-history"><div class="panel-heading"><h3>検索式の履歴</h3>
    <span class="caption">${queries.length} バージョン · 過去の式も形式を選んで出力</span></div>
    ${queries.slice().reverse().map(historyItemHtml)}</div>`;
}
function bindHistory() {
  $$('.history-view').forEach((el) =>
    listen(
      el,
      'click',
      () => {
        appState.viewedQueryId = el.dataset.id;
        setStep(1);
      },
      undefined,
      'query-output.js:2'
    )
  );
}

function uploadStatusHtml(snapshot) {
  if (!snapshot.patents.length) return '';
  const info = snapshot.import_info;
  const demo = snapshot.demo ? '（架空のサンプル）' : '';
  return html`<div class="upload-status">✓ ${snapshot.patents.length} 件を読み込み済み ${demo}
    ${info ? html`<br>重複除外 ${info.duplicates} 件 · 名称なし ${info.skipped} 件` : ''}</div>
    <button class="button primary wide" id="open-map">特許の地図を見る →</button>`;
}
function csvImportPanelHtml(snapshot) {
  return html`<div class="panel"><h2>検索結果CSVの取り込み</h2>
    <p class="muted">検索サイトで調べた特許群をCSVで渡してください。近い技術ごとにマップをつくります。</p>
    <label>取り込み方法<select id="csv-import-mode"><option value="replace">現在の特許群を置き換える</option>
    <option value="merge">追加して既存の判定を保持する</option></select></label>
    <div class="dropzone" id="dropzone" role="button" tabindex="0" aria-label="特許CSVを選択">
    <span class="upload-symbol">↓</span><b>CSVをここにドロップ</b><p>またはクリックしてファイルを選択</p>
    <p>UTF-8 / Shift-JIS · 20MB・5,000行まで</p></div>
    <input id="csv-file" type="file" accept=".csv,.tsv,text/csv" class="hidden"><h3>読み込める項目</h3>
    <div class="schema"><span>発明の名称 *</span><span>要約</span><span>公報番号</span><span>出願人</span><span>IPC</span>
    <span>FI</span><span>F-term</span><span>公開日</span><span>ファミリーID</span></div>
    <p class="caption">* 発明の名称は必須です。日本語・英語の一般的な列名を自動認識し、不明な場合は割り当てを表示します。
    同一公報番号は重複を除きます。</p>${uploadStatusHtml(snapshot)}
    <p class="caption">「置き換え」は現在の特許群と判定を置き換えます。「追加」は同じ公報・内容の判定を保持し、新しい特許を追加します。
    FIとFタームは別項目で取り込みます。</p></div>`;
}
function renderQuery(host = $('#step-content'), snapshot = getState()) {
  const q = snapshot.queries.find((q) => q.id === appState.viewedQueryId) || snapshot.queries.at(-1);
  replaceHtml(
    host,
    html`<div class="query-layout"><div class="panel">${queryCard(q)}</div>
      ${csvImportPanelHtml(snapshot)}</div>${historyHtml()}`
  );
  bindQuery(q);
  bindHistory();
  on('#open-map', 'click', () => setStep(2));
  bindDropzone();
}
async function uploadFile(file, mapping = {}) {
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) throw new Error('CSVは20MB以下にしてください。');
  appState.pendingFile = file;
  appState.pendingImportMode = researchWorkbenchState.researchImportPending
    ? 'merge'
    : $('#csv-import-mode')?.value || appState.pendingImportMode;
  toast('CSVを読み込み、特許を配置しています…');
  if (appState.activeTab === 'workbench') captureResearchDraft();
  researchDraftFlush();
  const data = await api(
    '/upload?merge=' +
      (appState.pendingImportMode === 'merge') +
      '&mapping=' +
      encodeURIComponent(JSON.stringify(mapping)),
    undefined,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: await file.arrayBuffer(),
    }
  );
  if (data.needs_mapping) {
    showMapping(data);
    return;
  }
  setState(data);
  appState.picked.clear();
  $('#mapping-dialog').close();
  if (researchWorkbenchState.researchImportPending) {
    researchWorkbenchState.researchImportPending = false;
    setTab('workbench');
    renderResearchWorkbench();
    updateChrome();
  } else setStep(2);
  toast(`${getState().patents.length} 件を読み込みました。`);
}
function bindDropzone() {
  on('#dropzone', 'click', () => $('#csv-file').click());
  on('#dropzone', 'keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      $('#csv-file').click();
    }
  });
  on('#csv-file', 'change', (e) => uploadFile(e.target.files[0]));
  const dz = $('#dropzone');
  listen(
    dz,
    'dragover',
    (e) => {
      e.preventDefault();
      dz.classList.add('dragover');
    },
    undefined,
    'query-output.js:3'
  );
  listen(dz, 'dragleave', () => dz.classList.remove('dragover'), undefined, 'query-output.js:4');
  listen(
    dz,
    'drop',
    run(async (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
      await uploadFile(e.dataTransfer.files[0]);
    }),
    undefined,
    'query-output.js:5'
  );
}
function mappingFieldHtml(k, v, data) {
  const options = data.headers.map(
    (h) => html`<option value="${esc(h)}" ${data.mapping[k] === h ? 'selected' : ''}>${esc(h)}</option>`
  );
  return html`<label>${v}<select data-field="${k}" ${k === 'title' ? 'required' : ''}>
    <option value="">割り当てなし</option>${options}</select></label>`;
}
function showMapping(data) {
  const fields = {
    title: '発明の名称 *',
    abstract: '要約',
    id: '公報番号',
    applicant: '出願人',
    ipc: 'IPC',
    fi: 'FI',
    fterm: 'F-term',
    cpc: 'CPC',
    year: '公開日',
    family: 'ファミリーID',
  };
  replaceHtml(
    $('#mapping-fields'),
    Object.entries(fields)
      .map(([k, v]) => mappingFieldHtml(k, v, data))
      .join('')
  );
  $('#mapping-dialog').showModal();
}

export { queryCard, requestOutput, bindQuery, historyHtml, bindHistory, renderQuery, uploadFile };

export function initCsvImport() {
  on('#cancel-mapping', 'click', () => {
    researchWorkbenchState.researchImportPending = false;
    $('#mapping-dialog').close();
  });
  on('#mapping-form', 'submit', async (e) => {
    e.preventDefault();
    const mapping = Object.fromEntries($$('#mapping-fields select').map((s) => [s.dataset.field, s.value]));
    await uploadFile(appState.pendingFile, mapping);
  });
}

export { renderQuery as render };
