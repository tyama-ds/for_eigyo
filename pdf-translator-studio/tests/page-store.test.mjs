import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createPageStore } from '../dist/page-store.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function fixture(count = 3, dimensions = () => [600, 800]) {
  const calls = { render: [], extract: [], destroy: 0, cleanup: [], canvases: [] };
  const proxies = Array.from({ length: count }, (_, index) => ({
    pageNumber: index + 1,
    getViewport: () => { const [width, height] = dimensions(index); return { width, height, scale: 1 }; },
    cleanup: () => calls.cleanup.push(index + 1),
  }));
  const doc = { numPages: count, getPage: async n => proxies[n - 1],
    loadingTask: { destroy: async () => { calls.destroy++; } } };
  const engine = {
    async renderPage(document, n, scale) {
      assert.equal(document, doc);
      calls.render.push(n);
      const page = proxies[n - 1];
      const viewport = page.getViewport();
      const canvas = { width: viewport.width * scale, height: viewport.height * scale };
      calls.canvases.push(canvas);
      return { pageNumber: n, page, viewport, ...viewport, renderScale: scale, canvas, blocks: [] };
    },
    async extractBlocks(page) {
      calls.extract.push(page.pageNumber);
      return [{ id: 'temporary', original: `Page ${page.pageNumber}`, translation: '', x: 10,
        sourceRect: { x: 10, y: 20, width: 30, height: 12 } }];
    },
  };
  return { calls, doc, engine, proxies };
}

test('269-page document initializes metadata without allocating any page bitmap', async () => {
  const f = fixture(269, index => index === 268 ? [800, 600] : [600, 800]);
  const progress = [];
  const store = await createPageStore(f.doc, { engine: f.engine, onProgress: (...args) => progress.push(args) });
  assert.equal(store.pages.length, 269);
  assert.deepEqual(f.calls.render, []);
  assert.deepEqual(f.calls.extract, []);
  assert.ok(store.pages.every(page => page.canvas === null && !page.loaded && page.blocks.length === 0));
  assert.deepEqual([store.pages[268].width, store.pages[268].height], [800, 600]);
  assert.deepEqual(progress.at(-1), [269, 269]);
  await store.ensurePage(268);
  assert.deepEqual(f.calls.render, [269]);
  assert.equal(store.pages[268].blocks[0].id, 'p269-b1');
  await store.dispose();
});

test('revisiting released pages retains edits and immutable original block snapshots', async () => {
  const f = fixture();
  const loaded = [];
  const store = await createPageStore(f.doc, { engine: f.engine, onPageLoaded: p => loaded.push(p.pageNumber) });
  const page = store.pages[0];
  assert.equal(await store.ensurePage(page), page);
  const bitmap = page.canvas;
  const edited = page.blocks;
  edited[0].translation = '翻訳済み';
  edited[0].sourceRect.x = 25;
  store.releasePage(page);
  assert.equal(page.canvas, null);
  assert.deepEqual([bitmap.width, bitmap.height], [1, 1]);
  assert.equal(page.loaded, true);
  assert.equal(page.originalBlocks[0].translation, '');
  assert.equal(page.originalBlocks[0].sourceRect.x, 10);
  await store.ensurePage(page);
  assert.equal(page.blocks, edited);
  assert.equal(page.blocks[0].translation, '翻訳済み');
  assert.deepEqual(f.calls.extract, [1]);
  assert.deepEqual(f.calls.render, [1, 1]);
  assert.deepEqual(loaded, [1]);
  await store.dispose();
});

test('concurrent ensures share one load and release during that load frees its eventual bitmap', async () => {
  const f = fixture();
  const gate = deferred();
  const render = f.engine.renderPage;
  f.engine.renderPage = async (...args) => { await gate.promise; return render(...args); };
  const store = await createPageStore(f.doc, { engine: f.engine });
  const first = store.ensurePage(0);
  const duplicate = store.ensurePage(store.pages[0]);
  assert.equal(first, duplicate);
  store.releasePage(0);
  gate.resolve();
  const state = await first;
  assert.equal(state.canvas, null);
  assert.equal(state.loaded, true);
  assert.deepEqual(f.calls.render, [1]);
  assert.deepEqual(f.calls.canvases.map(c => [c.width, c.height]), [[1, 1]]);
  await store.dispose();
});

test('requesting an in-flight page again after release retains the requested bitmap', async () => {
  const f = fixture();
  const gate = deferred();
  const render = f.engine.renderPage;
  f.engine.renderPage = async (...args) => { await gate.promise; return render(...args); };
  const store = await createPageStore(f.doc, { engine: f.engine });
  const first = store.ensurePage(0);
  store.releasePage(0);
  assert.equal(store.ensurePage(0), first);
  gate.resolve();
  await first;
  assert.ok(store.pages[0].canvas);
  await store.dispose();
});

test('iterating all 269 pages can keep the number of retained bitmaps at one', async () => {
  const f = fixture(269);
  const store = await createPageStore(f.doc, { engine: f.engine });
  for (const page of store.pages) {
    await store.ensurePage(page);
    assert.equal(store.pages.filter(p => p.canvas).length, 1);
    store.releasePage(page);
  }
  assert.ok(store.pages.every(p => p.loaded && !p.canvas));
  assert.equal(f.calls.extract.length, 269);
  assert.ok(f.calls.canvases.every(c => c.width === 1 && c.height === 1));
  await store.dispose();
});

test('an extraction failure releases the bitmap and can be retried without stale partial blocks', async () => {
  const f = fixture();
  const extract = f.engine.extractBlocks;
  let fail = true;
  f.engine.extractBlocks = (...args) => { if (fail) throw new Error('temporary extraction failure'); return extract(...args); };
  const store = await createPageStore(f.doc, { engine: f.engine });
  await assert.rejects(store.ensurePage(0), /temporary extraction/);
  assert.equal(store.pages[0].loaded, false);
  assert.deepEqual(store.pages[0].blocks, []);
  assert.deepEqual([f.calls.canvases[0].width, f.calls.canvases[0].height], [1, 1]);
  fail = false;
  await store.ensurePage(0);
  assert.equal(store.pages[0].loaded, true);
  await store.dispose();
});

test('disposing while extraction is pending destroys the worker once and prevents late state publication', async () => {
  const f = fixture();
  const gate = deferred();
  const started = deferred();
  const extract = f.engine.extractBlocks;
  f.engine.extractBlocks = async (...args) => { started.resolve(); await gate.promise; return extract(...args); };
  f.doc.loadingTask.destroy = async () => { f.calls.destroy++; gate.resolve(); };
  const store = await createPageStore(f.doc, { engine: f.engine });
  const load = store.ensurePage(0);
  const rejected = assert.rejects(load, { name: 'AbortError' });
  await started.promise;
  const dispose = store.dispose();
  assert.equal(store.dispose(), dispose);
  await dispose;
  await rejected;
  assert.equal(f.calls.destroy, 1);
  assert.equal(store.pages[0].loaded, false);
  assert.equal(store.pages[0].canvas, null);
  assert.deepEqual(store.pages[0].blocks, []);
  assert.deepEqual([f.calls.canvases[0].width, f.calls.canvases[0].height], [1, 1]);
  await assert.rejects(store.ensurePage(0), { name: 'AbortError' });
});

test('metadata loading retains page-dimension protections without imposing a page-count cap', async () => {
  for (const dimension of [[2000, 2000], [0, 800], [Infinity, 800], [-1, 800]]) {
    const f = fixture(2, index => index ? dimension : [600, 800]);
    await assert.rejects(createPageStore(f.doc, { engine: f.engine }), /寸法|サイズ/);
    assert.equal(f.calls.destroy, 0, 'caller owns document when construction fails');
    assert.equal(f.calls.render.length, 0);
    assert.ok(f.calls.cleanup.includes(1));
    assert.ok(f.calls.cleanup.includes(2));
  }
});

test('unknown page references are rejected instead of loading arbitrary document pages', async () => {
  const f = fixture();
  const store = await createPageStore(f.doc, { engine: f.engine });
  await assert.rejects(store.ensurePage(-1), /見つかりません/);
  await assert.rejects(store.ensurePage({ pageNumber: 1 }), /見つかりません/);
  assert.throws(() => store.releasePage(3), /見つかりません/);
  assert.equal(f.calls.render.length, 0);
  await store.dispose();
});
