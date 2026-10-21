import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { createStaticServer } from '../server.mjs';

// Only synthetic documents and credentials reach loopback fixtures in this file.
const password = 'synthetic p@ss:word';
const username = 'synthetic proxy user';
const apiKey = 'synthetic-api-key';
const authorization = `Basic ${Buffer.from(`${username}:${password}`).toString('base64')}`;
const payload = provider => ({
  config: { provider, model: provider === 'openai' ? 'gpt-4o' : 'synthetic-local', targetLanguage: 'ja' },
  blocks: [{ id: 'p1-b1', text: 'A synthetic annual report.' }],
});

async function listen(t, server) {
  const sockets = new Set();
  server.on('connection', socket => {
    sockets.add(socket);
    socket.once('close', () => sockets.delete(socket));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const close = async () => {
    for (const socket of sockets) socket.destroy();
    if (server.listening) await new Promise(resolve => server.close(resolve));
  };
  t.after(close);
  return { port: server.address().port, close };
}

async function settingsFile(t) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'pdf-lingua-proxy-integration-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  return path.join(dir, 'proxy-settings.json');
}

function call(port, route, { method = 'GET', body } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: '127.0.0.1', port, path: route, method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      agent: new http.Agent({ proxyEnv: {} }),
    }, response => {
      let raw = '';
      response.setEncoding('utf8');
      response.on('data', chunk => { raw += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, raw, value: JSON.parse(raw) }));
      response.on('error', reject);
    });
    request.on('error', reject);
    request.end(body === undefined ? undefined : JSON.stringify(body));
  });
}

function translationOrigin(records, tls) {
  const handler = (request, response) => {
    let raw = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { raw += chunk; });
    request.on('end', () => {
      const body = JSON.parse(raw);
      records.push({ url: request.url, headers: request.headers, body });
      const input = JSON.parse(body.messages?.at(-1).content ?? body.input[0].content);
      const text = JSON.stringify({ translations: input.blocks.map(block => ({ id: block.id, text: '合成の年次報告書。' })) });
      const result = request.url.endsWith('/responses')
        ? { status: 'completed', output: [{ type: 'message', content: [{ type: 'output_text', text }] }] }
        : { choices: [{ finish_reason: 'stop', message: { content: text } }] };
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(result));
    });
  };
  return tls ? https.createServer(tls, handler) : http.createServer(handler);
}

function forwardingProxy(records, { expectedAuthorization, rejectRequests } = {}) {
  const allowed = request => !rejectRequests?.() && (!expectedAuthorization || request.headers['proxy-authorization'] === expectedAuthorization);
  const server = http.createServer((request, response) => {
    records.push({ method: request.method, url: request.url, authorization: request.headers['proxy-authorization'] });
    if (!allowed(request)) {
      response.writeHead(407, { 'Content-Type': 'application/json', 'Proxy-Authenticate': 'Basic realm="synthetic-test"' });
      response.end(JSON.stringify({ error: `${password} ${apiKey}` }));
      return;
    }
    const target = new URL(request.url);
    assert.equal(target.hostname, '127.0.0.1');
    const headers = { ...request.headers };
    delete headers['proxy-authorization'];
    const upstream = http.request(target, { method: request.method, headers, agent: new http.Agent({ proxyEnv: {} }) }, incoming => {
      response.writeHead(incoming.statusCode, incoming.headers);
      incoming.pipe(response);
    });
    upstream.on('error', () => response.destroy());
    request.pipe(upstream);
  });
  server.on('connect', (request, client, head) => {
    records.push({ method: 'CONNECT', url: request.url, authorization: request.headers['proxy-authorization'] });
    if (!allowed(request)) {
      client.end('HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n');
      return;
    }
    const target = new URL(`http://${request.url}`);
    assert.ok(['127.0.0.1', 'localhost'].includes(target.hostname));
    const upstream = net.connect({ host: '127.0.0.1', port: Number(target.port) }, () => {
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) upstream.write(head);
      client.pipe(upstream);
      upstream.pipe(client);
    });
    upstream.on('error', () => client.destroy());
    client.on('error', () => upstream.destroy());
    client.on('close', () => upstream.destroy());
  });
  return server;
}

function assertPublic(response) {
  for (const secret of [password, encodeURIComponent(password), authorization, apiKey]) {
    assert.ok(!response.raw.includes(secret), 'HTTP responses must not expose credentials');
  }
  assert.equal(response.value.password, undefined);
}

async function save(port, patch) {
  const response = await call(port, '/api/proxy-settings', { method: 'PUT', body: patch });
  assert.equal(response.status, 200, response.raw);
  assertPublic(response);
  return response.value;
}

async function translate(port, provider = 'local', patch = {}) {
  const input = payload(provider);
  Object.assign(input.config, patch);
  const response = await call(port, '/api/translate-batch', { method: 'POST', body: input });
  assert.equal(response.status, 200, response.raw);
  assertPublic(response);
  assert.deepEqual(response.value.translations, [{ id: 'p1-b1', text: '合成の年次報告書。' }]);
  return response.value;
}

test('saved proxy and Basic credentials apply on the next real request and survive a server restart', async t => {
  const proxySettingsPath = await settingsFile(t);
  const origins = []; const proxied = [];
  const origin = await listen(t, translationOrigin(origins));
  const proxy = await listen(t, forwardingProxy(proxied, { expectedAuthorization: authorization }));
  const env = {
    LOCAL_LLM_BASE_URL: `http://127.0.0.1:${origin.port}/v1`, LOCAL_LLM_MODEL: 'synthetic-local',
    LOCAL_LLM_API_KEY: apiKey, REQUEST_TIMEOUT_MS: '2000',
    // App settings must override every environment casing, not just uppercase.
    HTTP_PROXY: 'http://127.0.0.1:1', http_proxy: 'http://127.0.0.1:2',
    HTTPS_PROXY: 'http://127.0.0.1:3', https_proxy: 'http://127.0.0.1:4',
    NO_PROXY: '*', no_proxy: '*', PROXY_USERNAME: 'stale-user', PROXY_PASSWORD: 'stale-password',
  };
  const app = await listen(t, createStaticServer({ env, proxySettingsPath }));
  await translate(app.port);
  assert.equal(proxied.length, 0, 'local LLM initially bypasses proxies');
  const saved = await save(app.port, { mode: 'custom', url: `http://127.0.0.1:${proxy.port}`, username, password, noProxy: '', useForLocal: true });
  assert.equal(saved.passwordConfigured, true);
  await translate(app.port);
  assert.equal(proxied.length, 1);
  assert.equal(proxied[0].authorization, authorization);
  assert.equal(origins[1].headers.authorization, `Bearer ${apiKey}`);
  assert.equal(origins[1].headers['proxy-authorization'], undefined);

  await save(app.port, { ...saved, password: '' });
  await app.close();
  const restarted = await listen(t, createStaticServer({ env, proxySettingsPath }));
  const loaded = await call(restarted.port, '/api/proxy-settings');
  assert.equal(loaded.status, 200);
  assertPublic(loaded);
  assert.equal(loaded.value.passwordConfigured, true);
  assert.equal(loaded.value.mode, 'custom');
  await translate(restarted.port);
  assert.equal(proxied.length, 2);
  assert.equal(proxied[1].authorization, authorization, 'blank password preserves the saved secret');
  const config = await call(restarted.port, '/api/config');
  assertPublic(config);
  assert.equal(config.value.proxy.configured, true);
});

test('saved local bypass, NO_PROXY, direct mode, and environment mode route real requests correctly', async t => {
  const proxySettingsPath = await settingsFile(t);
  const origins = []; const appProxied = []; const environmentProxied = [];
  const origin = await listen(t, translationOrigin(origins));
  const appProxy = await listen(t, forwardingProxy(appProxied));
  const environmentProxy = await listen(t, forwardingProxy(environmentProxied));
  const env = { LOCAL_LLM_BASE_URL: `http://127.0.0.1:${origin.port}/v1`, LOCAL_LLM_MODEL: 'synthetic-local', LOCAL_LLM_USE_PROXY: 'true', HTTP_PROXY: `http://127.0.0.1:${environmentProxy.port}`, REQUEST_TIMEOUT_MS: '2000' };
  const app = await listen(t, createStaticServer({ env, proxySettingsPath }));
  await save(app.port, { mode: 'custom', url: `http://127.0.0.1:${appProxy.port}`, noProxy: '', useForLocal: false });
  await translate(app.port);
  assert.equal(appProxied.length, 0);
  assert.equal(environmentProxied.length, 0);
  await save(app.port, { mode: 'custom', useForLocal: true });
  await translate(app.port);
  assert.equal(appProxied.length, 1);
  await save(app.port, { mode: 'custom', noProxy: '127.0.0.1' });
  await translate(app.port);
  assert.equal(appProxied.length, 1, 'NO_PROXY bypasses the configured app proxy');
  await save(app.port, { mode: 'direct', noProxy: '' });
  await translate(app.port);
  assert.equal(appProxied.length, 1);
  assert.equal(environmentProxied.length, 0, 'direct mode suppresses the environment proxy too');
  await save(app.port, { mode: 'environment' });
  await translate(app.port);
  assert.equal(environmentProxied.length, 1);
  assert.equal(origins.length, 5);
});

test('clearing a saved password removes it from subsequent proxy authentication and failures stay private', async t => {
  const proxySettingsPath = await settingsFile(t);
  const origins = []; const proxied = [];
  const origin = await listen(t, translationOrigin(origins));
  const proxy = await listen(t, forwardingProxy(proxied, { expectedAuthorization: authorization }));
  const app = await listen(t, createStaticServer({ proxySettingsPath, env: { LOCAL_LLM_BASE_URL: `http://127.0.0.1:${origin.port}/v1`, LOCAL_LLM_MODEL: 'synthetic-local', REQUEST_TIMEOUT_MS: '2000' } }));
  await save(app.port, { mode: 'custom', url: `http://127.0.0.1:${proxy.port}`, username, password, useForLocal: true });
  await translate(app.port);
  const cleared = await save(app.port, { mode: 'custom', clearPassword: true });
  assert.equal(cleared.passwordConfigured, false);
  const response = await call(app.port, '/api/translate-batch', { method: 'POST', body: payload('local') });
  assert.equal(response.status, 502);
  assert.match(response.value.error, /プロキシ認証/);
  assertPublic(response);
  assert.equal(proxied.length, 2, 'proxy authentication failures must not trigger translation retries');
  assert.equal(proxied[1].authorization, `Basic ${Buffer.from(`${username}:`).toString('base64')}`);
  assert.equal(origins.length, 1);
  assert.ok(!(await readFile(proxySettingsPath, 'utf8')).includes(password));
});

test('app-saved proxy carries both OpenAI API formats through a verified HTTPS CONNECT tunnel', async t => {
  const proxySettingsPath = await settingsFile(t);
  const certificatePath = fileURLToPath(new URL('./fixtures/localhost-test-cert.pem', import.meta.url));
  const privateKeyPath = fileURLToPath(new URL('./fixtures/localhost-test-key.pem', import.meta.url));
  const origins = []; const proxied = [];
  const origin = await listen(t, translationOrigin(origins, { cert: await readFile(certificatePath), key: await readFile(privateKeyPath) }));
  const proxy = await listen(t, forwardingProxy(proxied, { expectedAuthorization: authorization }));
  // Node reads extra CAs only on startup; the child runs the actual app server.
  const script = `const { createStaticServer } = await import(process.argv[1]);
    const server = createStaticServer({ env: JSON.parse(process.argv[2]), proxySettingsPath: process.argv[3] });
    server.listen(0, '127.0.0.1', () => process.send({ port: server.address().port }));
    process.on('message', () => server.close(() => process.exit(0)));`;
  const child = spawn(process.execPath, ['--input-type=module', '--eval', script, new URL('../server.mjs', import.meta.url).href,
    JSON.stringify({ OPENAI_BASE_URL: `https://localhost:${origin.port}/v1`, OPENAI_API_KEY: apiKey, REQUEST_TIMEOUT_MS: '3000' }), proxySettingsPath], {
    windowsHide: true, env: { ...process.env, NODE_EXTRA_CA_CERTS: certificatePath }, stdio: ['ignore', 'ignore', 'pipe', 'ipc'],
  });
  let diagnostics = '';
  child.stderr.on('data', chunk => { diagnostics += chunk; });
  t.after(async () => {
    if (child.exitCode !== null) return;
    await new Promise(resolve => {
      const timer = setTimeout(() => child.kill(), 2000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
      child.send('close', error => { if (error) child.kill(); });
    });
  });
  const port = await new Promise((resolve, reject) => {
    child.once('message', message => resolve(message.port));
    child.once('error', reject);
    child.once('exit', code => reject(new Error(`Test app exited early (${code}): ${diagnostics}`)));
  });
  await save(port, { mode: 'custom', url: `http://127.0.0.1:${proxy.port}`, username, password, noProxy: '', useForLocal: false });
  assert.equal((await translate(port, 'openai')).apiMode, 'chat');
  assert.equal((await translate(port, 'openai', { model: 'gpt-5' })).apiMode, 'responses');
  assert.equal(proxied.length, 2);
  assert.ok(proxied.every(record => record.method === 'CONNECT' && record.authorization === authorization));
  assert.deepEqual(origins.map(record => record.url), ['/v1/chat/completions', '/v1/responses']);
  assert.ok(origins.every(record => record.headers.authorization === `Bearer ${apiKey}` && record.headers['proxy-authorization'] === undefined));
});
