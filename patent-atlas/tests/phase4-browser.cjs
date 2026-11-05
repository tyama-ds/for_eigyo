// Optional browser verification: NODE_PATH must resolve playwright; no real workspace is used.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { once } = require('node:events');

(async () => {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'patent-atlas-phase4-browser-'));
  const output = path.resolve(process.env.PHASE4_SCREENSHOTS || path.join(root, 'docs/screenshots/phase4'));
  fs.mkdirSync(output, { recursive: true });
  const socket = net.createServer();
  socket.listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const port = socket.address().port;
  await new Promise((resolve) => socket.close(resolve));
  const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const server = spawn(python, ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: root,
    windowsHide: true,
    env: { ...process.env, PATENT_ATLAS_DATA: path.join(temp, 'workspace'), PYTHONDONTWRITEBYTECODE: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let serverLog = '',
    browser;
  server.stdout.on('data', (b) => (serverLog += b));
  server.stderr.on('data', (b) => (serverLog += b));
  server.on('error', (e) => (serverLog += e.message));
  const origin = `http://127.0.0.1:${port}`;
  try {
    for (let i = 0; i < 300; i++) {
      if (server.exitCode !== null) throw new Error(serverLog);
      try {
        if ((await fetch(origin + '/api/state')).ok) break;
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 100));
      if (i === 299) throw new Error('隔離サーバーの起動に失敗: ' + serverLog);
    }
    browser = await chromium.launch({
      headless: true,
      ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
        ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH }
        : {}),
    });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.route('**/*', (route) =>
      new URL(route.request().url()).origin === origin ? route.continue() : route.abort()
    );
    async function check(stage) {
      assert.equal(await page.locator('nav').count(), 1);
      assert.equal(await page.locator('[data-flow-action]:visible').count(), 5);
      assert.equal(await page.locator('.tab.active').count(), 1);
      assert.equal(await page.locator('.view.active').count(), 1);
      if (stage)
        assert.equal(
          await page.locator('[data-flow-action][aria-current]').getAttribute('data-flow-action'),
          stage
        );
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
    async function shot(name, stage) {
      await page.waitForFunction(() => !document.querySelector('#toast')?.classList.contains('visible'));
      await page.evaluate(() => scrollTo(0, 0));
      await page.evaluate(
        () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      );
      await check(stage);
      await page.screenshot({ path: path.join(output, name + '.png') });
    }
    async function stage(id) {
      await page.locator(`[data-flow-action="${id}"]`).click();
      await check(id);
    }
    async function tool(tab, editor) {
      await page
        .locator(`[data-stage-tool="${tab}"]${editor ? `[data-stage-editor="${editor}"]` : ''}`)
        .click();
      assert.equal(await page.locator('.tab.active').getAttribute('data-tab'), 'workbench');
      await check();
    }
    await page.goto(origin, { waitUntil: 'networkidle' });
    await check('input');
    assert.equal(await page.locator('.workspace-heading.is-compact').count(), 0);
    for (const id of ['elements', 'review', 'refine']) {
      const button = page.locator(`[data-flow-action="${id}"]`);
      assert.equal(await button.isDisabled(), true);
      assert.ok(await button.getAttribute('title'));
    }
    for (const mode of ['examples', 'tools', 'target', 'discover']) {
      await page.locator(`[data-research-mode="${mode}"]`).check();
      await check('input');
      assert.equal(
        await page.locator('[data-research-mode]:checked').getAttribute('data-research-mode'),
        mode
      );
    }
    await page.locator('#research-purpose').fill('先行技術調査（架空データによる画面確認）');
    await page
      .locator('#research-goal')
      .fill('温度を取得するセンサと、温度に応じて冷却量を変える制御部を備える装置。');
    const elements = '温度を取得するセンサ\n温度に応じて冷却量を変える制御部';
    await page.locator('#invention-manual-elements').fill(elements);
    await page.locator('[data-research-input-method="llm"]').click();
    assert.equal(await page.locator('#invention-manual-elements').count(), 0);
    assert.equal(await page.locator('#invention-plan-llm').isDisabled(), true);
    await page.locator('[data-research-input-method="manual"]').click();
    assert.equal(await page.locator('#invention-manual-elements').inputValue(), elements);
    await shot('01-input', 'input');
    await page.locator('#invention-plan-local').click();
    await page.waitForSelector('#invention-elements', { timeout: 60000 });
    await shot('02-elements', 'elements');
    assert.equal(await page.locator('#invention-input-area').count(), 0);
    await stage('input');
    assert.equal(await page.locator('#invention-manual-elements').inputValue(), elements);
    await stage('elements');
    await tool('workbench', 'concepts');
    assert.equal(await page.locator('#research-plan-local').count(), 1);
    await tool('workbench', 'invention');
    assert.equal(await page.locator('#invention-elements').count(), 1);
    await tool('discovery');
    await shot('06-discovery', 'elements');
    await tool('workbench', 'invention');
    await page.locator('#invention-preview').click();
    await page.locator('#invention-apply').click();
    await page.waitForSelector('#csv-file', { state: 'attached' });
    await shot('03-query', 'query');
    assert.equal(await page.locator('.workspace-heading.is-compact').count(), 1);
    const rows = Array.from(
      { length: 8 },
      (_, i) =>
        `JP202600000${i + 1}A,架空の${i < 4 ? '温度センサ冷却制御' : '包装材料の加工'}${i + 1},${i < 4 ? '取得温度に応じて冷却量を変える装置' : '包装フィルムを成形して食品を密封する装置'},${i < 4 ? 'G05D23/00' : 'B65B9/00'}`
    );
    await page.locator('#csv-file').setInputFiles({
      name: '架空特許.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from('公報番号,発明の名称,要約,IPC\n' + rows.join('\n')),
    });
    await page.waitForSelector('#patent-rows tr', { timeout: 60000 });
    for (let i = 0; i < 8; i++) {
      const response = page.waitForResponse(
        (r) => r.url().endsWith('/api/labels') && r.request().method() === 'POST'
      );
      await page
        .locator(`#patent-rows [data-id="JP202600000${i + 1}A"][data-label="${i < 4 ? 'keep' : 'exclude'}"]`)
        .click();
      await response;
    }
    await shot('04-review', 'review');
    assert.doesNotMatch(await page.locator('#projection-status').textContent(), /証明ではなく/);
    assert.match(await page.locator('#projection-method-note').textContent(), /証明ではなく/);
    await page.locator('#map-svg').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, '11-map-detail.png') });
    await page.locator('#patent-rows tr').first().scrollIntoViewIfNeeded();
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    );
    await page.mouse.move(1, 1);
    await page.locator('#patent-rows tr td:nth-child(2)').first().hover();
    await page.waitForSelector('#patent-row-tooltip', { state: 'visible' });
    await page.evaluate(() => scrollTo(0, document.body.scrollHeight));
    const nav = await page.locator('#research-flow-header').boundingBox();
    assert.ok(nav.y >= 0 && nav.y < 5);
    await check('review');
    await tool('agent');
    await shot('07-agent', 'review');
    await stage('refine');
    await shot('05-refine', 'refine');
    await tool('orchestration');
    assert.equal(await page.locator('#orchestration-criteria').inputValue(), '');
    await shot('08-orchestration', 'refine');
    await page.locator('[data-tab="settings"]').click();
    await shot('09-settings');
    const help = page.locator('summary[aria-label="LLM接続のヘルプ"]');
    await help.click();
    assert.ok((await help.locator('..').getAttribute('open')) !== null);
    await shot('10-help');
    await stage('input');
    assert.equal(await page.locator('#invention-manual-elements').inputValue(), elements);
    await page.reload({ waitUntil: 'networkidle' });
    await stage('input');
    assert.equal(await page.locator('#invention-manual-elements').inputValue(), elements);
    assert.deepEqual(errors, []);
    console.log(
      JSON.stringify({
        result: 'PASS',
        viewport: '1440x900',
        screenshots: output,
        isolatedData: temp,
        patents: 8,
        errors,
      })
    );
  } finally {
    if (browser) await browser.close();
    server.kill();
    fs.writeFileSync(path.join(temp, 'server.log'), serverLog);
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
