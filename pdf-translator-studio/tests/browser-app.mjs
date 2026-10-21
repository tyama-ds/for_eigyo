/** UI integration QA: isolated headless Edge context and mocked translation API. */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = path.join(root, 'tmp', 'app-qa');
await mkdir(output, { recursive: true });
const origin = process.env.APP_ORIGIN || 'http://127.0.0.1:4173';
const browserModule = process.env.PLAYWRIGHT_MODULE
  ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : 'playwright';
const { chromium } = await import(browserModule);
const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, acceptDownloads: true });
const page = await context.newPage();
const pageErrors = [], externalRequests = [], apiCalls = [], completed = [];
let apiMode = 'success', releasePending, holdAtCall;
let translationSettings = { localTimeoutSeconds: 600, localBatchBlocks: 4, localBatchCharacters: 1000, openaiTimeoutSeconds: 180 };
page.on('pageerror', error => pageErrors.push(error.message));
page.on('dialog', dialog => dialog.accept());
await context.route('**/*', async route => {
  const url = route.request().url();
  if (url.startsWith(origin + '/')) return route.fallback();
  externalRequests.push(url);
  return route.abort();
});
await page.route(origin + '/api/config', route => route.fulfill({ json: {
  local: { baseUrl: 'http://llm.test.invalid/v1', model: 'qa-local-model', configured: true },
  openai: { baseUrl: 'https://openai.test.invalid/v1', model: 'gpt-4o', configured: true, apiMode: 'auto' },
  proxy: { configured: true, noProxy: 'localhost,.internal' }, apiModes: ['auto', 'chat', 'responses'],
} }));
await page.route(origin + '/api/translation-settings', route => {
  if (route.request().method() === 'PUT') translationSettings = route.request().postDataJSON();
  return route.fulfill({ json: translationSettings });
});
await page.route(origin + '/api/translate-batch', async route => {
  const payload = route.request().postDataJSON();
  apiCalls.push(payload);
  if (apiMode === 'error') return route.fulfill({ status: 502, json: { error: 'QA: モデルの応答形式が不正です。' } });
  if (apiMode === 'hold' && apiCalls.length >= holdAtCall) {
    await new Promise(resolve => { releasePending = resolve; });
    try { await route.abort(); } catch { /* The browser may already have cancelled. */ }
    return;
  }
  return route.fulfill({ json: { translations: payload.blocks.map(b => ({ id: b.id, text: '翻訳済み' })) } });
});
const idle = () => page.waitForFunction(() => !document.body.classList.contains('busy'));
const value = selector => page.locator(selector).inputValue();
const toast = text => page.waitForFunction(expected => document.querySelector('#toast').textContent.includes(expected), text);
const capture = async name => { await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true }); };
async function waitForPreview() {
  await page.waitForFunction(() => document.querySelector('#original-paper canvas')?.width > 1 && document.querySelector('#translated-paper canvas')?.width > 1);
  await idle();
}
async function clickDownload(selector, filename) {
  const promise = page.waitForEvent('download');
  await page.locator(selector).click();
  const downloaded = await promise;
  assert.equal(await downloaded.failure(), null);
  const destination = path.join(output, filename);
  await downloaded.saveAs(destination);
  await idle();
  return destination;
}
try {
  await page.goto(origin, { waitUntil: 'networkidle' });
  assert.equal(await page.title(), 'PDF Lingua · 翻訳スタジオ');
  assert.equal(await page.locator('#translate-button').isDisabled(), true);
  assert.equal(await value('#model'), 'qa-local-model');
  await capture('desktop-empty');

  await page.locator('#demo-button').click();
  await waitForPreview();
  assert.equal(await page.locator('#page-total').textContent(), '1');
  assert.equal(await page.locator('#original-paper .text-box').count(), 23);
  assert.equal(await page.locator('#translated-paper .text-box').count(), 23);
  assert.equal(await page.locator('#translated-count').textContent(), '21 / 23');
  assert.equal(apiCalls.length, 0, 'demo does not call any provider');
  assert.equal(await page.evaluate(() => {
    const a = document.querySelector('#original-paper canvas'), b = document.querySelector('#translated-paper canvas');
    return a.width === b.width && a.height === b.height && a.toDataURL() !== b.toDataURL();
  }), true);
  completed.push('demo, source/translation canvas, no provider requests');

  const heading = page.locator('#original-paper .text-box[title="Design across languages"]');
  await heading.click();
  assert.equal(await value('#original-text'), 'Design across languages');
  assert.equal(await value('#translation-text'), '言語を超えるデザイン');
  await page.locator('#translation-text').fill('編集テスト');
  await page.locator('#font-size').fill('28');
  await page.waitForFunction(() => document.querySelector('#block-status').textContent === '訳文あり');
  await page.locator('#show-boxes').uncheck();
  assert.equal(await page.locator('#preview').evaluate(el => el.classList.contains('hide-boxes')), true);
  await page.locator('#show-boxes').check();
  await page.locator('#zoom').selectOption('1');
  await page.waitForFunction(() => Math.abs(parseFloat(document.querySelector('#original-paper').style.width) - 595.28) < 1);
  await page.locator('#zoom').selectOption('fit');
  completed.push('box selection, manual translation, font size, box toggle, zoom');

  const savedProject = await clickDownload('#save-project', 'sample.lingua.json');
  const saved = JSON.parse(await readFile(savedProject, 'utf8'));
  assert.equal(saved.format, 'pdf-lingua-1');
  assert.equal(saved.pages[0].blocks.length, 23);
  const edited = saved.pages[0].blocks.find(b => b.translation === '編集テスト');
  assert.ok(edited);
  assert.equal(edited.fontSize, 28);
  await page.locator('#translation-text').fill('変更後');
  await page.locator('#project-input').setInputFiles(savedProject);
  await toast('編集データを復元しました');
  assert.equal(await value('#translation-text'), '編集テスト');
  await page.locator('#project-input').setInputFiles({ name: 'wrong.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({ ...saved, hash: 'wrong-document' })) });
  await toast('同じ元PDFを開いてください');
  assert.equal(await value('#translation-text'), '編集テスト');
  completed.push('project download/restore and wrong-document rejection');

  await page.locator('#translation-text').fill('<img src=x onerror="globalThis.__qaInjected=1">');
  assert.equal(await page.evaluate(() => globalThis.__qaInjected), undefined);
  assert.equal(await page.locator('#block-editor img').count(), 0);
  await page.locator('#translation-text').fill('編集テスト');
  const pdfPath = await clickDownload('#export-button', 'sample-translated.pdf');
  assert.equal((await readFile(pdfPath)).subarray(0, 5).toString(), '%PDF-');
  await toast('PDFを保存しました');
  completed.push('literal markup stays inert; PDF export download');

  await page.locator('#scope').selectOption('selected');
  assert.equal(await page.locator('#skip-translated').isChecked(), true, 'manual translations are protected by default');
  const beforeSkip = apiCalls.length;
  await page.locator('#translate-button').click();
  await idle();
  assert.equal(apiCalls.length, beforeSkip, 'an already translated selected region is skipped');
  assert.equal(await value('#translation-text'), '編集テスト');
  await page.locator('#skip-translated').uncheck();
  await page.locator('#translate-button').click();
  await toast('翻訳が完了しました');
  await idle();
  assert.equal(apiCalls.at(-1).blocks.length, 1);
  assert.equal(apiCalls.at(-1).blocks[0].text, 'Design across languages');
  assert.equal(await value('#translation-text'), '翻訳済み');
  assert.equal(apiCalls.at(-1).config.provider, 'local');
  apiMode = 'error';
  const beforeError = apiCalls.length;
  await page.locator('#translate-button').click();
  await toast('QA: モデルの応答形式');
  await idle();
  assert.equal(apiCalls.length, beforeError + 1, 'validation failures are not retried');
  assert.equal(await value('#translation-text'), '翻訳済み');
  const errorStatus = await page.locator('#status').textContent();
  assert.ok(errorStatus.includes('QA: モデルの応答形式が不正です。'));
  assert.equal(await page.locator('#resume-translation').isVisible(), true);
  assert.equal(await page.locator('#resume-translation').isEnabled(), true);
  await page.locator('#toast').waitFor({ state: 'hidden' });
  assert.equal(await page.locator('#status').textContent(), errorStatus, 'error details persist after the toast disappears and progress timer stops');
  apiMode = 'hold';
  const beforeAbort = apiCalls.length;
  holdAtCall = beforeAbort + 2;
  await page.locator('#scope').selectOption('page');
  await page.locator('#translation-text').fill('取消前の訳文');
  await page.locator('#translate-button').click();
  await page.waitForFunction(() => !document.querySelector('#cancel-button').hidden);
  assert.equal(await page.locator('#translation-text').isDisabled(), true);
  assert.equal(await page.locator('#zoom').isDisabled(), true);
  await page.waitForFunction(() => /翻訳中 4 \/ 23 領域 · バッチ 2 \/ 6 · [1-9]\d*秒経過/.test(document.querySelector('#status').textContent));
  assert.equal(apiCalls.at(-2).blocks.length, 4, 'local UI requests use the smaller default batch');
  assert.equal(apiCalls.at(-2).config.providerTimeoutMs, 600000, 'local UI requests use the saved wait time');
  assert.equal(apiCalls.length, beforeAbort + 2, 'elapsed-time progress never sends another API request');
  const completedBeforeCancel = apiCalls.at(-2).blocks.map(block => block.id);
  const selectedId = await heading.getAttribute('data-block');
  const expectedSelected = completedBeforeCancel.includes(selectedId) ? '翻訳済み' : '取消前の訳文';
  await page.locator('#cancel-button').click();
  await toast('翻訳を中止しました');
  await idle();
  assert.equal(await value('#translation-text'), expectedSelected, 'cancel retains completed translations');
  releasePending?.(); releasePending = undefined; apiMode = 'success';
  const cancelledStatus = await page.locator('#status').textContent();
  await page.waitForTimeout(1200);
  assert.equal(await page.locator('#status').textContent(), cancelledStatus, 'cancel clears the elapsed-time interval');
  assert.equal(await page.locator('#resume-translation').isEnabled(), true);
  assert.ok((await page.locator('#resume-translation').textContent()).includes('19'));
  const checkpoint = JSON.parse(await readFile(await clickDownload('#save-project', 'cancelled-checkpoint.lingua.json'), 'utf8'));
  for (const id of completedBeforeCancel) assert.equal(checkpoint.pages[0].blocks.find(block => block.id === id).translation, '翻訳済み');

  // A failure on the next pending batch must preserve the earlier checkpoint.
  apiMode = 'error';
  const beforeResumeError = apiCalls.length;
  await page.locator('#resume-translation').click();
  await toast('QA: モデルの応答形式');
  await idle();
  assert.equal(apiCalls.length, beforeResumeError + 1);
  assert.ok(apiCalls.at(-1).blocks.every(block => !completedBeforeCancel.includes(block.id)), 'resume never sends a finished region again');
  const retained = JSON.parse(await readFile(await clickDownload('#save-project', 'error-checkpoint.lingua.json'), 'utf8'));
  assert.deepEqual(retained.pages, checkpoint.pages, 'failed batch cannot undo or replace any validated translation');
  assert.equal(await page.locator('#resume-translation').isEnabled(), true);

  apiMode = 'success';
  const beforeResumeSuccess = apiCalls.length;
  await page.locator('#resume-translation').click();
  await toast('翻訳が完了しました');
  await idle();
  const resumedIds = apiCalls.slice(beforeResumeSuccess).flatMap(call => call.blocks.map(block => block.id));
  assert.equal(resumedIds.length, 19);
  assert.equal(new Set(resumedIds).size, 19, 'remaining regions are sent exactly once');
  assert.ok(resumedIds.every(id => !completedBeforeCancel.includes(id)));
  assert.equal(await page.locator('#resume-translation').isVisible(), false);
  assert.equal(await page.locator('#translated-count').textContent(), '23 / 23');
  completed.push('default skip preserves manual text; mock success/error/cancel; validated batch retention and export; resume sends only remaining regions; busy controls, elapsed progress and persistent errors');

  await page.locator('#scope').selectOption('selected');
  await page.locator('#provider').selectOption('openai');
  assert.equal(await value('#model'), 'gpt-4o');
  await page.locator('#settings-button').click();
  await page.locator('#api-mode').selectOption('chat');
  await page.locator('#reasoning').selectOption('low');
  await page.getByRole('button', { name: '設定を閉じる' }).click();
  await page.locator('#model').fill('gpt-5');
  await page.locator('#translate-button').click();
  await toast('翻訳が完了しました');
  await idle();
  assert.deepEqual(apiCalls.at(-1).config, { provider: 'openai', model: 'gpt-5', sourceLanguage: 'auto', targetLanguage: 'Japanese', apiMode: 'chat', reasoningEffort: 'low', providerTimeoutMs: 180000 });
  completed.push('OpenAI selection, model and explicit API/reasoning settings');
  await capture('desktop-edited');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => window.innerWidth === 390);
  await page.waitForFunction(() => parseFloat(document.querySelector('#original-paper').style.width) <= document.querySelector('#original-scroll').clientWidth);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true, 'mobile has no document-level horizontal overflow');
  await capture('mobile-edited');
  await page.setViewportSize({ width: 1440, height: 1100 });
  completed.push('1440px and 390px responsive layout');

  const twoPages = await page.evaluate(() => {
    const pdf = new jspdf.jsPDF({ unit: 'pt', format: [500, 700] });
    pdf.text('First page note', 50, 70); pdf.addPage(); pdf.text('Second page note', 50, 70);
    return Array.from(new Uint8Array(pdf.output('arraybuffer')));
  });
  await page.locator('#file-input').setInputFiles({ name: 'two-pages.pdf', mimeType: 'application/pdf', buffer: Buffer.from(twoPages) });
  await page.waitForFunction(() => document.querySelector('#page-total').textContent === '2' && !document.body.classList.contains('busy'));
  await page.locator('#page-list button').nth(1).click();
  await page.waitForSelector('#original-paper .text-box[title="Second page note"]');
  await page.locator('#scope').selectOption('page');
  await page.locator('#translate-button').click();
  await idle();
  assert.equal(apiCalls.at(-1).blocks[0].text, 'Second page note');
  assert.equal(apiCalls.at(-1).blocks.length, 1);
  await page.locator('#scope').selectOption('all');
  await page.locator('#translate-button').click();
  await idle();
  assert.equal(apiCalls.at(-1).blocks.length, 2);
  await page.locator('#page-list button').first().click();
  await page.waitForSelector('#original-paper .text-box[title="First page note"]');
  await page.locator('#original-paper .text-box').first().click();
  assert.equal(await value('#translation-text'), '翻訳済み');
  completed.push('multi-page PDF upload, page navigation, page/all scopes');

  // Final review images show the supplied sample, rather than mocked API text.
  // Read public local settings only; provider requests remain intercepted.
  await page.unroute(origin + '/api/config');
  await page.goto(origin, { waitUntil: 'networkidle' });
  await page.locator('#demo-button').click();
  await waitForPreview();
  await page.locator('#original-paper .text-box[title="Design across languages"]').click();
  assert.equal(await value('#translation-text'), '言語を超えるデザイン');
  await page.locator('#toast').waitFor({ state: 'hidden' });
  await page.evaluate(() => window.scrollTo(0, 0));
  await capture('studio');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => parseFloat(document.querySelector('#original-paper').style.width) <= document.querySelector('#original-scroll').clientWidth);
  await page.evaluate(() => window.scrollTo(0, 0));
  await capture('studio-mobile');

  assert.deepEqual(pageErrors, []);
  assert.deepEqual(externalRequests, []);
  const report = { completed, pageErrors, externalRequests, mockedTranslationRequests: apiCalls.length, screenshots: ['desktop-empty.png', 'desktop-edited.png', 'mobile-edited.png', 'studio.png', 'studio-mobile.png'] };
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  await capture('failure').catch(() => {});
  console.error('UI QA failed:', error.message);
  console.error(JSON.stringify({ completed, pageErrors, externalRequests, status: await page.locator('#status').textContent().catch(() => ''), toast: await page.locator('#toast').textContent().catch(() => '') }, null, 2));
  throw error;
} finally {
  releasePending?.();
  await context.close();
  await browser.close();
}
