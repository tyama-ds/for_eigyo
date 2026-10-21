import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtemp, mkdir, writeFile, symlink, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createStaticServer } from '../server.mjs';

function request(server, requestPath, method = 'GET', { headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port: server.address().port, path: requestPath, method, headers }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks).toString() }));
      response.on('error', reject);
    });
    req.on('error', reject);
    req.end(body);
  });
}

test('static server serves browser assets and blocks paths outside the public directory', async t => {
  const temp = await mkdtemp(path.join(os.tmpdir(), 'pdf-translator-server-'));
  const root = path.join(temp, 'dist');
  await mkdir(root);
  await mkdir(path.join(root, 'folder'));
  await writeFile(path.join(root, 'index.html'), '<h1>PDF translation</h1>');
  await writeFile(path.join(root, 'worker.mjs'), 'export const value = 1;');
  await writeFile(path.join(root, 'decoder.wasm'), Buffer.from([0, 97, 115, 109]));
  await writeFile(path.join(root, 'sample.pdf'), '%PDF-1.7');
  await writeFile(path.join(root, '.secret'), 'hidden');
  await writeFile(path.join(temp, 'secret.txt'), 'outside');
  const server = createStaticServer({ root });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    await rm(temp, { recursive: true, force: true });
  });

  await t.test('GET and HEAD have the same content metadata', async () => {
    const get = await request(server, '/?version=1');
    const head = await request(server, '/', 'HEAD');
    assert.equal(get.status, 200);
    assert.equal(get.body, '<h1>PDF translation</h1>');
    assert.equal(head.status, 200);
    assert.equal(head.body, '');
    assert.equal(head.headers['content-length'], get.headers['content-length']);
    assert.equal(get.headers['x-content-type-options'], 'nosniff');
    assert.equal(get.headers['access-control-allow-origin'], undefined);
  });

  await t.test('module, WebAssembly, and PDF types are served correctly', async () => {
    for (const [file, mime] of [['worker.mjs', 'text/javascript; charset=utf-8'], ['decoder.wasm', 'application/wasm'], ['sample.pdf', 'application/pdf']]) {
      const response = await request(server, `/${file}`);
      assert.equal(response.status, 200);
      assert.equal(response.headers['content-type'], mime);
    }
  });

  await t.test('non-read methods, missing files, and directories are rejected', async () => {
    const post = await request(server, '/', 'POST');
    assert.equal(post.status, 405);
    assert.equal(post.headers.allow, 'GET, HEAD');
    for (const file of ['/missing', '/folder', '/folder/']) assert.equal((await request(server, file)).status, 404);
  });

  await t.test('encoded traversal, hidden files, Windows separators, and malformed paths are rejected', async () => {
    for (const file of ['/../secret.txt', '/%2e%2e/secret.txt', '/folder/%2E%2E/%2E%2E/secret.txt', '/.secret']) {
      assert.equal((await request(server, file)).status, 403, file);
    }
    for (const file of ['/%2e%2e%5csecret.txt', '/file%00.pdf', '/sample.pdf:secret', '/%ZZ']) {
      assert.equal((await request(server, file)).status, 400, file);
    }
  });

  await t.test('a symlink cannot escape the public directory', async t => {
    const link = path.join(root, 'escape');
    try {
      await symlink(temp, link, process.platform === 'win32' ? 'junction' : 'dir');
    } catch (error) {
      if (['EPERM', 'EACCES'].includes(error.code)) return t.skip('Symlink creation is unavailable on this host');
      throw error;
    }
    assert.equal((await request(server, '/escape/secret.txt')).status, 403);
  });
});

test('API routing keeps requests local, bounds input, and hides unexpected errors', async t => {
  let received;
  const provider = {
    getPublicConfig: () => ({ local: { configured: true }, openai: { configured: false } }),
    translateBatch: async (payload, { signal }) => {
      received = { payload, signal };
      if (payload.mode === 'unexpected') throw new Error('PRIVATE_UPSTREAM_TOKEN');
      if (payload.mode === 'public') throw Object.assign(new Error('モデルが指定されていません。'), { public: true, status: 400 });
      return { translations: [{ id: 'a', text: '翻訳' }], apiMode: 'chat', model: 'local-model' };
    },
  };
  const server = createStaticServer({ providers: provider });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const jsonHeaders = { 'content-type': 'application/json' };

  await t.test('config and translation delegate to the provider', async () => {
    const config = await request(server, '/api/config');
    assert.equal(config.status, 200);
    assert.deepEqual(JSON.parse(config.body), provider.getPublicConfig());
    assert.equal(config.headers['cache-control'], 'no-store');
    const payload = { segments: [{ id: 'a', text: 'Translate me' }], targetLanguage: 'ja' };
    const response = await request(server, '/api/translate-batch', 'POST', { headers: jsonHeaders, body: JSON.stringify(payload) });
    assert.equal(response.status, 200);
    assert.equal(JSON.parse(response.body).translations[0].text, '翻訳');
    assert.deepEqual(received.payload, payload);
    assert.ok(received.signal instanceof AbortSignal);
  });

  await t.test('foreign hosts, foreign origins, and mismatched local origins are blocked', async () => {
    for (const headers of [
      { host: 'evil.example' },
      { host: `127.0.0.1:${server.address().port + 1}` },
      { origin: 'https://evil.example' },
      { origin: `http://localhost:${server.address().port}` },
      { origin: 'null' },
    ]) {
      assert.equal((await request(server, '/api/config', 'GET', { headers })).status, 403);
    }
    const localOrigin = `http://127.0.0.1:${server.address().port}`;
    assert.equal((await request(server, '/api/config', 'GET', { headers: { origin: localOrigin } })).status, 200);
  });

  await t.test('invalid JSON, unsupported media types, and large bodies are rejected', async () => {
    assert.equal((await request(server, '/api/translate-batch', 'POST', { body: '{}' })).status, 415);
    assert.equal((await request(server, '/api/translate-batch', 'POST', { headers: jsonHeaders, body: '{' })).status, 400);
    assert.equal((await request(server, '/api/translate-batch', 'POST', { headers: jsonHeaders, body: JSON.stringify({ text: 'x'.repeat(1024 * 1024) }) })).status, 413);
    assert.equal((await request(server, '/api/translate-batch')).status, 405);
    assert.equal((await request(server, '/api/unknown')).status, 404);
  });

  await t.test('only explicitly public error messages reach the browser', async () => {
    const unexpected = await request(server, '/api/translate-batch', 'POST', { headers: jsonHeaders, body: '{"mode":"unexpected"}' });
    assert.equal(unexpected.status, 500);
    assert.ok(!unexpected.body.includes('PRIVATE_UPSTREAM_TOKEN'));
    const expected = await request(server, '/api/translate-batch', 'POST', { headers: jsonHeaders, body: '{"mode":"public"}' });
    assert.equal(expected.status, 400);
    assert.equal(JSON.parse(expected.body).error, 'モデルが指定されていません。');
  });

  await t.test('closing the browser request aborts the active translation', async () => {
    let started;
    let aborted;
    const waitForStarted = new Promise(resolve => { started = resolve; });
    const waitForAbort = new Promise(resolve => { aborted = resolve; });
    provider.translateBatch = async (_payload, { signal }) => {
      started();
      await new Promise(resolve => signal.addEventListener('abort', () => { aborted(); resolve(); }, { once: true }));
      return { translations: [] };
    };
    const req = http.request({ hostname: '127.0.0.1', port: server.address().port, path: '/api/translate-batch', method: 'POST', headers: jsonHeaders });
    req.on('error', () => {});
    req.end('{}');
    await waitForStarted;
    req.destroy();
    await Promise.race([waitForAbort, new Promise((_, reject) => { const timer = setTimeout(() => reject(new Error('The provider did not receive cancellation')), 1000); timer.unref(); })]);
  });
});
