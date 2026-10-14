import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/report-exports.js',import.meta.url),'utf8');
const PNG='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z1VAAAAAASUVORK5CYII=';
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const escape=value=>String(value).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
class Node {
  constructor(tag,attrs={},children=[],text=''){this.localName=tag;this.attrs={...attrs};this.children=children;this.text=text;this.computed={fill:'#88aacc',stroke:'none','font-family':'sans-serif','font-size':'12px'};this.inline={};this.style={setProperty:(name,value)=>{this.inline[name]=value;}};this.isConnected=true;this.ownerDocument={baseURI:'http://localhost/app?result_id=result-a'};this.owner={dataset:{landscapeResult:'result-a',reportResultId:'result-a'}};this.viewBox={baseVal:{width:800,height:600}};}
  get attributes(){return Object.entries(this.attrs).map(([name,value])=>({name,value}));}
  getAttribute(name){return this.attrs[name]??null;}
  setAttribute(name,value){this.attrs[name]=String(value);}
  removeAttribute(name){delete this.attrs[name];}
  querySelectorAll(){return this.children.flatMap(child=>[child,...child.querySelectorAll('*')]);}
  cloneNode(){const copy=new Node(this.localName,this.attrs,this.children.map(child=>child.cloneNode()),this.text);copy.computed={...this.computed};return copy;}
  getBoundingClientRect(){return {width:800,height:600};}
  closest(){return this.owner;}
}
function serialize(node){const attrs=[...Object.entries(node.attrs),...(Object.keys(node.inline).length?[['style',Object.entries(node.inline).map(([name,value])=>`${name}:${value}`).join(';')]]:[])];return `<${node.localName}${attrs.map(([name,value])=>` ${name}="${escape(value)}"`).join('')}>${escape(node.text)}${node.children.map(serialize).join('')}</${node.localName}>`;}
function scene(){const marker=new Node('marker',{id:'arrow'}),gradient=new Node('linearGradient',{id:'gradient'}),glow=new Node('filter',{id:'glow'}),path=new Node('path',{d:'M10 20C30 40 50 60 70 80','marker-end':'url(#arrow)','transform':'translate(15 8) scale(1.5)'}),text=new Node('text',{x:'20',y:'30'},[],'時層の論文');path.computed={fill:'url("http://localhost/app?result_id=result-a#gradient")',filter:'url(#glow)','marker-end':'url(#arrow)',stroke:'#88ffdd',transform:'matrix(1.5, 0, 0, 1.5, 15, 8)'};return new Node('svg',{viewBox:'0 0 800 600'},[new Node('defs',{},[marker,gradient,glow]),path,text]);}
function harness({fetchImpl,svg=scene(),decode='success',canvasPNG=PNG,querySources=null}={}){
  const events=new Map(),documentEvents=new Map(),calls=[],urls=[],revoked=[],anchors=[],timers=new Map(),pendingImages=[];let timerId=0;
  const window={__ATLAS_UI_TEST__:true,location:{href:'http://localhost/app?result_id=result-a'},getComputedStyle:node=>({display:node.computed.display||'block',visibility:node.computed.visibility||'visible',getPropertyValue:name=>node.computed[name]||''}),atob:value=>Buffer.from(value,'base64').toString('binary'),setTimeout(callback){const id=++timerId;timers.set(id,callback);return id;},clearTimeout:id=>timers.delete(id),addEventListener(name,callback){events.set(name,callback);},URL:{createObjectURL(blob){const url=`blob:test-${urls.length}`;urls.push({url,blob});return url;},revokeObjectURL:url=>revoked.push(url)},fetch:async(url,options)=>{calls.push({url,options});return fetchImpl?fetchImpl(url,options):{ok:true,headers:{get:()=>"attachment; filename*=UTF-8''report.docx"},blob:async()=>new Blob(['document'])};}};
  Object.defineProperty(window,'localStorage',{get(){throw new Error('settings storage must not be read');}});Object.defineProperty(window,'AtlasConnections',{get(){throw new Error('connection settings must not be read');}});
  window.Image=class {set src(value){this.url=value;pendingImages.push(this);if(decode==='success')Promise.resolve().then(()=>this.onload());if(decode==='error')Promise.resolve().then(()=>this.onerror());}};
  const body={appendChild:node=>anchors.push(node)};
  const document={body,addEventListener(name,callback){documentEvents.set(name,callback);},querySelectorAll(selector){if(querySources)return querySources(selector);if(selector.includes('> svg.map-svg'))return svg?[svg]:[];return [];},createElement(tag){if(tag==='canvas')return {getContext:()=>({fillRect(){},drawImage(){}}),toDataURL:()=>canvasPNG};return {clicked:false,removed:false,click(){this.clicked=true;},remove(){this.removed=true;}};}};
  vm.runInContext(source,vm.createContext({window,document,URL,Blob,XMLSerializer:class{serializeToString(node){return serialize(node);}},console,Promise,Set,Map}));
  return {t:window.__reportExportsTest,api:window.AtlasReportExports,window,document,events,documentEvents,calls,urls,revoked,anchors,timers,pendingImages,svg,flushTimers(){for(const [id,callback] of [...timers]){timers.delete(id);callback();}}};
}
const snapshot=(resultId='result-a',extra={})=>({result_id:resultId,title:'時層マップ',caption:'表示時点の静止図',data_url:PNG,...extra});

test('every supported report kind exposes all four document formats and retains a candidate selection',()=>{
  const h=harness();for(const kind of ['result','corpus','landscape','annual','field','foresight']){const html=h.api.render(kind,'report-a','result-a',{candidate_id:'topic-a'});for(const format of ['pdf','docx','xlsx','pptx'])assert.match(html,new RegExp(`value="${format}"`));assert.match(html,/新しいLLM生成は行いません/);}
  const item=h.t.record('foresight','report-a','result-a',{candidate_id:'topic-a'});assert.equal(h.t.exportPayload(item).candidate_id,'topic-a');assert.equal(h.api.render('unknown','id','result-a'),'');
});

test('export uses one plain same-origin POST with scoped PNGs and downloads the returned Blob',async()=>{
  const h=harness();h.t.addSnapshot('result-a',snapshot());h.t.addSnapshot('result-b',snapshot('result-b'));
  const item=h.t.record('annual','report-a','result-a');item.format='docx';await h.t.download(item);
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/report-exports/annual/report-a');assert.equal(h.calls[0].options.method,'POST');assert.deepEqual(Object.keys(h.calls[0].options.headers),['Content-Type']);
  const body=JSON.parse(h.calls[0].options.body);assert.equal(body.format,'docx');assert.equal(body.include_figures,true);assert.equal(body.snapshots.length,1);assert.equal(body.snapshots[0].result_id,'result-a');assert.equal(body.snapshots[0].data_url,PNG);assert.equal('id' in body.snapshots[0],false);
  assert.equal(h.anchors[0].download,'report.docx');assert.equal(h.anchors[0].clicked,true);assert.equal(h.anchors[0].removed,true);assert.equal(item.busy,false);h.flushTimers();assert.deepEqual(h.revoked,[h.urls[0].url]);
});

test('duplicate export clicks are disabled while pending and errors remain retryable',async()=>{
  let release,attempts=0;const h=harness({fetchImpl:()=>{attempts++;return new Promise(resolve=>release=resolve);}}),item=h.t.record('corpus','report-a','result-a');
  const pending=h.t.download(item);await h.t.download(item);assert.equal(attempts,1);assert.match(h.api.render('corpus','report-a','result-a'),/ファイルを作成中/);assert.match(h.api.render('corpus','report-a','result-a'),/data-rx-format disabled/);
  release({ok:false,status:422,json:async()=>({detail:'図の所有者が一致しません。'})});await pending;assert.equal(item.busy,false);assert.match(item.error,/所有者/);
  const retry=h.t.download(item);release({ok:true,headers:{get:()=>null},blob:async()=>new Blob(['pdf'])});await retry;assert.equal(attempts,2);assert.equal(item.error,'');
});

test('SVG serialization preserves current geometry, Japanese text and internal paint definitions without editing the live SVG',()=>{
  const h=harness(),before=serialize(h.svg),captured=h.t.serializeSVG(h.svg);
  assert.equal(serialize(h.svg),before);assert.match(captured.text,/時層の論文/);assert.match(captured.text,/matrix\(1\.5, 0, 0, 1\.5, 15, 8\)/);assert.match(captured.text,/marker-end:url\(#arrow\)/);assert.match(captured.text,/fill:url\(#gradient\)/);assert.match(captured.text,/filter:url\(#glow\)/);assert.match(captured.text,/id="gradient"/);assert.doesNotMatch(captured.text,/http:\/\/localhost/);assert.equal(captured.width,1600);assert.equal(captured.height,1200);
});

test('external URLs, embedded content and unmatched fragment references are rejected before rasterization',async()=>{
  for(const bad of ['url(https://remote.example/chart.png)','url(data:image/png;base64,AAAA)','url(#missing)','url(http://localhost/other#gradient)']){const h=harness();h.svg.children[1].computed.fill=bad;await h.t.capture('result-a','landscape');assert.equal(h.urls.length,0);assert.equal(h.calls.length,0);assert.equal(h.t.images('result-a').length,0);assert.ok(h.t.captures.get('result-a').error);}
  for(const tag of ['script','foreignObject','image']){const h=harness();h.svg.children.push(new Node(tag,{href:'https://remote.example/'}));assert.throws(()=>h.t.serializeSVG(h.svg),/追加できません/);}
  const h=harness();h.svg.children[1].setAttribute('onclick','evil()');assert.doesNotMatch(h.t.serializeSVG(h.svg).text,/onclick/);
});

test('capture rasterizes the frozen SVG, revokes its temporary URL and stores only a PNG for the owning result',async()=>{
  const h=harness();await h.t.capture('result-a','landscape');const saved=h.t.images('result-a');assert.equal(saved.length,1);assert.equal(saved[0].data_url,PNG);assert.equal(saved[0].result_id,'result-a');assert.equal(h.t.images('result-b').length,0);assert.equal(h.urls.length,1);assert.equal(h.urls[0].blob.type,'image/svg+xml;charset=utf-8');assert.deepEqual(h.revoked,[h.urls[0].url]);assert.equal(h.timers.size,0);assert.equal(h.calls.length,0);
});

test('a result switch during rasterization prevents a stale figure attachment',async()=>{
  const h=harness({decode:'pending'}),pending=h.t.capture('result-a','landscape');await tick();assert.equal(h.pendingImages.length,1);
  h.events.get('atlas:result')({detail:{id:'result-b'}});h.pendingImages[0].onload();await pending;
  assert.equal(h.t.images('result-a').length,0);assert.equal(h.t.images('result-b').length,0);assert.match(h.t.captures.get('result-a').error,/変わった/);assert.equal(h.revoked.length,1);assert.equal(h.t.captures.get('result-a').busy,false);
});

test('snapshot caption freezes the projection, scope, topic and visible year window at capture time',async()=>{
  const h=harness({decode:'pending'});h.svg.owner.dataset.rxCaption='投影 PCA / 全対象論文 / 時層 / 話題 Materials / 2022〜2024';
  const pending=h.t.capture('result-a','landscape');await tick();h.svg.owner.dataset.rxCaption='投影 t-SNE / 別の期間';h.pendingImages[0].onload();await pending;
  assert.match(h.t.images('result-a')[0].caption,/投影 PCA.*2022〜2024/);assert.doesNotMatch(h.t.images('result-a')[0].caption,/t-SNE/);
  h.svg.owner.dataset.rxReady='false';assert.equal(h.t.findSource('result-a','landscape'),null);
  const bounded=h.t.addSnapshot('result-b',snapshot('result-b',{title:'あ'.repeat(250)}));assert.equal(bounded.title.length,200);
});

test('rasterization failure releases resources and preserves previous attachments',async()=>{
  const h=harness({decode:'error'});h.t.addSnapshot('result-a',snapshot());await h.t.capture('result-a','landscape');assert.equal(h.t.images('result-a').length,1);assert.equal(h.t.captures.get('result-a').busy,false);assert.match(h.t.captures.get('result-a').error,/PNGに変換/);assert.equal(h.revoked.length,1);assert.equal(h.timers.size,0);
});

test('PNG ownership, count, decoded size, signature and dimensions are bounded',()=>{
  const h=harness();assert.throws(()=>h.t.addSnapshot('result-a',snapshot('result-b')),/一致しません/);
  for(let i=0;i<4;i++)h.t.addSnapshot('result-a',snapshot());assert.throws(()=>h.t.addSnapshot('result-a',snapshot()),/4枚/);
  assert.throws(()=>h.t.validatePNG('data:image/svg+xml;base64,AAAA'),/PNG形式/);assert.throws(()=>h.t.validatePNG('data:image/png;base64,AAAA'),/画像情報/);
  const bytes=Buffer.from(PNG.split(',')[1],'base64');bytes.writeUInt32BE(4097,16);assert.throws(()=>h.t.validatePNG('data:image/png;base64,'+bytes.toString('base64')),/サイズ/);
  const huge=Buffer.alloc(h.t.MAX_BYTES+1);Buffer.from(PNG.split(',')[1],'base64').copy(huge);assert.throws(()=>h.t.validatePNG('data:image/png;base64,'+huge.toString('base64')),/2MB/);
});

test('removing a snapshot only affects that result and escaped labels never become HTML',()=>{
  const h=harness(),a=h.t.addSnapshot('result-a',snapshot('result-a',{title:'<img src=x onerror=evil>'}));h.t.addSnapshot('result-b',snapshot('result-b'));
  assert.match(h.api.captureControls('result-a','landscape'),/&lt;img/);assert.doesNotMatch(h.api.captureControls('result-a','landscape'),/<img src=x/);
  const root={dataset:{rxResult:'result-a'}},button={dataset:{rxAction:'remove',rxImage:a.id},closest:selector=>selector==='[data-rx-result]'?root:null};h.t.handleClick({target:{closest:()=>button}});
  assert.equal(h.t.images('result-a').length,0);assert.equal(h.t.images('result-b').length,1);
});

test('capture selection rejects unrelated results, hidden maps and unavailable chart scopes',()=>{
  const h=harness();assert.ok(h.t.findSource('result-a','landscape'));assert.equal(h.t.findSource('result-b','landscape'),null);assert.equal(h.t.findSource('result-a','field'),null);h.svg.computed.visibility='hidden';assert.equal(h.t.findSource('result-a','landscape'),null);
});

test('suggested download filenames cannot contain path separators or control characters',()=>{
  const h=harness(),item=h.t.record('result','result-a','result-a');item.format='pptx';const name=h.t.filename('attachment; filename="../../bad\r\nname.pptx"',item);assert.doesNotMatch(name,/[\\/\r\n]/);assert.match(name,/\.pptx$/);
});

test('multiple visible charts expose an escaped choice and capture the selected exact SVG',async()=>{
  const map=scene(),annual=scene();annual.children.at(-1).text='年次推移の実際の図';annual.setAttribute('aria-label','論文数の年次推移 2022、2023');
  const panel={querySelector:selector=>selector==='h2,h3'?{textContent:'年次 <script> & 出版'}:null},owner={dataset:{reportResultId:'result-a',rxCaption:'分析期間 2022〜2023'}};
  annual.closest=selector=>selector==='#content'?owner:panel;
  const h=harness({querySources:selector=>selector.includes('> svg.map-svg')?[map]:selector.includes('.overview-bottom')?[annual]:[]});
  const sources=h.t.findSources('result-a');assert.equal(sources.length,2);assert.match(h.api.captureControls('result-a'),/data-rx-source/);assert.match(h.api.captureControls('result-a'),/年次 &lt;script&gt; &amp; 出版/);assert.doesNotMatch(h.api.captureControls('result-a'),/<script>/);
  const selected=sources.find(item=>item.kind==='overview'),root={dataset:{rxResult:'result-a',rxScope:'auto'}};
  h.documentEvents.get('change')({target:{value:selected.sourceKey,matches:selector=>selector==='[data-rx-source]',closest:()=>root}});
  assert.equal(h.t.findSource('result-a').sourceKey,selected.sourceKey);await h.t.capture('result-a','auto',selected.sourceKey);
  assert.match(await h.urls[0].blob.text(),/年次推移の実際の図/);assert.equal(h.t.images('result-a')[0].title,'年次 <script> & 出版');assert.match(h.t.images('result-a')[0].caption,/2022〜2023/);
});

test('a chart choice removed by a view rerender never silently captures another chart',async()=>{
  const map=scene(),annual=scene();let showAnnual=true;
  const h=harness({querySources:selector=>selector.includes('> svg.map-svg')?[map]:showAnnual&&selector.includes('.overview-bottom')?[annual]:[]});
  const key=h.t.findSources('result-a').find(item=>item.kind==='overview').sourceKey;showAnnual=false;
  await h.t.capture('result-a','auto',key);assert.equal(h.t.images('result-a').length,0);assert.equal(h.urls.length,0);assert.match(h.t.captures.get('result-a').error,/選び直して/);
  assert.doesNotMatch(h.api.captureControls('result-a'),/data-rx-source/);
});

test('general capture accepts only current owned real chart selectors and omits icons and keyword tables',()=>{
  const wanted=['.overview-bottom','.citation-grid','.forecast-grid','.an-graph-stage'];
  for(const marker of wanted){const svg=scene();svg.owner.dataset.reportResultId='result-a';const queries=[];
    const h=harness({querySources:selector=>{queries.push(selector);return selector.includes(marker)?[svg]:[];}});
    assert.equal(h.t.findSources('result-a').length,1);assert.equal(h.t.findSources('result-b').length,0);
    svg.setAttribute('aria-hidden','true');assert.equal(h.t.findSources('result-a').length,0);
    assert.ok(queries.filter(selector=>selector.includes(marker)).every(selector=>selector.includes('[data-report-view=')));
    assert.ok(queries.every(selector=>!selector.includes('#keywords-table')));
  }
});

test('citation mode, forecast fallback topic and displayed author filters become snapshot captions',()=>{
  for(const [kind,marker,values,expected] of [
    ['citations','.citation-grid',{'[data-citation-mode].active':{textContent:'出版年別・累積引用数'}},/出版年別・累積引用数/],
    ['forecast','.forecast-grid',{'.forecast-controls .forecast-topic.active':{textContent:'実際に表示したテーマ'}},/表示テーマ：実際に表示したテーマ/],
    ['authors','.an-graph-stage',{'.author-network-context .source-pill':{textContent:'共著コミュニティ'},'[data-an-control="metric"]':{selectedOptions:[{textContent:'媒介中心性'}]},'#author-filter-summary':{textContent:'12著者 · 材料研究'}},/共著コミュニティ[\s\S]*媒介中心性[\s\S]*12著者/]
  ]){const svg=scene();svg.owner.querySelector=selector=>values[selector];const h=harness({querySources:selector=>selector.includes(marker)?[svg]:[]}),selected=h.t.findSource('result-a');assert.equal(selected.kind,kind);assert.match(h.t.sourceCaption(selected),expected);}
});
