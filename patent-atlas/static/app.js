import { appState } from './ui-session.js';
import { initSettings, fillSettings } from './settings-panel.js';
import { initCsvImport } from './query-output.js';
import {
  initClassification,
  hideClassTooltip,
  captureClassDraft,
  drawClassNodes,
  updateSelectedClasses,
  renderClassFocus,
  resetClassExplorer,
} from './classification-ui.js';
import { initAgent, render as renderAgent } from './agent-ui.js';
import { debounce, $$, $, api, setState, on, busy, toast, getState } from './core.js';
import * as PatentAtlasMotion from './atlas-motion.js';
import {
  captureResearchDraft,
  render as renderResearchWorkbench,
  initResearchWorkbench,
} from './research-workbench.js';
import { researchDraftFlush, initResearchDraftStore } from './research-draft-store.js';
import { hidePatentRowPreview } from './patent-inspector.js';
import { captureLearningDraft } from './learning-ui.js';
import { captureDiscoveryDraft, render as renderDiscovery } from './discovery-ui.js';
import { captureOrchestrationDraft, render as renderOrchestration } from './orchestration-ui.js';
import { renderStep } from './workflow-ui.js';
import { renderResearchFlowHeader } from './research-guidance.js';
import { updateChrome } from './workspace-chrome.js';
import { refreshJob } from './job-ui.js';
import { counts } from './map-ui.js';

function setTab(tab) {
  if (tab === appState.activeTab) return;
  const previous = appState.activeTab;
  PatentAtlasMotion.cancelAll();
  if (previous === 'workbench') captureResearchDraft();
  researchDraftFlush();
  hideClassTooltip();
  hidePatentRowPreview();
  captureLearningDraft();
  if (appState.activeTab === 'explore' && appState.step === 0) captureClassDraft();
  if (appState.activeTab === 'discovery') captureDiscoveryDraft();
  if (appState.activeTab === 'orchestration') captureOrchestrationDraft();
  appState.activeTab = tab;
  const primary = tab === 'settings' ? 'settings' : 'workbench';
  $$('.tab').forEach((el) => {
    el.classList.toggle('active', el.dataset.tab === primary);
    el.setAttribute?.('aria-selected', String(el.dataset.tab === primary));
  });
  $$('.view').forEach((el) => el.classList.toggle('active', el.id === primary + '-tab'));
  $$('.research-panel').forEach((el) =>
    el.classList.toggle('active', el.id === (tab === 'workbench' ? 'research-workbench' : tab + '-tab'))
  );
  if (tab === 'workbench') renderResearchWorkbench($('#research-workbench'), getState());
  if (tab === 'explore') renderStep();
  if (tab === 'agent') renderAgent($('#agent-tab'), getState());
  if (tab === 'discovery') renderDiscovery($('#discovery-content'), getState());
  if (tab === 'orchestration') renderOrchestration($('#orchestration-content'), getState());
  renderResearchFlowHeader();
  if (previous !== tab) {
    const order = ['explore', 'discovery', 'agent', 'orchestration', 'settings'];
    PatentAtlasMotion.enter($('#' + tab + '-tab'), {
      direction: order.indexOf(tab) - order.indexOf(previous),
      kind: 'tab',
    });
  }
}
function setStep(value) {
  const previous = appState.step;
  PatentAtlasMotion.cancelAll();
  hideClassTooltip();
  hidePatentRowPreview();
  if (appState.step === 0) captureClassDraft();
  appState.step = value;
  appState.picked.clear();
  renderStep();
  if (previous !== value && appState.activeTab === 'explore')
    PatentAtlasMotion.enter($('#step-content'), { direction: value - previous, kind: 'step' });
  const still =
    document.documentElement.dataset.atlasMotion === 'static' ||
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  window.scrollTo({ top: 0, behavior: still ? 'instant' : 'smooth' });
}

async function init() {
  const [initialState, formats] = await Promise.all([api('/state'), api('/query/formats')]);
  setState(initialState);
  appState.formatCatalog = formats;
  initResearchDraftStore();
  fillSettings();
  updateChrome();
  renderStep();
  initResearchWorkbench();
  window.addEventListener(
    'resize',
    debounce(() => {
      if (appState.activeTab === 'explore' && appState.step === 0 && $('#class-svg')) {
        drawClassNodes();
        updateSelectedClasses();
        renderClassFocus();
      }
    }, 120)
  );
  $$('.tab').forEach((el) => el.addEventListener('click', () => setTab(el.dataset.tab)));
  on('#demo-button', 'click', async (e) =>
    busy(e.currentTarget, '準備中…', async () => {
      setState(await api('/demo', {}));
      resetClassExplorer();
      appState.picked.clear();
      setTab('explore');
      setStep(0);
      toast('架空のサンプル72件を読み込みました。Step 02で特許マップを試せます。');
    })
  );
  initSettings();
  initCsvImport();
  initClassification();
  initAgent();
  setInterval(() => {
    if (getState().job.status === 'running' || getState().orchestration?.status === 'running')
      refreshJob().catch((e) => toast(e.message, true));
  }, 1600);
  // Progressive WebMCP support; the normal GUI works without this proposed browser API.
  const context = document.modelContext;
  if (context?.registerTool) {
    try {
      await context.registerTool({
        name: 'read_patent_workspace',
        title: 'Read patent workspace',
        description:
          'Read the current keywords, selected classifications and patent counts. Does not modify data.',
        inputSchema: { type: 'object', properties: {}, additionalProperties: false },
        annotations: { readOnlyHint: true, untrustedContentHint: true },
        execute: async (input) => {
          if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).length)
            throw new Error('No input fields are accepted.');
          return {
            keywords: getState().keywords,
            selected: getState().selected,
            patent_count: getState().patents.length,
            labels: counts(),
          };
        },
      });
    } catch {
      /* Optional browser feature. */
    }
  }
}

export { setTab, setStep, init };
