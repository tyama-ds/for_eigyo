import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
function harness(){
  const events=[],elements=new Map();
  const $=selector=>{if(!elements.has(selector))elements.set(selector,{textContent:'',hidden:false,set innerHTML(value){events.push(['html',selector,value]);}});return elements.get(selector);};
  const api=async()=>{},showPaper=()=>{};
  const values={state:{view:'citation-flow',result:{id:'a'.repeat(32),meta:{}}},$, $$:()=>[],api,showPaper,e:String,num:String,icon:()=>'',URLSearchParams,
    largeCorpusNotice:()=>'',renderOverview:()=>'<p>overview</p>',renderTechnology:()=>'',renderKeywords:()=>'',renderAuthors:()=>'',renderCitations:()=>'',renderForecasts:()=>'',renderPapers:()=>'',
    window:{AtlasCitationFlow:{unmount(){events.push(['unmount']);},mount(root,result,context){events.push(['mount',root,result,context]);}}}};
  const context=vm.createContext(values);
  for(const prefix of ['const viewLabels=','function render(){','function showLoading(','function parseRestoreLink(']){
    const line=source.split('\n').find(line=>line.startsWith(prefix));assert.ok(line,prefix);vm.runInContext(line,context);
  }
  return {context,events,$,api,showPaper};
}

test('separate citation view mounts with browser-aware API and is disposed before leaving',()=>{
  const h=harness();h.context.render();
  assert.equal(h.events[0][0],'unmount');
  assert.match(h.events.find(event=>event[0]==='html')[2],/citation-flow-root/);
  const mounted=h.events.find(event=>event[0]==='mount');
  assert.equal(mounted[1],h.$('#citation-flow-root'));
  assert.equal(mounted[3].api,h.api);assert.equal(mounted[3].showPaper,h.showPaper);
  h.events.length=0;h.context.state.view='overview';h.context.render();
  assert.equal(h.events[0][0],'unmount');assert.equal(h.events.some(event=>event[0]==='mount'),false);
  h.events.length=0;h.context.showLoading('new analysis');assert.equal(h.events[0][0],'unmount');
});

test('saved analysis can restore directly into citation flow without another assessment',()=>{
  const h=harness(),id='a'.repeat(32);
  const restored=h.context.parseRestoreLink(`?result_id=${id}&view=citation-flow`);
  assert.equal(restored.resultId,id);assert.equal(restored.view,'citation-flow');
  assert.equal(restored.assessmentId,null);
});
