/* Search observations, not training epochs. Editable fields survive background polls. */
let convergenceBusy=false,convergenceDraft=null,convergenceReviewId=null;
const convergenceMetrics=[
 {key:'new_keep_rate',label:'新しい必要特許の割合',className:'new',short:'新規必要'},
 {key:'topic_shift_rate',label:'IPC構成の変化',className:'topic',short:'IPC構成'},
 {key:'hit_change_rate',label:'検索総件数の変化',className:'hits',short:'総件数'}
];
function convergenceNumber(value){return typeof value==='number'&&Number.isFinite(value)&&value>=0?value:null;}
function convergencePercent(value){const n=convergenceNumber(value);return n===null?'—':`${(n*100).toFixed(1)}%`;}
function convergenceCount(value){const n=convergenceNumber(value);return n===null?'未記録':n.toLocaleString('ja-JP');}
function convergenceDate(value){
 if(value===null||value===undefined||value==='')return '日時未記録';
 const numeric=typeof value==='number'?value:typeof value==='string'&&/^\d{10,13}(?:\.\d+)?$/.test(value.trim())?Number(value):null;
 const date=new Date(numeric===null?value:Math.abs(numeric)<1e12?numeric*1000:numeric);
 return Number.isFinite(date.getTime())?new Intl.DateTimeFormat('ja-JP',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date):'日時未記録';
}
function convergenceDeferred(p){return p.label!=='keep'&&p.label!=='exclude'&&p.label_source==='agent'&&!!String(p.label_reason||'').trim()&&typeof p.agent_confidence==='number'&&Number.isFinite(p.agent_confidence)&&p.agent_confidence>=0&&p.agent_confidence<=1;}
function convergenceObservationKey(){return state.search_result?.id||'baseline';}
function convergenceCurrentRecord(){const id=state.search_result?.id;return [...(state.convergence?.records||[])].reverse().find(r=>id?r.observation_id===id:r.scope==='workspace_baseline'||!r.observation_id||r.observation_id==='baseline');}
function convergenceRecordDefaults(){const saved=convergenceCurrentRecord();return {key:convergenceObservationKey(),total_hits:saved?.total_hits??'',query_id:saved?.query_id??state.search_result?.query_id??'',dirty:false};}
function captureConvergenceDraft(){if(!$('#convergence-total'))return;convergenceDraft={key:convergenceObservationKey(),total_hits:$('#convergence-total').value,query_id:$('#convergence-query').value,dirty:true};}
function convergenceRecordPayload(total,query,observationId,uploadedCount){
 const text=String(total??'').trim();let total_hits=null;
 if(text){if(!/^\d+$/.test(text))throw new Error('検索総件数は0以上の整数で入力してください。');total_hits=Number(text);if(!Number.isSafeInteger(total_hits)||total_hits<Number(uploadedCount||0))throw new Error('検索総件数は今回のCSV件数以上の整数にしてください。');}
 return {total_hits,query_id:query||null,observation_id:observationId||null};
}
function convergenceSeries(records,key,x,y){
 const segments=[];let current=[];
 records.forEach((r,i)=>{const n=convergenceNumber(r[key]);if(n===null){if(current.length)segments.push(current);current=[];}else current.push({x:x(i),y:y(n),value:n,index:i});});
 if(current.length)segments.push(current);return segments;
}
function convergenceChart(records,counts=false){
 if(!records.length)return '<div class="convergence-empty"><span>01 — 02 — 03</span><strong>検索を重ねると、変化が見えてきます。</strong><p>検索結果を記録すると、実測値だけをプロットします。最初の1回は比較の基準です。</p></div>';
 const width=800,height=counts?220:290,left=64,right=30,top=30,bottom=44,plotWidth=width-left-right,plotHeight=height-top-bottom;
 const metrics=counts?[{key:'uploaded_count',label:'CSV取込件数',className:'uploaded'},{key:'total_hits',label:'検索サイトの総件数',className:'total'}]:convergenceMetrics;
 const maximum=counts?Math.max(1,...records.flatMap(r=>metrics.map(m=>convergenceNumber(r[m.key])||0))):1;
 const x=i=>left+(records.length===1?plotWidth/2:i*plotWidth/(records.length-1)),y=n=>top+plotHeight*(1-Math.min(maximum,n)/maximum);
 const ticks=counts?[0,.25,.5,.75,1]:[0,.25,.5,.75,1];
 const grid=ticks.map(t=>`<line x1="${left}" x2="${width-right}" y1="${y(maximum*t)}" y2="${y(maximum*t)}" class="convergence-gridline"/><text x="${left-12}" y="${y(maximum*t)+4}" text-anchor="end">${counts?Math.round(maximum*t).toLocaleString('ja-JP'):Math.round(t*100)+'%'}</text>`).join('');
 const guide=counts?'':`<line x1="${left}" x2="${width-right}" y1="${y(.05)}" y2="${y(.05)}" class="convergence-guide"/><text x="${width-right}" y="${y(.05)-7}" text-anchor="end" class="convergence-guide-label">安定の目安 5%</text>`;
 const lines=metrics.map(m=>convergenceSeries(records,m.key,x,y).map(segment=>`<polyline class="convergence-line convergence-${m.className}" points="${segment.map(p=>`${p.x},${p.y}`).join(' ')}"/>`).join('')).join('');
 const points=metrics.map(m=>records.map((r,i)=>{const n=convergenceNumber(r[m.key]);if(n===null)return '';const label=`第${r.ordinal||i+1}回 · ${m.label} ${counts?convergenceCount(n)+'件':convergencePercent(n)}${!counts&&n>1?'（グラフ上端は100%）':''}`;return `<g class="convergence-point convergence-${m.className}" tabindex="0" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title><circle class="convergence-hit-area" cx="${x(i)}" cy="${y(n)}" r="11"/><circle cx="${x(i)}" cy="${y(n)}" r="4.5"/></g>`;}).join('')).join('');
 const labelEvery=Math.max(1,Math.ceil(records.length/12));
 const labels=records.map((r,i)=>i%labelEvery===0||i===records.length-1?`<text x="${x(i)}" y="${height-20}" text-anchor="middle">${Number(r.ordinal)||i+1}</text>`:'').join('');
 return `<div class="convergence-chart-scroll"><svg class="convergence-chart" viewBox="0 0 ${width} ${height}" aria-label="${counts?'検索回ごとのCSV件数と検索総件数':'検索回ごとの新規必要特許・IPC構成・検索総件数の変化'}" role="group"><title>${counts?'検索規模の推移':'探索の収束曲線'}</title>${grid}${guide}${lines}${points}${labels}<text x="${width-right}" y="${height-3}" text-anchor="end">検索回</text></svg></div>`;
}
function convergenceHistoryHtml(records){
 if(!records.length)return '<p class="caption">まだ記録がありません。</p>';
 return `<div class="convergence-table-scroll"><table class="convergence-table"><caption>各検索回の実測値。— は比較できない項目です。</caption><thead><tr><th scope="col">回 / 検索式</th><th scope="col">CSV / 総件数</th><th scope="col">必要 / 不要</th><th scope="col">保留 / 未判定</th><th scope="col">新規必要</th><th scope="col">IPC構成変化</th><th scope="col">総件数変化</th></tr></thead><tbody>${records.map((r,i)=>`<tr><th scope="row"><b>第${esc(r.ordinal||i+1)}回</b><small>${esc(convergenceDate(r.created_at))}</small><details><summary>${r.query_id?'使用した検索式':'検索式 未指定'}</summary><pre>${esc(r.expression||'記録なし')}</pre></details></th><td>${convergenceCount(r.uploaded_count)}<small>/ ${r.total_hits===null||r.total_hits===undefined?'総件数 未記録':convergenceCount(r.total_hits)}${r.result_complete?' · 全件取込':''}</small></td><td>${convergenceCount(r.keep_count)} / ${convergenceCount(r.exclude_count)}</td><td>${convergenceCount(r.deferred_count)} / ${convergenceCount(r.unprocessed_count)}</td><td>${convergencePercent(r.new_keep_rate)}<small>${r.new_keep_count===null||r.new_keep_count===undefined?'基準回':convergenceCount(r.new_keep_count)+'件'}</small></td><td>${convergencePercent(r.topic_shift_rate)}<small>新規IPC ${convergenceCount(r.new_ipc_count)}</small></td><td>${convergencePercent(r.hit_change_rate)}</td></tr>`).join('')}</tbody></table></div>`;
}
function convergenceReviewHtml(p,count){
 if(!p)return '<div class="convergence-review-empty"><span>✓</span><b>人の確認を待つ保留はありません。</b><p>未判定が残っている場合は、特許の地図から判定を進めてください。</p></div>';
 const decision={keep:'必要の可能性',exclude:'不要の可能性',unsure:'判断困難',defer:'判断困難'}[p.llm_decision]||'判定を保留';
 return `<div class="convergence-review-card"><div class="convergence-review-meta"><span class="pill">保留 ${count}件</span><span>LLM: ${decision} · 自己評価 ${convergencePercent(p.agent_confidence)}</span></div><h4>${esc(p.title||'タイトル未取込')}</h4><p class="convergence-patent-id">${esc(p.publication_number||p.id||'')} · IPC ${esc(p.ipc||'未取込')}</p><p class="convergence-abstract">${esc(p.abstract||'要約は取り込まれていません。元の公報を確認して判断してください。')}</p><div class="convergence-reason"><b>保留の理由</b><p>${esc(p.label_reason||'')}</p></div><div class="button-row"><button class="button primary small" id="convergence-keep">必要として確定</button><button class="button small" id="convergence-exclude">不要として確定</button><button class="button subtle small" id="convergence-skip" ${count<2?'disabled':''}>次の保留を見る →</button></div><p class="caption">「次の保留を見る」は判定を変更しません。必要・不要の確定は、検索式を更新するときの根拠になります。</p></div>`;
}
function renderConvergence(){
 const host=$('#convergence-content');if(!host)return;
 const mountedNow=!$('#convergence-total');
 if(mountedNow){
  host.innerHTML=`<section class="panel convergence-panel"><div class="convergence-heading"><div><span class="eyebrow">SEARCH CONVERGENCE</span><h3>探索は、落ち着いてきた？</h3><p class="caption">検索回ごとの変化と、人が確認すべき保留をひとつの場所に。</p></div><span id="convergence-status" class="convergence-status"></span></div><div id="convergence-summary" class="convergence-summary" aria-live="polite"></div><div id="convergence-graph"></div><div class="convergence-legend">${convergenceMetrics.map(m=>`<span><i class="convergence-${m.className}"></i>${m.label}</span>`).join('')}<span class="caption">欠測値は線をつなぎません</span></div><p class="convergence-method">各指標が5%以下の回が3回続くと、安定の目安です。学習損失ではなく検索結果の変化を表示します。IPC構成はトピックの代理指標であり、調査の網羅性を保証するものではありません。</p><details class="convergence-count-details"><summary>検索規模と各回の記録を見る</summary><div id="convergence-count-chart"></div><div class="convergence-legend"><span><i class="convergence-uploaded"></i>CSV取込件数</span><span><i class="convergence-total"></i>検索サイトの総件数</span></div><p class="caption">CSV件数と検索総件数は別に記録します。検索総件数の未記録は0件として扱いません。同じ検索サイト・対象期間など、比較できる範囲で記録してください。</p><div id="convergence-history"></div></details><div class="convergence-record"><div><span class="eyebrow">RECORD THIS SEARCH</span><h4>今回の検索結果を記録</h4><p id="convergence-observation-note" class="caption"></p></div><div class="convergence-record-fields"><label>実際に使った検索式<select id="convergence-query"><option value="">未指定（比較の参考記録）</option></select></label><label>検索サイトに表示された総件数<input id="convergence-total" type="number" min="0" step="1" inputmode="numeric" placeholder="未記録でも保存できます"></label></div><p id="convergence-query-preview" class="convergence-query-preview"></p><button id="convergence-record" class="button small">今回の検索結果を記録</button><p class="caption">同じCSVの判定を更新しても、検索回は増えません。CSVを追加・差し替えたら、新しい検索回として記録します。</p></div><div class="convergence-complete"><div id="convergence-blockers"></div><button id="convergence-complete" class="button primary small">確認して探索を完了</button></div><p id="convergence-error" class="convergence-error" role="alert" hidden></p></section><section class="panel convergence-review-panel"><div class="convergence-heading"><div><span class="eyebrow">HUMAN REVIEW</span><h3>保留を、人の目で確認。</h3><p class="caption">保留・未判定が残っている間は完了にしません。LLMの採用済み判定も、必要に応じて特許の地図で確認できます。</p></div><button class="button subtle small" id="convergence-map">特許の地図へ ↗</button></div><div id="convergence-pending" class="caption" aria-live="polite"></div><div id="convergence-review"></div></section>`;
  on('#convergence-total','input',captureConvergenceDraft);on('#convergence-query','change',()=>{captureConvergenceDraft();renderConvergenceQueryPreview();});
  on('#convergence-record','click',()=>convergenceAction('record'));on('#convergence-complete','click',()=>convergenceAction('complete'));
  on('#convergence-map','click',()=>{setTab('explore');setStep(2);});
 }
 const data=state.convergence||{},records=data.records||[],status=data.status||'insufficient';
 $('#convergence-status').textContent={insufficient:'比較データ待ち',exploring:'探索中',stable:'安定の目安に到達',completed:'探索完了'}[status]||'探索中';$('#convergence-status').dataset.status=status;
 $('#convergence-summary').textContent=status==='completed'?`探索完了 · ${records.length}回の記録`:`記録 ${records.length}回 · 安定 ${Number(data.stable_streak)||0} / ${Number(data.required_streak)||3}回${records.length===1?' · 次回から変化を比較':''}`;
 for(const [id,html] of [['graph',convergenceChart(records)],['count-chart',convergenceChart(records,true)],['history',convergenceHistoryHtml(records)]]){const el=$('#convergence-'+id);if(el._convergenceHtml!==html){el.innerHTML=html;el._convergenceHtml=html;}}
 if(!convergenceDraft||convergenceDraft.key!==convergenceObservationKey()||!convergenceDraft.dirty){convergenceDraft=convergenceRecordDefaults();$('#convergence-total').value=String(convergenceDraft.total_hits);}else if(mountedNow)$('#convergence-total').value=String(convergenceDraft.total_hits);
 const querySelect=$('#convergence-query'),queries=state.queries||[],querySignature=JSON.stringify(queries.map(q=>[q.id,q.expression])),selected=convergenceDraft.query_id;
 if(querySelect.dataset.signature!==querySignature){querySelect.innerHTML='<option value="">未指定（比較の参考記録）</option>'+queries.map((q,i)=>`<option value="${esc(q.id)}">検索式 ${i+1} · ${esc(String(q.expression||q.label||q.id).slice(0,95))}</option>`).join('');querySelect.dataset.signature=querySignature;}
 querySelect.value=queries.some(q=>q.id===selected)?selected:'';renderConvergenceQueryPreview();
 const uploaded=state.search_result?.uploaded_count??state.patents?.length??0,current=convergenceCurrentRecord();
 $('#convergence-total').min=String(uploaded);$('#convergence-observation-note').textContent=state.search_result?`最新のCSV: ${convergenceCount(uploaded)}件。判定は現在保存されている内容で記録します。`:`CSVの検索回情報がないため、現在の${convergenceCount(uploaded)}件を比較の基準として記録します。`;
 $('#convergence-record').textContent=current?'この回の判定を更新':'今回の検索結果を記録';
 const blockers=data.blockers||[];$('#convergence-blockers').innerHTML=status==='completed'?'<b>探索を完了しました。</b><p class="caption">記録した範囲での完了です。条件を変えて検索結果を追加すると、あらためて比較できます。</p>':`<b>${data.eligible_to_complete?'保留の確認と安定条件を満たしました。':'完了までに確認すること'}</b>${blockers.length?'<ul>'+blockers.map(b=>`<li>${esc(b)}</li>`).join('')+'</ul>':'<p class="caption">グラフの安定だけで自動完了にはしません。検索結果と条件を確認して完了します。</p>'}${data.note?`<p class="caption">${esc(data.note)}</p>`:''}`;
 const deferred=(state.patents||[]).filter(convergenceDeferred),p=deferred.find(p=>p.id===convergenceReviewId)||deferred[0];convergenceReviewId=p?.id||null;
 const pending=data.pending||{};$('#convergence-pending').textContent=`保留 ${deferred.length}件 · 未判定 ${Number(pending.unprocessed)||0}件`;
 const reviewHtml=convergenceReviewHtml(p,deferred.length),review=$('#convergence-review');
 if(review._convergenceHtml!==reviewHtml){review.innerHTML=reviewHtml;review._convergenceHtml=reviewHtml;const shown={id:p?.id,expected_reason:p?.label_reason,expected_confidence:p?.agent_confidence};on('#convergence-keep','click',()=>convergenceAction('review',{...shown,label:'keep'}));on('#convergence-exclude','click',()=>convergenceAction('review',{...shown,label:'exclude'}));on('#convergence-skip','click',()=>{const currentRows=(state.patents||[]).filter(convergenceDeferred),i=currentRows.findIndex(p=>p.id===convergenceReviewId);convergenceReviewId=currentRows[(i+1)%currentRows.length]?.id||null;renderConvergence();});}
 const locked=convergenceBusy||state.job?.status==='running'||state.orchestration?.status==='running';
 for(const id of ['total','query','record','keep','exclude','skip']){const el=$('#convergence-'+id);if(el)el.disabled=locked||(id==='record'&&!uploaded)||(id==='skip'&&deferred.length<2);}
 $('#convergence-complete').disabled=locked||!data.eligible_to_complete||status==='completed';
}
function renderConvergenceQueryPreview(){const q=(state.queries||[]).find(q=>q.id===$('#convergence-query')?.value);if($('#convergence-query-preview'))$('#convergence-query-preview').textContent=q?.expression||'検索式未指定の記録は参考用です。安定判定には実際に使った検索式を指定してください。';}
async function convergenceAction(action,body){
 if(convergenceBusy||state.job?.status==='running'||state.orchestration?.status==='running')return;
 if(action==='record'){captureConvergenceDraft();body=convergenceRecordPayload(convergenceDraft.total_hits,convergenceDraft.query_id,state.search_result?.id,state.search_result?.uploaded_count??state.patents?.length);}
 if(action==='complete'&&!state.convergence?.eligible_to_complete)return;
 if(action==='review'){const p=(state.patents||[]).find(p=>p.id===body?.id&&convergenceDeferred(p));if(!p)return;body={expected_reason:p.label_reason,expected_confidence:p.agent_confidence,...body};}
 convergenceBusy=true;$('#convergence-error').hidden=true;renderConvergence();
 try{const result=await api('/convergence/'+action,body||{});state=result?.patents?result:await api('/state');if(action==='record')convergenceDraft=null;updateChrome();if(typeof renderOrchestrationProgress==='function')renderOrchestrationProgress();toast(action==='review'?'人の判定を保存し、この回の記録を更新しました。':action==='complete'?'探索を完了しました。':'検索結果を記録しました。');}
 catch(error){if(error.status===409){try{state=await api('/state');updateChrome();}catch(_){/* Keep the failed action visible; never retry a write automatically. */}}$('#convergence-error').hidden=false;$('#convergence-error').textContent=error.message;throw error;}
 finally{convergenceBusy=false;renderConvergence();}
}
