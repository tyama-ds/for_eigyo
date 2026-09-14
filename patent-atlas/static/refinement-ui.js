/* Explicit, reviewable IPC changes derived from the imported patent population. */
let refinementRequest=0,refinementLoaded=false,refinementClassPicked=new Set(),refinementNeedsRefresh=false;
function refinementClassRows(feedback, candidates){
 const rows=new Map((feedback.candidates||[]).map(c=>[keyOf(c),{...c}]));
 for(const key of feedback.active_keys||[]){
  if(!rows.has(key)){const c=candidates.find(c=>keyOf(c)===key);if(c)rows.set(key,{...c});}
 }
 return [...rows.values()];
}
function refinementInitialKeys(feedback){return [...new Set([...(feedback.active_keys||[]),...(feedback.recommended_keys||[])])];}
function refinementReplacementKeys(feedback){return [...new Set([...(feedback.active_keys||[]).filter(key=>!key.startsWith('IPC:')),...(feedback.recommended_keys||[])])];}
function refinementIpcHtml(feedback){
 const rows=refinementClassRows(feedback,state.candidates),active=new Set(feedback.active_keys||[]);
 return `<p class="caption">${esc(feedback.note||'')}</p><div class="button-row"><button id="refinement-recommend-ipc" class="button small" ${(feedback.recommended_keys||[]).length?'':'disabled'}>推奨IPCで置き換える</button><button id="refinement-restore-ipc" class="button small subtle">現在の選択に戻す</button></div><p class="caption">現在の分類と推奨IPCを選択済みです。置き換えは既存IPCを推奨IPCに入れ替え、CPC・F-termを残します。上位分類を残して子をORで加えても狭まりません。</p><div class="refinement-ipc-options">${rows.map(c=>{
 const key=keyOf(c),title=c.title_ja||c.title_en||c.title||c.code;
 const hasEvidence=Array.isArray(c.evidence);
 return `<article class="refinement-ipc-card ${c.recommended?'recommended':''}"><label class="check"><input type="checkbox" name="refinement-class" value="${esc(key)}" ${refinementClassPicked.has(key)?'checked':''}><span><b>${esc(c.kind)} ${esc(c.code)}</b> ${c.recommended?'<span class="pill">推奨</span>':''}${active.has(key)?'<span class="pill">現在の選択</span>':''}<small>${esc(title)}</small></span></label>${hasEvidence?`<div class="refinement-ipc-counts"><span>必要 ${Number(c.keep_count||0)} / 不要 ${Number(c.exclude_count||0)} 公報</span><span>予測：必要寄り ${Number(c.predicted_keep_count||0)} / 不要寄り ${Number(c.predicted_exclude_count||0)}</span><span>ファミリー等 ${Number(c.family_count||0)}</span></div><details><summary>根拠の特許と選び方</summary><p>${esc(c.reason)}</p>${[['必要',c.keep_ids],['不要',c.exclude_ids],['必要寄りの予測',c.predicted_keep_ids],['不要寄りの予測',c.predicted_exclude_ids]].filter(([,ids])=>ids?.length).map(([label,ids])=>`<p><b>${label}</b>：${ids.slice(0,30).map(esc).join('、')}${ids.length>30?` ほか${ids.length-30}件`:''}</p>`).join('')}</details>`:'<p class="caption">現在の条件から引き継ぎ。今回の判定群による支持は未確認です。</p>'}</article>`;
 }).join('')||'<p class="caption">判定・関連度とIPCがそろった特許を読み込むと、分類候補を表示します。</p>'}</div><div id="refinement-ipc-change" class="refinement-ipc-change" role="status"></div>${feedback.warnings?.length?`<details><summary>分類の取り込み・集計について ${feedback.warnings.length} 件</summary><ul>${feedback.warnings.map(message=>`<li>${esc(message)}</li>`).join('')}</ul></details>`:''}`;
}
function updateRefinementIpcChange(){
 const el=$('#refinement-ipc-change');if(!el)return;
 const active=refinement.classifications?.active_keys||[],chosen=[...refinementClassPicked];
 const added=chosen.filter(k=>!active.includes(k)),removed=active.filter(k=>!refinementClassPicked.has(k));
 el.textContent=`次の式：分類 ${chosen.length} 件（追加 ${added.length} / 解除 ${removed.length}）。`+
  (added.length?' 分類はORなので、追加により範囲が広がる場合があります。':'')+
  (removed.length?' 解除した分類は次の式に含めません。':'');
}
function paintRefinementIpc(){
 const host=$('#refinement-ipc-options');if(!host)return;
 host.innerHTML=refinementIpcHtml(refinement.classifications||{});
 $$('[name="refinement-class"]').forEach(el=>el.addEventListener('change',()=>{if(el.checked)refinementClassPicked.add(el.value);else refinementClassPicked.delete(el.value);updateRefinementIpcChange();}));
 on('#refinement-recommend-ipc','click',()=>{refinementClassPicked=new Set(refinementReplacementKeys(refinement.classifications));paintRefinementIpc();});
 on('#refinement-restore-ipc','click',()=>{refinementClassPicked=new Set(refinement.classifications.active_keys||[]);paintRefinementIpc();});
 updateRefinementIpcChange();
}
function renderRefinementWorkspace(){
 if(refinementNeedsRefresh){
  refinementNeedsRefresh=false;const serial=++refinementRequest;
  $('#step-content').innerHTML='<div class="panel"><p>保存済みの検索条件を確認しています…</p></div>';
  api('/state').then(fresh=>{if(serial!==refinementRequest||step!==3||activeTab!=='explore'){refinementNeedsRefresh=true;return;}state=fresh;renderRefinementWorkspace();}).catch(error=>{refinementNeedsRefresh=true;if(serial===refinementRequest&&step===3&&activeTab==='explore')toast(error.message,true);});return;
 }
 const q=state.queries.at(-1),c=counts(),serial=++refinementRequest;
 refinementLoaded=false;
 $('#step-content').innerHTML=`<div class="query-layout"><div class="panel" id="refinement-controls"><span class="eyebrow">REFINE YOUR SEARCH</span><h2>残したい技術に、近づける。</h2><p class="muted">必要 ${c.keep} 件・不要 ${c.exclude} 件と学習後の関連度から、特徴語・IPCを次の式に反映します。</p><div class="notice">追加語は検索範囲を狭めます。除外語は既知の必要特許に出現しない語を候補にします。IPCは必要・不要の両方の支持を比較し、分類のNOT条件は作りません。</div><h3 style="margin-top:24px">必要な技術の特徴語</h3><div id="include-terms" class="term-options"><p class="caption">候補を読み込んでいます…</p></div><h3>不要な技術の除外候補</h3><div id="exclude-terms" class="term-options exclude"></div><section class="refinement-ipc"><h3>判定した特許から、IPCを見直す</h3><div id="refinement-ipc-options"><p class="caption">公報の分類と判定を照合しています…</p></div></section><button class="button primary wide" id="refine-query" disabled>この特徴語・分類で次の式を提案 →</button><p class="caption">前回の特徴語は選択済みで引き継ぎます。IPCの追加・解除と、その根拠の特許は検索履歴に保存します。</p>${state.training?trainingHtml():''}</div><div class="panel">${queryCard(q)}<div class="button-row" style="margin-top:20px"><button class="button primary" id="next-csv">次のCSVを読み込む →</button></div></div></div>${historyHtml()}`;
 bindQuery(q);bindHistory();on('#next-csv','click',()=>setStep(1));
 api('/refinement').then(result=>{
  if(serial!==refinementRequest||step!==3||activeTab!=='explore')return;
  refinement=result;refinementClassPicked=new Set(refinementInitialKeys(result.classifications||{}));
  for(const [kind,empty] of [['include','必要・不要の両方を判定すると候補が現れます。'],['exclude','現在、除外候補はありません。']]){
   $('#'+kind+'-terms').innerHTML=result[kind].map(t=>`<label class="check"><input type="checkbox" name="${kind}-term" value="${esc(t)}" ${result['active_'+kind]?.includes(t)?'checked':''}>${esc(t)}</label>`).join('')||`<p class="caption">${empty}</p>`;
  }
  paintRefinementIpc();refinementLoaded=true;$('#refine-query').disabled=state.job.status==='running';
 }).catch(error=>{if(serial===refinementRequest&&step===3&&activeTab==='explore'&&$('#refinement-ipc-options')){$('#refinement-ipc-options').innerHTML=`<p class="learning-error">${esc(error.message)} ページを切り替えて再読込してください。</p>`;toast(error.message,true);}});
 on('#refine-query','click',async e=>{
  if(!refinementLoaded||state.job.status==='running')return;
  const body={refine:true,include:$$('[name="include-term"]:checked').map(el=>el.value),exclude:$$('[name="exclude-term"]:checked').map(el=>el.value),classification_keys:[...refinementClassPicked]};
  await busy(e.currentTarget,'改善案を作成中…',async()=>{
   $('#refinement-controls').inert=true;
   try{const fresh=await api('/query',body);if(serial!==refinementRequest||step!==3||activeTab!=='explore'){refinementNeedsRefresh=true;return;}state=fresh;viewedQueryId=null;renderStep();toast('判定・学習から見直した分類と特徴語を検索履歴に保存しました。');}
   finally{if(serial===refinementRequest&&$('#refinement-controls'))$('#refinement-controls').inert=false;}
  });
 });
}
function classificationChangesHtml(changes){
 if(!changes)return '';
 return `<details class="classification-feedback-history"><summary>判定・学習から反映したIPCと根拠</summary><p class="caption">追加：${(changes.added_keys||[]).map(esc).join('、')||'なし'}<br>解除：${(changes.removed_keys||[]).map(esc).join('、')||'なし'}</p>${(changes.evidence||[]).map(c=>`<p class="caption"><b>${esc(c.kind)} ${esc(c.code)}</b> · 必要 ${Number(c.keep_count||0)} / 不要 ${Number(c.exclude_count||0)} 公報<br>${esc(c.reason)}<br>必要の根拠：${(c.keep_ids||[]).slice(0,30).map(esc).join('、')||'なし'}<br>不要の根拠：${(c.exclude_ids||[]).slice(0,30).map(esc).join('、')||'なし'}</p>`).join('')}</details>`;
}
