import assert from 'node:assert/strict';
import { test } from 'node:test';
import path from 'node:path';
import '../dist/vendor/jszip.min.js';
import { buildDocx, buildPptx, exportOffice, getPptxGeometry, validateOfficePages, OFFICE_MIME } from '../dist/office-export.js';

const Zip = globalThis.JSZip;
const png = new Uint8Array(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64'));
const fixture = (width = 595.28, height = 841.89) => ({ width, height, png });
const xml = (zip, name) => zip.file(name).async('string');
const attrs = text => Object.fromEntries([...text.matchAll(/([\w:]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
const open = async (build, pages) => Zip.loadAsync(await build(pages, { Zip, type: 'uint8array' }));

async function checkInternalRelationships(zip) {
  // Resolving every package relationship catches missing image, master, layout,
  // properties, and theme parts without duplicating the production XML writer.
  for (const [name, file] of Object.entries(zip.files)) {
    if (file.dir || !name.endsWith('.rels')) continue;
    const sourceDir = name === '_rels/.rels' ? '' : path.posix.dirname(path.posix.dirname(name));
    const seen = new Set();
    for (const match of (await file.async('string')).matchAll(/<Relationship\s([^>]+)\/>/g)) {
      const relation = attrs(match[1]);
      assert.ok(!seen.has(relation.Id), `duplicate relationship ${name}: ${relation.Id}`);
      seen.add(relation.Id);
      assert.notEqual(relation.TargetMode, 'External');
      const target = path.posix.normalize(path.posix.join(sourceDir, relation.Target));
      assert.ok(zip.file(target), `${name} points to missing ${target}`);
    }
  }
}

test('Word sections retain portrait, landscape, fractional dimensions and page order', async () => {
  const pages = [fixture(), fixture(841.89, 595.28), fixture(420.13, 420.19)];
  const zip = await open(buildDocx, pages);
  const doc = await xml(zip, 'word/document.xml');
  const sizes = [...doc.matchAll(/<w:pgSz ([^>]+)\/>/g)].map(m => attrs(m[1]));
  assert.equal(sizes.length, pages.length);
  for (let i = 0; i < pages.length; i++) {
    assert.ok(Math.abs(Number(sizes[i]['w:w']) / 20 - pages[i].width) <= 0.02501);
    assert.ok(Math.abs(Number(sizes[i]['w:h']) / 20 - pages[i].height) <= 0.02501);
    assert.equal(sizes[i]['w:orient'], pages[i].width > pages[i].height ? 'landscape' : 'portrait');
  }
  const anchors = [...doc.matchAll(/<wp:anchor\b[\s\S]*?<\/wp:anchor>/g)].map(m => m[0]);
  assert.equal(anchors.length, pages.length);
  assert.equal((doc.match(/<w:p>/g) || []).length, pages.length, 'no empty break paragraphs');
  assert.ok(!doc.includes('<w:br'), 'only section breaks, never additional page breaks');
  assert.match(doc, /<\/w:p><w:sectPr>[\s\S]*<\/w:sectPr><\/w:body>/);
  for (let i = 0; i < anchors.length; i++) {
    assert.match(anchors[i], /<wp:positionH relativeFrom="page"><wp:posOffset>0<\/wp:posOffset>/);
    assert.match(anchors[i], /<wp:positionV relativeFrom="page"><wp:posOffset>0<\/wp:posOffset>/);
    assert.match(anchors[i], new RegExp(`<wp:extent cx="${Math.round(pages[i].width * 12700)}" cy="${Math.round(pages[i].height * 12700)}"`));
    assert.match(anchors[i], new RegExp(`r:embed="rIdImage${i + 1}"`));
    assert.match(anchors[i], /<wp:wrapNone\/>/);
  }
  await checkInternalRelationships(zip);
});

test('same-size PowerPoint slides have no margins; images retain exact physical extent', async () => {
  const pages = [fixture(), fixture()];
  const zip = await open(buildPptx, pages);
  const presentation = await xml(zip, 'ppt/presentation.xml');
  assert.match(presentation, /autoCompressPictures="0"/);
  const size = attrs(presentation.match(/<p:sldSz ([^>]+)\/>/)[1]);
  assert.equal(Number(size.cx), Math.round(pages[0].width * 12700));
  assert.equal(Number(size.cy), Math.round(pages[0].height * 12700));
  for (let i = 0; i < pages.length; i++) {
    const slide = await xml(zip, `ppt/slides/slide${i + 1}.xml`);
    const picture = slide.match(/<p:pic>[\s\S]*?<\/p:pic>/)[0];
    assert.match(picture, new RegExp(`<a:off x="0" y="0"/><a:ext cx="${size.cx}" cy="${size.cy}"`));
    assert.ok(!picture.includes('srcRect'), 'no source cropping');
  }
  await checkInternalRelationships(zip);
});

test('mixed-size PowerPoint pages are centered at original scale without clipping', async () => {
  const pages = [fixture(612, 792), fixture(792, 612), fixture(350.123, 720.75)];
  const geometry = getPptxGeometry(pages);
  assert.equal(geometry.width, 792);
  assert.equal(geometry.height, 792);
  const zip = await open(buildPptx, pages);
  for (let i = 0; i < pages.length; i++) {
    const slide = await xml(zip, `ppt/slides/slide${i + 1}.xml`);
    const transform = slide.match(/<p:spPr><a:xfrm>([\s\S]*?)<\/a:xfrm>/)[1];
    const off = attrs(transform.match(/<a:off ([^>]+)\/>/)[1]);
    const ext = attrs(transform.match(/<a:ext ([^>]+)\/>/)[1]);
    const x = Number(off.x), y = Number(off.y), w = Number(ext.cx), h = Number(ext.cy);
    assert.equal(w, Math.round(pages[i].width * 12700));
    assert.equal(h, Math.round(pages[i].height * 12700));
    assert.ok(x >= 0 && y >= 0 && x + w <= geometry.cx && y + h <= geometry.cy);
    assert.ok(Math.abs(2 * x + w - geometry.cx) <= 1);
    assert.ok(Math.abs(2 * y + h - geometry.cy) <= 1);
  }
});

for (const [format, build, directory] of [['docx', buildDocx, 'word'], ['pptx', buildPptx, 'ppt']]) {
  test(`${format}: PNG bytes are preserved for binary, blob, and canvas data URL input`, async () => {
    const pages = [fixture(), { ...fixture(), png: new Blob([png], { type: 'image/png' }) },
      { ...fixture(), png: 'data:image/png;base64,' + Buffer.from(png).toString('base64') }];
    const zip = await open(build, pages);
    for (let i = 0; i < pages.length; i++) {
      assert.deepEqual(await zip.file(`${directory}/media/page-${i + 1}.png`).async('uint8array'), png);
    }
    const blob = await build([fixture()], { Zip });
    assert.ok(blob instanceof Blob);
    assert.equal(blob.type, OFFICE_MIME[format]);
  });

  test(`${format}: rejects empty, nonfinite, unsupported size, non-PNG and missing library`, async () => {
    for (const pages of [[], [fixture(NaN, 600)], [fixture(500, Infinity)], [fixture(0, 500)], [fixture(-500, 500)], [fixture(5000, 5000)]]) {
      await assert.rejects(build(pages, { Zip }));
    }
    await assert.rejects(build([{ ...fixture(), png: new Uint8Array([1, 2, 3]) }], { Zip }), /PNG/);
    for (const data of ['data:image/png;base64,', 'data:image/png;base64,***', 'data:image/png;base64,YmFk']) {
      await assert.rejects(build([{ ...fixture(), png: data }], { Zip }), /PNG/);
    }
    await assert.rejects(build([fixture()], { Zip: null }), /ライブラリ/);
  });
}

test('supported Office boundaries and minimum dimensions do not silently rescale', () => {
  assert.doesNotThrow(() => validateOfficePages([fixture(1584, 1584)], 'docx'));
  assert.throws(() => validateOfficePages([fixture(1584.001, 1584)], 'docx'), /PDF/);
  assert.throws(() => validateOfficePages([fixture(17.99, 72)], 'docx'), /PDF/);
  assert.doesNotThrow(() => validateOfficePages([fixture(4032, 4032)], 'pptx'));
  assert.throws(() => validateOfficePages([fixture(4032.001, 4032)], 'pptx'), /PDF/);
  assert.throws(() => validateOfficePages([fixture(71.99, 72)], 'pptx'), /1 インチ/);
  assert.throws(() => validateOfficePages([fixture()], 'pdf'), /docx/);
  assert.throws(() => validateOfficePages([fixture()], 'constructor'), /docx/);
  assert.throws(() => validateOfficePages([fixture()], '__proto__'), /docx/);
});

function fakeCanvas({ fail = false } = {}) {
  return { width: 1200, height: 1600, toBlob(callback, type) {
    assert.equal(type, 'image/png');
    callback(fail ? null : new Blob([png], { type }));
  } };
}
test('export snapshots all page edits before await, keeps caller fit unchanged, and frees canvases', async () => {
  const pages = [1, 2].map(pageNumber => ({ ...fixture(), pageNumber,
    blocks: [{ id: `p${pageNumber}`, translation: 'before', fit: { shrunk: false }, sourceRect: { x: 12 } }] }));
  const originalFits = pages.map(p => p.blocks[0].fit);
  const canvases = [], progress = [], renders = [];
  const blob = await exportOffice(pages, { format: 'docx', Zip, onProgress: (...p) => progress.push(p),
    renderCanvas: async (state, options) => {
      renders.push([state.pageNumber, state.blocks[0].translation, state.blocks[0].sourceRect.x, options.scale]);
      state.blocks[0].fit = { overflow: false };
      if (state.pageNumber === 1) {
        pages[1].blocks[0].translation = 'after';
        pages[1].blocks[0].sourceRect.x = 99;
      }
      const canvas = fakeCanvas(); canvases.push(canvas); return canvas;
    } });
  assert.equal(blob.type, OFFICE_MIME.docx);
  assert.deepEqual(renders, [[1, 'before', 12, 2.25], [2, 'before', 12, 2.25]]);
  assert.deepEqual(progress, [[1, 2], [2, 2]]);
  for (let i = 0; i < pages.length; i++) assert.equal(pages[i].blocks[0].fit, originalFits[i]);
  for (const canvas of canvases) assert.deepEqual([canvas.width, canvas.height], [1, 1]);
});

test('both Office formats block actual render overflow and free the failed canvas', async () => {
  for (const format of ['docx', 'pptx']) {
    const canvas = fakeCanvas();
    const block = { id: 'p1-b1', translation: 'Too much text', fit: null };
    await assert.rejects(exportOffice([{ ...fixture(), pageNumber: 1, blocks: [block] }], {
      format, Zip, renderCanvas: async page => { page.blocks[0].fit = { overflow: true }; return canvas; },
    }), error => error.code === 'TEXT_OVERFLOW' && error.blockIds[0] === block.id);
    assert.equal(block.fit, null);
    assert.deepEqual([canvas.width, canvas.height], [1, 1]);
  }
});

test('PNG conversion failures release the temporary canvas and do not generate an Office download', async () => {
  const canvas = fakeCanvas({ fail: true });
  await assert.rejects(exportOffice([{ ...fixture(), blocks: [] }], {
    format: 'pptx', Zip, renderCanvas: async () => canvas,
  }), /ページ画像/);
  assert.deepEqual([canvas.width, canvas.height], [1, 1]);
});

test('preflight rejects all invalid geometry and unavailable libraries before rendering', async () => {
  let renders = 0;
  const renderCanvas = async () => { renders++; return fakeCanvas(); };
  await assert.rejects(exportOffice([fixture(), fixture(9999, 9999)], { format: 'docx', Zip, renderCanvas }));
  await assert.rejects(exportOffice([fixture()], { format: 'docx', Zip: null, renderCanvas }));
  assert.equal(renders, 0);
});

test('lazy Office exports prepare unread text and release each page before the next page', async () => {
  for (const format of ['docx', 'pptx']) {
    const pages = Array.from({ length: 65 }, (_, i) => ({ ...fixture(), pageNumber: i + 1, loaded: false, blocks: [] }));
    const prepared = [], released = [], canvases = [], progress = [];
    let resident = 0;
    const blob = await exportOffice(pages, { format, Zip,
      preparePage: async page => {
        assert.equal(resident, 0, 'previous PDF page is no longer resident');
        resident++;
        const result = { ...page, loaded: true, blocks: [{ translation: `Translated ${page.pageNumber}`, fit: null }] };
        prepared.push(result);
        return result;
      },
      renderCanvas: async page => {
        assert.equal(page.blocks[0].translation, `Translated ${page.pageNumber}`);
        page.blocks[0].fit = { overflow: false };
        const canvas = fakeCanvas(); canvases.push(canvas); return canvas;
      },
      releasePage: async page => {
        assert.equal(page, prepared.at(-1), 'release receives the acquired page');
        assert.deepEqual([canvases.at(-1).width, canvases.at(-1).height], [1, 1]);
        assert.equal(page.blocks[0].fit, null, 'rendering only modifies the text snapshot');
        resident--;
        released.push(page.pageNumber);
      },
      onProgress: (...value) => progress.push(value),
    });
    const zip = await Zip.loadAsync(await blob.arrayBuffer());
    const media = Object.keys(zip.files).filter(name => /\/media\/page-\d+\.png$/.test(name));
    assert.equal(media.length, 65);
    assert.equal(resident, 0);
    assert.deepEqual(released, pages.map(page => page.pageNumber));
    assert.deepEqual(progress, pages.map(page => [page.pageNumber, 65]));
  }
});

test('lazy Office exports release partially prepared and failed pages without hiding the original failure', async () => {
  for (const stage of ['prepare', 'render', 'overflow', 'encode']) {
    const source = { ...fixture(), pageNumber: 1, loaded: false, blocks: [] };
    const prepared = { ...source, blocks: [{ id: 'lazy-overflow', translation: 'Translated', fit: null }] };
    const error = new Error(`Failed at ${stage}`), releases = [];
    let canvas;
    await assert.rejects(exportOffice([source], { format: 'docx', Zip,
      preparePage: async () => { if (stage === 'prepare') throw error; return prepared; },
      renderCanvas: async page => {
        if (stage === 'render') throw error;
        page.blocks[0].fit = { overflow: stage === 'overflow' };
        canvas = fakeCanvas({ fail: stage === 'encode' }); return canvas;
      },
      releasePage: async page => { releases.push(page); throw new Error('Cleanup failed'); },
    }), failure => stage === 'overflow' ? failure.code === 'TEXT_OVERFLOW'
      : stage === 'encode' ? /ページ画像/.test(failure.message) : failure === error);
    assert.deepEqual(releases, [stage === 'prepare' ? source : prepared]);
    if (canvas) assert.deepEqual([canvas.width, canvas.height], [1, 1]);
  }
});
