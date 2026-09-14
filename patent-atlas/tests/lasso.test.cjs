const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js','utf8');
const functions = source.slice(source.indexOf('function enableLasso('), source.indexOf('function renderQuery('));
function setup() {
  const events={}, selected=[], paths=[];
  const nodes=[{id:'inside',x:30,y:30},{id:'outside',x:130,y:130}].map(n=>({...n,tagName:'circle',getBoundingClientRect:()=>({x:n.x-5,y:n.y-5,width:10,height:10})}));
  const svg={dataset:{},capture:false,getScreenCTM:()=>({inverse:()=>({})}),addEventListener:(type,fn)=>events[type]=fn,setPointerCapture(){this.capture=true;},hasPointerCapture(){return this.capture;},releasePointerCapture(){this.capture=false;}};
  let completed=0;
  const context={tool:'lasso',$:()=>({setAttribute:(name,value)=>paths.push(value)}),$$:()=>nodes,setTimeout:()=>{},DOMPoint:class{constructor(x,y){this.x=x;this.y=y;}matrixTransform(){return this;}}};
  vm.createContext(context);vm.runInContext(functions,context);
  context.enableLasso(svg,'path','.point',n=>selected.push(n.id),()=>completed++);
  const pointer=(type,x,y)=>events[type]({clientX:x,clientY:y,button:0,pointerId:1});
  return {context,svg,selected,paths,pointer,completed:()=>completed};
}
test('closed freehand lasso selects enclosed nodes only',()=>{
  const s=setup();s.pointer('pointerdown',10,10);
  for(const [x,y] of [[70,10],[70,70],[10,70],[10,10]])s.pointer('pointermove',x,y);
  s.pointer('pointerup',10,10);
  assert.deepEqual(s.selected,['inside']);assert.equal(s.completed(),1);assert.equal(s.paths.at(-1),'');assert.equal(s.svg.capture,false);
});
test('a click is not captured, so node click selection remains available',()=>{
  const s=setup();s.pointer('pointerdown',30,30);assert.equal(s.svg.capture,false);s.pointer('pointerup',30,30);assert.deepEqual(s.selected,[]);assert.equal(s.completed(),0);
});
test('pointer cancellation clears an unfinished selection without changing nodes',()=>{
  const s=setup();s.pointer('pointerdown',10,10);s.pointer('pointermove',70,10);s.pointer('pointercancel',70,10);assert.deepEqual(s.selected,[]);assert.equal(s.svg.dataset.drawing,undefined);assert.equal(s.paths.at(-1),'');
});
test('concave polygon does not select a point in its notch',()=>{
  const s=setup(), poly=[{x:0,y:0},{x:100,y:0},{x:100,y:100},{x:50,y:40},{x:0,y:100}];
  assert.equal(s.context.insidePolygon({x:20,y:20},poly),true);assert.equal(s.context.insidePolygon({x:50,y:80},poly),false);
});
