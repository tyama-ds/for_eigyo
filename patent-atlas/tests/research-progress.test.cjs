const test = require('node:test');
const assert = require('node:assert/strict');

const { loadModule } = require('./module-harness.cjs');

function makeHost() {
  return {
    hidden: true,
    dataset: {},
    renders: 0,
    html: '',
    clock: null,
    set innerHTML(value) {
      this.html = value;
      this.renders++;
      const match = value.match(/data-research-progress-elapsed>([^<]+)</);
      this.clock = match ? { textContent: match[1] } : null;
    },
    get innerHTML() {
      return this.html;
    },
    querySelector(selector) {
      return selector === '[data-research-progress-elapsed]' ? this.clock : null;
    },
  };
}

async function setup() {
  let now = 1000,
    host = makeHost(),
    nextTimer = 0;
  const timers = new Map(),
    cleared = [];
  const context = {
    performance: { now: () => now },
    document: { getElementById: (id) => (id === 'research-progress' ? host : null) },
    setInterval: (callback, delay) => {
      assert.equal(delay, 1000);
      timers.set(++nextTimer, callback);
      return nextTimer;
    },
    clearInterval: (id) => {
      cleared.push(id);
      timers.delete(id);
    },
  };
  context.setState = (next) => (context.state = next);
  context.getState = () => context.state;

  await loadModule(context, 'research-progress.js', [
    'researchProgressIsRunning',
    'researchProgressStart',
    'researchProgressFinish',
    'researchProgressHtml',
    'renderResearchProgress',
  ]);
  return {
    context,
    timers,
    cleared,
    get host() {
      return host;
    },
    advance: (milliseconds) => {
      now += milliseconds;
    },
    tick: () => {
      for (const callback of [...timers.values()]) callback();
    },
    replaceHost: (value) => {
      host = value;
    },
  };
}

const localSettings = { provider: 'local', model: 'local-model', llm_timeout: 120 };

test('idle is empty and a manual operation does not expose LLM configuration', async () => {
  const s = await setup(),
    c = s.context;
  assert.equal(c.researchProgressHtml(), '');
  assert.equal(c.researchProgressIsRunning(), false);
  c.renderResearchProgress();
  assert.equal(s.host.hidden, true);
  c.researchProgressStart({ label: '手入力の整理', useLlm: false, settings: localSettings });
  assert.equal(c.researchProgressIsRunning(), true);
  assert.equal(s.host.hidden, false);
  const html = c.researchProgressHtml();
  assert.match(html, /手動入力の整理/);
  assert.doesNotMatch(html, /local-model|120秒|応答待ち設定/);
  assert.match(html, /入力は保持しています/);
});

test('elapsed time comes from the clock and indeterminate status claims no percent or remaining time', async () => {
  const s = await setup(),
    c = s.context;
  c.researchProgressStart({ label: '発明の要素分解', useLlm: true, settings: localSettings });
  assert.match(c.researchProgressHtml(), /経過 <span data-research-progress-elapsed>0:00/);
  s.advance(78420); // No timer tick: suspended or delayed timers must not undercount.
  const html = c.researchProgressHtml();
  assert.match(html, /data-research-progress-elapsed>1:18/);
  assert.match(html, /120秒／回/);
  assert.match(html, /再試行.*全体の処理時間の上限ではありません/);
  assert.match(html, /<progress[^>]*><\/progress>/);
  assert.doesNotMatch(html, /<progress[^>]*(?:value|max)=|残り|%/);
});

test('timer ticks change only the elapsed node, without recreating the live status', async () => {
  const s = await setup(),
    c = s.context;
  c.researchProgressStart({ useLlm: true, settings: localSettings });
  const rendered = s.host.renders;
  s.advance(61000);
  s.tick();
  assert.equal(s.host.clock.textContent, '1:01');
  assert.equal(s.host.renders, rendered);
  assert.match(s.host.innerHTML, /role="status" aria-live="polite"/);
  assert.match(s.host.innerHTML, /aria-live="off">経過/);
});

test('rerendering into a new container retains the same elapsed state and one timer', async () => {
  const s = await setup(),
    c = s.context;
  const token = c.researchProgressStart({ label: '観点の起案', useLlm: true, settings: localSettings });
  s.advance(95000);
  s.replaceHost(makeHost());
  c.renderResearchProgress();
  assert.equal(s.host.clock.textContent, '1:35');
  assert.equal(s.host.dataset.researchProgressToken, String(token));
  assert.equal(s.timers.size, 1);
  c.renderResearchProgress();
  assert.equal(s.timers.size, 1);
  s.advance(1000);
  s.tick();
  assert.equal(s.host.clock.textContent, '1:36');
});

test('a duplicate start cannot replace or restart an in-flight request', async () => {
  const s = await setup(),
    c = s.context;
  const token = c.researchProgressStart({ label: '最初の処理', useLlm: true, settings: localSettings });
  s.advance(40000);
  assert.throws(() => c.researchProgressStart({ label: '後から押した処理' }), /実行中/);
  assert.equal(s.timers.size, 1);
  assert.equal(s.cleared.length, 0);
  assert.match(c.researchProgressHtml(), /最初の処理/);
  assert.doesNotMatch(c.researchProgressHtml(), /後から押した処理/);
  assert.match(c.researchProgressHtml(), /data-research-progress-elapsed>0:40/);
  assert.equal(c.researchProgressFinish(token), true);
});

test('successful completion stops its timer and freezes its elapsed duration', async () => {
  const s = await setup(),
    c = s.context;
  const token = c.researchProgressStart({ label: '観点の起案' });
  s.advance(125000);
  assert.equal(c.researchProgressFinish(token), true);
  assert.equal(c.researchProgressIsRunning(), false);
  assert.equal(s.timers.size, 0);
  s.advance(60000);
  s.tick();
  const html = c.researchProgressHtml();
  assert.match(html, /is-complete/);
  assert.match(html, /data-research-progress-elapsed>2:05/);
  assert.doesNotMatch(html, /<progress/);
  assert.match(html, /作成した案を確認/);
  assert.equal(c.researchProgressFinish(token, new Error('late error')), false);
  assert.match(c.researchProgressHtml(), /is-complete/);
});

test('late or missing tokens cannot finish a later request or clear its timer', async () => {
  const s = await setup(),
    c = s.context;
  const first = c.researchProgressStart({ label: '一つ目' });
  c.researchProgressFinish(first);
  const second = c.researchProgressStart({ label: '二つ目' });
  assert.notEqual(first, second);
  const cleared = s.cleared.length;
  assert.equal(c.researchProgressFinish(first, '古い処理の失敗'), false);
  assert.equal(c.researchProgressFinish(undefined), false);
  assert.equal(s.cleared.length, cleared);
  assert.equal(s.timers.size, 1);
  assert.match(c.researchProgressHtml(), /二つ目/);
  assert.doesNotMatch(c.researchProgressHtml(), /古い処理の失敗/);
  assert.equal(c.researchProgressIsRunning(), true);
});

test('failure shows a persistent escaped reason, retains input guidance, and drops Error metadata', async () => {
  const s = await setup(),
    c = s.context;
  const token = c.researchProgressStart({ useLlm: true, settings: localSettings });
  const error = {
    message: 'モデルが <script>失敗</script>',
    stack: 'secret-stack',
    settings: { api_key: 'secret-key' },
  };
  s.advance(130000);
  assert.equal(c.researchProgressFinish(token, error), true);
  assert.equal(s.timers.size, 0);
  assert.equal(c.researchProgressIsRunning(), false);
  const html = c.researchProgressHtml();
  assert.match(html, /is-error/);
  assert.match(html, /&lt;script&gt;失敗&lt;\/script&gt;/);
  assert.match(html, /入力は保持しています/);
  assert.match(html, /再実行できます/);
  assert.doesNotMatch(html, /<script>|secret-stack|secret-key/);
  s.replaceHost(makeHost());
  c.renderResearchProgress();
  assert.match(s.host.innerHTML, /&lt;script&gt;失敗/);
  assert.doesNotMatch(JSON.stringify(s.context.researchProgressCurrent), /secret-stack|secret-key|api_key/);
});

test('only public display fields are copied from settings, and later changes do not rewrite the operation', async () => {
  const s = await setup(),
    c = s.context;
  const settings = {
    provider: 'openai',
    model: '<img src=x onerror="boom">',
    llm_timeout: 300,
    api_key: 'private-api-key',
    proxy: 'private-proxy',
    ops_secret: 'private-ops-secret',
    base_url: 'private-host',
  };
  const before = JSON.stringify(settings);
  c.researchProgressStart({ label: '<svg onload="boom">', useLlm: true, settings });
  assert.equal(JSON.stringify(settings), before);
  settings.model = 'changed-model';
  settings.llm_timeout = 600;
  const html = c.researchProgressHtml(),
    stored = JSON.stringify(s.context.researchProgressCurrent);
  assert.match(html, /外部LLM/);
  assert.match(html, /300秒／回/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;svg/);
  assert.doesNotMatch(html, /<img|<svg|private-|changed-model|600秒/);
  assert.doesNotMatch(stored, /private-|api_key|proxy|ops_secret|base_url|"settings"/);
});

test('a temporarily absent container does not stop the operation or leak a finished timer', async () => {
  const s = await setup(),
    c = s.context;
  s.replaceHost(null);
  const token = c.researchProgressStart({ useLlm: true, settings: localSettings });
  s.advance(30000);
  assert.doesNotThrow(() => s.tick());
  s.replaceHost(makeHost());
  s.tick();
  assert.equal(s.host.clock.textContent, '0:30');
  s.replaceHost(null);
  assert.equal(c.researchProgressFinish(token, '通信エラー'), true);
  assert.equal(s.timers.size, 0);
  s.replaceHost(makeHost());
  c.renderResearchProgress();
  assert.match(s.host.innerHTML, /通信エラー/);
});

test('invalid wait settings do not become invented timeout values', async () => {
  for (const llm_timeout of [undefined, '120', true, 29, 601, NaN]) {
    const s = await setup(),
      c = s.context;
    c.researchProgressStart({ useLlm: true, settings: { provider: 'local', llm_timeout } });
    const html = c.researchProgressHtml();
    assert.match(html, /応答待ちの上限は接続設定によります/);
    assert.doesNotMatch(html, /\d+秒／回/);
  }
});

test('negative clock movement cannot produce a negative elapsed display', async () => {
  const s = await setup(),
    c = s.context;
  c.researchProgressStart({ label: '時計の検証' });
  s.advance(-5000);
  assert.match(c.researchProgressHtml(), /data-research-progress-elapsed>0:00/);
});
