import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../static/map-terrain.js', import.meta.url), 'utf8');
const window = {};
vm.runInNewContext(source, {window});
const terrain = window.AtlasTerrain;
const plain = value => JSON.parse(JSON.stringify(value));
const options = {width:800, height:500, pad:48, heightScale:65, mode:'relief'};
const fixture = () => ({version:1, grid:{width:3, height:3, values:[0,.25,0,.25,1,.25,0,.25,0]}, contours:[{level:.5, paths:[[[.5,.15],[.85,.5],[.5,.85],[.15,.5],[.5,.15]]]}], node_heights:{center:1}});

test('flat projection retains the exact original paper map at any density', () => {
  for (const z of [0, .6, 1]) {
    assert.deepEqual(plain(terrain.project(0, 0, z, {...options, mode:'flat'})), [48,48]);
    assert.deepEqual(plain(terrain.project(1, 1, z, {...options, mode:'flat'})), [752,452]);
    assert.deepEqual(plain(terrain.project(.5, .5, z, {...options, mode:'flat'})), [400,250]);
  }
});

test('raised papers and equal density contours share the same relief projection', () => {
  const base = terrain.project(.5,.15,0,options), raised = terrain.project(.5,.15,.5,options);
  assert.equal(raised[0],base[0]);
  assert.ok(Math.abs(base[1] - raised[1] - 32.5) < 1e-10);
  const html = terrain.render(fixture(),options);
  const rounded = raised.map(value => Math.round(value*100)/100).join(',');
  assert.ok(html.includes(`d="M${rounded}L`));
});

test('projection is bounded at all corners including high exaggeration and small views', () => {
  for (const height of [80,350,500]) {
    for (const x of [0,1]) for (const y of [0,1]) for (const z of [0,1]) {
      const [px,py] = terrain.project(x,y,z,{...options,height,heightScale:99999});
      assert.ok(px >= 0 && px <= 800);
      assert.ok(py >= 0 && py <= height);
    }
  }
  for (const v of terrain.project(NaN,Infinity,undefined,{width:NaN,height:undefined,pad:Infinity,heightScale:NaN,mode:'relief'})) assert.ok(Number.isFinite(v));
});

test('bilinear density sampling is continuous at cell boundaries and clamps edges', () => {
  const grid = {grid:{width:2,height:2,values:[0,1,1,0]}};
  assert.equal(terrain.heightAt(grid,.5,.5),.5);
  assert.equal(terrain.heightAt(grid,0,0),0);
  assert.equal(terrain.heightAt(grid,1,0),1);
  assert.equal(terrain.heightAt(grid,20,-3),1);
  const a = terrain.heightAt(fixture(),.5-1e-8,.5), b = terrain.heightAt(fixture(),.5+1e-8,.5);
  assert.ok(Math.abs(a-b) < 1e-7);
  assert.equal(terrain.heightAt(null,.4,.5),0);
});

test('empty or malformed grids do not create a fictitious landscape', () => {
  for (const value of [null,{}, {grid:{width:1,height:1,values:[1]}}, {grid:{width:2,height:2,values:[1]}}, {grid:{width:2,height:2,values:[0,0,0,0]}}]) assert.equal(terrain.render(value,options),'');
  assert.equal(terrain.render({grid:{width:500,height:500,values:Array(250000).fill(1)}},options),'');
});

test('flat, relief, and contour visibility modes leave hit testing to existing paper nodes', () => {
  const relief = terrain.render(fixture(),options), flat = terrain.render(fixture(),{...options,mode:'flat'}), hidden = terrain.render(fixture(),{...options,contours:false});
  assert.match(relief,/class="terrain-floor"/);
  assert.doesNotMatch(flat,/class="terrain-floor"/);
  assert.match(flat,/class="terrain-contours"/);
  assert.doesNotMatch(hidden,/class="terrain-contours"/);
  for (const html of [relief,flat,hidden]) {
    assert.match(html,/aria-hidden="true" focusable="false" pointer-events="none"/);
    assert.doesNotMatch(html,/tabindex|data-paper|onclick|role="button"/);
  }
});

test('untrusted labels and malformed contour segments cannot create SVG markup or bridges', () => {
  const data = fixture();
  data.meta = {label:'<script>alert(1)</script>'};
  data.contours = [{level:.5,paths:[[[0,0],[.1,.1],['bad',.2],[.8,.8],[1,1]]]}, {level:'"><script>',paths:[[[0,0],[1,1]]]}];
  const html = terrain.render(data,{...options,mode:'flat',idPrefix:'"><script>alert(1)</script>'});
  assert.doesNotMatch(html,/script|alert|NaN|undefined|Infinity/);
  assert.match(html,/d="M48,48L118\.4,88\.4M611\.2,371\.2L752,452"/);
});

test('rendering is deterministic and never changes result JSON', () => {
  const data = fixture(), before = JSON.stringify(data);
  for (const contour of data.contours) {contour.paths.forEach(points => {points.forEach(Object.freeze);Object.freeze(points);});Object.freeze(contour.paths);Object.freeze(contour);}
  Object.freeze(data.grid.values);Object.freeze(data.grid);Object.freeze(data);
  assert.equal(terrain.render(data,options),terrain.render(data,options));
  assert.equal(JSON.stringify(data),before);
});

test('full production grid stays within the 3200 face rendering budget', () => {
  const data = {grid:{width:49,height:33,values:Array.from({length:49*33},(_,i)=>i/(49*33))},contours:[]};
  const html = terrain.render(data,options);
  const faces = html.match(/<path d=/g) || [];
  assert.equal(faces.length,3072);
  assert.ok(html.length < 600000);
});

test('repeated contour vertices and nonfinite density values stay finite', () => {
  const data = fixture();
  data.grid.values[0] = NaN; data.grid.values[1] = Infinity; data.grid.values[8] = -5;
  data.contours.push({level:.5,paths:[[[1,1],[1,1],[1,1]]]});
  assert.doesNotMatch(terrain.render(data,options),/NaN|undefined|Infinity/);
});
