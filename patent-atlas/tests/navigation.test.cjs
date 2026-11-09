const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loadModule, createDom } = require('./module-harness.cjs');
const html = fs.readFileSync('static/index.html', 'utf8');

function element(id, tab) {
  const classes = new Set();
  return {
    id,
    dataset: { tab },
    attributes: {},
    classes,
    classList: {
      toggle(name, on) {
        if (on) classes.add(name);
        else classes.delete(name);
      },
    },
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
  };
}
async function setup(empty = false) {
  const tabs = [element('', 'workbench'), element('', 'settings')];
  const views = ['workbench-tab', 'settings-tab'].map((id) => element(id));
  const panels = ['research-workbench', 'explore-tab', 'discovery-tab', 'agent-tab', 'orchestration-tab'].map(
    (id) => element(id)
  );
  const hero = element('hero'),
    host = { innerHTML: '' };
  const context = {
    state: {
      patents: empty ? [] : [{ id: 'p' }],
      queries: empty ? [] : [{ id: 'q' }],
      research_workbench: { invention: { plan: { elements: [{}] } } },
    },
    activeTab: 'settings',
    step: 0,
    researchBrief: () => ({ entry_mode: 'target' }),
    esc: String,
    $: (selector) =>
      selector === '#research-flow-header' ? host : selector === '.workspace-heading' ? hero : null,
    $$: (selector) => ({ '.tab': tabs, '.view': views, '.research-panel': panels })[selector] || [],
  };
  for (const name of [
    'hideClassTooltip',
    'captureClassDraft',
    'captureDiscoveryDraft',
    'renderResearchWorkbench',
    'renderStep',
    'renderAgent',
    'renderDiscovery',
    'renderOrchestration',
  ])
    context[name] = () => {};
  context.setState = (next) => (context.state = next);
  context.getState = () => context.state;
  (await loadModule(context, 'research-guidance.js', [
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
  ]),
    await loadModule(context, 'app.js', ['setTab']));
  return { context, tabs, views, panels, hero, host };
}

test('index has one persistent navigation and exactly one view per primary tab', () => {
  const document = createDom().window.document;
  assert.equal(document.querySelectorAll('nav').length, 1);
  assert.equal(document.querySelector('nav').id, 'research-flow-header');
  const tabs = [...document.querySelectorAll('button.tab')].map((node) => node.dataset.tab);
  const views = [...document.querySelectorAll('section.view')].map((node) => node.id.replace(/-tab$/, ''));
  assert.deepEqual(tabs.sort(), ['settings', 'workbench']);
  assert.deepEqual(views.sort(), tabs);
  assert.equal(document.querySelector('#research-workspace-shortcuts,.stepper,.research-journey'), null);
});

test('all internal tools select the research tab and one corresponding panel', async () => {
  const s = await setup();
  for (const tab of ['workbench', 'explore', 'discovery', 'agent', 'orchestration', 'settings']) {
    s.context.setTab(tab);
    const primary = tab === 'settings' ? tab : 'workbench';
    assert.deepEqual(
      s.tabs.filter((el) => el.classes.has('active')).map((el) => el.dataset.tab),
      [primary]
    );
    assert.deepEqual(
      s.views.filter((el) => el.classes.has('active')).map((el) => el.id),
      [primary + '-tab']
    );
    assert.deepEqual(
      s.panels.filter((el) => el.classes.has('active')).map((el) => el.id),
      tab === 'settings' ? [] : [tab === 'workbench' ? 'research-workbench' : tab + '-tab']
    );
    assert.equal(s.tabs.find((el) => el.dataset.tab === primary).attributes['aria-selected'], 'true');
  }
});

test('five navigation stages remain rendered on every panel, stage and settings', async () => {
  const s = await setup();
  for (const tab of ['workbench', 'explore', 'discovery', 'agent', 'orchestration', 'settings']) {
    for (let step = 0; step < 4; step++) {
      s.context.activeTab = tab;
      s.context.step = step;
      for (const stage of ['input', 'elements']) {
        s.context.researchFlowStage = stage;
        s.context.renderResearchFlowHeader();
        assert.equal((s.host.innerHTML.match(/data-flow-action=/g) || []).length, 5);
        assert.doesNotMatch(s.host.innerHTML, /<details|<nav/);
      }
    }
  }
});

test('unavailable stages expose their reason and hero depends only on patent/query counts', async () => {
  const s = await setup(true);
  s.context.state.research_workbench = {};
  s.context.renderResearchFlowHeader();
  for (const id of ['elements', 'review', 'refine'])
    assert.match(s.host.innerHTML, new RegExp(`data-flow-action="${id}" disabled title="[^"]+"`));
  assert.equal(s.hero.classes.has('is-compact'), false);
  for (const key of ['patents', 'queries']) {
    s.context.state[key] = [{ id: 'test' }];
    s.context.renderResearchFlowHeader();
    assert.equal(s.hero.classes.has('is-compact'), true);
    s.context.state[key] = [];
  }
});

test('retained tools belong to the appropriate stage, including the library editor', async () => {
  const { context: c } = await setup();
  assert.deepEqual(
    Array.from(c.researchFlowTools('elements'), (row) => row[0]),
    ['workbench', 'workbench', 'explore', 'discovery']
  );
  assert.deepEqual(
    Array.from(c.researchFlowTools('review'), (row) => row[0]),
    ['explore', 'agent']
  );
  assert.deepEqual(
    Array.from(c.researchFlowTools('refine'), (row) => row[0]),
    ['explore', 'orchestration']
  );
  c.researchBrief = () => ({ entry_mode: 'examples' });
  assert.equal(c.researchFlowTools('elements')[0][3], 'library');
});
