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
  assert.match(html,/<section class="landscape-report-fallback" aria-label="計算結果の説明（代替表示）">/);
  assert.match(html,/重心が変化しました/);assert.doesNotMatch(html,/<details/);
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
  assert.ok(html.indexOf('900 GPa')<html.indexOf('照合に注意が必要です'), 'the answer is visible before potentially long warning details');
  assert.doesNotMatch(html,/LLM評論の生成に失敗|<details|代替表示/);
});

test('a saved failure flag never hides or relabels an actual LLM answer as a calculated fallback',()=>{
  const h=harness(),text='前期の抄録では電極構造を比較し、後期の抄録では耐久性の検証を行っています。';
  h.internals.report.data={requested_provider:'local',generation_status:'failed',llm_error:'後処理に失敗しました。',narrative:{mode:'local_llm',headline:'抄録から読む変化',sections:[{title:'対象と手法',text,evidence_ids:['p1','p2']}],validation:{status:'warning',warnings:[{code:'llm_failed',message:'後処理に失敗しました。'}]}}};
  const html=h.internals.reportHTML(h.context);
  assert.match(html,/LOCAL LLM · 要確認/);assert.ok(html.includes(text));
  assert.ok(html.indexOf(text)<html.indexOf('LLM評論の生成に失敗しました'));
  assert.match(html,/取得できた回答本文は上に表示しています/);
  assert.doesNotMatch(html,/<details|CALCULATED OBSERVATIONS|代替表示/);
  assert.equal((html.match(/後処理に失敗しました/g)||[]).length,1);
  assert.match(html,/data-paper="p1"/);assert.match(html,/data-paper="p2"/);
});

test('an empty LLM completion is explicitly incomplete instead of looking like a generated answer',()=>{
  const h=harness();
  for(const sections of [[],[{title:'本文のない見出し',text:'  ',evidence_ids:['p1']}]]){
    h.internals.report.data={requested_provider:'openai',generation_status:'generated',narrative:{mode:'openai',headline:'回答の見出しだけ',sections,caveats:['注意事項だけ'],validation:{status:'warning',warnings:[{code:'uncited_section',message:'参照先を確認してください。'}]}}};
    const html=h.internals.reportHTML(h.context);
    assert.match(html,/LLMの回答本文が空です/);assert.match(html,/LLMによる評論は表示できていません/);
    assert.match(html,/参照先を確認してください/);assert.doesNotMatch(html,/class="landscape-generated"|回答の見出しだけ|本文のない見出し/);
  }
});

test('section-only validation warnings leave the answer expanded and surface escaped warning details',()=>{
  const h=harness();
  h.internals.report.data={generation_status:'generated',narrative:{mode:'openai',headline:'比較',sections:[{title:'結果',text:'<script>unsafe</script> 抄録の比較内容。',validation:{status:'warning',warnings:[{message:'<img src=x>数値に要確認'}]}}]}};
  const html=h.internals.reportHTML(h.context);
  assert.match(html,/照合に注意が必要です/);assert.match(html,/&lt;script>/);assert.match(html,/&lt;img/);
  assert.doesNotMatch(html,/<script>|<img|<details|代替表示/);
  assert.ok(html.indexOf('抄録の比較内容')<html.indexOf('数値に要確認'));
});

test('failed generation without a narrative does not promise a nonexistent fallback',()=>{
  const h=harness();h.internals.report.data={requested_provider:'local',generation_status:'failed',llm_error:'接続に失敗しました。'};
  const html=h.internals.reportHTML(h.context);
  assert.match(html,/LLM評論の生成に失敗しました/);assert.match(html,/接続に失敗しました/);
  assert.doesNotMatch(html,/下の代替表示|class="landscape-generated"/);
});

test('submitted abstract coverage and exact input excerpts are inspectable without covering the answer',()=>{
  const h=harness();
  h.internals.report.data={requested_provider:'local',generation_status:'generated',input_summary:{paper_count:3,abstract_count:2,missing_abstract_count:1,truncated_abstract_count:1,before:{paper_count:2,abstract_count:1},after:{paper_count:1,abstract_count:1}},evidence_papers:[{id:'p1',title:'<img onerror="x"> 前期の論文',year:2024,abstract:'Actual <script>abstract</script> sent to the model.',abstract_original_chars:8000,abstract_sent_chars:2400,abstract_truncated:true},{id:'p2',title:'後期の論文',year:2025,abstract:'Observed electrode durability.'},{id:'p3',title:'抄録のない論文',abstract:''}],narrative:{mode:'local_llm',headline:'抄録の比較',sections:[{title:'解釈',text:'後期は耐久性に焦点を移しています。',evidence_ids:['p2']}]}};
  const html=h.internals.reportHTML(h.context);
  assert.match(html,/抄録あり 2 \/ 3論文（前期 1 \/ 2論文 · 後期 1 \/ 1論文）/);
  assert.match(html,/抄録なし 1論文 · 長さを調整した抄録 1件/);
  assert.match(html,/<details><summary>準備した論文・抄録を確認/);assert.doesNotMatch(html,/<details open/);
  assert.match(html,/入力 2400文字 \/ 元の抄録 8000文字（長さを調整）/);
  assert.match(html,/Actual &lt;script>abstract/);assert.match(html,/Observed electrode durability/);assert.match(html,/抄録は取得されていません/);
  assert.doesNotMatch(html,/<img|<script>/);
  assert.ok(html.indexOf('</details>')<html.indexOf('後期は耐久性に焦点を移しています'), 'the answer is outside the collapsed input details');
});

test('unknown generated evidence IDs are displayed only as warnings and never as paper links',()=>{
  const h=harness(),unknown='not-provided" onclick="bad';
  h.internals.report.data={generation_status:'generated',narrative:{mode:'openai',headline:'比較',sections:[{title:'解釈',text:'返された分析の本文を保持します。',evidence_ids:['p1'],unverified_evidence_ids:[unknown],validation:{status:'warning'}}],validation:{status:'warning',warnings:[{code:'unknown_evidence_id',message:'入力資料にない論文IDがあります。'}]}}};
  const html=h.internals.reportHTML(h.context);
  assert.match(html,/返された分析の本文を保持します/);assert.match(html,/照合できない論文ID：not-provided&quot; onclick=&quot;bad/);
  assert.match(html,/data-paper="p1"/);assert.doesNotMatch(html,/data-paper="not-provided| onclick="bad|代替表示/);
});

test('short citation labels match source periods and preserve prose and excerpt line breaks',()=>{
  const h=harness();
  h.internals.report.data={generation_status:'generated',input_summary:{paper_count:3,abstract_count:3,missing_abstract_count:0,truncated_abstract_count:1},evidence_papers:[{id:'p1',citation_id:'B1',side:'before',title:'Prior methods',abstract:'First paragraph.\n\nLast paragraph.',abstract_truncated:true,abstract_original_chars:9000,abstract_sent_chars:4000,excerpt_strategy:'head_and_tail'},{id:'p2',citation_id:'A1',side:'after',title:'Recent methods',abstract:'Later abstract.'},{id:'p3',citation_id:'P1',side:'centroid',title:'Representative study',abstract:'Representative abstract.'}],narrative:{mode:'local_llm',headline:'本文で比較',sections:[{title:'研究対象',text:'B1では材料を比較。\n\nA1では耐久性を比較。',evidence_ids:['p1','p2']}],caveats:['注意事項。\n追加の留意点。']}};
  const html=h.internals.reportHTML(h.context);
  for(const label of ['B1 · 前期 · Prior methods','A1 · 後期 · Recent methods','P1 · 選択期間 · Representative study'])assert.ok(html.includes(label));
  assert.match(html,/data-paper="p1" title="B1 · 前期 · Prior methods">B1 · 前期 · Prior methods/);
  assert.match(html,/入力 4000文字 \/ 元の抄録 9000文字（冒頭・末尾の抜粋）/);
  assert.match(html,/<p style="white-space:pre-wrap">First paragraph\.\n\nLast paragraph\.<\/p>/);
  assert.match(html,/<p style="white-space:pre-wrap">B1では材料を比較。\n\nA1では耐久性を比較。<\/p>/);
  assert.match(html,/<p style="white-space:pre-wrap">注意事項。\n追加の留意点。<\/p>/);
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

test('input capacity audit shows actual request excerpts and preserves prepared source separately',()=>{
  const h=harness();
  h.internals.report.data={input_summary:{paper_count:12,abstract_count:12},evidence_papers:[{id:'original',abstract:'OMITTED SECRET EXCERPT'}],llm_input:{status:'completed',context_window:4096,context_source:'server',input_tokens_estimate:2100,output_tokens:1365,requested_output_tokens:4000,safety_tokens:512,reduced:true,retries:1,input_summary:{paper_count:1,abstract_count:1,missing_abstract_count:0},papers:[{id:'sent',title:'<sent>',abstract:'ACTUAL EXCERPT'}],omitted_paper_ids:['original']}};
  const html=h.internals.reportHTML(h.context);
  assert.match(html,/実際に送信した入力資料/);assert.match(html,/ACTUAL EXCERPT/);
  assert.doesNotMatch(html,/OMITTED SECRET EXCERPT/);assert.match(html,/&lt;sent>/);
  assert.match(html,/4096/);assert.match(html,/2100/);assert.match(html,/1365/);assert.match(html,/縮小再試行 1回/);
  h.internals.report.data.llm_input.status='failed';h.internals.report.data.llm_input.request_attempts=0;
  const failed=h.internals.reportHTML(h.context);
  assert.match(failed,/送信前のリクエスト候補/);assert.doesNotMatch(failed,/実際に送信した/);
});

test('foresight audit renders extraction and critique independently with escaped labels',()=>{
  const h=harness(),audit={context_window:4096,context_source:'<bad>',input_tokens_estimate:1000,output_tokens:512,safety_tokens:512,status:'completed'};
  const html=h.ui.renderInputBudget({extraction:audit,critique:{...audit,reduced:true}},h.context.e,String);
  assert.match(html,/根拠抽出/);assert.match(html,/評論生成/);assert.match(html,/抜粋・選択/);assert.doesNotMatch(html,/<bad>/);
});
