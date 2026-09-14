const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/refinement-ui.js','utf8');
const esc = value => String(value ?? '').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const decode = value => value.replace(/&(amp|lt|gt|quot|#39);/g,(_,key)=>({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"}[key]));
const flush = () => new Promise(setImmediate);
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
const IPC='IPC:H01M10/00',NEXT='IPC:B60W60/00',PREDICTED='IPC:G06V20/58',FT='F-term:5H029AJ06',CPC='CPC:B60W60/001';
function feedback(){return {active_keys:[IPC,FT,CPC],recommended_keys:[NEXT],candidates:[
  {kind:'IPC',code:'H01M10/00',title:'電池',recommended:false,evidence:[],keep_ids:[],exclude_ids:[]},
  {kind:'IPC',code:'B60W60/00',title:'自動運転',recommended:true,evidence:[],keep_count:4,exclude_count:0,keep_ids:['JP1','JP2']},
  {kind:'IPC',code:'G06V20/58',title:'道路の画像解析',recommended:false,evidence:[],predicted_keep_count:8,predicted_keep_ids:['JP-predicted']},
]};}
function result(){return {include:['自動運転','電池'],exclude:['冷却'],active_include:['自動運転'],active_exclude:[],classifications:feedback()};}
function setup(apiHandler=async path=>path==='/refinement'?result():{}){
  const dom={},inputs=[],requests=[],notices=[],rendered=[];
  function node(id){
    let html='';const el={id,value:'',textContent:'',disabled:false,inert:false,events:new Map(),addEventListener(name,fn){this.events.set(name,fn);}};
    Object.defineProperty(el,'innerHTML',{get:()=>html,set:value=>{
      html=value;
      // Rendering is the test boundary: fresh IDs represent each newly rendered
      // workspace; checkbox parsing exposes only the user's rendered choices.
      if(id==='step-content'){for(const key of Object.keys(dom))if(key!=='#step-content')delete dom[key];inputs.length=0;}
      for(const match of value.matchAll(/\bid="([^"]+)"/g))dom['#'+match[1]]=node(match[1]);
      const names=[...value.matchAll(/<input\b[^>]*\bname="([^"]+)"[^>]*>/g)].map(match=>match[1]);
      for(const name of new Set(names))for(let i=inputs.length-1;i>=0;i--)if(inputs[i].name===name)inputs.splice(i,1);
      for(const match of value.matchAll(/<input\b([^>]*\bname="([^"]+)"[^>]*)>/g)){
        const input=node('');input.name=match[2];input.value=decode(match[1].match(/\bvalue="([^"]*)"/)?.[1]||'');input.checked=/\bchecked\b/.test(match[1]);inputs.push(input);
      }
    }});return el;
  }
  dom['#step-content']=node('step-content');
  const context={state:{queries:[],training:null,job:{status:'idle'},candidates:[{kind:'F-term',code:'5H029AJ06',title:'電池特性'},{kind:'CPC',code:'B60W60/001',title:'Control'}]},refinement:{},step:3,activeTab:'explore',viewedQueryId:null,esc,keyOf:c=>c.key||`${c.kind}:${c.code}`,
    $:selector=>dom[selector]||null,$$:selector=>{const name=selector.match(/name="([^"]+)"/)?.[1];return inputs.filter(input=>input.name===name&&(!selector.includes(':checked')||input.checked));},
    on:(selector,event,fn)=>dom[selector]?.addEventListener(event,fn),counts:()=>({keep:2,exclude:2}),trainingHtml:()=>'',queryCard:()=>'',historyHtml:()=>'',bindQuery(){},bindHistory(){},setStep:next=>context.step=next,renderStep:()=>rendered.push('render'),
    api:(path,body)=>{requests.push({path,body});return apiHandler(path,body);},toast:(...args)=>notices.push(args),busy:async(_button,_message,action)=>action(),
  };
  vm.createContext(context);vm.runInContext(source,context);
  return {context,dom,inputs,requests,notices,rendered,read:expression=>vm.runInContext(expression,context),click:id=>dom[id].events.get('click')({currentTarget:dom[id]})};
}

test('initial selection unions active and recommended codes without predicted-only candidates',()=>{
  const s=setup(),f=feedback();f.active_keys.push(IPC);f.recommended_keys.push(NEXT);
  assert.deepEqual(Array.from(s.context.refinementInitialKeys(f)),[IPC,FT,CPC,NEXT]);
  assert.ok(!s.context.refinementInitialKeys(f).includes(PREDICTED));
});

test('recommended replacement retains non-IPC classifications and does not mutate evidence',()=>{
  const s=setup(),f=feedback(),before=JSON.stringify(f);
  assert.deepEqual(Array.from(s.context.refinementReplacementKeys(f)),[FT,CPC,NEXT]);
  assert.equal(JSON.stringify(f),before);
  const rows=s.context.refinementClassRows(f,s.context.state.candidates);
  assert.equal(rows.length,5);assert.equal(rows.filter(c=>c.kind==='F-term').length,1);
  rows[0].title='changed view';assert.equal(f.candidates[0].title,'電池');
});

test('rendered checkboxes select recommendations and active codes but not prediction-only evidence',()=>{
  const s=setup();s.context.refinement={classifications:feedback()};s.read('refinementClassPicked=new Set(refinementInitialKeys(refinement.classifications))');
  const html=s.context.refinementIpcHtml(feedback());
  const inputFor=key=>[...html.matchAll(/<input[^>]*>/g)].map(match=>match[0]).find(tag=>tag.includes(`value="${key}"`));
  assert.match(inputFor(IPC),/checked/);assert.match(inputFor(NEXT),/checked/);assert.match(inputFor(FT),/checked/);assert.match(inputFor(CPC),/checked/);
  assert.ok(!inputFor(PREDICTED).includes('checked'));assert.ok(html.includes('予測：必要寄り 8'));
});

test('classification labels, evidence identifiers, reasons, warnings and history are escaped',()=>{
  const s=setup(),bad='<img src=x onerror="bad()">&\'x\'';
  const f={note:bad,active_keys:[],recommended_keys:[],warnings:[bad],candidates:[{kind:bad,code:bad,title:bad,evidence:[],reason:bad,keep_ids:[bad],exclude_ids:[bad],predicted_keep_ids:[bad]}]};
  const html=s.context.refinementIpcHtml(f),history=s.context.classificationChangesHtml({added_keys:[bad],removed_keys:[bad],evidence:f.candidates});
  for(const rendered of [html,history]){assert.ok(!rendered.includes('<img'));assert.ok(rendered.includes('&lt;img src=x onerror=&quot;bad()&quot;&gt;&amp;&#39;x&#39;'));assert.ok(!rendered.includes('href='));}
});

test('replacement and restore controls are explicit and preserve current CPC/F-term',async()=>{
  const s=setup();s.context.renderRefinementWorkspace();await flush();
  s.click('#refinement-recommend-ipc');assert.deepEqual(Array.from(s.read('[...refinementClassPicked]')),[FT,CPC,NEXT]);
  assert.match(s.dom['#refinement-ipc-change'].textContent,/追加 1 \/ 解除 1/);
  s.click('#refinement-restore-ipc');assert.deepEqual(Array.from(s.read('[...refinementClassPicked]')),[IPC,FT,CPC]);
  assert.match(s.dom['#refinement-ipc-change'].textContent,/追加 0 \/ 解除 0/);
});

test('a late refinement read cannot overwrite a newer workspace instance',async()=>{
  const first=deferred(),second=deferred();let reads=0;const s=setup(()=>++reads===1?first.promise:second.promise);
  s.context.renderRefinementWorkspace();s.context.renderRefinementWorkspace();
  const latest={...result(),include:['最新の語'],active_include:['最新の語'],classifications:{active_keys:[NEXT],recommended_keys:[],candidates:[]}};
  second.resolve(latest);await flush();const current=s.dom['#include-terms'],html=current.innerHTML;
  first.resolve({...result(),include:['古い語']});await flush();
  assert.equal(s.context.refinement,latest);assert.equal(s.dom['#include-terms'],current);assert.equal(current.innerHTML,html);assert.ok(!html.includes('古い語'));
});

test('a refinement read arriving after leaving the step does not paint or enable it',async()=>{
  const pending=deferred(),s=setup(()=>pending.promise);s.context.renderRefinementWorkspace();const html=s.dom['#include-terms'].innerHTML;
  s.context.step=2;pending.resolve(result());await flush();assert.equal(s.dom['#include-terms'].innerHTML,html);assert.equal(s.read('refinementLoaded'),false);
});

test('a late read error after switching tabs does not show an error in the new screen',async()=>{
  const pending=deferred(),s=setup(()=>pending.promise);s.context.renderRefinementWorkspace();const host=s.dom['#refinement-ipc-options'],html=host.innerHTML;
  s.context.activeTab='settings';pending.reject(Error('<old request failed>'));await flush();
  assert.equal(host.innerHTML,html);assert.equal(s.notices.length,0);
});

test('current read errors are escaped and leave proposal unavailable',async()=>{
  const s=setup(async()=>{throw Error('<img src=x>');});s.context.renderRefinementWorkspace();await flush();
  assert.ok(s.dom['#refinement-ipc-options'].innerHTML.includes('&lt;img src=x&gt;'));assert.ok(!s.dom['#refinement-ipc-options'].innerHTML.includes('<img'));
  assert.equal(s.read('refinementLoaded'),false);assert.equal(s.notices.length,1);
});

test('proposal payload contains only the reviewed classifications and checked terms',async()=>{
  const s=setup(async(path,body)=>path==='/refinement'?result():{queries:[{id:'next'}],job:{status:'idle'}});
  s.context.renderRefinementWorkspace();await flush();s.click('#refinement-recommend-ipc');
  await s.click('#refine-query');const request=s.requests.find(r=>r.path==='/query');
  assert.deepEqual(JSON.parse(JSON.stringify(request.body)),{refine:true,include:['自動運転'],exclude:[],classification_keys:[FT,CPC,NEXT]});
  assert.ok(!request.body.classification_keys.includes(PREDICTED));
});

test('a late proposal cannot overwrite or unlock a newly rendered refinement instance',async()=>{
  const pending=deferred(),s=setup(async path=>path==='/refinement'?result():pending.promise);
  s.context.renderRefinementWorkspace();await flush();const oldControls=s.dom['#refinement-controls'];const submitted=s.click('#refine-query');
  assert.equal(oldControls.inert,true);
  s.context.renderRefinementWorkspace();await flush();const newControls=s.dom['#refinement-controls'];newControls.inert=true;
  const currentState=s.context.state;currentState.marker='newer state';
  pending.resolve({queries:[{id:'obsolete response'}],job:{status:'idle'}});await submitted;
  assert.equal(s.context.state,currentState);assert.equal(s.context.state.marker,'newer state');
  assert.equal(newControls.inert,true,'an old finally block must not unlock new controls');
  assert.equal(s.rendered.length,0);assert.equal(s.notices.length,0);
});
