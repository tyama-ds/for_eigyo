import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { once } from 'node:events';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startApplication } from '../launch.mjs';

async function tempDirectory(t) {
  const temp = await mkdtemp(path.join(os.tmpdir(), 'pdf-lingua-launcher-'));
  t.after(async () => {
    assert.ok(path.resolve(temp).startsWith(path.resolve(os.tmpdir()) + path.sep));
    await rm(temp, { recursive: true, force: true });
  });
  return temp;
}

test('launcher loads an extra CA before child startup and propagates the child exit code', async t => {
  const temp = await tempDirectory(t);
  const environmentFile = path.join(temp, '.env');
  const entrypoint = path.join(temp, 'inspect-child.mjs');
  const certificatePath = fileURLToPath(new URL('./fixtures/localhost-test-cert.pem', import.meta.url));
  const originalCa = process.env.NODE_EXTRA_CA_CERTS;
  const originalMarker = process.env.PDF_LINGUA_LAUNCHER_TEST;
  delete process.env.NODE_EXTRA_CA_CERTS;
  delete process.env.PDF_LINGUA_LAUNCHER_TEST;
  t.after(() => {
    if (originalCa === undefined) delete process.env.NODE_EXTRA_CA_CERTS;
    else process.env.NODE_EXTRA_CA_CERTS = originalCa;
    if (originalMarker === undefined) delete process.env.PDF_LINGUA_LAUNCHER_TEST;
    else process.env.PDF_LINGUA_LAUNCHER_TEST = originalMarker;
  });
  await writeFile(environmentFile, `NODE_EXTRA_CA_CERTS=${certificatePath.replaceAll('\\', '/')}\nPDF_LINGUA_LAUNCHER_TEST=loaded-before-start\n`);
  await writeFile(entrypoint, `import tls from 'node:tls'; process.stdout.write(JSON.stringify({ extraCAs: tls.getCACertificates('extra').length, marker: process.env.PDF_LINGUA_LAUNCHER_TEST, cwd: process.cwd() })); process.exitCode = 7;`);
  const listenerCount = process.listenerCount('SIGTERM');
  const { child, exited } = startApplication({ cwd: temp, environmentFile, entrypoint, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '';
  let errors = '';
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { errors += chunk; });
  const closed = once(child, 'close');
  assert.equal(await exited, 7);
  await closed;
  assert.equal(errors, '');
  const result = JSON.parse(output);
  assert.equal(result.extraCAs, 1);
  assert.equal(result.marker, 'loaded-before-start');
  assert.equal(path.resolve(result.cwd), path.resolve(temp));
  assert.equal(process.listenerCount('SIGTERM'), listenerCount);
});

test('launcher permits a missing .env and forwards termination to its child', async t => {
  const temp = await tempDirectory(t);
  const entrypoint = path.join(temp, 'long-running-child.mjs');
  await writeFile(entrypoint, 'process.stdout.write("ready"); setInterval(() => {}, 1000);');
  const { child, exited } = startApplication({ cwd: temp, environmentFile: path.join(temp, '.env'), entrypoint, stdio: ['ignore', 'pipe', 'pipe'] });
  await once(child.stdout, 'data');
  process.emit('SIGTERM');
  const exitCode = await exited;
  assert.notEqual(exitCode, 0);
  assert.ok(child.killed);
});

test('launcher rejects unreadable configuration without exposing its contents', async t => {
  const temp = await tempDirectory(t);
  assert.throws(() => startApplication({ environmentFile: temp }), error => error.message === '.env ファイルを読み込めませんでした。');
});
