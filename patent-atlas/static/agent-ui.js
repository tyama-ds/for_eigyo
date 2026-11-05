import { on, api, toast, busy, setState, replaceHtml, $, getState, esc } from './core.js';
import { setTab, setStep } from './app.js';
import { refreshJob } from './job-ui.js';

function renderAgent(host = $('#agent-tab'), snapshot = getState()) {
  if (!$('#agent-keywords').value) $('#agent-keywords').value = snapshot.keywords;
  replaceHtml(
    $('#agent-log'),
    snapshot.agent_log.length
      ? snapshot.agent_log
          .map(
            (l) => `<div class="log-entry"><time>${esc(l.time)}</time><span>${esc(l.message)}</span></div>`
          )
          .join('')
      : '<div class="empty-state"><div class="empty-state-icon">◎</div><h3>判断の記録はまだありません</h3>' +
          '<p>判定理由と進捗がここに残ります。</p></div>'
  );
  $('#agent-log').scrollTop = $('#agent-log').scrollHeight;
}

export { renderAgent };

export function initAgent() {
  on('#view-agent-map', 'click', () => {
    setTab('explore');
    setStep(2);
  });
  on('#stop-job', 'click', async () => {
    const r = await api('/job/stop', {});
    toast(r.message);
  });
  on('#run-agent', 'click', async (e) =>
    busy(e.currentTarget, '開始しています…', async () => {
      const words = $('#agent-keywords').value.trim();
      if (words !== getState().keywords) {
        setState(await api('/candidates', { keywords: words, use_llm: false }));
      }
      await api('/agent', {
        criteria: $('#agent-criteria').value,
        max_items: Number($('#agent-limit').value),
        threshold: Number($('#agent-threshold').value),
        train: $('#agent-train').checked,
        mode: $('#agent-mode').value,
      });
      await refreshJob();
    })
  );
}

export { renderAgent as render };
