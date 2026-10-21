import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

export const defaultTranslationSettingsPath = fileURLToPath(new URL('./data/translation-settings.json', import.meta.url));
const maxSettingsBytes = 4096;
const bounds = {
  localTimeoutSeconds: [30, 1800], localBatchBlocks: [1, 24],
  localBatchCharacters: [100, 6000], openaiTimeoutSeconds: [30, 1800],
};
const failure = (message, status = 400) => Object.assign(new Error(message), { public: true, status });

export function defaultTranslationSettings(env = process.env) {
  const legacy = Number(env.REQUEST_TIMEOUT_MS);
  const seconds = Number.isInteger(legacy) && legacy >= 1000 && legacy <= 1800000
    ? Math.max(30, Math.min(1800, Math.round(legacy / 1000))) : undefined;
  return {
    localTimeoutSeconds: seconds ?? 600, localBatchBlocks: 4,
    localBatchCharacters: 1000, openaiTimeoutSeconds: seconds ?? 180,
  };
}

function validate(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).length !== Object.keys(bounds).length
      || Object.keys(input).some(key => !Object.hasOwn(bounds, key))) {
    throw failure('翻訳設定の項目を確認してください。');
  }
  const result = {};
  for (const [key, [min, max]] of Object.entries(bounds)) {
    if (!Number.isInteger(input[key]) || input[key] < min || input[key] > max) {
      throw failure('応答待ち時間は30〜1800秒、ローカルLLMの領域数は1〜24、文字数は100〜6000の整数で指定してください。');
    }
    result[key] = input[key];
  }
  return result;
}

/** The settings file is created only by an explicit save. */
export function createTranslationSettingsStore({ filePath = defaultTranslationSettingsPath, env = process.env } = {}) {
  const target = path.resolve(filePath);
  const defaults = defaultTranslationSettings(env);
  let loaded;
  let pending = Promise.resolve();
  async function load() {
    if (!loaded) loaded = (async () => {
      try {
        const bytes = await readFile(target);
        if (bytes.length > maxSettingsBytes) throw new Error('oversized');
        return validate(JSON.parse(bytes.toString('utf8')));
      } catch (error) {
        if (error.code === 'ENOENT') return { ...defaults };
        throw failure('保存済みの翻訳設定を読み込めません。アプリの data/translation-settings.json を確認してください。', 500);
      }
    })();
    return loaded;
  }
  return {
    async get() {
      await pending;
      return { ...await load() };
    },
    save(input) {
      const operation = pending.then(async () => {
        const next = validate(input);
        const temporary = `${target}.${randomUUID()}.tmp`;
        try {
          await mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
          await writeFile(temporary, `${JSON.stringify(next, null, 2)}\n`, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
          await rename(temporary, target);
        } catch {
          await rm(temporary, { force: true }).catch(() => {});
          throw failure('翻訳設定を保存できません。アプリの保存先への書き込み権限を確認してください。', 500);
        }
        loaded = Promise.resolve(next);
        return { ...next };
      });
      pending = operation.catch(() => {});
      return operation;
    },
  };
}
