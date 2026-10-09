import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const code=readFileSync(new URL('../static/publication-dates.js',import.meta.url),'utf8');
const datasetID='a'.repeat(32),jobID='b'.repeat(32),derivedID='c'.repeat(32),otherDatasetID='d'.repeat(32);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const job=(extra={})=>({id:jobID,dataset_id:datasetID,status:'paused',target:'missing_month',batch_size:500,
  unique_dois:20000,processed_dois:500,remaining_dois:19500,found_dois:420,not_found_dois:80,error_dois:0,cached_dois:0,eligible_papers:21000,...extra});
const library=(jobs=[])=>({coverage:{month_or_day:200,missing_month:20800,no_doi:1000},jobs});
const page=(extra={})=>({items:[],offset:0,limit:20,total:0,...extra});
const attrs=text=>Object.fromEntries([...text.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(match=>[match[1],match[2]??'']));

function harness(responder){
  const calls=[],applied=[],timers=new Map();let nextTimer=0,dialog;
  class Node{
    constructor(owner,attributes={}){this.owner=owner;this.attributes=attributes;this.value=attributes.value||'';this.checked='checked' in attributes;this.disabled='disabled' in attributes;this.hidden=false;this.dataset={};this.textContent='';this._html='';this.events=new Map();for(const [key,value] of Object.entries(attributes))if(key.startsWith('data-'))this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;}
    set innerHTML(value){this._html=value;this.owner?.register(value);}
    get innerHTML(){return this._html;}
    addEventListener(name,callback){this.events.set(name,[...(this.events.get(name)||[]),callback]);}
    dispatch(name,event={}){for(const callback of this.events.get(name)||[])callback(event);}
    setAttribute(name,value){this.attributes[name]=value;}
    checkValidity(){return !this.value||/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(this.value);}
    closest(selector){return selector==='[data-pd-action]'&&this.dataset.pdAction?this:null;}
  }
  class Dialog extends Node{
    constructor(){super(null);this.owner=this;this.nodes=new Map();this.open=false;}
    set innerHTML(value){this.nodes.clear();this._html=value;this.register(value);}
    get innerHTML(){return this._html;}
    register(html){
      for(const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/g)){
        const a=attrs(match[2]),keys=[];if(a.id)keys.push(`#${a.id}`);if(a['data-pd-field'])keys.push(`[data-pd-field="${a['data-pd-field']}"]`);if(a['data-pd-action'])keys.push(`[data-pd-action="${a['data-pd-action']}"]`);
        if(keys.length){const node=new Node(this,a);for(const key of keys)this.nodes.set(key,node);}
      }
      for(const match of html.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)){
        const a=attrs(match[1]),node=this.nodes.get(`[data-pd-field="${a['data-pd-field']}"]`);if(!node)continue;
        const options=[...match[2].matchAll(/<option\b([^>]*)>/g)].map(item=>attrs(item[1]));node.value=(options.find(item=>'selected' in item)||options[0]||{}).value||'';
      }
    }
    querySelector(selector){return this.nodes.get(selector)||null;}
    showModal(){this.open=true;}
    close(){this.open=false;this.dispatch('close');}
  }
  const window={__ATLAS_UI_TEST__:true},document={createElement(){dialog=new Dialog();return dialog;},body:{appendChild(){}}};
  const sandbox=vm.createContext({window,document,URLSearchParams,console,Promise,Map,Set,
    setTimeout(callback){const id=++nextTimer;timers.set(id,callback);return id;},clearTimeout(id){timers.delete(id);}});
  vm.runInContext(code,sandbox);
  const context={dataset:{id:datasetID,name:'Scopus <steel>',paper_count:21000},onApplied:value=>applied.push(value),api:async(url,options)=>{
    calls.push({url,options});if(responder){const value=responder(url,options);if(value!==undefined)return value;}
    if(url.startsWith('/api/datasets/'))return library();
    if(url.includes('/records?'))return page();
    if(url.endsWith('/apply'))return {dataset:{id:derivedID,name:'補完版'},applied_count:420};
    return job();
  }};
  const t=window.__publicationDatesTest;
  return {t,context,calls,applied,timers,get dialog(){return dialog;},field:key=>dialog.querySelector(`[data-pd-field="${key}"]`),
    node:selector=>dialog.querySelector(selector),change(key){dialog.dispatch('change',{target:this.field(key)});},
    click(name){dialog.dispatch('click',{target:dialog.querySelector(`[data-pd-action="${name}"]`),preventDefault(){}});}};
}

test('opening the library uses dataset endpoint and defaults to 500 DOI lookups',async()=>{
  const h=harness();await h.t.open(h.context);
  assert.equal(h.calls[0].url,`/api/datasets/${datasetID}/publication-dates`);
  assert.equal(h.field('batch').value,'500');assert.equal(h.field('target').value,'missing_month');
  assert.equal(h.field('policy').value,'same_year');assert.equal(h.field('overwrite').checked,false);
  assert.equal(h.t.options().batch_size,500);assert.match(h.dialog.innerHTML,/Scopus &lt;steel&gt;/);
  assert.match(h.dialog.innerHTML,/DOIと入力したメールはCrossrefへ送信/);
});

test('start and resume send the documented options without mixing application credentials',async()=>{
  const h=harness();await h.t.open(h.context);h.field('target').value='missing_day';h.field('email').value='reader@example.org';
  await h.t.action('start');
  const start=h.calls.find(call=>call.url==='/api/publication-date-jobs'&&call.options);
  assert.equal(start.options.method,'POST');assert.deepEqual(JSON.parse(start.options.body),{batch_size:500,contact_email:'reader@example.org',dataset_id:datasetID,target:'missing_day'});
  h.field('batch').value='20000';await h.t.action('resume');
  const resumed=h.calls.find(call=>call.url.endsWith('/resume'));
  assert.deepEqual(JSON.parse(resumed.options.body),{batch_size:20000,contact_email:'reader@example.org'});
});

test('invalid batch sizes never initiate a network mutation',async()=>{
  for(const value of ['', '0','20001','1.5','not-a-number','Infinity','-4']){
    const h=harness();await h.t.open(h.context);h.field('batch').value=value;await h.t.action('start');
    assert.equal(h.calls.filter(call=>call.options?.method==='POST').length,0,value);
    assert.match(h.t.state.error,/1〜20,000の整数/);assert.equal(h.t.state.busy,false);
  }
});

test('a malformed optional email prevents start but an empty email is valid',async()=>{
  const h=harness();await h.t.open(h.context);h.field('email').value='invalid email';await h.t.action('start');
  assert.match(h.t.state.error,/メール/);assert.equal(h.calls.filter(call=>call.options).length,0);
  h.field('email').value='';await h.t.action('start');assert.equal(h.calls.filter(call=>call.url==='/api/publication-date-jobs').length,1);
});

test('saved history uses job dataset ownership and restores its target',async()=>{
  const h=harness(url=>url.startsWith('/api/datasets/')?library([job({target:'all'})]):url===`/api/publication-date-jobs/${jobID}`?job({target:'all'}):undefined);
  await h.t.open(h.context);assert.equal(h.t.state.job.id,jobID);assert.equal(h.field('target').value,'all');
  assert.ok(h.calls.some(call=>call.url.includes('/records?limit=20&offset=0&policy=same_year&overwrite_existing=false')));
  const wrong=harness(url=>url===`/api/publication-date-jobs/${jobID}`?job({dataset_id:otherDatasetID}):undefined);
  await wrong.t.open(wrong.context);await wrong.t.loadJob(jobID);assert.match(wrong.t.state.error,/データセットが一致しません/);assert.equal(wrong.t.state.job,null);
});

test('preview and independent CSV use the currently chosen date policy and overwrite flag',async()=>{
  const h=harness();await h.t.open(h.context);h.t.state.job=job();h.field('policy').value='print_first';h.field('overwrite').checked=true;
  await h.t.records();h.t.renderStatus();
  assert.match(h.calls.at(-1).url,/policy=print_first&overwrite_existing=true/);
  assert.match(h.node('#pd-status').innerHTML,/export\?policy=print_first&overwrite_existing=true/);
  h.change('policy');await tick();assert.match(h.node('#pd-policy-note').textContent,/出版年.*年度集計.*変わ/);
});

test('applying dates passes the policy and notifies parent of the new immutable dataset',async()=>{
  const h=harness();await h.t.open(h.context);h.t.state.job=job();h.field('policy').value='online_first';h.field('overwrite').checked=true;
  h.field('email').value='reader@example.org';await h.t.action('apply');
  const call=h.calls.find(item=>item.url.endsWith('/apply'));assert.deepEqual(JSON.parse(call.options.body),{policy:'online_first',overwrite_existing:true});
  assert.equal(h.applied.length,1);assert.equal(h.applied[0].dataset.id,derivedID);
  assert.equal(h.dialog.open,false);assert.equal(h.field('email').value,'');
});

test('an invalid apply response leaves the dialog open and does not notify parent',async()=>{
  const h=harness(url=>url.endsWith('/apply')?{dataset:{id:'not-an-id'}}:undefined);await h.t.open(h.context);h.t.state.job=job();
  await h.t.action('apply');assert.equal(h.applied.length,0);assert.equal(h.dialog.open,true);assert.match(h.t.state.error,/補完版データセット/);
});

test('closing a dialog clears the contact address and polling without pausing the server job',async()=>{
  const h=harness(url=>url===`/api/publication-date-jobs/${jobID}`?job({status:'running'}):undefined);
  await h.t.open(h.context);h.t.state.job=job({status:'running'});h.field('email').value='reader@example.org';await h.t.poll();
  assert.equal(h.timers.size,1);h.dialog.close();assert.equal(h.timers.size,0);assert.equal(h.field('email').value,'');
  assert.equal(h.calls.filter(call=>call.url.endsWith('/pause')).length,0);
});

test('an old library response cannot replace a newly opened dataset',async()=>{
  const old=deferred(),h=harness(url=>url.startsWith(`/api/datasets/${datasetID}/`)?old.promise:undefined);
  const opening=h.t.open(h.context);await tick();h.dialog.close();await h.t.open({...h.context,dataset:{id:otherDatasetID,name:'Other corpus',paper_count:7}});
  old.resolve(library([job()]));await opening;assert.equal(h.t.state.context.dataset.id,otherDatasetID);assert.equal(h.t.state.job,null);
  assert.equal(h.t.state.library.jobs.length,0);
});

test('a late start response cannot revive a closed dialog or begin polling',async()=>{
  const pending=deferred(),h=harness((url,options)=>url==='/api/publication-date-jobs'&&options?pending.promise:undefined);
  await h.t.open(h.context);const work=h.t.action('start');await tick();h.dialog.close();pending.resolve(job({status:'running'}));await work;
  assert.equal(h.t.state.job,null);assert.equal(h.timers.size,0);assert.equal(h.t.state.busy,false);
});

test('a late apply response does not switch a newly opened dataset',async()=>{
  const pending=deferred(),h=harness(url=>url.endsWith('/apply')?pending.promise:undefined);await h.t.open(h.context);h.t.state.job=job();
  const applying=h.t.action('apply');await tick();h.dialog.close();await h.t.open({...h.context,dataset:{id:otherDatasetID,name:'New selection',paper_count:7}});
  pending.resolve({dataset:{id:derivedID}});await applying;assert.equal(h.applied.length,0);assert.equal(h.dialog.open,true);assert.equal(h.t.state.context.dataset.id,otherDatasetID);
});

test('outdated record success cannot overwrite newer policy or pagination',async()=>{
  const pending=deferred(),h=harness(url=>url.includes('/records?')?pending.promise:undefined);await h.t.open(h.context);h.t.state.job=job();
  const work=h.t.records();h.field('policy').value='online_first';h.t.state.offset=20;
  pending.resolve(page({items:[{title:'old answer'}],total:20}));await work;assert.equal(h.t.state.records,null);
});

test('a failed old preview request cannot display an error after reopening another dataset',async()=>{
  const pending=deferred(),h=harness(url=>url.includes('/records?')?pending.promise:undefined);await h.t.open(h.context);h.t.state.job=job();
  h.change('policy');await tick();h.dialog.close();await h.t.open({...h.context,dataset:{id:otherDatasetID,name:'Other',paper_count:7}});
  pending.reject(new Error('Old dataset preview failed'));await tick();assert.equal(h.t.state.error,'');assert.equal(h.node('#pd-error').textContent,'');
});

test('preview renders selected candidate dates, Japanese reasons and escaped source text',async()=>{
  const h=harness();await h.t.open(h.context);h.t.state.records=page({total:1,items:[{paper_id:'p1',title:'<img src=x onerror=alert(1)>',doi:'10.1234/test',
    original:{year:2024,publication_date:'',date_precision:'year'},status:'found',candidates:[{date:'2024-05-16',precision:'day',kind:'published-online'}],
    proposed:{candidate:{date:'2024-05-16',precision:'day',kind:'published-online'},apply:true,conflict:false,reason:'selected'},conflict:false}]});
  h.t.renderRecords();const html=h.node('#pd-results').innerHTML;
  assert.equal((html.match(/2024-05-16/g)||[]).length,2,'both retrieved candidate and adoption column show the date');
  assert.match(html,/オンライン公開日/);assert.doesNotMatch(html,/<img|>selected</);assert.match(html,/&lt;img/);
});

test('running jobs disable expensive mutations and counts remain DOI counts',async()=>{
  const h=harness();await h.t.open(h.context);h.t.state.job=job({status:'running'});h.t.renderStatus();
  assert.equal(h.node('[data-pd-action="apply"]').disabled,true);assert.equal(h.node('[data-pd-action="resume"]').disabled,true);
  assert.equal(h.node('[data-pd-action="pause"]').hidden,false);assert.equal(h.field('batch').disabled,true);
  const html=h.node('#pd-status').innerHTML;assert.match(html,/500 \/ 20,000 DOI/);assert.match(html,/21,000件/);assert.match(html,/重複を除いたDOI数/);
});

test('an unadopted conflicting candidate never appears as the saved date',async()=>{
  const h=harness();await h.t.open(h.context);
  const html=h.t.recordRow({title:'Existing date',original:{publication_date:'2024-05',date_precision:'month'},
    candidates:[{date:'2024-04-03',precision:'day',kind:'published-online'}],
    proposed:{candidate:{date:'2024-04-03'},apply:false,reason:'existing_date_conflict',conflict:true},
    observation:{fetched_at:'2026-10-09T12:34:56+00:00'}});
  const savedColumn=html.split('<td>').at(-1);
  assert.match(savedColumn,/<strong>2024-05<\/strong>/);assert.doesNotMatch(savedColumn,/2024-04-03/);
  assert.match(savedColumn,/既存の日付と一致しないため保持/);assert.match(savedColumn,/⚠/);
  assert.match(html,/Crossref取得: 2026-10-09 12:34:56 UTC/);
});

test('failure for an obsolete policy cannot replace the current preview',async()=>{
  const old=deferred(),h=harness(url=>url.includes('/records?')&&url.includes('policy=same_year')?old.promise:undefined);
  await h.t.open(h.context);h.t.state.job=job();h.change('policy');await tick();
  h.field('policy').value='online_first';h.change('policy');await tick();
  old.reject(new Error('Obsolete policy request failed'));await tick();
  assert.equal(h.t.state.error,'');assert.equal(h.t.state.records.total,0);
});

test('pause targets the active job and preserves the selected dataset',async()=>{
  const h=harness();await h.t.open(h.context);h.t.state.job=job({status:'running'});await h.t.action('pause');
  const call=h.calls.find(item=>item.url.endsWith('/pause'));
  assert.equal(call.url,`/api/publication-date-jobs/${jobID}/pause`);assert.equal(call.options.method,'POST');
  assert.deepEqual(JSON.parse(call.options.body),{});assert.equal(h.t.state.context.dataset.id,datasetID);
});

test('invalid dataset IDs are rejected before any library request',async()=>{
  const h=harness();await assert.rejects(h.t.open({...h.context,dataset:{id:'../outside'}}),/データセットを選択/);
  assert.equal(h.calls.length,0);
});
