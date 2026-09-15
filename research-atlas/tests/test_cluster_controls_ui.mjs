import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
const html=readFileSync(new URL('../static/index.html',import.meta.url),'utf8');
function harness(){
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,{value:'',hidden:false,disabled:false,textContent:''});return elements.get(id);};
  const state={kmeansEmbedding:'tfidf',status:{transformer_available:true}},ctx=vm.createContext({$,state,Object,Number,applyDatasetAnalysisDefaults(){}});
  for(const name of ['topicModelName','selectedTopicAvailable','sbertAvailable','syncClusterControls','syncModelControls','readOptions','restoreResultOptions']){const line=source.split('\n').find(row=>row.startsWith(`function ${name}(`));assert.ok(line,name);vm.runInContext(line,ctx);}
  for(const [id,value] of Object.entries({'topic-model':'kmeans','topic-count':'8','embedding':'tfidf','sbert-model':'mpnet','min-topic-size':'7','start-year':'2020','end-year':'2025','forecast-horizon':'3','monthly-window':'3','cluster-eps':'.35','cluster-min_samples':'9','cluster-n_neighbors':'12','cluster-birch_threshold':'.4','cluster-max_clusters':'24'}))$('#'+id).value=value;
  return {ctx,$,state};
}
test('all clustering methods are selectable with accurate names',()=>{
  const h=harness();for(const id of ['kmeans','kmeans_pp','minibatch_kmeans','xmeans','knn_graph','dbscan','gmm','birch','agglomerative','nmf','lda','bertopic']){assert.match(html,new RegExp(`option value="${id}"`));assert.notEqual(h.ctx.topicModelName(id),'undefined');}
});
test('density and graph controls are model-specific and preserve SBERT selection',()=>{
  const h=harness();h.state.kmeansEmbedding='sbert';h.$('#topic-model').value='dbscan';h.ctx.syncModelControls();
  assert.equal(h.$('#embedding').value,'sbert');assert.equal(h.$('#min-topic-size').disabled,false);assert.equal(h.$('#cluster-eps').disabled,false);assert.equal(h.$('#cluster-n_neighbors').disabled,true);assert.equal(h.$('#topic-count').disabled,true);
  let options=h.ctx.readOptions();assert.equal(options.min_topic_size,7);assert.equal(JSON.stringify(options.cluster_options),JSON.stringify({eps:.35,min_samples:9}));
  h.$('#topic-model').value='knn_graph';h.ctx.syncModelControls();options=h.ctx.readOptions();assert.equal(JSON.stringify(options.cluster_options),JSON.stringify({n_neighbors:12}));assert.match(h.$('#topic-model-help').textContent,/教師あり分類のk-NNではありません/);
  h.$('#topic-model').value='nmf';h.ctx.syncModelControls();assert.equal(h.$('#embedding').value,'tfidf');assert.equal(h.$('#cluster-options').hidden,true);assert.equal(JSON.stringify(h.ctx.readOptions().cluster_options),'{}');
});
test('saved clustering model and its relevant options restore without retaining unrelated stale values',()=>{
  const h=harness();h.ctx.restoreResultOptions({meta:{topic_model:'birch',embedding:'tfidf'},options:{cluster_options:{birch_threshold:.6}},frontiers:{}});
  assert.equal(h.$('#topic-model').value,'birch');assert.equal(h.$('#cluster-birch_threshold').value,'0.6');assert.equal(h.$('#cluster-eps').disabled,true);assert.equal(JSON.stringify(h.ctx.readOptions().cluster_options),JSON.stringify({birch_threshold:.6}));
});
