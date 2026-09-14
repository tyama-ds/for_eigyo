/* Read-only patent previews shared by tables; no workspace or label mutations. */
(function (scope, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (scope && scope.document) {
    const controller = api.createController(scope.document, scope);
    scope.bindPatentRowPreviews = controller.bind;
    scope.hidePatentRowPreview = controller.hide;
  }
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  const TOOLTIP_ID = 'patent-row-tooltip';
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  function text(value, fallback = '未収録') {
    if (Array.isArray(value)) value = value.filter(item => typeof item === 'string').join(' · ');
    if (typeof value !== 'string' && typeof value !== 'number') return fallback;
    if (typeof value === 'number' && !Number.isFinite(value)) return fallback;
    return String(value).trim() || fallback;
  }
  function percentage(value, fallback = '未算出') {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? `${Math.round(value * 100)}%` : fallback;
  }
  function previewHtml(patent = {}) {
    const decision = patent.label === 'keep' ? '必要' : patent.label === 'exclude' ? '不要' : patent.label_source === 'agent' ? '保留' : '未判定';
    const tone = patent.label === 'keep' ? 'keep' : patent.label === 'exclude' ? 'exclude' : 'unlabeled';
    const source = patent.label_source === 'human' ? '利用者' : patent.label_source === 'agent' ? '判断エージェント' : '未収録';
    const relevanceName = patent.score_source === 'llm' ? 'LLMの関連度（自己評価）' : '学習モデルの関連度';
    const scoreNote = patent.score_source === 'llm' ? '関連度と確信度はいずれもLLMの自己評価です。関連性の保証ではありません。' : '関連度は学習モデルのスコア、確信度はAIの自己評価です。';
    const field = (name, value) => `<div><dt>${name}</dt><dd>${escapeHtml(value)}</dd></div>`;
    return `<div class="patent-preview-heading"><span class="patent-preview-eyebrow">PATENT PREVIEW</span><span class="patent-preview-decision ${tone}">${decision}</span></div>
      <h3 class="patent-preview-title">${escapeHtml(text(patent.title, '名称未収録'))}</h3>
      <dl class="patent-preview-meta">${field('公報番号 / ID', text(patent.id))}${field('出願人', text(patent.applicant))}${field('公開年', text(patent.year))}</dl>
      <section class="patent-preview-abstract"><h4>要約</h4><p>${escapeHtml(text(patent.abstract))}</p><small>取り込まれた要約を表示しています。</small></section>
      <dl class="patent-preview-classifications">${field('IPC', text(patent.ipc,'未取込'))}${field('FI', text(patent.fi,'未取込'))}${field('Fターム', text(patent.fterm,'未取込'))}</dl>
      <section class="patent-preview-assessment"><h4>要否の判断</h4><dl>${field('判定者', source)}${field('判定理由', text(patent.label_reason))}</dl>
      <div class="patent-preview-scores"><div><span>${relevanceName}</span><strong>${percentage(patent.score)}</strong></div><div><span>AI判定の確信度</span><strong>${patent.label_source === 'agent' ? percentage(patent.agent_confidence, '未収録') : '対象外'}</strong></div></div>
      <p class="patent-preview-note">${scoreNote}</p></section>
      <div class="patent-preview-footer">スクロールで詳細を確認 · Escで閉じる</div>`;
  }
  function positionPreview(point, size, viewport) {
    const edge = 12, gap = 18;
    const maxX = Math.max(edge, viewport.width - size.width - edge);
    const maxY = Math.max(edge, viewport.height - size.height - edge);
    let x = point.x + gap, y = point.y + gap;
    if (x > maxX) x = point.x - size.width - gap;
    if (y > maxY) y = point.y - size.height - gap;
    return {left: Math.max(edge, Math.min(x, maxX)), top: Math.max(edge, Math.min(y, maxY))};
  }
  function createController(doc, win) {
    let binding = null, panel = null, activeRow = null, hideTimer = null, dismissedRow = null;
    let panelHovered = false, rowHovered = false, focusOwned = false;
    const later = win.setTimeout.bind(win), cancel = win.clearTimeout.bind(win);
    function cancelHide() { if (hideTimer !== null) { cancel(hideTimer); hideTimer = null; } }
    function hide() {
      cancelHide();
      if (panel) { panel.hidden = true; panel.innerHTML = ''; }
      activeRow = null; panelHovered = false; rowHovered = false; focusOwned = false;
    }
    function scheduleHide() {
      cancelHide();
      hideTimer = later(() => { hideTimer = null; if (!panelHovered && !rowHovered && !focusOwned) hide(); }, 220);
    }
    function ensurePanel() {
      if (panel && panel.isConnected !== false) return panel;
      panel = doc.getElementById(TOOLTIP_ID) || doc.createElement('div');
      panel.id = TOOLTIP_ID;
      panel.className = 'patent-row-tooltip';
      panel.setAttribute('role', 'tooltip');
      panel.setAttribute('tabindex', '0');
      panel.setAttribute('aria-label', '特許の要約と判定情報');
      panel.hidden = true;
      panel.addEventListener('mouseenter', () => { panelHovered = true; cancelHide(); });
      panel.addEventListener('mouseleave', () => { panelHovered = false; scheduleHide(); });
      panel.addEventListener('focusin', () => { focusOwned = true; cancelHide(); });
      panel.addEventListener('focusout', event => {
        if (panel.contains(event.relatedTarget) || activeRow?.contains(event.relatedTarget)) return;
        focusOwned = false; rowHovered = false; scheduleHide();
      });
      if (!panel.isConnected) doc.body.appendChild(panel);
      return panel;
    }
    function show(row, patent, event) {
      cancelHide();
      const card = ensurePanel();
      if (activeRow !== row || card.hidden) {
        activeRow = row; card.innerHTML = previewHtml(patent); card.scrollTop = 0;
      }
      card.hidden = false;
      const box = row.getBoundingClientRect();
      const point = event.type === 'mouseover' ? {x: event.clientX, y: event.clientY} : {x: box.left + Math.min(box.width / 2, 240), y: box.bottom};
      const bounds = card.getBoundingClientRect();
      const position = positionPreview(point, {width: bounds.width, height: bounds.height}, {width: win.innerWidth, height: win.innerHeight});
      card.style.left = `${position.left}px`; card.style.top = `${position.top}px`;
    }
    function disposeBinding() {
      hide();
      if (!binding) return;
      binding.listeners.forEach(([target, name, callback, options]) => target.removeEventListener(name, callback, options));
      binding.observer?.disconnect();
      binding = null;
    }
    function bind(root, rows) {
      disposeBinding();
      dismissedRow = null;
      if (!root || typeof root.addEventListener !== 'function') return;
      const byId = new Map((Array.isArray(rows) ? rows : []).map(row => [String(row.id), row]));
      binding = {listeners: [], observer: null};
      const listen = (target, name, callback, options) => { target.addEventListener(name, callback, options); binding.listeners.push([target, name, callback, options]); };
      const findRow = target => {
        const row = target?.closest?.('tr[data-patent-id]');
        return row && root.contains(row) && byId.has(String(row.dataset.patentId)) ? row : null;
      };
      listen(root, 'mouseover', event => {
        const row = findRow(event.target);
        if (!row || row === dismissedRow) return;
        dismissedRow = null;
        rowHovered = true; cancelHide();
        if (row === activeRow && !panel?.hidden) return;
        focusOwned = row.contains(doc.activeElement);
        panelHovered = false;
        show(row, byId.get(String(row.dataset.patentId)), event);
      });
      listen(root, 'mouseout', event => {
        const row = findRow(event.target);
        if (row && row === dismissedRow && !row.contains(event.relatedTarget)) dismissedRow = null;
        if (!row || row !== activeRow || row.contains(event.relatedTarget)) return;
        rowHovered = false; scheduleHide();
      });
      listen(root, 'focusin', event => {
        const row = findRow(event.target);
        if (!row || row === dismissedRow) return;
        dismissedRow = null;
        focusOwned = true; cancelHide();
        show(row, byId.get(String(row.dataset.patentId)), event);
      });
      listen(root, 'focusout', event => {
        if (dismissedRow && !dismissedRow.contains(event.relatedTarget)) dismissedRow = null;
        if (!activeRow || activeRow.contains(event.relatedTarget) || panel?.contains(event.relatedTarget)) return;
        focusOwned = false; rowHovered = false; scheduleHide();
      });
      listen(doc, 'keydown', event => { if (event.key === 'Escape') { const row = activeRow; hide(); dismissedRow = row; } });
      listen(win, 'resize', hide);
      listen(doc, 'scroll', event => { if (!panel || (event.target !== panel && !panel.contains(event.target))) hide(); }, true);
      if (typeof win.MutationObserver === 'function') {
        binding.observer = new win.MutationObserver(() => { if (activeRow && !root.contains(activeRow)) hide(); });
        binding.observer.observe(root, {childList: true, subtree: true});
      }
    }
    return {bind, hide, dispose: disposeBinding};
  }
  return {TOOLTIP_ID, previewHtml, positionPreview, createController};
});
