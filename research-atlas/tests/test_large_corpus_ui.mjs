import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function context(names,values={}){
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,{value:'',checked:false,hidden:false,open:true});return elements.get(id);};
  const ctx=vm.createContext({$,URLSearchParams,FormData,setTimeout,clearTimeout,num:String,e:escape,icon:()=>'',...values});
  for(const name of names){const line=source.split('\n').find(line=>new RegExp(`^(?:async )?function ${name}\\(`).test(line));assert.ok(line,name);vm.runInContext(line,ctx);}
  return {ctx,elements,$:ctx.$};
}
const file=name=>({name,size:42*1024*1024});
const dataset=(id,paper_count,year=2025)=>({id,paper_count,name:id,is_demo:false,analysis_defaults:{start_year:1996,end_year:year}});
function importHarness(options={}){
  const calls=[],state={file:file('a.csv'),files:[file('a.csv'),file('b.csv'),file('c.csv')],importBusy:false,selectionVersion:0,uploadMode:'papers',uploadBase:null,dataset:null,reportCache:new Map()};
  let counter=0;
  const h=context(['applyDatasetAnalysisDefaults','importFile','renderImportProgress'],{state,
    setImportBusy:value=>{state.importBusy=value;},updateUploadMode:()=>{},updateDataset:()=>{},closeDialogs:()=>calls.push('close'),
    refreshStatus:async()=>calls.push('status'),message:value=>calls.push(['message',value]),showStoredDataset:()=>calls.push('stored'),runAnalysis:async()=>calls.push('analyze'),reportsForDataset:()=>[],
    api:async url=>{calls.push(['import',url]);counter++;if(options.failAt===counter)throw new Error('CSV row limit');return {dataset:dataset(`batch-${counter}`,20000),report:{duplicates_removed:counter===1?2:0}};},
    post:async(url,body)=>{calls.push(['merge',body.base_dataset_id,body.additional_dataset_id]);return {dataset:dataset(`merged-${counter}`,counter===2?39000:58000),report:{duplicates_removed:1000}};},
  });
  h.$('#csv-provider').value='scopus_csv';h.$('#csv-after-import').value='save';
  return {...h,calls,state};
}

test('CSV selection honors an explicit positive size limit from an older server',()=>{
  const h=context(['selectFiles'],{state:{status:{limits:{max_upload_bytes:268435456}},uploadMode:'papers'},updateUploadMode:()=>{}});
  h.ctx.selectFiles([file('a.csv'),{name:'b.csv',size:268435456}]);
  assert.equal(h.ctx.state.files.length,2);assert.equal(h.$('#csv-after-import').value,'save');assert.equal(h.$('#import-button').disabled,false);
  h.ctx.selectFiles([{name:'too-large.csv',size:268435457}]);
  assert.equal(h.ctx.state.files.length,0);assert.equal(h.$('#import-button').disabled,true);assert.match(h.$('#upload-error').textContent,/256 MB/);
  h.ctx.selectFiles([file('valid.csv'),file('invalid.exe')]);assert.equal(h.ctx.state.files.length,0);
});

test('CSV selection accepts files above 20 MB and 256 MB without a server size limit',()=>{
  for(const status of [{limits:{max_upload_bytes:null}},{limits:{}},undefined]){
    const h=context(['selectFiles'],{state:{status,uploadMode:'papers'},updateUploadMode:()=>{}});
    h.ctx.selectFiles([file('scopus.csv'),{name:'large.csv',size:600*1024*1024}]);
    assert.equal(h.ctx.state.files.length,2);assert.equal(h.$('#csv-after-import').value,'save');
    assert.equal(h.$('#import-button').disabled,false);assert.equal(h.$('#upload-error').hidden,true);
    assert.match(h.$('#file-label').textContent,/642\.0 MB/);
    h.ctx.selectFiles([{name:'large.csv',size:600*1024*1024}]);
    assert.equal(h.ctx.state.file.name,'large.csv');assert.match(h.$('#file-label').textContent,/600\.0 MB/);
    h.ctx.selectFiles([file('invalid.exe')]);assert.equal(h.ctx.state.files.length,0);
  }
});

test('upload instructions retain row limits and do not invent a file size limit',()=>{
  for(const maxBytes of [null,undefined,268435456]){
    const h=context(['updateUploadMode'],{state:{status:{limits:{max_upload_bytes:maxBytes}},uploadMode:'papers'},$$:()=>[],mergeChoice:()=>''});
    h.ctx.updateUploadMode();
    const label=h.$('#upload-limits').textContent;
    assert.match(label,/20000行/);assert.match(label,/200000論文/);
    if(maxBytes)assert.match(label,/256 MB/);
    else{assert.match(label,/ファイル容量の固定上限なし/);assert.doesNotMatch(label,/256|最大 .*MB/);}
  }
});

test('multi-file save imports sequentially and carries the latest merged dataset forward without analysis',async()=>{
  const h=importHarness();await h.ctx.importFile();
  assert.deepEqual(h.calls.filter(Array.isArray).filter(row=>['import','merge'].includes(row[0])),[
    ['import','/api/import'],['import','/api/import'],['merge','batch-1','batch-2'],['import','/api/import'],['merge','merged-2','batch-3']]);
  assert.equal(h.state.dataset.id,'merged-3');assert.equal(h.state.dataset.paper_count,58000);assert.equal(h.state.file,null);
  assert.equal(h.$('#start-year').value,1996);assert.equal(h.$('#end-year').value,2025);
  assert.ok(h.calls.includes('stored'));assert.ok(!h.calls.includes('analyze'));
  assert.equal(h.state.importProgress.length,3);assert.equal(h.state.importProgress[0].duplicates,2);assert.equal(h.state.importProgress[2].duplicates,1000);
  assert.match(h.$('#upload-progress').innerHTML,/58000/);
});

test('failed later CSV retains saved batches and retries only the remaining file into the saved collection',async()=>{
  const h=importHarness({failAt:3});await h.ctx.importFile();
  assert.equal(h.state.dataset.id,'merged-2');assert.equal(h.state.uploadBase.id,'merged-2');assert.equal(h.state.dataset.paper_count,39000);
  assert.equal(h.state.files.length,1);assert.equal(h.state.file.name,'c.csv');assert.equal(h.$('#csv-merge-current').checked,true);
  assert.match(h.$('#upload-error').textContent,/2 \/ 3ファイル、39000論文は保存済み/);assert.match(h.$('#upload-error').textContent,/CSV row limit/);
  assert.ok(!h.calls.includes('analyze'));assert.equal(h.state.importBusy,false);
  h.ctx.api=async()=>({dataset:dataset('retry-3',20000),report:{}});
  await h.ctx.importFile();assert.ok(h.calls.some(row=>Array.isArray(row)&&row[0]==='merge'&&row[1]==='merged-2'&&row[2]==='retry-3'));
});

test('append to the current dataset happens before a single final analysis',async()=>{
  const h=importHarness();h.state.uploadBase=dataset('existing',40000);h.state.files=[h.state.file];h.$('#csv-merge-current').checked=true;h.$('#csv-after-import').value='analyze';
  await h.ctx.importFile();assert.ok(h.calls.some(row=>Array.isArray(row)&&row[0]==='merge'&&row[1]==='existing'&&row[2]==='batch-1'));
  assert.equal(h.calls.filter(row=>row==='analyze').length,1);assert.ok(!h.calls.includes('stored'));
});

test('file names and progress values cannot inject HTML',()=>{
  const h=context(['renderImportProgress'],{state:{importProgress:[{name:'<img src=x onerror=alert(1)>.csv',total:20000,duplicates:2,invalid:3}]}});
  h.ctx.renderImportProgress('<script>bad</script>');assert.doesNotMatch(h.$('#upload-progress').innerHTML,/<img|<script/);assert.match(h.$('#upload-progress').innerHTML,/無効行 3件/);
});

test('saved collections reopen for appending after restart without starting an analysis',()=>{
  const existing=dataset('saved-200k',200000),calls=[];
  const h=context(['openDatasetForAppend','datasetCardHTML'],{state:{selectionVersion:0,status:{datasets:[existing]}},
    applyDatasetAnalysisDefaults:value=>calls.push(['years',value.id]),updateDataset:()=>{},closeDialogs:()=>{},showStoredDataset:()=>calls.push('stored'),
    showUpload:()=>calls.push('upload'),updateUploadMode:()=>{},runAnalysis:()=>{throw new Error('must not start analysis');}});
  assert.match(h.ctx.datasetCardHTML(existing),/data-dataset-append="saved-200k"/);
  h.ctx.openDatasetForAppend(existing.id);assert.equal(h.ctx.state.dataset.id,existing.id);assert.equal(h.ctx.state.selectionVersion,1);
  assert.deepEqual(calls,[['years','saved-200k'],'stored','upload']);assert.equal(h.$('#csv-after-import').value,'save');
});

const paper=(id,title=id)=>({id,title,abstract:'A full abstract',year:2025,topic_id:'t',citations:7,authors:[],keywords:[]});
function libraryHarness(extra={}){
  const state={result:{id:'large',meta:{papers_truncated:true,papers_total:200000},papers:[paper('sample')],topics:[]},view:'papers',query:'',paperTopic:'all',paperYear:'all',paperSort:'citations',paperPage:0,paperAuthor:null,paperRequest:0,paperCache:new Map()};
  return context(['paperRequestKey','paperPageURL','loadPaperPage','paperTable','getPaper','evidenceList','largeCorpusNotice','topicCitationTotal','topicCitationMean'],{state,
    clamp:(value,a,b)=>Math.max(a,Math.min(b,value)),panelHeader:(title,subtitle,body)=>`${title}${body}`,topicColor:()=>'',topicFor:()=>({}),citationOrigin:()=>'',paperProviders:()=>[],
    filteredPapers:()=>{throw new Error('large corpus must be filtered on the server');},empty:(title,description)=>`${title} ${description}`,...extra});
}

test('large paper library queries the full dataset with paging, filters, stable sort, and bounded rendering',async()=>{
  const requests=[],h=libraryHarness({api:async url=>{requests.push(url);return {items:[paper('remote','<Remote>')],total:200000,offset:20,limit:20};}});
  Object.assign(h.ctx.state,{query:'steel & Ni',paperTopic:'topic/one',paperYear:'2001',paperSort:'year',paperPage:1,paperAuthor:'author:42'});
  await h.ctx.loadPaperPage();const url=new URL(requests[0],'http://localhost');
  assert.equal(url.pathname,'/api/results/large/papers');assert.equal(url.searchParams.get('offset'),'20');assert.equal(url.searchParams.get('limit'),'20');
  assert.equal(url.searchParams.get('query'),'steel & Ni');assert.equal(url.searchParams.get('topic_id'),'topic/one');assert.equal(url.searchParams.get('year'),'2001');assert.equal(url.searchParams.get('author_id'),'author:42');assert.equal(url.searchParams.get('sort'),'year');
  const html=h.$('#papers-table').innerHTML;assert.match(html,/200000 RESULTS/);assert.match(html,/21–40/);assert.match(html,/&lt;Remote&gt;/);assert.doesNotMatch(html,/data-paper="sample"/);
  assert.equal(h.ctx.state.result.papers.length,1);assert.equal(h.ctx.state.paperRemote.items.length,1);
});

test('late library success or failure cannot replace the latest search',async()=>{
  for(const fail of [false,true]){
    let resolve,reject;const h=libraryHarness({api:()=>new Promise((yes,no)=>{resolve=yes;reject=no;})});
    const pending=h.ctx.loadPaperPage();h.ctx.state.query='new query';
    if(fail)reject(new Error('old error'));else resolve({items:[paper('old')],total:1});
    await pending;assert.equal(h.ctx.state.paperRemote,undefined);assert.equal(h.$('#papers-table').innerHTML,undefined);
  }
});

test('paper detail outside the map sample loads individually and does not grow the result corpus',async()=>{
  const requests=[],h=libraryHarness({api:async url=>{requests.push(url);return paper('europepmc:MED:42');}});
  const full=await h.ctx.getPaper('europepmc:MED:42');assert.equal(full.abstract,'A full abstract');
  await h.ctx.getPaper('europepmc:MED:42');assert.deepEqual(requests,['/api/results/large/papers/europepmc%3AMED%3A42']);
  assert.equal(h.ctx.state.result.papers.length,1);
  assert.match(h.ctx.evidenceList(['not-loaded']),/data-paper="not-loaded"/);assert.match(h.ctx.evidenceList(['not-loaded']),/クリックして読み込む/);
  for(let i=0;i<220;i++)await h.ctx.getPaper(`p-${i}`);assert.equal(h.ctx.state.paperCache.size,200);
});

test('large corpus notices and citation values use full analytical metadata rather than displayed papers',()=>{
  const h=libraryHarness({topicFor:()=>({citation_total:120000,citation_known_count:100000,citation_mean:1.2})});
  assert.match(h.ctx.largeCorpusNotice(),/200000論文/);assert.match(h.ctx.largeCorpusNotice(),/マップは表示用の標本/);
  assert.equal(h.ctx.topicCitationTotal('t'),120000);assert.equal(h.ctx.topicCitationMean('t'),'1.2');
  h.ctx.topicFor=()=>({});assert.equal(h.ctx.topicCitationTotal('t'),null);assert.equal(h.ctx.topicCitationMean('t'),'—');
});

test('large field evidence exposes the full count while keeping DOM ids and the displayed list bounded',()=>{
  const h=context(['fieldEvidenceButton','showFieldEvidence','fieldScopeNotice'],{state:{result:{meta:{is_demo:false}}},
    fieldCurrent:()=>true,isSampled:()=>false,addFieldReturnLink:()=>{},openDialog:()=>{},evidenceList:(ids,limit)=>`showing ${Math.min(ids.length,limit)}`});
  const ids=Array.from({length:50},(_,index)=>`p${index}`),html=h.ctx.fieldEvidenceButton(ids,'根拠',200000);
  assert.match(html,/data-evidence-total="200000"/);assert.match(html,/50 \/ 200000件/);
  h.ctx.showFieldEvidence(ids,'根拠',200000);assert.match(h.$('#detail-body').innerHTML,/全200000件/);assert.match(h.$('#detail-body').innerHTML,/最大40件/);assert.match(h.$('#detail-body').innerHTML,/showing 40/);
  assert.match(h.ctx.fieldScopeNotice({display_limits:{evidence_ids:50},scope:{sampled:false}}),/各指標は全対象論文/);
  assert.match(h.ctx.fieldScopeNotice({display_limits:{evidence_ids:50},scope:{sampled:false}}),/最大50件/);
});
