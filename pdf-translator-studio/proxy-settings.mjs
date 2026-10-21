import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

export const defaultProxySettingsPath = fileURLToPath(new URL('./data/proxy-settings.json', import.meta.url));
const maxSettingsBytes = 16 * 1024;
const defaults = { mode: 'environment', url: '', noProxy: '', username: '', password: '', useForLocal: false };
const allowedFields = new Set([...Object.keys(defaults), 'clearPassword', 'passwordConfigured']);
const failure = (message, status = 400) => Object.assign(new Error(message), { public: true, status });

function textField(value, name, maxLength) {
  if (typeof value !== 'string' || value.length > maxLength || /[\u0000-\u001f\u007f]/.test(value)) {
    throw failure(`${name}の形式または長さを確認してください。`);
  }
  return value;
}

function normalizedSettings(input, previous = defaults) {
  if (!input || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).some(key => !allowedFields.has(key))) {
    throw failure('プロキシ設定の項目を確認してください。');
  }
  if (!['environment', 'custom', 'direct'].includes(input.mode)) throw failure('プロキシ接続方式を選択してください。');
  const next = { ...previous, mode: input.mode };
  for (const [field, label, maxLength] of [['url', 'プロキシURL', 2048], ['noProxy', '除外ホスト', 4096], ['username', 'ユーザー名', 256]]) {
    if (Object.hasOwn(input, field)) next[field] = textField(input[field], label, maxLength).trim();
  }
  for (const field of ['useForLocal', 'clearPassword', 'passwordConfigured']) {
    if (Object.hasOwn(input, field) && typeof input[field] !== 'boolean') throw failure('プロキシ設定の選択値を確認してください。');
  }
  if (Object.hasOwn(input, 'useForLocal')) next.useForLocal = input.useForLocal;
  if (Object.hasOwn(input, 'password')) {
    const password = textField(input.password, 'パスワード', 4096);
    if (password && input.clearPassword) throw failure('パスワードの変更と削除は同時に指定できません。');
    // An empty password means keep the saved secret, never erase it accidentally.
    if (password) next.password = password;
  }
  if (input.clearPassword) next.password = '';
  if (next.username.includes(':')) throw failure('ユーザー名にコロンは使用できません。');
  if (next.url) {
    let url;
    try { url = new URL(next.url); } catch { throw failure('プロキシURLは http:// または https:// から入力してください。'); }
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password
        || url.search || url.hash || url.pathname !== '/' || next.url.includes('?') || next.url.includes('#')) {
      throw failure('プロキシURLには HTTP(S) のホスト名とポートだけを入力し、認証情報は専用欄に入力してください。');
    }
    next.url = url.href;
  } else if (next.mode === 'custom') throw failure('プロキシURLを入力してください。');
  return next;
}

function publicSettings(settings) {
  const { mode, url, noProxy, username, useForLocal } = settings;
  return { mode, url, noProxy, username, passwordConfigured: Boolean(settings.password), useForLocal };
}

/** Resolve one immutable request snapshot without changing process.env. */
export function applyProxySettings(env, settings) {
  const result = { ...env };
  if (settings.mode === 'environment') return result;
  for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY', 'PROXY_USERNAME', 'PROXY_PASSWORD', 'LOCAL_LLM_USE_PROXY']) {
    delete result[key];
    delete result[key.toLowerCase()];
  }
  if (settings.mode === 'custom') {
    result.HTTP_PROXY = settings.url;
    result.HTTPS_PROXY = settings.url;
    result.NO_PROXY = settings.noProxy;
    result.PROXY_USERNAME = settings.username;
    result.PROXY_PASSWORD = settings.password;
    result.LOCAL_LLM_USE_PROXY = String(settings.useForLocal);
  }
  return result;
}

/** App-local storage. Secrets are omitted from all browser responses. */
export function createProxySettingsStore({ filePath = defaultProxySettingsPath } = {}) {
  const target = path.resolve(filePath);
  let loaded;
  let pending = Promise.resolve();
  async function load() {
    if (!loaded) loaded = (async () => {
      try {
        const bytes = await readFile(target);
        if (bytes.length > maxSettingsBytes) throw new Error('oversized');
        const parsed = JSON.parse(bytes.toString('utf8'));
        return normalizedSettings(parsed);
      } catch (error) {
        if (error.code === 'ENOENT') return { ...defaults };
        throw failure('保存済みのプロキシ設定を読み込めません。アプリの data/proxy-settings.json を確認してください。', 500);
      }
    })();
    return loaded;
  }
  return {
    async get() {
      await pending;
      return publicSettings(await load());
    },
    async effectiveEnv(env = process.env) {
      await pending;
      return applyProxySettings(env, await load());
    },
    save(input) {
      const operation = pending.then(async () => {
        const next = normalizedSettings(input, await load());
        const body = `${JSON.stringify(next, null, 2)}\n`;
        if (Buffer.byteLength(body) > maxSettingsBytes) throw failure('プロキシ設定が大きすぎます。');
        const temporary = `${target}.${randomUUID()}.tmp`;
        try {
          await mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
          await writeFile(temporary, body, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
          await rename(temporary, target);
        } catch {
          await rm(temporary, { force: true }).catch(() => {});
          throw failure('プロキシ設定を保存できません。アプリの保存先への書き込み権限を確認してください。', 500);
        }
        loaded = Promise.resolve(next);
        return publicSettings(next);
      });
      // Failed saves must not poison later requests or later valid saves.
      pending = operation.catch(() => {});
      return operation;
    },
  };
}
