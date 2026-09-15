/* Temporal planes use a single Python projection. Display offsets never enter drift statistics. */
(function(global){
  'use strict';
  const clamp=(v,a,b)=>Math.max(a,Math.min(b,Number(v)||0));
  const fmt=n=>String(Math.round(n*1000)/1000);
  const path=points=>'M'+points.map(p=>p.map(fmt).join(',')).join('L');
  const DEFAULT_CAMERA={yaw:-.16,pitch:.72};
  let orbitGeometry=[];
  const camera=value=>({yaw:clamp(value?.yaw??DEFAULT_CAMERA.yaw,-1.25,1.25),pitch:clamp(value?.pitch??DEFAULT_CAMERA.pitch,.25,1.3)});
  function periodWindow(periods,start=null,size=8){
    const count=Math.max(1,Math.min(12,size)),last=Math.max(0,periods.length-count);
    const offset=start==null?last:clamp(start,0,last);
    return {periods:periods.slice(offset,offset+count),start:offset,last,size:count};
  }
  function project(x,y,z,options={}){
    if(options.camera&&options.mode!=='flat'){
      const c=camera(options.camera),px=(clamp(x,0,1)-.5)*570,py=(clamp(y,0,1)-.5)*310;
      const fraction=options.layerCount>1?clamp(options.layerIndex,0,options.layerCount-1)/(options.layerCount-1):.5;
      const height=options.mode==='layers'?(fraction-.5)*245:clamp(z,0,1)*(options.heightScale||102);
      const rotatedX=Math.cos(c.yaw)*px-Math.sin(c.yaw)*py,depth=Math.sin(c.yaw)*px+Math.cos(c.yaw)*py;
      return [400+rotatedX,options.mode==='layers'?325+Math.sin(c.pitch)*depth-Math.cos(c.pitch)*height:365+Math.sin(c.pitch)*depth-Math.cos(c.pitch)*height];
    }
    if(options.mode!=='layers')return global.AtlasTerrain.project(x,y,z,options);
    const fraction=options.layerCount>1?clamp(options.layerIndex,0,options.layerCount-1)/(options.layerCount-1):.5;
    return [74+clamp(x,0,1)*596+clamp(y,0,1)*50+fraction*22,326+clamp(y,0,1)*174-fraction*232];
  }
  function bind(kind,points,options,offset=[0,0]){
    const id=orbitGeometry.length;orbitGeometry.push({kind,points,options,offset});return `data-orbit-index="${id}"`;
  }
  function referenceGrid(terrain,opt,key){
    if(!opt.referenceGrid)return '';
    const lines=global.AtlasTerrain.referenceGridLines(terrain,opt.mode);
    if(!lines.length)return '';
    return `<g class="landscape-reference-grid" aria-hidden="true" pointer-events="none">${lines.map((points,i)=>`<path data-motion-key="reference-grid-${key}-${i}" ${bind('openpath',points,opt)} d="${path(points.map(p=>project(...p,opt)))}"/>`).join('')}</g>`;
  }
  function terrainSurface(terrain,opt){
    const grid=terrain?.grid;if(!grid||!Array.isArray(grid.values))return '';
    const w=Number(grid.width),h=Number(grid.height);if(!Number.isInteger(w)||!Number.isInteger(h)||w<2||h<2||(w-1)*(h-1)>1600||grid.values.length!==w*h)return '';
    const at=(x,y)=>[x/(w-1),y/(h-1),clamp(grid.values[y*w+x],0,1)],triangles=[];
    // At most 800 surface faces. Density measurements and contours remain unchanged.
    const step=(w-1)*(h-1)>400?2:1;
    for(let y=0;y<h-1;y+=step)for(let x=0;x<w-1;x+=step){const xx=Math.min(w-1,x+step),yy=Math.min(h-1,y+step),a=at(x,y),b=at(xx,y),c=at(xx,yy),d=at(x,yy);for(const points of [[a,b,d],[b,c,d]]){const z=points.reduce((sum,p)=>sum+p[2],0)/3,color=`rgb(${Math.round(14+28*z)},${Math.round(33+64*z)},${Math.round(46+53*z)})`;triangles.push(`<path ${bind('path',points,opt)} d="${path(points.map(p=>project(...p,opt)))}Z" fill="${color}" stroke="${color}"/>`);}}
    const contours=[];let remaining=12000;
    if(opt.contours)for(const contour of (terrain.contours||[]).slice(0,32))for(const raw of (contour.paths||[]).slice(0,512)){if(remaining<=0)break;if(!Array.isArray(raw)||!Number.isFinite(contour.level))continue;const clean=raw.slice(0,Math.min(remaining,4096));remaining-=clean.length;let points=[];const flush=()=>{if(points.length>1)contours.push(`<path class="terrain-contour" ${bind('openpath',points,opt)} d="${path(points.map(p=>project(...p,opt)))}" stroke-opacity=".45"/>`);points=[];};for(const p of clean){if(Array.isArray(p)&&p.length>=2&&p.every(Number.isFinite))points.push([p[0],p[1],clamp(contour.level,0,1)]);else flush();}flush();}
    return `<g class="map-terrain" aria-hidden="true" pointer-events="none"><g class="terrain-surface">${triangles.join('')}</g>${referenceGrid(terrain,opt,'surface')}<g class="terrain-contours">${contours.join('')}</g></g>`;
  }
  function bindOrbit(stage,onChange){
    const svg=stage?.querySelector?.('svg.map-svg');if(!svg||!svg.addEventListener||stage.dataset.mapMode==='flat')return;
    const records=[...svg.querySelectorAll('[data-orbit-index]')].map(el=>({el,...orbitGeometry[Number(el.dataset.orbitIndex)]}));
    const state={camera:camera(records[0]?.options?.camera),drag:null,frame:null,suppressClick:false};
    const paint=()=>{state.frame=null;for(const r of records){if(!r.points)continue;const points=r.points.map(p=>project(...p,{...r.options,camera:state.camera}));const a=points[0],b=points[1];if(r.kind==='point'){r.el.setAttribute('cx',fmt(a[0]));r.el.setAttribute('cy',fmt(a[1]));}else if(r.kind==='text'){r.el.setAttribute('x',fmt(a[0]+r.offset[0]));r.el.setAttribute('y',fmt(a[1]+r.offset[1]));}else if(r.kind==='translate')r.el.setAttribute('transform',`translate(${fmt(a[0])} ${fmt(a[1])})`);else if(r.kind==='label')r.el.setAttribute('transform',`translate(${fmt(a[0]-r.offset[0])} ${fmt(a[1]-r.offset[1])})`);else if(r.kind==='line'){for(const [key,value] of Object.entries({x1:a[0],y1:a[1],x2:b[0],y2:b[1]}))r.el.setAttribute(key,fmt(value));}else r.el.setAttribute('d',path(points)+(r.kind==='path'?'Z':''));}onChange?.(state.camera);};
    svg.addEventListener('pointerdown',event=>{if(event.button!==0||event.isPrimary===false)return;event.preventDefault();state.suppressClick=false;state.drag={id:event.pointerId,x:event.clientX,y:event.clientY,start:camera(state.camera),moved:false};});
    svg.addEventListener('pointermove',event=>{const d=state.drag;if(!d||event.pointerId!==d.id)return;const dx=event.clientX-d.x,dy=event.clientY-d.y;if(!d.moved&&Math.hypot(dx,dy)<5)return;if(!d.moved){d.moved=true;svg.setPointerCapture?.(event.pointerId);stage.dataset.orbiting='true';}event.preventDefault();state.camera=camera({yaw:d.start.yaw+dx*.006,pitch:d.start.pitch-dy*.006});if(state.frame==null)state.frame=global.requestAnimationFrame(paint);});
    const finish=event=>{if(!state.drag||event.pointerId!==state.drag.id)return;state.suppressClick=state.drag.moved;if(svg.hasPointerCapture?.(event.pointerId))svg.releasePointerCapture(event.pointerId);state.drag=null;delete stage.dataset.orbiting;};
    svg.addEventListener('pointerup',finish);svg.addEventListener('pointercancel',finish);
    svg.addEventListener('click',event=>{if(state.suppressClick){state.suppressClick=false;event.preventDefault();event.stopImmediatePropagation();}},true);
    return state;
  }
  function arrowGeometry(movement,options={}){
    if(movement.insufficient_reason==='unclassified_topic'||!movement.from||!movement.to)return null;
    if(![movement.from.x,movement.from.y,movement.to.x,movement.to.y].every(Number.isFinite))return null;
    const distance=Math.hypot(movement.to.x-movement.from.x,movement.to.y-movement.from.y);
    if(!Number.isFinite(distance)||distance===0)return null;
    // Both endpoints are projected onto the earlier plane; time separation cannot create an arrow.
    const height=point=>options.mode==='relief'?global.AtlasTerrain.heightAt(options.terrain,point.x,point.y):0;
    const a=project(movement.from.x,movement.from.y,height(movement.from),options),b=project(movement.to.x,movement.to.y,height(movement.to),options);
    return {from:a,to:b,path:path([a,b]),distance};
  }
  function render(context,data,prefs){
    orbitGeometry=[];
    const {e,num,icon,topicColor,mapLabels,isUnclassified}=context;
    const model=data.map,nodes=(model.nodes||[]).filter(n=>Number.isFinite(n.x)&&Number.isFinite(n.y));
    const win=periodWindow(data.periods||[],prefs.start),periodIndex=new Map(win.periods.map((p,i)=>[p.id,i]));
    const periodByNode=new Map([...nodes.filter(n=>n.period_id).map(n=>[n.id,n.period_id]),...(data.periods||[]).flatMap(p=>(p.node_ids||[]).map(id=>[id,p.id]))]);
    const mode=prefs.mode,terrain=data.terrain,opt={width:800,height:620,pad:48,mode,heightScale:prefs.height/100*524*.3,contours:prefs.contours,referenceGrid:prefs.referenceGrid,camera:camera(prefs.camera)};
    const layerOpt=id=>({...opt,terrain,layerIndex:periodIndex.get(id)??0,layerCount:win.periods.length});
    const coords=n=>project(n.x,n.y,terrain?.node_heights?.[n.id]||0,layerOpt(periodByNode.get(n.id)));
    const visible=mode==='layers'?nodes.filter(n=>periodIndex.has(periodByNode.get(n.id))):nodes;
    const byId=new Map(visible.map(n=>[n.id,n])),papers=new Map(context.result.papers.map(p=>[p.id,p]));
    const highlight=new Set(context.mapHighlight?.ids||[]),showHighlight=context.view==='technology'&&highlight.size>0;
    const topic=prefs.topic||context.topic||data.topics?.[0]?.id;
    const centroids=(data.centroids||[]).filter(c=>periodIndex.has(c.period_id)&&(topic==='all'||c.topic_id===topic));
    const selectedMovements=(data.movements||[]).filter(m=>periodIndex.has(m.from_period)&&periodIndex.has(m.to_period)&&(topic==='all'||m.topic_id===topic));
    const planes=win.periods.map((p,i)=>{
      const options={...opt,layerIndex:i,layerCount:win.periods.length};
      const corners=[[0,0],[1,0],[1,1],[0,1]].map(([x,y])=>project(x,y,0,options)),label=project(0,1,0,options);
      return `<g class="time-plane" opacity="${mode==='layers'?1:0}" pointer-events="none"><path data-motion-key="plane-${e(p.id)}" ${bind('path',[[0,0,0],[1,0,0],[1,1,0],[0,1,0]],options)} d="${path([...corners,corners[0]])}"/>${mode==='layers'?referenceGrid(null,options,e(p.id)):''}<text data-motion-key="period-${e(p.id)}" ${bind('text',[[0,1,0]],options,[5,-8])} x="${label[0]+5}" y="${label[1]-8}">${e(p.label)} · ${num(p.count)}論文</text></g>`;
    }).join('');
    let terrainIndex=0;
    const surface=terrain?.grid&&mode!=='layers'?(mode==='relief'?terrainSurface(terrain,opt):global.AtlasTerrain.render(terrain,opt)).replace(/<path\b([^>]*)>/g,(tag,attrs)=>attrs.includes('terrain-floor')||attrs.includes('data-motion-key')?tag:`<path data-motion-key="terrain-${terrainIndex++}"${attrs}>`):'';
    const edges=(model.edges||[]).map(edge=>{const a=byId.get(edge.source),b=byId.get(edge.target);if(!a||!b||mode==='layers'&&periodByNode.get(a.id)!==periodByNode.get(b.id))return '';const ac=coords(a),bc=coords(b);return `<line data-motion-key="edge-${e(edge.source)}-${e(edge.target)}" ${bind('line',[[a.x,a.y,terrain?.node_heights?.[a.id]||0],[b.x,b.y,terrain?.node_heights?.[b.id]||0]],layerOpt(periodByNode.get(a.id)))} x1="${ac[0]}" y1="${ac[1]}" x2="${bc[0]}" y2="${bc[1]}" stroke="${topicColor(a.topic_id)}" opacity=".12"/>`;}).join('');
    const points=visible.map(n=>{const [x,y]=coords(n),paper=papers.get(n.id),selected=topic==='all'||n.topic_id===topic,lit=highlight.has(n.id),size=2+Math.min(6,Math.log1p(Math.max(0,n.citations||0))*.72);return `${lit?`<circle class="map-highlight-halo" data-motion-key="halo-${e(n.id)}" ${bind('point',[[n.x,n.y,terrain?.node_heights?.[n.id]||0]],layerOpt(periodByNode.get(n.id)))} cx="${x}" cy="${y}" r="${size+5}" fill="none" stroke="${topicColor(n.topic_id)}"/>`:''}<circle class="map-node" data-motion-key="paper-${e(n.id)}" ${bind('point',[[n.x,n.y,terrain?.node_heights?.[n.id]||0]],layerOpt(periodByNode.get(n.id)))} cx="${x}" cy="${y}" r="${size}" fill="${topicColor(n.topic_id)}" opacity="${showHighlight?(lit?1:.12):selected?.92:.24}" data-paper="${e(n.id)}" tabindex="0" role="button" aria-label="${e(n.label)}, ${n.year}年" data-tooltip="${e(n.label)}" data-tooltip-sub="${e(paper?.publication_date||`${n.year}年（出版月不明）`)}"><title>${e(n.label)}</title></circle>`;}).join('');
    const arrows=selectedMovements.map(m=>{const options=layerOpt(m.from_period),g=arrowGeometry(m,options);if(!g)return '';const status=m.status==='shift'?'内容変化の候補':m.status==='insufficient'?'比較データ不足':'内容変化は未確認',gap=m.gap_periods>0?` / ${m.gap_periods}期間の空白あり`:'';return `<path class="centroid-arrow ${m.status!=='shift'?'centroid-arrow-unconfirmed':''} ${prefs.movement===m.id?'selected':''}" data-motion-key="arrow-${e(m.id)}" ${bind('openpath',[[m.from.x,m.from.y,mode==='relief'?global.AtlasTerrain.heightAt(terrain,m.from.x,m.from.y):0],[m.to.x,m.to.y,mode==='relief'?global.AtlasTerrain.heightAt(terrain,m.to.x,m.to.y):0]],options)} d="${g.path}" marker-end="url(#centroid-red-arrow)" data-landscape-movement="${e(m.id)}" role="button" tabindex="0" aria-label="${e(m.from_period)}から${e(m.to_period)}の重心移動。${status}${e(gap)}"><title>${e(m.from_period)} → ${e(m.to_period)} / 同一平面の移動距離 ${fmt(g.distance)} / ${status}${e(gap)}</title></path>`;}).join('');
    const centers=centroids.map(c=>{const [x,y]=project(c.x,c.y,mode==='relief'?global.AtlasTerrain.heightAt(terrain,c.x,c.y):0,layerOpt(c.period_id)),movement=selectedMovements.find(m=>m.to_period===c.period_id&&m.topic_id===c.topic_id)||selectedMovements.find(m=>m.from_period===c.period_id&&m.topic_id===c.topic_id);return `<g class="topic-centroid" data-motion-key="centroid-${e(c.topic_id)}-${e(c.period_id)}" ${bind('translate',[[c.x,c.y,mode==='relief'?global.AtlasTerrain.heightAt(terrain,c.x,c.y):0]],layerOpt(c.period_id))} transform="translate(${x} ${y})" data-landscape-centroid-topic="${e(c.topic_id)}" data-landscape-centroid-period="${e(c.period_id)}" role="button" tabindex="0" ${movement?`data-landscape-movement="${e(movement.id)}"`:''} aria-label="${e(c.period_id)}の重心、${num(c.count)}論文"><circle class="centroid-pulse" r="10" fill="none" stroke="${topicColor(c.topic_id)}"/><circle r="4" fill="#f5fff9" stroke="${topicColor(c.topic_id)}" stroke-width="2"/><path d="M-7 0H7M0-7V7" stroke="${topicColor(c.topic_id)}" stroke-width=".7"/><title>${e(c.period_id)} · 重心 · ${num(c.count)}論文</title>${mode!=='layers'?`<text x="9" y="-9">${e(c.period_id)}</text>`:''}</g>`;}).join('');
    const groups=context.result.topics.filter(t=>!isUnclassified(t)).map(t=>{const members=visible.filter(n=>n.topic_id===t.id);if(!members.length)return null;return {...t,members,cx:members.reduce((s,n)=>s+coords(n)[0],0)/members.length,cy:members.reduce((s,n)=>s+coords(n)[1],0)/members.length};}).filter(Boolean);
    for(const g of groups){const x=g.members.reduce((sum,n)=>sum+n.x,0)/g.members.length,y=g.members.reduce((sum,n)=>sum+n.y,0)/g.members.length,z=g.members.reduce((sum,n)=>sum+(terrain?.node_heights?.[n.id]||0),0)/g.members.length,layerIndex=g.members.reduce((sum,n)=>sum+(periodIndex.get(periodByNode.get(n.id))??0),0)/g.members.length;g.orbitAttribute=bind('label',[[x,y,z]],{...opt,layerIndex,layerCount:win.periods.length},[g.cx,g.cy]);}const labels=mapLabels(groups,800,620);
    return `<div class="map-stage map-terrain-stage temporal-map-stage large" data-map-container data-map-mode="${mode}"><div class="map-corner">SHARED COORDINATE SPACE<br>${e(context.representation)} / ${num(visible.length)} NODES${mode!=='flat'?'<br>ドラッグで回転':''}</div><div class="map-corner bottom">${e(model.method||'SEMANTIC PROJECTION')}<br>${mode==='layers'?'Z = TIME / XY = SAME PROJECTION':mode==='relief'?'HEIGHT = RELATIVE DENSITY':'RELATIVE DISTANCE / NO PHYSICAL UNIT'}</div><div class="map-tools"><button class="icon-button" data-map-zoom="in" aria-label="マップを拡大">${icon('plus')}</button><button class="icon-button" data-map-zoom="out" aria-label="マップを縮小">${icon('minus')}</button><button class="icon-button" data-map-zoom="reset" aria-label="マップをリセット">${icon('target')}</button></div><svg class="map-svg" viewBox="0 0 800 620" role="group" aria-label="技術の時層マップ。共通の座標系で期間別の論文と重心を比較。赤矢印は同一平面上の重心移動です。"><defs><marker id="centroid-red-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#ff6c79"/></marker></defs><g class="map-drawing" transform="translate(400 310) scale(${context.mapZoom}) translate(-400 -310)">${surface}${planes}<g pointer-events="none">${edges}</g>${points}<g class="centroid-arrows">${arrows}</g>${centers}${labels}</g></svg></div>`;
  }
  function interpolateAttribute(from,to,progress){
    const pattern=/-?(?:\d*\.)?\d+(?:e[+-]?\d+)?/gi;
    const a=String(from).match(pattern)||[],b=String(to).match(pattern)||[];
    if(a.length!==b.length||String(from).replace(pattern,'#')!==String(to).replace(pattern,'#'))return to;
    let i=0;return String(to).replace(pattern,value=>fmt(Number(a[i++])+(Number(value)-Number(a[i-1]))*progress));
  }
  function animate(before,after){
    if(!before?.querySelectorAll||!after?.querySelectorAll||global.matchMedia?.('(prefers-reduced-motion: reduce)').matches||!global.requestAnimationFrame)return;
    const old=new Map([...before.querySelectorAll('[data-motion-key]')].map(el=>[el.getAttribute('data-motion-key'),el])),changes=[];
    for(const el of after.querySelectorAll('[data-motion-key]')){const prior=old.get(el.getAttribute('data-motion-key'));if(!prior)continue;for(const name of ['cx','cy','x','y','x1','y1','x2','y2','d','transform']){const from=prior.getAttribute(name),to=el.getAttribute(name);if(from!=null&&to!=null&&from!==to){changes.push({el,name,from,to});el.setAttribute(name,from);}}}
    let start;
    function frame(now){if(!after.isConnected||after.dataset?.orbiting==='true')return;start??=now;const t=Math.min(1,(now-start)/850),ease=1-Math.pow(1-t,3);for(const c of changes)c.el.setAttribute(c.name,t===1?c.to:interpolateAttribute(c.from,c.to,ease));if(t<1)global.requestAnimationFrame(frame);}
    global.requestAnimationFrame(frame);
  }
  global.AtlasLayers=Object.freeze({periodWindow,project,arrowGeometry,render,interpolateAttribute,animate,bindOrbit,camera,DEFAULT_CAMERA});
})(window);
