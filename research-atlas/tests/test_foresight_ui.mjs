import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/foresight.js',import.meta.url),'utf8');
function harness(fetchImpl){
  const elements=new Map(),events=new Map(),documentEvents=new Map();
  const document={querySelector:selector=>elements.get(selector)||null,addEventListener(name,handler){documentEvents.set(name,handler);},body:{appendChild(node){elements.set(`#${node.id}`,node);}}};
  const window={__ATLAS_UI_TEST__:true,addEventListener(name,handler){events.set(name,handler);}};
  const sandbox=vm.createContext({window,document,location:{origin:'http://127.0.0.1:8000'},URL,console,setTimeout,clearTimeout,fetch:fetchImpl||(()=>{throw new Error('unexpected fetch');})});
  vm.runInContext(source,sandbox);
  return {t:window.__foresightTest,window,elements,events,documentEvents,sandbox};
}
const candidate=(id,extra={})=>({id,label:`Field ${id}`,keywords:['material'],growth:{available:true,growth_pct:25,series:[{year:2024,count:4},{year:2025,count:5}],reason:'固定した資料内の比較',forecast_available:false},readiness:{evidence_coverage_score:50,stage:'unassessed',eligible_papers:2,coverage_pct:100,dimensions:[{id:'performance',label:'性能・効果',status:'mentioned',paper_count:1,evidence_ids:['fact-1']}],notes:[]},evidence:[{id:'fact-1',paper_id:'p1',dimension:'performance',text:'The sample improved performance.',stance:'support'}],recommendations:[{paper_id:'p1',score:54,relevance:.54,feedback_relevance:'unjudged'}],commentary:{summary:'Measured candidate',support:['言及を確認'],counter:['反証は未確認'],limitations:[],next_steps:['原文を確認']},...extra});
const result={id:'result-a',meta:{years:[2024,2025],is_demo:false,sampled:false},papers:[{id:'p1',title:'Paper one',abstract:'The sample improved performance.',year:2025,authors:[]}]};
const assessment=(id='assessment-a',extra={})=>({id,result_id:'result-a',meta:{...result.meta},papers:result.papers,candidates:[candidate('topic-a')],rounds:[],warnings:[],...extra});
const response=data=>({ok:true,json:async()=>data});

test('new browser connection settings invalidate a pending status response',async()=>{
  const pending=[];const {t,events}=harness(async()=>new Promise(resolve=>pending.push(resolve)));
  const previous=t.loadStatus();assert.equal(pending.length,1);
  events.get('atlas:connections-changed')({detail:{revision:1}});assert.equal(pending.length,2);assert.equal(t.ui.status,null);
  pending[1](response({llm:{openai:{configured:false},local:{models:[]}},providers:[]}));await new Promise(resolve=>setImmediate(resolve));
  pending[0](response({llm:{openai:{configured:true},local:{models:[]}},providers:[]}));await previous;
  assert.equal(t.ui.status.llm.openai.configured,false);assert.equal(t.ui.statusPending,false);
});

test('growth unknown and unavailable evidence are never plotted as zero',()=>{
  const {t}=harness();t.ui.result=result;
  const unknown=candidate('unknown',{growth:{available:false,growth_pct:null},readiness:{evidence_coverage_score:null}});
  assert.equal(t.hasGrowth(unknown),false);
  assert.equal(t.hasGrowth(candidate('no-denominator',{growth:{available:true,growth_pct:null}})),false);
  const html=t.comparisonChart([unknown]);
  assert.match(html,/成長は未判定/);assert.match(html,/0%として配置しません/);assert.doesNotMatch(html,/class="fs-candidate-point/);
  assert.match(t.candidateDetail(unknown),/実用化の評価/);assert.match(t.candidateDetail(unknown),/未評価/);
});

test('fact ids resolve to canonical papers; feedback does not send support or counter',()=>{
  const {t}=harness();t.ui.result=result;t.applyAssessment(assessment());
  assert.deepEqual(Array.from(t.resolveEvidenceIds(['fact-1','p1'])),['p1']);
  t.ui.status={providers:[{id:'europepmc',available:true}],embeddings:[{id:'tfidf',available:true}]};
  t.handleChange({target:{dataset:{fsFeedback:'p1',fsFeedbackCandidate:'topic-a'},value:'relevant'}});
  const payload=t.explorePayload(t.ui.assessment.candidates[0]);
  assert.equal(payload.mode,'local');assert.equal(payload.max_rounds,1);
  assert.deepEqual(JSON.parse(JSON.stringify(payload.feedback)),[{paper_id:'p1',relevance:'relevant'}]);
  assert.equal('stance' in payload.feedback[0],false);
  t.handleChange({target:{dataset:{fsFeedback:'p1',fsFeedbackCandidate:'topic-a'},value:'unknown'}});
  assert.equal(t.explorePayload(t.ui.assessment.candidates[0]).feedback[0].relevance,'unknown');
  assert.equal(t.normalizeRelevance('counter'),'unknown');assert.equal(t.normalizeRelevance('unjudged'),'unknown');
});

test('external sources, generated data, bounds, and unavailable SBERT are validated',()=>{
  const {t}=harness();t.ui.result=result;t.applyAssessment(assessment());t.ui.status={providers:[{id:'scopus',available:false}],embeddings:[{id:'tfidf',available:true},{id:'sbert',available:false}]};
  const c=t.ui.assessment.candidates[0];t.ui.mode='external';t.ui.provider='scopus';assert.throws(()=>t.explorePayload(c),/利用可能/);
  t.ui.provider='europepmc';t.ui.status.providers=[{id:'europepmc',available:true}];t.ui.assessment.meta.is_demo=true;assert.throws(()=>t.explorePayload(c),/合成・テスト/);
  t.ui.mode='local';t.ui.rounds=4;assert.throws(()=>t.explorePayload(c),/1〜3/);
  t.ui.rounds=1;t.ui.limit=101;assert.throws(()=>t.explorePayload(c),/20〜100/);
  t.ui.limit=40;t.ui.embedding='sbert';assert.throws(()=>t.explorePayload(c),/SBERT/);
});

test('paper titles, evidence, model prose and links are escaped',()=>{
  const {t}=harness();t.ui.result=result;
  const bad=candidate('topic-a',{label:'<img src=x onerror=alert(1)>',evidence:[{id:'fact-1',paper_id:'p1',text:'<script>bad</script>'}],narrative:{headline:'<iframe src=x>',sections:[{title:'<b>x</b>',text:'<script>claim</script>',evidence_ids:['p1']}],caveats:[]}});
  t.applyAssessment(assessment('a',{candidates:[bad]}));const html=t.candidateDetail(bad);
  assert.doesNotMatch(html,/<(?:img|script|iframe)[\s>]/);assert.match(html,/&lt;img/);assert.match(html,/&lt;script/);
  assert.equal(t.safeURL('javascript:alert(1)'),'');assert.equal(t.safeURL('https://user:pass@example.com'),'');assert.equal(t.safeURL('//example.com'),'');assert.equal(t.safeURL('https://doi.org/10.1/a'),'https://doi.org/10.1/a');
});

test('initial assessment creation sends no external search or LLM request',async()=>{
  const calls=[];const {t}=harness(async(url,options)=>{calls.push({url,body:options?.body&&JSON.parse(options.body)});if(url==='/api/assessments')return response({job_id:'job'});if(url==='/api/jobs/job')return response({status:'completed',assessment_id:'assessment-a'});return response(assessment());});
  t.ui.result=result;await t.createAssessment();
  assert.deepEqual(calls.map(call=>call.url),['/api/assessments','/api/jobs/job','/api/assessments/assessment-a']);
  assert.deepEqual(calls[0].body,{result_id:'result-a'});assert.equal(t.ui.assessment.id,'assessment-a');
});

test('new analysis prevents a late assessment from replacing the current data',async()=>{
  let release;const calls=[];const {t}=harness(async url=>{calls.push(url);await new Promise(resolve=>{release=resolve;});return response({job_id:'late'});});
  t.ui.result=result;const pending=t.createAssessment();await new Promise(resolve=>setImmediate(resolve));
  t.reset({...result,id:'result-b'});release();await pending;
  assert.equal(t.ui.assessment,null);assert.equal(t.ui.result.id,'result-b');assert.deepEqual(calls,['/api/assessments']);assert.equal(t.ui.busy,false);
});

test('partially saved exploration is shown after a failed round',async()=>{
  const partial=assessment('partial',{rounds:[{index:1,candidate_id:'topic-a',added_papers:2}]});
  const {t}=harness(async url=>url==='/explore'?response({job_id:'job'}):url==='/api/jobs/job'?response({status:'failed',assessment_id:'partial',error:'検索先の接続エラー'}):response(partial));
  t.ui.result=result;t.applyAssessment(assessment());await t.runJob('/explore',{},'explore');
  assert.equal(t.ui.assessment.id,'partial');assert.equal(t.ui.assessment.rounds.length,1);assert.match(t.ui.error,/接続エラー/);assert.equal(t.ui.busy,false);
});

test('wrong result ownership is rejected while the original assessment remains',async()=>{
  const {t}=harness(async url=>url==='/operation'?response({job_id:'job'}):url==='/api/jobs/job'?response({status:'completed',assessment_id:'wrong'}):response(assessment('wrong',{result_id:'other'})));
  t.ui.result=result;t.applyAssessment(assessment());await t.runJob('/operation',{},'busy');assert.equal(t.ui.assessment.id,'assessment-a');assert.match(t.ui.error,/別のデータセット/);
});

test('NMF handoff chooses a candidate after assessment creation and result reset clears feedback',()=>{
  const {t,window,events}=harness();t.ui.result=result;window.AtlasForesight.selectTopic('topic-b');t.applyAssessment(assessment('a',{candidates:[candidate('topic-a'),candidate('topic-b')]}));assert.equal(t.ui.selected,'topic-b');
  t.ui.feedback.set('x',{candidate_id:'topic-b',paper_id:'p1',relevance:'relevant'});events.get('atlas:result')({detail:null});assert.equal(t.ui.assessment,null);assert.equal(t.ui.feedback.size,0);assert.equal(t.ui.result,null);
});

test('existing app navigation and analysis lifecycle dispatch the module bridge',()=>{
  const app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8'),html=readFileSync(new URL('../static/index.html',import.meta.url),'utf8');
  assert.match(html,/data-view="foresight"/);assert.match(html,/\/static\/foresight\.js/);assert.match(app,/new CustomEvent\('atlas:result',\{detail:null\}\)/);assert.match(app,/new CustomEvent\('atlas:result',\{detail:completed\}\)/);assert.match(app,/data-foresight-topic/);assert.match(app,/AtlasForesight\?\.mount/);
});

test('pseudo-positive recommendations are never restored as a user judgment',()=>{
  const {t}=harness();t.ui.result=result;
  const c=candidate('topic-a');c.recommendations[0].feedback_relevance='relevant';
  t.applyAssessment(assessment('a',{candidates:[c],feedback:[{candidate_id:'topic-a',paper_id:'p1',relevance:'relevant',source:'pseudo'}]}));
  assert.equal(t.selectedFeedback(c).length,0);
  assert.match(t.candidateDetail(c),/value="unknown" selected/);
  t.applyAssessment(assessment('b',{feedback:[{candidate_id:'topic-a',paper_id:'p1',relevance:'irrelevant',source:'user'}]}));
  assert.equal(t.selectedFeedback(c)[0].relevance,'irrelevant');
});

test('query preview is local and does not arrive on a different selected candidate',async()=>{
  let release;const calls=[];
  const {t,window}=harness(async url=>{calls.push(url);await new Promise(resolve=>{release=resolve;});return response({scopus_query:'TITLE-ABS-KEY(material)'});});
  t.ui.result=result;t.applyAssessment(assessment('a',{candidates:[candidate('topic-a'),candidate('topic-b')]}));
  const pending=t.loadQueryPreview();await new Promise(resolve=>setImmediate(resolve));window.AtlasForesight.selectTopic('topic-b');release();await pending;
  assert.equal(t.ui.queryPreview,null);assert.equal(t.ui.queryBusy,false);
  assert.deepEqual(calls,['/api/assessments/a/queries?candidate_id=topic-a']);
});

test('saved assessment restore replaces unsent relevance with saved user feedback',async()=>{
  const saved=assessment('saved',{feedback:[{candidate_id:'topic-a',paper_id:'p1',relevance:'irrelevant',source:'user'}]});
  const {t}=harness(async()=>response(saved));t.ui.result=result;t.applyAssessment(assessment());
  t.handleChange({target:{dataset:{fsFeedback:'p1',fsFeedbackCandidate:'topic-a'},value:'relevant'}});
  await t.restoreAssessment('saved');assert.equal(t.ui.assessment.id,'saved');assert.equal(t.selectedFeedback(t.ui.assessment.candidates[0])[0].relevance,'irrelevant');assert.equal(t.ui.savedOpen,false);
});


test('permalink opens a saved assessment via API and preserves its history across reloads',async()=>{
  const resultId='aeda6d53f096494b90cd3004b965c579',assessmentId='1bde12c5598146cca30274a2217e0c35';
  const original={...result,id:resultId},saved=assessment(assessmentId,{result_id:resultId,rounds:[{index:1,candidate_id:'topic-a'}]});
  const calls=[],history=[];const {t,window}=harness(async url=>{calls.push(url);return response(saved);});
  window.history={replaceState(...args){history.push(args);}};
  assert.equal(await window.AtlasForesight.restoreFromLink(original,assessmentId),true);
  assert.deepEqual(calls,[`/api/assessments/${assessmentId}`]);assert.equal(t.ui.assessment.rounds.length,1);assert.equal(t.ui.attempted,true);
  const url=new URL(t.assessmentLink());assert.equal(url.searchParams.get('result_id'),resultId);assert.equal(url.searchParams.get('assessment_id'),assessmentId);assert.equal(url.searchParams.get('view'),'foresight');
  assert.equal(history[0][2],url.href);assert.match(t.permalinkControls(),/復元リンク/);assert.match(t.permalinkControls(),/リンクをコピー/);
});

test('mismatched or missing saved assessment blocks auto creation and preserves a clear error',async()=>{
  for(const saved of [assessment('requested',{result_id:'different'}),assessment('different'),null]){
    const calls=[];const {t,window}=harness(async url=>{calls.push(url);return saved?response(saved):{ok:false,json:async()=>({detail:'保存評価が見つかりません'})};});
    assert.equal(await window.AtlasForesight.restoreFromLink(result,'requested'),false);
    assert.equal(t.ui.assessment,null);assert.equal(t.ui.attempted,true);assert.match(t.ui.error,/一致しない|見つかりません/);
    t.ui.status={providers:[]};t.mount(null,result);
    assert.deepEqual(calls,['/api/assessments/requested']);
  }
});

test('saved assessment arriving after a dataset change cannot update data or URL',async()=>{
  let release;const history=[];const {t,window}=harness(async()=>{await new Promise(resolve=>{release=resolve;});return response(assessment('saved'));});
  window.history={replaceState(...args){history.push(args);}};
  const pending=window.AtlasForesight.restoreFromLink(result,'saved');t.reset({...result,id:'other-result'});release();
  assert.equal(await pending,false);assert.equal(t.ui.assessment,null);assert.equal(t.ui.result.id,'other-result');assert.equal(history.length,0);
});

const connectedLLM={llm:{local:{available:true,models:[{id:'local-model'}],default_model:'local-model'},openai:{configured:true}}};
function readyCommentary(h,extra={}){h.t.ui.result=result;h.t.applyAssessment(assessment('assessment-a',extra));h.t.ui.status=connectedLLM;h.t.ui.llmProvider='local';h.t.ui.llmModel='local-model';}

test('saved LLM failure is shown at the generation button after restoring an assessment',()=>{
  const h=harness();readyCommentary(h,{candidates:[candidate('topic-a',{llm_error:'実抄録の根拠がありません。 <bad>'})]});
  const html=h.t.candidateDetail(h.t.ui.assessment.candidates[0]);
  const form=html.slice(html.indexOf('<form id="fs-commentary-form"'),html.indexOf('</form>',html.indexOf('<form id="fs-commentary-form"')));
  assert.match(form,/前回のLLM解釈を生成できませんでした/);assert.match(form,/実抄録の根拠がありません/);assert.match(form,/role="alert"/);assert.match(form,/&lt;bad&gt;/);
  assert.ok(html.indexOf('id="fs-generated-commentary"')>html.indexOf('</form>',html.indexOf('<form id="fs-commentary-form"')));
});

test('synthetic data disables LLM generation in advance and shows how to proceed',async()=>{
  const calls=[],h=harness(async url=>{calls.push(url);throw new Error('must not request');});readyCommentary(h,{meta:{...result.meta,is_demo:true}});
  const html=h.t.llmControls();assert.match(html,/合成・テストデータ/);assert.match(html,/実際の論文抄録を取り込んでください/);assert.match(html,/入力データの種類による制限/);assert.match(html,/LLMの接続不良や研究の科学的根拠がないことを示しません/);
  assert.match(html,/<option value="local" selected disabled>ローカルLLM · デモデータ対象外/);assert.match(html,/<option value="openai"  disabled>OpenAI · デモデータ対象外/);assert.doesNotMatch(html,/根拠不足/);
  assert.match(html,/<button type="button"[^>]*data-action="datasets"[^>]*>保存済みデータを選ぶ<\/button>/);assert.match(html,/<button type="button"[^>]*data-action="upload"[^>]*>実論文データを追加<\/button>/);assert.match(html,/<button class="button button-quiet" type="submit" disabled>/);
  await h.t.generateCommentary();assert.equal(calls.length,0);assert.match(h.t.commentaryFeedback(),/合成・テストデータ/);assert.equal(h.t.ui.busy,false);
});

test('submit starts one request and displays generation status immediately beside the button',async()=>{
  let release;const calls=[],h=harness(async(url,options)=>{calls.push({url,body:options?.body&&JSON.parse(options.body)});return new Promise(resolve=>{release=resolve;});});readyCommentary(h);
  let prevented=false;h.documentEvents.get('submit')({target:{id:'fs-commentary-form'},preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(calls.length,1);assert.deepEqual(calls[0].body,{candidate_id:'topic-a',provider:'local',model:'local-model'});
  assert.match(h.t.llmControls(),/aria-busy="true"/);assert.match(h.t.llmControls(),/生成中…/);assert.match(h.t.commentaryFeedback(),/根拠を抽出/);
  await h.t.generateCommentary();assert.equal(calls.length,1);
  h.t.reset({...result,id:'other-result'});release(response({job_id:'old'}));await new Promise(resolve=>setImmediate(resolve));assert.equal(h.t.ui.commentaryFeedback,null);assert.equal(calls.length,1);
});

test('server preflight rejection is displayed beside the button without clearing existing narrative',async()=>{
  const h=harness(async()=>({ok:false,json:async()=>({detail:'実抄録がありません。実際の抄録を取り込んでください。'})}));
  const original=candidate('topic-a',{narrative:{mode:'local',headline:'Previous interpretation',sections:[]}});readyCommentary(h,{candidates:[original]});
  await h.t.generateCommentary();assert.equal(h.t.ui.assessment.candidates[0],original);assert.equal(h.t.ui.busy,false);
  const html=h.t.llmControls();assert.match(html,/role="alert"/);assert.match(html,/実抄録がありません/);assert.match(html,/取得済みの根拠は保持/);assert.doesNotMatch(html,/aria-busy="true"/);
});

test('a completed local commentary preserves the chosen model and puts prose below controls',async()=>{
  const saved=assessment('completed',{candidates:[candidate('topic-a',{narrative:{mode:'local',model:'local-model',headline:'New grounded interpretation',sections:[]}})]});
  const h=harness(async url=>url.endsWith('/commentaries')?response({job_id:'j'}):url==='/api/jobs/j'?response({status:'completed',assessment_id:'completed'}):response(saved));readyCommentary(h);
  await h.t.generateCommentary();assert.equal(h.t.ui.assessment,saved);assert.equal(h.t.ui.llmProvider,'local');assert.equal(h.t.ui.llmModel,'local-model');
  const html=h.t.candidateDetail(saved.candidates[0]);assert.match(html,/解釈を生成しました/);assert.ok(html.indexOf('New grounded interpretation')>html.indexOf('id="fs-commentary-form"'));
});

test('a successful status without an actual LLM narrative becomes a visible failure',async()=>{
  const h=harness(async url=>url.endsWith('/commentaries')?response({job_id:'j'}):url==='/api/jobs/j'?response({status:'completed',assessment_id:'empty'}):response(assessment('empty')));readyCommentary(h);
  await h.t.generateCommentary();assert.match(h.t.commentaryFeedback(),/解釈の本文が返りません/);assert.doesNotMatch(h.t.commentaryFeedback(),/解釈を生成しました/);
});

test('deterministic display is labeled LLM unused and remains available for demo data',async()=>{
  const calls=[],saved=assessment('refreshed',{meta:{...result.meta,is_demo:true}});
  const h=harness(async(url,options)=>{calls.push({url,body:options?.body&&JSON.parse(options.body)});return url.endsWith('/commentaries')?response({job_id:'j'}):url==='/api/jobs/j'?response({status:'completed',assessment_id:'refreshed'}):response(saved);});readyCommentary(h,{meta:{...result.meta,is_demo:true}});h.t.ui.llmProvider='none';
  assert.match(h.t.llmControls(),/定型レポートを表示/);await h.t.generateCommentary();assert.equal(calls[0].body.provider,'none');assert.match(h.t.commentaryFeedback(),/LLMは使用していません/);assert.equal(h.t.ui.commentaryFeedback.status,'complete');
});

test('late commentary errors do not appear for a newer selected data set',async()=>{
  let release;const h=harness(async()=>new Promise(resolve=>{release=resolve;}));readyCommentary(h);const old=h.t.generateCommentary();
  h.t.reset({...result,id:'new-data'});release({ok:false,json:async()=>({detail:'old backend error'})});await old;
  assert.equal(h.t.ui.error,'');assert.equal(h.t.ui.commentaryFeedback,null);
});

test('commentary status for one candidate is not shown as another candidate result',async()=>{
  let release;const h=harness(async()=>new Promise(resolve=>{release=resolve;}));readyCommentary(h,{candidates:[candidate('topic-a'),candidate('topic-b')]});const pending=h.t.generateCommentary();
  h.window.AtlasForesight.selectTopic('topic-b');assert.equal(h.t.commentaryFeedback(),'');assert.match(h.t.llmControls(),/別の探索処理/);
  h.t.reset(null);release(response({job_id:'j'}));await pending;
});


const numericWarning=(location,extra={})=>({code:'numeric_mismatch',location,message:'根拠と数値・符号・単位が一致しません。',unmatched_numbers:['900','-12'],unmatched_quantities:[{value:'900',unit:'GPa'}],...extra});
function warnedCandidate(){const warning=numericWarning('sections/0/text'),factWarning=numericWarning('facts/llm-fact-1/statement');return candidate('topic-a',{narrative:{mode:'local',model:'local-model',headline:'Generated interpretation',validation:{status:'warning',warnings:[warning,factWarning]},sections:[{title:'Strength finding',text:'The alloy reaches 900GPa. The measured change is -12%.',evidence_ids:['llm-fact-1'],validation:{status:'warning',warnings:[warning]}},{title:'Next check',text:'Verify the reported conditions.',validation:{status:'passed',warnings:[]}}]},content_facts:[{id:'llm-fact-1',paper_id:'p1',kind:'result',statement:'The alloy reaches 900GPa.',quote:'The alloy reached 0.9 GPa.',verification:'quote_checked_numeric_warning',validation:{status:'warning',warnings:[factWarning]}}]});}

test('numeric warnings retain the full generated text and expose numbers, signs, units and locations',()=>{
  const h=harness(),c=warnedCandidate();readyCommentary(h,{candidates:[c]});const unchanged=JSON.stringify(c);
  const html=h.t.commentary(c);
  assert.match(html,/role="alert"/);assert.match(html,/⚠ 数値照合に失敗・要確認/);
  assert.match(html,/<p>The alloy reaches 900GPa\. The measured change is -12%\.<\/p>/);
  assert.match(html,/<article class="fs-section-warning"><h4><span[^>]*>⚠/);
  assert.match(html,/<details class="fs-validation-details">/);assert.match(html,/sections\/0\/text/);assert.match(html,/生成文 第1節 \/ 本文/);
  assert.match(html,/<code>900 GPa<\/code>/);assert.match(html,/<code>-12<\/code>/);
  assert.match(html,/<article class=""><h4>Next check<\/h4>/);
  assert.ok(html.indexOf('fs-validation-warning')<html.indexOf('<h3>Generated interpretation'));
  assert.equal(JSON.stringify(c),unchanged);assert.equal(c.growth.growth_pct,25);assert.equal(c.readiness.evidence_coverage_score,50);
});

test('warned content facts are visible without claiming their numeric references passed',()=>{
  const h=harness(),c=warnedCandidate();readyCommentary(h,{candidates:[c]});
  const html=h.t.evidenceProfile(c);
  assert.match(html,/<details class="fs-content-facts" open>/);assert.match(html,/class="fs-fact-warning"/);
  assert.match(html,/<h4>The alloy reaches 900GPa\.<\/h4>/);assert.match(html,/<blockquote>The alloy reached 0\.9 GPa\.<\/blockquote>/);
  assert.match(html,/facts\/llm-fact-1\/statement/);assert.match(html,/数値・符号・単位に照合できない/);
  assert.doesNotMatch(html,/原文一致と数値参照を機械検査しています/);
  delete c.narrative.validation;delete c.narrative.sections[0].validation;delete c.content_facts[0].validation;
  assert.equal(h.t.candidateValidation(c).status,'warning');
  assert.match(h.t.commentary(c),/⚠ 数値照合に失敗・要確認/);
});

test('saved warning assessment restores prose and warning status without generating again',async()=>{
  const c=warnedCandidate(),saved=assessment('saved-warning',{candidates:[c]}),calls=[];
  const h=harness(async url=>{calls.push(url);return response(saved);});h.t.ui.result=result;
  await h.t.restoreAssessment('saved-warning');
  assert.deepEqual(calls,['/api/assessments/saved-warning']);assert.equal(h.t.ui.error,'');
  assert.match(h.t.commentaryFeedback(),/保存された生成文を警告付き/);assert.match(h.t.commentaryFeedback(),/is-warning/);
  assert.match(h.t.commentary(h.t.ui.assessment.candidates[0]),/900GPa/);
});

test('warning completion is neither a generic error nor an all-clear success',async()=>{
  const saved=assessment('warning-done',{candidates:[warnedCandidate()]});
  const h=harness(async url=>url.endsWith('/commentaries')?response({job_id:'j'}):url==='/api/jobs/j'?response({status:'completed',assessment_id:'warning-done'}):response(saved));readyCommentary(h);
  await h.t.generateCommentary();assert.equal(h.t.ui.commentaryFeedback.status,'warning');assert.equal(h.t.ui.error,'');
  assert.match(h.t.commentaryFeedback(),/生成文は警告付き/);assert.match(h.t.commentaryFeedback(),/role="alert"/);
  assert.doesNotMatch(h.t.commentaryFeedback(),/is-complete|is-error|解釈を生成しました/);
  assert.match(h.t.commentary(saved.candidates[0]),/900GPa/);
});

test('passed validation shows original narrative with no numeric alert',()=>{
  const h=harness(),c=candidate('topic-a',{narrative:{mode:'local',headline:'Strength result',validation:{status:'passed',warnings:[]},sections:[{title:'Result',text:'Strength reached 0.9 GPa.',validation:{status:'passed',warnings:[]}}]},content_facts:[{id:'checked',paper_id:'p1',statement:'Strength reached 0.9 GPa.',quote:'Strength reached 0.9 GPa.',verification:'quote_and_numbers_checked',validation:{status:'passed',warnings:[]}}]});readyCommentary(h,{candidates:[c]});
  const html=h.t.commentary(c)+h.t.evidenceProfile(c);assert.match(html,/Strength reached 0\.9 GPa/);
  assert.doesNotMatch(html,/⚠|fs-validation-warning|fs-section-warning|fs-fact-warning|数値照合に失敗/);
  assert.equal(h.t.commentaryFeedback(),'');
});

test('validation messages, source locations and units cannot inject HTML',()=>{
  const h=harness(),c=warnedCandidate(),bad='<img src=x onerror=alert(1)>',warning=numericWarning(bad,{message:bad,unmatched_numbers:[bad],unmatched_quantities:[{value:bad,unit:'</code><script>bad</script>'}]});
  c.narrative.validation={status:'warning',warnings:[warning]};c.narrative.sections[0].validation={status:'warning',warnings:[warning]};c.narrative.headline=bad;c.content_facts[0].statement=bad;c.content_facts[0].validation={status:'warning',warnings:[warning]};readyCommentary(h,{candidates:[c]});
  const html=h.t.commentary(c)+h.t.evidenceProfile(c)+h.t.commentaryFeedback();
  assert.doesNotMatch(html,/<(?:img|script)[\s>]/);assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;/);assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);
});
