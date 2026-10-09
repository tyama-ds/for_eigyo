/* Publication-date evidence lives in its own resumable library. */
(() => {
  'use strict';
  const labels={preparing:'候補を整理中',running:'取得中',pausing:'停止処理中',paused:'一時停止',completed:'取得完了',error:'取得を停止',failed:'取得を停止'};
  const kinds={'published-online':'オンライン公開日','published-print':'冊子発行日',published:'出版日',issued:'発行日'};
  const reasons={year_conflict:'元の出版年と一致する月日がありません',no_precise_date:'月・日を補える情報がありません',existing_date_kept:'既存の日付を保持します',existing_date_conflict:'既存の日付と一致しないため保持します',selected:'補完版に採用します',already_present:'既存の日付と同じです',outside_target:'今回の取得対象外です'};
  const recordStatuses={pending:'未取得',found:'日付を取得済み',not_found:'未収録・出版日なし',error:'取得エラー',no_doi:'DOIを取得できません',outside_target:'対象外'};
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=v=>Number.isFinite(Number(v))&&v!=null?Number(v).toLocaleString('ja-JP'):'—';
  const state={version:0,context:null,library:null,job:null,records:null,error:'',busy:false,timer:null,offset:0};
  let dialog;
  const active=j=>['preparing','running','pausing'].includes(j?.status);
  const validId=v=>typeof v==='string'&&/^[a-f0-9]{32}$/.test(v);
  const current=v=>v===state.version&&Boolean(dialog?.open);
  const field=id=>dialog?.querySelector(`[data-pd-field="${id}"]`);
  function stopPoll(){if(state.timer)clearTimeout(state.timer);state.timer=null;}
  function ensureDialog(){
    if(dialog)return dialog;
    dialog=document.createElement('dialog');dialog.id='publication-dates-dialog';dialog.className='dialog dialog-wide publication-dates-dialog';
    dialog.setAttribute('aria-labelledby','publication-dates-title');document.body.appendChild(dialog);
    dialog.addEventListener('close',()=>{++state.version;stopPoll();state.busy=false;if(field('email'))field('email').value='';});
    dialog.addEventListener('click',handleClick);dialog.addEventListener('change',handleChange);
    return dialog;
  }
  function shell(){
    dialog.innerHTML=`<div class="dialog-header"><div><div class="eyebrow">PUBLICATION DATE LIBRARY</div><h2 id="publication-dates-title">出版日を補い、時間の解像度を上げる。</h2><p class="muted">${esc(state.context.dataset.name)} · ${num(state.context.dataset.paper_count)}論文</p></div><button class="icon-button" data-pd-action="close" aria-label="出版日補完を閉じる">×</button></div><div class="dialog-body pd-body"><p>DOI、またはDOIを含むLinkからCrossrefの出版日を取得します。結果は独立した日付台帳へ保存し、元の論文データとは別に確認できます。</p><div id="pd-library"></div><div class="pd-controls"><label>取得対象<select data-pd-field="target"><option value="missing_month">月が不明の論文</option><option value="missing_day">日まで分からない論文</option><option value="all">DOIのある全論文</option></select></label><label>1回に取得するDOI数<input data-pd-field="batch" type="number" min="1" max="20000" step="1" value="500"></label><label>連絡先メール（任意）<input data-pd-field="email" type="email" maxlength="254" autocomplete="off" placeholder="空欄でも取得できます"></label></div><p class="field-help">初期値500件。取得済みDOIは再利用し、未取得分から再開します。空欄は公開枠、メール指定はCrossrefのpolite枠を利用し、サーバーの応答に合わせて速度を調整します。DOIと入力したメールはCrossrefへ送信します。メールは台帳・.envに保存しません。抄録・本文は送りません。</p><div class="pd-actions"><button class="button button-primary" data-pd-action="start">日付の取得を開始</button><button class="button button-quiet" data-pd-action="resume">続きのバッチを取得</button><button class="button button-quiet" data-pd-action="pause">一時停止</button><button class="text-link" data-pd-action="fresh">別の条件で新しい台帳</button></div><div id="pd-status" aria-live="polite"></div><div id="pd-error" role="alert"></div><div id="pd-results"></div><section class="pd-apply"><h3>確認した日付を、分析用の新データセットへ</h3><div class="pd-controls"><label>日付の採用規則<select data-pd-field="policy"><option value="same_year">元の出版年を保持して補完</option><option value="online_first">オンライン公開日を優先</option><option value="print_first">冊子発行日を優先</option></select></label><label class="pd-check"><input type="checkbox" data-pd-field="overwrite">既存の月日も更新する</label></div><p id="pd-policy-note" class="field-help">初期設定では元の出版年と一致する日付で欠損を補います。年が違う候補は確認事項として残します。</p><button class="button button-primary" data-pd-action="apply">補完版データセットを保存</button><p class="field-help">保存後に補完版を再分析すると、月別・3か月別の時層に反映されます。元データと過去の分析結果は残ります。年のみの情報から月日を推定することはありません。</p></section><p class="pd-limitations">DOIを含まないScopusリンクやCrossref未収録の文献は、自動補完できない場合があります。画面を閉じても実行中のバッチは続きます。PC・サーバーの再起動後は保存した台帳から再開できます。</p></div>`;
  }
  function renderLibrary(){
    const area=dialog.querySelector('#pd-library'),library=state.library;if(!library){area.innerHTML='<p role="status">日付の収録状況を確認しています…</p>';return;}
    const c=library.coverage||{},jobs=library.jobs||[];
    area.innerHTML=`<div class="pd-stats"><div><span>月まで利用可能</span><strong>${num(c.month_or_day)}</strong></div><div><span>月が不明</span><strong>${num(c.missing_month)}</strong></div><div><span>DOIを取得できない</span><strong>${num(c.no_doi)}</strong></div></div>${jobs.length?`<label class="field-label">保存済みの日付台帳<select data-pd-field="history"><option value="">台帳を選択</option>${jobs.map(j=>`<option value="${esc(j.id||j.job_id)}" ${(j.id||j.job_id)===(state.job?.id||state.job?.job_id)?'selected':''}>${esc(String(j.created_at||'').slice(0,19).replace('T',' '))} · ${esc(labels[j.status]||j.status)} · ${num(j.processed_dois)}/${num(j.unique_dois)} DOI</option>`).join('')}</select></label>`:''}`;
  }
  function renderStatus(){
    if(!dialog?.open)return;
    const j=state.job,counts=j?.counts||j||{},running=active(j),id=j?.id||j?.job_id;
    for(const action of ['start','resume','pause','fresh','apply']){
      const button=dialog.querySelector(`[data-pd-action="${action}"]`);if(!button)continue;
      button.hidden=(action==='start'&&!!j)||(action==='resume'&&(!j||j.status==='completed'))||(action==='pause'&&!running)||(action==='fresh'&&!j);
      button.disabled=state.busy||((action==='start'||action==='fresh'||action==='apply')&&running)||(action==='apply'&&!j)||(action==='resume'&&running);
    }
    for(const key of ['target','batch','email','policy','overwrite','history'])if(field(key))field(key).disabled=state.busy||running||(key==='target'&&!!j);
    dialog.querySelector('#pd-error').textContent=state.error;
    if(!j){dialog.querySelector('#pd-status').innerHTML='';return;}
    const total=Number(counts.unique_dois)||0,done=Number(counts.processed_dois)||0;
    dialog.querySelector('#pd-status').innerHTML=`<section class="pd-job"><div class="pd-job-heading"><strong>${esc(labels[j.status]||j.status)}</strong><span>${num(done)} / ${num(total)} DOI</span></div><progress max="${Math.max(1,total)}" value="${Math.min(total,done)}" aria-label="出版日の取得進捗"></progress><div class="pd-job-counts"><span>日付あり ${num(counts.found_dois)}</span><span>未収録・日付なし ${num(counts.not_found_dois)}</span><span>エラー ${num(counts.error_dois)}</span><span>再利用 ${num(counts.cached_dois)}</span><span>残り ${num(counts.remaining_dois)}</span></div>${j.message||j.error?`<p>${esc(j.message||j.error)}</p>`:''}${j.retry_at?`<p>再試行可能時刻：${esc(j.retry_at)}</p>`:''}<p class="field-help">ここでの件数は重複を除いたDOI数です。元の対象論文 ${num(counts.eligible_papers)}件。日付が得られても、年の不一致などで採用できない場合があります。</p>${validId(id)?`<a class="text-link" href="/api/publication-date-jobs/${id}/export?${previewQuery()}" download>独立した日付台帳をCSVで保存 ↗</a>`:''}</section>`;
  }
  function previewQuery(){return new URLSearchParams({policy:field('policy')?.value||'same_year',overwrite_existing:String(!!field('overwrite')?.checked)}).toString();}
  function dateLabel(value){if(!value)return '—';return typeof value==='string'?value:value.publication_date||value.date||String(value.year||'—');}
  function recordRow(p){
    const proposed=p.proposed||{},candidate=proposed.candidate;
    const adopted=proposed.apply?dateLabel(candidate):dateLabel(p.original);
    const candidates=(p.candidates||[]).map(c=>`<div>${esc(c.date)}<small>${esc(kinds[c.kind]||c.kind)} · ${esc(c.precision)}</small></div>`).join('');
    const observation=p.observation;
    const provenance=observation?.fetched_at?`<small>Crossref取得: ${esc(String(observation.fetched_at).slice(0,19).replace('T',' '))} UTC</small>`:'';
    return `<tr><td><strong>${esc(p.title||p.paper_id)}</strong><small>${esc(p.doi||'DOIなし')}</small></td><td>${esc(dateLabel(p.original))}<small>${esc(p.original?.date_precision||'')}</small></td><td>${candidates||esc(recordStatuses[p.status]||p.status||'未取得')}${provenance}</td><td><strong>${esc(adopted)}</strong><small>${esc(reasons[proposed.reason]||'')}</small>${p.conflict||proposed.conflict?'<span class="pd-conflict">⚠ 元の日付、または候補同士に年・月・日の違いがあります</span>':''}</td></tr>`;
  }
  function renderRecords(){
    const area=dialog.querySelector('#pd-results');if(!state.records){area.innerHTML='';return;}
    const rows=state.records.items||[],offset=Number(state.records.offset)||0,total=Number(state.records.total)||0;
    area.innerHTML=`<section class="pd-preview"><h3>採用する日付のプレビュー</h3><div class="pd-table-wrap"><table><thead><tr><th>論文 / DOI</th><th>元の日付</th><th>取得した日付と定義</th><th>保存後の日付 / 確認事項</th></tr></thead><tbody>${rows.map(recordRow).join('')||'<tr><td colspan="4">表示できる対象論文がありません。</td></tr>'}</tbody></table></div><div class="pd-pagination"><button class="button button-quiet" data-pd-action="previous" ${offset===0?'disabled':''}>前の20件</button><span>${total?num(offset+1):0}–${num(Math.min(total,offset+rows.length))} / ${num(total)}</span><button class="button button-quiet" data-pd-action="next" ${offset+rows.length>=total?'disabled':''}>次の20件</button></div></section>`;
  }
  async function records(version=state.version){
    const id=state.job?.id||state.job?.job_id;if(!validId(id))return;
    const requestedOffset=state.offset,query=previewQuery();
    const value=await state.context.api(`/api/publication-date-jobs/${id}/records?limit=20&offset=${requestedOffset}&${query}`);
    if(!current(version)||(state.job?.id||state.job?.job_id)!==id||state.offset!==requestedOffset||previewQuery()!==query)return;
    state.records=value;renderRecords();
  }
  function refreshRecords(){
    const version=state.version,id=state.job?.id||state.job?.job_id,offset=state.offset,query=previewQuery();
    records(version).catch(error=>{if(current(version)&&(state.job?.id||state.job?.job_id)===id&&state.offset===offset&&previewQuery()===query){state.error=error.message;renderStatus();}});
  }
  async function poll(version=state.version){
    const id=state.job?.id||state.job?.job_id;if(!validId(id)||!current(version))return;
    try{const j=await state.context.api(`/api/publication-date-jobs/${id}`);if(!current(version)||(state.job?.id||state.job?.job_id)!==id)return;state.job=j;renderStatus();if(active(j))state.timer=setTimeout(()=>poll(version),1800);else await records(version);}catch(error){if(current(version)){state.error=error.message;renderStatus();}}
  }
  async function loadJob(id){
    if(!validId(id))return;stopPoll();const version=++state.version;state.records=null;state.error='';state.busy=true;renderStatus();
    try{const j=await state.context.api(`/api/publication-date-jobs/${id}`);if(!current(version))return;if(j.dataset_id!==state.context.dataset.id)throw new Error('対象データセットが一致しません。');state.job=j;if(field('target'))field('target').value=j.target||'missing_month';state.offset=0;renderRecords();await records(version);if(current(version)&&active(j))state.timer=setTimeout(()=>poll(version),1800);}catch(error){if(current(version))state.error=error.message;}finally{if(current(version)){state.busy=false;renderStatus();}}
  }
  async function open(context){
    if(!validId(context.dataset?.id))throw new Error('出版日を補完するデータセットを選択してください。');
    ensureDialog();stopPoll();const version=++state.version;Object.assign(state,{context,library:null,job:null,records:null,error:'',busy:true,offset:0});shell();if(!dialog.open)dialog.showModal();renderLibrary();renderStatus();
    try{const library=await context.api(`/api/datasets/${context.dataset.id}/publication-dates`);if(!current(version))return;state.library=library;renderLibrary();state.busy=false;renderStatus();if(library.jobs?.length)await loadJob(library.jobs[0].id||library.jobs[0].job_id);}catch(error){if(current(version)){state.error=error.message;state.busy=false;renderStatus();}}
  }
  function options(){const batch=Number(field('batch').value),email=field('email').value.trim();if(!Number.isInteger(batch)||batch<1||batch>20000)throw new Error('1回の取得数は1〜20,000の整数で指定してください。');if(email&&!field('email').checkValidity())throw new Error('連絡先メールの形式を確認してください。');return {batch_size:batch,contact_email:email};}
  async function action(name){
    if(state.busy)return;const version=state.version,context=state.context,id=state.job?.id||state.job?.job_id;state.error='';
    try{
      const body=(name==='start'||name==='resume')?options():name==='apply'?{policy:field('policy').value,overwrite_existing:!!field('overwrite').checked}:{};
      if(name==='start')Object.assign(body,{dataset_id:context.dataset.id,target:field('target').value});
      if(name!=='start'&&!validId(id))return;
      state.busy=true;stopPoll();renderStatus();
      const value=await context.api(name==='start'?'/api/publication-date-jobs':`/api/publication-date-jobs/${id}/${name}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!current(version))return;
      if(name==='apply'){if(!validId(value.dataset?.id))throw new Error('補完版データセットを確認できませんでした。');dialog.close();await context.onApplied?.(value);return;}
      state.job=value;state.records=null;state.offset=0;renderRecords();await poll(version);
    }catch(error){if(current(version))state.error=error.message;}finally{if(current(version)){state.busy=false;renderStatus();if(active(state.job)&&!state.timer)state.timer=setTimeout(()=>poll(version),1800);}}
  }
  function handleClick(event){
    const name=event.target.closest('[data-pd-action]')?.dataset.pdAction;if(!name)return;event.preventDefault();
    if(name==='close'){dialog.close();return;}
    if(name==='fresh'&&!active(state.job)){++state.version;stopPoll();state.job=null;state.records=null;state.error='';renderRecords();renderStatus();return;}
    if(name==='previous'||name==='next'){state.offset=Math.max(0,state.offset+(name==='next'?20:-20));refreshRecords();return;}
    action(name);
  }
  function handleChange(event){const key=event.target.dataset.pdField;if(key==='history'){loadJob(event.target.value);return;}if(key==='policy'||key==='overwrite'){dialog.querySelector('#pd-policy-note').textContent=field('policy').value==='same_year'?'元の出版年と一致する日付で欠損を補います。年の異なる候補は採用しません。':'オンライン公開年と冊子発行年が異なる場合、補完版の出版年・年度集計も変わります。再分析時は対象年の範囲も確認してください。';renderStatus();refreshRecords();}}
  window.AtlasPublicationDates=Object.freeze({open});
  if(window.__ATLAS_UI_TEST__)window.__publicationDatesTest={state,open,action,options,loadJob,records,poll,renderRecords,renderStatus,recordRow,esc,active};
})();
