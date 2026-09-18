const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/query-library-ui.js','utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
const entry=()=>({id:'e1',name:'車両調査',purpose:'先行技術調査',facets:['用途'],source_format:'jplatpat',original_text:'[[車両/TX+自動車/TX]*[B60/IP]]-[玩具/TX]',status:'editable',issues:[],terms:['車両','自動車','玩具'],classification_terms:['IPC:B60'],query:{boolean_tree:{op:'not',children:[{op:'and',children:[{op:'or',children:[{op:'text',value:'車両'},{op:'text',value:'自動車'}]},{op:'class',system:'IPC',value:'B60'}]},{op:'text',value:'玩具'}]}}});
function setup(){
 const requests=[],navigation=[],dom={};
 const c={document:{getElementById:id=>dom[id]||null},state:{research_workbench:{brief:{purpose:'今回の目的'},library:[entry()]}},api:async(path,body)=>{requests.push({path,body});if(path.endsWith('/adapt'))return {query:entry().query,expression:'preview',preview_hash:'h1',warnings:[]};return c.state;},setTab:tab=>navigation.push(tab),setStep:step=>navigation.push(step),setTimeout};
 vm.createContext(c);vm.runInContext(source,c);return {c,requests,navigation,dom,read:expr=>vm.runInContext(expr,c),set:(key,value)=>{c.tmp=value;vm.runInContext(`queryLibraryDraft.${key}=tmp`,c);delete c.tmp;}};
}
test('empty library offers paste/file and explicit import formats',()=>{
 const {c}=setup(),html=c.queryLibraryHtml({library:[]});assert.match(html,/ql-file/);assert.match(html,/ql-text/);assert.match(html,/schema_version/);assert.match(html,/J-PlatPat/);assert.doesNotMatch(html,/id="ql-apply"/);
});
test('editable example preserves original purpose and offers current workbench purpose',()=>{
 const {c}=setup(),html=c.queryLibraryHtml(c.state.research_workbench);assert.match(html,/先行技術調査/);assert.match(html,/今回の目的/);assert.match(html,/IPC:B60/);assert.match(html,/ql-preview/);assert.doesNotMatch(html,/id="ql-apply"/);
});
test('reference-only examples cannot be adapted or downloaded as structure',()=>{
 const {c}=setup();c.state.research_workbench.library[0].status='reference_only';c.state.research_workbench.library[0].issues=['未対応構文'];const html=c.queryLibraryHtml(c.state.research_workbench);assert.match(html,/未対応構文/);assert.doesNotMatch(html,/id="ql-preview"|id="ql-apply"|id="ql-download"/);assert.throws(()=>c.queryLibraryPortable(c.state.research_workbench.library[0]));
});
test('all imported content is escaped including tree values and errors',()=>{
 const s=setup(),evil='<img src=x onerror="alert(1)">';const e=s.c.state.research_workbench.library[0];for(const k of ['id','name','purpose','original_text','source_format'])e[k]=evil;e.terms=[evil];e.classification_terms=[evil];e.facets=[evil];s.set('error',evil);let html=s.c.queryLibraryHtml(s.c.state.research_workbench);assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);assert.doesNotMatch(s.c.queryLibraryTreeHtml({op:'class',value:evil,system:evil}),/<img/);
});
test('preview sends exact full-phrase mappings and an explicit target purpose',async()=>{
 const s=setup();s.c.queryLibraryChoose('e1');s.set('edits',{'車両':'航空機','自動車':'自動車'});s.set('classEdits',{'IPC:B60':'B64'});s.set('adaptPurpose','航空機の調査');await s.c.queryLibraryAction('preview');assert.deepEqual(plain(s.requests),[{path:'/research/library/adapt',body:{example_id:'e1',replacements:{'車両':'航空機'},class_replacements:{'IPC:B60':'B64'},purpose:'航空機の調査'}}]);assert.equal(s.read('queryLibraryDraft.preview.preview_hash'),'h1');
});
test('apply requires reviewed preview and carries stale-state hash',async()=>{
 const s=setup();await assert.rejects(s.c.queryLibraryAction('apply'),/プレビュー/);assert.equal(s.requests.length,0);await s.c.queryLibraryAction('preview');await s.c.queryLibraryAction('apply');assert.deepEqual(plain(s.requests[1]),{path:'/research/library/apply',body:{example_id:'e1',replacements:{},class_replacements:{},purpose:'今回の目的',expected_preview_hash:'h1'}});assert.deepEqual(s.navigation,['explore',1]);
});
test('edits after preview invalidate apply and do not send a write',async()=>{
 const s=setup();await s.c.queryLibraryAction('preview');s.set('edits',{'車両':'航空機'});await assert.rejects(s.c.queryLibraryAction('apply'),/変わりました/);assert.equal(s.requests.length,1);assert.equal(s.read('queryLibraryDraft.preview'),null);
});
test('blank replacements fail before network and failed preview keeps drafts',async()=>{
 const s=setup();s.c.queryLibraryChoose('e1');s.set('edits',{'車両':''});await assert.rejects(s.c.queryLibraryAction('preview'),/空欄/);assert.equal(s.requests.length,0);s.set('edits',{'車両':'航空機'});s.c.api=async()=>{throw Error('接続失敗');};await assert.rejects(s.c.queryLibraryAction('preview'),/接続失敗/);assert.equal(s.read('queryLibraryDraft.edits["車両"]'),'航空機');assert.equal(s.read('queryLibraryDraft.busy'),false);assert.equal(s.read('queryLibraryDraft.preview'),null);
});
test('file reader enforces limits before reading and auto selects JSON',async()=>{
 const s=setup();let read=false;await assert.rejects(s.c.queryLibraryReadFile({size:400001,text:async()=>{read=true;return '{}';}}),/400KB/);assert.equal(read,false);await s.c.queryLibraryReadFile({size:2,name:'example.json',text:async()=>'{}'});assert.equal(s.read('queryLibraryDraft.source_format'),'portable_json');assert.equal(s.read('queryLibraryDraft.text'),'{}');assert.equal(s.read('queryLibraryDraft.name'),'example');
});
test('portable download contains original logical tree only, no workspace settings',()=>{
 const s=setup(),e=entry();e.query.settings={api_key:'secret'};const data=plain(s.c.queryLibraryPortable(e));assert.equal(data.schema_version,1);assert.deepEqual(data.query,{boolean_tree:e.query.boolean_tree});assert.equal(JSON.stringify(data).includes('secret'),false);
});
test('preview shows nested AND OR NOT and apply only when a preview exists',()=>{
 const s=setup();s.c.queryLibraryChoose('e1');s.set('preview',{query:entry().query,preview_hash:'h1',warnings:[]});const html=s.c.queryLibraryHtml(s.c.state.research_workbench);assert.match(html,/ql-tree-not/);assert.match(html,/ql-tree-or/);assert.match(html,/ql-tree-and/);assert.match(html,/id="ql-apply"/);
});
test('busy guard prevents duplicate requests',async()=>{
 const s=setup();s.set('busy',true);await s.c.queryLibraryAction('import');await s.c.queryLibraryAction('preview');assert.equal(s.requests.length,0);
});
test('styles preserve hidden alerts and honor reduced motion/static setting',()=>{
 const css=fs.readFileSync('static/query-library-ui.css','utf8');assert.match(css,/\[hidden\]\{display:none!important/);assert.match(css,/prefers-reduced-motion:reduce/);assert.match(css,/data-atlas-motion=static/);
});
test('object-prototype keyword names are retained as ordinary literal terms',()=>{
 const s=setup();const e=entry();e.terms=['__proto__','constructor'];s.c.queryLibraryChoose(e.id);
 vm.runInContext('queryLibraryDraft.edits["__proto__"]="literal replacement";queryLibraryDraft.edits.constructor="new literal";',s.c);
 const payload=plain(s.c.queryLibraryPayload(e,s.c.state.research_workbench));assert.equal(payload.replacements.__proto__,'literal replacement');assert.equal(payload.replacements.constructor,'new literal');
});
test('LLM suggestion only fills editable proposals and invalidates an old preview',async()=>{
 const s=setup();s.c.queryLibraryChoose('e1');s.set('adaptPurpose','航空機の調査');s.set('adaptGoal','航空機の経路制御に応用する');s.set('preview',{preview_hash:'old'});
 const proposal={replacements:{'車両':'航空機'},class_replacements:{'IPC:B60':'B64'},reasons:[{kind:'term',original:'車両',replacement:'航空機',reason:'対象機器の変更'}],questions:['NOT条件を確認してください。']};
 s.c.api=async(path,body)=>{s.requests.push({path,body});return proposal;};const before=JSON.stringify(s.c.state);await s.c.queryLibraryAction('suggest');
 assert.deepEqual(plain(s.requests),[{path:'/research/library/suggest',body:{example_id:'e1',purpose:'航空機の調査',goal:'航空機の経路制御に応用する'}}]);assert.equal(s.read('queryLibraryDraft.edits["車両"]'),'航空機');assert.equal(s.read('queryLibraryDraft.preview'),null);assert.equal(JSON.stringify(s.c.state),before);assert.equal(s.navigation.length,0);assert.match(s.c.queryLibraryHtml(s.c.state.research_workbench),/対象機器の変更/);
});
test('LLM suggestions require both purpose and technical goal before network',async()=>{
 const s=setup();await assert.rejects(s.c.queryLibraryAction('suggest'),/技術説明/);assert.equal(s.requests.length,0);
});
test('failed or invalid LLM suggestion preserves manual edits and shows error',async()=>{
 const s=setup();s.c.queryLibraryChoose('e1');s.set('adaptGoal','技術の説明');s.set('edits',{'車両':'搬送機'});s.c.api=async()=>{throw Error('LLM失敗');};await assert.rejects(s.c.queryLibraryAction('suggest'),/LLM失敗/);assert.equal(s.read('queryLibraryDraft.edits["車両"]'),'搬送機');
 assert.throws(()=>s.c.queryLibraryAcceptSuggestion(entry(),{replacements:{unknown:'new'},class_replacements:{},reasons:[],questions:[]}),/元の検索式/);assert.equal(s.read('queryLibraryDraft.edits["車両"]'),'搬送機');
});
test('suggestion reasons are escaped and changes clear stale reasons',()=>{
 const s=setup();s.c.queryLibraryChoose('e1');s.set('suggestion',{replacements:{},class_replacements:{},reasons:[{original:'<img>',replacement:'<img>',reason:'<img>'}],questions:['<img>']});assert.doesNotMatch(s.c.queryLibraryHtml(s.c.state.research_workbench),/<img>/);s.c.queryLibraryInvalidate();assert.equal(s.read('queryLibraryDraft.suggestion'),null);
});
