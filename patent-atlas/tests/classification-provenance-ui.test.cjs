const test = require('node:test');
const assert = require('node:assert/strict');

const { loadModule } = require('./module-harness.cjs');
async function setup(patents = []) {
  const context = {
    state: { patents, research_workbench: { brief: { target_ids: [] }, target_patents: [] } },
    structuredClone,
    Set,
    Map,
    esc: (v) =>
      String(v ?? '').replace(
        /[&<>"']/g,
        (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
      ),
  };
  context.setState = (next) => (context.state = next);
  context.getState = () => context.state;

  await loadModule(context, 'research-workbench.js', [
    'researchBrief',
    'captureResearchDraft',
    'captureResearchPlan',
    'syncResearchPlanningControls',
    'researchPlanState',
    'researchGroupOptions',
    'researchLogicHtml',
    'researchTargetOptions',
    'researchPlanHtml',
    'invalidateResearchPreview',
    'researchPlanPayload',
    'renderResearchWorkbench',
    'previewResearchPlan',
    'initResearchWorkbench',
    'researchClassificationProvenance',
    'requestResearchPlan',
  ]);
  return context;
}
test('FI-only records say the imported IPC field is missing without inferring assigned IPC', async () => {
  const rows = [{ id: 'QA1', title: 'Synthetic', ipc: [], fi: 'G05D1/02@N' }],
    before = JSON.stringify(rows),
    c = await setup(rows);
  const html = c.researchTargetOptions();
  assert.match(html, /公報IPC：未収録/);
  assert.match(html, /原本にIPCがないという意味ではありません/);
  assert.match(html, /付与IPCは区別/);
  assert.equal(JSON.stringify(rows), before);
});
test('same-code and broader FI candidates are explicitly separate from assigned IPC', async () => {
  const c = await setup();
  const exact = c.researchClassificationProvenance({
    classification_origins: [{ type: 'fi_to_ipc', method: 'exact_base' }],
    origins: [],
  });
  const broad = c.researchClassificationProvenance({
    classification_origins: [{ type: 'fi_to_ipc', method: 'subclass_fallback' }],
  });
  assert.match(exact, /FIからのIPC候補.*付与IPCとは別/);
  assert.doesNotMatch(exact, /上位へ拡張|公報のIPC欄から抽出/);
  assert.match(broad, /上位へ拡張.*付与IPCとは別/);
});
test('actual IPC and F-term correspondence remain distinct even for a merged candidate', async () => {
  const c = await setup(),
    label = c.researchClassificationProvenance({
      classification_origins: [{ type: 'patent', field: 'ipc' }],
      origins: [{ type: 'fterm_theme_to_ipc' }],
    });
  assert.match(label, /公報のIPC欄から抽出/);
  assert.match(label, /Fターム対応範囲.*付与IPCとは別/);
  const html = (await setup([{ id: 'QA2', title: 'Synthetic', ipc: 'G06F16/00' }])).researchTargetOptions();
  assert.match(html, /公報IPC：G06F16\/00/);
  assert.doesNotMatch(html, /未収録/);
});
