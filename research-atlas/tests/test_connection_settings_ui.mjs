import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../static/connection-settings.js',import.meta.url),'utf8');
const key='research-atlas.connections.v1';
function harness({initial=null,fetchImpl,store}={}) {
  const values=store||new Map(initial===null?[]:[[key,initial]]), elements=new Map(), windowEvents=new Map(), documentEvents=new Map(), sent=[], emitted=[];
  const ids=['connections-summary','connections-dialog','connections-message','connections-origin','connection-proxy-fields','connection-openai-key','connection-openai-model','connection-local-backend','connection-local-url','connection-local-model','connection-local-key','connection-proxy-enabled','connection-proxy-url','connection-proxy-username','connection-proxy-password','connection-no-proxy'];
  for(const id of ids)elements.set(`#${id}`,{id,value:'',checked:false,open:false,hidden:false,disabled:false,textContent:'',addEventListener(){},setAttribute(){},showModal(){this.open=true;},close(){this.open=false;}});
  const localStorage={getItem:name=>values.get(name)??null,setItem:(name,value)=>values.set(name,value),removeItem:name=>values.delete(name)};
  const window={__ATLAS_UI_TEST__:true,localStorage,fetch:async(...args)=>{sent.push(args);return fetchImpl?fetchImpl(...args):{ok:true,json:async()=>({ok:true,message:'接続できました'})};},addEventListener:(name,handler)=>windowEvents.set(name,handler),dispatchEvent:event=>emitted.push(event)};
  const document={querySelector:selector=>elements.get(selector)||null,querySelectorAll:()=>[],addEventListener:(name,handler)=>documentEvents.set(name,handler)};
  const sandbox=vm.createContext({window,document,location:{href:'http://127.0.0.1:8000/',origin:'http://127.0.0.1:8000'},URL,Headers,TextEncoder,btoa,CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}},console});
  vm.runInContext(source,sandbox);
  return {api:window.AtlasConnections,t:window.__connectionSettingsTest,window,values,elements,windowEvents,documentEvents,sent,emitted};
}
const plain=value=>JSON.parse(JSON.stringify(value));
function configured(h) {const settings=h.t.defaults();settings.openai={api_key:'fake-key-for-test-only',model:'fake-model'};settings.proxy={enabled:true,url:'http://proxy.test:8080',username:'研究者',password:'fake-pass-only',no_proxy:'localhost,127.0.0.1,::1'};return settings;}

test('save persists browser-only settings and reload restores Unicode exactly',()=>{
  const h=harness(),settings=configured(h);h.t.save(settings);const next=harness({store:h.values});
  assert.deepEqual(plain(next.api.get()),plain(settings));assert.equal(h.values.size,1);assert.equal(h.sent.length,0);
  assert.deepEqual(plain(h.emitted[0].detail),{revision:1,reason:'save'});assert.ok(!JSON.stringify(h.emitted).includes('fake-key'));
  const copy=next.api.get();copy.openai.api_key='edited';assert.equal(next.api.get().openai.api_key,'fake-key-for-test-only');
});
test('cancel discards draft edits and saving does not run analysis',()=>{
  const h=harness();h.t.save(configured(h));h.api.open();h.elements.get('#connection-openai-key').value='unsaved-fake-key';h.t.close();h.api.open();
  assert.equal(h.elements.get('#connection-openai-key').value,'fake-key-for-test-only');assert.equal(h.sent.length,0);
  assert.equal(h.elements.get('#connections-dialog').open,true);
});
test('clear removes stored secrets and later API requests carry only blank credentials',async()=>{
  const h=harness();h.t.save(configured(h));h.t.clear();assert.equal(h.values.has(key),false);assert.equal(h.api.get().openai.api_key,'');
  await h.api.fetch('/api/status');const encoded=h.sent[0][1].headers.get('X-Atlas-Connection');const payload=JSON.parse(Buffer.from(encoded,'base64').toString('utf8'));
  assert.equal(payload.openai.api_key,'');assert.equal(payload.proxy.enabled,false);assert.equal(h.emitted.at(-1).detail.reason,'clear');
});
test('malformed saved data gives a safe message and never reuses invalid credentials',()=>{
  const h=harness({initial:'{"api_key":"fake-secret-corrupted"'});assert.equal(h.api.get().openai.api_key,'');assert.match(h.t.storageError,/読み込めません/);
  h.api.open();assert.doesNotMatch(h.elements.get('#connections-message').textContent,/fake-secret/);assert.equal(h.sent.length,0);
});
test('storage failure preserves the prior settings and emits no changed event',()=>{
  const h=harness();const first=configured(h);h.t.save(first);const update=configured(h);update.openai.api_key='second-fake';h.window.localStorage.setItem=()=>{throw new Error('storage full');};
  assert.throws(()=>h.t.save(update),/保存できません/);assert.equal(h.api.get().openai.api_key,first.openai.api_key);assert.equal(h.emitted.length,1);
  h.window.localStorage.removeItem=()=>{throw new Error('disabled');};assert.throws(()=>h.t.clear(),/削除できません/);assert.equal(h.api.get().openai.api_key,first.openai.api_key);
});
test('storage unavailability does not block normal API calls or silently persist',async()=>{
  const h=harness();h.window.localStorage.getItem=()=>{throw new Error('blocked');};h.t.load();assert.match(h.t.storageError,/保存設定/);
  await h.api.fetch('/api/status');assert.equal(h.sent.length,1);assert.equal(h.api.get().openai.api_key,'');
});
test('only same-origin API fetches get the header; FormData and caller headers survive',async()=>{
  const h=harness();h.t.save(configured(h));const body=new FormData();body.append('file','test');
  await h.api.fetch('/api/import',{method:'POST',headers:{'X-Request-ID':'test'},body});
  const options=h.sent[0][1];assert.equal(options.body,body);assert.equal(options.headers.get('Content-Type'),null);assert.equal(options.headers.get('X-Request-ID'),'test');assert.equal(options.redirect,'error');
  const payload=JSON.parse(Buffer.from(options.headers.get('X-Atlas-Connection'),'base64').toString('utf8'));assert.equal(payload.proxy.username,'研究者');
  for(const path of ['https://example.com/api/status','http://localhost:8000/api/status','/static/app.js','/api-not-a-route'])await h.api.fetch(path,{headers:{Accept:'text/plain'}});
  for(const [,externalOptions] of h.sent.slice(1))assert.equal(new Headers(externalOptions.headers).has('X-Atlas-Connection'),false);
});
test('connection test sends the unsaved draft only to the test endpoint',async()=>{
  const h=harness();h.api.open();h.elements.get('#connection-openai-key').value='fake-draft-key';h.elements.get('#connection-openai-model').value='fake-model';
  await h.t.testConnection('openai');const [url,options]=h.sent[0];assert.equal(url,'/api/connections/test');assert.deepEqual(JSON.parse(options.body),{target:'openai'});
  assert.equal(JSON.parse(Buffer.from(options.headers.get('X-Atlas-Connection'),'base64').toString('utf8')).openai.api_key,'fake-draft-key');
  assert.equal(h.api.get().openai.api_key,'');assert.equal(h.values.size,0);assert.match(h.elements.get('#connections-message').textContent,/接続できました/);
});
test('late test replies cannot overwrite status after closing or changing the draft',async()=>{
  let release;const h=harness({fetchImpl:async()=>{await new Promise(resolve=>release=resolve);return {ok:true,json:async()=>({message:'stale response'})};}});
  h.api.open();const pending=h.t.testConnection('local');await new Promise(resolve=>setImmediate(resolve));h.t.close();h.api.open();release();await pending;
  assert.notEqual(h.elements.get('#connections-message').textContent,'stale response');
});
test('another tab save and global storage clear update settings without credentials in events',()=>{
  const h=harness();h.api.open();const settings=configured(h);h.values.set(key,JSON.stringify(settings));h.windowEvents.get('storage')({key,storageArea:h.window.localStorage});
  assert.equal(h.api.get().openai.api_key,settings.openai.api_key);assert.equal(h.elements.get('#connection-openai-key').value,settings.openai.api_key);assert.match(h.elements.get('#connections-message').textContent,/別タブ/);
  h.values.clear();h.windowEvents.get('storage')({key:null,storageArea:h.window.localStorage});assert.equal(h.api.get().openai.api_key,'');assert.equal(h.emitted.at(-1).detail.reason,'storage');
  assert.doesNotMatch(JSON.stringify(h.emitted),/fake-key/);
});
test('invalid URL, embedded credentials, control characters and oversized headers are rejected',()=>{
  const h=harness();for(const change of [s=>s.local.url='https://external.test',s=>s.proxy.url='http://user:pass@proxy.test',s=>s.proxy.url='http://proxy.test/path',s=>s.openai.api_key='fake\nkey']){const s=configured(h);change(s);assert.throws(()=>h.t.save(s));}
  const huge=configured(h);huge.openai.api_key='x'.repeat(4096);huge.local.api_key='x'.repeat(4096);huge.proxy.password='x'.repeat(4096);assert.throws(()=>h.t.save(huge),/長すぎる/);assert.throws(()=>h.t.encode(huge),/合計サイズ/);
});
test('server-invalid model, secret and proxy exclusion values cannot be persisted',()=>{
  const h=harness();h.t.save(configured(h));const prior=h.values.get(key);
  for(const change of [s=>s.openai.model='bad model',s=>s.local.model='日本語',s=>s.openai.model='m'.repeat(161),s=>s.openai.api_key='x'.repeat(2049),s=>s.local.api_key='x'.repeat(2049),s=>s.proxy.username='x'.repeat(2049),s=>s.proxy.password='x'.repeat(2049),s=>s.proxy.no_proxy='https://example.com',s=>s.proxy.no_proxy='host:65536',s=>s.proxy.no_proxy='10.0.0.0/33',s=>s.proxy.no_proxy='[::1]:0',s=>s.proxy.no_proxy='a..b',s=>s.local.url='http://127.0.0.1:0']) {
    const candidate=configured(h);change(candidate);assert.throws(()=>h.t.save(candidate));assert.equal(h.values.get(key),prior);
  }
});
test('server-invalid stored settings fall back to usable defaults without leaking into requests',async()=>{
  const h=harness();const bad=configured(h);bad.openai.model='bad model';h.values.set(key,JSON.stringify(bad));h.t.load();assert.match(h.t.storageError,/保存設定/);
  await h.api.fetch('/api/status');const payload=JSON.parse(Buffer.from(h.sent[0][1].headers.get('X-Atlas-Connection'),'base64').toString('utf8'));assert.equal(payload.openai.api_key,'');assert.equal(payload.openai.model,'');
});
test('URL normalization and supported proxy host, IP, CIDR, suffix and port rules match server transport',()=>{
  const h=harness(),settings=configured(h);settings.local.url='http://127.1:11434/';settings.proxy.url='HTTP://PROXY.TEST:8080/';settings.proxy.no_proxy=' localhost , 127.0.0.1, ::1, .example.com, *.example.org:443, 10.0.0.0/8, 192.168.0.0/255.255.0.0, [::1]:11434, 2001:db8::/32, * ';
  const normalized=h.t.save(settings);assert.equal(normalized.local.url,'http://127.0.0.1:11434');assert.equal(normalized.proxy.url,'http://proxy.test:8080');assert.equal(normalized.proxy.no_proxy,'localhost,127.0.0.1,::1,.example.com,*.example.org:443,10.0.0.0/8,192.168.0.0/255.255.0.0,[::1]:11434,2001:db8::/32,*');
});
test('settings markup is separate from analytical submit and credential fields are masked',()=>{
  const html=readFileSync(new URL('../static/index.html',import.meta.url),'utf8'),app=readFileSync(new URL('../static/app.js',import.meta.url),'utf8'),foresight=readFileSync(new URL('../static/foresight.js',import.meta.url),'utf8');
  assert.ok(html.indexOf('/static/connection-settings.js')<html.indexOf('/static/app.js'));assert.match(html,/id="connections-form"/);assert.match(html,/data-connection-open/);
  for(const id of ['connection-openai-key','connection-local-key','connection-proxy-password'])assert.match(html,new RegExp(`id="${id}" type="password"`));
  assert.doesNotMatch(html+app+foresight,/\.env/);assert.match(app,/AtlasConnections\?\.fetch/);assert.match(foresight,/AtlasConnections\?\.fetch/);
  assert.match(app,/request!==fieldState\.llmRequest/);assert.match(foresight,/token!==ui\.statusRequest/);
});
