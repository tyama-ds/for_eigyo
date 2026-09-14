/* Hierarchy browsing, source patents, and explicit selection are separate actions. */
let classFocusKey=null, classSourceMode='keywords', classDraft=null, classUndo=[], classBusy=false;
const targetPatentIds=new Set();
let classLayoutCache;
let classMotionController;
function syncClassMotion(){classMotionController?.();}
let classLanguage='ja', classTooltipTimer;
let classTranslationPending=false;
let classProposalView=false, classProposalAttempt=null;
let classSelectionStale=false;
function classSelectionSyncHtml(){return classSelectionStale?'<div class="class-proposal-error" role="alert">最新の分類・選択を取得できていません。古い選択による上書きを防ぐため、分類の変更と初案作成を一時停止しています。<button id="class-resync" class="button small">最新の状態を再取得</button></div>':'';}
function blockStaleClassSelection(){
 classSelectionStale=true;classUndo=[];
 selectionSave=Promise.reject(new Error('最新の分類・選択との同期が必要です。「対話で探索」で最新の状態を再取得してください。'));
 selectionSave.catch(()=>{});
}
async function syncClassSelection(){
 if(classBusy)return false;
 captureClassDraft();classBusy=true;const root=$('#step-content');if(root){root.inert=true;root.setAttribute('aria-busy','true');}
 try{
  const fresh=await api('/state');
  if(!Array.isArray(fresh.selected)||!Array.isArray(fresh.candidates))throw new Error('分類・選択の応答を確認できませんでした。');
  const fields=['selected','candidates','classification_view','candidate_proposal','keywords','settings','classification_catalog'];
  state={...state,...Object.fromEntries(fields.filter(field=>Object.prototype.hasOwnProperty.call(fresh,field)).map(field=>[field,fresh[field]]))};
  selectionSave=Promise.resolve();classSelectionStale=false;classUndo=[];classProposalView=false;classFocusKey=null;
  if(classProposalAttempt)classProposalAttempt.synced=true;
  return true;
 }catch(error){blockStaleClassSelection();throw error;}
 finally{classBusy=false;if(root){root.inert=false;root.removeAttribute('aria-busy');}}
}
function classProposalItems(){return (state.candidate_proposal?.items||[]).filter(item=>state.candidates.some(c=>keyOf(c)===keyOf(item)));}
function classProposalReason(item){
 const proposal=(state.candidate_proposal?.items||[]).find(row=>keyOf(row)===keyOf(item));
 return proposal?.reason||item.llm_reason||'';
}
function classProposalReasonHtml(item){const reason=classProposalReason(item);return reason?`<p class="class-llm-reason"><b>LLMの提案理由</b><br>${esc(reason)}<small>分類の正式名称・定義とは別の、関連性についての提案です。</small></p>`:'';}
function classRejectedCandidateIndex(item,index){return Number.isInteger(item.index)&&item.index>=0?item.index:index;}
function classRejectedBroader(item){const broader=item.broader_candidate;return broader?.kind==='IPC'&&broader.verified===true&&broader.selectable!==false&&broader.code?broader:null;}
function classRejectedCandidateHtml(item,index){
 const revision=item.revision, broader=classRejectedBroader(item), proposal=state.candidate_proposal;
 const reasonRepeated=revision?.status==='retired'&&item.reason===revision.explanation;
 const sources=(Array.isArray(revision?.sources)?revision.sources:[]).filter(source=>typeof source?.url==='string'&&/^https:\/\//i.test(source.url));
 const retired=revision?.status==='retired'?`<div class="class-revision-note"><span class="class-revision-label">旧版の分類</span><p>${esc(revision.explanation||'このコードは旧版のIPCに収録されていました。現行版とは区別して確認してください。')}</p>${revision.last_verified_version||revision.changed_version?`<p class="class-revision-versions">${revision.last_verified_version?`収録を確認した版：${esc(revision.last_verified_version)}`:''}${revision.last_verified_version&&revision.changed_version?' · ':''}${revision.changed_version?`変更された版：${esc(revision.changed_version)}`:''}</p>`:''}${sources.length?`<div class="class-revision-sources">${sources.map(source=>`<a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.title||'公式資料')} ↗</a>`).join('')}</div>`:''}</div>`:'';
 const added=item.broader_added===true, canAdd=broader&&proposal?.id;
 return `<article class="class-rejected-item"><div class="class-proposal-code"><strong>${esc(item.kind||'')} ${esc(item.code||'コードなし')}</strong><span>除外</span></div>${reasonRepeated?'':`<p>${esc(item.reason||'形式・辞書との照合で除外')}</p>`}${retired}${broader?`<div class="class-broader-proposal"><span class="class-broader-label">確認済みの上位候補</span><strong>IPC ${esc(broader.code)}</strong><p class="class-broader-title">${esc(classTitle(broader))}</p><p>${esc(broader.reason||'現行IPCの上位分類を候補として確認できます。')}</p><p class="caption">上位分類のため検索範囲が広がります。候補への追加後、地図で選択すると初案に使えます。</p>${added?'<span class="class-broader-added" role="status">✓ 上位分類を候補に追加済み</span>':canAdd?`<button class="button small" data-proposal-broader="${classRejectedCandidateIndex(item,index)}">上位の ${esc(broader.code)} を候補に追加</button>`:'<p class="caption">候補を再生成すると、この上位分類を追加できます。</p>'}</div>`:''}</article>`;
}
function classProposalPanelHtml(){
 const proposal=state.candidate_proposal, error=classProposalAttempt?.status==='error';
 const failure=error?`<p class="class-proposal-error" role="alert">${classProposalAttempt.action==='broader'?'上位候補を追加できませんでした。':'今回の候補生成に失敗しました。'}<br>${esc(classProposalAttempt.message)}<br>${classSelectionStale?'最新の状態を取得するまで、分類の変更は停止しています。':classProposalAttempt.synced?'他の操作で変更された最新の候補・選択を再取得しました。入力の下書きは保持しています。':'前回の候補・選択は保持しています。'}</p>`:'';
 if(!proposal)return `${failure}<div class="class-proposal-heading"><span class="eyebrow">LLM RESPONSE</span><h3>提案の反映先を確認</h3></div><p class="caption">これまでのLLM返答の記録はありません。「LLMからも候補を提案」を有効にして候補を見直すと、返ってきた分類コード・理由と反映件数をここに表示します。</p>`;
 const rows=proposal.items||[], available=classProposalItems(), rejected=proposal.rejected||[];
 const accepted=Number(proposal.accepted_count??rows.length), returned=Number(proposal.returned_count??rows.length), isLlm=proposal.llm_used===true;
 const differs=String(proposal.keywords||'')!==String(state.keywords||'');
 const title=error&&classProposalAttempt.action!=='broader'?'前回の候補生成結果':isLlm?'今回のLLM提案':'今回の候補生成';
 return `${failure}<div class="class-proposal-heading"><span class="eyebrow">LLM RESPONSE</span><h3>${title}</h3></div><p class="class-proposal-context">対象：${esc(proposal.keywords||'キーワードなし')}${differs?' <b>（現在のテーマとは異なります）</b>':''}</p>${!isLlm?'<p class="caption">今回はLLMからの提案を取得していません。辞書・既存の起点から生成した候補を地図に表示しています。</p>':`<div class="class-proposal-metrics"><span><b>${returned}</b>返却</span><span><b>${accepted}</b>候補に反映</span><span><b>${Number(proposal.new_count||0)}</b>新規</span><span><b>${Number(proposal.existing_count||0)}</b>既存と重複</span><span><b>${Number(proposal.rejected_count??rejected.length)}</b>除外</span></div><p class="caption">反映した候補はコンステレーションで確認できます。<b>検索式に使うには分類を選び、「この条件で初案をつくる」を押してください。</b>候補への反映だけでは選択・検索式は変わりません。</p>${available.length?`<button id="class-proposal-map" class="button small wide ${classProposalView?'subtle':''}">${classProposalView?'地図に今回のLLM候補を表示中':'今回のLLM候補を地図で見る'} (${available.length})</button>`:''}<div class="class-proposal-items">${rows.map(item=>{const key=keyOf(item), actual=state.candidates.find(c=>keyOf(c)===key), selected=state.selected.includes(key);const kind=item.is_new===true||item.status==='new'?'新規':item.is_new===false||item.status==='existing'?'既存の候補':'反映済み';return `<article class="class-proposal-item"><div class="class-proposal-code"><strong>${esc(item.kind)} ${esc(item.code)}</strong><span class="${kind==='新規'?'is-new':''}">${kind}</span></div><p>${esc(actual?classTitle(actual):item.title||'')}</p><p class="class-proposal-reason"><b>LLMの提案理由</b><br>${esc(item.reason||'理由の記載なし')}</p>${actual?`<button class="button small ${selected?'primary':'subtle'}" data-proposal-select="${esc(key)}" aria-pressed="${selected}" ${actual.selectable===false?'disabled':''}>${actual.selectable===false?'案内用・下位分類を選択':selected?'✓ 検索に使用中 · 解除':'＋ 検索に使う'}</button>`:'<small class="caption">現在の候補一覧にはありません。候補を見直してください。</small>'}</article>`;}).join('')}</div>${!rows.length?'<p class="caption">今回、地図に反映できたLLM候補はありません。</p>':''}${rejected.length?`<details class="class-proposal-rejected"><summary>除外された候補と理由 (${rejected.length})</summary>${rejected.map(classRejectedCandidateHtml).join('')}</details>`:''}`}${(proposal.notices||[]).length?`<div class="class-proposal-notices">${proposal.notices.map(text=>`<p>${esc(text)}</p>`).join('')}</div>`:''}`;
}
function renderClassProposalFeedback(){
 const host=$('#class-proposal-feedback');if(!host)return;
 host.innerHTML=classProposalPanelHtml();
 on('#class-proposal-map','click',showClassProposalMap);
 $$('[data-proposal-select]',host).forEach(button=>button.addEventListener('click',()=>toggleClass(button.dataset.proposalSelect)));
 $$('[data-proposal-broader]',host).forEach(button=>button.addEventListener('click',run(()=>addClassProposalBroader(Number(button.dataset.proposalBroader)))));
}
function showClassProposalMap(){
 if(classBusy||!classProposalItems().length)return;
 captureClassDraft();classProposalView=true;classFocusKey=keyOf(classProposalItems()[0]);renderClassResponse();
 $('#class-svg')?.scrollIntoView?.({block:'center',behavior:'smooth'});
}
async function suggestClassCandidates(body){
 classProposalAttempt=null;
 try{if(await requestClassState(()=>api('/candidates',body))){classProposalView=false;classFocusKey=null;renderClassResponse();}}
 catch(error){
  classProposalAttempt={status:'error',message:error.message};
  if(error.status===409){
   blockStaleClassSelection();
   try{await syncClassSelection();}catch(syncError){classProposalAttempt.message+=' 最新の状態の再取得にも失敗しました：'+syncError.message;}
  }
  renderClassResponse();throw error;
 }
}
async function addClassProposalBroader(index){
 if(classBusy||!Number.isInteger(index)||index<0)return false;
 const proposal=state.candidate_proposal, rejected=(proposal?.rejected||[]).find((item,position)=>classRejectedCandidateIndex(item,position)===index);
 const broader=rejected&&classRejectedBroader(rejected);
 if(!proposal?.id||!broader||rejected.broader_added===true)return false;
 captureClassDraft();classProposalAttempt=null;
 try{
  if(!await requestClassState(()=>api('/candidates/broader',{proposal_id:proposal.id,rejected_index:index})))return false;
  classProposalView=false;classFocusKey=keyOf(broader);renderClassResponse();
  if(activeTab==='explore'&&step===0)$('#class-svg')?.scrollIntoView?.({block:'center',behavior:'smooth'});
  toast('上位IPC '+broader.code+' を候補に追加しました。地図で選択すると初案に使えます。');
  return true;
 }catch(error){
  classProposalAttempt={status:'error',action:'broader',message:error.message};
  if(error.status===409){
   blockStaleClassSelection();
   try{await syncClassSelection();}catch(syncError){classProposalAttempt.message+=' 最新の状態の再取得にも失敗しました：'+syncError.message;}
  }
  renderClassResponse();throw error;
 }
}
try { if (globalThis.localStorage?.getItem('patent-class-label-language') === 'en') classLanguage='en'; } catch (_) { /* Display preferences are optional. */ }
function classTitle(item){return PatentClassLabels.label(item,classLanguage);}
function classScopeText(item){
 if(item.kind==='IPC'&&item.selection_scope==='subtree')return '上位IPC・下位分類を含む広い範囲として選択できます。';
 if(item.selectable===false)return item.kind==='F-term'?'案内用のテーマ・観点です。下位のタームを選択してください。':'この上位コードは辞書で未確認です。「広げる」か分類指定で確認してください。';
 return '';
}
function applyClassTranslations(rows){
 const byKey=new Map((rows||[]).map(row=>[`${row.kind}:${row.code}`,Object.fromEntries(Object.entries(row).filter(([name])=>/^title_(ja|en)(_|$)|^title_short_/.test(name)))]));
 const merge=item=>({...item,...(byKey.get(keyOf(item))||{})});
 state.candidates=state.candidates.map(merge);
 if(state.discovery?.plan)state.discovery.plan.candidate_hypotheses=(state.discovery.plan.candidate_hypotheses||[]).map(merge);
 if(state.discovery?.analysis)state.discovery.analysis.recommendations=(state.discovery.analysis.recommendations||[]).map(merge);
}
function renderClassTranslationStatus(){
 const host=$('#class-translation-status');if(!host)return;
 const list=classificationItems(),missing=list.filter(c=>c.kind==='IPC'&&c.verified&&!c.title_ja);
 const fallbackCount=list.filter(c=>PatentClassLabels.fallback(c,classLanguage)).length;
 host.innerHTML=`名称のみ切り替えます。ホバーで日本語・英語の定義を表示。${fallbackCount?` 未収録の${fallbackCount}件は原文で表示します。`:''}${missing.length?`<button id="class-translate-missing" class="button small subtle" ${classTranslationPending?'disabled':''}>${classTranslationPending?'日本語名を取得中…':`未収録の日本語を取得 (${missing.length})`}</button><span class="class-translation-network">未収録の分類コードをJ-PlatPatへ送信。設定中のproxyを使用します。</span>`:''}`;
 on('#class-translate-missing','click',async()=>{
  if(classTranslationPending)return;
  classTranslationPending=true;renderClassTranslationStatus();
  try{
   const result=await api('/classifications/translations',{codes:missing.slice(0,48).map(c=>c.code)});
   applyClassTranslations(result.translations);
   if(activeTab==='explore'&&step===0){drawClassNodes();updateSelectedClasses();renderClassFocus();}
   toast(result.warnings?.length?result.warnings.join(' '):'日本語の分類名を保存しました。');
  }finally{classTranslationPending=false;if(activeTab==='explore'&&step===0)renderClassTranslationStatus();}
 });
}
function setClassLanguage(value){
 if(!['ja','en'].includes(value))return;
 classLanguage=value;
 try{globalThis.localStorage?.setItem('patent-class-label-language',value);}catch(_){}
 drawClassNodes();updateSelectedClasses();renderClassFocus();
 const focus=classFocus();if(focus)classDetail(focus);
}
function hideClassTooltip(){clearTimeout(classTooltipTimer);const tip=$('#class-tooltip');if(tip)tip.hidden=true;}
function queueHideClassTooltip(){clearTimeout(classTooltipTimer);classTooltipTimer=setTimeout(hideClassTooltip,180);}
function classNamesHtml(item){
 const names=PatentClassLabels.bilingual(item);
 return `<dl class="class-bilingual"><div><dt>日本語 <small>${esc(PatentClassLabels.status(names.jaStatus))} ${esc(item.title_ja_version||'')}</small></dt><dd lang="ja">${esc(names.ja||'日本語訳は未収録です。')}${item.title_ja_parent?`<small class="class-parent-caption">上位：${esc(item.parent||item.title_ja_parent_code||'')} ${esc(item.title_ja_parent)}</small>`:''}</dd></div><div><dt>English <small>${esc(PatentClassLabels.status(names.enStatus))} ${esc(item.title_en_version||'')}</small></dt><dd lang="en">${esc(names.en||'English translation is not available.')}${item.title_en_parent?`<small class="class-parent-caption">Parent: ${esc(item.parent||item.title_en_parent_code||'')} ${esc(item.title_en_parent)}</small>`:''}</dd></div></dl>`;
}
function positionClassTooltip(event,node){
 const tip=$('#class-tooltip');if(!tip||tip.hidden)return;
 const box=node.getBoundingClientRect(),x=Number.isFinite(event?.clientX)?event.clientX:box.right,y=Number.isFinite(event?.clientY)?event.clientY:box.top;
 const bounds=tip.getBoundingClientRect(),margin=12;
 let left=x+16,top=y+16;
 if(left+bounds.width>window.innerWidth-margin)left=x-bounds.width-16;
 if(top+bounds.height>window.innerHeight-margin)top=y-bounds.height-12;
 tip.style.left=Math.max(margin,Math.min(left,window.innerWidth-bounds.width-margin))+'px';
 tip.style.top=Math.max(margin,Math.min(top,window.innerHeight-bounds.height-margin))+'px';
}
function showClassTooltip(item,event,node){
 if(classBusy||$('#class-svg')?.dataset.drawing)return;
 clearTimeout(classTooltipTimer);
 let tip=$('#class-tooltip');
 if(!tip){tip=document.createElement('div');tip.id='class-tooltip';tip.className='class-tooltip';tip.setAttribute('role','tooltip');tip.addEventListener('mouseenter',()=>clearTimeout(classTooltipTimer));tip.addEventListener('mouseleave',queueHideClassTooltip);document.body.appendChild(tip);}
 tip.innerHTML=`<strong>${esc(item.kind)} ${esc(item.code)}</strong>${classScopeText(item)?`<p class="caption">${esc(classScopeText(item))}</p>`:''}${classNamesHtml(item)}${classProposalReasonHtml(item)}${PatentClassLabels.fallback(item,classLanguage)?`<p class="caption">${esc(PatentClassLabels.fallback(item,classLanguage))}</p>`:''}`;
 tip.hidden=false;positionClassTooltip(event,node);
}
function classLayoutMode(){return state.settings?.classification_layout==='grid'?'grid':'semantic';}
function describeClassLayout(){const grid=$('#classification-layout')?.value==='grid';if($('#layout-description'))$('#layout-description').textContent=grid?'分類を等間隔で整列し、コードや名称を順に確認できます。':'材料・製法などの技術項目、分類名・関連語、正式な分類階層から近い候補をまとめます。まとまりには見出しが付きます。同じ候補は同じ配置を保ち、距離は近さの目安です。';}
function classificationItems(){const keys=classProposalView?classProposalItems().map(keyOf):state.classification_view?.keys;return keys?keys.map(k=>state.candidates.find(c=>keyOf(c)===k)).filter(Boolean):state.candidates;}
function classFocus(){return state.candidates.find(c=>keyOf(c)===classFocusKey)||state.classification_view?.focus||state.candidates.find(c=>keyOf(c)===state.selected[0])||classificationItems()[0];}
function captureClassDraft(){if($('#keywords'))classDraft={...(classDraft||{}),keywords:$('#keywords').value};for(const id of ['seed-ipc','seed-fterm','seed-cpc','seed-label','target-search','class-search'])if($('#'+id))classDraft[id]=$('#'+id).value;}
function resetClassExplorer(){classFocusKey=null;classDraft=null;classUndo=[];classProposalView=false;classProposalAttempt=null;if(classSelectionStale)selectionSave=Promise.resolve();classSelectionStale=false;targetPatentIds.clear();}
async function requestClassState(request){if(classBusy)return false;if(classSelectionStale)throw new Error('最新の分類・選択との同期が必要です。「最新の状態を再取得」を押してください。');classBusy=true;$('#step-content').inert=true;$('#step-content').setAttribute('aria-busy','true');try{await selectionSave.catch(async error=>{const fresh=await api('/state');state.selected=fresh.selected;selectionSave=Promise.resolve();classUndo=[];updateSelectedClasses();throw new Error('選択を保存できなかったため、保存済みの状態に戻しました。再操作してください。 '+error.message);});state=await request();return true;}finally{classBusy=false;$('#step-content').inert=false;$('#step-content').removeAttribute('aria-busy');}}
function renderClassResponse(){if(activeTab==='explore'&&step===0)renderStep();}
function sourcePanel(){
 const draft=classDraft||{};
 if(classSourceMode==='manual')return `<div class="seed-panel"><label>推薦するIPC<textarea id="seed-ipc" rows="2" placeholder="H01M 10/00&#10;A61K 9/00">${esc(draft['seed-ipc']||'')}</textarea></label><label>推薦するF-term<textarea id="seed-fterm" rows="2" placeholder="5H029AM11, 5H029AJ06">${esc(draft['seed-fterm']||'')}</textarea></label><details><summary>CPCも指定する</summary><label>CPC<textarea id="seed-cpc" rows="2" placeholder="H01M10/0562">${esc(draft['seed-cpc']||'')}</textarea></label></details><label>出典メモ（任意）<input id="seed-label" value="${esc(draft['seed-label']||'')}" placeholder="例：ターゲット JP… の分類"></label><button id="add-seeds" class="button primary wide">この分類を起点にする ↗</button><p class="caption">空白・改行・カンマ区切りで複数入力。名称は辞書から取得します。IPC / CPC / F-termは別々の欄に入力してください。</p></div>`;
 if(classSourceMode==='patent')return `<div class="seed-panel"><p class="caption">取り込み済みのIPC・Fタームを起点にします。FI・Fタームから得たIPC候補は、元の付与IPCと区別して表示します。要否判定は保持します。</p><input id="target-search" aria-label="ターゲット特許を検索" placeholder="公報番号・名称で検索" value="${esc(draft['target-search']||'')}"><div id="target-patent-list" class="target-patent-list"></div><button id="use-targets" class="button primary wide">選んだ特許を起点にする ↗</button><p class="caption">公報番号からの外部取得は未接続です。CSVを取り込むか「分類指定」へ分類を貼り付けてください。</p><button class="button subtle small" id="target-import">CSVを取り込む →</button></div>`;
 return `<label class="check"><input type="checkbox" id="use-llm" ${state.settings.provider!=='offline'?'checked':''}>LLMからも候補を提案</label><button id="suggest" class="button primary wide">キーワードから候補を見直す ↗</button><section id="class-proposal-feedback" class="class-proposal-feedback" aria-label="LLM候補の反映結果" aria-live="polite">${classProposalPanelHtml()}</section><div class="hint-card"><h3>材料・製法まで、候補を広げる。</h3><p>装置・材料・製造・界面などの観点から、分類の分野をまたいで候補を探します。探索ラボと同じ候補生成を使い、関連性は根拠を確認して選べます。</p></div><p class="caption">IPC ${Number(state.classification_catalog?.ipc?.count||0).toLocaleString()}分類・2026.01 / 英語名称。日本語名は一部を収録し、未収録分は地図上の取得ボタンで追加できます。F-term階層は5H029を収録。</p>`;
}
function renderClassifications(){
 if(!classDraft)classDraft={keywords:state.keywords};
 const view=classProposalView?{direction:'llm_proposal',total:classProposalItems().length,note:'今回のLLM提案を表示中です。「分野をまたぐ候補」で全体に戻れます。'}:state.classification_view||{}, list=classificationItems();
 $('#step-content').innerHTML=`${classSelectionSyncHtml()}<div class="explorer-grid classification-explorer"><div class="panel prompt-panel"><span class="eyebrow">CHOOSE YOUR STARTING POINT</span><h2>起点を決めて、<br>範囲を探る。</h2><label>全文キーワード（任意）<textarea id="keywords" rows="3" placeholder="分類のみの検索なら空欄">${esc(classDraft.keywords??state.keywords)}</textarea></label><p class="caption">入力した語句はAND条件になります。英語フレーズは &quot;solid electrolyte&quot; のように囲みます。</p><div class="source-tabs" role="group" aria-label="分類の起点"><button data-source="keywords" class="${classSourceMode==='keywords'?'active':''}">キーワード</button><button data-source="manual" class="${classSourceMode==='manual'?'active':''}">分類指定</button><button data-source="patent" class="${classSourceMode==='patent'?'active':''}">特許から</button></div>${sourcePanel()}</div>
 <div class="panel canvas-panel"><div class="canvas-header"><div><h3>分類のコンステレーション</h3><p>${list.length} / ${view.total??list.length} 件を表示 · ${classLayoutMode()==='semantic'?'近い技術を、近くに':'分類を整列して表示'}</p></div><div class="canvas-toolbar"><label class="class-language-control">名称<select id="class-language" aria-label="コンステレーションの表示言語"><option value="ja" ${classLanguage==='ja'?'selected':''}>日本語</option><option value="en" ${classLanguage==='en'?'selected':''}>English</option></select></label><button class="icon-button" id="class-layout-settings">配置：${classLayoutMode()==='semantic'?'意味の近さ':'整列'} ⚙</button><button class="icon-button ${tool==='lasso'?'active':''}" id="lasso-tool">◌ 囲んで選択</button><button class="icon-button" id="add-class">＋ 推薦</button></div></div>
 <div id="class-translation-status" class="class-translation-note"></div><div class="classification-search"><select id="browse-kind" aria-label="探索する分類体系"><option value="IPC">IPC</option><option value="F-term" ${view.kind==='F-term'?'selected':''}>F-term</option><option value="CPC" ${view.kind==='CPC'?'selected':''}>CPC</option></select><input id="class-search" aria-label="分類コード・名称を検索" placeholder="分類コード・名称（IPCは英語）" value="${esc(classDraft['class-search']||'')}"><button id="find-class" class="button small">探す</button></div>
 <div class="browse-shortcuts"><button id="class-roots" class="button subtle small">全分野から</button><button id="class-overview" class="button subtle small">分野をまたぐ候補</button><button id="class-seeds" class="button subtle small">推薦した分類</button></div><div class="class-trail">${(view.trail||[]).map(t=>`<button class="trail-item" data-code="${esc(t.code)}" data-kind="${esc(t.kind)}" title="${esc(t.title)}">${esc(t.code)}</button>`).join('<span>›</span>')}</div>${view.focus&&['children','broader'].includes(view.direction)?`<p class="class-scope-note">現在は <b>${esc(view.focus.kind)} ${esc(view.focus.code)}</b> の枝を表示しています。他分野も含めて見渡すには「分野をまたぐ候補」を選べます。</p>`:''}<div id="class-focus" class="class-focus"></div>
 <div class="canvas-wrap"><svg id="class-svg" class="constellation ${tool==='lasso'?'lasso-mode':''}" viewBox="0 0 1000 530" aria-label="分類候補。クリックで起点を選び、囲んで複数選択" role="group"><g id="class-regions" aria-hidden="true"></g><g id="class-neighbors" aria-hidden="true"></g><g id="class-links" aria-hidden="true"></g><g id="class-nodes"></g><path class="lasso-path" id="class-lasso"/></svg>${!list.length?'<div class="canvas-empty"><div class="empty-orbit">◎</div><h3>技術の起点を、見つける。</h3><p>「全分野から」で階層をたどるか、IPC・F-termを直接指定できます。</p></div>':''}<div class="float-hint">クリックで起点と選択 · 囲んで一括選択</div></div>
 <div id="class-detail" class="detail-strip" aria-live="polite"></div><div class="browse-note">${esc(view.note||'候補を見渡す操作では、保存済みの検索式は変わりません。')}${view.next_offset!=null?'<button id="class-next" class="button small">次の24件 →</button>':''}${view.offset>0?'<button id="class-prev" class="button small">← 前の24件</button>':''}</div><div class="canvas-footer"><div class="legend"><span><i></i>IPC</span><span><i class="amber"></i>F-term</span><span><i class="blue"></i>CPC</span></div><span>上位IPCも選択可 · 破線：未確認 · ◇：案内用</span></div></div></div>
 <div class="selection-bar"><div class="selection-main"><div class="selected-content" id="selected-classes"></div><p class="caption">上位IPCは下位分類を含む範囲として初案に使えます。分類同士はORで接続し、親を残して子を追加しても狭まらないため、置き換えか個別解除で調整してください。</p><div class="button-row"><button id="clear-class" class="button small subtle">すべて解除</button><button id="undo-class" class="button small subtle" ${classUndo.length?'':'disabled'}>選択を1つ戻す</button></div></div><button class="button primary" id="to-query">この条件で初案をつくる →</button></div><details class="list-toggle"><summary>表示中の分類をリストで確認する</summary><div class="class-list" id="class-list"></div></details>`;
 drawClassNodes();updateSelectedClasses();renderClassFocus();renderClassProposalFeedback();
 on('#class-resync','click',async()=>{try{await syncClassSelection();}finally{renderClassResponse();}});
 on('#class-language','change',e=>setClassLanguage(e.currentTarget.value));
 on('#class-layout-settings','click',()=>{setTab('settings');$('#classification-layout').focus();$('#classification-layout').scrollIntoView({block:'center',behavior:'smooth'});});
 $$('.source-tabs button').forEach(el=>el.addEventListener('click',()=>{captureClassDraft();classSourceMode=el.dataset.source;renderStep();}));
 on('#suggest','click',async e=>busy(e.currentTarget,'候補を探しています…',async()=>{captureClassDraft();const body={keywords:classDraft.keywords,use_llm:$('#use-llm').checked};await suggestClassCandidates(body);}));
 on('#clear-class','click',()=>changeClassSelection([]));on('#undo-class','click',undoClassSelection);
 on('#lasso-tool','click',()=>{tool=tool==='lasso'?'click':'lasso';$('#class-svg').classList.toggle('lasso-mode',tool==='lasso');$('#lasso-tool').classList.toggle('active',tool==='lasso');});
 on('#add-class','click',()=>{captureClassDraft();classSourceMode='manual';renderStep();});
 on('#find-class','click',()=>browseClasses({direction:'search',kind:$('#browse-kind').value,text:$('#class-search').value}));on('#class-search','keydown',e=>{if(e.key==='Enter'){e.preventDefault();return browseClasses({direction:'search',kind:$('#browse-kind').value,text:$('#class-search').value});}});
 on('#class-roots','click',()=>browseClasses({direction:'roots',kind:$('#browse-kind').value}));on('#class-overview','click',()=>browseClasses({direction:'overview'}));on('#class-seeds','click',()=>browseClasses({direction:'seeds'}));
 $$('.trail-item').forEach(el=>el.addEventListener('click',run(()=>browseClasses({direction:'children',code:el.dataset.code,kind:el.dataset.kind}))));
 on('#class-next','click',()=>browseClasses({direction:view.direction,kind:view.kind,code:view.code,text:view.text,offset:view.next_offset}));on('#class-prev','click',()=>browseClasses({direction:view.direction,kind:view.kind,code:view.code,text:view.text,offset:Math.max(0,view.offset-24)}));
 on('#add-seeds','click',async e=>busy(e.currentTarget,'追加しています…',async()=>{captureClassDraft();await addClassSeeds({ipc_text:classDraft['seed-ipc']||'',fterm_text:classDraft['seed-fterm']||'',cpc_text:classDraft['seed-cpc']||'',label:classDraft['seed-label']||'利用者の推薦'});}));
 on('#target-search','input',renderTargetPatents);on('#target-import','click',()=>{captureClassDraft();setStep(1);});
 on('#use-targets','click',async e=>busy(e.currentTarget,'分類を取り出しています…',()=>addClassSeeds({patent_ids:[...targetPatentIds]})));
 if(classSourceMode==='patent')renderTargetPatents();
 on('#to-query','click',async e=>busy(e.currentTarget,'初案を作成中…',async()=>{captureClassDraft();const words=classDraft.keywords;if(await requestClassState(async()=>{await api('/keywords',{keywords:words});return api('/query',{});})){viewedQueryId=null;setStep(1);}}));
}
async function browseClasses(body){captureClassDraft();if(!await requestClassState(()=>api('/classifications/browse',body)))return;classProposalView=false;const focus=state.classification_view?.focus;classFocusKey=focus?keyOf(focus):null;renderClassResponse();}
async function addClassSeeds(body){captureClassDraft();if(!await requestClassState(()=>api('/classifications/seeds',{...body,select:false})))return;classProposalView=false;classFocusKey=state.classification_view.keys[0]||null;renderClassResponse();toast('起点を追加しました。検索に使う分類を選んでください。');}
function renderTargetPatents(){for(const id of targetPatentIds)if(!state.patents.some(p=>p.id===id))targetPatentIds.delete(id);const query=($('#target-search')?.value||'').toLowerCase();const rows=state.patents.filter(p=>(p.id+' '+p.title).toLowerCase().includes(query));$('#target-patent-list').innerHTML=rows.slice(0,30).map(p=>`<label class="target-row"><input type="checkbox" data-id="${esc(p.id)}" ${targetPatentIds.has(p.id)?'checked':''}><span><b>${esc(p.id)}</b><small>${esc(p.title)}</small><small>${p.ipc?'IPC：'+esc(p.ipc):'IPC未取込'}${p.fi?`<br>FI：${esc(p.fi)}`:''}${p.fterm?`<br>Fターム：${esc(p.fterm)}`:''}</small></span></label>`).join('')||'<p class="caption">対象特許がありません。</p>';$('#target-patent-list').insertAdjacentHTML('beforeend',`<p class="caption">${rows.length} 件中 ${Math.min(30,rows.length)} 件表示 · 選択 ${targetPatentIds.size} 件</p>`);$$('#target-patent-list input').forEach(el=>el.addEventListener('change',()=>{if(el.checked)targetPatentIds.add(el.dataset.id);else targetPatentIds.delete(el.dataset.id);renderTargetPatents();}));}
function rememberClassSelection(){classUndo.push([...state.selected]);if(classUndo.length>30)classUndo.shift();}
function changeClassSelection(next){if(classBusy||classSelectionStale)return;rememberClassSelection();state.selected=[...new Set(next)];updateSelectedClasses();persistSelection();}
function undoClassSelection(){if(classBusy||classSelectionStale||!classUndo.length)return;state.selected=classUndo.pop().filter(k=>state.candidates.some(c=>keyOf(c)===k&&c.selectable!==false));updateSelectedClasses();persistSelection();}
function persistSelection(){if(classSelectionStale)return;const selected=[...state.selected];selectionSave=selectionSave.catch(()=>{}).then(()=>api('/selection',{selected}));selectionSave.catch(error=>toast(error.message,true));}
function toggleClass(code){if(classBusy||classSelectionStale)return;const c=state.candidates.find(c=>keyOf(c)===code);if(!c)return;classFocusKey=code;renderClassFocus();classDetail(c);if(c.selectable!==false)changeClassSelection(state.selected.includes(code)?state.selected.filter(k=>k!==code):[...state.selected,code]);}
function updateSelectedClasses(){if(!$('#selected-classes'))return;$('#selected-classes').innerHTML=`<span class="selected-count"><b>${state.selected.length.toString().padStart(2,'0')}</b> 件を検索に使用</span>${state.selected.map(k=>{const c=state.candidates.find(c=>keyOf(c)===k);return `<button class="chip remove-class" data-key="${esc(k)}" title="選択から解除">${esc(c?.kind||'')} ${esc(c?.code||k)} ×</button>`;}).join('')}`;$$('.remove-class').forEach(el=>el.addEventListener('click',()=>changeClassSelection(state.selected.filter(k=>k!==el.dataset.key))));$$('.node, #class-list button[data-code]').forEach(n=>{const yes=state.selected.includes(n.dataset.code);n.classList.toggle('selected',yes);n.classList.toggle('active',yes);n.setAttribute('aria-pressed',String(yes));const mark=$('.selection-mark',n);if(mark)mark.textContent=yes?'✓':n.dataset.selectable==='false'?'◇':'';});if($('#undo-class'))$('#undo-class').disabled=!classUndo.length;renderClassProposalFeedback();}
function renderClassFocus(){const c=classFocus();if(!$('#class-focus'))return;$('#class-focus').innerHTML=c?`<div><span class="eyebrow">探索の起点</span><b>${esc(c.kind)} ${esc(c.code)}</b><span class="focus-title">${esc(classTitle(c))}</span>${classScopeText(c)?`<small>${esc(classScopeText(c))}</small>`:''}</div><div class="focus-actions"><button id="class-broader" class="button small">↗ 広げる</button><button id="class-narrower" class="button small">↘ 狭める</button><button id="replace-class" class="button small subtle" ${c.selectable===false?'disabled':''}>選択をこの分類に置き換える</button></div>`:'<p class="caption">分類をクリックすると、広げる・狭める起点になります。</p>';if(!c)return;on('#class-broader','click',()=>browseClasses({direction:'broader',kind:c.kind,code:c.code}));on('#class-narrower','click',()=>browseClasses({direction:'children',kind:c.kind,code:c.code}));on('#replace-class','click',()=>{changeClassSelection([keyOf(c)]);toast('検索に使う分類を '+c.code+' に置き換えました。「選択を1つ戻す」で戻せます。');});$$('.node').forEach(n=>n.classList.toggle('focused',n.dataset.code===keyOf(c)));}
function classDetail(c){
 const facetNames={device:'装置・構造',material:'材料・組成',process:'製造・プロセス',interface:'界面・接合',performance:'性能・評価',application:'用途・システム'};
 const facets=(c.facets||[]).map(id=>facetNames[id]).filter(Boolean);
 const matches=c.matched_classifications||[];
 $('#class-detail').classList.add('visible');
 $('#class-detail').innerHTML=`<b>${esc(c.kind)} ${esc(c.code)} · ${esc(classTitle(c))}</b> <span class="pill">${c.verified?'コード・定義を辞書で確認済み':'存在・定義は未確認'}</span>${c.evidence_status==='derived_candidate'?'<span class="pill">対応づけによるIPC候補 · 付与IPCとは別</span>':''}${c.evidence_status==='hypothesis'?'<span class="pill">探索仮説・適合性は確認待ち</span>':''}${classNamesHtml(c)}${facets.length?`<p class="class-facet-tags">${facets.map(label=>`<span>${esc(label)}</span>`).join('')}</p>`:''}<p>${esc(c.reason||'公式分類の階層を確認できます。')}</p>${classProposalReasonHtml(c)}${c.parent?`<span class="caption">公式の上位分類：${esc(c.parent)}</span>`:''}${matches.length?`<details class="class-match-details"><summary>推薦の起点になった分類 ${matches.length} 件</summary>${matches.slice(0,12).map(m=>`<p><b>${esc(m.kind)} ${esc(m.code)}</b> · ${esc(classTitle(m))}<br><span class="caption">${esc(m.reason)}</span></p>`).join('')}${matches.length>12?'<p class="caption">代表の12件を表示しています。</p>':''}</details>`:''}${(c.origins||c.classification_origins||[]).map(o=>`<p class="caption">${o.type==='patent'?'ターゲット特許':o.type==='discovery'?'探索ラボ':o.type==='fi_to_ipc'?'FIから得たIPC候補':o.type==='fterm_theme_to_ipc'?'Fタームのテーマ範囲から得たIPC候補':'利用者の推薦'}：${esc(o.patent_id||'')} ${esc(o.label)}${o.raw?`<br>入力原文：${esc(o.raw)}`:''}${o.warning?`<br>${esc(o.warning)}`:''}</p>`).join('')}<a href="${esc(c.source)}" target="_blank" rel="noopener noreferrer">公式分類表を確認 ↗</a>`;
}
function drawClassNodes(){
 hideClassTooltip();
 renderClassTranslationStatus();
 classMotionController=null;
 cancelAnimationFrame(animation);const list=classificationItems(),svg=$('#class-svg'),mode=classLayoutMode();const requestedWidth=Math.max(620,Math.min(2400,Math.round(svg.parentElement.clientWidth)));
 const cacheKey=JSON.stringify([mode,requestedWidth,list.map(c=>[keyOf(c),c.title,c.title_official,c.title_en,c.terms,c.parent,c.ancestors,c.facets,c.group])]);
 if(classLayoutCache?.key!==cacheKey)classLayoutCache={key:cacheKey,value:PatentClassLayout.compute(list,{mode,width:requestedWidth})};
 const {positions,width,height,method,groups=[],links=[]}=classLayoutCache.value;svg.dataset.layout=mode;svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.style.height=`${Math.max(380,height*svg.parentElement.clientWidth/width)}px`;
 let note=$('#class-layout-note');if(!note){note=document.createElement('p');note.id='class-layout-note';note.className='class-layout-note';svg.parentElement.insertAdjacentElement('afterend',note);}note.textContent=mode==='semantic'?`${method}で配置 · まとまりの見出しは技術の目安 / 実線：正式な親子関係 · 点線：名称・技術項目の近さ`:'等間隔の一覧表示 / 実線：表示中の正式な親子関係';
 $('#class-regions').innerHTML=mode==='semantic'?groups.map(group=>{const b=group.bounds;return `<g class="class-region"><rect x="${b.x}" y="${b.y}" width="${b.width}" height="${b.height}" rx="30"/><text x="${b.x+16}" y="${b.y+23}" data-width="${b.width-32}">${esc(PatentClassLabels.group(group.label,classLanguage))}<tspan class="class-region-count"> · ${group.memberIndices.length}</tspan></text></g>`;}).join(''):'';
 $('#class-neighbors').innerHTML=mode==='semantic'?links.map(link=>{const a=positions[link.from],b=positions[link.to];if(!a||!b)return '';return `<line class="class-neighbor" data-from="${link.from}" data-to="${link.to}" x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}"><title>${esc(link.reason||'名称・技術項目が近い候補')}</title></line>`;}).join(''):'';
 $('#class-nodes').innerHTML=list.map((c,i)=>{const p=positions[i],r=36,title=classTitle(c);return `<g class="node ${c.kind==='F-term'?'fterm':c.kind==='CPC'?'cpc':''} ${!c.verified?'unverified':''} ${c.selectable===false?'navigation-node':''}" data-code="${esc(keyOf(c))}" data-selectable="${c.selectable!==false}" data-i="${i}" transform="translate(${p[0]} ${p[1]})" role="button" tabindex="0" aria-label="${esc(c.kind+' '+c.code+' '+title)}" aria-describedby="class-tooltip" aria-pressed="false"><title>${esc(PatentClassLabels.tooltip(c))}</title><circle class="node-circle" r="${r}"/><text class="node-kind" y="-6">${esc(c.kind)}</text><text class="node-code" y="53">${esc(c.code)}</text><text class="node-title" y="74">${esc(title.length>21?title.slice(0,19)+'…':title)}</text><text class="selection-mark" y="17"></text></g>`;}).join('');
 $('#class-links').innerHTML=list.map((c,i)=>{const pi=list.findIndex(p=>p.kind===c.kind&&p.code===c.parent);if(pi<0)return '';return `<line data-from="${pi}" data-to="${i}" x1="${positions[pi][0]}" y1="${positions[pi][1]}" x2="${positions[i][0]}" y2="${positions[i][1]}" stroke="#7898aa40" stroke-width="1"/>`;}).join('');
 $('#class-list').innerHTML=list.map(c=>`<button type="button" data-code="${esc(keyOf(c))}" aria-pressed="false" title="${esc(PatentClassLabels.tooltip(c))}">${esc(c.kind)} ${esc(c.code)}<br>${esc(classTitle(c))}<br><span class="caption">${c.selectable===false?'案内用・選択不可':c.selection_scope==='subtree'?'上位IPC・下位を含む':c.verified?'辞書で確認済み':'未確認候補'} ${(c.origins||[]).length?'· 推薦した分類':''}</span></button>`).join('');
 $$('.node',svg).forEach(n=>{const c=list[Number(n.dataset.i)];n.addEventListener('mouseenter',e=>{classDetail(c);showClassTooltip(c,e,n);});n.addEventListener('mousemove',e=>positionClassTooltip(e,n));n.addEventListener('mouseleave',queueHideClassTooltip);n.addEventListener('focus',e=>{classDetail(c);showClassTooltip(c,e,n);});n.addEventListener('blur',queueHideClassTooltip);n.addEventListener('click',()=>{if(!svg.dataset.dragged)toggleClass(keyOf(c));});n.addEventListener('keydown',e=>{if(e.key==='Escape')hideClassTooltip();if(e.key===' '||e.key==='Enter'){e.preventDefault();toggleClass(keyOf(c));}});});$$('#class-list button').forEach(n=>n.addEventListener('click',()=>toggleClass(n.dataset.code)));
 let lassoBefore;enableLasso(svg,'#class-lasso','.node[data-selectable="true"]',node=>{if(classBusy||classSelectionStale)return;if(!lassoBefore)lassoBefore=[...state.selected];if(!state.selected.includes(node.dataset.code))state.selected.push(node.dataset.code);},()=>{if(lassoBefore){classUndo.push(lassoBefore);lassoBefore=null;}updateSelectedClasses();persistSelection();});
 $$('.node-title, .node-code',svg).forEach(label=>{if(label.getComputedTextLength()>166){label.setAttribute('textLength','166');label.setAttribute('lengthAdjust','spacingAndGlyphs');}});
 $$('.class-region text',svg).forEach(label=>{const width=Number(label.dataset.width);if(label.getComputedTextLength()>width){label.setAttribute('textLength',String(width));label.setAttribute('lengthAdjust','spacingAndGlyphs');}});
 if(mode==='semantic'){
  const nodes=$$('.node',svg),edges=$$('#class-links line, #class-neighbors line',svg);
  const moving=()=>document.contains(svg)&&activeTab==='explore'&&step===0&&document.documentElement.dataset.atlasMotion!=='static'&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const tick=time=>{
   if(!moving())return;
   if(!svg.dataset.drawing&&!svg.matches(':hover')&&!svg.contains(document.activeElement)){
    const floating=positions.map((p,i)=>[p[0]+Math.sin(time/3600+i)*1.2,p[1]+Math.cos(time/3300+i)*1.5]);
    nodes.forEach((n,i)=>n.setAttribute('transform',`translate(${floating[i][0]} ${floating[i][1]})`));
    edges.forEach(line=>{const a=floating[Number(line.dataset.from)],b=floating[Number(line.dataset.to)];line.setAttribute('x1',a[0]);line.setAttribute('y1',a[1]);line.setAttribute('x2',b[0]);line.setAttribute('y2',b[1]);});
   }
   animation=requestAnimationFrame(tick);
  };
  classMotionController=()=>{cancelAnimationFrame(animation);if(moving())animation=requestAnimationFrame(tick);};
  syncClassMotion();
 }
}
