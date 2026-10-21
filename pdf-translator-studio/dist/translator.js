/** Browser translation client. Provider credentials and proxy settings stay on the server. */

export class TranslationError extends Error {
  constructor(message, { code = 'TRANSLATION_ERROR', status, retryable = false } = {}) {
    super(message);
    this.name = 'TranslationError';
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

function abortError() {
  return new DOMException('翻訳を中止しました。', 'AbortError');
}

function checkAbort(signal) {
  if (signal?.aborted) throw abortError();
}

function integerOption(value, fallback, minimum, maximum, label) {
  const result = value ?? fallback;
  if (!Number.isInteger(result) || result < minimum || result > maximum) {
    throw new TranslationError(`${label}の設定値が範囲外です。`, { code: 'INVALID_CONFIG' });
  }
  return result;
}

function normalizeBlocks(blocks) {
  if (!Array.isArray(blocks)) {
    throw new TranslationError('翻訳対象のブロックが配列ではありません。', { code: 'INVALID_BLOCKS' });
  }
  const seen = new Set();
  return blocks.map((block) => {
    if (!block || !['string', 'number'].includes(typeof block.id) ||
        (typeof block.id === 'number' && !Number.isFinite(block.id)) || typeof block.text !== 'string') {
      throw new TranslationError('翻訳対象には id と文字列の text が必要です。', { code: 'INVALID_BLOCKS' });
    }
    const id = String(block.id);
    if (!id || seen.has(id)) {
      throw new TranslationError('翻訳対象のブロック ID が空、または重複しています。', { code: 'INVALID_BLOCKS' });
    }
    seen.add(id);
    return { id, text: block.text };
  });
}

/** A long block stays intact, in its own batch; surrogate pairs are never sliced. */
export function batchBlocks(blocks, { maxBatchCharacters = 6000, maxBatchBlocks = 24 } = {}) {
  const charLimit = integerOption(maxBatchCharacters, 6000, 1, 100000, 'バッチ文字数');
  const blockLimit = integerOption(maxBatchBlocks, 24, 1, 200, 'バッチブロック数');
  const batches = [];
  let batch = [];
  let length = 0;
  for (const block of blocks) {
    if (batch.length && (batch.length >= blockLimit || length + block.text.length > charLimit)) {
      batches.push(batch);
      batch = [];
      length = 0;
    }
    batch.push(block);
    length += block.text.length;
  }
  if (batch.length) batches.push(batch);
  return batches;
}

/** Reject partial or mismatched output instead of placing text in the wrong box. */
export function parseTranslations(content, expectedBlocks) {
  const invalid = (detail) => new TranslationError(`翻訳 API の応答を確認できませんでした（${detail}）。JSON 出力に対応したモデルで再試行してください。`, { code: 'INVALID_RESPONSE' });
  if (typeof content !== 'string') throw invalid('本文がありません');
  let source = content.trim();
  const fence = source.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  if (fence) source = fence[1];
  let parsed;
  try {
    parsed = JSON.parse(source);
  } catch {
    throw invalid('JSON ではありません');
  }
  if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.translations)) {
    throw invalid('translations 配列がありません');
  }
  const expected = new Set(expectedBlocks.map((block) => String(block.id)));
  const result = new Map();
  for (const entry of parsed.translations) {
    if (!entry || typeof entry.id !== 'string' || !expected.has(entry.id)) throw invalid('未知のブロック ID');
    if (result.has(entry.id)) throw invalid('ブロック ID が重複');
    if (typeof entry.text !== 'string' || !entry.text.trim()) throw invalid('翻訳文が空です');
    result.set(entry.id, entry.text);
  }
  if (result.size !== expected.size) throw invalid('翻訳が不足しています');
  return Object.fromEntries(result);
}

/** Validate the server's JSON object using the same strict ID correspondence. */
export function parseTranslationResponse(payload, expectedBlocks) {
  return parseTranslations(JSON.stringify(payload), expectedBlocks);
}

function waitWithAbort(milliseconds, signal) {
  checkAbort(signal);
  return new Promise((resolve, reject) => {
    const finish = () => { signal?.removeEventListener('abort', abort); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    const abort = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); reject(abortError()); };
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
  });
}

function withAbort(promise, signal) {
  checkAbort(signal);
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(abortError()); };
    signal.addEventListener('abort', abort, { once: true });
    Promise.resolve(promise).then(
      (value) => { signal.removeEventListener('abort', abort); resolve(value); },
      (error) => { signal.removeEventListener('abort', abort); reject(error); },
    );
    if (signal.aborted) abort();
  });
}

function responseError(status, body) {
  let detail = '';
  try {
    const parsed = JSON.parse(body);
    const message = parsed?.error?.message ?? parsed?.message ?? parsed?.error;
    if (typeof message === 'string') detail = message.replace(/[\u0000-\u001f]/g, ' ').slice(0, 350);
  } catch { /* HTML error pages are not useful to display. */ }
  const hints = {
    400: 'モデル名と API の対応形式を確認してください。',
    401: 'サーバーに設定した API キーを確認してください。',
    403: 'サーバーの API キーの権限を確認してください。',
    404: 'API の URL とモデル名を確認してください。',
    413: '翻訳対象が大きすぎます。バッチ文字数を小さくしてください。',
    429: '利用制限に達しました。時間をおいて再試行してください。',
    504: 'API の応答待ち時間を超えました。接続設定の API 待ち時間を長くするか、1回の翻訳量を減らしてください。',
  };
  return new TranslationError(`翻訳 API が HTTP ${status} を返しました。${hints[status] || 'しばらく待って再試行してください。'}${detail ? ` ${detail}` : ''}`, {
    code: 'HTTP_ERROR', status, retryable: status === 429 || status === 503,
  });
}

async function requestBatch(batch, config, { signal, fetchImpl }) {
  checkAbort(signal);
  const controller = new AbortController();
  const externalAbort = () => controller.abort();
  signal?.addEventListener('abort', externalAbort, { once: true });
  let timedOut = false;
  const timer = config.requestTimeoutMs > 0
    ? setTimeout(() => { timedOut = true; controller.abort(); }, config.requestTimeoutMs)
    : null;
  try {
    const response = await withAbort(fetchImpl('/api/translate-batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
      credentials: 'same-origin', redirect: 'error', referrerPolicy: 'same-origin',
      body: JSON.stringify({
        blocks: batch,
        config: {
          provider: config.provider,
          model: config.model,
          sourceLanguage: config.sourceLanguage,
          targetLanguage: config.targetLanguage,
          apiMode: config.apiMode,
          ...(config.reasoningEffort ? { reasoningEffort: config.reasoningEffort } : {}),
          ...(config.providerTimeoutMs !== undefined ? { providerTimeoutMs: config.providerTimeoutMs } : {}),
        },
      }),
    }), controller.signal);
    const body = await withAbort(response.text(), controller.signal);
    checkAbort(signal);
    if (!response.ok) {
      const error = responseError(response.status, body);
      const retryAfter = response.headers?.get('retry-after');
      if (retryAfter) {
        const seconds = Number(retryAfter);
        const delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(retryAfter) - Date.now();
        if (Number.isFinite(delay)) error.retryAfterMs = Math.min(10000, Math.max(0, delay));
      }
      throw error;
    }
    return parseTranslations(body, batch);
  } catch (error) {
    if (signal?.aborted) throw abortError();
    if (timedOut) throw new TranslationError('翻訳 API が時間内に応答しませんでした。接続先とモデルの稼働状態を確認してください。', { code: 'TIMEOUT' });
    if (error instanceof TranslationError) throw error;
    if (error?.name === 'AbortError') throw abortError();
    throw new TranslationError('アプリの翻訳サーバーに接続できません。サーバーが起動しているか、ネットワーク接続が有効か確認してください。', { code: 'NETWORK_ERROR', retryable: true });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', externalAbort);
  }
}

/**
 * @param {Array<{id: string|number, text: string}>} blocks
 * @param {{provider:'local'|'openai',model:string,sourceLanguage?:string,targetLanguage:string,apiMode?:'auto'|'responses'|'chat',reasoningEffort?:string,providerTimeoutMs?:number}} config
 * @param {{signal?:AbortSignal,onProgress?:Function,onBatch?:Function,fetchImpl?:Function}} options
 * onBatch is an opt-in checkpoint hook for each completely validated API batch.
 * It receives {translations, blocks, completed, total, batch, totalBatches} and is
 * awaited before continuing. Without this hook, failures expose no partial result.
 * @returns {Promise<Record<string,string>>} All translations, keyed by original id.
 */
export async function translateBlocks(blocks, config = {}, { signal, onProgress, onBatch, fetchImpl = globalThis.fetch } = {}) {
  checkAbort(signal);
  const normalized = normalizeBlocks(blocks);
  const pending = normalized.filter((block) => block.text.trim());
  const result = new Map(normalized.filter((block) => !block.text.trim()).map(({ id, text }) => [id, text]));
  if (!pending.length) {
    onProgress?.({ completed: normalized.length, total: normalized.length, batch: 0, totalBatches: 0 });
    return Object.fromEntries(result);
  }
  const settings = {
    provider: config.provider ?? 'local',
    model: String(config.model ?? '').trim(),
    sourceLanguage: String(config.sourceLanguage || 'auto').trim(),
    targetLanguage: String(config.targetLanguage ?? '').trim(),
    apiMode: config.apiMode ?? 'auto',
    reasoningEffort: config.reasoningEffort ? String(config.reasoningEffort) : undefined,
    maxRetries: integerOption(config.maxRetries, 1, 0, 2, '再試行回数'),
    retryDelayMs: integerOption(config.retryDelayMs, 750, 0, 10000, '再試行待機時間'),
    // The server owns provider timeouts; no shorter client timer should trigger a duplicate paid request.
    requestTimeoutMs: integerOption(config.requestTimeoutMs, 0, 0, 900000, 'タイムアウト'),
    providerTimeoutMs: config.providerTimeoutMs === undefined ? undefined
      : integerOption(config.providerTimeoutMs, undefined, 1000, 1800000, 'API 待ち時間'),
  };
  if (!['local', 'openai'].includes(settings.provider)) throw new TranslationError('接続先の種類を選択してください。', { code: 'INVALID_CONFIG' });
  if (!['auto', 'responses', 'chat'].includes(settings.apiMode)) throw new TranslationError('API の形式を選択してください。', { code: 'INVALID_CONFIG' });
  if (!settings.model) throw new TranslationError('接続先で使用できるモデル名を入力してください。', { code: 'INVALID_CONFIG' });
  if (!settings.targetLanguage) throw new TranslationError('翻訳先の言語を選択してください。', { code: 'INVALID_CONFIG' });
  if (typeof fetchImpl !== 'function') throw new TranslationError('このブラウザーは fetch に対応していません。', { code: 'UNSUPPORTED_BROWSER' });
  // Smaller requests keep local models responsive without changing cloud throughput.
  const batches = batchBlocks(pending, {
    maxBatchCharacters: config.maxBatchCharacters ?? (settings.provider === 'local' ? 1000 : 6000),
    maxBatchBlocks: config.maxBatchBlocks ?? (settings.provider === 'local' ? 4 : 24),
  });
  let completed = result.size;
  onProgress?.({ completed, total: normalized.length, batch: 0, totalBatches: batches.length });
  for (let index = 0; index < batches.length; index += 1) {
    const batch = batches[index];
    let translated;
    onProgress?.({ completed, total: normalized.length, batch: index + 1, totalBatches: batches.length, phase: 'request' });
    for (let attempt = 0; ; attempt += 1) {
      checkAbort(signal);
      try {
        translated = await requestBatch(batch, settings, { signal, fetchImpl });
        break;
      } catch (error) {
        if (!error.retryable || attempt >= settings.maxRetries) throw error;
        await waitWithAbort(error.retryAfterMs ?? settings.retryDelayMs * (2 ** attempt), signal);
      }
    }
    checkAbort(signal);
    for (const block of batch) result.set(block.id, translated[block.id]);
    completed += batch.length;
    if (onBatch) {
      await onBatch({
        translations: { ...translated },
        blocks: batch.map(block => ({ ...block })),
        completed, total: normalized.length, batch: index + 1, totalBatches: batches.length,
      });
      checkAbort(signal);
    }
    onProgress?.({ completed, total: normalized.length, batch: index + 1, totalBatches: batches.length });
  }
  checkAbort(signal);
  return Object.fromEntries(result);
}
