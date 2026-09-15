import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const code=readFileSync(new URL('../static/landscape.js',import.meta.url),'utf8');
const renderer=readFileSync(new URL('../static/map-terrain.js',import.meta.url),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const terrain={version:1,grid:{width:2,height:2,values:[0,.5,.5,1]},node_heights:{p1:.5,p2:.7},contours:[{level:.5,paths:[[[0,1],[1,0]]]}]};
function harness(api=async()=>terrain){
  const listeners={},updates=[],stages=[],calls=[];
  let zoom=1;
  const root={dataset:{landscapeResult:'result-a'},set outerHTML(value){updates.push(value);},querySelector(selector){return selector==='[data-map-container]'?{set outerHTML(value){stages.push(value);}}:{textContent:''};}};
  const sandbox={window:{__ATLAS_UI_TEST__:true},document:{addEventListener(type,fn){listeners[type]=fn;},querySelector(selector){return selector==='.landscape-root'?root:null;}},Promise,Map,Set};
  vm.createContext(sandbox);vm.runInContext(renderer,sandbox);vm.runInContext(code,sandbox);
  const escape=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
  const result={id:'result-a',meta:{topic_model:'nmf'},map:{nodes:[{id:'p1',label:'<img onerror="x">',x:.3,y:.4,year:2024,citations:1,topic_id:'t1'},{id:'p2',label:'Paper 2',x:.6,y:.7,year:2025,topic_id:'t1'}],edges:[{source:'p1',target:'p2'}],truncated:true},papers:[{id:'p1',citations:1},{id:'p2',citations:null}],topics:[{id:'t1',label:'Steel'}]};
  const context={result,large:true,view:'technology',topic:'t1',get mapZoom(){return zoom;},mapHighlight:{ids:['p1']},e:escape,num:String,icon:()=>'',topicColor:()=> '#58e0c5',mapLabels:groups=>groups.map(g=>`<g class="map-topic-label" data-topic="${g.id}" role="button" tabindex="0"/>`).join(''),isUnclassified:()=>false,empty:()=>'<p>empty</p>',representation:'NMF',api:url=>{calls.push(url);return api(url);}};
  return {sandbox,context,root,listeners,updates,stages,calls,setZoom:value=>{zoom=value;},ui:sandbox.window.AtlasLandscape,internals:sandbox.window.__landscapeTest};
}

test('saved maps fetch only terrain once and keep papers and field controls selectable',async()=>{
  const h=harness(),before=JSON.stringify(h.context.result);
  const pending=h.ui.view(h.context);assert.match(pending,/計算しています/);
  h.ui.view(h.context);await tick();
  assert.deepEqual(h.calls,['/api/results/result-a/terrain']);
  const html=h.ui.view(h.context);
  assert.match(html,/class="map-terrain/);assert.match(html,/data-map-mode="relief"/);
  assert.match(html,/data-paper="p1" tabindex="0" role="button"/);
  assert.match(html,/data-topic="t1" role="button" tabindex="0"/);
  assert.match(html,/map-highlight-halo/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img/);
  assert.match(html,/抽出した論文だけ/);assert.equal(JSON.stringify(h.context.result),before);
});

test('late terrain cannot overwrite a different result',async()=>{
  const pending=new Map();
  const h=harness(url=>new Promise(resolve=>pending.set(url,resolve)));
  h.ui.view(h.context);await tick();
  const next={...h.context,result:{...h.context.result,id:'result-b'}};
  h.root.dataset.landscapeResult='result-b';h.ui.view(next);await tick();
  pending.get('/api/results/result-a/terrain')(terrain);await tick();assert.equal(h.updates.length,0);
  pending.get('/api/results/result-b/terrain')(terrain);await tick();
  assert.equal(h.updates.length,1);assert.match(h.updates[0],/data-landscape-result="result-b"/);
});

test('failed terrain preserves flat point map and retries only on request',async()=>{
  let fail=true;const h=harness(async()=>{if(fail)throw new Error('<bad terrain>');return terrain;});
  h.ui.view(h.context);await tick();
  const html=h.ui.view(h.context);assert.match(html,/data-map-mode="flat"/);assert.match(html,/data-paper="p1"/);assert.match(html,/&lt;bad terrain>/);assert.match(html,/data-landscape-retry/);
  assert.equal(h.calls.length,1);fail=false;h.internals.fetchTerrain(h.context,true);await tick();
  assert.equal(h.calls.length,2);assert.match(h.ui.view(h.context),/data-map-mode="relief"/);
});

test('height adjustment preserves zoom and does not replace the active slider or run analysis',async()=>{
  const h=harness();h.ui.view(h.context);await tick();h.updates.length=0;h.setZoom(1.8);
  h.listeners.input({target:{id:'landscape-height',value:'100'}});
  assert.equal(h.updates.length,0);assert.equal(h.stages.length,1);assert.match(h.stages[0],/scale\(1.8\)/);
  assert.equal(h.calls.length,1);assert.equal(h.internals.preferences.height,100);
});

test('flat and relief node positions match the surface projection and contours can be hidden',async()=>{
  const h=harness();h.ui.view(h.context);await tick();
  for(const mode of ['flat','relief']){
    h.internals.preferences.mode=mode;
    const html=h.ui.view(h.context),match=html.match(/class="map-node" cx="([^"]+)" cy="([^"]+)"[^>]+data-paper="p1"/);
    const expected=h.sandbox.window.AtlasTerrain.project(.3,.4,.5,{width:800,height:500,pad:48,mode,heightScale:.65*404*.30});
    assert.deepEqual([Number(match[1]),Number(match[2])],Array.from(expected));
  }
  h.internals.preferences.contours=false;const html=h.ui.view(h.context);
  assert.doesNotMatch(html,/class="terrain-contour(?: |")/);assert.match(html,/terrain-surface/);
});

test('failed thinking completion labels numerical fallback separately and escapes details',()=>{
  const h=harness(),message='思考部分（<think>）が閉じられず、最終回答を確認できませんでした。';
  h.internals.report.data={id:'report-a',requested_provider:'local',generation_status:'failed',llm_error:message,narrative:{mode:'deterministic',headline:'計算された説明',sections:[{title:'数値',text:'重心が変化しました。',evidence_ids:['p1']}],validation:{status:'warning',warnings:[{code:'llm_failed',message}]}}};
  const html=h.internals.reportHTML(h.context);
  assert.match(html,/LLM評論の生成に失敗しました/);
  assert.match(html,/<details class="landscape-report-fallback"><summary>計算結果の説明（代替表示）/);
  assert.match(html,/&lt;think>/);assert.doesNotMatch(html,/<think>/);
  assert.equal((html.match(/思考部分/g)||[]).length,1);
  assert.doesNotMatch(html,/照合に注意が必要です/);
  assert.match(html,/data-paper="p1"/);assert.match(html,/report-a\/export/);
});

test('numeric mismatch keeps successful LLM critique expanded with ordinary warning',()=>{
  const h=harness();
  h.internals.report.data={generation_status:'generated',narrative:{mode:'local_llm',headline:'論文の比較',sections:[{title:'結果',text:'強度は900 GPaです。',validation:{status:'warning'}}],validation:{status:'warning',warnings:[{code:'numeric_mismatch',message:'単位の照合に失敗しました。'}]}}};
  const html=h.internals.reportHTML(h.context);
  assert.match(html,/照合に注意が必要です/);assert.match(html,/LOCAL LLM/);assert.match(html,/900 GPa/);
  assert.doesNotMatch(html,/LLM評論の生成に失敗|<details|代替表示/);
});

test('legacy saved failure receives explicit fallback label while selected calculation does not',()=>{
  const h=harness(),narrative={mode:'deterministic',headline:'数値の説明',sections:[]};
  h.internals.report.data={llm_error:'過去の生成失敗',narrative};
  assert.match(h.internals.reportHTML(h.context),/LLM評論の生成に失敗しました/);
  h.internals.report.data={requested_provider:'none',generation_status:'not_requested',narrative};
  const html=h.internals.reportHTML(h.context);
  assert.match(html,/CALCULATED OBSERVATIONS/);
  assert.doesNotMatch(html,/失敗|代替表示|<details/);
});
