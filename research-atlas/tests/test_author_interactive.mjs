import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/author-network.js',import.meta.url),'utf8');
const app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const index=readFileSync(new URL('../static/index.html',import.meta.url),'utf8');
const node=(id,label,x,metrics,neighbors,cluster='u1')=>({id,label,x,y:.4,count:10,cluster_id:cluster,metrics,neighbor_ids:neighbors,affiliations:[{name:cluster==='u1'?'大学 A':'大学 B'}]});
function network(){return {id:'net1',result_id:'r1',group_by:'institution',nodes:[
  node('a','Alice',.2,{degree:2,strength:4,betweenness:.6,pagerank:.4,local_clustering:0,institution_bridge:1},['b','d']),
  node('b','Bob',.4,{degree:2,strength:3,betweenness:.1,pagerank:.3,local_clustering:1,institution_bridge:1},['a','c']),
  node('c','Carol',.6,{degree:1,strength:1,betweenness:.1,pagerank:.2,local_clustering:1,institution_bridge:0},['b'],'u2'),
  node('d','Dan',.8,{degree:1,strength:2,betweenness:0,pagerank:.1,local_clustering:0,institution_bridge:null},['a'],'u2'),
],edges:[{source:'a',target:'b',weight:2}],export_data:{edges:[{source:'a',target:'b',weight:2},{source:'b',target:'c',weight:1},{source:'a',target:'d',weight:2}]},metric_definitions:{betweenness:{label:'媒介中心性',description:'非加重の最短経路から計算します。',direction:'descending'}},metrics_scope:{node_count:4,edge_count:3}};}
const clusters=[{id:'u1',label:'大学 A',color:'#58e0c5'},{id:'u2',label:'大学 B',color:'#aebced'}];
function element(dataset={}){return {dataset,attributes:new Map(),focusCount:0,setAttribute(k,v){this.attributes.set(k,String(v));},focus(){this.focusCount++;},closest(selector){if(selector.includes('[data-an-node]')&&this.dataset.anNode)return this;if(selector.includes('[data-an-select]')&&this.dataset.anSelect)return this;if(selector.includes('[data-an-action]')&&this.dataset.anAction)return this;if(selector==='.an-svg')return this.svg||null;return null;}};}
function event(target,overrides={}){return {target,button:0,pointerId:1,clientX:200,clientY:200,detail:1,prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;},...overrides};}
function harness(data=network()){
  const window={__ATLAS_UI_TEST__:true}, sandbox=vm.createContext({window,console,Map,Set});vm.runInContext(source,sandbox);
  const t=window.__authorInteractiveTest, api=window.AtlasAuthorNetwork, events=new Map(), selected=[],detail=[],captured=[],released=[];
  const svg=element();svg.svg=svg;svg.getBoundingClientRect=()=>({left:0,top:0,width:860,height:540});
  const viewport=element(),zoom={textContent:''},stage={outerHTML:''},selection={innerHTML:''},ranking={innerHTML:''};
  const nodes=data.nodes.map(n=>Object.assign(element({anNode:n.id}),{svg}));
  const edges=data.export_data.edges.map(e=>element({anSource:e.source,anTarget:e.target}));
  const clusterElements=clusters.map(c=>{const ellipse=element(),text=element();return Object.assign(element({anCluster:c.id}),{ellipse,text,querySelector:tag=>tag==='ellipse'?ellipse:text});});
  const controls=new Map([['#an-metric',element()],['#an-top',element()]]);
  const host={innerHTML:'',querySelector(selector){return ({'.an-svg':svg,'.an-viewport':viewport,'.an-zoom-value':zoom,'.an-graph-stage':stage,'.an-selection':selection,'.an-ranking':ranking})[selector]||controls.get(selector)||null;},querySelectorAll(selector){return ({'[data-an-node]':nodes,'[data-an-source]':edges,'[data-an-cluster]':clusterElements,'[data-an-select]':[]})[selector]||[];},addEventListener(type,handler){if(!events.has(type))events.set(type,new Set());events.get(type).add(handler);},removeEventListener(type,handler){events.get(type)?.delete(handler);},setPointerCapture:id=>captured.push(id),releasePointerCapture:id=>released.push(id),dispatch(type,e){for(const handler of events.get(type)||[])handler(e);}};
  const options={resultId:data.result_id,group:'institution',onSelect:id=>selected.push(id),onDetail:id=>detail.push(id)};
  host.innerHTML=api.render(data,data.nodes,clusters,options);api.mount(host);
  return {t,api,host,events,data,options,svg,viewport,zoom,stage,selection,ranking,nodes,edges,clusterElements,controls,selected,detail,captured,released};
}

test('institution grouping and isolated interaction assets are connected to the author view',()=>{
  assert.match(app,/groupBy:'institution'/);
  assert.match(app,/AtlasAuthorNetwork\?\.mount\(\$\('#author-map-container'\)\)/);
  assert.match(app,/function updateAuthorFilters\(\).*?AtlasAuthorNetwork\?\.unmount\(\)/);
  for(const method of ['render','showLoading','showError'])assert.match(app,new RegExp(`function ${method}\\([^\\n]*?AtlasAuthorNetwork\\?\\.unmount\\(\\)`));
  assert.match(app,/function resetAuthorNetwork\(\).*?AtlasAuthorNetwork\?\.reset\(\)/);
  assert.ok(index.indexOf('/static/author-network.js')<index.indexOf('/static/app.js'));
  assert.match(index,/\/static\/author-network.css/);
});

test('positive candidates rank over the entire population and include ties at the cutoff',()=>{
  const h=harness(),rows=h.t.ranked(h.data.nodes,'betweenness',2);
  assert.deepEqual(Array.from(rows,r=>[r.node.id,r.rank]),[['a',1],['b',2],['c',2]]);
  assert.equal(h.t.ranked(h.data.nodes,'institution_bridge',5).length,2,'zero and missing affiliation scores are not keyman highlights');
  h.t.state.top=3;
  const filtered=h.api.render(h.data,[h.data.nodes[0]],clusters,h.options);
  assert.match(filtered,/計算・順位の対象：マップ対象の4著者/);
  assert.match(filtered,/全3共著関係/);
  assert.match(filtered,/data-an-select="b"/);
  assert.match(filtered,/大学 A · 絞り込み対象外/);
  assert.match(filtered,/検索・絞り込みでも計算母集団は変わりません/);
});

test('metric and top controls update ranking in place, preserve the camera and explain transitivity',()=>{
  const h=harness();h.t.zoom(2);const before={...h.t.state.camera};
  const change=event({dataset:{anControl:'metric'},value:'local_clustering'});h.host.dispatch('change',change);
  assert.equal(h.t.state.metric,'local_clustering');assert.equal(change.stopped,true);
  assert.deepEqual({...h.t.state.camera},before);
  assert.match(h.host.innerHTML,/高い値＝共著者同士の結束/);
  assert.match(h.host.innerHTML,/橋渡しを探す場合は「媒介中心性」/);
  assert.equal(h.controls.get('#an-metric').focusCount,1);
  assert.match(h.host.innerHTML,/data-an-select="b"/);assert.doesNotMatch(h.host.innerHTML,/data-an-select="a"/);
  h.host.dispatch('change',event({dataset:{anControl:'top'},value:'10'}));assert.equal(h.t.state.top,10);
  h.host.dispatch('change',event({dataset:{anControl:'metric'},value:'fake'}));assert.equal(h.t.state.metric,'local_clustering');
});

test('missing legacy metrics are not synthesized from the capped graph',()=>{
  const data=network();for(const n of data.nodes)delete n.metrics;
  const h=harness(data);assert.equal(h.t.metricValue(data.nodes[0]),null);
  assert.match(h.host.innerHTML,/指標は未計算です。ネットワークを再計算/);
  assert.match(h.host.innerHTML,/この指標で正の値を持つ著者がいません/);
  assert.doesNotMatch(h.host.innerHTML,/class="an-node is-keyman/);
});

test('selecting an author highlights actual neighbors and restores incident edges past the cap',()=>{
  const h=harness();assert.equal(h.t.visibleEdges().length,1);
  h.t.select('a');assert.deepEqual(Array.from(h.t.neighborIDs(h.data,'a')).sort(),['b','d']);
  assert.deepEqual(Array.from(h.t.visibleEdges(),e=>[e.source,e.target]),[['a','b'],['a','d']]);
  assert.match(h.stage.outerHTML,/an-node [^"]*is-neighbor[^"]*" data-an-node="d"/);
  assert.match(h.stage.outerHTML,/an-node [^"]*is-muted[^"]*" data-an-node="c"/);
  assert.match(h.selection.innerHTML,/直接の共著者 2人（現在の絞り込み内 2人）/);
  assert.deepEqual(h.selected,['a']);
  h.host.dispatch('click',event(element({anAction:'detail'})));assert.deepEqual(h.detail,['a']);
  h.t.select(null);assert.equal(h.t.visibleEdges().length,1);assert.match(h.selection.innerHTML,/著者を選択すると/);
});

test('filtering preserves selected author and full neighbor counts without drawing excluded authors',()=>{
  const h=harness();h.t.select('a');
  const html=h.api.render(h.data,[h.data.nodes[1]],clusters,h.options);
  assert.match(html,/SELECTED RESEARCHER · 絞り込み条件の対象外/);
  assert.match(html,/直接の共著者 2人（現在の絞り込み内 1人）/);
  assert.equal(h.t.visibleEdges().length,0);
  assert.equal((h.t.graphHTML().match(/data-an-node=/g)||[]).length,1);
  assert.equal(h.t.state.selected,'a');
});

test('large-corpus display payload retains all selected incident lines using observed neighbor weights',()=>{
  const h=harness();h.data.nodes[0].neighbor_weights={b:2,d:7};
  const large={...h.data};delete large.export_data;
  h.api.render(large,large.nodes,clusters,h.options);h.t.select('a');
  assert.deepEqual(Array.from(h.t.visibleEdges(),edge=>[edge.source,edge.target,edge.weight]),[['a','b',2],['a','d',7]]);
  assert.match(h.stage.outerHTML,/共著7論文/);
  const filtered=h.api.render(large,[large.nodes[0],large.nodes[1]],clusters,h.options);
  assert.equal(h.t.visibleEdges().length,1,'only authors within the current filter may be drawn');
  assert.match(filtered,/直接の共著者 2人（現在の絞り込み内 1人）/);
});

test('node drag updates endpoints and institution hulls and prevents selection from the following click',()=>{
  const h=harness(),originalX=58+.2*(860-116),originalY=65+.4*(540-130);
  h.host.dispatch('pointerdown',event(h.nodes[0]));
  h.host.dispatch('pointermove',event(h.nodes[0],{clientX:240,clientY:225}));
  h.host.dispatch('pointerup',event(h.nodes[0],{clientX:240,clientY:225}));
  assert.equal(h.t.state.positions.get('a').x,originalX+40);assert.equal(h.t.state.positions.get('a').y,originalY+25);
  assert.equal(h.edges[0].attributes.get('x1'),String(originalX+40));
  assert.ok(h.clusterElements[0].ellipse.attributes.has('cx'));
  const click=event(h.nodes[0]);h.host.dispatch('click',click);
  assert.equal(click.prevented,true);assert.deepEqual(h.selected,[]);assert.deepEqual(h.captured,[1]);assert.deepEqual(h.released,[1]);
});

test('a short node press selects once, whereas keyboard clicks remain usable after canceled drag',()=>{
  const h=harness();h.host.dispatch('pointerdown',event(h.nodes[0]));h.host.dispatch('pointerup',event(h.nodes[0]));h.host.dispatch('click',event(h.nodes[0]));
  assert.deepEqual(h.selected,['a']);
  h.host.dispatch('pointerdown',event(h.nodes[1]));h.host.dispatch('pointercancel',event(h.nodes[1]));
  h.host.dispatch('click',event(element({anSelect:'b'}),{detail:0}));assert.deepEqual(h.selected,['a','b']);
});

test('panning and pointer anchored zoom use SVG coordinates, including letterbox offsets',()=>{
  const h=harness();h.svg.getBoundingClientRect=()=>({left:10,top:20,width:1000,height:540});
  const point=h.t.svgPoint({clientX:80,clientY:20});assert.deepEqual({...point},{x:0,y:0});
  h.host.dispatch('pointerdown',event(h.svg,{clientX:280,clientY:220}));h.host.dispatch('pointermove',event(h.svg,{clientX:380,clientY:260}));h.host.dispatch('pointerup',event(h.svg,{clientX:380,clientY:260}));
  assert.deepEqual({...h.t.state.camera},{x:100,y:40,k:1});
  h.t.zoom(2,{x:200,y:140});assert.deepEqual({...h.t.state.camera},{x:0,y:-60,k:2});
  assert.equal(h.viewport.attributes.get('transform'),'translate(0 -60) scale(2)');
  h.t.zoom(100);assert.equal(h.t.state.camera.k,5);h.t.zoom(.0001);assert.equal(h.t.state.camera.k,.5);
  const wheel=event(h.svg,{deltaY:-100});h.host.dispatch('wheel',wheel);assert.equal(wheel.prevented,true);assert.ok(h.t.state.camera.k>.5);
});

test('keyboard supports node selection, node movement, camera navigation and selection clearing',()=>{
  const h=harness();const enter=event(h.nodes[0],{key:'Enter'});h.host.dispatch('keydown',enter);assert.equal(enter.stopped,true);assert.equal(h.t.state.selected,'a');
  h.host.dispatch('keydown',event(h.nodes[0],{key:'ArrowRight'}));assert.ok(h.t.state.positions.has('a'));
  h.host.dispatch('keydown',event(h.svg,{key:'ArrowLeft'}));assert.equal(h.t.state.camera.x,-36);
  h.host.dispatch('keydown',event(h.svg,{key:'+'}));assert.equal(h.t.state.camera.k,1.25);
  h.host.dispatch('keydown',event(h.svg,{key:'Home'}));assert.deepEqual({...h.t.state.camera},{x:0,y:0,k:1});
  h.host.dispatch('keydown',event(h.nodes[0],{key:'Escape'}));assert.equal(h.t.state.selected,null);
});

test('reset restores positions and camera and changing datasets clears selection and layout',()=>{
  const h=harness();h.t.select('a');h.t.moveNode('a',20,30);h.t.zoom(3);
  h.host.dispatch('click',event(element({anAction:'reset'})));
  assert.equal(h.t.state.positions.size,0);assert.deepEqual({...h.t.state.camera},{x:0,y:0,k:1});
  const next={...h.data,id:'net2',result_id:'r2'};h.api.render(next,next.nodes,clusters,{...h.options,resultId:'r2'});
  assert.equal(h.t.state.selected,null);assert.equal(h.t.state.positions.size,0);
});

test('mount and unmount do not leak listeners or perform work in detached views',()=>{
  const h=harness();for(let i=0;i<6;i++)h.api.mount(h.host);
  assert.ok([...h.events.values()].every(callbacks=>callbacks.size===1));
  h.api.unmount();assert.ok([...h.events.values()].every(callbacks=>callbacks.size===0));
  h.host.dispatch('click',event(element({anSelect:'b'})));assert.deepEqual(h.selected,[]);
  h.api.mount(h.host);h.host.dispatch('click',event(element({anSelect:'b'})));assert.deepEqual(h.selected,['b']);
  h.api.reset();assert.equal(h.t.state.selected,null);assert.ok([...h.events.values()].every(callbacks=>callbacks.size===0));
});

test('names, affiliations and metric descriptions are escaped and controls have accessible names',()=>{
  const data=network();data.nodes[0].label='<img src=x onerror=alert(1)>';data.nodes[0].affiliations=[{name:'<script>bad</script>'}];data.metric_definitions.betweenness.description='<svg onload=bad>';
  const h=harness(data);h.t.select('a');
  assert.doesNotMatch(h.host.innerHTML,/<img|<svg onload/);assert.match(h.host.innerHTML,/&lt;img/);
  assert.doesNotMatch(h.selection.innerHTML,/<script>/);assert.match(h.selection.innerHTML,/&lt;script/);
  assert.match(h.host.innerHTML,/aria-describedby="an-help"/);assert.match(h.host.innerHTML,/role="button" tabindex="0" aria-pressed=/);
  assert.match(h.host.innerHTML,/label for="an-metric"/);assert.match(h.host.innerHTML,/aria-label="拡大"/);
  assert.doesNotMatch(h.host.innerHTML,/data-author=/,'node activation is isolated from the legacy document click handler');
});
