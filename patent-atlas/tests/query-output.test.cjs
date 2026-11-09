const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loadModule } = require('./module-harness.cjs');

test('shared URL validation accepts only HTTP and HTTPS strings', async () => {
  const c = {};
  await loadModule(c, 'core.js', ['safeUrl']);
  for (const value of [
    'javascript:alert(1)',
    'JavaScript:alert(1)',
    'java\nscript:alert(1)',
    'data:text/html,test',
    'file:///tmp/test',
    '//example.test',
    '/relative',
    '',
    null,
    undefined,
    42,
    { toString: () => 'https://example.test' },
  ])
    assert.equal(c.safeUrl(value), '', String(value));
  for (const value of ['http://localhost:8765/', 'https://example.test/', 'HTTPS://example.test/'])
    assert.equal(c.safeUrl(value), value);
});

test('query output omits unsafe source and service links and retains safe links', async () => {
  const nodes = {};
  const node = (selector) =>
    (nodes[selector] ||= {
      dataset: { queryId: 'q1' },
      value: '',
      checked: false,
      href: 'https://previous.test',
      setAttribute(name, value) {
        this[name] = value;
      },
      removeAttribute(name) {
        delete this[name];
      },
    });
  const result = {
    can_copy: true,
    expression: '電池/TX',
    problems: [],
    warnings: [],
    instructions: '',
    text_scope: '',
    classification_note: '',
    verification: '',
    verified_at: '',
    sources: [],
    url: '',
  };
  const c = {
    $: node,
    $$: () => [],
    exportRequest: 0,
    outputEdits: new Map(),
    api: async () => result,
  };
  (await loadModule(c, 'core.js', ['safeUrl']),
    await loadModule(c, 'query-output.js', ['queryCard', 'requestOutput', 'historyHtml']));
  for (const url of [
    'javascript:alert(1)',
    'data:text/html,test',
    'https://example.test/?q="分類"&x=1',
    'http://localhost:8765/',
    'javascript:alert(2)',
  ]) {
    result.url = url;
    result.sources = [{ url, title: '<img src=x onerror="alert(1)">' }];
    await c.requestOutput({ id: 'q1' });
    const html = node('#output-meta').innerHTML;
    assert.equal(node('#expression').textContent, '電池/TX');
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img/);
    if (url.startsWith('http')) {
      assert.equal(node('#open-search-service').href, url);
      assert.match(html, /<a href="https?:\/\//);
      if (url.includes('分類')) assert.match(html, /&quot;分類&quot;&amp;x=1/);
    } else {
      assert.doesNotMatch(html, /\bhref\s*=/);
      assert.equal(Object.hasOwn(node('#open-search-service'), 'href'), false);
    }
  }
});

test('query history and format identifiers cannot break out of HTML attributes', async () => {
  const id = 'x" onclick="alert(1)';
  const query = { id, type: '初案', version: 1, expression: '', created_at: '' };
  const c = {
    state: { queries: [query] },
    formatCatalog: [{ id, name: '形式' }],
    outputFormat: id,
    outputEdits: new Map(),
  };
  (await loadModule(c, 'core.js', ['safeUrl']),
    await loadModule(c, 'query-output.js', ['queryCard', 'requestOutput', 'historyHtml']));
  assert.match(c.historyHtml(), /data-id="x&quot; onclick=&quot;alert\(1\)"/);
  assert.match(c.queryCard(query), /option value="x&quot; onclick=&quot;alert\(1\)"/);
});

test('a delayed edit from a previous history card preserves the current export', async () => {
  const current = { query_id: 'new', can_copy: true, expression: 'battery/TX' };
  let disabled = 0;
  const context = {
    currentOutput: current,
    exportRequest: 8,
    $: (selector) =>
      selector === '#query-output' ? { dataset: { queryId: 'new' } } : { setAttribute: () => disabled++ },
    api: () => {
      throw new Error('obsolete request must not be sent');
    },
  };
  context.setState = (next) => (context.state = next);
  context.getState = () => context.state;
  await loadModule(context, 'query-output.js', ['queryCard', 'requestOutput', 'historyHtml']);
  await context.requestOutput({ id: 'old' });
  assert.equal(context.currentOutput, current);
  assert.equal(context.exportRequest, 8);
  assert.equal(disabled, 0);
});

test('a delayed edit after leaving the search step has no side effects', async () => {
  const context = { currentOutput: null, exportRequest: 8, $: () => null };
  context.setState = (next) => (context.state = next);
  context.getState = () => context.state;
  await loadModule(context, 'query-output.js', ['queryCard', 'requestOutput', 'historyHtml']);
  await context.requestOutput({ id: 'old' });
  assert.equal(context.exportRequest, 8);
});

test('query cards include actual classification-change evidence through the shared lexical helper', async () => {
  const context = {
    formatCatalog: [{ id: 'jplatpat', name: 'J-PlatPat' }],
    outputFormat: 'jplatpat',
    outputEdits: new Map(),
    esc: (value) =>
      String(value ?? '').replace(
        /[&<>"']/g,
        (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]
      ),
  };
  context.setState = (next) => (context.state = next);
  context.getState = () => context.state;
  const refinementSource = fs.readFileSync('static/refinement-ui.js', 'utf8');
  // Exercise a shared script binding that is not a globalThis property. Use the
  // real refinement renderer so query-card integration cannot silently vanish.
  await loadModule(context, 'refinement-ui.js', ['classificationChangesHtml']);
  await loadModule(context, 'query-output.js', ['queryCard']);
  const query = {
    id: 'refined',
    version: 2,
    type: '改善案',
    expression: '自動運転/TX',
    keywords: ['自動運転'],
    classification_changes: {
      added_keys: ['IPC:B60W60/00'],
      removed_keys: ['IPC:H01M10/00'],
      evidence: [
        {
          kind: 'IPC',
          code: 'B60W60/00',
          keep_count: 2,
          exclude_count: 1,
          reason: '必要公報で支持された分類',
          keep_ids: ['JP202400001A', 'JP<support>'],
          exclude_ids: ['JP202400002A'],
        },
      ],
    },
  };
  const html = context.queryCard(query);
  assert.ok(html.includes('判定・学習から反映したIPCと根拠'));
  assert.ok(html.includes('追加：IPC:B60W60/00'));
  assert.ok(html.includes('解除：IPC:H01M10/00'));
  assert.ok(html.includes('必要公報で支持された分類'));
  assert.ok(html.includes('JP202400001A'));
  assert.ok(html.includes('JP202400002A'));
  assert.ok(html.includes('JP&lt;support&gt;'));
  assert.ok(!html.includes('JP<support>'));
  const initial = context.queryCard({
    ...query,
    id: 'initial',
    version: 1,
    type: '初案',
    classification_changes: undefined,
  });
  assert.ok(!initial.includes('classification-feedback-history'));
  assert.ok(!initial.includes('判定・学習から反映したIPCと根拠'));
});

test('report tables use the selected export options and the report preview holds the markdown', async () => {
  const nodes = {};
  const node = (selector) =>
    (nodes[selector] ||= {
      dataset: { queryId: 'q1' },
      value:
        selector === '#output-format'
          ? 'derwent_innovation'
          : selector === '#report-period-from'
            ? '2013'
            : '',
      checked: selector === '#report-local-hits',
      disabled: true,
      textContent: '',
      innerHTML: '',
      events: new Map(),
      addEventListener(name, fn) {
        this.events.set(name, fn);
      },
      setAttribute(name, value) {
        this[name] = value;
      },
      removeAttribute(name) {
        delete this[name];
      },
    });
  const requests = [];
  const c = {
    $: node,
    $$: () => [],
    on: (selector, event, fn) => node(selector).addEventListener(event, fn),
    exportRequest: 0,
    outputEdits: new Map(),
    api: async (path, body) => {
      requests.push({ path, body });
      return path === '/query/report'
        ? {
            query_id: 'q1',
            syntax: 'derwent_innovation',
            markdown: '### 調査条件\n| 項目 |',
            tsv: 'a\tb',
            problems: ['保留 <x>'],
            warnings: [],
          }
        : {
            can_copy: true,
            expression: '',
            problems: [],
            warnings: [],
            instructions: '',
            text_scope: '',
            classification_note: '',
            verification: '',
            verified_at: '',
            sources: [],
            url: '',
          };
    },
    setStep() {},
  };
  await loadModule(c, 'core.js', ['safeUrl']);
  await loadModule(c, 'query-output.js', ['queryCard', 'bindQuery']);
  const card = c.queryCard({ id: 'q1', version: 1, type: '初案', expression: 'x', keywords: ['電池'] });
  assert.match(card, /id="output-report"/);
  assert.match(card, /id="report-period-from"/);
  assert.match(card, /id="make-report"/);
  c.bindQuery({ id: 'q1', version: 1 });
  await node('#make-report').events.get('click')();
  const request = requests.find((r) => r.path === '/query/report');
  assert.equal(request.body.format, 'derwent_innovation');
  assert.equal(request.body.options.period_from, '2013');
  assert.equal(request.body.options.basis, 'priority');
  assert.equal(request.body.local_hits, true);
  assert.equal(node('#report-preview').textContent, '### 調査条件\n| 項目 |');
  assert.match(node('#report-feedback').innerHTML, /保留 &lt;x&gt;/);
  assert.equal(node('#copy-report').disabled, false);
});
