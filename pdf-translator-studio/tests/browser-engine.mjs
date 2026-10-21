/** Local browser integration QA; requires an installed Playwright and Edge/Chrome. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createStaticServer } from '../server.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = path.join(root, 'tmp', 'engine-qa');
await mkdir(output, { recursive: true });
const modulePath = process.env.PLAYWRIGHT_MODULE
  || path.join(os.homedir(), '.cache', 'codex-runtimes', 'codex-primary-runtime',
    'dependencies', 'node', 'node_modules', 'playwright', 'index.mjs');
const { chromium } = await import(pathToFileURL(modulePath).href);
const server = createStaticServer();
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1100 }, deviceScaleFactor: 1 });
  const failures = [];
  page.on('pageerror', error => failures.push(error.message));
  await page.route(`${origin}/engine-fixture.html`, route => route.fulfill({ contentType: 'text/html', body:
    '<!doctype html><html lang="ja"><meta charset="utf-8"><title>PDF Engine QA</title>'
    + '<style>body{margin:0;background:#e9ece9;padding:24px;font-family:Arial,sans-serif}main{display:flex;gap:24px}section{background:white;padding:16px}h2{font-size:14px}canvas{display:block;width:595.28px;height:auto}</style>'
    + '<script src="/vendor/jspdf.umd.min.js"></script><main><section><h2>Original PDF</h2><div id="original"></div></section>'
    + '<section><h2>Japanese replacement</h2><div id="translated"></div></section></main></html>' }));
  await page.goto(`${origin}/engine-fixture.html`);
  const result = await page.evaluate(async () => {
    const engine = await import('/pdf-engine.js');
    const originalBytes = engine.createDemoPdf();
    const doc = await engine.loadPdf(originalBytes);
    const state = await engine.renderPage(doc, 1, 1.5);
    state.blocks = await engine.extractBlocks(state.page, state.viewport, state.canvas);
    document.querySelector('#original').append(state.canvas);
    const replacements = new Map([
      ['FIELDNOTES / RESEARCH BRIEF', 'FIELDNOTES / リサーチレポート'],
      ['Design across languages', '言語を越えるデザイン'],
      ['Keep the structure. Make the message travel.', '構成を保ち、伝えたいことを世界へ。'],
      ['OCTOBER 2026', '2026年10月'],
      ['A document is more than its words.', '文書の価値は、言葉だけではない。'],
      ['Every page carries a visual rhythm: a heading, a quiet margin,', '見出し、余白、画像。ページには視覚的なリズムがあります。'],
      ['an image, and the precise relationship between them.', 'それぞれの要素が、緻密な関係を生み出します。'],
      ['Translation should respect that rhythm.', '翻訳でも、そのリズムを大切に。'],
      ['THE APPROACH', 'アプローチ'],
      ['Translate the text. Preserve the composition.', '言葉を翻訳し、構図を保つ。'],
      ['Review each region before exporting your translated PDF.', '書き出す前に、各領域の訳文をご確認ください。'],
      ['Built for a careful review', '丁寧に確認するための設計'],
      ['ELEMENT', '要素'], ['WHAT TO CHECK', '確認すること'],
      ['Page geometry', 'ページの構成'], ['Size, spacing, and alignment', 'サイズ、余白、配置'],
      ['Typography', '文字のデザイン'], ['Font size and text fit', '文字サイズと枠内の収まり'],
      ['Visual context', '視覚的な文脈'], ['Images, rules, and background colors', '画像、罫線、背景色'],
      ['A little care makes every language feel at home.', 'ひと工夫で、どの言語も自然な見た目に。'],
    ]);
    for (const block of state.blocks) if (replacements.has(block.original)) block.translation = replacements.get(block.original);
    const translated = await engine.renderTranslatedCanvas(state, { scale: 1.5 });
    document.querySelector('#translated').append(translated);
    const sampleBlob = await engine.exportPdf([state]);
    const exported = await engine.loadPdf(await sampleBlob.arrayBuffer());
    const outputPage = await exported.getPage(1);
    const outputViewport = outputPage.getViewport({ scale: 1 });
    const outputText = await outputPage.getTextContent();
    const custom = new jspdf.jsPDF({ unit: 'pt', format: [500, 700], orientation: 'portrait' });
    custom.text('Portrait page', 30, 50);
    custom.addPage([700, 500], 'landscape'); custom.text('Landscape page', 30, 50);
    const mixedDocument = await engine.loadPdf(custom.output('arraybuffer'));
    const mixedPages = [];
    for (let n = 1; n <= 2; n++) mixedPages.push(await engine.renderPage(mixedDocument, n, 1));
    const mixedBlob = await engine.exportPdf(mixedPages);
    const mixedExport = await engine.loadPdf(await mixedBlob.arrayBuffer());
    const mixedSizes = [];
    for (let n = 1; n <= 2; n++) {
      const vp = (await mixedExport.getPage(n)).getViewport({ scale: 1 });
      mixedSizes.push([vp.width, vp.height]);
    }
    const sampleBase64 = await new Promise(resolve => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.readAsDataURL(sampleBlob);
    });
    const result = {
      blockCount: state.blocks.length,
      translatedCount: state.blocks.filter(b => b.translation).length,
      unsupportedCount: state.blocks.filter(b => !b.supported).length,
      warnings: state.blocks.filter(b => b.warning).map(b => ({ original: b.original, warning: b.warning })),
      fontDetails: state.blocks.slice(0, 5).map(({ original, fontSize, fontWeight, background, color, sourceFont }) => ({ original, fontSize, fontWeight, background, color, sourceFont })),
      overflowCount: state.blocks.filter(b => b.fit?.overflow).length,
      sourceSize: [state.width, state.height],
      outputSize: [outputViewport.width, outputViewport.height],
      outputTextCount: outputText.items.length,
      mixedSizes,
      sampleBase64,
    };
    await exported.loadingTask.destroy(); await mixedDocument.loadingTask.destroy(); await mixedExport.loadingTask.destroy();
    return result;
  });
  assert.equal(result.blockCount, 23);
  assert.equal(result.translatedCount, 21);
  assert.equal(result.unsupportedCount, 0);
  assert.equal(result.overflowCount, 0);
  result.sourceSize.forEach((size, i) => assert.ok(Math.abs(size - result.outputSize[i]) < 0.02));
  assert.equal(result.outputTextCount, 0, 'output is deliberately rasterized');
  assert.deepEqual(result.mixedSizes, [[500, 700], [700, 500]]);
  assert.deepEqual(failures, []);
  await page.screenshot({ path: path.join(output, 'comparison.png'), fullPage: true });
  await writeFile(path.join(output, 'translated-sample.pdf'), Buffer.from(result.sampleBase64, 'base64'));
  delete result.sampleBase64;
  await writeFile(path.join(output, 'report.json'), JSON.stringify({ ...result, failures }, null, 2));
  console.log(JSON.stringify(result, null, 2));
  console.log(`Browser QA artifacts: ${output}`);
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
