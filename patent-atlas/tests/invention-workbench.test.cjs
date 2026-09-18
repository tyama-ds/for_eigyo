const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/invention-workbench.js','utf8');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function setup(){
 const dom={},groups={},handlers=new Map(),calls=[],navigation=[];
 const brief={entry_mode:'target',purpose:'先行技術を探す',goal:'冷却を制御する装置',keywords:'冷却',target_ids:['JP1'],user_aspects:[]};
 const input={invention_text:'センサと制御部を備える装置。',manual_elements:['温度センサ','冷却制御部']};
 const state={settings:{provider:'local',model:'test'},patents:[],queries:[],research_workbench:{revision:3,brief:structuredClone(brief),invention:{input:structuredClone(input),plan:null}}};
 const plan={id:'inv1',brief:structuredClone(brief),input:{...structuredClone(input),purpose:brief.purpose,goal:brief.goal,keywords:brief.keywords},method:'llm',summary:'センサと制御部の案',elements:[
  {id:'e1',name:'温度センサ',description:'温度を取得する。',relation:'検出値を制御部へ送る',evidence:[{source_id:'input',quote:'センサと制御部を備える装置。'}],perspectives:[{id:'e1p1',name:'温度検出',terms:['温度検出','温度センサ'],classification_keys:['IPC:G01K'],reason:'温度を測る手段'}]},
  {id:'e2',name:'冷却制御部',description:'取得温度に応じて冷却する。',relation:'センサの測定結果を使う',evidence:[],perspectives:[{id:'e2p1',name:'冷却手段',terms:['冷却'],classification_keys:[],reason:'冷却の機構'},{id:'e2p2',name:'制御方式',terms:['フィードバック'],classification_keys:[],reason:'制御の働き'}]}
 ],classification_candidates:[{key:'IPC:G01K',kind:'IPC',code:'G01K',title:'温度測定',selectable:true,verified:true,origins:[{type:'patent'}]}],questions:['要素間の関係を確認してください。']};
 state.research_workbench.invention.plan=plan;
 let briefDraft=structuredClone(brief);
 const context={state,structuredClone,Set,Map,esc,researchBrief:()=>briefDraft,captureResearchDraft:()=>{},updateChrome:()=>{},toast:()=>{},queryLibraryTreeHtml:tree=>`<div>tree:${esc(JSON.stringify(tree))}</div>`,
  $:(selector,root)=>root?.children?.[selector]||dom[selector]||null,$$:(selector,root)=>root?.groups?.[selector]||groups[selector]||[],on:(selector,event,handler)=>handlers.set(`${selector}:${event}`,handler),
  setTab:value=>navigation.push(['tab',value]),setStep:value=>navigation.push(['step',value]),api:async(path,body)=>{calls.push({path,body});return state;},busy:async(_button,_message,fn)=>fn()};
 vm.createContext(context);vm.runInContext(source,context);
 return {context,dom,groups,handlers,calls,navigation,plan,input,read:expression=>vm.runInContext(expression,context)};
}
function plain(value){return JSON.parse(JSON.stringify(value));}

test('default is one element, classification hypotheses are inactive, and saved plan is immutable',()=>{
 const s=setup(),c=s.context,before=JSON.stringify(c.state);const d=c.inventionPlanState();
 assert.equal(d.strategy,'element');assert.deepEqual([...d.element_ids],['e1']);assert.equal(d.elements[0].perspectives[0].class_mode,'text');assert.equal(d.elements[0].perspectives[0].classification_keys.length,0);
 assert.deepEqual(plain(d.elements.map(e=>e.perspectives.map(p=>p.selected))),[[true],[true,false]]);
 const html=c.inventionWorkbenchHtml();assert.match(html,/LLMで発明を要素分解/);assert.match(html,/要素の記載と検索上の役割を確認/);assert.match(html,/仮説 · 対応する原文/);assert.match(html,/取得していない公報本文・請求項は参照しません/);assert.equal(JSON.stringify(c.state),before);
});
test('each selected element serializes edited perspectives and explicit nested joins',()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.strategy='combination';d.element_ids=['e1','e2'];d.perspective_joins.e2='and';d.elements[0].perspectives[0].terms=['熱検出'];d.elements[0].perspectives[0].class_mode='or';d.elements[0].perspectives[0].classification_keys=['IPC:G01K'];
 d.elements[1].perspectives[1].selected=true;
 const p=c.inventionPayload();assert.equal(p.revision,3);assert.equal(p.plan_id,'inv1');assert.equal(p.strategy,'combination');assert.deepEqual(plain(p.perspective_joins),{e1:'or',e2:'and'});assert.deepEqual([...p.elements[0].perspectives[0].terms],['熱検出']);assert.deepEqual([...p.elements[0].perspectives[0].classification_keys],['IPC:G01K']);
 assert.deepEqual(plain(p.elements[1].perspectives.map(p=>p.id)),['e2p1','e2p2']);
 d.strategy='alternatives';d.element_ids=['e2'];assert.deepEqual(plain(c.inventionPayload().elements.map(e=>e.id)),['e2']);
});
test('global and element NOT remain separately scoped and require explicit confirmation',()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.exclusions.all=['玩具'];d.exclusions.e1=['赤外線'];d.exclusions.e2=['蒸発'];
 assert.throws(()=>c.inventionPayload(),/NOT.*確認/);d.confirm_exclusions=true;
 const p=c.inventionPayload();assert.deepEqual(plain(p.exclusions),[{scope:'all',terms:['玩具']},{scope:'e1',terms:['赤外線']}]);assert.equal(p.confirm_exclusions,true);
 d.element_ids=['e2'];assert.deepEqual(plain(c.inventionPayload().exclusions),[{scope:'all',terms:['玩具']},{scope:'e2',terms:['蒸発']}]);assert.deepEqual([...d.exclusions.e1],['赤外線']);
});
test('inactive classifications do not alter text search and class intersection consent is explicit',()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState(),p=d.elements[0].perspectives[0];p.classification_keys=['IPC:G01K'];
 assert.deepEqual([...c.inventionPayload().elements[0].perspectives[0].classification_keys],[]);assert.equal(c.inventionPayload().confirm_class_intersection,false);
 p.class_mode='and';d.confirm_class_intersection=true;assert.deepEqual([...c.inventionPayload().elements[0].perspectives[0].classification_keys],['IPC:G01K']);assert.equal(c.inventionPayload().confirm_class_intersection,true);
});
test('changed brief, claims, manual elements or targets require a new plan',()=>{
 for(const mutate of [c=>c.researchBrief().purpose='別の目的',c=>c.researchBrief().goal='別技術',c=>c.researchBrief().keywords='電池',c=>c.researchBrief().target_ids.push('JP2'),c=>c.researchBrief().user_aspects.push('価格'),c=>c.inventionDraftInput().invention_text+='追記',c=>c.inventionDraftInput().manual_elements.push('新要素')]){
  const s=setup(),c=s.context;c.inventionPlanState();assert.equal(c.inventionPlanStale(),false);mutate(c);assert.equal(c.inventionPlanStale(),true);assert.throws(()=>c.inventionPayload(),/要素分解をやり直し/);
 }
});
test('stale plan visibly disables preview while keeping edited terms on rerender and import',()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.elements[0].perspectives[0].terms=['編集した語'];c.inventionDraftInput().invention_text='編集済みの請求項';
 c.state=structuredClone(c.state);c.state.research_workbench.revision++;const html=c.inventionWorkbenchHtml();assert.match(html,/<button id="invention-preview"[^>]*disabled/);assert.match(html,/編集した語/);assert.match(html,/編集済みの請求項/);assert.equal(c.inventionPlanState(),d);
});
test('all source text, IDs, classifications and query warnings are escaped',()=>{
 const s=setup(),c=s.context;s.plan.elements[0].name='<img src=x>';s.plan.elements[0].relation='<svg>';s.plan.elements[0].evidence=[{source_id:'<source>',quote:'</blockquote><script>'}];s.plan.elements[0].perspectives[0].name='<perspective>';s.plan.classification_candidates[0].title='<class>';s.plan.questions=['<question>'];c.inventionDraftInput().invention_text='<claims>';
 const d=c.inventionPlanState();d.preview={query:{boolean_tree:{op:'text',value:'<term>'}},expression:'<expression>',warnings:['<warning>']};const html=c.inventionWorkbenchHtml();
 for(const text of ['img src=x','svg','source','script','perspective','class','question','claims','expression','warning']){assert.doesNotMatch(html,new RegExp(`<${text}>`));assert.match(html,new RegExp(`&lt;${text}&gt;`));}
});
test('UI capture reads terms, selection, classification modes, joins and each exclusion scope',()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();s.dom['#invention-elements']={};s.dom['#invention-text']={value:'センサと制御部を備える装置。'};s.dom['#invention-manual-elements']={value:'温度センサ\n冷却制御部'};s.dom['#invention-strategy']={value:'alternatives'};s.dom['#invention-confirm-exclusions']={checked:true};s.dom['#invention-confirm-classes']={checked:true};
 s.groups['[data-invention-use]:checked']=[{value:'e1'},{value:'e2'}];s.groups['[data-invention-perspective]']=[{dataset:{inventionElement:'e1',inventionPerspective:'e1p1'},children:{'[data-invention-terms]':{value:'検出\n 温度計 '},'[data-invention-class-mode]':{value:'class'}},groups:{'[data-invention-class]:checked':[{value:'IPC:G01K'}]}}];s.groups['[data-invention-join]']=[{dataset:{inventionJoin:'e2'},value:'and'}];s.groups['[data-invention-exclude]']=[{dataset:{inventionExclude:'all'},value:'玩具'},{dataset:{inventionExclude:'e1'},value:'赤外線'}];
 c.captureInventionDraft();assert.equal(d.strategy,'alternatives');assert.deepEqual([...d.element_ids],['e1','e2']);assert.deepEqual([...d.elements[0].perspectives[0].terms],['検出','温度計']);assert.equal(d.elements[0].perspectives[0].class_mode,'class');assert.deepEqual([...d.elements[0].perspectives[0].classification_keys],['IPC:G01K']);assert.equal(d.perspective_joins.e2,'and');assert.deepEqual([...d.exclusions.all],['玩具']);assert.deepEqual([...d.exclusions.e1],['赤外線']);assert.equal(d.confirm_exclusions,true);
});
test('preview responses cannot restore stale edited conditions, even during an in-flight request',async()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();let resolve;c.api=()=>new Promise(yes=>resolve=yes);const pending=c.previewInvention();d.elements[0].perspectives[0].terms=['変更後'];c.invalidateInventionPreview();resolve({query:{boolean_tree:{}},expression:'古い式',preview_hash:'old'});await pending;assert.equal(d.preview,null);
});
test('preview failures retain input and previous successful preview without applying anything',async()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.preview={preview_hash:'previous'};const oldState=JSON.stringify(c.state);c.api=async()=>{throw new Error('接続できません');};
 await assert.rejects(c.previewInvention(),/接続できません/);assert.equal(d.preview.preview_hash,'previous');assert.deepEqual([...c.inventionDraftInput().manual_elements],['温度センサ','冷却制御部']);assert.equal(JSON.stringify(c.state),oldState);assert.equal(s.navigation.length,0);
});
test('planning preserves edits typed while waiting and does not discard a prior draft on failure',async()=>{
 const s=setup(),c=s.context;c.inventionPlanState();let resolve;c.api=()=>new Promise(yes=>resolve=yes);const pending=c.requestInventionPlan(true);c.inventionDraftInput().invention_text='待ち時間に追記した説明';c.researchBrief().goal='追記した目標';const result=structuredClone(c.state);result.research_workbench.invention.plan.id='new';resolve(result);await pending;
 assert.equal(c.inventionDraftInput().invention_text,'待ち時間に追記した説明');assert.equal(c.researchBrief().goal,'追記した目標');assert.equal(c.inventionPlanStale(),true);
 const d=c.inventionPlanState();d.elements[0].perspectives[0].terms=['保持する編集'];c.api=async()=>{throw new Error('LLMが失敗');};await assert.rejects(c.requestInventionPlan(true),/LLMが失敗/);assert.deepEqual([...c.inventionPlanState().elements[0].perspectives[0].terms],['保持する編集']);assert.equal(s.read('inventionPlanning'),false);
});
test('manual planning validates bounded explicit elements and sends selected research context',async()=>{
 const s=setup(),c=s.context;c.inventionDraftInput().manual_elements=[];await assert.rejects(c.requestInventionPlan(false),/1行ずつ/);c.inventionDraftInput().manual_elements=Array(9).fill('要素');await assert.rejects(c.requestInventionPlan(false),/8件/);c.inventionDraftInput().manual_elements=['x'.repeat(601)];await assert.rejects(c.requestInventionPlan(false),/600文字/);
 c.inventionDraftInput().manual_elements=['温度センサ','冷却制御部'];await c.requestInventionPlan(false);assert.equal(s.calls.length,1);assert.equal(s.calls[0].path,'/research/invention/plan');assert.equal(s.calls[0].body.use_llm,false);assert.deepEqual([...s.calls[0].body.brief.target_ids],['JP1']);
});
test('apply uses exact preview hash, blocks changed payloads, and navigates only after success',async()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();c.api=async(path,body)=>{s.calls.push({path,body});return path.endsWith('/preview')?{query:{boolean_tree:{op:'text',value:'温度'}},expression:'温度',preview_hash:'h1'}:c.state;};
 await c.previewInvention();d.elements[0].perspectives[0].terms.push('追記');await assert.rejects(c.applyInvention(),/もう一度プレビュー/);assert.equal(s.calls.length,1);d.elements[0].perspectives[0].terms.pop();await c.applyInvention();assert.equal(s.calls[1].path,'/research/invention/apply');assert.equal(s.calls[1].body.expected_preview_hash,'h1');assert.deepEqual(s.navigation,[['tab','explore'],['step',1]]);
});

test('only adopted perspectives enter the payload and deselected edits survive rerender',()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.element_ids=['e2'];const extra=d.elements[1].perspectives[1];extra.terms=['保持する追加語'];extra.class_mode='or';extra.classification_keys=['IPC:G01K'];
 assert.deepEqual(plain(c.inventionPayload().elements[0].perspectives.map(p=>p.id)),['e2p1']);
 extra.selected=true;assert.deepEqual(plain(c.inventionPayload().elements[0].perspectives.map(p=>p.id)),['e2p1','e2p2']);
 extra.selected=false;const html=c.inventionWorkbenchHtml();assert.match(html,/保持する追加語/);assert.deepEqual([...extra.classification_keys],['IPC:G01K']);assert.equal(extra.class_mode,'or');assert.equal(c.inventionPlanState(),d);
 const labels=[...html.matchAll(/<input type="checkbox" data-invention-perspective-use[^>]*>/g)].map(match=>match[0]);assert.equal(labels.length,3);assert.match(labels[0],/checked/);assert.match(labels[1],/checked/);assert.doesNotMatch(labels[2],/checked/);
});

test('a selected element with no adopted perspective reports its name without sending a request',async()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.element_ids=['e2'];d.elements[1].perspectives.forEach(p=>p.selected=false);
 await assert.rejects(c.previewInvention(),/「冷却制御部」.*検索観点を1つ以上/);assert.equal(s.calls.length,0);
 d.element_ids=['e1'];assert.doesNotThrow(()=>c.inventionPayload());
});

test('classification AND confirmation ignores unadopted perspectives and unselected elements',()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.element_ids=['e2'];const extra=d.elements[1].perspectives[1];extra.class_mode='and';extra.classification_keys=['IPC:G01K'];
 assert.doesNotThrow(()=>c.inventionPayload());extra.selected=true;assert.throws(()=>c.inventionPayload(),/採用した観点.*分類.*確認/);
 d.confirm_class_intersection=true;assert.equal(c.inventionPayload().elements[0].perspectives.length,2);
 d.confirm_class_intersection=false;d.element_ids=['e1'];assert.doesNotThrow(()=>c.inventionPayload());
});

test('classification intersection across perspectives or elements counts only adopted active classes',()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.element_ids=['e2'];d.perspective_joins.e2='and';
 for(const p of d.elements[1].perspectives){p.class_mode='class';p.classification_keys=['IPC:G01K'];}
 assert.doesNotThrow(()=>c.inventionPayload());d.elements[1].perspectives[1].selected=true;assert.throws(()=>c.inventionPayload(),/分類.*確認/);
 d.elements[1].perspectives[1].selected=false;d.strategy='combination';d.element_ids=['e1','e2'];d.elements[0].perspectives[0].classification_keys=['IPC:G01K'];assert.doesNotThrow(()=>c.inventionPayload());
 d.elements[0].perspectives[0].class_mode='class';assert.throws(()=>c.inventionPayload(),/分類.*確認/);d.strategy='alternatives';assert.doesNotThrow(()=>c.inventionPayload());
});

test('adoption checkbox captures selection, retains edited terms, and invalidates a preview',()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.element_ids=['e2'];d.preview={preview_hash:'old'};d.previewPayload='old';let handler;
 const checkbox={checked:true,addEventListener:(event,fn)=>{if(event==='input')handler=fn;},matches:selector=>selector==='[data-invention-perspective-use]'};
 s.dom['#invention-elements']={};s.dom['#invention-query-preview']={innerHTML:'old'};s.groups['[data-invention-use]:checked']=[{value:'e2'}];s.groups['#invention-workbench input,#invention-workbench textarea,#invention-workbench select']=[checkbox];
 s.groups['[data-invention-perspective]']=[{dataset:{inventionElement:'e2',inventionPerspective:'e2p2'},children:{'[data-invention-perspective-use]':checkbox,'[data-invention-terms]':{value:'追加して編集した語'},'[data-invention-class-mode]':{value:'text'}}}];
 c.bindInventionWorkbench();handler();const p=d.elements[1].perspectives[1];assert.equal(p.selected,true);assert.equal(d.preview,null);assert.equal(d.previewPayload,null);assert.equal(s.dom['#invention-query-preview'].innerHTML,'');assert.equal(c.inventionPayload().elements[0].perspectives.length,2);
 checkbox.checked=false;handler();assert.equal(p.selected,false);assert.deepEqual([...p.terms],['追加して編集した語']);assert.equal(c.inventionPayload().elements[0].perspectives.length,1);
});

test('changing adopted perspectives while preview is pending cannot restore the earlier preview',async()=>{
 const s=setup(),c=s.context,d=c.inventionPlanState();d.element_ids=['e2'];let resolve;c.api=()=>new Promise(yes=>resolve=yes);
 const pending=c.previewInvention();d.elements[1].perspectives[1].selected=true;c.invalidateInventionPreview();resolve({query:{boolean_tree:{}},expression:'元の観点',preview_hash:'old'});await pending;assert.equal(d.preview,null);
});

test('boundary-ellipsis evidence adjustment is disclosed only for the exact status and stays escaped',()=>{
 const s=setup(),c=s.context,e=s.plan.elements[0];e.evidence=[{source_id:'invention_text',quote:'<原文>',quote_adjustment:'boundary_ellipsis_removed',original_quote:'…<original>…'},{source_id:'research_goal',quote:'<別原文>',quote_adjustment:'unchanged'}];
 const html=c.inventionWorkbenchHtml();assert.equal((html.match(/前後の省略記号を除き、原文と照合/g)||[]).length,1);assert.match(html,/貼り付けた発明の原文/);assert.match(html,/調べたい技術・解決したいこと/);assert.match(html,/&lt;原文&gt;/);assert.match(html,/&lt;別原文&gt;/);assert.doesNotMatch(html,/<原文>|<別原文>|<original>/);
});
