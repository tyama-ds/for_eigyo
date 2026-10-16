import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const code = readFileSync(new URL('../static/scopus-dates.js', import.meta.url), 'utf8');
const datasetID = 'a'.repeat(32), jobID = 'b'.repeat(32), derivedID = 'c'.repeat(32), otherDatasetID = 'd'.repeat(32), snapshotID = 'e'.repeat(32), batchID = 'f'.repeat(32);
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};};
const job = (extra = {}) => ({id: jobID, dataset_id: datasetID, target: 'missing_month', status: 'partial', unique_eids: 20000, eligible_papers: 21000, pending_eids: 19900, processed_eids: 100, found_eids: 90, error_eids: 10, ...extra});
const library = (jobs = []) => ({coverage: {total_papers: 22000, month_or_day: 1000, missing_month: 21000, eligible_identity: 21000, no_eid: 800, identity_conflicts: 200}, jobs});
const page = (extra = {}) => ({items: [], offset: 0, limit: 20, total: 0, snapshot_id: snapshotID, ...extra});
const queueFile = () => ({schema: 'research-atlas.scopus-date-queue.v1', job_id: jobID, batch_id: batchID, created_at: '2026-10-14T01:00:00Z', entries: [{eid: '2-s2.0-1234', url: 'https://www.scopus.com/pages/publications/1234?origin=resultslist', title: 'A paper'}]});
const resultFile = (extra = {}) => ({schema: 'research-atlas.scopus-date-results.v1', job_id: jobID, batch_id: batchID, observations: [{eid: '2-s2.0-1234', url: 'https://www.scopus.com/pages/publications/1234?origin=resultslist', status: 'found', date_text: '15 October 2024', matched_by: 'label', observed_at: '2026-10-14T01:00:00Z'}], ...extra});
const file = (value = resultFile(), extra = {}) => {const text = JSON.stringify(value); return {name: 'scopus-results.json', size: Buffer.byteLength(text), text: async () => text, ...extra};};
const attrs = text => Object.fromEntries([...text.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(match => [match[1], match[2] ?? '']));

function harness(responder) {
  const calls = [], applied = [], downloads = [], blobs = new Map(); let dialog;
  class Node {
    constructor(owner, attributes = {}) {this.owner = owner; this.attributes = attributes; this.value = attributes.value || ''; this.checked = 'checked' in attributes; this.disabled = 'disabled' in attributes; this.hidden = false; this.dataset = {}; this.textContent = ''; this._html = ''; this.events = new Map(); for (const [key, value] of Object.entries(attributes)) if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;}
    set innerHTML(value) {this._html = value; this.owner?.register(value);}
    get innerHTML() {return this._html;}
    addEventListener(name, callback) {this.events.set(name, [...(this.events.get(name) || []), callback]);}
    dispatch(name, event = {}) {for (const callback of this.events.get(name) || []) callback(event);}
    setAttribute(name, value) {this.attributes[name] = value;}
    closest(selector) {return selector === '[data-sd-action]' && this.dataset.sdAction ? this : null;}
  }
  class Dialog extends Node {
    constructor() {super(null); this.owner = this; this.nodes = new Map(); this.open = false;}
    set innerHTML(value) {this.nodes.clear(); this._html = value; this.register(value);}
    get innerHTML() {return this._html;}
    register(html) {
      for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/g)) {
        const a = attrs(match[2]), keys = [];
        if (a.id) keys.push(`#${a.id}`);
        if (a['data-sd-field']) keys.push(`[data-sd-field="${a['data-sd-field']}"]`);
        if (a['data-sd-action']) keys.push(`[data-sd-action="${a['data-sd-action']}"]`);
        if (keys.length) {const node = new Node(this, a); for (const key of keys) this.nodes.set(key, node);}
      }
      for (const match of html.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)) {
        const a = attrs(match[1]), node = this.nodes.get(`[data-sd-field="${a['data-sd-field']}"]`); if (!node) continue;
        const options = [...match[2].matchAll(/<option\b([^>]*)>/g)].map(item => attrs(item[1])); node.value = (options.find(item => 'selected' in item) || options[0] || {}).value || '';
      }
    }
    querySelector(selector) {return this.nodes.get(selector) || null;}
    showModal() {this.open = true;}
    close() {this.open = false; this.dispatch('close');}
  }
  const window = {__ATLAS_UI_TEST__: true};
  const document = {createElement(type) {if (type === 'dialog') {dialog = new Dialog(); return dialog;} return {click() {downloads.push({filename: this.download, blob: blobs.get(this.href)});}, remove() {}};}, body: {appendChild() {}}};
  const sandbox = vm.createContext({window, document, URLSearchParams, console, Promise, Map, Set, Blob, URL: {createObjectURL(blob) {const id = `blob:${blobs.size}`; blobs.set(id, blob); return id;}, revokeObjectURL(id) {blobs.delete(id);}}, setTimeout() {}});
  vm.runInContext(code, sandbox);
  const context = {dataset: {id: datasetID, name: 'Scopus <steel>', paper_count: 22000}, onApplied: value => applied.push(value), api: async (url, options) => {
    calls.push({url, options}); if (responder) {const value = responder(url, options); if (value !== undefined) return value;}
    if (url.startsWith('/api/datasets/')) return library();
    if (url.includes('/records?')) return page();
    if (url.endsWith('/queue')) return queueFile();
    if (url.endsWith('/import')) return {imported: 1};
    if (url.endsWith('/apply')) return {dataset: {id: derivedID, name: '補完版'}, applied_count: 90};
    return job();
  }};
  return {t: window.__scopusDatesTest, context, calls, applied, downloads, get dialog() {return dialog;}, field: key => dialog.querySelector(`[data-sd-field="${key}"]`), node: selector => dialog.querySelector(selector), change(key) {dialog.dispatch('change', {target: this.field(key)});}, click(name) {dialog.dispatch('click', {target: dialog.querySelector(`[data-sd-action="${name}"]`), preventDefault() {}});}};
}

test('initial dialog explains browser workflow and preserves date defaults', async () => {
  const h = harness(); await h.t.open(h.context);
  assert.equal(h.calls[0].url, `/api/datasets/${datasetID}/scopus-dates`);
  assert.equal(h.field('batch').value, '100'); assert.equal(h.field('target').value, 'missing_month');
  assert.equal(h.field('allow-year').checked, false); assert.equal(h.field('overwrite').checked, false);
  assert.equal(h.field('file').disabled, true); assert.equal(h.node('[data-sd-action="apply"]').disabled, true);
  assert.match(h.dialog.innerHTML, /Scopus &lt;steel&gt;/); assert.match(h.dialog.innerHTML, /\/api\/scopus-browser-extension/);
  assert.match(h.dialog.innerHTML, /ログイン情報・Cookie・抄録・本文/);
});

test('queue creates a dataset-scoped job and downloads only the bounded queue', async () => {
  const h = harness(); await h.t.open(h.context); h.field('target').value = 'missing_day';
  await h.t.queue();
  const create = h.calls.find(call => call.url === '/api/scopus-date-jobs');
  assert.deepEqual(JSON.parse(create.options.body), {dataset_id: datasetID, target: 'missing_day'});
  assert.deepEqual(JSON.parse(h.calls.find(call => call.url.endsWith('/queue')).options.body), {batch_size: 100, retry_errors: false});
  assert.equal(h.downloads.length, 1); assert.match(h.downloads[0].filename, /^scopus-date-queue-/);
  assert.deepEqual(JSON.parse(await h.downloads[0].blob.text()), queueFile());
  assert.equal(h.t.state.snapshot, snapshotID); assert.match(h.t.state.notice, /1件/);
});

test('continuing a saved job passes explicit retry settings without creating another job', async () => {
  const h = harness(); await h.t.open(h.context); await h.t.loadJob(jobID); h.field('batch').value = '500'; h.field('retry').checked = true;
  await h.t.queue(); assert.equal(h.calls.filter(call => call.url === '/api/scopus-date-jobs').length, 0);
  assert.deepEqual(JSON.parse(h.calls.find(call => call.url.endsWith('/queue')).options.body), {batch_size: 500, retry_errors: true});
});

test('invalid batch sizes cannot create jobs or request queues', async () => {
  for (const value of ['', '0', '501', '1.5', '-3', 'Infinity', 'NaN']) {
    const h = harness(); await h.t.open(h.context); h.field('batch').value = value; await h.t.queue();
    assert.equal(h.calls.filter(call => call.options?.method === 'POST').length, 0, value);
    assert.match(h.t.state.error, /1〜500の整数/); assert.equal(h.t.state.busy, false);
  }
});

test('empty queue reports completion without saving a misleading file', async () => {
  const h = harness(url => url.endsWith('/queue') ? {...queueFile(), entries: []} : undefined); await h.t.open(h.context); await h.t.queue();
  assert.equal(h.downloads.length, 0); assert.match(h.t.state.notice, /追加取得するEIDはありません/);
});

test('history restores job and records, while another dataset job is rejected', async () => {
  const h = harness(url => url.startsWith('/api/datasets/') ? library([job({target: 'all'})]) : url === `/api/scopus-date-jobs/${jobID}` ? job({target: 'all'}) : undefined);
  await h.t.open(h.context); assert.equal(h.field('target').value, 'all'); assert.equal(h.t.state.snapshot, snapshotID);
  const wrong = harness(url => url === `/api/scopus-date-jobs/${jobID}` ? job({dataset_id: otherDatasetID}) : undefined);
  await wrong.t.open(wrong.context); await wrong.t.loadJob(jobID); assert.equal(wrong.t.state.job, null); assert.match(wrong.t.state.error, /データセットが一致/);
});

test('paging, exports and apply use the same fixed preview snapshot', async () => {
  const h = harness(); await h.t.open(h.context); await h.t.loadJob(jobID);
  h.t.state.offset = 20; await h.t.records();
  assert.match(h.calls.at(-1).url, new RegExp(`offset=20.*snapshot_id=${snapshotID}`));
  assert.match(h.node('#sd-status').innerHTML, new RegExp(`format=csv.*snapshot_id=${snapshotID}`));
  assert.match(h.node('#sd-status').innerHTML, /format=json/);
  await h.t.apply();
  assert.deepEqual(JSON.parse(h.calls.find(call => call.url.endsWith('/apply')).options.body), {allow_year_change: false, overwrite_existing: false, snapshot_id: snapshotID});
  assert.equal(h.applied.length, 1); assert.equal(h.applied[0].dataset.id, derivedID); assert.equal(h.dialog.open, false);
});

test('a policy change discards the old snapshot and warns about annual counts', async () => {
  const h = harness(); await h.t.open(h.context); await h.t.loadJob(jobID); h.t.state.offset = 20;
  h.field('allow-year').checked = true; h.change('allow-year'); await tick();
  const last = h.calls.filter(call => call.url.includes('/records?')).at(-1);
  assert.match(last.url, /offset=0.*allow_year_change=true/); assert.doesNotMatch(last.url, /snapshot_id=/);
  assert.match(h.node('#sd-policy-note').textContent, /出版年・年度別の論文数が変わる/);
});

test('import validates identity and reloads a fresh snapshot', async () => {
  const h = harness(); await h.t.open(h.context); await h.t.loadJob(jobID); h.t.state.offset = 20;
  await h.t.importResult(file());
  const call = h.calls.find(item => item.url.endsWith('/import'));
  assert.deepEqual(JSON.parse(call.options.body), resultFile()); assert.equal(h.t.state.offset, 0);
  assert.doesNotMatch(h.calls.filter(item => item.url.includes('/records?')).at(-1).url, /snapshot_id=/);
  assert.match(h.t.state.notice, /日付台帳へ保存/); assert.equal(h.field('file').value, '');
});

test('malformed, oversized and wrong-job result files never reach the API', async () => {
  const invalid = [file(resultFile({job_id: otherDatasetID})), file(resultFile({schema: 'other'})), file(resultFile({batch_id: '../bad'})), file(resultFile({observations: Array(501).fill({})})), file(null), file(resultFile(), {size: 1024 * 1024 + 1}), file(resultFile(), {name: 'result.html'}), file(resultFile(), {text: async () => '{broken'})];
  for (const candidate of invalid) {
    const h = harness(); await h.t.open(h.context); await h.t.loadJob(jobID); await h.t.importResult(candidate);
    assert.equal(h.calls.filter(call => call.url.endsWith('/import')).length, 0); assert.ok(h.t.state.error); assert.equal(h.t.state.busy, false);
  }
});

test('an old file read cannot import into another dataset after reopening', async () => {
  const pending = deferred(), h = harness(); await h.t.open(h.context); await h.t.loadJob(jobID);
  const loading = h.t.importResult(file(resultFile(), {text: () => pending.promise})); await tick(); h.dialog.close();
  await h.t.open({...h.context, dataset: {id: otherDatasetID, name: 'New corpus'}}); pending.resolve(JSON.stringify(resultFile())); await loading;
  assert.equal(h.calls.filter(call => call.url.endsWith('/import')).length, 0); assert.equal(h.t.state.error, '');
});

test('late job creation cannot download a queue after the dialog was closed', async () => {
  const pending = deferred(), h = harness(url => url === '/api/scopus-date-jobs' ? pending.promise : undefined);
  await h.t.open(h.context); const work = h.t.queue(); await tick(); h.dialog.close(); pending.resolve(job()); await work;
  assert.equal(h.downloads.length, 0); assert.equal(h.calls.filter(call => call.url.endsWith('/queue')).length, 0);
});

test('late queue response cannot download into a new dataset session', async () => {
  const pending = deferred(), h = harness(url => url.endsWith('/queue') ? pending.promise : undefined);
  await h.t.open(h.context); const work = h.t.queue(); await tick(); h.dialog.close(); await h.t.open({...h.context, dataset: {id: otherDatasetID, name: 'Other'}});
  pending.resolve(queueFile()); await work; assert.equal(h.downloads.length, 0); assert.equal(h.t.state.job, null);
});

test('obsolete library failure cannot appear after reopening another dataset', async () => {
  const pending = deferred(), h = harness(url => url.startsWith(`/api/datasets/${datasetID}/`) ? pending.promise : undefined);
  const work = h.t.open(h.context); await tick(); h.dialog.close(); await h.t.open({...h.context, dataset: {id: otherDatasetID, name: 'Other'}});
  pending.reject(new Error('Old corpus error')); await work; assert.equal(h.t.state.error, ''); assert.equal(h.t.state.context.dataset.id, otherDatasetID);
});

test('outdated preview failure cannot erase a newer successful preview', async () => {
  const old = deferred(); let requests = 0;
  const h = harness(url => url.includes('/records?') && requests++ === 0 ? old.promise : undefined);
  await h.t.open(h.context); h.t.state.job = job(); const work = h.t.records();
  h.field('allow-year').checked = true; h.t.resetPreview(); await h.t.records(); old.reject(new Error('Old preview failed')); await work;
  assert.equal(h.t.state.error, ''); assert.equal(h.t.state.snapshot, snapshotID); assert.equal(h.t.state.recordsBusy, false);
});

test('preview lacking a fixed snapshot cannot be applied', async () => {
  const h = harness(url => url.includes('/records?') ? page({snapshot_id: ''}) : undefined);
  await h.t.open(h.context); await h.t.loadJob(jobID); await h.t.apply();
  assert.match(h.t.state.error, /プレビューを固定/); assert.equal(h.calls.filter(call => call.url.endsWith('/apply')).length, 0);
});

test('late apply response cannot switch the newly selected corpus', async () => {
  const pending = deferred(), h = harness(url => url.endsWith('/apply') ? pending.promise : undefined);
  await h.t.open(h.context); await h.t.loadJob(jobID); const work = h.t.apply(); await tick(); h.dialog.close();
  await h.t.open({...h.context, dataset: {id: otherDatasetID, name: 'Other'}}); pending.resolve({dataset: {id: derivedID}}); await work;
  assert.equal(h.applied.length, 0); assert.equal(h.t.state.context.dataset.id, otherDatasetID); assert.equal(h.dialog.open, true);
});

test('an invalid derived dataset response stays visible as an error', async () => {
  const h = harness(url => url.endsWith('/apply') ? {dataset: {id: '../bad'}} : undefined); await h.t.open(h.context); await h.t.loadJob(jobID); await h.t.apply();
  assert.equal(h.applied.length, 0); assert.equal(h.dialog.open, true); assert.match(h.t.state.error, /補完版データセット/);
});

test('date evidence is escaped and unadopted conflict never replaces the saved date', async () => {
  const h = harness(); await h.t.open(h.context);
  const html = h.t.recordRow({title: '<img src=x onerror=alert(1)>', eid: '2-s2.0-1234', status: 'found', original: {publication_date: '2024-05', date_precision: 'month'}, observation: {date_text: '2 April 2023 <script>x</script>', observed_at: '2026-10-14T01:00:00Z', matched_by: 'label'}, proposed: {candidate: {date: '2023-04-02'}, apply: false, conflict: true, reason: 'year_conflict'}});
  assert.match(html, /&lt;img/); assert.doesNotMatch(html, /<img|<script/);
  const saved = html.split('<td>').at(-1); assert.match(saved, /<strong>2024-05<\/strong>/); assert.doesNotMatch(saved, /2023-04-02/); assert.match(saved, /元の出版年と異なる/); assert.match(saved, /⚠/);
  assert.match(html, /Publication date の項目名/);
});

test('future date explicitly remains outside the saved dataset', async () => {
  const h = harness(); await h.t.open(h.context);
  const html = h.t.recordRow({original: {year: 2027}, proposed: {candidate: {date: '2027-10-01'}, apply: false, reason: 'future_date'}});
  assert.match(html, /未来の日付は補完版への採用対象外/); assert.match(html.split('<td>').at(-1), /<strong>2027<\/strong>/);
});

test('status counts are EID counts and invalid dataset ids are rejected', async () => {
  const h = harness(); await h.t.open(h.context); await h.t.loadJob(jobID);
  assert.match(h.node('#sd-status').innerHTML, /100 \/ 20,000 EID/); assert.match(h.node('#sd-status').innerHTML, /対象論文は21,000件/);
  const invalid = harness(); await assert.rejects(invalid.t.open({...invalid.context, dataset: {id: '../other'}}), /データセットを選択/); assert.equal(invalid.calls.length, 0);
});

test('integration loads the module and exposes Scopus through the existing date dialog', () => {
  const html = readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
  const existing = readFileSync(new URL('../static/publication-dates.js', import.meta.url), 'utf8');
  assert.match(html, /src="\/static\/scopus-dates\.js"/); assert.match(html, /href="\/static\/scopus-dates\.css"/);
  assert.match(existing, /data-pd-action="scopus"/); assert.match(existing, /AtlasScopusDates\.open\(context\)/);
});
