// Optional Phase 5 visual audit, invoked through CSS_AUDIT=1 tests/phase4-browser.cjs.
// Baseline static assets and all workspaces are supplied from isolated directories.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

async function route(request) {
  const url = new URL(request.request().url());
  if (process.env.CSS_BASELINE_STATIC && url.pathname.startsWith('/static/')) {
    const name = url.pathname.slice('/static/'.length);
    if (name !== path.basename(name)) throw new Error('Unexpected static asset path');
    return request.fulfill({ path: path.join(process.env.CSS_BASELINE_STATIC, name) });
  }
  return request.continue();
}

async function capture(page, output, name) {
  // Wait for the existing debounced draft save before measuring or capturing the navigation.
  await page.waitForFunction(() => !document.querySelector('[data-draft-state="pending"]'));
  await page.evaluate(() => document.fonts.ready);
  const styles = await page.evaluate(() => {
    const properties = [
      'display',
      'position',
      'color',
      'backgroundColor',
      'backgroundImage',
      'backgroundSize',
      'boxShadow',
      'textShadow',
      'opacity',
      'fontFamily',
      'fontSize',
      'fontWeight',
      'lineHeight',
      'letterSpacing',
      'paddingTop',
      'paddingRight',
      'paddingBottom',
      'paddingLeft',
      'marginTop',
      'marginRight',
      'marginBottom',
      'marginLeft',
      'borderTopWidth',
      'borderRightWidth',
      'borderBottomWidth',
      'borderLeftWidth',
      'borderTopStyle',
      'borderTopColor',
      'borderRightColor',
      'borderBottomColor',
      'borderLeftColor',
      'borderTopLeftRadius',
      'borderTopRightRadius',
      'borderBottomLeftRadius',
      'borderBottomRightRadius',
      'gap',
      'rowGap',
      'columnGap',
      'alignItems',
      'justifyContent',
      'flexDirection',
      'gridTemplateColumns',
      'textAlign',
      'minHeight',
      'maxHeight',
      'width',
      'height',
      'overflowX',
      'overflowY',
      'fill',
      'stroke',
      'outlineColor',
      'outlineWidth',
    ];
    const result = {};
    function visit(el, key) {
      if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') return;
      if (el.getClientRects().length) {
        const style = getComputedStyle(el);
        result[key] = Object.fromEntries(properties.map((p) => [p, style[p]]));
      }
      Array.from(el.children).forEach((child, i) =>
        visit(child, key + '/' + child.tagName.toLowerCase() + ':' + i)
      );
    }
    visit(document.body, 'body');
    return result;
  });
  fs.writeFileSync(path.join(output, name + '.styles.json'), JSON.stringify(styles));
  // Only elapsed time and the asynchronous draft-save message are nondeterministic.
  await page.screenshot({
    path: path.join(output, name + '.png'),
    fullPage: name.startsWith('components-'),
    mask: [page.locator('[data-research-progress-elapsed]'), page.locator('.research-flow-save')],
    maskColor: '#716f7f',
  });
}

async function probes(browser, origin, output) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  await page.route('**/*', (r) => (new URL(r.request().url()).origin === origin ? route(r) : r.abort()));
  const index = fs.readFileSync(
    process.env.CSS_BASELINE_STATIC
      ? path.join(process.env.CSS_BASELINE_STATIC, 'index.html')
      : 'static/index.html',
    'utf8'
  );
  const links = [...index.matchAll(/<link rel="stylesheet"[^>]*>/g)]
    .map((m) => m[0].replace('href="/', 'href="' + origin + '/'))
    .join('\n');
  const icons = process.env.CSS_BASELINE_STATIC ? 'empty-orbit' : 'empty-state-icon';
  const empty = (name) =>
    process.env.CSS_BASELINE_STATIC
      ? name
      : 'empty-state" data-empty="' +
        { 'ql-empty': 'library', 'discovery-empty': 'discovery', 'convergence-empty': 'convergence' }[name];
  await page.setContent(
    `<html data-atlas-motion="static"><head>${links}</head><body><main>
    <div class="panel"><div class="button-row"><button class="button">標準</button><button class="button primary">採用</button><button class="button subtle">詳細</button><button class="button positive">必要</button><button class="button danger">不要</button><button class="button" disabled>停止中</button><span class="pill">3件</span></div><p class="caption">確認用の説明</p><div class="notice">確認用の注意</div></div>
    <div id="research-workbench"><div class="research-brief"><label class="research-target"><input type="checkbox"><span>候補</span></label><details class="research-extra-input" open><label>入力<input value="値"></label></details></div><div class="research-plan"><label class="research-class"><input type="checkbox">分類</label><div class="research-concept"><label class="research-group-choice">結び方<select><option>OR</option></select></label></div></div></div>
    <div class="invention-workbench"><div class="invention-element"><label class="invention-perspective-join">観点<select><option>AND</option></select></label><div class="invention-perspective"><label class="invention-class-option"><input type="checkbox"><span>分類候補</span></label></div></div></div>
    <div class="ql-library"><label class="button ql-file-label">読み込み<input type="file"></label><div class="ql-example-list"><div class="${empty('ql-empty')}"><span>01 → 02 → 03</span><h3>過去の式</h3><p>説明</p></div></div></div>
    <div id="convergence-graph"><div class="${empty('convergence-empty')}"><span>01 — 02 — 03</span><strong>検索を重ねる</strong><p>実測値を表示</p></div></div>
    <div id="discovery-content"><div id="discovery-results"><div class="panel ${empty('discovery-empty')}"><span class="${icons}">◎</span><h3>探索計画</h3><p>CSVで検証</p></div></div></div>
    <div class="panel empty-state"><div class="${icons}">◎</div><h3>未取り込み</h3><p>CSVを選ぶ</p></div>
    <div class="class-revision-note"><p class="class-revision-versions">分類の版</p></div>
    </main></body></html>`,
    { waitUntil: 'networkidle' }
  );
  for (const width of [1440, 900, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await capture(page, output, 'components-' + width);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator('.button.primary').first().hover();
  await capture(page, output, 'components-hover');
  await page.locator('.research-extra-input input').focus();
  await capture(page, output, 'components-focus');
  if (!process.env.CSS_BASELINE_STATIC) {
    for (const selector of [
      '.research-target',
      '.research-class',
      '.invention-class-option',
      '.ql-empty',
      '.llm-training-options',
    ]) {
      const actual = selector === '.ql-empty' ? '.ql-example-list .empty-state' : selector;
      await page.evaluate((selector) => {
        const el =
          document.querySelector(selector) || document.body.appendChild(document.createElement('div'));
        el.classList.add('llm-training-options');
        el.hidden = true;
        if (getComputedStyle(el).display !== 'none') throw new Error('hidden lost: ' + selector);
      }, actual);
    }
    assert.equal(await page.locator('[hidden]:visible').count(), 0);
    await page.evaluate(() => {
      for (const className of ['job-banner', 'button', 'llm-training-options']) {
        const el = document.body.appendChild(document.createElement('div'));
        el.className = className;
        el.style.display = 'flex';
        el.hidden = true;
        if (getComputedStyle(el).display !== 'none') throw new Error('Inline display overrides hidden');
        el.remove();
      }
      const banner = document.body.appendChild(document.createElement('div'));
      banner.className = 'job-banner hidden';
      if (getComputedStyle(banner).display !== 'none') throw new Error('Legacy hidden banner is visible');
      banner.remove();
    });
  }
  await page.close();
}
module.exports = { route, capture, probes };
