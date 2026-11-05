const test = require('node:test');
const assert = require('node:assert/strict');

const { loadModule } = require('./module-harness.cjs');
async function setup() {
  const context = {};
  await loadModule(context, 'core.js', ['getState', 'setState', 'patchRows', 'safeUrl', 'api', 'busy']);
  return context;
}
test('state replacement is explicit and returns the same workspace object', async () => {
  const c = await setup(),
    state = { patents: [] };
  assert.equal(c.setState(state), state);
  assert.equal(c.getState(), state);
});
test('row patches preserve references held by map and table listeners', async () => {
  const c = await setup(),
    row = { id: 'JP1', label: 'unreviewed', title: '元の名称' };
  c.setState({ patents: [row] });
  assert.equal(c.patchRows([{ id: 'JP1', label: 'keep', title: '元の名称' }]), true);
  assert.equal(c.getState().patents[0], row);
  assert.equal(row.label, 'keep');
});
test('row patches reject a different population without partially changing rows', async () => {
  const c = await setup(),
    row = { id: 'JP1', label: 'keep' };
  c.setState({ patents: [row] });
  assert.equal(c.patchRows([{ id: 'JP2', label: 'exclude' }]), false);
  assert.equal(row.label, 'keep');
  assert.equal(c.getState().patents[0], row);
});

test('html tag folds line breaks in static text, joins arrays and drops null without escaping', async () => {
  const { html, esc } = await import('../static/core.js');
  const rows = ['<li>a</li>', null, '<li>b</li>'];
  const out = html`<ul class="x">
    ${rows}
    <li>${esc('<c>')}</li>${undefined}
  </ul>`;
  assert.equal(out, '<ul class="x"><li>a</li><li>b</li><li>&lt;c&gt;</li></ul>');
  assert.equal(html`<pre>a\nb</pre>`, '<pre>a\nb</pre>');
  const tag = html`<input id="x"
    data-k="${1}"
    ${'checked'}>
    text</input>`;
  assert.equal(tag, '<input id="x" data-k="1" checked>text</input>');
  assert.equal(html`<a title="t"
    >x</a>`, '<a title="t">x</a>');
  assert.equal(html`<b>${0}</b> ${false}`, '<b>0</b> false');
});
