import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const report={id:'report-a',result_id:'result-a',focus:{id:'topic-a',label:'Steel',keywords:[],count:3},neighbor:null,neighbors:[],topic_model:'nmf',narrative:{headline:'Old observations'}};
function harness(overrides={}){
 const elements=new Map(),messages=[],progress=[{textContent:''},{textContent:''}];
 const $=selector=>{if(!elements.has(selector))elements.set(selector,{innerHTML:'',textContent:'',open:true});return elements.get(selector);};
 const escape=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
 const fieldState={request:0,resultId:'result-a',topicId:'topic-a',neighborId:null,report,busy:false,error:'',stage:'',provider:'none',completed:false,llm:{local:{available:true,models:[{id:'chosen-model'}]},openai:{configured:true,model:'openai-model'}},localModel:'chosen-model'};
 const ctx=vm.createContext({fieldState,state:{result:{id:'result-a',meta:{topic_model:'nmf',start_year:2021,end_year:2025}}},$, $$:()=>progress,e:escape,icon:()=>'',num:String,topicModelName:String,topicFor:()=>report.focus,message:value=>messages.push(value),fieldScopeNotice:()=>'',fieldComparisonChart:()=>'',fieldFlow:()=>'',fieldComparisonTable:()=>'',fieldKeywords:()=>'',fieldForecast:()=>'',fieldConnectionsEvidence:()=>'',fieldPanel:()=>'',fieldNarrative:r=>`<p>${escape(r.narrative?.headline)}</p>`,fieldNotes:()=>'',evidenceList:()=>'',panelHeader:()=>'',post:async()=>({job_id:'j'}),api:async url=>url.startsWith('/api/jobs/')?{status:'completed',field_report_id:'report-b'}:{...report,id:'report-b',narrative:{headline:'Generated interpretation'}},setTimeout:fn=>{fn();return 0;},...overrides});
 for(const name of ['fieldCurrent','fieldGenerationFeedback','refreshFieldGenerationFeedback','requestFieldReport','fieldLLMControls','renderFieldReport']){
  const start=source.search(new RegExp(`^(?:async )?function ${name}\\(`,'m'));
  assert.ok(start>=0,name);const tail=source.slice(start),next=tail.slice(1).search(/\n(?:async )?function /);
  vm.runInContext(next<0?tail:tail.slice(0,next+1),ctx);
 }
 return {ctx,fieldState,elements,progress,messages,$};
}

test('field interpretation renders its status and errors beside its generation controls',()=>{
 const h=harness();h.fieldState.error='Local model rejected <request>';
 const html=h.ctx.fieldLLMControls();assert.match(html,/id="field-generation-feedback"/);assert.match(html,/role="alert"/);assert.match(html,/Local model rejected &lt;request&gt;/);
 h.fieldState.error='';h.fieldState.busy=true;h.fieldState.provider='local';h.fieldState.stage='Extracting research evidence';
 assert.match(h.ctx.fieldLLMControls(),/生成中…/);assert.match(h.ctx.fieldLLMControls(),/Extracting research evidence/);assert.match(h.ctx.fieldLLMControls(),/aria-busy="true"/);
 h.ctx.refreshFieldGenerationFeedback();assert.ok(h.progress.every(node=>node.textContent==='Extracting research evidence'));
});

test('successful field generation preserves the selected model and shows prose below controls',async()=>{
 const sent=[],h=harness({post:async(url,payload)=>{sent.push(payload);return {job_id:'j'};}});
 await h.ctx.requestFieldReport('local');assert.equal(sent[0].model,'chosen-model');assert.equal(h.fieldState.localModel,'chosen-model');assert.equal(h.fieldState.completed,true);
 const html=h.$('#field-report-body').innerHTML;assert.match(html,/解釈を更新しました/);assert.ok(html.indexOf('Generated interpretation')>html.indexOf('id="field-llm-controls"'));
});

test('a failed field generation preserves the old report and retries the same provider',async()=>{
 const h=harness({post:async()=>{throw new Error('モデルの接続を確認してください。');}});await h.ctx.requestFieldReport('local');
 assert.equal(h.fieldState.report,report);assert.equal(h.fieldState.provider,'local');assert.equal(h.fieldState.busy,false);
 assert.match(h.ctx.fieldLLMControls(),/モデルの接続を確認してください/);assert.match(h.ctx.fieldLLMControls(),/同じ方法で再試行/);
 assert.match(source,/'field-retry':\(\)=>requestFieldReport\(fieldState.provider\|\|'none'\)/);
});

test('duplicate generation is ignored while its immediate progress remains visible',async()=>{
 let release;let count=0;const h=harness({post:()=>{count++;return new Promise(resolve=>{release=resolve;});}});
 const first=h.ctx.requestFieldReport('local');await h.ctx.requestFieldReport('openai');assert.equal(count,1);assert.equal(h.fieldState.busy,true);assert.match(h.ctx.fieldLLMControls(),/生成中…/);
 h.fieldState.request++;release({job_id:'old'});await first;
});

test('late field response cannot replace another field selection',async()=>{
 let release;const h=harness({post:()=>new Promise(resolve=>{release=resolve;})});const first=h.ctx.requestFieldReport('local');
 h.fieldState.request++;h.fieldState.topicId='topic-b';h.fieldState.error='New selection';h.fieldState.busy=false;release({job_id:'old'});await first;
 assert.equal(h.fieldState.report,report);assert.equal(h.fieldState.error,'New selection');assert.equal(h.fieldState.busy,false);
});

test('unavailable field LLM gives a visible reason instead of a silent return',async()=>{
 const h=harness({post:()=>{throw new Error('unexpected call');}});h.fieldState.llm.local.available=false;await h.ctx.requestFieldReport('local');
 assert.equal(h.fieldState.report,report);assert.match(h.$('#field-generation-feedback').innerHTML,/ローカルLLMに接続できません/);assert.equal(h.fieldState.busy,false);
});
