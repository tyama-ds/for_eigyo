import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../static/citation-flow.js', import.meta.url), 'utf8');
const fixture = (extra = {}) => ({
  version: 1, result_id: 'r1', interval: 'year', topic_id: 'all',
  periods: [{id:'2020', label:'2020', index:0, count:1}, {id:'2025', label:'2025', index:1, count:2}],
  topics: [{id:'topic-1', label:'Research', color:'#abcdef'}],
  nodes: [{id:'old', title:'Older foundation', year:2020, period_id:'2020', topic_id:'topic-1', x:.1, y:.3}, {id:'new', title:'New research', year:2025, period_id:'2025', topic_id:'topic-1', x:.8, y:.7}, {id:'new2', title:'Another current paper', year:2025, period_id:'2025', topic_id:'topic-1', x:.5, y:.4}],
  edges: [{id:'reference-1', source_id:'new', target_id:'old', source_period:'2025', target_period:'2020'}],
  centroids: [{period_id:'2025', topic_id:'all', current:{x:.65,y:.55,count:2,evidence_ids:['new','new2']}, foundation:{x:.1,y:.3,count:1,evidence_ids:['old']}, paired_current:{x:.8,y:.7,count:1}, cosine_distance:.36, coverage:{papers_total:2,matched_citing_papers:1,matched_target_papers:1,semantic_citing_papers:1}}],
  coverage: {papers_total:3, papers_with_references:1, matched_citing_papers:1, matched_target_papers:1, valid_edges:1}, meta:{analysis_papers:3}, warnings:[], ...extra
});
function eventTarget() {
  const listeners = new Map();
  return {listeners, addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(callback); }, removeEventListener(name, callback) { listeners.get(name)?.delete(callback); }, dispatch(name, event = {}) { for (const callback of listeners.get(name) || []) callback(event); }};
}
function harness({reduced = false, api = async () => fixture()} = {}) {
  const frames = new Map(), doc = Object.assign(eventTarget(), {hidden:false});
  const media = Object.assign(eventTarget(), {matches:reduced});
  let frameId = 0;
  const window = {__ATLAS_UI_TEST__:true, matchMedia:() => media, requestAnimationFrame(callback) { const id = ++frameId; frames.set(id, callback); return id; }, cancelAnimationFrame(id) { frames.delete(id); }};
  // Deliberately omit timers and browser storage: the feature must not need either.
  vm.runInContext(source, vm.createContext({window, document:doc, AbortController, console}));
  const makeRoot = () => Object.assign(eventTarget(), {innerHTML:'', isConnected:true, ownerDocument:doc, classList:{add(){},remove(){}}, querySelector(selector) { return selector === '.cf-svg' && this.innerHTML.includes('<svg') ? {querySelector:() => null} : null; }, querySelectorAll:() => []});
  const root = makeRoot();
  return {t:window.__citationFlowTest, module:window.AtlasCitationFlow, root, makeRoot, api, frames, doc, media,
    mount(result = {id:'r1', papers:[]}, element = root) { return window.AtlasCitationFlow.mount(element, result, {api}); },
    frame(timestamp) { const tasks = [...frames.values()]; frames.clear(); for (const callback of tasks) callback(timestamp); }
  };
}

test('observed citation keeps NEW to OLD direction; no inverted or invented links', () => {
  const {t} = harness();
  const raw = fixture();
  raw.edges.push({id:'backward',source_id:'old',target_id:'new'}, {id:'missing',source_id:'new',target_id:'outside'}, {id:'self',source_id:'new',target_id:'new'}, {id:'conflict',source_id:'new2',target_id:'old',source_period:'2020'});
  const data = t.normalizeData(raw);
  assert.equal(data.edges.length, 1);
  assert.equal(data.edges[0].source_id, 'new'); assert.equal(data.edges[0].target_id, 'old');
  assert.equal(raw.edges.length, 5, 'normalization must not modify the server response');
  assert.equal(t.normalizeData(fixture({edges:[]})).edges.length, 0);
});

test('curve and traveling pulse start at the citing paper and end at its actual older endpoint', () => {
  const {t} = harness();
  const sourcePoint = t.project({x:.8,y:.7},1,2), targetPoint = t.project({x:.1,y:.3},0,2);
  assert.ok(sourcePoint[1] < targetPoint[1], 'newer plane is above the older plane');
  const curve = t.edgeGeometry(sourcePoint, targetPoint);
  assert.deepEqual([...t.bezier(curve.controls,0)], [...sourcePoint]);
  assert.deepEqual([...t.bezier(curve.controls,1)], [...targetPoint]);
  const middle = t.bezier(curve.controls,.5); assert.ok(middle[1] > sourcePoint[1] && middle[1] < targetPoint[1]);
  assert.equal(t.edgeGeometry([NaN,1],[1,2]),null);
});

test('every layer endpoint remains within the SVG for allowed orbit and spread extremes', () => {
  const {t} = harness();
  for (const yaw of [-1.05,0,1.05]) for (const pitch of [.25,.92]) for (const spread of [180,430]) for (const layer of [0,7]) for (const x of [0,1]) for (const y of [0,1]) {
    const p = t.project({x,y},layer,8,{camera:{yaw,pitch},spread});
    assert.ok(p[0] > 0 && p[0] < 970, `x=${p[0]}`); assert.ok(p[1] > 0 && p[1] < 720, `y=${p[1]}`);
  }
});

test('time window caps eight planes and graph selection never produces a dangling display edge', async () => {
  const raw = fixture({periods:Array.from({length:20},(_,i)=>({id:String(2000+i),label:String(2000+i),index:i,count:1}))});
  raw.nodes = Array.from({length:260},(_,i)=>({id:`p${i}`,title:`P${i}`,period_id:String(2000+i%20),year:2000+i%20,x:.5,y:.5}));
  raw.edges = Array.from({length:259},(_,i)=>({id:`e${i}`,source_id:`p${i+1}`,target_id:'p0'}));
  const h = harness({api:async()=>raw}); await h.mount().ready;
  const data = h.t.state.data, scene = h.t.state.scene;
  assert.equal(data.nodes.length,240); assert.equal(scene.win.periods.length,8); assert.equal(scene.win.start,12);
  const ids = new Set(scene.nodes.map(n=>n.id)); assert.ok(scene.edges.every(e=>ids.has(e.source_id)&&ids.has(e.target_id)));
  assert.equal(h.t.periodWindow(raw.periods,999,100).periods.length,8); h.module.unmount();
});

test('late dataset response cannot replace the current dataset, even if transport ignores abort', async () => {
  const pending = [], h = harness({api:(url,options)=>new Promise(resolve=>pending.push({url,options,resolve}))});
  const first = h.mount(), secondRoot = h.makeRoot(), second = h.mount({id:'r2',papers:[]},secondRoot);
  assert.equal(pending[0].options.signal.aborted,true);
  pending[1].resolve(fixture({result_id:'r2',warnings:['Newest result']})); await second.ready;
  pending[0].resolve(fixture({warnings:['Stale result']})); await first.ready;
  assert.match(secondRoot.innerHTML,/Newest result/); assert.doesNotMatch(secondRoot.innerHTML,/Stale result/);
  assert.equal(h.t.state.result.id,'r2'); h.module.unmount();
});

test('references outside the visible time window are distinguished from a corpus without matched links', async () => {
  const periods = Array.from({length:20},(_,i)=>({id:String(2000+i),label:String(2000+i),index:i,count:i===19?1:0}));
  const raw = fixture({periods,centroids:[],nodes:[{id:'old',title:'Old reference',period_id:'2000',year:2000,x:.2,y:.3},{id:'new',title:'Current source',period_id:'2019',year:2019,x:.7,y:.8}],edges:[{id:'across-window',source_id:'new',target_id:'old'}]});
  const h = harness({api:async()=>raw}); await h.mount().ready;
  assert.equal(h.t.state.data.edges.length,1); assert.equal(h.t.state.scene.edges.length,0);
  assert.match(h.root.innerHTML,/表示中の時層に両端がある引用線はありません/);
  assert.match(h.root.innerHTML,/年・四半期へ切り替えると広い期間を比較できます/);
  assert.doesNotMatch(h.root.innerHTML,/照合できた引用線がありません/);
  h.module.unmount();
  const unmatched = harness({api:async()=>fixture({edges:[]})}); await unmatched.mount().ready;
  assert.match(unmatched.root.innerHTML,/照合できた引用線がありません/);
  assert.doesNotMatch(unmatched.root.innerHTML,/表示中の時層に両端/); unmatched.module.unmount();
});

test('late interval response is discarded after a second scoped request', async () => {
  const pending = [], h = harness({api:(url)=>new Promise(resolve=>pending.push({url,resolve}))});
  const initial = h.mount(); pending[0].resolve(fixture()); await initial.ready;
  const state = h.t.state; state.interval = 'month'; const older = h.t.load(state);
  state.interval = 'quarter'; const latest = h.t.load(state);
  pending[2].resolve(fixture({warnings:['Quarter response']})); await latest;
  pending[1].resolve(fixture({warnings:['Month response']})); await older;
  assert.match(h.root.innerHTML,/Quarter response/); assert.doesNotMatch(h.root.innerHTML,/Month response/);
  assert.match(pending[1].url,/interval=month/); assert.match(pending[2].url,/interval=quarter/); h.module.unmount();
});

test('pause, hidden page, detached root, reduced motion and unmount stop animation work', async () => {
  const h = harness(); const controller = h.mount(); await controller.ready;
  assert.equal(h.frames.size,1); h.frame(0); h.frame(16); assert.equal(h.frames.size,1);
  h.t.state.playing = false; h.t.syncAnimation(h.t.state); assert.equal(h.frames.size,0);
  h.t.state.playing = true; h.t.syncAnimation(h.t.state); assert.equal(h.frames.size,1);
  h.doc.hidden = true; h.doc.dispatch('visibilitychange'); assert.equal(h.frames.size,0);
  h.doc.hidden = false; h.doc.dispatch('visibilitychange'); assert.equal(h.frames.size,1);
  h.media.matches = true; h.media.dispatch('change'); assert.equal(h.frames.size,0); assert.match(h.root.innerHTML,/静止表示/);
  h.media.matches = false; h.media.dispatch('change'); assert.equal(h.frames.size,1);
  h.root.isConnected = false; h.frame(100); assert.equal(h.frames.size,0);
  controller.unmount(); assert.equal(h.t.state,null);
  assert.ok([...h.root.listeners.values()].every(set=>!set.size)); assert.ok([...h.doc.listeners.values()].every(set=>!set.size)); assert.ok([...h.media.listeners.values()].every(set=>!set.size));
});

test('initial reduced-motion preference renders completed layers without scheduling timers or frames', async () => {
  const h = harness({reduced:true}); await h.mount().ready;
  assert.equal(h.frames.size,0); assert.equal(h.t.expansionProgress(0,7,true),1);
  assert.match(h.root.innerHTML,/data-reduced-motion="true"/); assert.match(h.root.innerHTML,/data-cf-action="replay" disabled/);
  h.module.unmount();
});

test('unmount aborts pending work and ignores its eventual success', async () => {
  let resolve, signal; const h = harness({api:(_,opts)=>{signal=opts.signal;return new Promise(done=>resolve=done);}});
  const controller = h.mount(), before = h.root.innerHTML; controller.unmount();
  assert.equal(signal.aborted,true); resolve(fixture({warnings:['Should never render']})); await controller.ready;
  assert.equal(h.root.innerHTML,before); assert.equal(h.frames.size,0);
});

test('absent foundation remains unknown, and cosine compares the paired source cohort', async () => {
  const raw = fixture(); raw.centroids[0].foundation=null; raw.centroids[0].paired_current=null; raw.centroids[0].cosine_distance=null;
  const h = harness({api:async()=>raw}); await h.mount().ready;
  const html = h.t.inspector(h.t.state); assert.match(html,/比較不可/); assert.match(html,/対応する新研究と研究基盤の両方が必要/); assert.doesNotMatch(html,/0\.000/);
  assert.doesNotMatch(h.root.innerHTML,/data-cf-center="foundation"/);
  h.t.state.data = h.t.normalizeData(fixture());
  assert.match(h.t.inspector(h.t.state),/引用照合済みの同じ新論文群 1件で比較/); h.module.unmount();
});

test('untrusted titles, topic labels, warnings and attributes are escaped', async () => {
  const raw = fixture(); raw.nodes[0].title='<script>alert(1)</script>'; raw.nodes[0].id='old" onclick="evil'; raw.edges=[];
  raw.topics[0].label='<img src=x onerror=evil>'; raw.topics[0].color='red" onload="evil'; raw.warnings=['<iframe src=evil>']; raw.centroids[0].current.evidence_ids=['new" onfocus="evil'];
  const h = harness({api:async()=>raw}); await h.mount().ready;
  assert.doesNotMatch(h.root.innerHTML,/<(?:script|img|iframe)[\s>]/); assert.doesNotMatch(h.root.innerHTML,/\son(?:click|load|focus)="evil/);
  assert.match(h.root.innerHTML,/&lt;script&gt;/); assert.match(h.root.innerHTML,/&lt;img/); assert.match(h.root.innerHTML,/&quot; onfocus=&quot;/);
  assert.equal(h.t.color('url(javascript:evil)'),'#90aaca'); h.module.unmount();
});

test('evidence selection opens source and target originals, including outside result display sample', async () => {
  const h = harness(); await h.mount({id:'r1',papers:[]}).ready;
  h.t.state.edge='reference-1'; const html = h.t.inspector(h.t.state);
  assert.match(html,/data-paper="new"/); assert.match(html,/data-paper="old"/); assert.ok(html.indexOf('data-paper="new"') < html.indexOf('data-paper="old"'));
  assert.match(html,/引用元 → 参照先/); h.module.unmount();
});
