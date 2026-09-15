/* Citation time planes. Visual projection and animation never enter the metrics. */
(function (global) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const list = value => Array.isArray(value) ? value : [];
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const clamp = (value, min, max) => Math.max(min, Math.min(max, finite(Number(value)) ? Number(value) : min));
  const number = value => finite(Number(value)) && value != null ? Number(value).toLocaleString('ja-JP') : '—';
  const fmt = value => String(Math.round(value * 100) / 100);
  const color = value => /^#[\da-f]{3,8}$/i.test(String(value)) ? value : '#90aaca';
  const DEFAULT_CAMERA = Object.freeze({yaw: -.22, pitch: .53});
  let mounted = null, sequence = 0;

  function periodWindow(periods, start = null, size = 8) {
    const length = Math.max(1, Math.min(8, Math.floor(size))), last = Math.max(0, periods.length - length);
    const offset = start == null ? last : Math.floor(clamp(start, 0, last));
    return {periods: periods.slice(offset, offset + length), start: offset, last, size: length};
  }
  function validPoint(point) { return !!point && finite(point.x) && finite(point.y); }
  function normalizeData(raw = {}) {
    const periods = list(raw.periods).filter(p => p && p.id != null).map((p, i) => ({...p, id: String(p.id), index: finite(p.index) ? p.index : i})).sort((a, b) => a.index - b.index);
    const ranks = new Map(periods.map((p, i) => [p.id, i]));
    const seen = new Set();
    const nodes = list(raw.nodes).filter(n => {
      if (!n || n.id == null || !validPoint(n) || !ranks.has(String(n.period_id)) || seen.has(String(n.id))) return false;
      seen.add(String(n.id)); return true;
    }).slice(0, 240).map(n => ({...n, id: String(n.id), period_id: String(n.period_id)}));
    const byId = new Map(nodes.map(n => [n.id, n]));
    const edgeKeys = new Set();
    // Never reverse, infer or synthesize citations. Equal-period citations remain valid.
    const edges = list(raw.edges).filter(edge => {
      const a = byId.get(String(edge.source_id)), b = byId.get(String(edge.target_id));
      if (!a || !b || a.id === b.id || ranks.get(a.period_id) < ranks.get(b.period_id)) return false;
      if (edge.source_period != null && String(edge.source_period) !== a.period_id) return false;
      if (edge.target_period != null && String(edge.target_period) !== b.period_id) return false;
      const key = `${a.id}\u0000${b.id}`; if (edgeKeys.has(key)) return false; edgeKeys.add(key); return true;
    }).slice(0, 480).map((edge, i) => ({...edge, id: String(edge.id ?? `edge-${i}`), source_id: String(edge.source_id), target_id: String(edge.target_id)}));
    const centroids = list(raw.centroids).filter(c => c && ranks.has(String(c.period_id))).map(c => ({...c, period_id: String(c.period_id), current: validPoint(c.current) ? c.current : null, foundation: validPoint(c.foundation) ? c.foundation : null, paired_current: validPoint(c.paired_current) ? c.paired_current : null}));
    return {...raw, periods, nodes, edges, centroids, topics: list(raw.topics), coverage: raw.coverage || {}, meta: raw.meta || {}, warnings: list(raw.warnings), byId};
  }
  function camera(value = DEFAULT_CAMERA) { return {yaw: clamp(value.yaw, -1.05, 1.05), pitch: clamp(value.pitch, .25, .92)}; }
  function project(point, layer, count, options = {}) {
    const c = camera(options.camera || DEFAULT_CAMERA);
    const x = (clamp(point.x, 0, 1) - .5) * 610, y = (clamp(point.y, 0, 1) - .5) * 265;
    const rx = Math.cos(c.yaw) * x - Math.sin(c.yaw) * y, depth = Math.sin(c.yaw) * x + Math.cos(c.yaw) * y;
    const fraction = count > 1 ? layer / (count - 1) - .5 : 0;
    const span = clamp(options.spread ?? 365, 180, 430);
    const elevation = fraction * span * clamp(options.expansion ?? 1, 0, 1);
    // Preserve a gutter at every allowed orbit, without independently clipping endpoints.
    const depthExtent = Math.sin(c.pitch) * (Math.abs(Math.sin(c.yaw)) * 305 + Math.abs(Math.cos(c.yaw)) * 132.5);
    const depthScale = Math.min(1, (315 - span / 2) / (depthExtent || 1));
    return [485 + rx, 363 + Math.sin(c.pitch) * depth * depthScale - elevation];
  }
  function edgeGeometry(source, target) {
    if (!source || !target || ![...source, ...target].every(finite)) return null;
    const dx = target[0] - source[0], dy = target[1] - source[1];
    const bend = clamp(Math.abs(dy) * .18 + Math.abs(dx) * .07, 15, 65);
    const controls = [source, [source[0] + dx * .28 + bend, source[1] + dy * .36], [source[0] + dx * .73 + bend, source[1] + dy * .72], target];
    return {controls, path: `M${source.map(fmt).join(',')}C${controls[1].map(fmt).join(',')} ${controls[2].map(fmt).join(',')} ${target.map(fmt).join(',')}`};
  }
  function bezier(controls, t) {
    const u = 1 - clamp(t, 0, 1), v = 1 - u;
    return [0, 1].map(axis => u*u*u*controls[0][axis] + 3*u*u*v*controls[1][axis] + 3*u*v*v*controls[2][axis] + v*v*v*controls[3][axis]);
  }
  function expansionProgress(elapsed, index, reduced) { return reduced ? 1 : 1 - Math.pow(1 - clamp((elapsed - index * 90) / 850, 0, 1), 3); }
  function selectedCentroid(state) {
    const candidates = state.data?.centroids.filter(c => c.period_id === state.cohort) || [];
    return candidates.find(c => String(c.topic_id ?? 'all') === state.topic) || (state.topic === 'all' ? candidates.find(c => c.topic_id == null) : null) || null;
  }
  function paperButton(state, id, caption) {
    const paper = state.data.byId.get(String(id)) || list(state.result.papers).find(p => String(p.id) === String(id));
    return `<button type="button" class="cf-paper-link" data-paper="${esc(id)}"><span>${esc(caption || paper?.title || id)}</span><small>${esc(paper?.year ?? '年不明')} · 原文を確認 ↗</small></button>`;
  }
  function evidenceList(state, ids) {
    const unique = [...new Set(list(ids).map(String))];
    return unique.length ? unique.slice(0, 12).map(id => paperButton(state, id)).join('') + (unique.length > 12 ? `<p class="cf-note">根拠 ${number(unique.length)}件のうち先頭12件を表示</p>` : '') : '<p class="cf-note">確認できる根拠論文がありません。</p>';
  }
  function inspector(state) {
    const c = selectedCentroid(state), data = state.data, period = data.periods.find(p => p.id === state.cohort);
    const edge = data.edges.find(item => item.id === state.edge);
    const coverage = c?.coverage || {}, total = coverage.papers_total ?? c?.current?.count ?? 0, matched = coverage.matched_citing_papers ?? c?.foundation?.count ?? 0;
    const ratio = total ? `${(100 * matched / total).toFixed(0)}%` : '—';
    const distance = c?.cosine_distance;
    const comparison = c?.foundation && c?.paired_current && finite(distance);
    return `<div class="cf-inspector-top"><span class="cf-eyebrow">SELECTED COHORT</span><h2>${esc(period?.label || '期間を選択')}</h2><p>${esc(state.topic === 'all' ? '全トピック' : data.topics.find(t => String(t.id) === state.topic)?.label || state.topic)}</p></div>
      <div class="cf-center-stat cf-current-stat"><span><i></i>新しい研究の重心</span><strong>${number(c?.current?.count ?? 0)}<small>論文</small></strong><p>この期間に出版された論文の内容中心</p></div>
      <div class="cf-center-stat cf-foundation-stat"><span><i></i>参照した研究基盤の重心</span><strong>${c?.foundation ? number(c.foundation.count) : '—'}<small>${c?.foundation ? '引用元論文' : '比較不可'}</small></strong><p>${c?.foundation ? '引用元ごとの参照先中心を、等しく平均' : '有効な参照先・文章表現が不足しています。'}</p></div>
      <div class="cf-metric"><span>対応する新研究との距離 <small>cosine</small></span><strong>${comparison ? distance.toFixed(3) : '—'}</strong><p>${comparison ? `引用照合済みの同じ新論文群 ${number(c.paired_current.count)}件で比較。大きいほど内容の隔たりが大きい。` : '対応する新研究と研究基盤の両方が必要です。'}</p></div>
      <div class="cf-coverage"><div><span>研究基盤を照合できた引用元</span><b>${ratio}</b></div><div class="cf-meter"><i style="width:${total ? clamp(100 * matched / total, 0, 100) : 0}%"></i></div><p>${number(matched)} / ${number(total)}論文${coverage.matched_target_papers != null ? ` · 参照先 ${number(coverage.matched_target_papers)}論文` : ''}${coverage.semantic_citing_papers != null && coverage.semantic_citing_papers < matched ? `<br>うち文章表現で比較可能 ${number(coverage.semantic_citing_papers)}論文` : ''}</p></div>
      <div class="cf-evidence">${edge ? `<div class="cf-evidence-heading"><span class="cf-eyebrow">SELECTED CITATION</span><button type="button" data-cf-action="clear-edge" aria-label="引用の選択を解除">×</button></div><h3>引用元 → 参照先</h3>${paperButton(state, edge.source_id)}<div class="cf-evidence-arrow">↓ <span>実際の引用関係</span></div>${paperButton(state, edge.target_id)}` : `<span class="cf-eyebrow">EVIDENCE</span><h3>重心を構成する根拠論文の例</h3><details open><summary>新しい研究 ${number(c?.current?.count ?? 0)}</summary>${evidenceList(state, c?.current?.evidence_ids)}</details><details><summary>参照した研究基盤</summary>${evidenceList(state, c?.foundation?.evidence_ids)}</details>`}</div>`;
  }
  function header(state) {
    const base = `/api/results/${encodeURIComponent(state.result.id)}/citation-flow/export`, params = `interval=${encodeURIComponent(state.interval)}&topic_id=${encodeURIComponent(state.topic)}`;
    return `<div class="cf-topbar"><div><span class="cf-eyebrow">CITATION FLOW / TEMPORAL OBSERVATORY</span><p>新しい研究が、どの研究基盤を参照しているか。${state.data?.meta.is_demo || state.result.meta?.is_demo ? '<span class="cf-demo-label">合成デモ</span>' : ''}</p></div><div class="cf-exports"><a href="${base}?format=csv&${params}" download>CSV ↓</a><a href="${base}?format=json&${params}" download>JSON ↓</a></div></div>`;
  }
  function controls(state) {
    const data = state.data;
    return `<div class="cf-controls"><label>時間の単位<select data-cf-control="interval" aria-label="時間の単位">${[['year','年'],['quarter','四半期'],['month','月']].map(([id, label]) => `<option value="${id}" ${state.interval === id ? 'selected' : ''}>${label}</option>`).join('')}</select></label><label class="cf-topic-control">トピック<select data-cf-control="topic" aria-label="引用元のトピック"><option value="all">すべてのトピック</option>${data.topics.filter(t => t.id !== 'all').map(t => `<option value="${esc(t.id)}" ${state.topic === String(t.id) ? 'selected' : ''}>${esc(t.label)}</option>`).join('')}</select></label><label>比較する出版期間<select data-cf-control="cohort" aria-label="比較する出版期間">${data.periods.map(p => `<option value="${esc(p.id)}" ${state.cohort === p.id ? 'selected' : ''}>${esc(p.label)} · ${number(p.count)}論文</option>`).reverse().join('')}</select></label><div class="cf-motion-controls"><button type="button" data-cf-action="play" ${state.reduced ? 'disabled' : ''} aria-pressed="${state.playing}">${state.playing && !state.reduced ? 'Ⅱ 一時停止' : '▷ 再生'}</button><button type="button" data-cf-action="replay" ${state.reduced ? 'disabled' : ''}>↻ 時層を展開</button></div></div>`;
  }
  function sceneMarkup(state) {
    const data = state.data, win = periodWindow(data.periods, state.start), ranks = new Map(win.periods.map((p, i) => [p.id, i]));
    const nodes = data.nodes.filter(n => ranks.has(n.period_id)), ids = new Set(nodes.map(n => n.id));
    const edges = data.edges.filter(edge => ids.has(edge.source_id) && ids.has(edge.target_id));
    const centroid = selectedCentroid(state), selectedEdges = edges.filter(edge => data.byId.get(edge.source_id)?.period_id === state.cohort);
    const pulseIds = new Set((selectedEdges.length ? selectedEdges : edges).slice(0, 80).map(edge => edge.id));
    const uid = state.uid;
    state.scene = {win, ranks, nodes, edges, pulseIds, centroid, geometry: new Map()};
    return `<svg class="cf-svg" viewBox="0 0 970 720" role="group" aria-label="引用の時層マップ。上は新しい研究、下は古い研究。線の矢印は引用元から参照先を示します。ドラッグで視点を回転できます。"><defs><linearGradient id="${uid}-plane" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#6bd5ea" stop-opacity=".06"/><stop offset="1" stop-color="#416ecb" stop-opacity=".015"/></linearGradient><filter id="${uid}-glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="2.1"/></filter><marker id="${uid}-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L8 4L0 8Z" fill="#8ee7f7"/></marker></defs>
      <g class="cf-planes">${win.periods.map((p, i) => `<g data-cf-plane="${i}" class="${p.id === state.cohort ? 'selected' : ''}"><path class="cf-plane-fill" fill="url(#${uid}-plane)"/><path class="cf-plane-grid"/><text class="cf-plane-label" data-cf-cohort="${esc(p.id)}" role="button" tabindex="0" aria-label="${esc(p.label)}の重心を比較">${esc(p.label)}<tspan class="cf-plane-count"> / ${number(p.count)}</tspan></text></g>`).join('')}</g>
      <g class="cf-edges">${edges.map((edge, i) => `<g data-cf-edge-record="${i}" class="cf-edge ${edge.id === state.edge ? 'selected' : ''} ${data.byId.get(edge.source_id)?.period_id === state.cohort ? 'cohort' : ''}"><path class="cf-edge-line" marker-end="url(#${uid}-arrow)"/><path class="cf-edge-hit" data-cf-edge="${esc(edge.id)}" role="button" tabindex="0" aria-label="${esc(data.byId.get(edge.source_id)?.title)} から ${esc(data.byId.get(edge.target_id)?.title)} への引用"><title>${esc(data.byId.get(edge.source_id)?.title)} → ${esc(data.byId.get(edge.target_id)?.title)}</title></path>${pulseIds.has(edge.id) ? `<circle class="cf-pulse-glow" r="5" filter="url(#${uid}-glow)"/><circle class="cf-pulse" r="1.8"/>` : ''}</g>`).join('')}</g>
      <g class="cf-nodes">${nodes.map((node, i) => { const highlighted = state.edge && data.edges.some(edge => edge.id === state.edge && [edge.source_id, edge.target_id].includes(node.id)); return `<g data-cf-node-record="${i}" class="cf-node ${node.period_id === state.cohort ? 'current' : ''} ${highlighted ? 'selected' : ''}" data-paper="${esc(node.id)}" role="button" tabindex="0" aria-label="${esc(node.title || node.id)}, ${esc(node.year)}年"><circle class="cf-node-halo" r="7"/><circle r="${highlighted ? 4.3 : node.period_id === state.cohort ? 3 : 2.4}" fill="${color(data.topics.find(t => t.id === node.topic_id)?.color)}"/><title>${esc(node.title || node.id)} · ${esc(node.year)}</title></g>`; }).join('')}</g>
      <g class="cf-centroids">${centroid && ranks.has(centroid.period_id) ? `${centroid.current && centroid.foundation ? '<path class="cf-centroid-bridge"/><text class="cf-centroid-bridge-label">図上の中心差</text>' : ''}${centroid.foundation ? '<g data-cf-center="foundation" class="cf-center cf-foundation"><circle class="cf-center-glow" r="16"/><path class="cf-center-ring" d="M0-10L10 0L0 10L-10 0Z"/><path d="M0-4L4 0L0 4L-4 0Z" class="cf-center-core"/><text x="15" y="20">参照した研究基盤</text></g>' : ''}${centroid.current ? '<g data-cf-center="current" class="cf-center cf-current"><circle class="cf-center-glow" r="17"/><circle class="cf-center-ring" r="10"/><circle class="cf-center-core" r="4"/><path class="cf-crosshair" d="M-15 0H-7M7 0H15M0-15V-7M0 7V15"/><text x="16" y="-14">新しい研究</text></g>' : ''}` : ''}</g></svg>`;
  }
  function render(state) {
    if (!state.alive || state.root.isConnected === false) return;
    if (!state.data) {
      state.root.innerHTML = `<div class="citation-flow">${header(state)}<div class="cf-empty" role="status"><span class="cf-eyebrow">CITATION FLOW</span><h2>${state.error ? '引用の流れを読み込めませんでした' : '研究の時層を準備しています'}</h2><p>${esc(state.error || '取得済みの論文と参照関係を、共通の座標系に配置しています。')}</p>${state.error ? '<button type="button" data-cf-action="retry">再試行</button>' : '<div class="cf-loading-line"></div>'}</div></div>`;
      return;
    }
    const data = state.data, win = periodWindow(data.periods, state.start), coverage = data.coverage;
    const validEdges = coverage.valid_edges ?? data.edges.length;
    const body = data.periods.length ? `<div class="cf-workspace"><section class="cf-map-panel"><div class="cf-map-heading"><div><span class="cf-eyebrow">RESEARCH THROUGH TIME</span><strong>引用がつなぐ、研究の時層</strong></div><span class="cf-live-label"><i></i>${state.reduced ? '静止表示' : state.playing ? 'FLOW ACTIVE' : 'FLOW PAUSED'}</span></div><div class="cf-stage" data-cf-stage><div class="cf-stage-note">共通 PCA 座標<br><span>XY = 論文の内容 / 時層 = 出版期間</span></div><div class="cf-time-label cf-time-new">NEW <span>新しい研究</span></div><div class="cf-time-label cf-time-old">OLD <span>先行する研究</span></div>${sceneMarkup(state)}<div class="cf-orbit-note">ドラッグで視点を回転</div>${!data.edges.length ? '<div class="cf-no-edges">照合できた引用線がありません。<br><span>参照情報の欠損・対象外の論文は線を描きません。</span></div>' : !state.scene.edges.length ? '<div class="cf-no-edges">表示中の時層に両端がある引用線はありません。<br><span>年・四半期へ切り替えると広い期間を比較できます。</span></div>' : ''}</div><div class="cf-stage-controls"><label>時層の間隔<input type="range" min="180" max="430" step="5" value="${state.spread}" data-cf-control="spread" aria-label="時層の間隔"></label><button type="button" data-cf-action="reset">⌖ 視点を戻す</button></div><div class="cf-timeline"><button type="button" data-cf-action="older" aria-label="古い期間を表示" ${win.start === 0 ? 'disabled' : ''}>←</button><div><div><span>${esc(win.periods[0]?.label)}</span><span>${esc(win.periods.at(-1)?.label)}</span></div><input type="range" min="0" max="${win.last}" step="1" value="${win.start}" data-cf-control="window" aria-label="表示する時層の期間" ${!win.last ? 'disabled' : ''}></div><button type="button" data-cf-action="newer" aria-label="新しい期間を表示" ${win.start === win.last ? 'disabled' : ''}>→</button><span>${number(win.periods.length)} / ${number(data.periods.length)} 時層</span></div><div class="cf-legend"><span><i class="cf-key-current"></i>新研究の重心</span><span><i class="cf-key-foundation"></i>研究基盤の重心</span><span><i class="cf-key-line"></i>引用元 <b>→</b> 参照先</span><span><i class="cf-key-paper"></i>論文（色＝トピック）</span></div><p class="cf-caption">2つの重心は、比較する引用元の時層に重ねて表示。参照先の論文は、実際の出版時層に配置しています。線は実際の引用関係を示し、因果・支持・研究成果の継承を保証しません。</p></section><aside class="cf-inspector" aria-label="選択した出版期間の引用と重心">${inspector(state)}</aside></div>` : '<div class="cf-empty"><h2>時層に配置できる論文がありません</h2><p>出版日と有効な文章表現を持つ論文が必要です。</p></div>';
    state.root.innerHTML = `<div class="citation-flow" ${state.reduced ? 'data-reduced-motion="true"' : ''}>${header(state)}${controls(state)}<div class="cf-summary"><span><b>${number(coverage.papers_total ?? data.meta.analysis_papers ?? data.nodes.length)}</b> 集計対象論文</span><span><b>${number(validEdges)}</b> 有効な引用関係</span><span><b>${number(coverage.matched_citing_papers ?? 0)}</b> 参照先を照合した引用元</span><span>描画 <b>${number(state.scene?.nodes.length ?? data.nodes.length)}</b>点 / <b>${number(state.scene?.edges.length ?? data.edges.length)}</b>線</span></div>${body}<details class="cf-method"><summary>集計範囲・読み方${data.warnings.length ? ` · ${number(data.warnings.length)}件の注記` : ''}</summary><p>集計は表示標本に限らず、分析対象の全論文に基づきます。描画は最大240点・480線、同時に最大8時層です。トピックは引用元を絞り込み、参照先には別トピックも含みます。未照合の対象外論文はこの画面から追加取得しません。</p><p>研究基盤の重心は、各引用元が参照する論文の中心を先に求め、引用元間で等しく平均します。コサイン距離は参照先を照合できた同一の引用元集合と比較した値です。画面上の2点間の距離とは異なります。共通PCAは内容を圧縮した表示座標です。</p>${data.warnings.map(w => `<p>${esc(typeof w === 'string' ? w : w.message || JSON.stringify(w))}</p>`).join('')}</details></div>`;
    bindScene(state); paintGeometry(state); syncAnimation(state);
  }
  function bindScene(state) {
    const scene = state.scene; if (!scene) return;
    scene.svg = state.root.querySelector('.cf-svg');
    scene.planeEls = Array.from(state.root.querySelectorAll('[data-cf-plane]'));
    scene.nodeEls = Array.from(state.root.querySelectorAll('[data-cf-node-record]'));
    scene.edgeEls = Array.from(state.root.querySelectorAll('[data-cf-edge-record]')).map(el => ({el, line: el.querySelector('.cf-edge-line'), hit: el.querySelector('.cf-edge-hit'), pulse: el.querySelector('.cf-pulse'), glow: el.querySelector('.cf-pulse-glow')}));
    scene.centerEls = Array.from(state.root.querySelectorAll('[data-cf-center]'));
  }
  function pointFor(state, point, period) {
    const layer = state.scene.ranks.get(period), expansion = expansionProgress(state.elapsed, layer, state.reduced);
    return project(point, layer, state.scene.win.periods.length, {camera: state.camera, spread: state.spread, expansion});
  }
  function paintGeometry(state) {
    const scene = state.scene; if (!scene?.svg) return;
    const polygon = points => `M${points.map(p => p.map(fmt).join(',')).join('L')}Z`;
    scene.planeEls.forEach((el, i) => {
      const period = scene.win.periods[i], points = [[0,0],[1,0],[1,1],[0,1]].map(([x,y]) => pointFor(state, {x,y}, period.id));
      el.querySelector('.cf-plane-fill')?.setAttribute('d', polygon(points));
      const lines = [.25, .5, .75].flatMap(f => [[{x:f,y:0},{x:f,y:1}],[{x:0,y:f},{x:1,y:f}]]).map(pair => `M${pair.map(p => pointFor(state, p, period.id).map(fmt).join(',')).join('L')}`).join('');
      el.querySelector('.cf-plane-grid')?.setAttribute('d', lines);
      const label = el.querySelector('.cf-plane-label'); label?.setAttribute('x', fmt(points[3][0] - 4)); label?.setAttribute('y', fmt(points[3][1] + 17));
      el.setAttribute('opacity', fmt(.25 + .75 * expansionProgress(state.elapsed, i, state.reduced)));
    });
    scene.nodeEls.forEach((el, i) => {
      const node = scene.nodes[i], p = pointFor(state, node, node.period_id);
      el.setAttribute('transform', `translate(${p.map(fmt).join(' ')})`);
      el.setAttribute('opacity', fmt(expansionProgress(state.elapsed, scene.ranks.get(node.period_id), state.reduced)));
    });
    scene.geometry.clear();
    scene.edgeEls.forEach((record, i) => {
      const edge = scene.edges[i], a = state.data.byId.get(edge.source_id), b = state.data.byId.get(edge.target_id);
      const geometry = edgeGeometry(pointFor(state, a, a.period_id), pointFor(state, b, b.period_id));
      if (!geometry) return; scene.geometry.set(edge.id, geometry);
      record.line?.setAttribute('d', geometry.path); record.hit?.setAttribute('d', geometry.path);
      record.el.setAttribute('opacity', fmt(Math.min(expansionProgress(state.elapsed, scene.ranks.get(a.period_id), state.reduced), expansionProgress(state.elapsed, scene.ranks.get(b.period_id), state.reduced))));
    });
    const centroid = scene.centroid;
    scene.centerEls.forEach(el => { const point = centroid?.[el.dataset.cfCenter]; if (point) el.setAttribute('transform', `translate(${pointFor(state, point, centroid.period_id).map(fmt).join(' ')})`); });
    if (centroid?.current && centroid?.foundation) {
      const a = pointFor(state, centroid.foundation, centroid.period_id), b = pointFor(state, centroid.current, centroid.period_id);
      scene.svg.querySelector('.cf-centroid-bridge')?.setAttribute('d', `M${a.map(fmt).join(',')}L${b.map(fmt).join(',')}`);
      const text = scene.svg.querySelector('.cf-centroid-bridge-label'); text?.setAttribute('x', fmt((a[0]+b[0])/2)); text?.setAttribute('y', fmt((a[1]+b[1])/2 - 9));
    }
    paintPulses(state);
  }
  function paintPulses(state) {
    state.scene?.edgeEls?.forEach((record, i) => {
      if (!record.pulse) return;
      const geometry = state.scene.geometry.get(state.scene.edges[i].id); if (!geometry) return;
      const t = state.reduced ? .5 : ((state.elapsed + i * 167) % 3600) / 3600, p = bezier(geometry.controls, t);
      for (const el of [record.pulse, record.glow]) { el?.setAttribute('cx', fmt(p[0])); el?.setAttribute('cy', fmt(p[1])); }
    });
  }
  function canAnimate(state) { return state.alive && state.root.isConnected !== false && state.playing && !state.reduced && !state.doc.hidden && !!state.data && !!state.scene?.svg; }
  function stopAnimation(state) { if (state.frame != null) global.cancelAnimationFrame(state.frame); state.frame = null; state.lastFrame = null; }
  function syncAnimation(state) {
    if (!canAnimate(state)) { stopAnimation(state); return; }
    if (state.frame != null) return;
    state.frame = global.requestAnimationFrame(timestamp => {
      state.frame = null; if (!canAnimate(state)) { state.lastFrame = null; return; }
      if (state.lastFrame != null) state.elapsed += Math.min(64, Math.max(0, timestamp - state.lastFrame));
      state.lastFrame = timestamp;
      const expanding = state.elapsed < 850 + (state.scene.win.periods.length - 1) * 90;
      if (expanding || state.wasExpanding || state.geometryDirty) { paintGeometry(state); state.geometryDirty = false; } else paintPulses(state);
      state.wasExpanding = expanding;
      syncAnimation(state);
    });
  }
  async function load(state) {
    const ticket = ++state.request;
    state.abort?.abort(); state.abort = typeof AbortController === 'function' ? new AbortController() : null;
    stopAnimation(state); state.data = null; state.scene = null; state.error = ''; render(state);
    const path = `/api/results/${encodeURIComponent(state.result.id)}/citation-flow?interval=${state.interval}&topic_id=${encodeURIComponent(state.topic)}`;
    try {
      const raw = await state.api(path, state.abort ? {signal: state.abort.signal} : {});
      if (!state.alive || ticket !== state.request || state.root.isConnected === false) return;
      state.data = normalizeData(raw); state.start = null; state.edge = null; state.elapsed = state.reduced ? 2000 : 0;
      if (!state.data.periods.some(p => p.id === state.cohort)) state.cohort = state.data.periods.at(-1)?.id || '';
      revealCohort(state); render(state);
    } catch (error) {
      if (!state.alive || ticket !== state.request || error?.name === 'AbortError') return;
      state.error = error?.message || 'データの読み込みに失敗しました。'; render(state);
    }
  }
  function revealCohort(state) {
    const periods = state.data.periods, index = periods.findIndex(p => p.id === state.cohort), win = periodWindow(periods, state.start);
    if (index < win.start || index >= win.start + win.size) state.start = Math.max(0, Math.min(index - win.size + 1, win.last));
  }
  function selectCohort(state, id) { state.cohort = id; state.edge = null; revealCohort(state); render(state); }
  function handleClick(state, event) {
    if (state.suppressClick) { state.suppressClick = false; event.preventDefault(); event.stopPropagation(); return; }
    const target = event.target.closest?.('[data-cf-action],[data-cf-cohort],[data-cf-edge],[data-paper]'); if (!target) return;
    if (target.dataset.paper != null) { if (state.context.showPaper) { event.stopPropagation(); state.context.showPaper(target.dataset.paper); } return; }
    if (target.dataset.cfCohort) { selectCohort(state, target.dataset.cfCohort); return; }
    if (target.dataset.cfEdge) { state.edge = target.dataset.cfEdge; render(state); return; }
    switch (target.dataset.cfAction) {
      case 'retry': load(state); break;
      case 'play': if (!state.reduced) { state.playing = !state.playing; stopAnimation(state); render(state); } break;
      case 'replay': if (!state.reduced) { state.elapsed = 0; state.playing = true; state.lastFrame = null; render(state); } break;
      case 'reset': state.camera = {...DEFAULT_CAMERA}; paintGeometry(state); break;
      case 'clear-edge': state.edge = null; render(state); break;
      case 'older': case 'newer': { const win = periodWindow(state.data.periods, state.start); state.start = clamp(win.start + (target.dataset.cfAction === 'older' ? -1 : 1), 0, win.last); render(state); break; }
    }
  }
  function handleChange(state, event) {
    const control = event.target.dataset?.cfControl, value = event.target.value;
    if (control === 'interval' && ['year', 'quarter', 'month'].includes(value)) { state.interval = value; state.cohort = ''; load(state); }
    else if (control === 'topic') { state.topic = value; load(state); }
    else if (control === 'cohort') selectCohort(state, value);
    else if (control === 'window') { state.start = Math.floor(clamp(value, 0, periodWindow(state.data.periods).last)); render(state); }
  }
  function bindEvents(state) {
    const on = (target, type, callback, options) => { target?.addEventListener(type, callback, options); state.cleanups.push(() => target?.removeEventListener(type, callback, options)); };
    on(state.root, 'click', event => handleClick(state, event));
    on(state.root, 'change', event => handleChange(state, event));
    on(state.root, 'input', event => { if (event.target.dataset?.cfControl === 'spread') { state.spread = clamp(event.target.value, 180, 430); paintGeometry(state); } });
    on(state.root, 'keydown', event => { if (['Enter', ' '].includes(event.key) && event.target.matches?.('svg [role="button"]')) { event.preventDefault(); handleClick(state, event); } });
    on(state.root, 'pointerdown', event => {
      if (event.button !== 0 || event.isPrimary === false || !event.target.closest?.('.cf-svg')) return;
      state.drag = {id: event.pointerId, x: event.clientX, y: event.clientY, camera: {...state.camera}, moved: false}; state.suppressClick = false;
    });
    on(state.root, 'pointermove', event => {
      const d = state.drag; if (!d || event.pointerId !== d.id) return;
      const dx = event.clientX - d.x, dy = event.clientY - d.y; if (!d.moved && Math.hypot(dx, dy) < 5) return;
      d.moved = true; state.root.setPointerCapture?.(d.id); event.preventDefault();
      state.camera = camera({yaw: d.camera.yaw + dx * .004, pitch: d.camera.pitch - dy * .003});
      state.root.classList.add('cf-orbiting'); state.geometryDirty = true;
      if (!canAnimate(state)) { paintGeometry(state); state.geometryDirty = false; }
    });
    const finish = event => { if (state.drag?.id !== event.pointerId) return; state.suppressClick = state.drag.moved; state.drag = null; state.root.classList.remove('cf-orbiting'); if (state.root.hasPointerCapture?.(event.pointerId)) state.root.releasePointerCapture(event.pointerId); };
    on(state.root, 'pointerup', finish); on(state.root, 'pointercancel', finish);
    on(state.doc, 'visibilitychange', () => { stopAnimation(state); syncAnimation(state); });
    const reduce = () => { state.reduced = !!state.media?.matches; if (state.reduced) { stopAnimation(state); state.elapsed = 2000; } render(state); };
    if (state.media?.addEventListener) on(state.media, 'change', reduce);
    else if (state.media?.addListener) { state.media.addListener(reduce); state.cleanups.push(() => state.media.removeListener(reduce)); }
  }
  function unmountState(state) {
    if (!state || !state.alive) return;
    state.alive = false; ++state.request; state.abort?.abort(); stopAnimation(state); state.cleanups.splice(0).forEach(fn => fn());
    state.drag = null; state.scene = null; state.root.classList.remove('cf-orbiting'); if (mounted === state) mounted = null;
  }
  function mount(root, result, context = {}) {
    if (mounted) unmountState(mounted);
    if (!root || !result?.id) return null;
    const media = global.matchMedia?.('(prefers-reduced-motion: reduce)');
    const state = {root, result, context, api: context.api || (async (url, options) => { const response = await global.fetch(url, options); if (!response.ok) throw new Error(`読み込みに失敗しました (${response.status})`); return response.json(); }), doc: root.ownerDocument || document, uid: `cf-${++sequence}`, media, reduced: !!media?.matches, alive: true, request: 0, cleanups: [], interval: 'year', topic: 'all', cohort: '', start: null, edge: null, spread: 365, camera: {...DEFAULT_CAMERA}, playing: true, elapsed: media?.matches ? 2000 : 0, frame: null, lastFrame: null, data: null, scene: null};
    mounted = state; bindEvents(state); const ready = load(state);
    return {unmount: () => unmountState(state), reload: () => load(state), ready};
  }
  global.AtlasCitationFlow = Object.freeze({mount, unmount: () => unmountState(mounted)});
  if (global.__ATLAS_UI_TEST__) global.__citationFlowTest = {esc, color, normalizeData, periodWindow, project, edgeGeometry, bezier, expansionProgress, inspector, selectedCentroid, sceneMarkup, render, canAnimate, syncAnimation, stopAnimation, load, handleClick, handleChange, get state() { return mounted; }};
})(window);
