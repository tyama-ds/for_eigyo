const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/index.html', 'utf8');
const app = fs.readFileSync('static/app.js', 'utf8');
const guidance = fs.readFileSync('static/research-guidance.js', 'utf8');

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
function setup(empty = false) {
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
  vm.createContext(context);
  vm.runInContext(
    guidance + '\n' + app.slice(app.indexOf('function setTab('), app.indexOf('function setStep(')),
    context
  );
  return { context, tabs, views, panels, hero, host };
}

test('index has one persistent navigation and exactly one view per primary tab', () => {
  assert.equal((html.match(/<nav\b/g) || []).length, 1);
  assert.match(html, /<nav id="research-flow-header"/);
  const tabs = [...html.matchAll(/<button\b[^>]*class="tab\b[^>]*data-tab="([^"]+)"/g)].map((m) => m[1]);
  const views = [...html.matchAll(/<section\b[^>]*id="([^"]+)-tab"[^>]*class="view\b/g)].map((m) => m[1]);
  assert.deepEqual(tabs.sort(), ['settings', 'workbench']);
  assert.deepEqual(views.sort(), tabs);
  assert.doesNotMatch(html, /research-workspace-shortcuts|class="stepper"|research-journey/);
  assert.doesNotMatch(
    app.slice(app.indexOf('function setStep('), app.indexOf('function updateChrome(')),
    /shortcuts|\.open\s*=/
  );
});

test('all internal tools select the research tab and one corresponding panel', () => {
  const s = setup();
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

test('five navigation stages remain rendered on every panel, stage and settings', () => {
  const s = setup();
  for (const tab of ['workbench', 'explore', 'discovery', 'agent', 'orchestration', 'settings']) {
    for (let step = 0; step < 4; step++) {
      s.context.activeTab = tab;
      s.context.step = step;
      for (const stage of ['input', 'elements']) {
        vm.runInContext(`researchFlowStage='${stage}'`, s.context);
        s.context.renderResearchFlowHeader();
        assert.equal((s.host.innerHTML.match(/data-flow-action=/g) || []).length, 5);
        assert.doesNotMatch(s.host.innerHTML, /<details|<nav/);
      }
    }
  }
});

test('unavailable stages expose their reason and hero depends only on patent/query counts', () => {
  const s = setup(true);
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

test('retained tools belong to the appropriate stage, including the library editor', () => {
  const { context: c } = setup();
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
