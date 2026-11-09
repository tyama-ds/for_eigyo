const test = require('node:test');
const assert = require('node:assert/strict');
const { createDom } = require('./module-harness.cjs');

test('replaceHtml skips an identical signature and restores edited input, details, scroll and focus', async () => {
  const dom = createDom(),
    { replaceHtml } = await import('../static/core.js');
  const host = dom.window.document.querySelector('#step-content');
  const html =
    '<details class="list-toggle"><summary>分類</summary><textarea id="projection-adopt-note"></textarea><div id="scroll"></div></details>';
  replaceHtml(host, html);
  const input = host.querySelector('textarea');
  input.value = '判定境界を確認中';
  input.focus();
  input.setSelectionRange(2, 5);
  host.querySelector('details').open = true;
  host.querySelector('#scroll').scrollTop = 72;
  assert.equal(replaceHtml(host, html).changed, false);
  assert.equal(host.querySelector('textarea'), input);
  replaceHtml(host, '<p>判定更新</p>' + html);
  assert.equal(host.querySelector('textarea').value, '判定境界を確認中');
  assert.equal(host.querySelector('details').open, true);
  assert.equal(host.querySelector('#scroll').scrollTop, 72);
  assert.equal(dom.window.document.activeElement.id, 'projection-adopt-note');
  assert.equal(dom.window.document.activeElement.selectionStart, 2);
});

test('nested projection mounting restores the note captured before the parent map repaint', async () => {
  const dom = createDom(),
    { replaceHtml } = await import('../static/core.js');
  const host = dom.window.document.querySelector('#step-content');
  replaceHtml(host, '<div id="projection"></div>');
  replaceHtml(host.querySelector('#projection'), '<textarea id="projection-adopt-note"></textarea>');
  host.querySelector('textarea').value = '入力途中';
  replaceHtml(host, '<p>判定済み</p><div id="projection"></div>');
  replaceHtml(host.querySelector('#projection'), '<textarea id="projection-adopt-note"></textarea>');
  assert.equal(host.querySelector('textarea').value, '入力途中');
});

test('server updates and deliberate resets are applied instead of reviving stale edits', async () => {
  const dom = createDom(),
    { replaceHtml } = await import('../static/core.js');
  const host = dom.window.document.querySelector('#step-content');
  replaceHtml(host, '<input id="term" value="前回"><input id="use" type="checkbox">');
  host.querySelector('#term').value = '未保存';
  host.querySelector('#use').checked = true;
  replaceHtml(host, '<input id="term" value="採用した案"><input id="use" type="checkbox">');
  assert.equal(host.querySelector('#term').value, '採用した案');
  assert.equal(host.querySelector('#use').checked, true);
  replaceHtml(host, '<input id="term" value="初期値">', { preserve: false });
  assert.equal(host.querySelector('#term').value, '初期値');
});

test('rebinding a retained button replaces its callback rather than adding another action', async () => {
  const dom = createDom(),
    { replaceHtml, listen } = await import('../static/core.js');
  const host = dom.window.document.querySelector('#step-content'),
    calls = [];
  for (const value of [1, 2]) {
    replaceHtml(host, '<button id="action">実行</button>');
    listen(host.querySelector('button'), 'click', () => calls.push(value), undefined, 'action');
  }
  host.querySelector('button').click();
  assert.deepEqual(calls, [2]);
});
test('automatic details defaults follow a mode change while a manual toggle survives', async () => {
  const dom = createDom(),
    { replaceHtml } = await import('../static/core.js');
  const host = dom.window.document.querySelector('#step-content');
  replaceHtml(host, '<details class="target" open><summary>対象</summary></details>');
  replaceHtml(host, '<details class="target"><summary>対象</summary></details>');
  assert.equal(host.querySelector('details').open, false);
  host.querySelector('details').open = true;
  replaceHtml(host, '<p>応答</p><details class="target"><summary>対象</summary></details>');
  assert.equal(host.querySelector('details').open, true);
});

test('debounce uses the last arguments once and supports cancellation and an explicit flush', async () => {
  const { debounce } = await import('../static/core.js');
  const timers = new Map(),
    calls = [];
  let serial = 0;
  const later = debounce((value) => calls.push(value), 10, {
    set(fn) {
      timers.set(++serial, fn);
      return serial;
    },
    clear(id) {
      timers.delete(id);
    },
  });
  later('古い入力');
  later('最新');
  assert.equal(timers.size, 1);
  assert.equal(later.pending(), true);
  later.flush();
  assert.deepEqual(calls, ['最新']);
  assert.equal(timers.size, 0);
  later('取消');
  later.cancel();
  assert.equal(later.pending(), false);
  later.flush();
  assert.deepEqual(calls, ['最新']);
});

test('classification display uses one priority without changing evidence or codes', async () => {
  const { classificationTitle, classificationKey } = await import('../static/core.js');
  const row = {
    kind: 'IPC',
    code: 'B60',
    title_short_ja: '短い名前',
    title_ja: '日本語',
    title: '原文',
    title_en: 'English',
  };
  for (const [key, expected] of [
    ['title_short_ja', '短い名前'],
    ['title_ja', '日本語'],
    ['title', '原文'],
    ['title_en', 'English'],
    ['code', 'B60'],
  ]) {
    const before = JSON.stringify(row);
    assert.equal(classificationTitle(row), expected);
    assert.equal(JSON.stringify(row), before);
    delete row[key];
  }
  assert.equal(classificationKey({ kind: 'IPC', code: 'B60' }), 'IPC:B60');
});
test('switching plan identity cannot carry an old edit to an identically named field', async () => {
  const dom = createDom(),
    { replaceHtml } = await import('../static/core.js');
  const host = dom.window.document.querySelector('#step-content');
  const html = '<input id="replacement" value="既定値">';
  replaceHtml(host, html, { scope: 'example-1' });
  host.querySelector('input').value = '前の式だけの編集';
  replaceHtml(host, html, { scope: 'example-2' });
  assert.equal(host.querySelector('input').value, '既定値');
  host.querySelector('input').value = '取消対象';
  replaceHtml(host, html, { preserve: false });
  assert.equal(host.querySelector('input').value, '既定値');
});
