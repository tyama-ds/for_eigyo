const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const explorerSource = fs.readFileSync('static/classification-ui.js', 'utf8');
const appSource = fs.readFileSync('static/app.js', 'utf8');
const tabSource = appSource.slice(appSource.indexOf('function setTab('), appSource.indexOf('function setStep('));
const A = 'IPC:A01B1/04', B = 'IPC:H01M10/0562';

function candidate(key) {
  const [kind, code] = key.split(':');
  return {kind, code, title: code, key, selectable: true};
}

function snapshot(selected = [A]) {
  return {
    keywords: 'original', selected: [...selected], candidates: [candidate(A), candidate(B)],
    patents: [], classification_view: {keys: [A, B], focus: candidate(B)},
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

const flush = () => new Promise(setImmediate);

function setup(apiHandler = async () => snapshot()) {
  const root = {
    inert: false, attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
  };
  const targetList = {innerHTML: '', insertAdjacentHTML(_position, text) { this.innerHTML += text; }};
  const dom = {
    '#step-content': root,
    '#keywords': {value: 'original'},
    '#seed-ipc': {value: ''},
    '#class-search': {value: ''},
    '#target-search': {value: ''},
    '#target-patent-list': targetList,
  };
  const requests = [], notices = [];
  const context = {
    state: snapshot(), selectionSave: Promise.resolve(), activeTab: 'explore', step: 0,
    $: selector => dom[selector] || null, $$: () => [],
    keyOf: item => item.key || `${item.kind}:${item.code}`,
    esc: text => String(text ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char])),
    toast: (...args) => notices.push(args), renderAgent() {},
    api: async (path, body) => { requests.push({path, body}); return apiHandler(path, body); },
    dom, rendered: 0, selectionPaints: 0, setTimeout, clearTimeout,
    PatentClassLabels: require('../static/classification-labels.js'),
  };
  vm.createContext(context);
  vm.runInContext(explorerSource + '\n' + tabSource, context);
  // Keep state transitions and request functions real. Rendering is a boundary
  // stub: repaint the captured draft as the actual template does, without a DOM.
  vm.runInContext(`
    classDraft = {keywords: 'original'};
    function renderStep() {
      rendered++;
      dom['#keywords'].value = classDraft.keywords;
      dom['#seed-ipc'].value = classDraft['seed-ipc'] || '';
      dom['#class-search'].value = classDraft['class-search'] || '';
    }
    function updateSelectedClasses() { selectionPaints++; }
    function renderClassFocus() {}
    function classDetail() {}
    function drawClassNodes() { rendered++; }
  `, context);
  return {context, dom, root, requests, notices, read: expression => vm.runInContext(expression, context)};
}

test('switching tabs retains the keyword, manual seed and classification-search drafts', () => {
  const s = setup();
  s.dom['#keywords'].value = 'edited "solid electrolyte"';
  s.dom['#seed-ipc'].value = 'H01M 10/00\nA01B 1/04';
  s.dom['#class-search'].value = 'solar cell';
  s.context.setTab('agent');
  s.context.setTab('explore');
  assert.equal(s.dom['#keywords'].value, 'edited "solid electrolyte"');
  assert.equal(s.dom['#seed-ipc'].value, 'H01M 10/00\nA01B 1/04');
  assert.equal(s.dom['#class-search'].value, 'solar cell');
  assert.equal(s.context.state.keywords, 'original', 'tab changes must not silently save a draft');
});

test('clicking the active tab keeps the current DOM and draft without repainting', () => {
  const s = setup();
  s.dom['#keywords'].value = 'work in progress';
  s.dom['#seed-ipc'].value = 'C01B25/00';
  s.context.setTab('explore');
  assert.equal(s.context.rendered, 0);
  assert.equal(s.dom['#keywords'].value, 'work in progress');
  assert.equal(s.dom['#seed-ipc'].value, 'C01B25/00');
  assert.equal(s.requests.length, 0);
});

test('constellation language changes labels only while retaining drafts and selections', () => {
  const s = setup();
  s.dom['#keywords'].value = 'unsaved draft';
  const before = JSON.stringify(s.context.state);
  s.context.setClassLanguage('en');
  assert.equal(s.read('classLanguage'), 'en');
  s.context.setClassLanguage('ja');
  assert.equal(s.read('classLanguage'), 'ja');
  assert.equal(s.dom['#keywords'].value, 'unsaved draft');
  assert.equal(JSON.stringify(s.context.state), before);
  assert.equal(s.requests.length, 0);
});

test('upper IPC clicks save selection, toggle off and undo while F-term containers stay navigation only', async () => {
  const s = setup();
  s.context.state.selected = [];
  s.context.state.candidates = ['IPC:H', 'IPC:H04', 'IPC:H04L'].map(key => ({...candidate(key), selection_scope: 'subtree'}));
  s.context.state.candidates.push({...candidate('F-term:5H029'), selectable: false});
  for (const key of ['IPC:H', 'IPC:H04', 'IPC:H04L']) s.context.toggleClass(key);
  await s.context.selectionSave;
  assert.deepEqual([...s.requests.at(-1).body.selected], ['IPC:H', 'IPC:H04', 'IPC:H04L']);
  assert.match(s.context.classScopeText(s.context.state.candidates[2]), /下位分類を含む/);
  s.context.toggleClass('F-term:5H029');
  assert.equal(s.requests.length, 3);
  s.context.toggleClass('IPC:H04L');
  await s.context.selectionSave;
  assert.deepEqual([...s.requests.at(-1).body.selected], ['IPC:H', 'IPC:H04']);
  s.context.undoClassSelection();
  await s.context.selectionSave;
  assert.deepEqual([...s.requests.at(-1).body.selected], ['IPC:H', 'IPC:H04', 'IPC:H04L']);
});

test('hover definitions escape both languages and identify reference translations', () => {
  const s = setup();
  const html = s.context.classNamesHtml({kind:'IPC',code:'C01D15/00',title_ja:'<日本語>',title_en:'<English>',title_ja_status:'official_translation',title_en_status:'app_translation'});
  assert.match(html, /&lt;日本語&gt;/);
  assert.match(html, /&lt;English&gt;/);
  assert.match(html, /公式日本語訳/);
  assert.match(html, /アプリ内の参考訳/);
  assert.doesNotMatch(html, /<日本語>|<English>/);
});

test('a translation response merges language fields only after newer workspace edits', () => {
  const s=setup();
  s.context.state.keywords='new topic';
  s.context.state.selected=[B];
  const title=s.context.state.candidates[0].title;
  s.context.applyClassTranslations([{kind:'IPC',code:'A01B1/04',title_ja:'日本語名',title:'incorrect replacement',selected:[A]}]);
  assert.equal(s.context.state.candidates[0].title_ja,'日本語名');
  assert.equal(s.context.state.candidates[0].title,title);
  assert.deepEqual([...s.context.state.selected],[B]);
  assert.equal(s.context.state.keywords,'new topic');
});

test('browsing rejects concurrent selection edits, preserves selection, and unlocks after its response', async () => {
  const reply = deferred();
  const s = setup((path, body) => path === '/classifications/browse' ? reply.promise : snapshot(body.selected));
  const pending = s.context.browseClasses({direction: 'roots', kind: 'IPC'});
  await flush();
  assert.equal(s.root.inert, true);
  assert.equal(s.root.attributes['aria-busy'], 'true');
  s.context.changeClassSelection([A, B]);
  s.context.undoClassSelection();
  assert.deepEqual([...s.context.state.selected], [A]);
  assert.equal(s.requests.filter(r => r.path === '/selection').length, 0);
  reply.resolve(snapshot());
  await pending;
  assert.equal(s.root.inert, false);
  assert.equal(s.root.attributes['aria-busy'], undefined);
  assert.equal(s.read('classBusy'), false);
  assert.deepEqual([...s.context.state.selected], [A]);
  s.context.changeClassSelection([A, B]);
  await s.context.selectionSave;
  assert.deepEqual([...s.requests.at(-1).body.selected], [A, B]);
});

test('a second browse does not create an out-of-order request while the first is pending', async () => {
  const reply = deferred();
  const s = setup(() => reply.promise);
  const first = s.context.browseClasses({direction: 'roots', kind: 'IPC'});
  await flush();
  await s.context.browseClasses({direction: 'search', kind: 'IPC', text: 'battery'});
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].body.direction, 'roots');
  reply.resolve(snapshot());
  await first;
  assert.equal(s.context.rendered, 1);
});

test('a response after leaving the explorer does not repaint or switch the current tab', async () => {
  const reply = deferred();
  const s = setup(() => reply.promise);
  const pending = s.context.browseClasses({direction: 'roots', kind: 'IPC'});
  await flush();
  s.context.setTab('agent');
  reply.resolve(snapshot());
  await pending;
  assert.equal(s.context.activeTab, 'agent');
  assert.equal(s.context.rendered, 0);
  assert.equal(s.root.inert, false);
});

test('a failed browse releases the interaction lock and a subsequent browse succeeds', async () => {
  let attempts = 0;
  const s = setup(async () => {
    if (++attempts === 1) throw new Error('temporary read failure');
    return snapshot();
  });
  await assert.rejects(s.context.browseClasses({direction: 'roots'}), /temporary read failure/);
  assert.equal(s.root.inert, false);
  assert.equal(s.read('classBusy'), false);
  await s.context.browseClasses({direction: 'roots'});
  assert.equal(attempts, 2);
  assert.equal(s.context.rendered, 1);
});

test('a failed selection save restores persisted selection and permits the next operation', async () => {
  const s = setup(async path => {
    if (path === '/selection') throw new Error('selection write failed');
    return snapshot([A]);
  });
  s.context.changeClassSelection([B]);
  await assert.rejects(s.context.selectionSave, /selection write failed/);
  assert.deepEqual([...s.context.state.selected], [B], 'the initial change is optimistic');
  await assert.rejects(s.context.browseClasses({direction: 'roots'}), /保存済みの状態に戻しました/);
  assert.deepEqual([...s.context.state.selected], [A]);
  assert.equal(s.read('classUndo.length'), 0);
  assert.equal(s.root.inert, false);
  await s.context.selectionSave;
  assert.equal(s.requests.filter(r => r.path === '/classifications/browse').length, 0);
  await s.context.browseClasses({direction: 'roots'});
  assert.equal(s.requests.filter(r => r.path === '/classifications/browse').length, 1);
  assert.equal(s.root.inert, false);
});

test('undo after browsing restores the previous selection even when it is off the current page', async () => {
  const s = setup(async (path, body) => {
    const value = snapshot(path === '/selection' ? body.selected : [B]);
    value.classification_view = {keys: [B], focus: candidate(B)};
    return value;
  });
  s.context.changeClassSelection([B]);
  await s.context.selectionSave;
  await s.context.browseClasses({direction: 'children', kind: 'IPC', code: 'H01M10/0561'});
  assert.deepEqual([...s.context.classificationItems().map(x => x.key)], [B]);
  s.context.undoClassSelection();
  await s.context.selectionSave;
  assert.deepEqual([...s.context.state.selected], [A]);
  assert.deepEqual([...s.requests.at(-1).body.selected], [A]);
});

test('target list reconciles IDs after a CSV replacement without dropping still-present targets', () => {
  const s = setup();
  s.context.state.patents = [{id: 'NEW-1', title: 'Replacement patent', ipc: 'H01M10/00'}];
  s.read("targetPatentIds.add('OLD-1'); targetPatentIds.add('NEW-1');");
  s.context.renderTargetPatents();
  assert.deepEqual([...s.read('[...targetPatentIds]')], ['NEW-1']);
  assert.match(s.dom['#target-patent-list'].innerHTML, /選択 1 件/);
  assert.doesNotMatch(s.dom['#target-patent-list'].innerHTML, /OLD-1/);
});

function proposalSnapshot() {
  const value = snapshot([A]);
  value.candidate_proposal = {
    id: 'proposal-1', keywords: 'original', llm_used: true,
    returned_count: 3, accepted_count: 2, new_count: 1, existing_count: 1, rejected_count: 1,
    items: [
      {...candidate(A), is_new: false, reason: '<existing reason>'},
      {...candidate(B), is_new: true, reason: '製造プロセスに関係するため'},
    ],
    rejected: [{kind: 'IPC', code: '<invalid>', reason: '<not in dictionary>'}],
    notices: ['<notice>'],
  };
  return value;
}

test('legacy workspaces show no historical reply without inventing LLM results', () => {
  const s = setup();
  assert.match(s.context.classProposalPanelHtml(), /LLM返答の記録はありません/);
  assert.doesNotMatch(s.context.classProposalPanelHtml(), /候補に反映<\/span>/);
  assert.equal(s.context.classProposalReasonHtml({...candidate(A), reason: 'dictionary evidence'}), '');
});

test('proposal feedback explains counts, duplicate codes, exclusions, reasons, and separate selection', () => {
  const s = setup();
  s.context.state = proposalSnapshot();
  const html = s.context.classProposalPanelHtml();
  assert.match(html, /<b>3<\/b>返却/);
  assert.match(html, /<b>2<\/b>候補に反映/);
  assert.match(html, /<b>1<\/b>新規/);
  assert.match(html, /<b>1<\/b>既存と重複/);
  assert.match(html, /<b>1<\/b>除外/);
  assert.match(html, /既存の候補/);
  assert.match(html, /LLMの提案理由/);
  assert.match(html, /候補への反映だけでは選択・検索式は変わりません/);
  assert.match(html, /検索に使用中 · 解除/);
  assert.match(html, /＋ 検索に使う/);
  assert.match(html, /&lt;existing reason&gt;/);
  assert.match(html, /&lt;invalid&gt;/);
  assert.match(html, /&lt;not in dictionary&gt;/);
  assert.match(html, /&lt;notice&gt;/);
  assert.doesNotMatch(html, /<existing reason>|<invalid>|<notice>/);
});

test('dictionary-only responses and older themes are identified explicitly', () => {
  const s = setup();
  s.context.state = proposalSnapshot();
  s.context.state.candidate_proposal.llm_used = false;
  s.context.state.keywords = 'new theme';
  const html = s.context.classProposalPanelHtml();
  assert.match(html, /LLMからの提案を取得していません/);
  assert.match(html, /現在のテーマとは異なります/);
  assert.doesNotMatch(html, /今回のLLM候補を地図で見る/);
});

test('showing returned codes on the map is a display filter and does not select or persist them', async () => {
  const s = setup(async () => proposalSnapshot());
  s.context.state = proposalSnapshot();
  s.context.state.candidate_proposal.items = [s.context.state.candidate_proposal.items[1]];
  const before = JSON.stringify(s.context.state);
  s.dom['#keywords'].value = 'unfinished draft';
  s.context.showClassProposalMap();
  assert.deepEqual([...s.context.classificationItems().map(row => row.key)], [B]);
  assert.equal(JSON.stringify(s.context.state), before);
  assert.equal(s.requests.length, 0);
  assert.equal(s.dom['#keywords'].value, 'unfinished draft');
  s.context.setTab('agent');
  s.context.setTab('explore');
  assert.deepEqual([...s.context.classificationItems().map(row => row.key)], [B]);
  await s.context.browseClasses({direction: 'overview'});
  assert.deepEqual([...s.context.classificationItems().map(row => row.key)], [A, B]);
  assert.deepEqual([...s.context.state.selected], [A]);
});

test('proposal choice uses ordinary persistent selection while map filtering remains unchanged', async () => {
  const s = setup(async (_path, body) => snapshot(body.selected));
  s.context.state = proposalSnapshot();
  s.context.showClassProposalMap();
  s.context.toggleClass(B);
  await s.context.selectionSave;
  assert.deepEqual([...s.requests.at(-1).body.selected], [A, B]);
  assert.match(s.context.classProposalPanelHtml(), new RegExp(`data-proposal-select="${B}" aria-pressed="true"`));
  s.context.undoClassSelection();
  await s.context.selectionSave;
  assert.deepEqual([...s.context.state.selected], [A]);
});

test('failed suggestion is labelled separately from the retained previous successful response', async () => {
  let fail = true;
  const s = setup(async () => {
    if (fail) throw new Error('<model failed>');
    return proposalSnapshot();
  });
  s.context.state = proposalSnapshot();
  const before = JSON.stringify(s.context.state);
  await assert.rejects(s.context.suggestClassCandidates({keywords: 'new theme', use_llm: true}), /model failed/);
  assert.equal(JSON.stringify(s.context.state), before);
  let html = s.context.classProposalPanelHtml();
  assert.match(html, /今回の候補生成に失敗しました/);
  assert.match(html, /前回の候補生成結果/);
  assert.match(html, /&lt;model failed&gt;/);
  fail = false;
  await s.context.suggestClassCandidates({keywords: 'original', use_llm: true});
  html = s.context.classProposalPanelHtml();
  assert.match(html, /今回のLLM提案/);
  assert.doesNotMatch(html, /今回の候補生成に失敗しました/);
});

test('hover proposal reasons are escaped and distinguish relevance from official definitions', () => {
  const s = setup();
  s.context.state = proposalSnapshot();
  const html = s.context.classProposalReasonHtml(s.context.state.candidates[0]);
  assert.match(html, /LLMの提案理由/);
  assert.match(html, /&lt;existing reason&gt;/);
  assert.match(html, /分類の正式名称・定義とは別/);
  assert.equal(s.context.classProposalReasonHtml(candidate('IPC:Z99Z1/00')), '');
});

test('API errors expose the HTTP status even when the response body is not JSON', async () => {
  const apiSource = appSource.slice(appSource.indexOf('async function api('), appSource.indexOf('function on('));
  const context = {fetch: async () => ({ok:false,status:409,json:async()=>({detail:'another edit won'})})};
  vm.createContext(context);vm.runInContext(apiSource,context);
  await assert.rejects(context.api('/candidates',{}), error => error.status===409&&error.message==='another edit won');
  context.fetch = async () => ({ok:false,status:503,json:async()=>{throw new Error('invalid json');}});
  await assert.rejects(context.api('/state'), error => error.status===503&&/応答を読み取れません/.test(error.message));
});

test('a 409 refreshes concurrent selections before enabling edits and preserves local drafts', async () => {
  const reply=deferred(), conflict=Object.assign(new Error('concurrent edit'),{status:409});
  const s=setup(async (path,body)=>{
    if(path==='/candidates')throw conflict;
    if(path==='/state')return reply.promise;
    return snapshot(body.selected);
  });
  s.context.state=proposalSnapshot();
  s.dom['#keywords'].value='unsaved keyword draft';s.dom['#seed-ipc'].value='IPC draft';
  s.read(`classUndo.push(${JSON.stringify([A])});`);
  const pending=s.context.suggestClassCandidates({keywords:'unsaved keyword draft',use_llm:true});
  await flush();
  assert.equal(s.root.inert,true);
  s.context.toggleClass(B);s.context.changeClassSelection([B]);s.context.persistSelection();
  assert.deepEqual([...s.context.state.selected],[A]);
  assert.equal(s.requests.filter(request=>request.path==='/selection').length,0);
  const fresh=proposalSnapshot();fresh.selected=[B];fresh.keywords='server theme';
  reply.resolve(fresh);
  await assert.rejects(pending,error=>error.status===409);
  assert.deepEqual([...s.context.state.selected],[B]);
  assert.equal(s.context.state.keywords,'server theme');
  assert.equal(s.dom['#keywords'].value,'unsaved keyword draft');
  assert.equal(s.dom['#seed-ipc'].value,'IPC draft');
  assert.equal(s.read('classUndo.length'),0);
  assert.equal(s.root.inert,false);
  assert.match(s.context.classProposalPanelHtml(),/最新の候補・選択を再取得しました/);
  s.context.toggleClass(A);await s.context.selectionSave;
  assert.deepEqual([...s.requests.at(-1).body.selected],[B,A]);
});

test('failed conflict refresh blocks old selection writes across modes until explicit refresh succeeds', async () => {
  let available=false;
  const s=setup(async(path,body)=>{
    if(path==='/candidates')throw Object.assign(new Error('concurrent edit'),{status:409});
    if(path==='/state'){
      if(!available)throw new Error('offline');
      return snapshot([B]);
    }
    return snapshot(body.selected);
  });
  s.context.state=proposalSnapshot();
  await assert.rejects(s.context.suggestClassCandidates({use_llm:true}),error=>error.status===409);
  assert.equal(s.read('classSelectionStale'),true);
  assert.match(s.context.classSelectionSyncHtml(),/古い選択による上書きを防ぐ/);
  await assert.rejects(s.context.selectionSave,/同期が必要/);
  s.context.toggleClass(B);s.context.changeClassSelection([B]);s.context.undoClassSelection();s.context.persistSelection();
  await assert.rejects(s.context.requestClassState(()=>s.context.api('/query',{})),/同期が必要/);
  assert.deepEqual([...s.context.state.selected],[A]);
  assert.equal(s.requests.filter(request=>['/selection','/query'].includes(request.path)).length,0);
  available=true;await s.context.syncClassSelection();
  assert.equal(s.read('classSelectionStale'),false);
  await s.context.selectionSave;
  s.context.toggleClass(A);await s.context.selectionSave;
  assert.deepEqual([...s.requests.at(-1).body.selected],[B,A]);
});

const PARENT = 'IPC:G05D1/00';
function broaderProposalSnapshot(added = false) {
  const value = proposalSnapshot();
  const broader = {...candidate(PARENT), verified: true, title: '<official parent title>', reason: '<broader range>'};
  value.candidate_proposal.rejected = [{
    index: 2, kind: 'IPC', code: 'G05D1/02', reason: '現行版には収録されていません。',
    revision: {
      status: 'retired', last_verified_version: '2023.01', changed_version: '2024.01',
      explanation: '<revision evidence>', sources: [
        {title: '<official source>', url: 'https://www.wipo.int/classifications/ipc?code=G05D&version=2024'},
        {title: 'unsafe source', url: 'javascript:alert(1)'},
      ],
    }, broader_candidate: broader, broader_added: added,
  }];
  if (added) {
    value.candidates.push(broader);
    value.classification_view.keys.push(PARENT);
  }
  return value;
}

test('rejected candidates show escaped revision evidence, safe official links and a separate verified parent', () => {
  const s = setup();
  s.context.state = broaderProposalSnapshot();
  const html = s.context.classProposalPanelHtml();
  assert.match(html, /旧版の分類/);
  assert.match(html, /収録を確認した版：2023.01/);
  assert.match(html, /変更された版：2024.01/);
  assert.match(html, /&lt;revision evidence&gt;/);
  assert.match(html, /https:\/\/www\.wipo\.int\/classifications\/ipc\?code=G05D&amp;version=2024/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /&lt;official source&gt;/);
  assert.match(html, /確認済みの上位候補/);
  assert.match(html, /&lt;official parent title&gt;/);
  assert.match(html, /&lt;broader range&gt;/);
  assert.match(html, /data-proposal-broader="2"/);
  assert.match(html, /上位の G05D1\/00 を候補に追加/);
  assert.match(html, /候補への追加後、地図で選択すると初案に使えます/);
  assert.doesNotMatch(html, /javascript:|unsafe source|<revision evidence>|<official parent title>/);
  s.context.state.candidate_proposal.rejected[0].reason = '<revision evidence>';
  const deduplicated = s.context.classProposalPanelHtml();
  assert.equal((deduplicated.match(/&lt;revision evidence&gt;/g) || []).length, 1);
  assert.match(deduplicated, /旧版の分類/);
});

test('parent controls remain compatible with old metadata and never offer unverified parents', async () => {
  const s = setup();
  s.context.state = broaderProposalSnapshot(true);
  assert.match(s.context.classProposalPanelHtml(), /上位分類を候補に追加済み/);
  assert.doesNotMatch(s.context.classProposalPanelHtml(), /data-proposal-broader=/);
  assert.equal(await s.context.addClassProposalBroader(2), false);
  s.context.state = broaderProposalSnapshot();
  delete s.context.state.candidate_proposal.rejected[0].index;
  assert.match(s.context.classProposalPanelHtml(), /data-proposal-broader="0"/);
  delete s.context.state.candidate_proposal.id;
  assert.doesNotMatch(s.context.classProposalPanelHtml(), /data-proposal-broader=/);
  assert.equal(await s.context.addClassProposalBroader(0), false);
  s.context.state = broaderProposalSnapshot();
  s.context.state.candidate_proposal.rejected[0].broader_candidate.verified = false;
  assert.doesNotMatch(s.context.classProposalPanelHtml(), /確認済みの上位候補|data-proposal-broader=/);
  assert.equal(await s.context.addClassProposalBroader(2), false);
  assert.equal(await s.context.addClassProposalBroader(NaN), false);
  assert.equal(s.requests.length, 0);
  s.context.state = proposalSnapshot();
  assert.match(s.context.classProposalPanelHtml(), /&lt;not in dictionary&gt;/);
});

test('adding a parent uses its rejected proposal index, preserves selection and focuses the expanded map', async () => {
  const reply = deferred();
  const s = setup(() => reply.promise);
  s.context.state = broaderProposalSnapshot();
  s.context.showClassProposalMap();
  s.dom['#keywords'].value = 'unsaved draft';
  s.dom['#seed-ipc'].value = 'manual draft';
  const pending = s.context.addClassProposalBroader(2);
  await flush();
  assert.equal(s.root.inert, true);
  s.context.toggleClass(B);
  assert.deepEqual([...s.context.state.selected], [A]);
  assert.equal(await s.context.addClassProposalBroader(2), false);
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].path, '/candidates/broader');
  assert.equal(s.requests[0].body.proposal_id, 'proposal-1');
  assert.equal(s.requests[0].body.rejected_index, 2);
  assert.equal(Object.prototype.hasOwnProperty.call(s.requests[0].body, 'selected'), false);
  reply.resolve(broaderProposalSnapshot(true));
  assert.equal(await pending, true);
  assert.equal(s.root.inert, false);
  assert.equal(s.read('classProposalView'), false);
  assert.equal(s.read('classFocusKey'), PARENT);
  assert.deepEqual([...s.context.classificationItems().map(row => row.key)], [A, B, PARENT]);
  assert.deepEqual([...s.context.state.selected], [A]);
  assert.equal(s.dom['#keywords'].value, 'unsaved draft');
  assert.equal(s.dom['#seed-ipc'].value, 'manual draft');
  assert.match(s.context.classProposalPanelHtml(), /追加済み/);
});

test('a stale parent proposal refreshes concurrent edits and labels the parent action failure correctly', async () => {
  const conflict = Object.assign(new Error('<proposal changed>'), {status: 409}), reply = deferred();
  const s = setup(async path => {
    if (path === '/candidates/broader') throw conflict;
    if (path === '/state') return reply.promise;
    throw new Error('unexpected mutation');
  });
  s.context.state = broaderProposalSnapshot();
  s.dom['#keywords'].value = 'unsaved new topic';
  const pending = s.context.addClassProposalBroader(2);
  await flush();
  assert.equal(s.root.inert, true);
  s.context.toggleClass(B);s.context.persistSelection();
  assert.deepEqual([...s.context.state.selected], [A]);
  const fresh = proposalSnapshot();fresh.selected = [B];fresh.candidate_proposal.id = 'new-proposal';
  reply.resolve(fresh);
  await assert.rejects(pending, error => error.status === 409);
  assert.deepEqual([...s.context.state.selected], [B]);
  assert.equal(s.context.state.candidate_proposal.id, 'new-proposal');
  assert.equal(s.dom['#keywords'].value, 'unsaved new topic');
  assert.equal(s.read('classSelectionStale'), false);
  assert.equal(s.root.inert, false);
  const html = s.context.classProposalPanelHtml();
  assert.match(html, /上位候補を追加できませんでした/);
  assert.match(html, /&lt;proposal changed&gt;/);
  assert.match(html, /最新の候補・選択を再取得しました/);
  assert.doesNotMatch(html, /今回の候補生成に失敗|前回の候補生成結果/);
  assert.deepEqual(s.requests.map(request => request.path), ['/candidates/broader', '/state']);
});

test('a failed refresh after adding a stale parent blocks selection and subsequent parent writes', async () => {
  const s = setup(async path => {
    if (path === '/candidates/broader') throw Object.assign(new Error('stale proposal'), {status: 409});
    if (path === '/state') throw new Error('offline');
    throw new Error('unexpected mutation');
  });
  s.context.state = broaderProposalSnapshot();
  await assert.rejects(s.context.addClassProposalBroader(2), error => error.status === 409);
  assert.equal(s.read('classSelectionStale'), true);
  assert.match(s.context.classProposalPanelHtml(), /最新の状態を取得するまで、分類の変更は停止/);
  s.context.toggleClass(B);s.context.persistSelection();
  await assert.rejects(s.context.addClassProposalBroader(2), /同期が必要/);
  assert.deepEqual([...s.context.state.selected], [A]);
  assert.deepEqual(s.requests.map(request => request.path), ['/candidates/broader', '/state']);
});
