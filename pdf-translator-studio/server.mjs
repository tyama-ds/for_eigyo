import http from 'node:http';
import { createReadStream } from 'node:fs';
import { realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createProxySettingsStore } from './proxy-settings.mjs';

const defaultRoot = fileURLToPath(new URL('./dist/', import.meta.url));
const envPath = fileURLToPath(new URL('./.env', import.meta.url));
const maxJsonBytes = 1024 * 1024;
const mimeTypes = new Map([
  ['.html', 'text/html; charset=utf-8'],
  ['.css', 'text/css; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.mjs', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.wasm', 'application/wasm'],
  ['.pdf', 'application/pdf'],
  ['.png', 'image/png'],
  ['.jpg', 'image/jpeg'],
  ['.jpeg', 'image/jpeg'],
  ['.webp', 'image/webp'],
  ['.svg', 'image/svg+xml'],
  ['.ico', 'image/x-icon'],
  ['.woff', 'font/woff'],
  ['.woff2', 'font/woff2'],
  ['.ttf', 'font/ttf'],
  ['.otf', 'font/otf'],
  ['.txt', 'text/plain; charset=utf-8'],
  ['.bcmap', 'application/octet-stream'],
]);

function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function respond(request, response, statusCode, message, extraHeaders = {}) {
  const body = `${message}\n`;
  response.writeHead(statusCode, {
    'Content-Type': 'text/plain; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    ...extraHeaders,
  });
  response.end(request.method === 'HEAD' ? undefined : body);
}

function respondJson(request, response, statusCode, value, extraHeaders = {}) {
  if (response.destroyed || response.writableEnded) return;
  const body = JSON.stringify(value);
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
    ...extraHeaders,
  });
  response.end(request.method === 'HEAD' ? undefined : body);
}

function publicError(status, message) {
  return Object.assign(new Error(message), { status, public: true });
}

function isLocalRequest(request) {
  const port = request.socket.localPort;
  const host = request.headers.host?.toLowerCase();
  const allowed = new Set([`127.0.0.1:${port}`, `localhost:${port}`]);
  if (port === 80) { allowed.add('127.0.0.1'); allowed.add('localhost'); }
  if (!allowed.has(host)) return false;
  // Requests with an Origin must come from this exact local origin. No CORS grant.
  const origin = request.headers.origin;
  if (origin !== undefined && origin !== `http://${host}`) return false;
  return true;
}

function readJson(request, limit = maxJsonBytes) {
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers['content-type'] || '')) {
    request.resume();
    return Promise.reject(publicError(415, 'Content-Type は application/json を指定してください。'));
  }
  const sizeError = () => publicError(413, limit === maxJsonBytes ? 'リクエストが大きすぎます。1 MB 以下に分割してください。' : 'プロキシ設定が大きすぎます。16 KB 以下にしてください。');
  if (Number(request.headers['content-length']) > limit) {
    request.resume();
    return Promise.reject(sizeError());
  }
  return new Promise((resolve, reject) => {
    let size = 0;
    let chunks = [];
    const cleanup = () => {
      request.off('data', onData);
      request.off('end', onEnd);
      request.off('aborted', onAborted);
      request.off('error', onError);
    };
    const fail = error => { cleanup(); chunks = []; reject(error); };
    const onAborted = () => fail(publicError(400, 'リクエストが中断されました。'));
    const onError = () => fail(publicError(400, 'リクエストを読み取れませんでした。'));
    const onData = chunk => {
      size += chunk.length;
      if (size > limit) {
        fail(sizeError());
        request.resume();
        return;
      }
      chunks.push(chunk);
    };
    const onEnd = () => {
      cleanup();
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch { reject(publicError(400, 'JSON の形式を確認してください。')); }
    };
    request.on('data', onData);
    request.once('end', onEnd);
    request.once('aborted', onAborted);
    request.once('error', onError);
  });
}

/** Loopback server; credentials stay in the server-side provider module. */
export function createStaticServer({ root = defaultRoot, providers, env = process.env, proxySettingsPath, proxySettingsStore } = {}) {
  const resolvedRoot = path.resolve(root);
  const proxySettings = proxySettingsStore || createProxySettingsStore({ filePath: proxySettingsPath });
  let providerImport;
  const getProviders = () => providers || (providerImport ||= import('./providers.mjs'));
  const server = http.createServer(async (request, response) => {
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('Referrer-Policy', 'no-referrer');
    response.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
    response.setHeader('Cache-Control', 'no-cache');

    if (!isLocalRequest(request)) {
      respondJson(request, response, 403, { error: 'このローカルサーバーと同じオリジンからアクセスしてください。' });
      request.resume();
      return;
    }

    let pathname;
    try {
      // Read the raw path so URL normalization cannot hide traversal segments.
      pathname = decodeURIComponent((request.url || '/').split('?')[0]);
    } catch {
      respond(request, response, 400, 'Invalid URL encoding');
      return;
    }
    if (!pathname.startsWith('/') || /[\\\u0000-\u001f\u007f:]/.test(pathname)) {
      respond(request, response, 400, 'Invalid path');
      return;
    }

    if (pathname.startsWith('/api/')) {
      const controller = new AbortController();
      const onClose = () => { if (!response.writableEnded) controller.abort(); };
      response.once('close', onClose);
      request.once('aborted', () => controller.abort());
      try {
        if (pathname === '/api/config') {
          if (request.method !== 'GET' && request.method !== 'HEAD') {
            respondJson(request, response, 405, { error: 'Method not allowed' }, { Allow: 'GET, HEAD' });
          } else {
            const service = await getProviders();
            respondJson(request, response, 200, await service.getPublicConfig(await proxySettings.effectiveEnv(env)));
          }
        } else if (pathname === '/api/proxy-settings') {
          if (request.method === 'GET' || request.method === 'HEAD') {
            respondJson(request, response, 200, await proxySettings.get());
          } else if (request.method === 'PUT') {
            respondJson(request, response, 200, await proxySettings.save(await readJson(request, 16 * 1024)));
          } else {
            respondJson(request, response, 405, { error: 'Method not allowed' }, { Allow: 'GET, HEAD, PUT' });
          }
        } else if (pathname === '/api/translate-batch') {
          if (request.method !== 'POST') {
            respondJson(request, response, 405, { error: 'Method not allowed' }, { Allow: 'POST' });
          } else {
            const payload = await readJson(request);
            const service = await getProviders();
            const result = await service.translateBatch(payload, { signal: controller.signal, env: await proxySettings.effectiveEnv(env) });
            respondJson(request, response, 200, result);
          }
        } else {
          respondJson(request, response, 404, { error: 'Not found' });
        }
      } catch (error) {
        const safe = error?.public === true;
        const status = safe && Number.isInteger(error.status) && error.status >= 400 && error.status <= 599 ? error.status : 500;
        respondJson(request, response, status, { error: safe ? error.message : '翻訳処理に失敗しました。サーバー設定と翻訳サービスの状態を確認してください。' });
      } finally {
        response.off('close', onClose);
        request.resume();
      }
      return;
    }

    if (request.method !== 'GET' && request.method !== 'HEAD') {
      respond(request, response, 405, 'Method not allowed', { Allow: 'GET, HEAD' });
      request.resume();
      return;
    }
    const segments = pathname.split('/').filter(Boolean);
    if (segments.some(segment => segment.startsWith('.'))) {
      respond(request, response, 403, 'Forbidden');
      return;
    }
    if (pathname === '/') pathname = '/index.html';

    try {
      const canonicalRoot = await realpath(resolvedRoot);
      const candidate = path.resolve(canonicalRoot, `.${pathname}`);
      if (!isWithin(canonicalRoot, candidate)) {
        respond(request, response, 403, 'Forbidden');
        return;
      }
      const canonicalFile = await realpath(candidate);
      if (!isWithin(canonicalRoot, canonicalFile)) {
        respond(request, response, 403, 'Forbidden');
        return;
      }
      const file = await stat(canonicalFile);
      if (!file.isFile()) {
        respond(request, response, 404, 'Not found');
        return;
      }
      response.writeHead(200, {
        'Content-Type': mimeTypes.get(path.extname(canonicalFile).toLowerCase()) || 'application/octet-stream',
        'Content-Length': file.size,
      });
      if (request.method === 'HEAD') {
        response.end();
        return;
      }
      const stream = createReadStream(canonicalFile);
      stream.on('error', () => response.destroy());
      response.on('close', () => stream.destroy());
      stream.pipe(response);
    } catch (error) {
      if (response.headersSent) {
        response.destroy();
        return;
      }
      const absent = ['ENOENT', 'ENOTDIR', 'EACCES', 'EPERM'].includes(error.code);
      respond(request, response, absent ? 404 : 500, absent ? 'Not found' : 'Unable to read file');
    }
  });
  server.headersTimeout = 10_000;
  server.requestTimeout = 30_000;
  return server;
}

const isMain = process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url;
if (isMain) {
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 24 || (major === 24 && minor < 5)) {
    console.error('Node.js 24.5 以上が必要です。');
    process.exit(1);
  }
  try { process.loadEnvFile(envPath); }
  catch (error) {
    if (error.code !== 'ENOENT') {
      console.error('.env ファイルを読み込めませんでした。');
      process.exit(1);
    }
  }
  const rawPort = process.env.PORT || '4173';
  const port = Number(rawPort);
  if (!/^\d+$/.test(rawPort) || !Number.isInteger(port) || port < 1 || port > 65535) {
    console.error('PORT は 1〜65535 の整数を指定してください。');
    process.exitCode = 1;
  } else {
    const server = createStaticServer();
    server.on('error', error => {
      console.error(error.code === 'EADDRINUSE'
        ? `ポート ${port} は使用中です。PORT 環境変数で別のポートを指定してください。`
        : `起動できませんでした: ${error.message}`);
      process.exitCode = 1;
    });
    server.listen(port, '127.0.0.1', () => {
      console.log(`PDF Lingua: http://127.0.0.1:${port}`);
      console.log('終了するには Ctrl+C を押してください。');
    });
  }
}
