let state;
let toastTimer;
function getState() {
  return state;
}
function setState(next) {
  state = next;
  return state;
}
// Preserve row identity while polling; callers decide how to handle a changed set.
function patchRows(rows) {
  const latest = new Map((rows || []).map((row) => [row.id, row]));
  if (
    !state?.patents ||
    latest.size !== state.patents.length ||
    state.patents.some((row) => !latest.has(row.id))
  )
    return false;
  for (const row of state.patents) Object.assign(row, latest.get(row.id));
  return true;
}
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
  );
// Tagged template for HTML fragments: arrays are concatenated, null/undefined vanish, everything
// else is inserted as-is. Values are NOT escaped here; callers apply esc() to untrusted text.
function html(strings, ...values) {
  let out = '';
  for (let i = 0; i < strings.length; i++) {
    out += strings[i];
    if (i < values.length) {
      const value = values[i];
      out += Array.isArray(value) ? value.join('') : value == null ? '' : value;
    }
  }
  return out;
}
function safeUrl(value) {
  return typeof value === 'string' && /^https?:\/\//i.test(value) ? value : '';
}
const run =
  (fn) =>
  async (...args) => {
    try {
      await fn(...args);
    } catch (error) {
      toast(error.message, true);
    }
  };
function toast(message, error = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast visible${error ? ' error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('visible'), error ? 8500 : 4000);
}
async function api(path, body, options = {}) {
  const response = await fetch('/api' + path, {
    method: body === undefined ? 'GET' : 'POST',
    ...(body === undefined
      ? {}
      : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    ...options,
  });
  let result;
  try {
    result = await response.json();
  } catch {
    const error = new Error('応答を読み取れません。Pythonサーバーの起動状態を確認してください。');
    error.status = response.status;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(typeof result.detail === 'string' ? result.detail : '入力を確認してください。');
    error.status = response.status;
    throw error;
  }
  return result;
}
function on(selector, event, handler) {
  listen($(selector), event, run(handler), undefined, selector);
}
function busy(button, text, fn) {
  return (async () => {
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = text;
    try {
      return await fn();
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  })();
}
function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export { getState, setState, patchRows, $, $$, esc, html, safeUrl, run, toast, api, on, busy, download };
// DOM state belongs to a mounted host, not to the response which caused a repaint.
const renderedHtml = new WeakMap();
const pendingRestores = new WeakMap();
const detailsDefaults = new WeakMap();
const listeners = new WeakMap();
function listen(node, event, handler, options, key = event) {
  if (!node) return;
  let bindings = listeners.get(node);
  if (!bindings) listeners.set(node, (bindings = new Map()));
  const identity = event + ':' + key;
  const previous = bindings.get(identity);
  if (previous) node.removeEventListener(event, previous.handler, previous.options);
  node.addEventListener(event, handler, options);
  bindings.set(identity, { handler, options });
}
function elementKey(node, index) {
  const scope = (node.closest('[data-render-scope]')?.dataset.renderScope || '') + ':';
  if (node.id) return scope + 'id:' + node.id;
  for (const attr of [
    'data-discovery-detail',
    'data-discovery-focus',
    'data-ql-term',
    'data-ql-class',
    'data-testid',
  ])
    if (node.hasAttribute(attr)) return scope + attr + ':' + node.getAttribute(attr);
  if (node.tagName === 'DETAILS')
    return scope + 'details:' + node.className + ':' + node.querySelector('summary')?.textContent;
  if (node.name)
    return scope + 'name:' + node.name + ':' + (['checkbox', 'radio'].includes(node.type) ? node.value : '');
  return scope + node.tagName + ':' + node.className + ':' + index;
}
function replaceHtml(host, html, { preserve = true, scope } = {}) {
  const unchanged = () => {};
  unchanged.changed = false;
  if (!host) return unchanged;
  html = String(html ?? '');
  if (
    preserve &&
    renderedHtml.get(host) === html &&
    (scope === undefined || host.dataset.renderScope === scope)
  )
    return unchanged;
  const document = host.ownerDocument;
  const selector = 'input,textarea,select,details,[data-preserve-toggle],[data-discovery-focus],[id]';
  const before = new Map();
  const active = document.activeElement;
  const nodes = [...host.querySelectorAll(selector)];
  nodes.forEach((node, index) => {
    const record = { top: node.scrollTop, left: node.scrollLeft };
    if (node.tagName === 'DETAILS' && detailsDefaults.has(node) && node.open !== detailsDefaults.get(node))
      record.open = node.open;
    if (node.hasAttribute('data-preserve-toggle')) record.hidden = node.hidden;
    if (node.matches('input:not([type=file]),textarea,select')) {
      const baseline =
        node.tagName === 'SELECT'
          ? [...node.options]
              .filter((o) => o.defaultSelected)
              .map((o) => o.value)
              .join('\n')
          : node.defaultValue;
      record.input = {
        baseline,
        value: node.value,
        checked: node.checked,
        defaultChecked: node.defaultChecked,
      };
    }
    if (node === active)
      record.focus = {
        start: node.selectionStart,
        end: node.selectionEnd,
        direction: node.selectionDirection,
      };
    before.set(elementKey(node, index), record);
  });
  const hostScroll = [host.scrollTop, host.scrollLeft];
  host.innerHTML = html;
  if (scope !== undefined) host.dataset.renderScope = scope;
  renderedHtml.set(host, html);
  for (const details of host.querySelectorAll('details')) detailsDefaults.set(details, details.open);
  const restore = () => {
    if (!preserve) return;
    [...host.querySelectorAll(selector)].forEach((node, index) => {
      const record = before.get(elementKey(node, index));
      if (!record) return;
      before.delete(elementKey(node, index));
      if (record.open !== undefined) node.open = record.open;
      if (record.hidden !== undefined) node.hidden = record.hidden;
      const input = record.input;
      if (input && node.matches('input:not([type=file]),textarea,select')) {
        const baseline =
          node.tagName === 'SELECT'
            ? [...node.options]
                .filter((o) => o.defaultSelected)
                .map((o) => o.value)
                .join('\n')
            : node.defaultValue;
        // An intentional model update wins. Only the still-uncommitted edit survives.
        if (baseline === input.baseline && input.value !== input.baseline) node.value = input.value;
        if (node.defaultChecked === input.defaultChecked && input.checked !== input.defaultChecked)
          node.checked = input.checked;
      }
      if (record.focus) {
        node.focus({ preventScroll: true });
        if (
          Number.isInteger(record.focus.start) &&
          ['text', 'search', 'url', 'tel', 'password', undefined].includes(node.type)
        )
          node.setSelectionRange?.(record.focus.start, record.focus.end, record.focus.direction);
        else if (node.tagName === 'TEXTAREA')
          node.setSelectionRange(record.focus.start, record.focus.end, record.focus.direction);
      }
      node.scrollTop = record.top;
      node.scrollLeft = record.left;
    });
    [host.scrollTop, host.scrollLeft] = hostScroll;
  };
  pendingRestores.set(host, restore);
  restore();
  for (let parent = host.parentElement; parent; parent = parent.parentElement)
    pendingRestores.get(parent)?.();
  restore.changed = true;
  return restore;
}
export { replaceHtml, listen };
function classificationKey(item, explicit = true) {
  return (explicit && item.key) || `${item.kind || ''}:${item.code || ''}`;
}
function classificationTitle(item = {}) {
  return String(item.title_short_ja || item.title_ja || item.title || item.title_en || item.code || '');
}
const judgmentLabels = Object.freeze({ keep: '必要', exclude: '不要', unjudged: '未判定', deferred: '保留' });
const suggestionLabels = Object.freeze({
  keep: '必要の可能性',
  exclude: '不要の可能性',
  unsure: '判断困難',
  defer: '判断困難',
});
function judgmentLabel(label, fallback = judgmentLabels.unjudged) {
  return judgmentLabels[label] || fallback;
}
function tooltipPosition(point, size, viewport, { edge = 12, gap = 18, reverseY = gap, flip = true } = {}) {
  const maxX = Math.max(edge, viewport.width - size.width - edge);
  const maxY = Math.max(edge, viewport.height - size.height - edge);
  let x = point.x + gap,
    y = point.y + gap;
  if (flip && x > maxX) x = point.x - size.width - gap;
  if (flip && y > maxY) y = point.y - size.height - reverseY;
  return { left: Math.max(edge, Math.min(x, maxX)), top: Math.max(edge, Math.min(y, maxY)) };
}
function debounce(
  callback,
  delay,
  timers = { set: (fn, ms) => setTimeout(fn, ms), clear: (id) => clearTimeout(id) }
) {
  let timer = null,
    arguments_;
  const cancel = () => {
    if (timer !== null) timers.clear(timer);
    timer = null;
    arguments_ = undefined;
  };
  const flush = () => {
    if (timer === null) return;
    const args = arguments_;
    cancel();
    return callback(...args);
  };
  const schedule = (...args) => {
    cancel();
    arguments_ = args;
    timer = timers.set(flush, delay);
  };
  schedule.cancel = cancel;
  schedule.flush = flush;
  schedule.pending = () => timer !== null;
  return schedule;
}
export { classificationKey, classificationTitle, suggestionLabels, judgmentLabel, tooltipPosition, debounce };
