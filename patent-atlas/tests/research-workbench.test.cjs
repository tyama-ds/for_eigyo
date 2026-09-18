const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/research-workbench.js','utf8');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function setup(){
 const dom={'#research-workbench':{innerHTML:''}},groups={},handlers=new Map(),calls=[],navigation=[];
 const state={patents:[],queries:[],keywords:'既存KW',settings:{provider:'local',base_url:'http://127.0.0.1:1234/v1',model:'qwen',classification_layout:'semantic'},research_workbench:{revision:7,brief:{entry_mode:'target',purpose:'周辺調査',goal:'走行支援',keywords:'自動運転',user_aspects:[],target_ids:[]},plan:null,library:[],adoptions:[]}};
 const context={state,activeTab:'workbench',esc,structuredClone,Set,Map,FormData,queryLibraryTreeHtml:()=>'<span>論理木</span>',queryLibraryHtml:()=>'<div>式例取込</div>',bindQueryLibrary:()=>{},updateChrome:()=>{},toast:()=>{},
  $:(selector,root)=>root?.children?.[selector]||dom[selector]||null,$$:selector=>groups[selector]||[],on:(selector,event,handler)=>handlers.set(`${selector}:${event}`,handler),
  setTab:value=>navigation.push(['tab',value]),setStep:value=>navigation.push(['step',value]),api:async(path,body)=>{calls.push({path,body});return state;},busy:async(_button,_message,fn)=>fn(),uploadFile:async()=>{},pendingImportMode:'replace'};
 vm.createContext(context);vm.runInContext(source,context);
 return{context,dom,groups,handlers,calls,navigation,read:expression=>vm.runInContext(expression,context)};
}
function plan(classes=[]){return{id:'p1',method:'llm',summary:'候補案',purpose:'周辺調査',concepts:[{id:'a',name:'移動',role:'required',terms:['走行','移動'],abstract_terms:['誘導'],reason:'入力に基づく',evidence_ids:[]},{id:'b',name:'除外項目',role:'exclude',terms:['充電'],abstract_terms:[],reason:'任意の除外候補',evidence_ids:[]}],classification_candidates:classes,classification_notes:[],questions:[]};}
function classification(origins){return{key:'IPC:B60',kind:'IPC',code:'B60',title:'車両',selectable:true,verified:true,origins,reason:''};}

test('four starts are explicit, with seed discovery leading into seed-based work',()=>{
 const s=setup(),c=s.context;c.renderResearchWorkbench();let html=s.dom['#research-workbench'].innerHTML;
 for(const mode of ['target','discover','examples','tools'])assert.match(html,new RegExp(`data-research-mode="${mode}"`));
 assert.match(html,/今回の検索目的/);assert.match(html,/SEED PATENTS/);
 c.researchBrief().entry_mode='discover';c.renderResearchWorkbench();html=s.dom['#research-workbench'].innerHTML;
 assert.match(html,/広いKWで実在特許を探し/);assert.match(html,/仮のIPC/);assert.match(html,/research-promote/);
});
test('examples and individual tools have separate entry views and leave saved provider settings unchanged',()=>{
 const s=setup(),c=s.context,before=JSON.stringify(c.state.settings);
 c.researchBrief().entry_mode='examples';c.renderResearchWorkbench();assert.match(s.dom['#research-workbench'].innerHTML,/式例取込/);assert.doesNotMatch(s.dom['#research-workbench'].innerHTML,/id="research-purpose"/);
 c.researchBrief().entry_mode='tools';c.renderResearchWorkbench();const html=s.dom['#research-workbench'].innerHTML;
 for(const name of ['分類を探索','特許マップ・要否判定','統括・収束を見る','表示・接続設定'])assert.match(html,new RegExp(name));
 assert.equal(JSON.stringify(c.state.settings),before);assert.equal(c.state.keywords,'既存KW');
});
test('target rendering escapes IDs, document text, and all imported classifications',()=>{
 const s=setup(),c=s.context;c.state.patents=[{id:'<img>',title:'<script>名称',ipc:'<ipc>',fi:'<fi>',fterm:'<fterm>',cpc:'<cpc>'}];c.researchBrief().target_ids=['<img>'];
 const html=c.researchTargetOptions();for(const name of ['img','script','ipc','fi','fterm','cpc'])assert.doesNotMatch(html,new RegExp(`<${name}>`));assert.match(html,/checked/);assert.match(html,/&lt;fi&gt;/);
});
test('capturing the brief preserves selected IDs, hidden filtered selections, and optional aspects',()=>{
 const s=setup(),c=s.context;s.dom['#research-purpose']={value:'新しい目的'};s.dom['#research-goal']={value:'新しい説明'};s.dom['#research-keywords']={value:'新KW'};s.dom['#research-target-options']={};s.groups['.research-target input:checked']=[{value:'a'},{value:'b',hidden:true}];
 c.researchBrief().user_aspects=['機能'];c.captureResearchDraft();const d=c.researchBrief();assert.equal(d.purpose,'新しい目的');assert.deepEqual([...d.target_ids],['a','b']);assert.deepEqual([...d.user_aspects],['機能']);assert.equal(c.state.research_workbench.brief.purpose,'周辺調査');
});
test('unselected optional/exclude concepts never silently become NOT conditions',()=>{
 const s=setup(),c=s.context,p=plan();p.concepts.push({id:'optional',name:'任意項目',role:'optional',terms:['速度'],abstract_terms:[],reason:'候補',evidence_ids:[]});const before=JSON.stringify(p),draft=c.researchPlanState(p);
 assert.equal(draft.concepts[0].selected,true);assert.equal(draft.concepts[1].selected,false);assert.equal(draft.concepts[1].role,'exclude');assert.equal(draft.concepts[2].selected,false);assert.equal(JSON.stringify(p),before);
});
test('editing a concept persists on rerender of its plan but a new plan clears the old draft',()=>{
 const s=setup(),c=s.context,p=plan();const first=c.researchPlanState(p);first.concepts[0].terms=['編集済み'];first.classification_keys=['IPC:B60'];assert.equal(c.researchPlanState(p).concepts[0].terms[0],'編集済み');
 const fresh=c.researchPlanState({...p,id:'p2'});assert.equal(fresh.concepts[0].terms[0],'走行');assert.equal(fresh.classification_keys.length,0);
});
test('payload sends only selected concepts and preserves their explicit roles and edited terms',()=>{
 const s=setup(),c=s.context,p=plan();c.state.research_workbench.plan=p;c.researchPlanState(p);const draft=s.read('researchPlanDraft');draft.concepts[1].selected=true;draft.classification_keys=['IPC:B60'];
 const payload=c.researchPlanPayload();assert.equal(payload.revision,7);assert.equal(payload.plan_id,'p1');assert.equal(payload.concepts[1].role,'exclude');assert.deepEqual([...payload.classification_keys],['IPC:B60']);
});
test('preview is invalidated before edited conditions can be adopted',()=>{
 const s=setup(),c=s.context;c.researchPlanState(plan());s.read("researchPlanDraft.preview={preview_hash:'old',query:{boolean_tree:{}},expression:'old'}");s.dom['#research-query-preview']={innerHTML:'old'};c.invalidateResearchPreview();assert.equal(s.read('researchPlanDraft.preview'),null);assert.equal(s.dom['#research-query-preview'].innerHTML,'');
});
test('optional aspect dialog rejects excess or long items and commits valid input only',()=>{
 const s=setup(),c=s.context;let closed=false;s.dom['#research-aspects-text']={value:'a\nb'};s.dom['#research-aspects-dialog']={close:()=>closed=true};c.initResearchWorkbench();const submit=s.handlers.get('#research-aspects-form:submit');
 submit({preventDefault:()=>{}});assert.equal(closed,true);assert.deepEqual([...c.researchBrief().user_aspects],['a','b']);
 s.dom['#research-aspects-text'].value=Array.from({length:9},(_,i)=>'aspect'+i).join('\n');assert.throws(()=>submit({preventDefault:()=>{}}),/8件/);assert.deepEqual([...c.researchBrief().user_aspects],['a','b']);
 s.dom['#research-aspects-text'].value='a'.repeat(161);assert.throws(()=>submit({preventDefault:()=>{}}),/160/);
});
test('promoting discovery to target mode requires a selected real patent',()=>{
 const s=setup(),c=s.context;c.researchBrief().entry_mode='discover';c.renderResearchWorkbench();const promote=s.handlers.get('#research-promote:click');assert.throws(()=>promote(),/ターゲット特許/);
 c.state.patents=[{id:'JP1',title:'車両'}];c.researchBrief().target_ids=['JP1'];promote();assert.equal(c.researchBrief().entry_mode,'target');assert.equal(c.researchBrief().target_ids[0],'JP1');
});
test('classification provenance escapes imported publication identifiers',()=>{
 const s=setup(),c=s.context,html=c.researchPlanHtml(plan([classification([{type:'patent',patent_id:'<img src=x onerror=alert(1)>'}])]));
 assert.doesNotMatch(html,/<img src=x/);assert.match(html,/&lt;img/);
});
test('catalog-derived concept hypotheses are not represented as patent evidence',()=>{
 const s=setup(),c=s.context,html=c.researchPlanHtml(plan([classification([{type:'catalog'},{type:'concept_hypothesis'}])]));
 assert.doesNotMatch(html,/特許からの根拠/);assert.match(html,/仮説/);
});
test('FI and F-term correspondences are distinguished from directly assigned IPC',()=>{
 for(const [type,pattern] of [['fi_to_ipc',/FI[^<]*(対応|由来|抽出|候補)/],['fterm_theme_to_ipc',/F.?ターム[^<]*(対応|由来|候補)/]]){
  const s=setup(),c=s.context,html=c.researchPlanHtml(plan([classification([{type,patent_id:'JP1'}])]));assert.match(html,pattern);
 }
});
test('changed purpose, goals, keywords, target selection or mode cannot silently reuse the old plan',()=>{
 for(const [field,value] of [['purpose','別の目的'],['goal','別の技術'],['keywords','電池'],['target_ids',['JP2']],['user_aspects',['新観点']],['entry_mode','discover']]){
  const s=setup(),c=s.context,p=plan();c.state.research_workbench.plan=p;c.researchPlanState(p);c.researchBrief()[field]=value;
  assert.throws(()=>c.researchPlanPayload(),/観点|計画|条件|起案/,field);
 }
});
test('an LLM response cannot discard brief edits entered while it was waiting',async()=>{
 const s=setup(),c=s.context;c.renderResearchWorkbench();s.dom['#research-error']={textContent:''};s.dom['#research-goal']={value:'応答待ち前の説明'};
 let resolve;const pending=new Promise(yes=>resolve=yes);c.api=()=>pending;
 const request=s.handlers.get('#research-plan-llm:click')({currentTarget:{}});
 s.dom['#research-goal'].value='利用者の追加入力';const fresh=structuredClone(c.state);fresh.research_workbench.brief.goal='応答待ち前の説明';fresh.research_workbench.plan=plan();
 resolve(fresh);await request;assert.equal(c.researchBrief().goal,'利用者の追加入力');
});
test('late planning error remains its original error after changing to another entry view',async()=>{
 const s=setup(),c=s.context;c.renderResearchWorkbench();s.dom['#research-error']={textContent:''};let reject;const pending=new Promise((_yes,no)=>reject=no);c.api=()=>pending;
 const request=s.handlers.get('#research-plan-llm:click')({currentTarget:{}});delete s.dom['#research-error'];c.researchBrief().entry_mode='tools';reject(new Error('LLM接続失敗'));
 await assert.rejects(request,/LLM接続失敗/);assert.equal(c.researchBrief().entry_mode,'tools');
});
test('adding a target does not reselect a saved target the user just unchecked',async()=>{
 const s=setup(),c=s.context;c.state.research_workbench.brief.target_ids=['JP-old'];c.state.patents=[{id:'JP-old',title:'前の特許'}];c.researchBrief().target_ids=[];
 c.FormData=function(){return new Map([['id','JP-new'],['title','新しい特許']]);};s.dom['#research-target-dialog']={close:()=>{}};
 const fresh=structuredClone(c.state);fresh.research_workbench.brief.target_ids=['JP-old','JP-new'];fresh.patents.push({id:'JP-new',title:'新しい特許'});c.api=async()=>fresh;c.initResearchWorkbench();
 await s.handlers.get('#research-target-form:submit')({preventDefault:()=>{},currentTarget:{},target:{reset:()=>{}}});
 assert.deepEqual([...c.researchBrief().target_ids],['JP-new']);
});
test('saved seed references remain selectable after the search-result CSV is replaced',()=>{
 const s=setup(),c=s.context;c.state.research_workbench.target_patents=[{id:'JP-seed',title:'基準の公報',ipc:'B60W',fi:'B60W 60/00'}];c.researchBrief().target_ids=['JP-seed'];c.state.patents=[{id:'JP-result',title:'今回の結果',ipc:'G05D'}];
 const before=JSON.stringify(c.state),html=c.researchTargetOptions();assert.match(html,/JP-seed/);assert.match(html,/value="JP-seed" checked/);assert.match(html,/JP-result/);assert.equal(JSON.stringify(c.state),before);
});
test('current CSV and reference with same publication ID produce one selectable target',()=>{
 const s=setup(),c=s.context;c.state.research_workbench.target_patents=[{id:'JP-same',title:'保存した基準',ipc:'B60'}];c.state.patents=[{id:'JP-same',title:'現在のメタデータ',ipc:'B60W'}];
 const html=c.researchTargetOptions();assert.equal((html.match(/value="JP-same"/g)||[]).length,1);assert.match(html,/現在のメタデータ/);
});
test('planning guard prevents duplicate concurrent LLM requests and clears after failure',async()=>{
 const s=setup(),c=s.context;let reject,calls=0;c.api=()=>{calls++;return new Promise((_yes,no)=>reject=no);};
 const pending=c.requestResearchPlan(true);await assert.rejects(c.requestResearchPlan(true),/起案中/);assert.equal(calls,1);reject(new Error('失敗'));await assert.rejects(pending,/失敗/);assert.equal(s.read('researchPlanning'),false);
});

test('previous plan is visibly marked after an entry change while original input and send guard survive',()=>{
 const s=setup(),c=s.context,p=plan();p.brief=structuredClone(c.state.research_workbench.brief);p.brief.target_ids=['JP-seed'];c.state.research_workbench.brief.target_ids=['JP-seed'];c.state.research_workbench.plan=p;
 c.researchBrief().entry_mode='discover';c.researchBrief().target_ids=[];c.renderResearchWorkbench();const html=s.dom['#research-workbench'].innerHTML;
 const notice=html.match(/<div id="research-stale-plan"[^>]*>/)[0];assert.doesNotMatch(notice,/hidden/);assert.match(html,/前回の案/);assert.equal(c.researchBrief().purpose,'周辺調査');assert.equal(c.researchBrief().goal,'走行支援');assert.throws(()=>c.researchPlanPayload(),/もう一度起案/);
 c.researchBrief().entry_mode='target';c.researchBrief().target_ids=['JP-seed'];c.renderResearchWorkbench();assert.match(s.dom['#research-workbench'].innerHTML.match(/<div id="research-stale-plan"[^>]*>/)[0],/hidden/);
});

test('brief edits show the previous-plan notice immediately without erasing concept edits',()=>{
 const s=setup(),c=s.context,p=plan();p.brief=structuredClone(c.state.research_workbench.brief);c.state.research_workbench.plan=p;c.researchPlanState(p);s.read('researchPlanDraft').concepts[0].terms=['保持する観点'];
 let inputHandler;s.dom['#research-purpose']={value:'変更後の目的'};s.dom['#research-stale-plan']={hidden:true};s.groups['.research-brief input,.research-brief textarea']=[{addEventListener:(_event,handler)=>inputHandler=handler}];c.renderResearchWorkbench();inputHandler();
 assert.equal(s.dom['#research-stale-plan'].hidden,false);assert.equal(c.researchBrief().purpose,'変更後の目的');assert.equal(s.read('researchPlanDraft').concepts[0].terms[0],'保持する観点');assert.throws(()=>c.researchPlanPayload(),/もう一度起案/);
});

function unionPlan(){const p=plan();p.concepts=[
 {id:'driving',name:'自動運転',role:'required',terms:['自動運転','自律走行'],abstract_terms:[],or_group:'driving_axis',group_reason:'技術の代替表現として拾う',reason:'入力',evidence_ids:[]},
 {id:'system',name:'運転システム',role:'required',terms:['運転システム'],abstract_terms:[],or_group:'driving_axis',group_reason:'技術の代替表現として拾う',reason:'入力',evidence_ids:[]},
 {id:'vehicle',name:'車両',role:'required',terms:['車両'],abstract_terms:[],or_group:'',reason:'対象',evidence_ids:[]},
 {id:'optional',name:'速度制御',role:'optional',terms:['速度制御'],abstract_terms:[],or_group:'driving_axis',reason:'任意',evidence_ids:[]},
 {id:'excluded',name:'玩具',role:'exclude',terms:['玩具'],abstract_terms:[],reason:'明示除外',evidence_ids:[]}
 ];return p;}

test('proposed union groups survive preview payload without selecting optional or excluded concepts',()=>{
 const s=setup(),c=s.context,p=unionPlan();c.state.research_workbench.plan=p;const d=c.researchPlanState(p),payload=c.researchPlanPayload();
 assert.deepEqual([...payload.concepts].map(x=>x.id),['driving','system','vehicle']);assert.equal(payload.concepts[0].or_group,'driving_axis');assert.equal(payload.concepts[1].or_group,'driving_axis');assert.notEqual(payload.concepts[2].or_group,'driving_axis');assert.equal(d.concepts[3].selected,false);assert.equal(d.concepts[4].selected,false);
 const html=c.researchLogicHtml(p,d);assert.equal((html.match(/class="research-logic-set"/g)||[]).length,2);assert.match(html,/OR · 和集合/);assert.match(html,/積集合/);assert.doesNotMatch(html,/速度制御|玩具/);
});

test('group control supports merging then separating one concept with readable names and no raw IDs',()=>{
 const s=setup(),c=s.context,p=unionPlan(),d=c.researchPlanState(p),system=d.concepts[1];let options=c.researchGroupOptions(p,d,system);
 assert.match(options,/条件 · 自動運転 \/ 運転システム \/ 速度制御/);assert.match(options,/この観点だけの独立条件にする/);
 assert.doesNotMatch(options.replace(/<[^>]*>/g,''),/driving_axis|ui_/);
 const separate=options.match(/value="([^"]+)"[^>]*>この観点だけの独立条件にする/)[1];system.or_group=separate;
 assert.equal((c.researchLogicHtml(p,d).match(/class="research-logic-set"/g)||[]).length,3);
 system.or_group=d.concepts[2].or_group;const html=c.researchLogicHtml(p,d);assert.equal((html.match(/class="research-logic-set"/g)||[]).length,2);assert.match(c.researchGroupOptions(p,d,system),/条件 · 運転システム \/ 車両/);
});

test('unique independent defaults never collide with a proposed group identifier',()=>{
 const s=setup(),c=s.context,p=unionPlan();p.concepts[0].or_group='ui_3';const d=c.researchPlanState(p);
 assert.notEqual(d.concepts[2].or_group,'ui_3');assert.notEqual(d.concepts[2].or_group,d.concepts[0].or_group);
});

test('explicit must-have aspects cannot be merged into OR and are not offered to other groups',()=>{
 for(const legacy of [false,true]){
  const s=setup(),c=s.context,p=unionPlan();p.concepts[2].name='必須: 車両';p.concepts[2].locked_and=!legacy;p.concepts[2].or_group='driving_axis';p.brief={...c.state.research_workbench.brief,user_aspects:['必須: 車両']};c.state.research_workbench.brief.user_aspects=['必須: 車両'];c.state.research_workbench.plan=p;
  const d=c.researchPlanState(p),html=c.researchPlanHtml(p),payload=c.researchPlanPayload();assert.match(html,/aria-label="必須: 車両 の和集合グループ" disabled/);assert.equal(payload.concepts[2].or_group,'');assert.equal((c.researchLogicHtml(p,d).match(/class="research-logic-set"/g)||[]).length,2);
  assert.doesNotMatch(c.researchGroupOptions(p,d,d.concepts[0]),/条件 · [^<]*車両/);
 }
});

test('explicit exclusion is separate from unions even if an old draft contains a group',()=>{
 const s=setup(),c=s.context,p=unionPlan();c.state.research_workbench.plan=p;const d=c.researchPlanState(p);d.concepts[4].selected=true;d.concepts[4].or_group='driving_axis';
 const payload=c.researchPlanPayload(),html=c.researchLogicHtml(p,d);assert.equal(payload.concepts.find(x=>x.id==='excluded').or_group,'');assert.match(html,/NOT · 除外/);assert.equal((html.match(/class="research-logic-set"/g)||[]).length,2);
});

test('capturing group edits preserves plan metadata, edited terms and selection state',()=>{
 const s=setup(),c=s.context,p=unionPlan();c.state.research_workbench.plan=p;const d=c.researchPlanState(p);s.dom['#research-concepts']={};
 s.groups['.research-concept']=d.concepts.map(x=>({dataset:{concept:x.id},children:{'input[data-use]':{checked:x.selected},'select[data-role]':{value:x.role},'select[data-or-group]':{value:x.id==='system'?'ui_3':x.or_group},textarea:{value:x.id==='system'?'運転システム\n走行制御':x.terms.join('\n')}}}));
 c.captureResearchPlan();const payload=c.researchPlanPayload(),system=payload.concepts.find(x=>x.id==='system');assert.equal(system.or_group,'ui_3');assert.deepEqual([...system.terms],['運転システム','走行制御']);assert.equal(s.read('researchPlanDraft').concepts[1].group_reason,'技術の代替表現として拾う');assert.equal(s.read('researchPlanDraft').concepts[3].selected,false);
});

test('changing a group updates the live set diagram and invalidates the prior preview',()=>{
 const s=setup(),c=s.context,p=unionPlan();c.state.research_workbench.plan=p;const d=c.researchPlanState(p);let handler;
 s.dom['#research-concepts']={};s.dom['#research-logic']={innerHTML:''};s.dom['#research-query-preview']={innerHTML:'old'};d.preview={expression:'old',query:{boolean_tree:{}}};
 s.groups['.research-concept']=d.concepts.map(x=>({dataset:{concept:x.id},children:{'input[data-use]':{checked:x.selected},'select[data-role]':{value:x.role},'select[data-or-group]':{value:x.or_group,innerHTML:'',disabled:false},textarea:{value:x.terms.join('\n')}}}));
 s.groups['.research-plan input,.research-plan textarea,.research-plan select']=[{addEventListener:(_event,fn)=>handler=fn}];c.renderResearchWorkbench();s.groups['.research-concept'][1].children['select[data-or-group]'].value='ui_3';handler();
 assert.equal(s.read('researchPlanDraft').preview,null);assert.equal(s.dom['#research-query-preview'].innerHTML,'');assert.match(s.dom['#research-logic'].innerHTML,/運転システム/);assert.equal(s.read('researchPlanDraft').concepts[1].or_group,'ui_3');
});

test('group reasons and live query terms escape untrusted model strings',()=>{
 const s=setup(),c=s.context,p=unionPlan();p.concepts[0].name='<img>';p.concepts[0].group_reason='<script>reason';p.concepts[0].terms=['<svg>'];const html=c.researchPlanHtml(p);assert.doesNotMatch(html,/<img>|<script>|<svg>/);assert.match(html,/&lt;img&gt;/);assert.match(html,/&lt;svg&gt;/);
});
