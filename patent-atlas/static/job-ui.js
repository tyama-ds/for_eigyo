import { patchRows } from './core.js';
import { judgmentLabel, replaceHtml, getState, $$, api, $, setState, toast } from './core.js';
import { appState } from './ui-session.js';
import { updateChrome } from './workspace-chrome.js';
import { renderConvergence } from './convergence-ui.js';
import { renderDiscovery } from './discovery-ui.js';
import { renderAgent } from './agent-ui.js';
import { renderOrchestrationProgress } from './orchestration-ui.js';
import { updateMapAssessment, trainingHtml, renderPatentRows, updatePicked } from './map-ui.js';
import { renderLearningProgress, captureLearningDraft } from './learning-ui.js';
import { captureClassDraft } from './classification-ui.js';
import { renderStep } from './workflow-ui.js';

function syncRunningLlmJudgments(fresh) {
  const job = fresh.job,
    training = fresh.training,
    previous = getState().training;
  if (
    job?.status !== 'running' ||
    job.kind !== 'training' ||
    job.mode !== 'llm' ||
    job.id !== getState().job?.id ||
    training?.mode !== 'llm' ||
    training.job_id !== job.id
  )
    return false;
  const oldCount = previous?.job_id === job.id ? Number(previous.completed_count) || 0 : 0;
  if (!Number.isFinite(training.completed_count) || training.completed_count <= oldCount) return false;
  // Keep objects held by map listeners and row previews.
  return patchRows(fresh.patents);
}
function updatePatentAssessmentCells() {
  const byId = new Map(getState().patents.map((row) => [row.id, row]));
  for (const node of $$('#patent-rows tr[data-patent-id]')) {
    const row = byId.get(node.dataset.patentId);
    if (!row || node.cells.length < 4) continue;
    node.cells[2].textContent = row.score == null ? '—' : Math.round(row.score * 100);
    node.cells[3].textContent = judgmentLabel(row.label) + (row.label_source === 'agent' ? ' · AI' : '');
  }
}
async function refreshJob() {
  if (appState.jobRefreshPending) return;
  appState.jobRefreshPending = true;
  try {
    const requestedJob = getState().job?.id;
    const fresh = await api('/state'),
      previous = getState().job.status;
    if (
      getState().job?.id !== requestedJob ||
      (previous === 'running' && requestedJob && fresh.job?.id !== requestedJob)
    )
      return;
    if (
      fresh.job?.status === 'running' &&
      fresh.training?.job_id === getState().training?.job_id &&
      fresh.training?.job_id === fresh.job?.id &&
      Number(fresh.training.completed_count) < Number(getState().training?.completed_count)
    )
      return;
    const orchestrationPoll =
      appState.activeTab === 'orchestration' ||
      fresh.job?.kind === 'orchestration' ||
      getState().job?.kind === 'orchestration';
    let judgmentsChanged = syncRunningLlmJudgments(fresh);
    if (orchestrationPoll && fresh.patents) {
      const latest = new Map(fresh.patents.map((row) => [row.id, row]));
      if (
        latest.size === getState().patents.length &&
        getState().patents.every((row) => latest.has(row.id))
      ) {
        for (const row of getState().patents) Object.assign(row, latest.get(row.id));
        judgmentsChanged = true;
      } else getState().patents = fresh.patents;
      for (const field of ['candidates', 'selected', 'classification_view', 'queries', 'clusters'])
        if (fresh[field] !== undefined) getState()[field] = fresh[field];
    }
    getState().job = fresh.job;
    getState().discovery = fresh.discovery;
    getState().orchestration = fresh.orchestration;
    getState().convergence = fresh.convergence;
    getState().search_result = fresh.search_result;
    updateChrome();
    if (appState.activeTab === 'orchestration') renderConvergence();
    if (appState.activeTab === 'discovery') renderDiscovery();
    if (getState().job.status === 'running') {
      getState().agent_log = fresh.agent_log;
      getState().training = fresh.training;
      if (appState.activeTab === 'agent') renderAgent();
      if (appState.activeTab === 'orchestration') renderOrchestrationProgress();
      if (appState.activeTab === 'explore' && appState.step === 2) {
        if (judgmentsChanged) {
          updateMapAssessment();
          updatePatentAssessmentCells();
        }
        renderLearningProgress();
        if ($('#training-results')) replaceHtml($('#training-results'), trainingHtml());
      }
    } else if (previous === 'running' || (appState.shownJob !== getState().job.id && getState().job.id)) {
      appState.shownJob = getState().job.id;
      captureLearningDraft();
      if (appState.activeTab === 'explore' && appState.step === 0) captureClassDraft();
      setState(fresh);
      updateChrome();
      if (appState.activeTab === 'explore' && appState.step === 2) {
        // Keep form values, list filters, hover targets, map position and selection.
        renderLearningProgress();
        replaceHtml($('#training-results'), trainingHtml());
        renderPatentRows();
        updateMapAssessment();
        updatePicked();
      } else if (appState.activeTab === 'explore') renderStep();
      if (appState.activeTab === 'agent') renderAgent();
      if (appState.activeTab === 'discovery') renderDiscovery();
      if (appState.activeTab === 'orchestration') renderOrchestrationProgress();
      if (getState().job.status === 'error') toast(getState().job.error, true);
      else if (getState().job.status === 'cancelled') toast('処理を停止しました。');
      else if (getState().orchestration?.status === 'review')
        toast('統括ワークフローで選択内容を確認してください。');
      else if (getState().orchestration?.status === 'waiting_csv')
        toast('検索式を更新しました。次のCSVを追加できます。');
      else toast('処理が完了しました。');
    }
  } finally {
    appState.jobRefreshPending = false;
  }
}

export { refreshJob };
