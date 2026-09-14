import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
function context(functions, values) {
  const sandbox = vm.createContext(values);
  for (const name of functions) {
    const line = source.split('\n').find(line => new RegExp(`^(?:async )?function ${name}\\(`).test(line));
    assert.ok(line, name);
    vm.runInContext(line, sandbox);
  }
  return sandbox;
}

test('source notice is safe before the initial dataset or result arrives', () => {
  const el = {};
  const ctx = context(['reportsForDataset', 'currentSourceReports', 'renderSourceNotice'], {
    state: { result: null, dataset: null, reportCache: new Map() }, $: () => el,
  });
  ctx.renderSourceNotice();
  assert.equal(el.hidden, true);
  assert.equal(ctx.currentSourceReports().length, 0);
  ctx.state.reportCache.set('saved', ['cached']);
  ctx.state.dataset = { id: 'saved' };
  assert.deepEqual(ctx.currentSourceReports(), ['cached']);
});

test('CSV import replaces stale years and monthly anchor before analysis', async () => {
  const elements = new Map();
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, { value: '', checked: false });
    return elements.get(selector);
  };
  $('#start-year').value = '2026'; $('#end-year').value = '2026';
  $('#monthly-anchor').value = '2026-08'; $('#csv-provider').value = 'scopus_csv';
  const dataset = { id: 'imported', name: 'sample.csv', paper_count: 100, is_demo: true,
    analysis_defaults: { start_year: 2025, end_year: 2025 } };
  const requests = [];
  let analyzed = false;
  const ctx = context(['applyDatasetAnalysisDefaults', 'importFile'], {
    state: { file: 'csv-file', importBusy: false, selectionVersion: 0, uploadMode: 'papers',
      uploadBase: null, reportCache: new Map() }, $, FormData,
    setImportBusy: () => {}, updateUploadMode: () => {}, updateDataset: () => {},
    closeDialogs: () => {}, refreshStatus: async () => {}, num: String,
    message: () => {}, api: async (url, options) => {
      requests.push([url, options.body.get('provider')]); return { dataset };
    }, runAnalysis: async () => {
      analyzed = true;
      assert.equal($('#start-year').value, 2025);
      assert.equal($('#end-year').value, 2025);
      assert.equal($('#monthly-anchor').value, '');
    },
  });
  await ctx.importFile();
  assert.equal(analyzed, true);
  assert.deepEqual(requests, [['/api/import', 'scopus_csv']]);
  assert.equal(ctx.state.dataset, dataset);
});

test('connection failures give a local startup hint and preserve server validation errors', async () => {
  const ctx = context(['api'], { window:{}, fetch: async () => { throw new TypeError('Failed to fetch'); } });
  await assert.rejects(ctx.api('/api/import'), /start.bat/);
  ctx.fetch = async () => ({ ok: false, json: async () => ({ detail: 'CSVの見出しがありません' }) });
  await assert.rejects(ctx.api('/api/import'), /CSVの見出しがありません/);
});

test('switching from a single-year CSV to the built-in demo restores its years', async () => {
  const elements = new Map(['#start-year','#end-year','#monthly-anchor'].map(id => [id,{value:'2025'}]));
  const dataset = {id:'demo', analysis_defaults:{start_year:2021,end_year:2025}};
  let analyzed = false;
  const ctx = context(['applyDatasetAnalysisDefaults','loadDemo'], {
    state: {selectionVersion:0}, $: id=>elements.get(id), closeDialogs:()=>{}, showLoading:()=>{},
    post:async()=>({dataset}), refreshStatus:async()=>{}, updateDataset:()=>{}, showError:error=>{throw error;},
    runAnalysis:async()=>{
      analyzed = true;
      assert.equal(elements.get('#start-year').value,2021);
      assert.equal(elements.get('#end-year').value,2025);
    },
  });
  await ctx.loadDemo();
  assert.ok(analyzed);
});

test('single-year data shows insufficient history instead of an unclassified-theme error', () => {
  const ctx = context(['renderForecasts','badge'], {
    state: { result: { meta: { annual_comparison_available:false } } },
    classifiedTopics:()=>[{id:'nmf-topic',forecast:[]}], activeTopic:()=>null,
    empty:(title,description)=>title+' '+description,
  });
  assert.match(ctx.renderForecasts(), /複数年の論文データを追加/);
  assert.doesNotMatch(ctx.renderForecasts(), /未分類/);
  assert.match(ctx.badge({status:'stable',is_outlier:false}), /年次未判定/);
});
