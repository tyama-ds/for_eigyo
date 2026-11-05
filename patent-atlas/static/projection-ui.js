import {
  debounce,
  tooltipPosition,
  replaceHtml,
  listen,
  esc,
  getState,
  on,
  $,
  busy,
  setState,
  api,
  toast,
  $$,
} from './core.js';
import { updateChrome } from './workspace-chrome.js';
import { showPatent, updatePicked } from './map-ui.js';
import { appState, colors } from './ui-session.js';

const scheduleProjectionEvaluation = debounce(projectionFetchEvaluation, 600);
/* Enrich the existing map without replacing learning controls or user labels. */
const atlasProjection = {
  method: 'pca',
  data: null,
  dataset: '',
  pins: new Set(),
  positions: new Map(),
  request: 0,
  evaluationRequest: 0,
  evaluation: null,
  zoom: { x: 0, y: 0, k: 1 },
  boundary: false,
  frame: 0,
  timer: 0,
};
function projectionDatasetKey(rows) {
  let hash = 2166136261;
  for (const row of rows)
    for (const value of [row.id, row.title, row.abstract]) {
      const text = String(value || '');
      for (let i = 0; i < text.length; i++) {
        hash ^= text.charCodeAt(i);
        hash = Math.imul(hash, 16777619);
      }
      hash ^= 31;
      hash = Math.imul(hash, 16777619);
    }
  return rows.length + ':' + (hash >>> 0);
}
function projectionStill() {
  return (
    document.documentElement.dataset.atlasMotion === 'static' ||
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}
function projectionControlsHtml() {
  return `<details class="panel-help"><summary role="button" aria-label="特許マップのヘルプ">?</summary><div><p id="projection-method-note"></p></div></details><div class="projection-controls"><label>配置を比較<select id="projection-method" aria-label="特許の配置方式"><option value="pca">PCA · 全体の傾向</option><option value="tsne">t-SNE · 局所の近さ</option><option value="umap">UMAP · 近傍のつながり</option></select></label><div class="button-row"><button class="button small" id="projection-pin">選択を追跡に固定</button><button class="button small" id="projection-pan">✥ 移動</button><button class="button small" id="projection-zoom-in" aria-label="マップを拡大">＋</button><button class="button small" id="projection-zoom-out" aria-label="マップを縮小">−</button><button class="button small" id="projection-reset">全体表示</button></div></div><p id="projection-status" class="caption projection-status" role="status">配置を準備しています…</p><div class="projection-pins" id="projection-pins" aria-label="追跡する特許"></div>`;
}
function projectionEvaluationHtml(e) {
  if (!e) return '<p class="caption">人の判定と確認候補を集計しています…</p>';
  const percent = e.signal_ratio == null ? '—' : (e.signal_ratio * 100).toFixed(1) + '%';
  const sn =
    e.signal_noise_ratio == null
      ? e.sn_status === 'no_excludes'
        ? '不要0件'
        : '未評価'
      : e.signal_noise_ratio.toFixed(2) + ' : 1';
  return `<div class="panel-heading"><div><h3>検索式の採用</h3></div><label class="check"><input id="projection-boundary" type="checkbox" ${atlasProjection.boundary ? 'checked' : ''}>確認候補を強調</label></div><div class="projection-metrics"><div><small>人が確認したS/N</small><strong>${esc(sn)}</strong></div><div><small>確認済み部分の必要率</small><strong>${esc(percent)}</strong></div><div><small>人の確定判定</small><strong>${Number(e.human_reviewed)} 件</strong><span>必要 ${Number(e.human_keep)} / 不要 ${Number(e.human_exclude)}</span></div><div><small>AI判定 / 未判定</small><strong>${Number(e.machine_keep) + Number(e.machine_exclude)} / ${Number(e.unreviewed_count)}</strong><span>保留 ${Number(e.hold_count)} 件</span></div></div><p class="caption">${e.unknown_source_count ? '判定元が不明な旧判定 ' + Number(e.unknown_source_count) + ' 件はS/N・AI件数に含めていません。 ' : ''}${esc(e.scope_note)}${e.sn_status === 'no_excludes' ? ' 不要例が0件のためS/Nの数値は算出していません。' : ''}</p><details class="projection-boundary-details" ${atlasProjection.boundary ? 'open' : ''}><summary>境界・保留の確認候補 ${Number(e.boundary_candidate_count || 0)} 件</summary><p class="caption">${esc(e.boundary_note)} モデル関連度0.5付近・LLM保留は別の理由として表示します。最大100件。</p><div class="projection-candidates">${(e.boundary_candidates || []).map((c) => `<button class="projection-candidate" data-projection-focus="${esc(c.id)}"><b>${esc(c.id)}</b><span>${esc(c.reason)}</span>${c.keep_neighbor ? `<small>必要例 ${esc(c.keep_neighbor)} / 不要例 ${esc(c.exclude_neighbor)}</small>` : ''}</button>`).join('') || '<p class="caption">現在の条件に当たる候補はありません。必要・不要の人の判定例が揃うと近傍の比較も行います。</p>'}</div></details>${e.target_count ? `<p class="caption">現在の特許群にあるターゲット：${Number(e.target_found)} / ${Number(e.target_count)} 件${e.missing_target_ids?.length ? ' · 未回収 ' + e.missing_target_ids.map(esc).join('、') : ''}（登録した種特許を含む場合があります。実際の検索による回収はCSVの履歴で確認）</p>` : ''}<p class="caption">検索式の採用・終了は、確認候補とターゲットの回収状況、収束履歴を合わせて判断してください。</p>`;
}
function projectionTooltipHtml(row) {
  return `<b>${esc(row.id)} · ${esc(row.title)}</b><p>${esc(row.abstract || '要約がありません。')}</p><small>IPC ${esc(row.ipc || '未取込')}${row.fi ? ' / FI ' + esc(row.fi) : ''}${row.cpc ? ' / CPC ' + esc(row.cpc) : ''}</small>${row.label_reason ? `<p>${esc(row.label_source === 'human' ? '人の判断' : 'AIの判断')}：${esc(row.label_reason)}</p>` : ''}<small>クリックで選択 · ダブルクリックで追跡</small>`;
}
function projectionAdoptionHtml() {
  const query = getState().queries?.at(-1),
    records = getState().research_workbench?.adoptions || [];
  if (!query)
    return '<h3>検索式の採用</h3><details class="panel-help"><summary role="button" aria-label="検索式の採用のヘルプ">?</summary><div><p>上の指標は現在の特許データに対する確認結果です。実検索母集団の再現率ではありません。</p></div></details><p class="caption">初案または次の検索式を作成すると、確認結果と採用理由を記録できます。</p>';
  const observed = getState().search_result?.query_id,
    linked = observed === query.id,
    busy = getState().job?.status === 'running';
  return `<div class="panel-heading"><div><h3>検索式 v${Number(query.version || getState().queries.length)} の採用</h3></div><details class="panel-help"><summary role="button" aria-label="検索式の採用のヘルプ">?</summary><div><p>上の指標は現在の特許データに対する確認結果です。実検索母集団の再現率ではありません。</p></div></details></div><p class="caption">採用は調査の完了・収束とは別に記録します。</p>${!linked ? `<p class="projection-adoption-notice">${observed ? '現在のCSVは別の検索式の結果として記録されています。' : '現在のCSVとこの検索式の対応は未確認です。'}この式の検索性能を実測した結果としては扱いません。</p><label class="check"><input id="projection-adopt-unlinked" type="checkbox">現在のCSVは参考資料として扱い、採用理由を記録する</label>` : ''}<label>採用理由<textarea id="projection-adopt-note" rows="2" maxlength="2000" placeholder="例：必要例の回収と境界候補の確認を行い、この目的には十分と判断した"></textarea></label><div class="button-row"><button id="projection-adopt" class="button primary" data-query-id="${esc(query.id)}" ${busy ? 'disabled' : ''}>この検索式の採用を記録</button><span class="caption">現在のS/N・確認候補・データとの対応を保存</span></div>${
    records.length
      ? `<details class="projection-adoption-history"><summary>採用の記録 ${records.length} 件</summary>${records
          .slice(-5)
          .reverse()
          .map(
            (r) =>
              `<p><b>${esc(r.created_at)}</b> ${esc(r.note)}<br><small>人の確認 ${Number(r.evaluation?.human_reviewed || 0)} 件 · 必要 ${Number(r.evaluation?.human_keep || 0)} / 不要 ${Number(r.evaluation?.human_exclude || 0)}</small></p>`
          )
          .join('')}</details>`
      : ''
  }`;
}
function projectionBindAdoption() {
  on('#projection-adopt', 'click', async (e) => {
    const button = e.currentTarget,
      query = getState().queries?.at(-1);
    if (!query || query.id !== button.dataset.queryId)
      throw new Error('検索式が更新されました。地図を開き直して確認してください。');
    const note = $('#projection-adopt-note').value.trim();
    if (!note) throw new Error('採用理由を入力してください。');
    const linked = getState().search_result?.query_id === query.id;
    if (!linked && !$('#projection-adopt-unlinked')?.checked)
      throw new Error('CSVと検索式の対応を確認し、参考資料として扱う場合はチェックしてください。');
    await busy(button, '記録中…', async () => {
      setState(
        await api('/research/adopt', {
          query_id: query.id,
          note,
          allow_unlinked: !linked,
          revision: getState().research_workbench?.revision,
        })
      );
      updateChrome();
      replaceHtml($('#projection-adoption'), projectionAdoptionHtml());
      projectionBindAdoption();
      toast('検索式の採用理由と現在の評価を記録しました。');
    });
  });
}
function projectionPinColor(id) {
  let n = 0;
  for (const c of id) n = (n * 31 + c.charCodeAt(0)) >>> 0;
  return ['#e2a34e', '#74b7cd', '#c991c1', '#a2bd71'][n % 4];
}
function projectionPoint(point) {
  return { x: 50 + point.x * 900, y: 35 + point.y * 510 };
}
function projectionSetZoom() {
  const z = atlasProjection.zoom,
    transform = `translate(${z.x} ${z.y}) scale(${z.k})`;
  for (const selector of ['#map-points', '#projection-trails'])
    $(selector)?.setAttribute('transform', transform);
}
function projectionZoom(factor, center = { x: 500, y: 290 }) {
  const old = atlasProjection.zoom,
    k = Math.max(0.5, Math.min(12, old.k * factor));
  atlasProjection.zoom = {
    k,
    x: center.x - ((center.x - old.x) * k) / old.k,
    y: center.y - ((center.y - old.y) * k) / old.k,
  };
  projectionSetZoom();
}
function projectionFocus(id) {
  const point = atlasProjection.positions.get(id),
    row = getState().patents.find((r) => r.id === id);
  if (!point || !row) return;
  const k = Math.max(2, atlasProjection.zoom.k);
  atlasProjection.zoom = { k, x: 500 - point.x * k, y: 290 - point.y * k };
  projectionSetZoom();
  showPatent(row);
  const node = $$('.point').find((n) => n.dataset.id === id);
  node?.focus({ preventScroll: true });
  node?.classList.add('projection-focus');
  setTimeout(() => node?.classList.remove('projection-focus'), 1500);
}
function projectionRenderPins() {
  const host = $('#projection-pins');
  if (!host) return;
  const ids = new Set(getState().patents.map((r) => r.id));
  for (const id of atlasProjection.pins) if (!ids.has(id)) atlasProjection.pins.delete(id);
  replaceHtml(
    host,
    [...atlasProjection.pins]
      .map(
        (id, i) =>
          `<span class="projection-pin" style="--pin-color:${projectionPinColor(id)}"><button data-pin-focus="${esc(id)}" title="この特許を中心に表示">${i + 1}. ${esc(id)}</button><button data-pin-remove="${esc(id)}" aria-label="${esc(id)}の追跡を解除">×</button></span>`
      )
      .join('') ||
      '<span class="caption">追跡する特許を選び「選択を追跡に固定」。方式を切り替えても同じ公報を色・番号・移動線で追えます。</span>'
  );
  $$('[data-pin-focus]', host).forEach((n) =>
    listen(n, 'click', () => projectionFocus(n.dataset.pinFocus), undefined, 'projection-ui.js:1')
  );
  $$('[data-pin-remove]', host).forEach((n) =>
    listen(
      n,
      'click',
      () => {
        atlasProjection.pins.delete(n.dataset.pinRemove);
        projectionRenderPins();
        projectionDecorate();
      },
      undefined,
      'projection-ui.js:2'
    )
  );
}
function projectionDecorate() {
  const candidateIds = new Set((atlasProjection.evaluation?.boundary_candidates || []).map((c) => c.id));
  $$('#map-points .point').forEach((n) => {
    const id = n.dataset.id,
      pinned = atlasProjection.pins.has(id);
    n.classList.toggle('projection-pinned', pinned);
    n.classList.toggle('projection-boundary-point', atlasProjection.boundary && candidateIds.has(id));
    n.style.setProperty('--pin-color', projectionPinColor(id));
  });
  const pins = $('#projection-pin-labels');
  if (pins) {
    replaceHtml(
      pins,
      [...atlasProjection.pins]
        .map((id, i) => {
          const p = atlasProjection.positions.get(id);
          return p
            ? `<text x="${p.x + 10}" y="${p.y - 10}" fill="${projectionPinColor(id)}">${i + 1}</text>`
            : '';
        })
        .join('')
    );
  }
}
function projectionShowTooltip(row, event) {
  showPatent(row);
  const el = $('#projection-tooltip');
  if (!el) return;
  replaceHtml(el, projectionTooltipHtml(row));
  el.hidden = false;
  const wrap = el.parentElement.getBoundingClientRect(),
    x = event.clientX ?? wrap.left + 30,
    y = event.clientY ?? wrap.top + 30;
  const position = tooltipPosition(
    { x: x - wrap.left, y: y - wrap.top },
    { width: 342, height: el.offsetHeight },
    wrap,
    { edge: 8, gap: 14, flip: false }
  );
  el.style.left = position.left + 'px';
  el.style.top = position.top + 'px';
}
function projectionHideTooltip() {
  const el = $('#projection-tooltip');
  if (el) el.hidden = true;
}
function projectionDraw(data, animate = true) {
  const host = $('#map-points'),
    svg = $('#map-svg');
  if (!host || !svg) return;
  cancelAnimationFrame(atlasProjection.frame);
  projectionHideTooltip();
  const old = atlasProjection.positions,
    next = new Map(data.points.map((p) => [p.id, projectionPoint(p)])),
    byId = new Map(getState().patents.map((r) => [r.id, r]));
  replaceHtml(
    host,
    data.points
      .map((p) => {
        const row = byId.get(p.id);
        if (!row) return '';
        const start = old.get(p.id) || next.get(p.id);
        return `<circle class="point ${esc(row.label || '')} ${appState.picked.has(p.id) ? 'selected' : ''}" data-id="${esc(p.id)}" cx="${start.x}" cy="${start.y}" r="${getState().patents.length > 700 ? 3.5 : 5.5}" style="${!row.label ? 'fill:' + colors[(row.cluster || 0) % colors.length] : ''}" role="button" tabindex="0" aria-pressed="${appState.picked.has(p.id)}" aria-label="${esc(p.id + ' ' + row.title)}"/>`;
      })
      .join('') + '<g id="projection-pin-labels" aria-hidden="true"></g>'
  );
  const tracked = [...new Set([...atlasProjection.pins, ...appState.picked])];
  replaceHtml(
    $('#projection-trails'),
    tracked
      .map((id) => {
        const a = old.get(id),
          b = next.get(id);
        return a && b
          ? `<path d="M${a.x} ${a.y} L${b.x} ${b.y}" stroke="${projectionPinColor(id)}"/><circle cx="${a.x}" cy="${a.y}" r="4" stroke="${projectionPinColor(id)}"/>`
          : '';
      })
      .join('')
  );
  const nodes = $$('#map-points .point');
  nodes.forEach((node) => {
    const row = byId.get(node.dataset.id);
    listen(
      node,
      'mouseenter',
      (e) => projectionShowTooltip(getState().patents.find((r) => r.id === row.id) || row, e),
      undefined,
      'projection-ui.js:3'
    );
    listen(node, 'mouseleave', projectionHideTooltip, undefined, 'projection-ui.js:4');
    listen(
      node,
      'focus',
      (e) => projectionShowTooltip(getState().patents.find((r) => r.id === row.id) || row, e),
      undefined,
      'projection-ui.js:5'
    );
    listen(node, 'blur', projectionHideTooltip, undefined, 'projection-ui.js:6');
    listen(
      node,
      'click',
      () => {
        if (svg.dataset.dragged) return;
        appState.picked.has(row.id) ? appState.picked.delete(row.id) : appState.picked.add(row.id);
        updatePicked();
        showPatent(row);
      },
      undefined,
      'projection-ui.js:7'
    );
    listen(
      node,
      'dblclick',
      (e) => {
        e.preventDefault();
        atlasProjection.pins.has(row.id)
          ? atlasProjection.pins.delete(row.id)
          : atlasProjection.pins.add(row.id);
        projectionRenderPins();
        projectionDecorate();
      },
      undefined,
      'projection-ui.js:8'
    );
    listen(
      node,
      'keydown',
      (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          appState.picked.has(row.id) ? appState.picked.delete(row.id) : appState.picked.add(row.id);
          updatePicked();
        }
      },
      undefined,
      'projection-ui.js:9'
    );
  });
  const duration = animate && !projectionStill() && old.size ? 1000 : 0,
    startTime = performance.now();
  function frame(now) {
    if ($('#map-svg') !== svg) return;
    const t = duration && !projectionStill() ? Math.min(1, (now - startTime) / duration) : 1,
      eased = 1 - Math.pow(1 - t, 3);
    atlasProjection.positions = new Map();
    for (const node of nodes) {
      const id = node.dataset.id,
        a = old.get(id) || next.get(id),
        b = next.get(id),
        p = { x: a.x + (b.x - a.x) * eased, y: a.y + (b.y - a.y) * eased };
      node.setAttribute('cx', p.x);
      node.setAttribute('cy', p.y);
      atlasProjection.positions.set(id, p);
    }
    projectionDecorate();
    if (t < 1) atlasProjection.frame = requestAnimationFrame(frame);
  }
  frame(startTime);
  projectionSetZoom();
  updatePicked();
}
function projectionRenderEvaluation() {
  const panel = $('#projection-evaluation');
  if (!panel) return;
  replaceHtml(panel, projectionEvaluationHtml(atlasProjection.evaluation));
  on('#projection-boundary', 'change', (e) => {
    atlasProjection.boundary = e.target.checked;
    projectionDecorate();
    const details = $('.projection-boundary-details');
    if (details) details.open = atlasProjection.boundary;
  });
  $$('[data-projection-focus]', panel).forEach((n) =>
    listen(n, 'click', () => projectionFocus(n.dataset.projectionFocus), undefined, 'projection-ui.js:10')
  );
  projectionDecorate();
}
async function projectionRequest(method) {
  const token = ++atlasProjection.request,
    dataset = projectionDatasetKey(getState().patents),
    host = $('#map-svg');
  if (!host) return;
  $('#projection-status').textContent =
    `${method === 'tsne' ? 't-SNE' : method.toUpperCase()} の配置を計算しています。現在の地図は保持しています…`;
  $('#projection-method').disabled = true;
  try {
    const result = await api('/map/project', { method, reference: atlasProjection.data?.points || null });
    if (
      token !== atlasProjection.request ||
      $('#map-svg') !== host ||
      projectionDatasetKey(getState().patents) !== dataset
    )
      return;
    const data = result.projection;
    if (!data || data.method !== method || !Array.isArray(data.points))
      throw new Error('配置応答の形式が不正です。');
    const expected = new Set(getState().patents.map((r) => r.id));
    if (
      data.points.length !== expected.size ||
      new Set(data.points.map((p) => p.id)).size !== expected.size ||
      data.points.some((p) => !expected.has(p.id) || !Number.isFinite(p.x) || !Number.isFinite(p.y))
    )
      throw new Error('配置の特許IDまたは座標が一致しません。');
    atlasProjection.method = method;
    atlasProjection.data = data;
    atlasProjection.dataset = dataset;
    atlasProjection.evaluation = result.evaluation || atlasProjection.evaluation;
    projectionDraw(data);
    projectionRenderEvaluation();
    projectionRenderPins();
    const detailId = $('#patent-detail')?.dataset.patentId,
      detailRow = getState().patents.find((row) => row.id === detailId);
    if (detailRow) showPatent(detailRow);
    $('#projection-status').textContent =
      `${data.metadata.algorithm || method.toUpperCase()} · ${data.metadata.representation || ''}。`;
    const note = $('#projection-method-note');
    if (note) note.textContent = data.metadata.note || '';
    const footer = $('.map-panel .canvas-footer>span');
    if (footer) footer.textContent = '2D配置の尺度は方式ごとに異なります · 移動線は同じ公報の位置を結びます';
  } catch (error) {
    if (token === atlasProjection.request && $('#map-svg') === host) {
      $('#projection-status').textContent = '配置変更を完了できませんでした。' + error.message;
      toast(error.message, true);
    }
  } finally {
    if (token === atlasProjection.request && $('#map-svg') === host) {
      $('#projection-method').disabled = false;
      $('#projection-method').value = atlasProjection.method;
    }
  }
}
async function projectionFetchEvaluation() {
  const token = ++atlasProjection.evaluationRequest,
    key = projectionDatasetKey(getState().patents),
    host = $('#projection-evaluation');
  if (!host) return;
  try {
    const result = await api('/map/evaluation');
    if (
      token !== atlasProjection.evaluationRequest ||
      $('#projection-evaluation') !== host ||
      key !== projectionDatasetKey(getState().patents)
    )
      return;
    atlasProjection.evaluation = result.evaluation || result;
    projectionRenderEvaluation();
  } catch (error) {
    if ($('#projection-evaluation') === host)
      replaceHtml(host, `<p class="caption">評価を更新できませんでした。${esc(error.message)}</p>`);
  }
}
function updateProjectionAssessment() {
  if (!$('#projection-evaluation')) return;
  if ($('#projection-adopt')) $('#projection-adopt').disabled = getState().job?.status === 'running';
  scheduleProjectionEvaluation.cancel();
  scheduleProjectionEvaluation();
  projectionDecorate();
}
function bindProjectionControls() {
  on('#projection-method', 'change', (e) => projectionRequest(e.target.value));
  on('#projection-pin', 'click', () => {
    if (!appState.picked.size) {
      toast('追跡する特許を地図上で選択してください。');
      return;
    }
    for (const id of appState.picked) atlasProjection.pins.add(id);
    projectionRenderPins();
    projectionDecorate();
  });
  on('#projection-pan', 'click', () => {
    appState.tool = 'pan';
    $('#projection-pan').classList.add('active');
    $('#map-lasso-tool').classList.remove('active');
    $('#map-svg').classList.remove('lasso-mode');
  });
  on('#map-lasso-tool', 'click', () => $('#projection-pan').classList.remove('active'));
  on('#projection-zoom-in', 'click', () => projectionZoom(1.35));
  on('#projection-zoom-out', 'click', () => projectionZoom(1 / 1.35));
  on('#projection-reset', 'click', () => {
    atlasProjection.zoom = { x: 0, y: 0, k: 1 };
    projectionSetZoom();
  });
  const svg = $('#map-svg');
  const point = (e) => new DOMPoint(e.clientX, e.clientY).matrixTransform(svg.getScreenCTM().inverse());
  let down = null,
    moved = false;
  listen(
    svg,
    'wheel',
    (e) => {
      e.preventDefault();
      projectionHideTooltip();
      projectionZoom(e.deltaY < 0 ? 1.12 : 1 / 1.12, point(e));
    },
    { passive: false },
    'projection-ui.js:11'
  );
  listen(
    svg,
    'pointerdown',
    (e) => {
      if (appState.tool !== 'pan' || e.button !== 0) return;
      down = { p: point(e), ...atlasProjection.zoom };
      moved = false;
      svg.setPointerCapture(e.pointerId);
      projectionHideTooltip();
    },
    undefined,
    'projection-ui.js:12'
  );
  listen(
    svg,
    'pointermove',
    (e) => {
      if (!down) return;
      const p = point(e),
        dx = p.x - down.p.x,
        dy = p.y - down.p.y;
      if (Math.hypot(dx, dy) > 3) moved = true;
      atlasProjection.zoom = { k: down.k, x: down.x + dx, y: down.y + dy };
      projectionSetZoom();
    },
    undefined,
    'projection-ui.js:13'
  );
  const finish = (e) => {
    if (!down) return;
    down = null;
    if (moved) {
      svg.dataset.dragged = '1';
      setTimeout(() => delete svg.dataset.dragged, 0);
    }
    if (svg.hasPointerCapture(e.pointerId)) svg.releasePointerCapture(e.pointerId);
  };
  listen(svg, 'pointerup', finish, undefined, 'projection-ui.js:14');
  listen(svg, 'pointercancel', finish, undefined, 'projection-ui.js:15');
}
function mountProjectionWorkbench() {
  const svg = $('#map-svg');
  if (!svg || $('#projection-method')) return;
  ++atlasProjection.request;
  ++atlasProjection.evaluationRequest;
  cancelAnimationFrame(atlasProjection.frame);
  scheduleProjectionEvaluation.cancel();
  const dataset = projectionDatasetKey(getState().patents);
  if (atlasProjection.dataset !== dataset) {
    atlasProjection.data = null;
    atlasProjection.evaluation = null;
    atlasProjection.positions = new Map();
    atlasProjection.zoom = { x: 0, y: 0, k: 1 };
  }
  $('.map-panel .canvas-header').insertAdjacentHTML('afterend', projectionControlsHtml());
  $('.map-layout').insertAdjacentHTML(
    'afterend',
    '<div class="panel projection-evaluation" id="projection-evaluation" aria-label="検索式の採用判断"></div>'
  );
  $('#projection-evaluation').insertAdjacentHTML(
    'afterend',
    '<div class="panel projection-adoption" id="projection-adoption"></div>'
  );
  replaceHtml($('#projection-adoption'), projectionAdoptionHtml());
  projectionBindAdoption();
  const wrap = svg.parentElement;
  wrap.classList.add('projection-canvas-wrap');
  wrap.insertAdjacentHTML(
    'beforeend',
    '<div id="projection-tooltip" class="projection-tooltip" role="tooltip" hidden></div>'
  );
  replaceHtml($('#map-clusters'), '');
  const trails = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  trails.id = 'projection-trails';
  trails.setAttribute('aria-hidden', 'true');
  svg.insertBefore(trails, $('#map-points'));
  $('#projection-method').value = atlasProjection.method;
  bindProjectionControls();
  projectionRenderPins();
  projectionRenderEvaluation();
  if (atlasProjection.data) {
    projectionDraw(atlasProjection.data, false);
    $('#projection-status').textContent =
      `${atlasProjection.data.metadata.algorithm || atlasProjection.method.toUpperCase()} · ${atlasProjection.data.metadata.representation}。`;
    const note = $('#projection-method-note');
    if (note) note.textContent = atlasProjection.data.metadata.note || '';
    projectionFetchEvaluation();
  } else {
    const prior = {
      points: getState().patents.map((r) => ({
        id: r.id,
        x: Number.isFinite(r.x) ? r.x : 0.5,
        y: Number.isFinite(r.y) ? r.y : 0.5,
      })),
    };
    projectionDraw(prior, false);
    projectionRequest(atlasProjection.method);
  }
}

function projectionTrackPatent(id) {
  if (!getState().patents.some((p) => p.id === id)) return;
  atlasProjection.pins.add(id);
  projectionRenderPins();
  projectionDecorate();
  projectionFocus(id);
  $('#map-svg')?.scrollIntoView({ behavior: projectionStill() ? 'instant' : 'smooth', block: 'center' });
}

export {
  atlasProjection,
  projectionDatasetKey,
  projectionStill,
  projectionEvaluationHtml,
  projectionTooltipHtml,
  projectionAdoptionHtml,
  projectionPinColor,
  projectionSetZoom,
  projectionZoom,
  projectionRenderPins,
  projectionDraw,
  projectionRenderEvaluation,
  projectionRequest,
  updateProjectionAssessment,
  mountProjectionWorkbench,
  projectionTrackPatent,
};
