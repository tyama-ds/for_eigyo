/* Prior query examples: preserve Boolean scopes, preview edits, then apply. */
'use strict';
const queryLibraryFormats=[['jplatpat','J-PlatPat'],['portable_json','Patent Atlas JSON'],['derwent_innovation','Derwent Innovation'],['derwent_dii','Derwent Innovations Index'],['espacenet','Espacenet'],['uspto','USPTO'],['patentscope','PATENTSCOPE'],['google_patents','Google Patents'],['reference','その他・不明']];
let queryLibraryDraft={selected:null,text:'',name:'',purpose:'',source_format:'jplatpat',edits:Object.create(null),classEdits:Object.create(null),adaptPurpose:null,adaptGoal:null,suggestion:null,preview:null,previewPayload:null,error:'',busy:false};
const qlEsc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function queryLibraryEntries(workbench){return Array.isArray(workbench?.library)?workbench.library:[];}
function queryLibrarySelected(workbench){const entries=queryLibraryEntries(workbench);return entries.find(e=>e.id===queryLibraryDraft.selected)||entries[0]||null;}
function queryLibraryLabel(format){return queryLibraryFormats.find(([id])=>id===format)?.[1]||String(format||'元のサービス未指定');}
function queryLibraryTreeHtml(node){
 if(!node||typeof node!=='object')return '';
 if(node.op==='text')return `<span class="ql-condition ql-text">${qlEsc(node.value)}</span>`;
 if(node.op==='class')return `<span class="ql-condition ql-class">${qlEsc(node.system)} ${qlEsc(node.value)}</span>`;
 const label={and:'すべて含む · AND',or:'いずれか · OR',not:'左の条件から、右の条件を除く · NOT'}[node.op];
 if(!label||!Array.isArray(node.children))return '';
 return `<div class="ql-tree ql-tree-${qlEsc(node.op)}"><b>${label}</b><div>${node.children.map(queryLibraryTreeHtml).join('')}</div></div>`;
}
function queryLibraryHtml(workbench={}){
 const d=queryLibraryDraft,entries=queryLibraryEntries(workbench),selected=queryLibrarySelected(workbench),isSelected=selected?.id===d.selected;
 const edits=isSelected?d.edits:{},classEdits=isSelected?d.classEdits:{},preview=isSelected?d.preview:null;
 const purpose=isSelected&&d.adaptPurpose!==null?d.adaptPurpose:(workbench.brief?.purpose||'');
 const goal=isSelected&&d.adaptGoal!==null?d.adaptGoal:(workbench.brief?.goal||'');
 const suggestion=isSelected?d.suggestion:null;
 const disabled=d.busy?'disabled':'';
 return `<section id="query-library" class="ql-library" aria-label="過去の検索式から作る">
 <div class="ql-heading"><div><span class="eyebrow">QUERY LIBRARY</span><h2>良い検索式を、次の調査へ。</h2><p class="caption">元の式を残したまま、観点・語句・分類を調整し、プレビューして採用します。</p></div><span class="pill">${entries.length}件の検索式例</span></div>
 <div class="ql-layout"><section class="panel ql-import"><h3>検索式例を取り込む</h3><label>名前<input id="ql-name" maxlength="160" value="${qlEsc(d.name)}" placeholder="例：車両制御の先行技術調査" ${disabled}></label>
 <label>元の検索サービス<select id="ql-source" ${disabled}>${queryLibraryFormats.map(([id,label])=>`<option value="${id}" ${d.source_format===id?'selected':''}>${label}</option>`).join('')}</select></label>
 <label>元の調査目的<textarea id="ql-purpose" rows="2" maxlength="4000" placeholder="どのような調査で使った式ですか" ${disabled}>${qlEsc(d.purpose)}</textarea></label>
 <label>検索式・構造化JSON<textarea id="ql-text" class="ql-source-input" rows="6" maxlength="100000" placeholder="[車両/TX+自動車/TX]*[B60/IP]" ${disabled}>${qlEsc(d.text)}</textarea></label>
 <div class="button-row"><label class="button small ql-file-label" for="ql-file">ファイルから読む<input id="ql-file" type="file" accept=".json,.txt,application/json,text/plain" ${disabled}></label><button id="ql-import" class="button primary small" ${disabled}>例を保存</button></div>
 <details class="ql-format-help"><summary>取り込める形式・JSONの例</summary><p>調整できる式：J-PlatPatの /TX・/IP・/CP・/FT と [ ]、+（OR）、*（AND）、-（AND NOT）。異なる演算子の範囲は角括弧で明示してください。英文の句は引用符で囲みます。</p><p>他サービスの式や未対応の構文も参照用として保存できます。演算子・近接検索・ワイルドカードを単語に読み替えません。</p><pre>${qlEsc(JSON.stringify({schema_version:1,name:'車両制御の例',purpose:'制御方式の調査',source_format:'jplatpat',facets:['用途','制御方式'],query:{keywords:['車両'],include_terms:['制御','運転'],exclude_terms:['玩具'],classifications:[{kind:'IPC',code:'B60'}]}},null,2))}</pre><p>JSONの keywords はAND、include_terms と classifications は各群内OR、exclude_terms は除外です。複雑な構造は boolean_tree で保持します。登録後「JSONを保存」で移行用ファイルを取得できます。</p></details>
 </section><section class="ql-work-area"><div class="ql-example-list" aria-label="保存した検索式例">${entries.length?entries.map(e=>`<button class="ql-example ${e.id===selected?.id?'selected':''}" data-ql-select="${qlEsc(e.id)}" aria-pressed="${e.id===selected?.id?'true':'false'}" ${disabled}><strong>${qlEsc(e.name)}</strong><span>${qlEsc(queryLibraryLabel(e.source_format))}</span><small>${e.status==='editable'?'条件を調整できます':'参照として保存'}</small></button>`).join(''):'<div class="ql-empty"><span>01 → 02 → 03</span><h3>過去の判断を、検索式の出発点に。</h3><p>式を貼り付けるか、JSON・テキストファイルを読み込んでください。</p></div>'}</div>
 ${selected?`<article class="panel ql-selected"><div class="ql-heading"><h3>${qlEsc(selected.name)}</h3>${selected.status==='editable'?`<button id="ql-download" class="button subtle small" ${disabled}>JSONを保存</button>`:''}</div><p class="caption">元の目的：${qlEsc(selected.purpose||'未記録')}</p>${selected.facets?.length?`<p class="caption">元の観点：${selected.facets.map(qlEsc).join(' / ')}</p>`:''}<details class="ql-original"><summary>元の検索式・取込データ</summary><pre>${qlEsc(selected.original_text)}</pre></details>
 ${selected.status==='editable'?`<div class="ql-adapt-header"><h4>今回の調査に合わせて調整</h4><p class="caption">元の分類は引き継ぎます。別の分野へ移す場合は分類も見直してください。空欄にして条件を消す操作はできません。</p></div><label>今回の調査目的<textarea id="ql-adapt-purpose" maxlength="300" rows="2" ${disabled}>${qlEsc(purpose)}</textarea></label>
 <label>適応先の技術説明<textarea id="ql-adapt-goal" maxlength="5000" rows="3" placeholder="例：自動車の経路制御の式を、工場内の搬送ロボットの調査に応用したい" ${disabled}>${qlEsc(goal)}</textarea></label><button id="ql-suggest" class="button small" ${disabled}>LLMで置換候補を起案</button><p class="caption">設定済みLLMを使い、元の例から置換候補を提案して下の入力欄を更新します。提案後も、プレビューと採用は人が行います。手動調整だけでも利用できます。</p>
 <div id="ql-suggestion" class="ql-suggestion" aria-live="polite" ${suggestion?'':'hidden'}>${suggestion?`<h4>置換候補の理由</h4>${suggestion.reasons.length?`<ul>${suggestion.reasons.map(r=>`<li><b>${qlEsc(r.original)} → ${qlEsc(r.replacement)}</b><p>${qlEsc(r.reason)}</p></li>`).join('')}</ul>`:'<p class="caption">変更候補はありません。確認事項を参考に手動で調整してください。</p>'}${suggestion.questions.length?`<h4>採用前に確認</h4><ul>${suggestion.questions.map(q=>`<li>${qlEsc(q)}</li>`).join('')}</ul>`:''}`:''}</div>
 <div class="ql-edit-columns"><div><h4>観点に対応する語句</h4>${(selected.terms||[]).map((term,index)=>`<label class="ql-replacement"><span>${qlEsc(term)}</span><input data-ql-term="${index}" value="${qlEsc(Object.prototype.hasOwnProperty.call(edits,term)?edits[term]:term)}" maxlength="500" aria-label="${qlEsc(term)} の置換先" ${disabled}></label>`).join('')||'<p class="caption">この例に本文語句はありません。</p>'}</div><div><h4>検索対象の分類</h4>${(selected.classification_terms||[]).map((term,index)=>`<label class="ql-replacement"><span>${qlEsc(term)}</span><input data-ql-class="${index}" value="${qlEsc(Object.prototype.hasOwnProperty.call(classEdits,term)?classEdits[term]:term.slice(term.indexOf(':')+1))}" maxlength="50" aria-label="${qlEsc(term)} の置換先コード" ${disabled}></label>`).join('')||'<p class="caption">この例に分類条件はありません。</p>'}</div></div><button id="ql-preview" class="button small" ${disabled}>調整した条件をプレビュー</button>
 <div id="ql-preview-area" class="ql-preview" aria-live="polite">${preview?`<span class="eyebrow">REVIEW BEFORE APPLY</span><h4>この条件で新しい式を作ります</h4>${queryLibraryTreeHtml(preview.query?.boolean_tree)}${preview.expression?`<pre>${qlEsc(preview.expression)}</pre>`:''}${(preview.warnings||[]).length?`<ul class="ql-warnings">${preview.warnings.map(w=>`<li>${qlEsc(w)}</li>`).join('')}</ul>`:''}<button id="ql-apply" class="button primary small" ${disabled}>確認して検索式を作成</button><p class="caption">元の検索式例を保持し、新しい検索式として履歴に追加します。DB・LLM・プロキシの設定は引き継ぎます。</p>`:'<p class="caption">語句と分類の変更後にプレビューすると、AND・OR・NOTの範囲を確認できます。</p>'}</div>`:`<div class="ql-reference-note"><h4>参照用として保存しました</h4><ul>${(selected.issues||[]).map(issue=>`<li>${qlEsc(issue)}</li>`).join('')}</ul><p>元の式を参考に、J-PlatPatの対応構文や構造化JSONで条件を再登録すると調整できます。</p></div>`}</article>`:''}</section></div>
 <p id="ql-error" class="ql-error" role="alert" ${d.error?'':'hidden'}>${qlEsc(d.error)}</p></section>`;
}
function queryLibraryRefresh(){
 const host=document.getElementById('query-library');if(!host)return;
 host.outerHTML=queryLibraryHtml(state.research_workbench||{});bindQueryLibrary();
}
function queryLibraryChoose(id){
 queryLibraryDraft.selected=id;queryLibraryDraft.edits=Object.create(null);queryLibraryDraft.classEdits=Object.create(null);queryLibraryDraft.adaptPurpose=null;queryLibraryDraft.adaptGoal=null;queryLibraryDraft.suggestion=null;queryLibraryDraft.preview=null;queryLibraryDraft.previewPayload=null;queryLibraryDraft.error='';
}
function queryLibraryInvalidate(){
 queryLibraryDraft.preview=null;queryLibraryDraft.previewPayload=null;
 queryLibraryDraft.suggestion=null;const suggestion=document.getElementById('ql-suggestion');if(suggestion){suggestion.innerHTML='';suggestion.hidden=true;}
 const host=document.getElementById('ql-preview-area');if(host)host.innerHTML='<p class="caption">条件が変わりました。もう一度プレビューしてください。</p>';
}
function queryLibraryPayload(entry,workbench){
 const d=queryLibraryDraft;if(!entry||entry.status!=='editable')throw Error('調整できる検索式例を選んでください。');
 const replacements=Object.create(null),class_replacements=Object.create(null);
 for(const term of entry.terms||[]){if(Object.prototype.hasOwnProperty.call(d.edits,term)){const value=d.edits[term].trim();if(!value)throw Error('検索語句は空欄にできません。');if(value!==term)replacements[term]=value;}}
 for(const term of entry.classification_terms||[]){if(Object.prototype.hasOwnProperty.call(d.classEdits,term)){const value=d.classEdits[term].trim();if(!value)throw Error('分類は空欄にできません。');if(value!==term.slice(term.indexOf(':')+1))class_replacements[term]=value;}}
 return {example_id:entry.id,replacements,class_replacements,purpose:d.adaptPurpose??workbench?.brief?.purpose??''};
}
function queryLibraryAcceptSuggestion(entry,result){
 if(!result||!result.replacements||Array.isArray(result.replacements)||typeof result.replacements!=='object'||!result.class_replacements||Array.isArray(result.class_replacements)||typeof result.class_replacements!=='object'||!Array.isArray(result.reasons)||!Array.isArray(result.questions))throw Error('置換候補の形式を確認できません。入力欄は保持しています。');
 for(const [mapping,allowed] of [[result.replacements,entry.terms||[]],[result.class_replacements,entry.classification_terms||[]]])for(const [key,value] of Object.entries(mapping)){if(!allowed.includes(key)||typeof value!=='string'||!value.trim())throw Error('元の検索式にない置換候補です。入力欄は保持しています。');}
 if(result.reasons.some(r=>!r||typeof r.original!=='string'||typeof r.replacement!=='string'||typeof r.reason!=='string')||result.questions.some(q=>typeof q!=='string'))throw Error('置換理由を確認できません。入力欄は保持しています。');
 queryLibraryInvalidate();
 queryLibraryDraft.edits=Object.assign(Object.create(null),result.replacements);queryLibraryDraft.classEdits=Object.assign(Object.create(null),result.class_replacements);queryLibraryDraft.suggestion=result;
}
async function queryLibraryReadFile(file){
 if(!file)return;if(file.size>400000)throw Error('ファイルは400KB以内、本文100,000文字以内にしてください。');
 const text=await file.text();if(text.length>100000)throw Error('本文を100,000文字以内にしてください。');
 queryLibraryDraft.text=text;if(!queryLibraryDraft.name)queryLibraryDraft.name=String(file.name||'').replace(/\.(?:json|txt)$/i,'').slice(0,160);
 if(/\.json$/i.test(file.name||''))queryLibraryDraft.source_format='portable_json';
 queryLibraryDraft.error='';queryLibraryRefresh();
}
function queryLibraryPortable(entry){
 if(entry?.status!=='editable'||!entry.query?.boolean_tree)throw Error('構造化した検索式例を選んでください。');
 return {schema_version:1,name:entry.name,purpose:entry.purpose,facets:entry.facets||[],source_format:entry.source_format,query:{boolean_tree:entry.query.boolean_tree}};
}
function queryLibraryDownload(entry){
 const data=JSON.stringify(queryLibraryPortable(entry),null,2),url=URL.createObjectURL(new Blob([data],{type:'application/json;charset=utf-8'}));
 const a=document.createElement('a');a.href=url;a.download='patent-atlas-query-example.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function queryLibraryAction(action){
 const d=queryLibraryDraft;if(d.busy)return;
 let entry=queryLibrarySelected(state.research_workbench||{});if(entry&&entry.id!==d.selected)queryLibraryChoose(entry.id);
 d.error='';d.busy=true;queryLibraryRefresh();
 try{
  if(action==='import'){
   if(!d.text.trim())throw Error('検索式かJSONを入力してください。');
   const oldIds=new Set(queryLibraryEntries(state.research_workbench).map(e=>e.id));
   const result=await api('/research/library/import',{text:d.text,source_format:d.source_format,name:d.name,purpose:d.purpose});
   state=result.state||result;
   const added=queryLibraryEntries(state.research_workbench).find(e=>!oldIds.has(e.id));if(added)queryLibraryChoose(added.id);
   d.text='';d.name='';d.purpose='';
  }else if(action==='suggest'){
   if(!entry||entry.status!=='editable')throw Error('調整できる検索式例を選んでください。');
   const purpose=String(d.adaptPurpose??state.research_workbench?.brief?.purpose??'').trim(),goal=String(d.adaptGoal??state.research_workbench?.brief?.goal??'').trim();
   if(!purpose||!goal)throw Error('今回の調査目的と、適応先の技術説明を入力してください。');
   const result=await api('/research/library/suggest',{example_id:entry.id,purpose,goal});queryLibraryAcceptSuggestion(entry,result);
  }else if(action==='preview'){
   const payload=queryLibraryPayload(entry,state.research_workbench);
   d.preview=null;d.previewPayload=null;
   const result=await api('/research/library/adapt',payload);
   if(!result.query?.boolean_tree||!result.preview_hash)throw Error('プレビューの条件を確認できません。もう一度実行してください。');
   d.preview=result;d.previewPayload=payload;
  }else if(action==='apply'){
   if(!d.preview?.preview_hash||!d.previewPayload)throw Error('先に条件をプレビューしてください。');
   const current=queryLibraryPayload(entry,state.research_workbench);
   if(JSON.stringify(current)!==JSON.stringify(d.previewPayload)){queryLibraryInvalidate();throw Error('条件が変わりました。もう一度プレビューしてください。');}
   const result=await api('/research/library/apply',{...d.previewPayload,expected_preview_hash:d.preview.preview_hash});
   state=result.state||result;d.preview=null;d.previewPayload=null;
   if(typeof updateChrome==='function')updateChrome();
   if(typeof setTab==='function')setTab('explore');if(typeof setStep==='function')setStep(1);
  }
  if(typeof updateChrome==='function')updateChrome();
 }catch(error){d.error=error.message||'処理に失敗しました。入力内容は保持しています。';throw error;}
 finally{d.busy=false;queryLibraryRefresh();}
}
function bindQueryLibrary(){
 const root=document.getElementById('query-library');if(!root||root.dataset.bound)return;root.dataset.bound='true';
 const entry=queryLibrarySelected(state.research_workbench||{});if(entry&&entry.id!==queryLibraryDraft.selected)queryLibraryChoose(entry.id);
 const listen=(selector,event,handler)=>{root.querySelector(selector)?.addEventListener(event,async e=>{try{await handler(e);}catch(error){queryLibraryDraft.error=error.message;const box=document.getElementById('ql-error');if(box){box.textContent=error.message;box.hidden=false;}}});};
 for(const [selector,key] of [['#ql-name','name'],['#ql-purpose','purpose'],['#ql-text','text'],['#ql-source','source_format']])listen(selector,selector==='#ql-source'?'change':'input',e=>{queryLibraryDraft[key]=e.target.value;});
 listen('#ql-adapt-purpose','input',e=>{queryLibraryDraft.adaptPurpose=e.target.value;queryLibraryInvalidate();});
 listen('#ql-adapt-goal','input',e=>{queryLibraryDraft.adaptGoal=e.target.value;queryLibraryInvalidate();});
 root.querySelectorAll('[data-ql-select]').forEach(button=>button.addEventListener('click',()=>{if(queryLibraryDraft.busy)return;queryLibraryChoose(button.dataset.qlSelect);queryLibraryRefresh();}));
 root.querySelectorAll('[data-ql-term]').forEach(input=>input.addEventListener('input',()=>{queryLibraryDraft.edits[entry.terms[Number(input.dataset.qlTerm)]]=input.value;queryLibraryInvalidate();}));
 root.querySelectorAll('[data-ql-class]').forEach(input=>input.addEventListener('input',()=>{queryLibraryDraft.classEdits[entry.classification_terms[Number(input.dataset.qlClass)]]=input.value;queryLibraryInvalidate();}));
 listen('#ql-file','change',e=>queryLibraryReadFile(e.target.files?.[0]));
 for(const action of ['import','suggest','preview','apply'])listen('#ql-'+action,'click',()=>queryLibraryAction(action));
 listen('#ql-download','click',()=>queryLibraryDownload(entry));
}
