const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { createDom } = require('./module-harness.cjs');

test('all production modules import natively without leaking declarations into the global scope', async () => {
  const document = createDom().window.document;
  const before = new Set(Object.getOwnPropertyNames(globalThis));
  for (const file of fs.readdirSync('static').filter((name) => name.endsWith('.js') && name !== 'main.js')) {
    await import('../static/' + file);
    const source = fs.readFileSync('static/' + file, 'utf8');
    assert.doesNotMatch(source, /typeof\s+\w+\s*[!=]==?\s*['"](?:function|undefined)['"]/, file);
    if (file !== 'core.js') assert.doesNotMatch(source, /(?<![.\w-])state\s*=(?!=)/, file);
  }
  assert.deepEqual(
    Object.getOwnPropertyNames(globalThis).filter((key) => !before.has(key)),
    []
  );
  const scripts = [...document.querySelectorAll('script[src]')];
  assert.equal(scripts.length, 1);
  assert.equal(scripts[0].type, 'module');
  assert.equal(scripts[0].getAttribute('src'), '/static/main.js');
});

test('panel modules expose render(host, snapshot) and bootstrap contains no panel markup', async () => {
  createDom();
  for (const file of [
    'classification-ui',
    'query-output',
    'map-ui',
    'refinement-ui',
    'research-workbench',
    'discovery-ui',
    'orchestration-ui',
    'agent-ui',
    'settings-panel',
    'query-library-ui',
    'invention-workbench',
  ]) {
    const module = await import('../static/' + file + '.js');
    assert.equal(typeof module.render, 'function', file);
  }
  assert.doesNotMatch(fs.readFileSync('static/app.js', 'utf8'), /innerHTML|outerHTML|<section|<div/);
  for (const file of fs
    .readdirSync('tests')
    .filter((name) => name.endsWith('.test.cjs') && name !== 'modules.test.cjs')) {
    const source = fs.readFileSync('tests/' + file, 'utf8');
    assert.doesNotMatch(source, /node:vm|vm\.run|indexOf\(['"](?:async )?function/);
  }
});
