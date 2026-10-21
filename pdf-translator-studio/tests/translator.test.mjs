import test from 'node:test';
import assert from 'node:assert/strict';
import { batchBlocks, parseTranslations, parseTranslationResponse, translateBlocks } from '../dist/translator.js';

const config = { provider: 'local', model: 'translation-model', targetLanguage: 'Japanese', maxRetries: 0 };
const blocks = [{ id: 'title', text: 'Hello 🌍' }, { id: 'body', text: 'Second paragraph' }];
const response = (translations, options = {}) => new Response(JSON.stringify({ translations }), { status: 200, ...options });

test('validates exact IDs, maps reordered output, and preserves Unicode and line breaks', () => {
  const parsed = parseTranslations('```json\n' + JSON.stringify({ translations: [{ id: 'body', text: '第２段落\n続き' }, { id: 'title', text: 'こんにちは 🌍' }] }) + '\n```', blocks);
  assert.deepEqual(parsed, { body: '第２段落\n続き', title: 'こんにちは 🌍' });
  assert.deepEqual(parseTranslationResponse({ translations: [{ id: 'a', text: '文字列' }] }, [{ id: 'a' }]), { a: '文字列' });
  assert.deepEqual(parseTranslations('{"translations":[{"id":"__proto__","text":"安全"}]}', [{ id: '__proto__' }]), JSON.parse('{"__proto__":"安全"}'));
});

test('rejects partial, extra, duplicate, empty, malformed, and numerically coerced output', () => {
  const invalid = [
    { translations: [{ id: 'title', text: '題名' }] },
    { translations: [{ id: 'title', text: '題名' }, { id: 'unknown', text: '本文' }] },
    { translations: [{ id: 'title', text: '題名' }, { id: 'title', text: '重複' }] },
    { translations: [{ id: 'title', text: ' ' }, { id: 'body', text: '本文' }] },
    { translations: [{ id: 1, text: '題名' }, { id: 'body', text: '本文' }] },
    { result: '翻訳' },
  ];
  for (const value of invalid) assert.throws(() => parseTranslations(JSON.stringify(value), blocks), { code: 'INVALID_RESPONSE' });
  assert.throws(() => parseTranslations('Here is the result: {}', blocks), { code: 'INVALID_RESPONSE' });
});

test('batches by character and block budget without slicing text or losing surrogate pairs', () => {
  const source = [{ id: 'a', text: '🌍🌍' }, { id: 'b', text: '世界' }, { id: 'c', text: 'A very long item' }, { id: 'd', text: 'd' }];
  assert.deepEqual(batchBlocks(source, { maxBatchCharacters: 5, maxBatchBlocks: 2 }).map((batch) => batch.map((b) => b.id)), [['a'], ['b'], ['c'], ['d']]);
  assert.deepEqual(batchBlocks(source, { maxBatchCharacters: 100, maxBatchBlocks: 2 }).flat(), source);
});

test('local requests default to 8 blocks and 1500 characters while cloud and helper defaults stay unchanged', async () => {
  const source = Array.from({ length: 25 }, (_, i) => ({ id: `block-${i}`, text: 'Text' }));
  const collect = async (input, options = {}) => {
    const requests = [];
    await translateBlocks(input, { ...config, ...options }, {
      fetchImpl: async (_url, options) => {
        const { blocks } = JSON.parse(options.body);requests.push(blocks);
        return response(blocks.map(({ id }) => ({ id, text: '翻訳' })));
      },
    });
    return requests.map(batch => batch.length);
  };
  assert.deepEqual(await collect(source), [8, 8, 8, 1]);
  assert.deepEqual(await collect(source, { provider: 'openai' }), [24, 1]);
  assert.deepEqual(batchBlocks(source).map(batch => batch.length), [24, 1]);
  const longBlocks = source.slice(0, 4).map(block => ({ ...block, text: 'x'.repeat(600) }));
  assert.deepEqual(await collect(longBlocks), [2, 2]);
  assert.deepEqual(await collect(longBlocks, { provider: 'openai' }), [4]);
  assert.deepEqual(await collect(source, { maxBatchBlocks: 12 }), [12, 12, 1]);
  assert.deepEqual(await collect(longBlocks, { maxBatchCharacters: 3000 }), [4]);
});

test('reports the in-flight batch before a slow request completes without extra requests', async () => {
  let resolveRequest;
  let calls = 0;
  const progress = [];
  const pending = translateBlocks(blocks, { ...config, maxBatchBlocks: 1 }, {
    onProgress: event => progress.push(event),
    fetchImpl: async (_url, options) => {
      calls += 1;
      const { blocks } = JSON.parse(options.body);
      if (calls === 1) await new Promise(resolve => { resolveRequest = resolve; });
      return response(blocks.map(({ id }) => ({ id, text: '翻訳' })));
    },
  });
  assert.equal(calls, 1);
  assert.deepEqual(progress.at(-1), { completed: 0, total: 2, batch: 1, totalBatches: 2, phase: 'request' });
  resolveRequest();
  await pending;
  assert.equal(calls, 2);
  assert.deepEqual(progress.find(event => event.phase === 'request' && event.batch === 2), { completed: 1, total: 2, batch: 2, totalBatches: 2, phase: 'request' });
  assert.deepEqual(progress.at(-1), { completed: 2, total: 2, batch: 2, totalBatches: 2 });
});

test('sends only permitted settings to same-origin server, with no browser secrets, and reports full progress', async () => {
  const requests = [];
  const progress = [];
  const source = [...blocks, { id: 'space', text: ' \n' }];
  const result = await translateBlocks(source, { ...config, apiKey: 'must-not-send', endpoint: 'https://must-not-send.invalid', proxy: 'must-not-send', maxBatchBlocks: 1 }, {
    onProgress: (event) => progress.push(event),
    fetchImpl: async (url, options) => {
      requests.push({ url, options });
      const data = JSON.parse(options.body);
      return response(data.blocks.map(({ id, text }) => ({ id, text: `${text} → 翻訳` })));
    },
  });
  assert.deepEqual(result, { space: ' \n', title: 'Hello 🌍 → 翻訳', body: 'Second paragraph → 翻訳' });
  assert.equal(requests.length, 2);
  assert.equal(requests[0].url, '/api/translate-batch');
  assert.equal(requests[0].options.headers.Authorization, undefined);
  assert.equal(requests[0].options.redirect, 'error');
  assert.equal(requests[0].options.credentials, 'same-origin');
  const body = JSON.parse(requests[0].options.body);
  assert.deepEqual(body.blocks, [blocks[0]]);
  assert.deepEqual(body.config, { provider: 'local', model: 'translation-model', sourceLanguage: 'auto', targetLanguage: 'Japanese', apiMode: 'auto' });
  assert.ok(!requests[0].options.body.includes('must-not-send'));
  assert.deepEqual(progress.at(-1), { completed: 3, total: 3, batch: 2, totalBatches: 2 });
});

test('empty text does not call provider and duplicate input IDs fail before sending', async () => {
  const fetchImpl = () => { throw new Error('Must not send'); };
  assert.deepEqual(await translateBlocks([{ id: 'blank', text: '  ' }], {}, { fetchImpl }), { blank: '  ' });
  assert.deepEqual(await translateBlocks([], {}, { fetchImpl }), {});
  await assert.rejects(translateBlocks([{ id: 'a', text: 'A' }, { id: 'a', text: 'B' }], config, { fetchImpl }), { code: 'INVALID_BLOCKS' });
});

test('fails atomically when a later batch is incomplete', async () => {
  let calls = 0;
  await assert.rejects(translateBlocks(blocks, { ...config, maxBatchBlocks: 1 }, {
    fetchImpl: async () => (++calls === 1 ? response([{ id: 'title', text: '題名' }]) : response([])),
  }), { code: 'INVALID_RESPONSE' });
  assert.equal(calls, 2);
});

test('HTTP auth errors give useful Japanese messages and do not retry', async () => {
  let calls = 0;
  await assert.rejects(translateBlocks(blocks, { ...config, maxRetries: 2 }, {
    fetchImpl: async () => { calls += 1; return new Response('{"error":{"message":"invalid key"}}', { status: 401 }); },
  }), (error) => error.code === 'HTTP_ERROR' && error.status === 401 && /API キー/.test(error.message));
  assert.equal(calls, 1);
});

test('server validation failures and timeouts do not trigger duplicate paid requests', async () => {
  for (const status of [500, 502, 504]) {
    let calls = 0;
    await assert.rejects(translateBlocks(blocks, { ...config, maxRetries: 2, retryDelayMs: 0 }, {
      fetchImpl: async () => { calls += 1; return new Response('{"error":"invalid model output"}', { status }); },
    }), { status });
    assert.equal(calls, 1);
  }
});

test('transient errors retry only up to configured bound', async () => {
  let calls = 0;
  const result = await translateBlocks(blocks, { ...config, maxRetries: 1, retryDelayMs: 0 }, {
    fetchImpl: async () => (++calls === 1 ? new Response('{}', { status: 503 }) : response([{ id: 'title', text: '題名' }, { id: 'body', text: '本文' }])),
  });
  assert.equal(calls, 2);
  assert.equal(result.title, '題名');
  calls = 0;
  await assert.rejects(translateBlocks(blocks, { ...config, maxRetries: 1, retryDelayMs: 0 }, {
    fetchImpl: async () => { calls += 1; return new Response('{}', { status: 429, headers: { 'retry-after': '0' } }); },
  }), { status: 429 });
  assert.equal(calls, 2);
});

test('network errors explain server connection failures', async () => {
  await assert.rejects(translateBlocks(blocks, config, { fetchImpl: async () => { throw new TypeError('Failed to fetch'); } }), (error) => error.code === 'NETWORK_ERROR' && /翻訳サーバー/.test(error.message));
});

test('pre-abort sends nothing, and abort cancels a provider that never resolves', async () => {
  const pre = new AbortController();
  pre.abort();
  await assert.rejects(translateBlocks(blocks, config, { signal: pre.signal, fetchImpl: () => { throw new Error('Must not send'); } }), { name: 'AbortError' });
  const controller = new AbortController();
  const pending = translateBlocks(blocks, config, { signal: controller.signal, fetchImpl: () => { queueMicrotask(() => controller.abort()); return new Promise(() => {}); } });
  await assert.rejects(pending, { name: 'AbortError' });
});

test('abort during retry delay stops without another request', async () => {
  const controller = new AbortController();
  let calls = 0;
  await assert.rejects(translateBlocks(blocks, { ...config, maxRetries: 2, retryDelayMs: 1000 }, {
    signal: controller.signal,
    fetchImpl: async () => { calls += 1; setTimeout(() => controller.abort(), 5); return new Response('{}', { status: 503 }); },
  }), { name: 'AbortError' });
  assert.equal(calls, 1);
});

test('server timeout and malformed output are distinguished from cancellation', async () => {
  await assert.rejects(translateBlocks(blocks, { ...config, requestTimeoutMs: 5 }, { fetchImpl: () => new Promise(() => {}) }), { code: 'TIMEOUT' });
  await assert.rejects(translateBlocks(blocks, config, {
    fetchImpl: async () => new Response('{"translations":['),
  }), { code: 'INVALID_RESPONSE' });
});

test('OpenAI API modes and reasoning settings are forwarded without model-specific guessing in the client', async () => {
  let received;
  await translateBlocks([blocks[0]], { ...config, provider: 'openai', model: 'gpt-5', apiMode: 'responses', reasoningEffort: 'low' }, {
    fetchImpl: async (_url, options) => { received = JSON.parse(options.body); return response([{ id: 'title', text: '題名' }]); },
  });
  assert.equal(received.config.provider, 'openai');
  assert.equal(received.config.model, 'gpt-5');
  assert.equal(received.config.apiMode, 'responses');
  assert.equal(received.config.reasoningEffort, 'low');
  await assert.rejects(translateBlocks(blocks, { ...config, provider: 'unsupported' }), { code: 'INVALID_CONFIG' });
  await assert.rejects(translateBlocks(blocks, { ...config, apiMode: 'unsupported' }), { code: 'INVALID_CONFIG' });
});
