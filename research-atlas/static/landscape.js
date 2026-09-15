/* Python supplies density; this module only projects and draws it. */
(() => {
  'use strict';
  const cache=new Map(), landscapeCache=new Map(), preferences={mode:'relief',contours:true,referenceGrid:false,height:65,projection:'auto',interval:'year',topic:null,start:null,movement:null,provider:'none',scope:'sample',centroid:null,camera:{yaw:-.16,pitch:.72}};
  let latest=null;
  let preferenceResult=null,reportRequest=0,report={busy:false,data:null,error:'',stage:''};
  const ready=entry=>Boolean(entry?.data?.grid?.values?.length);
  const enhanced=context=>Boolean(context.large&&window.AtlasLayers);
  const landscapeKey=context=>`${context.result.id}|${preferences.projection}|${preferences.interval}|${preferences.scope}`;
  function resetReport(){++reportRequest;report={busy:false,data:null,error:'',stage:''};}
  function fetchLandscape(context,retry=false){
    const key=landscapeKey(context),projection=preferences.projection,interval=preferences.interval,scope=preferences.scope;if(landscapeCache.has(key)&&!retry)return;
    const entry={loading:true,data:null,error:''};landscapeCache.set(key,entry);
    while(landscapeCache.size>12)landscapeCache.delete(landscapeCache.keys().next().value);
    Promise.resolve().then(()=>context.api(`/api/results/${encodeURIComponent(context.result.id)}/landscape?projection=${encodeURIComponent(projection)}&interval=${encodeURIComponent(interval)}&scope=${encodeURIComponent(scope)}`)).then(data=>{
      if(data.result_id!==context.result.id||!Array.isArray(data.map?.nodes)||!Array.isArray(data.periods)||!data.projection_id)throw new Error('時層マップの分析データを確認できませんでした。');
      if(scope==='full'&&(data.scope||data.meta?.scope)!=='full')throw new Error('全件対応版の応答を確認できませんでした。サーバーを最新版へ更新してください。');
      entry.data=data;
    }).catch(error=>{entry.error=error.message||'時層マップを取得できませんでした。';}).finally(()=>{
      entry.loading=false;if(landscapeCache.get(key)===entry&&latest&&landscapeKey(latest)===key)refresh();
    });
  }
  function advancedControls(context,entry){
    const {e,num}=context,data=entry?.data,win=window.AtlasLayers.periodWindow(data?.periods||[],preferences.start);
    const topics=(data?.topics||context.result.topics).filter(t=>!context.isUnclassified(t));
    return `<div class="landscape-advanced-controls"><label>分析範囲<select id="landscape-scope"><option value="sample" ${preferences.scope==='sample'?'selected':''}>簡易版 · 表示標本</option><option value="full" ${preferences.scope==='full'?'selected':''}>全件対応版 · 全論文</option></select></label><label>座標の投影<select id="landscape-projection" aria-label="マップの投影方法">${[['auto','自動選択'],['tsne','t-SNE'],['pca','PCA'],['umap','UMAP']].map(([id,label])=>`<option value="${id}" ${id===preferences.projection?'selected':''}>${label}</option>`).join('')}</select></label><div class="terrain-view-switch" role="group" aria-label="ランドスケープの表示方法">${[['flat','平面'],['relief','立体'],['layers','時層に展開']].map(([id,label])=>`<button type="button" data-landscape-mode="${id}" aria-pressed="${preferences.mode===id}" ${!data?'disabled':''}>${label}</button>`).join('')}</div><label class="terrain-contour-toggle"><span>密度の等高線</span><input type="checkbox" data-landscape-contours ${preferences.contours?'checked':''} ${!data||preferences.mode==='layers'?'disabled':''}></label><label class="terrain-grid-toggle" title="位置関係を読み取るための補助線"><span>補助グリッド</span><input type="checkbox" data-landscape-grid ${preferences.referenceGrid?'checked':''} ${!data||preferences.mode==='flat'?'disabled':''}></label><label for="landscape-height">立体の高さ<input id="landscape-height" type="range" min="20" max="100" step="5" value="${preferences.height}" ${!data||preferences.mode!=='relief'?'disabled':''}></label></div><div class="landscape-time-controls"><label>時間の単位<select id="landscape-interval"><option value="year" ${preferences.interval==='year'?'selected':''}>年別</option><option value="quarter" ${preferences.interval==='quarter'?'selected':''}>3か月別</option><option value="month" ${preferences.interval==='month'?'selected':''}>月別</option></select></label><label>重心を追う話題<select id="landscape-topic">${topics.map(t=>`<option value="${e(t.id)}" ${t.id===preferences.topic?'selected':''}>${e(t.label)}</option>`).join('')}<option value="all" ${preferences.topic==='all'?'selected':''}>全話題</option></select></label><div class="landscape-window"><button type="button" data-landscape-window="-1" aria-label="以前の期間を表示" ${!data||win.start===0?'disabled':''}>←</button><span>${win.periods.length?`${e(win.periods[0].label)} — ${e(win.periods.at(-1).label)}`:'期間を読み込み中'}<small>最大8層 / 全${num(data?.periods?.length||0)}期間</small></span><button type="button" data-landscape-window="1" aria-label="新しい期間を表示" ${!data||win.start===win.last?'disabled':''}>→</button></div></div><div class="terrain-load-status" role="status">${entry?.error?`${e(entry.error)} <button type="button" class="text-link" data-landscape-retry>再取得</button>`:entry?.loading?'Pythonで共通座標・時層・話題の重心変化を計算しています…':''}</div>`;
  }
  const score=value=>value==null||!Number.isFinite(Number(value))?'—':Number(value).toFixed(3);
  const statusLabel=value=>({shift:'内容変化の候補',stable:'内容変化は未確認',insufficient:'比較データ不足'})[value]||'未判定';
  function testScopeNote(context,data,movement){
    if(data.meta?.test_scope!=='bounded_permutation_sample')return '';
    if(movement.p_value==null)return '<p class="field-help">重心と論文数は全件集計です。この比較では、件数不足・期間の空白などのため置換検定を実施していません。</p>';
    const before=movement.test_from_count,after=movement.test_to_count,sampled=before<(movement.from_valid_count??movement.from_count)||after<(movement.to_valid_count??movement.to_count);
    return `<p class="field-help">重心と論文数は全件集計です。置換検定には${sampled?'各期の有効論文から抽出した標本を':'各期の有効論文をすべて'}使用しています（前期${context.num(before)}件・後期${context.num(after)}件）。</p>`;
  }
  function visibleMovements(data){const win=window.AtlasLayers.periodWindow(data.periods||[],preferences.start),ids=new Set(win.periods.map(p=>p.id));return (data.movements||[]).filter(m=>(preferences.topic==='all'||m.topic_id===preferences.topic)&&ids.has(m.from_period)&&ids.has(m.to_period));}
  function selectedMovement(data){const items=visibleMovements(data);return items.find(m=>m.id===preferences.movement)||items.at(-1);}
  function reportHTML(context){
    const {e}=context,data=report.data,n=data?.narrative;
    const warnings=n?.validation?.warnings||[],failed=data?.generation_status==='failed'||Boolean(data?.llm_error)||warnings.some(w=>w?.code==='llm_failed');
    const messages=[...new Set([...warnings.map(w=>typeof w==='string'?w:w.message||w.code||'確認が必要です。'),...(data?.llm_error?[data.llm_error]:[])])];
    const alert=failed||messages.length?`<div class="landscape-report-warning" role="alert">⚠ ${failed?'LLM評論の生成に失敗しました。':'照合に注意が必要です。'}${messages.map(message=>`<div>${e(message)}</div>`).join('')}${failed?'<p>数値分析は保存されています。下の代替表示は計算結果の説明で、LLMが生成した評論ではありません。</p>':''}</div>`:'';
    const narrative=n?`<div class="landscape-generated"><span class="eyebrow">${failed?'CALCULATED OBSERVATIONS · FALLBACK':['local','local_llm'].includes(n.mode)?'LOCAL LLM':n.mode==='openai'?'OPENAI':'CALCULATED OBSERVATIONS'}</span><h4>${e(n.headline||'重心移動の解釈')}</h4>${(n.sections||[]).map(s=>`<h4>${s.validation?.status==='warning'?'⚠ ':''}${e(s.title)}</h4><p>${e(s.text)}</p>${(s.evidence_ids||[]).length?`<div class="landscape-evidence">${s.evidence_ids.slice(0,6).map((id,i)=>`<button data-paper="${e(id)}">根拠論文 ${i+1} ↗</button>`).join('')}</div>`:''}`).join('')}${(n.caveats||[]).map(c=>`<p>${e(c)}</p>`).join('')}${data?.id?`<a class="text-link" href="/api/landscape-reports/${encodeURIComponent(data.id)}/export" download>解釈・分析データをCSVで出力 ↗</a>`:''}</div>`:'';
    return `${report.busy?`<p role="status">${e(report.stage||'解釈を生成しています…')}</p>`:''}${report.error?`<div class="landscape-report-warning" role="alert">⚠ ${e(report.error)}<br>上の計算結果は保持されています。</div>`:''}${alert}${failed&&n?`<details class="landscape-report-fallback"><summary>計算結果の説明（代替表示）</summary>${narrative}</details>`:narrative}`;
  }
  function driftPanel(context,data){
    const {e,num}=context,items=visibleMovements(data),selected=selectedMovement(data),summary=data.interpretation?.summary||'';
    return `<section class="landscape-drift-panel" aria-label="話題の重心移動の分析"><span class="eyebrow">TOPIC DRIFT · OBSERVATION / INTERPRETATION</span><h3>話題の重心は、何を語るか。</h3>${centroidPanel(context,data)}${items.length?`<div class="landscape-movement-list" role="group" aria-label="比較する期間">${items.map(m=>`<button type="button" data-landscape-movement="${e(m.id)}" aria-pressed="${m.id===selected?.id}"><span>${e(m.from_period)} → ${e(m.to_period)}</span><small>${e(statusLabel(m.status))}</small></button>`).join('')}</div>`:''}${selected?`<div class="landscape-metrics"><div><span>マップ上の重心移動</span><strong>${score(selected.distance_2d)}</strong><small>共通座標内の距離 / 単位なし</small></div><div><span>投影前の文章表現の変化</span><strong>${score(selected.cosine_distance)}</strong><small>コサイン距離 / 大きいほど変化</small></div><div><span>${e(statusLabel(selected.status))}</span><strong>${num(selected.from_count)} → ${num(selected.to_count)}</strong><small>${preferences.scope==='full'?'全対象論文数':'表示標本の論文数'} / q = ${score(selected.q_value)}</small></div></div><p>${e(selected.explanation||'同じ話題の中で、各期間の論文の平均位置を比較しています。')}</p>${testScopeNote(context,data,selected)}<div class="landscape-term-change"><div><strong>${e(selected.from_period)} の語</strong><span>${(selected.from_terms||[]).slice(0,5).map(t=>e(t.term)).join(' · ')||'比較可能な語なし'}</span></div><span class="term-direction">→</span><div><strong>${e(selected.to_period)} の語</strong><span>${(selected.to_terms||[]).slice(0,5).map(t=>e(t.term)).join(' · ')||'比較可能な語なし'}</span></div></div><div class="landscape-evidence">${[['前期',selected.evidence_before],['後期',selected.evidence_after]].map(([label,ids])=>(ids||[]).slice(0,3).map((id,i)=>`<button data-paper="${e(id)}">${label}の根拠 ${i+1} ↗</button>`).join('')).join('')}</div>`:`<p>${e(summary||'この時間窓には比較できる重心の組がありません。以前の期間を選ぶか、年別へ切り替えてください。')}</p>`}<p>重心の移動は、同じ話題に属する論文群の平均的な内容・テーマ構成が変わった可能性を示します。研究者の移籍や技術の進歩を直接表しません。2次元の移動だけで判断せず、投影前の文章表現の距離・語の変化・根拠論文を合わせて読みます。「未確認」は変化がないことの証明ではありません。</p><div class="landscape-report-controls"><label for="landscape-provider">解釈</label><select id="landscape-provider"><option value="none" ${preferences.provider==='none'?'selected':''}>計算結果のレポート</option><option value="local" ${preferences.provider==='local'?'selected':''}>ローカルLLM</option><option value="openai" ${preferences.provider==='openai'?'selected':''}>OpenAI API</option></select><button type="button" class="button button-primary button-small" data-landscape-generate ${!selected||report.busy?'disabled':''}>${report.busy?'生成中…':'解釈を生成'}</button><button type="button" class="text-link" data-connection-open>LLM接続設定</button></div><p class="field-help">OpenAIを選んで生成すると、比較指標・代表語・根拠論文の抜粋をAPIへ送信します。文章は仮説の補助です。</p><div id="landscape-report-output" aria-live="polite">${reportHTML(context)}</div></section>`;
  }
  function advancedView(context){
    if(preferenceResult!==context.result.id){preferenceResult=context.result.id;const projection=context.result.options?.map_projection||context.result.meta?.map_projection;preferences.projection=['auto','pca','umap','tsne'].includes(projection)?projection:'auto';preferences.topic=context.topic||context.result.topics[0]?.id;preferences.start=null;preferences.movement=null;preferences.centroid=null;resetReport();}
    fetchLandscape(context);const entry=landscapeCache.get(landscapeKey(context)),data=entry?.data;Promise.resolve().then(bindCurrentOrbit);
    return `<div class="landscape-root" data-landscape-result="${context.e(context.result.id)}" data-landscape-key="${context.e(landscapeKey(context))}">${advancedControls(context,entry)}${data?window.AtlasLayers.render(context,data,preferences):scene(context,null)}<div class="landscape-reading-key"><span><i>⊙</i> 点滅 = 話題の重心</span><span><b>→</b> 赤 = 同一平面上の重心移動</span><span>破線 = 内容変化は未確認・比較データ不足</span></div><p class="terrain-explanation">全期間に一度だけ投影した共通座標を使用します。時層の高さは時間、立体の高さ・等高線は表示論文の相対密度です。矢印は前の期間の平面上に投影し、層の高さの差を移動量に含めません。${data?`表示対象 ${context.num(data.map.nodes.length)}論文。${preferences.scope==='full'?`全件対応版：${context.num(data.meta?.analysis_papers??data.meta?.corpus_papers??0)}論文の内容・重心を集計します。画面の点は描画用の抜粋です。`:'簡易版：表示標本内の探索分析です。全論文の統計とは区別してください。'}`:''}</p>${data?(data.warnings||[]).map(w=>`<p class="terrain-explanation">${context.e(w)}</p>`).join('')+driftPanel(context,data):''}</div>`;
  }
  function selectedCentroid(data){
    const win=window.AtlasLayers.periodWindow(data.periods||[],preferences.start),periods=new Set(win.periods.map(p=>p.id));
    const candidates=(data.centroids||[]).filter(c=>periods.has(c.period_id)&&(preferences.topic==='all'||c.topic_id===preferences.topic));
    return candidates.find(c=>c.topic_id===preferences.centroid?.topic_id&&c.period_id===preferences.centroid?.period_id)||candidates.at(-1);
  }
  function selectTopic(topic){preferences.topic=topic;preferences.movement=null;preferences.centroid=null;latest?.setTopic?.(topic);resetReport();refresh();}
  function selectCentroid(topic,period){preferences.topic=topic;preferences.centroid={topic_id:topic,period_id:period};const data=latest&&landscapeCache.get(landscapeKey(latest))?.data;if(data){const win=window.AtlasLayers.periodWindow(data.periods||[],preferences.start),index=data.periods.findIndex(p=>p.id===period);if(index>=0&&!win.periods.some(p=>p.id===period))preferences.start=Math.max(0,Math.min(win.last,index-3));}latest?.setTopic?.(topic);resetReport();refresh();}
  function resetCamera(){preferences.camera={yaw:-.16,pitch:.72};refresh(true);}
  function bindCurrentOrbit(){const root=document.querySelector('.landscape-root'),stage=root?.querySelector('[data-map-container]');if(!latest||!enhanced(latest)||!stage?.dataset||stage.dataset.orbitBound)return;const orbit=window.AtlasLayers.bindOrbit(stage,value=>{preferences.camera={...value};});if(orbit)stage.dataset.orbitBound='true';}
  function centroidPanel(context,data){
    const {e,num}=context,c=selectedCentroid(data),topic=(data.topics||[]).find(t=>t.id===(c?.topic_id||preferences.topic));
    if(!topic)return '';
    const periods=(data.centroids||[]).filter(row=>row.topic_id===topic.id);
    return `<div class="landscape-centroid-panel"><div class="landscape-centroid-heading"><h4>${e(topic.label)} · 重心の分析</h4><button type="button" class="text-link" data-detail-topic="${e(topic.id)}">分野の総合レポート ↗</button></div>${c?`<div class="landscape-centroid-actions"><label>代表論文を見る期間<select id="landscape-centroid-period">${periods.map(row=>`<option value="${e(row.period_id)}" ${row.period_id===c.period_id?'selected':''}>${e(row.period_id)} · ${num(row.count)}論文</option>`).join('')}</select></label><button type="button" class="button button-quiet button-small" data-landscape-centroid-generate ${report.busy?'disabled':''}>重心付近の代表論文を分析</button></div><p>${e(c.period_id)}：${num(c.count)}論文の重心。${preferences.scope==='full'?'全対象論文':'表示標本'}から代表論文を調べ、共通する対象・手法・相違点を解釈します。</p>`:'<p>この話題には期間別の重心がありません。日付情報を確認してください。</p>'}</div>`;
  }
  async function generateReport(kind='movement'){
    if(!latest||!enhanced(latest))return;const context=latest,key=landscapeKey(context),data=landscapeCache.get(key)?.data,movement=data&&selectedMovement(data),centroid=data&&selectedCentroid(data);if((kind==='centroid'?!centroid:!movement)||report.busy)return;
    const token=++reportRequest,payload={result_id:context.result.id,projection:preferences.projection,projection_id:data.projection_id,interval:preferences.interval,scope:preferences.scope,kind,provider:preferences.provider,...(kind==='centroid'?{topic_id:centroid.topic_id,period_id:centroid.period_id}:{movement_id:movement.id})};
    const current=()=>token===reportRequest&&latest&&landscapeKey(latest)===key&&(kind==='centroid'?selectedCentroid(data)?.topic_id===centroid.topic_id&&selectedCentroid(data)?.period_id===centroid.period_id:selectedMovement(data)?.id===movement.id);
    report={busy:true,data:null,error:'',stage:'選択した期間の根拠をまとめています…'};refresh();
    try{const job=await context.api('/api/landscape-reports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      while(current()){const update=await context.api(`/api/jobs/${encodeURIComponent(job.job_id)}`);if(!current())return;if(update.status==='failed')throw new Error(update.error||'解釈の生成に失敗しました。');if(update.status==='completed'){
          const id=update.landscape_report_id||update.report_id;if(!id)throw new Error('レポートの識別子が返されませんでした。');
          const completed=await context.api(`/api/landscape-reports/${encodeURIComponent(id)}`);if(!current())return;
          if(completed.result_id!==context.result.id||completed.projection_id!==data.projection_id||completed.interval!==payload.interval||(completed.scope||'sample')!==payload.scope||(kind==='centroid'?completed.centroid?.topic_id!==centroid.topic_id||completed.centroid?.period_id!==centroid.period_id:completed.movement?.id!==movement.id))throw new Error('別の座標・期間の解釈が返されたため表示を中止しました。');report.data=completed;break;
        }report.stage=update.stage||report.stage;const target=document.querySelector('#landscape-report-output');if(target)target.innerHTML=reportHTML(context);await new Promise(resolve=>setTimeout(resolve,1000));}
    }catch(error){if(current())report.error=error.message;}finally{if(current()){report.busy=false;refresh();}}
  }
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
    return {width:800,height:context.large?500:350,pad:48,mode:ready(entry)?(preferences.mode==='layers'?'relief':preferences.mode):'flat',heightScale:preferences.height/100*((context.large?500:350)-96)*.30,contours:preferences.contours,referenceGrid:preferences.referenceGrid,idPrefix:context.large?'terrain-large':'terrain-overview'};
  }
  function controls(context,entry) {
    const disabled=!ready(entry),{e}=context;
    return `<div class="terrain-controls"><div class="terrain-view-switch" role="group" aria-label="ランドスケープの表示方法"><button type="button" data-landscape-mode="flat" aria-pressed="${preferences.mode==='flat'}" ${disabled?'disabled':''}>平面</button><button type="button" data-landscape-mode="relief" aria-pressed="${preferences.mode==='relief'}" ${disabled?'disabled':''}>立体</button></div><label class="terrain-contour-toggle"><input type="checkbox" data-landscape-contours ${preferences.contours?'checked':''} ${disabled?'disabled':''}>等高線</label><label class="terrain-grid-toggle" title="位置関係を読み取るための補助線"><input type="checkbox" data-landscape-grid ${preferences.referenceGrid?'checked':''} ${disabled||preferences.mode==='flat'?'disabled':''}>補助グリッド</label><label class="terrain-height-control" for="landscape-height">高さ <input id="landscape-height" type="range" min="20" max="100" step="5" value="${preferences.height}" aria-label="ランドスケープの高さ" ${disabled||preferences.mode!=='relief'?'disabled':''}><output id="landscape-height-value" for="landscape-height">${preferences.height}%</output></label><span class="terrain-density-key"><i></i>密度 低 → 高</span></div><div class="terrain-load-status" role="status">${entry?.error?`${e(entry.error)} <button type="button" class="text-link" data-landscape-retry>等高線を再取得</button>`:entry?.loading?'Pythonで論文密度と等高線を計算しています…':''}</div>`;
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
    if(enhanced(context))return advancedView(context);
    fetchTerrain(context);const entry=cache.get(context.result.id);
    return `<div class="landscape-root" data-landscape-result="${context.e(context.result.id)}">${controls(context,entry)}${scene(context,entry)}<p class="terrain-explanation">高さ・等高線は、表示論文の配置を平滑化した相対密度（KDE）です。研究の重要度や未開拓性を示すものではありません。${context.result.map.truncated?'表示用に抽出した論文だけを使用しています。':''}</p></div>`;
  }
  function refresh(sceneOnly=false) {
    if(!latest)return;
    const root=document.querySelector('.landscape-root');
    if(!root||root.dataset.landscapeResult!==latest.result.id)return;
    if(enhanced(latest)){
      const before=root.querySelector('[data-map-container]'),data=landscapeCache.get(landscapeKey(latest))?.data;
      if(sceneOnly&&data){if(before)before.outerHTML=window.AtlasLayers.render(latest,data,preferences);}
      else root.outerHTML=view(latest);
      const after=document.querySelector('.landscape-root')?.querySelector('[data-map-container]');window.AtlasLayers.animate(before,after);bindCurrentOrbit();return;
    }
    if(sceneOnly){const stage=root.querySelector('[data-map-container]');if(stage)stage.outerHTML=scene(latest,cache.get(latest.result.id));const output=root.querySelector('#landscape-height-value');if(output)output.textContent=String(preferences.height)+'%';}
    else root.outerHTML=view(latest);
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-landscape-mode],[data-landscape-retry],[data-landscape-window],[data-landscape-movement],[data-landscape-generate],[data-landscape-centroid-topic],[data-landscape-centroid-generate]');if(!button||!latest)return;event.preventDefault();
    if(button.hasAttribute('data-landscape-retry')){if(enhanced(latest))fetchLandscape(latest,true);else fetchTerrain(latest,true);refresh();return;}
    if(button.hasAttribute('data-landscape-centroid-generate')){generateReport('centroid');return;}
    if(button.hasAttribute('data-landscape-centroid-topic')){selectCentroid(button.dataset.landscapeCentroidTopic,button.dataset.landscapeCentroidPeriod);return;}
    if(button.hasAttribute('data-landscape-generate')){generateReport();return;}
    if(button.hasAttribute('data-landscape-movement')){preferences.movement=button.dataset.landscapeMovement;preferences.centroid=null;resetReport();refresh();return;}
    if(button.hasAttribute('data-landscape-window')){const data=landscapeCache.get(landscapeKey(latest))?.data;if(data){const win=window.AtlasLayers.periodWindow(data.periods||[],preferences.start);preferences.start=Math.max(0,Math.min(win.last,win.start+Number(button.dataset.landscapeWindow)*4));preferences.movement=null;resetReport();refresh();}return;}
    if(['flat','relief','layers'].includes(button.dataset.landscapeMode)){preferences.mode=button.dataset.landscapeMode;refresh();document.querySelector(`[data-landscape-mode="${preferences.mode}"]`)?.focus();}
  });
  document.addEventListener('change',event=>{
    const target=event.target;
    if(target.id==='landscape-scope'&&['sample','full'].includes(target.value)){preferences.scope=target.value;preferences.movement=null;preferences.centroid=null;resetReport();refresh();document.querySelector('#landscape-scope')?.focus();return;}
    if(target.id==='landscape-projection'&&['auto','pca','umap','tsne'].includes(target.value)){preferences.projection=target.value;preferences.movement=null;resetReport();refresh();document.querySelector('#landscape-projection')?.focus();return;}
    if(target.id==='landscape-interval'&&['year','quarter','month'].includes(target.value)){preferences.interval=target.value;preferences.start=null;preferences.movement=null;resetReport();refresh();document.querySelector('#landscape-interval')?.focus();return;}
    if(target.id==='landscape-topic'){selectTopic(target.value);document.querySelector('#landscape-topic')?.focus();return;}
    if(target.id==='landscape-centroid-period'){selectCentroid(preferences.topic,target.value);return;}
    if(target.id==='landscape-provider'){preferences.provider=target.value;return;}
    if(target.matches('[data-landscape-grid]')){preferences.referenceGrid=target.checked;refresh();document.querySelector('[data-landscape-grid]')?.focus();return;}
    if(!target.matches('[data-landscape-contours]'))return;preferences.contours=target.checked;refresh();document.querySelector('[data-landscape-contours]')?.focus();
  });
  document.addEventListener('keydown',event=>{if(!['Enter',' '].includes(event.key))return;const target=event.target.closest?.('g[data-landscape-centroid-topic],g[data-landscape-movement],path[data-landscape-movement]');if(target){event.preventDefault();if(target.dataset.landscapeCentroidTopic)selectCentroid(target.dataset.landscapeCentroidTopic,target.dataset.landscapeCentroidPeriod);else{preferences.movement=target.dataset.landscapeMovement;resetReport();refresh();}}});
  document.addEventListener('input',event=>{if(event.target.id!=='landscape-height')return;preferences.height=Math.max(20,Math.min(100,Number(event.target.value)||65));refresh(true);});
  window.AtlasLandscape=Object.freeze({view,selectTopic,resetCamera});
  if(window.__ATLAS_UI_TEST__)window.__landscapeTest={cache,landscapeCache,preferences,fetchTerrain,fetchLandscape,controls,scene,refresh,advancedControls,selectedMovement,selectedCentroid,selectTopic,selectCentroid,visibleMovements,generateReport,centroidPanel,testScopeNote,reportHTML,get report(){return report;}};
})();
