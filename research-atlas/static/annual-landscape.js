/* Annual report orchestration. All scientific text comes from existing chapter reports. */
(function (global) {
  'use strict';
  const array = value => Array.isArray(value) ? value : [];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const count = value => finite(value) ? value.toLocaleString('ja-JP') : '—';
  const share = value => finite(value) ? `${(value * 100).toFixed(1)}%` : '—';
  const yearOf = value => /^\d{4}(?:$|[-Q])/.test(String(value)) ? Number(String(value).slice(0, 4)) : null;
  const finalStatuses = new Set(['generated','not_requested','not_generated','partial','failed','cancelled']);
  const annualCache = new Map();
  const state = {key:null, context:null, landscape:null, preferences:null, version:0, start:null, end:null, transitions:false, busy:false, cancelling:false, report:null, reportId:null, jobId:null, error:'', stage:'', timer:null, waitResolve:null, abort:null};

  function identity(context, preferences) { return [context?.result?.id,preferences?.topic,preferences?.projection,preferences?.scope,preferences?.interval].join('|'); }
  function stopWaiting() {
    if (state.timer != null) global.clearTimeout(state.timer);
    state.timer = null; const resolve = state.waitResolve; state.waitResolve = null; resolve?.();
  }
  function invalidate() {
    ++state.version; state.abort?.abort(); stopWaiting();
    state.busy = false; state.cancelling = false; state.report = null; state.reportId = null; state.jobId = null; state.error = ''; state.stage = ''; state.key = null;
  }
  function sync(context, preferences) {
    const key = identity(context, preferences);
    if (key !== state.key) { invalidate(); state.key = key; state.start = null; state.end = null; state.transitions = false; state.landscape = null; state.annualEntry = null; }
    state.context = context; state.preferences = preferences;
  }
  function annualSource() { return state.preferences?.interval==='year'?state.landscape:state.annualEntry?.data; }
  function ensureAnnualSnapshot() {
    if(state.preferences?.interval==='year')return;
    const context=state.context,prefs={...state.preferences},key=state.key,cacheKey=[context.result.id,prefs.projection,prefs.scope].join('|');
    if(annualCache.has(cacheKey)){state.annualEntry=annualCache.get(cacheKey);return;}
    const entry={loading:true,data:null,error:''};annualCache.set(cacheKey,entry);state.annualEntry=entry;
    while(annualCache.size>8)annualCache.delete(annualCache.keys().next().value);
    Promise.resolve().then(()=>context.api(`/api/results/${encodeURIComponent(context.result.id)}/landscape?projection=${encodeURIComponent(prefs.projection||'auto')}&interval=year&scope=${encodeURIComponent(prefs.scope||'sample')}`)).then(data=>{
      if(data?.result_id!==context.result.id||!Array.isArray(data.periods)||!data.projection_id||(data.scope||data.meta?.scope||'sample')!==(prefs.scope||'sample')||data.interval&&data.interval!=='year')throw new Error('年別の分析範囲を確認できませんでした。');
      entry.data=data;
    }).catch(error=>{entry.error=error.message||'年別の分析範囲を取得できませんでした。';}).finally(()=>{
      entry.loading=false;if(state.key!==key||state.annualEntry!==entry)return;
      const bounds=availableYears(state.context,state.landscape);if(state.start==null)state.start=bounds.min;if(state.end==null)state.end=bounds.max;render();
    });
  }
  function availableYears(context, landscape) {
    const source=state.preferences?.interval==='year'?landscape:state.annualEntry?.data;
    const actual = array(source?.periods).map(period => yearOf(period.id)).filter(year => year != null);
    return {min:actual.length?Math.min(...actual):null,max:actual.length?Math.max(...actual):null};
  }
  function rangeError() {
    if(state.preferences?.interval!=='year'&&state.annualEntry?.loading)return '年別の分析範囲を準備しています。';
    if(state.preferences?.interval!=='year'&&state.annualEntry?.error)return state.annualEntry.error;
    const bounds = availableYears(state.context,state.landscape);
    if (!Number.isInteger(state.start) || !Number.isInteger(state.end) || bounds.min == null || bounds.max == null) return '比較できる出版年がありません。';
    if (state.start > state.end) return '開始年を終了年以前にしてください。';
    if (state.start < bounds.min || state.end > bounds.max) return `${bounds.min}〜${bounds.max}年の範囲を選んでください。`;
    if (state.end - state.start >= 50) return '一度に作成できるのは50年以内です。';
    return '';
  }
  function estimate() {
    const prefs = state.preferences || {}, years = new Set(array(annualSource()?.centroids).filter(row => row.topic_id === prefs.topic && row.count > 0).map(row => yearOf(row.period_id)).filter(year => year != null && year >= state.start && year <= state.end));
    const n = years.size, comparisons = state.transitions ? Math.max(0,n - 1) : 0;
    return {years:n,comparisons,calls:prefs.provider === 'none' ? 0 : n + comparisons};
  }
  function payload() {
    const prefs = state.preferences || {}, error = rangeError();
    if (!state.context?.result?.id || !prefs.topic || prefs.topic === 'all') throw new Error('重心を追う話題を1つ選んでください。');
    if (error) throw new Error(error);
    const value = {result_id:state.context.result.id,projection:prefs.projection || 'auto',scope:prefs.scope || 'sample',topic_id:prefs.topic,start_year:state.start,end_year:state.end,provider:['local','openai'].includes(prefs.provider) ? prefs.provider : 'none',include_transitions:state.transitions,interval:'year'};
    // This is always the annual snapshot, never the monthly/quarterly map's ID.
    if (annualSource()?.projection_id) value.projection_id = annualSource().projection_id;
    return value;
  }
  function providerName(value) { return ({none:'計算結果のレポート',local:'ローカルLLM',openai:'OpenAI API'})[value] || '計算結果のレポート'; }
  function statusLabel(value) { return ({preparing:'年ごとの根拠を準備中',generating:'評論を生成中',pending:'順番待ち',generated:'評論を作成済み',not_requested:'計算結果',not_generated:'評論未生成（根拠不足）',partial:'一部の評論が未完了',failed:'評論の生成に失敗',cancelled:'生成を停止',skipped:'評論なし'})[value] || '準備中'; }
  function topicLabel() { return array(state.landscape?.topics || state.context?.result?.topics).find(topic => topic.id === state.preferences?.topic)?.label || '話題を選択'; }
  function controlsHTML() {
    const bounds = availableYears(state.context,state.landscape), planned = estimate(), provider = state.preferences?.provider || 'none', error = rangeError(), all = !state.preferences?.topic || state.preferences.topic === 'all';
    const progress = state.report?.progress;
    return `<div class="al-controls"><label>開始年<select data-al-control="start" ${state.busy?'disabled':''} aria-label="年次レポートの開始年">${yearOptions(bounds,state.start)}</select></label><span class="al-range-arrow">→</span><label>終了年<select data-al-control="end" ${state.busy?'disabled':''} aria-label="年次レポートの終了年">${yearOptions(bounds,state.end)}</select></label><label class="al-transition-option"><input type="checkbox" data-al-control="transitions" ${state.transitions?'checked':''} ${state.busy?'disabled':''}><span>年と年の変化も評論<small>各年の評論に、期間間の比較を追加</small></span></label><button type="button" class="button button-primary" data-al-action="generate" ${state.busy||error||all?'disabled':''}>${state.busy?'年ごとに作成中…':'年次レポートを作成'}</button></div><div class="al-run-plan"><span><b>${esc(topicLabel())}</b> · ${state.preferences?.scope==='full'?'全対象論文':'表示標本'} · 年別</span><span>${esc(providerName(provider))}${provider!=='none'?` / 最大 ${count(planned.calls)}回（各年 ${count(planned.years)} ＋ 比較 ${count(planned.comparisons)}）`: ' / LLM呼び出しなし'}</span></div><p class="al-help">各年は重心付近の最大6論文の抄録から、既存の評論形式で詳しく読み解きます。${provider!=='none'?'評論は1章ずつ生成するため、年数に応じて時間がかかります。抄録のない章は呼び出しません。回数は章の生成単位です。ローカルLLMの入力超過時は、各生成で最大2回再試行します。':'全期間のまとめ・論文数・構成比は計算結果として表示します。'}${state.transitions?'比較は実際に分析できた期間の組だけを対象とします。':''}${state.preferences?.interval!=='year'?'月・四半期表示でも、年だけが分かる論文を含めて年別に再集計します。年別の観測範囲を読み込み、呼び出し回数の上限を見積もっています。':''}</p>${all?'<p class="al-warning">「重心を追う話題」で1つの話題を選ぶと作成できます。</p>':error?`<p class="al-warning">${esc(error)}${state.annualEntry?.error?' <button type="button" class="text-link" data-al-action="retry-snapshot">年別範囲を再取得</button>':''}</p>`:''}${state.busy?`<div class="al-progress" role="status"><div><strong>${esc(state.stage||statusLabel(state.report?.generation_status))}</strong><span>${count(progress?.completed??0)} / ${progress?.total!=null?count(progress.total):'—'}章${progress?.planned_llm_calls!=null?` · LLM ${count(progress.llm_calls??0)} / ${count(progress.planned_llm_calls)}回`:''}</span></div><button type="button" data-al-action="cancel" ${!state.reportId||state.cancelling?'disabled':''}>${state.cancelling?'停止を予約しました':'以降の生成を停止'}</button>${state.cancelling?'<p>現在の処理が終わったところで停止します。作成済みの章は残ります。</p>':''}</div>`:''}${state.error?`<p class="al-warning" role="alert">${esc(state.error)}${state.report?' 作成済みの内容は下に保持しています。':''}</p>`:''}`;
  }
  function yearOptions(bounds, selected) {
    if (bounds.min == null || bounds.max == null) return '<option value="">年不明</option>';
    const years = []; for (let year=bounds.min;year<=bounds.max&&years.length<500;year++) years.push(year);
    return years.map(year=>`<option value="${year}" ${year===selected?'selected':''}>${year}年</option>`).join('');
  }
  function tableHTML(report) {
    const rows = array(report.annual_rows).length ? array(report.annual_rows) : array(report.years), sorted = [...rows].sort((a,b)=>Number(a.year)-Number(b.year));
    const max = Math.max(1,...sorted.map(row=>finite(row.count)?row.count:0));
    return `<div class="al-table-wrap"><table class="al-annual-table"><caption>年間論文数と、同じ年の対象集合に占める構成比</caption><thead><tr><th scope="col">出版年</th><th scope="col">論文数</th><th scope="col">件数の比較</th><th scope="col">構成比</th><th scope="col">その年の対象集合</th></tr></thead><tbody>${sorted.map(row=>{const unknown=row.coverage_status==='no_corpus_observation'||row.period_count===0;return `<tr><th scope="row">${esc(row.year??row.period_id)}</th><td>${unknown?'—':count(row.count)}</td><td class="al-bar-cell"><span class="al-count-track" aria-hidden="true"><i style="width:${Math.max(0,Math.min(100,100*(row.count||0)/max))}%"></i></span>${unknown?'<small>対象集合の観測なし</small>':row.count===0?'<small>この話題の論文なし</small>':''}</td><td>${unknown?'—':share(row.share_of_period)}</td><td>${unknown?'観測なし':`${count(row.period_count)}論文`}</td></tr>`;}).join('')}</tbody></table><p class="al-help">構成比の分母は、同じ年・同じ分析範囲にある全話題の論文です。取得対象のない年は「—」と表示し、研究分野全体の出版数がゼロとは解釈しません。</p></div>`;
  }
  function chapterBody(row) {
    if (['pending','generating'].includes(row.generation_status)) return `<p class="al-chapter-note">${row.generation_status==='generating'?'この章の評論を生成しています。':'この章の評論は順番を待っています。'}</p>`;
    if (row.report) {
      // Embedded chapter IDs need not be independently saved/exportable.
      const chapter = {...row.report,id:null};
      if (global.AtlasLandscape?.renderReport) return global.AtlasLandscape.renderReport(state.context,chapter);
      return '<p class="al-warning">評論の表示機能を読み込めませんでした。ページを再読み込みしてください。</p>';
    }
    if (row.coverage_status==='no_corpus_observation'||row.period_count===0) return '<p class="al-chapter-note">この年は分析対象の論文を観測していません。研究活動がゼロとは判断できず、評論は作成しません。</p>';
    if (row.count===0) return '<p class="al-chapter-note">この年の対象集合には、選択した話題の論文がありません。評論は作成しません。</p>';
    if (row.generation_status==='cancelled') return '<p class="al-chapter-note">この章の生成前に停止しました。</p>';
    if (row.llm_error) return `<p class="al-warning" role="alert">${esc(row.llm_error)}</p>`;
    if (row.generation_status==='skipped') return '<p class="al-chapter-note">評論に使える抄録が不足しています。LLMによる内容の解釈は作成していません。</p>';
    return '<p class="al-chapter-note">この章の評論はまだありません。</p>';
  }
  function yearHTML(row) {
    const unknown=row.coverage_status==='no_corpus_observation'||row.period_count===0, warning=['failed','skipped'].includes(row.generation_status)||row.report?.narrative?.validation?.status==='warning';
    return `<article class="al-year" id="annual-year-${esc(row.year)}"><header><div><span class="al-eyebrow">YEAR IN REVIEW</span><h3>${esc(row.year)}<small>年</small></h3></div><div class="al-year-metrics"><strong>${unknown?'—':count(row.count)}<small>論文</small></strong><span>構成比 ${unknown?'—':share(row.share_of_period)}</span></div><span class="al-status ${warning?'warning':''}">${warning?'⚠ ':''}${esc(statusLabel(row.generation_status))}</span></header>${array(row.terms).length?`<div class="al-terms">${array(row.terms).slice(0,8).map(term=>`<span>${esc(typeof term==='string'?term:term.term)}</span>`).join('')}</div>`:''}<div class="al-chapter-body">${chapterBody(row)}</div></article>`;
  }
  function transitionHTML(row) {
    const from=yearOf(row.from_period),to=yearOf(row.to_period),gap=from!=null&&to!=null&&to-from>1;
    return `<article class="al-transition"><header><span class="al-eyebrow">CHANGE BETWEEN PERIODS</span><h3>${esc(row.from_period)} <span>→</span> ${esc(row.to_period)}</h3><span class="al-status">${esc(statusLabel(row.generation_status))}</span></header>${gap?'<p class="al-help">期間に空白があります。連続する年の比較ではありません。</p>':''}<div class="al-chapter-body">${chapterBody(row)}</div></article>`;
  }
  function reportHTML(report) {
    const children=new Map(array(report.years).map(row=>[Number(row.year),row]));
    const timeline=array(report.annual_rows).length?array(report.annual_rows).map(row=>({...row,generation_status:report.generation_status==='cancelled'?'cancelled':'pending',report:null,...children.get(Number(row.year))})):array(report.years);
    const years=[...timeline].sort((a,b)=>Number(a.year)-Number(b.year)),transitions=[...array(report.transitions)].sort((a,b)=>(yearOf(a.from_period)||0)-(yearOf(b.from_period)||0));
    const total=report.progress?.total,completed=report.progress?.completed;
    const exportURL=`/api/landscape-annual-reports/${encodeURIComponent(report.id)}/export?format=csv`;
    return `<div class="al-report" id="annual-landscape-report"><header class="al-report-header"><div><span class="al-eyebrow">ANNUAL RESEARCH REVIEW</span><h2>${esc(report.topic?.label||topicLabel())}</h2><p>${esc(report.start_year)} — ${esc(report.end_year)} <span>· ${report.scope==='full'?'全対象論文':'表示標本'} · ${esc(providerName(report.provider))}</span></p></div><div class="al-report-actions"><button type="button" data-al-action="print">印刷 / PDF保存</button><a href="${exportURL}" download>CSV ↓</a></div></header><div class="al-report-status"><span>${esc(statusLabel(report.generation_status))}</span><span>${completed!=null&&total!=null?`${count(completed)} / ${count(total)}章`:''}${report.progress?.llm_calls!=null?` · LLM ${count(report.progress.llm_calls)}回`:''}</span></div>${report.overview?.text?`<section class="al-overview"><span class="al-eyebrow">CALCULATED OVERVIEW · 計算結果のまとめ</span><h3>${esc(report.overview.title||'期間全体の観測')}</h3><p>${esc(report.overview.text)}</p><small>年間の件数・構成比・語の変化を計算したまとめです。LLMによる全期間の総合評論ではありません。</small></section>`:''}${years.length||array(report.annual_rows).length?tableHTML(report):'<p class="al-chapter-note">年別の論文数と根拠を準備しています。</p>'}${years.length?`<nav class="al-year-index" aria-label="年次レポートの目次">${years.map(row=>`<a href="#annual-year-${esc(row.year)}">${esc(row.year)}<small>${row.coverage_status==='no_corpus_observation'?'観測なし':`${count(row.count)}論文`}</small></a>`).join('')}</nav>`:''}<div class="al-chapters">${years.map(row=>yearHTML(row)+transitions.filter(item=>yearOf(item.from_period)===Number(row.year)).map(transitionHTML).join('')).join('')}</div>${array(report.limitations).length?`<details class="al-limitations"><summary>分析範囲と留意点</summary>${array(report.limitations).map(item=>`<p>${esc(item)}</p>`).join('')}</details>`:''}</div>`;
  }
  function html() {
    return `<section class="annual-landscape" id="annual-landscape-panel" data-al-key="${esc(state.key)}"><header class="al-panel-heading"><div><span class="al-eyebrow">READ THE FIELD, YEAR BY YEAR</span><h2>年ごとに研究を読み、変化をたどる。</h2><p>年次グラフと、その年の代表論文の評論を1つのレポートにまとめます。</p></div><span class="al-year-symbol" aria-hidden="true">01<span>—</span>12</span></header>${controlsHTML()}${state.report?reportHTML(state.report):'<div class="al-before-run"><span>年次推移</span><i>→</i><span>各年の論文評論</span><i>→</i><span>印刷・保存</span></div>'}</section>`;
  }
  function render() {
    const root=document.querySelector('#annual-landscape-panel');
    if (root&&root.dataset.alKey===state.key) root.outerHTML=html();
  }
  function renderProgress(){const root=document.querySelector('#annual-landscape-panel');if(root?.dataset.alKey===state.key){const label=root.querySelector('.al-progress strong');if(label)label.textContent=state.stage;}}
  function jobStamp(update){const progress=update.progress||{};return JSON.stringify([update.status,update.generation_status,update.projection_id,progress.completed,progress.total,progress.llm_calls,progress.planned_llm_calls,progress.active?.kind,progress.active?.label]);}
  function panel(context,landscape,preferences) {
    sync(context,preferences); state.landscape=landscape;
    ensureAnnualSnapshot();
    const bounds=availableYears(context,landscape);
    if (state.start==null) state.start=bounds.min; if (state.end==null) state.end=bounds.max;
    return html();
  }
  function requestSignature() { return [state.key,state.start,state.end,state.transitions].join('|'); }
  function current(token,signature) { return token===state.version&&signature===requestSignature(); }
  function checkReport(report,request) {
    if (!report||report.result_id!==request.result_id||report.topic?.id!==request.topic_id||report.scope!==request.scope||Number(report.start_year)!==request.start_year||Number(report.end_year)!==request.end_year||report.include_transitions!=null&&report.include_transitions!==request.include_transitions||report.interval&&report.interval!=='year'||report.projection&&report.projection!==request.projection||request.projection_id&&report.projection_id&&report.projection_id!==request.projection_id) throw new Error('別の話題・期間・分析範囲のレポートが返されたため、表示を中止しました。');
    return report;
  }
  function wait() { return new Promise(resolve=>{state.waitResolve=resolve;state.timer=global.setTimeout(()=>{state.timer=null;state.waitResolve=null;resolve();},900);}); }
  async function generate() {
    if (state.busy) return;
    let request;try{request=payload();}catch(error){state.error=error.message;render();return;}
    ++state.version;stopWaiting();state.abort?.abort();state.abort=typeof AbortController==='function'?new AbortController():null;
    const token=state.version,signature=requestSignature(),api=state.context.api,options=state.abort?{signal:state.abort.signal}:{};
    state.busy=true;state.cancelling=false;state.report=null;state.reportId=null;state.jobId=null;state.error='';state.bundleStamp=null;state.stage='年次レポートの作成を開始しています…';render();
    try {
      const job=await api('/api/landscape-annual-reports',{...options,method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
      if(!current(token,signature))return;
      if(!job.job_id||!job.annual_report_id)throw new Error('レポートの識別子を確認できませんでした。');
      state.reportId=job.annual_report_id;state.jobId=job.job_id;render();
      while(current(token,signature)&&state.busy) {
        const update=await api(`/api/jobs/${encodeURIComponent(job.job_id)}`,options);if(!current(token,signature)||!state.busy)return;
        state.stage=update.stage||state.stage;const stamp=jobStamp(update);
        if(state.report&&stamp===state.bundleStamp&&!['failed','completed','cancelled'].includes(update.status)){renderProgress();await wait();continue;}
        const report=await api(`/api/landscape-annual-reports/${encodeURIComponent(state.reportId)}`,options);if(!current(token,signature)||!state.busy)return;
        state.report=checkReport(report,request);state.bundleStamp=stamp;state.stage=update.stage||statusLabel(report.generation_status);
        if(finalStatuses.has(report.generation_status)||['failed','completed','cancelled'].includes(update.status)) {
          state.busy=false;state.cancelling=false;if(update.status==='failed')state.error=update.error||'年次レポートの処理に失敗しました。';render();break;
        }
        render();await wait();
      }
    } catch(error) {if(current(token,signature)&&error?.name!=='AbortError'){state.error=error.message||'年次レポートを取得できませんでした。';state.busy=false;state.cancelling=false;render();}}
  }
  async function cancel() {
    if(!state.busy||!state.reportId||state.cancelling)return;
    const token=state.version,signature=requestSignature(),id=state.reportId;state.cancelling=true;render();
    try {
      const response=await state.context.api(`/api/landscape-annual-reports/${encodeURIComponent(id)}/cancel`,{method:'POST'});
      if(!current(token,signature))return;
      if(response.report)state.report=checkReport(response.report,payload());
      if(finalStatuses.has(response.generation_status)){state.busy=false;state.cancelling=false;stopWaiting();}
      render();
    } catch(error){if(current(token,signature)){state.cancelling=false;state.error=error.message||'停止を予約できませんでした。';render();}}
  }
  function handleChange(event) {
    const control=event.target.dataset?.alControl;if(!control)return;
    ++state.version;state.abort?.abort();stopWaiting();state.busy=false;state.cancelling=false;state.report=null;state.reportId=null;state.error='';
    if(control==='start')state.start=Number(event.target.value);
    if(control==='end')state.end=Number(event.target.value);
    if(control==='transitions')state.transitions=!!event.target.checked;
    render();
  }
  function handleClick(event) {
    const target=event.target.closest?.('[data-al-action]');if(!target)return;
    if(target.dataset.alAction==='generate')generate();
    if(target.dataset.alAction==='cancel')cancel();
    if(target.dataset.alAction==='retry-snapshot'){annualCache.delete([state.context.result.id,state.preferences.projection,state.preferences.scope].join('|'));ensureAnnualSnapshot();render();}
    if(target.dataset.alAction==='print'&&state.report){
      cleanupPrint();const root=document.createElement('div');root.id='annual-landscape-print';root.innerHTML=reportHTML(state.report);
      root.querySelectorAll('.al-limitations').forEach(details=>{details.open=true;});document.body.appendChild(root);document.body.classList.add('annual-printing');
      try{global.print();}catch(error){cleanupPrint();throw error;}
    }
  }
  function cleanupPrint(){document.querySelector('#annual-landscape-print')?.remove();document.body.classList.remove('annual-printing');}
  document.addEventListener('click',handleClick);document.addEventListener('change',handleChange);
  global.addEventListener('afterprint',cleanupPrint);
  global.addEventListener('atlas:result',event=>{if(!event.detail||event.detail.id!==state.context?.result?.id)invalidate();});
  global.AtlasAnnualLandscape=Object.freeze({panel,sync,invalidate,setProvider:()=>render()});
  if(global.__ATLAS_UI_TEST__)global.__annualLandscapeTest={state,panel,sync,invalidate,payload,estimate,availableYears,rangeError,reportHTML,tableHTML,yearHTML,transitionHTML,checkReport,generate,cancel,handleChange,handleClick,html};
})(window);
