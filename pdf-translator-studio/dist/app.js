import { loadPdf, renderTranslatedCanvas, exportPdf, createDemoPdf } from './pdf-engine.js';
import { createPageStore } from './page-store.js';
import { exportOffice } from './office-export.js';
import { translateBlocks } from './translator.js';
import { createTranslationJob, getPendingJobBlocks, applyTranslationBatch } from './translation-job.js';

const $ = id => document.getElementById(id);
const state = { doc:null, store:null, pages:[], current:0, selected:null, busy:false, controller:null, filename:'', hash:'', config:null, renderVersion:0, previewPromise:Promise.resolve() };
const proxyState = { loaded:false, loading:false, saving:false, passwordConfigured:false, noProxyInitialized:false };
const translationSettingsState = { value:null, loading:false, saving:false };
const translationSettingFields = { localTimeoutSeconds:'local-timeout-seconds', openaiTimeoutSeconds:'openai-timeout-seconds', localBatchBlocks:'local-batch-blocks', localBatchCharacters:'local-batch-characters' };
let toastTimer, renderTimer;
const allBlocks = () => state.pages.flatMap(p => p.blocks);
const currentPage = () => state.pages[state.current];
const selectedBlock = () => currentPage()?.blocks.find(b => b.id === state.selected);
function notify(message, error = false) { $('toast').textContent = message; $('toast').classList.toggle('error',error); $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=> $('toast').hidden=true,error?10000:5000); }
function status(message) { $('status').textContent=message; }
function busy(value, message) {
  state.busy=value; document.body.classList.toggle('busy',value);
  for(const id of ['upload-button','demo-button','empty-upload','translate-button','export-button','export-format','save-project','restore-project','provider','model','source-language','target-language','scope','file-input','api-mode','reasoning','page-jump','page-jump-button','zoom']) $(id).disabled=value || (['translate-button','export-button','save-project','restore-project','page-jump','page-jump-button'].includes(id)&&!state.pages.length);
  $('block-editor').querySelectorAll('input,select,textarea,button').forEach(el=>el.disabled=value);
  $('page-list').querySelectorAll('button').forEach(el=>el.disabled=value);
  $('progress').hidden=!value; if(!value)$('progress').value=0;
  updateProxyControls();
  updateTranslationSettingsControls();updateResumeControls();$('skip-translated').disabled=value;
  if(message)status(message);
}
function download(blob, name) { const url=URL.createObjectURL(blob); const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000); }

async function getConfig({preserveSelection=false}={}) {
  try {const response=await fetch('/api/config',{cache:'no-store'}); if(!response.ok)throw Error(); state.config=await response.json(); updateProvider({preserveSelection});return true;}
  catch { $('provider-note').textContent='ローカルサーバー未接続';$('connection-details').textContent='node launch.mjs で起動してください。PDFの表示・手動編集は利用できます。';return false; }
}
function updateProvider({preserveSelection=false}={}) {
  const provider=$('provider').value;
  const config=state.config?.[provider];
  if(!preserveSelection)$('model').value=config?.model || '';
  $('provider-note').textContent=config?`${config.baseUrl}${provider==='openai'&&!config.configured?' · APIキー未設定':''}`:'接続設定が取得できません';
  $('send-note').textContent=provider==='openai'?'翻訳する文字をOpenAIへ送信します。保存されたプロキシ設定を使用します。':`翻訳する文字を社内の接続先へ送信します。${state.config?.proxy?.useForLocal?'保存されたプロキシ設定を使用します。':'プロキシを迂回します。'}`;
  if(provider==='openai'&&!preserveSelection) $('api-mode').value=config?.apiMode||'auto';
  if(state.config) {
    const container=$('connection-details');container.replaceChildren();
    for (const line of [`社内LLM：${state.config.local.baseUrl}`,`OpenAI：${state.config.openai.baseUrl}`,`APIキー：${state.config.openai.configured?'設定済み':'未設定'}`,`外部プロキシ：${state.config.proxy.configured?'設定あり':'未設定（直接接続）'}`]){const p=document.createElement('div');p.textContent=line;container.append(p);}
  }
  updateResumeControls();
}
function translationConfig(){
  const config={provider:$('provider').value,model:$('model').value.trim(),sourceLanguage:$('source-language').value,targetLanguage:$('target-language').value,apiMode:$('provider').value==='openai'?$('api-mode').value:'chat'};
  if(config.provider==='openai'&&$('reasoning').value)config.reasoningEffort=$('reasoning').value;
  return config;
}
function translationContext(config=translationConfig()){
  const scope=$('scope').value;
  return JSON.stringify({hash:state.hash,...config,endpoint:state.config?.[config.provider]?.baseUrl,scope,page:scope==='page'?currentPage()?.pageNumber:null,selected:scope==='selected'?state.selected:null});
}
function updateResumeControls(){
  const job=state.translationJob,pending=job?job.total-job.completed.size:0,compatible=job?.contextKey===translationContext();
  $('resume-translation').hidden=!pending;$('resume-note').hidden=!pending;
  $('resume-translation').disabled=state.busy||!compatible;
  $('resume-translation').textContent=`続きから翻訳（残り${pending}領域）`;
  $('resume-note').textContent=compatible?`${job.completed.size}領域の完了分を保持しています。画面を閉じる前に「編集データ保存」で保存できます。`:'翻訳先・モデル・言語・範囲を元に戻すと再開できます。訳文がある領域をスキップして新たに翻訳することもできます。';
}
function clearTranslationJob(){state.translationJob=null;updateResumeControls();}
function translationSettingsStatus(message,error=false){$('translation-settings-status').textContent=message;$('translation-settings-status').classList.toggle('error',error);}
function updateTranslationSettingsControls(){
  const locked=state.busy||translationSettingsState.loading||translationSettingsState.saving;
  for(const id of Object.values(translationSettingFields))$(id).disabled=locked||!translationSettingsState.value;
  $('translation-settings-save').disabled=locked||!translationSettingsState.value;
  $('translation-settings-reload').disabled=locked;
}
function applyTranslationSettings(value){
  if(!value||Object.keys(translationSettingFields).some(key=>!Number.isInteger(value[key])))throw Error('翻訳設定の応答を読み取れませんでした。サーバーを最新版で起動してください。');
  translationSettingsState.value={...value};for(const [key,id] of Object.entries(translationSettingFields))$(id).value=value[key];
}
async function translationSettingsResponse(response){const data=await response.json().catch(()=>null);if(!response.ok)throw Error(data?.error||'翻訳設定を取得できません。サーバーを最新版で起動してください。');return data;}
async function loadTranslationSettings(){
  if(translationSettingsState.loading||translationSettingsState.saving)return false;
  translationSettingsState.loading=true;translationSettingsStatus('翻訳設定を読み込み中…');$('translation-settings-reload').hidden=true;updateTranslationSettingsControls();
  try{applyTranslationSettings(await translationSettingsResponse(await fetch('/api/translation-settings',{cache:'no-store'})));translationSettingsStatus('保存済みの設定を読み込みました。');return true;}
  catch(error){translationSettingsStatus(error.message,true);$('translation-settings-reload').hidden=false;return false;}
  finally{translationSettingsState.loading=false;updateTranslationSettingsControls();}
}
async function saveTranslationSettings(){
  if(state.busy||translationSettingsState.loading||translationSettingsState.saving||!translationSettingsState.value)return;
  for(const id of Object.values(translationSettingFields))if(!$(id).reportValidity())return;
  const value=Object.fromEntries(Object.entries(translationSettingFields).map(([key,id])=>[key,Number($(id).value)]));
  translationSettingsState.saving=true;busy(true);translationSettingsStatus('翻訳設定を保存中…');
  try{applyTranslationSettings(await translationSettingsResponse(await fetch('/api/translation-settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)})));translationSettingsStatus('保存しました。次の翻訳・再開から反映されます。');}
  catch(error){translationSettingsStatus(error.message,true);}
  finally{translationSettingsState.saving=false;busy(false);renderInspector();}
}
function proxyStatus(message,error=false){$('proxy-status').textContent=message;$('proxy-status').classList.toggle('error',error);}
function updateProxyControls(){
  const mode=$('proxy-mode').value,custom=mode==='custom';
  const locked=state.busy||proxyState.loading||proxyState.saving||!proxyState.loaded;
  $('proxy-mode').disabled=locked;$('proxy-save').disabled=locked;
  $('proxy-custom-fields').hidden=!custom;$('proxy-custom-fields').disabled=locked||!custom;
  $('proxy-password').disabled=locked||!custom||$('proxy-clear-password').checked;
  $('proxy-reload').disabled=state.busy||proxyState.loading||proxyState.saving;
  $('proxy-save').textContent=proxyState.saving?'保存中…':'プロキシ設定を保存';
  $('proxy-mode-note').textContent=!proxyState.loaded?'プロキシ設定を読み込んでから変更できます。':mode==='custom'?'下の設定を保存すると、次の通信から反映されます。再起動は不要です。':mode==='direct'?'保存すると、OpenAI・ローカルLLMともプロキシを使わず直接接続します。':'サーバー起動時に読み込まれた環境変数・.env のプロキシ設定を使用します。';
  $('proxy-password-note').textContent=proxyState.passwordConfigured?'パスワードは保存済みです。空欄で保存すると現在のパスワードを保持します。':'保存済みパスワードはありません。';
}
function applyProxySettings(settings){
  $('proxy-mode').value=settings.mode;$('proxy-url').value=settings.url||'';$('proxy-no-proxy').value=settings.noProxy||'';$('proxy-username').value=settings.username||'';
  $('proxy-password').value='';$('proxy-clear-password').checked=false;$('proxy-use-for-local').checked=Boolean(settings.useForLocal);
  proxyState.passwordConfigured=Boolean(settings.passwordConfigured);proxyState.loaded=true;
  proxyState.noProxyInitialized=settings.mode==='custom'||Boolean(settings.noProxy);
}
async function readProxyResponse(response){
  const data=await response.json().catch(()=>null);
  if(!response.ok)throw Error(data?.error||'プロキシ設定にアクセスできません。アプリのサーバーを確認してください。');
  if(!data||!['custom','direct','environment'].includes(data.mode))throw Error('プロキシ設定の応答を読み取れませんでした。アプリのサーバーを確認してください。');
  return data;
}
async function loadProxySettings(){
  if(proxyState.loading||proxyState.saving)return;
  proxyState.loading=true;proxyStatus('プロキシ設定を読み込み中…');$('proxy-reload').hidden=true;updateProxyControls();
  try{applyProxySettings(await readProxyResponse(await fetch('/api/proxy-settings',{cache:'no-store'})));proxyStatus('保存済みの設定を読み込みました。');}
  catch(error){proxyStatus(error.message,true);$('proxy-reload').hidden=false;}
  finally{proxyState.loading=false;updateProxyControls();}
}
async function saveProxySettings(){
  if(state.busy||proxyState.loading||proxyState.saving||!proxyState.loaded)return;
  const payload={mode:$('proxy-mode').value,url:$('proxy-url').value.trim(),noProxy:$('proxy-no-proxy').value.trim(),username:$('proxy-username').value,password:$('proxy-clear-password').checked?'':$('proxy-password').value,clearPassword:$('proxy-clear-password').checked,useForLocal:$('proxy-use-for-local').checked};
  proxyState.saving=true;busy(true);proxyStatus('プロキシ設定を保存中…');
  try{
    const settings=await readProxyResponse(await fetch('/api/proxy-settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}));
    applyProxySettings(settings);
    const refreshed=await getConfig({preserveSelection:true});
    proxyStatus(refreshed?'保存しました。次の通信から反映されます。再起動は不要です。':'保存しました。接続状況の再取得に失敗しましたが、設定は次の通信から反映されます。',!refreshed);
  }catch(error){proxyStatus(error.message,true);}
  finally{proxyState.saving=false;busy(false);renderInspector();}
}
function requestPassword() {
  return new Promise(resolve=>{
    const dialog=$('password-dialog');$('pdf-password').value='';dialog.showModal();
    const close=()=>{cleanup();resolve(null);};
    const submit=e=>{e.preventDefault();const pw=$('pdf-password').value;cleanup();dialog.close();resolve(pw);};
    const cancel=()=>{cleanup();dialog.close();resolve(null);};
    function cleanup(){dialog.removeEventListener('cancel',close);$('password-form').removeEventListener('submit',submit);$('password-cancel').removeEventListener('click',cancel);}
    dialog.addEventListener('cancel',close);$('password-form').addEventListener('submit',submit);$('password-cancel').addEventListener('click',cancel);
  });
}
async function readFile(file) {
  if(!file||state.busy)return;
  if(!file.name.toLowerCase().endsWith('.pdf'))return notify('PDFファイルを選択してください。',true);
  if(file.size>30*1024*1024)return notify('30MB以内のPDFを選択してください。',true);
  busy(true,'PDFファイルを読み込み中…');
  try{await openDocument(await file.arrayBuffer(),file.name,false);}
  catch(error){notify(error.message,true);status('PDFファイルを読み込めませんでした。');}
  finally{busy(false);$('file-input').value='';}
}
async function openDocument(bytes, name, demo=false) {
  busy(true,'PDFの文字とレイアウトを読み込み中…');
  clearTimeout(renderTimer);
  state.renderVersion++;
  await state.previewPromise;
  let doc, store;
  try {
    const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(x=>x.toString(16).padStart(2,'0')).join('');
    let password;
    for(let attempt=0;attempt<3;attempt++) {
      try {doc=await loadPdf(bytes.slice(0),{password});break;}
      catch(error){if(error.name!=='PasswordException')throw error;password=await requestPassword();if(password===null)throw Error('PDFの読み込みを中止しました。');}
    }
    if(!doc)throw Error('パスワードが一致しません。PDFを再度選択してください。');
    store=await createPageStore(doc,{onProgress:(done,total)=>{
      $('progress').value=done/total*100;status(`ページを確認中 ${done} / ${total} ページ`);
    }});
    await store.ensurePage(0);
    state.renderVersion++;
    if(state.store)await state.store.dispose();
    state.doc=doc;state.store=store;state.pages=store.pages;state.current=0;state.selected=null;state.filename=name;state.hash=hash;
    clearTranslationJob();
    $('filename').textContent=name;$('sample-tag').hidden=!demo;$('empty-state').hidden=true;$('preview').hidden=false;
    $('page-total').textContent=state.pages.length;$('page-jump').max=state.pages.length;renderPages();updateExportFormat();
    if(demo)applySampleTranslation();
    await showPage(0,{manageBusy:false});
    if(!currentPage().blocks.length)notify('このページから文字を抽出できませんでした。画像だけのページはOCR処理が必要です。',true);
  } catch(error) { if(store&&store!==state.store)await store.dispose();else if(doc&&doc!==state.doc)await doc.loadingTask?.destroy();notify(error.message,true);status('読み込みを完了できませんでした。'); }
  finally{busy(false);$('file-input').value='';}
}
function renderPages(){const list=$('page-list');list.replaceChildren();state.pages.forEach((p,i)=>{const button=document.createElement('button');button.className=`page-button${i===state.current?' active':''}`;button.textContent=String(i+1).padStart(2,'0');button.title=p.loaded?`${i+1}ページ · ${p.blocks.length}領域`:`${i+1}ページ · 開くと読み込みます`;button.disabled=state.busy;button.onclick=()=>{if(!state.busy)showPage(i);};list.append(button);});$('page-jump').value=state.current+1;}
function documentStatus(){const p=currentPage();if(!p)return;status(`${state.pages.length}ページ · 読込済み ${state.pages.filter(page=>page.loaded).length} / ${state.pages.length} · このページ ${p.blocks.length}文字領域${$('sample-tag').hidden?'':' · サンプル訳文（API通信なし）'}`);}
async function showPage(index,{manageBusy=true}={}){
  if(!Number.isInteger(index)||index<0||index>=state.pages.length)return;
  if(manageBusy)busy(true,`${index+1}ページを読み込み中…`);
  clearTimeout(renderTimer);state.renderVersion++;
  try{
    await state.previewPromise;
    const p=await state.store.ensurePage(index);
    const previous=currentPage();state.current=index;state.selected=null;
    if(previous&&previous!==p)state.store.releasePage(previous);
    renderPages();renderInspector();await renderPreview();documentStatus();updateResumeControls();
  }catch(error){notify(error.message,true);}
  finally{if(manageBusy){busy(false);renderInspector();}}
}
function paperWidth(p){const zoom=$('zoom').value;return zoom==='fit'?Math.max(100,$('original-scroll').clientWidth-(innerWidth<650?14:36)):p.width*Number(zoom);}
function placePaper(container,canvas,p){container.querySelectorAll('canvas').forEach(old=>{old.width=1;old.height=1;});container.replaceChildren(canvas);const width=paperWidth(p);container.style.width=`${width}px`;container.style.height=`${p.height/p.width*width}px`;
  for(const b of p.blocks){const box=document.createElement('button');box.type='button';box.className=`text-box${b.id===state.selected?' selected':''}${b.supported===false?' unsupported':''}${b.fit?.overflow?' overflow':''}`;box.dataset.block=b.id;box.title=b.original;box.setAttribute('aria-label',`文字領域: ${b.original}`);box.style.left=`${b.x/p.width*100}%`;box.style.top=`${b.y/p.height*100}%`;box.style.width=`${b.width/p.width*100}%`;box.style.height=`${b.height/p.height*100}%`;box.onclick=()=>selectBlock(b.id);container.append(box);}
}
function renderPreview(){
  const p=currentPage();if(!p)return Promise.resolve();const token=++state.renderVersion;const store=state.store;
  state.previewPromise=state.previewPromise.then(async()=>{
  if(token!==state.renderVersion)return;
  try {
    await store.ensurePage(p);
    const translated=await renderTranslatedCanvas(p,{scale:1.5});
    if(token!==state.renderVersion){translated.width=1;return;}
    const original=document.createElement('canvas');original.width=p.canvas.width;original.height=p.canvas.height;original.getContext('2d').drawImage(p.canvas,0,0);
    placePaper($('original-paper'),original,p);placePaper($('translated-paper'),translated,p);
    $('preview').classList.toggle('hide-boxes',!$('show-boxes').checked);
    const translatedCount=p.blocks.filter(b=>b.translation&&b.enabled&&b.supported!==false).length;
    $('translated-count').textContent=`${translatedCount} / ${p.blocks.length}`;
    const overflow=p.blocks.filter(b=>b.fit?.overflow&&b.translation&&b.enabled).length;
    $('quality').textContent=overflow?`要調整 ${overflow}領域`:p.blocks.length?'文字位置をクリックして編集':'';
    updateBlockStatus();
  }catch(error){notify(error.message,true);}
  });
  return state.previewPromise;
}
function selectBlock(id){if(state.busy)return;state.selected=id;document.querySelectorAll('.text-box').forEach(b=>b.classList.toggle('selected',b.dataset.block===id));renderInspector();updateResumeControls();}
function renderInspector(){const b=selectedBlock();$('inspector-empty').hidden=Boolean(b);$('block-editor').hidden=!b;if(!b)return;
  $('block-number').textContent=`領域 ${b.id.split('-b')[1].padStart(2,'0')}`;$('original-text').value=b.original;$('translation-text').value=b.translation||'';$('block-enabled').checked=b.enabled;
  $('font-size').value=+b.fontSize.toFixed(1);$('font-family').value=b.fontFamily?.includes('mono')?'monospace':b.fontFamily==='serif'?'serif':'sans-serif';
  $('text-color').value=toHex(b.color);$('background-color').value=toHex(b.background);
  for(const prop of ['x','y','width','height'])$(`block-${prop}`).value=+b[prop].toFixed(1);
  $('translation-text').disabled=b.supported===false;$('block-enabled').disabled=b.supported===false;
  updateBlockStatus();
}
function toHex(color){if(/^#[0-9a-f]{6}$/i.test(color))return color;const m=String(color).match(/\d+/g);return m?.length>=3?'#'+m.slice(0,3).map(x=>Math.min(255,+x).toString(16).padStart(2,'0')).join(''):'#ffffff';}
function updateBlockStatus(){const b=selectedBlock();if(!b)return;$('block-status').textContent=b.supported===false?'未対応':b.fit?.overflow?'枠から超過':b.translation?'訳文あり':'未翻訳';$('block-warning').textContent=[b.warning,b.backgroundComplex?'背景が複雑です。色や図の重なりを確認してください。':'',b.fit?.overflow?'訳文が収まりません。枠の大きさか訳文を調整してください。':b.fit?`表示サイズ ${b.fit.fontSize.toFixed(1)} pt`:''].filter(Boolean).join(' ');}
function editBlock(){const b=selectedBlock();if(!b||state.busy)return;const p=currentPage();b.translation=$('translation-text').value;b.enabled=$('block-enabled').checked;b.fontSize=Math.min(200,Math.max(4,Number($('font-size').value)||b.fontSize));b.fontFamily=$('font-family').value;b.color=$('text-color').value;b.background=$('background-color').value;
  for(const prop of ['x','y','width','height']){const n=Number($(`block-${prop}`).value);if(Number.isFinite(n))b[prop]=n;}
  b.x=Math.max(0,Math.min(p.width-1,b.x));b.y=Math.max(0,Math.min(p.height-1,b.y));b.width=Math.max(1,Math.min(p.width-b.x,b.width));b.height=Math.max(1,Math.min(p.height-b.y,b.height));
  clearTranslationJob();clearTimeout(renderTimer);renderTimer=setTimeout(renderPreview,180);
}
async function translate({resume=false}={}){
  if(state.busy||!state.pages.length)return;
  const config=translationConfig();
  if(!config.model)return notify('利用するモデル名を入力してください。',true);
  const scope=$('scope').value,contextKey=translationContext(config);
  let progressTimer,job;
  clearTimeout(renderTimer);state.controller=new AbortController();busy(true,'翻訳を準備しています…');$('cancel-button').hidden=false;
  try{
    if(!translationSettingsState.value&&!await loadTranslationSettings())throw Error('接続設定で翻訳設定を読み直してください。サーバーを最新版で起動してください。');
    const settings=translationSettingsState.value;
    config.providerTimeoutMs=1000*(config.provider==='local'?settings.localTimeoutSeconds:settings.openaiTimeoutSeconds);
    if(config.provider==='local'){config.maxBatchBlocks=settings.localBatchBlocks;config.maxBatchCharacters=settings.localBatchCharacters;}
    await state.previewPromise;
    if(!resume&&scope==='all'){
      for(let i=0;i<state.pages.length;i++){
        if(state.controller.signal.aborted)throw new DOMException('翻訳を中止しました。','AbortError');
        const p=state.pages[i];
        try{await state.store.ensurePage(p);}finally{if(p!==currentPage())state.store.releasePage(p);}
        status(`翻訳する文字を読み込み中 ${i+1} / ${state.pages.length} ページ`);$('progress').value=(i+1)/state.pages.length*100;
      }
      renderPages();
    }
    if(resume){
      job=state.translationJob;if(!job||job.contextKey!==contextKey)throw Error('翻訳先・モデル・言語・範囲を元に戻してから再開してください。');
    }else{
      let candidates=scope==='all'?allBlocks():scope==='selected'?[selectedBlock()].filter(Boolean):currentPage().blocks;
      candidates=candidates.filter(b=>b.enabled&&b.supported!==false&&b.original.trim());
      if(!candidates.length){notify('翻訳できる文字領域を選択してください。',true);documentStatus();return;}
      if($('skip-translated').checked)candidates=candidates.filter(b=>!b.translation?.trim());
      if(!candidates.length){const message='未翻訳の領域はありません。訳し直す場合は「訳文がある領域をスキップ」を解除してください。';notify(message);status(message);return;}
      job=createTranslationJob(candidates,contextKey);state.translationJob=job;
    }
    const blocks=getPendingJobBlocks(job),previousCompleted=job.completed.size;
    const snapshot=blocks.map(b=>({id:b.id,text:b.original}));
    const started=performance.now();let latestProgress;
    const updateProgress=()=>{
      if(!latestProgress)return;
      const {completed,batch,totalBatches}=latestProgress;
      status(`翻訳中 ${previousCompleted+completed} / ${job.total} 領域 · バッチ ${Math.max(1,batch)} / ${totalBatches} · ${Math.floor((performance.now()-started)/1000)}秒経過`);
      $('progress').value=job.total?(previousCompleted+completed)/job.total*100:0;
    };
    progressTimer=setInterval(updateProgress,1000);
    await translateBlocks(snapshot,config,{signal:state.controller.signal,onProgress:progress=>{latestProgress=progress;updateProgress();},onBatch:async batch=>{
      applyTranslationBatch(job,batch);await renderPreview();updateResumeControls();
    }});
    clearInterval(progressTimer);
    clearTranslationJob();await renderPreview();status(`${job.total}領域を翻訳しました。文字の収まりと内容を確認してください。`);notify('翻訳が完了しました。');
  }catch(error){
    const message=error.name==='AbortError'?'翻訳を中止しました。':error.message;
    const remaining=job?job.total-job.completed.size:0;
    const retained=job?` ${job.completed.size}領域の完了分を保持しています。${remaining?`残り${remaining}領域は「続きから翻訳」で再開できます。`:''}画面を閉じる前に編集データを保存してください。`:'';
    if(job&&!remaining)clearTranslationJob();notify(message,error.name!=='AbortError');status(message+retained);
  }
  finally{clearInterval(progressTimer);state.controller=null;$('cancel-button').hidden=true;busy(false);renderInspector();}
}
const exportNames = {pdf:'PDF',docx:'Word',pptx:'PowerPoint'};
function updateExportFormat(){
  const format=$('export-format').value;
  $('export-button').textContent=`↓ ${exportNames[format]}を保存`;
  const mixed=state.pages.some(p=>Math.abs(p.width-state.pages[0].width)>.05||Math.abs(p.height-state.pages[0].height)>.05);
  const notes={
    pdf:'全ページを画像PDFに保存します。文字の選択・検索はできません。',
    docx:'元のページ寸法でWordに固定配置します。文字は画像になり、Word上で直接編集できません。',
    pptx:mixed?'ページサイズが混在しています。共通のスライド枠に原寸で中央配置し、余白を設けます。文字は画像になります。':'1ページを1スライドとして原寸で配置します。文字は画像になり、PowerPoint上で直接編集できません。'
  };
  $('export-note').textContent=notes[format];
}
async function saveDocument(){
  if(state.busy||!state.pages.length)return;
  clearTimeout(renderTimer);
  const format=$('export-format').value;
  const name=exportNames[format];
  if(!name)return notify('出力形式を選択してください。',true);
  busy(true,`${name}を書き出しています…`);
  try{
    await state.previewPromise;
    const onProgress=(done,total)=>{$('progress').value=done/total*100;status(`${name}作成中 ${done} / ${total} ページ`);};
    const pageOptions={onProgress,preparePage:p=>state.store.ensurePage(p),releasePage:p=>state.store.releasePage(p)};
    const blob=format==='pdf'?await exportPdf(state.pages,pageOptions):await exportOffice(state.pages,{format,...pageOptions});
    download(blob,state.filename.replace(/\.pdf$/i,'')+`-translated.${format}`);
    status(`${name}を保存しました。ページの外見を画像として固定しています。`);
    notify(`${name}を保存しました。`);
  }catch(error){notify(error.message,true);status(`${name}を書き出せませんでした。領域と用紙寸法を確認してください。`);}
  finally{await renderPreview();renderPages();busy(false);renderInspector();}
}
const editable=['translation','enabled','x','y','width','height','fontSize','fontFamily','color','background'];
function saveProject(){if(state.busy||!state.pages.length)return;const data={format:'pdf-lingua-1',hash:state.hash,filename:state.filename,pageCount:state.pages.length,pages:state.pages.filter(p=>p.loaded).map(p=>({pageNumber:p.pageNumber,blocks:p.blocks.map(b=>Object.fromEntries(['id',...editable].map(k=>[k,b[k]])))}))};download(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),state.filename.replace(/\.pdf$/i,'')+'.lingua.json');notify('編集データを保存しました。同じPDFを開いて復元できます。');}
async function restoreProject(file){if(!file||state.busy)return;busy(true,'編集データを読み込み中…');clearTimeout(renderTimer);try{await state.previewPromise;if(file.size>100*1024*1024)throw Error('100MB以内の編集データを選択してください。');const data=JSON.parse(await file.text());if(data.format!=='pdf-lingua-1'||data.hash!==state.hash||!Array.isArray(data.pages))throw Error('このPDFの編集データではありません。同じ元PDFを開いてください。');const updates=[],seenPages=new Set();
  for(const p of data.pages){const target=Number.isInteger(p.pageNumber)?state.pages[p.pageNumber-1]:null;if(!target||!Array.isArray(p.blocks)||seenPages.has(p.pageNumber))throw Error('編集データのページ形式が不正です。');seenPages.add(p.pageNumber);if(!p.blocks.length)continue;
    try{
      await state.store.ensurePage(target);const seenBlocks=new Set(),byId=new Map(target.blocks.map(b=>[b.id,b]));
      for(const saved of p.blocks){const b=byId.get(saved.id);if(!b||seenBlocks.has(saved.id))throw Error('編集領域が一致しません。');seenBlocks.add(saved.id);const values={};for(const key of editable){const value=saved[key];if(['x','y','width','height','fontSize'].includes(key)){if(!Number.isFinite(value)||value<0||value>10000)throw Error('編集データの寸法が不正です。');}else if(key==='enabled'){if(typeof value!=='boolean')throw Error('編集データの形式が不正です。');}else if(typeof value!=='string'||value.length>50000)throw Error('編集データの文字列が不正です。');values[key]=value;}if(!['sans-serif','serif','monospace'].includes(values.fontFamily))values.fontFamily=b.fontFamily;if(!/^#[0-9a-f]{6}$/i.test(values.color)||!/^#[0-9a-f]{6}$/i.test(values.background))throw Error('編集データの色が不正です。');if(values.x+values.width>target.width+.1||values.y+values.height>target.height+.1||values.width<1||values.height<1||values.fontSize<4||values.fontSize>200)throw Error('編集データがページの範囲外です。');updates.push([b,values]);}
    }finally{if(target!==currentPage())state.store.releasePage(target);}
    status(`編集データを読み込み中 ${seenPages.size} / ${data.pages.length} ページ`);$('progress').value=seenPages.size/data.pages.length*100;
  }
  updates.forEach(([b,values])=>Object.assign(b,values));clearTranslationJob();await renderPreview();renderInspector();notify('編集データを復元しました。');
}catch(error){notify(error.message,true);}finally{$('project-input').value='';renderPages();documentStatus();busy(false);renderInspector();}}
function applySampleTranslation(){
  const pairs=[['FIELDNOTES / RESEARCH BRIEF','FIELDNOTES / 調査レポート'],['Design across languages','言語を超えるデザイン'],['Keep the structure. Make the message travel.','構成を保ち、伝えたいことを世界へ。'],['OCTOBER 2026','2026年10月'],['A document is more than its words.','文書は、言葉だけではできていない。'],['Every page carries a visual rhythm: a heading, a quiet margin,','見出し、余白、そして画像。'],['an image, and the precise relationship between them.','その配置が、ページのリズムをつくります。'],['Translation should respect that rhythm.','翻訳も、そのリズムを大切に。'],['THE APPROACH','翻訳の考え方'],['Translate the text. Preserve the composition.','言葉を訳し、構成を保つ。'],['Review each region before exporting your translated PDF.','領域ごとに確認して、翻訳PDFに保存しましょう。'],['Built for a careful review','細部まで、確かめながら'],['ELEMENT','要素'],['WHAT TO CHECK','確認するポイント'],['Page geometry','ページ構成'],['Size, spacing, and alignment','大きさ、余白、位置'],['Typography','文字の表現'],['Font size and text fit','文字サイズと枠への収まり'],['Visual context','視覚的な要素'],['Images, rules, and background colors','画像、罫線、背景色'],['A little care makes every language feel at home.','どの言語でも、自然に伝わる文書へ。']];
  const map=new Map(pairs);allBlocks().forEach(b=>{if(map.has(b.original))b.translation=map.get(b.original);});
}

for(const id of ['upload-button','empty-upload'])$(id).onclick=()=>$('file-input').click();
$('file-input').onchange=e=>readFile(e.target.files[0]);$('demo-button').onclick=()=>{if(!state.busy)openDocument(createDemoPdf(),'fieldnotes-sample.pdf',true);};
$('settings-button').onclick=()=>{$('settings-dialog').showModal();if(!proxyState.loaded)loadProxySettings();else updateProxyControls();if(!translationSettingsState.value)loadTranslationSettings();else updateTranslationSettingsControls();};$('provider').onchange=()=>updateProvider();
for(const id of ['model','source-language','target-language','scope','api-mode','reasoning'])$(id).addEventListener('input',updateResumeControls);
for(const id of Object.values(translationSettingFields))$(id).addEventListener('input',()=>translationSettingsStatus('変更はまだ保存されていません。'));
$('translation-settings-save').onclick=saveTranslationSettings;$('translation-settings-reload').onclick=loadTranslationSettings;
$('proxy-mode').onchange=()=>{if($('proxy-mode').value==='custom'&&!proxyState.noProxyInitialized){$('proxy-no-proxy').value='localhost,127.0.0.1,::1,.internal';proxyState.noProxyInitialized=true;}proxyStatus('変更はまだ保存されていません。');updateProxyControls();};
$('proxy-no-proxy').addEventListener('input',()=>{proxyState.noProxyInitialized=true;});
for(const id of ['proxy-url','proxy-no-proxy','proxy-username','proxy-password','proxy-clear-password','proxy-use-for-local'])$(id).addEventListener('input',()=>{proxyStatus('変更はまだ保存されていません。');updateProxyControls();});
$('proxy-save').onclick=saveProxySettings;$('proxy-reload').onclick=loadProxySettings;
$('translate-button').onclick=()=>translate();$('resume-translation').onclick=()=>translate({resume:true});$('cancel-button').onclick=()=>state.controller?.abort();$('export-button').onclick=saveDocument;$('export-format').onchange=updateExportFormat;
$('show-boxes').onchange=()=>$('preview').classList.toggle('hide-boxes',!$('show-boxes').checked);$('zoom').onchange=()=>{if(!state.busy)renderPreview();};
$('block-editor').onsubmit=e=>e.preventDefault();$('block-editor').addEventListener('input',editBlock);
$('reset-block').onclick=()=>{const b=selectedBlock();if(!b||state.busy)return;const original=currentPage().originalBlocks.find(item=>item.id===b.id);if(original)Object.assign(b,structuredClone(original));clearTranslationJob();renderInspector();renderPreview();};
$('page-jump-form').onsubmit=e=>{e.preventDefault();if(state.busy||!state.pages.length)return;const page=Number($('page-jump').value);if(!Number.isInteger(page)||page<1||page>state.pages.length)return notify(`1〜${state.pages.length}のページ番号を入力してください。`,true);showPage(page-1);};
$('save-project').onclick=saveProject;$('restore-project').onclick=()=>$('project-input').click();$('project-input').onchange=e=>restoreProject(e.target.files[0]);
let sync=false;for(const [a,b] of [['original-scroll','translated-scroll'],['translated-scroll','original-scroll']])$(a).addEventListener('scroll',()=>{if(sync)return;sync=true;$(b).scrollTop=$(a).scrollTop;$(b).scrollLeft=$(a).scrollLeft;requestAnimationFrame(()=>sync=false);});
let dragDepth=0;document.addEventListener('dragover',e=>e.preventDefault());document.addEventListener('dragenter',e=>{e.preventDefault();dragDepth++;document.body.classList.add('drag-over');});document.addEventListener('dragleave',()=>{if(--dragDepth<=0)document.body.classList.remove('drag-over');});document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;document.body.classList.remove('drag-over');readFile(e.dataTransfer.files[0]);});
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(!state.busy)renderPreview();},200);});
window.addEventListener('beforeunload',e=>{if(allBlocks().some(b=>b.translation)){e.preventDefault();e.returnValue='';}});
updateExportFormat();getConfig();loadTranslationSettings();
