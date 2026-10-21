import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { createTranslationSettingsStore, defaultTranslationSettings } from '../translation-settings.mjs';
import { createStaticServer } from '../server.mjs';
import { translateBatch } from '../providers.mjs';
import { requestJson } from '../proxy-fetch.mjs';

const defaults = { localTimeoutSeconds: 600, localBatchBlocks: 4, localBatchCharacters: 1000, openaiTimeoutSeconds: 180 };
const saved = { localTimeoutSeconds: 900, localBatchBlocks: 2, localBatchCharacters: 500, openaiTimeoutSeconds: 300 };
async function temporaryStore(t, env = {}) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'pdf-translation-settings-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const filePath = path.join(directory, 'data', 'translation-settings.json');
  return { directory, filePath, store: createTranslationSettingsStore({ filePath, env }) };
}

test('translation settings defaults honor bounded legacy timeout without creating a file', async t => {
  assert.deepEqual(defaultTranslationSettings({}), defaults);
  for (const value of ['invalid', '999', '1800001', '30000.5', '']) assert.deepEqual(defaultTranslationSettings({ REQUEST_TIMEOUT_MS: value }), defaults);
  assert.deepEqual(defaultTranslationSettings({ REQUEST_TIMEOUT_MS: '240500' }), { ...defaults, localTimeoutSeconds: 241, openaiTimeoutSeconds: 241 });
  assert.equal(defaultTranslationSettings({ REQUEST_TIMEOUT_MS: '1000' }).localTimeoutSeconds, 30);
  const { store, filePath } = await temporaryStore(t);
  const first = await store.get();
  assert.deepEqual(first, defaults);
  first.localBatchBlocks = 24;
  assert.deepEqual(await store.get(), defaults);
  await assert.rejects(readFile(filePath), { code: 'ENOENT' });
});

test('settings persist, restore, serialize saves and reject invalid edits atomically', async t => {
  const { store, filePath } = await temporaryStore(t);
  assert.deepEqual(await store.save(saved), saved);
  assert.deepEqual(await createTranslationSettingsStore({ filePath, env: { REQUEST_TIMEOUT_MS: '1000' } }).get(), saved);
  const original = await readFile(filePath, 'utf8');
  for (const invalid of [null, [], {}, { ...saved, extra: 'PRIVATE_VALUE' }, { ...saved, localTimeoutSeconds: 29 }, { ...saved, localTimeoutSeconds: 1801 }, { ...saved, localBatchBlocks: 0 }, { ...saved, localBatchBlocks: 25 }, { ...saved, localBatchCharacters: 99 }, { ...saved, localBatchCharacters: 6001 }, { ...saved, openaiTimeoutSeconds: 29 }, { ...saved, openaiTimeoutSeconds: '180' }, { ...saved, localBatchBlocks: 1.1 }]) {
    await assert.rejects(store.save(invalid), error => error.public && error.status === 400 && !error.message.includes('PRIVATE_VALUE'));
    assert.equal(await readFile(filePath, 'utf8'), original);
  }
  await Promise.all([store.save(defaults), store.save(saved)]);
  assert.deepEqual(await store.get(), saved);
  assert.deepEqual(JSON.parse(await readFile(filePath, 'utf8')), saved);
});

test('corrupt translation settings return a fixed error and an explicit save repairs them', async t => {
  const { store, filePath } = await temporaryStore(t);
  await store.save(saved);
  await writeFile(filePath, '{ PRIVATE_DATA');
  const restarted = createTranslationSettingsStore({ filePath });
  await assert.rejects(restarted.get(), error => error.public && error.status === 500 && !error.message.includes('PRIVATE_DATA'));
  await restarted.save(defaults);
  assert.deepEqual(await restarted.get(), defaults);
});

function request(server, route, method = 'GET', { headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port: server.address().port, path: route, method, headers }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks).toString('utf8') }));
      response.on('error', reject);
    });
    req.on('error', reject);
    req.end(body);
  });
}
async function listen(t, server) {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));
  return server;
}

test('settings API enforces same-origin, methods, body limits and persistence', async t => {
  const { filePath } = await temporaryStore(t);
  const server = await listen(t, createStaticServer({ translationSettingsPath: filePath, env: {} }));
  const route = '/api/translation-settings';
  const headers = { 'content-type': 'application/json' };
  const body = JSON.stringify(saved);
  for (const invalid of [{ origin: 'https://evil.example' }, { origin: 'null' }, { host: 'evil.example' }]) {
    assert.equal((await request(server, route, 'PUT', { headers: { ...headers, ...invalid }, body })).status, 403);
  }
  assert.equal((await request(server, route, 'PUT', { body })).status, 415);
  assert.equal((await request(server, route, 'PUT', { headers, body: '{' })).status, 400);
  assert.equal((await request(server, route, 'PUT', { headers, body: JSON.stringify({ extra: 'x'.repeat(17000) }) })).status, 413);
  assert.equal((await request(server, route, 'POST', { headers, body })).status, 405);
  assert.deepEqual(JSON.parse((await request(server, route)).body), defaults);
  assert.equal((await request(server, route, 'HEAD')).body, '');
  const result = await request(server, route, 'PUT', { headers, body });
  assert.equal(result.status, 200);
  assert.equal(result.headers['cache-control'], 'no-store');
  assert.deepEqual(JSON.parse(result.body), saved);
  assert.deepEqual(JSON.parse((await request(server, route)).body), saved);
  assert.equal((await request(server, '/data/translation-settings.json')).status, 404);
});

test('translation route applies saved timeout, accepts a pinned job timeout and rejects invalid overrides', async t => {
  const { store } = await temporaryStore(t);
  await store.save(saved);
  const calls = [];
  const provider = { translateBatch: (payload, options) => translateBatch(payload, { ...options, request: async (_url, body, options) => {
    calls.push(options.timeoutMs);
    const blocks = JSON.parse(body.messages[1].content).blocks;
    return { choices: [{ finish_reason: 'stop', message: { content: JSON.stringify({ translations: blocks.map(b => ({ id: b.id, text: '翻訳' })) }) } }] };
  } }) };
  const server = await listen(t, createStaticServer({ providers: provider, env: {}, translationSettingsStore: store, proxySettingsStore: { effectiveEnv: async env => env } }));
  const headers = { 'content-type': 'application/json' };
  const payload = { blocks: [{ id: 'one', text: 'Text' }], config: { provider: 'local', model: 'local-model', targetLanguage: '日本語' } };
  const translate = value => request(server, '/api/translate-batch', 'POST', { headers, body: JSON.stringify(value) });
  assert.equal((await translate(payload)).status, 200);
  assert.equal(calls.at(-1), 900000);
  assert.equal((await translate({ ...payload, config: { ...payload.config, providerTimeoutMs: 600000 } })).status, 200);
  assert.equal(calls.at(-1), 600000);
  assert.equal((await translate({ ...payload, config: { ...payload.config, providerTimeoutMs: '600000' } })).status, 400);
  assert.equal(calls.length, 2);
});

test('response deadlines cancel the upstream request and retain status with a fixed public code', async t => {
  let accepted = 0;
  const slow = await listen(t, http.createServer((_request, response) => {
    accepted++;
    setTimeout(() => { if (!response.destroyed) response.end('{"ok":true}'); }, 300);
  }));
  const url = `http://127.0.0.1:${slow.address().port}`;
  let timeout;
  await assert.rejects(requestJson(url, {}, { timeoutMs: 200, useProxy: false }), error => {
    timeout = error;
    return error.status === 504 && error.public && error.code === 'REQUEST_TIMEOUT' && /1秒/.test(error.message) && /応答待ち時間/.test(error.message);
  });
  assert.deepEqual(await requestJson(url, {}, { timeoutMs: 1000, useProxy: false }), { ok: true });
  assert.equal(accepted, 2);
  const { store } = await temporaryStore(t);
  let throwUnknown = false;
  const server = await listen(t, createStaticServer({ env: {}, translationSettingsStore: store, providers: {
    translateBatch: async () => { throw throwUnknown ? Object.assign(new Error('safe message'), { public: true, status: 502, code: 'PRIVATE_UPSTREAM_VALUE' }) : timeout; },
  } }));
  const execute = () => request(server, '/api/translate-batch', 'POST', { headers: { 'content-type': 'application/json' }, body: '{}' });
  const result = await execute();
  assert.equal(result.status, 504);
  assert.equal(JSON.parse(result.body).code, 'REQUEST_TIMEOUT');
  throwUnknown = true;
  assert.ok(!(await execute()).body.includes('PRIVATE_UPSTREAM_VALUE'));
});
