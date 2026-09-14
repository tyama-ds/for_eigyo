const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/query-output.js','utf8');

test('a delayed edit from a previous history card preserves the current export',async()=>{
 const current={query_id:'new',can_copy:true,expression:'battery/TX'};
 let disabled=0;
 const context={currentOutput:current,exportRequest:8,$:selector=>selector==='#query-output'?{dataset:{queryId:'new'}}:{setAttribute:()=>disabled++},api:()=>{throw new Error('obsolete request must not be sent');}};
 vm.createContext(context);vm.runInContext(source,context);
 await context.requestOutput({id:'old'});
 assert.equal(context.currentOutput,current);
 assert.equal(context.exportRequest,8);
 assert.equal(disabled,0);
});

test('a delayed edit after leaving the search step has no side effects',async()=>{
 const context={currentOutput:null,exportRequest:8,$:()=>null};
 vm.createContext(context);vm.runInContext(source,context);
 await context.requestOutput({id:'old'});
 assert.equal(context.exportRequest,8);
});

test('query cards include actual classification-change evidence through the shared lexical helper',()=>{
 const context={formatCatalog:[{id:'jplatpat',name:'J-PlatPat'}],outputFormat:'jplatpat',outputEdits:new Map(),
  esc:value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))};
 vm.createContext(context);
 const refinementSource=fs.readFileSync('static/refinement-ui.js','utf8');
 // Exercise a shared script binding that is not a globalThis property. Use the
 // real refinement renderer so query-card integration cannot silently vanish.
 vm.runInContext(`const classificationChangesHtml=(()=>{${refinementSource}\nreturn classificationChangesHtml;})();`,context);
 assert.equal(vm.runInContext('typeof classificationChangesHtml',context),'function');
 assert.equal(context.classificationChangesHtml,undefined);
 vm.runInContext(source,context);
 const query={id:'refined',version:2,type:'改善案',expression:'自動運転/TX',keywords:['自動運転'],classification_changes:{
  added_keys:['IPC:B60W60/00'],removed_keys:['IPC:H01M10/00'],evidence:[{
   kind:'IPC',code:'B60W60/00',keep_count:2,exclude_count:1,reason:'必要公報で支持された分類',
   keep_ids:['JP202400001A','JP<support>'],exclude_ids:['JP202400002A'],
  }],
 }};
 const html=context.queryCard(query);
 assert.ok(html.includes('判定・学習から反映したIPCと根拠'));
 assert.ok(html.includes('追加：IPC:B60W60/00'));
 assert.ok(html.includes('解除：IPC:H01M10/00'));
 assert.ok(html.includes('必要公報で支持された分類'));
 assert.ok(html.includes('JP202400001A'));
 assert.ok(html.includes('JP202400002A'));
 assert.ok(html.includes('JP&lt;support&gt;'));
 assert.ok(!html.includes('JP<support>'));
 const initial=context.queryCard({...query,id:'initial',version:1,type:'初案',classification_changes:undefined});
 assert.ok(!initial.includes('classification-feedback-history'));
 assert.ok(!initial.includes('判定・学習から反映したIPCと根拠'));
});
