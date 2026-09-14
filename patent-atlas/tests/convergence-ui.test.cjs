const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('static/convergence-ui.js','utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function setup(){
 const ids=['content','total','query','query-preview','record','complete','status','summary','graph','count-chart','history','observation-note','blockers','pending','review','keep','exclude','skip','map','error'];
 const dom=Object.fromEntries(ids.map(id=>['#convergence-'+id,{value:'',min:'0',disabled:false,hidden:false,textContent:'',innerHTML:'',dataset:{}}]));
 const state={patents:[{id:'P1',label:'keep'}],queries:[{id:'q1',expression:'IPC = G05D'}],search_result:{id:'csv1',uploaded_count:1},convergence:{records:[],status:'insufficient',pending:{deferred:0,unprocessed:0},blockers:['比較回数が不足しています。'],eligible_to_complete:false},job:{status:'idle'}};
 const requests=[],handlers=new Map(),messages=[];
 const context={state,$:s=>dom[s]||null,esc,on:(id,event,fn)=>handlers.set(id+':'+event,fn),api:async(path,body)=>{requests.push({path,body});return state;},updateChrome:()=>{},toast:(message)=>messages.push(message),setTab:()=>{},setStep:()=>{}};
 vm.createContext(context);vm.runInContext(source,context);
 return {context,dom,requests,handlers,messages,read:code=>vm.runInContext(code,context)};
}
function records(){return [{id:'r1',observation_id:'csv0',ordinal:1,uploaded_count:20,total_hits:100,new_keep_rate:null,topic_shift_rate:null,hit_change_rate:null},{id:'r2',observation_id:'csv1',ordinal:2,uploaded_count:25,total_hits:105,new_keep_rate:.04,topic_shift_rate:.03,hit_change_rate:.05}];}

test('missing rate or count is not coerced into zero',()=>{
 const c=setup().context;for(const missing of [null,undefined,'',false,'0',NaN]){assert.equal(c.convergencePercent(missing),'—');assert.equal(c.convergenceCount(missing),'未記録');}
 assert.equal(c.convergencePercent(0),'0.0%');assert.equal(c.convergenceCount(0),'0');assert.equal(c.convergencePercent(.045),'4.5%');
});
test('missing observations split graph lines and preserve a true zero point',()=>{
 const c=setup().context,r=[{rate:null},{rate:.5},{rate:0},{rate:null},{rate:.2}],segments=c.convergenceSeries(r,'rate',i=>i,n=>n);
 assert.deepEqual(plain(segments.map(s=>s.map(p=>p.index))),[[1,2],[4]]);
 const html=c.convergenceChart([{ordinal:1,new_keep_rate:null},{ordinal:2,new_keep_rate:0}]);assert.equal((html.match(/class="convergence-point /g)||[]).length,1);assert.match(html,/第2回 · 新しい必要特許の割合 0.0%/);assert.doesNotMatch(html,/第1回 · 新しい/);
});
test('graphs expose keyboard points, numeric labels and a separate count graph',()=>{
 const c=setup().context,html=c.convergenceChart(records());assert.match(html,/tabindex="0" role="img"/);assert.match(html,/安定の目安 5%/);assert.match(html,/新しい必要特許の割合 4.0%/);assert.match(html,/>100%<\/text>/);
 const count=c.convergenceChart(records(),true);assert.match(count,/検索サイトの総件数 105件/);assert.match(count,/CSV取込件数 25件/);assert.doesNotMatch(count,/安定の目安/);
 const above=c.convergenceChart([{hit_change_rate:2}]);assert.match(above,/200.0%（グラフ上端は100%）/);
});
test('empty and first baseline records never invent a trend',()=>{
 const s=setup();assert.match(s.context.convergenceChart([]),/最初の1回は比較の基準/);const html=s.context.convergenceChart([records()[0]]);assert.doesNotMatch(html,/class="convergence-point /);
 s.context.state.convergence.records=[records()[0]];s.context.renderConvergence();assert.match(s.dom['#convergence-summary'].textContent,/次回から変化を比較/);
});
test('count entry validates actual total and binds to the visible CSV',()=>{
 const c=setup().context;assert.deepEqual(plain(c.convergenceRecordPayload('', '', 'csv1', 443)),{total_hits:null,query_id:null,observation_id:'csv1'});
 assert.deepEqual(plain(c.convergenceRecordPayload('500','q1','csv1',443)),{total_hits:500,query_id:'q1',observation_id:'csv1'});
 for(const bad of ['442','-1','1.5','Infinity','1e3','9007199254740992'])assert.throws(()=>c.convergenceRecordPayload(bad,'q1','csv1',443));
 assert.equal(c.convergenceRecordPayload('0','',null,0).total_hits,0);
});
test('polling preserves in-progress inputs and switches drafts only for a new CSV',()=>{
 const s=setup();s.context.renderConvergence();s.dom['#convergence-total'].value='3200';s.dom['#convergence-query'].value='q1';s.context.captureConvergenceDraft();
 s.context.state.convergence.records=records();s.context.renderConvergence();assert.equal(s.dom['#convergence-total'].value,'3200');assert.equal(s.dom['#convergence-query'].value,'q1');assert.equal(s.dom['#convergence-record'].textContent,'この回の判定を更新');
 s.context.state.search_result={id:'csv2',uploaded_count:30};s.context.renderConvergence();assert.equal(s.dom['#convergence-total'].value,'');assert.equal(s.dom['#convergence-query'].value,'');
});
test('an existing observation prepopulates recorded query and total',()=>{
 const s=setup();s.context.state.convergence.records=[{...records()[1],query_id:'q1'}];s.context.renderConvergence();assert.equal(s.dom['#convergence-total'].value,'105');assert.equal(s.dom['#convergence-query'].value,'q1');
 assert.equal(s.dom['#convergence-query-preview'].textContent,'IPC = G05D');
});
test('baseline observations with generated IDs are recognized and updated',()=>{
 const s=setup();s.context.state.search_result=null;s.context.state.convergence.records=[{id:'r1',observation_id:'generated-baseline-uuid',scope:'workspace_baseline',total_hits:80,query_id:'q1'}];s.context.renderConvergence();
 assert.equal(s.context.convergenceCurrentRecord().id,'r1');assert.equal(s.dom['#convergence-record'].textContent,'この回の判定を更新');assert.equal(s.dom['#convergence-total'].value,'80');assert.equal(s.dom['#convergence-query'].value,'q1');
});
test('timestamps support Unix seconds, milliseconds, and ISO strings',()=>{
 const c=setup().context,seconds=1789354800,iso=new Date(seconds*1000).toISOString(),formatted=c.convergenceDate(seconds);
 assert.match(formatted,/2026\/09\/14/);assert.doesNotMatch(formatted,/1789354800/);assert.equal(c.convergenceDate(seconds*1000),formatted);assert.equal(c.convergenceDate(String(seconds)),formatted);assert.equal(c.convergenceDate(iso),formatted);
 for(const missing of [null,undefined,'','not a date'])assert.equal(c.convergenceDate(missing),'日時未記録');
 const html=c.convergenceHistoryHtml([{...records()[0],created_at:seconds}]);assert.match(html,/2026\/09\/14/);assert.doesNotMatch(html,/1789354800/);
});
test('untouched record fields follow auto-record metadata until the user edits',()=>{
 const s=setup();s.context.renderConvergence();assert.equal(s.dom['#convergence-total'].value,'');
 s.context.state.convergence.records=[{...records()[1],query_id:'q1'}];s.context.renderConvergence();assert.equal(s.dom['#convergence-total'].value,'105');assert.equal(s.dom['#convergence-query'].value,'q1');
 s.dom['#convergence-total'].value='250';s.context.captureConvergenceDraft();s.context.state.convergence.records[0].total_hits=110;s.context.renderConvergence();assert.equal(s.dom['#convergence-total'].value,'250');
});
test('only truly assessed unresolved rows appear in human review',()=>{
 const c=setup().context,valid={id:'p',label:null,label_source:'agent',label_reason:'本文不足',agent_confidence:.5};
 assert.equal(c.convergenceDeferred(valid),true);for(const patch of [{label:'keep'},{label:'exclude'},{label_source:'human'},{label_reason:''},{agent_confidence:null},{agent_confidence:'0.4'},{agent_confidence:NaN},{agent_confidence:-1},{agent_confidence:1.1}])assert.equal(c.convergenceDeferred({...valid,...patch}),false);
});
test('patent and search text is escaped in cards, chart labels and history',()=>{
 const c=setup().context,evil='<img src=x onerror=alert(1)>';const html=c.convergenceReviewHtml({title:evil,abstract:evil,ipc:evil,id:evil,label_reason:evil,agent_confidence:.5},2);
 assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
 const table=c.convergenceHistoryHtml([{...records()[0],expression:evil,created_at:evil,query_id:'q'}]);assert.doesNotMatch(table,/<img/);assert.match(table,/&lt;img/);
 const graph=c.convergenceChart([{ordinal:evil,new_keep_rate:.2}]);assert.doesNotMatch(graph,/<img/);
});
test('graph stability never completes automatically and running jobs disable writes',async()=>{
 const s=setup();s.context.state.convergence.status='stable';s.context.renderConvergence();assert.equal(s.dom['#convergence-complete'].disabled,true);await s.context.convergenceAction('complete');assert.equal(s.requests.length,0);
 s.context.state.convergence.eligible_to_complete=true;s.context.state.job.status='running';s.context.renderConvergence();for(const id of ['total','query','record','keep','exclude','complete'])assert.equal(s.dom['#convergence-'+id].disabled,true);
 await s.context.convergenceAction('record');assert.equal(s.requests.length,0);
});
test('record action sends explicit input and reuses the observation identity',async()=>{
 const s=setup();s.context.renderConvergence();s.dom['#convergence-total'].value='550';s.dom['#convergence-query'].value='q1';await s.context.convergenceAction('record');
 assert.deepEqual(plain(s.requests[0]),{path:'/convergence/record',body:{total_hits:550,query_id:'q1',observation_id:'csv1'}});assert.equal(s.requests.length,1);assert.equal(s.read('convergenceBusy'),false);
});
test('review cannot relabel an already confirmed row and sends no model request',async()=>{
 const s=setup();await s.context.convergenceAction('review',{id:'P1',label:'exclude'});assert.equal(s.requests.length,0);
 s.context.state.patents=[{id:'P2',label:null,label_source:'agent',label_reason:'本文が不足',agent_confidence:.6}];await s.context.convergenceAction('review',{id:'P2',label:'keep'});assert.deepEqual(plain(s.requests[0]),{path:'/convergence/review',body:{id:'P2',label:'keep',expected_reason:'本文が不足',expected_confidence:.6}});assert.equal(s.requests.length,1);
});
test('review click carries the assessment actually shown even if state changed in another tab',async()=>{
 const s=setup();s.context.state.patents=[{id:'P2',label:null,label_source:'agent',label_reason:'表示時点の理由',agent_confidence:.55}];s.context.renderConvergence();
 s.context.state.patents[0].label_reason='別タブで変わった理由';s.context.state.patents[0].agent_confidence=.7;
 await s.handlers.get('#convergence-keep:click')();assert.deepEqual(plain(s.requests[0].body),{id:'P2',label:'keep',expected_reason:'表示時点の理由',expected_confidence:.55});
});
test('failed actions preserve drafts and release busy guard without retrying the write',async()=>{
 const s=setup();s.context.renderConvergence();s.dom['#convergence-total'].value='230';s.context.api=async(path,body)=>{s.requests.push({path,body});throw Error('保存に失敗');};await assert.rejects(s.context.convergenceAction('record'),/保存に失敗/);
 assert.equal(s.read('convergenceBusy'),false);assert.equal(s.dom['#convergence-total'].value,'230');assert.equal(s.dom['#convergence-error'].hidden,false);assert.match(s.dom['#convergence-error'].textContent,/保存に失敗/);assert.equal(s.requests.length,1);
});
test('changed server state refreshes on conflict without silently resubmitting',async()=>{
 const s=setup();s.context.renderConvergence();s.context.api=async(path,body)=>{s.requests.push({path,body});if(path==='/state')return s.context.state;const e=Error('CSVが変更されました。');e.status=409;throw e;};
 await assert.rejects(s.context.convergenceAction('record'),/CSVが変更/);assert.deepEqual(s.requests.map(r=>r.path),['/convergence/record','/state']);assert.equal(s.read('convergenceBusy'),false);
});
test('styles preserve hidden controls and users motion settings',()=>{const css=fs.readFileSync('static/convergence-ui.css','utf8');assert.match(css,/#convergence-content\s+\[hidden\]\s*\{display:none!important/);assert.match(css,/prefers-reduced-motion:reduce/);assert.match(css,/data-atlas-motion=static/);});
