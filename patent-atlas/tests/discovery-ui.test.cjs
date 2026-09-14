const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/discovery-ui.js', 'utf8');
const key = 'IPC:H01M10/0562';
const candidate = {kind: 'IPC', code: 'H01M10/0562', title: 'Solid electrolytes', selectable: true, evidence_status: 'hypothesis'};

function setup() {
 const nodes = Object.fromEntries(['content', 'keywords', 'english', 'use-llm', 'round-limit', 'query-limit', 'result-limit', 'results', 'history', 'progress', 'manual-count', 'ops-status', 'plan-note', 'run', 'analyze', 'import', 'csv-file', 'adopt', 'picked-count'].map(name => ['#discovery-' + name, {innerHTML: '', value: '', files: [], textContent: '', disabled: false}]));
 nodes['#discovery-keywords'].value = 'typed draft';
 nodes['#discovery-round-limit'].value = '2'; nodes['#discovery-query-limit'].value = '6'; nodes['#discovery-result-limit'].value = '10';
 const context = {
  state: {keywords: 'saved topic', settings: {}, job: {status: 'idle'}, patents: [{id: 'JP1'}], selected: ['IPC:A01B1/00'], candidates: [], discovery: {status: 'planned', plan: {keywords: 'saved topic', facets: [], candidate_hypotheses: [candidate]}, rounds: [], logs: []}},
  activeTab: 'discovery', step: 0, selectionSave: Promise.resolve(),
  $: selector => nodes[selector] || null, $$: () => [], on() {},
  esc: value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char])),
  run: fn => fn, toast() {}, updateChrome() {}, captureClassDraft() {},
  setTab(tab) { context.activeTab = tab; }, setStep(step) { context.step = step; },
  api: async () => { throw new Error('Unexpected API call'); }, nodes,
 };
 vm.createContext(context); vm.runInContext(source, context);
 return context;
}

test('metadata supersedes a matching hypothesis without changing source selections', () => {
 const c = setup();
 c.state.discovery.analysis = {recommendations: [{...candidate, evidence_status: 'patent_metadata', support_count: 2}]};
 const items = c.discoveryItems();
 assert.equal(items.length, 1); assert.equal(items[0].support_count, 2);
 assert.deepEqual(c.state.selected, ['IPC:A01B1/00']);
 assert.equal(c.state.discovery.plan.candidate_hypotheses[0].evidence_status, 'hypothesis');
});

test('LLM feedback distinguishes legacy plans, offline plans and zero-change LLM responses', () => {
 const c=setup();
 assert.match(c.discoveryLlmProposalHtml({}),/反映記録がありません/);
 assert.match(c.discoveryLlmProposalHtml({llm_proposal:{used:false}}),/LLMを使わず/);
 const html=c.discoveryLlmProposalHtml({llm_proposal:{used:true,english_terms:[],facets:[],notices:['既存の語と同じため追加0件']}});
 assert.match(html,/主題語 0 · 観点 0/);assert.match(html,/追加0件/);assert.doesNotMatch(html,/LLMを使わず/);
 assert.match(html,/新規追加なし/);assert.doesNotMatch(html,/ここへ反映しました/);
});

test('LLM feedback shows actual additions and destinations while escaping model text', () => {
 const c=setup();
 const plan={llm_proposal:{used:true,english_terms:['autonomous vehicle'],facets:[{id:'process',label:'制御プロセス',terms:['軌道生成'],english_terms:['<img src=x>']}],candidate_keys:['IPC:B60W'],notices:['<script>bad()</script>']}};
 const html=c.discoveryLlmProposalHtml(plan);
 assert.match(html,/autonomous vehicle/);assert.match(html,/軌道生成/);assert.match(html,/反映先：探索計画の主題語/);
 assert.match(html,/反映先：探索の観点・分類候補/);assert.match(html,/IPC:B60W/);
 assert.ok(html.includes('&lt;img'));assert.ok(html.includes('&lt;script&gt;'));
 assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));
 assert.ok(c.discoveryResultsHtml({plan}).indexOf('LLM RESPONSE')<c.discoveryResultsHtml({plan}).indexOf('SEARCH FROM MULTIPLE ANGLES'));
});

test('failed plan creation preserves prior results and explicitly identifies them as previous', async () => {
 const c=setup();c.nodes['#discovery-llm-request-status']={innerHTML:''};
 const previous=JSON.stringify(c.state.discovery);
 c.api=async()=>{throw new Error('LLMのJSON形式が不正 <example>');};
 await assert.rejects(c.requestDiscovery('/discovery/plan',{use_llm:true}),/JSON/);
 assert.equal(JSON.stringify(c.state.discovery),previous);
 assert.match(c.nodes['#discovery-llm-request-status'].innerHTML,/変更前の計画/);
 assert.match(c.nodes['#discovery-llm-request-status'].innerHTML,/&lt;example&gt;/);
 c.api=async()=>({...c.state,discovery:{...c.state.discovery,plan:{keywords:'new topic',llm_proposal:{used:false}}}});
 await c.requestDiscovery('/discovery/plan',{use_llm:false});
 assert.equal(c.nodes['#discovery-llm-request-status'].innerHTML,'');
});

test('publications and query strings are escaped; synthetic evidence is explicitly identified', () => {
 const c = setup();
 const html = c.discoveryCandidateHtml({...candidate, title: '<script>bad()</script>', evidence_status: 'patent_metadata', support_count: 1, family_count: 1,
  evidence: [{patent_id: '"><img src=x>', title: '<b>not markup</b>', source: {provider: '<OPS>'}, synthetic: true}]});
 assert.ok(html.includes('架空サンプル由来')); assert.ok(html.includes('&lt;script&gt;'));
 assert.ok(!html.includes('<script>')); assert.ok(!html.includes('<img'));
 assert.ok(html.includes('&lt;OPS&gt;')); assert.ok(!html.includes('[object Object]'));
 const query = c.discoveryQueryHtml({query: 'ta="battery" AND <unsafe>', reason: '<reason>'});
 assert.ok(query.includes('data-discovery-query="ta=&quot;battery&quot; AND &lt;unsafe&gt;"'));
 assert.ok(query.includes('&lt;reason&gt;'));
});

test('poll renders preserve editable form values and checked candidate keys', () => {
 const c = setup();
 vm.runInContext(`discoveryPicked.add('${key}')`, c);
 c.renderDiscovery();
 const result = c.nodes['#discovery-results'].innerHTML;
 assert.ok(result.includes('checked')); assert.equal(c.nodes['#discovery-content'].innerHTML, '');
 c.nodes['#discovery-keywords'].value = 'unfinished input';
 c.state.discovery.logs.push({time: '12:00', message: 'Searching'});
 c.renderDiscovery();
 assert.equal(c.nodes['#discovery-keywords'].value, 'unfinished input');
 assert.equal(c.nodes['#discovery-results'].innerHTML, result);
 assert.ok(c.nodes['#discovery-history'].innerHTML.includes('Searching'));
 assert.equal(c.nodes['#discovery-analyze'].disabled, true, 'old plan must not silently analyze a changed topic');
});

test('CSV and OPS actions require a plan; OPS additionally requires credentials', () => {
 const c = setup(); c.state.discovery.plan = null;
 c.nodes['#discovery-csv-file'].files = [{name: 'patents.csv'}];
 c.updateDiscoveryControls(); assert.equal(c.nodes['#discovery-analyze'].disabled, true); assert.equal(c.nodes['#discovery-import'].disabled, true);
 c.state.discovery.plan = {keywords: 'typed draft'};
 c.updateDiscoveryControls(); assert.equal(c.nodes['#discovery-analyze'].disabled, false); assert.equal(c.nodes['#discovery-import'].disabled, false); assert.equal(c.nodes['#discovery-run'].disabled, true);
 c.state.settings = {ops_key_set: true, ops_secret_set: true};
 c.updateDiscoveryControls(); assert.equal(c.nodes['#discovery-run'].disabled, false);
});

test('search caps reject empty, fractional and out-of-range inputs', () => {
 const c = setup();
 assert.deepEqual(JSON.parse(JSON.stringify(c.discoveryLimits({max_rounds: '2', max_queries: '6', per_query: '10'}))), {max_rounds: 2, max_queries: 6, per_query: 10});
 for (const draft of [{max_rounds: '', max_queries: 6, per_query: 10}, {max_rounds: 1.5, max_queries: 6, per_query: 10}, {max_rounds: 2, max_queries: 13, per_query: 10}, {max_rounds: 2, max_queries: 6, per_query: 26}]) assert.throws(() => c.discoveryLimits(draft));
});

test('adoption waits for a pending manual selection and sends only explicitly checked valid candidates', async () => {
 const c = setup(); let resolveSelection, request;
 c.selectionSave = new Promise(resolve => { resolveSelection = resolve; });
 c.api = async (path, body) => { request = {path, body}; return {...c.state, candidates: [candidate]}; };
 vm.runInContext(`discoveryPicked.add('${key}'); discoveryPicked.add('IPC:STALE')`, c);
 const pending = c.adoptDiscoveryClasses();
 await Promise.resolve(); assert.equal(request, undefined);
 resolveSelection(); await pending;
 assert.equal(request.path, '/discovery/adopt'); assert.deepEqual(Array.from(request.body.keys), [key]);
 assert.deepEqual(c.state.selected, ['IPC:A01B1/00']); assert.equal(c.activeTab, 'explore');
});

test('failed manual selection save prevents adoption and retains the checked candidates', async () => {
 const c = setup(); let requests = 0;
 c.selectionSave = Promise.reject(new Error('save failed'));
 c.api = async () => { requests++; return c.state; };
 vm.runInContext(`discoveryPicked.add('${key}')`, c);
 await assert.rejects(c.adoptDiscoveryClasses(), /save failed/);
 assert.equal(requests, 0); assert.equal(c.activeTab, 'discovery');
 assert.equal(vm.runInContext(`discoveryPicked.has('${key}')`, c), true);
});

test('changing user English terms requires a new plan, including saved plans without that field', () => {
 const c = setup(); c.nodes['#discovery-keywords'].value = 'saved topic';
 assert.equal(c.discoveryPlanChanged(), false);
 c.nodes['#discovery-english'].value = 'solid electrolyte';
 assert.equal(c.discoveryPlanChanged(), true);
 c.state.discovery.plan.user_english_terms = 'solid electrolyte';
 assert.equal(c.discoveryPlanChanged(), false);
 c.nodes['#discovery-english'].value = 'sulfide electrolyte';
 c.updateDiscoveryControls(); assert.equal(c.nodes['#discovery-analyze'].disabled, true);
 assert.ok(c.nodes['#discovery-plan-note'].textContent.includes('英語検索語'));
 assert.ok(c.nodes['#discovery-plan-note'].textContent.includes('以前の計画は記録に保存'));
});

test('initial draft restores raw user English input, never generated search terms', () => {
 const c = setup(); delete c.nodes['#discovery-keywords'];
 c.state.discovery.plan.user_english_terms = 'cell structure, sulfide electrolyte';
 c.state.discovery.plan.english_terms = ['generated phrase'];
 c.renderDiscovery();
 const html = c.nodes['#discovery-content'].innerHTML;
 assert.ok(html.includes('>cell structure, sulfide electrolyte</textarea>'));
 assert.ok(!html.includes('generated phrase'));
 assert.ok(html.includes('例：solid state battery, solid electrolyte'));
 assert.equal(vm.runInContext('discoveryDraft.english_terms', c), 'cell structure, sulfide electrolyte');
});

test('archived plans render without rounds and update when only archives change', () => {
 const c = setup(); c.renderDiscovery();
 c.state.discovery.archives = [{id: 'archive/1?x=<a>', keywords: '<old theme>', time: '2026-09-12'}];
 c.renderDiscovery();
 const html = c.nodes['#discovery-history'].innerHTML;
 assert.ok(html.includes('以前の計画 1 件'));
 assert.ok(html.includes('&lt;old theme&gt;'));
 assert.ok(html.includes('/api/discovery/export?archive_id=archive%2F1%3Fx%3D%3Ca%3E'));
});

test('request responses update only discovery and job after a newer manual edit', async () => {
 for (const path of ['/discovery/plan', '/discovery/analyze', '/discovery/import', '/discovery/run']) {
  const c = setup(); c.activeTab = 'explore'; let resolveRequest;
  const stale = {...c.state, selected: ['IPC:OLD'], patents: [{id: 'OLD'}], keywords: 'old', queries: [{id: 'old-query'}], candidates: [{code: 'OLD'}],
   discovery: {...c.state.discovery, status: 'done', logs: [{message: 'new discovery result'}]}, job: {status: 'done'}};
  c.api = async requestedPath => requestedPath === '/state' ? stale : new Promise(resolve => { resolveRequest = resolve; });
  const pending = c.requestDiscovery(path, {});
  c.state = {...c.state, selected: ['IPC:NEW'], patents: [{id: 'NEW', label: 'keep'}], keywords: 'new', queries: [{id: 'new-query'}], candidates: [{code: 'NEW'}], settings: {provider: 'local'}};
  resolveRequest(path === '/discovery/run' ? {job_id: 'test-job'} : stale);
  await pending;
  assert.deepEqual(c.state.selected, ['IPC:NEW'], path); assert.equal(c.state.patents[0].id, 'NEW', path);
  assert.equal(c.state.keywords, 'new', path); assert.equal(c.state.queries[0].id, 'new-query', path);
  assert.equal(c.state.candidates[0].code, 'NEW', path); assert.equal(c.state.settings.provider, 'local', path);
  assert.equal(c.state.discovery.logs[0].message, 'new discovery result', path); assert.equal(c.state.job.status, 'done', path);
 }
});

test('adoption merges only candidate view, discovery and job after a concurrent manual edit', async () => {
 const c = setup(); let resolveRequest;
 const stale = {...c.state, selected: ['IPC:OLD'], patents: [{id: 'OLD'}], keywords: 'old', queries: [{id: 'old-query'}],
  candidates: [candidate], classification_view: {keys: [key]}, discovery: {...c.state.discovery, status: 'done'}, job: {status: 'done'}};
 c.api = async () => new Promise(resolve => { resolveRequest = resolve; });
 vm.runInContext(`discoveryPicked.add('${key}')`, c);
 const pending = c.adoptDiscoveryClasses(); await new Promise(setImmediate);
 c.state = {...c.state, selected: ['IPC:NEW'], patents: [{id: 'NEW', label: 'exclude'}], keywords: 'new', queries: [{id: 'new-query'}]};
 resolveRequest(stale); await pending;
 assert.deepEqual(c.state.selected, ['IPC:NEW']); assert.equal(c.state.patents[0].label, 'exclude');
 assert.equal(c.state.keywords, 'new'); assert.equal(c.state.queries[0].id, 'new-query');
 assert.equal(c.state.candidates[0].code, candidate.code); assert.equal(c.state.classification_view.keys[0], key);
 assert.equal(c.state.discovery.status, 'done'); assert.equal(c.state.job.status, 'done'); assert.equal(c.activeTab, 'explore');
});

test('OPS lower-bound totals and partial retrieval remain explicit in history', () => {
 const c = setup();
 const html = c.discoveryHistoryHtml({rounds: [{round: 1, queries: [{query: 'ta=battery', result_count: 25, new_documents: 22, total: 10000, total_is_lower_bound: true, truncated: true}]}]});
 assert.match(html, /10,000 件以上/);
 assert.ok(html.includes('先頭から取得できた 25 件'));
 assert.ok(html.includes('ヒット全件を取得した結果ではありません'));
});

function comparisonItems() {
 return [
  {...candidate, title_ja: '固体電解質', title_en: 'Solid electrolytes', facets: ['material']},
  {...candidate, code: 'H01M4/04', title_ja: '電極の製造方法', title_en: 'Manufacturing electrodes', evidence_status: 'patent_metadata', support_count: 4, synthetic_count: 0, family_count: 3, facets: ['process'], evidence: [{patent_id: 'EP1', title: 'Coating an electrode', source: 'EPO OPS'}]},
  {...candidate, code: 'G01R31/36', title_ja: '電池の試験', title_en: 'Battery testing', evidence_status: 'patent_metadata', support_count: 3, synthetic_count: 3, family_count: 3, facets: ['performance'], evidence: [{patent_id: 'DEMO-1', title: 'Sample', synthetic: true}]},
 ];
}

test('facet, bilingual text, source and selection filters preserve every underlying candidate', () => {
 const c = setup(), items = comparisonItems();
 c.state.discovery.plan.candidate_hypotheses = items;
 vm.runInContext("discoveryCompare.source = 'patent'", c);
 assert.deepEqual(Array.from(c.discoveryFilteredItems(), item => item.code), ['H01M4/04']);
 vm.runInContext("discoveryCompare.source = 'synthetic'", c);
 assert.deepEqual(Array.from(c.discoveryFilteredItems(), item => item.code), ['G01R31/36']);
 vm.runInContext("discoveryCompare.source = 'all'; discoveryCompare.facet = 'process'; discoveryCompare.search = 'electrode'", c);
 assert.equal(c.discoveryFilteredItems().length, 1);
 vm.runInContext("discoveryCompare.search = '電解質'", c); assert.equal(c.discoveryFilteredItems().length, 0);
 vm.runInContext(`discoveryCompare.facet = 'all'; discoveryCompare.search = ''; discoveryCompare.selectedOnly = true; discoveryPicked.add('${key}')`, c);
 assert.equal(c.discoveryFilteredItems()[0].code, candidate.code);
 assert.equal(c.discoveryItems().length, 3); assert.deepEqual(c.state.selected, ['IPC:A01B1/00']);
});

test('comparison map represents all candidates with scoped nodes, exact accessible names and explicit source types', () => {
 const c = setup(), items = comparisonItems();
 c.PatentClassLayout = require('../static/classification-layout.js');
 c.PatentClassLabels = require('../static/classification-labels.js');
 const layout = c.PatentClassLayout.compute(items, {mode: 'semantic', width: 900});
 const html = c.discoveryMapHtml(items, layout);
 assert.equal((html.match(/class="discovery-star /g) || []).length, 3);
 assert.ok(!/class="node[ "]/.test(html)); assert.ok(html.includes('r="32"'));
 assert.ok(html.includes('aria-label="IPC H01M4/04 電極の製造方法 · 公報由来候補"'));
 assert.ok(html.includes('架空サンプル由来')); assert.ok(html.includes('辞書候補・探索仮説'));
 assert.ok(html.includes('role="button" tabindex="0"')); assert.ok(html.includes('data-discovery-selectable="true"'));
 assert.equal(c.discoveryTitle(items[0], 'en'), 'Solid electrolytes');
 assert.equal(c.discoveryTitle(items[0], 'ja'), '固体電解質');
});

test('map is the default and a single bilingual inspector replaces the flat candidate wall', () => {
 const c = setup(), items = comparisonItems(); c.state.discovery.plan.candidate_hypotheses = items;
 c.PatentClassLabels = require('../static/classification-labels.js');
 const html = c.discoveryComparisonHtml(items);
 assert.ok(html.includes('id="discovery-class-map"')); assert.ok(html.includes('id="discovery-map-viewport"'));
 assert.ok(!html.includes('discovery-candidate-grid'));
 assert.equal((html.match(/<article class="discovery-candidate /g) || []).length, 1);
 assert.ok(html.includes('固体電解質')); assert.ok(html.includes('Solid electrolytes'));
 const inspected = c.discoveryInspectorHtml(items[1]);
 assert.ok(inspected.includes('Coating an electrode')); assert.ok(inspected.includes('EPO OPS'));
 assert.ok(inspected.includes('<details open class="discovery-evidence"'));
});

test('map picking and lasso stay local, reject navigation nodes and preserve hidden checked keys', () => {
 const c = setup(), items = comparisonItems(); items.push({...candidate, kind: 'F-term', code: '5H029', selectable: false});
 c.state.discovery.plan.candidate_hypotheses = items;
 let apiCalls = 0; c.api = async () => { apiCalls++; };
 c.toggleDiscoveryPicked(key, true);
 c.toggleDiscoveryPicked('F-term:5H029', true);
 vm.runInContext("discoveryCompare.source = 'patent'", c);
 c.applyDiscoveryLasso(['IPC:H01M4/04', 'IPC:G01R31/36', 'IPC:UNKNOWN', 'F-term:5H029']);
 assert.deepEqual(Array.from(vm.runInContext('[...discoveryPicked].sort()', c)), [key, 'IPC:H01M4/04'].sort());
 assert.deepEqual(c.state.selected, ['IPC:A01B1/00']); assert.equal(apiCalls, 0);
 const polygon = [{x: 0, y: 0}, {x: 100, y: 0}, {x: 100, y: 100}, {x: 0, y: 100}];
 assert.equal(c.discoveryPointInPolygon({x: 50, y: 50}, polygon), true);
 assert.equal(c.discoveryPointInPolygon({x: 110, y: 50}, polygon), false);
});

test('list mode provides every filtered row without losing checked state across updates', () => {
 const c = setup(), items = comparisonItems(); c.state.discovery.plan.candidate_hypotheses = items;
 vm.runInContext(`discoveryCompare.mode = 'list'; discoveryPicked.add('${key}')`, c);
 const html = c.discoveryComparisonBodyHtml();
 assert.ok(!html.includes('id="discovery-class-map"'));
 assert.equal((html.match(/data-discovery-row=/g) || []).length, 3);
 assert.ok(html.includes(`data-discovery-key="${key}" aria-label="${candidate.code} を選択" checked`));
 c.state.discovery.analysis = {recommendations: [{...items[0], evidence_status: 'patent_metadata', support_count: 2}]};
 c.renderDiscovery();
 assert.equal(vm.runInContext('discoveryCompare.mode', c), 'list');
 assert.equal(vm.runInContext(`discoveryPicked.has('${key}')`, c), true);
});

test('adoption freezes its local selection until the requested snapshot is handed off', async () => {
 const c = setup(), items = comparisonItems(); c.state.discovery.plan.candidate_hypotheses = items;
 let complete; c.selectionSave = new Promise(resolve => { complete = resolve; });
 c.api = async () => c.state;
 c.toggleDiscoveryPicked(key, true);
 const pending = c.adoptDiscoveryClasses();
 c.toggleDiscoveryPicked('IPC:H01M4/04', true); c.applyDiscoveryLasso(['IPC:G01R31/36']);
 assert.deepEqual(Array.from(vm.runInContext('[...discoveryPicked]', c)), [key]);
 complete(); await pending;
 assert.equal(vm.runInContext('discoveryAdopting', c), false);
 assert.deepEqual(c.state.selected, ['IPC:A01B1/00']);
});

test('short official leaves retain bilingual parent context and catalogue version', () => {
 const c = setup(); c.PatentClassLabels = require('../static/classification-labels.js');
 const html = c.discoveryBilingualHtml({...candidate, title_ja: '固体', title_en: 'Solid materials', title_ja_status: 'official_translation',
  title_ja_version: '2026.01', title_en_version: '2026.01', parent: 'H01M10/0561',
  title_ja_parent: '無機物のみからなる電解質', title_en_parent: 'Electrolytes consisting of inorganic materials only'});
 assert.ok(html.includes('公式日本語訳 · 2026.01'));
 assert.ok(html.includes('上位：H01M10/0561 無機物のみからなる電解質'));
 assert.ok(html.includes('Parent: H01M10/0561 Electrolytes consisting of inorganic materials only'));
 const escaped = c.discoveryBilingualHtml({...candidate, title_ja_parent: '<parent>', title_ja_parent_code: '<code>', title_ja_version: '<version>'});
 assert.ok(escaped.includes('上位：&lt;code&gt; &lt;parent&gt;')); assert.ok(escaped.includes('&lt;version&gt;'));
});

test('semantic headings carry their actual available group width for text fitting', () => {
 const c = setup(), item = comparisonItems()[0];
 const html = c.discoveryMapHtml([item], {positions: [[100, 100]], groups: [{label: 'Materials and compositions', memberIndices: [0], bounds: {x: 10, y: 10, width: 180, height: 160}}]});
 assert.ok(html.includes('data-width="154"'));
 assert.ok(html.includes('Materials and compositions'));
});
