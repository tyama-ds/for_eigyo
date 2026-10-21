import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { mkdtemp, rm } from 'node:fs/promises';
import { createStaticServer } from '../server.mjs';
import { translateBatch } from '../providers.mjs';
import { translateBlocks } from '../dist/translator.js';
import { createTranslationJob, getPendingJobBlocks, applyTranslationBatch } from '../dist/translation-job.js';

test('client, server and job retain completed batches across 504 and resume only unfinished IDs', async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'pdf-resume-integration-'));
  const requests = [];
  const providers = { translateBatch: (payload, options) => translateBatch(payload, {
    ...options,
    request: async (_url, body, options) => {
      const blocks = JSON.parse(body.messages[1].content).blocks;
      requests.push({ ids: blocks.map(block => block.id), timeoutMs: options.timeoutMs });
      if (requests.length === 2) throw Object.assign(new Error('APIの応答待ち時間を超えました。'), { public: true, status: 504, code: 'REQUEST_TIMEOUT' });
      return { choices: [{ finish_reason: 'stop', message: { content: JSON.stringify({ translations: blocks.map(block => ({ id: block.id, text: `訳:${block.text}` })) }) } }] };
    },
  }) };
  const server = createStaticServer({
    providers, env: {},
    proxySettingsPath: path.join(directory, 'proxy-settings.json'),
    translationSettingsPath: path.join(directory, 'translation-settings.json'),
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    await new Promise(resolve => { server.closeAllConnections(); server.close(resolve); });
    await rm(directory, { recursive: true, force: true });
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const blocks = Array.from({ length: 5 }, (_, index) => ({ id: `block-${index}`, original: `Text ${index}`, translation: `previous-${index}`, enabled: true, supported: true }));
  const job = createTranslationJob(blocks, 'document-one:local:Japanese');
  const config = { provider: 'local', model: 'synthetic-model', targetLanguage: '日本語', maxBatchBlocks: 2, providerTimeoutMs: 900000 };
  const checkpoints = [];
  const options = {
    fetchImpl: (url, options) => fetch(new URL(url, base), options),
    onBatch: value => { applyTranslationBatch(job, value); checkpoints.push(value.blocks.map(block => block.id)); },
  };
  const pending = () => getPendingJobBlocks(job).map(block => ({ id: block.id, text: block.original }));
  await assert.rejects(translateBlocks(pending(), config, options), error => error.status === 504 && !error.retryable);
  assert.equal(requests.length, 2, '504 does not issue an automatic retry');
  assert.deepEqual(checkpoints, [['block-0', 'block-1']]);
  assert.deepEqual(blocks.map(block => block.translation), ['訳:Text 0', '訳:Text 1', 'previous-2', 'previous-3', 'previous-4']);
  assert.deepEqual(pending().map(block => block.id), ['block-2', 'block-3', 'block-4']);
  await translateBlocks(pending(), { ...config, maxBatchBlocks: 1 }, options);
  assert.deepEqual(requests.map(request => request.ids), [
    ['block-0', 'block-1'], ['block-2', 'block-3'], ['block-2'], ['block-3'], ['block-4'],
  ]);
  assert.ok(requests.every(request => request.timeoutMs === 900000));
  assert.deepEqual(pending(), []);
  assert.equal(job.completed.size, 5);
  assert.deepEqual(blocks.map(block => block.translation), blocks.map(block => `訳:${block.original}`));
});
