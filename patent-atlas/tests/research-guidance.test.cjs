const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/research-guidance.js', 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));

function setup({mode = 'target', targets = [], planMode = 'target', hasPlan = true} = {}) {
  const brief = {entry_mode: mode, purpose: '先行技術を探す', goal: '温度に応じて冷却する', keywords: '電池', target_ids: [...targets], user_aspects: ['温度取得']};
  const plan = hasPlan ? {id: 'plan-1', elements: [{id: 'e1'}], brief: {...brief, entry_mode: planMode}} : null;
  const state = {patents: [{id: 'p1'}], queries: [{id: 'q1'}], settings: {provider: 'local'}, research_workbench: {id: 'wb1', brief: structuredClone(brief), invention: {plan}}};
  const calls = [], dom = {};
  const context = {
    state, activeTab: 'workbench', step: 0,
    researchBrief: () => brief, inventionPlan: () => plan,
    inventionDraftInput: () => ({manual_elements: [], invention_text: ''}),
    captureResearchDraft: () => calls.push(['capture']),
    researchDraftFlush: () => calls.push(['flush', brief.entry_mode]),
    setTab: tab => { calls.push(['tab', tab]); context.activeTab = tab; },
    setStep: step => { calls.push(['step', step]); context.step = step; },
    renderResearchWorkbench: () => calls.push(['render']),
    $: selector => dom[selector] || null,
    $$: () => []
  };
  vm.createContext(context); vm.runInContext(source, context);
  context.applyResearchFlowStage = () => calls.push(['stage', vm.runInContext('researchFlowStage', context)]);
  return {context, state, brief, plan, calls, dom, read: expression => vm.runInContext(expression, context)};
}

test('five stages reflect actual data prerequisites and highlight only the visible guided step', () => {
  const c = setup().context;
  const empty = {patents: [], queries: [], research_workbench: {brief: {entry_mode: 'target'}, invention: {plan: null}}};
  const before = JSON.stringify(empty), rows = c.researchFlowActions(empty);
  assert.deepEqual(plain(rows.map(row => row.id)), ['input', 'elements', 'query', 'review', 'refine']);
  assert.deepEqual(plain(rows.filter(row => row.disabled).map(row => row.id)), ['elements', 'review', 'refine']);
  assert.equal(rows.find(row => row.id === 'query').disabled, undefined); // CSV can be imported directly.
  assert.deepEqual(plain(rows.filter(row => row.active).map(row => row.id)), ['input']);
  assert.equal(JSON.stringify(empty), before);
  const populated = {patents: [{id: 'p'}], queries: [{id: 'q'}], research_workbench: {invention: {plan: {elements: [{id: 'e1'}]}}}};
  assert.equal(c.researchFlowActions(populated).some(row => row.disabled), false);
  assert.deepEqual(plain(c.researchFlowActions(populated, 'elements').filter(row => row.active).map(row => row.id)), ['elements']);
  assert.deepEqual(plain(c.researchFlowActions(populated, 'elements', 'explore', 2).filter(row => row.active).map(row => row.id)), ['review']);
  assert.equal(c.researchFlowActions(populated, 'input', 'settings').some(row => row.active), false);
  for (const mode of ['examples', 'tools']) assert.equal(c.researchFlowActions(populated, 'input', 'workbench', 0, mode).some(row => row.active), false);
  for (const plan of [null, {}, {elements: []}]) {
    populated.research_workbench.invention.plan = plan;
    const elementStage = c.researchFlowActions(populated, 'elements').find(row => row.id === 'elements');
    assert.equal(elementStage.disabled, true); assert.equal(elementStage.active, false);
  }
  populated.queries = [];
  assert.equal(c.researchFlowActions(populated).find(row => row.id === 'refine').disabled, true);
});

test('destinations map to existing routes and reject unknown or inherited object keys', () => {
  const c = setup().context;
  const expected = {input: {tab: 'workbench', stage: 'input'}, elements: {tab: 'workbench', stage: 'elements'},
    query: {tab: 'explore', step: 1}, review: {tab: 'explore', step: 2}, refine: {tab: 'explore', step: 3}};
  for (const [action, destination] of Object.entries(expected)) assert.deepEqual(plain(c.researchFlowDestination(action)), destination);
  for (const action of ['', 'unknown', '__proto__', 'constructor', 'toString', undefined]) assert.equal(c.researchFlowDestination(action), null);
});

test('preflight requires purpose and real manual elements without requiring LLM configuration', () => {
  const c = setup().context, settings = {provider: 'offline'}, brief = {purpose: '  ', goal: '説明あり'};
  assert.equal(c.researchFlowPreflight(brief, {manual_elements: ['要素']}, 'manual', settings).field, 'research-purpose');
  brief.purpose = '先行技術探索';
  for (const manual_elements of [undefined, [], [' ', ''], '要素', [null]]) {
    assert.equal(c.researchFlowPreflight(brief, {manual_elements}, 'manual', settings).field, 'invention-manual-elements');
  }
  const input = {manual_elements: ['温度を検出する'], invention_text: ''}, before = JSON.stringify({brief, input, settings});
  assert.equal(c.researchFlowPreflight(brief, input, 'manual', settings), null);
  assert.equal(JSON.stringify({brief, input, settings}), before);
});

test('LLM decomposition accepts each supported source but not keywords or blank lines alone', () => {
  const c = setup().context, settings = {provider: 'local'};
  const brief = {purpose: '先行技術探索', goal: '', keywords: '電池 冷却', target_ids: []};
  assert.equal(c.researchFlowPreflight(brief, {manual_elements: ['  ']}, 'llm', settings).field, 'research-goal');
  for (const [patch, input] of [
    [{goal: '温度で冷却を制御する'}, {}],
    [{}, {invention_text: '温度センサと制御部を備える。'}],
    [{target_ids: ['JP-TEST']}, {}],
    [{}, {manual_elements: ['温度センサ']}]
  ]) assert.equal(c.researchFlowPreflight({...brief, ...patch}, input, 'llm', settings), null);
});

test('unconfigured LLM and an unknown method receive actionable guidance before any request', () => {
  const c = setup().context, brief = {purpose: '先行技術探索', goal: '温度による制御'};
  for (const provider of [undefined, 'offline', '', 'unknown']) {
    const issue = c.researchFlowPreflight(brief, {}, 'llm', {provider});
    assert.equal(issue.field, 'research-connect-llm'); assert.match(issue.message, /手動/);
  }
  for (const provider of ['local', 'openai']) assert.equal(c.researchFlowPreflight(brief, {}, 'llm', {provider}), null);
  assert.equal(c.researchFlowPreflight(brief, {}, 'unexpected', {provider: 'local'}).field, 'research-input-help');
});

test('returning to guided work preserves the current mode or uses the saved plan before inferring a new one', () => {
  const c = setup().context;
  assert.equal(c.researchFlowEntryMode({entry_mode: 'discover', target_ids: ['seed']}, {brief: {entry_mode: 'target'}}), 'discover');
  assert.equal(c.researchFlowEntryMode({entry_mode: 'target', target_ids: []}, null), 'target');
  assert.equal(c.researchFlowEntryMode({entry_mode: 'examples', target_ids: ['seed']}, {brief: {entry_mode: 'discover'}}), 'discover');
  assert.equal(c.researchFlowEntryMode({entry_mode: 'tools', target_ids: []}, {brief: {entry_mode: 'target'}}), 'target');
  assert.equal(c.researchFlowEntryMode({entry_mode: 'tools', target_ids: ['seed']}, null), 'target');
  assert.equal(c.researchFlowEntryMode({entry_mode: 'examples', target_ids: []}, null), 'discover');
});

test('navigation from examples or tools actually rerenders the guided form and saves the changed mode', () => {
  for (const mode of ['examples', 'tools']) {
    const s = setup({mode, targets: ['seed'], planMode: 'target'}), c = s.context;
    const original = structuredClone(s.brief);
    c.researchFlowNavigate('elements');
    assert.equal(s.brief.entry_mode, 'target'); assert.equal(s.read('researchFlowStage'), 'elements');
    assert.equal(s.calls.filter(([name]) => name === 'render').length, 1);
    assert.deepEqual(s.calls.find(([name]) => name === 'flush'), ['flush', 'target']);
    assert.deepEqual(s.brief, {...original, entry_mode: 'target'});
    assert.deepEqual(s.calls.at(-1), ['stage', 'elements']);
  }
});

test('disabled and unknown navigation leave drafts and workspace untouched, while CSV remains reachable', () => {
  const s = setup({mode: 'tools', hasPlan: false}), c = s.context;
  s.state.patents = []; s.state.queries = [];
  const before = JSON.stringify({state: s.state, brief: s.brief});
  for (const action of ['elements', 'review', 'refine', 'unknown']) c.researchFlowNavigate(action);
  assert.deepEqual(s.calls, []); assert.equal(JSON.stringify({state: s.state, brief: s.brief}), before);
  c.researchFlowNavigate('query');
  assert.deepEqual(s.calls.slice(-2), [['tab', 'explore'], ['step', 1]]);
  assert.equal(JSON.stringify({state: s.state, brief: s.brief}), before);
});

test('validation reveals the matching input method and focuses the missing field without clearing input', () => {
  const s = setup(), c = s.context, input = {manual_elements: [], invention_text: '保持する原文'};
  c.inventionDraftInput = () => input;
  const original = JSON.stringify({brief: s.brief, input});
  s.dom['#invention-manual-elements'] = {focus: () => s.calls.push(['focus']), scrollIntoView: () => s.calls.push(['scroll'])};
  s.read("researchFlowStage='elements'; researchFlowInputMethod='llm'");
  assert.throws(() => c.researchFlowValidate(false), /要素を1行/);
  assert.equal(s.read('researchFlowStage'), 'input'); assert.equal(s.read('researchFlowInputMethod'), 'manual');
  assert.deepEqual(s.calls, [['stage', 'input'], ['focus'], ['scroll']]);
  assert.equal(JSON.stringify({brief: s.brief, input}), original);
});

test('optional details keep the same field node and are not nested or reopened on a second wrap', () => {
  const s = setup(), c = s.context;
  let created = 0;
  function node() {
    const value = {children: [], parentElement: null, className: '', open: false};
    value.classList = {contains: name => value.className.split(/\s+/).includes(name)};
    value.append = child => {
      if (child.parentElement) child.parentElement.children.splice(child.parentElement.children.indexOf(child), 1);
      value.children.push(child); child.parentElement = value;
    };
    value.replaceWith = replacement => {
      const parent = value.parentElement, index = parent.children.indexOf(value);
      parent.children[index] = replacement; replacement.parentElement = parent; value.parentElement = null;
    };
    return value;
  }
  c.document = {createElement: () => { created++; return node(); }};
  const parent = node(), field = node(); field.value = '編集途中の語句'; parent.append(field);
  const first = c.researchWrapDetail(field, '原文を追加', true);
  assert.equal(first.children.at(-1), field); assert.equal(field.value, '編集途中の語句');
  first.open = false;
  const second = c.researchWrapDetail(field, '原文を追加', true);
  assert.equal(second, first); assert.equal(created, 2); assert.equal(second.open, false);
  assert.equal(parent.children.length, 1); assert.equal(parent.children[0].children.at(-1), field);
});
