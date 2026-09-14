/* Search-service output UI. Shares the application state; exports do not mutate a saved query. */
function queryCard(q) {
 if(!q)return '<div class="empty-state"><div class="empty-orbit">⌘</div><h3>分類を選んで初案をつくりましょう。</h3><button class="button primary" id="back-class">Step 00へ →</button></div>';
 if(!formatCatalog.some(f=>f.id===outputFormat))outputFormat='jplatpat';
 const terms=[...new Set([...(q.keywords||[]),...(q.include_terms||[]),...(q.exclude_terms||[])])], edits=outputEdits.get(q.id)||{};
 return `<div id="query-output" data-query-id="${esc(q.id)}"><div class="panel-heading"><h3>検索式 <span class="pill">v${q.version} · ${esc(q.type)}</span></h3><button class="button small" id="copy-query" disabled>この形式をコピー ⧉</button></div>
 <label class="output-target-label">出力する検索サービス<select id="output-format">${formatCatalog.map(f=>`<option value="${f.id}" ${outputFormat===f.id?'selected':''}>${esc(f.name)}</option>`).join('')}</select></label>
 <div class="output-options"><label>分類範囲<select id="output-descendants"><option value="code">DB標準のコード指定</option><option value="descendants">下位分類も明示する</option></select></label><label>検索する本文の言語<select id="output-language"><option value="auto">語句から自動判定</option><option value="en">英語</option><option value="ja">日本語</option></select></label></div>
 <div id="output-feedback" aria-live="polite"></div><pre class="expression" id="expression">変換しています…</pre><div id="output-instructions" class="notice"></div>
 <details class="output-term-editor"><summary>出力用の語句を編集・英語に置き換える</summary><p class="caption">各行は元の1語句に対応します。空白を含む入力も1つのフレーズとして出力します。元の検索式は変更しません。</p><div class="output-term-grid">${terms.map((t,i)=>`<label><span>${esc(t)}</span><input class="output-term" data-term="${esc(t)}" value="${esc(edits[t]??t)}" aria-label="出力用語句 ${esc(t)}" maxlength="500"></label>`).join('')}</div><button class="button small" id="reset-output-terms" type="button">元の語句に戻す</button></details>
 <label class="check output-risk"><input id="omit-unsupported" type="checkbox">未対応分類を今回の出力だけから外す（検索範囲が変わります）</label><label class="check output-risk"><input id="allow-unverified" type="checkbox">未確認分類を確認のうえ含める</label>
 <div class="output-meta" id="output-meta"></div><div class="button-row output-actions"><a class="button" id="open-search-service" href="#" target="_blank" rel="noopener noreferrer">検索サービスを開く ↗</a><button class="button subtle" id="download-query" disabled>検索条件を保存 ↓</button></div>
 ${typeof classificationChangesHtml==='function'?classificationChangesHtml(q.classification_changes):''}<details class="source-expression"><summary>元の設計式・変更内容</summary><pre class="expression">${esc(q.expression)}</pre><p class="caption">キーワードはAND、分類はOR。追加語はOR群をANDで結び、除外語はNOTにします。</p>${q.removed_terms?.length?`<p class="caption">前回から解除した語：${q.removed_terms.map(esc).join('、')}</p>`:''}${q.known_keep_coverage?`<p class="caption">追加語の条件で残る既知の必要特許：${q.known_keep_coverage.retained} / ${q.known_keep_coverage.total} 件（タイトル・要約の文字列照合）。</p>`:''}</details></div>`;
}
function outputPending() {currentOutput=null;$('#copy-query')?.setAttribute('disabled','');$('#download-query')?.setAttribute('disabled','');for(const selector of ['#output-meta','#output-instructions','#output-feedback'])if($(selector))$(selector).textContent='';$('#open-search-service')?.removeAttribute('href');}
async function requestOutput(q) {
 if(!$('#query-output')||$('#query-output').dataset.queryId!==q.id)return;
 const serial=++exportRequest;outputPending();
 const replacements=Object.fromEntries($$('.output-term').map(el=>[el.dataset.term,el.value]));outputEdits.set(q.id,replacements);
 const body={query_id:q.id,format:$('#output-format').value,replacements,omit_unsupported:$('#omit-unsupported').checked,allow_unverified:$('#allow-unverified').checked,descendants:$('#output-descendants').value==='descendants',language:$('#output-language').value};
 $('#expression').textContent='変換しています…';
 try {
  const result=await api('/query/export',body);
  if(serial!==exportRequest||$('#query-output')?.dataset.queryId!==q.id)return;
  currentOutput=result;
  $('#expression').textContent=result.can_copy?result.expression:'条件を確認すると、この形式の検索式を表示します。';
  $('#output-feedback').innerHTML=(result.problems.length?`<div class="output-problems"><b>出力を保留しています</b><ul>${result.problems.map(p=>`<li>${esc(p)}</li>`).join('')}</ul></div>`:'')+(result.warnings.length?`<details class="output-warnings" ${result.omitted_classifications.length||result.replacements.length?'open':''}><summary>変換上の確認事項 · ${result.warnings.length} 件</summary><ul>${result.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></details>`:'');
  $('#output-instructions').textContent=result.instructions;
  $('#output-meta').innerHTML=`<div class="query-field-row"><span>対象フィールド</span><b>${esc(result.text_scope)}</b></div><p class="caption">${esc(result.classification_note)}</p><p class="caption">${esc(result.verification)} · 照合日 ${esc(result.verified_at)}</p><div class="output-sources">${result.sources.map(s=>`<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)} ↗</a>`).join('')}</div>`;
  $('#open-search-service').href=result.url;$('#open-search-service').textContent='検索サービスを開く ↗';
  $('#copy-query').disabled=!result.can_copy;$('#download-query').disabled=!result.can_copy;
 } catch(error) {
  if(serial!==exportRequest||$('#query-output')?.dataset.queryId!==q.id)return;
  $('#expression').textContent='出力を保留しています。';$('#output-feedback').innerHTML=`<div class="output-problems">${esc(error.message)}</div>`;
 }
}
function bindQuery(q) {
 on('#back-class','click',()=>setStep(0));if(!q)return;
 requestOutput(q);
 on('#output-format','change',async()=>{outputFormat=$('#output-format').value;localStorage.setItem('patent-output-format',outputFormat);$('#omit-unsupported').checked=false;$('#allow-unverified').checked=false;$('#output-descendants').value='code';await requestOutput(q);});
 for(const selector of ['#output-descendants','#output-language','#omit-unsupported','#allow-unverified'])on(selector,'change',()=>requestOutput(q));
 let editTimer;$$('.output-term').forEach(el=>el.addEventListener('input',()=>{++exportRequest;outputPending();clearTimeout(editTimer);editTimer=setTimeout(()=>requestOutput(q),350);}));
 on('#reset-output-terms','click',async()=>{$$('.output-term').forEach(el=>el.value=el.dataset.term);await requestOutput(q);});
 on('#copy-query','click',async()=>{if(!currentOutput?.can_copy||currentOutput.query_id!==q.id)throw new Error('出力条件を確認してください。');await navigator.clipboard.writeText(currentOutput.expression);toast(currentOutput.name+' 形式をコピーしました。');});
 on('#download-query','click',()=>{const r=currentOutput;if(!r?.can_copy||r.query_id!==q.id)throw new Error('出力条件を確認してください。');download(`patent-query-v${q.version}-${r.format_id}.txt`,`${r.name} / v${q.version}\n\n${r.expression}\n\n${r.instructions}\n対象: ${r.text_scope}\n分類: ${r.classification_note}\n\n${r.warnings.join('\n')}\n\n${r.verification} (${r.verified_at})\n${r.sources.map(s=>s.title+' '+s.url).join('\n')}`);});
}
function historyHtml(){return state.queries.length?`<div class="panel query-history"><div class="panel-heading"><h3>検索式の履歴</h3><span class="caption">${state.queries.length} バージョン · 過去の式も形式を選んで出力</span></div>${state.queries.slice().reverse().map(q=>`<div class="history-item"><span class="pill">v${q.version}</span><span>${esc(q.type)}</span><small>${esc(q.created_at)}</small><button class="button subtle history-view" data-id="${q.id}">形式を選んで出力</button></div>`).join('')}</div>`:'';}
function bindHistory(){$$('.history-view').forEach(el=>el.addEventListener('click',()=>{viewedQueryId=el.dataset.id;setStep(1);}));}
