import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { requestJson, proxyEnvironment } from '../proxy-fetch.mjs';

// These localhost-only PEM fixtures are intentionally public test credentials.
// They are never used by the application, and are valid through 2036-10-18.
const certificatePath = fileURLToPath(new URL('./fixtures/localhost-test-cert.pem', import.meta.url));
const privateKeyPath = fileURLToPath(new URL('./fixtures/localhost-test-key.pem', import.meta.url));

async function listen(t, server) {
  const sockets = new Set();
  server.on('connection', socket => {
    sockets.add(socket);
    socket.once('close', () => sockets.delete(socket));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  });
  return server.address().port;
}

function makeOrigin(records, { secure, key, cert, handler } = {}) {
  const onRequest = (request, response) => {
    const chunks = [];
    request.on('data', chunk => chunks.push(chunk));
    request.on('end', () => {
      const record = { url: request.url, headers: request.headers, body: JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') };
      records.push(record);
      if (handler) handler(request, response, record);
      else {
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({ ok: true, received: record.body }));
      }
    });
  };
  return secure ? https.createServer({ key, cert }, onRequest) : http.createServer(onRequest);
}

function makeForwardProxy(records, expectedAuthorization) {
  const server = http.createServer((request, response) => {
    records.push({ method: request.method, url: request.url, authorization: request.headers['proxy-authorization'] });
    if (expectedAuthorization && request.headers['proxy-authorization'] !== expectedAuthorization) {
      response.writeHead(407, { 'Proxy-Authenticate': 'Basic realm="local test"' });
      response.end('PRIVATE_PROXY_BODY');
      return;
    }
    const target = new URL(request.url);
    if (target.hostname !== '127.0.0.1') {
      response.writeHead(403).end();
      return;
    }
    const headers = { ...request.headers };
    delete headers['proxy-authorization'];
    const upstream = http.request(target, { method: request.method, headers, agent: new http.Agent({ proxyEnv: {} }) }, upstreamResponse => {
      response.writeHead(upstreamResponse.statusCode, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    });
    upstream.on('error', () => response.destroy());
    request.pipe(upstream);
  });
  server.on('connect', (request, client, head) => {
    records.push({ method: 'CONNECT', url: request.url, authorization: request.headers['proxy-authorization'] });
    if (expectedAuthorization && request.headers['proxy-authorization'] !== expectedAuthorization) {
      client.end('HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n');
      return;
    }
    const target = new URL(`http://${request.url}`);
    if (!['127.0.0.1', 'localhost'].includes(target.hostname)) {
      client.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');
      return;
    }
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

test('HTTP forward proxy carries the JSON request and Basic authentication', async t => {
  const origins = [];
  const proxied = [];
  const originPort = await listen(t, makeOrigin(origins));
  const auth = `Basic ${Buffer.from('proxy user:p@ss:word').toString('base64')}`;
  const proxyPort = await listen(t, makeForwardProxy(proxied, auth));
  const target = `http://127.0.0.1:${originPort}/v1/chat/completions`;
  const result = await requestJson(target, { model: 'test-model' }, {
    headers: { Authorization: 'Bearer test-api-key' },
    env: { HTTP_PROXY: `http://127.0.0.1:${proxyPort}`, PROXY_USERNAME: 'proxy user', PROXY_PASSWORD: 'p@ss:word' },
  });
  assert.equal(result.ok, true);
  assert.equal(proxied.length, 1);
  assert.equal(proxied[0].url, target);
  assert.equal(proxied[0].authorization, auth);
  assert.equal(origins[0].headers.authorization, 'Bearer test-api-key');
  assert.equal(origins[0].headers['proxy-authorization'], undefined);
  assert.deepEqual(origins[0].body, { model: 'test-model' });
});

test('NO_PROXY and direct intranet mode bypass a configured proxy', async t => {
  const origins = [];
  const proxied = [];
  const originPort = await listen(t, makeOrigin(origins));
  const proxyPort = await listen(t, makeForwardProxy(proxied));
  const target = `http://127.0.0.1:${originPort}/translate`;
  const env = { HTTP_PROXY: `http://127.0.0.1:${proxyPort}`, NO_PROXY: '127.0.0.1' };
  assert.equal((await requestJson(target, {}, { env })).ok, true);
  assert.equal((await requestJson(target, {}, { env: { ...env, NO_PROXY: '' }, useProxy: false })).ok, true);
  assert.equal(proxied.length, 0);
  assert.equal(origins.length, 2);
});

test('proxy URL credentials and lowercase environment precedence are respected', async t => {
  const origins = [];
  const proxied = [];
  const originPort = await listen(t, makeOrigin(origins));
  const auth = `Basic ${Buffer.from('name:p@ss').toString('base64')}`;
  const proxyPort = await listen(t, makeForwardProxy(proxied, auth));
  const env = { HTTP_PROXY: 'http://127.0.0.1:1', http_proxy: `http://name:p%40ss@127.0.0.1:${proxyPort}`, NO_PROXY: '*' , no_proxy: 'not-this-host.invalid' };
  assert.equal((await requestJson(`http://127.0.0.1:${originPort}/`, {}, { env })).ok, true);
  assert.equal(proxied[0].authorization, auth);
  assert.throws(() => proxyEnvironment({ HTTPS_PROXY: 'socks5://localhost:1234' }), error => error.public === true && error.status === 400);
});

test('timeouts and user cancellation stop active requests', async t => {
  const records = [];
  let arrived;
  const originPort = await listen(t, makeOrigin(records, { handler: () => arrived?.() }));
  const target = `http://127.0.0.1:${originPort}/`;
  await assert.rejects(requestJson(target, {}, { env: {}, timeoutMs: 40 }), error => error.public === true && error.status === 504);
  const controller = new AbortController();
  const started = new Promise(resolve => { arrived = resolve; });
  const rejection = assert.rejects(requestJson(target, {}, { env: {}, signal: controller.signal, timeoutMs: 2000 }), error => error.name === 'AbortError' && error.status === 499);
  await started;
  controller.abort();
  await rejection;
});

test('upstream error bodies never enter public errors', async t => {
  const originPort = await listen(t, makeOrigin([], { handler: (_req, response) => {
    response.writeHead(401, { 'Content-Type': 'application/json' });
    response.end('{"error":"PRIVATE_UPSTREAM_TOKEN"}');
  } }));
  await assert.rejects(requestJson(`http://127.0.0.1:${originPort}/`, {}, { env: {} }), error => error.public === true && !error.message.includes('PRIVATE_UPSTREAM_TOKEN') && error.message.includes('401'));
});

test('upstream failures give actionable classifications without exposing response text or retrying', async t => {
  const privateValue = 'PRIVATE_UPSTREAM_TOKEN';
  const cases = [
    { status: 400, body: { error: { code: 'context_length_exceeded', message: privateValue } }, code: 'CONTEXT_LIMIT', match: /コンテキスト上限/ },
    { status: 400, body: { error: `Requested 8192 output tokens exceed the 4096 context length. ${privateValue}` }, code: 'CONTEXT_LIMIT', match: /領域・文字数/ },
    { status: 400, body: { error: { param: 'max_tokens', message: `must be less than 4096 ${privateValue}` } }, code: 'TOKEN_LIMIT', match: /LOCAL_LLM_MAX_TOKENS/ },
    { status: 400, body: { error: { code: 'model_not_loaded', message: privateValue } }, code: 'MODEL_NOT_LOADED', match: /ロード/ },
    { status: 400, body: { error: { message: `No models loaded. ${privateValue}` } }, code: 'MODEL_NOT_LOADED', match: /ロード/ },
    { status: 404, body: { error: { code: 'model_not_found', message: privateValue } }, code: 'MODEL_NOT_FOUND', match: /モデルID/ },
    { status: 404, body: { error: { message: `model '${privateValue}' does not exist` } }, code: 'MODEL_NOT_FOUND', match: /モデルID/ },
    { status: 400, body: { error: { code: 'unsupported_parameter', param: 'response_format', message: privateValue } }, code: 'UNSUPPORTED_RESPONSE_FORMAT', match: /JSON出力形式/ },
    { status: 400, body: { error: { message: `response_format json_schema is not supported ${privateValue}` } }, code: 'UNSUPPORTED_RESPONSE_FORMAT', match: /JSON出力形式/ },
    { status: 400, body: { error: { code: 'invalid_api_key', message: privateValue } }, code: 'API_AUTHENTICATION', match: /APIキー/ },
    { status: 429, body: { error: { code: 'insufficient_quota', message: privateValue } }, code: 'API_QUOTA', match: /残高/ },
    { status: 429, body: { error: { message: privateValue } }, code: 'API_RATE_LIMIT', match: /利用頻度/ },
    { status: 407, body: { error: { message: privateValue } }, code: 'PROXY_AUTHENTICATION', match: /プロキシ認証/ },
    { status: 403, body: { error: { message: privateValue } }, code: 'API_FORBIDDEN', match: /アクセス権/ },
    { status: 400, body: { error: { code: privateValue, param: privateValue, message: privateValue } }, code: 'UPSTREAM_HTTP_ERROR', match: /API形式/ },
    { status: 502, body: `<html>${privateValue}</html>`, code: 'UPSTREAM_HTTP_ERROR', match: /接続先の状態/ },
  ];
  const records = [];
  let current;
  const originPort = await listen(t, makeOrigin(records, { handler: (_req, response) => {
    response.writeHead(current.status, { 'Content-Type': 'application/json' });
    response.end(typeof current.body === 'string' ? current.body : JSON.stringify(current.body));
  } }));
  for (const item of cases) {
    current = item;
    const count = records.length;
    await assert.rejects(requestJson(`http://127.0.0.1:${originPort}/`, {}, { env: {} }), error => {
      assert.equal(error.public, true);
      assert.equal(error.status, item.status === 429 ? 429 : 502);
      assert.equal(error.code, item.code);
      assert.match(error.message, item.match);
      assert.ok(!error.message.includes(privateValue));
      assert.ok(!JSON.stringify(error).includes(privateValue));
      return true;
    });
    assert.equal(records.length, count + 1, `${item.code} must not retry`);
  }
});

test('network failures distinguish DNS, refusal and reset without exposing private connection details', async t => {
  const privateValue = 'PRIVATE_HOST_AND_PASSWORD';
  const withCode = code => Object.assign(new Error(privateValue), { code });
  const cases = [
    { error: withCode('ENOTFOUND'), code: 'DNS_LOOKUP_FAILED', match: /ホスト名を解決/ },
    { error: withCode('EAI_AGAIN'), code: 'DNS_LOOKUP_FAILED', match: /DNS/ },
    { error: new AggregateError([withCode('ECONNREFUSED'), withCode('ECONNREFUSED')], privateValue), code: 'CONNECTION_REFUSED', match: /ポート番号/ },
    { error: new Error(privateValue, { cause: withCode('ECONNRESET') }), code: 'CONNECTION_RESET', match: /途中で切断/ },
    { error: withCode('EPIPE'), code: 'CONNECTION_RESET', match: /途中で切断/ },
    { error: withCode('ETIMEDOUT'), code: 'CONNECTION_TIMEOUT', match: /タイムアウト/, status: 504 },
    { error: new Error(privateValue, { cause: withCode('UNABLE_TO_VERIFY_LEAF_SIGNATURE') }), code: 'TLS_CERTIFICATE', match: /社内CA/ },
    { error: withCode(privateValue), code: 'CONNECTION_FAILED', match: /APIに接続できません/ },
  ];
  let current; let calls = 0;
  t.mock.method(http, 'request', () => {
    calls += 1;
    const request = new EventEmitter();
    request.end = () => { queueMicrotask(() => request.emit('error', current.error)); };
    return request;
  });
  for (const item of cases) {
    current = item;
    const count = calls;
    await assert.rejects(requestJson('http://127.0.0.1:1/', {}, { env: {} }), error => {
      assert.equal(error.public, true);
      assert.equal(error.status, item.status || 502);
      assert.equal(error.code, item.code);
      assert.match(error.message, item.match);
      assert.ok(!error.message.includes(privateValue));
      assert.ok(!JSON.stringify(error).includes(privateValue));
      return true;
    });
    assert.equal(calls, count + 1, `${item.code} must not retry`);
  }
});

test('HTTPS uses authenticated CONNECT and trusts only the supplied test CA', async t => {
  const origins = [];
  const proxied = [];
  const [cert, key] = await Promise.all([readFile(certificatePath), readFile(privateKeyPath)]);
  const originPort = await listen(t, makeOrigin(origins, { secure: true, cert, key }));
  const auth = `Basic ${Buffer.from('connect-user:connect-password').toString('base64')}`;
  const proxyPort = await listen(t, makeForwardProxy(proxied, auth));
  const target = `https://127.0.0.1:${originPort}/v1/responses`;
  const proxyUrl = `http://connect-user:connect-password@127.0.0.1:${proxyPort}`;

  // The untrusted self-signed certificate must fail; no rejectUnauthorized bypass.
  await assert.rejects(requestJson(target, {}, { env: { HTTPS_PROXY: proxyUrl } }), error => error.public === true && error.message.includes('TLS'));

  // NODE_EXTRA_CA_CERTS is read at process startup, so use a child for this case.
  const moduleUrl = new URL('../proxy-fetch.mjs', import.meta.url).href;
  const script = 'const { requestJson } = await import(process.argv[1]); const result = await requestJson(process.argv[2], { model: "tls-test" }, { env: { HTTPS_PROXY: process.argv[3] } }); process.stdout.write(JSON.stringify(result));';
  const result = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['--input-type=module', '--eval', script, moduleUrl, target, proxyUrl], {
      windowsHide: true,
      env: { ...process.env, NODE_EXTRA_CA_CERTS: certificatePath },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    let errors = '';
    child.stdout.on('data', chunk => { output += chunk; });
    child.stderr.on('data', chunk => { errors += chunk; });
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve(JSON.parse(output)) : reject(new Error(`TLS test child failed (${code}): ${errors}`)));
  });
  assert.equal(result.ok, true);
  assert.equal(proxied.length, 2);
  assert.ok(proxied.every(request => request.method === 'CONNECT' && request.authorization === auth));
  assert.equal(origins.length, 1);
  assert.equal(origins[0].url, '/v1/responses');
  assert.equal(origins[0].headers['proxy-authorization'], undefined);
});
