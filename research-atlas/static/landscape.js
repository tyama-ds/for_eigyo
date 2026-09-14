/* Python supplies density; this module only projects and draws it. */
(() => {
  'use strict';
  const cache=new Map(), preferences={mode:'relief',contours:true,height:65};
  let latest=null;
  const ready=entry=>Boolean(entry?.data?.grid?.values?.length);
  function fetchTerrain(context,retry=false) {
    const id=context.result.id;
    if(!id||(cache.has(id)&&!retry))return;
    const entry={loading:true,data:null,error:''};cache.set(id,entry);
    while(cache.size>8)cache.delete(cache.keys().next().value);
    Promise.resolve().then(()=>context.api(`/api/results/${encodeURIComponent(id)}/terrain`)).then(data=>{
      if(data.version!==1||!data.grid||!Array.isArray(data.grid.values))throw new Error('等高線のデータ形式を確認できませんでした。');
      entry.data=data;
    }).catch(error=>{entry.error=error.message||'等高線を取得できませんでした。';}).finally(()=>{
      entry.loading=false;if(cache.get(id)===entry&&latest?.result.id===id)refresh();
    });
  }
  function options(context,entry) {
    return {width:800,height:context.large?500:350,pad:48,mode:ready(entry)?preferences.mode:'flat',heightScale:preferences.height/100*((context.large?500:350)-96)*.30,contours:preferences.contours,idPrefix:context.large?'terrain-large':'terrain-overview'};
  }
  function controls(context,entry) {
    const disabled=!ready(entry),{e}=context;
    return `<div class="terrain-controls"><div class="terrain-view-switch" role="group" aria-label="ランドスケープの表示方法"><button type="button" data-landscape-mode="flat" aria-pressed="${preferences.mode==='flat'}" ${disabled?'disabled':''}>平面</button><button type="button" data-landscape-mode="relief" aria-pressed="${preferences.mode==='relief'}" ${disabled?'disabled':''}>立体</button></div><label class="terrain-contour-toggle"><input type="checkbox" data-landscape-contours ${preferences.contours?'checked':''} ${disabled?'disabled':''}>等高線</label><label class="terrain-height-control" for="landscape-height">高さ <input id="landscape-height" type="range" min="20" max="100" step="5" value="${preferences.height}" aria-label="ランドスケープの高さ" ${disabled||preferences.mode!=='relief'?'disabled':''}><output id="landscape-height-value" for="landscape-height">${preferences.height}%</output></label><span class="terrain-density-key"><i></i>密度 低 → 高</span></div><div class="terrain-load-status" role="status">${entry?.error?`${e(entry.error)} <button type="button" class="text-link" data-landscape-retry>等高線を再取得</button>`:entry?.loading?'Pythonで論文密度と等高線を計算しています…':''}</div>`;
  }
  function scene(context,entry) {
    const {result:r,e,num,icon,topicColor,mapLabels,isUnclassified}=context;
    const model=r.map||{},nodes=(model.nodes||[]).filter(n=>Number.isFinite(n.x)&&Number.isFinite(n.y));
    const opt=options(context,entry),{width,height}=opt,terrain=ready(entry)?entry.data:null;
    const coords=n=>window.AtlasTerrain.project(Math.max(0,Math.min(1,n.x)),Math.max(0,Math.min(1,n.y)),terrain?.node_heights?.[n.id]||0,opt);
    const byId=new Map(nodes.map(n=>[n.id,n])),paperById=new Map(r.papers.map(p=>[p.id,p]));
    const showHighlight=context.view==='technology'&&Boolean(context.mapHighlight),highlightIds=new Set(showHighlight?context.mapHighlight.ids:[]);
    const groups=r.topics.filter(t=>!isUnclassified(t)).map(t=>{const members=nodes.filter(n=>n.topic_id===t.id);if(!members.length)return null;return {...t,cx:members.reduce((s,n)=>s+coords(n)[0],0)/members.length,cy:members.reduce((s,n)=>s+coords(n)[1],0)/members.length};}).filter(Boolean);
    const edges=(model.edges||[]).map(edge=>{const a=byId.get(edge.source),b=byId.get(edge.target);if(!a||!b)return '';const ac=coords(a),bc=coords(b);return `<line x1="${ac[0]}" y1="${ac[1]}" x2="${bc[0]}" y2="${bc[1]}" stroke="${topicColor(a.topic_id)}" stroke-width=".6" opacity=".16"/>`;}).join('');
    const points=[...nodes].sort((a,b)=>coords(a)[1]-coords(b)[1]).map(n=>{const [x,y]=coords(n),size=2+Math.min(6,Math.log1p(Math.max(0,n.citations||0))*.72),highlighted=highlightIds.has(n.id),paper=paperById.get(n.id);return `${highlighted?`<circle class="map-highlight-halo" cx="${x}" cy="${y}" r="${size+5}" fill="none" stroke="${topicColor(n.topic_id)}" stroke-width="1" opacity=".8"/>`:''}<circle class="map-node" cx="${x}" cy="${y}" r="${size}" fill="${topicColor(n.topic_id)}" opacity="${showHighlight?(highlighted?'1':'.12'):context.view==='technology'&&n.topic_id!==context.topic?.toString()?'.58':'.9'}" stroke="${topicColor(n.topic_id)}" stroke-width=".5" data-paper="${e(n.id)}" tabindex="0" role="button" aria-label="${e(n.label)}, ${n.year}年" data-tooltip="${e(n.label)}" data-tooltip-sub="${e(paper?.publication_date||`${n.year}年（出版月不明）`)} · ${paper?.citations==null?'被引用数未取得':`${num(n.citations)} 被引用`}"><title>${e(n.label)}</title></circle>`;}).join('');
    return `<div class="map-stage map-terrain-stage ${context.large?'large':''}" data-map-container data-map-mode="${opt.mode}"><div class="map-corner">${['nmf','lda'].includes(r.meta.topic_model)?'TOPIC DISTRIBUTION SPACE':'DOCUMENT SIMILARITY SPACE'}<br>${e(context.representation)} / ${num(nodes.length)} NODES</div><div class="map-corner bottom">${e(model.method||'SEMANTIC PROJECTION')}<br>${terrain?(opt.mode==='relief'?'DENSITY RELIEF / ':'')+'RELATIVE DENSITY 0–1':'RELATIVE DISTANCE / NO PHYSICAL UNIT'}</div><div class="map-tools"><button class="icon-button" data-map-zoom="in" aria-label="マップを拡大" title="拡大">${icon('plus')}</button><button class="icon-button" data-map-zoom="out" aria-label="マップを縮小" title="縮小">${icon('minus')}</button><button class="icon-button" data-map-zoom="reset" aria-label="マップをリセット" title="表示をリセット">${icon('target')}</button></div><svg class="map-svg" viewBox="0 0 ${width} ${height}" role="group" aria-label="技術ランドスケープ。高さと等高線は表示論文の相対密度、色はトピック、点の大きさは累積被引用数です。"><g class="map-drawing" transform="translate(${width/2} ${height/2}) scale(${context.mapZoom}) translate(${-width/2} ${-height/2})">${terrain?window.AtlasTerrain.render(terrain,opt):''}<g class="map-similarity-edges" pointer-events="none">${edges}</g>${points}${mapLabels(groups,width,height)}</g></svg></div>`;
  }
  function view(context) {
    latest=context;
    if(!context.result.map?.nodes?.length)return context.empty('マップを生成できませんでした','タイトルや抄録のある論文を公開検索またはCSVから追加してください。');
    fetchTerrain(context);const entry=cache.get(context.result.id);
    return `<div class="landscape-root" data-landscape-result="${context.e(context.result.id)}">${controls(context,entry)}${scene(context,entry)}<p class="terrain-explanation">高さ・等高線は、表示論文の配置を平滑化した相対密度（KDE）です。研究の重要度や未開拓性を示すものではありません。${context.result.map.truncated?'表示用に抽出した論文だけを使用しています。':''}</p></div>`;
  }
  function refresh(sceneOnly=false) {
    if(!latest)return;
    const root=document.querySelector('.landscape-root');
    if(!root||root.dataset.landscapeResult!==latest.result.id)return;
    if(sceneOnly){const stage=root.querySelector('[data-map-container]');if(stage)stage.outerHTML=scene(latest,cache.get(latest.result.id));const output=root.querySelector('#landscape-height-value');if(output)output.textContent=String(preferences.height)+'%';}
    else root.outerHTML=view(latest);
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-landscape-mode],[data-landscape-retry]');if(!button||!latest)return;event.preventDefault();
    if(button.hasAttribute('data-landscape-retry')){fetchTerrain(latest,true);refresh();return;}
    if(['flat','relief'].includes(button.dataset.landscapeMode)){preferences.mode=button.dataset.landscapeMode;refresh();document.querySelector(`[data-landscape-mode="${preferences.mode}"]`)?.focus();}
  });
  document.addEventListener('change',event=>{if(!event.target.matches('[data-landscape-contours]'))return;preferences.contours=event.target.checked;refresh();document.querySelector('[data-landscape-contours]')?.focus();});
  document.addEventListener('input',event=>{if(event.target.id!=='landscape-height')return;preferences.height=Math.max(20,Math.min(100,Number(event.target.value)||65));refresh(true);});
  window.AtlasLandscape=Object.freeze({view});
  if(window.__ATLAS_UI_TEST__)window.__landscapeTest={cache,preferences,fetchTerrain,controls,scene,refresh};
})();
