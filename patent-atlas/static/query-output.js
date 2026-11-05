import {
  debounce,
  replaceHtml,
  listen,
  esc,
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
function queryCard(q) {
  if (!q)
    return '<div class="empty-state"><div class="empty-state-icon">⌘</div><h3>分類を選んで初案をつくりましょう。</h3><button class="button primary" id="back-class">Step 00へ →</button></div>';
  if (!appState.formatCatalog.some((f) => f.id === appState.outputFormat)) appState.outputFormat = 'jplatpat';
  const terms = q.term_catalog || [
      ...new Set([...(q.keywords || []), ...(q.include_terms || []), ...(q.exclude_terms || [])]),
    ],
    edits = outputEdits.get(q.id) || {};
  return `<div id="query-output" data-query-id="${esc(q.id)}"><div class="panel-heading"><h3>検索式 <span class="pill">v${q.version} · ${esc(q.type)}</span></h3><button class="button small" id="copy-query" disabled>この形式をコピー ⧉</button></div>
 <label class="output-target-label">出力する検索サービス<select id="output-format">${appState.formatCatalog.map((f) => `<option value="${esc(f.id)}" ${appState.outputFormat === f.id ? 'selected' : ''}>${esc(f.name)}</option>`).join('')}</select></label>
 <div class="output-options"><label>分類範囲<select id="output-descendants"><option value="code">DB標準のコード指定</option><option value="descendants">下位分類も明示する</option></select></label><label>検索する本文の言語<select id="output-language"><option value="auto">語句から自動判定</option><option value="en">英語</option><option value="ja">日本語</option></select></label></div>
 <div id="output-feedback" aria-live="polite"></div><pre class="expression" id="expression">変換しています…</pre><div id="output-instructions" class="notice"></div>
 <details class="output-term-editor"><summary>出力用の語句を編集・英語に置き換える</summary><p class="caption">各行は元の1語句に対応します。空白を含む入力も1つのフレーズとして出力します。元の検索式は変更しません。</p><div class="output-term-grid">${terms.map((t, i) => `<label><span>${esc(t)}</span><input class="output-term" data-term="${esc(t)}" value="${esc(edits[t] ?? t)}" aria-label="出力用語句 ${esc(t)}" maxlength="500"></label>`).join('')}</div><button class="button small" id="reset-output-terms" type="button">元の語句に戻す</button></details>
 <label class="check output-risk"><input id="omit-unsupported" type="checkbox">未対応分類を今回の出力だけから外す（検索範囲が変わります）</label><label class="check output-risk"><input id="allow-unverified" type="checkbox">未確認分類を確認のうえ含める</label>
 <div class="output-meta" id="output-meta"></div><div class="button-row output-actions"><a class="button" id="open-search-service" href="#" target="_blank" rel="noopener noreferrer">検索サービスを開く ↗</a><button class="button subtle" id="download-query" disabled>検索条件を保存 ↓</button></div>
 ${classificationChangesHtml(q.classification_changes)}<details class="source-expression"><summary>元の設計式・変更内容</summary><pre class="expression">${esc(q.expression)}</pre><p class="caption">${q.boolean_tree ? '観点内OR・観点間AND、または取り込み式の論理構造を保持しています。' : 'キーワードはAND、分類はOR。追加語はOR群をANDで結び、除外語はNOTにします。'}</p>${q.removed_terms?.length ? `<p class="caption">前回から解除した語：${q.removed_terms.map(esc).join('、')}</p>` : ''}${q.known_keep_coverage ? `<p class="caption">追加語の条件で残る既知の必要特許：${q.known_keep_coverage.retained} / ${q.known_keep_coverage.total} 件（タイトル・要約の文字列照合）。</p>` : ''}</details></div>`;
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
    replaceHtml(
      $('#output-feedback'),
      (result.problems.length
        ? `<div class="output-problems"><b>出力を保留しています</b><ul>${result.problems.map((p) => `<li>${esc(p)}</li>`).join('')}</ul></div>`
        : '') +
        (result.warnings.length
          ? `<details class="output-warnings" ${result.omitted_classifications.length || result.replacements.length ? 'open' : ''}><summary>変換上の確認事項 · ${result.warnings.length} 件</summary><ul>${result.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></details>`
          : '')
    );
    $('#output-instructions').textContent = result.instructions;
    replaceHtml(
      $('#output-meta'),
      `<div class="query-field-row"><span>対象フィールド</span><b>${esc(result.text_scope)}</b></div><p class="caption">${esc(result.classification_note)}</p><p class="caption">${esc(result.verification)} · 照合日 ${esc(result.verified_at)}</p><div class="output-sources">${result.sources
        .map((s) => {
          const url = safeUrl(s.url);
          return url
            ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)} ↗</a>`
            : `<span>${esc(s.title)}</span>`;
        })
        .join('')}</div>`
    );
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
    download(
      `patent-query-v${q.version}-${r.format_id}.txt`,
      `${r.name} / v${q.version}\n\n${r.expression}\n\n${r.instructions}\n対象: ${r.text_scope}\n分類: ${r.classification_note}\n\n${r.warnings.join('\n')}\n\n${r.verification} (${r.verified_at})\n${r.sources.map((s) => s.title + ' ' + s.url).join('\n')}`
    );
  });
}
function historyHtml() {
  return getState().queries.length
    ? `<div class="panel query-history"><div class="panel-heading"><h3>検索式の履歴</h3><span class="caption">${getState().queries.length} バージョン · 過去の式も形式を選んで出力</span></div>${getState()
        .queries.slice()
        .reverse()
        .map(
          (q) =>
            `<div class="history-item"><span class="pill">v${q.version}</span><span>${esc(q.type)}</span><small>${esc(q.created_at)}</small><button class="button subtle history-view" data-id="${esc(q.id)}">形式を選んで出力</button></div>`
        )
        .join('')}</div>`
    : '';
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

function renderQuery(host = $('#step-content'), snapshot = getState()) {
  const q = snapshot.queries.find((q) => q.id === appState.viewedQueryId) || snapshot.queries.at(-1);
  replaceHtml(
    host,
    `<div class="query-layout"><div class="panel">${queryCard(q)}</div><div class="panel"><span class="eyebrow">BRING YOUR RESULTS</span><h2>検索結果を、次のヒントに。</h2><p class="muted">検索サイトで調べた特許群をCSVで渡してください。近い技術ごとにマップをつくります。</p><label>取り込み方法<select id="csv-import-mode"><option value="replace">現在の特許群を置き換える</option><option value="merge">追加して既存の判定を保持する</option></select></label><div class="dropzone" id="dropzone" role="button" tabindex="0" aria-label="特許CSVを選択"><span class="upload-symbol">↓</span><b>CSVをここにドロップ</b><p>またはクリックしてファイルを選択</p><p>UTF-8 / Shift-JIS · 20MB・5,000行まで</p></div><input id="csv-file" type="file" accept=".csv,.tsv,text/csv" class="hidden"><h3>読み込める項目</h3><div class="schema"><span>発明の名称 *</span><span>要約</span><span>公報番号</span><span>出願人</span><span>IPC</span><span>FI</span><span>F-term</span><span>公開日</span><span>ファミリーID</span></div><p class="caption">* 発明の名称は必須です。日本語・英語の一般的な列名を自動認識し、不明な場合は割り当てを表示します。同一公報番号は重複を除きます。</p>${snapshot.patents.length ? `<div class="upload-status">✓ ${snapshot.patents.length} 件を読み込み済み ${snapshot.demo ? '（架空のサンプル）' : ''}${snapshot.import_info ? `<br>重複除外 ${snapshot.import_info.duplicates} 件 · 名称なし ${snapshot.import_info.skipped} 件` : ''}</div><button class="button primary wide" id="open-map">特許の地図を見る →</button>` : ''}<p class="caption">「置き換え」は現在の特許群と判定を置き換えます。「追加」は同じ公報・内容の判定を保持し、新しい特許を追加します。FIとFタームは別項目で取り込みます。</p></div></div>${historyHtml()}`
  );
  bindQuery(q);
  bindHistory();
  on('#open-map', 'click', () => setStep(2));
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
      .map(
        ([k, v]) =>
          `<label>${v}<select data-field="${k}" ${k === 'title' ? 'required' : ''}><option value="">割り当てなし</option>${data.headers.map((h) => `<option value="${esc(h)}" ${data.mapping[k] === h ? 'selected' : ''}>${esc(h)}</option>`).join('')}</select></label>`
      )
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
