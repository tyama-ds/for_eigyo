const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/learning-ui.js', 'utf8');
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => {resolve=yes; reject=no;}); return {promise,resolve,reject}; }
function setup(apiHandler = async () => ({job_id:'job-new'})) {
  const ids = ['training-mode','train-button','include-agent','include-agent-control','llm-training-options','llm-training-criteria','llm-training-used-criteria','llm-training-limit','llm-training-threshold','patent-search','label-filter','learning-mode-note','learning-progress','map-training-progress','learning-progress-percent','learning-progress-status','learning-progress-message','learning-progress-count','learning-progress-error','map-training-stop','next-refine'];
  const dom = Object.fromEntries(ids.map(id => ['#'+id,{value:'',checked:false,disabled:false,hidden:false,dataset:{},textContent:'',innerHTML:''}]));
  Object.assign(dom['#training-mode'],{value:'llm'}); dom['#llm-training-limit'].value='25'; dom['#llm-training-threshold'].value='.85'; dom['#label-filter'].value='all';
  const requests = [], notices = [], handlers = new Map(), judgmentButtons=[{disabled:false},{disabled:false}];
  const context = {state:{job:{status:'idle'},training:null,keywords:'元の探索語',settings:{provider:'local',model:'test-model'}}, esc,
    $:selector=>dom[selector]||null,$$:selector=>selector==='.row-label,.map-selection-actions button'?judgmentButtons:[],
    on:(id,event,handler)=>handlers.set(`${id}:${event}`,handler),
    api:(path,body)=>{requests.push({path,body});return apiHandler(path,body);},
    refreshJob:async()=>{}, toast:(...args)=>notices.push(args),
  };
  vm.createContext(context);vm.runInContext(source,context);
  return {context,dom,requests,notices,handlers,judgmentButtons,read:expression=>vm.runInContext(expression,context)};
}

test('progress clamps invalid values and marks successful completion as 100 percent',()=>{
  const {context:c}=setup();
  assert.equal(c.learningProgressState({id:'j',kind:'training',status:'running',progress:-30},null).percent,0);
  assert.equal(c.learningProgressState({id:'j',kind:'training',status:'running',progress:33.7},null).percent,34);
  assert.ok(c.learningProgressState({id:'j',kind:'training',status:'running',progress:400},null).percent<=100);
  assert.equal(c.learningProgressState({id:'j',kind:'training',status:'running',progress:'invalid'},null).percent,0);
  assert.equal(c.learningProgressState({id:'j',kind:'training',status:'done',progress:91},null).percent,100);
  assert.equal(c.learningProgressState({status:'idle'},{mode:'llm',status:'completed'}).percent,100);
});

test('cancelled and failed jobs never display a successful 100 percent',()=>{
  const {context:c}=setup();
  for(const status of ['cancelled','error']) {
    const p=c.learningProgressState({id:'j',kind:'training',status,progress:100,error:status==='error'?'connection failed':null},null);
    assert.ok(p.percent<100,`${status} at a reported 100 must remain visibly incomplete`);
    assert.equal(p.status,status);assert.equal(p.running,false);
  }
});

test('saved LLM counts are shown only for the matching running job',()=>{
  const {context:c}=setup(), stored={mode:'llm',job_id:'old',status:'completed',completed_count:80,total_count:80,judged_count:60,deferred_count:20};
  const fresh={id:'new',kind:'training',mode:'llm',status:'running',progress:0};
  assert.equal(c.learningProgressState(fresh,stored).count,'');
  assert.match(c.learningProgressState({...fresh,id:'old'},stored).count,/80 \/ 80/);
  assert.equal(c.learningProgressState({...fresh,mode:'transformer'},stored).count,'');
  assert.equal(c.learningProgressState({...fresh,kind:'agent',mode:undefined},stored).count,'');
});

test('opening a running training job shows its actual mode and matching LLM criteria',()=>{
  const s=setup();s.dom['#training-mode'].value='lightweight';
  s.context.state.job={id:'active',kind:'training',mode:'llm',status:'running',progress:40};
  s.context.state.training={job_id:'active',mode:'llm',criteria:'実行中の判断基準',max_items:443,threshold:.9};
  s.context.renderLearningMode();
  assert.equal(s.dom['#training-mode'].value,'llm');
  assert.equal(s.dom['#llm-training-options'].hidden,false);
  assert.equal(s.dom['#llm-training-criteria'].value,'実行中の判断基準');
  assert.equal(s.dom['#llm-training-limit'].value,'443');
  assert.equal(s.dom['#llm-training-threshold'].value,'0.9');
  for(const id of ['training-mode','llm-training-criteria','llm-training-limit','llm-training-threshold'])assert.equal(s.dom['#'+id].disabled,true);
  s.context.state.training={job_id:'older',mode:'llm',criteria:'古い基準',max_items:100,threshold:.8};
  s.context.renderLearningMode();
  assert.equal(s.dom['#llm-training-criteria'].value,'実行中の判断基準');
  assert.equal(s.dom['#llm-training-limit'].value,'443');
});

test('idle mode rendering leaves current draft inputs and saved draft unchanged',()=>{
  const s=setup();s.dom['#training-mode'].value='transformer';
  s.dom['#llm-training-criteria'].value='編集中の基準';s.dom['#llm-training-limit'].value='27';s.dom['#llm-training-threshold'].value='0.95';
  s.context.captureLearningDraft();const draft=s.read('JSON.stringify(learningDraft)');
  s.context.state.job={id:'older',kind:'training',mode:'llm',status:'idle'};
  s.context.state.training={job_id:'older',mode:'llm',criteria:'保存された古い基準',max_items:443,threshold:.8};
  s.context.renderLearningMode();
  assert.equal(s.dom['#training-mode'].value,'transformer');
  assert.equal(s.dom['#llm-training-criteria'].value,'編集中の基準');
  assert.equal(s.dom['#llm-training-limit'].value,'27');
  assert.equal(s.dom['#llm-training-threshold'].value,'0.95');
  assert.equal(s.read('JSON.stringify(learningDraft)'),draft);
  assert.equal(s.dom['#training-mode'].disabled,false);
});

test('automatic criteria stay blank across polling and the next run uses fresh server context',async()=>{
  for(const source of ['adopted_query','keywords']){
    const s=setup();
    s.context.state.job={id:'active',kind:'training',mode:'llm',status:'running',progress:40};
    s.context.state.training={job_id:'active',mode:'llm',criteria:'以前の自動基準 <b>本文</b>',criteria_source:source,max_items:443,threshold:.9};
    s.context.renderLearningMode();s.context.captureLearningDraft();
    assert.equal(s.dom['#llm-training-criteria'].value,'');
    assert.equal(s.read('learningDraft.criteria'),'');
    assert.equal(s.dom['#llm-training-used-criteria'].hidden,false);
    assert.match(s.dom['#llm-training-used-criteria'].textContent,/以前の自動基準 <b>本文<\/b>/);
    assert.equal(s.dom['#llm-training-used-criteria'].innerHTML,'');
    s.context.state.job={id:'active',kind:'training',mode:'llm',status:'done'};
    s.context.renderLearningMode();
    assert.match(s.dom['#llm-training-used-criteria'].textContent,/直近の判定/);
    await s.context.startMapTraining();
    assert.equal(s.requests[0].body.criteria,'');
  }
});

test('explicit criteria remain editable and an older job cannot masquerade as current criteria',()=>{
  const s=setup();
  s.context.state.job={id:'active',kind:'training',mode:'llm',status:'running'};
  s.context.state.training={job_id:'active',mode:'llm',criteria:'利用者の明示基準',criteria_source:'explicit',max_items:25,threshold:.85};
  s.context.renderLearningMode();
  assert.equal(s.dom['#llm-training-criteria'].value,'利用者の明示基準');
  assert.match(s.dom['#llm-training-used-criteria'].textContent,/入力した基準/);
  s.context.state.job.id='next-job';s.context.renderLearningMode();
  assert.equal(s.dom['#llm-training-used-criteria'].hidden,true);
  assert.equal(s.dom['#llm-training-used-criteria'].textContent,'');
  assert.match(s.context.learningControlsHtml(),/空欄なら採用した式の目的・観点/);
});

test('progress polls keep all in-progress draft fields and search/filter values',()=>{
  const s=setup();
  s.dom['#llm-training-criteria'].value='編集途中の基準';s.dom['#llm-training-limit'].value='17';s.dom['#llm-training-threshold'].value='0.95';
  s.dom['#patent-search'].value='JP edited';s.dom['#label-filter'].value='unlabeled';s.dom['#include-agent'].checked=true;
  s.context.captureLearningDraft();const before=s.read('JSON.stringify(learningDraft)');
  s.context.state.job={id:'j',kind:'training',mode:'llm',status:'running',progress:47};s.context.renderLearningProgress();
  assert.equal(s.read('JSON.stringify(learningDraft)'),before);
  assert.equal(s.dom['#llm-training-criteria'].value,'編集途中の基準');assert.equal(s.dom['#llm-training-limit'].value,'17');
  assert.equal(s.dom['#patent-search'].value,'JP edited');assert.equal(s.dom['#label-filter'].value,'unlabeled');
  assert.equal(s.dom['#train-button'].disabled,true);assert.equal(s.dom['#next-refine'].disabled,true);
  assert.ok(s.judgmentButtons.every(button=>button.disabled));
  delete s.dom['#training-mode'];s.context.captureLearningDraft();assert.equal(s.read('JSON.stringify(learningDraft)'),before);
});

test('LLM limits and confidence are validated before any request is sent',async()=>{
  for(const value of ['', ' ', '0','5001','1.5','NaN','Infinity','<script>']) {
    const s=setup();s.dom['#llm-training-limit'].value=value;
    await assert.rejects(s.context.startMapTraining(),/判定上限/);assert.equal(s.requests.length,0);assert.equal(s.read('learningStarting'),false);
  }
  for(const value of ['', ' ', '-1','.49','1.01','NaN','Infinity']) {
    const s=setup();s.dom['#llm-training-threshold'].value=value;
    await assert.rejects(s.context.startMapTraining(),/信頼度/);assert.equal(s.requests.length,0);assert.equal(s.read('learningStarting'),false);
  }
});

test('LLM start sends exact criteria and numeric limits without changing the search keywords',async()=>{
  const s=setup();s.dom['#llm-training-criteria'].value='界面を含む\n充電設備を除く';s.dom['#include-agent'].checked=true;
  await s.context.startMapTraining();
  assert.equal(s.requests.length,1);assert.equal(s.requests[0].path,'/train');
  assert.deepEqual(JSON.parse(JSON.stringify(s.requests[0].body)),{mode:'llm',include_agent:true,criteria:'界面を含む\n充電設備を除く',max_items:25,threshold:.85});
  assert.equal(s.context.state.keywords,'元の探索語');assert.equal(s.context.state.job.id,'job-new');
});

test('blank LLM criteria remain blank for the server keyword fallback and boundaries are allowed',async()=>{
  const s=setup();s.dom['#llm-training-criteria'].value='';s.dom['#llm-training-limit'].value='100';s.dom['#llm-training-threshold'].value='1';
  await s.context.startMapTraining();assert.equal(s.requests[0].body.criteria,'');assert.equal(s.requests[0].body.max_items,100);assert.equal(s.requests[0].body.threshold,1);
});

test('full CSV judgment limits are sent numerically and the initial limit remains 100',async()=>{
  for(const count of [443,5000]) {
    const s=setup();s.dom['#llm-training-limit'].value=String(count);
    assert.equal(s.read('learningDraft.max_items'),100);
    assert.match(s.context.learningControlsHtml(),/id="llm-training-limit"[^>]*max="5000"/);
    await s.context.startMapTraining();
    assert.equal(s.requests.length,1);
    assert.equal(s.requests[0].body.max_items,count);
  }
});

test('two rapid starts send one request and existing running jobs prevent a start',async()=>{
  const pending=deferred(),s=setup(()=>pending.promise);const first=s.context.startMapTraining();
  assert.equal(s.read('learningStarting'),true);assert.equal(s.dom['#train-button'].disabled,true);
  await s.context.startMapTraining();assert.equal(s.requests.length,1);
  pending.resolve({job_id:'new'});await first;assert.equal(s.read('learningStarting'),false);
  await s.context.startMapTraining();assert.equal(s.requests.length,1,'a running job blocks another request');
});

test('a failed start releases the guard and re-enables controls',async()=>{
  const s=setup(async()=>{throw Error('offline');});
  await assert.rejects(s.context.startMapTraining(),/offline/);assert.equal(s.read('learningStarting'),false);assert.equal(s.dom['#train-button'].disabled,false);
});

test('non-LLM training excludes stale LLM options and restored criteria are escaped',async()=>{
  const s=setup();s.dom['#training-mode'].value='transformer';s.dom['#llm-training-limit'].value='invalid';s.dom['#llm-training-criteria'].value='</textarea><img src=x onerror="x()">';
  s.context.captureLearningDraft();const html=s.context.learningControlsHtml();assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;/textarea&gt;'));
  await s.context.startMapTraining();assert.deepEqual(JSON.parse(JSON.stringify(s.requests[0].body)),{mode:'transformer',include_agent:false});
});

const appSource=fs.readFileSync('static/app.js','utf8');
test('table regeneration creates disabled judgment buttons immediately while running or starting',()=>{
  const rowSource=appSource.slice(appSource.indexOf('function renderPatentRows('),appSource.indexOf('function trainingHtml('));
  for(const status of ['running','starting','idle']){
    const s=setup();s.dom['#patent-rows']={innerHTML:''};s.context.run=fn=>fn;
    s.context.state.patents=[{id:'JP1',title:'test patent',label:null,score:null}];
    s.context.state.job.status=status==='running'?'running':'idle';
    s.read(`learningStarting=${status==='starting'}`);
    vm.runInContext(rowSource,s.context);s.context.renderPatentRows();
    const buttons=s.dom['#patent-rows'].innerHTML.match(/<button\b[^>]*\brow-label\b[^>]*>/g);
    assert.equal(buttons.length,2);
    for(const button of buttons)assert.equal(/\bdisabled\b/.test(button),status!=='idle',status);
  }
});
const pollingSource=appSource.slice(appSource.indexOf('function syncRunningLlmJudgments('),appSource.indexOf('function download('));
function pollingSetup(){
  const pending=deferred(),s=setup(()=>pending.promise),row={id:'JP1',label:null,label_source:null,score:null,x:.25,y:.4};
  const tableRow={dataset:{patentId:'JP1'},cells:Array.from({length:5},()=>({textContent:''}))};
  const originalQuery=s.context.$$;s.context.$$=selector=>selector==='#patent-rows tr[data-patent-id]'?[tableRow]:originalQuery(selector);
  s.context.state.patents=[row];s.context.state.job={id:'running-job',kind:'training',mode:'llm',status:'running'};
  s.context.state.training={job_id:'running-job',mode:'llm',completed_count:4};
  s.context.activeTab='explore';s.context.step=2;s.context.shownJob=null;
  const calls={map:0,chrome:0};
  s.context.updateMapAssessment=()=>{calls.map++;calls.keep=s.context.state.patents.filter(p=>p.label==='keep').length;};
  s.context.updateChrome=()=>calls.chrome++;s.context.renderDiscovery=()=>{};s.context.renderAgent=()=>{};
  s.context.trainingHtml=()=>'';
  vm.runInContext(pollingSource,s.context);
  function response(count=8,id='running-job'){
    return {job:{id,kind:'training',mode:'llm',status:'running',progress:30},
      training:{job_id:id,mode:'llm',completed_count:count},patents:[{...row,label:'keep',label_source:'agent',score:.94}],agent_log:[]};
  }
  return {...s,pending,row,tableRow,calls,response};
}

test('saved LLM batches refresh cards and existing table cells while preserving row objects and inputs',async()=>{
  const s=pollingSetup(),cells=s.tableRow.cells;
  s.dom['#patent-search'].value='JP';s.dom['#label-filter'].value='all';
  const request=s.context.refreshJob();s.pending.resolve(s.response());await request;
  assert.equal(s.context.state.patents[0],s.row,'preview and map listeners retain the same object');
  assert.equal(s.row.label,'keep');assert.equal(s.row.score,.94);
  assert.equal(s.tableRow.cells,cells);assert.equal(cells[2].textContent,94);assert.equal(cells[3].textContent,'必要 · AI');
  assert.equal(s.calls.map,1);assert.equal(s.calls.keep,1);
  assert.equal(s.row.x,.25);assert.equal(s.row.y,.4);
  assert.equal(s.dom['#patent-search'].value,'JP');assert.equal(s.dom['#label-filter'].value,'all');
  assert.ok(s.judgmentButtons.every(button=>button.disabled));
});

test('unchanged batches do not replace patent judgments and regressing progress responses are ignored',async()=>{
  for(const count of [4,0]){
    const s=pollingSetup(),request=s.context.refreshJob();s.pending.resolve(s.response(count));await request;
    assert.equal(s.row.label,null);assert.equal(s.calls.map,0);assert.equal(s.context.state.training.completed_count,4);
    assert.equal(s.tableRow.cells[3].textContent,'');
  }
});

test('responses for another job or an in-flight job superseded locally cannot overwrite current judgments',async()=>{
  for(const superseded of [false,true]){
    const s=pollingSetup(),request=s.context.refreshJob();
    if(superseded)s.context.state.job={id:'newer-job',kind:'training',mode:'llm',status:'running'};
    s.pending.resolve(s.response(8,superseded?'running-job':'older-job'));await request;
    assert.equal(s.row.label,null);assert.equal(s.calls.map,0);
    assert.equal(s.context.state.job.id,superseded?'newer-job':'running-job');
    assert.equal(s.context.state.training.completed_count,4);
  }
});
