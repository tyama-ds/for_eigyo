const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('static/orchestration-ui.js','utf8');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const plain=value=>JSON.parse(JSON.stringify(value));
function deferred(){let resolve,reject;const promise=new Promise((r,e)=>{resolve=r;reject=e;});return {resolve,reject,promise};}
function setup(){
 const ids=['content','criteria','judge','learning','include-agent','limit','threshold','review','status','cycle','percent','progress','message','keywords','timeline','stage-progress','counts','data-note','llm-note','start','start-note','stop','resume','error','next','next-cycle','reset','checkpoint','confirm','log'];
 const dom=Object.fromEntries(ids.map(id=>['#orchestration-'+id,{value:'',checked:false,hidden:false,disabled:false,dataset:{},textContent:'',innerHTML:'',scrollHeight:100,scrollTop:0,clientHeight:100}]));
 Object.assign(dom['#orchestration-criteria'],{value:'自動運転の特許'});dom['#orchestration-judge'].checked=true;dom['#orchestration-learning'].value='none';dom['#orchestration-limit'].value='443';dom['#orchestration-threshold'].value='.8';dom['#orchestration-review'].checked=true;
 const selectors={},requests=[],handlers=new Map();
 const state={keywords:'自動運転',patents:[{id:'P1',title:'運転',label:'keep',label_source:'human',fi:'G05D1/00'}],job:{status:'idle'},settings:{provider:'local',model:'gemma'},orchestration:{status:'idle'},training:null};
 const context={state,$:s=>dom[s]||null,$$:s=>selectors[s]||[],esc,on:(id,event,fn)=>handlers.set(id+':'+event,fn),api:async(path,body)=>{requests.push({path,body});return path==='/state'?state:{job_id:'new'};},updateChrome:()=>{},toast:()=>{},setTab:()=>{},setStep:()=>{},selectionSave:Promise.resolve()};
 vm.createContext(context);vm.runInContext(source,context);
 return {context,dom,selectors,requests,handlers,read:code=>vm.runInContext(code,context)};
}

test('defaults use saved workflow and judgment criteria, with review enabled',()=>{
 const s=setup();s.context.state.training={criteria:'保存済みの基準'};
 assert.equal(s.context.orchestrationDefaults().criteria,'保存済みの基準');assert.equal(s.context.orchestrationDefaults().review,true);
 s.context.state.orchestration.options={criteria:'統括基準',review:false,judge:false,max_items:100,learning:'lightweight'};
 const d=s.context.orchestrationDefaults();assert.equal(d.criteria,'統括基準');assert.equal(d.review,false);assert.equal(d.judge,false);assert.equal(d.max_items,100);
});
test('input validates numeric boundaries and offline cycle can skip LLM',()=>{
 const c=setup().context,valid={criteria:'',judge:false,learning:'none',max_items:5000,threshold:.8,review:true};
 assert.equal(c.orchestrationOptions(valid).judge,false);assert.equal(c.orchestrationOptions(valid).review,true);
 for(const max_items of ['',0,5001,2.5,'bad'])assert.throws(()=>c.orchestrationOptions({...valid,max_items}),/1〜5000/);
 for(const threshold of ['',.49,1.1,Infinity,'bad'])assert.throws(()=>c.orchestrationOptions({...valid,threshold}),/0.5〜1/);
 assert.throws(()=>c.orchestrationOptions({...valid,judge:true}),/判定の基準/);assert.throws(()=>c.orchestrationOptions({...valid,learning:'llm'}),/学習方式/);
});
test('global counts keep human, AI, deferred and unprocessed categories distinct',()=>{
 const c=setup().context,counts=c.orchestrationCounts([{label:'keep',label_source:'human',ipc:'H01M'},{label:'exclude',label_source:'agent',fi:'B01D'},{label:null,label_source:'agent',label_reason:'情報不足',agent_confidence:.6,llm_decision:'unsure',fterm:'5H029AM11'},{label:null}]);
 assert.deepEqual(plain(counts),{total:4,keep:1,exclude:1,deferred:1,pending:1,human:1,ipc:1,fi:1,fterm:1});
});
test('interrupted progress is never presented as successful 100 percent',()=>{
 const c=setup().context;
 for(const status of ['running','paused','error','review'])assert.ok(c.orchestrationPercent(100,status)<100);
 assert.equal(c.orchestrationPercent(22,'waiting_csv'),100);assert.equal(c.orchestrationPercent('x','running'),0);assert.equal(c.orchestrationPercent(-5,'running'),0);
});
test('candidate cards distinguish derived IPC and escape untrusted text',()=>{
 const c=setup().context,html=c.orchestrationCheckpointHtml({type:'classifications',selected_keys:['IPC:H01M'],candidates:[{kind:'IPC',code:'H01M',title:'<img src=x onerror=1>',reason:'<script>x</script>',evidence_status:'derived_candidate'},{kind:'IPC',code:'A',selectable:false}]});
 assert.match(html,/公報付与IPCとは別/);assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));assert.match(html,/&lt;img/);assert.match(html,/data-orchestration-key="IPC:H01M" checked/);assert.match(html,/data-orchestration-key="IPC:A"  disabled/);
});
test('checkpoint displays initially selected candidates first without mutating saved order',()=>{
 const s=setup(),candidates=[{kind:'IPC',code:'H01M',title:'電池'},{kind:'IPC',code:'G05D',title:'車両制御',title_en:'Vehicle control'},{kind:'IPC',code:'B60W',title:'制御'}],keys=candidates.map(c=>c.code);
 const html=s.context.orchestrationCheckpointHtml({type:'classifications',selected_keys:['IPC:G05D'],candidates});
 assert.ok(html.indexOf('data-orchestration-key="IPC:G05D"')<html.indexOf('data-orchestration-key="IPC:H01M"'));assert.deepEqual(candidates.map(c=>c.code),keys);assert.match(html,/Vehicle control/);assert.match(html,/表示 3 \/ 3 件 · 選択 1 件/);
});
test('candidate filtering keeps hidden selections in the review payload and restores all rows',()=>{
 const s=setup();s.dom['#orchestration-candidate-search']={value:'Ｇ０５Ｄ 1/00'};s.dom['#orchestration-candidate-count']={textContent:''};s.dom['#orchestration-candidate-empty']={hidden:true};
 const rows=[{dataset:{orchestrationCandidateKey:'IPC:H01M',orchestrationSearch:'IPC H01M 電池 Battery'},hidden:false},{dataset:{orchestrationCandidateKey:'IPC:G05D1/00',orchestrationSearch:'IPC G05D1/00 車両 Vehicle control'},hidden:false}];
 const checked=[{dataset:{orchestrationKey:'IPC:H01M'},checked:true,disabled:false},{dataset:{orchestrationKey:'IPC:G05D1/00'},checked:false,disabled:false}];s.selectors['[data-orchestration-search]']=rows;s.selectors['[data-orchestration-key]']=checked;
 s.context.filterOrchestrationCandidates();assert.equal(rows[0].hidden,true);assert.equal(rows[1].hidden,false);assert.equal(checked[0].checked,true);assert.match(s.dom['#orchestration-candidate-count'].textContent,/表示 1 \/ 2 件 · 選択 1 件（絞り込みで非表示 1 件）/);assert.deepEqual(plain(s.context.orchestrationReviewPayload('classifications')),{selected_keys:['IPC:H01M']});
 s.dom['#orchestration-candidate-search'].value='no match';s.context.filterOrchestrationCandidates();assert.equal(s.dom['#orchestration-candidate-empty'].hidden,false);assert.ok(rows.every(row=>row.hidden));
 s.dom['#orchestration-candidate-search'].value='';s.context.filterOrchestrationCandidates();assert.ok(rows.every(row=>!row.hidden));assert.equal(s.dom['#orchestration-candidate-empty'].hidden,true);assert.equal(checked[0].checked,true);
});
test('progress polls preserve checkpoint search input and hidden filter results',()=>{
 const s=setup();s.context.state.orchestration={id:'search',status:'review',stage:'classifications',checkpoint:{type:'classifications',candidates:[]}};s.context.renderOrchestrationProgress();
 s.dom['#orchestration-candidate-search']={value:'control'};s.dom['#orchestration-checkpoint'].innerHTML='filtered review with selections';s.context.renderOrchestrationProgress();
 assert.equal(s.dom['#orchestration-candidate-search'].value,'control');assert.equal(s.dom['#orchestration-checkpoint'].innerHTML,'filtered review with selections');
});
test('workflow hidden state wins over button and candidate display declarations',()=>{
 const css=fs.readFileSync('static/orchestration-ui.css','utf8');assert.match(css,/#orchestration-content\s+\[hidden\]\s*\{\s*display\s*:\s*none\s*!important/);
});
test('query review sends an intentional empty selection and only checked terms',()=>{
 const s=setup();s.selectors['[data-orchestration-key]']=[{checked:false,dataset:{orchestrationKey:'IPC:H01M'}},{checked:true,disabled:true,dataset:{orchestrationKey:'IPC:A'}}];
 s.selectors['[data-orchestration-term="include"]']=[{checked:true,value:'motor'},{checked:false,value:'battery'}];s.selectors['[data-orchestration-term="exclude"]']=[{checked:false,value:'factory'}];
 assert.deepEqual(plain(s.context.orchestrationReviewPayload('query')),{selected_keys:[],include_terms:['motor'],exclude_terms:[]});
});
test('polling preserves inputs and checkpoint DOM selections for the same review',()=>{
 const s=setup();s.context.state.orchestration={id:'one',cycle:1,status:'review',stage:'classifications',progress:20,checkpoint:{type:'classifications',candidates:[],selected_keys:[]}};
 s.context.renderOrchestrationProgress();s.dom['#orchestration-checkpoint'].innerHTML='user selection preserved';s.dom['#orchestration-criteria'].value='編集中の基準';
 s.context.state.orchestration.message='new progress';s.context.renderOrchestrationProgress();
 assert.equal(s.dom['#orchestration-checkpoint'].innerHTML,'user selection preserved');assert.equal(s.dom['#orchestration-criteria'].value,'編集中の基準');assert.equal(s.dom['#orchestration-message'].textContent,'new progress');
 assert.equal(s.dom['#orchestration-criteria'].disabled,true);assert.equal(s.dom['#orchestration-reset'].hidden,false);
 s.context.state.orchestration.checkpoint={type:'query',candidates:[],selected_keys:[],term_options:{include:['motor'],exclude:[]}};s.context.renderOrchestrationProgress();assert.match(s.dom['#orchestration-checkpoint'].innerHTML,/次の検索式を確認/);
});
test('current job counts cannot be borrowed from previous LLM training',()=>{
 const s=setup();s.context.state.orchestration={id:'o',status:'running',stage:'assess',progress:30};s.context.state.job={id:'job-new',kind:'orchestration',status:'running'};
 s.context.state.training={job_id:'job-old',mode:'llm',completed_count:88,total_count:100};s.context.renderOrchestrationProgress();assert.doesNotMatch(s.dom['#orchestration-stage-progress'].innerHTML,/88/);
 s.context.state.training.job_id='job-new';s.context.renderOrchestrationProgress();assert.match(s.dom['#orchestration-stage-progress'].innerHTML,/88 \/ 100/);assert.match(s.dom['#orchestration-stage-progress'].innerHTML,/progress/);
});
test('review, paused, waiting CSV actions are offered only in their states',()=>{
 const s=setup();for(const status of ['idle','running','review','paused','error','waiting_csv']){s.context.state.orchestration={status};s.context.renderOrchestrationProgress();assert.equal(s.dom['#orchestration-resume'].hidden,!['paused','error'].includes(status),status);assert.equal(s.dom['#orchestration-next'].hidden,status!=='waiting_csv',status);assert.equal(s.dom['#orchestration-start'].disabled,status!=='idle',status);}
});
test('duplicate actions are prevented and failure releases the action guard',async()=>{
 const s=setup(),request=deferred();s.context.api=async(path,body)=>{s.requests.push({path,body});return request.promise;};
 const first=s.context.orchestrationAction('resume');await s.context.orchestrationAction('resume');assert.equal(s.requests.length,1);request.reject(Error('network failed'));await assert.rejects(first,/network failed/);assert.equal(s.read('orchestrationStarting'),false);
});
test('start waits for classification selection persistence before sending a workflow request',async()=>{
 const s=setup(),saved=deferred();s.context.selectionSave=saved.promise;
 const pending=s.context.startOrchestration();assert.equal(s.requests.length,0);saved.resolve();await pending;
 assert.equal(s.requests[0].path,'/orchestration/start');assert.equal(s.requests[0].body.max_items,443);assert.equal(s.requests[0].body.review,true);assert.equal(s.requests.at(-1).path,'/state');
});
test('CSV merge mode and FI column stay independent from IPC',()=>{
 const app=fs.readFileSync('static/app.js','utf8'),classification=fs.readFileSync('static/classification-ui.js','utf8');
 assert.match(app,/fi:'FI'/);assert.match(app,/\/upload\?merge=/);assert.match(app,/pendingImportMode===\'merge\'/);assert.match(classification,/IPC未取込/);assert.match(classification,/FI：\$\{esc\(p.fi\)\}/);assert.match(classification,/Fターム：\$\{esc\(p.fterm\)\}/);
});
test('training failures allow mode recovery without changing judgment options',()=>{
 const s=setup();s.context.state.orchestration={status:'error',stage:'train'};s.context.renderOrchestrationProgress();
 assert.equal(s.dom['#orchestration-learning'].disabled,false);assert.equal(s.dom['#orchestration-criteria'].disabled,true);assert.equal(s.dom['#orchestration-review'].disabled,true);
 s.dom['#orchestration-learning'].value='lightweight';s.dom['#orchestration-include-agent'].checked=true;
 assert.deepEqual(plain(s.context.orchestrationResumePayload()),{learning:'lightweight',include_agent:true});
 s.context.state.orchestration.stage='assess';assert.deepEqual(plain(s.context.orchestrationResumePayload()),{});
});
test('classification review remains the current stage after discovery completes',()=>{
 const s=setup();s.context.state.orchestration={status:'review',stage:'classifications',steps:[{id:'discover',status:'done'}]};s.context.renderOrchestrationProgress();
 assert.match(s.dom['#orchestration-timeline'].innerHTML,/<li class="current" aria-current="step">/);
});
test('review identifies selections added in another tab without repainting checkboxes',()=>{
 const s=setup();s.dom['#orchestration-additional-selection']={textContent:''};s.context.state.selected=['IPC:H01M','IPC:B01D'];s.context.state.orchestration={id:'review',status:'review',stage:'classifications',checkpoint:{type:'classifications',selection_baseline:['IPC:H01M'],selected_keys:['IPC:H01M'],candidates:[]}};
 s.context.renderOrchestrationProgress();assert.match(s.dom['#orchestration-additional-selection'].textContent,/追加された分類：IPC:B01D/);
 s.dom['#orchestration-checkpoint'].innerHTML='review selection';s.context.state.selected.push('IPC:G05D');s.context.renderOrchestrationProgress();assert.match(s.dom['#orchestration-additional-selection'].textContent,/IPC:G05D/);assert.equal(s.dom['#orchestration-checkpoint'].innerHTML,'review selection');
});
test('legacy agent deferred judgments are counted without declaring them fresh',()=>{
 const c=setup().context;const result=c.orchestrationCounts([{label:null,label_source:'agent',label_reason:'保留',agent_confidence:.6}]);assert.equal(result.deferred,1);assert.equal(result.pending,0);
});
test('cleared AI judgments and invalid saved confidence remain unprocessed even with an old decision',()=>{
 const c=setup().context,old={label:null,llm_decision:'keep',label_source:'agent',label_reason:'関連あり',agent_confidence:.9};
 const invalid=[{...old,label_source:null,label_reason:''},{...old,label_source:'human'},{...old,label_reason:'  '},...[null,true,'0.8',NaN,Infinity,-.1,1.1].map(agent_confidence=>({...old,agent_confidence}))];
 const result=c.orchestrationCounts(invalid);assert.equal(result.deferred,0);assert.equal(result.pending,invalid.length);
 const boundaries=c.orchestrationCounts([{...old,agent_confidence:0},{...old,agent_confidence:1},{label:'keep'},{label:'exclude'}]);assert.equal(boundaries.deferred,2);assert.equal(boundaries.keep,1);assert.equal(boundaries.exclude,1);assert.equal(boundaries.pending,0);
});
test('discovery separates derived IPC candidates from assigned IPC and search hypotheses',()=>{
 const s=setup();s.context.state.discovery={plan:{facets:[]}};vm.runInContext(fs.readFileSync('static/discovery-ui.js','utf8'),s.context);
 const item={key:'IPC:G05D',kind:'IPC',code:'G05D',title:'制御',evidence_status:'derived_candidate',support_count:7,family_count:7,positive_count:2,ai_positive_count:3,negative_count:1,classification_origins:[{type:'fi_to_ipc',raw:'G05D1/00 <script>',warning:'候補です',source_version:'2026',ipc_version:'2026.01'}],evidence:[]};
 assert.equal(s.context.discoveryEvidenceType(item),'derived');assert.equal(s.context.discoveryHasEvidence(item),true);assert.match(s.context.discoveryEvidenceLabel(item),/付与IPCとは別/);
 const html=s.context.discoveryCandidateHtml(item);assert.match(html,/AIの必要 3/);assert.match(html,/対応元を確認/);assert.ok(!html.includes('<script>'));assert.match(html,/&lt;script&gt;/);
 s.read("discoveryCompare.source='derived'");assert.equal(s.context.discoveryFilteredItems([item,{...item,key:'IPC:A',evidence_status:'hypothesis'}]).length,1);
});
test('patent preview keeps original FI alongside missing IPC and F-term',()=>{
 const inspector=require('../static/patent-inspector.js');const html=inspector.previewHtml({title:'車両制御',fi:'G05D1/00, B',fterm:'5H029AM11'});
 assert.match(html,/<dt>IPC<\/dt><dd>未取込/);assert.match(html,/<dt>FI<\/dt><dd>G05D1\/00, B/);assert.match(html,/<dt>Fターム<\/dt><dd>5H029AM11/);
});
