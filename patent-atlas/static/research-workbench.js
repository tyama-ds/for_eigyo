'use strict';
/* Four entry paths use one existing workspace and preserve connection/output settings. */
let researchDraft=null, researchPlanDraft=null, researchImportPending=false, researchPlanning=false;
const researchEntries=[
 ['target','01','ターゲットから作る','特許の分類と技術要素を出発点に、周辺へ広げる。'],
 ['discover','02','ターゲットを探す','漠然とした説明を観点に分け、最初の特許を見つける。'],
 ['examples','03','過去の式から作る','検索式例の論理構造を保ち、今回の技術へ調整する。'],
 ['tools','04','個別の機能を使う','分類探索・マップ・要否判定などを直接開く。']
];
function researchBrief(){return researchDraft||(researchDraft=structuredClone(state.research_workbench?.brief||{entry_mode:'target',purpose:'',goal:'',keywords:state.keywords||'',user_aspects:[],target_ids:[]}));}
function captureResearchDraft(){
 const brief=researchBrief();
 for(const [field,id] of [['purpose','research-purpose'],['goal','research-goal'],['keywords','research-keywords']])if($('#'+id))brief[field]=$('#'+id).value;
 if($('#research-target-options'))brief.target_ids=$$('.research-target input:checked').map(el=>el.value);
 captureResearchPlan();
}
function captureResearchPlan(){
 if(!$('#research-concepts')||!researchPlanDraft)return;
 researchPlanDraft.concepts=$$('.research-concept').map(el=>{const previous=researchPlanDraft.concepts.find(c=>c.id===el.dataset.concept);return {...previous,id:el.dataset.concept,selected:$('input[data-use]',el).checked,role:$('select[data-role]',el).value,or_group:$('select[data-or-group]',el)?.value||previous?.or_group||'',terms:$('textarea',el).value.split(/\r?\n/).map(t=>t.trim()).filter(Boolean)};});
 researchPlanDraft.classification_keys=$$('.research-class input:checked').map(el=>el.value);
 researchPlanDraft.acknowledge_unmapped=$('#research-unmapped-ack')?.checked||false;
}
function researchPlanState(plan){
 if(!plan)return null;
 if(researchPlanDraft?.id!==plan.id)researchPlanDraft={id:plan.id,concepts:plan.concepts.map(c=>({...c,selected:c.role==='required',role:c.role==='exclude'?'exclude':'required',or_group:c.or_group||researchOwnGroup(c,plan),terms:[...c.terms]})),classification_keys:[],preview:null};
 return researchPlanDraft;
}
function researchOwnGroup(concept,plan){const reserved=new Set(plan.concepts.map(c=>c.or_group).filter(Boolean));let key=`ui_${plan.concepts.indexOf(concept)+1}`;while(reserved.has(key))key+='_s';return key;}
function researchConceptGroupLocked(concept,plan){
 if(concept.locked_and||concept.role==='exclude')return true;
 // Older saved plans predate locked_and. Preserve a user's explicit must-have aspect.
 return (plan.brief?.user_aspects||state.research_workbench?.brief?.user_aspects||[]).some(aspect=>/^(必須|required)\s*[:：]/i.test(aspect)&&(aspect===concept.name||aspect.replace(/^(必須|required)\s*[:：]\s*/i,'')===concept.name));
}
function researchConceptGroup(concept,draft,plan){return researchConceptGroupLocked(concept,plan)?`locked:${concept.id}`:(draft.or_group||researchOwnGroup(concept,plan));}
function researchGroupOptions(plan,draft,current){
 const source=plan.concepts.find(c=>c.id===current.id),own=researchOwnGroup(source,plan);
 if(researchConceptGroupLocked(source,plan)||current.role==='exclude')return `<option value="${esc(current.or_group||own)}">${current.role==='exclude'?'除外は独立した条件':source.role==='exclude'?'独立条件 · グループの結合なし':'明示した必須条件 · 独立'}</option>`;
 const choices=new Map();
 for(const c of plan.concepts){const d=draft.concepts.find(x=>x.id===c.id);if(d.role==='exclude'||researchConceptGroupLocked(c,plan))continue;const key=researchConceptGroup(c,d,plan);if(!choices.has(key))choices.set(key,[]);choices.get(key).push(c.name);}
 const labels=new Map([...choices].map(([key,names])=>[key,`条件 · ${names.join(' / ')}`]));
 const separate=(!choices.has(own)?own:plan.concepts.map(c=>researchOwnGroup(c,plan)).find(key=>!choices.has(key)));
 if(separate&&choices.get(current.or_group)?.length>1)labels.set(separate,'この観点だけの独立条件にする');
 return [...labels].map(([key,label])=>`<option value="${esc(key)}" ${current.or_group===key?'selected':''}>${esc(label)}</option>`).join('');
}
function researchLogicHtml(plan,draft){
 const groups=new Map(),excluded=[];
 for(const c of plan.concepts){const d=draft.concepts.find(x=>x.id===c.id);if(!d.selected)continue;if(d.role==='exclude'){excluded.push(d);continue;}const key=researchConceptGroup(c,d,plan);if(!groups.has(key))groups.set(key,[]);groups.get(key).push({...d,name:c.name});}
 const termsHtml=c=>`<span class="research-logic-terms">${c.terms.length?c.terms.map(t=>`<span>${esc(t)}</span>`).join('<b class="research-logic-or">OR</b>'):'<i>語句を入力</i>'}</span>`;
 const groupHtml=[...groups.values()].map((members,index)=>`<div class="research-logic-set"><small>条件${index+1} · ${members.length>1?'この中のどれか':'語句のいずれか'}</small>${members.map(c=>`<div class="research-logic-concept"><span class="research-logic-name">${esc(c.name)}</span>${termsHtml(c)}</div>`).join('<b class="research-logic-union">OR · 和集合</b>')}</div>`).join('<b class="research-logic-intersection">AND<br><small>積集合</small></b>');
 return `<div class="research-logic-heading"><b>現在の語句の組合せ</b><span>同じ条件に入れるとOR、別の条件にするとAND</span></div><div class="research-logic-equation">${groupHtml||'<p class="caption">含める観点を選ぶと、組合せを確認できます。</p>'}</div>${excluded.length?`<div class="research-logic-exclude"><b>NOT · 除外</b>${excluded.map(termsHtml).join('<b class="research-logic-or">OR</b>')}</div>`:''}${draft.classification_keys.length?'<p class="caption">選択した分類の和集合を、この語句の式にANDで加えます。</p>':''}`;
}
function refreshResearchLogic(){
 const plan=state.research_workbench?.plan,draft=researchPlanDraft;if(!plan||!draft)return;
 const host=$('#research-logic');if(host)host.innerHTML=researchLogicHtml(plan,draft);
 for(const element of $$('.research-concept')){const current=draft.concepts.find(c=>c.id===element.dataset.concept),source=plan.concepts.find(c=>c.id===element.dataset.concept),select=$('select[data-or-group]',element);if(select){select.innerHTML=researchGroupOptions(plan,draft,current);select.disabled=researchConceptGroupLocked(source,plan)||current.role==='exclude';}const help=$('.research-group-help',element);if(help)help.textContent='同じ条件を選んだ観点同士はOR、別の条件とはAND。'+(researchConceptGroupLocked(source,plan)&&source.role!=='exclude'?'明示した必須観点は独立条件を保持します。':source.group_reason?' 起案時のまとめ方：'+source.group_reason:'');}
}
function researchTargetOptions(){
 const ids=new Set(researchBrief().target_ids),references=new Map((state.research_workbench?.target_patents||[]).map(p=>[p.id,p]));for(const p of state.patents){const old=references.get(p.id)||{},merged={...p};for(const field of ['title','abstract','ipc','fi','fterm','cpc'])if(!merged[field]||(Array.isArray(merged[field])&&!merged[field].length))merged[field]=old[field]||merged[field];references.set(p.id,merged);}const rows=[...references.values()].sort((a,b)=>Number(ids.has(b.id))-Number(ids.has(a.id)));
 return rows.map(p=>`<label class="research-target" data-search="${esc((p.id+' '+p.title).toLocaleLowerCase())}"><input type="checkbox" value="${esc(p.id)}" ${ids.has(p.id)?'checked':''}><span><b>${esc(p.title)}</b><small>${esc(p.id)} · IPC ${esc(p.ipc||'なし')} ${p.fi?' · FI '+esc(p.fi):''}${p.fterm?' · Fターム '+esc(p.fterm):''}${p.cpc?' · CPC '+esc(p.cpc):''}</small></span></label>`).join('')||'<p class="caption">CSVを追加するか、ターゲット特許の情報を入力してください。</p>';
}
function researchPlanHtml(plan){
 if(!plan)return '<div class="research-plan-empty"><span>◎</span><h3>目的から、検索の観点へ。</h3><p>条件を整理すると、観点・語句・根拠となる分類をここで比較できます。</p></div>';
 const draft=researchPlanState(plan),selected=new Set(draft.classification_keys);
 return `<div class="panel-heading"><div><span class="eyebrow">CONCEPTS → QUERY</span><h2>観点を組み立てる</h2></div><span class="pill">${plan.method==='llm'?'LLMによる案':'入力の整理'}</span></div><p class="muted">${esc(plan.summary)}</p><p class="caption">採用する観点にチェック。代替表現・近い技術は同じ条件にまとめてOR（和集合）、併せて満たす別の観点はAND（積集合）にします。一般語・専門語だけで区別せず、意味に合わせて調整してください。除外は明示的に選んだ観点だけです。</p>
 <div id="research-logic" class="research-logic" aria-live="polite">${researchLogicHtml(plan,draft)}</div>
 <div id="research-concepts">${plan.concepts.map(c=>{const d=draft.concepts.find(x=>x.id===c.id),locked=researchConceptGroupLocked(c,plan)||d.role==='exclude';return `<article class="research-concept" data-concept="${esc(c.id)}"><div class="panel-heading"><label class="check"><input data-use type="checkbox" ${d.selected?'checked':''}><b>${esc(c.name)}</b></label><select data-role aria-label="${esc(c.name)} の条件"><option value="required" ${d.role==='required'?'selected':''}>含める</option><option value="exclude" ${d.role==='exclude'?'selected':''}>除外 · NOT</option></select></div><label class="research-group-choice">和集合グループ<select data-or-group aria-label="${esc(c.name)} の和集合グループ" ${locked?'disabled':''}>${researchGroupOptions(plan,draft,d)}</select></label><p class="caption research-group-help">同じ条件を選んだ観点同士はOR、別の条件とはAND。${researchConceptGroupLocked(c,plan)&&c.role!=='exclude'?'明示した必須観点は独立条件を保持します。':esc(c.group_reason||'')}</p><label>語句 · 1行に1語句（この欄の語はOR）<textarea rows="3" maxlength="5000">${esc(d.terms.join('\n'))}</textarea></label><p class="caption">${esc(c.reason)}${c.evidence_ids?.length?' / 根拠：'+c.evidence_ids.map(esc).join('、'):''}</p>${c.abstract_terms?.length?`<div class="research-expansion"><span>機能・概念を広げる</span>${c.abstract_terms.map(t=>`<button class="button small" data-abstract="${esc(t)}" type="button">＋ ${esc(t)}</button>`).join('')}</div>`:''}</article>`;}).join('')}</div>
 ${plan.unmapped_keywords?.length?`<div class="notice">観点に未割当の入力語：${plan.unmapped_keywords.map(esc).join('、')}。上の語句欄に割り当ててください。<label class="check"><input id="research-unmapped-ack" type="checkbox" ${draft.acknowledge_unmapped?'checked':''}>未割当の語を今回の式から省略することを確認した</label></div>`:''}
 <details class="research-classifications" open><summary>分類の根拠を比較する · ${plan.classification_candidates.length} 件</summary><p class="caption">実付与・対応付け・上位への拡張・観点からの仮説を区別しています。分類は任意です。選択すると分類群のORを、観点の式にANDで加えます。</p><div class="research-class-grid">${plan.classification_candidates.map(c=>{const tag=researchClassificationProvenance(c),hypothesis=tag.includes('仮説'),origins=c.origins||[];return `<label class="research-class ${hypothesis?'hypothesis':''}"><input type="checkbox" value="${esc(c.key)}" ${selected.has(c.key)?'checked':''} ${c.selectable?'':'disabled'}><span><small>${tag} · ${c.verified?'辞書で確認':'未確認'}</small><b>${esc(c.kind)} ${esc(c.code)}</b><span>${esc(c.title_short_ja||c.title_ja||c.title)}</span><small>${esc(c.reason||'')}${origins.map(o=>o.patent_id?' / '+esc(o.patent_id):'').join('')}</small></span></label>`;}).join('')}</div></details>
 ${[...(plan.questions||[]),...(plan.classification_notes||[])].length?`<details class="research-notes"><summary>確認する点・根拠の限界</summary><ul>${[...(plan.questions||[]),...(plan.classification_notes||[])].map(t=>`<li>${esc(t)}</li>`).join('')}</ul></details>`:''}
 <div class="button-row"><button class="button primary" id="research-preview">検索条件をプレビュー →</button></div><div id="research-query-preview" aria-live="polite">${researchPreviewHtml()}</div>`;
}
function researchPreviewHtml(){const p=researchPlanDraft?.preview;return p?`<div class="research-preview"><h3>今回の検索条件</h3>${queryLibraryTreeHtml(p.query.boolean_tree)}<pre class="expression">${esc(p.expression)}</pre><p class="caption">採用後、保存済みの検索サービス形式で出力します。分類が未確認の場合は出力画面で確認できます。</p><button class="button primary" id="research-apply">この条件で検索式を作成 →</button></div>`:'';}
function invalidateResearchPreview(){if(researchPlanDraft)researchPlanDraft.preview=null;if($('#research-query-preview'))$('#research-query-preview').innerHTML='';const notice=$('#research-stale-plan');if(notice)notice.hidden=!(state.research_workbench?.plan&&researchBriefChanged());}
function researchPlanPayload(){captureResearchDraft();if(researchBriefChanged())throw new Error('入力条件が観点案の作成後に変わっています。観点をもう一度起案してください。');captureResearchPlan();const plan=state.research_workbench.plan;return {revision:state.research_workbench.revision,plan_id:researchPlanDraft.id,concepts:researchPlanDraft.concepts.filter(c=>c.selected).map(({id,role,terms,or_group})=>({id,role,terms,or_group:role==='exclude'||researchConceptGroupLocked(plan.concepts.find(c=>c.id===id),plan)?'':or_group})),classification_keys:researchPlanDraft.classification_keys,acknowledge_unmapped:researchPlanDraft.acknowledge_unmapped};}
function researchToolsHtml(){const tools=[['explore',0,'分類を探索','キーワード・分類コンステレーション'],['explore',1,'検索式とCSV','DB別の出力・検索結果の取込'],['explore',2,'特許マップ・要否判定','PCA / t-SNE / UMAP、学習と境界の確認'],['explore',3,'検索式を改善','必要・不要の語句と分類を反映'],['discovery',0,'探索ラボ','分類を拡張・CSVまたは設定済みOPSで探索'],['agent',0,'判断エージェント','指定した基準で特許の要否を判定'],['orchestration',0,'統括・収束を見る','反復の進行と収束履歴'],['settings',0,'表示・接続設定','既存のLLM・プロキシ・表示設定']];return `<div class="research-tools">${tools.map(([tab,s,title,description])=>`<button class="panel research-tool" data-open-tab="${tab}" data-open-step="${s}"><span>↗</span><h3>${title}</h3><p>${description}</p></button>`).join('')}</div>`;}
function renderResearchWorkbench(){
 const host=$('#research-workbench');if(!host)return;const wb=state.research_workbench||{},brief=researchBrief(),mode=brief.entry_mode;
 host.innerHTML=`<div class="research-heading"><div><span class="eyebrow">RESEARCH DESK</span><h2>今回の調査は、どこから始めますか。</h2></div><span class="pill">${state.patents.length} 件の特許 / ${state.queries.length} 件の検索式</span></div><nav class="research-entries" aria-label="調査の開始方法">${researchEntries.map(([id,n,title,description])=>`<button class="research-entry ${mode===id?'active':''}" data-research-mode="${id}" aria-pressed="${mode===id}"><small>${n}</small><b>${title}</b><span>${description}</span></button>`).join('')}</nav>
 <div class="research-journey"><span>目的・観点</span><b>→</b><span>検索式</span><b>→</b><span>検索・CSV</span><b>→</b><span>マップ・要否</span><b>→</b><span>改善・採用</span></div>
 ${mode==='tools'?researchToolsHtml():mode==='examples'?queryLibraryHtml(wb):`<div class="research-layout"><div class="panel research-brief"><span class="eyebrow">${mode==='target'?'SEED PATENTS':'DISCOVER SEEDS'}</span><h2>${mode==='target'?'根拠のある特許から。':'技術の話から始める。'}</h2>${mode==='discover'?'<div class="notice">おすすめは併用方式。広いKWで実在特許を探し、仮のIPCも比較します。最初から分類で絞りすぎず、見つけた特許をターゲットにして深めます。</div>':''}<label>今回の検索目的 <span class="required">必須</span><input id="research-purpose" maxlength="300" value="${esc(brief.purpose)}" list="research-purpose-options" placeholder="例：周辺技術の把握、先行技術調査"><datalist id="research-purpose-options"><option value="技術動向・周辺技術の把握"><option value="新規性・進歩性の検討に向けた先行技術調査"><option value="競合の技術領域を把握する"><option value="特定の構成・機能に関連する特許を集める"></datalist></label><label>調べたい技術・解決したいこと<textarea id="research-goal" rows="4" maxlength="5000" placeholder="構成、働き、課題、対象範囲など、分かる範囲で説明してください。">${esc(brief.goal)}</textarea></label><label>手掛かりのキーワード<input id="research-keywords" maxlength="2000" value="${esc(brief.keywords)}" placeholder='例：全固体電池 界面抵抗'></label><p class="caption">語句は空白区切り。英語の句は二重引用符で囲みます。</p><button class="button subtle" id="research-aspects-open">想定している観点を入力 ${brief.user_aspects.length?'· '+brief.user_aspects.length+' 件':'（任意）'} ↗</button>
 <details class="research-targets" ${mode==='target'?'open':''}><summary>ターゲット特許を指定 · ${brief.target_ids.length} 件</summary><p class="caption">名称・要約と、取り込んだIPC / FI / Fターム / CPCを参照します。最大20件。FIのIPC部・Fタームの対応候補は根拠を区別します。</p><div class="button-row"><button class="button small" id="research-upload">CSVを追加</button><button class="button small" id="research-target-add">特許情報を入力</button></div><input id="research-csv-file" type="file" accept=".csv,.tsv" hidden><input id="research-target-filter" type="search" placeholder="番号・名称で候補を絞る" aria-label="ターゲット候補を検索"><div id="research-target-options">${researchTargetOptions()}</div>${mode==='discover'?'<button class="button small" id="research-promote">選んだ特許から作る →</button>':''}</details>
 <div class="research-plan-actions"><button class="button primary" id="research-plan-llm">LLMで観点・広げ方を起案 ↗</button><button class="button" id="research-plan-local">入力をそのまま整理</button><p class="caption">LLMには目的・観点と、選択した特許の名称・要約・分類を送ります。保存済みの接続設定を使います。「入力を整理」は通信しません。</p></div><div id="research-error" role="alert"></div></div><div class="panel research-plan"><div id="research-stale-plan" class="notice" role="status" ${wb.plan&&researchBriefChanged()?'':'hidden'}>表示している内容は前回の案です。入力条件が変わったため、観点をもう一度起案してからプレビュー・採用してください。</div>${researchPlanHtml(wb.plan)}</div></div>`}`;
 $$('.research-brief input,.research-brief textarea').forEach(el=>el.addEventListener('input',()=>{captureResearchDraft();invalidateResearchPreview();}));
 $$('[data-research-mode]').forEach(el=>el.addEventListener('click',()=>{captureResearchDraft();if(el.dataset.researchMode==='discover'&&researchDraft.entry_mode!=='discover')researchDraft.target_ids=[];researchDraft.entry_mode=el.dataset.researchMode;invalidateResearchPreview();renderResearchWorkbench();}));
 $$('[data-open-tab]').forEach(el=>el.addEventListener('click',()=>{setTab(el.dataset.openTab);if(el.dataset.openTab==='explore')setStep(Number(el.dataset.openStep));}));
 if(mode==='examples'){bindQueryLibrary();return;}if(mode==='tools')return;
 on('#research-aspects-open','click',()=>{captureResearchDraft();$('#research-aspects-text').value=researchDraft.user_aspects.join('\n');$('#research-aspects-dialog').showModal();});
 on('#research-target-filter','input',e=>{$$('.research-target').forEach(el=>el.hidden=!el.dataset.search.includes(e.target.value.toLocaleLowerCase()));});
 on('#research-upload','click',()=>$('#research-csv-file').click());
 on('#research-csv-file','change',async e=>{captureResearchDraft();researchImportPending=true;pendingImportMode='merge';try{await uploadFile(e.target.files[0]);}catch(error){researchImportPending=false;throw error;}});
 on('#research-target-add','click',()=>{captureResearchDraft();$('#research-target-dialog').showModal();});
 on('#research-promote','click',()=>{captureResearchDraft();if(!researchDraft.target_ids.length)throw new Error('ターゲット特許を選んでください。');researchDraft.entry_mode='target';invalidateResearchPreview();renderResearchWorkbench();});
 for(const [id,llm] of [['research-plan-llm',true],['research-plan-local',false]])on('#'+id,'click',e=>busy(e.currentTarget,'観点を整理しています…',()=>requestResearchPlan(llm)));
 $$('[data-abstract]').forEach(el=>el.addEventListener('click',()=>{const field=$('textarea',el.closest('.research-concept'));const values=field.value.split(/\r?\n/);if(!values.includes(el.dataset.abstract))field.value+=(field.value?'\n':'')+el.dataset.abstract;captureResearchPlan();invalidateResearchPreview();refreshResearchLogic();}));
 $$('.research-plan input,.research-plan textarea,.research-plan select').forEach(el=>el.addEventListener('input',()=>{captureResearchPlan();invalidateResearchPreview();refreshResearchLogic();}));
 on('#research-preview','click',e=>busy(e.currentTarget,'確認中…',async()=>{researchPlanDraft.preview=await api('/research/preview',researchPlanPayload());$('#research-query-preview').innerHTML=researchPreviewHtml();bindResearchApply();}));
 bindResearchApply();refreshResearchLogic();
}
function bindResearchApply(){on('#research-apply','click',e=>busy(e.currentTarget,'作成中…',async()=>{const preview=researchPlanDraft.preview;const body=researchPlanPayload();state=await api('/research/apply',{...body,expected_preview_hash:preview?.preview_hash});researchDraft=structuredClone(state.research_workbench.brief);updateChrome();setTab('explore');setStep(1);toast('検索条件を履歴に保存しました。検索サービスの形式で出力します。');}));}
function initResearchWorkbench(){
 on('#research-aspects-close','click',()=>$('#research-aspects-dialog').close());
 on('#research-aspects-form','submit',e=>{e.preventDefault();const values=$('#research-aspects-text').value.split(/\r?\n/).map(t=>t.trim()).filter(Boolean);if(values.length>8||values.some(t=>t.length>160))throw new Error('観点は8件まで、1件160文字以内です。');researchBrief().user_aspects=values;invalidateResearchPreview();$('#research-aspects-dialog').close();renderResearchWorkbench();});
 on('#research-target-close','click',()=>$('#research-target-dialog').close());
 on('#research-target-form','submit',async e=>{e.preventDefault();captureResearchDraft();const values=Object.fromEntries(new FormData(e.currentTarget));values.target_ids=[...researchBrief().target_ids];await busy($('#research-target-save'),'追加中…',async()=>{state=await api('/research/targets/add',values);researchBrief().target_ids=[...new Set([...values.target_ids,state.research_workbench.last_added_target_id||values.id])];invalidateResearchPreview();$('#research-target-dialog').close();e.target.reset();renderResearchWorkbench();updateChrome();});});
 renderResearchWorkbench();
}

function researchClassificationProvenance(c){
 const types=new Set((c.origins||[]).map(o=>o.type));
 const tags=[];
 if(types.has('patent'))tags.push('公報の分類欄');
 if(types.has('fi_to_ipc'))tags.push('FIからのIPC候補');
 if(types.has('fterm_theme_to_ipc'))tags.push('Fターム対応範囲');
 if(types.has('target_parent'))tags.push('上位へ拡張');
 return tags.join(' / ')||(c.llm_proposed?'LLMからの仮説':'観点からの仮説');
}
function researchBriefChanged(){
 const plan=state.research_workbench?.plan;
 if(!plan)return true;
 const brief=researchBrief(),saved=plan.brief||state.research_workbench.brief;
 return ['purpose','goal','keywords','entry_mode'].some(key=>String(brief[key]||'').trim()!==String(saved[key]||'').trim())||['target_ids','user_aspects'].some(key=>JSON.stringify([...(brief[key]||[])].sort())!==JSON.stringify([...(saved[key]||[])].sort()));
}

async function requestResearchPlan(useLlm){
 if(researchPlanning)throw new Error('観点を起案中です。完了を待ってください。');
 captureResearchDraft();const submitted=structuredClone(researchBrief());researchPlanning=true;
 if($('#research-error'))$('#research-error').textContent='';
 try{
  const result=await api('/research/plan',{revision:state.research_workbench.revision,brief:submitted,use_llm:useLlm});
  captureResearchDraft();const edited=JSON.stringify(researchBrief())!==JSON.stringify(submitted);
  state=result;if(!edited)researchDraft=structuredClone(state.research_workbench.brief);
  researchPlanDraft=null;
  if(activeTab==='workbench')renderResearchWorkbench();updateChrome();
  if(edited)toast('起案中の入力変更は保持しています。新しい条件で観点を再作成してください。');
 }catch(error){if($('#research-error'))$('#research-error').textContent=error.message;throw error;}
 finally{researchPlanning=false;}
}
