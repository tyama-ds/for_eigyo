import test from 'node:test';
import assert from 'node:assert/strict';
import { buildRequest, getPublicConfig, readResponse, translateBatch } from '../providers.mjs';

const blocks = [{ id: 'heading', text: 'Quarterly report' }, { id: 'note', text: 'Revenue increased by 12%.' }];
const translations = [{ id: 'heading', text: '四半期報告' }, { id: 'note', text: '売上高は12%増加しました。' }];
const environment = { OPENAI_API_KEY: 'test-server-only-key', LOCAL_LLM_MODEL: 'qwen-test' };
const payload = (config = {}) => ({ blocks, config: { provider: 'openai', model: 'gpt-4o', sourceLanguage: 'auto', targetLanguage: '日本語', ...config } });
const chatResponse = (items = translations) => ({ choices: [{ finish_reason: 'stop', message: { content: JSON.stringify({ translations: items }) } }] });
const responsesResponse = (items = translations) => ({ status: 'completed', output: [{ type: 'message', content: [{ type: 'output_text', text: JSON.stringify({ translations: items }) }] }] });
const requestedBlocks = body => JSON.parse(body.messages[1].content).blocks;
const validLocalResponse = body => chatResponse(requestedBlocks(body).map(b => ({ id: b.id, text: `訳: ${b.text}` })));

test('local structured output constrains count and IDs and supports explicit compatibility modes', () => {
  const local = payload({ provider: 'local', model: 'local-test' });
  const spec = buildRequest(local, {});
  const list = spec.body.response_format.json_schema.schema.properties.translations;
  assert.equal(list.minItems, 2);
  assert.equal(list.maxItems, 2);
  assert.deepEqual(list.items.properties.id.enum, ['heading', 'note']);
  assert.equal(spec.body.response_format.json_schema.strict, true);
  assert.equal(buildRequest(local, { LOCAL_LLM_JSON_MODE: 'prompt' }).body.response_format, undefined);
  assert.deepEqual(buildRequest(local, { LOCAL_LLM_JSON_MODE: 'json_object' }).body.response_format, { type: 'json_object' });
  assert.throws(() => buildRequest(local, { LOCAL_LLM_JSON_MODE: 'wrong' }), /LOCAL_LLM_JSON_MODE/);
  assert.equal(buildRequest(payload(), environment).body.response_format.type, 'json_object');
});

test('local auto falls back only on explicit unsupported JSON-format errors', async () => {
  const calls = [];
  const result = await translateBatch(payload({ provider: 'local', model: 'local-test' }), { env: {}, request: async (_url, body) => {
    calls.push(body);
    if (calls.length === 1) throw Object.assign(new Error('unsupported'), { code: 'UNSUPPORTED_RESPONSE_FORMAT' });
    return validLocalResponse(body);
  } });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].response_format.type, 'json_schema');
  assert.equal(calls[1].response_format, undefined);
  assert.equal(result.translations.length, 2);
  for (const mode of ['auto', 'json_schema']) {
    let requests = 0;
    await assert.rejects(translateBatch(payload({ provider: 'local', model: 'local-test' }), { env: { LOCAL_LLM_JSON_MODE: mode }, request: async () => {
      requests++;
      throw Object.assign(new Error('do not retry'), { code: mode === 'auto' ? 'API_AUTHENTICATION' : 'UNSUPPORTED_RESPONSE_FORMAT' });
    } }), /do not retry/);
    assert.equal(requests, 1);
  }
});

test('local missing IDs and truncated JSON are retried as smaller validated sets', async () => {
  for (const kind of ['missing', 'length']) {
    const groups = [];
    const result = await translateBatch(payload({ provider: 'local', model: 'local-test' }), { env: {}, request: async (_url, body) => {
      const input = requestedBlocks(body); groups.push(input.map(b => b.id));
      if (input.length > 1) return kind === 'missing' ? chatResponse([translations[0]]) : { choices: [{ finish_reason: 'length', message: { content: '{' } }] };
      return validLocalResponse(body);
    } });
    assert.deepEqual(groups, [['heading', 'note'], ['heading'], ['note']]);
    assert.deepEqual(result.translations.map(b => b.id), ['heading', 'note']);
    assert.equal(result.model, 'local-test');
  }
});

test('local recovery rejects the entire batch if a smaller set still has missing IDs', async () => {
  let calls = 0;
  await assert.rejects(translateBatch(payload({ provider: 'local', model: 'local-test' }), { env: {}, request: async (_url, body) => {
    calls++;
    const input = requestedBlocks(body);
    return input.length === 1 && input[0].id === 'heading' ? validLocalResponse(body) : chatResponse([]);
  } }), error => error.code === 'INVALID_TRANSLATION_RESPONSE');
  assert.equal(calls, 3);
});

test('local recovery observes cancellation before sending another half', async () => {
  const controller = new AbortController(); let calls = 0;
  await assert.rejects(translateBatch(payload({ provider: 'local', model: 'local-test' }), { env: {}, signal: controller.signal, request: async (_url, body) => {
    calls++;
    if (calls === 1) return chatResponse([]);
    controller.abort();
    return validLocalResponse(body);
  } }), error => error.name === 'AbortError');
  assert.equal(calls, 2);
});

test('local recovery is bounded and OpenAI errors do not start extra inference calls', async () => {
  let localCalls = 0;
  const many = Array.from({ length: 32 }, (_, i) => ({ id: `b${i}`, text: 'text' }));
  await assert.rejects(translateBatch({ ...payload({ provider: 'local', model: 'local-test' }), blocks: many }, { env: {}, request: async () => { localCalls++; return chatResponse([]); } }), error => error.code === 'INVALID_TRANSLATION_RESPONSE');
  assert.equal(localCalls, 4);
  let openaiCalls = 0;
  await assert.rejects(translateBatch(payload(), { env: environment, request: async () => { openaiCalls++; return chatResponse([]); } }), error => error.code === 'INVALID_TRANSLATION_RESPONSE');
  assert.equal(openaiCalls, 1);
});

test('GPT-4o auto uses chat, max_completion_tokens, JSON output, and store false', () => {
  const spec = buildRequest(payload(), { ...environment, OPENAI_MAX_OUTPUT_TOKENS: '4096' });
  assert.equal(spec.apiMode, 'chat');
  assert.equal(spec.url, 'https://api.openai.com/v1/chat/completions');
  assert.equal(spec.body.max_completion_tokens, 4096);
  assert.equal(spec.body.max_tokens, undefined);
  assert.equal(spec.body.max_output_tokens, undefined);
  assert.equal(spec.body.messages[0].role, 'system');
  assert.equal(spec.body.store, false);
  assert.equal(spec.body.response_format.type, 'json_object');
  assert.equal(spec.useProxy, true);
});

test('GPT-5 auto uses Responses with a strict schema and no temperature', () => {
  const spec = buildRequest(payload({ model: 'gpt-5' }), { ...environment, OPENAI_MAX_OUTPUT_TOKENS: '8192' });
  assert.equal(spec.apiMode, 'responses');
  assert.equal(spec.url, 'https://api.openai.com/v1/responses');
  assert.equal(spec.body.max_output_tokens, 8192);
  assert.equal(spec.body.max_tokens, undefined);
  assert.equal(spec.body.max_completion_tokens, undefined);
  assert.equal(spec.body.temperature, undefined);
  assert.equal(spec.body.store, false);
  assert.equal(spec.body.text.format.type, 'json_schema');
  assert.equal(spec.body.text.format.strict, true);
  assert.match(spec.body.instructions, /untrusted document content/);
  assert.deepEqual(JSON.parse(spec.body.input[0].content).blocks, blocks);
});

test('explicit GPT-5 chat uses developer role, max_completion_tokens and requested reasoning effort', () => {
  const spec = buildRequest(payload({ model: 'gpt-5', apiMode: 'chat', reasoningEffort: 'low' }), environment);
  assert.equal(spec.apiMode, 'chat');
  assert.equal(spec.body.messages[0].role, 'developer');
  assert.equal(spec.body.max_completion_tokens, 16384);
  assert.equal(spec.body.reasoning_effort, 'low');
  assert.equal(spec.body.temperature, undefined);
  assert.equal(spec.body.max_tokens, undefined);
  assert.equal(spec.body.store, false);
});

test('later GPT generations use auto Responses without forcing a reasoning level', () => {
  for (const model of ['gpt-6', 'gpt-6.1', 'gpt-6-astra', 'gpt-10', 'o3']) {
    const spec = buildRequest(payload({ model }), environment);
    assert.equal(spec.apiMode, 'responses', model);
    assert.equal(spec.body.reasoning, undefined, model);
    assert.equal(spec.body.reasoning_effort, undefined, model);
    assert.equal(spec.body.temperature, undefined, model);
  }
  const withEffort = buildRequest(payload({ model: 'gpt-5' }), { ...environment, OPENAI_REASONING_EFFORT: 'medium' });
  assert.deepEqual(withEffort.body.reasoning, { effort: 'medium' });
});

test('explicit API mode overrides auto selection and server mode remains configurable', () => {
  assert.equal(buildRequest(payload({ model: 'gpt-4o', apiMode: 'responses' }), environment).apiMode, 'responses');
  assert.equal(buildRequest(payload({ model: 'gpt-5' }), { ...environment, OPENAI_API_MODE: 'chat' }).apiMode, 'chat');
  assert.equal(buildRequest(payload({ model: 'gpt-5', apiMode: 'auto' }), { ...environment, OPENAI_API_MODE: 'chat' }).apiMode, 'responses');
});

test('local LLM uses its own max_tokens, internal HTTP URL, optional auth, and bypasses corporate proxy', () => {
  const spec = buildRequest(payload({ provider: 'local', model: 'intranet-translator' }), {
    LOCAL_LLM_BASE_URL: 'http://llm.internal:8000/v1',
    LOCAL_LLM_API_KEY: 'local-service-key',
    LOCAL_LLM_MAX_TOKENS: '2048',
    OPENAI_MAX_OUTPUT_TOKENS: '9999',
    OPENAI_REASONING_EFFORT: 'high',
    HTTPS_PROXY: 'http://proxy.internal:8080',
  });
  assert.equal(spec.apiMode, 'chat');
  assert.equal(spec.url, 'http://llm.internal:8000/v1/chat/completions');
  assert.equal(spec.body.max_tokens, 2048);
  assert.equal(spec.body.max_completion_tokens, undefined);
  assert.equal(spec.body.reasoning_effort, undefined);
  assert.equal(spec.headers.Authorization, 'Bearer local-service-key');
  assert.equal(spec.useProxy, false);
  assert.equal(buildRequest(payload({ provider: 'local', model: 'qwen-test' }), { LOCAL_LLM_USE_PROXY: 'true' }).useProxy, true);
});

test('OpenAI reasoning environment does not leak into local Responses requests', () => {
  const spec = buildRequest(payload({ provider: 'local', model: 'gpt-5-local', apiMode: 'responses' }), { OPENAI_REASONING_EFFORT: 'high' });
  assert.equal(spec.body.reasoning, undefined);
});

test('provider URLs reject invalid schemes, credentials, query secrets, fragments and plaintext OpenAI', () => {
  for (const value of ['not-a-url', 'file:///tmp/provider', 'ftp://provider.test/v1', 'https://name:secret@provider.test/v1', 'https://provider.test/v1?key=secret', 'https://provider.test/v1#secret', 'http://provider.test/v1']) {
    assert.throws(() => buildRequest(payload(), { ...environment, OPENAI_BASE_URL: value }), (error) => error.public && error.status === 400, value);
  }
  assert.equal(buildRequest(payload(), { ...environment, OPENAI_BASE_URL: 'https://gateway.internal/v1/chat/completions/' }).url, 'https://gateway.internal/v1/chat/completions');
  assert.equal(buildRequest(payload({ model: 'gpt-5' }), { ...environment, OPENAI_BASE_URL: 'https://gateway.internal/v1/responses' }).url, 'https://gateway.internal/v1/responses');
});

test('public configuration strips credentials and query secrets and exposes no API or proxy passwords', () => {
  const publicConfig = getPublicConfig({
    LOCAL_LLM_BASE_URL: 'http://local-user:local-password@llm.internal/v1?token=local-query-secret#hidden',
    LOCAL_LLM_API_KEY: 'local-api-secret',
    OPENAI_BASE_URL: 'https://openai-user:openai-password@gateway.internal/v1?key=openai-query-secret',
    OPENAI_API_KEY: 'openai-api-secret',
    HTTPS_PROXY: 'http://proxy-user:proxy-secret@proxy.internal:8080',
    PROXY_USERNAME: 'credential-user', PROXY_PASSWORD: 'credential-password', NO_PROXY: '.internal,localhost',
  });
  const serialized = JSON.stringify(publicConfig);
  for (const secret of ['local-user', 'local-password', 'local-query-secret', 'local-api-secret', 'openai-user', 'openai-password', 'openai-query-secret', 'openai-api-secret', 'proxy-user', 'proxy-secret', 'credential-user', 'credential-password']) {
    assert.ok(!serialized.includes(secret), secret);
  }
  assert.equal(publicConfig.local.baseUrl, 'http://llm.internal/v1');
  assert.equal(publicConfig.openai.baseUrl, 'https://gateway.internal/v1');
  assert.equal(publicConfig.openai.configured, true);
  assert.equal(publicConfig.proxy.configured, true);
  assert.equal(publicConfig.proxy.noProxy, '.internal,localhost');
  assert.equal(getPublicConfig({}).openai.configured, false);
});

test('input validation rejects duplicate IDs, blank text, oversized batches and invalid settings before requesting', () => {
  const cases = [
    { ...payload(), blocks: [{ id: 'same', text: 'One' }, { id: 'same', text: 'Two' }] },
    { ...payload(), blocks: [{ id: 'a', text: '  ' }] },
    { ...payload(), blocks: [{ id: 1, text: 'Text' }] },
    { ...payload(), blocks: [{ id: 'a', text: 'x'.repeat(30001) }] },
    { ...payload(), blocks: Array.from({ length: 101 }, (_, index) => ({ id: String(index), text: 'x' })) },
    payload({ provider: 'unknown' }), payload({ apiMode: 'unknown' }), payload({ targetLanguage: '' }), payload({ reasoningEffort: 'imaginary' }),
  ];
  for (const invalid of cases) assert.throws(() => buildRequest(invalid, environment), (error) => error.public && error.status === 400);
  assert.throws(() => buildRequest(payload(), {}), /OPENAI_API_KEY/);
});

test('both response formats restore original block order and preserve Unicode', () => {
  const reordered = [translations[1], translations[0]];
  assert.deepEqual(readResponse(chatResponse(reordered), 'chat', blocks), { translations });
  assert.deepEqual(readResponse(responsesResponse(reordered), 'responses', blocks), { translations });
});

test('both response formats reject duplicate, unknown, missing and empty translations', () => {
  const malformed = [
    [translations[0]],
    [translations[0], translations[0]],
    [translations[0], { id: 'other', text: '別の文' }],
    [translations[0], { id: 'note', text: '' }],
  ];
  for (const items of malformed) {
    assert.throws(() => readResponse(chatResponse(items), 'chat', blocks), /IDまたはJSON/);
    assert.throws(() => readResponse(responsesResponse(items), 'responses', blocks), /IDまたはJSON/);
  }
});

test('refusals and incomplete token-limit responses never become a successful translation', () => {
  assert.throws(() => readResponse({ choices: [{ message: { refusal: 'Refused' } }] }, 'chat', blocks), /拒否/);
  assert.throws(() => readResponse({ status: 'completed', output: [{ type: 'message', content: [{ type: 'refusal', refusal: 'Refused' }] }] }, 'responses', blocks), /拒否/);
  assert.throws(() => readResponse({ choices: [{ finish_reason: 'length', message: { content: JSON.stringify({ translations }) } }] }, 'chat', blocks), /未完了/);
  assert.throws(() => readResponse({ status: 'incomplete', incomplete_details: { reason: 'max_output_tokens' }, output: [] }, 'responses', blocks), /未完了/);
});

test('malformed provider containers produce public errors instead of uncaught TypeErrors', () => {
  for (const [data, apiMode] of [[null, 'chat'], [null, 'responses'], [{ output: {} }, 'responses'], [{ output: [{ type: 'message', content: {} }] }, 'responses']]) {
    assert.throws(() => readResponse(data, apiMode, blocks), (error) => error.public === true && error.name !== 'TypeError');
  }
});

test('translateBatch integrates payload construction and validation through an injected request without real credentials', async () => {
  const controller = new AbortController();
  let captured;
  const result = await translateBatch(payload({ model: 'gpt-5' }), {
    env: { ...environment, REQUEST_TIMEOUT_MS: '240000', HTTPS_PROXY: 'http://proxy.internal:8080' },
    signal: controller.signal,
    request: async (...args) => { captured = args; return responsesResponse(); },
  });
  assert.deepEqual(result, { translations, apiMode: 'responses', model: 'gpt-5' });
  const [url, body, options] = captured;
  assert.equal(url, 'https://api.openai.com/v1/responses');
  assert.equal(body.store, false);
  assert.equal(options.headers.Authorization, 'Bearer test-server-only-key');
  assert.equal(options.signal, controller.signal);
  assert.equal(options.useProxy, true);
  assert.equal(options.timeoutMs, 240000);
  assert.ok(!JSON.stringify(result).includes('test-server-only-key'));
});

test('translateBatch retains cancellation and provider failures from the request layer', async () => {
  const aborted = Object.assign(new Error('cancelled'), { name: 'AbortError' });
  await assert.rejects(translateBatch(payload(), { env: environment, request: async () => { throw aborted; } }), (error) => error === aborted);
  let called = false;
  await assert.rejects(translateBatch(payload({ provider: 'unsupported' }), { env: environment, request: async () => { called = true; } }), { status: 400 });
  assert.equal(called, false);
});
