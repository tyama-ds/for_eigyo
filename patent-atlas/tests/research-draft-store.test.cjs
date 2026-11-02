const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('static/research-draft-store.js', 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));

function setup(options = {}) {
  const saved = options.saved || new Map(), listeners = new Map(), timers = new Map();
  let timerId = 0;
  const brief = {entry_mode:'target',purpose:'先行技術を調べる',goal:'原文の技術',keywords:'センサ',user_aspects:[],target_ids:['JP1']};
  const researchPlan = {id:'rp1',concepts:[
    {id:'c1',name:'温度検出',role:'required',terms:['温度'],reason:'SERVER_DESCRIPTION',evidence_ids:['JP1']},
    {id:'c2',name:'冷却',role:'optional',terms:['冷却'],reason:'SERVER_DESCRIPTION'},
  ]};
  const inventionPlan = {id:'ip1',elements:[
    {id:'e1',name:'センサ',perspectives:[{id:'e1p1',terms:['温度センサ']},{id:'e1p2',terms:['熱電対']}]},
    {id:'e2',name:'制御',perspectives:[{id:'e2p1',terms:['冷却制御']}]},
  ]};
  const state = {settings:{api_key:'API_SECRET',proxy:'PROXY_SECRET'},patents:[{id:'JP1',abstract:'PATENT_FULL_TEXT'}],research_workbench:{id:'workspace-a',revision:3,brief,plan:researchPlan,invention:{plan:inventionPlan}}};
  const status = {textContent:'',setAttribute(){}};
  const context = {
    state,structuredClone,Set,Map,JSON,Date,
    researchDraft:null,researchPlanDraft:null,inventionInputDraft:null,inventionPlanDraft:null,
    document:{getElementById:()=>status},
    sessionStorage:{
      getItem:key=>{if(options.readError)throw new Error('denied');return saved.get(key)??null;},
      setItem:(key,value)=>{if(options.writeError)throw new Error('quota');saved.set(key,value);},
      removeItem:key=>{if(options.removeError)throw new Error('denied');saved.delete(key);},
    },
    window:{addEventListener:(event,handler)=>{const items=listeners.get(event)||[];items.push(handler);listeners.set(event,items);}},
    setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},
    clearTimeout:id=>timers.delete(id),
  };
  if(options.noInvention){delete context.inventionInputDraft;delete context.inventionPlanDraft;delete state.research_workbench.invention;}
  vm.createContext(context);vm.runInContext(script,context);
  function seedEdits(){
    context.researchDraft={...clone(brief),goal:'編集中の説明\n引用「温度」',keywords:'温度 自動車',target_ids:['JP1','JP2'],user_aspects:['機能の観点'],api_key:'DRAFT_SECRET'};
    context.researchPlanDraft={id:'rp1',concepts:researchPlan.concepts.map((row,index)=>({...clone(row),selected:index===0,role:index?'exclude':'required',or_group:'together',terms:[index?'玩具':'温度検出','sensor']})),classification_keys:['IPC:G01K'],acknowledge_unmapped:true,preview:{query:{secret:'OLD_PREVIEW'}},other:'UNEXPECTED_DRAFT_PROPERTY'};
    if(!options.noInvention){
      context.inventionInputDraft={invention_text:'取得温度に応じて\n冷却ファンを制御する。',manual_elements:['温度センサ','回転数の制御'],settings:{api_key:'INPUT_SECRET'}};
      context.inventionPlanDraft={id:'ip1',strategy:'alternatives',element_ids:['e1','e2'],elements:inventionPlan.elements.map(e=>({id:e.id,perspectives:e.perspectives.map((p,index)=>({id:p.id,selected:index===0,terms:[p.terms[0],'編集した語'],classification_keys:['IPC:G01K'],class_mode:'or',reason:'DRAFT_REASON'}))})),perspective_joins:{e1:'and',e2:'or'},exclusions:{all:['玩具'],e1:['赤外線'],e2:[]},confirm_exclusions:true,confirm_class_intersection:false,preview:{expression:'OLD_PREVIEW'},previewPayload:'OLD_HASH'};
    }
  }
  return {context,saved,listeners,timers,status,seedEdits,read:expression=>vm.runInContext(expression,context)};
}

test('reload restores edited input and selections atomically, without previews or server writes',()=>{
  const first=setup();first.seedEdits();const before=JSON.stringify(first.context);
  assert.equal(first.context.researchDraftFlush(),true);
  assert.equal(JSON.stringify(first.context),before);
  const second=setup({saved:first.saved}),serverBefore=JSON.stringify(second.context.state);
  assert.equal(second.context.initResearchDraftStore(),true);
  assert.equal(JSON.stringify(second.context.state),serverBefore);
  assert.equal(second.context.researchDraft.goal,'編集中の説明\n引用「温度」');
  assert.deepEqual([...second.context.researchDraft.target_ids],['JP1','JP2']);
  assert.equal(second.context.researchPlanDraft.concepts[1].role,'exclude');
  assert.equal(second.context.researchPlanDraft.concepts[0].name,'温度検出');
  assert.equal(second.context.researchPlanDraft.concepts[0].reason,'SERVER_DESCRIPTION');
  assert.equal(second.context.inventionInputDraft.invention_text,'取得温度に応じて\n冷却ファンを制御する。');
  assert.equal(second.context.inventionPlanDraft.strategy,'alternatives');
  assert.equal(second.context.inventionPlanDraft.elements[0].perspectives[1].selected,false);
  assert.deepEqual([...second.context.inventionPlanDraft.elements[0].perspectives[1].terms],['熱電対','編集した語']);
  assert.deepEqual([...second.context.inventionPlanDraft.exclusions.e1],['赤外線']);
  assert.equal(second.context.researchPlanDraft.preview,null);
  assert.equal(second.context.inventionPlanDraft.preview,null);
  assert.equal(second.context.inventionPlanDraft.previewPayload,null);
  assert.match(second.context.researchDraftStatusHtml(),/このタブの下書きを復元/);
});

test('only editable fields are stored; settings, source prose, and review tokens never enter storage',()=>{
  const s=setup();s.seedEdits();assert.equal(s.context.researchDraftFlush(),true);
  const raw=[...s.saved.values()][0];
  for(const excluded of ['API_SECRET','PROXY_SECRET','PATENT_FULL_TEXT','SERVER_DESCRIPTION','OLD_PREVIEW','OLD_HASH','DRAFT_SECRET','DRAFT_REASON','INPUT_SECRET','UNEXPECTED_DRAFT_PROPERTY','preview'])assert.equal(raw.includes(excluded),false,excluded);
  assert.ok(raw.includes('取得温度に応じて'));
  assert.ok(raw.includes('温度検出'));
});

test('a different server revision, plan, or workspace never automatically restores older edits',()=>{
  const first=setup();first.seedEdits();first.context.researchDraftFlush();
  for(const change of [s=>s.research_workbench.revision++,s=>s.research_workbench.plan.id='rp2',s=>s.research_workbench.invention.plan.id='ip2',s=>s.research_workbench.id='workspace-b']){
    const next=setup({saved:first.saved});change(next.context.state);const before=JSON.stringify(next.context.state);
    assert.equal(next.context.researchDraftRestore(),false);
    assert.equal(next.context.researchDraft,null);assert.equal(next.context.inventionPlanDraft,null);
    assert.equal(JSON.stringify(next.context.state),before);
  }
});

test('pending edits cannot be written under a new revision until caller explicitly rebases the response',()=>{
  const s=setup();s.context.initResearchDraftStore();s.seedEdits();s.context.researchDraftFlush();const old=[...s.saved.values()][0];
  s.context.researchDraftScheduleSave();s.context.state.research_workbench.revision++;
  assert.equal(s.context.researchDraftFlush(),false);assert.equal([...s.saved.values()][0],old);
  s.context.researchDraft.goal='応答反映後に保持した入力';assert.equal(s.context.researchDraftRebase(),true);assert.equal(s.context.researchDraftFlush(),true);
  const next=setup({saved:s.saved});next.context.state.research_workbench.revision++;
  assert.equal(next.context.researchDraftRestore(),true);assert.equal(next.context.researchDraft.goal,'応答反映後に保持した入力');
});

test('new plan response excludes the former plan draft while keeping valid current inputs',()=>{
  const s=setup();s.context.initResearchDraftStore();s.seedEdits();s.context.state.research_workbench.plan.id='rp-new';s.context.state.research_workbench.invention.plan.id='ip-new';s.context.state.research_workbench.revision++;
  assert.equal(s.context.researchDraftRebase(),true);assert.equal(s.context.researchDraftFlush(),true);
  const record=JSON.parse([...s.saved.values()][0]);
  assert.equal(record.drafts.research_plan,null);assert.equal(record.drafts.invention_plan,null);
  assert.equal(record.drafts.brief.goal,s.context.researchDraft.goal);assert.equal(record.context.invention_plan_id,'ip-new');
});

test('switching projects does not copy old live forms into the new project',()=>{
  const s=setup();s.context.initResearchDraftStore();s.seedEdits();s.context.researchDraftFlush();const before=[...s.saved.entries()];
  s.context.state.research_workbench.id='workspace-b';assert.equal(s.context.researchDraftRebase(),false);assert.equal(s.context.researchDraftFlush(),false);
  assert.deepEqual([...s.saved.entries()],before);
  assert.equal(s.context.initResearchDraftStore(),false);
  assert.equal(s.context.researchDraft,null);assert.equal(s.context.researchPlanDraft,null);assert.equal(s.context.inventionInputDraft,null);assert.equal(s.context.inventionPlanDraft,null);
});

test('corrupt records and invalid references cannot partially replace a live draft',()=>{
  const first=setup();first.seedEdits();first.context.researchDraftFlush();const [key,raw]=[...first.saved.entries()][0];
  const wrong=JSON.parse(raw);wrong.drafts.invention_plan.elements[1].perspectives[0].id='missing';
  for(const content of ['not json','null',JSON.stringify(wrong)]){
    const next=setup({saved:new Map([[key,content]])});next.context.researchDraft={already:'editing'};
    assert.equal(next.context.researchDraftRestore(),false);assert.deepEqual(next.context.researchDraft,{already:'editing'});
    assert.match(next.context.researchDraftStatusHtml(),/保存・復元できません/);
  }
});

test('storage denial and quota failures leave editable inputs intact and show a visible failure',()=>{
  for(const options of [{readError:true},{writeError:true}]){
    const s=setup(options);s.seedEdits();const before=JSON.stringify(s.context.researchDraft);
    assert.doesNotThrow(()=>s.context.initResearchDraftStore());
    if(options.writeError)assert.equal(s.context.researchDraftFlush(),false);
    assert.equal(JSON.stringify(s.context.researchDraft),before);assert.match(s.context.researchDraftStatusHtml(),/保存・復元できません/);
  }
});

test('missing invention feature still saves and restores the common research draft',()=>{
  const first=setup({noInvention:true});first.seedEdits();assert.equal(first.context.researchDraftFlush(),true);
  const next=setup({saved:first.saved,noInvention:true});assert.equal(next.context.researchDraftRestore(),true);
  assert.equal(next.context.researchDraft.keywords,'温度 自動車');assert.equal('inventionInputDraft' in next.context,false);
});

test('repeated initialization never overwrites subsequent edits or duplicates unload listeners',()=>{
  const first=setup();first.seedEdits();first.context.researchDraftFlush();
  const next=setup({saved:first.saved});assert.equal(next.context.initResearchDraftStore(),true);
  next.context.researchDraft.goal='復元後の新しい編集';assert.equal(next.context.initResearchDraftStore(),false);
  assert.equal(next.context.researchDraft.goal,'復元後の新しい編集');
  assert.equal(next.listeners.get('pagehide').length,1);assert.equal(next.listeners.get('beforeunload').length,1);
});

test('debounce saves the latest edit and unchanged server updates do not cancel it',()=>{
  const s=setup();s.context.initResearchDraftStore();s.seedEdits();s.context.researchDraftScheduleSave();s.context.researchDraft.goal='最後の入力';s.context.researchDraftScheduleSave();
  assert.equal(s.timers.size,1);assert.equal(s.context.researchDraftRebase(),true);assert.equal(s.timers.size,1);
  [...s.timers.values()][0]();assert.equal(s.timers.size,0);assert.equal(JSON.parse([...s.saved.values()][0]).drafts.brief.goal,'最後の入力');
});

test('pagehide captures the final keystroke and flushes without a scheduled timer',()=>{
  const s=setup();s.seedEdits();s.context.captureResearchDraft=()=>{s.context.researchDraft.goal='最後のキー入力';s.context.researchDraftScheduleSave();};
  s.context.initResearchDraftStore();s.listeners.get('pagehide')[0]();
  assert.equal(JSON.parse([...s.saved.values()][0]).drafts.brief.goal,'最後のキー入力');assert.equal(s.timers.size,0);
});

test('reset removes only this workspace tab record, never server state or active edits',()=>{
  const s=setup();s.seedEdits();s.context.researchDraftFlush();s.saved.set('unrelated','keep');const server=JSON.stringify(s.context.state),draft=JSON.stringify(s.context.researchDraft);
  assert.equal(s.context.researchDraftReset(),true);assert.equal(s.saved.size,1);assert.equal(s.saved.get('unrelated'),'keep');
  assert.equal(JSON.stringify(s.context.state),server);assert.equal(JSON.stringify(s.context.researchDraft),draft);
});
