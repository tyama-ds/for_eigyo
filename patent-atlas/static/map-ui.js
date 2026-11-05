import {
  judgmentLabel,
  replaceHtml,
  listen,
  getState,
  $,
  on,
  esc,
  html,
  $$,
  setState,
  api,
  toast,
  run,
} from './core.js';
import { setStep } from './app.js';
import { appState, colors } from './ui-session.js';
import { learningControlsHtml, bindLearningControls, learningUiState } from './learning-ui.js';
import {
  mountProjectionWorkbench,
  updateProjectionAssessment,
  atlasProjection,
  projectionTrackPatent,
} from './projection-ui.js';
import { enableLasso } from './lasso.js';
import { renderStep } from './workflow-ui.js';
import { bindPatentRowPreviews } from './patent-inspector.js';

function counts() {
  return {
    keep: getState().patents.filter((p) => p.label === 'keep').length,
    exclude: getState().patents.filter((p) => p.label === 'exclude').length,
  };
}
function mapEmptyHtml() {
  return html`<div class="panel empty-state"><div class="empty-state-icon">◎</div><h2>特許がまだありません</h2>
    <p>Step 01からCSVを読み込むか、サンプルを試してください。</p>
    <button id="to-import" class="button primary">CSVを読み込む →</button></div>`;
}
function mapCanvasPanelHtml(snapshot) {
  const lasso = appState.tool === 'lasso';
  return html`<div class="panel canvas-panel map-panel"><div class="canvas-header"><div>
    <h3>特許のランドスケープ ${snapshot.demo ? '<span class="demo-badge">架空のサンプル</span>' : ''}</h3>
    <p>${snapshot.patents.length} 件 · ${snapshot.clusters.length} クラスタ · タイトルと要約の類似度</p></div>
    <div class="canvas-toolbar">
    <button id="map-lasso-tool" class="icon-button ${lasso ? 'active' : ''}">◌ 囲んで選択</button>
    <button id="map-clear" class="icon-button">選択解除</button></div></div>
    <div class="canvas-wrap"><svg id="map-svg" class="constellation ${lasso ? 'lasso-mode' : ''}"
    viewBox="0 0 1000 580"
    role="group" aria-label="特許類似度マップ"><g id="map-clusters"></g><g id="map-points"></g>
    <path id="map-lasso" class="lasso-path"/></svg></div>
    <div class="canvas-footer"><div class="legend"><span><i style="background:var(--green)"></i>必要</span>
    <span><i style="background:var(--pink)"></i>不要</span>
    <span><i style="background:var(--blue)"></i>未判定は群別色</span></div>
    <span>2D配置：TF-IDF / SVD · 軸に物理的な意味はありません</span></div></div>`;
}
function mapSidePanelHtml(snapshot, c) {
  return html`<div class="map-side"><div class="panel"><div class="map-stats">
    <div class="stat"><strong>${snapshot.patents.length}</strong><small>読み込み</small></div>
    <div class="stat keep"><strong>${c.keep}</strong><small>必要</small></div>
    <div class="stat exclude"><strong>${c.exclude}</strong><small>不要</small></div></div>
    <h3>選択中 <span id="picked-count" class="pill">${appState.picked.size} 件</span></h3>
    <div class="map-selection-actions" style="margin-top:14px">
    <button id="label-keep" class="button positive">✓ 必要</button>
    <button id="label-exclude" class="button danger">× 不要</button>
    <button id="label-reset" class="button small">判定解除</button></div>
    <p class="caption">点を囲むかクリックして、一括で判定できます。キーボード操作は下の特許リストから。</p></div>
    <div class="panel" id="patent-detail"><h3>特許の詳細</h3>
    <div class="map-detail-empty">点にマウスを重ねると、<br>内容と配置の理由が見えます。</div></div></div>`;
}
function mapLearningPanelHtml() {
  return html`<div class="panel learning-panel"><div class="panel-heading"><div>
    <h3 style="margin-top:8px">学習・LLM判定</h3></div>
    <button class="button subtle" id="next-refine">次の検索式へ →</button></div>${learningControlsHtml()}
    <div id="training-results">${trainingHtml()}</div></div>`;
}
function mapPatentListHtml() {
  return html`<div class="panel patent-list"><div class="panel-heading"><div><h3>特許リスト</h3>
    <p class="caption" style="margin:4px 0 0">行にマウスを重ねると、要約と判定理由を確認できます。</p></div>
    <div class="filter-row"><input id="patent-search" placeholder="名称・公報番号で絞り込み" aria-label="特許を検索">
    <select id="label-filter" aria-label="判定で絞り込み"><option value="all">すべて</option>
    <option value="keep">必要</option><option value="exclude">不要</option>
    <option value="unlabeled">未判定</option></select></div></div>
    <div class="table-scroll"><table><thead><tr><th>公報番号</th><th>発明の名称</th><th>関連度</th><th>判定</th>
    <th>変更</th></tr></thead><tbody id="patent-rows"></tbody></table></div></div>`;
}
function mapLayoutHtml(snapshot, c) {
  return html`<div class="map-layout">${mapCanvasPanelHtml(snapshot)}${mapSidePanelHtml(snapshot, c)}</div>
    ${mapLearningPanelHtml()}${mapPatentListHtml()}`;
}
function renderMap(host = $('#step-content'), snapshot = getState()) {
  if (!snapshot.patents.length) {
    replaceHtml(host, mapEmptyHtml());
    on('#to-import', 'click', () => setStep(1));
    return;
  }
  replaceHtml(host, mapLayoutHtml(snapshot, counts()));
  drawMap();
  bindLearningControls();
  renderPatentRows();
  on('#map-lasso-tool', 'click', () => {
    appState.tool = appState.tool === 'lasso' ? 'click' : 'lasso';
    $('#map-svg').classList.toggle('lasso-mode', appState.tool === 'lasso');
    $('#map-lasso-tool').classList.toggle('active', appState.tool === 'lasso');
  });
  on('#map-clear', 'click', () => {
    appState.picked.clear();
    updatePicked();
  });
  ['keep', 'exclude', 'reset'].forEach((label) =>
    on('#label-' + label, 'click', () => applyLabels([...appState.picked], label === 'reset' ? null : label))
  );
  on('#next-refine', 'click', () => setStep(3));
  on('#patent-search', 'input', renderPatentRows);
  on('#label-filter', 'change', renderPatentRows);
  mountProjectionWorkbench();
}
function pointPosition(r, i) {
  const jitterX = (Math.sin(i * 13.17) * 0.5 + 0.5) * 26 - 13,
    jitterY = (Math.cos(i * 19.13) * 0.5 + 0.5) * 26 - 13;
  return { x: r.x * 920 + 40 + jitterX, y: r.y * 490 + 35 + jitterY };
}
function mapPointHtml(r, i, total) {
  const p = pointPosition(r, i),
    picked = appState.picked.has(r.id);
  return html`<circle class="point ${r.label || ''} ${picked ? 'selected' : ''}" data-i="${i}"
    data-id="${esc(r.id)}"
    cx="${p.x}" cy="${p.y}" r="${total > 700 ? 3.5 : 6.5}"
    style="${!r.label ? 'fill:' + colors[r.cluster % colors.length] : ''}" role="button" tabindex="0"
    aria-label="${esc(r.id + ' ' + r.title)}" aria-pressed="${picked}"/>`;
}
function mapClusterLabelHtml(c, rows) {
  const group = rows.filter((r) => r.cluster === c.id);
  const x = (group.reduce((a, r) => a + r.x, 0) / group.length) * 920 + 40;
  const y = Math.max(23, Math.min(...group.map((r) => r.y)) * 490 + 9);
  return html`<text class="cluster-label" x="${x}" y="${y}" text-anchor="middle">
    ${esc(c.name)} · ${c.count}</text>`;
}
function bindMapPoint(el, row) {
  const toggle = () => {
    appState.picked.has(row.id) ? appState.picked.delete(row.id) : appState.picked.add(row.id);
    updatePicked();
  };
  listen(el, 'mouseenter', () => showPatent(row), undefined, 'map-ui.js:1');
  listen(el, 'focus', () => showPatent(row), undefined, 'map-ui.js:2');
  listen(
    el,
    'click',
    () => {
      if (!$('#map-svg').dataset.dragged) {
        toggle();
        showPatent(row);
      }
    },
    undefined,
    'map-ui.js:3'
  );
  listen(
    el,
    'keydown',
    (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggle();
      }
    },
    undefined,
    'map-ui.js:4'
  );
}
function drawMap() {
  const rows = getState().patents;
  replaceHtml($('#map-points'), rows.map((r, i) => mapPointHtml(r, i, rows.length)).join(''));
  replaceHtml(
    $('#map-clusters'),
    getState()
      .clusters.map((c) => mapClusterLabelHtml(c, rows))
      .join('')
  );
  $$('.point').forEach((el) => bindMapPoint(el, rows[Number(el.dataset.i)]));
  enableLasso($('#map-svg'), '#map-lasso', '.point', (n) => appState.picked.add(n.dataset.id), updatePicked);
}
function updatePicked() {
  if (!$('#picked-count')) return;
  $('#picked-count').textContent = appState.picked.size + ' 件';
  $$('.point').forEach((n) => {
    n.classList.toggle('selected', appState.picked.has(n.dataset.id));
    n.setAttribute('aria-pressed', String(appState.picked.has(n.dataset.id)));
  });
}
function updateMapAssessment() {
  const c = counts();
  for (const label of ['keep', 'exclude'])
    if ($('.map-stats .' + label + ' strong')) $('.map-stats .' + label + ' strong').textContent = c[label];
  $$('.point').forEach((node) => {
    const row = getState().patents.find((r) => r.id === node.dataset.id);
    if (!row) return;
    for (const label of ['keep', 'exclude']) node.classList.toggle(label, row.label === label);
    node.style.fill = row.label ? '' : colors[row.cluster % colors.length];
  });
  const focused = $('#patent-detail')?.dataset.patentId;
  if (focused) {
    const row = getState().patents.find((r) => r.id === focused);
    if (row) showPatent(row);
  }
  updateProjectionAssessment();
}
function patentDetailHtml(row, currentPlacement) {
  const cluster = getState().clusters.find((c) => c.id === row.cluster)?.name || '';
  const judge = row.label_reason
    ? html`<div class="detail-block"><b>${row.label_source === 'agent' ? 'エージェント' : 'あなた'}の判断</b>
        ${esc(row.label_reason)}</div>`
    : '';
  const scoreLabel = row.score_source === 'llm' ? 'LLMの関連度（自己評価）' : 'モデルの関連度';
  const score =
    row.score != null
      ? html`<div class="detail-block"><b>${scoreLabel} ${Math.round(row.score * 100)} / 100</b>
          検索関連性の保証された確率ではありません。</div>`
      : '';
  return html`<div class="detail-id">${esc(row.id)} ${getState().demo ? '· 架空' : ''}</div>
    <h3 class="detail-title">${esc(row.title)}</h3>
    <p class="detail-text">${esc(row.abstract || '要約がありません。')}</p>
    <div class="detail-block"><b>出願人・分類</b>${esc(row.applicant || '不明')}<br>IPC：${esc(row.ipc || '未取込')}
    ${row.fi ? html`<br>FI：${esc(row.fi)}` : ''}${row.fterm ? html`<br>Fターム：${esc(row.fterm)}` : ''}
    ${row.cpc ? html`<br>CPC：${esc(row.cpc)}` : ''}</div>
    <div class="detail-block"><b>なぜ、このあたり？</b>${esc(currentPlacement)}<br>${esc(row.inclusion_reason)}<br>
    群：${esc(cluster)}</div>${judge}${score}`;
}
function showPatent(row) {
  row = getState().patents.find((r) => r.id === row?.id) || row;
  if (!row) return;
  $('#patent-detail').dataset.patentId = row.id;
  const currentPlacement = atlasProjection.data
    ? atlasProjection.data.metadata.algorithm +
      '：名称・要約の近さを配置。方式ごとに軸と距離の尺度が変わります。'
    : row.map_reason;
  replaceHtml($('#patent-detail'), patentDetailHtml(row, currentPlacement));
}
async function applyLabels(ids, label) {
  if (!ids.length) throw new Error('先に特許を選んでください。');
  setState(await api('/labels', { ids, label }));
  appState.picked.clear();
  renderStep();
  toast(`${ids.length} 件を${judgmentLabel(label)}にしました。`);
}
function patentRowHtml(r, disabled) {
  return html`<tr data-patent-id="${esc(r.id)}" tabindex="0" aria-describedby="patent-row-tooltip">
    <td>${esc(r.id)}</td>
    <td>${esc(r.title)}</td><td>${r.score == null ? '—' : Math.round(r.score * 100)}</td>
    <td>${judgmentLabel(r.label)}${r.label_source === 'agent' ? ' · AI' : ''}</td><td><div class="button-row">
    <button class="button small positive row-label" data-id="${esc(r.id)}" data-label="keep"${disabled}
    >必要</button>
    <button class="button small danger row-label" data-id="${esc(r.id)}" data-label="exclude"${disabled}
    >不要</button>
    <button class="button small row-track" data-id="${esc(r.id)}" aria-label="${esc(r.id)}を地図で追跡">追跡</button>
    </div></td></tr>`;
}
function renderPatentRows() {
  const disabled =
    getState().job?.status === 'running' || learningUiState.learningStarting ? ' disabled' : '';
  const text = ($('#patent-search')?.value || '').toLowerCase(),
    filter = $('#label-filter')?.value || 'all';
  const rows = getState().patents.filter(
    (r) =>
      (r.title + ' ' + r.id).toLowerCase().includes(text) &&
      (filter === 'all' || (filter === 'unlabeled' && !r.label) || r.label === filter)
  );
  replaceHtml(
    $('#patent-rows'),
    rows
      .slice(0, 500)
      .map((r) => patentRowHtml(r, disabled))
      .join('') || '<tr><td colspan="5">該当する特許はありません。</td></tr>'
  );
  if (rows.length > 500)
    $('#patent-rows').insertAdjacentHTML(
      'beforeend',
      '<tr><td colspan="5">先頭500件を表示しています。絞り込みをご利用ください。</td></tr>'
    );
  $$('.row-label').forEach((el) =>
    listen(
      el,
      'click',
      run(() => applyLabels([el.dataset.id], el.dataset.label)),
      undefined,
      'map-ui.js:5'
    )
  );
  $$('.row-track').forEach((el) =>
    listen(
      el,
      'click',
      () => {
        projectionTrackPatent(el.dataset.id);
      },
      undefined,
      'map-ui.js:6'
    )
  );
  bindPatentRowPreviews($('#patent-rows'), rows.slice(0, 500));
}
function trainingHelpHtml(note) {
  return html`<details class="panel-help"><summary role="button" aria-label="学習・判定結果のヘルプ">?</summary>
    <div><p class="caption">${note}</p></div></details>`;
}
function trainingMetricHtml([name, value]) {
  return html`<div class="metric"><small>${name}</small><strong>${Math.round(value * 100)}%</strong></div>`;
}
function trainingMetricsHtml(metrics) {
  const rows = [
    ['必要クラスの適合率', metrics.precision],
    ['必要クラスの再現率', metrics.recall],
    ['F1', metrics.f1],
  ];
  return html`<div class="learning-results">
    ${rows.map(trainingMetricHtml)}
    <div class="metric"><small>独立評価</small><strong>${metrics.n} 件</strong></div></div>`;
}
function trainingHtml() {
  const t = getState().training;
  if (!t) return '<p class="training-description">学習・判定を実行すると、結果をここに表示します。</p>';
  if (t.mode === 'llm') {
    const fallback =
      'モデルの重みを学習した結果ではありません。関連度・信頼度はLLMの自己評価で、独立評価は行っていません。';
    const progress = `処理 ${Number(t.completed_count || 0)} / ${Number(t.total_count || 0)} 件`;
    const judged = `判定採用 ${Number(t.judged_count || 0)} 件 · 保留 ${Number(t.deferred_count || 0)} 件`;
    return html`<p class="training-description"><b>LLMによる要否判定</b> · ${progress} · ${judged}</p>
      ${trainingHelpHtml(esc(t.evaluation_note || fallback))}`;
  }
  const note = `${esc(t.evaluation_note)} 同一ファミリーIDの公報は学習・評価間で分けません。IDがない場合は同文書単位で分割します。`;
  const agent = t.includes_agent_labels ? ' · AI判断を含む' : '';
  return html`<p class="training-description">${esc(t.method)} · 学習 ${t.train_count} 件${agent}</p>
    ${t.metrics ? trainingMetricsHtml(t.metrics) : ''}${trainingHelpHtml(note)}`;
}

export { counts, renderMap, updatePicked, updateMapAssessment, showPatent, renderPatentRows, trainingHtml };

export { renderMap as render };
