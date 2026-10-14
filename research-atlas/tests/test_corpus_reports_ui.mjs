import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/corpus-reports.js',import.meta.url),'utf8');
const resultID='a'.repeat(32),reportID='b'.repeat(32),otherResultID='c'.repeat(32),otherReportID='d'.repeat(32);
const result=(id=resultID)=>({id,meta:{papers_total:20000,papers_truncated:true,is_demo:false}});
const report=(extra={})=>({id:reportID,report_id:reportID,result_id:resultID,provider:'local',model:'qwen3:8b',status:'paused',stage:'100件を抽出しました。',batch_size:100,run_all:false,
  counts:{total:20000,completed:80,missing:10,failed:10,pending:19900,cached:4,processed_chars:80000,total_chars:20000000},estimate:{elapsed_seconds:600,remaining_seconds:149250,sampled_papers:80},
  partial:true,synthesis_status:'not_requested',annual:[],topics:[],methods:[],groups:[],warnings:[],narrative:null,...extra});
const page=(extra={})=>({items:[],total:20000,offset:0,limit:20,...extra});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('poll shows saved adaptive sizing and retry totals without changing browser connection settings',async()=>{
  let current=report({adaptive:{enabled:true,extraction:{max_chars:2500},synthesis:{max_tokens:1200,max_items:4},timeout_recoveries:1,target_seconds:120,max_timeout_recoveries:3}});
  const h=harness(url=>url.startsWith('/api/corpus-reports?')?{reports:[current]}:url===`/api/corpus-reports/${reportID}`?current:undefined);
  await h.mount();
  assert.match(h.host.innerHTML,/2,500文字/);assert.match(h.host.innerHTML,/累計1回/);
  current=report({...current,status:'paused',stage:'時間切れへの自動再試行の上限に達しました。',adaptive:{...current.adaptive,extraction:{max_chars:1250},timeout_recoveries:3}});
  await h.t.poll();
  assert.match(h.host.innerHTML,/1,250文字/);assert.match(h.host.innerHTML,/累計3回/);
  assert.match(h.host.innerHTML,/完了時間の保証ではありません/);
  assert.match(h.host.innerHTML,/失敗した論文を再試行/);
  assert.ok(h.writes.every(row=>row.key.startsWith('research-atlas:corpus-report:')));
});

test('OpenAI report does not claim LocalLLM adaptive recovery',async()=>{
  const value=report({provider:'openai'});
  const h=harness(url=>url.startsWith('/api/corpus-reports?')?{reports:[value]}:url===`/api/corpus-reports/${reportID}`?value:undefined);
  await h.mount();
  assert.doesNotMatch(h.host.innerHTML,/LocalLLMの速度に合わせて自動調整/);
});
function harness(responder){
  const calls=[],shown=[],writes=[],store=new Map(),timers=new Map(),events=new Map();let sequence=0;
  const host={innerHTML:'',isConnected:true,events:new Map(),addEventListener(name,fn){this.events.set(name,fn);},removeEventListener(name){this.events.delete(name);}};
  const window={__ATLAS_UI_TEST__:true,localStorage:{getItem:key=>store.get(key)||null,setItem(key,value){store.set(key,value);writes.push({key,value});}},
    addEventListener(name,fn){events.set(name,fn);}};
  const sandbox=vm.createContext({window,console,Promise,Map,Set,setTimeout(fn){const id=++sequence;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);}});
  vm.runInContext(source,sandbox);
  const context={showPaper:id=>shown.push(id),api:async(url,options)=>{
    calls.push({url,options});if(responder){const response=responder(url,options);if(response!==undefined)return response;}
    if(url==='/api/llm/status')return {local:{available:true},openai:{configured:false}};
    if(url.startsWith('/api/corpus-reports?'))return {reports:[]};
    if(url.includes('/papers?'))return page();
    return report();
  }};
  const t=window.__corpusReportsTest;
  return {t,host,calls,shown,writes,store,timers,events,context,
    mount:value=>t.mount(host,value||result(),context),
    field(name,value){t.change({target:{dataset:{crField:name},value}});},
    click(name,extra={}){const target={dataset:{crAction:name,...extra},closest(){return this;}};t.click({target,preventDefault(){}});}};
}
const body=call=>JSON.parse(call.options.body);
const mutations=h=>h.calls.filter(call=>call.options?.method==='POST');

test('whole-corpus tab loads the analysis library, defaults to a 100-paper trial and keeps connection settings untouched',async()=>{
  const h=harness();await h.mount();
  assert.ok(h.calls.some(call=>call.url===`/api/corpus-reports?result_id=${resultID}`));
  assert.match(h.host.innerHTML,/100件を試行/);assert.match(h.host.innerHTML,/1件ずつ/);
  assert.match(h.host.innerHTML,/画面を離れても処理は続きます/);
  await h.t.operation('start');assert.deepEqual(body(mutations(h)[0]),{batch_size:100,run_all:false,result_id:resultID,provider:'local'});
  assert.ok(h.writes.length);assert.ok(h.writes.every(item=>item.key===`research-atlas:corpus-report:${resultID}`&&item.value===reportID));
  assert.equal(h.t.s.status.local.available,true);
});

test('custom batch and all-corpus execution preserve API contracts and the chosen provider',async()=>{
  const h=harness();await h.mount();h.field('provider','openai');h.field('model','  gpt-4.1-mini  ');h.field('mode','all');h.field('batch','20000');
  await h.t.operation('start');assert.deepEqual(body(mutations(h)[0]),{batch_size:20000,run_all:true,result_id:resultID,provider:'openai',model:'gpt-4.1-mini'});
  assert.ok(h.writes.every(item=>!item.value.includes('gpt')));
  h.field('mode','batch');h.field('batch','317');await h.t.operation('resume');
  assert.deepEqual(body(mutations(h)[1]),{batch_size:317,run_all:false,retry_failed:false});
  await h.t.operation('retry');assert.deepEqual(body(mutations(h)[2]),{batch_size:317,run_all:false,retry_failed:true});
});

test('invalid custom batch sizes never issue a mutation',async()=>{
  for(const value of ['', '0','20001','1.5','NaN','Infinity','-3']){
    const h=harness();await h.mount();h.field('mode','batch');h.field('batch',value);await h.t.operation('start');
    assert.equal(mutations(h).length,0,value);assert.match(h.t.s.error,/1〜20,000件の整数/);assert.equal(h.t.s.busy,false);
  }
});

test('pause and synthesis reuse a report without changing its saved provider or model',async()=>{
  const h=harness();await h.mount();await h.t.selectReport(reportID);h.t.s.provider='openai';h.t.s.model='other-model';
  await h.t.operation('pause');await h.t.operation('synthesize');
  assert.deepEqual(mutations(h).map(call=>({url:call.url,body:body(call)})),[
    {url:`/api/corpus-reports/${reportID}/pause`,body:{}},{url:`/api/corpus-reports/${reportID}/synthesize`,body:{}}]);
});

test('ID-only creation responses are followed by a checked report fetch',async()=>{
  const h=harness((url,options)=>url==='/api/corpus-reports'&&options?{report_id:reportID}:undefined);await h.mount();await h.t.operation('start');
  assert.equal(h.t.s.report.id,reportID);assert.ok(h.calls.some(call=>call.url===`/api/corpus-reports/${reportID}`));
});

test('saved report selection restores only the remembered ID for the current analysis',async()=>{
  const other=report({id:otherReportID,report_id:otherReportID});
  const h=harness(url=>url.startsWith('/api/corpus-reports?')?{reports:[report(),other,report({result_id:otherResultID})]}:url===`/api/corpus-reports/${otherReportID}`?other:undefined);
  h.store.set(`research-atlas:corpus-report:${resultID}`,otherReportID);await h.mount();
  assert.equal(h.t.s.report.id,otherReportID);assert.equal(h.t.s.reports.length,2);
  assert.match(h.host.innerHTML,/qwen3:8b/);assert.match(h.host.innerHTML,/既存レポートは同じ方式・モデルで再開/);
});

test('report result ownership and invalid identifiers are checked before adoption',async()=>{
  const h=harness(url=>url===`/api/corpus-reports/${reportID}`?report({result_id:otherResultID}):undefined);await h.mount();
  await h.t.selectReport(reportID);assert.equal(h.t.s.report,null);assert.match(h.t.s.error,/一致しません/);assert.equal(h.writes.length,0);
  const count=h.calls.length;await h.t.selectReport('../settings');assert.equal(h.calls.length,count);
});

test('coverage distinguishes missing and failed abstracts from completion and estimates time',async()=>{
  const h=harness();await h.mount();await h.t.selectReport(reportID);
  assert.match(h.host.innerHTML,/0\.4%/);assert.match(h.host.innerHTML,/部分レポート/);assert.match(h.host.innerHTML,/抄録欠損/);
  assert.match(h.host.innerHTML,/正しい理解や知見の完全な抽出を保証しません/);assert.match(h.host.innerHTML,/論文本文は対象に含みません/);
  assert.match(h.host.innerHTML,/20,000/);assert.match(h.host.innerHTML,/41\.5時間/);
  assert.equal(h.t.duration(null),'計測待ち');
});

test('all exports and year/topic aggregation remain reachable for partial results',async()=>{
  const h=harness(url=>url===`/api/corpus-reports/${reportID}`?report({annual:[{year:2021,total:10,completed:7,missing:1,failed:1,pending:1}],topics:[{label:'Steel <fatigue>',total:20,completed:10,missing:5,failed:2,pending:3}],methods:[{method:'EBSD <analysis>',paper_count:12}]}):undefined);
  await h.mount();await h.t.selectReport(reportID);
  for(const format of ['pdf','csv','json'])assert.ok(h.host.innerHTML.includes(`/api/corpus-reports/${reportID}/export?format=${format}`));
  assert.match(h.host.innerHTML,/2021/);assert.match(h.host.innerHTML,/Steel &lt;fatigue&gt;/);assert.match(h.host.innerHTML,/EBSD &lt;analysis&gt;/);
});

test('verification warnings retain full narrative text and bound large provenance lists',()=>{
  const h=harness(),text=h.t.narrativeHTML({text:'Detailed <conclusion>',warnings:[{code:'numeric_mismatch',message:'数値の不一致'}],sections:[{title:'移行',text:'2021から2022へ。',paper_ids:Array.from({length:20000},(_,i)=>`p${i}`),warnings:[{code:'numeric_mismatch',message:'単位の不一致'}]}]});
  assert.match(text,/⚠ 照合に注意/);assert.match(text,/Detailed &lt;conclusion&gt;/);assert.match(text,/単位の不一致/);
  assert.match(text,/20,000論文/);assert.match(text,/全IDはJSON/);assert.doesNotMatch(text,/p19999/);assert.ok(text.length<3000);
});

test('stored prose and paper extraction are escaped and source buttons use the existing original-paper callback',async()=>{
  const attack='<img src=x onerror=alert(1)>',h=harness(url=>url.includes('/papers?')?page({items:[{id:'paper-x',title:attack,year:2025,status:'completed',extraction:{facts:[{statement:attack}]} }]}):undefined);
  await h.mount();await h.t.selectReport(reportID);assert.doesNotMatch(h.host.innerHTML,/<img/);assert.match(h.host.innerHTML,/&lt;img/);
  h.click('source',{crPaper:'paper-x'});assert.deepEqual(h.shown,['paper-x']);
  const text=h.t.narrativeHTML({headline:attack,text:attack,sections:[{title:attack,text:attack}],warnings:[{message:attack}]});assert.doesNotMatch(text,/<img/);
});

test('audit paging/filtering uses 20-item server pages and guards oversized responses',async()=>{
  const h=harness(url=>url.includes('/papers?')?page({items:Array.from({length:250},(_,i)=>({id:`p${i}`,title:`Paper ${i}`,status:'pending'}))}):undefined);
  await h.mount();await h.t.selectReport(reportID);assert.equal(h.t.s.papers.items.length,20);assert.doesNotMatch(h.host.innerHTML,/Paper 21/);
  h.click('next');await tick();assert.ok(h.calls.at(-1).url.endsWith('offset=20&limit=20'));
  h.field('filter','failed');await tick();assert.ok(h.calls.at(-1).url.endsWith('offset=0&limit=20&status=failed'));
});

test('a stale audit response or error cannot overwrite a newer filter',async()=>{
  const pending=deferred();let delay=false;
  const h=harness(url=>delay&&url.includes('/papers?')&&!url.includes('status=failed')?pending.promise:url.includes('status=failed')?page({items:[{id:'failed-new',status:'failed',title:'Current failure'}]}):undefined);
  await h.mount();await h.t.selectReport(reportID);delay=true;const old=h.t.loadPapers();h.field('filter','failed');await tick();pending.reject(new Error('old error'));await old;
  assert.equal(h.t.s.papers.items[0].id,'failed-new');assert.equal(h.t.s.auditError,'');assert.doesNotMatch(h.host.innerHTML,/old error/);
});

test('polling ends on unmount, leaves server work running, and restores from the durable report on return',async()=>{
  const r=report({status:'running'}),h=harness(url=>url.startsWith('/api/corpus-reports?')?{reports:[r]}:url===`/api/corpus-reports/${reportID}`?r:undefined);
  await h.mount();assert.equal(h.timers.size,1);h.t.unmount();assert.equal(h.timers.size,0);assert.equal(h.host.events.size,0);assert.equal(mutations(h).length,0);
  await h.mount();assert.equal(h.t.s.report.id,reportID);assert.equal(h.timers.size,1);
});

test('result change invalidates late creation responses without persisting their report ID',async()=>{
  const pending=deferred(),h=harness((url,options)=>url==='/api/corpus-reports'&&options?pending.promise:undefined);
  await h.mount();const creating=h.t.operation('start');await h.mount(result(otherResultID));pending.resolve(report());await creating;
  assert.equal(h.t.s.result.id,otherResultID);assert.equal(h.t.s.report,null);assert.equal(h.writes.length,0);assert.equal(h.t.s.busy,false);
});

test('late library and detail responses never populate a different analysis',async()=>{
  const slow=deferred(),h=harness(url=>url===`/api/corpus-reports?result_id=${resultID}`?slow.promise:undefined);
  const previous=h.mount();await h.mount(result(otherResultID));slow.resolve({reports:[report()]});await previous;
  assert.equal(h.t.s.reports.length,0);assert.equal(h.t.s.result.id,otherResultID);
  const detail=deferred(),second=harness(url=>url===`/api/corpus-reports/${reportID}`?detail.promise:undefined);
  await second.mount();const choosing=second.t.selectReport(reportID);await second.mount(result(otherResultID));detail.resolve(report());await choosing;assert.equal(second.t.s.report,null);
});

test('late report polling cannot revert a newer paused state',async()=>{
  const old=deferred();let delay=false;
  const h=harness(url=>delay&&url===`/api/corpus-reports/${reportID}`?old.promise:undefined);await h.mount();await h.t.selectReport(reportID);
  delay=true;const polling=h.t.poll();await h.t.operation('pause');old.resolve(report({status:'running',stage:'old'}));await polling;
  assert.equal(h.t.s.report.status,'paused');assert.notEqual(h.t.s.report.stage,'old');assert.equal(h.timers.size,0);
});

test('two overlapping refreshes keep the latest response',async()=>{
  const first=deferred(),second=deferred();let refresh=0;
  const h=harness(url=>refresh&&url===`/api/corpus-reports/${reportID}`?(refresh++===1?first.promise:second.promise):undefined);
  await h.mount();await h.t.selectReport(reportID);refresh=1;const a=h.t.poll(),b=h.t.poll();second.resolve(report({stage:'newest'}));await b;first.resolve(report({stage:'stale'}));await a;assert.equal(h.t.s.report.stage,'newest');
});

test('mutation failures preserve existing extraction and unlock controls',async()=>{
  const h=harness((url,options)=>options&&url.endsWith('/resume')?Promise.reject(new Error('接続設定を確認してください。')):undefined);
  await h.mount();await h.t.selectReport(reportID);await h.t.operation('resume');
  assert.equal(h.t.s.report.counts.completed,80);assert.equal(h.t.s.busy,false);assert.match(h.host.innerHTML,/保存済みの抽出結果と元の分析は保持/);assert.match(h.host.innerHTML,/接続設定を確認/);
});

test('application result reset stops polling and removes handlers',async()=>{
  const h=harness();await h.mount();h.events.get('atlas:result')({detail:null});assert.equal(h.t.s.host,null);assert.equal(h.host.events.size,0);
});

test('saved connection edits invalidate old status replies without rewriting settings',async()=>{
  const previous=deferred();let requests=0;
  const h=harness(url=>url==='/api/llm/status'?(++requests===1?previous.promise:{local:{available:false},openai:{configured:true}}):undefined);
  await h.mount();h.events.get('atlas:connections-changed')({detail:{revision:2}});await tick();previous.resolve({local:{available:true},openai:{configured:false}});await tick();
  assert.equal(h.t.s.status.local.available,false);assert.equal(h.t.s.status.openai.configured,true);assert.equal(h.writes.length,0);
});

test('verification alerts remain readable when thousands of papers share a different caution',()=>{
  const h=harness(),text=h.t.narrativeHTML({text:'評論',warnings:[...Array.from({length:20000},()=>({code:'fact_limit',message:'抽出件数が上限'})),{code:'numeric_mismatch',message:'確認できない数値が存在'}]});
  assert.match(text,/確認できない数値が存在/);assert.match(text,/20,001件/);assert.ok(text.length<2000);
});

test('invalid or absent analysis is explained without API calls',async()=>{
  const h=harness();await h.t.mount(h.host,null,h.context);assert.match(h.host.innerHTML,/分析結果を読み込んで/);assert.equal(h.calls.length,0);
});

test('preparation failure with zero rows remains resumable and can be replaced',async()=>{
  const h=harness(url=>url===`/api/corpus-reports/${reportID}`?report({status:'error',counts:{total:0,pending:0,completed:0}}):undefined);
  await h.mount();await h.t.selectReport(reportID);
  assert.match(h.host.innerHTML,/data-cr-action="resume" >未処理分を再開/);assert.match(h.host.innerHTML,/data-cr-action="new" >別のレポート/);
  await h.t.operation('resume');assert.ok(mutations(h).some(call=>call.url.endsWith('/resume')));
});

test('partially extracted papers with grounded facts can be synthesized',async()=>{
  const h=harness(url=>url===`/api/corpus-reports/${reportID}`?report({has_facts:true,counts:{total:20,pending:10,failed:10,completed:0}}):undefined);
  await h.mount();await h.t.selectReport(reportID);
  assert.match(h.host.innerHTML,/data-cr-action="synthesize" >評論を生成/);assert.match(h.host.innerHTML,/評論時間を含まず/);
});

test('paper facts present readable statements, exact quotes and numerical warnings before JSON',()=>{
  const h=harness(),html=h.t.paperHTML({id:'p1',status:'failed',extraction:{facts:[{kind:'negative_result',statement:'900 MPaと記述された。',quote:'The reported value was 800 MPa.',warnings:[{code:'numeric_mismatch',message:'数値照合に注意',unmatched_numbers:['900'],unmatched_quantities:[{value:'900',unit:'MPa'}],location:'facts/0'}]}]}});
  assert.match(html,/否定的な結果/);assert.match(html,/900 MPaと記述された/);assert.match(html,/抄録原文/);assert.match(html,/The reported value was 800 MPa/);
  assert.match(html,/未照合の数値・単位：900 MPa/);assert.ok(html.indexOf('cr-fact')<html.indexOf('構造化した抽出結果（JSON）'));
});

test('a completed batch with outstanding papers is labeled as a partial result in both heading and history',async()=>{
  const h=harness(url=>url===`/api/corpus-reports/${reportID}`?report({status:'completed',partial:true}):undefined);
  await h.mount();await h.t.selectReport(reportID);assert.equal((h.host.innerHTML.match(/今回の処理完了（部分結果）/g)||[]).length,2);
});

test('compact narrative previews preserve original source counts and summarized warning alerts',()=>{
  const h=harness(),html=h.t.narrativeHTML({preview:true,text:'本文は全て残る。',warning_count:1400,warnings:[{code:'compression',message:'圧縮による注意'}],warning_summary:[{code:'numeric_mismatch',count:300}],sections:[{title:'変遷',text:'詳細な論述',paper_ids:['p1','p2'],paper_ids_count:20000,paper_ids_truncated:true}]});
  assert.match(html,/⚠ 照合に注意/);assert.match(html,/1,400件/);assert.match(html,/根拠 20,000論文/);assert.match(html,/評論本文はそのまま表示/);assert.match(html,/詳細な論述/);
});

test('new tab assets load before the application and lifecycle integrates with valid restore views',()=>{
  const html=readFileSync(new URL('../static/index.html',import.meta.url),'utf8'),app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
  assert.match(html,/data-view="corpus-reports"/);assert.match(html,/corpus-reports\.css/);assert.ok(html.indexOf('/static/corpus-reports.js')<html.indexOf('/static/app.js'));
  assert.match(app,/"corpus-reports":\["WHOLE CORPUS REPORT"/);assert.match(app,/AtlasCorpusReports\?\.mount\(\$\('#corpus-reports-root'\),state.result,\{api,showPaper\}\)/);
  assert.equal((app.match(/AtlasCorpusReports\?\.unmount\(\)/g)||[]).length,3);
});
