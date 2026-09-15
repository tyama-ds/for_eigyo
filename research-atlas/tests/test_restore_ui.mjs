import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const resultId='aeda6d53f096494b90cd3004b965c579',assessmentId='1bde12c5598146cca30274a2217e0c35';
const link={resultId,assessmentId,view:'foresight'};
function harness(overrides={}){
  const elements=new Map(),calls=[],events=[];
  const $=id=>{if(!elements.has(id))elements.set(id,{value:'',textContent:'',hidden:true});return elements.get(id);};
  const dataset={id:'original-dataset',name:'Saved papers',analysis_defaults:{start_year:2021,end_year:2025}};
  const result={id:resultId,dataset_id:dataset.id,meta:{start_year:2026,end_year:2026,topic_model:'nmf',embedding:'tfidf',forecast_horizon:2},options:{start_year:2026,end_year:2026,n_topics:4,topic_model:'nmf',embedding:'tfidf',anchor_month:'2026-08',window_months:3,min_topic_size:5},topics:[{id:'topic-a'}],papers:[]};
  const values={URL,URLSearchParams,location:{href:'http://127.0.0.1:8000/?x=keep&result_id=old&assessment_id=old&view=foresight'},viewLabels:{overview:[],foresight:[],papers:[]},state:{selectionVersion:0,run:0,dataset:null,result:null,status:{datasets:[dataset]}},$,showLoading(){},resetFieldReport(){},resetAuthorNetwork(){},updateDataset(){},syncModelControls(){},resetFilters(){},render(){calls.push('render');},renderWarnings(){},renderSourceNotice(){},loadInsights(){},classifiedTopics:()=>[],analysisMethodLabel:()=>'',topicModelName:()=>'',mapRepresentationLabel:()=>'',CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},window:{dispatchEvent(event){events.push(event);},history:{replaceState(...args){calls.push(args);}},AtlasForesight:{async restoreFromLink(value,id){calls.push(['restoreAssessment',value.id,id]);return true;}}},api:async url=>{calls.push(url);return result;},...overrides};
  const ctx=vm.createContext(values);
  for(const name of ['parseRestoreLink','clearRestoreLink','applyDatasetAnalysisDefaults','restoreResultOptions','restoreAnalysisLink']){
    const line=source.split('\n').find(line=>new RegExp(`^(?:async )?function ${name}\\(`).test(line));
    assert.ok(line,name);vm.runInContext(line,ctx);
  }
  return {ctx,elements,calls,events,result,dataset,$};
}

test('restoration accepts validated IDs only and no parameters keeps normal startup',()=>{
  const {ctx}=harness();assert.equal(ctx.parseRestoreLink(''),null);
  const parsed=ctx.parseRestoreLink(`?result_id=${resultId}&assessment_id=${assessmentId}&view=foresight`);
  assert.deepEqual(JSON.parse(JSON.stringify(parsed)),link);
  assert.throws(()=>ctx.parseRestoreLink(`?assessment_id=${assessmentId}`),/識別子/);
  assert.throws(()=>ctx.parseRestoreLink('?result_id=%3Cscript%3E'),/識別子/);
  assert.throws(()=>ctx.parseRestoreLink(`?result_id=${resultId}&assessment_id=`),/識別子/);
  assert.throws(()=>ctx.parseRestoreLink(`?result_id=${resultId}&result_id=${resultId}`),/識別子/);
  assert.throws(()=>ctx.parseRestoreLink(`?result_id=${resultId}&view=__proto__`),/画面/);
  assert.match(source,/if\(link\)await restoreAnalysisLink\(link\);else await loadDemo\(\)/);
});

test('restoration retrieves the saved result and original dataset without running analysis',async()=>{
  const {ctx,calls,result,dataset,$,events}=harness();
  await ctx.restoreAnalysisLink(link);
  assert.equal(ctx.state.dataset,dataset);assert.equal(ctx.state.result,result);assert.equal(ctx.state.view,'foresight');
  assert.deepEqual(calls,[`/api/results/${resultId}?view=summary`,['restoreAssessment',resultId,assessmentId],'render']);
  assert.equal(events.length,1);assert.equal(events[0].detail,result);
  assert.equal($('#start-year').value,2026);assert.equal($('#end-year').value,2026);
  assert.equal($('#monthly-anchor').value,'2026-08');assert.equal($('#monthly-window').value,'3');
  assert.equal($('#topic-model').value,'nmf');assert.equal($('#topic-count').value,'4');assert.equal($('#forecast-horizon').value,'2');
  assert.equal($('#analysis-time').textContent,'保存結果を復元');
});

test('missing, incorrect or orphaned result is not committed to app state',async()=>{
  const missing=harness({api:async()=>{throw new Error('分析が見つかりません');}});
  await assert.rejects(missing.ctx.restoreAnalysisLink(link),/見つかりません/);assert.equal(missing.ctx.state.result,null);
  for(const change of [{id:'wrong'},{dataset_id:'missing'}]){
    const h=harness();Object.assign(h.result,change);
    await assert.rejects(h.ctx.restoreAnalysisLink(link),/確認できません|元データ/);
    assert.equal(h.ctx.state.result,null);assert.equal(h.ctx.state.dataset,null);
    assert.equal(h.calls.some(value=>Array.isArray(value)),false);
  }
});

test('late success and late failure cannot replace a newer selected analysis',async()=>{
  for(const fail of [false,true]){
    let release,reject;
    const h=harness({api:()=>new Promise((resolve,no)=>{release=resolve;reject=no;})});
    const pending=h.ctx.restoreAnalysisLink(link);
    h.ctx.state.selectionVersion++;h.ctx.state.run++;h.ctx.state.result={id:'new-result'};
    if(fail)reject(new Error('old connection failure'));else release(h.result);
    await pending;assert.equal(h.ctx.state.result.id,'new-result');assert.equal(h.ctx.state.dataset,null);assert.equal(h.events.length,0);
  }
});

test('failed assessment restore displays its error in the exploration view',async()=>{
  const h=harness();h.ctx.window.AtlasForesight.restoreFromLink=async()=>false;
  await h.ctx.restoreAnalysisLink({...link,view:'papers'});assert.equal(h.ctx.state.view,'foresight');assert.equal(h.ctx.state.result,h.result);
});

test('starting a new analysis clears old restore identifiers only',()=>{
  const {ctx,calls}=harness();ctx.clearRestoreLink();assert.equal(calls.length,1);
  const url=new URL(calls[0][2]);assert.equal(url.search,'?x=keep');
  assert.match(source,/if\(!state.dataset\)\{return loadDemo\(\);\}clearRestoreLink\(\)/);
});
