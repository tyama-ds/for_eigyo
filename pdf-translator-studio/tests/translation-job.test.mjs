import test from 'node:test';
import assert from 'node:assert/strict';
import { createTranslationJob, getPendingJobBlocks, applyTranslationBatch } from '../dist/translation-job.js';
import { translateBlocks } from '../dist/translator.js';

function fixture() {
  const blocks = [
    { id: 'a', original: 'First paragraph', translation: '', enabled: true, supported: true },
    { id: 'b', original: 'Second paragraph', translation: '既存の訳文', enabled: true, supported: true },
    { id: 'c', original: 'Third paragraph', translation: '', enabled: true, supported: true },
  ];
  return { blocks, job: createTranslationJob(blocks, 'document+selection+model+languages') };
}

function batch(blocks) {
  return {
    blocks: blocks.map(({ id, original }) => ({ id, text: original })),
    translations: Object.fromEntries(blocks.map(({ id }) => [id, `翻訳 ${id}`])),
  };
}

test('retains successful batches and resumes only unfinished references in source order', () => {
  const { blocks, job } = fixture();
  assert.equal(job.contextKey, 'document+selection+model+languages');
  assert.equal(job.total, 3);
  assert.equal(job.completed.size, 0);
  assert.equal(getPendingJobBlocks(job)[0], blocks[0]);
  assert.equal(applyTranslationBatch(job, batch([blocks[0]])), 1);
  assert.equal(blocks[0].translation, '翻訳 a');
  assert.deepEqual([...job.completed], ['a']);
  assert.deepEqual(getPendingJobBlocks(job), blocks.slice(1));
  assert.equal(applyTranslationBatch(job, batch(blocks.slice(1))), 2);
  assert.deepEqual(getPendingJobBlocks(job), []);
  assert.equal(job.completed.size, 3);
});

test('client checkpoints resume after HTTP 504 without resending completed text, with revised wait and batch settings', async () => {
  const { blocks, job } = fixture();
  const requests = [];
  const config = { provider: 'local', model: 'local-model', targetLanguage: 'Japanese', maxRetries: 2, maxBatchBlocks: 1 };
  const source = () => getPendingJobBlocks(job).map(({ id, original }) => ({ id, text: original }));
  const fetchImpl = async (_url, options) => {
    const request = JSON.parse(options.body);
    requests.push(request);
    if (requests.length === 3) return new Response('{}', { status: 504 });
    return new Response(JSON.stringify({ translations: request.blocks.map(({ id }) => ({ id, text: `翻訳 ${id}` })) }));
  };
  await assert.rejects(translateBlocks(source(), config, {
    fetchImpl, onBatch: value => applyTranslationBatch(job, value),
  }), { status: 504 });
  assert.equal(requests.length, 3);
  assert.equal(job.completed.size, 2);
  assert.deepEqual(blocks.map(block => block.translation), ['翻訳 a', '翻訳 b', '']);
  await translateBlocks(source(), { ...config, maxBatchBlocks: 4, providerTimeoutMs: 600000 }, {
    fetchImpl, onBatch: value => applyTranslationBatch(job, value),
  });
  assert.deepEqual(requests.map(request => request.blocks.map(block => block.id)), [['a'], ['b'], ['c'], ['c']]);
  assert.equal(requests[3].config.providerTimeoutMs, 600000);
  assert.equal(job.completed.size, 3);
  assert.deepEqual(getPendingJobBlocks(job), []);
});

test('duplicate, unknown, missing, empty and mismatched batches fail before any writes', () => {
  const corruptions = [
    value => value.blocks.push(value.blocks[0]),
    value => { value.blocks[1].id = 'unknown'; },
    value => { delete value.translations.b; },
    value => { value.translations.unknown = '余分'; },
    value => { value.translations.b = ' '; },
    value => { value.blocks[1].text = 'Changed input'; },
    value => { value.translations = { a: '一部' }; },
  ];
  for (const corrupt of corruptions) {
    const { blocks, job } = fixture();
    const before = blocks.map(block => block.translation);
    const value = batch(blocks.slice(0, 2));
    corrupt(value);
    assert.throws(() => applyTranslationBatch(job, value), { code: 'TRANSLATION_JOB_CHANGED' });
    assert.deepEqual(blocks.map(block => block.translation), before);
    assert.equal(job.completed.size, 0);
  }
});

test('changed source, translation or eligibility rejects resume and an entire in-flight batch atomically', () => {
  for (const change of [
    block => { block.original = 'Edited source'; },
    block => { block.id = 'changed-id'; },
    block => { block.translation = '手動編集'; },
    block => { block.enabled = false; },
    block => { block.supported = false; },
    block => { block.supported = undefined; },
  ]) {
    const { blocks, job } = fixture();
    const value = batch(blocks.slice(0, 2));
    change(blocks[1]);
    const changed = blocks.map(block => block.translation);
    assert.throws(() => getPendingJobBlocks(job), { code: 'TRANSLATION_JOB_CHANGED' });
    assert.throws(() => applyTranslationBatch(job, value), { code: 'TRANSLATION_JOB_CHANGED' });
    assert.deepEqual(blocks.map(block => block.translation), changed);
    assert.equal(job.completed.size, 0);
  }
});

test('completed regions are never overwritten even if included after an unfinished region', () => {
  const { blocks, job } = fixture();
  applyTranslationBatch(job, batch([blocks[0]]));
  blocks[0].translation = '完了後の手動編集';
  assert.deepEqual(getPendingJobBlocks(job), blocks.slice(1));
  assert.throws(() => applyTranslationBatch(job, batch([blocks[1], blocks[0]])), { code: 'TRANSLATION_JOB_CHANGED' });
  assert.equal(blocks[0].translation, '完了後の手動編集');
  assert.equal(blocks[1].translation, '既存の訳文');
  assert.deepEqual([...job.completed], ['a']);
});

test('validates input jobs and supports harmless prototype-like IDs without inherited values', () => {
  for (const blocks of [
    [{ id: 'a', original: 'A' }, { id: 'a', original: 'B' }],
    [{ id: '', original: 'A' }], [{ id: 'a', original: '  ' }],
    [{ id: 'a', original: 'A', enabled: false }], [{ id: 'a', original: 'A', supported: false }],
  ]) assert.throws(() => createTranslationJob(blocks, 'context'), { code: 'TRANSLATION_JOB_CHANGED' });
  assert.throws(() => getPendingJobBlocks({}), { code: 'TRANSLATION_JOB_CHANGED' });
  assert.deepEqual(getPendingJobBlocks(createTranslationJob([], 'empty')), []);
  const block = { id: '__proto__', original: 'Text', translation: '' };
  const job = createTranslationJob([block], 'context');
  assert.throws(() => applyTranslationBatch(job, {
    blocks: [{ id: '__proto__', text: 'Text' }], translations: Object.create({ __proto__: 'inherited' }),
  }), { code: 'TRANSLATION_JOB_CHANGED' });
  applyTranslationBatch(job, { blocks: [{ id: '__proto__', text: 'Text' }], translations: JSON.parse('{"__proto__":"翻訳"}') });
  assert.equal(block.translation, '翻訳');
  assert.equal(job.completed.has('__proto__'), true);
});
