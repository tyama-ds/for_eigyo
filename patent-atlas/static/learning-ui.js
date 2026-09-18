/* Map-local training controls. Polls update progress without rebuilding the form. */
let learningDraft={mode:'lightweight',criteria:'',max_items:100,threshold:.8,include_agent:false,search:'',filter:'all'};
let learningStarting=false;
function captureLearningDraft(){
 if(!$('#training-mode'))return;
 learningDraft={mode:$('#training-mode').value,criteria:$('#llm-training-criteria')?.value||'',
  max_items:$('#llm-training-limit')?.value??100,threshold:$('#llm-training-threshold')?.value??.8,
  include_agent:!!$('#include-agent')?.checked,search:$('#patent-search')?.value||'',filter:$('#label-filter')?.value||'all'};
}
function learningControlsHtml(){
 const d=learningDraft;
 return `<div class="learning-row"><label class="learning-mode-label">学習・判定方式<select id="training-mode" aria-label="学習方式"><option value="lightweight" ${d.mode==='lightweight'?'selected':''}>軽量学習 · TF-IDF</option><option value="transformer" ${d.mode==='transformer'?'selected':''}>Transformer本体を微調整</option><option value="llm" ${d.mode==='llm'?'selected':''}>LLMにおまかせ</option></select></label><button class="button primary" id="train-button">学習する ↗</button><label class="check" id="include-agent-control"><input id="include-agent" type="checkbox" ${d.include_agent?'checked':''}>エージェントの判定を含める</label><a class="button subtle small" href="/api/export/labels" download>判定をCSVで保存 ↓</a></div>
 <div id="llm-training-options" class="llm-training-options" hidden><label>判定基準（空欄なら採用した式の目的・観点、未設定なら探索キーワード）<textarea id="llm-training-criteria" rows="2" maxlength="5000" placeholder="例：製造工程や界面の改善を含み、充電設備だけの特許は除く">${esc(d.criteria)}</textarea></label><p id="llm-training-used-criteria" class="caption" hidden></p><div class="llm-training-limits"><label>今回判定する上限<input id="llm-training-limit" type="number" min="1" max="5000" step="1" value="${esc(d.max_items)}"></label><label>判定を採用する信頼度<input id="llm-training-threshold" type="number" min="0.5" max="1" step="0.05" value="${esc(d.threshold)}"></label></div><p class="caption">設定したLLMへタイトル・要約・IPCと判定例を渡します。採用した式があれば目的・観点も参照し、入力した判定基準を優先します。人の確定判定を優先し、低信頼の回答は保留。LLMの重みは学習しません。</p></div>
 <p id="learning-mode-note" class="caption learning-mode-note"></p>
 <div id="learning-progress" class="learning-progress" hidden><div class="learning-progress-heading"><div><span class="eyebrow">PROGRESS</span><b id="learning-progress-status"></b></div><strong id="learning-progress-percent">0%</strong></div><progress id="map-training-progress" max="100" value="0" aria-label="学習・判定の進捗"></progress><p id="learning-progress-message" role="status"></p><div class="learning-progress-bottom"><small id="learning-progress-count"></small><button id="map-training-stop" class="button small subtle" hidden>停止する</button></div><p id="learning-progress-error" class="learning-error" hidden></p><small class="caption">割合は処理工程の目安です。LLMの応答待ち・モデル読込中は進捗が止まる場合があります。</small></div>`;
}
function learningProgressState(job, training){
 const relevant=job?.kind==='training'||job?.kind==='agent';
 const stored=training?.mode==='llm'&&(!relevant||training.job_id===job.id)?training:null;
 const status=relevant?job.status:stored?.status==='completed'?'done':stored?.status;
 const numeric=Number(job?.progress);
 return {visible:!!(relevant&&job.id||stored),status:status||'idle',running:relevant&&job.status==='running',
  percent:status==='done'?100:relevant&&Number.isFinite(numeric)?Math.max(0,Math.min(99,Math.round(numeric))):Math.max(0,Math.min(99,Number(stored?.progress)||0)),
  message:relevant?job.message:stored?.message||'保存済みのLLM判定結果',error:relevant?job.error:stored?.error||'',
  count:stored&&(!relevant||job.mode==='llm')?`${Number(stored.completed_count||0)} / ${Number(stored.total_count||0)} 件を処理 · 判定採用 ${Number(stored.judged_count||0)} 件 · 保留 ${Number(stored.deferred_count||0)} 件`:''};
}
function renderLearningProgress(){
 const host=$('#learning-progress');if(!host)return;
 const j=state.job||{},p=learningProgressState(j,state.training),busy=learningStarting||j.status==='running';
 host.hidden=!p.visible;host.dataset.status=p.status;
 $('#map-training-progress').value=p.percent;
 $('#learning-progress-percent').textContent=p.percent+'%';
 $('#learning-progress-status').textContent=({idle:'待機中',running:'処理中',done:'完了',cancelled:'停止しました',error:'処理を終了しました'})[p.status]||p.status;
 $('#learning-progress-message').textContent=p.message||'準備中';
 $('#learning-progress-count').textContent=p.count;
 const error=$('#learning-progress-error');error.hidden=!p.error;error.textContent=p.error||'';
 const stop=$('#map-training-stop');stop.hidden=!p.running;stop.disabled=!!j.stop_requested;
 stop.textContent=j.stop_requested?'停止を待っています…':'停止する';
 for(const selector of ['#training-mode','#train-button','#include-agent','#llm-training-criteria','#llm-training-limit','#llm-training-threshold'])if($(selector))$(selector).disabled=busy;
 for(const button of $$('.row-label,.map-selection-actions button'))button.disabled=busy;
 if($('#next-refine'))$('#next-refine').disabled=busy;
}
function renderLearningMode(){
 const job=state.job||{},training=state.training;
 if(job.status==='running'&&job.kind==='training'){
  const mode=$('#training-mode');
  if(mode&&['lightweight','transformer','llm'].includes(job.mode))mode.value=job.mode;
  if(job.mode==='llm'&&training?.mode==='llm'&&training.job_id===job.id){
   const savedCriteria=['adopted_query','keywords'].includes(training.criteria_source)?'':training.criteria;
   for(const [selector,value] of [['#llm-training-criteria',savedCriteria],['#llm-training-limit',training.max_items],['#llm-training-threshold',training.threshold]]){
    if($(selector)&&value!==undefined&&value!==null)$(selector).value=String(value);
   }
  }
 }
 const llm=$('#training-mode')?.value==='llm';
 const usedCriteria=$('#llm-training-used-criteria');
 if(usedCriteria){
  const current=training?.mode==='llm'&&training.criteria&&(job.status!=='running'||training.job_id===job.id);
  usedCriteria.hidden=!llm||!current;
  const source=({adopted_query:'採用した式の目的・観点',keywords:'探索キーワード',explicit:'入力した基準'})[training?.criteria_source]||'保存された基準';
  usedCriteria.textContent=current?`${job.status==='running'?'今回':'直近の判定'}で使用した基準（${source}）：${training.criteria}`:'';
 }
 $('#llm-training-options').hidden=!llm;$('#include-agent-control').hidden=llm;
 $('#train-button').textContent=llm?'LLMで判定する ↗':'学習する ↗';
 $('#learning-mode-note').textContent=llm?(state.settings.provider==='offline'?'LLMが未接続です。「表示・接続設定」でLocal LLMまたはAPIを設定してください。':`接続先：${state.settings.provider==='local'?'Local LLM':'LLM API'} · ${state.settings.model||'モデル未設定'}。未判定の特許から順に判定します。`):'軽量学習は必要・不要が各2件、Transformerは各4件から。関連度を計算し、次の式の特徴語・IPC候補に反映します。';
 renderLearningProgress();
}
async function startMapTraining(){
 if(learningStarting||state.job?.status==='running')return;
 captureLearningDraft();
 const d=learningDraft,body={mode:d.mode,include_agent:d.include_agent};
 if(d.mode==='llm'){
  const limit=Number(d.max_items),threshold=Number(d.threshold);
  if(!String(d.max_items).trim()||!Number.isInteger(limit)||limit<1||limit>5000)throw new Error('判定上限は1〜5000の整数にしてください。');
  if(!String(d.threshold).trim()||!Number.isFinite(threshold)||threshold<.5||threshold>1)throw new Error('信頼度は0.5〜1の数値にしてください。');
  Object.assign(body,{criteria:d.criteria,max_items:limit,threshold});
 }
 learningStarting=true;renderLearningProgress();
 try{
  const result=await api('/train',body);
  state.job={id:result.job_id,kind:'training',mode:d.mode,status:'running',progress:0,message:'準備中',error:null};
  await refreshJob();$('#learning-progress')?.scrollIntoView?.({block:'nearest',behavior:'auto'});
  if(state.job.status==='running')toast(d.mode==='llm'?'LLM判定を開始しました。':'学習を開始しました。');
 }finally{learningStarting=false;renderLearningProgress();}
}
function bindLearningControls(){
 on('#training-mode','change',()=>{captureLearningDraft();renderLearningMode();});
 for(const id of ['#llm-training-criteria','#llm-training-limit','#llm-training-threshold','#include-agent'])on(id,'change',captureLearningDraft);
 on('#train-button','click',startMapTraining);
 on('#map-training-stop','click',async()=>{await api('/job/stop',{});state.job.stop_requested=true;renderLearningProgress();});
 if($('#patent-search'))$('#patent-search').value=learningDraft.search;
 if($('#label-filter'))$('#label-filter').value=learningDraft.filter;
 renderLearningMode();
}
