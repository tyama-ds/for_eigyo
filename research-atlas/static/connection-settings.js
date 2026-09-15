/* Browser-owned connection preferences. Never written to the server's environment. */
(() => {
  'use strict';
  const STORAGE_KEY = 'research-atlas.connections.v1';
  const HEADER = 'X-Atlas-Connection';
  const $ = selector => document.querySelector(selector);
  const defaults = () => ({version:1,openai:{api_key:'',model:''},local:{backend:'ollama',url:'http://127.0.0.1:11434',model:'',api_key:''},proxy:{enabled:false,url:'',username:'',password:'',no_proxy:'localhost,127.0.0.1,::1'}});
  let current = defaults(), storageError = '', revision = 0, draftVersion = 0, testing = false;
  const copy = value => JSON.parse(JSON.stringify(value));
  function noProxy(value) {
    const invalid = () => {throw new Error('プロキシの除外先はホスト名・IP・CIDR・任意ポートをカンマ区切りで指定してください。URLは指定できません。');};
    const ipv4 = host => /^(?:0|[1-9]\d{0,2})(?:\.(?:0|[1-9]\d{0,2})){3}$/.test(host) && host.split('.').every(part=>Number(part)<=255);
    const ipv6 = host => {
      if (!host.includes(':') || /[^0-9a-f:.]/i.test(host)) return false;
      try {return new URL(`http://[${host}]/`).hostname.startsWith('[');} catch {return false;}
    };
    const ipNetwork = text => {
      const [host,prefix,...rest]=text.split('/');
      if (rest.length || (!ipv4(host) && !ipv6(host))) return false;
      if (prefix === undefined) return true;
      if (/^\d+$/.test(prefix)) return Number(prefix)<=(ipv4(host)?32:128);
      // Python's IPv4 parser also accepts contiguous dotted netmasks / hostmasks.
      if (ipv4(host) && ipv4(prefix)) {const bits=prefix.split('.').map(part=>Number(part).toString(2).padStart(8,'0')).join('');return /^1*0*$/.test(bits)||/^0*1*$/.test(bits);}
      return false;
    };
    const parts=value.split(',').map(part=>part.trim()).filter(Boolean);
    for (const raw of parts) {
      let rule=raw.toLowerCase(),host,port;
      if (rule==='*' || ipNetwork(rule)) continue;
      if (rule.startsWith('[')) {
        const match=rule.match(/^\[([^\]]+)\](?::(\d{1,5}))?$/);
        if (!match || (!ipv4(match[1]) && !ipv6(match[1]))) invalid();
        host=match[1];port=match[2];
      } else {
        if (rule.includes(':')) {const index=rule.lastIndexOf(':');port=rule.slice(index+1);rule=rule.slice(0,index);if(!/^\d+$/.test(port))invalid();}
        host=rule.replace(/^\*\./,'').replace(/^\.+|\.+$/g,'');
        if (!/^[a-z0-9](?:[a-z0-9.\-]{0,251}[a-z0-9])?$/.test(host) || host.includes('..')) invalid();
      }
      if (port!==undefined && !(Number(port)>=1 && Number(port)<=65535)) invalid();
    }
    return parts.join(',');
  }
  function normalize(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || value.version !== 1) throw new Error('保存した接続設定の形式を読み込めません。設定を削除して入力し直してください。');
    const output = defaults();
    for (const section of ['openai','local','proxy']) {
      if (!value[section] || typeof value[section] !== 'object' || Array.isArray(value[section])) throw new Error('接続設定の形式が正しくありません。');
      for (const key of Object.keys(output[section])) {
        const entry = value[section][key];
        if (key === 'enabled') {
          if (typeof entry !== 'boolean') throw new Error('プロキシの有効・無効を確認してください。');
          output[section][key] = entry;
        } else {
          const limit=key==='model'?160:2048;
          if (typeof entry !== 'string' || entry.length > limit || /[\u0000-\u001f\u007f]/.test(entry)) throw new Error(`接続設定の入力が長すぎるか、改行・制御文字が含まれています。モデルは160文字、その他は2048文字以内で指定してください。`);
          output[section][key] = ['password','api_key','username'].includes(key) ? entry : entry.trim();
        }
      }
    }
    if (!['ollama','openai_compatible'].includes(output.local.backend)) throw new Error('ローカルLLMの接続方式を選択してください。');
    for (const section of ['openai','local']) if(output[section].model && !/^[A-Za-z0-9_][A-Za-z0-9_.:/\-]{0,159}$/.test(output[section].model)) throw new Error('モデルIDは半角英数字・アンダースコアで始め、半角英数字・_・.・:・/・-で指定してください。');
    const validateURL = (text, label, local = false) => {
      let url;
      try { url = new URL(text); } catch { throw new Error(`${label}のURLを確認してください。`); }
      if (!['http:','https:'].includes(url.protocol) || url.username || url.password || /[?#\\\s]/.test(text) || url.port==='0') throw new Error(`${label}は認証情報・クエリ・フラグメントを含まないHTTP(S) URLと有効なポートを指定してください。`);
      const host=url.hostname.replace(/\.$/,'');
      if (!host.startsWith('[') && (host.length>253 || !host.split('.').every(label=>/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label)))) throw new Error(`${label}のIPアドレス・ホスト名を確認してください。`);
      if (!local && url.pathname !== '/') throw new Error('プロキシURLはホストとポートまでを指定してください。');
      const canonical=url.href.replace(/\/+$/,'');
      if (canonical.length>2048) throw new Error(`${label}のURLが長すぎます。短いURLを指定してください。`);
      return canonical;
    };
    output.local.url=validateURL(output.local.url, 'ローカルLLM', true);
    if (output.proxy.enabled || output.proxy.url) output.proxy.url=validateURL(output.proxy.url, 'プロキシ');
    output.proxy.no_proxy=noProxy(output.proxy.no_proxy);
    if (output.proxy.password && !output.proxy.username) throw new Error('プロキシのパスワードを使う場合はユーザー名も入力してください。');
    encode(output);
    return output;
  }
  function encode(value) {
    const bytes = new TextEncoder().encode(JSON.stringify(value));
    const encoded = btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''));
    if (encoded.length > 16384) throw new Error('接続設定の合計サイズが上限を超えています。入力を短くしてください。');
    return encoded;
  }
  function load() {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      current = saved === null ? defaults() : normalize(JSON.parse(saved));
      storageError = '';
    } catch {
      current = defaults();
      storageError = 'ブラウザの保存設定を読み込めません。保存領域が無効か、データの形式が壊れています。接続設定を削除するか、ブラウザの保存許可を確認してください。';
    }
  }
  function announce(reason) {
    ++revision;
    window.dispatchEvent(new CustomEvent('atlas:connections-changed', {detail:{revision,reason}}));
    updateSummary();
  }
  function save(value) {
    const next = normalize(value);
    try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); }
    catch { throw new Error('ブラウザに保存できませんでした。保存領域の空き容量・保存許可を確認してください。設定は変更されていません。'); }
    current = next; storageError = ''; announce('save');
    return copy(current);
  }
  function clear() {
    try { window.localStorage.removeItem(STORAGE_KEY); }
    catch { throw new Error('ブラウザの保存設定を削除できませんでした。ブラウザの保存許可を確認してください。'); }
    current = defaults(); storageError = ''; announce('clear');
  }
  async function connectionFetch(input, options = {}, draft) {
    const raw = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
    const url = new URL(raw, location.href || location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) return window.fetch(input, options);
    const headers = new Headers(options.headers || (typeof input === 'object' ? input.headers : undefined));
    headers.set(HEADER, encode(draft === undefined ? current : normalize(draft)));
    // A redirected API request must not carry browser credentials to another destination.
    return window.fetch(input, {...options, headers, redirect:'error'});
  }
  function updateSummary() {
    const summary = $('#connections-summary');
    if (summary) summary.textContent = storageError ? 'ブラウザ保存を確認してください' : `OpenAI ${current.openai.api_key && current.openai.model ? '設定あり' : '未設定'} · ローカルLLM ${current.local.model ? '設定あり' : 'モデル未設定'} · プロキシ ${current.proxy.enabled ? '有効' : '無効'}`;
  }
  const fields = {
    'connection-openai-key':['openai','api_key'], 'connection-openai-model':['openai','model'],
    'connection-local-backend':['local','backend'], 'connection-local-url':['local','url'],
    'connection-local-model':['local','model'], 'connection-local-key':['local','api_key'],
    'connection-proxy-enabled':['proxy','enabled'], 'connection-proxy-url':['proxy','url'],
    'connection-proxy-username':['proxy','username'], 'connection-proxy-password':['proxy','password'],
    'connection-no-proxy':['proxy','no_proxy']
  };
  function fill(value) {
    for (const [id,[section,key]] of Object.entries(fields)) {
      const element = $(`#${id}`);
      if (element) element[key === 'enabled' ? 'checked' : 'value'] = value[section][key];
    }
    ++draftVersion; testing = false; syncControls();
  }
  function readDraft() {
    const value = defaults();
    for (const [id,[section,key]] of Object.entries(fields)) value[section][key] = $(`#${id}`)[key === 'enabled' ? 'checked' : 'value'];
    return normalize(value);
  }
  function showMessage(text = '', error = false) {
    const node = $('#connections-message');
    if (!node) return;
    node.textContent = text; node.hidden = !text; node.className = error ? 'inline-error connections-message' : 'connections-message';
    node.setAttribute('role', error ? 'alert' : 'status');
  }
  function syncControls() {
    const enabled = Boolean($('#connection-proxy-enabled')?.checked);
    const proxyFields = $('#connection-proxy-fields');
    if (proxyFields) proxyFields.disabled = !enabled;
    document.querySelectorAll('[data-connection-test]').forEach(button => {button.disabled = testing || (button.dataset.connectionTest === 'proxy' && !enabled);});
  }
  function open() {
    const dialog = $('#connections-dialog');
    if (!dialog) return;
    fill(current); showMessage(storageError, Boolean(storageError));
    $('#connections-origin').textContent = location.origin;
    if (!dialog.open) dialog.showModal();
  }
  function close() { ++draftVersion; testing = false; $('#connections-dialog')?.close(); fill(current); }
  async function testConnection(target) {
    if (testing) return;
    let draft;
    try { draft = readDraft(); } catch (error) { showMessage(error.message, true); return; }
    const version = ++draftVersion;
    testing = true; syncControls(); showMessage('接続を確認しています。設定はまだ保存していません。');
    try {
      const response = await connectionFetch('/api/connections/test', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target})}, draft);
      let result;
      try { result = await response.json(); } catch { throw new Error('接続確認の応答を読み込めませんでした。'); }
      if (!response.ok || result.ok === false) throw new Error(typeof result.detail === 'string' ? result.detail : result.message || `接続を確認できませんでした (${response.status})`);
      if (version === draftVersion) showMessage(result.message || '接続を確認できました。利用する場合は「ブラウザに保存」を押してください。');
    } catch (error) {
      if (version === draftVersion) showMessage(error instanceof TypeError ? 'アプリのサーバーに接続できませんでした。起動状態を確認してください。' : error.message, true);
    } finally {
      if (version === draftVersion) {testing = false; syncControls();}
    }
  }
  load();
  document.addEventListener('click', event => {
    const target = event.target.closest('[data-connection-open],[data-connection-close],[data-connection-test],[data-connection-clear]');
    if (!target) return;
    event.preventDefault();
    if (target.hasAttribute('data-connection-open')) open();
    else if (target.hasAttribute('data-connection-close')) close();
    else if (target.hasAttribute('data-connection-clear')) {try {clear(); fill(current); showMessage('このブラウザの保存設定を削除しました。APIキー・プロキシ認証は未設定です。');} catch (error) {showMessage(error.message, true);}}
    else testConnection(target.dataset.connectionTest);
  });
  document.addEventListener('submit', event => {
    if (event.target.id !== 'connections-form') return;
    event.preventDefault();
    try {save(readDraft()); ++draftVersion; testing = false; syncControls(); showMessage('このブラウザに保存しました。再分析せずに接続設定を反映しました。');} catch (error) {showMessage(error.message, true);}
  });
  document.addEventListener('input', event => {
    if (!Object.hasOwn(fields,event.target.id)) return;
    ++draftVersion; testing = false; syncControls(); showMessage('未保存の変更があります。保存すると接続設定に反映されます。');
  });
  document.addEventListener('change', event => {
    if (!Object.hasOwn(fields,event.target.id)) return;
    ++draftVersion; testing = false; syncControls(); showMessage('未保存の変更があります。保存すると接続設定に反映されます。');
  });
  $('#connections-dialog')?.addEventListener('close', () => {++draftVersion; testing = false; fill(current);});
  window.addEventListener('storage', event => {
    if (event.key !== STORAGE_KEY && event.key !== null) return;
    if (event.storageArea && event.storageArea !== window.localStorage) return;
    load(); ++draftVersion; testing = false; announce('storage');
    if ($('#connections-dialog')?.open) {fill(current); showMessage(storageError || '同じブラウザの別タブで変更された接続設定を読み込みました。', Boolean(storageError));}
  });
  updateSummary();
  window.AtlasConnections = Object.freeze({fetch:connectionFetch,open,get:()=>copy(current),get revision(){return revision;}});
  if (window.__ATLAS_UI_TEST__) window.__connectionSettingsTest = {defaults,normalize,encode,save,clear,load,readDraft,fill,close,testConnection,get storageError(){return storageError;}};
})();
