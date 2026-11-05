import { on, toast, busy, getState, $, setState, api } from './core.js';
import { captureClassDraft, describeClassLayout } from './classification-ui.js';
import { renderStep } from './workflow-ui.js';
import { appState } from './ui-session.js';
import { updateChrome } from './workspace-chrome.js';

function fillSettings(host = $('#settings-tab'), snapshot = getState()) {
  const s = snapshot.settings;
  $('#provider').value = s.provider;
  $('#base-url').value = s.base_url;
  $('#llm-model').value = s.model;
  $('#llm-timeout').value = s.llm_timeout ?? 120;
  $('#classification-layout').value = s.classification_layout || 'semantic';
  describeClassLayout();
  $('#ops-status').textContent =
    s.ops_key_set && s.ops_secret_set
      ? 'OPS接続情報をメモリに保持中。実際の接続は検索実行時に確認します。'
      : s.ops_key_set || s.ops_secret_set
        ? 'キーとシークレットを両方設定してください。'
        : '未設定。CSVでの探索はこのまま使えます。';
  $('#ca-bundle').value = s.ca_bundle;
  $('#bypass-local').checked = s.bypass_local;
  $('#transformer-model').value = s.transformer_model;
  $('#allow-download').checked = s.allow_model_download;
  $('#key-status').textContent = s.api_key_set
    ? 'キーをメモリに保持中。空欄のまま保存すると維持します。'
    : 'APIキーは未設定です。';
  $('#proxy-status').textContent = s.proxy_set
    ? 'プロキシ設定をメモリに保持中。'
    : '未設定。LLM通信はプロキシを経由しません。';
}
async function saveSettings() {
  await appState.selectionSave;
  const llmTimeout = Number($('#llm-timeout').value);
  if (!Number.isInteger(llmTimeout) || llmTimeout < 30 || llmTimeout > 600)
    throw new Error('LLM応答の待ち時間は30〜600秒の整数で指定してください。');
  setState(
    await api('/settings', {
      classification_layout: $('#classification-layout').value,
      ops_key: $('#ops-key').value,
      ops_secret: $('#ops-secret').value,
      provider: $('#provider').value,
      base_url: $('#base-url').value,
      model: $('#llm-model').value,
      llm_timeout: llmTimeout,
      api_key: $('#api-key').value,
      proxy: $('#proxy-url').value,
      ca_bundle: $('#ca-bundle').value,
      bypass_local: $('#bypass-local').checked,
      transformer_model: $('#transformer-model').value,
      allow_model_download: $('#allow-download').checked,
    })
  );
  $('#api-key').value = '';
  $('#proxy-url').value = '';
  $('#ops-key').value = '';
  $('#ops-secret').value = '';
  fillSettings();
  updateChrome();
}

export { fillSettings, saveSettings };

export function initSettings() {
  on('#settings-form', 'submit', async (e) => {
    e.preventDefault();
    await saveSettings();
    toast('設定を保存しました。');
  });
  on('#test-connection', 'click', async (e) =>
    busy(e.currentTarget, '接続中…', async () => {
      await saveSettings();
      const r = await api('/settings/test', {});
      toast(r.message);
    })
  );
  on('#classification-layout', 'change', describeClassLayout);
  on('#save-layout', 'click', async (e) =>
    busy(e.currentTarget, '保存中…', async () => {
      await appState.selectionSave;
      const fresh = await api('/settings', { classification_layout: $('#classification-layout').value });
      getState().settings = fresh.settings;
      describeClassLayout();
      if (appState.activeTab === 'explore' && appState.step === 0) {
        captureClassDraft();
        renderStep();
      }
      toast('配置を保存しました。「対話で探索」で確認できます。');
    })
  );
  on('#save-ops', 'click', async (e) =>
    busy(e.currentTarget, '保存中…', async () => {
      const fresh = await api('/settings', {
        ops_key: $('#ops-key').value,
        ops_secret: $('#ops-secret').value,
      });
      getState().settings = fresh.settings;
      $('#ops-key').value = '';
      $('#ops-secret').value = '';
      fillSettings();
      toast('OPS接続情報をメモリに保存しました。');
    })
  );
  on('#clear-ops', 'click', async () => {
    const fresh = await api('/settings', { clear_ops: true });
    getState().settings = fresh.settings;
    $('#ops-key').value = '';
    $('#ops-secret').value = '';
    fillSettings();
    toast('OPS接続情報を消去しました。');
  });
  on('#provider', 'change', (e) => {
    if (e.target.value === 'local') {
      $('#base-url').value = 'http://localhost:11434/v1';
      $('#llm-model').value = 'qwen3:8b';
    } else if (e.target.value === 'openai') {
      $('#base-url').value = 'https://api.openai.com/v1';
      $('#llm-model').value = '';
    }
  });
  on('#clear-secrets', 'click', async () => {
    setState(await api('/settings', { clear_secrets: true }));
    fillSettings();
    toast('APIキーとプロキシ設定を消去しました。');
  });
}

export { fillSettings as render };
