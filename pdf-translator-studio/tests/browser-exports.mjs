/**
 * Repeated real-browser export QA. Requires installed Playwright and Edge/Chrome.
 * Builds selectable-text PDF inputs, translates their extracted regions locally,
 * and checks 3 repeated PDF/DOCX/PPTX exports per fixture. No LLM API is called.
 * The generated fixtures are also inputs to independent Office renderer QA.
 */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createStaticServer } from '../server.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = process.env.EXPORT_QA_OUTPUT || path.join(root, 'tmp', 'office-qa', 'fixtures');
const runtimeModules = path.join(os.homedir(), '.cache', 'codex-runtimes', 'codex-primary-runtime', 'dependencies', 'node', 'node_modules');
const modulePath = process.env.PLAYWRIGHT_MODULE || path.join(runtimeModules, 'playwright', 'index.mjs');
const { chromium } = await import(pathToFileURL(modulePath).href);
const { default: JSZip } = await import(pathToFileURL(process.env.JSZIP_MODULE || path.join(runtimeModules, 'jszip', 'lib', 'index.js')).href);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const formats = ['pdf', 'docx', 'pptx'];
const repetitions = 3;
const report = { repetitions, fixtureCases: [], uiDownloads: [], pageErrors: [], externalRequests: [], translationRequests: 0 };
await mkdir(output, { recursive: true });
const server = createStaticServer();
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;

function closeEnough(actual, expected, tolerance, message) {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${message}: ${actual} versus ${expected}`);
}

/** Known solid-color artwork, independent of translated glyph antialiasing. Units: pt. */
function registrationMarks(name, width, height) {
  if (name === 'layout-colors') return [
    { name: 'top-left-red', x: 0, y: 0, width: 26, height: 9, rgb: [240, 44, 49] },
    { name: 'top-right-blue', x: 708, y: 0, width: 12, height: 23, rgb: [19, 90, 244] },
    { name: 'bottom-left-green', x: 0, y: 431, width: 10, height: 19, rgb: [18, 168, 111] },
    { name: 'bottom-right-purple', x: 689, y: 440, width: 31, height: 10, rgb: [158, 64, 221] },
  ];
  if (name === 'mixed-sizes') return [
    { name: 'top-border-green', x: 0, y: 0, width, height: 7, rgb: [24, 61, 56] },
    { name: 'bottom-left-orange', x: 0, y: height - 9, width: 32, height: 9, rgb: [210, 87, 49] },
    { name: 'bottom-right-blue', x: width - 18, y: height - 24, width: 18, height: 24, rgb: [49, 100, 187] },
  ];
  if (name === 'demo-a4') return [
    { name: 'green-header-left-edge', x: 0, y: 0, width: 20, height: 190, rgb: [30, 92, 81] },
    { name: 'gold-circle', x: 478, y: 687, width: 54, height: 54, rgb: [233, 215, 173] },
  ];
  return [];
}

/** Inspect the independently parsed archive, not the exporter implementation. */
async function inspectOffice(bytes, format, metadata) {
  const zip = await JSZip.loadAsync(bytes, { checkCRC32: true });
  const entries = Object.keys(zip.files);
  assert.ok(entries.includes('[Content_Types].xml'));
  const media = entries.filter(name => new RegExp(`^${format === 'docx' ? 'word' : 'ppt'}/media/.*\\.png$`).test(name));
  assert.equal(media.length, metadata.pages.length, `${format}: exactly one flattened image per page`);
  const expectedHashes = metadata.pages.map(p => p.canonicalSha256).sort();
  const mediaHashes = [];
  for (const name of media) mediaHashes.push(hash(await zip.file(name).async('nodebuffer')));
  assert.deepEqual(mediaHashes.sort(), expectedHashes, `${format}: every embedded PNG must exactly equal a canonical translation render`);
  for (const name of entries.filter(n => /\.rels$/.test(n))) {
    const xml = await zip.file(name).async('string');
    assert.ok(!/TargetMode="External"/.test(xml), `${format}: external resource in ${name}`);
  }
  if (format === 'docx') {
    const xml = await zip.file('word/document.xml').async('string');
    const sizes = [...xml.matchAll(/<w:pgSz\b[^>]*w:w="(\d+)"[^>]*w:h="(\d+)"[^>]*\/?\s*>/g)];
    assert.equal(sizes.length, metadata.pages.length, 'Word page-specific section sizes');
    sizes.forEach((match, i) => {
      closeEnough(+match[1] / 20, metadata.pages[i].widthPt, 0.05, 'Word page width');
      closeEnough(+match[2] / 20, metadata.pages[i].heightPt, 0.05, 'Word page height');
    });
    const extents = [...xml.matchAll(/<wp:extent\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/g)];
    assert.equal(extents.length, metadata.pages.length);
    extents.forEach((match, i) => {
      closeEnough(+match[1] / 12700, metadata.pages[i].widthPt, 0.001, 'Word image width');
      closeEnough(+match[2] / 12700, metadata.pages[i].heightPt, 0.001, 'Word image height');
    });
    assert.equal((xml.match(/<w:drawing\b/g) || []).length, metadata.pages.length);
    return { mediaCount: media.length, pageSections: sizes.length, exactImageHashes: true };
  }
  const presentation = await zip.file('ppt/presentation.xml').async('string');
  assert.equal((presentation.match(/<p:sldId\b/g) || []).length, metadata.pages.length);
  const size = presentation.match(/<p:sldSz\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
  assert.ok(size, 'PowerPoint slide dimensions');
  closeEnough(+size[1] / 12700, metadata.slide.widthPt, 0.001, 'PowerPoint slide width');
  closeEnough(+size[2] / 12700, metadata.slide.heightPt, 0.001, 'PowerPoint slide height');
  const slideNames = entries.filter(n => /^ppt\/slides\/slide\d+\.xml$/.test(n)).sort((a, b) => +a.match(/slide(\d+)/)[1] - +b.match(/slide(\d+)/)[1]);
  assert.equal(slideNames.length, metadata.pages.length);
  for (let i = 0; i < slideNames.length; i++) {
    const xml = await zip.file(slideNames[i]).async('string');
    assert.equal((xml.match(/<p:pic\b/g) || []).length, 1);
    const pic = xml.match(/<p:pic\b[\s\S]*?<\/p:pic>/)[0];
    const offset = pic.match(/<a:off\b[^>]*x="(\d+)"[^>]*y="(\d+)"/);
    const extent = pic.match(/<a:ext\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"/);
    assert.ok(offset && extent, 'PowerPoint image geometry');
    const source = metadata.pages[i];
    closeEnough(+offset[1] / 12700, (metadata.slide.widthPt - source.widthPt) / 2, 0.001, 'PowerPoint centered image x');
    closeEnough(+offset[2] / 12700, (metadata.slide.heightPt - source.heightPt) / 2, 0.001, 'PowerPoint centered image y');
    closeEnough(+extent[1] / 12700, source.widthPt, 0.001, 'PowerPoint original physical width');
    closeEnough(+extent[2] / 12700, source.heightPt, 0.001, 'PowerPoint original physical height');
  }
  return { mediaCount: media.length, slideCount: slideNames.length, exactImageHashes: true };
}

try {
  browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1500, height: 1120 }, acceptDownloads: true, deviceScaleFactor: 1 });
  await context.route('**/*', route => {
    const url = route.request().url();
    if (url.startsWith(origin + '/') || url.startsWith('blob:') || url.startsWith('data:')) return route.fallback();
    report.externalRequests.push(url); return route.abort();
  });
  const page = await context.newPage();
  page.on('pageerror', error => report.pageErrors.push(error.message));
  await page.route(origin + '/export-fixture.html', route => route.fulfill({ contentType: 'text/html', body:
    '<!doctype html><meta charset="utf-8"><title>Repeated document export QA</title><script src="/vendor/jspdf.umd.min.js"></script><script src="/vendor/jszip.min.js"></script>' }));
  await page.goto(origin + '/export-fixture.html');
  const cases = await page.evaluate(async () => {
    const engine = await import('/pdf-engine.js');
    const asBase64 = blob => new Promise(resolve => { const r = new FileReader(); r.onload = () => resolve(r.result.split(',')[1]); r.readAsDataURL(blob); });
    const makePdf = (w, h) => new jspdf.jsPDF({ unit: 'pt', format: [w, h], orientation: w > h ? 'landscape' : 'portrait', compress: true });
    const examples = [{ name: 'demo-a4', description: 'A4 designed document: colored title, paragraphs, rounded panel, table, vector illustrations and CJK translation', bytes: engine.createDemoPdf() }];

    const pdf = makePdf(720, 450);
    pdf.setFillColor('#fbf6ee'); pdf.rect(0, 0, 720, 450, 'F');
    // Deliberately place asymmetric colored edge marks to catch crop, mirror and shift.
    pdf.setFillColor('#f02c31'); pdf.rect(0, 0, 26, 9, 'F');
    pdf.setFillColor('#135af4'); pdf.rect(708, 0, 12, 23, 'F');
    pdf.setFillColor('#12a86f'); pdf.rect(0, 431, 10, 19, 'F');
    pdf.setFillColor('#9e40dd'); pdf.rect(689, 440, 31, 10, 'F');
    pdf.setFillColor('#172e52'); pdf.rect(32, 24, 656, 76, 'F');
    pdf.setTextColor('#ffffff'); pdf.setFont('helvetica', 'bold'); pdf.setFontSize(24); pdf.text('Global design review', 48, 65);
    pdf.setTextColor('#182b3d'); pdf.setFont('times', 'italic'); pdf.setFontSize(14); pdf.text('International typography', 42, 130);
    pdf.setFont('helvetica', 'normal'); pdf.setFontSize(12); pdf.text('Multiline translation region', 42, 168);
    pdf.setFont('courier', 'normal'); pdf.setFontSize(12); pdf.text('Metric value 123.45', 410, 130);
    pdf.setFont('helvetica', 'bold'); pdf.setFontSize(11);
    pdf.setFillColor('#dce9f2'); pdf.rect(42, 228, 620, 30, 'F');
    pdf.text('ITEM NAME', 55, 248); pdf.text('STATUS', 320, 248); pdf.text('RESULT', 525, 248);
    const rows = [['Alignment', 'Approved', 'Stable'], ['Color fidelity', 'Checked', 'Accurate'], ['Page edges', 'Preserved', 'Complete']];
    pdf.setFont('helvetica', 'normal');
    rows.forEach((row, i) => { const y = 278 + i * 32; pdf.setDrawColor('#5f7388'); pdf.line(42, y + 11, 662, y + 11); row.forEach((text, c) => pdf.text(text, [55, 320, 525][c], y)); });
    pdf.setTextColor('#a11f3a'); pdf.setFontSize(10); pdf.text('ROTATED ORIGINAL', 694, 370, { angle: 90 });
    pdf.setFillColor('#e5b64f'); pdf.circle(580, 182, 25, 'F'); pdf.setFillColor('#307f78'); pdf.circle(614, 196, 16, 'F');
    pdf.setFontSize(9); pdf.setTextColor('#324d69'); pdf.text('End of document', 42, 419);
    examples.push({ name: 'layout-colors', description: 'Landscape, edge registration marks, white text on dark fill, table lines, serif/mono text, diacritics, multiline CJK and preserved rotated text', bytes: pdf.output('arraybuffer') });

    const mixed = makePdf(360, 540);
    const sizes = [[360, 540], [720, 405], [480, 480]];
    sizes.forEach(([w, h], index) => {
      if (index) mixed.addPage([w, h], w > h ? 'landscape' : 'portrait');
      mixed.setFillColor(['#f3ebe0', '#e7f1f4', '#f3e8ef'][index]); mixed.rect(0, 0, w, h, 'F');
      mixed.setFillColor('#183d38'); mixed.rect(0, 0, w, 7, 'F');
      mixed.setFillColor('#d25731'); mixed.rect(0, h - 9, 32, 9, 'F');
      mixed.setFillColor('#3164bb'); mixed.rect(w - 18, h - 24, 18, 24, 'F');
      mixed.setFont('helvetica', 'bold'); mixed.setTextColor('#173d38'); mixed.setFontSize(22); mixed.text(['Portrait document', 'Landscape document', 'Square document'][index], 30, 54);
      mixed.setFont('helvetica', 'normal'); mixed.setFontSize(12); mixed.text('Consistent physical page geometry', 30, 90);
      mixed.text('Stable content, page ' + (index + 1), 30, 124);
      mixed.setLineWidth(1); mixed.setDrawColor('#6a7e86'); mixed.rect(30, 153, w - 60, h - 220);
      mixed.setFont('times', 'italic'); mixed.setFontSize(14); mixed.text('Language and layout', 43, 181);
      mixed.setFont('helvetica', 'normal'); mixed.setFontSize(9); mixed.text('PAGE ' + (index + 1), 30, h - 30);
    });
    examples.push({ name: 'mixed-sizes', description: 'Three pages with portrait/landscape/square dimensions and colored bleed edges; PPTX centers each unchanged page on a shared slide', bytes: mixed.output('arraybuffer') });

    const demoTranslations = new Map([
      ['FIELDNOTES / RESEARCH BRIEF', 'FIELDNOTES / 調査資料'], ['Design across languages', '言語を超えるデザイン'],
      ['Keep the structure. Make the message travel.', '構成を保ち、言葉を世界へ。'], ['OCTOBER 2026', '2026年10月'],
      ['A document is more than its words.', '文書の価値は言葉だけではない。'],
      ['Every page carries a visual rhythm: a heading, a quiet margin,', '見出し、余白、画像。ページにはリズムがあります。'],
      ['an image, and the precise relationship between them.', '各要素の関係を保ちます。'], ['Translation should respect that rhythm.', '翻訳でもリズムを大切に。'],
      ['THE APPROACH', '手法'], ['Translate the text. Preserve the composition.', '言葉を訳し、構図を保つ。'],
      ['Review each region before exporting your translated PDF.', '保存前に訳文をご確認ください。'], ['Built for a careful review', '丁寧な確認のために'],
      ['ELEMENT', '要素'], ['WHAT TO CHECK', '確認事項'], ['Page geometry', 'ページ構成'], ['Size, spacing, and alignment', 'サイズ・余白・配置'],
      ['Typography', '文字組み'], ['Font size and text fit', '文字サイズ・収まり'], ['Visual context', '視覚要素'],
      ['Images, rules, and background colors', '画像・罫線・背景色'], ['A little care makes every language feel at home.', 'どの言語も自然な見た目に。'],
    ]);
    const special = new Map([
      ['Global design review', '国際デザイン評価'], ['International typography', 'Café, naïve, résumé — 字体'],
      ['Multiline translation region', '一行目の翻訳\n二行目の確認'], ['Metric value 123.45', '数値 123.45'],
      ['ITEM NAME', '項目'], ['STATUS', '状態'], ['RESULT', '結果'], ['Alignment', '配置'], ['Approved', '承認'], ['Stable', '安定'],
      ['Color fidelity', '色の再現'], ['Checked', '確認'], ['Accurate', '正確'], ['Page edges', 'ページ端'], ['Preserved', '保持'], ['Complete', '完了'], ['End of document', '文書の末尾'],
      ['Portrait document', '縦長の文書'], ['Landscape document', '横長の文書'], ['Square document', '正方形の文書'],
      ['Consistent physical page geometry', '元のページ寸法を保持'], ['Language and layout', '言語と配置'],
    ]);
    window.__qaFixtures = [];
    const results = [];
    for (const example of examples) {
      const doc = await engine.loadPdf(example.bytes), states = [], pageImages = [];
      for (let n = 1; n <= doc.numPages; n++) {
        const state = await engine.renderPage(doc, n, 1.5);
        state.blocks = await engine.extractBlocks(state.page, state.viewport, state.canvas);
        for (const block of state.blocks) {
          if (!block.supported) continue;
          const text = demoTranslations.get(block.original) || special.get(block.original)
            || (/^Stable content/.test(block.original) ? '内容の安定性 ' + n : /^PAGE /.test(block.original) ? '頁 ' + n : '');
          if (text) block.translation = text;
          if (block.original === 'Multiline translation region') { block.width = 210; block.height = 40; }
          if (block.original === 'International typography') { block.width = 300; block.height = 24; }
        }
        const canvas = await engine.renderTranslatedCanvas(state, { scale: 2.25 });
        if (state.blocks.some(b => b.fit?.overflow)) throw Error(example.name + ': overflowing fixture translation');
        pageImages.push({ widthPt: state.width, heightPt: state.height, pixelWidth: canvas.width, pixelHeight: canvas.height,
          originalTextCount: state.blocks.length, translatedCount: state.blocks.filter(b => b.translation).length,
          unsupportedCount: state.blocks.filter(b => !b.supported).length,
          canonicalBase64: canvas.toDataURL('image/png').split(',')[1] });
        canvas.width = 1; canvas.height = 1; states.push(state);
      }
      const slideWidth = Math.max(...states.map(s => s.width)), slideHeight = Math.max(...states.map(s => s.height));
      for (let i = 0; i < states.length; i++) {
        const state = states[i], image = new Image(); image.src = 'data:image/png;base64,' + pageImages[i].canonicalBase64; await image.decode();
        const canvas = document.createElement('canvas'); canvas.width = Math.ceil(slideWidth * 2.25); canvas.height = Math.ceil(slideHeight * 2.25);
        const ctx = canvas.getContext('2d'); ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(image, (slideWidth - state.width) / 2 * 2.25, (slideHeight - state.height) / 2 * 2.25, state.width * 2.25, state.height * 2.25);
        pageImages[i].slideBase64 = canvas.toDataURL('image/png').split(',')[1];
        pageImages[i].slidePixelWidth = canvas.width; pageImages[i].slidePixelHeight = canvas.height;
        canvas.width = 1; canvas.height = 1;
      }
      window.__qaFixtures.push({ name: example.name, doc, states });
      results.push({ name: example.name, description: example.description, sourceBase64: await asBase64(new Blob([example.bytes], { type: 'application/pdf' })), pages: pageImages,
        slide: { widthPt: slideWidth, heightPt: slideHeight }, scale: 2.25 });
    }
    return results;
  });

  for (const fixture of cases) {
    const directory = path.join(output, fixture.name);
    await mkdir(directory, { recursive: true });
    const sourceBytes = Buffer.from(fixture.sourceBase64, 'base64');
    await writeFile(path.join(directory, 'source.pdf'), sourceBytes);
    const metadata = { name: fixture.name, description: fixture.description, sourcePdf: 'source.pdf', sourceSha256: hash(sourceBytes), scale: fixture.scale, slide: fixture.slide, pages: [], exports: [] };
    for (let i = 0; i < fixture.pages.length; i++) {
      const { canonicalBase64, slideBase64, ...metrics } = fixture.pages[i];
      const canonical = Buffer.from(canonicalBase64, 'base64'), slide = Buffer.from(slideBase64, 'base64');
      const canonicalPng = `canonical-page-${i + 1}.png`, canonicalSlidePng = `canonical-slide-${i + 1}.png`;
      await writeFile(path.join(directory, canonicalPng), canonical); await writeFile(path.join(directory, canonicalSlidePng), slide);
      assert.ok(metrics.originalTextCount > 0, 'fixture must contain selectable source PDF text');
      assert.ok(metrics.translatedCount > 0, 'fixture must contain translations');
      metadata.pages.push({ pageNumber: i + 1, ...metrics, registrationMarks: registrationMarks(fixture.name, metrics.widthPt, metrics.heightPt), canonicalPng, canonicalSha256: hash(canonical), canonicalSlidePng, canonicalSlideSha256: hash(slide) });
    }
    await writeFile(path.join(directory, 'metadata.json'), JSON.stringify(metadata, null, 2));
    for (let repetition = 1; repetition <= repetitions; repetition++) {
      for (const format of formats) {
        const rendered = await page.evaluate(async ({ name, format }) => {
          const engine = await import('/pdf-engine.js'), { exportOffice } = await import('/office-export.js');
          const fixture = window.__qaFixtures.find(f => f.name === name), progress = [];
          const fn = format === 'pdf' ? engine.exportPdf : exportOffice;
          const blob = await fn(fixture.states, { format, onProgress: (n, total) => progress.push([n, total]) });
          const base64 = await new Promise(resolve => { const r = new FileReader(); r.onload = () => resolve(r.result.split(',')[1]); r.readAsDataURL(blob); });
          const pdfPages = [];
          if (format === 'pdf') {
            const doc = await engine.loadPdf(await blob.arrayBuffer());
            for (let n = 1; n <= doc.numPages; n++) {
              const p = await doc.getPage(n), vp = p.getViewport({ scale: 1 });
              const output = await engine.renderPage(doc, n, 2.25), canonical = await engine.renderTranslatedCanvas(fixture.states[n - 1], { scale: 2.25 });
              const a = output.canvas.getContext('2d').getImageData(0, 0, output.canvas.width, output.canvas.height).data;
              const b = canonical.getContext('2d').getImageData(0, 0, canonical.width, canonical.height).data;
              let sum = 0, changed = 0;
              for (let j = 0; j < a.length; j += 4) { const d = Math.max(Math.abs(a[j] - b[j]), Math.abs(a[j + 1] - b[j + 1]), Math.abs(a[j + 2] - b[j + 2])); sum += d; if (d > 20) changed++; }
              pdfPages.push({ widthPt: vp.width, heightPt: vp.height, pixelWidth: output.canvas.width, pixelHeight: output.canvas.height,
                textItems: (await p.getTextContent()).items.length, meanMaxChannelError: sum / (a.length / 4), changedPixelFraction: changed / (a.length / 4) });
              output.canvas.width = 1; output.canvas.height = 1; canonical.width = 1; canonical.height = 1;
            }
            await doc.loadingTask.destroy();
          }
          return { base64, progress, mimeType: blob.type, pdfPages };
        }, { name: fixture.name, format });
        assert.deepEqual(rendered.progress, metadata.pages.map((_, i) => [i + 1, metadata.pages.length]), `${fixture.name} ${format}: progress`);
        const bytes = Buffer.from(rendered.base64, 'base64'), file = `export-${repetition}.${format}`;
        await writeFile(path.join(directory, file), bytes);
        let checks;
        if (format === 'pdf') {
          assert.equal(bytes.subarray(0, 5).toString(), '%PDF-');
          assert.equal(rendered.pdfPages.length, metadata.pages.length);
          for (let i = 0; i < rendered.pdfPages.length; i++) {
            const out = rendered.pdfPages[i], expected = metadata.pages[i];
            closeEnough(out.widthPt, expected.widthPt, 0.02, 'PDF width'); closeEnough(out.heightPt, expected.heightPt, 0.02, 'PDF height');
            assert.equal(out.pixelWidth, expected.pixelWidth); assert.equal(out.pixelHeight, expected.pixelHeight);
            assert.equal(out.textItems, 0, 'flattened output');
            assert.ok(out.meanMaxChannelError < 3, `PDF pixel mean error: ${out.meanMaxChannelError}`);
            assert.ok(out.changedPixelFraction < 0.025, `PDF changed pixels: ${out.changedPixelFraction}`);
          }
          checks = { pdfPages: rendered.pdfPages };
        } else checks = await inspectOffice(bytes, format, metadata);
        metadata.exports.push({ file, format, repetition, byteLength: bytes.length, sha256: hash(bytes), mimeType: rendered.mimeType, ...checks });
        await writeFile(path.join(directory, 'metadata.json'), JSON.stringify(metadata, null, 2));
        console.log(`PASS ${fixture.name} ${format} repeat ${repetition}`);
      }
    }
    report.fixtureCases.push(metadata);
  }
  await writeFile(path.join(output, 'fixtures-ready.json'), JSON.stringify({ fixtures: report.fixtureCases.map(f => ({ name: f.name, directory: path.join(output, f.name), metadata: 'metadata.json' })) }, null, 2));
  console.log(`OFFICE_RENDERER_FIXTURES_READY ${output}`);

  // A real renderer overflow must fail atomically in every output format.
  report.overflowRejections = await page.evaluate(async () => {
    const engine = await import('/pdf-engine.js'), { exportOffice } = await import('/office-export.js');
    const fixture = window.__qaFixtures[0], block = fixture.states[0].blocks.find(b => b.supported);
    const original = { translation: block.translation, width: block.width, height: block.height };
    block.translation = '収まりきらない訳文'.repeat(100); block.width = 1; block.height = 1;
    const results = [];
    try {
      for (const format of ['pdf', 'docx', 'pptx']) {
        const progress = [];
        try {
          const fn = format === 'pdf' ? engine.exportPdf : exportOffice;
          await fn(fixture.states, { format, onProgress: (n, total) => progress.push([n, total]) });
          results.push({ format, rejected: false });
        } catch (error) { results.push({ format, rejected: true, code: error.code, blockIds: error.blockIds, progress, expectedId: block.id }); }
      }
    } finally { Object.assign(block, original); }
    return results;
  });
  for (const check of report.overflowRejections) {
    assert.equal(check.rejected, true); assert.equal(check.code, 'TEXT_OVERFLOW');
    assert.ok(check.blockIds.includes(check.expectedId)); assert.deepEqual(check.progress, []);
  }

  // Exercise actual UI uploads/downloads, including preserved settings during export/navigation.
  const ui = await context.newPage();
  ui.on('pageerror', error => report.pageErrors.push(error.message));
  ui.on('dialog', dialog => dialog.accept());
  await ui.route(origin + '/api/config', route => route.fulfill({ json: { local: { baseUrl: 'http://llm.test.invalid/v1', model: 'qa-local-model', configured: true }, openai: { baseUrl: 'https://openai.test.invalid/v1', model: 'gpt-4o', configured: true, apiMode: 'auto' }, proxy: { configured: true } } }));
  await ui.route(origin + '/api/translate-batch', route => { report.translationRequests++; return route.abort(); });
  await ui.goto(origin, { waitUntil: 'networkidle' });
  const idle = () => ui.waitForFunction(() => !document.body.classList.contains('busy'));
  assert.deepEqual(await ui.locator('#export-format option').evaluateAll(options => options.map(o => o.value)), formats);
  assert.equal(await ui.locator('#export-button').isDisabled(), true);
  await ui.locator('#demo-button').click(); await idle();
  await ui.waitForFunction(() => document.querySelector('#translated-paper canvas')?.width > 1);
  await ui.locator('#provider').selectOption('openai'); await ui.locator('#model').fill('gpt-5.4');
  await ui.locator('#target-language').selectOption('Japanese');
  const readSettings = () => ui.evaluate(() => Object.fromEntries(['provider', 'model', 'target-language', 'source-language', 'scope', 'zoom'].map(id => [id, document.getElementById(id).value])));
  const settings = await readSettings();
  const demoName = await ui.locator('#filename').textContent();
  for (const format of formats) {
    await ui.locator('#export-format').selectOption(format);
    const event = ui.waitForEvent('download'); await ui.locator('#export-button').click();
    const download = await event; assert.equal(await download.failure(), null);
    const expectedName = demoName.replace(/\.pdf$/i, '') + '-translated.' + format;
    assert.equal(download.suggestedFilename(), expectedName);
    const savedPath = path.join(output, `ui-sample.${format}`); await download.saveAs(savedPath); await idle();
    assert.deepEqual(await readSettings(), settings, 'output must preserve provider/language/model/view settings');
    assert.equal(await ui.locator('#export-format').inputValue(), format);
    const bytes = await readFile(savedPath);
    if (format === 'pdf') assert.equal(bytes.subarray(0, 5).toString(), '%PDF-');
    else { const zip = await JSZip.loadAsync(bytes, { checkCRC32: true }); assert.ok(zip.file(format === 'docx' ? 'word/document.xml' : 'ppt/presentation.xml')); }
    report.uiDownloads.push({ format, suggestedFilename: download.suggestedFilename(), byteLength: bytes.length });
  }
  await ui.locator('#file-input').setInputFiles(path.join(output, 'mixed-sizes', 'source.pdf')); await idle();
  await ui.waitForFunction(() => document.querySelector('#page-total').textContent === '3');
  await ui.locator('#page-list button').nth(2).click(); await idle();
  assert.deepEqual(await readSettings(), settings, 'upload and page navigation preserve settings');
  assert.equal(await ui.locator('#export-format').inputValue(), 'pptx');
  await ui.locator('#demo-button').click(); await idle();
  await ui.locator('#original-paper .text-box[title="Design across languages"]').click();
  await ui.locator('#toast').waitFor({ state: 'hidden' });
  for (const format of formats) {
    await ui.locator('#export-format').selectOption(format);
    await ui.screenshot({ path: path.join(output, `export-selector-${format}.png`), fullPage: true });
  }
  await ui.locator('#export-format').selectOption('docx');
  await ui.screenshot({ path: path.join(output, 'export-selector-desktop.png'), fullPage: true });
  await ui.setViewportSize({ width: 390, height: 844 });
  await ui.waitForFunction(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
  await ui.screenshot({ path: path.join(output, 'export-selector-mobile.png'), fullPage: true });
  assert.equal(report.translationRequests, 0, 'export UI must not call translation API');
  assert.deepEqual(report.pageErrors, []); assert.deepEqual(report.externalRequests, []);
  report.totalEngineExports = report.fixtureCases.reduce((n, f) => n + f.exports.length, 0);
  report.totalUiExports = report.uiDownloads.length;
  report.completed = true;
  await writeFile(path.join(output, 'browser-exports-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ completed: true, totalEngineExports: report.totalEngineExports, totalUiExports: report.totalUiExports, pageErrors: report.pageErrors, externalRequests: report.externalRequests, fixturesDirectory: output }, null, 2));
  await context.close();
} catch (error) {
  report.completed = false; report.failure = { message: error.message, stack: error.stack };
  await writeFile(path.join(output, 'browser-exports-report.json'), JSON.stringify(report, null, 2));
  throw error;
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
