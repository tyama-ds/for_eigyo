import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const files=['map-terrain.js','map-layers.js','landscape.js'].map(name=>readFileSync(new URL('../static/'+name,import.meta.url),'utf8'));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const terrain={version:1,grid:{width:2,height:2,values:[0,.5,.5,1]},node_heights:{p1:.4,p2:.8},contours:[]};
const payload={result_id:'r1',projection_id:'shared-pca',map:{method:'PCA',nodes:[{id:'p1',label:'Steel <img onerror="x">',x:.2,y:.3,year:2024,topic_id:'t1',period_id:'2024'},{id:'p2',label:'Steel 2',x:.7,y:.6,year:2025,topic_id:'t1',period_id:'2025'}],edges:[]},terrain,topics:[{id:'t1',label:'Steel'}],periods:[{id:'2024',label:'2024年',count:1,node_ids:['p1']},{id:'2025',label:'2025年',count:1,node_ids:['p2']}],centroids:[{topic_id:'t1',period_id:'2024',count:1,x:.2,y:.3},{topic_id:'t1',period_id:'2025',count:1,x:.7,y:.6}],movements:[{id:'move1',topic_id:'t1',from_period:'2024',to_period:'2025',from:{x:.2,y:.3},to:{x:.7,y:.6},distance_2d:.583,cosine_distance:.2,q_value:.03,status:'shift',from_count:5,to_count:6,from_terms:[{term:'steel'}],to_terms:[{term:'phase'}],explanation:'話題の構成が変わった可能性。'}],warnings:[],interpretation:{summary:'共通座標で比較。'}};
function harness(api=async()=>structuredClone(payload)){
  const listeners={},updates=[],calls=[],frames=[];
  const stage={isConnected:true,querySelectorAll:()=>[]};
  const root={dataset:{landscapeResult:'r1'},set outerHTML(html){updates.push(html);},querySelector:()=>stage};
  const doc={addEventListener(type,fn){listeners[type]=fn;},querySelector:selector=>selector==='.landscape-root'?root:null};
  const window={__ATLAS_UI_TEST__:true,matchMedia:()=>({matches:false}),requestAnimationFrame:cb=>frames.push(cb)};
  const sandbox={window,document:doc,Promise,Map,Set,setTimeout};vm.createContext(sandbox);files.forEach(code=>vm.runInContext(code,sandbox));
  const result={id:'r1',meta:{topic_model:'nmf'},topics:[{id:'t1',label:'Steel'}],papers:[{id:'p1'},{id:'p2'}],map:structuredClone(payload.map)};
  const context={result,large:true,view:'technology',topic:'t1',mapZoom:1.4,e:escape,num:String,icon:()=>'',topicColor:()=> '#58e0c5',mapLabels:groups=>groups.map(g=>`<g data-topic="${escape(g.id)}" role="button" tabindex="0"/>`).join(''),isUnclassified:()=>false,empty:()=>'',representation:'NMF',api:(url,options)=>{calls.push({url,options});return api(url,options);}};
  return {window,sandbox,context,listeners,updates,calls,root,frames,layers:window.AtlasLayers,ui:window.AtlasLandscape,internals:window.__landscapeTest};
}
test('time planes preserve common XY; moving between planes cannot fabricate a drift arrow',()=>{
  const {layers}=harness(),opts={mode:'layers',layerCount:8,layerIndex:0};
  const same={status:'stable',from:{x:.4,y:.5},to:{x:.4,y:.5}};
  assert.equal(layers.arrowGeometry(same,opts),null);
  const shift={status:'shift',from:{x:.1,y:.3},to:{x:.8,y:.6}};
  const a=layers.arrowGeometry(shift,opts),b=layers.arrowGeometry(shift,{...opts,layerIndex:7});
  assert.equal(a.distance,b.distance);
  assert.ok(Math.abs((a.to[0]-a.from[0])-(b.to[0]-b.from[0]))<1e-8);
  assert.ok(Math.abs((a.to[1]-a.from[1])-(b.to[1]-b.from[1]))<1e-8);
  assert.equal(layers.arrowGeometry({...shift,status:'insufficient'},opts).distance,a.distance);
  assert.equal(layers.arrowGeometry({...shift,status:'insufficient',insufficient_reason:'unclassified_topic'},opts),null);
  assert.equal(layers.arrowGeometry({...shift,from:{x:null,y:.3}},opts),null);
  assert.equal(layers.arrowGeometry({...shift,to:{x:Infinity,y:.3}},opts),null);
  assert.equal(layers.arrowGeometry({...shift,from:null},opts),null);
});
test('long time series retain empty periods and window navigation is bounded to eight planes',()=>{
  const {layers}=harness(),periods=Array.from({length:600},(_,i)=>({id:String(i),count:0}));
  const last=layers.periodWindow(periods),first=layers.periodWindow(periods,-100),overflow=layers.periodWindow(periods,10000);
  assert.equal(last.start,592);assert.equal(last.periods.length,8);assert.equal(last.periods[0].count,0);
  assert.equal(first.start,0);assert.equal(overflow.start,592);
});
test('animated interpolation actually moves coordinates and uses stable path topology',()=>{
  const {layers}=harness();
  assert.equal(layers.interpolateAttribute('translate(10 20)','translate(110 220)',.5),'translate(60 120)');
  assert.equal(layers.interpolateAttribute('M0,0L10,10','M50,20L90,80',.5),'M25,10L50,45');
  assert.equal(layers.interpolateAttribute('M0 0L1 1','M1 1L2 2L3 3',.5),'M1 1L2 2L3 3');
});
test('reduced motion skips coordinate animation',()=>{
  const h=harness();h.window.matchMedia=()=>({matches:true});
  h.layers.animate({querySelectorAll:()=>[]},{querySelectorAll:()=>[],isConnected:true});assert.equal(h.frames.length,0);
});
test('shared projection view keeps result immutable and provides keyboard accessible evidence, topics, and arrows',async()=>{
  const h=harness(),before=JSON.stringify(h.context.result);h.ui.view(h.context);await tick();h.internals.preferences.mode='layers';
  const html=h.ui.view(h.context);
  assert.match(html,/data-map-mode="layers"/);assert.match(html,/data-paper="p1" tabindex="0" role="button"/);
  assert.match(html,/data-topic="t1" role="button" tabindex="0"/);assert.match(html,/data-landscape-movement="move1" role="button" tabindex="0"/);
  assert.match(html,/centroid-red-arrow/);assert.match(html,/centroid-pulse/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img/);
  assert.match(html,/同一平面/);assert.match(html,/表示対象 2論文/);assert.match(html,/scale\(1.4\)/);
  assert.equal(JSON.stringify(h.context.result),before);assert.equal(h.calls.length,1);
});
test('projection and interval requests are captured and stale replies cannot replace current selection',async()=>{
  const waiting=new Map(),h=harness(url=>new Promise(resolve=>waiting.set(url,resolve)));
  h.ui.view(h.context);
  h.listeners.change({target:{id:'landscape-projection',value:'pca'}});
  h.listeners.change({target:{id:'landscape-interval',value:'month'}});
  await tick();assert.equal(h.calls.length,3);
  assert.match(h.calls[0].url,/projection=auto&interval=year/);assert.match(h.calls[1].url,/projection=pca&interval=year/);assert.match(h.calls[2].url,/projection=pca&interval=month/);
  h.updates.length=0;waiting.get(h.calls[0].url)(structuredClone(payload));await tick();assert.equal(h.updates.length,0);
  waiting.get(h.calls[2].url)(structuredClone(payload));await tick();assert.equal(h.updates.length,1);
});
test('missing UMAP or projection failure leaves papers accessible and explains error',async()=>{
  const h=harness(async()=>{throw new Error('UMAPを追加してください。');});h.ui.view(h.context);await tick();
  const html=h.ui.view(h.context);assert.match(html,/UMAPを追加/);assert.match(html,/data-landscape-retry/);assert.match(html,/data-paper="p1"/);
});
test('comparison evidence keeps both periods visible when each has six source papers',async()=>{
  const data=structuredClone(payload);data.movements[0].evidence_before=Array.from({length:6},(_,i)=>`before-${i}`);data.movements[0].evidence_after=Array.from({length:6},(_,i)=>`after-${i}`);
  const h=harness(async()=>data);h.ui.view(h.context);await tick();const html=h.ui.view(h.context);
  for(const side of ['before','after']){for(let i=0;i<3;i++)assert.match(html,new RegExp(`data-paper="${side}-${i}"`));assert.doesNotMatch(html,new RegExp(`data-paper="${side}-3"`));}
  assert.match(html,/前期の根拠 1/);assert.match(html,/後期の根拠 1/);
  assert.match(html,/投影前の文章表現の変化/);assert.doesNotMatch(html,/元の文章表現/);
});
test('insufficient comparisons retain geometric arrows as dashed paths with gap and status labels',async()=>{
  const data=structuredClone(payload);Object.assign(data.movements[0],{status:'insufficient',insufficient_reason:'small_sample',gap_periods:2});
  const h=harness(async()=>data);h.ui.view(h.context);await tick();h.internals.preferences.mode='layers';const html=h.ui.view(h.context);
  assert.match(html,/class="centroid-arrow centroid-arrow-unconfirmed/);
  assert.match(html,/aria-label="2024から2025の重心移動。比較データ不足 \/ 2期間の空白あり"/);
  assert.match(html,/破線 = 内容変化は未確認・比較データ不足/);
  assert.match(html,/marker-end="url\(#centroid-red-arrow\)"/);
});
test('report keeps unverified narrative visible with an alert and uses browser-wrapped api',async()=>{
  const h=harness(async(url,options)=>{
    if(url==='/api/landscape-reports'){const body=JSON.parse(options.body);assert.equal(body.movement_id,'move1');assert.equal(body.provider,'local');assert.equal(body.connection,undefined);return {job_id:'j1'};}
    if(url==='/api/jobs/j1')return {status:'completed',landscape_report_id:'rep1'};
    if(url==='/api/landscape-reports/rep1')return {result_id:'r1',projection_id:'shared-pca',interval:'year',movement:{id:'move1'},narrative:{mode:'local',headline:'未検証の評論',sections:[{title:'仮説',text:'可能性を検討します。'}],validation:{status:'warning',warnings:[{code:'numeric_mismatch',message:'数値照合に失敗'}]}}};
    return structuredClone(payload);
  });h.ui.view(h.context);await tick();h.internals.preferences.provider='local';await h.internals.generateReport();
  const html=h.ui.view(h.context);assert.match(html,/未検証の評論/);assert.match(html,/role="alert"/);assert.match(html,/数値照合に失敗/);assert.equal(h.internals.report.busy,false);
});
test('late LLM reply for a previous interval is ignored',async()=>{
  let finish;const h=harness(async(url)=>{
    if(url==='/api/landscape-reports')return {job_id:'j1'};
    if(url==='/api/jobs/j1')return new Promise(resolve=>{finish=resolve;});
    return structuredClone(payload);
  });h.ui.view(h.context);await tick();const request=h.internals.generateReport();await tick();
  h.listeners.change({target:{id:'landscape-interval',value:'month'}});finish({status:'completed',landscape_report_id:'old'});await request;
  assert.equal(h.internals.report.data,null);assert.equal(h.calls.some(c=>c.url==='/api/landscape-reports/old'),false);
});

test('scope is explicit in requests, cache identity and counts distinguish full population from drawn papers',async()=>{
  const data=structuredClone(payload);data.scope='full';data.meta={analysis_papers:250000,map_displayed_papers:2};data.centroids[1].count=150000;
  const h=harness(async()=>data);h.ui.view(h.context);await tick();h.listeners.change({target:{id:'landscape-scope',value:'full'}});await tick();
  const html=h.ui.view(h.context);assert.match(h.calls.at(-1).url,/scope=full/);assert.equal(h.calls.length,2);
  assert.match(html,/簡易版/);assert.match(html,/全件対応版/);assert.match(html,/250000論文の内容・重心/);assert.match(html,/150000論文の重心/);assert.match(html,/表示対象 2論文/);
});

test('topic title selection switches centroid analysis and preserves a separate field report action',async()=>{
  const data=structuredClone(payload);data.topics.push({id:'t2',label:'Welding'});data.centroids.push({topic_id:'t2',period_id:'2025',count:77,x:.4,y:.5});
  const h=harness(async()=>data),selected=[];h.context.setTopic=id=>selected.push(id);h.ui.view(h.context);await tick();h.ui.selectTopic('t2');
  assert.deepEqual(selected,['t2']);assert.equal(h.internals.selectedCentroid(data).topic_id,'t2');const html=h.ui.view(h.context);
  assert.match(html,/Welding · 重心の分析/);assert.match(html,/data-detail-topic="t2"/);assert.match(html,/重心付近の代表論文を分析/);
});

test('centroid report requests the selected period, topic and scope with the same projection identity',async()=>{
  const h=harness(async(url,options)=>{
    if(url==='/api/landscape-reports'){const body=JSON.parse(options.body);assert.equal(body.kind,'centroid');assert.equal(body.topic_id,'t1');assert.equal(body.period_id,'2024');assert.equal(body.scope,'sample');assert.equal(body.movement_id,undefined);return {job_id:'centroid-job'};}
    if(url==='/api/jobs/centroid-job')return {status:'completed',landscape_report_id:'center-report'};
    if(url==='/api/landscape-reports/center-report')return {kind:'centroid',scope:'sample',result_id:'r1',projection_id:'shared-pca',interval:'year',centroid:{topic_id:'t1',period_id:'2024'},narrative:{headline:'代表論文の分析',sections:[]}};
    return structuredClone(payload);
  });h.ui.view(h.context);await tick();h.internals.selectCentroid('t1','2024');await h.internals.generateReport('centroid');assert.equal(h.internals.report.data?.kind,'centroid');assert.equal(h.internals.report.error,'');
});

test('orbit updates existing coordinates once per frame and preserves click versus drag behavior',()=>{
  const h=harness(),prefs={mode:'relief',height:65,topic:'t1',contours:true,camera:{yaw:-.16,pitch:.72}};
  const html=h.layers.render(h.context,payload,prefs),tag=html.match(/<circle class="map-node"[^>]+>/)[0],index=tag.match(/data-orbit-index="(\d+)"/)[1];
  const attributes={cx:tag.match(/cx="([^"]+)"/)[1],cy:tag.match(/cy="([^"]+)"/)[1]},before={...attributes},listeners={},captures=[];
  const el={dataset:{orbitIndex:index},setAttribute:(key,value)=>{attributes[key]=value;}};
  const svg={querySelectorAll:()=>[el],addEventListener:(type,fn)=>{listeners[type]=fn;},setPointerCapture:id=>captures.push(id),hasPointerCapture:()=>true,releasePointerCapture:()=>{}};
  const stage={dataset:{mapMode:'relief'},querySelector:()=>svg},updates=[],orbit=h.layers.bindOrbit(stage,value=>updates.push(value));
  const event=(x,y)=>({button:0,pointerId:4,clientX:x,clientY:y,preventDefault(){},stopImmediatePropagation(){this.stopped=true;}});
  listeners.pointerdown(event(10,10));listeners.pointerup(event(10,10));let click=event(10,10);listeners.click(click);assert.equal(click.stopped,undefined);assert.equal(h.frames.length,0);
  listeners.pointerdown(event(10,10));listeners.pointermove(event(30,25));listeners.pointermove(event(60,50));assert.equal(h.frames.length,1);assert.deepEqual(attributes,before);h.frames.shift()(100);assert.notDeepEqual(attributes,before);assert.deepEqual(captures,[4]);assert.equal(updates.length,1);
  listeners.pointerup(event(60,50));click=event(60,50);listeners.click(click);assert.equal(click.stopped,true);
  assert.equal(listeners.wheel,undefined);assert.ok(orbit.camera.pitch>=.25&&orbit.camera.pitch<=1.3);
  const capped=h.layers.camera({yaw:900,pitch:-100});assert.equal(capped.yaw,1.25);assert.equal(capped.pitch,.25);
});

test('map selection refreshes the surrounding selected-technology panel as well as map preferences',()=>{
  const appSource=readFileSync(new URL('../static/app.js',import.meta.url),'utf8'),line=appSource.split('\n').find(row=>row.startsWith('function setMapTopic(')),aside={},cards=[{dataset:{topic:'old'}},{dataset:{topic:'new'}}];
  for(const card of cards)card.classList={toggle:(_name,value)=>{card.selected=value;}};
  const state={view:'technology'},ctx=vm.createContext({state,$:()=>aside,$$:()=>cards,topicFor:id=>id==='new'?{id,label:'New field'}:null,topicDetail:t=>t.label});vm.runInContext(line,ctx);ctx.setMapTopic('new');assert.equal(state.topic,'new');assert.equal(aside.outerHTML,'New field');assert.equal(cards[0].selected,false);assert.equal(cards[1].selected,true);
});

test('orbit terrain bounds drawing work and never bridges invalid contour segments',()=>{
  const h=harness(),data=structuredClone(payload);data.terrain.grid={width:49,height:33,values:Array(49*33).fill(.5)};data.terrain.contours=[{level:.5,paths:[[[0,0],[.2,.2],null,[.8,.8],[1,1]]]}];
  const html=h.layers.render(h.context,data,{mode:'relief',height:65,topic:'t1',contours:true});
  assert.ok((html.match(/data-orbit-index/g)||[]).length<850);assert.equal((html.match(/class="terrain-contour"/g)||[]).length,2);assert.doesNotMatch(html,/NaN|Infinity/);
});

test('full-scope test labels distinguish all valid rows, a bounded subset, and an untested comparison',()=>{
  const h=harness(),meta={meta:{test_scope:'bounded_permutation_sample'}},movement={from_count:100,to_count:90,from_valid_count:95,to_valid_count:85,test_from_count:95,test_to_count:85,p_value:.02};
  let note=h.internals.testScopeNote(h.context,meta,movement);assert.match(note,/有効論文をすべて使用/);assert.doesNotMatch(note,/抽出した標本/);
  note=h.internals.testScopeNote(h.context,meta,{...movement,from_count:500,from_valid_count:490,test_from_count:200});assert.match(note,/抽出した標本を使用/);assert.match(note,/前期200件・後期85件/);
  note=h.internals.testScopeNote(h.context,meta,{...movement,p_value:null});assert.match(note,/置換検定を実施していません/);
  const appSource=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');assert.match(appSource,/描画用 \$\{num\(state\.result\.map\.nodes\.length\)\}論文/);
});

test('orbit binds the main map SVG when zoom-tool icon SVGs precede it',()=>{
  const h=harness(),mapListeners={},iconListeners={},selectors=[],icon={addEventListener:(type,fn)=>{iconListeners[type]=fn;}},map={querySelectorAll:()=>[],addEventListener:(type,fn)=>{mapListeners[type]=fn;}};
  const stage={dataset:{mapMode:'layers'},querySelector:selector=>{selectors.push(selector);return selector==='svg.map-svg'?map:icon;}};
  const orbit=h.layers.bindOrbit(stage);assert.ok(orbit);assert.deepEqual(selectors,['svg.map-svg']);assert.equal(Object.keys(iconListeners).length,0);assert.equal(typeof mapListeners.pointerdown,'function');
  let prevented=false,stopped=false;mapListeners.pointerdown({button:0,pointerId:1,clientX:0,clientY:0,preventDefault(){prevented=true;}});mapListeners.pointerup({pointerId:1});mapListeners.click({preventDefault(){},stopImmediatePropagation(){stopped=true;}});
  assert.equal(prevented,true);assert.equal(stopped,false);
});
