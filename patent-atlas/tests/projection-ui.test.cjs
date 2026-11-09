const test = require('node:test');
const assert = require('node:assert/strict');
const { loadModule, createDom } = require('./module-harness.cjs');
const fs = require('node:fs');

const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
  );
async function setup() {
  const testDom = createDom(),
    document = testDom.window.document;
  document.documentElement.dataset.atlasMotion = 'static';
  document.querySelector('#step-content').innerHTML =
    '<svg id="map-svg"></svg><input id="projection-method" value="pca"><div id="projection-status"></div><div id="projection-evaluation"></div>';
  const dom = new Proxy({}, { get: (target, key) => target[key] || document.querySelector(key) });
  const calls = [],
    notices = [];
  const context = {
    testDom,
    state: {
      patents: [
        { id: 'a', title: '一', abstract: '内容' },
        { id: 'b', title: '二', abstract: '他' },
      ],
    },
    esc,
    $: (id) => dom[id] || null,
    $$: () => [],
    api: async (path, body) => {
      calls.push({ path, body });
      return {};
    },
    toast: (...a) => notices.push(a),
    on: () => {},
    document,
    window: { matchMedia: () => ({ matches: false }) },
    setTimeout,
    clearTimeout,
    performance: { now: () => 0 },
    cancelAnimationFrame: () => {},
    requestAnimationFrame: () => 0,
  };
  context.setState = (next) => (context.state = next);
  context.getState = () => context.state;
  await loadModule(context, 'projection-ui.js', [
    'projectionDatasetKey',
    'projectionStill',
    'projectionEvaluationHtml',
    'projectionTooltipHtml',
    'projectionAdoptionHtml',
    'projectionPinColor',
    'projectionSetZoom',
    'projectionZoom',
    'projectionRenderPins',
    'projectionDraw',
    'projectionRenderEvaluation',
    'projectionRequest',
  ]);
  context.projectionDraw = () => {};
  context.projectionRenderEvaluation = () => {};
  context.projectionRenderPins = () => {};
  return { context, dom, calls, notices };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function result(method = 'pca') {
  return {
    projection: {
      method,
      points: [
        { id: 'a', x: 0.2, y: 0.3 },
        { id: 'b', x: 0.8, y: 0.7 },
      ],
      metadata: { algorithm: method, representation: '特徴', note: '注記' },
    },
    evaluation: { human_keep: 1 },
  };
}
test('dataset key depends on content and order but not labels or projections', async () => {
  const { context: c } = await setup();
  const a = [
    { id: 'a', title: 'x', abstract: 'y' },
    { id: 'b', title: 'z' },
  ];
  assert.equal(
    c.projectionDatasetKey(a),
    c.projectionDatasetKey(a.map((r) => ({ ...r, label: 'keep', x: 0.8 })))
  );
  assert.notEqual(
    c.projectionDatasetKey(a),
    c.projectionDatasetKey(a.map((r) => ({ ...r, abstract: 'changed' })))
  );
  assert.notEqual(c.projectionDatasetKey(a), c.projectionDatasetKey([...a].reverse()));
});
test('review scope, null denominators, AI counts and unsafe text are rendered honestly', async () => {
  const { context: c } = await setup();
  const html = c.projectionEvaluationHtml({
    human_keep: 1,
    human_exclude: 0,
    human_reviewed: 1,
    machine_keep: 20,
    machine_exclude: 10,
    unreviewed_count: 100,
    hold_count: 3,
    signal_ratio: 1,
    signal_noise_ratio: null,
    sn_status: 'no_excludes',
    scope_note: '部分集合<scope>',
    boundary_note: '元の空間',
    boundary_candidates: [{ id: '<img>', reason: '<script>', kind: 'llm_hold' }],
    boundary_candidate_count: 1,
  });
  assert.match(html, /不要0件/);
  assert.match(html, /100.0%/);
  assert.match(html, /部分集合&lt;scope&gt;/);
  assert.doesNotMatch(html, /<img>|<script>|Infinity/);
  assert.match(html, /30 \/ 100/);
});
test('tooltip escapes patent content and includes abstract and classifications', async () => {
  const { context: c } = await setup();
  const html = c.projectionTooltipHtml({
    id: '<img>',
    title: '"title"',
    abstract: '<script>本文',
    ipc: 'B60',
    fi: 'B60W',
    cpc: 'B60W60/00',
    label_source: 'human',
    label_reason: '理由',
  });
  assert.match(html, /&lt;img&gt;/);
  assert.match(html, /&lt;script&gt;本文/);
  assert.match(html, /B60W60\/00/);
  assert.match(html, /人の判断/);
  assert.doesNotMatch(html, /<script>/);
});
test('pin color is identity-based, not current row position', async () => {
  const { context: c } = await setup();
  const before = c.projectionPinColor('JP1');
  c.state.patents.reverse();
  assert.equal(c.projectionPinColor('JP1'), before);
});
test('static and OS reduced-motion both disable travel animation', async () => {
  const { context: c } = await setup();
  assert.equal(c.projectionStill(), true);
  c.document.documentElement.dataset.atlasMotion = 'dynamic';
  assert.equal(c.projectionStill(), false);
  c.window.matchMedia = () => ({ matches: true });
  assert.equal(c.projectionStill(), true);
});
test('new projection response keeps selected method and pinned identities', async () => {
  const s = await setup(),
    c = s.context;
  c.api = async () => result('tsne');
  s.context.atlasProjection.pins.add('b');
  await c.projectionRequest('tsne');
  assert.equal(s.context.atlasProjection.method, 'tsne');
  assert.equal(s.context.atlasProjection.pins.has('b'), true);
  assert.equal(s.dom['#projection-method'].disabled, false);
});
test('switching projection refreshes the already open patent detail without changing its identity or tracking', async () => {
  const s = await setup(),
    c = s.context;
  await loadModule(c, 'map-ui.js', ['showPatent']);
  c.state.clusters = [];
  s.dom['#patent-detail'] = { dataset: {}, innerHTML: '' };
  (() => {
    s.context.atlasProjection.data = { method: 'pca', metadata: { algorithm: '中心化した特徴表現のPCA' } };
    s.context.atlasProjection.pins.add('b');
    s.context.atlasProjection.zoom = { x: 12, y: 35, k: 2 };
  })();
  c.showPatent(c.state.patents[1]);
  assert.match(s.dom['#patent-detail'].innerHTML, /中心化した特徴表現のPCA/);
  const fresh = result('tsne');
  fresh.projection.metadata.algorithm = 't-SNE';
  c.api = async () => fresh;
  await c.projectionRequest('tsne');
  assert.match(s.dom['#patent-detail'].innerHTML, /t-SNE/);
  assert.doesNotMatch(s.dom['#patent-detail'].innerHTML, /中心化した特徴表現のPCA/);
  assert.equal(s.dom['#patent-detail'].dataset.patentId, 'b');
  assert.equal(s.context.atlasProjection.pins.has('b'), true);
  assert.equal(JSON.stringify(s.context.atlasProjection.zoom), '{"x":12,"y":35,"k":2}');
});
test('failed projection leaves the open patent detail describing the retained method', async () => {
  const s = await setup(),
    c = s.context;
  s.dom['#patent-detail'] = { dataset: { patentId: 'b' }, innerHTML: 'PCA current detail' };
  let redraws = 0;
  c.showPatent = () => redraws++;
  c.api = async () => {
    throw new Error('projection failed');
  };
  await c.projectionRequest('umap');
  assert.equal(redraws, 0);
  assert.equal(s.dom['#patent-detail'].innerHTML, 'PCA current detail');
});
test('failed UMAP leaves previous coordinates and method unchanged', async () => {
  const s = await setup(),
    c = s.context;
  s.context.atlasProjection.data = { method: 'pca', points: [{ id: 'a', x: 0.1, y: 0.2 }] };
  c.api = async () => {
    throw new Error('pip install umap-learn');
  };
  await c.projectionRequest('umap');
  assert.equal(s.context.atlasProjection.data.points[0].x, 0.1);
  assert.equal(s.context.atlasProjection.method, 'pca');
  assert.match(s.dom['#projection-status'].textContent, /pip install/);
  assert.equal(s.dom['#projection-method'].value, 'pca');
});
test('late response cannot replace newer method selection', async () => {
  const s = await setup(),
    c = s.context,
    a = deferred(),
    b = deferred();
  c.api = (_p, body) => (body.method === 'pca' ? a.promise : b.promise);
  const first = c.projectionRequest('pca'),
    second = c.projectionRequest('tsne');
  b.resolve(result('tsne'));
  await second;
  a.resolve(result('pca'));
  await first;
  assert.equal(s.context.atlasProjection.method, 'tsne');
});
test('dataset replaced while projection is running rejects response', async () => {
  const s = await setup(),
    c = s.context,
    a = deferred();
  c.api = () => a.promise;
  const pending = c.projectionRequest('tsne');
  c.state.patents[0].abstract = 'changed';
  a.resolve(result('tsne'));
  await pending;
  assert.equal(s.context.atlasProjection.data, null);
});
test('detached map cannot receive a stale projection', async () => {
  const s = await setup(),
    c = s.context,
    a = deferred();
  c.api = () => a.promise;
  const pending = c.projectionRequest('tsne');
  s.dom['#map-svg'] = {};
  a.resolve(result('tsne'));
  await pending;
  assert.equal(s.context.atlasProjection.data, null);
});
test('duplicate or nonfinite returned coordinates never replace a valid layout', async () => {
  for (const points of [
    [
      { id: 'a', x: 0, y: 0 },
      { id: 'a', x: 1, y: 1 },
    ],
    [
      { id: 'a', x: NaN, y: 0 },
      { id: 'b', x: 1, y: 1 },
    ],
    [{ id: 'a', x: 0, y: 0 }],
  ]) {
    const s = await setup(),
      c = s.context;
    c.api = async () => ({ ...result('tsne'), projection: { ...result('tsne').projection, points } });
    await c.projectionRequest('tsne');
    assert.equal(s.context.atlasProjection.data, null);
    assert.equal(s.notices.length, 1);
  }
});
test('zoom is bounded and preserves cursor anchor', async () => {
  const s = await setup(),
    c = s.context;
  c.projectionSetZoom = () => {};
  c.projectionZoom(2, { x: 100, y: 50 });
  assert.equal(s.context.atlasProjection.zoom.k, 2);
  assert.equal(s.context.atlasProjection.zoom.x, -100);
  c.projectionZoom(100);
  assert.equal(s.context.atlasProjection.zoom.k, 12);
  c.projectionZoom(0.0001);
  assert.equal(s.context.atlasProjection.zoom.k, 0.5);
});
test('adoption labels unknown CSV provenance instead of presenting S/N as validation of the new query', async () => {
  const { context: c } = await setup();
  c.state.queries = [{ id: 'q-new', version: 3 }];
  c.state.search_result = { query_id: 'q-old' };
  let html = c.projectionAdoptionHtml();
  assert.match(html, /別の検索式/);
  assert.match(html, /参考資料として扱い/);
  assert.match(html, /再現率ではありません/);
  c.state.search_result.query_id = 'q-new';
  html = c.projectionAdoptionHtml();
  assert.doesNotMatch(html, /projection-adopt-unlinked/);
  assert.match(html, /検索式 v3/);
});
test('adoption history escapes untrusted notes and does not require any existing query', async () => {
  const { context: c } = await setup();
  assert.match(c.projectionAdoptionHtml(), /初案または次の検索式/);
  c.state.queries = [{ id: '<unsafe>', version: 1 }];
  c.state.research_workbench = {
    adoptions: [{ note: '<script>bad', created_at: 'today', evaluation: { human_reviewed: 2 } }],
  };
  const html = c.projectionAdoptionHtml();
  assert.match(html, /&lt;script&gt;bad/);
  assert.match(html, /&lt;unsafe&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
