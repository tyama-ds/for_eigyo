/* One workflow, shared patent evidence. Polls never rebuild editable controls. */
let orchestrationDraft=null, orchestrationStarting=false, orchestrationCheckpointKey='';
const orchestrationStages=[['discover','分類探索'],['assess','特許判定'],['train','学習'],['query','検索式更新'],['waiting_csv','次のCSV待ち']];
function orchestrationDefaults(){
 const options=state.orchestration?.options||{},training=state.training||{};
 return {criteria:options.criteria||training.criteria||state.keywords||'',judge:options.judge??true,learning:options.learning||'none',include_agent:options.include_agent??false,max_items:options.max_items??5000,threshold:options.threshold??.8,review:options.review??true};
}
function captureOrchestrationDraft(){
 if(!$('#orchestration-criteria'))return;
 orchestrationDraft={criteria:$('#orchestration-criteria').value,judge:$('#orchestration-judge').checked,learning:$('#orchestration-learning').value,include_agent:$('#orchestration-include-agent').checked,max_items:$('#orchestration-limit').value,threshold:$('#orchestration-threshold').value,review:$('#orchestration-review').checked};
}
function orchestrationOptions(draft){
 const max_items=Number(draft.max_items),threshold=Number(draft.threshold),criteria=String(draft.criteria||'').trim();
 if(!String(draft.max_items).trim()||!Number.isInteger(max_items)||max_items<1||max_items>5000)throw new Error('判定上限は1〜5000の整数にしてください。');
 if(!String(draft.threshold).trim()||!Number.isFinite(threshold)||threshold<.5||threshold>1)throw new Error('信頼度は0.5〜1の数値にしてください。');
 if(!['none','lightweight','transformer'].includes(draft.learning))throw new Error('学習方式を選択してください。');
 if(draft.judge&&!criteria)throw new Error('LLM判定の基準を入力してください。');
 return {criteria,judge:!!draft.judge,learning:draft.learning,include_agent:!!draft.include_agent,max_items,threshold,review:!!draft.review};
}
function orchestrationCounts(patents=[]){
 const counts={total:patents.length,keep:0,exclude:0,deferred:0,pending:0,human:0,ipc:0,fi:0,fterm:0};
 for(const row of patents){if(row.label==='keep')counts.keep++;else if(row.label==='exclude')counts.exclude++;else if(row.label_source==='agent'&&String(row.label_reason||'').trim()&&typeof row.agent_confidence==='number'&&Number.isFinite(row.agent_confidence)&&row.agent_confidence>=0&&row.agent_confidence<=1)counts.deferred++;else counts.pending++;if(row.label_source==='human')counts.human++;for(const kind of ['ipc','fi','fterm'])if(String(row[kind]||'').trim())counts[kind]++;}
 return counts;
}
function orchestrationPercent(value,status){const n=Number(value);return status==='waiting_csv'?100:Number.isFinite(n)?Math.max(0,Math.min(99,Math.round(n))):0;}
function orchestrationStatus(status){return {idle:'開始前',running:'実行中',review:'あなたの確認待ち',paused:'停止中',error:'処理を停止しました',waiting_csv:'次のCSV待ち'}[status]||'開始前';}
function orchestrationCandidate(item){return {key:item.key||`${item.kind}:${item.code}`,title:item.title_ja||item.title||item.title_en||'',code:item.code||'',kind:item.kind||'IPC'};}
function orchestrationSearchText(value){return String(value||'').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,'');}
function filterOrchestrationCandidates(){
 const search=$('#orchestration-candidate-search');if(!search)return;
 const query=orchestrationSearchText(search.value),selected=new Set($$('[data-orchestration-key]').filter(el=>el.checked&&!el.disabled).map(el=>el.dataset.orchestrationKey)),rows=$$('[data-orchestration-search]');
 let visible=0,hiddenSelected=0;
 for(const row of rows){row.hidden=!!query&&!orchestrationSearchText(row.dataset.orchestrationSearch).includes(query);if(!row.hidden)visible++;else if(selected.has(row.dataset.orchestrationCandidateKey))hiddenSelected++;}
 if($('#orchestration-candidate-count'))$('#orchestration-candidate-count').textContent=`表示 ${visible} / ${rows.length} 件 · 選択 ${selected.size} 件${hiddenSelected?`（絞り込みで非表示 ${hiddenSelected} 件）`:''}`;
 if($('#orchestration-candidate-empty'))$('#orchestration-candidate-empty').hidden=visible>0||!rows.length;
}
function orchestrationCheckpointHtml(checkpoint){
 if(!checkpoint)return '';
 const selected=new Set(checkpoint.selected_keys||[]),candidates=[...(checkpoint.candidates||[])].sort((a,b)=>Number(selected.has(orchestrationCandidate(b).key))-Number(selected.has(orchestrationCandidate(a).key)));
 const terms=checkpoint.term_options||{},value=item=>typeof item==='string'?item:item.term||item.word||'';
 return `<div class="orchestration-review-heading"><div><span class="eyebrow">REVIEW & CONTINUE</span><h3>${checkpoint.type==='classifications'?'検索に使う分類を確認':'次の検索式を確認'}</h3></div><span class="pill">ここで調整できます</span></div><p class="caption">選択した分類をORでまとめ、キーワードと組み合わせます。FI・Fターム由来のIPCは検索候補として扱います。</p><p id="orchestration-additional-selection" class="caption">確認待ち中に他タブで新しく選んだ分類は、続行時に保持します。</p>
 <div class="orchestration-candidate-toolbar"><label>コード・名称で絞り込む<input id="orchestration-candidate-search" type="search" placeholder="例：G05D / 車両 / control" autocomplete="off"></label><p class="caption" id="orchestration-candidate-count" role="status">表示 ${candidates.length} / ${candidates.length} 件 · 選択 ${candidates.filter(item=>selected.has(orchestrationCandidate(item).key)&&item.selectable!==false).length} 件</p><small>選択済みを先頭に表示。絞り込んでも選択は保持します。</small></div>
 <div class="orchestration-candidates">${candidates.map(item=>{const c=orchestrationCandidate(item),derived=item.evidence_status==='derived_candidate'||(item.classification_origins||[]).some(o=>['fi_to_ipc','fterm_theme_to_ipc'].includes(o.type));return `<label class="orchestration-candidate" data-orchestration-candidate-key="${esc(c.key)}" data-orchestration-search="${esc([c.kind,c.code,c.title,item.title_ja,item.title_en,item.title].filter(Boolean).join(' '))}"><input type="checkbox" data-orchestration-key="${esc(c.key)}" ${selected.has(c.key)?'checked':''} ${item.selectable===false?'disabled':''}><span><b>${esc(c.kind)} ${esc(c.code)}</b><span>${esc(c.title)}</span><small>${derived?'対応づけから得た候補 · 公報付与IPCとは別':item.evidence_status==='patent_metadata'?'取り込んだ公報の分類':'探索候補'}${item.reason?` · ${esc(item.reason)}`:''}</small></span></label>`;}).join('')||'<p class="caption">分類候補はありません。キーワードを使った探索を続けられます。</p>'}<p id="orchestration-candidate-empty" class="caption" hidden>該当する候補がありません。コード・名称の絞り込みを変えてください。</p></div>
 ${checkpoint.type==='query'?`<div class="orchestration-term-grid">${[['include','追加する語'],['exclude','除外する語']].map(([kind,title])=>`<div><b>${title}</b><div class="orchestration-terms">${(terms[kind]||[]).map(item=>{const term=value(item);return `<label class="check"><input type="checkbox" data-orchestration-term="${kind}" value="${esc(term)}" ${(checkpoint[kind+'_terms']||[]).includes(term)?'checked':''}>${esc(term)}</label>`;}).join('')||'<span class="caption">候補なし</span>'}</div></div>`).join('')}</div><p class="caption">以下は提案時点の式です。上の選択変更は「確認して続ける」で式へ反映されます。</p><pre class="orchestration-expression">${esc(checkpoint.expression||'選択を確認して検索式を作成します。')}</pre>`:''}
 <div class="button-row"><button class="button primary" id="orchestration-confirm">確認して続ける →</button><button class="button subtle small" id="orchestration-review-map">特許の地図で判断を確認 ↗</button></div>`;
}
function renderOrchestration(){
 const host=$('#orchestration-content');if(!host)return;
 if(!$('#orchestration-criteria')){
  orchestrationDraft=orchestrationDraft||orchestrationDefaults();const d=orchestrationDraft;
  host.innerHTML=`<div class="orchestration-hero panel"><div><span class="eyebrow">ORCHESTRATE YOUR RESEARCH</span><h2>判断をつなぎ、<br>探索をひとつ先へ。</h2><p class="muted">分類探索・特許判定・学習・検索式の更新を、同じ特許群と判断でつなぎます。</p></div><div class="orchestration-hero-status"><span id="orchestration-cycle" class="eyebrow">CYCLE 01</span><b id="orchestration-status">開始前</b><span class="caption">必要なところで、人が確認できます。</span></div></div>
  <ol class="orchestration-timeline" id="orchestration-timeline" aria-label="統括ワークフローの工程"></ol>
  <div class="orchestration-layout"><div class="panel orchestration-config"><span class="eyebrow">WORKFLOW SETTINGS</span><h3>このサイクルの進め方</h3><p id="orchestration-keywords" class="caption"></p><label>必要・不要の判定基準<textarea id="orchestration-criteria" rows="4" maxlength="5000" placeholder="対象技術と、除外したい範囲を記載">${esc(d.criteria)}</textarea></label>
  <label class="check"><input id="orchestration-judge" type="checkbox" ${d.judge?'checked':''}>LLMで未判定の特許を判定する</label><p id="orchestration-llm-note" class="caption"></p><div class="form-row"><label>今回の判定上限<input id="orchestration-limit" type="number" min="1" max="5000" step="1" value="${esc(d.max_items)}"></label><label>採用する信頼度<input id="orchestration-threshold" type="number" min="0.5" max="1" step="0.05" value="${esc(d.threshold)}"></label></div><p class="caption">信頼度はLLMの自己評価です。低信頼度の判定は保留にし、人の確定判定を優先します。</p>
  <label>判定後の学習<select id="orchestration-learning"><option value="none" ${d.learning==='none'?'selected':''}>学習せず、判定を検索式へ反映</option><option value="lightweight" ${d.learning==='lightweight'?'selected':''}>軽量学習 · TF-IDF</option><option value="transformer" ${d.learning==='transformer'?'selected':''}>Transformer本体を微調整</option></select></label><label class="check"><input id="orchestration-include-agent" type="checkbox" ${d.include_agent?'checked':''}>学習にAIの採用済み判定を含める</label><p class="caption">軽量学習は必要・不要が各2件、Transformerは各4件から実行します。</p><label class="check orchestration-review-switch"><input id="orchestration-review" type="checkbox" ${d.review?'checked':''}>分類・検索式の更新前に確認を挟む</label><button class="button primary wide" id="orchestration-start">1サイクルを開始 →</button><p class="caption" id="orchestration-start-note"></p></div>
  <div class="orchestration-main"><div class="panel orchestration-progress"><div class="panel-heading"><h3>進捗と保存済みの判断</h3><strong id="orchestration-percent">0%</strong></div><progress id="orchestration-progress" max="100" value="0" aria-label="統括ワークフロー全体の進捗"></progress><p id="orchestration-message" role="status" aria-live="polite"></p><div id="orchestration-stage-progress"></div><div id="orchestration-counts" class="orchestration-counts"></div><p class="caption" id="orchestration-data-note"></p><div class="button-row"><button id="orchestration-stop" class="button subtle small" hidden>停止する</button><button id="orchestration-resume" class="button primary small" hidden>保存したところから再開 →</button><button id="orchestration-reset" class="button small subtle" hidden>工程をリセット</button><button id="orchestration-map" class="button small subtle">特許の地図 ↗</button><button id="orchestration-lab" class="button small subtle">探索ラボ ↗</button></div><p class="orchestration-error" id="orchestration-error" role="alert" hidden></p></div>
  <div class="panel orchestration-review" id="orchestration-checkpoint" hidden></div><div class="panel orchestration-next" id="orchestration-next" hidden><span class="eyebrow">READY FOR THE NEXT CYCLE</span><h3>検索して、次のCSVを。</h3><p>更新した検索式で検索し、結果を追加して次のサイクルへ進めます。</p><div class="button-row"><button id="orchestration-query" class="button primary">検索式の形式を選んで出力 ↗</button><button id="orchestration-import" class="button">次のCSVを追加 →</button><button id="orchestration-next-cycle" class="button subtle">CSV取込後、次のサイクルへ →</button></div><p class="caption">同じ公報の判定を保持して追加できます。検索サイトへの実行・CSVの取得は手動です。</p></div>
  <div class="panel orchestration-log-panel"><div class="panel-heading"><h3>工程の記録</h3><a class="button small subtle" href="/api/export" download>全体を保存 ↓</a></div><div id="orchestration-log" class="orchestration-log"></div></div></div></div>`;
  for(const id of ['criteria','judge','limit','threshold','learning','include-agent','review'])on('#orchestration-'+id,'change',captureOrchestrationDraft);
  on('#orchestration-judge','change',renderOrchestrationProgress);on('#orchestration-learning','change',renderOrchestrationProgress);
  on('#orchestration-reset','click',()=>orchestrationAction('reset'));on('#orchestration-start','click',()=>startOrchestration());on('#orchestration-stop','click',()=>orchestrationAction('stop'));on('#orchestration-resume','click',()=>orchestrationAction('resume',orchestrationResumePayload()));
  on('#orchestration-map','click',()=>{setTab('explore');setStep(2);});on('#orchestration-lab','click',()=>setTab('discovery'));
  on('#orchestration-query','click',()=>{setTab('explore');setStep(3);});on('#orchestration-import','click',()=>{setTab('explore');setStep(1);if($('#csv-import-mode'))$('#csv-import-mode').value='merge';});on('#orchestration-next-cycle','click',()=>orchestrationAction('resume'));
 }
 renderOrchestrationProgress();
}
function renderOrchestrationProgress(){
 if(!$('#orchestration-status'))return;
 if(typeof renderConvergence==='function'){
  if(!$('#convergence-content')){const convergenceHost=document.createElement('div');convergenceHost.id='convergence-content';$('#orchestration-content .orchestration-main').prepend(convergenceHost);}
  renderConvergence();
 }
 const workflow=state.orchestration||{},status=workflow.status||'idle',running=status==='running',otherRunning=state.job?.status==='running'&&state.job?.kind!=='orchestration',trainRecovery=['paused','error'].includes(status)&&workflow.stage==='train';
 const locked=orchestrationStarting||['running','review','paused','error','waiting_csv'].includes(status),percent=orchestrationPercent(workflow.progress,status),c=orchestrationCounts(state.patents);
 $('#orchestration-status').textContent=orchestrationStatus(status);$('#orchestration-cycle').textContent='CYCLE '+String(workflow.cycle||1).padStart(2,'0');$('#orchestration-percent').textContent=percent+'%';$('#orchestration-progress').value=percent;$('#orchestration-message').textContent=workflow.message||'現在の特許群と、保存済みの分類を起点にします。';$('#orchestration-keywords').textContent='探索キーワード：'+(state.keywords||'未設定（対話で探索から設定）');
 const stage=workflow.stage==='classifications'?'discover':workflow.stage==='refine'?'query':workflow.stage,index=orchestrationStages.findIndex(([id])=>id===stage);
 $('#orchestration-timeline').innerHTML=orchestrationStages.map(([id,label],i)=>{const saved=(workflow.steps||[]).find(s=>s.id===id),active=id===stage,done=!active&&(status==='waiting_csv'&&i<4||saved?.status==='done'||saved?.status==='completed'||index>i);return `<li class="${done?'complete':active?'current':''}" ${active?'aria-current="step"':''}><span class="orchestration-step-number">${done?'✓':String(i+1).padStart(2,'0')}</span><div><b>${label}</b><small>${saved?.status==='skipped'?'スキップ':done?'完了':active?orchestrationStatus(status):'待機'}</small></div></li>`;}).join('');
 const training=state.training||{},job=state.job||{},sameJob=training.job_id===job.id&&job.kind==='orchestration',stageCount=running&&stage==='assess'&&sameJob&&training.mode==='llm'?`${Number(training.completed_count||0)} / ${Number(training.total_count||0)} 件`:'';
 $('#orchestration-stage-progress').innerHTML=stageCount?`<span>今回の特許判定：${stageCount}</span><progress max="${Math.max(1,Number(training.total_count)||1)}" value="${Math.max(0,Number(training.completed_count)||0)}" aria-label="現在工程の特許判定進捗"></progress>`:running?'実行中の工程：'+esc(orchestrationStages.find(([id])=>id===stage)?.[1]||'準備'):'割合は工程の進み具合を示します。';
 $('#orchestration-counts').innerHTML=[['取込済み',c.total],['必要',c.keep],['不要',c.exclude],['保留',c.deferred],['未判定',c.pending]].map(([label,count])=>`<div><strong>${count}</strong><span>${label}</span></div>`).join('');
 $('#orchestration-data-note').textContent=`全体の保存済み件数 · 手動判定 ${c.human} 件を保持。IPC ${c.ipc} 件 / FI ${c.fi} 件 / Fターム ${c.fterm} 件。${c.total&&!state.patents.some(p=>String(p.abstract||'').trim())?'要約未取込のため、タイトル中心の判断です。':''}`;
 $('#orchestration-llm-note').textContent=state.settings.provider==='offline'?'LLM未接続。判定のチェックを外すと、保存済みの判断を使って進められます。':`接続先：${state.settings.provider==='local'?'Local LLM':'LLM API'} · ${state.settings.model||'モデル未設定'}。判定対象の特許情報と判定基準を送信します。`;
 for(const id of ['criteria','judge','learning','review'])$('#orchestration-'+id).disabled=locked||otherRunning;
 for(const id of ['limit','threshold'])$('#orchestration-'+id).disabled=locked||otherRunning||!$('#orchestration-judge').checked;
 if(trainRecovery)$('#orchestration-learning').disabled=orchestrationStarting||otherRunning;$('#orchestration-include-agent').disabled=(locked&&!trainRecovery)||orchestrationStarting||otherRunning||$('#orchestration-learning').value==='none';
 $('#orchestration-start').disabled=locked||otherRunning||!c.total;$('#orchestration-start-note').textContent=!c.total?'先に「対話で探索」のStep 01からCSVを取り込んでください。':otherRunning?'別の処理が実行中です。完了後に開始できます。':trainRecovery?'学習方式を変更して再開できます。「学習せず」を選ぶと保存済みの判定から検索式の更新へ進みます。':locked?'現在のサイクルは開始時の設定を使います。確認・再開で進めてください。':'手動判定・AI判定・保留を再利用します。基準を変更しても既判定を自動で上書きしません。';
 const stop=$('#orchestration-stop');stop.hidden=!['running','review'].includes(status);stop.disabled=orchestrationStarting||!!job.stop_requested;stop.textContent=job.stop_requested?'停止を待っています…':'停止する';
 const resume=$('#orchestration-resume');resume.hidden=!['paused','error'].includes(status);resume.disabled=orchestrationStarting||otherRunning;
 const error=$('#orchestration-error');error.hidden=!workflow.error;error.textContent=workflow.error||'';$('#orchestration-next').hidden=status!=='waiting_csv';$('#orchestration-next-cycle').disabled=orchestrationStarting||otherRunning;$('#orchestration-reset').hidden=!['paused','error','review','waiting_csv'].includes(status);$('#orchestration-reset').disabled=orchestrationStarting||otherRunning;
 const checkpoint=workflow.checkpoint,showCheckpoint=status==='review'&&checkpoint,key=showCheckpoint?`${workflow.id}:${workflow.cycle}:${checkpoint.type}`:'';
 $('#orchestration-checkpoint').hidden=!showCheckpoint;
 if(key&&key!==orchestrationCheckpointKey){$('#orchestration-checkpoint').innerHTML=orchestrationCheckpointHtml(checkpoint);orchestrationCheckpointKey=key;on('#orchestration-candidate-search','input',filterOrchestrationCandidates);if(!$('#orchestration-checkpoint').dataset.filterBound){on('#orchestration-checkpoint','change',filterOrchestrationCandidates);$('#orchestration-checkpoint').dataset.filterBound='1';}filterOrchestrationCandidates();on('#orchestration-confirm','click',()=>orchestrationAction('resume',orchestrationReviewPayload(checkpoint.type)));on('#orchestration-review-map','click',()=>{setTab('explore');setStep(2);});}else if(!key)orchestrationCheckpointKey='';
 if($('#orchestration-confirm'))$('#orchestration-confirm').disabled=orchestrationStarting||otherRunning;
 if(showCheckpoint&&$('#orchestration-additional-selection')){const baseline=new Set(checkpoint.selection_baseline||checkpoint.selected_keys||[]),added=(state.selected||[]).filter(key=>!baseline.has(key));$('#orchestration-additional-selection').textContent='確認待ち中に他タブで新しく選んだ分類は、続行時に保持します。'+(added.length?' 追加された分類：'+added.join('、'):'');}
 const logs=workflow.logs||[],logHost=$('#orchestration-log'),signature=JSON.stringify(logs);if(logHost.dataset.signature!==signature){const atBottom=logHost.scrollHeight-logHost.scrollTop-logHost.clientHeight<40;logHost.innerHTML=logs.length?logs.map(row=>`<div><time>${esc(row.time||'')}</time><span>${esc(row.message||'')}</span></div>`).join(''):'<p class="caption">実行すると、使った分類・判定・学習の工程を記録します。</p>';logHost.dataset.signature=signature;if(atBottom)logHost.scrollTop=logHost.scrollHeight;}
}
function orchestrationResumePayload(){return ['paused','error'].includes(state.orchestration?.status)&&state.orchestration?.stage==='train'?{learning:$('#orchestration-learning').value,include_agent:$('#orchestration-include-agent').checked}:{};}
function orchestrationReviewPayload(type){
 const body={selected_keys:$$('[data-orchestration-key]').filter(el=>el.checked&&!el.disabled).map(el=>el.dataset.orchestrationKey)};
 if(type==='query')for(const kind of ['include','exclude'])body[kind+'_terms']=$$(`[data-orchestration-term="${kind}"]`).filter(el=>el.checked).map(el=>el.value);
 return body;
}
async function startOrchestration(){
 if(orchestrationStarting||state.job?.status==='running')return;captureOrchestrationDraft();const body=orchestrationOptions(orchestrationDraft);
 await selectionSave;await orchestrationAction('start',body);
}
async function orchestrationAction(action,body={}){
 if(orchestrationStarting)return;orchestrationStarting=true;renderOrchestrationProgress();
 try{await api('/orchestration/'+action,body);state=await api('/state');updateChrome();renderOrchestrationProgress();if(action==='reset'){orchestrationCheckpointKey='';toast('工程をリセットしました。特許・判定・検索式は保持しています。');}if(action==='stop')toast('停止を受け付けました。保存済みの結果は保持します。');}
 finally{orchestrationStarting=false;renderOrchestrationProgress();}
}
