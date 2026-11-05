'use strict';

/* Display only: no request, workspace mutation, or connection-secret storage. */
let researchProgressCurrent = null;
let researchProgressSequence = 0;
let researchProgressTimer = null;

function researchProgressNow() {
  const monotonic = globalThis.performance?.now?.();
  return Number.isFinite(monotonic) ? monotonic : Date.now();
}

function researchProgressEscape(value) {
  return String(value ?? '').replace(
    /[&<>"']/g,
    (character) =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      })[character]
  );
}

function researchProgressIsRunning() {
  return researchProgressCurrent?.status === 'running';
}

function researchProgressElapsed(progress = researchProgressCurrent) {
  if (!progress) return '0:00';
  const end = progress.finishedAt ?? researchProgressNow();
  const seconds = Math.max(0, Math.floor((end - progress.startedAt) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function researchProgressHost() {
  return typeof document === 'undefined' ? null : document.getElementById('research-progress');
}

function researchProgressTick() {
  if (!researchProgressIsRunning()) return;
  const host = researchProgressHost();
  if (!host) return;
  const clock = host.querySelector('[data-research-progress-elapsed]');
  if (host.dataset.researchProgressToken !== String(researchProgressCurrent.token) || !clock) {
    renderResearchProgress();
    return;
  }
  // Keep the live status node intact; screen readers need not announce it each second.
  clock.textContent = researchProgressElapsed();
}

function researchProgressStart({ label, useLlm = false, settings = {} } = {}) {
  if (researchProgressIsRunning()) {
    throw new Error('案の作成を実行中です。現在の処理が終わってから再実行してください。');
  }
  const usesLlm = useLlm === true;
  const provider = usesLlm
    ? settings?.provider === 'local'
      ? 'local'
      : settings?.provider === 'openai'
        ? 'external'
        : 'llm'
    : 'manual';
  const timeout = settings?.llm_timeout;
  researchProgressCurrent = {
    token: ++researchProgressSequence,
    label: typeof label === 'string' && label.trim() ? label.trim().slice(0, 160) : '案の作成',
    provider,
    model: usesLlm && typeof settings?.model === 'string' ? settings.model.trim().slice(0, 160) : '',
    timeout: usesLlm && Number.isInteger(timeout) && timeout >= 30 && timeout <= 600 ? timeout : null,
    status: 'running',
    startedAt: researchProgressNow(),
    finishedAt: null,
    error: '',
  };
  renderResearchProgress();
  if (researchProgressTimer !== null) clearInterval(researchProgressTimer);
  researchProgressTimer = setInterval(researchProgressTick, 1000);
  return researchProgressCurrent.token;
}

function researchProgressFinish(token, error) {
  const progress = researchProgressCurrent;
  if (!progress || progress.token !== token || progress.status !== 'running') return false;
  const failed = error !== undefined && error !== null;
  progress.finishedAt = researchProgressNow();
  progress.status = failed ? 'error' : 'complete';
  // Do not retain an Error object, its stack, or any attached request/settings object.
  progress.error = failed
    ? (typeof error === 'string'
        ? error
        : typeof error?.message === 'string'
          ? error.message
          : '処理を完了できませんでした。'
      ).slice(0, 800)
    : '';
  if (researchProgressTimer !== null) clearInterval(researchProgressTimer);
  researchProgressTimer = null;
  renderResearchProgress();
  return true;
}

function researchProgressHtml() {
  const progress = researchProgressCurrent;
  if (!progress) return '';
  const running = progress.status === 'running';
  const failed = progress.status === 'error';
  const provider = { local: 'Local LLM', external: '外部LLM', llm: 'LLM', manual: '手動入力の整理' }[
    progress.provider
  ];
  const phase = running ? '処理中' : failed ? '完了できませんでした' : '完了';
  const retained = running
    ? '入力は保持しています。応答を受け取ると案を表示します。'
    : failed
      ? '入力は保持しています。入力・接続設定やエラー内容を確認して再実行できます。'
      : '入力は保持しています。作成した案を確認してください。';
  const wait =
    progress.provider === 'manual'
      ? ''
      : progress.timeout === null
        ? '応答待ちの上限は接続設定によります。再試行で全体の処理時間が長くなる場合があります。'
        : `応答待ち設定：${progress.timeout}秒／回。1回の応答待ちの上限です。再試行があるため、全体の処理時間の上限ではありません。`;
  return `<section class="research-progress-card is-${progress.status}" data-research-progress-token="${progress.token}" aria-label="案の作成状況">
    <div class="research-progress-heading" role="status" aria-live="polite" aria-atomic="true"><strong>${researchProgressEscape(progress.label)}</strong><span class="research-progress-phase">${phase}</span></div>
    <div class="research-progress-meta"><span>${provider}${progress.model ? ' · ' + researchProgressEscape(progress.model) : ''}</span><span aria-live="off">経過 <span data-research-progress-elapsed>${researchProgressElapsed(progress)}</span></span></div>
    ${running ? `<progress class="research-progress-indeterminate" aria-label="${progress.provider === 'manual' ? '入力を整理しています' : 'LLMの応答を待っています'}"></progress>` : ''}
    ${failed ? `<p class="research-progress-error">${researchProgressEscape(progress.error || '処理を完了できませんでした。')}</p>` : ''}
    ${wait ? `<p class="caption research-progress-timeout">${wait}</p>` : ''}
    <p class="caption research-progress-retained">${retained}</p>
  </section>`;
}

function renderResearchProgress() {
  const host = researchProgressHost();
  if (!host) return;
  host.hidden = researchProgressCurrent === null;
  host.innerHTML = researchProgressHtml();
  if (researchProgressCurrent) host.dataset.researchProgressToken = String(researchProgressCurrent.token);
  else delete host.dataset.researchProgressToken;
}
