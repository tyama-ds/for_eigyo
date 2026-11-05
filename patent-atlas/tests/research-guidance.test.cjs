const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loadModule } = require('./module-harness.cjs');
const source = fs.readFileSync('static/research-guidance.js', 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

async function setup({ mode = 'target', targets = [], planMode = 'target', hasPlan = true } = {}) {
  const brief = {
    entry_mode: mode,
    purpose: '先行技術を探す',
    goal: '温度に応じて冷却する',
    keywords: '電池',
    target_ids: [...targets],
    user_aspects: ['温度取得'],
  };
  const plan = hasPlan
    ? { id: 'plan-1', elements: [{ id: 'e1' }], brief: { ...brief, entry_mode: planMode } }
    : null;
  const state = {
    patents: [{ id: 'p1' }],
    queries: [{ id: 'q1' }],
    settings: { provider: 'local' },
    research_workbench: { id: 'wb1', brief: structuredClone(brief), invention: { plan } },
  };
  const calls = [],
    dom = {};
  const context = {
    state,
    activeTab: 'workbench',
    step: 0,
    researchBrief: () => brief,
    inventionPlan: () => plan,
    inventionDraftInput: () => ({ manual_elements: [], invention_text: '' }),
    captureResearchDraft: () => calls.push(['capture']),
    researchDraftFlush: () => calls.push(['flush', brief.entry_mode]),
    setTab: (tab) => {
      calls.push(['tab', tab]);
      context.activeTab = tab;
    },
    setStep: (step) => {
      calls.push(['step', step]);
      context.step = step;
    },
    renderResearchWorkbench: () => calls.push(['render']),
    $: (selector) => dom[selector] || null,
    $$: () => [],
  };
  context.setState = (next) => (context.state = next);
  context.getState = () => context.state;
  await loadModule(context, 'research-guidance.js', [
    'researchFlowActions',
    'researchFlowEntryMode',
    'researchFlowDestination',
    'researchFlowPreflight',
    'researchFlowValidate',
    'researchFlowNavigate',
    'researchFlowTools',
    'renderResearchFlowHeader',
    'researchFlowAfterPlan',
    'applyResearchFlowStage',
    'mountResearchGuidance',
    'researchFlowRevealPreview',
  ]);
  context.applyResearchFlowStage = () => calls.push(['stage', context.researchFlowStage]);
  return {
    context,
    state,
    brief,
    plan,
    calls,
    dom,
  };
}

test('five stages reflect actual data prerequisites and highlight only the visible guided step', async () => {
  const c = (await setup()).context;
  const empty = {
    patents: [],
    queries: [],
    research_workbench: { brief: { entry_mode: 'target' }, invention: { plan: null } },
  };
  const before = JSON.stringify(empty),
    rows = c.researchFlowActions(empty);
  assert.deepEqual(plain(rows.map((row) => row.id)), ['input', 'elements', 'query', 'review', 'refine']);
  assert.deepEqual(plain(rows.filter((row) => row.disabled).map((row) => row.id)), [
    'elements',
    'review',
    'refine',
  ]);
  assert.equal(rows.find((row) => row.id === 'query').disabled, undefined); // CSV can be imported directly.
  assert.deepEqual(plain(rows.filter((row) => row.active).map((row) => row.id)), ['input']);
  assert.equal(JSON.stringify(empty), before);
  const populated = {
    patents: [{ id: 'p' }],
    queries: [{ id: 'q' }],
    research_workbench: { invention: { plan: { elements: [{ id: 'e1' }] } } },
  };
  assert.equal(
    c.researchFlowActions(populated).some((row) => row.disabled),
    false
  );
  assert.deepEqual(
    plain(
      c
        .researchFlowActions(populated, 'elements')
        .filter((row) => row.active)
        .map((row) => row.id)
    ),
    ['elements']
  );
  assert.deepEqual(
    plain(
      c
        .researchFlowActions(populated, 'elements', 'explore', 2)
        .filter((row) => row.active)
        .map((row) => row.id)
    ),
    ['review']
  );
  assert.equal(
    c.researchFlowActions(populated, 'input', 'settings').some((row) => row.active),
    false
  );
  for (const mode of ['examples', 'tools'])
    assert.equal(
      c.researchFlowActions(populated, 'input', 'workbench', 0, mode).find((row) => row.id === 'input')
        .active,
      true
    );
  for (const plan of [null, {}, { elements: [] }]) {
    populated.research_workbench.invention.plan = plan;
    const elementStage = c.researchFlowActions(populated, 'elements').find((row) => row.id === 'elements');
    assert.equal(elementStage.disabled, true);
    assert.equal(elementStage.active, false);
  }
  populated.queries = [];
  assert.equal(c.researchFlowActions(populated).find((row) => row.id === 'refine').disabled, true);
});

test('destinations map to existing routes and reject unknown or inherited object keys', async () => {
  const c = (await setup()).context;
  const expected = {
    input: { tab: 'workbench', stage: 'input' },
    elements: { tab: 'workbench', stage: 'elements' },
    query: { tab: 'explore', step: 1 },
    review: { tab: 'explore', step: 2 },
    refine: { tab: 'explore', step: 3 },
  };
  for (const [action, destination] of Object.entries(expected))
    assert.deepEqual(plain(c.researchFlowDestination(action)), destination);
  for (const action of ['', 'unknown', '__proto__', 'constructor', 'toString', undefined])
    assert.equal(c.researchFlowDestination(action), null);
});

test('preflight requires purpose and real manual elements without requiring LLM configuration', async () => {
  const c = (await setup()).context,
    settings = { provider: 'offline' },
    brief = { purpose: '  ', goal: '説明あり' };
  assert.equal(
    c.researchFlowPreflight(brief, { manual_elements: ['要素'] }, 'manual', settings).field,
    'research-purpose'
  );
  brief.purpose = '先行技術探索';
  for (const manual_elements of [undefined, [], [' ', ''], '要素', [null]]) {
    assert.equal(
      c.researchFlowPreflight(brief, { manual_elements }, 'manual', settings).field,
      'invention-manual-elements'
    );
  }
  const input = { manual_elements: ['温度を検出する'], invention_text: '' },
    before = JSON.stringify({ brief, input, settings });
  assert.equal(c.researchFlowPreflight(brief, input, 'manual', settings), null);
  assert.equal(JSON.stringify({ brief, input, settings }), before);
});

test('LLM decomposition accepts each supported source but not keywords or blank lines alone', async () => {
  const c = (await setup()).context,
    settings = { provider: 'local' };
  const brief = { purpose: '先行技術探索', goal: '', keywords: '電池 冷却', target_ids: [] };
  assert.equal(
    c.researchFlowPreflight(brief, { manual_elements: ['  '] }, 'llm', settings).field,
    'research-goal'
  );
  for (const [patch, input] of [
    [{ goal: '温度で冷却を制御する' }, {}],
    [{}, { invention_text: '温度センサと制御部を備える。' }],
    [{ target_ids: ['JP-TEST'] }, {}],
    [{}, { manual_elements: ['温度センサ'] }],
  ])
    assert.equal(c.researchFlowPreflight({ ...brief, ...patch }, input, 'llm', settings), null);
});

test('unconfigured LLM and an unknown method receive actionable guidance before any request', async () => {
  const c = (await setup()).context,
    brief = { purpose: '先行技術探索', goal: '温度による制御' };
  for (const provider of [undefined, 'offline', '', 'unknown']) {
    const issue = c.researchFlowPreflight(brief, {}, 'llm', { provider });
    assert.equal(issue.field, 'research-connect-llm');
    assert.match(issue.message, /手動/);
  }
  for (const provider of ['local', 'openai'])
    assert.equal(c.researchFlowPreflight(brief, {}, 'llm', { provider }), null);
  assert.equal(
    c.researchFlowPreflight(brief, {}, 'unexpected', { provider: 'local' }).field,
    'research-input-help'
  );
});

test('mode selection remains stable when navigating the shared stages', async () => {
  const c = (await setup()).context;
  for (const entry_mode of ['target', 'discover', 'examples', 'tools'])
    assert.equal(
      c.researchFlowEntryMode({ entry_mode, target_ids: ['seed'] }, { brief: { entry_mode: 'target' } }),
      entry_mode
    );
  assert.equal(c.researchFlowEntryMode({}, { brief: { entry_mode: 'discover' } }), 'discover');
  assert.equal(c.researchFlowEntryMode({ target_ids: ['seed'] }, null), 'target');
  assert.equal(c.researchFlowEntryMode({}, null), 'discover');
});

test('navigation from examples or tools keeps the mode and edits while rendering the chosen stage', async () => {
  for (const mode of ['examples', 'tools']) {
    const s = await setup({ mode, targets: ['seed'] }),
      c = s.context;
    const original = structuredClone(s.brief);
    c.researchFlowNavigate('elements');
    assert.equal(s.brief.entry_mode, mode);
    assert.equal(s.context.researchFlowStage, 'elements');
    assert.equal(s.calls.filter(([name]) => name === 'render').length, 1);
    assert.deepEqual(
      s.calls.find(([name]) => name === 'flush'),
      ['flush', mode]
    );
    assert.deepEqual(s.brief, original);
    assert.deepEqual(s.calls.at(-1), ['render']);
  }
});

test('disabled and unknown navigation leave drafts and workspace untouched, while CSV remains reachable', async () => {
  const s = await setup({ mode: 'target', hasPlan: false }),
    c = s.context;
  s.state.patents = [];
  s.state.queries = [];
  const before = JSON.stringify({ state: s.state, brief: s.brief });
  for (const action of ['elements', 'review', 'refine', 'unknown']) c.researchFlowNavigate(action);
  assert.deepEqual(s.calls, []);
  assert.equal(JSON.stringify({ state: s.state, brief: s.brief }), before);
  c.researchFlowNavigate('query');
  assert.deepEqual(s.calls.slice(-2), [
    ['tab', 'explore'],
    ['step', 1],
  ]);
  assert.equal(JSON.stringify({ state: s.state, brief: s.brief }), before);
});

test('validation reveals the matching input method and focuses the missing field without clearing input', async () => {
  const s = await setup(),
    c = s.context,
    input = { manual_elements: [], invention_text: '保持する原文' };
  c.inventionDraftInput = () => input;
  const original = JSON.stringify({ brief: s.brief, input });
  s.dom['#invention-manual-elements'] = {
    focus: () => s.calls.push(['focus']),
    scrollIntoView: () => s.calls.push(['scroll']),
  };
  (() => {
    s.context.researchFlowStage = 'elements';
    s.context.researchFlowInputMethod = 'llm';
  })();
  assert.throws(() => c.researchFlowValidate(false), /要素を1行/);
  assert.equal(s.context.researchFlowStage, 'input');
  assert.equal(s.context.researchFlowInputMethod, 'manual');
  assert.deepEqual(s.calls, [['render'], ['focus'], ['scroll']]);
  assert.equal(JSON.stringify({ brief: s.brief, input }), original);
});

test('stage markup is created directly and render does not wrap or relocate existing nodes', () => {
  assert.doesNotMatch(source, /createElement|insertAdjacentHTML|replaceWith|\.hidden\s*=/);
  const workbench = fs.readFileSync('static/research-workbench.js', 'utf8');
  assert.doesNotMatch(workbench, /research-legacy-plan|research-direct-actions|insertAdjacentHTML/);
  const invention = fs.readFileSync('static/invention-workbench.js', 'utf8');
  assert.match(invention, /function inventionWorkbenchHtml\(stage/);
  assert.match(invention, /stage !== 'elements' \? inputHtml/);
  assert.match(invention, /stage !== 'input' \? reviewHtml/);
  assert.match(invention, /class="research-element-detail"/);
  assert.match(workbench, /class="research-optional research-extra-input"/);
});
