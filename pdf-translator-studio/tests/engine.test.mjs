import assert from 'node:assert/strict';
import { test } from 'node:test';
import { textItemsToBlocks, mergeTextRuns, wrapText, fitText, exportPdf, renderPage, renderTranslatedCanvas } from '../dist/pdf-engine.js';

const viewport = { transform: [1, 0, 0, -1, 0, 800], scale: 1, width: 600, height: 800 };
const styles = { f1: { ascent: 0.8, descent: -0.2, fontFamily: 'sans-serif' } };
const item = (str, x, y, width = 40, extras = {}) => ({
  str, transform: [12, 0, 0, 12, x, y], width, height: 12, fontName: 'f1', dir: 'ltr', ...extras,
});

test('PDF baseline coordinates become top-left viewport geometry', () => {
  const [block] = textItemsToBlocks([item('Hello', 40, 700, 60)], styles, viewport);
  assert.equal(block.x, 40);
  assert.ok(Math.abs(block.y - 90.4) < 1e-9);
  assert.equal(block.width, 60);
  assert.equal(block.height, 12);
  assert.equal(block.baseline, 100);
  assert.equal(block.fontSize, 12);
  assert.equal(block.supported, true);
  assert.deepEqual(block.sourceRect, { x: block.x, y: block.y, width: block.width, height: block.height });
});

test('scaled viewports still produce page-point coordinates', () => {
  const [block] = textItemsToBlocks([item('Hello', 40, 700, 60)], styles,
    { transform: [2, 0, 0, -2, 0, 1600], scale: 2, width: 1200, height: 1600 });
  assert.equal(block.x, 40);
  assert.equal(block.width, 60);
  assert.equal(block.fontSize, 12);
  assert.equal(block.baseline, 100);
});

test('PDF UserUnit scales text width as well as position and font size', () => {
  const [block] = textItemsToBlocks([item('Hello', 40, 700, 60)], styles,
    { transform: [2, 0, 0, -2, 0, 1600], scale: 1, userUnit: 2, width: 1200, height: 1600 });
  assert.equal(block.x, 80);
  assert.equal(block.width, 120);
  assert.equal(block.fontSize, 24);
  assert.equal(block.baseline, 200);
});

test('rotated, vertical, and skewed text are excluded from automatic replacement', () => {
  const rotated = item('Rotated', 40, 700, 60, { transform: [0, 12, -12, 0, 40, 700] });
  const vertical = item('縦書き', 100, 700, 36, { dir: 'ttb' });
  const skewed = item('Skew', 150, 700, 50, { transform: [12, 0, 4, 12, 150, 700] });
  for (const block of textItemsToBlocks([rotated, vertical, skewed], styles, viewport)) {
    assert.equal(block.supported, false);
    assert.equal(block.enabled, false);
    assert.ok(block.warning);
  }
});

test('nearby fragments merge, separate columns and explicit line ends stay separate', () => {
  const blocks = textItemsToBlocks([
    item('Hello', 40, 700, 30), item('world', 73, 700, 35, { hasEOL: true }),
    item('Table cell', 300, 700, 80), item('Next line', 40, 680, 80),
  ], styles, viewport);
  const merged = mergeTextRuns(blocks);
  assert.equal(merged.length, 3);
  assert.equal(merged[0].original, 'Hello world');
  assert.equal(merged[0].width, 68);
  assert.equal(merged[0].sourceRect.width, 68);
  assert.equal(merged[1].original, 'Table cell');
  assert.equal(blocks[0].original, 'Hello', 'input blocks remain unchanged');
});

test('different text colors remain separate even with touching geometry', () => {
  const blocks = textItemsToBlocks([item('RED', 40, 700, 30), item('BLUE', 70, 700, 30)], styles, viewport);
  blocks[0].color = '#ff0000'; blocks[1].color = '#0000ff';
  assert.equal(mergeTextRuns(blocks).length, 2);
});

test('wrapping respects spaces, explicit newlines and grapheme clusters', () => {
  const measure = str => [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(str)].length;
  assert.deepEqual(wrapText('alpha beta gamma', 10, measure), ['alpha beta', 'gamma']);
  assert.deepEqual(wrapText('a\n\nb', 10, measure), ['a', '', 'b']);
  const emojiLines = wrapText('👨‍👩‍👧‍👦👨‍👩‍👧‍👦', 1, measure);
  assert.deepEqual(emojiLines, ['👨‍👩‍👧‍👦', '👨‍👩‍👧‍👦']);
  const japanese = wrapText('文書の配置を保つ', 4, measure);
  assert.equal(japanese.join(''), '文書の配置を保つ');
  assert.ok(japanese.every(line => measure(line) <= 4));
});

function measuringContext() {
  return {
    font: '12px sans-serif',
    measureText(value) {
      const size = Number(this.font.match(/([\d.]+)px/)[1]);
      return { width: Array.from(value).length * size * 0.5,
        actualBoundingBoxAscent: size * 0.8, actualBoundingBoxDescent: size * 0.2 };
    },
    save() {}, restore() {}, scale() {}, drawImage() {}, fillRect() {},
    beginPath() {}, rect() {}, clip() {}, fillText() {},
  };
}

test('fit uses the largest feasible size and reports unsatisfied overflow', () => {
  const context = measuringContext();
  const base = { width: 120, height: 15, fontSize: 12 };
  const short = fitText(context, 'Short label', base);
  assert.equal(short.fontSize, 12);
  assert.equal(short.overflow, false);
  const longer = fitText(context, 'A translated label with extra words', base);
  assert.ok(longer.fontSize < 12);
  assert.equal(longer.overflow, false);
  const impossible = fitText(context, 'Very long '.repeat(100), { width: 20, height: 10, fontSize: 12 });
  assert.equal(impossible.overflow, true);
  assert.equal(impossible.fontSize, 5.4);
});

test('render canvas allocation stays bounded for oversized PDF pages', async () => {
  const oldDocument = globalThis.document;
  globalThis.document = {
    createElement() { return { width: 0, height: 0, getContext: () => measuringContext() }; },
  };
  const page = {
    getViewport: ({ scale }) => ({ width: 10000 * scale, height: 10000 * scale, scale }),
    render: () => ({ promise: Promise.resolve() }),
  };
  try {
    const state = await renderPage({ getPage: async () => page }, 1, 4);
    assert.ok(state.canvas.width * state.canvas.height < 12_010_000, 'RGBA allocation stays near 48 MB');
    assert.ok(state.canvas.width <= 16384 && state.canvas.height <= 16384);
    assert.equal(state.width, 10000, 'physical page dimensions are not changed');
    page.getViewport = () => ({ width: Infinity, height: 1000 });
    await assert.rejects(renderPage({ getPage: async () => page }, 1), /ページサイズ/);
  } finally { globalThis.document = oldDocument; }
});

test('PDF export preserves mixed page dimensions and rejects clipped translations', async () => {
  const dimensions = [];
  const allocated = [];
  const oldDocument = globalThis.document;
  const oldJspdf = globalThis.jspdf;
  globalThis.document = {
    createElement() {
      const canvas = { width: 0, height: 0, getContext: () => measuringContext() };
      allocated.push(canvas);
      return canvas;
    },
    fonts: { ready: Promise.resolve() },
  };
  globalThis.jspdf = { jsPDF: class {
    constructor(options) { dimensions.push(options.format); }
    setProperties() {}
    addPage(size) { dimensions.push(size); }
    addImage() {}
    output() { return new Blob(['test'], { type: 'application/pdf' }); }
  } };
  try {
    const progress = [];
    const pages = [
      { pageNumber: 1, width: 600, height: 800, canvas: {}, blocks: [] },
      { pageNumber: 2, width: 800, height: 600, canvas: {}, blocks: [] },
    ];
    const blob = await exportPdf(pages, { onProgress: (n, total) => progress.push([n, total]) });
    assert.equal(blob.type, 'application/pdf');
    assert.deepEqual(dimensions, [[600, 800], [800, 600]]);
    assert.deepEqual(progress, [[1, 2], [2, 2]]);
    pages[0].blocks = [{ id: 'overflow', translation: 'Text '.repeat(100),
      x: 10, y: 10, width: 10, height: 5, fontSize: 12, enabled: true, supported: true }];
    await assert.rejects(exportPdf(pages), error => error.code === 'TEXT_OVERFLOW' && error.blockIds[0] === 'overflow');
    assert.ok(allocated.every(canvas => canvas.width === 1 && canvas.height === 1), 'successful and overflow exports release page buffers');
    pages[0].blocks = [];
    globalThis.jspdf.jsPDF.prototype.addImage = () => { throw new Error('Image encoding failed'); };
    await assert.rejects(exportPdf(pages), /Image encoding failed/);
    assert.ok(allocated.every(canvas => canvas.width === 1 && canvas.height === 1), 'encoding failure also releases its page buffer');
  } finally { globalThis.document = oldDocument; globalThis.jspdf = oldJspdf; }
});

test('failed source rendering releases the export canvas and propagates the original error', async () => {
  const oldDocument = globalThis.document;
  const canvas = { width: 0, height: 0, getContext: () => measuringContext() };
  const failure = new Error('Source PDF rendering failed');
  globalThis.document = { createElement: () => canvas };
  try {
    await assert.rejects(renderTranslatedCanvas({ width: 600, height: 800, blocks: [], page: {
      getViewport: ({ scale }) => ({ width: 600 * scale, height: 800 * scale }),
      render: () => ({ promise: Promise.reject(failure) }),
    } }), error => error === failure);
    assert.equal(canvas.width, 1);
    assert.equal(canvas.height, 1);
  } finally { globalThis.document = oldDocument; }
});

test('failed preview rendering frees its canvas before a page store can take ownership', async () => {
  const oldDocument = globalThis.document;
  const failure = new Error('Preview render failed');
  const canvas = { width: 0, height: 0, getContext: () => measuringContext() };
  globalThis.document = { createElement: () => canvas };
  try {
    await assert.rejects(renderPage({ getPage: async () => ({
      getViewport: () => ({ width: 600, height: 800 }),
      render: () => ({ promise: Promise.reject(failure) }),
    }) }, 1), error => error === failure);
    assert.deepEqual([canvas.width, canvas.height], [1, 1]);
  } finally { globalThis.document = oldDocument; }
});

test('lazy PDF export prepares and releases pages sequentially using their actual dimensions', async () => {
  const oldDocument = globalThis.document, oldJspdf = globalThis.jspdf;
  const allocated = [], dimensions = [], releases = [], progress = [];
  let resident = 0, images = 0;
  globalThis.document = {
    createElement() {
      const canvas = { width: 0, height: 0, getContext: () => measuringContext() };
      allocated.push(canvas); return canvas;
    },
  };
  globalThis.jspdf = { jsPDF: class {
    constructor(options) { dimensions.push(options.format); }
    setProperties() {}
    addPage(size) { dimensions.push(size); }
    addImage() { images++; }
    output() { return new Blob(['test'], { type: 'application/pdf' }); }
  } };
  try {
    const pages = Array.from({ length: 65 }, (_, i) => ({ pageNumber: i + 1, width: 1, height: 1, loaded: false, blocks: [] }));
    await exportPdf(pages, {
      preparePage: async page => {
        assert.equal(resident, 0); resident++;
        return { ...page, width: 400 + page.pageNumber, height: 600, canvas: {},
          blocks: [{ translation: 'Text', x: 10, y: 10, width: 100, height: 30, fontSize: 12 }] };
      },
      releasePage: async page => {
        assert.equal(page.width, 400 + page.pageNumber);
        assert.equal(page.blocks[0].fit.overflow, false, 'unread page text was rendered');
        assert.deepEqual([allocated.at(-1).width, allocated.at(-1).height], [1, 1]);
        resident--; releases.push(page.pageNumber);
      },
      onProgress: (...value) => progress.push(value),
    });
    assert.equal(images, 65);
    assert.equal(resident, 0);
    assert.deepEqual(dimensions, pages.map(page => [400 + page.pageNumber, 600]));
    assert.deepEqual(releases, pages.map(page => page.pageNumber));
    assert.deepEqual(progress, pages.map(page => [page.pageNumber, 65]));
  } finally { globalThis.document = oldDocument; globalThis.jspdf = oldJspdf; }
});

test('lazy PDF export releases prepared pages on preparation, render, overflow and encoding failures', async () => {
  const oldDocument = globalThis.document, oldJspdf = globalThis.jspdf;
  try {
    for (const stage of ['prepare', 'render', 'overflow', 'encode']) {
      const failure = new Error(`Failed at ${stage}`), allocated = [], releases = [];
      globalThis.document = { createElement() {
        const canvas = { width: 0, height: 0, getContext: () => measuringContext() };
        allocated.push(canvas); return canvas;
      } };
      globalThis.jspdf = { jsPDF: class {
        setProperties() {}
        addImage() { if (stage === 'encode') throw failure; }
        output() { throw new Error('Failed export must not produce a download'); }
      } };
      const source = { pageNumber: 1, width: 600, height: 800, loaded: false, blocks: [] };
      const prepared = { ...source, canvas: {}, blocks: stage === 'overflow'
        ? [{ id: 'lazy-overflow', translation: 'Text '.repeat(100), x: 10, y: 10, width: 10, height: 5, fontSize: 12 }] : [] };
      if (stage === 'render') prepared.page = {
        getViewport: () => ({}), render: () => ({ promise: Promise.reject(failure) }),
      };
      await assert.rejects(exportPdf([source], {
        preparePage: async () => { if (stage === 'prepare') throw failure; return prepared; },
        releasePage: async page => { releases.push(page); throw new Error('Cleanup failed'); },
      }), error => stage === 'overflow' ? error.code === 'TEXT_OVERFLOW' : error === failure);
      assert.deepEqual(releases, [stage === 'prepare' ? source : prepared]);
      assert.ok(allocated.every(canvas => canvas.width === 1 && canvas.height === 1));
    }
  } finally { globalThis.document = oldDocument; globalThis.jspdf = oldJspdf; }
});
