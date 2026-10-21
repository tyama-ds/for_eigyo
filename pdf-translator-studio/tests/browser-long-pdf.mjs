/**
 * Long-document UI regression QA. Runs on its own ephemeral local server.
 * Translation uses a deterministic local test double; never calls an LLM.
 * Optional LONG_PDF_SOURCE exercises the real 269-page annual report as well.
 * Artifacts and measured results are written to tmp/long-pdf-qa by default.
 */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createStaticServer } from '../server.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = process.env.LONG_PDF_QA_OUTPUT || path.join(root, 'tmp', 'long-pdf-qa');
const modules = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules');
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || path.join(modules, 'playwright/index.mjs')).href);
const { default: JSZip } = await import(pathToFileURL(process.env.JSZIP_MODULE || path.join(modules, 'jszip/lib/index.js')).href);
const report = { completed: [], documents: [], pageErrors: [], externalRequests: [], mockApiCalls: [], canvasSamples: [], realApiCalls: 0 };
const fixtureCount = 41;
const exportFormats = (process.env.LONG_PDF_EXPORT_FORMATS || 'pdf,docx,pptx').split(',');
assert.ok(exportFormats.every(format => ['pdf', 'docx', 'pptx'].includes(format)));
const providers = {
  getPublicConfig: () => ({ local: { baseUrl: 'http://qa.invalid/v1', model: 'qa-local-model', configured: true }, openai: { baseUrl: 'https://qa.invalid/v1', model: 'gpt-4o', configured: false }, proxy: { configured: false } }),
  translateBatch: payload => {
    report.mockApiCalls.push(payload);
    return { translations: payload.blocks.map(block => ({ id: block.id, text: `訳 ${block.id.match(/^p(\d+)/)?.[1] || ''}` })) };
  },
};
await mkdir(output, { recursive: true });
const server = createStaticServer({ providers });
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
const startTime = Date.now();
const closeEnough = (actual, expected, tolerance, label) => assert.ok(Math.abs(actual - expected) <= tolerance, `${label}: ${actual} != ${expected}`);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
async function inspectPdf(filename, sizes) {
  // Parsing large exported image PDFs in the app renderer would add a second
  // full document to its memory use. Keep validation in an independent process.
  const python = process.env.PYTHON_BINARY || path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe');
  const script = `import json,sys\nfrom pypdf import PdfReader\np=PdfReader(sys.argv[1])\nsizes=json.loads(sys.argv[2])\nassert len(p.pages)==len(sizes)\nmaximum=0\nfor page,expected in zip(p.pages,sizes):\n maximum=max(maximum,abs(float(page.mediabox.width)-expected['width']),abs(float(page.mediabox.height)-expected['height']))\n count=sum(op==b'Do' and page['/Resources']['/XObject'][args[0]].get_object().get('/Subtype')=='/Image' for args,op in page.get_contents().operations)\n assert count==1\nassert maximum<0.01\nprint(json.dumps({'pages':len(p.pages),'images':len(p.pages),'maxPageSizeErrorPt':maximum,'pageSizes':'pass'}))`;
  const { stdout } = await promisify(execFile)(python, ['-c', script, filename, JSON.stringify(sizes)], { maxBuffer: 4 * 1024 * 1024 });
  return JSON.parse(stdout);
}

async function inspectOffice(filename, format, sizes) {
  const zip = await JSZip.loadAsync(await readFile(filename), { checkCRC32: true });
  const names = Object.keys(zip.files);
  const images = names.filter(name => new RegExp(`^${format === 'docx' ? 'word' : 'ppt'}/media/.*\\.png$`).test(name));
  assert.equal(images.length, sizes.length, `${format}: one image for every source page`);
  for (const name of names.filter(name => name.endsWith('.rels'))) assert.ok(!/TargetMode="External"/.test(await zip.file(name).async('string')));
  if (format === 'docx') {
    const xml = await zip.file('word/document.xml').async('string');
    const pageSizes = [...xml.matchAll(/<w:pgSz\b[^>]*w:w="(\d+)"[^>]*w:h="(\d+)"/g)];
    const extents = [...xml.matchAll(/<wp:extent\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/g)];
    assert.equal(pageSizes.length, sizes.length);
    assert.equal(extents.length, sizes.length);
    sizes.forEach((size, index) => {
      closeEnough(+pageSizes[index][1] / 20, size.width, .05, 'Word page width');
      closeEnough(+pageSizes[index][2] / 20, size.height, .05, 'Word page height');
      closeEnough(+extents[index][1] / 12700, size.width, .001, 'Word image width');
      closeEnough(+extents[index][2] / 12700, size.height, .001, 'Word image height');
    });
    return { pageSections: pageSizes.length, images: images.length, pageAndImageSizes: 'pass', archiveCRC: 'pass' };
  }
  const xml = await zip.file('ppt/presentation.xml').async('string');
  const slideSize = xml.match(/<p:sldSz\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
  const maxWidth = Math.max(...sizes.map(size => size.width)), maxHeight = Math.max(...sizes.map(size => size.height));
  closeEnough(+slideSize[1] / 12700, maxWidth, .001, 'PowerPoint slide width');
  closeEnough(+slideSize[2] / 12700, maxHeight, .001, 'PowerPoint slide height');
  const slides = names.filter(name => /^ppt\/slides\/slide\d+\.xml$/.test(name)).sort((a, b) => +a.match(/slide(\d+)/)[1] - +b.match(/slide(\d+)/)[1]);
  assert.equal(slides.length, sizes.length);
  for (let index = 0; index < slides.length; index++) {
    const slide = await zip.file(slides[index]).async('string');
    assert.equal((slide.match(/<p:pic\b/g) || []).length, 1);
    const picture = slide.match(/<p:pic\b[\s\S]*?<\/p:pic>/)[0];
    const extent = picture.match(/<a:ext\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/), offset = picture.match(/<a:off\b[^>]*x="(\d+)"[^>]*y="(\d+)"/);
    closeEnough(+extent[1] / 12700, sizes[index].width, .001, 'PowerPoint image width');
    closeEnough(+extent[2] / 12700, sizes[index].height, .001, 'PowerPoint image height');
    closeEnough(+offset[1] / 12700, (maxWidth - sizes[index].width) / 2, .001, 'PowerPoint image x');
    closeEnough(+offset[2] / 12700, (maxHeight - sizes[index].height) / 2, .001, 'PowerPoint image y');
  }
  return { slides: slides.length, images: images.length, pageAndImageSizes: 'pass', archiveCRC: 'pass' };
}

try {
  browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1660, height: 1120 }, acceptDownloads: true });
  await context.addInitScript(() => {
    const refs = [];
    const original = Document.prototype.createElement;
    Document.prototype.createElement = function (...args) {
      const element = original.apply(this, args);
      if (element instanceof HTMLCanvasElement) refs.push(new WeakRef(element));
      return element;
    };
    globalThis.__qaCanvasStats = () => {
      const canvases = refs.map(ref => ref.deref()).filter(canvas => canvas && canvas.width > 64 && canvas.height > 64);
      return { retainedLargeCanvases: canvases.length, retainedPixels: canvases.reduce((sum, canvas) => sum + canvas.width * canvas.height, 0), dimensions: canvases.map(canvas => [canvas.width, canvas.height]) };
    };
  });
  await context.route('**/*', async route => {
    const url = route.request().url();
    if (url.startsWith(origin + '/__qa_artifact/')) {
      const name = decodeURIComponent(new URL(url).pathname.slice('/__qa_artifact/'.length));
      assert.equal(path.basename(name), name);
      return route.fulfill({ body: await readFile(path.join(output, name)), contentType: name.endsWith('.pdf') ? 'application/pdf' : 'application/octet-stream' });
    }
    if (url.startsWith(origin + '/') || /^(blob:|data:)/.test(url)) return route.continue();
    report.externalRequests.push(url); return route.abort();
  });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  page.on('pageerror', error => report.pageErrors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  page.setDefaultTimeout(120000);
  const idle = () => page.waitForFunction(() => !document.body.classList.contains('busy'));
  async function preview() {
    await idle();
    await page.waitForFunction(() => document.querySelector('#original-paper canvas')?.width > 1 && document.querySelector('#translated-paper canvas')?.width > 1);
  }
  async function selectPage(number) {
    await page.locator('#page-list button').nth(number - 1).click();
    await page.waitForFunction(number => document.querySelector('#page-list button.active')?.textContent.trim() === String(number).padStart(2, '0'), number);
    await preview();
  }
  async function canvasSample(label) {
    await cdp.send('HeapProfiler.collectGarbage');
    const stats = await page.evaluate(() => globalThis.__qaCanvasStats());
    report.canvasSamples.push({ label, ...stats });
    assert.ok(stats.retainedLargeCanvases <= 8, `${label}: unbounded canvas retention (${stats.retainedLargeCanvases})`);
    return stats;
  }
  async function download(selector, name, timeout = 120000) {
    const pending = page.waitForEvent('download', { timeout });
    await page.locator(selector).click();
    let rejectFailure;
    const failed = new Promise((_, reject) => { rejectFailure = reject; });
    const heartbeat = setInterval(async () => {
      try {
        const state = await page.evaluate(() => ({ busy: document.body.classList.contains('busy'), error: !document.querySelector('#toast').hidden && document.querySelector('#toast').classList.contains('error'), message: document.querySelector('#toast').textContent, status: document.querySelector('#status').textContent }));
        if (!state.busy && state.error) rejectFailure(new Error(`UI download failed: ${state.message}`));
        else console.log(`${name}: ${state.status}`);
      } catch (error) { rejectFailure(error); }
    }, 15000);
    let item;
    try { item = await Promise.race([pending, failed]); }
    finally { clearInterval(heartbeat); }
    assert.equal(await item.failure(), null);
    const filename = path.join(output, name);
    await item.saveAs(filename); await idle();
    return filename;
  }
  async function saveProject(name) { return JSON.parse(await readFile(await download('#save-project', name), 'utf8')); }
  async function openSource(filename, expectedCount) {
    await page.locator('#file-input').setInputFiles(filename);
    await page.waitForFunction(name => document.querySelector('#file-input').value === '' && document.querySelector('#filename').textContent === name, path.basename(filename));
    await preview();
    assert.equal(await page.locator('#page-total').textContent(), String(expectedCount));
    assert.equal(await page.locator('#page-list button').count(), expectedCount);
  }
  async function editFirst(text) {
    await page.locator('#original-paper .text-box:not(.unsupported)').first().click();
    const id = await page.locator('#original-paper .text-box.selected').getAttribute('data-block');
    await page.locator('#translation-text').fill(text);
    await page.waitForFunction(() => document.querySelector('#block-status').textContent === '訳文あり');
    return id;
  }
  async function expectBlock(id, text) {
    await page.locator(`#original-paper .text-box[data-block="${id}"]`).click();
    assert.equal(await page.locator('#translation-text').inputValue(), text);
  }
  async function pdfSizes(filename, requirePageImages = false) {
    return page.evaluate(async ({ filename, requirePageImages }) => {
      const engine = await import('/pdf-engine.js');
      const { OPS } = await import('/vendor/pdf.mjs');
      const doc = await engine.loadPdf(await (await fetch('/__qa_artifact/' + encodeURIComponent(filename))).arrayBuffer());
      const sizes = [];
      for (let n = 1; n <= doc.numPages; n++) {
        const p = await doc.getPage(n), view = p.getViewport({ scale: 1 });
        if (requirePageImages) {
          const ops = await p.getOperatorList();
          const imageCount = ops.fnArray.filter(op => op === OPS.paintImageXObject || op === OPS.paintInlineImageXObject).length;
          if (imageCount !== 1) throw Error(`Exported PDF page ${n} has ${imageCount} images instead of one`);
        }
        sizes.push({ width: view.width, height: view.height }); p.cleanup();
      }
      await doc.loadingTask.destroy(); return sizes;
    }, { filename, requirePageImages });
  }
  async function exportAll(prefix, sizes) {
    const outputs = [];
    for (const format of exportFormats) {
      const started = Date.now();
      console.log(`Export ${prefix}: ${format} (${sizes.length} pages)`);
      await page.locator('#export-format').selectOption(format);
      const filename = await download('#export-button', `${prefix}.${format}`, 1200000);
      let validation;
      if (format === 'pdf') validation = await inspectPdf(filename, sizes);
      else validation = await inspectOffice(filename, format, sizes);
      const bytes = await readFile(filename);
      outputs.push({ format, filename: path.basename(filename), bytes: bytes.length, sha256: hash(bytes), seconds: (Date.now() - started) / 1000, validation });
      await canvasSample(`${prefix} after ${format}`);
    }
    return outputs;
  }

  await page.goto(origin, { waitUntil: 'networkidle' });
  if (!process.env.LONG_PDF_REAL_ONLY) {
  const fixture = await page.evaluate(count => {
    const sizes = Array.from({ length: count }, (_, i) => i % 2 ? [480, 360] : [360, 480]);
    const pdf = new jspdf.jsPDF({ unit: 'pt', format: sizes[0], compress: true });
    sizes.forEach(([width, height], index) => {
      if (index) pdf.addPage([width, height], width > height ? 'landscape' : 'portrait');
      pdf.setFillColor(20 + index * 5, 90, 140); pdf.rect(0, height - 20, width, 20, 'F');
      pdf.setFont('helvetica', 'normal'); pdf.setFontSize(18); pdf.text(`Page ${index + 1} original text`, 25, 50);
    });
    return pdf.output('datauristring').split(',')[1];
  }, fixtureCount);
  const fixtureFile = path.join(output, 'long-41-source.pdf');
  await writeFile(fixtureFile, Buffer.from(fixture, 'base64'));
  const sizes = await pdfSizes(path.basename(fixtureFile));
  await openSource(fixtureFile, fixtureCount);
  await canvasSample('41 pages initial load');
  const firstId = await editFirst('先頭の編集');
  await selectPage(21); const middleId = await editFirst('中央の編集');
  await selectPage(41); const lastId = await editFirst('末尾の編集');
  await selectPage(1); await expectBlock(firstId, '先頭の編集');
  await selectPage(21); await expectBlock(middleId, '中央の編集');
  await selectPage(41); await expectBlock(lastId, '末尾の編集');
  await canvasSample('41 pages repeated navigation');
  const partial = await saveProject('long-41-partial.lingua.json');
  assert.equal(partial.pageCount, fixtureCount);
  assert.deepEqual(partial.pages.map(p => p.pageNumber), [1, 21, 41], 'save omits unread pages without pretending they have zero text regions');
  assert.equal(partial.pages.find(p => p.pageNumber === 1).blocks.find(b => b.id === firstId).translation, '先頭の編集');
  await openSource(fixtureFile, fixtureCount);
  await page.locator('#project-input').setInputFiles(path.join(output, 'long-41-partial.lingua.json'));
  await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('復元しました'));
  await idle(); await selectPage(41); await expectBlock(lastId, '末尾の編集');
  await selectPage(21); await expectBlock(middleId, '中央の編集');
  await selectPage(2);
  assert.equal(await page.locator('#original-paper .text-box').count(), 1, 'previously unread page still extracts its original text');
  await page.locator('#original-paper .text-box').first().click();
  assert.equal(await page.locator('#original-text').inputValue(), 'Page 2 original text');
  assert.equal(await page.locator('#translation-text').inputValue(), '');
  report.completed.push('41 pages: lazy navigation, editing persistence, partial JSON roundtrip, unread page extraction');

  await page.locator('#scope').selectOption('all');
  await page.locator('#skip-translated').uncheck(); // This case deliberately replaces all three manual edits.
  await page.locator('#translate-button').click();
  await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('翻訳が完了しました'), null, { timeout: 180000 });
  await idle();
  const translatedIds = report.mockApiCalls.flatMap(call => call.blocks.map(block => block.id));
  assert.equal(translatedIds.length, fixtureCount);
  assert.deepEqual([...new Set(translatedIds.map(id => +id.match(/^p(\d+)/)[1]))].sort((a, b) => a - b), Array.from({ length: fixtureCount }, (_, i) => i + 1));
  const translated = await saveProject('long-41-translated.lingua.json');
  assert.equal(translated.pages.length, fixtureCount);
  assert.ok(translated.pages.every(p => p.blocks.length === 1 && p.blocks[0].translation === `訳 ${p.pageNumber}`));
  await canvasSample('41 pages after all-page translation');
  await page.locator('#toast').waitFor({ state: 'hidden' });
  await page.screenshot({ path: path.join(output, 'long-41-translated-gui.png'), fullPage: true });
  report.completed.push('all-page translation includes all 41 pages, including pages never opened; deterministic test double only');
  report.documents.push({ name: '41-page mixed-size fixture', source: path.basename(fixtureFile), pageCount: fixtureCount, outputs: await exportAll('long-41-translated', sizes) });
  }

  const realSource = process.env.LONG_PDF_SOURCE;
  if (realSource) {
    const filename = path.join(output, 'long-real-source.pdf');
    await writeFile(filename, await readFile(realSource));
    const realSizes = await pdfSizes(path.basename(filename));
    const beforeApi = report.mockApiCalls.length;
    await openSource(filename, realSizes.length);
    await canvasSample(`${realSizes.length} pages initial load`);
    await selectPage(Math.ceil(realSizes.length / 2));
    await selectPage(realSizes.length);
    await page.locator('#toast').waitFor({ state: 'hidden' });
    await page.screenshot({ path: path.join(output, 'long-real-last-page-gui.png'), fullPage: true });
    await selectPage(1); const id = await editFirst('検証');
    await selectPage(realSizes.length); await selectPage(1); await expectBlock(id, '検証');
    await canvasSample(`${realSizes.length} pages repeated navigation`);
    await page.screenshot({ path: path.join(output, 'long-real-first-page-gui.png'), fullPage: true });
    await saveProject('long-real-partial.lingua.json');
    report.documents.push({ name: 'real annual report; manual QA edit only, not translated', source: realSource, pageCount: realSizes.length, outputs: await exportAll('long-real-qa', realSizes) });
    assert.equal(report.mockApiCalls.length, beforeApi, 'real public annual report never sent to any translation service');
    report.completed.push(`${realSizes.length} pages: original document opened without splitting, navigation/edit persistence, actual ${exportFormats.map(format => format.toUpperCase()).join('/')} UI downloads`);
  }
  assert.deepEqual(report.pageErrors, []);
  assert.deepEqual(report.externalRequests, []);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.failure = { message: error.message, stack: error.stack };
  console.error(error); process.exitCode = 1;
} finally {
  report.seconds = (Date.now() - startTime) / 1000;
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
  console.log(JSON.stringify({ status: report.status, completed: report.completed, documents: report.documents, pageErrors: report.pageErrors, output }, null, 2));
}
