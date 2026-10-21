/** Real-browser regression: recover from a failed PDF.js import without reload. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createStaticServer } from '../server.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = path.join(root, 'tmp', 'module-retry-qa');
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
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const moduleRequests = [], workerRequests = [], pageErrors = [];
  let remainingFailures = 2;
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname === '/vendor/pdf.worker.mjs') workerRequests.push(url.pathname + url.search);
  });
  await page.route(`${origin}/vendor/pdf.mjs*`, route => {
    const url = new URL(route.request().url());
    moduleRequests.push(url.pathname + url.search);
    // Simulate the library request made after the local server has stopped.
    if (remainingFailures-- > 0) return route.abort('connectionrefused');
    return route.continue();
  });
  await page.goto(origin, { waitUntil: 'networkidle' });
  await page.evaluate(() => { window.retryQaDocument = 'same-document'; });

  const idle = () => page.waitForFunction(() => !document.body.classList.contains('busy'));
  for (let attempt = 1; attempt <= 2; attempt++) {
    await page.locator('#demo-button').click();
    await idle();
    const message = await page.locator('#toast').textContent();
    assert.match(message, /ローカルサーバーを起動/);
    assert.match(message, /サンプルを再度開いて/);
    assert.equal(moduleRequests.length, attempt, 'each retry must actually request the module');
    assert.equal(await page.locator('#original-paper canvas').count(), 0);
    assert.equal(await page.locator('#demo-button').isEnabled(), true);
  }

  // The server is available again. Click the same action without reloading.
  await page.locator('#demo-button').click();
  await page.waitForFunction(() => document.querySelector('#original-paper canvas')?.width > 1
    && document.querySelector('#translated-paper canvas')?.width > 1);
  await idle();
  assert.equal(await page.evaluate(() => window.retryQaDocument), 'same-document');
  assert.equal(await page.locator('#page-total').textContent(), '1');
  assert.equal(await page.locator('#original-paper .text-box').count(), 23);
  assert.equal(await page.locator('#translated-count').textContent(), '21 / 23');
  assert.deepEqual(moduleRequests, ['/vendor/pdf.mjs', '/vendor/pdf.mjs?retry=1', '/vendor/pdf.mjs?retry=2']);
  assert.ok(workerRequests.length > 0, 'the real PDF.js worker must load');
  assert.ok(workerRequests.every(url => url === '/vendor/pdf.worker.mjs'), 'worker URL is unchanged');

  // Reopening another document reuses the successful module, with no new import.
  await page.locator('#demo-button').click();
  await idle();
  assert.equal(await page.locator('#original-paper .text-box').count(), 23);
  assert.equal(moduleRequests.length, 3);
  assert.deepEqual(pageErrors, []);
  const report = { passed: true, failedAttempts: 2, sameDocumentRecovery: true,
    moduleRequests, workerRequests, blockCount: 23, translatedCount: 21, pageErrors };
  await mkdir(output, { recursive: true });
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
