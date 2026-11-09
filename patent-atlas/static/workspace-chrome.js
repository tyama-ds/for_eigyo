import { $, getState } from './core.js';
import { renderResearchFlowHeader } from './research-guidance.js';

function updateChrome() {
  $('#connection-label').textContent =
    getState().settings.provider === 'offline'
      ? 'オフライン · ローカル保存'
      : getState().settings.provider === 'local'
        ? 'Local LLM 設定済み'
        : '外部API 設定済み';
  $('#footer-info').textContent =
    `${getState().patents.length} 件の特許 · ${getState().queries.length} 件の検索式` +
    (getState().demo ? ' · 架空のサンプルデータ' : '');
  $('#agent-count').textContent = `${getState().patents.length} 件の特許`;
  const j = getState().job,
    running = j.status === 'running';
  $('#job-banner').classList.toggle('hidden', !running);
  $('#job-message').textContent = j.message;
  $('#job-progress').value = j.progress;
  $('#run-agent').disabled = running;
  renderResearchFlowHeader();
}

export { updateChrome };
