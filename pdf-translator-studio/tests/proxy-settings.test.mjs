import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { applyProxySettings, createProxySettingsStore } from '../proxy-settings.mjs';
import { createStaticServer } from '../server.mjs';

async function temporaryStore(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'pdf-proxy-settings-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const filePath = path.join(directory, 'data', 'proxy-settings.json');
  return { directory, filePath, store: createProxySettingsStore({ filePath }) };
}
const custom = { mode: 'custom', url: 'http://proxy.test:8080', noProxy: 'localhost,.internal', username: 'office-user', password: 'SYNTHETIC_SECRET_!@#', useForLocal: false };

test('proxy settings default to environment and persist without exposing passwords', async t => {
  const { store, filePath } = await temporaryStore(t);
  assert.deepEqual(await store.get(), { mode: 'environment', url: '', noProxy: '', username: '', passwordConfigured: false, useForLocal: false });
  const saved = await store.save(custom);
  assert.deepEqual(saved, { mode: 'custom', url: 'http://proxy.test:8080/', noProxy: 'localhost,.internal', username: 'office-user', passwordConfigured: true, useForLocal: false });
  assert.ok(!JSON.stringify(saved).includes(custom.password));
  assert.equal(JSON.parse(await readFile(filePath, 'utf8')).password, custom.password);
  const restarted = createProxySettingsStore({ filePath });
  assert.deepEqual(await restarted.get(), saved);
  assert.equal((await restarted.effectiveEnv({})).PROXY_PASSWORD, custom.password);
});

test('blank passwords preserve the saved credential and explicit clear removes it', async t => {
  const { store, filePath } = await temporaryStore(t);
  await store.save(custom);
  await store.save({ mode: 'custom', password: '' });
  assert.equal((await store.effectiveEnv({})).PROXY_PASSWORD, custom.password);
  await store.save({ mode: 'custom', password: 'new synthetic password' });
  assert.equal((await store.effectiveEnv({})).PROXY_PASSWORD, 'new synthetic password');
  const cleared = await store.save({ mode: 'custom', password: '', clearPassword: true });
  assert.equal(cleared.passwordConfigured, false);
  assert.equal(JSON.parse(await readFile(filePath, 'utf8')).password, '');
});

test('invalid settings are rejected atomically and cannot leak submitted secrets', async t => {
  const { store, filePath } = await temporaryStore(t);
  await store.save(custom);
  const original = await readFile(filePath, 'utf8');
  const invalid = [
    null, [], { mode: 'other' }, { mode: 'custom', url: '' },
    { mode: 'custom', url: 'http://name:SENSITIVE_PASSWORD@proxy.test:8080' },
    { mode: 'custom', url: 'socks5://proxy.test:1080' },
    { mode: 'custom', url: 'http://proxy.test/path' },
    { mode: 'custom', url: 'https://proxy.test?secret=SENSITIVE_PASSWORD' },
    { mode: 'custom', url: 'https://proxy.test#SENSITIVE_PASSWORD' },
    { mode: 'custom', url: 'not a url SENSITIVE_PASSWORD' },
    { mode: 'custom', useForLocal: 'true' },
    { mode: 'custom', noProxy: [] },
    { mode: 'custom', username: 'bad:username' },
    { mode: 'custom', username: 'x'.repeat(257) },
    { mode: 'custom', noProxy: 'x'.repeat(4097) },
    { mode: 'custom', password: 'x'.repeat(4097) },
    { mode: 'custom', password: 'bad\nSENSITIVE_PASSWORD' },
    { mode: 'custom', password: null },
    { mode: 'custom', clearPassword: 'true' },
    { mode: 'custom', clearPassword: true, password: 'SENSITIVE_PASSWORD' },
    { mode: 'custom', unknown: 'SENSITIVE_PASSWORD' },
  ];
  for (const input of invalid) {
    await assert.rejects(store.save(input), error => error.public === true && error.status === 400 && !error.message.includes('SENSITIVE_PASSWORD'));
    assert.equal(await readFile(filePath, 'utf8'), original);
  }
  assert.equal((await store.save({ mode: 'direct' })).mode, 'direct');
});

test('concurrent saves serialize and retain credentials from the preceding save', async t => {
  const { store, filePath } = await temporaryStore(t);
  await Promise.all([
    store.save(custom),
    store.save({ mode: 'custom', noProxy: 'example.test', password: '' }),
    store.save({ mode: 'direct' }),
  ]);
  const stored = JSON.parse(await readFile(filePath, 'utf8'));
  assert.equal(stored.mode, 'direct');
  assert.equal(stored.noProxy, 'example.test');
  assert.equal(stored.password, custom.password);
  assert.deepEqual(await createProxySettingsStore({ filePath }).get(), await store.get());
});

test('saved settings override all proxy environment spellings without changing other settings', () => {
  const env = { HTTP_PROXY: 'http://old-upper', http_proxy: 'http://old-lower', HTTPS_PROXY: 'http://old-upper', https_proxy: 'http://old-lower', NO_PROXY: '*', no_proxy: '*', ALL_PROXY: 'http://old', all_proxy: 'http://old', PROXY_USERNAME: 'old-user', proxy_username: 'old-user', PROXY_PASSWORD: 'old-password', proxy_password: 'old-password', LOCAL_LLM_USE_PROXY: 'true', local_llm_use_proxy: 'true', OPENAI_API_KEY: 'test-api-key', LOCAL_LLM_MODEL: 'local-model' };
  const original = structuredClone(env);
  const direct = applyProxySettings(env, { ...custom, mode: 'direct' });
  assert.deepEqual(direct, { OPENAI_API_KEY: 'test-api-key', LOCAL_LLM_MODEL: 'local-model' });
  const configured = applyProxySettings(env, custom);
  assert.equal(configured.HTTP_PROXY, custom.url);
  assert.equal(configured.HTTPS_PROXY, custom.url);
  assert.equal(configured.NO_PROXY, custom.noProxy);
  assert.equal(configured.PROXY_PASSWORD, custom.password);
  assert.equal(configured.LOCAL_LLM_USE_PROXY, 'false');
  assert.equal(configured.http_proxy, undefined);
  assert.deepEqual(applyProxySettings(env, { mode: 'environment' }), env);
  assert.deepEqual(env, original);
});

test('corrupt saved data fails closed with a fixed error instead of exposing file content', async t => {
  const { store, filePath } = await temporaryStore(t);
  await store.save(custom);
  await writeFile(filePath, '{ PRIVATE_FILE_CONTENT');
  const restarted = createProxySettingsStore({ filePath });
  for (const action of [() => restarted.get(), () => restarted.effectiveEnv({})]) {
    await assert.rejects(action(), error => error.public === true && error.status === 500 && !error.message.includes('PRIVATE_FILE_CONTENT'));
  }
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

test('settings API rejects cross-origin and unsafe mutations before changing saved values', async t => {
  const { filePath } = await temporaryStore(t);
  const server = createStaticServer({ proxySettingsPath: filePath, env: {} });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const route = '/api/proxy-settings';
  const body = JSON.stringify(custom);
  const jsonHeaders = { 'content-type': 'application/json' };
  for (const headers of [{ origin: 'https://evil.example' }, { origin: 'null' }, { origin: `http://localhost:${server.address().port}` }, { host: 'evil.example' }]) {
    assert.equal((await request(server, route, 'PUT', { headers: { ...jsonHeaders, ...headers }, body })).status, 403);
  }
  assert.equal((await request(server, route, 'PUT', { body })).status, 415);
  assert.equal((await request(server, route, 'PUT', { headers: { 'content-type': 'text/plain' }, body })).status, 415);
  assert.equal((await request(server, route, 'PUT', { headers: jsonHeaders, body: '{' })).status, 400);
  assert.equal((await request(server, route, 'PUT', { headers: jsonHeaders, body: JSON.stringify({ mode: 'custom', url: 'x'.repeat(17000) }) })).status, 413);
  assert.equal((await request(server, route, 'POST', { headers: jsonHeaders, body })).status, 405);
  assert.equal(JSON.parse((await request(server, route)).body).mode, 'environment');
  const saved = await request(server, route, 'PUT', { headers: { ...jsonHeaders, origin: `http://127.0.0.1:${server.address().port}` }, body });
  assert.equal(saved.status, 200);
  assert.equal(saved.headers['cache-control'], 'no-store');
  assert.ok(!saved.body.includes(custom.password));
  const read = await request(server, route);
  assert.equal(read.status, 200);
  assert.ok(!read.body.includes(custom.password));
  assert.equal(JSON.parse(read.body).passwordConfigured, true);
  assert.equal((await request(server, '/data/proxy-settings.json')).status, 404);
});

test('config and translation receive current saved settings on each request', async t => {
  const { filePath } = await temporaryStore(t);
  const env = { http_proxy: 'http://environment-proxy.test:8080', PROXY_PASSWORD: 'environment-secret', OPENAI_MODEL: 'gpt-4o' };
  const seen = [];
  const providers = {
    getPublicConfig: current => ({ configured: Boolean(current.HTTP_PROXY || current.http_proxy), model: current.OPENAI_MODEL }),
    translateBatch: async (_payload, options) => { seen.push(options.env); return { translations: [] }; },
  };
  const server = createStaticServer({ providers, env, proxySettingsPath: filePath });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const jsonHeaders = { 'content-type': 'application/json' };
  const translate = () => request(server, '/api/translate-batch', 'POST', { headers: jsonHeaders, body: '{}' });
  const save = value => request(server, '/api/proxy-settings', 'PUT', { headers: jsonHeaders, body: JSON.stringify(value) });
  assert.equal((await translate()).status, 200);
  assert.equal(seen.at(-1).http_proxy, env.http_proxy);
  assert.equal((await save(custom)).status, 200);
  assert.equal((await translate()).status, 200);
  assert.equal(seen.at(-1).HTTP_PROXY, 'http://proxy.test:8080/');
  assert.equal(seen.at(-1).http_proxy, undefined);
  assert.equal(seen.at(-1).PROXY_PASSWORD, custom.password);
  assert.equal(JSON.parse((await request(server, '/api/config')).body).configured, true);
  assert.equal((await save({ mode: 'direct' })).status, 200);
  assert.equal((await translate()).status, 200);
  assert.equal(seen.at(-1).HTTP_PROXY, undefined);
  assert.equal(seen.at(-1).http_proxy, undefined);
  assert.equal(seen.at(-1).PROXY_PASSWORD, undefined);
  assert.equal(JSON.parse((await request(server, '/api/config')).body).configured, false);
  assert.equal((await save({ mode: 'environment' })).status, 200);
  assert.equal((await translate()).status, 200);
  assert.equal(seen.at(-1).http_proxy, env.http_proxy);
  assert.equal(seen.at(-1).OPENAI_MODEL, 'gpt-4o');
});
