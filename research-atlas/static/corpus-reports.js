/* Durable whole-corpus reading. Only report identifiers are stored in the browser. */
(() => {
  'use strict';
  const list=value=>Array.isArray(value)?value:[];
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const validId=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value);
  const finite=value=>value!==null&&value!==undefined&&Number.isFinite(Number(value));
  const number=value=>finite(value)?Number(value).toLocaleString('ja-JP',{maximumFractionDigits:0}):'—';
  const labels={preparing:'対象論文を準備中',queued:'順番待ち',running:'処理中',paused:'一時停止',completed:'処理完了',error:'処理を停止',failed:'失敗',pending:'未処理',missing:'抄録欠損',partial:'一部のみ完了',not_requested:'未生成',not_available:'根拠不足',stale:'追加抽出前の評論'};
  const reportLabel=report=>report.status==='completed'&&report.partial?'今回の処理完了（部分結果）':labels[report.status]||report.status||'保存済みレポート';
  const s={host:null,context:null,result:null,epoch:0,request:0,paperRequest:0,connectionRequest:0,libraryRequest:0,timer:null,report:null,reports:[],papers:null,busy:false,loading:false,error:'',auditError:'',status:null,statusError:'',provider:'local',model:'',mode:'trial',batch:'100',offset:0,filter:'all',groupLimit:12,listError:''};
  const idOf=value=>value?.id||value?.report_id;
  const active=report=>['preparing','queued','running'].includes(report?.status)||report?.synthesis_status==='running';
  const mounted=()=>Boolean(s.host&&s.host.isConnected!==false&&s.context);
  const current=(epoch,id)=>mounted()&&s.epoch===epoch&&(!id||idOf(s.report)===id);
  const key=id=>`research-atlas:corpus-report:${id}`;
  function remembered(id){try{const value=window.localStorage?.getItem(key(id));return validId(value)?value:null;}catch{return null;}}
  function remember(id){if(!validId(id)||!validId(s.result?.id))return;try{window.localStorage?.setItem(key(s.result.id),id);}catch{}}
  function stopPoll(){if(s.timer!==null)clearTimeout(s.timer);s.timer=null;}
  function unmount(){stopPoll();++s.epoch;++s.request;++s.paperRequest;++s.connectionRequest;++s.libraryRequest;if(s.host){s.host.removeEventListener('click',click);s.host.removeEventListener('change',change);s.host.removeEventListener('input',input);}s.host=null;s.context=null;s.busy=false;s.loading=false;}
  async function request(path,body){return s.context.api(path,body===undefined?undefined:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
  function checkedReport(value){const report=value?.report||value;if(!validId(idOf(report))||report.result_id!==s.result?.id)throw new Error('対象の分析結果とレポートが一致しません。');return report;}
  function adopt(value){s.report=checkedReport(value);remember(idOf(s.report));const position=s.reports.findIndex(item=>idOf(item)===idOf(s.report));if(position>=0)s.reports[position]=s.report;else s.reports.unshift(s.report);}
  function warningText(item){
    if(typeof item==='string')return item;
    const parts=[item?.message||item?.text||JSON.stringify(item)];
    if(list(item?.unmatched_numbers).length)parts.push(`未照合の数値：${list(item.unmatched_numbers).slice(0,8).join(' / ')}`);
    if(list(item?.unmatched_quantities).length)parts.push(`未照合の数値・単位：${list(item.unmatched_quantities).slice(0,8).map(row=>`${row.value} ${row.unit}`).join(' / ')}`);
    if(item?.location)parts.push(`箇所：${item.location}`);
    return parts.join(' · ');
  }
  function notes(items,totalCount){
    const rows=list(items);if(!rows.length)return '';
    const total=Math.max(rows.length,Number(totalCount)||0);
    const seen=new Set(),unique=[];
    // Keep verification failures visible even when many papers share other cautions.
    const ordered=[...rows.filter(row=>['numeric_mismatch','unverified_references','quote_mismatch'].includes(row?.code)),...rows];
    for(const row of ordered){const text=warningText(row);if(!seen.has(text)){seen.add(text);unique.push(text);}if(unique.length===31)break;}
    return `<ul class="cr-notes">${unique.slice(0,30).map(text=>`<li>${esc(text)}</li>`).join('')}</ul>${total>Math.min(30,unique.length)?`<p class="cr-help">注意事項 ${number(total)}件。同内容をまとめ、最大30種類を表示しています。全記録はJSONに保存できます。</p>`:''}`;
  }
  const duration=seconds=>!finite(seconds)?'計測待ち':Number(seconds)<60?`${number(seconds)}秒`:Number(seconds)<3600?`${number(Number(seconds)/60)}分`:`${Number(Number(seconds)/3600).toFixed(1)}時間`;
  function controls(){
    const locked=s.busy||active(s.report),existing=!!s.report,provider=existing?s.report.provider||s.provider:s.provider,model=existing?s.report.model||'ブラウザの既定モデル':s.model;
    return `<section class="cr-panel cr-controls"><div><span class="cr-eyebrow">READ / CHECKPOINT / RESUME</span><h2>抄録を、1件ずつ根拠へ。</h2><p>まず100件を試行し、実測時間を確認できます。処理済みの論文は保存され、続きから再開できます。</p></div><div class="cr-form-grid"><label>LLM接続先<select data-cr-field="provider" ${locked||existing?'disabled':''}><option value="local" ${provider==='local'?'selected':''}>ローカルLLM</option><option value="openai" ${provider==='openai'?'selected':''}>OpenAI API</option></select></label><label>モデル<input data-cr-field="model" value="${esc(existing?model:s.model)}" maxlength="160" placeholder="空欄は接続設定の既定モデル" ${locked||existing?'disabled':''}></label><label>実行範囲<select data-cr-field="mode" ${locked?'disabled':''}><option value="trial" ${s.mode==='trial'?'selected':''}>100件を試行</option><option value="batch" ${s.mode==='batch'?'selected':''}>指定件数ずつ処理</option><option value="all" ${s.mode==='all'?'selected':''}>未処理の全件を連続処理</option></select></label><label>1バッチの件数<input data-cr-field="batch" type="number" min="1" max="20000" step="1" value="${esc(s.mode==='trial'?'100':s.batch)}" ${locked||s.mode==='trial'?'disabled':''}></label></div><p class="cr-help">接続・APIキー・プロキシは既存のブラウザ設定を使います。${provider==='openai'?'実行すると、対象論文の抄録・書誌情報をOpenAI APIへ送信します。':'実行すると、対象論文の抄録・書誌情報を設定したLLMサーバーへ送信します。'}既存レポートは同じ方式・モデルで再開します。</p><div class="cr-actions">${!existing?`<button class="button button-primary" data-cr-action="start" ${locked||s.loading?'disabled':''}>${s.mode==='trial'?'100件で試行を開始':s.mode==='all'?'全件の処理を開始':'指定件数の処理を開始'} ↗</button>`:`<button class="button button-primary" data-cr-action="resume" ${locked||!(Number(s.report.counts?.pending)>0||s.report.status==='error'||!(Number(s.report.counts?.total)>0))?'disabled':''}>未処理分を再開 ↗</button><button class="button button-quiet" data-cr-action="retry" ${locked||!(Number(s.report.counts?.failed)>0)?'disabled':''}>失敗した論文を再試行</button><button class="button button-quiet" data-cr-action="pause" ${s.busy||!active(s.report)?'disabled':''}>一時停止</button><button class="text-link" data-cr-action="new" ${locked?'disabled':''}>別のレポートを作成</button>`}<button class="text-link" data-connection-open>LLM・プロキシ接続設定 ↗</button><button class="text-link" data-cr-action="connection" ${s.busy?'disabled':''}>接続状態を確認</button></div>${s.statusError?`<p class="cr-warning">${esc(s.statusError)}</p>`:s.status?`<p class="cr-help">LocalLLM ${s.status.local?.available?'接続可能':'未接続'} · OpenAI ${s.status.openai?.configured?'設定あり':'未設定'}</p>`:''}<p class="cr-help">画面を離れても処理は続きます。停止は「一時停止」、PC・サーバー再起動後は保存済みレポートから再開してください。</p></section>`;
  }
  function adaptiveHTML(r){
    if(r.provider!=='local')return '';
    const a=r.adaptive||{},extract=a.extraction||{},summary=a.synthesis||{};
    return `<div class="cr-adaptive"><h3>LocalLLMの速度に合わせて自動調整</h3><p class="cr-help">遅い応答に合わせて1回の入力を小さくします。時間切れは分割して再試行し、改善しない場合は完了部分を保存して一時停止します。</p>${a.enabled?`<div class="cr-timing"><span>抄録の分割上限 <b>${number(extract.max_chars)}文字</b></span><span>評論の入力目安 <b>推定${number(summary.max_tokens)}トークン／${number(summary.max_items)}項目</b></span><span>時間切れへの自動対処 <b>累計${number(a.timeout_recoveries)}回</b></span></div><p class="cr-help">1回の応答${duration(a.target_seconds)}を目安に調整します（完了時間の保証ではありません）。時間切れへの再分割・再試行は1論文／1区分の実行につき最大${number(a.max_timeout_recoveries)}回。接続・認証エラーは再分割の対象外です。</p>`:'<p class="cr-help">最初の応答から速度を計測します。調整値はレポートに保存され、再開時にも使われます。</p>'}</div>`;
  }
  function coverage(){
    const r=s.report,c=r.counts||{},total=Number(c.total)||0,complete=Number(c.completed)||0,ratio=total?Math.max(0,Math.min(100,100*complete/total)):0,estimate=r.estimate||{};
    return `<section class="cr-panel cr-coverage"><div class="cr-section-heading"><div><span class="cr-eyebrow">COVERAGE, NOT A CLAIM OF PERFECT UNDERSTANDING</span><h2>${esc(reportLabel(r))}</h2></div><span class="source-pill">${esc(r.provider||'')} ${esc(r.model||'')}</span></div><p class="cr-stage" role="status">${esc(r.stage||'保存済みの処理状況')}</p><div class="cr-counts">${[['total','対象論文'],['completed','抽出完了'],['pending','未処理'],['missing','抄録欠損'],['failed','失敗']].map(([name,label])=>`<div class="cr-count-${name}"><span>${label}</span><strong>${number(c[name])}</strong></div>`).join('')}</div><div class="cr-progress-label"><span>抄録抽出の完了率</span><strong>${ratio.toFixed(1)}%</strong></div><progress max="${Math.max(total,1)}" value="${complete}" aria-label="全論文の抽出完了率"></progress><p class="cr-help">処理した抄録 ${number(c.processed_chars)} / ${number(c.total_chars)}文字 · 保存済み抽出の再利用 ${number(c.cached)}件。全件処理は、すべての内容の正しい理解や知見の完全な抽出を保証しません。論文本文は対象に含みません。</p><div class="cr-timing"><span>経過 <b>${duration(estimate.elapsed_seconds)}</b></span><span>実測対象 <b>${number(estimate.sampled_papers)}件</b></span><span>残り抽出時間の概算（評論時間を含まず） <b>${duration(estimate.remaining_seconds)}</b></span></div><p class="cr-help">時間は実測速度に基づく概算です。抄録の長さ、モデル、失敗再試行により変わります。評論の生成時間は別途必要です。</p>${r.partial?'<p class="cr-warning">部分レポートです。未処理・失敗・抄録欠損を分けて確認してください。処理済みの情報を全資料の結論へ一般化しないでください。</p>':''}${notes(r.warnings)}${r.error?`<p class="cr-error" role="alert">${esc(typeof r.error==='string'?r.error:r.error.message||'処理に失敗しました。')}</p>`:''}</section>`;
  }
  function metricTable(title,rows,name){
    const values=list(rows);if(!values.length)return `<section class="cr-panel"><h3>${title}</h3><p class="cr-help">集計を準備しています。</p></section>`;
    return `<section class="cr-panel"><h3>${title}</h3><div class="cr-table-wrap"><table><thead><tr><th>${name==='year'?'出版年':'分野'}</th><th>対象</th><th>完了</th><th>欠損</th><th>失敗</th><th>未処理</th></tr></thead><tbody>${values.slice(0,40).map(row=>`<tr><td>${esc(row[name]??row.label??row.topic_id??'不明')}</td>${['total','completed','missing','failed','pending'].map(key=>`<td>${number(row[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${values.length>40?`<p class="cr-help">先頭40行を表示。全${number(values.length)}行はCSV／JSONで確認できます。</p>`:''}</section>`;
  }
  function narrativeHTML(value){
    if(!value)return '<p class="cr-help">評論はまだ生成されていません。</p>';
    if(typeof value==='string')return `<p class="cr-prose">${esc(value)}</p>`;
    const validation=value.validation||{},allWarnings=[...list(validation.warnings),...list(value.warnings)],warning=validation.status==='warning'||[...allWarnings,...list(value.warning_summary)].some(item=>typeof item==='object'&&['numeric_mismatch','unverified_references','quote_mismatch'].includes(item?.code));
    return `${warning?'<p class="cr-warning">⚠ 照合に注意が必要です。生成文は保持しています。根拠と照合内容を確認してください。</p>':''}${notes(allWarnings,value.warning_count)}${value.headline?`<h3>${esc(value.headline)}</h3>`:''}${value.text||value.summary?`<p class="cr-prose">${esc(value.text||value.summary)}</p>`:''}${list(value.sections).map(section=>{const refs=list(section.paper_ids||section.evidence_ids),refCount=Math.max(refs.length,Number(section.paper_ids_count)||0),warnings=[...list(section.validation?.warnings),...list(section.warnings)];return `<article class="cr-prose-section"><h4>${section.validation?.status==='warning'||warnings.length||Number(section.warning_count)>0?'⚠ ':''}${esc(section.title||'分析')}</h4><p class="cr-prose">${esc(section.text||'')}</p>${notes(warnings,section.warning_count)}${refCount?`<p class="cr-help">根拠 ${number(refCount)}論文：${refs.slice(0,10).map(esc).join(' / ')}${refCount>10?' …（全IDはJSONに保存）':''}</p>`:''}</article>`;}).join('')}${notes(value.caveats)}${value.preview?'<p class="cr-help">評論本文はそのまま表示し、参照ID・警告・原文記録の一覧を省略しています。全記録はCSV／JSONで確認できます。</p>':''}`;
  }
  function synthesis(){
    const r=s.report,groups=list(r.groups),status=r.synthesis_status||'not_requested';
    return `<section class="cr-panel cr-synthesis"><div class="cr-section-heading"><div><span class="cr-eyebrow">EVIDENCE → YEAR / FIELD → SYNTHESIS</span><h2>全体の評論</h2></div><button class="button button-primary" data-cr-action="synthesize" ${s.busy||active(r)||!(r.has_facts===true||(r.has_facts===undefined&&Number(r.counts?.completed)>0))?'disabled':''}>${['completed','partial','stale'].includes(status)?'評論を再生成':'評論を生成'} ↗</button></div><p class="cr-help">状態：${esc(labels[status]||status)}。抽出した根拠と年・分野別の集計をもとに評論します。${r.partial?'現在は処理済み部分のみを対象に生成します。':''}</p>${status==='stale'?'<p class="cr-warning">追加抽出によって根拠が更新されました。以下は以前の根拠に基づく評論です。再生成してください。</p>':''}${narrativeHTML(r.narrative)}${groups.length?`<div class="cr-group-reports"><h3>年別・分野別の評論</h3>${groups.slice(0,s.groupLimit).map(group=>`<details data-cr-detail="group-${esc(group.kind)}-${esc(group.key)}"><summary>${esc(group.label||group.key||'分析グループ')} <span>${esc(labels[group.status]||group.status||'')} · 根拠 ${number(group.source_count)}件</span></summary>${group.error?`<p class="cr-warning">${esc(group.error)}</p>`:""}${narrativeHTML(group.narrative)}</details>`).join('')}${groups.length>s.groupLimit?`<button class="text-link" data-cr-action="more-groups">残り${number(groups.length-s.groupLimit)}グループを表示</button>`:''}</div>`:''}</section>`;
  }
  function paperHTML(paper){
    const evidence=paper.extraction,serialized=evidence?JSON.stringify(evidence,null,2):'',clipped=serialized.length>100000,facts=list(evidence?.facts),kinds={purpose:'研究目的',material:'材料・対象',condition:'条件',method:'研究手法',result:'結果',limitation:'限界・制約',negative_result:'否定的な結果'};
    const factHTML=fact=>`<article class="cr-fact"><h4>${list(fact.warnings).length?'⚠ ':''}${esc(kinds[fact.kind]||fact.kind||'抽出した根拠')}</h4><p>${esc(fact.statement||'')}</p>${fact.quote?`<blockquote><span>抄録原文</span>${esc(fact.quote)}</blockquote>`:''}${notes(fact.warnings)}</article>`;
    return `<tr><td><strong>${esc(paper.title||paper.id||paper.paper_id)}</strong><small>${esc(paper.year??'出版年不明')} · ${esc(paper.topic_id||'')}</small><button class="text-link" data-cr-action="source" data-cr-paper="${esc(paper.id||paper.paper_id||'')}">原典の抄録を確認 ↗</button></td><td><span class="cr-paper-status cr-paper-${esc(['completed','pending','missing','failed'].includes(paper.status)?paper.status:'pending')}">${esc(labels[paper.status]||paper.status||'未処理')}</span><small>${number(paper.processed_chars)} / ${number(paper.abstract_chars)}文字</small>${paper.error?`<p class="cr-warning">${esc(typeof paper.error==='string'?paper.error:paper.error.message||'抽出に失敗しました')}</p>`:''}</td><td>${facts.slice(0,3).map(factHTML).join('')}${facts.length>3?`<details data-cr-detail="facts-${esc(paper.id||paper.paper_id)}"><summary>根拠をさらに読む（全${number(facts.length)}件／先頭20件を表示）</summary>${facts.slice(3,20).map(factHTML).join('')}${facts.length>20?'<p class="cr-help">21件目以降は下の構造化記録とCSV／JSONで確認できます。</p>':''}</details>`:''}${notes(evidence?.warnings)}${evidence?`<details data-cr-detail="paper-${esc(paper.id||paper.paper_id)}"><summary>構造化した抽出結果（JSON）</summary><pre>${esc(serialized.slice(0,100000))}</pre>${clipped?'<p class="cr-help">表示は先頭100,000文字です。全情報はCSV／JSONで確認できます。</p>':''}</details>`:'<span class="cr-help">抽出結果なし</span>'}</td></tr>`;
  }
  function audit(){
    const page=s.papers,rows=list(page?.items),total=Number(page?.total)||0;
    return `<section class="cr-panel"><div class="cr-section-heading"><div><span class="cr-eyebrow">PAPER-BY-PAPER AUDIT</span><h2>論文ごとの処理・根拠</h2></div><label>処理状態<select data-cr-field="filter">${[['all','すべて'],['completed','抽出完了'],['pending','未処理'],['missing','抄録欠損'],['failed','失敗']].map(([id,label])=>`<option value="${id}" ${s.filter===id?'selected':''}>${label}</option>`).join('')}</select></label></div>${s.auditError?`<p class="cr-error" role="alert">${esc(s.auditError)}</p>`:''}<div class="cr-table-wrap"><table class="cr-audit-table"><thead><tr><th>論文</th><th>処理状態</th><th>抽出した根拠</th></tr></thead><tbody>${rows.map(paperHTML).join('')||`<tr><td colspan="3">${page?'該当する論文はありません。':'処理台帳を読み込んでいます…'}</td></tr>`}</tbody></table></div><div class="cr-pagination"><button class="button button-quiet" data-cr-action="previous" ${s.offset===0?'disabled':''}>前の20件</button><span>${page&&total?number(s.offset+1):0}–${number(Math.min(total,s.offset+rows.length))} / ${number(page?.total)}</span><button class="button button-quiet" data-cr-action="next" ${!page||s.offset+rows.length>=total?'disabled':''}>次の20件</button></div></section>`;
  }
  function render(){
    if(!mounted())return;const r=s.report,id=idOf(r),methods=list(r?.methods),opened=new Set(Array.from(s.host.querySelectorAll?.("details[open][data-cr-detail]")||[],node=>node.dataset.crDetail));
    s.host.innerHTML=`<div class="cr-workspace"><div class="cr-heading"><div><span class="cr-eyebrow">THE WHOLE CORPUS, WITH A TRACEABLE READING RECORD</span><p>入力した抄録全体を処理し、分野と年の変化を読む。</p></div>${validId(id)?`<div class="cr-exports">${window.AtlasReportExports?.render('corpus',id,s.result.id)||''}${(window.AtlasReportExports?['csv','json']:['pdf','csv','json']).map(format=>`<a href="/api/corpus-reports/${id}/export?format=${format}" download>${format.toUpperCase()} ↓</a>`).join('')}</div>`:''}</div><div class="cr-library"><label>保存済みレポート<select data-cr-field="saved" ${s.busy?'disabled':''}><option value="">レポートを選択</option>${s.reports.filter(item=>validId(idOf(item))).map(item=>`<option value="${idOf(item)}" ${idOf(item)===id?'selected':''}>${esc(String(item.created_at||'').slice(0,19).replace('T',' '))} · ${esc(reportLabel(item))} · ${number(item.counts?.completed)} / ${number(item.counts?.total)}件</option>`).join('')}</select></label><button class="button button-quiet" data-cr-action="refresh" ${s.busy?'disabled':''}>状況を更新 ↻</button></div>${s.listError?`<p class="cr-warning">${esc(s.listError)}</p>`:''}${s.result?.meta?.is_demo||r?.is_demo?'<p class="cr-warning">合成・テストデータです。実際の研究動向を示すレポートには使えません。</p>':''}${s.error?`<div class="cr-error" role="alert">${esc(s.error)}<p>保存済みの抽出結果と元の分析は保持されています。</p></div>`:''}${s.loading?'<p class="cr-help" role="status">保存済みレポートを確認しています…</p>':''}${controls()}${r?`${coverage()}${r.provider==='local'?`<section class="cr-panel">${adaptiveHTML(r)}</section>`:''}<div class="cr-aggregate-grid">${metricTable('年ごとの処理状況',r.annual,'year')}${metricTable('分野ごとの処理状況',r.topics,'label')}</div>${methods.length?`<section class="cr-panel"><h3>抽出された研究手法の言及</h3><div class="cr-methods">${methods.slice(0,30).map(row=>`<span>${esc(row.method)} <b>${number(row.paper_count)}論文</b></span>`).join('')}</div><p class="cr-help">上位30件を表示。言及は実験実施や有効性の証明とは異なります。</p></section>`:''}${synthesis()}${audit()}`:'<section class="cr-panel cr-empty"><h3>抽出の記録を積み重ねて、レポートにする。</h3><ol><li>全論文を対象に抄録から研究目的・手法・結果・限界を抽出</li><li>年・分野別の件数と根拠を集計</li><li>年別・分野別から全体の評論へ統合</li></ol><p>代表論文だけの検索・要約とは別の処理です。欠損・失敗・未処理を隠さず、原文と抽出結果をたどれる形で保存します。</p></section>'}</div>`;
    for(const node of s.host.querySelectorAll?.('details[data-cr-detail]')||[])if(opened.has(node.dataset.crDetail))node.open=true;
  }
  function executionOptions(){
    const batch=s.mode==='trial'?100:Number(s.batch);
    if(!['trial','batch','all'].includes(s.mode)||!Number.isInteger(batch)||batch<1||batch>20000)throw new Error('1バッチは1〜20,000件の整数で指定してください。');
    return {batch_size:batch,run_all:s.mode==='all'};
  }
  async function loadPapers(epoch=s.epoch){
    const id=idOf(s.report);if(!validId(id)||!current(epoch,id))return;
    const token=++s.paperRequest,offset=s.offset,filter=s.filter;
    try{const page=await request(`/api/corpus-reports/${id}/papers?offset=${offset}&limit=20${filter==='all'?'':`&status=${encodeURIComponent(filter)}`}`);if(!current(epoch,id)||token!==s.paperRequest||s.offset!==offset||s.filter!==filter)return;s.papers={...page,items:list(page?.items).slice(0,20)};s.auditError='';render();}catch(error){if(current(epoch,id)&&token===s.paperRequest&&s.offset===offset&&s.filter===filter){s.auditError=error.message;render();}}
  }
  function schedule(epoch){stopPoll();if(current(epoch)&&active(s.report))s.timer=setTimeout(()=>{s.timer=null;poll(epoch);},2200);}
  async function poll(epoch=s.epoch){
    const id=idOf(s.report);if(!validId(id)||!current(epoch,id)||s.busy)return;const token=++s.request;
    try{const value=await request(`/api/corpus-reports/${id}`);if(!current(epoch,id)||token!==s.request)return;adopt(value);s.error='';render();await loadPapers(epoch);}catch(error){if(current(epoch,id)&&token===s.request){s.error=error.message;render();}}finally{if(current(epoch,id)&&token===s.request)schedule(epoch);}
  }
  async function selectReport(id){
    if(!validId(id))return;stopPoll();const epoch=++s.epoch;s.busy=true;s.error='';s.auditError='';s.papers=null;s.offset=0;s.groupLimit=12;render();
    try{const value=await request(`/api/corpus-reports/${id}`);if(!current(epoch))return;adopt(value);render();await loadPapers(epoch);}catch(error){if(current(epoch)){s.error=error.message;}}finally{if(current(epoch)){s.busy=false;render();schedule(epoch);}}
  }
  async function loadLibrary(epoch=s.epoch){
    const resultId=s.result.id,token=++s.libraryRequest;
    try{const response=await request(`/api/corpus-reports?result_id=${resultId}`);if(!current(epoch)||s.result.id!==resultId||token!==s.libraryRequest)return;s.reports=list(response?.reports||response?.items||response).filter(item=>!item.result_id||item.result_id===resultId);s.listError='';render();}catch(error){if(current(epoch)&&token===s.libraryRequest){s.listError=error.message;render();}}
  }
  async function loadConnection(epoch=s.epoch){
    const host=s.host,resultId=s.result?.id,token=++s.connectionRequest;
    try{const value=await request('/api/llm/status');if(!mounted()||s.host!==host||s.result?.id!==resultId||token!==s.connectionRequest)return;s.status=value;s.statusError='';render();}catch(error){if(mounted()&&s.host===host&&s.result?.id===resultId&&token===s.connectionRequest){s.statusError=error.message;render();}}
  }
  async function mount(host,result,context){
    unmount();s.host=host;s.context=context;s.result=result;s.report=null;s.reports=[];s.papers=null;s.error='';s.listError='';s.auditError='';s.offset=0;s.filter='all';s.loading=true;s.status=null;s.statusError='';
    window.document?.querySelector?.('#navigation [data-view="corpus-reports"]')?.scrollIntoView?.({block:'nearest',inline:'nearest'});
    host.addEventListener('click',click);host.addEventListener('change',change);host.addEventListener('input',input);const epoch=s.epoch;
    if(!validId(result?.id)){s.loading=false;s.error='分析結果を読み込んでから全件レポートを開いてください。';render();return;}
    render();loadConnection(epoch);await loadLibrary(epoch);if(!current(epoch))return;s.loading=false;
    const saved=remembered(result.id),selected=s.reports.some(item=>idOf(item)===saved)?saved:idOf(s.reports[0]);
    if(validId(selected))await selectReport(selected);else render();
  }
  async function operation(name){
    if(!mounted()||s.busy)return;const epoch=s.epoch,id=idOf(s.report);s.error='';
    try{
      if(name!=='start'&&!validId(id))return;
      let body={};if(['start','resume','retry'].includes(name))body=executionOptions();
      if(name==='start'){if(!['local','openai'].includes(s.provider))throw new Error('LLM接続先を選択してください。');Object.assign(body,{result_id:s.result.id,provider:s.provider});if(s.model.trim())body.model=s.model.trim();}
      if(name==='resume'||name==='retry')body.retry_failed=name==='retry';
      s.busy=true;++s.request;++s.libraryRequest;stopPoll();render();const endpoint=name==='start'?'/api/corpus-reports':`/api/corpus-reports/${id}/${name==='retry'?'resume':name}`;
      let value=await request(endpoint,body);if(!current(epoch))return;
      if(!(value?.report||value)?.result_id){const created=idOf(value);if(!validId(created))throw new Error('レポートの識別子が返されませんでした。');value=await request(`/api/corpus-reports/${created}`);if(!current(epoch))return;}
      adopt(value);s.papers=null;s.offset=0;render();await loadPapers(epoch);
    }catch(error){if(current(epoch))s.error=error.message;}finally{if(current(epoch)){s.busy=false;render();schedule(epoch);}}
  }
  function input(event){const key=event.target.dataset?.crField;if(key==='batch')s.batch=event.target.value;if(key==='model')s.model=event.target.value;}
  function change(event){const key=event.target.dataset?.crField;if(!key)return;if(key==='saved'){selectReport(event.target.value);return;}if(key==='filter'){s.filter=['all','completed','pending','missing','failed'].includes(event.target.value)?event.target.value:'all';s.offset=0;s.papers=null;render();loadPapers();return;}if(key==='provider')s.provider=event.target.value;if(key==='mode')s.mode=event.target.value;input(event);if(key==='provider'||key==='mode')render();}
  function click(event){const target=event.target.closest?.('[data-cr-action]');if(!target)return;event.preventDefault();const name=target.dataset.crAction;if(target.disabled)return;
    if(name==='source'){s.context.showPaper?.(target.dataset.crPaper);return;}
    if(name==='connection'){loadConnection();return;}
    if(name==='new'){if(s.busy||active(s.report))return;++s.epoch;stopPoll();s.report=null;s.papers=null;s.error='';s.auditError='';s.mode='trial';s.batch='100';render();return;}
    if(name==='refresh'){if(s.report)poll();else loadLibrary();return;}
    if(name==='previous'||name==='next'){s.offset=Math.max(0,s.offset+(name==='next'?20:-20));s.papers=null;render();loadPapers();return;}
    if(name==='more-groups'){s.groupLimit+=12;render();return;}
    operation(name);
  }
  window.AtlasCorpusReports=Object.freeze({mount,unmount});
  window.addEventListener('atlas:connections-changed',()=>{if(mounted()){s.status=null;s.statusError='';loadConnection();}});
  window.addEventListener('atlas:result',event=>{if(s.result?.id!==event.detail?.id)unmount();});
  if(window.__ATLAS_UI_TEST__)window.__corpusReportsTest={s,mount,unmount,operation,selectReport,loadPapers,poll,loadLibrary,render,change,input,click,executionOptions,narrativeHTML,paperHTML,coverage,duration,active};
})();
