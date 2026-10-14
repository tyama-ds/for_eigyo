import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const annualCode=readFileSync(new URL('../static/annual-landscape.js',import.meta.url),'utf8');
const landscapeCode=readFileSync(new URL('../static/landscape.js',import.meta.url),'utf8');
const annualCSS=readFileSync(new URL('../static/annual-landscape.css',import.meta.url),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const chapter=(year,text=`${year}年の抄録を詳しく比較しました。`)=>({id:`embedded-${year}`,generation_status:'generated',requested_provider:'local',evidence_papers:[{id:`p${year}`,citation_id:'P1',side:'centroid',title:`Study ${year}`,abstract:'Measured materials and results.'}],input_summary:{paper_count:1,abstract_count:1,missing_abstract_count:0},narrative:{mode:'local_llm',headline:`${year}年の研究対象`,sections:[{title:'研究内容',text,evidence_ids:[`p${year}`]}],caveats:[],validation:{status:'passed',warnings:[]}}});
const report=(extra={})=>({id:'annual-1',kind:'annual',result_id:'result-a',projection:'pca',projection_id:'proj-y',interval:'year',scope:'full',provider:'local',topic:{id:'topic-a',label:'Materials'},start_year:2022,end_year:2024,include_transitions:false,generation_status:'generated',annual_rows:[{year:2022,count:20,period_count:100,share_of_period:.2,coverage_status:'observed'},{year:2023,count:30,period_count:100,share_of_period:.3,coverage_status:'observed'},{year:2024,count:40,period_count:200,share_of_period:.2,coverage_status:'observed'}],years:[{year:2022,period_id:'2022',count:20,period_count:100,share_of_period:.2,generation_status:'generated',coverage_status:'observed',report:chapter(2022)},{year:2023,period_id:'2023',count:30,period_count:100,share_of_period:.3,generation_status:'generated',coverage_status:'observed',report:chapter(2023)},{year:2024,period_id:'2024',count:40,period_count:200,share_of_period:.2,generation_status:'generated',coverage_status:'observed',report:chapter(2024)}],transitions:[],overview:{mode:'deterministic',title:'期間全体の観測',text:'論文数と構成比を計算したまとめです。'},limitations:['取得集合内の観測です。'],progress:{completed:3,total:3,llm_calls:3,planned_llm_calls:3},...extra});
function eventSurface(){const events=new Map();return {events,addEventListener(type,callback){if(!events.has(type))events.set(type,[]);events.get(type).push(callback);},dispatch(type,event={}){for(const callback of events.get(type)||[])callback(event);}};}
const annualSnapshot={result_id:'result-a',scope:'full',interval:'year',projection_id:'annual-projection',periods:[{id:'2022'},{id:'2023'},{id:'2024'}],centroids:[{topic_id:'topic-a',period_id:'2022',count:20},{topic_id:'topic-a',period_id:'2023',count:30},{topic_id:'topic-a',period_id:'2024',count:40}]};
function harness(api=async url=>url.includes('/landscape?')?annualSnapshot:url==='/api/landscape-annual-reports'?{job_id:'job-1',annual_report_id:'annual-1'}:url.startsWith('/api/jobs/')?{status:'completed'}:report()){
  const timers=new Map(),calls=[],classes=new Set(),window=eventSurface(),document=eventSurface();let timerID=0,printed=0;
  Object.assign(window,{__ATLAS_UI_TEST__:true,setTimeout(callback){const id=++timerID;timers.set(id,callback);return id;},clearTimeout(id){timers.delete(id);},print(){printed++;}});
  const root={dataset:{alKey:''},html:'',renders:0,progress:{textContent:''},querySelector(selector){return selector==='.al-progress strong'?this.progress:null;},set outerHTML(value){this.html=value;this.renders++;}};
  const extraNodes=new Map();
  Object.assign(document,{querySelector:selector=>selector==='#annual-landscape-panel'?root:extraNodes.get(selector)||null,createElement(){return {id:'',innerHTML:'',details:[{open:false}],querySelectorAll(){return this.details;},remove(){extraNodes.delete(`#${this.id}`);}};},body:{appendChild(node){extraNodes.set(`#${node.id}`,node);},classList:{add:name=>classes.add(name),remove:name=>classes.delete(name)}}});
  const sandbox=vm.createContext({window,document,AbortController,console,Promise,Map,Set});
  vm.runInContext(annualCode,sandbox);vm.runInContext(landscapeCode,sandbox);
  const context={result:{id:'result-a',topics:[{id:'topic-a',label:'Materials'}],meta:{years:[2020,2021,2022,2023,2024,2025]}},e:esc,num:value=>String(value),api:(url,options)=>{calls.push({url,options});return api(url,options);}};
  const landscape={result_id:'result-a',projection_id:'proj-y',topics:context.result.topics,periods:[{id:'2022'},{id:'2023'},{id:'2024'}],centroids:[{topic_id:'topic-a',period_id:'2022',count:20},{topic_id:'topic-a',period_id:'2023',count:30},{topic_id:'topic-a',period_id:'2024',count:40}]};
  const prefs={topic:'topic-a',projection:'pca',scope:'full',interval:'year',provider:'local'};
  const t=window.__annualLandscapeTest;
  const mount=(ctx=context,land=landscape,preferences=prefs)=>{root.html=t.panel(ctx,land,preferences);root.dataset.alKey=t.state.key;return root.html;};mount();
  return {t,window,document,context,landscape,prefs,root,calls,timers,classes,get printed(){return printed;},mount,fireTimer(){const [id,callback]=timers.entries().next().value||[];if(callback){timers.delete(id);callback();}}};
}

test('annual payload preserves selected topic and analysis scope and defaults to year reviews only',async()=>{
  const h=harness();await h.t.generate();
  const request=JSON.parse(h.calls[0].options.body);
  assert.deepEqual(request,{result_id:'result-a',projection:'pca',scope:'full',topic_id:'topic-a',start_year:2022,end_year:2024,provider:'local',include_transitions:false,interval:'year',projection_id:'proj-y'});
  assert.equal(h.calls.filter(call=>call.url==='/api/landscape-annual-reports').length,1);
  assert.equal(h.t.state.report.id,'annual-1');assert.equal(h.t.state.busy,false);
  assert.equal(h.t.state.start,2022,'actual yearly periods take precedence over wider result metadata');
});

test('quarterly and monthly maps read an annual snapshot and never pass their own projection IDs',async()=>{
  for(const interval of ['quarter','month']){
    const h=harness(),prefs={...h.prefs,interval};h.mount(h.context,{...h.landscape,projection_id:'nonannual',periods:[{id:'2022-01'},{id:'2024-12'}]},prefs);
    assert.throws(()=>h.t.payload(),/年別の分析範囲を準備/);await tick();
    const request=h.t.payload();assert.equal(request.interval,'year');assert.equal(request.projection_id,'annual-projection');assert.equal(request.start_year,2022);assert.equal(request.end_year,2024);
    assert.match(h.calls[0].url,/interval=year&scope=full/);h.mount(h.context,{...h.landscape,projection_id:'nonannual'},prefs);await tick();assert.equal(h.calls.length,1,'annual snapshot is cached across rerenders');
  }
});

test('monthly subset cannot exclude year-only endpoint papers or include unobserved endpoint years',async()=>{
  const snapshot={...annualSnapshot,periods:[{id:'2021'},{id:'2022'},{id:'2023'}],centroids:[{topic_id:'topic-a',period_id:'2021',count:2},{topic_id:'topic-a',period_id:'2022',count:4},{topic_id:'topic-a',period_id:'2023',count:3}]};
  const h=harness(async()=>snapshot),context={...h.context,result:{...h.context.result,meta:{years:[2020,2021,2022,2023,2024]}}},prefs={...h.prefs,interval:'month'};
  h.mount(context,{...h.landscape,periods:[{id:'2022-06'}],centroids:[{topic_id:'topic-a',period_id:'2022-06',count:4}]},prefs);
  await tick();
  assert.equal(h.t.state.start,2021);assert.equal(h.t.state.end,2023);assert.equal(h.t.estimate().calls,3);
  h.t.handleChange({target:{dataset:{alControl:'transitions'},checked:true}});assert.equal(h.t.estimate().calls,5);
  assert.match(h.root.html,/年だけが分かる論文を含めて年別に再集計/);
});

test('all topics and invalid annual ranges cannot submit a report',async()=>{
  const h=harness();h.prefs.topic='all';h.mount();await h.t.generate();assert.equal(h.calls.length,0);assert.match(h.root.html,/話題を1つ選んでください/);
  h.prefs.topic='topic-a';h.mount();h.t.state.start=2024;h.t.state.end=2022;await h.t.generate();assert.equal(h.calls.length,0);assert.match(h.t.state.error,/開始年/);
  h.t.state.start=2021;h.t.state.end=2024;assert.throws(()=>h.t.payload(),/2022〜2024/);
});

test('transition estimate is an explicit upper bound and latest inherited provider is used',()=>{
  const h=harness();assert.equal(h.t.estimate().calls,3);h.t.handleChange({target:{dataset:{alControl:'transitions'},checked:true}});
  assert.equal(h.t.estimate().calls,5);assert.match(h.root.html,/最大 5回（各年 3 ＋ 比較 2）/);
  h.prefs.provider='openai';h.window.AtlasAnnualLandscape.setProvider();assert.match(h.root.html,/OpenAI API/);assert.equal(h.t.payload().provider,'openai');
  h.prefs.provider='none';h.window.AtlasAnnualLandscape.setProvider();assert.equal(h.t.estimate().calls,0);assert.match(h.root.html,/LLM呼び出しなし/);
});

test('year reviews render in time order with actual count/share and expanded warning-bearing prose',()=>{
  const h=harness(),data=report();data.years.reverse();data.annual_rows.reverse();
  data.years.find(row=>row.year===2023).report.narrative.validation={status:'warning',warnings:[{code:'numeric_mismatch',message:'数値を確認してください。'}]};
  const html=h.t.reportHTML(data);
  assert.ok(html.indexOf('id="annual-year-2022"')<html.indexOf('id="annual-year-2023"'));assert.ok(html.indexOf('id="annual-year-2023"')<html.indexOf('id="annual-year-2024"'));
  assert.match(html,/20\.0%/);assert.match(html,/>40<small>論文/);assert.doesNotMatch(html,/>1<small>論文/);
  assert.ok(html.indexOf('2023年の抄録を詳しく比較しました')<html.indexOf('数値を確認してください'));
  assert.match(html,/data-paper="p2023"/);assert.match(html,/計算結果のまとめ/);assert.match(html,/LLMによる全期間の総合評論ではありません/);
  assert.doesNotMatch(html,/api\/landscape-reports\/embedded/,'embedded reports must not offer nonexistent standalone exports');
});

test('zero topic papers, unobserved years and missing abstracts receive distinct explanations',()=>{
  const h=harness();
  const unknown=h.t.yearHTML({year:2020,count:0,period_count:0,share_of_period:null,coverage_status:'no_corpus_observation',generation_status:'skipped',report:null});
  assert.match(unknown,/分析対象の論文を観測していません/);assert.match(unknown,/構成比 —/);assert.doesNotMatch(unknown,/0\.0%/);
  const zero=h.t.yearHTML({year:2021,count:0,period_count:80,share_of_period:0,coverage_status:'topic_zero',generation_status:'skipped',report:null});
  assert.match(zero,/選択した話題の論文がありません/);assert.match(zero,/構成比 0\.0%/);
  const noAbstract=h.t.yearHTML({year:2022,count:8,period_count:80,share_of_period:.1,coverage_status:'observed',generation_status:'skipped',report:null});
  assert.match(noAbstract,/評論に使える抄録が不足/);assert.doesNotMatch(noAbstract,/class="landscape-generated"/);
});

test('partial completed chapters remain visible while later chapters generate',async()=>{
  let reads=0;const partial=report({generation_status:'generating',progress:{completed:1,total:3,llm_calls:1,planned_llm_calls:3}});partial.years[1].generation_status='generating';partial.years[1].report=null;partial.years[2].generation_status='pending';partial.years[2].report=null;
  const h=harness(async url=>url==='/api/landscape-annual-reports'?{job_id:'job-1',annual_report_id:'annual-1'}:url.startsWith('/api/jobs/')?{status:reads?'completed':'running'}:++reads===1?partial:report());
  const task=h.t.generate();await tick();
  assert.equal(h.t.state.busy,true);assert.match(h.root.html,/2022年の抄録を詳しく比較しました/);assert.match(h.root.html,/この章の評論を生成しています/);assert.match(h.root.html,/順番を待っています/);
  assert.equal(h.timers.size,1);h.fireTimer();await task;assert.equal(h.t.state.busy,false);assert.match(h.root.html,/2024年の抄録を詳しく比較しました/);
});

test('late dataset, topic and range responses cannot replace current annual selection',async()=>{
  for(const change of ['dataset','topic','range']){
    let release;const h=harness(url=>url==='/api/landscape-annual-reports'?Promise.resolve({job_id:'job-1',annual_report_id:'annual-1'}):url.startsWith('/api/jobs/')?Promise.resolve({status:'completed'}):new Promise(resolve=>release=resolve));
    const task=h.t.generate();await tick();
    if(change==='dataset')h.mount({...h.context,result:{...h.context.result,id:'result-b'}});
    if(change==='topic')h.mount(h.context,h.landscape,{...h.prefs,topic:'topic-b'});
    if(change==='range')h.t.handleChange({target:{dataset:{alControl:'start'},value:'2023'}});
    release(report());await task;assert.equal(h.t.state.report,null);assert.equal(h.timers.size,0);assert.doesNotMatch(h.root.html,/2022年の抄録を詳しく比較しました/);
  }
});

test('cancel stops further polling and retains completed chapters and terminal status',async()=>{
  const partial=report({generation_status:'generating',progress:{completed:1,total:3,llm_calls:1,planned_llm_calls:3}});partial.years[1]={...partial.years[1],report:null,generation_status:'pending'};
  const stopped={...partial,generation_status:'cancelled'};
  const h=harness(async url=>url==='/api/landscape-annual-reports'?{job_id:'job-1',annual_report_id:'annual-1'}:url.endsWith('/cancel')?{annual_report_id:'annual-1',generation_status:'cancelled',cancel_requested:true,report:stopped}:url.startsWith('/api/jobs/')?{status:'running'}:partial);
  const task=h.t.generate();await tick();await h.t.cancel();await task;
  assert.equal(h.calls.find(call=>call.url.endsWith('/cancel')).options.method,'POST');assert.equal(h.t.state.busy,false);assert.equal(h.timers.size,0);assert.equal(h.t.state.report.generation_status,'cancelled');assert.match(h.root.html,/2022年の抄録を詳しく比較しました/);
  assert.equal(h.calls.filter(call=>call.url.startsWith('/api/jobs/')).length,1);
});

test('camera rerenders preserve report while scope and projection changes invalidate it',()=>{
  const h=harness();h.t.state.report=report();h.prefs.camera={yaw:.4,pitch:.8};h.mount();assert.equal(h.t.state.report.id,'annual-1');
  h.prefs.scope='sample';h.mount();assert.equal(h.t.state.report,null);h.t.state.report=report();h.prefs.projection='tsne';h.mount();assert.equal(h.t.state.report,null);
});

test('server replies for a different annual scope or snapshot are rejected',()=>{
  const h=harness(),request=h.t.payload();
  for(const patch of [{scope:'sample'},{topic:{id:'topic-b'}},{start_year:2021},{interval:'month'},{projection_id:'another'}])assert.throws(()=>h.t.checkReport(report(patch),request),/別の話題・期間/);
});

test('all untrusted labels, overview prose and input abstracts are escaped',()=>{
  const h=harness(),data=report({topic:{id:'topic-a',label:'<img src=x onerror=evil>'},overview:{text:'<script>bad</script>'},limitations:['<iframe src=evil>']});data.years[0].terms=[{term:'<svg onload=evil>'}];data.years[0].report.evidence_papers[0].abstract='<script>input</script>';
  const html=h.t.reportHTML(data);assert.doesNotMatch(html,/<(?:img|script|iframe|svg)[\s>]/);assert.match(html,/&lt;img/);assert.match(html,/&lt;script&gt;input/);assert.match(html,/&lt;svg/);
});

test('actual transition gaps are labeled and print clones only the annual report with cleanup',()=>{
  const h=harness();const html=h.t.transitionHTML({from_period:'2020',to_period:'2023',generation_status:'generated',report:chapter(2023)});assert.match(html,/連続する年の比較ではありません/);
  h.t.state.report=report();h.t.handleClick({target:{closest:()=>({dataset:{alAction:'print'}})}});assert.equal(h.printed,1);assert.ok(h.classes.has('annual-printing'));
  const print=h.document.querySelector('#annual-landscape-print');assert.ok(print);assert.match(print.innerHTML,/ANNUAL RESEARCH REVIEW/);assert.equal(print.details[0].open,true);
  h.window.dispatch('afterprint');assert.ok(!h.classes.has('annual-printing'));assert.equal(h.document.querySelector('#annual-landscape-print'),null);
  assert.match(annualCSS,/body\.annual-printing>:not\(#annual-landscape-print\)\{display:none!important\}/);assert.match(annualCSS,/position:static!important/);assert.doesNotMatch(annualCSS,/visibility:hidden/);
});

test('unchanged progress polls update only the small status label without refetching or replacing prose',async()=>{
  let polls=0,reads=0;const partial=report({generation_status:'generating',progress:{completed:1,total:3,llm_calls:2,planned_llm_calls:3,active:{kind:'year',label:'2023'}}});
  const h=harness(async url=>{if(url==='/api/landscape-annual-reports')return {job_id:'job-1',annual_report_id:'annual-1'};if(url.startsWith('/api/jobs/')){polls++;return {status:polls>=3?'completed':'running',stage:`受信 ${polls}秒`,generation_status:polls>=3?'generated':'generating',progress:partial.progress};}reads++;return polls>=3?report():partial;});
  const task=h.t.generate();await tick();const renders=h.root.renders;assert.equal(reads,1);
  h.fireTimer();await tick();assert.equal(reads,1);assert.equal(h.root.renders,renders);assert.equal(h.root.progress.textContent,'受信 2秒');
  h.fireTimer();await task;assert.equal(reads,2);assert.equal(h.t.state.busy,false);
});

test('annual metric timeline retains not-yet-prepared years after cancellation',()=>{
  const h=harness(),data=report({generation_status:'cancelled'});data.years=data.years.slice(0,1);
  const html=h.t.reportHTML(data);assert.match(html,/id="annual-year-2022"/);assert.match(html,/id="annual-year-2023"/);assert.match(html,/id="annual-year-2024"/);assert.match(html,/この章の生成前に停止/);assert.match(html,/2022年の抄録を詳しく比較しました/);
});


test('annual comparison evidence settings are opt-in and included independently of yearly representative reviews',()=>{
  const h=harness();assert.equal(h.t.payload().papers_per_period,undefined);
  h.t.handleChange({target:{dataset:{alControl:'transitions'},checked:true}});
  h.t.handleChange({target:{dataset:{alControl:'papers_per_period'},value:'15'}});
  h.t.handleChange({target:{dataset:{alControl:'selection_method'},value:'recent'}});
  h.t.handleChange({target:{dataset:{alControl:'abstract_only'},checked:true}});
  const request=h.t.payload();assert.equal(request.papers_per_period,15);assert.equal(request.selection_method,'recent');assert.equal(request.abstract_only,true);
  assert.match(h.root.html,/各年は重心付近の最大6論文/);assert.match(h.root.html,/期間比較だけに適用/);
  for(const value of ['0','21','2.5','']){h.t.handleChange({target:{dataset:{alControl:'papers_per_period'},value}});assert.throws(()=>h.t.payload(),/1〜20件の整数/);}
});

test('annual saved selection is checked and displayed from the report rather than the current controls',()=>{
  const h=harness();h.t.handleChange({target:{dataset:{alControl:'transitions'},checked:true}});
  const data=report({include_transitions:true,selection:{papers_per_period:6,selection_method:'centroid',abstract_only:false,method_label:'<saved method>'}});
  assert.equal(h.t.checkReport(data,h.t.payload()),data);
  const html=h.t.reportHTML(data);assert.match(html,/&lt;saved method&gt;/);assert.doesNotMatch(html,/<saved method>/);
  assert.throws(()=>h.t.checkReport({...data,selection:{papers_per_period:5,selection_method:'centroid'}},h.t.payload()),/異なる論文選択条件/);
  h.t.state.report=data;h.t.handleChange({target:{dataset:{alControl:'selection_method'},value:'diverse'}});assert.equal(h.t.state.report,null);
});

test('annual evidence controls lock during generation and changed selection invalidates late results',async()=>{
  let release;const h=harness(url=>url==='/api/landscape-annual-reports'?Promise.resolve({job_id:'job-1',annual_report_id:'annual-1'}):url.startsWith('/api/jobs/')?Promise.resolve({status:'completed'}):new Promise(resolve=>release=resolve));
  h.t.handleChange({target:{dataset:{alControl:'transitions'},checked:true}});const task=h.t.generate();await tick();
  assert.match(h.root.html,/<fieldset class="al-selection-controls" disabled>/);
  h.t.handleChange({target:{dataset:{alControl:'papers_per_period'},value:'20'}});assert.equal(h.t.state.selection.papers_per_period,6);
  h.t.state.selection.selection_method='recent';release(report({include_transitions:true}));await task;assert.equal(h.t.state.report,null);
});


test('annual input-only count typing updates the request without replacing the focused input',async()=>{
  const h=harness(async url=>url==='/api/landscape-annual-reports'?{job_id:'job-1',annual_report_id:'annual-1'}:url.startsWith('/api/jobs/')?{status:'completed'}:report({include_transitions:true,selection:{papers_per_period:10,selection_method:'centroid',abstract_only:false}}));
  h.t.handleChange({target:{dataset:{alControl:'transitions'},checked:true}});h.t.state.report=report();
  const before=h.root.renders;
  h.document.dispatch('input',{target:{dataset:{alControl:'papers_per_period'},value:'1'}});
  h.document.dispatch('input',{target:{dataset:{alControl:'papers_per_period'},value:'10'}});
  assert.equal(h.root.renders,before,'multi-digit entry keeps the number input mounted');assert.equal(h.t.state.report,null);
  await h.t.generate();const request=JSON.parse(h.calls.find(c=>c.url==='/api/landscape-annual-reports').options.body);
  assert.equal(request.papers_per_period,10);assert.equal(h.t.state.report.selection.papers_per_period,10);
});

test('annual input-only invalid count prevents submission and a later edit repairs the draft',async()=>{
  const h=harness();h.t.handleChange({target:{dataset:{alControl:'transitions'},checked:true}});
  h.document.dispatch('input',{target:{dataset:{alControl:'papers_per_period'},value:'21'}});await h.t.generate();
  assert.equal(h.calls.length,0);assert.match(h.t.state.error,/1〜20件の整数/);
  h.document.dispatch('input',{target:{dataset:{alControl:'papers_per_period'},value:'12'}});assert.equal(h.t.payload().papers_per_period,12);assert.equal(h.t.state.error,'');
});
