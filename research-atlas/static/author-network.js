/* Interactive coauthor view. Python supplies metrics; this module only presents them. */
(() => {
  'use strict';
  const W = 860, H = 540;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clamp = (n, low, high) => Math.max(low, Math.min(high, Number(n) || 0));
  const paint = value => /^#[0-9a-f]{3,8}$/i.test(String(value)) ? value : '#58e0c5';
  const labels = {
    betweenness: '媒介中心性', local_clustering: '近隣の結束度（局所推移性）',
    institution_bridge: '所属機関への橋渡し数', degree: '近隣著者数',
    strength: '共著強度', pagerank: 'PageRank',
  };
  const descriptions = {
    betweenness: '著者間の最短経路を仲介する度合い。高い著者は集団間をつなぐ位置にあります。',
    local_clustering: 'ある著者の共著者同士も共著している割合。高い値は近隣の結束を示し、集団間の橋渡しの強さではありません。',
    institution_bridge: '明示された自身の所属とは異なる所属機関へ、共著関係でつながる機関数。所属未取得は順位付けできません。',
    degree: 'マップ対象著者のうち、直接共著した相手の人数です。',
    strength: '直接の共著関係の重み（共著論文数）の合計です。1論文に複数の共著者がいる場合、相手ごとに数えます。',
    pagerank: '共著関係の重みと相手のつながりを反映したネットワーク内の中心性です。',
  };
  const state = {key:'', metric:'betweenness', top:5, selected:null, camera:{x:0,y:0,k:1}, positions:new Map()};
  let context = null, host = null, removeListeners = [], gesture = null, suppressClick = false;
  const metricValue = (node, key = state.metric) => node.metrics?.[key] != null && Number.isFinite(Number(node.metrics[key])) ? Number(node.metrics[key]) : null;
  const formatValue = (value, key = state.metric) => value == null ? '未計算・不明' : ['degree','strength','institution_bridge'].includes(key) ? Number(value).toLocaleString('ja-JP') : Number(value).toLocaleString('ja-JP', {maximumFractionDigits:4});
  function ranked(nodes, metric = state.metric, limit = state.top) {
    const entries = nodes.filter(node => metricValue(node, metric) > 0).sort((a,b) => metricValue(b, metric)-metricValue(a, metric) || String(a.label || a.id).localeCompare(String(b.label || b.id), 'ja') || String(a.id).localeCompare(String(b.id)));
    if (!entries.length) return [];
    const cutoff = metricValue(entries[Math.min(entries.length, limit)-1], metric);
    let previous = null, rank = 0;
    return entries.filter(node => metricValue(node, metric) >= cutoff).map((node,index) => {
      const value = metricValue(node, metric);
      if (value !== previous) rank = index+1;
      previous = value;
      return {node,value,rank};
    });
  }
  function neighborIDs(network, id) {
    const node = network.nodes?.find(entry => entry.id === id);
    if (Array.isArray(node?.neighbor_ids)) return new Set(node.neighbor_ids);
    const neighbors = new Set();
    for (const edge of network.export_data?.edges || network.edges || []) {
      if (edge.source === id) neighbors.add(edge.target);
      if (edge.target === id) neighbors.add(edge.source);
    }
    return neighbors;
  }
  const position = node => state.positions.get(node.id) || {x:58+clamp(node.x,0,1)*(W-116),y:65+clamp(node.y,0,1)*(H-130)};
  const clusterID = node => node.cluster_id ?? node.topic_id;
  const clusterFor = node => context.clusters.find(cluster => cluster.id === clusterID(node));
  const nodeColor = node => paint(clusterFor(node)?.color);
  const metricDefinition = () => context.network.metric_definitions?.[state.metric] || {};
  const metricLabel = () => metricDefinition().label || labels[state.metric];
  function radius(node) {
    const values = context.network.nodes.map(n => metricValue(n)).filter(n => n != null);
    const maximum = Math.max(0, ...values), value = metricValue(node);
    return value == null || maximum === 0 ? 5 : 5+9*Math.sqrt(Math.max(0,value)/maximum);
  }
  function visibleEdges() {
    const ids = new Set(context.nodes.map(n => n.id)), found = new Map();
    const candidates = [...(context.network.edges || [])];
    if (state.selected) {
      candidates.push(...(context.network.export_data?.edges || []).filter(e => e.source === state.selected || e.target === state.selected));
      // Large-corpus responses omit export_data; the selected node retains its observed incident weights.
      const selected=context.network.nodes.find(node=>node.id===state.selected);
      for(const [target,weight] of Object.entries(selected?.neighbor_weights || {})) {
        if(Number.isFinite(Number(weight))&&Number(weight)>0)candidates.push({source:state.selected,target,weight:Number(weight)});
      }
    }
    for (const edge of candidates) {
      if (!ids.has(edge.source) || !ids.has(edge.target)) continue;
      found.set([String(edge.source),String(edge.target)].sort().join('\u0000'), edge);
    }
    return [...found.values()];
  }
  function clusterBounds(cluster) {
    const members = context.nodes.filter(n => clusterID(n) === cluster.id);
    if (!members.length) return null;
    const points = members.map(position), xs = points.map(p=>p.x), ys = points.map(p=>p.y);
    return {x:(Math.min(...xs)+Math.max(...xs))/2,y:(Math.min(...ys)+Math.max(...ys))/2,rx:Math.max(29,(Math.max(...xs)-Math.min(...xs))/2+28),ry:Math.max(29,(Math.max(...ys)-Math.min(...ys))/2+28)};
  }
  function selectedHTML() {
    const node = context.network.nodes.find(n => n.id === state.selected);
    if (!node) return '<div class="an-selection-empty">著者を選択すると、直接の共著者を強調し、指標と所属を確認できます。</div>';
    const neighbors = neighborIDs(context.network,node.id), visible = new Set(context.nodes.map(n => n.id));
    const visibleNeighbors = [...neighbors].filter(id => visible.has(id)).length;
    const affiliations = (node.affiliations || []).map(a => typeof a === 'string' ? a : a.name).filter(Boolean);
    return `<div class="an-selected-header"><div><small>SELECTED RESEARCHER${visible.has(node.id)?'':' · 絞り込み条件の対象外'}</small><h3>${esc(node.label || node.id)}</h3><p>${esc(affiliations.join(' / ') || '所属未取得')}</p></div><button type="button" class="text-link" data-an-action="clear">選択解除 ×</button></div><div class="an-selected-metrics">${Object.keys(labels).map(key=>`<div><span>${esc(context.network.metric_definitions?.[key]?.label || labels[key])}</span><strong>${formatValue(metricValue(node,key),key)}</strong></div>`).join('')}</div><div class="an-selected-footer"><span>直接の共著者 ${neighbors.size}人（現在の絞り込み内 ${visibleNeighbors}人）</span><button type="button" class="button button-quiet button-small" data-an-action="detail">所属・識別根拠・論文を見る ↗</button></div>`;
  }
  function rankingHTML() {
    const rows = ranked(context.network.nodes), visible = new Set(context.nodes.map(n=>n.id));
    if (!rows.length) return '<p class="an-muted">この指標で正の値を持つ著者がいません。未計算の保存結果では「ネットワークを再計算」を実行してください。</p>';
    return `<div class="an-ranking-head"><strong>キーマン候補 · ${esc(metricLabel())}</strong><span>高い順 · 上位${state.top}人＋同順位</span></div><div class="an-ranking-list">${rows.map(({node,value,rank})=>`<button type="button" class="an-rank-row ${node.id===state.selected?'is-selected':''}" data-an-select="${esc(node.id)}" aria-pressed="${node.id===state.selected}"><b>${rank}</b><span><strong>${esc(node.label||node.id)}</strong><small>${esc(clusterFor(node)?.label || node.cluster_label || '所属・分類不明')}${visible.has(node.id)?'':' · 絞り込み対象外'}</small></span><em>${formatValue(value)}</em></button>`).join('')}</div>`;
  }
  function graphHTML() {
    const nodes = context.nodes, byID = new Map(nodes.map(n => [n.id,n])), edges = visibleEdges();
    const leaders = new Set(ranked(context.network.nodes).map(item => item.node.id));
    const neighbors = state.selected ? neighborIDs(context.network,state.selected) : new Set();
    const selectedVisible = byID.has(state.selected);
    const labelsShown = new Set([...new Set([...leaders,...nodes.map(n=>n.id)])].filter(id=>byID.has(id)).slice(0,8));
    labelsShown.add(state.selected);
    const groups = context.clusters.map(cluster => {
      const bounds = clusterBounds(cluster);if (!bounds) return '';
      const label = String(cluster.label || cluster.id);
      return `<g data-an-cluster="${esc(cluster.id)}"><ellipse class="an-cluster-area" cx="${bounds.x}" cy="${bounds.y}" rx="${bounds.rx}" ry="${bounds.ry}" fill="${paint(cluster.color)}" stroke="${paint(cluster.color)}"/><text class="an-cluster-title" x="${bounds.x}" y="${bounds.y-bounds.ry-9}" text-anchor="middle" fill="${paint(cluster.color)}">${esc(label.length>33?label.slice(0,32)+'…':label)}<title>${esc(label)}</title></text></g>`;
    }).join('');
    const links = edges.map(edge => {
      const a = position(byID.get(edge.source)), b = position(byID.get(edge.target)), incident = edge.source === state.selected || edge.target === state.selected;
      return `<line class="an-edge ${incident?'is-incident':''} ${selectedVisible&&!incident?'is-muted':''}" data-an-source="${esc(edge.source)}" data-an-target="${esc(edge.target)}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke-width="${Math.min(4,.7+Math.sqrt(Number(edge.weight)||0)*.5)}"><title>${esc(byID.get(edge.source).label)} ↔ ${esc(byID.get(edge.target).label)} · 共著${esc(edge.weight)}論文</title></line>`;
    }).join('');
    const points = nodes.map(node => {
      const p = position(node), r = radius(node), selected = node.id===state.selected, neighbor = neighbors.has(node.id);
      const label = String(context.group==='id' ? node.id : node.label || node.id), c = nodeColor(node);
      return `<g class="an-node ${leaders.has(node.id)?'is-keyman':''} ${selected?'is-selected':''} ${neighbor?'is-neighbor':''} ${selectedVisible&&!selected&&!neighbor?'is-muted':''}" data-an-node="${esc(node.id)}" transform="translate(${p.x} ${p.y})" role="button" tabindex="0" aria-pressed="${selected}" aria-label="${esc(label)}、${esc(metricLabel())} ${formatValue(metricValue(node))}。Enterで選択、矢印キーで位置を調整。" style="--an-color:${c}"><circle class="an-keyman-halo" r="${r+6}"/><circle class="an-node-dot" r="${r}"/><circle class="an-focus-ring" r="${r+10}"/><text class="an-node-title ${labelsShown.has(node.id)?'is-visible':''}" y="${r+17}" text-anchor="middle">${esc(label.length>27?label.slice(0,26)+'…':label)}</text><title>${esc(label)} · ${esc(clusterFor(node)?.label || '分類不明')} · ${esc(metricLabel())} ${formatValue(metricValue(node))}</title></g>`;
    }).join('');
    return `<div class="an-graph-stage"><div class="an-map-corner">COLLABORATION SPACE<br>${nodes.length} PEOPLE · ${edges.length} LINKS</div><div class="an-camera-controls" role="group" aria-label="共著ネットワークの表示操作"><button type="button" data-an-action="zoom-in" aria-label="拡大">＋</button><button type="button" data-an-action="zoom-out" aria-label="縮小">−</button><button type="button" data-an-action="reset" aria-label="位置とズームを初期配置に戻す">初期配置</button><output class="an-zoom-value">${Math.round(state.camera.k*100)}%</output></div><svg class="an-svg" viewBox="0 0 ${W} ${H}" role="group" tabindex="0" aria-label="ドラッグで移動、ホイールで拡大縮小できる共著ネットワーク" aria-describedby="an-help"><g class="an-viewport" transform="translate(${state.camera.x} ${state.camera.y}) scale(${state.camera.k})">${groups}${links}${points}</g></svg>${nodes.length?'':'<div class="an-empty-graph">条件に一致する著者がいません。検索語やクラスタを変更してください。</div>'}</div>`;
  }
  function html() {
    const definition = metricDefinition(), scope = context.network.metrics_scope || {};
    const count = scope.node_count ?? context.network.nodes.length, edgeCount = scope.edge_count;
    const missing = !context.network.nodes.some(node => metricValue(node) != null);
    return `<section class="an-interactive" aria-label="キーマン指標と共著ネットワーク"><div class="an-metric-controls"><label for="an-metric">キーマンを探す指標<select id="an-metric" data-an-control="metric">${Object.entries(labels).map(([key,label])=>`<option value="${key}" ${state.metric===key?'selected':''}>${esc(context.network.metric_definitions?.[key]?.label || label)}</option>`).join('')}</select></label><label for="an-top">強調する上位人数<select id="an-top" data-an-control="top">${[3,5,10,20].map(top=>`<option value="${top}" ${state.top===top?'selected':''}>${top}人＋同順位</option>`).join('')}</select></label></div><p class="an-definition"><strong>${esc(metricLabel())}</strong> ${esc(definition.description || descriptions[state.metric])}${state.metric==='local_clustering'?'<br>高い値＝共著者同士の結束。異なる集団の橋渡しを探す場合は「媒介中心性」を選択します。':''}</p><p class="an-scope">計算・順位の対象：マップ対象の${count}著者（最大120著者）${edgeCount==null?'':`と、その間の全${edgeCount}共著関係`}。検索・絞り込みでも計算母集団は変わりません。点の大きさ＝選択指標、光の輪＝正の値を持つ上位候補。研究の質・科学的影響力を直接評価する値ではありません。${missing?' 指標は未計算です。ネットワークを再計算してください。':''}</p>${graphHTML()}<p class="an-help" id="an-help">背景をドラッグして移動 · ホイール／＋−で拡大縮小 · 著者をドラッグして配置調整 · クリックで共著者を強調。キーボード：Tabで著者へ、Enterで選択、矢印で移動、Escで選択解除。図にフォーカスすると＋−・矢印・Homeでも表示を操作できます。</p><div class="an-selection" aria-live="polite">${selectedHTML()}</div><div class="an-ranking">${rankingHTML()}</div></section>`;
  }
  function render(network, nodes, clusters, options = {}) {
    const key = `${options.resultId || network.result_id || ''}:${network.id || 'saved'}:${network.group_by || 'topic'}`;
    if (state.key !== key) {state.key=key;state.positions.clear();state.camera={x:0,y:0,k:1};state.selected=null;}
    if (options.selected && (network.nodes || []).some(n=>n.id===options.selected)) state.selected = options.selected;
    context = {network:{...network,nodes:network.nodes || []},nodes,clusters,group:options.group,...options};
    return html();
  }
  function listen(target, type, handler, options) {
    target.addEventListener(type,handler,options);
    removeListeners.push(()=>target.removeEventListener(type,handler,options));
  }
  function unmount() {
    for (const remove of removeListeners) remove();
    removeListeners=[];host=null;gesture=null;suppressClick=false;
  }
  function reset() {unmount();context=null;state.key='';state.selected=null;state.positions.clear();state.camera={x:0,y:0,k:1};}
  function mount(element) {
    unmount();if (!element || !context) return;
    host=element;
    listen(host,'click',click);
    listen(host,'change',change);
    listen(host,'keydown',keydown);
    listen(host,'pointerdown',pointerdown);
    listen(host,'pointermove',pointermove);
    listen(host,'pointerup',pointerup);
    listen(host,'pointercancel',pointercancel);
    listen(host,'wheel',wheel,{passive:false});
  }
  function refresh(focusID) {
    if (!host) return;
    host.innerHTML=html();
    if (focusID) host.querySelector(`#${focusID}`)?.focus();
  }
  function applyCamera() {
    host?.querySelector('.an-viewport')?.setAttribute('transform',`translate(${state.camera.x} ${state.camera.y}) scale(${state.camera.k})`);
    const value=host?.querySelector('.an-zoom-value');if (value) value.textContent=`${Math.round(state.camera.k*100)}%`;
  }
  function zoom(factor, point = {x:W/2,y:H/2}) {
    const old=state.camera.k, next=clamp(old*factor,.5,5);
    state.camera.x=point.x-(point.x-state.camera.x)*next/old;
    state.camera.y=point.y-(point.y-state.camera.y)*next/old;
    state.camera.k=next;applyCamera();
  }
  function select(id) {
    state.selected = context.network.nodes.some(node=>node.id===id) ? id : null;
    context.onSelect?.(state.selected);
    // Selection can add incident edges omitted by the drawing cap; preserve keyboard focus.
    const active = host?.querySelector('.an-node:focus')?.dataset.anNode;
    const activeRank = host?.querySelector('.an-rank-row:focus')?.dataset.anSelect;
    if (!host) return;
    const stage=host.querySelector('.an-graph-stage');if(stage) stage.outerHTML=graphHTML();
    const selection=host.querySelector('.an-selection');if(selection) selection.innerHTML=selectedHTML();
    const ranking=host.querySelector('.an-ranking');if(ranking) ranking.innerHTML=rankingHTML();
    if(active) [...host.querySelectorAll('[data-an-node]')].find(node=>node.dataset.anNode===active)?.focus();
    if(activeRank) [...host.querySelectorAll('[data-an-select]')].find(node=>node.dataset.anSelect===activeRank)?.focus();
  }
  function moveNode(id, x, y) {
    state.positions.set(id,{x:clamp(x,20,W-20),y:clamp(y,20,H-20)});
    if(!host)return;
    for(const el of host.querySelectorAll('[data-an-node]')) if(el.dataset.anNode===id){const p=state.positions.get(id);el.setAttribute('transform',`translate(${p.x} ${p.y})`);}
    const byID=new Map(context.nodes.map(node=>[node.id,node]));
    for(const el of host.querySelectorAll('[data-an-source]')) {
      const a=position(byID.get(el.dataset.anSource)), b=position(byID.get(el.dataset.anTarget));
      for(const [name,value] of Object.entries({x1:a.x,y1:a.y,x2:b.x,y2:b.y})) el.setAttribute(name,value);
    }
    for(const el of host.querySelectorAll('[data-an-cluster]')) {
      const cluster=context.clusters.find(c=>c.id===el.dataset.anCluster), bounds=cluster&&clusterBounds(cluster);if(!bounds)continue;
      const ellipse=el.querySelector('ellipse'), text=el.querySelector('text');
      for(const [name,value] of Object.entries({cx:bounds.x,cy:bounds.y,rx:bounds.rx,ry:bounds.ry})) ellipse?.setAttribute(name,value);
      text?.setAttribute('x',bounds.x);text?.setAttribute('y',bounds.y-bounds.ry-9);
    }
  }
  function svgPoint(event) {
    const svg=host?.querySelector('.an-svg'), rect=svg?.getBoundingClientRect();
    if(!rect?.width || !rect.height)return {x:0,y:0};
    // Account for xMidYMid meet letterboxing at every viewport size.
    const scale=Math.min(rect.width/W,rect.height/H),left=rect.left+(rect.width-W*scale)/2,top=rect.top+(rect.height-H*scale)/2;
    return {x:(event.clientX-left)/scale,y:(event.clientY-top)/scale};
  }
  function pointerdown(event) {
    if(!gesture)suppressClick=false;
    if(event.button!==0||gesture||!event.target.closest('.an-svg'))return;
    const point=svgPoint(event), node=event.target.closest('[data-an-node]'), selected=context.nodes.find(n=>n.id===node?.dataset.anNode);
    const origin=selected ? position(selected) : state.camera;
    gesture={pointer:event.pointerId,id:selected?.id,point,clientX:event.clientX,clientY:event.clientY,x:origin.x,y:origin.y,moved:false};
    suppressClick=false;host.setPointerCapture?.(event.pointerId);
  }
  function pointermove(event) {
    if(!gesture||gesture.pointer!==event.pointerId)return;
    const point=svgPoint(event), dx=point.x-gesture.point.x,dy=point.y-gesture.point.y;
    gesture.moved ||= Math.hypot(event.clientX-gesture.clientX,event.clientY-gesture.clientY)>4;
    if(!gesture.moved)return;
    event.preventDefault();
    if(gesture.id)moveNode(gesture.id,gesture.x+dx/state.camera.k,gesture.y+dy/state.camera.k);
    else{state.camera.x=gesture.x+dx;state.camera.y=gesture.y+dy;applyCamera();}
  }
  function pointerup(event) {
    if(!gesture||gesture.pointer!==event.pointerId)return;
    const previous=gesture;gesture=null;
    host?.releasePointerCapture?.(event.pointerId);
    // Capture may retarget click to the host, so activate short node presses here.
    suppressClick=previous.moved||Boolean(previous.id);
    if(previous.id&&!previous.moved)select(previous.id);
  }
  function pointercancel(event) {
    if(gesture?.pointer===event.pointerId){gesture=null;suppressClick=true;host?.releasePointerCapture?.(event.pointerId);}
  }
  function wheel(event) {
    if(!event.target.closest('.an-svg'))return;
    event.preventDefault();zoom(Math.exp(-clamp(event.deltaY,-200,200)*.002),svgPoint(event));
  }
  function click(event) {
    if(suppressClick&&event.detail!==0){suppressClick=false;event.preventDefault();event.stopPropagation();return;}
    suppressClick=false;
    const target=event.target.closest('[data-an-action],[data-an-select],[data-an-node]');if(!target)return;
    event.stopPropagation();
    if(target.dataset.anSelect){select(target.dataset.anSelect);return;}
    if(target.dataset.anNode){select(target.dataset.anNode);return;}
    switch(target.dataset.anAction){
      case 'zoom-in':zoom(1.25);break;
      case 'zoom-out':zoom(.8);break;
      case 'reset':state.camera={x:0,y:0,k:1};state.positions.clear();refresh();host?.querySelector('[data-an-action="reset"]')?.focus();break;
      case 'clear':select(null);host?.querySelector('.an-svg')?.focus();break;
      case 'detail':if(state.selected)context.onDetail?.(state.selected);break;
    }
  }
  function change(event) {
    const control=event.target.dataset.anControl;if(!control)return;
    event.stopPropagation();
    if(control==='metric'&&Object.hasOwn(labels,event.target.value))state.metric=event.target.value;
    if(control==='top'&&[3,5,10,20].includes(Number(event.target.value)))state.top=Number(event.target.value);
    refresh(control==='metric'?'an-metric':'an-top');
  }
  function keydown(event) {
    const node=event.target.closest('[data-an-node]'),svg=event.target.closest('.an-svg');
    if(!svg)return;
    const arrows={ArrowLeft:[-12,0],ArrowRight:[12,0],ArrowUp:[0,-12],ArrowDown:[0,12]};
    if(!['Enter',' ','Escape','Home','+','=','-',...Object.keys(arrows)].includes(event.key))return;
    event.preventDefault();event.stopPropagation();
    if(event.key==='Escape'){select(null);return;}
    if(node&&['Enter',' '].includes(event.key)){select(node.dataset.anNode);return;}
    if(node&&arrows[event.key]){const n=context.nodes.find(n=>n.id===node.dataset.anNode),p=position(n),[x,y]=arrows[event.key];moveNode(n.id,p.x+x,p.y+y);return;}
    if(arrows[event.key]){const [x,y]=arrows[event.key];state.camera.x+=x*3;state.camera.y+=y*3;applyCamera();}
    else if(['+','='].includes(event.key))zoom(1.25);
    else if(event.key==='-')zoom(.8);
    else if(event.key==='Home'){state.camera={x:0,y:0,k:1};applyCamera();}
  }
  window.AtlasAuthorNetwork={render,mount,unmount,reset};
  if(window.__ATLAS_UI_TEST__)window.__authorInteractiveTest={state,ranked,neighborIDs,metricValue,formatValue,visibleEdges,graphHTML,html,selectedHTML,select,zoom,moveNode,pointerdown,pointermove,pointerup,pointercancel,keydown,click,change,wheel,svgPoint};
})();
