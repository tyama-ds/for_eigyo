/* Shared document exports and explicit, result-scoped static figure attachments. */
(function (global) {
  'use strict';
  const FORMATS = {pdf:'PDF',docx:'Word（DOCX）',xlsx:'Excel（XLSX）',pptx:'PowerPoint（PPTX）'};
  const KINDS = new Set(['result','corpus','landscape','annual','field','foresight']);
  const MAX_IMAGES=4, MAX_BYTES=2*1024*1024, MAX_SIDE=4096, MAX_PIXELS=12000000;
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const validId=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{1,160}$/.test(value);
  const records=new Map(),attachments=new Map(),captures=new Map(),choices=new Map(),sourceKeys=new WeakMap();
  let sequence=0,snapshotSequence=0,sourceSequence=0,resultEpoch=0,activeResult=null,refreshQueued=false;
  const SOURCES={
    landscape:{selector:'#content .landscape-root[data-landscape-result] [data-map-container] > svg.map-svg',owner:'.landscape-root',key:'landscapeResult',title:'技術ランドスケープ',caption:'表示時点の視点・時間層を固定した静止図。点は論文、色は話題。時層の高さは出版期間、立体の高さは相対密度です。'},
    citation:{selector:'#content #citation-flow-root .citation-flow [data-cf-stage] > svg.cf-svg',owner:'#citation-flow-root',key:'reportResultId',title:'引用の時層マップ',caption:'表示時点の静止図。矢印は新しい引用元から古い参照先へ向かいます。青は新研究、黄は参照した研究基盤の重心。引用関係は因果関係を保証しません。'},
    field:{selector:'#field-report-dialog[open] #field-report-body svg[role="img"]',owner:'#field-report-body',key:'reportResultId',title:'分野別の出版動向',caption:'表示中の分野比較を固定した静止図。取得した論文集合の出版動向です。'},
    foresight:{selector:'#content #foresight-root svg.fs-comparison-chart, #content #foresight-root svg.fs-growth-chart',owner:'#foresight-root',key:'reportResultId',title:'成長と根拠言及の比較',caption:'表示時点の静止図。取得論文の成長と根拠言及の充足度を比較します。成熟度・実用化確率を表しません。'},
    overview:{selector:'#content[data-report-view="overview"] .overview-bottom > section.panel .chart-wrap > svg[role="img"]',owner:'#content',key:'reportResultId',title:'研究活動の推移',caption:'表示時点の年次推移を固定した静止図。取得した論文集合の出版動向です。'},
    citations:{selector:'#content[data-report-view="citations"] .citation-grid .citation-chart > svg[role="img"]',owner:'#content',key:'reportResultId',title:'引用の時系列',caption:'表示時点の引用集計を固定した静止図。未取得の引用は0件として扱いません。'},
    forecast:{selector:'#content[data-report-view="forecasts"] .forecast-grid .forecast-chart > svg[role="img"]',owner:'#content',key:'reportResultId',title:'研究活動の予測シナリオ',caption:'取得した出版数を外挿した静止図。実用化・成功確率を表しません。'},
    authors:{selector:'#content[data-report-view="authors"] #author-map-container .an-interactive .an-graph-stage > svg.an-svg',owner:'#content',key:'reportResultId',title:'研究者の共著ネットワーク',caption:'現在の配置・ズーム・強調を固定した静止図。点は著者、線は共著関係です。距離や面積は研究の近さを表しません。'}
  };
  const PAINT=['color','fill','fill-opacity','fill-rule','stroke','stroke-opacity','stroke-width','stroke-dasharray','stroke-dashoffset','stroke-linecap','stroke-linejoin','stroke-miterlimit','opacity','font-family','font-size','font-weight','font-style','letter-spacing','text-anchor','dominant-baseline','alignment-baseline','paint-order','vector-effect','visibility','display','transform','transform-origin','transform-box','filter','clip-path','mask','marker-start','marker-mid','marker-end','stop-color','stop-opacity','flood-color','flood-opacity'];
  function images(resultId){return attachments.get(resultId)||[];}
  function captureState(resultId){if(!captures.has(resultId))captures.set(resultId,{busy:false,error:'',message:''});return captures.get(resultId);}
  function record(kind,id,resultId,options={}){
    if(!KINDS.has(kind)||!validId(id)||!validId(resultId))return null;
    const candidate=kind==='foresight'&&validId(options.candidate_id)?options.candidate_id:null;
    const identity=JSON.stringify([kind,id,resultId,candidate]);
    if(!records.has(identity))records.set(identity,{key:`rx-${++sequence}`,kind,id,resultId,candidate,format:'pdf',busy:false,error:'',message:''});
    return records.get(identity);
  }
  function findRecord(key){return [...records.values()].find(item=>item.key===key);}
  function scopeFor(kind){return kind==='field'?'field':kind==='foresight'?'foresight':'auto';}
  function render(kind,id,resultId,options={}){
    const item=record(kind,id,resultId,options);if(!item)return '';
    queueRefresh();return exportHTML(item);
  }
  function exportHTML(item){
    return `<div class="report-export" data-rx-key="${item.key}"><div class="rx-format-row"><label>保存形式<select data-rx-format ${item.busy?'disabled':''} aria-label="レポートの保存形式">${Object.entries(FORMATS).map(([value,label])=>`<option value="${value}" ${item.format===value?'selected':''}>${label}</option>`).join('')}</select></label><button type="button" data-rx-action="download" ${item.busy?'disabled':''}>${item.busy?'ファイルを作成中…':'ダウンロード ↓'}</button></div><p class="rx-help">保存済みの本文・集計表・グラフを出力します。${item.candidate?'選択中の候補が対象です。':''}新しいLLM生成は行いません。</p>${captureHTML(item.resultId,scopeFor(item.kind))}${item.error?`<p class="rx-error" role="alert">${esc(item.error)}</p>`:item.message?`<p class="rx-message" role="status">${esc(item.message)}</p>`:''}</div>`;
  }
  function captureControls(resultId,scope='auto'){
    if(!validId(resultId)||scope!=='auto'&&!SOURCES[scope])return '';
    queueRefresh();return captureHTML(resultId,scope);
  }
  function captureHTML(resultId,scope){
    const state=captureState(resultId),items=images(resultId);
    return `<div class="rx-capture" data-rx-result="${esc(resultId)}" data-rx-scope="${esc(scope)}"><div class="rx-source-picker">${sourcePicker(resultId,scope)}</div><div class="rx-capture-heading"><button type="button" data-rx-action="capture" ${state.busy||items.length>=MAX_IMAGES?'disabled':''}>${state.busy?'図を追加中…':'表示中の図を追加'}</button><span>${items.length} / ${MAX_IMAGES}枚</span></div><p class="rx-help">PNGの静止図として添付します。動き・操作は含まれません。同じ分析結果のレポートで利用でき、ページを再読み込みすると消えます。</p>${items.length?`<div class="rx-snapshots">${items.map(item=>`<figure><img src="${item.data_url}" alt="${esc(item.title)}"><figcaption>${esc(item.title)}</figcaption><button type="button" data-rx-action="remove" data-rx-image="${item.id}" aria-label="${esc(item.title)}を添付から削除">削除</button></figure>`).join('')}</div>`:''}${state.error?`<p class="rx-error" role="alert">${esc(state.error)}</p>`:state.message?`<p class="rx-message" role="status">${esc(state.message)}</p>`:''}</div>`;
  }
  function queueRefresh(){if(refreshQueued)return;refreshQueued=true;Promise.resolve().then(()=>{refreshQueued=false;updateAvailability();});}
  function refresh(){
    for(const root of document.querySelectorAll('[data-rx-key]')){const item=findRecord(root.dataset.rxKey);if(item)root.outerHTML=exportHTML(item);}
    for(const root of document.querySelectorAll('[data-rx-result]')){if(!root.closest('[data-rx-key]'))root.outerHTML=captureHTML(root.dataset.rxResult,root.dataset.rxScope||'auto');}
    updateAvailability();
  }
  function visible(svg){
    if(!svg||svg.isConnected===false||svg.getAttribute('aria-hidden')==='true')return false;
    const bounds=svg.getBoundingClientRect();if(!(bounds.width>0&&bounds.height>0))return false;
    const style=global.getComputedStyle(svg);return style.display!=='none'&&style.visibility!=='hidden';
  }
  function findSources(resultId,scope='auto'){
    const kinds=scope==='auto'?['landscape','citation','overview','citations','forecast','authors','foresight']:[scope],found=[],seen=new Set();
    for(const kind of kinds){const definition=SOURCES[kind];if(!definition)continue;
      for(const svg of document.querySelectorAll(definition.selector)){
        const owner=svg.closest(definition.owner);
        if(seen.has(svg)||owner?.dataset?.[definition.key]!==resultId||owner.dataset.rxReady==='false'||!visible(svg))continue;
        seen.add(svg);if(!sourceKeys.has(svg))sourceKeys.set(svg,`chart-${++sourceSequence}`);
        const panel=svg.closest('.panel,.fs-panel'),heading=panel?.querySelector?.('h2,h3')?.textContent?.trim();
        const title=heading||svg.getAttribute('aria-label')||definition.title;
        found.push({svg,kind,...definition,sourceKey:sourceKeys.get(svg),title:String(title).slice(0,200)});
      }
    }
    return found;
  }
  function selectedSource(resultId,scope,sources=findSources(resultId,scope)){
    const key=`${resultId}|${scope}`,selected=sources.find(item=>item.sourceKey===choices.get(key))||sources[0];
    if(selected)choices.set(key,selected.sourceKey);else choices.delete(key);return selected||null;
  }
  function findSource(resultId,scope='auto',sourceKey=null){
    const sources=findSources(resultId,scope);return sourceKey?sources.find(item=>item.sourceKey===sourceKey)||null:selectedSource(resultId,scope,sources);
  }
  function sourcePicker(resultId,scope){
    const sources=findSources(resultId,scope),selected=selectedSource(resultId,scope,sources);
    return sources.length>1?`<label>追加する図<select data-rx-source aria-label="追加する図" ${captureState(resultId).busy?'disabled':''}>${sources.map(item=>`<option value="${item.sourceKey}" ${item===selected?'selected':''}>${esc(item.title)}</option>`).join('')}</select></label>`:selected?`<span class="rx-source-name">対象：${esc(selected.title)}</span>`:'';
  }
  function sourceCaption(source){
    const owner=source.svg.closest(source.owner),panel=source.svg.closest('.panel,.fs-panel'),parts=[source.caption,owner?.dataset.rxCaption];
    const read=(selector,selected=false)=>{const node=owner?.querySelector?.(selector);return selected?node?.selectedOptions?.[0]?.textContent:node?.value??node?.textContent;};
    if(source.kind==='citations')parts.push(read('[data-citation-mode].active'));
    if(source.kind==='forecast')parts.push('表示テーマ：'+(read('.forecast-controls .forecast-topic.active')||'未選択'));
    if(source.kind==='authors')parts.push(read('.author-network-context .source-pill'),read('[data-an-control="metric"]',true),read('[data-an-control="top"]',true),read('#author-filter-summary'),read('.an-map-corner'),read('.an-selected-header h3'));
    parts.push(source.svg.getAttribute('aria-label'),panel?.querySelector?.('.chart-legend,.field-chart-legend')?.textContent,panel?.querySelector?.('.panel-note')?.textContent);
    return parts.filter(Boolean).map(part=>String(part).trim().replace(/\s+/g,' ')).join('\n').slice(0,1200);
  }
  function updateAvailability(){
    for(const root of document.querySelectorAll('[data-rx-result]')){
      const button=root.querySelector('[data-rx-action="capture"]');if(!button)continue;
      const resultId=root.dataset.rxResult,scope=root.dataset.rxScope||'auto',state=captureState(resultId),source=findSource(resultId,scope),picker=root.querySelector('.rx-source-picker'),html=sourcePicker(resultId,scope);
      if(picker&&picker.innerHTML!==html)picker.innerHTML=html;
      button.disabled=state.busy||images(resultId).length>=MAX_IMAGES||!source;
      button.title=!source?'この画面には追加できる対象の図がありません。技術マップや引用の流れで図を追加できます。':images(resultId).length>=MAX_IMAGES?'添付は4枚までです。不要な図を削除してください。':`${source.title}を静止図として追加`;
    }
  }
  function localReference(value,ids,base){
    let fragment;
    if(value.startsWith('#'))fragment=value.slice(1);
    else{let url,current;try{url=new URL(value,base);current=new URL(base);}catch{throw new Error('図の外部参照を除去できませんでした。');}
      if(url.origin!==current.origin||url.pathname!==current.pathname||url.search!==current.search||!url.hash)throw new Error('外部画像・外部参照を含む図は追加できません。');
      fragment=decodeURIComponent(url.hash.slice(1));}
    if(!ids.has(fragment)||/["'()\s]/.test(fragment))throw new Error('図の内部参照を確認できませんでした。');
    return '#'+fragment;
  }
  function safeStyle(value,ids,base){
    if(/@import|expression\s*\(|javascript\s*:/i.test(value))throw new Error('図に利用できないスタイルがあります。');
    return value.replace(/url\(\s*(['"]?)(.*?)\1\s*\)/gi,(_,quote,url)=>`url(${localReference(url,ids,base)})`);
  }
  function serializeSVG(svg){
    const clone=svg.cloneNode(true),originals=[svg,...svg.querySelectorAll('*')],copies=[clone,...clone.querySelectorAll('*')];
    const ids=new Set(copies.map(node=>node.getAttribute('id')).filter(Boolean));
    const base=svg.ownerDocument?.baseURI||global.location.href;
    const disallowed=new Set(['script','foreignobject','iframe','object','embed','style','image','audio','video','animate','animatetransform','animatemotion','set']);
    originals.forEach((original,index)=>{
      const target=copies[index];if(!target)return;
      if(disallowed.has(String(original.localName).toLowerCase()))throw new Error('外部画像・スクリプト・埋め込み要素を含む図は追加できません。');
      target.removeAttribute('style');
      for(const attr of [...target.attributes]){
        const name=attr.name.toLowerCase();
        if(name.startsWith('on')){target.removeAttribute(attr.name);continue;}
        if(name==='href'||name==='xlink:href')target.setAttribute(attr.name,localReference(attr.value,ids,base));
        else if(/url\(/i.test(attr.value))target.setAttribute(attr.name,safeStyle(attr.value,ids,base));
      }
      const style=global.getComputedStyle(original);
      for(const property of PAINT){const value=style.getPropertyValue(property);if(value)target.style.setProperty(property,safeStyle(value,ids,base));}
    });
    const box=svg.viewBox?.baseVal,bounds=svg.getBoundingClientRect(),width=box?.width>0?box.width:bounds.width,height=box?.height>0?box.height:bounds.height;
    if(!Number.isFinite(width)||!Number.isFinite(height)||width<=0||height<=0)throw new Error('図の大きさを確認できませんでした。');
    const scale=1600/Math.max(width,height),w=Math.max(1,Math.round(width*scale)),h=Math.max(1,Math.round(height*scale));
    clone.setAttribute('xmlns','http://www.w3.org/2000/svg');clone.setAttribute('width',String(w));clone.setAttribute('height',String(h));
    if(!clone.getAttribute('viewBox'))clone.setAttribute('viewBox',`0 0 ${width} ${height}`);
    return {text:new XMLSerializer().serializeToString(clone),width:w,height:h};
  }
  async function rasterize(serialized){
    const objectURL=global.URL.createObjectURL(new Blob([serialized.text],{type:'image/svg+xml;charset=utf-8'}));let timeout;
    try{
      const image=new global.Image();
      await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error('図をPNGに変換できませんでした。'));timeout=global.setTimeout(()=>reject(new Error('図の画像変換が時間内に完了しませんでした。')),15000);image.src=objectURL;});
      const canvas=document.createElement('canvas');canvas.width=serialized.width;canvas.height=serialized.height;
      const context=canvas.getContext('2d');if(!context)throw new Error('画像の作成機能を利用できません。');
      context.fillStyle='#0b1725';context.fillRect(0,0,canvas.width,canvas.height);context.drawImage(image,0,0,canvas.width,canvas.height);
      return canvas.toDataURL('image/png');
    }finally{if(timeout)global.clearTimeout(timeout);global.URL.revokeObjectURL(objectURL);}
  }
  function validatePNG(dataURL){
    if(typeof dataURL!=='string'||!/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(dataURL))throw new Error('PNG形式の図を確認できませんでした。');
    const encoded=dataURL.slice(dataURL.indexOf(',')+1),bytes=encoded.length*3/4-(encoded.endsWith('==')?2:encoded.endsWith('=')?1:0);
    if(bytes>MAX_BYTES)throw new Error('図が2MBを超えています。表示内容を減らして追加してください。');
    const decoded=global.atob(encoded);if(decoded.length<24||[137,80,78,71,13,10,26,10].some((byte,index)=>decoded.charCodeAt(index)!==byte)||decoded.slice(12,16)!=='IHDR')throw new Error('PNGの画像情報を確認できませんでした。');
    const uint=offset=>[0,1,2,3].reduce((sum,index)=>sum*256+decoded.charCodeAt(offset+index),0),width=uint(16),height=uint(20);
    if(!width||!height||width>MAX_SIDE||height>MAX_SIDE||width*height>MAX_PIXELS)throw new Error('図の画像サイズが上限を超えています。');
    return {bytes,width,height};
  }
  function addSnapshot(resultId,value){
    if(!validId(resultId)||value.result_id!==resultId)throw new Error('図と分析結果が一致しません。');
    validatePNG(value.data_url);const stored=images(resultId);if(stored.length>=MAX_IMAGES)throw new Error('添付できる図は4枚までです。');
    const next={id:`figure-${++snapshotSequence}`,result_id:resultId,title:String(value.title||'表示中の図').slice(0,200),caption:String(value.caption||'表示時点の静止図').slice(0,1200),data_url:value.data_url};
    attachments.set(resultId,[...stored,next]);while(attachments.size>8)attachments.delete(attachments.keys().next().value);return next;
  }
  async function capture(resultId,scope='auto',sourceKey=null){
    const state=captureState(resultId);if(state.busy)return;
    const epoch=resultEpoch;state.error='';state.message='';state.busy=true;refresh();
    try{
      if(images(resultId).length>=MAX_IMAGES)throw new Error('添付できる図は4枚までです。');
      const source=findSource(resultId,scope,sourceKey);if(!source)throw new Error('選択した図が現在の画面にありません。図を選び直してください。');
      // Freeze the current orbit, geometry and paint before asynchronous decoding.
      const caption=sourceCaption(source);
      const serialized=serializeSVG(source.svg),dataURL=await rasterize(serialized);
      if(epoch!==resultEpoch||source.svg.isConnected===false||source.svg.closest(source.owner)?.dataset?.[source.key]!==resultId)throw new Error('表示する分析結果が変わったため、図の追加を中止しました。');
      addSnapshot(resultId,{result_id:resultId,title:source.title,caption,data_url:dataURL});state.message='静止図を添付しました。同じ分析結果のレポート出力に含まれます。';
    }catch(error){state.error=error.message||'図を追加できませんでした。';}finally{state.busy=false;refresh();}
  }
  function exportPayload(item){return {format:item.format,include_figures:true,...(item.candidate?{candidate_id:item.candidate}:{}),snapshots:images(item.resultId).map(({result_id,title,caption,data_url})=>({result_id,title,caption,data_url}))};}
  function filename(header,item){
    let value=header?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];if(value){try{value=decodeURIComponent(value);}catch{value='';}}
    if(!value)value=header?.match(/filename="([^"]+)"/i)?.[1]||header?.match(/filename=([^;]+)/i)?.[1];
    value=String(value||`${item.kind}-${item.id}.${item.format}`).replace(/[\\/<>:"|?*\x00-\x1f]/g,'_').slice(0,180);
    return value.toLowerCase().endsWith('.'+item.format)?value:`${value}.${item.format}`;
  }
  async function download(item){
    if(!item||item.busy||!FORMATS[item.format])return;
    item.busy=true;item.error='';item.message='';refresh();
    try{
      const body=exportPayload(item);
      // File rendering needs no API keys, LLM settings or proxy headers.
      const response=await global.fetch(`/api/report-exports/${item.kind}/${encodeURIComponent(item.id)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!response.ok){let detail;try{detail=(await response.json()).detail;}catch{}throw new Error(typeof detail==='string'?detail:`ファイルを作成できませんでした (${response.status})`);}
      const blob=await response.blob();if(!blob.size)throw new Error('空のファイルが返されました。');
      const url=global.URL.createObjectURL(blob),anchor=document.createElement('a');
      try{anchor.href=url;anchor.download=filename(response.headers.get('Content-Disposition'),item);document.body.appendChild(anchor);anchor.click();}finally{anchor.remove();global.setTimeout(()=>global.URL.revokeObjectURL(url),1000);}
      item.message=`${FORMATS[item.format]}のダウンロードを開始しました。`;
    }catch(error){item.error=error.message||'ファイルを作成できませんでした。';}finally{item.busy=false;refresh();}
  }
  function handleClick(event){
    const target=event.target.closest?.('[data-rx-action]');if(!target)return;
    const item=findRecord(target.closest('[data-rx-key]')?.dataset.rxKey),captureRoot=target.closest('[data-rx-result]'),resultId=captureRoot?.dataset.rxResult;
    if(target.dataset.rxAction==='download')download(item);
    if(target.dataset.rxAction==='capture'&&validId(resultId))capture(resultId,captureRoot.dataset.rxScope||'auto',captureRoot.querySelector?.('[data-rx-source]')?.value||null);
    if(target.dataset.rxAction==='remove'&&validId(resultId)){attachments.set(resultId,images(resultId).filter(image=>image.id!==target.dataset.rxImage));captureState(resultId).message='図を添付から削除しました。';captureState(resultId).error='';refresh();}
  }
  document.addEventListener('click',handleClick);
  document.addEventListener('change',event=>{if(event.target.matches?.('[data-rx-source]')){const root=event.target.closest('[data-rx-result]');if(root&&findSources(root.dataset.rxResult,root.dataset.rxScope).some(item=>item.sourceKey===event.target.value)){choices.set(`${root.dataset.rxResult}|${root.dataset.rxScope}`,event.target.value);updateAvailability();}return;}if(!event.target.matches?.('[data-rx-format]'))return;const item=findRecord(event.target.closest('[data-rx-key]')?.dataset.rxKey);if(item&&!item.busy&&FORMATS[event.target.value]){item.format=event.target.value;item.error='';item.message='';refresh();}});
  global.addEventListener('atlas:result',event=>{const next=event.detail?.id||null;if(next!==activeResult){activeResult=next;++resultEpoch;}queueRefresh();});
  global.AtlasReportExports=Object.freeze({render,captureControls,refresh:queueRefresh});
  if(global.__ATLAS_UI_TEST__)global.__reportExportsTest={records,attachments,captures,choices,render,captureControls,findSource,findSources,sourcePicker,sourceCaption,serializeSVG,localReference,safeStyle,validatePNG,addSnapshot,exportPayload,filename,download,capture,handleClick,record,images,MAX_BYTES};
})(window);
