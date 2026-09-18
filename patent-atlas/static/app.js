'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const keyOf = c => c.key || (c.kind+':'+c.code);
const colors = ['#7462a3','#ae7959','#678a82','#a07299','#9c9158','#6a7b9f'];
let state, step = 0, activeTab = 'workbench', tool = 'lasso', picked = new Set(), pendingFile, toastTimer, animation, selectionSave = Promise.resolve(), shownJob;
let refinement = {include:[],exclude:[]};
let formatCatalog=[], outputFormat=localStorage.getItem('patent-output-format')||'jplatpat', currentOutput=null, exportRequest=0, viewedQueryId=null;
const outputEdits=new Map();
let pendingImportMode='replace';
const run = fn => async (...args) => { try { await fn(...args); } catch (error) { toast(error.message, true); } };
function toast(message, error = false) { const el = $('#toast'); el.textContent = message; el.className = `toast visible${error?' error':''}`; clearTimeout(toastTimer); toastTimer = setTimeout(()=>el.classList.remove('visible'), error?8500:4000); }
async function api(path, body, options = {}) {
 const response = await fetch('/api'+path, {method:body === undefined?'GET':'POST', ...(body === undefined?{}:{headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}), ...options});
 let result; try { result = await response.json(); } catch { const error=new Error('応答を読み取れません。Pythonサーバーの起動状態を確認してください。'); error.status=response.status; throw error; }
 if (!response.ok) { const error=new Error(typeof result.detail === 'string' ? result.detail : '入力を確認してください。'); error.status=response.status; throw error; }
 return result;
}
function on(selector, event, handler) { $(selector)?.addEventListener(event, run(handler)); }
function busy(button, text, fn) { return (async()=> { const original=button.innerHTML; button.disabled=true; button.textContent=text; try { return await fn(); } finally { button.disabled=false; button.innerHTML=original; } })(); }
function setTab(tab) {
 if(tab===activeTab)return;
 const previous=activeTab; globalThis.PatentAtlasMotion?.cancelAll();
 if(previous==='workbench'&&typeof captureResearchDraft==='function')captureResearchDraft();
 hideClassTooltip(); globalThis.hidePatentRowPreview?.(); if(typeof captureLearningDraft==='function')captureLearningDraft(); if(activeTab==='explore'&&step===0)captureClassDraft(); if(activeTab==='discovery')captureDiscoveryDraft();if(activeTab==='orchestration'&&typeof captureOrchestrationDraft==='function')captureOrchestrationDraft();activeTab=tab;
 $$('.tab').forEach(el=>el.classList.toggle('active',el.dataset.tab===tab)); $$('.view').forEach(el=>el.classList.toggle('active',el.id===tab+'-tab'));
 if(tab==='workbench')renderResearchWorkbench();if(tab==='explore')renderStep(); if(tab==='agent')renderAgent(); if(tab==='discovery')renderDiscovery();if(tab==='orchestration')renderOrchestration();
 if(previous!==tab){const order=['explore','discovery','agent','orchestration','settings'];globalThis.PatentAtlasMotion?.enter($('#'+tab+'-tab'),{direction:order.indexOf(tab)-order.indexOf(previous),kind:'tab'});}
}
function setStep(value) {
 const previous=step;globalThis.PatentAtlasMotion?.cancelAll();hideClassTooltip();globalThis.hidePatentRowPreview?.();if(step===0)captureClassDraft();step=value;picked.clear();renderStep();
 if(previous!==value&&activeTab==='explore')globalThis.PatentAtlasMotion?.enter($('#step-content'),{direction:value-previous,kind:'step'});
 const still=document.documentElement.dataset.atlasMotion==='static'||window.matchMedia('(prefers-reduced-motion: reduce)').matches;
 window.scrollTo({top:0,behavior:still?'instant':'smooth'});
}
function updateChrome() {
 $('#connection-label').textContent = state.settings.provider==='offline'?'オフライン · ローカル保存':state.settings.provider==='local'?'Local LLM 設定済み':'外部API 設定済み';
 $('#footer-info').textContent = `${state.patents.length} 件の特許 · ${state.queries.length} 件の検索式${state.demo?' · 架空のサンプルデータ':''}`;
 $('#agent-count').textContent = `${state.patents.length} 件の特許`;
 const j=state.job, running=j.status==='running'; $('#job-banner').classList.toggle('hidden',!running); $('#job-message').textContent=j.message; $('#job-progress').value=j.progress;
 $('#run-agent').disabled=running;
}
function fillSettings() {
 const s=state.settings; $('#provider').value=s.provider; $('#base-url').value=s.base_url; $('#llm-model').value=s.model; $('#llm-timeout').value=s.llm_timeout??120;
 $('#classification-layout').value=s.classification_layout||'semantic'; describeClassLayout();
 $('#ops-status').textContent=s.ops_key_set&&s.ops_secret_set?'OPS接続情報をメモリに保持中。実際の接続は検索実行時に確認します。':s.ops_key_set||s.ops_secret_set?'キーとシークレットを両方設定してください。':'未設定。CSVでの探索はこのまま使えます。';
 $('#ca-bundle').value=s.ca_bundle; $('#bypass-local').checked=s.bypass_local; $('#transformer-model').value=s.transformer_model; $('#allow-download').checked=s.allow_model_download;
 $('#key-status').textContent=s.api_key_set?'キーをメモリに保持中。空欄のまま保存すると維持します。':'APIキーは未設定です。';
 $('#proxy-status').textContent=s.proxy_set?'プロキシ設定をメモリに保持中。':'未設定。LLM通信はプロキシを経由しません。';
}
async function saveSettings() {
 await selectionSave;
 const llmTimeout=Number($('#llm-timeout').value);if(!Number.isInteger(llmTimeout)||llmTimeout<30||llmTimeout>600)throw new Error('LLM応答の待ち時間は30〜600秒の整数で指定してください。');
 state=await api('/settings',{classification_layout:$('#classification-layout').value,ops_key:$('#ops-key').value,ops_secret:$('#ops-secret').value,provider:$('#provider').value,base_url:$('#base-url').value,model:$('#llm-model').value,llm_timeout:llmTimeout,api_key:$('#api-key').value,proxy:$('#proxy-url').value,ca_bundle:$('#ca-bundle').value,bypass_local:$('#bypass-local').checked,transformer_model:$('#transformer-model').value,allow_model_download:$('#allow-download').checked});
 $('#api-key').value=''; $('#proxy-url').value=''; $('#ops-key').value=''; $('#ops-secret').value='';fillSettings(); updateChrome();
}
function renderStep() {
 if(typeof captureLearningDraft==='function')captureLearningDraft();globalThis.hidePatentRowPreview?.();
 cancelAnimationFrame(animation); $$('.step').forEach(el=>el.classList.toggle('active',Number(el.dataset.step)===step));
 const root=$('#step-content'); globalThis.PatentAtlasMotion?.cancel(root);
 [renderClassifications,renderQuery,renderMap,renderRefinement][step](); updateChrome();
}
function enableLasso(svg,pathSelector,nodeSelector,select,done) {
 if(svg.dataset.lassoBound)return;svg.dataset.lassoBound='1';
 let points=[],down=false,start,dragged=false;const path=$(pathSelector,svg);
 const point=e=>new DOMPoint(e.clientX,e.clientY).matrixTransform(svg.getScreenCTM().inverse());
 svg.addEventListener('pointerdown',e=>{if(tool!=='lasso'||e.button!==0)return;down=true;dragged=false;start=point(e);points=[start];svg.dataset.drawing='1';});
 svg.addEventListener('pointermove',e=>{if(!down)return;const p=point(e);if(Math.hypot(p.x-start.x,p.y-start.y)>5){dragged=true;svg.setPointerCapture(e.pointerId);}if(dragged){points.push(p);path.setAttribute('d','M'+points.map(q=>q.x+','+q.y).join(' L')+' Z');}});
 const finish=e=>{if(!down)return;down=false;delete svg.dataset.drawing;path.setAttribute('d','');if(dragged&&points.length>3){svg.dataset.dragged='1';$$(nodeSelector,svg).forEach(node=>{const circle=node.tagName.toLowerCase()==='circle'?node:$('circle',node);const box=circle.getBoundingClientRect();const p=new DOMPoint(box.x+box.width/2,box.y+box.height/2).matrixTransform(svg.getScreenCTM().inverse());if(insidePolygon(p,points))select(node);});done();setTimeout(()=>delete svg.dataset.dragged,0);}if(svg.hasPointerCapture(e.pointerId))svg.releasePointerCapture(e.pointerId);};
 svg.addEventListener('pointerup',finish);svg.addEventListener('pointercancel',e=>{down=false;delete svg.dataset.drawing;path.setAttribute('d','');});
}
function insidePolygon(p,poly){let yes=false;for(let i=0,j=poly.length-1;i<poly.length;j=i++){const a=poly[i],b=poly[j];if(((a.y>p.y)!==(b.y>p.y))&&(p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x))yes=!yes;}return yes;}
function renderQuery() {
 const q=state.queries.find(q=>q.id===viewedQueryId)||state.queries.at(-1);
 $('#step-content').innerHTML=`<div class="query-layout"><div class="panel">${queryCard(q)}</div><div class="panel"><span class="eyebrow">BRING YOUR RESULTS</span><h2>検索結果を、次のヒントに。</h2><p class="muted">検索サイトで調べた特許群をCSVで渡してください。近い技術ごとにマップをつくります。</p><label>取り込み方法<select id="csv-import-mode"><option value="replace">現在の特許群を置き換える</option><option value="merge">追加して既存の判定を保持する</option></select></label><div class="dropzone" id="dropzone" role="button" tabindex="0" aria-label="特許CSVを選択"><span class="upload-symbol">↓</span><b>CSVをここにドロップ</b><p>またはクリックしてファイルを選択</p><p>UTF-8 / Shift-JIS · 20MB・5,000行まで</p></div><input id="csv-file" type="file" accept=".csv,.tsv,text/csv" class="hidden"><h3>読み込める項目</h3><div class="schema"><span>発明の名称 *</span><span>要約</span><span>公報番号</span><span>出願人</span><span>IPC</span><span>FI</span><span>F-term</span><span>公開日</span><span>ファミリーID</span></div><p class="caption">* 発明の名称は必須です。日本語・英語の一般的な列名を自動認識し、不明な場合は割り当てを表示します。同一公報番号は重複を除きます。</p>${state.patents.length?`<div class="upload-status">✓ ${state.patents.length} 件を読み込み済み ${state.demo?'（架空のサンプル）':''}${state.import_info?`<br>重複除外 ${state.import_info.duplicates} 件 · 名称なし ${state.import_info.skipped} 件`:''}</div><button class="button primary wide" id="open-map">特許の地図を見る →</button>`:''}<p class="caption">「置き換え」は現在の特許群と判定を置き換えます。「追加」は同じ公報・内容の判定を保持し、新しい特許を追加します。FIとFタームは別項目で取り込みます。</p></div></div>${historyHtml()}`;
 bindQuery(q);bindHistory();on('#open-map','click',()=>setStep(2));on('#dropzone','click',()=>$('#csv-file').click());on('#dropzone','keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('#csv-file').click();}});on('#csv-file','change',e=>uploadFile(e.target.files[0]));
 const dz=$('#dropzone');dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('dragover');});dz.addEventListener('dragleave',()=>dz.classList.remove('dragover'));dz.addEventListener('drop',run(async e=>{e.preventDefault();dz.classList.remove('dragover');await uploadFile(e.dataTransfer.files[0]);}));
}
async function uploadFile(file,mapping={}) {
 if(!file)return;if(file.size>20*1024*1024)throw new Error('CSVは20MB以下にしてください。');pendingFile=file;pendingImportMode=(typeof researchImportPending!=='undefined'&&researchImportPending)?'merge':($('#csv-import-mode')?.value||pendingImportMode);toast('CSVを読み込み、特許を配置しています…');
 const data=await api('/upload?merge='+(pendingImportMode==='merge')+'&mapping='+encodeURIComponent(JSON.stringify(mapping)),undefined,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:await file.arrayBuffer()});
 if(data.needs_mapping){showMapping(data);return;}state=data;picked.clear();$('#mapping-dialog').close();if(typeof researchImportPending!=='undefined'&&researchImportPending){researchImportPending=false;setTab('workbench');renderResearchWorkbench();updateChrome();}else setStep(2);toast(`${state.patents.length} 件を読み込みました。`);
}
function showMapping(data) {const fields={title:'発明の名称 *',abstract:'要約',id:'公報番号',applicant:'出願人',ipc:'IPC',fi:'FI',fterm:'F-term',cpc:'CPC',year:'公開日',family:'ファミリーID'};$('#mapping-fields').innerHTML=Object.entries(fields).map(([k,v])=>`<label>${v}<select data-field="${k}" ${k==='title'?'required':''}><option value="">割り当てなし</option>${data.headers.map(h=>`<option value="${esc(h)}" ${data.mapping[k]===h?'selected':''}>${esc(h)}</option>`).join('')}</select></label>`).join('');$('#mapping-dialog').showModal();}
function counts(){return {keep:state.patents.filter(p=>p.label==='keep').length,exclude:state.patents.filter(p=>p.label==='exclude').length};}
function renderMap() {
 if(!state.patents.length){$('#step-content').innerHTML='<div class="panel empty-state"><div class="empty-orbit">◎</div><h2>まだ、特許の地図はありません。</h2><p>Step 01からCSVを読み込むか、サンプルを試してください。</p><button id="to-import" class="button primary">CSVを読み込む →</button></div>';on('#to-import','click',()=>setStep(1));return;}
 const c=counts();
 $('#step-content').innerHTML=`<div class="map-layout"><div class="panel canvas-panel map-panel"><div class="canvas-header"><div><h3>特許のランドスケープ ${state.demo?'<span class="demo-badge">架空のサンプル</span>':''}</h3><p>${state.patents.length} 件 · ${state.clusters.length} クラスタ · タイトルと要約の類似度</p></div><div class="canvas-toolbar"><button id="map-lasso-tool" class="icon-button ${tool==='lasso'?'active':''}">◌ 囲んで選択</button><button id="map-clear" class="icon-button">選択解除</button></div></div><div class="canvas-wrap"><svg id="map-svg" class="constellation ${tool==='lasso'?'lasso-mode':''}" viewBox="0 0 1000 580" role="group" aria-label="特許類似度マップ"><g id="map-clusters"></g><g id="map-points"></g><path id="map-lasso" class="lasso-path"/></svg></div><div class="canvas-footer"><div class="legend"><span><i style="background:var(--green)"></i>必要</span><span><i style="background:var(--pink)"></i>不要</span><span><i style="background:var(--blue)"></i>未判定は群別色</span></div><span>2D配置：TF-IDF / SVD · 軸に物理的な意味はありません</span></div></div><div class="map-side"><div class="panel"><div class="map-stats"><div class="stat"><strong>${state.patents.length}</strong><small>読み込み</small></div><div class="stat keep"><strong>${c.keep}</strong><small>必要</small></div><div class="stat exclude"><strong>${c.exclude}</strong><small>不要</small></div></div><h3>選択中 <span id="picked-count" class="pill">${picked.size} 件</span></h3><div class="map-selection-actions" style="margin-top:14px"><button id="label-keep" class="button positive">✓ 必要</button><button id="label-exclude" class="button danger">× 不要</button><button id="label-reset" class="button small">判定解除</button></div><p class="caption">点を囲むかクリックして、一括で判定できます。キーボード操作は下の特許リストから。</p></div><div class="panel" id="patent-detail"><h3>特許をのぞく</h3><div class="map-detail-empty">点にマウスを重ねると、<br>内容と配置の理由が見えます。</div></div></div></div><div class="panel learning-panel"><div class="panel-heading"><div><span class="eyebrow">LEARN FROM YOUR DECISIONS</span><h3 style="margin-top:8px">判断を学び、関連度を見つける。</h3></div><button class="button subtle" id="next-refine">次の検索式へ →</button></div>${learningControlsHtml()}<div id="training-results">${trainingHtml()}</div></div><div class="panel patent-list"><div class="panel-heading"><div><h3>特許リスト</h3><p class="caption" style="margin:4px 0 0">行にマウスを重ねると、要約と判定理由を確認できます。</p></div><div class="filter-row"><input id="patent-search" placeholder="名称・公報番号で絞り込み" aria-label="特許を検索"><select id="label-filter" aria-label="判定で絞り込み"><option value="all">すべて</option><option value="keep">必要</option><option value="exclude">不要</option><option value="unlabeled">未判定</option></select></div></div><div class="table-scroll"><table><thead><tr><th>公報番号</th><th>発明の名称</th><th>関連度</th><th>判定</th><th>変更</th></tr></thead><tbody id="patent-rows"></tbody></table></div></div>`;
 drawMap();bindLearningControls();renderPatentRows();
 on('#map-lasso-tool','click',()=>{tool=tool==='lasso'?'click':'lasso';$('#map-svg').classList.toggle('lasso-mode',tool==='lasso');$('#map-lasso-tool').classList.toggle('active',tool==='lasso');});
 on('#map-clear','click',()=>{picked.clear();updatePicked();});
 ['keep','exclude','reset'].forEach(label=>on('#label-'+label,'click',()=>applyLabels([...picked],label==='reset'?null:label)));
 on('#next-refine','click',()=>setStep(3));on('#patent-search','input',renderPatentRows);on('#label-filter','change',renderPatentRows);
 if(typeof mountProjectionWorkbench==='function')mountProjectionWorkbench();
}
function pointPosition(r,i) { const jitterX=(Math.sin(i*13.17)*.5+.5)*26-13, jitterY=(Math.cos(i*19.13)*.5+.5)*26-13;return {x:r.x*920+40+jitterX,y:r.y*490+35+jitterY}; }
function drawMap() {
 const rows=state.patents;
 $('#map-points').innerHTML=rows.map((r,i)=>{const p=pointPosition(r,i);return `<circle class="point ${r.label||''} ${picked.has(r.id)?'selected':''}" data-i="${i}" data-id="${esc(r.id)}" cx="${p.x}" cy="${p.y}" r="${rows.length>700?3.5:6.5}" style="${!r.label?'fill:'+colors[r.cluster%colors.length]:''}" role="button" tabindex="0" aria-label="${esc(r.id+' '+r.title)}" aria-pressed="${picked.has(r.id)}"/>`;}).join('');
 $('#map-clusters').innerHTML=state.clusters.map(c=>{const group=rows.filter(r=>r.cluster===c.id);const x=group.reduce((a,r)=>a+r.x,0)/group.length*920+40;const y=Math.max(23,Math.min(...group.map(r=>r.y))*490+9);return `<text class="cluster-label" x="${x}" y="${y}" text-anchor="middle">${esc(c.name)} · ${c.count}</text>`;}).join('');
 $$('.point').forEach(el=>{const row=rows[Number(el.dataset.i)];el.addEventListener('mouseenter',()=>showPatent(row));el.addEventListener('focus',()=>showPatent(row));el.addEventListener('click',()=>{if(!$('#map-svg').dataset.dragged){picked.has(row.id)?picked.delete(row.id):picked.add(row.id);updatePicked();showPatent(row);}});el.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();picked.has(row.id)?picked.delete(row.id):picked.add(row.id);updatePicked();}});});
 enableLasso($('#map-svg'),'#map-lasso','.point',n=>picked.add(n.dataset.id),updatePicked);
}
function updatePicked(){if(!$('#picked-count'))return;$('#picked-count').textContent=picked.size+' 件';$$('.point').forEach(n=>{n.classList.toggle('selected',picked.has(n.dataset.id));n.setAttribute('aria-pressed',String(picked.has(n.dataset.id)));});}
function updateMapAssessment(){
 const c=counts();for(const label of ['keep','exclude'])if($('.map-stats .'+label+' strong'))$('.map-stats .'+label+' strong').textContent=c[label];
 $$('.point').forEach(node=>{const row=state.patents.find(r=>r.id===node.dataset.id);if(!row)return;for(const label of ['keep','exclude'])node.classList.toggle(label,row.label===label);node.style.fill=row.label?'':colors[row.cluster%colors.length];});
 const focused=$('#patent-detail')?.dataset.patentId;if(focused){const row=state.patents.find(r=>r.id===focused);if(row)showPatent(row);}
 if(typeof updateProjectionAssessment==='function')updateProjectionAssessment();
}
function showPatent(row) {
 row=state.patents.find(r=>r.id===row?.id)||row;if(!row)return;$('#patent-detail').dataset.patentId=row.id;
 const currentPlacement=typeof atlasProjection!=='undefined'&&atlasProjection.data?atlasProjection.data.metadata.algorithm+'：名称・要約の近さを配置。方式ごとに軸と距離の尺度が変わります。':row.map_reason;
 $('#patent-detail').innerHTML=`<div class="detail-id">${esc(row.id)} ${state.demo?'· 架空':''}</div><h3 class="detail-title">${esc(row.title)}</h3><p class="detail-text">${esc(row.abstract||'要約がありません。')}</p><div class="detail-block"><b>出願人・分類</b>${esc(row.applicant||'不明')}<br>IPC：${esc(row.ipc||'未取込')}${row.fi?`<br>FI：${esc(row.fi)}`:''}${row.fterm?`<br>Fターム：${esc(row.fterm)}`:''}${row.cpc?`<br>CPC：${esc(row.cpc)}`:''}</div><div class="detail-block"><b>なぜ、このあたり？</b>${esc(currentPlacement)}<br>${esc(row.inclusion_reason)}<br>群：${esc(state.clusters.find(c=>c.id===row.cluster)?.name||'')}</div>${row.label_reason?`<div class="detail-block"><b>${row.label_source==='agent'?'エージェント':'あなた'}の判断</b>${esc(row.label_reason)}</div>`:''}${row.score!=null?`<div class="detail-block"><b>${row.score_source==='llm'?'LLMの関連度（自己評価）':'モデルの関連度'} ${Math.round(row.score*100)} / 100</b>検索関連性の保証された確率ではありません。</div>`:''}`;
}
async function applyLabels(ids,label) {if(!ids.length)throw new Error('先に特許を選んでください。');state=await api('/labels',{ids,label});picked.clear();renderStep();toast(`${ids.length} 件を${label==='keep'?'必要':label==='exclude'?'不要':'未判定'}にしました。`);}
function renderPatentRows() {const disabled=state.job?.status==='running'||typeof learningStarting!=='undefined'&&learningStarting?' disabled':'';const text=($('#patent-search')?.value||'').toLowerCase(),filter=$('#label-filter')?.value||'all';const rows=state.patents.filter(r=>(r.title+' '+r.id).toLowerCase().includes(text)&&(filter==='all'||filter==='unlabeled'&&!r.label||r.label===filter));$('#patent-rows').innerHTML=rows.slice(0,500).map(r=>`<tr data-patent-id="${esc(r.id)}" tabindex="0" aria-describedby="patent-row-tooltip"><td>${esc(r.id)}</td><td>${esc(r.title)}</td><td>${r.score==null?'—':Math.round(r.score*100)}</td><td>${r.label==='keep'?'必要':r.label==='exclude'?'不要':'未判定'}${r.label_source==='agent'?' · AI':''}</td><td><div class="button-row"><button class="button small positive row-label" data-id="${esc(r.id)}" data-label="keep"${disabled}>必要</button><button class="button small danger row-label" data-id="${esc(r.id)}" data-label="exclude"${disabled}>不要</button><button class="button small row-track" data-id="${esc(r.id)}" aria-label="${esc(r.id)}を地図で追跡">追跡</button></div></td></tr>`).join('')||'<tr><td colspan="5">該当する特許はありません。</td></tr>';if(rows.length>500)$('#patent-rows').insertAdjacentHTML('beforeend','<tr><td colspan="5">先頭500件を表示しています。絞り込みをご利用ください。</td></tr>');$$('.row-label').forEach(el=>el.addEventListener('click',run(()=>applyLabels([el.dataset.id],el.dataset.label))));$$('.row-track').forEach(el=>el.addEventListener('click',()=>{if(typeof projectionTrackPatent==='function')projectionTrackPatent(el.dataset.id);}));globalThis.bindPatentRowPreviews?.($('#patent-rows'),rows.slice(0,500));}
function trainingHtml() {
 const t=state.training;if(!t)return '<p class="training-description">学習・判定を実行すると、結果をここに表示します。</p>';
 if(t.mode==='llm')return `<p class="training-description"><b>LLMによる要否判定</b> · 処理 ${Number(t.completed_count||0)} / ${Number(t.total_count||0)} 件 · 判定採用 ${Number(t.judged_count||0)} 件 · 保留 ${Number(t.deferred_count||0)} 件</p><p class="caption">${esc(t.evaluation_note||'モデルの重みを学習した結果ではありません。関連度・信頼度はLLMの自己評価で、独立評価は行っていません。')}</p>`;
 return `<p class="training-description">${esc(t.method)} · 学習 ${t.train_count} 件${t.includes_agent_labels?' · AI判断を含む':''}</p>${t.metrics?`<div class="learning-results">${[['必要クラスの適合率',t.metrics.precision],['必要クラスの再現率',t.metrics.recall],['F1',t.metrics.f1]].map(([name,value])=>`<div class="metric"><small>${name}</small><strong>${Math.round(value*100)}%</strong></div>`).join('')}<div class="metric"><small>独立評価</small><strong>${t.metrics.n} 件</strong></div></div>`:''}<p class="caption">${esc(t.evaluation_note)} 同一ファミリーIDの公報は学習・評価間で分けません。IDがない場合は同文書単位で分割します。</p>`;
}
function renderRefinement(){renderRefinementWorkspace();}
function renderAgent(){if(!$('#agent-keywords').value)$('#agent-keywords').value=state.keywords;$('#agent-log').innerHTML=state.agent_log.length?state.agent_log.map(l=>`<div class="log-entry"><time>${esc(l.time)}</time><span>${esc(l.message)}</span></div>`).join(''):'<div class="empty-state"><div class="empty-orbit">◎</div><h3>基準を決めたら、探索を開始。</h3><p>判定理由と進捗がここに残ります。</p></div>';$('#agent-log').scrollTop=$('#agent-log').scrollHeight;}
function syncRunningLlmJudgments(fresh){
 const job=fresh.job,training=fresh.training,previous=state.training;
 if(job?.status!=='running'||job.kind!=='training'||job.mode!=='llm'||job.id!==state.job?.id||training?.mode!=='llm'||training.job_id!==job.id)return false;
 const oldCount=previous?.job_id===job.id?Number(previous.completed_count)||0:0;
 if(!Number.isFinite(training.completed_count)||training.completed_count<=oldCount)return false;
 const latest=new Map((fresh.patents||[]).map(row=>[row.id,row]));
 if(latest.size!==state.patents.length||state.patents.some(row=>!latest.has(row.id)))return false;
 // Keep the objects used by map listeners and row previews, and every DOM row.
 for(const row of state.patents)Object.assign(row,latest.get(row.id));
 return true;
}
function updatePatentAssessmentCells(){
 const byId=new Map(state.patents.map(row=>[row.id,row]));
 for(const node of $$('#patent-rows tr[data-patent-id]')){
  const row=byId.get(node.dataset.patentId);if(!row||node.cells.length<4)continue;
  node.cells[2].textContent=row.score==null?'—':Math.round(row.score*100);
  node.cells[3].textContent=(row.label==='keep'?'必要':row.label==='exclude'?'不要':'未判定')+(row.label_source==='agent'?' · AI':'');
 }
}
let jobRefreshPending=false;
async function refreshJob(){
 if(jobRefreshPending)return;jobRefreshPending=true;
 try{
  const requestedJob=state.job?.id;
  const fresh=await api('/state'),previous=state.job.status;
  if(state.job?.id!==requestedJob||previous==='running'&&requestedJob&&fresh.job?.id!==requestedJob)return;
  if(fresh.training?.job_id===state.training?.job_id&&fresh.training?.job_id===fresh.job?.id&&Number(fresh.training.completed_count)<Number(state.training?.completed_count))return;
 const orchestrationPoll=activeTab==='orchestration'||fresh.job?.kind==='orchestration'||state.job?.kind==='orchestration';
 let judgmentsChanged=syncRunningLlmJudgments(fresh);
 if(orchestrationPoll&&fresh.patents){const latest=new Map(fresh.patents.map(row=>[row.id,row]));if(latest.size===state.patents.length&&state.patents.every(row=>latest.has(row.id))){for(const row of state.patents)Object.assign(row,latest.get(row.id));judgmentsChanged=true;}else state.patents=fresh.patents;for(const field of ['candidates','selected','classification_view','queries','clusters'])if(fresh[field]!==undefined)state[field]=fresh[field];}
 state.job=fresh.job;state.discovery=fresh.discovery;state.orchestration=fresh.orchestration;state.convergence=fresh.convergence;state.search_result=fresh.search_result;updateChrome();
 if(activeTab==='orchestration'&&typeof renderConvergence==='function')renderConvergence();
  if(activeTab==='discovery')renderDiscovery();
  if(state.job.status==='running'){
   state.agent_log=fresh.agent_log;state.training=fresh.training;
   if(activeTab==='agent')renderAgent();if(activeTab==='orchestration')renderOrchestrationProgress();
   if(activeTab==='explore'&&step===2){
    if(judgmentsChanged){updateMapAssessment();updatePatentAssessmentCells();}
    renderLearningProgress();if($('#training-results'))$('#training-results').innerHTML=trainingHtml();
   }
  }else if(previous==='running'||shownJob!==state.job.id&&state.job.id){
   shownJob=state.job.id;if(typeof captureLearningDraft==='function')captureLearningDraft();state=fresh;updateChrome();
   if(activeTab==='explore'&&step===2){
    // Keep form values, list filters, hover targets, map position and selection.
    renderLearningProgress();$('#training-results').innerHTML=trainingHtml();renderPatentRows();updateMapAssessment();updatePicked();
   }else if(activeTab==='explore')renderStep();
   if(activeTab==='agent')renderAgent();if(activeTab==='discovery')renderDiscovery();if(activeTab==='orchestration')renderOrchestrationProgress();
   if(state.job.status==='error')toast(state.job.error,true);else if(state.job.status==='cancelled')toast('処理を停止しました。');else if(state.orchestration?.status==='review')toast('統括ワークフローで選択内容を確認してください。');else if(state.orchestration?.status==='waiting_csv')toast('検索式を更新しました。次のCSVを追加できます。');else toast('処理が完了しました。');
  }
 }finally{jobRefreshPending=false;}
}
function download(name,text){const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function init(){
 [state,formatCatalog]=await Promise.all([api('/state'),api('/query/formats')]);fillSettings();updateChrome();renderStep();initResearchWorkbench();
 let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(activeTab==='explore'&&step===0&&$('#class-svg')){drawClassNodes();updateSelectedClasses();renderClassFocus();}},120);});
 $$('.tab').forEach(el=>el.addEventListener('click',()=>setTab(el.dataset.tab)));$$('.step').forEach(el=>el.addEventListener('click',()=>setStep(Number(el.dataset.step))));
 on('#demo-button','click',async e=>busy(e.currentTarget,'準備中…',async()=>{state=await api('/demo',{});resetClassExplorer();picked.clear();setTab('explore');setStep(0);toast('架空のサンプル72件を読み込みました。Step 02で特許マップを試せます。');}));
 on('#settings-form','submit',async e=>{e.preventDefault();await saveSettings();toast('設定を保存しました。');});on('#test-connection','click',async e=>busy(e.currentTarget,'接続中…',async()=>{await saveSettings();const r=await api('/settings/test',{});toast(r.message);}));
 on('#classification-layout','change',describeClassLayout);
 on('#save-layout','click',async e=>busy(e.currentTarget,'保存中…',async()=>{await selectionSave;const fresh=await api('/settings',{classification_layout:$('#classification-layout').value});state.settings=fresh.settings;describeClassLayout();if(activeTab==='explore'&&step===0){captureClassDraft();renderStep();}toast('配置を保存しました。「対話で探索」で確認できます。');}));
 on('#save-ops','click',async e=>busy(e.currentTarget,'保存中…',async()=>{const fresh=await api('/settings',{ops_key:$('#ops-key').value,ops_secret:$('#ops-secret').value});state.settings=fresh.settings;$('#ops-key').value='';$('#ops-secret').value='';fillSettings();toast('OPS接続情報をメモリに保存しました。');}));
 on('#clear-ops','click',async()=>{const fresh=await api('/settings',{clear_ops:true});state.settings=fresh.settings;$('#ops-key').value='';$('#ops-secret').value='';fillSettings();toast('OPS接続情報を消去しました。');});
 on('#provider','change',e=>{if(e.target.value==='local'){$('#base-url').value='http://localhost:11434/v1';$('#llm-model').value='qwen3:8b';}else if(e.target.value==='openai'){$('#base-url').value='https://api.openai.com/v1';$('#llm-model').value='';}});
 on('#clear-secrets','click',async()=>{state=await api('/settings',{clear_secrets:true});fillSettings();toast('APIキーとプロキシ設定を消去しました。');});
 on('#cancel-mapping','click',()=>{if(typeof researchImportPending!=='undefined')researchImportPending=false;$('#mapping-dialog').close();});on('#mapping-form','submit',async e=>{e.preventDefault();const mapping=Object.fromEntries($$('#mapping-fields select').map(s=>[s.dataset.field,s.value]));await uploadFile(pendingFile,mapping);});
 on('#cancel-classification','click',()=>$('#classification-dialog').close());on('#classification-form','submit',async e=>{e.preventDefault();state=await api('/classification',{kind:$('#new-class-kind').value,code:$('#new-class-code').value,title:$('#new-class-title').value});$('#classification-dialog').close();renderStep();});
 on('#view-agent-map','click',()=>{setTab('explore');setStep(2);});on('#stop-job','click',async()=>{const r=await api('/job/stop',{});toast(r.message);});
 on('#run-agent','click',async e=>busy(e.currentTarget,'開始しています…',async()=>{const words=$('#agent-keywords').value.trim();if(words!==state.keywords){state=await api('/candidates',{keywords:words,use_llm:false});}await api('/agent',{criteria:$('#agent-criteria').value,max_items:Number($('#agent-limit').value),threshold:Number($('#agent-threshold').value),train:$('#agent-train').checked,mode:$('#agent-mode').value});await refreshJob();}));
 setInterval(()=>{if(state.job.status==='running'||state.orchestration?.status==='running')refreshJob().catch(e=>toast(e.message,true));},1600);
 // Progressive WebMCP support; the normal GUI works without this proposed browser API.
 const context=document.modelContext;if(context?.registerTool){try{await context.registerTool({name:'read_patent_workspace',title:'Read patent workspace',description:'Read the current keywords, selected classifications and patent counts. Does not modify data.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},execute:async(input)=>{if(!input||typeof input!=='object'||Array.isArray(input)||Object.keys(input).length)throw new Error('No input fields are accepted.');return {keywords:state.keywords,selected:state.selected,patent_count:state.patents.length,labels:counts()};}});}catch{/* Optional browser feature. */}}
}
init().catch(error=>{$('#step-content').innerHTML='<div class="panel empty-state"><h2>ワークスペースに接続できません。</h2><p>Pythonサーバーを起動してからページを再読み込みしてください。</p></div>';toast(error.message,true);});
