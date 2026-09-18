const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const appSource=fs.readFileSync('static/app.js','utf8');
const source=appSource.slice(appSource.indexOf('function fillSettings()'),appSource.indexOf('function renderStep()'));
function setup(settings={}){
 const dom={},calls=[];
 const context={state:{settings:{provider:'local',base_url:'http://127.0.0.1:1234/v1',model:'preserved',classification_layout:'semantic',ca_bundle:'',bypass_local:true,transformer_model:'',allow_model_download:false,...settings}},
  $:selector=>dom[selector]??=( {value:'',checked:false,textContent:''} ),describeClassLayout:()=>{},updateChrome:()=>{},selectionSave:Promise.resolve(),
  api:async(path,body)=>{calls.push({path,body});return{settings:{...context.state.settings,...body}};}};
 vm.createContext(context);vm.runInContext(source,context);context.fillSettings();return{context,dom,calls};
}
test('legacy settings show the default and existing timeout fills exactly',()=>{
 assert.equal(setup().dom['#llm-timeout'].value,120);
 assert.equal(setup({llm_timeout:300}).dom['#llm-timeout'].value,300);
});
test('timeout saves as a number alongside unchanged connection values',async()=>{
 const {context,dom,calls}=setup();dom['#llm-timeout'].value='300';await context.saveSettings();
 assert.equal(calls.length,1);assert.equal(calls[0].path,'/settings');assert.equal(calls[0].body.llm_timeout,300);
 assert.equal(calls[0].body.model,'preserved');assert.equal(calls[0].body.base_url,'http://127.0.0.1:1234/v1');
 assert.equal(dom['#llm-timeout'].value,300);
});
test('blank, fractional and out-of-range waits cannot send a settings mutation',async()=>{
 for(const value of ['', '29','601','120.5','bad']){
  const {context,dom,calls}=setup();dom['#llm-timeout'].value=value;
  await assert.rejects(context.saveSettings(),/30〜600秒の整数/);assert.equal(calls.length,0);
 }
});
